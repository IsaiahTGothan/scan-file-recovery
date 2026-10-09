"""Copy selected files to a healthy destination - safely and verifiably.

How a recovery runs
-------------------
1. **Plan** - every file gets a Windows-safe, unique destination name and
   is ordered by its position on the source disk, so the drive reads mostly
   front to back instead of seeking back and forth.
2. **Copy pass** - each file is read once in FAST mode (no retries; damaged
   areas are skipped), written to ``name.lifeboat-part``, flushed to disk,
   verified by reading it back, and only then renamed to its real name.
   Readable files are therefore safe on the destination as early as possible.
3. **Rescue passes** - files that still have unread areas are revisited:
   skipped areas, then trimming and scraping of damaged blocks, then
   (optionally) retries of bad sectors.  Recovered pieces are patched into
   the existing files and verified.
4. **Report** - an HTML and CSV report lists every file with its status,
   damaged byte ranges and SHA-256.

Source disconnects/hangs and a full or vanished destination pause the job
and ask the user; nothing is lost and the job continues where it stopped.
The journal makes a stopped job resumable.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..device.base import DeviceInfo
from ..errors import (
    E_DEST_FAT32,
    E_DEST_FULL,
    E_DEST_GONE,
    E_DEST_WRITE,
    E_FILE_ENCRYPTED,
    E_FILE_LOST,
    E_FILE_OVERWRITTEN,
    E_FILE_PARTIAL,
    E_FILE_UNSUPPORTED,
    E_INTERNAL,
    E_VERIFY,
    Cancelled,
)
from ..events import (
    Choice,
    EventBus,
    Intervention,
    InterventionHandler,
    JobControl,
    Progress,
    RateMeter,
    Throttle,
)
from ..fs.content import BAD, OK, UNREAD, FileContentReader, normalize_states
from ..fs.model import F, FileLayout, Node
from ..rescue.reader import MODE_LABELS, ReadMode, RescueReader
from ..rescue.sectormap import SectorMap, State
from ..resilience import run_with_device_retry
from ..util import format_size
from .destination import is_disk_full, is_gone, long_path, make_dirs, set_times, volume_filesystem
from .journal import META_DIR, SOURCE_MAP, Journal, JournalEntry
from .names import NameSpace, sanitize
from .preflight import FAT32_LIMIT, FAT_NAMES

log = logging.getLogger("lifeboat.recover")

CHUNK = 4 << 20
PART_SUFFIX = ".lifeboat-part"

PASSES = {
    "quick": [ReadMode.FAST],
    "standard": [ReadMode.FAST, ReadMode.SWEEP, ReadMode.TRIM, ReadMode.SCRAPE],
    "maximum": [ReadMode.FAST, ReadMode.SWEEP, ReadMode.TRIM, ReadMode.SCRAPE, ReadMode.RETRY,
                ReadMode.RETRY, ReadMode.RETRY],
}


class Status:
    PENDING = "pending"
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"

    SHORT = {
        PENDING: "Not processed",
        OK: "Recovered",
        PARTIAL: "Damaged",
        FAILED: "Failed",
        SKIPPED: "Not processed",
    }

    LABELS = {
        PENDING: "Not started",
        OK: "Recovered",
        PARTIAL: "Damaged (partly recovered)",
        FAILED: "Failed",
        SKIPPED: "Skipped",
    }


@dataclass
class RecoveryOptions:
    destination: str
    job_folder: bool = True
    verify: bool = True
    thoroughness: str = "standard"
    preserve_times: bool = True
    mark_damaged: bool = False
    resume: bool = False
    job_name: str = ""


@dataclass
class FileTask:
    node: Node
    source_path: str
    rel_path: str
    key: str
    size: int
    order: int = -1
    status: str = Status.PENDING
    states: list[tuple[int, int, int]] = field(default_factory=list)
    sha256: str = ""
    message: str = ""
    code: str = ""
    notes: list[str] = field(default_factory=list)
    dest: str = ""

    def bytes_in(self, state: int) -> int:
        return sum(e - s for s, e, st in self.states if st == state)

    @property
    def ok_bytes(self) -> int:
        return self.bytes_in(OK)

    @property
    def bad_bytes(self) -> int:
        return self.bytes_in(BAD) + self.bytes_in(UNREAD)

    def damaged_ranges(self) -> list[tuple[int, int]]:
        return [(s, e) for s, e, st in self.states if st != OK]


@dataclass
class RecoverySummary:
    job_dir: str
    tasks: list[FileTask]
    seconds: float
    cancelled: bool = False
    finished_early: bool = False
    report_html: str = ""
    report_csv: str = ""
    error: str = ""

    def count(self, status: str) -> int:
        return sum(1 for t in self.tasks if t.status == status)

    @property
    def total_bytes(self) -> int:
        return sum(t.size for t in self.tasks)

    @property
    def recovered_bytes(self) -> int:
        return sum(t.ok_bytes for t in self.tasks if t.status in (Status.OK, Status.PARTIAL))

    @property
    def damaged_bytes(self) -> int:
        return sum(t.bad_bytes for t in self.tasks if t.status == Status.PARTIAL)

    @property
    def outcome(self) -> str:
        if self.error:
            return "failed"
        if self.count(Status.FAILED) or self.count(Status.PARTIAL) or self.cancelled:
            if self.count(Status.OK) == 0 and self.count(Status.PARTIAL) == 0:
                return "failed"
            return "warning"
        return "success"

    def headline(self) -> str:
        ok, partial, failed = self.count(Status.OK), self.count(Status.PARTIAL), self.count(Status.FAILED)
        skipped = self.count(Status.SKIPPED) + self.count(Status.PENDING)
        parts = [f"{ok:,} recovered"]
        if partial:
            parts.append(f"{partial:,} damaged")
        if failed:
            parts.append(f"{failed:,} failed")
        if skipped:
            parts.append(f"{skipped:,} not processed")
        return ", ".join(parts)


class _VerifyMismatch(Exception):
    pass


def node_key(node: Node) -> str:
    vol = node.volume
    offset = vol.offset if vol is not None else 0
    return f"{offset}|{'/'.join(node.path_parts()[1:])}|{node.size}"


def _sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(long_path(path), "rb", buffering=0) as fh:
        _drop_cache(fh.fileno())
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _drop_cache(fd: int) -> None:
    """Make the next reads come from the disk, not from memory (best effort)."""
    advise = getattr(os, "posix_fadvise", None)
    if advise is not None:
        try:
            advise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass


def read_back(path: str, offset: int, length: int) -> bytes:
    if sys.platform == "win32":
        from .winverify import read_unbuffered

        return read_unbuffered(long_path(path), offset, length)
    with open(long_path(path), "rb", buffering=0) as fh:
        _drop_cache(fh.fileno())
        fh.seek(offset)
        return fh.read(length)


def hash_file_unbuffered(path: str) -> str:
    if sys.platform == "win32":
        from .winverify import sha256_unbuffered

        return sha256_unbuffered(long_path(path))
    return _sha256_of(path)


class RecoveryJob:
    def __init__(
        self,
        files: list[Node],
        reader: RescueReader,
        source: DeviceInfo,
        options: RecoveryOptions,
        events: EventBus | None = None,
        control: JobControl | None = None,
        progress: Callable[[Progress], None] | None = None,
        interventions: InterventionHandler | None = None,
        folders: list[Node] | None = None,
    ) -> None:
        self.files = files
        self.folders = folders or []
        self.reader = reader
        self.source = source
        self.options = options
        self.events = events or EventBus()
        self.control = control or JobControl()
        self.progress = progress or (lambda _p: None)
        self.interventions = interventions or InterventionHandler()
        self.tasks: list[FileTask] = []
        self.job_dir = ""
        self.journal: Journal | None = None
        self._finish_early = threading.Event()
        self._names = NameSpace()
        self._dest_dirs: dict[int, str] = {}
        self._dir_nodes: dict[str, Node] = {}
        self._dest_fat = False
        self._map_saved = time.monotonic()
        self._consecutive_write_errors = 0
        self._sync_each_file = True
        self._throttle = Throttle(0.25)
        self._meter = RateMeter()
        self._done_bytes = 0
        self._pass_total = 0
        self._pass_index = 0
        self._pass_count = 0
        self._phase = ""
        self._errors = 0
        self._warnings = 0
        self._resumable_paths: set[str] = set()

    # ------------------------------------------------------------------ control
    def finish_early(self) -> None:
        """Stop after the current file and skip the remaining rescue passes."""
        self._finish_early.set()

    # ----------------------------------------------------------------- planning
    def _dest_dir_for(self, node: Node | None) -> str:
        """Destination folder (relative) for the folder ``node``."""
        if node is None or node.parent is None:
            return ""
        cached = self._dest_dirs.get(id(node))
        if cached is not None:
            return cached
        parent = self._dest_dir_for(node.parent)
        name = self._names.claim(parent, sanitize(node.name), deleted=bool(node.flags & F.DELETED))
        rel = os.path.join(parent, name) if parent else name
        self._dest_dirs[id(node)] = rel
        self._dir_nodes[rel] = node
        return rel

    def plan(self) -> list[FileTask]:
        live = [n for n in self.files if not n.flags & F.DELETED]
        deleted = [n for n in self.files if n.flags & F.DELETED]
        tasks: list[FileTask] = []
        seen: set[int] = set()
        for folder_node in self.folders:
            self._dest_dir_for(folder_node)
        for node in live + deleted:
            if id(node) in seen or node.flags & F.DIR:
                continue
            seen.add(id(node))
            folder = self._dest_dir_for(node.parent)
            name = self._names.claim(folder, sanitize(node.name), deleted=bool(node.flags & F.DELETED))
            rel = os.path.join(folder, name) if folder else name
            task = FileTask(node, node.path(), rel, node_key(node), node.size)
            tasks.append(task)
        for task in tasks:
            vol = task.node.volume
            try:
                task.order = vol.layout(task.node).first_disk_offset() if vol is not None else -1
            except Exception:  # noqa: BLE001 - ordering is an optimisation only
                task.order = -1
        tasks.sort(key=lambda t: t.order)
        self.tasks = tasks
        return tasks

    # --------------------------------------------------------------------- run
    def run(self) -> RecoverySummary:
        started = time.monotonic()
        summary = RecoverySummary("", [], 0.0)
        try:
            self._prepare()
            summary.job_dir = self.job_dir
            self._run_passes()
        except Cancelled as exc:
            summary.cancelled = True
            self.events.warning(f"Recovery stopped: {exc.message}")
        except Exception as exc:
            log.exception("Recovery failed")
            summary.error = str(exc)
            self.events.critical(f"Recovery stopped by an unexpected error: {exc}", code=E_INTERNAL)
        finally:
            summary.tasks = self.tasks
            summary.seconds = time.monotonic() - started
            summary.finished_early = self._finish_early.is_set()
            self._finalize(summary)
        return summary

    def _prepare(self) -> None:
        opts = self.options
        if not self.tasks:
            self.plan()
        if opts.job_folder:
            stamp = time.strftime("%Y-%m-%d %H.%M")
            base = opts.job_name or f"Lifeboat Recovery {stamp}"
            job = os.path.join(opts.destination, sanitize(base))
            suffix = 2
            while os.path.exists(long_path(job)):
                job = os.path.join(opts.destination, sanitize(f"{base} ({suffix})"))
                suffix += 1
            self.job_dir = job
        else:
            self.job_dir = opts.destination
        make_dirs(self.job_dir)
        self._dest_fat = volume_filesystem(self.job_dir).lower() in FAT_NAMES
        for task in self.tasks:
            task.dest = os.path.join(self.job_dir, task.rel_path)
        self.journal = Journal(self.job_dir)
        if opts.resume:
            self._apply_resume()
        self.journal.open({"source": self.source.identity, "source_name": self.source.title,
                           "files": len(self.tasks)})
        if sys.platform == "win32" and opts.verify:
            # Verification reads every file back past the Windows cache, which first writes the
            # file's data to the destination drive; flushing each file as well would only add a
            # slow drive-cache flush per file. (The journal's periodic flush makes it all durable.)
            from .winverify import reads_unbuffered

            self._sync_each_file = not reads_unbuffered(long_path(self.journal.path))
        self.events.info(
            f"Recovering {len(self.tasks):,} files ({format_size(sum(t.size for t in self.tasks))}) "
            f"to {self.job_dir}")

    def _apply_resume(self) -> None:
        _header, entries = Journal.load(self.job_dir)
        map_path = os.path.join(self.job_dir, META_DIR, SOURCE_MAP)
        if os.path.exists(long_path(map_path)):
            try:
                previous = SectorMap.load(long_path(map_path), self.reader.size)
                for start, end, state in previous.segments():
                    if state in (State.GOOD, State.BAD, State.FAILED):
                        self.reader.map.upgrade(start, end, state)
            except (OSError, ValueError):
                log.warning("Could not load the previous sector map", exc_info=True)
        reused = 0
        by_key = {t.key: t for t in self.tasks}
        self._resumable_paths = {e.rel_path for e in entries.values()}
        for key, entry in entries.items():
            task = by_key.get(key)
            if task is not None and entry.status != Status.OK:
                # Redo it in the same place as last time.
                task.rel_path = entry.rel_path
                task.dest = os.path.join(self.job_dir, entry.rel_path)
        for key, entry in entries.items():
            task = by_key.get(key)
            if task is None or entry.status != Status.OK:
                continue
            path = os.path.join(self.job_dir, entry.rel_path)
            try:
                if os.path.getsize(long_path(path)) != task.size:
                    continue
            except OSError:
                continue
            task.rel_path = entry.rel_path
            task.dest = path
            task.status = Status.OK
            task.sha256 = entry.sha256
            task.states = [(0, task.size, OK)] if task.size else []
            task.notes.append("Recovered in an earlier session")
            reused += 1
        if reused:
            self.events.info(f"Resuming: {reused:,} files were already recovered and verified.")

    # ----------------------------------------------------------------- passes
    def _run_passes(self) -> None:
        passes = PASSES.get(self.options.thoroughness, PASSES["standard"])
        self._pass_count = len(passes)
        todo = [t for t in self.tasks if t.status == Status.PENDING]
        self._pass_index = 1
        self._phase = "Copying files"
        self._pass_total = sum(t.size for t in todo)
        self._done_bytes = 0
        self._meter.reset()
        self.reader.reset_skipping()
        for index, task in enumerate(todo):
            self.control.check()
            if self._finish_early.is_set():
                break
            self._copy_task(task, index, len(todo))
            self._save_map()
        for number, mode in enumerate(passes[1:], start=2):
            if self._finish_early.is_set():
                break
            relevant = [t for t in self.tasks if self._needs(t, mode)]
            if not relevant:
                continue
            self._pass_index = number
            self._phase = MODE_LABELS[mode]
            self._pass_total = sum(self._range_bytes(t, mode) for t in relevant)
            self._done_bytes = 0
            self._meter.reset()
            self.events.info(
                f"Pass {number} of {len(passes)}: {MODE_LABELS[mode].lower()} in {len(relevant):,} damaged "
                f"file(s) ({format_size(self._pass_total)}). You can stop at any time; what was recovered is kept.")
            for index, task in enumerate(relevant):
                self.control.check()
                if self._finish_early.is_set():
                    break
                self._patch_task(task, mode, index, len(relevant))
                self._save_map()

    @staticmethod
    def _needs(task: FileTask, mode: ReadMode) -> bool:
        if task.status not in (Status.PARTIAL,) or not os.path.exists(long_path(task.dest)):
            return False
        wanted = (BAD,) if mode == ReadMode.RETRY else (UNREAD,)
        return any(st in wanted for _s, _e, st in task.states)

    @staticmethod
    def _range_bytes(task: FileTask, mode: ReadMode) -> int:
        wanted = (BAD,) if mode == ReadMode.RETRY else (UNREAD,)
        return sum(e - s for s, e, st in task.states if st in wanted)

    # ---------------------------------------------------------------- progress
    def _report_progress(self, item: str, items_done: int, items_total: int, force: bool = False) -> None:
        if not self._throttle.ready(force):
            return
        rate = self._meter.update(self._done_bytes)
        bad = sum(t.bad_bytes for t in self.tasks if t.status in (Status.PARTIAL,))
        self.progress(Progress(
            phase=self._phase, done=self._done_bytes, total=self._pass_total, item=item, rate=rate,
            eta=self._meter.eta(self._done_bytes, self._pass_total), items_done=items_done,
            items_total=items_total, errors=self._errors, warnings=self._warnings, bad_bytes=bad,
            pass_index=self._pass_index, pass_count=self._pass_count,
        ))

    def _save_map(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._map_saved < 30:
            return
        self._map_saved = now
        try:
            self.reader.map.save(long_path(os.path.join(self.job_dir, META_DIR, SOURCE_MAP)))
        except OSError:
            log.warning("Could not save the sector map", exc_info=True)

    # ------------------------------------------------------------- source I/O
    def _read(self, reader: FileContentReader, start: int, end: int, mode: ReadMode):
        return run_with_device_retry(lambda: reader.read(start, end, mode), self.reader, self.interventions,
                                     self.events, self.control, "recovering files")

    # --------------------------------------------------------- copy (pass 1)
    def _copy_task(self, task: FileTask, index: int, count: int) -> None:
        node = task.node
        vol = node.volume
        self._report_progress(task.source_path, index, count, force=index == 0)
        try:
            layout = vol.layout(node) if vol is not None else FileLayout(0)
        except Exception as exc:
            log.exception("layout failed for %s", task.source_path)
            self._fail(task, E_FILE_LOST, f"The file's location could not be decoded: {exc}")
            self._done_bytes += task.size
            return
        if layout.unsupported:
            code = E_FILE_ENCRYPTED if node.flags & F.ENCRYPTED else E_FILE_UNSUPPORTED
            self._fail(task, code, layout.unsupported)
            self._done_bytes += task.size
            return
        if self._dest_fat and task.size > FAT32_LIMIT:
            self._fail(task, E_DEST_FAT32, "Too large for the FAT32 destination (4 GB limit).")
            self._done_bytes += task.size
            return
        task.notes.extend(layout.problems)
        if node.flags & F.OVERWRITTEN:
            task.notes.append("Deleted file whose space was reused: the content may be overwritten.")
        if node.flags & F.ASSUMED_CONTIGUOUS:
            task.notes.append("Deleted file: its location was estimated.")
        if node.flags & F.NAME_GUESSED:
            task.notes.append("The first letter of the name was lost when the file was deleted.")
        self._avoid_overwrite(task)
        content = FileContentReader(layout, self.reader)
        attempts = 0
        before = self._done_bytes
        while True:
            self._done_bytes = before
            try:
                digest, states = self._write_new(task, content, index, count)
                if self.options.verify and task.size:
                    if hash_file_unbuffered(task.dest + PART_SUFFIX) != digest:
                        raise _VerifyMismatch
                os.replace(long_path(task.dest + PART_SUFFIX), long_path(task.dest))
                break
            except _VerifyMismatch:
                attempts += 1
                if attempts >= 2:
                    self._discard_part(task)
                    self._fail(task, E_VERIFY, "The copy on the destination did not match what was read "
                                               "(checked twice). The destination drive may be unreliable.")
                    return
                self.events.warning(f"Verification failed for {task.source_path}; writing it again.",
                                    code=E_VERIFY)
            except Cancelled:
                self._discard_part(task)
                raise
            except OSError as exc:
                if self._handle_dest_error(task, exc):
                    continue
                self._discard_part(task)
                return
        self._consecutive_write_errors = 0
        task.sha256 = digest
        task.states = normalize_states(states) if task.size else []
        self._settle(task)
        if self.options.preserve_times:
            set_times(long_path(task.dest), node.ctime, node.atime, node.mtime)
        self._journal(task)

    def _avoid_overwrite(self, task: FileTask) -> None:
        """Never replace a file that already exists in the destination folder."""
        if task.rel_path in self._resumable_paths:
            return
        if not os.path.lexists(long_path(task.dest)):
            return
        folder, name = os.path.split(task.rel_path)
        stem, dot, ext = name.rpartition(".")
        if not dot or not stem:
            stem, ext = name, ""
        counter = 2
        while True:
            candidate = f"{stem} ({counter}){'.' + ext if ext else ''}"
            rel = os.path.join(folder, candidate) if folder else candidate
            dest = os.path.join(self.job_dir, rel)
            if not os.path.lexists(long_path(dest)):
                task.rel_path, task.dest = rel, dest
                return
            counter += 1

    def _write_new(self, task: FileTask, content: FileContentReader, index: int,
                   count: int) -> tuple[str, list[tuple[int, int, int]]]:
        part = task.dest + PART_SUFFIX
        make_dirs(os.path.dirname(task.dest) or self.job_dir)
        digest = hashlib.sha256()
        states: list[tuple[int, int, int]] = []
        with open(long_path(part), "wb") as out:
            pos = 0
            while pos < task.size:
                self.control.check()
                end = min(task.size, pos + CHUNK)
                chunk = self._read(content, pos, end, ReadMode.FAST)
                out.write(chunk.data)
                digest.update(chunk.data)
                states.extend(chunk.states)
                self._done_bytes += end - pos
                self._report_progress(task.source_path, index, count)
                pos = end
            out.flush()
            if self._sync_each_file:
                os.fsync(out.fileno())
        return digest.hexdigest(), states

    def _discard_part(self, task: FileTask) -> None:
        try:
            os.remove(long_path(task.dest + PART_SUFFIX))
        except OSError:
            pass

    # ------------------------------------------------------- rescue passes
    def _patch_task(self, task: FileTask, mode: ReadMode, index: int, count: int) -> None:
        vol = task.node.volume
        if vol is None:
            return
        try:
            layout = vol.layout(task.node)
        except Exception:
            log.exception("layout failed for %s", task.source_path)
            return
        content = FileContentReader(layout, self.reader)
        wanted = (BAD,) if mode == ReadMode.RETRY else (UNREAD,)
        targets = [(s, e) for s, e, st in task.states if st in wanted]
        patched: list[tuple[int, bytes]] = []
        new_states: list[tuple[int, int, int]] = [x for x in task.states]
        while True:
            try:
                with open(long_path(task.dest), "r+b") as out:
                    for start, end in targets:
                        pos = start
                        while pos < end:
                            self.control.check()
                            stop = min(end, pos + CHUNK)
                            chunk = self._read(content, pos, stop, mode)
                            for s, e, st in chunk.states:
                                if st == OK:
                                    piece = bytes(chunk.data[s - pos:e - pos])
                                    out.seek(s)
                                    out.write(piece)
                                    patched.append((s, piece))
                            new_states = _replace_states(new_states, pos, stop, chunk.states)
                            self._done_bytes += stop - pos
                            self._report_progress(task.source_path, index, count)
                            pos = stop
                    out.flush()
                    if self._sync_each_file:
                        os.fsync(out.fileno())
                break
            except OSError as exc:
                if self._handle_dest_error(task, exc):
                    patched = []
                    new_states = [x for x in task.states]
                    continue
                return
        if self.options.verify:
            for offset, piece in patched:
                if read_back(task.dest, offset, len(piece)) != piece:
                    self._fail(task, E_VERIFY, "A repaired part of the file did not verify on the destination.")
                    return
        recovered = sum(len(p) for _o, p in patched)
        task.states = normalize_states(new_states)
        if recovered:
            task.sha256 = hash_file_unbuffered(task.dest)
            self.events.info(f"Recovered {format_size(recovered)} more of {task.source_path}.")
        self._settle(task)
        self._journal(task)

    # ------------------------------------------------------------- outcomes
    def _settle(self, task: FileTask) -> None:
        """Update the status after a pass.  Files with nothing readable yet stay
        PARTIAL so the rescue passes still get to try them; the final verdict
        is made in :meth:`_finalize`."""
        if task.status == Status.FAILED:
            return
        if any(st != OK for _s, _e, st in task.states):
            first = task.status != Status.PARTIAL
            task.status = Status.PARTIAL
            task.code = E_FILE_PARTIAL
            unread = task.bytes_in(UNREAD)
            bad = task.bytes_in(BAD)
            task.message = (f"{format_size(bad + unread)} of {format_size(task.size)} could not be read yet"
                            if unread else f"{format_size(bad)} of {format_size(task.size)} unreadable")
            if first:
                self._warnings += 1
        else:
            if task.status == Status.PARTIAL:
                self.events.success(f"Fully recovered after retries: {task.source_path}")
            task.status = Status.OK
            task.code = ""
            task.message = ""
            if task.node.flags & F.OVERWRITTEN:
                task.code = E_FILE_OVERWRITTEN

    def _fail(self, task: FileTask, code: str, message: str) -> None:
        task.status = Status.FAILED
        task.code = code
        task.message = message
        self._errors += 1
        self.events.error(f"Not recovered: {task.source_path} - {message}", code=code, path=task.source_path)
        self._journal(task)

    def _journal(self, task: FileTask) -> None:
        if self.journal is None:
            return
        self.journal.record(JournalEntry(
            task.key, task.rel_path, task.status, task.size, task.sha256,
            [[s, e] for s, e, st in task.states if st == BAD],
            [[s, e] for s, e, st in task.states if st == UNREAD],
        ))

    # --------------------------------------------------- destination errors
    def _handle_dest_error(self, task: FileTask, exc: OSError) -> bool:
        """Return True to retry the file, False when the file was given up."""
        self._consecutive_write_errors += 1
        if is_disk_full(exc):
            self.events.critical("The destination drive is full.", code=E_DEST_FULL, details=str(exc))
            choice = self.interventions.request(Intervention(
                code=E_DEST_FULL,
                title="Destination drive is full",
                message=("Free up space on the destination (or connect a bigger drive to the same folder), "
                         "then press Retry. Skip leaves this file out; Stop ends the recovery."),
                options=(Choice.RETRY, Choice.SKIP, Choice.ABORT),
            ))
            if choice is Choice.RETRY:
                return True
            if choice is Choice.SKIP:
                self._discard_part(task)
                self._fail(task, E_DEST_FULL, "Skipped because the destination was full.")
                return False
            raise Cancelled("Stopped because the destination drive is full.")
        root_ok = os.path.isdir(long_path(self.job_dir))
        if is_gone(exc) or not root_ok or self._consecutive_write_errors >= 5:
            self.events.critical("The destination drive is not available.", code=E_DEST_GONE, details=str(exc))
            choice = self.interventions.request(Intervention(
                code=E_DEST_GONE,
                title="Destination not available",
                message=("Lifeboat can no longer write to the destination. Reconnect the destination drive; "
                         "Lifeboat continues automatically when the folder is reachable again."),
                options=(Choice.RETRY, Choice.ABORT),
                auto_retry=lambda: os.path.isdir(long_path(self.job_dir)) and _writable(self.job_dir),
            ))
            if choice is Choice.RETRY:
                self._consecutive_write_errors = 0
                return True
            raise Cancelled("Stopped because the destination is not available.")
        self._fail(task, E_DEST_WRITE, f"Could not write the file: {exc.strerror or exc}")
        return False

    # ---------------------------------------------------------------- finish
    def _finalize(self, summary: RecoverySummary) -> None:
        for task in self.tasks:
            if task.status == Status.PENDING:
                task.status = Status.SKIPPED
                task.message = "Not processed (the recovery was stopped)."
            elif task.status == Status.PARTIAL and task.size and task.ok_bytes == 0:
                # Nothing but zeros: do not leave a useless file behind.
                try:
                    os.remove(long_path(task.dest))
                except OSError:
                    pass
                if task.bytes_in(UNREAD):
                    self._fail(task, E_FILE_LOST, "No data could be read in the passes that were run. "
                                                  "Recovering again with Standard or Maximum thoroughness "
                                                  "may get more.")
                else:
                    self._fail(task, E_FILE_LOST, "None of this file's data could be read.")
        if self.options.mark_damaged:
            for task in self.tasks:
                if task.status == Status.PARTIAL:
                    self._mark_damaged(task)
        if self.options.preserve_times and self.job_dir:
            for rel, folder in sorted(self._dir_nodes.items(), key=lambda kv: -kv[0].count(os.sep)):
                path = os.path.join(self.job_dir, rel)
                if os.path.isdir(long_path(path)):
                    set_times(long_path(path), folder.ctime, folder.atime, folder.mtime)
        if self.job_dir:
            self._save_map(force=True)
            try:
                from .report import write_reports

                summary.report_html, summary.report_csv = write_reports(self, summary)
            except Exception:
                log.exception("Could not write the report")
                self.events.error("The recovery report could not be written to the destination.")
        if self.journal is not None:
            self.journal.close()
        self.reader.flush_reports()
        outcome = summary.outcome
        line = f"Recovery finished: {summary.headline()}."
        if outcome == "success":
            self.events.success(line)
        elif outcome == "warning":
            self.events.warning(line)
        else:
            self.events.error(line)

    def _mark_damaged(self, task: FileTask) -> None:
        stem, dot, ext = task.dest.rpartition(".")
        if not dot or os.sep in ext:
            stem, ext = task.dest, ""
        target = f"{stem} [DAMAGED]{'.' + ext if ext else ''}"
        try:
            os.replace(long_path(task.dest), long_path(target))
            task.dest = target
            task.rel_path = os.path.relpath(target, self.job_dir)
        except OSError:
            pass


def _replace_states(states: list[tuple[int, int, int]], start: int, end: int,
                    new: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    out: list[tuple[int, int, int]] = []
    for s, e, st in states:
        if e <= start or s >= end:
            out.append((s, e, st))
            continue
        if s < start:
            out.append((s, start, st))
        if e > end:
            out.append((end, e, st))
    out.extend(new)
    return normalize_states(out)


def _writable(folder: str) -> bool:
    probe = os.path.join(long_path(folder), ".lifeboat-probe")
    try:
        with open(probe, "wb") as fh:
            fh.write(b"x")
        os.remove(probe)
        return True
    except OSError:
        return False
