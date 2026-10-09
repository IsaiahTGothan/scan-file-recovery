"""FAT12 / FAT16 / FAT32.

* The FAT is read lazily in 64 KiB pages; unreadable parts of the first FAT
  are taken from the second copy.
* Long file names are rebuilt (with checksum validation), including those
  of deleted entries.
* Deleted files: FAT clears the cluster chain on delete, so the data is
  assumed to be contiguous from the first cluster, as every recovery tool
  does; the file is flagged accordingly and marked "overwritten" when those
  clusters are now in use by other files.
* Deleted folders are followed through as many contiguous free clusters as
  still look like directory data.
* Loops and out-of-range values in chains are detected and cut.
"""

from __future__ import annotations

import struct
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from ..errors import E_BOOT_BACKUP, E_META_CORRUPT, E_META_UNREADABLE
from ..events import EventBus, JobControl, Progress
from ..rescue.reader import ReadMode, RescueReader
from ..util import fat_datetime
from .model import Extent, F, FileLayout, Node, Volume

ATTR_READONLY = 0x01
ATTR_HIDDEN = 0x02
ATTR_SYSTEM = 0x04
ATTR_VOLUME = 0x08
ATTR_DIR = 0x10
ATTR_LFN = 0x0F

MAX_DIR_BYTES = 65536 * 32  # FAT directories hold at most 65536 entries


@dataclass
class FatBoot:
    bytes_per_sector: int
    sectors_per_cluster: int
    reserved: int
    num_fats: int
    root_entries: int
    total_sectors: int
    fat_size: int
    root_cluster: int
    fat_type: int
    first_data_sector: int
    cluster_count: int
    label: str
    serial: int
    backup_boot: int
    active_fat: int

    @property
    def cluster_size(self) -> int:
        return self.bytes_per_sector * self.sectors_per_cluster

    @property
    def volume_size(self) -> int:
        return self.total_sectors * self.bytes_per_sector

    @classmethod
    def parse(cls, sector: bytes) -> FatBoot | None:
        if len(sector) < 512 or sector[510:512] != b"\x55\xaa":
            return None
        jump_ok = (sector[0] == 0xEB and sector[2] == 0x90) or sector[0] == 0xE9
        bps, spc, reserved, nfats, root_entries, total16, media, fat16 = struct.unpack_from(
            "<HBHBHHBH", sector, 11)
        if bps not in (512, 1024, 2048, 4096):
            return None
        if spc == 0 or spc & (spc - 1) or spc > 128:
            return None
        if reserved == 0 or nfats not in (1, 2):
            return None
        if media != 0xF0 and media < 0xF8:
            return None
        total32 = struct.unpack_from("<I", sector, 32)[0]
        total = total16 or total32
        fat32_size = struct.unpack_from("<I", sector, 36)[0]
        fat_size = fat16 or fat32_size
        if total == 0 or fat_size == 0:
            return None
        fat_text = sector[54:62] if fat16 else sector[82:90]
        if not jump_ok and not fat_text.startswith(b"FAT"):
            return None
        root_dir_sectors = (root_entries * 32 + bps - 1) // bps
        first_data = reserved + nfats * fat_size + root_dir_sectors
        if first_data >= total:
            return None
        cluster_count = (total - first_data) // spc
        if cluster_count < 1:
            return None
        if cluster_count < 4085:
            fat_type = 12
        elif cluster_count < 65525:
            fat_type = 16
        else:
            fat_type = 32
        root_cluster = 0
        backup = 0
        active = 0
        if fat16 == 0:
            if root_entries != 0:
                return None
            fat_type = 32
            ext_flags = struct.unpack_from("<H", sector, 40)[0]
            if ext_flags & 0x80:
                active = ext_flags & 0x0F
                if active >= nfats:
                    active = 0
            root_cluster = struct.unpack_from("<I", sector, 44)[0]
            backup = struct.unpack_from("<H", sector, 50)[0]
            if root_cluster < 2 or root_cluster >= cluster_count + 2:
                return None
            label_raw = sector[71:82]
            serial = struct.unpack_from("<I", sector, 67)[0]
        else:
            if fat_type == 32:  # FAT12/16 layout claiming FAT32 cluster counts
                return None
            label_raw = sector[43:54] if sector[38] == 0x29 else b""
            serial = struct.unpack_from("<I", sector, 39)[0] if sector[38] == 0x29 else 0
        # The FAT must be large enough to describe every cluster.
        bits = {12: 12, 16: 16, 32: 32}[fat_type]
        if fat_size * bps * 8 < (cluster_count + 2) * bits:
            return None
        label = label_raw.decode("cp437", "replace").strip()
        if label.upper() == "NO NAME":
            label = ""
        return cls(bps, spc, reserved, nfats, root_entries, total, fat_size, root_cluster,
                   fat_type, first_data, cluster_count, label, serial, backup, active)


