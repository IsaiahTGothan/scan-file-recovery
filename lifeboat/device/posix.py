"""Raw block devices on Linux (and other POSIX systems).

Reads use ``O_DIRECT`` where available so the kernel does not read ahead
into neighbouring (possibly bad) sectors, and each read runs on a helper
thread so that a hung drive cannot freeze Lifeboat.
"""

from __future__ import annotations

import fcntl
import mmap
import os
import re
import struct
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

from ..errors import DeviceGoneError, DeviceHungError, DeviceOpenError, ReadError, ReadErrorKind
from .base import BlockDevice, DeviceInfo, classify_posix_error

_BLKGETSIZE64 = 0x80081272
_BLKSSZGET = 0x1268
_BLKPBSZGET = 0x127B


def _ioctl_int(fd: int, request: int, fmt: str) -> int | None:
    try:
        size = struct.calcsize(fmt)
        buf = fcntl.ioctl(fd, request, bytes(size))
        return int(struct.unpack(fmt, buf)[0])
    except OSError:
        return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return ""


def _udev_properties(devname: str) -> dict[str, str]:
    props: dict[str, str] = {}
    dev_file = Path("/sys/block") / devname / "dev"
    majmin = _read_text(dev_file)
    if not majmin:
        return props
    data = Path("/run/udev/data") / f"b{majmin}"
    try:
        for line in data.read_text(errors="replace").splitlines():
            if line.startswith("E:") and "=" in line:
                key, value = line[2:].split("=", 1)
                props[key] = value
    except OSError:
        pass
    return props


def _mounts() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0].startswith("/dev/"):
                    mountpoint = parts[1].replace("\\040", " ")
                    result.setdefault(os.path.realpath(parts[0]), []).append(mountpoint)
    except OSError:
        pass
    return result


def list_devices() -> list[DeviceInfo]:
    """Enumerate whole disks from /sys/block (Linux only)."""
    base = Path("/sys/block")
    if not base.is_dir():
        return []
    mounts = _mounts()
    devices: list[DeviceInfo] = []
    for entry in sorted(base.iterdir()):
        name = entry.name
        if re.match(r"^(ram|zram|sr|fd|md|dm-|nbd)", name):
            continue
        sectors = _read_text(entry / "size")
        if not sectors.isdigit() or int(sectors) == 0:
            continue
        size = int(sectors) * 512
        logical = int(_read_text(entry / "queue" / "logical_block_size") or 512)
        physical = int(_read_text(entry / "queue" / "physical_block_size") or logical)
        model = _read_text(entry / "device" / "model")
        vendor = _read_text(entry / "device" / "vendor")
        udev = _udev_properties(name)
        serial = _read_text(entry / "device" / "serial") or udev.get("ID_SERIAL_SHORT", "")
        resolved = str(entry.resolve())
        if "/usb" in resolved:
            bus = "USB"
        elif "nvme" in name:
            bus = "NVMe"
        elif "mmc" in name:
            bus = "SD/MMC"
        elif name.startswith("loop"):
            bus = "Loop"
        elif "/ata" in resolved:
            bus = "SATA"
        else:
            bus = udev.get("ID_BUS", "").upper()
        volumes: list[str] = []
        system = False
        dev_path = f"/dev/{name}"
        for part in sorted(entry.glob(f"{name}*")):
            for mp in mounts.get(f"/dev/{part.name}", []):
                volumes.append(mp)
                if mp == "/":
                    system = True
        for mp in mounts.get(dev_path, []):
            volumes.append(mp)
            if mp == "/":
                system = True
        devices.append(
            DeviceInfo(
                path=dev_path,
                kind="disk",
                size=size,
                sector_size=logical,
                physical_sector_size=physical,
                model=model or name,
                vendor=vendor,
                serial=serial,
                bus=bus,
                removable=_read_text(entry / "removable") == "1",
                system=system,
                volumes=volumes,
            )
        )
    return devices


