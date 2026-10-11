/**
 * 触发模式识别：从评论/描述正文里解析 `@BOT_NAME review|work`。
 *
 * 抽成独立模块是为了能单独跑单测——它是整套触发规则里唯一有分支的纯逻辑，
 * 而 webhook 的其余部分只是字段搬运。
 */

/** 执行模式：review 只评审、work 按自然语言描述干活 */
export type TaskMode = 'review' | 'work'

/** 把机器名转义成可用于正则的字面量 */
function escapeRegExp(input: string): string {
  return input.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/**
 * 从正文里解析 @机器人 + 模式；没有 @BOT_NAME 或模式词不合法时返回 null（调用方丢弃）。
 *
 * 允许的形式：`@BOT_NAME review`、`@BOT_NAME work 自然语言描述……`
 * - 大小写不敏感；
 * - `@BOT_NAME ` 之后必须紧跟模式词（空白分隔，`@BOT_NAME3` 不算）；
 * - work 模式取模式词之后的原文（多行、代码块原样保留）；
 * - review 模式忽略后面的多余文字。
 */
export function parseBotDirective(
  text: string,
  botName: string,
): { mode: TaskMode; instruction: string } | null {
  const source = text ?? ''
  // 非贪婪匹配第一个 `@` + 机器名 + 空白；不加 g 标志，保证从最早的位置开始找
  const name = escapeRegExp(botName.trim())
  const mention = name ? new RegExp(`@${name}\\s+`) : /@\S+\s+/
  const start = mention.exec(source)
  if (!start) return null

  const rest = source.slice(start.index + start[0].length)
  const keyword = /^(\S+)/.exec(rest)
  const mode = keyword?.[1].toLowerCase()
  if (mode !== 'review' && mode !== 'work') return null

  // review 模式不需要额外的自然语言，忽略后面的文字
  const instruction = mode === 'work' ? rest.slice(keyword![1].length).trim() : ''
  return { mode, instruction }
}

/** @ 机器人了但模式词不合法（如 `@bot 你好`），归一到 review；没 @ 就返回 null */
export function implicitMode(text: string, botName: string): TaskMode | null {
  const source = text ?? ''
  const name = escapeRegExp(botName.trim())
  const mention = name ? new RegExp(`@${name}\\b`) : /@\S+/
  return mention.test(source) ? 'review' : null
}
