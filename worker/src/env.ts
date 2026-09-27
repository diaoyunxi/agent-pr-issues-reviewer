/** Worker 运行时注入的环境变量与密钥（密钥用 `wrangler secret put` 写入） */
export interface AppEnv {
  /** 本仓库（你 fork 的仓库），格式 owner/repo；Worker 把任务 JSON 推到这里的 tasks/ 目录 */
  CONTROL_REPO: string
  /**
   * 机器人在评论里的 @ 名称（GitHub / Gitee 各配一份）。
   * 评论里必须出现 `@BOT_NAME ` 才会触发；PR/Issue 开启时用它判断是否按 @ 的模式执行。
   * 不配则退化为"任意 @ 提及"（Issue 开启也会被认为是 @ 了机器人）。
   */
  BOT_NAME?: string
  /** 个人令牌，App 不可用时回退用；写本仓库的 tasks/ 也需要它 */
  GITHUB_PAT: string
  /** Gitee 个人令牌（Gitee 侧唯一身份凭据） */
  GITEE_PAT?: string
  /** GitHub App ID */
  GH_APP_ID?: string
  /** GitHub App 安装 ID（装到目标仓库所有者账号上的那次安装） */
  GH_APP_INSTALLATION_ID?: string
  /** GitHub App 私钥（PKCS#8 PEM，整段放 Secret） */
  GH_APP_PRIVATE_KEY?: string
  /** GitHub Webhook 的 HMAC 密钥，未配置则不校验（仅限本地调试） */
  GITHUB_WEBHOOK_SECRET?: string
  /** Gitee Webhook 密码，Gitee 用明文密码而非 HMAC */
  GITEE_WEBHOOK_SECRET?: string
  /** GitHub API 基址，仅 GitHub Enterprise 需要覆写 */
  GITHUB_API?: string
  /** Gitee API 基址 */
  GITEE_API?: string
}
