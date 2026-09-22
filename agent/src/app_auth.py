"""GitHub App / Gitee App 鉴权：优先用 App 身份，失败自动回退个人令牌（PAT）。

GitHub App 用私钥签出 JWT，再换取 1 小时有效的安装令牌；
Gitee 的「应用」不给安装令牌换发接口，只认授权得到的 access_token，
所以 Gitee 侧直接透传 App 授权的 access_token，只有它失效时才回退 PAT。

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
        app_token: str = "",
        fallback_token: str = "",
        api_base: str = "",
        session: requests.Session | None = None,
    ):
        self.provider = provider
        self.installation_id = installation_id
        self.app_id = app_id
        self.private_key = private_key
        self.app_token = app_token
        self.fallback_token = fallback_token
        self.api_base = (api_base or (GITEE_API if provider == "gitee" else GITHUB_API)).rstrip("/")
        self.session = session or requests.Session()
        self._token: str | None = None
        self._expires_at = 0.0
        self.source = "none"

    def app_configured(self) -> bool:
        """App 凭据是否齐全到可以尝试换令牌。"""
        if self.provider == "github":
            return bool(self.installation_id and self.app_id and self.private_key)
        return bool(self.app_token)

    def token(self) -> str:
        """取可用令牌；App 优先，换发失败则回退个人令牌。"""
        if self.app_configured():
            if self._token and time.time() < self._expires_at:
                return self._token
            try:
                # 留 5 分钟余量，避免令牌在请求途中过期
                self._token, ttl = self._issue_app_token()
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

    def _issue_app_token(self) -> tuple[str, int]:
        """返回 (令牌, 有效期秒数)。"""
        if self.provider == "github":
            return self._github_installation_token()
        # Gitee 的 App access_token 是授权时下发的，没有换发接口，这里只探测一次有效性；
        # 有效期未知，按 1 小时缓存，过期后再探测，失效时由上层回退 PAT
        resp = self.session.get(f"{self.api_base}/user", params={"access_token": self.app_token}, timeout=30)
        resp.raise_for_status()
        return self.app_token, 3600

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
        app_token=os.environ.get("GITEE_APP_TOKEN", ""),
        fallback_token=os.environ.get("GITHUB_TOKEN", ""),
        api_base=os.environ.get("GITHUB_API", ""),
        session=session,
    )


def github_comment_url(repo: str, number: int) -> str:
    return f"{GITHUB_API}/repos/{repo}/issues/{number}/comments"


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
