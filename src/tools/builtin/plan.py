"""一次提交计划正文，由框架保存和审核。"""
import asyncio
from pydantic import BaseModel, Field

from src.events.types import caller_identity
from src.tools.decorator import tool
from src.tools.display import ToolResult
from src.mode import RunMode
from src.tools.policy import AccessKind, DataFlow, ToolAudience, ToolAvailability, ToolPolicy


class SubmitPlan(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, description='完整自包含计划正文；修订时提交完整替换内容')


@tool(model=SubmitPlan, description='一次提交完整计划。框架保存、展示、审核；不要另写文件、登记路径或重复输出计划。', policy=ToolPolicy(AccessKind.INTERNAL, DataFlow.LOCAL, plan_safe=True), availability=ToolAvailability(frozenset({RunMode.PLAN}), feature='plan', audience=ToolAudience.MAIN_ONLY), counts_as_work=False)
async def submit_plan(title, content, agent, deps, authorization):
    if not authorization.allowed or agent.mode is not RunMode.PLAN or deps.plan_mgr is None:
        return ToolResult.failure('invalid_plan_state', '仅计划模式下可提交计划')
    if not title.strip() or not content.strip():
        return ToolResult.failure('invalid_arguments', '计划标题和正文不能为空')
    title = str(deps.data_guard.redact(title))
    content = str(deps.data_guard.redact(content))
    try:
        path = await asyncio.to_thread(deps.plan_mgr.save, content, deps.session_state.plan if deps.session_state else {})
    except (OSError, ValueError) as exc:
        return ToolResult.failure('plan_save_failed', str(exc))
    state = {'path': str(path), 'title': title, 'approved': False}
    if deps.session_state is not None:
        deps.session_state.plan = state
    await deps.event_bus.request_output(f'计划：{title}\n{path}\n')
    await deps.event_bus.request_output(content, markdown=True)
    caller_type, caller_uuid = caller_identity(agent)
    choice, feedback = await deps.event_bus.request_choice_input(
        prompt='计划审核', options=[('auto', '自动执行'), ('manual', '手动执行')],
        descriptions=['在当前会话实施计划', '保存计划并结束当前回合'],
        input_placeholder='输入修改意见…', default_index=0, markdown=False,
        caller_agent_type=caller_type, caller_uuid=caller_uuid,
    )
    if choice in {'auto', 'manual'} and not feedback.strip():
        state['approved'] = True
        deps.plan_mgr.exit_mode(agent)
    if deps.session_mgr is not None and deps.session_state is not None:
        await asyncio.to_thread(deps.session_mgr.save_state, deps.session_id, deps.session_state)
    if feedback.strip():
        return ToolResult(f'修改意见：{feedback.strip()}\n请修订后再次 submit_plan；路径：{path}')
    if choice == 'auto':
        return ToolResult(f'已批准计划：{path}。已退出计划模式，加载 builtin:execute-plan 并实施；正文已在当前对话中，不要重复读取。')
    return ToolResult(f'计划已保存：{path}。' + ('用户选择手动执行。' if choice == 'manual' else '审核已取消，保持计划模式。'), end_turn=True)
