"""Apple's file compression (decmpfs), used on HFS+ and APFS.

macOS stores many files compressed (apps, system files, anything copied with ``ditto
--hfsCompression``): the file itself looks empty, a ``com.apple.decmpfs`` extended attribute holds
the real size and the compression method, and the compressed bytes are either in that attribute
(small files) or in the file's resource fork, cut into 64 KiB pieces compressed one by one.

Methods: zlib (Python's own), LZVN and LZFSE. The LZVN and LZFSE decoders below follow Apple's
open-source reference implementation (https://github.com/lzfse/lzfse):

    Copyright (c) 2015-2016, Apple Inc. All rights reserved.
    Redistribution and use in source and binary forms, with or without modification, are permitted
    under the BSD 3-Clause license; the full notice is in THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

CHUNK = 0x10000                     # uncompressed bytes per piece in the resource fork


class CompressionError(ValueError):
    """The compressed data is damaged, or not what its header says."""


# ----------------------------------------------------------------------------- decmpfs
ZLIB_ATTR, ZLIB_RSRC = 3, 4
DATALESS = 5                        # an iCloud file whose data was never downloaded
LZVN_ATTR, LZVN_RSRC = 7, 8
RAW_ATTR, RAW_RSRC = 9, 10
LZFSE_ATTR, LZFSE_RSRC = 11, 12
LZBITMAP_ATTR, LZBITMAP_RSRC = 13, 14

METHODS = {1: "raw", ZLIB_ATTR: "zlib", ZLIB_RSRC: "zlib", LZVN_ATTR: "lzvn", LZVN_RSRC: "lzvn",
           RAW_ATTR: "raw", RAW_RSRC: "raw", LZFSE_ATTR: "lzfse", LZFSE_RSRC: "lzfse"}
IN_RESOURCE_FORK = {ZLIB_RSRC, LZVN_RSRC, RAW_RSRC, LZFSE_RSRC, LZBITMAP_RSRC}


@dataclass
class Decmpfs:
    """The ``com.apple.decmpfs`` attribute of a compressed file."""

    kind: int
    size: int                       # the file's real (uncompressed) size
    inline: bytes                   # compressed data stored in the attribute itself

    @property
    def method(self) -> str | None:
        return METHODS.get(self.kind)

    @property
    def in_resource_fork(self) -> bool:
        return self.kind in IN_RESOURCE_FORK

    def unsupported(self) -> str:
        """Why this file can't be decompressed, or "" if it can."""
        if self.kind == DATALESS:
            return "Online-only iCloud file: its data was never downloaded to this drive."
        if self.method is None:
            return f"Compressed with a method Lifeboat can't decode yet (macOS type {self.kind})."
        return ""


def parse_decmpfs(xattr: bytes) -> Decmpfs | None:
    if len(xattr) < 16 or xattr[:4] != b"fpmc":
        return None
    kind, size = struct.unpack_from("<IQ", xattr, 4)
    return Decmpfs(kind, size, bytes(xattr[16:]))


