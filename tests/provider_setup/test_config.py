"""ConfigManager 新增能力的功能集中测试：显式 Provider 配置检测与全局 .env 批量写入。"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from dotenv import dotenv_values

from src.mgr.config_mgr import ConfigManager
from src.mgr.paths import builtin_root


def _builtin_provider_names() -> list[str]:
    """返回内置 config.yaml 中 llm_provider 的 provider 名列表。"""
    builtin = yaml.safe_load((builtin_root() / "config.yaml").read_text())
    return list(builtin.get("llm_provider", {}))


def _clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除所有内置 Provider 的 API_KEY/API_URL 环境变量，避免宿主环境干扰。"""
    for name in _builtin_provider_names():
        for suffix in ("API_KEY", "API_URL"):
            monkeypatch.delenv(f"{name.upper()}_{suffix}", raising=False)


def _manager(tmp_path: Path, *, project_trusted: bool = False) -> ConfigManager:
    return ConfigManager(
        global_dir=tmp_path / "global",
        workdir=tmp_path / "work",
        project_trusted=project_trusted,
    )


# ---------- has_explicit_provider_config ----------


def test_has_explicit_provider_config_false_with_only_builtin_config(tmp_path, monkeypatch):
    """只有内置 config.yaml 的 llm_provider 时不算显式配置。"""
    _clear_provider_env(monkeypatch)
    manager = _manager(tmp_path)

    assert manager.has_explicit_provider_config() is False


@pytest.mark.parametrize("suffix", ["API_KEY", "API_URL"])
def test_has_explicit_provider_config_true_for_builtin_env_key_even_empty(
    tmp_path, monkeypatch, suffix
):
    """任一内置 Provider 的 {NAME}_API_KEY/API_URL 键存在即算显式配置，空字符串也算。"""
    _clear_provider_env(monkeypatch)
    name = _builtin_provider_names()[0]
    monkeypatch.setenv(f"{name.upper()}_{suffix}", "")

    manager = _manager(tmp_path)

    assert manager.has_explicit_provider_config() is True


def test_has_explicit_provider_config_false_for_unrelated_env_key(tmp_path, monkeypatch):
    """非内置 Provider 名的 API_KEY/API_URL 环境变量不算显式配置。"""
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MYSTERY_API_KEY", "secret")

    manager = _manager(tmp_path)

    assert manager.has_explicit_provider_config() is False


def test_has_explicit_provider_config_true_for_global_env_file(tmp_path, monkeypatch):
    """全局 ~/.agent/.env 中的内置 Provider 键算显式配置，未信任项目也生效。"""
    _clear_provider_env(monkeypatch)
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / ".env").write_text("ANTHROPIC_API_KEY='secret'\n")

    manager = _manager(tmp_path, project_trusted=False)

    assert manager.has_explicit_provider_config() is True


def test_has_explicit_provider_config_project_env_only_when_trusted(tmp_path, monkeypatch):
    """项目 .agent/.env 与 workdir 根 .env 仅在 trusted 时纳入有效环境。"""
    _clear_provider_env(monkeypatch)
    project_dir = tmp_path / "work" / ".agent"
    project_dir.mkdir(parents=True)
    (project_dir / ".env").write_text("OPENAI_API_KEY='secret'\n")
    (tmp_path / "work" / ".env").write_text("DEEPSEEK_API_KEY='secret'\n")

    assert _manager(tmp_path, project_trusted=False).has_explicit_provider_config() is False
    assert _manager(tmp_path, project_trusted=True).has_explicit_provider_config() is True


def test_has_explicit_provider_config_true_for_global_config_mapping(tmp_path, monkeypatch):
    """全局 config.yaml 的非空 llm_provider mapping 算显式配置。"""
    _clear_provider_env(monkeypatch)
    global_path = tmp_path / "global" / "config.yaml"
    global_path.parent.mkdir()
    global_path.write_text(
        "llm_provider:\n  custom:\n    base_url: https://example.test/v1\n"
    )

    manager = _manager(tmp_path)

    assert manager.has_explicit_provider_config() is True


