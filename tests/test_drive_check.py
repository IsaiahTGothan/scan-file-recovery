"""Drive check: verdicts and advice, the Linux probe on a fake /sys tree, and the drive list."""

from __future__ import annotations

import json
import os

import pytest

from lifeboat.device import check
from lifeboat.device.base import NO_MEDIA, NOT_ANSWERING, DeviceInfo
from lifeboat.device.check import VERDICTS, CheckResult, report_text, try_first_sector
from lifeboat.device.image import MemoryDevice
from lifeboat.device.simulated import FaultPlan, SimulatedFailingDevice
from lifeboat.errors import E_OPEN_DENIED, DeviceOpenError

TB5 = 5_000_981_078_016


def test_every_verdict_has_a_headline_and_advice():
    cases = {
        "readable": CheckResult("a", size=TB5, read_ok=True, read_seconds=0.02),
        "slow": CheckResult("b", size=TB5, read_ok=True, read_seconds=4.5),
        "damaged_start": CheckResult("c", size=TB5, read_ok=False, read_error_kind="media"),
        "not_answering": CheckResult("d", size=0),
        "no_media": CheckResult("e", size=0, removable_media=True),
        "adapter_only": CheckResult("f", kind="adapter", connection="USB"),
        "device_error": CheckResult("g", kind="device", problem_code=43),
        "not_tested": CheckResult("h", size=TB5, tested=False),
        "in_use": CheckResult("i", size=TB5, in_use=True, tested=False),
    }
    assert set(cases) == set(VERDICTS)
    for verdict, result in cases.items():
        assert result.verdict == verdict
        assert result.headline == VERDICTS[verdict][0]
        assert len(result.advice) > 40
        json.dumps(result.to_dict())  # the CLI prints it as JSON


def test_what_disk_management_would_show():
    assert CheckResult("a", size=TB5, read_ok=True).system_text == "Yes"
    assert CheckResult("b", size=0).system_text == "No"
    assert CheckResult("c", size=0, removable_media=True).system_text == 'As "No Media"'
    assert CheckResult("d", kind="adapter").system_text == "No"
    # A disk Windows stopped (Code 10) isn't usable, even if it reported a size earlier.
    assert not CheckResult("e", size=TB5, problem_code=10).seen_by_system


def test_advice_matches_the_situation():
    timeout = CheckResult("a", size=TB5, read_ok=False, read_error="no answer", read_error_kind="timeout")
    assert timeout.verdict == "not_answering" and "5.0 TB" in timeout.advice
    gone = CheckResult("b", size=TB5, read_ok=False, read_error_kind="gone")
    assert gone.verdict == "not_answering"
    other = CheckResult("c", size=TB5, read_ok=False, read_error_kind="other")
    assert other.verdict == "damaged_start" and "Don't initialize" in other.advice
    code43 = CheckResult("d", kind="device", problem_code=43)
    assert "(Code 43)" in code43.advice and "Windows stopped it" in code43.problem_text
    unknown = CheckResult("e", kind="device", problem_code=99)
    assert "(Code 99)" in unknown.problem_text
    failing = CheckResult("f", size=TB5, read_ok=True, read_seconds=0.01, health="Bad")
    assert failing.verdict == "readable" and failing.level == "warn" and "SMART" in failing.advice
    refused = CheckResult("g", size=TB5, tested=False, read_error="Access denied")
    assert refused.verdict == "not_tested" and "refused" in refused.advice


def test_report_text_lists_everything():
    results = [CheckResult("Seagate Portable", size=0, connection="USB", path=r"\\.\PhysicalDrive3",
                           serial="NAAB1234", instance_id="USBSTOR\\DISK&VEN_SEAGATE"),
               CheckResult("JMicron bridge", kind="adapter", connection="USB")]
    text = report_text(results)
    for expected in ("Seagate Portable", "Detected, not answering", "Size: not reported", "NAAB1234",
                     "JMicron bridge", "Adapter only, no drive", "What to do:"):
        assert expected in text
    assert report_text([]) == "No storage devices found."


