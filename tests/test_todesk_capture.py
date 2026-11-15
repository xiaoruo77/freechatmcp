from contextlib import nullcontext
import ctypes
import hashlib
from types import SimpleNamespace

from PIL import Image, ImageDraw
import pytest

from web_tools import todesk_capture as capture


def texture(size=(240, 200)):
    image = Image.new("RGB", size)
    image.putdata([((x * 7) % 256, (y * 11) % 256, (x * 3 + y * 5) % 256)
                   for y in range(size[1]) for x in range(size[0])])
    return image


@pytest.fixture
def viewer(monkeypatch):
    class FakeNative:
        def __init__(self):
            self.calls = []
            self.rect = (100, 200, 340, 400, 8, 28, 224, 164)
            self.image = texture()
            self.after_render = None
        def dpi_context(self):
            return nullcontext()
        def geometry(self, hwnd):
            self.calls.append(("geometry", hwnd))
            return self.rect
        def render(self, hwnd, width, height):
            self.calls.append(("PrintWindow", hwnd, width, height))
            if self.after_render:
                self.after_render()
            return self.image
    native = FakeNative()
    monkeypatch.setattr(capture, "_NativeCapture", lambda: native)
    record = {"hwnd": 123, "pid": 456, "create_time": 12345.5,
              "executable": "D:/ToDesk/ToDesk.exe", "title": "DEMO-NODE"}
    return native, record


