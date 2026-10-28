from pathlib import Path
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def staged(tmp_path):
    shell = shutil.which('powershell') or shutil.which('pwsh')
    if not shell:
        pytest.skip('PowerShell unavailable')
    root = tmp_path / 'project'
    deploy = root / '10_web_deploy'
    stage = deploy / 'migration_backups/native-ipc-v72-20261028'
    evidence = deploy / 'validation_runs/native-ipc-v72-20261028'
    stage.mkdir(parents=True)
    evidence.mkdir(parents=True)
    for name in ['deploy-native-ipc-v72.ps1', 'rollback-native-ipc-v72.ps1']:
        shutil.copy2(ROOT / '10_web_deploy' / name, deploy / name)
    current = root / '06_web_tools/module.py'
    current.parent.mkdir()
    current.write_text('new version', encoding='utf-8')
    previous = stage / 'original/module.py'
    previous.parent.mkdir()
    previous.write_text('old version', encoding='utf-8')
    added = root / '06_web_tools/added.py'
    added.write_text('new file', encoding='utf-8')
    hash_file = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = [{'kind': 'modified', 'path': r'06_web_tools\module.py', 'backup_path': r'original\module.py',
                 'sha256': hash_file(current), 'original_sha256': hash_file(previous)},
                {'kind': 'added', 'path': r'06_web_tools\added.py', 'sha256': hash_file(added)}]
    manifest_file = stage / 'delivery-manifest.json'
    manifest_file.write_text(json.dumps(manifest), encoding='utf-8')
    marker = {'passed': True, 'validation_mode': 'isolated',
              'engines': {'cef': {'passed': True}, 'lightpanda': {'passed': True}}}
    report = evidence / 'isolated-live-report.json'
    report.write_text(json.dumps(marker), encoding='utf-8')
    return {'root': root, 'deploy': deploy, 'stage': stage, 'evidence': evidence,
            'shell': shell, 'report': report, 'marker': marker, 'current': current,
            'added': added, 'manifest': manifest, 'manifest_file': manifest_file}


def run(staged, script, *args):
    result = subprocess.run([staged['shell'], '-NoProfile', '-File', str(staged['deploy'] / script), *map(str, args)],
                            capture_output=True, timeout=30)
    result.combined = (result.stdout + result.stderr).decode(errors='replace')
    return result


def test_powershell51_deployment_preview_validates_only_source_hashes(staged):
    result = run(staged, 'deploy-native-ipc-v72.ps1')
    assert result.returncode == 0, result.combined
    assert 'Preview only' in result.combined
    assert not (staged['evidence'] / 'deployment.json').exists()
    assert staged['current'].read_text() == 'new version'


def test_isolated_cef_lightpanda_gate_validate_only_never_touches_services(staged):
    result = run(staged, 'deploy-native-ipc-v72.ps1', '-ValidateOnly', '-ValidationReport', staged['report'])
    assert result.returncode == 0, result.combined
    assert 'No service action performed' in result.combined
    assert not (staged['evidence'] / 'deployment.json').exists()


@pytest.mark.parametrize('field,value', [('passed', False), ('passed', 'true'),
    ('validation_mode', 'mock'), ('lightpanda', False), ('cef', False)])
def test_mock_incomplete_or_non_boolean_validation_is_not_deployable(staged, field, value):
    marker = staged['marker']
    if field in {'cef', 'lightpanda'}:
        marker['engines'][field]['passed'] = value
    else:
        marker[field] = value
    staged['report'].write_text(json.dumps(marker), encoding='utf-8')
    result = run(staged, 'deploy-native-ipc-v72.ps1', '-Apply', '-ValidationReport', staged['report'])
    assert result.returncode != 0 and 'isolated' in result.combined
    assert staged['current'].read_text() == 'new version'
    assert not (staged['evidence'] / 'deployment.json').exists()


def test_report_outside_native_evidence_directory_is_refused(staged):
    outside = staged['root'] / 'elsewhere.json'
    outside.write_text(json.dumps(staged['marker']), encoding='utf-8')
    result = run(staged, 'deploy-native-ipc-v72.ps1', '-Apply', '-ValidationReport', outside)
    assert result.returncode != 0 and 'Validation report must be' in result.combined


