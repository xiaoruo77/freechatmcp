"""No GUI or child processes: mock the cross-session background launcher API."""
import os
from types import SimpleNamespace

import pytest

from web_tools import windows_user_process as module


@pytest.fixture
def native_background(monkeypatch):
    if os.name != 'nt':
        pytest.skip('Win32 API shape tests require Windows ctypes declarations')
    state = {'calls': [], 'clock': 0.0, 'env_fail': False, 'create_fail': False,
             'wait_fail': False, 'exit_fail': False, 'stall': False, 'terminated': False,
             'exit_code': 0}

    def handle(value):
        return getattr(value, 'value', value)

    class Kernel:
        # Deliberately no CreatePipe/CreateFile/SetHandleInformation methods:
        # this launcher must never create cross-session inherited stdio.
        def CloseHandle(self, value):
            state['calls'].append(('close', handle(value)))
            return True

        def WaitForSingleObject(self, process, milliseconds):
            state['calls'].append(('wait', handle(process), milliseconds))
            if state['wait_fail']:
                return 0xFFFFFFFF
            if state['stall'] and not state['terminated']:
                state['clock'] += .2
                return module._WAIT_TIMEOUT
            return module._WAIT_OBJECT_0

        def TerminateProcess(self, process, code):
            state['calls'].append(('terminate', handle(process), code))
            state['terminated'] = True
            return True

        def GetExitCodeProcess(self, process, target):
            state['calls'].append(('exit_code', handle(process)))
            if state['exit_fail']:
                return False
            target._obj.value = 1 if state['terminated'] else state['exit_code']
            return True

    class UserEnv:
        def CreateEnvironmentBlock(self, target, token, inherit):
            state['calls'].append(('environment', handle(token), inherit))
            if state['env_fail']:
                return False
            target._obj.value = 202
            return True

        def DestroyEnvironmentBlock(self, environment):
            state['calls'].append(('destroy_environment', handle(environment)))
            return True

    class Advapi:
        def CreateProcessAsUserW(self, token, executable, command_line, process_security,
                                 thread_security, inherit, flags, environment, cwd,
                                 startup_pointer, process_pointer):
            startup = startup_pointer._obj
            state['creation'] = dict(token=handle(token), executable=executable,
                command=command_line.value, inherit=inherit, flags=flags,
                environment=handle(environment), cwd=cwd, desktop=startup.lpDesktop,
                startup_flags=startup.dwFlags,
                stdio=(startup.hStdInput, startup.hStdOutput, startup.hStdError))
            state['calls'].append(('create',))
            if state['create_fail']:
                return False
            process_pointer._obj.hProcess = 303
            process_pointer._obj.hThread = 404
            process_pointer._obj.dwProcessId = 505
            return True

    monkeypatch.setattr(module, 'is_local_system', lambda: True)
    monkeypatch.setattr(module, '_active_user_token', lambda: module.wintypes.HANDLE(101))
    monkeypatch.setattr(module, '_executable', lambda command: r'C:\Test Runtime\python.exe')
    monkeypatch.setattr(module, '_kernel32', Kernel())
    monkeypatch.setattr(module, '_userenv', UserEnv())
    monkeypatch.setattr(module, '_advapi32', Advapi())
    monkeypatch.setattr(module, '_win32_error', lambda operation: OSError(operation+' failed'))
    monkeypatch.setattr(module.time, 'monotonic', lambda: state['clock'])
    return state


def test_system_background_uses_user_environment_and_explicit_desktop_without_inherited_stdio(native_background, tmp_path):
    state = native_background
    result = module.run_background_command_as_active_user(['python.exe', 'background launcher.py'], tmp_path)
    assert result == module.UserProcessResult(0, '', '')
    creation = state['creation']
    assert creation['desktop'] == 'winsta0\\default'
    assert creation['token'] == 101 and creation['environment'] == 202
    assert ('environment', 101, False) in state['calls']
    assert creation['inherit'] is False
    assert creation['startup_flags'] & module._STARTF_USESTDHANDLES == 0
    assert creation['stdio'] == (None, None, None)
    assert creation['flags'] & module._CREATE_UNICODE_ENVIRONMENT
    assert creation['flags'] & module._CREATE_NEW_PROCESS_GROUP
    assert creation['flags'] & module._CREATE_NO_WINDOW
    assert creation['cwd'] == str(tmp_path)
    assert creation['command'].startswith('"C:\\Test Runtime\\python.exe"')
    assert state['calls'][-4:] == [('close', 404), ('close', 303), ('destroy_environment', 202), ('close', 101)]
    assert not state['terminated']


