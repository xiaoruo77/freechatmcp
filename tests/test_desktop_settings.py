from __future__ import annotations

import json
from pathlib import Path

import pytest

from web_config.settings import Settings


DESKTOP_ENV = (
    "ENABLED", "CONTROL_ENABLED", "TRANSPORT", "URL", "TOKEN_FILE", "TIMEOUT", "COMMAND", "ARGS_JSON",
)


@pytest.fixture
def desktop_config(monkeypatch, tmp_path):
    for key in DESKTOP_ENV:
        monkeypatch.delenv("WEB_AGENT_WINDOWS_MCP_" + key, raising=False)
    monkeypatch.setenv("WEB_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("WEB_AGENT_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("WEB_AGENT_LOGS", str(tmp_path / "logs"))
    monkeypatch.setenv("WEB_AGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("WEB_AGENT_TOKEN", "desktop-settings-test-token")
    monkeypatch.setenv("WEB_AGENT_ORCHESTRATION_MODE", "legacy_sampling")
    folder = tmp_path / "07_web_config"
    folder.mkdir()
    return folder / "windows_mcp.json"


def write_config(path: Path, config):
    path.write_text(json.dumps(config), encoding="utf-8-sig")


def test_no_file_defaults_desktop_off_without_creating_configuration(desktop_config):
    loaded = Settings.load()
    assert loaded.windows_mcp_enabled is False
    assert loaded.windows_mcp_control_enabled is False
    assert loaded.windows_mcp_transport == "stdio"
    assert loaded.windows_mcp_command == "uvx"
    assert loaded.windows_mcp_timeout == 30
    assert loaded.windows_mcp_token_file is None
    assert "windows-mcp==0.8.8" in loaded.windows_mcp_args
    assert not desktop_config.exists()


def test_full_http_file_is_loaded_without_reading_token_contents(desktop_config, tmp_path):
    token_path = tmp_path / "private-token-reference"
    token_path.write_bytes(b"\xff\xfe\x00invalid-secret-not-for-settings")
    write_config(desktop_config, {
        "enabled": True, "control_enabled": True, "transport": "streamable-http",
        "url": "http://127.0.0.1:48273/mcp", "token_file": str(token_path), "timeout": 45,
        "command": "unused-launcher", "args": ["serve", "--transport", "stdio"],
    })
    loaded = Settings.load()
    assert loaded.windows_mcp_enabled is loaded.windows_mcp_control_enabled is True
    assert loaded.windows_mcp_url == "http://127.0.0.1:48273/mcp"
    assert loaded.windows_mcp_token_file == token_path
    assert loaded.windows_mcp_timeout == 45
    assert loaded.windows_mcp_command == "unused-launcher"
    assert token_path.read_bytes() == b"\xff\xfe\x00invalid-secret-not-for-settings"


def test_environment_overrides_each_file_option_including_explicit_false(desktop_config, tmp_path, monkeypatch):
    write_config(desktop_config, {
        "enabled": True, "control_enabled": True, "transport": "streamable-http",
        "url": "http://127.0.0.1:48273/mcp", "token_file": str(tmp_path / "file-token"),
        "timeout": 50, "command": "file-command", "args": ["file-arg"],
    })
    overrides = {
        "ENABLED": "false", "CONTROL_ENABLED": "0", "TRANSPORT": "stdio", "URL": "",
        "TOKEN_FILE": "", "TIMEOUT": "20", "COMMAND": "env-command", "ARGS_JSON": '["env-arg"]',
    }
    for name, value in overrides.items():
        monkeypatch.setenv("WEB_AGENT_WINDOWS_MCP_" + name, value)
    loaded = Settings.load()
    assert loaded.windows_mcp_enabled is loaded.windows_mcp_control_enabled is False
    assert loaded.windows_mcp_transport == "stdio"
    assert loaded.windows_mcp_url == ""
    assert loaded.windows_mcp_token_file is None
    assert loaded.windows_mcp_timeout == 20
    assert loaded.windows_mcp_command == "env-command"
    assert loaded.windows_mcp_args == ["env-arg"]


def test_partial_file_can_get_http_connection_fields_from_environment(desktop_config, tmp_path, monkeypatch):
    write_config(desktop_config, {"transport": "streamable-http"})
    monkeypatch.setenv("WEB_AGENT_WINDOWS_MCP_URL", "http://[::1]:48273/mcp")
    monkeypatch.setenv("WEB_AGENT_WINDOWS_MCP_TOKEN_FILE", str(tmp_path / "private-token"))
    assert Settings.load().windows_mcp_url == "http://[::1]:48273/mcp"


@pytest.mark.parametrize("config", [
    [], None, "configuration", {"token": "sensitive-secret"}, {"orchestration_mode": "chatgpt_direct"},
    {"enabled": "false"}, {"enabled": 1}, {"control_enabled": None},
    {"transport": "http"}, {"transport": []},
    {"timeout": True}, {"timeout": 1.5}, {"timeout": "30"}, {"timeout": 0}, {"timeout": 121},
    {"args": "serve --transport stdio"}, {"args": [1]}, {"args": [True]}, {"args": ["safe\u0000unsafe"]},
    {"command": ""}, {"command": 1}, {"command": "invalid\ncommand"},
    {"url": None}, {"token_file": "relative-token"}, {"token_file": 1},
])
def test_bad_file_types_and_unknown_keys_fail_before_runtime_side_effects(desktop_config, config):
    write_config(desktop_config, config)
    with pytest.raises(ValueError) as error:
        Settings.load()
    assert "sensitive-secret" not in str(error.value)
    assert str(desktop_config) not in str(error.value)
    assert not (desktop_config.parents[1] / "data").exists()


@pytest.mark.parametrize("url", [
    "http://example.com:48273/mcp", "http://localhost:48273/mcp", "http://0.0.0.0:48273/mcp",
    "http://127.0.0.2:48273/mcp", "http://[::]:48273/mcp", "ftp://127.0.0.1/mcp",
    "http://private-user:sensitive-secret@127.0.0.1:48273/mcp", "http://127.0.0.1/mcp?token=sensitive-secret",
    "http://127.0.0.1/mcp#sensitive-secret", "http://127.0.0.1:bad/mcp", "http://127.0.0.1:0/mcp",
    "http://127.0.0.1:65536/mcp", " http://127.0.0.1/mcp", "http://[::1/mcp",
])
def test_non_loopback_and_credential_bearing_urls_fail_safely(desktop_config, url):
    write_config(desktop_config, {"url": url})
    with pytest.raises(ValueError) as error:
        Settings.load()
    assert url not in str(error.value)
    assert "sensitive-secret" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("config", [
    {"transport": "streamable-http"},
    {"transport": "streamable-http", "url": "http://127.0.0.1:48273/mcp"},
])
def test_http_requires_absolute_private_token_reference(desktop_config, config):
    write_config(desktop_config, config)
    with pytest.raises(ValueError, match="absolute token_file"):
        Settings.load()


@pytest.mark.parametrize("name,value", [
    ("ENABLED", "perhaps-sensitive-secret"), ("CONTROL_ENABLED", "2"),
    ("TRANSPORT", "http"), ("TIMEOUT", "not-an-integer"), ("TIMEOUT", "0"), ("TIMEOUT", "121"),
    ("TIMEOUT", "30.0"), ("TIMEOUT", "9" * 5000), ("COMMAND", ""),
    ("ARGS_JSON", "sensitive-secret"), ("ARGS_JSON", '"sensitive-secret"'), ("ARGS_JSON", '[1]'),
    ("TOKEN_FILE", "relative-sensitive-secret"), ("URL", "http://example.com/sensitive-secret"),
])
def test_invalid_environment_values_are_strict_and_do_not_leak_input(desktop_config, monkeypatch, name, value):
    monkeypatch.setenv("WEB_AGENT_WINDOWS_MCP_" + name, value)
    with pytest.raises(ValueError) as error:
        Settings.load()
    assert "sensitive-secret" not in str(error.value)
    assert str(desktop_config) not in str(error.value)


@pytest.mark.parametrize("raw,expected", [("false", False), ("0", False), ("OFF", False), (" no ", False), ("true", True), ("1", True), ("YES", True), ("on", True)])
def test_boolean_environment_values_are_explicit(desktop_config, monkeypatch, raw, expected):
    monkeypatch.setenv("WEB_AGENT_WINDOWS_MCP_ENABLED", raw)
    assert Settings.load().windows_mcp_enabled is expected


def test_invalid_file_cannot_be_hidden_by_environment_override(desktop_config, monkeypatch):
    write_config(desktop_config, {"enabled": "false"})
    monkeypatch.setenv("WEB_AGENT_WINDOWS_MCP_ENABLED", "false")
    with pytest.raises(ValueError, match="JSON booleans"):
        Settings.load()


@pytest.mark.parametrize("raw", ['{"enabled": true,', '{"enabled": false, "enabled": true}', '{"token_file": "sensitive-secret"'])
def test_malformed_or_ambiguous_file_fails_without_echoing_content(desktop_config, raw):
    desktop_config.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError) as error:
        Settings.load()
    assert "sensitive-secret" not in str(error.value)
    assert str(desktop_config) not in str(error.value)


def test_desktop_file_is_read_at_load_time_without_mutating_existing_settings(desktop_config):
    write_config(desktop_config, {"enabled": False})
    first = Settings.load()
    write_config(desktop_config, {"enabled": True})
    second = Settings.load()
    assert first.windows_mcp_enabled is False
    assert second.windows_mcp_enabled is True


def test_desktop_changes_leave_other_timeout_and_mode_parsing_unchanged(desktop_config, monkeypatch):
    monkeypatch.setenv("WEB_AGENT_TASK_TIMEOUT", "invalid-legacy-value")
    assert Settings.load().default_task_timeout == 3600
    mode = desktop_config.with_name("orchestration.json")
    mode.write_text(json.dumps({"orchestration_mode": "chatgpt_direct", "enabled": True}), encoding="utf-8")
    with pytest.raises(ValueError, match="only orchestration_mode"):
        Settings.load()