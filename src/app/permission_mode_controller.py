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

_PERMISSION_MODE_MENU = "\n权限模式:\n" + "\n".join(
    f"  [{index}] {mode.value:<20} - {mode.description}"
    for index, mode in enumerate(MENU_MODES, start=1)
) + "\n"
_PERMISSION_MODE_PROMPT = f"选择 (1-{len(MENU_MODES)} 或模式名): "


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
        self.install_state_provider()

    def install_state_provider(self) -> None:
        """向 UI 注册权限模式状态提供函数。"""
        self.ui.set_system_state_provider(
            lambda: SystemState(permission_mode=self.permission_mgr.mode.value)
        )

    async def prompt_selection(self, agent: Any) -> bool:
        """显示权限模式菜单并等待用户选择。

        Args:
            agent: Agent 实例，模式变更后刷新其 schema。

        Returns:
            模式是否发生了变化。
        """
        current = self.permission_mgr.mode.value
        await self.event_bus.request_output(
            f"{_PERMISSION_MODE_MENU}  当前权限模式: {current}\n"
        )
        try:
            answer = await self.event_bus.request_input(_PERMISSION_MODE_PROMPT)
        except (asyncio.CancelledError, KeyboardInterrupt, NoEventSubscribers):
            return False

        mode = parse_permission_mode(answer)
        if mode is None:
            await self.event_bus.request_output(
                f"无效选择，保持当前权限模式: {current}\n"
            )
            return False

        changed = self.permission_mgr.set_mode(mode)
        await self.event_bus.request_output(f"已切换到 {mode.value} 权限模式。\n")
        if changed:
            self._refresh_agent(agent)
        return changed

    def install_shortcut(self, agent: Any) -> None:
        """注册 Shift+Tab 权限模式轮转快捷键。

        Args:
            agent: Agent 实例。
        """
        self.ui.set_permission_mode_toggle_handler(lambda: self._cycle(agent))

    def clear_shortcut(self) -> None:
        """移除 Shift+Tab 快捷键绑定。"""
        self.ui.set_permission_mode_toggle_handler(None)

    def notify_state_changed(self) -> None:
        """通知 UI 权限模式已变更。"""
        self.ui.on_system_state_changed()

    def cycle_mode(self, agent: Any) -> bool:
        """在 CAROUSEL_MODES 中循环切换权限模式。

        Args:
            agent: Agent 实例。

        Returns:
            模式是否发生了变化。
        """
        current_index = 0
        for index, mode in enumerate(CAROUSEL_MODES):
            current_mode = self.permission_mgr.mode
            if mode is current_mode or mode.value == current_mode.value:
                current_index = index
                break
        next_mode = CAROUSEL_MODES[(current_index + 1) % len(CAROUSEL_MODES)]
        changed = self.permission_mgr.set_mode(next_mode)
        if changed:
            self._refresh_agent(agent)
        return changed

    def _cycle(self, agent: Any) -> None:
        """Shift+Tab 快捷键回调。"""
        self.cycle_mode(agent)

    def _refresh_agent(self, agent: Any) -> None:
        """刷新 agent schema 和 UI 状态。"""
        agent.refresh_tools_schemas()
        self.notify_state_changed()
