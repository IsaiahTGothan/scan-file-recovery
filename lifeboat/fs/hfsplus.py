"""HFS+ and HFSX ("Mac OS Extended"): Macs before APFS, and still many external drives, camera
disks and Time Machine backups.

Everything about a file is in the catalog, a B-tree of records keyed by parent folder and name.
Lifeboat reads the catalog file front to back in large pieces (the gentlest pattern for a failing
drive). Records in nodes the tree still uses make up the live folder tree. Deleted files are found
in the places that keep old copies of catalog records:

* nodes the B-tree no longer uses (freed when folders shrank) still hold their old records;
* the journal (journaled volumes, the default) holds recent copies of the catalog nodes macOS
  changed, including the ones from before a file was deleted.

A record naming a file the live catalog no longer has is a deleted file. The record holds the
location of the file's first eight pieces, so it can usually be recovered whole unless its space
was reused since (checked against the allocation bitmap).

Damage handling:

* the volume header falls back to its copy at the end of the volume;
* a journal with unfinished changes (the drive was unplugged) is replayed in memory, never on
  the drive;
* catalog areas that can't be read are retried within a time limit and reported;
* every record is bounds-checked; folder loops and missing parents go to "Lost files".
"""

from __future__ import annotations

import bisect
import struct
import time
import unicodedata
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from ..errors import E_BOOT_BACKUP, E_META_CORRUPT, E_META_UNREADABLE
from ..events import EventBus, JobControl, Progress
from ..rescue.reader import ReadMode, RescueReader
from ..util import format_size, hfs_time, plural
from .applecomp import CompressionError, chunk_table, parse_decmpfs, table_bytes_needed
from .model import INVALID, CompressedChunks, Extent, F, FileLayout, Node, Volume

ROOT_PARENT, ROOT_FOLDER = 1, 2
EXTENTS_ID, CATALOG_ID, ALLOCATION_ID, ATTRIBUTES_ID = 3, 4, 6, 8
FIRST_USER_ID = 16

FOLDER, FILE, FOLDER_THREAD, FILE_THREAD = 1, 2, 3, 4
LEAF, HEADER, MAP = -1, 1, 2
DATA_FORK, RSRC_FORK = 0x00, 0xFF
ATTR_INLINE, ATTR_FORK = 0x10, 0x20

VOL_JOURNALED = 0x2000
UF_COMPRESSED = 0x20
FINDER_INVISIBLE = 0x4000
S_IFMT, S_IFLNK = 0o170000, 0o120000

FILE_LINK = (b"hlnk", b"hfs+")       # (type, creator) of a hard link's visible entry
FOLDER_LINK = (b"fdrp", b"MACS")     # a folder hard link (Time Machine)
SYMLINK = (b"slnk", b"rhap")
PRIVATE_FILES = "\0\0\0\0HFS+ Private Data"        # where hard-linked files really live
PRIVATE_FOLDERS = ".HFS+ Private Directory Data\r"  # where hard-linked folders really live
SYSTEM_NAMES = frozenset({
    ".journal", ".journal_info_block", ".fseventsd", ".Spotlight-V100", ".DocumentRevisions-V100",
    ".TemporaryItems", ".hotfiles.btree", ".PKInstallSandboxManager", ".PKInstallSandboxManager-SystemSoftware",
    ".vol", ".file", ".metadata_never_index", ".com.apple.timemachine.donotpresent", ".DS_Store",
    PRIVATE_FILES, PRIVATE_FOLDERS,
})
DECMPFS_NAME = "com.apple.decmpfs".encode("utf-16-be")
MAX_LINKED_NODES = 2_000_000          # copies made to show folder hard links (Time Machine)
MAX_JOURNAL = 512 << 20


# ------------------------------------------------------------------------------- headers
@dataclass
class Fork:
    size: int                         # bytes
    blocks: int                       # allocation blocks
    extents: list[tuple[int, int]]    # (first block, block count) of the first eight pieces

    @classmethod
    def parse(cls, raw: bytes, pos: int) -> Fork:
        size, _clump, blocks = struct.unpack_from(">QII", raw, pos)
        pairs = struct.unpack_from(">16I", raw, pos + 16)
        return cls(size, blocks, [(pairs[i], pairs[i + 1]) for i in range(0, 16, 2) if pairs[i + 1]])


@dataclass
class HfsHeader:
    start: int                        # device offset of the volume (inside an HFS wrapper, if any)
    signature: bytes
    attributes: int
    journal_info_block: int
    modify_date: int
    file_count: int
    folder_count: int
    block_size: int
    total_blocks: int
    free_blocks: int
    finder_info: bytes
    allocation: Fork
    extents: Fork
    catalog: Fork
    attributes_file: Fork
    used_backup: bool = False
    wrapped: bool = False

    @property
    def volume_size(self) -> int:
        return self.total_blocks * self.block_size

    @property
    def journaled(self) -> bool:
        return bool(self.attributes & VOL_JOURNALED)

    @classmethod
    def parse(cls, raw: bytes, start: int) -> HfsHeader | None:
        """Parse a volume header (the 512 bytes at volume offset 1024)."""
        if len(raw) < 512 or raw[:2] not in (b"H+", b"HX"):
            return None
        version = struct.unpack_from(">H", raw, 2)[0]
        if version not in (4, 5):
            return None
        attributes, _mounted, journal_block = struct.unpack_from(">I4sI", raw, 4)
        (_create, modify, _backup, _checked, files, folders, block_size, total,
         free) = struct.unpack_from(">9I", raw, 16)
        if block_size < 512 or block_size > 1 << 20 or block_size & (block_size - 1):
            return None
        if total == 0 or free > total:
            return None
        catalog = Fork.parse(raw, 272)
        if not catalog.extents or catalog.size == 0:
            return None
        return cls(start, bytes(raw[:2]), attributes, journal_block, modify, files, folders, block_size, total,
                   free, bytes(raw[80:112]), Fork.parse(raw, 112), Fork.parse(raw, 192), catalog,
                   Fork.parse(raw, 352))


