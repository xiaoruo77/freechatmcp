"""Native document operations scoped to a private-desktop WPS window.

The caller is the private-desktop actor thread. NativeOM is obtained from that
window's document child; no global COM application lookup or focus/input occurs.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
from typing import Any


class WPSBackgroundError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def checked_output(source: Path, output: str, allowed_roots: list[Path]) -> Path:
    if not isinstance(output, str) or not Path(output).is_absolute():
        raise WPSBackgroundError("output_path_required", "A new absolute output path is required")
    destination = Path(output).resolve()
    if destination == source.resolve() or destination.exists():
        raise WPSBackgroundError("output_exists", "Save As never overwrites the source or an existing file")
    if destination.suffix.casefold() != ".docx" or not destination.parent.is_dir():
        raise WPSBackgroundError("output_path_denied", "Save As requires an existing directory and a new .docx file")
    if not any(destination.is_relative_to(root.resolve()) for root in allowed_roots):
        raise WPSBackgroundError("output_path_denied", "Output is outside the configured local roots")
    return destination


def replacement_plan(text: str, find: Any, replace: Any, expected_count: Any, expected_sha256: Any) -> tuple[int, str]:
    if not isinstance(find, str) or not find or not isinstance(replace, str):
        raise WPSBackgroundError("invalid_replacement", "A nonempty literal find string and replacement text are required")
    # Word-compatible Find interprets caret codes even with wildcards disabled.
    # Refuse those expressions instead of claiming an unsafe literal edit.
    if "^" in find or "^" in replace or any(ord(c) < 32 for c in find + replace):
        raise WPSBackgroundError("invalid_replacement", "Replacement expressions/control characters are not supported by the literal adapter")
    if max(len(find), len(replace)) > 40000 or type(expected_count) is not int or not 1 <= expected_count <= 1000:
        raise WPSBackgroundError("invalid_replacement", "Replacement size/count is outside the supported limit")
    if not isinstance(expected_sha256, str) or expected_sha256 != text_sha256(text):
        raise WPSBackgroundError("content_conflict", "Document text changed; read it again before editing")
    count = text.count(find)
    if count != expected_count:
        raise WPSBackgroundError("replacement_count_mismatch", "Literal match count differs from expected_count")
    return count, text.replace(find, replace)


def _desktop_name(handle) -> str:
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    user.GetUserObjectInformationW.restype = wintypes.BOOL
    buffer = ctypes.create_unicode_buffer(256)
    needed = wintypes.DWORD()
    if not user.GetUserObjectInformationW(handle, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
        raise WPSBackgroundError("desktop_unverified", "Cannot verify the actor's private desktop")
    return buffer.value


def _private_windows() -> tuple[str, set[int]]:
    if os.name != "nt":
        raise WPSBackgroundError("windows_required", "WPS background automation requires Windows")
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentThreadId.restype = wintypes.DWORD
    user.GetThreadDesktop.argtypes = [wintypes.DWORD]
    user.GetThreadDesktop.restype = wintypes.HANDLE
    handle = user.GetThreadDesktop(kernel.GetCurrentThreadId())
    name = _desktop_name(handle)
    if name != "Xiaoruo1Background":
        raise WPSBackgroundError("private_desktop_required", "WPS actions must run on the private desktop actor")
    roots = set()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def collect(hwnd, _):
        roots.add(int(hwnd))
        return True

    user.EnumDesktopWindows.argtypes = [wintypes.HANDLE, callback_type, wintypes.LPARAM]
    user.EnumDesktopWindows.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    if not user.EnumDesktopWindows(handle, collect, 0) and ctypes.get_last_error():
        raise WPSBackgroundError("desktop_unverified", "Cannot enumerate the private desktop")
    return name, roots


def _native_document(root: int, expected_pid: int, private_roots: set[int]):
    import win32gui
    import win32process
    import comtypes
    import comtypes.client.dynamic
    from comtypes.automation import IDispatch

    if root not in private_roots or win32process.GetWindowThreadProcessId(root)[1] != expected_pid:
        raise WPSBackgroundError("wps_target_mismatch", "The WPS root window changed or is outside the private desktop")
    children = []

    def collect(hwnd, _):
        if win32gui.GetClassName(hwnd) == "_WwG":
            children.append(hwnd)

    win32gui.EnumChildWindows(root, collect, None)
    if not children:
        raise WPSBackgroundError("wps_nativeom_unavailable", "This WPS window exposes no supported native document view")
    oleacc = ctypes.WinDLL("oleacc")
    oleacc.AccessibleObjectFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.POINTER(comtypes.GUID), ctypes.POINTER(ctypes.c_void_p)]
    oleacc.AccessibleObjectFromWindow.restype = ctypes.c_long
    iid = comtypes.GUID("{00020400-0000-0000-C000-000000000046}")
    candidates = []
    for child in children:
        ptr = ctypes.c_void_p()
        hr = oleacc.AccessibleObjectFromWindow(child, 0xFFFFFFF0, ctypes.byref(iid), ctypes.byref(ptr))
        if hr < 0 or not ptr.value:
            continue
        view = comtypes.client.dynamic.Dispatch(ctypes.cast(ptr, ctypes.POINTER(IDispatch)))
        try:
            view_hwnd = int(view.Hwnd)
            if win32gui.GetAncestor(view_hwnd, 2) != root:
                continue
            document = view.Document
            full_name = Path(str(document.FullName)).resolve()
            candidates.append((view, document, full_name, view_hwnd))
        except Exception:
            continue
    if len(candidates) != 1:
        raise WPSBackgroundError("wps_document_ambiguous", "The exact private WPS document could not be identified")
    return candidates[0]


def wps_action(action: str, args: dict[str, Any], windows: list[dict[str, Any]], data_root: Path) -> dict[str, Any]:
    """Read, literal-replace or Save As a document in an exact private WPS HWND.

    ``args`` always supplies hwnd and document_path; edits also supply the
    read result's text_sha256. All returns describe actual NativeOM results.
    ``save_as`` saves a new .docx and never overwrites the opened source.
    """
    if action not in {"status", "read", "replace_text", "save_as"}:
        raise WPSBackgroundError("wps_action_unsupported", "Only status/read/replace_text/save_as are supported")
    fields = {"hwnd", "document_path", "max_chars"} | ({"find", "replace", "expected_count", "expected_sha256"} if action == "replace_text" else set()) | ({"output_path", "expected_sha256"} if action == "save_as" else set())
    if not isinstance(args, dict) or set(args) - fields:
        raise WPSBackgroundError("wps_arguments_invalid", "Unsupported WPS arguments")
    max_chars = args.get("max_chars", 16000)
    if type(max_chars) is not int or not 1 <= max_chars <= 100000:
        raise WPSBackgroundError("wps_arguments_invalid", "max_chars must be from 1 to 100000")
    hwnd = args.get("hwnd")
    path = args.get("document_path")
    if type(hwnd) is not int or not isinstance(path, str) or not Path(path).is_absolute():
        raise WPSBackgroundError("wps_target_required", "An exact HWND and absolute document_path are required")
    source = Path(path).resolve(strict=True)
    if source.suffix.casefold() not in {".docx", ".doc", ".rtf", ".wps"}:
        raise WPSBackgroundError("wps_document_denied", "Only Writer documents are supported by this NativeOM adapter")
    targets = [window for window in windows if window.get("hwnd") == hwnd and window.get("desktop") == "Xiaoruo1Background"]
    if len(targets) != 1:
        raise WPSBackgroundError("wps_target_mismatch", "The selected WPS window is not in the private desktop inventory")
    desktop, roots = _private_windows()
    view, document, actual, view_hwnd = _native_document(hwnd, targets[0]["pid"], roots)
    if actual != source:
        raise WPSBackgroundError("wps_document_mismatch", "The native WPS document differs from document_path")
    text = str(document.Content.Text)
    if len(text) > 2_000_000:
        raise WPSBackgroundError("wps_document_too_large", "Document text exceeds the supported limit")
    if action == "replace_text":
        count, expected = replacement_plan(text, args.get("find"), args.get("replace"), args.get("expected_count"), args.get("expected_sha256"))
        selected = document.Content.Duplicate
        # Explicit literal matching; no Selection, window activation or keyboard.
        selected.Find.Execute(args["find"], True, False, False, False, False, True, 0, False, args["replace"], 2)
        text = str(document.Content.Text)
        if text != expected:
            raise WPSBackgroundError("wps_edit_verification_failed", "The document differs from the requested literal replacement; inspect without replaying")
    elif action == "save_as":
        if args.get("expected_sha256") != text_sha256(text):
            raise WPSBackgroundError("content_conflict", "Document text changed; read it again before Save As")
        roots_allowed = [source.parent, Path(data_root)]
        destination = checked_output(source, args.get("output_path"), roots_allowed)
        document.SaveAs2(str(destination), 12)  # wdFormatXMLDocument
        if Path(str(document.FullName)).resolve() != destination or not destination.is_file():
            raise WPSBackgroundError("wps_save_verification_failed", "WPS did not save the requested new output")
        actual = destination
    else:
        count = None
    result = {"ok": True, "action": action, "desktop": desktop, "hwnd": hwnd, "native_view_hwnd": view_hwnd,
              "document_path": str(actual), "text": text[:max_chars], "truncated": len(text) > max_chars,
              "text_sha256": text_sha256(text), "saved": bool(document.Saved), "api": "AccessibleObjectFromWindow/NativeOM",
              "foreground_input_used": False, "source_overwritten": False}
    if action == "replace_text":
        result["replacement_count"] = count
    return result
