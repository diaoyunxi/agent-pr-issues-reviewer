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
