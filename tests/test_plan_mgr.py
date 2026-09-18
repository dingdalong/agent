"""计划模式的指令与持久化契约。"""
from types import SimpleNamespace
import pytest
from src.mode import RunMode
from src.mgr.plan_mgr import PlanMgr
from src.mgr.reminder_mgr import ReminderMgr


def test_main_and_child_instructions(tmp_path):
    mgr = PlanMgr(tmp_path)
    main = mgr.instructions(False)
    child = mgr.instructions(True)
    for prompt in (main, child):
        assert prompt.startswith('# 当前模式：计划模式\n你当前处于计划模式。')
        assert '读取和搜索本地文件' in prompt
        assert '单元测试、集成测试和端到端测试' in prompt
        assert '只能写入框架专用临时目录' in prompt
        assert '禁止创建、编辑、删除或移动项目文件' in prompt
        assert '禁止申请额外写权限或网络权限' in prompt
    assert 'submit_plan' in main
    assert '# 规划流程' in main
    assert '状态权威写入者' in main
    assert '立即调用 submit_plan' in main
    assert '不提交完整计划' in child
    assert 'submit_plan' not in child
    assert 'write_file' not in main


def test_mode_changes_do_not_register_history_reminders(tmp_path):
    mgr = PlanMgr(tmp_path)
    agent = SimpleNamespace(mode=RunMode.EXECUTE)
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
    agent.set_mode(RunMode.PLAN)
    before = list(agent.history)
    system = agent._prompt_mgr.build()
    first = agent._prompt_mgr.build_mode_instructions()
    second = agent._prompt_mgr.build_mode_instructions()
    assert first == second
    assert '# 规划流程' in first
    assert 'load_skill' not in first
    assert '每次新增工具调用必须解决' not in first
    assert agent._prompt_mgr.build() == system
    agent.set_mode(RunMode.EXECUTE)
    assert '# 规划流程' not in agent._prompt_mgr.build_mode_instructions()
    assert agent._prompt_mgr.build() == system
    assert agent.history == before


def test_real_coding_prompt_isolates_plan_content_to_plan_suffix(tmp_path):
    """真实 coding 角色在普通模式不泄露计划流程或控制工具指导。"""
    from tests.test_subagent_skills import _main

    agent = _main(tmp_path, "coding")
    agent.deps.plan_mgr = PlanMgr(tmp_path)
    execute_prompt = agent._prompt_mgr.build()
    execute_text = "\n\n".join(message["content"] for message in execute_prompt)
    execute_text += "\n\n" + agent._prompt_mgr.build_mode_instructions()
    for forbidden in ("计划模式", "Plan", "plan-workflow", "execute-plan", "submit_plan"):
        assert forbidden not in execute_text

    agent.set_mode(RunMode.PLAN)
    plan_prompt = agent._prompt_mgr.build()
    assert execute_prompt == plan_prompt
    assert "submit_plan" in agent._prompt_mgr.build_mode_instructions()
