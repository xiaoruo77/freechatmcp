from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "10_web_deploy" / "vscode_background_extension"


def test_extension_has_no_focus_terminal_command_or_auth_api():
    source = (EXTENSION / "extension.js").read_text(encoding="utf-8")
    for forbidden in (
        "showTextDocument", "showNotebookDocument", "executeCommand", "createTerminal",
        "sendText", "clipboard", "vscode.authentication", "eval(", "child_process",
        "showInformationMessage", "showQuickPick", "showInputBox",
    ):
        assert forbidden not in source
    assert "vscode.workspace.openNotebookDocument" in source
    assert "host: '127.0.0.1'" in source
    assert "exclusive: true" in source
    manifest = json.loads((EXTENSION / "package.json").read_text(encoding="utf-8"))
    assert manifest["activationEvents"] == ["onStartupFinished"]
    assert "dependencies" not in manifest


def test_path_hash_and_cell_guard_contract(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise extension helpers")
    root = tmp_path / "allowed"
    root.mkdir()
    inside = root / "a.txt"
    inside.write_text("unchanged", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    script = r"""
const assert = require('node:assert/strict');
const h = require(process.argv[1]);
(async () => {
  const [root, inside, outside] = process.argv.slice(2);
  assert.equal(h.normalizeDocumentEol('first\nsecond\r\nthird\r', 'CRLF'), 'first\r\nsecond\r\nthird\r\n');
  assert.equal(h.normalizeDocumentEol('first\r\nsecond\rthird\n', 'LF'), 'first\nsecond\nthird\n');
  assert.equal(await h.resolveAllowedPath(inside, [root]), inside);
  await assert.rejects(h.resolveAllowedPath(outside, [root]), e => e.code === 'path_outside_allowed_roots');
  await assert.rejects(h.resolveAllowedPath(inside, [root], [inside]), e => e.code === 'protected_bridge_file');
  assert.throws(() => h.assertExpectedHash('new', h.sha256('old')), e => e.code === 'content_conflict');
  assert.throws(() => h.assertExpectedHash('old', undefined), e => e.code === 'expected_hash_required');
  const cells = [{kind:'markdown', source:'keep'}, {kind:'code', source:'old'}];
  assert.deepEqual(h.validateCellEdits([{index:1, source:'new', expected_sha256:h.sha256('old')}], cells), [{index:1,source:'new'}]);
  assert.throws(() => h.validateCellEdits([{index:0, source:'bad', expected_sha256:h.sha256('keep')}], cells), e => e.code === 'code_cells_only');
  assert.throws(() => h.validateCellEdits([{index:1, source:'bad', expected_sha256:h.sha256('stale')}], cells), e => e.code === 'content_conflict');
  assert.throws(() => h.validateCellEdits([{index:3, source:'bad', expected_sha256:h.sha256('x')}], cells), e => e.code === 'invalid_cell_index');
  console.log('guards passed');
})().catch(e => {console.error(e); process.exitCode = 1;});
"""
    result = subprocess.run(
        [node, "-e", script, str(EXTENSION / "bridge_helpers.js"), str(root.resolve()), str(inside.resolve()), str(outside.resolve())],
        text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "guards passed" in result.stdout
