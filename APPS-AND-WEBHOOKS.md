# GitHub App / Gitee App 权限与 Webhook 事件清单

依据 `worker/src/index.ts`（事件解析）、`worker/src/app-auth.ts`（App 换令牌）与 `agent/src`（克隆、回写评论、work 模式 push）实际用到的能力整理。

---

## 一、GitHub App

### 1. 仓库权限（Permissions & events → Repository permissions）

| 权限项 | 选什么 | 为什么需要 |
| --- | --- | --- |
| **Metadata** | Read-only | 必选基线权限，GitHub 强制要求 |
| **Contents** | **Read and write** | 读：克隆目标仓库代码；写：`work` 模式 `git push` 回源分支 |
| **Issues** | **Read and write** | 回写 AI 评论（PR 评论也走 `issues/{n}/comments`） |
| **Pull requests** | **Read and write** | 读 PR 标题/正文/分支/sha，`work` 模式改 PR 内容 |
| Discussions / Actions / 其余 | 不勾 | 用不到，不要多给 |

### 2. 账号权限（Account permissions）

全部留 `No access`。本项目不读写用户资料、不管理组织。

### 3. 订阅事件（Subscribe to events）

| 事件 | 是否勾选 | 触发后的行为 |
| --- | --- | --- |
| **Pull request** | ✅ 必选 | PR `opened`/`reopened`/`synchronize`/`ready_for_review` → 默认 `review` |
| **Issue comment** | ✅ 必选 | 评论里 `@BOT_NAME review\|work` → 按指定模式执行 |
| **Issues** | ✅ 必选 | Issue `opened` 且正文 @ 了机器人 → 按指定模式执行 |
| Pull request review comment | ❌ 不勾 | Worker 不处理行内评审评论，勾了也只会被丢弃 |
| Push / Release / 其余 | ❌ 不勾 | 无用事件，徒增请求量 |

### 4. Webhook 设置（App 设置页 → Webhook）

| 字段 | 填什么 |
| --- | --- |
| Webhook URL | `https://<你的 Worker 域名>/`（根路径即可，Worker 只收 POST） |
| Content type | `application/json`（**必须**，否则解析失败） |
| Secret | 与 Worker Secret `GITHUB_WEBHOOK_SECRET` 完全一致 |
| SSL verification | `Enable` |
| Active | 勾选 |

### 5. 安装范围（Install App）

安装时把这两个仓库都选上（或选 All repositories）：

1. **你 fork 的本仓库**（`CONTROL_REPO`）：Worker 要往它的 `tasks/` 写任务 JSON；
2. **目标业务仓库**：agent 要克隆它、回写评论、`work` 模式回推提交。

安装完成后拿到 **Installation ID**（URL 形如 `.../installations/<数字>`），即 `GH_APP_INSTALLATION_ID`。

---

## 二、Gitee 应用（开放平台 → 创建应用）

Gitee 没有「安装令牌」概念，只能拿到授权时下发的 `access_token`，因此权限 = 授权时勾选的 scope。

### 1. 应用授权 scope

| Scope | 是否勾选 | 为什么需要 |
| --- | --- | --- |
| **user_info** | ✅ | Worker 用 `/user` 探测令牌是否有效 |
| **projects** | ✅ | 读取/写入项目，用于克隆与 `work` 模式推送 |
| **issues** | ✅ | 读写 Issue 并回写评论 |
| **notes** | ✅ | 评论能力（Issue / PR 下的评论都靠它） |
| **pull_requests** | ✅ | 读写 PR、回写 PR 评论 |
| hook / groups / 其余 | ❌ | 用不到 |

授权后拿到的 `access_token` 填入 `GITEE_APP_TOKEN`。
用哪个账号授权，令牌就对该账号可见的仓库生效——**授权账号必须对目标仓库有写权限**。

### 2. 目标仓库 Webhook（仓库 → 管理 → WebHooks → 添加）

| 字段 | 填什么 |
| --- | --- |
| URL | `https://<你的 Worker 域名>/` |
| **WebHook 密码** | 与 Worker Secret `GITEE_WEBHOOK_SECRET` 一致 |
| 签名密钥 | **留空**（留空时 Gitee 回带明文密码；填了会改发签名值，当前 Worker 校验逻辑不认） |
| 勾选事件 | **Issue**、**Merge Request（PR）**、**评论（Note）** |
| 是否发送 | 勾选激活 |

---

## 三、两边事件 → 代码行为对照

| 平台 | 事件名 | 代码里的判定 | 结果 |
| --- | --- | --- | --- |
| GitHub | `pull_request` | action ∈ opened/reopened/synchronize/ready_for_review | 默认 `review`，PR 正文 @ 了则按 @ 的模式 |
| GitHub | `issue_comment` | 正文含 `@BOT_NAME review\|work` | 按 @ 的模式；否则丢弃（204） |
| GitHub | `issues`(opened) | 正文含 `@BOT_NAME review\|work` | 按 @ 的模式；否则丢弃 |
| Gitee | Merge Request | `pull_request` 字段存在 | 同 GitHub 的 PR |
| Gitee | 评论（Note） | `x-gitee-event` == `note` | 必须 @ + 模式词，否则丢弃 |
| Gitee | Issue | `issue` 字段存在且 @ 了 | 按 @ 的模式；否则丢弃 |

### 两个要现场核对的点

1. **Gitee 评论事件的 header 值**：Worker 是**精确匹配** `note` 才按评论处理。
   Gitee 官方示例里 header 形如 `Note Hook`。若实际下发的是带空格的形式，评论链路不会命中——
   去 Cloudflare 的「实时日志 / Workers Logs」看一次真实 `X-Gitee-Event` 值确认；
   不匹配就把该值规范化（或在 Worker 侧改成 `event.includes('note')` 之类的宽松判定）后再回归一次。
2. **GitHub 行内评论不触发**：只订阅了 `Issue comment`，PR 的行内 review comment 不会进来，属预期行为。

---

## 四、回退令牌（PAT）需要的最小权限

App 没配或失效时会回退到 PAT，权限不够同样会失败。

### GitHub PAT（fine-grained，推荐）

- 作用域：只选 **控制仓库（fork）** + **目标仓库**
- Repository permissions：`Contents: Read and write`、`Issues: Read and write`、`Pull requests: Read and write`、`Metadata: Read-only`
- classic PAT 的话勾选 `repo` 即可（不要再给 `admin:*`、`delete_repo`）

### Gitee 私人令牌

- 勾选 `projects`、`issues`、`pull_requests`、`notes` 四项
- 有效期设成**长期**；过期后若 `GITEE_APP_TOKEN` 也失效，回写评论会直接失败

---

## 五、上线自检

1. 目标仓库开一个 PR → Worker 返回 202 → fork 仓库 `tasks/` 出现 JSON → Actions 跑起来 → PR 收到评论。
2. 评论 `@BOT_NAME review` → 再跑一次；换 `@BOT_NAME work 改个错别字` → 检查是否产生提交。
3. Actions 日志里搜「获取到 App 令牌」，确认评论作者是 `xxx[bot]`；若显示回退，按日志里的原因修 App 凭据。
4. 把 Webhook Secret 故意改错一次，确认 Worker 返回 401/403——能拒绝说明校验真的生效了。
