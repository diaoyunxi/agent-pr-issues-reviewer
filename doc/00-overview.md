# 概览

**项目**：Agent PR / Issue Reviewer

基于 Cloudflare Worker + GitHub Actions 的自动化 AI 代码助手：既能审查代码，也能按评论里的自然语言要求直接改代码。

## 原文前言

# Agent PR / Issue Reviewer

基于 **Cloudflare Worker + GitHub Actions** 的自动化 AI 代码助手：既能**审查代码**，也能按评论里的自然语言要求**直接改代码**。

控制面（你 **fork 的仓库**）与数据面（目标业务仓库）分离：Worker 只负责「登记任务」，Actions 只负责「跑 Agent + 回写评论」。

Agent 侧是一个**只有 bash 工具**的 OpenAI Agents SDK agent：它把上游仓库 `git clone` 到 `/tmp` 的子目录，然后在仓库里自己读代码；行为由目标仓库里的 `src/config.json` 与两份提示词（`prompt-review.txt` / `prompt-work.txt`）决定，改配置不用改代码。

触发方式有两种 **mode**：

- **review（评审）**：PR 开启时默认走这条；也可以评论 `@你的App review` 手动触发。
- **work（干活）**：评论 `@你的App work 把 README 里的链接改成 xxx`，模型按描述改代码并提交。

规则细节见「[模块一 → 触发规则](#触发规则哪些事件会跑跑什么模式)」。
