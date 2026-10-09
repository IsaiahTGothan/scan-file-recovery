"""The sector map: what is known about every byte of the source.

The map is a piecewise-constant function over ``[0, size)``.  It is the
single source of truth for "is this area readable?": the rescue reader
updates it, recovered files derive their status from it, the disk-map view
draws it and it is saved in GNU ddrescue's mapfile format so a recovery or
imaging session can be resumed (and inspected with ddrescueview).
"""

from __future__ import annotations

import enum
import os
import tempfile
import threading
import time
from bisect import bisect_right
from collections.abc import Iterable
from pathlib import Path


class State(enum.IntEnum):
    UNTRIED = 0   # never read                                  ddrescue '?'
    GOOD = 1      # read successfully                           ddrescue '+'
    SKIPPED = 2   # skipped on purpose near an error, retry later  ddrescue '?'
    FAILED = 3    # a large read failed here; needs finer reads   ddrescue '*'
    BAD = 4       # unreadable at the smallest unit               ddrescue '-'


#: Display-only value for map cells that are partly read.
PARTIAL = 5

_TO_DDRESCUE = {
    State.UNTRIED: "?",
    State.GOOD: "+",
    State.SKIPPED: "?",
    State.FAILED: "*",
    State.BAD: "-",
}
_FROM_DDRESCUE = {
    "?": State.UNTRIED,
    "+": State.GOOD,
    "*": State.FAILED,
    "/": State.FAILED,
    "-": State.BAD,
}


class MapFileError(ValueError):
    pass


