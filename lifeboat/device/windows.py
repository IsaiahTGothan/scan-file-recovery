"""Raw, read-only access to physical drives and volumes on Windows.

Design notes
------------
* Handles are opened with ``GENERIC_READ`` only, with
  ``FILE_FLAG_NO_BUFFERING`` (no read-ahead into neighbouring bad sectors)
  and ``FILE_FLAG_OVERLAPPED`` so every read can be given a deadline.
* When a read misses its deadline it is cancelled with ``CancelIoEx``.  If a
  dying drive does not even honour the cancel, the request's buffer and
  OVERLAPPED block are parked (never freed while the kernel may still write
  to them) and the caller is told the drive is hung.
* Device queries (size, model, serial, alignment, SMART) use a separate,
  non-overlapped handle.
"""

from __future__ import annotations

import ctypes
import string
import struct
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass

from ..errors import (
    E_OPEN_DENIED,
    DeviceGoneError,
    DeviceHungError,
    DeviceOpenError,
    ReadError,
    ReadErrorKind,
)
from .base import BlockDevice, DeviceInfo, HealthInfo, SmartAttribute, classify_windows_error

if sys.platform != "win32":  # pragma: no cover - imported only on Windows
    raise ImportError("lifeboat.device.windows is only available on Windows")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

HANDLE = wintypes.HANDLE
DWORD = wintypes.DWORD
BOOL = wintypes.BOOL
LPVOID = wintypes.LPVOID
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_OVERLAPPED = 0x40000000
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
INFINITE = 0xFFFFFFFF
ERROR_IO_PENDING = 997
ERROR_OPERATION_ABORTED = 995
ERROR_ACCESS_DENIED = 5
ERROR_FILE_NOT_FOUND = 2
ERROR_MORE_DATA = 234
STATUS_PENDING = 0x103

IOCTL_DISK_GET_DRIVE_GEOMETRY = 0x00070000
IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x000700A0
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x002D1080
IOCTL_STORAGE_CHECK_VERIFY2 = 0x002D0800
IOCTL_STORAGE_PREDICT_FAILURE = 0x002D1100
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000

DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4

_BUS_TYPES = {
    0: "Unknown", 1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "FireWire", 5: "SSA", 6: "Fibre Channel",
    7: "USB", 8: "RAID", 9: "iSCSI", 10: "SAS", 11: "SATA", 12: "SD", 13: "MMC",
    14: "Virtual", 15: "Virtual (file)", 16: "Storage Spaces", 17: "NVMe", 18: "SCM", 19: "UFS",
}


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", DWORD),
        ("OffsetHigh", DWORD),
        ("hEvent", HANDLE),
    ]


def _fn(name: str, argtypes: list[object], restype: object) -> object:
    func = getattr(kernel32, name)
    func.argtypes = argtypes
    func.restype = restype
    return func


CreateFileW = _fn("CreateFileW", [wintypes.LPCWSTR, DWORD, DWORD, LPVOID, DWORD, DWORD, HANDLE], HANDLE)
ReadFile = _fn("ReadFile", [HANDLE, LPVOID, DWORD, ctypes.POINTER(DWORD), ctypes.POINTER(OVERLAPPED)], BOOL)
GetOverlappedResult = _fn("GetOverlappedResult",
                          [HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(DWORD), BOOL], BOOL)
CancelIoEx = _fn("CancelIoEx", [HANDLE, ctypes.POINTER(OVERLAPPED)], BOOL)
CreateEventW = _fn("CreateEventW", [LPVOID, BOOL, BOOL, wintypes.LPCWSTR], HANDLE)
ResetEvent = _fn("ResetEvent", [HANDLE], BOOL)
WaitForSingleObject = _fn("WaitForSingleObject", [HANDLE, DWORD], DWORD)
CloseHandle = _fn("CloseHandle", [HANDLE], BOOL)
DeviceIoControl = _fn("DeviceIoControl",
                      [HANDLE, DWORD, LPVOID, DWORD, LPVOID, DWORD, ctypes.POINTER(DWORD), LPVOID], BOOL)
