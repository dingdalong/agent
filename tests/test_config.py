"""AppConfig 和 load_config 测试。"""
import yaml
from pathlib import Path

from src.config import AppConfig, load_config


class TestAppConfig:
    """AppConfig 路径解析测试。"""

    def test_resolve_relative(self, tmp_path: Path):
        config = AppConfig(workspace=tmp_path, data_dir=tmp_path / ".agent_data", raw={})
        result = config.resolve("skills/")
        assert result == (tmp_path / "skills").resolve()

    def test_resolve_absolute(self, tmp_path: Path):
        config = AppConfig(workspace=tmp_path, data_dir=tmp_path / ".agent_data", raw={})
        abs_path = "/usr/local/share"
        result = config.resolve(abs_path)
        assert result == Path(abs_path)

    def test_resolve_data_relative(self, tmp_path: Path):
        data_dir = tmp_path / ".agent_data"
        config = AppConfig(workspace=tmp_path, data_dir=data_dir, raw={})
        result = config.resolve_data("chroma")
        assert result == (data_dir / "chroma").resolve()

    def test_resolve_data_absolute(self, tmp_path: Path):
        config = AppConfig(workspace=tmp_path, data_dir=tmp_path / ".agent_data", raw={})
        abs_path = "/var/data/chroma"
        result = config.resolve_data(abs_path)
        assert result == Path(abs_path)


class TestLoadConfig:
    """load_config 集成测试。"""

    def test_default_workspace_is_config_dir(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"llm": {"model": "test"}}))
        config = load_config(str(config_file))
        assert config.workspace == tmp_path.resolve()

    def test_custom_workspace_relative_to_config_dir(self, tmp_path: Path):
        project_dir = tmp_path / "my_project"
        project_dir.mkdir()
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"workspace": "my_project"}))
        config = load_config(str(config_file))
        assert config.workspace == project_dir.resolve()

    def test_default_data_dir(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({}))
        config = load_config(str(config_file))
        assert config.data_dir == (tmp_path / ".agent_data").resolve()

    def test_custom_data_dir(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"data_dir": "my_data"}))
        config = load_config(str(config_file))
        assert config.data_dir == (tmp_path / "my_data").resolve()

    def test_missing_config_file(self, tmp_path: Path):
        config = load_config(str(tmp_path / "nonexistent.yaml"))
        assert config.workspace == tmp_path.resolve()
        assert config.data_dir == (tmp_path / ".agent_data").resolve()

    def test_raw_dict_preserved(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"llm": {"model": "deepseek"}}))
        config = load_config(str(config_file))
        assert config.raw["llm"]["model"] == "deepseek"

    def test_env_secrets_merged(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({}))
        config = load_config(str(config_file))
        assert config.raw["llm"]["api_key"] == "sk-test-key"

    def test_data_dir_relative_to_custom_workspace(self, tmp_path: Path):
        """data_dir 相对于 workspace 解析，而非 config 目录。"""
        project_dir = tmp_path / "my_project"
        project_dir.mkdir()
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"workspace": "my_project", "data_dir": "my_data"}))
        config = load_config(str(config_file))
        assert config.data_dir == (project_dir / "my_data").resolve()

    def test_yaml_value_not_overridden_by_env(self, tmp_path: Path, monkeypatch):
        """YAML 中有值时，.env 不覆盖。"""
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"llm": {"api_key": "yaml-key"}}))
        config = load_config(str(config_file))
        assert config.raw["llm"]["api_key"] == "yaml-key"
