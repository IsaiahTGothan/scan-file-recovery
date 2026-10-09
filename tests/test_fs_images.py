"""Scan reference images made by the real filesystem tools and compare every file."""

from __future__ import annotations

import pytest

from lifeboat.fs.model import F
from tests.conftest import image_path, manifest, sha256
from tests.helpers import find, is_complete, open_volume, read_all, tree_index

FS_NAMES = ["ntfs", "fat32", "fat16", "fat12", "exfat"]


def _check_files(name: str, *, deleted: bool) -> None:
    volume = open_volume(image_path(name))
    index = tree_index(volume.root)
    entries = [e for e in manifest(name) if e["deleted"] == deleted]
    assert entries, "manifest has no entries of this kind"
    problems = []
    for entry in entries:
        node = find(index, entry["path"], deleted=deleted)
        if node is None:
            problems.append(f"missing: {entry['path']}")
            continue
        if node.size != entry["size"]:
            problems.append(f"size {entry['path']}: {node.size} != {entry['size']}")
            continue
        data, states = read_all(volume, node)
        if not is_complete(states):
            problems.append(f"incomplete read: {entry['path']} {states}")
        elif sha256(data) != entry["sha256"]:
            problems.append(f"content mismatch: {entry['path']}")
        if entry.get("mtime") and node.mtime is not None and abs(node.mtime - entry["mtime"]) > 2.01:
            problems.append(f"mtime {entry['path']}: {node.mtime} != {entry['mtime']}")
    assert not problems, "\n".join(problems[:40])


@pytest.mark.parametrize("name", FS_NAMES)
def test_live_files_match(images, name):
    _check_files(name, deleted=False)


@pytest.mark.parametrize("name", FS_NAMES)
def test_deleted_files_recovered(images, name):
    _check_files(name, deleted=True)


def test_ntfs_details(images):
    volume = open_volume(image_path("ntfs"))
    assert volume.label == "" or isinstance(volume.label, str)
    index = tree_index(volume.root)
    comp = find(index, "Compressed/comp_text.txt")
    assert comp is not None and comp.flags & F.COMPRESSED
    assert volume.layout(comp).compressed is not None
    sparse = find(index, "sparse.bin")
    assert sparse is not None and sparse.flags & F.SPARSE
    frag = find(index, "frag/huge_fragmented.bin")
    assert frag is not None
    assert volume.layout(frag).fragments > 1, "fixture should have produced a fragmented file"
    # alternate data stream
    stream = find(index, "ads.txt:secret")
    assert stream is not None and stream.flags & F.STREAM
    data, states = read_all(volume, stream)
    assert is_complete(states) and len(data) == 120
    # hard link appears in both places, flagged
    link = find(index, "Documents/notes_link.txt")
    orig = find(index, "Documents/notes.txt")
    assert link is not None and orig is not None
    assert link.flags & F.HARDLINK and orig.flags & F.HARDLINK
    # deleted folder keeps its children
    old = find(index, "Trash/OldProject", deleted=True)
    assert old is not None and old.is_dir
    # some fillers were overwritten by the fragmented file
    fillers = [n for p, nodes in index.items() if p.startswith("frag/fill") for n in nodes]
    assert any(n.flags & F.DELETED for n in fillers)
    assert any(n.flags & F.OVERWRITTEN for n in fillers)
    # system files are flagged
    mft = find(index, "$MFT")
    assert mft is not None and mft.flags & F.SYSTEM
    # resident small file
    small = find(index, "small/f0000.txt")
    assert small is not None and small.flags & F.RESIDENT


def test_fat_details(images):
    volume = open_volume(image_path("fat32"))
    assert volume.kind == "FAT32"
    assert volume.label == "FAT32TEST"
    index = tree_index(volume.root)
    frag = find(index, "fragmented.bin")
    assert frag is not None and volume.layout(frag).fragments > 1
    hidden = find(index, "lower.txt")
    assert hidden is not None and hidden.flags & F.HIDDEN
    gone = find(index, "Trash/gone.jpg", deleted=True)
    assert gone is not None and gone.flags & F.ASSUMED_CONTIGUOUS
    # a pure 8.3 name loses its first letter when deleted ...
    assert gone.flags & F.NAME_GUESSED and gone.name == "_one.jpg"
    # ... but a long name is rebuilt in full from its LFN entries (checksum-verified)
    long_name = find(index, "Trash/a much longer deleted name.txt", deleted=True)
    assert long_name is not None and not long_name.flags & F.NAME_GUESSED
    for bits in (16, 12):
        vol = open_volume(image_path(f"fat{bits}"))
        assert vol.kind == f"FAT{bits}"


def test_exfat_details(images):
    volume = open_volume(image_path("exfat"))
    assert volume.kind == "exFAT"
    assert volume.label == "EXFATTEST"
    index = tree_index(volume.root)
    frag = find(index, "fragmented.bin")
    assert frag is not None
    assert volume.layout(frag).fragments > 1
    removed = find(index, "Removed", deleted=True)
    assert removed is not None and removed.is_dir
