from copy import deepcopy
from types import SimpleNamespace

import pytest

from web_tools import background_uu as module
from test_background_uu import private_uu


class Keyboard:
    def __init__(self, requests=None, *, complete=True, on_close=None):
        self._requests={} if requests is None else requests
        self._attempted=True
        self._ready=None
        self.complete=complete
        self.on_close=on_close
        self.close_calls=0

    def close(self):
        self.close_calls+=1
        if self.on_close:self.on_close()
        return {'cleanup_complete':self.complete}


class HealthyKeyboard(Keyboard):
    def __init__(self,requests=None,*,on_start=None,on_status=None):
        super().__init__(requests)
        self.start_calls=0
        self.status_calls=0
        self.on_start=on_start
        self.on_status=on_status

    def start(self,timeout=45):
        self.start_calls+=1
        if self.on_start:self.on_start()
        self._ready={'ready':True}
        return self._ready

    def request(self,action,args,**options):
        assert action=='status' and args=={}
        self.status_calls+=1
        if self.on_status:self.on_status()
        return {'ok':True,'result':{'foreground':{'available':False}}}


def disconnected(private_uu, state='disconnected'):
    session,observed,cli=private_uu
    assert session.perform('connect',{'device_name':'演示设备'})['ok']
    observed['connections'][0].update(connectionState=state,isControlling=False)
    observed['windows']=[window for window in observed['windows'] if window['title']!='演示设备']
    observed['calls'].clear()
    return session,observed,cli


@pytest.mark.parametrize('closed_state',['disconnected','closed','failed'])
def test_repeated_connect_replaces_failed_read_only_bootstrap_and_reconnects_once(private_uu,closed_state):
    session,state,_=disconnected(private_uu,closed_state)
    keyboard=Keyboard({'probe':{'action':'status','reply':None}})
    session._keyboard=keyboard
    session._last_remote_target={'hwnd':999}
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['ok'] and result['connected'] and result['viewer_available']
    assert keyboard.close_calls==1
    assert session._keyboard is None and session._last_remote_target is None
    assert [call for call in state['calls'] if call[:2]==('device','connect')]==[('device','connect','private-device-id')]
    assert not any(call[:2]==('device','disconnect') for call in state['calls'])
    assert state['launches']==1


def test_cold_bootstrap_failure_that_disconnected_video_can_be_recovered(private_uu):
    session,state,cli=private_uu
    endpoint=Keyboard()
    session._keyboard_factory=lambda *args:endpoint
    def bootstrap_failed():
        session._keyboard=endpoint
        state['connections'][0].update(connectionState='disconnected',isControlling=False)
        state['windows']=[window for window in state['windows'] if window['title']!='演示设备']
        raise module.UUBackgroundError('uu_remote_input_timeout','Cold terminal startup timed out')
    session._ensure_keyboard=bootstrap_failed
    first=session.perform('connect',{'device_name':'演示设备'})
    assert first['error_code']=='uu_remote_input_timeout'
    session._ensure_keyboard=lambda:None
    second=session.perform('connect',{'device_name':'演示设备'})
    assert second['ok'] and endpoint.close_calls==1
    assert len([call for call in state['calls'] if call[:2]==('device','connect')])==2


@pytest.mark.parametrize('pending_state',['connecting','reconnecting','connected','unknown',''])
def test_pending_or_unknown_target_state_is_not_replayed(private_uu,pending_state):
    session,state,_=disconnected(private_uu,pending_state)
    endpoint=Keyboard()
    session._keyboard=endpoint
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['error_code']=='uu_connection_pending'
    assert endpoint.close_calls==0
    assert not any(call[:2]==('device','connect') for call in state['calls'])
    assert session._connection_requested and session._target_id=='private-device-id'


def test_absent_record_does_not_count_as_explicit_disconnect(private_uu):
    session,state,_=disconnected(private_uu)
    state['connections']=[]
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['error_code']=='uu_connection_pending'
    assert not any(call[:2]==('device','connect') for call in state['calls'])


