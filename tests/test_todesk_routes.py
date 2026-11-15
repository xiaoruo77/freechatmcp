import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from web_mcp.direct_tools import DirectTools, TOOL_METADATA
from web_tools import background_desktop_host as host_module


@pytest.mark.parametrize('action',['connect','click','type','key','scroll','drag','close'])
async def test_todesk_write_requires_confirm_before_dispatch(action,monkeypatch):
    tool=DirectTools.__new__(DirectTools)
    tool.ctx=SimpleNamespace(settings=SimpleNamespace(admin_mode=True))
    async def forbidden(*a,**k):pytest.fail('must not reach user broker without confirm')
    monkeypatch.setattr('web_mcp.direct_tools.call_background',forbidden)
    with pytest.raises(PermissionError):await tool._todesk_control(action,{},False)


async def test_todesk_tool_maps_to_same_authenticated_broker(monkeypatch):
    tool=DirectTools.__new__(DirectTools)
    settings=SimpleNamespace(admin_mode=True)
    tool.ctx=SimpleNamespace(settings=settings)
    calls=[]
    async def call(*a,**k):calls.append((a,k));return {'ok':True}
    monkeypatch.setattr('web_mcp.direct_tools.call_background',call)
    result=await tool._todesk_control('connect',{'device_name':'DEMO-NODE'},True)
    assert result['ok'] and calls[0][0]==(settings,'todesk',{'action':'connect','arguments':{'device_name':'DEMO-NODE'}})
    assert TOOL_METADATA['todesk_control']['scope']=='desktop_control'


@pytest.mark.parametrize('action',['click','type','key','scroll','drag'])
async def test_unverified_background_input_never_dispatches_even_with_confirm(action,monkeypatch):
    tool=DirectTools.__new__(DirectTools)
    tool.ctx=SimpleNamespace(settings=SimpleNamespace(admin_mode=True))
    async def forbidden(*a,**k):pytest.fail('unverified input must not reach native broker')
    monkeypatch.setattr('web_mcp.direct_tools.call_background',forbidden)
    result=await tool._todesk_control(action,{},True)
    assert result['ok'] is False and result['error_code']=='background_input_unverified'
    assert result['evidence']['action_may_have_run'] is False


def test_host_todesk_dispatch_does_not_use_private_launch_or_uu_session(tmp_path,monkeypatch):
    import web_tools.todesk_control as module
    config=tmp_path/'07_web_config';config.mkdir()
    (config/'todesk_control.json').write_text(json.dumps({'enabled':True,'allowed_devices':{'DEMO-NODE':{'device_id':'282828282'}}}))
    calls=[]
    class Session:
        def __init__(self,**args):calls.append(('construct',args))
        def perform(self,*args):calls.append(('perform',args));return {'ok':True}
    monkeypatch.setattr(module,'ToDeskSession',Session)
    monkeypatch.setattr('web_tools.todesk_background.StrictBackgroundToDesk', lambda *a: object())
    actor=host_module.BackgroundDesktopHost(data_root=tmp_path/'08_web_data',allowed_apps={'todesk':tmp_path/'ToDesk.exe'})
    actor._windows=lambda:[]
    actor._run_uu_default=lambda fn,*a,**k:fn(*a)
    actor._launch_generic_app=lambda *a:pytest.fail('no local ToDesk client relaunch')
    result=actor._dispatch('todesk',{'action':'connect','arguments':{'device_name':'DEMO-NODE'}})
    assert result['ok'] and actor._uu_session is None
    assert calls[1]==('perform',('connect',{'device_name':'DEMO-NODE'}))
