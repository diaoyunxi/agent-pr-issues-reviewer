"""持密钥的执行器 sidecar：在单个 CI job 内与 AI 进程通过 Unix socket 通信。

设计目的：让 AI（模型）能 `git push` / `gh` / `curl` 带令牌，却**永远看不到真令牌**。

- AI 进程只把「命令字符串（可含 `${GH_TOKEN}` 等占位符）」通过 socket 发过来；
- 执行器在**自己的进程**里持有真令牌（来自 Actions Secrets 注入的 env），把占位符替换为真值后执行；
- 执行结果（stdout/stderr）在回传前把所有已知密钥值脱敏成 `***`；
- AI 进程的运行环境不含任何密钥，模型只能看到脱敏后的输出，因此拿不到令牌。

与「两个 job 运行时通信」的区别：GitHub Actions 的 job 之间无法实时双向通信，
所以这里用「一个 job 里的两个进程」达到同样效果——既有运行时交互，又真正隔离令牌。

协议（长度前缀的 JSON，每条连接一次请求/响应）：
  请求：4 字节大端长度 N + N 字节 UTF-8 JSON {"command","cwd","timeout"}
  响应：4 字节大端长度 N + N 字节 UTF-8 JSON {"stdout","stderr","exit_code"}
"""

import json
import os
import socket
import struct
import subprocess
import tempfile
from pathlib import Path

from app_auth import build_token_provider
from repo import sanitize_env

SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "KEY")
SOCK_PATH = os.environ.get("EXECUTOR_SOCK", "/tmp/executor.sock")
DEFAULT_PORT = int(os.environ.get("EXECUTOR_PORT", "8731"))
# 解析出的令牌往往很长，短于该长度的串不做全量脱敏，避免误伤正常输出
MIN_REDACT_LEN = 8


def _use_unix() -> bool:
    """Linux CI 用 Unix socket；Windows 等无 AF_UNIX 的平台回退 TCP。"""
    return bool(os.environ.get("EXECUTOR_SOCK")) and hasattr(socket, "AF_UNIX")


def resolve_address():
    """返回 (family, addr)。Unix socket 优先（EXECUTOR_SOCK），否则 TCP 127.0.0.1:EXECUTOR_PORT。"""
    if _use_unix():
        return socket.AF_UNIX, os.environ["EXECUTOR_SOCK"]
    return socket.AF_INET, ("127.0.0.1", DEFAULT_PORT)


def is_ready() -> bool:
    """执行器是否已可连接（供拉起方轮询）。"""
    family, addr = resolve_address()
    if family == socket.AF_UNIX:
        return os.path.exists(addr)
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            s.connect(addr)
        return True
    except OSError:
        return False


def _collect_secrets() -> dict:
    """从本进程 env 收集密钥：git 推送令牌 + 其余像密钥的变量，供占位符替换与脱敏。"""
    secrets: dict = {}
    # git 推送用的令牌：优先 App 身份，回退个人令牌；GitHub / Gitee 各取一份
    for provider, key in (("github", "GH_TOKEN"), ("gitee", "GITEE_TOKEN")):
        try:
            tok = build_token_provider(provider).token()
        except Exception:  # noqa: BLE001 - 取不到就跳过，绝不因鉴权失败阻断执行器
            tok = ""
        if tok:
            secrets[key] = tok
    # 其余名字里带密钥语义的 env 变量，也允许用 ${NAME} 占位引用（如 PAT_TOKEN）
    for name, val in os.environ.items():
        if name in secrets or not val:
            continue
        if any(m in name.upper() for m in SECRET_MARKERS):
            secrets[name] = val
    return secrets


