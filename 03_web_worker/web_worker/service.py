from __future__ import annotations

import asyncio
import logging
from typing import Any

from web_config.settings import Settings
from web_logs.logger import audit
from web_memory.task_repository import TaskRepository
from web_memory.store import Database
from web_tools.runner import ProfileRunner


class WorkerService:
    """SQLite-backed worker daemon. The API only enqueues; this process executes."""

    def __init__(self, settings: Settings, db: Database, logger: logging.Logger):
        self.settings = settings
        self.db = db
        self.tasks = TaskRepository(db)
        self.runner = ProfileRunner(settings)
        self.logger = logger
        self.stop_event = asyncio.Event()
        self.current_task_id: str | None = None

    async def run_forever(self) -> None:
        await self.db.init()
        orphaned = await self.tasks.mark_orphaned()
        if orphaned:
            audit(self.logger, "worker_orphaned_tasks", count=orphaned)
        audit(self.logger, "worker_started", poll_interval=self.settings.task_poll_interval)
        while not self.stop_event.is_set():
            task = await self.tasks.claim_next()
            if task is None:
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=self.settings.task_poll_interval)
                except asyncio.TimeoutError:
                    pass
                continue
            self.current_task_id = task["id"]
            try:
                await self._run_task(task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive daemon boundary
                await self.tasks.add_log(task["id"], "ERROR", f"Worker exception: {type(exc).__name__}: {exc}")
                await self.tasks.finish(task["id"], "failed", error=f"{type(exc).__name__}: {exc}")
                audit(self.logger, "task_failed", task_id=task["id"], profile=task["profile"], error=str(exc))
            finally:
                self.current_task_id = None
        audit(self.logger, "worker_stopped")

    async def _run_task(self, task: dict[str, Any]) -> None:
        task_id = task["id"]
        profile = task["profile"]

        async def is_cancelled() -> bool:
            return await self.tasks.is_cancel_requested(task_id)

        async def on_output(level: str, message: str) -> None:
            await self.tasks.add_log(task_id, level, message)

        await self.tasks.add_log(task_id, "INFO", f"Starting profile {profile}")
        audit(self.logger, "task_started", task_id=task_id, profile=profile)
        if profile == "workflow":
            result = await self.runner.run_workflow(task["payload"], task_id, is_cancelled, on_output)
        else:
            result = await self.runner.run(profile, task["payload"], task_id, is_cancelled, on_output)
        output_files = self.runner.save_outputs(task_id, result)
        if result.status == "failed":
            if result.stderr:
                error = "Runner failed; inspect the stderr output file"
            else:
                error = (
                    "Runner failed without stderr; inspect the task result, stdout, "
                    f"and return code ({result.return_code}) before retrying"
                )
        else:
            error = None
        result_payload = {
            "return_code": result.return_code,
            "duration_seconds": result.duration_seconds,
            "stdout_bytes": len(result.stdout.encode("utf-8")),
            "stderr_bytes": len(result.stderr.encode("utf-8")),
        }
        if result.details:
            result_payload["details"] = result.details
        await self.tasks.finish(
            task_id,
            result.status,
            result=result_payload,
            output_files=output_files,
            error=error,
        )
        await self.tasks.add_log(task_id, "INFO", f"Task finished with status {result.status}")
        audit(self.logger, "task_finished", task_id=task_id, profile=profile, status=result.status, return_code=result.return_code)

    def stop(self) -> None:
        self.stop_event.set()


async def run_worker() -> None:
    from web_logs.logger import configure_logging

    settings = Settings.load()
    logger = configure_logging(settings.log_root, settings.log_level)
    db = Database(settings.database_path, settings.project_root / "08_web_data" / "schema.sql")
    service = WorkerService(settings, db, logger)
    try:
        await service.run_forever()
    finally:
        await db.close()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()