from typing import List, Dict, Any, Optional, Callable
import tiktoken
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

from src.core.api import call_model_with_retry  # 假设你的 call_model_with_retry 支持非流式调用
from src.core.performance import time_function

@time_function()
def summarize_conversation(messages: list) -> str:
    """
    调用模型生成对话摘要。
    messages: 需要摘要的对话消息列表（通常是不包含 system 的多轮对话）。
    返回摘要文本。
    """
    # 构造摘要提示
    prompt = (
        "请将以下对话内容总结为一段简洁的摘要，保留关键信息（如用户偏好、重要事实、已完成的步骤）。"
        "只输出摘要本身，不要多余的解释。\n\n"
        "对话：\n"
    )
    for msg in messages:
        role = msg["role"]
        content = msg.get("content", "")
        if role == "user":
            prompt += f"用户：{content}\n"
        elif role == "assistant":
            prompt += f"助手：{content}\n"
        # tool 消息也可考虑，但摘要中可能不需要细节，简化处理

    # 调用模型（使用非流式）
    response, _, _ = call_model_with_retry(
        messages=[{"role": "user", "content": prompt}],
        stream=False,
        tools=None  # 摘要不需要工具
    )

    return response

class ConversationBuffer:
    def __init__(
        self,
        max_rounds: int = 10,
        max_tokens: int = 4096,
        system_prompt: Optional[str] = None,
        tokenizer: Optional[Callable[[str], int]] = None,
        conversation_id: Optional[str] = None,
    ):
        """
        :param max_rounds: 最多保留的对话轮数
        :param max_tokens: 最大 token 数（用于截断）
        :param system_prompt: 系统提示词（始终保留）
        :param tokenizer: 自定义 token 计数函数，输入字符串返回 token 数。若为 None，则使用 tiktoken 的 cl100k_base 近似。
        """
        self.max_rounds = max_rounds
        self.max_tokens = max_tokens
        self.system_prompt = system_prompt
        self.conversation_id = conversation_id or str(uuid.uuid4())
        self.messages: List[Dict[str, Any]] = []

        # 设置 token 计数器
        if tokenizer is None:
            try:
                enc = tiktoken.get_encoding("cl100k_base")
                self._token_counter = lambda text: len(enc.encode(text))  # 改名
            except Exception as e:
                logger.warning(f"Failed to load tiktoken, using approximate count: {e}")
                self._token_counter = lambda text: len(text) // 3 + 1
        else:
            self._token_counter = tokenizer

    def _count_messages_tokens(self, messages: List[Dict]) -> int:
        total = 0
        for msg in messages:
            if "content" in msg and msg["content"]:
                total += self._token_counter(msg["content"])  # 调用改名后的计数器
        return total

    def should_compress(self) -> bool:
        return self._count_messages_tokens(self.messages) > self.max_tokens

    @time_function()
    def compress(self, vector_memory: Optional["VectorMemory"] = None):
        """压缩最早的对话，用摘要替换"""
        if len(self.messages) < 4:  # 至少需要几轮对话才有压缩意义
            return

        # 决定要压缩多少对话：例如压缩最早的一半轮数，但至少保留最近几轮
        # 简单做法：压缩最早的一半消息，但要保证压缩后摘要不会太长
        total_msgs = len(self.messages)
        compress_count = total_msgs // 2  # 压缩一半
        if compress_count < 2:  # 最少压缩 2 条消息（至少一轮 user+assistant）
            compress_count = 2

        # 提取要压缩的部分（最旧的消息）
        old_msgs = self.messages[:compress_count]
        remaining_msgs = self.messages[compress_count:]

        # 生成摘要
        summary = summarize_conversation(old_msgs)

        if vector_memory:
            # 将摘要存入长期记忆，创建 SummaryMemory 对象
            from .memory_types import SummaryMemory
            summary_memory = SummaryMemory(
                summary_text=summary,
                conversation_id=self.conversation_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
                metadata={
                    "type": "conversation_summary",
                    "session_id": self.conversation_id
                }
            )
            vector_memory.add_memory(summary_memory)
        # 构造摘要消息：可以作为 system 消息，也可以作为 assistant 消息
        # 建议作为 system 消息，因为摘要内容是客观总结，不属于用户或助手直接发言
        summary_msg = {"role": "system", "content": f"对话历史摘要：{summary}"}

        # 更新消息列表：摘要 + 剩余消息
        self.messages = [summary_msg] + remaining_msgs

        # 可选：记录日志
        print(f"[记忆系统] 对话已压缩，摘要：{summary}")

    def add_user_message(self, content: str):
        self.messages.append({"role": "user", "content": content})

    def add_assistant_message(self, message: Dict[str, Any]):
        self.messages.append(message)

    def add_tool_message(self, tool_call_id: str, content: str):
        self.messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})

    def _split_prefix_and_rounds(self):
        prefix_messages = []
        rounds = []
        current_round = []

        for msg in self.messages:
            role = msg.get("role")
            if role == "system" and not prefix_messages and not rounds and not current_round:
                prefix_messages.append(msg)
                continue

            if role == "user":
                if current_round:
                    rounds.append(current_round)
                current_round = [msg]
                continue

            if not current_round:
                current_round = [msg]
            else:
                current_round.append(msg)

        if current_round:
            rounds.append(current_round)

        return prefix_messages, rounds

    def _flatten_rounds(self, prefix_messages: List[Dict[str, Any]], rounds: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        flattened = list(prefix_messages)
        for round_messages in rounds:
            flattened.extend(round_messages)
        return flattened

    def get_messages_for_api(self) -> List[Dict[str, Any]]:
        """
        返回适合 API 的消息列表：
        - 始终包含 system_prompt（如果有）
        - 保留最近 max_rounds 轮对话，且保证工具调用链完整
        - 如果 token 超出 max_tokens，则按完整轮次从最旧消息开始丢弃
        """
        prefix_messages, rounds = self._split_prefix_and_rounds()
        selected_rounds = rounds[-self.max_rounds:] if self.max_rounds > 0 else []

        # 计算系统提示词的 token 数
        system_tokens = self._token_counter(self.system_prompt) if self.system_prompt else 0

        selected = self._flatten_rounds(prefix_messages, selected_rounds)
        while selected and self._count_messages_tokens(selected) + system_tokens > self.max_tokens:
            if selected_rounds:
                removed_round = selected_rounds.pop(0)
                logger.debug(
                    "Removed round due to token limit: %s",
                    [msg.get("role") for msg in removed_round],
                )
            elif prefix_messages:
                removed_prefix = prefix_messages.pop(0)
                logger.debug(
                    "Removed prefix message due to token limit: %s - %s",
                    removed_prefix.get("role"),
                    removed_prefix.get("content", "")[:50],
                )
            else:
                break
            selected = self._flatten_rounds(prefix_messages, selected_rounds)

        result = []
        if self.system_prompt:
            result.append({"role": "system", "content": self.system_prompt})
        result.extend(selected)
        return result

    def clear(self):
        self.messages.clear()

import os
import uuid
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Union, Set

import chromadb
from chromadb.config import Settings

from .embeddings import OllamaEmbeddingFunction  # 假设已存在
from .memory_extractor import Fact, FactExtractor  # 导入我们新定义的数据类
from .serializer import flatten_metadata, MemorySerializer  # 迁移到 serializer 模块
from .memory_types import Memory, MemoryType, MemoryRegistry, FactMemory, SummaryMemory  # 新记忆类型系统
from .versioning import VersioningStrategyFactory  # 版本控制工厂

logger = logging.getLogger(__name__)


# ---------- 辅助函数 ----------


# ---------- 向量记忆存储 ----------
class VectorMemory:
    """
    使用 ChromaDB 存储记忆向量，支持版本控制、活跃标记和检索。
    版本控制基于 speaker, type, attribute 的组合（base_id）。
    """

    def __init__(self,
                 collection_name: str = "user_memories",
                 persist_directory: str = "./chroma_data"):
        embedding_model = os.getenv("OPENAI_MODEL_EMBEDDING")
        embedding_url = os.getenv("OPENAI_MODEL_EMBEDDING_URL")
        if not embedding_model or not embedding_url:
            raise ValueError("OPENAI_MODEL_EMBEDDING and OPENAI_MODEL_EMBEDDING_URL must be set")

        self.embedding_fn = OllamaEmbeddingFunction(
            embedding_model,
            embedding_url
        )
        self.client = chromadb.PersistentClient(
            path=persist_directory,
            settings=Settings(anonymized_telemetry=False)
        )
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            embedding_function=self.embedding_fn
        )

    def add_conversation(self,
                user_input: str,
                assistant_response: str = "",
                source_id: Optional[str] = None,
                include_types: Optional[Set[str]] = None,
                enable_sensitive_filter: bool = True):

        extractor = FactExtractor()
        facts1 = extractor.extract(user_input, assistant_response, source_id, include_types, enable_sensitive_filter)
        for f in facts1:
            self._add_fact(f)  # attribute 应为 "user.name"，版本 1

    @time_function()
    def _add_memory_object(self, memory: Memory) -> str:
        """
        添加任意类型的记忆对象，自动处理序列化、版本控制和存储。
        返回新插入的记忆 ID。
        """
        # 序列化记忆
        serializer = MemorySerializer()
        content_text, flattened_metadata = serializer.serialize(memory)

        # 从元数据中提取 base_id
        base_id = flattened_metadata.get("base_id")
        if not base_id:
            raise ValueError("Serialized metadata missing 'base_id' field")

        # 获取现有活跃版本
        existing = self._get_active_versions(base_id)

        # 冲突解决：决定是否插入新版本
        if existing:
            best_existing = self._select_best_existing(existing)
            if not self._should_replace(flattened_metadata, best_existing):
                logger.debug(f"Existing memory (v{best_existing['metadata']['version']}) is better, skipping insertion for memory type '{memory.memory_type}'")
                return best_existing["id"]
            else:
                # 将现有活跃版本全部标记为非活跃
                self._deactivate_versions([mem["id"] for mem in existing])
                # 版本号递增
                flattened_metadata["version"] = best_existing["metadata"]["version"] + 1
        else:
            flattened_metadata["version"] = 1

        # 确保元数据包含必要字段
        flattened_metadata.setdefault("is_active", True)

        # 插入新记忆
        memory_id = str(uuid.uuid4())
        self.collection.add(
            documents=[content_text],
            metadatas=[flattened_metadata],
            ids=[memory_id]
        )
        logger.info(f"Inserted new memory {memory_id} (base_id={base_id}, type={memory.memory_type}, v{flattened_metadata['version']})")
        return memory_id

    def add_memory(self, *args, **kwargs) -> str:
        """
        添加记忆。支持两种调用方式：
        1. add_memory(memory: Memory) -> str
        2. add_memory(content: str, metadata: Dict[str, Any]) -> str (旧版兼容)
        """
        # 检查是否传递了 Memory 对象
        if len(args) == 1 and isinstance(args[0], Memory):
            memory = args[0]
            # 使用通用序列化路径
            return self._add_memory_object(memory)

        # 检查是否以位置参数形式传递了内容和元数据
        if len(args) == 2 and isinstance(args[0], str) and isinstance(args[1], dict):
            content, metadata = args
        # 检查是否以关键字参数形式传递了内容和元数据
        elif len(args) == 1 and isinstance(args[0], str) and 'metadata' in kwargs and isinstance(kwargs['metadata'], dict):
            content = args[0]
            metadata = kwargs['metadata']
        else:
            raise TypeError("Invalid arguments. Use add_memory(memory) or add_memory(content, metadata)")

        # 向后兼容：将旧版调用转换为 SummaryMemory
        from .memory_types import SummaryMemory
        summary_memory = SummaryMemory(
            summary_text=content,
            conversation_id=metadata.get("conversation_id") or metadata.get("session_id", "unknown"),
            timestamp=metadata.get("timestamp", datetime.now(timezone.utc).isoformat()),
            metadata=metadata
        )
        if "key_points" in metadata:
            key_points = metadata.get("key_points")
            if isinstance(key_points, str):
                try:
                    summary_memory.key_points = json.loads(key_points)
                except json.JSONDecodeError:
                    summary_memory.key_points = []
            elif isinstance(key_points, list):
                summary_memory.key_points = key_points
        if metadata.get("length") is not None:
            summary_memory.length = metadata["length"]
        return self._add_memory_object(summary_memory)

    # ---------- 核心操作 ----------
    def _add_fact(self, fact: Fact) -> str:
        """
        添加一条事实记忆，自动处理版本控制和元数据展平。
        返回新插入的记忆 ID。
        """
        # 将 Fact 转换为 FactMemory 并使用通用 add_memory 方法
        fact_memory = FactMemory.from_fact(fact)
        return self._add_memory_object(fact_memory)

    @time_function()
    def search(self,
            query: str,
            n_results: int = 5,
            type_filter: Optional[str] = None,
            memory_type: Optional[Union[MemoryType, str]] = None,
            include_inactive: bool = False,
            detailed: bool = False,
            return_memory_objects: bool = False) -> Union[List[Dict[str, Any]], List[Memory]]:
        """
        检索记忆，返回记忆列表。

        Args:
            query: 查询字符串
            n_results: 返回的最大结果数
            type_filter: 按记忆类型过滤（如 "user.preference"）
            memory_type: 按记忆类型枚举过滤（如 MemoryType.FACT）
            include_inactive: 是否包含已标记为非活跃的记忆
            detailed: 若为 True，返回所有元数据；否则只返回核心字段（仅当 return_memory_objects=False 时有效）
            return_memory_objects: 若为 True，返回 Memory 对象列表；否则返回字典列表

        Returns:
            如果 return_memory_objects=False，返回字典列表：
            - 当 detailed=False 时包含 id, fact, type, confidence, speaker,
            attribute, timestamp, version
            - 当 detailed=True 时包含 id, fact, metadata（全部元数据）
            如果 return_memory_objects=True，返回 Memory 对象列表。
        """
        # Build conditions for where clause
        conditions = []
        if not include_inactive:
            conditions.append({"is_active": True})
        if type_filter:
            conditions.append({"type": type_filter})
        if memory_type is not None:
            if isinstance(memory_type, MemoryType):
                conditions.append({"memory_type": memory_type.value})
            else:
                conditions.append({"memory_type": memory_type})

        # Build where clause with $and operator if we have conditions
        if conditions:
            if len(conditions) == 1:
                where = conditions[0]
            else:
                where = {"$and": conditions}
        else:
            where = {}

        results = self.collection.query(
            query_texts=[query],
            n_results=n_results,
            where=where,
            include=["documents", "metadatas", "distances"]
        )

        # distances 是欧氏距离，越小越相似
        threshold = 1.1  # 设定距离阈值（需根据实验调整）

        memories = []
        if results.get("ids") and results["ids"] and len(results["ids"]) > 0 and results["ids"][0]:
            ids = results["ids"][0]
            # Safely get documents, metadatas, and distances with fallbacks
            documents_list = results.get("documents")
            documents = documents_list[0] if documents_list is not None and len(documents_list) > 0 else []
            metadatas_list = results.get("metadatas")
            metadatas = metadatas_list[0] if metadatas_list is not None and len(metadatas_list) > 0 else []
            distances_list = results.get("distances")
            distances = distances_list[0] if distances_list is not None and len(distances_list) > 0 else []

            for i, doc_id in enumerate(ids):
                if i >= len(documents):
                    continue
                content = documents[i] if i < len(documents) else ""
                metadata = metadatas[i] if i < len(metadatas) else {}
                dist = distances[i] if i < len(distances) else float('inf')

                # Apply memory_type filter on client side as well (for safety)
                if memory_type is not None:
                    metadata_memory_type = metadata.get("memory_type")
                    if isinstance(memory_type, MemoryType):
                        expected_value = memory_type.value
                    else:
                        expected_value = memory_type
                    if metadata_memory_type != expected_value:
                        continue

                if dist > threshold:
                    continue

                if return_memory_objects:
                    # 反序列化为 Memory 对象
                    memory = self._deserialize_memory(doc_id, content, metadata)
                    memories.append(memory)
                else:
                    if detailed:
                        memories.append({
                            "id": doc_id,
                            "fact": content,
                            "metadata": metadata
                        })
                    else:
                        # 提取核心字段，过滤掉 None 值
                        core = {
                            "id": doc_id,
                            "fact": content,
                            "type": metadata.get("type"),
                            "confidence": metadata.get("confidence"),
                            "speaker": metadata.get("speaker"),
                            "attribute": metadata.get("attribute"),
                            "timestamp": metadata.get("timestamp"),
                            "version": metadata.get("version"),
                        }
                        # 移除值为 None 的字段（可选，保持干净）
                        core = {k: v for k, v in core.items() if v is not None}
                        memories.append(core)

        return memories

    def search_by_type(self, query: str, memory_type: Union[MemoryType, str], n_results: int = 5, type_filter: Optional[str] = None, include_inactive: bool = False, detailed: bool = False, return_memory_objects: bool = False, **kwargs) -> Union[List[Dict[str, Any]], List[Memory]]:
        """
        按记忆类型搜索记忆，其他参数与 search 方法相同。
        """
        return self.search(query=query, n_results=n_results, type_filter=type_filter, memory_type=memory_type, include_inactive=include_inactive, detailed=detailed, return_memory_objects=return_memory_objects, **kwargs)

    def get_by_type(self, memory_type: Union[MemoryType, str], include_inactive: bool = False, return_memory_objects: bool = False) -> Union[List[Dict[str, Any]], List[Memory]]:
        """
        获取指定类型的所有记忆（无查询条件）。
        """
        where: Dict[str, Any] = {"memory_type": memory_type.value if isinstance(memory_type, MemoryType) else memory_type}
        if not include_inactive:
            where["is_active"] = True  # type: ignore[assignment]

        results = self.collection.get(where=where)
        memories = []

        if not results or not results.get("ids"):
            return memories

        ids = results["ids"]
        documents_list = results.get("documents")
        documents = documents_list if documents_list is not None else []
        metadatas_list = results.get("metadatas")
        metadatas = metadatas_list if metadatas_list is not None else []

        for i, doc_id in enumerate(ids):
            content = documents[i] if i < len(documents) else ""
            metadata = metadatas[i] if i < len(metadatas) else {}
            if return_memory_objects:
                memory = self._deserialize_memory(doc_id, content, metadata)
                memories.append(memory)
            else:
                memories.append({
                    "id": doc_id,
                    "fact": content,
                    "metadata": metadata
                })
        return memories

    def get_by_id(self, memory_id: str) -> Optional[Dict]:
        result = self.collection.get(ids=[memory_id])
        if result["ids"]:
            documents_list = result.get("documents")
            documents = documents_list if documents_list is not None else []
            metadatas_list = result.get("metadatas")
            metadatas = metadatas_list if metadatas_list is not None else []
            return {
                "id": result["ids"][0],
                "fact": documents[0] if documents else "",
                "metadata": metadatas[0] if metadatas else {}
            }
        return None

    def get_history(self, base_fact_id: str) -> List[Dict]:
        """获取同一基础事实的所有版本历史（base_fact_id 由 speaker, type, attribute 生成）"""
        results = self.collection.get(where={"$or": [{"base_fact_id": base_fact_id}, {"base_id": base_fact_id}]})
        history = []

        if not results or not results.get("ids"):
            return history

        ids = results["ids"]
        documents_list = results.get("documents")
        documents = documents_list if documents_list is not None else []
        metadatas_list = results.get("metadatas")
        metadatas = metadatas_list if metadatas_list is not None else []

        for i, doc_id in enumerate(ids):
            content = documents[i] if i < len(documents) else ""
            metadata = metadatas[i] if i < len(metadatas) else {}
            history.append({
                "id": doc_id,
                "fact": content,
                "metadata": metadata
            })

        # Sort by version, safely handle missing version
        history.sort(key=lambda x: x.get("metadata", {}).get("version", 0))
        return history

    def delete(self, memory_id: str):
        self.collection.delete(ids=[memory_id])

    def deactivate(self, memory_id: str):
        """软删除：将记忆标记为非活跃"""
        self.collection.update(
            ids=[memory_id],
            metadatas=[{"is_active": False}]
        )

    def clear_all(self):
        self.collection.delete(where={})

    # ---------- 内部辅助方法 ----------
    @time_function()
    def _get_active_versions(self, base_id: str) -> List[Dict]:
        """获取指定基础ID的所有活跃记忆"""
        # 支持旧字段 base_fact_id 和新字段 base_id
        results = self.collection.get(
            where={"$and": [{"$or": [{"base_fact_id": base_id}, {"base_id": base_id}]}, {"is_active": True}]}
        )
        memories = []

        if not results or not results.get("ids"):
            return memories

        ids = results["ids"]
        metadatas_list = results.get("metadatas")
        metadatas = metadatas_list if metadatas_list is not None else []

        for i, mem_id in enumerate(ids):
            metadata = metadatas[i] if i < len(metadatas) else {}
            memories.append({
                "id": mem_id,
                "metadata": metadata
            })
        return memories

    def _select_best_existing(self, memories: List[Dict]) -> Dict:
        """从一组活跃记忆中选出最佳版本（版本号最高）"""
        best = max(memories, key=lambda m: m["metadata"]["version"])
        return best

    def _should_replace(self, new_meta: Dict, best_existing: Dict) -> bool:
        """
        判断新事实是否应替换现有最佳版本。
        基于置信度和时间戳。
        """
        old_meta = best_existing["metadata"]
        new_conf = new_meta.get("confidence", 0.5)
        old_conf = old_meta.get("confidence", 0.5)
        new_time = new_meta.get("timestamp", "")
        old_time = old_meta.get("timestamp", "")

        if new_conf > old_conf:
            return True
        if new_conf == old_conf and new_time > old_time:
            return True
        return False

    def _deactivate_versions(self, memory_ids: List[str]):
        """将多个记忆标记为非活跃"""
        for mem_id in memory_ids:
            self.collection.update(
                ids=[mem_id],
                metadatas=[{"is_active": False}]
            )

    def _deserialize_memory(self, memory_id: str, content: str, metadata: Any) -> Memory:
        """Deserialize a memory from storage."""
        # Convert metadata to dict if it's not already
        if not isinstance(metadata, dict):
            try:
                metadata = dict(metadata)
            except Exception as e:
                logger.warning(f"Failed to convert metadata to dict: {e}, using empty dict")
                metadata = {}

        # Prepare dictionary for MemoryRegistry
        data = {"id": memory_id}
        memory_type = metadata.get("memory_type")
        if not memory_type:
            # Try to infer from presence of fields
            if "fact_text" in metadata:
                memory_type = MemoryType.FACT.value
            elif "summary_text" in metadata:
                memory_type = MemoryType.SUMMARY.value
            else:
                # Default to FACT for backward compatibility
                memory_type = MemoryType.FACT.value
                data["fact_text"] = content
        else:
            # Ensure content is added to data with appropriate field name
            if memory_type == MemoryType.FACT.value:
                data["fact_text"] = content
            elif memory_type == MemoryType.SUMMARY.value:
                data["summary_text"] = content
            else:
                # Unknown type, store as generic content
                data["content"] = content
        data.update(metadata)
        # Override content fields with the actual content (in case metadata has stale content)
        if memory_type == MemoryType.FACT.value:
            data["fact_text"] = content
        elif memory_type == MemoryType.SUMMARY.value:
            data["summary_text"] = content
        else:
            data["content"] = content
        return MemoryRegistry.from_dict(data)
