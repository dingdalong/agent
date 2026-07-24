"""确定性工具集：补齐 LLM 无法可靠完成的操作（真随机、当前时间/日期运算、
哈希编码、精确文本统计）。四个工具均为 readonly 纯计算，无 feature 门控、
无外部依赖（全部 stdlib），声明为普通 def 由装饰器自动 to_thread 卸载。"""

import base64
import hashlib
import random as _random
import secrets
import string
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from src.tools.decorator import ToolPermission, tool

# 星期索引（datetime.weekday() 返回 0=周一）对应的中英文名，避免 strftime 的 locale 依赖。
_WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
_WEEKDAY_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _weekday_name(dt: datetime) -> str:
    """返回日期对应的星期名，格式 '星期四 (Thursday)'。

    Args:
        dt: 目标日期时间。
    Returns:
        中英文星期名字符串。
    """
    idx = dt.weekday()
    return f"{_WEEKDAY_CN[idx]} ({_WEEKDAY_EN[idx]})"


# ---------------------------------------------------------------------------
# 工具 1：random —— 生成各类真随机值
# ---------------------------------------------------------------------------

class RandomInput(BaseModel):
    """生成随机值。LLM 自身无法产生真随机，交由此工具。"""
    operation: Literal[
        "int", "float", "choice", "sample", "shuffle",
        "uuid", "password", "token_hex", "dice", "coin",
    ] = Field(description="随机操作类型")
    low: Optional[int] = Field(None, description="int 下界（含），缺省 0")
    high: Optional[int] = Field(None, description="int 上界（含）/float 上界（不含），缺省 int=100、float=1")
    items: Optional[list[str]] = Field(None, description="choice/sample/shuffle 的候选列表")
    count: int = Field(1, description="int 生成个数，或 sample 抽取个数")
    length: Optional[int] = Field(None, description="password/token_hex 长度，缺省 16")
    sides: int = Field(6, description="dice 每颗骰子的面数")
    num_dice: int = Field(1, description="dice 骰子数量")


@tool(
    model=RandomInput,
    name="random",
    description="生成真随机值：随机整数/浮点、从列表随机选取/抽样/洗牌、UUID、密码、"
                "十六进制令牌、掷骰、抛硬币。LLM 无法自行产生真随机，需要随机结果时用此工具。",
    permission=ToolPermission(kind="readonly"),
)
def random_value(
    operation: str,
    low: Optional[int] = None,
    high: Optional[int] = None,
    items: Optional[list[str]] = None,
    count: int = 1,
    length: Optional[int] = None,
    sides: int = 6,
    num_dice: int = 1,
) -> str:
    """按 operation 生成随机值。

    Args:
        operation: 随机操作类型（见 RandomInput）。
        low: int 下界（含），缺省 0。
        high: int 上界（含）/float 上界（不含）。
        items: choice/sample/shuffle 的候选列表。
        count: int 生成个数或 sample 抽取个数。
        length: password/token_hex 长度，缺省 16。
        sides: dice 每颗骰子面数。
        num_dice: dice 骰子数量。
    Returns:
        随机结果字符串；参数非法时返回以「随机生成错误：」开头的说明。
    """
    try:
        if operation == "int":
            lo = 0 if low is None else low
            hi = 100 if high is None else high
            if lo > hi:
                raise ValueError("low 不能大于 high")
            if count <= 1:
                return f"随机整数: {_random.randint(lo, hi)}"
            nums = [_random.randint(lo, hi) for _ in range(count)]
            return f"随机整数({count}个): {nums}"
        if operation == "float":
            lo = 0.0 if low is None else float(low)
            hi = 1.0 if high is None else float(high)
            if lo > hi:
                raise ValueError("low 不能大于 high")
            return f"随机浮点: {_random.uniform(lo, hi)}"
        if operation in ("choice", "sample", "shuffle"):
            if not items:
                raise ValueError(f"{operation} 需要非空的 items 列表")
            if operation == "choice":
                return f"随机选取: {_random.choice(items)}"
            if operation == "sample":
                if count > len(items):
                    raise ValueError(f"sample 个数 {count} 超过候选数 {len(items)}")
                return f"随机抽样({count}个): {_random.sample(items, count)}"
            shuffled = list(items)
            _random.shuffle(shuffled)
            return f"洗牌结果: {shuffled}"
        if operation == "uuid":
            return f"UUID: {uuid.uuid4()}"
        if operation == "password":
            n = 16 if length is None else length
            alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
            pwd = "".join(secrets.choice(alphabet) for _ in range(n))
            return f"随机密码: {pwd}"
        if operation == "token_hex":
            n = 16 if length is None else length
            return f"十六进制令牌: {secrets.token_hex(n)}"
        if operation == "dice":
            rolls = [_random.randint(1, sides) for _ in range(num_dice)]
            return f"掷骰({num_dice}d{sides}): 点数={rolls}, 总和={sum(rolls)}"
        if operation == "coin":
            return f"抛硬币: {_random.choice(['heads', 'tails'])}"
        raise ValueError(f"不支持的 operation: {operation}")
    except Exception as e:
        return f"随机生成错误：{e}"


