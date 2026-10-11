"""APFS: the Mac filesystem since 2017 (Macs, external drives, Time Machine disks since macOS 11).

An APFS partition is a container holding one or more volumes ("Macintosh HD", "Macintosh HD -
Data", Preboot, a Time Machine backup...). Lifeboat finds the newest valid checkpoint of the
container, then for every volume walks its file-system tree: a B-tree of records (inodes, folder
entries, file extents, extended attributes) whose nodes are found through the volume's object
map. Index nodes are read first and the leaves after, sorted by position on the drive, so the
drive is read mostly front to back.

APFS never overwrites metadata in place (copy-on-write): every change writes new tree nodes and
leaves the old ones behind. The container keeps several recent checkpoints, each describing the
volumes as they were. Files present in an older checkpoint but gone from the newest one are
recently deleted files; they come back with their names and locations, flagged as overwritten
when their space is used by a live file now.

What Lifeboat reads: case-sensitive and -insensitive volumes, compressed files (zlib, LZVN,
LZFSE), sparse files, clones, hard links, symlinks, resource forks, sealed system volumes, and
snapshots (Time Machine keeps each backup on an APFS disk as one). Encrypted volumes (FileVault,
"APFS (Encrypted)") are detected and reported; they can't be read without unlocking them on a Mac.

Damage handling: the superblock in block 0 falls back to the checkpoint area; every object's
checksum is verified; unreadable or damaged nodes are retried within a time limit, skipped and
reported; folder loops and missing parents go to "Lost files".
"""

from __future__ import annotations

import bisect
import operator
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..errors import E_BOOT_BACKUP, E_FS_UNSUPPORTED, E_META_CORRUPT, E_META_UNREADABLE
from ..events import EventBus, JobControl, Progress
from ..rescue.reader import ReadMode, RescueReader
from ..util import apfs_time, format_size, format_timestamp
from .apfs_crypto import ApfsKeys, xts_decrypt
from .applecomp import CompressionError, chunk_table, parse_decmpfs, table_bytes_needed
from .model import INVALID, SPARSE, CompressedChunks, Extent, F, FileLayout, Node, Volume

# object types (low 16 bits of o_type)
T_NX_SUPERBLOCK, T_BTREE, T_BTREE_NODE, T_OMAP, T_CHECKPOINT_MAP, T_FS, T_FSTREE = 0x1, 0x2, 0x3, 0xB, 0xC, 0xD, 0xE
T_SNAPMETATREE, T_FEXT_TREE = 0x10, 0x1F

# B-tree node flags
BT_ROOT, BT_LEAF, BT_FIXED = 1, 2, 4

# file-system record types (top 4 bits of a key's first 8 bytes)
R_SNAP_METADATA, R_INODE, R_XATTR, R_FILE_EXTENT, R_DIR_REC, R_SNAP_NAME = 1, 3, 4, 8, 9, 11
OBJ_ID_MASK = (1 << 60) - 1

ROOT_DIR, PRIVATE_DIR = 2, 3
FIRST_USER_ID = 16
DT_DIR, DT_LNK = 4, 10
UF_COMPRESSED = 0x20
UF_HIDDEN = 0x8000
S_IFMT, S_IFDIR, S_IFLNK = 0o170000, 0o040000, 0o120000
INO_EXT_DSTREAM = 8
FS_UNENCRYPTED = 0x1
INCOMPAT_CASE_INSENSITIVE = 0x1
INCOMPAT_SEALED = 0x20
OMAP_VAL_DELETED, OMAP_VAL_ENCRYPTED = 0x1, 0x4
XATTR_STREAM, XATTR_EMBEDDED = 0x1, 0x2
OBJ_PHYSICAL = 0x40000000

ROLES = {0: "", 1: "System", 2: "User", 4: "Recovery", 8: "VM", 0x10: "Preboot", 0x20: "Installer", 0x40: "Data",
         0x80: "Baseband", 0xC0: "Update", 0x100: "xART", 0x140: "Hardware", 0x180: "Backup", 0x200: "Enterprise",
         0x280: "Prelogin"}
SYSTEM_ROLES = {1, 4, 8, 0x10, 0x20, 0x80, 0xC0, 0x100, 0x140, 0x280}
SYSTEM_NAMES = frozenset({
    ".fseventsd", ".Spotlight-V100", ".DocumentRevisions-V100", ".TemporaryItems", ".PKInstallSandboxManager",
    ".PKInstallSandboxManager-SystemSoftware", ".vol", ".file", ".metadata_never_index", ".DS_Store",
    ".com.apple.timemachine.donotpresent", "private-dir",
})
WANTED_XATTRS = {"com.apple.decmpfs", "com.apple.ResourceFork", "com.apple.fs.symlink"}
MAX_SNAPSHOT_NODES = 3_000_000
READ_RUN = 4 << 20

_M32 = 0xFFFFFFFF
Piece = tuple[int, int, int, int]       # a file extent: logical offset, length, block, crypto id


def checksum_ok(block: bytes) -> bool:
    """APFS objects start with a Fletcher-64 checksum of the rest of the block."""
    if len(block) < 64 or len(block) % 4:
        return False
    count = (len(block) - 8) // 4
    words = struct.unpack_from(f"<{count}I", block, 8)
    s1 = sum(words) % _M32
    s2 = sum(map(operator.mul, words, range(count, 0, -1))) % _M32
    c1 = _M32 - ((s1 + s2) % _M32)
    c2 = _M32 - ((s1 + c1) % _M32)
    return struct.unpack_from("<Q", block, 0)[0] == (c2 << 32) | c1


def object_header(block: bytes) -> tuple[int, int, int, int]:
    """(oid, xid, type, subtype) of an APFS object."""
    oid, xid, otype, subtype = struct.unpack_from("<QQII", block, 8)
    return oid, xid, otype & 0xFFFF, subtype


# --------------------------------------------------------------------------- superblock
@dataclass
class NxSuperblock:
    start: int                          # device offset of the container
    block_size: int
    block_count: int
    xid: int
    omap_oid: int
    fs_oids: list[int]
    desc_base: int
    desc_blocks: int
    uuid: bytes
    keylocker: tuple[int, int]
    used_backup: bool = False

    @property
    def volume_size(self) -> int:
        return self.block_size * self.block_count

    @classmethod
    def parse(cls, block: bytes, start: int, verify: bool = True) -> NxSuperblock | None:
        if len(block) < 1400 or block[32:36] != b"NXSB":
            return None
        _oid, xid, otype, _sub = object_header(block)
        block_size, block_count = struct.unpack_from("<IQ", block, 36)
        if otype != T_NX_SUPERBLOCK or block_size < 4096 or block_size > 65536 or block_size & (block_size - 1):
            return None
        if len(block) < block_size or (verify and not checksum_ok(block[:block_size])):
            return None
        desc_blocks, _data_blocks, desc_base = struct.unpack_from("<IIQ", block, 104)
        omap_oid = struct.unpack_from("<Q", block, 160)[0]
        max_fs = min(struct.unpack_from("<I", block, 180)[0], 100)
        fs_oids = [oid for oid in struct.unpack_from("<100Q", block, 184)[:max(max_fs, 1)] if oid]
        keylocker = struct.unpack_from("<QQ", block, 1296)
        if block_count == 0 or not omap_oid or omap_oid >= block_count:
            return None
        return cls(start, block_size, block_count, xid, omap_oid, fs_oids, desc_base, desc_blocks & 0x7FFFFFFF,
                   bytes(block[72:88]), (keylocker[0], keylocker[1]))


