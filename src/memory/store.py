"""统一记忆存储，替代两个 VectorMemory 实例。

单一 ChromaDB collection，通过 metadata 区分记忆类型。
版本控制、序列化逻辑内联。
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import chromadb
from chromadb.config import Settings

from src.core.performance import async_time_function, time_function

from .decay import calculate_importance
from .embeddings import EmbeddingClient
from .extractor import FactExtractor
from .types import MemoryRecord, MemoryType

logger = logging.getLogger(__name__)


class MemoryStore:
    """统一记忆存储。"""

    def __init__(
        self,
        collection_name: str = "memories",
        persist_dir: str = "./chroma_data",
        distance_threshold: float = 1.1,
    ):
        embedding_model = os.getenv("OPENAI_MODEL_EMBEDDING")
        embedding_url = os.getenv("OPENAI_MODEL_EMBEDDING_URL")
        if not embedding_model or not embedding_url:
            raise ValueError("OPENAI_MODEL_EMBEDDING and OPENAI_MODEL_EMBEDDING_URL must be set")

        self._embedding = EmbeddingClient(embedding_model, embedding_url)
        self._extractor = FactExtractor()
        self._threshold = distance_threshold

        self._client = chromadb.PersistentClient(
            path=persist_dir,
            settings=Settings(anonymized_telemetry=False),
        )
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            embedding_function=self._embedding,
        )

    # ------------------------------------------------------------------ #
    #  写入
    # ------------------------------------------------------------------ #

    @time_function()
    def add(self, record: MemoryRecord) -> str:
        """添加记忆，自动处理 base_id 计算和版本控制。"""
        record.base_id = record.compute_base_id()

        existing = self._get_active_versions(record.base_id)
        if existing:
            best = max(existing, key=lambda r: r.version)
            if not self._should_replace(record, best):
                logger.debug(f"Existing v{best.version} is better, skipping")
                return best.id
            self._deactivate_batch([r.id for r in existing])
            record.version = best.version + 1
        else:
            record.version = 1

        record.is_active = True
        memory_id = str(uuid.uuid4())
        record.id = memory_id

        self._collection.add(
            documents=[record.content],
            metadatas=[record.to_chroma_metadata()],
            ids=[memory_id],
        )
        logger.info(
            f"Inserted {memory_id} (base_id={record.base_id[:12]}..., "
            f"type={record.memory_type}, v{record.version})"
        )
        return memory_id

    @async_time_function()
    async def add_from_conversation(
        self,
        user_input: str,
        assistant_response: str = "",
        source_id: str | None = None,
    ) -> list[str]:
        """从对话中提取事实并存储。"""
        facts = await self._extractor.extract(user_input, assistant_response, source_id)
        ids = []
        for fact in facts:
            record = MemoryRecord(
                memory_type=MemoryType.FACT,
                content=fact.fact_text,
                speaker=fact.speaker,
                type_tag=fact.type,
                attribute=fact.attribute,
                confidence=fact.confidence,
                source=fact.source,
                original_utterance=fact.original_utterance,
                extra=fact.metadata,
            )
            ids.append(self.add(record))
        return ids

    def add_summary(
        self,
        summary_text: str,
        conversation_id: str,
        key_points: list[str] | None = None,
    ) -> str:
        """添加对话摘要。"""
        record = MemoryRecord(
            memory_type=MemoryType.SUMMARY,
            content=summary_text,
            conversation_id=conversation_id,
            key_points=key_points or [],
            confidence=1.0,
        )
        return self.add(record)

    # ------------------------------------------------------------------ #
    #  检索
    # ------------------------------------------------------------------ #

    @time_function()
    def search(
        self,
        query: str,
        n: int = 5,
        memory_type: MemoryType | None = None,
        type_tag: str | None = None,
    ) -> list[MemoryRecord]:
        """语义检索，返回 MemoryRecord 列表。"""
        conditions: list[dict[str, Any]] = [{"is_active": True}]
        if memory_type is not None:
            conditions.append({"memory_type": memory_type.value})
        if type_tag is not None:
            conditions.append({"type_tag": type_tag})

        where = conditions[0] if len(conditions) == 1 else {"$and": conditions}

        results = self._collection.query(
            query_texts=[query],
            n_results=n,
            where=where,
            include=["documents", "metadatas", "distances"],
        )

        records = []
        if not results.get("ids") or not results["ids"][0]:
            return records

        ids = results["ids"][0]
        documents = (results.get("documents") or [[]])[0]
        metadatas = (results.get("metadatas") or [[]])[0]
        distances = (results.get("distances") or [[]])[0]

        hit_ids = []
        for i, doc_id in enumerate(ids):
            dist = distances[i] if i < len(distances) else float("inf")
            if dist > self._threshold:
                continue
            content = documents[i] if i < len(documents) else ""
            metadata = metadatas[i] if i < len(metadatas) else {}
            record = MemoryRecord.from_chroma(doc_id, content, metadata)
            records.append(record)
            hit_ids.append(doc_id)

        # 批量更新访问统计
        if hit_ids:
            self._update_access_stats(hit_ids)

        return records

    def get_by_type(self, memory_type: MemoryType) -> list[MemoryRecord]:
        """按类型获取所有活跃记忆。"""
        results = self._collection.get(
            where={"$and": [{"memory_type": memory_type.value}, {"is_active": True}]},
        )
        return self._results_to_records(results)

    def get_by_id(self, memory_id: str) -> MemoryRecord | None:
        """按 ID 获取单条记忆。"""
        result = self._collection.get(ids=[memory_id])
        if not result["ids"]:
            return None
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []
        return MemoryRecord.from_chroma(
            result["ids"][0],
            documents[0] if documents else "",
            metadatas[0] if metadatas else {},
        )

    def get_history(self, base_id: str) -> list[MemoryRecord]:
        """获取版本历史（含非活跃版本）。"""
        results = self._collection.get(where={"base_id": base_id})
        records = self._results_to_records(results)
        records.sort(key=lambda r: r.version)
        return records

    # ------------------------------------------------------------------ #
    #  修改 / 删除
    # ------------------------------------------------------------------ #

    def delete(self, memory_id: str) -> None:
        self._collection.delete(ids=[memory_id])

    def deactivate(self, memory_id: str) -> None:
        self._collection.update(ids=[memory_id], metadatas=[{"is_active": False}])

    def clear_all(self) -> None:
        self._collection.delete(where={})

    # ------------------------------------------------------------------ #
    #  衰减
    # ------------------------------------------------------------------ #

    def cleanup(self, min_importance: float = 0.1) -> int:
        """清理 importance 低于阈值的活跃记忆，返回清理数量。"""
        self.recalculate_importance()
        results = self._collection.get(where={"is_active": True})
        records = self._results_to_records(results)

        to_deactivate = [r.id for r in records if r.importance < min_importance]
        if to_deactivate:
            self._deactivate_batch(to_deactivate)
            logger.info(f"Cleaned up {len(to_deactivate)} low-importance memories")
        return len(to_deactivate)

    def recalculate_importance(self) -> None:
        """批量重算所有活跃记忆的 importance。"""
        results = self._collection.get(where={"is_active": True})
        records = self._results_to_records(results)
        if not records:
            return

        now = datetime.now(timezone.utc)
        ids = []
        new_metadatas = []
        for r in records:
            new_imp = calculate_importance(r, now)
            if abs(new_imp - r.importance) > 0.001:
                ids.append(r.id)
                new_metadatas.append({"importance": new_imp})

        if ids:
            self._collection.update(ids=ids, metadatas=new_metadatas)
            logger.debug(f"Recalculated importance for {len(ids)} memories")

    # ------------------------------------------------------------------ #
    #  内部方法
    # ------------------------------------------------------------------ #

    def _get_active_versions(self, base_id: str) -> list[MemoryRecord]:
        results = self._collection.get(
            where={"$and": [{"base_id": base_id}, {"is_active": True}]},
        )
        return self._results_to_records(results)

    def _should_replace(self, new: MemoryRecord, best: MemoryRecord) -> bool:
        """判断新记忆是否应替换现有最佳版本。用 datetime 比较而非字符串。"""
        if new.confidence > best.confidence:
            return True
        if new.confidence == best.confidence and new.created_at > best.created_at:
            return True
        return False

    def _deactivate_batch(self, ids: list[str]) -> None:
        """批量去活。"""
        if not ids:
            return
        self._collection.update(
            ids=ids,
            metadatas=[{"is_active": False}] * len(ids),
        )

    def _update_access_stats(self, ids: list[str]) -> None:
        """批量更新 last_accessed 和 access_count。"""
        now_str = datetime.now(timezone.utc).isoformat()
        # 先获取当前 access_count
        results = self._collection.get(ids=ids)
        if not results["ids"]:
            return
        metadatas = results.get("metadatas") or []
        new_metadatas = []
        for i, _ in enumerate(results["ids"]):
            meta = metadatas[i] if i < len(metadatas) else {}
            current_count = meta.get("access_count", 0)
            new_metadatas.append({
                "last_accessed": now_str,
                "access_count": current_count + 1,
            })
        self._collection.update(ids=results["ids"], metadatas=new_metadatas)

    def _results_to_records(self, results: dict) -> list[MemoryRecord]:
        """将 ChromaDB get() 结果转换为 MemoryRecord 列表。"""
        records = []
        if not results or not results.get("ids"):
            return records
        ids = results["ids"]
        documents = results.get("documents") or []
        metadatas = results.get("metadatas") or []
        for i, doc_id in enumerate(ids):
            content = documents[i] if i < len(documents) else ""
            metadata = metadatas[i] if i < len(metadatas) else {}
            records.append(MemoryRecord.from_chroma(doc_id, content, metadata))
        return records
