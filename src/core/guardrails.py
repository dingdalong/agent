import re
from typing import Tuple

class InputGuardrail:
    """输入安全检查（关键词+正则）"""
    def __init__(self, blocked_patterns=None):
        self.blocked_patterns = blocked_patterns or [
            r"忽略.*指令|忽略.*系统提示",
            r"删除.*文件|rm\s+-rf",
            r"DROP\s+TABLE",
            r"eval\s*\(",
            r"exec\s*\(",
        ]

    def check(self, user_input: str) -> Tuple[bool, str]:
        """返回 (是否通过, 拒绝理由)"""
        for pattern in self.blocked_patterns:
            if re.search(pattern, user_input, re.IGNORECASE):
                return False, f"输入包含不安全内容（匹配模式：{pattern}）"
        return True, ""

class OutputGuardrail:
    """输出安全检查"""
    def __init__(self, blocked_content=None):
        self.blocked_content = blocked_content or [
            "rm -rf",
            "DROP TABLE",
            "eval(",
        ]

    def check(self, output: str) -> Tuple[bool, str]:
        for phrase in self.blocked_content:
            if phrase in output:
                return False, f"输出包含不安全内容：{phrase}"
        return True, ""
