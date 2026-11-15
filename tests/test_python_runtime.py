"""Interpreter selection tests use inert files, never real tasks or notebooks."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from web_mcp.paths import WorkspaceViolation
from web_tools.python_runtime import resolve_python_runtime


@pytest.fixture
def runtime(tmp_path):
    workspace = tmp_path / "allowed"
    workspace.mkdir()
    service = workspace / "service"
    service.mkdir()
    project = workspace / "project"
    project.mkdir()
    settings = SimpleNamespace(
        project_root=service,
        allowed_workspaces=[workspace],
        extra={},
    )
    return settings, project, workspace


def executable(root: Path, name: str | None = None) -> Path:
    path = root / (name or ("python.exe" if os.name == "nt" else "python3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"inert interpreter fixture; not executed")
    path.chmod(path.stat().st_mode | 0o111)
    return path.resolve()


def venv(root: Path) -> Path:
    relative = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return executable(root, relative)


def vscode(project: Path, data: dict) -> Path:
    path = project / ".vscode" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def default_config(settings, value) -> Path:
    path = settings.project_root / "07_web_config" / "python_runtime.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"default_python_executable": value}), encoding="utf-8")
    return path


def test_explicit_interpreter_wins_over_all_project_and_default_candidates(runtime):
    settings, project, workspace = runtime
    chosen = executable(workspace / "chosen")
    vscode(project, {"python.defaultInterpreterPath": "missing-python"})
    venv(project / ".venv")
    default_config(settings, "also-invalid")
    assert resolve_python_runtime({"python_executable": str(chosen)}, settings, project) == {
        "executable": str(chosen), "source": "explicit",
    }


def test_service_override_is_explicit_and_ignores_project_configuration(runtime):
    settings, project, _ = runtime
    vscode(project, {"python.defaultInterpreterPath": "missing-python"})
    assert resolve_python_runtime({"python_executable": "service"}, settings, project) == {
        "executable": sys.executable, "source": "service",
    }


@pytest.mark.parametrize("value", ["python", "./python", "", 7, '"python.exe"', "python.exe -c print(1)", "python\n"])
def test_invalid_explicit_value_fails_instead_of_falling_back(runtime, value):
    settings, project, _ = runtime
    venv(project / ".venv")
    with pytest.raises((ValueError, FileNotFoundError)):
        resolve_python_runtime({"python_executable": value}, settings, project)


def test_missing_explicit_path_fails_instead_of_using_project_venv(runtime):
    settings, project, workspace = runtime
    venv(project / ".venv")
    with pytest.raises(FileNotFoundError):
        resolve_python_runtime({"python_executable": str(workspace / "missing" / "python.exe")}, settings, project)


def test_explicit_venv_directory_is_resolved(runtime):
    settings, project, workspace = runtime
    environment = workspace / "selected-env"
    chosen = venv(environment)
    assert resolve_python_runtime({"python_executable": str(environment)}, settings, project) == {
        "executable": str(chosen), "source": "explicit",
    }


def test_explicit_path_outside_allowed_roots_is_rejected(runtime, tmp_path):
    settings, project, _ = runtime
    outside = executable(tmp_path / "outside")
    with pytest.raises(WorkspaceViolation):
        resolve_python_runtime({"python_executable": str(outside)}, settings, project)


def test_arbitrary_executable_name_is_rejected(runtime):
    settings, project, workspace = runtime
    other = executable(workspace, "cmd.exe")
    with pytest.raises(ValueError, match="not a Python executable"):
        resolve_python_runtime({"python_executable": str(other)}, settings, project)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable permissions")
def test_non_executable_python_file_is_rejected(runtime):
    settings, project, workspace = runtime
    candidate = executable(workspace)
    candidate.chmod(0o600)
    with pytest.raises(ValueError, match="not executable"):
        resolve_python_runtime({"python_executable": str(candidate)}, settings, project)


def test_nearest_vscode_configuration_wins_and_default_key_precedes_legacy(runtime):
    settings, project, _ = runtime
    child = project / "nested"
    child.mkdir()
    old = venv(project / "old-env")
    chosen = venv(child / "selected-env")
    vscode(project, {"python.defaultInterpreterPath": str(old)})
    vscode(child, {"python.defaultInterpreterPath": "${workspaceFolder}/selected-env", "python.pythonPath": str(old)})
    assert resolve_python_runtime({}, settings, child) == {"executable": str(chosen), "source": "vscode"}


def test_ancestor_vscode_setting_precedes_nearer_automatic_venv(runtime):
    settings, project, _ = runtime
    child = project / "nested"
    child.mkdir()
    chosen = venv(project / "declared-env")
    venv(child / ".venv")
    vscode(project, {"python.pythonPath": "declared-env"})
    assert resolve_python_runtime({}, settings, child) == {"executable": str(chosen), "source": "vscode"}


def test_jsonc_comments_urls_and_trailing_commas_are_safe(runtime):
    settings, project, _ = runtime
    chosen = venv(project / "environment")
    path = vscode(project, {})
    path.write_text(
        '\ufeff{\n// line comment\n"url": "https://example.test/a//b",\n'
        '"other": ["/* quoted text */",], /* block comment */\n'
        '"python.pythonPath": "${workspaceFolder}/environment",\n}',
        encoding="utf-8",
    )
    assert resolve_python_runtime({}, settings, project) == {"executable": str(chosen), "source": "vscode"}


@pytest.mark.parametrize("value", ["../outside", "${workspaceFolder}/../outside"])
def test_project_relative_interpreter_cannot_escape_project(runtime, value):
    settings, project, workspace = runtime
    venv(workspace / "outside")
    vscode(project, {"python.defaultInterpreterPath": value})
    with pytest.raises(WorkspaceViolation, match="escapes its project"):
        resolve_python_runtime({}, settings, project)


def test_vscode_absolute_shared_interpreter_is_allowed_within_allowlist(runtime):
    settings, project, workspace = runtime
    chosen = executable(workspace / "shared")
    vscode(project, {"python.defaultInterpreterPath": str(chosen)})
    assert resolve_python_runtime({}, settings, project) == {"executable": str(chosen), "source": "vscode"}


@pytest.mark.parametrize("value", ["missing", "${env:PYTHON}", "", None])
def test_invalid_vscode_selection_does_not_silently_use_another_runtime(runtime, value):
    settings, project, _ = runtime
    vscode(project, {"python.defaultInterpreterPath": value})
    venv(project / ".venv")
    with pytest.raises((ValueError, FileNotFoundError)):
        resolve_python_runtime({}, settings, project)


@pytest.mark.parametrize("body", ["{broken", "[]", "{/* unfinished"])
def test_invalid_vscode_jsonc_is_reported(runtime, body):
    settings, project, _ = runtime
    path = vscode(project, {})
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match="configuration"):
        resolve_python_runtime({}, settings, project)


def test_unrelated_vscode_settings_do_not_block_project_venv(runtime):
    settings, project, _ = runtime
    chosen = venv(project / ".venv")
    vscode(project, {"editor.tabSize": 4})
    assert resolve_python_runtime({}, settings, project) == {"executable": str(chosen), "source": "project_venv"}


def test_nearest_venv_wins_before_default_and_dotvenv_before_venv(runtime):
    settings, project, workspace = runtime
    child = project / "nested"
    child.mkdir()
    venv(project / ".venv")
    venv(child / "venv")
    chosen = venv(child / ".venv")
    default_config(settings, str(executable(workspace / "default")))
    assert resolve_python_runtime({}, settings, child) == {"executable": str(chosen), "source": "project_venv"}


def test_broken_project_venv_does_not_silently_fall_back(runtime):
    settings, project, workspace = runtime
    (project / ".venv").mkdir()
    default_config(settings, str(executable(workspace / "default")))
    with pytest.raises(ValueError, match="has no interpreter"):
        resolve_python_runtime({}, settings, project)


def test_global_default_is_used_without_project_runtime(runtime):
    settings, project, workspace = runtime
    chosen = executable(workspace / "default")
    default_config(settings, str(chosen))
    assert resolve_python_runtime({}, settings, project) == {"executable": str(chosen), "source": "configured_default"}


def test_default_user_base_is_returned_only_for_configured_default(runtime):
    settings, project, workspace = runtime
    chosen = executable(workspace / "default")
    user_base = workspace / "user-python"
    user_base.mkdir()
    config = default_config(settings, str(chosen))
    config.write_text(json.dumps({
        "default_python_executable": str(chosen),
        "default_python_user_base": str(user_base),
    }), encoding="utf-8")
    assert resolve_python_runtime({}, settings, project) == {
        "executable": str(chosen), "source": "configured_default", "user_base": str(user_base.resolve()),
    }
    assert resolve_python_runtime({"python_executable": str(chosen)}, settings, project) == {
        "executable": str(chosen), "source": "explicit",
    }
    project_python = venv(project / ".venv")
    assert resolve_python_runtime({}, settings, project) == {
        "executable": str(project_python), "source": "project_venv",
    }


@pytest.mark.parametrize("kind", ["relative", "missing", "file", "outside", "empty", "null"])
def test_invalid_configured_user_base_fails_instead_of_silently_omitting_it(runtime, tmp_path, kind):
    settings, project, workspace = runtime
    chosen = executable(workspace / "default")
    outside = tmp_path / "outside-userbase"
    outside.mkdir()
    values = {
        "relative": "user-python",
        "missing": str(workspace / "missing-userbase"),
        "file": str(chosen),
        "outside": str(outside),
        "empty": "",
        "null": None,
    }
    config = default_config(settings, str(chosen))
    config.write_text(json.dumps({
        "default_python_executable": str(chosen),
        "default_python_user_base": values[kind],
    }), encoding="utf-8")
    with pytest.raises((ValueError, FileNotFoundError)):
        resolve_python_runtime({}, settings, project)


@pytest.mark.parametrize("value", ["relative/python.exe", "", None])
def test_invalid_global_default_fails_instead_of_service_fallback(runtime, value):
    settings, project, _ = runtime
    default_config(settings, value)
    with pytest.raises(ValueError):
        resolve_python_runtime({}, settings, project)


def test_missing_configuration_falls_back_to_running_service(runtime):
    settings, project, _ = runtime
    assert resolve_python_runtime({}, settings, project) == {"executable": sys.executable, "source": "service"}


def test_config_override_isolated_from_production_default(runtime):
    settings, project, workspace = runtime
    default_config(settings, "invalid-production-default")
    settings.extra["python_runtime_config"] = workspace / "isolated-runtime.json"
    assert resolve_python_runtime({}, settings, project)["source"] == "service"
    chosen = executable(workspace / "overridden")
    Path(settings.extra["python_runtime_config"]).write_text(
        json.dumps({"default_python_executable": str(chosen)}), encoding="utf-8",
    )
    assert resolve_python_runtime({}, settings, project) == {"executable": str(chosen), "source": "configured_default"}


def test_missing_trusted_config_override_outside_workspace_does_not_read_production(runtime, tmp_path):
    settings, project, _ = runtime
    default_config(settings, "invalid-production-default")
    settings.extra["python_runtime_config"] = tmp_path / "outside-missing-config.json"
    assert resolve_python_runtime({}, settings, project) == {
        "executable": sys.executable, "source": "service",
    }


def test_existing_config_override_outside_workspace_is_rejected(runtime, tmp_path):
    settings, project, _ = runtime
    config = tmp_path / "outside-existing-config.json"
    config.write_text('{}', encoding="utf-8")
    settings.extra["python_runtime_config"] = config
    with pytest.raises(WorkspaceViolation):
        resolve_python_runtime({}, settings, project)


def test_service_checkout_environment_does_not_override_business_default(runtime):
    settings, _, workspace = runtime
    venv(settings.project_root / ".venv")
    vscode(settings.project_root, {"python.defaultInterpreterPath": "missing-service-setting"})
    chosen = executable(workspace / "business-default")
    default_config(settings, str(chosen))
    assert resolve_python_runtime({}, settings, settings.project_root) == {
        "executable": str(chosen), "source": "configured_default",
    }


def test_ancestor_scan_stops_at_allowed_boundary(runtime, tmp_path):
    settings, project, _ = runtime
    vscode(tmp_path, {"python.defaultInterpreterPath": "missing-outside-runtime"})
    assert resolve_python_runtime({}, settings, project)["source"] == "service"


def test_cwd_must_be_an_allowed_directory_even_for_explicit_service(runtime, tmp_path):
    settings, _, _ = runtime
    with pytest.raises(WorkspaceViolation):
        resolve_python_runtime({"python_executable": "service"}, settings, tmp_path)


def test_interpreter_symlink_cannot_escape_allowed_roots(runtime, tmp_path):
    settings, project, workspace = runtime
    outside = executable(tmp_path / "outside")
    alias = workspace / outside.name
    try:
        alias.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("Creating symlinks is unavailable")
    with pytest.raises(WorkspaceViolation):
        resolve_python_runtime({"python_executable": str(alias)}, settings, project)
