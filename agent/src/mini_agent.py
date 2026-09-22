"""一个够用的工具调用 Agent：让模型自己决定拉取哪些文件，再产出审查意见。

不引第三方 Agent 框架，是为了让整条链路只依赖 requests 与 cryptography，方便在 CI 里跑。
"""

import json
import os

import requests

from app_auth import TokenProvider, gitee_auth_headers

# 限制单轮对话的上下文体积，避免大 PR 直接把模型上下文撑爆
MAX_DIFF_CHARS = 60_000
MAX_FILE_CHARS = 20_000
MAX_TOOL_ROUNDS = 6

GITHUB_API = "https://api.github.com"
GITEE_API = "https://gitee.com/api/v5"


class AgentTools:
    """Agent 可调用的目标仓库工具集，GitHub 与 Gitee 都走这一份实现。

    令牌由 TokenProvider 提供，GitHub 侧拿到的是 App 安装令牌或 PAT，
    Gitee 侧拿到的是 App 授权 access_token 或 PAT。
    """

    def __init__(
        self,
        repo: str,
        pr_number: int,
        provider: TokenProvider,
        is_issue: bool = False,
        api_base: str = "",
    ):
        self.repo = repo
        self.pr_number = pr_number
        self.provider = provider
        self.is_issue = is_issue
        self.platform = provider.provider
        default_base = GITEE_API if self.platform == "gitee" else GITHUB_API
        self.api_base = (api_base or provider.api_base or default_base).rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "ai-review-agent"})

    def _headers(self, extra: dict | None = None) -> dict:
        token = self.provider.token()
        base = (
            gitee_auth_headers(token)
            if self.platform == "gitee"
            else {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        base.update(extra or {})
        return base

    def _params(self, extra: dict | None = None) -> dict:
        # Gitee 的 GET 接口在部分路径上只认 access_token 查询串
        params = dict(extra or {})
        if self.platform == "gitee":
            params["access_token"] = self.provider.token()
        return params

    def _get(self, path: str, **params):
        resp = self.session.get(
            f"{self.api_base}{path}",
            params=self._params(params),
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _issue_or_pr(self, path_suffix: str = "") -> str:
        """Issue 事件下没有 PR 详情接口，取详情要换成 issues 端点。"""
        kind = "issues" if self.is_issue else "pulls"
        return f"/repos/{self.repo}/{kind}/{self.pr_number}{path_suffix}"

    def pr_meta(self) -> dict:
        """PR/Issue 标题与改动统计。"""
        detail = self._get(self._issue_or_pr())
        title = detail.get("title") or ""

        if self.is_issue:
            return {"title": title, "changed_files": []}

        if self.platform == "gitee":
            files = self._get(self._issue_or_pr("/files"))
            if isinstance(files, dict):
                files = files.get("files", [])
        else:
            files = self._get(self._issue_or_pr("/files"), per_page=100)

        return {
            "title": title,
            "changed_files": [
                {
                    "filename": f.get("filename") or f.get("new_path") or "",
                    "status": f.get("status", ""),
                    "additions": f.get("additions", 0),
                    "deletions": f.get("deletions", 0),
                }
                for f in files
            ],
        }

    def diff(self) -> str:
        """PR 的 unified diff。"""
        headers = self._headers()
        if self.platform == "github":
            headers["Accept"] = "application/vnd.github.v3.diff"
        resp = self.session.get(
            f"{self.api_base}{self._issue_or_pr()}",
            params=self._params({"diff": "1"}),
            headers=headers,
            timeout=60,
        )
        if resp.status_code >= 400:
            # Gitee 取 diff 失败时降级用 patch 字段拼，至少别让 Agent 空手
            return self._fallback_patch()
        return resp.text[:MAX_DIFF_CHARS]

    def _fallback_patch(self) -> str:
        try:
            files = self._get(self._issue_or_pr("/files"))
        except Exception:  # noqa: BLE001 - 兜底路径失败就如实返回空 diff
            return "（无法获取 diff）"
        if isinstance(files, dict):
            files = files.get("files", [])
        chunks = [f.get("patch") or f.get("diff") or "" for f in files]
        return "\n".join(chunks)[:MAX_DIFF_CHARS]

    def read_file(self, path: str, ref: str | None = None) -> str:
        """读取目标仓库里某个文件的完整内容，供模型补充上下文。"""
        import base64

        data = self._get(f"/repos/{self.repo}/contents/{path}", **({"ref": ref} if ref else {}))
        content = data.get("content")
        if not content:
            return "（文件为空或不可读）"
        return base64.b64decode(content).decode("utf-8", errors="replace")[:MAX_FILE_CHARS]


TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "pr_diff",
            "description": "获取本 PR 的完整 unified diff",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取仓库中某个文件的完整内容，用于理解上下文",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件路径"}},
                "required": ["path"],
            },
        },
    },
]


class ReviewAgent:
    def __init__(self, tools: AgentTools, api_base: str, api_key: str, model: str):
        self.tools = tools
        self.api_base = api_base.rstrip("/")
        self.model = model
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "ai-review-agent",
            }
        )

    def _chat(self, messages: list, with_tools: bool) -> dict:
        payload = {"model": self.model, "messages": messages, "temperature": 0.2}
        if with_tools:
            payload["tools"] = TOOL_SPECS
        resp = self.session.post(f"{self.api_base}/chat/completions", json=payload, timeout=180)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]

    def _dispatch(self, name: str, args: dict) -> str:
        if name == "pr_diff":
            return self.tools.diff()
        if name == "read_file":
            path = args.get("path", "")
            # 防目录穿越：只允许仓库内相对路径
            if not path or path.startswith("/") or ".." in path.split("/"):
                return "非法的文件路径"
            return self.tools.read_file(path)
        return f"未知工具：{name}"

    def run(self) -> str:
        meta = self.tools.pr_meta()
        messages = [
            {
                "role": "system",
                "content": (
                    "你是一位严格的代码评审者。先用工具获取 diff，必要时读取相关文件，"
                    "然后输出 Markdown 格式的评审意见：先给结论，再按严重级别列出问题"
                    "（每条给出文件、行号、原因、修改建议）。没问题的部分不必展开。"
                ),
            },
            {
                "role": "user",
                "content": f"PR 标题：{meta['title']}\n改动文件：{json.dumps(meta['changed_files'], ensure_ascii=False)}",
            },
        ]

        for _ in range(MAX_TOOL_ROUNDS):
            message = self._chat(messages, with_tools=True)
            messages.append(message)

            tool_calls = message.get("tool_calls")
            if not tool_calls:
                return message.get("content") or "（模型未返回内容）"

            for call in tool_calls:
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = self._dispatch(call["function"]["name"], args)
                # 工具结果回填时截断，避免上下文爆炸
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": (result or "")[:MAX_FILE_CHARS],
                    }
                )

        # 轮次用尽仍无结论，降级为直接基于 diff 的一次性总结
        messages.append({"role": "user", "content": "请直接基于已有信息给出评审结论。"})
        return self._chat(messages, with_tools=False).get("content") or "（模型未返回内容）"


def build_agent_from_env(tools: AgentTools) -> ReviewAgent:
    """从 GitHub Secrets 注入的环境变量里拿 API 地址与密钥。"""
    api_base = os.environ.get("AI_API_BASE", "https://api.openai.com/v1")
    api_key = os.environ["AI_API_KEY"]
    model = os.environ.get("AI_MODEL", "gpt-4o-mini")
    return ReviewAgent(tools, api_base, api_key, model)
