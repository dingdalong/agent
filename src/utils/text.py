import re

def extract_json(text: str) -> str:
    """从可能包含 Markdown 代码块的文本中提取 JSON 字符串"""
    pattern = r'```(?:json)?\s*([\s\S]*?)\s*```'
    match = re.search(pattern, text)
    return match.group(1).strip() if match else text.strip()
