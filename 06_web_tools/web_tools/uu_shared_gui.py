"""Temporary remote graphical proxy that never owns UU's local receiver.

The installed user's Default-desktop UU GUI remains running. Only a temporary
UU terminal and its fixed remote graphics/input helper belong to this session;
there is deliberately no client-launch, GUI-close or device-disconnect path.

After the remote helper idles out, recovery requires an explicit keyboard_start.
Only a proven stopped helper with no uncertain input may be replaced after its
exact terminal confirms cleanup; previous input is never replayed on recovery.
Receiver preservation describes lifecycle ownership, not phone-side acceptance:
incoming_phone_control_verified stays unknown until an external phone test.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid

from .background_lifecycle import _default_window_pids


class SharedUUGuiError(RuntimeError):
    def __init__(self, code, message, *, may_have_run=False, **details):
        super().__init__(message)
        self.code = code
        self.may_have_run = may_have_run
        self.details = details


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _label(value):
    return ''.join(c for c in str(value or '') if c.isprintable())[:200]


def _objects(value):
    pending = [value]
    count = 0
    while pending and count < 10000:
        item = pending.pop()
        count += 1
        if isinstance(item, dict):
            yield item
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


class SharedUUGuiSession:
    """Coordinate trusted capture tickets and a single owned remote endpoint."""

    ACTIONS = frozenset({'status', 'devices', 'connect', 'keyboard_start',
                         'keyboard_status', 'screenshot', 'click', 'type',
                         'key', 'scroll', 'drag', 'disconnect', 'close'})
    INPUT_ACTIONS = frozenset({'click', 'type', 'key', 'scroll', 'drag'})
    SNAPSHOT_MAX_AGE = 60.0
    NO_INPUT_ERRORS = frozenset({
        'target_identity_mismatch', 'target_not_foreground', 'target_window_stale',
        'interactive_session_required', 'interactive_desktop_required',
        'remote_computer_mismatch', 'remote_key_already_down',
        'invalid_text', 'invalid_keys', 'secure_attention_unsupported',
        'invalid_request', 'nonce_mismatch', 'unsupported_action',
        'request_checksum_mismatch', 'invalid_request_frame',
        'transport_window_protected', 'remote_mouse_already_down',
        'mouse_target_session_mismatch', 'invalid_mouse_coordinates',
        'invalid_mouse_bounds', 'invalid_mouse_button', 'invalid_click_count',
        'invalid_wheel_delta', 'invalid_drag_duration'})

    def __init__(self, data_root, executable, cli_runner=None, *, endpoint_factory,
                 capture_client):
        self.data_root = Path(data_root).resolve()
        self._executable_absolute = Path(executable).is_absolute()
        self.executable = Path(executable).resolve()
        self.gui = (self.executable.parent / 'bin' / 'GameViewer.exe').resolve()
        self.cli = (self.executable.parent / 'bin' / 'uuyc-cli.exe').resolve()
        self._cli_runner = cli_runner or subprocess.run
        self._endpoint_factory = endpoint_factory
        self._capture_client = capture_client
        self._endpoint = None
        self._ready = False
        self._helper_stopped = False
        self._recovery_cleanup_pending = False
        self._target_id = None
        self._target_name = None
        self._receiver_identities = {}
        self._snapshot = None
        self._last_cleanup = None
        self._operations = OrderedDict()
        self._lock = threading.RLock()
        self._phase = 'validation'
        self._operation_id = None

    def _result(self, **values):
        return {'ok': True, 'mode': 'shared_receiver_remote_graphics_proxy',
                'native_uu_viewer': False, 'phone_receiver_preserved': True,
                'incoming_phone_control_verified': None,
                'local_receiver_processes_preserved': True,
                'host_input_used': False, 'foreground_input_used': False,
                'service_processes_touched': False,
                'preserved_gui_pids': sorted(self._receiver_identities),
                'capabilities': {
                    'native_uu_viewer': False, 'phone_receiver_preserved': True,
                    'remote_graphical_proxy': self._ready,
                    'remote_screenshot': self._ready,
                    'remote_mouse_click': self._ready,
                    'remote_keyboard': self._ready,
                    'remote_text': self._ready,
                    'local_foreground_unchanged': True,
                    'verification_required': True}, **values}

    def perform(self, action, arguments=None):
        with self._lock:
            self._phase = 'validation'
            self._operation_id = None
            try:
                if (not isinstance(action, str) or action not in self.ACTIONS
                        or (arguments is not None and not isinstance(arguments, dict))):
                    raise SharedUUGuiError('uu_action_invalid', 'Choose a supported shared UU action and an arguments object')
                args = {} if arguments is None else arguments
                self._phase = action
                if action == 'connect':
                    if (set(args) != {'device_name'} or not isinstance(args['device_name'], str)
                            or not 1 <= len(args['device_name']) <= 200):
                        raise SharedUUGuiError('uu_device_name_required', 'Use one exact device_name from devices')
                    return self._connect(args['device_name'])
                if action in self.INPUT_ACTIONS:
                    return self._input(action, args)
                if args:
                    raise SharedUUGuiError('uu_arguments_invalid', 'This UU action accepts no arguments')
                if action in {'disconnect', 'close'}:
                    return self._close()
                if action == 'devices':
                    self._check_receiver()
                    return self._result(devices=self._public_devices(self._devices()))
                if action == 'status':
                    self._check_receiver()
                    return self._result(receiver_verified=True, logged_in=True,
                        connected=self._ready, keyboard_ready=self._ready,
                        device_name=self._target_name,
                        cleanup=self._last_cleanup)
                if action == 'keyboard_start':
                    return self._keyboard_start()
                if action == 'keyboard_status':
                    return self._keyboard_status()
                if action == 'screenshot':
                    return self._screenshot()
                raise SharedUUGuiError('uu_action_invalid', 'Unsupported shared UU action')
            except SharedUUGuiError as exc:
                return self._result(ok=False, error_code=exc.code, message=str(exc),
                    phase=self._phase, action_may_have_run=exc.may_have_run,
                    remote_effect_verified=False,
                    **({'operation_id': self._operation_id} if self._operation_id else {}),
                    **({'cleanup_complete': False} if action in {'close', 'disconnect'} else {}),
                    **exc.details)
            except Exception:
                return self._result(ok=False, error_code='uu_shared_gui_failed',
                    message='The temporary remote graphical proxy failed; the shared UU receiver was not closed or replaced',
                    phase=self._phase, action_may_have_run=action in self.INPUT_ACTIONS,
                    remote_effect_verified=False,
                    **({'operation_id': self._operation_id} if self._operation_id else {}),
                    **({'cleanup_complete': False} if action in {'close', 'disconnect'} else {}))

    def action(self, action, arguments=None):
        return self.perform(action, arguments)

    def _check_executables(self):
        if (not self._executable_absolute or self.executable.name.casefold() != 'gameviewer.exe'
                or not all(path.is_file() for path in (self.executable, self.gui, self.cli))):
            raise SharedUUGuiError('uu_executable_invalid', 'Use the existing installed UU launcher, bin client and official CLI')

    def _receiver_inventory(self):
        """Observe exact user GUI identities; never acquire process ownership."""
        self._check_executables()
        paths = {str(self.executable).casefold(), str(self.gui).casefold()}
        try:
            import psutil
            default = _default_window_pids()
            found = {}
            for process in psutil.process_iter(['pid', 'exe', 'create_time']):
                try:
                    info = process.info
                    pid = info.get('pid', process.pid)
                    if pid not in default or not _integer(pid, 1, 2147483647):
                        continue
                    path = info.get('exe')
                    if not path or str(Path(path).resolve()).casefold() not in paths:
                        continue
                    created = info.get('create_time')
                    if type(created) not in {int, float} or not math.isfinite(created) or created <= 0:
                        created = process.create_time()
                    if type(created) not in {int, float} or not math.isfinite(created) or created <= 0:
                        continue
                    found[pid] = (str(Path(path).resolve()).casefold(), created)
                except psutil.NoSuchProcess:
                    continue
                except (psutil.Error, OSError, TypeError, ValueError):
                    continue
        except Exception:
            raise SharedUUGuiError('uu_receiver_state_unknown', 'The existing Default-desktop UU receiver could not be verified; it was not touched') from None
        if not found:
            raise SharedUUGuiError('uu_default_receiver_required', 'Keep an existing logged-in UU client on the normal desktop; this route never launches or replaces it')
        return found

    def _check_receiver(self):
        found = self._receiver_inventory()
        if self._receiver_identities and any(found.get(pid) != identity for pid, identity in self._receiver_identities.items()):
            raise SharedUUGuiError('uu_receiver_identity_changed', 'The shared receiver identity changed; no new input or replacement was attempted')
        account = self._cli('status').get('data')
        if not isinstance(account, dict) or account.get('isLoggedIn') is not True:
            raise SharedUUGuiError('uu_login_required', 'The existing shared UU client must already be logged in')
        if not self._receiver_identities:
            self._receiver_identities = found
        return found

    def _cli(self, *command):
        # This module has no mutation-capable CLI operation, even internally.
        if command not in {('status',), ('device', 'list')}:
            raise SharedUUGuiError('uu_cli_action_forbidden', 'Only receiver status and device discovery are supported')
        try:
            completed = self._cli_runner([str(self.cli), *command, '--json'],
                cwd=str(self.cli.parent), stdin=subprocess.DEVNULL,
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired:
            raise SharedUUGuiError('uu_cli_timeout', 'The read-only UU status request timed out') from None
        payload = None
        for stream in (completed.stdout, completed.stderr):
            if not isinstance(stream, str) or len(stream) > 1_000_000:
                continue
            try:
                candidate = json.loads(stream)
            except ValueError:
                continue
            if isinstance(candidate, dict):
                payload = candidate
                break
        if payload is None or completed.returncode != 0 or payload.get('success') is not True:
            raise SharedUUGuiError('uu_cli_response_invalid', 'The read-only UU status request did not return a successful bounded JSON result')
        return payload

    def _devices(self):
        result = {}
        for item in _objects(self._cli('device', 'list')):
            if (all(key in item for key in ('deviceId', 'deviceName', 'isOnline'))
                    and type(item['deviceId']) in {str, int} and str(item['deviceId'])
                    and isinstance(item['deviceName'], str)):
                result[str(item['deviceId'])] = item
        return list(result.values())

    @staticmethod
    def _public_devices(items):
        return [{'device_name': _label(item['deviceName']),
                 'online': item.get('isOnline') is True or item.get('isOnline') == 1,
                 'platform': _label(item.get('platform'))} for item in items]

    def _connect(self, name):
        self._check_receiver()
        if self._endpoint is not None:
            if name != self._target_name:
                raise SharedUUGuiError('uu_session_busy', 'Close this temporary endpoint before choosing another exact device')
            if not self._ready:
                raise SharedUUGuiError('uu_endpoint_cleanup_required', 'The previous endpoint did not become ready; close it before retrying')
            return self._keyboard_status()
        matches = [item for item in self._devices() if item['deviceName'] == name]
        if len(matches) != 1:
            raise SharedUUGuiError('uu_device_ambiguous', 'The exact device name must identify exactly one device')
        if matches[0].get('isOnline') is not True and matches[0].get('isOnline') != 1:
            raise SharedUUGuiError('uu_device_offline', 'The selected remote device is offline')
        self._target_id = str(matches[0]['deviceId'])
        self._target_name = name
        self._start_endpoint()
        return self._keyboard_status()

    def _start_endpoint(self):
        from .uu_remote_keyboard import RemoteKeyboardError
        self._check_receiver()
        if not self._target_id:
            raise SharedUUGuiError('uu_device_not_selected', 'Call connect with one exact online device_name first')
        if self._ready:
            return
        if self._endpoint is not None:
            raise SharedUUGuiError('uu_endpoint_cleanup_required', 'Close the incomplete temporary endpoint before another start')
        if not callable(self._endpoint_factory) or self._capture_client is None:
            raise SharedUUGuiError('uu_graphics_proxy_unavailable', 'The temporary graphical endpoint is not configured')
        self._endpoint = self._endpoint_factory(self.cli, self._target_id)
        self._helper_stopped = False
        self._recovery_cleanup_pending = False
        try:
            self._phase = 'remote_endpoint_start'
            self._endpoint.start(timeout=45)
            origin = self._capture_client.public_origin
            self._phase = 'remote_capture_configure'
            self._request('configure_capture', {'origin': origin})
            self._check_receiver()
            self._ready = True
        except RemoteKeyboardError as exc:
            self._ready = False
            stage = exc.stage if isinstance(exc.stage, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', exc.stage) else None
            raise SharedUUGuiError(exc.code, 'The temporary remote endpoint did not start; the shared UU receiver was preserved',
                may_have_run=exc.may_have_run, **({'stage': stage} if stage else {})) from None
        except Exception:
            # Retain the exact endpoint for explicit retryable cleanup. Do not
            # replace or disconnect any shared GUI, service, server or device.
            self._ready = False
            raise

    def _require_endpoint(self):
        self._check_receiver()
        if self._helper_stopped:
            raise SharedUUGuiError('uu_remote_helper_stopped', 'The remote helper ended; call keyboard_start explicitly before a new screenshot or input')
        if self._endpoint is None or not self._ready:
            raise SharedUUGuiError('uu_remote_input_not_ready', 'Connect one exact remote device before using its graphical proxy')

    def _note_helper_stopped(self):
        self._helper_stopped = True
        self._ready = False
        self._snapshot = None

    def _unknown_inputs(self):
        """Do not exchange a channel while any previous input lacks a receipt."""
        pending = {identity for identity, record in self._operations.items()
                   if record.get('uncertain') is True}
        records = getattr(self._endpoint, '_requests', None)
        if isinstance(records, dict):
            for identity, record in records.items():
                if not isinstance(record, dict) or record.get('action') not in self.INPUT_ACTIONS:
                    continue
                reply = record.get('reply')
                conclusive = isinstance(reply, dict) and reply.get('ok') is True
                if isinstance(reply, dict) and reply.get('ok') is False:
                    error = reply.get('error')
                    conclusive = isinstance(error, dict) and error.get('code') in self.NO_INPUT_ERRORS
                if record.get('unknown_outcome') is True or not conclusive:
                    safe_id = identity if isinstance(identity, str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,80}', identity) else 'unclassified_request'
                    pending.add(safe_id)
        return sorted(pending)

    def _keyboard_start(self):
        """Explicitly recover an idle-stopped endpoint, never an uncertain write."""
        self._check_receiver()
        if self._endpoint is None:
            self._start_endpoint()
            return self._keyboard_status()
        if not self._recovery_cleanup_pending:
            self._phase = 'remote_endpoint_probe'
            try:
                state = self._request('status', {})
            except SharedUUGuiError as exc:
                if exc.code != 'uu_remote_helper_stopped':
                    raise
            else:
                if not self._ready:
                    raise SharedUUGuiError('uu_endpoint_cleanup_required', 'The incomplete endpoint was not proved idle-stopped; close it before another start')
                return self._result(connected=True, keyboard_ready=True,
                    device_name=self._target_name, remote_state=state,
                    endpoint_recovered=False)
        # A retry after incomplete cleanup uses the already proven stopped
        # identity; close() can have closed the channel so a second status is
        # impossible. Keep this exact endpoint until cleanup is confirmed.
        if not self._helper_stopped:
            raise SharedUUGuiError('uu_endpoint_cleanup_required', 'No stopped-helper proof is available for safe recovery')
        pending = self._unknown_inputs()
        if pending:
            raise SharedUUGuiError('uu_remote_outcome_unknown',
                'A previous input has no conclusive receipt; the stopped endpoint was preserved and no input was replayed',
                may_have_run=True, pending_operation_ids=pending[:10])
        self._ready = False
        self._snapshot = None
        self._phase = 'remote_endpoint_cleanup'
        self._recovery_cleanup_pending = True
        try:
            cleanup = self._endpoint.close()
        except Exception:
            raise SharedUUGuiError('uu_endpoint_cleanup_pending', 'The exact stopped terminal could not confirm cleanup; no replacement was started') from None
        self._last_cleanup = dict(cleanup) if isinstance(cleanup, dict) else {'cleanup_complete': False}
        if self._last_cleanup.get('cleanup_complete') is not True:
            raise SharedUUGuiError('uu_endpoint_cleanup_pending', 'The exact stopped terminal has not confirmed full cleanup; no replacement was started')
        self._endpoint = None
        self._helper_stopped = False
        self._recovery_cleanup_pending = False
        for record in self._operations.values():
            if 'result' not in record:
                record['retired'] = True
        # _target_id/name remain the exact originally selected device. No
        # device discovery alias can silently choose a different machine.
        self._start_endpoint()
        result = self._keyboard_status()
        result['endpoint_recovered'] = True
        result['fresh_screenshot_required'] = True
        return result

    def _request(self, action, args, request_id=None, timeout=15):
        from .uu_remote_keyboard import RemoteKeyboardError
        try:
            reply = self._endpoint.request(action, args, request_id=request_id, timeout=timeout)
        except RemoteKeyboardError as exc:
            if exc.code == 'uu_remote_helper_stopped':
                self._note_helper_stopped()
            stage = exc.stage if isinstance(exc.stage, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', exc.stage) else None
            raise SharedUUGuiError(exc.code, 'The temporary remote endpoint did not return a conclusive receipt', may_have_run=exc.may_have_run,
                **({'request_id': exc.request_id} if exc.request_id else {}),
                **({'stage': stage} if stage else {})) from None
        if not isinstance(reply, dict) or type(reply.get('ok')) is not bool:
            raise SharedUUGuiError('uu_remote_response_invalid', 'The remote endpoint returned no conclusive receipt', may_have_run=action in self.INPUT_ACTIONS)
        if reply['ok'] is not True:
            error = reply.get('error')
            code = error.get('code') if isinstance(error, dict) else None
            if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,79}', code):
                code = 'uu_remote_action_rejected'
            if code == 'uu_remote_helper_stopped':
                self._note_helper_stopped()
            raise SharedUUGuiError(code, 'The remote graphical endpoint rejected the operation',
                                   may_have_run=action in self.INPUT_ACTIONS and code not in self.NO_INPUT_ERRORS)
        result = reply.get('result')
        if not isinstance(result, dict):
            raise SharedUUGuiError('uu_remote_response_invalid', 'The remote endpoint returned no structured result', may_have_run=action in self.INPUT_ACTIONS)
        return result

    def _keyboard_status(self):
        self._check_receiver()
        if not self._ready:
            return self._result(connected=False, keyboard_ready=False, device_name=self._target_name,
                **({'helper_stopped': True, 'next_action': 'keyboard_start'} if self._helper_stopped else {}))
        return self._result(connected=True, keyboard_ready=True, device_name=self._target_name,
                            remote_state=self._request('status', {}))

    @staticmethod
    def _bounds(value):
        if (not isinstance(value, dict) or set(value) != {'left', 'top', 'width', 'height'}
                or not _integer(value.get('left'), -32768, 32767)
                or not _integer(value.get('top'), -32768, 32767)
                or not _integer(value.get('width'), 1, 8192)
                or not _integer(value.get('height'), 1, 8192)
                or value['width'] * value['height'] > 32_000_000):
            raise SharedUUGuiError('uu_remote_capture_invalid', 'The screenshot has no valid physical remote desktop bounds')
        return dict(value)

    @staticmethod
    def _target(value):
        if not isinstance(value, dict):
            raise SharedUUGuiError('uu_remote_target_invalid', 'Use the exact foreground identity from a fresh screenshot')
        result = {key: value.get(key) for key in ('hwnd', 'pid', 'creation_time_utc_ticks')}
        if (not _integer(result['hwnd'], 1, 9223372036854775807)
                or not _integer(result['pid'], 1, 2147483647)
                or not isinstance(result['creation_time_utc_ticks'], str)
                or not re.fullmatch(r'[1-9][0-9]{0,18}', result['creation_time_utc_ticks'])
                or int(result['creation_time_utc_ticks']) > 9223372036854775807):
            raise SharedUUGuiError('uu_remote_target_invalid', 'Use the exact foreground identity from a fresh screenshot')
        return result

    def _screenshot(self):
        self._require_endpoint()
        self._snapshot = None
        self._phase = 'capture_ticket'
        try:
            ticket = self._capture_client.issue()
        except Exception:
            raise SharedUUGuiError('uu_capture_ticket_failed', 'The local screenshot receiver could not issue a ticket') from None
        capture_id = ticket.get('capture_id') if isinstance(ticket, dict) else None
        try:
            if (not isinstance(ticket, dict)
                    or not all(isinstance(ticket.get(key), str) and ticket[key] for key in ('upload_url', 'token', 'capture_id'))):
                raise SharedUUGuiError('uu_capture_ticket_invalid', 'The capture receiver did not issue a complete one-use ticket')
            self._phase = 'remote_capture'
            receipt = self._request('capture', {key: ticket[key] for key in ('upload_url', 'token', 'capture_id')}, timeout=60)
            if receipt.get('capture_id') != capture_id:
                raise SharedUUGuiError('uu_remote_capture_invalid', 'The remote capture receipt did not match its ticket')
            bounds = self._bounds(receipt.get('remote_bounds'))
            self._phase = 'capture_upload_wait'
            try:
                metadata = self._capture_client.wait(capture_id)
            except TimeoutError:
                raise SharedUUGuiError('uu_capture_upload_timeout', 'The remote screenshot upload did not complete within its bounded wait') from None
            except Exception:
                raise SharedUUGuiError('uu_capture_upload_failed', 'The local screenshot receiver could not confirm a valid remote image') from None
            mime_type = metadata.get('mime_type') if isinstance(metadata, dict) else None
            receipt_mime = receipt.get('mime_type', 'image/png')
            extension = '.jpg' if mime_type == 'image/jpeg' else '.png'
            image_format = 'jpeg' if mime_type == 'image/jpeg' else 'png'
            if (not isinstance(metadata, dict) or mime_type not in ('image/png', 'image/jpeg')
                    or receipt_mime != mime_type or receipt.get('format', image_format) != image_format
                    or not isinstance(metadata.get('path'), str) or not metadata['path']
                    or Path(metadata['path']).suffix != extension
                    or type(metadata.get('width')) is not int or type(metadata.get('height')) is not int
                    or metadata.get('width') != bounds['width'] or metadata.get('height') != bounds['height']
                    or type(receipt.get('width', bounds['width'])) is not int
                    or type(receipt.get('height', bounds['height'])) is not int
                    or receipt.get('width', bounds['width']) != bounds['width']
                    or receipt.get('height', bounds['height']) != bounds['height']):
                raise SharedUUGuiError('uu_remote_capture_invalid', 'The received image format or dimensions did not match its physical remote desktop bounds')
            if 'size_bytes' in receipt:
                size = receipt['size_bytes']
                stored_size = metadata.get('size', metadata.get('size_bytes'))
                if (type(size) is not int or size <= 0 or type(stored_size) is not int
                        or stored_size != size):
                    raise SharedUUGuiError('uu_remote_capture_invalid', 'The received image size did not match the remote capture receipt')
            digest = receipt.get('sha256')
            if (not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)
                    or metadata.get('sha256') != digest):
                raise SharedUUGuiError('uu_remote_capture_invalid', 'The received image digest did not match the remote capture receipt')
            foreground = receipt.get('foreground')
            try:
                target = self._target(foreground)
            except SharedUUGuiError:
                target = None
            self._snapshot = {'bounds': bounds, 'target': target, 'at': time.monotonic()}
            return self._result(device_name=self._target_name, capture=dict(metadata),
                remote_bounds=bounds, remote_foreground=foreground,
                capture_id=capture_id, capture_source='remote_user_desktop_graphics_proxy')
        finally:
            cancel = getattr(self._capture_client, 'cancel', None)
            if callable(cancel) and capture_id:
                try:
                    cancel(capture_id)
                except Exception:
                    pass

    def _fresh_snapshot(self):
        if (self._snapshot is None
                or time.monotonic() - self._snapshot['at'] > self.SNAPSHOT_MAX_AGE):
            raise SharedUUGuiError('uu_remote_snapshot_required', 'Take a fresh remote screenshot before input')
        return self._snapshot

    @staticmethod
    def _keys(keys):
        values = keys.split('+') if isinstance(keys, str) else keys
        allowed = {'CTRL', 'CONTROL', 'ALT', 'SHIFT', 'ENTER', 'RETURN', 'TAB',
                   'BACKSPACE', 'ESC', 'ESCAPE', 'SPACE', 'DELETE', 'INSERT',
                   'HOME', 'END', 'PAGEUP', 'PAGEDOWN', 'LEFT', 'UP', 'RIGHT', 'DOWN'}
        allowed.update(chr(value) for value in range(ord('A'), ord('Z') + 1))
        allowed.update(str(value) for value in range(10))
        allowed.update('F' + str(value) for value in range(1, 25))
        if not isinstance(values, list) or not 1 <= len(values) <= 5 or any(not isinstance(key, str) for key in values):
            raise SharedUUGuiError('uu_remote_keys_invalid', 'Use a fixed chord of 1-5 supported keys')
        values = [key.strip().upper() for key in values]
        canonical = [{'CONTROL': 'CTRL', 'RETURN': 'ENTER', 'ESCAPE': 'ESC'}.get(key, key) for key in values]
        if any(key not in allowed for key in values) or len(set(canonical)) != len(canonical) or {'CTRL', 'ALT', 'DELETE'} <= set(canonical):
            raise SharedUUGuiError('uu_remote_keys_invalid', 'Unsupported, duplicate or secure-attention keys are not accepted')
        return values

    def _input_plan(self, action, args):
        snapshot = self._fresh_snapshot()
        if action in {'type', 'key'}:
            main = 'text' if action == 'type' else 'keys'
            if set(args) - {main, 'expected_target', 'operation_id', 'activate'} or main not in args:
                raise SharedUUGuiError('uu_arguments_invalid', 'Use text/keys with optional expected_target, operation_id and activate')
            target = snapshot['target']
            if target is None:
                raise SharedUUGuiError('uu_remote_target_invalid', 'The fresh screenshot did not identify an input-capable remote window')
            if 'expected_target' in args and self._target(args['expected_target']) != target:
                raise SharedUUGuiError('uu_remote_target_mismatch', 'The supplied target differs from the fresh screenshot foreground')
            activate = args.get('activate', True)
            if type(activate) is not bool:
                raise SharedUUGuiError('uu_arguments_invalid', 'activate must be a boolean')
            if action == 'type':
                text = args['text']
                if not isinstance(text, str) or not 1 <= len(text) <= 40000 or '\0' in text:
                    raise SharedUUGuiError('uu_remote_text_invalid', 'Use 1-40000 valid non-NUL text characters')
                try:
                    text.encode('utf-8')
                except UnicodeError:
                    raise SharedUUGuiError('uu_remote_text_invalid', 'The text contains an invalid Unicode sequence') from None
                text = text.replace('\r\n', '\n').replace('\r', '\n')
                values = [text[i:i + 1000] for i in range(0, len(text), 1000)]
            else:
                values = [self._keys(args['keys'])]
            return [{main: value, 'expect': dict(target), 'activate': activate} for value in values]
        fields = {'x', 'y', 'operation_id'}
        fields |= {'button', 'count'} if action == 'click' else {'vertical', 'horizontal'} if action == 'scroll' else {'to_x', 'to_y', 'duration_ms'}
        if set(args) - fields or not {'x', 'y'} <= set(args):
            raise SharedUUGuiError('uu_arguments_invalid', 'Mouse arguments must match the fixed screenshot-coordinate schema')
        bounds = snapshot['bounds']
        def point(x, y):
            if not _integer(x, 0, bounds['width'] - 1) or not _integer(y, 0, bounds['height'] - 1):
                raise SharedUUGuiError('uu_remote_coordinates_invalid', 'Mouse coordinates must be integer pixels inside the latest screenshot')
            return x + bounds['left'], y + bounds['top']
        x, y = point(args['x'], args['y'])
        payload = {'x': x, 'y': y, 'bounds': dict(bounds)}
        if action == 'click':
            button, count = args.get('button', 'left'), args.get('count', 1)
            if not isinstance(button, str) or button not in {'left', 'right', 'middle'} or not _integer(count, 1, 2):
                raise SharedUUGuiError('uu_arguments_invalid', 'Use a supported mouse button and count 1 or 2')
            payload.update(button=button, count=count)
        elif action == 'scroll':
            vertical, horizontal = args.get('vertical', 0), args.get('horizontal', 0)
            if not _integer(vertical, -12000, 12000) or not _integer(horizontal, -12000, 12000) or not (vertical or horizontal):
                raise SharedUUGuiError('uu_arguments_invalid', 'Use nonzero bounded integer wheel deltas')
            payload.update(vertical=vertical, horizontal=horizontal)
        else:
            tx, ty = point(args.get('to_x'), args.get('to_y'))
            duration = args.get('duration_ms', 500)
            if not _integer(duration, 50, 5000):
                raise SharedUUGuiError('uu_arguments_invalid', 'Drag duration_ms must be between 50 and 5000')
            payload.update(to_x=tx, to_y=ty, duration_ms=duration)
        return [payload]

    def _input(self, action, args):
        self._require_endpoint()
        opid = args.get('operation_id', uuid.uuid4().hex)
        if not isinstance(opid, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,64}', opid):
            raise SharedUUGuiError('uu_operation_id_invalid', 'Use a stable operation_id of 1-64 supported characters')
        self._operation_id = opid
        try:
            canonical = json.dumps({'action': action, 'arguments': {key: value for key, value in args.items() if key != 'operation_id'}},
                                   ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        except (ValueError, TypeError, UnicodeError):
            raise SharedUUGuiError('uu_arguments_invalid', 'Input arguments must match the fixed JSON schema') from None
        try:
            digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        except UnicodeError:
            raise SharedUUGuiError('uu_remote_text_invalid', 'Input contains an invalid Unicode sequence') from None
        record = self._operations.get(opid)
        if record is not None and record['digest'] != digest:
            raise SharedUUGuiError('uu_operation_id_conflict', 'This operation_id was already used with different arguments')
        if record is not None and record.get('retired') is True:
            raise SharedUUGuiError('uu_operation_endpoint_retired',
                'This operation belonged to the previous stopped endpoint and will not be replayed; verify a fresh screenshot before a new operation',
                delivered_chunks=len(record['receipts']))
        pending = [identity for identity, state in self._operations.items()
                   if state.get('uncertain') is True and identity != opid]
        if pending:
            raise SharedUUGuiError('uu_remote_outcome_unknown',
                'A previous input has no conclusive receipt; resolve it with the same operation_id before another input',
                may_have_run=True, pending_operation_ids=pending[:10])
        if record is None:
            if len(self._operations) >= 128:
                raise SharedUUGuiError('uu_operation_limit', 'Close this bounded temporary endpoint before more operations')
            plan = self._input_plan(action, args)
            record = {'digest': digest, 'plan': plan, 'receipts': []}
            self._operations[opid] = record
        if 'result' in record:
            return json.loads(json.dumps(record['result']))
        # A mouse operation can alter layout/focus. Invalidate before dispatch,
        # including uncertain receipts; only a fresh image may select a target.
        if action in {'click', 'scroll', 'drag'}:
            self._snapshot = None
        for index in range(len(record['receipts']), len(record['plan'])):
            try:
                receipt = self._request(action, record['plan'][index], request_id=f'{opid}:{index}')
            except SharedUUGuiError as exc:
                # A later stopped-helper observation says nothing about the
                # result of an earlier uncertain write under this same ID.
                record['uncertain'] = exc.may_have_run or (record.get('uncertain') is True and exc.code == 'uu_remote_helper_stopped')
                raise
            except Exception:
                record['uncertain'] = True
                raise
            record['receipts'].append(receipt)
            record['uncertain'] = False
        result = self._result(action=action, device_name=self._target_name,
            operation_id=opid, delivered_chunks=len(record['receipts']),
            transport_results=list(record['receipts']),
            verification_required=True, remote_effect_verified=False)
        record['result'] = result
        return result

    def _close(self):
        self._snapshot = None
        self._ready = False
        if self._endpoint is None:
            return self._result(connected=False, cleanup_complete=True, cleanup=self._last_cleanup,
                                shared_receiver_closed=False, device_disconnected=False)
        # The endpoint implementation owns an exact terminal channel. Do not
        # inspect or close the user's GUI, services, server or device globally.
        cleanup = self._endpoint.close()
        if not isinstance(cleanup, dict):
            cleanup = {'cleanup_complete': False}
        self._last_cleanup = dict(cleanup)
        complete = cleanup.get('cleanup_complete') is True
        if complete:
            self._endpoint = None
            self._helper_stopped = False
            self._recovery_cleanup_pending = False
            self._target_id = self._target_name = None
            self._operations.clear()
        return self._result(ok=complete, connected=False, cleanup_complete=complete,
            cleanup=cleanup, shared_receiver_closed=False, device_disconnected=False,
            **({} if complete else {'error_code': 'uu_endpoint_cleanup_pending',
                'message': 'The exact temporary endpoint has not confirmed full cleanup; it was retained for retry'}))