class SectorMap:
    def __init__(self, size: int) -> None:
        if size < 0:
            raise ValueError("size must be >= 0")
        self.size = size
        self._starts: list[int] = [0]
        self._states: list[State] = [State.UNTRIED]
        self._totals: dict[State, int] = {state: 0 for state in State}
        self._totals[State.UNTRIED] = size
        self._lock = threading.RLock()
        self.version = 0
        self.current_pos = 0
        self.current_pass = 1

    # ------------------------------------------------------------------ helpers
    def _seg_end(self, index: int) -> int:
        if index + 1 < len(self._starts):
            return self._starts[index + 1]
        return self.size

    # ------------------------------------------------------------------ updates
    def set(self, start: int, end: int, state: State) -> None:
        start = max(0, start)
        end = min(self.size, end)
        if start >= end:
            return
        with self._lock:
            starts, states = self._starts, self._states
            first = bisect_right(starts, start) - 1
            last = bisect_right(starts, end - 1) - 1
            if first == last and states[first] == state:
                return
            for index in range(first, last + 1):
                seg_start = max(starts[index], start)
                seg_end = min(self._seg_end(index), end)
                self._totals[states[index]] -= seg_end - seg_start
            self._totals[state] += end - start
            tail_state = states[last]
            tail_end = self._seg_end(last)
            new_starts: list[int] = []
            new_states: list[State] = []
            if starts[first] < start:
                new_starts.append(starts[first])
                new_states.append(states[first])
            new_starts.append(start)
            new_states.append(state)
            if end < tail_end:
                new_starts.append(end)
                new_states.append(tail_state)
            starts[first:last + 1] = new_starts
            states[first:last + 1] = new_states
            # Merge equal neighbours around the replaced window.
            index = max(first, 1)
            stop = first + len(new_starts) + 1
            while index < len(starts) and index <= stop:
                if states[index] == states[index - 1]:
                    del starts[index]
                    del states[index]
                    stop -= 1
                else:
                    index += 1
            self.version += 1

    def align(self, sector: int) -> None:
        """Expand non-GOOD segments to whole sectors.

        Maps written by other tools may use byte granularity; device reads
        must stay sector aligned.
        """
        if sector <= 1:
            return
        for seg_start, seg_end, state in self.segments():
            if state == State.GOOD:
                continue
            if seg_start % sector or seg_end % sector:
                lo = seg_start - seg_start % sector
                hi = seg_end + (-seg_end % sector)
                self.set(lo, min(self.size, hi), state)

    def upgrade(self, start: int, end: int, state: State) -> None:
        """Set ``state`` only where the current state is "less known".

        Used when loading an older map into a newer session: GOOD never goes
        back to FAILED, BAD is only replaced by GOOD.
        """
        for seg_start, seg_end, current in self.segments(start, end):
            if current == state:
                continue
            if current == State.GOOD:
                continue
            if current == State.BAD and state != State.GOOD:
                continue
            self.set(seg_start, seg_end, state)

    # ------------------------------------------------------------------ queries
    def state_at(self, pos: int) -> State:
        with self._lock:
            index = bisect_right(self._starts, pos) - 1
            return self._states[max(index, 0)]

    def segments(self, start: int = 0, end: int | None = None) -> list[tuple[int, int, State]]:
        """Snapshot of segments overlapping ``[start, end)``, clipped to it."""
        if end is None:
            end = self.size
        start = max(0, start)
        end = min(self.size, end)
        out: list[tuple[int, int, State]] = []
        if start >= end:
            return out
        with self._lock:
            starts, states = self._starts, self._states
            index = max(bisect_right(starts, start) - 1, 0)
            while index < len(starts) and starts[index] < end:
                seg_start = max(starts[index], start)
                seg_end = min(self._seg_end(index), end)
                if seg_start < seg_end:
                    out.append((seg_start, seg_end, states[index]))
                index += 1
        return out

    def ranges(self, states: Iterable[State], start: int = 0, end: int | None = None) -> list[tuple[int, int]]:
        wanted = set(states)
        merged: list[tuple[int, int]] = []
        for seg_start, seg_end, state in self.segments(start, end):
            if state in wanted:
                if merged and merged[-1][1] == seg_start:
                    merged[-1] = (merged[-1][0], seg_end)
                else:
                    merged.append((seg_start, seg_end))
        return merged

    def all_in(self, start: int, end: int, states: Iterable[State]) -> bool:
        wanted = set(states)
        return all(state in wanted for _s, _e, state in self.segments(start, end))

    def totals(self) -> dict[State, int]:
        with self._lock:
            return dict(self._totals)

    def segment_count(self) -> int:
        with self._lock:
            return len(self._starts)

    def summarize(self, cells: int) -> list[int]:
        """Reduce the map to ``cells`` display cells.

        Each cell gets the worst problem state it contains (BAD > FAILED >
        SKIPPED), otherwise GOOD when fully read, PARTIAL when partly read
        and UNTRIED when nothing was read.
        """
        if cells <= 0 or self.size == 0:
            return []
        severity = {State.BAD: 4, State.FAILED: 3, State.SKIPPED: 2}
        worst = [0] * cells
        good_bytes = [0] * cells
        cell_size = self.size / cells
        with self._lock:
            snapshot = list(zip(self._starts, self._states, strict=True))
        for index, (seg_start, state) in enumerate(snapshot):
            seg_end = snapshot[index + 1][0] if index + 1 < len(snapshot) else self.size
            first = min(int(seg_start / cell_size), cells - 1)
            last = min(int((seg_end - 1) / cell_size), cells - 1)
            if state in severity:
                level = severity[state]
                for cell in range(first, last + 1):
                    if worst[cell] < level:
                        worst[cell] = level
            elif state == State.GOOD:
                if first == last:
                    good_bytes[first] += seg_end - seg_start
                else:
                    good_bytes[first] += int((first + 1) * cell_size) - seg_start
                    for cell in range(first + 1, last):
                        good_bytes[cell] += int((cell + 1) * cell_size) - int(cell * cell_size)
                    good_bytes[last] += seg_end - int(last * cell_size)
        result = []
        by_level = {4: int(State.BAD), 3: int(State.FAILED), 2: int(State.SKIPPED)}
        for cell in range(cells):
            if worst[cell]:
                result.append(by_level[worst[cell]])
                continue
            span = int((cell + 1) * cell_size) - int(cell * cell_size)
            if good_bytes[cell] >= span:
                result.append(int(State.GOOD))
            elif good_bytes[cell] > 0:
                result.append(PARTIAL)
            else:
                result.append(int(State.UNTRIED))
        return result

    # --------------------------------------------------------------- persistence
    def to_ddrescue(self, status: str = "?") -> str:
        lines = [
            "# Mapfile. Created by Lifeboat Data Recovery (GNU ddrescue compatible)",
            f"# Current time: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "# current_pos  current_status  current_pass",
            f"0x{self.current_pos:08X}     {status}               {self.current_pass}",
            "#      pos        size  status",
        ]
        for seg_start, seg_end, state in self.segments():
            lines.append(f"0x{seg_start:08X}  0x{seg_end - seg_start:08X}  {_TO_DDRESCUE[state]}")
        return "\n".join(lines) + "\n"

    def save(self, path: str | os.PathLike[str], status: str = "?") -> None:
        """Write the map atomically (temp file + fsync + rename)."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = self.to_ddrescue(status)
        fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="ascii", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    @classmethod
    def from_ddrescue(cls, text: str, size: int | None = None) -> SectorMap:
        blocks: list[tuple[int, int, State]] = []
        status_line_seen = False
        current_pos = 0
        current_pass = 1
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if not status_line_seen:
                status_line_seen = True
                try:
                    current_pos = int(parts[0], 0)
                    if len(parts) >= 3:
                        current_pass = int(parts[2], 0)
                except (ValueError, IndexError) as exc:
                    raise MapFileError(f"Bad mapfile status line: {line!r}") from exc
                continue
            if len(parts) < 3:
                raise MapFileError(f"Bad mapfile line: {line!r}")
            try:
                pos = int(parts[0], 0)
                length = int(parts[1], 0)
            except ValueError as exc:
                raise MapFileError(f"Bad mapfile line: {line!r}") from exc
            state = _FROM_DDRESCUE.get(parts[2])
            if state is None or pos < 0 or length < 0:
                raise MapFileError(f"Bad mapfile line: {line!r}")
            blocks.append((pos, length, state))
        end = max((pos + length for pos, length, _ in blocks), default=0)
        total = size if size is not None else end
        result = cls(total)
        for pos, length, state in blocks:
            if state != State.UNTRIED:
                result.set(pos, pos + length, state)
        result.current_pos = current_pos
        result.current_pass = current_pass
        return result

    @classmethod
    def load(cls, path: str | os.PathLike[str], size: int | None = None) -> SectorMap:
        text = Path(path).read_text(encoding="ascii", errors="replace")
        return cls.from_ddrescue(text, size)
