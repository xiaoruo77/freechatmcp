from __future__ import annotations

import json

import pytest


def configure(monkeypatch, tmp_path):
    from web_config.settings import Settings
    monkeypatch.setenv("WEB_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("WEB_AGENT_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("WEB_AGENT_LOGS", str(tmp_path / "logs"))
    monkeypatch.delenv("WEB_AGENT_ORCHESTRATION_MODE", raising=False)
    folder = tmp_path / "07_web_config"
    folder.mkdir()
    return Settings, folder / "orchestration.json"


def test_mode_rollout_is_opt_in_and_environment_can_override(monkeypatch, tmp_path):
    Settings, path = configure(monkeypatch, tmp_path)
    assert Settings.load().orchestration_mode == "legacy_sampling"
    path.write_text(json.dumps({"orchestration_mode": "chatgpt_direct"}), encoding="utf-8-sig")
    assert Settings.load().orchestration_mode == "chatgpt_direct"
    monkeypatch.setenv("WEB_AGENT_ORCHESTRATION_MODE", "legacy_sampling")
    assert Settings.load().orchestration_mode == "legacy_sampling"


@pytest.mark.parametrize("config", [{"orchestration_mode": "invalid"}, {"orchestration_mode": "chatgpt_direct", "token": "ignored-secret"}, []])
def test_mode_file_rejects_bad_mode_and_unexpected_settings(monkeypatch, tmp_path, config):
    Settings, path = configure(monkeypatch, tmp_path)
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError):
        Settings.load()


def test_desktop_launcher_uses_explicit_argument_array_and_defaults_off(monkeypatch, tmp_path):
    Settings, _ = configure(monkeypatch, tmp_path)
    monkeypatch.setenv("WEB_AGENT_WINDOWS_MCP_ARGS_JSON", '["serve","--transport","stdio"]')
    settings = Settings.load()
    assert settings.windows_mcp_args == ["serve", "--transport", "stdio"]
    assert not settings.windows_mcp_enabled and not settings.windows_mcp_control_enabled
    monkeypatch.setenv("WEB_AGENT_WINDOWS_MCP_ARGS_JSON", '"serve --transport stdio"')
    with pytest.raises(ValueError):
        Settings.load()