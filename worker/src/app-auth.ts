/**
 * GitHub App / Gitee App 鉴权：优先用 App 身份，失败自动回退个人令牌（PAT）。
 *
 * GitHub App 用私钥签 JWT，再换取 1 小时有效的安装令牌；
 * Gitee 的「应用」没有安装令牌换发接口，只认授权下发的 access_token，
 * 因此 Gitee 侧直接透传，仅在它失效时回退 PAT。
 */

import type { AppEnv } from './env.ts'

const JWT_TTL_SECONDS = 540
/** 令牌过期前提前刷新，避免请求途中正好失效 */
const REFRESH_MARGIN_SECONDS = 300
/** 换发失败到下次重试之间的等待时间，避免每个请求都打一次失败的 App 接口 */
const FAILURE_BACKOFF_SECONDS = 60

interface CachedToken {
  token: string
  /** 令牌来源，供日志与任务 JSON 记录 */
  source: 'app' | 'pat'
  expiresAt: number
}

const cache = new Map<string, CachedToken>()

/** 取该平台当前可用的令牌：App 优先，App 不可用则回退 PAT */
export async function resolveToken(env: AppEnv, platform: 'github' | 'gitee'): Promise<CachedToken> {
  const fallback = platform === 'gitee' ? env.GITEE_PAT : env.GITHUB_PAT
  const appConfigured = platform === 'gitee' ? Boolean(env.GITEE_APP_TOKEN) : Boolean(
    env.GH_APP_ID && env.GH_APP_INSTALLATION_ID && env.GH_APP_PRIVATE_KEY,
  )

  if (appConfigured) {
    const cached = cache.get(platform)
    if (cached && Date.now() < cached.expiresAt) return cached
    try {
      const issued = platform === 'gitee'
        ? await verifyGiteeAppToken(env)
        : await issueGitHubInstallationToken(env)
      cache.set(platform, issued)
      return issued
    } catch (err) {
      console.error(`[app-auth] ${platform} App 令牌获取失败，回退个人令牌：`, err)
      // 短时退避，避免 Webhook 风暴时反复打失败的 App 接口
      if (!fallback) throw new Error(`App 令牌获取失败且未配置 ${platform} 个人令牌`)
      const degraded: CachedToken = {
        token: fallback,
        source: 'pat',
        expiresAt: Date.now() + FAILURE_BACKOFF_SECONDS * 1000,
      }
      cache.set(platform, degraded)
      return degraded
    }
  }

  if (!fallback) throw new Error(`未配置 ${platform} 的 App 凭据与个人令牌`)
  return { token: fallback, source: 'pat', expiresAt: Date.now() + REFRESH_MARGIN_SECONDS * 1000 }
}

async function issueGitHubInstallationToken(env: AppEnv): Promise<CachedToken> {
  const api = (env.GITHUB_API ?? 'https://api.github.com').replace(/\/$/, '')
  const resp = await fetch(`${api}/app/installations/${env.GH_APP_INSTALLATION_ID}/access_tokens`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${await signGitHubAppJwt(env)}`,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'agent-pr-reviewer-worker',
    },
  })
  if (!resp.ok) {
    throw new Error(`GitHub App 安装令牌接口 ${resp.status}: ${await resp.text()}`)
  }
  const data = (await resp.json()) as { token: string; expires_at?: string }
  const expiresAt = data.expires_at ? Date.parse(data.expires_at) : Date.now() + 3600_000
  return {
    token: data.token,
    source: 'app',
    expiresAt: Math.min(expiresAt, Date.now() + 3600_000) - REFRESH_MARGIN_SECONDS * 1000,
  }
}

/** Gitee 的 App access_token 由授权时下发，这里只探测一次有效性，失效则交给上层回退 */
async function verifyGiteeAppToken(env: AppEnv): Promise<CachedToken> {
  const api = (env.GITEE_API ?? 'https://gitee.com/api/v5').replace(/\/$/, '')
  const resp = await fetch(`${api}/user?access_token=${encodeURIComponent(env.GITEE_APP_TOKEN!)}`, {
    headers: { 'User-Agent': 'agent-pr-reviewer-worker' },
  })
  if (!resp.ok) {
    throw new Error(`Gitee App 令牌校验失败 ${resp.status}: ${await resp.text()}`)
  }
  return {
    token: env.GITEE_APP_TOKEN!,
    source: 'app',
    expiresAt: Date.now() + 3600_000 - REFRESH_MARGIN_SECONDS * 1000,
  }
}

async function signGitHubAppJwt(env: AppEnv): Promise<string> {
  const now = Math.floor(Date.now() / 1000)
  const header = base64UrlEncode(JSON.stringify({ alg: 'RS256', typ: 'JWT' }))
  const payload = base64UrlEncode(
    JSON.stringify({ iat: now - 60, exp: now + JWT_TTL_SECONDS, iss: String(env.GH_APP_ID) }),
  )
  const signingInput = `${header}.${payload}`

  const key = await crypto.subtle.importKey(
    'pkcs8',
    pemToArrayBuffer(normalizePrivateKey(env.GH_APP_PRIVATE_KEY!)),
    { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' },
    false,
    ['sign'],
  )
  const signature = await crypto.subtle.sign(
    'RSASSA-PKCS1-v1_5',
    key,
    new TextEncoder().encode(signingInput),
  )
  return `${signingInput}.${base64UrlEncodeBytes(new Uint8Array(signature))}`
}

/** 换行在 Secret 里常被写成字面量 \n，这里统一还原成真正的换行 */
function normalizePrivateKey(pem: string): string {
  return pem.includes('\\n') ? pem.replace(/\\n/g, '\n') : pem
}

function pemToArrayBuffer(pem: string): ArrayBuffer {
  const body = pem
    .replace(/-----BEGIN [^-]+-----/, '')
    .replace(/-----END [^-]+-----/, '')
    .replace(/\s+/g, '')
  const binary = atob(body)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i)
  }
  return bytes.buffer
}

function base64UrlEncode(input: string): string {
  return base64UrlEncodeBytes(new TextEncoder().encode(input))
}

function base64UrlEncodeBytes(bytes: Uint8Array): string {
  let binary = ''
  for (const byte of bytes) {
    binary += String.fromCharCode(byte)
  }
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}
