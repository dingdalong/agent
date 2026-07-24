"""utility.py 四个确定性工具的单元测试：确定性操作断言精确值，
随机操作断言性质，非法/缺参数走错误路径返回字符串不抛异常。

工具函数经 @tool 装饰后仍是原函数（装饰器 return func），且不依赖注入的
context，故可直接 import 调用。"""

from __future__ import annotations

import uuid

from src.tools.builtin.utility import (
    datetime_tool,
    encode,
    random_value,
    text_stats,
)


# ---------------------------------------------------------------------------
# encode —— 已知向量精确断言
# ---------------------------------------------------------------------------

def test_encode_base64_roundtrip():
    assert encode("base64_encode", "hello") == "base64: aGVsbG8="
    assert encode("base64_decode", "aGVsbG8=") == "解码: hello"


def test_encode_hex_roundtrip():
    assert encode("hex_encode", "hi") == "hex: 6869"
    assert encode("hex_decode", "6869") == "解码: hi"


def test_encode_url():
    assert encode("url_encode", "a b/c") == "url: a%20b/c"
    assert encode("url_decode", "a%20b") == "解码: a b"


def test_encode_hashes():
    assert encode("md5", "hello") == "md5: 5d41402abc4b2a76b9719d911017c592"
    assert encode("sha1", "hello") == "sha1: aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d"
    assert encode("sha256", "hello") == (
        "sha256: 2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_encode_bad_operation_returns_error():
    out = encode("rot13", "hello")
    assert out.startswith("编解码错误：")


# ---------------------------------------------------------------------------
# text_stats —— 精确计数
# ---------------------------------------------------------------------------

def test_text_stats_counts():
    assert text_stats("char_count", "hello") == "字符数: 5"
    assert text_stats("byte_count", "你好") == "字节数(UTF-8): 6"
    assert text_stats("word_count", "hello world foo") == "词数: 3"
    assert text_stats("line_count", "a\nb\nc") == "行数: 3"
    assert text_stats("reverse", "abc") == "反转: cba"


def test_text_stats_count_substring_strawberry():
    # 经典"strawberry 有几个 r"——LLM 常数错。
    assert text_stats("count_substring", "strawberry", "r") == "子串 'r' 出现次数: 3"


def test_text_stats_summary_contains_all_metrics():
    out = text_stats("summary", "a b\nc")
    assert "字符数: 5" in out
    assert "词数: 3" in out
    assert "行数: 2" in out


def test_text_stats_count_substring_missing_arg_returns_error():
    out = text_stats("count_substring", "abc")
    assert out.startswith("文本统计错误：")


# ---------------------------------------------------------------------------
# datetime —— 确定性日期运算
# ---------------------------------------------------------------------------

def test_datetime_diff():
    out = datetime_tool("diff", date1="2026-01-01", date2="2026-01-11")
    assert "相差 10 天" in out


def test_datetime_add():
    out = datetime_tool("add", date1="2026-01-01", amount=10, unit="days")
    assert "2026-01-11" in out


def test_datetime_weekday():
    # 2000-01-01 是星期六。
    out = datetime_tool("weekday", date1="2000-01-01")
    assert "星期六" in out and "Saturday" in out


def test_datetime_from_timestamp_utc():
    out = datetime_tool("from_timestamp", timestamp=0, timezone_name="UTC")
    assert "1970-01-01" in out and "Thursday" in out


def test_datetime_to_timestamp_utc():
    out = datetime_tool("to_timestamp", date1="1970-01-01T00:00:00", timezone_name="UTC")
    assert "0.0" in out


def test_datetime_now_has_fields():
    out = datetime_tool("now", timezone_name="UTC")
    assert "当前时间:" in out and "星期:" in out and "Unix 时间戳:" in out


def test_datetime_diff_missing_arg_returns_error():
    out = datetime_tool("diff", date1="2026-01-01")
    assert out.startswith("时间运算错误：")


# ---------------------------------------------------------------------------
# random —— 断言性质而非精确值
# ---------------------------------------------------------------------------

def test_random_int_in_range():
    for _ in range(50):
        out = random_value("int", low=1, high=6)
        val = int(out.split(": ")[1])
        assert 1 <= val <= 6


def test_random_sample_distinct_subset():
    items = ["a", "b", "c", "d"]
    out = random_value("sample", items=items, count=2)
    picked = out.split(": ", 1)[1]  # "['a', 'c']"
    chosen = eval(picked)
    assert len(chosen) == 2
    assert len(set(chosen)) == 2
    assert set(chosen) <= set(items)


def test_random_shuffle_is_permutation():
    items = ["a", "b", "c", "d"]
    out = random_value("shuffle", items=items)
    shuffled = eval(out.split(": ", 1)[1])
    assert sorted(shuffled) == sorted(items)


def test_random_uuid_valid():
    out = random_value("uuid")
    token = out.split(": ")[1]
    # 能被 uuid.UUID 解析即为合法。
    assert str(uuid.UUID(token)) == token


def test_random_password_length():
    out = random_value("password", length=20)
    pwd = out.split(": ", 1)[1]
    assert len(pwd) == 20


def test_random_token_hex_length():
    out = random_value("token_hex", length=8)
    token = out.split(": ")[1]
    assert len(token) == 16  # token_hex(n) 产出 2n 个十六进制字符
    assert all(c in "0123456789abcdef" for c in token)


def test_random_coin():
    out = random_value("coin")
    assert out.split(": ")[1] in ("heads", "tails")


def test_random_dice_sum():
    out = random_value("dice", sides=6, num_dice=3)
    assert "掷骰(3d6)" in out and "总和=" in out


def test_random_choice_empty_items_returns_error():
    out = random_value("choice", items=[])
    assert out.startswith("随机生成错误：")


def test_random_bad_operation_returns_error():
    out = random_value("teleport")
    assert out.startswith("随机生成错误：")
