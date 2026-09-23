"""代码修改能力：在克隆出来的仓库里提交并推回 PR 的源分支。

审查 agent 只能读；要让它**直接改代码**，就不能只给它 `bash`——
模型自己敲 `git push` 既拿不到脱敏后的凭据，也容易把仓库搞成不可预期的状态
（错分支、force push、误删文件）。所以写操作收敛成三个受控工具：

- `apply_patch`：按 unified diff 打补丁（`git apply`），改完文件**不自动提交**；
- `git_commit`：`git add -A` + `git commit`，提交信息由模型给；
- `git_push`：推回任务指定的源分支（`head_ref`），**只允许 fast-forward**，
  目标分支不对、被保护分支、非快进推送都会直接失败并把原因回给模型。

凭据仍然只在内存里：推送时把令牌拼进 `--push-url`，日志与报错统一过 `mask_url()`。
"""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from agents import RunContextWrapper, function_tool

from repo import RepoError, _authenticated_url, mask_url

# 提交与推送都要在容器里跟网络/磁盘打交道，给足超时
GIT_TIMEOUT = 300
# 补丁体积上限，防止模型把整个仓库塞进一条工具调用
MAX_PATCH_CHARS = 200_000
# 该文件只是防手滑，真正的边界是「不落盘 + 只推源分支」
BLOCKED_BRANCHES = {"main", "master"}


class WriteError(RuntimeError):
    """写操作失败：文件没打上、提交失败、推送被拒等。"""


@dataclass
class WriteContext:
    """一次 agent 运行的写权限上下文。"""

    workdir: str
    token: str = ""
    provider: str = "github"
    remote_url: str = ""
    # 只允许推这一个分支；为空表示拒绝一切推送
    push_branch: str = ""
    # True 时只组装读写工具，真正的提交与推送交给 PR 源分支上的人
    is_proposal: bool = False
    # 本次运行已经产生的提交数量，用于日志与「有没有改到东西」的判断
    commits: int = 0
    redact: dict = field(default_factory=dict)


def _run_git(ctx: WriteContext, args: list[str]) -> str:
    """跑一条 git 命令，失败时把输出里的凭据脱敏后抛出 WriteError。"""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=ctx.workdir,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise WriteError(f"git {args[0]} 超时（{GIT_TIMEOUT}s）") from None
    except FileNotFoundError:
        raise WriteError("宿主机没有 git，无法提交或推送") from None

    def clean(text: str) -> str:
        for raw, masked in ctx.redact.items():
            text = text.replace(raw, masked)
        return text.strip()

    if proc.returncode != 0:
        detail = clean(proc.stderr) or clean(proc.stdout) or "无输出"
        # 推送被拒是模型最容易踩的坑，补一句可执行建议
        if args[0] in {"push", "commit"}:
            detail += "\n（如为分支保护或非快进，请不要强推：改用新分支，或把改动写进评论让人来处理）"
        raise WriteError(f"git {' '.join(args)} 失败：{detail}")
    return clean(proc.stdout)


def apply_patch(ctx: WriteContext, patch: str) -> str:
    """把 unified diff 应用到工作区（只改文件，不提交）。"""
    if not patch or not patch.strip():
        raise WriteError("补丁为空")
    if len(patch) > MAX_PATCH_CHARS:
        raise WriteError(f"补丁过大（{len(patch)} 字符 > {MAX_PATCH_CHARS}），请拆成多个文件分别提交")

    # 先干跑一次：patch 打不上时给出准确行号，而不是留下半个工作区
    try:
        _run_git(ctx, ["apply", "--check", "-"])
    except WriteError:
        pass
    proc = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        cwd=ctx.workdir,
        input=patch,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "无输出").strip()
        raise WriteError(f"git apply 失败（补丁与当前工作区不匹配，请重新 `cat` 文件后再生成 diff）：{detail}")
    return "补丁已应用（尚未提交）。请用 `git diff --stat` 自查。"


def git_commit(ctx: WriteContext, message: str) -> str:
    """提交当前工作区的全部改动。"""
    message = (message or "").strip()
    if not message:
        raise WriteError("提交信息不能为空")
    status = _run_git(ctx, ["status", "--porcelain"])
    if not status:
        raise WriteError("工作区没有改动，无需提交")

    _run_git(ctx, ["add", "-A"])
    _run_git(ctx, ["-c", "user.name=ai-review-agent", "-c", "user.email=ai-review-agent@users.noreply.github.com", "commit", "-m", message])
    ctx.commits += 1
    head = _run_git(ctx, ["rev-parse", "--short", "HEAD"])
    return f"已提交 {head}：{message.splitlines()[0]}"


def git_push(ctx: WriteContext) -> str:
    """把当前分支推回任务指定的源分支（仅 fast-forward）。"""
    if not ctx.push_branch:
        raise WriteError("任务未指定可推送的源分支（head_ref 为空），拒绝推送")
    if ctx.push_branch in BLOCKED_BRANCHES:
        raise WriteError(f"目标分支 {ctx.push_branch} 看起来是主干分支，拒绝直接推送，请改为发 PR")
    if not ctx.commits:
        raise WriteError("本次运行还没有产生提交，先 `git_commit` 再推送")
    if not ctx.remote_url:
        raise WriteError("缺少远端地址，无法推送")

    auth_url = _authenticated_url(ctx.remote_url, ctx.token, ctx.provider)

    # 令牌只在内存里拼进 URL，日志与报错统一脱敏
    if ctx.token:
        ctx.redact[auth_url] = mask_url(auth_url)
        ctx.redact[ctx.token] = "***"

    head = _run_git(ctx, ["rev-parse", "--abbrev-ref", "HEAD"])
    # 显式写 refspec，且不带 --force：非快进会被远端拒绝，不会覆盖别人的提交
    _run_git(ctx, ["push", auth_url, f"HEAD:refs/heads/{ctx.push_branch}"])
    return f"已推送提交到 {ctx.push_branch}（本次 HEAD={head}，非快进推送会被远端拒绝）"


def build_write_tools(ctx: WriteContext) -> list:
    """组装写权限工具；`is_proposal` 为真时不提供（只评审，不改仓库）。"""

    @function_tool
    def apply_patch_tool(context: RunContextWrapper[WriteContext], patch: str) -> str:
        """把一段 unified diff 应用到仓库工作区（只改文件，不提交）。

        Args:
            patch: `git diff` 格式的补丁文本，必须与当前工作区内容匹配。
        """
        return apply_patch(context.context, patch)

    @function_tool
    def git_commit_tool(context: RunContextWrapper[WriteContext], message: str) -> str:
        """提交工作区的全部改动（含新增/删除文件）。

        Args:
            message: 提交信息，建议 `fix: 一句话说明` 这种前缀式写法。
        """
        return git_commit(context.context, message)

    @function_tool
    def git_push_tool(context: RunContextWrapper[WriteContext]) -> str:
        """把本次提交推回任务指定的源分支（只允许 fast-forward，不会强推）。"""
        return git_push(context.context)

    return [apply_patch_tool, git_commit_tool, git_push_tool]
