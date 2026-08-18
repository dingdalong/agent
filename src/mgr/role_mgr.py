"""角色管理器 — 发现、解析、暴露当前激活角色的路径。

三层发现（低→高）：内置 src/roles/ → 全局 ~/.agent/roles/ → 项目 .agent/roles/。
激活角色由 config.yaml 的 role.default 键指定，缺省回退 coding。
角色定义文件为 role.md（YAML frontmatter + body），与子 agent 的 *.md 同格式。
提供 frontmatter 解析与字段提取工具函数（共用）。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from src.mgr.paths import builtin_root, common_role_dir

if TYPE_CHECKING:
    from src.mgr.config_mgr import ConfigManager

logger = logging.getLogger(__name__)

# 缺省角色名 — 当 config 未指定或指定的角色不存在时回退到此角色。
DEFAULT_ROLE = "coding"

_RESERVED_ROLE_NAMES = frozenset({"common", "default"})


def _valid_role_name(name: str) -> bool:
    """判断角色目录名是否与框架保留结构冲突。

    Args:
        name: 角色目录名。

    Returns:
        非空字符串且不是保留名时为 True，否则为 False。
    """
    return isinstance(name, str) and bool(name) and name not in _RESERVED_ROLE_NAMES


def format_role_config_key(role_name: str, *suffix: str) -> str:
    """格式化不受角色名中点号影响的配置诊断路径。"""
    quoted_name = json.dumps(role_name, ensure_ascii=False)
    tail = "".join(f".{part}" for part in suffix)
    return f"role[{quoted_name}]{tail}"


def role_model_yaml_example(
    role_name: str,
    default_model: str = "<模型ID>",
    fast_model: str = "<模型ID>",
) -> str:
    """生成以真实角色名为 mapping key 的单行可粘贴 YAML。"""
    return yaml.safe_dump(
        {
            "role": {
                role_name: {
                    "model": {
                        "default": default_model,
                        "fast": fast_model,
                    }
                }
            }
        },
        allow_unicode=True,
        default_flow_style=True,
        sort_keys=False,
    ).strip()


def discover_roles(
    workdir: Path,
    global_dir: Path | None,
    project_trusted: bool,
) -> dict[str, Path]:
    """按内置、全局、可信项目的优先级发现可激活角色。

    Args:
        workdir: 用户工作目录。
        global_dir: 全局配置目录；为 None 时跳过全局层。
        project_trusted: 是否允许读取项目层角色。

    Returns:
        角色名到角色目录的映射；同名后层覆盖前层，保留名不包含在内。
    """
    scan_dirs: list[Path] = [builtin_root() / "roles"]
    if global_dir:
        scan_dirs.append(global_dir / "roles")
    if project_trusted:
        scan_dirs.append(workdir / ".agent" / "roles")

    roles: dict[str, Path] = {}
    for directory in scan_dirs:
        if not directory.exists():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_dir() or not (path / "role.md").exists():
                continue
            if not _valid_role_name(path.name):
                logger.warning("角色目录名与保留结构冲突，已忽略：%s", path)
                continue
            roles[path.name] = path
    return roles


def active_role_name(config_mgr: ConfigManager) -> str:
    """读取并规范化配置中的 ``role.default``。

    Args:
        config_mgr: 配置管理器。

    Returns:
        配置角色名；配置缺失、非字符串或空值时返回 DEFAULT_ROLE。
    """
    try:
        val = config_mgr.get_config("role.default")
    except KeyError:
        return DEFAULT_ROLE
    if isinstance(val, str) and val.strip():
        return val.strip()
    return DEFAULT_ROLE


def resolve_role_name(role_name: str, roles: Mapping[str, Path]) -> str:
    """根据已发现角色解析有效角色名。

    Args:
        role_name: 规范化后的 ``role.default`` 角色名。
        roles: 已发现的角色名到路径映射。

    Returns:
        已发现时返回原角色名，否则返回 DEFAULT_ROLE。
    """
    return role_name if role_name in roles else DEFAULT_ROLE


# ── frontmatter 解析（RoleMgr / SubAgentMgr 共用）────────────────────


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """从 .md 文本中分离 YAML frontmatter 和 body。

    Args:
        text: .md 文件全文。

    Returns:
        (frontmatter_dict, body_text)。无 frontmatter 时 dict 为空，body 为全文。
    """
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
    if not match:
        return {}, text
    meta = yaml.safe_load(match.group(1)) or {}
    return meta, match.group(2)


def extract_manifest(
    meta: dict,
    path: Path,
    *,
    prompt: str = "",
    id_field: str = "agent_type",
    default_id: str = "",
    default_description: str = "没有说明内容",
) -> AgentManifest:
    """从 frontmatter dict + prompt body 构造 AgentManifest。

    差异点通过参数注入消除：
    - prompt: markdown body（parse_frontmatter 返回的第二项）
    - id_field: 标识字段名（SubAgentMgr 用 "agent_type"，RoleMgr 用 "agent_type" 固定为 "main"）
    - default_id: 标识字段缺省值（SubAgentMgr 用 path.stem，RoleMgr 用 path.name）
    - default_description: 描述缺省值

    Args:
        meta: frontmatter 解析后的 dict。
        path: 定义文件路径（用于日志）。
        prompt: markdown body 文本。
        id_field: 用作标识的字段名。
        default_id: 标识字段缺失时的回退值。
        default_description: 描述缺失时的回退值。

    Returns:
        AgentManifest 实例。
    """
    from src.llm.base import normalize_reasoning_effort

    # 标识
    identifier = meta.get(id_field)
    if not isinstance(identifier, str) or not identifier.strip():
        identifier = default_id

    # 描述
    description = meta.get("description", default_description)
    if not isinstance(description, str):
        description = default_description

    # 工具白名单：逗号分割，空 → None（全部工具可用）
    raw_tools = meta.get("tools", "")
    tools: set[str] | None = None
    if raw_tools and isinstance(raw_tools, str):
        parsed = {t.strip() for t in raw_tools.split(",") if t.strip()}
        if parsed:
            tools = parsed

    # 模型
    model: str | None = None
    raw_model = meta.get("model")
    if isinstance(raw_model, str) and raw_model.strip():
        model = raw_model.strip()

    start_in_plan_mode = meta.get("startInPlanMode", False)
    if not isinstance(start_in_plan_mode, bool):
        logger.warning("%s 的 startInPlanMode 必须是 bool，已使用 false", path)
        start_in_plan_mode = False

    # 思考模式：仅 bool 有效，非 bool 静默忽略
    enable_thinking: bool | None = None
    raw_thinking = meta.get("thinking")
    if isinstance(raw_thinking, bool):
        enable_thinking = raw_thinking

    # 推理力度：合法档位覆盖 provider 配置，非法告警忽略
    reasoning_effort: str | None = None
    raw_effort = meta.get("reasoning_effort")
    if raw_effort is not None:
        reasoning_effort = normalize_reasoning_effort(str(raw_effort))
        if reasoning_effort is None:
            logger.warning(
                "%s 的 reasoning_effort 非法：%r，已忽略",
                path, raw_effort,
            )

    # 记忆范围
    memory: str | None = None
    raw_memory = meta.get("memory")
    if isinstance(raw_memory, str) and raw_memory.strip():
        memory = raw_memory.strip()

    # 可插拔 feature 集：YAML 列表 → set（空列表 → 空 set，全部禁用）；
    # 无该键 → None（未声明，继承/默认全开）；非列表 → 告警忽略。
    features: set[str] | None = None
    raw_features = meta.get("features")
    if isinstance(raw_features, list):
        features = {str(f).strip() for f in raw_features if str(f).strip()}
    elif raw_features is not None:
        logger.warning("%s 的 features 应为列表，实际为 %r，已忽略", path, raw_features)

    return AgentManifest(
        agent_type=identifier,
        description=description,
        path=path,
        prompt=prompt.strip() or None,
        tools=tools,
        model=model,
        start_in_plan_mode=start_in_plan_mode,
        enable_thinking=enable_thinking,
        reasoning_effort=reasoning_effort,
        memory=memory,
        features=features,
    )


@dataclass
class AgentManifest:
    """从 role.md / agents/*.md 解析出的完整定义。

    包含 frontmatter 字段 + markdown body（prompt）。
    agent_type — 角色固定为 ``"main"``；子 agent 取自 agents/*.md 的 YAML key ``agent_type``。
    与 Agent.agent_type 一致。
    """

    agent_type: str
    description: str
    path: Path
    prompt: str | None = None
    tools: set[str] | None = None
    memory: str | None = None
    model: str | None = None
    start_in_plan_mode: bool = False
    enable_thinking: bool | None = None
    reasoning_effort: str | None = None
    features: set[str] | None = None


@dataclass
class RoleMgr:
    """角色管理器。

    Args:
        config_mgr: 配置管理器，用于读取 role.default 及角色覆盖配置。
        workdir: 用户工作目录。
        global_dir: 全局配置目录（~/.agent/），为 None 时跳过全局层。
    """

    config_mgr: ConfigManager
    workdir: Path
    global_dir: Path | None = None

    _role_path: Path | None = field(init=False, default=None)
    _manifest: AgentManifest | None = field(init=False, default=None)
    _all_roles: dict[str, Path] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self._discover()
        self._resolve()

    def reload(self) -> None:
        self._all_roles.clear()
        self._discover()
        self._resolve()

    # —— 发现 ————————————————————————————————————————————————————————

    def _discover(self) -> None:
        """通过共享发现逻辑刷新所有已安装角色。"""
        self._all_roles.update(
            discover_roles(
                self.workdir,
                self.global_dir,
                self.config_mgr.project_trusted,
            )
        )

    # —— 解析 ————————————————————————————————————————————————————————

    def _resolve(self) -> None:
        """从配置读取角色名，定位角色目录并解析 role.md。

        若配置不存在或无值 → 回退 DEFAULT_ROLE。
        若指定角色在 _all_roles 中不存在 → warning + 回退。
        """
        configured_role = active_role_name(self.config_mgr)
        role_name = resolve_role_name(configured_role, self._all_roles)
        if configured_role != role_name:
            logger.warning(
                "角色 '%s' 未找到，回退到 '%s'。可用角色：%s",
                configured_role,
                DEFAULT_ROLE,
                ", ".join(sorted(self._all_roles)) or "(无)",
            )
        path = self._all_roles.get(role_name)

        if path is None:
            logger.warning("默认角色 '%s' 也未找到，无角色激活。", DEFAULT_ROLE)
            self._role_path = None
            self._manifest = None
            return

        self._role_path = path
        role_md_path = path / "role.md"
        if role_md_path.exists():
            try:
                meta, prompt = parse_frontmatter(role_md_path.read_text())
            except Exception as exc:
                logger.warning("角色定义 %s 解析失败：%s", role_md_path, exc)
                meta, prompt = {}, ""
            self._reject_manifest_model(meta, role_md_path, path.name)
            self._manifest = extract_manifest(
                meta, role_md_path,
                prompt=prompt,
                id_field="agent_type",
                default_id="main",
                default_description="",
            )
            # 主 agent 恒用 default 槽位，角色 manifest 不再承载模型名。
            self._manifest.model = None
            self._apply_config_overrides(path.name)
        else:
            self._manifest = None

        logger.info("激活角色：%s（%s）", self.role_name, path)

    @staticmethod
    def _reject_manifest_model(meta: dict, role_md_path: Path, role_name: str) -> None:
        """role.md 残留 model 字段时报错，避免模型配置被静默忽略。

        Args:
            meta: role.md 的 frontmatter 解析结果。
            role_md_path: role.md 文件路径（写入报错消息）。
            role_name: 角色名（用于提示正确的配置键）。

        Raises:
            LLMConfigurationError: frontmatter 中存在 model 键时。
        """
        if "model" not in meta:
            return

        from src.llm.errors import LLMConfigurationError

        raise LLMConfigurationError(
            f"角色定义 {role_md_path} 的 model 字段已废弃：主 agent 恒用 default 槽位，"
            f"模型改由配置 {format_role_config_key(role_name, 'model', 'default')} 与 "
            f"{format_role_config_key(role_name, 'model', 'fast')} 控制，请删除该字段。"
            "当前阶段模型发现尚未执行。"
        )

    def _apply_config_overrides(self, role_name: str) -> None:
        """将活动角色的推理力度配置覆盖到已解析 manifest。"""
        if self._manifest is None:
            return

        try:
            raw_effort = self.config_mgr.get_config_parts(
                ("role", role_name, "reasoning_effort")
            )
        except KeyError:
            raw_effort = None
        if raw_effort is None:
            return

        from src.llm.base import normalize_reasoning_effort

        reasoning_effort = normalize_reasoning_effort(str(raw_effort))
        if reasoning_effort is None:
            effort_key = format_role_config_key(role_name, "reasoning_effort")
            logger.warning(
                "角色配置 %s 非法：%r，已忽略",
                effort_key,
                raw_effort,
            )
            return
        self._manifest.reasoning_effort = reasoning_effort

    # —— 查询 ————————————————————————————————————————————————————————

    @property
    def active(self) -> bool:
        """是否有已激活的角色。"""
        return self._manifest is not None

    @property
    def manifest(self) -> AgentManifest | None:
        """当前角色的 AgentManifest。"""
        return self._manifest

    @property
    def role_name(self) -> str | None:
        """当前角色名（即角色文件夹名）。"""
        if self._role_path is not None:
            return self._role_path.name
        return None

    # —— 资产路径（无角色时返回 None）——————————————————————————————————

    def _make_path(self, sub: str) -> Path | None:
        """构造角色子目录路径，仅目录存在时返回。

        Args:
            sub: 角色目录内的相对子路径。

        Returns:
            Path 或 None。
        """
        if not self._role_path:
            return None
        p = self._role_path / sub
        return p if p.exists() else None

    def agents_dir(self) -> Path | None:
        """角色子 agent 定义目录（*.md）。"""
        return self._make_path("agents")

    def skills_dir(self) -> Path | None:
        """角色技能目录（*/SKILL.md）。"""
        return self._make_path("skills")

    def plugins_dir(self) -> Path | None:
        """角色插件目录。"""
        return self._make_path("plugins")

    def agent_md_path(self) -> Path | None:
        """激活角色共享 AGENTS.md 文件路径。"""
        if not self._role_path:
            return None
        p = self._role_path / "AGENTS.md"
        return p if p.is_file() else None

    def mcp_servers_path(self) -> Path | None:
        """角色 mcp_servers.json 文件路径。"""
        if not self._role_path:
            return None
        p = self._role_path / "mcp_servers.json"
        return p if p.is_file() else None

    # —— 共享资源路径（不依赖角色激活状态）——————————————————————————————

    def common_dir(self) -> Path | None:
        """共享资源目录（所有角色可用）。"""
        p = common_role_dir()
        return p if p.exists() else None

    def common_agents_dir(self) -> Path | None:
        """共享子 agent 定义目录。"""
        p = common_role_dir() / "agents"
        return p if p.exists() else None

    def common_skills_dir(self) -> Path | None:
        """共享技能目录。"""
        p = common_role_dir() / "skills"
        return p if p.exists() else None

    def common_agent_md_path(self) -> Path | None:
        """跨角色共享 AGENTS.md 文件路径。"""
        p = common_role_dir() / "AGENTS.md"
        return p if p.is_file() else None
