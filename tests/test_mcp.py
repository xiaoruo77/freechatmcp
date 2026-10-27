from __future__ import annotations


def test_mcp_tool_registration():
    from web_mcp.mcp_server import mcp

    names = set(mcp._tool_manager._tools)
    required = {
        "list_directory", "get_path_info", "create_directory", "read_file", "write_file", "patch_file", "copy_path", "move_path", "delete_path", "search_file", "find_paths", "inspect_project", "modify_project",
        "get_system_info", "list_processes", "list_wsl_directory", "discover_environment", "self_check", "search_web", "git_status", "git_diff", "git_log", "git_branches", "git_commit", "run_python_script", "run_wsl_workflow", "run_powershell_workflow", "run_cmd_workflow", "run_terminal_workflow", "run_project_build", "run_md_simulation", "run_production_md", "create_task", "run_workflow_until_complete", "run_autonomous_workflow", "monitor_task", "list_tasks", "read_task_output", "retry_task", "cancel_task", "list_conversations", "get_conversation",
        "create_canvas", "get_canvas", "update_canvas", "list_canvas_history", "restore_canvas_revision",
        "browser_list_tabs", "browser_list_downloads", "browser_open_tab", "browser_navigate", "browser_read_page", "browser_click", "browser_type", "browser_select_option", "browser_wait", "browser_screenshot", "browser_close_tab",
    }
    assert required <= names


def test_answer_boundary_rejects_running_task_and_allows_only_real_terminal_failure():
    import pytest
    from mcp.server.fastmcp.exceptions import ToolError

    from web_mcp.mcp_server import _terminal_or_checkpoint

    with pytest.raises(ToolError):
        _terminal_or_checkpoint(
            "run_production_md",
            {
                "task_id": "task-running",
                "status": "running",
                "terminal": True,
                "completed": False,
                "workflow_status": "terminal_timeout",
                "user_visible_final": True,
            },
        )

    result = _terminal_or_checkpoint(
        "run_production_md",
        {
            "task_id": "task-failed",
            "status": "failed",
            "terminal": True,
            "completed": False,
            "workflow_status": "terminal_timeout",
            "user_visible_final": True,
            "final_answer_allowed": True,
        },
    )
    assert result["workflow_status"] == "terminal_timeout"
    assert result["answer"]
    assert result["final_answer"] == result["answer"]


def test_low_level_browser_handoff_is_a_tool_error_until_supervisor_is_terminal():
    import asyncio

    import pytest
    from mcp.server.fastmcp.exceptions import ToolError

    import web_mcp.mcp_server as mcp_server

    class FakeRegistry:
        async def invoke(self, _name, _arguments):
            return {
                "success": False,
                "completed": False,
                "terminal": False,
                "user_visible_final": False,
                "final_answer_allowed": False,
                "continue_required": True,
                "workflow_status": "in_progress",
                "server_handoff": True,
                "handoff_id": "chat-tab:turn",
                "next_action": "continue_server_handoff_without_new_task",
            }

    previous = mcp_server._registry
    mcp_server._registry = FakeRegistry()
    try:
        with pytest.raises(ToolError, match="nonterminal checkpoint"):
            asyncio.run(mcp_server._invoke_with_heartbeat("browser_wait", {"tab_id": "tab"}))
    finally:
        mcp_server._registry = previous


def test_browser_goal_handoff_returns_immediately_while_supervisor_runs():
    import asyncio
    from types import SimpleNamespace

    from web_mcp.tool_registry import ToolRegistry

    class FakeBrowser:
        async def read_page(self, *_args, **_kwargs):
            return {"url": "https://glycam.org/txt/"}

    registry = ToolRegistry.__new__(ToolRegistry)
    registry.ctx = SimpleNamespace(browser=FakeBrowser())
    registry._browser_goal_handoffs = {}
    registry._browser_goal_handoff_tasks = {}
    registry._browser_goal_handoff_lock = asyncio.Lock()

    async def infer_goal():
        return "chat-tab", "打开 GLYCAM 并完成一个复杂任务", "turn-signature"

    async def never_finishes(*_args, **_kwargs):
        await asyncio.sleep(3_600)

    registry._infer_latest_chat_goal = infer_goal
    registry._run_browser_goal_handoff = never_finishes

    async def exercise():
        result = await registry._maybe_handoff_browser_goal(
            "browser_read_page", {"tab_id": "glycam-tab"}
        )
        running = registry._browser_goal_handoff_tasks["chat-tab:turn-signature"]
        running.cancel()
        try:
            await running
        except asyncio.CancelledError:
            pass
        return result

    result = asyncio.run(exercise())
    assert result["server_handoff"] is True
    assert result["terminal"] is False
    assert result["continue_required"] is True
    assert result["workflow_status"] == "in_progress"
    assert result["next_action"] == "continue_server_handoff_without_new_task"