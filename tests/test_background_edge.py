from pathlib import Path
from types import SimpleNamespace
import sys
import pytest
from web_tools.background_edge import _verify,edge_action


def test_edge_refuses_debugger_owned_by_main_browser(monkeypatch,tmp_path):
    connections=[SimpleNamespace(status='LISTEN',laddr=SimpleNamespace(port=48913,ip='127.0.0.1'),pid=7)]
    process=SimpleNamespace(name=lambda:'msedge.exe',cmdline=lambda:['msedge.exe','--user-data-dir=C:/main'],pid=7)
    monkeypatch.setitem(sys.modules,'psutil',SimpleNamespace(net_connections=lambda *a:connections,Process=lambda p:process))
    with pytest.raises(RuntimeError,match='verification failed'):
        _verify(tmp_path/'edge-profile',[{'pid':7}])


def test_edge_refuses_nonloopback_debugger(monkeypatch,tmp_path):
    connection=SimpleNamespace(status='LISTEN',laddr=SimpleNamespace(port=48913,ip='0.0.0.0'),pid=7)
    monkeypatch.setitem(sys.modules,'psutil',SimpleNamespace(net_connections=lambda *a:[connection]))
    with pytest.raises(RuntimeError,match='loopback'):_verify(tmp_path,[])


def test_edge_rejects_arbitrary_protocol_or_eval(monkeypatch,tmp_path):
    monkeypatch.setattr('web_tools.background_edge._verify',lambda *a:None)
    with pytest.raises(ValueError):edge_action('evaluate',{'script':'secret()'},data_root=tmp_path,windows=[])
