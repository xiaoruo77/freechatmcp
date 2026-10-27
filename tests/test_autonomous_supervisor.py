from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace


class _SamplingSession:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls = 0

    async def create_message(self, *_args, **_kwargs):
        self.calls += 1
        return SimpleNamespace(content=[SimpleNamespace(text=self.responses.pop(0))])


class _SamplingContext:
    def __init__(self, responses: list[str]):
        self.session = _SamplingSession(responses)
        self.request_id = "autonomous-test-request"
        self.progress: list[str] = []

    async def info(self, *_args, **_kwargs):
        return None

    async def report_progress(self, _current, _total=None, message=None):
        if message:
            self.progress.append(str(message))


class _HangingSamplingSession:
    async def create_message(self, *_args, **_kwargs):
        await asyncio.sleep(60)


def test_terminal_browser_handoff_compacts_internal_transcript():
    from web_mcp.tool_registry import _compact_terminal_result

    result = _compact_terminal_result(
        {
            "task_id": "terminal-compaction",
            "profile": "workflow",
            "status": "success",
            "terminal": True,
            "completed": True,
            "success": True,
            "workflow_status": "terminal_success",
            "answer": "已验证完成：结果已交付。",
            "transcript": [
                {
                    "cycle": index,
                    "kind": "action",
                    "action": "browser_read_page",
                    "result": {
                        "task_id": "terminal-compaction",
                        "status": "success",
                        "completed": True,
                    },
                    "page_text": "x" * 20_000,
                }
                for index in range(20)
            ],
        }
    )

    assert "transcript" not in result
    assert result["terminal_result_compact"] is True
    assert result["trace_summary"]
    assert result["answer"]


class _HangingSamplingContext:
    def __init__(self):
        self.session = _HangingSamplingSession()
        self.request_id = "hanging-sampling-test"


def _make_registry(isolated_settings):
    from web_canvas.service import CanvasService
    from web_logs.logger import configure_logging
    from web_memory.store import Database
    from web_mcp.tool_registry import ToolContext, ToolRegistry

    db = Database(isolated_settings.database_path, isolated_settings.project_root / "08_web_data" / "schema.sql")
    logger = configure_logging(isolated_settings.log_root, "WARNING")
    registry = ToolRegistry(ToolContext(isolated_settings, db, CanvasService(db), logger))
    return db, registry


def test_sampling_decision_accepts_fenced_json():
    from web_mcp.tool_registry import _sampling_decision

    decision = _sampling_decision('diagnosis complete\n```json\n{"action":{"name":"read_file","arguments":{"path":"x"}}}\n```')

    assert decision is not None
    assert decision["steps"][0]["action"] == "read_file"
    assert decision["steps"][0]["args"]["path"] == "x"


def test_terminal_result_exposes_verified_artifact_details():
    from web_mcp.tool_registry import _enrich_terminal_result

    result = _enrich_terminal_result(
        {
            "success": True,
            "completed": True,
            "terminal": True,
            "answer": "Workflow completed successfully.",
            "transcript": [
                {
                    "result": {
                        "glycan_input": "D:\\work\\six-sugar.zip",
                        "sequence": "DGlcpb1-4DGlcpb1-OH",
                        "browser_download_path": "D:\\downloads\\six-sugar.zip",
                    }
                },
                {
                    "result": {
                        "task_id": "task-test",
                        "status": "success",
                        "result": {"return_code": 0, "duration_seconds": 12.5, "output_files": ["D:\\out\\result.json"]},
                        "stdout": "PIPELINE_SUCCESS: D:/work/run\nEM_LOG: D:/work/em.log\nNVT_LOG: D:/work/nvt.log\nNPT_LOG: D:/work/npt.log",
                    }
                },
            ],
        }
    )

    assert "D:\\work\\six-sugar.zip" in result["answer"]
    assert "task-test" in result["answer"]
    assert "PIPELINE_SUCCESS" in result["answer"]
    assert "EM_LOG" in result["answer"]
    assert "NVT_LOG" in result["answer"]
    assert "NPT_LOG" in result["answer"]


