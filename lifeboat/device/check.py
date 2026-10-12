"""Drive check: what the computer can see of each drive, and what that means.

A failing drive often shows up in Device Manager (its USB adapter, or the drive's
controller) but not in Disk Management, because the disk itself doesn't answer.
The check lists every storage device the system knows about, tests whether each
disk reports its size and lets its first sector be read (one small, read-only
read with a time limit), and explains the result in plain language.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypeVar

from ..errors import E_OPEN_DENIED, DeviceGoneError, DeviceHungError, DeviceOpenError, ReadError
from ..util import format_capacity, plural
from .base import BlockDevice

log = logging.getLogger("lifeboat.device")

SYSFS = Path("/sys")   # Linux: where the kernel lists devices (tests point it at a fake tree)
READ_TIMEOUT = 8.0     # seconds for the one test read
SLOW_SECONDS = 2.0     # a healthy drive answers in a fraction of a second
PROBE_TIMEOUT = 10.0   # seconds for opening a drive or asking its size and name
T = TypeVar("T")

# verdict -> (headline, level); level is "good", "warn", "bad" or "info"
VERDICTS: dict[str, tuple[str, str]] = {
    "readable": ("Readable", "good"),
    "slow": ("Readable, but slow", "warn"),
    "damaged_start": ("Answers, first sector unreadable", "warn"),
    "not_answering": ("Detected, not answering", "bad"),
    "no_media": ("No media", "info"),
    "adapter_only": ("Adapter only, no drive", "bad"),
    "device_error": ("Device error", "bad"),
    "not_tested": ("Not tested (needs administrator)", "info"),
    "in_use": ("In use by Lifeboat", "info"),
}

# Device Manager error codes ("This device cannot start. (Code 10)").
PROBLEM_CODES: dict[int, str] = {
    1: "it isn't configured correctly",
    3: "its driver may be damaged, or the computer is low on memory",
    10: "it can't start",
    12: "it can't find enough free resources",
    14: "it needs the computer to restart",
    18: "its drivers need to be reinstalled",
    19: "its configuration in the registry is damaged",
    21: "Windows is removing it",
    22: "it is disabled",
    24: "it isn't present, isn't working properly, or doesn't have all its drivers",
    28: "its drivers aren't installed",
    29: "it is disabled in the computer's firmware",
    31: "it isn't working properly",
    32: "its driver is disabled",
    37: "its driver failed to start",
    38: "an earlier copy of its driver is still loaded",
    39: "its driver may be damaged or missing",
    41: "Windows loaded its driver but can't find the device",
    43: "it reported a problem and Windows stopped it",
    45: "it isn't connected",
    47: "it was prepared for safe removal",
    48: "its driver is blocked",
    52: "its driver signature can't be verified",
}

def system_tool() -> str:
    """The system's own disk tool, where a drive that answers shows up."""
    if sys.platform == "win32":
        return "Disk Management"
    if sys.platform == "darwin":
        return "Disk Utility"
    return "the system's disk list"


def _elevated() -> str:
    return "as root (sudo)" if sys.platform.startswith("linux") else "as administrator"


_CABLE_STEPS = ("Try, in this order: another USB cable and a port on the back of the computer (not a hub or the "
                "front panel); the drive's own power supply or a powered USB hub; another adapter or dock, or a "
                "SATA port inside a desktop.")