def test_has_explicit_provider_config_llm_default_only_not_explicit(tmp_path, monkeypatch):
    """用户层只写 llm.default 不算显式配置。"""
    _clear_provider_env(monkeypatch)
    global_path = tmp_path / "global" / "config.yaml"
    global_path.parent.mkdir()
    global_path.write_text("llm:\n  default: some-model\n")

    manager = _manager(tmp_path)

    assert manager.has_explicit_provider_config() is False


def test_has_explicit_provider_config_empty_mapping_not_explicit(tmp_path, monkeypatch):
    """空 llm_provider mapping 不算显式配置。"""
    _clear_provider_env(monkeypatch)
    global_path = tmp_path / "global" / "config.yaml"
    global_path.parent.mkdir()
    global_path.write_text("llm_provider: {}\n")

    manager = _manager(tmp_path)

    assert manager.has_explicit_provider_config() is False


def test_has_explicit_provider_config_project_mapping_only_when_trusted(tmp_path, monkeypatch):
    """项目 config.yaml 的 llm_provider 仅在 trusted 时算显式配置。"""
    _clear_provider_env(monkeypatch)
    project_path = tmp_path / "work" / ".agent" / "config.yaml"
    project_path.parent.mkdir(parents=True)
    project_path.write_text("llm_provider:\n  custom:\n    base_url: https://example.test/v1\n")

    assert _manager(tmp_path, project_trusted=False).has_explicit_provider_config() is False
    assert _manager(tmp_path, project_trusted=True).has_explicit_provider_config() is True


def test_has_explicit_provider_config_project_null_only_when_trusted(tmp_path, monkeypatch):
    """项目 config.yaml 的 llm_provider 显式 null 仅在 trusted 时视为显式配置。"""
    _clear_provider_env(monkeypatch)
    project_path = tmp_path / "work" / ".agent" / "config.yaml"
    project_path.parent.mkdir(parents=True)
    project_path.write_text("llm_provider:\n")

    assert _manager(tmp_path, project_trusted=False).has_explicit_provider_config() is False
    assert _manager(tmp_path, project_trusted=True).has_explicit_provider_config() is True


def test_has_explicit_provider_config_incomplete_provider_segment_counts(tmp_path, monkeypatch):
    """未知/不完整的非空 Provider 段仍算显式配置，交由后续 LLMMgr 报错。"""
    _clear_provider_env(monkeypatch)
    global_path = tmp_path / "global" / "config.yaml"
    global_path.parent.mkdir()
    global_path.write_text("llm_provider:\n  custom_provider: {}\n")

    manager = _manager(tmp_path)

    assert manager.has_explicit_provider_config() is True


@pytest.mark.parametrize(
    "content",
    [
        "llm_provider: just-a-string\n",
        "llm_provider: [deepseek]\n",
        "- not-a-mapping\n",
        "llm_provider:\n",
    ],
)
def test_has_explicit_provider_config_non_mapping_is_conservative_explicit(
    tmp_path, monkeypatch, content
):
    """用户层 llm_provider 不是 mapping 时保守视为显式配置，避免向导覆盖用户内容。"""
    _clear_provider_env(monkeypatch)
    global_path = tmp_path / "global" / "config.yaml"
    global_path.parent.mkdir()
    global_path.write_text(content)

    manager = _manager(tmp_path)

    assert manager.has_explicit_provider_config() is True


@pytest.mark.parametrize(
    "content",
    [
        "llm: [\n",
        "llm_provider: {bad\n",
    ],
)
def test_has_explicit_provider_config_invalid_yaml_is_conservative_explicit(
    tmp_path, monkeypatch, content
):
    """用户层 config.yaml YAML 无效时保守视为显式配置，避免向导覆盖用户内容。"""
    _clear_provider_env(monkeypatch)
    global_path = tmp_path / "global" / "config.yaml"
    global_path.parent.mkdir()
    global_path.write_text(content)

    manager = _manager(tmp_path)

    assert manager.has_explicit_provider_config() is True


# ---------- set_global_env ----------


def test_set_global_env_creates_new_file_with_all_values(tmp_path, monkeypatch):
    """批量写入新建全局 .env，且不修改 os.environ。"""
    _clear_provider_env(monkeypatch)
    manager = _manager(tmp_path)
    target = tmp_path / "global" / ".env"

    manager.set_global_env(
        {
            "DEEPSEEK_API_KEY": "secret-key",
            "ANTHROPIC_API_URL": "https://api.anthropic.test",
        }
    )

    assert dotenv_values(target) == {
        "DEEPSEEK_API_KEY": "secret-key",
        "ANTHROPIC_API_URL": "https://api.anthropic.test",
    }
    assert os.environ.get("DEEPSEEK_API_KEY") is None


