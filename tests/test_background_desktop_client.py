from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from web_tools import background_desktop_client as client


def test_vscode_arguments_cannot_replace_authorized_action(tmp_path, monkeypatch):
    config = tmp_path / "07_web_config"
    config.mkdir()
    token = tmp_path / "private-token"
    token.write_text("test-credential-" * 4)
    (config / "background_desktop.json").write_text(json.dumps({"port": 48911, "token_file": str(token)}))
    observed = {}

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, *args):
            return b'{"ok":true,"backend":"local_background","result":{}}'

    class Opener:
        def open(self, request, **kwargs):
            observed.update(json.loads(request.data))
            return Response()

    monkeypatch.setattr(client, "build_opener", lambda *args: Opener())
    client._request(SimpleNamespace(project_root=tmp_path), "read_document", {"action": "write_document", "path": "one.txt"}, vscode=True)
    assert observed["action"] == "read_document"


@pytest.mark.parametrize('subaction,expected',[('screenshot',95),('type',60),('close',60),('disconnect',60),('connect',110),('keyboard_start',110),('key',30),('click',30),('scroll',30),('drag',30),('status',30)])
def test_uu_outer_deadline_covers_screenshot_upload_and_ack(tmp_path,monkeypatch,subaction,expected):
    config=tmp_path/'07_web_config';config.mkdir()
    token=tmp_path/'token';token.write_text('x'*48)
    (config/'background_desktop.json').write_text(json.dumps({'port':48911,'token_file':str(token)}))
    observed=[]
    class Reply:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,*a):return b'{"ok":true,"backend":"local_background","result":{}}'
    def open_(request,**options):
        observed.append(options['timeout'])
        return Reply()
    monkeypatch.setattr(client,'build_opener',lambda *a:SimpleNamespace(open=open_))
    client._request(SimpleNamespace(project_root=tmp_path),'uu',{'action':subaction})
    assert observed==[expected]


async def test_interactive_coordinates_never_reach_background_service(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Physical-input request must never reach the backend")
    monkeypatch.setattr(client, "call", forbidden)
    for action, arguments in [("Click", {"loc": [1, 2]}), ("Shortcut", {"shortcut": "ctrl+s"})]:
        result = await client.windows_action(SimpleNamespace(), action, arguments)
        assert result["ok"] is False
        assert result["error_code"] == "background_pattern_required"
        assert result["evidence"]["action_may_have_run"] is False


async def test_failure_has_no_foreground_fallback(monkeypatch):
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError()))
    result = await client.call(SimpleNamespace(), "launch_app", {"app": "vscode"})
    assert result["ok"] is False
    assert result["error_code"] == "local_background_unavailable"
    assert result["retryable"] is False
    assert result["evidence"]["host_input_used"] is False


async def test_uu_failure_preserves_safe_code_details_and_structured_backend_data(monkeypatch):
    backend={'ok':False,'error_code':'private_focus_attach_failed',
        'message':'Cannot attach the two private GUI input queues','phase':'window_input',
        'diagnostics':{'cli_return_code':103,'cli_error_code':'ACCOUNT_RESTORING'},
        'request_id':'uu-1234:request','action_may_have_run':True,'remote_effect_verified':False}
    monkeypatch.setattr(client,'_request',lambda *a,**k:{'ok':False,'backend':'local_background','error':backend,'result':backend})
    result=await client.call(SimpleNamespace(),'uu',{'action':'click'})
    assert result['error_code']=='private_focus_attach_failed'
    assert result['error']['message']==backend['message']
    assert result['error']['phase']=='window_input'
    assert result['error']['diagnostics']==backend['diagnostics']
    assert result['evidence']['request_id']=='uu-1234:request'
    assert result['evidence']['action_may_have_run'] is True
    assert result['result']['data']==backend
    assert result['status']=='failed' and result['terminal'] is True and result['retryable'] is False


async def test_validation_rejection_preserves_explicit_no_dispatch_for_write_action(monkeypatch):
    backend={'ok':False,'error_code':'uu_ui_target_stale','message':'Take a fresh ui_snapshot',
             'phase':'ui_click','action_may_have_run':False}
    monkeypatch.setattr(client,'_request',lambda *a,**k:{'ok':False,'backend':'local_background','result':backend,'error':backend})
    result=await client.call(SimpleNamespace(),'uu',{'action':'ui_click','arguments':{'element_id':'stale'}})
    assert result['error_code']=='uu_ui_target_stale'
    assert result['evidence']['action_may_have_run'] is False
    assert result['result']['data']['phase']=='ui_click'


async def test_unknown_transport_outcome_is_not_inferred_from_write_action(monkeypatch):
    def unavailable(*a,**k):raise TimeoutError('Bearer private-token-secret raw request')
    monkeypatch.setattr(client,'_request',unavailable)
    result=await client.call(SimpleNamespace(),'uu',{'action':'type','arguments':{'text':'test'}})
    assert result['error_code']=='local_background_unavailable'
    assert result['evidence']['action_may_have_run'] is None
    assert result['result']['data'] is None
    assert result['terminal'] is True and result['retryable'] is False and result['status']=='failed'
    assert 'private-token-secret' not in json.dumps(result)


async def test_unknown_backend_exception_does_not_expose_raw_error_or_result(monkeypatch):
    raw={'code':'RuntimeError','message':'Authorization Bearer private-token-secret'}
    monkeypatch.setattr(client,'_request',lambda *a,**k:{'ok':False,'backend':'local_background','result':raw,'error':raw})
    result=await client.call(SimpleNamespace(),'launch_app',{'app':'vscode'})
    assert result['error_code']=='background_action_unavailable'
    assert result['result']['data'] is None and result['evidence']['action_may_have_run'] is None
    assert 'private-token-secret' not in json.dumps(result)


async def test_structured_failure_without_dispatch_evidence_remains_unknown(monkeypatch):
    backend={'ok':False,'error_code':'background_actor_failed','message':'Actor failed without foreground fallback'}
    monkeypatch.setattr(client,'_request',lambda *a,**k:{'ok':False,'backend':'local_background','error':backend,'result':backend})
    result=await client.call(SimpleNamespace(),'cleanup')
    assert result['error_code']=='background_actor_failed'
    assert result['evidence']['action_may_have_run'] is None
    assert result['result']['data']==backend


async def test_nested_vscode_safe_error_is_preserved(monkeypatch):
    backend={'ok':False,'error':{'code':'document_changed','message':'Expected document hash no longer matches'},
             'request_id':'vscode-123'}
    monkeypatch.setattr(client,'_request',lambda *a,**k:{'ok':False,'backend':'local_background','error':backend})
    result=await client.call(SimpleNamespace(),'write_document',{},vscode=True)
    assert result['error_code']=='document_changed'
    assert result['result']['data']==backend
    assert result['evidence']['request_id']=='vscode-123'
    assert result['operation']=='vscode_background' and result['retryable'] is False


async def test_success_uses_backend_dispatch_flag_without_action_name_inference(monkeypatch):
    backend={'ok':True,'action_may_have_run':False,'request_id':'read-123'}
    monkeypatch.setattr(client,'_request',lambda *a,**k:{'ok':True,'backend':'local_background','result':backend})
    result=await client.call(SimpleNamespace(),'launch_app',{'app':'vscode'})
    assert result['ok'] and result['evidence']['action_may_have_run'] is False
    assert result['evidence']['request_id']=='read-123'
