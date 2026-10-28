import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest


def sandbox(tmp_path):
    shell = shutil.which('powershell') or shutil.which('pwsh')
    if not shell:
        pytest.skip('PowerShell unavailable')
    root = tmp_path / 'project'
    deploy = root / '10_web_deploy'
    stage = deploy / 'migration_backups/browser-unification-20261027'
    stage.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / '10_web_deploy/rollback-browser-unification.ps1'
    script = deploy / source.name
    shutil.copy2(source, script)
    original = stage / 'prior/module.py'
    original.parent.mkdir()
    original.write_text('before', encoding='utf-8')
    modified = root / '06_web_tools/module.py'
    modified.parent.mkdir()
    modified.write_text('after', encoding='utf-8')
    added = root / '06_web_tools/new.py'
    added.write_text('added', encoding='utf-8')
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    rows = [{'kind': 'modified', 'path': r'06_web_tools\module.py', 'backup_path': r'prior\module.py',
             'original_sha256': digest(original), 'sha256': digest(modified)},
            {'kind': 'added', 'path': r'06_web_tools\new.py', 'sha256': digest(added)}]
    (stage / 'delivery-manifest.json').write_text(json.dumps(rows), encoding='utf-8')
    return shell, script, modified, added, stage


def test_real_powershell_stage_restore_and_new_source_quarantine(tmp_path):
    shell, script, modified, added, stage = sandbox(tmp_path)
    result = subprocess.run([shell, '-NoProfile', '-File', str(script), '-Apply'], capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert modified.read_text(encoding='utf-8') == 'before'
    assert not added.exists()
    assert list(stage.glob('rollback-new-source-*/06_web_tools/new.py'))


def test_changed_after_delivery_blocks_before_any_restore(tmp_path):
    shell, script, modified, added, stage = sandbox(tmp_path)
    modified.write_text('newer-user-edit', encoding='utf-8')
    result = subprocess.run([shell, '-NoProfile', '-File', str(script), '-Apply'], capture_output=True, timeout=30)
    assert result.returncode != 0
    assert modified.read_text(encoding='utf-8') == 'newer-user-edit' and added.exists()