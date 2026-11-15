from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from web_tools.profiles import RunnerCommand
from web_tools.runner import ProfileRunner
from web_tools.windows_user_process import UserProcessResult


async def not_cancelled():
    return False


async def discard_output(level, message):
    pass


def configured_runner(monkeypatch, tmp_path, executable, details=None):
    runner = ProfileRunner(SimpleNamespace())
    command = RunnerCommand([executable, "diagnostic"], tmp_path, 10,
                            "Identity diagnostic", dict(details or {}))
    monkeypatch.setattr(runner, "build", lambda *args: command)
    return runner, command


class CompletedProcess:
    returncode = 0

    async def communicate(self):
        return b"current-user-output", b""


@pytest.mark.parametrize("executable", ["powershell.exe", "cmd.exe"])
@pytest.mark.asyncio
async def test_system_terminal_uses_active_user_and_preserves_details(monkeypatch, tmp_path, executable):
    import web_tools.runner as module

    runner, command = configured_runner(monkeypatch, tmp_path, executable,
                                        {"execution_context": "active_user", "profile_loaded": True})
    monkeypatch.setattr(module, "is_local_system", lambda: True)
    monkeypatch.setattr(module, "should_use_active_user_context", lambda: False)
    calls = []

    def active_user(argv, cwd, timeout, cancel_event):
        calls.append((argv, cwd, timeout, cancel_event))
        return UserProcessResult(0, "signed-in-user-output", "")

    monkeypatch.setattr(module, "run_command_as_active_user", active_user)

    async def reject_direct(*args, **kwargs):
        pytest.fail("A configured user terminal must not inherit SYSTEM")

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", reject_direct)
    result = await runner.run("terminal_workflow", {}, "identity-test", not_cancelled, discard_output)
    assert result.status == "success"
    assert result.stdout == "signed-in-user-output"
    assert calls[0][:3] == (command.command, tmp_path, 10)
    assert result.details == {"execution_context": "active_user", "profile_loaded": True,
                              "execution_context_used": "active_user"}
    assert "execution_context_used" not in command.details


@pytest.mark.parametrize("executable", ["wsl", "wsl.exe"])
@pytest.mark.asyncio
async def test_existing_wsl_active_user_dispatch_is_preserved(monkeypatch, tmp_path, executable):
    import web_tools.runner as module

    runner, _ = configured_runner(monkeypatch, tmp_path, executable)
    monkeypatch.setattr(module, "is_local_system", lambda: False)
    monkeypatch.setattr(module, "should_use_active_user_context", lambda: True)
    monkeypatch.setattr(module, "run_command_as_active_user",
                        lambda *args: UserProcessResult(0, "wsl-output", ""))
    result = await runner.run("wsl_workflow", {}, "identity-test", not_cancelled, discard_output)
    assert result.status == "success"
    assert result.stdout == "wsl-output"
    assert result.details["execution_context_used"] == "active_user"


@pytest.mark.asyncio
async def test_terminal_identity_failure_never_falls_back_to_system(monkeypatch, tmp_path):
    import web_tools.runner as module

    runner, _ = configured_runner(monkeypatch, tmp_path, "powershell.exe",
                                  {"execution_context": "active_user"})
    monkeypatch.setattr(module, "is_local_system", lambda: True)

    def unavailable(*args):
        raise RuntimeError("No signed-in user")

    monkeypatch.setattr(module, "run_command_as_active_user", unavailable)

    async def reject_direct(*args, **kwargs):
        pytest.fail("User-context failure must never fall back to SYSTEM")

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", reject_direct)
    with pytest.raises(RuntimeError, match="No signed-in user"):
        await runner.run("powershell_workflow", {}, "identity-test", not_cancelled, discard_output)


@pytest.mark.asyncio
async def test_existing_nonterminal_keeps_worker_environment(monkeypatch, tmp_path):
    import web_tools.runner as module

    runner, command = configured_runner(monkeypatch, tmp_path, "python.exe", {"notebook_output": "result"})
    command.environment = {"PYTHON_TEST_ENV": "preserved"}
    monkeypatch.setattr(module, "is_local_system", lambda: True)
    monkeypatch.setattr(module, "should_use_active_user_context", lambda: True)
    monkeypatch.setattr(module, "run_command_as_active_user",
                        lambda *args: pytest.fail("Python jobs must retain their worker context"))
    direct_calls = []

    async def direct(*argv, **kwargs):
        direct_calls.append((argv, kwargs))
        return CompletedProcess()

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", direct)
    result = await runner.run("python_script", {}, "identity-test", not_cancelled, discard_output)
    assert direct_calls[0][1]["env"] == command.environment
    assert result.details == command.details
    assert result.stdout == "current-user-output"


@pytest.mark.asyncio
async def test_current_user_terminal_runs_directly_and_preserves_details(monkeypatch, tmp_path):
    import web_tools.runner as module

    runner, command = configured_runner(monkeypatch, tmp_path, "powershell.exe",
                                        {"execution_context": "active_user", "profile_loaded": True})
    monkeypatch.setattr(module, "is_local_system", lambda: False)
    monkeypatch.setattr(module, "run_command_as_active_user",
                        lambda *args: pytest.fail("Already-user process must not duplicate a token"))

    async def direct(*args, **kwargs):
        return CompletedProcess()

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", direct)
    result = await runner.run("powershell_workflow", {}, "identity-test", not_cancelled, discard_output)
    assert result.status == "success"
    assert result.details == {**command.details, "execution_context_used": "current_process"}


@pytest.mark.parametrize("process_result, expected_status, expected_stderr", [
    (UserProcessResult(1, "partial", "failure"), "failed", "failure"),
    (UserProcessResult(-1, "partial", "", timed_out=True), "failed", "Task timed out"),
    (UserProcessResult(-1, "partial", "", cancelled=True), "cancelled", ""),
])
@pytest.mark.asyncio
async def test_active_user_outcomes_keep_status_and_metadata(monkeypatch, tmp_path, process_result,
                                                          expected_status, expected_stderr):
    import web_tools.runner as module

    runner, _ = configured_runner(monkeypatch, tmp_path, "cmd.exe", {"execution_context": "active_user"})
    monkeypatch.setattr(module, "is_local_system", lambda: True)
    monkeypatch.setattr(module, "run_command_as_active_user", lambda *args: process_result)
    result = await runner.run("cmd_workflow", {}, "identity-test", not_cancelled, discard_output)
    assert result.status == expected_status
    assert result.stderr == expected_stderr
    assert result.stdout == "partial"
    assert result.details["execution_context_used"] == "active_user"
