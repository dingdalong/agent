# memory_extractor.py
import re
import json
import hashlib
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Set, Union

from src.core.async_api import call_model
from src.core.performance import async_time_function

logger = logging.getLogger(__name__)


# ---------- 配置 ----------
class ExtractorConfig:
    """提取器配置常量"""
    ALLOWED_TYPES = {
        "user": {"personal_info", "preference", "experience", "relationship", "goal", "behavior_pattern"},
        "assistant": {"capability", "version", "limitation", "self_description"},
        "world": {"fact", "definition", "event", "person", "place", "concept"},
        "conversation": {"task_state", "pending_action", "summary", "context"},
        "interaction": {"feedback", "rating", "correction"}
    }
    FLAT_ALLOWED_TYPES = {f"{cat}.{sub}" for cat, subs in ALLOWED_TYPES.items() for sub in subs}

    SPEAKER_USER = "user"
    SPEAKER_ASSISTANT = "assistant"
    SPEAKER_SYSTEM = "system"

    CONFIDENCE_THRESHOLD = 0.6

    # 模糊词和肯定词（用于置信度微调）
    FUZZY_WORDS = {"可能", "大概", "也许", "似乎", "我觉得", "我猜", "maybe", "perhaps", "probably"}
    STRONG_WORDS = {"绝对", "肯定", "一定", "definitely", "absolutely", "certainly"}

    # 敏感信息检测正则（示例）
    SENSITIVE_PATTERNS = [re.compile(r'1[3-9]\d{9}')]  # 手机号


