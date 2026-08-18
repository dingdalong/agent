"""子 agent manifest 的 model 字段校验与委派透传测试。

子 agent 的 `model:` 只允许 `default`/`fast`（含 Claude Code 兼容别名）或完整模型
ID；空值使用 default 槽位，其他非法值在 manifest 加载期直接报错。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.agent import Agent
from src.llm.errors import LLMConfigurationError
from src.mgr.llm_mgr import MODEL_ALIASES
from src.mgr.role_mgr import AgentManifest
from src.mgr.subagent_mgr import SubAgentMgr

_KNOWN_MODEL = "claude-opus-5"


class _LLMMgrStub:
    """只提供已加载模型列表的最小 LLMMgr 替身。"""

    def __init__(self, models: tuple[str, ...] = (_KNOWN_MODEL,)) -> None:
        """保存已加载模型集合。

        Args:
            models: 视为已加载的完整模型 ID。

        Returns:
            None。
        """
        self._models = list(models)

    def list_models(self) -> list[str]:
        """返回已加载的完整模型 ID 列表。

        Returns:
            模型 ID 列表。
        """
        return list(self._models)


def _write_subagent(workdir: Path, name: str, model: str | None = None) -> Path:
    """在项目层 agents 目录写入一个子 agent 定义文件。

    Args:
        workdir: 用户工作目录，定义文件写入其下 .agent/agents/。
        name: 子 agent 的 agent_type，同时作为文件名。
        model: frontmatter 的 model 值；None 表示不写该字段。

    Returns:
        写入的定义文件路径。
    """
    agents_dir = workdir / ".agent" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / f"{name}.md"
    lines = ["---", f"agent_type: {name}", "description: 测试子 agent"]
    if model is not None:
        lines.append(f"model: {model}")
    lines += ["---", "子 agent 提示词。", ""]
    path.write_text("\n".join(lines))
    return path

def _write_subagent_with_raw_model(
    workdir: Path,
    name: str,
    raw_model: object,
) -> Path:
    """写入显式声明原始 model 值的子 agent 定义文件。

    Args:
        workdir: 用户工作目录，定义文件写入其下 .agent/agents/。
        name: 子 agent 的 agent_type，同时作为文件名。
        raw_model: 交给 YAML 序列化的原始 model 值。

    Returns:
        写入的定义文件路径。
    """
    agents_dir = workdir / ".agent" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / f"{name}.md"
    meta = {
        "agent_type": name,
        "description": "测试子 agent",
        "model": raw_model,
    }
    path.write_text(
        "---\n"
        + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
        + "---\n子 agent 提示词。\n"
    )
    return path



def _deps(llm_mgr: _LLMMgrStub | None = None) -> SimpleNamespace:
    """构造 SubAgentMgr 加载期所需的最小依赖。

    Args:
        llm_mgr: 模型管理器替身；None 表示依赖里没有可用的 llm_mgr。

    Returns:
        仅含 role_mgr 与 llm_mgr 的依赖对象。
    """
    return SimpleNamespace(role_mgr=None, llm_mgr=llm_mgr)


@pytest.mark.parametrize("bad_model", ["best", "inherit", "gpt-9", "claude-opus-6"])
def test_illegal_manifest_model_reports_file_path(tmp_path: Path, bad_model: str) -> None:
    """非法 model 值在加载期报错，且消息含文件路径、非法值与允许取值。

    Args:
        tmp_path: 测试工作目录。
        bad_model: 写入 manifest 的非法 model 值。

    Returns:
        None。
    """
    path = _write_subagent(tmp_path, "worker", model=bad_model)

    with pytest.raises(LLMConfigurationError) as exc_info:
        SubAgentMgr(tmp_path, _deps(_LLMMgrStub()))

    message = exc_info.value.info.message
    assert str(path) in message
    assert bad_model in message
    assert "default" in message
    assert "fast" in message
    assert "opus" in message
    assert "sonnet" in message
    assert "haiku" in message
    assert "完整模型 ID" in message


@pytest.mark.parametrize(
    "raw_model",
    [None, "", "   "],
    ids=["null", "empty", "whitespace"],
)
def test_explicit_empty_raw_model_uses_default_slot(
    tmp_path: Path,
    raw_model: object,
) -> None:
    """null、空串和纯空白应统一规范为未设置。"""
    _write_subagent_with_raw_model(tmp_path, "worker", raw_model)

    mgr = SubAgentMgr(tmp_path, _deps(_LLMMgrStub()))

    assert mgr._documents["worker"].model is None


@pytest.mark.parametrize(
    "raw_model",
    [False, True, 0, 123, [], {}],
    ids=["false", "true", "zero", "number", "list", "dict"],
)
def test_explicit_non_string_raw_model_is_rejected(
    tmp_path: Path,
    raw_model: object,
) -> None:
    """非字符串、非 null 的 model 必须在加载边界报错。"""
    path = _write_subagent_with_raw_model(tmp_path, "worker", raw_model)

    with pytest.raises(LLMConfigurationError) as exc_info:
        SubAgentMgr(tmp_path, _deps(_LLMMgrStub()))

    message = exc_info.value.info.message
    assert str(path) in message
    assert repr(raw_model) in message
    assert "default" in message
    assert "fast" in message
    assert "opus" in message
    assert "sonnet" in message
    assert "haiku" in message
    assert "完整模型 ID" in message


@pytest.mark.parametrize("alias", sorted(MODEL_ALIASES))
def test_model_aliases_are_accepted(tmp_path: Path, alias: str) -> None:
    """槽位别名与 Claude Code 兼容别名原样保留，不在加载期报错。

    Args:
        tmp_path: 测试工作目录。
        alias: 合法别名。

    Returns:
        None。
    """
    _write_subagent_with_raw_model(tmp_path, "worker", f"  {alias}  ")

    mgr = SubAgentMgr(tmp_path, _deps(_LLMMgrStub()))

    assert mgr._documents["worker"].model == alias


def test_full_model_id_is_accepted(tmp_path: Path) -> None:
    """已加载的完整模型 ID 合法。

    Args:
        tmp_path: 测试工作目录。

    Returns:
        None。
    """
    _write_subagent(tmp_path, "worker", model=_KNOWN_MODEL)

    mgr = SubAgentMgr(tmp_path, _deps(_LLMMgrStub()))

    assert mgr._documents["worker"].model == _KNOWN_MODEL


def test_missing_model_is_accepted(tmp_path: Path) -> None:
    """未声明 model 时解析为 None（委派时走 default 槽位），不报错。

    Args:
        tmp_path: 测试工作目录。

    Returns:
        None。
    """
    _write_subagent(tmp_path, "worker")

    mgr = SubAgentMgr(tmp_path, _deps(_LLMMgrStub()))

    assert mgr._documents["worker"].model is None


def test_unavailable_llm_mgr_skips_model_id_check(tmp_path: Path) -> None:
    """deps 无 llm_mgr 时无法获知可用模型集，完整模型 ID 一律放行。

    Args:
        tmp_path: 测试工作目录。

    Returns:
        None。
    """
    _write_subagent(tmp_path, "worker", model="some-unknown-model")

    mgr = SubAgentMgr(tmp_path, _deps(None))

    assert mgr._documents["worker"].model == "some-unknown-model"


def test_delegation_passes_manifest_model_verbatim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """委派时 manifest.model 原样透传，不再有 inherit 继承父 agent 的分支。

    Args:
        tmp_path: 测试工作目录。
        monkeypatch: pytest 补丁夹具。

    Returns:
        None。
    """
    captured: dict[str, object] = {}

    class _Child:
        """记录委派结果的最小子 agent 替身。"""

        uuid = "child"
        history: list[dict[str, str]] = []

        async def run(self, _prompt: str) -> SimpleNamespace:
            """返回固定成功结果。

            Args:
                _prompt: 子任务提示词。

            Returns:
                成功结果。
            """
            return SimpleNamespace(final_text="ok", llm_error=None)

    def from_manifest(cls, manifest, deps, **overrides):
        """记录构造覆盖字段并返回子 agent 替身。

        Args:
            cls: Agent 类。
            manifest: 子 agent manifest。
            deps: Agent 依赖。
            **overrides: 构造覆盖字段。

        Returns:
            子 agent 替身。
        """
        del cls, manifest, deps
        captured.update(overrides)
        return _Child()

    monkeypatch.setattr(Agent, "from_manifest", classmethod(from_manifest))
    mgr = object.__new__(SubAgentMgr)
    mgr.workdir = tmp_path
    mgr.global_dir = None
    mgr._documents = {
        "worker": AgentManifest(
            agent_type="worker",
            description="test",
            path=tmp_path / "worker.md",
            model="inherit",
        )
    }
    mgr.deps = SimpleNamespace(
        tools_mgr=SimpleNamespace(resolve_subagent_tools=lambda tools: set(tools or ())),
        hooks_mgr=None,
        event_bus=None,
    )
    parent = SimpleNamespace(
        plan_active=False,
        llm=SimpleNamespace(model="parent-real-model"),
        enable_thinking=True,
        reasoning_effort=None,
        features=set(),
        _task_mgr=None,
    )

    result = asyncio.run(mgr.task_delegator("worker", "work", parent_agent=parent))

    assert result == "ok"
    assert captured["model"] == "inherit"
