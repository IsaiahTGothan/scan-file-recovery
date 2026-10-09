"""Imaging tests: clone a failing (simulated) drive and resume from the mapfile."""

from __future__ import annotations

import os

from lifeboat.device.base import DeviceInfo
from lifeboat.device.image import ImageDevice, MemoryDevice
from lifeboat.device.simulated import FaultPlan, SimulatedFailingDevice
from lifeboat.events import EventBus, JobControl
from lifeboat.imaging import ImagingJob, ImagingOptions
from lifeboat.rescue.reader import ReadPolicy, RescueReader
from lifeboat.rescue.sectormap import SectorMap, State
from lifeboat.scan.scanner import Scanner
from tests.conftest import image_path, manifest, sha256
from tests.helpers import find, is_complete, read_all, tree_index

MiB = 1 << 20


def _job(device, out, control=None, progress=None, thoroughness="standard"):
    reader = RescueReader(device, ReadPolicy(timeout=1.0, block_size=256 * 1024, unit=4096), events=EventBus())
    info = DeviceInfo(path="sim", kind="image", size=device.size)
    return ImagingJob(reader, info, ImagingOptions(str(out), thoroughness=thoroughness), events=EventBus(),
                      control=control, progress=progress)


def test_image_of_failing_drive(tmp_path):
    data = os.urandom(16 * MiB)
    bad = [(5 * MiB, 5 * MiB + 4096), (9 * MiB + 8192, 9 * MiB + 16384)]
    dev = SimulatedFailingDevice(MemoryDevice(data), FaultPlan(bad=bad))
    out = tmp_path / "disk.img"
    summary = _job(dev, out).run()
    assert summary.outcome == "warning"
    assert summary.bad == 4096 + 8192
    image = out.read_bytes()
    assert len(image) == len(data)
    expected = bytearray(data)
    for s, e in bad:
        expected[s:e] = bytes(e - s)
    assert image == bytes(expected)
    sm = SectorMap.load(str(out) + ".map", len(data))
    assert sm.ranges([State.BAD]) == bad
    # every bad sector was tried only a few times
    for s, e in bad:
        for sector in range(s, e, 512):
            assert dev.reads_touching(sector, sector + 512) <= 4


def test_imaging_resumes_from_mapfile(tmp_path):
    data = os.urandom(32 * MiB)
    out = tmp_path / "disk.img"
    control = JobControl()

    def progress(p):
        if p.done >= 12 * MiB:
            control.cancel()

    slow = SimulatedFailingDevice(MemoryDevice(data), FaultPlan(slow=[(0, len(data))], slow_delay=0.01))
    first = _job(slow, out, control=control, progress=progress).run()
    assert first.cancelled and 0 < first.good < len(data)
    dev = SimulatedFailingDevice(MemoryDevice(data), FaultPlan())
    second = _job(dev, out).run()
    assert second.outcome == "success"
    assert out.read_bytes() == data
    # resumed: the already rescued part was not read again
    assert dev.reads_touching(0, first.good - 4 * MiB) == 0


def test_recover_files_from_image_with_mapfile(images, tmp_path):
    """Image an NTFS drive with a bad area, then scan the image: the damaged
    file must be reported as damaged, everything else intact."""
    inner = ImageDevice(image_path("ntfs"))
    reader = RescueReader(inner, ReadPolicy(timeout=1.0))
    info = DeviceInfo(path="ntfs", kind="image", size=inner.size)
    clean = Scanner(reader, info).quick_scan()
    target = find(tree_index(clean.volumes[0].root), "Photos/IMG_0001.JPG")
    disk0 = target.volume.layout(target).extents[0].disk_offset
    bad = (disk0 + 8192, disk0 + 12288)
    dev = SimulatedFailingDevice(ImageDevice(image_path("ntfs")), FaultPlan(bad=[bad]))
    out = tmp_path / "ntfs-copy.img"
    summary = _job(dev, out).run()
    assert summary.bad == 4096
    sm = SectorMap.load(str(out) + ".map", inner.size)
    unreadable = sm.ranges([State.BAD, State.FAILED, State.SKIPPED, State.UNTRIED])
    image = ImageDevice(out, unreadable=unreadable)
    reader2 = RescueReader(image, ReadPolicy(timeout=1.0))
    result = Scanner(reader2, DeviceInfo(path=str(out), kind="image", size=image.size)).quick_scan()
    index = tree_index(result.volumes[0].root)
    node = find(index, "Photos/IMG_0001.JPG")
    data, states = read_all(result.volumes[0].volume, node)
    assert not is_complete(states)
    other = find(index, "Photos/IMG_0002.JPG")
    data, states = read_all(result.volumes[0].volume, other)
    assert is_complete(states)
    entry = next(e for e in manifest("ntfs") if e["path"] == "Photos/IMG_0002.JPG")
    assert sha256(data) == entry["sha256"]
