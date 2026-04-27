class TodoManager:
    def __init__(self):
        self.items = []

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
        return any(item.get("status") != "completed" for item in self.items)
