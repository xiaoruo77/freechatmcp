from types import SimpleNamespace
import pytest
from web_tools.browser_process_identity import identify_existing_browser_peer, XIAORUO_EXE


class Proc:
    def __init__(self, pid=123, exe=str(XIAORUO_EXE), command=None, parents=None, created=100.0):
        self.pid,self._exe,self._command,self._parents,self._created=pid,exe,command or [exe],parents or [],created
    def exe(self): return self._exe
    def cmdline(self): return self._command
    def parents(self): return self._parents
    def create_time(self): return self._created


class System:
    def __init__(self, process=None, rows=None):
        self.process = process or Proc()
        self.rows = rows if rows is not None else [SimpleNamespace(laddr=("127.0.0.1",50123),raddr=("127.0.0.1",48272),pid=self.process.pid,status="ESTABLISHED")]
        self.scan_count=0
    def net_connections(self,kind):
        assert kind=="tcp";self.scan_count+=1;return self.rows
    def Process(self,pid):
        assert pid==self.process.pid;return self.process


def test_manual_existing_process_identity_stable_for_same_lifetime():
    system=System()
    a=identify_existing_browser_peer("127.0.0.1",50123,psutil_api=system)
    b=identify_existing_browser_peer("127.0.0.1",50123,psutil_api=system)
    assert a["instance_id"]==b["instance_id"] and a["manual_instance"]
    assert a["browser_kind"]=="xiaoruo_browser" and a["engine"]=="cef"
    assert not {"debug_attach","scroll","activate_tab"}.intersection(a["supported_actions"])
    assert not any(key in a for key in ["cmdline","cookies","profile","token"])
    changed=identify_existing_browser_peer("127.0.0.1",50123,psutil_api=System(Proc(created=101)))
    assert a["instance_id"]!=changed["instance_id"]


def test_existing_child_is_attributed_only_to_exact_cef_ancestor():
    parent=Proc(pid=99)
    child=Proc(pid=123,exe=r"C:\unrelated\helper.exe",parents=[parent])
    value=identify_existing_browser_peer("127.0.0.1",50123,psutil_api=System(child))
    assert value["process_id"]==99 and value["manual_instance"]
    wrong=Proc(exe=r"C:\elsewhere\xiaoruo-browser.exe")
    assert identify_existing_browser_peer("127.0.0.1",50123,psutil_api=System(wrong)) is None


def test_stdio_instance_is_reported_as_automated():
    process=Proc(command=[str(XIAORUO_EXE),"--mcp-stdio"])
    value=identify_existing_browser_peer("127.0.0.1",50123,psutil_api=System(process))
    assert value["manual_instance"] is False


@pytest.mark.parametrize("rows", [[],[SimpleNamespace(laddr=("127.0.0.1",50123),raddr=("127.0.0.1",9999),pid=123,status="ESTABLISHED")],
 [SimpleNamespace(laddr=("127.0.0.1",50123),raddr=("127.0.0.1",48272),pid=None,status="ESTABLISHED")],
 [SimpleNamespace(laddr=("127.0.0.1",50123),raddr=("127.0.0.1",48272),pid=123,status="CLOSE_WAIT")]])
def test_absent_wrong_endpoint_or_no_owner_is_never_guessed(rows):
    assert identify_existing_browser_peer("127.0.0.1",50123,psutil_api=System(rows=rows)) is None


def test_access_denied_and_external_peer_are_unclassified():
    class Denied(System):
        def net_connections(self,kind):raise PermissionError("denied")
    assert identify_existing_browser_peer("127.0.0.1",50123,psutil_api=Denied()) is None
    system=System()
    assert identify_existing_browser_peer("192.168.1.1",50123,psutil_api=system) is None
    assert system.scan_count==0