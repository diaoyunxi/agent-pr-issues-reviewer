"""把任务 JSON / 环境变量整理成给模型的第一条消息。

Agent 跑在一个临时克隆出来的仓库目录里（由 CI 把目标仓库 clone 到 /tmp），
本仓库（fork 出来的控制面）只负责登记任务与回写评论，不持有目标仓库代码，
所有业务信息都来自任务 JSON：平台、仓库、PR/Issue 号、标题、描述、上游仓库 URL。
"""

import json
import os
from pathlib import Path

# 描述里经常贴日志/长文，做一次截断，避免第一轮就把上下文撑爆
MAX_BODY_CHARS = 4_000
# work 模式的要求是原文，尽量别截断
MAX_INSTRUCTION_CHARS = 8_000

TASK_FILE_HINT = Path("/tmp/task.json")


def _from_task_file() -> dict:
    """PR 列表页可以直接选一份任务 JSON 跑（workflow_dispatch 的 task_json 输入）。"""
    raw_path = os.environ.get("TASK_JSON", "")
    candidates = [Path(raw_path)] if raw_path else []
    candidates.append(TASK_FILE_HINT)
    for path in candidates:
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
    return {}


def build_context() -> dict:
    """环境变量优先（CI 直接传），其次回落到任务 JSON。"""
    task = _from_task_file()

    def pick(key: str, *aliases: str, default: str = "") -> str:
        for name in (key, *aliases):
            value = os.environ.get(name)
            if value:
                return value
        for name in (key.lower(), *(a.lower() for a in aliases)):
            value = task.get(name)
            if value:
                return str(value)
        return default

    provider = pick("PROVIDER", default="github")
    repo = pick("TARGET_REPO", "REPO", default="")
    upstream = pick("UPSTREAM_REPO", "REPO_URL", "CLONE_URL", default="")

    return {
        "provider": provider,
        "repo": repo,
        "upstream_url": upstream,
        "pr_number": pick("PR_NUMBER", "NUMBER", default=""),
        "is_issue": pick("IS_ISSUE", default="false").lower() == "true",
        "title": pick("TITLE", default=""),
        "body": pick("BODY", "DESCRIPTION", default=""),
        "url": pick("HTML_URL", "URL", default=""),
        "user": pick("USER", "AUTHOR", default=""),
        "action": pick("ACTION", default=""),
        # 执行模式与 work 的自然语言要求
        "mode": (pick("MODE", default="review") or "review").lower(),
        "instruction": pick("INSTRUCTION", default=""),
        "base_sha": pick("BASE_SHA", default=""),
        "head_sha": pick("HEAD_SHA", default=""),
        "base_ref": pick("BASE_REF", default=""),
        "head_ref": pick("HEAD_REF", default=""),
        "task_file": str(TASK_FILE_HINT) if TASK_FILE_HINT.is_file() else "",
    }


def build_prompt(ctx: dict) -> str:
    """拼首条 user 消息：任务描述 + 明确的"先看 diff"指引；work 模式先讲清要做什么。"""
    kind = "Issue" if ctx["is_issue"] else "Pull Request"
    mode = (ctx.get("mode") or "review").lower()
    instruction = (ctx.get("instruction") or "").strip()
    if len(instruction) > MAX_INSTRUCTION_CHARS:
        instruction = f"{instruction[:MAX_INSTRUCTION_CHARS]}\n…（要求过长已截断）"
    body = (ctx["body"] or "").strip()
    if len(body) > MAX_BODY_CHARS:
        body = f"{body[:MAX_BODY_CHARS]}\n…（描述过长已截断）"
    body = body or "（无描述）"

    if mode == "work":
        lines = [
            f"请按要求处理 {ctx['provider']} 仓库 {ctx['repo'] or '(未知)'} 的 {kind} #{ctx['pr_number'] or '?'}。",
            "",
            f"- 标题：{ctx['title'] or '(无标题)'}",
            f"- 发起人：{ctx['user'] or '(未知)'}",
            "",
            "用户的要求：",
            instruction or "（评论里没有给出额外描述，请依据标题与正文自行判断）",
            "",
        ]
    else:
        lines = [
            f"请审查 {ctx['provider']} 仓库 {ctx['repo'] or '(未知)'} 的 {kind} #{ctx['pr_number'] or '?'}。",
            "",
            f"- 标题：{ctx['title'] or '(无标题)'}",
            f"- 发起人：{ctx['user'] or '(未知)'}",
        ]
    if ctx["url"]:
        lines.append(f"- 链接：{ctx['url']}")
    if ctx["head_ref"] or ctx["base_ref"]:
        lines.append(f"- 分支：{ctx['head_ref'] or '?'} → {ctx['base_ref'] or '?'}")
    if ctx["task_file"]:
        lines.append(f"- 完整任务 JSON：{ctx['task_file']}（可用 cat 查看）")

    lines += [
        "",
        "描述：",
        body,
        "",
        "仓库代码已经克隆到当前工作目录，你可以直接用 bash 读代码。",
    ]

    if ctx["is_issue"]:
        if mode == "work":
            lines.append("这是一条 Issue 上的评论任务：没有 diff 可看，按上面的要求处理。")
        else:
            lines.append("这是一条 Issue：没有 diff 可看，请判断描述是否清晰、是否缺信息，需要时给出实现建议。")
    elif ctx["base_sha"] and ctx["head_sha"]:
        lines.append(
            f"本次改动：`git diff {ctx['base_sha'][:12]} {ctx['head_sha'][:12]}`"
            f"（也可用 `git diff origin/base origin/head`），请以此为准。"
        )
    else:
        lines.append("请先用 `git log --oneline -10` 与 `git diff HEAD~1 HEAD` 找到本次改动。")

    return "\n".join(lines)
