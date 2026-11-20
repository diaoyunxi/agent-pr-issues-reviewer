> [← 返回文档目录](../README.md) ｜ 本分册由 `README.md` 的「模块二：本仓库（Fork）的 GitHub Actions」章节拆分而来。

## 模块二：本仓库（Fork）的 GitHub Actions

`.github/workflows/ai-review.yml`：


要点：

- **上游仓库 URL 是必填项**：本仓库（fork）只带 Worker / Agent / CI，真正的业务代码由 agent 自己克隆到 `/tmp`。
  取值顺序：任务 JSON 的 `repo_url`/`clone_url` → 仓库变量 `UPSTREAM_REPO` → 按
  `provider + repo` 拼默认地址。都拿不到会直接 `::error::` 退出。
- **不再 checkout 目标仓库**：job 只 checkout 本仓库（fork，里面就带 `agent/` 脚本），代码由
  `review.py` 克隆到 `/tmp` 的子目录，工作目录随之锁在仓库内。
- **并发控制**：`concurrency` + `cancel-in-progress: true`，同一 PR 连续 push 只跑最后一次。
- **防死循环**：`on.push.paths` 只监听 `tasks/*.json`，而清理提交是**删除**该文件；
  `paths` 过滤对删除也生效，因此本不会再触发；`[skip ci]` 作为第二道保险。
- **仓库体积**：JSON 用完即删。若仍在意历史提交带来的膨胀，可改用 `repository_dispatch`
  触发（workflow 已内置该分支），Worker 端把 `PUT contents` 换成 `POST /dispatches`。
- **令牌换发（`Issue app installation token` 步骤）**：GitHub 侧用 `openssl` 签 RS256 JWT，
  再 POST 换安装令牌；Gitee 侧无 App，直接用 `GITEE_PAT`。
  两条路径任一步失败都落到 `PAT_TOKEN`，不会让整个 job 挂掉，因此该步骤**不需要 `continue-on-error`**。
  换到的令牌交给 agent 用于 `git clone` **与**回写评论，两边身份一致。
- **重新触发**：CI 没有手动 `workflow_dispatch`。要补跑一次，要么让 Worker 再 push 一条任务
  JSON，要么（若启用 `repository_dispatch`）由 Worker 重新派发 `ai-review` 事件；所有参数
  仍以 Worker 推送的任务 JSON 为准，不接收手工填写。`AGENT_CONFIG` / `AGENT_NAME`
  用仓库变量控制跑哪个 agent。
- **模式透传**：任务 JSON 的 `mode` / `instruction` 由 `Read task payload` 步骤读出，
  分别写进 `MODE` / `INSTRUCTION` 环境变量给 agent；旧 JSON 缺 `mode` 时按 `review` 兜底。

---
