from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
import logging
from typing import Any, Iterable, Literal
from pydantic import ValidationError
from src.tools.decorator import ToolEntry

logger = logging.getLogger(__name__)

RulePermission = Literal["allow", "deny"]
ArgPattern = str | list[str]

RULE_PERMISSIONS = {"allow", "deny"}

@dataclass(frozen=True)
class PermissionEntry:
    tool_name: str
    permission: RulePermission
    args: dict[str, ArgPattern]

PermissionDecision = tuple[str, str]

class PermissionManager:
    """管理工具调用的权限决策。"""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        rules: list[dict[str, Any]] | None = None,
        tools: Iterable[ToolEntry] | None = None,
    ):
        self.session_allow: list[PermissionEntry] = []
        self.deny_rules: list[PermissionEntry] = []
        self.allow_rules: list[PermissionEntry] = []

        self._load_tool_rules(tools or [])
        self._load_config_rules(config or {}, rules=rules)

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
                    args=self._normalize_args(rule.args or {}, f"{tool.name}.permission.rules[{index}].args"),
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
                    args=self._normalize_args(rule.get("args", {}), f"permission.rules[{index}].args"),
                )
            except ValueError as exc:
                logger.warning("丢弃权限配置规则 permission.rules[%s]：%s", index, exc)
                continue
            if entry.permission == "deny":
                self.deny_rules.append(entry)
            else:
                self.allow_rules.append(entry)

    def _args_match(self, patterns: dict[str, ArgPattern], values: dict[str, Any]) -> bool:
        for arg_name, pattern in patterns.items():
            if isinstance(pattern, str) and pattern == "*":
                if arg_name not in values:
                    return False
                continue
            value = values.get(arg_name)
            if not isinstance(value, str):
                return False
            pattern_list = pattern if isinstance(pattern, list) else [pattern]
            matched = False
            for item in pattern_list:
                if fnmatch(value, item):
                    matched = True
                    break
                if item.endswith(":*"):
                    prefix = item[:-2]
                    if value == prefix or value.startswith(f"{prefix} "):
                        matched = True
                        break
            if not matched:
                return False
        return True

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
            if self._args_match(rule.args, values):
                return "deny", f"被拒绝规则阻止：{self._format_rule(rule)}"

        for rule in [*self.allow_rules, *self.session_allow]:
            if rule.tool_name not in {"*", tool_name}:
                continue

            if isinstance(tool, ToolEntry):
                required_args = set(tool.permission.args or []) if tool.permission else set()
                rule_args = set(rule.args)
                if not required_args and rule_args:
                    continue
                if required_args and rule_args != required_args:
                    continue
                values = self._format_args(tool, tool_input)
            elif rule.args:
                continue
            else:
                values = tool_input

            if self._args_match(rule.args, values):
                return "allow", f"匹配权限规则：{self._format_rule(rule)}"

        return "ask", f"{tool_name} 未匹配权限规则，默认询问"

    async def authorize(self, tool: ToolEntry, tool_input: dict[str, Any], deps: Any) -> PermissionDecision:
        """执行前权限确认。返回最终决策。"""
        permission, reason = self.check(tool, tool_input)
        should_notify = permission in {"allow", "deny"}

        if permission == "ask":
            if deps is None or getattr(deps, "event_bus", None) is None:
                permission = "deny"
                reason = f"权限需要用户确认，但缺少 event_bus：{tool.name}"
            else:
                answer = await deps.event_bus.request_permission(
                    tool_name=tool.name,
                    detail=self._build_detail(tool, tool_input),
                )
                normalized = answer.strip().lower()
                if normalized == "always":
                    permission_args = tool.permission.args if tool.permission and tool.permission.args else []
                    if not permission_args:
                        self.session_allow.append(PermissionEntry(tool.name, "allow", {}))
                    else:
                        values = self._format_args(tool, tool_input)
                        args: dict[str, str] = {}
                        for arg_name in permission_args:
                            value = values.get(arg_name)
                            if isinstance(value, str):
                                args[arg_name] = value
                        if set(args) == set(permission_args):
                            self.session_allow.append(PermissionEntry(tool.name, "allow", args))
                    permission = "allow"
                    reason = "用户在当前会话中始终允许"
                elif normalized in {"y", "yes"}:
                    permission = "allow"
                    reason = "用户已允许"
                else:
                    permission = "deny"
                    reason = "用户拒绝了权限请求"
                    should_notify = True

        event_bus = getattr(deps, "event_bus", None) if deps is not None else None
        tips = tool.permission.tips if tool.permission else None
        should_notify = should_notify and event_bus is not None and (permission == "deny" or tips is not None)
        if should_notify:
            detail = self._build_detail(tool, tool_input) if tips else ""
            await event_bus.notify_permission(
                status=permission,
                tool_name=tool.name,
                detail=detail,
            )
        return permission, reason

