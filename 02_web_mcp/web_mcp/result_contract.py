"""Model-readable results for the opt-in ChatGPT-owned orchestration path."""
from __future__ import annotations

import re
from typing import Any
from pydantic import BaseModel, ConfigDict


class ToolEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")
    ok: bool
    status: str
    terminal: bool
    task_id: str | None
    operation: str
    result: dict[str, Any]
    artifacts: list[Any]
    error: dict[str, Any] | None
    retryable: bool
    evidence: dict[str, Any]


def safe_error(value: Any) -> str:
    text = str(value)
    text = re.sub(r"(?i)Bearer\s+[^\s,;]+", "Bearer [REDACTED]", text)
    return re.sub(
        r"(?i)(token|secret|password|authorization|cookie|api[_ -]?key)\s*([=:])\s*([^\s,;&]+)",
        r"\1\2[REDACTED]", text,
    )[:4000]


def error_result(operation: str, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, PermissionError) or type(exc).__name__ == "WorkspaceViolation":
        code = "permission_denied"
    elif isinstance(exc, FileNotFoundError):
        code = "not_found"
    elif isinstance(exc, FileExistsError):
        code = "already_exists"
    elif isinstance(exc, (ValueError, TypeError)):
        code = "invalid_argument"
    elif isinstance(exc, TimeoutError):
        code = "operation_timeout"
    else:
        code = "operation_error"
    return normalize_result(operation, {
        "status": "failed", "ok": False, "error_code": code,
        "error": safe_error(exc), "retryable": isinstance(exc, (TimeoutError, ConnectionError)),
    })


def _diagnostics(value: Any, sensitive: bool = False) -> Any:
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            name = str(key).lower()
            if any(word in name for word in ("token", "password", "secret", "cookie", "authorization", "api_key")):
                clean[key] = "[REDACTED]"
            else:
                clean[key] = _diagnostics(item, sensitive or name in {"error", "stderr", "stdout", "logs", "message", "diagnostic_evidence"})
        return clean
    if isinstance(value, list):
        return [_diagnostics(item, sensitive) for item in value]
    return safe_error(value) if sensitive and isinstance(value, str) else value


def normalize_result(operation: str, value: Any) -> dict[str, Any]:
    """terminal describes this operation/task, never the entire user's goal.

    Keep compatibility fields while placing the exact original payload in
    result.data. No launch/queued response is promoted to completed.
    """
    if isinstance(value, dict) and value.get("operation") and isinstance(value.get("result"), dict) and "data" in value["result"]:
        return {**_diagnostics(value), "operation": operation}
    raw = _diagnostics(dict(value) if isinstance(value, dict) else {"items": value} if isinstance(value, list) else {"value": value})
    status = str(raw.get("status") or "").lower()
    task_id = raw.get("task_id") or (raw.get("id") if str(raw.get("id", "")).startswith("task_") else None)
    # Existing repositories use task-... on some deployments.
    if not task_id and str(raw.get("id", "")).startswith("task-"):
        task_id = raw["id"]
    if value is None:
        raw.update(status="failed", error_code="not_found", error="Requested record was not found")
        status = "failed"
    active = status in {"queued", "running", "processing", "in_progress", "detached", "active"}
    planning = status == "needs_chatgpt_planning" or raw.get("workflow_status") == "needs_chatgpt_planning"
    error = raw.get("error")
    rc = raw.get("returncode", raw.get("return_code"))
    failed = bool(status in {"failed", "error", "timeout"} or raw.get("ok") is False or raw.get("success") is False or error or (isinstance(rc, int) and rc != 0))
    if planning:
        status, active, failed = "needs_chatgpt_planning", False, False
    elif active:
        # Queuing is a successful request; it provides no execution proof yet.
        failed = False
    elif failed:
        status = "failed"
    elif status not in {"cancelled", "blocked"}:
        status = "completed"
    terminal = not (active or planning)
    code = raw.get("error_code") or raw.get("error_class") or (error.get("code") if isinstance(error, dict) else None) or ("operation_failed" if failed else None)
    message = safe_error(error) if error else ("Operation failed; inspect returned evidence" if failed else None)
    evidence = raw.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {"checks": raw.get("checks") or raw.get("acceptance_results") or []}
    artifacts = raw.get("artifacts") or raw.get("output_files") or []
    result = {
        **raw,
        "ok": not failed,
        "status": status,
        "terminal": terminal,
        "completed": status == "completed",
        "task_id": task_id,
        "operation": operation,
        "result": {"summary": str(raw.get("summary") or raw.get("message") or f"{operation}: {status}")[:2000], "data": raw},
        "artifacts": artifacts,
        "error": {"code": code, "message": message} if failed else None,
        "error_code": code if failed else None,
        "retryable": bool(raw.get("retryable", False)),
        "evidence": evidence,
    }
    for obsolete in ("do_not_reply", "final_answer_allowed", "user_visible_final", "finalize_now"):
        result.pop(obsolete, None)
    if active and task_id:
        result["suggested_next_tool"] = "task_status"
    elif planning:
        result["suggested_next_tool"] = "goal_start"
    return result