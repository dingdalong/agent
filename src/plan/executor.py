import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Tuple

from src.tools.tool_executor import ToolExecutor
from src.core.async_api import call_model
from src.core.io import agent_input
from src.plan.models import Plan, Step
from src.plan.exceptions import VariableResolutionError, StepExecutionError, DependencyError, PlanValidationError
from config import PLAN_DEFAULT_TIMEOUT, PLAN_MAX_VARIABLE_DEPTH

logger = logging.getLogger(__name__)

# 代码级常量
VARIABLE_PREFIX = "$"
ACTION_TOOL = "tool"
ACTION_USER_INPUT = "user_input"
ACTION_SUBTASK = "subtask"

DEFERRED_PLACEHOLDER = "[待确认]"


@dataclass
class DeferredStep:
    """被延迟执行的敏感工具步骤"""
    step: Step
    resolved_args: Dict[str, Any] = field(default_factory=dict)

def resolve_variables(obj: Any, context: Dict[str, Any]) -> Any:
    """解析变量引用，支持 $step_id.field 语法"""
    if isinstance(obj, dict):
        return {k: resolve_variables(v, context) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [resolve_variables(v, context) for v in obj]
    elif isinstance(obj, str) and obj.startswith(VARIABLE_PREFIX):
        # 解析路径，如 $step1.result 或 $step2.data.name
        parts = obj[len(VARIABLE_PREFIX):].split('.')
        if len(parts) > PLAN_MAX_VARIABLE_DEPTH:
            logger.warning(f"变量路径深度超过限制({PLAN_MAX_VARIABLE_DEPTH}): {obj}")
            return obj
        cur = context
        for i, part in enumerate(parts):
            if isinstance(cur, dict):
                if part not in cur:
                    logger.debug(f"变量路径不存在: {obj}, 可用键: {list(cur.keys())}")
                    return obj
                cur = cur[part]
            elif isinstance(cur, str):
                logger.debug(f"尝试从字符串值访问属性: {obj}, 当前值: {cur}")
                return obj
            else:
                logger.debug(f"尝试从非字典类型访问属性: {obj}, 类型: {type(cur).__name__}")
                return obj
        return cur
    else:
        return obj


def build_dependency_graph(steps: List[Step]) -> Dict[str, List[str]]:
    """构建步骤依赖图

    Args:
        steps: 步骤列表

    Returns:
        字典：步骤ID -> 该步骤依赖的步骤ID列表
    """
    graph = {}
    for step in steps:
        graph[step.id] = step.depends_on.copy()
    return graph


def topological_sort_layered(steps: List[Step]) -> List[List[Step]]:
    """对步骤进行分层拓扑排序，同一层的步骤无互相依赖，可并行执行

    Args:
        steps: 步骤列表

    Returns:
        分层排序后的步骤列表，每层是一个可并行执行的步骤组

    Raises:
        DependencyError: 如果存在循环依赖或缺失依赖
    """
    graph = build_dependency_graph(steps)
    step_map = {step.id: step for step in steps}

    # 计算入度
    in_degree = {step.id: 0 for step in steps}
    for step in steps:
        for dep in step.depends_on:
            if dep not in in_degree:
                raise DependencyError(
                    f"步骤 {step.id} 依赖不存在的步骤 {dep}",
                    step_id=step.id,
                    missing_deps=[dep]
                )
            in_degree[step.id] += 1

    # 分层BFS
    layers = []
    queue = [step.id for step in steps if in_degree[step.id] == 0]

    while queue:
        layers.append([step_map[sid] for sid in queue])
        next_queue = []
        for step_id in queue:
            for node, deps in graph.items():
                if step_id in deps:
                    in_degree[node] -= 1
                    if in_degree[node] == 0:
                        next_queue.append(node)
        queue = next_queue

    # 检查循环依赖
    total_sorted = sum(len(layer) for layer in layers)
    if total_sorted != len(steps):
        unsorted = set(step.id for step in steps) - {s.id for layer in layers for s in layer}
        raise DependencyError(
            f"存在循环依赖：步骤 {unsorted} 形成循环",
            step_id=list(unsorted)[0] if unsorted else "",
            missing_deps=list(unsorted)
        )

    return layers


def _build_subtask_prompt(step: Step, context: Dict[str, Any]) -> str:
    """构建子任务的 LLM 提示词，包含任务描述和前序步骤结果"""
    parts = []

    # 主任务描述
    subtask_prompt = step.subtask_prompt or step.description
    # 解析 prompt 中的变量引用
    resolved_prompt = resolve_variables(subtask_prompt, context)
    parts.append(f"任务：{resolved_prompt}")

    # 附加前序步骤的结果作为参考
    if context:
        parts.append("\n参考信息（前序步骤结果）：")
        for step_id, result in context.items():
            result_str = str(result)
            if len(result_str) > 500:
                result_str = result_str[:500] + "..."
            parts.append(f"- {step_id}: {result_str}")

    return "\n".join(parts)


async def execute_step(
    step: Step,
    context: Dict[str, Any],
    tool_executor: ToolExecutor,
    timeout: Optional[float] = None
) -> str:
    """执行单个步骤，返回结果字符串

    Args:
        step: 要执行的步骤
        context: 上下文变量字典
        tool_executor: 工具执行器
        timeout: 超时时间（秒），None使用默认值，0表示不限制
    """
    step_timeout = timeout if timeout is not None else PLAN_DEFAULT_TIMEOUT

    if step.action == ACTION_TOOL:
        if not step.tool_name:
            raise StepExecutionError(
                "工具步骤缺少 tool_name",
                step_id=step.id,
                step_description=step.description,
                action=step.action
            )
        # 解析参数中的变量
        resolved_args = resolve_variables(step.tool_args or {}, context)
        try:
            coro = tool_executor.execute(step.tool_name, resolved_args)
            if step_timeout > 0:
                result = await asyncio.wait_for(coro, timeout=step_timeout)
            else:
                result = await coro
            return str(result)
        except (asyncio.TimeoutError, TimeoutError) as e:
            raise StepExecutionError(
                f"步骤执行超时({step_timeout}秒)",
                step_id=step.id,
                step_description=step.description,
                action=step.action
            ) from e
        except Exception as e:
            raise StepExecutionError(
                f"工具执行失败: {e}",
                step_id=step.id,
                step_description=step.description,
                action=step.action
            ) from e

    elif step.action == ACTION_USER_INPUT:
        # 用户输入不设超时
        prompt = f"\n助手: {step.description or '请提供信息：'}\n\n你: "
        try:
            user_input = await agent_input(prompt)
            return user_input
        except Exception as e:
            raise StepExecutionError(
                f"用户输入获取失败: {e}",
                step_id=step.id,
                step_description=step.description,
                action=step.action
            ) from e

    elif step.action == ACTION_SUBTASK:
        prompt = _build_subtask_prompt(step, context)
        messages = [
            {"role": "system", "content": "你是一个任务执行助手。请根据任务描述和上下文信息，生成详细的内容。"},
            {"role": "user", "content": prompt}
        ]
        try:
            # 使用流式调用，避免生成长文本时 HTTP 超时
            response, _, _ = await call_model(messages)
            return response
        except (asyncio.TimeoutError, TimeoutError) as e:
            raise StepExecutionError(
                f"子任务执行超时({step_timeout}秒)",
                step_id=step.id,
                step_description=step.description,
                action=step.action
            ) from e
        except StepExecutionError:
            raise
        except Exception as e:
            raise StepExecutionError(
                f"子任务执行失败: {e}",
                step_id=step.id,
                step_description=step.description,
                action=step.action
            ) from e

    else:
        raise StepExecutionError(
            f"未知动作类型: {step.action}",
            step_id=step.id,
            step_description=step.description,
            action=step.action
        )

def validate_plan(plan: Plan) -> None:
    """验证计划的合法性

    Raises:
        PlanValidationError: 验证失败时抛出
    """
    errors = []

    # 检查步骤ID唯一性
    seen_ids = set()
    for step in plan.steps:
        if step.id in seen_ids:
            errors.append(f"重复的步骤ID: {step.id}")
        seen_ids.add(step.id)

    # 检查tool类型步骤必须有tool_name
    for step in plan.steps:
        if step.action == ACTION_TOOL and not step.tool_name:
            errors.append(f"步骤 {step.id} 类型为 tool 但缺少 tool_name")

    if errors:
        raise PlanValidationError("计划验证失败", validation_errors=errors)


def _is_sensitive_tool_step(step: Step, tool_executor: ToolExecutor) -> bool:
    """判断步骤是否为敏感工具步骤"""
    return (step.action == ACTION_TOOL
            and step.tool_name is not None
            and tool_executor.is_sensitive(step.tool_name))


async def execute_plan(
    plan: Plan,
    tool_executor: ToolExecutor,
    max_concurrency: Optional[int] = None,
    continue_on_error: bool = False
) -> Tuple[Dict[str, Any], List[DeferredStep]]:
    """执行整个计划，敏感工具步骤延迟执行。

    同一层（无互相依赖）的步骤会并行执行。
    敏感工具步骤不会在此函数中执行，而是收集到 deferred_steps 中返回，
    由调用方在展示结果后统一处理确认和执行。

    Args:
        plan: 要执行的计划
        tool_executor: 工具执行器
        max_concurrency: 最大并行度（None表示不限制）
        continue_on_error: 为True时，步骤失败不中断整个计划

    Returns:
        (context, deferred_steps): 已执行步骤结果字典 和 延迟的敏感步骤列表
    """
    # 0. 验证计划
    validate_plan(plan)

    context = {}  # 存储每个步骤的结果
    failed_steps = set()  # 记录失败的步骤ID
    deferred_steps: List[DeferredStep] = []  # 延迟的敏感步骤

    # 1. 分层拓扑排序
    try:
        layers = topological_sort_layered(plan.steps)
    except DependencyError as e:
        logger.error(f"依赖关系错误: {e}")
        raise

    semaphore = asyncio.Semaphore(max_concurrency) if max_concurrency else None

    async def _run_step(step: Step) -> tuple:
        async def _exec():
            logger.debug(f"执行步骤 {step.id}: {step.description}")
            result = await execute_step(step, context, tool_executor)
            return step.id, result

        if semaphore:
            async with semaphore:
                return await _exec()
        return await _exec()

    def _should_skip(step: Step) -> bool:
        """检查步骤是否因依赖失败而应跳过"""
        return any(dep in failed_steps for dep in step.depends_on)

    def _defer_step(step: Step) -> None:
        """将敏感工具步骤加入延迟列表"""
        resolved_args = resolve_variables(step.tool_args or {}, context)
        deferred_steps.append(DeferredStep(step=step, resolved_args=resolved_args))
        context[step.id] = DEFERRED_PLACEHOLDER

    # 2. 逐层执行，同层并行；敏感工具步骤延迟
    for layer in layers:
        if len(layer) == 1:
            step = layer[0]
            if continue_on_error and _should_skip(step):
                error_msg = f"跳过：依赖的步骤失败"
                context[step.id] = error_msg
                failed_steps.add(step.id)
                continue

            # 敏感工具步骤延迟
            if _is_sensitive_tool_step(step, tool_executor):
                _defer_step(step)
                continue

            try:
                logger.debug(f"执行步骤 {step.id}: {step.description}")
                result = await execute_step(step, context, tool_executor)
                context[step.id] = result
            except StepExecutionError as e:
                if not continue_on_error:
                    raise
                logger.error(f"步骤 {step.id} 执行失败: {e}")
                context[step.id] = f"执行失败: {e}"
                failed_steps.add(step.id)
        else:
            # 分离敏感步骤和普通步骤
            runnable = []
            for step in layer:
                if continue_on_error and _should_skip(step):
                    context[step.id] = f"跳过：依赖的步骤失败"
                    failed_steps.add(step.id)
                elif _is_sensitive_tool_step(step, tool_executor):
                    _defer_step(step)
                else:
                    runnable.append(step)

            if not runnable:
                continue

            if continue_on_error:
                # 使用 return_exceptions 避免单个失败中断整层
                results = await asyncio.gather(
                    *[_run_step(step) for step in runnable],
                    return_exceptions=True
                )
                for step, result in zip(runnable, results):
                    if isinstance(result, BaseException):
                        logger.error(f"步骤 {step.id} 执行失败: {result}")
                        context[step.id] = f"执行失败: {result}"
                        failed_steps.add(step.id)
                    else:
                        step_id, step_result = result
                        context[step_id] = step_result
            else:
                results = await asyncio.gather(*[_run_step(step) for step in runnable])
                for step_id, result in results:
                    context[step_id] = result

    return context, deferred_steps
