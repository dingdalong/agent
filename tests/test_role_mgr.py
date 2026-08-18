"""RoleMgr 的激活角色与主角色配置覆盖测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import src.mgr.role_mgr as role_mgr_module
from src.llm.errors import LLMConfigurationError
from src.mgr.role_mgr import (
    DEFAULT_ROLE,
    RoleMgr,
    active_role_name,
)


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

    def get_config_parts(self, parts: tuple[str, ...]) -> Any:
        value: Any = self.config
        for part in parts:
            if not isinstance(value, dict) or part not in value:
                raise KeyError(parts)
            value = value[part]
        return value


def _add_role(
    global_dir: Path,
    name: str = "reviewer",
    *,
    model: str | None = None,
    reasoning_effort: str = "high",
) -> None:
    """在临时全局目录下写入一个角色定义。

    Args:
        global_dir: 临时全局配置目录（~/.agent/ 的替身）。
        name: 角色目录名。
        model: 写入 frontmatter 的 model 字段值；None 表示不写该字段。
        reasoning_effort: 写入 frontmatter 的推理力度。

    Returns:
        None。
    """
    role_dir = global_dir / "roles" / name
    role_dir.mkdir(parents=True)
    model_line = f"model: {model}\n" if model is not None else ""
    (role_dir / "role.md").write_text(
        "---\n"
        f"{model_line}"
        f"reasoning_effort: {reasoning_effort}\n"
        "---\n"
        "测试角色。\n"
    )


def _role_mgr(
    tmp_path: Path,
    config: dict[str, Any],
    *,
    model: str | None = None,
) -> tuple[RoleMgr, ConfigStub]:
    """构造带临时 reviewer 角色的 RoleMgr。

    Args:
        tmp_path: pytest 临时目录。
        config: ConfigStub 的配置字典。
        model: reviewer/role.md 的 model 字段值；None 表示不写该字段。

    Returns:
        (RoleMgr, ConfigStub) 二元组。
    """
    global_dir = tmp_path / "global"
    _add_role(global_dir, model=model)
    config_mgr = ConfigStub(config)
    return (
        RoleMgr(config_mgr=config_mgr, workdir=tmp_path / "work", global_dir=global_dir),
        config_mgr,
    )


# —— 角色发现 ————————————————————————————————————————————————————————


def test_discover_roles_preserves_layer_precedence_and_excludes_common(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发现顺序应为内置→全局→可信项目，同名后层覆盖且 common 不可激活。"""
    builtin_root = tmp_path / "builtin"
    global_dir = tmp_path / "global"
    workdir = tmp_path / "work"
    builtin_coding = builtin_root / "roles" / "coding"
    builtin_common = builtin_root / "roles" / "common"
    global_coding = global_dir / "roles" / "coding"
    global_research = global_dir / "roles" / "research"
    project_coding = workdir / ".agent" / "roles" / "coding"
    project_review = workdir / ".agent" / "roles" / "review"
    project_common = workdir / ".agent" / "roles" / "common"
    for role_dir in (
        builtin_coding,
        builtin_common,
        global_coding,
        global_research,
        project_coding,
        project_review,
        project_common,
    ):
        role_dir.mkdir(parents=True)
        (role_dir / "role.md").write_text("---\n---\nrole\n")
    monkeypatch.setattr(role_mgr_module, "builtin_root", lambda: builtin_root)

    roles = role_mgr_module.discover_roles(workdir, global_dir, True)

    assert roles == {
        "coding": project_coding,
        "research": global_research,
        "review": project_review,
    }
    assert "common" not in roles


def test_discover_roles_excludes_untrusted_project_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """项目未信任时发现结果不得包含项目角色或项目同名覆盖。"""
    builtin_root = tmp_path / "builtin"
    global_dir = tmp_path / "global"
    workdir = tmp_path / "work"
    builtin_coding = builtin_root / "roles" / "coding"
    global_research = global_dir / "roles" / "research"
    project_research = workdir / ".agent" / "roles" / "research"
    project_review = workdir / ".agent" / "roles" / "review"
    for role_dir in (
        builtin_coding,
        global_research,
        project_research,
        project_review,
    ):
        role_dir.mkdir(parents=True)
        (role_dir / "role.md").write_text("---\n---\nrole\n")
    monkeypatch.setattr(role_mgr_module, "builtin_root", lambda: builtin_root)

    roles = role_mgr_module.discover_roles(workdir, global_dir, False)

    assert roles == {
        "coding": builtin_coding,
        "research": global_research,
    }


