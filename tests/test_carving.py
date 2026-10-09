"""Carving: real sample files dropped at sector boundaries into random junk."""

from __future__ import annotations

import io
import os
import shutil
import sqlite3
import struct
import subprocess
import wave
import zipfile

import pytest

from lifeboat.device.base import DeviceInfo
from lifeboat.device.image import ImageDevice
from lifeboat.events import EventBus
from lifeboat.fs.carving import Carver
from lifeboat.rescue.reader import ReadPolicy, RescueReader
from lifeboat.scan.scanner import Scanner, ScanOptions
from tests.conftest import sha256
from tests.helpers import read_all

PIL = pytest.importorskip("PIL.Image")


def _jpeg(seed: int) -> bytes:
    from PIL import Image

    img = Image.effect_noise((320, 240), 40 + seed).convert("RGB")
    thumb = Image.effect_noise((64, 48), 20).convert("RGB")
    buf = io.BytesIO()
    exif = Image.Exif()
    exif[0x010F] = "LifeboatCam"
    # Embed a thumbnail-like JPEG in a comment to make sure it is not reported twice.
    tbuf = io.BytesIO()
    thumb.save(tbuf, "JPEG")
    img.save(buf, "JPEG", quality=85, exif=exif, comment=tbuf.getvalue()[:60000])
    return buf.getvalue()


def _png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.effect_noise((200, 150), 60).convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def _gif() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    frames = [Image.effect_noise((60, 60), 30 + i).convert("P") for i in range(3)]
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:])
    return buf.getvalue()


def _bmp() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.effect_noise((90, 70), 50).convert("RGB").save(buf, "BMP")
    return buf.getvalue()


def _tiff() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.effect_noise((120, 80), 50).convert("RGB").save(buf, "TIFF")
    return buf.getvalue()


def _webp() -> bytes | None:
    from PIL import Image, features

    if not features.check("webp"):
        return None
    buf = io.BytesIO()
    Image.effect_noise((100, 80), 50).convert("RGB").save(buf, "WEBP", quality=80)
    return buf.getvalue()


def _pdf() -> bytes:
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _zip(kind: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if kind == "docx":
            zf.writestr("[Content_Types].xml", "<Types/>")
            zf.writestr("word/document.xml", "<w:document>" + "hello " * 2000 + "</w:document>")
        elif kind == "xlsx":
            zf.writestr("[Content_Types].xml", "<Types/>")
            zf.writestr("xl/workbook.xml", "<workbook>" + "1," * 3000 + "</workbook>")
        else:
            zf.writestr("readme.txt", os.urandom(20000).hex())
            zf.writestr("data.bin", os.urandom(30000))
    return buf.getvalue()


def _sqlite(tmp_path) -> bytes:
    path = tmp_path / "db.sqlite"
    con = sqlite3.connect(path)
    con.execute("create table t (a, b)")
    con.executemany("insert into t values (?, ?)", [(i, os.urandom(50)) for i in range(2000)])
    con.commit()
    con.close()
    return path.read_bytes()


def _wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(os.urandom(16000))
    return buf.getvalue()


def _sevenzip() -> bytes:
    body = os.urandom(5000)
    header = b"\x01\x04\x06\x00\x01\x09"
    start = b"7z\xbc\xaf\x27\x1c\x00\x04" + struct.pack("<I", 0) + struct.pack("<QQ", len(body), len(header))
    start += struct.pack("<I", 0)
    return start + body + header


def _ffmpeg(tmp_path, ext: str, args: list[str]) -> bytes | None:
    if shutil.which("ffmpeg") is None:
        return None
    out = tmp_path / f"sample.{ext}"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *args, str(out)]
    if subprocess.run(cmd, capture_output=True).returncode != 0:
        return None
    return out.read_bytes()


def build_samples(tmp_path) -> dict[str, bytes]:
    samples = {
        "jpg1": _jpeg(1),
        "jpg2": _jpeg(2),
        "png": _png(),
        "gif": _gif(),
        "bmp": _bmp(),
        "tif": _tiff(),
        "pdf": _pdf(),
        "docx": _zip("docx"),
        "xlsx": _zip("xlsx"),
        "zip": _zip("zip"),
        "sqlite": _sqlite(tmp_path),
        "wav": _wav(),
        "7z": _sevenzip(),
    }
    webp = _webp()
    if webp is not None:
        samples["webp"] = webp
    video = ["-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10"]
    audio = ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    for ext, args in (
        ("mp4", [*video, "-pix_fmt", "yuv420p"]),
        ("mov", [*video, "-pix_fmt", "yuv420p"]),
        ("mkv", video),
        ("avi", video),
        ("mp3", [*audio, "-id3v2_version", "3", "-metadata", "title=Test"]),
        ("ogg", [*audio, "-c:a", "libvorbis"]),
    ):
        data = _ffmpeg(tmp_path, ext, args)
        if data is not None:
            samples[ext] = data
    return samples


def test_carving_finds_all_samples(tmp_path):
    samples = build_samples(tmp_path)
    image = bytearray()
    placed: dict[int, str] = {}
    rng = os.urandom
    for name, blob in samples.items():
        image += rng(512 * 7 + 123)
        image += bytes(-len(image) % 512)
        placed[len(image)] = name
        image += blob
    image += rng(4096)
    image += bytes(-len(image) % 512)
    path = tmp_path / "raw.img"
    path.write_bytes(bytes(image))

    device = ImageDevice(path)
    reader = RescueReader(device, ReadPolicy(timeout=1.0))
    carver = Carver(reader)
    chunk = 1 << 20
    for pos in range(0, device.size, chunk):
        carver.feed(pos, reader.read_bytes(pos, min(chunk, device.size - pos)))
    found = {}
    for group in carver.root.children:
        for node in group.children:
            found[node.ref[0]] = node
    problems = []
    for offset, name in placed.items():
        node = found.get(offset)
        if node is None:
            problems.append(f"{name} not found at {offset}")
            continue
        data, _states = read_all(carver.volume, node)
        if sha256(data) != sha256(samples[name]):
            problems.append(f"{name}: wrong size {node.size} vs {len(samples[name])}")
    extra = sorted(set(found) - set(placed))
    assert not problems, problems
    assert not extra, f"false positives at {extra}"


def test_deep_scan_includes_carved_files(tmp_path):
    jpeg = _jpeg(5)
    image = os.urandom(65536) + jpeg + bytes(-len(jpeg) % 512) + os.urandom(65536)
    path = tmp_path / "img.dd"
    path.write_bytes(image)
    device = ImageDevice(path)
    reader = RescueReader(device, ReadPolicy(timeout=1.0))
    scanner = Scanner(reader, DeviceInfo(path=str(path), kind="image", size=device.size), events=EventBus(),
                      options=ScanOptions(find_partitions=True, carve=True))
    result = scanner.deep_scan()
    assert result.carved == 1
    carved = [n for n in result.root.walk() if n.ref and isinstance(n.ref, tuple) and n.ref[0] == 65536]
    assert carved and carved[0].size == len(jpeg)
