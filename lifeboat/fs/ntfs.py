"""NTFS: read the Master File Table and rebuild the folder tree.

The tree is rebuilt from the parent reference stored in every file record
(not from directory indexes), which also finds deleted files and folders.
Records whose parent no longer exists end up in "Lost files", grouped by
their original parent folder number.

Damage handling:

* the boot sector falls back to the backup copy at the end of the volume;
* MFT record 0 falls back to $MFTMirr; if both are unreadable the MFT is
  assumed contiguous from its start cluster;
* MFT areas that cannot be read in the fast pass are re-read more
  thoroughly within a time budget; records that stay unreadable are
  counted and reported;
* every record is bounds-checked; corrupt attributes are skipped.
"""

from __future__ import annotations

import struct
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..errors import E_BOOT_BACKUP, E_META_CORRUPT, E_META_UNREADABLE, FilesystemError
from ..events import EventBus, JobControl, Progress
from ..rescue.reader import ReadMode, RescueReader
from ..util import filetime_to_unix, format_size
from .model import INVALID, SPARSE, CompressedRuns, Extent, F, FileLayout, Node, Volume

ROOT_RECORD = 5
EXTEND_RECORD = 11
FIRST_USER_RECORD = 24

ATTR_STANDARD_INFORMATION = 0x10
ATTR_ATTRIBUTE_LIST = 0x20
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80
ATTR_REPARSE_POINT = 0xC0
ATTR_END = 0xFFFFFFFF

ATTR_FLAG_COMPRESSED = 0x0001
ATTR_FLAG_ENCRYPTED = 0x4000
ATTR_FLAG_SPARSE = 0x8000

REC_IN_USE = 0x0001
REC_DIRECTORY = 0x0002

FILE_ATTR_READONLY = 0x0001
FILE_ATTR_HIDDEN = 0x0002
FILE_ATTR_SYSTEM = 0x0004

REPARSE_WOF = 0x80000017
REPARSE_DEDUP = 0x80000013
REPARSE_SYMLINK = 0xA000000C
REPARSE_MOUNT_POINT = 0xA0000003

NS_POSIX, NS_WIN32, NS_DOS, NS_WIN32_DOS = 0, 1, 2, 3

_REC_HDR = struct.Struct("<4sHHQHHHHIIQHHI")
_ATTR_HDR = struct.Struct("<IIBBHHH")
_NONRES = struct.Struct("<QQHH4xQQQ")
_FN = struct.Struct("<QQQQQQQIIBB")
_SI = struct.Struct("<QQQQI")


@dataclass
class NtfsBoot:
    bytes_per_sector: int
    sectors_per_cluster: int
    cluster_size: int
    total_sectors: int
    mft_lcn: int
    mftmirr_lcn: int
    record_size: int
    index_size: int
    serial: int

    @property
    def volume_size(self) -> int:
        # The backup boot sector sits in the sector after the counted ones.
        return (self.total_sectors + 1) * self.bytes_per_sector

    @classmethod
    def parse(cls, sector: bytes) -> NtfsBoot | None:
        if len(sector) < 512 or sector[3:11] != b"NTFS    ":
            return None
        if sector[510:512] != b"\x55\xaa":
            return None
        bps = struct.unpack_from("<H", sector, 0x0B)[0]
        if bps not in (256, 512, 1024, 2048, 4096):
            return None
        spc_raw = sector[0x0D]
        if spc_raw == 0:
            return None
        if spc_raw <= 0x80:
            spc = spc_raw
        else:
            shift = 256 - spc_raw
            if shift > 20:
                return None
            spc = 1 << shift
        if spc & (spc - 1):
            return None
        cluster = bps * spc
        total = struct.unpack_from("<Q", sector, 0x28)[0]
        mft = struct.unpack_from("<Q", sector, 0x30)[0]
        mirr = struct.unpack_from("<Q", sector, 0x38)[0]
        cpr = struct.unpack_from("<b", sector, 0x40)[0]
        cpi = struct.unpack_from("<b", sector, 0x44)[0]
        record = cpr * cluster if cpr > 0 else (1 << -cpr if -31 < cpr < 0 else 0)
        index = cpi * cluster if cpi > 0 else (1 << -cpi if -31 < cpi < 0 else 0)
        if record < 256 or record > 65536 or record & (record - 1):
            return None
        if total == 0:
            return None
        clusters = total // spc
        if mft == 0 or mft >= clusters or mirr >= clusters:
            return None
        serial = struct.unpack_from("<Q", sector, 0x48)[0]
        return cls(bps, spc, cluster, total, mft, mirr, record, index, serial)