VirtualAlloc = _fn("VirtualAlloc", [LPVOID, ctypes.c_size_t, DWORD, DWORD], LPVOID)
VirtualFree = _fn("VirtualFree", [LPVOID, ctypes.c_size_t, DWORD], BOOL)
GetLogicalDrives = _fn("GetLogicalDrives", [], DWORD)
GetDriveTypeW = _fn("GetDriveTypeW", [wintypes.LPCWSTR], wintypes.UINT)
GetVolumeInformationW = _fn(
    "GetVolumeInformationW",
    [wintypes.LPCWSTR, wintypes.LPWSTR, DWORD, ctypes.POINTER(DWORD), ctypes.POINTER(DWORD),
     ctypes.POINTER(DWORD), wintypes.LPWSTR, DWORD],
    BOOL,
)
GetWindowsDirectoryW = _fn("GetWindowsDirectoryW", [wintypes.LPWSTR, wintypes.UINT], wintypes.UINT)
GetVolumePathNameW = _fn("GetVolumePathNameW", [wintypes.LPCWSTR, wintypes.LPWSTR, DWORD], BOOL)
GetVolumeNameForVolumeMountPointW = _fn("GetVolumeNameForVolumeMountPointW",
                                        [wintypes.LPCWSTR, wintypes.LPWSTR, DWORD], BOOL)
SetErrorMode = _fn("SetErrorMode", [wintypes.UINT], wintypes.UINT)

# Never let Windows pop up "insert a disk"/critical-error boxes for failing media.
SEM_FAILCRITICALERRORS = 0x0001
SEM_NOOPENFILEERRORBOX = 0x8000
SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX)


