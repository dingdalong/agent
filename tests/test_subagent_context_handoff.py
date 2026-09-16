"""委派链路的共享上下文交接测试。

注入点刻意选在 `SubAgentMgr.task_delegator` 而非 `ReminderMgr`，本文件因此不受
「agent 层用例用 NoopReminder 整体替换 `_reminder_mgr`、走 ReminderMgr 的新逻辑会
全绿地失效」这个已知测试盲区影响，可以真正验证注入与记账。

并行隔离用例（`test_parallel_delegations_are_isolated`）是本文件的核心：它锁死的
正是选 task_delegator 想避开的那类 bug——把「本次委派要注入什么」存到进程级单例的
槽位上，会被并发的 `asyncio.gather` 委派互相覆盖。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent import Agent
from src.mgr.context_mgr import ContextMgr
from src.mgr.role_mgr import AgentManifest
from src.mgr.subagent_mgr import SubAgentMgr
from src.mode import RunMode

_REPORT = "结论：注入点在 task_delegator，见 src/mgr/subagent_mgr.py:291 处的 run 调用。"


def _build_mgr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    context_mgr: ContextMgr | None,
    agent_types: tuple[str, ...] = ("explore",),
    final_text: str = _REPORT,
    llm_error: object = None,
    run_hook: object = None,
) -> tuple[SubAgentMgr, list[str]]:
    """构造一个只跑 stub 子 agent 的 SubAgentMgr。

    刻意不 stub `agent.run` 之外的东西（尤其不替换 `_reminder_mgr`），让 prompt
    的拼接顺序走真实链路。

    Args:
        tmp_path: 测试工作目录。
        monkeypatch: pytest 补丁夹具。
        context_mgr: 注入 deps 的账本；None 表示 deps 上没有该属性。
        agent_types: 需要注册的子 agent 类型。
        final_text: stub 子 agent 的返回文本。
        llm_error: stub 子 agent 的 llm_error 字段。
        run_hook: 可选协程，在 run 返回前 await，用于制造并发交错。

    Returns:
        (管理器, 捕获到的 prompt 列表)。
    """
    prompts: list[str] = []

    class _Child:
        """记录收到的 prompt 的最小子 agent 替身。"""

        uuid = "child"
        history: list[dict[str, str]] = []

        async def run(self, prompt: str) -> SimpleNamespace:
            """记录 prompt 并返回固定结果。

            Args:
                prompt: 子任务提示词。

            Returns:
                运行结果。
            """
            prompts.append(prompt)
            if run_hook is not None:
                await run_hook()
            return SimpleNamespace(final_text=final_text, llm_error=llm_error)

    def _from_manifest(cls, manifest, deps, **overrides):
        """返回子 agent 替身。

        Args:
            cls: Agent 类。
            manifest: 子 agent manifest。
            deps: Agent 依赖。
            **overrides: 构造覆盖字段。

        Returns:
            子 agent 替身。
        """
        del cls, manifest, deps, overrides
        return _Child()

    monkeypatch.setattr(Agent, "from_manifest", classmethod(_from_manifest))

    mgr = object.__new__(SubAgentMgr)
    mgr.workdir = tmp_path
    mgr.global_dir = None
    mgr._documents = {
        name: AgentManifest(
            agent_type=name,
            description="test",
            path=tmp_path / f"{name}.md",
        )
        for name in agent_types
    }
    deps_fields = {
        "hooks_mgr": None,
        "event_bus": None,
    }
    if context_mgr is not None:
        deps_fields["context_mgr"] = context_mgr
    mgr.deps = SimpleNamespace(**deps_fields)
    return mgr, prompts


def _parent() -> SimpleNamespace:
    """构造最小父 agent 替身。

    Returns:
        父 agent 替身。
    """
    return SimpleNamespace(
        mode=RunMode.EXECUTE,
        llm=SimpleNamespace(model="m"),
        enable_thinking=True,
        reasoning_effort=None,
        features=set(),
        _task_mgr=None,
    )


def _ledger(tmp_path: Path, **kwargs: object) -> ContextMgr:
    """构造已绑定会话的账本。

    Args:
        tmp_path: 测试工作目录。
        **kwargs: 覆盖 ContextMgr 构造参数。

    Returns:
        账本实例。
    """
    mgr = ContextMgr(workdir=tmp_path, **kwargs)
    mgr.bind_session("sess")
    return mgr


# —— 注入 ——

def test_empty_ledger_leaves_prompt_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """账本为空时委派 prompt 逐字节不变——零侵入回归护栏。"""
    ledger = _ledger(tmp_path)
    mgr, prompts = _build_mgr(tmp_path, monkeypatch, context_mgr=ledger)

    asyncio.run(mgr.task_delegator("explore", "去查一下 A", parent_agent=_parent()))

    assert prompts == ["去查一下 A"]


def test_digest_is_prepended_with_task_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """摘要在前、任务正文在最后（recency），且摘要块闭合完整。"""
    ledger = _ledger(tmp_path)
    ledger.add(kind="note", topic="既有发现", content=_REPORT, author="plan")
    mgr, prompts = _build_mgr(tmp_path, monkeypatch, context_mgr=ledger)

    asyncio.run(mgr.task_delegator("explore", "去查一下 A", parent_agent=_parent()))

    prompt = prompts[0]
    assert prompt.startswith("<shared_context>")
    assert prompt.endswith("去查一下 A")
    assert prompt.index("</shared_context>") < prompt.index("去查一下 A")


def test_shared_context_none_disables_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shared_context="none" 完全隔离，供独立复核使用。"""
    ledger = _ledger(tmp_path)
    ledger.add(kind="note", topic="既有发现", content=_REPORT, author="plan")
    mgr, prompts = _build_mgr(tmp_path, monkeypatch, context_mgr=ledger)

    asyncio.run(mgr.task_delegator(
        "explore", "独立复核", parent_agent=_parent(), shared_context="none",
    ))

    assert prompts == ["独立复核"]