# ---------------------------------------------------------------------------
# 工具 2：datetime —— 当前时间与日期运算
# ---------------------------------------------------------------------------

class DateTimeInput(BaseModel):
    """获取当前时间或做日期运算。LLM 不知道"现在"，且日期加减/求差常出错。"""
    operation: Literal[
        "now", "diff", "add", "weekday", "to_timestamp", "from_timestamp",
    ] = Field(description="时间操作类型")
    timezone_name: Optional[str] = Field(None, description="IANA 时区名，如 Asia/Shanghai，缺省 UTC")
    date1: Optional[str] = Field(None, description="ISO 日期/时间字符串，如 2026-07-24 或 2026-07-24T10:30:00")
    date2: Optional[str] = Field(None, description="diff 的第二个 ISO 日期/时间字符串")
    amount: Optional[int] = Field(None, description="add 的增量（可为负）")
    unit: Optional[Literal["days", "hours", "minutes", "weeks"]] = Field(None, description="add 的时间单位")
    timestamp: Optional[float] = Field(None, description="from_timestamp 的 Unix 时间戳（秒）")


def _resolve_tz(timezone_name: Optional[str]) -> timezone | ZoneInfo:
    """解析时区名，缺省返回 UTC。

    Args:
        timezone_name: IANA 时区名，None 表示 UTC。
    Returns:
        时区对象。
    """
    return ZoneInfo(timezone_name) if timezone_name else timezone.utc


@tool(
    model=DateTimeInput,
    name="datetime",
    description="当前时间与日期运算：获取现在的时间（带时区/星期/时间戳）、两个日期相差多少、"
                "日期加减、某天星期几、日期与 Unix 时间戳互转。LLM 不知道当前时间且日期运算易错，需要时用此工具。",
    permission=ToolPermission(kind="readonly"),
)
def datetime_tool(
    operation: str,
    timezone_name: Optional[str] = None,
    date1: Optional[str] = None,
    date2: Optional[str] = None,
    amount: Optional[int] = None,
    unit: Optional[str] = None,
    timestamp: Optional[float] = None,
) -> str:
    """按 operation 获取当前时间或做日期运算。

    Args:
        operation: 时间操作类型（见 DateTimeInput）。
        timezone_name: IANA 时区名，缺省 UTC。
        date1: 主 ISO 日期/时间字符串。
        date2: diff 的第二个 ISO 日期/时间字符串。
        amount: add 的增量。
        unit: add 的时间单位（days/hours/minutes/weeks）。
        timestamp: from_timestamp 的 Unix 时间戳。
    Returns:
        结果字符串；参数非法时返回以「时间运算错误：」开头的说明。
    """
    try:
        tz = _resolve_tz(timezone_name)
        if operation == "now":
            now = datetime.now(tz)
            return (f"当前时间: {now.isoformat()}\n"
                    f"星期: {_weekday_name(now)}\n"
                    f"Unix 时间戳: {now.timestamp()}")
        if operation == "diff":
            if not date1 or not date2:
                raise ValueError("diff 需要 date1 和 date2")
            d1 = datetime.fromisoformat(date1)
            d2 = datetime.fromisoformat(date2)
            delta = d2 - d1
            return (f"{date1} 到 {date2}: 相差 {delta.days} 天, "
                    f"共 {delta.total_seconds()} 秒")
        if operation == "add":
            if not date1 or amount is None or not unit:
                raise ValueError("add 需要 date1、amount 和 unit")
            base = datetime.fromisoformat(date1)
            result = base + timedelta(**{unit: amount})
            return f"{date1} 加 {amount} {unit} = {result.isoformat()} ({_weekday_name(result)})"
        if operation == "weekday":
            if not date1:
                raise ValueError("weekday 需要 date1")
            return f"{date1} 是 {_weekday_name(datetime.fromisoformat(date1))}"
        if operation == "to_timestamp":
            if not date1:
                raise ValueError("to_timestamp 需要 date1")
            d = datetime.fromisoformat(date1)
            if d.tzinfo is None:
                d = d.replace(tzinfo=tz)
            return f"{date1} 的 Unix 时间戳: {d.timestamp()}"
        if operation == "from_timestamp":
            if timestamp is None:
                raise ValueError("from_timestamp 需要 timestamp")
            d = datetime.fromtimestamp(timestamp, tz)
            return f"时间戳 {timestamp} = {d.isoformat()} ({_weekday_name(d)})"
        raise ValueError(f"不支持的 operation: {operation}")
    except Exception as e:
        return f"时间运算错误：{e}"


