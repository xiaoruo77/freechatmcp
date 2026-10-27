from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ("deploy-chatgpt-direct.ps1", "rollback-chatgpt-direct.ps1")


def _shell(name: str) -> str:
    if os.name != "nt":
        pytest.skip("Deployment argv regression requires Windows PowerShell")
    command = shutil.which(name)
    if command is None and name == "powershell":
        fallback = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        command = str(fallback) if fallback.is_file() else None
    if command is None:
        pytest.skip(f"{name} is not installed")
    return command


def _read_only_check(script_name: str) -> str:
    # Extract only the native Python invocation, never dot-source or run the
    # deployment/rollback script and never invoke any service operation.
    source = (ROOT / "10_web_deploy" / script_name).read_text(encoding="utf-8-sig")
    matches = [line.strip() for line in source.splitlines() if line.strip().startswith("$active = & ")]
    assert len(matches) == 1, "Expected exactly one task-state preflight"
    command = matches[0]
    assert "sqlite3.connect" in command and "mode=ro" in command
    assert "$dbPath" in command
    assert not any(word in command.lower() for word in ("restart-service", "stop-service", "start-service", "remove-item", "move-item"))
    return command


def _invoke_check(tmp_path: Path, shell_name: str, script_name: str, database: Path,
                  deployment_task_id: str | None = None) -> subprocess.CompletedProcess[str]:
    wrapper = tmp_path / "native argv check.ps1"
    # A BOM lets Windows PowerShell 5.1 read the same UTF-8 test wrapper as
    # current PowerShell, including fixture paths containing non-ASCII text.
    wrapper.write_text(
        "param([string]$TestProjectRoot, [string]$TestDbPath, [string]$DeploymentTaskId = '')\n"
        "$ErrorActionPreference = 'Stop'\n"
        "Write-Output ('SHELL_VERSION=' + $PSVersionTable.PSVersion.ToString())\n"
        "$projectRoot = $TestProjectRoot\n"
        "$dbPath = $TestDbPath\n"
        + _read_only_check(script_name) + "\n"
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
        "Write-Output ('ACTIVE_COUNT=' + [int]$active)\n",
        encoding="utf-8-sig",
    )
    command = [_shell(shell_name), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(wrapper), str(ROOT), str(database)]
    if deployment_task_id is not None:
        command.append(deployment_task_id)
    return subprocess.run(
        command,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, check=False,
    )


@pytest.mark.parametrize("shell_name", ["powershell", "pwsh"])
@pytest.mark.parametrize("script_name", SCRIPTS)
@pytest.mark.parametrize(("statuses", "expected"), [
    ([], 0),
    (["success", "failed", "cancelled"], 0),
    (["queued", "running", "queued", "success"], 3),
])
def test_task_preflight_native_argv_works_in_ps51_and_current_pwsh(tmp_path, shell_name, script_name, statuses, expected):
    folder = tmp_path / "task database 测试 with spaces"
    folder.mkdir()
    database = folder / "task state.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY, status TEXT NOT NULL)")
        connection.executemany("INSERT INTO tasks(id,status) VALUES(?,?)", [(f"task-{index}", status) for index, status in enumerate(statuses)])
    before = hashlib.sha256(database.read_bytes()).digest()
    result = _invoke_check(tmp_path, shell_name, script_name, database)
    assert result.returncode == 0, f"{shell_name}: {result.stdout}\n{result.stderr}"
    version = next(line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith("SHELL_VERSION="))
    if shell_name == "powershell":
        assert version.startswith("5.1."), f"Expected real Windows PowerShell 5.1; got {version}"
    else:
        assert int(version.split(".", 1)[0]) >= 7
    assert f"ACTIVE_COUNT={expected}" in result.stdout.splitlines()
    assert hashlib.sha256(database.read_bytes()).digest() == before


@pytest.mark.parametrize("shell_name", ["powershell", "pwsh"])
@pytest.mark.parametrize(("rows", "own_task_id", "expected"), [
    ([("task-deployment", "running")], "task-deployment", 0),
    ([("task-deployment", "running"), ("task-unrelated", "queued")], "task-deployment", 1),
    ([("task-deployment", "running"), ("task-unrelated", "queued")], None, 2),
    ([("task-deployment", "running"), ("task-unrelated", "queued")], "task-different", 2),
])
def test_deployment_preflight_excludes_only_its_explicit_task(tmp_path, shell_name, rows, own_task_id, expected):
    database = tmp_path / "deployment self task.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY, status TEXT NOT NULL)")
        connection.executemany("INSERT INTO tasks(id,status) VALUES(?,?)", rows)
    before = hashlib.sha256(database.read_bytes()).digest()
    result = _invoke_check(tmp_path, shell_name, "deploy-chatgpt-direct.ps1", database, own_task_id)
    assert result.returncode == 0, f"{shell_name}: {result.stdout}\n{result.stderr}"
    assert f"ACTIVE_COUNT={expected}" in result.stdout.splitlines()
    assert hashlib.sha256(database.read_bytes()).digest() == before


@pytest.mark.parametrize("shell_name", ["powershell", "pwsh"])
@pytest.mark.parametrize("script_name", SCRIPTS)
def test_task_preflight_readonly_mode_does_not_create_missing_database(tmp_path, shell_name, script_name):
    database = tmp_path / "missing database.sqlite3"
    result = _invoke_check(tmp_path, shell_name, script_name, database)
    assert result.returncode != 0
    assert "ACTIVE_COUNT=" not in result.stdout
    assert not database.exists()


def test_legacy_fixture_is_independent_of_deployed_mode(isolated_settings):
    assert isolated_settings.orchestration_mode == "legacy_sampling"