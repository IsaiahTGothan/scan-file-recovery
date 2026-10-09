"""The in-memory tree of everything a scan found, and file data layouts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..rescue.reader import RescueReader


class F:
    """Node flags (plain ints: millions of nodes, so no IntFlag overhead)."""

    DIR = 1 << 0
    DELETED = 1 << 1
    ORPHAN = 1 << 2               # original folder unknown ("Lost files")
    SYSTEM = 1 << 3               # filesystem metadata ($MFT, ...)
    HIDDEN = 1 << 4
    COMPRESSED = 1 << 5
    ENCRYPTED = 1 << 6            # Windows EFS: content cannot be decrypted
    SPARSE = 1 << 7
    RESIDENT = 1 << 8             # data stored inside the file record
    REPARSE = 1 << 9
    CARVED = 1 << 10              # found by signature, no filesystem metadata
    OVERWRITTEN = 1 << 11         # deleted and its space is in use by other data
    NAME_GUESSED = 1 << 12        # part of the name was lost (FAT deleted entries)
    VIRTUAL = 1 << 13             # grouping folder created by Lifeboat
    STREAM = 1 << 14              # NTFS alternate data stream
    DAMAGED_META = 1 << 15        # its metadata was partly unreadable
    ASSUMED_CONTIGUOUS = 1 << 16  # data location estimated (deleted FAT/exFAT)
    HARDLINK = 1 << 17
    READONLY = 1 << 18
    UNSUPPORTED = 1 << 19         # storage format Lifeboat cannot decode
    VOLUME = 1 << 20              # root of a partition/volume


class Node:
    __slots__ = (
        "name", "parent", "children", "flags", "size",
        "ctime", "mtime", "atime", "volume", "ref",
        "check", "sel_bytes", "sel_count",
    )

    def __init__(
        self,
        name: str,
        flags: int = 0,
        size: int = 0,
        volume: Volume | None = None,
        ref: Any = None,
        ctime: float | None = None,
        mtime: float | None = None,
        atime: float | None = None,
    ) -> None:
        self.name = name
        self.parent: Node | None = None
        self.children: list[Node] | None = [] if flags & F.DIR else None
        self.flags = flags
        self.size = size
        self.ctime = ctime
        self.mtime = mtime
        self.atime = atime
        self.volume = volume
        self.ref = ref
        self.check = 0          # 0 unchecked, 1 partially, 2 checked
        self.sel_bytes = 0
        self.sel_count = 0

    def __repr__(self) -> str:
        return f"<Node {self.path()!r} flags=0x{self.flags:x} size={self.size}>"

    @property
    def is_dir(self) -> bool:
        return bool(self.flags & F.DIR)

    def add(self, child: Node) -> Node:
        assert self.children is not None, "only folders have children"
        child.parent = self
        self.children.append(child)
        return child

    def child(self, name: str) -> Node | None:
        for item in self.children or ():
            if item.name == name:
                return item
        return None

    def ensure_dir(self, name: str, flags: int = 0) -> Node:
        found = self.child(name)
        if found is not None and found.is_dir:
            return found
        return self.add(Node(name, F.DIR | flags, volume=self.volume))

    def ancestors(self) -> Iterator[Node]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def path_parts(self) -> list[str]:
        parts = [self.name]
        node = self.parent
        while node is not None and node.parent is not None:
            parts.append(node.name)
            node = node.parent
        parts.reverse()
        return parts

    def path(self) -> str:
        return "/".join(self.path_parts())

    def walk(self) -> Iterator[Node]:
        """All nodes of the subtree, depth first, including self."""
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            if node.children:
                stack.extend(reversed(node.children))

    def files(self) -> Iterator[Node]:
        for node in self.walk():
            if not node.flags & F.DIR:
                yield node

    def volume_root(self) -> Node | None:
        node: Node | None = self
        while node is not None:
            if node.flags & F.VOLUME:
                return node
            node = node.parent
        return None


@dataclass(slots=True)
class Extent:
    """``length`` bytes of the file at ``file_offset`` live at ``disk_offset``.

    ``disk_offset`` is an absolute byte offset on the source device, or
    ``SPARSE`` (the bytes are zeros by definition), or ``INVALID`` (the
    metadata points somewhere impossible; the bytes are unrecoverable).
    """

    file_offset: int
    length: int
    disk_offset: int

    @property
    def file_end(self) -> int:
        return self.file_offset + self.length


SPARSE = -1
INVALID = -2


@dataclass
class CompressedRuns:
    """NTFS LZNT1 compressed data: cluster runs in VCN order."""

    cluster_size: int
    unit_clusters: int
    runs: list[tuple[int, int, int]]   # (vcn, absolute disk offset or SPARSE/INVALID, cluster count)


@dataclass
class FileLayout:
    size: int
    extents: list[Extent] = field(default_factory=list)
    valid_size: int | None = None
    resident: bytes | None = None
    compressed: CompressedRuns | None = None
    problems: list[str] = field(default_factory=list)
    unsupported: str | None = None

    def disk_ranges(self) -> list[tuple[int, int]]:
        """Absolute device ranges holding the file's data (sorted, merged)."""
        out: list[tuple[int, int]] = []
        valid = self.size if self.valid_size is None else min(self.valid_size, self.size)
        if self.compressed is not None:
            cs = self.compressed.cluster_size
            for _vcn, disk, count in self.compressed.runs:
                if disk >= 0:
                    out.append((disk, disk + count * cs))
        else:
            for ext in self.extents:
                if ext.disk_offset < 0 or ext.file_offset >= valid:
                    continue
                length = min(ext.length, valid - ext.file_offset)
                out.append((ext.disk_offset, ext.disk_offset + length))
        out.sort()
        merged: list[tuple[int, int]] = []
        for start, end in out:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        return merged

    def first_disk_offset(self) -> int:
        """Where the data starts on disk, used to order reads for fewer seeks."""
        if self.compressed is not None:
            for _vcn, disk, _count in self.compressed.runs:
                if disk >= 0:
                    return disk
            return -1
        for ext in self.extents:
            if ext.disk_offset >= 0:
                return ext.disk_offset
        return -1

    @property
    def fragments(self) -> int:
        if self.compressed is not None:
            return sum(1 for _v, disk, _c in self.compressed.runs if disk >= 0)
        return sum(1 for ext in self.extents if ext.disk_offset >= 0)


class Volume(ABC):
    """A filesystem (or a group of carved files) found on the source."""

    kind = "?"

    def __init__(self, reader: RescueReader, offset: int, size: int, label: str = "") -> None:
        self.reader = reader
        self.offset = offset
        self.size = size
        self.label = label
        self.cluster_size = 0
        self.warnings: list[str] = []
        self.root: Node | None = None
        self.serial = ""

    @abstractmethod
    def layout(self, node: Node) -> FileLayout:
        """Where the node's data lives on the device."""

    def describe(self) -> str:
        from ..util import format_size

        label = f" '{self.label}'" if self.label else ""
        return f"{self.kind}{label} ({format_size(self.size)})"
