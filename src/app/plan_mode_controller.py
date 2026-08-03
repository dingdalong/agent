"""入口 Agent 的 Plan 状态与 Shift+Tab 协调器。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from src.events.types import PlanStateChanged
from src.interfaces.base import UserInterface


class PlanModeController:
    def __init__(self, ui: UserInterface, event_bus: Any) -> None:
        self.ui = ui
        self.event_bus = event_bus
        self.agent: Any = None
        self.ui.set_plan_state_provider(
            lambda: bool(self.agent is not None and self.agent.plan_active)
        )

    def install_shortcut(self, agent: Any) -> None:
        self.agent = agent
        self.ui.set_plan_toggle_handler(self.toggle)

    def toggle(self) -> bool:
        if self.agent is None:
            return False
        active = not self.agent.plan_active
        changed = self.agent.set_plan_active(active)
        if not changed:
            return False
        self.notify_state_changed()
        try:
            asyncio.create_task(self.event_bus.emit(PlanStateChanged(
                timestamp=time.time(),
                source=self.agent.agent_type,
                active=active,
            )))
        except RuntimeError:
            pass
        return True

    def notify_state_changed(self) -> None:
        self.ui.on_plan_state_changed()