def embedded_start(mdb: bytes, start: int) -> int | None:
    """Where the HFS+ volume inside an HFS wrapper (old Macs) starts, or None."""
    if len(mdb) < 0x82 or mdb[:2] != b"BD" or mdb[0x7C:0x7E] not in (b"H+", b"HX"):
        return None
    block_size = struct.unpack_from(">I", mdb, 0x14)[0]
    first_sector = struct.unpack_from(">H", mdb, 0x1C)[0]
    embed_block = struct.unpack_from(">H", mdb, 0x7E)[0]
    if block_size < 512 or block_size % 512:
        return None
    return start + first_sector * 512 + embed_block * block_size


def primary_header(reader: RescueReader, offset: int, head: bytes) -> HfsHeader | None:
    """The HFS+ volume header at ``offset`` (``head``: the first 2 KiB there), also inside an HFS
    wrapper (old Macs)."""
    found = HfsHeader.parse(head[1024:1536], offset)
    if found is not None:
        return found
    inner = embedded_start(head[1024:1536], offset)
    if inner is not None and inner + 1536 <= reader.size:
        found = HfsHeader.parse(bytes(reader.read_critical(inner + 1024, 512).data), inner)
        if found is not None:
            found.wrapped = True
            return found
    return None


def backup_header(reader: RescueReader, offset: int, size: int) -> HfsHeader | None:
    """The copy of the volume header kept 1 KiB before the end of the volume."""
    if size <= 1536 or offset + size > reader.size:
        return None
    found = HfsHeader.parse(bytes(reader.read_critical(offset + size - 1024, 512).data), offset)
    if found is None or found.volume_size > size + found.block_size:
        return None
    found.used_backup = True
    return found


# ------------------------------------------------------------------------------- B-trees
def node_records(node: bytes) -> tuple[int, int, int, list[tuple[int, int]]] | None:
    """(kind, height, next node, [(start, end) of every record]) or None if not a sound node."""
    size = len(node)
    if size < 512:
        return None
    flink, _blink, kind, height, count = struct.unpack_from(">IIbBH", node, 0)
    if kind not in (-1, 0, 1, 2) or count == 0 or 14 + 2 * (count + 1) > size:
        return None
    table = struct.unpack_from(f">{count + 1}H", node, size - 2 * (count + 1))[::-1]
    limit = size - 2 * (count + 1)
    if table[0] != 14:
        return None
    spans = []
    for i in range(count):
        a, b = table[i], table[i + 1]
        if b <= a or b > limit:
            return None
        spans.append((a, b))
    return kind, height, flink, spans


def decode_name(raw: bytes) -> str:
    """A catalog name as macOS shows it in Terminal: composed accents, ':' where HFS+ keeps '/'."""
    name = raw.decode("utf-16-be", "replace")
    return unicodedata.normalize("NFC", name).replace("/", ":")


class HfsFile:
    """What Lifeboat keeps of a catalog file record."""

    __slots__ = ("cnid", "data", "rsrc", "owner_flags", "mode", "ftype", "creator", "special", "deleted")

    def __init__(self, cnid: int, data: Fork, rsrc: Fork, owner_flags: int, mode: int, ftype: bytes,
                 creator: bytes, special: int) -> None:
        self.cnid = cnid
        self.data = data
        self.rsrc = rsrc
        self.owner_flags = owner_flags
        self.mode = mode
        self.ftype = ftype
        self.creator = creator
        self.special = special
        self.deleted = False

    @property
    def compressed(self) -> bool:
        return bool(self.owner_flags & UF_COMPRESSED)

    @property
    def symlink(self) -> bool:
        return (self.ftype, self.creator) == SYMLINK or self.mode & S_IFMT == S_IFLNK


class Entry:
    """One catalog record: a folder, a file, or a thread (which names a folder or file by id)."""

    __slots__ = ("kind", "parent", "name", "cnid", "file", "ctime", "mtime", "atime", "hidden")

    def __init__(self, kind: int, parent: int, name: str, cnid: int, file: HfsFile | None = None,
                 ctime: float | None = None, mtime: float | None = None, atime: float | None = None,
                 hidden: bool = False) -> None:
        self.kind = kind
        self.parent = parent
        self.name = name
        self.cnid = cnid
        self.file = file
        self.ctime = ctime
        self.mtime = mtime
        self.atime = atime
        self.hidden = hidden


def parse_catalog_record(rec: bytes) -> Entry | None:
    if len(rec) < 8:
        return None
    key_len, parent, name_len = struct.unpack_from(">HIH", rec, 0)
    if key_len < 6 or key_len + 2 > len(rec) or name_len > 255 or 6 + 2 * name_len > key_len:
        return None
    name_raw = bytes(rec[8:8 + 2 * name_len])
    pos = 2 + key_len + (key_len & 1)
    data = rec[pos:]
    if len(data) < 10:
        return None
    kind = struct.unpack_from(">h", data, 0)[0]
    if kind == FOLDER and len(data) >= 88:
        cnid, create, modify, _attr, access = struct.unpack_from(">5I", data, 8)
        finder_flags = struct.unpack_from(">H", data, 56)[0]
        name = decode_name(name_raw)
        return Entry(FOLDER, parent, name, cnid, None, hfs_time(create), hfs_time(modify), hfs_time(access),
                     bool(finder_flags & FINDER_INVISIBLE) or name.startswith("."))
    if kind == FILE and len(data) >= 248:
        cnid, create, modify, _attr, access = struct.unpack_from(">5I", data, 8)
        owner_flags, mode, special = struct.unpack_from(">xBHI", data, 40)
        ftype, creator = bytes(data[48:52]), bytes(data[52:56])
        finder_flags = struct.unpack_from(">H", data, 56)[0]
        info = HfsFile(cnid, Fork.parse(data, 88), Fork.parse(data, 168), owner_flags, mode, ftype, creator, special)
        name = decode_name(name_raw)
        return Entry(FILE, parent, name, cnid, info, hfs_time(create), hfs_time(modify), hfs_time(access),
                     bool(finder_flags & FINDER_INVISIBLE) or name.startswith("."))
    if kind in (FOLDER_THREAD, FILE_THREAD) and name_len == 0:
        thread_parent, thread_len = struct.unpack_from(">IH", data, 4)
        if thread_len > 255 or 10 + 2 * thread_len > len(data):
            return None
        return Entry(kind, thread_parent, decode_name(bytes(data[10:10 + 2 * thread_len])), parent)
    return None


