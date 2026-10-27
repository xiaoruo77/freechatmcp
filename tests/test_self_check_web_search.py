from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace


def test_search_query_redacts_local_paths_and_secrets():
    from web_tools.web_search import build_search_query, sanitize_search_text

    query = build_search_query(
        "run_md_simulation",
        "diagnosable_runtime_failure",
        r"Traceback at D:\private\AUTO_file\main.py token=do-not-send-this",
    )

    assert "D:\\" not in query
    assert "do-not-send-this" not in query
    assert len(query) <= 240


def test_self_check_keeps_recoverable_errors_nonterminal(tmp_path: Path):
    from web_mcp.tool_registry import ToolRegistry

    class Tasks:
        async def get(self, _task_id):
            return None

    registry = ToolRegistry.__new__(ToolRegistry)
    registry.tasks = Tasks()
    registry.ctx = SimpleNamespace(
        settings=SimpleNamespace(
            workspace_root=tmp_path,
            allowed_workspaces=[tmp_path],
            admin_mode=False,
            port=48271,
            browser_bridge_url="http://127.0.0.1:48272",
        )
    )

    result = asyncio.run(
        registry._self_check(
            operation="run_md_simulation",
            error_text=r"FileNotFoundError: D:\missing\main.py",
            auto_search=False,
        )
    )

    assert result["terminal"] is False
    assert result["final_answer_allowed"] is False
    assert result["repair_required"] is True
    assert result["web_search_required"] is True
    assert "D:\\" not in result["web_search"]["query"]