"""A device wrapper that simulates a failing drive.

Used by the test-suite to prove that bad sectors, slow areas, hangs and
disconnects are handled correctly, and by ``lifeboat-cli selftest``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from ..errors import ReadError, ReadErrorKind
from .base import BlockDevice, DeviceInfo


@dataclass
class FaultPlan:
    bad: list[tuple[int, int]] = field(default_factory=list)       # always fail (media error)
    flaky: dict[tuple[int, int], int] = field(default_factory=dict)  # fail N times, then succeed
    slow: list[tuple[int, int]] = field(default_factory=list)      # succeed after ``slow_delay``
    hang: list[tuple[int, int]] = field(default_factory=list)      # never answer (times out)
    slow_delay: float = 0.05
    disconnect_after_reads: int | None = None                      # unplug after N reads


class SimulatedFailingDevice(BlockDevice):
    supports_timeout = True

    def __init__(self, inner: BlockDevice, plan: FaultPlan) -> None:
        self.inner = inner
        self.plan = plan
        self.info = DeviceInfo(
            path=f"simulated:{inner.info.path}",
            kind=inner.info.kind,
            size=inner.info.size,
            sector_size=inner.info.sector_size,
            physical_sector_size=inner.info.physical_sector_size,
            model="Simulated failing drive",
            serial="SIM-0001",
        )
        self.reads = 0
        self.read_log: list[tuple[int, int, str]] = []
        self.connected = True
        self._flaky_left = dict(plan.flaky)
        self._lock = threading.Lock()

    @staticmethod
    def _hits(ranges: list[tuple[int, int]], start: int, end: int) -> bool:
        return any(s < end and e > start for s, e in ranges)

    def read_raw(self, offset: int, length: int, timeout: float | None = None) -> bytes:
        self._check_aligned(offset, length)
        end = offset + length
        with self._lock:
            if not self.connected:
                self.read_log.append((offset, length, "gone"))
                raise ReadError("Device not connected", kind=ReadErrorKind.GONE, offset=offset, length=length)
            self.reads += 1
            limit = self.plan.disconnect_after_reads
            if limit is not None and self.reads > limit:
                self.connected = False
                self.read_log.append((offset, length, "gone"))
                raise ReadError("Device not connected", kind=ReadErrorKind.GONE, offset=offset, length=length)
            for (s, e), left in list(self._flaky_left.items()):
                if s < end and e > offset and left > 0:
                    self._flaky_left[(s, e)] = left - 1
                    self.read_log.append((offset, length, "flaky"))
                    raise ReadError("Simulated flaky sector", kind=ReadErrorKind.MEDIA,
                                    offset=offset, length=length)
            if self._hits(self.plan.bad, offset, end):
                self.read_log.append((offset, length, "bad"))
                raise ReadError("Simulated bad sector (CRC error)", kind=ReadErrorKind.MEDIA,
                                offset=offset, length=length, os_code=23)
            hang = self._hits(self.plan.hang, offset, end)
            slow = self._hits(self.plan.slow, offset, end)
        if hang:
            wait = timeout if timeout is not None else 1.0
            time.sleep(wait)
            self.read_log.append((offset, length, "timeout"))
            raise ReadError("Simulated hang", kind=ReadErrorKind.TIMEOUT, offset=offset, length=length)
        if slow:
            delay = self.plan.slow_delay
            if timeout is not None and delay > timeout:
                time.sleep(timeout)
                self.read_log.append((offset, length, "timeout"))
                raise ReadError("Simulated slow read timed out", kind=ReadErrorKind.TIMEOUT,
                                offset=offset, length=length)
            time.sleep(delay)
        self.read_log.append((offset, length, "ok"))
        return self.inner.read_raw(offset, length, timeout)

    def is_present(self) -> bool:
        return self.connected

    def reconnect(self) -> None:
        """Simulate plugging the drive back in."""
        with self._lock:
            self.connected = True
            self.plan.disconnect_after_reads = None

    def reopen(self) -> bool:
        return self.connected

    def reads_touching(self, start: int, end: int) -> int:
        return sum(1 for off, ln, _ in self.read_log if off < end and off + ln > start)

    def close(self) -> None:
        self.inner.close()
