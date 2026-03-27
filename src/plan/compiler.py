"""PlanCompiler -- 将 Plan 转换为 CompiledGraph。"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from src.plan.models import Plan, Step
from src.plan.exceptions import CompileError
from src.agents.context import RunContext, DictState
from src.graph.types import FunctionNode, NodeResult, CompiledGraph, Edge, ParallelGroup
from src.graph.builder import GraphBuilder
from src.agents.registry import AgentRegistry
from src.agents.runner import AgentRunner
from src.tools.router import ToolRouter

logger = logging.getLogger(__name__)

VARIABLE_PREFIX = "$"


def resolve_variables(obj: Any, context: dict) -> Any:
    """解析变量引用，支持 $step_id.field 嵌套语法。

    Args:
        obj: 待解析的对象（str / dict / list / 其他原样返回）
        context: 变量上下文字典
    """
    if isinstance(obj, dict):
        return {k: resolve_variables(v, context) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_variables(v, context) for v in obj]
    if isinstance(obj, str) and obj.startswith(VARIABLE_PREFIX):
        parts = obj[len(VARIABLE_PREFIX):].split(".")
        cur: Any = context
        for part in parts:
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return obj  # 路径不存在，原样返回
        return cur
    return obj


def _state_to_dict(state: Any) -> dict:
    """将 DictState（Pydantic extra='allow'）转为普通 dict。"""
    if hasattr(state, "model_extra") and state.model_extra:
        return dict(state.model_extra)
    return {}


def _topological_sort_layered(steps: list[Step]) -> list[list[Step]]:
    """分层拓扑排序。同一层的步骤无互相依赖，可并行。

    Raises:
        CompileError: 循环依赖或缺失依赖
    """
    step_ids = {s.id for s in steps}
    step_map = {s.id: s for s in steps}

    # 检查依赖存在性
    for step in steps:
        for dep in step.depends_on:
            if dep not in step_ids:
                raise CompileError(
                    f"步骤 {step.id} 依赖不存在的步骤 {dep}",
                    details=[f"{step.id} -> {dep}"],
                )

    # 计算入度
    in_degree = {s.id: 0 for s in steps}
    dependents: dict[str, list[str]] = {s.id: [] for s in steps}
    for step in steps:
        for dep in step.depends_on:
            in_degree[step.id] += 1
            dependents[dep].append(step.id)

    # 分层 BFS
    layers: list[list[Step]] = []
    queue = [sid for sid, deg in in_degree.items() if deg == 0]

    while queue:
        layers.append([step_map[sid] for sid in queue])
        next_queue: list[str] = []
        for sid in queue:
            for dep_id in dependents[sid]:
                in_degree[dep_id] -= 1
                if in_degree[dep_id] == 0:
                    next_queue.append(dep_id)
        queue = next_queue

    sorted_count = sum(len(layer) for layer in layers)
    if sorted_count != len(steps):
        unsorted = step_ids - {s.id for layer in layers for s in layer}
        raise CompileError(
            f"存在循环依赖：{unsorted}",
            details=[f"循环涉及步骤: {', '.join(unsorted)}"],
        )

    return layers


class PlanCompiler:
    """将 Plan 编译为 CompiledGraph。"""

    def __init__(self, agent_registry: AgentRegistry, tool_router: ToolRouter):
        self._registry = agent_registry
        self._router = tool_router
        self._runner = AgentRunner(registry=agent_registry)

    def compile(self, plan: Plan) -> CompiledGraph:
        """Plan -> CompiledGraph。

        1. 验证计划合法性
        2. 分层拓扑排序
        3. 每个 Step -> FunctionNode
        4. 层间关系 -> Edge / ParallelGroup
        """
        self._validate(plan)
        layers = _topological_sort_layered(plan.steps)

        builder = GraphBuilder()
        prev_exit: str | None = None

        for layer_idx, layer in enumerate(layers):
            # 为本层每个 step 创建 FunctionNode
            for step in layer:
                fn = self._make_node_fn(step)
                builder.add_function(step.id, fn)

            if len(layer) == 1:
                step_id = layer[0].id
                if layer_idx == 0:
                    builder.set_entry(step_id)
                if prev_exit is not None:
                    builder.add_edge(prev_exit, step_id)
                prev_exit = step_id
            else:
                # 多个步骤 -> ParallelGroup + merge 节点
                merge_name = f"_merge_{layer_idx}"
                merge_fn = self._make_merge_fn()
                builder.add_function(merge_name, merge_fn)
                builder.add_parallel([s.id for s in layer], then=merge_name)

                if layer_idx == 0:
                    builder.set_entry(layer[0].id)
                if prev_exit is not None:
                    for step in layer:
                        builder.add_edge(prev_exit, step.id)
                prev_exit = merge_name

        return builder.compile()

    def _validate(self, plan: Plan) -> None:
        """编译前验证。"""
        if not plan.steps:
            raise CompileError("空计划：没有步骤")

        # 检查 ID 唯一性
        seen: set[str] = set()
        for step in plan.steps:
            if step.id in seen:
                raise CompileError(f"重复的步骤 ID: {step.id}")
            seen.add(step.id)

        # 检查 agent 存在性
        for step in plan.steps:
            if step.agent_name and not self._registry.has(step.agent_name):
                raise CompileError(f"Agent '{step.agent_name}' 不存在于注册表中")

    def _make_node_fn(self, step: Step):
        """为一个 Step 创建 FunctionNode 的执行函数。"""
        if step.tool_name is not None:
            return self._make_tool_fn(step)
        return self._make_agent_fn(step)

    def _make_tool_fn(self, step: Step):
        """工具步骤 -> 闭包函数。"""
        tool_name = step.tool_name
        tool_args = step.tool_args
        router = self._router

        async def fn(ctx: RunContext) -> NodeResult:
            resolved = resolve_variables(tool_args, _state_to_dict(ctx.state))
            result = await router.route(tool_name, resolved)
            return NodeResult(output=result)

        return fn

    def _make_agent_fn(self, step: Step):
        """Agent 步骤 -> 闭包函数。"""
        agent_name = step.agent_name
        agent_prompt = step.agent_prompt or step.description
        registry = self._registry
        runner = self._runner

        async def fn(ctx: RunContext) -> NodeResult:
            resolved_prompt = resolve_variables(agent_prompt, _state_to_dict(ctx.state))
            agent = registry.get(agent_name)
            agent_ctx = replace(ctx, input=resolved_prompt)
            result = await runner.run(agent, agent_ctx)
            return NodeResult(output={"text": result.text, "data": result.data}, handoff=result.handoff)

        return fn

    @staticmethod
    def _make_merge_fn():
        """并行组合并节点 -> 空操作透传。

        并行节点的结果已由 GraphEngine._write_state 写入 state，
        后续节点通过 $step_id 变量引用读取。merge 节点仅作汇合点。
        """

        async def fn(ctx: RunContext) -> NodeResult:
            return NodeResult(output=None)

        return fn
