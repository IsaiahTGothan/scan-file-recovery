"""Make the HFS+ and APFS test disks used by the unit tests, with macOS's own filesystem code.

Linux can't write HFS+ or APFS, so these disks are made on a Mac (a CI job runs this script and
commits the result to tests/fixtures/apple). Each disk is created by hdiutil, filled through the
mounted volume, detached and converted to a raw image, then gzipped. Next to each image, a
manifest lists every file written: path, size, SHA-256, whether it was deleted afterwards, how
macOS stored it (compressed, hard link, symlink, sparse), and the disk's layout.

What the disks hold, so the reader is tested on what real Macs produce:
  * names with accents (HFS+ stores them decomposed), Japanese, emoji, a colon, 200 characters
  * files macOS compresses (zlib, LZVN, LZFSE; small ones inside an attribute, large ones in the
    resource fork), a resource fork, extended attributes
  * a file in dozens of pieces (more than HFS+ keeps in the catalog), hard links, a symlink,
    a folder with hundreds of files (a multi-level catalog), a sparse file (APFS)
  * deleted files and folders; on APFS some deleted after a snapshot was taken, some not
  * a second APFS volume that is encrypted with a password ("lifeboat")
  * partition layouts: GPT, Apple Partition Map, and none

Run on macOS as root (snapshots need it):  sudo python tests/macos/make_fixtures.py OUTDIR
"""

from __future__ import annotations

import errno
import gzip
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

KiB = 1 << 10
MiB = 1 << 20
T_OLD = 1_234_567_890      # 2009-02-13: a fixed modification time to check date conversion
T_NEW = 1_767_225_600      # 2026-01-01

DISKS = [
    # name, filesystem, partition layout, size, volume name
    ("hfsplus", "Journaled HFS+", "GPTSPUD", "40m", "LBHFS"),
    ("hfsx_apm", "Case-sensitive Journaled HFS+", "SPUD", "32m", "LBHFSX"),
    ("hfsplus_bare", "HFS+", "NONE", "32m", "LBPLAIN"),
    ("apfs", "APFS", "GPTSPUD", "64m", "LBAPFS"),
]


def content(seed: str, size: int) -> bytes:
    """Bytes that compress well (small images in git) yet differ in every 4 KiB block."""
    shared = hashlib.shake_256(seed.encode()).digest(4096 - 32)
    out = bytearray()
    block = 0
    while len(out) < size:
        out += hashlib.sha256(f"{seed}:{block}".encode()).digest() + shared
        block += 1
    return bytes(out[:size])


def text(seed: str, size: int) -> bytes:
    """Readable text that macOS's compressors shrink (they skip data that doesn't compress)."""
    words = [b"lifeboat", b"recovery", b"sector", b"catalog", b"extent", b"folder", b"drive", b"apple"]
    out = bytearray()
    i = 0
    while len(out) < size:
        h = hashlib.sha256(f"{seed}{i}".encode()).digest()
        out += words[h[0] % len(words)] + (b"\n" if h[1] % 9 == 0 else b" ")
        i += 1
    return bytes(out[:size])


