"""Sector-by-sector imaging of a failing drive (ddrescue-style).

Professional practice for a dying drive is to copy it *once*, in the order
that gets the most data off soonest, and then work from the copy.  The
image is written next to a GNU ddrescue compatible mapfile recording which
areas are good, failed or bad, so:

* imaging can be stopped and resumed at any time;
* scanning and recovering from the image never touches the failing drive;
* files recovered from the image are still reported as damaged where the
  drive could not be read.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..device.base import DeviceInfo
from ..errors import E_DEST_FAT32, E_DEST_FULL, E_DEST_GONE, E_DEST_ON_SOURCE, E_INTERNAL, Cancelled, LifeboatError
from ..events import Choice, EventBus, Intervention, InterventionHandler, JobControl, Progress, RateMeter, Throttle
from ..recover.destination import (
    disks_for_path,
    disks_for_source,
    free_space,
    is_disk_full,
    is_gone,
    long_path,
    make_dirs,
    volume_filesystem,
)
from ..recover.preflight import FAT32_LIMIT, FAT_NAMES
from ..rescue.reader import MODE_LABELS, ReadMode, RescueReader
from ..rescue.sectormap import SectorMap, State
from ..resilience import run_with_device_retry
from ..util import format_size

log = logging.getLogger("lifeboat.imaging")

PASSES = {
    "quick": [ReadMode.FAST],
    "standard": [ReadMode.FAST, ReadMode.SWEEP, ReadMode.TRIM, ReadMode.SCRAPE],
    "maximum": [ReadMode.FAST, ReadMode.SWEEP, ReadMode.TRIM, ReadMode.SCRAPE, ReadMode.RETRY, ReadMode.RETRY,
                ReadMode.RETRY],
}
STEP = 4 << 20


@dataclass
class ImagingOptions:
    output: str
    mapfile: str = ""
    thoroughness: str = "standard"

    def map_path(self) -> str:
        return self.mapfile or self.output + ".map"


@dataclass
class ImagingSummary:
    output: str
    mapfile: str
    size: int
    good: int
    bad: int
    untried: int
    seconds: float
    cancelled: bool = False
    finished_early: bool = False
    error: str = ""

    @property
    def percent(self) -> float:
        return 100.0 * self.good / self.size if self.size else 0.0

    @property
    def outcome(self) -> str:
        if self.error:
            return "failed"
        if self.good == self.size:
            return "success"
        return "warning"


class ImagingError(LifeboatError):
    pass


def check_image_destination(output: str, source: DeviceInfo, size: int) -> list[tuple[str, str]]:
    """Return blocking problems as (code, message)."""
    problems: list[tuple[str, str]] = []
    folder = os.path.dirname(os.path.abspath(output)) or "."
    probe = folder
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    src = disks_for_source(source.path, source.kind, source.disk_number)
    if src and src & disks_for_path(probe):
        problems.append((E_DEST_ON_SOURCE, "The image would be written to the drive being imaged. "
                                           "Choose a different drive."))
        return problems
    fs = volume_filesystem(probe).lower()
    if fs in FAT_NAMES and size > FAT32_LIMIT:
        problems.append((E_DEST_FAT32, "The destination uses FAT32, which cannot hold files of 4 GB or more."))
    existing = os.path.getsize(long_path(output)) if os.path.exists(long_path(output)) else 0
    free = free_space(probe)
    if free >= 0 and free + existing < size:
        problems.append((E_DEST_FULL, f"The destination has {format_size(free)} free; the image needs "
                                      f"{format_size(size)}."))
    return problems


def _set_sparse(fh: object) -> None:
    """Mark the image sparse on NTFS so unread areas take no space and no time."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        handle = msvcrt.get_osfhandle(fh.fileno())  # type: ignore[attr-defined]
        returned = wintypes.DWORD(0)
        ctypes.windll.kernel32.DeviceIoControl(wintypes.HANDLE(handle), 0x000900C4, None, 0, None, 0,
                                               ctypes.byref(returned), None)
    except Exception:
        log.debug("could not mark image sparse", exc_info=True)