class FatTable:
    PAGE = 64 * 1024

    def __init__(self, vol: FatVolume) -> None:
        self.vol = vol
        boot = vol.boot
        self.bits = boot.fat_type
        self.entries = boot.cluster_count + 2
        self.fat_bytes = boot.fat_size * boot.bytes_per_sector
        bps = boot.bytes_per_sector
        order = [boot.active_fat] + [i for i in range(boot.num_fats) if i != boot.active_fat]
        self.copies = [vol.offset + (boot.reserved + i * boot.fat_size) * bps for i in order]
        self._pages: OrderedDict[int, tuple[bytes, list[tuple[int, int]]]] = OrderedDict()
        self.unreadable_bytes = 0
        if self.bits == 32:
            self.eoc, self.bad, self.mask = 0x0FFFFFF8, 0x0FFFFFF7, 0x0FFFFFFF
        elif self.bits == 16:
            self.eoc, self.bad, self.mask = 0xFFF8, 0xFFF7, 0xFFFF
        else:
            self.eoc, self.bad, self.mask = 0xFF8, 0xFF7, 0xFFF

    def _load_page(self, index: int) -> tuple[bytes, list[tuple[int, int]]]:
        cached = self._pages.get(index)
        if cached is not None:
            self._pages.move_to_end(index)
            return cached
        start = index * self.PAGE
        length = min(self.PAGE, self.fat_bytes - start)
        reader = self.vol.reader
        outcome = reader.read(self.copies[0] + start, length, ReadMode.FAST)
        data = bytearray(outcome.data)
        missing = [(s - outcome.offset, e - outcome.offset) for s, e in outcome.unread + outcome.bad]
        for copy in self.copies[1:]:
            if not missing:
                break
            still = []
            for s, e in missing:
                alt = reader.read_critical(copy + start + s, e - s)
                data[s:e] = alt.data
                for bs, be in alt.unread + alt.bad:
                    still.append((bs - copy - start, be - copy - start))
            missing = still
        if missing:
            still = []
            for s, e in missing:
                alt = reader.read_critical(self.copies[0] + start + s, e - s)
                data[s:e] = alt.data
                for bs, be in alt.unread + alt.bad:
                    still.append((bs - self.copies[0] - start, be - self.copies[0] - start))
            missing = still
            self.unreadable_bytes += sum(e - s for s, e in missing)
        page = (bytes(data), missing)
        self._pages[index] = page
        while len(self._pages) > 256:
            self._pages.popitem(last=False)
        return page

    def _bytes(self, offset: int, length: int) -> tuple[bytes, bool]:
        """FAT bytes at ``offset`` and whether they were readable."""
        out = bytearray()
        ok = True
        pos = offset
        end = offset + length
        while pos < end:
            index = pos // self.PAGE
            data, missing = self._load_page(index)
            local = pos - index * self.PAGE
            take = min(end - pos, len(data) - local)
            if take <= 0:
                return bytes(out) + bytes(end - pos), False
            for s, e in missing:
                if s < local + take and e > local:
                    ok = False
            out += data[local:local + take]
            pos += take
        return bytes(out), ok

    def get(self, cluster: int) -> int | None:
        """FAT entry for ``cluster`` (None if that part of the FAT is unreadable)."""
        if cluster < 0 or cluster >= self.entries:
            return None
        if self.bits == 32:
            raw, ok = self._bytes(cluster * 4, 4)
            value = struct.unpack("<I", raw)[0] & 0x0FFFFFFF
        elif self.bits == 16:
            raw, ok = self._bytes(cluster * 2, 2)
            value = struct.unpack("<H", raw)[0]
        else:
            raw, ok = self._bytes(cluster + cluster // 2, 2)
            pair = raw[0] | (raw[1] << 8)
            value = pair >> 4 if cluster & 1 else pair & 0xFFF
        return value if ok else None

    def chain(self, start: int, limit: int) -> tuple[list[int], str]:
        """Follow a chain from ``start`` for at most ``limit`` clusters.

        Returns (clusters, problem) where problem is "" for a clean chain.
        """
        clusters: list[int] = []
        seen: set[int] = set()
        current = start
        while len(clusters) < limit:
            if current < 2 or current >= self.entries:
                return clusters, "chain points outside the volume"
            if current in seen:
                return clusters, "loop in cluster chain"
            seen.add(current)
            clusters.append(current)
            nxt = self.get(current)
            if nxt is None:
                return clusters, "FAT unreadable"
            if nxt >= self.eoc:
                return clusters, ""
            if nxt == self.bad:
                return clusters, "cluster marked bad"
            if nxt == 0:
                return clusters, "chain ends in a free cluster"
            current = nxt
        return clusters, ""

    def range_free(self, first: int, count: int) -> bool | None:
        """True if all clusters in [first, first+count) are free (None if unknown)."""
        if first < 2 or first + count > self.entries:
            return None
        if self.bits == 12:
            for cluster in range(first, first + count):
                value = self.get(cluster)
                if value is None:
                    return None
                if value:
                    return False
            return True
        width = self.bits // 8
        pos = first * width
        end = (first + count) * width
        step = 1 << 20
        while pos < end:
            raw, ok = self._bytes(pos, min(step, end - pos))
            if not ok:
                return None
            if raw.count(0) != len(raw):
                if self.bits == 32:
                    for i in range(0, len(raw), 4):
                        if struct.unpack_from("<I", raw, i)[0] & 0x0FFFFFFF:
                            return False
                else:
                    return False
            pos += step
        return True


@dataclass
class FatRef:
    start: int
    size: int
    deleted: bool
    is_dir: bool


@dataclass
class DirEntry:
    name: str
    attr: int
    start: int
    size: int
    ctime: float | None
    mtime: float | None
    atime: float | None
    deleted: bool
    name_guessed: bool


def lfn_checksum(short: bytes) -> int:
    total = 0
    for byte in short:
        total = (((total & 1) << 7) + (total >> 1) + byte) & 0xFF
    return total


def _decode_lfn(parts: list[bytes]) -> str | None:
    raw = b"".join(parts)
    try:
        text = raw.decode("utf-16-le")
    except UnicodeDecodeError:
        return None
    end = text.find("\x00")
    if end >= 0:
        text = text[:end]
    text = text.rstrip("￿")
    if not text or "￿" in text:
        return None
    return text


def _short_name(raw: bytes, case: int) -> str:
    base = raw[:8].rstrip(b" ").decode("cp437", "replace")
    ext = raw[8:11].rstrip(b" ").decode("cp437", "replace")
    if case & 0x08:
        base = base.lower()
    if case & 0x10:
        ext = ext.lower()
    return f"{base}.{ext}" if ext else base


def _plausible_entry(entry: bytes) -> bool:
    first = entry[0]
    if first == 0:
        return True
    attr = entry[11]
    if attr == ATTR_LFN:
        return True
    if attr & 0xC0:
        return False
    for byte in entry[1:11]:
        if byte < 0x20 or byte in b'"*+,/:;<=>?[\\]|':
            return False
    return first >= 0x20 or first == 0x05


def parse_directory(data: bytes, holes: list[tuple[int, int]] | None = None) -> tuple[list[DirEntry], str]:
    """Parse raw directory bytes.  Returns (entries, volume label)."""
    entries: list[DirEntry] = []
    label = ""
    lfn: list[tuple[int, bytes, int]] = []   # (sequence byte, 26 name bytes, checksum)
    holes = holes or []
    for pos in range(0, len(data) - 31, 32):
        if any(s < pos + 32 and e > pos for s, e in holes):
            lfn = []
            continue
        entry = data[pos:pos + 32]
        first = entry[0]
        if first == 0x00:
            break
        attr = entry[11]
        if attr == ATTR_LFN:
            lfn.append((first, entry[1:11] + entry[14:26] + entry[28:32], entry[13]))
            continue
        deleted = first == 0xE5
        short = bytearray(entry[0:11])
        if first == 0x05:
            short[0] = 0xE5
        if attr & ATTR_VOLUME and not attr & ATTR_DIR:
            if not deleted and not label:
                label = bytes(short).decode("cp437", "replace").strip()
            lfn = []
            continue
        if short[0] == 0x2E:
            lfn = []
            continue
        if attr & 0xC0:
            lfn = []
            continue
        long_name, guessed_first = _assemble_lfn(lfn, bytes(short), deleted)
        lfn = []
        name_guessed = False
        if long_name:
            name = long_name
        else:
            if deleted:
                short[0] = guessed_first or 0x5F  # '_'
                name_guessed = guessed_first is None
            name = _short_name(bytes(short), entry[12])
        if not name or name in (".", ".."):
            continue
        hi, ctime_t, ctime_d, adate, hi_cluster, mtime_t, mtime_d, lo_cluster, size = struct.unpack_from(
            "<BHHHHHHHI", entry, 13)
        entries.append(DirEntry(
            name=name,
            attr=attr,
            start=(hi_cluster << 16) | lo_cluster,
            size=size,
            ctime=fat_datetime(ctime_d, ctime_t, hi),
            mtime=fat_datetime(mtime_d, mtime_t),
            atime=fat_datetime(adate),
            deleted=deleted,
            name_guessed=name_guessed,
        ))
    return entries, label


def _assemble_lfn(parts: list[tuple[int, bytes, int]], short: bytes, deleted: bool) -> tuple[str | None, int | None]:
    """Rebuild a long name from the LFN entries preceding a short entry."""
    if not parts:
        return None, None
    checksum = parts[-1][2]
    # Only the contiguous tail with one checksum belongs to this entry.
    tail: list[tuple[int, bytes, int]] = []
    for part in reversed(parts):
        if part[2] != checksum:
            break
        tail.append(part)
    if not deleted:
        if lfn_checksum(short) != checksum:
            return None, None
        for index, part in enumerate(tail, start=1):
            if part[0] & 0x1F != index:
                return None, None
        if not tail[-1][0] & 0x40:
            return None, None
        return _decode_lfn([p[1] for p in tail]), None
    # Deleted: the first byte of the short name and the LFN sequence numbers
    # were overwritten with 0xE5.  Find the original first byte through the
    # checksum to prove the LFN entries really belong to this short entry.
    if any(p[0] != 0xE5 for p in tail):
        return None, None
    for candidate in range(0x20, 0x100):
        if candidate == 0xE5:
            continue
        if lfn_checksum(bytes([candidate]) + short[1:]) == checksum:
            name = _decode_lfn([p[1] for p in tail])
            return name, candidate
    return None, None


class FatVolume(Volume):
    def __init__(
        self,
        reader: RescueReader,
        offset: int,
        boot: FatBoot,
        events: EventBus | None = None,
        control: JobControl | None = None,
        used_backup: bool = False,
    ) -> None:
        super().__init__(reader, offset, boot.volume_size, boot.label)
        self.boot = boot
        self.kind = f"FAT{boot.fat_type}"
        self.cluster_size = boot.cluster_size
        self.events = events
        self.control = control
        self.serial = f"{boot.serial >> 16:04X}-{boot.serial & 0xFFFF:04X}"
        self.fat = FatTable(self)
        self.directories = 0
        if used_backup:
            self._warn("The FAT32 boot sector is damaged; using its backup copy.", E_BOOT_BACKUP)

    def _warn(self, message: str, code: str = E_META_CORRUPT) -> None:
        self.warnings.append(message)
        if self.events is not None:
            self.events.warning(message, code=code, source="fat")

    def cluster_offset(self, cluster: int) -> int:
        boot = self.boot
        return self.offset + (boot.first_data_sector + (cluster - 2) * boot.sectors_per_cluster) * boot.bytes_per_sector

    def _read_meta(self, offset: int, length: int) -> tuple[bytes, list[tuple[int, int]]]:
        outcome = self.reader.read(offset, length, ReadMode.FAST)
        if not outcome.complete:
            outcome = self.reader.read_critical(offset, length)
        holes = [(s - offset, e - offset) for s, e in outcome.unread + outcome.bad]
        return bytes(outcome.data), holes

    def _read_clusters(self, clusters: list[int]) -> tuple[bytes, list[tuple[int, int]]]:
        cs = self.cluster_size
        data = bytearray()
        holes: list[tuple[int, int]] = []
        index = 0
        while index < len(clusters):
            run_start = clusters[index]
            run_len = 1
            while index + run_len < len(clusters) and clusters[index + run_len] == run_start + run_len:
                run_len += 1
            chunk, chunk_holes = self._read_meta(self.cluster_offset(run_start), run_len * cs)
            base = len(data)
            holes.extend((base + s, base + e) for s, e in chunk_holes)
            data += chunk
            index += run_len
        return bytes(data), holes

    def _deleted_dir_clusters(self, start: int) -> list[int]:
        """First cluster of a deleted folder plus following free clusters that look like directory data."""
        if start < 2 or start >= self.boot.cluster_count + 2:
            return []
        clusters = [start]
        cs = self.cluster_size
        limit = MAX_DIR_BYTES // cs
        cluster = start + 1
        while len(clusters) < limit and cluster < self.boot.cluster_count + 2:
            if self.fat.get(cluster) != 0:
                break
            data, holes = self._read_meta(self.cluster_offset(cluster), cs)
            if holes or not data[:1] or data[0] == 0:
                break
            if not all(_plausible_entry(data[i:i + 32]) for i in range(0, cs, 32)):
                break
            if data[0:2] == b". " and data[11] & ATTR_DIR:
                break  # start of another directory
            clusters.append(cluster)
            cluster += 1
        return clusters

    def _dir_data(self, ref: FatRef | None) -> tuple[bytes, list[tuple[int, int]], str]:
        boot = self.boot
        if ref is None:
            if boot.fat_type == 32:
                clusters, problem = self.fat.chain(boot.root_cluster, MAX_DIR_BYTES // self.cluster_size)
                data, holes = self._read_clusters(clusters)
                return data, holes, problem
            bps = boot.bytes_per_sector
            start = self.offset + (boot.reserved + boot.num_fats * boot.fat_size) * bps
            data, holes = self._read_meta(start, boot.root_entries * 32)
            return data, holes, ""
        if ref.deleted:
            clusters = self._deleted_dir_clusters(ref.start)
            data, holes = self._read_clusters(clusters)
            if not (data[0:1] == b"." and data[11:12] and data[11] & ATTR_DIR):
                return b"", [], "deleted folder was overwritten"
            return data, holes, ""
        clusters, problem = self.fat.chain(ref.start, MAX_DIR_BYTES // self.cluster_size)
        data, holes = self._read_clusters(clusters)
        return data, holes, problem

    def load(self, progress: Callable[[Progress], None] | None = None) -> Node:
        root = Node("", F.DIR | F.VOLUME | F.VIRTUAL, volume=self, ref=None)
        stack: list[tuple[Node, FatRef | None, int]] = [(root, None, 0)]
        visited: set[int] = set()
        files = 0
        while stack:
            if self.control is not None:
                self.control.check()
            node, ref, depth = stack.pop()
            data, holes, problem = self._dir_data(ref)
            self.directories += 1
            if problem and ref is not None and not ref.deleted:
                node.flags |= F.DAMAGED_META
                self._warn(f"Folder '{node.path()}': {problem}.")
            if holes:
                node.flags |= F.DAMAGED_META
                self._warn(f"Folder '{node.path() or '(root)'}' is partly unreadable.", E_META_UNREADABLE)
            entries, label = parse_directory(data, holes)
            if ref is None and label and not self.label:
                self.label = label
            for entry in entries:
                child = self._entry_node(entry, node.flags & F.DELETED)
                node.add(child)
                if child.is_dir:
                    child_ref = child.ref
                    assert isinstance(child_ref, FatRef)
                    if child_ref.start in visited or child_ref.start < 2 or depth > 64:
                        continue
                    visited.add(child_ref.start)
                    stack.append((child, child_ref, depth + 1))
                else:
                    files += 1
            if progress is not None and self.directories % 50 == 0:
                progress(Progress("Reading folders", self.directories, 0, item=f"{files:,} files found"))
        if self.fat.unreadable_bytes:
            self._warn("Parts of both FAT copies are unreadable; some file locations were estimated.",
                       E_META_UNREADABLE)
        self.root = root
        return root

    def _entry_node(self, entry: DirEntry, parent_deleted: int) -> Node:
        is_dir = bool(entry.attr & ATTR_DIR)
        flags = F.DIR if is_dir else 0
        deleted = entry.deleted or bool(parent_deleted)
        if deleted:
            flags |= F.DELETED
        if entry.attr & (ATTR_HIDDEN | ATTR_SYSTEM):
            flags |= F.HIDDEN
        if entry.attr & ATTR_READONLY:
            flags |= F.READONLY
        if entry.name_guessed:
            flags |= F.NAME_GUESSED
        ref = FatRef(entry.start, entry.size if not is_dir else 0, deleted, is_dir)
        node = Node(entry.name, flags, 0 if is_dir else entry.size, volume=self, ref=ref,
                    ctime=entry.ctime, mtime=entry.mtime, atime=entry.atime)
        if deleted and not is_dir and entry.size:
            node.flags |= F.ASSUMED_CONTIGUOUS
            count = -(-entry.size // self.cluster_size)
            free = self.fat.range_free(entry.start, count)
            if free is False:
                node.flags |= F.OVERWRITTEN
        return node

    def layout(self, node: Node) -> FileLayout:
        ref = node.ref
        if not isinstance(ref, FatRef) or ref.is_dir:
            return FileLayout(0)
        size = ref.size
        layout = FileLayout(size)
        if size == 0:
            return layout
        cs = self.cluster_size
        need = -(-size // cs)
        if ref.start < 2 or ref.start >= self.boot.cluster_count + 2:
            layout.problems.append("The file's first cluster is invalid.")
            return layout
        if ref.deleted:
            clusters = list(range(ref.start, min(ref.start + need, self.boot.cluster_count + 2)))
            if len(clusters) < need:
                layout.problems.append("The file would extend past the end of the volume.")
        else:
            clusters, problem = self.fat.chain(ref.start, need)
            if len(clusters) < need:
                # Continue contiguously after the damaged point: the best estimate.
                last = clusters[-1] if clusters else ref.start - 1
                extra = list(range(last + 1, min(last + 1 + need - len(clusters), self.boot.cluster_count + 2)))
                layout.problems.append(
                    f"Cluster chain damaged ({problem or 'too short'}); the rest of the file's "
                    "location was estimated.")
                clusters += extra
        extents: list[Extent] = []
        file_offset = 0
        index = 0
        while index < len(clusters) and file_offset < size:
            run_start = clusters[index]
            run_len = 1
            while index + run_len < len(clusters) and clusters[index + run_len] == run_start + run_len:
                run_len += 1
            length = min(run_len * cs, size - file_offset)
            extents.append(Extent(file_offset, length, self.cluster_offset(run_start)))
            file_offset += length
            index += run_len
        layout.extents = extents
        return layout
