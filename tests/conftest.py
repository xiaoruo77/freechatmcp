from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("WEB_AGENT_HOME", str(ROOT))
os.environ.setdefault("WEB_AGENT_WORKSPACE", str(ROOT))
os.environ.setdefault("WEB_AGENT_TOKEN", "pytest-token")
os.environ.setdefault("WEB_AGENT_EMBEDDED_WORKER", "false")
# Unit suites must never reserve a real browser's single native IPC slot.
os.environ.setdefault("WEB_AGENT_NATIVE_BROWSER_IPC_ENABLED", "false")


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    from web_config.settings import Settings

    # Production desktop enablement must not grant input privileges to tests.
    monkeypatch.setenv("WEB_AGENT_WINDOWS_MCP_ENABLED", "false")
    monkeypatch.setenv("WEB_AGENT_WINDOWS_MCP_CONTROL_ENABLED", "false")
    base = Settings.load()
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    outputs = data / "task_outputs"
    scripts = data / "generated_scripts"
    for path in (data, logs, outputs, scripts):
        path.mkdir(parents=True, exist_ok=True)
    return replace(
        base,
        token="pytest-token",
        database_path=data / "test.sqlite3",
        data_root=data,
        log_root=logs,
        task_output_root=outputs,
        generated_script_root=scripts,
        embedded_worker=False,
        # The deployed mode file must not change the legacy regression suite.
        # Direct-mode tests explicitly opt in with dataclasses.replace().
        orchestration_mode="legacy_sampling",
    )
