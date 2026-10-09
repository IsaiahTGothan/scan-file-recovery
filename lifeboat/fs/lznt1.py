"""LZNT1 decompression (NTFS file compression).

A compression unit (normally 16 clusters = 64 KiB) is stored as a series of
chunks.  Each chunk starts with a 16-bit header: the low 12 bits are the
chunk size minus 3, bit 15 says whether the chunk is compressed.  A chunk
expands to at most 4096 bytes; shorter output is zero padded.  Inside a
compressed chunk, a flag byte announces 8 tokens: literal bytes (bit 0) or
16-bit back references (bit 1) whose split between offset and length
depends on the current position within the 4 KiB chunk.
"""

from __future__ import annotations

CHUNK = 4096


class LZNT1Error(ValueError):
    pass


def _split(position: int) -> tuple[int, int]:
    """(offset shift, length mask) for a back reference at ``position``."""
    bits = 0
    value = position - 1
    while value >= 0x10:
        bits += 1
        value >>= 1
    return 12 - bits, 0xFFF >> bits


# Precompute for every position inside a chunk.
_SPLITS = [(0, 0)] + [_split(p) for p in range(1, CHUNK + 1)]


def decompress(data: bytes, out_size: int) -> bytes:
    out = bytearray()
    pos = 0
    total = len(data)
    while pos + 2 <= total and len(out) < out_size:
        header = data[pos] | (data[pos + 1] << 8)
        if header == 0:
            break
        length = (header & 0x0FFF) + 1
        pos += 2
        if pos + length > total:
            raise LZNT1Error("truncated chunk")
        chunk = data[pos:pos + length]
        pos += length
        start = len(out)
        if not header & 0x8000:
            out += chunk
        else:
            i = 0
            n = len(chunk)
            while i < n:
                flags = chunk[i]
                i += 1
                for bit in range(8):
                    if i >= n:
                        break
                    if not (flags >> bit) & 1:
                        out.append(chunk[i])
                        i += 1
                        continue
                    if i + 1 >= n:
                        raise LZNT1Error("truncated back reference")
                    token = chunk[i] | (chunk[i + 1] << 8)
                    i += 2
                    position = len(out) - start
                    if position <= 0 or position > CHUNK:
                        raise LZNT1Error("back reference at chunk start")
                    shift, mask = _SPLITS[position]
                    offset = (token >> shift) + 1
                    count = (token & mask) + 3
                    src = len(out) - offset
                    if src < start:
                        raise LZNT1Error("back reference before chunk start")
                    if offset >= count:
                        out += out[src:src + count]
                    else:
                        pattern = out[src:src + offset]
                        reps = count // offset + 1
                        out += (pattern * reps)[:count]
                    if len(out) - start > CHUNK:
                        raise LZNT1Error("chunk expands beyond 4096 bytes")
        produced = len(out) - start
        if produced > CHUNK:
            raise LZNT1Error("chunk larger than 4096 bytes")
        if produced < CHUNK:
            out += bytes(CHUNK - produced)
    if len(out) < out_size:
        out += bytes(out_size - len(out))
    return bytes(out[:out_size])


def compress_for_tests(data: bytes) -> bytes:
    """Minimal LZNT1 compressor (literal + simple matches) used by tests only."""
    result = bytearray()
    for base in range(0, len(data), CHUNK):
        block = data[base:base + CHUNK]
        tokens = bytearray()
        i = 0
        flags_pos = -1
        bit = 8
        while i < len(block):
            if bit == 8:
                flags_pos = len(tokens)
                tokens.append(0)
                bit = 0
            best_len = 0
            best_off = 0
            if i > 0:
                shift, mask = _SPLITS[i]
                max_off = min(i, (0xFFFF >> shift) + 1, 256)
                max_len = min(len(block) - i, mask + 3)
                for off in range(1, max_off + 1):
                    k = 0
                    while k < max_len and block[i + k] == block[i - off + k]:
                        k += 1
                    if k > best_len:
                        best_len, best_off = k, off
                        if k == max_len:
                            break
            if best_len >= 3:
                shift, mask = _SPLITS[i]
                token = ((best_off - 1) << shift) | (best_len - 3)
                tokens += bytes((token & 0xFF, token >> 8))
                tokens[flags_pos] |= 1 << bit
                i += best_len
            else:
                tokens.append(block[i])
                i += 1
            bit += 1
        if len(tokens) < len(block):
            header = 0x8000 | 0x3000 | (len(tokens) + 2 - 3)
            result += bytes((header & 0xFF, header >> 8)) + tokens
        else:
            header = 0x3000 | (len(block) + 2 - 3)
            result += bytes((header & 0xFF, header >> 8)) + block
    return bytes(result)
