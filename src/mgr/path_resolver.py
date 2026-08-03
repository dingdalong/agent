"""授权和文件 I/O 共用的路径规范化与分类。"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from src.tools.policy import PathRole, ToolPolicy


MAX_READ_FILE_BYTES = 8 * 1024 * 1024


class PathClass(StrEnum):
    WORKSPACE = "workspace"
    PLAN = "plan"
    PROTECTED = "protected"
    OUTSIDE = "outside"
    UNSAFE = "unsafe"


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    argument: str
    role: PathRole
    original: str | None
    path: Path
    classification: PathClass
    exists: bool


@dataclass(frozen=True, slots=True)
class PathGrant:
    argument: str
    role: PathRole
    path: Path
    classification: PathClass


class PathResolutionError(ValueError):
    pass


class PathResolver:
    def __init__(self, workdir: str | Path) -> None:
        self.workdir = Path(workdir).expanduser().resolve(strict=True)

    def resolve(self, value: str | os.PathLike[str] | None) -> Path:
        raw = self.workdir if value is None else Path(value).expanduser()
        if not raw.is_absolute():
            raw = self.workdir / raw
        try:
            return raw.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise PathResolutionError(f"无法解析路径：{value!r}") from exc

    def classify(self, path: Path) -> PathClass:
        if self._is_unsafe_pseudo_path(path) or self._is_special_existing(path):
            return PathClass.UNSAFE
        if not self._is_within(path, self.workdir):
            return PathClass.OUTSIDE
        relative = path.relative_to(self.workdir)
        parts = relative.parts
        if len(parts) >= 2 and parts[0] == ".agent" and parts[1] == "plans":
            plan_relative = Path(*parts[2:])
            if self._is_protected_relative(plan_relative):
                return PathClass.PROTECTED
            return PathClass.PLAN
        if self._is_protected_relative(relative):
            return PathClass.PROTECTED
        return PathClass.WORKSPACE

    def extract(self, policy: ToolPolicy, arguments: Mapping[str, Any]) -> tuple[ResolvedPath, ...]:
        resolved: list[ResolvedPath] = []
        for spec in policy.path_args:
            value = arguments.get(spec.name)
            if value is not None and not isinstance(value, (str, os.PathLike)):
                raise PathResolutionError(f"路径参数 {spec.name} 必须是字符串或 None")
            path = self.resolve(value)
            resolved.append(ResolvedPath(
                argument=spec.name,
                role=spec.role,
                original=str(value) if value is not None else None,
                path=path,
                classification=self.classify(path),
                exists=path.exists(),
            ))
        return tuple(resolved)

    def resolve_move_target(self, source: str, destination: str) -> Path:
        source_path = self.resolve(source)
        destination_path = self.resolve(destination)
        if destination_path.is_dir():
            destination_path = self.resolve(destination_path / source_path.name)
        return destination_path

    def grant(self, resolved: ResolvedPath) -> PathGrant:
        return PathGrant(
            argument=resolved.argument,
            role=resolved.role,
            path=resolved.path,
            classification=resolved.classification,
        )

    def revalidate(self, grant: PathGrant, value: str | os.PathLike[str] | Path) -> Path:
        """在实际 I/O 前确认授权目标的规范路径和分类均未变化。"""
        path = self.resolve(value)
        classification = self.classify(path)
        if path != grant.path or classification is not grant.classification:
            raise PathResolutionError(f"授权后的路径或分类已变化：{grant.argument}")
        if classification is PathClass.UNSAFE:
            raise PathResolutionError(f"不允许访问特殊或伪文件：{path}")
        return path

    def validate_local_read(self, path: Path) -> None:
        classification = self.classify(path)
        if classification is PathClass.UNSAFE:
            raise PathResolutionError(f"不允许读取特殊或伪文件：{path}")
        try:
            info = path.stat()
        except OSError as exc:
            raise PathResolutionError(f"无法读取路径信息：{path}") from exc
        if stat.S_ISREG(info.st_mode):
            if info.st_size > MAX_READ_FILE_BYTES:
                raise PathResolutionError(f"单文件超过 8 MiB：{path}")
            return
        if stat.S_ISDIR(info.st_mode):
            return
        raise PathResolutionError(f"只允许读取普通文件或目录：{path}")

    @staticmethod
    def _is_within(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    @staticmethod
    def _is_unsafe_pseudo_path(path: Path) -> bool:
        parts = path.parts
        return len(parts) > 1 and parts[1] in {"dev", "proc", "sys"}

    @staticmethod
    def _is_special_existing(path: Path) -> bool:
        try:
            mode = path.stat().st_mode
        except OSError:
            return False
        return not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))

    @staticmethod
    def _is_protected_relative(relative: Path) -> bool:
        parts = relative.parts
        if not parts:
            return False
        name = relative.name.lower()
        protected_names = {".git", ".agent", ".vscode", ".idea"}
        if protected_names.intersection(parts):
            return True
        if name.startswith(".env"):
            return True
        if name in {
            "credentials", "credentials.json", "service-account.json", "kubeconfig",
            "known_hosts", "authorized_keys", "id_rsa", "id_dsa", "id_ecdsa",
            "id_ed25519", ".dockercfg",
        }:
            return True
        if name.endswith((".pem", ".key", ".p12", ".pfx", ".jks")):
            return True
        protected_parts = {
            ".ssh", ".aws", ".azure", ".config/gcloud", ".kube", ".docker", "gcloud",
        }
        return bool(protected_parts.intersection(parts))
