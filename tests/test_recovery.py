"""End-to-end recovery tests: scan a real image, recover to disk, compare."""

from __future__ import annotations

import csv
import hashlib
import os
import shutil
import subprocess
import uuid

import pytest

from lifeboat.device.base import DeviceInfo
from lifeboat.device.image import ImageDevice
from lifeboat.device.simulated import FaultPlan, SimulatedFailingDevice
from lifeboat.errors import E_DEST_FAT32, E_DEST_FULL, E_DEST_ON_SOURCE
from lifeboat.events import Choice, EventBus, InterventionHandler, JobControl, Level
from lifeboat.fs.model import Extent, F, FileLayout, Node, Volume
from lifeboat.recover import RecoveryJob, RecoveryOptions, Status, check_destination
from lifeboat.recover.names import NameSpace, sanitize
from lifeboat.rescue.reader import ReadPolicy, RescueReader
from lifeboat.scan.scanner import Scanner
from tests.conftest import image_path, manifest

MiB = 1 << 20


def sha(path) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def scan_device(device, events=None, policy=None):
    events = events or EventBus()
    reader = RescueReader(device, policy or ReadPolicy(timeout=1.0, block_size=256 * 1024), events=events)
    info = DeviceInfo(path=str(getattr(device, "info").path), kind="image", size=device.size)
    result = Scanner(reader, info, events=events).quick_scan()
    return reader, info, result, events


def all_files(root: Node) -> list[Node]:
    return [n for n in root.walk() if not n.flags & F.DIR and not n.flags & (F.SYSTEM | F.STREAM)]


def by_rel(tasks):
    return {t.source_path.split("/", 1)[1]: t for t in tasks}


def run_job(files, reader, info, dest, events=None, handler=None, control=None, **opts):
    options = RecoveryOptions(destination=str(dest), **opts)
    job = RecoveryJob(files, reader, info, options, events=events or EventBus(), control=control,
                      interventions=handler)
    return job, job.run()


# --------------------------------------------------------------------------- names
def test_sanitize_windows_rules():
    assert sanitize("what?.txt") == "what_.txt"
    assert sanitize("a:b|c*d") == "a_b_c_d"
    assert sanitize("trailing dot.") == "trailing dot_"
    assert sanitize("trailing space ") == "trailing space_"
    assert sanitize("CON") == "_CON"
    assert sanitize("aux.txt") == "_aux.txt"
    assert sanitize("Lpt1.log") == "_Lpt1.log"
    assert sanitize("console.txt") == "console.txt"
    assert sanitize("") == "_"
    assert sanitize("..") == "__"
    long = "x" * 300 + ".jpeg"
    out = sanitize(long)
    assert len(out) <= 255 and out.endswith(".jpeg")
    ns = NameSpace()
    assert ns.claim("d", "File.txt") == "File.txt"
    assert ns.claim("d", "file.TXT") == "file (2).TXT"
    assert ns.claim("d", "File.txt", deleted=True) == "File (deleted).txt"
    assert ns.claim("d", "File.txt", deleted=True) == "File (deleted 2).txt"
    assert ns.claim("other", "File.txt") == "File.txt"