def find_superblock(reader: RescueReader, offset: int, head: bytes, search: bool = True) -> NxSuperblock | None:
    """The container superblock in block 0 or, when block 0 is damaged, the newest valid one in the
    checkpoint area right after it (``search``: also when block 0 doesn't look like APFS at all)."""
    stale = None
    if head[32:36] == b"NXSB":
        block_size = struct.unpack_from("<I", head, 36)[0]
        if 4096 <= block_size <= 65536 and len(head) < block_size and offset + block_size <= reader.size:
            head = bytes(reader.read_critical(offset, block_size).data)
        found = NxSuperblock.parse(head, offset)
        if found is not None:
            return found
        stale = NxSuperblock.parse(head, offset, verify=False)
    elif not search:
        return None
    best = None
    if stale is not None and 0 < stale.desc_base < 1 << 20 and 0 < stale.desc_blocks <= 4096:
        best = _newest_in_area(reader, offset, stale.block_size, stale.desc_base, stale.desc_blocks)
    if best is None:
        # The checkpoint area usually starts at block 1: look through the first 2 MiB.
        length = min(2 << 20, reader.size - offset)
        area = bytes(reader.read_critical(offset, length).data) if length > 0 else b""
        for pos in range(4096, len(area) - 4095, 4096):
            if area[pos + 32:pos + 36] != b"NXSB":
                continue
            size = struct.unpack_from("<I", area, pos + 36)[0]
            candidate = NxSuperblock.parse(area[pos:pos + size], offset) if size <= len(area) - pos else None
            if candidate is not None and (best is None or candidate.xid > best.xid):
                best = candidate
        if best is not None and stale is None and not container_starts_at(reader, offset, best):
            return None
    if best is not None:
        best.used_backup = True
    return best


def container_starts_at(reader: RescueReader, start: int, sb: NxSuperblock) -> bool:
    """Whether a container described by ``sb`` really starts at ``start``: its object map, a physical
    object, must be at the block number the superblock gives."""
    at = start + sb.omap_oid * sb.block_size
    if at + sb.block_size > reader.size:
        return False
    block = bytes(reader.read(at, sb.block_size).data)
    oid, _xid, otype, _sub = object_header(block)
    return otype == T_OMAP and oid == sb.omap_oid and checksum_ok(block)


def _newest_in_area(reader: RescueReader, offset: int, block_size: int, base: int, count: int
                    ) -> NxSuperblock | None:
    start = offset + base * block_size
    length = count * block_size
    if start + length > reader.size:
        return None
    area = bytes(reader.read_critical(start, length).data)
    best: NxSuperblock | None = None
    for i in range(count):
        found = NxSuperblock.parse(area[i * block_size:(i + 1) * block_size], offset)
        if found is not None and (best is None or found.xid > best.xid):
            best = found
    return best


# ---------------------------------------------------------------------------- B-trees
def node_entries(block: bytes, fixed: tuple[int, int] | None = None
                 ) -> tuple[int, int, list[tuple[bytes, bytes]], tuple[int, int] | None] | None:
    """(flags, level, [(key, value)], fixed key/value sizes from the root's tree info) of a B-tree
    node, or None if it is malformed."""
    size = len(block)
    flags, level, count = struct.unpack_from("<HHI", block, 32)
    toc_off, toc_len = struct.unpack_from("<HH", block, 40)
    base = 56
    key_start = base + toc_off + toc_len
    val_end = size - (40 if flags & BT_ROOT else 0)
    if key_start > val_end or count > size // 4:
        return None
    if flags & BT_ROOT and flags & BT_FIXED:
        k_size, v_size = struct.unpack_from("<II", block, size - 40 + 8)
        fixed = (k_size, v_size)
    out: list[tuple[bytes, bytes]] = []
    if flags & BT_FIXED:
        if fixed is None or toc_len < 4 * count:
            return None
        k_size, v_size = fixed
        if level > 0:
            v_size = 8
        for i in range(count):
            k_off, v_off = struct.unpack_from("<HH", block, base + toc_off + 4 * i)
            k = key_start + k_off
            if k + k_size > val_end:
                return None
            if v_off == 0xFFFF:
                value = b""
            else:
                v = val_end - v_off
                if v < key_start or v + v_size > val_end:
                    return None
                value = block[v:v + v_size]
            out.append((block[k:k + k_size], value))
    else:
        if toc_len < 8 * count:
            return None
        for i in range(count):
            k_off, k_len, v_off, v_len = struct.unpack_from("<HHHH", block, base + toc_off + 8 * i)
            k = key_start + k_off
            v = val_end - v_off
            if k + k_len > val_end or (v_off != 0xFFFF and (v < key_start or v + v_len > val_end)):
                return None
            out.append((block[k:k + k_len], b"" if v_off == 0xFFFF else block[v:v + v_len]))
    return flags, level, out, fixed


# --------------------------------------------------------------------------- records
class Inode:
    __slots__ = ("oid", "parent", "private_id", "ctime", "mtime", "atime", "bsd_flags", "mode", "size", "nlink",
                 "xattrs", "deleted")

    def __init__(self, oid: int, parent: int, private_id: int, ctime: float | None, mtime: float | None,
                 atime: float | None, bsd_flags: int, mode: int, size: int, nlink: int) -> None:
        self.oid = oid
        self.parent = parent
        self.private_id = private_id
        self.ctime = ctime
        self.mtime = mtime
        self.atime = atime
        self.bsd_flags = bsd_flags
        self.mode = mode
        self.size = size
        self.nlink = nlink
        self.xattrs: dict[str, tuple[int, bytes]] | None = None
        self.deleted = False

    @property
    def is_dir(self) -> bool:
        return self.mode & S_IFMT == S_IFDIR

    @property
    def is_symlink(self) -> bool:
        return self.mode & S_IFMT == S_IFLNK


def parse_inode(oid: int, value: bytes) -> Inode | None:
    if len(value) < 92:
        return None
    parent, private_id, create, modify, _change, access, _internal = struct.unpack_from("<7Q", value, 0)
    nlink, _prot, _gen, bsd_flags, _owner, _group, mode = struct.unpack_from("<iIIIIIH", value, 56)
    size = 0
    count, _used = struct.unpack_from("<HH", value, 92) if len(value) >= 96 else (0, 0)
    pos = 96 + 4 * count
    for i in range(count):
        x_type, _x_flags, x_size = struct.unpack_from("<BBH", value, 96 + 4 * i)
        if pos + x_size > len(value):
            break
        if x_type == INO_EXT_DSTREAM and x_size >= 8:
            size = struct.unpack_from("<Q", value, pos)[0]
        pos += (x_size + 7) & ~7
    return Inode(oid, parent, private_id, apfs_time(create), apfs_time(modify), apfs_time(access), bsd_flags, mode,
                 size, nlink)


def parse_drec(key: bytes, value: bytes) -> tuple[str, int, int] | None:
    """A folder entry: (name, file id, entry type)."""
    if len(value) < 18 or len(key) < 10:
        return None
    hashed_len = struct.unpack_from("<I", key, 8)[0] & 0x3FF
    if 12 + hashed_len == len(key):
        raw = key[12:12 + hashed_len]
    else:
        plain_len = struct.unpack_from("<H", key, 8)[0]
        if 10 + plain_len != len(key):
            return None
        raw = key[10:10 + plain_len]
    name = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
    file_id, _added, flags = struct.unpack_from("<QQH", value, 0)
    return name, file_id & OBJ_ID_MASK, flags & 0xF


