"""Execute a Python notebook in a fresh kernel from one selected interpreter.

The controller uses the service's Jupyter libraries. The kernel always uses
the supplied Python executable, independently of installed/global kernelspecs.
This helper never attaches to an existing kernel or overwrites its input.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
from typing import Any


def kernel_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    """Do not pass the controller's Python activation into the selected kernel."""
    result = dict(os.environ if environment is None else environment)
    contaminated = {"pythonhome", "pythonpath", "virtual_env", "virtual_env_prompt"}
    for key in tuple(result):
        if key.lower() in contaminated:
            result.pop(key, None)
    return result


def _restrict_connection_file_to_token_user(filename: str) -> None:
    """Use the effective process SID when Windows cannot map its account name.

    This retains Jupyter's current-user plus administrators DACL and protects
    it from inherited entries. It is called only before a fresh connection
    file receives any secrets.
    """
    import ntsecuritycon
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    administrators = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid)
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_WRITE | ntsecuritycon.DELETE,
        user_sid,
    )
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, administrators)
    win32security.SetNamedSecurityInfo(
        filename,
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )


@contextmanager
def _connection_file_permission_scope(connection_file: Path):
    """Keep secure_write intact; narrowly handle System account lookup 1332."""
    if sys.platform != "win32":
        yield
        return
    import jupyter_core.paths as jupyter_paths

    expected = connection_file.resolve()
    original = jupyter_paths.win32_restrict_file_to_user

    def restrict(filename: str) -> None:
        try:
            original(filename)
        except Exception as exc:
            if getattr(exc, "winerror", None) != 1332 or Path(filename).resolve() != expected:
                raise
            # secure_write must still be at its empty-file stage. Never use
            # this fallback on an existing connection or arbitrary file.
            if not expected.is_file() or expected.stat().st_size != 0:
                raise PermissionError("Refusing ACL fallback for a nonempty kernel connection file") from exc
            _restrict_connection_file_to_token_user(str(expected))

    jupyter_paths.win32_restrict_file_to_user = restrict
    try:
        yield
    finally:
        jupyter_paths.win32_restrict_file_to_user = original


def _validate_python_notebook(notebook: Any) -> None:
    metadata = notebook.get("metadata", {})
    for section, key in (("language_info", "name"), ("kernelspec", "language")):
        language = metadata.get(section, {}).get(key)
        if language and str(language).lower() not in {"python", "python2", "python3"}:
            raise ValueError("Only Python notebooks are supported by this runner")


def _cell_errors(notebook: Any) -> list[dict[str, Any]]:
    errors = []
    for index, cell in enumerate(notebook.cells):
        for output in cell.get("outputs", []):
            if output.get("output_type") == "error":
                errors.append({
                    "cell_index": index,
                    "ename": str(output.get("ename", "Error"))[:200],
                    "evalue": str(output.get("evalue", ""))[:1000],
                })
    return errors


async def _execute_in_kernel(
    notebook: Any,
    python_executable: Path,
    cwd: Path,
    temporary_root: Path,
    timeout: int,
    executed: list[int],
) -> None:
    from jupyter_client import AsyncKernelManager
    from jupyter_client.kernelspec import KernelSpecManager
    from jupyter_core.utils import ensure_async
    from nbclient import NotebookClient

    name = "xiaoruo1-selected-python"
    kernels_root = temporary_root / "kernels"
    spec_directory = kernels_root / name
    spec_directory.mkdir(parents=True)
    (spec_directory / "kernel.json").write_text(json.dumps({
        "argv": [str(python_executable), "-m", "ipykernel_launcher", "-f", "{connection_file}"],
        "display_name": "Xiaoruo1 selected Python",
        "language": "python",
    }), encoding="utf-8")
    specs = KernelSpecManager(kernel_dirs=[str(kernels_root)], ensure_native_kernel=False)
    manager = AsyncKernelManager(
        kernel_name=name,
        kernel_spec_manager=specs,
        connection_file=str(temporary_root / "kernel-connection.json"),
    )
    client = NotebookClient(
        notebook,
        km=manager,
        kernel_name=name,
        timeout=timeout,
        startup_timeout=min(60, max(10, timeout)),
        allow_errors=False,
        force_raise_errors=True,
        interrupt_on_timeout=False,
        ipython_hist_file=":memory:",
        extra_arguments=["--HistoryManager.hist_file=:memory:"],
        resources={"metadata": {"path": str(cwd)}},
    )
    execution_error: BaseException | None = None
    try:
        # Manage the lifecycle explicitly. In particular, the supplied manager
        # belongs to this helper; nbclient otherwise treats it as caller-owned.
        await client.async_start_new_kernel(cwd=str(cwd), env=kernel_environment())
        await client.async_start_new_kernel_client()
        client.reset_execution_trackers()
        assert client.kc is not None
        info_id = await ensure_async(client.kc.kernel_info())
        info = await client.async_wait_for_reply(info_id)
        if info is not None and "language_info" in info.get("content", {}):
            notebook.metadata["language_info"] = info["content"]["language_info"]
        for index, cell in enumerate(notebook.cells):
            await client.async_execute_cell(cell, index, execution_count=client.code_cells_executed + 1)
            if cell.cell_type == "code" and cell.get("execution_count") is not None:
                executed.append(index)
        client.set_widgets_metadata()
    except BaseException as exc:
        execution_error = exc
        raise
    finally:
        cleanup_error: BaseException | None = None
        try:
            if client.kc is not None:
                client.kc.stop_channels()
        except BaseException as exc:
            cleanup_error = exc
        try:
            if manager.has_kernel:
                try:
                    await manager.shutdown_kernel(now=True)
                except BaseException:
                    # A failure partway through startup can leave incomplete
                    # connection metadata. Still terminate only our own kernel.
                    if manager.provisioner is not None and manager.provisioner.has_process:
                        await manager.provisioner.kill(restart=False)
                    raise
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        try:
            await manager.cleanup_resources()
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        if execution_error is None and cleanup_error is not None:
            raise cleanup_error


