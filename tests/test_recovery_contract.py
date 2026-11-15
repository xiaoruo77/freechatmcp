from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


def test_recovery_classifier_covers_the_screenshot_failure_classes():
    from web_mcp.tool_registry import _classify_recoverable_failure

    cases = [
        (
            {"status": "failed", "error": "Runner failed; inspect stderr"},
            {"stderr": "main.py: error: the following arguments are required: --glycan-dir, --target-root"},
            "missing_required_arguments",
        ),
        (
            {"status": "failed", "error": "Runner failed"},
            {"stderr": "FileNotFoundError: /bad/main.py"},
            "path_or_invocation",
        ),
        (
            {"status": "failed", "error": "Runner failed"},
            {"stderr": "sequence mismatch: four-sugar requested, three-sugar directory"},
            "input_identity_or_cardinality",
        ),
        (
            {"status": "failed", "error": "Runner failed"},
            {"stderr": "502 Bad Gateway from browser bridge"},
            "transient_bridge_or_network",
        ),
    ]

    for current, streams, expected in cases:
        result = _classify_recoverable_failure("md_simulation", {}, current, streams)
        assert result is not None
        assert result["error_class"] == expected


def test_nonzero_task_without_error_text_is_still_recoverable():
    from web_mcp.tool_registry import _classify_recoverable_failure

    result = _classify_recoverable_failure(
        "md_simulation",
        {},
        {"status": "failed", "error": None, "result": {"return_code": 1}},
        {"stdout": "", "stderr": ""},
    )
    assert result is not None
    assert result["error_class"] == "diagnosable_runtime_failure"


def test_failed_task_without_any_evidence_is_still_sent_to_self_check():
    from web_mcp.tool_registry import _classify_recoverable_failure

    result = _classify_recoverable_failure(
        "md_simulation",
        {},
        {"status": "failed", "error": None, "result": {}},
        {"stdout": "", "stderr": ""},
    )
    assert result is not None
    assert result["error_class"] == "unknown_diagnosable_failure"


def test_recovery_checkpoint_is_not_an_answer_boundary():
    from web_mcp.tool_registry import ToolRegistry

    registry = ToolRegistry.__new__(ToolRegistry)
    registry._recovery_attempts = {}
    registry._recovery_task_results = {}

    async def capture(_task_id: str) -> dict[str, str]:
        return {
            "stdout": "",
            "stderr": "main.py: error: the following arguments are required: --glycan-dir, --target-root",
        }

    registry._capture_task_streams = capture
    results = [
        asyncio.run(
            registry._finished_task_result(
                "md_simulation",
                {"script_path": "D:/AUTO_file/main.py", "cwd": "D:/work"},
                {
                    "id": f"task-recovery-contract-{index}",
                    "status": "failed",
                    "error": "Runner failed; inspect stderr",
                    "result": {"return_code": 2},
                    "output_files": [],
                    "logs": [],
                },
            )
        )
        for index in range(4)
    ]

    assert [item["terminal"] for item in results] == [False, False, False, True]
    assert results[0]["repair_required"] is True
    assert results[0]["do_not_reply"] is True
    assert results[3]["recovery_budget_exhausted"] is True


def test_long_runner_output_preserves_terminal_pipeline_identity(tmp_path: Path):
    """The current GLYCAM run directory is printed after the long GROMACS log."""

    from web_mcp.tool_registry import ToolRegistry, _extract_md_system_dir

    stdout = tmp_path / "stdout.txt"
    current_system = "/mnt/d/workspace-fixtures/scratch-fixtures/temp_file/GLYCAM_system_current"
    stdout.write_text(
        ("diagnostic line\n" * 3_000)
        + f"\nPIPELINE_SUCCESS: {current_system}\n",
        encoding="utf-8",
    )

    registry = ToolRegistry.__new__(ToolRegistry)

    async def get_task(_task_id: str):
        return {"output_files": [str(stdout)]}

    registry.tasks = SimpleNamespace(get=get_task)
    registry.ctx = SimpleNamespace(settings=SimpleNamespace(task_output_root=tmp_path))

    streams = asyncio.run(registry._capture_task_streams("task-long-output"))
    assert current_system in streams["stdout"]
    assert _extract_md_system_dir(streams) == "D:\\workspace-fixtures\\scratch-fixtures\\temp_file\\GLYCAM_system_current"


