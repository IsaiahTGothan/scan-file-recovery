"""Common interface for anything Lifeboat can read sectors from.

Implementations are strictly read-only: no code path in Lifeboat opens a
source for writing.
"""

from __future__ import annotations

import errno
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..errors import ReadErrorKind
from ..util import format_capacity


@dataclass
class SmartAttribute:
    ident: int
    name: str
    current: int
    worst: int
    raw: int


@dataclass
class HealthInfo:
    status: str                      # "Good", "Caution", "Bad", "Unknown"
    source: str                      # "ATA SMART", "NVMe health log", ...
    summary: list[str] = field(default_factory=list)
    attributes: list[SmartAttribute] = field(default_factory=list)


@dataclass
class DeviceInfo:
    path: str
    kind: str                        # "disk", "volume" or "image"
    size: int
    sector_size: int = 512
    physical_sector_size: int = 512
    model: str = ""
    vendor: str = ""
    serial: str = ""
    revision: str = ""
    bus: str = ""
    removable: bool = False
    system: bool = False             # holds the running operating system
    disk_number: int | None = None   # Windows PhysicalDrive number
    volumes: list[str] = field(default_factory=list)
    label: str = ""
    filesystem: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        if self.kind == "image":
            return self.path.replace("\\", "/").rsplit("/", 1)[-1]
        if self.kind == "volume":
            name = self.volumes[0] if self.volumes else self.path
            return f"{name} {self.label}".strip() if self.label else name
        name = " ".join(part for part in (self.vendor, self.model) if part).strip()
        return name or self.path

    @property
    def title(self) -> str:
        if self.kind == "disk" and self.disk_number is not None:
            return f"Disk {self.disk_number}: {self.display_name}"
        return self.display_name

    @property
    def capacity_text(self) -> str:
        return format_capacity(self.size)

    @property
    def identity(self) -> str:
        """Stable key used to find the same device again after a reconnect."""
        if self.kind == "disk":
            if self.serial:
                return f"disk|{self.model}|{self.serial}|{self.size}"
            return f"disk|{self.model}|{self.size}|{self.sector_size}"
        if self.kind == "volume":
            return f"volume|{self.path.upper()}"
        return f"{self.kind}|{self.path}|{self.size}"


class BlockDevice(ABC):
    """Read-only random access to a disk, volume or image.

    ``read_raw`` takes sector-aligned offsets and lengths, may raise
    :class:`~lifeboat.errors.ReadError` (with a ``kind``),
    :class:`~lifeboat.errors.DeviceGoneError` or
    :class:`~lifeboat.errors.DeviceHungError`, and must never return a
    shorter buffer silently: a short read is reported as an error for the
    missing part.
    """

    info: DeviceInfo
    supports_timeout: bool = False

    @property
    def size(self) -> int:
        return self.info.size

    @property
    def sector_size(self) -> int:
        return self.info.sector_size

    @property
    def physical_sector_size(self) -> int:
        return max(self.info.physical_sector_size, self.info.sector_size)

    @abstractmethod
    def read_raw(self, offset: int, length: int, timeout: float | None = None) -> bytes:
        ...

    def is_present(self) -> bool:
        """Cheap check that the device still answers."""
        return True

    def reopen(self) -> bool:
        """Try to reattach after a disconnect.  Returns True on success."""
        return True

    def close(self) -> None:
        pass

    def __enter__(self) -> BlockDevice:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _check_aligned(self, offset: int, length: int) -> None:
        ss = self.sector_size
        if offset % ss or length % ss or length <= 0 or offset < 0:
            raise ValueError(
                f"unaligned device read offset={offset} length={length} sector={ss}"
            )


# --- error classification -----------------------------------------------------------

_WIN_MEDIA = {23, 25, 27, 30, 31, 22, 1117, 483, 1127, 1129, 1393, 1785}
_WIN_TIMEOUT = {121, 1460, 258, 995}
_WIN_GONE = {2, 3, 6, 15, 21, 55, 433, 1110, 1112, 1167, 1617, 1006}
_WIN_RANGE = {38, 87}

_POSIX_MEDIA = {errno.EIO, getattr(errno, "ENODATA", 61), getattr(errno, "EBADMSG", 74),
                getattr(errno, "EREMOTEIO", 121), getattr(errno, "EILSEQ", 84)}
_POSIX_TIMEOUT = {errno.ETIMEDOUT}
_POSIX_GONE = {errno.ENODEV, errno.ENXIO, getattr(errno, "ENOMEDIUM", 123), errno.EBADF,
               getattr(errno, "ESHUTDOWN", 108), errno.ENOENT}
_POSIX_RANGE = {errno.EINVAL, getattr(errno, "EOVERFLOW", 75)}


def classify_windows_error(code: int) -> ReadErrorKind:
    if code in _WIN_TIMEOUT:
        return ReadErrorKind.TIMEOUT
    if code in _WIN_GONE:
        return ReadErrorKind.GONE
    if code in _WIN_RANGE:
        return ReadErrorKind.OUT_OF_RANGE
    if code in _WIN_MEDIA:
        return ReadErrorKind.MEDIA
    return ReadErrorKind.OTHER


def classify_posix_error(code: int) -> ReadErrorKind:
    if code in _POSIX_TIMEOUT:
        return ReadErrorKind.TIMEOUT
    if code in _POSIX_GONE:
        return ReadErrorKind.GONE
    if code in _POSIX_RANGE:
        return ReadErrorKind.OUT_OF_RANGE
    if code in _POSIX_MEDIA:
        return ReadErrorKind.MEDIA
    return ReadErrorKind.OTHER
