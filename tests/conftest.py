from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
# Keep the GUI tests away from the settings of a Lifeboat installed on the same machine.
os.environ.setdefault("LIFEBOAT_SETTINGS_NAME", "Lifeboat tests")
# On Windows, Qt's offscreen platform (the GUI tests' screen) reads fonts from a folder of Qt's own
# that PySide6 doesn't ship; without fonts it draws every letter as a box, twice as wide as text.
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
# The font the app gets from the system, for the GUI tests (the offscreen platform has none).
UI_FONT = {"win32": "Segoe UI"}.get(sys.platform, "")

IMAGES = Path(os.environ.get("LIFEBOAT_TEST_IMAGES", ROOT / ".images"))
APPLE = ROOT / "fixtures" / "apple"      # disks made by macOS (tests/macos/make_fixtures.py)


@pytest.fixture(autouse=True)
def _default_formats():
    """Sizes in 1,024-byte KB and year-month-day dates after every test: the app's settings can
    change both (and on a Mac, "match this computer" means 1,000-byte KB)."""
    yield
    from lifeboat.util import set_size_units, set_time_style

    set_size_units(False)
    set_time_style("ymd")


@pytest.fixture(scope="session")
def images() -> dict:
    """Build (once) and return the index of reference filesystem images."""
    from tests.fixtures.build_images import ToolMissing, build_all

    if sys.platform != "linux":
        pytest.skip("reference images are built with Linux filesystem tools")
    try:
        index = build_all(IMAGES)
    except ToolMissing as exc:
        if os.environ.get("LIFEBOAT_REQUIRE_IMAGES"):
            raise
        pytest.skip(f"image tool not installed: {exc}")
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("LIFEBOAT_REQUIRE_IMAGES"):
            raise
        pytest.skip(f"could not build reference images: {exc}")
    return index


def image_path(name: str) -> Path:
    return IMAGES / f"{name}.img"


def manifest(name: str) -> list[dict]:
    return json.loads((IMAGES / f"{name}.json").read_text())


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unpack(packed: Path, image: Path) -> None:
    """Gunzip a test disk, leaving its empty stretches as holes (they are mostly zeros)."""
    with gzip.open(packed, "rb") as src, open(image, "wb") as dst:
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                break
            if chunk.count(0) == len(chunk):
                dst.seek(len(chunk), os.SEEK_CUR)
            else:
                dst.write(chunk)
        dst.truncate()


@pytest.fixture(scope="session")
def disks(tmp_path_factory) -> dict[str, tuple[Path, dict]]:
    """The Mac-made test disks (HFS+, APFS): name -> (image file, manifest)."""
    out = {}
    folder = tmp_path_factory.mktemp("apple")
    for packed in sorted(APPLE.glob("*.img.gz")):
        name = packed.name[:-len(".img.gz")]
        image = folder / f"{name}.img"
        _unpack(packed, image)
        out[name] = (image, json.loads((APPLE / f"{name}.json").read_text(encoding="utf-8")))
    return out