def test_delivery_changed_after_review_prevents_deploy_before_service_calls(staged):
    staged['current'].write_text('another developer edit', encoding='utf-8')
    result = run(staged, 'deploy-native-ipc-v72.ps1', '-Apply', '-ValidationReport', staged['report'])
    assert result.returncode != 0 and 'Delivery hash mismatch' in result.combined
    assert not (staged['evidence'] / 'deployment.json').exists()


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_real_sqlite_active_task_guard_stops_before_scheduled_task_calls(staged, status):
    # Copy only the small existing Python redirector and its home config into
    # the disposable project; no runtime installation or service is performed.
    python = staged['root'] / '.venv/Scripts/python.exe'
    python.parent.mkdir(parents=True)
    shutil.copy2(sys.executable, python)
    source_config = Path(sys.prefix) / 'pyvenv.cfg'
    if source_config.exists():
        shutil.copy2(source_config, python.parent.parent / 'pyvenv.cfg')
    else:
        (python.parent.parent / 'pyvenv.cfg').write_text('home = ' + str(Path(sys._base_executable).parent) + '\n', encoding='utf-8')
    db_path = staged['root'] / '08_web_data/web_agent.sqlite3'
    db_path.parent.mkdir()
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE tasks (status TEXT)')
        db.execute('INSERT INTO tasks VALUES (?)', (status,))
    result = run(staged, 'deploy-native-ipc-v72.ps1', '-Apply', '-ValidationReport', staged['report'])
    assert result.returncode != 0 and 'Active queued/running tasks' in result.combined
    saved = json.loads((staged['evidence'] / 'deployment.json').read_text(encoding='utf-8-sig'))
    assert saved['success'] is False
    with sqlite3.connect(db_path) as db:
        assert db.execute('SELECT status FROM tasks').fetchone()[0] == status


def test_real_rollback_restores_only_source_and_quarantines_new_code(staged):
    data = staged['root'] / '08_web_data/credential.token'
    data.parent.mkdir()
    data.write_text('disposable-sensitive-fixture', encoding='utf-8')
    result = run(staged, 'rollback-native-ipc-v72.ps1', '-Apply')
    assert result.returncode == 0, result.combined
    assert staged['current'].read_text() == 'old version'
    assert not staged['added'].exists()
    assert list(staged['stage'].glob('rollback-new-source-*/06_web_tools/added.py'))
    assert data.read_text() == 'disposable-sensitive-fixture'


def test_rollback_refuses_newer_edits_before_any_mutation(staged):
    staged['current'].write_text('latest user edit', encoding='utf-8')
    result = run(staged, 'rollback-native-ipc-v72.ps1', '-Apply')
    assert result.returncode != 0 and 'changed after delivery' in result.combined
    assert staged['current'].read_text() == 'latest user edit' and staged['added'].exists()


@pytest.mark.parametrize('target', [r'..\outside.py', r'08_web_data\credential.token'])
def test_rollback_rejects_outside_or_sensitive_manifest_targets(staged, target):
    staged['manifest'][0]['path'] = target
    staged['manifest_file'].write_text(json.dumps(staged['manifest']), encoding='utf-8')
    result = run(staged, 'rollback-native-ipc-v72.ps1', '-Apply')
    assert result.returncode != 0
    assert staged['current'].read_text() == 'new version' and staged['added'].exists()


def test_both_scripts_parse_in_real_windows_powershell51(staged):
    code = "$errors=$null;$tokens=$null;[Management.Automation.Language.Parser]::ParseFile($args[0],[ref]$tokens,[ref]$errors)|Out-Null;if($errors.Count){exit 1}"
    for script in ['deploy-native-ipc-v72.ps1', 'rollback-native-ipc-v72.ps1']:
        # -File avoids the PS5.1 native argument quoting boundary for the path.
        checker = staged['deploy'] / 'parse.ps1'
        checker.write_text("param([string]$Source)\n$parseErrors=$null;$parseTokens=$null\n[Management.Automation.Language.Parser]::ParseFile($Source,[ref]$parseTokens,[ref]$parseErrors)|Out-Null\nif($parseErrors.Count){exit 1}\n", encoding='utf-8')
        result = run(staged, 'parse.ps1', str(staged['deploy'] / script))
        assert result.returncode == 0, result.combined