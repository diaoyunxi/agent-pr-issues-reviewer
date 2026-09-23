"""agent 的工具：在克隆下来的仓库目录里跑 bash，可选带上受控的写操作。

只给一个 bash 工具的取舍见 README：模型拿到 shell，读代码、搜调用方、看 git 历史都靠它，
不用再为「看目录」「读文件」「搜关键字」各写一个 API 工具。
需要「直接改代码」时，bash 不够——模型没法自己拿到脱敏凭据去 push，
所以写操作由 git_write.py 提供的受控工具承担，见那里的说明。

安全边界由三件事兜住：工作目录锁在仓库内、环境变量里剔掉凭据、
单条命令有超时与输出上限。
"""

import subprocess
from dataclasses import dataclass, field

from agents import RunContextWrapper, function_tool

MAX_OUTPUT_CHARS = 30_000
DEFAULT_TIMEOUT = 120.0


@dataclass
class ShellContext:
    """一次 agent 运行的共享状态：工作目录在克隆出来的仓库里。"""

    workdir: str
    timeout: float = DEFAULT_TIMEOUT
    max_output_chars: int = MAX_OUTPUT_CHARS
    env: dict | None = None
    # 交给读代码工具用的额外上下文（当前为空，结构留在此处方便扩展）
    extras: dict = field(default_factory=dict)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n…（输出过长，已截断 {len(text) - limit} 字符）…\n\n{text[-half:]}"


def run_bash(ctx: ShellContext, command: str) -> str:
    """执行一条命令并返回 stdout+stderr；失败不抛异常，把结果交给模型判断。"""
    if not command or not command.strip():
        return "命令为空"

    try:
        proc = subprocess.run(
            ["bash", "-lc", command],
            cwd=ctx.workdir,
            capture_output=True,
            text=True,
            timeout=ctx.timeout,
            env=ctx.env,
        )
    except subprocess.TimeoutExpired:
        return f"命令超时（>{ctx.timeout:.0f}s），请缩小范围后重试"
    except FileNotFoundError:
        return "找不到 bash，无法执行命令"

    parts = []
    if proc.stdout:
        parts.append(proc.stdout)
    if proc.stderr:
        parts.append(f"[stderr]\n{proc.stderr}")
    body = "\n".join(parts).strip() or "（无输出）"
    if proc.returncode != 0:
        body = f"[exit {proc.returncode}]\n{body}"
    return _truncate(body, ctx.max_output_chars)


def build_bash_tool(ctx: ShellContext):
    """把 run_bash 包成 SDK 的 function tool，docstring 即模型看到的工具说明。"""

    @function_tool
    def bash(context: RunContextWrapper[ShellContext], command: str) -> str:
        """在工作目录（已克隆的待审查仓库）里执行一条 bash 命令。

        用于读代码、看 git diff/log、搜索调用方等。支持管道、重定向与 `&&` 串联。
        直接执行 `git commit` / `git push` 可能失败：需要提交或推送时请用专用工具。
        Args:
            command: 要执行的 bash 命令，例如 "git diff --stat" 或 "rg -n 'def foo' src"。
        """
        return run_bash(context.context, command)

    return bash


def build_tools(ctx: ShellContext) -> list:
    """按配置组装只读工具；写权限工具在 git_write.build_write_tools()。"""
    return [build_bash_tool(ctx)]
