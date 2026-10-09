"""Bad-sector aware reading.

The strategy follows what professional imaging tools do, applied to any
read Lifeboat makes (metadata during a scan, file data during a recovery,
the whole disk while imaging):

1. **FAST** - read large blocks.  On an error, do not retry: mark the block
   FAILED, skip ahead (the skip doubles after each consecutive problem) and
   keep going.  Slow but successful reads also trigger a skip.  This gets
   the healthy data off the drive before it degrades further.
2. **SWEEP** - read the areas that were skipped, in moderate blocks,
   without skipping.  Errors mark blocks FAILED.
3. **TRIM** - split every FAILED block into smaller pieces to localise the
   error, then read the failing pieces unit by unit from both edges inwards
   until the first error on each side.
4. **SCRAPE** - read every remaining unit of FAILED blocks once.
5. **RETRY** - optionally re-read units marked BAD.

Every outcome is recorded in the :class:`SectorMap`, so no area is ever read
again in a pass where that would not help - the main reason older tools
crawl on failing drives.  A "unit" is the drive's physical sector size
(4 KiB on modern drives): reading 512-byte pieces of a bad 4 KiB sector only
multiplies the time spent on errors.
"""

from __future__ import annotations

import enum
import threading
import time
from dataclasses import dataclass, field

from ..device.base import BlockDevice
from ..errors import (
    E_READ_BAD,
    E_READ_TIMEOUT,
    DeviceGoneError,
    DeviceHungError,
    ReadError,
    ReadErrorKind,
)
from ..events import Event, EventBus, JobControl, Level, Throttle
from ..util import align_down, align_up, format_size, merge_ranges
from .cache import PageCache
from .sectormap import SectorMap, State

CACHE_MAX_READ = 256 * 1024


class ReadMode(enum.Enum):
    FAST = "fast"
    SWEEP = "sweep"
    TRIM = "trim"
    SCRAPE = "scrape"
    RETRY = "retry"


MODE_LABELS = {
    ReadMode.FAST: "Copying readable data",
    ReadMode.SWEEP: "Reading skipped areas",
    ReadMode.TRIM: "Trimming damaged areas",
    ReadMode.SCRAPE: "Scraping damaged areas",
    ReadMode.RETRY: "Retrying bad sectors",
}


@dataclass
class ReadPolicy:
    block_size: int = 1 << 20          # largest single device read (FAST)
    sweep_block: int = 128 << 10       # block size when re-reading skipped areas
    timeout: float = 15.0              # seconds before a read is abandoned
    skip_initial: int = 64 << 10       # first skip after an error (FAST)
    skip_max: int = 256 << 20          # largest skip
    slow_seconds: float = 3.0          # successful reads slower than this trigger a skip
    unit: int | None = None            # trim/scrape granularity (default: physical sector)
    max_consecutive_timeouts: int = 4  # then ask the user what to do
    cache_bytes: int = 32 << 20


@dataclass
class ReaderStats:
    reads: int = 0
    bytes_ok: int = 0
    errors: int = 0
    timeouts: int = 0
    slow_reads: int = 0
    skipped_bytes: int = 0
    last_offset: int = 0
    last_error: str = ""
    busy_seconds: float = 0.0