def run(cmd: list[str], check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    print(">", " ".join(cmd), flush=True)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if not quiet:
        for stream in (res.stdout, res.stderr):
            if stream.strip():
                print(stream.strip()[-3000:], flush=True)
    if check and res.returncode != 0:
        raise SystemExit(f"{cmd[0]} failed with exit code {res.returncode}")
    return res


def plist(cmd: list[str]) -> dict:
    res = subprocess.run(cmd, capture_output=True, check=True)
    return plistlib.loads(res.stdout)


class Disk:
    def __init__(self, mount: Path, case_sensitive: bool, apfs: bool) -> None:
        self.mount = mount
        self.case_sensitive = case_sensitive
        self.apfs = apfs
        self.files: dict[str, dict] = {}
        self.notes: dict[str, object] = {}

    # ----------------------------------------------------------------- writing
    def write(self, rel: str, data: bytes, mtime: float | None = None, **extra: object) -> Path:
        path = self.mount / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        self.files[rel] = {"path": rel, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                           "deleted": False, "mtime": mtime, **extra}
        return path

    def delete(self, rel: str) -> None:
        path = self.mount / rel
        if path.is_dir():
            shutil.rmtree(path)
            for key, item in self.files.items():
                if key.startswith(rel + "/"):
                    item["deleted"] = True
        else:
            path.unlink()
            self.files[rel]["deleted"] = True

    def sync(self) -> None:
        os.sync()
        time.sleep(1.5)   # let the filesystem write a new checkpoint / journal transaction

    def flags(self, rel: str) -> int:
        return os.lstat(self.mount / rel).st_flags

    # ----------------------------------------------------------------- content
    def basics(self) -> None:
        self.write("readme.txt", text("readme", 1234), mtime=T_OLD)
        self.write("empty.txt", b"")
        self.write("Photos/IMG_0001.HEIC", content("img1", 300 * KiB + 17), mtime=T_NEW)
        self.write("Photos/Vacation 2026/beach day.jpg", content("beach", 2 * MiB))
        self.write("Docs/Résumé – final.pages", content("resume", 70_000))
        self.write("Docs/naïve café.txt", text("cafe", 5000))
        self.write("Docs/写真/東京.jpg", content("tokyo", 123_456))
        self.write("Music/🎵 song.m4a", content("song", 456_789))
        self.write("Docs/Meeting 10:30.txt", text("meeting", 777))
        self.write("Docs/" + "long name " * 19 + ".txt", text("long", 999))
        self.write("Docs/nested/deeper/even deeper/notes.md", text("notes", 4096))
        self.write("Video/clip.mov", content("clip", 3 * MiB + 1))
        if self.case_sensitive:
            self.write("Case/Report.txt", text("upper", 1000))
            self.write("Case/report.txt", text("lower", 2000))
        # A folder with hundreds of files makes the catalog / file-system tree several levels deep.
        for i in range(400):
            self.write(f"Many/file {i:03}.txt", text(f"many{i}", 300 + i))

    def links(self) -> None:
        target = "Docs/linked report.txt"
        self.write(target, text("hardlink", 9000))
        os.link(self.mount / target, self.mount / "Docs/hard link to report.txt")
        self.files["Docs/hard link to report.txt"] = dict(self.files[target], path="Docs/hard link to report.txt",
                                                          hardlink_of=target)
        os.symlink("Docs/linked report.txt", self.mount / "link to report")
        self.files["link to report"] = {"path": "link to report", "symlink": "Docs/linked report.txt",
                                        "deleted": False}
        # Folder hard links exist only on HFS+ (Time Machine uses them); macOS may refuse.
        if not self.apfs:
            (self.mount / "TM/source").mkdir(parents=True)
            self.write("TM/source/in linked folder.txt", text("tmdir", 2500))
            (self.mount / "TM/other").mkdir()
            try:
                os.link(self.mount / "TM/source", self.mount / "TM/other/folder link")
                self.files["TM/other/folder link/in linked folder.txt"] = dict(
                    self.files["TM/source/in linked folder.txt"], path="TM/other/folder link/in linked folder.txt")
                self.notes["folder_hardlink"] = True
            except OSError as exc:
                self.notes["folder_hardlink"] = f"refused: {exc}"

    def forks(self) -> None:
        rel = "Classic/with resource fork.txt"
        self.write(rel, text("datafork", 3000))
        rsrc = content("rsrc", 20_000)
        (self.mount / rel / "..namedfork" / "rsrc").write_bytes(rsrc)
        self.files[rel]["rsrc_size"] = len(rsrc)
        self.files[rel]["rsrc_sha256"] = hashlib.sha256(rsrc).hexdigest()
        run(["xattr", "-w", "com.example.note", "kept by Lifeboat? no, but it must not get in the way",
             str(self.mount / rel)])

    def compressed(self) -> None:
        tool = shutil.which("afsctool")
        methods = ["ZLIB", "LZVN", "LZFSE"] if tool else ["default"]
        self.notes["compression_tool"] = tool or "ditto"
        for method in methods:
            for size_name, size in (("small", 2500), ("large", 300 * KiB + 5)):
                rel = f"Compressed/{method.lower()} {size_name}.txt"
                data = text(f"{method}{size_name}", size)
                if tool:
                    self.write(rel, data)
                    run([tool, "-c", "-T", method, str(self.mount / rel)], check=False)
                else:
                    staging = Path("/tmp") / f"lb-{method}-{size_name}.txt"
                    staging.write_bytes(data)
                    target = self.mount / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    run(["ditto", "--hfsCompression", str(staging), str(target)], check=False)
                    self.files[rel] = {"path": rel, "size": size, "sha256": hashlib.sha256(data).hexdigest(),
                                       "deleted": False, "mtime": None}
                self.files[rel]["compressed"] = bool(self.flags(rel) & 0x20)   # UF_COMPRESSED
                self.files[rel]["method"] = method

    def sparse(self) -> None:
        rel = "Sparse/mostly hole.bin"
        path = self.mount / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"start")
            fh.seek(6 * MiB)
            fh.write(b"the end")
        data = b"start" + bytes(6 * MiB - 5) + b"the end"
        self.files[rel] = {"path": rel, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                           "deleted": False, "mtime": None, "sparse": True}

    def fragmented(self) -> None:
        """A file in dozens of pieces. HFS+: fill the disk, punch holes, write a file into them.
        APFS holds back freed space too long for that, so two files grow in turns instead."""
        pieces = [f"Fragmented/piece {i:02}.bin" for i in range(48)]
        for i, rel in enumerate(pieces):
            self.write(rel, content(f"piece{i}", 24 * KiB))
        self.sync()
        if not self.apfs:
            self.fill("Fragmented/filler.bin", keep=3 * MiB)
            self.sync()
        for rel in pieces[::2]:
            self.delete(rel)
            self.files[rel]["overwritten_maybe"] = True
        self.sync()
        if self.apfs:
            self.interleaved("Fragmented/in many pieces.bin", "Fragmented/grew alongside.bin",
                             2 * MiB + 500 * KiB, 16 * KiB)
        else:
            self.write("Fragmented/in many pieces.bin", content("fragmented", 2 * MiB + 500 * KiB))
            self.sync()
            self.delete("Fragmented/filler.bin")
        self.sync()

    def interleaved(self, rel_a: str, rel_b: str, size: int, step: int) -> None:
        data = {rel_a: content("fragmented", size), rel_b: content("alongside", size)}
        handles = {rel: open(self.mount / rel, "wb") for rel in data}   # noqa: SIM115
        try:
            for start in range(0, size, step):
                for rel, fh in handles.items():
                    fh.write(data[rel][start:start + step])
                    fh.flush()
                    os.fsync(fh.fileno())
        finally:
            for fh in handles.values():
                fh.close()
        for rel, blob in data.items():
            self.files[rel] = {"path": rel, "size": size, "sha256": hashlib.sha256(blob).hexdigest(),
                               "deleted": False, "mtime": None}

    def snapshot(self) -> str:
        """Take an APFS snapshot of the test volume; say how (or why not)."""
        tool = "/System/Library/Filesystems/apfs.fs/Contents/Resources/apfs_snapshot"
        attempts = [[tool, "-c", "lifeboat-test", str(self.mount)]] if os.path.exists(tool) else []
        attempts.append(["tmutil", "localsnapshot", str(self.mount)])
        for cmd in attempts:
            res = run(cmd, check=False)
            if res.returncode == 0:
                listed = run(["diskutil", "apfs", "listSnapshots", str(self.mount)], check=False)
                return f"{cmd[0]}: {listed.stdout.strip()[-500:]}"
        return "failed"

    def fill(self, rel: str, keep: int) -> None:
        """Write zeros until the disk is full (free space as reported is not all usable on APFS),
        then give ``keep`` bytes back."""
        path = self.mount / rel
        chunk = bytes(MiB)
        written = 0
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            while True:
                try:
                    count = os.write(fd, chunk)
                except OSError as exc:
                    if exc.errno != errno.ENOSPC:
                        raise
                    break
                written += count
                if count < len(chunk):
                    break
        finally:
            os.close(fd)
        size = max(0, written - keep)
        os.truncate(path, size)
        digest = hashlib.sha256()
        for start in range(0, size, MiB):
            digest.update(chunk[:min(MiB, size - start)])
        self.files[rel] = {"path": rel, "size": size, "sha256": digest.hexdigest(), "deleted": False,
                           "mtime": None}

    def deleted(self, snapshot: bool) -> None:
        self.write("Deleted/before snapshot.jpg", content("del-snap", 400 * KiB), mtime=T_NEW)
        self.write("Deleted/folder/inside.txt", text("del-inside", 7000))
        self.write("Deleted/folder/photo.jpg", content("del-photo", 90 * KiB))
        self.sync()
        if snapshot:
            self.notes["snapshot"] = self.snapshot()
            self.sync()
        self.write("Deleted/after snapshot.pdf", content("del-after", 150 * KiB))
        self.write("Deleted/small note.txt", text("del-small", 900))
        self.sync()
        for rel in ("Deleted/before snapshot.jpg", "Deleted/after snapshot.pdf", "Deleted/small note.txt"):
            self.delete(rel)
        self.delete("Deleted/folder")
        self.sync()
        self.write("after deleting.txt", text("after", 500))
        self.sync()


