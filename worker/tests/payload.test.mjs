/**
 * Worker 触发规则端到端单测：直接调 fetch handler，看哪些事件会入队、mode 是什么。
 *
 * 用假的 waitUntil + 打桩的 fetch 拦住"写中转仓库"这一步，不产生任何真实请求。
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

const ENV = { CONTROL_REPO: 'me/control', GITHUB_PAT: 'x', BOT_NAME: 'agent-pr-reviewer' }

/** 跑一次 webhook，返回 {status, task}；204 表示按规则丢弃 */
async function dispatch(payload, event, env = ENV) {
  const worker = (await import('../src/index.ts')).default
  const pushed = []
  const originalFetch = globalThis.fetch
  globalThis.fetch = async (url, init) => {
    pushed.push(JSON.parse(new TextDecoder().decode(
      Uint8Array.from(atob(JSON.parse(init.body).content), (c) => c.charCodeAt(0)),
    )))
    return new Response('{}', { status: 201 })
  }
  globalThis.ctx = {
    waitUntil: (promise) => {
      pending.push(promise)
    },
  }
  const pending = []
  try {
    const res = await worker.fetch(
      new Request('https://w.example/', {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'x-github-event': event },
        body: JSON.stringify(payload),
      }),
      env,
    )
    await Promise.all(pending)
    return { status: res.status, task: pushed[0] ?? null }
  } finally {
    globalThis.fetch = originalFetch
    delete globalThis.ctx
  }
}

const repo = { full_name: 'owner/repo', clone_url: 'https://github.com/owner/repo.git' }
const pr = { number: 7, title: 't', body: 'body', html_url: 'u', user: { login: 'a' }, head: {}, base: {} }

test('PR 开启：默认 review', async () => {
  const { status, task } = await dispatch({ action: 'opened', repository: repo, pull_request: pr }, 'pull_request')
  assert.equal(status, 202)
  assert.equal(task.mode, 'review')
  assert.equal(task.instruction, '')
  assert.equal(task.is_issue, false)
})

test('PR 开启但被 @ 了：按 @ 的模式走', async () => {
  const work = await dispatch(
    { action: 'opened', repository: repo, pull_request: { ...pr, body: '@agent-pr-reviewer work 补个测试' } },
    'pull_request',
  )
  assert.equal(work.task.mode, 'work')
  assert.equal(work.task.instruction, '补个测试')

  const mentioned = await dispatch(
    { action: 'opened', repository: repo, pull_request: { ...pr, body: '@agent-pr-reviewer 看看这里' } },
    'pull_request',
  )
  assert.equal(mentioned.task.mode, 'review')
})

test('Issue 开启：默认不管，@ 了才处理', async () => {
  const ignored = await dispatch(
    { action: 'opened', repository: repo, issue: { number: 3, body: '描述', user: {} } },
    'issues',
  )
  assert.equal(ignored.status, 204)
  assert.equal(ignored.task, null)

  const handled = await dispatch(
    { action: 'opened', repository: repo, issue: { number: 3, body: '@agent-pr-reviewer review', user: {} } },
    'issues',
  )
  assert.equal(handled.status, 202)
  assert.equal(handled.task.mode, 'review')
  assert.equal(handled.task.is_issue, true)
})

test('评论事件：必须 @APP 名 + 模式才处理', async () => {
  const base = { action: 'created', repository: repo, issue: { number: 9, title: 't', user: {} } }

  // 没 @ 直接丢弃
  const noMention = await dispatch({ ...base, comment: { body: '随便写点啥', user: { login: 'a' } } }, 'issue_comment')
  assert.equal(noMention.status, 204)

  // @ 了但没跟模式词，也丢弃
  const noMode = await dispatch({ ...base, comment: { body: '@agent-pr-reviewer 你好', user: { login: 'a' } } }, 'issue_comment')
  assert.equal(noMode.status, 204)

  // PR 上的评论，work 模式
  const prComment = await dispatch(
    {
      action: 'created',
      repository: repo,
      issue: {
        number: 9,
        title: 't',
        user: {},
        pull_request: { url: 'https://api.github.com/repos/owner/repo/pulls/9' },
      },
      comment: { body: '@agent-pr-reviewer work 把日志级别改成 info', user: { login: 'a' } },
    },
    'issue_comment',
  )
  assert.equal(prComment.status, 202)
  assert.equal(prComment.task.mode, 'work')
  assert.equal(prComment.task.instruction, '把日志级别改成 info')
  assert.equal(prComment.task.is_issue, false)
  assert.equal(prComment.task.pr_number, 9)

  // Issue 上的评论，review 模式 → 回写到 Issue
  const issueComment = await dispatch(
    { ...base, comment: { body: '@agent-pr-reviewer review', user: { login: 'a' } } },
    'issue_comment',
  )
  assert.equal(issueComment.status, 202)
  assert.equal(issueComment.task.is_issue, true)
  assert.equal(issueComment.task.mode, 'review')
})
