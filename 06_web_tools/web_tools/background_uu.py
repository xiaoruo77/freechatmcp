"""Own one UU graphical connection on the caller's non-input desktop.

The official CLI is used only for device discovery and connection lifecycle.
Remote interaction goes through the actual private UU video window.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import time
from typing import Callable
import uuid

from . import uu_window_transport


class UUBackgroundError(RuntimeError):
    def __init__(self, code: str, message: str, *, may_have_run: bool = False,
                 diagnostics: dict | None = None):
        super().__init__(message)
        self.code = code
        self.may_have_run = may_have_run
        self.diagnostics=diagnostics or {}


def _objects(value):
    """Walk bounded JSON structures without exposing their private fields."""
    pending=[value]
    count=0
    while pending and count<10000:
        item=pending.pop();count+=1
        if isinstance(item,dict):
            yield item
            pending.extend(item.values())
        elif isinstance(item,list):pending.extend(item)


def _label(value):
    return ''.join(c for c in str(value or '') if c.isprintable())[:200]


def _cli_diagnostics(payload,returncode):
    """Keep only CLI error identifiers, never messages, IDs or raw output."""
    result={'cli_return_code':returncode}
    candidates=[payload.get('errorCode'),payload.get('code')]
    if isinstance(payload.get('error'),dict):candidates.extend([payload['error'].get('code'),payload['error'].get('errorCode')])
    for code in candidates:
        if type(code) is int and -1_000_000<=code<=1_000_000:
            result['cli_error_code']=code;break
        if isinstance(code,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}',code):
            result['cli_error_code']=code;break
    return result


class UUBackgroundSession:
    ACTIONS=frozenset({'devices','status','connect','inspect','screenshot','click','type',
                      'key','scroll','drag','resize','disconnect','close',
                      'keyboard_start','keyboard_status',
                      'ui_snapshot','ui_click','ui_type','ui_toggle','ui_expand','ui_collapse'})
    INPUT_ACTIONS=frozenset({'click','type','key','scroll','drag','resize','ui_click','ui_type',
                             'ui_toggle','ui_expand','ui_collapse'})

    def __init__(self, data_root: Path, executable: Path, windows: Callable,
                 launch: Callable, *, owned_processes: Callable | None = None,
                 cli_runner: Callable | None = None, idle_handoff: Callable | None = None,
                 keyboard_factory: Callable | None = None):
        self.data_root=Path(data_root).resolve()
        self.executable=Path(executable).resolve()
        self.gui=(self.executable.parent/'bin'/'GameViewer.exe').resolve()
        self.cli=(self.executable.parent/'bin'/'uuyc-cli.exe').resolve()
        self.windows=windows
        self.launch=launch
        self.owned_processes=owned_processes
        self._cli_runner=cli_runner
        self._idle_handoff=idle_handoff
        self._handoff=None
        self._keyboard_factory=keyboard_factory
        self._keyboard=None
        self._keyboard_ready=False
        self._last_remote_target=None
        self._last_keyboard_cleanup=None
        self._owned={}
        self._started=False
        self._target_id=None
        self._target_name=None
        self._connection_requested=False
        self._connected=False
        self._transport=None
        self._ui=None
        self._ui_ids={}
        self._deadline=None
        self._action_may_have_run=False

    def _result(self, **values):
        return {'ok':True,'host_input_used':False,'foreground_input_used':False,
                'capabilities': {'automatic_private_connection': True,
                                 'private_window_capture': True,
                                 'remote_mouse_click': True,
                                 'remote_keyboard': self._keyboard_ready,
                                 'remote_text': self._keyboard_ready,
                                 'keyboard_transport': 'uu_remote_user_session' if self._keyboard_ready else None,
                                 'verification_required': True}, **values}

    def perform(self, action: str, arguments: dict | None = None) -> dict:
        self._action_may_have_run=False
        self._deadline=time.monotonic()+90 if action in {'connect','keyboard_start'} else None
        phase='validation'
        try:
            if not isinstance(action,str) or action not in self.ACTIONS or (arguments is not None and not isinstance(arguments,dict)):
                raise UUBackgroundError('uu_action_invalid','Choose a supported UU action and arguments object')
            args={} if arguments is None else arguments
            if action in {'key', 'type'}:
                phase='remote_keyboard'
                return self._keyboard_input(action,args)
            if action in {'keyboard_start','keyboard_status'}:
                if args:raise UUBackgroundError('uu_arguments_invalid','This keyboard action accepts no arguments')
                phase=action
                if action=='keyboard_start':self._ensure_keyboard()
                return self._keyboard_status()
            if action=='connect':
                if set(args)!={'device_name'} or not isinstance(args['device_name'],str) or not args['device_name'] or len(args['device_name'])>200:
                    raise UUBackgroundError('uu_device_name_required','Choose one exact device_name from devices; device IDs are not accepted')
                phase='connect'
                return self._connect(args['device_name'])
            if action in {'status','devices','disconnect','close'}:
                if args:raise UUBackgroundError('uu_arguments_invalid','This UU action does not accept arguments')
                phase=action
                return getattr(self,'_'+action)()
            if action in {'ui_snapshot','ui_click','ui_type','ui_toggle','ui_expand','ui_collapse'}:
                phase=action
                return self._ui_action(action,args)
            phase='viewer_selection'
            hwnd=self._viewer()
            if action=='inspect':
                if args:raise UUBackgroundError('uu_arguments_invalid','inspect does not accept arguments')
                phase='window_inspect'
                return self._result(device_name=self._target_name,window=self._get_transport().inspect(hwnd))
            if action=='screenshot':
                if args:raise UUBackgroundError('uu_arguments_invalid','screenshot does not accept arguments')
                phase='window_capture'
                return self._screenshot(hwnd)
            phase='window_input'
            self._action_may_have_run=True
            outcome=self._get_transport().perform(hwnd,action,args)
            self._last_remote_target=None
            if not isinstance(outcome,dict) or outcome.get('ok') is not True:
                return {'ok':False,'error_code':'uu_window_input_failed',
                        'phase':phase,
                        'message':'The private UU window did not accept this input; no foreground fallback',
                        'host_input_used':False,'action_may_have_run':True,'remote_effect_verified':False}
            return self._result(action=action,device_name=self._target_name,transport=outcome,
                                verification_required=True,remote_effect_verified=False)
        except uu_window_transport.WindowTransportError as exc:
            return {'ok':False,'error_code':exc.code,'message':str(exc),'phase':phase,
                    'host_input_used':False,'foreground_input_used':False,
                    'action_may_have_run':self._action_may_have_run,'remote_effect_verified':False,
                    **({'cleanup_complete':False} if action=='close' else {})}
        except UUBackgroundError as exc:
            return {'ok':False,'error_code':exc.code,'message':str(exc),
                    'phase':phase,
                    'host_input_used':False,'foreground_input_used':False,
                    'action_may_have_run':exc.may_have_run or self._action_may_have_run,'remote_effect_verified':False,
                    'connection_request_dispatched':self._connection_requested,
                    **({'diagnostics':exc.diagnostics} if exc.diagnostics else {}),
                    **({'cleanup_complete':False} if action=='close' else {})}
        except Exception:
            return {'ok':False,'error_code':'uu_background_failed',
                    'phase':phase,
                    'message':'Private UU operation failed; no user-desktop fallback was attempted',
                    'host_input_used':False,'foreground_input_used':False,
                    'action_may_have_run':action in self.INPUT_ACTIONS or action in {'connect','disconnect','close'},
                    'remote_effect_verified':False,**({'cleanup_complete':False} if action=='close' else {})}
        finally:
            self._deadline=None

    def action(self, action: str, arguments: dict | None = None) -> dict:
        return self.perform(action,arguments)

    def _cli(self, *command: str, timeout: float = 5) -> dict:
        if not self.cli.is_file():
            raise UUBackgroundError('uu_cli_unavailable','The installed official UU CLI is unavailable')
        changing=command[:2] in {('device','connect'),('device','disconnect')}
        if self._deadline is not None:
            remaining=self._deadline-time.monotonic()
            if remaining<=0:
                raise UUBackgroundError('uu_operation_timeout','UU connection did not finish within its bounded operation deadline',may_have_run=self._action_may_have_run)
            timeout=min(timeout,remaining)
        try:
            completed=(self._cli_runner or subprocess.run)([str(self.cli),*command,'--json'],cwd=str(self.cli.parent),
                stdin=subprocess.DEVNULL,capture_output=True,text=True,encoding='utf-8',errors='replace',
                timeout=min(25,max(.1,timeout)),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except subprocess.TimeoutExpired:
            raise UUBackgroundError('uu_cli_timeout','UU connection command timed out; its outcome must be checked before retrying',may_have_run=changing) from None
        payload=None
        for stream in (completed.stdout,completed.stderr):
            if not isinstance(stream,str) or len(stream)>1_000_000:continue
            try:
                candidate=json.loads(stream)
                if isinstance(candidate,dict):payload=candidate;break
            except ValueError:continue
        if payload is None:
            raise UUBackgroundError('uu_cli_response_invalid','UU CLI returned no valid bounded JSON result',may_have_run=changing)
        if completed.returncode==102:
            raise UUBackgroundError('uu_client_not_ready','The private UU client is not ready yet',
                                    diagnostics=_cli_diagnostics(payload,completed.returncode))
        if completed.returncode!=0 or payload.get('success') is not True:
            raise UUBackgroundError('uu_cli_failed','UU CLI rejected the requested operation',may_have_run=changing,
                                    diagnostics=_cli_diagnostics(payload,completed.returncode))
        return payload

    def _remember(self,pid):
        import psutil
        if type(pid) is not int or pid<=0:return False
        try:
            process=psutil.Process(pid)
            executable=str(Path(process.exe()).resolve()).casefold()
            if executable not in {str(self.executable).casefold(),str(self.gui).casefold()}:return False
            self._owned[pid]=(process.create_time(),executable)
            return True
        except psutil.Error:return False

    def _owned_live(self,pid):
        import psutil
        if self.owned_processes is not None and pid not in set(self.owned_processes()):return False
        if pid not in self._owned:
            if self.owned_processes is None or not self._remember(pid):return False
        try:
            process=psutil.Process(pid)
            return (process.create_time(),str(Path(process.exe()).resolve()).casefold())==self._owned[pid]
        except psutil.Error:return False

    def _assert_no_existing_ui(self):
        import psutil
        expected={str(self.executable).casefold(),str(self.gui).casefold()}
        for process in psutil.process_iter(['pid','exe']):
            try:
                exe=process.info.get('exe')
                if exe and str(Path(exe).resolve()).casefold() in expected and not self._owned_live(process.pid):
                    raise UUBackgroundError('uu_existing_user_client','UU is already running outside this session. Its window will not be moved, closed or reused')
            except psutil.Error:continue

    def _ensure_client(self):
        if not self._started:
            if self._idle_handoff is not None:
                self._handoff=self._idle_handoff()
                if not self._handoff.get('ok'):
                    raise UUBackgroundError(self._handoff.get('error_code','uu_client_handoff_unavailable'),
                        self._handoff.get('message','The existing UU client could not be prepared without disturbing user work'))
            self._assert_no_existing_ui()
            self._action_may_have_run=True
            launched=self.launch()
            if not isinstance(launched,dict):
                raise UUBackgroundError('uu_private_launch_failed','The private UU launcher did not return an ownership result')
            # A GUI wrapper may have exited or shown only a splash. Neither is
            # proof the CLI is ready; ownership is established before polling.
            self._remember(launched.get('launched_pid'))
            members=set(self.owned_processes()) if self.owned_processes is not None else set()
            for window in launched.get('windows',[]):
                pid=window.get('pid')
                if self._private(window) and (pid in members or (self.owned_processes is None and launched.get('process_job_ui_restrictions') is True)):
                    self._remember(pid)
            if not self._owned and not members:
                raise UUBackgroundError('uu_private_launch_failed','No owned private UU client process was observed')
            self._started=True
        self._assert_no_existing_ui()
        deadline=min(time.monotonic()+15,self._deadline if self._deadline is not None else float('inf'))
        phase='status';diagnostics={}
        while True:
            try:
                phase='status'
                # The top-level status command owns account state. In the
                # actual CLI, device status reports connections only.
                status=self._cli('status',timeout=2)
                account=status.get('data')
                if not isinstance(account,dict) or account.get('isLoggedIn') is not True:
                    phase='login'
                    diagnostics={}
                else:
                    phase='device_list'
                    # A responsive status endpoint precedes account recovery
                    # and usable device discovery during real UU startup.
                    return self._cli('device','list',timeout=2)
            except UUBackgroundError as exc:
                if exc.code not in {'uu_client_not_ready','uu_cli_failed','uu_cli_timeout','uu_cli_response_invalid'}:raise
                diagnostics={**exc.diagnostics,'phase':phase,'last_error_code':exc.code}
            if time.monotonic()>=deadline:
                if phase=='login':
                    raise UUBackgroundError('uu_login_pending','The private UU account is not signed in or is still restoring. No login or device connection was attempted; wait or explicitly sign in to UU before retrying',diagnostics={'phase':'login'})
                if phase=='device_list':
                    raise UUBackgroundError('uu_device_list_pending','The private UU account is responding but its device list is not ready. No device connection was attempted; retry after account recovery',diagnostics=diagnostics)
                raise UUBackgroundError('uu_client_not_ready','The private UU client is still starting; no device connection was attempted',diagnostics=diagnostics)
            time.sleep(.25)

    @staticmethod
    def _private(window):
        return isinstance(window,dict) and bool(re.fullmatch(r'Xiaoruo1Background(?:_[A-Za-z0-9_-]{1,48})?',str(window.get('desktop',''))))

    def _validate_window(self,hwnd):
        if type(hwnd) is not int or hwnd<=0:
            raise UUBackgroundError('uu_window_invalid','Choose the owned private UU viewer')
        matches=[w for w in self.windows() if w.get('hwnd')==hwnd and self._private(w) and w.get('visible')]
        if len(matches)!=1 or not self._owned_live(matches[0].get('pid')):
            raise UUBackgroundError('uu_foreign_window','The UU window is outside the owned private process job')
        window=matches[0]
        if self._owned[window['pid']][1]!=str(self.gui).casefold():
            raise UUBackgroundError('uu_foreign_window','Only the owned UU GUI process may receive input')
        if not self._target_name or window.get('title')!=self._target_name:
            raise UUBackgroundError('uu_viewer_mismatch','The private UU window does not match this session device')
        return window

    def _viewer(self):
        if not self._started or not self._connection_requested:
            raise UUBackgroundError('uu_not_connected','Call connect with an exact device_name first')
        matches=[]
        for window in self.windows():
            if window.get('title')==self._target_name and window.get('visible') and self._private(window):
                try:self._validate_window(window['hwnd']);matches.append(window['hwnd'])
                except UUBackgroundError:continue
        if len(matches)!=1:
            raise UUBackgroundError('uu_private_viewer_unavailable','The exact owned private remote-view window is not available')
        return matches[0]

    def _get_transport(self):
        if self._transport is None:
            from .uu_window_transport import WindowMessageTransport
            self._transport=WindowMessageTransport(self._validate_window)
        return self._transport

    def _ensure_keyboard(self):
        if self._keyboard_factory is None:
            raise UUBackgroundError('uu_background_keyboard_unavailable',
                'The remote-user-session keyboard endpoint is not configured; local foreground input will not be used')
        self._viewer()
        if not self._connected:
            raise UUBackgroundError('uu_not_connected','Connect the private UU video window first')
        from .uu_remote_keyboard import RemoteKeyboardError
        recoverable={'uu_remote_helper_stopped','uu_terminal_exited','uu_remote_request_limit'}

        def unresolved_writes(endpoint):
            records=getattr(endpoint,'_requests',None)
            if not isinstance(records,dict):return ['unclassified_request_state']
            unresolved=[]
            no_input_errors={'target_identity_mismatch','target_not_foreground','target_window_stale',
                'interactive_session_required','interactive_desktop_required','remote_computer_mismatch',
                'remote_key_already_down','invalid_text','invalid_keys','secure_attention_unsupported',
                'invalid_request','nonce_mismatch','unsupported_action','request_checksum_mismatch',
                'invalid_request_frame','transport_window_protected'}
            for request_id,record in records.items():
                # Older receipts have no action field. Do not discard them by
                # guessing whether an unclassified operation was only a read.
                if not isinstance(record,dict) or not isinstance(record.get('action'),str):
                    unknown=True
                elif record['action'] in {'status','stop','create_test_window','fixture_state','close_test_window'}:
                    unknown=False
                elif record['action'] not in {'type','key'}:
                    unknown=True
                else:
                    reply=record.get('reply')
                    unknown=not isinstance(reply,dict) or type(reply.get('ok')) is not bool
                    if isinstance(reply,dict) and reply.get('ok') is False:
                        error=reply.get('error')
                        unknown=not isinstance(error,dict) or error.get('code') not in no_input_errors
                    if record.get('unknown_outcome') is True:unknown=True
                if unknown:
                    safe_id=request_id if isinstance(request_id,str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,80}',request_id) else 'unclassified_request'
                    unresolved.append(safe_id)
            return unresolved

        if self._keyboard is not None and self._keyboard_ready and not getattr(self,'_keyboard_recovery_needed',False):
            try:
                # This probe never dispatches input. In particular, a failed
                # status receipt is not itself an unresolved type/key write.
                reply=self._keyboard.request('status',{},timeout=10)
            except RemoteKeyboardError as exc:
                if exc.code not in recoverable:
                    raise UUBackgroundError(exc.code,str(exc),diagnostics={
                        'stage':getattr(exc,'stage',None),'request_id':exc.request_id}) from None
                self._keyboard_recovery_needed=True
                self._keyboard_ready=False
                self._last_remote_target=None
            else:
                if not isinstance(reply,dict) or reply.get('ok') is not True:
                    error=reply.get('error') if isinstance(reply,dict) else None
                    error=error if isinstance(error,dict) else {}
                    raise UUBackgroundError(error.get('code','uu_remote_keyboard_status_invalid'),
                        error.get('message','The remote keyboard status did not confirm a usable endpoint'))
                return

        if self._keyboard is not None and getattr(self,'_keyboard_recovery_needed',False):
            pending=unresolved_writes(self._keyboard)
            if pending:
                raise UUBackgroundError('uu_remote_keyboard_outcome_unknown',
                    'A previous keyboard write has no conclusive receipt; its endpoint and evidence were preserved',
                    may_have_run=True,diagnostics={'pending_request_ids':pending[:10],'pending_request_count':len(pending)})
            try:cleanup=self._keyboard.close()
            except Exception:
                raise UUBackgroundError('uu_remote_keyboard_cleanup_pending',
                    'The previous owned keyboard endpoint could not be confirmed closed; it was not replaced') from None
            self._last_keyboard_cleanup=cleanup
            if not isinstance(cleanup,dict) or cleanup.get('cleanup_complete') is not True:
                raise UUBackgroundError('uu_remote_keyboard_cleanup_pending',
                    'The previous owned keyboard endpoint has not fully closed; it was not replaced',
                    diagnostics={'cleanup':cleanup if isinstance(cleanup,dict) else {'cleanup_complete':False}})
            self._keyboard=None
            self._keyboard_recovery_needed=False
            self._keyboard_ready=False
            self._last_remote_target=None
        if self._keyboard is None:
            self._keyboard=self._keyboard_factory(self.cli,self._target_id)
        if not self._keyboard_ready:
            timeout=min(45,max(.1,self._deadline-time.monotonic())) if self._deadline else 45
            try:
                self._keyboard.start(timeout=timeout)
                self._keyboard_ready=True
                self._keyboard_recovery_needed=False
            except RemoteKeyboardError as exc:
                raise UUBackgroundError(exc.code,str(exc),may_have_run=exc.may_have_run,
                    diagnostics={'stage':getattr(exc,'stage',None)}) from None

    def _keyboard_request(self,action,args,request_id=None):
        from .uu_remote_keyboard import RemoteKeyboardError
        try:
            reply=self._keyboard.request(action,args,request_id=request_id,timeout=10)
        except RemoteKeyboardError as exc:
            raise UUBackgroundError(exc.code,str(exc),may_have_run=exc.may_have_run,
                diagnostics={'stage':getattr(exc,'stage',None),'request_id':exc.request_id}) from None
        if not reply.get('ok'):
            error=reply.get('error') or {}
            raise UUBackgroundError(error.get('code','uu_remote_keyboard_rejected'),
                error.get('message','The remote desktop rejected the input'),
                may_have_run=action in {'type','key'},diagnostics={'request_id':reply.get('reqid')})
        return reply['result']

    def _keyboard_status(self):
        if not self._keyboard_ready:
            return self._result(keyboard_ready=False)
        state=self._keyboard_request('status',{})
        return self._result(keyboard_ready=True,remote_state=state)

    def _keyboard_input(self,action,args):
        if self._keyboard_factory is None:
            raise UUBackgroundError('uu_background_keyboard_unavailable','The remote-user-session keyboard endpoint is not configured')
        main='text' if action=='type' else 'keys'
        if set(args)-{main,'expected_target','operation_id','activate'} or main not in args:
            raise UUBackgroundError('uu_keyboard_arguments_invalid','Use text/keys, expected_target, and an optional operation_id')
        if action=='type' and (not isinstance(args[main],str) or not 1<=len(args[main])<=40000):
            raise UUBackgroundError('uu_keyboard_text_invalid','Text must contain 1-40000 characters')
        if action=='type':
            # Normalize before slicing, so a CRLF pair straddling the 1000-char
            # transport boundary still produces exactly one Enter key.
            text=args[main].replace('\r\n','\n').replace('\r','\n')
        else:text=None
        target=args.get('expected_target') or self._last_remote_target
        if not isinstance(target,dict):
            raise UUBackgroundError('uu_remote_snapshot_required','Take a fresh UU screenshot and use its remote_foreground identity before typing or pressing keys')
        expected={key:target.get(key) for key in ('hwnd','pid','creation_time_utc_ticks')}
        opid=args.get('operation_id') or uuid.uuid4().hex
        if not isinstance(opid,str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,64}',opid):
            raise UUBackgroundError('uu_keyboard_operation_id_invalid','Use a bounded stable operation_id')
        self._ensure_keyboard()
        status=self._cli('device','status')
        if not any(c.get('connectionState')=='connected' for c in self._target_connections(status)):
            raise UUBackgroundError('uu_video_disconnected','The owned remote video connection is no longer active; no input was sent')
        values=[args[main]] if action=='key' else [text[i:i+1000] for i in range(0,len(text),1000)]
        deliveries=[]
        for index,value in enumerate(values):
            payload={main:value,'expect':expected}
            # Match normal remote-control semantics: activate the exact remote
            # app identity, never the controller's desktop. A stable default
            # also keeps same-operation retries byte-for-byte consistent.
            payload['activate']=args.get('activate',True)
            # Validate the entire fixed schema locally before marking an
            # operation as possibly delivered to the remote computer.
            from .uu_remote_keyboard import RemoteKeyboardSession,RemoteKeyboardError
            try:RemoteKeyboardSession._validate(action,payload)
            except RemoteKeyboardError as exc:raise UUBackgroundError(exc.code,str(exc)) from None
            self._action_may_have_run=True
            deliveries.append(self._keyboard_request(action,payload,opid+':'+str(index)))
        self._last_remote_target=None
        return self._result(action=action,operation_id=opid,device_name=self._target_name,
            delivery='remote_user_session_SendInput_over_UU',deliveries=deliveries,
            remote_effect_verified=False,verification_required=True)

    def _stop_keyboard(self):
        if self._keyboard is None:return
        result=self._keyboard.close()
        self._last_keyboard_cleanup=result
        if not result.get('cleanup_complete'):
            raise UUBackgroundError('uu_remote_keyboard_cleanup_pending',
                'The owned remote input endpoint has not fully stopped; video connection is preserved for cleanup',
                diagnostics={'cleanup':result})
        self._keyboard=None
        self._keyboard_ready=False
        self._last_remote_target=None

    def _ui_windows(self):
        if not self._started:
            raise UUBackgroundError('uu_client_not_started','Start a private UU session with connect before inspecting its own controls')
        result=[]
        for window in self.windows():
            pid=window.get('pid')
            if self._private(window) and window.get('visible') and self._owned_live(pid) and self._owned[pid][1]==str(self.gui).casefold():
                result.append(window)
        if not result:
            raise UUBackgroundError('uu_private_ui_unavailable','No owned private UU client window is available')
        return result

    def _ui_action(self,action,args):
        # The actor can retain an already connected session across a local
        # module reload; UIA state is always recreated before a new snapshot.
        if not hasattr(self, '_ui_ids'):
            self._ui_ids = {}
            self._ui = None
        from .background_desktop_host import _UIAutomation,BackgroundDesktopError
        fields={'ui_snapshot':{'hwnd','max_elements'},'ui_click':{'element_id'},'ui_type':{'element_id','text'},
                'ui_toggle':{'element_id'},'ui_expand':{'element_id'},'ui_collapse':{'element_id'}}
        if set(args)-fields[action]:
            raise UUBackgroundError('uu_ui_arguments_invalid','Use snapshot controls and their opaque element_id; coordinates and commands are not accepted')
        windows=self._ui_windows()
        try:
            if action=='ui_snapshot':
                self._ui_ids.clear()
                limit=args.get('max_elements',200)
                hwnd=args.get('hwnd')
                if type(limit) is not int or not 1<=limit<=500:
                    raise UUBackgroundError('uu_ui_arguments_invalid','max_elements must be an integer from 1 to 500')
                if hwnd is not None and (type(hwnd) is not int or hwnd not in {w['hwnd'] for w in windows}):
                    raise UUBackgroundError('uu_foreign_window','Select an HWND from this session owned private UU client windows')
                if self._ui is None:self._ui=_UIAutomation()
                elements=self._ui.snapshot(windows,hwnd,limit)
                generation=uuid.uuid4().hex
                public=[]
                for item in elements:
                    identity=item.get('element_id')
                    if not isinstance(identity,str) or item.get('hwnd') not in {w['hwnd'] for w in windows}:continue
                    opaque=f'uu:{generation}:{len(public)+1}'
                    self._ui_ids[opaque]=identity
                    public.append({**item,'element_id':opaque})
                return self._result(scope='owned_uu_controls',windows=windows,elements=public,
                    note='These are UU client controls; remote desktop canvas input uses the separate window transport')
            opaque=args.get('element_id')
            if not isinstance(opaque,str) or opaque not in self._ui_ids or self._ui is None:
                raise UUBackgroundError('uu_ui_target_stale','Take a fresh ui_snapshot and use one of its opaque element IDs')
            native={'element_id':self._ui_ids[opaque]}
            if action=='ui_type':
                text=args.get('text')
                if not isinstance(text,str) or len(text)>40000:
                    raise UUBackgroundError('uu_ui_arguments_invalid','text must be a string of at most 40000 characters')
                native['text']=text
            self._action_may_have_run=True
            result=self._ui.perform(action.removeprefix('ui_'),native,windows)
            if not isinstance(result,dict) or result.get('ok') is not True:
                raise UUBackgroundError('uu_ui_action_failed','The owned UU control did not accept its native pattern action',may_have_run=True)
            result={**result,'element_id':opaque}
            return self._result(scope='owned_uu_controls',action=action,control=result,
                                verification_required=True,remote_effect_verified=False)
        except BackgroundDesktopError as exc:
            raise UUBackgroundError(exc.code,str(exc),may_have_run=self._action_may_have_run) from None

    def _devices(self):
        if not self._started:
            return self._result(started=False,devices=[],next_action='connect(device_name)',message='Call connect with the exact device name to start a private UU client')
        self._assert_no_existing_ui()
        items=self._device_records(self._cli('device','list'))
        return self._result(started=True,devices=[{'device_name':_label(d['deviceName']),
            'online':d.get('isOnline') is True or d.get('isOnline')==1,'platform':_label(d.get('platform'))} for d in items])

    @staticmethod
    def _device_records(payload):
        found={}
        for obj in _objects(payload):
            if all(key in obj for key in ('deviceId','deviceName','isOnline')) and isinstance(obj['deviceId'],(str,int)):
                found[str(obj['deviceId'])]=obj
        return list(found.values())

    @staticmethod
    def _connections(payload):
        return [item for item in _objects(payload) if 'deviceId' in item and ('connectionState' in item or 'isControlling' in item)]

    @staticmethod
    def _active(connection):
        return connection.get('isControlling') is True or str(connection.get('connectionState','')).casefold() not in {'','disconnected','closed','failed'}

    def _target_connections(self,payload):
        return [c for c in self._connections(payload) if str(c['deviceId'])==self._target_id]

    def _status(self):
        if not self._started:
            return self._result(started=False,connected=False,devices=[],next_action='connect(device_name)')
        self._assert_no_existing_ui()
        payload=self._cli('device','status')
        mine=self._target_connections(payload)
        self._connected=any(c.get('connectionState')=='connected' or c.get('isControlling') is True for c in mine)
        try:hwnd=self._viewer()
        except UUBackgroundError:hwnd=None
        return self._result(started=True,connected=self._connected,device_name=self._target_name,
                            viewer_available=hwnd is not None,hwnd=hwnd)

    def _connect(self,name):
        closed={'disconnected','closed','failed'}
        video_retry_used=False

        def check_deadline():
            if self._deadline is not None and time.monotonic()>=self._deadline:
                raise UUBackgroundError('uu_operation_timeout','UU connect reached its existing operation deadline; no further connection was requested')

        def video_state():
            check_deadline()
            payload=self._cli('device','status',timeout=2)
            self._connected=any(c.get('connectionState')=='connected' or c.get('isControlling') is True
                                for c in self._target_connections(payload))
            try:hwnd=self._viewer()
            except UUBackgroundError:hwnd=None
            return payload,hwnd

        def confirmed_disconnected(payload):
            data=payload.get('data') if isinstance(payload,dict) else None
            rows=data.get('connections') if isinstance(data,dict) else None
            if not isinstance(rows,list):return False
            mine=[row for row in rows if isinstance(row,dict) and str(row.get('deviceId'))==self._target_id]
            if not mine or any(not isinstance(row.get('connectionState'),str)
                or row['connectionState'].casefold() not in closed or row.get('isControlling') is not False for row in mine):
                return False
            if data.get('hasActiveConnections') is not False or any(
                not isinstance(row,dict) or 'deviceId' not in row
                or not isinstance(row.get('connectionState'),str)
                or row['connectionState'].casefold() not in closed
                or row.get('isControlling') is not False for row in rows):
                raise UUBackgroundError('uu_existing_connection',
                    'UU has another active or unconfirmed connection; all existing sessions were preserved')
            return True

        def require_conclusive_writes():
            endpoint=self._keyboard
            if endpoint is None:return
            records=getattr(endpoint,'_requests',None)
            unresolved=[]
            no_input_errors={'target_identity_mismatch','target_not_foreground','target_window_stale',
                'interactive_session_required','interactive_desktop_required','remote_computer_mismatch',
                'remote_key_already_down','invalid_text','invalid_keys','secure_attention_unsupported',
                'invalid_request','nonce_mismatch','unsupported_action','request_checksum_mismatch',
                'invalid_request_frame','transport_window_protected'}
            if not isinstance(records,dict):
                unresolved=['unclassified_request_state']
            else:
                for request_id,record in records.items():
                    action=record.get('action') if isinstance(record,dict) else None
                    if action in {'status','stop','create_test_window','fixture_state','close_test_window'}:continue
                    reply=record.get('reply') if isinstance(record,dict) else None
                    conclusive=action in {'type','key'} and isinstance(reply,dict) and reply.get('ok') is True
                    if action in {'type','key'} and isinstance(reply,dict) and reply.get('ok') is False:
                        error=reply.get('error')
                        conclusive=isinstance(error,dict) and error.get('code') in no_input_errors
                    if not conclusive or record.get('unknown_outcome') is True:
                        safe_id=request_id if isinstance(request_id,str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,80}',request_id) else 'unclassified_request'
                        unresolved.append(safe_id)
            if unresolved:
                raise UUBackgroundError('uu_remote_keyboard_outcome_unknown',
                    'A previous keyboard write has no conclusive receipt; its endpoint and evidence were preserved',
                    may_have_run=True,diagnostics={'pending_request_ids':unresolved[:10],'pending_request_count':len(unresolved)})

        def healthy_keyboard():
            endpoint=self._keyboard
            if endpoint is None or self._keyboard_ready is not True:return False
            check_deadline()
            remaining=min(10,max(.1,self._deadline-time.monotonic())) if self._deadline else 10
            try:reply=endpoint.request('status',{},timeout=remaining)
            except Exception:return False
            return isinstance(reply,dict) and reply.get('ok') is True and isinstance(reply.get('result'),dict)

        def prepare_disconnected_endpoint():
            require_conclusive_writes()
            if healthy_keyboard():
                # A terminal startup can end video while its keyboard helper
                # remains usable. Do not restart that healthy helper again.
                self._keyboard_recovery_needed=False
                return True
            endpoint=self._keyboard
            check_deadline()
            self._stop_keyboard()
            if endpoint is not None and (not isinstance(self._last_keyboard_cleanup,dict)
                    or self._last_keyboard_cleanup.get('cleanup_complete') is not True):
                self._keyboard=endpoint
                self._keyboard_ready=False
                raise UUBackgroundError('uu_remote_keyboard_cleanup_pending',
                    'The previous owned keyboard endpoint did not return a confirmed cleanup result; no reconnect was attempted')
            return False

        def select_device(expected_id=None):
            listing=self._ensure_client()
            devices=[device for device in self._device_records(listing) if device['deviceName']==name]
            if len(devices)!=1:
                raise UUBackgroundError('uu_device_ambiguous','The exact device name must match exactly one device')
            device=devices[0]
            if expected_id is not None and str(device['deviceId'])!=expected_id:
                raise UUBackgroundError('uu_device_identity_changed','The device name now identifies a different device; no reconnect was attempted')
            if device.get('isOnline') is not True and device.get('isOnline')!=1:
                raise UUBackgroundError('uu_device_offline','The selected device is offline')
            return device

        def dispatch_video():
            check_deadline()
            self._assert_no_existing_ui()
            self._last_remote_target=None
            self._connection_requested=True
            self._action_may_have_run=True
            self._cli('device','connect',self._target_id,timeout=10)
            return min(time.monotonic()+15,self._deadline if self._deadline is not None else float('inf'))

        if self._connection_requested:
            if name!=self._target_name:
                raise UUBackgroundError('uu_session_busy','Disconnect the session device before selecting another device')
            self._assert_no_existing_ui()
            payload,hwnd=video_state()
            if not (self._connected and hwnd is not None):
                if not confirmed_disconnected(payload):
                    raise UUBackgroundError('uu_connection_pending',
                        'The requested device is still connecting or its state is unconfirmed; no connection request was repeated')
                kept_keyboard=prepare_disconnected_endpoint()
                select_device(self._target_id)
                check_deadline()
                if not confirmed_disconnected(self._cli('device','status',timeout=2)):
                    raise UUBackgroundError('uu_connection_pending','UU state changed during endpoint preparation; no connection request was repeated')
                deadline=dispatch_video()
                # A preserved helper needs only this one video repair. A new
                # cold helper may need one later video-only repair after start.
                video_retry_used=kept_keyboard or self._keyboard_factory is None
            else:
                deadline=min(time.monotonic()+15,self._deadline if self._deadline is not None else float('inf'))
        else:
            device=select_device()
            status=self._cli('device','status')
            if any(self._active(c) for c in self._connections(status)) or any(o.get('hasActiveConnections') is True for o in _objects(status)):
                raise UUBackgroundError('uu_existing_connection','UU already has an active connection outside this session; it will not be reused or disconnected')
            self._target_id=str(device['deviceId']);self._target_name=name
            deadline=dispatch_video()

        while True:
            payload,hwnd=video_state()
            if self._connected and hwnd is not None:
                if self._keyboard_factory is not None:
                    keyboard_was_ready=self._keyboard_ready
                    self._ensure_keyboard()
                    if not keyboard_was_ready:
                        deadline=min(time.monotonic()+15,self._deadline if self._deadline is not None else float('inf'))
                    # Never return the HWND captured before the potentially
                    # long terminal bootstrap: it can disconnect video.
                    payload,hwnd=video_state()
                    if not (self._connected and hwnd is not None) and confirmed_disconnected(payload):
                        if video_retry_used:
                            raise UUBackgroundError('uu_viewer_not_ready','Video disconnected again after its one confirmed recovery; the keyboard endpoint was preserved',may_have_run=True)
                        require_conclusive_writes()
                        if not healthy_keyboard():
                            raise UUBackgroundError('uu_remote_keyboard_status_invalid','The initialized keyboard endpoint no longer confirmed health; no video repair was dispatched')
                        check_deadline()
                        if not confirmed_disconnected(self._cli('device','status',timeout=2)):
                            raise UUBackgroundError('uu_connection_pending','UU state changed before video-only recovery; no connection request was repeated')
                        deadline=dispatch_video()
                        video_retry_used=True
                        continue
                if self._connected and hwnd is not None:
                    return self._result(started=True,connected=True,device_name=name,hwnd=hwnd,
                        viewer_available=True,remote_effect_verified=False,verification_required=True,
                        client_handoff=self._handoff)
            if time.monotonic()>=deadline:
                windows=[{k:w.get(k) for k in ('hwnd','pid','title','visible','desktop')}
                    for w in self.windows() if w.get('pid') in self._owned][:20]
                states=[{k:c.get(k) for k in ('deviceName','connectionState','isControlling')}
                    for c in self._target_connections(payload)]
                raise UUBackgroundError('uu_viewer_not_ready','The connection request was dispatched but the latest owned private video window is not ready; inspect status',may_have_run=True,
                    diagnostics={'video_connected':self._connected,'owned_windows':windows,'connection_states':states})
            time.sleep(.25)

    def _screenshot(self,hwnd):
        from .background_capture import capture_window
        self._last_remote_target=None
        self._validate_window(hwnd)
        extra={}
        before=None
        recoverable={'uu_remote_helper_stopped','uu_terminal_exited','uu_remote_request_limit'}

        def identity(target):
            if not isinstance(target,dict) or target.get('available') is not True:return None
            if type(target.get('hwnd')) is not int or target['hwnd']<=0 or type(target.get('pid')) is not int or target['pid']<=0:return None
            created=target.get('creation_time_utc_ticks')
            if not isinstance(created,str) or not re.fullmatch(r'[1-9][0-9]{0,18}',created):return None
            return target['hwnd'],target['pid'],created

        if self._keyboard_ready:
            try:before=self._keyboard_request('status',{})
            except UUBackgroundError as exc:
                extra['keyboard_status_error']={'code':exc.code,'message':str(exc)}
                if exc.code in recoverable:
                    self._keyboard_ready=False
                    self._keyboard_recovery_needed=True
        path=self.data_root/'background_desktop'/'captures'/('uu-'+uuid.uuid4().hex+'.png')
        try:
            from .uu_window_transport import dpi_context
            with dpi_context():
                capture=capture_window(hwnd,path)
            self._validate_window(hwnd)
        except uu_window_transport.WindowTransportError:
            raise
        except Exception:
            raise UUBackgroundError('uu_capture_unavailable','The owned UU video window did not produce a usable background image; no foreground capture was used') from None
        if self._keyboard_ready and before is not None:
            try:
                after=self._keyboard_request('status',{})
                first=identity(before.get('foreground')) if isinstance(before,dict) else None
                target=after.get('foreground') if isinstance(after,dict) else None
                last=identity(target)
                if first is not None and first==last:
                    self._last_remote_target=dict(target)
                    extra['remote_foreground']=dict(target)
                elif first!=last:
                    extra['target_changed']=True
                    extra['keyboard_status_error']={'code':'uu_remote_target_changed',
                        'message':'The remote foreground changed during capture; take a fresh screenshot before keyboard input'}
                else:
                    extra['keyboard_status_error']={'code':'uu_remote_foreground_unavailable',
                        'message':'No stable remote foreground identity was available during capture'}
            except UUBackgroundError as exc:
                extra['keyboard_status_error']={'code':exc.code,'message':str(exc)}
                if exc.code in recoverable:
                    self._keyboard_ready=False
                    self._keyboard_recovery_needed=True
        return self._result(device_name=self._target_name,hwnd=hwnd,capture=capture,**extra)

    def _disconnect(self):
        self._stop_keyboard()
        if not self._connection_requested:
            return self._result(disconnected=True,connection_created_by_session=False)
        # Never send a global/all-device disconnect, even when other sessions
        # have appeared since our connection was created.
        self._action_may_have_run=True
        self._cli('device','disconnect',self._target_id,timeout=5)
        deadline=time.monotonic()+7
        while True:
            payload=self._cli('device','status',timeout=2)
            if not any(self._active(c) for c in self._target_connections(payload)):
                name=self._target_name
                self._target_id=None;self._target_name=None
                self._connection_requested=False;self._connected=False
                return self._result(disconnected=True,device_name=name)
            if time.monotonic()>=deadline:
                raise UUBackgroundError('uu_disconnect_pending','The exact device disconnect is not yet confirmed; the client was preserved',may_have_run=True)
            time.sleep(.25)

    def _close(self):
        self._disconnect()
        # UU's GUI and receiver have shared lifetime even when its window is
        # invisible or located on a private desktop. Legacy ownership records
        # must never authorize closing them. The new shared-GUI route owns only
        # its temporary remote endpoint, not this installed application.
        preserved=[pid for pid in list(self._owned) if self._owned_live(pid)]
        self._started=False;self._owned.clear()
        self._ui=None;self._ui_ids.clear()
        return self._result(cleanup_complete=True,closed_pids=[],
                            preserved_receiver_pids=preserved,
                            keyboard_cleanup=self._last_keyboard_cleanup)
