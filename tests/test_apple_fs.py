"""HFS+ and APFS on real disks made by macOS (tests/macos/make_fixtures.py, run by CI on a Mac).

Every file the manifest lists must be found with its exact content: compressed files, hard links,
the file in dozens of pieces, accented and emoji names. Deleted files must be found too, and
whatever content they come back with must be either exact or flagged as overwritten.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import unicodedata
from pathlib import Path

import pytest

from lifeboat.device.image import ImageDevice
from lifeboat.events import EventBus
from lifeboat.fs.content import FileContentReader
from lifeboat.fs.model import F, Node
from lifeboat.rescue.reader import ReadMode, ReadPolicy, RescueReader
from lifeboat.scan import Scanner
from lifeboat.scan.scanner import ScanResult

FIXTURES = Path(__file__).parent / "fixtures" / "apple"
HFS = ["hfsplus", "hfsx_apm", "hfsplus_bare"]


@pytest.fixture(scope="module")
def disks(tmp_path_factory) -> dict[str, tuple[Path, dict]]:
    out = {}
    folder = tmp_path_factory.mktemp("apple")
    for gz in sorted(FIXTURES.glob("*.img.gz")):
        name = gz.name[:-len(".img.gz")]
        image = folder / f"{name}.img"
        with gzip.open(gz, "rb") as src, open(image, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        out[name] = (image, json.loads((FIXTURES / f"{name}.json").read_text()))
    return out


def _scan(image: Path) -> tuple[ScanResult, RescueReader, list[str]]:
    device = ImageDevice(str(image))
    bus = EventBus()
    messages: list[str] = []
    bus.subscribe(lambda e: messages.append(e.message))
    reader = RescueReader(device, ReadPolicy(timeout=5), events=bus)
    return Scanner(reader, device.info, bus).quick_scan(), reader, messages


def _index(root: Node) -> dict[str, list[Node]]:
    out: dict[str, list[Node]] = {}
    for node in root.walk():
        if node is root:
            continue
        path = unicodedata.normalize("NFC", "/".join(node.path_parts()[1:]))
        out.setdefault(path, []).append(node)
    return out


def _content(result: ScanResult, reader: RescueReader, node: Node) -> tuple[bytes, bool]:
    assert node.volume is not None
    layout = node.volume.layout(node)
    chunk = FileContentReader(layout, reader).read(0, layout.size, ReadMode.FAST)
    return bytes(chunk.data), chunk.complete


def _check(name: str, disks: dict, deleted_required: bool) -> tuple[Node, dict[str, list[Node]]]:
    image, manifest = disks[name]
    result, reader, _messages = _scan(image)
    volumes = [v for v in result.volumes if v.volume is not None]
    assert len(volumes) == 1, [v.title for v in result.volumes]
    root = volumes[0].root
    assert root is not None
    index = _index(root)
    for item in manifest["files"]:
        path = unicodedata.normalize("NFC", item["path"])
        deleted = item.get("deleted", False)
        nodes = [n for n in index.get(path, []) if bool(n.flags & F.DELETED) == deleted and not n.flags & F.STREAM]
        if item.get("symlink"):
            assert nodes and nodes[0].flags & F.REPARSE, path
            continue
        if deleted and not deleted_required:
            continue
        assert nodes, f"not found: {path}"
        node = nodes[0]
        data, complete = _content(result, reader, node)
        exact = complete and len(data) == item["size"] and hashlib.sha256(data).hexdigest() == item["sha256"]
        if deleted:
            assert exact or node.flags & F.OVERWRITTEN, f"deleted file came back wrong without a warning: {path}"
        else:
            assert exact, f"wrong content: {path}"
            if item.get("compressed"):
                assert node.flags & F.COMPRESSED, path
            if item.get("hardlink_of"):
                assert node.flags & F.HARDLINK, path
        if item.get("rsrc_sha256"):
            fork = next(n for n in index[path + ":rsrc"] if n.flags & F.STREAM)
            data, complete = _content(result, reader, fork)
            assert complete and hashlib.sha256(data).hexdigest() == item["rsrc_sha256"], path
    return root, index


@pytest.mark.parametrize("name", HFS)
def test_hfsplus_files(disks, name):
    if name not in disks:
        pytest.skip("test disk not made yet")
    journaled = "Journaled" in disks[name][1]["disk"]["filesystem"]
    root, index = _check(name, disks, deleted_required=journaled)
    assert root.volume is not None and root.volume.kind == "HFS+"
    assert root.volume.label == disks[name][1]["disk"]["label"]
    if journaled:
        deleted = [n for nodes in index.values() for n in nodes if n.flags & F.DELETED and not n.flags & F.DIR]
        assert len(deleted) >= 25
        assert index["Deleted/folder"][0].flags & F.DELETED          # the deleted folder comes back too


def test_hfsplus_folder_hard_link(disks):
    """Time Machine's folder hard links show the linked folder's files under every link."""
    if "hfsplus" not in disks or disks["hfsplus"][1]["disk"].get("folder_hardlink") is not True:
        pytest.skip("macOS refused to make a folder hard link")
    _root, index = _check("hfsplus", disks, deleted_required=True)
    link = index["TM/other/folder link"][0]
    assert link.is_dir and link.flags & F.HARDLINK
    assert [c.name for c in link.children or []] == ["in linked folder.txt"]


def test_hfsplus_damaged_header_uses_backup(disks, tmp_path):
    """Zeroing the volume header: the copy at the end of the volume takes over."""
    if "hfsplus_bare" not in disks:
        pytest.skip("test disk not made yet")
    image = tmp_path / "damaged.img"
    shutil.copy(disks["hfsplus_bare"][0], image)
    with open(image, "r+b") as fh:
        fh.seek(1024)
        fh.write(bytes(512))
    result, _reader, messages = _scan(image)
    volume = next(v.volume for v in result.volumes if v.volume is not None)
    assert volume.kind == "HFS+"
    assert any("backup copy" in m for m in messages)
    assert sum(1 for _ in result.root.files()) > 400


def test_apfs_files(disks):
    if "apfs" not in disks:
        pytest.skip("test disk not made yet")
    root, index = _check("apfs", disks, deleted_required=False)
    assert root.volume is not None and root.volume.kind == "APFS"
    assert root.volume.label == "LBAPFS"
    assert index["Sparse/mostly hole.bin"][0].flags & F.SPARSE
    assert index["Docs/hard link to report.txt"][0].flags & F.HARDLINK
    # Files deleted in the last transactions come back from the container's older checkpoints.
    deleted = {path for path, nodes in index.items() for n in nodes if n.flags & F.DELETED and not n.flags & F.DIR}
    assert {"Deleted/small note.txt", "Deleted/folder/photo.jpg"} <= deleted


def test_apfs_damaged_superblock_uses_checkpoint(disks, tmp_path):
    """Wiping block 0 of the container: the newest superblock in the checkpoint area takes over."""
    if "apfs" not in disks:
        pytest.skip("test disk not made yet")
    image = tmp_path / "damaged.img"
    shutil.copy(disks["apfs"][0], image)
    result, _reader, _messages = _scan(image)
    start = next(v.offset for v in result.volumes if v.volume is not None)
    with open(image, "r+b") as fh:
        fh.seek(start + 100)
        fh.write(b"\xff" * 64)                                   # breaks block 0's checksum
    result, _reader, messages = _scan(image)
    volume = next(v.volume for v in result.volumes if v.volume is not None)
    assert volume.kind == "APFS"
    assert any("checkpoint" in m for m in messages)
    assert sum(1 for _ in result.root.files()) > 400


def test_apfs_deep_scan_finds_deleted_files(disks):
    """Deep Scan rebuilds deleted APFS files from the old tree nodes APFS leaves in free space."""
    if "apfs" not in disks:
        pytest.skip("test disk not made yet")
    image, manifest = disks["apfs"]
    device = ImageDevice(str(image))
    reader = RescueReader(device, ReadPolicy(timeout=5))
    from lifeboat.scan.scanner import ScanOptions

    result = Scanner(reader, device.info, options=ScanOptions(carve=False)).deep_scan()
    index = _index(result.root)
    wanted = [f for f in manifest["files"] if f.get("deleted")]
    found = exact = 0
    for item in wanted:
        nodes = [n for n in index.get(unicodedata.normalize("NFC", item["path"]), []) if n.flags & F.DELETED]
        if not nodes:
            continue
        found += 1
        data, complete = _content(result, reader, nodes[0])
        if complete and hashlib.sha256(data).hexdigest() == item["sha256"]:
            exact += 1
        else:
            assert nodes[0].flags & F.OVERWRITTEN, item["path"]
    assert found >= len(wanted) - 2 and exact >= len(wanted) - 4, (found, exact, len(wanted))


@pytest.mark.parametrize("name", ["hfsplus", "apfs"])
def test_wiped_start_of_drive(disks, tmp_path, name):
    """The first 40 KB wiped (partition table and the start of the volume): a normal scan rebuilds
    from the backup GPT and the volume's copies; with the backup GPT gone too, Deep Scan finds it."""
    if name not in disks:
        pytest.skip("test disk not made yet")
    image = tmp_path / f"{name}.img"
    shutil.copy(disks[name][0], image)
    with open(image, "r+b") as fh:
        fh.write(bytes(40960))
    result, _reader, _messages = _scan(image)
    assert sum(1 for _ in result.root.files()) > 400
    size = image.stat().st_size
    with open(image, "r+b") as fh:            # the backup GPT: the last 34 sectors
        fh.seek(size - 34 * 512)
        fh.write(bytes(34 * 512))
    device = ImageDevice(str(image))
    from lifeboat.scan.scanner import ScanOptions

    result = Scanner(RescueReader(device, ReadPolicy(timeout=5)), device.info,
                     options=ScanOptions(carve=False)).deep_scan()
    lost = [v for v in result.volumes if v.found_by_deep_scan and v.volume is not None]
    assert lost and lost[0].volume.kind == ("APFS" if name == "apfs" else "HFS+")
    assert sum(1 for _ in result.root.files()) > 400
