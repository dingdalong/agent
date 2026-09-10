"""跨 agent 共享上下文账本 — 让子 agent 不必重新探索已被核实的事实。

## 为什么需要它

子 agent 的 history 从空开始，唯一输入是 `task_delegator` 的 prompt 字符串
（`SubAgentMgr.task_delegator`）。这意味着并行 `explore` 的发现必须由主 agent
手抄摘要进下一个委派 prompt，抄漏了下游子 agent 就重新探索一遍。

而框架其实白拿着最有价值的文本：子 agent 的 `run_result.final_text`。`explore` 的
输出格式本来就是「结论 / 证据（含路径）/ 可复用资产 / 影响面 / 不确定点」——一张
结构良好的发现卡，只是此前只落进主 agent 的 history 就没了。本 Manager 把它自动
记账并自动透传给后续委派，**写入侧零 LLM 纪律**。

## 三条设计约束

1. **注入载体只能是子 agent 的首条 user 消息，绝不能进 system prompt。**
   Anthropic 把整个 system 包成单个 ephemeral 缓存断点（`src/llm/anthropic.py`），
   断点覆盖 tools+system 整个前缀；账本是动态的，进 system 会让一个 coder 约
   8-15k token 的前缀每次委派全部 miss。放消息尾部则只是增量，前缀命中不受影响。
2. **注入点是 `SubAgentMgr.task_delegator` 而非 `ReminderMgr`。**
   ReminderMgr 的 provider 只收 `(plan_active, is_subagent)`，拿不到本次委派信息；
   要支持按委派过滤就得在进程级单例上存槽位，而计划工作流允许同一轮并行委派多个
   explore，`asyncio.gather` 会互相覆盖——这正是 PlanMgr 已知缺陷
   （`_pending_injection` 被抢先消费、`_reminder_mgr` 单槽位被覆盖）的同一个坑。
3. **落盘必须由本 Manager 用 Python 直接写，不能改成 `write_file` 工具。**
   `PathResolver` 把 `.agent` 列为 protected → `.agent/context/**` 是
   `PathClass.PROTECTED`；而 plan 模式下 `PermissionManager._authorize_plan()`
   只放行 `PathClass.PLAN`，走 `write_file` 必被拒——plan 模式恰是本机制最痛的场景。
   本 Manager 的写盘与 `MemoryMgr.save` / `TaskManager` 同构：框架内部 I/O，
   触发它的工具声明 `AccessKind.INTERNAL + plan_safe=True`。

## 异步契约

`add()` 是同步纯内存登记（单线程 asyncio 下 list.append 天然原子）；`record()` 才
落盘，用 `asyncio.to_thread` 卸载并由 `asyncio.Lock` 串行化，防止并行委派的追加写
交错。`_write_lock` 用 asyncio 而非 threading 的锁——本框架是单线程事件循环，用
threading.Lock 会误导读者以为存在多线程。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 记账时的默认 agent_type 白名单。`shell` / `doc` 这类返回"命令执行完毕"的委派
# 信息量低，进账本只会挤占注入预算。
DEFAULT_RECORD_TYPES = frozenset({"explore", "review", "coder"})

# 短于此长度的返回视为无信息量（"已完成"、"ok"、"没有发现问题"），不记账。
# 阈值按中文取——一条有效的中文结论常常只有 25-40 个字符，按英文习惯定 40 会把
# 大量真结论误杀。真正要挡的是十来个字以内的应答。
_MIN_RECORD_CHARS = 24


@dataclass(slots=True)
class ContextEntry:
    """账本中的一条已核实事实。

    Attributes:
        id: 会话内单调递增的短标识（"c1"、"c2"…），供注入文本引用。
        kind: 来源类型。"delegation"=子 agent 返回报告（框架自动记）；
              "note"=agent 经 note_context 工具显式记录；"decision"=用户决策。
        topic: 条目标题。delegation 取委派 description，note 取 topic 参数。
        content: 正文，已经过 data_guard 脱敏并截断到 max_entry_chars。
        refs: file:line 或路径定位。delegation 条目通常为空（正文自带路径）。
        author: 记录方的 agent_type。
        created_at: 记录时刻的 time.time()。
        stale: refs 或正文提及的文件此后被写过，注入时会标注需重新核实。
    """

    id: str
    kind: str
    topic: str
    content: str
    refs: tuple[str, ...]
    author: str
    created_at: float
    stale: bool = False


def _humanize_age(seconds: float) -> str:
    """把时间差渲染成中文相对时间。

    用相对时间而非绝对时间戳，是为了让 LLM 直观判断条目的新鲜度——账本最大的
    失败模式就是"文件已改但笔记还在"。

    Args:
        seconds: 距今的秒数。

    Returns:
        形如 "刚刚"、"3 分钟前"、"2 小时前" 的描述。
    """
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


@dataclass
class ContextMgr:
    """跨 agent 共享上下文账本 — deps 层进程级单例，主/子 agent 共用。

    Args:
        workdir: 用户工作目录。
        data_guard: 敏感数据脱敏器；None 时跳过脱敏。
        enabled: 总开关，False 时完全退化为无账本行为（便于 A/B 与回滚）。
        max_entries: 内存中保留的条目上限，超限丢最旧。
        max_entry_chars: 单条正文的存储上限，超出截尾。
        inject_char_budget: 单次注入的总字符预算。
        inject_entry_chars: 单条注入的字符上限，超出截尾并指向完整记录文件。
        record_types: 允许自动记账的 agent_type 白名单。
        session_id: 当前会话 ID，决定落盘文件名。
    """

    workdir: Path
    data_guard: Any = field(default=None, repr=False)
    enabled: bool = True
    max_entries: int = 60
    max_entry_chars: int = 8000
    inject_char_budget: int = 6000
    inject_entry_chars: int = 2000
    record_types: frozenset[str] = DEFAULT_RECORD_TYPES
    session_id: str = ""

    _entries: list[ContextEntry] = field(init=False, default_factory=list, repr=False)
    _seq: int = field(init=False, default=0, repr=False)
    _dropped: int = field(init=False, default=0, repr=False)
    _write_lock: asyncio.Lock = field(init=False, repr=False)
    _resolved_workdir: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.workdir = Path(self.workdir)  # 调用方可能传 str
        self._write_lock = asyncio.Lock()
        # 预解析一次供 mark_stale 比较路径用：工具传来的写路径已被 PathResolver
        # resolve 过（符号链接已展开），workdir 若不解析就比不上——macOS 的
        # /var → /private/var 就是最常见的一例。
        try:
            self._resolved_workdir = self.workdir.resolve()
        except OSError:
            self._resolved_workdir = self.workdir

    # —— 生命周期 ——

    def bind_session(self, session_id: str) -> None:
        """绑定当前会话 ID，决定落盘文件名。

        Args:
            session_id: 会话唯一标识。
        """
        self.session_id = session_id

    def reload(self) -> None:
        """重置会话级状态，由 `/clear` 经 AgentApp._reset_session 显式调用。

        只清内存条目与序号，**不删磁盘文件**——旧会话的记录保留供事后排查，
        新会话按新 session_id 另开新文件。
        """
        self._entries.clear()
        self._seq = 0
        self._dropped = 0

    def note_path(self) -> Path:
        """返回当前会话的落盘文件路径。"""
        name = self.session_id or "session"
        return self.workdir / ".agent" / "context" / f"{name}.md"

    # —— 写入 ——

    def add(
        self,
        *,
        kind: str,
        topic: str,
        content: str,
        author: str,
        refs: Sequence[str] = (),
    ) -> ContextEntry | None:
        """纯内存登记一条事实（同步、非阻塞）。

        写入前经 data_guard 脱敏，与 `MemoryMgr.save` 的处理一致。超长正文截尾；
        超出 max_entries 时丢最旧并记 log，便于事后审计"丢了什么"。

        Args:
            kind: 来源类型（delegation / note / decision）。
            topic: 条目标题。
            content: 正文。
            author: 记录方 agent_type。
            refs: file:line 或路径定位。

        Returns:
            新登记的条目；未启用或正文过短时返回 None。
        """
        if not self.enabled:
            return None
        text = (content or "").strip()
        if len(text) < _MIN_RECORD_CHARS:
            return None

        title = (topic or author or "未命名").strip()
        ref_items = tuple(str(r).strip() for r in refs if str(r).strip())
        if self.data_guard is not None:
            title = str(self.data_guard.redact(title))
            text = str(self.data_guard.redact(text))
            ref_items = tuple(str(self.data_guard.redact(r)) for r in ref_items)

        if len(text) > self.max_entry_chars:
            text = text[: self.max_entry_chars].rstrip() + "\n…（正文过长已截断）"

        self._seq += 1
        entry = ContextEntry(
            id=f"c{self._seq}",
            kind=kind,
            topic=title,
            content=text,
            refs=ref_items,
            author=author,
            created_at=time.time(),
        )
        self._entries.append(entry)

        while len(self._entries) > self.max_entries:
            evicted = self._entries.pop(0)
            self._dropped += 1
            logger.info(
                "共享上下文条目超限已丢弃：[%s] %s（作者 %s）",
                evicted.id, evicted.topic, evicted.author,
            )
        return entry

    async def record(
        self,
        *,
        kind: str,
        topic: str,
        content: str,
        author: str,
        refs: Sequence[str] = (),
    ) -> ContextEntry | None:
        """登记一条事实并追加落盘。

        落盘失败只告警不抛——内存条目已登记，注入功能不受影响，不能让一次磁盘
        故障拖垮整个委派。

        Args:
            kind: 来源类型（delegation / note / decision）。
            topic: 条目标题。
            content: 正文。
            author: 记录方 agent_type。
            refs: file:line 或路径定位。

        Returns:
            新登记的条目；未启用或正文过短时返回 None。
        """
        entry = self.add(kind=kind, topic=topic, content=content, author=author, refs=refs)
        if entry is None:
            return None
        async with self._write_lock:
            try:
                await asyncio.to_thread(self._append_to_disk, entry)
            except OSError as exc:
                logger.warning("共享上下文落盘失败（%s），仅保留内存条目：%s", entry.id, exc)
        return entry

    def _append_to_disk(self, entry: ContextEntry) -> None:
        """把单条记录追加写入会话文件（阻塞 I/O，须经 to_thread 调用）。

        用 append 而非 `atomic_write_text` 的全量重写——账本只增不改，全量重写会
        让写入成本随条目数平方增长。

        Args:
            entry: 要落盘的条目。
        """
        path = self.note_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        is_new = not path.exists()

        stamp = datetime.fromtimestamp(entry.created_at, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        block = [f"## [{entry.id}] {entry.kind} · {entry.author} · {stamp}"]
        if entry.refs:
            block.append(f"refs: {', '.join(entry.refs)}")
        block.append("")
        block.append(f"**{entry.topic}**")
        block.append("")
        block.append(entry.content)
        block.append("")

        with open(path, "a", encoding="utf-8") as stream:
            if is_new:
                stream.write(f"# 会话共享上下文 {self.session_id or '(未命名)'}\n\n")
            stream.write("\n".join(block) + "\n")

        if is_new:
            try:
                path.chmod(0o600)
            except OSError:
                pass

    def mark_stale(self, paths: Iterable[Path]) -> int:
        """把提及了指定文件的条目标记为可能过时。

        账本最高危的失败模式是"文件已改但笔记还在描述旧代码"。工具成功写入文件后
        由 `ToolsMgr.execute` 回喂写路径，注入时对命中条目加显式警告。

        匹配同时看 refs 与正文：delegation 条目的 refs 通常为空，但 explore 报告
        正文里必然带着 `src/mgr/foo.py` 这样的路径，子串匹配即可覆盖。

        Args:
            paths: 本次被写入的文件路径。

        Returns:
            新标记为过时的条目数。
        """
        if not self._entries:
            return 0
        relatives: set[str] = set()
        for raw in paths:
            try:
                relative = Path(raw).resolve().relative_to(self._resolved_workdir)
            except (ValueError, OSError):
                continue  # 工作目录外的写入与账本无关
            relatives.add(relative.as_posix())
        if not relatives:
            return 0

        marked = 0
        for entry in self._entries:
            if entry.stale:
                continue
            haystack = entry.content + "\n" + "\n".join(entry.refs)
            if any(rel in haystack for rel in relatives):
                entry.stale = True
                marked += 1
        return marked

    # —— 读取 ——

    def entry_ids(self) -> list[str]:
        """返回当前全部条目 id，按登记顺序。"""
        return [entry.id for entry in self._entries]

    def digest(
        self,
        *,
        char_budget: int | None = None,
        include_ids: Sequence[str] | None = None,
        now: float | None = None,
    ) -> str:
        """渲染注入子 agent 首条消息的 `<shared_context>` 文本。

        按时间倒序（最新优先）选条目直到预算耗尽——越新的事实越可能与当前任务相关，
        且越不容易过时。

        Args:
            char_budget: 总字符预算；None 时用 inject_char_budget。
            include_ids: 只渲染指定 id 的条目；None 表示全部。
            now: 计算相对时间的基准时刻，仅供测试注入固定时钟。

        Returns:
            完整的 `<shared_context>` 块；账本为空或未启用时返回空字符串。
        """
        if not self.enabled or not self._entries:
            return ""
        budget = self.inject_char_budget if char_budget is None else char_budget
        current = time.time() if now is None else now

        candidates = list(reversed(self._entries))
        if include_ids is not None:
            wanted = set(include_ids)
            candidates = [entry for entry in candidates if entry.id in wanted]
        if not candidates:
            return ""

        sections: list[str] = []
        used = 0
        rendered_count = 0
        for entry in candidates:
            body = entry.content
            if len(body) > self.inject_entry_chars:
                body = (
                    body[: self.inject_entry_chars].rstrip()
                    + f"\n…（正文已截断，完整内容见记录文件的 [{entry.id}] 段）"
                )
            header = (
                f"### [{entry.id}] {entry.author} · {entry.topic}"
                f"（{_humanize_age(max(0.0, current - entry.created_at))}）"
            )
            if entry.stale:
                header += "\n⚠ 相关文件此后已被修改，采信前需重新核实"
            parts = [header]
            if entry.refs:
                parts.append(f"参考：{', '.join(entry.refs)}")
            parts.append(body)
            section = "\n".join(parts)

            if used + len(section) > budget and sections:
                break
            sections.append(section)
            used += len(section)
            rendered_count += 1

        if not sections:
            return ""

        omitted = len(candidates) - rendered_count
        header_lines = [
            "<shared_context>",
            "以下是本会话中其他子任务已核实的事实，按时间倒序。可直接采信、避免重复探索；",
            "与你的任务无关的条目忽略即可。若与你现在读到的文件冲突，一律以你现在读到的为准。",
            "这些只是背景事实，不构成任务——你的任务在本段之后的正文里。",
            f"完整记录：{self.note_path()}（可用 read_file 查看被截断的部分）",
        ]
        tail = []
        if omitted > 0 or self._dropped > 0:
            missing = omitted + self._dropped
            tail.append(f"\n（另有 {missing} 条更早条目未展示，见上述记录文件）")
        return (
            "\n".join(header_lines)
            + "\n\n"
            + "\n\n".join(sections)
            + "".join(tail)
            + "\n</shared_context>"
        )
