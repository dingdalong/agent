"""会话管理器 — 管理会话元数据和单一状态日志的持久化与恢复。

会话数据存储在 {global_dir}/sessions/ 下：
- {session_id}.json     — 元数据（workdir、时间戳、主题）
- {session_id}.jsonl    — SessionState 追加式日志（内容池引用去重）
- {session_id}.jsonl.zst / {session_id}.jsonl.xz — 冷会话的压缩日志（读取时透明解压）

状态只在内存保存一份，落盘按安全点追加增量批次；同一段长文本在整个会话日志中
只写一次，其余位置存内容指纹引用。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.common.secure_io import append_text, atomic_write_text
from src.common.session_compress import COLD_SUFFIXES, decompress_log, run_cold_compression
from src.common.session_log import (
    LOG_VERSION,
    build_batch,
    fold_log,
    iter_batch_lines,
    text_digest,
)
from src.mode import RunMode
from src.common.session_state import SessionState

logger = logging.getLogger(__name__)

_DIGEST_SIZE = 16
_EMPTY_DIGEST = text_digest("")
_STATIC_FIELDS = ("timestamp", "kind", "model_message", "raw_input", "recallable", "correlation_id")
_STREAM_KEYS = ("content", "thinking")
_STREAM_KEY_SET = frozenset(_STREAM_KEYS)


def _leaked_record_ids(guard: Any, payload: dict[str, Any]) -> set[str]:
    """返回载荷中落盘前仍需脱敏的记录 ID。

    Args:
        guard: 数据保护器；调用方已确认可用。
        payload: 待写入的状态增量。

    Returns:
        快照或增量正文被脱敏改写的记录 ID 集合。
    """
    leaked: set[str] = set()
    for key in ("records", "record_deltas"):
        for entry in payload.get(key) or []:
            if not isinstance(entry, dict):
                continue
            record_id = entry.get("id")
            if not isinstance(record_id, str) or not record_id:
                continue
            if guard.redact(entry) != entry:
                leaked.add(record_id)
    return leaked


def _without_record_deltas(payload: dict[str, Any], record_ids: set[str]) -> dict[str, Any]:
    """丢弃指定记录的正文增量，只保留整份快照。

    增量里的流式边界摘要描述的是改写前的正文，继续写入会让磁盘与内存错位；
    这些记录由调用方登记 ``resync``，下一轮以整份快照收敛。

    Args:
        payload: 已脱敏的待写入载荷。
        record_ids: 需要丢弃增量的记录 ID 集合。

    Returns:
        丢弃增量后的载荷；无需丢弃时返回原对象。
    """
    deltas = payload.get("record_deltas")
    if not record_ids or not isinstance(deltas, list):
        return payload
    kept = [
        delta for delta in deltas
        if not (isinstance(delta, dict) and delta.get("id") in record_ids)
    ]
    if len(kept) == len(deltas):
        return payload
    result = dict(payload)
    if kept:
        result["record_deltas"] = kept
    else:
        result.pop("record_deltas", None)
    return result


def _digest(value: Any) -> bytes:
    """返回一个值的规范化比较摘要。

    只服务于「是否与上次落盘相同」的判断：排序键后的紧凑 JSON 让字典字段顺序
    不影响比较、列表顺序敏感，语义与逐字节比较一致；无法 JSON 编码时退化为
    ``repr`` 摘要，保证过滤路径不抛异常。

    Args:
        value: 待比较的 JSON 兼容值。

    Returns:
        16 字节 blake2b 摘要。
    """
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
    except (TypeError, ValueError):
        encoded = repr(value)
    return hashlib.blake2b(encoded.encode("utf-8"), digest_size=_DIGEST_SIZE).digest()


def _static_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """把整份记录快照或记录增量归一成同一份静态投影，用于去重比较。

    两种载荷的字段位置不同（快照在顶层、增量在 ``fields``），但表达的是同一份
    「字段 + 非正文视图数据」；归一后才能用同一个摘要判断「这条记录是否已经按
    当前形态落盘过」。

    Args:
        value: 整份记录快照或一条 ``record_deltas`` 条目。

    Returns:
        形状固定的静态投影字典（不含 ``content``/``thinking``）。
    """
    view = value.get("view")
    view = view if isinstance(view, dict) else {}
    fields = value.get("fields")
    if isinstance(fields, Mapping):
        static_fields = {key: fields.get(key) for key in _STATIC_FIELDS}
        data = view.get("set")
    else:
        static_fields = {key: value.get(key) for key in _STATIC_FIELDS}
        data = view.get("data")
    data = data if isinstance(data, Mapping) else {}
    return {
        "id": value.get("id"),
        "fields": static_fields,
        "view": {
            "kind": view.get("kind") if isinstance(view.get("kind"), str) else "",
            "set": {key: item for key, item in data.items() if key not in _STREAM_KEY_SET},
        },
    }


@dataclass
class ResumeResult:
    """resolve_resume() 解析成功时的返回值。

    Attributes:
        session_id: 目标会话 UUID。
        state: 加载的单一会话状态。
        metadata: 目标会话的元数据字典（含 mode、topic 等）。
    """
    session_id: str
    state: SessionState
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _WrittenState:
    """某个会话已经落盘的投影摘要，用于丢弃与上次相同的重复提交。

    只保留 16 字节内容摘要，不保留整份编码副本，运行期内存不再随会话正文增长。

    Attributes:
        records: 记录 ID → 已落盘记录投影的摘要。
        deltas: 记录 ID → 已落盘静态投影（字段 + 非正文视图数据）的摘要。
        calls: ``(call_id, attempt)`` → 已落盘调用记录的摘要。
        plan: 已落盘计划载荷的摘要。
        context_length: 已落盘的上下文长度（用于识别空增量）。
    """

    records: dict[str, bytes] = field(default_factory=dict)
    deltas: dict[str, bytes] = field(default_factory=dict)
    calls: dict[tuple[Any, Any], bytes] = field(default_factory=dict)
    plan: bytes | None = None
    context_length: int = -1
    context_ids: tuple[str, ...] = ()
    streams: dict[tuple[str, str], str] = field(default_factory=dict)


class SessionMgr:
    """会话管理器 — 持久化会话元数据和单一状态，支持 /resume 恢复。

    Attributes:
        _sessions_dir: 会话文件存储目录（{global_dir}/sessions/）。
        _workdir: 当前工作目录，记录到元数据中。
    """

    def __init__(self, global_dir: Path, workdir: Path, data_guard: Any = None) -> None:
        """初始化会话管理器。

        Args:
            global_dir: 全局配置目录（~/.agent/）。
            workdir: 当前工作目录。
        """
        self._sessions_dir: Path = global_dir / "sessions"
        self._workdir: Path = workdir
        self._data_guard = data_guard
        self._pools: dict[str, dict[str, str]] = {}
        self._seqs: dict[str, int] = {}
        self._written: dict[str, _WrittenState] = {}
        self._log_lock = threading.Lock()

    def start_cold_compression(self) -> threading.Thread:
        """在后台线程压缩长期未更新的会话日志。

        Returns:
            已启动的守护线程；压缩能力缺失或无候选文件时立即结束。
        """
        def work() -> None:
            try:
                with self._log_lock:
                    run_cold_compression(self._sessions_dir)
            except Exception as e:  # pragma: no cover - 后台任务不得影响主流程
                logger.warning("冷压缩会话日志失败: %s", e)

        thread = threading.Thread(
            target=work,
            name="session-cold-compress",
            daemon=True,
        )
        thread.start()
        return thread

    def save_metadata(
        self,
        session_id: str,
        *,
        is_new: bool = False,
        topic: str = "",
        mode: RunMode = RunMode.EXECUTE,
    ) -> None:
        """保存或更新会话元数据文件。

        首次调用（is_new=True）写入 created_at；后续调用仅更新 updated_at。

        Args:
            session_id: 会话 UUID。
            is_new: 是否为新会话首次写入。
            topic: 会话主题（截取前 100 字符）。非空时写入或覆盖 topic 字段。
            mode: 当前会话运行模式。
        """
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        meta_path = self._sessions_dir / f"{session_id}.json"
        now = datetime.now(timezone.utc).isoformat()

        if meta_path.exists() and not is_new:
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                meta = {}
        else:
            meta = {
                "session_id": session_id,
                "workdir": str(self._workdir),
                "created_at": now,
            }

        meta["updated_at"] = now
        if topic:
            meta["topic"] = topic[:100]
        meta["mode"] = mode.value

        try:
            safe_meta = self._data_guard.redact(meta) if self._data_guard is not None else meta
            atomic_write_text(meta_path, json.dumps(safe_meta, ensure_ascii=False, indent=2))
        except (OSError, UnicodeError) as e:
            logger.warning("写入会话元数据失败: %s", e)

    def append_state(self, session_id: str, state: SessionState) -> bool:
        """把会话状态自上次提交以来的增量追加到日志。

        无变更时不产生任何写入；空状态同样表示「无增量」，不会写入空文件。
        状态快照在调用线程准备，只有追加成功后才确认 dirty 基线；写盘失败会保留
        待提交内容供下一次调用重试。

        Args:
            session_id: 会话 UUID。
            state: 内存中的唯一会话状态。
        """
        commit = state.prepare_commit()
        if commit is None:
            return True
        success = self.append_batch(session_id, commit.payload, commit.resync)
        if success:
            state.ack_commit(commit)
        else:
            state.requeue_commit(commit)
        return success

    def append_batch(
        self,
        session_id: str,
        payload: dict[str, Any],
        resync: set[str] | None = None,
    ) -> bool:
        """把一批已取出的状态增量编码后追加到日志。

        编码在会话级内容池上完成，可安全地在工作线程调用。与上次落盘逐字节相同的
        区块会被丢弃；过滤后没有真实变更时整批不写，也不改动内容池与批序号，因此
        日志里不会出现同一份记录的重复提交。

        文本脱敏发生在文本进入系统时（用户输入、工具结果、模型流式增量），这里
        只做漏网检测：仍被改写说明入口漏了一条路径，记录告警后按脱敏结果写入。

        Args:
            session_id: 会话 UUID。
            payload: ``SessionState.prepare_commit()`` 返回的增量载荷。
            resync: 可选集合；写入端无法按增量对位的记录 ID 会登记在这里。生产
                调用方直接传 ``commit.resync``，由 ``SessionState.ack_commit()``
                据此让这些记录退回整份快照。

        Returns:
            True 表示无待写入内容或写入成功。
        """
        try:
            self._sessions_dir.mkdir(parents=True, exist_ok=True)
            safe_payload = payload
            if self._data_guard is not None:
                redacted = self._data_guard.redact(payload)
                if redacted != payload:
                    # 入口脱敏漏了一条路径：整份快照按脱敏结果写入（绝不泄漏），
                    # 对不上基线的增量整段丢弃，登记 resync 由状态层下一轮按
                    # 整段复核后的规范文本重写；禁止写出与内存不一致的正文。
                    logger.warning("会话载荷在落盘前仍需脱敏，入口脱敏可能漏了一条路径")
                    leaked = _leaked_record_ids(self._data_guard, payload)
                    if leaked:
                        if resync is not None:
                            resync |= leaked
                        redacted = _without_record_deltas(redacted, leaked)
                safe_payload = redacted
            log_path = self._log_path(session_id)
            with self._log_lock:
                self._restore_active_log(session_id)
                self._hydrate_projection_locked(session_id)
                pool = self._pools.setdefault(session_id, {})
                written = self._written.setdefault(session_id, _WrittenState())
                filtered, next_written = self._filter_payload(
                    safe_payload, written, resync=resync,
                )
                if filtered is None:
                    return True
                seq = self._seqs.get(session_id, self._last_seq(session_id))
                seq += 1
                candidate_pool = dict(pool)
                batch = build_batch(
                    seq=seq,
                    timestamp=time.time(),
                    payload=filtered,
                    pool=candidate_pool,
                )
                append_text(log_path, json.dumps(batch, ensure_ascii=False) + "\n")
                pool.clear()
                pool.update(candidate_pool)
                self._written[session_id] = next_written
                self._seqs[session_id] = seq
                return True
        except (OSError, UnicodeError) as e:
            with self._log_lock:
                self._pools.pop(session_id, None)
                self._written.pop(session_id, None)
                self._seqs.pop(session_id, None)
            logger.warning("写入会话状态失败: %s", e)
            return False

    def _hydrate_projection_locked(self, session_id: str) -> None:
        """从现有日志恢复追加端的内容池、序号和去重摘要。"""
        if session_id in self._pools and session_id in self._written and session_id in self._seqs:
            return
        text = self._read_log(session_id)
        if text is None:
            return
        payload, contents, last_seq = fold_log(iter_batch_lines(text))
        self._pools[session_id] = contents
        self._seqs[session_id] = last_seq
        self._written[session_id] = self._prime_written(payload)

    def _filter_payload(
        self,
        payload: dict[str, Any],
        written: _WrittenState,
        *,
        resync: set[str] | None = None,
    ) -> tuple[dict[str, Any] | None, _WrittenState]:
        """丢弃与上次落盘相同的区块，返回（待写入载荷, 已落盘摘要）。

        比较只在 16 字节摘要上进行，因此不需要为每个会话保留整份编码副本。
        记录增量同样按摘要去重：正文部分用 ``stream_boundaries`` 校验链式关系，
        静态部分用归一化投影的摘要判断是否真的变了；只丢弃「已经在盘上」的内容，
        永远不会丢弃无法对位的正文——对不上时保留正文并登记到 ``resync``，由
        ``SessionState.ack_commit()`` 整份重写该记录，避免用户已经看到的内容在
        日志里被静默截断。

        Args:
            payload: 待写入的状态增量。
            written: 该会话已落盘的投影摘要。
            resync: 可选集合；登记无法按增量对位、需要整份重写的记录 ID。

        Returns:
            过滤后的载荷与更新后的已落盘摘要；没有真实变更时载荷为 None，
            已落盘摘要原样返回。
        """
        next_written = _WrittenState(
            records=dict(written.records),
            deltas=dict(written.deltas),
            calls=dict(written.calls),
            plan=written.plan,
            context_length=written.context_length,
            context_ids=written.context_ids,
            streams=dict(written.streams),
        )
        result: dict[str, Any] = {}
        records = payload.get("records")
        if isinstance(records, list):
            kept_records: list[Any] = []
            for record in records:
                record_id = record.get("id") if isinstance(record, dict) else None
                if isinstance(record_id, str):
                    digest = _digest(record)
                    if next_written.records.get(record_id) == digest:
                        continue
                    next_written.records[record_id] = digest
                    next_written.deltas[record_id] = _digest(_static_projection(record))
                    view = record.get("view")
                    data = view.get("data") if isinstance(view, dict) else None
                    for key in _STREAM_KEYS:
                        text = data.get(key) if isinstance(data, Mapping) else None
                        next_written.streams[(record_id, key)] = text_digest(
                            text if isinstance(text, str) else "",
                        )
                kept_records.append(record)
            if kept_records:
                result["records"] = kept_records
        plan = payload.get("plan")
        if isinstance(plan, dict):
            digest = _digest(plan)
            if digest != next_written.plan:
                next_written.plan = digest
                result["plan"] = plan
        context = payload.get("context")
        if isinstance(context, dict):
            keep = context.get("keep")
            append = context.get("append")
            if isinstance(keep, int) and keep >= 0 and isinstance(append, list):
                target = next_written.context_ids[:keep] + tuple(
                    item for item in append if isinstance(item, str) and item
                )
                if target != next_written.context_ids:
                    next_written.context_ids = target
                    next_written.context_length = len(target)
                    result["context"] = {"keep": keep, "append": list(append)}
        calls = payload.get("llm_calls")
        if isinstance(calls, list):
            kept_calls: list[Any] = []
            for call in calls:
                key = (
                    (call.get("call_id"), call.get("attempt"))
                    if isinstance(call, dict) else None
                )
                if isinstance(key, tuple):
                    digest = _digest(call)
                    if next_written.calls.get(key) == digest:
                        continue
                    next_written.calls[key] = digest
                kept_calls.append(call)
            if kept_calls:
                result["llm_calls"] = kept_calls
        deltas = payload.get("record_deltas")
        if isinstance(deltas, list):
            kept_deltas: list[Any] = []
            for delta in deltas:
                if not isinstance(delta, dict) or not isinstance(delta.get("id"), str):
                    continue
                record_id = delta["id"]
                static_digest = _digest(_static_projection(delta))
                raw_view = delta.get("view")
                view = raw_view if isinstance(raw_view, dict) else {}
                boundaries = delta.get("stream_boundaries")
                kept_append: dict[str, str] = {}
                kept_boundaries: dict[str, Any] = {}
                append_view = view.get("append")
                if isinstance(append_view, dict) and isinstance(boundaries, dict):
                    for key, value in append_view.items():
                        if not isinstance(key, str) or not isinstance(value, str):
                            continue
                        boundary = boundaries.get(key)
                        if not isinstance(boundary, dict):
                            continue
                        base = boundary.get("base")
                        after = boundary.get("after")
                        if not isinstance(after, str):
                            continue
                        stream_key = (record_id, key)
                        current = next_written.streams.get(stream_key, _EMPTY_DIGEST)
                        if current == after:
                            continue
                        if current != base:
                            logger.warning(
                                "会话增量基线不匹配，保留该段正文并安排整份快照: %s/%s",
                                record_id, key,
                            )
                            if resync is not None:
                                resync.add(record_id)
                        kept_append[key] = value
                        kept_boundaries[key] = boundary
                        next_written.streams[stream_key] = after
                if not kept_append and next_written.deltas.get(record_id) == static_digest:
                    continue
                next_written.deltas[record_id] = static_digest
                filtered = dict(delta)
                filtered_view = dict(view)
                filtered_view["append"] = kept_append
                filtered["view"] = filtered_view
                filtered["stream_boundaries"] = kept_boundaries
                kept_deltas.append(filtered)
            if kept_deltas:
                result["record_deltas"] = kept_deltas
        if not result:
            return None, written
        return result, next_written

    def _prime_written(self, payload: dict[str, Any]) -> _WrittenState:
        """用已加载的会话状态预热已落盘投影摘要。

        恢复会话后如果不预热，下一次提交会把整段上下文当成新内容重写一遍。

        Args:
            payload: ``fold_log`` 折叠出的完整状态载荷。

        Returns:
            与该日志当前内容一致的已落盘摘要。
        """
        written = _WrittenState()
        for record in payload.get("records") or []:
            if not isinstance(record, dict):
                continue
            record_id = record.get("id")
            if not isinstance(record_id, str):
                continue
            written.records[record_id] = _digest(record)
            written.deltas[record_id] = _digest(_static_projection(record))
            view = record.get("view")
            data = view.get("data") if isinstance(view, dict) else None
            if isinstance(data, Mapping):
                for key in _STREAM_KEYS:
                    value = data.get(key)
                    if isinstance(value, str):
                        written.streams[(record_id, key)] = text_digest(value)
        plan = payload.get("plan")
        if isinstance(plan, dict):
            written.plan = _digest(plan)
        for call in payload.get("llm_calls") or []:
            if isinstance(call, dict):
                written.calls[(call.get("call_id"), call.get("attempt"))] = _digest(call)
        written.context_length = len(payload.get("context_ids") or [])
        written.context_ids = tuple(
            item for item in payload.get("context_ids") or [] if isinstance(item, str)
        )
        return written

    def load_state(self, session_id: str) -> SessionState | None:
        """加载并校验单一会话状态；日志缺失、损坏或无法恢复时返回 None。"""
        try:
            with self._log_lock:
                text = self._read_log(session_id)
                if text is None:
                    return None
                payload, contents, last_seq = fold_log(iter_batch_lines(text))
                if last_seq == 0:
                    logger.warning(
                        "会话日志没有当前格式（v%s）的批记录，按不可恢复处理: %s",
                        LOG_VERSION, session_id,
                    )
                    return None
                safe_payload = self._data_guard.redact(payload) if self._data_guard is not None else payload
                state = SessionState.from_dict(safe_payload)
                if state is None:
                    logger.warning("会话日志损坏，无法重建状态: %s", session_id)
                    return None
                self._pools[session_id] = contents
                self._seqs[session_id] = max(self._seqs.get(session_id, 0), last_seq)
                self._written[session_id] = self._prime_written(payload)
        except (OSError, UnicodeError) as e:
            logger.warning("加载会话状态失败: %s", e)
            return None
        return state

    def _restore_active_log(self, session_id: str) -> None:
        """把冷归档还原成可追加的活跃日志，保证续写不丢历史。

        归档文件与活跃文件同名不同后缀，直接续写会让读取端只看到新日志；这里先
        把归档解压回活跃文件，再删除归档，续写后整个会话仍是一条连续日志。

        Args:
            session_id: 会话 UUID。

        Returns:
            None。
        """
        log_path = self._log_path(session_id)
        if log_path.exists():
            return
        for suffix in COLD_SUFFIXES:
            cold_path = self._sessions_dir / f"{session_id}{suffix}"
            if not cold_path.exists():
                continue
            atomic_write_text(log_path, decompress_log(cold_path))
            cold_path.unlink(missing_ok=True)
            return

    def _log_path(self, session_id: str) -> Path:
        """返回会话日志的活跃路径。"""
        return self._sessions_dir / f"{session_id}.jsonl"

    def _read_log(self, session_id: str) -> str | None:
        """读取会话日志原文，必要时透明解压冷日志。"""
        log_path = self._log_path(session_id)
        if log_path.exists():
            return log_path.read_text(encoding="utf-8")
        for suffix in COLD_SUFFIXES:
            cold_path = self._sessions_dir / f"{session_id}{suffix}"
            if cold_path.exists():
                return decompress_log(cold_path)
        return None

    def _last_seq(self, session_id: str) -> int:
        """从日志恢复最后一条批记录的序号。"""
        log_path = self._log_path(session_id)
        if not log_path.exists():
            return 0
        seq = 0
        try:
            with log_path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict) and isinstance(value.get("seq"), int):
                        seq = max(seq, value["seq"])
        except OSError as e:
            logger.warning("读取会话日志序号失败: %s", e)
        return seq

    def _has_state(self, session_id: str) -> bool:
        """快速扫描日志，判断会话是否包含当前格式的批记录。"""
        log_path = self._log_path(session_id)
        if log_path.exists():
            try:
                with log_path.open("r", encoding="utf-8") as stream:
                    for line in stream:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            value = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(value, dict) and value.get("v") == LOG_VERSION:
                            return True
            except (json.JSONDecodeError, OSError, UnicodeError):
                return False
            return False
        return any(
            (self._sessions_dir / f"{session_id}{suffix}").exists()
            for suffix in COLD_SUFFIXES
        )

    def list_sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        """列出最近的会话，按 updated_at 降序排列。

        Args:
            limit: 最多返回的会话数量。

        Returns:
            会话元数据字典列表，包含 session_id、workdir、updated_at、topic 等。
        """
        if not self._sessions_dir.exists():
            return []
        sessions: list[dict[str, Any]] = []
        for f in self._sessions_dir.glob("*.json"):
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
                if "session_id" in meta:
                    safe_meta = self._data_guard.redact(meta) if self._data_guard is not None else meta
                    sessions.append(safe_meta)
            except (json.JSONDecodeError, OSError):
                continue
        sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
        return sessions[:limit]

    def list_resumable(self, current_session_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """列出可恢复的历史会话（按 updated_at 降序，排除当前会话）。

        Args:
            current_session_id: 当前会话 ID，从结果中排除。
            limit: 最多返回的会话数量。

        Returns:
            排除当前会话后的会话元数据字典列表。
        """
        return [
            session
            for session in self.list_sessions(limit=10_000)
            if session.get("session_id") != current_session_id
            and self._has_state(str(session.get("session_id", "")))
        ][:limit]

    def resolve_resume(
        self,
        cmd_args: list[str],
        current_session_id: str,
        current_workdir: str,
    ) -> str | ResumeResult:
        """处理 /resume 命令的会话解析与加载逻辑。

        解析目标、加载状态并校验工作目录。

        Args:
            cmd_args: 命令参数列表，序号或 session_id 前缀（无参列表交互由 app 层命令处理）。
            current_session_id: 当前会话 ID，用于从列表中排除。
            current_workdir: 当前工作目录路径字符串，用于校验一致性。

        Returns:
            str: 要显示给用户的错误信息。
            ResumeResult: 成功解析的会话数据，调用方负责应用到 Agent 状态。
        """
        sessions = self.list_resumable(current_session_id, limit=10)

        if not sessions:
            return "没有可恢复的历史会话。\n"

        # 解析目标会话：先尝试序号，再尝试 session_id 前缀匹配
        target_arg = cmd_args[0]
        target_session: dict | None = None

        try:
            idx = int(target_arg)
            if 1 <= idx <= len(sessions):
                target_session = sessions[idx - 1]
        except ValueError:
            pass

        if target_session is None:
            for s in sessions:
                sid = s.get("session_id", "")
                if sid == target_arg or sid.startswith(target_arg):
                    target_session = s
                    break

        if target_session is None:
            return f"未找到匹配的会话: {target_arg}\n"

        target_id = target_session["session_id"]

        state = self.load_state(target_id)
        if state is None:
            return f"会话 {target_id[:8]}... 没有有效的会话状态。\n"

        # 工作目录不一致时拒绝恢复
        saved_workdir = target_session.get("workdir", "")
        if saved_workdir and current_workdir and saved_workdir != current_workdir:
            return (
                f"无法恢复：原会话工作目录为 {saved_workdir}，当前为 {current_workdir}。\n"
                f"请在原工作目录下启动后再恢复该会话。\n"
            )

        return ResumeResult(
            session_id=target_id,
            state=state,
            metadata=target_session,
        )

    def get_metadata(self, session_id: str) -> dict[str, Any] | None:
        """获取指定会话的元数据。

        Args:
            session_id: 目标会话 UUID。

        Returns:
            元数据字典，不存在时返回 None。
        """
        meta_path = self._sessions_dir / f"{session_id}.json"
        if not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return self._data_guard.redact(meta) if self._data_guard is not None else meta
        except (json.JSONDecodeError, OSError):
            return None