def is_admin() -> bool:
    try:
        return bool(shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _open(path: str, access: int, flags: int = 0) -> int:
    handle = CreateFileW(path, access, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, flags, None)
    if handle is None or handle == INVALID_HANDLE_VALUE:
        err = ctypes.get_last_error()
        if err == ERROR_ACCESS_DENIED:
            raise DeviceOpenError(
                f"Access denied to {path}. Run Lifeboat as administrator.", code=E_OPEN_DENIED
            )
        raise DeviceOpenError(f"Cannot open {path} (Windows error {err}: {ctypes.FormatError(err).strip()})")
    return int(handle)


def _ioctl(handle: int, code: int, in_buf: bytes | None, out_size: int) -> bytes | None:
    out = ctypes.create_string_buffer(out_size)
    returned = DWORD(0)
    if in_buf is not None:
        inp = ctypes.create_string_buffer(in_buf, len(in_buf))
        ok = DeviceIoControl(handle, code, inp, len(in_buf), out, out_size, ctypes.byref(returned), None)
    else:
        ok = DeviceIoControl(handle, code, None, 0, out, out_size, ctypes.byref(returned), None)
    if not ok:
        return None
    return out.raw[: returned.value]


def _cstr(blob: bytes, offset: int) -> str:
    if offset <= 0 or offset >= len(blob):
        return ""
    end = blob.find(b"\x00", offset)
    if end < 0:
        end = len(blob)
    return blob[offset:end].decode("ascii", "replace").strip()


@dataclass
class _Descriptor:
    vendor: str = ""
    product: str = ""
    revision: str = ""
    serial: str = ""
    bus: str = ""
    removable: bool = False


def _storage_descriptor(handle: int) -> _Descriptor:
    query = struct.pack("<II4x", 0, 0)  # StorageDeviceProperty, PropertyStandardQuery
    blob = _ioctl(handle, IOCTL_STORAGE_QUERY_PROPERTY, query, 4096)
    if not blob or len(blob) < 36:
        return _Descriptor()
    removable = blob[10] != 0
    vendor_off, product_off, rev_off, serial_off, bus_type = struct.unpack_from("<IIIII", blob, 12)
    serial = _cstr(blob, serial_off)
    return _Descriptor(
        vendor=_cstr(blob, vendor_off),
        product=_cstr(blob, product_off),
        revision=_cstr(blob, rev_off),
        serial=serial,
        bus=_BUS_TYPES.get(bus_type, f"Bus {bus_type}"),
        removable=removable,
    )


def _alignment(handle: int) -> tuple[int, int] | None:
    query = struct.pack("<II4x", 6, 0)  # StorageAccessAlignmentProperty
    blob = _ioctl(handle, IOCTL_STORAGE_QUERY_PROPERTY, query, 64)
    if not blob or len(blob) < 24:
        return None
    logical, physical = struct.unpack_from("<II", blob, 16)
    if logical in (512, 1024, 2048, 4096) and physical >= logical:
        return logical, physical
    return None


def _geometry_ex(handle: int) -> tuple[int, int, int] | None:
    blob = _ioctl(handle, IOCTL_DISK_GET_DRIVE_GEOMETRY_EX, None, 256)
    if not blob or len(blob) < 32:
        return None
    media_type, = struct.unpack_from("<I", blob, 8)
    bytes_per_sector, = struct.unpack_from("<I", blob, 20)
    disk_size, = struct.unpack_from("<q", blob, 24)
    return disk_size, bytes_per_sector, media_type


def _length_info(handle: int) -> int | None:
    blob = _ioctl(handle, IOCTL_DISK_GET_LENGTH_INFO, None, 8)
    if not blob or len(blob) < 8:
        return None
    return int(struct.unpack_from("<q", blob, 0)[0])


def _geometry_sector_size(handle: int) -> int | None:
    blob = _ioctl(handle, IOCTL_DISK_GET_DRIVE_GEOMETRY, None, 24)
    if not blob or len(blob) < 24:
        return None
    return int(struct.unpack_from("<I", blob, 20)[0])


def _volume_disk_extents(handle: int) -> list[tuple[int, int, int]]:
    blob = _ioctl(handle, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, None, 8 + 24 * 32)
    if not blob or len(blob) < 8:
        return []
    count, = struct.unpack_from("<I", blob, 0)
    extents = []
    for i in range(count):
        pos = 8 + 24 * i
        if pos + 24 > len(blob):
            break
        disk, = struct.unpack_from("<I", blob, pos)
        start, length = struct.unpack_from("<qq", blob, pos + 8)
        extents.append((disk, start, length))
    return extents


@dataclass
class VolumeEntry:
    letter: str           # "E:"
    label: str
    filesystem: str
    disks: list[int]
    drive_type: int
    size: int = 0


def list_volumes() -> list[VolumeEntry]:
    mask = GetLogicalDrives()
    out: list[VolumeEntry] = []
    for index, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << index):
            continue
        root = f"{letter}:\\"
        dtype = GetDriveTypeW(root)
        if dtype not in (DRIVE_REMOVABLE, DRIVE_FIXED):
            continue
        label_buf = ctypes.create_unicode_buffer(261)
        fs_buf = ctypes.create_unicode_buffer(261)
        serial = DWORD(0)
        maxlen = DWORD(0)
        flags = DWORD(0)
        ok = GetVolumeInformationW(root, label_buf, 261, ctypes.byref(serial), ctypes.byref(maxlen),
                                   ctypes.byref(flags), fs_buf, 261)
        label = label_buf.value if ok else ""
        fs_name = fs_buf.value if ok else "RAW"
        disks: list[int] = []
        size = 0
        try:
            handle = _open(f"\\\\.\\{letter}:", 0)
        except DeviceOpenError:
            handle = 0
        if handle:
            try:
                disks = sorted({disk for disk, _start, _len in _volume_disk_extents(handle)})
            finally:
                CloseHandle(handle)
        try:
            handle = _open(f"\\\\.\\{letter}:", GENERIC_READ)
        except DeviceOpenError:
            handle = 0
        if handle:
            try:
                size = _length_info(handle) or 0
            finally:
                CloseHandle(handle)
        out.append(VolumeEntry(f"{letter}:", label, fs_name, disks, dtype, size))
    return out