def test_background_timeout_terminates_only_new_launcher_and_releases_resources(native_background, tmp_path):
    state = native_background
    state['stall'] = True
    result = module.run_background_command_as_active_user(['python.exe'], tmp_path, timeout=1)
    assert result.timed_out and result.return_code == 1
    assert result.stdout == result.stderr == ''
    assert [call for call in state['calls'] if call[0] == 'terminate'] == [('terminate', 303, 1)]
    assert state['clock'] < 2
    assert ('destroy_environment', 202) in state['calls']
    assert ('close', 101) in state['calls']


@pytest.mark.parametrize('failure,operation', [
    ('env_fail', 'CreateEnvironmentBlock'), ('create_fail', 'CreateProcessAsUserW'),
    ('wait_fail', 'WaitForSingleObject'), ('exit_fail', 'GetExitCodeProcess')])
def test_every_win32_failure_releases_token_environment_and_created_handles(native_background, tmp_path, failure, operation):
    state = native_background
    state[failure] = True
    with pytest.raises(OSError, match=operation):
        module.run_background_command_as_active_user(['python.exe'], tmp_path)
    assert ('close', 101) in state['calls']
    assert (('destroy_environment', 202) in state['calls']) is (failure != 'env_fail')
    if failure in {'wait_fail', 'exit_fail'}:
        assert ('close', 303) in state['calls'] and ('close', 404) in state['calls']
    if failure == 'wait_fail':
        assert ('terminate', 303, 1) in state['calls']
    if failure in {'env_fail', 'create_fail'}:
        assert not state['terminated']


def test_system_background_preserves_actual_nonzero_exit_code(native_background, tmp_path):
    native_background['exit_code'] = 17
    result = module.run_background_command_as_active_user(['python.exe'], tmp_path)
    assert result.return_code == 17 and not result.timed_out


def test_current_user_background_is_hidden_and_sends_no_output_to_parent(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(module, 'is_local_system', lambda: False)
    monkeypatch.setattr(module, '_active_user_token', lambda: pytest.fail('must not duplicate a token for current user'), raising=False)

    def run(command, **kwargs):
        seen.update(command=command, **kwargs)
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(module.subprocess, 'run', run)
    result = module.run_background_command_as_active_user(['python.exe', 'launcher.py'], tmp_path, timeout=1000)
    assert result == module.UserProcessResult(7, '', '')
    assert seen['stdin'] == seen['stdout'] == seen['stderr'] == module.subprocess.DEVNULL
    assert seen['timeout'] == 30
    assert seen['cwd'] == str(tmp_path)
    if os.name == 'nt':
        assert seen['creationflags'] & module.subprocess.CREATE_NO_WINDOW


def test_current_user_timeout_returns_empty_output_and_timeout_result(monkeypatch, tmp_path):
    monkeypatch.setattr(module, 'is_local_system', lambda: False)
    def timeout(*args, **kwargs):
        raise module.subprocess.TimeoutExpired(args[0], kwargs['timeout'], output=b'not returned')
    monkeypatch.setattr(module.subprocess, 'run', timeout)
    result = module.run_background_command_as_active_user(['python.exe'], tmp_path)
    assert result == module.UserProcessResult(-1, '', '', timed_out=True)


@pytest.mark.parametrize('timeout', [True, 0, -1, float('nan'), float('inf'), '10'])
def test_invalid_timeout_never_starts_a_process(monkeypatch, tmp_path, timeout):
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: pytest.fail('no launch'))
    with pytest.raises(ValueError):
        module.run_background_command_as_active_user(['python.exe'], tmp_path, timeout)


@pytest.mark.parametrize('command', [[], 'python.exe', [''], ['python.exe', 'bad\0arg']])
def test_invalid_argv_never_starts_a_process(monkeypatch, tmp_path, command):
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: pytest.fail('no launch'))
    with pytest.raises(ValueError):
        module.run_background_command_as_active_user(command, tmp_path)
