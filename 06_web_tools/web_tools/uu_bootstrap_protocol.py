"""Build the fixed, checksummed UU in-memory helper bootstrap.

``build_receiver`` contains only a chunk state machine and hash function. It
can be exercised locally without loading a helper, executing source, or using
any desktop/input API. ``build_loader`` adds the remote-only read/execute loop.
"""
from __future__ import annotations

import re


MAX_CHUNK_LENGTH = 1024
MAX_CHUNK_COUNT = 200
MAX_SOURCE_LENGTH = 200_000
MAX_WIRE_LENGTH = 1300


def _bounds(nonce: str, chunk_count: int, total_length: int) -> None:
    if not isinstance(nonce, str) or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", nonce) is None:
        raise ValueError("nonce must contain 32-128 ASCII letters, digits, underscores or hyphens")
    if type(chunk_count) is not int or not 1 <= chunk_count <= MAX_CHUNK_COUNT:
        raise ValueError("chunk_count must be an integer from 1 to 200")
    if (type(total_length) is not int or not 1 <= total_length <= MAX_SOURCE_LENGTH
            or total_length % 4 or not chunk_count <= total_length <= chunk_count * MAX_CHUNK_LENGTH):
        raise ValueError("total_length must be a possible bounded Base64 source length")


def build_receiver(nonce: str, chunk_count: int, total_length: int) -> str:
    """Return pure PowerShell receiver definitions for protocol self-tests.

    Call ``xrReceive <wire>`` to obtain an ACK or NAK string. Source bytes are
    held in ``$script:XR``; this fragment never decodes or executes them.
    Completed chunks, including the final chunk, remain idempotently replayable.
    """
    _bounds(nonce, chunk_count, total_length)
    # All interpolated fields are validated ASCII identifiers or bounded ints.
    return (
        "$script:XR='';$script:xrNext=0;$script:xrSeen=@{};"
        f"$script:xrNonce='{nonce}';$script:xrCount={chunk_count};$script:xrLength={total_length};"
        "function xrHash([string]$v){"
        "$s=[Security.Cryptography.SHA256]::Create();try{"
        "return [BitConverter]::ToString($s.ComputeHash([Text.Encoding]::ASCII.GetBytes($v))).Replace('-','').ToLowerInvariant()"
        "}finally{$s.Dispose()}};"
        "function xrNak{return ('XRU_NAK_'+$script:xrNonce+'|'+$script:xrNext+'|END')};"
        "function xrAck([int]$i,$r){return ('XRU_APPEND_'+$script:xrNonce+'_'+$i+'|'+$r.l+'|'+$r.h+'|END')};"
        "function xrReceive([string]$w){"
        "if($null -eq $w -or $w.Length -gt 1300 -or $w -cnotmatch "
        "'^XRUCHUNK\\|([A-Za-z0-9_-]{32,128})\\|(0|[1-9][0-9]{0,2})\\|([A-Za-z0-9+/]+={0,2})\\|([0-9a-f]{64})\\|END$')"
        "{return (xrNak)};"
        "$n=$Matches[1];$i=[int]$Matches[2];$d=$Matches[3];$h=$Matches[4];"
        "if($n -cne $script:xrNonce -or $i -ge $script:xrCount -or $d.Length -gt 1024 -or (xrHash $d) -cne $h)"
        "{return (xrNak)};"
        "if($script:xrSeen.ContainsKey($i)){"
        "$r=$script:xrSeen[$i];if($r.h -ceq $h -and $r.d -ceq $d){return (xrAck $i $r)};return (xrNak)};"
        "if($i -ne $script:xrNext -or ($i -lt ($script:xrCount-1) -and $d.Contains('='))){return (xrNak)};"
        "$z=$script:XR.Length+$d.Length;$remaining=$script:xrCount-$i-1;"
        "if(($z+$remaining) -gt $script:xrLength -or ($z+$remaining*1024) -lt $script:xrLength){return (xrNak)};"
        "$script:XR+=$d;$r=@{h=$h;l=$z;d=$d};$script:xrSeen[$i]=$r;$script:xrNext++;return (xrAck $i $r)};"
    )


def build_loader(nonce: str, computer: str, chunk_count: int, total_length: int, source_sha256: str) -> str:
    """Return the fixed PowerShell bootstrap program, with no user code fields.

    The source hash covers the complete ASCII Base64-encoded gzip source. A
    chunk is appended only after its nonce, index, hash and possible length are
    checked. No decoding or ScriptBlock execution occurs before the full check.
    """
    _bounds(nonce, chunk_count, total_length)
    if not isinstance(computer, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,63}", computer) is None:
        raise ValueError("computer must be a bounded ASCII computer name")
    if not isinstance(source_sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", source_sha256) is None:
        raise ValueError("source_sha256 must be a 64-character SHA-256 hex digest")
    digest = source_sha256.lower()
    program = build_receiver(nonce, chunk_count, total_length) + (
        "function xrEmit([string]$v){try{[Console]::Clear()}catch{};[Console]::WriteLine($v);[Console]::Out.Flush()};"
        "xrEmit ('XRU_LOAD_'+$script:xrNonce+'|'+0+'|END');"
        "while($script:xrNext -lt $script:xrCount){"
        "$xrLine=[Console]::ReadLine();if($null -eq $xrLine){exit 103};xrEmit (xrReceive $xrLine)};"
        "$xrWholeHash=xrHash $script:XR;"
        "xrEmit ('XRU_VERIFY_'+$script:xrNonce+'|'+$script:XR.Length+'|'+$xrWholeHash+'|END');"
        f"if($script:XR.Length -ne {total_length} -or $xrWholeHash -cne '{digest}')"
        "{xrEmit ('XRU_FAIL_'+$script:xrNonce+'|'+1+'|END');exit 104};"
        "$xrM=[IO.MemoryStream]::new([Convert]::FromBase64String($script:XR));"
        "$xrG=[IO.Compression.GZipStream]::new($xrM,[IO.Compression.CompressionMode]::Decompress);"
        "$xrR=[IO.StreamReader]::new($xrG,[Text.Encoding]::UTF8);"
        "try{$xrSource=$xrR.ReadToEnd()}finally{$xrR.Dispose()};$script:XR=$null;"
        f"&([ScriptBlock]::Create($xrSource)) -ExpectedComputer '{computer}' -Nonce '{nonce}';exit"
    )
    if len(program) > 6000:
        raise ValueError("fixed bootstrap program exceeds its command bound")
    return program


def chunk_wire(nonce: str, index: int, data: str) -> str:
    """Encode one validated chunk for controller-side callers and self-tests."""
    if not isinstance(nonce, str) or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", nonce) is None:
        raise ValueError("invalid nonce")
    if type(index) is not int or not 0 <= index < MAX_CHUNK_COUNT:
        raise ValueError("invalid chunk index")
    if not isinstance(data, str) or not 1 <= len(data) <= MAX_CHUNK_LENGTH or re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", data) is None:
        raise ValueError("invalid Base64 data chunk")
    import hashlib
    checksum = hashlib.sha256(data.encode("ascii")).hexdigest()
    return f"XRUCHUNK|{nonce}|{index}|{data}|{checksum}|END"
