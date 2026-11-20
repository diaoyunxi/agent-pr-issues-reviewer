/**
 * Worker 侧纯逻辑单测：@机器人 与模式识别。
 *
 * 跑法：`npm test`（用 Node 自带的 test runner + tsc 编译出的临时模块，不依赖网络）。
 * 只覆盖 parseBotDirective / implicitMode 这类纯函数，Webhook 全链路仍需在测试仓库端到端验证。
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { parseBotDirective, implicitMode } from '../src/mode.ts'

const BOT = 'agent-pr-reviewer'

test('@机器人 + review 命中', () => {
  assert.deepEqual(parseBotDirective(`@${BOT} review`, BOT), { mode: 'review', instruction: '' })
  assert.deepEqual(parseBotDirective(`@${BOT} review 顺便看看日志`, BOT), { mode: 'review', instruction: '' })
})

test('@机器人 + work 取出后面的自然语言', () => {
  const body = `@${BOT} work 把 README 里的 x 改成 y\n\n第二行也保留`
  assert.deepEqual(parseBotDirective(body, BOT), {
    mode: 'work',
    instruction: '把 README 里的 x 改成 y\n\n第二行也保留',
  })
})

test('大小写不敏感', () => {
  assert.equal(parseBotDirective(`@${BOT} WORK 干活`, BOT)?.mode, 'work')
  assert.equal(parseBotDirective(`@${BOT} Review`, BOT)?.mode, 'review')
})

test('缺 @ / 缺模式词 / 模式词非法时返回 null', () => {
  assert.equal(parseBotDirective('review 一下', BOT), null)
  assert.equal(parseBotDirective(`@other-bot work 干活`, BOT), null)
  assert.equal(parseBotDirective(`@${BOT} 你好`, BOT), null)
  // @ 与模式词之间必须空白分隔，`@bot3` 不算 @ 了 `bot`
  assert.equal(parseBotDirective(`@${BOT}3 work 干活`, BOT), null)
})

test('取的是第一个 @机器人 后面的模式', () => {
  assert.equal(parseBotDirective(`@${BOT} work 干活，@${BOT} review 别听这个`, BOT)?.mode, 'work')
})

test('没配 BOT_NAME 时退化为任意 @ 提及', () => {
  assert.equal(parseBotDirective('@somebody work 干活', '')?.mode, 'work')
  assert.equal(parseBotDirective('没有提及任何人', ''), null)
})

test('implicitMode 只判断有没有 @ 机器人，用于 PR/Issue 开启', () => {
  assert.equal(implicitMode(`@${BOT} 你好`, BOT), 'review')
  assert.equal(implicitMode(`@${BOT}`, BOT), 'review')
  assert.equal(implicitMode('普通描述', BOT), null)
})
