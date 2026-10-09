"""4 KB sectors: native 4Kn drives and drives moved out of USB enclosures.

Many USB enclosures present 4096-byte sectors; the same drive in a SATA dock
shows 512-byte sectors (or the other way round).  The partition table was
written in the enclosure's units, so partition offsets look wrong.  Lifeboat
must notice and adjust.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess

import pytest

from lifeboat.device.base import DeviceInfo
from lifeboat.device.image import ImageDevice
from lifeboat.events import EventBus
from lifeboat.rescue.reader import ReadPolicy, RescueReader
from lifeboat.scan.scanner import Scanner
from tests.fixtures.build_images import Mounted, data
from tests.helpers import find, is_complete, read_all, tree_index

MiB = 1 << 20
NEEDS = ("mkntfs", "ntfs-3g", "sgdisk", "sfdisk", "losetup")


def _require() -> None:
    if os.name != "posix" or os.geteuid() != 0:
        pytest.skip("needs root (loop devices and FUSE)")
    missing = [tool for tool in NEEDS if shutil.which(tool) is None]
    if missing:
        if os.environ.get("LIFEBOAT_REQUIRE_IMAGES"):
            raise RuntimeError(f"missing tools: {missing}")
        pytest.skip(f"missing tools: {missing}")


FILES = {"a.txt": data("4k-a", 5), "photo.jpg": data("4k-photo", 300_000), "deep/dir/file.bin": data("4k-deep", 70_000)}


def _ntfs_4k(path, size_mb: int = 40) -> None:
    with open(path, "wb") as fh:
        fh.truncate(size_mb * MiB)
    subprocess.run(["mkntfs", "-F", "-q", "-f", "-s", "4096", "-c", "4096", "-L", "N4K", str(path)],
                   check=True, capture_output=True)
    with Mounted(path, "ntfs") as root:
        for rel, content in FILES.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)


def _scan(image, sector_size: int):
    device = ImageDevice(image, sector_size=sector_size)
    bus = EventBus()
    reader = RescueReader(device, ReadPolicy(timeout=1.0), events=bus)
    result = Scanner(reader, DeviceInfo(path=str(image), kind="image", size=device.size), events=bus).quick_scan()
    return result


def _check(result) -> None:
    vols = [v for v in result.volumes if v.volume is not None]
    assert len(vols) == 1, [v.title for v in result.volumes]
    assert vols[0].volume.kind == "NTFS"
    assert vols[0].volume.record_size == 4096
    index = tree_index(vols[0].root)
    for rel, content in FILES.items():
        node = find(index, rel)
        assert node is not None, rel
        payload, states = read_all(vols[0].volume, node)
        assert is_complete(states), rel
        assert hashlib.sha256(payload).digest() == hashlib.sha256(content).digest(), rel


@pytest.mark.parametrize("device_sector", [4096, 512])
def test_native_4k_ntfs(tmp_path, device_sector):
    _require()
    image = tmp_path / "n4k.img"
    _ntfs_4k(image)
    _check(_scan(image, device_sector))


def _disk_with_4k_table(tmp_path, scheme: str):
    fs = tmp_path / "fs.img"
    _ntfs_4k(fs)
    disk = tmp_path / f"{scheme}.img"
    with open(disk, "wb") as fh:
        fh.truncate(64 * MiB)
    loop = subprocess.run(["losetup", "-f", "--show", "-b", "4096", str(disk)], capture_output=True, text=True)
    if loop.returncode != 0:
        pytest.skip(f"cannot create a 4K loop device: {loop.stderr}")
    dev = loop.stdout.strip()
    try:
        size_sectors = os.path.getsize(fs) // 4096
        if scheme == "gpt":
            subprocess.run(["sgdisk", "-o", dev], check=True, capture_output=True)
            subprocess.run(["sgdisk", "-n", f"1:256:+{size_sectors - 1}", "-t", "1:0700", dev], check=True,
                           capture_output=True)
        else:
            subprocess.run(["sfdisk", "--no-reread", "--no-tell-kernel", dev],
                           input=f"label: dos\nstart=256, size={size_sectors}, type=7\n", text=True,
                           check=True, capture_output=True)
    finally:
        subprocess.run(["losetup", "-d", dev], check=False)
    with open(fs, "rb") as src, open(disk, "r+b") as dst:
        dst.seek(256 * 4096)
        shutil.copyfileobj(src, dst)
    return disk


@pytest.mark.parametrize("scheme", ["gpt", "mbr"])
def test_enclosure_table_read_in_a_dock(tmp_path, scheme):
    """Table written with 4096-byte sectors, drive now shows 512-byte sectors."""
    _require()
    disk = _disk_with_4k_table(tmp_path, scheme)
    result = _scan(disk, 512)
    assert result.table is not None and result.table.scheme == scheme.upper()
    assert result.table.sector_size == 4096
    assert any("4096-byte sectors" in w for w in result.table.warnings)
    _check(result)
    # And in the enclosure itself (4096-byte sectors) nothing needs adjusting.
    native = _scan(disk, 4096)
    assert not any("sectors" in w for w in native.table.warnings)
    _check(native)