def test_autonomous_supervisor_repairs_inside_one_call(isolated_settings):
    probe = isolated_settings.project_root / "tests" / "autonomous_supervisor_probe.txt"
    probe.unlink(missing_ok=True)
    db, registry = _make_registry(isolated_settings)

    async def run():
        await db.init()
        connected = _SamplingContext(
            [
                '{"action":{"name":"read_file","arguments":{"path":"tests/autonomous_supervisor_probe.txt"}}}',
                '{"action":{"name":"write_file","arguments":{"path":"tests/autonomous_supervisor_probe.txt","content":"autonomous-ok","create_dirs":true}}}',
                '{"action":{"name":"read_file","arguments":{"path":"tests/autonomous_supervisor_probe.txt"}}}',
                '{"done":true,"answer":"verified"}',
            ]
        )
        try:
            result = await registry._run_autonomous_workflow(
                "Create and verify the probe file, repairing any missing-file error internally.",
                acceptance=[{"type": "file_contains", "path": "tests/autonomous_supervisor_probe.txt", "text": "autonomous-ok"}],
                max_cycles=12,
                timeout_seconds=30,
                ctx=connected,
            )
            return result, connected
        finally:
            await db.close()

    try:
        result, connected = asyncio.run(run())
        assert result["terminal"] is True
        assert result["completed"] is True
        assert result["workflow_status"] == "terminal_success"
        assert all(item["passed"] for item in result["acceptance"])
        assert any(item.get("kind") == "action_error" for item in result["transcript"])
        assert connected.session.calls == 4
        assert probe.read_text(encoding="utf-8") == "autonomous-ok"
        assert "continue_required" not in result
    finally:
        probe.unlink(missing_ok=True)


def test_initial_workflow_can_finish_without_an_extra_chat_turn(isolated_settings):
    probe = isolated_settings.project_root / "tests" / "autonomous_initial_probe.txt"
    probe.unlink(missing_ok=True)
    db, registry = _make_registry(isolated_settings)

    async def run():
        await db.init()
        try:
            return await registry._run_autonomous_workflow(
                "Write and verify the initial probe.",
                workflow={
                    "steps": [
                        {
                            "action": "write_file",
                            "args": {
                                "path": "tests/autonomous_initial_probe.txt",
                                "content": "initial-ok",
                                "create_dirs": True,
                            },
                        }
                    ],
                    "acceptance": [
                        {"type": "file_contains", "path": "tests/autonomous_initial_probe.txt", "text": "initial-ok"}
                    ],
                },
                timeout_seconds=30,
                ctx=None,
            )
        finally:
            await db.close()

    try:
        result = asyncio.run(run())
        assert result["terminal"] is True
        assert result["completed"] is True
        assert result["workflow_status"] == "terminal_success"
        assert probe.read_text(encoding="utf-8") == "initial-ok"
    finally:
        probe.unlink(missing_ok=True)


def test_declared_recovery_branch_finishes_without_sampling(isolated_settings):
    probe = isolated_settings.project_root / "tests" / "autonomous_declared_recovery_probe.txt"
    probe.unlink(missing_ok=True)
    db, registry = _make_registry(isolated_settings)

    async def run():
        await db.init()
        try:
            return await registry._run_autonomous_workflow(
                "Read a probe, repair the missing file, and verify it.",
                workflow={
                    "steps": [
                        {
                            "id": "read-before-create",
                            "action": "read_file",
                            "args": {"path": "tests/autonomous_declared_recovery_probe.txt"},
                            "on_error": [
                                {
                                    "action": "write_file",
                                    "args": {
                                        "path": "tests/autonomous_declared_recovery_probe.txt",
                                        "content": "declared-recovery-ok",
                                        "create_dirs": True,
                                    },
                                }
                            ],
                            "max_retries": 1,
                        }
                    ],
                    "acceptance": [
                        {
                            "type": "file_contains",
                            "path": "tests/autonomous_declared_recovery_probe.txt",
                            "text": "declared-recovery-ok",
                        }
                    ],
                },
                timeout_seconds=30,
                ctx=None,
            )
        finally:
            await db.close()

    try:
        result = asyncio.run(run())
        assert result["terminal"] is True
        assert result["completed"] is True
        assert result["workflow_status"] == "terminal_success"
        assert probe.read_text(encoding="utf-8") == "declared-recovery-ok"
        assert any(item.get("kind") == "recovery_branch_enqueued" for item in result["transcript"])
    finally:
        probe.unlink(missing_ok=True)


