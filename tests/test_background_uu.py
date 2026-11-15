import json
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from web_tools import background_uu as module


@pytest.fixture
def private_uu(tmp_path,monkeypatch):
    root=tmp_path/'UU';(root/'bin').mkdir(parents=True)
    launcher=root/'GameViewer.exe';launcher.touch()
    gui=root/'bin'/'GameViewer.exe';gui.touch()
    (root/'bin'/'uuyc-cli.exe').touch()
    state={'processes':{},'windows':[],'job':set(),'calls':[],'launches':0,'clock':0.0,
           'connections':[],'devices':[{'deviceId':'private-device-id','deviceName':'演示设备','isOnline':True,'platform':'windows'}],
           'terminated':[],'messages':[]}

    class Process:
        def __init__(self,pid):
            if pid not in state['processes']:raise OSError('gone')
            self.pid=pid
        @property
        def info(self):return {'pid':self.pid,'exe':self.exe()}
        def exe(self):return state['processes'][self.pid][0]
        def create_time(self):return state['processes'][self.pid][1]
        def terminate(self):state['terminated'].append(self.pid);state['processes'].pop(self.pid,None)

    monkeypatch.setitem(sys.modules,'psutil',SimpleNamespace(Process=Process,Error=OSError,
        process_iter=lambda fields:[Process(pid) for pid in state['processes']],wait_procs=lambda p,**k:(p,[])))
    monkeypatch.setattr(module.time,'monotonic',lambda:state['clock'])
    monkeypatch.setattr(module.time,'sleep',lambda seconds:state.__setitem__('clock',state['clock']+seconds))

    def launch():
        state['launches']+=1
        state['processes'].update({11:(str(launcher),1.0),22:(str(gui),2.0)})
        state['job']={11,22}
        state['windows']=[{'hwnd':101,'pid':22,'title':'UU startup splash','visible':True,'desktop':'Xiaoruo1Background_test'}]
        return {'ok':True,'launched_pid':11,'windows':list(state['windows']),'process_job_ui_restrictions':True}

    session=module.UUBackgroundSession(tmp_path,launcher,lambda:state['windows'],launch,
        owned_processes=lambda:state['job'])

    def cli(*command,**options):
        state['calls'].append(command)
        if command[:2]==('device','list'):
            return {'success':True,'data':{'nested':{'devices':state['devices']}}}
        if command==('status',):
            return {'success':True,'data':{'isLoggedIn':True}}
        if command[:2]==('device','status'):
            return {'success':True,'data':{'hasActiveConnections':any(c['connectionState']=='connected' for c in state['connections']),
                'connections':state['connections'],'total':len(state['connections'])}}
        if command[:2]==('device','connect'):
            state['connections']=[{'deviceId':command[2],'deviceName':'演示设备','connectionState':'connected','isControlling':True}]
            state['windows'].append({'hwnd':102,'pid':22,'title':'演示设备','visible':True,'desktop':'Xiaoruo1Background_test'})
            return {'success':True}
        if command[:2]==('device','disconnect'):
            for connection in state['connections']:
                if connection['deviceId']==command[2]:connection.update(connectionState='disconnected',isControlling=False)
            return {'success':True}
        raise AssertionError(command)

    session._cli=cli
    return session,state,cli


def test_read_actions_never_launch_or_return_device_ids(private_uu):
    session,state,_=private_uu
    assert session.perform('status')['started'] is False
    assert session.perform('devices')['devices']==[]
    assert state['launches']==0 and state['calls']==[]


def test_connect_waits_for_cli_readiness_after_splash(private_uu):
    session,state,cli=private_uu
    def delayed(*command,**options):
        if state['clock']<3:
            raise module.UUBackgroundError('uu_client_not_ready','not ready')
        return cli(*command,**options)
    session._cli=delayed
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['ok'] and result['viewer_available'] and state['clock']>=3
    assert result['hwnd']==102 and result['remote_effect_verified'] is False
    assert state['launches']==1
    assert 'private-device-id' not in json.dumps(result)
    devices=session.perform('devices')
    assert devices['devices']==[{'device_name':'演示设备','online':True,'platform':'windows'}]
    assert 'private-device-id' not in json.dumps(devices)


