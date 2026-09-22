/** Worker 运行时注入的环境变量与密钥（密钥用 `wrangler secret put` 写入） */
export interface AppEnv {
  /** 中转仓库，格式 owner/repo */
  CONTROL_REPO: string
  /** 个人令牌，App 不可用时回退用；写中转仓库也需要它 */
  GITHUB_PAT: string
  /** Gitee 个人令牌，Gitee App 不可用时回退用 */
  GITEE_PAT?: string
  /** GitHub App ID */
  GH_APP_ID?: string
  /** GitHub App 安装 ID（装到目标仓库所有者账号上的那次安装） */
  GH_APP_INSTALLATION_ID?: string
  /** GitHub App 私钥（PKCS#8 PEM，整段放 Secret） */
  GH_APP_PRIVATE_KEY?: string
  /** Gitee 应用的 access_token（授权后下发，无换发接口） */
  GITEE_APP_TOKEN?: string
  /** GitHub Webhook 的 HMAC 密钥，未配置则不校验（仅限本地调试） */
  GITHUB_WEBHOOK_SECRET?: string
  /** Gitee Webhook 密码，Gitee 用明文密码而非 HMAC */
  GITEE_WEBHOOK_SECRET?: string
  /** GitHub API 基址，仅 GitHub Enterprise 需要覆写 */
  GITHUB_API?: string
  /** Gitee API 基址 */
  GITEE_API?: string
}
