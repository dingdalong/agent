class TodoManager:
    def __init__(self):
        self.items = []
        self._rounds_without_update: int = 0

    async def update(self, items: list) -> str:
        validated, ip = [], 0
        for i, item in enumerate(items):
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "pending")).lower()
            af = str(item.get("active_form", "")).strip()

            if not content:
                raise ValueError(f"Item {i}: 缺失字段：content")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {i}: 无效状态：'{status}'")
            if not af:
                raise ValueError(f"Item {i}: 缺失字段：active_form")
            if status == "in_progress":
                ip += 1

            validated.append({"content": content, "status": status, "active_form": af})

        if len(validated) > 20:
            raise ValueError("待办事项最多20条")

        if ip > 1:
            raise ValueError("只允许存在一个 in_progress 状态。")

        self.items = validated
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "没有待办事项."
        lines = []
        for item in self.items:
            m = {
                "completed": "[x]",
                "in_progress": "[>]",
                "pending": "[ ]"
                }.get(item["status"], "[?]")
            suffix = f" <- {item['active_form']}" if item["status"] == "in_progress" else ""
            lines.append(f"{m} {item['content']}{suffix}")

        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)

    def has_open_items(self) -> bool:
        """检查是否存在未完成的待办事项。

        Returns:
            True 表示存在未完成项。
        """
        return any(item.get("status") != "completed" for item in self.items)

    # ── 提醒注入接口（由 ReminderMgr 调用）──────────────────────────

    def notify_tool_round(self, tool_names: list[str]) -> None:
        """工具执行轮结束时更新轮次计数。

        Args:
            tool_names: 本轮调用的工具名列表。包含 "todo_write" 时重置计数器。
        """
        if "todo_write" in tool_names:
            self._rounds_without_update = 0
        else:
            self._rounds_without_update += 1

    def pop_post_round_reminder(self, permission_mgr: object | None) -> str | None:
        """检查是否需要提醒更新待办事项。

        当存在未完成项且连续多轮未调用 todo_write 时，返回提醒文本。
        标签包装由 ReminderMgr 统一处理。

        Args:
            permission_mgr: 权限管理器（TodoManager 不使用，遵循统一接口）。

        Returns:
            提醒纯文本，或 None 表示无需注入。
        """
        if self.has_open_items() and self._rounds_without_update >= 3:
            return "更新你的待办事项。"
        return None