def test_sampling_timeout_is_bounded(isolated_settings, monkeypatch):
    db, registry = _make_registry(isolated_settings)

    async def run():
        await db.init()
        try:
            return await registry._autonomous_sample(
                "bounded sampling test",
                [],
                _HangingSamplingContext(),
            )
        finally:
            await db.close()

    monkeypatch.setattr("web_mcp.tool_registry.AUTONOMOUS_SAMPLING_TIMEOUT_SECONDS", 0.01)
    decision, error = asyncio.run(run())
    assert decision is None
    assert error is not None
    assert "timed out" in error


def test_sampling_timeout_after_recovery_is_nonterminal(isolated_settings, monkeypatch):
    """A failed internal callback must not become a successful chat answer."""

    db, registry = _make_registry(isolated_settings)

    async def fake_self_check(*_args, **_kwargs):
        return {
            "status": "failed",
            "terminal": False,
            "completed": False,
            "final_answer_allowed": False,
            "repair_required": True,
            "recoverable_error": True,
            "workflow_status": "self_check_repair_required",
            "error": "unsupported action needs a changed repair",
            "self_check_completed": True,
        }

    monkeypatch.setattr(registry, "_self_check", fake_self_check)
    monkeypatch.setattr("web_mcp.tool_registry.AUTONOMOUS_SAMPLING_TIMEOUT_SECONDS", 0.01)

    async def run():
        await db.init()
        try:
            return await registry._run_autonomous_workflow(
                "Perform the task and repair any failed action internally.",
                workflow={
                    "steps": [
                        # Deliberately unsupported: it exercises the exact
                        # post-diagnosis sampling boundary without mutating
                        # the workspace.
                        {"action": "check_file", "args": {}}
                    ]
                },
                timeout_seconds=30,
                ctx=_HangingSamplingContext(),
            )
        finally:
            await db.close()

    result = asyncio.run(run())
    assert result["completed"] is False
    assert result["terminal"] is False
    assert result["user_visible_final"] is False
    assert result["final_answer_allowed"] is False
    assert result["repair_required"] is True
    assert result["workflow_status"] == "repair_required"
    assert result["next_action"] == "self_check"


def test_done_cannot_bypass_an_unresolved_failure(isolated_settings):
    probe = isolated_settings.project_root / "tests" / "autonomous_premature_done_probe.txt"
    probe.unlink(missing_ok=True)
    db, registry = _make_registry(isolated_settings)

    async def run():
        await db.init()
        connected = _SamplingContext(
            [
                '{"action":{"name":"read_file","arguments":{"path":"tests/autonomous_premature_done_probe.txt"}}}',
                '{"done":true,"answer":"it is done"}',
                '{"action":{"name":"write_file","arguments":{"path":"tests/autonomous_premature_done_probe.txt","content":"repaired","create_dirs":true}}}',
                '{"action":{"name":"read_file","arguments":{"path":"tests/autonomous_premature_done_probe.txt"}}}',
                '{"done":true,"answer":"verified"}',
            ]
        )
        try:
            result = await registry._run_autonomous_workflow(
                "Create and verify the probe, and repair any failure internally.",
                acceptance=[{"type": "file_contains", "path": "tests/autonomous_premature_done_probe.txt", "text": "repaired"}],
                max_cycles=12,
                timeout_seconds=30,
                ctx=connected,
            )
            return result, connected
        finally:
            await db.close()

    try:
        result, connected = asyncio.run(run())
        assert result["workflow_status"] == "terminal_success"
        assert result["completed"] is True
        assert any(item.get("kind") == "premature_done_rejected" for item in result["transcript"])
        assert connected.session.calls == 5
        assert probe.read_text(encoding="utf-8") == "repaired"
    finally:
        probe.unlink(missing_ok=True)


