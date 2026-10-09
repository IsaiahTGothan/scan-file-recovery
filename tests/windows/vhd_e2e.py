"""Windows end-to-end test on a real NTFS disk (runs in GitHub Actions).

Steps (each a sub-command so CI logs show exactly where something failed):

  create      make a VHDX with diskpart, format it NTFS, write test files,
              delete some, then re-attach the disk read-only
  verify-api  read the raw disk (\\\\.\\PhysicalDriveN) and the volume
              (\\\\.\\X:) with Lifeboat, recover everything into a destination
              deeper than MAX_PATH and compare every byte
  verify-cli  run the packaged lifeboat-cli.exe against the same disk
  destroy     detach and delete the VHDX

Needs Windows and administrator rights.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import string
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

WORK = Path(os.environ.get("LIFEBOAT_E2E_DIR", r"C:\lifeboat-e2e"))
VHD = WORK / "test.vhdx"
STATE = WORK / "state.json"
MANIFEST = WORK / "manifest.json"
MiB = 1 << 20


def data(seed: str, size: int) -> bytes:
    return hashlib.shake_256(seed.encode()).digest(size) if size else b""


def run(cmd: list[str] | str, check: bool = True, **kw) -> subprocess.CompletedProcess:
    print(">", cmd if isinstance(cmd, str) else " ".join(cmd), flush=True)
    res = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if res.stdout.strip():
        print(res.stdout.strip()[-4000:], flush=True)
    if res.stderr.strip():
        print(res.stderr.strip()[-4000:], flush=True)
    if check and res.returncode != 0:
        raise SystemExit(f"command failed with exit code {res.returncode}")
    return res


def diskpart(script: str) -> None:
    path = WORK / "diskpart.txt"
    path.write_text(script, encoding="ascii")
    run(["diskpart", "/s", str(path)])


def powershell(command: str) -> str:
    return run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command]).stdout.strip()


def free_letter() -> str:
    used = set(powershell("(Get-PSDrive -PSProvider FileSystem).Name -join ''").upper())
    for letter in reversed(string.ascii_uppercase[3:]):
        if letter not in used:
            return letter
    raise SystemExit("no free drive letter")


def long(path: str) -> str:
    return "\\\\?\\" + os.path.abspath(path)


def write(root: str, rel: str, content: bytes, manifest: dict, mtime: float | None = None, **extra) -> None:
    target = os.path.join(root, rel)
    os.makedirs(long(os.path.dirname(target)), exist_ok=True)
    with open(long(target), "wb") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())  # make sure the data is on disk, not only in the Windows cache
    if mtime is not None:
        os.utime(long(target), (mtime, mtime))
    manifest[rel.replace("\\", "/")] = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                                        "deleted": False, "mtime": mtime, **extra}


def cmd_create() -> None:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    letter = free_letter()
    diskpart(f'create vdisk file="{VHD}" maximum=512 type=expandable\n'
             f'select vdisk file="{VHD}"\nattach vdisk\nconvert gpt\ncreate partition primary\n'
             f'format fs=ntfs quick label=LBTEST\nassign letter={letter}\n')
    root = f"{letter}:\\"
    time.sleep(2)
    manifest: dict = {}
    t0 = 1_600_000_000
    write(root, r"Documents\report.docx", data("report", 150_000), manifest, t0 + 1)
    write(root, r"Documents\notes.txt", data("notes", 3_000), manifest, t0 + 2)
    write(root, r"Photos\2024\IMG_0001.JPG", data("img1", 2 * MiB + 123), manifest, t0 + 3)
    write(root, r"Photos\2024\IMG_0002.JPG", data("img2", 3 * MiB), manifest, t0 + 4)
    write(root, r"Unicode\résumé été.txt", data("uni1", 1000), manifest)
    write(root, r"Unicode\日本語のファイル.txt", data("uni2", 2000), manifest)
    write(root, "small.txt", data("small", 120), manifest)
    write(root, "empty.txt", b"", manifest)
    deep = "\\".join(f"Deep folder level {i:02d} with a long name" for i in range(8))
    write(root, deep + r"\deep file.bin", data("deep", 70_000), manifest, deep=True)
    for i in range(300):
        write(root, f"Many\\file_{i:03d}.txt", data(f"many{i}", 100 + i * 7), manifest)
    comp = os.path.join(root, "Compressed")
    os.makedirs(comp)
    text = b"Lifeboat compressible text line.\n" * 20000
    write(root, r"Compressed\text.txt", text, manifest, compressed=True)
    write(root, r"Compressed\mixed.bin", text[:200_000] + data("mix", 100_000), manifest, compressed=True)
    run(["compact", "/c", "/s:" + comp, "/i", "/q"])
    # Files that will be deleted (nothing is written after the deletion).
    write(root, r"Trash\deleted photo.jpg", data("del1", 900_000), manifest)
    write(root, r"Trash\deleted note.txt", data("del2", 5_000), manifest)
    write(root, r"Old Project\plan.docx", data("del3", 80_000), manifest)
    write(root, r"Old Project\sub\budget.xlsx", data("del4", 40_000), manifest)
    # Flush everything first: a real deleted file was written long before it was deleted.
    powershell(f"Write-VolumeCache -DriveLetter {letter}")
    time.sleep(1)
    os.remove(os.path.join(root, r"Trash\deleted photo.jpg"))
    os.remove(os.path.join(root, r"Trash\deleted note.txt"))
    shutil.rmtree(os.path.join(root, "Old Project"))
    for rel in ("Trash/deleted photo.jpg", "Trash/deleted note.txt", "Old Project/plan.docx",
                "Old Project/sub/budget.xlsx"):
        manifest[rel]["deleted"] = True
    MANIFEST.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    powershell(f"Write-VolumeCache -DriveLetter {letter}")
    diskpart(f'select vdisk file="{VHD}"\ndetach vdisk\n')
    time.sleep(2)
    number = powershell(f'(Mount-DiskImage -ImagePath "{VHD}" -Access ReadOnly -PassThru | Get-Disk).Number')
    time.sleep(3)
    letters = powershell(f"(Get-Partition -DiskNumber {number} | Where-Object DriveLetter).DriveLetter -join ''")
    state = {"disk": int(number), "letter": letters[:1] if letters else ""}
    STATE.write_text(json.dumps(state))
    print("state:", state, flush=True)


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _state() -> dict:
    return json.loads(STATE.read_text())


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(long(path), "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _check_recovered(job_dir: str, volume_folder_hint: str = "") -> list[str]:
    manifest = _manifest()
    problems = []
    found = {}
    for dirpath, _dirs, files in os.walk(long(job_dir)):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, long(job_dir)).replace("\\", "/")
            found["/".join(rel.split("/")[1:])] = full
    for rel, entry in manifest.items():
        path = found.get(rel)
        if path is None:
            problems.append(f"missing: {rel}")
            continue
        if os.path.getsize(path) != entry["size"]:
            problems.append(f"size: {rel}")
        elif _sha(path[4:] if path.startswith("\\\\?\\") else path) != entry["sha256"]:
            with open(long(path[4:] if path.startswith("\\\\?\\") else path), "rb") as fh:
                head = fh.read(65536)
            zeros = "all zeros" if not head.strip(b"\0") else "non-zero data"
            problems.append(f"content: {rel} ({zeros})")
        if entry.get("mtime") and abs(os.path.getmtime(path) - entry["mtime"]) > 2:
            problems.append(f"mtime: {rel}")
        if entry.get("deep") and len(path) < 300:
            problems.append(f"deep path unexpectedly short: {len(path)}")
    return problems


def cmd_verify_api() -> None:
    from lifeboat.device.enumerate import is_admin, list_devices, open_device
    from lifeboat.errors import E_DEST_ON_SOURCE
    from lifeboat.events import EventBus
    from lifeboat.fs.model import F
    from lifeboat.recover import RecoveryJob, RecoveryOptions, Status, check_destination
    from lifeboat.rescue.reader import ReadPolicy, RescueReader
    from lifeboat.scan import Scanner

    assert is_admin(), "the end-to-end test needs administrator rights"
    state = _state()
    devices = list_devices()
    for d in devices:
        print(f"  {d.path} kind={d.kind} size={d.size} ss={d.sector_size}/{d.physical_sector_size} "
              f"bus={d.bus} model={d.model!r} serial={d.serial!r} vols={d.volumes} system={d.system}")
    disk = next(d for d in devices if d.kind == "disk" and d.disk_number == state["disk"])
    assert disk.size == 512 * MiB, disk.size
    assert any(d.system for d in devices if d.kind == "disk"), "the Windows disk should be flagged"
    bus = EventBus()
    bus.subscribe(lambda e: print(f"  [{e.level.name}] {e.message}", flush=True))
    device = open_device(disk)
    reader = RescueReader(device, ReadPolicy(timeout=10), events=bus)
    result = Scanner(reader, device.info, bus).quick_scan()
    assert result.table is not None and result.table.scheme == "GPT", result.table
    ntfs = [v for v in result.volumes if v.volume is not None and v.volume.kind == "NTFS"]
    assert len(ntfs) == 1, [v.title for v in result.volumes]
    files = [n for n in ntfs[0].root.walk() if not n.flags & (F.DIR | F.SYSTEM | F.STREAM)]
    print(f"found {len(files)} files", flush=True)
    deleted = [n for n in files if n.flags & F.DELETED]
    for n in deleted:
        print(f"  deleted: {n.path()} size={n.size} overwritten={bool(n.flags & F.OVERWRITTEN)}", flush=True)
    assert len(deleted) == 4, [n.path() for n in deleted]
    assert not [n for n in deleted if n.flags & F.OVERWRITTEN]
    # The destination may not be on the disk being recovered.
    if state["letter"]:
        report = check_destination(f"{state['letter']}:\\out", device.info, 100)
        assert any(i.code == E_DEST_ON_SOURCE for i in report.blocking), report.issues
    dest = WORK / ("out " + "x" * 120)
    shutil.rmtree(long(str(dest)), ignore_errors=True)
    job = RecoveryJob(files, reader, device.info, RecoveryOptions(destination=str(dest), job_folder=False), bus)
    summary = job.run()
    print("summary:", summary.headline(), flush=True)
    bad = [t for t in summary.tasks if t.status != Status.OK and t.size]
    assert not bad, [(t.source_path, t.status, t.message) for t in bad]
    problems = _check_recovered(str(dest))
    assert not problems, problems
    assert os.path.exists(summary.report_html)
    device.close()
    # Reading through the volume (drive letter) works too.
    if state["letter"]:
        volume = open_device(f"\\\\.\\{state['letter']}:")
        vreader = RescueReader(volume, ReadPolicy(timeout=10))
        vresult = Scanner(vreader, volume.info, EventBus()).quick_scan()
        vfiles = [n for n in vresult.root.walk() if not n.flags & (F.DIR | F.SYSTEM | F.STREAM)]
        assert len(vfiles) == len(files), (len(vfiles), len(files))
        volume.close()
    print("verify-api OK", flush=True)


def cmd_verify_cli(exe: str) -> None:
    state = _state()
    dest = WORK / "cli-out"
    shutil.rmtree(dest, ignore_errors=True)
    run([exe, "--version"])
    run([exe, "devices"])
    res = run([exe, "recover", f"\\\\.\\PhysicalDrive{state['disk']}", str(dest), "--no-job-folder", "-q"],
              check=False)
    assert res.returncode == 0, f"lifeboat-cli exited with {res.returncode}"
    problems = _check_recovered(str(dest))
    assert not problems, problems
    print("verify-cli OK", flush=True)


def cmd_destroy() -> None:
    if VHD.exists():
        run(["powershell", "-NoProfile", "-Command", f'Dismount-DiskImage -ImagePath "{VHD}"'], check=False)
        time.sleep(1)
    shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    if sys.platform != "win32":
        raise SystemExit("Windows only")
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
