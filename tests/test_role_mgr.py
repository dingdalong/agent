"""RoleMgr 的激活角色与主角色配置覆盖测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.mgr.role_mgr import RoleMgr


class ConfigStub:
    """提供 RoleMgr 所需的最小嵌套配置读取接口。"""

    project_trusted = False

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def get_config(self, key: str) -> Any:
        value: Any = self.config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                raise KeyError(key)
            value = value[part]
        return value


def _add_role(
    global_dir: Path,
    name: str = "reviewer",
    *,
    model: str = "manifest-model",
    reasoning_effort: str = "high",
) -> None:
    role_dir = global_dir / "roles" / name
    role_dir.mkdir(parents=True)
    (role_dir / "role.md").write_text(
        "---\n"
        f"model: {model}\n"
        f"reasoning_effort: {reasoning_effort}\n"
        "---\n"
        "测试角色。\n"
    )


def _role_mgr(tmp_path: Path, config: dict[str, Any]) -> tuple[RoleMgr, ConfigStub]:
    global_dir = tmp_path / "global"
    _add_role(global_dir)
    config_mgr = ConfigStub(config)
    return (
        RoleMgr(config_mgr=config_mgr, workdir=tmp_path / "work", global_dir=global_dir),
        config_mgr,
    )


def test_role_default_selects_active_role(tmp_path: Path) -> None:
    """role.default 应选择配置中的角色。"""
    role_mgr, _ = _role_mgr(tmp_path, {"role": {"default": "reviewer"}})

    assert role_mgr.role_name == "reviewer"
    assert role_mgr.manifest is not None
    assert role_mgr.manifest.model == "manifest-model"


def test_scalar_role_config_is_ignored(tmp_path: Path) -> None:
    """旧标量 role 配置不应被当作激活角色。"""
    role_mgr, _ = _role_mgr(tmp_path, {"role": "reviewer"})

    assert role_mgr.role_name == "coding"


def test_active_role_config_overrides_manifest_model_and_reasoning_effort(
    tmp_path: Path,
) -> None:
    """活动角色的配置应覆盖 role.md 的同名字段。"""
    role_mgr, _ = _role_mgr(
        tmp_path,
        {
            "role": {
                "default": "reviewer",
                "reviewer": {
                    "model": "configured-model",
                    "reasoning_effort": " XHIGH ",
                },
            },
        },
    )

    assert role_mgr.manifest is not None
    assert role_mgr.manifest.model == "configured-model"
    assert role_mgr.manifest.reasoning_effort == "xhigh"


@pytest.mark.parametrize(
    ("overrides", "expected_model", "expected_effort"),
    [
        ({"model": "configured-model"}, "configured-model", "high"),
        ({"reasoning_effort": "low"}, "manifest-model", "low"),
    ],
)
def test_role_config_overrides_fall_back_independently(
    tmp_path: Path,
    overrides: dict[str, str],
    expected_model: str,
    expected_effort: str,
) -> None:
    """任一配置覆盖缺失时，应保留 role.md 中另一个字段的值。"""
    role_mgr, _ = _role_mgr(
        tmp_path,
        {"role": {"default": "reviewer", "reviewer": overrides}},
    )

    assert role_mgr.manifest is not None
    assert role_mgr.manifest.model == expected_model
    assert role_mgr.manifest.reasoning_effort == expected_effort


def test_invalid_role_config_effort_keeps_manifest_value(tmp_path: Path) -> None:
    """非法推理力度配置不得覆盖 role.md 默认值。"""
    role_mgr, _ = _role_mgr(
        tmp_path,
        {
            "role": {
                "default": "reviewer",
                "reviewer": {"reasoning_effort": "invalid"},
            },
        },
    )

    assert role_mgr.manifest is not None
    assert role_mgr.manifest.reasoning_effort == "high"


def test_missing_role_falls_back_to_coding_and_uses_coding_overrides(tmp_path: Path) -> None:
    """无效角色回退 coding 后，应应用 role.coding 的覆盖。"""
    role_mgr, _ = _role_mgr(
        tmp_path,
        {
            "role": {
                "default": "missing-role",
                "coding": {"model": "fast", "reasoning_effort": "low"},
            },
        },
    )

    assert role_mgr.role_name == "coding"
    assert role_mgr.manifest is not None
    assert role_mgr.manifest.model == "fast"
    assert role_mgr.manifest.reasoning_effort == "low"


def test_reload_rereads_nested_role_config(tmp_path: Path) -> None:
    """reload 应重新读取活动角色的嵌套覆盖配置。"""
    role_mgr, config_mgr = _role_mgr(
        tmp_path,
        {
            "role": {
                "default": "reviewer",
                "reviewer": {"model": "fast", "reasoning_effort": "low"},
            },
        },
    )
    assert role_mgr.manifest is not None
    assert role_mgr.manifest.model == "fast"
    assert role_mgr.manifest.reasoning_effort == "low"

    config_mgr.config = {
        "role": {
            "default": "reviewer",
            "reviewer": {"model": "best", "reasoning_effort": "max"},
        },
    }
    role_mgr.reload()

    assert role_mgr.manifest is not None
    assert role_mgr.manifest.model == "best"
    assert role_mgr.manifest.reasoning_effort == "max"