class ImagingJob:
    def __init__(
        self,
        reader: RescueReader,
        source: DeviceInfo,
        options: ImagingOptions,
        events: EventBus | None = None,
        control: JobControl | None = None,
        progress: Callable[[Progress], None] | None = None,
        interventions: InterventionHandler | None = None,
    ) -> None:
        self.reader = reader
        self.source = source
        self.options = options
        self.events = events or EventBus()
        self.control = control or JobControl()
        self.progress = progress or (lambda _p: None)
        self.interventions = interventions or InterventionHandler()
        self._finish_early = False
        self._meter = RateMeter()
        self._throttle = Throttle(0.3)
        self._saved = time.monotonic()
        self._pass_index = 0
        self._pass_count = 0
        self._phase = ""
        self._fh: Any = None

    def finish_early(self) -> None:
        self._finish_early = True

    # ------------------------------------------------------------------ helpers
    def _report(self, position: int, force: bool = False) -> None:
        if not self._throttle.ready(force):
            return
        totals = self.reader.map.totals()
        good = totals[State.GOOD]
        rate = self._meter.update(self.reader.stats.bytes_ok)
        remaining = self.reader.size - good
        self.progress(Progress(
            phase=self._phase, done=good, total=self.reader.size, rate=rate,
            eta=remaining / rate if rate > 1 and self._pass_index == 1 else None,
            item=f"Position {format_size(position)} - {format_size(totals[State.BAD])} bad",
            errors=self.reader.stats.errors, bad_bytes=totals[State.BAD],
            pass_index=self._pass_index, pass_count=self._pass_count,
        ))

    def _save_map(self, force: bool = False, status: str = "?") -> None:
        now = time.monotonic()
        if not force and now - self._saved < 30:
            return
        self._saved = now
        try:
            if self._fh is not None:
                self._fh.flush()  # type: ignore[attr-defined]
                os.fsync(self._fh.fileno())  # type: ignore[attr-defined]
            self.reader.map.save(long_path(self.options.map_path()), status)
        except OSError as exc:
            log.warning("Could not save the mapfile: %s", exc)
            if is_disk_full(exc) or is_gone(exc):
                raise

    def _write(self, outcome: object) -> None:
        fh = self._fh
        data = outcome.data  # type: ignore[attr-defined]
        base = outcome.offset  # type: ignore[attr-defined]
        while True:
            try:
                for start, end in outcome.good:  # type: ignore[attr-defined]
                    fh.seek(start)  # type: ignore[attr-defined]
                    fh.write(data[start - base:end - base])  # type: ignore[attr-defined]
                return
            except OSError as exc:
                self._destination_problem(exc)

    def _destination_problem(self, exc: OSError) -> None:
        if is_disk_full(exc):
            code, title = E_DEST_FULL, "Destination drive is full"
            message = "Free up space on the destination, then press Retry."
        elif is_gone(exc):
            code, title = E_DEST_GONE, "Destination not available"
            message = "Reconnect the destination drive, then press Retry."
        else:
            raise exc
        self.events.critical(title, code=code, details=str(exc))
        choice = self.interventions.request(Intervention(code=code, title=title, message=message,
                                                         options=(Choice.RETRY, Choice.ABORT)))
        if choice is not Choice.RETRY:
            raise Cancelled(title)
        self._reopen_output()

    def _reopen_output(self) -> None:
        try:
            if self._fh is not None:
                self._fh.close()  # type: ignore[attr-defined]
        except OSError:
            pass
        self._fh = open(long_path(self.options.output), "r+b")  # noqa: SIM115

    def _read(self, start: int, length: int, mode: ReadMode) -> object:
        return run_with_device_retry(lambda: self.reader.read(start, length, mode), self.reader,
                                     self.interventions, self.events, self.control, "imaging the drive")

    # --------------------------------------------------------------------- run
    def run(self) -> ImagingSummary:
        started = time.monotonic()
        opts = self.options
        summary = ImagingSummary(opts.output, opts.map_path(), self.reader.size, 0, 0, 0, 0.0)
        try:
            problems = check_image_destination(opts.output, self.source, self.reader.size)
            blocking = [p for p in problems if p[0] in (E_DEST_ON_SOURCE, E_DEST_FAT32)]
            if blocking:
                raise ImagingError(blocking[0][1], code=blocking[0][0])
            for code, message in problems:
                self.events.warning(message, code=code)
            self._open()
            self._passes()
            self._save_map(force=True, status="+")
        except Cancelled as exc:
            summary.cancelled = True
            self.events.warning(f"Imaging stopped: {exc.message}")
        except LifeboatError as exc:
            summary.error = exc.message
            self.events.error(f"Imaging failed: {exc.message}", code=exc.code)
        except Exception as exc:
            log.exception("Imaging failed")
            summary.error = str(exc)
            self.events.critical(f"Imaging stopped by an unexpected error: {exc}", code=E_INTERNAL)
        finally:
            try:
                self._save_map(force=True)
            except OSError:
                pass
            if self._fh is not None:
                try:
                    self._fh.close()  # type: ignore[attr-defined]
                except OSError:
                    pass
            totals = self.reader.map.totals()
            summary.good = totals[State.GOOD]
            summary.bad = totals[State.BAD] + totals[State.FAILED]
            summary.untried = totals[State.UNTRIED] + totals[State.SKIPPED]
            summary.seconds = time.monotonic() - started
            summary.finished_early = self._finish_early
            self.reader.flush_reports()
        line = (f"Imaging finished: {summary.percent:.2f}% rescued "
                f"({format_size(summary.good)} of {format_size(summary.size)}), "
                f"{format_size(summary.bad)} unreadable.")
        if summary.outcome == "success":
            self.events.success(line)
        elif not summary.error:
            self.events.warning(line)
        return summary

    def _open(self) -> None:
        opts = self.options
        make_dirs(os.path.dirname(os.path.abspath(opts.output)) or ".")
        map_path = opts.map_path()
        if os.path.exists(long_path(map_path)):
            previous = SectorMap.load(long_path(map_path), self.reader.size)
            for start, end, state in previous.segments():
                if state != State.UNTRIED:
                    self.reader.map.upgrade(start, end, state)
            self.events.info(
                f"Resuming the image: {format_size(previous.totals()[State.GOOD])} were already rescued.")
        exists = os.path.exists(long_path(opts.output))
        self._fh = open(long_path(opts.output), "r+b" if exists else "w+b")  # noqa: SIM115
        if not exists:
            _set_sparse(self._fh)
        self._fh.truncate(self.reader.size)  # type: ignore[attr-defined]
        self.events.info(f"Imaging {self.source.title} ({format_size(self.reader.size)}) to {opts.output}")

    def _passes(self) -> None:
        passes = PASSES.get(self.options.thoroughness, PASSES["standard"])
        self._pass_count = len(passes)
        wanted = {
            ReadMode.FAST: [State.UNTRIED],
            ReadMode.SWEEP: [State.UNTRIED, State.SKIPPED],
            ReadMode.TRIM: [State.FAILED],
            ReadMode.SCRAPE: [State.FAILED],
            ReadMode.RETRY: [State.BAD],
        }
        self.reader.reset_skipping()
        for number, mode in enumerate(passes, start=1):
            if self._finish_early:
                break
            areas = self.reader.map.ranges(wanted[mode])
            if not areas:
                continue
            self._pass_index = number
            self._phase = f"Pass {number}/{len(passes)}: {MODE_LABELS[mode]}"
            self.events.info(f"{self._phase} ({format_size(sum(e - s for s, e in areas))}).")
            backwards = mode == ReadMode.SWEEP
            ordered = list(reversed(areas)) if backwards else areas
            for start, end in ordered:
                steps = list(range(start, end, STEP))
                if backwards:
                    steps.reverse()
                for pos in steps:
                    self.control.check()
                    if self._finish_early:
                        return
                    length = min(STEP, end - pos)
                    outcome = self._read(pos, length, mode)
                    self._write(outcome)
                    self.reader.map.current_pos = pos
                    self.reader.map.current_pass = number
                    self._report(pos)
                    self._save_map()
            self._report(end if areas else 0, force=True)