def test_md_goal_requires_verified_md_runner_before_done(isolated_settings, monkeypatch):
    db, registry = _make_registry(isolated_settings)
    calls: list[str] = []

    async def fake_execute(raw_step, _context):
        action = str(raw_step.get("action"))
        calls.append(action)
        if len(calls) == 1:
            return action, dict(raw_step.get("args") or {}), {
                "status": "failed",
                "completed": False,
                "terminal": False,
                "repair_required": True,
                "recoverable_error": True,
                "error": "GLYCAM directory does not exist",
            }
        return action, dict(raw_step.get("args") or {}), {
            "status": "success",
            "completed": True,
            "terminal": True,
            "workflow_status": "terminal_success",
            "terminal_status": "success",
        }

    monkeypatch.setattr(registry, "_autonomous_execute_action", fake_execute)

    async def run():
        await db.init()
        connected = _SamplingContext(
            [
                '{"action":{"name":"md_simulation","arguments":{"args":["--glycan-dir","real-build"]}}}',
                '{"done":true,"answer":"MD complete"}',
                '{"action":{"name":"md_simulation","arguments":{"args":["--glycan-dir","real-build-repaired"]}}}',
                '{"done":true,"answer":"MD complete"}',
            ]
        )
        try:
            result = await registry._run_autonomous_workflow(
                "用 GLYCAM 构建六糖并完成 GROMACS EM/NVT/NPT 分子动力学。",
                max_cycles=10,
                timeout_seconds=30,
                ctx=connected,
            )
            return result, connected
        finally:
            await db.close()

    result, connected = asyncio.run(run())
    assert result["workflow_status"] == "terminal_success"
    assert result["completed"] is True
    assert calls == ["md_simulation", "self_check", "md_simulation"]
    # The server now closes directly after verified md_simulation success;
    # an extra sampled "done" turn is no longer a continuation boundary.
    assert connected.session.calls == 3
    assert any(item.get("kind") == "premature_done_rejected" for item in result["transcript"])


def test_final_production_stage_is_enqueued_after_verified_md(isolated_settings, monkeypatch):
    db, registry = _make_registry(isolated_settings)
    calls: list[str] = []

    async def fake_execute(raw_step, _context):
        action = str(raw_step.get("action"))
        calls.append(action)
        if action == "md_simulation":
            return action, dict(raw_step.get("args") or {}), {
                "status": "success",
                "completed": False,
                "terminal": False,
                "user_visible_final": False,
                "do_not_reply": True,
                "final_answer_allowed": False,
                "repair_required": True,
                "recoverable_error": True,
                "workflow_status": "repair_required",
                "error_class": "md_setup_complete_production_required",
                "production_required": True,
                "production_duration_seconds": 300,
                "system_dir": "/mnt/d/work/GLYCAM_system_current",
                "target_root": "/mnt/d/work",
                "stdout": "PIPELINE_SUCCESS: /mnt/d/work/GLYCAM_system_current",
            }
        if action == "production_md":
            return action, dict(raw_step.get("args") or {}), {
                "status": "success",
                "completed": True,
                "terminal": True,
                "workflow_status": "terminal_success",
                "terminal_status": "success",
            }
        raise AssertionError(action)

    monkeypatch.setattr(registry, "_autonomous_execute_action", fake_execute)

    async def run():
        await db.init()
        connected = _SamplingContext(
            [
                '{"action":{"name":"md_simulation","arguments":{"args":["--glycan-dir","current"]}}}',
            ]
        )
        try:
            result = await registry._run_autonomous_workflow(
                "Build a GLYCAM glycan, run EM/NVT/NPT, then run the 5-minute final simulation.",
                max_cycles=8,
                timeout_seconds=30,
                ctx=connected,
            )
            return result, connected
        finally:
            await db.close()

    result, connected = asyncio.run(run())
    assert result["workflow_status"] == "terminal_success"
    assert result["completed"] is True
    assert calls == ["md_simulation", "production_md"]
    assert any(item.get("kind") == "production_stage_enqueued" for item in result["transcript"])
    assert connected.session.calls == 1


