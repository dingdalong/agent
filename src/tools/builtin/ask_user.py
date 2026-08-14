"""ask_user — 让 agent 能主动向用户提问（支持一次提出多个各自独立的问题）。"""
from __future__ import annotations
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from src.events.menu import FormQuestion
from src.events.types import caller_identity
from src.tools.policy import AccessKind, DataFlow, ToolPolicy
from src.tools.decorator import tool

if TYPE_CHECKING:
    from src.agent import Agent, AgentDeps


class Option(BaseModel):
    """问题的一个可选项。"""
    label: str = Field(description="选项显示文本（支持 Markdown），用户看到并选择的内容，需简洁，建议不超过一行")
    recommended: bool = Field(
        default=False,
        description="是否推荐该项（默认 false）；推荐项会置顶显示并紧接标注 (推荐)；单选与多选均允许多个推荐项",
    )
    description: str = Field(
        default="",
        description="该选项的参考说明（支持 Markdown），解释其含义、取舍或影响，供用户判断时参考；选项自明时可省略",
    )
    preview: str = Field(
        default="",
        description="该选项的预览内容（Markdown 格式），可包含代码块、ASCII 图、流程说明等，"
                    "帮助用户直观对比不同选项的区别；当任一选项提供 preview 时，"
                    "UI 切换为左右分栏布局，右侧实时展示当前光标所在选项的预览；"
                    "仅在单选模式下生效（multi_select 为 false）",
    )


class Question(BaseModel):
    """单个待向用户提出的问题。"""
    question: str = Field(description="要向用户提出的问题（支持 Markdown），需清晰具体")
    header: str = Field(
        description="问题的简短标签，用于表单顶部标签栏，需概括该问题主旨，6 个汉字宽度（12 列）以内，如「编程语言」「性能目标」",
    )
    options: list[Option] | None = Field(
        default=None,
        description="该问题的可选项列表：有固定候选项时提供，用户从菜单中选择；省略则该题仅作自由文本作答",
    )
    multi_select: bool = Field(
        default=False,
        description="是否允许多选；true 时用户用空格勾选多个选项（仅在提供 options 时有意义）",
    )


class AskUser(BaseModel):
    """ask_user 的参数模型。"""
    questions: list[Question] = Field(
        description="问题列表，1 到 5 个，每题各自独立作答",
        min_length=1,
        max_length=5,
    )


@tool(model=AskUser,
      description=(
          "当你需要用户补充信息、做出选择或确认后才能继续时调用。"
          "一次弹出一个标签页表单：questions 中每个问题占一个标签页，用户逐题作答后一并返回。"
          "每个需用户拍板的独立维度应是 questions 中的一条——严禁把多个问题塞进同一段问题文本，"
          "或把不同维度合并成一个组合选项（如「Python + 格子法」）。"
          "每题末尾恒有「其它」输入行，用户可不选给定项自行作答（因此无需再加「其他」类选项）；"
          "题干、选项标签和选项说明支持 Markdown；选项可设 recommended=true 表示推荐，"
          "推荐项自动置顶并标注 (推荐)。"
          "表单底部有讨论栏，用户的疑问或补充会以「讨论：…」附在返回末尾。"
          "返回逐题配对的「问题 + 回答」；用户取消或漏答的项以哨兵串标注。"
      ),
      policy=ToolPolicy(AccessKind.INTERNAL, DataFlow.LOCAL, plan_safe=True), subagent=False, counts_as_work=False)
async def ask_user(questions: list[dict], deps: AgentDeps, agent: Agent) -> str:
    """向用户提出一个或多个问题并返回逐题作答。

    Args:
        questions: 问题列表，字段结构见 Question/Option 模型。
        deps: Agent 依赖容器，提供事件总线。
        agent: 发起提问的 Agent 实例，用于在表单上标注是哪个 agent 提问。
    Returns:
        逐题配对的「问题 + 回答」文本，末尾附用户在讨论栏填写的「讨论：…」（若有）；
        用户取消或未作答的项以哨兵串标注，便于 LLM 察觉残缺。
    """
    form_questions = []
    for q in questions:
        options = q.get("options") or []
        if options:
            # 稳定排序：推荐项置顶，推荐组与普通组内部各自保持原顺序
            options = sorted(options, key=lambda o: not o.get("recommended", False))
        form_questions.append(FormQuestion(
            question=q["question"],
            options=[(o["label"], o["label"]) for o in options] if options else None,
            descriptions=[o.get("description", "") for o in options] if options else None,
            previews=[o.get("preview", "") for o in options] if options else None,
            recommended=[bool(o.get("recommended", False)) for o in options] if options else None,
            multi_select=q.get("multi_select", False),
            header=q.get("header", ""),
        ))
    caller_agent_type, caller_uuid = caller_identity(agent)
    answers, discussion = await deps.event_bus.request_form(
        form_questions, prompt="🤖 **提问**", markdown=True,
        caller_agent_type=caller_agent_type, caller_uuid=caller_uuid,
    )
    if not answers and not discussion.strip():
        return "[用户取消了作答，未回答任何问题]"
    lines: list[str] = []
    for i, q in enumerate(questions):
        answer = answers[i].strip() if i < len(answers) else ""
        lines.append(f"问题{i + 1}：{q['question']}\n回答：{answer or '[未作答]'}")
    if discussion.strip():
        lines.append(f"讨论：{discussion.strip()}")
    return "\n\n".join(lines)
