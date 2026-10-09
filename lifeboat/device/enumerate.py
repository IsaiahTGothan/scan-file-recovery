"""Platform-independent device discovery and opening."""

from __future__ import annotations

import logging
import os
import sys

from ..errors import DeviceOpenError
from .base import BlockDevice, DeviceInfo
from .image import ImageDevice

log = logging.getLogger("lifeboat.device")


def list_devices() -> list[DeviceInfo]:
    """List physical disks (and on Windows, mounted volumes)."""
    try:
        if sys.platform == "win32":
            from .windows import list_devices as _list
        elif sys.platform.startswith("linux"):
            from .posix import list_devices as _list
        else:
            return []
        return _list()
    except Exception:
        log.exception("Device enumeration failed")
        return []


def find_device(identity: str) -> DeviceInfo | None:
    for info in list_devices():
        if info.identity == identity:
            return info
    return None


def is_admin() -> bool:
    if sys.platform == "win32":
        from .windows import is_admin as _is_admin

        return _is_admin()
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def open_device(source: DeviceInfo | str, sector_size: int | None = None) -> BlockDevice:
    """Open a disk, volume or image file read-only."""
    info = source if isinstance(source, DeviceInfo) else None
    path = info.path if info is not None else str(source)
    if (info is not None and info.kind == "image") or os.path.isfile(path):
        return ImageDevice(path, sector_size=sector_size or 512)
    if sys.platform == "win32":
        from .windows import WindowsDevice

        return WindowsDevice(path, info)
    if os.name == "posix":
        from .posix import PosixDevice

        return PosixDevice(path, info)
    raise DeviceOpenError(f"Raw device access is not supported on {sys.platform}")