def test_account_readiness_uses_top_level_status_not_device_connection_status(private_uu):
    session,state,cli=private_uu
    assert cli('status')['data']=={'isLoggedIn':True}
    connections=cli('device','status')['data']
    assert set(connections)=={'hasActiveConnections','connections','total'}
    state['calls'].clear()
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['ok'] is True
    assert state['calls']==[('status',),('device','list'),('device','status'),
        ('device','connect','private-device-id'),('device','status')]


def test_connect_waits_for_account_restore_and_successful_device_list(private_uu):
    session,state,cli=private_uu
    def recovering(*command,**options):
        if command==('status',):
            result=cli(*command,**options)
            result['data']['isLoggedIn']=state['clock']>=2
            return result
        if command[:2]==('device','list') and state['clock']<4:
            state['calls'].append(command)
            raise module.UUBackgroundError('uu_cli_failed','temporary account recovery',
                diagnostics={'cli_return_code':103,'cli_error_code':'ACCOUNT_RESTORING'})
        return cli(*command,**options)
    session._cli=recovering
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['ok'] and state['clock']>=4 and state['launches']==1
    assert len([c for c in state['calls'] if c[:2]==('device','connect')])==1


@pytest.mark.parametrize('logged_in',[False,None])
def test_missing_login_is_pending_without_list_login_or_connect(private_uu,logged_in):
    session,state,cli=private_uu
    def not_logged_in(*command,**options):
        assert command==('status',)
        result=cli(*command,**options)
        result['data']['isLoggedIn']=logged_in
        return result
    session._cli=not_logged_in
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['error_code']=='uu_login_pending'
    assert result['connection_request_dispatched'] is False
    assert 15<=state['clock']<=15.5
    assert all(c==('status',) for c in state['calls'])
    assert session._target_id is None


def test_device_list_pending_returns_only_stable_diagnostics_and_can_be_retried(private_uu):
    session,state,cli=private_uu
    def list_unavailable(*command,**options):
        if command[:2]==('device','list'):
            raise module.UUBackgroundError('uu_cli_failed','private-device-id raw private details',
                diagnostics={'cli_return_code':103,'cli_error_code':'ACCOUNT_RESTORING'})
        return cli(*command,**options)
    session._cli=list_unavailable
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['error_code']=='uu_device_list_pending'
    assert result['connection_request_dispatched'] is False
    assert result['diagnostics']=={'phase':'device_list','last_error_code':'uu_cli_failed',
        'cli_return_code':103,'cli_error_code':'ACCOUNT_RESTORING'}
    assert 'private-device-id' not in json.dumps(result)
    assert not any(c[:2]==('device','connect') for c in state['calls'])
    session._cli=cli
    assert session.perform('connect',{'device_name':'演示设备'})['ok']
    assert state['launches']==1


def test_existing_user_gui_is_preserved_and_not_reused(private_uu):
    session,state,_=private_uu
    state['processes'][77]=(str(session.gui),77.0)
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['error_code']=='uu_existing_user_client'
    assert state['launches']==0 and state['calls']==[] and state['terminated']==[]


@pytest.mark.parametrize('devices,code',[
    ([{'deviceId':'a','deviceName':'演示设备','isOnline':False}],'uu_device_offline'),
    ([{'deviceId':'a','deviceName':'演示设备','isOnline':True},
      {'deviceId':'b','deviceName':'演示设备','isOnline':True}],'uu_device_ambiguous'),
    ([{'deviceId':'a','deviceName':'演示设备 other','isOnline':True}],'uu_device_ambiguous'),
])
def test_device_selection_requires_unique_exact_online_name(private_uu,devices,code):
    session,state,_=private_uu;state['devices']=devices
    assert session.perform('connect',{'device_name':'演示设备'})['error_code']==code
    assert not any(c[:2]==('device','connect') for c in state['calls'])


def test_foreign_active_connection_is_not_adopted_or_disconnected(private_uu):
    session,state,_=private_uu
    state['connections']=[{'deviceId':'foreign-id','connectionState':'connected'}]
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['error_code']=='uu_existing_connection'
    assert session.perform('disconnect')['connection_created_by_session'] is False
    assert not any(c[:2] in {('device','connect'),('device','disconnect')} for c in state['calls'])