@pytest.mark.parametrize('requests',[
    {'type-op':{'action':'type','reply':None}},
    {'key-op':{'action':'key','reply':{'ok':False,'error':{'code':'send_input_partial'}}}},
    {'old-op':{'reply':None}},
    {'op':{'action':'type','reply':{'ok':True},'unknown_outcome':True}},
    {'op':{'action':'unknown','reply':{'ok':True}}},
    {'op':None},
])
def test_unknown_write_preserves_endpoint_and_does_not_reconnect(private_uu,requests):
    session,state,_=disconnected(private_uu)
    endpoint=Keyboard(requests)
    session._keyboard=endpoint
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['error_code']=='uu_remote_keyboard_outcome_unknown'
    assert result['action_may_have_run'] is True
    assert endpoint.close_calls==0 and session._keyboard is endpoint
    assert not any(call[:2]==('device','connect') for call in state['calls'])


@pytest.mark.parametrize('reply',[
    {'ok':True,'result':{'inserted_events':2}},
    {'ok':False,'error':{'code':'target_identity_mismatch'}},
])
def test_conclusive_keyboard_receipts_do_not_block_safe_reconnect(private_uu,reply):
    session,state,_=disconnected(private_uu)
    endpoint=Keyboard({'done':{'action':'type','reply':reply}})
    session._keyboard=endpoint
    assert session.perform('connect',{'device_name':'演示设备'})['ok']
    assert endpoint.close_calls==1


def test_missing_receipt_table_is_unknown_and_preserved(private_uu):
    session,state,_=disconnected(private_uu)
    session._keyboard=SimpleNamespace(close=lambda:pytest.fail('unknown endpoint must be preserved'))
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['error_code']=='uu_remote_keyboard_outcome_unknown'
    assert not any(call[:2]==('device','connect') for call in state['calls'])


def test_cleanup_must_be_confirmed_before_reset_or_reconnect(private_uu):
    session,state,_=disconnected(private_uu)
    endpoint=Keyboard(complete=False)
    session._keyboard=endpoint
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['error_code']=='uu_remote_keyboard_cleanup_pending'
    assert endpoint.close_calls==1 and session._keyboard is endpoint
    assert session._connection_requested and session._target_id=='private-device-id'
    assert not any(call[:2]==('device','connect') for call in state['calls'])


@pytest.mark.parametrize('complete',[1,'true',None])
def test_truthy_or_missing_cleanup_flag_is_not_confirmation(private_uu,complete):
    session,state,_=disconnected(private_uu)
    endpoint=Keyboard(complete=complete)
    session._keyboard=endpoint
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['error_code']=='uu_remote_keyboard_cleanup_pending'
    assert session._keyboard is endpoint and session._connection_requested
    assert not any(call[:2]==('device','connect') for call in state['calls'])


def test_cleanup_exception_preserves_connection_and_endpoint(private_uu):
    session,state,_=disconnected(private_uu)
    def failed_close():raise RuntimeError('test cleanup failure')
    endpoint=Keyboard(on_close=failed_close)
    session._keyboard=endpoint
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['ok'] is False
    assert session._keyboard is endpoint and session._connection_requested
    assert not any(call[:2]==('device','connect') for call in state['calls'])


def test_other_active_device_is_preserved_before_cleanup(private_uu):
    session,state,_=disconnected(private_uu)
    other={'deviceId':'other-device','deviceName':'other','connectionState':'connected','isControlling':True}
    state['connections'].append(other)
    before=deepcopy(state['connections'])
    endpoint=Keyboard()
    session._keyboard=endpoint
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['error_code']=='uu_existing_connection'
    assert endpoint.close_calls==0 and state['connections']==before
    assert not any(call[:2] in {('device','connect'),('device','disconnect')} for call in state['calls'])


def test_other_connection_appearing_during_cleanup_prevents_reconnect(private_uu):
    session,state,_=disconnected(private_uu)
    other={'deviceId':'other-device','connectionState':'connected','isControlling':True}
    endpoint=Keyboard(on_close=lambda:state['connections'].append(other))
    session._keyboard=endpoint
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['error_code']=='uu_existing_connection'
    assert endpoint.close_calls==1
    assert session._connection_requested and session._target_id=='private-device-id'
    assert other in state['connections']
    assert not any(call[:2]==('device','connect') for call in state['calls'])


