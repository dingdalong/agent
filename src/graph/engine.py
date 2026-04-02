"""GraphEngine — 异步图执行器（Agent 无关）。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from src.graph.types import CompiledGraph, GraphNode, NodeResult, ParallelGroup
from src.events.bus import EventBus
from src.events.types import GraphStarted, GraphEnded, NodeStarted, NodeEnded, ErrorOccurred

logger = logging.getLogger(__name__)

StateT = TypeVar("StateT", bound=BaseModel)


@dataclass
class GraphResult(Generic[StateT]):
    """图执行的最终结果。"""
    output: Any
    state: StateT
    trace: list = field(default_factory=list)


class GraphEngine:
    """异步图执行器 — 与智能体无关的通用图遍历引擎。

    执行模型：
    1. 维护 pending 节点列表，从 entry 开始
    2. 检查 pending 是否匹配 ParallelGroup → asyncio.gather 并行执行
    3. 否则顺序执行单节点
    4. 每个节点的 output 写入 context.state（可通过 $node_name 引用）
    5. 根据 NodeResult 的 handoff / next / edges 决定下一步
    6. max_handoff_depth 防止无限循环
    """

    def __init__(
        self,
        event_bus: EventBus | None = None,
        max_handoff_depth: int = 10,
        max_parallel_width: int = 5,
    ):
        self._bus = event_bus
        self.max_handoff_depth = max_handoff_depth
        self.max_parallel_width = max_parallel_width

    async def run(self, graph: CompiledGraph, context: Any) -> GraphResult:
        """执行编译后的图。"""
        if self._bus:
            await self._bus.emit(GraphStarted(timestamp=time.time(), source="graph"))

        last_output: Any = None
        pending: list[str] = [graph.entry]
        visited: set[str] = set()

        while pending:
            parallel_group = self._find_parallel_group(pending, graph.parallel_groups)

            if parallel_group:
                nodes_to_run = parallel_group.nodes
                results = await self._run_parallel(nodes_to_run, graph, context)
                for name, node_result in results.items():
                    last_output = node_result.output
                    self._write_state(context, name, node_result.output)
                    self._write_last_output(context, node_result.output)
                    visited.add(name)
                pending = [parallel_group.then]

            elif len(pending) > 1:
                # 多个 pending 节点且无 ParallelGroup — 并行执行
                to_run = pending[:self.max_parallel_width]
                if len(pending) > self.max_parallel_width:
                    logger.warning(
                        "Parallel width %d exceeds limit %d, executing first %d",
                        len(pending), self.max_parallel_width, self.max_parallel_width,
                    )
                results = await self._run_parallel(to_run, graph, context)
                next_pending: list[str] = []
                for name, node_result in results.items():
                    last_output = node_result.output
                    self._write_state(context, name, node_result.output)
                    self._write_last_output(context, node_result.output)
                    visited.add(name)
                    resolved = self._resolve_edges(name, node_result, graph, context)
                    next_pending.extend(resolved)
                # 去重但保持顺序
                pending = list(dict.fromkeys(next_pending))

            else:
                current_name = pending.pop(0)
                if current_name in visited and current_name != graph.entry:
                    continue

                node = graph.nodes.get(current_name)
                if node is None:
                    logger.warning(f"Node '{current_name}' not found, skipping")
                    continue

                node_result = await self._execute_node(node, context)
                last_output = node_result.output
                self._write_state(context, current_name, node_result.output)
                self._write_last_output(context, node_result.output)
                visited.add(current_name)

                # 处理 handoff
                if node_result.handoff:
                    target = node_result.handoff.target
                    context.depth += 1
                    if context.depth > self.max_handoff_depth:
                        logger.warning(f"Max handoff depth reached ({self.max_handoff_depth})")
                    elif target in graph.nodes:
                        # 使用结构化消息的 task 字段作为下一个节点的输入
                        handoff_msg = getattr(node_result.handoff, "message", None)
                        context.input = handoff_msg.task if handoff_msg else node_result.handoff.task
                        pending = [target]
                        continue
                    else:
                        logger.error(f"Handoff target '{target}' not found in graph")
                        self._add_trace(context, current_name, "error",
                                        {"error": f"Handoff target '{target}' not found"})
                    continue

                # 处理显式 next
                if node_result.next is not None:
                    if isinstance(node_result.next, list):
                        pending = node_result.next
                    else:
                        pending = [node_result.next]
                    continue

                # 按边路由
                next_nodes = self._resolve_edges(current_name, node_result, graph, context)
                pending = next_nodes

        result = GraphResult(
            output=last_output,
            state=context.state,
            trace=list(getattr(context, "trace", [])),
        )
        if self._bus:
            await self._bus.emit(GraphEnded(timestamp=time.time(), source="graph", output=result.output))
        return result

    async def _execute_node(self, node: GraphNode, context: Any) -> NodeResult:
        if self._bus:
            node_type = type(node).__name__.replace("Node", "").lower()
            await self._bus.emit(NodeStarted(
                timestamp=time.time(), source=node.name, node_type=node_type,
            ))
        self._add_trace(context, node.name, "start")
        try:
            result = await node.execute(context)
        except Exception as e:
            self._add_trace(context, node.name, "error", {"error": str(e)})
            if self._bus:
                await self._bus.emit(ErrorOccurred(
                    timestamp=time.time(), source=node.name, error=str(e),
                ))
            raise
        self._add_trace(context, node.name, "end")
        if self._bus:
            summary = ""
            if isinstance(result.output, str):
                summary = result.output[:100]
            elif isinstance(result.output, dict):
                summary = str(result.output)[:100]
            await self._bus.emit(NodeEnded(
                timestamp=time.time(), source=node.name, output_summary=summary,
            ))
        return result

    async def _run_parallel(
        self, node_names: list[str], graph: CompiledGraph, context: Any,
    ) -> dict[str, NodeResult]:
        async def _run_one(name: str) -> tuple[str, NodeResult]:
            node = graph.nodes[name]
            result = await self._execute_node(node, context)
            return name, result

        tasks = [_run_one(name) for name in node_names]
        results = await asyncio.gather(*tasks)
        return dict(results)

    def _resolve_edges(
        self, source: str, node_result: NodeResult, graph: CompiledGraph, context: Any,
    ) -> list[str]:
        """根据边的 condition 字符串匹配决定下一步。"""
        candidates = [e for e in graph.edges if e.from_node == source]
        if not candidates:
            return []

        unconditional = [e for e in candidates if e.condition is None]
        conditional = [e for e in candidates if e.condition is not None]

        if conditional:
            chosen_branch = ""
            if isinstance(node_result.output, dict):
                chosen_branch = node_result.output.get("chosen_branch", "")
            elif hasattr(node_result.output, "data"):
                chosen_branch = node_result.output.data.get("chosen_branch", "")
            matched = [e for e in conditional if e.condition == chosen_branch]
            if not matched:
                logger.warning(
                    "No exact branch match for '%s', using first conditional edge",
                    chosen_branch,
                )
                matched = [conditional[0]]
            return [e.to_node for e in matched]

        return [e.to_node for e in unconditional]

    def _find_parallel_group(
        self, pending: list[str], groups: list[ParallelGroup],
    ) -> ParallelGroup | None:
        pending_set = set(pending)
        for group in groups:
            if pending_set & set(group.nodes):
                return group
        return None

    def _write_last_output(self, context: Any, output: Any) -> None:
        """将最新的节点输出写入 state._last_output，供 TerminalNode 等引用。"""
        try:
            setattr(context.state, "_last_output", output)
        except (AttributeError, ValueError):
            pass

    def _merge_parallel_outputs(
        self, names: list[str], results: list[NodeResult],
    ) -> dict:
        """合并并行节点的输出。"""
        texts = []
        data = {}
        for name, nr in zip(names, results):
            output = nr.output
            if isinstance(output, dict):
                texts.append(f"[{name}] {output.get('text', '')}")
                data[name] = output.get("data", {})
            elif hasattr(output, "text"):
                texts.append(f"[{name}] {output.text}")
                data[name] = getattr(output, "data", {})
            else:
                texts.append(f"[{name}] {output}")
        return {"text": "\n".join(texts), "data": data}

    def _write_state(self, context: Any, node_name: str, output: Any) -> None:
        try:
            setattr(context.state, node_name, output)
        except (AttributeError, ValueError):
            logger.debug(f"Cannot set state.{node_name}, state type may not support it")

    def _add_trace(self, context: Any, node: str, event: str, data: dict | None = None) -> None:
        trace = getattr(context, "trace", None)
        if trace is not None and hasattr(trace, "append"):
            trace.append({
                "node": node, "event": event, "timestamp": time.time(), "data": data or {},
            })
