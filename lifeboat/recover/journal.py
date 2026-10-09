"""Append-only job journal: what was recovered, so a job can be resumed."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

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
    def __init__(self, job_dir: str) -> None:
        self.dir = os.path.join(job_dir, META_DIR)
        self.path = os.path.join(self.dir, JOURNAL)
        self._lock = threading.Lock()
        self._fh = None

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
            try:
                os.fsync(self._fh.fileno())
            except OSError:
                pass

    def record(self, entry: JournalEntry) -> None:
        self._write({"type": "file", **entry.__dict__})

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
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