def test_generic_workflow_rejects_glycam_md_payloads():
    from web_mcp.tool_registry import _looks_like_md_workflow

    is_md, markers = _looks_like_md_workflow(
        [
            {
                "profile": "wsl",
                "commands": [
                    "python3 AUTO_file/main.py --glycan-dir input --target-root output",
                    "gmx mdrun -deffnm em",
                ],
            }
        ],
        [{"type": "file_exists", "path": "output/npt.log"}],
    )

    assert is_md is True
    assert {"glycan", "main.py", "gmx", "em"} <= set(markers)

    ordinary, ordinary_markers = _looks_like_md_workflow(
        [{"profile": "python", "script_path": "build_report.py"}],
        [{"type": "file_exists", "path": "report.txt"}],
    )
    assert ordinary is False
    assert ordinary_markers == []


def test_legacy_timed_mdrun_is_detected_for_compatibility_routing():
    from web_mcp.tool_registry import (
        _autonomous_goal_requires_production,
        _production_command_details,
        _production_intent_details,
    )

    details = _production_command_details(
        "retry_5min_md_fixed_decomposition",
        [
            "cd /mnt/d/workspace-fixtures/scratch-fixtures/temp_file/GLYCAM_system_new/work_1/3_core_file "
            "&& gmx mdrun -s md_5min.tpr -deffnm md_5min -ntmpi 2"
        ],
    )
    assert details["detected"] is True
    assert details["duration_seconds"] == 300.0
    assert details["prefix"] == "md_5min"
    assert any("GLYCAM_system_new" in item for item in details["path_candidates"])

    ordinary_npt = _production_command_details("run_npt", ["gmx mdrun -deffnm npt"])
    assert ordinary_npt["detected"] is False

    diagnostic = _production_intent_details("find_latest_production_input", ["find . -name '*.tpr'"])
    assert diagnostic["detected"] is True
    assert "find_latest_production_input" in diagnostic["markers"]

    ordinary_listing = _production_intent_details("inspect_latest_gromacs", ["find . -name '*.tpr'"])
    assert ordinary_listing["detected"] is False

    required, markers, duration = _autonomous_goal_requires_production(
        "Build the glycan, run EM/NVT/NPT, then run the 5-minute final simulation."
    )
    assert required is True
    assert duration == 300.0
    assert "final simulation" in markers
    assert "timed_stage_300s" in markers


def test_production_success_requires_runtime_and_trajectory_evidence():
    from web_mcp.tool_registry import _production_log_guard

    old_style_stdout = "\n".join(
        [
            "PRODUCTION_MD_SUCCESS: output",
            "PRODUCTION_MD_LOG: output/md.log",
            "PRODUCTION_MD_TPR: output/md.tpr",
            "PRODUCTION_MD_CHECKPOINT: output/md.cpt",
            "PRODUCTION_MD_TRAJECTORY: output/md.xtc",
            "PRODUCTION_MD_REQUESTED_WALL_SECONDS: 300",
        ]
    )
    rejected = _production_log_guard(
        "production_md",
        {},
        {"stdout": old_style_stdout, "stderr": ""},
        [],
    )
    assert rejected is not None
    assert rejected["error_class"] == "production_md_output_not_verified"
    assert "PRODUCTION_MD_RUNTIME_SECONDS" in rejected["error"]

    valid_stdout = old_style_stdout + "\n" + "\n".join(
        [
            "PRODUCTION_MD_RUNTIME_SECONDS: 306.042",
            "PRODUCTION_MD_STEPS: 3280",
            "PRODUCTION_MD_SIMULATION_TIME_PS: 6.56",
        ]
    )
    assert _production_log_guard(
        "production_md",
        {},
        {"stdout": valid_stdout, "stderr": ""},
        [],
    ) is None


