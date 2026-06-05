"""路径解析 — 集中管理全局配置目录、用户工作目录和首次运行初始化。"""

from __future__ import annotations

import os
from pathlib import Path

# 默认配置内容，首次运行时写入 ~/.agent/config.yaml
_DEFAULT_CONFIG = """\
llm_provider:
  deepseek:
    base_url: https://api.deepseek.com
    reasoning_effort: max
    context_limit: 400000
  openai:
    models:
      - gpt-5.5
    base_url: https://api.openai.com/v1
    reasoning_effort: xhigh
    context_limit: 200000
  anthropic:
    base_url: https://api.anthropic.com
    reasoning_effort: high
    context_limit: 200000
  ollama:
    base_url: http://127.0.0.1:8001/v1
    reasoning_effort: high
    preserve_thinking: true
    context_limit: 70000

llm:
  default:
    model: deepseek-v4-flash
    concurrency: 5
    max_retries: 3

tool:
  page_token_rate: 0.03

compact:
  auto_compact_rate: 0.8
  keep_recent_user_turns: 3
  keep_recent_messages_token_rate: 0.25

events:
    level: progress
"""

# 默认 .env 模板，首次运行时写入 ~/.agent/.env
_DEFAULT_ENV = """\
# 在此填写各 LLM provider 的 API Key
# DEEPSEEK_API_KEY=
# OPENAI_API_KEY=
# ANTHROPIC_API_KEY=
"""


def agent_home() -> Path:
    """返回全局配置目录。

    优先使用环境变量 $AGENT_HOME，否则默认 ~/.agent/。

    Returns:
        全局配置目录的 Path 对象。
    """
    return Path(os.environ.get("AGENT_HOME", Path.home() / ".agent"))


def workdir(override: str | None = None) -> Path:
    """返回用户工作目录。

    优先使用 override 参数，其次环境变量 $AGENT_WORKDIR，最后 Path.cwd()。

    Args:
        override: 命令行传入的工作目录覆盖值。

    Returns:
        用户工作目录的 Path 对象。
    """
    if override:
        return Path(override).resolve()
    env = os.environ.get("AGENT_WORKDIR")
    if env:
        return Path(env).resolve()
    return Path.cwd()


def ensure_global_config(home: Path) -> None:
    """首次运行时创建全局配置目录并写入默认文件。

    如果 home 目录或 config.yaml 不存在则自动创建；已存在则跳过。

    Args:
        home: 全局配置目录路径。
    """
    home.mkdir(parents=True, exist_ok=True)
    config_file = home / "config.yaml"
    if not config_file.exists():
        config_file.write_text(_DEFAULT_CONFIG)
    env_file = home / ".env"
    if not env_file.exists():
        env_file.write_text(_DEFAULT_ENV)
