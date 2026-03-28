import json
import pytest
from src.mcp.config import MCPServerConfig, load_mcp_config


def test_load_stdio_config(tmp_path):
    config_file = tmp_path / "mcp_servers.json"
    config_file.write_text(json.dumps({
        "mcpServers": {
            "desktop-commander": {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@wonderwhy-er/desktop-commander@latest"],
                "env": {"DEBUG": "1"}
            }
        }
    }))
    configs = load_mcp_config(str(config_file))
    assert len(configs) == 1
    c = configs[0]
    assert c.name == "desktop-commander"
    assert c.transport == "stdio"
    assert c.command == "npx"
    assert c.args == ["-y", "@wonderwhy-er/desktop-commander@latest"]
    assert c.env == {"DEBUG": "1"}
    assert c.enabled is True
    assert c.timeout == 30.0


def test_load_http_config(tmp_path):
    config_file = tmp_path / "mcp_servers.json"
    config_file.write_text(json.dumps({
        "mcpServers": {
            "my-api": {
                "transport": "http",
                "url": "http://localhost:8080/mcp"
            }
        }
    }))
    configs = load_mcp_config(str(config_file))
    assert len(configs) == 1
    c = configs[0]
    assert c.name == "my-api"
    assert c.transport == "http"
    assert c.url == "http://localhost:8080/mcp"
    assert c.command is None
    assert c.args == []
    assert c.env == {}


def test_load_missing_file_returns_empty():
    configs = load_mcp_config("/nonexistent/path.json")
    assert configs == []


def test_load_disabled_server_skipped(tmp_path):
    config_file = tmp_path / "mcp_servers.json"
    config_file.write_text(json.dumps({
        "mcpServers": {
            "disabled-one": {
                "transport": "stdio",
                "command": "echo",
                "enabled": False
            },
            "active-one": {
                "transport": "stdio",
                "command": "echo"
            }
        }
    }))
    configs = load_mcp_config(str(config_file))
    assert len(configs) == 1
    assert configs[0].name == "active-one"


def test_load_stdio_without_command_skipped(tmp_path):
    config_file = tmp_path / "mcp_servers.json"
    config_file.write_text(json.dumps({
        "mcpServers": {
            "bad": {
                "transport": "stdio"
            }
        }
    }))
    configs = load_mcp_config(str(config_file))
    assert configs == []


def test_load_http_without_url_skipped(tmp_path):
    config_file = tmp_path / "mcp_servers.json"
    config_file.write_text(json.dumps({
        "mcpServers": {
            "bad": {
                "transport": "http"
            }
        }
    }))
    configs = load_mcp_config(str(config_file))
    assert configs == []


def test_load_custom_timeout(tmp_path):
    config_file = tmp_path / "mcp_servers.json"
    config_file.write_text(json.dumps({
        "mcpServers": {
            "slow-server": {
                "transport": "stdio",
                "command": "slow-cmd",
                "timeout": 120.0
            }
        }
    }))
    configs = load_mcp_config(str(config_file))
    assert configs[0].timeout == 120.0
