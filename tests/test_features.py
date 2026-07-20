"""可插拔 feature 机制测试：feature 解析、工具归属漂移防护、按 feature 排除工具。"""

from __future__ import annotations

from pathlib import Path

import src.tools  # noqa: F401  导入触发内置工具注册到 _registry
from src.mgr.features import ALL_FEATURES, resolve_features
from src.mgr.paths import builtin_root
from src.mgr.permission_mgr import DEFAULT_MODE, PLAN_MODE
from src.mgr.role_mgr import AgentManifest, extract_manifest, parse_frontmatter
from src.mgr.tools_mgr import ToolsMgr
from src.tools.decorator import _registry


def _builtin_role_md_path(role_name: str) -> Path:
    """返回内置角色定义文件的路径。

    Args:
        role_name: 内置角色目录名。

    Returns:
        对应角色的 role.md 路径。
    """
    return builtin_root() / "roles" / role_name / "role.md"


def _load_role_manifest(role_name: str) -> AgentManifest:
    """读取内置角色 role.md 并解析为 AgentManifest。

    Args:
        role_name: 内置角色目录名。

    Returns:
        从对应 role.md 解析出的角色清单。
    """
    role_md = _builtin_role_md_path(role_name)
    meta, prompt = parse_frontmatter(role_md.read_text())
    return extract_manifest(
        meta, role_md, prompt=prompt,
        id_field="agent_type", default_id="main", default_description="",
    )


# ── resolve_features ────────────────────────────────────────────────

def test_none_enables_all_features():
    """未声明 features（None）→ 全部启用。"""
    assert resolve_features(None) == set(ALL_FEATURES)


def test_declared_subset_kept():
    """声明子集时仅保留声明的 feature。"""
    assert resolve_features({"subagent"}) == {"subagent"}


def test_empty_set_disables_all():
    """空集 → 全部禁用。"""
    assert resolve_features(set()) == set()


def test_unknown_feature_dropped():
    """未知 feature 名被丢弃，合法名保留。"""
    assert resolve_features({"subagent", "bogus"}) == {"subagent"}


def test_plan_requires_file():
    """plan 依赖 file：缺 file 时丢弃 plan，有 file 时保留。"""
    assert resolve_features({"plan"}) == set()
    assert resolve_features({"plan", "file"}) == {"plan", "file"}


# ── 工具 feature 归属漂移防护 ───────────────────────────────────────

def test_every_tool_feature_is_valid():
    """注册表中每个工具的 feature 为 None 或 ∈ ALL_FEATURES（防拼写漂移）。"""
    for entry in _registry:
        assert entry.feature is None or entry.feature in ALL_FEATURES, (
            f"工具 {entry.name} 的 feature={entry.feature!r} 非法"
        )


def test_expected_tool_feature_mapping():
    """核心工具的 feature 归属符合设计映射。"""
    by_name = {e.name: e.feature for e in _registry}
    assert by_name["task_create"] == "task"
    assert by_name["task_update"] == "task"
    assert by_name["task_list"] == "task"
    assert by_name["task_get"] == "task"
    assert by_name["load_skill"] == "skill"
    assert by_name["task_delegator"] == "subagent"
    assert by_name["read_file"] == "file"
    assert by_name["write_file"] == "file"
    assert by_name["save_memory"] == "memory"
    assert by_name["read_memory"] == "memory"
    assert by_name["enter_plan_mode"] == "plan"
    assert by_name["plan_write_file"] == "plan"
    # 无归属工具恒可用
    assert by_name["shell"] is None


# ── 按 feature 排除工具 ─────────────────────────────────────────────

def test_excluded_tool_names_by_feature():
    """仅启用 subagent 时，其他 feature 的工具全部被排除，无归属工具不排除。"""
    mgr = ToolsMgr()
    excluded = mgr.excluded_tool_names({"subagent"})
    # subagent 工具不排除
    assert "task_delegator" not in excluded
    # 其他 feature 工具被排除
    assert "task_create" in excluded
    assert "load_skill" in excluded
    assert "read_file" in excluded
    assert "save_memory" in excluded
    assert "enter_plan_mode" in excluded
    # 无归属工具不排除
    assert "shell" not in excluded


def test_all_features_excludes_nothing():
    """全部 feature 启用时不排除任何工具。"""
    mgr = ToolsMgr()
    assert mgr.excluded_tool_names(set(ALL_FEATURES)) == set()


# ── 角色 role.md → features 端到端 ──────────────────────────────────

def test_coding_role_configuration_contract():
    """coding 角色保留全工具与全 feature 默认值，并声明其主 agent 配置。"""
    raw_meta, _ = parse_frontmatter(_builtin_role_md_path("coding").read_text())
    assert "agent_type" not in raw_meta
    assert "tools" not in raw_meta
    assert "features" not in raw_meta

    manifest = _load_role_manifest("coding")

    assert manifest.model == "best"
    assert manifest.permission_mode is PLAN_MODE
    assert manifest.enable_thinking is True
    assert manifest.reasoning_effort == "max"
    assert manifest.memory == "project"
    assert manifest.tools is None
    assert manifest.features is None


def test_mijia_role_configuration_contract():
    """mijia 角色仅声明其快速模型、默认权限和 subagent feature。"""
    raw_meta, _ = parse_frontmatter(_builtin_role_md_path("mijia").read_text())
    assert "agent_type" not in raw_meta
    assert "tools" not in raw_meta
    assert "reasoning_effort" not in raw_meta
    assert "memory" not in raw_meta

    manifest = _load_role_manifest("mijia")

    assert manifest.model == "fast"
    assert manifest.permission_mode is DEFAULT_MODE
    assert manifest.enable_thinking is False
    assert manifest.features == {"subagent"}
    assert manifest.tools is None
    assert manifest.reasoning_effort is None
    assert manifest.memory is None


def test_mijia_role_features_and_schema():
    """mijia 声明 features:[subagent]，解析后仅保留 subagent，有效工具集恰好排除其余 feature 工具。"""
    manifest = _load_role_manifest("mijia")
    assert manifest.features == {"subagent"}
    feats = resolve_features(manifest.features)
    assert feats == {"subagent"}
    mgr = ToolsMgr()
    effective = mgr.all_tool_names() - mgr.excluded_tool_names(feats)
    assert "task_delegator" in effective
    for absent in ("task_create", "load_skill", "read_file", "write_file",
                   "save_memory", "enter_plan_mode", "plan_write_file"):
        assert absent not in effective


def test_coding_role_defaults_to_all_features():
    """coding 未声明 features → manifest.features 为 None → 解析为全开，不排除任何工具。"""
    manifest = _load_role_manifest("coding")
    assert manifest.features is None
    feats = resolve_features(manifest.features)
    assert feats == set(ALL_FEATURES)
    mgr = ToolsMgr()
    assert mgr.excluded_tool_names(feats) == set()
