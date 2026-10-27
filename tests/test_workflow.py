from __future__ import annotations

import asyncio
from pathlib import Path


def _run_workflow_test(isolated_settings, steps, final_checks):
    from web_canvas.service import CanvasService
    from web_logs.logger import configure_logging
    from web_memory.store import Database
    from web_mcp.tool_registry import ToolContext, ToolRegistry
    from web_worker.service import WorkerService

    async def run():
        db = Database(isolated_settings.database_path, isolated_settings.project_root / "08_web_data" / "schema.sql")
        await db.init()
        logger = configure_logging(isolated_settings.log_root, "WARNING")
        service = WorkerService(isolated_settings, db, logger)
        registry = ToolRegistry(ToolContext(isolated_settings, db, CanvasService(db), logger))
        worker_task = asyncio.create_task(service.run_forever())
        try:
            return await registry.invoke(
                "run_workflow_until_complete",
                {
                    "steps": steps,
                    "final_checks": final_checks,
                    "max_attempts": 1,
                    "overall_timeout": 30,
                    "wait_timeout": 30,
                    "poll_interval_ms": 200,
                },
            )
        finally:
            service.stop()
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
            await db.close()

    return asyncio.run(run())


def test_workflow_waits_for_all_steps_and_validates_output(isolated_settings):
    probe = isolated_settings.project_root / "tests" / "workflow_runtime_probe.txt"
    probe.unlink(missing_ok=True)
    try:
        steps = [
            {
                "label": "write first phase",
                "profile": "python_script",
                "payload": {
                    "cwd": str(isolated_settings.project_root),
                    "code": "from pathlib import Path\nPath('tests/workflow_runtime_probe.txt').write_text('phase-one\\n', encoding='utf-8')",
                },
            },
            {
                "label": "write second phase",
                "profile": "python_script",
                "payload": {
                    "cwd": str(isolated_settings.project_root),
                    "code": "from pathlib import Path\np=Path('tests/workflow_runtime_probe.txt')\np.write_text(p.read_text(encoding='utf-8') + 'phase-two\\n', encoding='utf-8')",
                },
            },
            {
                "label": "write final marker",
                "profile": "python_script",
                "payload": {
                    "cwd": str(isolated_settings.project_root),
                    "code": "from pathlib import Path\np=Path('tests/workflow_runtime_probe.txt')\np.write_text(p.read_text(encoding='utf-8') + 'phase-three\\n', encoding='utf-8')",
                },
            },
        ]
        result = _run_workflow_test(
            isolated_settings,
            steps,
            [
                {"type": "file_nonempty", "path": "tests/workflow_runtime_probe.txt"},
                {"type": "file_contains", "path": "tests/workflow_runtime_probe.txt", "text": "phase-one\nphase-two\nphase-three"},
            ],
        )
        assert result["completed"] is True
        assert result["status"] == "success"
        assert result["result"]["details"]["steps"][-1]["label"] == "write final marker"
        assert all(item["passed"] for item in result["result"]["details"]["final_checks"])
        assert probe.read_text(encoding="utf-8") == "phase-one\nphase-two\nphase-three\n"
    finally:
        probe.unlink(missing_ok=True)


def test_workflow_retries_failed_step_before_final_check(isolated_settings):
    marker = isolated_settings.project_root / "tests" / "workflow_retry_marker.txt"
    output = isolated_settings.project_root / "tests" / "workflow_retry_output.txt"
    marker.unlink(missing_ok=True)
    output.unlink(missing_ok=True)
    try:
        result = _run_workflow_test(
            isolated_settings,
            [
                {
                    "label": "retry once then finish",
                    "profile": "python_script",
                    "retries": 1,
                    "payload": {
                        "cwd": str(isolated_settings.project_root),
                        "code": (
                            "from pathlib import Path\n"
                            "marker=Path('tests/workflow_retry_marker.txt')\n"
                            "count=int(marker.read_text(encoding='utf-8')) if marker.exists() else 0\n"
                            "marker.write_text(str(count + 1), encoding='utf-8')\n"
                            "if count == 0: raise SystemExit('intentional first attempt failure')\n"
                            "Path('tests/workflow_retry_output.txt').write_text('retry-success', encoding='utf-8')"
                        ),
                    },
                }
            ],
            [{"type": "file_contains", "path": "tests/workflow_retry_output.txt", "text": "retry-success"}],
        )
        assert result["completed"] is True
        attempts = result["result"]["details"]["steps"]
        assert [item["status"] for item in attempts] == ["failed", "success"]
        assert marker.read_text(encoding="utf-8") == "2"
        assert output.read_text(encoding="utf-8") == "retry-success"
    finally:
        marker.unlink(missing_ok=True)
        output.unlink(missing_ok=True)


def test_workflow_normalizes_common_profile_aliases(isolated_settings):
    probe = isolated_settings.project_root / "tests" / "workflow_alias_probe.txt"
    probe.unlink(missing_ok=True)
    try:
        result = _run_workflow_test(
            isolated_settings,
            [
                {
                    "label": "alias profile",
                    "profile": "python",
                    "payload": {
                        "cwd": str(isolated_settings.project_root),
                        "code": "from pathlib import Path\nPath('tests/workflow_alias_probe.txt').write_text('alias-ok', encoding='utf-8')",
                    },
                }
            ],
            [{"type": "file_contains", "path": "tests/workflow_alias_probe.txt", "text": "alias-ok"}],
        )
        assert result["completed"] is True
        assert result["result"]["details"]["steps"][0]["profile"] == "python_script"
    finally:
        probe.unlink(missing_ok=True)


def test_workflow_stress_runs_twelve_ordered_steps(isolated_settings):
    probe = isolated_settings.project_root / "tests" / "workflow_stress_probe.txt"
    probe.unlink(missing_ok=True)
    try:
        steps = []
        for index in range(1, 13):
            marker = f"step-{index:02d}"
            steps.append(
                {
                    "label": f"ordered action {index}",
                    "profile": "python_script",
                    "payload": {
                        "cwd": str(isolated_settings.project_root),
                        "code": (
                            "from pathlib import Path\n"
                            "p=Path('tests/workflow_stress_probe.txt')\n"
                            f"p.write_text(p.read_text(encoding='utf-8') + '{marker}\\n' if p.exists() else '{marker}\\n', encoding='utf-8')"
                        ),
                    },
                }
            )
        result = _run_workflow_test(
            isolated_settings,
            steps,
            [
                {"type": "file_nonempty", "path": "tests/workflow_stress_probe.txt"},
                {"type": "file_contains", "path": "tests/workflow_stress_probe.txt", "text": "step-01\nstep-02\nstep-03\nstep-04\nstep-05\nstep-06\nstep-07\nstep-08\nstep-09\nstep-10\nstep-11\nstep-12"},
            ],
        )
        assert result["completed"] is True
        assert result["result"]["details"]["steps"][-1]["step"] == 12
        assert probe.read_text(encoding="utf-8") == "".join(f"step-{index:02d}\n" for index in range(1, 13))
    finally:
        probe.unlink(missing_ok=True)