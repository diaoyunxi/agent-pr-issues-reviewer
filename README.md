# Agent PR / Issue Reviewer

基于 Cloudflare Worker + GitHub Actions 的自动化 AI 代码助手：既能审查代码，也能按评论里的自然语言要求直接改代码。

> 本文件为文档目录；全部说明文档统一存放于 `doc/`。

## 文档目录

- [00 · 概览](doc/00-overview.md)　`本文档总述`
- [01 · 数据流向](doc/01-数据流向.md)
- [02 · 目录结构](doc/02-目录结构.md)
- [03 · 部署步骤](doc/03-部署步骤.md)
- [04 · 模块一：Cloudflare Worker（TypeScript）](doc/04-模块一-Cloudflare-Worker-TypeScript.md)
- [05 · 模块二：本仓库（Fork）的 GitHub Actions](doc/05-模块二-本仓库-Fork-的-GitHub-Actions.md)
- [06 · 模块三：AI Agent（Python，基于 OpenAI Agents SDK）](doc/06-模块三-AI-Agent-Python-基于-OpenAI-Agents-SDK.md)
- [07 · 安全与踩坑清单](doc/07-安全与踩坑清单.md)
- [08 · 二次开发约定](doc/08-二次开发约定.md)
- [09 · 本地验证](doc/09-本地验证.md)
- [apps-and-webhooks](doc/apps-and-webhooks.md)
- [变更日志](doc/changelog.md)
- [贡献指南](doc/contributing.md)
- [安全策略](doc/security.md)
- [tokens](doc/tokens.md)

> `agent/agents/`、`agent/tasks/`、`agents/` 下的 README 是目录占位，保留原位。

## 保留原位的文档与资源

- [agent/agents/README.md](agent/agents/README.md)
- [agent/tasks/README.md](agent/tasks/README.md)
- [agents/README.md](agents/README.md)

---

> 文档与代码一致性按 `doc/` 各分册核对；实现以仓库源码为准。