def _write_git_creds(secrets: dict) -> Path | None:
    """写一份 git credential store 文件（仅本执行器使用），让 `git push` 免令牌也能认证。

    不写进仓库的 .git/config，而是通过 GIT_CONFIG_* 环境变量按命令注入，
    这样 AI `git config --list` 也看不到令牌，令牌只存在于本文件（被脱敏兜底）。
    """
    lines = []
    gh = secrets.get("GH_TOKEN")
    ge = secrets.get("GITEE_TOKEN")
    if gh:
        lines.append(f"https://x-access-token:{gh}@github.com")
    if ge:
        lines.append(f"https://oauth2:{ge}@gitee.com")
    # 自托管 GitHub（GITHUB_API 指向非 api.github.com）也补一条
    api = (os.environ.get("GITHUB_API") or "").rstrip("/")
    if gh and api and api not in ("https://api.github.com", "http://api.github.com"):
        host = api.split("//", 1)[-1]
        lines.append(f"https://x-access-token:{gh}@{host}")
    if not lines:
        return None
    path = Path(tempfile.gettempdir()) / ".git-creds-executor"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _setup_gh(secrets: dict) -> None:
    """用 GitHub 令牌预先登录 `gh`，让 AI 的 `gh` 命令能直接认证。"""
    tok = secrets.get("GH_TOKEN")
    if not tok:
        return
    try:
        subprocess.run(
            ["gh", "auth", "login", "--with-token"],
            input=tok, text=True, timeout=30, capture_output=True,
        )
    except Exception:  # noqa: BLE001 - gh 不可用就跳过，不影响 git/curl 路径
        pass


def _redact(text: str, secrets: dict) -> str:
    """把输出里出现的任何已知密钥值替换成 ***，避免令牌经 socket 泄漏给模型。"""
    for val in secrets.values():
        if val and len(val) >= MIN_REDACT_LEN:
            text = text.replace(val, "***")
    return text


def _run_command(command: str, cwd: str | None, timeout: float, secrets: dict, creds_path: Path | None) -> tuple[str, str, int]:
    """在执行器进程内跑一条命令：env 剔除密钥，git 走 credential helper，占位符先替换。"""
    env = sanitize_env({}, Path(cwd) if cwd else None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if creds_path:
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "credential.helper"
        env["GIT_CONFIG_VALUE_0"] = f"store --file {creds_path}"

    # 占位符替换：AI 写的 ${GH_TOKEN} 等在执行器侧才变成真值，模型侧永远只是占位符
    for name, val in secrets.items():
        command = command.replace("${" + name + "}", val)

    try:
        proc = subprocess.run(
            ["bash", "-lc", command],
            cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return "(命令超时，请缩小范围后重试)", "", 124
    except FileNotFoundError:
        return "(找不到 bash，无法执行命令)", "", 127

    stdout = _redact(proc.stdout or "", secrets)
    stderr = _redact(proc.stderr or "", secrets)
    return stdout, stderr, proc.returncode


# —— socket 帧编解码 ——
def _recvall(conn: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def _recv_json(conn: socket.socket):
    hdr = _recvall(conn, 4)
    if len(hdr) < 4:
        return None
    n = struct.unpack(">I", hdr)[0]
    return json.loads(_recvall(conn, n).decode("utf-8"))


def _send_json(conn: socket.socket, obj: dict) -> None:
    data = json.dumps(obj).encode("utf-8")
    conn.sendall(struct.pack(">I", len(data)) + data)


def _handle(conn: socket.socket, secrets: dict, creds_path: Path | None) -> None:
    try:
        req = _recv_json(conn)
        if not req:
            return
        command = str(req.get("command", ""))
        cwd = req.get("cwd") or None
        timeout = float(req.get("timeout") or 120.0)
        stdout, stderr, code = _run_command(command, cwd, timeout, secrets, creds_path)
        _send_json(conn, {"stdout": stdout, "stderr": stderr, "exit_code": code})
    except Exception as err:  # noqa: BLE001 - 单条命令出错不影响执行器继续服务
        _send_json(conn, {"stdout": "", "stderr": f"[executor] {type(err).__name__}: {err}", "exit_code": 1})


def serve() -> None:
    family, addr = resolve_address()
    if _use_unix():
        if os.path.exists(addr):
            os.remove(addr)
    secrets = _collect_secrets()
    creds_path = _write_git_creds(secrets)
    _setup_gh(secrets)
    print(f"[executor] 就绪：addr={addr} gh={'yes' if secrets.get('GH_TOKEN') else 'no'} "
          f"creds={'yes' if creds_path else 'no'}", flush=True)

    srv = socket.socket(family, socket.SOCK_STREAM)
    srv.bind(addr)
    srv.listen(8)
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            break
        with conn:
            _handle(conn, secrets, creds_path)


if __name__ == "__main__":
    serve()
