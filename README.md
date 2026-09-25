# Agent PR / Issue Reviewer

基于 **Cloudflare Worker + GitHub Actions** 的自动化 AI 代码助手：既能**审查代码**，也能按评论里的自然语言要求**直接改代码**。

控制面（公开中转仓库）与数据面（目标业务仓库）分离：Worker 只负责「登记任务」，Actions 只负责「跑 Agent + 回写评论」。

Agent 侧是一个**只有 bash 工具**的 OpenAI Agents SDK agent：它把上游仓库 `git clone` 到 `/tmp` 的子目录，然后在仓库里自己读代码；行为由目标仓库里的 `agents/config.json` 与两份提示词（`prompt-review.txt` / `prompt-work.txt`）决定，改配置不用改代码。

触发方式有两种 **mode**：

- **review（评审）**：PR 开启时默认走这条；也可以评论 `@你的App review` 手动触发。
- **work（干活）**：评论 `@你的App work 把 README 里的链接改成 xxx`，模型按描述改代码并提交。

规则细节见「[模块一 → 触发规则](#触发规则哪些事件会跑跑什么模式)」。

## 数据流向

```text
[目标仓库] (触发 Webhook)
      │
      ▼
[Cloudflare Worker]  (模块一)
      │ 1. 校验签名，按触发规则算 mode，提取 repo / 编号 / 标题 / 内容 / URL / user / base+head sha
      │ 2. 生成唯一文件 tasks/{目标仓库名}-{编号}-{时间戳}.json（含 mode 与 work 的自然语言要求）
      │ 3. 用 GitHub App 安装令牌（回退 PAT）将文件 Push 至【中转仓库】
      ▼
[公开中转仓库] (被 Push 触发)
      │
      ▼
[GitHub Actions]  (模块二) 运行在中转仓库
      │ 1. git diff-tree 找到刚 Push 进来的最新 JSON
      │ 2. jq 解析 JSON，取出目标仓库、PR 信息与 upstream 仓库 URL
      │ 3. 换发 GitHub App 安装令牌 / Gitee 应用令牌（失败回退 PAT）
      │ 4. 把这枚令牌与 UPSTREAM_REPO 一起交给 agent
      │ 5. AI Agent (模块三)：git clone 上游仓库到 /tmp 的子目录
      │ 6. 在仓库目录里跑「只有 bash」的 agent（按 mode 读 prompt-review.txt 或 prompt-work.txt）
      │ 7. 用同一身份将结果评论回写至【目标仓库】（work 模式还会 commit + push）
      │ 8. 清理中转仓库里的 JSON 文件
      ▼
[目标仓库 PR 评论区]
```

> 注意第 4 步之后**没有 checkout**：待审查仓库里往往只有一份 CI 文件，
> 所以代码由 agent 自己克隆，仓库地址由 CI 通过 `UPSTREAM_REPO` 传进来。

## 目录结构

```text
.
├── wrangler.toml           # 模块一：Cloudflare Worker 配置（放根目录，Git 集成自动识别）
├── package.json            #   Worker 依赖与 deploy 脚本
├── tsconfig.json           #   Worker 类型检查配置
├── worker/
│   ├── src/
│   │   ├── index.ts        #   签名校验 / 触发规则 / 信息提取 / 触发中转
│   │   ├── mode.ts         #   `@BOT_NAME review|work` 的识别
│   │   ├── app-auth.ts     #   GitHub/Gitee App 令牌换发与 PAT 回退
│   │   └── env.ts          #   Worker 环境变量与密钥的类型声明
│   └── tests/              #   Worker 单测：触发规则 + @模式识别（npm test）
├── control-repo/           # 中转仓库（内容需复制到中转仓库根目录）
│   └── .github/workflows/
│       └── ai-review.yml   # 模块二：GitHub Actions
└── agent/                  # 模块三：AI Agent（脚本留在控制仓库，由 CI 直接调用）
    ├── requirements.txt    #   openai-agents + cryptography
    └── src/
        ├── review.py       #   入口：读配置 → 完整克隆上游仓库到 /tmp → 跑 agent → 回写评论
        ├── config.py       #   agents/config.json + 按模式选提示词的加载与校验
        ├── task_context.py #   任务 JSON / 环境变量 → 模型首条消息
        ├── agent_runner.py #   组装 OpenAI Agents SDK 的 Agent 并运行
        ├── repo.py         #   克隆到 /tmp 子目录、URL 脱敏、环境变量清洗
        ├── tools.py        #   唯一的工具：在仓库目录里执行 bash
        ├── app_auth.py     #   App 令牌优先、PAT 回退的 TokenProvider
        └── agents/
            ├── config.example.json  # agent 配置示例（目标仓库放 config.json）
            ├── prompt-review.txt    # review 模式提示词，纯文本
            ├── prompt-work.txt      # work 模式提示词（允许改代码并提交）
            └── task.example.json    # 任务 JSON 字段示例
```

> Worker 的 `wrangler.toml` / `package.json` / `tsconfig.json` 都放在**仓库根目录**，
> 这样 Cloudflare 的 Git 集成（Workers Builds）无需额外配置 root directory 就能自动部署；
> 入口文件通过 `wrangler.toml` 的 `main = "worker/src/index.ts"` 指向。

## 部署步骤

1. **中转仓库**：新建公开仓库，把 `control-repo/.github/workflows/ai-review.yml` 放进去，创建空的 `tasks/` 目录。
   `agent/` 脚本**留在中转仓库**即可，CI 直接从本仓库调用，不需要下发到目标仓库。
2. **目标仓库**：只要放配置与提示词到 `agents/` 目录——`agents/config.json`（可从
   `agent/src/agents/config.example.json` 复制），提示词按模式拆成
   `agents/prompt-review.txt`（评审用）与 `agents/prompt-work.txt`（干活用），由用户自行编写；
   只放一份 `agents/prompt.txt` 也可以，两种模式会共用它。
   代码由 agent 自己克隆，**不需要**再往目标仓库塞 agent 脚本，也不需要改目标仓库的 workflow。
   同时在**中转仓库**的 Variables 里配好 `UPSTREAM_REPO`（上游仓库 URL，多个仓库用输入覆盖）。
3. **配置 App（推荐）**：建好 GitHub App 与 Gitee 应用，把私钥/令牌写进密钥（见「App 身份配置」一节）。
   App 是可选项——不配就自动用个人令牌，链路照跑，只是评论与提交都以个人账号身份出现。
4. **Worker**：在**仓库根目录**执行 `npx wrangler secret put GITHUB_PAT`，逐个写入密钥后 `npx wrangler deploy`。
   若用 Cloudflare 控制台的 **Workers Builds（连接 Git 仓库）**，直接绑定本仓库即可：
   build 命令留空、deploy 命令用默认的 `npx wrangler deploy`，根目录就是仓库根，无需再改 root directory。
5. **配置 Webhook**：在 GitHub/Gitee 仓库设置里把 Webhook 指向 Worker 域名，内容类型 `application/json`，填入同一份密钥。

---

## 模块一：Cloudflare Worker（TypeScript）

### 触发规则：哪些事件会跑，跑什么模式

所有触发都会归一化成 `mode`：

| 事件 | 条件 | 结果 |
| --- | --- | --- |
| PR 开启类（`opened` / `reopened` / `synchronize` / `ready_for_review`） | 正文里没有 `@BOT_NAME` | `mode=review`（默认评审） |
| PR 开启类 | PR 描述里 `@BOT_NAME review\|work` | 按 @ 出来的模式 |
| Issue 开启（`opened`） | 正文里没有 `@BOT_NAME` | **丢弃**（返回 204，不入队） |
| Issue 开启 | 正文里 `@BOT_NAME review\|work` | 按 @ 出来的模式 |
| 评论（GitHub `issue_comment` / Gitee `note`） | 正文里 `@BOT_NAME ` 且后面跟 `review` / `work` | 按 @ 出来的模式（PR 评论、Issue 评论都支持） |
| 评论 | 没 @ 机器人，或 @ 了但后面不是 `review` / `work` | **丢弃** |

- `@` 的识别：`@` + `BOT_NAME` + 一个空格；`@BOT_NAME3` 不算命中（名字后必须是空白）。
- 大小写不敏感（`Review` / `WORK` 都认）。
- 模式词后面：
  - `review` → 评审，后面的多余文字忽略，`instruction` 为空；
  - `work` → 模式词之后的**原文**（含多行、代码块）整体作为 `instruction` 交给 Agent；
    不用打引号，直接写自然语言。
- `BOT_NAME` 是 Worker 的环境变量（GitHub / Gitee 各配一份，值就是评论里 @ 的那个 App 名称）；
  不配则退化成"任意 `@xxx` 提及"，此时 Issue 开启也会被当成 @ 了机器人而处理，线上务必配。

`worker/src/index.ts`：

```ts
/**
 * Webhook 接收器与转发器。
 *
 * 接收 GitHub / Gitee 的 Webhook，把目标仓库与 PR/Issue 信息落成一个 JSON 任务文件，
 * Push 到公开中转仓库，由中转仓库的 GitHub Actions 拉起 AI 执行（审查或干活）。
 *
 * 之所以要中转：执行方的 Actions 跑在公开中转仓库上（免费额度），
 * 而代码要从目标仓库拉取，所以这里只传递"对谁做什么"的描述，不传递代码本身。
 *
 * 触发规则（统一归一化成 mode: review | work）：
 * - PR 开启类事件（opened 等）：默认 mode=review，除非正文/描述里 @ 了机器人在先；
 * - Issue 开启：默认不管，除非正文里 @ 了机器人；
 * - 评论事件（issue_comment / note）：只有正文里 `@BOT_NAME ` 之后跟 review / work 才处理，
 *   否则连同 Issue 上的评论一起丢弃；
 * - @ 的识别：`@` + BOT_NAME + 一个空格，后面第一个词是 review 或 work；
 *   review → 评审，work → 按后面的自然语言描述干活。
 *
 * 写中转仓库的身份优先用 App：GitHub 走 App 安装令牌，Gitee 走 App access_token，
 * 两者都拿不到时回退个人令牌，见 app-auth.ts。
 */

import { resolveToken } from './app-auth.ts'
import type { AppEnv } from './env.ts'
import { implicitMode, parseBotDirective, type TaskMode } from './mode.ts'

export type Env = AppEnv
export type { TaskMode }

interface ReviewTask {
  /** 平台标识，Agent 端据此选择 API 基址与鉴权方式 */
  provider: 'github' | 'gitee'
  repo: string
  pr_number: number
  /** Issue 事件下 PR 号可能为空，用 is_issue 区分回写目标 */
  is_issue: boolean
  title: string
  body: string
  html_url: string
  user: string
  /** 事件动作：opened / synchronize / created 等，透传给 Agent */
  action: string
  /** 执行模式，Agent 端据此选配置与提示词 */
  mode: TaskMode
  /** work 模式下的自然语言要求：`@BOT_NAME work ` 之后的原文；review 模式为空串 */
  instruction: string
  /**
   * 上游（待执行）仓库的克隆地址。
   * Agent 侧只拿到这份 JSON 与一枚令牌，靠这个 URL 自己 clone 代码，
   * 因此这里必须给出可 clone 的地址，而不是只有 owner/repo。
   */
  repo_url: string
  /** PR 的基线提交，Agent 用它算 diff */
  base_sha: string
  /** PR 的最新提交，Agent 按它读代码 */
  head_sha: string
  /** 源分支（Gitee 上是 source_branch） */
  head_ref: string
  /** 目标分支 */
  base_ref: string
  created_at: string
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method !== 'POST') {
      return new Response('Method Not Allowed', { status: 405 })
    }

    // 原始报文必须只读一次，后续签名校验和解析都用它
    const rawBody = await request.text()
    const provider = detectProvider(request)

    const authError = await verifySignature(provider, request, rawBody, env)
    if (authError) {
      return new Response(authError, { status: 401 })
    }

    let task: ReviewTask | null
    try {
      task = parsePayload(provider, JSON.parse(rawBody), request, env)
    } catch (err) {
      return new Response(`Bad payload: ${(err as Error).message}`, { status: 400 })
    }

    // 不是要处理的事件（未 @ 机器人的评论、Issue 开启等），直接忽略，避免噪声触发
    if (!task) {
      // 204 不允许带 body，状态码本身就是全部信息
      return new Response(null, { status: 204 })
    }

    // 先返回 202，再让 Push 过程继续跑，避免 Webhook 发送方等超时
    const work = pushTaskToControlRepo(env, task).catch((err) => {
      console.error('push task failed', err)
    })

    try {
      // @ts-expect-error waitUntil 由 Cloudflare Workers 运行时注入
      const ctx = globalThis.ctx
      if (ctx?.waitUntil) {
        ctx.waitUntil(work)
      }
    } catch {
      // 非 Workers 运行环境（如单元测试）忽略
    }

    return new Response('Accepted', { status: 202 })
  },
}

function detectProvider(request: Request): 'github' | 'gitee' {
  // Gitee 会带 X-Gitee-Token / X-Gitee-Event，GitHub 用 X-GitHub-Event
  if (request.headers.get('x-gitee-event') || request.headers.get('x-gitee-token')) {
    return 'gitee'
  }
  return 'github'
}

/** 校验 Webhook 来源；返回错误文案表示校验失败，返回 null 表示通过 */
async function verifySignature(
  provider: 'github' | 'gitee',
  request: Request,
  rawBody: string,
  env: Env,
): Promise<string | null> {
  if (provider === 'gitee') {
    const secret = env.GITEE_WEBHOOK_SECRET
    if (!secret) return null
    // Gitee 不支持签名，只在 URL 查询串或 Header 里回带明文密码，用固定时间比较
    const token = request.headers.get('x-gitee-token') ?? new URL(request.url).searchParams.get('token') ?? ''
    return timingSafeEqual(token, secret) ? null : 'Invalid Gitee token'
  }

  const secret = env.GITHUB_WEBHOOK_SECRET
  if (!secret) return null

  const signature = request.headers.get('x-hub-signature-256')
  if (!signature?.startsWith('sha256=')) {
    return 'Missing X-Hub-Signature-256'
  }

  const expected = await hmacSha256Hex(secret, rawBody)
  const provided = signature.slice('sha256='.length)
  return timingSafeEqual(provided, expected) ? null : 'Signature mismatch'
}

async function hmacSha256Hex(secret: string, payload: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  )
  const signature = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(payload))
  return [...new Uint8Array(signature)].map((b) => b.toString(16).padStart(2, '0')).join('')
}

/** 定长比较，避免按字符提前返回导致的时间侧信道 */
function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false
  let diff = 0
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i)
  }
  return diff === 0
}

/** 把两个平台的 Payload 归一化成同一份任务描述 */
function parsePayload(
  provider: 'github' | 'gitee',
  payload: any,
  request: Request,
  env: Env,
): ReviewTask | null {
  // 机器人 @ 名来自环境变量 BOT_NAME；不配就退化成"任意 @ 提及"
  const botName = (env.BOT_NAME ?? '').trim()
  // 评论事件（GitHub issue_comment / Gitee note）与 PR 等事件的处理规则不同，先区分开
  const event = (request.headers.get('x-github-event') ?? request.headers.get('x-gitee-event') ?? '').trim()
  const comment = event === 'issue_comment' || event === 'note'
  return provider === 'gitee'
    ? parseGiteePayload(payload, botName, comment)
    : parseGitHubPayload(payload, botName, comment)
}

/**
 * PR 开启类事件：@ 了机器人就按 @ 的模式走，否则默认 review。
 * 取的是 PR 描述正文（GitHub 在 `pull_request` 事件里同时给出 PR 与可选 comment）。
 */
function prEventMode(payload: any, botName: string): { mode: TaskMode; instruction: string } {
  for (const text of [payload.comment?.body, payload.pull_request?.body]) {
    const directive = parseBotDirective(text, botName)
    if (directive) return directive
  }
  for (const text of [payload.comment?.body, payload.pull_request?.body]) {
    const mode = implicitMode(text, botName)
    if (mode) return { mode, instruction: '' }
  }
  return { mode: 'review', instruction: '' }
}

function parseGitHubPayload(payload: any, botName: string, isComment: boolean): ReviewTask | null {
  const action: string = payload.action ?? ''
  const commentBody: string = payload.comment?.body ?? ''

  // 评论事件：必须有 `@BOT_NAME review|work` 才处理，其余一律丢弃
  if (isComment || action === 'created') {
    if (!payload.issue) return null
    const directive = parseBotDirective(commentBody, botName)
    if (!directive) return null
    const issue = payload.issue
    const repo = payload.repository?.full_name ?? ''
    // GitHub 评论接口两个平台一致：PR 的评论也走 issues/{n}/comments
    return {
      provider: 'github',
      repo,
      pr_number: issue.number,
      // 是 PR 就回写到 PR，否则回写到 Issue
      is_issue: !issue.pull_request,
      title: issue.title ?? '',
      // work 模式下评论正文就是这条任务的要求，交给 Agent 读，避免只传 instruction 丢上下文
      body: commentBody,
      html_url: issue.html_url ?? payload.comment?.html_url ?? '',
      user: payload.comment?.user?.login ?? '',
      action: action || 'created',
      mode: directive.mode,
      instruction: directive.instruction,
      repo_url:
        payload.repository?.clone_url ?? (repo ? `https://github.com/${repo}.git` : ''),
      base_sha: '',
      head_sha: '',
      base_ref: '',
      head_ref: '',
      created_at: new Date().toISOString(),
    }
  }

  if (payload.pull_request) {
    const allowed = new Set(['opened', 'reopened', 'synchronize', 'ready_for_review'])
    if (!allowed.has(action)) return null
    const pr = payload.pull_request
    const repo = payload.repository.full_name
    const { mode, instruction } = prEventMode(payload, botName)
    return {
      provider: 'github',
      repo,
      pr_number: pr.number,
      is_issue: false,
      title: pr.title ?? '',
      body: pr.body ?? '',
      html_url: pr.html_url ?? '',
      user: pr.user?.login ?? '',
      action,
      mode,
      instruction,
      // 上游仓库地址：Agent 靠它 clone，优先用 payload 里的 clone_url，回退按 repo 拼
      repo_url: payload.repository.clone_url ?? `https://github.com/${repo}.git`,
      base_sha: pr.base?.sha ?? '',
      head_sha: pr.head?.sha ?? '',
      base_ref: pr.base?.ref ?? '',
      head_ref: pr.head?.ref ?? '',
      created_at: new Date().toISOString(),
    }
  }

  // Issue 开启默认不管；只有描述里 @ 了机器人才按 @ 的模式处理
  if (payload.issue && action === 'opened') {
    const directive = parseBotDirective(payload.issue.body ?? '', botName)
    if (!directive) return null
    const issue = payload.issue
    const repo = payload.repository.full_name
    return {
      provider: 'github',
      repo,
      pr_number: issue.number,
      is_issue: true,
      title: issue.title ?? '',
      body: issue.body ?? '',
      html_url: issue.html_url ?? '',
      user: issue.user?.login ?? '',
      action,
      mode: directive.mode,
      instruction: directive.instruction,
      repo_url: payload.repository.clone_url ?? `https://github.com/${repo}.git`,
      base_sha: '',
      head_sha: '',
      base_ref: '',
      head_ref: '',
      created_at: new Date().toISOString(),
    }
  }

  return null
}

