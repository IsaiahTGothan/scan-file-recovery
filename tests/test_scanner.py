"""Scanner tests on real images, including damaged ones."""

from __future__ import annotations

import shutil

from lifeboat.device.base import DeviceInfo
from lifeboat.device.image import ImageDevice
from lifeboat.device.simulated import FaultPlan, SimulatedFailingDevice
from lifeboat.events import Choice, EventBus, InterventionHandler, Level
from lifeboat.fs.model import F
from lifeboat.fs.ntfs import NtfsBoot
from lifeboat.rescue.reader import ReadPolicy, RescueReader
from lifeboat.scan.scanner import Scanner, ScanOptions
from tests.conftest import IMAGES, image_path, manifest, sha256
from tests.helpers import find, is_complete, read_all, tree_index


def scan(device, deep=False, options=None, handler=None, events=None):
    events = events or EventBus()
    reader = RescueReader(device, ReadPolicy(timeout=1.0), events=events)
    info = DeviceInfo(path="test", kind="image", size=device.size)
    scanner = Scanner(reader, info, events=events, interventions=handler, options=options)
    result = scanner.deep_scan() if deep else scanner.quick_scan()
    return result, events


def volume_index(result, kind):
    for vr in result.volumes:
        if vr.volume is not None and vr.volume.kind == kind:
            return vr, tree_index(vr.root)
    raise AssertionError(f"no {kind} volume in {[v.title for v in result.volumes]}")


def check_manifest(vr, index, name, deleted=False, limit=None):
    entries = [e for e in manifest(name) if e["deleted"] == deleted]
    if limit:
        entries = entries[:limit]
    for entry in entries:
        node = find(index, entry["path"], deleted=deleted)
        assert node is not None, entry["path"]
        data, states = read_all(vr.volume, node)
        assert is_complete(states), entry["path"]
        assert sha256(data) == entry["sha256"], entry["path"]


def copy(name, tmp_path):
    target = tmp_path / f"{name}.img"
    shutil.copyfile(image_path(name), target)
    return target


def test_mbr_disk_with_logical_partitions(images):
    result, _ = scan(ImageDevice(IMAGES / "mbr_disk.img"))
    assert result.table.scheme == "MBR"
    kinds = sorted(v.volume.kind for v in result.volumes if v.volume)
    assert kinds == ["FAT32", "NTFS", "exFAT"]
    offsets = images["mbr_disk"]["offsets"]
    for vr in result.volumes:
        assert vr.offset == offsets[{"FAT32": "fat32", "NTFS": "ntfs", "exFAT": "exfat"}[vr.volume.kind]]
    for kind, name in (("FAT32", "fat32"), ("NTFS", "ntfs"), ("exFAT", "exfat")):
        vr, index = volume_index(result, kind)
        check_manifest(vr, index, name, limit=60)
        check_manifest(vr, index, name, deleted=True)


def test_gpt_disk(images):
    result, _ = scan(ImageDevice(IMAGES / "gpt_disk.img"))
    assert result.table.scheme == "GPT"
    names = [e.name for e in result.table.entries]
    assert "Basic data partition" in names
    vr, index = volume_index(result, "NTFS")
    check_manifest(vr, index, "ntfs", limit=40)


def test_gpt_primary_damaged_uses_backup(images, tmp_path):
    disk = tmp_path / "gpt.img"
    shutil.copyfile(IMAGES / "gpt_disk.img", disk)
    with open(disk, "r+b") as fh:
        fh.seek(512 + 40)
        fh.write(b"\xde\xad\xbe\xef")  # breaks the header CRC
        fh.seek(1024)
        fh.write(bytes(4096))          # and the start of the entry array
    result, events = scan(ImageDevice(disk))
    assert result.table.scheme == "GPT"
    assert any("backup GPT" in w for w in result.table.warnings)
    vr, index = volume_index(result, "NTFS")
    check_manifest(vr, index, "ntfs", limit=20)


def test_ntfs_boot_sector_destroyed_uses_backup(images, tmp_path):
    img = copy("ntfs", tmp_path)
    with open(img, "r+b") as fh:
        fh.write(bytes(512))
    result, events = scan(ImageDevice(img))
    vr, index = volume_index(result, "NTFS")
    assert any("backup copy" in w for w in vr.volume.warnings)
    check_manifest(vr, index, "ntfs", limit=50)


def test_ntfs_mft_record0_destroyed_uses_mirror(images, tmp_path):
    img = copy("ntfs", tmp_path)
    boot = NtfsBoot.parse(img.read_bytes()[:512])
    with open(img, "r+b") as fh:
        fh.seek(boot.mft_lcn * boot.cluster_size)
        fh.write(bytes(1024))
    result, events = scan(ImageDevice(img))
    vr, index = volume_index(result, "NTFS")
    assert any("mirror" in w for w in vr.volume.warnings)
    check_manifest(vr, index, "ntfs")
    check_manifest(vr, index, "ntfs", deleted=True)


