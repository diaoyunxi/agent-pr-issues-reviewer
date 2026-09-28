"""agent 唯一的工具：在克隆下来的仓库目录里跑 bash。

只给一个工具是有意为之——模型拿到 shell，读代码、搜调用方、看 git 历史都靠它，
不用再为「看目录」「读文件」「搜关键字」各写一个 API 工具。

安全边界：在 CI 里，本工具不自己执行命令，而是把命令发给**持密钥的执行器 sidecar**
（通过 Unix socket），由它在自己的进程里用真令牌运行并把输出脱敏后回传。
这样模型的运行环境里不含任何密钥，连 `git push` / `gh` / `curl` 带令牌都能做，
却永远看不到令牌。无执行器时（本地/测试）回退为直接执行（此时 env 已被剔掉凭据）。
"""

import json
import os
import socket
import struct
import subprocess
from dataclasses import dataclass

from agents import RunContextWrapper, function_tool
from executor import resolve_address

MAX_OUTPUT_CHARS = 30_000
# 命令最大长度限制，防止超长命令导致 shell 参数溢出
MAX_COMMAND_LENGTH = 8192
DEFAULT_TIMEOUT = 120.0


@dataclass
class ShellContext:
    """一次 agent 运行的共享状态：工作目录在克隆出来的仓库里。"""

    workdir: str
    timeout: float = DEFAULT_TIMEOUT
    max_output_chars: int = MAX_OUTPUT_CHARS
    env: dict | None = None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n…（输出过长，已截断 {len(text) - limit} 字符）…\n\n{text[-half:]}"


def _recvall(conn: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def _run_remote(ctx: ShellContext, command: str) -> str:
    """把命令发给执行器 sidecar，拿回脱敏后的 stdout/stderr。"""
    family, addr = resolve_address()
    req = json.dumps({"command": command, "cwd": ctx.workdir, "timeout": ctx.timeout}).encode("utf-8")
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.connect(addr)
            s.sendall(struct.pack(">I", len(req)) + req)
            hdr = _recvall(s, 4)
            if len(hdr) < 4:
                return "执行器连接异常"
            resp = json.loads(_recvall(s, struct.unpack(">I", hdr)[0]).decode("utf-8"))
    except OSError as err:
        return f"执行器连接失败：{err}"

    parts = []
    if resp.get("stdout"):
        parts.append(resp["stdout"])
    if resp.get("stderr"):
        parts.append(f"[stderr]\n{resp['stderr']}")
    body = "\n".join(parts).strip() or "（无输出）"
    if resp.get("exit_code", 0) != 0:
        body = f"[exit {resp['exit_code']}]\n{body}"
    return _truncate(body, ctx.max_output_chars)


def _run_local(ctx: ShellContext, command: str) -> str:
    """无执行器时的本地回退：env 已被剔掉凭据，单条命令有超时与输出上限。"""
    if len(command) > MAX_COMMAND_LENGTH:
        return f"命令过长（{len(command)} 字符），最大允许 {MAX_COMMAND_LENGTH} 字符"
    """无执行器时的本地回退：env 已被剔掉凭据，单条命令有超时与输出上限。"""
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


def run_bash(ctx: ShellContext, command: str) -> str:
    """执行一条命令并返回 stdout+stderr；失败不抛异常，把结果交给模型判断。

    CI 下若检测到执行器 socket，命令会转发给它（真令牌只在执行器侧），
    否则本地执行。
    """
    if not command or not command.strip():
        return "命令为空"

    if os.environ.get("EXECUTOR_SOCK") or os.environ.get("EXECUTOR_PORT"):
        try:
            return _run_remote(ctx, command)
        except Exception:  # noqa: BLE001 - 执行器不可达就退回本地，不阻断
            return _run_local(ctx, command)
    return _run_local(ctx, command)


def build_bash_tool(ctx: ShellContext):
    """把 run_bash 包成 SDK 的 function tool，docstring 即模型看到的工具说明。"""

    @function_tool
    def bash(context: RunContextWrapper[ShellContext], command: str) -> str:
        """在工作目录（已克隆的待审查仓库）里执行一条 bash 命令。

        用于读代码、看 git diff/log、搜索调用方等。支持管道、重定向与 `&&` 串联。
        Args:
            command: 要执行的 bash 命令，例如 "git diff --stat" 或 "rg -n 'def foo' src"。
        """
        return run_bash(context.context, command)

    return bash


def build_tools(ctx: ShellContext) -> list:
    """按配置组装工具列表；当前只有 bash，扩展点也在这里。"""
    return [build_bash_tool(ctx)]
