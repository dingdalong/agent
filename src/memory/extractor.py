"""事实提取器：从对话中提取结构化记忆事实。

重构自 memory_extractor.py，修复 include_types bug。
"""

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

from pydantic import BaseModel

from src.llm.base import LLMProvider
from src.llm.structured import build_output_schema, parse_output
from src.utils.performance import async_time_function

logger = logging.getLogger(__name__)


# ---------- 配置 ----------


class ExtractorConfig:
    """提取器配置常量。"""

    ALLOWED_TYPES = {
        "user": {"personal_info", "preference", "experience", "relationship", "goal", "behavior_pattern"},
        "assistant": {"capability", "version", "limitation", "self_description"},
        "world": {"fact", "definition", "event", "person", "place", "concept"},
        "conversation": {"task_state", "pending_action", "summary", "context"},
        "interaction": {"feedback", "rating", "correction"},
    }
    FLAT_ALLOWED_TYPES = {f"{cat}.{sub}" for cat, subs in ALLOWED_TYPES.items() for sub in subs}

    SPEAKER_USER = "user"
    SPEAKER_ASSISTANT = "assistant"
    SPEAKER_SYSTEM = "system"

    CONFIDENCE_THRESHOLD = 0.6

    FUZZY_WORDS = {"可能", "大概", "也许", "似乎", "我觉得", "我猜", "maybe", "perhaps", "probably"}
    STRONG_WORDS = {"绝对", "肯定", "一定", "definitely", "absolutely", "certainly"}

    SENSITIVE_PATTERNS = [re.compile(r"1[3-9]\d{9}")]


# ---------- 结构化输出模型 ----------


class FactItem(BaseModel):
    fact_text: str
    confidence: float
    type: str
    is_plausible: Optional[bool] = None
    speaker: str
    attribute: str


class FactsResult(BaseModel):
    facts: List[FactItem]


_SUBMIT_FACTS_TOOL = build_output_schema(
    "submit_facts",
    "提交从对话中提取的事实列表",
    FactsResult,
)


# ---------- 数据模型 ----------


