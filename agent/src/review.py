"""AI 审查入口：解析配置 → 完整克隆上游仓库到 /tmp → 在仓库内跑 bash agent → 回写评论。

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
from git_write import WriteContext
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


def build_writer(cfg, ctx: dict, repo_dir: Path, token: str, provider: str) -> WriteContext:
    """按配置组装写权限上下文。

    - `require_approval=True`（默认）：推到一个新分支 `ai-review/<源分支>`，
      由评审人开 PR 合并——CI 里的自动提交不应该直接落到别人的 PR 分支上；
    - `require_approval=False`：直接推回 PR 源分支 `head_ref`，即「AI 直接改代码」。
    """
    head_ref = ctx["head_ref"] or ""
    if cfg.require_approval:
        # 分支名里不含时间戳，同一 PR 多次运行会更新同一个分支，不会堆一堆分支
        push_branch = f"ai-review/{head_ref}" if head_ref else "ai-review/agent-fix"
    else:
        push_branch = head_ref

    return WriteContext(
        workdir=str(repo_dir),
        token=token,
        provider=provider,
        remote_url=ctx["upstream_url"],
        push_branch=push_branch,
        is_proposal=cfg.require_approval,
    )


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

    # 1) 完整克隆上游仓库到 /tmp 的子目录
    upstream = ctx["upstream_url"]
    print(f"[review] 完整克隆上游仓库：{mask_url(upstream)} → {CLONE_ROOT}")
    try:
        # 完整克隆（全量历史、全部分支），之后再把工作区切到待审查的 head
        repo_dir = clone_repo(
            url=upstream,
            token=token,
            provider=provider,
            branch=ctx["head_ref"],
            base_sha=ctx["base_sha"],
            head_sha=ctx["head_sha"],
            workdir=CLONE_ROOT,
        )
        checkout_head(repo_dir, ctx["head_sha"], ctx["head_ref"])
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

    # 4) 写权限：allow_write 才组装写工具；require_approval=True 时只推到一个新分支，
    #    由人开 PR 合并，不直接动 PR 源分支
    writer = None
    write_mode = ""
    if cfg.allow_write:
        writer = build_writer(cfg, ctx, repo_dir, token, provider)
        write_mode = "proposal" if writer.is_proposal else "direct"
        print(f"[review] 写权限已开启：模式={write_mode} 目标分支={writer.push_branch}")

    try:
        result = run_agent(cfg, shell, build_prompt(ctx, write_mode), workspace, writer)
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