class PosixDevice(BlockDevice):
    supports_timeout = True

    def __init__(self, path: str, info: DeviceInfo | None = None) -> None:
        self._path = path
        self._direct = False
        self._fd = self._open(path)
        size = _ioctl_int(self._fd, _BLKGETSIZE64, "Q") if sys.platform.startswith("linux") else None
        if not size:
            size = os.lseek(self._fd, 0, os.SEEK_END)
        logical = _ioctl_int(self._fd, _BLKSSZGET, "i") if sys.platform.startswith("linux") else None
        physical = _ioctl_int(self._fd, _BLKPBSZGET, "I") if sys.platform.startswith("linux") else None
        if info is None:
            info = DeviceInfo(path=path, kind="disk", size=size)
        info.size = size
        info.sector_size = logical or info.sector_size or 512
        info.physical_sector_size = physical or info.physical_sector_size or info.sector_size
        self.info = info
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lifeboat-io")
        self._stuck: Future[bytes] | None = None

    def _open(self, path: str) -> int:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        direct = getattr(os, "O_DIRECT", 0)
        try:
            if direct:
                try:
                    fd = os.open(path, flags | direct)
                    self._direct = True
                    return fd
                except OSError as exc:
                    if exc.errno != 22:  # EINVAL: O_DIRECT unsupported here
                        raise
            fd = os.open(path, flags)
            self._direct = False
            return fd
        except PermissionError as exc:
            raise DeviceOpenError(
                f"Permission denied opening {path}. Run Lifeboat as root/administrator.",
                code="LB-101",
            ) from exc
        except OSError as exc:
            raise DeviceOpenError(f"Cannot open {path}: {exc.strerror}") from exc

    def _blocking_read(self, fd: int, offset: int, length: int) -> bytes:
        if self._direct:
            buf = mmap.mmap(-1, length)
            try:
                got = os.preadv(fd, [buf], offset)
                if got != length:
                    raise ReadError(
                        "Short read from device",
                        kind=ReadErrorKind.OUT_OF_RANGE if got == 0 else ReadErrorKind.MEDIA,
                        offset=offset + got,
                        length=length - got,
                    )
                return bytes(buf)
            finally:
                buf.close()
        data = os.pread(fd, length, offset)
        if len(data) != length:
            raise ReadError(
                "Short read from device",
                kind=ReadErrorKind.OUT_OF_RANGE if not data else ReadErrorKind.MEDIA,
                offset=offset + len(data),
                length=length - len(data),
            )
        return data

    def read_raw(self, offset: int, length: int, timeout: float | None = None) -> bytes:
        self._check_aligned(offset, length)
        if self._fd < 0:
            raise DeviceGoneError("Device is closed")
        if self._stuck is not None:
            try:
                self._stuck.result(timeout=timeout if timeout else None)
            except FutureTimeout as exc:
                raise DeviceHungError(
                    "The drive has not finished a previous read and is not responding."
                ) from exc
            except Exception:  # noqa: BLE001 - the stuck read's own error is irrelevant now
                pass
            self._stuck = None
        if offset + length > self.size:
            raise ReadError("Read past end of device", kind=ReadErrorKind.OUT_OF_RANGE,
                            offset=offset, length=length)
        fd = self._fd
        future = self._executor.submit(self._blocking_read, fd, offset, length)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as exc:
            self._stuck = future
            raise ReadError(
                f"Read timed out after {timeout:.0f} s", kind=ReadErrorKind.TIMEOUT,
                offset=offset, length=length,
            ) from exc
        except ReadError:
            raise
        except OSError as exc:
            kind = classify_posix_error(exc.errno or 0)
            raise ReadError(
                f"Read failed: {exc.strerror or exc}", kind=kind, offset=offset, length=length,
                os_code=exc.errno,
            ) from exc

    def is_present(self) -> bool:
        if self._fd < 0 or not os.path.exists(self._path):
            return False
        try:
            return os.lseek(self._fd, 0, os.SEEK_END) > 0
        except OSError:
            return False

    def reopen(self) -> bool:
        from .enumerate import find_device  # local import to avoid a cycle

        match = find_device(self.info.identity)
        path = match.path if match is not None else self._path
        if not os.path.exists(path):
            return False
        try:
            fd = self._open(path)
        except DeviceOpenError:
            return False
        old = self._fd
        self._fd = fd
        self._path = path
        self.info.path = path
        self._stuck = None
        self._executor.shutdown(wait=False)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lifeboat-io")
        if old >= 0:
            try:
                os.close(old)
            except OSError:
                pass
        return True

    def close(self) -> None:
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1
        self._executor.shutdown(wait=False)
