"""把模型写出的「行内代码评论」回写到 PR 的 diff 上（GitHub / Gitee）。

模型在 review 模式下可以把针对具体代码行的意见写进一个 JSON 文件
（默认工作目录下的 `ai-review-inline.json`），每条形如：
    {"path": "src/foo.py", "line": 42, "side": "RIGHT", "body": "建议..."}
review.py 在 agent 跑完后读取它，按平台把每条变成一次「行内评论」：

- GitHub：官方已弃用 `position`，推荐用 `line` + `side`（文件行号，直观好算）；
- Gitee：接口只认 diff 内的 `position`，这里用 `git diff` 现场把文件行号换算过去。

任何一条失败都只记录日志、不阻断其余评论与总结评论的回写；
这样即便某个平台的定位偏差或个别行找不到，整体评审流程也不会挂。
"""

import json
import re
import subprocess
from pathlib import Path

import requests
from app_auth import (
    auth_headers,
    gitee_auth_headers,
    gitee_inline_comment_url,
    gitee_query,
    github_inline_comment_url,
)

# 模型写出、review.py 读取的约定文件名（位于 agent 工作目录内）
INLINE_COMMENTS_FILE = "ai-review-inline.json"

# Gitee 的 position 从 diff 文件的第一行（`diff --git a/x b/x`）起算为 1；
# 不同 Gitee 版本可能以 `@@` 起算，若实战中行号整体偏移，调这个偏移即可。
GITEE_POSITION_BASE = 1


def read_inline_comments(workspace: Path) -> list[dict]:
    """读取模型写出的行内评论清单；解析失败或缺文件都安全返回空列表。"""
    path = workspace / INLINE_COMMENTS_FILE
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as err:
        print(f"[inline] 无法解析行内评论文件 {path}：{err}")
        return []

    # 允许直接是数组，或 {"comments": [...]} 包裹
    if isinstance(data, dict):
        data = data.get("comments")
    if not isinstance(data, list):
        print("[inline] 行内评论文件格式不正确（应为数组或含 comments 字段的对象）")
        return []

    valid: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        p = item.get("path")
        ln = item.get("line")
        body = item.get("body")
        side = str(item.get("side") or "RIGHT").upper()
        if not p or not isinstance(body, str) or not body.strip():
            continue
        try:
            ln = int(ln)  # type: ignore[assignment]
        except (TypeError, ValueError):
            continue
        if ln <= 0:
            continue
        if side not in ("RIGHT", "LEFT"):
            side = "RIGHT"
        valid.append({"path": str(p), "line": ln, "side": side, "body": body.strip()})
    return valid


def _to_repo_path(path_str: str, workspace: Path, repo_dir: Path) -> str:
    """把模型看到的（相对工作目录的）路径换算成相对仓库根的路径，供 API 使用。"""
    try:
        abs_path = (workspace / path_str).resolve()
        return str(abs_path.relative_to(repo_dir.resolve()))
    except (ValueError, OSError):
        return path_str


def _run_git(args: list[str], cwd: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError:
        raise RuntimeError("宿主机没有 git，无法计算 Gitee 行内评论位置") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("git diff 超时") from None
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "git diff 失败")
    return proc.stdout


def _gitee_diff(base_sha: str, head_sha: str, path: str, repo_dir: Path) -> str:
    """取单文件的 diff；默认上下文找不到行时放大到全文件上下文再试一次。"""
    try:
        return _run_git(["diff", "--no-color", base_sha, head_sha, "--", path], repo_dir)
    except Exception:  # noqa: BLE001 - 放大上下文兜底
        return _run_git(
            ["diff", "--no-color", "--unified=100000", base_sha, head_sha, "--", path],
            repo_dir,
        )


def _gitee_position(diff_text: str, target_line: int, side: str) -> int | None:
    """在 unified diff 里把「文件行号 + side」换算成 Gitee 需要的 position。

    position 从 `diff --git a/x b/x` 这一行起算为 1（见 GITEE_POSITION_BASE）。
    side=RIGHT 命中新文件行号对应的行，side=LEFT 命中旧文件行号对应的行；
    找不到（如该行离改动太远、不在任何 hunk 内）返回 None。
    """
    lines = diff_text.split("\n")
    old_line = new_line = 0
    in_hunk = False
    for idx, raw in enumerate(lines, start=GITEE_POSITION_BASE):
        if raw.startswith("@@"):
            m = re.search(r"-(\d+)(?:,\d+)? \+(\d+)(?:,\d+)?", raw)
            if m:
                old_line = int(m.group(1))
                new_line = int(m.group(2))
                in_hunk = True
            continue
        if not in_hunk:
            continue
        if raw.startswith("+"):
            if side == "RIGHT" and new_line == target_line:
                return idx
            new_line += 1
        elif raw.startswith("-"):
            if side == "LEFT" and old_line == target_line:
                return idx
            old_line += 1
        elif raw.startswith(" "):
            if side == "RIGHT" and new_line == target_line:
                return idx
            if side == "LEFT" and old_line == target_line:
                return idx
            old_line += 1
            new_line += 1
        # 文件头（diff --git / index / --- / +++）与 "\ No newline" 不计入 hunk 行号推进
    return None


def _post_one(
    provider: str,
    repo: str,
    number: int,
    token: str,
    head_sha: str,
    base_sha: str,
    repo_dir: Path,
    comment: dict,
) -> None:
    path = comment["path"]
    body = comment["body"]
    line = comment["line"]
    side = comment["side"]

    if provider == "gitee":
        if not (head_sha and base_sha):
            raise RuntimeError("缺少 base/head sha，无法为 Gitee 计算行内评论位置")
        diff = _gitee_diff(base_sha, head_sha, path, repo_dir)
        position = _gitee_position(diff, line, side)
        if position is None:
            raise RuntimeError(f"在 diff 中找不到 {path}:{line}({side}) 对应的位置")
        resp = requests.post(
            f"{gitee_inline_comment_url(repo, number)}?{gitee_query(token)}",
            headers=gitee_auth_headers(token),
            json={"body": body, "commit_id": head_sha, "path": path, "position": position},
            timeout=30,
        )
    else:
        resp = requests.post(
            github_inline_comment_url(repo, number),
            headers={**auth_headers(token), "Accept": "application/vnd.github+json"},
            json={"body": body, "commit_id": head_sha, "path": path, "line": line, "side": side},
            timeout=30,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")


def post_inline_comments(
    provider: str,
    repo: str,
    number: int,
    token: str,
    head_sha: str,
    base_sha: str,
    repo_dir: Path,
    workspace: Path,
    comments: list[dict],
) -> int:
    """逐条回写行内评论，返回成功条数；单条失败只记日志、不阻断。"""
    ok = 0
    for comment in comments:
        # 先把模型给出的路径换算成相对仓库根，再交给 API
        comment = {**comment, "path": _to_repo_path(comment["path"], workspace, repo_dir)}
        try:
            _post_one(provider, repo, number, token, head_sha, base_sha, repo_dir, comment)
            ok += 1
        except Exception as err:  # noqa: BLE001 - 单条失败不应拖垮整体评审
            print(
                f"[inline] 行内评论回写失败（{comment['path']}:{comment['line']} {comment['side']}）："
                f"{type(err).__name__}: {err}"
            )
    return ok
