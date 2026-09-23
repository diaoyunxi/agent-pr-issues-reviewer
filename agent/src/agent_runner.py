"""按配置组装并运行 agent：模型走 OpenAI 兼容的 chat/completions 接口。"""

import asyncio
import os
from pathlib import Path

from agents import Agent, ModelSettings, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from config import AgentConfig
from git_write import WriteContext, build_write_tools
from tools import ShellContext, build_tools


class AgentRunError(RuntimeError):
    """模型调用或 agent 循环失败。"""


def build_model(cfg: AgentConfig):
    """显式走 chat/completions，兼容第三方 OpenAI 兼容网关（DeepSeek/Qwen 等）。"""
    if not cfg.api_key:
        raise AgentRunError("缺少 AI_API_KEY，无法调用模型")
    client = AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.api_base or None)
    return OpenAIChatCompletionsModel(model=cfg.model or os.environ.get("AI_MODEL", "gpt-4o-mini"), openai_client=client)


def build_agent(cfg: AgentConfig, shell: ShellContext, writer: WriteContext | None = None) -> Agent:
    """组装 agent：永远有 bash；给了 writer 才额外带上写权限工具。"""
    tools = list(build_tools(shell))
    if writer is not None:
        tools += build_write_tools(writer)
    return Agent(
        name=cfg.name,
        instructions=cfg.instructions,
        tools=tools,
        model=build_model(cfg),
        model_settings=ModelSettings(temperature=cfg.temperature),
    )


def run_agent(cfg: AgentConfig, shell: ShellContext, prompt: str, workdir: Path, writer: WriteContext | None = None) -> str:
    """跑一轮完整对话，返回模型的最终评审正文。

    进程级也切到仓库目录：bash 工具与写权限工具都用相对路径敲命令，
    不切的话 `git apply` 会落到 CI 的工作目录上。
    """
    agent = build_agent(cfg, shell, writer)
    os.chdir(workdir)
    try:
        # 所有工具共享同一个 context：一个 ctx 里是 shell 设置，另一个带写权限。
        # SDK 只支持单 context，因此这里把 writer 放进 dict 让各工具自己取。
        context = {"shell": shell, "writer": writer}
        result = asyncio.run(Runner.run(agent, prompt, max_turns=cfg.max_turns, context=context))
    except Exception as err:  # noqa: BLE001 - 统一转成可读错误，外层会回写评论
        raise AgentRunError(f"{type(err).__name__}: {err}") from err

    output = result.final_output
    if isinstance(output, str) and output.strip():
        return output.strip()
    return "（模型未返回内容）"
