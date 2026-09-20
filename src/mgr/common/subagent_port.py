"""子 agent 委派端口 — 把「调度」与「构造并运行子 Agent」解耦。

`SubAgentMgr` 只依赖本模块声明的数据与协议：

* 父 agent 的委派事实由 `ParentAgentFacts` 显式提供，管理器不再回头
  读取调用方的私有字段（`enable_thinking`、`features`、`llm`…），
  因此 `src/mgr/**` 运行期不需要 import `src.agent`；
* 「构造并运行子 agent」的能力由调用方以 `SubagentExecutor` 注入，
  管理器只负责查表、任务状态、hook/事件与共享上下文记账。

`Agent` 是这些协议的唯一生产实现（`Agent.spawn_subagent` /
`Agent.delegate_subagent`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from src.llm.errors import LLMErrorInfo
from src.mode import RunMode

if TYPE_CHECKING:
    from src.mgr.role_mgr import AgentManifest


class TaskStatusWriter(Protocol):
    """委派路径需要的最小任务状态写入面。"""

    def update(self, task_id: str, *, status: str, owner: str) -> None:
        """更新任务状态与负责人。"""
        ...


@dataclass(frozen=True)
class ParentAgentFacts:
    """父 agent 传给委派路径的事实快照。

    Attributes:
        uuid: 父 agent 实例标识，用于 hook 与会话级进程清理。
        agent_type: 父 agent 类型标识。
        mode: 父 agent 当前运行模式，子 agent 继承。
        features: 父 agent 已解析的有效 feature 集；子 agent 未声明时继承。
        enable_thinking: 父 agent 声明是否启用思考；子 agent 未声明时继承。
        reasoning_effort: 父 agent 已解析的推理强度覆盖。
        provider_reasoning_effort: 父 agent provider 的默认推理强度，作为末级回退。
        task_writer: 父 agent 的任务管理器；未启用 task feature 时为 None。
    """

    uuid: str = ""
    agent_type: str = ""
    mode: RunMode = RunMode.EXECUTE
    features: frozenset[str] | None = None
    enable_thinking: bool = True
    reasoning_effort: str | None = None
    provider_reasoning_effort: str | None = None
    task_writer: TaskStatusWriter | None = field(default=None, repr=False)

    def resolve_thinking(self, declared: bool | None) -> bool:
        """解析子 agent 的思考开关：显式声明优先，否则继承父 agent。"""
        return self.enable_thinking if declared is None else declared

    def resolve_reasoning_effort(self, declared: str | None) -> str | None:
        """解析子 agent 的推理强度：显式声明 → 父已解析值 → 父 provider 默认值。"""
        if declared is not None:
            return declared
        if self.reasoning_effort is not None:
            return self.reasoning_effort
        return self.provider_reasoning_effort

    def resolve_features(self, declared: frozenset[str] | None) -> frozenset[str] | None:
        """解析子 agent 的 feature 集：显式声明优先，否则继承父 agent。"""
        return self.features if declared is None else declared


@dataclass(frozen=True)
class SubagentSpawnRequest:
    """一次子 agent 构造请求 — 继承值已解析完毕。

    继承值由 `ParentAgentFacts` 在调度侧解析后填入，执行器不再回读父 agent。

    Attributes:
        manifest: 目标子 agent 的定义。
        enable_thinking: 已解析的思考开关。
        reasoning_effort: 已解析的推理强度。
        features: 已解析的 feature 集；None 表示交由装配期按未声明处理。
        mode: 子 agent 的初始运行模式。
    """

    manifest: "AgentManifest"
    enable_thinking: bool
    reasoning_effort: str | None
    features: frozenset[str] | None
    mode: RunMode


@dataclass(frozen=True)
class SubagentHandle:
    """运行中的子 agent 身份句柄，供生命周期事件、hook 与会话清理使用。

    `history` 是运行实例消息列表的**活引用**：句柄创建后子 agent 仍会追加
    消息，因此结束事件必须通过 `snapshot_history()` 取当时的浅拷贝快照。
    """

    uuid: str
    history: list[dict] = field(default_factory=list, repr=False)

    @classmethod
    def of(cls, agent: "SubagentRun") -> "SubagentHandle":
        """从运行对象提取身份快照。

        Args:
            agent: 已构造的子 agent。

        Returns:
            身份快照。
        """
        return cls(uuid=str(agent.uuid), history=agent.history)

    def snapshot_history(self) -> list[dict]:
        """返回当时的消息浅拷贝，避免快照被后续追加改写。"""
        return list(self.history)


class SubagentOutcome(Protocol):
    """一次子 agent 运行的结果面；`RunResult` 结构上已满足。"""

    final_text: str
    llm_error: LLMErrorInfo | None


class SubagentRun(Protocol):
    """已构造子 agent 的最小运行面。"""

    uuid: UUID
    history: list[dict]

    async def run(self, input: str | None = None, **kwargs: object) -> SubagentOutcome:
        """执行一次任务并返回结果。"""
        ...


class SubagentExecutor(Protocol):
    """构造子 agent 的能力；由调用方 agent 实现。"""

    def spawn_subagent(self, request: SubagentSpawnRequest) -> SubagentRun:
        """按请求构造（不运行）一个子 agent。"""
        ...
