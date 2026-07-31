"""权限管理器 — 权限模式 + 规则引擎。

评估顺序：
1. deny 规则 → 2. ask 规则 → 3. check_permissions（工具安全逻辑，bypass_immune 立即返回，
非 bypass_immune 记录后穿透）→ 4. allow 规则（含 session_allow）→ 4.5. 处理穿透的 tool ask
（按模式分流）→ 4.7. mcp_servers.json 声明的 server 级规则（最低优先级层，仅 settings.json
静默时生效，含 deny→ask→allow）→ 5. bypass 模式 → 6. 模式默认策略。

settings.json 规则（Step 1-4）与 mcp_servers.json 规则（Step 4.7）共用同一 PermissionRule
表示与匹配引擎，仅优先级不同：settings 层先于 mcp 层评估，故 settings 的 allow 能覆盖 mcp 的 deny。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from itertools import chain
from typing import Any, Callable, Iterable, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from src.tools import ToolDict
    from src.tools.decorator import ToolEntry

from src.events.types import caller_identity
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
class PermissionVerdict:
    """auto 模式 LLM 判官的结构化裁决。

    Attributes:
        decision: 裁决 — "allow"（放行）、"ask"（交人工确认）、"deny"（拒绝并让 agent 重试）。
        reason: 裁决理由，用于日志、UI 展示与拒绝时供 agent 调整重试。
    """
    decision: Literal["allow", "ask", "deny"]
    reason: str = ""


@dataclass(frozen=True, slots=True)
class PermissionContext:
    """传给 tool.check_permissions 的上下文信息。

    Attributes:
        mode: 当前权限模式。
        workdir: 工作区根目录路径。
        tool_name: 被调用的工具名。
        trusted_dirs: 可信目录路径列表（如 global_dir），这些目录内的文件读写无需询问。
        specifier_arg: 该工具用于规则匹配的参数名（如 "command"、"path"），None 表示无内容级匹配。
    """
    mode: PermissionMode
    workdir: str
    tool_name: str
    trusted_dirs: tuple[str, ...] = ()
    specifier_arg: str | None = None


def tool_sort_order(kind: str | None, *, has_permission: bool = True) -> int:
    """返回工具排序权重：只读工具排在前面，非只读次之，无权限元数据的排最后。

    Args:
        kind: 工具类别（"readonly"/"edit"/None）。
        has_permission: 工具是否有权限元数据。False 表示外部工具。

    Returns:
        排序权重值（0=只读, 1=非只读, 2=外部工具）。
    """
    if not has_permission:
        return 2
    return 0 if kind == "readonly" else 1


PermissionPolicy = Literal["allow", "deny", "ask"]


@dataclass(frozen=True, slots=True, eq=False)
class PermissionMode:
    """权限模式。

    Attributes:
        value: 模式标识名。
        description: 模式描述。
        reminder: 每轮 turn start 注入的提醒文本，空串表示无需提醒。
    """
    value: str
    description: str
    reminder: str = ""


# ── 模式常量 ──────────────

DEFAULT_MODE = PermissionMode(
    value="default",
    description="只读自动放行；文件编辑和命令执行默认询问，可被 allow 规则放行",
)
ACCEPT_EDITS_MODE = PermissionMode(
    value="acceptEdits",
    description="只读和文件编辑自动放行；命令执行默认询问",
    reminder="【acceptEdits 模式】允许读取和编辑文件。",
)
PLAN_MODE = PermissionMode(
    value="plan",
    description="计划模式；只读自动放行，其余操作需确认",
    reminder="【计划模式】只允许读取文件。计划文件的创建和编辑是例外。",
)
BYPASS_MODE = PermissionMode(
    value="bypassPermissions",
    description="跳过权限检查；deny 和 ask 规则仍生效",
)
AUTO_MODE = PermissionMode(
    value="auto",
    description="文件编辑和安全 shell 命令自动放行；其余操作询问；deny 和 ask 规则仍生效",
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

_RULE_PATTERN = re.compile(r"^([\w*?-]+)(?:\((.+)\))?$")

PermissionDecision = tuple[Literal["allow", "deny", "ask", "auto_allow"], str]


@dataclass(frozen=True)
class PermissionRule:
    """一条权限规则，如 'shell(npm *)'。

    支持三种匹配模式：
    - exact：精确匹配（如 'git commit -m "fix"'）
    - prefix：前缀匹配，以 ':*' 结尾（如 'git commit:*'），要求完整词边界
    - wildcard：通配符匹配，含 '*' 或 '?'（如 'npm *'），走 fnmatch

    Attributes:
        tool: 工具名，如 "shell"。可含 "*"/"?" 通配符（如 "mcp__github__*"），由 _get_rules 在调用期 fnmatch 匹配。
        specifier: 匹配模式字符串。"*" 表示匹配该工具的所有调用。
        permission: "allow"、"deny" 或 "ask"。
    """
    tool: str
    specifier: str
    permission: Literal["allow", "deny", "ask"]

    @property
    def rule_type(self) -> Literal["exact", "prefix", "wildcard"]:
        """返回规则的匹配类型。

        Returns:
            'prefix'（以 ':*' 结尾）、'wildcard'（含 '*'/'?'）、'exact'（精确匹配）。
        """
        if self.specifier == "*":
            return "wildcard"
        if self.specifier.endswith(":*"):
            return "prefix"
        if "*" in self.specifier or "?" in self.specifier:
            return "wildcard"
        return "exact"

    @property
    def prefix_value(self) -> str | None:
        """提取前缀规则中 ':*' 之前的前缀值。

        Returns:
            前缀字符串（如 'git commit:*' → 'git commit'），非前缀规则返回 None。
        """
        if self.specifier.endswith(":*"):
            return self.specifier[:-2]
        return None

    def matches_specifier(self, specifier_value: str) -> bool:
        """判断规则的 specifier 是否匹配给定值。

        按 rule_type 分派匹配策略：
        - prefix：前缀后须跟空格或到末尾（词边界）
        - wildcard：fnmatch 通配符匹配
        - exact：精确相等

        Args:
            specifier_value: 从 tool_input 中提取的匹配值，无值时传空字符串。

        Returns:
            是否匹配。
        """
        if self.specifier == "*":
            return True
        rt = self.rule_type
        if rt == "prefix":
            prefix = self.prefix_value
            return specifier_value == prefix or specifier_value.startswith(prefix + " ")
        if rt == "wildcard":
            return fnmatch(specifier_value, self.specifier)
        return specifier_value == self.specifier

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


# ── auto 模式 LLM 判官 ─────────────────────────────────────────────────

# 判官配置缺省值（settings.json 无内置层，缺省在此兜底）
_JUDGE_DEFAULT_MODEL = "fast"
_JUDGE_DEFAULT_MAX_CONSECUTIVE = 3
_JUDGE_DEFAULT_MAX_TOTAL = 20

# 判官系统提示词：auto 模式下替代人工审批的放行闸门（对齐 Claude Code auto mode 的分类器角色）
_JUDGE_SYSTEM_PROMPT = """你是自主编码 agent 的权限判官，是「人工审批」的替身闸门。当前 agent 运行在 auto 权限模式：确定安全的操作已被快路径放行，确定危险的已被硬规则拦截，只有影响面模糊的操作才交到你手里。你要替代人类快速判断这次工具调用能否放行。

