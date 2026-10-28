from __future__ import annotations

import asyncio
import json
import inspect
from functools import wraps
import base64
from pathlib import Path
from typing import Any, Literal

from mcp.server.auth.provider import OAuthAuthorizationServerProvider, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from .tool_registry import ToolRegistry, _ensure_terminal_user_answer
from .result_contract import error_result, normalize_result, ToolEnvelope
from mcp.types import CallToolResult, TextContent, ImageContent, ToolAnnotations


_registry: ToolRegistry | None = None
_BACKGROUND_MCP_CALLS: set[asyncio.Task[Any]] = set()


def _retain_background_call(call: asyncio.Task[Any]) -> None:
    """Keep a supervisor alive when the remote MCP request is cancelled."""

    _BACKGROUND_MCP_CALLS.add(call)

    def _discard(done: asyncio.Task[Any]) -> None:
        _BACKGROUND_MCP_CALLS.discard(done)
        try:
            done.exception()
        except (asyncio.CancelledError, Exception):
            # The durable Worker/task record is the source of truth.  Avoid an
            # unhandled exception warning after a client-side disconnect.
            pass

    call.add_done_callback(_discard)


MCP_INSTRUCTIONS = (
    "Xiaoruo1 executes explicit file, terminal, Git, browser and deterministic Worker actions. "
    "In chatgpt_direct mode the current ChatGPT conversation owns all planning, diagnosis, "
    "next-tool selection and acceptance decisions. No Sampling, extra model API, or hidden "
    "AI process is used. Read each actual result and, when appropriate, make the next "
    "dependent tool call in the same response. queued/running are normal nonterminal task "
    "states: use task_status, then bounded read_task_output/task_log to verify completion. "
    "A completed tool operation is not proof that the whole user goal is complete. "
    "For complex goals save goal_start/goal_checkpoint records and finish only with concrete "
    "acceptance evidence. run_autonomous_workflow is a compatibility name: direct mode only "
    "executes explicit workflow.steps; a goal without steps needs ChatGPT planning. "
    "Use exact returned paths and tab IDs, inspect errors, and verify changed retries. "
    "Keep the existing deterministic GLYCAM/GROMACS runners and their input/artifact checks; "
    "do not replace requested scientific work with diagnostics. Windows desktop tools are "
    "optional and require an authenticated, unlocked interactive Windows user session. "
    "Respect host approvals and user authorization. If the host stops tool calls, save and "
    "report the goal_id and actual task state. Server instructions cannot force continued "
    "calls, bypass platform limits, or obtain new ChatGPT reasoning after the chat closes. "
    "legacy_sampling is an explicit compatibility mode and is outside this direct contract."
)


def configure_registry(registry: ToolRegistry | None) -> ToolRegistry | None:
    global _registry
    previous = _registry
    _registry = registry
    return previous


def release_registry(expected: ToolRegistry, previous: ToolRegistry | None) -> None:
    global _registry
    if _registry is expected:
        _registry = previous


def registry() -> ToolRegistry:
    if _registry is None:
        raise RuntimeError("web_mcp registry is not configured")
    return _registry


def _direct_mode() -> bool:
    return bool(getattr(_registry, "chatgpt_direct", False))