function parseGiteePayload(payload: any, botName: string, isComment: boolean): ReviewTask | null {
  const repo = payload.repository?.full_name ?? payload.project?.path_with_namespace ?? ''
  const repoUrl =
    (payload.repository ?? payload.project)?.html_url
      ? `${((payload.repository ?? payload.project).html_url as string).replace(/\/$/, '')}.git`
      : repo
        ? `https://gitee.com/${repo}.git`
        : ''
  const action: string = payload.action ?? ''
  const commentBody: string = payload.comment?.body ?? ''

  // 评论事件（note）：同样必须 @BOT_NAME + 模式，且要能判断目标 Issue / PR
  if (isComment || action === 'created') {
    const issue = payload.issue ?? payload.pull_request
    if (!issue) return null
    const directive = parseBotDirective(commentBody, botName)
    if (!directive) return null
    const isIssue = Boolean(payload.issue) && !payload.issue?.pull_request && !payload.pull_request
    return {
      provider: 'gitee',
      repo,
      pr_number: issue.number,
      is_issue: isIssue,
      title: issue.title ?? '',
      body: commentBody,
      html_url: issue.html_url ?? payload.comment?.html_url ?? '',
      user: payload.comment?.user?.login ?? '',
      action: action || 'created',
      mode: directive.mode,
      instruction: directive.instruction,
      repo_url: repoUrl,
      base_sha: '',
      head_sha: '',
      base_ref: '',
      head_ref: '',
      created_at: new Date().toISOString(),
    }
  }

  // Gitee 的 PR 事件里 pull_request 与 issue 事件里 issue 的结构与 GitHub 不同名
  if (payload.pull_request) {
    const pr = payload.pull_request
    const { mode, instruction } = prEventMode(payload, botName)
    return {
      provider: 'gitee',
      repo,
      pr_number: pr.number,
      is_issue: false,
      title: pr.title ?? '',
      body: pr.body ?? '',
      html_url: pr.html_url ?? '',
      user: pr.user?.login ?? '',
      action: action || 'update',
      mode,
      instruction,
      repo_url: pr.head?.repo?.clone_url ?? repoUrl,
      base_sha: pr.base?.sha ?? '',
      head_sha: pr.head?.sha ?? '',
      base_ref: pr.base?.ref ?? '',
      // Gitee 的 PR 结构用 source_branch / target_branch，不是 head.ref / base.ref
      head_ref: pr.head?.ref ?? pr.source_branch ?? '',
      created_at: new Date().toISOString(),
    }
  }

  // Issue 开启：默认不管，@ 了机器人（正文里先 @ 再跟模式词）才处理
  if (payload.issue) {
    const directive = parseBotDirective(payload.issue.body ?? '', botName)
    if (!directive) return null
    const issue = payload.issue
    return {
      provider: 'gitee',
      repo,
      pr_number: issue.number,
      is_issue: true,
      title: issue.title ?? '',
      body: issue.body ?? '',
      html_url: issue.html_url ?? '',
      user: issue.user?.login ?? '',
      action: action || 'update',
      mode: directive.mode,
      instruction: directive.instruction,
      repo_url: repoUrl,
      base_sha: '',
      head_sha: '',
      base_ref: '',
      head_ref: '',
      created_at: new Date().toISOString(),
    }
  }

  return null
}

