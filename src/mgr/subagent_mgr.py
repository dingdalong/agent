from __future__ import annotations
from typing import Any, TYPE_CHECKING

import time
import logging
from dataclasses import dataclass, field
from pathlib import Path

from src.events.types import SubagentLifecycle
from src.llm.errors import LLMConfigurationError
from src.mgr.llm_mgr import MODEL_ALIASES
from src.llm.models import split_model_reference
from src.mgr.role_mgr import parse_frontmatter, extract_manifest, AgentManifest

if TYPE_CHECKING:
    from src.agent import Agent, AgentDeps

logger = logging.getLogger(__name__)


@dataclass
class SubAgentMgr:
    """子智能体管理器 — 四层扫描：共享 → 角色 → 全局 → 项目，同名覆盖。

    Args:
        workdir: 用户工作目录。
        deps: 外部依赖。
        global_dir: 全局配置目录（~/.agent/）。
    """
    workdir: Path
    deps: AgentDeps = field(repr=False)
    global_dir: Path | None = None

    _documents: dict[str, AgentManifest] = field(init=False, default_factory=dict)

    def __post_init__(self):
        self._load_all()

    def _load_all(self) -> None:
        """扫描四层目录加载子智能体定义，同名后者覆盖前者。

        扫描顺序（低→高优先级）：共享 → 角色 → 全局 → 项目。

        Raises:
            LLMConfigurationError: 任一 manifest 的 model 字段非法。
        """
        project_dir = self.workdir / ".agent" / "agents"

        scan_dirs: list[tuple[Path, str]] = []

        # 共享子 agent（最低优先级，所有角色可用）
        role_mgr = getattr(self.deps, "role_mgr", None)
        if role_mgr is not None:
            cd = role_mgr.common_agents_dir()
            if cd is not None:
                scan_dirs.append((cd, "common"))

        # 角色子 agent（基准层）
        if role_mgr is not None and role_mgr.active:
            sd = role_mgr.agents_dir()
            if sd is not None:
                scan_dirs.append((sd, "role"))
        if self.global_dir:
            scan_dirs.append((self.global_dir / "agents", "global"))
        scan_dirs.append((project_dir, "project"))

        for directory, _source in scan_dirs:
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.md")):
                meta, prompt = parse_frontmatter(path.read_text())
                self._validate_raw_model(meta, path)
                manifest = extract_manifest(meta, path, prompt=prompt)
                self._documents[manifest.agent_type] = manifest

    @staticmethod
    def _validate_raw_model(meta: dict, path: Path) -> None:
        """校验并规范化子 agent frontmatter 中显式声明的 model。

        Args:
            meta: ``parse_frontmatter`` 返回的原始映射。
            path: manifest 文件路径，用于错误定位。

        Returns:
            None；空值会规范为 None，合法字符串会去除首尾空白后写回 ``meta``。

        Raises:
            LLMConfigurationError: model 键存在且值不是字符串或 null。
        """
        if "model" not in meta:
            return
        raw_model = meta["model"]
        if raw_model is None or isinstance(raw_model, str) and not raw_model.strip():
            meta["model"] = None
            return
        if not isinstance(raw_model, str):
            raise LLMConfigurationError(
                f"子 agent 定义 {path} 的 model 非法：{raw_model!r}。"
                "只允许 default、fast、opus、sonnet、haiku 或 供应商/模型ID"
            )
        model = raw_model.strip()
        if model not in MODEL_ALIASES:
            try:
                split_model_reference(model)
            except LLMConfigurationError as exc:
                raise LLMConfigurationError(
                    f"子 agent 定义 {path} 的 model 非法：{model!r}。"
                    "只允许 default、fast、opus、sonnet、haiku 或 供应商/模型ID"
                ) from exc
        meta["model"] = model

    def describe(self) -> str | None:
        if not self._documents:
            return
        lines = []
        for manifest in sorted(self._documents.values(), key=lambda m: m.agent_type):
            lines.append(f"- {manifest.agent_type}: {manifest.description}")
        return "\n".join(lines)

    def prompt_section(self) -> str:
        """返回子智能体列表提示词段，无子智能体时返回空串。"""
        describe = self.describe()
        if not describe:
            return ""
        return (
            "# 子智能体协作\n"
            "只有独立工作能获得并行收益、需要隔离大量中间输出或需要独立核验时才委派；存在匹配子 agent 本身不是委派理由。\n"
            "决定委派后按能力选择专用或通用子 agent。写清目标、范围、必要背景、已确认约束和验收方式。\n"
            "需要专业方法时，在委派正文中给出技能完整名、输入与产物路径，由执行器加载；技能匹配本身不构成委派理由。\n"
            "子 agent 每次从空历史开始，共享摘要不是完整对话；关键输入必须显式传递。独立复核传 shared_context=\"none\"。\n"
            "task_delegator 等待任务结束才返回；同轮独立调用可并行，全部返回后你才能继续推理。并行修改需划分不重叠的写入范围，不重复执行已委派工作。\n"
            "返回后核对实际产物和验证证据，再整合结果；需要澄清时由你与用户沟通，再发起包含完整输入的新委派。\n"
            "已有任务需跟踪时传 task_id：框架自动标记 in_progress 并设置 owner，异常或 LLM 错误回滚为无负责人的 pending；正常返回不自动完成。\n"
            "验收后由你标记 completed；未完成的部分可直接补齐或按上述条件重新委派。任务 ID 通过参数传入，不要求子 agent 管理父任务。\n\n"
            "# 可用子智能体\n" + describe
        )

    async def task_delegator(
        self,
        agent_type: str,
        prompt: str,
        *,
        parent_agent: Any = None,
        task_id: str | None = None,
        description: str = "",
        shared_context: str = "auto",
    ) -> str:
        """委派任务给子智能体并返回执行结果。

        若指定 task_id，委派前自动将任务标记为 in_progress 并设置 owner；
        子智能体异常退出时自动回滚为 pending。正常返回时不标 completed，
        留给主 agent 评估结果后决定。

        本方法同时是**跨 agent 上下文交接的唯一枢纽**：委派前把共享账本摘要拼到
        prompt 前面，委派后把子智能体的返回报告自动记账供后续委派复用。之所以放在
        这里而不是 ReminderMgr，是因为 ReminderMgr 的 provider 只收
        `(plan_active, is_subagent)`，拿不到本次委派信息，按委派过滤就得在进程级
        单例上存槽位——而计划工作流允许同一轮并行委派多个 explore，`asyncio.gather`
        会互相覆盖那个槽位（共享消费槽位已经
        踩过同一个坑）。本方法的局部变量天然 per-delegation、并发安全。

        Args:
            agent_type: 目标子智能体类型标识。
            prompt: 传给子智能体的完整任务正文。
            parent_agent: 调用方 Agent 实例，用于管理父任务状态和触发 hooks。
            task_id: 关联的任务 ID（可选），指定后框架自动管理任务状态。
            description: 委派的任务摘要，传入生命周期事件供 UI 展示，
                并作为共享上下文条目的标题。
            shared_context: "auto" 注入已积累的共享上下文；"none" 完全隔离，
                用于需要独立复核、避免先前结论影响判断的场景。

        Returns:
            子智能体的执行结果文本，或错误信息。
        """
        manifest = self._documents.get(agent_type)
        if not manifest:
            known = ", ".join(sorted(self._documents)) or "(none)"
            return f"错误: 不存在的子智能体：'{agent_type}'。可用子智能体列表：{known}"

        # —— 自动标记任务为 in_progress ——
        task_mgr = getattr(parent_agent, '_task_mgr', None) if parent_agent else None
        task_rolled_back = False

        def _rollback_task() -> None:
            """把关联任务恢复为无负责人的 pending 状态。

            Returns:
                None。
            """
            nonlocal task_rolled_back
            if task_rolled_back or not task_id or not task_mgr:
                return
            task_rolled_back = True
            try:
                task_mgr.update(task_id, status="pending", owner="")
            except ValueError:
                pass

        if task_id and task_mgr:
            try:
                task_mgr.update(task_id, status="in_progress", owner=agent_type)
            except ValueError:
                pass

        event_bus = getattr(self.deps, "event_bus", None)
        context_mgr = getattr(self.deps, "context_mgr", None)
        agent: Any = None
        primary_error: BaseException | None = None
        # 提到 try 之外初始化：异常/取消路径下也要能在记账处判断"这次委派没有正常完成"
        run_result: Any = None

        try:
            # 解析子 agent 的最终工具集（自动注入 subagent=True、排除 subagent=False）
            tools = self.deps.tools_mgr.resolve_subagent_tools(manifest.tools)

            # 模型：加载期已校验，None 表示走激活角色的 default 槽位
            model_value = manifest.model

            # 思考模式：显式设置则用设置值，否则继承父 agent
            enable_thinking = manifest.enable_thinking
            if enable_thinking is None:
                enable_thinking = getattr(parent_agent, "enable_thinking", True)

            # 推理力度：显式设置则用设置值，否则继承父 agent 已解析值
            reasoning_effort = manifest.reasoning_effort
            if reasoning_effort is None:
                reasoning_effort = getattr(parent_agent, "reasoning_effort", None)
                if reasoning_effort is None:
                    parent_llm = getattr(parent_agent, "llm", None)
                    reasoning_effort = getattr(parent_llm, "reasoning_effort", None)

            # feature 集：子 agent 自身 manifest 声明则用其值，否则继承父 agent 已解析的 feature 集
            features = manifest.features
            if features is None:
                features = getattr(parent_agent, "features", None)

            from src.agent import Agent
            agent = Agent.from_manifest(
                manifest=manifest,
                deps=self.deps,
                is_subagent=True,
                tools=tools,
                model=model_value,
                enable_thinking=enable_thinking,
                reasoning_effort=reasoning_effort,
                features=features,
                plan_active=bool(getattr(parent_agent, "plan_active", False)),
            )

            hooks_mgr = self.deps.hooks_mgr
            fire_hooks = hooks_mgr is not None and parent_agent is not None
            hook_kwargs = {}
            if fire_hooks:
                hook_kwargs = {
                    "session_id": self.deps.session_id,
                    "agent_id": str(getattr(parent_agent, "uuid", "")),
                    "agent_type": getattr(parent_agent, "agent_type", ""),
                }
                await hooks_mgr.run_event(
                    "SubagentStart",
                    agent_type,
                    {"subagent_type": agent_type, "subagent_id": str(agent.uuid), "prompt": prompt},
                    **hook_kwargs,
                )

            # 发射子 agent 生命周期开始事件
            if event_bus is not None:
                await event_bus.emit(SubagentLifecycle(
                    timestamp=time.time(),
                    source="subagent_mgr",
                    agent_uuid=str(agent.uuid),
                    agent_type=agent_type,
                    phase="start",
                    task=description,
                ))

            # —— 注入共享上下文 ——
            # 必须在 run() 之前：run() 内部会 redact 并 prepend turn-start reminder。
            # 摘要在前、任务正文在最后（recency）——参考材料靠前、指令靠后，同时降低
            # 子 agent 把背景事实误当成任务的概率（摘要头部另有显式声明）。
            # 只拼字符串、不进 system prompt：system 带着 Anthropic 的单一缓存断点，
            # 动态内容进去会让 tools+system 整个前缀每次委派全部失效。
            if context_mgr is not None and shared_context != "none":
                digest = context_mgr.digest()
                if digest:
                    prompt = f"{digest}\n\n{prompt}"

            run_result = await agent.run(prompt)
            result = run_result.final_text
            if run_result.llm_error is not None:
                _rollback_task()
        except BaseException as exc:
            # —— 子智能体异常退出，回滚任务状态 ——
            primary_error = exc
            _rollback_task()
            raise
        finally:
            if agent is not None and getattr(self.deps, "process_mgr", None):
                await self.deps.process_mgr.close((self.deps.session_id, str(agent.uuid)))
            # 发射子 agent 生命周期结束事件（异常/取消也发）；携完整原始消息记录供 /agents 回看。
            # list(...) 浅拷贝快照：结束后 history 内各 dict 不再被改写。异常/取消路径捕获到当时的部分 history。
            if agent is not None and event_bus is not None:
                try:
                    await event_bus.emit(SubagentLifecycle(
                        timestamp=time.time(),
                        source="subagent_mgr",
                        agent_uuid=str(agent.uuid),
                        agent_type=agent_type,
                        phase="end",
                        task=description,
                        messages=list(agent.history),
                    ))
                except BaseException:
                    if primary_error is None:
                        _rollback_task()
                        raise

        try:
            if fire_hooks:
                stop_result = await hooks_mgr.run_event(
                    "SubagentStop",
                    agent_type,
                    {"subagent_type": agent_type, "subagent_id": str(agent.uuid), "result": result},
                    **hook_kwargs,
                )
                if stop_result.blocked:
                    result = stop_result.block_reason or result
                elif stop_result.additional_context:
                    result = result + "\n\n" + "\n\n".join(str(c) for c in stop_result.additional_context)
        except BaseException:
            _rollback_task()
            raise

        # —— 自动记账 ——
        # 记的是父 agent 实际收到的 result（在 SubagentStop hook 可能改写之后），
        # 保证账本与主 agent 看到的内容一致。异常/取消路径走不到这里，天然不记账，
        # 与 _rollback_task() 的语义保持一致。
        if (
            context_mgr is not None
            and run_result is not None
            and run_result.llm_error is None
            and agent_type in context_mgr.record_types
            and isinstance(result, str)
        ):
            await context_mgr.record(
                kind="delegation",
                topic=description or agent_type,
                content=result,
                author=agent_type,
            )

        return result
