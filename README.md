# Agent PR / Issue Reviewer

基于 **Cloudflare Worker + GitHub Actions** 的自动化 AI 代码审查系统。

控制面（公开中转仓库）与数据面（目标业务仓库）分离：Worker 只负责「登记任务」，Actions 负责「拉代码 + 跑 Agent + 回写评论」。

## 数据流向

```text
[目标仓库] (触发 Webhook)
      │
      ▼
[Cloudflare Worker]  (模块一)
      │ 1. 校验签名，提取 repo / PR号 / 标题 / 内容 / URL / user
      │ 2. 生成唯一文件 tasks/{目标仓库名}-{PR号}-{时间戳}.json
      │ 3. 用 GitHub App 安装令牌（回退 PAT）将文件 Push 至【中转仓库】
      ▼
[公开中转仓库] (被 Push 触发)
      │
      ▼
[GitHub Actions]  (模块二) 运行在中转仓库
      │ 1. git diff-tree 找到刚 Push 进来的最新 JSON
      │ 2. jq 解析 JSON，提取目标仓库与 PR 信息
      │ 3. 换发 GitHub App 安装令牌 / Gitee 应用令牌（失败回退 PAT）
      │ 4. 用 App 身份拉取【目标仓库】代码
      │ 5. 执行 AI Agent 脚本  (模块三)
      │ 6. 用 App 身份将审查结果评论回写至【目标仓库】
      │ 7. 清理中转仓库里的 JSON 文件
      ▼
[目标仓库 PR 评论区]
```

## 目录结构

```text
.
├── wrangler.toml           # 模块一：Cloudflare Worker 配置（放根目录，Git 集成自动识别）
├── package.json            #   Worker 依赖与 deploy 脚本
├── tsconfig.json           #   Worker 类型检查配置
├── worker/
│   └── src/
│       ├── index.ts        #   签名校验 / 信息提取 / 触发中转
│       ├── app-auth.ts     #   GitHub/Gitee App 令牌换发与 PAT 回退
│       └── env.ts          #   Worker 环境变量与密钥的类型声明
├── control-repo/           # 中转仓库（内容需复制到中转仓库根目录）
│   └── .github/workflows/
│       └── ai-review.yml   # 模块二：GitHub Actions
└── agent/                  # 模块三：AI Agent（需放入目标仓库）
    ├── requirements.txt
    └── src/
        ├── mini_agent.py   #   自写的工具调用 Agent（GitHub / Gitee 双平台）
        ├── app_auth.py     #   App 令牌优先、PAT 回退的 TokenProvider
        ├── review.py       #   入口：取 diff → 跑 Agent → 回写评论
        └── task_schema.json#   任务 JSON 的字段示例
```

> Worker 的 `wrangler.toml` / `package.json` / `tsconfig.json` 都放在**仓库根目录**，
> 这样 Cloudflare 的 Git 集成（Workers Builds）无需额外配置 root directory 就能自动部署；
> 入口文件通过 `wrangler.toml` 的 `main = "worker/src/index.ts"` 指向。

## 部署步骤

1. **中转仓库**：新建公开仓库，把 `control-repo/.github/workflows/ai-review.yml` 放进去，创建空的 `tasks/` 目录。
2. **目标仓库**：把 `agent/` 目录放进目标仓库，并在仓库里配好 `AI_API_BASE` / `AI_API_KEY` 对应的 Secrets（若走 Actions 侧注入则无需）。
   目标仓库自身的 workflow 不需要改动，审查由中转仓库代跑。
3. **配置 App（推荐）**：建好 GitHub App 与 Gitee 应用，把私钥/令牌写进密钥（见「App 身份配置」一节）。
   App 是可选项——不配就自动用个人令牌，链路照跑，只是评论与提交都以个人账号身份出现。
4. **Worker**：在**仓库根目录**执行 `npx wrangler secret put GITHUB_PAT`，逐个写入密钥后 `npx wrangler deploy`。
   若用 Cloudflare 控制台的 **Workers Builds（连接 Git 仓库）**，直接绑定本仓库即可：
   build 命令留空、deploy 命令用默认的 `npx wrangler deploy`，根目录就是仓库根，无需再改 root directory。
