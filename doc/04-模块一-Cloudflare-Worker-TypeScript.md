> [← 返回文档目录](../README.md) ｜ 本分册由 `README.md` 的「模块一：Cloudflare Worker（TypeScript）」章节拆分而来。

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


密钥通过 `wrangler secret put` 注入，不写进 `wrangler.toml`：


### App 身份：优先用 App，失败自动回退个人令牌

Worker 与 Agent 两侧都按同一套优先级取令牌：**先换 App 令牌，换不到就用个人令牌**，不会因为 App 配错而中断审查。

| 平台 | App 令牌来源 | 有效期 | 回退 |
| --- | --- | --- | --- |
| GitHub | `GH_APP_ID` + `GH_APP_INSTALLATION_ID` + `GH_APP_PRIVATE_KEY` 签 JWT，换 `/app/installations/{id}/access_tokens` | 1 小时，自动换发 | `GITHUB_PAT` / `PAT_TOKEN` |
| Gitee | 无 App 身份，始终用个人令牌 `GITEE_PAT` | 由 Gitee 决定（个人令牌建议设长期有效） | — |

- 令牌只在进程内缓存：Worker 侧按平台缓存并在过期前 5 分钟刷新，换发失败退避 60 秒再试；
  Agent 侧每次运行只换一次，回写评论前再确认一次。
- Gitee 不配置 App，直接使用个人令牌 `GITEE_PAT`，无需走授权流程。
- 拉取**跨平台**代码（GitHub Actions 拉 Gitee 仓库）时，由 workflow 的 `checkout_token` 回退 `PAT_TOKEN`。

> ⚠️ Gitee 用个人令牌 `GITEE_PAT`，建议设较长有效期；过期后回写会失败并在 Actions 日志中报错。

---
