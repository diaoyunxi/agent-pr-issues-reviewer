# 需要填写的 Token / 变量清单

按填写位置分成两部分：**GitHub（你 fork 出来的本仓库）** 与 **Cloudflare（Worker）**。
`是否 Secret` 一列标明该值是否必须加密保存（是 → 存 Secrets；否 → 存明文 Variables / vars）。

---

## 一、GitHub 侧（Settings → Secrets and variables → Actions）

### Secrets（加密，日志自动打码）

| 名称 | 是否 Secret | 简介（用途） | 例子 |
| --- | --- | --- | --- |
| `PAT_TOKEN` | 是 | 拉取本仓库、清理任务文件、令牌回退 | `ghp_AbCdEf1234567890xyz` |
| `AI_API_KEY` | 是 | 调用大模型的 API Key，必填 | `sk-abcdef1234567890` |
| `GH_APP_ID` | 是 | GitHub App 的 ID，App 身份用 | `1234567` |
| `GH_APP_INSTALLATION_ID` | 是 | App 装到目标账号的安装 ID | `98765432` |
| `GH_APP_PRIVATE_KEY` | 是 | App 私钥 PEM 全文，最高敏感 | `-----BEGIN PRIVATE KEY-----\nMIIE...` |
| `GITEE_APP_TOKEN` | 是 | Gitee 应用授权下发的 access_token | `1a2b3c4d5e6f7g8h9i0j` |
| `AI_API_BASE` | 建议是 | 模型接口基址，兼容网关才需改 | `https://api.deepseek.com/v1` |
| `AI_MODEL` | 否 | 模型名，不填默认 gpt-4o-mini | `gpt-4o` |
| `GITEE_API` | 否 | Gitee API 基址，自托管才改 | `https://gitee.com/api/v5` |

> `GH_APP_*` 与 `GITEE_APP_TOKEN` 是可选增强：不配则以个人账号身份评论，链路照跑。
> `AI_API_BASE` / `AI_MODEL` 也可放 Variables，见下表，二选一即可。

### Variables（明文，非敏感）

| 名称 | 是否 Secret | 简介（用途） | 例子 |
| --- | --- | --- | --- |
| `UPSTREAM_REPO` | 否 | 待审查仓库地址，仅作兜底 | `https://github.com/you/repo.git` |
| `PROVIDER` | 否 | 平台选择，默认 github | `github` |
| `AGENT_CONFIG` | 否 | 目标仓库内配置文件路径 | `agents/config.json` |
| `AGENT_NAME` | 否 | 配置里跑哪个 agent 角色 | `reviewer` |
| `AI_API_BASE` | 否 | 与上方 Secret 二选一，明文版 | `https://api.openai.com/v1` |
| `AI_MODEL` | 否 | 与上方 Secret 二选一，明文版 | `gpt-4o-mini` |

### 配置入口

- Secrets：仓库 Settings → Secrets and variables → Actions → **New repository secret**
- Variables：同一页面切到 **Variables** 标签 → **New repository variable**
- GitHub App：Settings → Developer settings → GitHub Apps → New（拿到 App ID、Installation ID、私钥）

---

## 二、Cloudflare 侧（Workers & Pages → 你的 Worker → Settings → Variables and Secrets）

写入方式二选一，效果等价：控制台界面新增，或命令行 `npx wrangler secret put <名称>`。

### Secrets（加密，必须走 `wrangler secret put`）

| 名称 | 是否 Secret | 简介（用途） | 例子 |
| --- | --- | --- | --- |
| `GITHUB_PAT` | 是 | 把任务 JSON 写回本仓库 tasks/，必填 | `ghp_AbCdEf1234567890xyz` |
| `GITHUB_WEBHOOK_SECRET` | 是 | GitHub Webhook 的 HMAC 签名密钥 | `s3cr3t-webhook-2026` |
| `GITEE_PAT` | 是 | Gitee 个人令牌，Gitee 回退用 | `1a2b3c4d5e6f7g8h9i0j` |
| `GITEE_WEBHOOK_SECRET` | 是 | Gitee Webhook 密码（明文比对） | `gitee-hook-pwd` |
| `GH_APP_ID` | 是 | 与 GitHub 侧同一个 App ID | `1234567` |
| `GH_APP_INSTALLATION_ID` | 是 | 与 GitHub 侧同一个安装 ID | `98765432` |
| `GH_APP_PRIVATE_KEY` | 是 | 与 GitHub 侧同一份私钥 PEM | `-----BEGIN PRIVATE KEY-----\nMIIE...` |
| `GITEE_APP_TOKEN` | 是 | 与 GitHub 侧同一个 Gitee 令牌 | `1a2b3c4d5e6f7g8h9i0j` |

> `GITHUB_WEBHOOK_SECRET` 缺失时 Worker 会放行所有请求，**线上务必配置**。
> App 三项填了就用 App 身份，拿不到令牌自动回退 `GITHUB_PAT` / `GITEE_PAT`。

### 明文变量（`wrangler.toml` 的 `[vars]`，非敏感）

| 名称 | 是否 Secret | 简介（用途） | 例子 |
| --- | --- | --- | --- |
| `CONTROL_REPO` | 否 | 你 fork 的本仓库 owner/repo，必填 | `you/agent-pr-issues-reviewer` |
| `BOT_NAME` | 否 | 评论里 @ 的机器人名，务必配 | `ai-reviewer` |
| `GITHUB_API` | 否 | GitHub API 基址，GHE 才改 | `https://api.github.com` |
| `GITEE_API` | 否 | Gitee API 基址，默认不用改 | `https://gitee.com/api/v5` |

---

## 三、两侧共用的值（同一份填两遍）

| 值 | GitHub 侧位置 | Cloudflare 侧位置 |
| --- | --- | --- |
| `GH_APP_ID` / `GH_APP_INSTALLATION_ID` / `GH_APP_PRIVATE_KEY` | Actions Secrets | Worker Secrets |
| `GITEE_APP_TOKEN` | Actions Secrets | Worker Secrets |
| `GITHUB_WEBHOOK_SECRET` | 目标仓库 Webhook 设置页 | Worker Secrets |

Webhook 侧的密钥值必须与 Worker 里的完全一致，否则签名校验失败（GitHub 返回 401 / Gitee 返回 403）。