def windows_system_disks(volumes: list[VolumeEntry] | None = None) -> set[int]:
    buf = ctypes.create_unicode_buffer(300)
    if not GetWindowsDirectoryW(buf, 300):
        return set()
    letter = buf.value[:2].upper()
    for vol in volumes if volumes is not None else list_volumes():
        if vol.letter == letter:
            return set(vol.disks)
    return set()


def disks_for_path(path: str) -> list[int]:
    """Physical disk numbers backing the volume that contains ``path``."""
    buf = ctypes.create_unicode_buffer(1024)
    if not GetVolumePathNameW(path, buf, 1024):
        return []
    root = buf.value.rstrip("\\")
    if len(root) == 2 and root[1] == ":":
        device = f"\\\\.\\{root}"
    elif root.startswith("\\\\?\\Volume{"):
        device = root
    else:
        # A volume mounted in a folder (such as C:\Data\Disk2): ask for its volume name.
        guid = ctypes.create_unicode_buffer(1024)
        if not GetVolumeNameForVolumeMountPointW(buf.value, guid, 1024):
            return []
        device = guid.value.rstrip("\\")
    try:
        handle = _open(device, 0)
    except DeviceOpenError:
        return []
    try:
        return sorted({disk for disk, _s, _l in _volume_disk_extents(handle)})
    finally:
        CloseHandle(handle)


def is_network_path(path: str) -> bool:
    """True for UNC paths and mapped network drives."""
    buf = ctypes.create_unicode_buffer(1024)
    if not GetVolumePathNameW(path, buf, 1024):
        return path.startswith("\\\\") and not path.startswith(("\\\\?\\", "\\\\.\\"))
    root = buf.value
    if root.startswith("\\\\") and not root.startswith(("\\\\?\\", "\\\\.\\")):
        return True
    return GetDriveTypeW(root) == DRIVE_REMOTE


def list_devices() -> list[DeviceInfo]:
    admin = is_admin()
    volumes = list_volumes()
    system = windows_system_disks(volumes)
    by_disk: dict[int, list[VolumeEntry]] = {}
    for vol in volumes:
        for disk in vol.disks:
            by_disk.setdefault(disk, []).append(vol)
    devices: list[DeviceInfo] = []
    misses = 0
    for number in range(128):
        path = f"\\\\.\\PhysicalDrive{number}"
        try:
            handle = _open(path, 0)
        except DeviceOpenError:
            misses += 1
            if misses > 32 and number > 32:
                break
            continue
        try:
            geo = _geometry_ex(handle)
            desc = _storage_descriptor(handle)
            align = _alignment(handle)
        finally:
            CloseHandle(handle)
        if geo is None:
            continue
        size, bps, media_type = geo
        logical, physical = align if align else (bps, bps)
        notes = []
        if not admin:
            notes.append("Run Lifeboat as administrator to read this drive.")
        vols = by_disk.get(number, [])
        devices.append(
            DeviceInfo(
                path=path,
                kind="disk",
                size=size,
                sector_size=logical or bps or 512,
                physical_sector_size=physical or logical or 512,
                model=desc.product,
                vendor=desc.vendor,
                serial=desc.serial,
                revision=desc.revision,
                bus=desc.bus,
                removable=desc.removable or media_type == 11,
                system=number in system,
                disk_number=number,
                volumes=[f"{v.letter} {v.label}".strip() for v in vols],
                notes=notes,
            )
        )
    for vol in volumes:
        devices.append(
            DeviceInfo(
                path=f"\\\\.\\{vol.letter}",
                kind="volume",
                size=vol.size,
                volumes=[vol.letter],
                label=vol.label,
                filesystem=vol.filesystem,
                system=bool(set(vol.disks) & system),
                disk_number=vol.disks[0] if len(vol.disks) == 1 else None,
                notes=[] if admin else ["Run Lifeboat as administrator to read this volume."],
            )
        )
    return devices