def test_missing_context_mgr_is_harmless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """deps 上没有 context_mgr（角色未启用 subagent feature）时不注入、不报错。"""
    mgr, prompts = _build_mgr(tmp_path, monkeypatch, context_mgr=None)

    result = asyncio.run(mgr.task_delegator("explore", "去查", parent_agent=_parent()))

    assert result == _REPORT
    assert prompts == ["去查"]


def test_disabled_ledger_does_not_inject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """总开关关闭时行为与无账本一致，便于 A/B 对比。"""
    ledger = _ledger(tmp_path, enabled=False)
    mgr, prompts = _build_mgr(tmp_path, monkeypatch, context_mgr=ledger)

    asyncio.run(mgr.task_delegator("explore", "去查", parent_agent=_parent()))

    assert prompts == ["去查"]


# —— 记账 ——

def test_successful_delegation_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功返回自动入账，标题取委派 description、作者取子 agent 类型。"""
    ledger = _ledger(tmp_path)
    mgr, _ = _build_mgr(tmp_path, monkeypatch, context_mgr=ledger)

    asyncio.run(mgr.task_delegator(
        "explore", "去查", parent_agent=_parent(), description="定位注入点",
    ))

    assert ledger.entry_ids() == ["c1"]
    entry = ledger._entries[0]
    assert entry.kind == "delegation"
    assert entry.topic == "定位注入点"
    assert entry.author == "explore"
    assert entry.content == _REPORT


def test_blank_description_falls_back_to_agent_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """description 为空时用 agent_type 兜底，不产生无名条目。"""
    ledger = _ledger(tmp_path)
    mgr, _ = _build_mgr(tmp_path, monkeypatch, context_mgr=ledger)

    asyncio.run(mgr.task_delegator("explore", "去查", parent_agent=_parent()))

    assert ledger._entries[0].topic == "explore"


def test_llm_error_is_not_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM 终态失败的返回不入账，避免把错误文本当成已核实事实。"""
    ledger = _ledger(tmp_path)
    mgr, _ = _build_mgr(
        tmp_path, monkeypatch, context_mgr=ledger,
        final_text="错误：LLM 调用失败（network）", llm_error=SimpleNamespace(kind="network"),
    )

    asyncio.run(mgr.task_delegator("explore", "去查", parent_agent=_parent()))

    assert ledger.entry_ids() == []


def test_agent_type_outside_whitelist_is_not_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """白名单外的子 agent（如 shell）不记账，避免"命令执行完毕"挤占预算。"""
    ledger = _ledger(tmp_path)
    mgr, _ = _build_mgr(
        tmp_path, monkeypatch, context_mgr=ledger, agent_types=("shell",),
    )

    asyncio.run(mgr.task_delegator("shell", "跑测试", parent_agent=_parent()))

    assert ledger.entry_ids() == []


