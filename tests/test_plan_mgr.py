"""计划模式的指令与持久化契约。"""
from types import SimpleNamespace
import pytest
from src.mgr.plan_mgr import PlanMgr, _PLAN_SKILL_KEY
from src.mgr.reminder_mgr import ReminderMgr


def test_main_and_child_instructions(tmp_path):
    mgr = PlanMgr(tmp_path)
    main = mgr.get_turn_start_reminder(True, False)
    child = mgr.get_turn_start_reminder(True, True)
    assert 'submit_plan' in main and _PLAN_SKILL_KEY in main
    assert 'exec_command' in child and '不提交计划' in child
    assert _PLAN_SKILL_KEY not in child
    assert 'write_file' not in main


def test_post_round_instruction_consumed_once(tmp_path):
    mgr = PlanMgr(tmp_path)
    mgr.enter_mode(SimpleNamespace(plan_active=False, refresh_tools_schemas=lambda: None), ReminderMgr())
    assert mgr.pop_post_round_reminder(True, False)
    assert mgr.pop_post_round_reminder(True, False) is None


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
