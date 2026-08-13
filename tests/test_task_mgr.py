"""TaskManager 轮末清理（cleanup_if_all_completed）、Agent 挂载点（_cleanup_tasks_at_turn_end）与任务变更通知投递（_make_task_notifier）的测试。"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path

from src.agent.agent import Agent
from src.app.bootstrap import _make_task_notifier
from src.events import EventBus, TaskStateChanged
from src.mgr.task_mgr import TaskManager


def test_cleanup_all_completed_clears_memory_disk_and_emits_empty_snapshot(
    tmp_path: Path,
) -> None:
    """全部任务 completed 时：返回 True、清空内存、删除 tasks_dir、发空快照。"""
    tasks_dir = tmp_path / "tasks"
    snapshots: list[list[dict]] = []
    mgr = TaskManager(tasks_dir=tasks_dir, on_change=snapshots.append)

    mgr.create("task 1", "desc 1")
    mgr.create("task 2", "desc 2")
    mgr.update("1", status="completed")
    mgr.update("2", status="completed")

    assert mgr.cleanup_if_all_completed() is True
    assert mgr.list_tasks() == {"tasks": []}
    assert not tasks_dir.exists()
    assert snapshots[-1] == []


def test_cleanup_skipped_when_open_tasks_remain(tmp_path: Path) -> None:
    """仍有未完成任务时：返回 False，任务与磁盘目录均保留。"""
    tasks_dir = tmp_path / "tasks"
    mgr = TaskManager(tasks_dir=tasks_dir)

    mgr.create("task 1", "desc 1")
    mgr.create("task 2", "desc 2")
    mgr.update("1", status="completed")

    assert mgr.cleanup_if_all_completed() is False
    assert {t["id"] for t in mgr.list_tasks()["tasks"]} == {"1", "2"}
    assert tasks_dir.exists()


def test_cleanup_skipped_when_no_tasks(tmp_path: Path) -> None:
    """无任务时：返回 False，除构造时初始快照外无新增 on_change 调用。"""
    tasks_dir = tmp_path / "tasks"
    snapshots: list[list[dict]] = []
    mgr = TaskManager(tasks_dir=tasks_dir, on_change=snapshots.append)

    assert mgr.cleanup_if_all_completed() is False
    assert snapshots == [[]]


def test_create_after_cleanup_starts_fresh_list(tmp_path: Path) -> None:
    """清理后 create 新任务：列表只含新任务，ID 单调递增不重置。"""
    tasks_dir = tmp_path / "tasks"
    mgr = TaskManager(tasks_dir=tasks_dir)

    mgr.create("task 1", "desc 1")
    mgr.create("task 2", "desc 2")
    mgr.update("1", status="completed")
    mgr.update("2", status="completed")
    assert mgr.cleanup_if_all_completed() is True

    created = mgr.create("task 3", "desc 3")
    assert created["task"]["id"] == "3"
    assert mgr.list_tasks() == {
        "tasks": [{"id": "3", "subject": "task 3", "status": "pending", "blocked_by": []}]
    }


def test_update_to_completed_does_not_cleanup_mid_turn(tmp_path: Path) -> None:
    """把全部任务 update 为 completed 不触发清理（收尾阶段才清理）。"""
    tasks_dir = tmp_path / "tasks"
    mgr = TaskManager(tasks_dir=tasks_dir)

    mgr.create("task 1", "desc 1")
    mgr.create("task 2", "desc 2")
    mgr.update("1", status="completed")
    mgr.update("2", status="completed")

    # 不调用 cleanup_if_all_completed：任务仍在内存、磁盘目录仍存在
    assert len(mgr.list_tasks()["tasks"]) == 2
    assert tasks_dir.exists()


def test_delete_last_task_does_not_trigger_cleanup(tmp_path: Path) -> None:
    """status="deleted" 删除路径不引发清理异常，任务文件被删除。"""
    tasks_dir = tmp_path / "tasks"
    mgr = TaskManager(tasks_dir=tasks_dir)

    mgr.create("task 1", "desc 1")
    mgr.create("task 2", "desc 2")
    mgr.update("1", status="deleted")
    mgr.update("2", status="deleted")

    assert mgr.list_tasks() == {"tasks": []}
    assert not (tasks_dir / "1.json").exists()
    assert not (tasks_dir / "2.json").exists()


def test_cleanup_memory_mode_skips_disk() -> None:
    """纯内存模式（tasks_dir=None）下清理不触碰磁盘、不抛异常。"""
    mgr = TaskManager(tasks_dir=None, on_change=None)

    mgr.create("task 1", "desc 1")
    mgr.update("1", status="completed")

    assert mgr.cleanup_if_all_completed() is True
    assert mgr.list_tasks() == {"tasks": []}


def test_turn_end_cleanup_on_normal_done(tmp_path: Path) -> None:
    """轮末正常结束（无 LLM 错误/退出/命令）且全部任务 completed 时清空任务。"""
    tasks_dir = tmp_path / "tasks"
    mgr = TaskManager(tasks_dir=tasks_dir)
    mgr.create("task 1", "desc 1")
    mgr.update("1", status="completed")

    agent = object.__new__(Agent)
    agent._task_mgr = mgr
    ctx = types.SimpleNamespace(llm_error=None, exit_requested=False, command=None)

    asyncio.run(agent._cleanup_tasks_at_turn_end(ctx))

    assert mgr.list_tasks() == {"tasks": []}
    assert not tasks_dir.exists()


def test_turn_end_cleanup_skipped_on_llm_error(tmp_path: Path) -> None:
    """轮末 llm_error 非 None 时跳过清理，任务与磁盘目录保留。"""
    tasks_dir = tmp_path / "tasks"
    mgr = TaskManager(tasks_dir=tasks_dir)
    mgr.create("task 1", "desc 1")
    mgr.update("1", status="completed")

    agent = object.__new__(Agent)
    agent._task_mgr = mgr
    ctx = types.SimpleNamespace(llm_error=object(), exit_requested=False, command=None)

    asyncio.run(agent._cleanup_tasks_at_turn_end(ctx))

    assert len(mgr.list_tasks()["tasks"]) == 1
    assert tasks_dir.exists()


def test_turn_end_cleanup_skipped_on_exit_or_command(tmp_path: Path) -> None:
    """轮末 exit_requested=True 或 command 非 None 时跳过清理，任务与磁盘目录保留。"""
    for exit_requested, command in ((True, None), (False, ("clear", []))):
        tasks_dir = tmp_path / f"tasks-exit-{exit_requested}-cmd-{command is not None}"
        mgr = TaskManager(tasks_dir=tasks_dir)
        mgr.create("task 1", "desc 1")
        mgr.update("1", status="completed")

        agent = object.__new__(Agent)
        agent._task_mgr = mgr
        ctx = types.SimpleNamespace(llm_error=None, exit_requested=exit_requested, command=command)


        assert len(mgr.list_tasks()["tasks"]) == 1
        assert tasks_dir.exists()


def test_task_notifier_delivers_from_loop_and_worker_thread() -> None:
    """_make_task_notifier 在事件循环线程与 worker 线程调用都应投递 TaskStateChanged。"""
    async def run() -> None:
        bus = EventBus()
        notifier = _make_task_notifier(bus)
        it = bus.subscribe(event_types={TaskStateChanged})
        try:
            # 事件循环线程调用：首次调用应捕获 loop 引用
            notifier([{"id": "1", "subject": "t1"}])
            first = await asyncio.wait_for(anext(it), timeout=2)
            # worker 线程调用：应复用缓存的 loop 投递，而非丢弃事件
            await asyncio.to_thread(notifier, [{"id": "2", "subject": "t2"}])
            second = await asyncio.wait_for(anext(it), timeout=2)
        finally:
            await it.aclose()
        assert [e.tasks for e in (first, second)] == [
            [{"id": "1", "subject": "t1"}],
            [{"id": "2", "subject": "t2"}],
        ]

    asyncio.run(run())
