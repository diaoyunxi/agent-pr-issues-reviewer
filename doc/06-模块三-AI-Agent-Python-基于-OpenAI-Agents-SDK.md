> [← 返回文档目录](../README.md) ｜ 本分册由 `README.md` 的「模块三：AI Agent（Python，基于 OpenAI Agents SDK）」章节拆分而来。

## 模块三：AI Agent（Python，基于 OpenAI Agents SDK）

重构后的 agent 只做一件事：**把目标仓库克隆到 `/tmp` 的子目录，然后在仓库里跑一个「只有 bash」的 agent**。
模型自己用 `git` / `rg` / `sed` 去读代码，不需要我们再为「看 diff」「读文件」写 API 工具。

`mode` 决定用哪份提示词（`prompt-review.txt` / `prompt-work.txt`），并决定模型能不能改仓库：
`review` 禁止 `git commit/push`，`work` 放开写权限。除提示词外两条链路完全一致。

### 目录与职责


### 为什么这么改

| 需求 | 落地方式 |
| --- | --- |
| 仓库克隆到 `/tmp` 子目录 | `repo.py::clone_repo()`，目录名 `repo-<随机>`，跑完随容器销毁 |
| 完整克隆（不浅克隆） | `git clone --no-single-branch`：全量历史 + 所有分支的 remote ref，`git log`/`git blame`/跨提交 diff 都能用 |
| 限制工作目录在仓库内 | `bash` 工具的 `cwd` 钉在仓库根 + `sanitize_env` 把 `HOME`/`PWD` 也指过去 |
| 只给 bash 工具 | `tools.py` 只实现一个 `bash` function tool，`config.json` 里 `tools: ["bash"]` |
| 系统提示词单独成 txt | `src/prompt-review.txt` / `src/prompt-work.txt`，按 `mode` 选，模型读的是文件内容 |
| 两种模式共用一条链路 | `MODE` 环境变量 → `config.load_agent_config(mode=…)` 选提示词，`task_context.build_prompt()` 把 work 的要求写进首条消息 |
| 其他 agent 设置成 json | `src/config.json`，改完直接生效，不生成任何脚本 |
| CI 需要上游仓库 URL | 由 Worker 推送的任务 JSON 提供 `repo_url`（仓库变量 `UPSTREAM_REPO` 仅作兜底），`review.py` 自己 clone |

### agent 配置（`src/config.json`）


- 顶层每个 key 是一个 agent 角色；`AGENT_NAME` 决定这次跑哪一个（不填取排序后第一个）。
- 提示词按模式分开配（TXT 文件由用户自行编写）：
  - `prompt_file_review` / `prompt_file_work` 显式指定；不填则按约定名
    `prompt-review.txt` / `prompt-work.txt` 自动查找；都没有时回退 `prompt_file`（默认 `prompt.txt`）。
    也就是说**老仓库只放一份 `prompt.txt` 也能继续跑**，两种模式共用它。
  - 这次运行用哪份由任务 JSON 的 `mode` 决定（CI 通过 `MODE` 环境变量传进来）。
- `prompt_file` 相对配置文件目录解析（也支持相对仓库根或绝对路径），内容就是系统提示词。
- `workdir` 必须落在仓库目录内，配到仓库外会直接报错退出。
- `tools` 目前只认 `bash`；写未知工具名会在启动时报错，不会静默忽略。
- 顶层也可以直接写扁平结构（只有 `prompt_file`/`tools`/`model` 等字段）当单 agent 用。
- 路径可用 `AGENT_CONFIG` 覆盖（默认 `src/config.json`）。
- `allow_inline_comments`（布尔，默认 `true`）：仅对 `review` 模式生效。开启后，模型可以把
  针对**具体代码行**的评审意见写成工作目录下的 `ai-review-inline.json`，系统会把这些意见作为
  **行内评论**挂到 PR 的 diff 对应行上（GitHub 用 `line`+`side`，Gitee 用换算后的 `position`）。
  设为 `false` 则只产出总结评论，不做行内评论。

### 唯一的工具：bash