5. **配置 Webhook**：在 GitHub/Gitee 仓库设置里把 Webhook 指向 Worker 域名，内容类型 `application/json`，填入同一份密钥。

---

## 模块一：Cloudflare Worker（TypeScript）

`worker/src/index.ts`：

```ts
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
    return {
      provider: 'github',
      repo: payload.repository.full_name,
      pr_number: pr.number,
      is_issue: false,
      title: pr.title ?? '',
      body: pr.body ?? '',
      html_url: pr.html_url ?? '',
      user: pr.user?.login ?? '',
      action,
      created_at: new Date().toISOString(),
    }
  }

  if (payload.issue && action === 'opened') {
    const issue = payload.issue
    return {
      provider: 'github',
      repo: payload.repository.full_name,
      pr_number: issue.number,
      is_issue: true,
      title: issue.title ?? '',
      body: issue.body ?? '',
      html_url: issue.html_url ?? '',
      user: issue.user?.login ?? '',
      action,
      created_at: new Date().toISOString(),
    }
  }

  return null
}

function parseGiteePayload(payload: any): ReviewTask | null {
  // Gitee 的 PR 事件里 pull_request 与 issue 事件里 issue 的结构与 GitHub 不同名
  if (payload.pull_request) {
    const pr = payload.pull_request
    return {
      provider: 'gitee',
      repo: payload.repository?.full_name ?? payload.project?.path_with_namespace ?? '',
      pr_number: pr.number,
      is_issue: false,
      title: pr.title ?? '',
      body: pr.body ?? '',
      html_url: pr.html_url ?? '',
      user: pr.user?.login ?? '',
      action: payload.action ?? 'update',
      created_at: new Date().toISOString(),
    }
  }

  if (payload.issue) {
    const issue = payload.issue
    return {
      provider: 'gitee',
      repo: payload.repository?.full_name ?? payload.project?.path_with_namespace ?? '',
      pr_number: issue.number,
      is_issue: true,
      title: issue.title ?? '',
      body: issue.body ?? '',
      html_url: issue.html_url ?? '',
      user: issue.user?.login ?? '',
      action: payload.action ?? 'update',
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
```

密钥通过 `wrangler secret put` 注入，不写进 `wrangler.toml`：