def apply_fixups(buf: bytearray, off: int, size: int) -> int:
    """Apply the update sequence array in place.

    Returns the number of 512-byte strides whose check value did not match
    (0 = intact record) or -1 if the record header is structurally invalid.
    """
    usa_ofs, usa_cnt = struct.unpack_from("<HH", buf, off + 4)
    strides = size // 512
    if usa_cnt < 2 or usa_cnt - 1 > strides or usa_ofs < 0x28 or usa_ofs + 2 * usa_cnt > size:
        return -1
    check = bytes(buf[off + usa_ofs:off + usa_ofs + 2])
    mismatches = 0
    for i in range(1, usa_cnt):
        pos = off + i * 512 - 2
        if buf[pos:pos + 2] != check:
            mismatches += 1
        src = off + usa_ofs + 2 * i
        buf[pos:pos + 2] = buf[src:src + 2]
    return mismatches


def decode_runs(raw: bytes, start_vcn: int = 0) -> tuple[list[tuple[int, int | None, int]], bool]:
    """Decode NTFS mapping pairs.  Returns ([(vcn, lcn or None, count)], ok)."""
    runs: list[tuple[int, int | None, int]] = []
    pos = 0
    lcn = 0
    vcn = start_vcn
    end = len(raw)
    while pos < end:
        header = raw[pos]
        if header == 0:
            return runs, True
        len_size = header & 0x0F
        off_size = header >> 4
        pos += 1
        if len_size == 0 or len_size > 8 or off_size > 8 or pos + len_size + off_size > end:
            return runs, False
        count = int.from_bytes(raw[pos:pos + len_size], "little")
        pos += len_size
        if count <= 0:
            return runs, False
        if off_size == 0:
            runs.append((vcn, None, count))
        else:
            lcn += int.from_bytes(raw[pos:pos + off_size], "little", signed=True)
            pos += off_size
            if lcn < 0:
                return runs, False
            runs.append((vcn, lcn, count))
        vcn += count
    return runs, False  # no terminating zero


class DataAttr:
    """One $DATA stream: resident bytes or a list of run segments."""

    __slots__ = ("resident", "segments", "size", "valid", "alloc", "flags", "cu")

    def __init__(self) -> None:
        self.resident: bytes | None = None
        self.segments: list[tuple[int, bytes]] = []
        self.size = 0
        self.valid = 0
        self.alloc = 0
        self.flags = 0
        self.cu = 0


class Record:
    __slots__ = ("num", "seq", "flags", "names", "si", "data", "streams", "reparse", "damaged",
                 "has_list")

    def __init__(self, num: int, seq: int, flags: int) -> None:
        self.num = num
        self.seq = seq
        self.flags = flags
        # (parent number, parent seq, name, namespace, ctime, mtime, atime, size)
        self.names: list[tuple[int, int, str, int, int, int, int, int]] = []
        self.si: tuple[int, int, int, int] | None = None
        self.data: DataAttr | None = None
        self.streams: dict[str, DataAttr] | None = None
        self.reparse = 0
        self.damaged = False
        self.has_list = False

    @property
    def in_use(self) -> bool:
        return bool(self.flags & REC_IN_USE)

    @property
    def is_dir(self) -> bool:
        return bool(self.flags & REC_DIRECTORY)