class _Buffer:
    __slots__ = ("address", "size")

    def __init__(self, size: int) -> None:
        address = VirtualAlloc(None, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
        if not address:
            raise MemoryError("VirtualAlloc failed")
        self.address = address
        self.size = size

    def free(self) -> None:
        if self.address:
            VirtualFree(self.address, 0, MEM_RELEASE)
            self.address = 0


class _Zombie:
    """A cancelled read the driver has not completed yet."""

    __slots__ = ("buffer", "overlapped", "event")

    def __init__(self, buffer: _Buffer, overlapped: OVERLAPPED, event: int) -> None:
        self.buffer = buffer
        self.overlapped = overlapped
        self.event = event

    def done(self) -> bool:
        return self.overlapped.Internal != STATUS_PENDING

    def release(self) -> None:
        self.buffer.free()
        CloseHandle(self.event)


class WindowsDevice(BlockDevice):
    supports_timeout = True

    def __init__(self, path: str, info: DeviceInfo | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._zombies: list[_Zombie] = []
        self._buffer: _Buffer | None = None
        self._event = 0
        self._handle = 0
        self._query = 0
        self.info = info or DeviceInfo(path=path, kind="volume" if path.rstrip("\\")[-1:] == ":" else "disk",
                                       size=0)
        self._open_handles(path)
        self._refresh_geometry()

    # -- opening -----------------------------------------------------------------
    def _open_handles(self, path: str) -> None:
        handle = _open(path, GENERIC_READ, FILE_FLAG_NO_BUFFERING | FILE_FLAG_OVERLAPPED)
        try:
            query = _open(path, GENERIC_READ, 0)
        except DeviceOpenError:
            CloseHandle(handle)
            raise
        event = CreateEventW(None, True, False, None)
        if not event:
            CloseHandle(handle)
            CloseHandle(query)
            raise DeviceOpenError("CreateEvent failed")
        self._handle, self._query, self._event = handle, query, int(event)
        self._path = path

    def _refresh_geometry(self) -> None:
        size: int | None
        bps: int | None
        if self.info.kind == "volume":
            # Disk geometry IOCTLs are forwarded to the whole disk; only the
            # length query reports the size of the volume itself.
            size = _length_info(self._query)
            bps = _geometry_sector_size(self._query)
        else:
            geo = _geometry_ex(self._query)
            size = geo[0] if geo else _length_info(self._query)
            bps = geo[1] if geo else _geometry_sector_size(self._query)
        align = _alignment(self._query)
        if size is None or size <= 0:
            raise DeviceOpenError(f"Could not determine the size of {self._path}")
        logical = align[0] if align else (bps or 512)
        physical = align[1] if align else logical
        self.info.size = size - (size % logical)
        self.info.sector_size = logical
        self.info.physical_sector_size = max(physical, logical)
        if self.info.kind == "disk" and not (self.info.model or self.info.serial):
            desc = _storage_descriptor(self._query)
            self.info.model = desc.product
            self.info.vendor = desc.vendor
            self.info.serial = desc.serial
            self.info.bus = desc.bus

    # -- reading -----------------------------------------------------------------
    def _reap(self) -> None:
        alive = []
        for zombie in self._zombies:
            if zombie.done():
                zombie.release()
            else:
                alive.append(zombie)
        self._zombies = alive

    def _wait_for_zombies(self, timeout: float | None) -> None:
        if not self._zombies:
            return
        wait_ms = INFINITE if timeout is None else max(1, int(timeout * 1000))
        for zombie in list(self._zombies):
            if not zombie.done():
                WaitForSingleObject(zombie.event, wait_ms)
        self._reap()
        if self._zombies:
            raise DeviceHungError("The drive has not finished a cancelled read and is not responding.")

    def read_raw(self, offset: int, length: int, timeout: float | None = None) -> bytes:
        self._check_aligned(offset, length)
        with self._lock:
            if not self._handle:
                raise DeviceGoneError("Device handle is closed")
            self._reap()
            self._wait_for_zombies(timeout)
            if offset + length > self.info.size:
                raise ReadError("Read past end of device", kind=ReadErrorKind.OUT_OF_RANGE,
                                offset=offset, length=length)
            if self._buffer is None or self._buffer.size < length:
                if self._buffer is not None:
                    self._buffer.free()
                self._buffer = _Buffer(max(length, 1 << 20))
            buf = self._buffer
            ov = OVERLAPPED()
            ov.Offset = offset & 0xFFFFFFFF
            ov.OffsetHigh = (offset >> 32) & 0xFFFFFFFF
            ov.hEvent = self._event
            ResetEvent(self._event)
            ok = ReadFile(self._handle, buf.address, length, None, ctypes.byref(ov))
            if not ok:
                err = ctypes.get_last_error()
                if err != ERROR_IO_PENDING:
                    raise self._error(err, offset, length)
            timed_out = False
            wait_ms = INFINITE if timeout is None else max(1, int(timeout * 1000))
            if WaitForSingleObject(self._event, wait_ms) == WAIT_TIMEOUT:
                timed_out = True
                CancelIoEx(self._handle, ctypes.byref(ov))
                if WaitForSingleObject(self._event, 3000) == WAIT_TIMEOUT:
                    # The driver ignores the cancel.  Park the buffer and the
                    # OVERLAPPED block: the kernel still owns them.
                    self._zombies.append(_Zombie(buf, ov, self._event))
                    self._buffer = None
                    event = CreateEventW(None, True, False, None)
                    self._event = int(event) if event else 0
                    raise ReadError(
                        f"Read timed out after {timeout:.0f} s and could not be cancelled",
                        kind=ReadErrorKind.TIMEOUT, offset=offset, length=length,
                    )
            transferred = DWORD(0)
            ok = GetOverlappedResult(self._handle, ctypes.byref(ov), ctypes.byref(transferred), False)
            if not ok:
                err = ctypes.get_last_error()
                if timed_out or err == ERROR_OPERATION_ABORTED:
                    raise ReadError(
                        f"Read timed out after {timeout or 0:.0f} s",
                        kind=ReadErrorKind.TIMEOUT, offset=offset, length=length, os_code=err,
                    )
                raise self._error(err, offset, length)
            got = transferred.value
            if got != length:
                raise ReadError(
                    "Short read from device",
                    kind=ReadErrorKind.OUT_OF_RANGE if got == 0 else ReadErrorKind.MEDIA,
                    offset=offset + got, length=length - got,
                )
            return ctypes.string_at(buf.address, length)

    def _error(self, err: int, offset: int, length: int) -> ReadError:
        text = ctypes.FormatError(err).strip()
        return ReadError(
            f"Windows error {err}: {text}",
            kind=classify_windows_error(err),
            offset=offset,
            length=length,
            os_code=err,
        )

    # -- presence / reconnect ------------------------------------------------------
    def is_present(self) -> bool:
        if not self._query:
            return False
        out = DWORD(0)
        ok = DeviceIoControl(self._query, IOCTL_STORAGE_CHECK_VERIFY2, None, 0, None, 0, ctypes.byref(out), None)
        if ok:
            return True
        # Volumes may not implement CHECK_VERIFY2; fall back to a size query.
        return _length_info(self._query) is not None

    def reopen(self) -> bool:
        from .enumerate import find_device

        target = find_device(self.info.identity)
        path = target.path if target is not None else self._path
        with self._lock:
            old = (self._handle, self._query, self._event)
            try:
                self._open_handles(path)
            except DeviceOpenError:
                return False
            for handle in old:
                if handle:
                    CloseHandle(handle)
            if target is not None:
                self.info.path = target.path
                self.info.disk_number = target.disk_number
            return True

    def health(self) -> HealthInfo | None:
        return read_health(self._query)

    def close(self) -> None:
        with self._lock:
            for handle in (self._handle, self._query):
                if handle:
                    CloseHandle(handle)
            self._handle = self._query = 0
            self._reap()
            if not self._zombies and self._event:
                CloseHandle(self._event)
                self._event = 0
            if self._buffer is not None and not self._zombies:
                self._buffer.free()
                self._buffer = None


# --- health (best effort) -----------------------------------------------------------------

_SMART_NAMES = {
    1: "Read error rate", 3: "Spin-up time", 4: "Start/stop count", 5: "Reallocated sectors",
    7: "Seek error rate", 9: "Power-on hours", 10: "Spin retry count", 12: "Power cycles",
    184: "End-to-end errors", 187: "Reported uncorrectable", 188: "Command timeouts",
    190: "Airflow temperature", 194: "Temperature", 196: "Reallocation events",
    197: "Pending sectors", 198: "Offline uncorrectable", 199: "UDMA CRC errors",
}


def _parse_ata_smart(data: bytes) -> list[SmartAttribute]:
    attrs = []
    for i in range(30):
        pos = 2 + i * 12
        ident = data[pos]
        if ident == 0:
            continue
        current = data[pos + 3]
        worst = data[pos + 4]
        raw = int.from_bytes(data[pos + 5: pos + 11], "little")
        attrs.append(SmartAttribute(ident, _SMART_NAMES.get(ident, f"Attribute {ident}"), current, worst, raw))
    return attrs


def read_health(handle: int) -> HealthInfo | None:
    blob = _ioctl(handle, IOCTL_STORAGE_PREDICT_FAILURE, None, 516)
    if blob and len(blob) >= 516:
        predict, = struct.unpack_from("<I", blob, 0)
        attrs = _parse_ata_smart(blob[4:516])
        raw = {a.ident: a.raw & 0xFFFFFFFF for a in attrs}
        summary = []
        status = "Good"
        for ident, label in ((5, "reallocated"), (197, "pending"), (198, "uncorrectable")):
            value = raw.get(ident, 0)
            if value:
                summary.append(f"{value} {label} sectors")
                status = "Caution"
        if raw.get(199):
            summary.append(f"{raw[199]} cable (CRC) errors - try another cable/port")
        if 9 in raw:
            summary.append(f"{raw[9]:,} power-on hours")
        if predict:
            status = "Bad"
            summary.insert(0, "The drive predicts its own failure (SMART)")
        if attrs or predict:
            return HealthInfo(status, "ATA SMART", summary, attrs)
    # NVMe health information log
    query = bytearray(8 + 40 + 512)
    struct.pack_into("<II", query, 0, 50, 0)  # StorageDeviceProtocolSpecificProperty, standard query
    struct.pack_into("<IIIIII", query, 8, 3, 2, 2, 0, 40, 512)
    blob = _ioctl(handle, IOCTL_STORAGE_QUERY_PROPERTY, bytes(query), len(query))
    if blob and len(blob) >= 48 + 192:
        log = blob[48:48 + 512]
        critical = log[0]
        temp_k = int.from_bytes(log[1:3], "little")
        spare = log[3]
        used = log[5]
        hours = int.from_bytes(log[128:144], "little")
        media_errors = int.from_bytes(log[160:176], "little")
        status = "Good"
        summary = [f"{used}% of rated life used", f"{spare}% spare remaining", f"{hours:,} power-on hours"]
        if temp_k:
            summary.append(f"{temp_k - 273} °C")
        if media_errors:
            summary.append(f"{media_errors} media errors")
            status = "Caution"
        if critical:
            summary.insert(0, f"Critical warning flags 0x{critical:02X}")
            status = "Bad"
        return HealthInfo(status, "NVMe health log", summary, [])
    return None
