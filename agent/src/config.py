"""agent 配置文件加载：把 agents/config.json + prompt.txt 变成可运行的设置。

一次运行 = 一个 agent 角色。默认读取 `AGENT_CONFIG`（缺省 `agents/config.json`），
文件里每个 key 是一个 agent 配置，`AGENT_NAME`（缺省第一个）决定这次跑哪一个。
配置是纯 JSON + 纯文本，改完不需要动代码，也不需要重新生成任何脚本。
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = "agents/config.json"
DEFAULT_EXAMPLE_PATH = "agents/config.example.json"
DEFAULT_PROMPT_PATH = "agents/prompt.txt"
DEFAULT_TOOL = "bash"
# 两种执行模式各有默认提示词文件，用户放哪个都行：优先按模式取名，找不到再回退 prompt.txt
DEFAULT_PROMPT_BY_MODE = {"review": "prompt-review.txt", "work": "prompt-work.txt"}

# 未知 tool 名要在启动时直接报错，避免"配了但没生效"这种静默失败
KNOWN_TOOLS = {"bash"}


class ConfigError(RuntimeError):
    """配置缺失或非法，需要在 Actions 日志里一眼看到原因。"""


@dataclass
class AgentConfig:
    """一个 agent 角色的完整设置。"""

    name: str
    instructions: str
    prompt_file: str
    # 本次运行的模式（review / work），进模型首条消息，也给 work 模式决定要不要放开写权限
    mode: str = "review"
    model: str = ""
    temperature: float = 0.2
    max_turns: int = 20
    workdir: str = "."
    tools: list[str] = field(default_factory=lambda: [DEFAULT_TOOL])
    bash_timeout: float = 120.0
    bash_max_output_chars: int = 30_000
    # 运行时注入（不进配置文件，避免把密钥写进仓库）
    api_base: str = ""
    api_key: str = ""

    def resolve_workdir(self, repo_root: Path) -> Path:
        """把配置里的相对路径解析到仓库根之内，越界直接报错。"""
        candidate = (repo_root / self.workdir).resolve()
        if candidate != repo_root and repo_root not in candidate.parents:
            raise ConfigError(f"workdir 越界：{self.workdir} 不在仓库目录 {repo_root} 内")
        return candidate


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"找不到 agent 配置文件：{path}") from None
    except json.JSONDecodeError as err:
        raise ConfigError(f"agent 配置文件不是合法 JSON：{path}（{err}）") from None


def _pick_agent(raw: dict, agent_name: str) -> tuple[str, dict]:
    """从配置里挑出本次要跑的 agent，并兼容"直接写单个 agent"的扁平写法。"""
    if not isinstance(raw, dict):
        raise ConfigError("agent 配置顶层必须是 JSON 对象")

    # 扁平写法：顶层直接就是 name/prompt_file/... 时，视为只有一个 agent
    if "prompt_file" in raw or "tools" in raw or "model" in raw:
        key = raw.get("name") or agent_name or "reviewer"
        return str(key), raw

    if not raw:
        raise ConfigError("agent 配置为空")

    if agent_name:
        if agent_name not in raw:
            raise ConfigError(f"配置里没有名为 {agent_name} 的 agent，可选：{', '.join(sorted(raw))}")
        return agent_name, raw[agent_name]

    first = sorted(raw)[0]
    return first, raw[first]


def _resolve_prompt(prompt_file: str, config_dir: Path, repo_root: Path, required: bool = True) -> Path | None:
    """prompt 路径支持相对配置文件目录、相对仓库根、或绝对路径。"""
    path = Path(prompt_file)
    candidates = [path] if path.is_absolute() else [config_dir / path, repo_root / path]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    if not required:
        return None
    raise ConfigError(f"找不到系统提示词文件：{prompt_file}（已尝试 {', '.join(str(c) for c in candidates)}）")


def _resolve_mode_prompt(mode: str, entry: dict, config_dir: Path, repo_root: Path) -> Path:
    """按模式选提示词：`prompt_file_review` / `prompt_file_work` > 按模式约定名 > prompt_file。

    用户只配了 `prompt.txt` 时两种模式共用它，老仓库不用改就能继续跑。
    """
    explicit = str(entry.get(f"prompt_file_{mode}") or "")
    if explicit:
        found = _resolve_prompt(explicit, config_dir, repo_root)
        assert found is not None
        return found

    for name in (DEFAULT_PROMPT_BY_MODE.get(mode, ""),):
        if not name:
            break
        found = _resolve_prompt(name, config_dir, repo_root, required=False)
        if found:
            return found

    return _resolve_prompt(str(entry.get("prompt_file") or DEFAULT_PROMPT_PATH), config_dir, repo_root)  # type: ignore[return-value]


def load_agent_config(
    repo_root: Path,
    config_path: str = "",
    agent_name: str = "",
    mode: str = "",
) -> AgentConfig:
    """从磁盘读配置；缺配置时给出可照抄的示例路径，而不是含糊的报错。

    mode 决定用哪份提示词（review / work），也决定 work 模式是否放开写权限；
    缺省从环境变量 MODE 读，再缺省按 review。
    """
    mode = (mode or os.environ.get("MODE", "") or "review").strip().lower()
    if mode not in DEFAULT_PROMPT_BY_MODE:
        raise ConfigError(f"不支持的模式：{mode}（可用：review / work）")

    raw_path = config_path or os.environ.get("AGENT_CONFIG", DEFAULT_CONFIG_PATH)
    path = Path(raw_path)
    if not path.is_absolute():
        path = repo_root / path

    if not path.is_file():
        example = path.parent / Path(DEFAULT_EXAMPLE_PATH).name
        hint = f"，可从 {example} 复制一份改名" if example.is_file() else ""
        raise ConfigError(f"找不到 agent 配置文件：{path}{hint}")

    raw = _read_json(path)
    key, entry = _pick_agent(raw, agent_name or os.environ.get("AGENT_NAME", ""))
    if not isinstance(entry, dict):
        raise ConfigError(f"agent {key} 的配置必须是 JSON 对象")

    prompt_path = _resolve_mode_prompt(mode, entry, path.parent, repo_root)
    instructions = prompt_path.read_text(encoding="utf-8")

    tools = entry.get("tools") or [DEFAULT_TOOL]
    if isinstance(tools, str):
        tools = [tools]
    if not isinstance(tools, list) or not tools:
        raise ConfigError("tools 必须是非空数组，例如 [\"bash\"]")
    unknown = [t for t in tools if t not in KNOWN_TOOLS]
    if unknown:
        raise ConfigError(f"不支持的 tool：{', '.join(map(str, unknown))}（可用：{', '.join(sorted(KNOWN_TOOLS))}）")

    bash_cfg = entry.get("bash") or {}
    if not isinstance(bash_cfg, dict):
        raise ConfigError("bash 配置必须是 JSON 对象")

    config = AgentConfig(
        name=str(entry.get("name") or key),
        instructions=instructions,
        prompt_file=str(prompt_path),
        mode=mode,
        model=str(entry.get("model") or ""),
        temperature=float(entry.get("temperature", 0.2)),
        max_turns=int(entry.get("max_turns", 20)),
        workdir=str(entry.get("workdir") or "."),
        tools=[str(t) for t in tools],
        bash_timeout=float(bash_cfg.get("timeout_seconds", 120)),
        bash_max_output_chars=int(bash_cfg.get("max_output_chars", 30_000)),
    )
    # workdir 在加载阶段就校验一次：越界配置越早失败越好查
    config.resolve_workdir(repo_root)
    return config
