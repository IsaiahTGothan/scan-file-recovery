"""Destination drive helpers: long paths, timestamps, free space, identity.

On Windows every destination path goes through the ``\\\\?\\`` prefix, which
lifts the 260-character MAX_PATH limit that makes Explorer fail on deep
folder trees.
"""

from __future__ import annotations

import errno
import os
import shutil
import sys

from ..util import unix_to_filetime

IS_WINDOWS = sys.platform == "win32"

# Windows error codes meaning "no space" / "device went away".
WIN_DISK_FULL = {39, 112}
WIN_GONE = {2, 3, 15, 21, 53, 55, 64, 433, 1006, 1167, 1617}


def long_path(path: str) -> str:
    if not IS_WINDOWS:
        return os.path.abspath(path)
    if path.startswith("\\\\?\\"):
        return path
    full = os.path.abspath(path)
    if full.startswith("\\\\"):
        return "\\\\?\\UNC\\" + full[2:]
    return "\\\\?\\" + full


def display_path(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def make_dirs(path: str) -> None:
    """``os.makedirs`` that also works with extended-length paths."""
    path = long_path(path)
    if os.path.isdir(path):
        return
    missing = []
    current = path
    while not os.path.isdir(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        missing.append(current)
        current = parent
    for item in reversed(missing):
        try:
            os.mkdir(item)
        except FileExistsError:
            if not os.path.isdir(item):
                raise


def is_disk_full(exc: OSError) -> bool:
    if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
        return True
    return IS_WINDOWS and getattr(exc, "winerror", None) in WIN_DISK_FULL


def is_gone(exc: OSError) -> bool:
    if IS_WINDOWS and getattr(exc, "winerror", None) in WIN_GONE:
        return True
    return exc.errno in (errno.ENODEV, errno.ENXIO, errno.EIO, errno.ENOENT, getattr(errno, "ESTALE", errno.EIO))


# ------------------------------------------------------------------ timestamps
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

    class _FILETIME(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    _CreateFileW = _k32.CreateFileW
    _CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _CreateFileW.restype = wintypes.HANDLE
    _SetFileTime = _k32.SetFileTime
    _SetFileTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
                             ctypes.POINTER(_FILETIME)]
    _SetFileTime.restype = wintypes.BOOL
    _CloseHandle = _k32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _GetVolumePathNameW = _k32.GetVolumePathNameW
    _GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    _GetVolumePathNameW.restype = wintypes.BOOL
    _GetVolumeInformationW = _k32.GetVolumeInformationW
    _GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                                       ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
    _GetVolumeInformationW.restype = wintypes.BOOL
    _INVALID = ctypes.c_void_p(-1).value

    def _ft(ts: float | None) -> _FILETIME | None:
        if ts is None:
            return None
        value = unix_to_filetime(ts)
        if value <= 0:
            return None
        return _FILETIME(value & 0xFFFFFFFF, value >> 32)

    def set_times(path: str, ctime: float | None, atime: float | None, mtime: float | None) -> bool:
        handle = _CreateFileW(long_path(path), 0x100, 0x7, None, 3, 0x02000000, None)  # FILE_WRITE_ATTRIBUTES
        if handle is None or handle == _INVALID:
            return False
        try:
            c, a, m = _ft(ctime), _ft(atime), _ft(mtime)
            return bool(_SetFileTime(handle, ctypes.byref(c) if c else None, ctypes.byref(a) if a else None,
                                     ctypes.byref(m) if m else None))
        finally:
            _CloseHandle(handle)

    def volume_filesystem(path: str) -> str:
        buf = ctypes.create_unicode_buffer(1024)
        if not _GetVolumePathNameW(long_path(path), buf, 1024):
            return ""
        fs_buf = ctypes.create_unicode_buffer(64)
        if not _GetVolumeInformationW(buf.value, None, 0, None, None, None, fs_buf, 64):
            return ""
        return fs_buf.value
else:
    def set_times(path: str, ctime: float | None, atime: float | None, mtime: float | None) -> bool:
        if mtime is None and atime is None:
            return False
        m = mtime if mtime is not None else atime
        a = atime if atime is not None else m
        try:
            os.utime(path, (a, m))  # type: ignore[arg-type]
            return True
        except OSError:
            return False

    def volume_filesystem(path: str) -> str:
        best = ""
        fstype = ""
        real = os.path.realpath(path)
        try:
            with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    mount = parts[1].replace("\\040", " ")
                    if (real == mount or real.startswith(mount.rstrip("/") + "/")) and len(mount) >= len(best):
                        best, fstype = mount, parts[2]
        except OSError:
            return ""
        return fstype


def free_space(path: str) -> int:
    try:
        return shutil.disk_usage(long_path(path)).free
    except OSError:
        return -1


def disks_for_path(path: str) -> set[str]:
    """Physical disks holding ``path`` (``"disk:3"`` on Windows, ``/dev/sdb`` on Linux)."""
    if IS_WINDOWS:
        try:
            from ..device.windows import disks_for_path as _win

            return {f"disk:{n}" for n in _win(os.path.abspath(path))}
        except Exception:  # noqa: BLE001
            return set()
    try:
        st = os.stat(path)
    except OSError:
        return set()
    major, minor = os.major(st.st_dev), os.minor(st.st_dev)
    sys_path = f"/sys/dev/block/{major}:{minor}"
    if not os.path.exists(sys_path):
        return set()
    real = os.path.realpath(sys_path)
    if os.path.exists(os.path.join(real, "partition")):
        real = os.path.dirname(real)
    return {f"/dev/{os.path.basename(real)}"}


def disks_for_source(path: str, kind: str, disk_number: int | None) -> set[str]:
    if IS_WINDOWS:
        if kind in ("disk", "volume") and disk_number is not None:
            return {f"disk:{disk_number}"}
        if kind == "volume":
            return disks_for_path(path.replace("\\\\.\\", "") + "\\")
        return set()
    if kind == "disk":
        return {os.path.realpath(path)}
    return set()
