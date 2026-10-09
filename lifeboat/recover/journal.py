"""Append-only job journal: what was recovered, so a job can be resumed."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from .destination import long_path

META_DIR = ".lifeboat"
JOURNAL = "journal.jsonl"
SOURCE_MAP = "source.map"


@dataclass
class JournalEntry:
    key: str
    rel_path: str
    status: str
    size: int
    sha256: str
    bad: list[list[int]]
    unread: list[list[int]]


class Journal:
    # Each record reaches the operating system at once (it survives Lifeboat crashing), but
    # is forced onto the drive at most this often: a flush per file made recoveries of many
    # small files crawl.  A power cut can lose the last second of records; resuming then
    # simply copies those files again.
    SYNC_INTERVAL = 1.0

    def __init__(self, job_dir: str) -> None:
        self.dir = os.path.join(job_dir, META_DIR)
        self.path = os.path.join(self.dir, JOURNAL)
        self._lock = threading.Lock()
        self._fh: TextIO | None = None
        self._synced = 0.0

    def open(self, header: dict) -> None:
        os.makedirs(long_path(self.dir), exist_ok=True)
        fresh = not os.path.exists(long_path(self.path))
        self._fh = open(long_path(self.path), "a", encoding="utf-8")  # noqa: SIM115
        if fresh:
            self._write({"type": "header", "created": time.time(), **header})
        else:
            self._write({"type": "resume", "time": time.time(), **header})

    def _write(self, record: dict) -> None:
        if self._fh is None:
            return
        with self._lock:
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._fh.flush()
            if time.monotonic() - self._synced >= self.SYNC_INTERVAL:
                self._sync()

    def _sync(self) -> None:
        if self._fh is None:
            return
        self._synced = time.monotonic()
        try:
            os.fsync(self._fh.fileno())
        except OSError:
            pass

    def record(self, entry: JournalEntry) -> None:
        self._write({"type": "file", **entry.__dict__})

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                except OSError:
                    pass
                self._sync()
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None

    @staticmethod
    def load(job_dir: str) -> tuple[dict | None, dict[str, JournalEntry]]:
        path = os.path.join(job_dir, META_DIR, JOURNAL)
        header: dict | None = None
        entries: dict[str, JournalEntry] = {}
        try:
            text = Path(long_path(path)).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None, {}
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn last line after a crash
            kind = record.get("type")
            if kind == "header":
                header = record
            elif kind == "file":
                try:
                    entries[record["key"]] = JournalEntry(
                        record["key"], record["rel_path"], record["status"], int(record["size"]),
                        record.get("sha256", ""), record.get("bad", []), record.get("unread", []))
                except (KeyError, TypeError, ValueError):
                    continue
        return header, entries


def find_resumable(folder: str, source_identity: str) -> tuple[dict, int] | None:
    """If ``folder`` holds an unfinished job for the same source, return (header, files done)."""
    header, entries = Journal.load(folder)
    if header is None or header.get("source") != source_identity:
        return None
    done = sum(1 for e in entries.values() if e.status == "ok")
    return header, done
