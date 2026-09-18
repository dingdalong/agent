import asyncio
from types import SimpleNamespace

import pytest

from src.mgr.hooks_mgr import HookRunResult
from src.mgr.plan_mgr import PlanMgr
from src.common.session_state import SessionState
from src.mgr.reminder_mgr import ReminderMgr
from src.mode import RunMode


@pytest.mark.parametrize('choice,feedback,active,end_turn', [
    ('auto','',False,True), ('manual','',False,True), ('','',True,True), ('','修改范围',True,False),
])
def test_one_call_plan_submission(runtime, tmp_path, choice, feedback, active, end_turn):
    deps, agent = runtime
    output = []
    class Bus:
        async def request_output(self, text, **kwargs):
            output.append(text)
        async def request_choice_input(self, **kwargs):
            return choice, feedback
        async def emit(self, event):
            pass
    deps.event_bus = Bus()
    deps.plan_mgr = PlanMgr(tmp_path)
    deps.session_state = SessionState()
    deps.session_mgr = None
    agent._reminder_mgr = ReminderMgr()
    result = asyncio.run(deps.tools_mgr.execute('submit_plan', {'title':'目标 sentinel-secret','content':'完整计划正文'}, deps=deps, agent=agent))
    assert result.status == 'success', str(result)
    assert 'sentinel-secret' not in ''.join(output)
    assert 'sentinel-secret' not in deps.session_state.plan['title']
    assert (agent.mode is RunMode.PLAN) is active
    assert result.end_turn is end_turn
    assert sum(text == '完整计划正文' for text in output) == 1
    assert '完整计划正文' not in result.text
    saved = SessionState.from_dict(deps.session_state.to_dict())
    assert saved.plan['approved'] is (choice in {'auto','manual'})
    assert saved.plan['path'].endswith('.md')
    assert agent._queued_user_action == ('执行已批准的计划。' if choice == 'auto' else '')


def test_post_hook_block_preserves_approved_plan_turn_boundary(runtime, tmp_path):
    deps, agent = runtime

    class Bus:
        async def request_output(self, text, **kwargs):
            pass

        async def request_choice_input(self, **kwargs):
            return 'auto', ''

        async def emit(self, event):
            pass

    class Hooks:
        async def run_event(self, event, tool, payload, **kwargs):
            if event == 'PostToolUse':
                return HookRunResult(blocked=True, block_reason='blocked after execution')
            return HookRunResult()

    deps.event_bus = Bus()
    deps.hooks_mgr = Hooks()
    deps.plan_mgr = PlanMgr(tmp_path)
    deps.session_state = SessionState()
    deps.session_mgr = None

    result = asyncio.run(deps.tools_mgr.execute(
        'submit_plan', {'title': '目标', 'content': '正文'}, deps=deps, agent=agent,
    ))

    assert result.error_code == 'hook_blocked'
    assert result.end_turn is True
    assert agent.mode is RunMode.EXECUTE
    assert agent._queued_user_action == '执行已批准的计划。'


def test_plan_save_failure_does_not_exit(runtime, tmp_path):
    deps, agent = runtime
    deps.plan_mgr = PlanMgr(tmp_path)
    deps.session_state = SessionState()
    (tmp_path / '.agent').mkdir()
    (tmp_path / '.agent/plans').write_text('not a directory')
    result = asyncio.run(deps.tools_mgr.execute('submit_plan', {'title':'目标','content':'正文'}, deps=deps, agent=agent))
    assert result.error_code == 'plan_save_failed'
    assert agent.mode is RunMode.PLAN
    assert not deps.session_state.plan