def test_target_becoming_pending_during_cleanup_is_not_replayed(private_uu):
    session,state,_=disconnected(private_uu)
    endpoint=Keyboard(on_close=lambda:state['connections'][0].update(connectionState='connecting'))
    session._keyboard=endpoint
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['error_code']=='uu_connection_pending'
    assert session._connection_requested
    assert not any(call[:2]==('device','connect') for call in state['calls'])


def test_reconnect_uses_original_deadline_after_cleanup(private_uu):
    session,state,_=disconnected(private_uu)
    original=state['clock']+1
    session._deadline=original
    session._keyboard=Keyboard(on_close=lambda:state.__setitem__('clock',state['clock']+2))
    with pytest.raises(module.UUBackgroundError) as error:
        session._connect('演示设备')
    assert error.value.code=='uu_operation_timeout'
    assert session._deadline==original
    assert session._connection_requested
    assert not any(call[:2]==('device','connect') for call in state['calls'])


def test_same_name_with_changed_device_identity_is_not_silently_selected(private_uu):
    session,state,_=disconnected(private_uu)
    state['devices'][0]['deviceId']='new-device-id'
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['error_code']=='uu_device_identity_changed'
    assert not any(call[:2]==('device','connect') for call in state['calls'])


def test_new_failure_after_one_reconnect_is_returned_without_recursive_replay(private_uu):
    session,state,cli=disconnected(private_uu)
    def failed_again(*command,**options):
        result=cli(*command,**options)
        if command[:2]==('device','connect'):
            state['connections'][0].update(connectionState='disconnected',isControlling=False)
            state['windows']=[window for window in state['windows'] if window['title']!='演示设备']
        return result
    session._cli=failed_again
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['error_code']=='uu_viewer_not_ready'
    assert len([call for call in state['calls'] if call[:2]==('device','connect')])==1


def unique_video_windows(session,state,cli):
    state['video_connects']=0
    def wrapped(*command,**options):
        result=cli(*command,**options)
        if command[:2]==('device','connect'):
            state['video_connects']+=1
            for window in state['windows']:
                if window['title']=='演示设备':window['hwnd']=1100+state['video_connects']
        return result
    session._cli=wrapped


def drop_video(state,connection_state='disconnected'):
    state['connections'][0].update(connectionState=connection_state,isControlling=False)
    state['windows']=[window for window in state['windows'] if window['title']!='演示设备']


def test_keyboard_start_that_drops_video_is_repaired_without_restarting_keyboard(private_uu):
    session,state,cli=private_uu
    def cold_start():
        state['clock']+=33
        drop_video(state)
    keyboard=HealthyKeyboard(on_start=cold_start)
    session._keyboard_factory=lambda *args:keyboard
    unique_video_windows(session,state,cli)
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['ok'] and result['connected'] and result['viewer_available']
    assert result['hwnd']==1102
    assert session._keyboard is keyboard and session._keyboard_ready is True
    assert keyboard.start_calls==1 and keyboard.close_calls==0
    assert keyboard.status_calls>=2
    assert state['video_connects']==2 and state['clock']==33


def test_returned_viewer_is_selected_after_keyboard_initialization(private_uu):
    session,state,_=private_uu
    def replace_window():
        for window in state['windows']:
            if window['title']=='演示设备':window['hwnd']=777
    keyboard=HealthyKeyboard(on_start=replace_window)
    session._keyboard_factory=lambda *args:keyboard
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['ok'] and result['hwnd']==777
    assert keyboard.start_calls==1 and keyboard.close_calls==0
    assert len([call for call in state['calls'] if call[:2]==('device','connect')])==1


def test_disconnected_video_preserves_a_read_only_confirmed_healthy_keyboard(private_uu):
    session,state,cli=disconnected(private_uu)
    keyboard=HealthyKeyboard()
    keyboard._ready={'ready':True}
    session._keyboard=keyboard
    session._keyboard_ready=True
    session._keyboard_factory=lambda *args:pytest.fail('healthy keyboard must not be replaced')
    unique_video_windows(session,state,cli)
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['ok'] and result['hwnd']==1101
    assert session._keyboard is keyboard and session._keyboard_ready
    assert keyboard.status_calls>=2 and keyboard.start_calls==0 and keyboard.close_calls==0
    assert state['video_connects']==1


