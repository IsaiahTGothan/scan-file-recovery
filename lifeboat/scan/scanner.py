"""Scan orchestration: partitions -> filesystems -> one browsable tree.

* Quick scan reads the partition table and every supported filesystem.
* Deep scan reads the whole drive once, front to back (the gentlest access
  pattern for a failing drive), looking for lost partitions (boot sectors
  and their backups) and for files by signature.

A problem in one partition never stops the scan of the others: it is
reported and the scan goes on.
"""

from __future__ import annotations

import logging
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import TypeVar

from ..device.base import DeviceInfo
from ..errors import (
    E_BITLOCKER,
    E_FS_REGION_OUTSIDE,
    E_FS_UNSUPPORTED,
    E_INTERNAL,
    E_NO_PARTITIONS,
    Cancelled,
    DeviceGoneError,
    DeviceHungError,
)
from ..events import EventBus, InterventionHandler, JobControl, Progress, RateMeter
from ..fs.carving import Carver
from ..fs.detect import Probe, mount, probe
from ..fs.exfat import ExfatBoot
from ..fs.fat import FatBoot
from ..fs.model import F, Node, Volume
from ..fs.ntfs import NtfsBoot
from ..fs.partitions import PartitionEntry, PartitionTable, read_partition_table
from ..rescue.reader import ReadMode, RescueReader
from ..resilience import run_with_device_retry
from ..util import format_size

log = logging.getLogger("lifeboat.scan")
T = TypeVar("T")


@dataclass
class VolumeResult:
    title: str
    offset: int
    size: int
    probe: Probe
    entry: PartitionEntry | None = None
    volume: Volume | None = None
    root: Node | None = None
    error: str = ""
    found_by_deep_scan: bool = False


