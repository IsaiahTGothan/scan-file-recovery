from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

IMAGES = Path(os.environ.get("LIFEBOAT_TEST_IMAGES", ROOT / ".images"))


@pytest.fixture(scope="session")
def images() -> dict:
    """Build (once) and return the index of reference filesystem images."""
    from tests.fixtures.build_images import ToolMissing, build_all

    if sys.platform != "linux":
        pytest.skip("reference images are built with Linux filesystem tools")
    try:
        index = build_all(IMAGES)
    except ToolMissing as exc:
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
