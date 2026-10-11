"""macOS end-to-end test on real disks made by macOS itself (runs in GitHub Actions).

Steps (each a sub-command so CI logs show exactly where something failed):

  create      make FAT32 and exFAT disk images with hdiutil, write test files with macOS's own
              filesystem drivers, delete some, then attach each image read-only as a disk
              (/dev/diskN) without mounting it
  verify-api  as root: find the disks with Lifeboat's macOS drive list and Drive check, read them
              through /dev/rdiskN, recover everything and compare every byte
  verify-cli  the same through the bundled lifeboat-cli (Lifeboat.app/Contents/MacOS)
  destroy     detach the disks and delete the images

Needs macOS; verify-* need root (sudo).
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

WORK = Path(os.environ.get("LIFEBOAT_E2E_DIR", "/tmp/lifeboat-e2e"))
STATE = WORK / "state.json"
FORMATS = {"fat32": ("MS-DOS FAT32", "LBFAT32"), "exfat": ("ExFAT", "LBEXFAT")}
MiB = 1 << 20


def data(seed: str, size: int) -> bytes:
    return hashlib.shake_256(seed.encode()).digest(size) if size else b""


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print(">", " ".join(cmd), flush=True)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.stdout.strip():
        print(res.stdout.strip()[-4000:], flush=True)
    if res.stderr.strip():
        print(res.stderr.strip()[-4000:], flush=True)
    if check and res.returncode != 0:
        raise SystemExit(f"command failed with exit code {res.returncode}")
    return res


def attach(image: Path, mount: bool) -> tuple[str, str]:
    """Attach a disk image; return (whole disk, mount point or "")."""
    args = ["hdiutil", "attach", "-plist", str(image)]
    args += ["-nobrowse"] if mount else ["-readonly", "-nomount"]
    out = subprocess.run(args, capture_output=True, check=True).stdout
    entities = plistlib.loads(out)["system-entities"]
    disk = min((e["dev-entry"] for e in entities), key=len)
    mounts = [e.get("mount-point", "") for e in entities if e.get("mount-point")]
    return disk, mounts[0] if mounts else ""


def cmd_create() -> None:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    state: dict = {}
    for name, (fs, label) in FORMATS.items():
        image = WORK / f"{name}.dmg"
        run(["hdiutil", "create", "-size", "128m", "-fs", fs, "-volname", label, "-layout", "MBRSPUD", str(image)])
        disk, mount = attach(image, mount=True)
        manifest: dict[str, dict] = {}
        files = {
            "readme.txt": data(f"{name}-readme", 1234),
            "Photos/IMG_0001.JPG": data(f"{name}-img1", 3 * MiB + 17),
            "Photos/Vacation 2026/beach day.jpg": data(f"{name}-beach", 2 * MiB),
            "Docs/Report – final (v2).docx": data(f"{name}-report", 777_777),
            "Docs/nested/deeper/even deeper/notes.md": data(f"{name}-notes", 4096),
            "Video/clip.mp4": data(f"{name}-clip", 9 * MiB + 1),
            "empty.txt": b"",
        }
        for rel, content in files.items():
            target = Path(mount) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            manifest[rel] = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        deleted = {"deleted one.bin": data(f"{name}-del1", 300_000), "Docs/old draft.txt": data(f"{name}-del2", 5000)}
        for rel, content in deleted.items():
            (Path(mount) / rel).write_bytes(content)
        os.sync()
        for rel in deleted:
            (Path(mount) / rel).unlink()
        os.sync()
        run(["hdiutil", "detach", disk])
        time.sleep(1)
        disk, _mount = attach(image, mount=False)
        state[name] = {"image": str(image), "disk": disk, "manifest": manifest,
                       "deleted": {rel: len(c) for rel, c in deleted.items()}}
        print(f"{name}: attached read-only as {disk}", flush=True)
    STATE.write_text(json.dumps(state, indent=1))


def _state() -> dict:
    return json.loads(STATE.read_text())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _check_recovered(dest: Path, manifest: dict) -> list[str]:
    found: dict[str, Path] = {}
    for path in dest.rglob("*"):
        if path.is_file():
            rel = path.relative_to(dest).as_posix()
            found["/".join(rel.split("/")[1:])] = path  # drop the volume folder
    problems = []
    for rel, entry in manifest.items():
        path = found.get(rel)
        if path is None:
            problems.append(f"missing: {rel}")
        elif path.stat().st_size != entry["size"]:
            problems.append(f"size: {rel}")
        elif _sha(path) != entry["sha256"]:
            problems.append(f"content: {rel}")
    return problems


def cmd_verify_api() -> None:
    from lifeboat.device.check import run_check
    from lifeboat.device.enumerate import is_admin, list_devices, open_device
    from lifeboat.events import EventBus
    from lifeboat.fs.model import F
    from lifeboat.recover import RecoveryJob, RecoveryOptions, Status, check_destination
    from lifeboat.recover.destination import disks_for_path, disks_for_source
    from lifeboat.rescue.reader import ReadPolicy, RescueReader
    from lifeboat.scan import Scanner

    assert is_admin(), "run as root (sudo)"
    state = _state()
    devices = list_devices()
    for d in devices:
        print(f"  {d.path} size={d.size} ss={d.sector_size}/{d.physical_sector_size} bus={d.bus!r} "
              f"model={d.model!r} system={d.system} status={d.status!r} vols={d.volumes}", flush=True)
    assert any(d.system for d in devices), "the disk macOS runs from should be flagged"
    checked = run_check()
    for r in checked:
        print(f"  check: {r.name!r} kind={r.kind} path={r.path} size={r.size} verdict={r.verdict} "
              f"read={r.read_text}", flush=True)
    bus = EventBus()
    bus.subscribe(lambda e: print(f"  [{e.level.name}] {e.message}", flush=True))
    for name, item in state.items():
        raw = item["disk"].replace("/dev/disk", "/dev/rdisk")
        disk = next(d for d in devices if d.path == raw)
        assert disk.bus == "Disk Image" and disk.size >= 128 * MiB - 2 * MiB and disk.readable, disk
        verdict = next(r for r in checked if r.path == raw)
        assert verdict.verdict == "readable", verdict.to_dict()
        device = open_device(disk)
        assert device.size == disk.size and device.is_present()
        reader = RescueReader(device, ReadPolicy(timeout=10), events=bus)
        result = Scanner(reader, device.info, bus).quick_scan()
        volumes = [v for v in result.volumes if v.volume is not None]
        print(f"{name}: {[v.title for v in result.volumes]}", flush=True)
        assert len(volumes) == 1, result.volumes
        files = [n for n in volumes[0].root.walk() if not n.flags & (F.DIR | F.SYSTEM | F.STREAM)]
        deleted = [n for n in files if n.flags & F.DELETED]
        print(f"{name}: {len(files)} files, {len(deleted)} deleted: "
              f"{[(n.path(), n.size) for n in deleted]}", flush=True)
        sizes = {n.size for n in deleted}
        assert set(item["deleted"].values()) <= sizes, "the deleted test files should be found"
        dest = WORK / f"out-api-{name}"
        shutil.rmtree(dest, ignore_errors=True)
        # Never onto the drive being recovered; a folder on the Mac's own disk is fine.
        assert not disks_for_path(str(WORK)) & disks_for_source(disk.path, "disk", disk.disk_number)
        assert check_destination(str(WORK), device.info, 1).ok
        live = [n for n in files if not n.flags & F.DELETED]
        summary = RecoveryJob(live, reader, device.info, RecoveryOptions(destination=str(dest), job_folder=False),
                              bus).run()
        print(f"{name}: {summary.headline()}", flush=True)
        bad = [(t.source_path, t.status, t.message) for t in summary.tasks if t.status != Status.OK and t.size]
        assert not bad, bad
        problems = _check_recovered(dest, item["manifest"])
        assert not problems, problems
        device.close()
    print("verify-api OK", flush=True)


def cmd_verify_cli(exe: str) -> None:
    state = _state()
    run([exe, "--version"])
    run([exe, "devices"])
    res = run([exe, "check", "--json"], check=False)
    assert res.returncode in (0, 1), f"lifeboat-cli check exited with {res.returncode}"
    checked = {r["path"]: r for r in json.loads(res.stdout)}
    for name, item in state.items():
        raw = item["disk"].replace("/dev/disk", "/dev/rdisk")
        assert checked[raw]["verdict"] == "readable", checked[raw]
        dest = WORK / f"out-cli-{name}"
        shutil.rmtree(dest, ignore_errors=True)
        res = run([exe, "recover", raw, str(dest), "--no-job-folder", "--existing-only", "-q"], check=False)
        assert res.returncode == 0, f"lifeboat-cli recover exited with {res.returncode}"
        problems = _check_recovered(dest, item["manifest"])
        assert not problems, problems
    print("verify-cli OK", flush=True)


def cmd_destroy() -> None:
    if STATE.exists():
        for item in _state().values():
            run(["hdiutil", "detach", "-force", item["disk"]], check=False)
    shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    if sys.platform != "darwin":
        raise SystemExit("macOS only")
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "create":
        cmd_create()
    elif command == "verify-api":
        cmd_verify_api()
    elif command == "verify-cli":
        cmd_verify_cli(sys.argv[2])
    elif command == "destroy":
        cmd_destroy()
    else:
        raise SystemExit(__doc__)