def test_discover_roles_accepts_arbitrary_mapping_key_names_with_layer_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unicode、含点和长角色名应被发现并保持三层后者覆盖语义。"""
    builtin_root = tmp_path / "builtin"
    global_dir = tmp_path / "global"
    workdir = tmp_path / "work"
    role_names = (
        "foo-bar",
        "foo_bar",
        "foo.bar",
        "研发角色",
        "a" * 64,
    )
    role_roots = (
        builtin_root / "roles",
        global_dir / "roles",
        workdir / ".agent" / "roles",
    )
    for role_root in role_roots:
        for name in role_names:
            role_dir = role_root / name
            role_dir.mkdir(parents=True)
            (role_dir / "role.md").write_text("---\n---\nrole\n")
    monkeypatch.setattr(role_mgr_module, "builtin_root", lambda: builtin_root)

    roles = role_mgr_module.discover_roles(workdir, global_dir, True)

    assert roles == {
        name: workdir / ".agent" / "roles" / name for name in role_names
    }


@pytest.mark.parametrize(
    "name",
    ["coding", "foo.bar", "foo:bar", "-x", "_x", "角色", "a" * 65],
)
def test_valid_role_name_accepts_non_reserved_names(name: str) -> None:
    """角色名只要不与保留结构冲突就应通过校验。"""
    assert role_mgr_module._valid_role_name(name) is True


@pytest.mark.parametrize(
    "name", ["", "default", "common"],
)
def test_valid_role_name_rejects_empty_and_reserved_names(name: str) -> None:
    """角色名校验只拒绝空值和结构保留名。"""
    assert role_mgr_module._valid_role_name(name) is False


def test_discover_roles_skips_reserved_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发现入口只跳过与配置结构冲突的保留角色目录。"""
    builtin_root = tmp_path / "builtin"
    global_dir = tmp_path / "global"
    workdir = tmp_path / "work"
    builtin_coding = builtin_root / "roles" / "coding"
    builtin_coding.mkdir(parents=True)
    (builtin_coding / "role.md").write_text("---\n---\nrole\n")
    for name in ("default", "common"):
        role_dir = global_dir / "roles" / name
        role_dir.mkdir(parents=True)
        (role_dir / "role.md").write_text("---\n---\nrole\n")
    monkeypatch.setattr(role_mgr_module, "builtin_root", lambda: builtin_root)

    roles = role_mgr_module.discover_roles(workdir, global_dir, False)

    assert roles == {"coding": builtin_coding}


def test_dotted_configured_role_activates_and_reads_exact_effort(tmp_path: Path) -> None:
    """含点角色应原样激活，配置读取不得把名称拆成多段。"""
    global_dir = tmp_path / "global"
    _add_role(global_dir, name="foo.bar")

    role_mgr = RoleMgr(
        config_mgr=ConfigStub({
            "role": {
                "default": "foo.bar",
                "foo.bar": {"reasoning_effort": "xhigh"},
            }
        }),
        workdir=tmp_path / "work",
        global_dir=global_dir,
    )

    assert role_mgr.role_name == "foo.bar"
    assert role_mgr.manifest is not None
    assert role_mgr.manifest.reasoning_effort == "xhigh"

# —— active_role_name ————————————————————————————————————————————————


def test_default_role_constant_is_coding() -> None:
    """导出的缺省角色常量应为 coding。"""
    assert DEFAULT_ROLE == "coding"


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"role": {}},
        {"role": "reviewer"},
        {"role": {"default": ""}},
        {"role": {"default": "   "}},
        {"role": {"default": None}},
        {"role": {"default": 123}},
    ],
)
def test_active_role_name_falls_back_to_default(config: dict[str, Any]) -> None:
    """role.default 缺失、空值或非字符串时应回退 DEFAULT_ROLE。"""
    assert active_role_name(ConfigStub(config)) == DEFAULT_ROLE


def test_active_role_name_reads_configured_value() -> None:
    """role.default 有值时应返回该值，并去除首尾空白。"""
    assert active_role_name(ConfigStub({"role": {"default": "reviewer"}})) == "reviewer"
    assert (
        active_role_name(ConfigStub({"role": {"default": "  reviewer  "}})) == "reviewer"
    )