def table_bytes_needed(kind: int, size: int, head: bytes = b"") -> int:
    """How much of the resource fork's start holds the piece table (``head`` = what was read)."""
    count = -(-size // CHUNK)
    if kind == ZLIB_RSRC:
        if len(head) < 4:
            return 0x110
        base = int.from_bytes(head[:4], "big") + 4
        return base + 4 + count * 8
    return (count + 1) * 4


def chunk_table(kind: int, head: bytes, size: int, fork_size: int) -> list[tuple[int, int]]:
    """Where each compressed piece is in the resource fork: [(offset, length)], one per 64 KiB."""
    count = -(-size // CHUNK)
    if count == 0:
        return []
    if kind == ZLIB_RSRC:
        # A classic resource fork: header, then the "cmpf" resource holding a piece table.
        if len(head) < 16:
            raise CompressionError("the resource fork header is missing")
        base = int.from_bytes(head[:4], "big") + 4
        if base + 4 > len(head):
            raise CompressionError("the compression table is missing")
        stored = int.from_bytes(head[base:base + 4], "little")
        if stored != count or base + 4 + count * 8 > len(head):
            raise CompressionError("the compression table doesn't match the file size")
        entries = [(base + off, length) for off, length in struct.iter_unpack("<II", head[base + 4:
                                                                                       base + 4 + count * 8])]
    else:
        need = (count + 1) * 4
        if len(head) < need:
            raise CompressionError("the compression table is missing")
        offsets = struct.unpack_from(f"<{count + 1}I", head, 0)
        entries = [(offsets[i], offsets[i + 1] - offsets[i]) for i in range(count)]
    for off, length in entries:
        if length <= 0 or off < 0 or off + length > fork_size:
            raise CompressionError("the compression table points outside the resource fork")
    return entries


def decompress_chunk(method: str, data: bytes, size: int) -> bytes:
    """One compressed piece (or a whole small file) -> exactly ``size`` bytes."""
    if method == "zlib":
        if data[:1] and data[0] & 0x0F == 0x0F:           # stored uncompressed
            out = data[1:1 + size]
        else:
            try:
                out = zlib.decompress(data)
            except zlib.error as exc:
                raise CompressionError(f"zlib: {exc}") from None
    elif method == "lzvn":
        out = data[1:1 + size] if data[:1] == b"\x06" else lzvn_decompress(data, size)
    elif method == "lzfse":
        if data[:3] == b"bvx":
            out = lzfse_decompress(data)
        elif data[:1] in (b"\xff", b"\x06"):
            out = data[1:1 + size]
        else:
            raise CompressionError("not LZFSE data")
    elif method == "raw":
        out = data[1:1 + size] if len(data) == size + 1 else data[:size]
    else:
        raise CompressionError(f"unknown method {method}")
    if len(out) != size:
        raise CompressionError(f"expected {size:,} bytes, got {len(out):,}")
    return bytes(out)


# -------------------------------------------------------------------------------- LZVN
_SML_D, _MED_D, _LRG_D, _PRE_D, _SML_M, _LRG_M, _SML_L, _LRG_L, _NOP, _EOS, _UDEF = range(11)


def _lzvn_kinds() -> bytes:
    kinds = bytearray(256)
    for op in range(256):
        if op >= 0xF0:
            kinds[op] = _LRG_M if op == 0xF0 else _SML_M
        elif op >= 0xE0:
            kinds[op] = _LRG_L if op == 0xE0 else _SML_L
        elif 0xA0 <= op <= 0xBF:
            kinds[op] = _MED_D
        elif 0x70 <= op <= 0x7F or 0xD0 <= op <= 0xDF:
            kinds[op] = _UDEF
        elif op & 7 == 7:
            kinds[op] = _LRG_D
        elif op & 7 == 6:
            kinds[op] = _EOS if op == 0x06 else _NOP if op in (0x0E, 0x16) else _UDEF if op < 0x40 else _PRE_D
        else:
            kinds[op] = _SML_D
    return bytes(kinds)


_LZVN_KINDS = _lzvn_kinds()


def _copy_match(out: bytearray, distance: int, length: int) -> None:
    if distance <= 0 or distance > len(out):
        raise CompressionError("a match points before the start of the data")
    if distance >= length:
        start = len(out) - distance
        out += out[start:start + length]
    else:                           # overlapping copy: repeats the last ``distance`` bytes
        out += (out[-distance:] * (length // distance + 1))[:length]


def _lzvn_into(out: bytearray, src: bytes, pos: int, end: int, limit: int, distance: int = 0) -> int:
    """Decode LZVN from ``src[pos:end]`` onto ``out`` until it holds ``limit`` bytes or the stream
    ends. Earlier output stays reachable by matches. Returns the last match distance."""
    kinds = _LZVN_KINDS
    while len(out) < limit:
        if pos >= end:
            raise CompressionError("LZVN data ends early")
        op = src[pos]
        kind = kinds[op]
        if kind == _SML_D:
            literal, match = op >> 6, ((op >> 3) & 7) + 3
            if pos + 2 > end:
                raise CompressionError("LZVN data ends early")
            distance = ((op & 7) << 8) | src[pos + 1]
            pos += 2
        elif kind == _MED_D:
            literal = (op >> 3) & 3
            if pos + 3 > end:
                raise CompressionError("LZVN data ends early")
            word = src[pos + 1] | (src[pos + 2] << 8)
            match = (((op & 7) << 2) | (word & 3)) + 3
            distance = word >> 2
            pos += 3
        elif kind == _LRG_D:
            literal, match = op >> 6, ((op >> 3) & 7) + 3
            if pos + 3 > end:
                raise CompressionError("LZVN data ends early")
            distance = src[pos + 1] | (src[pos + 2] << 8)
            pos += 3
        elif kind == _PRE_D:
            literal, match = op >> 6, ((op >> 3) & 7) + 3
            pos += 1
        elif kind == _SML_M:
            literal, match = 0, op & 0x0F
            pos += 1
        elif kind == _LRG_M:
            if pos + 2 > end:
                raise CompressionError("LZVN data ends early")
            literal, match = 0, src[pos + 1] + 16
            pos += 2
        elif kind == _SML_L:
            literal, match = op & 0x0F, 0
            pos += 1
        elif kind == _LRG_L:
            if pos + 2 > end:
                raise CompressionError("LZVN data ends early")
            literal, match = src[pos + 1] + 16, 0
            pos += 2
        elif kind == _NOP:
            pos += 1
            continue
        elif kind == _EOS:
            break
        else:
            raise CompressionError(f"invalid LZVN instruction 0x{op:02x}")
        if literal:
            if pos + literal > end:
                raise CompressionError("LZVN data ends early")
            out += src[pos:pos + literal]
            pos += literal
        if match:
            _copy_match(out, distance, match)
    if len(out) > limit:
        del out[limit:]
    return distance


def lzvn_decompress(src: bytes, size: int) -> bytes:
    out = bytearray()
    _lzvn_into(out, src, 0, len(src), size)
    return bytes(out)


# ------------------------------------------------------------------------------- LZFSE
_L_SYMBOLS, _M_SYMBOLS, _D_SYMBOLS, _LIT_SYMBOLS = 20, 20, 64, 256
_L_STATES, _M_STATES, _D_STATES, _LIT_STATES = 64, 64, 256, 1024
_MATCHES_PER_BLOCK = 10000
_LITERALS_PER_BLOCK = 4 * _MATCHES_PER_BLOCK

_L_EXTRA = (0,) * 16 + (2, 3, 5, 8)
_L_BASE = (*range(16), 16, 20, 28, 60)
_M_EXTRA = (0,) * 16 + (3, 5, 8, 11)
_M_BASE = (*range(16), 16, 24, 56, 312)
_D_EXTRA = tuple(i // 4 for i in range(64))
_D_BASE = (
    0, 1, 2, 3, 4, 6, 8, 10, 12, 16, 20, 24, 28, 36, 44, 52, 60, 76, 92, 108, 124, 156, 188, 220, 252, 316,
    380, 444, 508, 636, 764, 892, 1020, 1276, 1532, 1788, 2044, 2556, 3068, 3580, 4092, 5116, 6140, 7164,
    8188, 10236, 12284, 14332, 16380, 20476, 24572, 28668, 32764, 40956, 49148, 57340, 65532, 81916, 98300,
    114684, 131068, 163836, 196604, 229372,
)
_V1_HEADER = struct.Struct("<7Ii4HiHHH20H20H64H256H")      # 770 bytes, stored padded to 772
_V1_HEADER_SIZE = 772


def _freq_value(bits: int) -> tuple[int, int]:
    """One frequency of a v2 header: (value, bits used)."""
    low = bits & 31
    nbits = (2, 3, 2, 5, 2, 3, 2, 8, 2, 3, 2, 5, 2, 3, 2, 14)[low & 15]
    if nbits == 8:
        return 8 + ((bits >> 4) & 0xF), 8
    if nbits == 14:
        return 24 + ((bits >> 4) & 0x3FF), 14
    return (0, 2, 1, 4, 0, 3, 1, -1, 0, 2, 1, 5, 0, 3, 1, -1,
            0, 2, 1, 6, 0, 3, 1, -1, 0, 2, 1, 7, 0, 3, 1, -1)[low], nbits


def _decode_freqs(src: bytes, start: int, end: int) -> list[int]:
    total = _L_SYMBOLS + _M_SYMBOLS + _D_SYMBOLS + _LIT_SYMBOLS
    if start == end:
        return [0] * total
    freqs = []
    accum = nbits = 0
    pos = start
    for _ in range(total):
        while pos < end and nbits + 8 <= 32:
            accum |= src[pos] << nbits
            nbits += 8
            pos += 1
        value, used = _freq_value(accum)
        if used > nbits:
            raise CompressionError("LZFSE frequency table is damaged")
        freqs.append(value)
        accum >>= used
        nbits -= used
    if nbits >= 8 or pos != end:
        raise CompressionError("LZFSE frequency table is damaged")
    return freqs


def _state_entries(nstates: int, freqs: tuple[int, ...] | list[int]) -> list[tuple[int, int, int]]:
    """(symbol, bits to read, next-state base) for every decoder state."""
    if sum(freqs) > nstates:
        raise CompressionError("LZFSE frequency table is damaged")
    entries: list[tuple[int, int, int]] = []
    width = nstates.bit_length()
    for symbol, f in enumerate(freqs):
        if f == 0:
            continue
        k = width - f.bit_length()
        j0 = ((2 * nstates) >> k) - f
        for j in range(f):
            if j < j0:
                entries.append((symbol, k, ((f + j) << k) - nstates))
            else:
                entries.append((symbol, k - 1, (j - j0) << (k - 1)))
    return entries


def _value_table(nstates: int, freqs: list[int], extra: tuple[int, ...], base: tuple[int, ...]
                 ) -> tuple[list[int], list[int], list[int], list[int]]:
    """Per state: (bits to read, extra value bits, next-state base, value base)."""
    total, vbits, delta, vbase = [], [], [], []
    for symbol, k, d in _state_entries(nstates, freqs):
        total.append(k + extra[symbol])
        vbits.append(extra[symbol])
        delta.append(d)
        vbase.append(base[symbol])
    return total, vbits, delta, vbase


def _bits_init(src: bytes, start: int, end: int, nbits: int) -> tuple[int, int, int]:
    """Start reading a backwards bit stream that ends at ``end``: (accum, bits in it, position)."""
    if nbits:
        if end < start + 8:
            raise CompressionError("LZFSE data ends early")
        end -= 8
        accum = int.from_bytes(src[end:end + 8], "little")
        avail = nbits + 64
    else:
        if end < start + 7:
            raise CompressionError("LZFSE data ends early")
        end -= 7
        accum = int.from_bytes(src[end:end + 7], "little")
        avail = 56
    if not 56 <= avail < 64 or accum >> avail:
        raise CompressionError("LZFSE bit stream is damaged")
    return accum, avail, end


def _decode_literals(src: bytes, end: int, nbits: int, states: list[int], count: int,
                     freqs: list[int]) -> bytearray:
    """FSE-decode a block's literals; their bit stream ends at ``end`` and is read backwards (like
    Apple's decoder, reads may reach into the bytes before it, never before the stream's start)."""
    entries = _state_entries(_LIT_STATES, freqs)
    if count and len(entries) != _LIT_STATES:
        entries += [(0, 0, 0)] * (_LIT_STATES - len(entries))
    sym = [e[0] for e in entries]
    kbits = [e[1] for e in entries]
    delta = [e[2] for e in entries]
    accum, avail, pos = _bits_init(src, 0, end, nbits)
    s0, s1, s2, s3 = states
    out = bytearray(count)
    try:
        for i in range(0, count, 4):
            need = (63 - avail) & ~7
            if need:
                pos -= need >> 3
                if pos < 0:
                    raise CompressionError("LZFSE literals end early")
                accum = (accum << need) | int.from_bytes(src[pos:pos + (need >> 3)], "little")
                avail += need
            k = kbits[s0]
            avail -= k
            out[i] = sym[s0]
            s0 = delta[s0] + (accum >> avail)
            accum &= (1 << avail) - 1
            k = kbits[s1]
            avail -= k
            out[i + 1] = sym[s1]
            s1 = delta[s1] + (accum >> avail)
            accum &= (1 << avail) - 1
            k = kbits[s2]
            avail -= k
            out[i + 2] = sym[s2]
            s2 = delta[s2] + (accum >> avail)
            accum &= (1 << avail) - 1
            k = kbits[s3]
            avail -= k
            out[i + 3] = sym[s3]
            s3 = delta[s3] + (accum >> avail)
            accum &= (1 << avail) - 1
    except IndexError:
        raise CompressionError("LZFSE literals are damaged") from None
    return out


def _decode_block(src: bytes, pos: int, out: bytearray, v2: bool) -> int:
    """Decode one LZFSE compressed block starting at ``pos`` (its magic); return the next position."""
    if v2:
        if pos + 32 > len(src):
            raise CompressionError("LZFSE block header ends early")
        n_raw, v0, v1, vv = struct.unpack_from("<IQQQ", src, pos + 4)
        header_size = vv & 0xFFFFFFFF
        if header_size < 32 or pos + header_size > len(src):
            raise CompressionError("LZFSE block header is damaged")
        n_literals = v0 & 0xFFFFF
        n_lit_payload = (v0 >> 20) & 0xFFFFF
        n_matches = (v0 >> 40) & 0xFFFFF
        literal_bits = ((v0 >> 60) & 7) - 7
        lit_states = [(v1 >> s) & 0x3FF for s in (0, 10, 20, 30)]
        n_lmd_payload = (v1 >> 40) & 0xFFFFF
        lmd_bits = ((v1 >> 60) & 7) - 7
        l_state, m_state, d_state = (vv >> 32) & 0x3FF, (vv >> 42) & 0x3FF, (vv >> 52) & 0x3FF
        freqs = _decode_freqs(src, pos + 32, pos + header_size)
        pos += header_size
    else:
        if pos + _V1_HEADER_SIZE > len(src):
            raise CompressionError("LZFSE block header ends early")
        fields = _V1_HEADER.unpack_from(src, pos)
        (_magic, n_raw, _n_payload, n_literals, n_matches, n_lit_payload, n_lmd_payload, literal_bits,
         *rest) = fields
        lit_states = list(rest[0:4])
        lmd_bits, l_state, m_state, d_state = rest[4:8]
        freqs = list(rest[8:])
        pos += _V1_HEADER_SIZE
    if (n_literals > _LITERALS_PER_BLOCK or n_matches > _MATCHES_PER_BLOCK or n_literals % 4
            or any(s >= _LIT_STATES for s in lit_states) or l_state >= _L_STATES or m_state >= _M_STATES
            or d_state >= _D_STATES):
        raise CompressionError("LZFSE block header is damaged")
    l_freq = freqs[:_L_SYMBOLS]
    m_freq = freqs[_L_SYMBOLS:_L_SYMBOLS + _M_SYMBOLS]
    d_freq = freqs[_L_SYMBOLS + _M_SYMBOLS:_L_SYMBOLS + _M_SYMBOLS + _D_SYMBOLS]
    lit_freq = freqs[_L_SYMBOLS + _M_SYMBOLS + _D_SYMBOLS:]
    lit_end = pos + n_lit_payload
    lmd_end = lit_end + n_lmd_payload
    if lmd_end > len(src):
        raise CompressionError("LZFSE block ends early")
    literals = _decode_literals(src, lit_end, literal_bits, lit_states, n_literals, lit_freq)
    l_total, l_vbits, l_delta, l_base = _value_table(_L_STATES, l_freq, _L_EXTRA, _L_BASE)
    m_total, m_vbits, m_delta, m_base = _value_table(_M_STATES, m_freq, _M_EXTRA, _M_BASE)
    d_total, d_vbits, d_delta, d_base = _value_table(_D_STATES, d_freq, _D_EXTRA, _D_BASE)
    start_len = len(out)
    accum, avail, bpos = _bits_init(src, lit_end, lmd_end, lmd_bits)
    lit_pos = 0
    distance = -1
    try:
        for _ in range(n_matches):
            need = (63 - avail) & ~7
            if need:
                bpos -= need >> 3
                if bpos < 0:
                    raise CompressionError("LZFSE matches end early")
                accum = (accum << need) | int.from_bytes(src[bpos:bpos + (need >> 3)], "little")
                avail += need
            n = l_total[l_state]
            avail -= n
            bits = accum >> avail
            accum &= (1 << avail) - 1
            vb = l_vbits[l_state]
            literal = l_base[l_state] + (bits & ((1 << vb) - 1))
            l_state = l_delta[l_state] + (bits >> vb)
            n = m_total[m_state]
            avail -= n
            bits = accum >> avail
            accum &= (1 << avail) - 1
            vb = m_vbits[m_state]
            match = m_base[m_state] + (bits & ((1 << vb) - 1))
            m_state = m_delta[m_state] + (bits >> vb)
            n = d_total[d_state]
            avail -= n
            bits = accum >> avail
            accum &= (1 << avail) - 1
            vb = d_vbits[d_state]
            new_d = d_base[d_state] + (bits & ((1 << vb) - 1))
            d_state = d_delta[d_state] + (bits >> vb)
            if new_d:
                distance = new_d
            if lit_pos + literal > len(literals) or distance < 0 or distance > len(out) + literal:
                raise CompressionError("LZFSE block is damaged")
            if literal:
                out += literals[lit_pos:lit_pos + literal]
                lit_pos += literal
            if match:
                _copy_match(out, distance, match)
    except IndexError:
        raise CompressionError("LZFSE block is damaged") from None
    if len(out) - start_len != n_raw:
        raise CompressionError("LZFSE block decoded to the wrong size")
    return lmd_end


def lzfse_decompress(src: bytes) -> bytes:
    """Decode a whole LZFSE stream (blocks "bvx2", "bvx1", "bvxn" and "bvx-", ended by "bvx$")."""
    out = bytearray()
    pos = 0
    end = len(src)
    while True:
        if pos + 4 > end:
            raise CompressionError("LZFSE data ends early")
        magic = src[pos:pos + 4]
        if magic == b"bvx$":
            return bytes(out)
        if magic == b"bvx-":
            if pos + 8 > end:
                raise CompressionError("LZFSE data ends early")
            n_raw = int.from_bytes(src[pos + 4:pos + 8], "little")
            pos += 8
            if pos + n_raw > end:
                raise CompressionError("LZFSE data ends early")
            out += src[pos:pos + n_raw]
            pos += n_raw
        elif magic == b"bvxn":
            if pos + 12 > end:
                raise CompressionError("LZFSE data ends early")
            n_raw, n_payload = struct.unpack_from("<II", src, pos + 4)
            pos += 12
            if pos + n_payload > end:
                raise CompressionError("LZFSE data ends early")
            target = len(out) + n_raw
            _lzvn_into(out, src, pos, pos + n_payload, target)
            if len(out) != target:
                raise CompressionError("LZVN block decoded to the wrong size")
            pos += n_payload
        elif magic in (b"bvx2", b"bvx1"):
            pos = _decode_block(src, pos, out, magic == b"bvx2")
        else:
            raise CompressionError("not an LZFSE block")


# Known answers, made by Apple's reference encoder (github.com/lzfse/lzfse): an FSE-coded block
# ("bvx2") and an LZVN one ("bvxn"). ``self_test`` decodes both.
_TEST_FSE = bytes.fromhex(
    "627678325014000028005001000b0070c6493baca4180000a70000003f28c00d97bf0030010080091300600200afc03cfa51ee0100"
    "b80700e01e00000000000000000000be0600000000000000e0c7000000e037e00578015e8017e00578015e0000000000005e0000"
    "0000008017e005780178347e03f80de0378017005e8077e2057801000000000000000000000000000000000000000000000000000000"
    "000000000000000000309b60e3472364f54854fa98a366ee47d3fbd77f00000000000000000098cbff03cdffdb406830101cd6f400"
    "62767824")
_TEST_LZVN = bytes.fromhex(
    "6276786ed400000088000000e01e4c696665626f6174206765747320796f75722066696c6573206f66662061206661696c696e67"
    "2064726976652e20382ef042e03a303132333435363738393a3b3c3d3e3f404142434445464748494a4b4c4d4e4f50515253545556"
    "5758595a5b5c5d5e5f606162636465666768696a6b6c6d6e6f70717273747576777879060000000000000062767824")


def self_test() -> None:
    """Decode the known answers above (raises if one comes out wrong)."""
    fse = b"".join(b"Lifeboat %d rescues files. " % (i % 7) for i in range(200))
    if lzfse_decompress(_TEST_FSE) != fse:
        raise CompressionError("LZFSE (FSE block) gives the wrong answer")
    lzvn = b"Lifeboat gets your files off a failing drive. " * 3 + bytes(range(48, 122))
    if lzfse_decompress(_TEST_LZVN) != lzvn:
        raise CompressionError("LZFSE (LZVN block) gives the wrong answer")
