"""权限管理器 — 权限模式 + 规则引擎。

评估顺序（8 步）：
1. 工具级 deny → 2. 工具级 ask → 3. 内容级 deny/ask（specifier_arg + fnmatch）
→ 4. check_permissions（工具安全逻辑）→ 5. 内容级 allow → 6. bypass 模式
→ 7. 工具级 allow + session_allow → 8. 模式默认策略。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from itertools import chain
from typing import Any, Callable, Iterable, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from src.tools.decorator import ToolEntry

from src.tools.decorator import format_tool_tips

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PermissionCheckResult:
    """工具级权限检查结果。

    Attributes:
        decision: 权限决策 — "allow"（放行）、"deny"（拒绝）、"ask"（需用户确认）、"passthrough"（无意见，交给后续流程）。
        reason: 决策原因，用于日志和 UI 展示。
        bypass_immune: 是否免疫 bypass 模式。为 True 时即使在 bypass 模式下也不会被自动放行。
    """
    decision: Literal["allow", "deny", "ask", "passthrough"]
    reason: str = ""
    bypass_immune: bool = False


@dataclass(frozen=True, slots=True)
class PermissionContext:
    """传给 tool.check_permissions 的上下文信息。

    Attributes:
        mode: 当前权限模式。
        workdir: 工作区根目录路径。
        tool_name: 被调用的工具名。
    """
    mode: PermissionMode
    workdir: str
    tool_name: str


def tool_sort_order(readonly: bool | None) -> int:
    """返回工具排序权重：只读工具排在前面，非只读次之，无权限元数据的排最后。

    Args:
        readonly: 工具是否只读。None 表示无权限元数据的外部工具。

    Returns:
        排序权重值（0=只读, 1=非只读, 2=外部工具）。
    """
    if readonly is None:
        return 2
    return 0 if readonly else 1


PermissionPolicy = Literal["allow", "deny", "ask"]


@dataclass(frozen=True, slots=True, eq=False)
class PermissionMode:
    """权限模式。

    Attributes:
        value: 模式标识名。
        description: 模式描述。
    """
    value: str
    description: str


# ── 模式常量 ──────────────

DEFAULT_MODE = PermissionMode(
    value="default",
    description="只读自动放行；文件编辑和命令执行默认询问，可被 allow 规则放行",
)
ACCEPT_EDITS_MODE = PermissionMode(
    value="acceptEdits",
    description="只读和文件编辑自动放行；命令执行默认询问",
)
PLAN_MODE = PermissionMode(
    value="plan",
    description="计划模式；只读自动放行，其余操作需确认",
)
BYPASS_MODE = PermissionMode(
    value="bypassPermissions",
    description="跳过权限检查；deny 和 ask 规则仍生效",
)
AUTO_MODE = PermissionMode(
    value="auto",
    description="自动放行所有操作；deny 和 ask 规则仍生效",
)
DONT_ASK_MODE = PermissionMode(
    value="dontAsk",
    description="从不弹窗；只读自动放行，其余操作拒绝",
)

# Shift+Tab 轮转模式（对齐 getNextPermissionMode.ts）
CAROUSEL_MODES: tuple[PermissionMode, ...] = (
    DEFAULT_MODE, ACCEPT_EDITS_MODE, PLAN_MODE,
)

# /mode 菜单可选模式
MENU_MODES: tuple[PermissionMode, ...] = (
    DEFAULT_MODE, ACCEPT_EDITS_MODE, PLAN_MODE,
    BYPASS_MODE, AUTO_MODE, DONT_ASK_MODE,
)

# 所有模式
ALL_MODES: tuple[PermissionMode, ...] = MENU_MODES


def parse_permission_mode(text: str) -> PermissionMode | None:
    """根据编号或模式名解析权限模式。

    Args:
        text: 编号（1-N）或模式名字符串。

    Returns:
        匹配的 PermissionMode，无匹配时返回 None。
    """
    normalized = text.strip().lower()
    for index, mode in enumerate(MENU_MODES, start=1):
        if normalized == str(index) or normalized == mode.value.lower():
            return mode
    return None


# ── 规则 ──────────────────────────────────────────────────────────────

_RULE_PATTERN = re.compile(r"^(\w+)(?:\((.+)\))?$")

PermissionDecision = tuple[Literal["allow", "deny", "ask", "auto_allow"], str]


@dataclass(frozen=True)
class PermissionRule:
    """一条权限规则，如 'shell(npm *)'。

    Attributes:
        tool: 工具名，如 "shell"。
        specifier: 括号内的匹配模式，None 表示匹配该工具全部调用。
        permission: "allow"、"deny" 或 "ask"。
    """
    tool: str
    specifier: str | None
    permission: Literal["allow", "deny", "ask"]

    def matches_tool(self, tool_name: str) -> bool:
        """判断规则是否匹配指定工具名（含 * 通配符）。

        Args:
            tool_name: 被调用的工具名。

        Returns:
            是否匹配。
        """
        return self.tool == "*" or self.tool == tool_name

    @property
    def is_tool_level(self) -> bool:
        """是否为工具级规则（无 specifier，匹配该工具的所有调用）。"""
        return self.specifier is None

    def __str__(self) -> str:
        """返回规则文本表示，如 'shell(npm *)'。"""
        if self.specifier is None:
            return self.tool
        return f"{self.tool}({self.specifier})"


def parse_rule(text: str, permission: Literal["allow", "deny", "ask"]) -> PermissionRule | None:
    """将规则文本解析为 PermissionRule。

    Args:
        text: 规则文本，如 "shell(npm *)" 或 "write_file"。
        permission: 该规则的权限类型。

    Returns:
        解析出的 PermissionRule，格式非法时返回 None。
    """
    text = text.strip()
    m = _RULE_PATTERN.match(text)
    if m is None:
        logger.warning("忽略无法解析的权限规则：%r", text)
        return None
    tool = m.group(1)
    specifier = m.group(2)
    return PermissionRule(tool=tool, specifier=specifier, permission=permission)


# ── 内容级规则匹配（仅被 check() 内部调用）─────────────────────────────


def _match_deny_ask_rules(
    content_rules: tuple[PermissionRule, ...],
    value: str,
) -> PermissionCheckResult | None:
    """在内容级规则中匹配 deny 和 ask 规则。

    Args:
        content_rules: 当前工具的内容级规则列表（已按 deny→ask→allow 排序）。
        value: 从 tool_input 中提取的匹配值（如命令字符串、文件路径）。

    Returns:
        匹配到的 PermissionCheckResult，无匹配返回 None。
    """
    for rule in content_rules:
        if rule.specifier is None or rule.permission == "allow":
            continue
        if fnmatch(value, rule.specifier):
            if rule.permission == "deny":
                return PermissionCheckResult("deny", f"被 deny 规则阻止：{rule}")
            if rule.permission == "ask":
                return PermissionCheckResult("ask", f"被 ask 规则要求确认：{rule}", bypass_immune=True)
    return None


def _match_allow_rules(
    content_rules: tuple[PermissionRule, ...],
    value: str,
) -> PermissionCheckResult | None:
    """在内容级规则中匹配 allow 规则。

    Args:
        content_rules: 当前工具的内容级规则列表。
        value: 从 tool_input 中提取的匹配值。

    Returns:
        匹配到的 PermissionCheckResult，无匹配返回 None。
    """
    for rule in content_rules:
        if rule.specifier is None or rule.permission != "allow":
            continue
        if fnmatch(value, rule.specifier):
            return PermissionCheckResult("allow", f"匹配 allow 规则：{rule}")
    return None


# ── PermissionManager ─────────────────────────────────────────────────


class PermissionManager:
    """管理工具调用的权限决策。

    评估顺序见模块文档字符串和 check() 方法。
    """

    def __init__(
        self,
        config_mgr: Any = None,
        tools: Iterable[ToolEntry] | None = None,
        workdir: str = "",
    ):
        self.mode: PermissionMode = DEFAULT_MODE
        self._pre_plan_mode: PermissionMode | None = None
        self.deny_rules: list[PermissionRule] = []
        self.ask_rules: list[PermissionRule] = []
        self.allow_rules: list[PermissionRule] = []
        self.session_allow: list[PermissionRule] = []
        self.config_mgr = config_mgr
        self._workdir = workdir
        self._check_permissions_fns: dict[str, Callable[[dict[str, Any], PermissionContext], PermissionCheckResult]] = {}
        self._tool_tips: dict[str, str] = {}
        self._readonly_flags: dict[str, bool] = {}
        self._specifier_args: dict[str, str] = {}

        self._load_tool_metadata(tools or [])
        self._load_config()

    def _load_tool_metadata(self, tools: Iterable[ToolEntry]) -> None:
        """从工具声明中提取 readonly 标志、specifier_arg、check_permissions 和 tips 模板。

        Args:
            tools: 所有已注册的工具列表。
        """
        for tool in tools:
            if tool.permission is None:
                continue
            self._readonly_flags[tool.name] = tool.permission.readonly
            if tool.permission.tips:
                self._tool_tips[tool.name] = tool.permission.tips
            if tool.permission.check_permissions is not None:
                self._check_permissions_fns[tool.name] = tool.permission.check_permissions
            if tool.permission.specifier_arg is not None:
                self._specifier_args[tool.name] = tool.permission.specifier_arg

    def _load_config(self) -> None:
        """从配置文件加载权限规则和默认模式。"""
        permissions = self.config_mgr.get_user_setting("permissions")
        if not isinstance(permissions, dict):
            return

        default_mode = permissions.get("defaultMode")
        if isinstance(default_mode, str):
            mode = parse_permission_mode(default_mode)
            if mode is None:
                logger.warning("忽略无效的 permissions.defaultMode：%r", default_mode)
            else:
                self.mode = mode

        self._parse_rules(permissions.get("deny", []), "deny", self.deny_rules)
        self._parse_rules(permissions.get("ask", []), "ask", self.ask_rules)
        self._parse_rules(permissions.get("allow", []), "allow", self.allow_rules)

    @staticmethod
    def _parse_rules(
        items: Iterable,
        permission: Literal["allow", "deny", "ask"],
        target: list[PermissionRule],
    ) -> None:
        """将配置文本列表解析为 PermissionRule 并追加到目标列表。

        Args:
            items: 规则文本列表（来自配置文件）。
            permission: 规则权限类型。
            target: 追加解析结果的目标列表。
        """
        for text in items:
            if isinstance(text, str) and (rule := parse_rule(text, permission)):
                target.append(rule)

    def set_mode(self, mode: PermissionMode) -> bool:
        """切换权限模式，处理 plan 模式转换逻辑。

        进入 plan 模式时保存当前模式以便 leave_plan_mode 恢复。

        Args:
            mode: 目标权限模式。

        Returns:
            模式是否发生了变化。
        """
        if mode is self.mode:
            return False
        if mode is PLAN_MODE:
            self._pre_plan_mode = self.mode
        self.mode = mode
        return True

    def leave_plan_mode(self) -> bool:
        """离开 plan 模式，恢复进入前保存的模式。

        Returns:
            是否成功离开（不在 plan 模式时返回 False）。
        """
        if self.mode is not PLAN_MODE:
            return False
        self.mode = self._pre_plan_mode or DEFAULT_MODE
        self._pre_plan_mode = None
        return True

    def is_tool_visible(self, tool: ToolEntry) -> bool:
        """判断工具是否应在当前权限模式下暴露给 LLM。

        plan 模式下隐藏非只读工具；其他模式全部可见。
        无权限元数据的外部工具始终可见。
        """
        if self.mode is not PLAN_MODE:
            return True
        if tool.permission is None:
            return True
        return tool.permission.readonly

    def reload(self) -> None:
        """重置会话级状态（/clear 时调用）。"""
        self.mode = DEFAULT_MODE
        self._pre_plan_mode = None
        self.session_allow.clear()
        self.deny_rules.clear()
        self.ask_rules.clear()
        self.allow_rules.clear()
        self._load_config()

    def check(self, tool_name: str, tool_input: dict[str, Any]) -> PermissionDecision:
        """权限检查核心流程。

        评估顺序：
        1. 工具级 deny 规则 → deny
        2. 工具级 ask 规则 → ask（bypass-immune）
        3. 内容级 deny/ask 规则（用 specifier_arg 提取值，fnmatch 匹配）→ deny/ask
        4. check_permissions（仅工具自身安全逻辑）→ deny/ask/allow/passthrough
        5. 内容级 allow 规则（含 session_allow）→ allow
        6. bypass 模式 → auto_allow
        7. 工具级 allow 规则 + session_allow → allow
        8. 模式默认策略（仅按 readonly 判断）

        Args:
            tool_name: 被调用的工具名。
            tool_input: 工具调用参数。

        Returns:
            (decision, reason) 元组，decision 为 allow|deny|ask|auto_allow。
        """
        # Step 1: 工具级 deny 规则
        for rule in self.deny_rules:
            if rule.is_tool_level and rule.matches_tool(tool_name):
                return "deny", f"被 deny 规则阻止：{rule}"

        # Step 2: 工具级 ask 规则（强制询问，bypass 模式也不跳过）
        for rule in self.ask_rules:
            if rule.is_tool_level and rule.matches_tool(tool_name):
                return "ask", f"被 ask 规则要求确认：{rule}"

        # 提取 specifier 值和内容级规则
        specifier_value = self._extract_specifier(tool_name, tool_input)
        content_rules = self._collect_content_rules(tool_name) if specifier_value is not None else ()

        # Step 3: 内容级 deny/ask 规则
        if specifier_value is not None:
            result = _match_deny_ask_rules(content_rules, specifier_value)
            if result is not None:
                if result.decision == "deny":
                    return "deny", result.reason
                if result.decision == "ask":
                    return "ask", result.reason

        # Step 4: check_permissions（仅工具自身安全逻辑）
        check_fn = self._check_permissions_fns.get(tool_name)
        if check_fn is not None:
            ctx = PermissionContext(mode=self.mode, workdir=self._workdir, tool_name=tool_name)
            result = check_fn(tool_input, ctx)
            if result.decision == "deny":
                return "deny", result.reason or f"被内置安全检测阻止：{tool_name}"
            if result.decision == "ask":
                if result.bypass_immune or self.mode is not BYPASS_MODE:
                    return "ask", result.reason or f"需要用户确认：{tool_name}"
            if result.decision == "allow":
                return "allow", result.reason or f"工具级检查放行：{tool_name}"

        # Step 5: 内容级 allow 规则（含 session_allow）
        if specifier_value is not None:
            result = _match_allow_rules(content_rules, specifier_value)
            if result is not None:
                return "allow", result.reason

        # Step 6: bypass 模式
        if self.mode is BYPASS_MODE:
            return "auto_allow", f"bypassPermissions 模式自动放行：{tool_name}"

        # Step 7: 工具级 allow 规则 + session_allow
        for rule in chain(self.allow_rules, self.session_allow):
            if rule.is_tool_level and rule.matches_tool(tool_name):
                return "allow", f"匹配 allow 规则：{rule}"

        # Step 8: 模式默认策略
        return self._mode_default(tool_name)

    def _extract_specifier(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        """根据 specifier_arg 从 tool_input 中提取 specifier 值。

        Args:
            tool_name: 工具名。
            tool_input: 工具调用参数。

        Returns:
            提取到的字符串值，未声明 specifier_arg 或值非字符串时返回 None。
        """
        arg = self._specifier_args.get(tool_name)
        if arg is None:
            return None
        value = tool_input.get(arg)
        return value if isinstance(value, str) else None

    def _collect_content_rules(self, tool_name: str) -> tuple[PermissionRule, ...]:
        """收集与指定工具匹配的所有内容级规则，按 deny→ask→allow+session_allow 排序。

        Args:
            tool_name: 工具名。

        Returns:
            内容级规则元组。
        """
        rules: list[PermissionRule] = []
        for rule in self.deny_rules:
            if not rule.is_tool_level and rule.matches_tool(tool_name):
                rules.append(rule)
        for rule in self.ask_rules:
            if not rule.is_tool_level and rule.matches_tool(tool_name):
                rules.append(rule)
        for rule in chain(self.allow_rules, self.session_allow):
            if not rule.is_tool_level and rule.matches_tool(tool_name):
                rules.append(rule)
        return tuple(rules)

    def _mode_default(self, tool_name: str) -> PermissionDecision:
        """按当前模式和 readonly 标志返回默认权限决策。

        Args:
            tool_name: 工具名。

        Returns:
            (decision, reason) 元组。
        """
        is_readonly = self._readonly_flags.get(tool_name, False)

        if self.mode is AUTO_MODE:
            return "auto_allow", f"auto 模式自动放行：{tool_name}"

        if self.mode is DONT_ASK_MODE:
            if is_readonly:
                return "auto_allow", f"dontAsk 模式放行只读：{tool_name}"
            return "deny", f"dontAsk 模式拒绝：{tool_name}"

        # DEFAULT / PLAN / ACCEPT_EDITS 共享逻辑：只读放行，其余询问
        if is_readonly:
            return "auto_allow", f"自动放行只读：{tool_name}"
        return "ask", f"{tool_name} 需要用户确认"

    async def resolve_ask(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        deps: Any,
    ) -> PermissionDecision:
        """通过 UI 提示用户确认权限。

        Args:
            tool_name: 工具名。
            tool_input: 工具调用参数。
            deps: AgentDeps 依赖对象。

        Returns:
            (decision, reason) 元组。
        """
        if deps is None or getattr(deps, "event_bus", None) is None:
            return "deny", f"权限需要用户确认，但缺少 event_bus：{tool_name}"

        detail = format_tool_tips(self._tool_tips.get(tool_name), tool_input, tool_name)
        answer = await deps.event_bus.request_permission(
            tool_name=tool_name,
            detail=detail,
        )
        normalized = answer.strip().lower()
        if normalized == "always":
            rule = self._build_session_rule(tool_name, tool_input)
            self.session_allow.append(rule)
            self._persist_allow_rule(rule)
            return "allow", "用户在当前会话中始终允许"
        if normalized in {"y", "yes"}:
            return "allow", "用户已允许"
        return "deny", "用户拒绝了权限请求"

    def _build_session_rule(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> PermissionRule:
        """根据工具调用构建 session allow 规则。

        使用工具声明的 specifier_arg 提取具体参数值作为 specifier，
        使 "always allow" 仅允许该特定值，而非该工具的所有调用。
        无 specifier_arg 时构建工具级规则（specifier=None）。

        Args:
            tool_name: 工具名。
            tool_input: 工具调用参数。

        Returns:
            构建的 PermissionRule。
        """
        value = self._extract_specifier(tool_name, tool_input)
        return PermissionRule(tool=tool_name, specifier=value, permission="allow")

    def _persist_allow_rule(self, rule: PermissionRule) -> None:
        """将 allow 规则持久化到 settings.json。

        Args:
            rule: 要持久化的权限规则。
        """
        if self.config_mgr is None:
            return
        try:
            self.config_mgr.append_permission_list("allow", str(rule))
        except (OSError, AttributeError) as exc:
            logger.warning("跳过权限持久化：%s", exc)

    async def notify_decision(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        deps: Any,
        decision: str,
    ) -> None:
        """向 UI 发送权限决策通知。

        Args:
            tool_name: 工具名。
            tool_input: 工具调用参数。
            deps: AgentDeps 依赖对象。
            decision: 权限决策（allow|deny|auto_allow）。
        """
        event_bus = getattr(deps, "event_bus", None) if deps is not None else None
        if event_bus is None:
            return

        if decision == "allow":
            return

        detail = format_tool_tips(self._tool_tips.get(tool_name), tool_input, tool_name)
        await event_bus.notify_permission(
            status=decision,
            tool_name=tool_name,
            detail=detail,
        )
