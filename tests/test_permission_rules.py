"""权限规则引擎测试：覆盖工具名通配符（MCP server 级规则）、连字符回归，以及
mcp_servers.json per-server 权限（最低优先级层、settings 分层覆盖）。"""

from __future__ import annotations

from typing import Any

from src.mgr.permission_mgr import (
    BYPASS_MODE,
    DEFAULT_MODE,
    PermissionManager,
    PermissionRule,
    parse_rule,
)


class _FakeConfigMgr:
    """最小化 config_mgr 桩：仅按 key 返回预置的 settings 段。"""

    def __init__(self, permissions: dict[str, Any]):
        self._settings = {"permissions": permissions}

    def get_user_setting(self, key: str) -> Any:
        """返回指定 settings 段，不存在时返回空 dict。"""
        return self._settings.get(key, {})


class _FakeMcpMgr:
    """最小化 mcp_mgr 桩：返回预置的各 server permissions 块。"""

    def __init__(self, server_perms: dict[str, dict[str, Any]]):
        self._perms = server_perms

    def server_permissions(self) -> dict[str, dict[str, Any]]:
        """返回各 server 的权限块。"""
        return self._perms


def _make_mgr(**permissions: Any) -> PermissionManager:
    """用预置 permissions 段构建 PermissionManager（无真实工具）。"""
    return PermissionManager(config_mgr=_FakeConfigMgr(permissions), tools=[])


def _make_mgr_with_mcp(
    server_perms: dict[str, dict[str, Any]],
    **permissions: Any,
) -> PermissionManager:
    """用预置 settings permissions 段 + mcp_servers per-server 权限构建 PermissionManager。"""
    return PermissionManager(
        config_mgr=_FakeConfigMgr(permissions),
        tools=[],
        mcp_mgr=_FakeMcpMgr(server_perms),
    )


def test_parse_rule_accepts_server_wildcard():
    rule = parse_rule("mcp__mijia__*", "allow")
    assert rule is not None
    assert rule.tool == "mcp__mijia__*"
    assert rule.specifier == "*"


def test_parse_rule_accepts_hyphenated_tool_name():
    # 连字符回归：旧的 \w+ 无法解析带 '-' 的 server/工具名
    rule = parse_rule("mcp__a-b__get_x", "allow")
    assert rule is not None
    assert rule.tool == "mcp__a-b__get_x"


def test_server_wildcard_allow_matches_any_tool():
    mgr = _make_mgr(allow=["mcp__mijia__*"])
    decision, _ = mgr.check("mcp__mijia__get_device_status", {}, DEFAULT_MODE)
    assert decision == "allow"


def test_exact_mcp_rule_still_works():
    mgr = _make_mgr(allow=["mcp__mijia__get_device_status"])
    assert mgr.check("mcp__mijia__get_device_status", {}, DEFAULT_MODE)[0] == "allow"
    # 同 server 的其它工具不被精确规则覆盖 → 默认询问
    assert mgr.check("mcp__mijia__set_device", {}, DEFAULT_MODE)[0] == "ask"


def test_deny_wildcard_beats_allow_wildcard():
    mgr = _make_mgr(allow=["mcp__github__get_*"], deny=["mcp__github__*"])
    decision, _ = mgr.check("mcp__github__get_pull_request", {}, DEFAULT_MODE)
    assert decision == "deny"


def test_prefix_wildcard_within_server():
    mgr = _make_mgr(allow=["mcp__github__get_*"])
    assert mgr.check("mcp__github__get_issue", {}, DEFAULT_MODE)[0] == "allow"
    assert mgr.check("mcp__github__create_issue", {}, DEFAULT_MODE)[0] == "ask"


# ── mcp_servers.json per-server 权限（最低优先级层） ──────────────────────