def test_active_role_name_does_not_require_existing_role_dir() -> None:
    """不存在的角色名也应原样返回，不做目录校验。"""
    assert active_role_name(ConfigStub({"role": {"default": "missing"}})) == "missing"


# —— role.md 的 model 字段已废弃 ————————————————————————————————————


@pytest.mark.parametrize("model", ["claude-opus-5", "fast", ""])
def test_role_md_model_field_raises_configuration_error(
    tmp_path: Path, model: str
) -> None:
    """role.md 残留 model 字段应报错，且消息包含该文件路径。"""
    with pytest.raises(LLMConfigurationError) as excinfo:
        _role_mgr(tmp_path, {"role": {"default": "reviewer"}}, model=model)

    role_md = tmp_path / "global" / "roles" / "reviewer" / "role.md"
    message = str(excinfo.value)
    assert str(role_md) in message
    assert 'role["reviewer"].model.default' in message
    assert "fast" in message
    assert "模型发现尚未执行" in message


def test_role_md_without_model_yields_none_manifest_model(tmp_path: Path) -> None:
    """role.md 不含 model 字段时 manifest.model 应为 None。"""
    role_mgr, _ = _role_mgr(tmp_path, {"role": {"default": "reviewer"}})

    assert role_mgr.role_name == "reviewer"
    assert role_mgr.manifest is not None
    assert role_mgr.manifest.model is None


@pytest.mark.parametrize(
    "model_config",
    [
        "configured-model",
        {"default": "claude-opus-5", "fast": "deepseek-v4-flash"},
        {"default": "claude-opus-5"},
        None,
    ],
)
def test_role_model_config_no_longer_overrides_manifest(
    tmp_path: Path, model_config: Any
) -> None:
    """role.<角色>.model 不再覆盖 manifest.model（由 LLMMgr 直接消费）。"""
    role_mgr, _ = _role_mgr(
        tmp_path,
        {
            "role": {
                "default": "reviewer",
                "reviewer": {"model": model_config},
            },
        },
    )

    assert role_mgr.manifest is not None
    assert role_mgr.manifest.model is None


# —— 激活角色与 reasoning_effort 覆盖 ————————————————————————————————


def test_role_default_selects_active_role(tmp_path: Path) -> None:
    """role.default 应选择配置中的角色。"""
    role_mgr, _ = _role_mgr(tmp_path, {"role": {"default": "reviewer"}})

    assert role_mgr.role_name == "reviewer"
    assert role_mgr.manifest is not None
    assert role_mgr.manifest.reasoning_effort == "high"


def test_scalar_role_config_is_ignored(tmp_path: Path) -> None:
    """旧标量 role 配置不应被当作激活角色。"""
    role_mgr, _ = _role_mgr(tmp_path, {"role": "reviewer"})

    assert role_mgr.role_name == DEFAULT_ROLE


def test_active_role_config_overrides_reasoning_effort(tmp_path: Path) -> None:
    """活动角色的 reasoning_effort 配置应覆盖 role.md 的同名字段。"""
    role_mgr, _ = _role_mgr(
        tmp_path,
        {
            "role": {
                "default": "reviewer",
                "reviewer": {"reasoning_effort": " XHIGH "},
            },
        },
    )

    assert role_mgr.manifest is not None
    assert role_mgr.manifest.reasoning_effort == "xhigh"
    assert role_mgr.manifest.model is None


def test_missing_effort_config_keeps_manifest_value(tmp_path: Path) -> None:
    """未配置 reasoning_effort 时应保留 role.md 中的值。"""
    role_mgr, _ = _role_mgr(
        tmp_path,
        {"role": {"default": "reviewer", "reviewer": {}}},
    )

    assert role_mgr.manifest is not None
    assert role_mgr.manifest.reasoning_effort == "high"


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
    """无效角色回退 coding 后，应应用 role.coding 的推理力度覆盖。"""
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
    assert role_mgr.manifest.model is None
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
    assert role_mgr.manifest.model is None
    assert role_mgr.manifest.reasoning_effort == "low"

    config_mgr.config = {
        "role": {
            "default": "reviewer",
            "reviewer": {"model": "best", "reasoning_effort": "max"},
        },
    }
    role_mgr.reload()

    assert role_mgr.manifest is not None
    assert role_mgr.manifest.model is None
    assert role_mgr.manifest.reasoning_effort == "max"