def test_exact_window_capture_has_consistent_client_coordinates(viewer, tmp_path):
    native, record = viewer
    verified = []
    target = tmp_path / "nested" / "viewer.png"
    result = capture.capture_todesk_window(record, target, verifier=lambda item: verified.append(dict(item)))
    assert verified == [record, record]
    assert native.calls == [("geometry", 123), ("PrintWindow", 123, 240, 200), ("geometry", 123)]
    assert result["client_origin"] == {"x": 8, "y": 28}
    assert (result["client_width"], result["client_height"]) == (224, 164)
    assert result["client_bounds"] == {"left": 8, "top": 28, "right": 232, "bottom": 192}
    assert result["content_bounds"] == result["client_bounds"]
    assert result["content_bounds_basis"] == "client_area"
    assert result["outer_bounds"] == {"left": 100, "top": 200, "right": 340, "bottom": 400}
    assert (result["width"], result["height"]) == (240, 200)
    assert result["capture_method"] == "PrintWindow(PW_RENDERFULLCONTENT)"
    assert result["source_hwnd"] == 123 and result["host_input_used"] is False
    assert result["mime_type"] == "image/png"
    assert result["size_bytes"] == target.stat().st_size
    assert result["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    with Image.open(target) as saved:
        assert saved.size == (240, 200)


@pytest.mark.parametrize("background", [(0, 0, 0), (255, 255, 255), (167, 167, 167)])
def test_blank_gpu_client_rejected_even_with_decorated_titlebar(viewer, tmp_path, background):
    native, record = viewer
    native.image = Image.new("RGB", (240, 200), background)
    native.image.paste(texture((240, 28)), (0, 0))
    target = tmp_path / "viewer.png"
    with pytest.raises(capture.ToDeskCaptureError) as error:
        capture.capture_todesk_window(record, target, verifier=lambda _: True)
    assert error.value.code == "window_capture_blank"
    assert not target.exists()


def test_single_colored_pixel_does_not_make_black_frame_valid(viewer, tmp_path):
    native, record = viewer
    native.image = Image.new("RGB", (240, 200))
    ImageDraw.Draw(native.image).point((100, 100), fill=(255, 255, 255))
    with pytest.raises(capture.ToDeskCaptureError, match="blank/unrendered"):
        capture.capture_todesk_window(record, tmp_path / "bad.png", verifier=lambda _: True)


def test_complex_toolbar_inside_client_does_not_validate_black_viewer(viewer, tmp_path):
    native, record = viewer
    native.image = Image.new("RGB", (240, 200))
    native.image.paste(texture((224, 52)), (8, 28))
    with pytest.raises(capture.ToDeskCaptureError, match="blank/unrendered"):
        capture.capture_todesk_window(record, tmp_path / "bad.png", verifier=lambda _: True)


def test_verifier_failure_before_capture_never_allocates_or_writes(viewer, tmp_path):
    native, record = viewer
    with pytest.raises(capture.ToDeskCaptureError) as error:
        capture.capture_todesk_window(record, tmp_path / "bad.png", verifier=lambda _: False)
    assert error.value.code == "window_identity_changed"
    assert not native.calls
    assert list(tmp_path.iterdir()) == []


def test_post_capture_process_change_does_not_publish_or_replace_existing(viewer, tmp_path):
    native, record = viewer
    verified = []
    def verify(item):
        verified.append(item)
        if len(verified) > 1:
            raise RuntimeError("process creation time changed")
    target = tmp_path / "viewer.png"
    target.write_bytes(b"old artifact")
    with pytest.raises(RuntimeError, match="creation time changed"):
        capture.capture_todesk_window(record, target, verifier=verify)
    assert len(verified) == 2
    assert target.read_bytes() == b"old artifact"
    assert list(tmp_path.iterdir()) == [target]


def test_mutated_bound_record_is_rejected(viewer, tmp_path):
    native, record = viewer
    native.after_render = lambda: record.update(pid=789)
    with pytest.raises(capture.ToDeskCaptureError) as error:
        capture.capture_todesk_window(record, tmp_path / "bad.png", verifier=lambda _: True)
    assert error.value.code == "window_identity_changed"
    assert not (tmp_path / "bad.png").exists()


def test_geometry_change_rejected_before_publishing(viewer, tmp_path):
    native, record = viewer
    native.after_render = lambda: setattr(native, "rect", (101, 200, 341, 400, 8, 28, 224, 164))
    with pytest.raises(capture.ToDeskCaptureError) as error:
        capture.capture_todesk_window(record, tmp_path / "bad.png", verifier=lambda _: True)
    assert error.value.code == "window_geometry_changed"


class FakeGDI:
    def __init__(self, events, fail=None):
        self.events, self.fail = events, fail
        self.storage = None
    def CreateCompatibleDC(self, source):
        self.events.append(("CreateCompatibleDC", source))
        return 0 if self.fail == "dc" else 20
    def CreateDIBSection(self, source, info_pointer, usage, bits_pointer, section, offset):
        self.events.append(("CreateDIBSection", source))
        if self.fail == "bitmap":
            return 0
        info = ctypes.cast(info_pointer, ctypes.POINTER(capture._BitmapInfo)).contents
        self.storage = (ctypes.c_ubyte * (info.header.biWidth * abs(info.header.biHeight) * 4))()
        ctypes.cast(bits_pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(self.storage)
        return 30
    def SelectObject(self, dc, obj):
        self.events.append(("SelectObject", dc, obj))
        return 0 if self.fail == "select" else 40
    def DeleteObject(self, bitmap):
        self.events.append(("DeleteObject", bitmap))
        return True
    def DeleteDC(self, dc):
        self.events.append(("DeleteDC", dc))
        return True


def fake_print_native(*, fail=None):
    native = object.__new__(capture._NativeCapture)
    events = []
    native.gdi = FakeGDI(events, fail)
    def window_dc(hwnd):
        events.append(("GetWindowDC", hwnd))
        return 0 if fail == "source" else 10
    def print_window(hwnd, dc, flags):
        events.append(("PrintWindow", hwnd, dc, flags))
        if fail == "print":
            return False
        payload = texture((64, 64)).tobytes("raw", "BGRX")
        ctypes.memmove(native.gdi.storage, payload, len(payload))
        return True
    native.user = SimpleNamespace(GetWindowDC=window_dc, PrintWindow=print_window,
                                  ReleaseDC=lambda hwnd, dc: events.append(("ReleaseDC", hwnd, dc)))
    return native, events


def test_native_printwindow_reads_only_bound_window_and_cleans_all_handles():
    native, events = fake_print_native()
    image = native.render(123, 64, 64)
    assert image.tobytes() == texture((64, 64)).tobytes()
    assert events == [("GetWindowDC", 123), ("CreateCompatibleDC", 10), ("CreateDIBSection", 10),
                      ("SelectObject", 20, 30), ("PrintWindow", 123, 20, 2),
                      ("SelectObject", 20, 40), ("DeleteObject", 30),
                      ("DeleteDC", 20), ("ReleaseDC", 123, 10)]


@pytest.mark.parametrize("failure,cleanup", [
    ("source", []),
    ("dc", [("ReleaseDC", 123, 10)]),
    ("bitmap", [("DeleteDC", 20), ("ReleaseDC", 123, 10)]),
    ("select", [("DeleteObject", 30), ("DeleteDC", 20), ("ReleaseDC", 123, 10)]),
    ("print", [("SelectObject", 20, 40), ("DeleteObject", 30), ("DeleteDC", 20), ("ReleaseDC", 123, 10)]),
])
def test_native_failures_release_every_acquired_handle(failure, cleanup):
    native, events = fake_print_native(fail=failure)
    with pytest.raises(capture.ToDeskCaptureError):
        native.render(123, 64, 64)
    if cleanup:
        assert events[-len(cleanup):] == cleanup
    assert not any(event[0] in {"BitBlt", "GetDC", "ShowWindow", "SetForegroundWindow", "SetWindowPos"} for event in events)


def test_thread_dpi_context_restored_on_failure():
    native = object.__new__(capture._NativeCapture)
    changes = []
    def switch(value):
        changes.append(value)
        return 900
    native.user = SimpleNamespace(SetThreadDpiAwarenessContext=switch)
    with pytest.raises(ValueError, match="test failure"):
        with native.dpi_context():
            raise ValueError("test failure")
    assert changes[0].value == ctypes.c_void_p(-4).value
    assert changes[1] == 900