def test_disconnect_only_targets_our_device_and_accepts_retained_disconnected_record(private_uu):
    session,state,_=private_uu
    assert session.perform('connect',{'device_name':'演示设备'})['ok']
    state['connections'].append({'deviceId':'foreign-id','connectionState':'connected','isControlling':True})
    result=session.perform('disconnect')
    assert result['ok'] and result['disconnected']
    assert len(state['connections'])==2
    assert state['connections'][1]['connectionState']=='connected'
    assert [c for c in state['calls'] if c[:2]==('device','disconnect')]==[('device','disconnect','private-device-id')]
    assert 'private-device-id' not in json.dumps(result)


@pytest.mark.parametrize('change', ['desktop','pid','creation_time','job','title'])
def test_foreign_or_reused_viewer_never_receives_input(private_uu,change):
    session,state,_=private_uu
    assert session.perform('connect',{'device_name':'演示设备'})['ok']
    session._transport=SimpleNamespace(perform=lambda *a:pytest.fail('must not send input'))
    viewer=state['windows'][-1]
    if change=='desktop':viewer['desktop']='Default'
    if change=='pid':viewer['pid']=77
    if change=='creation_time':state['processes'][22]=(str(session.gui),999.0)
    if change=='job':state['job'].remove(22)
    if change=='title':viewer['title']='other device'
    result=session.perform('click',{'x':100,'y':100})
    assert result['ok'] is False and result['remote_effect_verified'] is False


def test_input_is_window_scoped_and_never_claims_remote_verification(private_uu):
    session,state,_=private_uu
    session.perform('connect',{'device_name':'演示设备'})
    calls=[]
    session._transport=SimpleNamespace(perform=lambda *args:calls.append(args) or {'ok':True,'status':'dispatched','remote_effect_verified':False})
    result=session.perform('click',{'x':100,'y':100})
    assert calls==[(102,'click',{'x':100,'y':100})]
    assert result['ok'] and result['remote_effect_verified'] is False and result['verification_required'] is True


@pytest.mark.parametrize('action,args', [('type', {'text':'hello'}), ('key', {'keys':'N'})])
def test_keyboard_that_failed_real_uu_acceptance_is_not_exposed_as_success(private_uu,action,args):
    session,_,_=private_uu
    session._transport=SimpleNamespace(perform=lambda *a:pytest.fail('known ineffective keyboard must not be dispatched'))
    result=session.perform(action,args)
    assert result['ok'] is False
    assert result['error_code']=='uu_background_keyboard_unavailable'
    assert result['action_may_have_run'] is False


@pytest.fixture
def uu_controls(private_uu,monkeypatch):
    session,state,_=private_uu
    session.perform('connect',{'device_name':'演示设备'})
    calls=[]
    class UIAutomation:
        def snapshot(self,windows,hwnd,limit):
            calls.append(('snapshot',list(windows),hwnd,limit))
            return [{'element_id':f'1:{index+1}','hwnd':window['hwnd'],'name':'UU toolbar','patterns':['invoke','value']}
                    for index,window in enumerate(windows) if hwnd is None or hwnd==window['hwnd']][:limit]
        def perform(self,action,args,windows):
            calls.append((action,args,list(windows)))
            return {'ok':True,'action':action,'element_id':args['element_id'],'verification_required':True}
    monkeypatch.setattr('web_tools.background_desktop_host._UIAutomation',UIAutomation)
    return session,state,calls


def test_uu_ui_snapshot_excludes_foreign_default_and_launcher_windows(uu_controls):
    session,state,calls=uu_controls
    state['processes'][77]=(str(session.gui),77.0)
    state['windows'].extend([
        {'hwnd':300,'pid':22,'title':'User UU','visible':True,'desktop':'Default'},
        {'hwnd':301,'pid':77,'title':'Foreign UU','visible':True,'desktop':'Xiaoruo1Background_test'},
        {'hwnd':302,'pid':11,'title':'Launcher','visible':True,'desktop':'Xiaoruo1Background_test'},
    ])
    result=session.perform('ui_snapshot',{'max_elements':10})
    assert result['ok'] and result['scope']=='owned_uu_controls'
    assert {window['hwnd'] for window in result['windows']}=={101,102}
    assert calls[0][3]==10
    assert all(element['element_id'].startswith('uu:') for element in result['elements'])
    assert all(element['element_id'] not in {'1:1','1:2'} for element in result['elements'])


