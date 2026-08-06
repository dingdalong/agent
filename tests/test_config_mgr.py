from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.mgr.config_mgr import ConfigManager


def _manager(tmp_path: Path, *, project_trusted: bool = False) -> ConfigManager:
    return ConfigManager(
        global_dir=tmp_path / "global",
        workdir=tmp_path / "work",
        project_trusted=project_trusted,
    )


def test_set_config_writes_only_global_override_layer_after_reload(tmp_path):
    global_path = tmp_path / "global" / "config.yaml"
    global_path.parent.mkdir()
    global_path.write_text("llm:\n  concurrency: 2\n")
    manager = _manager(tmp_path)
    previous_default = manager.get_config("llm.default")

    manager.set_config("llm.default", "global-model", "global")

    assert manager.get_config("llm.default") == previous_default
    assert yaml.safe_load(global_path.read_text()) == {
        "llm": {"concurrency": 2, "default": "global-model"}
    }
    assert not (tmp_path / "work" / ".agent" / "config.yaml").exists()

    manager.reload()
    assert manager.get_config("llm.default") == "global-model"


def test_set_config_writes_project_layer_without_project_trust(tmp_path):
    manager = _manager(tmp_path, project_trusted=False)
    project_path = tmp_path / "work" / ".agent" / "config.yaml"

    manager.set_config("events.level", "trace", "project")

    assert yaml.safe_load(project_path.read_text()) == {"events": {"level": "trace"}}
    assert not (tmp_path / "global" / "config.yaml").exists()


@pytest.mark.parametrize("content", ["llm: [\n", "- not-a-mapping\n"])
def test_set_config_rejects_invalid_writable_yaml_without_overwrite(tmp_path, content):
    global_path = tmp_path / "global" / "config.yaml"
    global_path.parent.mkdir()
    global_path.write_text(content)
    manager = _manager(tmp_path)

    with pytest.raises(ValueError):
        manager.set_config("llm.default", "replacement", "global")

    assert global_path.read_text() == content


@pytest.mark.parametrize("key", ["", ".llm", "llm.", "llm..default"])
def test_set_config_rejects_invalid_key_without_creating_file(tmp_path, key):
    manager = _manager(tmp_path)

    with pytest.raises(ValueError):
        manager.set_config(key, "value", "global")

    assert not (tmp_path / "global" / "config.yaml").exists()


def test_set_config_rejects_invalid_scope_and_scalar_intermediate_node(tmp_path):
    global_path = tmp_path / "global" / "config.yaml"
    global_path.parent.mkdir()
    global_path.write_text("llm: scalar\n")
    manager = _manager(tmp_path)

    with pytest.raises(ValueError):
        manager.set_config("llm.default", "replacement", "global")
    with pytest.raises(ValueError):
        manager.set_config("llm.default", "replacement", "builtin")

    assert global_path.read_text() == "llm: scalar\n"
