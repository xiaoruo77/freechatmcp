from __future__ import annotations

import asyncio


def test_python_runner_and_queue(isolated_settings):
    from web_logs.logger import configure_logging
    from web_memory.store import Database
    from web_worker.service import WorkerService

    async def run():
        db = Database(isolated_settings.database_path, isolated_settings.project_root / "08_web_data" / "schema.sql")
        await db.init()
        logger = configure_logging(isolated_settings.log_root, "WARNING")
        service = WorkerService(isolated_settings, db, logger)
        task = await service.tasks.create("python_script", {"code": "print('worker-ok')"})
        worker = asyncio.create_task(service.run_forever())
        for _ in range(50):
            current = await service.tasks.get(task["id"])
            if current and current["status"] in {"success", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.1)
        current = await service.tasks.get(task["id"])
        service.stop()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await db.close()
        assert current["status"] == "success"
        assert current["output_files"]
        assert any(path.endswith("stdout.txt") for path in current["output_files"])


def test_new_md_goal_supersedes_queued_and_running_md_tasks(isolated_settings):
    from web_memory.store import Database
    from web_memory.task_repository import TaskRepository

    async def run():
        db = Database(isolated_settings.database_path, isolated_settings.project_root / "08_web_data" / "schema.sql")
        await db.init()
        tasks = TaskRepository(db)
        old_running = await tasks.create("md_simulation", {"args": ["old"]})
        await tasks.claim_next()
        old_queued = await tasks.create("production_md", {"system_dir": "old"})

        superseded = await tasks.supersede_active_profiles(
            {"md_simulation", "production_md"},
            reason="new-goal",
        )
        assert {item["id"] for item in superseded} == {old_running["id"], old_queued["id"]}
        running = await tasks.get(old_running["id"])
        queued = await tasks.get(old_queued["id"])
        assert running["status"] == "running"
        assert running["cancel_requested"] is True
        assert queued["status"] == "cancelled"
        assert queued["cancel_requested"] is True

        replacement = await tasks.create("md_simulation", {"args": ["new"]})
        assert replacement["status"] == "queued"
        assert (await tasks.claim_next())["id"] == replacement["id"]
        await db.close()

    asyncio.run(run())