async def _invoke_tool(tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
    if not _direct_mode():
        return await registry().invoke(tool_name, arguments)
    try:
        return normalize_result(tool_name, await registry().invoke(tool_name, arguments))
    except Exception as exc:
        return error_result(tool_name, exc)


def _terminal_or_checkpoint(tool_name: str, result: Any) -> Any:
    """Keep the web model inside a runner loop until it has a terminal result.

    A long task is persisted by the Worker, but the MCP request itself must
    return before an upstream proxy deadline.  Returning a plain JSON object
    for that checkpoint was easy for a chat model to mistake for "started".
    Raise a real MCP ToolError instead: the model receives an explicit tool
    error, including the task id and next action, and must call monitor_task or
    repair/retry rather than emitting a user-facing progress sentence.
    """
    if _direct_mode():
        return normalize_result(tool_name, result)
    if isinstance(result, dict) and result.get("terminal") is True:
        result = _ensure_terminal_user_answer(result)

    if isinstance(result, dict):
        status = str(result.get("status") or "").lower()
        workflow_status = str(result.get("workflow_status") or "").lower()
        active_statuses = {"queued", "running", "processing", "detached", "in_progress"}
        terminal_statuses = {"success", "failed", "cancelled"}
        active_task = bool(
            result.get("detached")
            or status in active_statuses
            or (
                result.get("task_id")
                and status not in terminal_statuses
                and (
                    result.get("terminal") is not True
                    or result.get("completed") is not True
                )
            )
        )
        explicit_terminal_failure = bool(
            result.get("terminal") is True
            and result.get("completed") is False
            and workflow_status in {
                "terminal_failure",
                "terminal_cancelled",
                "terminal_timeout",
                "terminal_cycle_limit",
                "terminal_sampling_unavailable",
                "self_check_terminal_failure",
            }
            and not active_task
        )
        # A result with completed=false is never a normal final answer.  The
        # only exception is an explicitly recorded terminal failure/timeout
        # after the task table has confirmed that no Worker task is active.
        # This closes the former hole where terminal_timeout or cycle_limit
        # could be passed through while the Worker was still running.
        requires_checkpoint = bool(
            active_task
            or result.get("continue_required")
            or result.get("repair_required")
            or result.get("recoverable_error")
            or result.get("final_answer_allowed") is False
            or workflow_status in {
                "repair_required",
                "recoverable_failure",
                "self_check_repair_required",
                "in_progress",
            }
            or (
                workflow_status == "terminal_sampling_unavailable"
                and result.get("completed") is not True
            )
            or (
                result.get("user_visible_final") is False
                and result.get("completed") is not True
            )
            or (
                result.get("completed") is not True
                and not explicit_terminal_failure
            )
        )
    else:
        requires_checkpoint = False

    if isinstance(result, dict) and requires_checkpoint:
        checkpoint = {
            key: result.get(key)
            for key in (
                "task_id",
                "status",
                "profile",
                "completed",
                "terminal",
                "success",
                "user_visible_final",
                "final_answer_allowed",
                "detached",
                "continue_required",
                "next_action",
                "message",
                "error",
                "workflow_status",
                "repair_required",
                "recoverable_error",
                "error_class",
                "repair_hint",
                "self_check_required",
                "self_check_request",
                "web_search",
                "web_search_required",
                "diagnostic_evidence",
                "recovery_budget_exhausted",
                "task_snapshot",
                "terminal_reason",
                "terminal_proof",
                "timeout",
                "cycle_limit",
                "server_handoff",
                "handoff_id",
                "handoff_goal",
                "handoff_chat_tab_id",
                "handoff_source",
            )
            if key in result
        }
        if result.get("self_check_required"):
            next_instruction = "Call self_check immediately, use its local/web evidence, repair, and do not answer yet."
        elif result.get("task_id"):
            next_instruction = "Continue the same task automatically and do not answer yet."
        else:
            next_instruction = (
                "No task was started. Follow next_action, call the indicated replacement "
                "tool automatically, and do not answer yet."
            )
        raise ToolError(
            f"{tool_name} returned a nonterminal checkpoint; this is not a user-facing result. "
            + next_instruction
            + " "
            + json.dumps(checkpoint, ensure_ascii=False, default=str)
        )
    return result


async def _invoke_with_heartbeat(
    tool_name: str,
    arguments: dict[str, Any],
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Keep a long runner response alive until it has a real result.

    The Worker task is persistent, but a silent MCP response lets the web
    client look idle and can make ChatGPT emit a progress paragraph before the
    Worker reaches NVT/NPT. Streamable HTTP can carry these optional
    notifications while the same tool call is open, so the visible turn stays
    in its tool-call state. The terminal guard is still applied by each caller.
    """
    if _direct_mode():
        return await _invoke_tool(tool_name, arguments)
    call = asyncio.create_task(_invoke_tool(tool_name, arguments))
    heartbeat = 0
    try:
        while not call.done():
            await asyncio.sleep(10)
            if call.done():
                break
            heartbeat += 1
            if ctx is not None:
                message = (
                    f"{tool_name} is still running; heartbeat {heartbeat}. "
                    "Do not produce a user-facing answer until the tool returns a terminal result."
                )
                try:
                    await ctx.info(message, logger_name="xiao-runner")
                    await ctx.report_progress(float(heartbeat), None, message)
                except Exception:
                    # Logging/progress is optional in MCP clients. Never turn
                    # an unsupported notification into a task failure.
                    pass
        result = await call
        # A low-level browser action can be the first observable edge of a
        # server-owned autonomous handoff.  Its supervisor may still be
        # running for several minutes.  Surface that state as a real MCP
        # ToolError, not ordinary JSON: otherwise the web model can treat a
        # bridge 504/checkpoint as a user-facing failure and end the turn
        # while the actual task is still executing.  The next browser_wait or
        # monitor_task then rebinds to the retained handoff and eventually
        # receives the terminal result.
        if (
            tool_name.startswith("browser_")
            and isinstance(result, dict)
            and result.get("server_handoff")
            and result.get("terminal") is not True
        ):
            return _terminal_or_checkpoint(tool_name, result)
        return result
    finally:
        if not call.done():
            # Do not cancel the server-owned supervisor merely because
            # ChatGPT/Cloudflare closed the long HTTP stream.  The old code
            # killed the orchestration here, leaving a running Worker task
            # with no code left to enqueue the production stage.
            _retain_background_call(call)


async def _invoke_one_shot_supervisor(
    goal: str,
    step: dict[str, Any],
    acceptance: list[dict[str, Any]] | None = None,
    timeout_seconds: int = 3_600,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Route compatibility runner calls through the same closed-loop engine.

    Older clients may have cached ``run_python_script``/``run_md_simulation``
    instead of the new primary tool.  They must still get the server-side
    diagnose→repair→retry behavior, rather than a recovery checkpoint that
    asks the web model to start a second turn.
    """

    if _direct_mode():
        names = {
            "python": "run_python_script", "powershell": "run_powershell_workflow",
            "cmd": "run_cmd_workflow", "terminal": "run_terminal_workflow",
            "wsl": "run_wsl_workflow", "project_build": "run_project_build",
            "md_simulation": "run_md_simulation", "production_md": "run_production_md",
        }
        return await _invoke_tool(names[step["action"]], step.get("args", {}))

    # A web ChatGPT client can choose a compatibility runner (most commonly
    # WSL diagnostics) before it has represented the user's actual multi-step
    # goal.  Let the MCP recover that goal from the same conversation and run
    # the complete supervisor before this short runner call becomes visible.
    handoff = await registry()._maybe_handoff_runner_goal(goal, step, ctx)
    if handoff is not None:
        return _terminal_or_checkpoint("run_autonomous_workflow", handoff)

    workflow = {"steps": [step], "acceptance": acceptance or []}
    return await _invoke_with_heartbeat(
        "run_autonomous_workflow",
        {
            "goal": goal,
            "workflow": workflow,
            "acceptance": acceptance or [],
            "timeout_seconds": timeout_seconds,
            "ctx": ctx,
        },
        ctx,
    )


mcp = FastMCP(
    "web_mcp",
    instructions=MCP_INSTRUCTIONS,
    streamable_http_path="/mcp",
    json_response=True,
    stateless_http=True,
)


@mcp.tool()
async def list_directory(path: str = ".", recursive: bool = False, limit: int = 500) -> dict[str, Any]:
    """List files and directories inside the configured workspace."""
    return await _invoke_tool("list_directory", {"path": path, "recursive": recursive, "limit": limit})


@mcp.tool()
async def get_path_info(path: str = ".") -> dict[str, Any]:
    """Return metadata for an allowed file or directory."""
    return await _invoke_tool("get_path_info", {"path": path})


@mcp.tool()
async def create_directory(path: str, parents: bool = True, exist_ok: bool = False) -> dict[str, Any]:
    """Create a directory inside an allowed workspace."""
    return await _invoke_tool("create_directory", {"path": path, "parents": parents, "exist_ok": exist_ok})


@mcp.tool()
async def read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> dict[str, Any]:
    """Read a UTF-8 text file from the configured workspace."""
    return await _invoke_tool("read_file", {"path": path, "start_line": start_line, "end_line": end_line})


@mcp.tool()
async def write_file(path: str, content: str, create_dirs: bool = False) -> dict[str, Any]:
    """Atomically write a UTF-8 text file in the configured workspace."""
    return await _invoke_tool("write_file", {"path": path, "content": content, "create_dirs": create_dirs})


@mcp.tool()
async def patch_file(path: str, find: str, replace: str, expected_count: int = 1) -> dict[str, Any]:
    """Replace an exact text fragment in one UTF-8 file with an expected-match guard."""
    return await _invoke_tool("patch_file", {"path": path, "find": find, "replace": replace, "expected_count": expected_count})


@mcp.tool()
async def copy_path(source: str, destination: str, overwrite: bool = False) -> dict[str, Any]:
    """Copy an allowed file or directory to another allowed workspace path."""
    return await _invoke_tool("copy_path", {"source": source, "destination": destination, "overwrite": overwrite})


@mcp.tool()
async def move_path(source: str, destination: str, overwrite: bool = False) -> dict[str, Any]:
    """Move an allowed file or directory to another allowed workspace path."""
    return await _invoke_tool("move_path", {"source": source, "destination": destination, "overwrite": overwrite})


@mcp.tool()
async def delete_path(path: str, recursive: bool = False, confirm: bool = False) -> dict[str, Any]:
    """Delete an allowed path; confirm=true is required and workspace roots cannot be deleted."""
    return await _invoke_tool("delete_path", {"path": path, "recursive": recursive, "confirm": confirm})


@mcp.tool()
async def search_file(
    query: str,
    path: str = ".",
    case_sensitive: bool = False,
    max_results: int = 200,
    include_paths: bool = True,
    max_depth: int = 32,
) -> dict[str, Any]:
    """Search recursively through names/paths and UTF-8 file contents.

    Path/name matches are included by default so nested projects and scripts
    can be discovered without the caller knowing their exact directory. Natural
    language/repository-name tokens use a conservative semantic fallback, and
    each path result reports whether it was an exact or semantic match.
    """
    return await _invoke_tool(
        "search_file",
        {
            "query": query,
            "path": path,
            "case_sensitive": case_sensitive,
            "max_results": max_results,
            "include_paths": include_paths,
            "max_depth": max_depth,
        },
    )


@mcp.tool()
async def find_paths(
    query: str,
    path: str = ".",
    kind: str = "any",
    case_sensitive: bool = False,
    max_results: int = 200,
    max_depth: int = 32,
) -> dict[str, Any]:
    """Find nested files/directories by exact or semantic name/path match.

    Use this first when a repository was cloned under a local project-specific
    name. For example, a query mentioning ``mds-gromacs-automation`` can find
    an ``MDS/AUTO_file/automated_scripts`` checkout without a hard-coded path.
    """
    return await _invoke_tool(
        "find_paths",
        {
            "query": query,
            "path": path,
            "kind": kind,
            "case_sensitive": case_sensitive,
            "max_results": max_results,
            "max_depth": max_depth,
        },
    )


@mcp.tool()
async def inspect_project(path: str = ".") -> dict[str, Any]:
    """Inspect project manifests and a bounded file tree."""
    return await _invoke_tool("inspect_project", {"path": path})


@mcp.tool()
async def modify_project(path: str = ".", files: dict[str, str] | None = None, changes: list[dict[str, Any]] | None = None, create_dirs: bool = True) -> dict[str, Any]:
    """Apply explicit file changes inside an allowed workspace."""
    return await _invoke_tool("modify_project", {"path": path, "files": files, "changes": changes, "create_dirs": create_dirs})


@mcp.tool()
async def get_system_info() -> dict[str, Any]:
    """Return non-secret local system and runtime information."""
    return await _invoke_tool("get_system_info")


@mcp.tool()
async def list_processes(name_contains: str | None = None, limit: int = 100) -> dict[str, Any]:
    """List local processes without environment variables or command secrets."""
    return await _invoke_tool("list_processes", {"name_contains": name_contains, "limit": limit})


@mcp.tool()
async def list_wsl_directory(path: str = "/", distribution: str | None = None, recursive: bool = False, limit: int = 500, timeout: int = 30) -> dict[str, Any]:
    """List entries in a WSL Linux directory. Use Linux paths such as /, /home, or /mnt/d."""
    return await _invoke_tool(
        "list_wsl_directory",
        {"path": path, "distribution": distribution, "recursive": recursive, "limit": limit, "timeout": timeout},
    )


@mcp.tool()
async def discover_environment(
    request: str = "",
    cwd: str | None = None,
    distribution: str | None = None,
    roots: list[str] | None = None,
    max_results: int = 120,
    read_limit: int = 6_000,
    max_depth: int = 32,
) -> dict[str, Any]:
    """Discover bounded local project, runtime, executable and WSL context without executing discovered files."""
    return await _invoke_tool(
        "discover_environment",
        {
            "request": request,
            "cwd": cwd,
            "distribution": distribution,
            "roots": roots,
            "max_results": max_results,
            "read_limit": read_limit,
            "max_depth": max_depth,
        },
    )


@mcp.tool()
async def self_check(
    operation: str = "",
    task_id: str | None = None,
    error_text: str = "",
    expected_paths: list[str] | None = None,
    browser_check: bool = False,
    auto_search: bool = True,
    search_domains: list[str] | None = None,
    search_max_results: int = 5,
) -> dict[str, Any]:
    """Run a read-only local self-check and automatically search public web evidence.

    Call this immediately after a recoverable runner, browser, bridge, path,
    dependency or runtime error. It never edits files or starts a task. When
    repair_required=true or final_answer_allowed=false is returned, use the
    local evidence and web results to make the smallest safe repair, then
    retry with a changed invocation and fresh checks. Queries are redacted
    before public search; do not put secrets in error_text.
    """
    return await _invoke_tool(
        "self_check",
        {
            "operation": operation,
            "task_id": task_id,
            "error_text": error_text,
            "expected_paths": expected_paths,
            "browser_check": browser_check,
            "auto_search": auto_search,
            "search_domains": search_domains,
            "search_max_results": search_max_results,
        },
    )


@mcp.tool()
async def search_web(
    query: str,
    max_results: int = 5,
    domains: list[str] | None = None,
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    """Search public web documentation with a bounded, public-safe query.

    Returns result metadata only. Never include local file paths, tokens,
    cookies, credentials or private task data in the query.
    """
    return await _invoke_tool(
        "search_web",
        {
            "query": query,
            "max_results": max_results,
            "domains": domains,
            "timeout_seconds": timeout_seconds,
        },
    )


@mcp.tool()
async def browser_start(url: str | None = None) -> dict[str, Any]:
    """Compatibility alias that opens a visible local Edge tab."""
    return await _invoke_tool("browser_start", {"url": url})


@mcp.tool()
async def browser_list_tabs() -> dict[str, Any]:
    """List tabs in the visible user-session browser controlled by Xiaoruo1."""
    return await _invoke_tool("browser_list_tabs")


@mcp.tool()
async def browser_list_downloads(limit: int = 20, ctx: Context | None = None) -> dict[str, Any]:
    """List recent Edge downloads with the real local file path and state.

    Chromium download records are preferred; when an older unpacked extension
    lacks that command, the bridge reads Edge's configured download directory.
    Never substitute a guessed Downloads path for the returned file_path.
    """
    return await _invoke_with_heartbeat(
        "browser_list_downloads", {"limit": limit, "__mcp_ctx": ctx}, ctx
    )


@mcp.tool()
async def browser_open_tab(url: str | None = None) -> dict[str, Any]:
    """Open a visible local browser tab at an HTTP(S) URL."""
    return await _invoke_tool("browser_open_tab", {"url": url})


@mcp.tool()
async def browser_navigate(tab_id: str, url: str, timeout: int = 30_000) -> dict[str, Any]:
    """Navigate one local browser tab to an HTTP(S) URL."""
    return await _invoke_tool("browser_navigate", {"tab_id": tab_id, "url": url, "timeout": timeout})


@mcp.tool()
async def browser_read_page(
    tab_id: str | None = None,
    max_chars: int = 20_000,
    settle_ms: int = 750,
    include_frames: bool = False,
    selector: str | None = None,
    mode: str = "auto",
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Read text and visible interactive-element metadata from a local Edge page.

    In a multi-step task always pass the explicit tab_id returned by
    browser_list_tabs; omit it only for a one-shot active-tab inspection.
    Large GitHub and SPA pages use a fast content-root strategy automatically.
    selector may be a CSS selector for a
    specific region; mode is auto, visible, or fast. Set include_frames=true to
    attempt accessible frames. The result includes visible inputs, buttons and
    links with ids and usable CSS selectors when available; pass those exact
    selectors to browser_type/browser_click rather than guessing generic
    textarea/input targets. Browser-internal pages such as edge://extensions
    and the Web Store remain protected by Chromium and cannot be injected into
    by an extension.
    """
    return await _invoke_with_heartbeat(
        "browser_read_page",
        {
            "tab_id": tab_id,
            "max_chars": max_chars,
            "settle_ms": settle_ms,
            "include_frames": include_frames,
            "selector": selector,
            "mode": mode,
            "__mcp_ctx": ctx,
        },
        ctx,
    )


@mcp.tool()
async def browser_read(tab_id: str | None = None, max_chars: int = 20_000, ctx: Context | None = None) -> dict[str, Any]:
    """Compatibility alias for reading bounded visible text and interactive metadata from a local Edge page."""
    return await _invoke_with_heartbeat(
        "browser_read", {"tab_id": tab_id, "max_chars": max_chars, "__mcp_ctx": ctx}, ctx
    )


@mcp.tool()
async def browser_click(
    tab_id: str,
    by: str | None = None,
    selector: str | None = None,
    name: str | None = None,
    exact: bool = False,
    timeout: int = 15_000,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Click one addressed element and verify that the page state changes.

    Use by='css' with selector for CSS, by='text' with selector or name for
    visible text, or by='role' with selector as the role and name as the
    accessible name. If by is omitted, the bridge infers an unmistakable CSS
    selector and otherwise keeps role semantics. For downloads, use
    ``download.file_path`` or ``downloads[].file_path`` from the result as the
    authoritative local artifact; do not guess a browser Downloads directory.
    If verification is ``click_sent_no_immediate_state_change``, the action was
    dispatched but the page is slow: call browser_wait/browser_read_page and
    continue the same task; do not stop or report completion from that result.
    """
    return await _invoke_with_heartbeat(
        "browser_click",
        {"tab_id": tab_id, "by": by, "selector": selector, "name": name, "exact": exact, "timeout": timeout, "__mcp_ctx": ctx},
        ctx,
    )


@mcp.tool()
async def browser_type(
    tab_id: str,
    text: str,
    by: str | None = None,
    selector: str | None = None,
    name: str | None = None,
    exact: bool = False,
    clear: bool = True,
    timeout: int = 15_000,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Type into one addressed element and verify the result.

    For a native HTML select/combobox, pass its exact CSS selector from
    browser_read_page and use ``text`` equal to the option's visible label;
    the same tool selects that option and verifies the resulting value. This
    keeps older web-chat tool sessions compatible with native dropdowns.
    """
    return await _invoke_with_heartbeat(
        "browser_type",
        {
            "tab_id": tab_id,
            "text": text,
            "by": by,
            "selector": selector,
            "name": name,
            "exact": exact,
            "clear": clear,
            "timeout": timeout,
            "__mcp_ctx": ctx,
        },
        ctx,
    )


@mcp.tool()
async def browser_press(
    tab_id: str,
    key: str,
    by: str | None = None,
    selector: str | None = None,
    name: str | None = None,
    exact: bool = False,
    timeout: int = 15_000,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Press one keyboard key in the addressed local Edge page target.

    Use this for keyboard-driven web controls such as mention autocomplete:
    after typing ``@Xiaoruo1`` press ``ArrowDown``/``Enter`` only after the
    visible menu is present. A key press is never task completion.
    """
    return await _invoke_with_heartbeat(
        "browser_press",
        {
            "tab_id": tab_id,
            "key": key,
            "by": by,
            "selector": selector,
            "name": name,
            "exact": exact,
            "timeout": timeout,
            "__mcp_ctx": ctx,
        },
        ctx,
    )


@mcp.tool()
async def browser_instances(timeout: int = 10_000, ctx: Context | None = None) -> dict[str, Any]:
    """Discover connected authenticated browser instances, capabilities, generation and tabs.

    Xiaoruo v72 prefers authenticated local Named Pipe; Edge keeps its extension.
    Does not launch browsers or copy profile/login data. Select returned ids for
    browser_control; reused or ambiguous tab ids must never be guessed.
    """
    return await _invoke_with_heartbeat("browser_instances", {"timeout": timeout, "__mcp_ctx": ctx}, ctx)


@mcp.tool()
async def browser_control(browser_id: str,
    action: Literal["status", "list_tabs", "open_tab", "close_tab", "activate_tab", "navigate", "read", "click", "type", "press", "select", "scroll", "wait", "screenshot", "downloads", "task_status", "reconcile"],
    tab_id: str | None = None, arguments: dict[str, Any] | None = None,
    operation_id: str | None = None, confirm: bool = False, timeout: int = 30_000,
    ctx: Context | None = None) -> dict[str, Any]:
    """Use the unified adapter for one explicit browser instance and tab.

    Writes require confirm=true, stable operation_id and arguments.expected_generation
    from discovery. Unknown outcomes require a fresh read and explicit evidence-based
    reconcile, never blind replay. No hidden model or arbitrary JavaScript.
    """
    return await _invoke_with_heartbeat("browser_control", {"browser_id": browser_id, "action": action,
        "tab_id": tab_id, "arguments": arguments, "operation_id": operation_id, "confirm": confirm,
        "timeout": timeout, "__mcp_ctx": ctx}, ctx)


@mcp.tool()
async def browser_debug_control(browser_id: str, tab_id: str,
    action: Literal["status", "attach", "snapshot", "detach"] = "status",
    arguments: dict[str, Any] | None = None, operation_id: str | None = None,
    confirm: bool = False, timeout: int = 10_000, ctx: Context | None = None) -> dict[str, Any]:
    """Opt-in bounded Chromium debugging; unavailable permission yields a real refusal.

    Does not request debugger permission automatically or expose arbitrary scripts,
    cookies, credentials or a network debugger port. User detach remains supported.
    """
    return await _invoke_with_heartbeat("browser_debug_control", {"browser_id": browser_id, "tab_id": tab_id,
        "action": action, "arguments": arguments, "operation_id": operation_id,
        "confirm": confirm, "timeout": timeout, "__mcp_ctx": ctx}, ctx)


@mcp.tool()
async def browser_upload_file(tab_id: str, selector: str, file_path: str, idempotency_key: str,
                              confirm: bool = False, timeout: int = 30_000,
                              ctx: Context | None = None) -> dict[str, Any]:
    """Select an authorized local MP4/cover using real file bytes in the current Edge tab.

    First read file_inputs and use one exact observed selector. A generic name
    matching multiple inputs is rejected. confirm=true and a stable unique
    idempotency_key are required. A file_selected result does not prove server
    receipt or platform processing. On timeout/cancellation query upload_status
    and read the webpage before deciding next steps; never blindly replay.
    Only approved media inside the fixed external dated video project is allowed.
    """
    return await _invoke_with_heartbeat("browser_upload_file", {
        "tab_id": tab_id, "selector": selector, "file_path": file_path,
        "idempotency_key": idempotency_key, "confirm": confirm, "timeout": timeout, "__mcp_ctx": ctx}, ctx)


@mcp.tool()
async def browser_upload_status(idempotency_key: str, ctx: Context | None = None) -> dict[str, Any]:
    """Read a prior upload selection attempt without repeating its side effects."""
    return await _invoke_with_heartbeat("browser_upload_status", {"idempotency_key": idempotency_key, "__mcp_ctx": ctx}, ctx)


@mcp.tool()
async def video_project_path(date: str | None = None, create: bool = False,
                             ctx: Context | None = None) -> dict[str, Any]:
    """Resolve the fixed external video root/yyyy_MM/dd, optionally creating it.

    date is YYYY-MM-DD, default today in Asia/Shanghai. Never save video material
    or finished videos inside the MCP program directory.
    """
    return await _invoke_with_heartbeat("video_project_path", {"date": date, "create": create, "__mcp_ctx": ctx}, ctx)


@mcp.tool()
async def browser_select_option(
    tab_id: str,
    option: str,
    option_by: str = "label",
    by: str | None = None,
    selector: str | None = None,
    name: str | None = None,
    exact: bool = False,
    timeout: int = 15_000,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Select an option in a native HTML select/combobox and verify it.

    Use browser_read_page first and pass the exact CSS selector returned for
    the select. option_by may be label (visible option text), value, text, or
    index (zero-based). This is needed for ordinary web forms that cannot be
    completed by click and type alone.
    """
    return await _invoke_with_heartbeat(
        "browser_select_option",
        {
            "tab_id": tab_id,
            "option": option,
            "option_by": option_by,
            "by": by,
            "selector": selector,
            "name": name,
            "exact": exact,
            "timeout": timeout,
            "__mcp_ctx": ctx,
        },
        ctx,
    )


@mcp.tool()
async def browser_wait(tab_id: str, milliseconds: int = 1_000, ctx: Context | None = None) -> dict[str, Any]:
    """Wait briefly for the addressed browser tab, then let ChatGPT inspect the actual page state."""
    return await _invoke_with_heartbeat(
        "browser_wait", {"tab_id": tab_id, "milliseconds": milliseconds, "__mcp_ctx": ctx}, ctx
    )


@mcp.tool()
async def browser_screenshot(tab_id: str, full_page: bool = False, ctx: Context | None = None) -> dict[str, Any]:
    """Save a screenshot of a local browser tab under the private data directory."""
    return await _invoke_with_heartbeat(
        "browser_screenshot", {"tab_id": tab_id, "full_page": full_page, "__mcp_ctx": ctx}, ctx
    )


@mcp.tool()
async def browser_close_tab(tab_id: str) -> dict[str, Any]:
    """Close one local browser tab."""
    return await _invoke_tool("browser_close_tab", {"tab_id": tab_id})


@mcp.tool()
async def git_status(path: str = ".") -> dict[str, Any]:
    """Read Git branch and working-tree status for an allowed project."""
    return await _invoke_tool("git_status", {"path": path})


@mcp.tool()
async def git_diff(path: str = ".", staged: bool = False, file_path: str | None = None, max_chars: int = 200000) -> dict[str, Any]:
    """Read a bounded Git diff for an allowed project."""
    return await _invoke_tool("git_diff", {"path": path, "staged": staged, "file_path": file_path, "max_chars": max_chars})


@mcp.tool()
async def git_log(path: str = ".", limit: int = 20) -> dict[str, Any]:
    """Read recent Git commits for an allowed project."""
    return await _invoke_tool("git_log", {"path": path, "limit": limit})


@mcp.tool()
async def git_branches(path: str = ".") -> dict[str, Any]:
    """Read local and remote Git branch names for an allowed project."""
    return await _invoke_tool("git_branches", {"path": path})


@mcp.tool()
async def git_commit(path: str, message: str, paths: list[str], confirm: bool = False) -> dict[str, Any]:
    """Create a Git commit after explicit confirmation and explicit path selection."""
    return await _invoke_tool("git_commit", {"path": path, "message": message, "paths": paths, "confirm": confirm})


@mcp.tool()
async def run_python_script(script_path: str | None = None, args: list[str] | None = None, cwd: str = ".", timeout: int | None = None, code: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """Queue the explicitly supplied Python task; poll task_status for real execution results."""
    result = await _invoke_one_shot_supervisor(
        f"Run the requested Python task to verified completion in {cwd}.",
        {"action": "python", "args": {"script_path": script_path, "args": args or [], "cwd": cwd, "timeout": timeout, "code": code}},
        timeout_seconds=timeout or 3_600,
        ctx=ctx,
    )
    return _terminal_or_checkpoint("run_python_script", result)


@mcp.tool()
async def run_wsl_workflow(workflow: str = "diagnostics", commands: list[str] | None = None, cwd: str = ".", distribution: str | None = None, timeout: int | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """Queue the explicit WSL workflow. Use the dedicated MD runner for maintained GLYCAM/GROMACS checks."""
    result = await _invoke_one_shot_supervisor(
        f"Run the WSL workflow {workflow!r} to verified completion in {cwd}.",
        {"action": "wsl", "args": {"workflow": workflow, "commands": commands or [], "cwd": cwd, "distribution": distribution, "timeout": timeout}},
        timeout_seconds=timeout or 3_600,
        ctx=ctx,
    )
    return _terminal_or_checkpoint("run_wsl_workflow", result)


@mcp.tool()
async def run_powershell_workflow(workflow: str = "diagnostics", commands: list[str] | None = None, cwd: str = ".", timeout: int | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """Queue the explicit PowerShell workflow; returns a durable task_id in direct mode."""
    result = await _invoke_one_shot_supervisor(
        f"Run the PowerShell workflow {workflow!r} to verified completion in {cwd}.",
        {"action": "powershell", "args": {"workflow": workflow, "commands": commands or [], "cwd": cwd, "timeout": timeout}},
        timeout_seconds=timeout or 3_600,
        ctx=ctx,
    )
    return _terminal_or_checkpoint("run_powershell_workflow", result)


@mcp.tool()
async def run_cmd_workflow(workflow: str = "diagnostics", commands: list[str] | None = None, cwd: str = ".", timeout: int | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """Queue the explicit CMD workflow; poll task_status and inspect output."""
    result = await _invoke_one_shot_supervisor(
        f"Run the Windows CMD workflow {workflow!r} to verified completion in {cwd}.",
        {"action": "cmd", "args": {"workflow": workflow, "commands": commands or [], "cwd": cwd, "timeout": timeout}},
        timeout_seconds=timeout or 3_600,
        ctx=ctx,
    )
    return _terminal_or_checkpoint("run_cmd_workflow", result)


@mcp.tool()
async def run_terminal_workflow(shell: str = "powershell", workflow: str = "diagnostics", commands: list[str] | None = None, cwd: str = ".", distribution: str | None = None, timeout: int | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """Queue an explicit deterministic terminal workflow and return its real task state."""
    result = await _invoke_one_shot_supervisor(
        f"Run the {shell} terminal workflow {workflow!r} to verified completion in {cwd}.",
        {"action": "terminal", "args": {"shell": shell, "workflow": workflow, "commands": commands or [], "cwd": cwd, "distribution": distribution, "timeout": timeout}},
        timeout_seconds=timeout or 3_600,
        ctx=ctx,
    )
    return _terminal_or_checkpoint("run_terminal_workflow", result)


@mcp.tool()
async def run_project_build(build_type: str = "python", cwd: str = ".", timeout: int | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """Queue the specified project build, then verify its task result and artifacts."""
    result = await _invoke_one_shot_supervisor(
        f"Build and verify the {build_type} project in {cwd}.",
        {"action": "project_build", "args": {"build_type": build_type, "cwd": cwd, "timeout": timeout}},
        timeout_seconds=timeout or 3_600,
        ctx=ctx,
    )
    return _terminal_or_checkpoint("run_project_build", result)


@mcp.tool()
async def run_md_simulation(script_path: str | None = None, args: list[str] | None = None, cwd: str = ".", timeout: int | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """Queue the maintained deterministic GLYCAM/GROMACS pipeline with real downloaded inputs. Inspect task_status and fresh EM/NVT/NPT evidence."""
    result = await _invoke_one_shot_supervisor(
        "Run the maintained GLYCAM/GROMACS bounded MD pipeline to verified EM/NVT/NPT completion.",
        {"action": "md_simulation", "args": {"script_path": script_path, "args": args or [], "cwd": cwd, "timeout": timeout}},
        timeout_seconds=timeout or 6 * 3_600,
        ctx=ctx,
    )
    return _terminal_or_checkpoint("run_md_simulation", result)


@mcp.tool()
async def run_production_md(
    system_dir: str | None = None,
    target_root: str = ".",
    duration_seconds: float = 300,
    work_id: str = "1",
    script_path: str | None = None,
    mdp: str | None = None,
    gpu: str | None = None,
    threads: int = 4,
    timeout: int | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Queue the maintained production MD script for the explicit system and duration; verify fresh output via task_status."""
    try:
        normalized_duration = float(duration_seconds)
    except (TypeError, ValueError) as exc:
        raise ToolError("duration_seconds must be a number") from exc
    wait_seconds = timeout or max(1_200, int(normalized_duration) + 900)
    result = await _invoke_one_shot_supervisor(
        "Run the user's existing production script to a verified terminal result.",
        {
            "action": "production_md",
            "args": {
                "system_dir": system_dir,
                "target_root": target_root,
                "duration_seconds": normalized_duration,
                "work_id": work_id,
                "script_path": script_path,
                "mdp": mdp,
                "gpu": gpu,
                "threads": threads,
                "timeout": timeout,
            },
        },
        acceptance=[{"type": "result_success"}],
        timeout_seconds=wait_seconds,
        ctx=ctx,
    )
    return _terminal_or_checkpoint("run_production_md", result)


@mcp.tool()
async def create_task(profile: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Queue a runner task and return its task id."""
    return await _invoke_tool("create_task", {"profile": profile, "payload": payload})


@mcp.tool()
async def run_workflow_until_complete(
    steps: list[dict[str, Any]],
    final_checks: list[dict[str, Any]] | None = None,
    max_attempts: int = 1,
    overall_timeout: int | None = None,
    wait_timeout: int | None = None,
    poll_interval_ms: int = 500,
    fresh_outputs: bool = True,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Execute only explicit finite steps and final_checks. Direct mode queues runner batches and returns task_id; use task_status until terminal."""
    if _direct_mode():
        return await _invoke_tool("run_workflow_until_complete", {
            "steps": steps, "final_checks": final_checks or [], "max_attempts": max_attempts,
            "overall_timeout": overall_timeout, "wait_timeout": wait_timeout,
            "poll_interval_ms": poll_interval_ms, "fresh_outputs": fresh_outputs,
        })
    handoff = await registry()._maybe_handoff_runner_goal(
        "Execute every supplied workflow step and pass every supplied final acceptance check.",
        {"steps": steps, "acceptance": final_checks or []},
        ctx,
    )
    if handoff is not None:
        return _terminal_or_checkpoint("run_workflow_until_complete", handoff)
    result = await _invoke_with_heartbeat(
        "run_autonomous_workflow",
        {
            "goal": "Execute every supplied workflow step and pass every supplied final acceptance check.",
            "workflow": {
                "steps": steps,
                "acceptance": final_checks or [],
                "max_attempts": max_attempts,
                "overall_timeout": overall_timeout,
                "wait_timeout": wait_timeout,
                "poll_interval_ms": poll_interval_ms,
                "fresh_outputs": fresh_outputs,
            },
            "acceptance": final_checks or [],
            "timeout_seconds": overall_timeout or 3_600,
            "ctx": ctx,
        },
        ctx,
    )
    return _terminal_or_checkpoint("run_workflow_until_complete", result)


@mcp.tool()
async def run_autonomous_workflow(
    goal: str,
    workflow: dict[str, Any] | None = None,
    acceptance: list[dict[str, Any]] | None = None,
    max_cycles: int = 48,
    timeout_seconds: int = 3_600,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Compatibility tool. Direct mode executes only explicit workflow.steps; goal-only input returns needs_chatgpt_planning without Sampling."""
    if _direct_mode():
        return await _invoke_tool("run_autonomous_workflow", {
            "goal": goal, "workflow": workflow, "acceptance": acceptance,
            "max_cycles": max_cycles, "timeout_seconds": timeout_seconds,
        })
    handoff = await registry()._maybe_handoff_runner_goal(
        goal,
        workflow if isinstance(workflow, dict) else {"acceptance": acceptance or []},
        ctx,
    )
    if handoff is not None:
        return _terminal_or_checkpoint("run_autonomous_workflow", handoff)

    result = await _invoke_with_heartbeat(
        "run_autonomous_workflow",
        {
            "goal": goal,
            "workflow": workflow,
            "acceptance": acceptance,
            "max_cycles": max_cycles,
            "timeout_seconds": timeout_seconds,
            "ctx": ctx,
        },
        ctx,
    )
    return _terminal_or_checkpoint("run_autonomous_workflow", result)


@mcp.tool()
async def run_workflow(workflow: dict[str, Any], timeout_seconds: int = 3_600, ctx: Context | None = None) -> dict[str, Any]:
    """Execute a finite explicitly specified workflow. Direct mode never invokes Sampling or infers steps from a goal."""
    if _direct_mode():
        return await _invoke_tool("run_workflow", {"workflow": workflow, "timeout_seconds": timeout_seconds})
    handoff = await registry()._maybe_handoff_runner_goal(
        str(workflow.get("name") or "Execute the supplied compatibility workflow to verified completion"),
        workflow,
        ctx,
    )
    if handoff is not None:
        return _terminal_or_checkpoint("run_workflow", handoff)
    result = await _invoke_with_heartbeat(
        "run_autonomous_workflow",
        {
            "goal": str(workflow.get("name") or "Execute the supplied compatibility workflow to verified completion"),
            "workflow": workflow,
            "acceptance": workflow.get("acceptance", workflow.get("acceptance_checks", workflow.get("final_checks", []))) if isinstance(workflow, dict) else [],
            "timeout_seconds": timeout_seconds,
            "ctx": ctx,
        },
        ctx,
    )
    return _terminal_or_checkpoint("run_workflow", result)


@mcp.tool()
async def monitor_task(task_id: str, timeout_seconds: int = 900, poll_interval_seconds: float = 0.5, ctx: Context | None = None) -> dict[str, Any] | None:
    """Read the actual state of one existing task. Direct mode returns immediately; queued/running are normal results."""
    result = await _invoke_tool(
        "monitor_task",
        {"task_id": task_id, "timeout_seconds": timeout_seconds, "poll_interval_seconds": poll_interval_seconds},
    )
    return _terminal_or_checkpoint("monitor_task", result)


@mcp.tool()
async def wait_task(task_id: str, timeout_seconds: int = 90, poll_interval_seconds: float = 1.0, ctx: Context | None = None) -> dict[str, Any] | None:
    """Wait briefly for an existing deterministic task. Direct mode bounds the wait to five seconds."""
    result = await _invoke_tool(
        "wait_task",
        {"task_id": task_id, "timeout_seconds": timeout_seconds, "poll_interval_seconds": poll_interval_seconds},
    )
    return _terminal_or_checkpoint("wait_task", result)


@mcp.tool()
async def list_tasks(limit: int = 100) -> list[dict[str, Any]]:
    """List recent task records and statuses."""
    return await _invoke_tool("list_tasks", {"limit": limit})


@mcp.tool()
async def read_task_output(task_id: str, stream: str = "stdout", max_chars: int = 16000, cursor: int = 0) -> dict[str, Any]:
    """Read a bounded stdout or stderr output file from a task."""
    return await _invoke_tool("read_task_output", {"task_id": task_id, "stream": stream, "max_chars": max_chars, "cursor": cursor})


@mcp.tool()
async def retry_task(task_id: str, confirm: bool = False) -> dict[str, Any]:
    """Create a new task from a previous finished task; confirm=true is required."""
    return await _invoke_tool("retry_task", {"task_id": task_id, "confirm": confirm})


@mcp.tool()
async def cancel_task(task_id: str) -> dict[str, Any] | None:
    """Request cancellation of a queued or running task."""
    return await _invoke_tool("cancel_task", {"task_id": task_id})


@mcp.tool()
async def list_conversations() -> list[dict[str, Any]]:
    """List saved local Xiaoruo1 conversations."""
    return await _invoke_tool("list_conversations")


@mcp.tool()
async def get_conversation(conversation_id: str) -> dict[str, Any] | None:
    """Read one saved local Xiaoruo1 conversation and its messages."""
    return await _invoke_tool("get_conversation", {"conversation_id": conversation_id})


@mcp.tool()
async def create_canvas(title: str, content: str = "", kind: str = "markdown", status: str = "draft") -> dict[str, Any]:
    """Create a revisioned Canvas document."""
    return await _invoke_tool("create_canvas", {"title": title, "content": content, "kind": kind, "status": status})


@mcp.tool()
async def get_canvas(canvas_id: str, revision: int | None = None) -> dict[str, Any] | None:
    """Read a Canvas and its selected revision."""
    return await _invoke_tool("get_canvas", {"canvas_id": canvas_id, "revision": revision})


@mcp.tool()
async def update_canvas(canvas_id: str, content: str, title: str | None = None, kind: str | None = None, status: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Create a new Canvas revision."""
    return await _invoke_tool("update_canvas", {"canvas_id": canvas_id, "content": content, "title": title, "kind": kind, "status": status, "metadata": metadata})


@mcp.tool()
async def list_canvas_history(canvas_id: str) -> list[dict[str, Any]]:
    """List Canvas revisions without returning document content."""
    return await _invoke_tool("list_canvas_history", {"canvas_id": canvas_id})


@mcp.tool()
async def restore_canvas_revision(canvas_id: str, revision_number: int) -> dict[str, Any] | None:
    """Restore a Canvas revision as a new revision."""
    return await _invoke_tool("restore_canvas_revision", {"canvas_id": canvas_id, "revision_number": revision_number})


@mcp.tool()
async def goal_start(goal: str, acceptance: list[str], workspace: str = ".", plan: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Save a goal and acceptance criteria. No hidden model or task is started."""
    return await _invoke_tool("goal_start", {"goal": goal, "acceptance": acceptance, "workspace": workspace, "plan": plan})


@mcp.tool()
async def goal_status(goal_id: str) -> dict[str, Any]:
    """Read saved checkpoints, errors, artifacts and linked Worker states."""
    return await _invoke_tool("goal_status", {"goal_id": goal_id})


@mcp.tool()
async def goal_checkpoint(goal_id: str, step_id: str, observation: dict[str, Any], next_intent: str | None = None,
                          idempotency_key: str | None = None, task_id: str | None = None, status: str = "completed",
                          artifacts: list[str] | None = None, error: dict[str, Any] | None = None, supersedes_task_id: str | None = None) -> dict[str, Any]:
    """Persist an observed step once per idempotency key; never re-execute it."""
    return await _invoke_tool("goal_checkpoint", {"goal_id": goal_id, "step_id": step_id, "observation": observation,
        "next_intent": next_intent, "idempotency_key": idempotency_key, "task_id": task_id, "status": status,
        "artifacts": artifacts, "error": error, "supersedes_task_id": supersedes_task_id})


@mcp.tool()
async def goal_finish(goal_id: str, outcome: str, evidence: dict[str, Any]) -> dict[str, Any]:
    """Record completed/failed/blocked. Completed requires checks with criterion, passed=true and evidence for every saved criterion."""
    return await _invoke_tool("goal_finish", {"goal_id": goal_id, "outcome": outcome, "evidence": evidence})


@mcp.tool()
async def goal_cancel(goal_id: str) -> dict[str, Any]:
    """Cancel a saved goal and request cancellation of its linked tasks."""
    return await _invoke_tool("goal_cancel", {"goal_id": goal_id})


@mcp.tool()
async def task_status(task_id: str) -> dict[str, Any]:
    """Return the actual state of an existing deterministic Worker task immediately."""
    return await _invoke_tool("task_status", {"task_id": task_id})


@mcp.tool()
async def task_log(task_id: str, cursor: int = 0, limit: int = 50) -> dict[str, Any]:
    """Read bounded ordered task logs; pass next_cursor to fetch the next page."""
    return await _invoke_tool("task_log", {"task_id": task_id, "cursor": cursor, "limit": limit})


@mcp.tool()
async def windows_snapshot(use_vision: bool = False, use_dom: bool = False) -> dict[str, Any]:
    """Read the real Windows desktop UI tree and optional private screenshot."""
    return await _invoke_tool("windows_snapshot", {"use_vision": use_vision, "use_dom": use_dom})


@mcp.tool()
async def windows_screenshot() -> dict[str, Any]:
    """Capture a private image of the interactive Windows desktop."""
    return await _invoke_tool("windows_screenshot", {})


@mcp.tool()
async def windows_control_status() -> dict[str, Any]:
    """Inspect Windows-MCP control/session status without sending desktop input."""
    return await _invoke_tool("windows_control_status", {})


@mcp.tool()
async def windows_click(loc: list[int] | None = None, label: int | None = None, button: str = "left", clicks: int = 1, confirm: bool = False) -> dict[str, Any]:
    """Click a target observed in Snapshot; requires enabled desktop control and explicit confirmation."""
    args = {"button": button, "clicks": clicks, **({"loc": loc} if loc is not None else {}), **({"label": label} if label is not None else {})}
    return await _invoke_tool("windows_click", {"arguments": args, "confirm": confirm})


@mcp.tool()
async def windows_type(text: str, label: int | None = None, loc: list[int] | None = None, clear: bool = False, press_enter: bool = False, confirm: bool = False) -> dict[str, Any]:
    """Type explicitly authorized text into an observed desktop target, then inspect a new Snapshot."""
    args = {"text": text, "clear": clear, "press_enter": press_enter, **({"loc": loc} if loc is not None else {}), **({"label": label} if label is not None else {})}
    return await _invoke_tool("windows_type", {"arguments": args, "confirm": confirm})


@mcp.tool()
async def windows_shortcut(shortcut: str, confirm: bool = False) -> dict[str, Any]:
    """Send an authorized shortcut (for example ctrl+s) to the interactive desktop."""
    return await _invoke_tool("windows_shortcut", {"arguments": {"shortcut": shortcut}, "confirm": confirm})


@mcp.tool()
async def windows_scroll(direction: str, wheel_times: int = 1, type: str = "vertical", loc: list[int] | None = None, confirm: bool = False) -> dict[str, Any]:
    """Scroll the observed Windows target and verify a subsequent Snapshot."""
    return await _invoke_tool("windows_scroll", {"arguments": {"direction": direction, "wheel_times": wheel_times, "type": type, **({"loc": loc} if loc is not None else {})}, "confirm": confirm})


@mcp.tool()
async def windows_app(mode: str, name: str, confirm: bool = False) -> dict[str, Any]:
    """Launch or switch an explicitly authorized app; arbitrary executable arguments are unavailable."""
    return await _invoke_tool("windows_app", {"arguments": {"mode": mode, "name": name}, "confirm": confirm})


_MCP_TOOL_FUNCTIONS = [
    goal_start, goal_status, goal_checkpoint, goal_finish, goal_cancel, task_status, task_log,
    windows_snapshot, windows_screenshot, windows_control_status, windows_click, windows_type,
    windows_shortcut, windows_scroll, windows_app,
    list_directory,
    get_path_info,
    create_directory,
    read_file,
    write_file,
    patch_file,
    copy_path,
    move_path,
    delete_path,
    search_file,
    find_paths,
    inspect_project,
    modify_project,
    get_system_info,
    list_processes,
    list_wsl_directory,
    discover_environment,
    self_check,
    search_web,
    browser_start,
    browser_list_tabs,
    browser_list_downloads,
    browser_open_tab,
    browser_navigate,
    browser_read_page,
    browser_read,
    browser_click,
    browser_type,
    browser_press,
    browser_select_option,
    browser_upload_file,
    browser_upload_status,
    browser_instances,
    browser_control,
    browser_debug_control,
    video_project_path,
    browser_wait,
    browser_screenshot,
    browser_close_tab,
    git_status,
    git_diff,
    git_log,
    git_branches,
    git_commit,
    run_python_script,
    run_wsl_workflow,
    run_powershell_workflow,
    run_cmd_workflow,
    run_terminal_workflow,
    run_project_build,
    run_md_simulation,
    run_production_md,
    create_task,
    run_workflow_until_complete,
    run_autonomous_workflow,
    run_workflow,
    monitor_task,
    wait_task,
    list_tasks,
    read_task_output,
    retry_task,
    cancel_task,
    list_conversations,
    get_conversation,
    create_canvas,
    get_canvas,
    update_canvas,
    list_canvas_history,
    restore_canvas_revision,
]


def _direct_public_tool(function):
    @wraps(function)
    async def invoke(*args, **kwargs):
        result = normalize_result(function.__name__, await function(*args, **kwargs))
        content = [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]
        native_browser_image = function.__name__ == "browser_control" and result.get("action") == "screenshot" and result.get("ok") is True
        if function.__name__ in {"windows_snapshot", "windows_screenshot"} or native_browser_image:
            private_root = (registry().ctx.settings.data_root / ("browser_screenshots" if native_browser_image else "windows_mcp")).resolve()
            for artifact in result.get("artifacts", []):
                if not isinstance(artifact, dict) or artifact.get("mime_type") not in {"image/png", "image/jpeg", "image/webp"}:
                    continue
                path = Path(artifact.get("path", "")).resolve()
                if path.is_relative_to(private_root) and path.is_file() and path.stat().st_size <= 12 * 1024 * 1024:
                    content.append(ImageContent(type="image", data=base64.b64encode(path.read_bytes()).decode("ascii"), mimeType=artifact["mime_type"]))
        return CallToolResult(content=content, structuredContent=result, isError=result["ok"] is False)
    # A direct tool always advertises the envelope, including formerly list-valued tools.
    invoke.__signature__ = inspect.signature(function, eval_str=True).replace(return_annotation=ToolEnvelope)
    invoke.__annotations__ = {**function.__annotations__, "return": ToolEnvelope}
    return invoke


def build_mcp(
    transport_security: TransportSecuritySettings | None = None,
    auth: AuthSettings | None = None,
    auth_server_provider: OAuthAuthorizationServerProvider[Any, Any, Any] | None = None,
    token_verifier: TokenVerifier | None = None,
    orchestration_mode: str = "legacy_sampling",
) -> FastMCP:
    """Create a fresh FastMCP instance for an application lifespan.

    The SDK's session manager is intentionally single-use, so factories are
    required for tests and for any process that constructs more than one app.
    """
    server = FastMCP(
        "web_mcp",
        instructions=MCP_INSTRUCTIONS,
        streamable_http_path="/mcp",
        # Stream notifications while long runners are active. A buffered JSON
        # response made the web client look idle and allowed it to answer
        # before the Worker reached a terminal EM/NVT/NPT result.
        json_response=False,
        stateless_http=True,
        retry_interval=1000,
        transport_security=transport_security,
        auth=auth,
        auth_server_provider=auth_server_provider,
        token_verifier=token_verifier,
    )
    read_tools = {"list_directory", "get_path_info", "read_file", "search_file", "find_paths", "inspect_project",
        "get_system_info", "list_processes", "discover_environment", "goal_status", "task_status", "task_log",
        "monitor_task", "wait_task", "list_tasks", "read_task_output", "browser_list_tabs", "browser_list_downloads",
        "browser_read_page", "browser_read", "browser_screenshot", "browser_upload_status", "browser_instances", "windows_snapshot", "windows_screenshot", "windows_control_status"}
    for function in _MCP_TOOL_FUNCTIONS:
        direct = orchestration_mode == "chatgpt_direct"
        server.tool(annotations=ToolAnnotations(readOnlyHint=function.__name__ in read_tools,
            destructiveHint=function.__name__ in {"delete_path", "move_path", "git_commit", "windows_click", "windows_type", "windows_shortcut", "windows_app"},
            openWorldHint=function.__name__.startswith(("browser_", "windows_", "run_"))))(_direct_public_tool(function) if direct else function)
    return server