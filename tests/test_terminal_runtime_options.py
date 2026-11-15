from __future__ import annotations

import json
from dataclasses import replace

import pytest

from web_mcp.paths import WorkspaceViolation
from web_tools.profiles import cmd_command, powershell_command, terminal_command


@pytest.fixture
def terminal_settings(isolated_settings, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = workspace / "home"
    home.mkdir()
    config = tmp_path / "terminal-runtime.json"
    settings = replace(
        isolated_settings,
        workspace_root=workspace,
        allowed_workspaces=[workspace],
        extra={**isolated_settings.extra, "terminal_runtime_config": str(config)},
    )
    return settings, config, home


def configure(config, home, **overrides):
    options = {"execution_context": "active_user", "default_cwd": str(home), "load_powershell_profile": True}
    options.update(overrides)
    config.write_text(json.dumps(options), encoding="utf-8")


def test_missing_configuration_keeps_legacy_shells(terminal_settings):
    settings, _, _ = terminal_settings
    command = powershell_command({"commands": ["Get-Location"]}, settings, "test")
    assert command.cwd == settings.workspace_root
    assert "-NoProfile" in command.command
    assert command.details["execution_context"] == "service"
    assert command.details["powershell_profile_loaded"] is False


@pytest.mark.parametrize("cwd", [None, "", "."])
def test_active_user_profile_and_default_home(terminal_settings, cwd):
    settings, config, home = terminal_settings
    configure(config, home)
    command = powershell_command({"commands": ["Get-Location"], "cwd": cwd}, settings, "test")
    assert command.cwd == home
    assert "-NoProfile" not in command.command
    assert "-ExecutionPolicy" not in command.command
    assert "Bypass" not in command.command
    assert command.details == {
        "execution_context": "active_user", "powershell_profile_loaded": True, "working_directory": str(home)
    }
    assert "[Console]::OutputEncoding = $OutputEncoding" in command.command[-1]


def test_relative_and_explicit_cwd_respect_workspace_boundary(terminal_settings, tmp_path):
    settings, config, home = terminal_settings
    configure(config, home)
    child = home / "child"
    child.mkdir()
    explicit = settings.workspace_root / "explicit"
    explicit.mkdir()
    assert cmd_command({"commands": ["cd"], "cwd": "child"}, settings, "test").cwd == child
    assert cmd_command({"commands": ["cd"], "cwd": str(explicit)}, settings, "test").cwd == explicit
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(WorkspaceViolation):
        cmd_command({"commands": ["cd"], "cwd": str(outside)}, settings, "test")


def test_cmd_and_terminal_alias_have_user_context(terminal_settings):
    settings, config, home = terminal_settings
    configure(config, home)
    command = terminal_command({"shell": "cmd", "commands": ["cd"]}, settings, "test")
    assert command.cwd == home
    assert command.details["execution_context"] == "active_user"
    assert command.command[-3:-1] == ["/d", "/c"]


@pytest.mark.parametrize("overrides", [
    {"execution_context": "arbitrary_user"},
    {"load_powershell_profile": "true"},
    {"default_cwd": "relative"},
    {"unexpected": True},
])
def test_invalid_configuration_fails_closed(terminal_settings, overrides):
    settings, config, home = terminal_settings
    configure(config, home, **overrides)
    with pytest.raises(ValueError):
        powershell_command({"commands": ["Get-Location"]}, settings, "test")


def test_default_cwd_cannot_escape_workspace(terminal_settings, tmp_path):
    settings, config, _ = terminal_settings
    outside = tmp_path / "outside"
    outside.mkdir()
    configure(config, outside)
    with pytest.raises(WorkspaceViolation):
        cmd_command({"commands": ["cd"]}, settings, "test")


def test_configured_cwd_must_be_a_directory(terminal_settings):
    settings, config, home = terminal_settings
    file = home / "file"
    file.write_text("test")
    configure(config, file)
    with pytest.raises(ValueError, match="directory"):
        powershell_command({"commands": ["Get-Location"]}, settings, "test")


def test_malformed_json_fails_closed(terminal_settings):
    settings, config, _ = terminal_settings
    config.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="could not be read"):
        cmd_command({"commands": ["cd"]}, settings, "test")