def parse_record(buf: bytearray, off: int, size: int, num: int) -> tuple[Record | None, int, int]:
    """Parse the record at ``buf[off:off+size]`` (fixups already applied).

    Returns (record, base record number, base sequence).  For extension
    records the base number is non-zero and the attributes belong to it.
    """
    try:
        (sig, _usa_ofs, _usa_cnt, _lsn, seq, _links, attr_ofs, flags, used, _alloc, base,
         _next, _pad, _recnum) = _REC_HDR.unpack_from(buf, off)
    except struct.error:
        return None, 0, 0
    if sig != b"FILE":
        return None, 0, 0
    base_num = base & 0xFFFFFFFFFFFF
    base_seq = base >> 48
    rec = Record(num, seq, flags)
    limit = off + min(max(used, 0), size) if 0 < used <= size else off + size
    pos = off + attr_ofs
    if attr_ofs < 0x30 - 8 or attr_ofs >= size:
        rec.damaged = True
        return rec, base_num, base_seq
    while pos + 16 <= limit:
        atype, alen, nonres, name_len, name_off, aflags, _aid = _ATTR_HDR.unpack_from(buf, pos)
        if atype == ATTR_END:
            break
        if alen < 16 or pos + alen > limit or alen & 7:
            rec.damaged = True
            break
        name = ""
        if name_len:
            if name_off + 2 * name_len > alen:
                rec.damaged = True
                pos += alen
                continue
            name = bytes(buf[pos + name_off:pos + name_off + 2 * name_len]).decode("utf-16-le", "replace")
        if nonres:
            if alen < 0x40:
                rec.damaged = True
                pos += alen
                continue
            if atype == ATTR_DATA:
                start_vcn, _last_vcn, runs_off, cu, alloc, dsize, valid = _NONRES.unpack_from(buf, pos + 16)
                if runs_off >= alen:
                    rec.damaged = True
                    pos += alen
                    continue
                attr = _data_attr(rec, name)
                attr.segments.append((start_vcn, bytes(buf[pos + runs_off:pos + alen])))
                if start_vcn == 0:
                    attr.size = dsize
                    attr.valid = valid
                    attr.alloc = alloc
                    attr.flags = aflags
                    attr.cu = cu
            elif atype == ATTR_ATTRIBUTE_LIST:
                rec.has_list = True
        else:
            vlen, voff = struct.unpack_from("<IH", buf, pos + 16)
            if voff + vlen > alen:
                rec.damaged = True
                pos += alen
                continue
            vstart = pos + voff
            if atype == ATTR_STANDARD_INFORMATION and vlen >= 36:
                ctime, mtime, _mft, atime, fattr = _SI.unpack_from(buf, vstart)
                rec.si = (ctime, mtime, atime, fattr)
            elif atype == ATTR_FILE_NAME and vlen >= 66:
                (parent, ctime, mtime, _mft, atime, _alloc, fsize, _fflags, _reparse, nlen,
                 ns) = _FN.unpack_from(buf, vstart)
                if 66 + 2 * nlen <= vlen and nlen:
                    fname = bytes(buf[vstart + 66:vstart + 66 + 2 * nlen]).decode("utf-16-le", "replace")
                    rec.names.append((parent & 0xFFFFFFFFFFFF, parent >> 48, fname, ns, ctime, mtime, atime, fsize))
            elif atype == ATTR_DATA:
                attr = _data_attr(rec, name)
                attr.resident = bytes(buf[vstart:vstart + vlen])
                attr.size = vlen
                attr.valid = vlen
                attr.flags = aflags
            elif atype == ATTR_REPARSE_POINT and vlen >= 4:
                rec.reparse = struct.unpack_from("<I", buf, vstart)[0]
            elif atype == ATTR_ATTRIBUTE_LIST:
                rec.has_list = True
        pos += alen
    return rec, base_num, base_seq


def _data_attr(rec: Record, name: str) -> DataAttr:
    if not name:
        if rec.data is None:
            rec.data = DataAttr()
        return rec.data
    if rec.streams is None:
        rec.streams = {}
    attr = rec.streams.get(name)
    if attr is None:
        attr = rec.streams[name] = DataAttr()
    return attr


def _merge_into(base: Record, ext: Record) -> None:
    base.names.extend(ext.names)
    if base.si is None and ext.si is not None:
        base.si = ext.si
    if ext.reparse and not base.reparse:
        base.reparse = ext.reparse
    for name, attr in ([("", ext.data)] if ext.data else []) + list((ext.streams or {}).items()):
        target = _data_attr(base, name)
        if attr.resident is not None and target.resident is None and not target.segments:
            target.resident = attr.resident
        target.segments.extend(attr.segments)
        if any(vcn == 0 for vcn, _ in attr.segments):
            target.size, target.valid, target.alloc = attr.size, attr.valid, attr.alloc
            target.flags, target.cu = attr.flags, attr.cu
    if ext.damaged:
        base.damaged = True


