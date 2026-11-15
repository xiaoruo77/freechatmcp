import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from web_tools.remote_gui_capture_client import RemoteCaptureClient
from web_tools.uu_remote_keyboard import RemoteKeyboardSession, RemoteKeyboardError


ID='a'*32


def client(tmp_path, replies):
    data=tmp_path/'data'; data.mkdir()
    config=tmp_path/'07_web_config'; config.mkdir()
    token=tmp_path/'bridge'; token.write_text('b'*48)
    (config/'background_desktop.json').write_text(json.dumps(dict(token_file=str(token),capture_public_origin='https://remote.example')))
    calls=[]
    class Reply:
        def __init__(self, value):self.value=value
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,limit):return json.dumps(self.value).encode() if self.value is not None else b''
    def open_(request,**kw):
        calls.append(request)
        return Reply(replies.pop(0))
    return RemoteCaptureClient(data,opener=SimpleNamespace(open=open_)),calls


def test_tickets_use_bridge_only_on_fixed_loopback_url(tmp_path):
    ticket=dict(capture_id=ID,upload_url='https://remote.example/remote-gui/captures/'+ID,token='t'*43)
    instance,calls=client(tmp_path,[ticket,None])
    assert instance.issue()==ticket
    instance.cancel(ID)
    assert all(r.full_url.startswith('http://127.0.0.1:48271/internal/remote-gui/captures') for r in calls)
    assert [r.method for r in calls]==['POST','DELETE']


def test_ticket_cannot_redirect_remote_capture_to_another_host(tmp_path):
    instance,_=client(tmp_path,[dict(capture_id=ID,upload_url='https://evil.example/remote-gui/captures/'+ID,token='t'*43)])
    with pytest.raises(ValueError,match='trusted origin'):instance.issue()


def test_completed_file_must_be_exact_ticket_png_in_capture_root(tmp_path):
    instance,_=client(tmp_path,[dict(status='completed',capture=dict(path=str(tmp_path/'foreign.png')))])
    with pytest.raises(ValueError,match='exact capture path'):instance.wait(ID)


def test_completed_file_returns_metadata_only_for_expected_capture(tmp_path):
    path=tmp_path/'data'/'background_desktop'/'captures'/('gui-'+ID+'.png')
    metadata=dict(path=str(path),width=10,height=10,mime_type='image/png')
    instance,_=client(tmp_path,[dict(status='completed',capture=metadata)])
    path.parent.mkdir(parents=True);path.write_bytes(b'PNG')
    assert instance.wait(ID)==metadata


@pytest.mark.parametrize('mime,extension', [('image/png', '.png'), ('image/jpeg', '.jpg')])
def test_completed_image_path_matches_its_strict_mime_and_ticket(tmp_path, mime, extension):
    path=tmp_path/'data'/'background_desktop'/'captures'/('gui-'+ID+extension)
    metadata=dict(path=str(path),width=10,height=10,mime_type=mime)
    instance,_=client(tmp_path,[dict(status='completed',capture=metadata)])
    path.parent.mkdir(parents=True);path.write_bytes(b'fixture')
    assert instance.wait(ID)==metadata
    assert instance._timeout==20


@pytest.mark.parametrize('mime,extension', [
    ('image/jpeg','.png'), ('image/png','.jpg'), ('image/jpeg','.jpeg'),
    ('image/gif','.gif'), ('image/webp','.webp'), (None,'.png'), ({},'.png'),
])
def test_completed_capture_rejects_mime_extension_mismatch_and_other_formats(tmp_path, mime, extension):
    path=tmp_path/'data'/'background_desktop'/'captures'/('gui-'+ID+extension)
    metadata=dict(path=str(path),width=10,height=10,mime_type=mime)
    instance,_=client(tmp_path,[dict(status='completed',capture=metadata)])
    path.parent.mkdir(parents=True);path.write_bytes(b'fixture')
    with pytest.raises(ValueError,match='exact capture path'):instance.wait(ID)


@pytest.mark.parametrize('action,args',[
 ('configure_capture',{'origin':'https://remote.example'}),
 ('capture',dict(capture_id=ID,upload_url='https://remote.example/remote-gui/captures/'+ID,token='t'*43)),
 ('click',dict(x=-10,y=20,bounds=dict(left=-100,top=0,width=1000,height=500),button='left',count=1)),
 ('scroll',dict(x=10,y=20,bounds=dict(left=0,top=0,width=100,height=100),vertical=120,horizontal=0)),
 ('drag',dict(x=10,y=20,to_x=30,to_y=40,bounds=dict(left=0,top=0,width=100,height=100),duration_ms=500)),
])
def test_fixed_remote_graphics_schema_accepts_bounded_actions(action,args):
    RemoteKeyboardSession._validate(action,args)


@pytest.mark.parametrize('action,args',[
 ('configure_capture',{'origin':'http://remote.example'}),
 ('configure_capture',{'origin':'https://remote.example/path'}),
 ('capture',dict(capture_id=ID,upload_url='https://remote.example/arbitrary/'+ID,token='t'*43)),
 ('click',dict(x=101,y=20,bounds=dict(left=0,top=0,width=100,height=100),button='left',count=1)),
 ('click',dict(x=True,y=20,bounds=dict(left=0,top=0,width=100,height=100),button='left',count=1)),
 ('click',dict(x=10,y=20,bounds=dict(left=0,top=0,width=100,height=100),button=[],count=1)),
 ('scroll',dict(x=10,y=20,bounds=dict(left=0,top=0,width=100,height=100),vertical=0,horizontal=0)),
 ('scroll',dict(x=10,y=20,bounds=dict(left=0,top=0,width=100,height=100),vertical=120,horizontal=0,expect={})),
])
def test_fixed_remote_graphics_rejects_unbounded_or_extra_fields(action,args):
    with pytest.raises(RemoteKeyboardError):RemoteKeyboardSession._validate(action,args)