def test_first_sector_read_on_simulated_drives():
    healthy = MemoryDevice(bytes(1 << 20))
    result = CheckResult("healthy", size=healthy.size)
    try_first_sector(healthy, result)
    assert result.read_ok and result.verdict == "readable"

    bad = SimulatedFailingDevice(MemoryDevice(bytes(1 << 20)), FaultPlan(bad=[(0, 512)]))
    result = CheckResult("bad start", size=bad.size)
    try_first_sector(bad, result)
    assert result.read_ok is False and result.read_error_kind == "media" and result.verdict == "damaged_start"

    hung = SimulatedFailingDevice(MemoryDevice(bytes(1 << 20)), FaultPlan(hang=[(0, 4096)]))
    result = CheckResult("hung", size=hung.size)
    old = check.READ_TIMEOUT
    check.READ_TIMEOUT = 0.2
    try:
        try_first_sector(hung, result)
    finally:
        check.READ_TIMEOUT = old
    assert result.verdict == "not_answering" and result.read_error_kind == "timeout"

    slow = SimulatedFailingDevice(MemoryDevice(bytes(1 << 20)), FaultPlan(slow=[(0, 4096)], slow_delay=0.3))
    result = CheckResult("slow", size=slow.size)
    old_slow = check.SLOW_SECONDS
    check.SLOW_SECONDS = 0.1
    try:
        try_first_sector(slow, result)
        assert result.verdict == "slow"
    finally:
        check.SLOW_SECONDS = old_slow


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def fake_sys(tmp_path, monkeypatch):
    """A /sys tree with: a USB drive that reports no size, an empty card reader, a readable drive, a drive
    whose first sector is bad, a drive Lifeboat may not open, an unused loop device, and a USB bridge with
    no drive behind it."""
    sys_root = tmp_path / "sys"
    usb = sys_root / "devices" / "pci0000:00" / "usb2" / "2-1" / "2-1:1.0"
    for name, size, removable, model, under_usb in (
            ("sdb", "0", "0", "Portable", True), ("sdc", "0", "1", "SD/MMC Reader", True),
            ("sdd", "2048", "0", "Healthy", False), ("sde", "2048", "0", "BadStart", False),
            ("sdf", "2048", "0", "Locked", False), ("loop0", "0", "0", "", False)):
        real = (usb / "host6" / "target6:0:0" / "6:0:0:0" / "block" / name) if under_usb \
            else (sys_root / "devices" / "virtual" / "block" / name)
        _write(real / "size", size)
        _write(real / "removable", removable)
        if model:
            _write(real / "device" / "model", model)
            _write(real / "device" / "vendor", "Seagate" if name == "sdb" else "ATA")
        link = sys_root / "block" / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(real)
    _write(usb / "bInterfaceClass", "08")
    bridge = sys_root / "devices" / "pci0000:00" / "usb2" / "2-2" / "2-2:1.0"
    _write(bridge / "bInterfaceClass", "08")
    _write(bridge.parent / "manufacturer", "JMicron")
    _write(bridge.parent / "product", "USB to ATA/ATAPI Bridge")
    hid = sys_root / "devices" / "pci0000:00" / "usb2" / "2-3" / "2-3:1.0"
    _write(hid / "bInterfaceClass", "03")  # a keyboard: not storage
    links = sys_root / "bus" / "usb" / "devices"
    links.mkdir(parents=True)
    for target in (usb, bridge, hid):
        (links / target.name).symlink_to(target)

    from lifeboat.device import posix

    monkeypatch.setattr(check, "SYSFS", sys_root)
    monkeypatch.setattr(posix, "SYSFS", sys_root)
    return sys_root


@pytest.mark.skipif(not os.sys.platform.startswith("linux"), reason="Linux /sys layout")
def test_linux_check_on_a_fake_sys_tree(fake_sys, monkeypatch):
    def fake_open(path):
        if path.endswith("sdf"):
            raise DeviceOpenError(f"Permission denied opening {path}.", code=E_OPEN_DENIED)
        if path.endswith("sde"):
            return SimulatedFailingDevice(MemoryDevice(bytes(2048 * 512)), FaultPlan(bad=[(0, 512)]))
        return MemoryDevice(bytes(2048 * 512))

    monkeypatch.setattr(check, "_open_linux", fake_open)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    seen = []
    results = check.run_check(lambda i, n, name: seen.append((i, n, name)))
    by_path = {r.path: r for r in results if r.kind == "disk"}
    assert set(by_path) == {"/dev/sdb", "/dev/sdc", "/dev/sdd", "/dev/sde", "/dev/sdf"}  # loop0 left out
    assert by_path["/dev/sdb"].verdict == "not_answering" and by_path["/dev/sdb"].connection == "USB"
    assert by_path["/dev/sdb"].name == "Seagate Portable"
    assert by_path["/dev/sdc"].verdict == "no_media"
    assert by_path["/dev/sdd"].verdict == "readable"
    assert by_path["/dev/sde"].verdict == "damaged_start"
    assert by_path["/dev/sdf"].verdict == "not_tested" and "Permission denied" in by_path["/dev/sdf"].read_error
    adapters = [r for r in results if r.kind == "adapter"]
    assert [a.name for a in adapters] == ["JMicron USB to ATA/ATAPI Bridge"]
    assert adapters[0].verdict == "adapter_only"
    assert results[-1].kind == "adapter"  # disks first
    assert seen and seen[0][1] == 5

    # A drive a job is reading is listed but not touched.
    touched = []
    monkeypatch.setattr(check, "_open_linux", lambda path: touched.append(path) or MemoryDevice(bytes(4096)))
    results = check.run_check(in_use={"/dev/sdd": 2048 * 512})
    busy = next(r for r in results if r.path == "/dev/sdd")
    assert busy.verdict == "in_use" and "/dev/sdd" not in touched


@pytest.mark.skipif(not os.sys.platform.startswith("linux"), reason="Linux /sys layout")
def test_drives_without_a_size_stay_in_the_drive_list(fake_sys):
    from lifeboat.device.posix import list_devices

    devices = {d.path: d for d in list_devices()}
    assert devices["/dev/sdb"].status == NOT_ANSWERING and not devices["/dev/sdb"].readable
    assert devices["/dev/sdc"].status == NO_MEDIA
    assert devices["/dev/sdd"].status == "" and devices["/dev/sdd"].readable
    assert "/dev/loop0" not in devices
    assert "Drive check" in devices["/dev/sdb"].notes[0]


def test_device_status_is_part_of_the_drive_info():
    info = DeviceInfo(path="/dev/sdx", kind="disk", size=0, model="Portable", status=NOT_ANSWERING)
    assert not info.readable
    assert DeviceInfo(path="/dev/sdy", kind="disk", size=1 << 30).readable