- 命令在 `bash -lc` 下执行，`cwd` = 克隆出来的仓库目录，所以 `git diff`、`rg` 都是相对仓库跑的。
- 支持管道、重定向、`&&`；单条命令默认 120s 超时，输出超 30000 字符会「掐头去尾」截断。
- 命令失败**不抛异常**：把 `[exit N]` 与 stderr 一起回给模型，让它自己调整命令。
- **CI 下命令不直接执行，而是转发给「持密钥的执行器 sidecar」**（`executor.py`，独立进程，通过
  Unix socket 通信）：真令牌只存在于执行器进程，模型发出的命令里只能用 `${GH_TOKEN}` /
  `${GITEE_TOKEN}` 占位符，执行器侧替换真值并脱敏输出后再回传。于是模型既能 `git push` /
  `gh` / `curl` 带令牌，又**永远拿不到令牌本身**。（`sanitize_env` 仍兜底：不带占位符的
  命令在以剔除凭据的环境里运行，`GIT_TERMINAL_PROMPT=0` 避免卡在凭据交互。）
- 无执行器时（本地冒烟/测试）回退为直接执行，此时 `env` 已被剔掉凭据。
- 这只是「防手滑」：agent 与本进程同机，真正的隔离边界是 CI runner 容器本身。

### 克隆与安全

- 克隆是**完整克隆**：`git clone --no-single-branch <auth-url> <dir>`，不带 `--depth`，
  拉全量历史与所有分支的 remote-tracking ref（`origin/<branch>`），
  这样 agent 的 `git log` / `git blame` / `git diff <base>...<head>` 结果才可信；
  代价是耗时与流量更大，`GIT_TIMEOUT` 放宽到 1200s。
- 克隆完由 `checkout_head()` 把工作区切到待审查提交：优先按 `head_sha` 检出，
  sha 在克隆结果里不可达时回退到 `head_ref` 分支，两者都没有则留在默认分支。
- URL 里拼 `x-access-token:<token>@`（Gitee 用 `oauth2:`）用于**克隆**，克隆完立刻把
  `origin` 还原成公开地址，令牌**不留在** `.git/config`；推送认证交给执行器 sidecar 的
  credential helper，模型读不到 `.git/config` 里的令牌。
- 任何日志输出都过 `mask_url()`，报错信息也会把令牌替换成 `***`，不会泄到 Actions 日志里。
- GitHub 优先用 App 身份（安装令牌），取不到回退 `PAT_TOKEN`；Gitee 始终用 `GITEE_PAT`。
- **CI 侧**：只传上游仓库 URL 与任务 JSON，**不再 checkout 代码**，也不需要待审查仓库里有别的文件。
  真令牌仅在执行器 sidecar 进程内使用，AI 进程不持有。

### 运行流程

1. `review.py` 读任务上下文（环境变量优先，其次 `/tmp/task.json`）；
2. 换令牌 → **完整克隆**上游仓库到 `/tmp/repo-xxxx`，再 `git checkout` 到待审查的 head；
3. 读克隆出来的仓库里的 `src/config.json`，并按 `MODE` 选 `prompt-review.txt` / `prompt-work.txt`（路径可用 `AGENT_CONFIG` 调整）；
4. 组装 agent（`bash` 工具 + 系统提示词 + 首条任务消息），跑 `Runner.run()`；
5. 把最终正文作为评论回写目标仓库（`review` 标题是「AI 代码审查」，`work` 是「AI 执行结果」）；
   任何环节失败都会回写失败评论，不会静默丢任务。
6. `work` 模式额外多两步：提示词允许模型改代码并 `git commit/push`，克隆时按分支而非 sha 准备，
   推回的是触发评论所在 PR 的源分支。

### 行内代码评论（review 模式）

`review` 模式下，除了落在总结评论里的 `文件:行号` 描述，还可以让评审意见**直接挂在 PR diff 的对应代码行上**：

- 由提示词驱动：模型审查时把具体代码行的意见写成工作目录下的 `ai-review-inline.json`
  （一个数组，每条 `{path, line, side, body}`），详见 `prompt-review.txt`；
- 由 `review.py` 在 agent 跑完后读取并回写，**令牌只存在于 `review.py` 与执行器侧，模型永远拿不到**；
- 平台差异被封装在 `inline_comments.py` 里：GitHub 用官方推荐的 `line` + `side`（文件行号，直观），
  Gitee 接口只认 diff 内的 `position`，由 `git diff` 现算；
- 任何一条行内评论失败都只记日志、不阻断其余评论与总结评论的回写；
- 开关在配置里：`src/config.json` 的 `allow_inline_comments`（默认 `true`），设为 `false` 即只出总结。

