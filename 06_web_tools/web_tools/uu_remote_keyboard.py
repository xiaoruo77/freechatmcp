from __future__ import annotations

"""Transient remote-user-session keyboard endpoint over an owned UU terminal.

The temporary endpoint captures and controls the other PC's real user desktop.
This transport never injects input on the controller computer and never logs
terminal output, device IDs, request text, or the helper source.
"""

import base64
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
import uuid
from urllib.parse import urlsplit
from collections import OrderedDict, deque

from .uu_conpty import PseudoConsoleProcess


_PROTOCOL = "xiaoruo1.uu.remote-input.v2"
_PREFIX = "XIAORUO1_UU_INPUT_V2|"
_REQUEST_PREFIX = "XIAORUO1_UU_REQUEST_V2|"
_BUFFER_LIMIT = 2 * 1024 * 1024
_FRAME_LIMIT = 65536
_WARMUP_SECONDS = 3.0
_ACK_SETTLE_SECONDS = .25
_BOOTSTRAP_CHUNK = 1024
_ACTIONS = frozenset({"status", "type", "key", "stop", "create_test_window", "fixture_state", "close_test_window",
                      "configure_capture", "capture", "click", "scroll", "drag"})
_EMPTY_ACTIONS = _ACTIONS - {"type", "key", "configure_capture", "capture", "click", "scroll", "drag"}
_KEYS = {"CTRL", "CONTROL", "ALT", "SHIFT", "ENTER", "RETURN", "TAB", "BACKSPACE", "ESC", "ESCAPE", "SPACE", "DELETE", "INSERT", "HOME", "END", "PAGEUP", "PAGEDOWN", "LEFT", "UP", "RIGHT", "DOWN"}
_KEYS.update(chr(value) for value in range(ord("A"), ord("Z") + 1))
_KEYS.update(str(value) for value in range(10))
_KEYS.update("F" + str(value) for value in range(1, 25))


