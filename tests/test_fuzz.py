"""Corruption fuzzing: damaged metadata must never crash or hang a scan.

Each case damages the filesystem structures of a real image in a random but
reproducible way (bit flips, zeroed sectors, garbage sectors, sectors
copied from elsewhere), scans it, and then computes the layout of - and
reads - every file found.  Any exception, an internal error event, or a scan
that takes too long is a failure.
"""

from __future__ import annotations

import random
import time

import pytest

from lifeboat.device.base import DeviceInfo
from lifeboat.device.image import MemoryDevice
from lifeboat.errors import E_INTERNAL
from lifeboat.events import EventBus, Level
from lifeboat.fs.content import FileContentReader
from lifeboat.fs.exfat import ExfatBoot
from lifeboat.fs.fat import FatBoot
from lifeboat.fs.model import F
from lifeboat.fs.ntfs import NtfsBoot
from lifeboat.rescue.reader import ReadMode, ReadPolicy, RescueReader
from lifeboat.scan.scanner import Scanner
from tests.conftest import image_path

CASES = 40


def metadata_regions(name: str, image: bytes) -> list[tuple[int, int]]:
    """Byte ranges holding filesystem structures worth damaging."""
    boot = image[:512]
    if name == "ntfs":
        nb = NtfsBoot.parse(boot)
        assert nb is not None
        mft = nb.mft_lcn * nb.cluster_size
        return [(0, 512), (mft, mft + 2200 * nb.record_size)]
    if name == "exfat":
        eb = ExfatBoot.parse(boot)
        assert eb is not None
        heap = eb.heap_offset << eb.bps_shift
        return [(0, 24 * 512), (eb.fat_offset << eb.bps_shift, heap), (heap, heap + (2 << 20))]
    fb = FatBoot.parse(boot)
    assert fb is not None
    data_start = fb.first_data_sector * fb.bytes_per_sector
    return [(0, data_start), (data_start, min(len(image), data_start + (2 << 20)))]


def corrupt(image: bytearray, regions: list[tuple[int, int]], rng: random.Random) -> None:
    for _ in range(rng.randint(1, 12)):
        start, end = rng.choice(regions)
        sector = rng.randrange(start // 512, max(start // 512 + 1, end // 512))
        pos = sector * 512
        kind = rng.random()
        if kind < 0.35:
            for _ in range(rng.randint(1, 16)):
                i = pos + rng.randrange(512)
                image[i] ^= 1 << rng.randrange(8)
        elif kind < 0.6:
            image[pos:pos + 512] = bytes(512)
        elif kind < 0.8:
            image[pos:pos + 512] = rng.randbytes(512)
        else:
            src = rng.randrange(0, len(image) // 512) * 512
            image[pos:pos + 512] = image[src:src + 512]


@pytest.mark.parametrize("name", ["ntfs", "exfat", "fat32", "fat16", "fat12"])
def test_corrupted_metadata_never_crashes(images, name):
    original = image_path(name).read_bytes()
    regions = metadata_regions(name, original)
    for case in range(CASES):
        rng = random.Random(f"{name}-{case}")
        image = bytearray(original)
        corrupt(image, regions, rng)
        device = MemoryDevice(bytes(image), name=f"{name}-fuzz-{case}")
        bus = EventBus()
        internal: list[str] = []
        bus.subscribe(lambda e, sink=internal: e.code == E_INTERNAL and sink.append(e.message + e.details))
        reader = RescueReader(device, ReadPolicy(timeout=1.0), events=bus)
        info = DeviceInfo(path=device.info.path, kind="image", size=device.size)
        started = time.monotonic()
        result = Scanner(reader, info, events=bus).quick_scan()
        elapsed = time.monotonic() - started
        assert elapsed < 60, f"case {case}: scan took {elapsed:.0f} s"
        assert not internal, f"case {case}: {internal[0]}"
        checked = 0
        for node in result.root.walk():
            if node.flags & F.DIR or node.volume is None:
                continue
            layout = node.volume.layout(node)
            if checked < 40 and layout.size < (8 << 20):
                FileContentReader(layout, reader).read(0, layout.size, ReadMode.FAST)
                checked += 1
        assert bus.counts[Level.CRITICAL] == 0, f"case {case}"


def test_garbage_disk_is_handled(images):
    """A drive full of noise: no partitions, no crash, deep scan finishes."""
    rng = random.Random(7)
    device = MemoryDevice(rng.randbytes(8 << 20), name="noise")
    bus = EventBus()
    reader = RescueReader(device, ReadPolicy(timeout=1.0), events=bus)
    info = DeviceInfo(path="noise", kind="image", size=device.size)
    result = Scanner(reader, info, events=bus).deep_scan()
    assert not [v for v in result.volumes if v.volume is not None and v.volume.kind != "Carved"]