def test_uu_ui_write_accepts_only_current_snapshot_opaque_ids(uu_controls):
    session,state,calls=uu_controls
    first=session.perform('ui_snapshot',{'hwnd':101})
    old=first['elements'][0]['element_id']
    second=session.perform('ui_snapshot',{'hwnd':101})
    current=second['elements'][0]['element_id']
    assert current!=old
    assert session.perform('ui_click',{'element_id':old})['error_code']=='uu_ui_target_stale'
    assert session.perform('ui_click',{'element_id':'1:1'})['error_code']=='uu_ui_target_stale'
    result=session.perform('ui_type',{'element_id':current,'text':'remote keyboard text'})
    assert result['ok'] and result['control']['element_id']==current
    assert result['remote_effect_verified'] is False
    assert calls[-1][0]=='type' and calls[-1][1]=={'element_id':'1:1','text':'remote keyboard text'}


def test_uu_ui_snapshot_refuses_explicit_foreign_hwnd(uu_controls):
    session,state,calls=uu_controls
    state['windows'].append({'hwnd':300,'pid':22,'title':'User UU','visible':True,'desktop':'Default'})
    result=session.perform('ui_snapshot',{'hwnd':300})
    assert result['error_code']=='uu_foreign_window' and calls==[]


@pytest.mark.parametrize('limit',[0,501,True,'200'])
def test_uu_ui_snapshot_limit_is_bounded_before_uia(uu_controls,limit):
    session,state,calls=uu_controls
    result=session.perform('ui_snapshot',{'max_elements':limit})
    assert result['error_code']=='uu_ui_arguments_invalid' and calls==[]


def test_uu_ui_revalidates_process_identity_before_write(uu_controls):
    session,state,calls=uu_controls
    identity=session.perform('ui_snapshot')['elements'][0]['element_id']
    state['processes'][22]=(str(session.gui),999.0)
    result=session.perform('ui_click',{'element_id':identity})
    assert result['ok'] is False and result['action_may_have_run'] is False
    assert len(calls)==1


def test_uu_ui_click_uses_native_invoke_without_canvas_transport(uu_controls):
    session,state,calls=uu_controls
    identity=session.perform('ui_snapshot')['elements'][0]['element_id']
    session._transport=SimpleNamespace(perform=lambda *a:pytest.fail('UIA control must not use canvas transport'))
    result=session.perform('ui_click',{'element_id':identity})
    assert result['ok'] and calls[-1][0]=='click'
    assert calls[-1][1]['element_id']=='1:1'


@pytest.mark.parametrize('action,native_action',[
    ('ui_toggle','toggle'),('ui_expand','expand'),('ui_collapse','collapse'),
])
def test_uu_ui_routes_toggle_and_menu_actions_to_the_matching_native_pattern(uu_controls,action,native_action):
    session,state,calls=uu_controls
    identity=session.perform('ui_snapshot')['elements'][0]['element_id']
    session._transport=SimpleNamespace(perform=lambda *a:pytest.fail('UU controls must not use canvas input'))
    result=session.perform(action,{'element_id':identity})
    assert result['ok'] and result['action']==action
    assert calls[-1][0]==native_action and calls[-1][1]=={'element_id':'1:1'}
    assert result['control']['element_id']==identity


@pytest.mark.parametrize('action',['ui_toggle','ui_expand','ui_collapse'])
def test_uu_ui_pattern_actions_reject_stale_ids(uu_controls,action):
    session,state,calls=uu_controls
    old=session.perform('ui_snapshot')['elements'][0]['element_id']
    session.perform('ui_snapshot')
    result=session.perform(action,{'element_id':old})
    assert result['error_code']=='uu_ui_target_stale' and result['action_may_have_run'] is False
    assert all(call[0]=='snapshot' for call in calls)