# ---------- 数据模型 ----------
@dataclass
class Fact:
    """表示一条记忆事实"""
    fact_text: str
    confidence: float
    type: str
    speaker: str
    source: str
    original_utterance: str
    attribute: str  # 新增：事实所属的具体属性标识，用于版本合并
    fact_id: str = field(init=False)
    is_plausible: Optional[bool] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    version: int = 1
    is_active: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not hasattr(self, 'fact_id') or not self.fact_id:
            self.fact_id = self._generate_id()

    def _generate_id(self) -> str:
        # 使用 attribute 参与 ID 生成，确保同一属性的事实可以相互覆盖
        base = f"{self.fact_text.strip().lower()}|{self.speaker}|{self.attribute}|{self.timestamp}"
        return hashlib.sha256(base.encode('utf-8')).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典，便于序列化"""
        return asdict(self)


# ---------- 辅助工具 ----------
class TextUtils:
    @staticmethod
    def extract_json(text: str) -> str:
        """从可能包含 Markdown 代码块的文本中提取 JSON 字符串"""
        pattern = r'```(?:json)?\s*([\s\S]*?)\s*```'
        match = re.search(pattern, text)
        return match.group(1).strip() if match else text.strip()

    @staticmethod
    def adjust_confidence(text: str, base_conf: float) -> float:
        """根据文本中的模糊/肯定词汇微调置信度"""
        lower = text.lower()
        if any(word in lower for word in ExtractorConfig.FUZZY_WORDS):
            base_conf = max(0.1, base_conf - 0.1)
        if any(word in lower for word in ExtractorConfig.STRONG_WORDS):
            base_conf = min(1.0, base_conf + 0.05)
        return round(base_conf, 2)

    @staticmethod
    def contains_sensitive(text: str) -> bool:
        """检查是否包含敏感信息"""
        for pattern in ExtractorConfig.SENSITIVE_PATTERNS:
            if pattern.search(text):
                return True
        return False


class TypeValidator:
    """类型验证与规范化"""
    @classmethod
    def normalize(cls, raw_type: str) -> Optional[str]:
        """返回规范的类型字符串，若无效则返回 None"""
        if raw_type in ExtractorConfig.FLAT_ALLOWED_TYPES:
            return raw_type
        # 尝试补全分类前缀
        for category, subs in ExtractorConfig.ALLOWED_TYPES.items():
            if raw_type in subs:
                return f"{category}.{raw_type}"
            if raw_type.startswith(f"{category}.") and raw_type.split('.', 1)[1] in subs:
                return raw_type
        return None

    @classmethod
    def is_allowed(cls, type_str: str, allowed_set: Optional[Set[str]] = None) -> bool:
        if allowed_set is None:
            allowed_set = ExtractorConfig.FLAT_ALLOWED_TYPES
        return type_str in allowed_set


# ---------- 备用解析器 ----------
class FallbackParser:
    """当模型输出非 JSON 时的补救措施"""
    @staticmethod
    def parse(text: str) -> List[Dict[str, Any]]:
        text = text.strip()
        if not text:
            return []

        # 尝试直接解析整个文本
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "facts" in data:
                return data["facts"]
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

        # 尝试提取数组
        array_pattern = r'\[\s*\{.*\}\s*\]'
        match = re.search(array_pattern, text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except:
                pass

        logger.warning(f"Fallback parsing failed for text (first 200 chars): {text[:200]}")
        return []


# ---------- 主要提取器类 ----------
class FactExtractor:
    """
    从对话中提取结构化记忆事实
    """

    def __init__(self, config: ExtractorConfig = None):
        self.config = config or ExtractorConfig()
        self.type_validator = TypeValidator()
        self.text_utils = TextUtils()
        self.fallback_parser = FallbackParser()
        self.target_types = self._determine_target_types(None)
        self.prompt = self._build_prompt(self.target_types)

    @async_time_function()
    async def extract(self,
                user_input: str,
                assistant_response: str = "",
                source_id: Optional[str] = None,
                include_types: Optional[Set[str]] = None,
                enable_sensitive_filter: bool = True) -> List[Fact]:
        """
        执行提取流程，返回 Fact 对象列表
        """

        # 调用模型
        model_output = await self._call_model(user_input, assistant_response)
        if not model_output:
            return []

        # 解析响应
        facts_data = self._parse_model_output(model_output)
        if not facts_data:
            return []

        # 构建并验证 Fact 对象
        facts = []
        for raw_fact in facts_data:
            fact = self._build_fact(raw_fact,
                                     user_input,
                                     assistant_response,
                                     source_id,
                                     enable_sensitive_filter,
                                     self.target_types)
            if fact:
                facts.append(fact)
        return facts

    def _determine_target_types(self, include_types: Optional[Set[str]]) -> Set[str]:
        if include_types is None:
            return self.config.FLAT_ALLOWED_TYPES
        return include_types & self.config.FLAT_ALLOWED_TYPES

    def _build_prompt(self, target_types: Set[str]) -> str:
        type_desc = "\n".join(f"  - {t}" for t in sorted(target_types))
        prompt = f"""你是一个面向长期记忆的信息提取器，任务是从以下对话中提取值得长期保存的知识。请根据内容将知识分类，并严格按照 JSON 格式输出。请仅输出 JSON，不要包含任何其他说明或 Markdown 代码块标记。

可提取的知识类型包括：
{type_desc}

要求：
1. 每条知识以简洁的陈述句表达，主语明确。
   - 对于用户信息，主语应为“用户”。
   - 对于助手信息，主语应为“助手”。
   - 对于世界知识，主语应为客观实体（如“巴黎是法国的首都”）。
2. 忽略一次性任务请求、寒暄、无实质内容。
3. 为每条知识提供以下字段：
   - fact_text: 陈述句
   - confidence: 0.0~1.0 的浮点数
   - type: 知识类型（必须从上述列表中选择）
   - is_plausible: true/false/null（常识合理性）
   - speaker: "user" 或 "assistant"（指信息由谁说出）
   - attribute: 字符串，表示该事实所属的具体属性。例如用户名字的 attribute 可以是 "user.name"；用户对茶的偏好可以是 "user.preference.drink.tea"。同一属性的不同值（如“小明”和“大明”）应使用相同的 attribute，以便后续更新。若事实无明确属性，可使用类型加关键词生成，如 "world.fact.capital"。