def leaf_entries(node: bytes) -> list[Entry] | None:
    """The catalog records of a leaf node, or None if this is not a catalog leaf node."""
    parsed = node_records(node)
    if parsed is None or parsed[0] != LEAF or parsed[1] != 1:
        return None
    out = []
    for a, b in parsed[3]:
        entry = parse_catalog_record(node[a:b])
        if entry is not None:
            out.append(entry)
    return out


def journal_checksum(data: bytes) -> int:
    value = 0
    for byte in data:
        value = ((value << 8) ^ (value + byte)) & 0xFFFFFFFF
    return ~value & 0xFFFFFFFF


# -------------------------------------------------------------------------------- volume
class HfsPlusVolume(Volume):
    kind = "HFS+"

    def __init__(
        self,
        reader: RescueReader,
        header: HfsHeader,
        events: EventBus | None = None,
        control: JobControl | None = None,
        metadata_retry_seconds: float = 180.0,
    ) -> None:
        super().__init__(reader, header.start, header.volume_size)
        self.header = header
        self.block_size = header.block_size
        self.cluster_size = header.block_size
        self.events = events
        self.control = control
        self.metadata_retry_seconds = metadata_retry_seconds
        self.case_sensitive = header.signature == b"HX"
        self.serial = header.finder_info[24:32].hex().upper() if any(header.finder_info[24:32]) else ""
        self.overlay: dict[int, bytes] = {}                 # journal replay: device offset -> bytes
        self._overlay_keys: list[int] = []
        self._overlay_unit = 512
        self.overflow: dict[tuple[int, int], list[tuple[int, list[tuple[int, int]]]]] = {}
        self.decmpfs: dict[int, bytes] = {}
        self.old_decmpfs: dict[int, bytes] = {}             # from attribute nodes no longer in use
        self.catalog_extents: list[Extent] = []
        self.node_size = 0
        self.unreadable_nodes = 0
        self.deleted_found = 0
        self._bitmap_pages: dict[int, bytes | None] = {}
        self._allocation: list[Extent] | None = None
        self._journal: tuple[int, int, int, int, str] | None = None   # base, size, block, header size, endian
        if header.used_backup:
            self._warn("The HFS+ volume header is damaged; using its backup copy.", E_BOOT_BACKUP)

    # ----------------------------------------------------------------- basics
    def _warn(self, message: str, code: str = E_META_CORRUPT) -> None:
        self.warnings.append(message)
        if self.events is not None:
            self.events.warning(message, code=code, source="hfs+")

    def _check(self) -> None:
        if self.control is not None:
            self.control.check()

    def block_offset(self, block: int) -> int:
        return self.offset + block * self.block_size

    def _read(self, offset: int, length: int, mode: ReadMode | None = ReadMode.FAST
              ) -> tuple[bytearray, list[tuple[int, int]]]:
        """Read metadata with the journal's unfinished changes applied: (bytes, unreadable ranges).
        ``mode`` None reads as hard as the drive allows (``read_critical``)."""
        outcome = (self.reader.read_critical(offset, length) if mode is None
                   else self.reader.read(offset, length, mode))
        data = bytearray(outcome.data)
        holes = [(s - offset, e - offset) for s, e in outcome.unread + outcome.bad]
        if self._overlay_keys:
            unit = self._overlay_unit
            i = bisect.bisect_left(self._overlay_keys, offset - unit + 1)
            while i < len(self._overlay_keys) and self._overlay_keys[i] < offset + length:
                key = self._overlay_keys[i]
                block = self.overlay[key]
                lo, hi = max(offset, key), min(offset + length, key + len(block))
                data[lo - offset:hi - offset] = block[lo - key:hi - key]
                holes = _cut(holes, lo - offset, hi - offset)
                i += 1
        return data, holes

    def _fork_extents(self, fork: Fork, cnid: int, fork_type: int, overflow: bool = True
                      ) -> tuple[list[Extent], list[str]]:
        """The device locations of a fork's bytes, in file order."""
        pieces = list(fork.extents)
        covered = sum(count for _start, count in pieces)
        problems: list[str] = []
        if overflow and covered < fork.blocks:
            for first, more in self.overflow.get((cnid, fork_type), []):
                if first != covered:
                    continue
                pieces.extend(more)
                covered += sum(count for _start, count in more)
                if covered >= fork.blocks:
                    break
        if covered * self.block_size < fork.size:
            problems.append("The location of part of this file is unknown.")
        extents: list[Extent] = []
        pos = 0
        bs = self.block_size
        for start, count in pieces:
            if pos >= fork.size:
                break
            length = min(count * bs, fork.size - pos)
            if start + count > self.header.total_blocks:
                extents.append(Extent(pos, length, INVALID))
                problems.append("Part of this file points outside the volume.")
            else:
                extents.append(Extent(pos, length, self.block_offset(start)))
            pos += length
        return extents, problems

    def _read_extents(self, extents: list[Extent], start: int, length: int, mode: ReadMode | None
                      ) -> tuple[bytearray, list[tuple[int, int]]]:
        """Read ``length`` bytes at ``start`` of a file described by ``extents``."""
        out = bytearray(length)
        holes: list[tuple[int, int]] = []
        end = start + length
        covered = start
        for ext in extents:
            lo, hi = max(start, ext.file_offset), min(end, ext.file_end)
            if lo >= hi:
                continue
            if lo > covered:
                holes.append((covered - start, lo - start))
            covered = max(covered, hi)
            if ext.disk_offset < 0:
                holes.append((lo - start, hi - start))
                continue
            data, bad = self._read(ext.disk_offset + (lo - ext.file_offset), hi - lo, mode)
            out[lo - start:hi - start] = data
            holes.extend((s + lo - start, e + lo - start) for s, e in bad)
        if covered < end:
            holes.append((covered - start, end - start))
        return out, holes

    # ---------------------------------------------------------------- journal
    def _journal_read(self, pos: int, length: int, mode: ReadMode | None = None) -> bytes:
        """``length`` bytes of the journal from ``pos``, wrapping around its end like macOS does."""
        assert self._journal is not None
        base, size, block, _hdr, _e = self._journal
        out = bytearray()
        while length > 0:
            if pos >= size:
                pos = block + (pos - size)
            take = min(length, size - pos)
            data, _bad = self._read(base + pos, take, mode)
            out += data
            pos += take
            length -= take
        return bytes(out)

    def _open_journal(self) -> None:
        hdr = self.header
        if not hdr.journaled or not hdr.journal_info_block or hdr.journal_info_block >= hdr.total_blocks:
            return
        info = bytes(self.reader.read_critical(self.block_offset(hdr.journal_info_block), 512).data)
        flags = struct.unpack_from(">I", info, 0)[0]
        if flags & 2:
            self._warn("The HFS+ journal is on another drive; its unfinished changes can't be applied.")
            return
        if not flags & 1:
            return
        j_offset, j_size = struct.unpack_from(">QQ", info, 36)
        base = self.offset + j_offset
        if j_size < 4096 or j_offset + j_size > self.size or base + 512 > self.reader.size:
            return
        head = bytes(self.reader.read_critical(base, 512).data)
        if head[:4] == b"xLNJ":
            endian = "<"
        elif head[:4] == b"JNLx":
            endian = ">"
        else:
            self._warn("The HFS+ journal is damaged; any changes it held were not applied.")
            return
        _magic, _e, start, end, size, blhdr_size, _sum, block = struct.unpack_from(endian + "IIQQQIII", head, 0)
        if (size != j_size or block < 512 or block > 65536 or block & (block - 1) or blhdr_size < block
                or not block <= start < size or not block <= end < size):
            self._warn("The HFS+ journal header is damaged; any changes it held were not applied.")
            return
        self._journal = (base, size, block, blhdr_size, endian)
        if start != end:
            self._replay(start, end)

    def _block_list(self, raw: bytes) -> list[tuple[int, int]] | None:
        """The (device block, size) entries of a journal block list header, or None if not one."""
        assert self._journal is not None
        _base, size, _block, blhdr_size, endian = self._journal
        if len(raw) < 32:
            return None
        max_blocks, count, used, checksum, _flags = struct.unpack_from(endian + "HHiIi", raw, 0)
        if count < 1 or count > max_blocks or used < blhdr_size or used > size or 16 + 16 * count > blhdr_size:
            return None
        check = bytearray(raw[:32])
        check[8:12] = b"\0\0\0\0"
        if journal_checksum(bytes(check)) != checksum:
            return None
        entries = []
        for i in range(1, count):
            bnum, bsize = struct.unpack_from(endian + "qI", raw, 16 + 16 * i)
            entries.append((bnum, bsize))
        return entries

    def _replay(self, start: int, end: int) -> None:
        """Apply the journal's unfinished transactions in memory, oldest first."""
        assert self._journal is not None
        _base, size, block, blhdr_size, endian = self._journal
        changes: dict[int, bytes] = {}
        pos = start
        transactions = 0
        while pos != end and transactions < 100_000:
            raw = self._journal_read(pos, blhdr_size)
            entries = self._block_list(raw)
            if entries is None:
                self._warn("Part of the HFS+ journal is damaged; only the changes before it were applied.")
                break
            used = struct.unpack_from(endian + "i", raw, 4)[0]
            data_pos = pos + blhdr_size
            for bnum, bsize in entries:
                if data_pos >= size:
                    data_pos = block + (data_pos - size)
                if bnum >= 0 and 0 < bsize <= 1 << 20:
                    data = self._journal_read(data_pos, bsize)
                    device = self.offset + bnum * block
                    for k in range(0, bsize, block):
                        changes[device + k] = data[k:k + block]
                data_pos += bsize
            transactions += 1
            pos += used
            if pos >= size:
                pos = block + (pos - size)
        if changes:
            self.overlay = changes
            self._overlay_unit = block
            self._overlay_keys = sorted(changes)
            self._warn(f"The drive was disconnected without being ejected: Lifeboat applied "
                       f"{plural(transactions, 'unfinished change')} from the journal, in memory only (the drive "
                       "is not written to).", E_META_CORRUPT)

    # ---------------------------------------------------------------- B-trees
    def _scan_btree(self, extents: list[Extent], fork_size: int, what: str,
                    visit: Callable[[int, bytes, bool | None], None],
                    progress: Callable[[Progress], None] | None) -> tuple[int, int]:
        """Read a B-tree file front to back; call ``visit(node number, node, in use)`` for every
        node (in use: from the tree's map, None if unknown). Returns (node size, first leaf)."""
        head, holes = self._read_extents(extents, 0, 512, None)
        if holes or head[8] != HEADER:
            self._warn(f"The {what} is unreadable or damaged.", E_META_UNREADABLE)
            return 0, 0
        first_leaf, _last = struct.unpack_from(">II", head, 24)
        node_size, _max_key, total = struct.unpack_from(">HHI", head, 32)
        if node_size < 512 or node_size > 32768 or node_size & (node_size - 1):
            self._warn(f"The {what} header is damaged.", E_META_CORRUPT)
            return 0, 0
        total = min(total, fork_size // node_size)
        used = self._btree_map(extents, node_size, total)
        per_chunk = max(1, (4 << 20) // node_size)
        pending: list[int] = []
        last_report = 0.0
        for first in range(0, total, per_chunk):
            self._check()
            count = min(per_chunk, total - first)
            data, bad = self._read_extents(extents, first * node_size, count * node_size, ReadMode.FAST)
            for i in range(count):
                lo, hi = i * node_size, (i + 1) * node_size
                if any(s < hi and e > lo for s, e in bad):
                    pending.append(first + i)
                    continue
                number = first + i
                visit(number, bytes(data[lo:hi]), _bit(used, number))
            now = time.monotonic()
            if progress is not None and now - last_report > 0.25:
                last_report = now
                progress(Progress(f"Reading the {what}", (first + count) * node_size, total * node_size,
                                  item=f"{first + count:,} of {plural(total, 'node')}"))
        if pending:
            pending = self._retry_nodes(extents, node_size, pending, used, visit, what, progress)
        if pending:
            self.unreadable_nodes += len(pending)
            lost = len(pending)
            self._warn(f"{plural(lost, 'node')} ({format_size(lost * node_size)}) of the {what} "
                       f"{'is' if lost == 1 else 'are'} unreadable; files {'it' if lost == 1 else 'they'} "
                       "described may be missing.", E_META_UNREADABLE)
        return node_size, first_leaf

    def _retry_nodes(self, extents: list[Extent], node_size: int, pending: list[int], used: bytes | None,
                     visit: Callable[[int, bytes, bool | None], None], what: str,
                     progress: Callable[[Progress], None] | None) -> list[int]:
        deadline = time.monotonic() + self.metadata_retry_seconds
        for mode in (ReadMode.SWEEP, ReadMode.TRIM, ReadMode.SCRAPE):
            still = []
            for index, number in enumerate(pending):
                self._check()
                if time.monotonic() > deadline:
                    still.extend(pending[index:])
                    break
                if progress is not None:
                    progress(Progress(f"Re-reading damaged parts of the {what}", index, len(pending), unit="part"))
                data, bad = self._read_extents(extents, number * node_size, node_size, mode)
                if bad:
                    still.append(number)
                else:
                    visit(number, bytes(data), _bit(used, number))
            pending = still
            if not pending or time.monotonic() > deadline:
                break
        return pending

    def _btree_map(self, extents: list[Extent], node_size: int, total: int) -> bytes | None:
        """Which nodes the tree uses (one bit per node), or None if the map is unreadable."""
        node, holes = self._read_extents(extents, 0, node_size, None)
        parsed = node_records(bytes(node))
        if holes or parsed is None or len(parsed[3]) < 3:
            return None
        a, b = parsed[3][2]
        bitmap = bytearray(node[a:b])
        follow = parsed[2]
        seen = set()
        while len(bitmap) * 8 < total and follow and follow not in seen and follow < total:
            seen.add(follow)
            more, bad = self._read_extents(extents, follow * node_size, node_size, None)
            parsed = node_records(bytes(more))
            if bad or parsed is None or parsed[0] != MAP or not parsed[3]:
                return None
            a, b = parsed[3][0]
            bitmap += more[a:b]
            follow = parsed[2]
        return bytes(bitmap) if len(bitmap) * 8 >= total else None

    def _load_overflow(self, progress: Callable[[Progress], None] | None) -> None:
        fork = self.header.extents
        extents, _problems = self._fork_extents(fork, EXTENTS_ID, DATA_FORK, overflow=False)
        if not extents:
            return

        def visit(_number: int, node: bytes, in_use: bool | None) -> None:
            if in_use is False:
                return
            parsed = node_records(node)
            if parsed is None or parsed[0] != LEAF:
                return
            for a, b in parsed[3]:
                rec = node[a:b]
                if len(rec) < 12 + 64:
                    continue
                key_len, fork_type, cnid, first = struct.unpack_from(">HBxII", rec, 0)
                if key_len != 10:
                    continue
                pairs = struct.unpack_from(">16I", rec, 12)
                pieces = [(pairs[i], pairs[i + 1]) for i in range(0, 16, 2) if pairs[i + 1]]
                self.overflow.setdefault((cnid, fork_type), []).append((first, pieces))

        self._scan_btree(extents, fork.size, "extents overflow file", visit, progress)
        for items in self.overflow.values():
            items.sort()

    def _load_attributes(self, progress: Callable[[Progress], None] | None) -> None:
        fork = self.header.attributes_file
        if fork.size == 0:
            return
        extents, _problems = self._fork_extents(fork, ATTRIBUTES_ID, DATA_FORK)

        def visit(_number: int, node: bytes, in_use: bool | None) -> None:
            parsed = node_records(node)
            if parsed is None or parsed[0] != LEAF:
                return
            target = self.decmpfs if in_use is not False else self.old_decmpfs
            for a, b in parsed[3]:
                rec = node[a:b]
                if len(rec) < 14:
                    continue
                key_len, cnid, start_block, name_len = struct.unpack_from(">HxxIIH", rec, 0)
                if start_block or 12 + 2 * name_len > key_len or bytes(rec[14:14 + 2 * name_len]) != DECMPFS_NAME:
                    continue
                pos = 2 + key_len + (key_len & 1)
                if pos + 16 > len(rec):
                    continue
                kind = struct.unpack_from(">I", rec, pos)[0]
                if kind == ATTR_INLINE:
                    size = struct.unpack_from(">I", rec, pos + 12)[0]
                    value = bytes(rec[pos + 16:pos + 16 + size])
                elif kind == ATTR_FORK and pos + 88 <= len(rec):
                    value = self._read_attr_fork(Fork.parse(bytes(rec), pos + 8))
                else:
                    continue
                if value and cnid not in target:
                    target[cnid] = value

        self._scan_btree(extents, fork.size, "attributes file", visit, progress)

    def _read_attr_fork(self, fork: Fork) -> bytes:
        if fork.size > 64 << 20:
            return b""
        extents, _problems = self._fork_extents(fork, 0, DATA_FORK, overflow=False)
        data, holes = self._read_extents(extents, 0, fork.size, None)
        return b"" if holes else bytes(data)

    # ---------------------------------------------------------------- catalog
    def load(self, progress: Callable[[Progress], None] | None = None) -> Node:
        self._open_journal()
        self._load_overflow(progress)
        self.catalog_extents, problems = self._fork_extents(self.header.catalog, CATALOG_ID, DATA_FORK)
        if problems:
            self._warn("Part of the catalog's location is unknown; some files may be missing.", E_META_CORRUPT)
        self._load_attributes(progress)
        live: dict[int, Entry] = {}
        stale: list[Entry] = []
        nodes: dict[int, tuple[int, list[Entry]]] = {}
        uses: dict[int, bool | None] = {}

        def visit(number: int, node: bytes, in_use: bool | None) -> None:
            entries = leaf_entries(node)
            if entries is None:
                return
            nodes[number] = (struct.unpack_from(">I", node, 0)[0], entries)
            uses[number] = in_use

        self.node_size, first_leaf = self._scan_btree(self.catalog_extents, self.header.catalog.size, "catalog",
                                                      visit, progress)
        if not self.node_size:
            raise ValueError("the HFS+ catalog is unreadable")
        chain = _chain(nodes, first_leaf)
        for number, (_flink, entries) in nodes.items():
            in_use = uses[number]
            if in_use is None:
                in_use = number in chain
            if in_use:
                for entry in entries:
                    if entry.kind in (FOLDER, FILE):
                        live.setdefault(entry.cnid, entry)
            else:
                stale.extend(entries)
        stale.extend(self._journal_entries(progress))
        return self._build_tree(live, stale, progress)

    def _journal_entries(self, progress: Callable[[Progress], None] | None) -> list[Entry]:
        """Catalog records in the old node copies kept by the journal (deleted files live here).
        The journal is read 4 MiB at a time; only catalog nodes are read out of it."""
        if self._journal is None or not self.node_size:
            return []
        base, size, block, blhdr_size, _endian = self._journal
        if size > MAX_JOURNAL:
            return []
        if progress is not None:
            progress(Progress("Looking for deleted files in the journal"))
        out: list[Entry] = []
        node_size = self.node_size
        window = 4 << 20
        pos = block
        while pos < size:
            self._check()
            length = min(window, size - pos)
            data, _holes = self._read(base + pos, length, ReadMode.FAST)
            for rel in range(0, length, block):
                if data[rel + 2:rel + 4] == b"\0\0":          # no blocks listed: not a list header
                    continue
                raw = (bytes(data[rel:rel + blhdr_size]) if rel + blhdr_size <= length
                       else self._journal_read(pos + rel, blhdr_size, ReadMode.FAST))
                entries = self._block_list(raw)
                if entries is None:
                    continue
                data_pos = pos + rel + blhdr_size
                for bnum, bsize in entries:
                    if data_pos >= size:
                        data_pos = block + (data_pos - size)
                    if bnum >= 0 and node_size <= bsize <= 1 << 20:
                        fork_offset = self._catalog_offset(self.offset + bnum * block)
                        if fork_offset is not None and fork_offset % node_size == 0:
                            blob = self._journal_read(data_pos, bsize, ReadMode.FAST)
                            for k in range(0, bsize - node_size + 1, node_size):
                                found = leaf_entries(blob[k:k + node_size])
                                if found:
                                    out.extend(found)
                    data_pos += bsize
            pos += length
        return out

    def _catalog_offset(self, device: int) -> int | None:
        for ext in self.catalog_extents:
            if ext.disk_offset >= 0 and ext.disk_offset <= device < ext.disk_offset + ext.length:
                return ext.file_offset + (device - ext.disk_offset)
        return None

    # ------------------------------------------------------------------ tree
    def _build_tree(self, live: dict[int, Entry], stale: list[Entry],
                    progress: Callable[[Progress], None] | None) -> Node:
        if progress is not None:
            progress(Progress("Rebuilding folders"))
        root_entry = live.get(ROOT_FOLDER)
        root = Node("", F.DIR | F.VOLUME | F.VIRTUAL, volume=self)
        if root_entry is not None:
            self.label = root_entry.name
            root.ctime, root.mtime = root_entry.ctime, root_entry.mtime
        folders: dict[int, Node] = {ROOT_FOLDER: root}
        for cnid, entry in live.items():
            if entry.kind == FOLDER and cnid != ROOT_FOLDER:
                folders[cnid] = self._folder_node(entry, deleted=False)
        lost = Node("Lost files", F.DIR | F.VIRTUAL, volume=self)
        lost_groups: dict[int, Node] = {}

        def lost_group(parent: int) -> Node:
            group = lost_groups.get(parent)
            if group is None:
                group = Node(f"Folder #{parent}", F.DIR | F.VIRTUAL | F.ORPHAN, volume=self)
                lost_groups[parent] = group
                lost.add(group)
            return group

        for cnid, node in folders.items():
            if cnid == ROOT_FOLDER:
                continue
            parent = folders.get(live[cnid].parent)
            if parent is None or parent is node:
                node.flags |= F.ORPHAN
                lost_group(live[cnid].parent).add(node)
            else:
                parent.add(node)
        _break_cycles(root, folders, lost)
        inodes = self._private_children(live, PRIVATE_FILES, "iNode")
        folder_links: list[tuple[Node, int]] = []
        for _cnid, entry in live.items():
            if entry.kind != FILE or entry.file is None:
                continue
            parent = folders.get(entry.parent)
            target = entry.file
            flags = 0
            if (target.ftype, target.creator) == FILE_LINK:
                real = inodes.get(target.special)
                if real is not None and real.file is not None:
                    target = real.file
                    flags |= F.HARDLINK
            if (entry.file.ftype, entry.file.creator) == FOLDER_LINK:
                link = Node(entry.name, F.DIR | F.HARDLINK, volume=self, ctime=entry.ctime, mtime=entry.mtime,
                            atime=entry.atime)
                folder_links.append((link, entry.file.special))
                node = link
            else:
                node = self._file_node(entry, target, flags)
            if parent is None:
                node.flags |= F.ORPHAN
                lost_group(entry.parent).add(node)
            else:
                parent.add(node)
                if node.children is None and target.rsrc.size and not target.compressed:
                    stream = Node(f"{entry.name}:rsrc", F.STREAM | F.HIDDEN, target.rsrc.size, volume=self,
                                  ref=("rsrc", target), ctime=entry.ctime, mtime=entry.mtime)
                    parent.add(stream)
        self._expand_folder_links(folder_links, live, folders)
        self._add_deleted(live, stale, folders, lost_group)
        for node in root.walk():
            if node.flags & F.SYSTEM and node.children:
                for item in node.walk():
                    item.flags |= F.SYSTEM
        if lost.children:
            root.add(lost)
        self.root = root
        return root

    def _private_children(self, live: dict[int, Entry], folder_name: str, prefix: str) -> dict[int, Entry]:
        """Children of a private folder at the root named ``prefix<number>``, by number."""
        private = next((e.cnid for e in live.values()
                        if e.kind == FOLDER and e.parent == ROOT_FOLDER and e.name == folder_name), None)
        out: dict[int, Entry] = {}
        if private is None:
            return out
        for entry in live.values():
            if entry.parent == private and entry.name.startswith(prefix) and entry.name[len(prefix):].isdigit():
                out[int(entry.name[len(prefix):])] = entry
        return out

    def _folder_node(self, entry: Entry, deleted: bool) -> Node:
        flags = F.DIR
        if deleted:
            flags |= F.DELETED
        if entry.hidden:
            flags |= F.HIDDEN
        if entry.name in SYSTEM_NAMES:
            flags |= F.SYSTEM | F.HIDDEN
        return Node(entry.name, flags, volume=self, ref=entry.cnid, ctime=entry.ctime, mtime=entry.mtime,
                    atime=entry.atime)

    def _file_node(self, entry: Entry, target: HfsFile, extra: int = 0, deleted: bool = False) -> Node:
        flags = extra
        if deleted:
            flags |= F.DELETED
        if entry.hidden:
            flags |= F.HIDDEN
        if entry.name in SYSTEM_NAMES or (entry.parent == ROOT_FOLDER and entry.cnid < FIRST_USER_ID):
            flags |= F.SYSTEM | F.HIDDEN
        size = target.data.size
        if target.compressed:
            flags |= F.COMPRESSED
            raw = self.decmpfs.get(target.cnid) or self.old_decmpfs.get(target.cnid)
            info = parse_decmpfs(raw) if raw else None
            if info is not None:
                size = info.size
                if info.unsupported():
                    flags |= F.UNSUPPORTED
        if target.symlink:
            flags |= F.REPARSE | F.SYSTEM
        return Node(entry.name, flags, size, volume=self, ref=target, ctime=entry.ctime, mtime=entry.mtime,
                    atime=entry.atime)

    def _expand_folder_links(self, links: list[tuple[Node, int]], live: dict[int, Entry],
                             folders: dict[int, Node]) -> None:
        """Show folder hard links (Time Machine backups) as copies of the folder they point to."""
        if not links:
            return
        targets = self._private_children(live, PRIVATE_FOLDERS, "dir_")
        pending = {id(link): number for link, number in links}
        queue = deque(links)
        budget = MAX_LINKED_NODES
        skipped = 0
        while queue:
            link, number = queue.popleft()
            entry = targets.get(number)
            source = folders.get(entry.cnid) if entry is not None else None
            if source is None:
                link.flags |= F.DAMAGED_META
                continue
            if budget <= 0:
                skipped += 1
                continue
            budget -= self._copy_children(source, link, pending, queue)
        if skipped:
            self._warn(f"This drive holds a Time Machine backup with more linked folders than Lifeboat can show at "
                       f"once; {skipped:,} of them are shown empty. Their files are in the other backups' copies.")

    def _copy_children(self, source: Node, target: Node, pending: dict[int, int],
                       queue: deque[tuple[Node, int]]) -> int:
        copied = 0
        stack = [(source, target)]
        while stack:
            src, dst = stack.pop()
            for child in src.children or ():
                if child.flags & F.STREAM:
                    continue
                twin = Node(child.name, child.flags, child.size, volume=self, ref=child.ref, ctime=child.ctime,
                            mtime=child.mtime, atime=child.atime)
                dst.add(twin)
                copied += 1
                if child.children is None:
                    continue
                number = pending.get(id(child))
                if number is not None:          # a folder link inside: expanded in its turn
                    pending[id(twin)] = number
                    queue.append((twin, number))
                else:
                    stack.append((child, twin))
        return copied

    # --------------------------------------------------------------- deleted
    def _add_deleted(self, live: dict[int, Entry], stale: list[Entry], folders: dict[int, Node],
                     lost_group: Callable[[int], Node]) -> None:
        old_folders: dict[int, Entry] = {}
        old_files: dict[int, Entry] = {}
        names: dict[int, Entry] = {}
        for entry in stale:
            if entry.cnid in live or entry.cnid < FIRST_USER_ID:
                continue
            if entry.kind == FOLDER:
                old_folders.setdefault(entry.cnid, entry)
            elif entry.kind == FILE and entry.file is not None:
                best = old_files.get(entry.cnid)
                if best is None or _better(entry, best):
                    old_files[entry.cnid] = entry
            else:
                names.setdefault(entry.cnid, entry)
        made: dict[int, Node] = {}

        def folder_for(cnid: int, depth: int = 0) -> Node:
            existing = folders.get(cnid) or made.get(cnid)
            if existing is not None:
                return existing
            entry = old_folders.get(cnid)
            if entry is None and cnid in names and names[cnid].kind == FOLDER_THREAD:
                thread = names[cnid]
                entry = Entry(FOLDER, thread.parent, thread.name, cnid)
            if entry is None or depth > 64:
                return lost_group(cnid)
            node = self._folder_node(entry, deleted=True)
            made[cnid] = node
            folder_for(entry.parent, depth + 1).add(node)
            return node

        for cnid in sorted(old_files):
            entry = old_files[cnid]
            assert entry.file is not None
            if (entry.file.ftype, entry.file.creator) in (FILE_LINK, FOLDER_LINK) or entry.file.symlink:
                continue
            entry.file.deleted = True
            node = self._file_node(entry, entry.file, deleted=True)
            if node.size and not entry.file.compressed and self._overwritten(entry.file):
                node.flags |= F.OVERWRITTEN
            parent = folder_for(entry.parent)
            if parent.flags & F.ORPHAN:
                node.flags |= F.ORPHAN
            parent.add(node)
            self.deleted_found += 1

    def _overwritten(self, file: HfsFile) -> bool:
        """Whether any of a deleted file's blocks are in use by something else now."""
        return any(self._blocks_in_use(start, count) for start, count in file.data.extents)

    def _blocks_in_use(self, first: int, count: int) -> bool:
        if self._allocation is None:
            self._allocation, _problems = self._fork_extents(self.header.allocation, ALLOCATION_ID, DATA_FORK)
        page_bits = 65536 * 8
        block = first
        end = first + count
        while block < end:
            index = block // page_bits
            if index not in self._bitmap_pages:
                length = min(65536, self.header.allocation.size - index * 65536)
                if length <= 0:
                    self._bitmap_pages[index] = None
                else:
                    data, holes = self._read_extents(self._allocation, index * 65536, length, ReadMode.FAST)
                    self._bitmap_pages[index] = None if holes else bytes(data)
            page = self._bitmap_pages[index]
            stop = min(end, (index + 1) * page_bits)
            if page is not None:
                for b in range(block - index * page_bits, min(stop - index * page_bits, len(page) * 8)):
                    if page[b >> 3] & (0x80 >> (b & 7)):
                        return True
            block = stop
        return False

    # ------------------------------------------------------------------ data
    def layout(self, node: Node) -> FileLayout:
        ref = node.ref
        if isinstance(ref, tuple):
            _kind, file = ref
            extents, problems = self._fork_extents(file.rsrc, file.cnid, RSRC_FORK, overflow=not file.deleted)
            return FileLayout(file.rsrc.size, extents=extents, problems=problems)
        if not isinstance(ref, HfsFile) or node.flags & F.DIR:
            return FileLayout(0)
        if ref.compressed:
            return self._compressed_layout(ref, node.size)
        extents, problems = self._fork_extents(ref.data, ref.cnid, DATA_FORK, overflow=not ref.deleted)
        if ref.deleted and problems:
            problems = ["Only the location of the first part of this deleted file is known."]
        return FileLayout(ref.data.size, extents=extents, problems=problems)

    def _compressed_layout(self, file: HfsFile, size: int) -> FileLayout:
        raw = self.decmpfs.get(file.cnid) or self.old_decmpfs.get(file.cnid)
        info = parse_decmpfs(raw) if raw else None
        if info is None:
            return FileLayout(size, unsupported="Compressed by macOS, but its compression details are missing.")
        if info.unsupported():
            return FileLayout(info.size, unsupported=info.unsupported())
        assert info.method is not None
        if not info.in_resource_fork:
            return FileLayout(info.size, chunked=CompressedChunks(info.method, max(1, info.size),
                                                                  inline=info.inline))
        extents, problems = self._fork_extents(file.rsrc, file.cnid, RSRC_FORK, overflow=not file.deleted)
        source = FileLayout(file.rsrc.size, extents=extents, problems=problems)
        chunked = CompressedChunks(info.method, 0x10000, source=source)
        layout = FileLayout(info.size, chunked=chunked, problems=list(problems))
        try:
            need = table_bytes_needed(info.kind, info.size)
            head, holes = self._read_extents(extents, 0, min(need, file.rsrc.size), None)
            if info.kind == 4 and not holes:
                need = table_bytes_needed(info.kind, info.size, bytes(head))
                head, holes = self._read_extents(extents, 0, min(need, file.rsrc.size), None)
            if holes:
                raise CompressionError("the compression table is unreadable")
            chunked.chunks = chunk_table(info.kind, bytes(head), info.size, file.rsrc.size)
        except CompressionError as exc:
            chunked.error = str(exc)
            layout.problems.append(f"Compressed by macOS: {exc}.")
        return layout


# ------------------------------------------------------------------------------- helpers
def _bit(bitmap: bytes | None, number: int) -> bool | None:
    if bitmap is None or number // 8 >= len(bitmap):
        return None
    return bool(bitmap[number // 8] & (0x80 >> (number % 8)))


def _cut(holes: list[tuple[int, int]], lo: int, hi: int) -> list[tuple[int, int]]:
    """Remove [lo, hi) from a list of ranges."""
    out = []
    for s, e in holes:
        if e <= lo or s >= hi:
            out.append((s, e))
            continue
        if s < lo:
            out.append((s, lo))
        if e > hi:
            out.append((hi, e))
    return out


def _chain(nodes: dict[int, tuple[int, list[Entry]]], first: int) -> set[int]:
    """Leaf nodes reachable from the first leaf by following the forward links."""
    seen: set[int] = set()
    current = first
    while current and current in nodes and current not in seen:
        seen.add(current)
        current = nodes[current][0]
    return seen


def _better(a: Entry, b: Entry) -> bool:
    """Of two old copies of a deleted file's record, is ``a`` the more complete one?"""
    fa, fb = a.file, b.file
    assert fa is not None and fb is not None
    return (a.mtime or 0, len(fa.data.extents), fa.data.size) > (b.mtime or 0, len(fb.data.extents), fb.data.size)


def _break_cycles(root: Node, folders: dict[int, Node], lost: Node) -> None:
    rooted: set[int] = {id(root)}
    for node in list(folders.values()):
        chain = []
        cursor: Node | None = node
        seen: set[int] = set()
        while cursor is not None and id(cursor) not in rooted:
            if id(cursor) in seen:
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
