from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
async def direct_registry(isolated_settings, tmp_path):
    from web_canvas.service import CanvasService
    from web_memory.store import Database
    from web_mcp.tool_registry import ToolContext, ToolRegistry

    settings = replace(isolated_settings, orchestration_mode="chatgpt_direct", workspace_root=tmp_path,
                       allowed_workspaces=[tmp_path], task_poll_interval=0.2)
    db = Database(settings.database_path, settings.project_root / "08_web_data" / "schema.sql")
    await db.init()
    registry = ToolRegistry(ToolContext(settings, db, CanvasService(db), logging.getLogger("direct-registry-tests")))
    try:
        yield registry
    finally:
        await db.close()


async def test_sampling_and_dom_inference_are_disabled(direct_registry):
    session = SimpleNamespace(create_message=AsyncMock(side_effect=AssertionError("Sampling must not run")))
    ctx = SimpleNamespace(session=session)
    decision, reason = await direct_registry._autonomous_sample("Any goal", [], ctx)
    assert decision is None and "disabled" in reason
    session.create_message.assert_not_awaited()
    direct_registry.ctx.browser.list_tabs = AsyncMock(side_effect=AssertionError("Must not read ChatGPT tabs"))
    assert await direct_registry._infer_latest_chat_goal() is None
    assert await direct_registry._maybe_handoff_browser_goal("browser_read_page", {}, ctx) is None
    assert await direct_registry._maybe_handoff_runner_goal("Any goal", {}, ctx) is None


async def test_plain_file_and_browser_tools_never_call_handoff(direct_registry, tmp_path):
    (tmp_path / "one.txt").write_text("one", encoding="utf-8")
    direct_registry._maybe_handoff_browser_goal = AsyncMock(side_effect=AssertionError("Unexpected handoff"))
    direct_registry._autonomous_sample = AsyncMock(side_effect=AssertionError("Unexpected sampling"))
    direct_registry.ctx.browser.read_page = AsyncMock(return_value={"text": "Observed page", "tab_id": "11"})
    read = await direct_registry.invoke("read_file", {"path": "one.txt"})
    page = await direct_registry.invoke("browser_read_page", {"tab_id": "11"})
    assert read["content"] == "one"
    assert page["text"] == "Observed page"
    direct_registry._maybe_handoff_browser_goal.assert_not_awaited()
    direct_registry._autonomous_sample.assert_not_awaited()


async def test_browser_observation_does_not_claim_the_larger_goal_completed(direct_registry):
    page = {"url": "https://glycam.org/cb/download/example", "text": "Project completed Simulation finished normally Step 3",
            "interactive_elements": [{"id": "download-structure"}]}
    direct_registry.ctx.browser.read_page = AsyncMock(return_value=page)
    result = await direct_registry.invoke("browser_read_page", {"tab_id": "11"})
    assert result == page
    assert "completed" not in result and "do_not_reply" not in result


@pytest.mark.parametrize(("name", "arguments", "profile"), [
    ("run_python_script", {"code": "print('direct')"}, "python_script"),
    ("run_powershell_workflow", {"workflow": "custom", "commands": ["Write-Output 'direct'"]}, "powershell_workflow"),
    ("run_cmd_workflow", {"workflow": "custom", "commands": ["echo direct"]}, "cmd_workflow"),
    ("run_wsl_workflow", {"workflow": "production diagnostics", "commands": ["pwd"]}, "wsl_workflow"),
    ("run_terminal_workflow", {"workflow": "custom", "commands": ["echo direct"]}, "terminal_workflow"),
    ("run_project_build", {"build_type": "python"}, "project_build"),
])
async def test_ordinary_runners_queue_only_the_requested_operation(direct_registry, name, arguments, profile):
    direct_registry._monitor_task = AsyncMock(side_effect=AssertionError("Runner must return promptly"))
    direct_registry._autonomous_sample = AsyncMock(side_effect=AssertionError("Unexpected sampling"))
    direct_registry._maybe_handoff_runner_goal = AsyncMock(side_effect=AssertionError("Unexpected handoff"))
    result = await asyncio.wait_for(direct_registry.invoke(name, arguments), timeout=1)
    assert result["status"] == "queued" and result["terminal"] is False
    task = await direct_registry.tasks.get(result["task_id"])
    assert task["profile"] == profile
    if "commands" in arguments:
        assert task["payload"]["commands"] == arguments["commands"]
    assert result["suggested_next_tool"] == "task_status"
    assert "do_not_reply" not in result


async def test_natural_language_goal_returns_planning_required_without_task(direct_registry):
    result = await direct_registry.invoke("run_autonomous_workflow", {"goal": "Build a website and deploy it"})
    assert result["status"] == "needs_chatgpt_planning"
    assert result["completed"] is False
    assert await direct_registry.tasks.list() == []


