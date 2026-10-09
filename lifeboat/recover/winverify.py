"""Read destination files back from the disk itself on Windows.

``FILE_FLAG_NO_BUFFERING`` bypasses the Windows file cache, so verification
proves the data really reached the destination drive instead of comparing
against a copy still sitting in memory.
"""

from __future__ import annotations

import ctypes
import hashlib
import sys
from ctypes import wintypes

if sys.platform != "win32":  # pragma: no cover
    raise ImportError("Windows only")

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_CreateFileW = _k32.CreateFileW
_CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                         wintypes.DWORD, wintypes.HANDLE]
_CreateFileW.restype = wintypes.HANDLE
_ReadFile = _k32.ReadFile
_ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                      wintypes.LPVOID]
_ReadFile.restype = wintypes.BOOL
_SetFilePointerEx = _k32.SetFilePointerEx
_SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong),
                              wintypes.DWORD]
_SetFilePointerEx.restype = wintypes.BOOL
_CloseHandle = _k32.CloseHandle
_CloseHandle.argtypes = [wintypes.HANDLE]
_VirtualAlloc = _k32.VirtualAlloc
_VirtualAlloc.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
_VirtualAlloc.restype = wintypes.LPVOID
_VirtualFree = _k32.VirtualFree
_VirtualFree.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD]

_INVALID = ctypes.c_void_p(-1).value
_BLOCK = 1 << 20
_ALIGN = 4096


def _open(path: str, unbuffered: bool) -> int:
    flags = 0x20000000 | 0x08000000 if unbuffered else 0x08000000  # NO_BUFFERING | SEQUENTIAL_SCAN
    handle = _CreateFileW(path, 0x80000000, 0x7, None, 3, flags, None)
    if handle is None or handle == _INVALID:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(handle)


def _read_blocks(path: str, start: int, length: int | None):
    """Yield the file's bytes from aligned ``start`` (to EOF when ``length`` is None)."""
    try:
        handle = _open(path, True)
        unbuffered = True
    except OSError:
        handle = _open(path, False)
        unbuffered = False
    buf = _VirtualAlloc(None, _BLOCK, 0x3000, 0x04)
    if not buf:
        _CloseHandle(handle)
        raise MemoryError("VirtualAlloc failed")
    try:
        if start and not _SetFilePointerEx(handle, start, None, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        remaining = length
        got = wintypes.DWORD(0)
        while remaining is None or remaining > 0:
            want = _BLOCK if remaining is None else min(_BLOCK, -(-remaining // _ALIGN) * _ALIGN)
            if not unbuffered and remaining is not None:
                want = min(_BLOCK, remaining)
            if not _ReadFile(handle, buf, want, ctypes.byref(got), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if got.value == 0:
                return
            data = ctypes.string_at(buf, got.value)
            if remaining is not None:
                data = data[:remaining]
                remaining -= len(data)
            yield data
            if got.value < want:
                return
    finally:
        _VirtualFree(buf, 0, 0x8000)
        _CloseHandle(handle)


def sha256_unbuffered(path: str) -> str:
    digest = hashlib.sha256()
    for block in _read_blocks(path, 0, None):
        digest.update(block)
    return digest.hexdigest()


def read_unbuffered(path: str, offset: int, length: int) -> bytes:
    start = offset - offset % _ALIGN
    lead = offset - start
    data = b"".join(_read_blocks(path, start, lead + length))
    return data[lead:lead + length]


def reads_unbuffered(path: str) -> bool:
    """True when ``path`` (and so its volume) can be read past the Windows file cache.

    Such a read first makes Windows write any cached changes of the file to the drive
    (the file system keeps cached and uncached access coherent), so verifying a file
    this way also puts its data on the destination drive.
    """
    try:
        handle = _open(path, True)
    except OSError:
        return False
    buf = _VirtualAlloc(None, _ALIGN, 0x3000, 0x04)
    try:
        got = wintypes.DWORD(0)
        return bool(buf) and bool(_ReadFile(handle, buf, _ALIGN, ctypes.byref(got), None))
    finally:
        if buf:
            _VirtualFree(buf, 0, 0x8000)
        _CloseHandle(handle)