@dataclass
class ScanResult:
    info: DeviceInfo
    root: Node
    table: PartitionTable | None = None
    volumes: list[VolumeResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    deep: bool = False
    carved: int = 0
    seconds: float = 0.0

    def counts(self) -> tuple[int, int, int]:
        """(files, deleted files, total bytes) over the whole tree."""
        files = deleted = size = 0
        for node in self.root.walk():
            if node.flags & F.DIR:
                continue
            files += 1
            size += node.size
            if node.flags & F.DELETED:
                deleted += 1
        return files, deleted, size


@dataclass
class ScanOptions:
    find_partitions: bool = True
    carve: bool = True
    carve_groups: set[str] | None = None
    metadata_retry_seconds: float = 180.0


ProgressFn = Callable[[Progress], None]


class Scanner:
    def __init__(
        self,
        reader: RescueReader,
        info: DeviceInfo,
        events: EventBus | None = None,
        control: JobControl | None = None,
        progress: ProgressFn | None = None,
        interventions: InterventionHandler | None = None,
        options: ScanOptions | None = None,
    ) -> None:
        self.reader = reader
        self.info = info
        self.events = events or EventBus()
        self.control = control or JobControl()
        self.progress = progress or (lambda _p: None)
        self.interventions = interventions or InterventionHandler()
        self.options = options or ScanOptions()

    # ------------------------------------------------------------------ helpers
    def _retry(self, action: Callable[[], T], doing: str) -> T:
        return run_with_device_retry(action, self.reader, self.interventions, self.events, self.control, doing)

    @staticmethod
    def _volume_title(index_label: str, vol: Volume | None, found: Probe, size: int) -> str:
        """Short, folder-friendly name such as "Partition 2 - Data (NTFS)"."""
        if vol is not None:
            label = f" - {vol.label}" if vol.label else ""
            return f"{index_label}{label} ({vol.kind})"
        kind = found.kind or "unknown filesystem"
        return f"{index_label} ({kind}, not readable)"

    def _load_volume(self, result: VolumeResult) -> None:
        found = result.probe
        try:
            vol = mount(self.reader, result.offset, found, self.events, self.control,
                        self.options.metadata_retry_seconds)
            if vol is None:
                return
            root = vol.load(self.progress)  # type: ignore[attr-defined]
        except (Cancelled, DeviceGoneError, DeviceHungError):
            raise
        except Exception as exc:  # noqa: BLE001 - a corrupt partition must not stop the scan
            log.error("Failed to read %s: %s\n%s", result.title, exc, traceback.format_exc())
            result.error = f"Could not read this filesystem: {exc}"
            self.events.error(f"{result.title}: {result.error}", code=E_INTERNAL,
                              details=traceback.format_exc(limit=6))
            return
        result.volume = vol
        result.root = root
        root.name = self._volume_title(result.title, vol, found, vol.size)

    # --------------------------------------------------------------- quick scan
    def quick_scan(self) -> ScanResult:
        started = time.monotonic()
        root = Node(self.info.title, F.DIR | F.VIRTUAL)
        result = ScanResult(self.info, root)
        self.progress(Progress("Reading partition table"))
        table = self._retry(partial(read_partition_table, self.reader), "reading the partition table")
        result.table = table
        for warning in table.warnings:
            result.warnings.append(warning)
            self.events.warning(warning, source="scan")
        entries = list(table.entries)
        if not entries:
            # No table at all: the drive may hold a filesystem directly whose
            # boot sector is damaged; probing also checks the backup copies.
            whole = PartitionEntry(index=1, start=0, size=self.reader.size, scheme="None", type_code="",
                                   type_name="Whole drive", sector_size=self.reader.sector_size)
            found = self._retry(partial(probe, self.reader, 0, self.reader.size), "identifying the drive")
            if found.supported:
                entries = [whole]
            else:
                self.events.warning("No partitions or filesystems were found on this drive. "
                                    "Run a Deep Scan to search for lost partitions and files.",
                                    code=E_NO_PARTITIONS)
        for entry in entries:
            self.control.check()
            if entry.scheme == "None":
                title = "Whole drive"
            else:
                title = f"Partition {entry.index}"
            if entry.end > self.reader.size:
                self.events.warning(f"{title} extends past the end of the drive.", code=E_FS_REGION_OUTSIDE)
            size = max(0, min(entry.size, self.reader.size - entry.start))
            found = self._retry(partial(probe, self.reader, entry.start, size), f"identifying {title}")
            vr = VolumeResult(title, entry.start, size, found, entry)
            if found.supported:
                self.progress(Progress(f"Reading {title} ({found.kind})"))
                self._retry(partial(self._load_volume, vr), f"reading {title}")
            elif entry.type_name in ("EFI system", "Microsoft reserved", "BIOS boot") and not found.kind:
                continue
            else:
                note = found.note or "No readable filesystem was found."
                code = E_BITLOCKER if found.kind == "BitLocker" else E_FS_UNSUPPORTED
                self.events.warning(f"{title}: {note}", code=code)
                vr.error = note
            if vr.root is None:
                vr.root = Node(self._volume_title(title, None, found, size),
                               F.DIR | F.VIRTUAL | F.VOLUME | F.UNSUPPORTED)
            result.volumes.append(vr)
            root.add(vr.root)
        result.seconds = time.monotonic() - started
        self.reader.flush_reports()
        files, deleted, total = result.counts()
        self.events.success(
            f"Scan finished: {files:,} files ({deleted:,} deleted), {format_size(total)}, "
            f"in {result.seconds:.0f} s.")
        return result

    # ---------------------------------------------------------------- deep scan
    def deep_scan(self, previous: ScanResult | None = None) -> ScanResult:
        started = time.monotonic()
        if previous is None:
            previous = self.quick_scan()
        result = previous
        result.deep = True
        known = {vr.offset for vr in result.volumes if vr.volume is not None}
        candidates: dict[int, Probe] = {}
        carver = Carver(self.reader, self.options.carve_groups) if self.options.carve else None
        size = self.reader.size
        chunk = 4 << 20
        pos = 0
        meter = RateMeter()
        last = 0.0
        self.reader.reset_skipping()
        while pos < size:
            self.control.check()
            length = min(chunk, size - pos)
            outcome = self._retry(partial(self.reader.read, pos, length, ReadMode.FAST), "searching the drive")
            data = bytes(outcome.data)
            if self.options.find_partitions:
                self._find_boot_sectors(pos, data, candidates)
            if carver is not None:
                self._retry(partial(carver.feed, pos, data), "searching for files")
            pos += length
            now = time.monotonic()
            if now - last > 0.3:
                last = now
                rate = meter.update(pos)
                self.progress(Progress(
                    "Deep scan: searching the whole drive", pos, size, rate=rate, eta=meter.eta(pos, size),
                    item=f"{carver.count if carver else 0:,} files found by signature, "
                         f"{len(candidates)} partition candidates",
                ))
        if self.options.find_partitions:
            self._add_found_partitions(result, candidates, known)
        if carver is not None and carver.count:
            result.root.add(carver.root)
            result.carved = carver.count
            carver.root.name = f"Files found by signature ({carver.count:,})"
            result.volumes.append(VolumeResult("Files found by signature", 0, size,
                                               Probe("Carved", True), volume=carver.volume,
                                               root=carver.root, found_by_deep_scan=True))
        result.seconds += time.monotonic() - started
        self.reader.flush_reports()
        self.events.success(
            f"Deep scan finished: {result.carved:,} files found by signature, "
            f"{sum(1 for v in result.volumes if v.found_by_deep_scan and v.volume and v.volume.kind != 'Carved')} "
            "lost partition(s).")
        return result

    def _find_boot_sectors(self, base: int, data: bytes, out: dict[int, Probe]) -> None:
        for magic, at in ((b"NTFS    ", 3), (b"EXFAT   ", 3), (b"FAT32   ", 82), (b"FAT16   ", 54),
                          (b"FAT12   ", 54)):
            index = data.find(magic)
            while index >= 0:
                start = index - at
                if start >= 0 and (base + start) % 512 == 0:
                    sector = data[start:start + 512]
                    self._consider_boot(base + start, sector, out)
                index = data.find(magic, index + 1)

    def _consider_boot(self, offset: int, sector: bytes, out: dict[int, Probe]) -> None:
        boot = NtfsBoot.parse(sector)
        if boot is not None:
            for start in (offset, offset - boot.total_sectors * boot.bytes_per_sector):
                if start >= 0 and start not in out and self._ntfs_valid(start, boot):
                    out[start] = Probe("NTFS", True, boot, used_backup=start != offset)
            return
        ex = ExfatBoot.parse(sector)
        if ex is not None:
            for start, backup in ((offset, False), (offset - 12 * ex.bytes_per_sector, True)):
                if start >= 0 and start not in out and self._exfat_valid(start, ex):
                    out[start] = Probe("exFAT", True, ex, used_backup=backup)
            return
        fb = FatBoot.parse(sector)
        if fb is not None:
            starts = [(offset, False)]
            if fb.fat_type == 32 and fb.backup_boot:
                starts.append((offset - fb.backup_boot * fb.bytes_per_sector, True))
            for start, backup in starts:
                if start >= 0 and start not in out and self._fat_valid(start, fb):
                    out[start] = Probe("FAT", True, fb, used_backup=backup)

    def _ntfs_valid(self, start: int, boot: NtfsBoot) -> bool:
        mft = start + boot.mft_lcn * boot.cluster_size
        if mft + boot.record_size > self.reader.size:
            return False
        return bytes(self.reader.read(mft, 512).data)[:4] == b"FILE"

    def _exfat_valid(self, start: int, boot: ExfatBoot) -> bool:
        root = start + (boot.heap_offset << boot.bps_shift) + (boot.root_cluster - 2) * boot.cluster_size
        if root + 512 > self.reader.size:
            return False
        head = bytes(self.reader.read(root, 512).data)
        types = {head[i] for i in range(0, 512, 32)}
        return bool(types & {0x81, 0x82, 0x83})

    def _fat_valid(self, start: int, boot: FatBoot) -> bool:
        fat = start + boot.reserved * boot.bytes_per_sector
        if fat + 8 > self.reader.size:
            return False
        head = bytes(self.reader.read(fat, 512).data)
        return head[0] >= 0xF0 and head[1] == 0xFF and head[2] == 0xFF

    def _add_found_partitions(self, result: ScanResult, candidates: dict[int, Probe], known: set[int]) -> None:
        for start in sorted(candidates):
            if start in known:
                continue
            found = candidates[start]
            boot = found.boot
            if isinstance(boot, NtfsBoot | ExfatBoot | FatBoot):
                size = boot.volume_size
            else:
                continue
            # Skip filesystems nested inside a volume we already read (e.g. VHD files).
            inside = any(vr.volume is not None and vr.offset < start < vr.offset + vr.size
                         and not vr.found_by_deep_scan for vr in result.volumes)
            title = f"Lost partition at {format_size(start)}" + (" (inside another volume)" if inside else "")
            vr = VolumeResult(title, start, size, found, found_by_deep_scan=True)
            self.events.info(f"Deep scan found a {found.kind} filesystem at byte {start:,}.")
            self._retry(partial(self._load_volume, vr), f"reading the {found.kind} found at {start:,}")
            if vr.root is not None:
                result.volumes.append(vr)
                result.root.add(vr.root)
                known.add(start)
