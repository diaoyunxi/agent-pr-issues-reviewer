"""GitHub App 鉴权：优先用 App 身份，失败自动回退个人令牌（PAT）。

GitHub App 用私钥签出 JWT，再换取 1 小时有效的安装令牌；
Gitee 侧无 App 身份，始终使用个人令牌（GITEE_PAT）。

每次运行只换一次令牌并缓存，避免在 Agent 的工具调用里反复请求。
"""

import json
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

# GitHub 侧 JWT 要求 iat 留出 60 秒回拨、exp 不超过 10 分钟
JWT_BACKDATE_SECONDS = 60
JWT_TTL_SECONDS = 540
# 令牌过期前提前刷新，避免请求途中正好失效
TOKEN_REFRESH_MARGIN_SECONDS = 300

GITHUB_API = "https://api.github.com"
GITEE_API = "https://gitee.com/api/v5"


def _b64url(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class TokenProvider:
    """按平台选择 App 身份，必要时回退到个人令牌。"""

    def __init__(
        self,
        provider: str,
        installation_id: str = "",
        app_id: str = "",
        private_key: str = "",
        fallback_token: str = "",
        api_base: str = "",
        session: requests.Session | None = None,
    ):
        self.provider = provider
        self.installation_id = installation_id
        self.app_id = app_id
        self.private_key = private_key
        self.fallback_token = fallback_token
        self.api_base = (api_base or (GITEE_API if provider == "gitee" else GITHUB_API)).rstrip("/")
        self.session = session or requests.Session()
        self._token: str | None = None
        self._expires_at = 0.0
        self.source = "none"

    def app_configured(self) -> bool:
        """App 凭据是否齐全到可以尝试换令牌（仅 GitHub 有 App 身份）。"""
        if self.provider != "github":
            return False
        return bool(self.installation_id and self.app_id and self.private_key)

    def token(self) -> str:
        """取可用令牌；App 优先，换发失败则回退个人令牌。"""
        if self.app_configured():
            if self._token and time.time() < self._expires_at:
                return self._token
            try:
                # 留 5 分钟余量，避免令牌在请求途中过期
                self._token, ttl = self._github_installation_token()
                self._expires_at = time.time() + ttl - TOKEN_REFRESH_MARGIN_SECONDS
                self.source = "app"
                return self._token
            except Exception as err:  # noqa: BLE001 - 换发失败必须能降级，不能直接挂任务
                print(f"[app_auth] App 令牌获取失败，回退个人令牌：{type(err).__name__}: {err}")

        if self.fallback_token:
            # 个人令牌不过期，缓存久一点即可
            self._token = self.fallback_token
            self._expires_at = time.time() + 3600
            self.source = "pat"
            return self._token

        raise RuntimeError("App 凭据与个人令牌均不可用，无法继续")

    def _github_installation_token(self) -> tuple[str, int]:
        resp = self.session.post(
            f"{self.api_base}/app/installations/{self.installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {self._github_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        expires_at = data.get("expires_at")
        ttl = 3600
        if expires_at:
            parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            ttl = max(300, int((parsed - datetime.now(timezone.utc)).total_seconds()))
        return data["token"], ttl

    def _github_jwt(self) -> str:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        now = int(time.time())
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        payload = _b64url(json.dumps({"iat": now - JWT_BACKDATE_SECONDS, "exp": now + JWT_TTL_SECONDS, "iss": str(self.app_id)}).encode())
        signing_input = f"{header}.{payload}".encode()

        key = serialization.load_pem_private_key(self.private_key.encode(), password=None)
        signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{payload}.{_b64url(signature)}"


def build_token_provider(provider: str, session: requests.Session | None = None) -> TokenProvider:
    """从环境变量（Actions Secrets 注入）组装 TokenProvider。"""
    return TokenProvider(
        provider=provider,
        installation_id=os.environ.get("GH_APP_INSTALLATION_ID", ""),
        app_id=os.environ.get("GH_APP_ID", ""),
        private_key=os.environ.get("GH_APP_PRIVATE_KEY", ""),
        fallback_token=os.environ.get("GITHUB_TOKEN", ""),
        api_base=os.environ.get("GITHUB_API", ""),
        session=session,
    )


def github_comment_url(repo: str, number: int) -> str:
    return f"{GITHUB_API}/repos/{repo}/issues/{number}/comments"


def github_inline_comment_url(repo: str, number: int) -> str:
    """PR 行内（diff 行）评论：把评论挂到具体代码行上。"""
    return f"{GITHUB_API}/repos/{repo}/pulls/{number}/comments"


def gitee_inline_comment_url(repo: str, number: int) -> str:
    """PR 行内（diff 行）评论：Gitee 只认 diff 内的 position，不认 line/side。"""
    return f"{GITEE_API}/repos/{repo}/pulls/{number}/comments"


def gitee_comment_url(repo: str, number: int, is_issue: bool) -> str:
    kind = "issues" if is_issue else "pulls"
    return f"{GITEE_API}/repos/{repo}/{kind}/{number}/comments"


def auth_headers(token: str) -> dict:
    """GitHub 侧：App 安装令牌与 PAT 都用 `Bearer`。"""
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


def gitee_auth_headers(token: str) -> dict:
    """Gitee 侧：令牌统一用 `token` 前缀，不是 Bearer。"""
    return {"Authorization": f"token {token}", "Accept": "application/json"}


def gitee_query(token: str) -> str:
    """Gitee 评论接口既收 Authorization 也收 access_token 查询串，双写更稳。"""
    return urlencode({"access_token": token})