async function pushTaskToControlRepo(env: Env, task: ReviewTask): Promise<void> {
  const api = (env.GITHUB_API ?? 'https://api.github.com').replace(/\/$/, '')
  // 文件名带时间戳，保证同一 PR 并发到达时不会互相覆盖
  const repoName = task.repo.replace('/', '__')
  const path = `tasks/${repoName}-${task.pr_number}-${Date.now()}.json`
  const url = `${api}/repos/${env.CONTROL_REPO}/contents/${path}`

  // 中转仓库在 GitHub，只能用 GitHub 侧身份；Gitee 的 App 令牌换不来 GitHub 写权限
  const { token, source } = await resolveToken(env, 'github')
  if (source === 'pat') {
    console.warn('[push-task] GitHub App 不可用，已回退 GITHUB_PAT')
  }

  const res = await fetch(url, {
    method: 'PUT',
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/vnd.github+json',
      'Content-Type': 'application/json',
      'User-Agent': 'agent-pr-reviewer-worker',
    },
    body: JSON.stringify({
      message: `chore: enqueue ${task.mode} task for ${task.repo}#${task.pr_number} [skip ci]`,
      content: base64Encode(JSON.stringify(task, null, 2)),
    }),
  })

  if (!res.ok) {
    throw new Error(`GitHub contents API ${res.status}: ${await res.text()}`)
  }
}

/** Workers 无 btoa 对宽字符的支持，先 UTF-8 编码再转 base64 */
function base64Encode(input: string): string {
  const bytes = new TextEncoder().encode(input)
  let binary = ''
  for (const byte of bytes) {
    binary += String.fromCharCode(byte)
  }
  return btoa(binary)
}
```

密钥通过 `wrangler secret put` 注入，不写进 `wrangler.toml`：

```bash
# 在仓库根目录执行（wrangler.toml 也在根目录）
npx wrangler secret put GITHUB_PAT              # 个人令牌，App 不可用时回退，且写中转仓库需要它
npx wrangler secret put GITHUB_WEBHOOK_SECRET
npx wrangler secret put GITEE_WEBHOOK_SECRET    # 只接 GitHub 时可省略
npx wrangler secret put BOT_NAME                # 评论里 @ 的 App 名称，触发规则靠它匹配
npx wrangler secret put GITEE_PAT               # Gitee 个人令牌，Gitee App 失效时回退
# —— 以下是 App 身份，可选；不配则全部走个人令牌 ——
npx wrangler secret put GH_APP_ID
npx wrangler secret put GH_APP_INSTALLATION_ID
npx wrangler secret put GH_APP_PRIVATE_KEY      # 直接粘贴 PEM 全文
npx wrangler secret put GITEE_APP_TOKEN
npx wrangler deploy
```

### App 身份：优先用 App，失败自动回退个人令牌

Worker 与 Agent 两侧都按同一套优先级取令牌：**先换 App 令牌，换不到就用个人令牌**，不会因为 App 配错而中断审查。

| 平台 | App 令牌来源 | 有效期 | 回退 |
| --- | --- | --- | --- |
| GitHub | `GH_APP_ID` + `GH_APP_INSTALLATION_ID` + `GH_APP_PRIVATE_KEY` 签 JWT，换 `/app/installations/{id}/access_tokens` | 1 小时，自动换发 | `GITHUB_PAT` / `PAT_TOKEN` |
| Gitee | `GITEE_APP_TOKEN`（应用授权后下发的 `access_token`，Gitee 无安装令牌换发接口） | 由 Gitee 决定，先做一次 `/user` 有效性探测 | `GITEE_PAT` / `PAT_TOKEN` |

- 令牌只在进程内缓存：Worker 侧按平台缓存并在过期前 5 分钟刷新，换发失败退避 60 秒再试；
  Agent 侧每次运行只换一次，回写评论前再确认一次。
- Gitee 应用没有"安装令牌"概念，只能拿到授权时的 `access_token`，因此 Gitee 侧只做探测不做事后换发；
  这一点与 GitHub 不同，不是实现遗漏。
- 拉取**跨平台**代码（GitHub Actions 拉 Gitee 仓库）时，Gitee App 令牌无法直接用于 `actions/checkout`，
  这种情况下由 workflow 的 `checkout_token` 回退 `PAT_TOKEN`。

> ⚠️ Gitee 应用令牌请用**能长期有效**的那种（Gitee 个人令牌可设长有效期）；
> 若 App 令牌过期又没配 `GITEE_PAT`，回写会失败并在 Actions 日志中报错。

---

## 模块二：中转仓库的 GitHub Actions

`control-repo/.github/workflows/ai-review.yml`：

```yaml
name: AI Review

on:
  push:
    paths:
      - 'tasks/*.json'
  # 备用触发方式：不依赖 Push，避免清理 JSON 造成仓库膨胀
  repository_dispatch:
    types: [ai-review]
  # 手动补跑：除了下面这些输入，也可以在仓库变量里配
  #   UPSTREAM_REPO / AI_API_BASE / AI_API_KEY / AI_MODEL，日常不必手填
  workflow_dispatch:
    inputs:
      upstream_repo:
        description: '上游（待审查）仓库 URL，如 https://github.com/owner/repo'
        required: false
      provider:
        description: '平台：github / gitee'
        required: false
        default: github
      target_repo:
        description: '目标仓库 owner/repo（回写评论用）'
        required: false
      pr_number:
        description: 'PR / Issue 编号'
        required: false
      is_issue:
        description: '是否是 Issue（true/false）'
        required: false
        default: 'false'
      mode:
        description: '执行模式：review（只评审）/ work（按 instruction 干活）'
        required: false
        default: 'review'
      instruction:
        description: 'work 模式的自然语言要求'
        required: false

# 同一 PR 的多次提交只保留最新一次审查，旧任务直接被取消
concurrency:
  group: ai-review-${{ github.repository }}-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: write