4. 返回严格的 JSON 格式，外层是一个对象，包含 "facts" 数组。
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
    }},
    {{
      "fact_text": "巴黎是法国的首都",
      "confidence": 0.99,
      "type": "world.fact",
      "is_plausible": true,
      "speaker": "assistant",
      "attribute": "world.fact.capital.france.paris"
    }}
  ]
}}

"""
        return prompt

    @async_time_function()
    async def _call_model(self,
                    user_input: str,
                    assistant_response: str) -> Optional[str]:
        try:
            prompt = [
                {"role": "system", "content": self.prompt},  # 固定部分
                {"role": "user", "content": f"用户说：{user_input}\n助手说：{assistant_response}"}  # 变化部分
            ]
            response, tool_calls, finish_reason = await call_model(
                prompt, temperature=0.0,
                response_format={"type": "json_object"}
            )
            if not response:
                return None
            return response
        except Exception as e:
            logger.error(f"Model call failed: {e}")
            return None

    def _parse_model_output(self, text: str) -> List[Dict[str, Any]]:
        try:
            data = json.loads(text)
            facts = data.get("facts", [])
            if isinstance(facts, list):
                return facts
            else:
                logger.warning("Parsed JSON does not contain a 'facts' list")
                return []
        except json.JSONDecodeError:
            logger.info("JSON decode failed, attempting fallback parse")
            return self.fallback_parser.parse(text)

    def _build_fact(self,
                    raw: Dict[str, Any],
                    user_input: str,
                    assistant_response: str,
                    source_id: Optional[str],
                    sensitive_filter: bool,
                    target_types: Set[str]) -> Optional[Fact]:
        # 基础字段提取
        fact_text = str(raw.get("fact_text", "")).strip()
        if not fact_text:
            return None

        # 敏感信息过滤
        if sensitive_filter and self.text_utils.contains_sensitive(fact_text):
            logger.info(f"Sensitive fact discarded: {fact_text}")
            return None

        # 置信度处理
        try:
            confidence = float(raw.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))
        except (ValueError, TypeError):
            confidence = 0.5
        confidence = self.text_utils.adjust_confidence(fact_text, confidence)

        if confidence < self.config.CONFIDENCE_THRESHOLD:
            return None

        # 类型验证
        raw_type = str(raw.get("type", "")).strip()
        validated_type = self.type_validator.normalize(raw_type)
        if not validated_type or not self.type_validator.is_allowed(validated_type, target_types):
            logger.debug(f"Invalid or excluded type '{raw_type}' for fact: {fact_text}")
            return None

        # 合理性
        is_plausible = raw.get("is_plausible")
        if isinstance(is_plausible, str):
            is_plausible = is_plausible.lower() == "true"
        elif not isinstance(is_plausible, bool):
            is_plausible = None
        if is_plausible is False:
            return None  # 常识不合理则丢弃

        # 发言者
        speaker = str(raw.get("speaker", "user")).strip().lower()
        if speaker not in (self.config.SPEAKER_USER, self.config.SPEAKER_ASSISTANT):
            logger.warning(f"Unknown speaker '{speaker}', defaulting to user")
            speaker = self.config.SPEAKER_USER

        # 属性（必须提供）
        attribute = str(raw.get("attribute", "")).strip()
        if not attribute:
            logger.warning(f"Missing 'attribute' for fact: {fact_text}, discarding")
            return None  # 没有属性无法进行版本更新，丢弃

        # 原始话语
        original = user_input if speaker == self.config.SPEAKER_USER else assistant_response

        # 元数据增强
        metadata = {
            "entities": [],  # 可留作后续NER填充
            "negation": self._detect_negation(fact_text),
            "temporal": "present"
        }

        # 构建 Fact 对象
        fact = Fact(
            fact_text=fact_text,
            confidence=confidence,
            type=validated_type,
            speaker=speaker,
            source=source_id or "unknown",
            original_utterance=original,
            attribute=attribute,
            is_plausible=is_plausible,
            metadata=metadata
        )
        return fact

    def _detect_negation(self, text: str) -> bool:
        negations = {"不", "没", "不是", "不喜欢", "don't", "doesn't"}
        return any(neg in text.lower() for neg in negations)