@dataclass
class CheckResult:
    name: str
    kind: str = "disk"              # "disk", "adapter" (USB storage with no disk) or "device" (other)
    connection: str = ""
    path: str = ""                  # \\.\PhysicalDrive2, /dev/sdb, /dev/disk4
    disk_number: int | None = None
    size: int = 0
    serial: str = ""
    removable_media: bool = False   # card readers and the like
    instance_id: str = ""           # Windows device instance ID
    problem_code: int = 0           # Device Manager error code
    read_ok: bool | None = None     # None: no read test was made
    read_seconds: float = 0.0
    read_error: str = ""
    read_error_kind: str = ""       # "media", "timeout", "gone", "range", "other"
    health: str = ""                # SMART status when known: "Good", "Caution", "Bad"
    tested: bool = True             # False when reading needs administrator rights
    in_use: bool = False            # Lifeboat is reading it right now, so the check leaves it alone

    @property
    def verdict(self) -> str:
        if self.problem_code:
            return "device_error"
        if self.kind == "adapter":
            return "adapter_only"
        if self.kind != "disk":
            return "device_error"
        if self.in_use:
            return "in_use"
        if self.size <= 0:
            return "no_media" if self.removable_media else "not_answering"
        if not self.tested or self.read_ok is None:
            return "not_tested"
        if not self.read_ok:
            return "not_answering" if self.read_error_kind in ("timeout", "gone") else "damaged_start"
        return "slow" if self.read_seconds > SLOW_SECONDS else "readable"

    @property
    def headline(self) -> str:
        return VERDICTS[self.verdict][0]

    @property
    def level(self) -> str:
        level = VERDICTS[self.verdict][1]
        if level == "good" and self.health in ("Caution", "Bad"):
            return "warn"
        return level

    @property
    def seen_by_system(self) -> bool:
        """What Disk Management needs: a disk that reports its size."""
        return self.kind == "disk" and self.size > 0 and not self.problem_code

    @property
    def system_text(self) -> str:
        """Short answer to "does Disk Management show it?"."""
        if self.seen_by_system:
            return "Yes"
        if self.kind == "disk" and self.removable_media and not self.problem_code:
            return "As \"No Media\""
        return "No"

    @property
    def read_text(self) -> str:
        """Short result of the first-sector read."""
        if self.in_use:
            return "Skipped (in use)"
        if self.read_ok is None:
            return "Not tested" if self.kind == "disk" and self.size > 0 else "-"
        if self.read_ok:
            return f"OK in {self.read_seconds:.2f} s" if self.read_seconds >= 0.01 else "OK"
        return {"media": "Bad sector", "range": "Bad sector", "timeout": "No answer", "gone": "No answer"}.get(
            self.read_error_kind, "Failed")

    @property
    def problem_text(self) -> str:
        if not self.problem_code:
            return ""
        reason = PROBLEM_CODES.get(self.problem_code, "it reported a problem")
        return f"Windows can't use this device: {reason} (Code {self.problem_code})."

    @property
    def advice(self) -> str:
        verdict = self.verdict
        error = f" ({self.read_error})" if self.read_error else ""
        if verdict == "readable":
            text = "The computer sees this drive and its first sector reads normally. You can scan and recover from it."
        elif verdict == "slow":
            text = (f"The drive answers, but its first sector took {self.read_seconds:.1f} seconds to read. Healthy "
                    "drives answer in a fraction of a second, so it is probably struggling (weak heads or bad "
                    "sectors). Make a disk image first, then recover from the image.")
        elif verdict == "damaged_start":
            text = (f"The drive reports its size ({format_capacity(self.size)}), but its first sector can't be "
                    f"read{error}. That sector holds the partition table, so {system_tool()} may call the disk "
                    "\"Not initialized\" or unreadable. Don't initialize or format it: that would overwrite what's "
                    "left. Make a disk image, then use Deep scan to find the partitions and files.")
        elif verdict == "not_answering" and self.size > 0:
            text = (f"The drive reports its size ({format_capacity(self.size)}), but didn't answer a read of its "
                    f"first sector{error}. It may be very weak or stuck. {_CABLE_STEPS} If it starts answering, "
                    "make a disk image right away.")
        elif verdict == "not_answering":
            text = ("The computer detects this drive, but the drive itself doesn't answer: it doesn't report its "
                    f"size, so {system_tool()} can't use it and nothing can read it yet. "
                    f"{_CABLE_STEPS} If it still doesn't answer, the drive has a hardware or firmware fault and "
                    "needs a recovery lab. Don't open it.")
        elif verdict == "no_media":
            text = ("This is a card reader or removable-media drive with nothing readable in it. If a card is "
                    "inserted, try another reader: a card that no reader detects has failed.")
        elif verdict == "adapter_only":
            text = ("The USB adapter or enclosure works, but no drive answers behind it. Check that the drive gets "
                    "power (many 3.5\" drives need the enclosure's own power supply), reseat it, or try another "
                    "enclosure or dock. If no adapter detects it, or it clicks, beeps or doesn't spin, it needs a "
                    "recovery lab.")
        elif verdict == "device_error":
            text = (f"{self.problem_text or 'Windows reports a problem with this device.'} This is usually a cable, "
                    "port or power problem: unplug the device, wait 10 seconds, and connect it to a port on the "
                    "back of the computer, ideally through a powered hub. If the error comes back, the adapter or "
                    "the drive is faulty.")
        elif verdict == "in_use":
            text = ("Lifeboat is reading this drive right now, so the check left it alone. Run the check again "
                    "when the job is finished to test it.")
        elif self.read_error:  # not_tested: opening it for reading was refused
            text = (f"The drive reports its size ({format_capacity(self.size)}), but the system refused to let "
                    f"Lifeboat open it for reading. Run Lifeboat {_elevated()}; if it already runs that way, "
                    "security software may be blocking direct disk access.")
        else:  # not_tested
            text = (f"Lifeboat isn't running {_elevated()}, so it could only check that the drive reports its "
                    f"size. Restart Lifeboat {_elevated()} to test reading it.")
        if self.health == "Bad":
            text += " Its own health check (SMART) says it is failing: copy what matters first."
        elif self.health == "Caution":
            text += " Its own health check (SMART) reports warnings."
        return text

    def to_dict(self) -> dict:
        data = asdict(self)
        data.update(verdict=self.verdict, headline=self.headline, level=self.level,
                    seen_by_system=self.seen_by_system, system_text=self.system_text, read_text=self.read_text,
                    advice=self.advice)
        return data

    def report_lines(self) -> list[str]:
        """The result as plain text, for the copied report and the CLI."""
        lines = [f"{self.name}", f"  Result: {self.headline}"]
        if self.kind == "disk":
            lines.append(f"  Size: {format_capacity(self.size) if self.size else 'not reported'}")
            lines.append(f"  In {system_tool()}: {self.system_text}")
            lines.append(f"  First sector: {self.read_text}" + (f" ({self.read_error})" if self.read_error else ""))
        for label, value in (("Connection", self.connection), ("Path", self.path), ("Serial", self.serial),
                             ("Health (SMART)", self.health), ("Device ID", self.instance_id)):
            if value:
                lines.append(f"  {label}: {value}")
        if self.problem_code:
            lines.append(f"  Device Manager: {self.problem_text}")
        lines.append(f"  What to do: {self.advice}")
        return lines


