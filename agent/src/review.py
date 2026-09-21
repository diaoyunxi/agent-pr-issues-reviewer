"""AI 审查入口：读环境变量 → 取 diff → 跑 Agent → 回写评论。

由中转仓库的 GitHub Actions 调用，工作目录是目标仓库的检出目录。
"""

import os
import sys
import traceback

import requests

from mini_agent import GitHubTools, build_agent_from_env

COMMENT_MARKER = "<!-- ai-review-agent -->"


def is_issue() -> bool:
    return os.environ.get("IS_ISSUE", "false").lower() == "true"


def post_comment(repo: str, number: int, token: str, body: str) -> None:
    """把审查结果写回目标仓库的 PR/Issue 评论区。

    PR 与 Issue 的评论都是 issues 端点下的资源，统一走这里，不必分平台判分支。
    """
    resp = requests.post(
        f"https://api.github.com/repos/{repo}/issues/{number}/comments",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        json={"body": body},
        timeout=30,
    )
    resp.raise_for_status()


def main() -> int:
    target_repo = os.environ["TARGET_REPO"]
    pr_number = int(os.environ["PR_NUMBER"])
    token = os.environ["GITHUB_TOKEN"]

    tools = GitHubTools(target_repo, pr_number, token)
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
        post_comment(target_repo, pr_number, token, body)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