# --------------------------------------------------------------------------- volumes
@dataclass
class ApfsSnapshot:
    xid: int
    name: str
    created: float | None
    sblock: int


@dataclass
class ApfsVolumeInfo:
    """One volume of the container, as its superblock describes it."""

    index: int
    name: str
    role: int
    omap: int
    root_tree: int
    fext_tree: int
    snap_tree: int
    fs_flags: int
    incompatible: int
    uuid: bytes
    files: int
    xid: int
    physical_tree: bool = False                          # the file tree's links are block numbers
    inodes: dict[int, Inode] = field(default_factory=dict)
    extents: dict[int, list[Piece]] = field(default_factory=dict)
    tree_nodes: set[int] = field(default_factory=set)
    snapshots: list[ApfsSnapshot] = field(default_factory=list)
    vek: bytes | None = None                             # the volume's key, once unlocked
    hint: str = ""                                       # the password hint saved with it

    @property
    def encrypted(self) -> bool:
        return not self.fs_flags & FS_UNENCRYPTED

    @property
    def locked(self) -> bool:
        return self.encrypted and self.vek is None

    @property
    def sealed(self) -> bool:
        return bool(self.incompatible & INCOMPAT_SEALED)

    @property
    def role_name(self) -> str:
        return ROLES.get(self.role, "")

    @classmethod
    def parse(cls, block: bytes, xid: int) -> ApfsVolumeInfo | None:
        if len(block) < 1056 or block[32:36] != b"APSB":
            return None
        index = struct.unpack_from("<I", block, 36)[0]
        incompatible = struct.unpack_from("<Q", block, 56)[0]
        omap, root_tree, _extentref, snap_tree = struct.unpack_from("<4Q", block, 128)
        files = struct.unpack_from("<Q", block, 184)[0]
        fs_flags = struct.unpack_from("<Q", block, 264)[0]
        name = block[704:960].split(b"\0", 1)[0].decode("utf-8", "replace")
        role = struct.unpack_from("<H", block, 964)[0]
        fext_tree = struct.unpack_from("<Q", block, 1032)[0] if len(block) >= 1040 else 0
        tree_type = struct.unpack_from("<I", block, 116)[0]
        return cls(index, name, role, omap, root_tree, fext_tree, snap_tree, fs_flags, incompatible,
                   bytes(block[240:256]), files, xid, bool(tree_type & OBJ_PHYSICAL))


@dataclass
class LockedVolume:
    """The ``ref`` of the folder standing for an encrypted volume that is still locked."""

    name: str
    hint: str
    uuid: bytes


class ApfsRef:
    """What a file node points at: an inode of one volume (and which of its streams)."""

    __slots__ = ("volume", "inode", "stream")

    def __init__(self, volume: ApfsVolumeInfo, inode: Inode, stream: str = "") -> None:
        self.volume = volume
        self.inode = inode
        self.stream = stream