def test_mcp_server_rules_apply_when_settings_silent():
    # settings 静默时，mcp_servers.json 的 allow/deny/ask 生效
    mgr = _make_mgr_with_mcp({
        "github": {"allow": ["get_*"], "deny": ["delete_*"], "ask": ["create_*"]},
    })
    assert mgr.check("mcp__github__get_issue", {}, DEFAULT_MODE)[0] == "allow"
    assert mgr.check("mcp__github__delete_repo", {}, DEFAULT_MODE)[0] == "deny"
    assert mgr.check("mcp__github__create_issue", {}, DEFAULT_MODE)[0] == "ask"


def test_settings_allow_overrides_mcp_deny():
    # 分层覆盖核心：settings allow 命中 → 覆盖 mcp deny；settings 静默 → 落 mcp deny
    mgr = _make_mgr_with_mcp(
        {"github": {"deny": ["delete_*"]}},
        allow=["mcp__github__delete_tmp"],
    )
    assert mgr.check("mcp__github__delete_tmp", {}, DEFAULT_MODE)[0] == "allow"
    assert mgr.check("mcp__github__delete_repo", {}, DEFAULT_MODE)[0] == "deny"


def test_mcp_deny_and_ask_survive_bypass_mode():
    # BYPASS 模式下 mcp 的 deny/ask 仍生效（置于 bypass 前）
    mgr = _make_mgr_with_mcp({
        "github": {"deny": ["delete_*"], "ask": ["create_*"]},
    })
    assert mgr.check("mcp__github__delete_repo", {}, BYPASS_MODE)[0] == "deny"
    assert mgr.check("mcp__github__create_issue", {}, BYPASS_MODE)[0] == "ask"
    # 未被 mcp 规则命中的工具在 bypass 下仍自动放行
    assert mgr.check("mcp__github__get_issue", {}, BYPASS_MODE)[0] == "auto_allow"


def test_mcp_star_entry_matches_whole_server():
    mgr = _make_mgr_with_mcp({"mijia": {"allow": ["*"]}})
    assert mgr.check("mcp__mijia__get_device_status", {}, DEFAULT_MODE)[0] == "allow"
    assert mgr.check("mcp__mijia__set_device", {}, DEFAULT_MODE)[0] == "allow"


def test_mcp_full_form_entry_is_escape_hatch():
    # 以 mcp__ 开头的条目按完整工具模式原样使用，可跨 server
    mgr = _make_mgr_with_mcp({"github": {"deny": ["mcp__other__danger_*"]}})
    assert mgr.check("mcp__other__danger_run", {}, DEFAULT_MODE)[0] == "deny"


def test_mcp_server_segment_is_sanitized():
    # server 名含 '.' 时，规则前缀须经 _safe_tool_name 清洗以对齐注册名 mcp__git_hub__*
    mgr = _make_mgr_with_mcp({"git.hub": {"allow": ["get_*"]}})
    assert mgr.check("mcp__git_hub__get_x", {}, DEFAULT_MODE)[0] == "allow"


def test_session_allow_overrides_mcp_deny():
    # "信任整个 server" 写入 session_allow（settings 层）→ 覆盖 mcp deny
    mgr = _make_mgr_with_mcp({"github": {"deny": ["delete_*"]}})
    mgr._add_rule(mgr.session_allow, PermissionRule("mcp__github__*", "*", "allow"))
    assert mgr.check("mcp__github__delete_repo", {}, DEFAULT_MODE)[0] == "allow"


def test_mcp_rules_reload_is_idempotent():
    mgr = _make_mgr_with_mcp({"github": {"allow": ["get_*"]}})
    assert mgr.check("mcp__github__get_issue", {}, DEFAULT_MODE)[0] == "allow"
    mgr.reload()
    assert mgr.check("mcp__github__get_issue", {}, DEFAULT_MODE)[0] == "allow"
    # reload 不重复累积规则
    assert len(mgr.mcp_allow_rules["mcp__github__get_*"]) == 1


def test_no_mcp_mgr_leaves_mcp_rules_empty():
    # 向后兼容：未注入 mcp_mgr 时 mcp 规则层为空，行为同纯 settings
    mgr = _make_mgr(allow=["read_file"])
    assert mgr.mcp_deny_rules == {}
    assert mgr.mcp_ask_rules == {}
    assert mgr.mcp_allow_rules == {}
