"""Line icons drawn as SVG and tinted for the current theme."""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QObject, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from ..branding import ACCENT, NAVY, assets_dir, owner_logo

_S = 'fill="none" stroke="{c}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"'

ICONS: dict[str, str] = {
    "hdd": f'<rect x="2.5" y="6" width="19" height="12" rx="2.5" {_S}/><path d="M6 14h7" {_S}/>'
           f'<circle cx="17.5" cy="14" r="1.1" fill="{{c}}"/>',
    "usb": f'<rect x="7" y="9" width="10" height="13" rx="2" {_S}/><path d="M9 9V3h6v6" {_S}/>'
           f'<path d="M10.5 5.5h.01M13.5 5.5h.01" {_S}/>',
    "sd": f'<path d="M7 2.5h7.5L19 7v13a1.5 1.5 0 0 1-1.5 1.5h-10A1.5 1.5 0 0 1 6 20V4" {_S}/>'
          f'<path d="M10 6v3M13 6v3M16 7.5V9" {_S}/>',
    "ssd": f'<rect x="3" y="5" width="18" height="14" rx="2" {_S}/><rect x="7" y="9" width="5" height="6" rx="1" {_S}/>'
           f'<path d="M15 10h3M15 14h3" {_S}/>',
    "image": f'<circle cx="12" cy="12" r="9" {_S}/><circle cx="12" cy="12" r="2.5" {_S}/>'
             f'<path d="M12 3a9 9 0 0 1 9 9" {_S} opacity=".5"/>',
    "folder": f'<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6H9l2 2.5h8.5A1.5 1.5 0 0 1 21 10v8.5a1.5 1.5 0 0 1-1.5 1.5h-15'
              f'A1.5 1.5 0 0 1 3 18.5z" {_S}/>',
    "file": f'<path d="M14 2.5H7A1.5 1.5 0 0 0 5.5 4v16A1.5 1.5 0 0 0 7 21.5h10a1.5 1.5 0 0 0 1.5-1.5V7z" {_S}/>'
            f'<path d="M14 2.5V7h4.5" {_S}/>',
    "photo": f'<rect x="3" y="4.5" width="18" height="15" rx="2" {_S}/><circle cx="9" cy="10" r="1.8" {_S}/>'
             f'<path d="m21 15.5-4.5-4.5L8 19.5" {_S}/>',
    "video": f'<rect x="2.5" y="5.5" width="14" height="13" rx="2" {_S}/><path d="m16.5 10 5-3v10l-5-3" {_S}/>',
    "audio": f'<path d="M9 18V5.5l11-2V16" {_S}/><circle cx="6.5" cy="18" r="2.5" {_S}/>'
             f'<circle cx="17.5" cy="16" r="2.5" {_S}/>',
    "doc": f'<path d="M14 2.5H7A1.5 1.5 0 0 0 5.5 4v16A1.5 1.5 0 0 0 7 21.5h10a1.5 1.5 0 0 0 1.5-1.5V7z" {_S}/>'
           f'<path d="M14 2.5V7h4.5M9 12h6M9 15.5h6M9 8.5h2" {_S}/>',
    "archive": f'<rect x="3" y="3.5" width="18" height="5" rx="1.5" {_S}/><path d="M4.5 8.5V19a1.5 1.5 0 0 0 1.5 '
               f'1.5h12a1.5 1.5 0 0 0 1.5-1.5V8.5M10 12.5h4" {_S}/>',
    "mail": f'<rect x="2.5" y="5" width="19" height="14" rx="2" {_S}/><path d="m3 6.5 9 6.5 9-6.5" {_S}/>',
    "db": f'<ellipse cx="12" cy="5.5" rx="8" ry="3" {_S}/><path d="M4 5.5v13c0 1.7 3.6 3 8 3s8-1.3 8-3v-13'
          f'M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3" {_S}/>',
    "search": f'<circle cx="10.5" cy="10.5" r="6.5" {_S}/><path d="m20.5 20.5-5-5" {_S}/>',
    "radar": f'<circle cx="12" cy="12" r="9" {_S}/><circle cx="12" cy="12" r="5" {_S}/>'
             f'<path d="M12 12 18.5 5.5" {_S}/><circle cx="12" cy="12" r="1" fill="{{c}}"/>',
    "recover": f'<path d="M12 3v11M7.5 9.5 12 14l4.5-4.5" {_S}/><path d="M4 14.5v3A2.5 2.5 0 0 0 6.5 20h11'
               f'a2.5 2.5 0 0 0 2.5-2.5v-3" {_S}/>',
    "clone": f'<rect x="8" y="8" width="13" height="13" rx="2" {_S}/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9'
             f'a2 2 0 0 0 2 2h3" {_S}/>',
    "stop": f'<rect x="6" y="6" width="12" height="12" rx="2" {_S}/>',
    "pause": f'<path d="M9 5v14M15 5v14" {_S}/>',
    "play": f'<path d="M7 4.5v15l12-7.5z" {_S}/>',
    "skip": f'<path d="M5 4.5v15l10-7.5zM19 5v14" {_S}/>',
    "settings": f'<circle cx="12" cy="12" r="3" {_S}/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 '
                f'2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 '
                f'19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 '
                f'0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-'
                f'2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 '
                f'1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 '
                f'1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" {_S}/>',
    "info": f'<circle cx="12" cy="12" r="9" {_S}/><path d="M12 11v5M12 7.5h.01" {_S}/>',
    "warning": f'<path d="M10.3 3.9 2.4 17.6A2 2 0 0 0 4.1 20.5h15.8a2 2 0 0 0 1.7-2.9L13.7 3.9a2 2 0 0 0-3.4 0z" {_S}/>'
               f'<path d="M12 9.5v4M12 17h.01" {_S}/>',
    "error": f'<circle cx="12" cy="12" r="9" {_S}/><path d="m15 9-6 6M9 9l6 6" {_S}/>',
    "success": f'<circle cx="12" cy="12" r="9" {_S}/><path d="m8 12.5 2.8 2.8L16.5 9.5" {_S}/>',
    "refresh": f'<path d="M20 11a8 8 0 1 0-2.3 5.7" {_S}/><path d="M20 4v7h-7" {_S}/>',
    "open": f'<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6H9l2 2.5h8.5A1.5 1.5 0 0 1 21 10v1" {_S}/>'
            f'<path d="m3 18.5 2.6-6.4A1.5 1.5 0 0 1 7 11h13.3a1 1 0 0 1 .9 1.4L18.6 19a1.5 1.5 0 0 1-1.4 1H4.5'
            f'A1.5 1.5 0 0 1 3 18.5z" {_S}/>',
    "report": f'<path d="M14 2.5H7A1.5 1.5 0 0 0 5.5 4v16A1.5 1.5 0 0 0 7 21.5h10a1.5 1.5 0 0 0 1.5-1.5V7z" {_S}/>'
              f'<path d="M14 2.5V7h4.5M9 17v-3M12 17v-6M15 17v-4" {_S}/>',
    "eye": f'<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" {_S}/><circle cx="12" cy="12" r="3" {_S}/>',
    "grid": f'<rect x="3" y="3" width="7" height="7" rx="1" {_S}/><rect x="14" y="3" width="7" height="7" rx="1" {_S}/>'
            f'<rect x="3" y="14" width="7" height="7" rx="1" {_S}/><rect x="14" y="14" width="7" height="7" rx="1" {_S}/>',
    "lock": f'<rect x="4.5" y="10.5" width="15" height="10.5" rx="2" {_S}/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5" {_S}/>',
    "help": f'<circle cx="12" cy="12" r="9" {_S}/><path d="M9.3 9a2.8 2.8 0 0 1 5.4 1c0 1.8-2.7 2.5-2.7 2.5'
            f'M12 16.8h.01" {_S}/>',
    "list": f'<path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01" {_S}/>',
    "check": f'<path d="m5 12.5 4.5 4.5L19 7.5" {_S}/>',
    "close": f'<path d="M6 6l12 12M18 6 6 18" {_S}/>',
    "lifebuoy": f'<circle cx="12" cy="12" r="9" {_S}/><circle cx="12" cy="12" r="4" {_S}/>'
                f'<path d="m4.9 4.9 4.3 4.3M14.8 14.8l4.3 4.3M14.8 9.2l4.3-4.3M9.2 14.8l-4.3 4.3" {_S}/>',
    "pulse": f'<path d="M2.5 12h4l2.5-6.5 4.5 13 2.8-6.5h5.2" {_S}/>',
    "star": f'<path d="M12 3.2l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17.2l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z" {_S}/>',
    "key": f'<circle cx="7.5" cy="15.5" r="4" {_S}/><path d="M10.4 12.6 20 3M16.5 6.5 19 9M14 9l2 2" {_S}/>',
    "gift": f'<rect x="3" y="8" width="18" height="4.5" rx="1" {_S}/><path d="M5 12.5V20a1 1 0 0 0 1 1h12a1 1 0 0 0 '
            f'1-1v-7.5M12 8v13M12 8C11 5 9.8 3.5 8 3.5a2.25 2.25 0 0 0 0 4.5M12 8c1-3 2.2-4.5 4-4.5a2.25 2.25 0 0 1 0 '
            f'4.5" {_S}/>',
    "palette": f'<path d="M12 3a9 9 0 1 0 0 18c1.2 0 1.8-.8 1.8-1.7 0-.5-.2-.9-.5-1.2-.3-.4-.5-.8-.5-1.3 0-1 .8-1.8 '
               f'1.8-1.8H17a4 4 0 0 0 4-4C21 6.6 17 3 12 3z" {_S}/><circle cx="7.5" cy="11.5" r="1.3" fill="{{c}}"/>'
               f'<circle cx="10.5" cy="7.2" r="1.3" fill="{{c}}"/><circle cx="15.5" cy="7.8" r="1.3" fill="{{c}}"/>',
    "keyboard": f'<rect x="2.5" y="6" width="19" height="12" rx="2" {_S}/><path d="M6 10h.01M10 10h.01M14 10h.01'
                f'M18 10h.01M7.5 14h9" {_S}/>',
    "globe": f'<circle cx="12" cy="12" r="9" {_S}/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" {_S}/>',
    "user": f'<circle cx="12" cy="8" r="4" {_S}/><path d="M4.5 20.5a7.5 7.5 0 0 1 15 0" {_S}/>',
    "sliders": f'<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1" {_S}/><circle cx="15" cy="6" r="2" {_S}/>'
               f'<circle cx="9" cy="12" r="2" {_S}/><circle cx="17" cy="18" r="2" {_S}/>',
    "sparkle": f'<path d="M11 3.5l1.9 5.1 5.1 1.9-5.1 1.9L11 17.5l-1.9-5.1L4 10.5l5.1-1.9z" {_S}/>'
               f'<path d="M18.5 15.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z" {_S}/>',
    "bell": f'<path d="M6 16.5V11a6 6 0 1 1 12 0v5.5l1.5 2h-15z" {_S}/><path d="M10 20.5a2 2 0 0 0 4 0" {_S}/>',
    "clock": f'<circle cx="12" cy="12" r="9" {_S}/><path d="M12 7v5l3.2 2" {_S}/>',
}