class ApfsVolume(Volume):
    """An APFS container (Lifeboat shows each of its volumes)."""

    kind = "APFS"

    def __init__(
        self,
        reader: RescueReader,
        superblock: NxSuperblock,
        events: EventBus | None = None,
        control: JobControl | None = None,
        metadata_retry_seconds: float = 180.0,
        passwords: list[str] | None = None,
    ) -> None:
        super().__init__(reader, superblock.start, superblock.volume_size)
        self.sb = superblock
        self.passwords = list(passwords or [])
        self.unlocked: list[str] = []
        self._keys: ApfsKeys | None = None
        self._locked_messages: dict[bytes, str] = {}
        self.block_size = superblock.block_size
        self.cluster_size = superblock.block_size
        self.events = events
        self.control = control
        self.metadata_retry_seconds = metadata_retry_seconds
        self.serial = superblock.uuid.hex().upper()
        self.volumes: list[ApfsVolumeInfo] = []
        self.unreadable_nodes = 0
        self.damaged_nodes = 0
        self.deleted_found = 0
        self._omaps: dict[int, dict[int, list[tuple[int, int, int]]]] = {}
        self._holders: dict[int, Node] = {}
        self._used_blocks: list[tuple[int, int]] | None = None
        self._used_starts: list[int] = []
        self._deadline = 0.0
        if superblock.used_backup:
            self._warn("The APFS container's main superblock is damaged; using the newest checkpoint instead.",
                       E_BOOT_BACKUP)

    # ----------------------------------------------------------------- basics
    def _warn(self, message: str, code: str = E_META_CORRUPT) -> None:
        self.warnings.append(message)
        if self.events is not None:
            self.events.warning(message, code=code, source="apfs")

    def _check(self) -> None:
        if self.control is not None:
            self.control.check()

    def block_offset(self, block: int) -> int:
        return self.offset + block * self.block_size

    def _read_block(self, paddr: int, xid_max: int | None = None, types: tuple[int, ...] = ()) -> bytes | None:
        """One object, read as hard as needed, checksum and type verified."""
        if paddr <= 0 or paddr >= self.sb.block_count:
            return None
        bs = self.block_size
        outcome = self.reader.read(self.block_offset(paddr), bs, ReadMode.FAST)
        if not outcome.complete:
            if time.monotonic() > self._deadline:
                self.unreadable_nodes += 1
                return None
            outcome = self.reader.read_critical(self.block_offset(paddr), bs)
            if not outcome.complete:
                self.unreadable_nodes += 1
                return None
        block = bytes(outcome.data)
        if not checksum_ok(block):
            self.damaged_nodes += 1
            return None
        _oid, xid, otype, _sub = object_header(block)
        if (types and otype not in types) or (xid_max is not None and xid > xid_max):
            self.damaged_nodes += 1
            return None
        return block

    def _read_run(self, paddrs: list[int]) -> dict[int, bytes]:
        """Read many blocks, sorted and merged into large reads (front to back)."""
        out: dict[int, bytes] = {}
        bs = self.block_size
        ordered = sorted(set(p for p in paddrs if 0 < p < self.sb.block_count))
        i = 0
        while i < len(ordered):
            self._check()
            start = ordered[i]
            j = i
            while j + 1 < len(ordered) and ordered[j + 1] - start < READ_RUN // bs and ordered[j + 1] - ordered[j] <= 8:
                j += 1
            end = ordered[j] + 1
            outcome = self.reader.read(self.block_offset(start), (end - start) * bs, ReadMode.FAST)
            data = bytes(outcome.data)
            bad = outcome.unread + outcome.bad
            for p in ordered[i:j + 1]:
                lo = self.block_offset(p)
                if any(s < lo + bs and e > lo for s, e in bad):
                    block = self._read_block(p)
                    if block is not None:
                        out[p] = block
                    continue
                out[p] = data[(p - start) * bs:(p - start + 1) * bs]
            i = j + 1
        return out

    # ------------------------------------------------------------- object maps
    def _omap(self, omap_paddr: int) -> dict[int, list[tuple[int, int, int]]]:
        """An object map: oid -> [(xid, flags, physical block)] sorted by xid."""
        cached = self._omaps.get(omap_paddr)
        if cached is not None:
            return cached
        result: dict[int, list[tuple[int, int, int]]] = {}
        self._omaps[omap_paddr] = result
        head = self._read_block(omap_paddr, types=(T_OMAP,))
        if head is None:
            self._warn("An APFS object map is unreadable; part of the container can't be shown.", E_META_UNREADABLE)
            return result
        tree = struct.unpack_from("<Q", head, 48)[0]
        stack: list[tuple[int, tuple[int, int] | None]] = [(tree, None)]
        seen: set[int] = set()
        while stack:
            paddr, fixed = stack.pop()
            if paddr in seen:
                continue
            seen.add(paddr)
            block = self._read_block(paddr, types=(T_BTREE, T_BTREE_NODE))
            parsed = node_entries(block, fixed) if block is not None else None
            if parsed is None:
                continue
            _flags, level, entries, fixed = parsed
            for key, value in entries:
                if len(key) < 16 or len(value) < 8:
                    continue
                oid, xid = struct.unpack_from("<QQ", key, 0)
                if level > 0:
                    stack.append((struct.unpack_from("<Q", value, 0)[0], fixed))
                elif len(value) >= 16:
                    vflags, _size, target = struct.unpack_from("<IIQ", value, 0)
                    result.setdefault(oid, []).append((xid, vflags, target))
        for items in result.values():
            items.sort()
        return result

    @staticmethod
    def _resolve(omap: dict[int, list[tuple[int, int, int]]], oid: int, xid: int) -> tuple[int, int] | None:
        """(physical block, flags) of the newest version of ``oid`` not newer than ``xid``."""
        items = omap.get(oid)
        if not items:
            return None
        i = bisect.bisect_right(items, (xid, 1 << 64, 0)) - 1
        if i < 0:
            return None
        _x, vflags, paddr = items[i]
        if vflags & OMAP_VAL_DELETED:
            return None
        return paddr, vflags

    # ------------------------------------------------------------------ loading
    def load(self, progress: Callable[[Progress], None] | None = None) -> Node:
        self._deadline = time.monotonic() + self.metadata_retry_seconds
        sb = self.sb
        latest = _newest_in_area(self.reader, self.offset, self.block_size, sb.desc_base, sb.desc_blocks)
        if latest is not None and latest.xid > sb.xid:
            self.sb = sb = latest
        container_omap = self._omap(sb.omap_oid)
        root = Node("", F.DIR | F.VOLUME | F.VIRTUAL, volume=self)
        self._holders = {}
        for oid in sb.fs_oids:
            found = self._resolve(container_omap, oid, sb.xid)
            block = self._read_block(found[0], types=(T_FS,)) if found is not None else None
            info = ApfsVolumeInfo.parse(block, sb.xid) if block is not None else None
            if info is None:
                self._warn(f"An APFS volume (object {oid}) is unreadable.", E_META_UNREADABLE)
                continue
            self.volumes.append(info)
        user_volumes = [v for v in self.volumes if v.role not in SYSTEM_ROLES]
        self.label = ", ".join(v.name for v in (user_volumes or self.volumes))
        single = len(self.volumes) == 1
        for info in self.volumes:
            self._check()
            if single:
                holder = root
                self._holders[id(info)] = holder
            else:
                title = info.name or f"Volume {info.index + 1}"
                if info.role_name and info.role_name not in title:
                    title += f" ({info.role_name})"
                holder = Node(title, F.DIR | F.VIRTUAL, volume=self)
                if info.role in SYSTEM_ROLES:
                    holder.flags |= F.SYSTEM
                root.add(holder)
                self._holders[id(info)] = holder
            if info.encrypted and not self._unlock(info):
                holder.flags |= F.UNSUPPORTED
                holder.ref = LockedVolume(info.name, info.hint, info.uuid)
                hint = f" (password hint: \"{info.hint}\")" if info.hint else ""
                message = (f"APFS volume '{info.name}' is encrypted{hint}. Unlock it with the password used on the "
                           "Mac: select it and click Unlock (on the command line: --password).")
                self._locked_messages[info.uuid] = message
                self._warn(message, E_FS_UNSUPPORTED)
                continue
            if progress is not None:
                progress(Progress(f"Reading APFS volume '{info.name}'"))
            self._load_volume(info, holder, progress)
        self._add_deleted(progress)
        for info in self.volumes:
            if info.snapshots and not info.locked:
                self._add_snapshots(info, self._holders[id(info)], progress)
        if self.unreadable_nodes:
            lost = format_size(self.unreadable_nodes * self.block_size)
            self._warn(f"{self.unreadable_nodes:,} APFS metadata blocks ({lost}) are unreadable; files they described "
                       "may be missing.", E_META_UNREADABLE)
        if self.damaged_nodes:
            self._warn(f"{self.damaged_nodes:,} APFS metadata blocks failed their checksum and were skipped.",
                       E_META_CORRUPT)
        for node in root.walk():
            if node.flags & F.SYSTEM and node.children:
                for item in node.walk():
                    item.flags |= F.SYSTEM
        self.root = root
        return root

    def unlock(self, uuid: bytes, password: str, progress: Callable[[Progress], None] | None = None,
               control: JobControl | None = None) -> Node | None:
        """Unlock a volume that stayed locked during the scan and read its files into the tree, in
        place: the rest of the scan (other volumes, what Deep Scan found, ticks) is untouched.
        Returns the volume's folder, or None if ``password`` doesn't open it."""
        info = next((v for v in self.volumes if v.uuid == uuid and v.locked), None)
        if info is None:
            return None
        if control is not None:
            self.control = control
        self._deadline = time.monotonic() + self.metadata_retry_seconds
        self.passwords = [password]
        if not self._unlock(info):
            return None
        holder = self._holders[id(info)]
        locked_ref, locked_flags = holder.ref, holder.flags
        holder.flags &= ~F.UNSUPPORTED
        holder.ref = None
        unreadable, damaged = self.unreadable_nodes, self.damaged_nodes
        try:
            if progress is not None:
                progress(Progress(f"Reading APFS volume '{info.name}'"))
            self._load_volume(info, holder, progress)
            self._used_blocks = None             # its files' blocks are in use: the overwritten check must know
            self._add_deleted(progress, only={info.uuid})
            if info.snapshots:
                self._add_snapshots(info, holder, progress)
        except BaseException:
            # Stopped (or the drive went away) half way: back to locked, as if nothing happened.
            holder.children = []
            holder.flags, holder.ref = locked_flags, locked_ref
            info.vek, info.inodes, info.extents, info.tree_nodes, info.snapshots = None, {}, {}, set(), []
            self._used_blocks = None
            raise
        message = self._locked_messages.pop(info.uuid, None)
        if message in self.warnings:
            self.warnings.remove(message)
        if holder.flags & F.SYSTEM:
            for item in holder.walk():
                item.flags |= F.SYSTEM
        if self.unreadable_nodes > unreadable:
            count = self.unreadable_nodes - unreadable
            self._warn(f"{count:,} metadata blocks of '{info.name}' ({format_size(count * self.block_size)}) are "
                       "unreadable; files they described may be missing.", E_META_UNREADABLE)
        if self.damaged_nodes > damaged:
            self._warn(f"{self.damaged_nodes - damaged:,} metadata blocks of '{info.name}' failed their checksum "
                       "and were skipped.", E_META_CORRUPT)
        return holder

    def _unlock(self, info: ApfsVolumeInfo) -> bool:
        """Try the passwords given for this scan on an encrypted volume; keep its key if one fits."""
        from .apfs_crypto import CryptoUnavailable

        try:
            if self._keys is None:
                self._keys = ApfsKeys(self.reader, self.offset, self.block_size, self.sb.uuid, self.sb.keylocker)
            keys = self._keys
            info.hint = keys.hint(info.uuid)
            for password in self.passwords:
                self._check()
                info.vek = keys.unlock(info.uuid, password)
                if info.vek is not None:
                    self.unlocked.append(info.name)
                    if self.events is not None:
                        self.events.success(f"APFS volume '{info.name}' unlocked.", source="apfs")
                    return True
        except CryptoUnavailable as exc:
            self._warn(str(exc), E_FS_UNSUPPORTED)
        return False

    def _tree_leaves(self, omap: dict[int, list[tuple[int, int, int]]], root_oid: int, xid: int,
                     skip: set[int] | None = None, visited: set[int] | None = None,
                     progress: Callable[[Progress], None] | None = None,
                     cache: dict[int, list[tuple[bytes, bytes]]] | None = None,
                     physical: bool = False, key: bytes | None = None) -> list[list[tuple[bytes, bytes]]]:
        """All leaf records of a file-system tree, reading level by level. Its links are object ids
        found through ``omap`` (or, for ``physical`` trees, block numbers). Nodes whose block is in
        ``skip`` are not read (shared with a tree already loaded); parsed leaves are kept in
        ``cache`` (snapshots share most of their nodes). Encrypted nodes are decrypted with ``key``
        (an unlocked volume's key) or left out without it."""
        cache = cache if cache is not None else {}
        sectors = self.block_size // 512

        def locate(oid: int) -> tuple[int, int] | None:
            return (oid, 0) if physical else self._resolve(omap, oid, xid)

        found = locate(root_oid)
        if found is None or (found[1] & OMAP_VAL_ENCRYPTED and key is None):
            return []
        encrypted: set[int] = {found[0]} if found[1] & OMAP_VAL_ENCRYPTED else set()
        level_blocks = [found[0]]
        leaves: list[list[tuple[bytes, bytes]]] = []
        depth = 0
        while level_blocks and depth < 16:
            depth += 1
            wanted = [p for p in level_blocks if not (skip and p in skip)]
            if visited is not None:
                visited.update(wanted)
            cached = {p: cache[p] for p in wanted if p in cache}
            blocks = self._read_run([p for p in wanted if p not in cached])
            next_level: list[int] = []
            for p in wanted:
                if p in cached:
                    leaves.append(cached[p])
                    continue
                block = blocks.get(p)
                if block is None:
                    continue
                if p in encrypted and key is not None:
                    block = xts_decrypt(key, block, p * sectors)
                _oid, bxid, otype, _sub = object_header(block)
                if otype not in (T_BTREE, T_BTREE_NODE) or bxid > xid or not checksum_ok(block):
                    self.damaged_nodes += 1
                    continue
                parsed = node_entries(block)
                if parsed is None:
                    self.damaged_nodes += 1
                    continue
                _flags, level, entries, _fixed = parsed
                if level == 0:
                    if len(cache) < 50_000:
                        cache[p] = entries
                    leaves.append(entries)
                    continue
                for _key, value in entries:
                    if len(value) < 8:
                        continue
                    child = locate(struct.unpack_from("<Q", value, 0)[0])
                    if child is None or (child[1] & OMAP_VAL_ENCRYPTED and key is None):
                        continue
                    if child[1] & OMAP_VAL_ENCRYPTED:
                        encrypted.add(child[0])
                    next_level.append(child[0])
            if progress is not None:
                progress(Progress("Reading the APFS file tree", len(leaves), 0, item=f"{len(leaves):,} tree nodes"))
            level_blocks = next_level
        return leaves

    def _physical_leaves(self, root: int) -> list[list[tuple[bytes, bytes]]]:
        """All leaf records of a physical B-tree (snapshot list, sealed volumes' extents)."""
        leaves = []
        stack: list[tuple[int, tuple[int, int] | None]] = [(root, None)]
        seen: set[int] = set()
        while stack:
            paddr, fixed = stack.pop()
            if paddr in seen or len(seen) > 1_000_000:
                continue
            seen.add(paddr)
            block = self._read_block(paddr, types=(T_BTREE, T_BTREE_NODE))
            parsed = node_entries(block, fixed) if block is not None else None
            if parsed is None:
                continue
            _flags, level, entries, fixed = parsed
            if level == 0:
                leaves.append(entries)
            else:
                stack.extend((struct.unpack_from("<Q", v, 0)[0], fixed) for _k, v in entries if len(v) >= 8)
        return leaves

    def _records(self, leaves: list[list[tuple[bytes, bytes]]]
                 ) -> tuple[dict[int, Inode], list[tuple[int, str, int, int]], dict[int, list[Piece]]]:
        """Inodes, folder entries (parent, name, file id, type) and file extents (logical offset,
        length, block, crypto id) of a tree."""
        inodes: dict[int, Inode] = {}
        drecs: list[tuple[int, str, int, int]] = []
        extents: dict[int, list[Piece]] = {}
        xattrs: list[tuple[int, str, int, bytes]] = []
        for entries in leaves:
            for key, value in entries:
                if len(key) < 8:
                    continue
                head = struct.unpack_from("<Q", key, 0)[0]
                kind = head >> 60
                oid = head & OBJ_ID_MASK
                if kind == R_INODE:
                    inode = parse_inode(oid, value)
                    if inode is not None:
                        inodes[oid] = inode
                elif kind == R_DIR_REC:
                    drec = parse_drec(key, value)
                    if drec is not None:
                        drecs.append((oid, drec[0], drec[1], drec[2]))
                elif kind == R_FILE_EXTENT and len(key) >= 16 and len(value) >= 24:
                    logical = struct.unpack_from("<Q", key, 8)[0]
                    length_flags, phys, crypto = struct.unpack_from("<QQQ", value, 0)
                    extents.setdefault(oid, []).append((logical, length_flags & ((1 << 56) - 1), phys, crypto))
                elif kind == R_XATTR and len(key) >= 10 and len(value) >= 4:
                    name_len = struct.unpack_from("<H", key, 8)[0]
                    name = key[10:10 + name_len].split(b"\0", 1)[0].decode("utf-8", "replace")
                    if name in WANTED_XATTRS:
                        flags, length = struct.unpack_from("<HH", value, 0)
                        xattrs.append((oid, name, flags, bytes(value[4:4 + length])))
        for oid, name, flags, data in xattrs:
            inode = inodes.get(oid)
            if inode is not None:
                if inode.xattrs is None:
                    inode.xattrs = {}
                inode.xattrs[name] = (flags, data)
        for items in extents.values():
            items.sort()
        return inodes, drecs, extents

    def _load_volume(self, info: ApfsVolumeInfo, holder: Node, progress: Callable[[Progress], None] | None) -> None:
        omap = self._omap(info.omap)
        leaves = self._tree_leaves(omap, info.root_tree, info.xid, visited=info.tree_nodes, progress=progress,
                                   physical=info.physical_tree, key=info.vek)
        inodes, drecs, extents = self._records(leaves)
        if info.sealed and info.fext_tree:
            for entries in self._physical_leaves(info.fext_tree):
                for key, value in entries:
                    if len(key) >= 16 and len(value) >= 16:
                        private_id, logical = struct.unpack_from("<QQ", key, 0)
                        length_flags, phys = struct.unpack_from("<QQ", value, 0)
                        extents.setdefault(private_id, []).append((logical, length_flags & ((1 << 56) - 1), phys, 0))
            for items in extents.values():
                items.sort()
        info.inodes = inodes
        info.extents = extents
        if not inodes:
            self._warn(f"APFS volume '{info.name}': its file tree is unreadable.", E_META_UNREADABLE)
        if holder is not None and holder.flags & F.VOLUME:
            holder.ctime = inodes[ROOT_DIR].ctime if ROOT_DIR in inodes else None
        self._build(info, holder, inodes, drecs, deleted=False)
        if info.snap_tree:
            self._find_snapshots(info)

    # -------------------------------------------------------------------- tree
    def _build(self, info: ApfsVolumeInfo, holder: Node, inodes: dict[int, Inode],
               drecs: list[tuple[int, str, int, int]], deleted: bool,
               existing: dict[int, Node] | None = None) -> dict[int, Node]:
        """Turn folder entries into nodes under ``holder`` (the volume's root folder)."""
        folders: dict[int, Node] = dict(existing or {})
        folders[ROOT_DIR] = folders.get(ROOT_DIR, holder)
        children: list[tuple[int, str, int, int]] = []
        for parent, name, file_id, dtype in drecs:
            inode = inodes.get(file_id)
            is_dir = dtype == DT_DIR or (inode is not None and inode.is_dir)
            if is_dir:
                if file_id in folders:
                    continue
                node = Node(name, F.DIR, volume=self, ref=ApfsRef(info, inode) if inode else None,
                            ctime=inode.ctime if inode else None, mtime=inode.mtime if inode else None,
                            atime=inode.atime if inode else None)
                self._flag(node, name, inode, deleted)
                folders[file_id] = node
                children.append((parent, name, file_id, -1))
            else:
                children.append((parent, name, file_id, dtype))
        lost = None
        groups: dict[int, Node] = {}
        created: dict[int, Node] = {}
        for parent, name, file_id, dtype in children:
            if dtype == -1:
                node = folders[file_id]
            else:
                inode = inodes.get(file_id)
                if inode is None:
                    continue
                node = self._file_node(info, name, inode, deleted)
            target = folders.get(parent)
            if target is None or target is node:
                if lost is None:
                    lost = _child(holder, "Lost files") or holder.add(Node("Lost files", F.DIR | F.VIRTUAL,
                                                                           volume=self))
                group = groups.get(parent)
                if group is None:
                    group = Node(f"Folder #{parent}", F.DIR | F.VIRTUAL | F.ORPHAN, volume=self)
                    groups[parent] = group
                    lost.add(group)
                node.flags |= F.ORPHAN
                group.add(node)
            else:
                target.add(node)
            if dtype != -1:
                created[file_id] = node
                stream = self._fork_node(info, name, inodes[file_id], deleted)
                if stream is not None:
                    (target if target is not None and target is not node else node.parent or holder).add(stream)
        _break_cycles(holder, folders)
        return folders

    def _flag(self, node: Node, name: str, inode: Inode | None, deleted: bool) -> None:
        if deleted:
            node.flags |= F.DELETED
        if name.startswith("."):
            node.flags |= F.HIDDEN
        if name in SYSTEM_NAMES:
            node.flags |= F.SYSTEM | F.HIDDEN
        if inode is not None and inode.bsd_flags & UF_HIDDEN:
            node.flags |= F.HIDDEN

    def _file_node(self, info: ApfsVolumeInfo, name: str, inode: Inode, deleted: bool) -> Node:
        size = inode.size
        flags = 0
        xattrs = inode.xattrs or {}
        if inode.bsd_flags & UF_COMPRESSED:
            flags |= F.COMPRESSED
            raw = self._xattr_bytes(info, inode, "com.apple.decmpfs", limit=64 << 20)
            header = parse_decmpfs(raw) if raw else None
            if header is not None:
                size = header.size
                if header.unsupported():
                    flags |= F.UNSUPPORTED
        if inode.is_symlink:
            flags |= F.REPARSE | F.SYSTEM
            target = xattrs.get("com.apple.fs.symlink")
            size = len(target[1].rstrip(b"\0")) if target else 0
        if inode.nlink > 1:
            flags |= F.HARDLINK
        if any(piece[2] == 0 for piece in info.extents.get(inode.private_id, ())):
            flags |= F.SPARSE
        node = Node(name, flags, size, volume=self, ref=ApfsRef(info, inode), ctime=inode.ctime, mtime=inode.mtime,
                    atime=inode.atime)
        self._flag(node, name, inode, deleted)
        return node

    def _fork_node(self, info: ApfsVolumeInfo, name: str, inode: Inode, deleted: bool) -> Node | None:
        if inode.bsd_flags & UF_COMPRESSED or not inode.xattrs or "com.apple.ResourceFork" not in inode.xattrs:
            return None
        flags, data = inode.xattrs["com.apple.ResourceFork"]
        size = struct.unpack_from("<Q", data, 8)[0] if flags & XATTR_STREAM and len(data) >= 16 else len(data)
        node = Node(f"{name}:rsrc", F.STREAM | F.HIDDEN, size, volume=self, ref=ApfsRef(info, inode, "rsrc"),
                    ctime=inode.ctime, mtime=inode.mtime)
        if deleted:
            node.flags |= F.DELETED
        return node

    # --------------------------------------------------------------- deleted
    def _add_deleted(self, progress: Callable[[Progress], None] | None, only: set[bytes] | None = None) -> None:
        """Files in older checkpoints that the newest one no longer has (in the volumes whose UUID is
        in ``only``, or all)."""
        sb = self.sb
        older = []
        area_start = self.offset + sb.desc_base * self.block_size
        if sb.desc_blocks and area_start + sb.desc_blocks * self.block_size <= self.reader.size:
            area = bytes(self.reader.read(area_start, sb.desc_blocks * self.block_size, ReadMode.FAST).data)
            for i in range(sb.desc_blocks):
                found = NxSuperblock.parse(area[i * self.block_size:(i + 1) * self.block_size], self.offset)
                if found is not None and found.xid < sb.xid:
                    older.append(found)
        older.sort(key=lambda s: s.xid, reverse=True)
        if not older:
            return
        if progress is not None:
            progress(Progress("Looking for deleted files in older checkpoints"))
        by_uuid = {v.uuid: v for v in self.volumes if not v.locked and (only is None or v.uuid in only)}
        found_inodes: dict[bytes, dict[int, Inode]] = {}
        found_drecs: dict[bytes, list[tuple[int, str, int, int]]] = {}
        found_extents: dict[bytes, dict[int, list[Piece]]] = {}
        for old in older:
            self._check()
            container_omap = self._omap(old.omap_oid)
            for oid in old.fs_oids:
                where = self._resolve(container_omap, oid, old.xid)
                block = self._read_block(where[0], xid_max=old.xid, types=(T_FS,)) if where else None
                vol = ApfsVolumeInfo.parse(block, old.xid) if block is not None else None
                current = by_uuid.get(vol.uuid) if vol is not None else None
                if vol is None or current is None:
                    continue
                leaves = self._tree_leaves(self._omap(vol.omap), vol.root_tree, old.xid, skip=current.tree_nodes,
                                           physical=vol.physical_tree, key=current.vek)
                inodes, drecs, extents = self._records(leaves)
                into = found_inodes.setdefault(vol.uuid, {})
                for ino, inode in inodes.items():
                    if ino not in current.inodes and ino >= FIRST_USER_ID:
                        into.setdefault(ino, inode)
                found_drecs.setdefault(vol.uuid, []).extend(
                    d for d in drecs if d[2] not in current.inodes and d[2] >= FIRST_USER_ID)
                ext_into = found_extents.setdefault(vol.uuid, {})
                for private_id, items in extents.items():
                    ext_into.setdefault(private_id, items)
        for uuid, inodes in found_inodes.items():
            self._attach_deleted(by_uuid[uuid], inodes, found_drecs.get(uuid, []), found_extents.get(uuid, {}))

    def _attach_deleted(self, info: ApfsVolumeInfo, inodes: dict[int, Inode], drecs: list[tuple[int, str, int, int]],
                        extents: dict[int, list[Piece]]) -> int:
        """Show deleted files (inodes the live tree no longer has) in their folders; return how many."""
        inodes = {ino: inode for ino, inode in inodes.items() if ino not in info.inodes and ino >= FIRST_USER_ID}
        if not inodes:
            return 0
        for private_id, items in extents.items():
            if private_id not in info.extents:
                info.extents[private_id] = items
        for inode in inodes.values():
            inode.deleted = True
            info.inodes[inode.oid] = inode
        seen: set[tuple[int, str]] = set()
        named = []
        for d in drecs:
            if d[2] in inodes and (d[2], d[1]) not in seen:
                seen.add((d[2], d[1]))
                named.append(d)
        holder = self._holders[id(info)]
        self._build(info, holder, inodes, named, deleted=True, existing=self._folder_index(holder))
        self._used_blocks = None
        added = 0
        for node in holder.walk():
            ref = node.ref
            if (node.flags & F.DELETED and not node.flags & F.DIR and isinstance(ref, ApfsRef)
                    and ref.inode.oid in inodes):
                added += 1
                if node.size and self._overwritten(ref):
                    node.flags |= F.OVERWRITTEN
        self.deleted_found += added
        return added

    def _folder_index(self, holder: Node) -> dict[int, Node]:
        out: dict[int, Node] = {ROOT_DIR: holder}
        for node in holder.walk():
            if node.flags & F.DIR and isinstance(node.ref, ApfsRef):
                out.setdefault(node.ref.inode.oid, node)
        return out

    def _overwritten(self, ref: ApfsRef) -> bool:
        """Whether a deleted file's blocks hold a live file or live metadata now."""
        bs = self.block_size
        if self._used_blocks is None:
            live_ids = {i.private_id for info in self.volumes for i in info.inodes.values() if not i.deleted}
            spans = [(p, p + 1) for info in self.volumes for p in info.tree_nodes]
            for info in self.volumes:
                for private_id, items in info.extents.items():
                    if private_id in live_ids:
                        spans.extend((phys, phys + max(1, -(-length // bs))) for _l, length, phys, _c in items if phys)
            spans.sort()
            merged: list[tuple[int, int]] = []
            for start, end in spans:
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                else:
                    merged.append((start, end))
            self._used_blocks = merged
            self._used_starts = [start for start, _end in merged]
        for _logical, length, phys, _crypto in ref.volume.extents.get(ref.inode.private_id, ()):
            if not phys:
                continue
            end = phys + max(1, -(-length // bs))
            i = bisect.bisect_right(self._used_starts, end - 1) - 1
            if i >= 0 and self._used_blocks[i][1] > phys:
                return True
        return False

    # ------------------------------------------------------------- deep scan
    _NODE_TAGS = (struct.pack("<II", T_BTREE_NODE, T_FSTREE), struct.pack("<II", T_BTREE, T_FSTREE))

    def deep_feed(self, offset: int, data: bytes) -> None:
        """Deep Scan hands over every piece of the drive: keep the old file-tree nodes (copies APFS
        left behind when it changed them) found in this container's free space."""
        if not hasattr(self, "_orphans"):
            self._orphans: list[tuple[int, list[tuple[bytes, bytes]]]] = []
            self._live_nodes = {p for info in self.volumes for p in info.tree_nodes}
        bs = self.block_size
        end = self.offset + self.size
        if offset + len(data) <= self.offset or offset >= end:
            return
        for tag in self._NODE_TAGS:
            index = data.find(tag)
            while index >= 0:
                pos = index - 24
                absolute = offset + pos
                if (absolute - self.offset) % bs == 0 and self.offset <= absolute < end:
                    block = data[pos:pos + bs] if pos >= 0 else b""
                    if len(block) < bs:          # the node straddles two pieces of the scan
                        block = bytes(self.reader.read(absolute, bs, ReadMode.FAST).data)
                    self._consider_orphan(absolute, block)
                index = data.find(tag, index + 1)

    def _consider_orphan(self, absolute: int, block: bytes) -> None:
        paddr = (absolute - self.offset) // self.block_size
        if len(block) != self.block_size or paddr in self._live_nodes:
            return
        if struct.unpack_from("<H", block, 34)[0] != 0 or not checksum_ok(block):
            return
        parsed = node_entries(block)
        if parsed is not None and parsed[2]:
            self._orphans.append((object_header(block)[1], parsed[2]))

    def deep_finish(self, progress: Callable[[Progress], None] | None = None) -> int:
        """After Deep Scan: rebuild the deleted files the old nodes describe. Returns how many."""
        orphans = getattr(self, "_orphans", [])
        readable = [v for v in self.volumes if not v.encrypted]   # encrypted nodes can't be recognised
        if not orphans or len(readable) != 1:
            return 0
        if progress is not None:
            progress(Progress("Rebuilding deleted APFS files"))
        info = readable[0]
        inodes: dict[int, Inode] = {}
        drecs: set[tuple[int, str, int, int]] = set()
        pieces: dict[tuple[int, int], tuple[int, int, int]] = {}
        for _xid, entries in sorted(orphans, key=lambda item: item[0]):
            found_inodes, found_drecs, found_extents = self._records([entries])
            for ino, inode in found_inodes.items():
                if ino not in info.inodes:
                    inodes[ino] = inode
            drecs.update(found_drecs)
            for private_id, items in found_extents.items():
                for logical, length, phys, crypto in items:
                    pieces[(private_id, logical)] = (length, phys, crypto)
        extents: dict[int, list[Piece]] = {}
        for (private_id, logical), (length, phys, crypto) in sorted(pieces.items()):
            extents.setdefault(private_id, []).append((logical, length, phys, crypto))
        self._orphans = []
        return self._attach_deleted(info, inodes, sorted(drecs), extents)

    # ------------------------------------------------------------- snapshots
    def _find_snapshots(self, info: ApfsVolumeInfo) -> None:
        for entries in self._physical_leaves(info.snap_tree):
            for key, value in entries:
                if len(key) < 8 or struct.unpack_from("<Q", key, 0)[0] >> 60 != R_SNAP_METADATA or len(value) < 50:
                    continue
                xid = struct.unpack_from("<Q", key, 0)[0] & OBJ_ID_MASK
                _ext, sblock, created, _changed, _inum, _ttype, _flags, name_len = struct.unpack_from(
                    "<QQQQQIIH", value, 0)
                name = value[50:50 + name_len].split(b"\0", 1)[0].decode("utf-8", "replace")
                info.snapshots.append(ApfsSnapshot(xid, name, apfs_time(created), sblock))
        info.snapshots.sort(key=lambda s: s.xid, reverse=True)

    def _add_snapshots(self, info: ApfsVolumeInfo, holder: Node | None, progress: Callable[[Progress], None] | None
                       ) -> None:
        if holder is None:
            return
        folder = Node("Snapshots", F.DIR | F.VIRTUAL, volume=self)
        holder.add(folder)
        budget = MAX_SNAPSHOT_NODES
        omap = self._omap(info.omap)
        cache: dict[int, list[tuple[bytes, bytes]]] = {}
        shown = 0
        for snap in info.snapshots:
            self._check()
            title = f"{snap.name} ({format_timestamp(snap.created)})" if snap.created else snap.name
            node = Node(title.replace("/", ":"), F.DIR | F.VIRTUAL, volume=self, ctime=snap.created,
                        mtime=snap.created)
            folder.add(node)
            if budget <= 0:
                node.flags |= F.DAMAGED_META
                continue
            block = self._read_block(snap.sblock, types=(T_FS,))
            vol = ApfsVolumeInfo.parse(block, snap.xid) if block is not None else None
            if vol is None:
                node.flags |= F.DAMAGED_META
                continue
            if progress is not None:
                progress(Progress(f"Reading snapshot '{snap.name}'"))
            leaves = self._tree_leaves(omap, vol.root_tree, snap.xid, cache=cache, physical=vol.physical_tree,
                                       key=info.vek)
            inodes, drecs, extents = self._records(leaves)
            view = ApfsVolumeInfo(vol.index, vol.name, vol.role, info.omap, vol.root_tree, 0, 0, vol.fs_flags,
                                  vol.incompatible, vol.uuid, vol.files, snap.xid, vol.physical_tree,
                                  inodes=inodes, extents=extents, vek=info.vek)
            self._build(view, node, inodes, drecs, deleted=False)
            budget -= len(inodes)
            shown += 1
        if shown < len(info.snapshots):
            self._warn(f"APFS volume '{info.name}' has more snapshots than Lifeboat can show at once; "
                       f"{len(info.snapshots) - shown} of them are shown empty.")

    # ------------------------------------------------------------------- data
    def _xattr_bytes(self, info: ApfsVolumeInfo, inode: Inode, name: str, limit: int) -> bytes:
        item = (inode.xattrs or {}).get(name)
        if item is None:
            return b""
        flags, data = item
        if flags & XATTR_EMBEDDED or not flags & XATTR_STREAM:
            return data
        layout = self._stream_layout(info, data)
        if layout.size > limit:
            return b""
        from .content import FileContentReader

        chunk = FileContentReader(layout, self.reader).read(0, layout.size, ReadMode.FAST)
        return bytes(chunk.data) if chunk.complete else b""

    def _stream_layout(self, info: ApfsVolumeInfo, xdata: bytes) -> FileLayout:
        """The layout of an extended attribute stored as a stream (j_xattr_dstream_t)."""
        if len(xdata) < 16:
            return FileLayout(0)
        stream_id, size = struct.unpack_from("<QQ", xdata, 0)
        return self._extent_layout(info, stream_id, size)

    def _extent_layout(self, info: ApfsVolumeInfo, private_id: int, size: int) -> FileLayout:
        layout = FileLayout(size, xts_key=info.vek)
        sectors = self.block_size // 512
        for logical, length, phys, crypto in info.extents.get(private_id, ()):
            if logical >= size:
                break
            length = min(length, size - logical)
            if phys == 0:
                layout.extents.append(Extent(logical, length, SPARSE))
            elif phys + -(-length // self.block_size) > self.sb.block_count:
                layout.extents.append(Extent(logical, length, INVALID))
                layout.problems.append("Part of this file points outside the container.")
            else:
                # The crypto id is the block the extent was first written to (its tweak, in blocks).
                tweak = (crypto or phys) * sectors if info.vek is not None else -1
                layout.extents.append(Extent(logical, length, self.block_offset(phys), tweak))
        covered = sum(e.length for e in layout.extents)
        if covered < size and size:
            layout.problems.append("The location of part of this file is unknown.")
        return layout

    def layout(self, node: Node) -> FileLayout:
        ref = node.ref
        if not isinstance(ref, ApfsRef) or node.flags & F.DIR:
            return FileLayout(0)
        info, inode = ref.volume, ref.inode
        if ref.stream == "rsrc":
            flags, data = (inode.xattrs or {}).get("com.apple.ResourceFork", (0, b""))
            if flags & XATTR_STREAM:
                return self._stream_layout(info, data)
            return FileLayout(len(data), resident=data)
        if inode.is_symlink:
            target = (inode.xattrs or {}).get("com.apple.fs.symlink", (0, b""))[1].rstrip(b"\0")
            return FileLayout(len(target), resident=target)
        if inode.bsd_flags & UF_COMPRESSED:
            return self._compressed_layout(info, inode, node.size)
        layout = self._extent_layout(info, inode.private_id, inode.size)
        if inode.deleted and layout.problems:
            layout.problems = ["Only part of this deleted file's location is known."]
        return layout

    def _compressed_layout(self, info: ApfsVolumeInfo, inode: Inode, size: int) -> FileLayout:
        raw = self._xattr_bytes(info, inode, "com.apple.decmpfs", limit=64 << 20)
        header = parse_decmpfs(raw) if raw else None
        if header is None:
            return FileLayout(size, unsupported="Compressed by macOS, but its compression details are missing.")
        if header.unsupported():
            return FileLayout(header.size, unsupported=header.unsupported())
        assert header.method is not None
        if not header.in_resource_fork:
            return FileLayout(header.size, chunked=CompressedChunks(header.method, max(1, header.size),
                                                                    inline=header.inline))
        flags, data = (inode.xattrs or {}).get("com.apple.ResourceFork", (0, b""))
        source = self._stream_layout(info, data) if flags & XATTR_STREAM else FileLayout(len(data), resident=data)
        chunked = CompressedChunks(header.method, 0x10000, source=source)
        layout = FileLayout(header.size, chunked=chunked, problems=list(source.problems))
        from .content import FileContentReader

        try:
            reader = FileContentReader(source, self.reader)
            need = min(table_bytes_needed(header.kind, header.size), source.size)
            head = reader.read(0, need, ReadMode.FAST)
            if header.kind == 4 and head.complete:
                need = min(table_bytes_needed(header.kind, header.size, bytes(head.data)), source.size)
                head = reader.read(0, need, ReadMode.FAST)
            if not head.complete:
                raise CompressionError("the compression table is unreadable")
            chunked.chunks = chunk_table(header.kind, bytes(head.data), header.size, source.size)
        except CompressionError as exc:
            chunked.error = str(exc)
            layout.problems.append(f"Compressed by macOS: {exc}.")
        return layout


def _child(parent: Node, which: object) -> Node | None:
    """A child folder of ``parent`` by name, or the one holding an APFS volume."""
    for child in parent.children or ():
        if isinstance(which, str) and child.name == which and child.flags & F.DIR:
            return child
        if isinstance(which, ApfsVolumeInfo) and child.flags & F.DIR and (
                child.name == which.name or child.name.startswith((which.name or "") + " (")):
            return child
    return None


def _break_cycles(root: Node, folders: dict[int, Node]) -> None:
    rooted: set[int] = {id(root)}
    lost: Node | None = None
    for node in list(folders.values()):
        chain = []
        cursor: Node | None = node
        seen: set[int] = set()
        while cursor is not None and id(cursor) not in rooted:
            if id(cursor) in seen:
                parent = cursor.parent
                if parent is not None and parent.children is not None:
                    parent.children.remove(cursor)
                if lost is None:
                    lost = _child(root, "Lost files") or root.add(Node("Lost files", F.DIR | F.VIRTUAL,
                                                                       volume=root.volume))
                cursor.flags |= F.ORPHAN
                lost.add(cursor)
                break
            seen.add(id(cursor))
            chain.append(cursor)
            cursor = cursor.parent
        rooted.update(id(item) for item in chain)