```bash
# 在仓库根目录执行（wrangler.toml 也在根目录）
npx wrangler secret put GITHUB_PAT              # 个人令牌，App 不可用时回退，且写中转仓库需要它
npx wrangler secret put GITHUB_WEBHOOK_SECRET
npx wrangler secret put GITEE_WEBHOOK_SECRET    # 只接 GitHub 时可省略
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

# 同一 PR 的多次提交只保留最新一次审查，旧任务直接被取消
concurrency:
  group: ai-review-${{ github.repository }}-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: write

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
        run: |
          set -euo pipefail

          if [ "${{ github.event_name }}" = "repository_dispatch" ]; then
            # repository_dispatch 模式下目标信息直接来自 client_payload
            echo '${{ toJson(github.event.client_payload) }}' > /tmp/task.json
            TASK_FILE=""
          else
            # 取本次 push 新增的 JSON；用 diff-tree，避免被其它改动干扰
            TASK_FILE=$(git diff-tree --no-commit-id --name-only -r "${{ github.sha }}" | grep '^tasks/.*\.json$' | head -n 1 || true)
            if [ -z "$TASK_FILE" ]; then
              echo "无任务文件，跳过" && exit 0
            fi
            cp "$TASK_FILE" /tmp/task.json
          fi

          echo "task_file=$TASK_FILE" >> "$GITHUB_OUTPUT"
          echo "provider=$(jq -r '.provider' /tmp/task.json)" >> "$GITHUB_OUTPUT"
          echo "target_repo=$(jq -r '.repo' /tmp/task.json)" >> "$GITHUB_OUTPUT"
          echo "pr_number=$(jq -r '.pr_number' /tmp/task.json)" >> "$GITHUB_OUTPUT"
          echo "is_issue=$(jq -r '.is_issue' /tmp/task.json)" >> "$GITHUB_OUTPUT"

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

      - name: Checkout target repo
        uses: actions/checkout@v4
        with:
          repository: ${{ steps.task.outputs.target_repo }}
          # 拉代码优先用 App 身份：GitHub 目标仓库拿安装令牌，Gitee 目标仓库拿 Gitee 应用令牌，
          # 都拿不到时回退 PAT_TOKEN（跨平台拉取也只能回退）
          token: ${{ steps.auth.outputs.checkout_token }}
          path: target-repo
          fetch-depth: 0

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install agent deps
        working-directory: target-repo
        run: |
          if [ -f agent/requirements.txt ]; then
            pip install -r agent/requirements.txt
          fi

      - name: Run AI agent
        working-directory: target-repo
        env:
          # App 凭据优先，缺失或失效时 Agent 内部回退到 GITHUB_TOKEN
          GH_APP_ID: ${{ secrets.GH_APP_ID }}
          GH_APP_INSTALLATION_ID: ${{ secrets.GH_APP_INSTALLATION_ID }}
          GH_APP_PRIVATE_KEY: ${{ secrets.GH_APP_PRIVATE_KEY }}
          GITEE_APP_TOKEN: ${{ secrets.GITEE_APP_TOKEN }}
          GITHUB_TOKEN: ${{ steps.auth.outputs.fallback_token }}
          GITEE_API: ${{ secrets.GITEE_API }}
          AI_API_BASE: ${{ secrets.AI_API_BASE }}
          AI_API_KEY: ${{ secrets.AI_API_KEY }}
          AI_MODEL: ${{ secrets.AI_MODEL }}
          TARGET_REPO: ${{ steps.task.outputs.target_repo }}
          PR_NUMBER: ${{ steps.task.outputs.pr_number }}
          IS_ISSUE: ${{ steps.task.outputs.is_issue }}
          PROVIDER: ${{ steps.task.outputs.provider }}
        run: python agent/review.py

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

- **并发控制**：`concurrency` + `cancel-in-progress: true`，同一 PR 连续 push 只跑最后一次。
- **防死循环**：`on.push.paths` 只监听 `tasks/*.json`，而清理提交是**删除**该文件；
  `paths` 过滤对删除也生效，因此本不会再触发；`[skip ci]` 作为第二道保险。
- **仓库体积**：JSON 用完即删。若仍在意历史提交带来的膨胀，可改用 `repository_dispatch`
  触发（workflow 已内置该分支），Worker 端把 `PUT contents` 换成 `POST /dispatches`。
- **令牌换发（`Issue app installation token` 步骤）**：GitHub 侧用 `openssl` 签 RS256 JWT，
  再 POST 换安装令牌；Gitee 侧没有换发接口，只探测 `GITEE_APP_TOKEN` 是否有效。
  两条路径任一步失败都落到 `PAT_TOKEN`，不会让整个 job 挂掉，因此该步骤**不需要 `continue-on-error`**。
- **跨平台拉代码**：`actions/checkout` 的 `token` 用上一步的输出；GitHub Actions 拉 Gitee 仓库时
  只能靠 `PAT_TOKEN`，这是平台限制而非配置疏漏。

---

## 模块三：AI Agent（Python）

`agent/src/app_auth.py`：GitHub App / Gitee App 优先、个人令牌回退的 `TokenProvider`。
GitHub 用私钥签 JWT 换安装令牌（`cryptography` 做 RS256 签名），Gitee 直接透传授权令牌并探测有效性；
令牌按 `TokenProvider` 实例缓存，Agent 的工具调用共用同一份。

`agent/src/mini_agent.py`：一个自写的工具调用 Agent，模型可自行决定拉取哪些文件补充上下文。
`AgentTools` 一套实现同时支持 GitHub 与 Gitee——按 `provider` 选 API 基址、鉴权头与端点，
PR 走 `pulls`、Issue 走 `issues`，所有读请求都通过 `TokenProvider` 取令牌。

```python
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
```

`agent/src/review.py`：入口脚本，负责取 diff、跑 Agent、回写评论，并把异常也回报到 PR。

```python
"""AI 审查入口：读环境变量 → 取 diff → 跑 Agent → 回写评论。

由中转仓库的 GitHub Actions 调用，工作目录是目标仓库的检出目录。
读代码、发评论优先用 GitHub App / Gitee App 身份，App 不可用时回退个人令牌。
"""

import os
import sys
import traceback

import requests

from app_auth import (
    TokenProvider,
    auth_headers,
    build_token_provider,
    gitee_auth_headers,
    gitee_comment_url,
    gitee_query,
    github_comment_url,
)
from mini_agent import AgentTools, build_agent_from_env

COMMENT_MARKER = "<!-- ai-review-agent -->"


def is_issue() -> bool:
    return os.environ.get("IS_ISSUE", "false").lower() == "true"


def post_comment(provider: str, repo: str, number: int, token: str, body: str, is_issue: bool) -> None:
    """把审查结果写回目标仓库的 PR/Issue 评论区。

    PR 与 Issue 的评论在各自平台都是 issues 端点下的资源，统一走这里，不必再判分支。
    """
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


def main() -> int:
    target_repo = os.environ["TARGET_REPO"]
    pr_number = int(os.environ["PR_NUMBER"])
    provider = os.environ.get("PROVIDER", "github")
    use_issue = is_issue()

    provider_client: TokenProvider = build_token_provider(provider)
    # 令牌只在这里取一次，Agent 的工具调用复用同一个 TokenProvider
    token = provider_client.token()
    print(f"[review] 鉴权身份：{provider_client.source}")

    tools = AgentTools(target_repo, pr_number, provider_client, is_issue=use_issue)
    agent = build_agent_from_env(tools)

    try:
        result = agent.run()
        body = f"{COMMENT_MARKER}\n## 🤖 AI 代码审查\n\n{result}"
    except Exception as err:  # noqa: BLE001 - 需要把任何异常都回报给 PR，避免静默失败
        traceback.print_exc()
        # 失败也要回写，否则发起人不知道任务已经挂了
        body = (
            f"{COMMENT_MARKER}\n## 🤖 AI 代码审查失败\n\n"
            f"任务执行异常：`{type(err).__name__}: {err}`\n\n"
            "请检查 Actions 日志与环境变量配置。"
        )

    try:
        # 长时间跑 Agent 后安装令牌可能已过期，回写前重新取一次
        post_comment(provider, target_repo, pr_number, provider_client.token(), body, use_issue)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Gitee 场景已内置：`provider=gitee` 时评论发往 `POST /api/v5/repos/{owner}/{repo}/{pulls|issues}/{number}/comments`，
鉴权头为 `Authorization: token <token>`（同时回带 `access_token` 查询串，兼容只认查询串的路径）；
diff、文件读取与改动统计都换到 Gitee OpenAPI v5 的对应端点，由 `AgentTools` 内部按 `provider` 分派。
令牌同样优先用 `GITEE_APP_TOKEN`，失效时回退 `GITEE_PAT` / `PAT_TOKEN`。

---

## Secrets 与环境变量清单

### Cloudflare Worker（`wrangler secret put`）

| 名称 | 必填 | 说明 |
| --- | --- | --- |
| `GITHUB_PAT` | ✅ | 有 `repo` 权限的 PAT：写中转仓库的 `tasks/`，App 不可用时回退 |
| `GITHUB_WEBHOOK_SECRET` | ✅ | 与 GitHub Webhook 侧填的同一个 HMAC 密钥 |
| `GITEE_WEBHOOK_SECRET` | ❌ | 接 Gitee 时才需要，Gitee 用明文密码校验 |
| `GITEE_PAT` | ❌ | Gitee 个人令牌，Gitee 应用令牌失效时回退 |
| `GH_APP_ID` | ❌ | GitHub App ID，与下列两项一起配齐才启用 App 身份 |
| `GH_APP_INSTALLATION_ID` | ❌ | App 装到目标仓库所有者账号上的安装 ID |
| `GH_APP_PRIVATE_KEY` | ❌ | App 私钥（PKCS#8 PEM），整段贴入，可含字面量 `\n` |
| `GITEE_APP_TOKEN` | ❌ | Gitee 应用授权后下发的 `access_token` |
| `CONTROL_REPO` | ✅ | 中转仓库 `owner/repo`，写在 `wrangler.toml` 的 `[vars]` |
| `GITHUB_API` | ❌ | 仅 GitHub Enterprise 需要覆写 |
| `GITEE_API` | ❌ | Gitee API 基址，默认 `https://gitee.com/api/v5` |

> Worker 侧 App 与 PAT 至少要有一组可用，否则启动写中转仓库直接失败。

### 中转仓库 GitHub Actions Secrets

| 名称 | 必填 | 说明 |
| --- | --- | --- |
| `PAT_TOKEN` | ✅ | 跨仓库 PAT：中转仓库写权限 + 目标仓库读权限；App 不可用时的唯一回退 |
| `GH_APP_ID` | ❌ | 与 Worker 侧同一个 GitHub App，私钥用来换安装令牌 |
| `GH_APP_INSTALLATION_ID` | ❌ | 同上 |
| `GH_APP_PRIVATE_KEY` | ❌ | 同上（PEM 全文） |
| `GITEE_APP_TOKEN` | ❌ | Gitee 应用令牌，`provider=gitee` 时优先使用 |
| `AI_API_BASE` | ✅ | 模型 API 基址，如 `https://api.openai.com/v1` |
| `AI_API_KEY` | ✅ | 模型 API Key |
| `AI_MODEL` | ❌ | 模型名，默认 `gpt-4o-mini` |
| `GITEE_API` | ❌ | 只在目标平台是 Gitee 且使用非官方域名时需要 |

> 这些 Secret 配在中转仓库里，且必须与 Worker 侧用**同一套 App 凭据**，否则两边身份不一致。

### Agent 运行时环境变量

由 workflow 注入，无需手工配置：

| 名称 | 说明 |
| --- | --- |
| `TARGET_REPO` / `PR_NUMBER` / `IS_ISSUE` / `PROVIDER` | 任务描述，来自任务 JSON |
| `GH_APP_ID` / `GH_APP_INSTALLATION_ID` / `GH_APP_PRIVATE_KEY` | App 优先路径的凭据 |
| `GITEE_APP_TOKEN` | Gitee App 优先路径的凭据 |
| `GITHUB_TOKEN` | 回退令牌（Actions 里取 `PAT_TOKEN` 或 `steps.auth.outputs.fallback_token`） |
| `GITEE_API` | Gitee API 基址，可选 |

任务 JSON 的字段定义见 `agent/src/task_schema.json`。

---

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
9. **大 PR 上下文**：diff 与文件内容都有字符上限（见 `mini_agent.py` 顶部常量），
   超限会被截断，宁可让模型少看也不要直接报错。
10. **失败必回写**：Agent 任何异常都会转成一条「审查失败」评论，不会静默丢任务。

## 二次开发约定

改动 `worker/src/index.ts`、`agent/src` 或 `control-repo/.github/workflows/ai-review.yml` 时，README 里内嵌的
对应代码块必须同步更新——文档与代码不一致会直接误导部署者。
（`worker/src/app-auth.ts` 与 `worker/src/env.ts` 没有内嵌代码块，改动它们只需同步本节与 Secrets 表。）可以用一段脚本自查：

```bash
python - <<'PY'
import pathlib, re
readme = pathlib.Path('README.md').read_text()
blocks = re.findall(r'```(ts|python|yaml)\n(.*?)```', readme, re.S)
for path, lang, idx in [('worker/src/index.ts', 'ts', 0),
                        ('agent/src/mini_agent.py', 'python', 0),
                        ('agent/src/review.py', 'python', 1),
                        ('control-repo/.github/workflows/ai-review.yml', 'yaml', 0)]:
    cands = [b for l, b in blocks if l == lang]
    assert cands[idx].strip() == pathlib.Path(path).read_text().strip(), path
print('README 代码块与源文件一致')
PY
```

## 本地验证

```bash
# Worker 类型检查（依赖装在根目录）
npm install && npx tsc --noEmit

# Agent 语法与依赖（cryptography 用于 GitHub App 私钥签 JWT）
cd agent && pip install -r requirements.txt && python -m py_compile src/*.py

# workflow 语法
python -c "import yaml; yaml.safe_load(open('control-repo/.github/workflows/ai-review.yml'))"
```

端到端验证建议：在测试仓库开一个 PR，确认「Worker 返回 202 → 中转仓库出现任务 JSON →
Actions 跑起来 → 目标 PR 收到评论 → 任务 JSON 被删除且未二次触发」。

身份验证：在 Actions 日志里搜「获取到 App 令牌」与「鉴权身份」，确认评论作者是 App（`xxx[bot]`）；
若显示回退，日志里会带上 App 侧失败原因，据此修凭据即可。