def test_transport_error_keeps_safe_code_message_and_input_phase(private_uu):
    session,state,_=private_uu
    session.perform('connect',{'device_name':'演示设备'})
    def fail(*args):
        raise module.uu_window_transport.WindowTransportError('private_focus_attach_failed',
            'Cannot attach the two private GUI input queues')
    session._transport=SimpleNamespace(perform=fail)
    result=session.perform('click',{'x':10,'y':10,'focus_mode':'native_private'})
    assert result['error_code']=='private_focus_attach_failed'
    assert result['message']=='Cannot attach the two private GUI input queues'
    assert result['phase']=='window_input' and result['action_may_have_run'] is True
    assert result['remote_effect_verified'] is False and result['host_input_used'] is False


def test_transport_inspection_error_is_not_marked_as_input(private_uu):
    session,state,_=private_uu
    session.perform('connect',{'device_name':'演示设备'})
    def fail(*args):raise module.uu_window_transport.WindowTransportError('private_window_stale','Window process is unavailable')
    session._transport=SimpleNamespace(inspect=fail)
    result=session.perform('inspect')
    assert result['error_code']=='private_window_stale' and result['phase']=='window_inspect'
    assert result['action_may_have_run'] is False


def test_unknown_transport_exception_does_not_expose_raw_details(private_uu):
    session,state,_=private_uu
    session.perform('connect',{'device_name':'演示设备'})
    def fail(*args):raise RuntimeError('private-device-id unknown raw details')
    session._transport=SimpleNamespace(perform=fail)
    result=session.perform('click',{'x':10,'y':10})
    assert result['error_code']=='uu_background_failed' and result['phase']=='window_input'
    assert result['action_may_have_run'] is True
    assert 'private-device-id' not in json.dumps(result)


def test_background_capture_failure_does_not_fall_back(private_uu,monkeypatch):
    session,state,_=private_uu
    session.perform('connect',{'device_name':'演示设备'})
    monkeypatch.setitem(sys.modules, "web_tools.uu_window_transport", SimpleNamespace(dpi_context=lambda: __import__("contextlib").nullcontext()))
    monkeypatch.setattr('web_tools.background_capture.capture_window',lambda *a:(_ for _ in ()).throw(RuntimeError('black image')))
    result=session.perform('screenshot')
    assert result['error_code']=='uu_capture_unavailable'
    assert result['host_input_used'] is False


def test_background_capture_uses_temporary_dpi_context_and_top_capture(private_uu,monkeypatch):
    session,state,_=private_uu
    session.perform('connect',{'device_name':'演示设备'})
    events=[]
    @contextmanager
    def dpi_context():
        events.append('enter')
        try:yield
        finally:events.append('exit')
    def capture(hwnd,path):
        assert events==['enter'] and hwnd==102
        assert path.parent==session.data_root/'background_desktop'/'captures'
        events.append('capture')
        return {'path':str(path),'mime_type':'image/png','width':1920,'height':1080}
    monkeypatch.setitem(sys.modules,'web_tools.uu_window_transport',SimpleNamespace(dpi_context=dpi_context))
    monkeypatch.setattr('web_tools.background_capture.capture_window',capture)
    result=session.perform('screenshot')
    assert result['ok'] and result['capture']['width']==1920
    assert events==['enter','capture','exit']


def test_dpi_transport_error_retains_capture_phase_without_claiming_input(private_uu,monkeypatch):
    session,state,_=private_uu
    session.perform('connect',{'device_name':'演示设备'})
    @contextmanager
    def unavailable_dpi():
        raise module.uu_window_transport.WindowTransportError('window_dpi_unavailable','Per-thread DPI control is unavailable')
        yield
    monkeypatch.setitem(sys.modules,'web_tools.uu_window_transport',SimpleNamespace(dpi_context=unavailable_dpi))
    result=session.perform('screenshot')
    assert result['error_code']=='window_dpi_unavailable' and result['phase']=='window_capture'
    assert result['action_may_have_run'] is False


