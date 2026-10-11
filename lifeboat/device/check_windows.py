"""Drive check on Windows: Device Manager's list, plus a probe of every disk it shows.

Every call that reaches the drive runs on a helper thread with a time limit, so a drive that
hangs instead of answering is reported as not answering and the check moves on.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping

if sys.platform != "win32":  # pragma: no cover - imported only on Windows
    raise ImportError("lifeboat.device.check_windows is only available on Windows")

from ..errors import DeviceOpenError
from .check import PROBE_TIMEOUT, CheckResult, Progress, TimedOut, read_test, with_timeout
from .windows import (
    CloseHandle,
    WindowsDevice,
    _Descriptor,
    _geometry_ex,
    _length_info,
    _open,
    _storage_descriptor,
    is_admin,
)

log = logging.getLogger("lifeboat.device")

_ENUMERATORS = {"USBSTOR": "USB", "NVME": "NVMe", "SCSI": "SCSI", "IDE": "ATA", "SD": "SD card"}


def _probe_disk(path: str) -> tuple[int, _Descriptor]:
    """Size and description of a disk: works without administrator rights."""
    handle = _open(path, 0)
    try:
        geo = _geometry_ex(handle)
        size = geo[0] if geo and geo[0] else (_length_info(handle) or 0)
        return max(0, int(size)), _storage_descriptor(handle)
    finally:
        CloseHandle(handle)


def _check_disk(result: CheckResult, admin: bool) -> None:
    # The helper threads work on their own copies, so one that answers too late changes nothing.
    try:
        size, desc = with_timeout(lambda: _probe_disk(result.path), PROBE_TIMEOUT)
    except TimedOut:
        result.read_ok, result.read_error_kind = False, "timeout"
        result.read_error = f"it didn't answer within {PROBE_TIMEOUT:.0f} seconds"
        return
    except DeviceOpenError as exc:
        log.info("Drive check: cannot open %s: %s", result.path, exc)
        result.read_ok, result.read_error, result.read_error_kind = False, exc.message, "gone"
        return
    result.size = size
    result.serial = result.serial or desc.serial
    result.removable_media = result.removable_media or desc.removable
    if desc.bus and not result.connection.startswith("USB"):
        result.connection = desc.bus
    if not result.name:
        result.name = " ".join(p for p in (desc.vendor, desc.product) if p)
    if size <= 0:
        return
    if not admin:
        result.tested = False
        return
    read_test(WindowsDevice, result)


def _connection(enumerator: str, service: str) -> str:
    if service.lower() == "uaspstor":
        return "USB (UAS)"
    if service.lower() == "usbstor":
        return "USB"
    return _ENUMERATORS.get(enumerator.upper(), enumerator)


def check_windows(progress: Progress | None = None, in_use: Mapping[str, int] | None = None) -> list[CheckResult]:
    from .pnp_windows import PnpDevice, storage_devices

    busy = in_use or {}
    admin = is_admin()
    try:
        pnp: list[PnpDevice] = storage_devices()
    except Exception:  # the disks can still be checked one by one
        log.exception("Drive check: Windows did not return its device list; checking disks directly")
        pnp = []

    results: list[CheckResult] = []
    disk_ids = {d.instance_id for d in pnp if d.is_disk}
    numbers_seen: set[int] = set()
    for dev in pnp:
        if dev.is_disk:
            result = CheckResult(name=dev.name, connection=_connection(dev.enumerator, dev.service),
                                 instance_id=dev.instance_id, problem_code=dev.problem,
                                 disk_number=dev.disk_number)
            if dev.disk_number is not None:
                result.path = f"\\\\.\\PhysicalDrive{dev.disk_number}"
                numbers_seen.add(dev.disk_number)
            results.append(result)
        elif dev.is_usb_storage:
            if dev.problem or not any(child in disk_ids for child in dev.children):
                results.append(CheckResult(name=dev.name, kind="adapter",
                                           connection=_connection(dev.enumerator, dev.service),
                                           instance_id=dev.instance_id, problem_code=dev.problem))
        else:
            results.append(CheckResult(name=dev.name, kind="device", connection=_connection(dev.enumerator, ""),
                                       instance_id=dev.instance_id, problem_code=dev.problem))

    # Disks Windows has a disk number for but that weren't in the device list (or the list failed).
    misses = 0
    for number in range(128):
        if number in numbers_seen:
            continue
        path = f"\\\\.\\PhysicalDrive{number}"
        try:
            CloseHandle(_open(path, 0))
        except DeviceOpenError:
            misses += 1
            if misses > 32 and number > 32:
                break
            continue
        results.append(CheckResult(name="", path=path, disk_number=number))

    total = len(results)
    for index, result in enumerate(results):
        if progress is not None:
            progress(index, total, result.name or result.path)
        if result.kind == "disk" and result.path in busy:
            result.in_use, result.tested, result.size = True, False, busy[result.path]
        elif result.kind == "disk" and result.path and not result.problem_code:
            _check_disk(result, admin)
        if not result.name:
            result.name = f"Disk {result.disk_number}" if result.disk_number is not None else "Unknown device"
    if progress is not None:
        progress(total, total, "")
    return results
