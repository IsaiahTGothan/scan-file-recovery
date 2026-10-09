"""Build real filesystem images with the reference Linux tools.

Images are created with mkntfs/ntfs-3g, mkfs.vfat/mtools and
mkfs.exfat/exfat-fuse - the same on-disk structures Windows produces - and
every file written is recorded in a manifest (path, size, SHA-256,
whether it was deleted afterwards).  Tests then scan the images with
Lifeboat and compare.

Run directly to (re)build:  python tests/fixtures/build_images.py OUTDIR
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VERSION = "9"
MiB = 1 << 20


class ToolMissing(RuntimeError):
    pass


def data(seed: str, size: int) -> bytes:
    """Deterministic incompressible bytes."""
    return hashlib.shake_256(seed.encode()).digest(size) if size else b""


def text(seed: str, size: int) -> bytes:
    words = [b"lifeboat", b"recovery", b"sector", b"cluster", b"record", b"folder", b"drive", b"file"]
    out = bytearray()
    i = 0
    while len(out) < size:
        h = hashlib.sha256(f"{seed}{i}".encode()).digest()
        out += words[h[0] % len(words)] + (b"\n" if h[1] % 9 == 0 else b" ")
        i += 1
    return bytes(out[:size])


def run(*cmd: str, check: bool = True, **kw) -> subprocess.CompletedProcess:
    exe = shutil.which(cmd[0])
    if exe is None:
        raise ToolMissing(cmd[0])
    return subprocess.run(cmd, check=check, capture_output=True, text=True, **kw)


class Manifest:
    def __init__(self) -> None:
        self.files: dict[str, dict] = {}

    def add(self, path: str, content: bytes, *, mtime: float | None = None, deleted: bool = False,
            recoverable: bool = True, **extra) -> None:
        self.files[path] = {
            "path": path,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "deleted": deleted,
            "recoverable": recoverable,
            "mtime": mtime,
            **extra,
        }

    def mark_deleted(self, path: str, recoverable: bool = True) -> None:
        self.files[path]["deleted"] = True
        self.files[path]["recoverable"] = recoverable

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(sorted(self.files.values(), key=lambda f: f["path"]), indent=1))


def _write(root: Path, rel: str, content: bytes, manifest: Manifest, mtime: float | None = None, **extra) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as fh:
        fh.write(content)
    if mtime is not None:
        os.utime(target, (mtime, mtime))
    manifest.add(rel, content, mtime=mtime, **extra)


class Mounted:
    def __init__(self, image: Path, kind: str, options: str = "") -> None:
        self.image = image
        self.kind = kind
        self.options = options
        self.loop = ""
        self.dir = Path(tempfile.mkdtemp(prefix="lbmnt-"))

    def __enter__(self) -> Path:
        if self.kind == "ntfs":
            cmd = ["ntfs-3g"]
            if self.options:
                cmd += ["-o", self.options]
            run(*cmd, str(self.image), str(self.dir))
        elif self.kind == "exfat":
            # exfat-fuse insists on a block device: go through a loop device.
            self.loop = run("losetup", "-f", "--show", str(self.image)).stdout.strip()
            run("mount.exfat-fuse", self.loop, str(self.dir))
        return self.dir

    def __exit__(self, *exc) -> None:
        subprocess.run(["sync"], check=False)
        for _ in range(20):
            res = subprocess.run(["umount", str(self.dir)], capture_output=True)
            if res.returncode == 0:
                break
            time.sleep(0.25)
        if self.loop:
            subprocess.run(["losetup", "-d", self.loop], check=False)
        shutil.rmtree(self.dir, ignore_errors=True)


T0 = 1_600_000_000  # fixed timestamps (2020-09-13)


def build_ntfs(path: Path, size_mb: int = 128) -> Manifest:
    m = Manifest()
    path.unlink(missing_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(size_mb * MiB)
    run("mkntfs", "-F", "-q", "-f", "-L", "LIFEBOAT", "-c", "4096", str(path))
    # Names Windows cannot use (created without the streams interface, first:
    # nothing may be written after the deletions at the end).
    with Mounted(path, "ntfs") as root:
        for name in ("aux.txt", "what?.txt", "pipe|name.txt", "star*.txt", 'quote".txt', "<angle>.txt",
                     "trailing dot.", "trailing space ", "colon:name.txt", "CON"):
            _write(root, f"weird/{name}", text(name, 64), m, weird=True)
        # Two names that differ only in case (legal in the POSIX namespace).
        _write(root, "weird/Case.txt", text("case1", 64), m, weird=True)
        _write(root, "weird/case.txt", text("case2", 64), m, weird=True)
    with Mounted(path, "ntfs", "streams_interface=windows") as root:
        _write(root, "Documents/report.docx", data("report", 150_000), m, T0 + 100)
        _write(root, "Documents/notes.txt", text("notes", 2048), m, T0 + 200)
        _write(root, "Documents/Sub Folder/deep/deeper/file.bin", data("deep", MiB), m, T0 + 300)
        for i in range(4):
            _write(root, f"Photos/IMG_{i:04d}.JPG", data(f"photo{i}", 300_000 + i * 50_000), m, T0 + 400 + i)
        for i in range(1500):
            _write(root, f"small/f{i:04d}.txt", text(f"small{i}", 40 + (i % 300)), m, T0 + i)
        _write(root, "unicode/résumé.txt", text("resume", 500), m, T0)
        _write(root, "unicode/日本語のファイル.txt", text("jp", 900), m, T0)
        _write(root, "unicode/emoji_🚀.txt", text("emoji", 100), m, T0)
        _write(root, "empty.txt", b"", m, T0)
        # Sparse file: data at the start and at 10 MiB, holes elsewhere.
        sparse = root / "sparse.bin"
        with open(sparse, "wb") as fh:
            fh.write(data("sparse-a", 8192))
            fh.seek(10 * MiB)
            fh.write(data("sparse-b", 8192))
            fh.truncate(12 * MiB)
        content = data("sparse-a", 8192) + bytes(10 * MiB - 8192) + data("sparse-b", 8192)
        content += bytes(12 * MiB - len(content))
        m.add("sparse.bin", content)
        # Compressed folder (NTFS LZNT1).
        comp = root / "Compressed"
        comp.mkdir()
        run("setfattr", "-n", "system.ntfs_attrib_be", "-v", "0x00000810", str(comp))
        _write(root, "Compressed/comp_text.txt", text("comptext", 300_000), m, compressed=True)
        _write(root, "Compressed/comp_random.bin", data("comprand", 200_000), m, compressed=True)
        _write(root, "Compressed/comp_mixed.bin", text("mix1", 70_000) + data("mix2", 70_000) +
               bytes(70_000) + text("mix3", 70_000), m, compressed=True)
        # Alternate data stream and a hard link.
        _write(root, "ads.txt", text("ads", 300), m)
        with open(root / "ads.txt:secret", "wb") as fh:
            fh.write(text("adsstream", 120))
        os.link(root / "Documents/notes.txt", root / "Documents/notes_link.txt")
        m.add("Documents/notes_link.txt", text("notes", 2048), hardlink=True)
        # Fragmented file: fill the volume, free every other file, write a big file.
        frag = root / "frag"
        frag.mkdir()
        filler = 0
        try:
            while True:
                with open(frag / f"fill{filler:04d}.bin", "wb") as fh:
                    fh.write(data(f"fill{filler}", 256 * 1024))
                filler += 1
                if filler > 2000:
                    break
        except OSError:
            pass
        (frag / f"fill{filler:04d}.bin").unlink(missing_ok=True)
        for i in range(0, filler, 2):
            (frag / f"fill{i:04d}.bin").unlink(missing_ok=True)
        _write(root, "frag/huge_fragmented.bin", data("fragmented", 6 * MiB), m, fragmented=True)
        # Keep room for the deleted-files section below.
        for i in range(1, min(filler, 120), 2):
            (frag / f"fill{i:04d}.bin").unlink(missing_ok=True)
        # Deleted files that must be recoverable: written last, nothing written after.
        _write(root, "Trash/deleted1.bin", data("deleted1", 200_000), m, T0 + 50)
        _write(root, "Trash/deleted_small.txt", text("deleted_small", 100), m, T0 + 51)
        _write(root, "Trash/OldProject/a.txt", text("old_a", 5000), m, T0 + 52)
        _write(root, "Trash/OldProject/b.bin", data("old_b", 90_000), m, T0 + 53)
        _write(root, "Trash/keep.txt", text("keep", 700), m, T0 + 54)
        os.sync()
        (root / "Trash/deleted1.bin").unlink()
        (root / "Trash/deleted_small.txt").unlink()
        shutil.rmtree(root / "Trash/OldProject")
        for rel in ("Trash/deleted1.bin", "Trash/deleted_small.txt", "Trash/OldProject/a.txt",
                    "Trash/OldProject/b.bin"):
            m.mark_deleted(rel)
    return m


def _mtools_env(image: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["MTOOLS_SKIP_CHECK"] = "1"
    env["MTOOLS_NO_VFAT"] = "0"
    env["LC_ALL"] = "C.UTF-8"
    return env


def _mcopy(image: Path, src: Path, dest: str) -> None:
    run("mcopy", "-m", "-o", "-i", str(image), str(src), f"::{dest}", env=_mtools_env(image))


def build_fat(path: Path, bits: int) -> Manifest:
    m = Manifest()
    path.unlink(missing_ok=True)
    if bits == 12:
        size_kb, cluster = 1440, 1
    elif bits == 16:
        size_kb, cluster = 32 * 1024, 4
    else:
        size_kb, cluster = 64 * 1024, 1
    run("mkfs.vfat", "-F", str(bits), "-n", f"FAT{bits}TEST", "-s", str(cluster), "-C", str(path), str(size_kb))
    env = _mtools_env(path)
    work = Path(tempfile.mkdtemp(prefix="lbfat-"))
    small = bits == 12

    def put(rel: str, content: bytes, mtime: float = T0, **extra) -> None:
        src = work / "src"
        src.write_bytes(content)
        os.utime(src, (mtime, mtime))
        parent = os.path.dirname(rel)
        if parent:
            parts = parent.split("/")
            for i in range(1, len(parts) + 1):
                run("mmd", "-D", "s", "-i", str(path), "::" + "/".join(parts[:i]), check=False, env=env)
        _mcopy(path, src, rel)
        m.add(rel, content, mtime=mtime, **extra)

    try:
        put("README.TXT", text("readme", 3000), T0 + 10)
        put("Long File Name With Spaces.txt", text("lfn", 1500), T0 + 20)
        put("DCIM/100CANON/IMG_0001.JPG", data("img1", 30_000 if small else 400_000), T0 + 30)
        put("DCIM/100CANON/IMG_0002.JPG", data("img2", 20_000 if small else 350_000), T0 + 31)
        put("Docs/Very Long Directory Name Number One/nested file.dat", data("nested", 5000), T0 + 40)
        put("unicode/café.txt", text("cafe", 300), T0 + 50)
        put("unicode/Ünïcödé naïve.txt", text("uni", 200), T0 + 51)
        put("empty.txt", b"", T0 + 60)
        # mixed case short names (NT case flags)
        put("lower.txt", text("lower", 100), T0 + 61)
        put("MiXeD.TxT", text("mixed", 100), T0 + 62)
        run("mattrib", "+h", "-i", str(path), "::lower.txt", env=env)
        # Files deleted at the very end; written now, while free space is contiguous.
        put("Trash/gone.jpg", data("gone", 8000 if small else 120_000), T0 + 80)
        put("Trash/a much longer deleted name.txt", text("gonelfn", 2000), T0 + 81)
        put("Trash/x.txt", text("x1", 300), T0 + 82)
        put("OldDir/inside1.txt", text("od1", 900), T0 + 83)
        put("OldDir/inside2.bin", data("od2", 7000), T0 + 84)
        if not small:
            for i in range(300):
                put(f"many/file_{i:03d}.txt", text(f"many{i}", 50 + i), T0 + 100 + i)
            # Fragmentation: fill the volume, free every other filler, write a big file.
            run("mmd", "-i", str(path), "::fill", env=env)
            src = work / "filler"
            src.write_bytes(data("filler", 128 * 1024))
            count = 0
            while count < 2000:
                res = run("mcopy", "-o", "-i", str(path), str(src), f"::fill/f{count:04d}.bin",
                          check=False, env=env)
                if res.returncode != 0:
                    run("mdel", "-i", str(path), f"::fill/f{count:04d}.bin", check=False, env=env)
                    break
                count += 1
            for i in range(0, count, 2):
                run("mdel", "-i", str(path), f"::fill/f{i:04d}.bin", env=env)
            put("fragmented.bin", data("fatfrag", 600_000), T0 + 70, fragmented=True)
        # Deletions last: nothing is written after this point.
        run("mdel", "-i", str(path), "::Trash/gone.jpg", env=env)
        run("mdel", "-i", str(path), "::Trash/a much longer deleted name.txt", env=env)
        run("mdel", "-i", str(path), "::Trash/x.txt", env=env)
        run("mdeltree", "-i", str(path), "::OldDir", env=env)
        for rel in ("Trash/gone.jpg", "Trash/a much longer deleted name.txt", "Trash/x.txt",
                    "OldDir/inside1.txt", "OldDir/inside2.bin"):
            m.mark_deleted(rel)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return m


def build_exfat(path: Path, size_mb: int = 96) -> Manifest:
    m = Manifest()
    path.unlink(missing_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(size_mb * MiB)
    run("mkfs.exfat", "-L", "EXFATTEST", "-c", "4K", str(path))
    with Mounted(path, "exfat") as root:
        _write(root, "DCIM/100MEDIA/DJI_0001.MP4", data("video", 3 * MiB), m, T0 + 1)
        _write(root, "DCIM/100MEDIA/DJI_0002.JPG", data("jpg", 500_000), m, T0 + 2)
        _write(root, "Documents/budget 2024.xlsx", data("xlsx", 77_777), m, T0 + 3)
        _write(root, "Documents/deep/er/and/deeper.txt", text("deep", 1234), m, T0 + 4)
        _write(root, "ユニコード/ファイル.txt", text("uni", 333), m, T0 + 5)
        _write(root, "A file with a really really long name that needs several name entries.txt",
               text("longname", 100), m, T0 + 6)
        _write(root, "empty.bin", b"", m, T0 + 7)
        for i in range(200):
            _write(root, f"many/item {i:03d}.txt", text(f"exitem{i}", 60 + i), m, T0 + 10 + i)
        # fragmented file
        tmp = root / "tmp"
        tmp.mkdir()
        for i in range(24):
            (tmp / f"t{i:02d}.bin").write_bytes(data(f"ext{i}", 64 * 1024))
        os.sync()
        for i in range(0, 24, 2):
            (tmp / f"t{i:02d}.bin").unlink()
        os.sync()
        for i in range(1, 24, 2):
            m.add(f"tmp/t{i:02d}.bin", data(f"ext{i}", 64 * 1024))
        _write(root, "fragmented.bin", data("exfrag", 1_500_000), m, T0 + 300, fragmented=True)
        # deleted (written last)
        _write(root, "Trash/deleted video.mp4", data("delvid", 900_000), m, T0 + 400)
        _write(root, "Trash/deleted note.txt", text("delnote", 4000), m, T0 + 401)
        _write(root, "Removed/sub/file in removed dir.txt", text("rmdir", 2500), m, T0 + 402)
        os.sync()
        (root / "Trash/deleted video.mp4").unlink()
        (root / "Trash/deleted note.txt").unlink()
        shutil.rmtree(root / "Removed")
        for rel in ("Trash/deleted video.mp4", "Trash/deleted note.txt", "Removed/sub/file in removed dir.txt"):
            m.mark_deleted(rel)
    return m


def _place(disk: Path, fs_image: Path, start_lba: int, sector: int = 512) -> None:
    with open(fs_image, "rb") as src, open(disk, "r+b") as dst:
        dst.seek(start_lba * sector)
        while True:
            block = src.read(4 * MiB)
            if not block:
                break
            dst.write(block)


def build_mbr_disk(path: Path, fat32: Path, ntfs: Path, exfat: Path) -> dict:
    """MBR disk: primary FAT32, then an extended partition with NTFS and exFAT logicals."""
    sizes = {k: v.stat().st_size // 512 for k, v in (("fat32", fat32), ("ntfs", ntfs), ("exfat", exfat))}
    p1 = 2048
    ext = p1 + sizes["fat32"] + 2048
    l1 = ext + 2048
    l2 = l1 + sizes["ntfs"] + 2048
    total = l2 + sizes["exfat"] + 4096
    path.unlink(missing_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(total * 512)
    script = (
        "label: dos\n"
        f"start={p1}, size={sizes['fat32']}, type=c\n"
        f"start={ext}, size={total - ext - 2048}, type=5\n"
        f"start={l1}, size={sizes['ntfs']}, type=7\n"
        f"start={l2}, size={sizes['exfat']}, type=7\n"
    )
    run("sfdisk", "--no-reread", "--no-tell-kernel", str(path), input=script)
    _place(path, fat32, p1)
    _place(path, ntfs, l1)
    _place(path, exfat, l2)
    return {"fat32": p1 * 512, "ntfs": l1 * 512, "exfat": l2 * 512}


def build_gpt_disk(path: Path, fat32: Path, ntfs: Path) -> dict:
    sizes = {k: v.stat().st_size // 512 for k, v in (("fat32", fat32), ("ntfs", ntfs))}
    p1 = 2048
    p2 = p1 + sizes["fat32"] + 2048
    total = p2 + sizes["ntfs"] + 4096
    path.unlink(missing_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(total * 512)
    run("sgdisk", "-o", str(path))
    run("sgdisk", "-n", f"1:{p1}:+{sizes['fat32']}", "-t", "1:ef00", "-c", "1:EFI system partition", str(path))
    run("sgdisk", "-n", f"2:{p2}:+{sizes['ntfs']}", "-t", "2:0700", "-c", "2:Basic data partition", str(path))
    _place(path, fat32, p1)
    _place(path, ntfs, p2)
    return {"fat32": p1 * 512, "ntfs": p2 * 512}


def build_all(out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    stamp = out / "VERSION"
    index_path = out / "index.json"
    if stamp.exists() and stamp.read_text() == VERSION and index_path.exists():
        return json.loads(index_path.read_text())
    for item in out.iterdir():
        if item.is_file():
            item.unlink()
    index: dict = {}
    for name, builder in (
        ("ntfs", lambda p: build_ntfs(p)),
        ("fat32", lambda p: build_fat(p, 32)),
        ("fat16", lambda p: build_fat(p, 16)),
        ("fat12", lambda p: build_fat(p, 12)),
        ("exfat", lambda p: build_exfat(p)),
    ):
        image = out / f"{name}.img"
        manifest = builder(image)
        manifest.save(out / f"{name}.json")
        index[name] = {"image": image.name, "manifest": f"{name}.json"}
    index["mbr_disk"] = {"image": "mbr_disk.img",
                         "offsets": build_mbr_disk(out / "mbr_disk.img", out / "fat32.img", out / "ntfs.img",
                                                   out / "exfat.img")}
    index["gpt_disk"] = {"image": "gpt_disk.img",
                         "offsets": build_gpt_disk(out / "gpt_disk.img", out / "fat32.img", out / "ntfs.img")}
    index_path.write_text(json.dumps(index, indent=1))
    stamp.write_text(VERSION)
    return index


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/.images")
    print(json.dumps(build_all(target), indent=1))
