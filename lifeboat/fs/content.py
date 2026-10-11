"""Read a file's bytes through the rescue reader, whatever its layout.

The result of every read says, for each byte range of the file, whether it
is OK (read or known to be zeros), UNREAD (not readable in this pass, may be
recovered by a later pass) or BAD (unreadable / location unknown).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..rescue.reader import ReadMode, RescueReader
from .applecomp import CompressionError, decompress_chunk
from .lznt1 import LZNT1Error, decompress
from .model import INVALID, SPARSE, Extent, FileLayout

OK = 0
UNREAD = 1
BAD = 2

STATE_NAMES = {OK: "ok", UNREAD: "unread", BAD: "bad"}


@dataclass
class ContentChunk:
    offset: int
    data: bytearray
    states: list[tuple[int, int, int]] = field(default_factory=list)  # file-relative

    def ranges(self, state: int) -> list[tuple[int, int]]:
        return [(s, e) for s, e, st in self.states if st == state]

    @property
    def complete(self) -> bool:
        return all(st == OK for _s, _e, st in self.states)


def normalize_states(states: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """Sort, drop empties and merge adjacent ranges with the same state."""
    ordered = sorted((s, e, st) for s, e, st in states if e > s)
    out: list[tuple[int, int, int]] = []
    for s, e, st in ordered:
        if out and out[-1][2] == st and out[-1][1] >= s:
            out[-1] = (out[-1][0], max(out[-1][1], e), st)
        else:
            out.append((s, e, st))
    return out


class FileContentReader:
    def __init__(self, layout: FileLayout, reader: RescueReader) -> None:
        self.layout = layout
        self.reader = reader
        self._piece: tuple[int, bytes] | None = None   # the last Apple-compressed piece decoded

    def read(self, start: int, end: int, mode: ReadMode = ReadMode.FAST) -> ContentChunk:
        size = self.layout.size
        start = max(0, start)
        end = min(end, size)
        chunk = ContentChunk(start, bytearray(max(0, end - start)))
        if end <= start:
            return chunk
        if self.layout.unsupported:
            chunk.states.append((start, end, BAD))
            return chunk
        if self.layout.resident is not None:
            data = self.layout.resident
            piece = data[start:end]
            chunk.data[: len(piece)] = piece
            chunk.states.append((start, end, OK))
            return chunk
        if self.layout.chunked is not None:
            self._read_chunked(chunk, start, end, mode)
        elif self.layout.compressed is not None:
            self._read_compressed(chunk, start, end, mode)
        else:
            self._read_extents(chunk, start, end, mode)
        valid = self.layout.valid_size
        if valid is not None and valid < end:
            # Bytes past the valid data length are zeros by definition.
            zero_from = max(start, valid)
            chunk.data[zero_from - start:] = bytes(end - zero_from)
            chunk.states = [(s, min(e, zero_from), st) for s, e, st in chunk.states if s < zero_from]
            chunk.states.append((zero_from, end, OK))
        chunk.states = normalize_states(chunk.states)
        return chunk

    # ----------------------------------------------------------------- extents
    def _read_extents(self, chunk: ContentChunk, start: int, end: int, mode: ReadMode) -> None:
        covered: list[tuple[int, int]] = []
        valid = self.layout.valid_size if self.layout.valid_size is not None else self.layout.size
        for ext in self.layout.extents:
            lo = max(start, ext.file_offset)
            hi = min(end, ext.file_end)
            if lo >= hi:
                continue
            covered.append((lo, hi))
            if ext.disk_offset == SPARSE:
                chunk.states.append((lo, hi, OK))
                continue
            if ext.disk_offset == INVALID or ext.disk_offset < 0:
                chunk.states.append((lo, hi, BAD))
                continue
            read_hi = min(hi, max(lo, valid))
            if read_hi > lo and self.layout.xts_key is not None and ext.tweak >= 0:
                self._read_encrypted(chunk, lo, read_hi, ext, mode)
            elif read_hi > lo:
                self._read_disk(chunk, lo, read_hi, ext.disk_offset + (lo - ext.file_offset), mode)
            if read_hi < hi:
                chunk.states.append((max(lo, read_hi), hi, OK))
        # Anything not described by an extent has no known location.
        covered.sort()
        pos = start
        for lo, hi in covered:
            if lo > pos:
                chunk.states.append((pos, lo, BAD))
            pos = max(pos, hi)
        if pos < end:
            chunk.states.append((pos, end, BAD))

    def _read_encrypted(self, chunk: ContentChunk, file_lo: int, file_hi: int, ext: Extent, mode: ReadMode) -> None:
        """Read part of an encrypted extent: whole 512-byte units are read and decrypted, then the
        wanted part is kept."""
        from .apfs_crypto import xts_decrypt

        assert self.layout.xts_key is not None
        rel_lo = (file_lo - ext.file_offset) // 512 * 512
        rel_hi = -(-(file_hi - ext.file_offset) // 512) * 512
        disk = ext.disk_offset + rel_lo
        outcome = self.reader.read(disk, rel_hi - rel_lo, mode)
        plain = xts_decrypt(self.layout.xts_key, bytes(outcome.data), ext.tweak + rel_lo // 512)
        cut = file_lo - (ext.file_offset + rel_lo)
        rel = file_lo - chunk.offset
        chunk.data[rel:rel + (file_hi - file_lo)] = plain[cut:cut + (file_hi - file_lo)]
        shift = file_lo - disk - cut
        for ranges, state in ((outcome.good, OK), (outcome.unread, UNREAD), (outcome.bad, BAD)):
            for s, e in ranges:
                lo, hi = max(s + shift, file_lo), min(e + shift, file_hi)
                if lo < hi:
                    chunk.states.append((lo, hi, state))

    def _read_disk(self, chunk: ContentChunk, file_lo: int, file_hi: int, disk: int, mode: ReadMode) -> None:
        outcome = self.reader.read(disk, file_hi - file_lo, mode)
        rel = file_lo - chunk.offset
        chunk.data[rel:rel + (file_hi - file_lo)] = outcome.data
        shift = file_lo - disk
        for s, e in outcome.good:
            chunk.states.append((s + shift, e + shift, OK))
        for s, e in outcome.unread:
            chunk.states.append((s + shift, e + shift, UNREAD))
        for s, e in outcome.bad:
            chunk.states.append((s + shift, e + shift, BAD))

    # -------------------------------------------------------------- compressed
    def _unit_pieces(self, unit_index: int) -> list[tuple[int, int]]:
        """(disk offset or SPARSE/INVALID, cluster count) covering one compression unit."""
        comp = self.layout.compressed
        assert comp is not None
        first_vcn = unit_index * comp.unit_clusters
        last_vcn = first_vcn + comp.unit_clusters
        pieces: list[tuple[int, int]] = []
        covered = first_vcn
        for vcn, disk, count in comp.runs:
            run_end = vcn + count
            if run_end <= first_vcn or vcn >= last_vcn:
                continue
            lo = max(vcn, first_vcn)
            hi = min(run_end, last_vcn)
            if lo > covered:
                pieces.append((SPARSE, lo - covered))
            if disk >= 0:
                pieces.append((disk + (lo - vcn) * comp.cluster_size, hi - lo))
            else:
                pieces.append((disk, hi - lo))
            covered = hi
        if covered < last_vcn:
            pieces.append((SPARSE, last_vcn - covered))
        return pieces

    def _read_compressed(self, chunk: ContentChunk, start: int, end: int, mode: ReadMode) -> None:
        comp = self.layout.compressed
        assert comp is not None
        cs = comp.cluster_size
        unit_bytes = cs * comp.unit_clusters
        first = start // unit_bytes
        last = (end - 1) // unit_bytes
        for unit in range(first, last + 1):
            u_start = unit * unit_bytes
            u_end = u_start + unit_bytes
            lo = max(start, u_start)
            hi = min(end, u_end)
            pieces = self._unit_pieces(unit)
            if any(disk == INVALID for disk, _c in pieces):
                chunk.states.append((lo, hi, BAD))
                continue
            allocated = sum(count for disk, count in pieces if disk >= 0)
            if allocated == 0:
                chunk.states.append((lo, hi, OK))  # sparse unit: zeros
                continue
            if allocated >= comp.unit_clusters:
                # Stored uncompressed: read the requested part directly.
                pos = u_start
                for disk, count in pieces:
                    piece_end = pos + count * cs
                    p_lo = max(lo, pos)
                    p_hi = min(hi, piece_end)
                    if p_lo < p_hi:
                        self._read_disk(chunk, p_lo, p_hi, disk + (p_lo - pos), mode)
                    pos = piece_end
                continue
            # Compressed unit: all of its stored clusters are needed.
            raw = bytearray()
            worst = 0
            for disk, count in pieces:
                if disk < 0:
                    continue
                outcome = self.reader.read(disk, count * cs, mode)
                if outcome.bad:
                    worst = max(worst, BAD)
                elif outcome.unread:
                    worst = max(worst, UNREAD)
                raw += outcome.data
            if worst:
                chunk.states.append((lo, hi, worst))
                continue
            try:
                plain = decompress(bytes(raw), unit_bytes)
            except LZNT1Error:
                chunk.states.append((lo, hi, BAD))
                continue
            rel = lo - chunk.offset
            chunk.data[rel:rel + (hi - lo)] = plain[lo - u_start:hi - u_start]
            chunk.states.append((lo, hi, OK))

    # ------------------------------------------------------- Apple compressed
    def _plain_piece(self, index: int, mode: ReadMode) -> tuple[bytes | None, int]:
        """Decompressed piece ``index``: (bytes or None, state when it could not be had)."""
        comp = self.layout.chunked
        assert comp is not None
        if self._piece is not None and self._piece[0] == index:
            return self._piece[1], OK
        size = self.layout.size
        expected = min(comp.chunk_size, size - index * comp.chunk_size)
        if comp.inline is not None:
            raw = comp.inline
        else:
            if comp.source is None or index >= len(comp.chunks):
                return None, BAD
            off, length = comp.chunks[index]
            got = FileContentReader(comp.source, self.reader).read(off, off + length, mode)
            states = [st for _s, _e, st in got.states]
            if BAD in states:
                return None, BAD
            if UNREAD in states:
                return None, UNREAD
            raw = bytes(got.data)
        try:
            plain = decompress_chunk(comp.method, raw, expected)
        except CompressionError:
            return None, BAD
        self._piece = (index, plain)
        return plain, OK

    def _read_chunked(self, chunk: ContentChunk, start: int, end: int, mode: ReadMode) -> None:
        comp = self.layout.chunked
        assert comp is not None
        if comp.error:
            chunk.states.append((start, end, BAD))
            return
        unit = comp.chunk_size if comp.inline is None else max(1, self.layout.size)
        for index in range(start // unit, (end - 1) // unit + 1):
            u_start = index * unit
            lo = max(start, u_start)
            hi = min(end, u_start + unit)
            plain, state = self._plain_piece(index, mode)
            if plain is None:
                chunk.states.append((lo, hi, state))
                continue
            rel = lo - chunk.offset
            chunk.data[rel:rel + (hi - lo)] = plain[lo - u_start:hi - u_start]
            chunk.states.append((lo, hi, OK))
