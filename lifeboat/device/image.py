"""Disk image files (raw/dd/img/bin/iso, split .001 images, fixed VHD)."""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from ..errors import DeviceGoneError, DeviceOpenError, ReadError, ReadErrorKind
from .base import BlockDevice, DeviceInfo

IMAGE_EXTENSIONS = (".img", ".dd", ".raw", ".bin", ".iso", ".ima", ".001", ".vhd", ".dsk", ".image")


@dataclass
class _Segment:
    start: int
    length: int
    path: Path


def _split_segments(first: Path) -> list[Path]:
    """Return [x.001, x.002, ...] if ``first`` is the first part of a split image."""
    match = re.fullmatch(r"(.*)\.(\d{3})", first.name)
    if not match or match.group(2) != "001":
        return [first]
    parts = [first]
    index = 2
    while True:
        candidate = first.with_name(f"{match.group(1)}.{index:03d}")
        if not candidate.is_file():
            break
        parts.append(candidate)
        index += 1
    return parts


def _vhd_footer_kind(path: Path, size: int) -> str | None:
    """Return 'fixed' for a fixed VHD, 'dynamic' for dynamic/differencing, None otherwise."""
    if size < 512:
        return None
    with open(path, "rb") as fh:
        head = fh.read(8)
        fh.seek(size - 512)
        footer = fh.read(512)
    if footer[:8] == b"conectix":
        disk_type = int.from_bytes(footer[60:64], "big")
        return "fixed" if disk_type == 2 else "dynamic"
    if head == b"conectix":
        return "dynamic"
    return None


class ImageDevice(BlockDevice):
    """A raw disk image on a healthy drive.

    ``unreadable`` lists byte ranges that are known to be missing from the
    image (for instance areas a ddrescue-style imager could not read).  Reads
    touching them fail like bad sectors would, so recovered files are
    reported as damaged instead of silently containing zeros.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        sector_size: int = 512,
        unreadable: list[tuple[int, int]] | None = None,
    ) -> None:
        first = Path(path)
        if not first.is_file():
            raise DeviceOpenError(f"Image file not found: {first}")
        with open(first, "rb") as probe:
            magic = probe.read(8)
        if magic == b"vhdxfile":
            raise DeviceOpenError(
                "VHDX images are not supported. Convert the image to a raw (.img) "
                "or fixed-size VHD first."
            )
        parts = _split_segments(first)
        self._segments: list[_Segment] = []
        offset = 0
        for part in parts:
            length = part.stat().st_size
            self._segments.append(_Segment(offset, length, part))
            offset += length
        total = offset
        notes: list[str] = []
        if len(parts) == 1:
            vhd = _vhd_footer_kind(first, total)
            if vhd == "dynamic":
                raise DeviceOpenError(
                    "Dynamic VHD images are not supported. Convert it to a fixed-size VHD "
                    "or raw image first."
                )
            if vhd == "fixed":
                total -= 512
                self._segments[0].length = total
                notes.append("Fixed-size VHD")
        else:
            notes.append(f"Split image in {len(parts)} parts")
        if sector_size not in (512, 1024, 2048, 4096):
            raise DeviceOpenError(f"Unsupported sector size {sector_size}")
        usable = total - (total % sector_size)
        self.info = DeviceInfo(
            path=str(first),
            kind="image",
            size=usable,
            sector_size=sector_size,
            physical_sector_size=sector_size,
            model="Disk image",
            notes=notes,
        )
        self._handles: dict[int, object] = {}
        self._lock = threading.Lock()
        self._unreadable = sorted(unreadable or [])
        self._closed = False
        try:
            for index, segment in enumerate(self._segments):
                self._handles[index] = open(segment.path, "rb", buffering=0)  # noqa: SIM115
        except OSError as exc:
            self.close()
            raise DeviceOpenError(f"Cannot open image: {exc}") from exc

    def _overlaps_unreadable(self, start: int, end: int) -> tuple[int, int] | None:
        for bad_start, bad_end in self._unreadable:
            if bad_start >= end:
                break
            if bad_end > start:
                return bad_start, bad_end
        return None

    def read_raw(self, offset: int, length: int, timeout: float | None = None) -> bytes:
        self._check_aligned(offset, length)
        if self._closed:
            raise DeviceGoneError("Image file was closed")
        end = offset + length
        if end > self.size:
            raise ReadError(
                "Read past end of image", kind=ReadErrorKind.OUT_OF_RANGE, offset=offset, length=length
            )
        hit = self._overlaps_unreadable(offset, end)
        if hit is not None:
            raise ReadError(
                "Area was not recovered into this image",
                kind=ReadErrorKind.MEDIA,
                offset=max(offset, hit[0]),
                length=min(end, hit[1]) - max(offset, hit[0]),
            )
        pieces: list[bytes] = []
        pos = offset
        with self._lock:
            for index, segment in enumerate(self._segments):
                seg_end = segment.start + segment.length
                if seg_end <= pos or segment.start >= end:
                    continue
                local = pos - segment.start
                want = min(end, seg_end) - pos
                handle = self._handles[index]
                try:
                    handle.seek(local)  # type: ignore[attr-defined]
                    data = handle.read(want)  # type: ignore[attr-defined]
                except OSError as exc:
                    raise ReadError(
                        f"Image read failed: {exc}",
                        kind=ReadErrorKind.MEDIA,
                        offset=pos,
                        length=want,
                        os_code=exc.errno,
                    ) from exc
                if data is None or len(data) != want:
                    raise ReadError(
                        "Image file is shorter than expected",
                        kind=ReadErrorKind.OUT_OF_RANGE,
                        offset=pos,
                        length=want,
                    )
                pieces.append(data)
                pos += want
                if pos >= end:
                    break
        return pieces[0] if len(pieces) == 1 else b"".join(pieces)

    def is_present(self) -> bool:
        return not self._closed and all(seg.path.exists() for seg in self._segments)

    def close(self) -> None:
        self._closed = True
        for handle in self._handles.values():
            try:
                handle.close()  # type: ignore[attr-defined]
            except OSError:
                pass
        self._handles.clear()


class MemoryDevice(BlockDevice):
    """In-memory device, used by tests and for small synthetic images."""

    def __init__(self, data: bytes | bytearray, sector_size: int = 512, name: str = "memory") -> None:
        if len(data) % sector_size:
            data = bytes(data) + bytes(sector_size - len(data) % sector_size)
        self._data = bytes(data)
        self.info = DeviceInfo(path=name, kind="image", size=len(self._data),
                               sector_size=sector_size, physical_sector_size=sector_size,
                               model="Memory image")

    def read_raw(self, offset: int, length: int, timeout: float | None = None) -> bytes:
        self._check_aligned(offset, length)
        if offset + length > self.size:
            raise ReadError("Read past end", kind=ReadErrorKind.OUT_OF_RANGE, offset=offset, length=length)
        return self._data[offset:offset + length]
