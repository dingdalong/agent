"""真实角色装配、委派与工具执行链中的技能复用回归。"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

import src.tools
from src.agent.agent import Agent, AgentDeps
from src.agent.states import RunContext
from src.llm.base import LLMResponse
from src.mode import RunMode
from src.mgr.data_guard import DataGuard
from src.mgr.features import resolve_features
from src.mgr.hooks_mgr import HookRunResult
from src.mgr.permission_mgr import JudgeVerdict, PermissionManager
from src.mgr.role_mgr import RoleMgr
from src.mgr.skill_mgr import SkillMgr
from src.mgr.subagent_mgr import SubAgentMgr
from src.tools.decorator import ToolEntry
from src.tools.policy import ToolOrigin


class _Config:
    """仅提供角色发现和 Agent 构造需要的配置。"""

    project_trusted = False

    def __init__(self, role_name: str) -> None:
        self.role_name = role_name

    def get_config(self, key: str):
        if key == "role.default":
            return self.role_name
        if key == "compact":
            return {"auto_compact_rate": 0.8}
        raise KeyError(key)

    def get_config_parts(self, parts: tuple[str, ...]):
        raise KeyError(parts)


def _main(tmp_path: Path, role_name: str) -> Agent:
    """用真实 Manager 和权限服务构造无网络的主 agent。"""
    from src.mgr.tools_mgr import ToolsMgr

    config = _Config(role_name)
    role = RoleMgr(config_mgr=config, workdir=tmp_path, global_dir=None)
    guard = DataGuard()
    llm = SimpleNamespace(
        provider_name="openai", model="test", context_limit=100000, reasoning_effort="high",
        page_token_budget=100000,
        estimate_tokens=lambda messages: sum(len(message["content"]) for message in messages),
    )
    deps = AgentDeps(
        config_mgr=config,
        role_mgr=role,
        llm_mgr=SimpleNamespace(get=lambda model: llm, list_models=lambda: ["test"]),
        tools_mgr=ToolsMgr(),
        workdir=tmp_path,
        data_guard=guard,
        permission_mgr=PermissionManager(
            workdir=str(tmp_path), judge_client=None, confirm=None, data_guard=guard,
        ),
    )
    return Agent.from_manifest(role.manifest, deps, mode=RunMode.EXECUTE)


def _child(parent: Agent, agent_type: str) -> Agent:
    """按调度器的 manifest、feature 与模式规则构造独立实例。"""
    manifest = parent._subagent_mgr._documents[agent_type]
    return Agent.from_manifest(
        manifest,
        parent.deps,
        is_subagent=True,
        tools=manifest.tools,
        features=manifest.features if manifest.features is not None else parent.features,
        mode=parent.mode,
    )


def _call(agent: Agent, name: str, arguments: dict) -> str:
    """经 Agent 工具入口执行，保留权限和 feature 检查。"""
    context = RunContext(
        messages=agent.history,
        response=LLMResponse(content="", tool_calls={0: {
            "id": "call", "name": name, "arguments": json.dumps(arguments),
        }}),
    )
    asyncio.run(agent._on_execute_tools(context))
    return agent.history[-1]["content"]


def test_skill_loading_deduplicates_per_turn_and_resets_with_new_context(tmp_path: Path) -> None:
    """同一用户轮次只注入一次正文；新轮次可再次加载。"""
    parent = _main(tmp_path, "coding")
    first_context = RunContext(messages=parent.history)

    async def load(context: RunContext) -> str:
        result = await parent.deps.tools_mgr.execute(
            "load_skill", {"name": "builtin:debugging"},
            deps=parent.deps, agent=parent, run_context=context,
        )
        return str(result)

    first = asyncio.run(load(first_context))
    duplicate = asyncio.run(load(first_context))
    next_turn = asyncio.run(load(RunContext(messages=parent.history)))

    assert '<skill name="builtin:debugging"' in first
    assert "无需重复加载" in duplicate
    assert '<skill name="builtin:debugging"' in next_turn


def test_blocked_skill_result_does_not_consume_turn_deduplication(tmp_path: Path) -> None:
    """正文未进入历史时，本轮仍可重新加载该技能。"""
    parent = _main(tmp_path, "coding")
    context = RunContext(messages=parent.history)

    class Hooks:
        async def run_event(self, event, tool, payload, **kwargs):
            if event == "PostToolUse":
                return HookRunResult(blocked=True, block_reason="blocked")
            return HookRunResult()

    parent.deps.hooks_mgr = Hooks()
    result = asyncio.run(parent.deps.tools_mgr.execute(
        "load_skill", {"name": "builtin:debugging"},
        deps=parent.deps, agent=parent, run_context=context,
    ))

    assert result.error_code == "hook_blocked"
    assert context.loaded_skills == set()


@pytest.mark.parametrize(("role_name", "exclusive", "skills"), [
    ("coding", {"coder", "review"}, {"debugging"}),
    ("mijia", set(), {"control-devices", "diagnose-home", "manage-scenes"}),
    ("onboard", {"repository-map", "evidence-analyst", "evidence-reviewer"}, {
        "onboard-analyze-module", "onboard-resolve-relations", "onboard-classify-evidence",
        "onboard-verify-evidence", "onboard-write-manual", "onboard-review-manual",
    }),
])
def test_roles_resolve_shared_agents_and_loadable_skills(
    tmp_path: Path, role_name: str, exclusive: set[str], skills: set[str],
) -> None:
    """完整扫描包含 common 回退，所有技能正文和引用资源均可加载。"""
    parent = _main(tmp_path, role_name)
    assert set(parent._subagent_mgr._documents) == exclusive | {
        "explore", "shell", "general-purpose",
    }
    expected = {f"builtin:{name}" for name in skills}
    assert set(parent._skill_mgr._documents) == expected
    for name in expected:
        text = _call(parent, "load_skill", {"name": name})
        assert f'<skill name="{name}"' in text
        document = parent._skill_mgr._documents[name]
        for reference in re.findall(r"\]\((references/[^)]+)\)", text):
            assert (document.manifest.path.parent / reference).is_file()


@pytest.mark.parametrize("is_subagent", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_skill_directory_requires_feature(
    tmp_path: Path, is_subagent: bool, enabled: bool,
) -> None:
    """技能目录由 feature/Manager 决定，不受统一 schema 或 manifest 列表影响。"""
    parent = _main(tmp_path, "coding")
    agent = _child(parent, "explore") if is_subagent else parent
    if not enabled:
        agent.features = resolve_features({"file"})
        agent._skill_mgr = None
    prompt = "\n\n".join(
        message["content"]
        for message in agent._prompt_mgr.build_initial_context_messages()
    )
    assert ("# 可用技能" in prompt) == enabled
    if not enabled:
        assert "<skill " not in _call(agent, "load_skill", {"name": "builtin:debugging"})


def test_loading_skill_preserves_readonly_and_execution_permissions(tmp_path: Path) -> None:
    """技能作为工具结果进入历史，不能扩大模式或调用方权限。"""
    parent = _main(tmp_path, "coding")
    parent.mode = RunMode.PLAN
    child = _child(parent, "explore")
    before_tools = set(child.tools)
    system = child._prompt_mgr.build()
    external_context = child._prompt_mgr.build_initial_context_messages()
    assert "# 可用技能" in external_context[0]["content"]
    assert "# 可用技能" not in system[0]["content"]
    assert '<skill name="builtin:debugging"' in _call(
        child, "load_skill", {"name": "builtin:debugging"},
    )
    assert child.tools == before_tools
    assert child.mode is RunMode.PLAN
    assert child._prompt_mgr.build() == system
    assert parent.history == []
    forbidden_calls = {
        "apply_patch": {"patch": "*** Begin Patch\n*** End Patch"},
        "submit_plan": {"title": "计划", "content": "内容"},
        "task_delegator": {
            "description": "任务",
            "agent_type": "explore",
            "prompt": "任务",
        },
    }
    for tool_name, arguments in forbidden_calls.items():
        assert "tool_unavailable" in _call(child, tool_name, arguments)
    assert "不存在的技能" in _call(child, "load_skill", {"name": "builtin:missing"})


def test_skill_does_not_allow_coder_writes_in_plan(tmp_path: Path) -> None:
    """有写工具的子 agent 加载技能后仍被 Plan 授权拒绝。"""
    parent = _main(tmp_path, "coding")
    parent.mode = RunMode.PLAN
    child = _child(parent, "coder")
    _call(child, "load_skill", {"name": "builtin:debugging"})
    target = tmp_path / "must-not-exist.txt"
    result = _call(child, "apply_patch", {"patch": "*** Begin Patch\n*** Add File: must-not-exist.txt\n+mutation\n*** End Patch"})
    assert "tool_unavailable" in result
    assert not target.exists()


def test_real_delegation_loads_skill_in_fresh_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只替代 LLM 运行，委派、构造、技能工具和返回使用真实链路。"""
    parent = _main(tmp_path, "onboard")
    parent.history.append({"role": "user", "content": "parent-only"})
    children = []

    async def run_child(agent: Agent, prompt: str):
        children.append(agent)
        assert agent.history == []
        assert "parent-only" not in prompt
        context = RunContext(messages=agent.history, response=LLMResponse(
            content="", tool_calls={0: {
                "id": "load", "name": "load_skill",
                "arguments": json.dumps({"name": "builtin:onboard-verify-evidence"}),
            }},
        ))
        await agent._on_execute_tools(context)
        return SimpleNamespace(final_text=agent.history[-1]["content"], llm_error=None)

    monkeypatch.setattr(Agent, "run", run_child)
    for _ in range(2):
        result = asyncio.run(parent._subagent_mgr.task_delegator(
            "evidence-reviewer", "加载 builtin:onboard-verify-evidence 核实指定报告",
            parent_agent=parent, shared_context="none",
        ))
        assert '<skill name="builtin:onboard-verify-evidence"' in result
    assert children[0].uuid != children[1].uuid
    assert children[0].history is not children[1].history
    assert parent.history == [{"role": "user", "content": "parent-only"}]


