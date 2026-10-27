"""Shared, bounded repair contract for compatibility workflows.

The original Xiaoruo2 workflow surface exposed a nested ``repair`` object.
Xiaoruo1 already has richer recovery fields, so this module adds the small
machine-readable contract without replacing the existing task/retry policy.
"""

from __future__ import annotations

from typing import Any


MAX_REPAIR_CYCLES = 12

_REPAIR_TOOLS = [
    "read_file",
    "patch_file",
    "write_file",
    "discover_environment",
    "self_check",
    "search_web",
    "run_python_script",
    "run_powershell_workflow",
    "run_terminal_workflow",
    "run_wsl_workflow",
    "git_diff",
    "run_workflow",
]


def build_repair_contract(
    *,
    success: bool,
    attempt: int = 0,
    failed_phase: str | None = None,
    error: str | None = None,
    evidence: Any = None,
    manifest_path: str | None = None,
    timed_out: bool = False,
    search_query: str | None = None,
    web_search_required: bool | None = None,
) -> dict[str, Any]:
    """Build the explicit client-facing repair/answer boundary."""

    bounded_attempt = max(0, min(int(attempt), MAX_REPAIR_CYCLES))
    if success:
        return {
            "required": False,
            "final_answer_allowed": True,
            "recoverable": False,
            "cycle": bounded_attempt,
            "max_cycles": MAX_REPAIR_CYCLES,
            "next_action": "All workflow stages and acceptance checks passed.",
            "web_search_required": False,
        }

    contract: dict[str, Any] = {
        "required": True,
        "final_answer_allowed": False,
        "recoverable": True,
        "cycle": bounded_attempt,
        "max_cycles": MAX_REPAIR_CYCLES,
        "failed_phase": failed_phase or "unknown",
        "timed_out": bool(timed_out),
        "procedure": [
            "Treat this result as a repair checkpoint, not a final answer.",
            "Read the exact error, stderr/stdout, browser state, manifest, and referenced logs.",
            "Call discover_environment with the original request and exact failure text.",
            "Call self_check; it performs read-only local checks and a sanitized public web search for troubleshooting evidence.",
            "Read the relevant source/configuration and identify the smallest cause.",
            "Apply a minimal reversible fix with patch_file/write_file, then run tests.",
            "Call run_workflow again with repair_attempt incremented and verify every check.",
            "Only report completion after repair.final_answer_allowed=true.",
        ],
        "allowed_tools": list(_REPAIR_TOOLS),
        "next_action": "读取证据→发现环境→最小修改→测试→用不同参数重新运行。",
        "web_search_required": bool(error) if web_search_required is None else bool(web_search_required),
        "web_search_tool": "search_web",
    }
    if error:
        contract["error"] = str(error)[:4_000]
    if manifest_path:
        contract["manifest_path"] = manifest_path
    if evidence not in (None, {}, []):
        contract["evidence"] = evidence
    if search_query:
        contract["search_query"] = str(search_query)[:240]
    return contract