注意：`side` 用 `RIGHT`（新增/修改行，落在 `+` 一侧）或 `LEFT`（被删行，落在 `-` 一侧），
`line` 必须是文件行号且落在改动附近，否则评论可能贴不到正确位置；Issue 没有 diff，不会走行内评论。

### 环境变量

CI 侧注入，脚本侧只读（真正必填的只有三个上游/模型相关的）：

| 名称 | 必填 | 说明 |
| --- | --- | --- |
| `UPSTREAM_REPO` | ✅ | 上游（待审查）仓库 URL，agent 用它 `git clone` |
| `AI_API_KEY` | ✅ | 模型 API Key |
| `AI_API_BASE` | ❌ | 模型 API 基址，默认 `https://api.openai.com/v1`（DeepSeek 等兼容网关填自己的） |
| `AI_MODEL` | ❌ | 模型名，默认 `gpt-4o-mini` |
| `AGENT_CONFIG` | ❌ | 配置文件路径（相对克隆出来的仓库），默认 `src/config.json` |
| `AGENT_NAME` | ❌ | 跑配置里的哪个 agent，默认排序后第一个 |
| `MODE` | ❌ | 执行模式 `review` / `work`，默认 `review`；决定用哪份提示词 |
| `INSTRUCTION` | ❌ | `work` 模式的自然语言要求，会写进模型首条消息 |
| `TARGET_REPO` / `PR_NUMBER` / `IS_ISSUE` / `PROVIDER` | ❌ | 回写评论用；`PROVIDER` 决定 API 基址与鉴权方式 |
| `TITLE` / `BODY` / `HTML_URL` / `USER` | ❌ | PR/Issue 元数据，进模型首条消息 |
| `BASE_SHA` / `HEAD_SHA` / `BASE_REF` / `HEAD_REF` | ❌ | 有 sha 时按 sha 浅拉取，只有分支名时按分支克隆 |
| `GH_APP_ID` / `GH_APP_INSTALLATION_ID` / `GH_APP_PRIVATE_KEY` | ❌ | App 优先路径（仅 GitHub）的凭据 |
| `GITHUB_TOKEN` | ❌ | 回退令牌（Actions 里取 `PAT_TOKEN`） |
| `CLONE_ROOT` | ❌ | 克隆根目录，默认 `/tmp` |

> 这些变量不会传给 agent 的 bash 命令：`sanitize_env` 会剔除名字里带
> `TOKEN`/`SECRET`/`PASSWORD`/`KEY` 的项。

### 任务 JSON 字段

任务 JSON 由 Worker 生成（`/tmp/task.json`，直接由 Webhook 报文解析而成），
字段示例见 `agent/src/src/task.example.json`：


`repo_url` 是上游仓库地址（没有它时 CI 会按 `provider + repo` 拼默认地址，
也可以用仓库变量 `UPSTREAM_REPO` 覆盖）。

`mode` / `instruction` 是执行模式相关字段：

- `mode`：`review`（只评审）或 `work`（按描述干活）。旧任务 JSON 没有这个字段，CI 一律按 `review` 处理。
- `instruction`：`work` 模式下的自然语言要求，取自 `@BOT_NAME work ` 之后的原文；`review` 模式为空串。

work 模式的任务里 `body` 是触发它的**评论正文**（不是 PR 描述），方便 Agent 看到完整上下文。

### 触发规则与模式

- PR 开启类事件 → 默认 `review`；描述里 @ 了机器人 → 按 @ 出来的模式。
- Issue 开启 → 默认**丢弃**；描述里 @ 了机器人 → 按 @ 出来的模式。
- 评论事件（GitHub `issue_comment` / Gitee `note`）→ 必须 `@BOT_NAME ` 且后面跟 `review` / `work`，否则丢弃。
- 写法：`@your-app review`、`@your-app work 把 README 里的链接改成 https://…`（后面直接写自然语言，不用引号）。
- 需要在 GitHub / Gitee 的 Webhook 里勾选 **Issue comment / Note** 事件，评论链路才会进来。

`agent/src/review.py`（入口，完整代码）：


`agent/src/tools.py`（唯一的 bash 工具）：


`agent/src/config.py`（配置文件加载，节选说明见上）：
