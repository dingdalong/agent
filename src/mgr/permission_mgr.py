from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Iterable, Literal
from pydantic import ValidationError
from src.tools.decorator import ArgPattern, PermissionChecker, ToolEntry, args_match

logger = logging.getLogger(__name__)

RulePermission = Literal["allow", "deny"]
ArgPattern = str | list[str]

RULE_PERMISSIONS = {"allow", "deny"}

@dataclass(frozen=True)
class PermissionEntry:
    tool_name: str
    permission: RulePermission
    args: dict[str, ArgPattern] | None
    checker: PermissionChecker | None = None

    def check(self, values: dict[str, Any]) -> bool:
        if self.checker is None and self.args is None:
            return True
        if self.checker is not None and self.checker(values):
            return True
        if self.args is None:
            return False
        return args_match(self.args, values)

PermissionDecision = tuple[str, str]

class PermissionManager:
    """管理工具调用的权限决策。"""

    def __init__(
        self,
        rules: list[dict[str, Any]] | None = None,
        tools: Iterable[ToolEntry] | None = None,
        config_mgr: Any = None,
    ):
        self.session_allow: list[PermissionEntry] = []
        self.deny_rules: list[PermissionEntry] = []
        self.allow_rules: list[PermissionEntry] = []
        self.config_mgr = config_mgr

        self._load_tool_rules(tools or [])
        permission_config = config_mgr.get_user_setting("permission") if config_mgr is not None else {}
        self._load_config_rules(permission_config, rules=rules)

    def reload(self) -> None:
        self.session_allow.clear()

    def _validate_permission(self, permission: Any, field_name: str) -> None:
        if permission not in RULE_PERMISSIONS:
            raise ValueError(
                f"{field_name} 必须是 {sorted(RULE_PERMISSIONS)} 之一，实际为 {permission!r}"
            )

    def _normalize_args(self, args: Any, field_name: str) -> dict[str, ArgPattern]:
        if args is None:
            return {}
        if not isinstance(args, dict):
            raise ValueError(f"{field_name} 必须是参数匹配字典")
        normalized: dict[str, ArgPattern] = {}
        for name, pattern in args.items():
            if not isinstance(name, str):
                raise ValueError(f"{field_name} 的键必须是字符串")
            if isinstance(pattern, str):
                normalized[name] = pattern
                continue
            if (
                isinstance(pattern, list)
                and pattern
                and all(isinstance(item, str) for item in pattern)
            ):
                normalized[name] = pattern
                continue
            raise ValueError(f"{field_name} 的值必须是字符串或非空字符串列表")
        return normalized

    def _load_tool_rules(self, tools: Iterable[ToolEntry]) -> None:
        for tool in tools or []:
            if tool.permission is None or tool.permission.rules is None:
                continue
            for index, rule in enumerate(tool.permission.rules):
                self._validate_permission(rule.permission, f"{tool.name}.permission.rules[{index}].permission")
                entry = PermissionEntry(
                    tool_name=tool.name,
                    permission=rule.permission,
                    args=(
                        self._normalize_args(rule.args, f"{tool.name}.permission.rules[{index}].args")
                        if rule.args is not None
                        else None
                    ),
                    checker=rule.checker,
                )
                if entry.permission == "deny":
                    self.deny_rules.append(entry)
                else:
                    self.allow_rules.append(entry)

    def _load_config_rules(
        self,
        config: dict[str, Any],
        rules: list[dict[str, Any]] | None = None,
    ) -> None:
        if not isinstance(config, dict):
            logger.warning("丢弃权限配置：permission 必须是字典")
            return
        unknown_keys = sorted(set(config) - {"rules"})
        if unknown_keys:
            logger.warning("丢弃权限配置：permission 包含未知字段：%s", ", ".join(unknown_keys))
            return

        config_rules = rules if rules is not None else config.get("rules", [])
        if not isinstance(config_rules, list):
            logger.warning("丢弃权限配置：permission.rules 必须是规则列表")
            return

        for index, rule in enumerate(config_rules):
            try:
                if not isinstance(rule, dict):
                    raise ValueError(f"permission.rules[{index}] 必须是规则字典")
                known_keys = {"tool", "args", "permission"}
                unknown_rule_keys = sorted(set(rule) - known_keys)
                if unknown_rule_keys:
                    raise ValueError(
                        f"permission.rules[{index}] 包含未知字段：{', '.join(unknown_rule_keys)}"
                    )

                permission = rule.get("permission")
                self._validate_permission(permission, f"permission.rules[{index}].permission")
                tool_name = rule.get("tool", "*")
                if not isinstance(tool_name, str):
                    raise ValueError(f"permission.rules[{index}].tool 必须是字符串")
                entry = PermissionEntry(
                    tool_name=tool_name,
                    permission=permission,
                    args=(
                        self._normalize_args(rule.get("args"), f"permission.rules[{index}].args")
                        if "args" in rule
                        else None
                    ),
                )
            except ValueError as exc:
                logger.warning("丢弃权限配置规则 permission.rules[%s]：%s", index, exc)
                continue
            if entry.permission == "deny":
                self.deny_rules.append(entry)
            else:
                self.allow_rules.append(entry)

    def _format_rule(self, rule: PermissionEntry) -> str:
        if not rule.args:
            return rule.tool_name
        parts = []
        for name, pattern in rule.args.items():
            value = "[" + "|".join(pattern) + "]" if isinstance(pattern, list) else pattern
            parts.append(f"{name}:{value}")
        return f"{rule.tool_name}({','.join(parts)})"

    def _format_args(self, tool: ToolEntry, tool_input: dict[str, Any]) -> dict[str, Any]:
        try:
            return {**tool_input, **tool.model(**tool_input).model_dump()}
        except ValidationError:
            return tool_input

    def _serialize_rule(self, rule: PermissionEntry) -> dict[str, Any]:
        serialized: dict[str, Any] = {
            "tool": rule.tool_name,
            "permission": rule.permission,
        }
        if rule.args:
            serialized["args"] = rule.args
        return serialized

    def _persist_allow_rule(self, rule: PermissionEntry) -> None:
        if self.config_mgr is None:
            return

        try:
            self.config_mgr.append_permission_rule(self._serialize_rule(rule))
        except OSError as exc:
            logger.warning("跳过权限持久化：%s", exc)

    def _build_detail(self, tool: ToolEntry, tool_input: dict[str, Any]) -> str:
        tips = tool.permission.tips if tool.permission else None
        if tips:
            values = self._format_args(tool, tool_input)
            try:
                return tips.format(**values)
            except (AttributeError, IndexError, KeyError, ValueError):
                return tips
        return tool.description

    def check(self, tool: ToolEntry | str, tool_input: dict[str, Any]) -> PermissionDecision:
        """
        返回：(permission, reason)，permission 为 allow|deny|ask。
        """
        tool_name = tool.name if isinstance(tool, ToolEntry) else tool
        for rule in self.deny_rules:
            if rule.tool_name not in {"*", tool_name}:
                continue
            values = self._format_args(tool, tool_input) if isinstance(tool, ToolEntry) else tool_input
            if rule.check(values):
                return "deny", f"被拒绝规则阻止：{self._format_rule(rule)}"

        for rule in [*self.allow_rules, *self.session_allow]:
            if rule.tool_name not in {"*", tool_name}:
                continue

            if isinstance(tool, ToolEntry):
                required_args = set(tool.permission.args or []) if tool.permission else set()
                rule_args = set(rule.args or {})
                if not required_args and rule_args:
                    continue
                if required_args and rule_args != required_args:
                    continue
                values = self._format_args(tool, tool_input)
            elif rule.args:
                continue
            else:
                values = tool_input

            if rule.check(values):
                return "allow", f"匹配权限规则：{self._format_rule(rule)}"

        return "ask", f"{tool_name} 未匹配权限规则，默认询问"

    async def resolve_ask(
        self,
        tool: ToolEntry,
        tool_input: dict[str, Any],
        deps: Any,
        *,
        persist_allowed: bool,
    ) -> PermissionDecision:
        """Resolve a final ask decision through UI once."""
        if deps is None or getattr(deps, "event_bus", None) is None:
            return "deny", f"权限需要用户确认，但缺少 event_bus：{tool.name}"

        answer = await deps.event_bus.request_permission(
            tool_name=tool.name,
            detail=self._build_detail(tool, tool_input),
        )
        normalized = answer.strip().lower()
        if normalized == "always":
            if persist_allowed:
                entry = self._build_allow_entry(tool, tool_input)
                if entry is not None:
                    self.session_allow.append(entry)
                    self._persist_allow_rule(entry)
            return "allow", "用户在当前会话中始终允许"
        if normalized in {"y", "yes"}:
            return "allow", "用户已允许"
        return "deny", "用户拒绝了权限请求"

    def _build_allow_entry(
        self,
        tool: ToolEntry,
        tool_input: dict[str, Any],
    ) -> PermissionEntry | None:
        permission_args = tool.permission.args if tool.permission and tool.permission.args else []
        if not permission_args:
            return PermissionEntry(tool.name, "allow", {})

        values = self._format_args(tool, tool_input)
        args: dict[str, str] = {}
        for arg_name in permission_args:
            value = values.get(arg_name)
            if isinstance(value, str):
                args[arg_name] = value
        return PermissionEntry(tool.name, "allow", args) if set(args) == set(permission_args) else None

    async def notify_decision(
        self,
        tool: ToolEntry,
        tool_input: dict[str, Any],
        deps: Any,
        permission: str,
        *,
        force: bool = False,
    ) -> None:
        event_bus = getattr(deps, "event_bus", None) if deps is not None else None
        if event_bus is None:
            return
        tips = tool.permission.tips if tool.permission else None
        if not force and permission != "deny" and tips is None:
            return
        detail = self._build_detail(tool, tool_input) if tips else ""
        await event_bus.notify_permission(
            status=permission,
            tool_name=tool.name,
            detail=detail,
        )