env:
  # 上游仓库 URL：真正使用时，待审查仓库里只有本 workflow 文件，
  # 代码由 agent 自己克隆，所以仓库地址必须由上游设置进来。
  # 优先级：workflow_dispatch 输入 > 仓库变量 UPSTREAM_REPO > 任务 JSON 的 repo_url/clone_url
  UPSTREAM_REPO: ${{ inputs.upstream_repo || vars.UPSTREAM_REPO }}
  PROVIDER: ${{ inputs.provider || vars.PROVIDER || 'github' }}

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout control repo
        uses: actions/checkout@v4
        with:
          # 需要 push 回清理提交，用 PAT 而不是默认 GITHUB_TOKEN
          token: ${{ secrets.PAT_TOKEN }}
          fetch-depth: 2

      - name: Read task payload
        id: task
        env:
          # workflow_dispatch 时用输入兜底，其余场景走任务 JSON
          INPUT_PROVIDER: ${{ inputs.provider || vars.PROVIDER || 'github' }}
          INPUT_REPO: ${{ inputs.target_repo }}
          INPUT_NUMBER: ${{ inputs.pr_number }}
          INPUT_ISSUE: ${{ inputs.is_issue || 'false' }}
          INPUT_MODE: ${{ inputs.mode || 'review' }}
          INPUT_INSTRUCTION: ${{ inputs.instruction }}
          INPUT_UPSTREAM: ${{ inputs.upstream_repo || vars.UPSTREAM_REPO }}
        run: |
          set -euo pipefail

          if [ "${{ github.event_name }}" = "repository_dispatch" ]; then
            echo '${{ toJson(github.event.client_payload) }}' > /tmp/task.json
            TASK_FILE=""
          elif [ "${{ github.event_name }}" = "workflow_dispatch" ]; then
            # 手动触发：输入优先，缺的字段回落到仓库变量
            jq -n \
              --arg provider "$INPUT_PROVIDER" \
              --arg repo "$INPUT_REPO" \
              --arg number "$INPUT_NUMBER" \
              --arg is_issue "$INPUT_ISSUE" \
              --arg repo_url "$INPUT_UPSTREAM" \
              --arg mode "$INPUT_MODE" \
              --arg instruction "$INPUT_INSTRUCTION" \
              '{provider: $provider, repo: $repo, pr_number: ($number | tonumber? // 0), is_issue: ($is_issue == "true"), repo_url: $repo_url, mode: $mode, instruction: $instruction}' \
              > /tmp/task.json
            TASK_FILE=""
          else
            # 取本次 push 新增的 JSON；用 diff-tree，避免被其它改动干扰
            TASK_FILE=$(git diff-tree --no-commit-id --name-only -r "${{ github.sha }}" | grep '^tasks/.*\.json$' | head -n 1 || true)
            if [ -z "$TASK_FILE" ]; then
              echo "无任务文件，跳过" && exit 0
            fi
            cp "$TASK_FILE" /tmp/task.json
          fi

          # 上游仓库 URL 的三级兜底：手动输入/仓库变量 → 任务 JSON → 按平台拼一个默认值
          UPSTREAM="${UPSTREAM_REPO:-$(jq -r '.repo_url // .clone_url // .upstream_repo // ""' /tmp/task.json)}"
          TARGET_REPO=$(jq -r '.repo' /tmp/task.json)
          if [ -z "$UPSTREAM" ] && [ -n "$TARGET_REPO" ]; then
            case "$(jq -r '.provider' /tmp/task.json)" in
              gitee) UPSTREAM="https://gitee.com/${TARGET_REPO}.git" ;;
              *)     UPSTREAM="https://github.com/${TARGET_REPO}.git" ;;
            esac
            echo "未设置上游仓库 URL，按目标仓库推导：$UPSTREAM"
          fi
          if [ -z "$UPSTREAM" ]; then
            echo "::error::缺少上游仓库 URL：请在仓库变量/输入里设置 UPSTREAM_REPO"
            exit 1
          fi

          echo "task_file=$TASK_FILE" >> "$GITHUB_OUTPUT"
          echo "provider=$(jq -r '.provider' /tmp/task.json)" >> "$GITHUB_OUTPUT"
          echo "target_repo=$TARGET_REPO" >> "$GITHUB_OUTPUT"
          echo "pr_number=$(jq -r '.pr_number' /tmp/task.json)" >> "$GITHUB_OUTPUT"
          echo "is_issue=$(jq -r '.is_issue' /tmp/task.json)" >> "$GITHUB_OUTPUT"
          echo "upstream_repo=$UPSTREAM" >> "$GITHUB_OUTPUT"
          # 旧任务 JSON 没有 mode 字段，一律按 review 处理，保证向后兼容
          echo "mode=$(jq -r '.mode // "review"' /tmp/task.json)" >> "$GITHUB_OUTPUT"
          echo "instruction=$(jq -r '.instruction // ""' /tmp/task.json)" >> "$GITHUB_OUTPUT"
          echo "title=$(jq -r '.title // ""' /tmp/task.json)" >> "$GITHUB_OUTPUT"
          echo "html_url=$(jq -r '.html_url // ""' /tmp/task.json)" >> "$GITHUB_OUTPUT"
          echo "user=$(jq -r '.user // ""' /tmp/task.json)" >> "$GITHUB_OUTPUT"
          echo "base_sha=$(jq -r '.base_sha // ""' /tmp/task.json)" >> "$GITHUB_OUTPUT"
          echo "head_sha=$(jq -r '.head_sha // ""' /tmp/task.json)" >> "$GITHUB_OUTPUT"
          echo "base_ref=$(jq -r '.base_ref // ""' /tmp/task.json)" >> "$GITHUB_OUTPUT"
          echo "head_ref=$(jq -r '.head_ref // ""' /tmp/task.json)" >> "$GITHUB_OUTPUT"

      - name: Issue app installation token
        id: auth
        env:
          PROVIDER: ${{ steps.task.outputs.provider }}
          GH_APP_ID: ${{ secrets.GH_APP_ID }}
          GH_APP_INSTALLATION_ID: ${{ secrets.GH_APP_INSTALLATION_ID }}
          GH_APP_PRIVATE_KEY: ${{ secrets.GH_APP_PRIVATE_KEY }}
          GITEE_APP_TOKEN: ${{ secrets.GITEE_APP_TOKEN }}
          PAT_TOKEN: ${{ secrets.PAT_TOKEN }}
        run: |
          set -euo pipefail

          # GitHub App 用私钥签 JWT 再换安装令牌；Gitee 应用只认授权下发的 access_token，
          # 先探测有效性，任一环节失败都回退 PAT，不阻塞审查
          if [ "$PROVIDER" = "gitee" ]; then
            if [ -n "${GITEE_APP_TOKEN:-}" ] && \
               curl -sf -H "User-Agent: ai-review-agent" \
                 "https://gitee.com/api/v5/user?access_token=${GITEE_APP_TOKEN}" > /dev/null; then
              APP_TOKEN="$GITEE_APP_TOKEN"
            fi
          elif [ -n "${GH_APP_ID:-}" ] && [ -n "${GH_APP_INSTALLATION_ID:-}" ] && [ -n "${GH_APP_PRIVATE_KEY:-}" ]; then
            # 私钥或换发接口出问题都属于"App 不可用"，要能降级到 PAT，因此整段用 if 包住而不是直接失败
            printf '%s' "$GH_APP_PRIVATE_KEY" > /tmp/app.pem
            NOW=$(date +%s)
            HEADER=$(printf '%s' '{"alg":"RS256","typ":"JWT"}' | openssl base64 -A | tr '+/' '-_' | tr -d '=')
            PAYLOAD=$(printf '{"iat":%s,"exp":%s,"iss":"%s"}' "$((NOW-60))" "$((NOW+540))" "$GH_APP_ID" \
              | openssl base64 -A | tr '+/' '-_' | tr -d '=')
            if SIGNATURE=$(printf '%s' "$HEADER.$PAYLOAD" | openssl dgst -sha256 -sign /tmp/app.pem 2>/dev/null \
                 | openssl base64 -A | tr '+/' '-_' | tr -d '=') && [ -n "$SIGNATURE" ]; then
              APP_TOKEN=$(curl -sf -X POST \
                -H "Authorization: Bearer $HEADER.$PAYLOAD.$SIGNATURE" \
                -H "Accept: application/vnd.github+json" \
                -H "X-GitHub-Api-Version: 2022-11-28" \
                -H "User-Agent: ai-review-agent" \
                "https://api.github.com/app/installations/$GH_APP_INSTALLATION_ID/access_tokens" \
                | jq -r '.token // empty' || true)
            else
              echo "GitHub App 私钥无法用于签名，按 App 不可用处理"
            fi
            rm -f /tmp/app.pem
          fi

          if [ -n "${APP_TOKEN:-}" ]; then
            echo "获取到 App 令牌，本次审查使用 App 身份"
            echo "checkout_token=$APP_TOKEN" >> "$GITHUB_OUTPUT"
            echo "fallback_token=$PAT_TOKEN" >> "$GITHUB_OUTPUT"
          else
            echo "未获取到 App 令牌，回退 PAT_TOKEN"
            echo "checkout_token=$PAT_TOKEN" >> "$GITHUB_OUTPUT"
            echo "fallback_token=$PAT_TOKEN" >> "$GITHUB_OUTPUT"
          fi

      - name: Install agent deps
        run: |
          # 待审查仓库只含本 workflow 文件，agent 脚本不在本地，从控制仓库取
          python -m pip install --quiet -r agent/requirements.txt

      - name: Run AI agent
        env:
          # —— 上游仓库（待审查对象）与任务上下文 ——
          # 代码由 review.py 自己克隆到 /tmp 的子目录，这里不需要 checkout
          UPSTREAM_REPO: ${{ steps.task.outputs.upstream_repo }}
          TARGET_REPO: ${{ steps.task.outputs.target_repo }}
          PR_NUMBER: ${{ steps.task.outputs.pr_number }}
          IS_ISSUE: ${{ steps.task.outputs.is_issue }}
          PROVIDER: ${{ steps.task.outputs.provider }}
          MODE: ${{ steps.task.outputs.mode }}
          INSTRUCTION: ${{ steps.task.outputs.instruction }}
          TITLE: ${{ steps.task.outputs.title }}
          HTML_URL: ${{ steps.task.outputs.html_url }}
          USER: ${{ steps.task.outputs.user }}
          BASE_SHA: ${{ steps.task.outputs.base_sha }}
          HEAD_SHA: ${{ steps.task.outputs.head_sha }}
          BASE_REF: ${{ steps.task.outputs.base_ref }}
          HEAD_REF: ${{ steps.task.outputs.head_ref }}
          # —— agent 配置文件位置（相对克隆出来的仓库目录）——
          AGENT_CONFIG: ${{ vars.AGENT_CONFIG || 'agents/config.json' }}
          AGENT_NAME: ${{ vars.AGENT_NAME }}
          # —— 模型 ——
          AI_API_BASE: ${{ secrets.AI_API_BASE || vars.AI_API_BASE }}
          AI_API_KEY: ${{ secrets.AI_API_KEY }}
          AI_MODEL: ${{ secrets.AI_MODEL || vars.AI_MODEL }}
          # —— 回退凭据（App 凭据优先，缺失或失效时 Agent 内部回退）——
          GH_APP_ID: ${{ secrets.GH_APP_ID }}
          GH_APP_INSTALLATION_ID: ${{ secrets.GH_APP_INSTALLATION_ID }}
          GH_APP_PRIVATE_KEY: ${{ secrets.GH_APP_PRIVATE_KEY }}
          GITEE_APP_TOKEN: ${{ secrets.GITEE_APP_TOKEN }}
          GITHUB_TOKEN: ${{ steps.auth.outputs.fallback_token }}
          GITEE_API: ${{ secrets.GITEE_API }}
        run: PYTHONPATH=agent/src python agent/src/review.py

      - name: Cleanup task file
        if: always()
        run: |
          set -euo pipefail
          TASK_FILE="${{ steps.task.outputs.task_file }}"
          if [ -z "$TASK_FILE" ] || [ ! -f "$TASK_FILE" ]; then
            echo "无任务文件需要清理" && exit 0
          fi
          git rm -f "$TASK_FILE"
          git -c user.name="github-actions[bot]" \
              -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
              commit -m "chore: cleanup task [skip ci]"
          # 清理提交只改 tasks/，配合 paths 过滤不会再触发本 workflow；
          # [skip ci] 是双保险，防止其它配置误触
          git push
