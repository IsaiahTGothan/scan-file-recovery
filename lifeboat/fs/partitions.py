"""MBR (with extended/logical partitions) and GPT partition tables.

Robustness features:

* GPT headers and entry arrays are CRC-checked; a damaged primary GPT falls
  back to the backup GPT at the end of the disk.
* Drives formatted inside USB enclosures often use 4096-byte sectors while
  a SATA dock shows them with 512-byte sectors (or the other way round).
  The partition start is tested at both sector sizes and the one where a
  filesystem actually is wins.
* A filesystem directly at sector 0 ("superfloppy", common on USB sticks)
  is recognised.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

from ..rescue.reader import RescueReader

MBR_TYPES = {
    0x01: "FAT12", 0x04: "FAT16", 0x05: "Extended", 0x06: "FAT16", 0x07: "NTFS / exFAT",
    0x0B: "FAT32", 0x0C: "FAT32", 0x0E: "FAT16", 0x0F: "Extended", 0x11: "Hidden FAT12",
    0x12: "Recovery (vendor)", 0x14: "Hidden FAT16", 0x16: "Hidden FAT16", 0x17: "Hidden NTFS / exFAT",
    0x1B: "Hidden FAT32", 0x1C: "Hidden FAT32", 0x1E: "Hidden FAT16", 0x27: "Windows recovery",
    0x42: "Windows dynamic disk", 0x82: "Linux swap", 0x83: "Linux", 0x85: "Extended",
    0x8E: "Linux LVM", 0xA5: "FreeBSD", 0xA8: "Apple UFS", 0xAB: "Apple boot", 0xAF: "Apple HFS+",
    0xDE: "Dell utility", 0xEE: "GPT protective", 0xEF: "EFI system", 0xFD: "Linux RAID",
}
EXTENDED_TYPES = {0x05, 0x0F, 0x85}

GPT_TYPES = {
    "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7": "Microsoft basic data",
    "C12A7328-F81F-11D2-BA4B-00A0C93EC93B": "EFI system",
    "E3C9E316-0B5C-4DB8-817D-F92DF00215AE": "Microsoft reserved",
    "DE94BBA4-06D1-4D40-A16A-BFD50179D6AC": "Windows recovery",
    "5808C8AA-7E8F-42E0-85D2-E1E90434CFB3": "Windows LDM metadata",
    "AF9B60A0-1431-4F62-BC68-3311714A69AD": "Windows LDM data",
    "E75CAF8F-F680-4CEE-AFA3-B001E56EFC2D": "Windows Storage Spaces",
    "48465300-0000-11AA-AA11-00306543ECAC": "Apple HFS+",
    "7C3457EF-0000-11AA-AA11-00306543ECAC": "Apple APFS",
    "426F6F74-0000-11AA-AA11-00306543ECAC": "Apple boot",
    "53746F72-6167-11AA-AA11-00306543ECAC": "Apple Core Storage",
    "52637672-7900-11AA-AA11-00306543ECAC": "Apple Recovery HD",
    "0FC63DAF-8483-4772-8E79-3D69D8477DE4": "Linux filesystem",
    "0657FD6D-A4AB-43C4-84E5-0933C84B4F4F": "Linux swap",
    "E6D6D379-F507-44C2-A23C-238F2A3DF928": "Linux LVM",
    "A19D880F-05FC-4D3B-A006-743F0F84911E": "Linux RAID",
    "21686148-6449-6E6F-744E-656564454649": "BIOS boot",
    "516E7CB4-6ECF-11D6-8FF8-00022D09712B": "FreeBSD data",
}
SKIP_GPT_TYPES = {"E3C9E316-0B5C-4DB8-817D-F92DF00215AE", "21686148-6449-6E6F-744E-656564454649"}


def guid_text(raw: bytes) -> str:
    d1, d2, d3 = struct.unpack_from("<IHH", raw, 0)
    return f"{d1:08X}-{d2:04X}-{d3:04X}-{raw[8:10].hex().upper()}-{raw[10:16].hex().upper()}"


@dataclass
class PartitionEntry:
    index: int
    start: int                 # byte offset on the device
    size: int                  # bytes
    scheme: str                # "MBR", "GPT", "APM", "None"
    type_code: str
    type_name: str
    name: str = ""
    active: bool = False
    sector_size: int = 512
    notes: list[str] = field(default_factory=list)

    @property
    def end(self) -> int:
        return self.start + self.size


@dataclass
class PartitionTable:
    scheme: str                # "GPT", "MBR", "None"
    entries: list[PartitionEntry]
    sector_size: int
    warnings: list[str] = field(default_factory=list)
    disk_guid: str = ""


def _looks_like_boot_sector(sector: bytes) -> bool:
    from .exfat import ExfatBoot
    from .fat import FatBoot
    from .ntfs import NtfsBoot

    if len(sector) < 512:
        return False
    if NtfsBoot.parse(sector) or ExfatBoot.parse(sector) or FatBoot.parse(sector):
        return True
    return sector[3:11] in (b"-FVE-FS-", b"ReFS\x00\x00\x00\x00")


APM_TYPES = {
    "Apple_HFS": "Apple HFS+", "Apple_HFSX": "Apple HFS+ (case-sensitive)", "Apple_APFS": "Apple APFS",
    "Apple_Boot": "Apple boot", "Apple_Boot_RAID": "Apple boot", "Apple_UFS": "Apple UFS",
    "Apple_RAID": "Apple RAID", "Apple_CoreStorage": "Apple Core Storage", "DOS_FAT_32": "FAT32",
    "DOS_FAT_16": "FAT16", "DOS_FAT_12": "FAT12", "Windows_FAT_32": "FAT32", "Windows_NTFS": "NTFS / exFAT",
}
APM_SKIP = {"Apple_partition_map", "Apple_Free", "Apple_Void", "Apple_Scratch", "Apple_Extra", "Apple_Patches",
            "Apple_Bootstrap", "Apple_FWDriver", "Apple_Loader"}


def _apple_fs_at_start(head: bytes) -> bool:
    """An HFS+ or APFS volume directly at sector 0 (no partition table)."""
    return head[1024:1026] in (b"H+", b"HX") or (len(head) >= 36 and head[32:36] == b"NXSB")


def read_apm(reader: RescueReader, head: bytes) -> PartitionTable | None:
    """Apple Partition Map: PowerPC-era Macs and many older Mac-formatted drives and disk images."""
    if head[:2] != b"ER":
        return None
    block = struct.unpack_from(">H", head, 2)[0]
    for size in [b for b in (block, 512) if b in (512, 1024, 2048, 4096)]:
        first = bytes(reader.read_critical(size, 512).data) if size + 512 <= reader.size else b""
        if first[:2] != b"PM":
            continue
        count = min(struct.unpack_from(">I", first, 4)[0], 256)
        entries: list[PartitionEntry] = []
        for index in range(1, count + 1):
            raw = first if index == 1 else bytes(reader.read_critical(index * size, 512).data)
            if raw[:2] != b"PM":
                break
            start, blocks = struct.unpack_from(">II", raw, 8)
            name = raw[16:48].split(b"\0", 1)[0].decode("mac_roman", "replace")
            ptype = raw[48:80].split(b"\0", 1)[0].decode("ascii", "replace")
            if ptype in APM_SKIP or ptype.startswith("Apple_Driver") or blocks == 0:
                continue
            entries.append(PartitionEntry(index=index, start=start * size, size=blocks * size, scheme="APM",
                                          type_code=ptype, type_name=APM_TYPES.get(ptype, ptype), name=name,
                                          sector_size=size))
        return PartitionTable("APM", entries, size)
    return None


def _parse_gpt(blob: bytes, header_off: int, ss: int) -> tuple[dict[str, int | bytes], bool] | None:
    header = blob[header_off:header_off + 92]
    if len(header) < 92 or header[:8] != b"EFI PART":
        return None
    hsize = struct.unpack_from("<I", header, 12)[0]
    if hsize < 92 or hsize > ss:
        return None
    raw = bytearray(blob[header_off:header_off + hsize])
    stored_crc = struct.unpack_from("<I", raw, 16)[0]
    raw[16:20] = b"\0\0\0\0"
    crc_ok = (zlib.crc32(raw) & 0xFFFFFFFF) == stored_crc
    current, backup, first_usable, last_usable = struct.unpack_from("<QQQQ", header, 24)
    entries_lba, count, entry_size, entries_crc = struct.unpack_from("<QIII", header, 72)
    if entry_size < 128 or entry_size > 1024 or count > 4096:
        return None
    return ({
        "current": current, "backup": backup, "first": first_usable, "last": last_usable,
        "disk_guid": bytes(header[56:72]), "entries_lba": entries_lba, "count": count,
        "entry_size": entry_size, "entries_crc": entries_crc,
    }, crc_ok)


def _gpt_entries(reader: RescueReader, hdr: dict[str, int | bytes], ss: int, disk_size: int
                 ) -> tuple[list[PartitionEntry], bool]:
    count = int(hdr["count"])  # type: ignore[arg-type]
    esize = int(hdr["entry_size"])  # type: ignore[arg-type]
    offset = int(hdr["entries_lba"]) * ss  # type: ignore[arg-type]
    length = count * esize
    if offset <= 0 or offset + length > disk_size:
        return [], False
    data = bytes(reader.read_critical(offset, length).data)
    crc_ok = (zlib.crc32(data) & 0xFFFFFFFF) == int(hdr["entries_crc"])  # type: ignore[arg-type]
    entries: list[PartitionEntry] = []
    for i in range(count):
        raw = data[i * esize:(i + 1) * esize]
        if raw[:16] == bytes(16):
            continue
        type_guid = guid_text(raw[0:16])
        first, last = struct.unpack_from("<QQ", raw, 32)
        if last < first:
            continue
        name = raw[56:128].decode("utf-16-le", "replace").split("\x00", 1)[0]
        entries.append(PartitionEntry(
            index=i + 1,
            start=first * ss,
            size=(last - first + 1) * ss,
            scheme="GPT",
            type_code=type_guid,
            type_name=GPT_TYPES.get(type_guid, "Unknown"),
            name=name,
            sector_size=ss,
        ))
    return entries, crc_ok


def read_gpt(reader: RescueReader, head: bytes) -> PartitionTable | None:
    disk_size = reader.size
    sizes = [reader.sector_size] + [s for s in (512, 4096) if s != reader.sector_size]
    for ss in sizes:
        parsed = _parse_gpt(head, ss, ss)
        warnings: list[str] = []
        entries: list[PartitionEntry] = []
        ok = False
        if parsed is not None:
            hdr, crc_ok = parsed
            entries, entries_ok = _gpt_entries(reader, hdr, ss, disk_size)
            ok = crc_ok and entries_ok
            if not ok:
                warnings.append("The primary GPT partition table is damaged.")
        if not ok:
            last_lba = disk_size // ss - 1
            tail = bytes(reader.read_critical(last_lba * ss, ss).data)
            backup = _parse_gpt(tail, 0, ss)
            if backup is not None:
                bhdr, bcrc = backup
                bentries, bentries_ok = _gpt_entries(reader, bhdr, ss, disk_size)
                if bcrc and bentries_ok:
                    entries = bentries
                    ok = True
                    warnings.append("Using the backup GPT partition table from the end of the disk.")
                    parsed = backup
            if not ok and parsed is not None and entries:
                warnings.append("GPT checksums do not match; the table was used anyway.")
                ok = True
        if ok and parsed is not None:
            hdr = parsed[0]
            if ss != reader.sector_size:
                warnings.append(
                    f"The partition table uses {ss}-byte sectors but the drive reports "
                    f"{reader.sector_size}-byte sectors (it was probably formatted in a USB enclosure). "
                    "Lifeboat adjusted for this automatically.")
            return PartitionTable("GPT", entries, ss, warnings, guid_text(bytes(hdr["disk_guid"])))  # type: ignore[arg-type]
    return None


def read_mbr(reader: RescueReader, head: bytes) -> PartitionTable | None:
    sector = head[:512]
    if sector[510:512] != b"\x55\xaa":
        return None
    ss = reader.sector_size
    entries: list[PartitionEntry] = []
    warnings: list[str] = []
    raw_entries = []
    for i in range(4):
        e = sector[446 + 16 * i:446 + 16 * (i + 1)]
        status, ptype = e[0], e[4]
        start, count = struct.unpack_from("<II", e, 8)
        if ptype == 0 or count == 0:
            continue
        if status not in (0x00, 0x80):
            return None  # not a partition table (probably boot code of a VBR)
        raw_entries.append((i + 1, status, ptype, start, count))
    if not raw_entries:
        return PartitionTable("MBR", [], ss, ["The MBR contains no partitions."])
    # Decide which sector size the table was written with.
    lba_size = _choose_lba_size(reader, [(s, c) for _i, _st, t, s, c in raw_entries if t not in EXTENDED_TYPES])
    if lba_size != ss:
        warnings.append(
            f"The partition table uses {lba_size}-byte sectors but the drive reports {ss}-byte sectors "
            "(it was probably formatted in a USB enclosure). Lifeboat adjusted for this automatically.")
    disk_end = reader.size
    for index, status, ptype, start, count in raw_entries:
        if ptype in EXTENDED_TYPES:
            entries.extend(_read_ebr_chain(reader, start, lba_size, len(entries) + 5, warnings))
            continue
        entry = PartitionEntry(
            index=index, start=start * lba_size, size=count * lba_size, scheme="MBR",
            type_code=f"0x{ptype:02X}", type_name=MBR_TYPES.get(ptype, "Unknown"),
            active=status == 0x80, sector_size=lba_size,
        )
        if entry.end > disk_end:
            entry.notes.append("Extends past the end of the drive.")
        entries.append(entry)
    return PartitionTable("MBR", entries, lba_size, warnings)


def _choose_lba_size(reader: RescueReader, starts: list[tuple[int, int]]) -> int:
    ss = reader.sector_size
    candidates = [ss] + [s for s in (512, 4096) if s != ss]
    for size in candidates:
        for start, _count in starts:
            offset = start * size
            if offset + 512 > reader.size:
                continue
            if _looks_like_boot_sector(bytes(reader.read_critical(offset, max(512, reader.sector_size)).data)):
                return size
    return ss


def _read_ebr_chain(reader: RescueReader, ext_start: int, ss: int, first_index: int,
                    warnings: list[str]) -> list[PartitionEntry]:
    entries: list[PartitionEntry] = []
    current = ext_start
    seen: set[int] = set()
    index = first_index
    while current and current not in seen and len(entries) < 128:
        seen.add(current)
        offset = current * ss
        if offset + 512 > reader.size:
            warnings.append("An extended partition entry points past the end of the drive.")
            break
        sector = bytes(reader.read_critical(offset, max(512, reader.sector_size)).data)
        if sector[510:512] != b"\x55\xaa":
            warnings.append("A logical partition table (EBR) is damaged; later logical partitions may be missing.")
            break
        e1 = sector[446:462]
        e2 = sector[462:478]
        ptype = e1[4]
        rel, count = struct.unpack_from("<II", e1, 8)
        if ptype and count:
            entries.append(PartitionEntry(
                index=index, start=(current + rel) * ss, size=count * ss, scheme="MBR",
                type_code=f"0x{ptype:02X}", type_name=MBR_TYPES.get(ptype, "Unknown"), sector_size=ss,
            ))
            index += 1
        next_rel = struct.unpack_from("<I", e2, 8)[0]
        if e2[4] not in EXTENDED_TYPES or next_rel == 0:
            break
        current = ext_start + next_rel
    return entries


def read_partition_table(reader: RescueReader) -> PartitionTable:
    head_len = min(reader.size, 64 * 1024)
    head = bytes(reader.read_critical(0, head_len).data)
    gpt = read_gpt(reader, head)
    mbr_sector = head[:512]
    if gpt is not None:
        return gpt
    if _looks_like_boot_sector(mbr_sector) or _apple_fs_at_start(head):
        entry = PartitionEntry(index=1, start=0, size=reader.size, scheme="None",
                               type_code="", type_name="Whole drive (no partition table)",
                               sector_size=reader.sector_size)
        return PartitionTable("None", [entry], reader.sector_size)
    apm = read_apm(reader, head)
    if apm is not None:
        return apm
    mbr = read_mbr(reader, head)
    if mbr is not None:
        protective = [e for e in mbr.entries if e.type_code == "0xEE"]
        if protective:
            mbr.warnings.append(
                "The disk has a protective MBR but no readable GPT. Run a Deep scan to find the partitions.")
            mbr.entries = [e for e in mbr.entries if e.type_code != "0xEE"]
        return mbr
    return PartitionTable("None", [], reader.sector_size, ["No partition table was found."])