def test_terminal_production_from_monitor_latches_workflow(isolated_settings, monkeypatch):
    """A late monitor result must close the goal and discard stale browser retries."""

    db, registry = _make_registry(isolated_settings)
    calls: list[str] = []

    async def fake_execute(raw_step, _context):
        action = str(raw_step.get("action"))
        calls.append(action)
        if action == "md_simulation":
            return action, dict(raw_step.get("args") or {}), {
                "success": True,
                "completed": True,
                "terminal": True,
                "workflow_status": "terminal_success",
                "system_dir": "D:\\work\\GLYCAM_system_current",
                "target_root": "D:\\work",
            }
        if action == "production_md":
            return action, dict(raw_step.get("args") or {}), {
                "task_id": "production-child",
                "status": "running",
                "completed": False,
                "terminal": False,
                "detached": True,
                "continue_required": True,
                "user_visible_final": False,
                "final_answer_allowed": False,
            }
        if action == "self_check":
            return action, dict(raw_step.get("args") or {}), {
                "status": "success",
                "completed": True,
                "terminal": True,
                "workflow_status": "terminal_success",
                "self_check_completed": True,
            }
        if action == "monitor_task":
            return action, dict(raw_step.get("args") or {}), {
                "task_id": "production-child",
                "profile": "production_md",
                "status": "success",
                "success": True,
                "completed": True,
                "terminal": True,
                "workflow_status": "terminal_success",
                "terminal_status": "success",
            }
        raise AssertionError(action)

    monkeypatch.setattr(registry, "_autonomous_execute_action", fake_execute)

    async def run():
        await db.init()
        connected = _SamplingContext(
            [
                '{"action":{"name":"md_simulation","arguments":{}}}',
                '{"action":{"name":"monitor_task","arguments":{"task_id":"production-child"}}}',
            ]
        )
        try:
            return await registry._run_autonomous_workflow(
                "Run GLYCAM EM/NVT/NPT and then a 5-minute final production MD.",
                max_cycles=10,
                timeout_seconds=30,
                ctx=connected,
            ), connected
        finally:
            await db.close()

    result, connected = asyncio.run(run())
    assert result["workflow_status"] == "terminal_success"
    assert result["completed"] is True
    assert result["terminal_proof"]["profile"] == "production_md"
    assert calls == ["md_simulation", "production_md", "self_check", "monitor_task"]
    assert connected.session.calls == 2


def test_md_goal_retries_browser_build_without_sampling(isolated_settings, monkeypatch):
    """A transient GLYCAM bridge error must not stop a no-sampling client."""

    db, registry = _make_registry(isolated_settings)
    calls: list[str] = []

    async def fake_execute(raw_step, _context):
        action = str(raw_step.get("action"))
        calls.append(action)
        if action == "browser_build_glycam" and calls.count(action) == 1:
            return action, dict(raw_step.get("args") or {}), {
                "success": False,
                "completed": False,
                "terminal": False,
                "repair_required": True,
                "recoverable_error": True,
                "error_class": "glycam_browser_action_failed",
                "error": "transient bridge page-type error",
            }
        if action == "browser_build_glycam":
            return action, dict(raw_step.get("args") or {}), {
                "success": True,
                "completed": True,
                "terminal": True,
                "workflow_status": "terminal_success",
                "glycam_sequence": "DGlcpb1-4DGlcpb1-4DGlcpb1-4DGlcpb1-4DGlcpb1-4DGlcpb1-OH",
                "glycan_input": "D:\\work\\six-sugar.zip",
                "target_root": "D:\\work",
            }
        if action == "self_check":
            return action, dict(raw_step.get("args") or {}), {
                "status": "recoverable_error",
                "terminal": False,
                "completed": False,
                "repair_required": True,
                "recoverable_error": True,
                "self_check_completed": True,
            }
        return action, dict(raw_step.get("args") or {}), {
            "success": True,
            "completed": True,
            "terminal": True,
            "workflow_status": "terminal_success",
            "terminal_status": "success",
        }

    monkeypatch.setattr(registry, "_autonomous_execute_action", fake_execute)

    async def run():
        await db.init()
        try:
            return await registry._run_autonomous_workflow(
                "打开 GLYCAM 随机构建一个六糖，并完成 GROMACS EM/NVT/NPT 分子动力学。",
                max_cycles=12,
                timeout_seconds=30,
                ctx=None,
            )
        finally:
            await db.close()

    result = asyncio.run(run())
    assert result["workflow_status"] == "terminal_success"
    assert result["completed"] is True
    assert calls == ["browser_build_glycam", "self_check", "browser_build_glycam", "md_simulation"]
    assert any(item.get("kind") == "deterministic_recovery_enqueued" for item in result["transcript"])


