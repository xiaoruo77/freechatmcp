from __future__ import annotations

import json
from contextvars import ContextVar
from functools import wraps
from typing import Any

from .repository import new_id, utc_now
from .store import Database
from .task_repository import TaskRepository


GOAL_OUTCOMES = {"completed", "failed", "blocked", "cancelled"}
CHECKPOINT_STATUSES = {"completed", "failed", "blocked", "cancelled", "queued", "running"}


def _canonical(value: Any) -> str:
    """Reject non-JSON observations instead of silently stringifying objects."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _isolated_connection(method):
    """Keep goal commits out of the embedded Worker's claim transaction."""

    @wraps(method)
    async def invoke(self, *args, **kwargs):
        if self._operation_db.get() is not None:
            return await method(self, *args, **kwargs)
        db = Database(self._base_db.path, self._base_db.schema_path)
        token = None
        try:
            await db.connect()
            token = self._operation_db.set(db)
            return await method(self, *args, **kwargs)
        finally:
            if token is not None:
                self._operation_db.reset(token)
            await db.close()

    return invoke


class GoalRepository:
    """Persist decisions already made by ChatGPT; never schedule or infer actions.

    Initializing another repository or reopening the database only reads the
    saved intent. Worker startup retains its existing orphan handling, so a
    running task interrupted by a restart becomes failed rather than replayed.
    Acceptance evidence is supplied by the calling ChatGPT session; this
    repository checks its coverage and task consistency, not semantic truth.
    """

    def __init__(self, db: Database):
        self._base_db = db
        self._operation_db: ContextVar[Database | None] = ContextVar("goal_operation_db", default=None)
        self.tasks = TaskRepository(db)

    @property
    def db(self) -> Database:
        return self._operation_db.get() or self._base_db

    @staticmethod
    def _decode_goal(row: dict[str, Any]) -> dict[str, Any]:
        for key, default in (("acceptance", []), ("plan", []), ("evidence", {})):
            row[key] = Database.loads(row.pop(key + "_json", None), default)
        return row

    @staticmethod
    def _decode_checkpoint(row: dict[str, Any]) -> dict[str, Any]:
        for key, default in (("observation", {}), ("artifacts", []), ("error", None)):
            row[key] = Database.loads(row.pop(key + "_json", None), default)
        row.pop("payload_json", None)
        row["supersedes_task_id"] = row["observation"].get("supersedes_task_id")
        return row

    @_isolated_connection
    async def create(
        self,
        goal: str,
        acceptance: list[str],
        workspace: str | None = None,
        plan: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("goal must be nonempty text")
        if (
            not isinstance(acceptance, list)
            or not acceptance
            or any(not isinstance(item, str) or not item.strip() for item in acceptance)
        ):
            raise ValueError("acceptance must contain nonempty criterion strings")
        criteria = [item.strip() for item in acceptance]
        if len(set(criteria)) != len(criteria):
            raise ValueError("acceptance criteria must be unique")
        if plan is not None and (not isinstance(plan, list) or any(not isinstance(item, dict) for item in plan)):
            raise ValueError("plan must be a list of explicit step objects")
        now = utc_now()
        goal_id = new_id("goal")
        await self.db.execute(
            "INSERT INTO goal_sessions(id,goal,acceptance_json,workspace,plan_json,status,evidence_json,created_at,updated_at,finished_at) "
            "VALUES(?,?,?,?,?,'active','{}',?,?,NULL)",
            (goal_id, goal.strip(), _canonical(criteria), workspace, _canonical(plan or []), now, now),
        )
        await self.db.commit()
        result = await self.get(goal_id)
        assert result is not None
        return result

    @_isolated_connection
    async def get(self, goal_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM goal_sessions WHERE id=?", (goal_id,))
        if row is None:
            return None
        result = self._decode_goal(row)
        checkpoints = await self.db.fetchall(
            "SELECT * FROM goal_checkpoints WHERE goal_id=? ORDER BY sequence", (goal_id,)
        )
        result["checkpoints"] = [self._decode_checkpoint(item) for item in checkpoints]
        tasks = await self.db.fetchall(
            "SELECT DISTINCT t.* FROM tasks t JOIN goal_checkpoints c ON c.task_id=t.id WHERE c.goal_id=? "
            "ORDER BY t.created_at,t.id", (goal_id,)
        )
        result["tasks"] = [TaskRepository._decode(item) for item in tasks]
        result["next_intent"] = checkpoints[-1]["next_intent"] if checkpoints else None
        result["terminal"] = result["status"] in GOAL_OUTCOMES
        return result

    @_isolated_connection
    async def checkpoint(
        self,
        goal_id: str,
        step_id: str,
        observation: dict[str, Any],
        next_intent: str | None = None,
        idempotency_key: str | None = None,
        task_id: str | None = None,
        status: str = "completed",
        artifacts: list[str] | None = None,
        error: dict[str, Any] | None = None,
        supersedes_task_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(step_id, str) or not step_id.strip():
            raise ValueError("step_id must be nonempty text")
        if not isinstance(observation, dict):
            raise ValueError("observation must be a JSON object")
        if status not in CHECKPOINT_STATUSES:
            raise ValueError("Unsupported checkpoint status")
        if next_intent is not None and not isinstance(next_intent, str):
            raise ValueError("next_intent must be text or null")
        if artifacts is not None and (not isinstance(artifacts, list) or any(not isinstance(item, str) for item in artifacts)):
            raise ValueError("artifacts must be a list of path/reference strings")
        if error is not None and not isinstance(error, dict):
            raise ValueError("error must be a JSON object or null")
        observation = dict(observation)
        if "supersedes_task_id" in observation and observation["supersedes_task_id"] != supersedes_task_id:
            raise ValueError("Use the explicit supersedes_task_id parameter to authorize attempt replacement")
        if supersedes_task_id is not None:
            if not isinstance(supersedes_task_id, str) or not supersedes_task_id.strip():
                raise ValueError("supersedes_task_id must be nonempty text")
            if not isinstance(task_id, str) or not task_id or task_id == supersedes_task_id:
                raise ValueError("Attempt replacement requires a different new task_id")
            if status != "completed":
                raise ValueError("A replacement checkpoint must be completed")
            observation["supersedes_task_id"] = supersedes_task_id
        key = step_id if idempotency_key is None else idempotency_key
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise ValueError("idempotency_key must be nonempty text of at most 200 characters")
        payload = _canonical({
            "step_id": step_id, "observation": observation, "next_intent": next_intent,
            "task_id": task_id, "status": status, "artifacts": artifacts or [], "error": error,
        })
        existing = await self.db.fetchone(
            "SELECT * FROM goal_checkpoints WHERE goal_id=? AND idempotency_key=?", (goal_id, key)
        )
        if existing is not None:
            if existing["payload_json"] != payload:
                raise ValueError("idempotency_key already belongs to a different checkpoint payload")
            return self._decode_checkpoint(existing)
        goal = await self.db.fetchone("SELECT status FROM goal_sessions WHERE id=?", (goal_id,))
        if goal is None:
            raise ValueError("Goal not found")
        if goal["status"] != "active":
            raise ValueError("Cannot append a checkpoint to a terminal goal")
        if task_id is not None and await self.db.fetchone("SELECT id FROM tasks WHERE id=?", (task_id,)) is None:
            raise ValueError("Linked task not found")
        replacement_guard = ""
        replacement_parameters: tuple = ()
        if supersedes_task_id is not None:
            old = await self.db.fetchone("SELECT status FROM tasks WHERE id=?", (supersedes_task_id,))
            if old is None or old["status"] not in {"failed", "cancelled"}:
                raise ValueError("Only a failed/cancelled historical task can be superseded")
            linked = await self.db.fetchone(
                "SELECT id FROM goal_checkpoints WHERE goal_id=? AND step_id=? AND task_id=? LIMIT 1",
                (goal_id, step_id, supersedes_task_id),
            )
            if linked is None:
                raise ValueError("The superseded task must already belong to this goal and step_id")
            new = await self.db.fetchone("SELECT status,result_json FROM tasks WHERE id=?", (task_id,))
            new_result = self.db.loads(new["result_json"], None) if new else None
            if (
                new is None or new["status"] != "success" or not isinstance(new_result, dict)
                or type(new_result.get("return_code")) is not int or new_result["return_code"] != 0
            ):
                raise ValueError("The replacement task must have a real success result")
            foreign = await self.db.fetchone(
                "SELECT id FROM goal_checkpoints WHERE task_id=? AND goal_id<>? LIMIT 1", (task_id, goal_id)
            )
            if foreign is not None:
                raise ValueError("The replacement task is associated with another goal")
            previous = await self.db.fetchall("SELECT task_id,observation_json FROM goal_checkpoints WHERE goal_id=?", (goal_id,))
            edges: dict[str, set[str]] = {}
            for item in previous:
                old_id = self.db.loads(item["observation_json"], {}).get("supersedes_task_id")
                if old_id and item["task_id"]:
                    edges.setdefault(old_id, set()).add(item["task_id"])
            pending, visited = [task_id], set()
            while pending:
                current = pending.pop()
                if current == supersedes_task_id:
                    raise ValueError("Attempt replacement cannot create a cycle")
                if current not in visited:
                    visited.add(current)
                    pending.extend(edges.get(current, ()))
            # Recheck task truth in the inserting statement. Another connection
            # may update a task after the readable validation above.
            replacement_guard = (
                " AND EXISTS(SELECT 1 FROM tasks WHERE id=? AND status IN ('failed','cancelled'))"
                " AND EXISTS(SELECT 1 FROM tasks WHERE id=? AND status='success' "
                "AND json_type(result_json,'$.return_code')='integer' AND json_extract(result_json,'$.return_code')=0)"
                " AND NOT EXISTS(SELECT 1 FROM goal_checkpoints WHERE task_id=? AND goal_id<>?)"
            )
            replacement_parameters = (supersedes_task_id, task_id, task_id, goal_id)
        now = utc_now()
        checkpoint_id = new_id("checkpoint")
        # One SQLite statement arbitrates concurrent retries, including retries
        # through independent service connections. No side effect is replayed.
        cursor = await self.db.execute(
            "INSERT INTO goal_checkpoints(id,goal_id,step_id,idempotency_key,observation_json,next_intent,task_id,status,artifacts_json,error_json,payload_json,created_at) "
            "SELECT ?,?,?,?,?,?,?,?,?,?,?,? WHERE EXISTS(SELECT 1 FROM goal_sessions WHERE id=? AND status='active')" + replacement_guard + " "
            "ON CONFLICT(goal_id,idempotency_key) DO NOTHING",
            (checkpoint_id, goal_id, step_id, key, _canonical(observation), next_intent, task_id, status,
             _canonical(artifacts or []), _canonical(error) if error is not None else None, payload, now, goal_id, *replacement_parameters),
        )
        if cursor.rowcount:
            await self.db.execute(
                "UPDATE goal_sessions SET updated_at=? WHERE id=? AND status='active'", (now, goal_id)
            )
        await self.db.commit()
        saved = await self.db.fetchone(
            "SELECT * FROM goal_checkpoints WHERE goal_id=? AND idempotency_key=?", (goal_id, key)
        )
        if saved is None:
            raise ValueError("Goal or replacement task status changed; read current status before retrying")
        if saved["payload_json"] != payload:
            raise ValueError("idempotency_key already belongs to a different checkpoint payload")
        return self._decode_checkpoint(saved)

    @staticmethod
    def _validate_acceptance(criteria: list[str], evidence: dict[str, Any]) -> None:
        checks = evidence.get("checks")
        if not isinstance(checks, list) or not checks:
            raise ValueError("completed requires evidence.checks for every acceptance criterion")
        checked: set[str] = set()
        for check in checks:
            if not isinstance(check, dict) or check.get("passed") is not True:
                raise ValueError("Every acceptance check must have the JSON boolean passed=true")
            criterion = check.get("criterion")
            if not isinstance(criterion, str) or criterion not in criteria or criterion in checked:
                raise ValueError("Each acceptance check must identify one unique saved criterion")
            source = check.get("evidence")
            if source is None or isinstance(source, (bool, int, float)) or not source:
                raise ValueError("Acceptance checks require nonempty observed evidence/references")
            if isinstance(source, str) and not source.strip():
                raise ValueError("Acceptance evidence cannot be whitespace")
            checked.add(criterion)
        if checked != set(criteria):
            raise ValueError("Acceptance evidence does not cover every saved criterion")

    @_isolated_connection
    async def finish(self, goal_id: str, outcome: str, evidence: dict[str, Any]) -> dict[str, Any]:
        if outcome not in GOAL_OUTCOMES:
            raise ValueError("Unsupported goal outcome")
        if not isinstance(evidence, dict):
            raise ValueError("evidence must be a JSON object")
        serialized = _canonical(evidence)
        goal = await self.get(goal_id)
        if goal is None:
            raise ValueError("Goal not found")
        if goal["terminal"]:
            if goal["status"] == outcome and _canonical(goal["evidence"]) == serialized:
                return goal
            raise ValueError("Goal already has a different terminal outcome/evidence")
        if outcome == "cancelled":
            raise ValueError("Use goal_cancel to cancel associated Worker tasks")
        if outcome == "completed":
            self._validate_acceptance(goal["acceptance"], evidence)
            task_status = {task["id"]: task["status"] for task in goal["tasks"]}
            successful_replacements = {
                task["id"] for task in goal["tasks"]
                if task["status"] == "success" and isinstance(task["result"], dict)
                and type(task["result"].get("return_code")) is int and task["result"]["return_code"] == 0
            }
            replacements = {
                (item["supersedes_task_id"], item["step_id"])
                for item in goal["checkpoints"]
                if item["supersedes_task_id"] and item["status"] == "completed"
                and item["task_id"] in successful_replacements
                and item["task_id"] != item["supersedes_task_id"]
            }
            for checkpoint in goal["checkpoints"]:
                task_id = checkpoint["task_id"]
                status = task_status.get(task_id)
                if task_id and status != "success" and not (
                    status in {"failed", "cancelled"} and (task_id, checkpoint["step_id"]) in replacements
                ):
                    raise ValueError("All linked Worker tasks must succeed or have an explicit successful same-step replacement")
            # A later completed observation for the same step can resolve a
            # transient failure. Failed Worker attempts require the explicit
            # successful replacement validated above and remain in history.
            latest = {item["step_id"]: item for item in goal["checkpoints"]}
            if any(item["status"] != "completed" for item in latest.values()):
                raise ValueError("Every latest step checkpoint must be completed before finishing")
        now = utc_now()
        completion_guard = (
            " AND NOT EXISTS(SELECT 1 FROM goal_checkpoints c JOIN tasks t ON t.id=c.task_id "
            "WHERE c.goal_id=goal_sessions.id AND t.status<>'success' AND NOT (t.status IN ('failed','cancelled') "
            "AND EXISTS(SELECT 1 FROM goal_checkpoints replacement JOIN tasks rt ON rt.id=replacement.task_id "
            "WHERE replacement.goal_id=c.goal_id AND replacement.step_id=c.step_id AND replacement.status='completed' "
            "AND rt.status='success' AND json_type(rt.result_json,'$.return_code')='integer' "
            "AND json_extract(rt.result_json,'$.return_code')=0 AND rt.id<>t.id "
            "AND json_extract(replacement.observation_json,'$.supersedes_task_id')=t.id)))"
            " AND NOT EXISTS(SELECT 1 FROM goal_checkpoints c WHERE c.goal_id=goal_sessions.id "
            "AND c.status<>'completed' AND c.sequence=(SELECT MAX(c2.sequence) FROM goal_checkpoints c2 "
            "WHERE c2.goal_id=c.goal_id AND c2.step_id=c.step_id))"
            if outcome == "completed" else ""
        )
        cursor = await self.db.execute(
            "UPDATE goal_sessions SET status=?,evidence_json=?,updated_at=?,finished_at=? WHERE id=? AND status='active'" + completion_guard,
            (outcome, serialized, now, now, goal_id),
        )
        await self.db.commit()
        if not cursor.rowcount:
            raise ValueError("Goal or linked task/step changed concurrently; read its saved status")
        result = await self.get(goal_id)
        assert result is not None
        return result

    @_isolated_connection
    async def cancel(self, goal_id: str) -> dict[str, Any]:
        goal = await self.get(goal_id)
        if goal is None:
            raise ValueError("Goal not found")
        if goal["terminal"] and goal["status"] != "cancelled":
            raise ValueError("Cannot cancel a goal with a different terminal outcome")
        now = utc_now()
        # Close checkpoint admission first; no newly linked task can escape the
        # cancellation snapshot. Repeating this method after a crash finishes
        # any cancellation requests that were not yet delivered to the Worker.
        await self.db.execute(
            "UPDATE goal_sessions SET status='cancelled',evidence_json=?,updated_at=?,finished_at=? WHERE id=? AND status='active'",
            (_canonical({"reason": "Cancelled by the caller"}), now, now, goal_id),
        )
        await self.db.commit()
        goal = await self.get(goal_id)
        assert goal is not None
        if goal["status"] != "cancelled":
            raise ValueError("Goal was finished concurrently; read its saved status")
        # Reuse cooperative cancellation. A running subprocess remains running
        # until the Worker acknowledges the request; goal_status exposes that.
        for task in goal["tasks"]:
            if task["status"] in {"queued", "running"}:
                await TaskRepository(self.db).request_cancel(task["id"])
        result = await self.get(goal_id)
        assert result is not None
        return result