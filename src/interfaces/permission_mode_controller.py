"""权限模式 UI 协调器。"""

from __future__ import annotations

import asyncio
from typing import Any

from src.events import NoEventSubscribers
from src.interfaces.base import SystemState, UserInterface
from src.mgr.permission_mgr import (
    CAROUSEL_MODES,
    MENU_MODES,
    PermissionManager,
    parse_permission_mode,
)

class PermissionModeController:
    """协调权限模式交互、UI 状态和 agent 工具 schema 刷新。"""

    def __init__(
        self,
        permission_mgr: PermissionManager,
        ui: UserInterface,
        event_bus: Any,
    ) -> None:
        self.permission_mgr = permission_mgr
        self.ui = ui
        self.event_bus = event_bus
        self.agent: Any = None
        self.install_state_provider()

    def install_state_provider(self) -> None:
        """向 UI 注册权限模式状态提供函数。

        状态条显示主 agent 的当前模式；agent 尚未绑定时回退到 default_mode。
        """
        self.ui.set_system_state_provider(
            lambda: SystemState(
                permission_mode=(
                    self.agent.permission_mode.value
                    if self.agent is not None
                    else self.permission_mgr.default_mode.value
                )
            )
        )

    async def prompt_selection(self) -> bool:
        """以方向键选择菜单展示权限模式并等待用户选择，作用于已绑定的主 agent。

        Returns:
            模式是否发生了变化（Esc 取消或无效选择时为 False）。
        """
        current = self.agent.permission_mode
        options = [(mode.value, f"{mode.value} - {mode.description}") for mode in MENU_MODES]
        default_index = next((i for i, mode in enumerate(MENU_MODES) if mode.value == current.value), 0)
        try:
            answer = await self.event_bus.request_choice(
                f"\n权限模式（当前: {current.value}）", options, default_index
            )
        except (asyncio.CancelledError, KeyboardInterrupt, NoEventSubscribers):
            return False

        if not answer:  # Esc 取消，静默
            return False
        mode = parse_permission_mode(answer)
        if mode is None:
            return False

        changed = self.agent.set_permission_mode(mode)
        await self.event_bus.request_output(f"已切换到 {mode.value} 权限模式。\n")
        if changed:
            self._refresh_agent()
        return changed

    def install_shortcut(self, agent: Any) -> None:
        """绑定主 agent 并注册 Shift+Tab 权限模式轮转快捷键。

        Args:
            agent: 主 Agent 实例，同时作为状态条显示的模式来源。
        """
        self.agent = agent
        self.ui.set_permission_mode_toggle_handler(lambda: self.cycle_mode())

    def notify_state_changed(self) -> None:
        """通知 UI 权限模式已变更。"""
        self.ui.on_system_state_changed()

    def cycle_mode(self) -> bool:
        """在 CAROUSEL_MODES 中循环切换已绑定主 agent 的权限模式（Shift+Tab 回调）。

        Returns:
            模式是否发生了变化。
        """
        current_index = 0
        for index, mode in enumerate(CAROUSEL_MODES):
            current_mode = self.agent.permission_mode
            if mode is current_mode or mode.value == current_mode.value:
                current_index = index
                break
        next_mode = CAROUSEL_MODES[(current_index + 1) % len(CAROUSEL_MODES)]
        changed = self.agent.set_permission_mode(next_mode)
        if changed:
            self._refresh_agent()
        return changed

    def _refresh_agent(self) -> None:
        """刷新已绑定主 agent 的 schema 和 UI 状态。"""
        self.agent.refresh_tools_schemas()
        self.notify_state_changed()
