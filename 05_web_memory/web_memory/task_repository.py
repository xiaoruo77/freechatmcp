from __future__ import annotations

from typing import Any

from .repository import new_id, utc_now
from .store import Database


TASK_STATUSES = {"queued", "running", "success", "failed", "cancelled"}
MD_TASK_PROFILES = {"md_simulation", "production_md"}


class TaskRepository:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _decode(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        row["payload"] = Database.loads(row.pop("payload_json", None), {})
        row["result"] = Database.loads(row.pop("result_json", None), None)
        row["output_files"] = Database.loads(row.pop("output_files_json", None), [])
        row["cancel_requested"] = bool(row.get("cancel_requested", 0))
        return row

    async def create(self, profile: str, payload: dict[str, Any]) -> dict[str, Any]:
        task = {
            "id": new_id("task"),
            "profile": profile,
            "payload_json": self.db.dumps(payload),
            "status": "queued",
            "created_at": utc_now(),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "result_json": None,
            "output_files_json": self.db.dumps([]),
            "cancel_requested": 0,
        }
        await self.db.execute(
            "INSERT INTO tasks(id,profile,payload_json,status,created_at,started_at,finished_at,error,result_json,output_files_json,cancel_requested) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            task.values(),
        )
        await self.db.commit()
        return self._decode(task) or {}

    async def get(self, task_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM tasks WHERE id=?", (task_id,))
        task = self._decode(row)
        if task:
            task["logs"] = await self.logs(task_id, limit=200)
        return task

    async def list(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),))
        return [self._decode(row) for row in rows if row]

    async def claim_next(self) -> dict[str, Any] | None:
        await self.db.begin_immediate()
        try:
            row = await self.db.fetchone("SELECT * FROM tasks WHERE status='queued' ORDER BY created_at ASC LIMIT 1")
            if not row:
                await self.db.commit()
                return None
            now = utc_now()
            await self.db.execute("UPDATE tasks SET status='running',started_at=? WHERE id=? AND status='queued'", (now, row["id"]))
            await self.db.commit()
            row["status"] = "running"
            row["started_at"] = now
            return self._decode(row)
        except Exception:
            await self.db.rollback()
            raise

    async def request_cancel(self, task_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT status FROM tasks WHERE id=?", (task_id,))
        if not row:
            return None
        if row["status"] == "queued":
            await self.db.execute("UPDATE tasks SET status='cancelled',finished_at=?,cancel_requested=1 WHERE id=?", (utc_now(), task_id))
        elif row["status"] == "running":
            await self.db.execute("UPDATE tasks SET cancel_requested=1 WHERE id=?", (task_id,))
        await self.db.commit()
        return await self.get(task_id)

    async def supersede_active_profiles(
        self,
        profiles: set[str] | list[str] | tuple[str, ...],
        *,
        reason: str,
        keep_task_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Stop older queued/running tasks before starting a new exclusive run.

        MD setup and production share one Worker and one WSL/GROMACS resource.
        Leaving an older run in front of a newer chat goal creates a false
        continuation: the new goal waits behind stale work and can later
        inspect the stale task's artifacts.  Queued tasks are cancelled
        immediately; running tasks receive the normal cooperative cancellation
        request so the Worker terminates the child process and records a
        terminal cancelled result before claiming the replacement task.
        """

        selected = tuple(sorted({str(item).strip() for item in profiles if str(item).strip()}))
        if not selected:
            return []
        keep = {str(item) for item in (keep_task_ids or set())}
        placeholders = ",".join("?" for _ in selected)
        rows = await self.db.fetchall(
            f"SELECT id,status,profile FROM tasks WHERE status IN ('queued','running') "
            f"AND profile IN ({placeholders}) ORDER BY created_at ASC",
            selected,
        )
        changed: list[str] = []
        now = utc_now()
        for row in rows:
            task_id = str(row.get("id") or "")
            if not task_id or task_id in keep:
                continue
            if row.get("status") == "queued":
                await self.db.execute(
                    "UPDATE tasks SET status='cancelled',finished_at=?,error=?,cancel_requested=1 "
                    "WHERE id=? AND status='queued'",
                    (now, reason, task_id),
                )
            else:
                await self.db.execute(
                    "UPDATE tasks SET cancel_requested=1,error=? WHERE id=? AND status='running'",
                    (reason, task_id),
                )
            changed.append(task_id)
        if changed:
            await self.db.commit()
        return [
            task
            for task_id in changed
            if (task := await self.get(task_id)) is not None
        ]

    async def is_cancel_requested(self, task_id: str) -> bool:
        row = await self.db.fetchone("SELECT cancel_requested,status FROM tasks WHERE id=?", (task_id,))
        return bool(row and (row["cancel_requested"] or row["status"] == "cancelled"))

    async def finish(
        self,
        task_id: str,
        status: str,
        result: Any = None,
        output_files: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        if status not in TASK_STATUSES:
            raise ValueError(f"Unsupported task status: {status}")
        await self.db.execute(
            "UPDATE tasks SET status=?,finished_at=?,error=?,result_json=?,output_files_json=? WHERE id=?",
            (status, utc_now(), error, self.db.dumps(result) if result is not None else None, self.db.dumps(output_files or []), task_id),
        )
        await self.db.commit()

    async def mark_orphaned(self) -> int:
        cursor = await self.db.execute(
            "UPDATE tasks SET status='failed',finished_at=?,error=? WHERE status='running'",
            (utc_now(), "Worker restarted while task was running"),
        )
        await self.db.commit()
        return cursor.rowcount

    async def add_log(self, task_id: str, level: str, message: str) -> None:
        await self.db.execute("INSERT INTO task_logs(task_id,level,message,created_at) VALUES(?,?,?,?)", (task_id, level, message[:20000], utc_now()))
        await self.db.commit()

    async def logs(self, task_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return await self.db.fetchall("SELECT * FROM task_logs WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, max(1, min(limit, 1000))))