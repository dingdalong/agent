"""跨 agent 共享上下文账本测试。

三条性质必须锁死：落盘是**追加**而非全量重写（否则写入成本随条目数平方增长）、
注入文本**有界**（它每次委派都要付一遍）、以及**并发写不交错**（计划工作流允许
同一轮并行委派多个 explore）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.mgr.context_mgr import ContextMgr

_LONG = "这是一条足够长的结论，说明在 src/mgr/foo.py:12 处确认了某个事实，可直接采信。"


class _GuardStub:
    """把固定敏感串替换成掩码的 data_guard 替身。"""

    def redact(self, text: str) -> str:
        """脱敏文本。

        Args:
            text: 原始文本。

        Returns:
            替换后的文本。
        """
        return text.replace("sk-secret", "[REDACTED]")


def _mgr(tmp_path: Path, **kwargs: object) -> ContextMgr:
    """构造绑定好会话的账本实例。

    Args:
        tmp_path: 测试工作目录。
        **kwargs: 覆盖 ContextMgr 的构造参数。

    Returns:
        已绑定会话的 ContextMgr。
    """
    mgr = ContextMgr(workdir=tmp_path, **kwargs)
    mgr.bind_session("sess")
    return mgr


# —— 写入 ——

def test_add_assigns_increasing_ids_and_fields(tmp_path: Path) -> None:
    """条目 id 单调递增，各字段原样落位。"""
    mgr = _mgr(tmp_path)

    first = mgr.add(kind="note", topic="主题一", content=_LONG, author="explore", refs=["a.py:1"])
    second = mgr.add(kind="delegation", topic="主题二", content=_LONG, author="plan")

    assert (first.id, second.id) == ("c1", "c2")
    assert first.kind == "note"
    assert first.topic == "主题一"
    assert first.refs == ("a.py:1",)
    assert first.author == "explore"
    assert second.refs == ()


def test_short_content_is_not_recorded(tmp_path: Path) -> None:
    """无信息量的短返回（"已完成"、"错误：…"）不进账本。"""
    mgr = _mgr(tmp_path)

    assert mgr.add(kind="delegation", topic="t", content="已完成", author="shell") is None
    assert mgr.entry_ids() == []


@pytest.mark.parametrize(
    "content",
    [
        "结论：权限唯一入口是 authorize()，见 src/mgr/permission_mgr.py:94。",
        "子 agent 的 history 从空开始，只收 prompt 一条消息。",
    ],
    ids=["with-ref", "plain"],
)
def test_short_chinese_conclusions_are_still_recorded(
    tmp_path: Path, content: str,
) -> None:
    """字数不多但有实质结论的中文条目必须留下。

    最短记账阈值若按英文习惯定在 40 字符，会把大量 25-40 字的有效中文结论误杀——
    中文单字承载的信息量远高于英文单字符。本用例锁死这个边界。

    Args:
        tmp_path: 测试工作目录。
        content: 一条真实长度的中文结论。

    Returns:
        None。
    """
    assert _mgr(tmp_path).add(
        kind="delegation", topic="t", content=content, author="explore",
    ) is not None


def test_disabled_manager_records_nothing(tmp_path: Path) -> None:
    """总开关关闭时完全退化：不记账、不渲染、不落盘。"""
    mgr = _mgr(tmp_path, enabled=False)

    assert mgr.add(kind="note", topic="t", content=_LONG, author="explore") is None
    assert mgr.digest() == ""
    assert not mgr.note_path().exists()


def test_content_is_redacted(tmp_path: Path) -> None:
    """正文、标题与 refs 都经过 data_guard 脱敏后才入账。"""
    mgr = _mgr(tmp_path, data_guard=_GuardStub())

    entry = mgr.add(
        kind="note",
        topic="密钥 sk-secret 的位置",
        content=f"{_LONG} 密钥是 sk-secret 请注意。",
        author="explore",
        refs=["sk-secret.py:1"],
    )

    assert "sk-secret" not in entry.content
    assert "sk-secret" not in entry.topic
    assert "sk-secret" not in entry.refs[0]
    assert "[REDACTED]" in entry.content


def test_oversized_content_is_truncated_on_store(tmp_path: Path) -> None:
    """超过存储上限的正文被截尾，避免单条撑爆内存账本。"""
    mgr = _mgr(tmp_path, max_entry_chars=100)

    entry = mgr.add(kind="note", topic="t", content="x" * 500, author="explore")

    assert len(entry.content) < 200
    assert "正文过长已截断" in entry.content


def test_entry_cap_evicts_oldest(tmp_path: Path) -> None:
    """条目数超上限时丢最旧，并计入 _dropped 供注入时提示。"""
    mgr = _mgr(tmp_path, max_entries=3)

    for index in range(6):
        mgr.add(kind="note", topic=f"t{index}", content=_LONG, author="explore")

    assert mgr.entry_ids() == ["c4", "c5", "c6"]
    assert mgr._dropped == 3


# —— 注入渲染 ——

def test_empty_ledger_renders_nothing(tmp_path: Path) -> None:
    """空账本渲染为空串，保证委派 prompt 逐字节不变。"""
    assert _mgr(tmp_path).digest() == ""


def test_digest_is_newest_first(tmp_path: Path) -> None:
    """按时间倒序渲染——越新的事实越相关、越不容易过时。"""
    mgr = _mgr(tmp_path)
    mgr.add(kind="note", topic="最早", content=_LONG, author="explore")
    mgr.add(kind="note", topic="最新", content=_LONG, author="plan")

    text = mgr.digest()

    assert text.index("最新") < text.index("最早")


def test_digest_respects_total_and_per_entry_budget(tmp_path: Path) -> None:
    """总预算与单条上限同时生效，超出部分给出可追溯的提示。"""
    mgr = _mgr(tmp_path, inject_char_budget=400, inject_entry_chars=120)
    for index in range(6):
        mgr.add(kind="note", topic=f"t{index}", content="y" * 600, author="explore")

    text = mgr.digest()

    assert len(text) < 1200
    assert "正文已截断" in text
    assert "未展示" in text
    assert str(mgr.note_path()) in text  # 被截断的部分必须能找回


def test_digest_always_renders_at_least_one_entry(tmp_path: Path) -> None:
    """即使单条就超预算也要渲染它，否则注入永远为空。"""
    mgr = _mgr(tmp_path, inject_char_budget=10, inject_entry_chars=50)
    mgr.add(kind="note", topic="t", content="z" * 400, author="explore")

    assert "[c1]" in mgr.digest()


def test_digest_header_states_facts_are_not_the_task(tmp_path: Path) -> None:
    """注入头部必须声明冲突优先级与"这不是你的任务"，防止范围蔓延。"""
    mgr = _mgr(tmp_path)
    mgr.add(kind="note", topic="t", content=_LONG, author="explore")

    text = mgr.digest()

    assert text.startswith("<shared_context>")
    assert text.endswith("</shared_context>")
    assert "以你现在读到的为准" in text
    assert "不构成任务" in text


def test_digest_include_ids_filters(tmp_path: Path) -> None:
    """指定 id 时只渲染选中的条目。"""
    mgr = _mgr(tmp_path)
    mgr.add(kind="note", topic="要的", content=_LONG, author="explore")
    mgr.add(kind="note", topic="不要的", content=_LONG, author="plan")

    text = mgr.digest(include_ids=["c1"])

    assert "要的" in text
    assert "不要的" not in text


def test_relative_age_is_rendered(tmp_path: Path) -> None:
    """条目带相对时间，供 LLM 判断新鲜度。"""
    mgr = _mgr(tmp_path)
    entry = mgr.add(kind="note", topic="t", content=_LONG, author="explore")

    assert "分钟前" in mgr.digest(now=entry.created_at + 300)


# —— 过时标记 ——

def test_mark_stale_flags_entries_mentioning_written_files(tmp_path: Path) -> None:
    """写过的文件让提及它的条目被标记，其余条目不受影响。"""
    mgr = _mgr(tmp_path)
    mgr.add(
        kind="delegation", topic="相关", author="explore",
        content="结论：核心逻辑在 src/mgr/foo.py:12，已核实其调用方只有一个。",
    )
    mgr.add(
        kind="delegation", topic="无关", author="plan",
        content="结论：文档索引在 docs/README.md，与本次改动没有关系，可以忽略。",
    )

    marked = mgr.mark_stale([tmp_path / "src" / "mgr" / "foo.py"])
    text = mgr.digest()

    assert marked == 1
    assert text.count("⚠") == 1
    assert "需重新核实" in text


def test_mark_stale_matches_refs_as_well(tmp_path: Path) -> None:
    """refs 命中也算过时，不只看正文。"""
    mgr = _mgr(tmp_path)
    mgr.add(
        kind="note", topic="t", author="explore",
        content="一条不含任何路径的结论文本，仅通过 refs 字段给出定位信息。",
        refs=["src/app/bar.py:3"],
    )

    assert mgr.mark_stale([tmp_path / "src" / "app" / "bar.py"]) == 1


def test_mark_stale_ignores_writes_outside_workdir(tmp_path: Path) -> None:
    """工作目录外的写入与账本无关。"""
    mgr = _mgr(tmp_path)
    mgr.add(kind="note", topic="t", content=_LONG, author="explore")

    assert mgr.mark_stale([Path("/etc/hosts")]) == 0


def test_mark_stale_is_idempotent(tmp_path: Path) -> None:
    """已标记的条目不重复计数。"""
    mgr = _mgr(tmp_path)
    mgr.add(
        kind="note", topic="t", author="explore",
        content="结论：核心逻辑在 src/mgr/foo.py:12，已核实其调用方只有一个。",
    )
    target = [tmp_path / "src" / "mgr" / "foo.py"]

    assert mgr.mark_stale(target) == 1
    assert mgr.mark_stale(target) == 0


# —— 落盘 ——

def test_record_appends_without_rewriting(tmp_path: Path) -> None:
    """第二条落盘后第一条仍在——锁死"追加而非全量重写"。"""
    async def _run() -> str:
        mgr = _mgr(tmp_path)
        await mgr.record(kind="delegation", topic="第一条", content=_LONG, author="explore")
        await mgr.record(kind="note", topic="第二条", content=_LONG, author="plan")
        return mgr.note_path().read_text()

    text = asyncio.run(_run())

    assert text.count("## [") == 2
    assert "第一条" in text
    assert "第二条" in text
    assert text.startswith("# 会话共享上下文 sess")


def test_record_writes_parsable_headers(tmp_path: Path) -> None:
    """头行机器可解析，为将来回读账本留口。"""
    async def _run() -> str:
        mgr = _mgr(tmp_path)
        await mgr.record(
            kind="note", topic="t", content=_LONG, author="explore", refs=["a.py:1"],
        )
        return mgr.note_path().read_text()

    text = asyncio.run(_run())

    assert "## [c1] note · explore · " in text
    assert "refs: a.py:1" in text


def test_concurrent_records_do_not_interleave(tmp_path: Path) -> None:
    """并行委派同时落盘时各块完整，不互相插入。

    计划工作流允许同一轮并行委派多个 explore，落盘必须串行化。

    Args:
        tmp_path: 测试工作目录。

    Returns:
        None。
    """
    async def _run() -> str:
        mgr = _mgr(tmp_path)
        await asyncio.gather(*(
            mgr.record(
                kind="delegation",
                topic=f"并行任务{index}",
                content=f"{_LONG} 编号 {index}。",
                author="explore",
            )
            for index in range(5)
        ))
        return mgr.note_path().read_text()

    text = asyncio.run(_run())

    assert text.count("## [") == 5
    for index in range(5):
        assert f"并行任务{index}" in text
    # 每个头行都独占一行，没有被别的块截断
    for line in text.splitlines():
        assert line.count("## [") <= 1


def test_record_survives_disk_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """落盘失败只告警，内存条目仍在——一次磁盘故障不能拖垮委派。"""
    async def _run() -> ContextMgr:
        mgr = _mgr(tmp_path)

        def _boom(_entry: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(mgr, "_append_to_disk", _boom)
        await mgr.record(kind="note", topic="t", content=_LONG, author="explore")
        return mgr

    mgr = asyncio.run(_run())

    assert mgr.entry_ids() == ["c1"]
    assert "[c1]" in mgr.digest()


# —— 生命周期 ——

def test_reload_clears_memory_but_keeps_file(tmp_path: Path) -> None:
    """/clear 清内存与序号，磁盘旧记录保留供事后排查。"""
    async def _run() -> tuple[ContextMgr, Path]:
        mgr = _mgr(tmp_path)
        await mgr.record(kind="note", topic="t", content=_LONG, author="explore")
        path = mgr.note_path()
        mgr.reload()
        return mgr, path

    mgr, path = asyncio.run(_run())

    assert mgr.entry_ids() == []
    assert mgr.digest() == ""
    assert path.exists()
    assert mgr.add(kind="note", topic="t", content=_LONG, author="x").id == "c1"


def test_bind_session_switches_target_file(tmp_path: Path) -> None:
    """切换会话后写入新文件，不污染旧会话记录。"""
    mgr = _mgr(tmp_path)
    first = mgr.note_path()
    mgr.bind_session("other")

    assert mgr.note_path() != first
    assert mgr.note_path().name == "other.md"