def test_ntfs_bad_sectors_inside_mft(images):
    inner = ImageDevice(image_path("ntfs"))
    boot = NtfsBoot.parse(inner.read_raw(0, 512))
    mft = boot.mft_lcn * boot.cluster_size
    bad = [(mft + 600 * 1024, mft + 600 * 1024 + 4096), (mft + 1200 * 1024, mft + 1210 * 1024)]
    dev = SimulatedFailingDevice(inner, FaultPlan(bad=bad))
    result, events = scan(dev)
    vr, index = volume_index(result, "NTFS")
    assert vr.volume.unreadable_records == 4 + 10
    assert any("unreadable" in w for w in vr.volume.warnings)
    # Everything described by readable records is intact.
    damaged_records = set(range(600, 604)) | set(range(1200, 1210))
    entries = [e for e in manifest("ntfs") if not e["deleted"]]
    missing = 0
    for entry in entries:
        node = find(index, entry["path"], deleted=False)
        if node is None:
            missing += 1
            continue
        data, states = read_all(vr.volume, node)
        assert is_complete(states)
        assert sha256(data) == entry["sha256"], entry["path"]
    assert 0 < missing <= len(damaged_records)
    assert events.counts[Level.WARNING] > 0
    # every bad sector was read only a few times despite all the passes
    for start, end in bad:
        for sector in range(start, end, 512):
            assert dev.reads_touching(sector, sector + 512) <= 4


def test_fat32_boot_sector_destroyed_uses_backup(images, tmp_path):
    img = copy("fat32", tmp_path)
    with open(img, "r+b") as fh:
        fh.write(bytes(512))
    result, _ = scan(ImageDevice(img))
    vr, index = volume_index(result, "FAT32")
    assert any("backup" in w for w in vr.volume.warnings)
    check_manifest(vr, index, "fat32", limit=60)


def test_fat_first_copy_unreadable_uses_second(images):
    inner = ImageDevice(image_path("fat32"))
    from lifeboat.fs.fat import FatBoot

    boot = FatBoot.parse(inner.read_raw(0, 512))
    fat1 = boot.reserved * boot.bytes_per_sector
    dev = SimulatedFailingDevice(inner, FaultPlan(bad=[(fat1, fat1 + boot.fat_size * boot.bytes_per_sector)]))
    result, _ = scan(dev)
    vr, index = volume_index(result, "FAT32")
    check_manifest(vr, index, "fat32")


def test_exfat_boot_sector_destroyed_uses_backup(images, tmp_path):
    img = copy("exfat", tmp_path)
    with open(img, "r+b") as fh:
        fh.write(bytes(512))
    result, _ = scan(ImageDevice(img))
    vr, index = volume_index(result, "exFAT")
    assert any("backup" in w for w in vr.volume.warnings)
    check_manifest(vr, index, "exfat", limit=40)


def test_deep_scan_finds_partitions_after_table_wiped(images, tmp_path):
    disk = tmp_path / "wiped.img"
    shutil.copyfile(IMAGES / "mbr_disk.img", disk)
    with open(disk, "r+b") as fh:
        fh.write(bytes(512))
        # also wipe the NTFS primary boot sector: deep scan must use the backup
        fh.seek(images["mbr_disk"]["offsets"]["ntfs"])
        fh.write(bytes(512))
    quick, _ = scan(ImageDevice(disk))
    assert not [v for v in quick.volumes if v.volume is not None]
    result, _ = scan(ImageDevice(disk), deep=True, options=ScanOptions(carve=False))
    found = {v.volume.kind: v for v in result.volumes if v.volume is not None}
    assert set(found) == {"FAT32", "NTFS", "exFAT"}
    offsets = images["mbr_disk"]["offsets"]
    assert found["NTFS"].offset == offsets["ntfs"]
    assert found["FAT32"].offset == offsets["fat32"]
    assert found["exFAT"].offset == offsets["exfat"]
    for kind, name in (("FAT32", "fat32"), ("NTFS", "ntfs"), ("exFAT", "exfat")):
        vr = found[kind]
        check_manifest(vr, tree_index(vr.root), name, limit=30)


class ReconnectingHandler(InterventionHandler):
    def __init__(self, device):
        super().__init__()
        self.device = device

    def request(self, intervention):
        self.history.append(intervention)
        self.device.reconnect()
        return Choice.RETRY


def test_scan_survives_disconnects(images):
    clean, _ = scan(ImageDevice(image_path("ntfs")))
    expected = sorted(n.path() for n in clean.root.walk())
    dev = SimulatedFailingDevice(ImageDevice(image_path("ntfs")), FaultPlan(disconnect_after_reads=5))
    handler = ReconnectingHandler(dev)
    result, events = scan(dev, handler=handler)
    assert handler.history, "the disconnect was not reported"
    assert sorted(n.path() for n in result.root.walk()) == expected
    assert events.counts[Level.CRITICAL] >= 1


def test_superfloppy_and_unsupported(images, tmp_path):
    result, _ = scan(ImageDevice(image_path("fat12")))
    assert result.table.scheme == "None"
    assert result.volumes[0].volume.kind == "FAT12"
    # A BitLocker volume is identified and explained, not misread.
    img = tmp_path / "bl.img"
    data = bytearray(image_path("fat12").read_bytes())
    data[3:11] = b"-FVE-FS-"
    img.write_bytes(bytes(data))
    result, events = scan(ImageDevice(img))
    assert result.volumes[0].volume is None
    assert result.volumes[0].root.flags & F.UNSUPPORTED
    assert any("BitLocker" in v.error for v in result.volumes)
