"""任务管理器 — 管理会话内任务的 CRUD、依赖关系、文件持久化和提醒注入。"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from src.mgr.secure_io import atomic_write_text


@dataclass
class Task:
    """单个任务的完整数据。

    Attributes:
        id: 顺序数字字符串标识符（"1", "2", ...）。
        subject: 简短的任务标题。
        description: 完整的需求描述。
        active_form: 进行时描述，用于 spinner 显示。None 时使用 subject。
        status: 任务状态。
        owner: 认领该任务的智能体标识符。None 表示未被认领。
        blocks: 本任务完成后才能开始的任务 ID 列表。
        blocked_by: 必须先完成才能开始本任务的任务 ID 列表。
        metadata: 任意键值对，None 表示未设置。
        started_monotonic: 进入 in_progress 的单调时钟时间戳，运行时字段，不持久化。
    """
    id: str
    subject: str
    description: str
    active_form: str | None = None
    status: Literal["pending", "in_progress", "completed"] = "pending"
    owner: str | None = None
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    metadata: dict[str, Any] | None = None
    started_monotonic: float | None = None

    def to_summary(self) -> dict[str, Any]:
        """转为摘要字典，用于 task_list 返回。

        Returns:
            包含 id、subject、status、owner、blocked_by 的字典。
            值为 None 的可选字段不包含。
        """
        d: dict[str, Any] = {
            "id": self.id,
            "subject": self.subject,
            "status": self.status,
            "blocked_by": list(self.blocked_by),
        }
        if self.owner is not None:
            d["owner"] = self.owner
        if self.active_form is not None:
            d["active_form"] = self.active_form
        if self.started_monotonic is not None:
            d["started_monotonic"] = self.started_monotonic
        return d

    def to_detail(self) -> dict[str, Any]:
        """转为完整字典，用于 task_get 返回和文件持久化。

        Returns:
            包含所有字段的字典。值为 None 的可选字段不包含。
        """
        d: dict[str, Any] = {
            "id": self.id,
            "subject": self.subject,
            "description": self.description,
            "status": self.status,
            "blocks": list(self.blocks),
            "blocked_by": list(self.blocked_by),
        }
        if self.active_form is not None:
            d["active_form"] = self.active_form
        if self.owner is not None:
            d["owner"] = self.owner
        if self.metadata is not None:
            d["metadata"] = self.metadata
        return d


class TaskManager:
    """任务管理器 — 管理会话内任务的 CRUD、依赖关系和文件持久化。

    通过 duck typing 向 ReminderMgr 提供提醒注入接口。
    当 tasks_dir 不为 None 时，每次变更自动写入磁盘（每个 task 一个 JSON 文件）。
    tasks_dir 为 None 时退化为纯内存模式（子 agent 独立实例，不持久化）。

    Attributes:
        _tasks: 任务存储，键为任务 ID。
        _next_id: 下一个任务的顺序 ID。
        _rounds_without_update: 连续未使用任务工具的轮次计数。
        _tasks_dir: 任务文件存储目录，None 表示纯内存模式。
    """

    MAX_TASKS = 50
    _TOOL_NAMES = frozenset({"task_create", "task_update", "task_list", "task_get"})
    _HIGHWATERMARK_FILE = ".highwatermark"

    def __init__(
        self,
        tasks_dir: Path | None = None,
        data_guard: Any = None,
        on_change: Callable[[list[dict]], None] | None = None,
    ) -> None:
        """初始化任务管理器。

        tasks_dir 不为 None 时自动加载已有的任务文件。

        Args:
            tasks_dir: 任务文件存储目录。None 表示纯内存模式。
            data_guard: 数据脱敏守卫。
            on_change: 任务变更回调，接收全量任务摘要列表。
        """
        self._tasks: dict[str, Task] = {}
        self._next_id: int = 1
        self._rounds_without_update: int = 0
        self._tasks_dir: Path | None = tasks_dir
        self._data_guard = data_guard
        self._on_change = on_change
        if self._tasks_dir is not None:
            self._load()
        # 恢复已有任务时立即通知 UI
        if self._tasks:
            self._emit_change()

    # ── 文件持久化 ──────────────────────────────────────────────

    def _load(self) -> None:
        """从 tasks_dir 加载已有的任务文件和 highwatermark。

        读取目录下所有 *.json 文件解析为 Task，
        读取 .highwatermark 恢复 _next_id 以避免 ID 重用。
        目录不存在时跳过。
        """
        if self._tasks_dir is None or not self._tasks_dir.exists():
            return
        # 读取 highwatermark
        hwm_path = self._tasks_dir / self._HIGHWATERMARK_FILE
        if hwm_path.exists():
            try:
                self._next_id = int(hwm_path.read_text().strip()) + 1
            except (ValueError, OSError):
                pass
        # 读取所有 task 文件
        max_id = 0
        for f in self._tasks_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if self._data_guard is not None:
                    data = self._data_guard.redact(data)
                task = Task(
                    id=data["id"],
                    subject=data["subject"],
                    description=data["description"],
                    active_form=data.get("active_form"),
                    status=data.get("status", "pending"),
                    owner=data.get("owner"),
                    blocks=data.get("blocks", []),
                    blocked_by=data.get("blocked_by", []),
                    metadata=data.get("metadata"),
                )
                self._tasks[task.id] = task
                try:
                    max_id = max(max_id, int(task.id))
                except ValueError:
                    pass
            except (json.JSONDecodeError, KeyError, OSError):
                continue
        # 确保 _next_id 大于所有已有 ID
        if max_id >= self._next_id:
            self._next_id = max_id + 1

    def _flush_task(self, task_id: str) -> None:
        """将单个 task 原子写入磁盘。

        使用 owner-only 原子写入，防止崩溃时文件损坏。

        Args:
            task_id: 要写入的任务 ID。
        """
        if self._tasks_dir is None or task_id not in self._tasks:
            return
        self._tasks_dir.mkdir(parents=True, exist_ok=True)
        task_path = self._tasks_dir / f"{task_id}.json"
        detail = self._tasks[task_id].to_detail()
        if self._data_guard is not None:
            detail = self._data_guard.redact(detail)
        data = json.dumps(detail, ensure_ascii=False, indent=2)
        atomic_write_text(task_path, data)

    def _delete_task_file(self, task_id: str) -> None:
        """删除磁盘上的单个 task 文件。

        Args:
            task_id: 要删除的任务 ID。
        """
        if self._tasks_dir is None:
            return
        task_path = self._tasks_dir / f"{task_id}.json"
        task_path.unlink(missing_ok=True)

    def _flush_highwatermark(self) -> None:
        """将当前最高分配 ID 写入 .highwatermark 文件，防止删除后 ID 重用。"""
        if self._tasks_dir is None:
            return
        self._tasks_dir.mkdir(parents=True, exist_ok=True)
        hwm_path = self._tasks_dir / self._HIGHWATERMARK_FILE
        atomic_write_text(hwm_path, str(self._next_id - 1))

    def _auto_cleanup(self) -> None:
        """当所有任务都已完成时，删除整个 tasks_dir 目录。"""
        if self._tasks_dir is None:
            return
        if not self._tasks:
            return
        if all(t.status == "completed" for t in self._tasks.values()):
            # 写 highwatermark 后删除目录
            self._flush_highwatermark()
            shutil.rmtree(self._tasks_dir, ignore_errors=True)

    def _emit_change(self) -> None:
        """通知监听器当前全量任务快照（过滤 _internal，与 list_tasks 一致）。"""
        if self._on_change is None:
            return
        tasks = []
        for task in self._tasks.values():
            if task.metadata and task.metadata.get("_internal"):
                continue
            summary = task.to_summary()
            summary["blocked_by"] = [
                bid for bid in task.blocked_by
                if bid in self._tasks and self._tasks[bid].status != "completed"
            ]
            tasks.append(summary)
        self._on_change(tasks)

    @staticmethod
    def clear_dir(tasks_dir: Path) -> None:
        """删除指定的 tasks 目录（/clear 时调用）。

        Args:
            tasks_dir: 要删除的任务目录路径。
        """
        if tasks_dir.exists():
            shutil.rmtree(tasks_dir, ignore_errors=True)

    # ── CRUD ──────────────────────────────────────────────────

    def create(
        self,
        subject: str,
        description: str,
        active_form: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """创建新任务，状态为 pending，并持久化到磁盘。

        Args:
            subject: 任务标题。
            description: 任务需求描述。
            active_form: 进行时描述（可选）。
            metadata: 任意键值对（可选）。

        Returns:
            {"task": {"id": ..., "subject": ...}}。

        Raises:
            ValueError: 超过任务上限。
        """
        if len(self._tasks) >= self.MAX_TASKS:
            raise ValueError(f"任务数量已达上限 {self.MAX_TASKS}")

        task_id = str(self._next_id)
        self._next_id += 1

        if self._data_guard is not None:
            subject = str(self._data_guard.redact(subject))
            description = str(self._data_guard.redact(description))
            active_form = (
                str(self._data_guard.redact(active_form)) if active_form is not None else None
            )
            metadata = self._data_guard.redact(metadata) if metadata is not None else None
        task = Task(
            id=task_id,
            subject=subject,
            description=description,
            active_form=active_form,
            metadata=metadata,
        )
        self._tasks[task_id] = task
        self._flush_task(task_id)
        self._flush_highwatermark()
        self._emit_change()
        return {"task": {"id": task_id, "subject": subject}}

    def update(
        self,
        task_id: str,
        *,
        subject: str | None = None,
        description: str | None = None,
        active_form: str | None = None,
        status: str | None = None,
        owner: str | None = None,
        add_blocks: list[str] | None = None,
        add_blocked_by: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """更新任务字段并持久化。

        status="deleted" 触发删除并级联清理依赖关系。
        owner 设置时检查任务是否已被其他智能体认领或被阻塞。
        metadata 合并更新，值为 None 的键删除。
        add_blocks/add_blocked_by 追加并双向同步。

        Args:
            task_id: 目标任务 ID。
            subject: 新标题（可选）。
            description: 新描述（可选）。
            active_form: 新进行时描述（可选）。
            status: 新状态，"deleted" 触发删除（可选）。
            owner: 认领任务的智能体标识符（可选）。
            add_blocks: 追加到 blocks 的任务 ID 列表（可选）。
            add_blocked_by: 追加到 blocked_by 的任务 ID 列表（可选）。
            metadata: 合并更新的键值对，值为 None 删除键（可选）。

        Returns:
            {"success": True, "task_id": ..., "updated_fields": [...]}。

        Raises:
            ValueError: 任务不存在、状态无效、或认领冲突。
        """
        if task_id not in self._tasks:
            raise ValueError(f"任务 {task_id} 不存在")

        if status == "deleted":
            self._delete_task(task_id)
            self._emit_change()
            return {"success": True, "task_id": task_id, "updated_fields": ["deleted"]}

        task = self._tasks[task_id]
        updated: list[str] = []

        if subject is not None:
            if self._data_guard is not None:
                subject = str(self._data_guard.redact(subject))
            task.subject = subject
            updated.append("subject")

        if description is not None:
            if self._data_guard is not None:
                description = str(self._data_guard.redact(description))
            task.description = description
            updated.append("description")

        if active_form is not None:
            if self._data_guard is not None:
                active_form = str(self._data_guard.redact(active_form))
            task.active_form = active_form
            updated.append("active_form")

        if status is not None:
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"无效状态: '{status}'")
            task.status = status
            if status == "in_progress" and task.started_monotonic is None:
                task.started_monotonic = time.monotonic()
            elif status != "in_progress":
                task.started_monotonic = None
            updated.append("status")

        if owner is not None:
            if self._data_guard is not None:
                owner = str(self._data_guard.redact(owner))
            self._claim_task(task, owner)
            updated.append("owner")

        if add_blocks:
            self._add_dependencies(task_id, add_blocks, direction="blocks")
            updated.append("blocks")

        if add_blocked_by:
            self._add_dependencies(task_id, add_blocked_by, direction="blocked_by")
            updated.append("blocked_by")

        if metadata is not None:
            if self._data_guard is not None:
                metadata = self._data_guard.redact(metadata)
            self._merge_metadata(task, metadata)
            updated.append("metadata")

        # 持久化变更的 task 及被依赖变更影响的 task
        self._flush_task(task_id)
        if add_blocks:
            for tid in add_blocks:
                self._flush_task(tid)
        if add_blocked_by:
            for tid in add_blocked_by:
                self._flush_task(tid)

        self._auto_cleanup()
        self._emit_change()

        return {"success": True, "task_id": task_id, "updated_fields": updated}

    def list_tasks(self) -> dict[str, Any]:
        """列出所有任务的摘要信息。

        blocked_by 仅包含未完成（non-completed）的任务 ID。
        过滤掉 metadata._internal 为 True 的任务。

        Returns:
            {"tasks": [{"id", "subject", "status", "blocked_by"}, ...]}。
        """
        result = []
        for task in self._tasks.values():
            if task.metadata and task.metadata.get("_internal"):
                continue
            summary = task.to_summary()
            # blocked_by 仅保留未完成且仍存在的任务 ID
            summary["blocked_by"] = [
                bid for bid in task.blocked_by
                if bid in self._tasks and self._tasks[bid].status != "completed"
            ]
            result.append(summary)
        return {"tasks": result}

    def get_task(self, task_id: str) -> dict[str, Any]:
        """获取单个任务的完整详情。

        Args:
            task_id: 目标任务 ID。

        Returns:
            {"task": {完整任务字段}}。

        Raises:
            ValueError: 任务不存在。
        """
        if task_id not in self._tasks:
            raise ValueError(f"任务 {task_id} 不存在")
        return {"task": self._tasks[task_id].to_detail()}

    # ── 内部方法 ──────────────────────────────────────────────

    def _delete_task(self, task_id: str) -> None:
        """删除任务并级联清理双向依赖，同时删除磁盘文件。

        从所有其他任务的 blocks/blocked_by 中移除 task_id。

        Args:
            task_id: 要删除的任务 ID。
        """
        del self._tasks[task_id]
        self._delete_task_file(task_id)
        affected: list[str] = []
        for task in self._tasks.values():
            changed = False
            if task_id in task.blocks:
                task.blocks.remove(task_id)
                changed = True
            if task_id in task.blocked_by:
                task.blocked_by.remove(task_id)
                changed = True
            if changed:
                affected.append(task.id)
        for tid in affected:
            self._flush_task(tid)
        self._auto_cleanup()

    def _claim_task(self, task: Task, claimant: str) -> None:
        """认领任务，设置 owner。

        已被其他智能体认领或已完成的任务不可认领。
        被未完成任务阻塞的任务不可认领。

        Args:
            task: 目标任务。
            claimant: 认领者智能体标识符。

        Raises:
            ValueError: 任务已被其他智能体认领、已完成或被阻塞。
        """
        if task.owner and task.owner != claimant:
            raise ValueError(
                f"任务 {task.id} 已被 {task.owner} 认领"
            )
        if task.status == "completed":
            raise ValueError(f"任务 {task.id} 已完成，无法认领")
        # 检查是否被未完成的任务阻塞
        unresolved = [
            bid for bid in task.blocked_by
            if bid in self._tasks and self._tasks[bid].status != "completed"
        ]
        if unresolved:
            raise ValueError(
                f"任务 {task.id} 被未完成的任务 {', '.join(unresolved)} 阻塞"
            )
        task.owner = claimant

    def _add_dependencies(
        self,
        task_id: str,
        target_ids: list[str],
        direction: Literal["blocks", "blocked_by"],
    ) -> None:
        """双向追加依赖关系，检测循环。

        direction="blocks"  → task.blocks 加 target, target.blocked_by 加 task
        direction="blocked_by" → task.blocked_by 加 target, target.blocks 加 task

        Args:
            task_id: 发起方任务 ID。
            target_ids: 目标任务 ID 列表。
            direction: 依赖方向。

        Raises:
            ValueError: 目标任务不存在、自引用或形成循环依赖。
        """
        task = self._tasks[task_id]
        for tid in target_ids:
            if tid == task_id:
                raise ValueError(f"任务不能依赖自身: {tid}")
            if tid not in self._tasks:
                raise ValueError(f"目标任务 {tid} 不存在")

            # 检测循环：如果 direction="blocks"，则 task 阻塞 target，
            # 需确认 target 不会通过 blocked_by 链回到 task。
            # 如果 direction="blocked_by"，则 target 阻塞 task，
            # 需确认 task 不会通过 blocked_by 链回到 target。
            blocker = task_id if direction == "blocks" else tid
            blocked = tid if direction == "blocks" else task_id
            if self._would_create_cycle(blocker, blocked):
                raise ValueError(
                    f"添加依赖 {blocker} → {blocked} 会形成循环"
                )

            target = self._tasks[tid]
            if direction == "blocks":
                if tid not in task.blocks:
                    task.blocks.append(tid)
                if task_id not in target.blocked_by:
                    target.blocked_by.append(task_id)
            else:
                if tid not in task.blocked_by:
                    task.blocked_by.append(tid)
                if task_id not in target.blocks:
                    target.blocks.append(task_id)

    def _would_create_cycle(self, blocker_id: str, blocked_id: str) -> bool:
        """检测添加 blocker → blocked 依赖后是否形成循环。

        通过 BFS 从 blocker 沿 blocked_by 链向上搜索，
        如果能到达 blocked 则说明会形成循环。

        Args:
            blocker_id: 阻塞方任务 ID。
            blocked_id: 被阻塞方任务 ID。

        Returns:
            True 表示会形成循环。
        """
        visited: set[str] = set()
        queue = [blocker_id]
        while queue:
            current = queue.pop(0)
            if current == blocked_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            if current in self._tasks:
                queue.extend(self._tasks[current].blocked_by)
        return False

    def _merge_metadata(self, task: Task, metadata: dict[str, Any]) -> None:
        """合并更新任务的 metadata，值为 None 的键删除。

        Args:
            task: 目标任务。
            metadata: 要合并的键值对。
        """
        if task.metadata is None:
            task.metadata = {}
        for key, value in metadata.items():
            if value is None:
                task.metadata.pop(key, None)
            else:
                task.metadata[key] = value
        if not task.metadata:
            task.metadata = None

    def describe(self, is_subagent: bool) -> str:
        """返回任务管理系统的 prompt 指导文本，供 PromptMgr 注入系统提示词。

        主/子 agent 各返回独立的完整提示词。

        Args:
            is_subagent: 是否为子智能体。

        Returns:
            Markdown 格式的任务管理指导文本。
        """
        if is_subagent:
            return (
                "# 任务管理\n\n"
                "## 何时使用\n"
                "- 复杂多步任务（3 步以上）需要追踪进度时\n"
                "- 一次性收到多个独立子任务时\n\n"
                "## 何时不使用\n"
                "- 单个简单任务，直接执行即可\n"
                "- 3 步以内的简单操作\n\n"
                "## 字段规范\n"
                '- subject：祈使句形式的简短标题（如"修复登录认证 bug"）\n'
                "- description：完整的需求描述\n"
                '- active_form：进行时描述，用于 spinner 显示（如"修复认证 bug 中"），省略时使用 subject\n\n'
                "## 状态工作流\n"
                "状态流转：pending → in_progress → completed\n\n"
                "- 执行任务前，先用 task_update 标记为 in_progress\n"
                "- 只有完全完成时才标记 completed\n"
                "- 遇到错误或阻塞时，保持 in_progress，尝试自行解决\n"
                "- 任务不再需要（需求取消、被其他任务合并覆盖）或创建有误（重复、描述错误）时，status 设为 deleted 永久删除\n"
                "- 已完成的任务保留 completed 状态，不要删除；全部 completed 后框架自动清理"
            )
        return (
            "# 任务管理\n\n"
            "## 何时使用\n"
            "- 复杂多步任务（3 步以上）需要追踪进度时\n"
            "- 用户一次性提出多个独立任务时\n"
            "- 需要将工作委派给子智能体并追踪完成情况时\n\n"
            "## 何时不使用\n"
            "- 单个简单任务、不需要委派子智能体时，直接执行即可（无需创建任务）\n"
            "- 3 步以内的简单操作、不需要委派子智能体时\n"
            "- 纯对话或信息查询\n\n"
            "## 字段规范\n"
            '- subject：祈使句形式的简短标题（如"修复登录认证 bug"）\n'
            "- description：完整的需求描述\n"
            '- active_form：进行时描述，用于 spinner 显示（如"修复认证 bug 中"），省略时使用 subject\n\n'
            "## 状态工作流\n"
            "状态流转：pending → in_progress → completed\n\n"
            "- 亲自执行任务（不委派）前，先用 task_update 标记为 in_progress\n"
            "- 只有完全完成时才标记 completed\n"
            "- 遇到错误或阻塞时，保持 in_progress，尝试自行解决；无法解决时向用户说明阻塞原因等待指示\n"
            "- 任务不再需要（需求取消、被其他任务合并覆盖）或创建有误（重复、描述错误）时，status 设为 deleted 永久删除\n"
            "- 已完成的任务保留 completed 状态，不要删除；全部 completed 后框架自动清理\n\n"
            "## 创建与依赖管理\n"
            "先批量创建所有任务（获得 ID），再通过 task_update 设置依赖关系：\n"
            "1. 在同一轮中调用多个 task_create 一次性创建所有子任务\n"
            "2. 在同一轮中调用多个 task_update，用 add_blocked_by 设置依赖关系\n"
            "3. 按依赖顺序逐个处理：优先通过 task_delegator 委派给合适的子智能体，没有合适子智能体时才亲自执行\n\n"
            "依赖说明：\n"
            "- add_blocked_by：指定必须先完成的前置任务 ID\n"
            "- add_blocks：指定本任务完成后才能开始的任务 ID\n\n"
            "## 委派工作流\n"
            "通过 task_delegator 委派子智能体时，传入 task_id 关联任务：\n"
            "- 框架自动将任务标记为 in_progress 并设置 owner，无需手动更新\n"
            "- 子智能体对任务系统完全透明，prompt 中只写任务内容，不要提及 task ID 或状态管理\n"
            "- 子智能体返回后，评估结果并执行对应操作：\n"
            "  - 结果合格：task_update 标记 completed\n"
            "  - 结果不合格：保持 in_progress，用 task_delegator 重新委派（可调整 prompt 或换 agent_type），同样传入 task_id\n"
            "  - 需要补充工作：保持 in_progress，自己直接完成剩余部分，再标记 completed\n"
            "- 子智能体异常退出时，框架自动将任务回滚为 pending"
        )

    def has_open_items(self) -> bool:
        """检查是否存在未完成的任务。

        Returns:
            True 表示存在未完成项。
        """
        return any(t.status != "completed" for t in self._tasks.values())

    # ── 提醒注入接口（由 ReminderMgr 调用）──────────────────────────

    def get_turn_start_reminder(self, mode: object | None) -> str:
        """在 turn 开始时注入当前任务状态摘要。

        当存在未完成任务且连续多轮未使用任务工具时，
        返回格式化的任务列表帮助模型恢复对任务的感知（特别是 compact 后）。

        Args:
            mode: 调用方 agent 的权限模式（TaskManager 不使用，遵循统一接口）。

        Returns:
            任务状态摘要文本，无未完成任务或近期使用过任务工具时返回空串。
        """
        if not self.has_open_items() or self._rounds_without_update < 3:
            return ""
        lines: list[str] = []
        for task in self._tasks.values():
            if task.metadata and task.metadata.get("_internal"):
                continue
            lines.append(f"#{task.id}. [{task.status}] {task.subject}")
        if not lines:
            return ""
        return "当前任务列表：\n" + "\n".join(lines)

    def notify_tool_round(self, tool_names: list[str]) -> None:
        """工具执行轮结束时更新轮次计数。

        Args:
            tool_names: 本轮调用的工具名列表。包含任意 task_* 工具时重置计数器。
        """
        if self._TOOL_NAMES & set(tool_names):
            self._rounds_without_update = 0
        else:
            self._rounds_without_update += 1

    def pop_post_round_reminder(self, mode: object | None) -> str | None:
        """检查是否需要提醒更新任务列表。

        当存在未完成项且连续多轮未调用任务工具时，返回提醒文本。
        标签包装由 ReminderMgr 统一处理。

        Args:
            mode: 调用方 agent 的权限模式（TaskManager 不使用，遵循统一接口）。

        Returns:
            提醒纯文本，或 None 表示无需注入。
        """
        if self.has_open_items() and self._rounds_without_update >= 3:
            return "更新你的任务列表。"
        return None