def test_dispatched_connect_with_no_viewer_is_not_automatically_replayed(private_uu):
    session,state,cli=private_uu
    def no_viewer(*command,**options):
        result=cli(*command,**options)
        state['windows']=[w for w in state['windows'] if w['title']!='演示设备']
        return result
    session._cli=no_viewer
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['error_code']=='uu_viewer_not_ready' and result['action_may_have_run'] is True
    assert state['clock']<=15.5
    assert session.perform('connect',{'device_name':'演示设备'})['error_code']=='uu_connection_pending'
    assert len([c for c in state['calls'] if c[:2]==('device','connect')])==1


def test_close_only_closes_owned_ui_and_preserves_service(private_uu,monkeypatch):
    session,state,_=private_uu
    session.perform('connect',{'device_name':'演示设备'})
    state['processes'][99]=(str(session.executable.parent/'bin'/'GameViewerServer.exe'),99.0)
    monkeypatch.setitem(sys.modules,'win32gui',SimpleNamespace(PostMessage=lambda hwnd,*a:state['messages'].append(hwnd)))
    monkeypatch.setitem(sys.modules,'win32con',SimpleNamespace(WM_CLOSE=16))
    monkeypatch.setattr('web_tools.background_lifecycle._default_window_pids',lambda:set())
    result=session.perform('close')
    assert result['cleanup_complete'] is True
    assert state['terminated']==[]
    assert 99 in state['processes'] and state['messages']==[]
    assert set(result['preserved_receiver_pids'])=={11,22}
    assert [c for c in state['calls'] if c[:2]==('device','disconnect')]==[('device','disconnect','private-device-id')]


def test_close_failure_preserves_ui_and_reports_incomplete_cleanup(private_uu,monkeypatch):
    session,state,_=private_uu
    session.perform('connect',{'device_name':'演示设备'})
    monkeypatch.setitem(sys.modules,'win32gui',SimpleNamespace(PostMessage=lambda *a:pytest.fail('shared user window must be preserved')))
    monkeypatch.setitem(sys.modules,'win32con',SimpleNamespace(WM_CLOSE=16))
    monkeypatch.setattr('web_tools.background_lifecycle._default_window_pids',lambda:{22})
    result=session.perform('close')
    assert result['ok'] and result['cleanup_complete'] is True
    assert state['terminated']==[]


def test_close_preserves_client_with_new_foreign_connection(private_uu,monkeypatch):
    session,state,_=private_uu
    session.perform('connect',{'device_name':'演示设备'})
    state['connections'].append({'deviceId':'foreign-id','connectionState':'connected','isControlling':True})
    monkeypatch.setitem(sys.modules,'win32gui',SimpleNamespace(PostMessage=lambda *a:pytest.fail('foreign connection client must be preserved')))
    monkeypatch.setitem(sys.modules,'win32con',SimpleNamespace(WM_CLOSE=16))
    monkeypatch.setattr('web_tools.background_lifecycle._default_window_pids',lambda:set())
    result=session.perform('close')
    assert result['ok'] and result['cleanup_complete'] is True
    assert state['terminated']==[] and state['connections'][-1]['connectionState']=='connected'


def test_close_rechecks_user_windows_before_terminating_any_owned_process(private_uu,monkeypatch):
    session,state,_=private_uu
    session.perform('connect',{'device_name':'演示设备'})
    observations=iter([set(),{22}])
    monkeypatch.setitem(sys.modules,'win32gui',SimpleNamespace(PostMessage=lambda *a:None))
    monkeypatch.setitem(sys.modules,'win32con',SimpleNamespace(WM_CLOSE=16))
    monkeypatch.setattr('web_tools.background_lifecycle._default_window_pids',lambda:next(observations))
    result=session.perform('close')
    assert result['ok'] and result['cleanup_complete'] is True
    assert state['terminated']==[]