你会收到：工作目录、工具名，以及工具参数（shell 给完整命令，文件工具给目标路径与操作，其它工具给名称与参数）。

裁决三选一，调用 record_verdict 返回：
- allow：明显安全的常规开发操作。如构建/测试/lint、读取与检索、工作目录内新建与修改、常规 git 操作（status/diff/add/commit/log 等）、安装项目依赖。
- deny：明显有害或越界。如删库或大范围不可逆删除、改动系统目录或用户主目录中与本项目无关的文件、向外部网络泄露仓库内容或凭证、绕过或提升自身权限、明显超出当前任务范围的操作。reason 里点明风险点，供 agent 换更安全做法后重试。
- ask：你无法判定影响面、或需人类知情的操作。如 git push、对外发布、删除数量不明的文件、触及工作区外但未必有害的路径。交人类确认。

判据：宁可 ask 也不要误放行有害操作；但对确属常规的开发操作要果断 allow，避免打断 agent。reason 用一句话说明理由。"""

# 结构化裁决工具：无 JSON-mode helper，用强制工具调用取回 (decision, reason)
_JUDGE_VERDICT_TOOL: "ToolDict" = {
    "type": "function",
    "function": {
        "name": "record_verdict",
        "description": "记录本次工具调用的权限裁决。",
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["allow", "ask", "deny"],
                    "description": "权限裁决：allow 放行 / ask 交人工确认 / deny 拒绝并让 agent 重试。",
                },
                "reason": {
                    "type": "string",
                    "description": "一句话说明裁决理由；deny 时点明风险点。",
                },
            },
            "required": ["decision", "reason"],
        },
    },
}


def _format_judge_request(tool_name: str, tool_input: dict[str, Any], workdir: str) -> str:
    """构造判官的用户消息：工作目录 + 工具名 + 参数摘要。

    Args:
        tool_name: 被裁决的工具名。
        tool_input: 工具调用参数。
        workdir: 当前工作目录。

    Returns:
        供判官阅读的自然语言请求文本。
    """
    if tool_name == "shell":
        detail = f"命令：\n{tool_input.get('command', '')}"
    else:
        try:
            detail = "参数：\n" + json.dumps(tool_input, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            detail = f"参数：\n{tool_input!r}"
    return (
        f"工作目录：{workdir}\n工具：{tool_name}\n{detail}\n\n"
        "请判断该操作能否在 auto 模式下自动放行，并调用 record_verdict 返回裁决。"
    )


def _parse_verdict(response: Any) -> PermissionVerdict:
    """从 LLM 响应的强制工具调用解析结构化裁决，解析失败回落 ask。

    Args:
        response: provider.chat 返回的 LLMResponse。

    Returns:
        解析出的 PermissionVerdict；未取到有效 record_verdict 时回落 ask。
    """
    tool_calls = getattr(response, "tool_calls", None) or {}
    for call in tool_calls.values():
        if call.get("name") != "record_verdict":
            continue
        try:
            args = json.loads(call.get("arguments") or "{}")
        except (TypeError, ValueError):
            continue
        decision = args.get("decision")
        if decision in ("allow", "ask", "deny"):
            return PermissionVerdict(decision, args.get("reason") or "")
    return PermissionVerdict("ask", "判官未返回有效裁决，回落人工确认")


# ── PermissionManager ─────────────────────────────────────────────────

# 规则字典类型：key 为 tool 名（可含 "*"/"?" 通配符，由 _get_rules 调用期 fnmatch 匹配），value 为该工具的规则列表。
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
        trusted_dirs: tuple[str, ...] = (),
        mcp_mgr: Any = None,
        role_default_mode: PermissionMode | None = None,
    ):
        self.default_mode: PermissionMode = DEFAULT_MODE
        # 激活角色 role.md 声明的默认权限模式；优先级高于 settings.json 的 defaultMode。
        self._role_default_mode = role_default_mode
        self.deny_rules: RulesDict = {}
        self.ask_rules: RulesDict = {}
        self.allow_rules: RulesDict = {}
        self.session_allow: RulesDict = {}
        # mcp_servers.json 声明的 server 级规则（最低优先级层，仅 settings.json 静默时生效）
        self.mcp_deny_rules: RulesDict = {}
        self.mcp_ask_rules: RulesDict = {}
        self.mcp_allow_rules: RulesDict = {}
        self.config_mgr = config_mgr
        self._mcp_mgr = mcp_mgr
        self._workdir = workdir
        self._trusted_dirs = trusted_dirs
        self._check_permissions_fns: dict[str, Callable[[dict[str, Any], PermissionContext], PermissionCheckResult]] = {}
        self._tool_tips: dict[str, str] = {}
        self._tool_kinds: dict[str, str | None] = {}
        self._specifier_args: dict[str, str] = {}
        self._mcp_servers: dict[str, str] = {}  # MCP 工具名 → 所属 server 名

        # auto 模式 LLM 判官：开关、模型、升级阈值、会话缓存与拒绝计数（缺省，_load_config 可覆盖）
        self.judge_enabled: bool = True
        self._judge_model: str = _JUDGE_DEFAULT_MODEL
        self._judge_max_consecutive: int = _JUDGE_DEFAULT_MAX_CONSECUTIVE
        self._judge_max_total: int = _JUDGE_DEFAULT_MAX_TOTAL
        self._judge_cache: dict[tuple[str, str], PermissionVerdict] = {}
        self._judge_consecutive_denials: int = 0
        self._judge_total_denials: int = 0

        self._load_tool_metadata(tools or [])
        self._load_config()

    def _load_tool_metadata(self, tools: Iterable[ToolEntry]) -> None:
        """从工具声明中提取 kind 标志、specifier_arg、check_permissions 和 tips 模板。

        Args:
            tools: 所有已注册的工具列表。
        """
        for tool in tools:
            if tool.permission is None:
                continue
            self._tool_kinds[tool.name] = tool.permission.kind
            if tool.permission.tips:
                self._tool_tips[tool.name] = tool.permission.tips
            if tool.permission.check_permissions is not None:
                self._check_permissions_fns[tool.name] = tool.permission.check_permissions
            if tool.permission.specifier_arg is not None:
                self._specifier_args[tool.name] = tool.permission.specifier_arg
            if tool.permission.mcp_server is not None:
                self._mcp_servers[tool.name] = tool.permission.mcp_server

    def _load_config(self) -> None:
        """从配置文件加载权限规则和默认模式。"""
        permissions = self.config_mgr.get_user_setting("permissions")
        if isinstance(permissions, dict):
            default_mode = permissions.get("defaultMode")
            if isinstance(default_mode, str):
                mode = parse_permission_mode(default_mode)
                if mode is None:
                    logger.warning("忽略无效的 permissions.defaultMode：%r", default_mode)
                else:
                    self.default_mode = mode

            self._parse_rules(permissions.get("deny", []), "deny", self.deny_rules)
            self._parse_rules(permissions.get("ask", []), "ask", self.ask_rules)
            self._parse_rules(permissions.get("allow", []), "allow", self.allow_rules)
            self._load_judge_config(permissions.get("autoJudge"))
            self._load_mcp_server_rules()

        # role.md 的 permissionMode 最后套用，优先级高于 settings.json 的 defaultMode；
        # 不受 settings.json 是否存在 permissions 块影响。
        if self._role_default_mode is not None:
            self.default_mode = self._role_default_mode

    def _load_judge_config(self, cfg: Any) -> None:
        """从 permissions.autoJudge 读取 auto 判官配置，非法或缺失项保留缺省。

        Args:
            cfg: permissions.autoJudge 的值，期望为 dict；非 dict 时不改动任何缺省。
        """
        if not isinstance(cfg, dict):
            return
        enabled = cfg.get("enabled")
        if isinstance(enabled, bool):
            self.judge_enabled = enabled
        model = cfg.get("model")
        if isinstance(model, str) and model:
            self._judge_model = model
        max_consecutive = cfg.get("maxConsecutiveDenials")
        if isinstance(max_consecutive, int) and max_consecutive > 0:
            self._judge_max_consecutive = max_consecutive
        max_total = cfg.get("maxTotalDenials")
        if isinstance(max_total, int) and max_total > 0:
            self._judge_max_total = max_total

    def _load_mcp_server_rules(self) -> None:
        """从 mcp_servers.json 各 server 的 permissions 块加载最低优先级权限规则。

        每个条目是相对该 server 的上游工具名通配（如 "get_*"、"*"），展开为
        mcp__<server>__<entry> 规则；以 "mcp__" 开头的条目按完整工具模式原样使用（逃生口）。
        server 段套用与工具注册一致的名称清洗（_safe_tool_name），保证规则前缀对齐注册名；
        entry 不清洗以保留其中的 "*"/"?" 通配。某 server 声明了权限却无对应已注册工具时告警。
        """
        if self._mcp_mgr is None:
            return
        from src.mgr.mcp_mgr import _safe_tool_name

        known_servers = set(self._mcp_servers.values())
        for server, perms in self._mcp_mgr.server_permissions().items():
            if not isinstance(perms, dict):
                continue
            if server not in known_servers:
                logger.warning("mcp_servers.json 中 server '%s' 声明了 permissions，但未发现其已注册工具", server)
            safe_prefix = _safe_tool_name(f"mcp__{server}__")
            for permission, target in (
                ("deny", self.mcp_deny_rules),
                ("ask", self.mcp_ask_rules),
                ("allow", self.mcp_allow_rules),
            ):
                for entry in perms.get(permission, []):
                    if not isinstance(entry, str):
                        continue
                    tool = entry if entry.startswith("mcp__") else f"{safe_prefix}{entry}"
                    self._add_rule(target, PermissionRule(tool=tool, specifier="*", permission=permission))

    @staticmethod
    def _add_rule(rules: RulesDict, rule: PermissionRule) -> None:
        """将规则添加到按工具名索引的规则字典中（自动去重）。

        Args:
            rules: 目标规则字典。
            rule: 要添加的权限规则。
        """
        existing = rules.setdefault(rule.tool, [])
        if not any(r.tool == rule.tool and r.specifier == rule.specifier for r in existing):
            existing.append(rule)

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

    def reload(self) -> None:
        """重置会话级状态（/clear 时调用）。"""
        self.default_mode = DEFAULT_MODE
        self.session_allow.clear()
        self.deny_rules.clear()
        self.ask_rules.clear()
        self.allow_rules.clear()
        self.mcp_deny_rules.clear()
        self.mcp_ask_rules.clear()
        self.mcp_allow_rules.clear()
        # auto 判官：重置配置为缺省并清空会话缓存与拒绝计数，_load_config 随后按最新配置覆盖
        self.judge_enabled = True
        self._judge_model = _JUDGE_DEFAULT_MODEL
        self._judge_max_consecutive = _JUDGE_DEFAULT_MAX_CONSECUTIVE
        self._judge_max_total = _JUDGE_DEFAULT_MAX_TOTAL
        self._judge_cache.clear()
        self._judge_consecutive_denials = 0
        self._judge_total_denials = 0
        self._load_config()

    # ── 模式提醒（ReminderMgr provider） ─────────────────────────────

    def get_turn_start_reminder(self, mode: PermissionMode | None) -> str:
        """每轮 turn start 由 ReminderMgr 调用，返回当前模式的提醒文本。

        Args:
            mode: 调用方 agent 的权限模式。

        Returns:
            提醒字符串，默认模式或 mode 为 None 时返回空串（ReminderMgr 自动跳过）。
        """
        if mode is None:
            return ""
        return mode.reminder

    @staticmethod
    def _get_rules(rules: RulesDict, tool_name: str) -> list[PermissionRule]:
        """获取匹配指定工具名的规则（精确键 + 通配符键）。

        先取精确键命中的规则，再扫描含 '*'/'?' 的工具名键做 fnmatch 匹配，
        使 'mcp__server__*' 这类按 server 通配的规则生效。精确键结果在前以稳定优先级。

        Args:
            rules: 按工具名索引的规则字典（key 可含 '*'/'?' 通配符）。
            tool_name: 被调用的工具名。

        Returns:
            该工具的规则列表，无匹配时返回空列表。
        """
        matched = list(rules.get(tool_name, []))
        for key, entries in rules.items():
            if ("*" in key or "?" in key) and fnmatch(tool_name, key):
                matched.extend(entries)
        return matched

    def check(self, tool_name: str, tool_input: dict[str, Any], mode: PermissionMode) -> PermissionDecision:
        """权限检查核心流程。

        评估顺序：
        1. deny 规则 → deny
        2. ask 规则 → ask（bypass-immune，任何模式都不跳过）
        3. check_permissions（工具自身安全逻辑）：
           - deny → deny
           - ask + bypass_immune → ask（dontAsk 模式转 deny）
           - ask + 非 bypass_immune → 记录后穿透到 Step 4
           - allow → allow
        4. allow 规则（含 session_allow）→ allow
        4.5. 处理 Step 3 记录的非 bypass_immune ask：
             - BYPASS/AUTO → 穿透到 Step 4.7/5/6
             - DONT_ASK → deny（从不弹窗）
             - 其他模式 → ask
        4.7. mcp_servers.json 的 server 级规则（最低优先级层）：deny → ask → allow，
             仅 settings.json 静默时到达；置于 bypass 前故 BYPASS 模式下其 deny/ask 仍生效。
        5. bypass 模式 → auto_allow
        6. 模式默认策略（按 kind 判断）

        Args:
            tool_name: 被调用的工具名。
            tool_input: 工具调用参数。
            mode: 调用方 agent 的权限模式。

        Returns:
            (decision, reason) 元组，decision 为 allow|deny|ask|auto_allow。
        """
        specifier_value = self._extract_specifier(tool_name, tool_input)

        # Step 1: deny 规则
        for rule in self._get_rules(self.deny_rules, tool_name):
            if rule.matches_specifier(specifier_value):
                return "deny", f"被 deny 规则阻止：{rule}"

        # Step 1.5: 复合命令逐段 deny 检查
        if tool_name == "shell" and specifier_value and self._is_compound_command(specifier_value):
            deny_reason = self._check_compound_against_deny_rules(specifier_value)
            if deny_reason is not None:
                return "deny", deny_reason

        # Step 2: ask 规则
        for rule in self._get_rules(self.ask_rules, tool_name):
            if rule.matches_specifier(specifier_value):
                return "ask", f"被 ask 规则要求确认：{rule}"

        # Step 3: check_permissions（工具自身安全逻辑）
        tool_ask_reason: str | None = None
        check_fn = self._check_permissions_fns.get(tool_name)
        if check_fn is not None:
            ctx = PermissionContext(mode=mode, workdir=self._workdir, tool_name=tool_name, trusted_dirs=self._trusted_dirs, specifier_arg=self._specifier_args.get(tool_name))
            result = check_fn(tool_input, ctx)
            if result.decision == "deny":
                return "deny", result.reason or f"被内置安全检测阻止：{tool_name}"
            if result.decision == "ask":
                if result.bypass_immune:
                    # dontAsk 模式从不弹窗，bypass_immune 的 ask 直接转 deny
                    if mode is DONT_ASK_MODE:
                        return "deny", f"dontAsk 模式拒绝（需安全确认）：{result.reason}"
                    return "ask", result.reason or f"需要用户确认：{tool_name}"
                # 非 bypass_immune：记录原因，穿透到 allow 规则（Step 4）
                tool_ask_reason = result.reason or f"需要用户确认：{tool_name}"
            if result.decision == "allow":
                return "allow", result.reason or f"工具级检查放行：{tool_name}"

        # Step 4: allow 规则（含 session_allow）— 复合命令逐段匹配，简单命令含包装剥离
        if tool_name == "shell" and specifier_value and self._is_compound_command(specifier_value):
            # 复合命令：逐段检查，所有段均被 allow 规则覆盖才放行
            if self._check_compound_against_allow_rules(specifier_value):
                return "allow", "复合命令各段均匹配 allow 规则"
        else:
            # 简单命令：原有匹配逻辑（含包装剥离）
            specifier_candidates = [specifier_value]
            if tool_name == "shell" and specifier_value:
                from src.tools.builtin.shell import strip_safe_wrappers_for_matching
                stripped = strip_safe_wrappers_for_matching(specifier_value)
                if stripped != specifier_value:
                    specifier_candidates.append(stripped)

            for rule in chain(
                self._get_rules(self.allow_rules, tool_name),
                self._get_rules(self.session_allow, tool_name),
            ):
                for candidate in specifier_candidates:
                    if rule.matches_specifier(candidate):
                        return "allow", f"匹配 allow 规则：{rule}"

        # Step 4.5: 处理 Step 3 中记录的非 bypass_immune 的 tool ask
        # 仅 BYPASS 模式跳过（AUTO 不跳过，AUTO 更保守——不确定的操作仍需确认）
        if tool_ask_reason is not None:
            if mode is DONT_ASK_MODE:
                return "deny", f"dontAsk 模式拒绝：{tool_ask_reason}"
            if mode is not BYPASS_MODE:
                return "ask", tool_ask_reason

        # Step 4.7: mcp_servers.json 声明的 server 级规则（最低优先级层，仅 settings.json 静默时到达）。
        # 置于 bypass 之前，使 BYPASS 模式下 mcp 的 deny/ask 仍生效（对齐 bypass「deny 和 ask 规则仍生效」契约）。
        for rule in self._get_rules(self.mcp_deny_rules, tool_name):
            if rule.matches_specifier(specifier_value):
                return "deny", f"被 mcp_servers.json deny 规则阻止：{rule}"
        for rule in self._get_rules(self.mcp_ask_rules, tool_name):
            if rule.matches_specifier(specifier_value):
                return "ask", f"被 mcp_servers.json ask 规则要求确认：{rule}"
        for rule in self._get_rules(self.mcp_allow_rules, tool_name):
            if rule.matches_specifier(specifier_value):
                return "allow", f"匹配 mcp_servers.json allow 规则：{rule}"

        # Step 5: bypass 模式
        if mode is BYPASS_MODE:
            return "auto_allow", f"bypassPermissions 模式自动放行：{tool_name}"

        # Step 6: 模式默认策略
        return self._mode_default(tool_name, mode)

    @staticmethod
    def _is_compound_command(command: str) -> bool:
        """检查 shell 命令是否为复合命令，用于复合命令安全防护。

        Args:
            command: shell 命令字符串。

        Returns:
            True 表示命令含分隔符（;、&&、||、| 等），是复合命令。
        """
        from src.tools.builtin.shell import is_compound_command
        return is_compound_command(command)

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

    def _mode_default(self, tool_name: str, mode: PermissionMode) -> PermissionDecision:
        """按给定模式和工具 kind 返回默认权限决策。

        Args:
            tool_name: 工具名。
            mode: 调用方 agent 的权限模式。

        Returns:
            (decision, reason) 元组。
        """
        kind = self._tool_kinds.get(tool_name)

        # AUTO：只读和文件编辑放行，其余询问（与 BYPASS 区分；安全 shell 命令由 check_shell_permissions 放行）
        if mode is AUTO_MODE:
            if kind in ("readonly", "edit"):
                return "auto_allow", f"auto 模式放行：{tool_name}"
            return "ask", f"{tool_name} 需要用户确认"

        if mode is DONT_ASK_MODE:
            if kind == "readonly":
                return "auto_allow", f"dontAsk 模式放行只读：{tool_name}"
            return "deny", f"dontAsk 模式拒绝：{tool_name}"

        # ACCEPT_EDITS：只读和文件编辑放行，其余询问
        if mode is ACCEPT_EDITS_MODE:
            if kind in ("readonly", "edit"):
                return "auto_allow", f"acceptEdits 模式放行：{tool_name}"
            return "ask", f"{tool_name} 需要用户确认"

        # DEFAULT / PLAN：只读放行，其余询问
        if kind == "readonly":
            return "auto_allow", f"自动放行只读：{tool_name}"
        return "ask", f"{tool_name} 需要用户确认"

    async def resolve_ask(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        deps: Any,
        agent: Any = None,
    ) -> PermissionDecision:
        """通过 UI 提示用户确认权限。

        Args:
            tool_name: 工具名。
            tool_input: 工具调用参数。
            deps: AgentDeps 依赖对象。
            agent: 发起该工具调用的 Agent 实例，用于标注是哪个 agent 请求授权（None 时不标注）。

        Returns:
            (decision, reason) 元组。
        """
        if deps is None or getattr(deps, "event_bus", None) is None:
            return "deny", f"权限需要用户确认，但缺少 event_bus：{tool_name}"

        detail = format_tool_tips(self._tool_tips.get(tool_name), tool_input, tool_name)

        # 计算建议规则，供 UI 展示给用户（复合命令生成多条规则）
        compound_rules = self._build_compound_session_rules(tool_name, tool_input)
        if compound_rules:
            rules_to_apply = compound_rules
        else:
            rules_to_apply = [self._build_session_rule(tool_name, tool_input)]
        suggested_rules = [str(r) for r in rules_to_apply if r.specifier != "*"]

        # MCP 工具：提供"信任整个 server"的 server 级通配规则（mcp__<server>__*）
        server = self._mcp_servers.get(tool_name)
        mcp_server_rule = (
            PermissionRule(tool=f"mcp__{server}__*", specifier="*", permission="allow")
            if server else None
        )

        caller_agent_type, caller_uuid = caller_identity(agent)
        answer = await deps.event_bus.request_permission(
            tool_name=tool_name,
            detail=detail,
            suggested_rules=suggested_rules,
            mcp_server_rule=str(mcp_server_rule) if mcp_server_rule else None,
            caller_agent_type=caller_agent_type,
            caller_uuid=caller_uuid,
        )
        normalized = answer.strip().lower()
        if normalized == "session":
            for rule in rules_to_apply:
                self._add_rule(self.session_allow, rule)
            return "allow", "用户在当前会话中始终允许"
        if normalized == "always":
            for rule in rules_to_apply:
                self._add_rule(self.session_allow, rule)
                self._persist_allow_rule(rule)
            return "allow", "用户始终允许（已保存）"
        if normalized == "session_server" and mcp_server_rule is not None:
            self._add_rule(self.session_allow, mcp_server_rule)
            return "allow", f"用户在当前会话中信任整个 server：{server}"
        if normalized == "always_server" and mcp_server_rule is not None:
            self._add_rule(self.session_allow, mcp_server_rule)
            self._persist_allow_rule(mcp_server_rule)
            return "allow", f"用户始终信任整个 server（已保存）：{server}"
        if normalized in {"y", "yes"}:
            return "allow", "用户已允许"
        return "deny", "用户拒绝了权限请求"

    def _build_session_rule(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> PermissionRule:
        """根据工具调用构建智能 session allow 规则。

        Shell 工具优先生成前缀规则（如 'git commit:*'），减少后续同类命令的 ask 弹窗。
        其他工具保持精确匹配。

        Args:
            tool_name: 工具名。
            tool_input: 工具调用参数。

        Returns:
            构建的 PermissionRule。
        """
        value = self._extract_specifier(tool_name, tool_input)
        if not value:
            return PermissionRule(tool=tool_name, specifier="*", permission="allow")

        if tool_name == "shell":
            from src.tools.builtin.shell import get_simple_command_prefix, get_first_word_prefix
            prefix = get_simple_command_prefix(value)
            if prefix:
                return PermissionRule(tool=tool_name, specifier=f"{prefix}:*", permission="allow")
            first_word = get_first_word_prefix(value)
            if first_word:
                return PermissionRule(tool=tool_name, specifier=f"{first_word}:*", permission="allow")

        return PermissionRule(tool=tool_name, specifier=value, permission="allow")

    def _build_compound_session_rules(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> list[PermissionRule] | None:
        """为复合 shell 命令的每个段构建前缀规则列表。

        仅对 shell 复合命令生效。调用 get_compound_segment_prefixes 拆段提取前缀，
        为每个前缀生成 prefix 规则。无法分解时返回 None，调用方退回单规则路径。

        Args:
            tool_name: 工具名。
            tool_input: 工具调用参数。

        Returns:
            前缀规则列表，非 shell / 非复合 / 无法分解时返回 None。
        """
        if tool_name != "shell":
            return None
        value = self._extract_specifier(tool_name, tool_input)
        if not value:
            return None
        from src.tools.builtin.shell import get_compound_segment_prefixes
        prefixes = get_compound_segment_prefixes(value)
        if prefixes is None:
            return None
        return [
            PermissionRule(tool="shell", specifier=f"{prefix}:*", permission="allow")
            for prefix in prefixes
        ]

    def _check_compound_against_allow_rules(self, command: str) -> bool:
        """逐段检查复合命令是否被 allow 规则完全覆盖。

        将复合命令拆分为段，每段独立匹配 allow_rules + session_allow 中的规则。
        每段应用 strip_safe_wrappers_for_matching 生成额外候选。
        所有段均被至少一条规则匹配时返回 True。

        Args:
            command: 完整 shell 命令字符串。

        Returns:
            True 表示所有段均被 allow 规则覆盖。
        """
        from src.tools.builtin.shell import (
            _split_unquoted_newlines, _shell_tokens, _shell_segments,
            strip_safe_wrappers_for_matching,
        )
        try:
            segments: list[list[str]] = []
            for part in _split_unquoted_newlines(command):
                segments.extend(_shell_segments(_shell_tokens(part)))
        except ValueError:
            return False

        all_rules = list(chain(
            self._get_rules(self.allow_rules, "shell"),
            self._get_rules(self.session_allow, "shell"),
        ))

        for segment in segments:
            if segment == ["|"]:
                continue
            seg_str = " ".join(segment)
            candidates = [seg_str]
            stripped = strip_safe_wrappers_for_matching(seg_str)
            if stripped != seg_str:
                candidates.append(stripped)
            matched = False
            for rule in all_rules:
                for candidate in candidates:
                    if rule.matches_specifier(candidate):
                        matched = True
                        break
                if matched:
                    break
            if not matched:
                return False
        return True

    def _check_compound_against_deny_rules(self, command: str) -> str | None:
        """逐段检查复合命令是否被 deny 规则命中。

        将复合命令拆分为段，每段独立匹配 deny_rules。
        任一段被命中即返回拒绝原因。

        Args:
            command: 完整 shell 命令字符串。

        Returns:
            拒绝原因字符串，无命中时返回 None。
        """
        from src.tools.builtin.shell import (
            _split_unquoted_newlines, _shell_tokens, _shell_segments,
        )
        try:
            segments: list[list[str]] = []
            for part in _split_unquoted_newlines(command):
                segments.extend(_shell_segments(_shell_tokens(part)))
        except ValueError:
            return None

        deny_rules = self._get_rules(self.deny_rules, "shell")
        if not deny_rules:
            return None

        for segment in segments:
            if segment == ["|"]:
                continue
            seg_str = " ".join(segment)
            for rule in deny_rules:
                if rule.matches_specifier(seg_str):
                    return f"复合命令中的 '{seg_str}' 被 deny 规则阻止：{rule}"
        return None

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
        agent: Any = None,
    ) -> None:
        """向 UI 发送权限决策通知。

        Args:
            tool_name: 工具名。
            tool_input: 工具调用参数。
            deps: AgentDeps 依赖对象。
            decision: 权限决策（allow|deny|auto_allow）。
            agent: 发起该工具调用的 Agent 实例，用于标注是哪个 agent 的决策（None 时不标注）。
        """
        event_bus = getattr(deps, "event_bus", None) if deps is not None else None
        if event_bus is None:
            return

        if decision == "allow":
            return

        detail = format_tool_tips(self._tool_tips.get(tool_name), tool_input, tool_name)
        caller_agent_type, caller_uuid = caller_identity(agent)
        await event_bus.notify_permission(
            status=decision,
            tool_name=tool_name,
            detail=detail,
            caller_agent_type=caller_agent_type,
            caller_uuid=caller_uuid,
        )

    async def auto_judge(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        mode: PermissionMode,
        deps: Any,
        agent: Any = None,
    ) -> PermissionDecision:
        """auto 模式下对 check() 判为 ask 的模糊操作调用 LLM 判官，返回 (decision, reason)。

        仅由 execute 在 AUTO 模式、check() 判 ask、非 hook 触发 ask 时调用。处理顺序：
        安全关键路径写入直接交人工（不调 LLM，防自我提权）→ 升级阈值（连续/累计拒绝）达标转人工
        → 会话缓存命中直接复用 → 否则调判官并写缓存 → 按裁决更新拒绝计数。判官出错/不可用/
        无效裁决一律回落人工确认（绝不 error→allow）。

        Args:
            tool_name: 被裁决的工具名。
            tool_input: 工具调用参数。
            mode: 调用方 agent 的权限模式（预期为 AUTO_MODE，由调用方保证）。
            deps: AgentDeps 依赖对象，提供 llm_mgr。
            agent: 发起该工具调用的 Agent 实例，用于 LLM 事件的 caller 标注。

        Returns:
            (decision, reason) 元组，decision 为 allow|ask|deny。
        """
        # 安全关键自守：命中即人工确认，绝不交 LLM 判官
        hit = self._judge_security_critical_hit(tool_name, tool_input)
        if hit is not None:
            return "ask", f"安全关键路径需人工确认：{hit}"

        # 升级：累计拒绝达上限为会话级硬底线（不重置）；连续拒绝达阈值升级一次后清零，交人工后判官恢复
        if self._judge_total_denials >= self._judge_max_total:
            return "ask", "判官累计拒绝已达上限，转人工确认"
        if self._judge_consecutive_denials >= self._judge_max_consecutive:
            self._judge_consecutive_denials = 0
            return "ask", "判官连续拒绝已达阈值，转人工确认"

        # 会话缓存命中直接复用，否则调判官并写缓存
        key = self._judge_cache_key(tool_name, tool_input)
        verdict = self._judge_cache.get(key)
        if verdict is None:
            try:
                verdict = await self._call_judge(tool_name, tool_input, deps, agent)
            except Exception as exc:  # 判官不可用一律回落人工，绝不误放行
                logger.warning("auto 判官调用失败，回落人工确认：%s", exc)
                return "ask", "判官不可用，回落人工确认"
            self._judge_cache[key] = verdict

        # 拒绝计数：deny 累加连续与累计；allow 清零连续计数
        if verdict.decision == "deny":
            self._judge_consecutive_denials += 1
            self._judge_total_denials += 1
        elif verdict.decision == "allow":
            self._judge_consecutive_denials = 0

        return verdict.decision, verdict.reason

    def _judge_security_critical_hit(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        """检测工具目标是否触及安全关键路径，供判官自守（命中则不调 LLM 直接交人工）。

        Args:
            tool_name: 被裁决的工具名。
            tool_input: 工具调用参数。

        Returns:
            命中的安全关键路径字符串，未命中时返回 None。
        """
        from src.tools.builtin.file import is_security_critical_path

        if tool_name == "shell":
            from src.tools.builtin.shell import _check_security_critical_paths

            command = tool_input.get("command")
            if isinstance(command, str):
                return _check_security_critical_paths(command, self._workdir)
            return None
        for key in ("path", "file_path", "source", "destination"):
            value = tool_input.get(key)
            if isinstance(value, str) and is_security_critical_path(value):
                return value
        return None

    def _judge_cache_key(self, tool_name: str, tool_input: dict[str, Any]) -> tuple[str, str]:
        """构造判官会话缓存键：(工具名, 规范化参数)。

        优先用 specifier（如 shell 的完整命令），否则用参数的稳定 JSON 序列化。

        Args:
            tool_name: 被裁决的工具名。
            tool_input: 工具调用参数。

        Returns:
            (tool_name, 规范化参数) 缓存键。
        """
        specifier = self._extract_specifier(tool_name, tool_input)
        if specifier:
            return (tool_name, specifier)
        try:
            return (tool_name, json.dumps(tool_input, sort_keys=True, ensure_ascii=False))
        except (TypeError, ValueError):
            return (tool_name, repr(tool_input))

    async def _call_judge(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        deps: Any,
        agent: Any,
    ) -> PermissionVerdict:
        """用快模型调用判官并解析结构化裁决（强制 record_verdict 工具调用）。

        Args:
            tool_name: 被裁决的工具名。
            tool_input: 工具调用参数。
            deps: AgentDeps 依赖对象，提供 llm_mgr。
            agent: 发起该工具调用的 Agent 实例，用于 LLM 事件的 caller 标注。

        Returns:
            解析出的 PermissionVerdict。

        Raises:
            RuntimeError: 缺少 llm_mgr 时。
            其它异常由底层 LLM 调用抛出，交由 auto_judge 兜底回落人工。
        """
        llm_mgr = getattr(deps, "llm_mgr", None) if deps is not None else None
        if llm_mgr is None:
            raise RuntimeError("缺少 llm_mgr，无法调用 auto 判官")
        provider = llm_mgr.get(self._judge_model)
        caller_agent_type, caller_uuid = caller_identity(agent)
        response = await provider.chat(
            messages=[{"role": "user", "content": _format_judge_request(tool_name, tool_input, self._workdir)}],
            prompt=[{"role": "system", "content": _JUDGE_SYSTEM_PROMPT}],
            tools=[_JUDGE_VERDICT_TOOL],
            tool_choice={"type": "function", "function": {"name": "record_verdict"}},
            temperature=0.0,
            enable_thinking=False,
            reasoning_effort_override="low",
            caller_agent_type=caller_agent_type,
            caller_uuid=caller_uuid,
        )
        return _parse_verdict(response)
