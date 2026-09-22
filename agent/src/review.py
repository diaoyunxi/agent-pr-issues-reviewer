"""AI 审查入口：读环境变量 → 取 diff → 跑 Agent → 回写评论。

由中转仓库的 GitHub Actions 调用，工作目录是目标仓库的检出目录。
读代码、发评论优先用 GitHub App / Gitee App 身份，App 不可用时回退个人令牌。
"""

import os
import sys
import traceback

import requests

from app_auth import (
    TokenProvider,
    auth_headers,
    build_token_provider,
    gitee_auth_headers,
    gitee_comment_url,
    gitee_query,
    github_comment_url,
)
from mini_agent import AgentTools, build_agent_from_env

COMMENT_MARKER = "<!-- ai-review-agent -->"


def is_issue() -> bool:
    return os.environ.get("IS_ISSUE", "false").lower() == "true"


def post_comment(provider: str, repo: str, number: int, token: str, body: str, is_issue: bool) -> None:
    """把审查结果写回目标仓库的 PR/Issue 评论区。

    PR 与 Issue 的评论在各自平台都是 issues 端点下的资源，统一走这里，不必再判分支。
    """
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


def main() -> int:
    target_repo = os.environ["TARGET_REPO"]
    pr_number = int(os.environ["PR_NUMBER"])
    provider = os.environ.get("PROVIDER", "github")
    use_issue = is_issue()

    provider_client: TokenProvider = build_token_provider(provider)
    # 令牌只在这里取一次，Agent 的工具调用复用同一个 TokenProvider
    token = provider_client.token()
    print(f"[review] 鉴权身份：{provider_client.source}")

    tools = AgentTools(target_repo, pr_number, provider_client, is_issue=use_issue)
    agent = build_agent_from_env(tools)

    try:
        result = agent.run()
        body = f"{COMMENT_MARKER}\n## 🤖 AI 代码审查\n\n{result}"
    except Exception as err:  # noqa: BLE001 - 需要把任何异常都回报给 PR，避免静默失败
        traceback.print_exc()
        # 失败也要回写，否则发起人不知道任务已经挂了
        body = (
            f"{COMMENT_MARKER}\n## 🤖 AI 代码审查失败\n\n"
            f"任务执行异常：`{type(err).__name__}: {err}`\n\n"
            "请检查 Actions 日志与环境变量配置。"
        )

    try:
        # 长时间跑 Agent 后安装令牌可能已过期，回写前重新取一次
        post_comment(provider, target_repo, pr_number, provider_client.token(), body, use_issue)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