def test_cli_reads_json_from_stderr_and_bounds_timeout(tmp_path,monkeypatch):
    root=tmp_path/'bin';root.mkdir();cli=root/'uuyc-cli.exe';cli.touch()
    session=module.UUBackgroundSession(tmp_path,tmp_path/'GameViewer.exe',lambda:[],lambda:None)
    calls=[]
    monkeypatch.setattr(module.subprocess,'run',lambda argv,**kwargs:calls.append((argv,kwargs)) or SimpleNamespace(
        stdout='',stderr=json.dumps({'success':True,'data':{}}),returncode=0))
    assert session._cli('device','status',timeout=50)['success']
    assert calls[0][0]==[str(cli),'device','status','--json']
    assert calls[0][1]['timeout']==25 and 'shell' not in calls[0][1]


def test_cli_uses_injected_runner_with_the_fixed_command_contract(tmp_path,monkeypatch):
    (tmp_path/'bin').mkdir();(tmp_path/'bin'/'uuyc-cli.exe').touch()
    calls=[]
    def runner(argv,**options):
        calls.append((argv,options))
        return SimpleNamespace(stdout=json.dumps({'success':True}),stderr='',returncode=0)
    session=module.UUBackgroundSession(tmp_path,tmp_path/'GameViewer.exe',lambda:[],lambda:None,cli_runner=runner)
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:pytest.fail('must use injected runner'))
    assert session._cli('device','status')['success']
    assert calls[0][0]==[str(tmp_path/'bin'/'uuyc-cli.exe'),'device','status','--json']
    assert calls[0][1]['cwd']==str(tmp_path/'bin') and calls[0][1]['timeout']<=25


def test_cli_timeout_has_unknown_outcome_without_leaking_device_id(tmp_path,monkeypatch):
    (tmp_path/'bin').mkdir();(tmp_path/'bin'/'uuyc-cli.exe').touch()
    session=module.UUBackgroundSession(tmp_path,tmp_path/'GameViewer.exe',lambda:[],lambda:None)
    def timed_out(*args,**kwargs):raise subprocess.TimeoutExpired(args[0],10)
    monkeypatch.setattr(module.subprocess,'run',timed_out)
    with pytest.raises(module.UUBackgroundError) as failure:session._cli('device','connect','private-device-id')
    assert failure.value.may_have_run and 'private-device-id' not in str(failure.value)


def test_cli_recognizes_not_ready_json_in_stderr(tmp_path,monkeypatch):
    (tmp_path/'bin').mkdir();(tmp_path/'bin'/'uuyc-cli.exe').touch()
    session=module.UUBackgroundSession(tmp_path,tmp_path/'GameViewer.exe',lambda:[],lambda:None)
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='',
        stderr=json.dumps({'success':False,'message':'Main application is not running'}),returncode=102))
    with pytest.raises(module.UUBackgroundError) as failure:session._cli('device','status')
    assert failure.value.code=='uu_client_not_ready'


def test_cli_failure_keeps_safe_error_code_without_raw_fields(tmp_path,monkeypatch):
    (tmp_path/'bin').mkdir();(tmp_path/'bin'/'uuyc-cli.exe').touch()
    session=module.UUBackgroundSession(tmp_path,tmp_path/'GameViewer.exe',lambda:[],lambda:None)
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='',
        stderr=json.dumps({'success':False,'errorCode':'ACCOUNT_RESTORING','deviceId':'private-device-id',
                           'message':'private-device-id account not ready'}),returncode=103))
    with pytest.raises(module.UUBackgroundError) as failure:session._cli('device','list')
    assert failure.value.diagnostics=={'cli_return_code':103,'cli_error_code':'ACCOUNT_RESTORING'}
    assert 'private-device-id' not in str(failure.value)


def test_cli_timeout_is_capped_by_total_connection_deadline(tmp_path,monkeypatch):
    (tmp_path/'bin').mkdir();(tmp_path/'bin'/'uuyc-cli.exe').touch()
    session=module.UUBackgroundSession(tmp_path,tmp_path/'GameViewer.exe',lambda:[],lambda:None)
    monkeypatch.setattr(module.time,'monotonic',lambda:48)
    session._deadline=50
    calls=[]
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:calls.append(k) or SimpleNamespace(
        stdout=json.dumps({'success':True}),stderr='',returncode=0))
    session._cli('device','status',timeout=25)
    assert calls[0]['timeout']==2
