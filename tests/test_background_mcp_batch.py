from __future__ import annotations

import logging
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
async def background_registry(isolated_settings, tmp_path):
    from web_canvas.service import CanvasService
    from web_memory.store import Database
    from web_mcp.tool_registry import ToolContext, ToolRegistry
    settings = replace(isolated_settings, orchestration_mode="chatgpt_direct", admin_mode=True,
                       workspace_root=tmp_path, allowed_workspaces=[tmp_path])
    db = Database(settings.database_path, settings.project_root / "08_web_data" / "schema.sql")
    await db.init()
    registry = ToolRegistry(ToolContext(settings, db, CanvasService(db), logging.getLogger("background-tests")))
    try:
        yield registry
    finally:
        await db.close()


async def test_cached_batch_uses_same_registered_background_tool(background_registry, monkeypatch):
    call = AsyncMock(return_value={"ok": True, "status": "completed", "result": {"data": {"created": True}}})
    monkeypatch.setattr("web_mcp.direct_tools.call_background", call)
    result = await background_registry.invoke("run_workflow", {"workflow": {"steps": [
        {"tool": "background_desktop", "arguments": {"action": "create", "confirm": True}}
    ]}})
    assert result["completed"] is True
    call.assert_awaited_once()
    assert call.call_args.args[1] == "create"


async def test_cached_batch_does_not_skip_write_confirmation(background_registry, monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr("web_mcp.direct_tools.call_background", call)
    result = await background_registry.invoke("run_workflow", {"workflow": {"steps": [
        {"tool": "vscode_background", "arguments": {"action": "write_document", "arguments": {"path": "sample.py", "text": "changed"}}}
    ]}})
    assert result["completed"] is False
    call.assert_not_awaited()