@dataclass
class ReadOutcome:
    """Result of a rescue read.

    ``data`` always has the requested length; areas listed in ``bad`` or
    ``unread`` are zero-filled.  ``unread`` areas may still be recovered by a
    later, more thorough pass; ``bad`` areas could not be read at the
    smallest unit (or lie outside the device).
    """

    offset: int
    data: bytearray
    good: list[tuple[int, int]] = field(default_factory=list)
    bad: list[tuple[int, int]] = field(default_factory=list)
    unread: list[tuple[int, int]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.bad and not self.unread

    @property
    def end(self) -> int:
        return self.offset + len(self.data)


class RescueReader:
    def __init__(
        self,
        device: BlockDevice,
        policy: ReadPolicy | None = None,
        sector_map: SectorMap | None = None,
        events: EventBus | None = None,
        control: JobControl | None = None,
    ) -> None:
        self.device = device
        self.policy = policy or ReadPolicy()
        if sector_map is None:
            sector_map = SectorMap(device.size)
        elif sector_map.size != device.size:
            raise ValueError("sector map does not match the device size")
        self.map = sector_map
        self.events = events
        self.control = control
        self.cache = PageCache(self.policy.cache_bytes)
        self.stats = ReaderStats()
        self._lock = threading.RLock()
        self._skip_from = 0
        self._skip_until = 0
        self._skip_size = self.policy.skip_initial
        self._timeouts_in_row = 0
        self._reported = 0
        self._suppressed = 0
        self._summary_throttle = Throttle(10.0)
        ss = device.sector_size
        unit = self.policy.unit or device.physical_sector_size
        unit = max(ss, align_up(unit, ss))
        self.unit = unit
        self.map.align(ss)

    # ---------------------------------------------------------------- properties
    @property
    def size(self) -> int:
        return self.device.size

    @property
    def sector_size(self) -> int:
        return self.device.sector_size

    # ---------------------------------------------------------------- public API
    def read(self, offset: int, length: int, mode: ReadMode = ReadMode.FAST) -> ReadOutcome:
        """Read ``length`` bytes at ``offset`` using the strategy of ``mode``."""
        outcome = ReadOutcome(offset, bytearray(max(0, length)))
        if length <= 0:
            return outcome
        with self._lock:
            end = offset + length
            dev_end = self.device.size
            if offset >= dev_end:
                outcome.bad.append((offset, end))
                return outcome
            eff_end = min(end, dev_end)
            if end > dev_end:
                outcome.bad.append((dev_end, end))
            ss = self.device.sector_size
            start = align_down(offset, ss)
            stop = align_up(eff_end, ss)
            for seg_start, seg_end, state in self.map.segments(start, stop):
                self._check()
                self._segment(seg_start, seg_end, state, mode, outcome, eff_end)
            outcome.good = merge_ranges(outcome.good)
            outcome.bad = merge_ranges(outcome.bad)
            outcome.unread = merge_ranges(outcome.unread)
            return outcome

    def read_critical(self, offset: int, length: int, max_mode: ReadMode = ReadMode.SCRAPE) -> ReadOutcome:
        """Read a small, important area (boot sector, partition table) as
        thoroughly as needed, escalating through the passes immediately."""
        order = [ReadMode.FAST, ReadMode.SWEEP, ReadMode.TRIM, ReadMode.SCRAPE, ReadMode.RETRY]
        outcome = self.read(offset, length, ReadMode.FAST)
        for mode in order[1:]:
            if outcome.complete:
                break
            again = self.read(offset, length, mode)
            outcome = again
            if mode == max_mode:
                break
        return outcome

    def read_bytes(self, offset: int, length: int, mode: ReadMode = ReadMode.FAST) -> bytes:
        return bytes(self.read(offset, length, mode).data)

    def flush_reports(self) -> None:
        if self._suppressed and self.events is not None:
            self.events.warning(
                f"{self._suppressed} more read problems on the source (their locations are in the Disk map)",
                code=E_READ_BAD,
            )
        self._suppressed = 0

    def reset_skipping(self) -> None:
        self._skip_from = 0
        self._skip_until = 0
        self._skip_size = self.policy.skip_initial

    # ------------------------------------------------------------ segment logic
    def _check(self) -> None:
        if self.control is not None:
            self.control.check()

    def _emit(self, outcome: ReadOutcome, start: int, end: int, kind: str, data: bytes | None = None,
              data_start: int = 0, limit: int | None = None) -> None:
        lo = max(start, outcome.offset)
        hi = min(end, limit if limit is not None else outcome.end, outcome.end)
        if lo >= hi:
            return
        if kind == "good":
            assert data is not None
            outcome.data[lo - outcome.offset:hi - outcome.offset] = data[lo - data_start:hi - data_start]
            outcome.good.append((lo, hi))
        elif kind == "bad":
            outcome.bad.append((lo, hi))
        else:
            outcome.unread.append((lo, hi))

    def _segment(self, start: int, end: int, state: State, mode: ReadMode, outcome: ReadOutcome,
                 limit: int) -> None:
        if state == State.BAD:
            if mode == ReadMode.RETRY:
                self._units(start, end, outcome, limit)
            else:
                self._emit(outcome, start, end, "bad", limit=limit)
        elif state == State.GOOD:
            self._good(start, end, mode, outcome, limit)
        elif state == State.UNTRIED:
            if mode == ReadMode.FAST:
                self._fast(start, end, outcome, limit)
            elif mode == ReadMode.SWEEP:
                self._sweep(start, end, outcome, limit)
            else:
                self._emit(outcome, start, end, "unread", limit=limit)
        elif state == State.SKIPPED:
            if mode == ReadMode.SWEEP:
                self._sweep(start, end, outcome, limit)
            else:
                self._emit(outcome, start, end, "unread", limit=limit)
        elif state == State.FAILED:
            if mode == ReadMode.TRIM:
                self._trim(start, end, outcome, limit)
            elif mode in (ReadMode.SCRAPE, ReadMode.RETRY):
                self._units(start, end, outcome, limit)
            else:
                self._emit(outcome, start, end, "unread", limit=limit)

    def _good(self, start: int, end: int, mode: ReadMode, outcome: ReadOutcome, limit: int) -> None:
        pos = start
        while pos < end:
            self._check()
            chunk_end = min(end, pos + self.policy.block_size)
            try:
                data, _elapsed = self._device_read(pos, chunk_end)
            except ReadError as err:
                # The area read fine earlier but fails now: the drive is degrading.
                self._report(err, pos, chunk_end)
                if mode in (ReadMode.FAST, ReadMode.SWEEP):
                    self.map.set(pos, chunk_end, State.FAILED)
                    self._emit(outcome, pos, chunk_end, "unread", limit=limit)
                else:
                    self.map.set(pos, chunk_end, State.FAILED)
                    self._units(pos, chunk_end, outcome, limit)
                pos = chunk_end
                continue
            self._emit(outcome, pos, chunk_end, "good", data, pos, limit)
            pos = chunk_end

    def _fast(self, start: int, end: int, outcome: ReadOutcome, limit: int) -> None:
        pos = start
        while pos < end:
            self._check()
            if self._skip_from <= pos < self._skip_until:
                zone_end = min(end, self._skip_until)
                self.map.set(pos, zone_end, State.SKIPPED)
                self.stats.skipped_bytes += zone_end - pos
                self._emit(outcome, pos, zone_end, "unread", limit=limit)
                pos = zone_end
                continue
            chunk_end = min(end, pos + self.policy.block_size)
            if pos < self._skip_from < chunk_end:
                chunk_end = self._skip_from
            try:
                data, elapsed = self._device_read(pos, chunk_end)
            except ReadError as err:
                self._report(err, pos, chunk_end)
                if err.kind is ReadErrorKind.OUT_OF_RANGE:
                    self.map.set(pos, chunk_end, State.BAD)
                    self._emit(outcome, pos, chunk_end, "bad", limit=limit)
                else:
                    self.map.set(pos, chunk_end, State.FAILED)
                    self._emit(outcome, pos, chunk_end, "unread", limit=limit)
                    self._start_skip(chunk_end)
                pos = chunk_end
                continue
            self.map.set(pos, chunk_end, State.GOOD)
            self._emit(outcome, pos, chunk_end, "good", data, pos, limit)
            if elapsed > self.policy.slow_seconds:
                self.stats.slow_reads += 1
                self._start_skip(chunk_end)
            else:
                self._skip_size = self.policy.skip_initial
            pos = chunk_end

    def _sweep(self, start: int, end: int, outcome: ReadOutcome, limit: int) -> None:
        block = max(self.unit, min(self.policy.sweep_block, self.policy.block_size))
        pos = start
        while pos < end:
            self._check()
            chunk_end = min(end, align_down(pos, block) + block)
            if chunk_end <= pos:
                chunk_end = min(end, pos + block)
            try:
                data, _elapsed = self._device_read(pos, chunk_end)
            except ReadError as err:
                self._report(err, pos, chunk_end)
                if err.kind is ReadErrorKind.OUT_OF_RANGE:
                    self.map.set(pos, chunk_end, State.BAD)
                    self._emit(outcome, pos, chunk_end, "bad", limit=limit)
                else:
                    self.map.set(pos, chunk_end, State.FAILED)
                    self._emit(outcome, pos, chunk_end, "unread", limit=limit)
                pos = chunk_end
                continue
            self.map.set(pos, chunk_end, State.GOOD)
            self._emit(outcome, pos, chunk_end, "good", data, pos, limit)
            pos = chunk_end

    def _trim(self, start: int, end: int, outcome: ReadOutcome, limit: int) -> None:
        """Localise errors inside a failed block, then trim the pieces that still fail."""
        block = max(self.unit, min(self.policy.sweep_block, self.policy.block_size))
        if end - start <= block:
            self._trim_edges(start, end, outcome, limit)
            return
        failed: list[tuple[int, int]] = []
        pos = start
        while pos < end:
            self._check()
            piece_end = min(end, align_down(pos, block) + block)
            try:
                data, _elapsed = self._device_read(pos, piece_end)
            except ReadError as err:
                self._report(err, pos, piece_end)
                failed.append((pos, piece_end))
            else:
                self.map.set(pos, piece_end, State.GOOD)
                self._emit(outcome, pos, piece_end, "good", data, pos, limit)
            pos = piece_end
        for piece_start, piece_end in failed:
            self._trim_edges(piece_start, piece_end, outcome, limit)

    def _trim_edges(self, start: int, end: int, outcome: ReadOutcome, limit: int) -> None:
        unit = self.unit
        left = start
        while left < end:
            self._check()
            unit_end = min(end, align_up(left + 1, unit))
            data = self._unit_read(left, unit_end)
            if data is None:
                self.map.set(left, unit_end, State.BAD)
                self._emit(outcome, left, unit_end, "bad", limit=limit)
                left = unit_end
                break
            self.map.set(left, unit_end, State.GOOD)
            self._emit(outcome, left, unit_end, "good", data, left, limit)
            left = unit_end
        right = end
        while right > left:
            self._check()
            unit_start = max(left, align_down(right - 1, unit))
            data = self._unit_read(unit_start, right)
            if data is None:
                self.map.set(unit_start, right, State.BAD)
                self._emit(outcome, unit_start, right, "bad", limit=limit)
                right = unit_start
                break
            self.map.set(unit_start, right, State.GOOD)
            self._emit(outcome, unit_start, right, "good", data, unit_start, limit)
            right = unit_start
        if left < right:
            self._emit(outcome, left, right, "unread", limit=limit)

    def _units(self, start: int, end: int, outcome: ReadOutcome, limit: int) -> None:
        unit = self.unit
        pos = start
        while pos < end:
            self._check()
            unit_end = min(end, align_up(pos + 1, unit))
            data = self._unit_read(pos, unit_end)
            if data is None:
                self.map.set(pos, unit_end, State.BAD)
                self._emit(outcome, pos, unit_end, "bad", limit=limit)
            else:
                self.map.set(pos, unit_end, State.GOOD)
                self._emit(outcome, pos, unit_end, "good", data, pos, limit)
            pos = unit_end

    def _unit_read(self, start: int, end: int) -> bytes | None:
        try:
            data, _elapsed = self._device_read(start, end)
            return data
        except ReadError as err:
            self._report(err, start, end)
            return None

    def _start_skip(self, after: int) -> None:
        self._skip_from = after
        self._skip_until = min(self.device.size, after + self._skip_size)
        self._skip_size = min(self._skip_size * 2, self.policy.skip_max)

    # ------------------------------------------------------------- device access
    def _present(self) -> bool:
        try:
            return bool(self.device.is_present())
        except Exception:  # noqa: BLE001 - a probe failure means "not present"
            return False

    def _device_read(self, start: int, end: int) -> tuple[bytes, float]:
        length = end - start
        if length <= CACHE_MAX_READ:
            cached = self.cache.get(start, end)
            if cached is not None:
                return cached, 0.0
        began = time.monotonic()
        self.stats.last_offset = start
        try:
            data = self.device.read_raw(start, length, self.policy.timeout)
        except ReadError as err:
            self.stats.errors += 1
            self.stats.busy_seconds += time.monotonic() - began
            if err.kind in (ReadErrorKind.GONE, ReadErrorKind.OTHER) and not self._present():
                raise DeviceGoneError("The source drive disconnected.") from err
            if err.kind is ReadErrorKind.TIMEOUT:
                self.stats.timeouts += 1
                self._timeouts_in_row += 1
                if self._timeouts_in_row >= self.policy.max_consecutive_timeouts:
                    self._timeouts_in_row = 0
                    if not self._present():
                        raise DeviceGoneError("The source drive disconnected.") from err
                    raise DeviceHungError(
                        f"The source drive did not answer {self.policy.max_consecutive_timeouts} "
                        "reads in a row."
                    ) from err
            else:
                self._timeouts_in_row = 0
            if err.kind is ReadErrorKind.GONE:
                # Reported as gone but still present: treat it as a media error.
                err.kind = ReadErrorKind.MEDIA
            raise
        elapsed = time.monotonic() - began
        self._timeouts_in_row = 0
        self.stats.reads += 1
        self.stats.bytes_ok += length
        self.stats.busy_seconds += elapsed
        if length <= CACHE_MAX_READ:
            self.cache.put(start, data)
        return data, elapsed

    def _report(self, err: ReadError, start: int, end: int) -> None:
        self.stats.last_error = err.message
        if self.events is None:
            return
        ss = self.device.sector_size
        if self._reported < 25:
            self._reported += 1
            kind = "timed out" if err.kind is ReadErrorKind.TIMEOUT else "failed"
            self.events.emit(
                Event(
                    Level.WARNING,
                    f"Read {kind} at sector {start // ss:,} ({format_size(end - start)}): {err.message}",
                    code=E_READ_TIMEOUT if err.kind is ReadErrorKind.TIMEOUT else E_READ_BAD,
                    details=f"Byte offset {start:,}-{end - 1:,}",
                    source="reader",
                )
            )
            if self._reported == 25:
                self.events.info(
                    "Further read errors are summarised every 10 seconds; the Disk map shows them all."
                )
        else:
            self._suppressed += 1
            if self._summary_throttle.ready():
                self.flush_reports()
