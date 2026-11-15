from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from web_tools.profiles import python_command
from web_tools.runner import ProfileRunner


def profile_settings(isolated_settings, tmp_path):
    return replace(isolated_settings, workspace_root=tmp_path, allowed_workspaces=[tmp_path])


def test_notebook_uses_selected_kernel_and_separate_artifacts(isolated_settings, tmp_path):
    settings = profile_settings(isolated_settings, tmp_path)
    notebook = tmp_path / "example.ipynb"
    content = {"nbformat": 4, "nbformat_minor": 5, "metadata": {"kernelspec": {"language": "python"}}, "cells": []}
    notebook.write_text(json.dumps(content), encoding="utf-8")
    before = notebook.read_bytes()
    cmd = python_command({"script_path": str(notebook), "python_executable": "service"}, settings, "nb-task")
    assert cmd.command[0] == sys.executable
    assert Path(cmd.command[1]).name == "notebook_execute.py"
    assert cmd.command[cmd.command.index("--python") + 1] == sys.executable
    assert cmd.details["notebook_output"] != str(notebook)
    assert cmd.cwd == tmp_path
    assert notebook.read_bytes() == before


def test_file_cwd_and_override_are_recorded(isolated_settings, tmp_path):
    settings = profile_settings(isolated_settings, tmp_path)
    script = tmp_path / "project" / "main.py"
    script.parent.mkdir()
    script.write_text("print('test')", encoding="utf-8")
    cmd = python_command({"script_path": str(script), "python_executable": "service"}, settings, "py-task")
    assert cmd.cwd == script.parent
    assert cmd.details["python_executable"] == sys.executable
    assert cmd.details["python_selection"] == "service"
    assert cmd.environment["PYTHONIOENCODING"] == "utf-8"


@pytest.mark.parametrize("suffix,language", [(".txt", "python"), (".ipynb", "julia")])
def test_unsupported_script_or_kernel_rejected(isolated_settings, tmp_path, suffix, language):
    settings = profile_settings(isolated_settings, tmp_path)
    script = tmp_path / ("example" + suffix)
    script.write_text(json.dumps({"nbformat": 4, "metadata": {"kernelspec": {"language": language}}, "cells": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        python_command({"script_path": str(script), "python_executable": "service"}, settings, "reject-task")


async def test_worker_reports_actual_runtime_and_utf8_output(isolated_settings, tmp_path):
    settings = profile_settings(isolated_settings, tmp_path)

    async def not_cancelled():
        return False

    async def output(level, message):
        pass

    result = await ProfileRunner(settings).run("python_script", {
        "code": "import sys; print(sys.executable); print('环境检查')",
        "cwd": str(tmp_path), "python_executable": "service", "timeout": 10,
    }, "runtime-result", not_cancelled, output)
    assert result.status == "success"
    assert sys.executable in result.stdout
    assert "环境检查" in result.stdout
    assert result.details["python_executable"] == sys.executable


def test_notebook_outputs_are_in_task_artifacts(isolated_settings, tmp_path):
    from web_tools.runner import RunnerResult

    settings = profile_settings(isolated_settings, tmp_path)
    root = settings.task_output_root / "artifacts"
    root.mkdir()
    notebook = root / "executed.ipynb"
    report = root / "notebook-execution.json"
    notebook.write_text("{}")
    report.write_text("{}")
    result = RunnerResult("success", 0, "", "", 1, {"notebook_output": str(notebook), "notebook_report": str(report)})
    paths = ProfileRunner(settings).save_outputs("artifacts", result)
    assert str(notebook) in paths and str(report) in paths


def test_configured_user_packages_reach_python_and_notebook(isolated_settings, tmp_path, monkeypatch):
    settings = profile_settings(isolated_settings, tmp_path)
    monkeypatch.setattr("web_tools.profiles.resolve_python_runtime", lambda *args: {
        "executable": sys.executable, "source": "configured_default", "user_base": str(tmp_path / "user-packages"),
    })
    cmd = python_command({"code": "print('test')", "cwd": str(tmp_path)}, settings, "user-site")
    assert cmd.environment["PYTHONUSERBASE"] == str(tmp_path / "user-packages")
    assert cmd.details["python_user_base"] == str(tmp_path / "user-packages")
