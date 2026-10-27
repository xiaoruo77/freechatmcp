from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace


def _deep_automation_tree(root: Path) -> Path:
    current = root
    for index in range(12):
        current /= f"layer_{index:02d}"
    script = current / "MDS" / "AUTO_file" / "automated_scripts" / "md.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('production driver')\n", encoding="utf-8")
    (script.parents[2] / "main.py").write_text("print('bounded setup entry')\n", encoding="utf-8")
    return script


def test_environment_discovery_reaches_deep_production_driver(isolated_settings, tmp_path):
    from web_tools.discovery import discover_environment

    script = _deep_automation_tree(tmp_path)
    settings = replace(isolated_settings, workspace_root=tmp_path, allowed_workspaces=[tmp_path])

    result = discover_environment(
        settings,
        cwd=str(tmp_path),
        roots=[str(tmp_path)],
        request="production MD automation",
        max_results=80,
        max_depth=32,
    )

    assert result["scan"]["recursive"] is True
    assert result["scan"]["max_depth"] == 32
    assert result["scan"]["truncated"] is False
    assert str(script) in result["script_candidates"]
    assert str(script) in {item["path"] for item in result["sources_read"]}


def test_find_paths_and_search_file_reach_nested_driver(isolated_settings, tmp_path):
    from web_mcp.tool_registry import ToolRegistry

    script = _deep_automation_tree(tmp_path)
    settings = replace(isolated_settings, workspace_root=tmp_path, allowed_workspaces=[tmp_path])
    registry = ToolRegistry.__new__(ToolRegistry)
    registry.ctx = SimpleNamespace(settings=settings)

    paths = registry._find_paths_sync("md.py", str(tmp_path), kind="file", max_depth=32)
    assert paths["timed_out"] is False
    relative_script = str(script.relative_to(tmp_path))
    assert any(item["path"] == relative_script for item in paths["results"])

    script.write_text("# production md entry point\n", encoding="utf-8")
    contents = registry._search_file_sync(
        "production md entry point",
        str(tmp_path),
        max_results=20,
        include_paths=True,
        max_depth=32,
    )
    assert any(item.get("path") == relative_script and item.get("line") == 1 for item in contents["results"])