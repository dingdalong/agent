"""会话日志的内容池与批记录编解码。

会话状态按追加式日志保存：每次提交只追加一条批记录，长文本在写入前统一抽取到
内容池，正文位置只保留 ``{"$ref": <内容指纹>}`` 引用。同一段文本无论在同一会话
中出现多少次，磁盘上都只有一份。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Iterator, Mapping


LOG_VERSION = 1
POOL_MIN_CHARS = 256
REF_KEY = "$ref"
_DIGEST_SIZE = 16


def content_id(text: str) -> str:
    """返回一段文本的内容指纹，作为内容池引用键。

    Args:
        text: 参与去重的完整文本。

    Returns:
        带 ``c`` 前缀的 32 位十六进制指纹。
    """
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=_DIGEST_SIZE)
    return "c" + digest.hexdigest()


def text_digest(text: str) -> str:
    """返回流式正文边界的 16 字节摘要，用于写侧链式校验。

    Args:
        text: 参与摘要的正文（通常是某次提交前的正文或整段正文）。

    Returns:
        32 位十六进制摘要，不带内容池指纹的 ``c`` 前缀。
    """
    return hashlib.blake2b(text.encode("utf-8"), digest_size=_DIGEST_SIZE).hexdigest()


def reference_id(value: object) -> str | None:
    """提取内容引用字典中的指纹，非引用时返回 None。

    Args:
        value: 待判断的已解码值。

    Returns:
        引用字典指向的指纹，或 None。
    """
    if not isinstance(value, dict) or len(value) != 1:
        return None
    ref = value.get(REF_KEY)
    return ref if isinstance(ref, str) and ref else None


def encode_value(value: Any, contents: dict[str, str]) -> Any:
    """把值中的长文本替换为引用，并把新内容登记到内容池。

    Args:
        value: 待编码的 JSON 兼容值。
        contents: 会话内容池，键为指纹、值为原文；就地追加本次新增内容。

    Returns:
        与输入同构的编码结果。
    """
    if isinstance(value, str):
        if len(value) < POOL_MIN_CHARS:
            return value
        ident = content_id(value)
        contents.setdefault(ident, value)
        return {REF_KEY: ident}
    if isinstance(value, list):
        return [encode_value(item, contents) for item in value]
    if isinstance(value, dict):
        return {key: encode_value(item, contents) for key, item in value.items()}
    return value


def decode_value(value: Any, contents: Mapping[str, str]) -> Any:
    """还原值中被引用替换的长文本。

    Args:
        value: 已编码的 JSON 兼容值。
        contents: 会话累计内容池。

    Returns:
        与输入同构的解码结果；缺失引用原样保留，便于诊断损坏日志。
    """
    if isinstance(value, dict):
        ident = reference_id(value)
        if ident is not None:
            text = contents.get(ident)
            return text if isinstance(text, str) else value
        return {key: decode_value(item, contents) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_value(item, contents) for item in value]
    return value


def build_batch(
    *,
    seq: int,
    timestamp: float,
    payload: dict[str, Any],
    pool: dict[str, str],
) -> dict[str, Any]:
    """构建一条可追加的批记录。

    Args:
        seq: 会话内单调递增的批序号。
        timestamp: 提交时刻的 Unix 时间戳。
        payload: 本次提交的状态增量（records/context/plan/llm_calls 的子集）；
            ``context`` 是 ``{"keep": n, "append": [记录 ID]}`` 形式的上下文增量。
        pool: 会话级已知内容池；本批新增内容会就地登记。

    Returns:
        已完成内容引用编码的批记录字典。
    """
    before = set(pool)
    encoded = encode_value(payload, pool)
    fresh = {key: text for key, text in pool.items() if key not in before}
    batch: dict[str, Any] = {
        "v": LOG_VERSION,
        "seq": seq,
        "ts": timestamp,
    }
    if fresh:
        batch["contents"] = fresh
    batch.update(encoded if isinstance(encoded, dict) else {})
    return batch


def fold_log(
    batches: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, str], int]:
    """折叠批记录，同时返回内容池与最大批序号。

    内容池与序号供写入端在重启后复用：已登记的长文本继续以引用落盘，新批记录
    从已有序号之后续写，同一份日志不会被拆成两段编号。上下文只按
    ``{"keep": n, "append": [...]}`` 增量折叠。格式不匹配的批记录被忽略，避免用
    残缺状态（例如读不出上下文的记录）恢复会话。

    Args:
        batches: 已按 ``seq`` 升序排列的批记录。

    Returns:
        ``(完整载荷, 内容池, 最大批序号)``；载荷可直接交给
        ``SessionState.from_dict`` 校验。没有任何当前版本批记录时最大批序号为 0。
    """
    contents: dict[str, str] = {}
    records: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    context_ids: list[str] = []
    plan: dict[str, Any] = {}
    llm_calls: dict[tuple[Any, Any], dict[str, Any]] = {}
    call_order: list[tuple[Any, Any]] = []
    last_seq = 0

    for raw_batch in batches:
        if not isinstance(raw_batch, Mapping):
            continue
        if raw_batch.get("v") != LOG_VERSION:
            continue
        seq = raw_batch.get("seq")
        if isinstance(seq, int) and seq > last_seq:
            last_seq = seq
        raw_contents = raw_batch.get("contents")
        if isinstance(raw_contents, Mapping):
            for key, text in raw_contents.items():
                if isinstance(key, str) and isinstance(text, str):
                    contents[key] = text
        batch = decode_value(raw_batch, contents)
        if not isinstance(batch, dict):
            continue
        raw_records = batch.get("records")
        if isinstance(raw_records, list):
            for record in raw_records:
                if not isinstance(record, dict):
                    continue
                record_id = record.get("id")
                if not isinstance(record_id, str) or not record_id:
                    continue
                if record_id not in records:
                    order.append(record_id)
                records[record_id] = record
        raw_deltas = batch.get("record_deltas")
        if isinstance(raw_deltas, list):
            for delta in raw_deltas:
                _apply_record_delta(records, delta)
        context_delta = batch.get("context")
        if isinstance(context_delta, dict):
            keep = context_delta.get("keep")
            append = context_delta.get("append")
            if isinstance(keep, int) and keep >= 0 and isinstance(append, list):
                context_ids = context_ids[:keep] + [
                    item for item in append if isinstance(item, str) and item
                ]
        if isinstance(batch.get("plan"), dict):
            plan = batch["plan"]
        raw_calls = batch.get("llm_calls")
        if isinstance(raw_calls, list):
            for call in raw_calls:
                if not isinstance(call, dict):
                    continue
                key = (call.get("call_id"), call.get("attempt"))
                if key not in llm_calls:
                    call_order.append(key)
                llm_calls[key] = call

    return {
        "version": LOG_VERSION,
        "records": [records[record_id] for record_id in order],
        "context_ids": context_ids,
        "plan": plan,
        "llm_calls": [llm_calls[key] for key in call_order],
    }, contents, last_seq


def _apply_record_delta(records: dict[str, dict[str, Any]], delta: object) -> None:
    """把一条记录增量合并进折叠出的完整记录状态。

    增量按键覆盖静态字段与视图数据（不含 ``content``/``thinking``），并按顺序追加
    流式正文；基记录不在本批之前的折叠结果里时忽略该增量，避免把正文挂到别的记录
    上。视图为空且增量没带视图内容时保持记录原样，不会凭空造出无效的视图。

    Args:
        records: 已按 ``seq`` 顺序折叠出的记录字典，键为记录 ID。
        delta: 一条 ``record_deltas`` 条目。

    Returns:
        None；就地更新 ``records``。
    """
    if not isinstance(delta, Mapping):
        return
    record_id = delta.get("id")
    record = records.get(record_id) if isinstance(record_id, str) else None
    if record is None:
        return
    fields = delta.get("fields")
    if isinstance(fields, Mapping):
        for key, value in fields.items():
            if isinstance(key, str):
                record[key] = value
    raw_view = delta.get("view")
    if not isinstance(raw_view, Mapping):
        return
    kind = raw_view.get("kind")
    raw_set = raw_view.get("set")
    raw_append = raw_view.get("append")
    if not (
        (isinstance(kind, str) and kind)
        or (isinstance(raw_set, Mapping) and raw_set)
        or (isinstance(raw_append, Mapping) and raw_append)
    ):
        return
    view = record.get("view")
    if not isinstance(view, dict):
        if not isinstance(kind, str) or not kind:
            return
        view = {"kind": kind, "data": {}}
        record["view"] = view
    if isinstance(kind, str) and kind:
        view["kind"] = kind
    data = view.setdefault("data", {})
    if not isinstance(data, dict):
        data = {}
        view["data"] = data
    if isinstance(raw_set, Mapping):
        data.update(raw_set)
    if isinstance(raw_append, Mapping):
        for key, value in raw_append.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            current = data.get(key, "")
            data[key] = (current if isinstance(current, str) else "") + value


def iter_batch_lines(text: str) -> Iterator[dict[str, Any]]:
    """逐行解析日志文本，跳过空行与损坏行。

    Args:
        text: 完整或前缀日志文本。

    Yields:
        解析成功的批记录字典。
    """
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            yield value
