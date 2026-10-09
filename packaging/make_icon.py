"""Render the Lifeboat icon to .ico/.png (and the installer side images).

Usage: python packaging/make_icon.py
Writes lifeboat/assets/lifeboat.ico, lifeboat.png and packaging/wizard-*.bmp.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main() -> None:
    from PIL import Image
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication([])  # noqa: F841 - needed for rendering
    from lifeboat.ui.icons import app_pixmap

    def render(size: int) -> Image.Image:
        pix = app_pixmap(size)
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        pix.save(buf, "PNG")
        return Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA")

    assets = ROOT / "lifeboat" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    big = render(256)
    big.save(assets / "lifeboat.png")
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    frames = {s: render(s) for s in sizes}
    frames[256].save(assets / "lifeboat.ico", format="ICO", sizes=[(s, s) for s in sizes],
                     append_images=[frames[s] for s in sizes if s != 256])
    # Inno Setup wizard images (BMP, no alpha): large 164x314 and small 55x55 at 100% scale.
    navy = (14, 26, 43)
    large = Image.new("RGB", (164, 314), navy)
    logo = render(120)
    large.paste(logo, (22, 96), logo)
    large.save(ROOT / "packaging" / "wizard-large.bmp")
    small = Image.new("RGB", (55, 55), (255, 255, 255))
    icon = render(51)
    small.paste(icon, (2, 2), icon)
    small.save(ROOT / "packaging" / "wizard-small.bmp")
    print("icons written to", assets)


if __name__ == "__main__":
    main()
