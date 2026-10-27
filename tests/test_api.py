from __future__ import annotations

import shutil
from pathlib import Path

from fastapi.testclient import TestClient


def headers():
    return {"Authorization": "Bearer pytest-token"}


def test_health_and_auth(isolated_settings):
    from web_mcp.server import create_app

    app = create_app(isolated_settings)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/config").status_code == 401
        response = client.get("/api/config", headers=headers())
        assert response.status_code == 200
        assert response.json()["mcp_path"] == "/mcp"
        # Keep the legacy SSE transport alive as well as Streamable HTTP.  A
        # ChatGPT connector may probe /sse before it negotiates /mcp; if the
        # route is missing the frontend catch-all returns HTML and the app
        # incorrectly reports that the MCP bridge is unavailable.
        route_paths = {getattr(route, "path", None) for route in app.routes}
        assert "/mcp" in route_paths
        assert "/sse" in route_paths
        assert "/messages" in route_paths
        mcp_probe_headers = {
            **headers(),
            "Host": "mcp.example.com",
        }
        assert client.options("/mcp", headers=mcp_probe_headers).status_code != 421


def test_file_tools_and_canvas_revision_flow(isolated_settings, tmp_path):
    from web_mcp.server import create_app

    app = create_app(isolated_settings)
    with TestClient(app) as client:
        file_name = "tests/runtime_tool_probe.txt"
        nested_root = Path(isolated_settings.project_root / "tests/runtime_search_nested/automated_scripts")
        nested_file = nested_root / "runtime_nested_script.py"
        try:
            write = client.post("/api/tools/invoke", headers=headers(), json={"name": "write_file", "arguments": {"path": file_name, "content": "alpha\\nbeta"}})
            assert write.status_code == 200
            read = client.post("/api/tools/invoke", headers=headers(), json={"name": "read_file", "arguments": {"path": file_name}})
            assert read.status_code == 200
            assert "alpha" in read.json()["result"]["content"]
            search = client.post("/api/tools/invoke", headers=headers(), json={"name": "search_file", "arguments": {"query": "alpha", "path": "tests"}})
            assert search.status_code == 200
            assert search.json()["result"]["results"]

            nested_root.mkdir(parents=True, exist_ok=True)
            nested_file.write_text("nested-marker", encoding="utf-8")
            path_search = client.post(
                "/api/tools/invoke",
                headers=headers(),
                json={"name": "find_paths", "arguments": {"query": "automated_scripts", "path": "tests", "kind": "directory"}},
            )
            assert path_search.status_code == 200
            assert any(item["path"].endswith("automated_scripts") for item in path_search.json()["result"]["results"])
            semantic_search = client.post(
                "/api/tools/invoke",
                headers=headers(),
                json={"name": "find_paths", "arguments": {"query": "mds-gromacs-automation", "path": "tests", "kind": "directory"}},
            )
            assert semantic_search.status_code == 200
            assert any(item["path"].endswith("automated_scripts") for item in semantic_search.json()["result"]["results"])
            name_search = client.post(
                "/api/tools/invoke",
                headers=headers(),
                json={"name": "search_file", "arguments": {"query": "runtime_nested_script.py", "path": "tests"}},
            )
            assert name_search.status_code == 200
            assert any(item["path"].endswith("runtime_nested_script.py") for item in name_search.json()["result"]["results"])
        finally:
            Path(isolated_settings.project_root / file_name).unlink(missing_ok=True)
            shutil.rmtree(isolated_settings.project_root / "tests/runtime_search_nested", ignore_errors=True)

        created = client.post("/api/canvas", headers=headers(), json={"title": "Test Canvas", "content": "rev one"})
        assert created.status_code == 200
        canvas_id = created.json()["id"]
        updated = client.put(f"/api/canvas/{canvas_id}", headers=headers(), json={"content": "rev two"})
        assert updated.status_code == 200
        assert updated.json()["current_revision"] == 2
        history = client.get(f"/api/canvas/{canvas_id}/history", headers=headers())
        assert [item["revision_number"] for item in history.json()["history"]] == [2, 1]
        restored = client.post(f"/api/canvas/{canvas_id}/restore/1", headers=headers())
        assert restored.status_code == 200
        assert restored.json()["current_revision"] == 3
        assert restored.json()["revision"]["content"] == "rev one"


def test_chat_and_task_enqueue(isolated_settings):
    from web_mcp.server import create_app

    app = create_app(isolated_settings)
    with TestClient(app) as client:
        result = client.post("/api/chat", headers=headers(), json={"message": "/tool get_system_info {}"})
        assert result.status_code == 200
        assert result.json()["tool_name"] == "get_system_info"
        task = client.post("/api/tasks", headers=headers(), json={"profile": "python_script", "payload": {"code": "print('ok')"}})
        assert task.status_code == 200
        task_id = task.json()["task_id"]
        detail = client.get(f"/api/tasks/{task_id}", headers=headers())
        assert detail.status_code == 200
        assert detail.json()["status"] == "queued"
        cancelled = client.post(f"/api/tasks/{task_id}/cancel", headers=headers())
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"