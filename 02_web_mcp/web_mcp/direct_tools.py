"""Small adapters for persisted host decisions and optional desktop actions."""
from __future__ import annotations

from typing import Any

from web_memory.goal_repository import GoalRepository
from web_tools.windows_mcp import WindowsMCPBridge
from .paths import resolve_workspace_path
from .result_contract import safe_error


TOOL_METADATA = {
    "goal_start": {"description": "Persist the ChatGPT goal, acceptance and explicit plan without starting AI work.", "scope": "task"},
    "goal_status": {"description": "Read saved checkpoints, errors, artifacts and linked deterministic tasks.", "scope": "task"},
    "goal_checkpoint": {"description": "Persist one host observation with an idempotency key; no action is replayed.", "scope": "task"},
    "goal_finish": {"description": "Record an outcome; completion requires evidence for every acceptance criterion.", "scope": "task"},
    "goal_cancel": {"description": "Cancel this goal and request cancellation of its linked deterministic tasks.", "scope": "task"},
    "task_status": {"description": "Return the actual status of an existing Worker task immediately.", "scope": "task"},
    "task_log": {"description": "Read bounded ordered task logs with a next_cursor.", "scope": "task"},
    **{f"windows_{name}": {"description": f"Optional Windows-MCP {action}; requires a configured interactive desktop bridge.", "scope": "desktop_read" if name in {"snapshot", "screenshot", "control_status"} else "desktop_control"}
       for name, action in {
           "snapshot": "Snapshot", "screenshot": "Screenshot", "control_status": "ControlStatus",
           "click": "Click", "type": "Type", "shortcut": "Shortcut", "scroll": "Scroll", "app": "App",
       }.items()},
}

WINDOWS_ACTIONS = {
    "windows_snapshot": "Snapshot", "windows_screenshot": "Screenshot", "windows_control_status": "ControlStatus",
    "windows_click": "Click", "windows_type": "Type", "windows_shortcut": "Shortcut", "windows_scroll": "Scroll", "windows_app": "App",
}


class DirectTools:
    def __init__(self, ctx):
        self.ctx = ctx
        self.goals = GoalRepository(ctx.db)
        self.desktop = WindowsMCPBridge(ctx.settings)
        self.task_status_callback = None

    async def invoke(self, name: str, arguments: dict[str, Any]) -> Any:
        if name in WINDOWS_ACTIONS:
            args = dict(arguments)
            confirm = args.pop("confirm", False)
            if TOOL_METADATA[name]["scope"] == "desktop_control":
                if not self.ctx.settings.admin_mode or not self.ctx.settings.windows_mcp_control_enabled:
                    raise PermissionError("Windows desktop control is disabled; enable it only after user approval")
                if confirm is not True:
                    raise PermissionError("Desktop control requires explicit confirm=true and user authorization")
            # The adapter independently validates its upstream whitelist/schema.
            return await self.desktop.call(WINDOWS_ACTIONS[name], args.get("arguments", args))
        return await getattr(self, "_" + name)(**arguments)

    async def _goal_start(self, goal: str, acceptance: list[str], workspace: str = ".", plan: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        root = resolve_workspace_path(workspace, self.ctx.settings.allowed_workspaces, must_exist=True)
        if not root.is_dir():
            raise ValueError("workspace must be a directory")
        value = await self.goals.create(goal, acceptance, str(root), plan)
        return {**value, "goal_id": value["id"]}

    async def _goal_status(self, goal_id: str) -> dict[str, Any] | None:
        value = await self.goals.get(goal_id)
        return {**value, "goal_id": goal_id} if value else None

    async def _goal_checkpoint(self, goal_id: str, step_id: str, observation: dict[str, Any], next_intent: str | None = None,
                               idempotency_key: str | None = None, task_id: str | None = None, status: str = "completed",
                               artifacts: list[str] | None = None, error: dict[str, Any] | None = None, supersedes_task_id: str | None = None) -> dict[str, Any]:
        value = await self.goals.checkpoint(goal_id, step_id, observation, next_intent, idempotency_key, task_id, status, artifacts, error, supersedes_task_id)
        return {**value, "goal_id": goal_id}

    async def _goal_finish(self, goal_id: str, outcome: str, evidence: dict[str, Any]) -> dict[str, Any]:
        value = await self.goals.finish(goal_id, outcome, evidence)
        return {**value, "goal_id": goal_id}

    async def _goal_cancel(self, goal_id: str) -> dict[str, Any]:
        value = await self.goals.cancel(goal_id)
        return {**value, "goal_id": goal_id}

    async def _task_status(self, task_id: str) -> dict[str, Any] | None:
        if self.task_status_callback is not None:
            return await self.task_status_callback(task_id, timeout_seconds=0)
        value = await self.ctx.tasks.get(task_id)
        return {**value, "task_id": task_id} if value else None

    async def _task_log(self, task_id: str, cursor: int = 0, limit: int = 50) -> dict[str, Any]:
        if not await self.ctx.tasks.get(task_id):
            raise FileNotFoundError(task_id)
        if cursor < 0:
            raise ValueError("cursor must be nonnegative")
        limit = max(1, min(int(limit), 100))
        rows = await self.ctx.db.fetchall("SELECT id,level,message,created_at FROM task_logs WHERE task_id=? AND id>? ORDER BY id LIMIT ?", (task_id, cursor, limit + 1))
        page = [{**row, "message": safe_error(row["message"])} for row in rows[:limit]]
        return {"task_id": task_id, "entries": page, "next_cursor": page[-1]["id"] if len(rows) > limit else None}