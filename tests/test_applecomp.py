"""Apple's compression (LZFSE, LZVN, decmpfs) against data made by Apple's reference encoder."""

from __future__ import annotations

import hashlib
import json
import random
import struct
import zlib
from pathlib import Path

import pytest

from lifeboat.fs.applecomp import (
    CompressionError,
    chunk_table,
    decompress_chunk,
    lzfse_decompress,
    lzvn_decompress,
    parse_decmpfs,
)

VECTORS = Path(__file__).parent / "fixtures" / "compression"
INDEX = json.loads((VECTORS / "vectors.json").read_text(encoding="utf-8"))


def _decode(name: str, data: bytes) -> bytes:
    spec = INDEX[name]
    return lzfse_decompress(data) if spec["method"] == "lzfse" else lzvn_decompress(data, spec["size"])


@pytest.mark.parametrize("name", sorted(INDEX))
def test_reference_vectors(name):
    data = (VECTORS / f"{name}.bin").read_bytes()
    out = _decode(name, data)
    assert len(out) == INDEX[name]["size"]
    assert hashlib.sha256(out).hexdigest() == INDEX[name]["sha256"]


@pytest.mark.parametrize("name", sorted(INDEX))
def test_damaged_data_never_crashes(name):
    """A damaged piece must raise CompressionError (or decode to something), never anything else."""
    data = (VECTORS / f"{name}.bin").read_bytes()
    rng = random.Random(name)
    for _ in range(150):
        broken = bytearray(data)
        for _ in range(rng.randrange(1, 6)):
            broken[rng.randrange(len(broken))] = rng.randrange(256)
        if rng.random() < 0.3:
            del broken[rng.randrange(len(broken)):]
        try:
            _decode(name, bytes(broken))
        except CompressionError:
            pass


def test_decmpfs_header_and_inline_methods():
    text = b"lifeboat " * 300
    header = b"fpmc" + struct.pack("<IQ", 3, len(text))
    info = parse_decmpfs(header + zlib.compress(text))
    assert info is not None and info.method == "zlib" and info.size == len(text) and not info.in_resource_fork
    assert decompress_chunk("zlib", info.inline, info.size) == text
    assert decompress_chunk("zlib", b"\xff" + text, len(text)) == text      # stored, not compressed
    assert decompress_chunk("lzvn", b"\x06" + text, len(text)) == text
    assert parse_decmpfs(b"nope" + bytes(12)) is None
    assert parse_decmpfs(b"fpmc" + struct.pack("<IQ", 5, 10)).unsupported().startswith("Online-only")


def test_resource_fork_tables():
    # LZVN/LZFSE: a table of offsets, one more than there are 64 KiB pieces.
    size = 0x10000 + 5
    table = struct.pack("<3I", 12, 112, 150)
    assert chunk_table(8, table, size, 150) == [(12, 100), (112, 38)]
    with pytest.raises(CompressionError):
        chunk_table(8, table, size, 120)                    # points past the end of the fork
    # zlib: a classic resource fork holding a "cmpf" resource with (offset, length) pairs.
    head = bytearray(0x100 + 4 + 4 + 16)
    struct.pack_into(">I", head, 0, 0x100)
    struct.pack_into("<I", head, 0x104, 2)
    struct.pack_into("<4I", head, 0x108, 20, 30, 50, 7)
    assert chunk_table(4, bytes(head), size, 0x200) == [(0x104 + 20, 30), (0x104 + 50, 7)]