async def test_explicit_file_batch_resolves_results_and_checks(direct_registry, tmp_path):
    result = await direct_registry.invoke("run_autonomous_workflow", {
        "goal": "Ignored by server planning", "workflow": {"steps": [
            {"id": "written", "action": "write_file", "args": {"path": "batch.txt", "content": "batch-ok"}},
            {"id": "verified", "action": "read_file", "args": {"path": "$steps.written.path"}},
        ]}, "acceptance": [{"type": "file_contains", "path": "batch.txt", "text": "batch-ok"}],
    })
    assert result["terminal"] is True and result["completed"] is True
    assert result["steps"][1]["result"]["content"] == "batch-ok"
    assert result["acceptance"][0]["passed"] is True
    assert (tmp_path / "batch.txt").read_text(encoding="utf-8") == "batch-ok"


async def test_mixed_batch_is_rejected_before_any_side_effect(direct_registry, tmp_path):
    with pytest.raises(ValueError, match="Split mixed"):
        await direct_registry.invoke("run_autonomous_workflow", {"goal": "Mixed", "workflow": {"steps": [
            {"action": "write_file", "args": {"path": "must-not-exist.txt", "content": "no"}},
            {"action": "python", "args": {"code": "print('must not run')"}},
        ]}})
    assert not (tmp_path / "must-not-exist.txt").exists()
    assert await direct_registry.tasks.list() == []


async def test_failure_is_returned_then_host_can_repair_and_verify(direct_registry):
    with pytest.raises(FileNotFoundError):
        await direct_registry.invoke("read_file", {"path": "repair.txt"})
    await direct_registry.invoke("write_file", {"path": "repair.txt", "content": "repaired"})
    result = await direct_registry.invoke("read_file", {"path": "repair.txt"})
    assert result["content"] == "repaired"


async def test_worker_result_is_real_and_pending_snapshot_is_prompt(direct_registry):
    from web_worker.service import WorkerService

    queued = await direct_registry.invoke("run_python_script", {"code": "import time; time.sleep(0.2); print('worker-evidence')"})
    task = await direct_registry.tasks.claim_next()
    worker = WorkerService(direct_registry.ctx.settings, direct_registry.ctx.db, direct_registry.ctx.logger)
    running = asyncio.create_task(worker._run_task(task))
    snapshot = await asyncio.wait_for(direct_registry.invoke("monitor_task", {"task_id": queued["task_id"]}), timeout=0.5)
    assert snapshot["status"] == "running" and snapshot["terminal"] is False
    await running
    final = await direct_registry.invoke("monitor_task", {"task_id": queued["task_id"]})
    assert final["status"] == "success" and final["terminal"] is True
    assert final["result"]["return_code"] == 0
    assert "worker-evidence" in final["stdout"]


async def test_failed_process_is_terminal_and_retains_return_code(direct_registry):
    from web_worker.service import WorkerService

    queued = await direct_registry.invoke("run_python_script", {"code": "raise SystemExit(9)"})
    task = await direct_registry.tasks.claim_next()
    await WorkerService(direct_registry.ctx.settings, direct_registry.ctx.db, direct_registry.ctx.logger)._run_task(task)
    result = await direct_registry.invoke("monitor_task", {"task_id": queued["task_id"]})
    assert result["status"] == "failed" and result["terminal"] is True
    assert result["result"]["return_code"] == 9
    assert result["retryable"] is True and result["error_code"]
    assert "do_not_reply" not in result


async def test_explicit_runner_batch_uses_existing_worker(direct_registry, tmp_path):
    from web_worker.service import WorkerService

    queued = await direct_registry.invoke("run_workflow", {"workflow": {"steps": [
        {"action": "python", "args": {"code": "from pathlib import Path; Path('worker-batch.txt').write_text('done', encoding='utf-8')"}},
    ], "final_checks": [{"type": "file_contains", "path": "worker-batch.txt", "text": "done"}]}})
    assert queued["status"] == "queued" and not (tmp_path / "worker-batch.txt").exists()
    task = await direct_registry.tasks.claim_next()
    await WorkerService(direct_registry.ctx.settings, direct_registry.ctx.db, direct_registry.ctx.logger)._run_task(task)
    final = await direct_registry.invoke("monitor_task", {"task_id": queued["task_id"]})
    assert final["status"] == "success" and final["completed"] is True
    assert (tmp_path / "worker-batch.txt").read_text(encoding="utf-8") == "done"


async def test_md_task_does_not_cancel_an_unrelated_submitted_task(direct_registry):
    first = await direct_registry._create_task("md_simulation", {"args": ["explicit"]})
    second = await direct_registry._create_task("production_md", {"system_dir": "explicit"})
    assert first["task_id"] != second["task_id"]
    assert (await direct_registry.tasks.get(first["task_id"]))["status"] == "queued"


async def test_missing_task_is_a_real_diagnostic_error(direct_registry):
    with pytest.raises(FileNotFoundError):
        await direct_registry.invoke("monitor_task", {"task_id": "missing-task"})