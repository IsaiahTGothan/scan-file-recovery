"""Checks that run before a single byte is written to the destination."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field

from ..device.base import DeviceInfo
from ..errors import E_DEST_FAT32, E_DEST_NOT_WRITABLE, E_DEST_ON_SOURCE, E_DEST_SPACE
from ..util import format_size
from .destination import (
    disks_for_path,
    disks_for_source,
    free_space,
    long_path,
    make_dirs,
    volume_filesystem,
)

FAT32_LIMIT = (4 << 30) - 1
FAT_NAMES = {"fat", "fat32", "vfat", "msdos", "fat16", "fat12"}


@dataclass
class PreflightIssue:
    code: str
    message: str
    blocking: bool


@dataclass
class PreflightReport:
    destination: str
    needed: int
    free: int
    filesystem: str
    too_big_for_fat32: int = 0
    issues: list[PreflightIssue] = field(default_factory=list)

    @property
    def blocking(self) -> list[PreflightIssue]:
        return [i for i in self.issues if i.blocking]

    @property
    def warnings(self) -> list[PreflightIssue]:
        return [i for i in self.issues if not i.blocking]

    @property
    def ok(self) -> bool:
        return not self.blocking

    @property
    def space_short(self) -> bool:
        return any(i.code == E_DEST_SPACE for i in self.issues)

    @property
    def is_fat(self) -> bool:
        return self.filesystem.lower() in FAT_NAMES


def check_destination(destination: str, source: DeviceInfo, needed: int, largest: int = 0,
                      files_over_4g: int = 0, allow_low_space: bool = False) -> PreflightReport:
    dest = os.path.abspath(destination)
    report = PreflightReport(dest, needed, -1, "")
    # 1. Never write to the drive being recovered.
    src_disks = disks_for_source(source.path, source.kind, source.disk_number)
    probe_path = dest
    while not os.path.exists(probe_path):
        parent = os.path.dirname(probe_path)
        if parent == probe_path:
            break
        probe_path = parent
    dest_disks = disks_for_path(probe_path)
    if src_disks and dest_disks and src_disks & dest_disks:
        report.issues.append(PreflightIssue(
            E_DEST_ON_SOURCE,
            "The destination is on the drive you are recovering from. Writing to a failing drive can "
            "destroy the very files you want back. Choose a folder on a different drive.",
            True,
        ))
        return report
    if source.kind == "image" and os.path.abspath(source.path).startswith(dest + os.sep):
        report.issues.append(PreflightIssue(
            E_DEST_ON_SOURCE, "The destination folder contains the disk image being recovered.", False))
    # 2. The folder must be creatable and writable.
    try:
        make_dirs(dest)
        probe_file = os.path.join(long_path(dest), f".lifeboat-write-test-{uuid.uuid4().hex}")
        with open(probe_file, "wb") as fh:
            fh.write(b"lifeboat")
            fh.flush()
            os.fsync(fh.fileno())
        os.remove(probe_file)
    except OSError as exc:
        report.issues.append(PreflightIssue(
            E_DEST_NOT_WRITABLE, f"Lifeboat cannot write to this folder: {exc.strerror or exc}.", True))
        return report
    # 3. Space.
    report.free = free_space(dest)
    report.filesystem = volume_filesystem(dest)
    margin = max(64 << 20, needed // 100)
    if report.free >= 0 and report.free < needed + margin:
        report.issues.append(PreflightIssue(
            E_DEST_SPACE,
            f"The destination has {format_size(report.free)} free but the selected files need "
            f"{format_size(needed)}. Lifeboat will stop and ask you when the drive is full.",
            not allow_low_space,
        ))
    # 4. FAT32 cannot store files of 4 GB or more.
    if report.is_fat and files_over_4g:
        report.too_big_for_fat32 = files_over_4g
        report.issues.append(PreflightIssue(
            E_DEST_FAT32,
            f"The destination uses FAT32, which cannot store files of 4 GB or larger. "
            f"{files_over_4g} selected file(s) would be skipped. Use an NTFS or exFAT drive instead.",
            False,
        ))
    return report
