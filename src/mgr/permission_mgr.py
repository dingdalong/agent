"""权限管理器 — 权限模式 + 规则引擎。

评估顺序（6 步）：
1. deny 规则 → 2. ask 规则 → 3. check_permissions（工具安全逻辑）
→ 4. allow 规则（含 session_allow）→ 5. bypass 模式 → 6. 模式默认策略。
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
        tool: 工具名，如 "shell"。配置中的 "*" 通配符在加载时已展开为具体工具名。
        specifier: fnmatch 匹配模式。"*" 表示匹配该工具的所有调用。
        permission: "allow"、"deny" 或 "ask"。
    """
    tool: str
    specifier: str
    permission: Literal["allow", "deny", "ask"]

    def matches_specifier(self, specifier_value: str) -> bool:
        """判断规则的 specifier 是否匹配给定值。

        Args:
            specifier_value: 从 tool_input 中提取的匹配值，无值时传空字符串。

        Returns:
            是否匹配。
        """
        return fnmatch(specifier_value, self.specifier)

    def __str__(self) -> str:
        """返回规则文本表示，如 'shell(npm *)'。"""
        if self.specifier == "*":
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
    specifier = m.group(2) or "*"
    return PermissionRule(tool=tool, specifier=specifier, permission=permission)


# ── PermissionManager ─────────────────────────────────────────────────

# 规则字典类型：key 为 tool 名（含 "*" 通配符），value 为该工具的规则列表。
RulesDict = dict[str, list[PermissionRule]]

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
        self.deny_rules: RulesDict = {}
        self.ask_rules: RulesDict = {}
        self.allow_rules: RulesDict = {}
        self.session_allow: RulesDict = {}
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
    def _add_rule(rules: RulesDict, rule: PermissionRule) -> None:
        """将规则添加到按工具名索引的规则字典中。

        Args:
            rules: 目标规则字典。
            rule: 要添加的权限规则。
        """
        rules.setdefault(rule.tool, []).append(rule)

    @staticmethod
    def _parse_rules(
        items: Iterable,
        permission: Literal["allow", "deny", "ask"],
        target: RulesDict,
    ) -> None:
        """将配置文本列表解析为 PermissionRule 并添加到目标字典。

        Args:
            items: 规则文本列表（来自配置文件）。
            permission: 规则权限类型。
            target: 按工具名索引的目标规则字典。
        """
        for text in items:
            if isinstance(text, str) and (rule := parse_rule(text, permission)):
                target.setdefault(rule.tool, []).append(rule)

    def set_mode(self, mode: PermissionMode) -> bool:
        """切换权限模式，进入 plan 模式时保存当前模式。

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

    def restore_pre_plan_mode(self) -> PermissionMode:
        """退出 plan 模式时恢复进入前的权限模式，同时清除计划文件路径。

        Returns:
            恢复后的权限模式。无保存记录时回退到 DEFAULT_MODE。
        """
        target = self._pre_plan_mode or DEFAULT_MODE
        self._pre_plan_mode = None
        self.mode = target
        return target

    def is_tool_visible(self, tool: ToolEntry) -> bool:
        """判断工具在当前权限模式下是否暴露给 LLM。

        可见性规则（按优先级）：
        - 无权限元数据的外部工具：始终可见
        - plan_visible 工具：仅 plan 模式可见（优先于 readonly）
        - readonly 工具：所有模式可见
        - 普通非只读工具：非 plan 模式可见，plan 模式隐藏
        """
        if tool.permission is None:
            return True
        if tool.permission.plan_visible:
            return self.mode is PLAN_MODE
        if tool.permission.readonly:
            return True
        return self.mode is not PLAN_MODE

    def reload(self) -> None:
        """重置会话级状态（/clear 时调用）。"""
        self.mode = DEFAULT_MODE
        self._pre_plan_mode = None
        self.session_allow.clear()
        self.deny_rules.clear()
        self.ask_rules.clear()
        self.allow_rules.clear()
        self._load_config()

    @staticmethod
    def _get_rules(rules: RulesDict, tool_name: str) -> list[PermissionRule]:
        """获取匹配指定工具名的规则。

        Args:
            rules: 按工具名索引的规则字典。
            tool_name: 被调用的工具名。

        Returns:
            该工具的规则列表，无匹配时返回空列表。
        """
        return rules.get(tool_name, [])

    def check(self, tool_name: str, tool_input: dict[str, Any]) -> PermissionDecision:
        """权限检查核心流程。

        评估顺序：
        1. deny 规则 → deny
        2. ask 规则 → ask
        3. check_permissions（工具自身安全逻辑）→ deny/ask/allow/passthrough
        4. allow 规则（含 session_allow）→ allow
        5. bypass 模式 → auto_allow
        6. 模式默认策略（按 readonly 判断）

        Args:
            tool_name: 被调用的工具名。
            tool_input: 工具调用参数。

        Returns:
            (decision, reason) 元组，decision 为 allow|deny|ask|auto_allow。
        """
        specifier_value = self._extract_specifier(tool_name, tool_input)

        # Step 1: deny 规则
        for rule in self._get_rules(self.deny_rules, tool_name):
            if rule.matches_specifier(specifier_value):
                return "deny", f"被 deny 规则阻止：{rule}"

        # Step 2: ask 规则
        for rule in self._get_rules(self.ask_rules, tool_name):
            if rule.matches_specifier(specifier_value):
                return "ask", f"被 ask 规则要求确认：{rule}"

        # Step 3: check_permissions（工具自身安全逻辑）
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

        # Step 4: allow 规则（含 session_allow）
        for rule in chain(
            self._get_rules(self.allow_rules, tool_name),
            self._get_rules(self.session_allow, tool_name),
        ):
            if rule.matches_specifier(specifier_value):
                return "allow", f"匹配 allow 规则：{rule}"

        # Step 5: bypass 模式
        if self.mode is BYPASS_MODE:
            return "auto_allow", f"bypassPermissions 模式自动放行：{tool_name}"

        # Step 6: 模式默认策略
        return self._mode_default(tool_name)

    def _extract_specifier(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """根据 specifier_arg 从 tool_input 中提取 specifier 值。

        Args:
            tool_name: 工具名。
            tool_input: 工具调用参数。

        Returns:
            提取到的字符串值，未声明 specifier_arg 或值非字符串时返回空字符串。
        """
        arg = self._specifier_args.get(tool_name)
        if arg is None:
            return ""
        value = tool_input.get(arg)
        return value if isinstance(value, str) else ""

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
        if normalized == "session":
            rule = self._build_session_rule(tool_name, tool_input)
            self._add_rule(self.session_allow, rule)
            return "allow", "用户在当前会话中始终允许"
        if normalized == "always":
            rule = self._build_session_rule(tool_name, tool_input)
            self._add_rule(self.session_allow, rule)
            self._persist_allow_rule(rule)
            return "allow", "用户始终允许（已保存）"
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
        无 specifier_arg 时 specifier 为 "*"（匹配所有调用）。

        Args:
            tool_name: 工具名。
            tool_input: 工具调用参数。

        Returns:
            构建的 PermissionRule。
        """
        value = self._extract_specifier(tool_name, tool_input)
        return PermissionRule(tool=tool_name, specifier=value or "*", permission="allow")

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