def test_set_global_env_replaces_existing_key_and_preserves_rest(tmp_path, monkeypatch):
    """替换已有 key、新增 key，并保留注释、export 与无关变量原文。"""
    _clear_provider_env(monkeypatch)
    manager = _manager(tmp_path)
    target = tmp_path / "global" / ".env"
    target.parent.mkdir()
    target.write_text(
        "# 注释行\n"
        "export KEEP_ME=original\n"
        "DEEPSEEK_API_KEY=old-value\n"
        "UNRELATED=keep\n"
    )

    manager.set_global_env(
        {"DEEPSEEK_API_KEY": "new-value", "OPENAI_API_URL": "https://openai.test/v1"}
    )

    assert dotenv_values(target) == {
        "KEEP_ME": "original",
        "DEEPSEEK_API_KEY": "new-value",
        "UNRELATED": "keep",
        "OPENAI_API_URL": "https://openai.test/v1",
    }
    text = target.read_text()
    assert "# 注释行" in text
    assert "export KEEP_ME=original" in text
    assert "UNRELATED=keep" in text


def test_set_global_env_special_chars_round_trip(tmp_path, monkeypatch):
    """特殊字符值经 dotenv_values 往返后保持一致。"""
    _clear_provider_env(monkeypatch)
    manager = _manager(tmp_path)
    value = "it's a \\ path $VAR \"quoted\""

    manager.set_global_env({"DEEPSEEK_API_KEY": value})

    assert dotenv_values(tmp_path / "global" / ".env")["DEEPSEEK_API_KEY"] == value


@pytest.mark.skipif(os.name != "posix", reason="POSIX 权限断言")
def test_set_global_env_file_and_dir_mode(tmp_path, monkeypatch):
    """写入后 .env 为 0600、所在目录为 0700。"""
    _clear_provider_env(monkeypatch)
    manager = _manager(tmp_path)

    manager.set_global_env({"DEEPSEEK_API_KEY": "k"})

    target = tmp_path / "global" / ".env"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("bad_key", ["", "1BAD", "A-B", "A B", "A.B", None])
def test_set_global_env_invalid_key_raises_without_creating_file(tmp_path, monkeypatch, bad_key):
    """非法变量名在任何文件操作前抛 ValueError。"""
    _clear_provider_env(monkeypatch)
    manager = _manager(tmp_path)
    target = tmp_path / "global" / ".env"

    with pytest.raises(ValueError):
        manager.set_global_env({bad_key: "v"})

    assert not target.exists()


def test_set_global_env_invalid_value_type_raises_without_creating_file(tmp_path, monkeypatch):
    """非 str 值在任何文件操作前抛 TypeError。"""
    _clear_provider_env(monkeypatch)
    manager = _manager(tmp_path)
    target = tmp_path / "global" / ".env"

    with pytest.raises(TypeError):
        manager.set_global_env({"DEEPSEEK_API_KEY": 123})

    assert not target.exists()


def test_set_global_env_empty_values_returns_without_creating_file(tmp_path, monkeypatch):
    """values 为空时直接返回，不创建文件。"""
    _clear_provider_env(monkeypatch)
    manager = _manager(tmp_path)

    manager.set_global_env({})

    assert not (tmp_path / "global" / ".env").exists()


def test_set_global_env_failure_keeps_original_and_cleans_staging(tmp_path, monkeypatch):
    """atomic_write_text 失败时原目标不变且无 staging 残留。"""
    _clear_provider_env(monkeypatch)
    manager = _manager(tmp_path)
    target = tmp_path / "global" / ".env"
    target.parent.mkdir()
    target.write_text("KEEP_ME=original\n")

    with patch("src.mgr.config_mgr.atomic_write_text", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            manager.set_global_env({"DEEPSEEK_API_KEY": "new"})

    assert target.read_text() == "KEEP_ME=original\n"
    leftovers = [p.name for p in (tmp_path / "global").iterdir() if p.name != ".env"]
    assert leftovers == []