@__import__("pytest").mark.skip(reason="Separate private MD project is not part of the public snapshot")
def test_gromacs_2026_multiline_performance_row_is_parsed_as_runtime(tmp_path: Path):
    """A real 5-minute GROMACS log must not be rejected by the audit parser."""

    script = Path(
        r"D:\workspace-fixtures\science-fixtures\02_md\MDS\AUTO_file\automated_scripts\md.py"
    )
    spec = importlib.util.spec_from_file_location("xiaoruo_production_md", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    log = tmp_path / "md.log"
    log.write_text(
        """Step 7360: Run time exceeded 0.083 hours, will terminate the run within 80 steps
Writing checkpoint, step 7440 at Tue Aug 25 16:24:56 2026
Energy conservation over simulation part #1 of length 14.88 ps, time 0 to 14.88 ps
               Core t (s)   Wall t (s)        (%)
       Time:     1212.104      303.028      400.0
Finished mdrun on rank 0 Tue Aug 25 16:24:56 2026
""",
        encoding="utf-8",
    )

    metrics = module._production_metrics(str(log))
    assert metrics["wall_seconds"] == 303.028
    assert metrics["steps"] == 7440
    assert metrics["simulation_time_ps"] == 14.88


def test_cached_short_md_arguments_are_removed_for_real_runs():
    from web_tools.profiles import _remove_legacy_short_md_overrides

    args = [
        "--glycan-dir",
        "input",
        "--target-root",
        "output",
        "--nsteps",
        "100",
        "--em-emtol",
        "10000",
        "--allow-nonconverged-em",
        "--threads",
        "4",
    ]
    assert _remove_legacy_short_md_overrides(args, {}) == [
        "--glycan-dir",
        "input",
        "--target-root",
        "output",
        "--threads",
        "4",
    ]
    assert _remove_legacy_short_md_overrides(args, {"allow_short_validation": True}) == args


def test_md_runner_rejects_empty_compatibility_calls_before_creating_worker_tasks():
    from web_mcp.tool_registry import ToolRegistry

    registry = ToolRegistry.__new__(ToolRegistry)
    result = asyncio.run(registry._run_md_simulation(args=[]))

    assert result["terminal"] is False
    assert result["repair_required"] is True
    assert result["final_answer_allowed"] is False
    assert result["error_class"] == "missing_required_arguments"
    assert "--glycan-dir" in result["diagnostic_evidence"]["missing"]
    assert "--target-root" in result["diagnostic_evidence"]["missing"]


def test_successful_md_setup_is_not_a_terminal_result_without_production():
    """PIPELINE_SUCCESS must remain a hand-off, not a false final answer."""

    from web_mcp.tool_registry import ToolRegistry

    registry = ToolRegistry.__new__(ToolRegistry)
    registry._recovery_task_results = {}
    registry._recovery_attempts = {}
    registry.ctx = SimpleNamespace(settings=SimpleNamespace(allowed_workspaces=[]))

    async def capture(_task_id: str) -> dict[str, str]:
        return {
            "stdout": (
                "PIPELINE_SUCCESS: /mnt/d/workspace-fixtures/scratch-fixtures/temp_file/"
                "GLYCAM_system_fresh_20260825\n"
                "EM_LOG: /mnt/d/run/em.log\n"
                "NVT_LOG: /mnt/d/run/nvt.log\n"
                "NPT_LOG: /mnt/d/run/npt.log\n"
            ),
            "stderr": "",
        }

    registry._capture_task_streams = capture
    result = asyncio.run(
        registry._finished_task_result(
            "md_simulation",
            {"script_path": "D:/AUTO_file/main.py", "cwd": "D:/work", "require_production": True},
            {
                "id": "task-setup-only-is-not-final",
                "status": "success",
                "finished_at": "2026-08-25T10:00:00Z",
                "result": {"return_code": 0},
                "output_files": [],
                "logs": [],
            },
        )
    )

    assert result["status"] == "success"  # the setup Worker itself succeeded
    assert result["setup_completed"] is True
    assert result["terminal"] is False
    assert result["completed"] is False
    assert result["final_answer_allowed"] is False
    assert result["repair_required"] is True
    assert result["error_class"] == "md_setup_complete_production_required"
    assert result["next_action"] == "run_production_md"
    assert result["system_dir"].endswith("GLYCAM_system_fresh_20260825")
    assert result["production_duration_seconds"] == 300.0


@__import__("pytest").mark.skip(reason="Separate private MD project is not part of the public snapshot")
def test_genion_solvent_group_parser_reads_stdout_and_stderr_variants():
    script = Path(
        r"D:\workspace-fixtures\science-fixtures\02_md\MDS\AUTO_file\automated_scripts\add_ions.py"
    )
    spec = importlib.util.spec_from_file_location("xiaoruo_add_ions", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    stdout_menu = "Group     7 (          SOL) has 22119 elements\nSelect a group:"
    stderr_menu = "\n  11 (SOL)\n"
    assert module.extract_solvent_group_index(stdout_menu) == "7"
    assert module.extract_solvent_group_index(stderr_menu) == "11"
    assert module.extract_solvent_group_index("Group 7 (System) has 10 elements") is None
    assert module.probe_requires_ion_group("No ions to add, will just copy input configuration.") is False
    assert module.probe_requires_ion_group("Group 7 (SOL) has 22119 elements") is True


@__import__("pytest").mark.skip(reason="Separate private MD project is not part of the public snapshot")
def test_protein_input_is_cleaned_before_pdb2gmx(tmp_path: Path):
    script = Path(
        r"D:\workspace-fixtures\science-fixtures\02_md\MDS\AUTO_file\main.py"
    )
    spec = importlib.util.spec_from_file_location("xiaoruo_md_main", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    source = tmp_path / "with_crystallographic_water.pdb"
    lines = []
    serial = 1
    for residue_number in range(1, 11):
        for atom_name, element in (("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O"), ("CB", "C")):
            lines.append(
                f"ATOM  {serial:5d} {atom_name:^4s} ALA A{residue_number:4d}    "
                f"{1.0:8.3f}{2.0:8.3f}{3.0:8.3f}{1.00:6.2f}{20.0:6.2f}          {element:>2s}"
            )
            serial += 1
    for water_number in range(1, 4):
        lines.append(
            f"HETATM{serial:5d} {'O':^4s} HOH B{water_number:4d}    "
            f"{4.0:8.3f}{5.0:8.3f}{6.0:8.3f}{1.00:6.2f}{20.0:6.2f}           O"
        )
        serial += 1
    lines.extend(("TER", "END"))
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")

    destination = tmp_path / "protein_only.pdb"
    info = module._canonicalize_protein_pdb(source, destination)
    cleaned = destination.read_text(encoding="utf-8")
    assert info["kept_atoms"] == 50
    assert info["dropped_atoms"] == 3
    assert "HOH" not in cleaned
    assert "ATOM  " in cleaned
    assert "END" in cleaned


def test_md_preflight_rejects_old_input_when_a_task_has_a_bound_fresh_download(tmp_path: Path):
    from web_mcp.tool_registry import ToolRegistry

    current = tmp_path / "glycam_current.zip"
    old = tmp_path / "glycam_old.zip"
    current.write_bytes(b"current")
    old.write_bytes(b"old")
    registry = ToolRegistry.__new__(ToolRegistry)
    registry.ctx = SimpleNamespace(settings=SimpleNamespace(allowed_workspaces=[tmp_path]))
    result = registry._autonomous_md_preflight(
        {"args": ["--glycan-dir", str(old), "--target-root", str(tmp_path)]},
        {
            "__requires_md": True,
            "__glycam_browser_verified": True,
            "__glycam_input": str(current),
            "__autonomous_started_epoch": 1.0,
            "__glycam_downloads": [{"state": "complete", "ended_at": 2.0, "file_path": str(current)}],
            "goal": "build a four sugar glycan",
        },
    )
    assert result is not None
    assert result["error_class"] == "stale_or_non_specific_final_check"
    assert result["repair_required"] is True


def test_production_system_selection_refuses_ambiguous_old_runs(tmp_path: Path):
    from web_tools.profiles import _production_system_dir

    def make_system(name: str) -> Path:
        system = tmp_path / name
        core = system / "work_1" / "3_core_file"
        processed = system / "work_1" / "1_processed_done"
        (system / "work_1" / "4_output").mkdir(parents=True)
        core.mkdir(parents=True)
        processed.mkdir(parents=True)
        for filename in ("npt.gro", "npt.cpt", "npt.tpr", "index.ndx"):
            (core / filename).write_text("x", encoding="utf-8")
        (processed / "topol.top").write_text("x", encoding="utf-8")
        (system / "setting_file.json").write_text("{}", encoding="utf-8")
        (system / "hardsoft.json").write_text("{}", encoding="utf-8")
        (system / "run_manifest.json").write_text(
            json.dumps({"status": "success", "stages": ["em", "nvt", "npt"]}),
            encoding="utf-8",
        )
        return system

    make_system("GLYCAM_system_old")
    make_system("GLYCAM_system_new")
    settings = SimpleNamespace(allowed_workspaces=[tmp_path])
    try:
        _production_system_dir({"target_root": str(tmp_path)}, settings)
    except ValueError as exc:
        assert "Multiple GLYCAM systems" in str(exc)
    else:
        raise AssertionError("implicit production selection must not choose by mtime")


def test_production_system_selection_rejects_explicit_older_run(tmp_path: Path):
    from web_tools.profiles import _production_system_dir

    def make_system(name: str, created_at: str) -> Path:
        system = tmp_path / name
        core = system / "work_1" / "3_core_file"
        processed = system / "work_1" / "1_processed_done"
        (system / "work_1" / "4_output").mkdir(parents=True)
        core.mkdir(parents=True)
        processed.mkdir(parents=True)
        for filename in ("npt.gro", "npt.cpt", "npt.tpr", "index.ndx"):
            (core / filename).write_text("x", encoding="utf-8")
        (processed / "topol.top").write_text("x", encoding="utf-8")
        (system / "setting_file.json").write_text("{}", encoding="utf-8")
        (system / "hardsoft.json").write_text("{}", encoding="utf-8")
        (system / "run_manifest.json").write_text(
            json.dumps({"status": "success", "stages": ["em", "nvt", "npt"], "created_at": created_at}),
            encoding="utf-8",
        )
        return system

    old = make_system("GLYCAM_system_old", "2026-08-25T00:38:28+00:00")
    make_system("GLYCAM_system_new", "2026-08-25T01:57:47+00:00")
    settings = SimpleNamespace(allowed_workspaces=[tmp_path])
    try:
        _production_system_dir({"system_dir": str(old), "target_root": str(tmp_path)}, settings)
    except ValueError as exc:
        assert "older than a newer verified run" in str(exc)
    else:
        raise AssertionError("an explicit older system must not be reused when a newer run exists")


def test_terminal_production_result_has_a_complete_user_answer():
    from web_mcp.tool_registry import _ensure_terminal_user_answer

    result = _ensure_terminal_user_answer(
        {
            "task_id": "production-contract",
            "profile": "production_md",
            "status": "success",
            "terminal": True,
            "completed": True,
            "success": True,
            "workflow_status": "terminal_success",
            "stdout": "\n".join(
                [
                    "PRODUCTION_MD_SUCCESS: D:/run",
                    "PRODUCTION_MD_LOG: D:/run/md.log",
                    "PRODUCTION_MD_TPR: D:/run/md.tpr",
                    "PRODUCTION_MD_CHECKPOINT: D:/run/md.cpt",
                    "PRODUCTION_MD_TRAJECTORY: D:/run/md.xtc",
                    "PRODUCTION_MD_RUNTIME_SECONDS: 303.028",
                    "PRODUCTION_MD_STEPS: 7440",
                    "PRODUCTION_MD_SIMULATION_TIME_PS: 14.88",
                    "PRODUCTION_MD_REQUESTED_WALL_SECONDS: 300",
                ]
            ),
        }
    )

    assert "303.028" in result["answer"]
    assert "7440" in result["final_answer"]
    assert result["terminal_user_message"] == result["answer"]
    assert "不得只回复" in result["terminal_instruction"]


def test_chat_goal_signature_changes_for_a_repeated_prompt_turn():
    from web_mcp.tool_registry import _chat_goal_signature

    goal = "@Xiaoruo1 打开网站，创建文件并运行验证。"
    first_page = f"你说：{goal} ChatGPT 说：第一次结果"
    second_page = f"你说：{goal} ChatGPT 说：第一次结果 你说：{goal} ChatGPT 说："

    assert _chat_goal_signature(first_page, goal) != _chat_goal_signature(second_page, goal)


def test_latest_chat_goal_prefers_the_newest_chat_tab():
    from web_mcp.tool_registry import ToolRegistry

    goal_old = "@Xiaoruo1 打开旧网站，构建文件，调用脚本运行模拟并验证结果。"
    goal_new = "@Xiaoruo1 打开新网站，构建文件，调用脚本运行模拟并验证结果。"

    class Browser:
        async def list_tabs(self):
            return {
                "tabs": [
                    {"tab_id": "307161179", "url": "https://chatgpt.com/c/old", "active": False},
                    {"tab_id": "307161685", "url": "https://chatgpt.com/c/new", "active": False},
                ]
            }

        async def read_page(self, tab_id, **_kwargs):
            goal = goal_new if tab_id == "307161685" else goal_old
            return {"text": f"你说：{goal} ChatGPT 说："}

    registry = ToolRegistry.__new__(ToolRegistry)
    registry.ctx = SimpleNamespace(browser=Browser())
    inferred = asyncio.run(registry._infer_latest_chat_goal())

    assert inferred is not None
    assert inferred[0] == "307161685"
    assert inferred[1] == goal_new
