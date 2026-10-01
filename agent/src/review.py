"""AI 执行入口：解析配置 → 完整克隆上游仓库到 /tmp → 在仓库内跑 bash agent → 回写评论。

两种模式共用这一条链路，由任务 JSON 的 `mode` 决定行为与提示词：
- `review`：只评审，禁止改仓库（默认）；
- `work`：按评论里的自然语言要求干活，提示词里明确允许 `git commit` / `git push`。

工作流（CI）只需要传两个东西：**上游仓库 URL**（UPSTREAM_REPO）与任务 JSON
（PR/Issue 元数据）。代码由脚本自己 `git clone` 到 /tmp 的子目录，
因此在 CI 里不需要 checkout action，也不要求待审查仓库里有什么特殊文件。

Agent 侧 GitHub 用 App 身份（安装令牌），Gitee 用个人令牌，二者都可在失败时回退个人令牌，
令牌只在内存里用，克隆用的 URL 做了脱敏，不会出现在日志里。
"""

import atexit
import os
import socket
import subprocess
import sys
import time
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
from config import ConfigError, build_agent_config
from executor import is_ready
from inline_comments import (
    INLINE_COMMENTS_FILE,
    post_inline_comments,
    read_inline_comments,
)
from repo import RepoError, checkout_head, clone_repo, mask_url, sanitize_env
from task_context import build_context, build_prompt
from tools import ShellContext

COMMENT_MARKER = "<!-- ai-review-agent -->"
# 评论标题按模式区分，同一 PR 上 review / work 的产出一眼能分清
TITLES = {"review": "## 🤖 AI 代码审查", "work": "## 🤖 AI 执行结果"}
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


def _start_executor():
    """拉起持密钥的执行器 sidecar（独立进程）。AI 的 bash 命令会经它的 socket 转发，
    真令牌只存在于该进程，模型拿不到。返回 Popen，失败返回 None（回退本地执行）。"""
    if os.environ.get("ENABLE_EXECUTOR") == "0":
        return None
    # Linux CI 用 Unix socket；无 AF_UNIX 的平台（如本地 Windows）回退 TCP
    if hasattr(socket, "AF_UNIX") and not os.environ.get("EXECUTOR_PORT"):
        os.environ.setdefault("EXECUTOR_SOCK", "/tmp/executor.sock")
    else:
        os.environ.setdefault("EXECUTOR_PORT", "8731")
    script = Path(__file__).resolve().parent / "executor.py"
    try:
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            env=os.environ.copy(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as err:  # noqa: BLE001 - 执行器起不来就退回本地执行，不阻断审查
        print(f"[review] 执行器启动失败，回退本地执行：{err}")
        return None
    for _ in range(50):
        if is_ready():
            break
        time.sleep(0.1)
    else:
        print("[review] 执行器未及时就绪，回退本地执行")
        _stop_executor(proc)
        return None
    print("[review] 执行器已就绪，AI 的 bash 走独立持密钥进程")
    return proc


def _stop_executor(proc) -> None:
    if not proc or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        proc.kill()


def main() -> int:
    ctx = build_context()
    provider = ctx["provider"]
    is_issue = ctx["is_issue"]
    number = int(ctx["pr_number"] or 0)
    repo = ctx["repo"]
    mode = (ctx.get("mode") or "review").lower()
    if mode not in TITLES:
        mode = "review"
    title = TITLES[mode]
    print(f"[review] 执行模式：{mode}")

    # 启动持密钥的执行器 sidecar；退出时无论成功失败都清理它
    executor = _start_executor()
    if executor:
        atexit.register(_stop_executor, executor)

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
            f"{COMMENT_MARKER}\n{title}失败\n\n"
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
    # work 模式要提交代码：评论触发时任务里没有 base/head sha，必须按分支克隆才能推回
    branch_for_clone = "" if mode == "work" else ctx["head_ref"]
    print(f"[review] 完整克隆上游仓库：{mask_url(upstream)} → {CLONE_ROOT}")
    try:
        # 完整克隆（全量历史、全部分支），之后再把工作区切到待审查的 head
        repo_dir = clone_repo(
            url=upstream,
            token=token,
            provider=provider,
            branch=branch_for_clone,
            base_sha=ctx["base_sha"],
            head_sha=ctx["head_sha"],
            workdir=CLONE_ROOT,
        )
        # work 模式留在克隆出来的默认分支上（要推回源分支）；其余按 head_sha 精确检出
        if mode == "work":
            checkout_head(repo_dir, "", ctx["head_ref"])
        else:
            checkout_head(repo_dir, ctx["head_sha"], ctx["head_ref"])
    except RepoError as err:
        return fail(f"克隆上游仓库失败：`{err}`")

    # 2) 写死的 agent 配置（提示词/参数在代码里），工作目录锁在仓库内
    try:
        cfg = build_agent_config(mode=mode)
        workspace = cfg.resolve_workdir(repo_dir)
    except ConfigError as err:
        return fail(f"agent 配置不可用：`{err}`")
    print(f"[review] agent={cfg.name} 模式={cfg.mode} 工作目录={workspace}")
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
        body = f"{COMMENT_MARKER}\n{title}\n\n{result}"
    except (AgentRunError, Exception) as err:  # noqa: BLE001 - 任何异常都要回报到 PR
        traceback.print_exc()
        body = (
            f"{COMMENT_MARKER}\n{title}失败\n\n"
            f"任务执行异常：`{type(err).__name__}: {err}`\n\n"
            "请检查 Actions 日志与环境变量配置。"
        )

    try:
        # 长时间跑 agent 后安装令牌可能已过期，回写前重新取一次
        token = provider_client.token()
        # review 模式下，把模型写在文件里的「行内代码评论」挂到 PR 的具体代码行上
        if mode == "review" and not is_issue and number and cfg.allow_inline_comments:
            comments = read_inline_comments(workspace)
            if comments:
                posted = post_inline_comments(
                    provider, repo, number, token, ctx["head_sha"], ctx["base_sha"],
                    repo_dir, workspace, comments,
                )
                print(f"[review] 已回写 {posted}/{len(comments)} 条行内评论（文件 {INLINE_COMMENTS_FILE}）")
        return _report(provider, repo, number, token, body, is_issue)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