```

要点：

- **上游仓库 URL 是必填项**：真正使用时待审查仓库里只有本 workflow 文件，代码由 agent 自己克隆。
  取值顺序：`workflow_dispatch` 输入 → 仓库变量 `UPSTREAM_REPO` → 任务 JSON 的
  `repo_url`/`clone_url` → 按 `provider + repo` 拼默认地址。都拿不到会直接 `::error::` 退出。
- **不再 checkout 目标仓库**：job 只 checkout 控制仓库（拿 agent 脚本），代码由
  `review.py` 克隆到 `/tmp` 的子目录，工作目录随之锁在仓库内。
- **并发控制**：`concurrency` + `cancel-in-progress: true`，同一 PR 连续 push 只跑最后一次。
- **防死循环**：`on.push.paths` 只监听 `tasks/*.json`，而清理提交是**删除**该文件；
  `paths` 过滤对删除也生效，因此本不会再触发；`[skip ci]` 作为第二道保险。
- **仓库体积**：JSON 用完即删。若仍在意历史提交带来的膨胀，可改用 `repository_dispatch`
  触发（workflow 已内置该分支），Worker 端把 `PUT contents` 换成 `POST /dispatches`。
- **令牌换发（`Issue app installation token` 步骤）**：GitHub 侧用 `openssl` 签 RS256 JWT，
  再 POST 换安装令牌；Gitee 侧没有换发接口，只探测 `GITEE_APP_TOKEN` 是否有效。
  两条路径任一步失败都落到 `PAT_TOKEN`，不会让整个 job 挂掉，因此该步骤**不需要 `continue-on-error`**。
  换到的令牌交给 agent 用于 `git clone` **与**回写评论，两边身份一致。
- **手动补跑**：`workflow_dispatch` 可直接填上游仓库 URL、目标仓库、PR 号、`mode` 与
  `instruction` 跑一次，适合在新仓库接入时先验证链路；`AGENT_CONFIG` / `AGENT_NAME`
  用仓库变量控制跑哪个 agent。
- **模式透传**：任务 JSON 的 `mode` / `instruction` 由 `Read task payload` 步骤读出，
  分别写进 `MODE` / `INSTRUCTION` 环境变量给 agent；旧 JSON 缺 `mode` 时按 `review` 兜底。

---

## 模块三：AI Agent（Python，基于 OpenAI Agents SDK）

重构后的 agent 只做一件事：**把目标仓库克隆到 `/tmp` 的子目录，然后在仓库里跑一个「只有 bash」的 agent**。
模型自己用 `git` / `rg` / `sed` 去读代码，不需要我们再为「看 diff」「读文件」写 API 工具。

`mode` 决定用哪份提示词（`prompt-review.txt` / `prompt-work.txt`），并决定模型能不能改仓库：
`review` 禁止 `git commit/push`，`work` 放开写权限。除提示词外两条链路完全一致。

### 目录与职责

```text
agent/
├── requirements.txt          # openai-agents + cryptography
└── src/
    ├── review.py             # 入口：取配置 → 完整克隆上游仓库 → 跑 agent → 回写评论
    ├── config.py             # 读 agents/config.json + 按模式选提示词，校验 tools / workdir
    ├── task_context.py       # 任务 JSON / 环境变量 → 给模型的首条消息
    ├── agent_runner.py       # 组装 Agent（chat/completions 兼容网关）并运行
    ├── repo.py               # 完整克隆到 /tmp 子目录、检出 head、URL 脱敏、环境变量清洗
    ├── tools.py              # 唯一的工具：在仓库目录里执行 bash
    ├── app_auth.py           # App 身份优先、PAT 回退的 TokenProvider（沿用）
    └── agents/
        ├── config.json       # agent 配置（运行时读；仓库里放 config.example.json）
        ├── prompt-review.txt # review 模式的系统提示词，纯文本，随便改
        ├── prompt-work.txt   # work 模式的系统提示词（允许 commit/push）
        └── task.example.json # 任务 JSON 字段示例
```

### 为什么这么改

| 需求 | 落地方式 |
| --- | --- |
| 仓库克隆到 `/tmp` 子目录 | `repo.py::clone_repo()`，目录名 `repo-<随机>`，跑完随容器销毁 |
| 完整克隆（不浅克隆） | `git clone --no-single-branch`：全量历史 + 所有分支的 remote ref，`git log`/`git blame`/跨提交 diff 都能用 |
| 限制工作目录在仓库内 | `bash` 工具的 `cwd` 钉在仓库根 + `sanitize_env` 把 `HOME`/`PWD` 也指过去 |
| 只给 bash 工具 | `tools.py` 只实现一个 `bash` function tool，`config.json` 里 `tools: ["bash"]` |
| 系统提示词单独成 txt | `agents/prompt-review.txt` / `agents/prompt-work.txt`，按 `mode` 选，模型读的是文件内容 |
| 两种模式共用一条链路 | `MODE` 环境变量 → `config.load_agent_config(mode=…)` 选提示词，`task_context.build_prompt()` 把 work 的要求写进首条消息 |
| 其他 agent 设置成 json | `agents/config.json`，改完直接生效，不生成任何脚本 |
| CI 需要上游仓库 URL | workflow 传 `UPSTREAM_REPO`（仓库变量/手动输入/任务 JSON 三级兜底），`review.py` 自己 clone |

### agent 配置（`agents/config.json`）

```json
{
  "reviewer": {
    "name": "code-reviewer",
    "prompt_file_review": "prompt-review.txt",
    "prompt_file_work": "prompt-work.txt",
    "model": "gpt-4o-mini",
    "temperature": 0.2,
    "max_turns": 20,
    "workdir": ".",
    "tools": ["bash"],
    "bash": {
      "timeout_seconds": 120,
      "max_output_chars": 30000
    }
  }
}
```

- 顶层每个 key 是一个 agent 角色；`AGENT_NAME` 决定这次跑哪一个（不填取排序后第一个）。
- 提示词按模式分开配（TXT 文件由用户自行编写）：
  - `prompt_file_review` / `prompt_file_work` 显式指定；不填则按约定名
    `prompt-review.txt` / `prompt-work.txt` 自动查找；都没有时回退 `prompt_file`（默认 `prompt.txt`）。
    也就是说**老仓库只放一份 `prompt.txt` 也能继续跑**，两种模式共用它。
  - 这次运行用哪份由任务 JSON 的 `mode` 决定（CI 通过 `MODE` 环境变量传进来）。
- `prompt_file` 相对配置文件目录解析（也支持相对仓库根或绝对路径），内容就是系统提示词。
- `workdir` 必须落在仓库目录内，配到仓库外会直接报错退出。
- `tools` 目前只认 `bash`；写未知工具名会在启动时报错，不会静默忽略。
- 顶层也可以直接写扁平结构（只有 `prompt_file`/`tools`/`model` 等字段）当单 agent 用。
- 路径可用 `AGENT_CONFIG` 覆盖（默认 `agents/config.json`）。

### 唯一的工具：bash

- 命令在 `bash -lc` 下执行，`cwd` = 克隆出来的仓库目录，所以 `git diff`、`rg` 都是相对仓库跑的。
- 支持管道、重定向、`&&`；单条命令默认 120s 超时，输出超 30000 字符会「掐头去尾」截断。
- 命令失败**不抛异常**：把 `[exit N]` 与 stderr 一起回给模型，让它自己调整命令。
- 环境变量做了清洗：带 `TOKEN`/`SECRET`/`PASSWORD`/`KEY` 的变量不会传给命令，
  模型 `env` 不到 `AI_API_KEY` 与平台令牌；`GIT_TERMINAL_PROMPT=0` 避免卡在凭据交互。
- 这只是「防手滑」：agent 与本进程同机，真正的隔离边界是 CI runner 容器本身。

### 克隆与安全

- 克隆是**完整克隆**：`git clone --no-single-branch <auth-url> <dir>`，不带 `--depth`，
  拉全量历史与所有分支的 remote-tracking ref（`origin/<branch>`），
  这样 agent 的 `git log` / `git blame` / `git diff <base>...<head>` 结果才可信；
  代价是耗时与流量更大，`GIT_TIMEOUT` 放宽到 1200s。
- 克隆完由 `checkout_head()` 把工作区切到待审查提交：优先按 `head_sha` 检出，
  sha 在克隆结果里不可达时回退到 `head_ref` 分支，两者都没有则留在默认分支。
- URL 里拼 `x-access-token:<token>@`（Gitee 用 `oauth2:`）。
- 任何日志输出都过 `mask_url()`，报错信息也会把令牌替换成 `***`，不会泄到 Actions 日志里。
- 令牌优先用 App 身份（GitHub 安装令牌 / Gitee 应用令牌），取不到再回退 `PAT_TOKEN`。
- **CI 侧**：只传上游仓库 URL 与任务 JSON，**不再 checkout 代码**，也不需要待审查仓库里有别的文件。

### 运行流程

1. `review.py` 读任务上下文（环境变量优先，其次 `/tmp/task.json`）；
2. 换令牌 → **完整克隆**上游仓库到 `/tmp/repo-xxxx`，再 `git checkout` 到待审查的 head；
3. 读克隆出来的仓库里的 `agents/config.json`，并按 `MODE` 选 `prompt-review.txt` / `prompt-work.txt`（路径可用 `AGENT_CONFIG` 调整）；
4. 组装 agent（`bash` 工具 + 系统提示词 + 首条任务消息），跑 `Runner.run()`；
5. 把最终正文作为评论回写目标仓库（`review` 标题是「AI 代码审查」，`work` 是「AI 执行结果」）；
   任何环节失败都会回写失败评论，不会静默丢任务。
6. `work` 模式额外多两步：提示词允许模型改代码并 `git commit/push`，克隆时按分支而非 sha 准备，
   推回的是触发评论所在 PR 的源分支。

### 环境变量

CI 侧注入，脚本侧只读（真正必填的只有三个上游/模型相关的）：

| 名称 | 必填 | 说明 |
| --- | --- | --- |
| `UPSTREAM_REPO` | ✅ | 上游（待审查）仓库 URL，agent 用它 `git clone` |
| `AI_API_KEY` | ✅ | 模型 API Key |
| `AI_API_BASE` | ❌ | 模型 API 基址，默认 `https://api.openai.com/v1`（DeepSeek 等兼容网关填自己的） |
| `AI_MODEL` | ❌ | 模型名，默认 `gpt-4o-mini` |
| `AGENT_CONFIG` | ❌ | 配置文件路径（相对克隆出来的仓库），默认 `agents/config.json` |
| `AGENT_NAME` | ❌ | 跑配置里的哪个 agent，默认排序后第一个 |
| `MODE` | ❌ | 执行模式 `review` / `work`，默认 `review`；决定用哪份提示词 |
| `INSTRUCTION` | ❌ | `work` 模式的自然语言要求，会写进模型首条消息 |
| `TARGET_REPO` / `PR_NUMBER` / `IS_ISSUE` / `PROVIDER` | ❌ | 回写评论用；`PROVIDER` 决定 API 基址与鉴权方式 |
| `TITLE` / `BODY` / `HTML_URL` / `USER` | ❌ | PR/Issue 元数据，进模型首条消息 |
| `BASE_SHA` / `HEAD_SHA` / `BASE_REF` / `HEAD_REF` | ❌ | 有 sha 时按 sha 浅拉取，只有分支名时按分支克隆 |
| `GH_APP_ID` / `GH_APP_INSTALLATION_ID` / `GH_APP_PRIVATE_KEY` | ❌ | App 优先路径的凭据 |
| `GITEE_APP_TOKEN` | ❌ | Gitee App 优先路径的凭据 |
| `GITHUB_TOKEN` | ❌ | 回退令牌（Actions 里取 `PAT_TOKEN`） |
| `CLONE_ROOT` | ❌ | 克隆根目录，默认 `/tmp` |

> 这些变量不会传给 agent 的 bash 命令：`sanitize_env` 会剔除名字里带
> `TOKEN`/`SECRET`/`PASSWORD`/`KEY` 的项。

### 任务 JSON 字段

任务 JSON 由 Worker 生成（`/tmp/task.json`，`workflow_dispatch` 时由输入组装），
字段示例见 `agent/src/agents/task.example.json`：

```json
{
  "provider": "github",
  "repo": "owner/target-repo",
  "repo_url": "https://github.com/owner/target-repo",
  "pr_number": 42,
  "is_issue": false,
  "title": "feat: 支持自定义构建缓存",
  "body": "PR 描述……",
  "html_url": "https://github.com/owner/target-repo/pull/42",
  "user": "someone",
  "action": "opened",
  "base_sha": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
  "head_sha": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
  "base_ref": "main",
  "head_ref": "feature/cache",
  "created_at": "2026-09-21T13:43:55.000Z"
}
```

`repo_url` 是上游仓库地址（没有它时 CI 会按 `provider + repo` 拼默认地址，
也可以用仓库变量 `UPSTREAM_REPO` 覆盖）。

`mode` / `instruction` 是执行模式相关字段：

- `mode`：`review`（只评审）或 `work`（按描述干活）。旧任务 JSON 没有这个字段，CI 一律按 `review` 处理。
- `instruction`：`work` 模式下的自然语言要求，取自 `@BOT_NAME work ` 之后的原文；`review` 模式为空串。

work 模式的任务里 `body` 是触发它的**评论正文**（不是 PR 描述），方便 Agent 看到完整上下文。

### 触发规则与模式

- PR 开启类事件 → 默认 `review`；描述里 @ 了机器人 → 按 @ 出来的模式。
- Issue 开启 → 默认**丢弃**；描述里 @ 了机器人 → 按 @ 出来的模式。
- 评论事件（GitHub `issue_comment` / Gitee `note`）→ 必须 `@BOT_NAME ` 且后面跟 `review` / `work`，否则丢弃。
- 写法：`@your-app review`、`@your-app work 把 README 里的链接改成 https://…`（后面直接写自然语言，不用引号）。
- 需要在 GitHub / Gitee 的 Webhook 里勾选 **Issue comment / Note** 事件，评论链路才会进来。

`agent/src/review.py`（入口，完整代码）：

```python
"""AI 执行入口：解析配置 → 完整克隆上游仓库到 /tmp → 在仓库内跑 bash agent → 回写评论。

两种模式共用这一条链路，由任务 JSON 的 `mode` 决定行为与提示词：
- `review`：只评审，禁止改仓库（默认）；
- `work`：按评论里的自然语言要求干活，提示词里明确允许 `git commit` / `git push`。

工作流（CI）只需要传两个东西：**上游仓库 URL**（UPSTREAM_REPO）与任务 JSON
（PR/Issue 元数据）。代码由脚本自己 `git clone` 到 /tmp 的子目录，
因此在 CI 里不需要 checkout action，也不要求待审查仓库里有什么特殊文件。

Agent 侧要么用 App 身份（GitHub 安装令牌 / Gitee 应用令牌），要么回退个人令牌，
令牌只在内存里用，克隆用的 URL 做了脱敏，不会出现在日志里。
"""

import os
import sys
import traceback
from pathlib import Path

import requests

from agent_runner import AgentRunError, run_agent
from app_auth import (
    TokenProvider,
    auth_headers,
    build_token_provider,
    gitee_auth_headers,
    gitee_comment_url,
    gitee_query,
    github_comment_url,
)
from config import ConfigError, load_agent_config
from repo import RepoError, clone_repo, checkout_head, mask_url, sanitize_env
from task_context import build_context, build_prompt
from tools import ShellContext

COMMENT_MARKER = "<!-- ai-review-agent -->"
# 评论标题按模式区分，同一 PR 上 review / work 的产出一眼能分清
TITLES = {"review": "## 🤖 AI 代码审查", "work": "## 🤖 AI 执行结果"}
# 克隆根目录：所有仓库都放在 /tmp 的子目录下，跑完即随容器销毁
CLONE_ROOT = os.environ.get("CLONE_ROOT", "/tmp")


def post_comment(provider: str, repo: str, number: int, token: str, body: str, is_issue: bool) -> None:
    """把审查结果写回目标仓库的 PR/Issue 评论区。"""
    if provider == "gitee":
        resp = requests.post(
            f"{gitee_comment_url(repo, number, is_issue)}?{gitee_query(token)}",
            headers=gitee_auth_headers(token),
            json={"body": body},
            timeout=30,
        )
    else:
        resp = requests.post(
            github_comment_url(repo, number),
            headers=auth_headers(token),
            json={"body": body},
            timeout=30,
        )
    resp.raise_for_status()


def _report(provider: str, repo: str, number: int, token: str, body: str, is_issue: bool) -> int:
    """回写评论；失败只记录日志并返回非零码，不再抛异常。"""
    if not (repo and number):
        print("[review] 缺少目标仓库或编号，跳过回写")
        return 1
    try:
        post_comment(provider, repo, number, token, body, is_issue)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1
    return 0


def main() -> int:
    ctx = build_context()
    provider = ctx["provider"]
    is_issue = ctx["is_issue"]
    number = int(ctx["pr_number"] or 0)
    repo = ctx["repo"]
    mode = (ctx.get("mode") or "review").lower()
    if mode not in TITLES:
        mode = "review"
    title = TITLES[mode]
    print(f"[review] 执行模式：{mode}")

    provider_client: TokenProvider = build_token_provider(provider)

    try:
        token = provider_client.token()
    except Exception as err:  # noqa: BLE001
        traceback.print_exc()
        # 连令牌都拿不到就没法回写评论，只能靠 workflow 日志
        print(f"[review] 无法获取令牌：{type(err).__name__}: {err}")
        return 1
    print(f"[review] 鉴权身份：{provider_client.source}")

    def fail(reason: str) -> int:
        body = (
            f"{COMMENT_MARKER}\n{title}失败\n\n"
            f"{reason}\n\n请检查上游仓库 URL、仓库权限与 Actions 日志。"
        )
        print(f"[review] {reason}")
        try:
            # 令牌可能已过期，回写前重新取一次
            return _report(provider, repo, number, provider_client.token(), body, is_issue)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return 1

    # 1) 完整克隆上游仓库到 /tmp 的子目录
    upstream = ctx["upstream_url"]
    # work 模式要提交代码：评论触发时任务里没有 base/head sha，必须按分支克隆才能推回
    branch_for_clone = "" if mode == "work" else ctx["head_ref"]
    print(f"[review] 完整克隆上游仓库：{mask_url(upstream)} → {CLONE_ROOT}")
    try:
        # 完整克隆（全量历史、全部分支），之后再把工作区切到待审查的 head
        repo_dir = clone_repo(
            url=upstream,
            token=token,
            provider=provider,
            branch=branch_for_clone,
            base_sha=ctx["base_sha"],
            head_sha=ctx["head_sha"],
            workdir=CLONE_ROOT,
        )
        # work 模式留在克隆出来的默认分支上（要推回源分支）；其余按 head_sha 精确检出
        if mode == "work":
            checkout_head(repo_dir, "", ctx["head_ref"])
        else:
            checkout_head(repo_dir, ctx["head_sha"], ctx["head_ref"])
    except RepoError as err:
        return fail(f"克隆上游仓库失败：`{err}`")

    # 2) 读配置（config.json + 按模式选提示词），工作目录锁在仓库内
    try:
        cfg = load_agent_config(repo_dir, mode=mode)
        workspace = cfg.resolve_workdir(repo_dir)
    except ConfigError as err:
        return fail(f"agent 配置不可用：`{err}`")
    print(f"[review] agent={cfg.name} 模式={cfg.mode} 工作目录={workspace}")
    print(f"[review] 系统提示词：{cfg.prompt_file}（{len(cfg.instructions)} 字符）")

    # 3) 跑 agent：只给 bash，环境变量剔掉凭据
    cfg.api_base = os.environ.get("AI_API_BASE", "https://api.openai.com/v1")
    cfg.api_key = os.environ.get("AI_API_KEY", "")
    shell = ShellContext(
        workdir=str(workspace),
        timeout=cfg.bash_timeout,
        max_output_chars=cfg.bash_max_output_chars,
        env=sanitize_env({}, workspace),
    )

    try:
        result = run_agent(cfg, shell, build_prompt(ctx), workspace)
        body = f"{COMMENT_MARKER}\n{title}\n\n{result}"
    except (AgentRunError, Exception) as err:  # noqa: BLE001 - 任何异常都要回报到 PR
        traceback.print_exc()
        body = (
            f"{COMMENT_MARKER}\n{title}失败\n\n"
            f"任务执行异常：`{type(err).__name__}: {err}`\n\n"
            "请检查 Actions 日志与环境变量配置。"
        )

    try:
        # 长时间跑 agent 后安装令牌可能已过期，回写前重新取一次
        return _report(provider, repo, number, provider_client.token(), body, is_issue)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
```

`agent/src/tools.py`（唯一的 bash 工具）：

```python
"""agent 唯一的工具：在克隆下来的仓库目录里跑 bash。

只给一个工具是有意为之——模型拿到 shell，读代码、搜调用方、看 git 历史都靠它，
不用再为「看目录」「读文件」「搜关键字」各写一个 API 工具。
安全边界由三件事兜住：工作目录锁在仓库内、环境变量里剔掉凭据、
单条命令有超时与输出上限。
"""

import subprocess
from dataclasses import dataclass

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
        Args:
            command: 要执行的 bash 命令，例如 "git diff --stat" 或 "rg -n 'def foo' src"。
        """
        return run_bash(context.context, command)

    return bash


def build_tools(ctx: ShellContext) -> list:
    """按配置组装工具列表；当前只有 bash，扩展点也在这里。"""
    return [build_bash_tool(ctx)]
```

`agent/src/config.py`（配置文件加载，节选说明见上）：

```python
"""agent 配置文件加载：把 agents/config.json + prompt.txt 变成可运行的设置。

一次运行 = 一个 agent 角色。默认读取 `AGENT_CONFIG`（缺省 `agents/config.json`），
文件里每个 key 是一个 agent 配置，`AGENT_NAME`（缺省第一个）决定这次跑哪一个。
配置是纯 JSON + 纯文本，改完不需要动代码，也不需要重新生成任何脚本。
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = "agents/config.json"
DEFAULT_EXAMPLE_PATH = "agents/config.example.json"
DEFAULT_PROMPT_PATH = "agents/prompt.txt"
DEFAULT_TOOL = "bash"
# 两种执行模式各有默认提示词文件，用户放哪个都行：优先按模式取名，找不到再回退 prompt.txt
DEFAULT_PROMPT_BY_MODE = {"review": "prompt-review.txt", "work": "prompt-work.txt"}

# 未知 tool 名要在启动时直接报错，避免"配了但没生效"这种静默失败
KNOWN_TOOLS = {"bash"}


class ConfigError(RuntimeError):
    """配置缺失或非法，需要在 Actions 日志里一眼看到原因。"""


@dataclass
class AgentConfig:
    """一个 agent 角色的完整设置。"""

    name: str
    instructions: str
    prompt_file: str
    # 本次运行的模式（review / work），进模型首条消息，也给 work 模式决定要不要放开写权限
    mode: str = "review"
    model: str = ""
    temperature: float = 0.2
    max_turns: int = 20
    workdir: str = "."
    tools: list[str] = field(default_factory=lambda: [DEFAULT_TOOL])
    bash_timeout: float = 120.0
    bash_max_output_chars: int = 30_000
    # 运行时注入（不进配置文件，避免把密钥写进仓库）
    api_base: str = ""
    api_key: str = ""

    def resolve_workdir(self, repo_root: Path) -> Path:
        """把配置里的相对路径解析到仓库根之内，越界直接报错。"""
        candidate = (repo_root / self.workdir).resolve()
        if candidate != repo_root and repo_root not in candidate.parents:
            raise ConfigError(f"workdir 越界：{self.workdir} 不在仓库目录 {repo_root} 内")
        return candidate


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"找不到 agent 配置文件：{path}") from None
    except json.JSONDecodeError as err:
        raise ConfigError(f"agent 配置文件不是合法 JSON：{path}（{err}）") from None


def _pick_agent(raw: dict, agent_name: str) -> tuple[str, dict]:
    """从配置里挑出本次要跑的 agent，并兼容"直接写单个 agent"的扁平写法。"""
    if not isinstance(raw, dict):
        raise ConfigError("agent 配置顶层必须是 JSON 对象")

    # 扁平写法：顶层直接就是 name/prompt_file/... 时，视为只有一个 agent
    if "prompt_file" in raw or "tools" in raw or "model" in raw:
        key = raw.get("name") or agent_name or "reviewer"
        return str(key), raw

    if not raw:
        raise ConfigError("agent 配置为空")

    if agent_name:
        if agent_name not in raw:
            raise ConfigError(f"配置里没有名为 {agent_name} 的 agent，可选：{', '.join(sorted(raw))}")
        return agent_name, raw[agent_name]

    first = sorted(raw)[0]
    return first, raw[first]


def _resolve_prompt(prompt_file: str, config_dir: Path, repo_root: Path, required: bool = True) -> Path | None:
    """prompt 路径支持相对配置文件目录、相对仓库根、或绝对路径。"""
    path = Path(prompt_file)
    candidates = [path] if path.is_absolute() else [config_dir / path, repo_root / path]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    if not required:
        return None
    raise ConfigError(f"找不到系统提示词文件：{prompt_file}（已尝试 {', '.join(str(c) for c in candidates)}）")


def _resolve_mode_prompt(mode: str, entry: dict, config_dir: Path, repo_root: Path) -> Path:
    """按模式选提示词：`prompt_file_review` / `prompt_file_work` > 按模式约定名 > prompt_file。

    用户只配了 `prompt.txt` 时两种模式共用它，老仓库不用改就能继续跑。
    """
    explicit = str(entry.get(f"prompt_file_{mode}") or "")
    if explicit:
        found = _resolve_prompt(explicit, config_dir, repo_root)
        assert found is not None
        return found

    for name in (DEFAULT_PROMPT_BY_MODE.get(mode, ""),):
        if not name:
            break
        found = _resolve_prompt(name, config_dir, repo_root, required=False)
        if found:
            return found

    return _resolve_prompt(str(entry.get("prompt_file") or DEFAULT_PROMPT_PATH), config_dir, repo_root)  # type: ignore[return-value]


def load_agent_config(
    repo_root: Path,
    config_path: str = "",
    agent_name: str = "",
    mode: str = "",
) -> AgentConfig:
    """从磁盘读配置；缺配置时给出可照抄的示例路径，而不是含糊的报错。

    mode 决定用哪份提示词（review / work），也决定 work 模式是否放开写权限；
    缺省从环境变量 MODE 读，再缺省按 review。
    """
    mode = (mode or os.environ.get("MODE", "") or "review").strip().lower()
    if mode not in DEFAULT_PROMPT_BY_MODE:
        raise ConfigError(f"不支持的模式：{mode}（可用：review / work）")

    raw_path = config_path or os.environ.get("AGENT_CONFIG", DEFAULT_CONFIG_PATH)
    path = Path(raw_path)
    if not path.is_absolute():
        path = repo_root / path

    if not path.is_file():
        example = path.parent / Path(DEFAULT_EXAMPLE_PATH).name
        hint = f"，可从 {example} 复制一份改名" if example.is_file() else ""
        raise ConfigError(f"找不到 agent 配置文件：{path}{hint}")

    raw = _read_json(path)
    key, entry = _pick_agent(raw, agent_name or os.environ.get("AGENT_NAME", ""))
    if not isinstance(entry, dict):
        raise ConfigError(f"agent {key} 的配置必须是 JSON 对象")

    prompt_path = _resolve_mode_prompt(mode, entry, path.parent, repo_root)
    instructions = prompt_path.read_text(encoding="utf-8")

    tools = entry.get("tools") or [DEFAULT_TOOL]
    if isinstance(tools, str):
        tools = [tools]
    if not isinstance(tools, list) or not tools:
        raise ConfigError("tools 必须是非空数组，例如 [\"bash\"]")
    unknown = [t for t in tools if t not in KNOWN_TOOLS]
    if unknown:
        raise ConfigError(f"不支持的 tool：{', '.join(map(str, unknown))}（可用：{', '.join(sorted(KNOWN_TOOLS))}）")

    bash_cfg = entry.get("bash") or {}
    if not isinstance(bash_cfg, dict):
        raise ConfigError("bash 配置必须是 JSON 对象")

    config = AgentConfig(
        name=str(entry.get("name") or key),
        instructions=instructions,
        prompt_file=str(prompt_path),
        mode=mode,
        model=str(entry.get("model") or ""),
        temperature=float(entry.get("temperature", 0.2)),
        max_turns=int(entry.get("max_turns", 20)),
        workdir=str(entry.get("workdir") or "."),
        tools=[str(t) for t in tools],
        bash_timeout=float(bash_cfg.get("timeout_seconds", 120)),
        bash_max_output_chars=int(bash_cfg.get("max_output_chars", 30_000)),
    )
    # workdir 在加载阶段就校验一次：越界配置越早失败越好查
    config.resolve_workdir(repo_root)
    return config
```

## 安全与踩坑清单

1. **PAT 绝不落盘**：Worker 与 Actions 都只从 Secrets 读，不进代码、不进任务 JSON。
   任务 JSON 里只有 `repo` / `pr_number` 这类公开信息。
2. **凭据最小权限**：PAT 只给中转仓库写权限 + 目标仓库读权限，不要给 `admin`；
   优先用 App 身份，安装令牌 1 小时过期、可按仓库授权，比长期 PAT 更收敛。
3. **App 私钥是最高敏感凭证**：`GH_APP_PRIVATE_KEY` 能换出任意已装仓库的令牌，
   只放 Secret，不进日志、不进任务 JSON；怀疑泄露就立即在 App 设置里重新生成私钥。
4. **回退不能掩盖配置错误**：App 换发失败时会打印原因并降级到 PAT，
   评论身份会从 `xxx[bot]` 变回个人账号——发现身份不对，先去 Actions 日志里搜 `app_auth` / `App 令牌`。
5. **签名校验必开**：`GITHUB_WEBHOOK_SECRET` 缺失时 Worker 会放行所有请求，仅限本地调试，
   线上务必配置。
6. **防死循环**：清理提交靠 `paths` 过滤（删除 `tasks/*.json` 不匹配 `tasks/*.json` 的新增路径）
   加 `[skip ci]` 双保险。若自行改过 `paths`，务必回归验证一次。
7. **仓库体积**：如需长期零堆积，切换 `repository_dispatch` 触发（workflow 已内置），
   任务信息走 `client_payload`，不再产生中转提交。
8. **并发风暴**：`concurrency` 用 `github.ref` 分组，同一分支上的任务会互相取消；
   若希望按 PR 分组，可在 Worker 侧把 PR 号写进分支名或改用 `repository_dispatch` +
   自定义 `concurrency.group`。
9. **上下文上限**：bash 工具单条命令的输出有上限（`bash.max_output_chars`，默认 30000），
   超限会「掐头去尾」截断；PR 描述超 4000 字符也会截断。
   让模型自己用 `git diff --stat`、分段 `git diff <file>` 控制读取量，比预置一堆截断逻辑更稳。
10. **bash 不是沙箱**：`sanitize_env` 只是把带 `TOKEN`/`SECRET`/`PASSWORD`/`KEY` 的环境变量
    剔出去，agent 与本进程同机同用户。真正的隔离边界是 CI runner 的临时容器；
    不要把审查任务跑在有长期凭据的常驻机器上。
11. **克隆 URL 含令牌**：`git clone` 时令牌拼在 URL 里（容器内临时存在），
    所有日志与报错都过 `mask_url()` 脱敏；不要把这套逻辑搬到会持久化 `.git/config` 的环境。
12. **workdir 不能越界**：`config.json` 的 `workdir` 只允许落在仓库目录内，越界会在启动时报错。
13. **配置写错要吵**：未知 tool 名、找不到 `config.json` / `prompt.txt`、JSON 语法错误都会直接失败并回写评论，
    不会静默降级——否则「配了没生效」比跑挂更难查。
14. **失败必回写**：克隆失败、配置错误、模型异常都会转成一条「审查失败」评论，不会静默丢任务。
15. **`work` 模式会写仓库**：它的提示词明确允许 `git add/commit/push`，推的是触发评论所在 PR 的源分支；
    只读评审请走 `review`。`BOT_NAME` 必须配准，否则任何 `@xxx` 都会被当成触发词，
    等于把"谁能驱动机器人改代码"放开了。
16. **评论触发要防自激**：机器人的回复里若带上 `@BOT_NAME work` 之类字样会再次触发自己。
    提示词里已要求不要复述触发词，接入新机器人时记得回归验证一次。

## 二次开发约定

改动 `worker/src/index.ts`、`agent/src` 或 `control-repo/.github/workflows/ai-review.yml` 时，README 里内嵌的
对应代码块必须同步更新——文档与代码不一致会直接误导部署者。
（`worker/src/app-auth.ts`、`worker/src/env.ts`、`worker/src/mode.ts` 没有内嵌代码块，改动它们只需同步本节与 Secrets 表。）可以用一段脚本自查：

```bash
python - <<'PY'
import pathlib, re
readme = pathlib.Path('README.md').read_text()
blocks = re.findall(r'```(ts|python|yaml)\n(.*?)```', readme, re.S)
for path, lang, n in [('worker/src/index.ts', 'ts', 0),
                      ('control-repo/.github/workflows/ai-review.yml', 'yaml', 0)]:
    cands = [b for l, b in blocks if l == lang]
    assert cands[n].strip() == pathlib.Path(path).read_text().strip(), path
python_blocks = [b for l, b in blocks if l == 'python']
for expected, actual in [(python_blocks[0], 'agent/src/review.py'),
                         (python_blocks[1], 'agent/src/tools.py'),
                         (python_blocks[2], 'agent/src/config.py')]:
    assert expected.strip() == pathlib.Path(actual).read_text().strip(), actual
print('README 代码块与源文件一致')
PY
```

## 本地验证

```bash
# Worker 类型检查 + Worker 单测（触发规则 / @模式识别，不联网）
npm install && npx tsc --noEmit
npm test

# Agent 依赖与语法（openai-agents 提供工具调用循环，cryptography 用于 App 私钥签 JWT）
pip install -r agent/requirements.txt && python -m py_compile agent/src/*.py

# Agent 单元测试（不联网、不调模型：配置加载 / URL 脱敏 / 环境清洗 / bash 工具 / 跳过克隆）
python agent/tests/test_units.py

# workflow 语法
python -c "import yaml; yaml.safe_load(open('control-repo/.github/workflows/ai-review.yml'))"
```

Agent 本地冒烟（不碰模型也能验证克隆 + 配置 + 工具）：

```bash
# 用一个假仓库验证「克隆到 /tmp → 读配置 → 跑 bash」这几步
git init /tmp/upstream && (cd /tmp/upstream && echo hi > a.txt && \
  git add -A && git -c user.email=a@b.c -c user.name=a commit -qm init)

UPSTREAM_REPO=/tmp/upstream GITHUB_TOKEN=dummy AI_API_KEY=dummy \
AI_API_BASE=http://127.0.0.1:8000/v1 TARGET_REPO=x/y PR_NUMBER=1 \
  python agent/src/review.py

# 想验 work 模式：加 MODE=work 与 INSTRUCTION，注意提示词要换成 prompt-work.txt
UPSTREAM_REPO=/tmp/upstream GITHUB_TOKEN=dummy AI_API_KEY=dummy \
MODE=work INSTRUCTION='把 a.txt 的内容改成 hello' \
AI_API_BASE=http://127.0.0.1:8000/v1 TARGET_REPO=x/y PR_NUMBER=1 \
  python agent/src/review.py
```

把 `AI_API_BASE` 指向任意 OpenAI 兼容服务（或本地 mock）就能跑通全链路；
克隆用的 URL 只要在日志里看不到令牌，脱敏就算生效。

端到端验证建议：在测试仓库开一个 PR，确认「Worker 返回 202 → 中转仓库出现任务 JSON →
Actions 跑起来 → 目标 PR 收到评论 → 任务 JSON 被删除且未二次触发」。

身份验证：在 Actions 日志里搜「获取到 App 令牌」与「鉴权身份」，确认评论作者是 App（`xxx[bot]`）；
若显示回退，日志里会带上 App 侧失败原因，据此修凭据即可。
