/**
 * Webhook 接收器与转发器。
 *
 * 接收 GitHub / Gitee 的 Webhook，把目标仓库与 PR/Issue 信息落成一个 JSON 任务文件，
 * Push 到公开中转仓库，由中转仓库的 GitHub Actions 拉起 AI 审查。
 *
 * 之所以要中转：审查方的 Actions 跑在公开中转仓库上（免费额度），
 * 而代码要从目标仓库拉取，所以这里只传递"审谁"的描述，不传递代码本身。
 *
 * 写中转仓库的身份优先用 App：GitHub 走 App 安装令牌，Gitee 走 App access_token，
 * 两者都拿不到时回退个人令牌，见 app-auth.ts。
 */

import { resolveToken } from './app-auth'
import type { AppEnv } from './env'

export type Env = AppEnv

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
  /** 审查类型：opened / synchronize / issue 等，透传给 Agent */
  action: string
  /**
   * 上游（待审查）仓库的克隆地址。
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
      task = parsePayload(provider, JSON.parse(rawBody))
    } catch (err) {
      return new Response(`Bad payload: ${(err as Error).message}`, { status: 400 })
    }

    // 不是 PR/Issue 相关事件，直接忽略，避免噪声触发
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
function parsePayload(provider: 'github' | 'gitee', payload: any): ReviewTask | null {
  if (provider === 'gitee') {
    return parseGiteePayload(payload)
  }
  return parseGitHubPayload(payload)
}

function parseGitHubPayload(payload: any): ReviewTask | null {
  const action: string = payload.action ?? ''
  const allowed = new Set(['opened', 'reopened', 'synchronize', 'ready_for_review'])

  if (payload.pull_request) {
    if (!allowed.has(action)) return null
    const pr = payload.pull_request
    const repo = payload.repository.full_name
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
      // 上游仓库地址：Agent 靠它 clone，优先用 payload 里的 clone_url，回退按 repo 拼
      repo_url: payload.repository.clone_url ?? `https://github.com/${repo}.git`,
      base_sha: pr.base?.sha ?? '',
      head_sha: pr.head?.sha ?? '',
      base_ref: pr.base?.ref ?? '',
      head_ref: pr.head?.ref ?? '',
      created_at: new Date().toISOString(),
    }
  }

  if (payload.issue && action === 'opened') {
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

function parseGiteePayload(payload: any): ReviewTask | null {
  // Gitee 的 PR 事件里 pull_request 与 issue 事件里 issue 的结构与 GitHub 不同名
  if (payload.pull_request) {
    const pr = payload.pull_request
    const repo = payload.repository?.full_name ?? payload.project?.path_with_namespace ?? ''
    return {
      provider: 'gitee',
      repo,
      pr_number: pr.number,
      is_issue: false,
      title: pr.title ?? '',
      body: pr.body ?? '',
      html_url: pr.html_url ?? '',
      user: pr.user?.login ?? '',
      action: payload.action ?? 'update',
      repo_url: pr.head?.repo?.clone_url ?? `https://gitee.com/${repo}.git`,
      base_sha: pr.base?.sha ?? '',
      head_sha: pr.head?.sha ?? '',
      base_ref: pr.base?.ref ?? '',
      // Gitee 的 PR 结构用 source_branch / target_branch，不是 head.ref / base.ref
      head_ref: pr.head?.ref ?? pr.source_branch ?? '',
      created_at: new Date().toISOString(),
    }
  }

  if (payload.issue) {
    const issue = payload.issue
    const repo = payload.repository?.full_name ?? payload.project?.path_with_namespace ?? ''
    return {
      provider: 'gitee',
      repo,
      pr_number: issue.number,
      is_issue: true,
      title: issue.title ?? '',
      body: issue.body ?? '',
      html_url: issue.html_url ?? '',
      user: issue.user?.login ?? '',
      action: payload.action ?? 'update',
      repo_url: (payload.repository ?? payload.project)?.html_url
        ? `${((payload.repository ?? payload.project).html_url as string).replace(/\/$/, '')}.git`
        : `https://gitee.com/${repo}.git`,
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
      message: `chore: enqueue review task for ${task.repo}#${task.pr_number} [skip ci]`,
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
