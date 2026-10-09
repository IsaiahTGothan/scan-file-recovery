"""Identify the filesystem at an offset and open it."""

from __future__ import annotations

from dataclasses import dataclass

from ..events import EventBus, JobControl
from ..rescue.reader import RescueReader
from .exfat import ExfatBoot, ExfatVolume
from .fat import FatBoot, FatVolume
from .model import Volume
from .ntfs import NtfsBoot, NtfsVolume


@dataclass
class Probe:
    kind: str                       # "NTFS", "FAT", "exFAT", or an unsupported name, or ""
    supported: bool
    boot: object | None = None
    used_backup: bool = False
    note: str = ""


UNSUPPORTED_NOTES = {
    "BitLocker": "BitLocker-encrypted. Unlock it in Windows, then choose its drive letter as the source.",
    "ReFS": "ReFS is not supported. Use Deep Scan to recover files by signature.",
    "HFS+": "Mac HFS+ is not supported yet. Use Deep Scan to recover files by signature.",
    "APFS": "Mac APFS is not supported yet. Use Deep Scan to recover files by signature.",
    "ext": "Linux ext2/3/4 is not supported. Use Deep Scan to recover files by signature.",
    "LUKS": "Linux encrypted (LUKS) volume; it cannot be read without unlocking it.",
    "swap": "Linux swap space (contains no files).",
}


def _identify_other(head: bytes) -> str:
    if head[3:7] == b"ReFS":
        return "ReFS"
    if len(head) >= 1026 and head[1024:1026] in (b"H+", b"HX"):
        return "HFS+"
    if len(head) >= 36 and head[32:36] == b"NXSB":
        return "APFS"
    if len(head) >= 1082 and head[1080:1082] == b"\x53\xef":
        return "ext"
    if head[:6] == b"LUKS\xba\xbe":
        return "LUKS"
    if len(head) >= 4096 and head[4086:4096] in (b"SWAPSPACE2", b"SWAP-SPACE"):
        return "swap"
    return ""


def probe(reader: RescueReader, offset: int, size: int | None = None) -> Probe:
    head_len = min(8192, max(0, reader.size - offset))
    if head_len < 512:
        return Probe("", False, note="Too small to hold a filesystem.")
    head = bytes(reader.read_critical(offset, head_len).data)
    sector = head[:512]
    if sector[3:11] == b"-FVE-FS-" or (sector[3:11] == b"MSWIN4.1" and b"-FVE-FS-" in sector):
        return Probe("BitLocker", False, note=UNSUPPORTED_NOTES["BitLocker"])
    boot = NtfsBoot.parse(sector)
    if boot is not None:
        return Probe("NTFS", True, boot)
    exboot = ExfatBoot.parse(sector)
    if exboot is not None:
        return Probe("exFAT", True, exboot)
    fboot = FatBoot.parse(sector)
    if fboot is not None:
        return Probe("FAT", True, fboot)
    other = _identify_other(head)
    if other:
        return Probe(other, False, note=UNSUPPORTED_NOTES.get(other, ""))
    # Backups of damaged boot sectors.
    if size:
        for ss in (512, 4096):
            tail_off = offset + size - ss
            if tail_off <= offset or tail_off + 512 > reader.size:
                continue
            tail = bytes(reader.read_critical(tail_off, max(512, reader.sector_size)).data)[:512]
            nboot = NtfsBoot.parse(tail)
            if nboot is not None and nboot.volume_size <= size + ss:
                return Probe("NTFS", True, nboot, used_backup=True)
    for ss in (512, 4096):
        for sector_no, parser, kind in ((12, ExfatBoot.parse, "exFAT"), (6, FatBoot.parse, "FAT")):
            backup_off = offset + sector_no * ss
            if backup_off + 512 > reader.size:
                continue
            data = bytes(reader.read_critical(backup_off, max(512, reader.sector_size)).data)[:512]
            parsed = parser(data)
            if parsed is None:
                continue
            if kind == "FAT":
                assert isinstance(parsed, FatBoot)
                if parsed.fat_type != 32 or parsed.bytes_per_sector != ss or parsed.backup_boot != sector_no:
                    continue
            elif kind == "exFAT":
                assert isinstance(parsed, ExfatBoot)
                if parsed.bytes_per_sector != ss:
                    continue
            return Probe(kind, True, parsed, used_backup=True)
    return Probe("", False, note="No known filesystem was found here.")


def mount(reader: RescueReader, offset: int, found: Probe, events: EventBus | None = None,
          control: JobControl | None = None, metadata_retry_seconds: float = 180.0) -> Volume | None:
    if not found.supported or found.boot is None:
        return None
    if found.kind == "NTFS":
        assert isinstance(found.boot, NtfsBoot)
        vol: Volume = NtfsVolume(reader, offset, found.boot, events, control, metadata_retry_seconds)
        if found.used_backup:
            vol.warnings.append("The NTFS boot sector is damaged; using its backup copy.")
            if events is not None:
                events.warning("The NTFS boot sector is damaged; using its backup copy.", code="LB-203")
        return vol
    if found.kind == "exFAT":
        assert isinstance(found.boot, ExfatBoot)
        return ExfatVolume(reader, offset, found.boot, events, control, found.used_backup)
    if found.kind == "FAT":
        assert isinstance(found.boot, FatBoot)
        return FatVolume(reader, offset, found.boot, events, control, found.used_backup)
    return None