# ----------------------------------------------------------------------- full runs
@pytest.mark.parametrize("name", ["ntfs", "fat32", "exfat"])
def test_recover_everything_matches_manifest(images, tmp_path, name):
    reader, info, result, events = scan_device(ImageDevice(image_path(name)))
    files = all_files(result.root)
    job, summary = run_job(files, reader, info, tmp_path, job_folder=True)
    assert summary.outcome in ("success", "warning")
    tasks = by_rel(summary.tasks)
    problems = []
    for entry in manifest(name):
        if entry["deleted"] and not entry["recoverable"]:
            continue
        task = tasks.get(entry["path"])
        if task is None:
            # FAT: deleted 8.3 names lose their first letter
            parent, _, fname = entry["path"].rpartition("/")
            matches = [t for p, t in tasks.items()
                       if p.rpartition("/")[0].lower() == parent.lower()
                       and p.rpartition("/")[2][1:].lower() == fname[1:].lower()
                       and t.node.flags & F.NAME_GUESSED]
            task = matches[0] if matches else None
        if task is None:
            problems.append(f"no task for {entry['path']}")
            continue
        if task.status != Status.OK:
            problems.append(f"{entry['path']}: {task.status} {task.message}")
            continue
        assert os.path.isfile(task.dest)
        if sha(task.dest) != entry["sha256"]:
            problems.append(f"content differs: {entry['path']}")
        if entry.get("mtime"):
            assert abs(os.path.getmtime(task.dest) - entry["mtime"]) <= 2.01, entry["path"]
    assert not problems, problems[:20]
    assert os.path.isfile(summary.report_html) and os.path.isfile(summary.report_csv)
    with open(summary.report_csv, encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == len(summary.tasks)
    assert not [t for t in summary.tasks if t.dest.endswith(".lifeboat-part")]
    leftovers = [p for p in job.job_dir and os.listdir(job.job_dir) if p.endswith(".lifeboat-part")]
    assert not leftovers


def test_weird_ntfs_names_become_windows_safe(images, tmp_path):
    reader, info, result, events = scan_device(ImageDevice(image_path("ntfs")))
    weird = [n for n in all_files(result.root) if "/weird/" in n.path()]
    job, summary = run_job(weird, reader, info, tmp_path)
    names = sorted(os.path.basename(t.dest) for t in summary.tasks)
    assert "_CON" in names and "_aux.txt" in names
    assert "what_.txt" in names and "pipe_name.txt" in names
    assert "trailing dot_" in names and "trailing space_" in names
    lower = [n.lower() for n in names]
    assert len(lower) == len(set(lower)), "case-insensitive duplicates must be renamed"
    assert all(t.status == Status.OK for t in summary.tasks)


def test_bad_sectors_inside_files_are_isolated(images, tmp_path):
    clean_reader, info, clean, _ = scan_device(ImageDevice(image_path("ntfs")))
    target = next(n for n in all_files(clean.root) if n.path().endswith("Documents/Sub Folder/deep/deeper/file.bin"))
    layout = target.volume.layout(target)
    disk0 = layout.extents[0].disk_offset
    bad = (disk0 + 300 * 1024, disk0 + 300 * 1024 + 4096)
    dev = SimulatedFailingDevice(ImageDevice(image_path("ntfs")), FaultPlan(bad=[bad]))
    reader, info, result, events = scan_device(dev, policy=ReadPolicy(timeout=1.0, block_size=256 * 1024,
                                                                      unit=4096))
    node = next(n for n in all_files(result.root) if n.path() == target.path())
    other = next(n for n in all_files(result.root) if n.path().endswith("Documents/report.docx"))
    job, summary = run_job([node, other], reader, info, tmp_path, thoroughness="standard")
    task = next(t for t in summary.tasks if t.node is node)
    assert task.status == Status.PARTIAL
    assert task.damaged_ranges() == [(300 * 1024, 300 * 1024 + 4096)]
    expected = bytearray(hashlib.shake_256(b"deep").digest(MiB))
    expected[300 * 1024:300 * 1024 + 4096] = bytes(4096)
    assert open(task.dest, "rb").read() == bytes(expected)
    assert next(t for t in summary.tasks if t.node is other).status == Status.OK
    assert summary.outcome == "warning"
    report = open(summary.report_html, encoding="utf-8").read()
    assert "Damaged" in report and "file.bin" in report
    with open(summary.report_csv, encoding="utf-8-sig") as fh:
        row = next(r for r in csv.DictReader(fh) if r["Original path"].endswith("file.bin"))
    assert row["Unreadable byte ranges"] == f"{300 * 1024:,}-{300 * 1024 + 4095:,}"


def test_flaky_sectors_are_healed_by_rescue_passes(images, tmp_path):
    clean_reader, info, clean, _ = scan_device(ImageDevice(image_path("ntfs")))
    target = next(n for n in all_files(clean.root) if n.path().endswith("Photos/IMG_0002.JPG"))
    disk0 = target.volume.layout(target).extents[0].disk_offset
    flaky = (disk0 + 64 * 1024, disk0 + 64 * 1024 + 4096)
    dev = SimulatedFailingDevice(ImageDevice(image_path("ntfs")), FaultPlan(flaky={flaky: 2}))
    reader, info, result, events = scan_device(dev, policy=ReadPolicy(timeout=1.0, unit=4096))
    node = next(n for n in all_files(result.root) if n.path() == target.path())
    job, summary = run_job([node], reader, info, tmp_path, thoroughness="standard")
    task = summary.tasks[0]
    assert task.status == Status.OK, task.message
    entry = next(e for e in manifest("ntfs") if e["path"] == "Photos/IMG_0002.JPG")
    assert sha(task.dest) == entry["sha256"]
    assert task.sha256 == entry["sha256"]


class Reconnect(InterventionHandler):
    def __init__(self, device):
        super().__init__()
        self.device = device

    def request(self, intervention):
        self.history.append(intervention)
        self.device.reconnect()
        return Choice.RETRY


def test_recovery_survives_source_disconnect(images, tmp_path):
    dev = SimulatedFailingDevice(ImageDevice(image_path("exfat")), FaultPlan())
    reader, info, result, events = scan_device(dev)
    files = all_files(result.root)
    dev.plan.disconnect_after_reads = dev.reads + 15
    handler = Reconnect(dev)
    job, summary = run_job(files, reader, info, tmp_path, handler=handler, events=events)
    assert handler.history
    assert all(t.status == Status.OK for t in summary.tasks), [t.message for t in summary.tasks if t.message]


def test_destination_on_source_disk_is_refused(tmp_path):
    from lifeboat.recover.destination import disks_for_path

    disks = disks_for_path(str(tmp_path))
    if not disks:
        pytest.skip("cannot resolve the disk of the temp folder here")
    source = DeviceInfo(path=next(iter(disks)), kind="disk", size=1 << 30)
    report = check_destination(str(tmp_path / "out"), source, needed=1000)
    assert not report.ok
    assert report.blocking[0].code == E_DEST_ON_SOURCE


def test_preflight_space_and_writability(tmp_path):
    source = DeviceInfo(path="img", kind="image", size=1 << 30)
    report = check_destination(str(tmp_path / "out"), source, needed=10)
    assert report.ok and report.free > 0
    report = check_destination(str(tmp_path / "out"), source, needed=1 << 60)
    assert not report.ok and report.space_short


class FakeVolume(Volume):
    kind = "fake"

    def layout(self, node):
        return FileLayout(node.size, [Extent(0, node.size, -1)])


def test_fat32_destination_rejects_4gb_files(tmp_path, monkeypatch):
    import lifeboat.recover.engine as engine

    monkeypatch.setattr(engine, "volume_filesystem", lambda _p: "vfat")
    device = ImageDevice.__new__(ImageDevice)
    reader = RescueReader(_tiny_device(tmp_path), ReadPolicy())
    vol = FakeVolume(reader, 0, 1 << 40)
    root = Node("disk", F.DIR | F.VIRTUAL)
    vroot = root.add(Node("vol", F.DIR | F.VOLUME, volume=vol))
    big = vroot.add(Node("big.mkv", 0, 5 << 30, volume=vol))
    small = vroot.add(Node("small.txt", 0, 1000, volume=vol))
    info = DeviceInfo(path="x", kind="image", size=1 << 20)
    job, summary = run_job([big, small], reader, info, tmp_path)
    statuses = {t.node.name: (t.status, t.code) for t in summary.tasks}
    assert statuses["big.mkv"] == (Status.FAILED, E_DEST_FAT32)
    assert statuses["small.txt"][0] == Status.OK
    del device


def _tiny_device(tmp_path):
    path = tmp_path / "tiny.img"
    path.write_bytes(bytes(1 << 20))
    return ImageDevice(path)


@pytest.fixture
def small_mount(tmp_path):
    if os.geteuid() != 0 or shutil.which("mkfs.ext4") is None:
        pytest.skip("needs root and mkfs.ext4 to create a tiny destination filesystem")
    img = tmp_path / "dest.img"
    mnt = tmp_path / "dest"
    mnt.mkdir()
    with open(img, "wb") as fh:
        fh.truncate(16 * MiB)
    subprocess.run(["mkfs.ext4", "-q", "-F", "-m", "0", str(img)], check=True)
    res = subprocess.run(["mount", "-o", "loop", str(img), str(mnt)], capture_output=True)
    if res.returncode != 0:
        pytest.skip(f"cannot loop-mount: {res.stderr!r}")
    yield mnt
    subprocess.run(["umount", str(mnt)], check=False)


class AnswerOnce(InterventionHandler):
    def __init__(self, answer):
        super().__init__()
        self.answer = answer

    def request(self, intervention):
        self.history.append(intervention)
        return self.answer


def test_destination_full_is_reported_and_skippable(images, small_mount):
    reader, info, result, events = scan_device(ImageDevice(image_path("ntfs")))
    files = [n for n in all_files(result.root) if n.size > 400_000 and not n.flags & F.DELETED]
    assert sum(n.size for n in files) > 18 * MiB
    handler = AnswerOnce(Choice.SKIP)
    job, summary = run_job(files, reader, info, small_mount, handler=handler, events=events)
    assert any(i.code == E_DEST_FULL for i in handler.history)
    full = [t for t in summary.tasks if t.code == E_DEST_FULL]
    assert full and all(t.status == Status.FAILED for t in full)
    assert not [p for p in os.listdir(job.job_dir) if p.endswith(".lifeboat-part")]
    assert events.counts[Level.CRITICAL] >= 1


def test_stop_and_resume(images, tmp_path):
    reader, info, result, events = scan_device(ImageDevice(image_path("fat16")))
    files = all_files(result.root)
    control = JobControl()
    seen = []

    def progress(p):
        seen.append(p)
        if p.items_done >= 40:
            control.cancel()

    options = RecoveryOptions(destination=str(tmp_path / "job"), job_folder=False)
    job = RecoveryJob(files, reader, info, options, events=events, control=control, progress=progress)
    first = job.run()
    assert first.cancelled
    done_first = first.count(Status.OK)
    assert 0 < done_first < len(files)
    assert first.count(Status.SKIPPED) > 0
    # Resume in the same folder with a fresh scan.
    reader2, info2, result2, events2 = scan_device(ImageDevice(image_path("fat16")))
    options2 = RecoveryOptions(destination=str(tmp_path / "job"), job_folder=False, resume=True)
    job2 = RecoveryJob(all_files(result2.root), reader2, info2, options2, events=events2)
    second = job2.run()
    assert second.count(Status.OK) == len(files)
    reused = [t for t in second.tasks if "earlier session" in " ".join(t.notes)]
    assert len(reused) >= done_first
    names = [p for p in os.listdir(tmp_path / "job")]
    assert not [n for n in names if "(2)" in n], "resume must not duplicate files"


def test_never_overwrites_existing_destination_files(images, tmp_path):
    reader, info, result, events = scan_device(ImageDevice(image_path("fat12")))
    node = next(n for n in all_files(result.root) if n.name == "README.TXT")
    dest = tmp_path / "out"
    folder = dest / node.parent.name
    folder.mkdir(parents=True)
    (folder / "README.TXT").write_text("precious")
    job, summary = run_job([node], reader, info, dest, job_folder=False)
    assert (folder / "README.TXT").read_text() == "precious"
    assert summary.tasks[0].dest.endswith("README (2).TXT")
    assert summary.tasks[0].status == Status.OK


def test_mark_damaged_option(images, tmp_path):
    clean_reader, info, clean, _ = scan_device(ImageDevice(image_path("fat32")))
    target = next(n for n in all_files(clean.root) if n.name == "IMG_0001.JPG")
    disk0 = target.volume.layout(target).extents[0].disk_offset
    dev = SimulatedFailingDevice(ImageDevice(image_path("fat32")), FaultPlan(bad=[(disk0, disk0 + 512)]))
    reader, info, result, events = scan_device(dev)
    node = next(n for n in all_files(result.root) if n.name == "IMG_0001.JPG")
    job, summary = run_job([node], reader, info, tmp_path, mark_damaged=True, thoroughness="quick")
    task = summary.tasks[0]
    assert task.status == Status.PARTIAL
    assert task.dest.endswith("IMG_0001 [DAMAGED].JPG") and os.path.exists(task.dest)


def test_unique_job_folders(tmp_path):
    reader = RescueReader(_tiny_device(tmp_path), ReadPolicy())
    info = DeviceInfo(path="x", kind="image", size=1 << 20)
    vol = FakeVolume(reader, 0, 1 << 20)
    root = Node("disk", F.DIR | F.VIRTUAL)
    vroot = root.add(Node("vol", F.DIR | F.VOLUME, volume=vol))
    node = vroot.add(Node(f"{uuid.uuid4().hex}.bin", 0, 100, volume=vol))
    _, a = run_job([node], reader, info, tmp_path / "d", job_name="Job")
    _, b = run_job([node], reader, info, tmp_path / "d", job_name="Job")
    assert a.job_dir != b.job_dir
