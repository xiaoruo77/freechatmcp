from __future__ import annotations

import asyncio
import os
import zipfile
from dataclasses import replace

import pytest


def test_shell_profiles_are_workspace_bound(isolated_settings):
    from web_tools.profiles import cmd_command, normalize_wsl_path, powershell_command, wsl_command

    powershell = powershell_command(
        {"commands": ["Write-Output 'powershell-profile-ok'"], "cwd": "."},
        isolated_settings,
        "task-test",
    )
    cmd = cmd_command(
        {"commands": ["echo cmd-profile-ok"], "cwd": "."},
        isolated_settings,
        "task-test",
    )
    assert powershell.cwd == isolated_settings.workspace_root
    assert cmd.cwd == isolated_settings.workspace_root
    assert "powershell" in powershell.command[0].lower() or "pwsh" in powershell.command[0].lower()
    assert cmd.command[0].lower().endswith(("cmd.exe", "cmd"))
    assert normalize_wsl_path("/home", "Deepin") == ("Deepin", "/home")
    assert normalize_wsl_path(r"\\wsl$\Deepin\home\xiaoruo") == ("Deepin", "/home/xiaoruo")
    wsl = wsl_command({"commands": ["pwd"], "cwd": "/"}, isolated_settings, "task-test")
    assert "cd /" in wsl.command[-1]


def test_md_profile_stages_browser_pdb_with_matching_glycam_zip(isolated_settings, tmp_path):
    from web_tools.profiles import _normalize_md_glycan_arg

    downloads = tmp_path / "downloads"
    downloads.mkdir()
    pdb = downloads / "DGlcpb1-4DGlcpb1-OH_structure_min-gas.pdb"
    pdb.write_text("ATOM\n", encoding="utf-8")
    archive = downloads / "DGlcpb1-4DGlcpb1-OH.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("nested/unminimized-gas.parm7", "parm7")
        handle.writestr("nested/unminimized-gas.rst7", "rst7")

    settings = replace(isolated_settings, workspace_root=tmp_path, allowed_workspaces=[tmp_path])
    args = _normalize_md_glycan_arg(
        {
            "args": [
                "--glycan-dir",
                str(pdb),
                "--target-root",
                str(tmp_path / "target"),
            ],
            "cwd": str(tmp_path),
        },
        settings,
        "task-stage-test",
    )
    staged = tmp_path / "target" / ".xiaoruo1_glycan_inputs"
    staged_dirs = list(staged.iterdir())
    assert len(staged_dirs) == 1
    assert args[1] == str(staged_dirs[0])
    assert (staged_dirs[0] / "min-gas.pdb").is_file()
    assert (staged_dirs[0] / "unminimized-gas.parm7").is_file()
    assert (staged_dirs[0] / "unminimized-gas.rst7").is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows shell integration test")
def test_powershell_profile_executes(isolated_settings):
    from web_tools.runner import ProfileRunner

    async def run():
        runner = ProfileRunner(isolated_settings)

        async def not_cancelled() -> bool:
            return False

        output: list[tuple[str, str]] = []

        async def capture(level: str, message: str) -> None:
            output.append((level, message))

        result = await runner.run(
            "powershell_workflow",
            {"commands": ["Write-Output 'powershell-integration-ok'"], "cwd": "."},
            "task-test",
            not_cancelled,
            capture,
        )
        assert result.status == "success"
        assert "powershell-integration-ok" in result.stdout

    asyncio.run(run())


@pytest.mark.skipif(os.name != "nt", reason="Windows shell integration test")
def test_cmd_profile_executes(isolated_settings):
    from web_tools.runner import ProfileRunner

    async def run():
        runner = ProfileRunner(isolated_settings)

        async def not_cancelled() -> bool:
            return False

        async def capture(level: str, message: str) -> None:
            return None

        result = await runner.run(
            "cmd_workflow",
            {"commands": ["echo cmd-integration-ok"], "cwd": "."},
            "task-test",
            not_cancelled,
            capture,
        )
        assert result.status == "success"
        assert "cmd-integration-ok" in result.stdout

    asyncio.run(run())


@pytest.mark.skipif(os.name != "nt", reason="WSL integration test")
def test_list_wsl_directory(isolated_settings):
    from web_canvas.service import CanvasService
    from web_logs.logger import configure_logging
    from web_memory.store import Database
    from web_mcp.tool_registry import ToolContext, ToolRegistry

    async def run():
        db = Database(isolated_settings.database_path, isolated_settings.project_root / "08_web_data" / "schema.sql")
        await db.init()
        logger = configure_logging(isolated_settings.log_root, "WARNING")
        registry = ToolRegistry(ToolContext(isolated_settings, db, CanvasService(db), logger))
        result = await registry.invoke(
            "list_wsl_directory",
            {"path": "/", "distribution": "Deepin", "recursive": False, "limit": 20},
        )
        await db.close()
        assert result["distribution"] == "Deepin"
        assert result["root"] == "/"
        assert result["entries"]

    asyncio.run(run())