def test_trivial_report_is_not_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无信息量的短返回不入账。"""
    ledger = _ledger(tmp_path)
    mgr, _ = _build_mgr(tmp_path, monkeypatch, context_mgr=ledger, final_text="已完成")

    asyncio.run(mgr.task_delegator("explore", "去查", parent_agent=_parent()))

    assert ledger.entry_ids() == []


def test_child_exception_is_not_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子 agent 抛异常时不入账，与任务状态回滚语义一致。"""
    ledger = _ledger(tmp_path)

    async def _explode() -> None:
        raise RuntimeError("boom")

    mgr, _ = _build_mgr(
        tmp_path, monkeypatch, context_mgr=ledger, run_hook=_explode,
    )

    with pytest.raises(RuntimeError):
        asyncio.run(mgr.task_delegator("explore", "去查", parent_agent=_parent()))

    assert ledger.entry_ids() == []


def test_unknown_agent_type_is_not_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不存在的子 agent 直接返回错误串，不入账。"""
    ledger = _ledger(tmp_path)
    mgr, _ = _build_mgr(tmp_path, monkeypatch, context_mgr=ledger)

    result = asyncio.run(mgr.task_delegator("nope", "去查", parent_agent=_parent()))

    assert result.startswith("错误")
    assert ledger.entry_ids() == []


def test_recorded_content_includes_stop_hook_additions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """记的是父 agent 实际收到的文本，含 SubagentStop hook 追加的内容。

    记账点必须在 hook 处理之后，否则账本与主 agent 看到的内容会不一致。

    Args:
        tmp_path: 测试工作目录。
        monkeypatch: pytest 补丁夹具。

    Returns:
        None。
    """
    ledger = _ledger(tmp_path)
    mgr, _ = _build_mgr(tmp_path, monkeypatch, context_mgr=ledger)

    class _HooksStub:
        """只对 SubagentStop 追加内容的 hooks 替身。"""

        async def run_event(self, event: str, *_args: object, **_kwargs: object):
            """返回 hook 结果。

            Args:
                event: 事件名。

            Returns:
                hook 结果。
            """
            if event == "SubagentStop":
                return SimpleNamespace(
                    blocked=False, block_reason=None, additional_context=["hook 追加的补充"],
                )
            return SimpleNamespace(blocked=False, block_reason=None, additional_context=[])

    mgr.deps.hooks_mgr = _HooksStub()
    mgr.deps.session_id = "s"

    result = asyncio.run(mgr.task_delegator("explore", "去查", parent_agent=_parent()))

    assert "hook 追加的补充" in result
    assert ledger._entries[0].content == result


# —— 并发隔离（本文件的核心用例）——

def test_parallel_delegations_are_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并行委派各自的注入内容互不串扰，且两条都记了账。

    计划工作流允许同一轮并行委派多个 explore。若把"本次委派注入什么"存到进程级
    单例的槽位上，`asyncio.gather` 会让它们互相覆盖——这正是 PlanMgr 的
    共享提醒消费槽位已经踩过的坑。注入点放在
    task_delegator 的局部变量里天然免疫。

    Args:
        tmp_path: 测试工作目录。
        monkeypatch: pytest 补丁夹具。

    Returns:
        None。
    """
    ledger = _ledger(tmp_path)

    async def _yield() -> None:
        """让出控制权，制造两个委派的执行交错。"""
        await asyncio.sleep(0)

    mgr, prompts = _build_mgr(
        tmp_path, monkeypatch, context_mgr=ledger, run_hook=_yield,
    )

    async def _run() -> None:
        await asyncio.gather(
            mgr.task_delegator(
                "explore", "任务甲", parent_agent=_parent(), description="甲",
            ),
            mgr.task_delegator(
                "explore", "任务乙", parent_agent=_parent(), description="乙",
            ),
        )

    asyncio.run(_run())

    # 两个委派开始时账本都还是空的，谁都不该看到对方的任务正文
    assert sorted(prompts) == ["任务乙", "任务甲"]
    assert len(ledger.entry_ids()) == 2
    assert {entry.topic for entry in ledger._entries} == {"甲", "乙"}


def test_later_delegation_sees_earlier_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """串行委派时，后一个子 agent 能看到前一个的报告——这就是整个机制的目的。"""
    ledger = _ledger(tmp_path)
    mgr, prompts = _build_mgr(tmp_path, monkeypatch, context_mgr=ledger)
    parent = _parent()

    async def _run() -> None:
        await mgr.task_delegator("explore", "先查", parent_agent=parent, description="第一步")
        await mgr.task_delegator("explore", "再查", parent_agent=parent, description="第二步")

    asyncio.run(_run())

    assert prompts[0] == "先查"
    assert "第一步" in prompts[1]
    assert _REPORT in prompts[1]
    assert prompts[1].endswith("再查")
