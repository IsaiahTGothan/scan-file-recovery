"""exFAT (SD cards, camera cards, large USB drives).

Deleted files keep their whole directory entry set (only the "in use" bit
is cleared), so names, sizes and timestamps survive.  Contiguous files
(the common case, "NoFatChain") can be recovered exactly; for fragmented
files the FAT chain is used when it is still intact.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass

from ..errors import E_BOOT_BACKUP, E_META_CORRUPT, E_META_UNREADABLE
from ..events import EventBus, JobControl, Progress
from ..rescue.reader import ReadMode, RescueReader
from ..util import exfat_timestamp
from .model import Extent, F, FileLayout, Node, Volume

ATTR_READONLY = 0x01
ATTR_HIDDEN = 0x02
ATTR_SYSTEM = 0x04
ATTR_DIR = 0x10

FLAG_ALLOCATION_POSSIBLE = 0x01
FLAG_NO_FAT_CHAIN = 0x02

MAX_DIR_BYTES = 256 << 20
EOC = 0xFFFFFFFF
BAD_CLUSTER = 0xFFFFFFF7


@dataclass
class ExfatBoot:
    partition_offset: int
    volume_length: int        # sectors
    fat_offset: int           # sectors
    fat_length: int           # sectors
    heap_offset: int          # sectors
    cluster_count: int
    root_cluster: int
    serial: int
    revision: int
    bps_shift: int
    spc_shift: int
    num_fats: int
    active_fat: int

    @property
    def bytes_per_sector(self) -> int:
        return 1 << self.bps_shift

    @property
    def cluster_size(self) -> int:
        return 1 << (self.bps_shift + self.spc_shift)

    @property
    def volume_size(self) -> int:
        return self.volume_length << self.bps_shift

    @classmethod
    def parse(cls, sector: bytes) -> ExfatBoot | None:
        if len(sector) < 512 or sector[3:11] != b"EXFAT   " or sector[510:512] != b"\x55\xaa":
            return None
        if any(sector[11:64]):
            return None
        (part_off, vol_len, fat_off, fat_len, heap_off, count, root, serial, rev, vflags,
         bps_shift, spc_shift, nfats) = struct.unpack_from("<QQIIIIIIHHBBB", sector, 64)
        if not 9 <= bps_shift <= 12 or spc_shift > 25 - bps_shift:
            return None
        if nfats not in (1, 2) or fat_off < 24 or fat_len == 0:
            return None
        if heap_off < fat_off + fat_len * nfats or count == 0:
            return None
        if root < 2 or root >= count + 2:
            return None
        if heap_off + (count << spc_shift) > vol_len:
            return None
        if fat_len * (1 << bps_shift) < (count + 2) * 4:
            return None
        active = 1 if (vflags & 1) and nfats == 2 else 0
        return cls(part_off, vol_len, fat_off, fat_len, heap_off, count, root, serial, rev,
                   bps_shift, spc_shift, nfats, active)


def entry_set_checksum(data: bytes) -> int:
    checksum = 0
    for index, byte in enumerate(data):
        if index == 2 or index == 3:
            continue
        checksum = ((((checksum & 1) << 15) | (checksum >> 1)) + byte) & 0xFFFF
    return checksum


@dataclass
class ExfatEntry:
    name: str
    attr: int
    flags: int
    first_cluster: int
    size: int
    valid: int
    ctime: float | None
    mtime: float | None
    atime: float | None
    deleted: bool
    damaged: bool


@dataclass
class ExfatRef:
    first_cluster: int
    size: int
    valid: int
    flags: int
    deleted: bool
    is_dir: bool


def parse_directory(data: bytes, holes: list[tuple[int, int]] | None = None
                    ) -> tuple[list[ExfatEntry], str, tuple[int, int] | None]:
    """Parse directory bytes.  Returns (entries, volume label, (bitmap cluster, length))."""
    entries: list[ExfatEntry] = []
    label = ""
    bitmap: tuple[int, int] | None = None
    holes = holes or []
    pos = 0
    total = len(data)
    while pos + 32 <= total:
        if any(s < pos + 32 and e > pos for s, e in holes):
            pos += 32
            continue
        etype = data[pos]
        if etype == 0x00:
            break
        if etype == 0x81 and bitmap is None:
            first, length = struct.unpack_from("<IQ", data, pos + 20)
            bitmap = (first, length)
        elif etype == 0x83:
            count = min(data[pos + 1], 11)
            label = data[pos + 2:pos + 2 + 2 * count].decode("utf-16-le", "replace")
        elif etype in (0x85, 0x05):
            parsed = _parse_set(data, pos, holes)
            if parsed is not None:
                entry, length = parsed
                entries.append(entry)
                pos += length
                continue
        pos += 32
    return entries, label, bitmap


def _parse_set(data: bytes, pos: int, holes: list[tuple[int, int]]) -> tuple[ExfatEntry, int] | None:
    in_use = data[pos] == 0x85
    secondary = data[pos + 1]
    if not 2 <= secondary <= 18:
        return None
    length = (secondary + 1) * 32
    if pos + length > len(data):
        return None
    if any(s < pos + length and e > pos for s, e in holes):
        return None
    raw = bytearray(data[pos:pos + length])
    stream_type = 0xC0 if in_use else 0x40
    name_type = 0xC1 if in_use else 0x41
    if raw[32] != stream_type:
        return None
    for k in range(2, secondary + 1):
        if raw[k * 32] != name_type:
            # Vendor extension entries may follow the names; stop at the first non-name.
            if k == 2:
                return None
            break
    stored = struct.unpack_from("<H", raw, 2)[0]
    if not in_use:
        for k in range(secondary + 1):
            raw[k * 32] |= 0x80
    damaged = entry_set_checksum(bytes(raw)) != stored
    if damaged and not in_use:
        return None  # a deleted set that was partly reused: not trustworthy
    attr = struct.unpack_from("<H", raw, 4)[0]
    create, modify, access = struct.unpack_from("<III", raw, 8)
    c10, m10, cutc, mutc, autc = raw[20], raw[21], raw[22], raw[23], raw[24]
    flags = raw[33]
    name_len = raw[35]
    valid, = struct.unpack_from("<Q", raw, 40)
    first, size = struct.unpack_from("<IQ", raw, 52)
    chars = bytearray()
    for k in range(2, secondary + 1):
        if raw[k * 32] & 0x7F != 0x41:
            break
        chars += raw[k * 32 + 2:k * 32 + 32]
    name = chars[:2 * name_len].decode("utf-16-le", "replace")
    if not name:
        return None
    entry = ExfatEntry(
        name=name,
        attr=attr,
        flags=flags,
        first_cluster=first,
        size=size,
        valid=min(valid, size),
        ctime=exfat_timestamp(create, c10, cutc),
        mtime=exfat_timestamp(modify, m10, mutc),
        atime=exfat_timestamp(access, 0, autc),
        deleted=not in_use,
        damaged=damaged,
    )
    return entry, length


class ExfatVolume(Volume):
    kind = "exFAT"

    def __init__(
        self,
        reader: RescueReader,
        offset: int,
        boot: ExfatBoot,
        events: EventBus | None = None,
        control: JobControl | None = None,
        used_backup: bool = False,
    ) -> None:
        super().__init__(reader, offset, boot.volume_size)
        self.boot = boot
        self.cluster_size = boot.cluster_size
        self.events = events
        self.control = control
        self.serial = f"{boot.serial >> 16:04X}-{boot.serial & 0xFFFF:04X}"
        self.bitmap: bytes | None = None
        self.directories = 0
        bps = boot.bytes_per_sector
        self._fat_base = offset + (boot.fat_offset + boot.active_fat * boot.fat_length) * bps
        self._fat_alt = (offset + (boot.fat_offset + (1 - boot.active_fat) * boot.fat_length) * bps
                         if boot.num_fats == 2 else None)
        self._fat_pages: dict[int, bytes | None] = {}
        if used_backup:
            self._warn("The exFAT boot sector is damaged; using its backup copy.", E_BOOT_BACKUP)

    def _warn(self, message: str, code: str = E_META_CORRUPT) -> None:
        self.warnings.append(message)
        if self.events is not None:
            self.events.warning(message, code=code, source="exfat")

    def cluster_offset(self, cluster: int) -> int:
        return self.offset + (self.boot.heap_offset << self.boot.bps_shift) + (cluster - 2) * self.cluster_size

    def _valid_cluster(self, cluster: int) -> bool:
        return 2 <= cluster < self.boot.cluster_count + 2

    # ------------------------------------------------------------------ FAT
    def _fat_entry(self, cluster: int) -> int | None:
        page_size = 64 * 1024
        offset = cluster * 4
        index = offset // page_size
        if index not in self._fat_pages:
            if len(self._fat_pages) > 512:
                self._fat_pages.clear()
            outcome = self.reader.read(self._fat_base + index * page_size, page_size, ReadMode.FAST)
            data = bytearray(outcome.data)
            ok = outcome.complete
            if not ok and self._fat_alt is not None:
                alt = self.reader.read_critical(self._fat_alt + index * page_size, page_size)
                for s, e in outcome.unread + outcome.bad:
                    rel_s, rel_e = s - outcome.offset, e - outcome.offset
                    data[rel_s:rel_e] = alt.data[rel_s:rel_e]
                ok = alt.complete
            if not ok:
                again = self.reader.read_critical(self._fat_base + index * page_size, page_size)
                if again.complete:
                    data = again.data
                    ok = True
            self._fat_pages[index] = bytes(data) if ok else None
        page = self._fat_pages[index]
        if page is None:
            return None
        local = offset - index * page_size
        return int(struct.unpack_from("<I", page, local)[0])

    def chain(self, start: int, limit: int) -> tuple[list[int], str]:
        clusters: list[int] = []
        seen: set[int] = set()
        current = start
        while len(clusters) < limit:
            if not self._valid_cluster(current):
                return clusters, "chain points outside the volume"
            if current in seen:
                return clusters, "loop in cluster chain"
            seen.add(current)
            clusters.append(current)
            nxt = self._fat_entry(current)
            if nxt is None:
                return clusters, "FAT unreadable"
            if nxt == EOC:
                return clusters, ""
            if nxt == BAD_CLUSTER:
                return clusters, "cluster marked bad"
            current = nxt
        return clusters, ""

    # --------------------------------------------------------------- bitmap
    def _load_bitmap(self, info: tuple[int, int] | None) -> None:
        if info is None:
            return
        first, length = info
        if not self._valid_cluster(first) or length > (self.boot.cluster_count + 7) // 8 + self.cluster_size:
            return
        need = -(-length // self.cluster_size)
        clusters, _problem = self.chain(first, need)
        if len(clusters) < need:
            clusters = list(range(first, first + need))
        data = bytearray()
        for cluster in clusters:
            outcome = self.reader.read(self.cluster_offset(cluster), self.cluster_size, ReadMode.FAST)
            if not outcome.complete:
                return
            data += outcome.data
        self.bitmap = bytes(data[:length])

    def clusters_in_use(self, first: int, count: int) -> bool | None:
        if self.bitmap is None or first < 2:
            return None
        bit = first - 2
        byte_lo = bit // 8
        byte_hi = (bit + count + 7) // 8
        if byte_hi > len(self.bitmap):
            return None
        bits = int.from_bytes(self.bitmap[byte_lo:byte_hi], "little") >> (bit % 8)
        return bool(bits & ((1 << count) - 1))

    # --------------------------------------------------------------- reading
    def _read_meta(self, offset: int, length: int) -> tuple[bytes, list[tuple[int, int]]]:
        outcome = self.reader.read(offset, length, ReadMode.FAST)
        if not outcome.complete:
            outcome = self.reader.read_critical(offset, length)
        return bytes(outcome.data), [(s - offset, e - offset) for s, e in outcome.unread + outcome.bad]

    def _clusters_for(self, first: int, size: int, flags: int, deleted: bool) -> tuple[list[int], str]:
        cs = self.cluster_size
        need = -(-size // cs)
        if need == 0:
            return [], ""
        if not self._valid_cluster(first):
            return [], "first cluster is invalid"
        if flags & FLAG_NO_FAT_CHAIN:
            end = min(first + need, self.boot.cluster_count + 2)
            return list(range(first, end)), "" if end - first == need else "extends past the volume"
        clusters, problem = self.chain(first, need)
        if len(clusters) == need and not problem:
            return clusters, ""
        if deleted:
            # The chain is gone or reused: fall back to the contiguous assumption.
            end = min(first + need, self.boot.cluster_count + 2)
            return list(range(first, end)), "assumed"
        last = clusters[-1] if clusters else first - 1
        extra = list(range(last + 1, min(last + 1 + need - len(clusters), self.boot.cluster_count + 2)))
        return clusters + extra, problem or "chain too short"

    def _dir_bytes(self, ref: ExfatRef | None) -> tuple[bytes, list[tuple[int, int]], str]:
        if ref is None:
            clusters, problem = self.chain(self.boot.root_cluster, MAX_DIR_BYTES // self.cluster_size)
            size = len(clusters) * self.cluster_size
        else:
            size = min(ref.size, MAX_DIR_BYTES)
            clusters, problem = self._clusters_for(ref.first_cluster, size, ref.flags, ref.deleted)
            if problem == "assumed":
                problem = ""
        data = bytearray()
        holes: list[tuple[int, int]] = []
        index = 0
        cs = self.cluster_size
        while index < len(clusters):
            run_start = clusters[index]
            run = 1
            while index + run < len(clusters) and clusters[index + run] == run_start + run:
                run += 1
            chunk, chunk_holes = self._read_meta(self.cluster_offset(run_start), run * cs)
            base = len(data)
            holes.extend((base + s, base + e) for s, e in chunk_holes)
            data += chunk
            index += run
        return bytes(data[:size]) if ref is not None else bytes(data), holes, problem

    def load(self, progress: Callable[[Progress], None] | None = None) -> Node:
        root = Node("", F.DIR | F.VOLUME | F.VIRTUAL, volume=self)
        stack: list[tuple[Node, ExfatRef | None, int]] = [(root, None, 0)]
        visited: set[int] = set()
        files = 0
        while stack:
            if self.control is not None:
                self.control.check()
            node, ref, depth = stack.pop()
            data, holes, problem = self._dir_bytes(ref)
            self.directories += 1
            entries, label, bitmap = parse_directory(data, holes)
            if ref is None:
                if label:
                    self.label = label
                self._load_bitmap(bitmap)
            if problem:
                node.flags |= F.DAMAGED_META
                self._warn(f"Folder '{node.path() or '(root)'}': {problem}.")
            if holes:
                node.flags |= F.DAMAGED_META
                self._warn(f"Folder '{node.path() or '(root)'}' is partly unreadable.", E_META_UNREADABLE)
            for entry in entries:
                child = self._entry_node(entry, bool(node.flags & F.DELETED))
                node.add(child)
                if child.is_dir:
                    child_ref = child.ref
                    assert isinstance(child_ref, ExfatRef)
                    key = child_ref.first_cluster
                    if key in visited or not self._valid_cluster(key) or depth > 64:
                        continue
                    visited.add(key)
                    stack.append((child, child_ref, depth + 1))
                else:
                    files += 1
            if progress is not None and self.directories % 50 == 0:
                progress(Progress("Reading folders", self.directories, 0, item=f"{files:,} files found"))
        self.root = root
        return root

    def _entry_node(self, entry: ExfatEntry, parent_deleted: bool) -> Node:
        is_dir = bool(entry.attr & ATTR_DIR)
        flags = F.DIR if is_dir else 0
        deleted = entry.deleted or parent_deleted
        if deleted:
            flags |= F.DELETED
        if entry.attr & (ATTR_HIDDEN | ATTR_SYSTEM):
            flags |= F.HIDDEN
        if entry.attr & ATTR_READONLY:
            flags |= F.READONLY
        if entry.damaged:
            flags |= F.DAMAGED_META
        ref = ExfatRef(entry.first_cluster, entry.size, entry.valid, entry.flags, deleted, is_dir)
        node = Node(entry.name, flags, 0 if is_dir else entry.size, volume=self, ref=ref,
                    ctime=entry.ctime, mtime=entry.mtime, atime=entry.atime)
        if deleted and not is_dir and entry.size:
            if not entry.flags & FLAG_NO_FAT_CHAIN:
                node.flags |= F.ASSUMED_CONTIGUOUS
            count = -(-entry.size // self.cluster_size)
            if self.clusters_in_use(entry.first_cluster, count):
                node.flags |= F.OVERWRITTEN
        return node

    def layout(self, node: Node) -> FileLayout:
        ref = node.ref
        if not isinstance(ref, ExfatRef) or ref.is_dir:
            return FileLayout(0)
        layout = FileLayout(ref.size, valid_size=ref.valid)
        if ref.size == 0:
            return layout
        clusters, problem = self._clusters_for(ref.first_cluster, ref.size, ref.flags, ref.deleted)
        if problem and problem != "assumed":
            layout.problems.append(f"Cluster chain damaged ({problem}); part of the location was estimated.")
        cs = self.cluster_size
        extents: list[Extent] = []
        offset = 0
        index = 0
        while index < len(clusters) and offset < ref.size:
            run_start = clusters[index]
            run = 1
            while index + run < len(clusters) and clusters[index + run] == run_start + run:
                run += 1
            length = min(run * cs, ref.size - offset)
            extents.append(Extent(offset, length, self.cluster_offset(run_start)))
            offset += length
            index += run
        layout.extents = extents
        return layout