def test_md_preflight_requires_verified_browser_build_and_matching_sugar_count(isolated_settings):
    db, registry = _make_registry(isolated_settings)
    build_dir = isolated_settings.project_root / "tests" / "md_preflight_build"
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "info.json").write_text(
        '{"indexOrderedSequence":"DGlcpA1-4DGlcpA1-4DGlcpA1-4DGlcpA1-OH"}',
        encoding="utf-8",
    )
    try:
        missing_browser = registry._autonomous_md_preflight(
            {"args": ["--glycan-dir", str(build_dir)]},
            {"__requires_md": True, "__glycam_browser_verified": False, "goal": "构建六糖并跑 EM/NVT/NPT"},
        )
        assert missing_browser is not None
        assert missing_browser["error_class"] == "glycam_build_not_verified"

        wrong_count = registry._autonomous_md_preflight(
            {"args": ["--glycan-dir", str(build_dir)]},
            {
                "__requires_md": True,
                "__glycam_browser_verified": True,
                "__glycam_downloads": [
                    {
                        "id": 1,
                        "state": "complete",
                        "file_path": str(build_dir / "build.zip"),
                        "end_time": datetime.now(timezone.utc).isoformat(),
                    }
                ],
                "__autonomous_started_epoch": datetime.now(timezone.utc).timestamp() - 1,
                "goal": "构建六糖并跑 EM/NVT/NPT",
            },
        )
        assert wrong_count is not None
        assert wrong_count["error_class"] == "input_identity_or_cardinality"
        assert wrong_count["diagnostic_evidence"]["actual_sugars"] == 4
    finally:
        for child in build_dir.iterdir():
            child.unlink()
        build_dir.rmdir()

    async def close_db():
        await db.close()

    asyncio.run(close_db())


def test_sampled_done_requires_a_generic_acceptance_contract(isolated_settings):
    probe = isolated_settings.project_root / "tests" / "autonomous_acceptance_contract_probe.txt"
    probe.unlink(missing_ok=True)
    db, registry = _make_registry(isolated_settings)

    async def run():
        await db.init()
        connected = _SamplingContext(
            [
                '{"action":{"name":"write_file","arguments":{"path":"tests/autonomous_acceptance_contract_probe.txt","content":"contract-ok","create_dirs":true}}}',
                '{"done":true,"answer":"premature"}',
                '{"done":true,"answer":"verified","acceptance":[{"type":"file_contains","path":"tests/autonomous_acceptance_contract_probe.txt","text":"contract-ok"}]}',
            ]
        )
        try:
            result = await registry._run_autonomous_workflow(
                "Create the probe and verify the delivered content.",
                max_cycles=8,
                timeout_seconds=30,
                ctx=connected,
            )
            return result, connected
        finally:
            await db.close()

    try:
        result, connected = asyncio.run(run())
        assert result["workflow_status"] == "terminal_success"
        assert result["completed"] is True
        assert connected.session.calls == 3
        assert any(
            item.get("kind") == "premature_done_rejected" and "acceptance" in str(item.get("reason"))
            for item in result["transcript"]
        )
    finally:
        probe.unlink(missing_ok=True)


def test_failure_injects_diagnosis_and_blocks_an_identical_retry(isolated_settings):
    probe = isolated_settings.project_root / "tests" / "autonomous_repeat_block_probe.txt"
    probe.unlink(missing_ok=True)
    db, registry = _make_registry(isolated_settings)

    async def run():
        await db.init()
        connected = _SamplingContext(
            [
                '{"action":{"name":"read_file","arguments":{"path":"tests/autonomous_repeat_block_probe.txt"}}}',
                '{"action":{"name":"read_file","arguments":{"path":"tests/autonomous_repeat_block_probe.txt"}}}',
                '{"action":{"name":"read_file","arguments":{"path":"tests/autonomous_repeat_block_probe.txt"}}}',
                '{"action":{"name":"write_file","arguments":{"path":"tests/autonomous_repeat_block_probe.txt","content":"repaired","create_dirs":true}}}',
                '{"done":true,"answer":"verified","acceptance":[{"type":"file_contains","path":"tests/autonomous_repeat_block_probe.txt","text":"repaired"}]}',
            ]
        )
        try:
            result = await registry._run_autonomous_workflow(
                "Create the probe, diagnose failures, repair it, and verify the result.",
                max_cycles=16,
                timeout_seconds=30,
                ctx=connected,
            )
            return result, connected
        finally:
            await db.close()

    try:
        result, connected = asyncio.run(run())
        assert result["workflow_status"] == "terminal_success"
        assert result["completed"] is True
        assert any(item.get("kind") == "automatic_diagnosis_enqueued" for item in result["transcript"])
        assert any(item.get("result", {}).get("error_class") == "repeated_identical_action" for item in result["transcript"] if item.get("kind") == "action")
        assert connected.session.calls == 5
    finally:
        probe.unlink(missing_ok=True)