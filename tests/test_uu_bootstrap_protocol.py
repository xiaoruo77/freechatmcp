"""Pure bootstrap validation; never execute a loader, GUI, or remote command."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import pytest

from web_tools.uu_bootstrap_protocol import build_loader, build_receiver, chunk_wire


NONCE = '0123456789abcdef0123456789abcdef'
SOURCE = base64.b64encode(b'abcdefghijkl').decode('ascii')


def _powershell(script):
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    if os.name != 'nt' or not executable.is_file():
        pytest.skip('The pure PowerShell state machine requires installed Windows PowerShell')
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    result = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _receive(wires, count=2, length=len(SOURCE)):
    # Run only the receiver function. This fragment has no ReadLine loop,
    # ScriptBlock, decompression, GUI, input, network, or filesystem calls.
    data = base64.b64encode(json.dumps(wires).encode()).decode('ascii')
    script = build_receiver(NONCE, count, length) + (
        "$xrTest=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"+data+"'))|ConvertFrom-Json;"
        "$xrOut=@();foreach($w in @($xrTest)){"
        "$a=xrReceive ([string]$w);$xrOut+=[pscustomobject]@{ack=$a;next=$script:xrNext;"
        "length=$script:XR.Length;source=$script:XR;records=$script:xrSeen.Count}};"
        "ConvertTo-Json -InputObject @($xrOut) -Depth 4 -Compress"
    )
    return json.loads(_powershell(script))


def _nak(index):
    return f'XRU_NAK_{NONCE}|{index}|END'


def _ack(index, length, data):
    return f'XRU_APPEND_{NONCE}_{index}|{length}|{hashlib.sha256(data.encode("ascii")).hexdigest()}|END'


def test_pure_receiver_accepts_chunks_and_idempotently_replays_each_ack():
    first, second = SOURCE[:8], SOURCE[8:]
    results = _receive([chunk_wire(NONCE, 0, first), chunk_wire(NONCE, 0, first),
                        chunk_wire(NONCE, 1, second), chunk_wire(NONCE, 1, second)])
    assert [item['ack'] for item in results] == [_ack(0, 8, first)]*2 + [_ack(1, 16, second)]*2
    assert [item['length'] for item in results] == [8, 8, 16, 16]
    assert [item['next'] for item in results] == [1, 1, 2, 2]
    assert results[-1]['source'] == SOURCE and results[-1]['records'] == 2


def test_rejected_or_conflicting_chunks_do_not_advance_or_replace_receipts():
    first, second = SOURCE[:8], SOURCE[8:]
    valid = chunk_wire(NONCE, 0, first)
    parts = valid.split('|')
    parts[4] = ('0' if parts[4][0] != '0' else '1') + parts[4][1:]
    bad_hash = '|'.join(parts)
    wrong_nonce = chunk_wire('f'*32, 0, first)
    conflict = chunk_wire(NONCE, 0, 'QUJDREVG')
    results = _receive([wrong_nonce, chunk_wire(NONCE, 1, second), bad_hash,
                        valid, conflict, valid, chunk_wire(NONCE, 1, second)])
    assert [item['ack'] for item in results[:3]] == [_nak(0)]*3
    assert all(item['length'] == item['next'] == item['records'] == 0 for item in results[:3])
    assert results[4]['ack'] == _nak(1) and results[4]['source'] == first
    assert results[5]['ack'] == _ack(0, 8, first)
    assert results[-1]['source'] == SOURCE


def test_malformed_wire_and_impossible_lengths_never_append():
    first = SOURCE[:8]
    oversized = 'A'*1025
    raw_oversized = f'XRUCHUNK|{NONCE}|0|{oversized}|{hashlib.sha256(oversized.encode()).hexdigest()}|END'
    leading_zero = chunk_wire(NONCE, 0, first).replace('|0|', '|00|', 1)
    uppercase_hash = chunk_wire(NONCE, 0, first)
    fields = uppercase_hash.split('|');fields[4] = fields[4].upper()
    # A 1-character first chunk cannot possibly reach 16 with one remaining
    # chunk when the total is 2048; bounds are checked before state mutation.
    impossible = chunk_wire(NONCE, 0, 'A')
    results = _receive(['not-a-frame', raw_oversized, leading_zero, '|'.join(fields)], 2, 16)
    assert all(item['ack'] == _nak(0) and item['length'] == 0 for item in results)
    results = _receive([impossible], 2, 2048)
    assert results[0]['ack'] == _nak(0) and results[0]['source'] == ''


def test_nonfinal_padding_and_wrong_final_length_are_rejected():
    first, second = SOURCE[:8], SOURCE[8:]
    results = _receive([chunk_wire(NONCE, 0, 'YWJjZA=='), chunk_wire(NONCE, 0, first),
                        chunk_wire(NONCE, 1, second[:4]), chunk_wire(NONCE, 1, second)])
    assert results[0]['ack'] == _nak(0) and results[0]['length'] == 0
    assert results[2]['ack'] == _nak(1) and results[2]['length'] == 8
    assert results[3]['source'] == SOURCE


def test_a_full_1024_character_chunk_and_long_nonce_fit_the_wire_bound():
    nonce = 'z'*128
    data = 'A'*1024
    wire = chunk_wire(nonce, 199, data)
    assert len(wire) <= 1300
    assert wire.startswith('XRUCHUNK|'+nonce+'|199|') and wire.endswith('|END')


def test_full_size_chunk_is_accepted_and_final_duplicate_does_not_grow_source():
    data = 'A'*1024
    wire = chunk_wire(NONCE, 0, data)
    results = _receive([wire, wire], 1, 1024)
    assert [item['ack'] for item in results] == [_ack(0, 1024, data)]*2
    assert all(item['length'] == 1024 and item['next'] == item['records'] == 1 for item in results)


def test_loader_parameter_maxima_stay_below_fixed_command_bound():
    program = build_loader('n'*128, 'C'*63, 200, 200000, 'f'*64)
    assert len(program) < 6000
    import gzip
    packed = base64.b64encode(gzip.compress(program.encode(), mtime=0)).decode()
    assert len(packed) < 2500


@pytest.mark.parametrize('kwargs', [
    {'nonce': 'short'}, {'nonce': "a'*32;exit"}, {'nonce': 'a'*129},
    {'computer': "PC';exit;#"}, {'computer': '测试电脑'}, {'computer': 'PC.'},
    {'chunk_count': True}, {'chunk_count': 0}, {'chunk_count': 201},
    {'total_length': True}, {'total_length': 0}, {'total_length': 200004},
    {'total_length': 15}, {'total_length': 4096}, {'source_sha256': 'g'*64},
])
def test_loader_parameters_are_bounded_and_cannot_inject_script(kwargs):
    values = dict(nonce=NONCE, computer='REMOTE_PC', chunk_count=2, total_length=16,
                  source_sha256=hashlib.sha256(SOURCE.encode()).hexdigest())
    values.update(kwargs)
    with pytest.raises(ValueError):
        build_loader(**values)


def test_loader_is_small_computed_and_checks_full_source_before_execution():
    digest = hashlib.sha256(SOURCE.encode()).hexdigest()
    program = build_loader(NONCE, 'REMOTE_PC', 2, 16, digest.upper())
    assert len(program) < 6000
    assert 'XRU_LOAD_'+NONCE+'|0|END' not in program  # command echo is not a READY marker
    assert "'XRU_LOAD_'+$script:xrNonce+'|'+0+'|END'" in program
    assert 'try{[Console]::Clear()}catch{}' in program
    assert program.index('$xrWholeHash=xrHash') < program.index('FromBase64String($script:XR)') < program.index('ScriptBlock]::Create')
    assert digest in program
    assert f"-ExpectedComputer 'REMOTE_PC' -Nonce '{NONCE}';exit" in program
    assert 'Set-Content' not in program and 'Invoke-Expression' not in program


def test_loader_is_syntactically_valid_powershell_without_executing_it():
    program = build_loader(NONCE, 'REMOTE_PC', 2, 16, hashlib.sha256(SOURCE.encode()).hexdigest())
    data = base64.b64encode(program.encode()).decode()
    script = ("$src=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"+data+"'));"
        "$tokens=$null;$errors=$null;"
        "$null=[Management.Automation.Language.Parser]::ParseInput($src,[ref]$tokens,[ref]$errors);"
        "if($errors.Count){$errors|ForEach-Object{$_.Message};exit 1};'parsed'")
    assert _powershell(script) == 'parsed'


@pytest.mark.parametrize('index,data', [(True, 'AAAA'), (-1, 'AAAA'), (200, 'AAAA'),
                                      (0, ''), (0, 'A'*1025), (0, "AAAA';exit")])
def test_chunk_wire_rejects_unsupported_fields(index, data):
    with pytest.raises(ValueError):
        chunk_wire(NONCE, index, data)
