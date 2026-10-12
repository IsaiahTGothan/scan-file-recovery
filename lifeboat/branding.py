"""Product identity. Change names, colours and the owner logo here."""

from __future__ import annotations

import sys
from pathlib import Path

APP_NAME = "Lifeboat"
APP_FULL_NAME = "Lifeboat Data Recovery"
APP_ID = "Zays.Lifeboat"
# The developer, as named in the app, the Windows file details and installer, and the Mac app info.
PUBLISHER = "ZAYS"
PUBLISHER_URL = "https://zays.us"
CREDIT = f"developed by {PUBLISHER}"
# Where settings are saved. Never rename it: everyone's saved settings would be left behind.
SETTINGS_ORG = "Zays"
TAGLINE = "Get your files off a failing drive."

# Brand palette (life-ring orange on deep navy).
ACCENT = "#FF6A1A"
ACCENT_DARK = "#D9530B"
NAVY = "#0E1A2B"

_OWNER_LOGO_NAMES = (
    "owner-logo.svg",
    "owner-logo.png",
    "owner-logo.jpg",
    "owner-logo.jpeg",
    "owner-logo.webp",
)


def assets_dir() -> Path:
    """Folder holding icons and the optional owner logo (works when frozen)."""
    frozen_base = getattr(sys, "_MEIPASS", None)
    if frozen_base:
        return Path(frozen_base) / "lifeboat" / "assets"
    return Path(__file__).resolve().parent / "assets"


def owner_logo() -> Path | None:
    """Return the owner's logo if one was dropped into ``lifeboat/assets``.

    The logo is optional: the application shows the publisher name in text
    when no logo file is present.
    """
    folder = assets_dir()
    for name in _OWNER_LOGO_NAMES:
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


def notices_file() -> Path | None:
    """THIRD_PARTY_NOTICES.md: next to the program when packaged, in the source folder otherwise."""
    frozen_base = getattr(sys, "_MEIPASS", None)
    base = Path(frozen_base) if frozen_base else Path(__file__).resolve().parent.parent
    path = base / "THIRD_PARTY_NOTICES.md"
    return path if path.is_file() else None
