"""Capture a verified ToDesk viewer HWND without reading the host desktop.

PrintWindow is the sole backend. GPU surfaces that return empty/unrendered
pixels are explicitly unsupported; no foreground, screen, or input fallback
is permitted. The PNG uses full-window physical pixels and describes its
client offset so screenshot coordinates can be mapped to window messages.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import ctypes
from ctypes import wintypes
from io import BytesIO
import hashlib
import os
from pathlib import Path
import tempfile
from collections.abc import Mapping


class ToDeskCaptureError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _BitmapHeader(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("header", _BitmapHeader), ("colors", wintypes.DWORD * 1)]


class _NativeCapture:
    def __init__(self):
        if os.name != "nt":
            raise ToDeskCaptureError("windows_required", "ToDesk window capture requires Windows")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        user_signatures = {
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "IsIconic": ([wintypes.HWND], wintypes.BOOL),
            "IsHungAppWindow": ([wintypes.HWND], wintypes.BOOL),
            "GetWindowRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "GetClientRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "ClientToScreen": ([wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            "GetWindowDC": ([wintypes.HWND], wintypes.HDC),
            "ReleaseDC": ([wintypes.HWND, wintypes.HDC], ctypes.c_int),
            "PrintWindow": ([wintypes.HWND, wintypes.HDC, wintypes.UINT], wintypes.BOOL),
            "SetThreadDpiAwarenessContext": ([wintypes.HANDLE], wintypes.HANDLE),
        }
        gdi_signatures = {
            "CreateCompatibleDC": ([wintypes.HDC], wintypes.HDC),
            "DeleteDC": ([wintypes.HDC], wintypes.BOOL),
            "CreateDIBSection": ([wintypes.HDC, ctypes.POINTER(_BitmapInfo), wintypes.UINT,
                                  ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE,
                                  wintypes.DWORD], wintypes.HBITMAP),
            "SelectObject": ([wintypes.HDC, wintypes.HANDLE], wintypes.HANDLE),
            "DeleteObject": ([wintypes.HANDLE], wintypes.BOOL),
        }
        try:
            for library, signatures in ((self.user, user_signatures), (self.gdi, gdi_signatures)):
                for name, (args, result) in signatures.items():
                    function = getattr(library, name)
                    function.argtypes, function.restype = args, result
        except AttributeError as exc:
            raise ToDeskCaptureError("window_capture_unsupported", "Required Windows capture APIs are unavailable") from exc

    @contextmanager
    def dpi_context(self):
        previous = self.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not previous:
            raise ToDeskCaptureError("window_dpi_unavailable", "Cannot enter physical-pixel DPI mode")
        try:
            yield
        finally:
            if not self.user.SetThreadDpiAwarenessContext(previous):
                raise ToDeskCaptureError("window_dpi_restore_failed", "Cannot restore the capture thread DPI mode")

    def geometry(self, hwnd):
        if not self.user.IsWindow(hwnd):
            raise ToDeskCaptureError("window_identity_changed", "The verified ToDesk window no longer exists")
        if self.user.IsIconic(hwnd):
            raise ToDeskCaptureError("window_capture_unsupported", "Minimized ToDesk windows cannot be captured by this backend")
        if self.user.IsHungAppWindow(hwnd):
            raise ToDeskCaptureError("window_capture_unsupported", "The ToDesk viewer is not responding")
        outer, client, origin = wintypes.RECT(), wintypes.RECT(), wintypes.POINT()
        if not (self.user.GetWindowRect(hwnd, ctypes.byref(outer))
                and self.user.GetClientRect(hwnd, ctypes.byref(client))
                and self.user.ClientToScreen(hwnd, ctypes.byref(origin))):
            raise ToDeskCaptureError("window_geometry_unavailable", "Cannot read the ToDesk window geometry")
        return (outer.left, outer.top, outer.right, outer.bottom,
                origin.x - outer.left, origin.y - outer.top,
                client.right - client.left, client.bottom - client.top)

    def render(self, hwnd, width, height):
        from PIL import Image
        with ExitStack() as cleanup:
            source = self.user.GetWindowDC(hwnd)
            if not source:
                raise ToDeskCaptureError("window_dc_unavailable", "Cannot obtain the ToDesk window DC")
            cleanup.callback(self.user.ReleaseDC, hwnd, source)
            target = self.gdi.CreateCompatibleDC(source)
            if not target:
                raise ToDeskCaptureError("window_dc_unavailable", "Cannot allocate the capture DC")
            cleanup.callback(self.gdi.DeleteDC, target)
            info = _BitmapInfo()
            info.header.biSize = ctypes.sizeof(_BitmapHeader)
            info.header.biWidth, info.header.biHeight = width, -height
            info.header.biPlanes, info.header.biBitCount = 1, 32
            pixels = ctypes.c_void_p()
            bitmap = self.gdi.CreateDIBSection(source, ctypes.byref(info), 0,
                                               ctypes.byref(pixels), None, 0)
            if bitmap:
                cleanup.callback(self.gdi.DeleteObject, bitmap)
            if not bitmap or not pixels.value:
                raise ToDeskCaptureError("window_bitmap_unavailable", "Cannot allocate the capture bitmap")
            previous = self.gdi.SelectObject(target, bitmap)
            if not previous or previous == ctypes.c_void_p(-1).value:
                raise ToDeskCaptureError("window_bitmap_unavailable", "Cannot select the capture bitmap")
            cleanup.callback(self.gdi.SelectObject, target, previous)
            # An untouched target must fail validation, rather than expose
            # uninitialized memory as a plausible screenshot.
            ctypes.memset(pixels, 167, width * height * 4)
            if not self.user.PrintWindow(hwnd, target, 2):  # PW_RENDERFULLCONTENT
                raise ToDeskCaptureError("window_capture_unsupported", "ToDesk did not render a PrintWindow image; no screen fallback is available")
            raw = ctypes.string_at(pixels, width * height * 4)
            return Image.frombytes("RGB", (width, height), raw, "raw", "BGRX")


def _field(record, name):
    return record.get(name) if isinstance(record, Mapping) else getattr(record, name, None)


def _identity(record):
    return tuple(_field(record, name) for name in ("hwnd", "pid", "create_time", "executable", "title"))


def _bounds(left, top, right, bottom):
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def _validate_geometry(geometry):
    left, top, right, bottom, x, y, cw, ch = geometry
    width, height = right - left, bottom - top
    if (not 32 <= width <= 8192 or not 32 <= height <= 8192
            or width * height > 32_000_000 or cw < 32 or ch < 32
            or x < 0 or y < 0 or x + cw > width or y + ch > height):
        raise ToDeskCaptureError("window_geometry_unsupported", "ToDesk window/client geometry is outside capture limits")
    return width, height


def _validate_content(image, geometry):
    """Reject empty GPU surfaces using central pixels and spatial coverage.

    Decoration or a narrow ToDesk toolbar must not make a black viewer pass.
    This is conservative: a genuinely uniform remote screen is unsupported.
    Content bounds describe the validated client area, not a guessed viewport.
    """
    from PIL import ImageStat
    _, _, _, _, x, y, cw, ch = geometry
    mx, my = max(2, cw // 12), max(2, ch // 8)
    analysis = image.crop((x + mx, y + my, x + cw - mx, y + ch - my))
    sample = analysis.resize((min(256, analysis.width), min(256, analysis.height)))
    stats = ImageStat.Stat(sample)
    colors = sample.getcolors(sample.width * sample.height) or []
    # Sampled detail must occur in multiple cells, rather than only a toolbar
    # sliver or a decorated border around an otherwise unrendered surface.
    detailed = 0
    detailed_rows, detailed_columns = set(), set()
    for row in range(3):
        for col in range(3):
            tile = sample.crop((col * sample.width // 3, row * sample.height // 3,
                                (col + 1) * sample.width // 3, (row + 1) * sample.height // 3))
            if max(ImageStat.Stat(tile).stddev) >= 3 and len(tile.getcolors(tile.width * tile.height) or []) >= 8:
                detailed += 1
                detailed_rows.add(row)
                detailed_columns.add(col)
    dominant = max((count for count, _ in colors), default=sample.width * sample.height) / (sample.width * sample.height)
    if (max(stats.stddev) < 3 or len(colors) < 12 or dominant >= .995 or detailed < 2
            or len(detailed_rows) < 2 or len(detailed_columns) < 2):
        raise ToDeskCaptureError("window_capture_blank", "ToDesk returned a blank/unrendered client image; this viewer is unsupported by PrintWindow")
    return {"content_colors": len(colors), "content_detailed_tiles": detailed,
            "content_detailed_rows": len(detailed_rows), "content_detailed_columns": len(detailed_columns),
            "content_dominant_fraction": dominant, "content_stddev": stats.stddev,
            "validation_bounds": _bounds(x + mx, y + my, x + cw - mx, y + ch - my)}


def capture_todesk_window(record, output_path: Path, *, verifier) -> dict:
    """Verify one viewer before/after a window-only capture and atomically save.

    ``verifier(record)`` must validate HWND, PID, process creation time,
    executable and session/viewer title, raising on mismatch. Explicit False
    is also rejected. It must not activate or otherwise alter the viewer.
    """
    identity = _identity(record)
    hwnd = identity[0]
    if isinstance(hwnd, bool) or not isinstance(hwnd, int) or hwnd <= 0:
        raise ToDeskCaptureError("invalid_window_record", "A verified ToDesk HWND is required")
    def verify():
        if _identity(record) != identity or verifier(record) is False or _identity(record) != identity:
            raise ToDeskCaptureError("window_identity_changed", "The ToDesk viewer identity changed during capture")
    verify()
    native = _NativeCapture()
    with native.dpi_context():
        geometry = native.geometry(hwnd)
        width, height = _validate_geometry(geometry)
        image = native.render(hwnd, width, height)
        if image.size != (width, height):
            raise ToDeskCaptureError("window_geometry_changed", "The captured image dimensions changed")
        if native.geometry(hwnd) != geometry:
            raise ToDeskCaptureError("window_geometry_changed", "The ToDesk viewer moved or resized during capture")
        validation = _validate_content(image, geometry)
        stream = BytesIO()
        image.save(stream, "PNG")
        payload = stream.getvalue()
        verify()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output_path.parent, prefix=".todesk-", suffix=".png", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
        os.replace(temporary, output_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    left, top, right, bottom, x, y, cw, ch = geometry
    return {"path": str(output_path), "mime_type": "image/png", "width": width, "height": height,
            "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
            "source_hwnd": hwnd, "capture_method": "PrintWindow(PW_RENDERFULLCONTENT)",
            "capture_scope": "verified_todesk_window", "host_input_used": False,
            "client_origin": {"x": x, "y": y}, "client_width": cw, "client_height": ch,
            "outer_bounds": _bounds(left, top, right, bottom),
            "client_bounds": _bounds(x, y, x + cw, y + ch),
            "content_bounds": _bounds(x, y, x + cw, y + ch), "content_bounds_basis": "client_area",
            **validation}
