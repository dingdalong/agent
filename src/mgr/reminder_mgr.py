"""提醒注入管理器 — 集中管理 agent 运行循环中的提醒注入。

通过 register() 注册提醒源（如 PlanMgr、TaskManager），
在 agent 状态机的三个时机统一调度注入：
- turn start: prepend 到用户输入
- tool round: 通知各提醒源更新内部状态
- post round: 收集需要追加到 messages 的提醒消息

提醒源通过 duck typing 识别：
- get_turn_start_reminder(plan_active) -> str: 返回纯文本内容
- notify_tool_round(tool_names) -> None
- pop_post_round_reminder(plan_active) -> str | None: 返回纯文本内容
提醒源只需实现所需的方法，未实现的方法会被跳过。
所有注入内容统一用 <reminder> 标签包装，由 ReminderMgr 处理。
"""

from __future__ import annotations

from typing import Any


class ReminderMgr:
    """集中管理 agent 运行循环中的提醒注入。

    Attributes:
        _providers: 已注册的提醒源列表，按注册顺序迭代。
    """

    def __init__(self) -> None:
        self._providers: list[Any] = []

    def register(self, provider: Any) -> None:
        """注册提醒源。重复注册同一对象会被忽略。

        Args:
            provider: 实现了至少一个提醒接口方法的对象。
        """
        if provider not in self._providers:
            self._providers.append(provider)

    def unregister(self, provider: Any) -> None:
        """注销提醒源。provider 不存在时静默跳过。

        Args:
            provider: 要注销的提醒源对象。
        """
        try:
            self._providers.remove(provider)
        except ValueError:
            pass

    def build_turn_start_instructions(
        self, plan_active: bool,
    ) -> str:
        """收集所有提醒源的 turn start 注入文本，用 <reminder> 标签包装后拼接。

        在 _on_request_input 和 run() 子智能体路径中调用。

        Args:
            plan_active: 调用方 agent 是否处于 Plan。

        Returns:
            用 <reminder> 包装并拼接后的注入文本。无注入时返回空串。
        """
        parts: list[str] = []
        for p in self._providers:
            fn = getattr(p, "get_turn_start_reminder", None)
            if fn is None:
                continue
            text = fn(plan_active)
            if text:
                parts.append(f"<reminder>{text}</reminder>")
        return "\n\n".join(parts)

    def notify_tool_round(self, tool_names: list[str]) -> None:
        """通知所有提醒源一轮工具执行已完成。

        在 _on_execute_tools 末尾调用。

        Args:
            tool_names: 本轮调用的工具名列表，供提醒源判断是否重置内部计数。
        """
        for p in self._providers:
            fn = getattr(p, "notify_tool_round", None)
            if fn is not None:
                fn(tool_names)

    def collect_post_round_messages(
        self, plan_active: bool,
    ) -> list[dict]:
        """收集所有提醒源的 post-round 消息，用 <reminder> 标签包装后构造消息字典。

        在 _on_post_round 开头调用，返回值逐条追加到 ctx.messages。
        提醒源只需返回纯文本内容（str），格式包装由本方法统一处理。

        Args:
            plan_active: 调用方 agent 是否处于 Plan。

        Returns:
            需追加到 messages 的消息字典列表。
        """
        msgs: list[dict] = []
        for p in self._providers:
            fn = getattr(p, "pop_post_round_reminder", None)
            if fn is None:
                continue
            text = fn(plan_active)
            if text:
                msgs.append({
                    "role": "user",
                    "content": f"<reminder>{text}</reminder>",
                })
        return msgs