def attach(image: Path) -> tuple[str, Path]:
    """Attach a disk image; return its whole disk (not an APFS container made from it) and mount point."""
    out = plist(["hdiutil", "attach", "-nobrowse", "-plist", str(image)])
    entities = out["system-entities"]
    whole = [e["dev-entry"] for e in entities if e["dev-entry"][len("/dev/disk"):].isdigit()]
    disk = min(whole, key=lambda dev: int(dev[len("/dev/disk"):]))
    mounts = [Path(e["mount-point"]) for e in entities if e.get("mount-point")]
    if not mounts:
        raise SystemExit(f"{image.name}: no volume was mounted")
    return disk, mounts[0]


def make(out: Path, name: str, fs: str, layout: str, size: str, label: str) -> None:
    work = Path("/tmp/lb-fixtures")
    work.mkdir(exist_ok=True)
    dmg = work / f"{name}.dmg"
    dmg.unlink(missing_ok=True)
    run(["hdiutil", "create", "-size", size, "-fs", fs, "-volname", label, "-layout", layout, str(dmg)])
    disk, mount = attach(dmg)
    apfs = "APFS" in fs
    info = plist(["diskutil", "info", "-plist", str(mount)])
    print(f"{name}: {disk} mounted at {mount}; {info.get('FilesystemName')}, "
          f"block {info.get('VolumeAllocationBlockSize')}", flush=True)
    (mount / ".metadata_never_index").write_bytes(b"")
    d = Disk(mount, case_sensitive="sensitive" in fs.lower(), apfs=apfs)
    d.notes.update(filesystem=fs, layout=layout, label=label, block_size=info.get("VolumeAllocationBlockSize"),
                   macos=subprocess.run(["sw_vers", "-productVersion"], capture_output=True,
                                        text=True).stdout.strip())
    d.basics()
    d.links()
    d.forks()
    d.compressed()
    if apfs:
        d.sparse()
    d.sync()
    d.fragmented()
    d.deleted(snapshot=apfs)
    if apfs:
        container = info.get("APFSContainerReference", "")
        res = run(["diskutil", "apfs", "addVolume", container, "APFS", "LBLocked", "-passphrase", "lifeboat"],
                  check=False)
        if res.returncode == 0:
            locked = Path("/Volumes/LBLocked")
            secret = text("secret", 3000)
            (locked / "secret.txt").write_bytes(secret)
            d.notes["encrypted_volume"] = {"name": "LBLocked", "password": "lifeboat", "file": "secret.txt",
                                           "sha256": hashlib.sha256(secret).hexdigest(), "size": len(secret)}
            os.sync()
            run(["diskutil", "unmount", str(locked)], check=False)
        else:
            d.notes["encrypted_volume"] = f"failed: {res.stderr.strip()}"
    for rel, item in d.files.items():
        if not item.get("deleted") and not item.get("symlink") and (mount / rel).exists():
            item["flags"] = d.flags(rel)
    os.sync()
    if run(["hdiutil", "detach", disk], check=False).returncode:
        time.sleep(3)
        run(["hdiutil", "detach", "-force", disk])
    time.sleep(1)
    raw = work / f"{name}.cdr"
    raw.unlink(missing_ok=True)
    run(["hdiutil", "convert", str(dmg), "-format", "UDTO", "-o", str(work / name)])
    target = out / f"{name}.img.gz"
    with open(raw, "rb") as src, gzip.open(target, "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst, 1 << 20)
    manifest = {"disk": d.notes, "image_size": raw.stat().st_size,
                "files": sorted(d.files.values(), key=lambda f: f["path"])}
    (out / f"{name}.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n")
    print(f"{name}: {raw.stat().st_size:,} bytes -> {target.stat().st_size:,} gzipped", flush=True)
    dmg.unlink()
    raw.unlink()


def main() -> None:
    if sys.platform != "darwin":
        raise SystemExit("macOS only")
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/apple")
    out.mkdir(parents=True, exist_ok=True)
    only = set(sys.argv[2:])
    for spec in DISKS:
        if not only or spec[0] in only:
            make(out, *spec)


if __name__ == "__main__":
    main()
