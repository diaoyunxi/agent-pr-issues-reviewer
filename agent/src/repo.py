"""把待审查的仓库克隆到 /tmp 下的一个子目录，供 agent 在里面跑 shell。

克隆用**令牌拼接在 URL 里的方式**（`https://x-access-token:<token>@host/owner/repo.git`），
因为带令牌的 remote 只临时存在于容器里，跑完 job 就没了；这样 CI 里不需要额外配
git credential helper 或依赖 checkout action，上游仓库地址由 CI 直接传进来即可。

克隆是**完整克隆**（全量历史 + 所有分支的 remote-tracking ref），不做 `--depth=1` 浅克隆：
agent 需要 `git log`、`git blame`、`git diff <base>...<head>` 这类跨历史的命令，
浅克隆下这些要么报错要么结果不可信。代价是耗时与流量更大，`GIT_TIMEOUT` 相应放宽。
"""

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

GIT_TIMEOUT = 1200  # 完整克隆：大仓库可能跑几分钟，放宽超时


class RepoError(RuntimeError):
    """克隆或检出失败。"""


def mask_url(url: str) -> str:
    """把 URL 里的令牌换成 ***，确保任何日志里都不会出现凭据。"""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, f"***@{host}", parts.path, parts.query, parts.fragment))


def _authenticated_url(url: str, token: str, provider: str) -> str:
    """按平台把令牌拼进 HTTPS URL；已经是带凭据的 URL 就不再改写。"""
    if not token:
        return url
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or "@" in parts.netloc:
        return url
    user = "oauth2" if provider == "gitee" else "x-access-token"
    netloc = f"{user}:{token}@{parts.hostname}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _run_git(args: list[str], cwd: Path | None = None, redact: dict[str, str] | None = None) -> str:
    """跑一条 git 命令；失败时把输出里的令牌脱敏后再抛出来。"""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
    except FileNotFoundError:
        raise RepoError("宿主机没有 git，无法克隆仓库") from None
    except subprocess.TimeoutExpired:
        raise RepoError(f"git {' '.join(args[:2])} 超时（{GIT_TIMEOUT}s）") from None

    def clean(text: str) -> str:
        for raw, masked in (redact or {}).items():
            text = text.replace(raw, masked)
        return text.strip()

    if proc.returncode != 0:
        detail = clean(proc.stderr) or clean(proc.stdout) or "无输出"
        raise RepoError(f"git {' '.join(args)} 失败：{detail}")
    return clean(proc.stdout)


def clone_repo(
    url: str,
    token: str = "",
    provider: str = "github",
    branch: str = "",
    base_sha: str = "",
    head_sha: str = "",
    workdir: str = "/tmp",
) -> Path:
    """**完整克隆**目标仓库到 /tmp 的一个子目录，返回仓库根目录。

    与浅克隆的区别：
    - 不带 `--depth`，`git log`/`git blame`/`git diff base...head` 都能正常工作；
    - 显式 `--no-single-branch`，拉取所有分支的 remote-tracking ref，
      便于 agent 自己比较 base/head 之外的分支；
    - 不指定 `--branch`，先克隆默认分支，再由 `checkout_head()` 切到待审查提交。

    `base_sha` / `head_sha` / `branch` 只用于日志提示与后续检出，不再影响克隆方式。
    """
    if not url:
        raise RepoError("上游仓库地址为空，请通过 UPSTREAM_REPO/REPO_URL 传入")

    target = Path(workdir).expanduser().resolve() / f"repo-{uuid.uuid4().hex[:12]}"
    Path(workdir).expanduser().resolve().mkdir(parents=True, exist_ok=True)

    auth_url = _authenticated_url(url, token, provider)
    redact = {auth_url: mask_url(auth_url)}
    if token:
        redact[token] = "***"

    wanted = head_sha or branch
    print(
        "[repo] 完整克隆上游仓库："
        f"{mask_url(auth_url)} → {target}"
        + (f"（目标提交 {wanted[:12]}）" if wanted else "")
    )

    try:
        # 完整克隆：全量历史 + 全部分支。不指定 --branch，避免把克隆限制在单一分支。
        _run_git(
            ["clone", "--no-single-branch", auth_url, str(target)],
            redact=redact,
        )
    except RepoError:
        shutil.rmtree(target, ignore_errors=True)
        raise

    # 让 CI 日志与 agent 自己敲的 git 命令都不要跳出证书/凭据交互
    _run_git(["config", "advice.detachedHead", "false"], cwd=target, redact=redact)
    return target


def checkout_head(repo_dir: Path, sha: str = "", branch: str = "") -> None:
    """把工作区切到待审查的提交（审查对象是 head，不是默认分支）。

    优先按 sha 检出；没有 sha 或该 sha 在克隆结果里不可达时，回退到分支名
    （远端分支已被完整克隆拉到 `origin/<branch>`，直接检出同名本地分支）。
    两者都没有就安静地留在默认分支，不做隐式兜底式的失败。
    """
    if sha:
        try:
            _run_git(["checkout", "-q", sha], cwd=repo_dir)
            return
        except RepoError as err:
            if not branch:
                raise
            print(f"[repo] 按提交 {sha[:12]} 检出失败（{err}），回退到分支 {branch}")
    if branch:
        _run_git(["checkout", "-q", branch], cwd=repo_dir)


def sanitize_env(env: dict | None = None, workspace: Path | None = None) -> dict:
    """构造给 agent shell 用的环境变量：去掉凭据，并钉住目录与 git 配置。

    agent 的 bash 工具跑在同一台机器上，理论上能 `env` 出来 AI_API_KEY / 平台令牌，
    所以这里显式剔除带密钥语义的变量，只留下跑命令必需的部分。
    不传 env 时以 os.environ 为底（只读，不改动进程环境）。
    """
    source = os.environ if env is None else env
    secret_markers = ("TOKEN", "SECRET", "PASSWORD", "KEY")
    result = {
        k: v for k, v in source.items() if not any(marker in k.upper() for marker in secret_markers)
    }
    system_env = os.environ if env is not None else source
    result.setdefault("PATH", system_env.get("PATH", "/usr/local/bin:/usr/bin:/bin"))
    result.setdefault("LANG", "C.UTF-8")
    result.setdefault("LC_ALL", "C.UTF-8")
    result.setdefault("GIT_TERMINAL_PROMPT", "0")
    result.setdefault("GIT_PAGER", "cat")
    result.setdefault("PAGER", "cat")
    if workspace:
        result["PWD"] = str(workspace)
        result["HOME"] = str(workspace)
    return result
