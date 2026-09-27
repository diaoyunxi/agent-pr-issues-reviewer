# Agent PR / Issue Reviewer

基于 **Cloudflare Worker + GitHub Actions** 的自动化 AI 代码助手：既能**审查代码**，也能按评论里的自然语言要求**直接改代码**。

控制面（你 **fork 的仓库**）与数据面（目标业务仓库）分离：Worker 只负责「登记任务」，Actions 只负责「跑 Agent + 回写评论」。

Agent 侧是一个**只有 bash 工具**的 OpenAI Agents SDK agent：它把上游仓库 `git clone` 到 `/tmp` 的子目录，然后在仓库里自己读代码；行为由目标仓库里的 `agents/config.json` 与两份提示词（`prompt-review.txt` / `prompt-work.txt`）决定，改配置不用改代码。

触发方式有两种 **mode**：

- **review（评审）**：PR 开启时默认走这条；也可以评论 `@你的App review` 手动触发。
- **work（干活）**：评论 `@你的App work 把 README 里的链接改成 xxx`，模型按描述改代码并提交。

规则细节见「[模块一 → 触发规则](#触发规则哪些事件会跑跑什么模式)」。

## 数据流向


> 注意第 4 步之后**没有 checkout 待审查仓库**：真正的业务代码由 agent 自己克隆到 `/tmp`，
> 仓库地址由 CI 通过 `UPSTREAM_REPO` 传进来。本仓库（fork 出来的控制面）只放 Worker / Agent / CI，不含业务代码。

## 目录结构


> Worker 的 `wrangler.toml` / `package.json` / `tsconfig.json` 都放在**仓库根目录**，
> 这样 Cloudflare 的 Git 集成（Workers Builds）无需额外配置 root directory 就能自动部署；
> 入口文件通过 `wrangler.toml` 的 `main = "worker/src/index.ts"` 指向。

## 部署步骤

默认方式：**直接 fork 本仓库**，fork 出来的仓库已自带 Worker / Agent / CI，开箱即用，无需再单独建一个只放 CI 的中转仓库。

1. **Fork 本仓库**：点 GitHub 右上角 Fork，得到你自己的 `你的名/agent-pr-issues-reviewer`。
   仓库里已经包含 `.github/workflows/ai-review.yml`（CI）与 `tasks/`（任务目录），无需额外搬运任何文件。
2. **目标仓库**：在**真正要被审查的仓库**（可以是任意别的仓库）里放配置与提示词到 `agents/` 目录——
   `agents/config.json`（可从 `agent/src/agents/config.example.json` 复制），提示词按模式拆成
   `agents/prompt-review.txt`（评审用）与 `agents/prompt-work.txt`（干活用），由用户自行编写；
   只放一份 `agents/prompt.txt` 也可以，两种模式会共用它。
   代码由 agent 自己克隆，**不需要**往目标仓库塞 agent 脚本，也不需要改目标仓库的 workflow。
   在**本仓库（fork）**的 Variables 里配好 `UPSTREAM_REPO`（上游仓库 URL，多个仓库用 workflow_dispatch 输入覆盖）。
3. **配置 App（推荐）**：建好 GitHub App 与 Gitee 应用，把私钥/令牌写进本仓库的 Secrets（见「App 身份配置」一节）。
   App 是可选项——不配就自动用个人令牌，链路照跑，只是评论与提交都以个人账号身份出现。
4. **Worker**：在**仓库根目录**执行 `npx wrangler secret put GITHUB_PAT`，逐个写入密钥后 `npx wrangler deploy`。
   若用 Cloudflare 控制台的 **Workers Builds（连接 Git 仓库）**，直接绑定你 fork 出来的仓库即可：
   build 命令留空、deploy 命令用默认的 `npx wrangler deploy`，根目录就是仓库根，无需再改 root directory。
   `wrangler.toml` 里的 `CONTROL_REPO` 默认就是本仓库名，若你改过 fork 名请同步改掉。
5. **配置 Webhook**：在**目标仓库**的 GitHub/Gitee 设置里把 Webhook 指向 Worker 域名，内容类型 `application/json`，填入同一份密钥。
   （Worker 收到事件后会把任务 JSON 推回本仓库的 `tasks/`，再由本仓库的 Actions 拉起 Agent。）

---

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

## 模块二：本仓库（Fork）的 GitHub Actions

`.github/workflows/ai-review.yml`：


要点：

- **上游仓库 URL 是必填项**：本仓库（fork）只带 Worker / Agent / CI，真正的业务代码由 agent 自己克隆到 `/tmp`。
  取值顺序：`workflow_dispatch` 输入 → 仓库变量 `UPSTREAM_REPO` → 任务 JSON 的
  `repo_url`/`clone_url` → 按 `provider + repo` 拼默认地址。都拿不到会直接 `::error::` 退出。
- **不再 checkout 目标仓库**：job 只 checkout 本仓库（fork，里面就带 `agent/` 脚本），代码由
  `review.py` 克隆到 `/tmp` 的子目录，工作目录随之锁在仓库内。
- **并发控制**：`concurrency` + `cancel-in-progress: true`，同一 PR 连续 push 只跑最后一次。
- **防死循环**：`on.push.paths` 只监听 `tasks/*.json`，而清理提交是**删除**该文件；
  `paths` 过滤对删除也生效，因此本不会再触发；`[skip ci]` 作为第二道保险。
- **仓库体积**：JSON 用完即删。若仍在意历史提交带来的膨胀，可改用 `repository_dispatch`
  触发（workflow 已内置该分支），Worker 端把 `PUT contents` 换成 `POST /dispatches`。
- **令牌换发（`Issue app installation token` 步骤）**：GitHub 侧用 `openssl` 签 RS256 JWT，
  再 POST 换安装令牌；Gitee 侧没有换发接口，只探测 `GITEE_APP_TOKEN` 是否有效。
  两条路径任一步失败都落到 `PAT_TOKEN`，不会让整个 job 挂掉，因此该步骤**不需要 `continue-on-error`**。
  换到的令牌交给 agent 用于 `git clone` **与**回写评论，两边身份一致。
- **手动补跑**：`workflow_dispatch` 可直接填上游仓库 URL、目标仓库、PR 号、`mode` 与
  `instruction` 跑一次，适合在新仓库接入时先验证链路；`AGENT_CONFIG` / `AGENT_NAME`
  用仓库变量控制跑哪个 agent。
- **模式透传**：任务 JSON 的 `mode` / `instruction` 由 `Read task payload` 步骤读出，
  分别写进 `MODE` / `INSTRUCTION` 环境变量给 agent；旧 JSON 缺 `mode` 时按 `review` 兜底。

---

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
| 系统提示词单独成 txt | `agents/prompt-review.txt` / `agents/prompt-work.txt`，按 `mode` 选，模型读的是文件内容 |
| 两种模式共用一条链路 | `MODE` 环境变量 → `config.load_agent_config(mode=…)` 选提示词，`task_context.build_prompt()` 把 work 的要求写进首条消息 |
| 其他 agent 设置成 json | `agents/config.json`，改完直接生效，不生成任何脚本 |
| CI 需要上游仓库 URL | workflow 传 `UPSTREAM_REPO`（仓库变量/手动输入/任务 JSON 三级兜底），`review.py` 自己 clone |

### agent 配置（`agents/config.json`）


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
- 路径可用 `AGENT_CONFIG` 覆盖（默认 `agents/config.json`）。

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
- 令牌优先用 App 身份（GitHub 安装令牌 / Gitee 应用令牌），取不到再回退 `PAT_TOKEN`。
- **CI 侧**：只传上游仓库 URL 与任务 JSON，**不再 checkout 代码**，也不需要待审查仓库里有别的文件。
  真令牌仅在执行器 sidecar 进程内使用，AI 进程不持有。

### 运行流程

1. `review.py` 读任务上下文（环境变量优先，其次 `/tmp/task.json`）；
2. 换令牌 → **完整克隆**上游仓库到 `/tmp/repo-xxxx`，再 `git checkout` 到待审查的 head；
3. 读克隆出来的仓库里的 `agents/config.json`，并按 `MODE` 选 `prompt-review.txt` / `prompt-work.txt`（路径可用 `AGENT_CONFIG` 调整）；
4. 组装 agent（`bash` 工具 + 系统提示词 + 首条任务消息），跑 `Runner.run()`；
5. 把最终正文作为评论回写目标仓库（`review` 标题是「AI 代码审查」，`work` 是「AI 执行结果」）；
   任何环节失败都会回写失败评论，不会静默丢任务。
6. `work` 模式额外多两步：提示词允许模型改代码并 `git commit/push`，克隆时按分支而非 sha 准备，
   推回的是触发评论所在 PR 的源分支。

### 环境变量

CI 侧注入，脚本侧只读（真正必填的只有三个上游/模型相关的）：

| 名称 | 必填 | 说明 |
| --- | --- | --- |
| `UPSTREAM_REPO` | ✅ | 上游（待审查）仓库 URL，agent 用它 `git clone` |
| `AI_API_KEY` | ✅ | 模型 API Key |
| `AI_API_BASE` | ❌ | 模型 API 基址，默认 `https://api.openai.com/v1`（DeepSeek 等兼容网关填自己的） |
| `AI_MODEL` | ❌ | 模型名，默认 `gpt-4o-mini` |
| `AGENT_CONFIG` | ❌ | 配置文件路径（相对克隆出来的仓库），默认 `agents/config.json` |
| `AGENT_NAME` | ❌ | 跑配置里的哪个 agent，默认排序后第一个 |
| `MODE` | ❌ | 执行模式 `review` / `work`，默认 `review`；决定用哪份提示词 |
| `INSTRUCTION` | ❌ | `work` 模式的自然语言要求，会写进模型首条消息 |
| `TARGET_REPO` / `PR_NUMBER` / `IS_ISSUE` / `PROVIDER` | ❌ | 回写评论用；`PROVIDER` 决定 API 基址与鉴权方式 |
| `TITLE` / `BODY` / `HTML_URL` / `USER` | ❌ | PR/Issue 元数据，进模型首条消息 |
| `BASE_SHA` / `HEAD_SHA` / `BASE_REF` / `HEAD_REF` | ❌ | 有 sha 时按 sha 浅拉取，只有分支名时按分支克隆 |
| `GH_APP_ID` / `GH_APP_INSTALLATION_ID` / `GH_APP_PRIVATE_KEY` | ❌ | App 优先路径的凭据 |
| `GITEE_APP_TOKEN` | ❌ | Gitee App 优先路径的凭据 |
| `GITHUB_TOKEN` | ❌ | 回退令牌（Actions 里取 `PAT_TOKEN`） |
| `CLONE_ROOT` | ❌ | 克隆根目录，默认 `/tmp` |

> 这些变量不会传给 agent 的 bash 命令：`sanitize_env` 会剔除名字里带
> `TOKEN`/`SECRET`/`PASSWORD`/`KEY` 的项。

### 任务 JSON 字段

任务 JSON 由 Worker 生成（`/tmp/task.json`，`workflow_dispatch` 时由输入组装），
字段示例见 `agent/src/agents/task.example.json`：


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


## 安全与踩坑清单

1. **PAT 绝不落盘**：Worker 与 Actions 都只从 Secrets 读，不进代码、不进任务 JSON。
   任务 JSON 里只有 `repo` / `pr_number` 这类公开信息。
2. **凭据最小权限**：PAT 只给本仓库（fork）写权限 + 目标仓库读权限，不要给 `admin`；
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
   任务信息走 `client_payload`，不再产生本仓库的清理提交。
8. **并发风暴**：`concurrency` 用 `github.ref` 分组，同一分支上的任务会互相取消；
   若希望按 PR 分组，可在 Worker 侧把 PR 号写进分支名或改用 `repository_dispatch` +
   自定义 `concurrency.group`。
9. **上下文上限**：bash 工具单条命令的输出有上限（`bash.max_output_chars`，默认 30000），
   超限会「掐头去尾」截断；PR 描述超 4000 字符也会截断。
   让模型自己用 `git diff --stat`、分段 `git diff <file>` 控制读取量，比预置一堆截断逻辑更稳。
10. **bash 不是沙箱**：`sanitize_env` 只是把带 `TOKEN`/`SECRET`/`PASSWORD`/`KEY` 的环境变量
    剔出去，agent 与本进程同机同用户。真正的隔离边界是 CI runner 的临时容器；
    不要把审查任务跑在有长期凭据的常驻机器上。
11. **克隆 URL 含令牌**：`git clone` 时令牌拼在 URL 里（容器内临时存在），
    所有日志与报错都过 `mask_url()` 脱敏；不要把这套逻辑搬到会持久化 `.git/config` 的环境。
12. **workdir 不能越界**：`config.json` 的 `workdir` 只允许落在仓库目录内，越界会在启动时报错。
13. **配置写错要吵**：未知 tool 名、找不到 `config.json` / `prompt.txt`、JSON 语法错误都会直接失败并回写评论，
    不会静默降级——否则「配了没生效」比跑挂更难查。
14. **失败必回写**：克隆失败、配置错误、模型异常都会转成一条「审查失败」评论，不会静默丢任务。
15. **`work` 模式会写仓库**：它的提示词明确允许 `git add/commit/push`，推的是触发评论所在 PR 的源分支；
    只读评审请走 `review`。`BOT_NAME` 必须配准，否则任何 `@xxx` 都会被当成触发词，
    等于把"谁能驱动机器人改代码"放开了。
16. **评论触发要防自激**：机器人的回复里若带上 `@BOT_NAME work` 之类字样会再次触发自己。
    提示词里已要求不要复述触发词，接入新机器人时记得回归验证一次。

## 二次开发约定

改动 `worker/src/index.ts`、`agent/src` 或 `.github/workflows/ai-review.yml` 时，README 里内嵌的
对应代码块必须同步更新——文档与代码不一致会直接误导部署者。
（`worker/src/app-auth.ts`、`worker/src/env.ts`、`worker/src/mode.ts` 没有内嵌代码块，改动它们只需同步本节与 Secrets 表。）可以用一段脚本自查：


## 本地验证


Agent 本地冒烟（不碰模型也能验证克隆 + 配置 + 工具）：


把 `AI_API_BASE` 指向任意 OpenAI 兼容服务（或本地 mock）就能跑通全链路；
克隆用的 URL 只要在日志里看不到令牌，脱敏就算生效。

端到端验证建议：在测试仓库开一个 PR，确认「Worker 返回 202 → 本仓库（fork）的 `tasks/` 出现任务 JSON →
Actions 跑起来 → 目标 PR 收到评论 → 任务 JSON 被删除且未二次触发」。

身份验证：在 Actions 日志里搜「获取到 App 令牌」与「鉴权身份」，确认评论作者是 App（`xxx[bot]`）；
若显示回退，日志里会带上 App 侧失败原因，据此修凭据即可。
