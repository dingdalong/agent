from src.tools import AccessKind, DataFlow, ToolPolicy
from src.tools.decorator import tool
from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from ddgs import DDGS

def format_search_results(results: List[Dict], max_snippet_len: int = 300) -> str:
    """将搜索结果列表格式化为 LLM 友好的文本块"""
    if not results:
        return "未找到相关结果。"

    formatted = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "无标题").strip()
        href = r.get("href", "")
        body = r.get("body", "").strip()
        # 截断过长的摘要
        if len(body) > max_snippet_len:
            body = body[:max_snippet_len] + "..."
        formatted.append(f"[{i}] {title}\n    URL: {href}\n    摘要: {body}")

    return "\n\n".join(formatted)

class WebSearch(BaseModel):
    query: str = Field(..., description="要锁搜的内容")
    max_results: Optional[int] = Field(None, description="搜索结果条数")

@tool(model=WebSearch, description="当需要实时信息、最新新闻或未知事实时，使用此工具进行联网搜索。",
      policy=ToolPolicy(AccessKind.REVIEW, DataFlow.EXTERNAL, detail_template="搜索：{query}"))
def web_search(query: str, max_results: int = 5) -> str:
    try:
        ddgs = DDGS()
        text_results = list(ddgs.text(query, region = "wt-wt", safesearch="strict", timelimit="w", max_results=max_results))
        context = format_search_results(text_results)
        return context
    except Exception as e:
        return f"搜索失败: {str(e)}"
