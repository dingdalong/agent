"""计划模式的指令与持久化契约。"""
from types import SimpleNamespace
import pytest
from src.mgr.plan_mgr import PlanMgr, _PLAN_SKILL_KEY
from src.mgr.reminder_mgr import ReminderMgr


def test_main_and_child_instructions(tmp_path):
    mgr = PlanMgr(tmp_path)
    main = mgr.instructions(None, False)
    child = mgr.instructions(None, True)
    assert 'submit_plan' in main
    assert '不提交计划' in child
    assert _PLAN_SKILL_KEY not in child
    assert 'write_file' not in main


def test_mode_changes_do_not_register_history_reminders(tmp_path):
    mgr = PlanMgr(tmp_path)
    agent = SimpleNamespace(plan_active=False, refresh_tools_schemas=lambda: None)
    assert mgr.enter_mode(agent)
    assert not mgr.enter_mode(agent)
    assert mgr.exit_mode(agent)
    assert not mgr.exit_mode(agent)


def test_save_replaces_active_plan_and_rejects_symlink(tmp_path):
    mgr = PlanMgr(tmp_path)
    first = mgr.save('one', {})
    assert first.read_text() == 'one\n'
    second = mgr.save('two', {'path': str(first)})
    assert first == second and first.read_text() == 'two\n'
    first.unlink()
    outside = tmp_path / 'outside'
    outside.write_text('untouched')
    first.symlink_to(outside)
    with pytest.raises(ValueError):
        mgr.save('bad', {'path': str(first)})
    assert outside.read_text() == 'untouched'


def test_current_mode_prompt_survives_rebuild_without_history_injection(tmp_path):
    from tests.test_subagent_skills import _main
    agent = _main(tmp_path, 'coding')
    agent.deps.plan_mgr = PlanMgr(tmp_path)
    agent.set_plan_active(False)
    agent.set_plan_active(True)
    before = list(agent.history)
    first = agent._prompt_mgr.build()[0]['content']
    second = agent._prompt_mgr.build()[0]['content']
    assert first == second
    assert first.count('新增调查必须解决') == 1
    assert agent._reminder_mgr.build_turn_start_instructions(True, False) == ''
    agent._prompt_mgr.invalidate_cache()
    assert agent._prompt_mgr.build()[0]['content'].count('新增调查必须解决') == 1
    agent.set_plan_active(False)
    assert '新增调查必须解决' not in agent._prompt_mgr.build()[0]['content']
    assert agent.history == before
