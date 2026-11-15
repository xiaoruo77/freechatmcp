"""Capture explicitly verified private-desktop windows without display input."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import hashlib


def validate_content(image) -> dict:
    from PIL import ImageStat
    width,height=image.size
    content=image.crop((min(16,width//8),min(80,height//4),max(width-16,width//2),max(height-16,height//2)))
    sample=content.resize((min(256,content.width),min(256,content.height)))
    stats=ImageStat.Stat(sample)
    colors=len(sample.getcolors(sample.width*sample.height) or [])
    if max(stats.stddev)<2 or colors<12:
        raise RuntimeError('The private window returned a blank/unrendered image; no foreground fallback')
    return {'content_stddev':stats.stddev,'content_colors':colors}


def capture_window(hwnd: int, output_path: Path) -> dict:
    """Caller must verify HWND membership in its non-input desktop first."""
    import win32gui
    import win32ui
    from PIL import Image, ImageStat
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if not 32 <= width <= 8192 or not 32 <= height <= 8192 or width * height > 32_000_000:
        raise ValueError("Window size is outside capture limits")
    if win32gui.IsIconic(hwnd):
        raise ValueError("A minimized private window cannot be captured reliably")
    dc = win32gui.GetWindowDC(hwnd)
    if not dc:
        raise RuntimeError("Window capture DC unavailable")
    source = win32ui.CreateDCFromHandle(dc)
    memory = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(source, width, height)
    previous = memory.SelectObject(bitmap)
    try:
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        user.PrintWindow.restype = wintypes.BOOL
        if not user.PrintWindow(hwnd, memory.GetSafeHdc(), 2):
            raise RuntimeError("Private window did not render a PrintWindow image")
        pixels = bitmap.GetBitmapBits(True)
        im = Image.frombuffer("RGB", (width, height), pixels, "raw", "BGRX", 0, 1)
        # Inspect the content region, not a decorated title bar around black.
        validation=validate_content(im)
        output_path.parent.mkdir(parents=True,exist_ok=True)
        im.save(output_path, "PNG")
        return {"path":str(output_path),"mime_type":"image/png","width":width,"height":height,
                "size_bytes":output_path.stat().st_size,"sha256":hashlib.sha256(output_path.read_bytes()).hexdigest(),
                "capture_method":"PrintWindow(PW_RENDERFULLCONTENT)","capture_scope":"private_window",
                "source_hwnd":hwnd,**validation,
                "host_input_used":False}
    finally:
        memory.SelectObject(previous)
        win32gui.DeleteObject(bitmap.GetHandle())
        memory.DeleteDC()
        source.DeleteDC()
        win32gui.ReleaseDC(hwnd, dc)


def capture_desktop(windows: list[dict], output_path: Path) -> dict:
    """Compose the verified private windows in their desktop Z order.

    A non-input desktop has no live monitor surface. This image explicitly
    identifies its source as window composition, never a main-screen capture.
    """
    import win32gui
    from PIL import Image
    candidates=[w for w in windows if w["visible"] and w["title"] and not win32gui.IsIconic(w["hwnd"])]
    if not candidates:raise ValueError("No rendered private windows are available")
    captures=[]
    for index,w in enumerate(reversed(candidates)):
        path=output_path.with_name(output_path.stem+f"-window-{index}.png")
        cap=capture_window(w["hwnd"],path)
        captures.append((w,cap,win32gui.GetWindowRect(w["hwnd"])))
    left=min(0,min(rect[0] for _,_,rect in captures));top=min(0,min(rect[1] for _,_,rect in captures))
    right=max(rect[2] for _,_,rect in captures);bottom=max(rect[3] for _,_,rect in captures)
    if right-left>8192 or bottom-top>8192 or (right-left)*(bottom-top)>32_000_000:
        raise ValueError("Private desktop composition exceeds size limit")
    canvas=Image.new("RGB",(right-left,bottom-top),(32,35,40))
    for _,cap,rect in captures:
        with Image.open(cap['path']) as im:canvas.paste(im,(rect[0]-left,rect[1]-top))
    canvas.save(output_path,"PNG")
    return {"path":str(output_path),"mime_type":"image/png","width":canvas.width,"height":canvas.height,
            "size_bytes":output_path.stat().st_size,"capture_method":"PrintWindow private windows in Z order",
            "capture_scope":"private_desktop_window_composition","windows":[{"hwnd":w['hwnd'],"title":w['title']} for w,_,_ in captures],
            "host_input_used":False}