# ---------------------------------------------------------------------------
# 工具 3：encode —— 编解码与哈希
# ---------------------------------------------------------------------------

class EncodeInput(BaseModel):
    """对文本做编解码或哈希。LLM 无法逐字符心算这些确定性变换。"""
    operation: Literal[
        "base64_encode", "base64_decode", "hex_encode", "hex_decode",
        "url_encode", "url_decode", "md5", "sha1", "sha256",
    ] = Field(description="编解码/哈希操作类型")
    text: str = Field(description="输入文本（按 UTF-8 处理）")


@tool(
    model=EncodeInput,
    description="文本编解码与哈希：base64、hex、URL 编解码，md5/sha1/sha256 哈希。"
                "这些逐字符的确定性变换 LLM 无法可靠心算，需要时用此工具。",
    permission=ToolPermission(kind="readonly"),
)
def encode(operation: str, text: str) -> str:
    """按 operation 对 text 做编解码或哈希。

    Args:
        operation: 编解码/哈希操作类型（见 EncodeInput）。
        text: 输入文本，按 UTF-8 处理。
    Returns:
        变换结果字符串；参数非法或解码失败时返回以「编解码错误：」开头的说明。
    """
    try:
        if operation == "base64_encode":
            return f"base64: {base64.b64encode(text.encode('utf-8')).decode('ascii')}"
        if operation == "base64_decode":
            return f"解码: {base64.b64decode(text).decode('utf-8')}"
        if operation == "hex_encode":
            return f"hex: {text.encode('utf-8').hex()}"
        if operation == "hex_decode":
            return f"解码: {bytes.fromhex(text).decode('utf-8')}"
        if operation == "url_encode":
            return f"url: {urllib.parse.quote(text)}"
        if operation == "url_decode":
            return f"解码: {urllib.parse.unquote(text)}"
        if operation in ("md5", "sha1", "sha256"):
            digest = hashlib.new(operation, text.encode("utf-8")).hexdigest()
            return f"{operation}: {digest}"
        raise ValueError(f"不支持的 operation: {operation}")
    except Exception as e:
        return f"编解码错误：{e}"


# ---------------------------------------------------------------------------
# 工具 4：text_stats —— 精确文本统计
# ---------------------------------------------------------------------------

class TextStatsInput(BaseModel):
    """精确统计文本。LLM 因分词无法准确数字符/词/某子串出现次数。"""
    operation: Literal[
        "summary", "char_count", "byte_count", "word_count",
        "line_count", "count_substring", "reverse",
    ] = Field(description="统计操作类型")
    text: str = Field(description="被统计的文本")
    substring: Optional[str] = Field(None, description="count_substring 要统计的子串")


@tool(
    model=TextStatsInput,
    description="精确文本统计：字符数、字节数、词数、行数、某子串出现次数、字符串反转。"
                "LLM 因分词数不准这些数量（如'strawberry 有几个 r'），需要精确计数时用此工具。",
    permission=ToolPermission(kind="readonly"),
)
def text_stats(operation: str, text: str, substring: Optional[str] = None) -> str:
    """按 operation 精确统计 text。

    Args:
        operation: 统计操作类型（见 TextStatsInput）。
        text: 被统计的文本。
        substring: count_substring 要统计的子串。
    Returns:
        统计结果字符串；参数非法时返回以「文本统计错误：」开头的说明。
    """
    try:
        if operation == "summary":
            no_space = "".join(text.split())
            return (f"字符数: {len(text)}\n"
                    f"去空白字符数: {len(no_space)}\n"
                    f"字节数(UTF-8): {len(text.encode('utf-8'))}\n"
                    f"词数: {len(text.split())}\n"
                    f"行数: {len(text.splitlines())}")
        if operation == "char_count":
            return f"字符数: {len(text)}"
        if operation == "byte_count":
            return f"字节数(UTF-8): {len(text.encode('utf-8'))}"
        if operation == "word_count":
            return f"词数: {len(text.split())}"
        if operation == "line_count":
            return f"行数: {len(text.splitlines())}"
        if operation == "count_substring":
            if not substring:
                raise ValueError("count_substring 需要非空的 substring")
            return f"子串 '{substring}' 出现次数: {text.count(substring)}"
        if operation == "reverse":
            return f"反转: {text[::-1]}"
        raise ValueError(f"不支持的 operation: {operation}")
    except Exception as e:
        return f"文本统计错误：{e}"
