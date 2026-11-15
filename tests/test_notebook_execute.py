from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

from web_tools.notebook_execute import _connection_file_permission_scope, execute_notebook, kernel_environment


def test_kernel_environment_removes_controller_activation_only():
    environment = {"PYTHONPATH": "wrong", "PythonHome": "wrong", "VIRTUAL_ENV": "wrong", "PATH": "retained"}
    assert kernel_environment(environment) == {"PATH": "retained"}
    assert environment["PYTHONPATH"] == "wrong"


def test_never_overwrites_input(tmp_path):
    source = tmp_path / "executed.ipynb"
    source.write_text("original", encoding="utf-8")
    with pytest.raises(ValueError, match="separate"):
        execute_notebook(source, sys.executable, tmp_path, tmp_path)
    assert source.read_text(encoding="utf-8") == "original"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows connection file permissions")
def test_account_lookup_fallback_is_scoped_and_restores_original(tmp_path, monkeypatch):
    import jupyter_core.paths as paths
    import web_tools.notebook_execute as runner

    class AccountLookupError(Exception):
        winerror = 1332

    def lookup_failure(filename):
        raise AccountLookupError()

    connection = tmp_path / "kernel-connection.json"
    connection.touch()
    calls = []
    monkeypatch.setattr(paths, "win32_restrict_file_to_user", lookup_failure)
    monkeypatch.setattr(runner, "_restrict_connection_file_to_token_user", calls.append)
    with _connection_file_permission_scope(connection):
        paths.win32_restrict_file_to_user(str(connection))
        with pytest.raises(AccountLookupError):
            paths.win32_restrict_file_to_user(str(tmp_path / "unrelated.json"))
    assert calls == [str(connection.resolve())]
    assert paths.win32_restrict_file_to_user is lookup_failure


@pytest.mark.skipif(sys.platform != "win32", reason="Windows connection file permissions")
def test_permission_fallback_does_not_hide_other_errors_or_touch_secrets(tmp_path, monkeypatch):
    import jupyter_core.paths as paths
    import web_tools.notebook_execute as runner

    class AccessError(Exception):
        winerror = 5

    class AccountLookupError(Exception):
        winerror = 1332

    connection = tmp_path / "kernel-connection.json"
    connection.write_text("must not read or modify this content", encoding="utf-8")
    calls = []
    monkeypatch.setattr(runner, "_restrict_connection_file_to_token_user", calls.append)
    for error, expected in ((AccessError, AccessError), (AccountLookupError, PermissionError)):
        def failing(filename):
            raise error()

        monkeypatch.setattr(paths, "win32_restrict_file_to_user", failing)
        with _connection_file_permission_scope(connection):
            with pytest.raises(expected):
                paths.win32_restrict_file_to_user(str(connection))
    assert calls == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows connection file permissions")
def test_fallback_acl_allows_only_current_sid_and_administrators(tmp_path, monkeypatch):
    import jupyter_core.paths as paths
    win32api = pytest.importorskip("win32api")
    win32con = pytest.importorskip("win32con")
    security = pytest.importorskip("win32security")

    class AccountLookupError(Exception):
        winerror = 1332

    def failing(filename):
        raise AccountLookupError()

    connection = tmp_path / "kernel-connection.json"
    connection.touch()
    monkeypatch.setattr(paths, "win32_restrict_file_to_user", failing)
    with _connection_file_permission_scope(connection):
        paths.win32_restrict_file_to_user(str(connection))
    token = security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        current_sid = security.GetTokenInformation(token, security.TokenUser)[0]
    finally:
        token.Close()
    administrators = security.CreateWellKnownSid(security.WinBuiltinAdministratorsSid)
    descriptor = security.GetNamedSecurityInfo(str(connection), security.SE_FILE_OBJECT, security.DACL_SECURITY_INFORMATION)
    dacl = descriptor.GetSecurityDescriptorDacl()
    allowed_sids = {security.ConvertSidToStringSid(dacl.GetAce(i)[2]) for i in range(dacl.GetAceCount())}
    assert allowed_sids == {security.ConvertSidToStringSid(current_sid), security.ConvertSidToStringSid(administrators)}
    assert descriptor.GetSecurityDescriptorControl()[0] & security.SE_DACL_PROTECTED


