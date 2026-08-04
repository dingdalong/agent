"""工具授权所需的声明式策略。"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Literal


class AccessKind(StrEnum):
    LOCAL_READ = "local_read"
    EXTERNAL_READ = "external_read"
    INTERNAL = "internal"
    WORKSPACE_WRITE = "workspace_write"
    REVIEW = "review"


class DataFlow(StrEnum):
    LOCAL = "local"
    EXTERNAL = "external"
    DYNAMIC = "dynamic"


class PathRole(StrEnum):
    READ = "read"
    WRITE = "write"
    SOURCE = "source"
    DESTINATION = "destination"


@dataclass(frozen=True, slots=True)
class PathArgument:
    name: str
    role: PathRole

    def __post_init__(self) -> None:
        if not self.name or callable(self.name) or not isinstance(self.role, PathRole):
            raise TypeError("PathArgument 必须包含名称和 PathRole")


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    access: AccessKind = AccessKind.REVIEW
    data_flow: DataFlow = DataFlow.DYNAMIC
    path_args: tuple[PathArgument, ...] = ()
    plan_safe: bool = False
    detail_template: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.access, AccessKind) or not isinstance(self.data_flow, DataFlow):
            raise TypeError("ToolPolicy 的 access/data_flow 必须使用对应枚举")
        if not isinstance(self.path_args, tuple) or any(
            not isinstance(item, PathArgument) for item in self.path_args
        ):
            raise TypeError("ToolPolicy.path_args 必须是 PathArgument 元组")
        if not isinstance(self.plan_safe, bool):
            raise TypeError("ToolPolicy.plan_safe 必须是 bool")
        if self.detail_template is not None and not isinstance(self.detail_template, str):
            raise TypeError("ToolPolicy.detail_template 必须是字符串或 None")
        if any(callable(getattr(self, field.name)) for field in fields(self)):
            raise TypeError("ToolPolicy 不允许携带 callable")


@dataclass(frozen=True, slots=True)
class ToolOrigin:
    kind: Literal["builtin", "mcp", "dynamic"]
    name: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"builtin", "mcp", "dynamic"}:
            raise ValueError(f"未知工具来源：{self.kind}")


DEFAULT_POLICY = ToolPolicy()
BUILTIN_ORIGIN = ToolOrigin("builtin")
