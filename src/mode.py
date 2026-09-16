"""Agent 运行模式。"""

from enum import StrEnum


class RunMode(StrEnum):
    EXECUTE = "execute"
    PLAN = "plan"

    @property
    def display_name(self) -> str:
        return "计划模式" if self is RunMode.PLAN else "普通模式"


PLAN_MODE_ALLOWED_ACTIONS = (
    "读取和搜索本地文件、代码、配置、日志以及 Git 元数据和历史",
    "执行只读的系统与环境检查，以及经过隐私预检的外部资料读取",
    "运行现有单元测试、集成测试和端到端测试；不得修改项目或外部状态，缓存、临时文件和测试输出只能写入框架专用临时目录",
    "使用明确标记为计划安全的内部控制操作",
)

PLAN_MODE_PROHIBITION = (
    "禁止创建、编辑、删除或移动项目文件，禁止申请额外写权限或网络权限，"
    "禁止通过已有可写进程间接修改项目；仅用户或计划审核可以切换模式。"
)


def plan_mode_boundary(*, can_submit_plan: bool) -> str:
    """构建 chat 前注入的完整 Plan 模式边界。"""
    actions = "\n".join(f"- {action}。" for action in PLAN_MODE_ALLOWED_ACTIONS)
    submission = (
        "\n- 计划正文只能通过 submit_plan 受控保存；这不授予其他项目写权限。"
        if can_submit_plan
        else ""
    )
    return (
        "# 当前模式：计划模式\n"
        "你当前处于计划模式。此模式只允许调查与规划，不允许实施项目改动。\n"
        "允许行为仅包括：\n"
        f"{actions}{submission}\n"
        f"{PLAN_MODE_PROHIBITION}"
    )


def plan_mode_denial_reminder() -> str:
    """构建权限拒绝中使用的紧凑 Plan 限制说明。"""
    allowed = "；".join(PLAN_MODE_ALLOWED_ACTIONS)
    return f"计划模式仅允许：{allowed}。{PLAN_MODE_PROHIBITION}"