Progress = Callable[[int, int, str], None]


class TimedOut(Exception):
    pass


def with_timeout(func: Callable[[], T], seconds: float) -> T:
    """Run ``func`` on a helper thread; give up on it after ``seconds``.

    A drive stuck in a request can't be interrupted, so the helper thread is left behind (a daemon
    thread: it ends when the drive answers or the program exits). Give it its own copies of
    anything it changes, so an answer that comes too late changes nothing.
    """
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["value"] = func()
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller
            box["error"] = exc

    thread = threading.Thread(target=run, name="lifeboat-check", daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise TimedOut
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["value"]  # type: ignore[return-value]


def _read_test(opener: Callable[[str], BlockDevice], path: str) -> CheckResult:
    found = CheckResult(name="")
    try:
        device = opener(path)
    except DeviceOpenError as exc:
        open_failed(found, exc)
        return found
    except OSError as exc:
        found.read_ok, found.read_error, found.read_error_kind = False, str(exc), "gone"
        return found
    try:
        try_first_sector(device, found)
        health_of = getattr(device, "health", None)
        if health_of is not None:
            try:
                health = health_of()
            except OSError:
                health = None
            if health is not None:
                found.health = health.status
    finally:
        device.close()
    return found


def read_test(opener: Callable[[str], BlockDevice], result: CheckResult) -> None:
    """Open the drive and read its first sector, within a time limit even if opening it hangs."""
    limit = READ_TIMEOUT + PROBE_TIMEOUT
    try:
        found = with_timeout(lambda: _read_test(opener, result.path), limit)
    except TimedOut:
        result.read_ok, result.read_error_kind = False, "timeout"
        result.read_error = f"it didn't answer within {plural(round(limit), 'second')}"
        return
    result.tested, result.read_ok, result.read_seconds = found.tested, found.read_ok, found.read_seconds
    result.read_error, result.read_error_kind = found.read_error, found.read_error_kind
    result.health = found.health or result.health  # keep a health status found another way


def open_failed(result: CheckResult, exc: DeviceOpenError) -> None:
    """Record why a drive couldn't be opened for the read test."""
    result.read_error = exc.message
    if exc.code == E_OPEN_DENIED:
        result.tested = False      # not allowed to read it: that says nothing about the drive
    else:
        result.read_ok, result.read_error_kind = False, "gone"


def try_first_sector(device: BlockDevice, result: CheckResult) -> None:
    """One small, read-only read at the start of the drive, with a time limit."""
    length = max(4096, device.sector_size)
    started = time.monotonic()
    try:
        device.read_raw(0, length, timeout=READ_TIMEOUT)
        result.read_ok = True
    except ReadError as err:
        result.read_ok = False
        result.read_error = err.message
        result.read_error_kind = err.kind.value
    except (DeviceHungError, DeviceGoneError) as err:
        result.read_ok = False
        result.read_error = str(err)
        result.read_error_kind = "timeout" if isinstance(err, DeviceHungError) else "gone"
    result.read_seconds = time.monotonic() - started


def run_check(progress: Progress | None = None, in_use: Mapping[str, int] | None = None) -> list[CheckResult]:
    """Check every storage device this computer knows about.

    ``in_use`` maps the path of each drive Lifeboat is reading right now to its size: those drives
    are listed but not touched.
    """
    busy = dict(in_use or {})
    if sys.platform == "win32":
        from .check_windows import check_windows

        results = check_windows(progress, busy)
    elif sys.platform == "darwin":
        from .check_mac import check_mac

        results = check_mac(progress, busy)
    elif sys.platform.startswith("linux"):
        results = _check_linux(progress, busy)
    else:
        results = []
    order = {"disk": 0, "adapter": 1, "device": 2}
    results.sort(key=lambda r: (order.get(r.kind, 3), r.disk_number if r.disk_number is not None else 999, r.name))
    return results


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return ""


def report_text(results: list[CheckResult]) -> str:
    """Every result as plain text, for the clipboard, a support request or the CLI."""
    blocks = ["\n".join(r.report_lines()) for r in results]
    return "\n\n".join(blocks) if blocks else "No storage devices found."


def _open_linux(path: str) -> BlockDevice:
    from .posix import PosixDevice

    return PosixDevice(path)


def _check_linux(progress: Progress | None, in_use: Mapping[str, int]) -> list[CheckResult]:
    base = SYSFS / "block"
    if not base.is_dir():
        return []
    root = getattr(os, "geteuid", lambda: 1)() == 0
    entries: list[tuple[Path, int]] = []
    for entry in sorted(base.iterdir()):
        if re.match(r"^(ram|zram|sr|fd|md|dm-|nbd)", entry.name):
            continue
        sectors = _read(entry / "size")
        size = int(sectors) * 512 if sectors.isdigit() else 0
        if entry.name.startswith("loop") and not size:
            continue  # an unused loop device
        entries.append((entry, size))
    results: list[CheckResult] = []
    for index, (entry, size) in enumerate(entries):
        name = entry.name
        vendor = _read(entry / "device" / "vendor")
        vendor = "" if vendor.startswith("0x") else vendor  # a PCI vendor number (virtio), not a name
        model = " ".join(p for p in (vendor, _read(entry / "device" / "model")) if p)
        resolved = str(entry.resolve())
        bus = "USB" if "/usb" in resolved else "NVMe" if "nvme" in name else "Loop" if name.startswith("loop") \
            else "SATA" if "/ata" in resolved else ""
        result = CheckResult(name=model or name, path=f"/dev/{name}", size=size, connection=bus,
                             removable_media=_read(entry / "removable") == "1")
        if progress is not None:
            progress(index, len(entries), result.name)
        if result.path in in_use:
            result.in_use, result.tested = True, False
        elif size:
            if root:
                read_test(_open_linux, result)
            else:
                result.tested = False
        results.append(result)
    if progress is not None:
        progress(len(entries), len(entries), "")
    results.extend(_linux_usb_adapters())
    return results


def _linux_usb_adapters() -> list[CheckResult]:
    """USB mass-storage interfaces with no disk behind them."""
    out: list[CheckResult] = []
    for link in sorted((SYSFS / "bus" / "usb" / "devices").glob("*:*")):
        iface = link.resolve()
        if _read(iface / "bInterfaceClass") != "08":
            continue
        if any(iface.glob("host*/target*/*/block/*")):
            continue
        parent = iface.parent
        name = " ".join(p for p in (_read(parent / "manufacturer"), _read(parent / "product")) if p)
        out.append(CheckResult(name=name or "USB storage device", kind="adapter", connection="USB",
                               instance_id=link.name))
    return out