class NtfsVolume(Volume):
    kind = "NTFS"

    def __init__(
        self,
        reader: RescueReader,
        offset: int,
        boot: NtfsBoot,
        events: EventBus | None = None,
        control: JobControl | None = None,
        metadata_retry_seconds: float = 180.0,
    ) -> None:
        super().__init__(reader, offset, boot.volume_size)
        self.boot = boot
        self.cluster_size = boot.cluster_size
        self.record_size = boot.record_size
        self.events = events
        self.control = control
        self.metadata_retry_seconds = metadata_retry_seconds
        self.serial = f"{boot.serial:016X}"
        self.records: dict[int, Record] = {}
        self.mft_extents: list[tuple[int, int, int]] = []  # (first record, disk offset, record count)
        self.unreadable_records = 0
        self.total_records = 0
        self.bitmap: bytes | None = None
        self.mft_assumed = False
        self._problem_records: set[int] = set()

    # ------------------------------------------------------------- utilities
    def _warn(self, message: str, code: str = E_META_CORRUPT) -> None:
        self.warnings.append(message)
        if self.events is not None:
            self.events.warning(message, code=code, source="ntfs")

    def _check(self) -> None:
        if self.control is not None:
            self.control.check()

    def lcn_to_disk(self, lcn: int) -> int:
        return self.offset + lcn * self.cluster_size

    def _read_record_at(self, disk_offset: int, num: int, critical: bool = True) -> Record | None:
        size = self.record_size
        outcome = (self.reader.read_critical(disk_offset, size) if critical
                   else self.reader.read(disk_offset, size))
        buf = outcome.data
        if buf[:4] != b"FILE":
            return None
        if apply_fixups(buf, 0, size) < 0:
            return None
        rec, _base, _seq = parse_record(buf, 0, size, num)
        return rec

    # ---------------------------------------------------------------- the MFT
    def _mft_runs_from(self, rec: Record) -> list[tuple[int, int | None, int]]:
        if rec.data is None or not rec.data.segments:
            return []
        runs: list[tuple[int, int | None, int]] = []
        for start_vcn, raw in sorted(rec.data.segments):
            decoded, _ok = decode_runs(raw, start_vcn)
            runs.extend(decoded)
        return runs

    def _locate_mft(self) -> None:
        boot = self.boot
        mft_disk = self.lcn_to_disk(boot.mft_lcn)
        rec0 = self._read_record_at(mft_disk, 0)
        source = "$MFT"
        if rec0 is None or not self._mft_runs_from(rec0):
            mirror = self._read_record_at(self.lcn_to_disk(boot.mftmirr_lcn), 0)
            if mirror is not None and self._mft_runs_from(mirror):
                rec0 = mirror
                source = "$MFTMirr"
                self._warn("The first MFT record is damaged; using its mirror copy ($MFTMirr).",
                           E_BOOT_BACKUP)
        runs: list[tuple[int, int | None, int]] = []
        size = 0
        if rec0 is not None:
            runs = self._mft_runs_from(rec0)
            size = rec0.data.size if rec0.data else 0
            if rec0.has_list:
                runs, size = self._extend_mft_runs(rec0, runs, size)
        if not runs:
            self._warn(
                "The MFT location could not be read; assuming it is stored in one piece. "
                "Some files may be missing - run a Deep Scan for more.",
                E_META_UNREADABLE,
            )
            clusters = max(1, (self.size - boot.mft_lcn * self.cluster_size) // self.cluster_size)
            runs = [(0, boot.mft_lcn, clusters)]
            size = 0
            self.mft_assumed = True
        self._set_mft_extents(runs, size)
        if self.events is not None:
            self.events.debug(f"MFT read from {source}: {len(self.mft_extents)} fragment(s)")

    def _extend_mft_runs(self, rec0: Record, runs: list[tuple[int, int | None, int]],
                         size: int) -> tuple[list[tuple[int, int | None, int]], int]:
        """Follow $ATTRIBUTE_LIST of a very fragmented MFT to collect all runs."""
        # Read the attribute list of record 0 again, raw, to get extension refs.
        outcome = self.reader.read_critical(self.lcn_to_disk(self.boot.mft_lcn), self.record_size)
        buf = outcome.data
        if buf[:4] != b"FILE" or apply_fixups(buf, 0, self.record_size) < 0:
            return runs, size
        refs = _attribute_list_refs(buf, self.record_size, self.reader, self)
        seen = {0}
        for ref in refs:
            if ref in seen:
                continue
            seen.add(ref)
            self._set_mft_extents(runs, size)
            disk = self._record_disk_offset(ref)
            if disk is None:
                continue
            ext = self._read_record_at(disk, ref)
            if ext is None or ext.data is None:
                continue
            for start_vcn, raw in ext.data.segments:
                decoded, _ok = decode_runs(raw, start_vcn)
                runs.extend(decoded)
            if any(vcn == 0 for vcn, _ in ext.data.segments):
                size = ext.data.size
        runs.sort(key=lambda r: r[0])
        return runs, size

    def _set_mft_extents(self, runs: list[tuple[int, int | None, int]], size: int) -> None:
        rs = self.record_size
        cs = self.cluster_size
        extents: list[tuple[int, int, int]] = []
        for vcn, lcn, count in sorted(runs, key=lambda r: r[0]):
            if lcn is None:
                continue
            byte_start = vcn * cs
            first_rec = -(-byte_start // rs)
            disk = self.lcn_to_disk(lcn) + (first_rec * rs - byte_start)
            n = (byte_start + count * cs) // rs - first_rec
            if n > 0:
                extents.append((first_rec, disk, n))
        if size:
            limit = size // rs
            clipped = []
            for first, disk, n in extents:
                if first >= limit:
                    continue
                clipped.append((first, disk, min(n, limit - first)))
            extents = clipped
        self.mft_extents = extents

    def _record_disk_offset(self, num: int) -> int | None:
        for first, disk, n in self.mft_extents:
            if first <= num < first + n:
                return disk + (num - first) * self.record_size
        return None

    def _parse_buffer(self, buf: bytearray, first_num: int, count: int, bad: list[tuple[int, int]],
                      ext_records: dict[int, dict[int, tuple[int, Record]]],
                      only: set[int] | None = None) -> int:
        """Parse ``count`` records; ``bad`` are buffer-relative unreadable ranges.

        Returns how many slots held a FILE record.  With ``only`` set, just
        those record numbers are (re)parsed - used when retrying damaged areas.
        """
        rs = self.record_size
        found = 0
        bad_sorted = sorted(bad)
        for i in range(count):
            num = first_num + i
            if only is not None and num not in only:
                continue
            off = i * rs
            damaged = any(b_start < off + rs and b_end > off for b_start, b_end in bad_sorted)
            if buf[off:off + 4] != b"FILE":
                if damaged:
                    self._problem_records.add(num)
                continue
            found += 1
            mismatches = apply_fixups(buf, off, rs)
            if mismatches < 0:
                self._problem_records.add(num)
                continue
            rec, base_num, base_seq = parse_record(buf, off, rs, num)
            if rec is None:
                continue
            if mismatches or damaged:
                rec.damaged = True
                self._problem_records.add(num)
            else:
                self._problem_records.discard(num)
            if base_num and base_num != num:
                ext_records.setdefault(base_num, {})[num] = (base_seq, rec)
            elif rec.names or rec.num == ROOT_RECORD:
                self.records[num] = rec
        return found

    def load(self, progress: Callable[[Progress], None] | None = None) -> Node:
        self._locate_mft()
        total = sum(n for _f, _d, n in self.mft_extents)
        self.total_records = total
        rs = self.record_size
        ext_records: dict[int, dict[int, tuple[int, Record]]] = {}
        chunk_records = max(1, (4 << 20) // rs)
        done = 0
        pending: list[tuple[int, int, int]] = []  # (first record, disk offset, count) with problems
        last_report = 0.0
        empty_run = 0
        for first, disk, n in self.mft_extents:
            index = 0
            while index < n:
                self._check()
                count = min(chunk_records, n - index)
                outcome = self.reader.read(disk + index * rs, count * rs, ReadMode.FAST)
                problems = [(s - outcome.offset, e - outcome.offset) for s, e in outcome.unread + outcome.bad]
                if outcome.unread:
                    pending.append((first + index, disk + index * rs, count))
                found = self._parse_buffer(outcome.data, first + index, count, problems, ext_records)
                index += count
                done += count
                if self.mft_assumed:
                    # Without the MFT's own size, stop after 16 MiB without records.
                    empty_run = 0 if found or problems else empty_run + count
                    if empty_run * rs >= 16 << 20:
                        total = done
                        break
                now = time.monotonic()
                if progress is not None and now - last_report > 0.25:
                    last_report = now
                    progress(Progress("Reading file table (MFT)", done, total, items_done=done,
                                      items_total=total, item=f"{done:,} of {total:,} records"))
        if self.mft_assumed:
            self.mft_extents = [(f, d, min(n, done)) for f, d, n in self.mft_extents]
            self.total_records = done
        if pending:
            self._retry_pending(pending, ext_records, progress)
        self.unreadable_records = self._count_unreadable()
        if self.unreadable_records:
            self._warn(
                f"{self.unreadable_records:,} file records ({format_size(self.unreadable_records * rs)}) "
                "of the MFT are unreadable; files they described may be missing.",
                E_META_UNREADABLE,
            )
        self._merge_extensions(ext_records)
        self._load_bitmap()
        return self._build_tree(progress)

    def _retry_pending(self, pending: list[tuple[int, int, int]],
                       ext_records: dict[int, dict[int, tuple[int, Record]]],
                       progress: Callable[[Progress], None] | None) -> None:
        rs = self.record_size
        deadline = time.monotonic() + self.metadata_retry_seconds
        for mode in (ReadMode.SWEEP, ReadMode.TRIM, ReadMode.SCRAPE):
            if time.monotonic() > deadline:
                self._warn("Stopped retrying unreadable MFT areas after the time limit.", E_META_UNREADABLE)
                break
            still: list[tuple[int, int, int]] = []
            for idx, (first, disk, count) in enumerate(pending):
                self._check()
                if time.monotonic() > deadline:
                    still.extend(pending[idx:])
                    break
                if progress is not None:
                    progress(Progress("Re-reading damaged parts of the MFT", idx, len(pending),
                                      item=f"records {first:,}-{first + count - 1:,}"))
                outcome = self.reader.read(disk, count * rs, mode)
                problems = [(s - outcome.offset, e - outcome.offset) for s, e in outcome.unread + outcome.bad]
                self._parse_buffer(outcome.data, first, count, problems, ext_records,
                                   only=set(self._problem_records))
                if outcome.unread:
                    still.append((first, disk, count))
            pending = still
            if not pending:
                break

    def _count_unreadable(self) -> int:
        rs = self.record_size
        count = 0
        from ..rescue.sectormap import State

        for _first, disk, n in self.mft_extents:
            for start, end in self.reader.map.ranges([State.BAD, State.FAILED, State.SKIPPED],
                                                     disk, disk + n * rs):
                first_rec = (start - disk) // rs
                last_rec = (end - 1 - disk) // rs
                count += last_rec - first_rec + 1
        return count

    def _merge_extensions(self, ext_records: dict[int, dict[int, tuple[int, Record]]]) -> None:
        for base_num, items in ext_records.items():
            base = self.records.get(base_num)
            if base is None:
                continue
            for _ext_num, (base_seq, ext) in sorted(items.items()):
                if base_seq == base.seq or (not base.in_use and base_seq + 1 == base.seq):
                    _merge_into(base, ext)

    def _load_bitmap(self) -> None:
        rec = self.records.get(6)
        if rec is None or rec.data is None:
            return
        layout = self._layout_for(rec.data, rec)
        if layout.unsupported or layout.size > 512 << 20:
            return
        from .content import BAD, FileContentReader

        try:
            chunk = FileContentReader(layout, self.reader).read(0, layout.size, ReadMode.FAST)
        except FilesystemError:
            return
        if any(st == BAD for _s, _e, st in chunk.states):
            return
        self.bitmap = bytes(chunk.data)

    def _allocated_fraction(self, layout: FileLayout) -> float:
        if self.bitmap is None:
            return 0.0
        cs = self.cluster_size
        total = 0
        used = 0
        for start, end in layout.disk_ranges():
            lcn = (start - self.offset) // cs
            count = max(1, -(-(end - start) // cs))
            total += count
            byte_lo = lcn // 8
            byte_hi = (lcn + count + 7) // 8
            if byte_hi > len(self.bitmap):
                continue
            bits = int.from_bytes(self.bitmap[byte_lo:byte_hi], "little") >> (lcn % 8)
            used += (bits & ((1 << count) - 1)).bit_count()
        return used / total if total else 0.0

    # ------------------------------------------------------------- the tree
    def _build_tree(self, progress: Callable[[Progress], None] | None) -> Node:
        records = self.records
        root_rec = records.get(ROOT_RECORD)
        root = Node("", F.DIR | F.VOLUME | F.VIRTUAL, volume=self, ref=root_rec)
        if root_rec is not None and root_rec.si is not None:
            root.ctime = filetime_to_unix(root_rec.si[0])
            root.mtime = filetime_to_unix(root_rec.si[1])
        dir_nodes: dict[int, Node] = {ROOT_RECORD: root}
        pending: list[tuple[Node, Record, int, int]] = []
        total = len(records)
        for index, rec in enumerate(records.values()):
            if index % 50000 == 0:
                self._check()
                if progress is not None:
                    progress(Progress("Rebuilding folders", index, total))
            if rec.num == ROOT_RECORD:
                continue
            names = _primary_names(rec.names)
            if len(names) > 1 and not rec.is_dir:
                hardlink = F.HARDLINK
            else:
                hardlink = 0
            for parent_num, parent_seq, name, _ns, fctime, fmtime, fatime, fsize in names:
                node = self._make_node(rec, name, fctime, fmtime, fatime, fsize)
                node.flags |= hardlink
                if rec.is_dir and rec.num not in dir_nodes:
                    dir_nodes[rec.num] = node
                pending.append((node, rec, parent_num, parent_seq))
                if rec.streams:
                    for sname, sattr in rec.streams.items():
                        stream = Node(f"{name}:{sname}", F.STREAM | F.HIDDEN,
                                      size=sattr.size, volume=self, ref=(rec, sname))
                        stream.ctime, stream.mtime, stream.atime = node.ctime, node.mtime, node.atime
                        if not rec.in_use:
                            stream.flags |= F.DELETED
                        pending.append((stream, rec, parent_num, parent_seq))
        lost = Node("Lost files", F.DIR | F.VIRTUAL, volume=self)
        lost_groups: dict[int, Node] = {}
        for node, rec, parent_num, parent_seq in pending:
            parent = self._resolve_parent(rec, parent_num, parent_seq, dir_nodes)
            if parent is None:
                group = lost_groups.get(parent_num)
                if group is None:
                    group = Node(f"Folder #{parent_num}", F.DIR | F.VIRTUAL | F.ORPHAN, volume=self)
                    lost_groups[parent_num] = group
                    lost.add(group)
                node.flags |= F.ORPHAN
                group.add(node)
            else:
                parent.add(node)
        self._break_cycles(root, dir_nodes, lost)
        for node in root.walk():
            if node.flags & F.SYSTEM and node.children:
                for item in node.walk():
                    item.flags |= F.SYSTEM
        if lost.children:
            root.add(lost)
        self.root = root
        return root

    def _resolve_parent(self, rec: Record, parent_num: int, parent_seq: int,
                        dir_nodes: dict[int, Node]) -> Node | None:
        parent_rec = self.records.get(parent_num)
        node = dir_nodes.get(parent_num)
        if node is None:
            return None
        if parent_num == ROOT_RECORD:
            return node
        if parent_rec is None or not parent_rec.is_dir:
            return None
        if parent_rec.seq == parent_seq:
            return node
        if not parent_rec.in_use and parent_rec.seq == parent_seq + 1:
            return node
        if parent_seq == 0:  # very old NTFS versions did not store sequence numbers
            return node
        return None

    def _break_cycles(self, root: Node, dir_nodes: dict[int, Node], lost: Node) -> None:
        rooted: set[int] = {id(root)}
        for node in list(dir_nodes.values()):
            chain = []
            cursor: Node | None = node
            seen: set[int] = set()
            while cursor is not None and id(cursor) not in rooted:
                if id(cursor) in seen:
                    # Cycle: detach the cursor into Lost files.
                    parent = cursor.parent
                    if parent is not None and parent.children is not None:
                        parent.children.remove(cursor)
                    cursor.flags |= F.ORPHAN
                    lost.add(cursor)
                    break
                seen.add(id(cursor))
                chain.append(cursor)
                cursor = cursor.parent
            rooted.update(id(item) for item in chain)

    def _make_node(self, rec: Record, name: str, fctime: int, fmtime: int, fatime: int,
                   fsize: int) -> Node:
        flags = 0
        if rec.is_dir:
            flags |= F.DIR
        if not rec.in_use:
            flags |= F.DELETED
        if rec.damaged:
            flags |= F.DAMAGED_META
        if rec.num < FIRST_USER_RECORD or (name.startswith("$") and rec.num < 64):
            flags |= F.SYSTEM
        if rec.si is not None:
            ctime, mtime, atime, fattr = rec.si
            if fattr & FILE_ATTR_HIDDEN:
                flags |= F.HIDDEN
            if fattr & FILE_ATTR_SYSTEM:
                flags |= F.HIDDEN
            if fattr & FILE_ATTR_READONLY:
                flags |= F.READONLY
        else:
            ctime, mtime, atime = fctime, fmtime, fatime
        data = rec.data
        size = 0
        if not rec.is_dir:
            if data is not None:
                size = data.size
                if data.resident is not None:
                    flags |= F.RESIDENT
                if data.flags & ATTR_FLAG_COMPRESSED:
                    flags |= F.COMPRESSED
                if data.flags & ATTR_FLAG_ENCRYPTED:
                    flags |= F.ENCRYPTED
                if data.flags & ATTR_FLAG_SPARSE:
                    flags |= F.SPARSE
            else:
                size = fsize
                if fsize:
                    flags |= F.DAMAGED_META
        if rec.reparse:
            flags |= F.REPARSE
            if rec.reparse in (REPARSE_WOF, REPARSE_DEDUP) or (rec.reparse & 0xFFFF0FFF) == 0x9000001A:
                flags |= F.UNSUPPORTED
        node = Node(name, flags, size, volume=self, ref=rec,
                    ctime=filetime_to_unix(ctime), mtime=filetime_to_unix(mtime),
                    atime=filetime_to_unix(atime))
        if flags & F.DELETED and not rec.is_dir and data is not None and data.resident is None:
            layout = self._layout_for(data, rec)
            if self._allocated_fraction(layout) > 0:
                node.flags |= F.OVERWRITTEN
        return node

    # ------------------------------------------------------------------ data
    def layout(self, node: Node) -> FileLayout:
        ref = node.ref
        if isinstance(ref, tuple):
            rec, stream = ref
            attr = (rec.streams or {}).get(stream)
        else:
            rec = ref
            attr = rec.data if rec is not None else None
        if rec is None:
            return FileLayout(0)
        if attr is None:
            layout = FileLayout(node.size)
            if node.size:
                layout.problems.append("The data location of this file was lost.")
            return layout
        if attr.size == 0 and attr.segments and attr.resident is None and node.size > 0:
            # The first part of the location list was lost; use the size from the name entry.
            attr.size = attr.valid = attr.alloc = node.size
        return self._layout_for(attr, rec)

    def _layout_for(self, attr: DataAttr, rec: Record) -> FileLayout:
        size = attr.size
        if attr.flags & ATTR_FLAG_ENCRYPTED:
            return FileLayout(size, unsupported="Encrypted with Windows EFS; cannot be decrypted.")
        if rec.reparse == REPARSE_WOF:
            return FileLayout(size, unsupported="Windows system-compressed file (WOF); not supported.")
        if rec.reparse == REPARSE_DEDUP:
            return FileLayout(size, unsupported="Stored by Windows Data Deduplication; not supported.")
        if (rec.reparse & 0xFFFF0FFF) == 0x9000001A and not attr.segments and attr.resident is None:
            return FileLayout(size, unsupported="Online-only OneDrive file; no data on this drive.")
        if attr.resident is not None and not attr.segments:
            return FileLayout(size, resident=attr.resident[:size])
        cs = self.cluster_size
        runs: list[tuple[int, int | None, int]] = []
        problems: list[str] = []
        for start_vcn, raw in sorted(attr.segments):
            decoded, ok = decode_runs(raw, start_vcn)
            if not ok:
                problems.append("Part of the file's location list is damaged.")
            runs.extend(decoded)
        runs.sort(key=lambda r: r[0])
        volume_clusters = self.size // cs
        layout = FileLayout(size, valid_size=min(attr.valid, size) if attr.valid else 0, problems=problems)
        if attr.flags & ATTR_FLAG_COMPRESSED and attr.cu:
            comp_runs: list[tuple[int, int, int]] = []
            for vcn, lcn, count in runs:
                if lcn is None:
                    comp_runs.append((vcn, SPARSE, count))
                elif lcn + count > volume_clusters:
                    comp_runs.append((vcn, INVALID, count))
                else:
                    comp_runs.append((vcn, self.lcn_to_disk(lcn), count))
            layout.compressed = CompressedRuns(cs, 1 << attr.cu, comp_runs)
            return layout
        extents: list[Extent] = []
        for vcn, lcn, count in runs:
            file_offset = vcn * cs
            if file_offset >= size:
                break
            length = min(count * cs, size - file_offset)
            if lcn is None:
                extents.append(Extent(file_offset, length, SPARSE))
            elif lcn + count > volume_clusters:
                extents.append(Extent(file_offset, length, INVALID))
                problems.append("Part of the file points outside the volume.")
            else:
                extents.append(Extent(file_offset, length, self.lcn_to_disk(lcn)))
        layout.extents = extents
        return layout


def _primary_names(names: list[tuple[int, int, str, int, int, int, int, int]]
                   ) -> list[tuple[int, int, str, int, int, int, int, int]]:
    """Drop DOS 8.3 aliases when a long name exists for the same parent."""
    if not names:
        return []
    long_parents = {(n[0], n[1]) for n in names if n[3] != NS_DOS}
    result = []
    seen = set()
    for entry in names:
        key = (entry[0], entry[2].lower())
        if entry[3] == NS_DOS and (entry[0], entry[1]) in long_parents:
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return result


def _attribute_list_refs(buf: bytearray, size: int, reader: RescueReader, vol: NtfsVolume) -> list[int]:
    """Record numbers referenced by $DATA entries of an $ATTRIBUTE_LIST."""
    refs: list[int] = []
    _sig, _uo, _uc, _lsn, _seq, _l, attr_ofs, _fl, used, _al, _b, _n, _p, _r = _REC_HDR.unpack_from(buf, 0)
    pos = attr_ofs
    limit = min(used, size)
    while pos + 16 <= limit:
        atype, alen, nonres = struct.unpack_from("<IIB", buf, pos)
        if atype == ATTR_END or alen < 16 or pos + alen > limit:
            break
        if atype == ATTR_ATTRIBUTE_LIST:
            if nonres:
                start_vcn, _lv, runs_off, _cu, _alloc, dsize, _valid = _NONRES.unpack_from(buf, pos + 16)
                runs, _ok = decode_runs(bytes(buf[pos + runs_off:pos + alen]), start_vcn)
                blob = bytearray()
                for _vcn, lcn, count in runs:
                    if lcn is None:
                        blob += bytes(count * vol.cluster_size)
                    else:
                        blob += reader.read_critical(vol.lcn_to_disk(lcn), count * vol.cluster_size).data
                value = bytes(blob[:dsize])
            else:
                vlen, voff = struct.unpack_from("<IH", buf, pos + 16)
                value = bytes(buf[pos + voff:pos + voff + vlen])
            p = 0
            while p + 26 <= len(value):
                etype, elen = struct.unpack_from("<IH", value, p)
                if elen < 26:
                    break
                ref = struct.unpack_from("<Q", value, p + 16)[0] & 0xFFFFFFFFFFFF
                if etype == ATTR_DATA:
                    refs.append(ref)
                p += elen
        pos += alen
    return refs