APP_ICON_SVG = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">
<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#16263b"/>
<stop offset="1" stop-color="{NAVY}"/></linearGradient></defs>
<rect x="8" y="8" width="240" height="240" rx="56" fill="url(#g)"/>
<circle cx="128" cy="128" r="74" fill="none" stroke="#ffffff" stroke-width="34"/>
<g fill="none" stroke="{ACCENT}" stroke-width="34">
<path d="M128 54 A74 74 0 0 1 180.3 75.7"/><path d="M202 128 A74 74 0 0 1 180.3 180.3"/>
<path d="M128 202 A74 74 0 0 1 75.7 180.3"/><path d="M54 128 A74 74 0 0 1 75.7 75.7"/></g>
<g fill="none" stroke="#0b1522" stroke-opacity=".35" stroke-width="3">
<circle cx="128" cy="128" r="91"/><circle cx="128" cy="128" r="57"/></g>
</svg>"""


def _svg(name: str, color: str) -> bytes:
    body = ICONS[name].replace("{c}", color)
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">{body}</svg>'.encode()


def screen_ratio() -> float:
    """Device pixels per point on the sharpest screen (2 on a Retina Mac, 1.5 at 150 % on Windows)."""
    app = QGuiApplication.instance()
    return max(1.0, app.devicePixelRatio()) if isinstance(app, QGuiApplication) else 1.0


def _render(svg: bytes, size: int, ratio: float = 1.0) -> QPixmap:
    """``size`` points square, drawn with ``ratio`` device pixels per point so it stays sharp."""
    pixels = round(size * ratio)
    renderer = QSvgRenderer(QByteArray(svg))
    image = QImage(pixels, pixels, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, pixels, pixels))
    painter.end()
    result = QPixmap.fromImage(image)
    result.setDevicePixelRatio(ratio)
    return result


@lru_cache(maxsize=512)
def icon(name: str, color: str | None = None) -> QIcon:
    from .theme import current

    tint = color or current().text
    result = QIcon()
    for size in (16, 20, 24, 32, 48, 64):
        result.addPixmap(_render(_svg(name, tint), size))
        if tint.lower() == "#ffffff":
            # On a disabled button: muted (Qt's greyed white would vanish on a light theme)
            result.addPixmap(_render(_svg(name, current().muted), size), QIcon.Mode.Disabled)
    return result


@lru_cache(maxsize=64)
def pixmap(name: str, size: int, color: str | None = None) -> QPixmap:
    from .theme import current

    return _render(_svg(name, color or current().text), size, screen_ratio())


@lru_cache(maxsize=4)
def app_icon() -> QIcon:
    result = QIcon()
    ico = assets_dir() / "lifeboat.ico"
    if ico.is_file():
        result.addFile(str(ico))
    for size in (16, 24, 32, 48, 64, 128, 256):
        result.addPixmap(_render(APP_ICON_SVG.encode(), size))
    return result


def app_pixmap(size: int) -> QPixmap:
    return _render(APP_ICON_SVG.encode(), size, screen_ratio())


def _one_colour(image: QImage) -> bool:
    """Every visible pixel the same colour (a logo in white, or in black, on transparent)."""
    step = max(1, min(image.width(), image.height()) // 48)
    seen: QColor | None = None
    for y in range(0, image.height(), step):
        for x in range(0, image.width(), step):
            c = image.pixelColor(x, y)
            if c.alpha() < 200:
                continue
            if seen is None:
                seen = c
            elif max(abs(c.red() - seen.red()), abs(c.green() - seen.green()), abs(c.blue() - seen.blue())) > 48:
                return False
    return seen is not None


@lru_cache(maxsize=8)
def owner_pixmap(height: int, color: str | None = None) -> QPixmap | None:
    """The developer's logo (lifeboat/assets/owner-logo.*), ``height`` pixels tall, sharp on high-DPI
    screens. A one-colour logo is drawn in the theme's text colour, so it shows on the dark and the
    light theme alike; a logo in colour is drawn as it is."""
    from .theme import current

    path = owner_logo()
    image = QImage(str(path)) if path is not None else QImage()
    if image.isNull():
        return None
    ratio = screen_ratio()
    image = image.scaledToHeight(round(height * ratio), Qt.TransformationMode.SmoothTransformation)
    image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    if _one_colour(image):
        painter = QPainter(image)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(image.rect(), QColor(color or current().text))
        painter.end()
    result = QPixmap.fromImage(image)
    result.setDevicePixelRatio(ratio)
    return result


def clear_cache() -> None:
    icon.cache_clear()
    pixmap.cache_clear()
    owner_pixmap.cache_clear()


# Icons given with put() remember their name and colour role, so refresh() can draw them again
# after the theme or the accent colour changed.
_ROLES = ("", "white", "accent", "muted", "bad", "ok", "warn", "info")


def _tint(role: str) -> str | None:
    from .theme import current

    t = current()
    return {"white": "#ffffff", "accent": t.accent, "muted": t.muted, "bad": t.bad, "ok": t.ok, "warn": t.warn,
            "info": t.info}.get(role)


def put(target: QObject, name: str, role: str = "") -> None:
    """Give a button or action an icon tinted for the current theme (``role``: a colour of the
    theme, "" for the text colour) and remember it for refresh()."""
    assert role in _ROLES, role
    target.setProperty("lb_icon", name)
    target.setProperty("lb_icon_role", role)
    target.setIcon(icon(name, _tint(role)))  # type: ignore[attr-defined]


def refresh(root: QObject) -> None:
    """Draw again, in the current theme's colours, every icon given with put() under ``root``."""
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QAbstractButton

    for target in [root, *root.findChildren(QAbstractButton), *root.findChildren(QAction)]:
        name = target.property("lb_icon")
        if name:
            target.setIcon(icon(str(name), _tint(str(target.property("lb_icon_role") or ""))))  # type: ignore[attr-defined]


def color(hex_value: str) -> QColor:
    return QColor(hex_value)


ICON_SIZE = QSize(18, 18)
