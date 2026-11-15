"""Task ownership and cancellation regressions, using only isolated databases/fakes."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from web_tools.profiles import RunnerCommand
from web_tools.runner import ProfileRunner
from web_tools.windows_user_process import UserProcessResult
from web_worker.lease import WorkerAlreadyRunning, WorkerLease


def _child_lease(path: Path, *, crash: bool = False):
    module_root = Path(__file__).resolve().parents[1] / "03_web_worker"
    script = (
        "import os,sys;from pathlib import Path;"
        f"sys.path.insert(0,{str(module_root)!r});"
        "from web_worker.lease import WorkerLease,WorkerAlreadyRunning;"
        "lease=WorkerLease(Path(sys.argv[1]));\n"
        "try:lease.__enter__()\n"
        "except WorkerAlreadyRunning:sys.exit(3)\n"
        + ("os._exit(0)\n" if crash else "lease.__exit__(None,None,None)\n")
    )
    return subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode


def test_execution_lease_is_cross_process_and_released_after_close_or_crash(tmp_path):
    database = tmp_path / "tasks.sqlite3"
    with WorkerLease(database):
        assert _child_lease(database) == 3
        with pytest.raises(WorkerAlreadyRunning):
            with WorkerLease(database):
                pytest.fail("An active lease was stolen")
    assert _child_lease(database) == 0
    assert _child_lease(database, crash=True) == 0
    with WorkerLease(database):
        pass


def test_execution_lease_releases_on_exception(tmp_path):
    database = tmp_path / "tasks.sqlite3"
    with pytest.raises(ValueError):
        with WorkerLease(database):
            raise ValueError("expected")
    with WorkerLease(database):
        pass


async def test_duplicate_worker_fails_before_orphan_recovery(isolated_settings):
    from web_memory.store import Database
    from web_worker.service import WorkerService

    calls = []
    db = Database(isolated_settings.database_path, isolated_settings.project_root / "08_web_data/schema.sql")
    async def forbidden_init():
        calls.append("init")
        pytest.fail("Duplicate worker reached database startup/orphan recovery")
    db.init = forbidden_init
    service = WorkerService(isolated_settings, db, logging.getLogger("worker-lease-test"))
    with WorkerLease(db.path):
        with pytest.raises(WorkerAlreadyRunning):
            await service.run_forever()
    assert not calls


async def test_worker_cancel_records_terminal_state_after_runner_cleanup(isolated_settings, monkeypatch):
    from web_memory.store import Database
    from web_worker.service import WorkerService

    db = Database(isolated_settings.database_path, isolated_settings.project_root / "08_web_data/schema.sql")
    await db.init()
    try:
        service = WorkerService(isolated_settings, db, logging.getLogger("worker-cancel-test"))
        task = await service.tasks.create("python_script", {})
        started, cleaned = asyncio.Event(), asyncio.Event()
        async def fake_run(task):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        monkeypatch.setattr(service, "_run_task", fake_run)
        running = asyncio.create_task(service.run_forever())
        await started.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert cleaned.is_set()
        assert (await service.tasks.get(task["id"]))["status"] == "cancelled"
        assert service.current_task_id is None
        with WorkerLease(db.path):
            pass
    finally:
        await db.close()


async def test_worker_stop_requests_cooperative_cancel_before_leaving_loop(isolated_settings, monkeypatch):
    from web_memory.store import Database
    from web_tools.runner import RunnerResult
    from web_worker.service import WorkerService

    db = Database(isolated_settings.database_path, isolated_settings.project_root / "08_web_data/schema.sql")
    await db.init()
    try:
        service = WorkerService(isolated_settings, db, logging.getLogger("worker-stop-test"))
        task = await service.tasks.create("python_script", {})
        async def fake_runner(profile, payload, task_id, is_cancelled, on_output):
            service.stop()
            assert await is_cancelled()
            return RunnerResult("cancelled", -1, "", "", 0.01)
        monkeypatch.setattr(service.runner, "run", fake_runner)
        await service.run_forever()
        assert (await service.tasks.get(task["id"]))["status"] == "cancelled"
    finally:
        await db.close()


async def test_cancellation_after_queued_selection_never_returns_a_phantom_claim(isolated_settings, monkeypatch):
    from web_memory.store import Database
    from web_memory.task_repository import TaskRepository

    db = Database(isolated_settings.database_path, isolated_settings.project_root / "08_web_data/schema.sql")
    await db.init()
    try:
        tasks = TaskRepository(db)
        task = await tasks.create("python_script", {"code": "must not run"})
        fetchone = db.fetchone
        intercepted = False
        async def race(sql, parameters=()):
            nonlocal intercepted
            row = await fetchone(sql, parameters)
            if "WHERE status='queued'" in sql and not intercepted:
                intercepted = True
                await tasks.request_cancel(task["id"])
            return row
        monkeypatch.setattr(db, "fetchone", race)
        assert await tasks.claim_next() is None
        result = await tasks.get(task["id"])
        assert result["status"] == "cancelled" and result["cancel_requested"]
        assert result["started_at"] is None
    finally:
        await db.close()


async def test_cancel_after_claim_remains_running_until_execution_cleanup_finishes(isolated_settings, monkeypatch):
    from web_memory.store import Database
    from web_memory.task_repository import TaskRepository

    db = Database(isolated_settings.database_path, isolated_settings.project_root / "08_web_data/schema.sql")
    await db.init()
    try:
        tasks = TaskRepository(db)
        task = await tasks.create("python_script", {"code": "must not run"})
        execute = db.execute
        raced = False
        async def claim_before_cancel(sql, parameters=()):
            nonlocal raced
            if sql.startswith("UPDATE tasks SET cancel_requested=1,") and not raced:
                raced = True
                assert (await tasks.claim_next())["id"] == task["id"]
            return await execute(sql, parameters)
        monkeypatch.setattr(db, "execute", claim_before_cancel)
        cancelled = await tasks.request_cancel(task["id"])
        assert cancelled["status"] == "running" and cancelled["cancel_requested"]
        assert cancelled["finished_at"] is None
        await tasks.finish(task["id"], "cancelled")
        assert (await tasks.get(task["id"]))["status"] == "cancelled"
        assert await tasks.request_cancel("missing") is None
    finally:
        await db.close()


async def test_cancelled_claim_rolls_back_transaction(isolated_settings, monkeypatch):
    from web_memory.store import Database
    from web_memory.task_repository import TaskRepository

    db = Database(isolated_settings.database_path, isolated_settings.project_root / "08_web_data/schema.sql")
    await db.init()
    try:
        tasks = TaskRepository(db)
        await tasks.create("python_script", {})
        fetchone = db.fetchone
        async def cancel_selection(sql, parameters=()):
            if "WHERE status='queued'" in sql:
                raise asyncio.CancelledError()
            return await fetchone(sql, parameters)
        monkeypatch.setattr(db, "fetchone", cancel_selection)
        with pytest.raises(asyncio.CancelledError):
            await tasks.claim_next()
        # A leftover transaction would make this next BEGIN fail immediately.
        await db.begin_immediate()
        await db.rollback()
    finally:
        await db.close()


async def _never_cancelled():
    return False


async def _discard(*args):
    pass


async def test_external_runner_cancel_stops_exact_owned_process_and_drains_output(tmp_path, monkeypatch):
    import web_tools.runner as module

    started, finished = asyncio.Event(), asyncio.Event()
    calls = []
    class FakeProcess:
        pid = 987654
        returncode = None
        async def communicate(self):
            started.set()
            await finished.wait()
            return b"partial", b""
    process = FakeProcess()
    runner = ProfileRunner(SimpleNamespace())
    monkeypatch.setattr(runner, "build", lambda *a: RunnerCommand(["python.exe"], tmp_path, 100, "test", {}))
    async def create(*args, **kwargs): return process
    async def stop(target, *, tree, expected_create_time):
        assert target is process
        calls.append((target.pid, tree, expected_create_time))
        target.returncode = -15
        finished.set()
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(module, "_process_create_time", lambda pid: 123.5)
    monkeypatch.setattr(runner, "_stop_process", stop)
    running = asyncio.create_task(runner.run("python_script", {}, "cancel-task", _never_cancelled, _discard))
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert calls == [(987654, True, 123.5)] and finished.is_set()


async def test_external_active_user_cancel_waits_for_native_cleanup(tmp_path, monkeypatch):
    import web_tools.runner as module

    started, stopped = threading.Event(), threading.Event()
    def fake_user(argv, cwd, timeout, cancel_event):
        started.set()
        assert cancel_event.wait(5)
        stopped.set()
        return UserProcessResult(-1, "partial", "", cancelled=True)
    runner = ProfileRunner(SimpleNamespace())
    command = RunnerCommand(["cmd.exe"], tmp_path, 100, "test", {"execution_context": "active_user"})
    monkeypatch.setattr(runner, "build", lambda *a: command)
    monkeypatch.setattr(module, "is_local_system", lambda: True)
    monkeypatch.setattr(module, "run_command_as_active_user", fake_user)
    running = asyncio.create_task(runner.run("cmd_workflow", {}, "cancel-task", _never_cancelled, _discard))
    assert await asyncio.to_thread(started.wait, 3)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert stopped.is_set()


async def test_cleanup_failure_does_not_replace_original_runner_cancellation(tmp_path, monkeypatch):
    import web_tools.runner as module

    started = asyncio.Event()
    class FakeProcess:
        pid = 987654
        returncode = None
        async def communicate(self):
            started.set()
            await asyncio.Event().wait()
    runner = ProfileRunner(SimpleNamespace())
    monkeypatch.setattr(runner, "build", lambda *a: RunnerCommand(["python.exe"], tmp_path, 100, "test", {}))
    async def create(*args, **kwargs): return FakeProcess()
    async def cleanup_fails(*args, **kwargs): raise RuntimeError("cleanup failure")
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(module, "_process_create_time", lambda pid: 123.5)
    monkeypatch.setattr(runner, "_stop_process", cleanup_fails)
    running = asyncio.create_task(runner.run("python_script", {}, "cancel-task", _never_cancelled, _discard))
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running


def test_owned_tree_cleanup_refuses_recycled_or_unknown_root(monkeypatch):
    import psutil
    from web_tools.windows_user_process import _terminate_owned_process_tree

    class Recycled:
        def create_time(self): return 999.0
        def children(self, **kwargs): pytest.fail("Must not inspect a recycled root")
        def terminate(self): pytest.fail("Must not terminate a recycled root")
    monkeypatch.setattr(psutil, "Process", lambda pid: Recycled())
    assert not _terminate_owned_process_tree(123, None)
    assert not _terminate_owned_process_tree(123, 1.0)


def test_owned_tree_cleanup_never_targets_unrelated_processes(monkeypatch):
    import psutil
    from web_tools.windows_user_process import _terminate_owned_process_tree

    events = []
    class Process:
        def __init__(self, pid): self.pid = pid
        def create_time(self): return 10.0
        def children(self, recursive):
            assert recursive and self.pid == 100
            return [Process(101)]
        def terminate(self): events.append(self.pid)
    monkeypatch.setattr(psutil, "Process", Process)
    monkeypatch.setattr(psutil, "wait_procs", lambda processes, timeout: (processes, []))
    assert _terminate_owned_process_tree(100, 10.0)
    assert events == [101, 100]