def test_project_override_and_lazy_dimension_references(tmp_path: Path) -> None:
    """保留项目覆盖规则，归类入口不会加载其他维度的完整方法。"""
    parent = _main(tmp_path, "onboard")
    project_agent = tmp_path / ".agent" / "agents" / "explore.md"
    project_agent.parent.mkdir(parents=True)
    project_agent.write_text("---\nagent_type: explore\ndescription: project-explore\n---\n")
    manager = SubAgentMgr(tmp_path, parent.deps)
    assert manager._documents["explore"].description == "project-explore"
    classifier = parent._skill_mgr.load_full_text("builtin:onboard-classify-evidence")
    assert "references/change-patterns.md" in classifier
    assert "两个独立完整案例" not in classifier
    user_skill = tmp_path / ".agent" / "skills" / "sample" / "SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text("---\nname: sample\ndescription: sample\n---\nuser-only-body\n")
    manager = SkillMgr(tmp_path, role_mgr=parent.deps.role_mgr)
    assert "user-only-body" not in manager.describe()
    assert "user-only-body" in manager.load_full_text("user:sample")


def test_mijia_general_executor_uses_registered_mcp_after_skill_loading(tmp_path: Path) -> None:
    """领域技能不需要静态 MCP 名称，通用执行器仍经授权调用真实工具入口。"""
    parent = _main(tmp_path, "mijia")
    devices = [{"id": "ceiling", "room": "客厅", "name": "吸顶灯"},
               {"id": "strip", "room": "客厅", "name": "灯带"}]
    queries = []

    class QueryArgs(BaseModel):
        pass

    def query_devices():
        queries.append("devices")
        return json.dumps(devices, ensure_ascii=False)

    tool_name = "mcp__home-test__devices"
    parent.deps.tools_mgr.register(ToolEntry(
        name=tool_name, func=query_devices, model=QueryArgs,
        parameters_schema=QueryArgs.model_json_schema(), description="查询测试设备",
        origin=ToolOrigin("mcp", "home-test"),
    ))
    judge = AsyncMock(return_value=JudgeVerdict("allow", "只读测试查询"))
    parent.deps.permission_mgr.judge_client = SimpleNamespace(judge=judge)
    child = _child(parent, "general-purpose")
    child.history.append({"role": "user", "content": "查询客厅设备，返回候选，不执行控制"})
    assert "<skill " in _call(child, "load_skill", {"name": "builtin:control-devices"})
    assert tool_name in {schema["function"]["name"] for schema in child.deps.tools_mgr.schemas()}
    assert json.loads(_call(child, tool_name, {}).split("\n", 1)[1]) == devices
    assert queries == ["devices"]
    assert judge.await_count == 1
    assert "write_file" not in {schema["function"]["name"] for schema in child.deps.tools_mgr.schemas()}
