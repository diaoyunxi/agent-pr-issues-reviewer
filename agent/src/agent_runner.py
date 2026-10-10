"""按配置组装并运行 agent：模型走 OpenAI 兼容的 chat/completions 接口。"""

import asyncio
import os
from pathlib import Path

from agents import Agent, ModelSettings, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from config import AgentConfig
from openai import AsyncOpenAI
from tools import ShellContext, build_tools


class AgentRunError(RuntimeError):
    """模型调用或 agent 循环失败。"""


def build_model(cfg: AgentConfig):
    """显式走 chat/completions，兼容第三方 OpenAI 兼容网关（DeepSeek/Qwen 等）。"""
    if not cfg.api_key:
        raise AgentRunError("缺少 AI_API_KEY，无法调用模型")
    client = AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.api_base or None)
    return OpenAIChatCompletionsModel(model=cfg.model or os.environ.get("AI_MODEL", "gpt-4o-mini"), openai_client=client)


def build_agent(cfg: AgentConfig, shell: ShellContext) -> Agent:
    return Agent(
        name=cfg.name,
        instructions=cfg.instructions,
        tools=build_tools(shell),
        model=build_model(cfg),
        model_settings=ModelSettings(temperature=cfg.temperature),
    )


def run_agent(cfg: AgentConfig, shell: ShellContext, prompt: str, workdir: Path) -> str:
    """跑一轮完整对话，返回模型的最终评审正文。"""
    agent = build_agent(cfg, shell)
    # 工具用相对路径敲命令，所以进程级也切到仓库目录，避免两边目录不一致
    os.chdir(workdir)
    try:
        result = asyncio.run(Runner.run(agent, prompt, max_turns=cfg.max_turns, context=shell))
    except Exception as err:  # noqa: BLE001 - 统一转成可读错误，外层会回写评论
        raise AgentRunError(f"{type(err).__name__}: {err}") from err

    output = result.final_output
    if isinstance(output, str) and output.strip():
        return output.strip()
    return "（模型未返回内容）"