@dataclass
class Fact:
    """表示一条从对话中提取的事实。"""

    fact_text: str
    confidence: float
    type: str
    speaker: str
    source: str
    original_utterance: str
    attribute: str
    fact_id: str = field(init=False)
    is_plausible: Optional[bool] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    version: int = 1
    is_active: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.fact_id = self._generate_id()

    def _generate_id(self) -> str:
        base = f"{self.fact_text.strip().lower()}|{self.speaker}|{self.attribute}|{self.timestamp}"
        return hashlib.sha256(base.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------- 辅助工具 ----------


class TextUtils:
    @staticmethod
    def adjust_confidence(text: str, base_conf: float) -> float:
        lower = text.lower()
        if any(word in lower for word in ExtractorConfig.FUZZY_WORDS):
            base_conf = max(0.1, base_conf - 0.1)
        if any(word in lower for word in ExtractorConfig.STRONG_WORDS):
            base_conf = min(1.0, base_conf + 0.05)
        return round(base_conf, 2)

    @staticmethod
    def contains_sensitive(text: str) -> bool:
        return any(p.search(text) for p in ExtractorConfig.SENSITIVE_PATTERNS)


class TypeValidator:
    @classmethod
    def normalize(cls, raw_type: str) -> Optional[str]:
        if raw_type in ExtractorConfig.FLAT_ALLOWED_TYPES:
            return raw_type
        for category, subs in ExtractorConfig.ALLOWED_TYPES.items():
            if raw_type in subs:
                return f"{category}.{raw_type}"
            if raw_type.startswith(f"{category}.") and raw_type.split(".", 1)[1] in subs:
                return raw_type
        return None

    @classmethod
    def is_allowed(cls, type_str: str, allowed_set: Optional[Set[str]] = None) -> bool:
        if allowed_set is None:
            allowed_set = ExtractorConfig.FLAT_ALLOWED_TYPES
        return type_str in allowed_set


# ---------- 主要提取器类 ----------


class FactExtractor:
    """从对话中提取结构化记忆事实。"""

    def __init__(
        self,
        config: Optional[ExtractorConfig] = None,
        llm: Optional[LLMProvider] = None,
    ):
        self.config = config or ExtractorConfig()
        self._llm = llm
        self._type_validator = TypeValidator()
        self._text_utils = TextUtils()
        # 缓存默认 prompt（include_types=None 时使用）
        self._default_target_types = self._determine_target_types(None)
        self._default_prompt = self._build_prompt(self._default_target_types)

    @async_time_function()
    async def extract(
        self,
        user_input: str,
        assistant_response: str = "",
        source_id: Optional[str] = None,
        include_types: Optional[Set[str]] = None,
        enable_sensitive_filter: bool = True,
    ) -> List[Fact]:
        """执行提取，返回 Fact 列表。"""
        # 修复：根据 include_types 动态选择 prompt
        if include_types is None:
            target_types = self._default_target_types
            prompt = self._default_prompt
        else:
            target_types = self._determine_target_types(include_types)
            prompt = self._build_prompt(target_types)

        facts_data = await self._call_model(user_input, assistant_response, prompt)
        if not facts_data:
            return []

        facts = []
        for raw_fact in facts_data:
            fact = self._build_fact(
                raw_fact, user_input, assistant_response,
                source_id, enable_sensitive_filter, target_types,
            )
            if fact:
                facts.append(fact)
        return facts

    def _determine_target_types(self, include_types: Optional[Set[str]]) -> Set[str]:
        if include_types is None:
            return self.config.FLAT_ALLOWED_TYPES
        return include_types & self.config.FLAT_ALLOWED_TYPES

    def _build_prompt(self, target_types: Set[str]) -> str:
        type_desc = "\n".join(f"  - {t}" for t in sorted(target_types))
        return f"""你是一个面向长期记忆的信息提取器，任务是从以下对话中提取值得长期保存的知识。请根据内容将知识分类，并严格按照 JSON 格式输出。请仅输出 JSON，不要包含任何其他说明或 Markdown 代码块标记。

可提取的知识类型包括：
{type_desc}

要求：
1. 每条知识以简洁的陈述句表达，主语明确。
   - 对于用户信息，主语应为"用户"。
   - 对于助手信息，主语应为"助手"。
   - 对于世界知识，主语应为客观实体（如"巴黎是法国的首都"）。
2. 忽略一次性任务请求、寒暄、无实质内容。
3. 为每条知识提供以下字段：
   - fact_text: 陈述句
   - confidence: 0.0~1.0 的浮点数
   - type: 知识类型（必须从上述列表中选择）
   - is_plausible: true/false/null（常识合理性）
   - speaker: "user" 或 "assistant"（指信息由谁说出）
   - attribute: 字符串，表示该事实所属的具体属性。例如用户名字的 attribute 可以是 "user.name"；用户对茶的偏好可以是 "user.preference.drink.tea"。同一属性的不同值应使用相同的 attribute，以便后续更新。
4. 请调用 submit_facts 工具提交提取结果。
示例：
{{
  "facts": [
    {{
      "fact_text": "用户喜欢喝拿铁咖啡",
      "confidence": 0.95,
      "type": "user.preference",
      "is_plausible": true,
      "speaker": "user",
      "attribute": "user.preference.drink.latte"
    }}
  ]
}}
"""

    @async_time_function()
    async def _call_model(
        self,
        user_input: str,
        assistant_response: str,
        system_prompt: str,
    ) -> Optional[List[Dict[str, Any]]]:
        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"用户说：{user_input}\n助手说：{assistant_response}"},
            ]
            if self._llm is None:
                raise RuntimeError("FactExtractor requires an LLMProvider instance (llm=...)")
            response = await self._llm.chat(
                messages, temperature=0.0, tools=[_SUBMIT_FACTS_TOOL], silent=True,
            )
            tool_calls = response.tool_calls
            result = parse_output(tool_calls, "submit_facts", FactsResult)
            if result:
                return [item.model_dump() for item in result.facts]
            return None
        except Exception as e:
            logger.error(f"Model call failed: {e}")
            return None

    def _build_fact(
        self,
        raw: Dict[str, Any],
        user_input: str,
        assistant_response: str,
        source_id: Optional[str],
        sensitive_filter: bool,
        target_types: Set[str],
    ) -> Optional[Fact]:
        fact_text = str(raw.get("fact_text", "")).strip()
        if not fact_text:
            return None

        if sensitive_filter and self._text_utils.contains_sensitive(fact_text):
            logger.info(f"Sensitive fact discarded: {fact_text}")
            return None

        try:
            confidence = float(raw.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))
        except (ValueError, TypeError):
            confidence = 0.5
        confidence = self._text_utils.adjust_confidence(fact_text, confidence)

        if confidence < self.config.CONFIDENCE_THRESHOLD:
            return None

        raw_type = str(raw.get("type", "")).strip()
        validated_type = self._type_validator.normalize(raw_type)
        if not validated_type or not self._type_validator.is_allowed(validated_type, target_types):
            logger.debug(f"Invalid or excluded type '{raw_type}' for fact: {fact_text}")
            return None

        is_plausible = raw.get("is_plausible")
        if isinstance(is_plausible, str):
            is_plausible = is_plausible.lower() == "true"
        elif not isinstance(is_plausible, bool):
            is_plausible = None
        if is_plausible is False:
            return None

        speaker = str(raw.get("speaker", "user")).strip().lower()
        if speaker not in (self.config.SPEAKER_USER, self.config.SPEAKER_ASSISTANT):
            speaker = self.config.SPEAKER_USER

        attribute = str(raw.get("attribute", "")).strip()
        if not attribute:
            logger.warning(f"Missing 'attribute' for fact: {fact_text}, discarding")
            return None

        original = user_input if speaker == self.config.SPEAKER_USER else assistant_response

        metadata = {
            "negation": self._detect_negation(fact_text),
            "temporal": "present",
        }

        return Fact(
            fact_text=fact_text,
            confidence=confidence,
            type=validated_type,
            speaker=speaker,
            source=source_id or "unknown",
            original_utterance=original,
            attribute=attribute,
            is_plausible=is_plausible,
            metadata=metadata,
        )

    @staticmethod
    def _detect_negation(text: str) -> bool:
        negations = {"不", "没", "不是", "不喜欢", "don't", "doesn't"}
        return any(neg in text.lower() for neg in negations)