def test_existing_connected_video_is_revalidated_after_first_keyboard_start(private_uu):
    session,state,cli=private_uu
    assert session.perform('connect',{'device_name':'演示设备'})['ok']
    state['calls'].clear()
    keyboard=HealthyKeyboard(on_start=lambda:drop_video(state))
    session._keyboard_factory=lambda *args:keyboard
    unique_video_windows(session,state,cli)
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['ok'] and result['hwnd']==1101
    assert keyboard.start_calls==1 and keyboard.close_calls==0
    assert state['video_connects']==1


def test_failed_keyboard_health_probe_is_cleaned_before_reconnecting(private_uu):
    session,state,_=disconnected(private_uu)
    def failed_status():raise RuntimeError('read-only status failed')
    old=HealthyKeyboard(on_status=failed_status)
    session._keyboard=old
    session._keyboard_ready=True
    new=HealthyKeyboard()
    session._keyboard_factory=lambda *args:new
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['ok']
    assert old.status_calls==1 and old.close_calls==1
    assert session._keyboard is new and new.start_calls==1


def test_healthy_keyboard_with_unknown_write_is_not_probed_or_replaced(private_uu):
    session,state,_=disconnected(private_uu)
    keyboard=HealthyKeyboard({'unknown-type':{'action':'type','reply':None}})
    session._keyboard=keyboard
    session._keyboard_ready=True
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['error_code']=='uu_remote_keyboard_outcome_unknown'
    assert keyboard.status_calls==0 and keyboard.close_calls==0
    assert not any(call[:2]==('device','connect') for call in state['calls'])


def test_other_active_connection_after_keyboard_start_blocks_video_repair(private_uu):
    session,state,_=private_uu
    other={'deviceId':'other-device','connectionState':'connected','isControlling':True}
    def start():
        drop_video(state)
        state['connections'].append(other)
    keyboard=HealthyKeyboard(on_start=start)
    session._keyboard_factory=lambda *args:keyboard
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['error_code']=='uu_existing_connection'
    assert session._keyboard is keyboard and keyboard.close_calls==0
    assert other in state['connections']
    assert len([call for call in state['calls'] if call[:2]==('device','connect')])==1


@pytest.mark.parametrize('state_name',['connecting','unknown'])
def test_pending_or_unknown_video_after_keyboard_start_is_not_replayed(private_uu,state_name):
    session,state,_=private_uu
    keyboard=HealthyKeyboard(on_start=lambda:drop_video(state,state_name))
    session._keyboard_factory=lambda *args:keyboard
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['ok'] is False
    assert keyboard.close_calls==0 and session._keyboard is keyboard
    assert len([call for call in state['calls'] if call[:2]==('device','connect')])==1


def test_keyboard_start_cannot_extend_the_video_recovery_deadline(private_uu):
    session,state,_=private_uu
    def expired_start():
        state['clock']+=91
        drop_video(state)
    keyboard=HealthyKeyboard(on_start=expired_start)
    session._keyboard_factory=lambda *args:keyboard
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['error_code']=='uu_operation_timeout'
    assert session._keyboard is keyboard and keyboard.close_calls==0
    assert len([call for call in state['calls'] if call[:2]==('device','connect')])==1


def test_video_repair_is_not_repeated_when_it_drops_again(private_uu):
    session,state,cli=private_uu
    keyboard=HealthyKeyboard(on_start=lambda:drop_video(state))
    def status():
        if keyboard.status_calls>=2:drop_video(state)
    keyboard.on_status=status
    session._keyboard_factory=lambda *args:keyboard
    unique_video_windows(session,state,cli)
    result=session.perform('connect',{'device_name':'演示设备'})
    assert result['error_code']=='uu_viewer_not_ready'
    assert state['video_connects']==2
    assert keyboard.start_calls==1 and keyboard.close_calls==0
