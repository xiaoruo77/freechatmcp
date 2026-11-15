"""Pure VT renderer tests; these never start UU or touch a desktop."""
from __future__ import annotations

import base64
import hashlib
import json
import re

import pytest

from web_tools.uu_remote_keyboard import _TerminalFrames, _PREFIX


NONCE = 'a' * 32
HASH = 'b' * 64
LOAD = f'XRU_LOAD_{NONCE}|0|END'
APPEND = f'XRU_APPEND_{NONCE}_12|13312|{HASH}|END'
VERIFY = f'XRU_VERIFY_{NONCE}|13312|{HASH}|END'


@pytest.mark.parametrize('column', [1, 140, 157, 159, 160])
@pytest.mark.parametrize('marker', [LOAD, APPEND, VERIFY,
    f'XRUPROBE_{NONCE}|REMOTE_PC|2|END', f'XRUA_{NONCE}_psrl|1|END',
    f'XRU_FAIL_{NONCE}|1|END', f'XRU_NAK_{NONCE}|12|END'])
def test_all_actual_control_frames_survive_physical_wrap_including_prefix(column, marker):
    terminal = _TerminalFrames()
    raw = f'\x1b[1;{column}H' + marker + '\r\n'
    for offset in range(0, len(raw), 13):
        terminal.feed(raw[offset:offset + 13])
    assert marker in terminal.text
    assert terminal.text.count(marker) == 1
    assert terminal.frames == []


def test_maximum_nonce_ack_reassembles_across_multiple_full_rows():
    terminal = _TerminalFrames()
    marker = f'XRU_APPEND_{"n" * 128}_199|200000|{HASH}|END'
    assert 160 < len(marker) < terminal._CONTROL_LIMIT
    terminal.feed('\x1b[1;160H' + marker + '\r\n')
    assert marker in terminal.text


def test_fragmented_control_suffix_and_prefix_repaint_become_one_complete_record():
    terminal = _TerminalFrames()
    # Prefix starts at 157 and therefore is itself wrapped. Paint the suffix
    # first, as UU's console screen proxy can emit out-of-order differences.
    terminal.feed('\x1b[2;1H' + LOAD[4:])
    assert LOAD not in terminal.text
    terminal.feed('\x1b[1;157H' + LOAD[:4])
    assert LOAD in terminal.text
    terminal.text = ''
    # Identical screen repaints must not masquerade as new acknowledgements.
    terminal.feed('\x1b[1;157H' + LOAD[:4])
    terminal.feed('\x1b[2;1H' + LOAD[4:])
    assert LOAD not in terminal.text


def test_truncated_frame_is_not_accepted_until_its_bounded_suffix_is_present():
    terminal = _TerminalFrames()
    terminal.feed('\x1b[1;140H' + LOAD[:-1])
    assert LOAD not in terminal.text
    assert 'XRU_LOAD_' not in terminal.text
    terminal.feed(LOAD[-1])
    assert LOAD in terminal.text


def test_control_record_is_captured_before_clear_in_same_read():
    terminal = _TerminalFrames()
    terminal.feed('\x1b[1;157H' + LOAD + '\r\n\x1b[2J\x1b[1;1HPS C:\\>')
    assert LOAD in terminal.text
    assert LOAD not in terminal.screen_text


@pytest.mark.parametrize('column', [1, 140, 157])
@pytest.mark.parametrize('expression', [
    "[Console]::WriteLine('" + LOAD + "')",
    'Write-Output "' + LOAD + '"',
    "'" + LOAD + "'",
    LOAD + '; this is an error or echoed expression',
    LOAD + "'",
    'PS C:\\> ' + LOAD,
])
def test_quoted_command_echo_or_trailing_error_text_never_becomes_control_ack(column, expression):
    terminal = _TerminalFrames()
    terminal.feed(f'\x1b[1;{column}H' + expression + '\r\n')
    assert LOAD not in terminal.text


def test_unterminated_nonfull_line_is_not_spliced_to_next_line():
    terminal = _TerminalFrames()
    split = 20
    terminal.feed(LOAD[:split] + '\r\n' + LOAD[split:] + '\r\n')
    assert LOAD not in terminal.text
    assert terminal.text == ''


@pytest.mark.parametrize('marker', [
    f'XRU_LOAD_{NONCE}|0|EN',
    f'XRU_LOAD_{NONCE}|0|END_UNTERMINATED',
    f'XRU_LOAD_{NONCE}|0|END error',
    f'XRU_APPEND_{NONCE}_0|1024|{"b" * 63}|END',
    f'XRU_APPEND_{NONCE}_0|1024|{"z" * 64}|END',
    f'XRU_VERIFY_{NONCE}|9999999|{HASH}|END',
    'XRU_LOAD_' + 'n' * 600 + '|0|END',
])
def test_malformed_or_oversized_control_records_are_not_retained(marker):
    terminal = _TerminalFrames()
    terminal.feed('\x1b[1;157H' + marker + '\r\n')
    assert marker not in terminal.text
    assert terminal.text == ''


def test_renderer_does_not_weaken_caller_nonce_length_or_hash_checks():
    terminal = _TerminalFrames()
    correct = re.compile(re.escape(f'XRU_APPEND_{NONCE}_12|') + r'([0-9]+)\|([0-9a-f]{64})\|END')
    wrong_nonce = f'XRU_APPEND_{"c" * 32}_12|13312|{HASH}|END'
    wrong_length = f'XRU_APPEND_{NONCE}_12|13311|{HASH}|END'
    wrong_hash = f'XRU_APPEND_{NONCE}_12|13312|{"c" * 64}|END'
    for marker in (wrong_nonce, wrong_length, wrong_hash):
        terminal.feed('\x1b[2J\x1b[1;157H' + marker + '\r\n')
    matches = list(correct.finditer(terminal.text))
    assert not any(int(m[1]) == 13312 and m[2] == HASH for m in matches)
    terminal.feed('\x1b[2J\x1b[1;157H' + APPEND + '\r\n')
    assert any(int(m[1]) == 13312 and m[2] == HASH for m in correct.finditer(terminal.text))


def encoded_frame(payload, *, tamper=False):
    raw = json.dumps(payload, separators=(',', ':')).encode()
    checksum = hashlib.sha256(raw).hexdigest()
    if tamper:
        checksum = '0' * 64
    return _PREFIX + base64.b64encode(raw).decode() + '|' + checksum + '|END\r\n'


def test_v2_parser_still_requires_checksum_and_preserves_wrapped_control_record():
    payload = {'protocol': 'xiaoruo1.uu.remote-input.v2', 'nonce': NONCE,
               'reqid': 'test-op', 'action': 'status', 'ok': True,
               'result': {'text': 'a' * 1000}}
    terminal = _TerminalFrames()
    terminal.feed('\x1b[1;157H' + LOAD + '\r\n' + encoded_frame(payload, tamper=True))
    assert terminal.frames == []
    assert LOAD in terminal.text
    terminal.feed(encoded_frame(payload))
    assert terminal.frames == [payload]
    assert LOAD in terminal.text


def test_clear_releases_control_dedup_state_for_reuse():
    terminal = _TerminalFrames()
    terminal.feed(LOAD + '\r\n')
    terminal.clear()
    assert terminal.text == '' and terminal._screen is None
    terminal.feed('\x1b[1;157H' + LOAD + '\r\n')
    assert LOAD in terminal.text