def test_non_python_notebook_has_failure_report(tmp_path):
    nbformat = pytest.importorskip("nbformat")
    source = tmp_path / "r.ipynb"
    nbformat.write(nbformat.v4.new_notebook(metadata={"language_info": {"name": "R"}}), source)
    report = execute_notebook(source, sys.executable, tmp_path, tmp_path / "out")
    assert report["success"] is False
    assert report["error"]["type"] == "ValueError"
    assert "Only Python" in report["error"]["message"]
    assert json.loads(Path(report["report_path"]).read_text(encoding="utf-8"))["success"] is False


@pytest.fixture
def kernel_python():
    pytest.importorskip("nbclient")
    pytest.importorskip("nbformat")
    selected = os.getenv("XIAORUO1_TEST_KERNEL_PYTHON")
    if selected:
        return Path(selected).resolve()
    if importlib.util.find_spec("ipykernel") is None:
        pytest.skip("Set XIAORUO1_TEST_KERNEL_PYTHON to an interpreter with ipykernel")
    return Path(sys.executable).resolve()


def _write_notebook(path, sources):
    import nbformat

    notebook = nbformat.v4.new_notebook(
        cells=[nbformat.v4.new_markdown_cell("keep this markdown"), *[nbformat.v4.new_code_cell(s) for s in sources]],
        metadata={"kernelspec": {"name": "nonexistent-user-kernel", "display_name": "Unrelated", "language": "python"}},
    )
    nbformat.write(notebook, path)


def test_uses_selected_interpreter_and_cwd_without_touching_source(tmp_path, kernel_python):
    import nbformat

    source = tmp_path / "source.ipynb"
    _write_notebook(source, ["import sys, os, json; print(json.dumps({'python': sys.executable, 'cwd': os.getcwd()}))"])
    original = source.read_bytes()
    report = execute_notebook(source, kernel_python, tmp_path, tmp_path / "out", timeout=30)
    assert report["success"], report
    assert report["executed_code_cells"] == 1
    result = nbformat.read(report["output_notebook"], as_version=4)
    evidence = json.loads(result.cells[1].outputs[0].text)
    assert Path(evidence["python"]).resolve() == kernel_python
    assert Path(evidence["cwd"]).resolve() == tmp_path
    assert source.read_bytes() == original
    assert result.cells[0].source == "keep this markdown"
    assert result.cells[1].source == nbformat.read(source, as_version=4).cells[1].source
    assert not list((tmp_path / "out").glob(".xiaoruo1-kernel-*"))


def test_failure_saves_partial_outputs_and_stops_following_cells(tmp_path, kernel_python):
    import nbformat

    source = tmp_path / "source.ipynb"
    _write_notebook(source, ["print('first cell completed')", "raise ValueError('expected failure')", "print('must not run')"])
    original = source.read_bytes()
    report = execute_notebook(source, kernel_python, tmp_path, tmp_path / "out", timeout=30)
    assert report["success"] is False
    assert report["executed_code_cells"] == 1
    assert report["error"]["cells"] == [{"cell_index": 2, "ename": "ValueError", "evalue": "expected failure"}]
    result = nbformat.read(report["output_notebook"], as_version=4)
    assert result.cells[1].outputs[0].text.strip() == "first cell completed"
    assert result.cells[2].outputs[0].output_type == "error"
    assert result.cells[3].execution_count is None
    assert source.read_bytes() == original
    assert not list((tmp_path / "out").glob(".xiaoruo1-kernel-*"))


def test_timeout_stops_owned_kernel_and_saves_partial_output(tmp_path, kernel_python):
    import nbformat
    import psutil

    source = tmp_path / "slow.ipynb"
    _write_notebook(source, ["import os, time; print(os.getpid(), flush=True); time.sleep(30)"])
    report = execute_notebook(source, kernel_python, tmp_path, tmp_path / "out", timeout=1)
    assert report["success"] is False
    assert report["error"]["type"] == "CellTimeoutError"
    assert report["executed_code_cells"] == 0
    result = nbformat.read(report["output_notebook"], as_version=4)
    kernel_pid = int(result.cells[1].outputs[0].text.strip())
    assert not psutil.pid_exists(kernel_pid)
    assert not list((tmp_path / "out").glob(".xiaoruo1-kernel-*"))
