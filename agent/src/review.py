"""AI 审查入口：解析配置 → 克隆上游仓库到 /tmp → 在仓库内跑 bash agent → 回写评论。

工作流（CI）只需要传两个东西：**上游仓库 URL**（UPSTREAM_REPO）与任务 JSON
（PR/Issue 元数据）。代码由脚本自己 `git clone` 到 /tmp 的子目录，
因此在 CI 里不需要 checkout action，也不要求待审查仓库里有什么特殊文件。

Agent 侧要么用 App 身份（GitHub 安装令牌 / Gitee 应用令牌），要么回退个人令牌，
令牌只在内存里用，克隆用的 URL 做了脱敏，不会出现在日志里。
"""

import os
import sys
import traceback
from pathlib import Path

import requests

from agent_runner import AgentRunError, run_agent
from app_auth import (
    TokenProvider,
    auth_headers,
    build_token_provider,
    gitee_auth_headers,
    gitee_comment_url,
    gitee_query,
    github_comment_url,
)
from config import ConfigError, load_agent_config
from repo import RepoError, clone_repo, checkout_head, mask_url, sanitize_env
from task_context import build_context, build_prompt
from tools import ShellContext

COMMENT_MARKER = "<!-- ai-review-agent -->"
# 克隆根目录：所有仓库都放在 /tmp 的子目录下，跑完即随容器销毁
CLONE_ROOT = os.environ.get("CLONE_ROOT", "/tmp")


def post_comment(provider: str, repo: str, number: int, token: str, body: str, is_issue: bool) -> None:
    """把审查结果写回目标仓库的 PR/Issue 评论区。"""
    if provider == "gitee":
        resp = requests.post(
            f"{gitee_comment_url(repo, number, is_issue)}?{gitee_query(token)}",
            headers=gitee_auth_headers(token),
            json={"body": body},
            timeout=30,
        )
    else:
        resp = requests.post(
            github_comment_url(repo, number),
            headers=auth_headers(token),
            json={"body": body},
            timeout=30,
        )
    resp.raise_for_status()


def _report(provider: str, repo: str, number: int, token: str, body: str, is_issue: bool) -> int:
    """回写评论；失败只记录日志并返回非零码，不再抛异常。"""
    if not (repo and number):
        print("[review] 缺少目标仓库或编号，跳过回写")
        return 1
    try:
        post_comment(provider, repo, number, token, body, is_issue)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1
    return 0


def main() -> int:
    ctx = build_context()
    provider = ctx["provider"]
    is_issue = ctx["is_issue"]
    number = int(ctx["pr_number"] or 0)
    repo = ctx["repo"]

    provider_client: TokenProvider = build_token_provider(provider)

    try:
        token = provider_client.token()
    except Exception as err:  # noqa: BLE001
        traceback.print_exc()
        # 连令牌都拿不到就没法回写评论，只能靠 workflow 日志
        print(f"[review] 无法获取令牌：{type(err).__name__}: {err}")
        return 1
    print(f"[review] 鉴权身份：{provider_client.source}")

    def fail(reason: str) -> int:
        body = (
            f"{COMMENT_MARKER}\n## 🤖 AI 代码审查失败\n\n"
            f"{reason}\n\n请检查上游仓库 URL、仓库权限与 Actions 日志。"
        )
        print(f"[review] {reason}")
        try:
            # 令牌可能已过期，回写前重新取一次
            return _report(provider, repo, number, provider_client.token(), body, is_issue)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return 1

    # 1) 克隆上游仓库到 /tmp 的子目录
    upstream = ctx["upstream_url"]
    print(f"[review] 克隆上游仓库：{mask_url(upstream)} → {CLONE_ROOT}")
    try:
        repo_dir = clone_repo(
            url=upstream,
            token=token,
            provider=provider,
            branch=ctx["head_ref"] if not (ctx["base_sha"] and ctx["head_sha"]) else "",
            base_sha=ctx["base_sha"],
            head_sha=ctx["head_sha"],
            workdir=CLONE_ROOT,
        )
        checkout_head(repo_dir, ctx["head_sha"])
    except RepoError as err:
        return fail(f"克隆上游仓库失败：`{err}`")

    # 2) 读配置（config.json + prompt.txt），工作目录锁在仓库内
    try:
        cfg = load_agent_config(repo_dir)
        workspace = cfg.resolve_workdir(repo_dir)
    except ConfigError as err:
        return fail(f"agent 配置不可用：`{err}`")
    print(f"[review] agent={cfg.name} 工作目录={workspace}")
    print(f"[review] 系统提示词：{cfg.prompt_file}（{len(cfg.instructions)} 字符）")

    # 3) 跑 agent：只给 bash，环境变量剔掉凭据
    cfg.api_base = os.environ.get("AI_API_BASE", "https://api.openai.com/v1")
    cfg.api_key = os.environ.get("AI_API_KEY", "")
    shell = ShellContext(
        workdir=str(workspace),
        timeout=cfg.bash_timeout,
        max_output_chars=cfg.bash_max_output_chars,
        env=sanitize_env({}, workspace),
    )

    try:
        result = run_agent(cfg, shell, build_prompt(ctx), workspace)
        body = f"{COMMENT_MARKER}\n## 🤖 AI 代码审查\n\n{result}"
    except (AgentRunError, Exception) as err:  # noqa: BLE001 - 任何异常都要回报到 PR
        traceback.print_exc()
        body = (
            f"{COMMENT_MARKER}\n## 🤖 AI 代码审查失败\n\n"
            f"任务执行异常：`{type(err).__name__}: {err}`\n\n"
            "请检查 Actions 日志与环境变量配置。"
        )

    try:
        # 长时间跑 agent 后安装令牌可能已过期，回写前重新取一次
        return _report(provider, repo, number, provider_client.token(), body, is_issue)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