class RemoteKeyboardError(RuntimeError):
    """Safe diagnostic; neither terminal bytes nor command arguments are kept."""

    def __init__(self, code: str, message: str, *, may_have_run: bool = False, request_id: str | None = None, stage: str | None = None):
        safe_stage = stage if isinstance(stage, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", stage) else None
        super().__init__(message + (f" (stage={safe_stage})" if safe_stage else ""))
        self.code = code
        self.may_have_run = may_have_run
        self.unknown_outcome = may_have_run
        self.request_id = request_id
        self.stage = safe_stage


class _TerminalFrames:
    """Reconstruct the UU terminal screen before decoding its output records.

    UU emits terminal screen differences, rather than an ordinary stdout byte
    stream. A marker may be assembled by cursor positioning and overwriting.
    Logical consumed text is intentionally independent of the current screen.
    """

    # Bootstrap ACKs are short ASCII records, separate from the checksummed V2
    # payload parser. A console repaint may put even the prefix across a row.
    _CONTROL_LIMIT = 512
    _CONTROL_PATTERN = re.compile(
        r"(?:XRUPROBE_[A-Za-z0-9_-]{32,128}\|[A-Za-z0-9_-]{1,63}\|[0-9]{1,10}"
        r"|XRUA_[A-Za-z0-9_-]{32,128}_[A-Za-z0-9_]{1,32}\|[0-9]{1,9}"
        r"|XRU_LOAD_[A-Za-z0-9_-]{32,128}\|0"
        r"|XRU_APPEND_[A-Za-z0-9_-]{32,128}_[0-9]{1,3}\|[0-9]{1,6}(?:\|[0-9a-fA-F]{64})?"
        r"|XRU_VERIFY_[A-Za-z0-9_-]{32,128}\|[0-9]{1,6}\|[0-9a-fA-F]{64}"
        r"|XRU_(?:NAK|FAIL)_[A-Za-z0-9_-]{32,128}\|[0-9]{1,3})\|END"
    )

    def __init__(self):
        self.text = ""
        self.frames: list[dict] = []
        self.dropped_bytes = 0
        self._screen = self._stream = None
        self._last_snapshot = ""
        self._seen_frames: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._row_cache: OrderedDict[int, tuple[object, str]] = OrderedDict()
        self._seen_contexts: set[str] = set()
        self._context_order: deque[str] = deque()

    def _ensure_screen(self) -> None:
        if self._stream is not None:
            return
        try:
            import pyte
        except ImportError:
            raise RemoteKeyboardError("uu_terminal_renderer_unavailable", "The pinned VT screen renderer is not installed") from None
        owner = self

        class CaptureScreen(pyte.HistoryScreen):
            # Printable runs only update pyte's dirty rows. Capture at pipe-read
            # boundaries, newlines, or just before destructive screen changes.
            def linefeed(self):
                super().linefeed()
                owner._capture_screen()

            def index(self):
                if owner._screen is not None and self.cursor.y == (self.margins.bottom if self.margins else self.lines - 1):
                    owner._capture_screen()
                super().index()

            def erase_in_line(self, *args, **kwargs):
                owner._capture_screen()
                super().erase_in_line(*args, **kwargs)

            def erase_in_display(self, *args, **kwargs):
                owner._capture_screen()
                super().erase_in_display(*args, **kwargs)

            def reset(self):
                owner._capture_screen()
                super().reset()

            def carriage_return(self):
                owner._capture_screen()
                super().carriage_return()

            def backspace(self):
                owner._capture_screen()
                super().backspace()

            def cursor_position(self, line=None, column=None):
                if owner._screen is not None and ((line or 1) - 1 < self.cursor.y or ((line or 1) - 1 == self.cursor.y and (column or 1) - 1 <= self.cursor.x)):
                    owner._capture_screen()
                super().cursor_position(line, column)

            def delete_lines(self, *args, **kwargs):
                owner._capture_screen()
                super().delete_lines(*args, **kwargs)

            def delete_characters(self, *args, **kwargs):
                owner._capture_screen()
                super().delete_characters(*args, **kwargs)

        # UU Windows 4.46 actually exposes a 160x52 remote console even when
        # the local ConPTY requests a wider buffer. Match its real geometry so
        # wrap/clear/cursor-diff updates cannot leave stale text in phantom cols.
        self._screen = CaptureScreen(160, 52, history=256)
        self._stream = pyte.Stream(self._screen)

    def _screen_rows(self) -> list[str]:
        if self._screen is None:
            return []

        dirty = set(self._screen.dirty)
        for index in dirty:
            line = self._screen.buffer.get(index)
            if line is not None:
                self._row_cache.pop(id(line), None)

        def render(line):
            if not line:
                return ""
            identity = id(line)
            cached = self._row_cache.get(identity)
            if cached is not None and cached[0] is line:
                self._row_cache.move_to_end(identity)
                return cached[1]
            # EL can leave thousands of explicit blank cells. Find the final
            # actual character and render only that content range once per edit.
            populated = {index: char.data for index, char in line.items()
                         if 0 <= index < self._screen.columns and char.data not in {"", " "}}
            last = max(populated, default=-1)
            value = "".join(populated.get(index, " ") if line.get(index) is None or line[index].data != "" else "" for index in range(last + 1))
            self._row_cache[identity] = (line, value)
            self._row_cache.move_to_end(identity)
            if len(self._row_cache) > 512:
                self._row_cache.popitem(last=False)
            return value

        rows = [render(line) for line in self._screen.history.top]
        rows.extend(render(self._screen.buffer.get(index, {})) for index in range(self._screen.lines))
        self._screen.dirty.clear()
        return rows

    def _snapshot(self) -> str:
        return "\n".join(self._screen_rows()).rstrip()

    def _control_contexts(self, rows: list[str]) -> list[str]:
        """Reassemble only complete protocol-shaped physical wrapped lines.

        An ordinary newline never authorizes concatenation: the preceding row
        must reach the terminal's right edge. Leading indentation is allowed,
        but prompts, quotes, command text and trailing error text invalidate
        the entire candidate, so a literal ACK in an echoed command is inert.
        Semantic nonce/index/length/hash checks remain with the caller.
        """
        columns = self._screen.columns
        contexts = []
        for index, row in enumerate(rows):
            candidate = row.lstrip(' ')
            # X or XR at the physical edge may be the first part of XRU.
            if not candidate.startswith('X'):
                continue
            end = index
            while len(candidate) <= self._CONTROL_LIMIT:
                if self._CONTROL_PATTERN.fullmatch(candidate):
                    contexts.append(candidate)
                    break
                if ('|END' in candidate or len(rows[end]) != columns
                        or end + 1 >= len(rows)):
                    break
                end += 1
                continuation = rows[end]
                if not continuation or re.fullmatch(r'[A-Za-z0-9_+/=|.-]+', continuation) is None:
                    break
                candidate += continuation
        return contexts

    def _capture_screen(self) -> None:
        if self._screen is None or not self._screen.dirty:
            return
        rows = self._screen_rows()
        candidates = self._control_contexts(rows)
        for index, line in enumerate(rows):
            if _PREFIX not in line:
                continue
            context = [line]
            # A base64 protocol frame can wrap onto subsequent screen rows.
            # Unchanged prefixes remain present while a suffix is repainted.
            if _PREFIX in line:
                for continuation in rows[index + 1:]:
                    if not continuation or re.fullmatch(r"[A-Za-z0-9+/=| ]+", continuation) is None:
                        break
                    context.append(continuation)
                    if '|END' in continuation or sum(map(len,context))>_FRAME_LIMIT+128:break
            candidates.append("\n".join(context))
        contexts = []
        for value in candidates:
            fingerprint = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
            if fingerprint in self._seen_contexts:
                continue
            self._seen_contexts.add(fingerprint)
            self._context_order.append(fingerprint)
            if len(self._context_order) > 2048:
                self._seen_contexts.discard(self._context_order.popleft())
            contexts.append(value)
        if contexts:
            # Consume only newly rendered protocol-bearing contexts. Repeatedly
            # copying every old screen row made one small ACK quadratic in the
            # accumulated terminal transcript.
            self.text += "\n" + "\n".join(contexts) + "\n"
            encoded = self.text.encode("utf-8", errors="replace")
            if len(encoded) > _BUFFER_LIMIT:
                retained = encoded[-_BUFFER_LIMIT:].decode("utf-8", errors="ignore")
                self.dropped_bytes += len(encoded) - len(retained.encode("utf-8"))
                self.text = retained
            self._parse()

    @property
    def screen_text(self) -> str:
        return self._snapshot()

    def clear(self) -> None:
        """Release the renderer and text immediately, including its callbacks."""
        self._stream = self._screen = None
        self._last_snapshot = self.text = ""
        self.frames.clear()
        self._seen_frames.clear()
        self._seen_order.clear()
        self._row_cache.clear()
        self._seen_contexts.clear()
        self._context_order.clear()

    def feed(self, raw: str) -> None:
        if not isinstance(raw, str):
            raise RemoteKeyboardError("uu_terminal_output_invalid", "The local terminal returned invalid output")
        if not raw:
            return
        self._ensure_screen()
        try:
            self._stream.feed(raw)
            self._capture_screen()
        except RemoteKeyboardError:
            raise
        except Exception:
            raise RemoteKeyboardError("uu_terminal_render_failed", "The terminal screen update could not be reconstructed") from None

    def _parse(self) -> None:
        while True:
            start = self.text.find(_PREFIX)
            if start < 0:
                return
            tail = self.text[start + len(_PREFIX):]
            match = re.match(r"([A-Za-z0-9+/=\r\n\t ]+)\|\s*([0-9a-fA-F\r\n\t ]+)\|\s*E\s*N\s*D",tail)
            if match is None:
                following=self.text.find(_PREFIX,start+len(_PREFIX))
                if following>=0:
                    self.text=self.text[:start]+self.text[following:]
                    continue
                if len(tail)>_FRAME_LIMIT+128:
                    self.text=self.text[:start]+self.text[start+len(_PREFIX):]
                    continue
                return
            compact=re.sub(r"\s","",match.group(1))
            if len(compact)>_FRAME_LIMIT:
                self.text=self.text[:start]+self.text[start+len(_PREFIX)+match.end():]
                continue
            try:
                decoded=base64.b64decode(compact,validate=True)
                if hashlib.sha256(decoded).hexdigest()!=re.sub(r'\s','',match.group(2)).casefold():
                    raise ValueError('incomplete screen repaint')
                payload=json.loads(decoded.decode('utf-8'))
            except (ValueError, UnicodeError):
                self.text=self.text[:start]+self.text[start+len(_PREFIX)+match.end():]
                continue
            self.text=self.text[:start]+self.text[start+len(_PREFIX)+match.end():]
            if isinstance(payload, dict):
                fingerprint = hashlib.sha256(decoded).hexdigest()
                if fingerprint not in self._seen_frames:
                    self._seen_frames.add(fingerprint)
                    self._seen_order.append(fingerprint)
                    if len(self._seen_order) > 512:
                        self._seen_frames.discard(self._seen_order.popleft())
                    self.frames.append(payload)
                    if len(self.frames) > 64:
                        del self.frames[:-64]


def _positive(value, maximum=9223372036854775807) -> bool:
    return type(value) is int and 0 < value <= maximum


def _timeout(value: float) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be finite and positive")
    return float(value)


class RemoteKeyboardSession:
    """Own a uniquely named UU terminal and an in-memory fixed-action helper."""

    def __init__(self, cli, device_id: str, *, process_factory=None, cli_runner=None, state_root=None):
        self.cli = Path(cli)
        if not isinstance(device_id, str) or not device_id or "\0" in device_id or len(device_id) > 512:
            raise ValueError("A bounded device identifier is required")
        self._device_id = device_id
        self._process_factory = process_factory or PseudoConsoleProcess
        self._cli_runner = cli_runner or subprocess.run
        self._process = None
        # Match the short alphanumeric/underscore naming form accepted by the
        # installed UU terminal CLI. Do not infer an undocumented maximum.
        self._name = "xru_input_" + uuid.uuid4().hex[:16]
        self._nonce = uuid.uuid4().hex
        self._probe_nonce = uuid.uuid4().hex
        self._terminal = _TerminalFrames()
        self._lock = threading.RLock()
        self._attempted = False
        self._ready: dict | None = None
        self._closed = False
        self._cleanup_result: dict | None = None
        self._requests: dict[str, dict] = {}
        self._fixture_open = False
        self._registry=None
        self._registry_record=None
        if state_root is not None:
            from .uu_session_registry import UUSessionRegistry
            self._registry=UUSessionRegistry(Path(state_root))
        self._stop_reply: dict | None = None
        self._stopped_frame: dict | None = None
        self.stage = "idle"

    def _management(self, action: str, timeout: float = 5, terminal_name=None) -> dict:
        name=self._name if terminal_name is None else terminal_name
        if action not in {'has','kill'} or not isinstance(name,str) or not re.fullmatch(r'xru_input_[0-9a-f]{16}',name):
            raise RemoteKeyboardError('uu_terminal_management_invalid','Only an exact owned terminal can be inspected or closed')
        for attempt in range(3 if action=='has' else 1):
            try:
                completed = self._cli_runner(
                    [str(self.cli), "term", "-d", self._device_id, action, name, "--json"],
                    cwd=str(self.cli.parent), stdin=subprocess.DEVNULL, capture_output=True,
                    text=True, encoding="utf-8", errors="replace", timeout=max(0.1, min(10, timeout)),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if action=='has' and completed.returncode==105 and attempt<2:
                    # UU's cold terminal subsystem can reject its initial
                    # read before the peer channel is ready. Same read only;
                    # do not repeat a mutating terminal operation here.
                    time.sleep(.75*(attempt+1))
                    continue
                break
            except Exception as exc:
                # Cold UU terminal discovery can outlive its first RPC. Only
                # retry this read; a kill with an unknown result is reconciled
                # by an exact existence check instead of blindly repeated.
                if action=='has' and attempt<2 and isinstance(exc,(subprocess.TimeoutExpired,TimeoutError)):
                    time.sleep(.25)
                    continue
                raise RemoteKeyboardError("uu_terminal_management_failed",
                    "The owned UU terminal management command failed ("+type(exc).__name__+")",stage=self.stage) from None
        payload = None
        for stream in (completed.stdout, completed.stderr):
            if not isinstance(stream, str) or len(stream) > _FRAME_LIMIT:
                continue
            try:
                decoded = json.loads(stream)
            except ValueError:
                continue
            if isinstance(decoded, dict):
                payload = decoded
                break
        missing_terminal = (
            action == "has" and completed.returncode == 1 and isinstance(payload, dict)
            and payload.get("success") is True and isinstance(payload.get("data"), dict)
            and payload["data"].get("exists") is False
        )
        if (completed.returncode != 0 and not missing_terminal) or payload is None or payload.get("success") is not True:
            error=payload.get('error') if isinstance(payload,dict) else None
            code=error.get('code') if isinstance(error,dict) else None
            safe=str(code) if type(code) is int or (isinstance(code,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,60}',code)) else 'unknown'
            raise RemoteKeyboardError("uu_terminal_management_rejected",
                "UU rejected terminal "+action+" (exit="+str(completed.returncode)+", code="+safe+")",stage=self.stage)
        return payload

    def _exists(self, timeout: float = 5, terminal_name=None) -> bool:
        payload = self._management("has", timeout,terminal_name)
        data = payload.get("data")
        if not isinstance(data, dict) or type(data.get("exists")) is not bool:
            raise RemoteKeyboardError("uu_terminal_state_invalid", "UU did not return an exact terminal existence result")
        return data["exists"]

    def _cleanup_orphans(self):
        if self._registry is None:return
        self.stage='orphan_cleanup'
        for record in self._registry.orphan_candidates(self._device_id):
            if not self._registry.is_orphan(record):continue
            if self._exists(terminal_name=record.terminal_name):
                if not self._registry.is_orphan(record):continue
                self._management('kill',terminal_name=record.terminal_name)
            if self._exists(terminal_name=record.terminal_name):
                raise RemoteKeyboardError('uu_orphan_cleanup_pending','An earlier owned terminal is still present')
            self._registry.forget(record)

    def _drain(self) -> None:
        if self._process is None:
            return
        try:
            output = self._process.read_available()
            self._terminal.feed(output)
        except RemoteKeyboardError:
            raise
        except Exception:
            raise RemoteKeyboardError("uu_terminal_read_failed", "The owned UU terminal output channel failed") from None

    def _write(self, value: str, *, request_id: str | None = None) -> None:
        try:
            writer=getattr(self._process,'write_paced',self._process.write)
            writer(value)
        except Exception:
            raise RemoteKeyboardError("uu_terminal_write_unknown", "The terminal write outcome is unknown; it was not retried", may_have_run=True, request_id=request_id) from None

    def _wait(self, predicate, deadline: float, *, request_id=None, may_have_run=False):
        while True:
            try:
                self._drain()
            except RemoteKeyboardError as exc:
                raise RemoteKeyboardError(exc.code, str(exc), may_have_run=may_have_run, request_id=request_id, stage=self.stage) from None
            value = predicate()
            if value is not None:
                # The real UU console drops a following line's leading bytes
                # while it switches ReadConsole back into input mode. A short
                # post-ACK settling interval passed continuous mixed-ASCII
                # transfer tests; integrity checks remain mandatory.
                time.sleep(_ACK_SETTLE_SECONDS)
                return value
            if time.monotonic() >= deadline:
                raise RemoteKeyboardError("uu_remote_input_timeout", "The remote input acknowledgement was not received; no automatic retry", may_have_run=may_have_run, request_id=request_id, stage=self.stage)
            try:
                if self._process.poll() is not None:
                    # One last drain before treating local attach exit as loss.
                    self._drain()
                    value = predicate()
                    if value is not None:
                        time.sleep(_ACK_SETTLE_SECONDS)
                        return value
                    raise RemoteKeyboardError("uu_terminal_exited", "The local UU terminal channel exited", may_have_run=may_have_run, request_id=request_id, stage=self.stage)
            except RemoteKeyboardError:
                raise
            except Exception:
                raise RemoteKeyboardError("uu_terminal_poll_failed", "The local UU terminal state could not be checked", may_have_run=may_have_run, request_id=request_id, stage=self.stage) from None
            time.sleep(min(0.02, max(0, deadline - time.monotonic())))

    def _frame(self, reqid: str, action: str) -> dict | None:
        for index, frame in enumerate(self._terminal.frames):
            if frame.get("protocol") != _PROTOCOL or frame.get("nonce") != self._nonce:
                continue
            if frame.get("reqid") != reqid:
                continue
            if frame.get("action") != action:
                # Unsolicited ready/stopped/protocol-error events intentionally
                # share the empty request ID. Select their action as well;
                # an uncorrelated rejected input line is not a STOP receipt.
                if not reqid:continue
                raise RemoteKeyboardError("uu_remote_protocol_mismatch", "A remote acknowledgement did not match the requested action", may_have_run=bool(reqid), request_id=reqid or None)
            if type(frame.get("ok")) is not bool:
                raise RemoteKeyboardError("uu_remote_protocol_invalid", "A remote acknowledgement had an invalid schema", may_have_run=bool(reqid), request_id=reqid or None)
            if frame["ok"] and not isinstance(frame.get("result"), dict):
                raise RemoteKeyboardError("uu_remote_protocol_invalid", "A remote acknowledgement had an invalid result", may_have_run=bool(reqid), request_id=reqid or None)
            if not frame["ok"]:
                error = frame.get("error")
                code = error.get("code") if isinstance(error, dict) else None
                # Preserve existing alphabetic helper errors. Numeric HTTP
                # diagnostics are admitted only from this fixed upload list.
                fixed_upload_codes = {
                    'capture_upload_http_401', 'capture_upload_http_403', 'capture_upload_http_404',
                    'capture_upload_http_409', 'capture_upload_http_413', 'capture_upload_http_415',
                    'capture_upload_http_429', 'capture_upload_http_500', 'capture_upload_http_502',
                    'capture_upload_http_503', 'capture_upload_http_504',
                }
                if (not isinstance(code, str) or not (re.fullmatch(r"[a-z_]{1,80}", code)
                                                     or code in fixed_upload_codes)):
                    raise RemoteKeyboardError("uu_remote_protocol_invalid", "A remote acknowledgement had an invalid error", may_have_run=bool(reqid), request_id=reqid or None)
                frame = {**frame, "error": {"code": code, "message": "The remote GUI input helper rejected this operation"}}
            del self._terminal.frames[index]
            return frame
        return None

    def _observe_stopped(self) -> dict | None:
        if self._stopped_frame is None:
            frame = self._frame("", "stopped")
            if frame is not None:
                if frame["ok"] is not True or frame["result"].get("reason") not in {"stop", "idle_timeout", "stdin_closed"}:
                    raise RemoteKeyboardError("uu_remote_protocol_invalid", "The helper exit acknowledgement had an invalid schema")
                self._stopped_frame = frame
        return self._stopped_frame

    def _console_ack(self, command: str, tag: str, stage: str, deadline: float) -> None:
        """Run one short bootstrap expression and require its computed ACK."""
        self.stage = stage
        prefix = "XRUA_" + self._nonce + "_" + tag + "|"
        line = command + ";[Console]::WriteLine('" + prefix + "'+1+'|END')\r"
        if len(line) > 200:
            raise RemoteKeyboardError("uu_remote_helper_invalid", "A helper preparation line exceeds its bounded length", stage=stage)
        self._write(line)
        pattern = re.compile(re.escape(prefix) + r"([0-9]+)\|END")

        def find_ack():
            match = pattern.search(self._terminal.text)
            if match is None:
                return None
            self._terminal.text = self._terminal.text[match.end():]
            if match.group(1) != "1":
                raise RemoteKeyboardError("uu_bootstrap_integrity_failed", "A helper preparation acknowledgement was invalid; no input action was sent", stage=stage)
            return True

        self._wait(find_ack, deadline)
        self._wait_shell_prompt(deadline)

    def _wait_shell_prompt(self,deadline):
        # Console.WriteLine can arrive before PowerShell finishes its command
        # and flushes/reinitializes ReadLine. Sending the next prefix during
        # that transition loses characters even though the previous ACK is real.
        def ready():
            renderer=self._terminal
            if renderer._screen is None:return None
            rows=renderer._screen_rows()
            index=len(renderer._screen.history.top)+renderer._screen.cursor.y
            line=rows[index] if index<len(rows) else ''
            return True if re.fullmatch(r'PS\s*[^>\r\n]{1,256}>\s*',line) else None
        self._wait(ready,deadline)

    def _helper_source(self) -> str:
        """Return the fixed helper; specialized transports may wrap its source."""
        return Path(__file__).with_name("uu_remote_input.ps1").read_text(encoding="utf-8-sig")

    def _bootstrap_chunk_size(self) -> int:
        return _BOOTSTRAP_CHUNK

    def _bootstrap_attempts(self) -> int:
        return 3

    def start(self, timeout: float = 45) -> dict:
        deadline = time.monotonic() + _timeout(timeout)
        with self._lock:
            if self._closed:
                raise RemoteKeyboardError("uu_remote_input_closed", "This remote input session has been closed")
            if self._ready is not None:
                return dict(self._ready)
            if self._attempted:
                raise RemoteKeyboardError("uu_remote_start_unknown", "This terminal creation was already attempted; close it before creating another")
            chunk_size = self._bootstrap_chunk_size()
            attempts = self._bootstrap_attempts()
            if type(chunk_size) is not int or not 1 <= chunk_size <= 1024 or type(attempts) is not int or not 1 <= attempts <= 6:
                raise RemoteKeyboardError("uu_remote_bootstrap_options_invalid", "Choose bounded source-chunk settings before opening a terminal")
            self.stage = "term_create"
            self._cleanup_orphans()
            self.stage = "term_create"
            source = self._helper_source()
            if len(source.encode("utf-8")) > 131072:
                raise RemoteKeyboardError("uu_remote_helper_invalid", "The fixed remote helper exceeds its bounded source size")
            if self._exists(min(10, max(0.1, deadline - time.monotonic()))):
                raise RemoteKeyboardError("uu_terminal_name_conflict", "The random terminal name already exists; it will not be reused or killed")
            if self._registry is not None:
                self._registry_record=self._registry.register(self._device_id,self._name)
            self._attempted = True
            try:
                self._process = self._process_factory(
                    [str(self.cli), "term", "-d", self._device_id, "new", self._name, "--shell", "powershell"],
                    cwd=str(self.cli.parent), columns=160, rows=52,
                )
            except Exception:
                raise RemoteKeyboardError("uu_terminal_start_unknown", "The owned UU terminal startup outcome is unknown; close is required", may_have_run=True) from None
            self.stage = "probe"
            warmup = min(_WARMUP_SECONDS, max(0, deadline - time.monotonic()))
            until = time.monotonic() + warmup
            while time.monotonic() < until:
                self._drain()
                time.sleep(min(0.05, max(0, until - time.monotonic())))
            probe_prefix = "XRUPROBE_" + self._probe_nonce + "|"
            probe = "[Console]::WriteLine('" + probe_prefix + "'+[Environment]::MachineName+'|'+[System.Diagnostics.Process]::GetCurrentProcess().SessionId+'|END')\r"
            self._write(probe)
            pattern = re.compile(re.escape(probe_prefix) + r"([A-Za-z0-9_-]+)\|([0-9]+)\|END")

            def find_probe():
                match = pattern.search(self._terminal.text)
                if match is None:
                    return None
                self._terminal.text = self._terminal.text[match.end():]
                return match.group(1), int(match.group(2))

            computer, session_id = self._wait(find_probe, deadline)
            self._wait_shell_prompt(deadline)
            local = {socket.gethostname().casefold(), os.environ.get("COMPUTERNAME", "").casefold()}
            local.update(value.split(".", 1)[0] for value in tuple(local))
            if computer.casefold() in local or not _positive(session_id, 2147483647):
                raise RemoteKeyboardError("uu_remote_identity_rejected", "The probe did not identify a different computer's interactive user session")
            compressed = base64.b64encode(gzip.compress(source.encode("utf-8"), compresslevel=9, mtime=0)).decode("ascii")
            chunks=[compressed[offset:offset+chunk_size] for offset in range(0,len(compressed),chunk_size)]
            expected_hash=hashlib.sha256(compressed.encode('ascii')).hexdigest()
            load_prefix='XRU_LOAD_'+self._nonce+'|'
            append_prefix='XRU_APPEND_'+self._nonce+'_'
            verify_prefix='XRU_VERIFY_'+self._nonce+'|'
            fail_prefix='XRU_FAIL_'+self._nonce+'|'
            from .uu_bootstrap_protocol import build_loader,chunk_wire
            program=build_loader(self._nonce,computer,len(chunks),len(compressed),expected_hash)
            packed=base64.b64encode(gzip.compress(program.encode('utf-8'),mtime=0)).decode('ascii')
            launcher=("$xrB=[IO.MemoryStream]::new([Convert]::FromBase64String('"+packed+"'));"
                "$xrZ=[IO.Compression.GZipStream]::new($xrB,[IO.Compression.CompressionMode]::Decompress);"
                "$xrQ=[IO.StreamReader]::new($xrZ,[Text.Encoding]::UTF8);"
                "&([ScriptBlock]::Create($xrQ.ReadToEnd()))")
            if len(launcher)>4000:
                raise RemoteKeyboardError('uu_remote_helper_invalid','The fixed bootstrap command exceeds its bounded console line')
            self.stage='bootstrap_reader'
            self._write(launcher+'\r')
            load_pattern=re.compile(re.escape(load_prefix)+r'0\|END')
            self._wait(lambda: True if load_pattern.search(self._terminal.text) else None,min(deadline,time.monotonic()+15))
            verify_pattern=re.compile(re.escape(verify_prefix)+r'([0-9]+)\|([0-9a-fA-F]{64})\|END')
            def source_verified():
                for match in verify_pattern.finditer(self._terminal.text):
                    if int(match.group(1))==len(compressed) and match.group(2).casefold()==expected_hash:return True
                return any(frame.get('protocol')==_PROTOCOL and frame.get('nonce')==self._nonce
                    and frame.get('reqid')=='' and frame.get('action')=='ready' for frame in self._terminal.frames)
            for index,chunk in enumerate(chunks):
                self.stage='bootstrap_chunk_'+str(index)
                wire=chunk_wire(self._nonce,index,chunk)+'\r'
                expected_length=min((index+1)*chunk_size,len(compressed))
                digest=hashlib.sha256(chunk.encode('ascii')).hexdigest()
                ack_pattern=re.compile(re.escape(append_prefix+str(index)+'|')+r'([0-9]+)\|([0-9a-f]{64})\|END')
                nak_pattern=re.compile(re.escape('XRU_NAK_'+self._nonce+'|')+str(index)+r'\|END')
                def accepted():
                    for match in ack_pattern.finditer(self._terminal.text):
                        if int(match.group(1))==expected_length and match.group(2)==digest:
                            self._terminal.text=self._terminal.text[match.end():]
                            return True
                    if index==len(chunks)-1 and source_verified():return True
                    nak=nak_pattern.search(self._terminal.text)
                    if nak:
                        self._terminal.text=self._terminal.text[nak.end():]
                        return 'retry'
                    return None
                # Only this exact nonce/index/hash source wire is retransmitted.
                # Receiver-side receipts append it once; the complete source
                # executes only after its one full-length/SHA256 verification.
                for attempt in range(attempts):
                    self._write(wire)
                    try:result=self._wait(accepted,min(deadline,time.monotonic()+3),may_have_run=False)
                    except RemoteKeyboardError as exc:
                        if exc.code!='uu_remote_input_timeout':raise
                        result='retry'
                    if result is True:break
                    if attempt==attempts-1 or time.monotonic()>=deadline:
                        raise RemoteKeyboardError('uu_bootstrap_integrity_failed',
                            'The source chunk was not acknowledged after bounded idempotent retries; no GUI input was sent',stage=self.stage)
                    time.sleep(_ACK_SETTLE_SECONDS)
            self.stage='verify'
            verify_pattern=re.compile(re.escape(verify_prefix)+r'([0-9]+)\|([0-9a-fA-F]{64})\|END')
            def find_integrity():
                if source_verified():return True
                if re.search(re.escape(fail_prefix)+r'1\|END',self._terminal.text):
                    raise RemoteKeyboardError('uu_bootstrap_integrity_failed','The remote bootstrap checksum did not match; no helper source was executed',stage=self.stage)
                for match in verify_pattern.finditer(self._terminal.text):
                    if int(match.group(1))==len(compressed) and match.group(2).casefold()==expected_hash:
                        self._terminal.text=self._terminal.text[match.end():]
                        return True
                return None
            self._wait(find_integrity,deadline)
            self.stage='helper_ready'
            frame = self._wait(lambda: self._frame("", "ready"), deadline)
            if frame.get("ok") is not True:
                raise RemoteKeyboardError(frame.get('error',{}).get('code','uu_remote_helper_rejected'),
                    "The fixed remote helper could not initialize",stage=self.stage)
            ready = frame["result"]
            if (ready.get("computer") != computer or ready.get("session_id") != session_id
                    or ready.get("active_session_id") != session_id
                    or str(ready.get("window_station", "")).casefold() != "winsta0"
                    or str(ready.get("thread_desktop", "")).casefold() != "default"
                    or str(ready.get("input_desktop", "")).casefold() != "default"
                    or not _positive(ready.get("helper_pid"), 2147483647)
                    or not re.fullmatch(r"[1-9][0-9]{0,18}", str(ready.get("helper_creation_time_utc_ticks", "")))):
                raise RemoteKeyboardError("uu_remote_runtime_rejected", "The helper did not prove the expected remote interactive desktop")
            self._ready = dict(ready)
            self.stage = "ready"
            return dict(ready)

    @staticmethod
    def _validate(action: str, args: dict) -> None:
        if not isinstance(action, str) or action not in _ACTIONS or not isinstance(args, dict):
            raise RemoteKeyboardError("uu_remote_action_invalid", "Choose one fixed remote input action and an arguments object")
        if action in _EMPTY_ACTIONS:
            if args:
                raise RemoteKeyboardError("uu_remote_arguments_invalid", "This remote input action accepts no arguments")
            return
        if action == 'configure_capture':
            origin = args.get('origin')
            parsed = urlsplit(origin) if isinstance(origin, str) else None
            if (set(args) != {'origin'} or parsed is None or parsed.scheme != 'https'
                    or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path or parsed.query or parsed.fragment or len(origin) > 512):
                raise RemoteKeyboardError('uu_remote_arguments_invalid', 'A fixed HTTPS capture origin is required')
            return
        if action == 'capture':
            identity = args.get('capture_id')
            url, token = args.get('upload_url'), args.get('token')
            parsed = urlsplit(url) if isinstance(url, str) else None
            if (set(args) != {'capture_id', 'upload_url', 'token'} or not isinstance(identity, str)
                    or not re.fullmatch(r'[a-f0-9]{32}', identity) or parsed is None
                    or parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or len(url) > 1024
                    or parsed.path != '/remote-gui/captures/' + identity
                    or not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', token)):
                raise RemoteKeyboardError('uu_remote_arguments_invalid', 'A bounded one-use PNG upload ticket is required')
            return
        if action in {'click', 'scroll', 'drag'}:
            fields = {'click': {'button', 'count'}, 'scroll': {'vertical', 'horizontal'},
                      'drag': {'to_x', 'to_y', 'duration_ms'}}[action]
            bounds = args.get('bounds')
            if (set(args) != {'x', 'y', 'bounds'} | fields or not isinstance(bounds, dict)
                    or set(bounds) != {'left', 'top', 'width', 'height'}
                    or any(type(bounds[k]) is not int for k in bounds)
                    or not 1 <= bounds['width'] <= 8192 or not 1 <= bounds['height'] <= 8192
                    or bounds['width'] * bounds['height'] > 32_000_000
                    or not -32768 <= bounds['left'] <= 32767 or not -32768 <= bounds['top'] <= 32767):
                raise RemoteKeyboardError('uu_remote_arguments_invalid', 'Bounded physical remote screenshot coordinates are required')
            for x, y in [(args.get('x'), args.get('y'))] + ([(args.get('to_x'), args.get('to_y'))] if action == 'drag' else []):
                if (type(x) is not int or type(y) is not int
                        or not bounds['left'] <= x < bounds['left'] + bounds['width']
                        or not bounds['top'] <= y < bounds['top'] + bounds['height']):
                    raise RemoteKeyboardError('uu_remote_arguments_invalid', 'Remote pointer coordinates are outside the captured screen')
            if action == 'click' and (not isinstance(args['button'], str) or args['button'] not in {'left','right','middle'} or type(args['count']) is not int or args['count'] not in {1,2}):
                raise RemoteKeyboardError('uu_remote_arguments_invalid', 'Use a supported mouse button and count')
            if action == 'scroll' and (any(type(args[k]) is not int or not -12000 <= args[k] <= 12000 for k in fields) or not (args['vertical'] or args['horizontal'])):
                raise RemoteKeyboardError('uu_remote_arguments_invalid', 'Remote wheel deltas must be bounded integers')
            if action == 'drag' and (type(args['duration_ms']) is not int or not 50 <= args['duration_ms'] <= 5000):
                raise RemoteKeyboardError('uu_remote_arguments_invalid', 'Drag duration must be 50-5000 milliseconds')
            return
        main = "text" if action == "type" else "keys"
        if set(args) - {main, "expect", "activate"} or not {main, "expect"} <= set(args):
            raise RemoteKeyboardError("uu_remote_arguments_invalid", "The remote input arguments did not match the fixed schema")
        expected = args.get("expect")
        if (not isinstance(expected, dict) or set(expected) != {"hwnd", "pid", "creation_time_utc_ticks"}
                or not _positive(expected.get("hwnd")) or not _positive(expected.get("pid"), 2147483647)
                or not isinstance(expected.get("creation_time_utc_ticks"), str)
                or not re.fullmatch(r"[1-9][0-9]{0,18}", expected["creation_time_utc_ticks"])
                or int(expected["creation_time_utc_ticks"]) > 9223372036854775807
                or ("activate" in args and type(args["activate"]) is not bool)):
            raise RemoteKeyboardError("uu_remote_target_invalid", "An exact positive remote window/process identity is required")
        if action == "type":
            text = args["text"]
            if not isinstance(text, str) or not 1 <= len(text) <= 1000 or "\0" in text:
                raise RemoteKeyboardError("uu_remote_text_invalid", "Text must contain 1-1000 non-NUL characters")
            try:
                text.encode("utf-8")
            except UnicodeError:
                raise RemoteKeyboardError("uu_remote_text_invalid", "Text contains an invalid Unicode sequence") from None
        else:
            keys = args["keys"].split("+") if isinstance(args["keys"], str) else args["keys"]
            if not isinstance(keys, list) or not 1 <= len(keys) <= 5 or any(not isinstance(key, str) for key in keys):
                raise RemoteKeyboardError("uu_remote_keys_invalid", "Use one fixed chord containing 1-5 supported keys")
            normalized = [key.strip().upper() for key in keys]
            canonical = [{"CONTROL": "CTRL", "RETURN": "ENTER", "ESCAPE": "ESC"}.get(key, key) for key in normalized]
            if any(key not in _KEYS for key in normalized) or len(set(canonical)) != len(canonical) or {"CTRL", "ALT", "DELETE"} <= set(canonical):
                raise RemoteKeyboardError("uu_remote_keys_invalid", "Unsupported, duplicate, or secure-attention keys are not accepted")

    def request(self, action: str, args: dict, request_id: str | None = None, timeout: float = 10) -> dict:
        deadline = time.monotonic() + _timeout(timeout)
        with self._lock:
            if self._closed or self._ready is None:
                raise RemoteKeyboardError("uu_remote_input_not_ready", "Start an owned remote input session before making requests")
            self._validate(action, args)
            self.stage = "request_" + action
            reqid = uuid.uuid4().hex if request_id is None else request_id
            if not isinstance(reqid, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", reqid):
                raise RemoteKeyboardError("uu_remote_request_id_invalid", "Request IDs must use 1-80 supported characters")
            canonical = json.dumps({"action": action, "arguments": args}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            record = self._requests.get(reqid)
            if record is not None and record["digest"] != digest:
                raise RemoteKeyboardError("uu_remote_request_id_conflict", "This request ID was already used for different arguments", request_id=reqid)
            if record is not None and record.get("reply") is not None:
                return json.loads(json.dumps(record["reply"]))
            if record is None:
                # After helper exit the terminal is a PowerShell shell again.
                # Never send a request line into that shell or implicitly restart.
                self._drain()
                if self._observe_stopped() is not None or self._stop_reply is not None:
                    raise RemoteKeyboardError("uu_remote_helper_stopped", "The fixed remote input helper has ended; close this terminal")
                if len(self._requests) >= (257 if action=='stop' else 256):
                    raise RemoteKeyboardError("uu_remote_request_limit", "Close this bounded session before issuing more requests")
                payload = {"nonce": self._nonce, "reqid": reqid, "action": action, "arguments": args}
                data=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf-8')
                encoded=base64.b64encode(data).decode('ascii')
                wire=_REQUEST_PREFIX+encoded+'|'+hashlib.sha256(data).hexdigest()+'|END'
                if len(wire) > 7600:
                    raise RemoteKeyboardError("uu_remote_request_too_large", "The request exceeds the bounded terminal input line")
                record = {"digest": digest, "reply": None, "action": action,
                          "wire":wire+"\r","attempts":1}
                # Record before writing: a failed write may have reached the
                # remote helper. Same-ID calls only poll, never resend input.
                self._requests[reqid] = record
                if action == "create_test_window":
                    self._fixture_open = True
                self._write(record['wire'], request_id=reqid)
            while True:
                retryable=record.get('attempts',1)<3 and bool(record.get('wire'))
                until=min(deadline,time.monotonic()+2.5) if retryable else deadline
                try:
                    reply=self._wait(lambda:self._frame(reqid,action),until,request_id=reqid,may_have_run=True)
                    break
                except RemoteKeyboardError as exc:
                    if exc.code!='uu_remote_input_timeout' or not retryable or time.monotonic()>=deadline:
                        raise
                    self._drain()
                    if self._observe_stopped() is not None or self._process.poll() is not None:
                        raise
                    # The remote endpoint reserves this nonce+request ID before
                    # input and caches its terminal receipt. Re-send exactly the
                    # same checksummed wire, never a fresh ID or a new endpoint.
                    record['attempts']=record.get('attempts',1)+1
                    time.sleep(_ACK_SETTLE_SECONDS)
                    self._write(record['wire'],request_id=reqid)
            record["reply"] = reply
            if reply["ok"]:
                if action == "create_test_window" and reply["result"].get("exists") is True:
                    self._fixture_open = True
                elif action == "close_test_window" and reply["result"].get("closed") is True:
                    self._fixture_open = False
                elif action == "fixture_state" and reply["result"].get("exists") is False:
                    self._fixture_open = False
                elif action == "stop":
                    self._stop_reply = reply
            return json.loads(json.dumps(reply))

    def close(self) -> dict:
        with self._lock:
            if self._cleanup_result is not None and self._cleanup_result.get("cleanup_complete"):
                return dict(self._cleanup_result)
            previous = self._cleanup_result or {}
            self.stage = "close"
            result = {"cleanup_complete": False, "stop_acknowledged": previous.get("stop_acknowledged", False),
                      "helper_stopped_observed": previous.get("helper_stopped_observed", False),
                      "remote_terminal_absent": not self._attempted, "local_channel_closed": self._process is None}
            errors = []
            if self._ready is not None and self._process is not None and not self._closed:
                try:
                    self._drain()
                    frame = self._observe_stopped()
                    stopped = self._stop_reply
                    if frame is None and stopped is None:
                        stopped = self.request("stop", {}, request_id="close-" + self._nonce, timeout=5)
                    result["stop_acknowledged"] = stopped is not None and stopped.get("ok") is True
                    if frame is None:
                        frame = self._wait(self._observe_stopped, time.monotonic() + 3)
                    result["helper_stopped_observed"] = True
                    result["remote_helper_exit_reason"] = frame["result"]["reason"]
                    if self._fixture_open:
                        self._fixture_open = not ((stopped or {}).get("result", {}).get("fixture_closed") is True or frame["result"].get("fixture_closed") is True)
                except RemoteKeyboardError as exc:
                    errors.append(exc.code)
                except Exception:
                    errors.append("uu_remote_stop_failed")
                # UU's screen proxy can coalesce the STOP receipt with the
                # final exit frame. A checksum-valid final frame is independent
                # proof even when the intermediate STOP receipt was not seen.
                if not result['helper_stopped_observed']:
                    try:
                        self._drain()
                        frame=self._observe_stopped()
                        if frame is not None:
                            result['helper_stopped_observed']=True
                            result['remote_helper_exit_reason']=frame['result']['reason']
                            if self._fixture_open and frame['result'].get('fixture_closed') is True:self._fixture_open=False
                    except RemoteKeyboardError:pass
            if self._attempted:
                # UU may acknowledge STOP before its terminal deletion is
                # visible. Reconcile that exact name, never the shared client.
                deadline = time.monotonic() + 8
                try:
                    result['remote_terminal_absent'] = not self._exists(timeout=2)
                except RemoteKeyboardError as exc:
                    errors.append(exc.code)
                if not result['remote_terminal_absent']:
                    try:
                        self._management('kill', timeout=2)
                    except RemoteKeyboardError as exc:
                        errors.append(exc.code)
                    while True:
                        try:
                            result['remote_terminal_absent'] = not self._exists(timeout=2)
                        except RemoteKeyboardError as exc:
                            errors.append(exc.code)
                        if result['remote_terminal_absent'] or time.monotonic() >= deadline:
                            break
                        time.sleep(.25)
            if self._process is not None:
                try:
                    self._process.close()
                    result["local_channel_closed"] = True
                except Exception:
                    errors.append("uu_local_terminal_close_failed")
            self._closed = True
            result["fixture_cleanup_confirmed"] = not self._fixture_open
            # If startup failed before READY, exact terminal deletion is the
            # available lifecycle proof; there was no accepted GUI request.
            helper_complete = self._ready is None or result["helper_stopped_observed"]
            result["cleanup_complete"] = bool(result["remote_terminal_absent"] and result["local_channel_closed"] and result["fixture_cleanup_confirmed"] and helper_complete)
            if self._registry_record is not None and result['remote_terminal_absent']:
                try:
                    self._registry.forget(self._registry_record)
                    result['registry_record_removed']=self._registry_record not in self._registry.records(self._device_id)
                    result['cleanup_complete']=result['cleanup_complete'] and result['registry_record_removed']
                except Exception:
                    errors.append('uu_terminal_registry_cleanup_pending')
                    result['cleanup_complete']=False
            if errors:
                result['diagnostic_codes' if result['cleanup_complete'] else 'error_codes'] = sorted(set(errors))
            self._cleanup_result = dict(result)
            # Discard terminal echoes and request receipts containing GUI text.
            self._terminal.clear()
            self._terminal = _TerminalFrames()
            self._requests.clear()
            return dict(result)