def execute_notebook(
    notebook_path: str | Path,
    python_executable: str | Path,
    cwd: str | Path,
    output_dir: str | Path,
    timeout: int = 600,
) -> dict[str, Any]:
    """Execute and persist actual evidence, including partial output on failure."""
    source = Path(notebook_path).resolve()
    python = Path(python_executable).resolve()
    workdir = Path(cwd).resolve()
    destination = Path(output_dir).resolve()
    output = destination / "executed.ipynb"
    report_path = destination / "notebook-execution.json"
    if output == source:
        raise ValueError("Notebook output must be separate from the input notebook")
    destination.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    executed: list[int] = []
    notebook = None
    report: dict[str, Any] = {
        "status": "failed",
        "success": False,
        "input_notebook": str(source),
        "python_executable": str(python),
        "cwd": str(workdir),
        "output_notebook": None,
        "report_path": str(report_path),
        "executed_code_cells": 0,
        "executed_cell_indexes": executed,
        "error": None,
    }
    try:
        if not source.is_file() or source.suffix.lower() != ".ipynb":
            raise ValueError("The notebook must be an existing .ipynb file")
        if not python.is_file() or not Path(python_executable).is_absolute():
            raise ValueError("The selected Python executable must be an existing absolute file")
        if not workdir.is_dir():
            raise ValueError("Notebook cwd must be an existing directory")
        if isinstance(timeout, bool) or not 1 <= int(timeout) <= 86400:
            raise ValueError("Cell timeout must be between 1 and 86400 seconds")
        import nbformat

        notebook = nbformat.read(source, as_version=4)
        _validate_python_notebook(notebook)
        # Stored outputs are not evidence of this execution attempt.
        for cell in notebook.cells:
            if cell.cell_type == "code":
                cell.outputs = []
                cell.execution_count = None
                cell.metadata.pop("execution", None)
        with tempfile.TemporaryDirectory(prefix=".xiaoruo1-kernel-", dir=destination) as temporary:
            temporary_path = Path(temporary)
            with _connection_file_permission_scope(temporary_path / "kernel-connection.json"):
                asyncio.run(_execute_in_kernel(notebook, python, workdir, temporary_path, int(timeout), executed))
        errors = _cell_errors(notebook)
        if errors:
            raise RuntimeError("Notebook execution produced a cell error")
        report.update(status="completed", success=True)
    except BaseException as exc:
        # CellExecutionError.__str__ includes the entire cell and its output.
        # Report a small error summary; detailed outputs stay in the artifact.
        report["error"] = {
            "type": type(exc).__name__,
            "frames": [
                {"file": frame.filename, "function": frame.name, "line": frame.lineno}
                for frame in traceback.extract_tb(exc.__traceback__)
            ],
        }
        errors = _cell_errors(notebook) if notebook is not None else []
        if errors:
            report["error"]["cells"] = errors
        elif type(exc).__name__ in {"CellExecutionError", "CellTimeoutError", "DeadKernelError"}:
            report["error"]["message"] = "Notebook kernel execution failed; inspect the saved partial notebook"
        else:
            report["error"]["message"] = str(exc)[:1000]
        if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
            report["status"] = "cancelled"
    finally:
        if notebook is not None:
            import nbformat

            temporary_output = output.with_suffix(".ipynb.tmp")
            nbformat.write(notebook, temporary_output)
            os.replace(temporary_output, output)
            report["output_notebook"] = str(output)
            report["total_code_cells"] = sum(cell.cell_type == "code" for cell in notebook.cells)
        report["executed_code_cells"] = len(executed)
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        temporary_report = report_path.with_suffix(".json.tmp")
        temporary_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary_report, report_path)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notebook", required=True)
    parser.add_argument("--python", required=True, dest="python_executable")
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--timeout", type=int, default=600)
    options = parser.parse_args(argv)
    report = execute_notebook(
        options.notebook,
        options.python_executable,
        options.cwd,
        options.output_dir,
        options.timeout,
    )
    print(json.dumps(report, ensure_ascii=True), flush=True)
    return 0 if report["success"] else (130 if report["status"] == "cancelled" else 1)


if __name__ == "__main__":
    sys.exit(main())
