"""Custom widgets: source list, disk map, notifications, banner, preview."""

from __future__ import annotations

import html
import math
import time
from collections import deque
from collections.abc import Iterable

from PySide6.QtCore import QEvent, QModelIndex, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ..device.base import NO_MEDIA, NOT_ANSWERING, DeviceInfo
from ..events import Choice, Event, Level, Progress
from ..fs.apfs import LockedVolume
from ..fs.model import F, Node
from ..rescue.sectormap import PARTIAL, SectorMap, State
from ..util import format_duration, format_rate, format_size, format_timestamp
from . import icons
from .bridge import PendingDecision
from .models import category, category_icon, chance, status_text
from .theme import current

ROLE_INFO = Qt.ItemDataRole.UserRole + 1


# --------------------------------------------------------------------- sources
def device_icon_name(info: DeviceInfo) -> str:
    bus = info.bus.upper()
    if info.kind == "image" or bus == "DISK IMAGE":
        return "image"
    if "USB" in bus:
        return "usb"
    if bus in ("SD", "MMC", "SD/MMC", "SECURE DIGITAL"):
        return "sd"
    if "NVME" in bus or "FABRIC" in bus or "PCI" in bus:  # Apple Fabric / PCI-Express: internal SSDs
        return "ssd"
    return "hdd"


STATUS_TEXT = {NOT_ANSWERING: "Not answering", NO_MEDIA: "No media"}


def device_subtitle(info: DeviceInfo) -> str:
    parts = []
    if info.status:
        parts.append(STATUS_TEXT.get(info.status, "Not readable"))
    if info.kind == "image":
        parts.append("Disk image")
    elif info.kind == "volume":
        parts.append(f"Volume {info.filesystem or ''}".strip())
    elif info.bus:
        parts.append(info.bus)
    if info.size:
        parts.append(info.capacity_text)
    if info.kind == "disk" and info.volumes:
        parts.append(", ".join(v.split()[0] for v in info.volumes[:4]))
    return " · ".join(parts)


class SourceDelegate(QStyledItemDelegate):
    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(option.rect.width(), 58)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        info: DeviceInfo | None = index.data(ROLE_INFO)
        if info is None:
            super().paint(painter, option, index)
            return
        t = current()
        painter.save()
        rect = option.rect.adjusted(4, 3, -4, -3)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(t.selection if selected else t.panel_alt))
            painter.drawRoundedRect(rect, 8, 8)
        icon_name = device_icon_name(info)
        status_colour = {NOT_ANSWERING: t.bad, NO_MEDIA: t.muted}.get(info.status, t.muted)
        tint = status_colour if info.status else (t.accent if selected else t.text)
        painter.drawPixmap(rect.left() + 10, rect.top() + 12, icons.pixmap(icon_name, 26, tint))
        text_left = rect.left() + 48
        badge = ""
        if info.system:
            badge = "SYSTEM DISK" if info.kind == "disk" else "SYSTEM"
        elif info.status == NOT_ANSWERING:
            badge = "CHECK"
        badge_width = 0
        if badge:
            font = QFont(option.font)
            font.setPointSizeF(max(6.5, option.font.pointSizeF() - 2.5))
            font.setBold(True)
            painter.setFont(font)
            badge_width = painter.fontMetrics().horizontalAdvance(badge) + 12
            badge_rect = QRect(rect.right() - badge_width - 6, rect.top() + 8, badge_width, 16)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(t.bad if info.status == NOT_ANSWERING else t.border))
            painter.drawRoundedRect(badge_rect, 8, 8)
            painter.setPen(QColor("#ffffff" if info.status == NOT_ANSWERING else t.muted))
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge)
            badge_width += 10
        title_font = QFont(option.font)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(t.text))
        title_rect = QRect(text_left, rect.top() + 7, rect.width() - 56 - badge_width, 20)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(info.display_name, Qt.TextElideMode.ElideRight,
                                                          title_rect.width()))
        painter.setFont(option.font)
        painter.setPen(QColor(status_colour if info.status == NOT_ANSWERING else t.muted))
        sub_rect = QRect(text_left, rect.top() + 27, rect.width() - 56, 18)
        subtitle = device_subtitle(info)
        painter.drawText(sub_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(subtitle, Qt.TextElideMode.ElideRight, sub_rect.width()))
        painter.restore()


class SourceList(QListWidget):
    source_selected = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.setItemDelegate(SourceDelegate(self))
        self.setMouseTracking(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("QListWidget { background: transparent; border: none; }")
        self.currentItemChanged.connect(self._changed)

    def _changed(self, item: QListWidgetItem | None, _prev: QListWidgetItem | None) -> None:
        if item is not None:
            info = item.data(ROLE_INFO)
            if info is not None:
                self.source_selected.emit(info)

    def set_devices(self, devices: list[DeviceInfo], keep: str | None = None) -> None:
        self.blockSignals(True)
        self.clear()
        disks = [d for d in devices if d.kind == "disk"]
        # Empty card readers last: they're listed (a card no reader detects looks the same), but rarely the point.
        disks.sort(key=lambda d: d.status == NO_MEDIA)
        sections = [("Drives", disks),
                    ("Volumes (drive letters)", [d for d in devices if d.kind == "volume"]),
                    ("Disk images", [d for d in devices if d.kind == "image"])]
        reselect = None
        for title, items in sections:
            if not items:
                continue
            header = QListWidgetItem(title.upper())
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            font = header.font()
            font.setBold(True)
            font.setPointSizeF(max(7.5, font.pointSizeF() - 1.5))
            header.setFont(font)
            header.setForeground(QColor(current().muted))
            header.setSizeHint(QSize(10, 30))
            self.addItem(header)
            for info in items:
                item = QListWidgetItem()
                item.setData(ROLE_INFO, info)
                tip = [info.title, device_subtitle(info)]
                if info.serial:
                    tip.append(f"Serial: {info.serial}")
                tip.extend(info.notes)
                item.setToolTip("\n".join(tip))
                self.addItem(item)
                if keep and info.identity == keep:
                    reselect = item
        if reselect is not None:
            # Highlighting the current source again is not the user choosing a source: with
            # signals on, it re-entered select_source, which (while a job runs) refreshed this
            # list again - endlessly, until the app crashed.
            self.setCurrentItem(reselect)
        self.blockSignals(False)


# --------------------------------------------------------------------- disk map
def _blend(a: QColor, b: QColor, k: float) -> QColor:
    k = max(0.0, min(1.0, k))
    return QColor(int(a.red() + (b.red() - a.red()) * k), int(a.green() + (b.green() - a.green()) * k),
                  int(a.blue() + (b.blue() - a.blue()) * k))


class DiskMap(QWidget):
    """Grid of blocks, one per slice of the drive, coloured by read state.

    Animation (Settings > Appearance): a block that changes flashes and fades to its new colour,
    and the block being read glows. Frames are drawn only while the map is on screen and
    something moves (30 a second, or 8 in "light" mode); the blocks themselves are drawn once
    into an image when the map changes (at most once a second), so even a job of many hours
    costs next to nothing. With animation off the map is redrawn once a second when it changes.
    """

    CELL = 9
    GAP = 2
    FADE = 0.7            # seconds for a changed block to settle on its new colour
    MAX_FADES = 800       # more blocks changing at once (a small image read in a second) just switch
    FPS = {"smooth": 30, "light": 8}

    def __init__(self) -> None:
        super().__init__()
        self.map: SectorMap | None = None
        self.mode = "smooth"
        self.head: int | None = None              # where the drive is being read, while a job runs
        self.frames_drawn = 0
        self._cells: list[int] = []
        self._fades: dict[int, tuple[float, int]] = {}  # block -> (when it changed, previous state)
        self._base: QPixmap | None = None         # every block in its current colour
        self._version = -1
        self._hover = -1
        self.setMinimumHeight(120)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._timer = QTimer(self)                # the map's data: once a second
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1000)
        self._frames = QTimer(self)               # animation: only while something moves
        self._frames.timeout.connect(self._frame)

    # ------------------------------------------------------------------ state
    def set_mode(self, mode: str) -> None:
        self.mode = mode if mode in ("smooth", "light", "off") else "smooth"
        self._fades.clear()
        self._frames.stop()
        self._kick()
        self.update()

    def set_map(self, sector_map: SectorMap | None) -> None:
        self.map = sector_map
        self.head = None
        self._version = -1
        self._cells = []
        self._fades.clear()
        self._base = None
        self.refresh()
        self.update()

    def set_head(self, offset: int | None) -> None:
        if offset == self.head:
            return
        self.head = offset
        self._kick()
        if self.mode == "off":
            self.update()

    @property
    def animating(self) -> bool:
        return self._frames.isActive()

    def _grid(self) -> tuple[int, int]:
        step = self.CELL + self.GAP
        cols = max(1, (self.width() - 8) // step)
        rows = max(1, (self.height() - 8) // step)
        return cols, rows

    def block_bytes(self) -> int:
        """How much of the drive one block stands for."""
        if self.map is None or not self._cells:
            return 0
        return self.map.size // len(self._cells)

    def refresh(self) -> None:
        if not self.isVisible() or self.map is None:
            return
        cols, rows = self._grid()
        count = cols * rows
        if self.map.version == self._version and len(self._cells) == count:
            return
        self._version = self.map.version
        cells = self.map.summarize(count)
        if len(cells) == len(self._cells) and self._base is not None:
            changed = [i for i, (old, new) in enumerate(zip(self._cells, cells, strict=True)) if old != new]
            if self.mode != "off" and len(changed) <= self.MAX_FADES:
                now = time.monotonic()
                for index in changed:
                    self._fades[index] = (now, self._cells[index])
            self._cells = cells
            self._paint_cells(self._base, changed)  # only what changed: cheap even once a second
        else:
            self._fades.clear()
            self._cells = cells
            self._base = None
        self.update()
        self._kick()

    def invalidate(self) -> None:
        """Redraw everything (after a theme change)."""
        self._base = None
        self.update()

    def _kick(self) -> None:
        """Run the animation timer while something moves and the map can be seen."""
        if self.mode == "off" or self._frames.isActive() or not self.isVisible():
            return
        if self.window().isMinimized() or (not self._fades and self.head is None):
            return
        self._frames.start(1000 // self.FPS[self.mode])

    def _frame(self) -> None:
        if self.mode == "off" or not self.isVisible() or self.window().isMinimized():
            self._frames.stop()
            return
        now = time.monotonic()
        self._fades = {i: f for i, f in self._fades.items() if now - f[0] < self.FADE}
        if not self._fades and self.head is None:
            self._frames.stop()
        self.update()

    # ------------------------------------------------------------------ events
    def showEvent(self, event: QEvent) -> None:
        super().showEvent(event)  # type: ignore[arg-type]
        self._version = -1
        self.refresh()
        self._kick()

    def hideEvent(self, event: QEvent) -> None:
        self._frames.stop()
        super().hideEvent(event)  # type: ignore[arg-type]

    def resizeEvent(self, event: QEvent) -> None:
        self._version = -1
        self._fades.clear()
        self.refresh()
        super().resizeEvent(event)  # type: ignore[arg-type]

    def _index_at(self, pos: QPoint) -> int:
        cols, _rows = self._grid()
        step = self.CELL + self.GAP
        col, row = (pos.x() - 4) // step, (pos.y() - 4) // step
        if pos.x() < 4 or pos.y() < 4 or col >= cols:
            return -1
        index = row * cols + col
        return index if 0 <= index < len(self._cells) else -1

    def describe(self, index: int) -> str:
        """What one block stands for and how much of it was read."""
        if self.map is None or not 0 <= index < len(self._cells):
            return ""
        cell = self.map.size / len(self._cells)
        start, end = int(index * cell), int((index + 1) * cell)
        totals: dict[State, int] = {}
        for seg_start, seg_end, state in self.map.segments(start, end):
            totals[state] = totals.get(state, 0) + seg_end - seg_start
        span = max(1, end - start)
        names = ((State.GOOD, "Read OK"), (State.SKIPPED, "Skipped, retried later"), (State.FAILED, "Failed"),
                 (State.BAD, "Bad sectors"), (State.UNTRIED, "Not read yet"))
        lines = [f"Block {index + 1:,} of {len(self._cells):,}: {format_size(start)} to {format_size(end)}"]
        for state, label in names:
            amount = totals.get(state, 0)
            if amount:
                share = amount * 100 / span
                lines.append(f"{label}: {format_size(amount)} ({share:.0f}%)" if share >= 1
                             else f"{label}: {format_size(amount)} (<1%)")
        return "\n".join(lines)

    def mouseMoveEvent(self, event: QEvent) -> None:
        index = self._index_at(event.position().toPoint())  # type: ignore[attr-defined]
        if index == self._hover:
            return
        self._hover = index
        if index < 0:
            QToolTip.hideText()
        else:
            QToolTip.showText(event.globalPosition().toPoint(), self.describe(index), self)  # type: ignore[attr-defined]

    def leaveEvent(self, event: QEvent) -> None:
        self._hover = -1
        super().leaveEvent(event)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ painting
    def _colours(self) -> dict[int, QColor]:
        t = current()
        return {
            int(State.UNTRIED): QColor(t.map_untried), int(State.GOOD): QColor(t.map_good),
            PARTIAL: QColor(t.map_partial), int(State.SKIPPED): QColor(t.map_skipped),
            int(State.FAILED): QColor(t.map_failed), int(State.BAD): QColor(t.map_bad),
        }

    def _cell_rect(self, index: int, cols: int) -> QRect:
        step = self.CELL + self.GAP
        return QRect(4 + (index % cols) * step, 4 + (index // cols) * step, self.CELL, self.CELL)

    def _render_base(self) -> QPixmap:
        ratio = self.devicePixelRatioF()
        pixmap = QPixmap(int(self.width() * ratio), int(self.height() * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(QColor(current().panel))
        self._paint_cells(pixmap, range(len(self._cells)))
        return pixmap

    def _paint_cells(self, pixmap: QPixmap, indexes: Iterable[int]) -> None:
        colours = self._colours()
        untried = colours[int(State.UNTRIED)]
        cols, _rows = self._grid()
        painter = QPainter(pixmap)
        for index in indexes:
            painter.fillRect(self._cell_rect(index, cols), colours.get(self._cells[index], untried))
        painter.end()

    def paintEvent(self, _event: QEvent) -> None:
        t = current()
        painter = QPainter(self)
        if self.map is None or not self._cells:
            painter.fillRect(self.rect(), QColor(t.panel))
            painter.setPen(QColor(t.muted))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "The disk map shows which parts of the drive were read, skipped or found bad.")
            return
        colours = self._colours()
        cols, _rows = self._grid()
        if self._base is None or self._base.deviceIndependentSize().toSize() != self.size():
            self._base = self._render_base()
        painter.drawPixmap(0, 0, self._base)
        now = time.monotonic()
        untried = colours[int(State.UNTRIED)]
        white = QColor("#ffffff")
        for index, (began, previous) in self._fades.items():
            if index >= len(self._cells):
                continue
            k = (now - began) / self.FADE
            if k >= 1:
                continue
            target = colours.get(self._cells[index], untried)
            bright = _blend(target, white, 0.55)
            # Old colour -> a bright flash of the new one -> the new colour.
            colour = (_blend(colours.get(previous, untried), bright, k / 0.25) if k < 0.25
                      else _blend(bright, target, (k - 0.25) / 0.75))
            painter.fillRect(self._cell_rect(index, cols), colour)
        if self.head is not None and self.map.size > 0:
            index = min(len(self._cells) - 1, max(0, self.head * len(self._cells) // self.map.size))
            rect = self._cell_rect(index, cols)
            accent = QColor(t.accent)
            pulse = 1.0 if self.mode == "off" else 0.5 + 0.5 * math.sin(now * 2 * math.pi / 1.1)
            glow = QColor(accent)
            glow.setAlphaF(0.25 + 0.55 * pulse)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(glow, 2.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(rect).adjusted(-2.5, -2.5, 2.5, 2.5), 3, 3)
            painter.fillRect(rect, _blend(accent, white, 0.25 * pulse))
        self.frames_drawn += 1


class MapLegend(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        t = current()
        self.labels: dict[str, QLabel] = {}
        for key, colour, text in (("good", t.map_good, "Read OK"), ("partial", t.map_partial, "Partly read"),
                                  ("skipped", t.map_skipped, "Skipped (retry later)"),
                                  ("failed", t.map_failed, "Failed block"), ("bad", t.map_bad, "Bad sectors"),
                                  ("untried", t.map_untried, "Not read")):
            swatch = QLabel()
            swatch.setFixedSize(12, 12)
            swatch.setStyleSheet(f"background:{colour}; border-radius:2px;")
            label = QLabel(text)
            label.setProperty("muted", True)
            row = QHBoxLayout()
            row.setSpacing(6)
            row.addWidget(swatch)
            row.addWidget(label)
            layout.addLayout(row)
            self.labels[key] = label
        layout.addStretch(1)
        self.totals = QLabel()
        self.totals.setProperty("muted", True)
        layout.addWidget(self.totals)

    def update_totals(self, sector_map: SectorMap | None, block: int = 0) -> None:
        if sector_map is None:
            self.totals.setText("")
            return
        totals = sector_map.totals()
        text = (f"OK {format_size(totals[State.GOOD])} · bad {format_size(totals[State.BAD])} · "
                f"failed {format_size(totals[State.FAILED])} · skipped {format_size(totals[State.SKIPPED])}")
        if block:
            text += f"   ·   1 block = {format_size(block)}"
        self.totals.setText(text)
        self.totals.setToolTip("Point at a block in the map to see what it holds.")


# --------------------------------------------------------------- notifications
class Toast(QFrame):
    closed = Signal(object)

    def __init__(self, parent: QWidget, level: str, title: str, message: str, timeout_ms: int) -> None:
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setProperty("level", level)
        t = current()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 8, 10)
        layout.setSpacing(10)
        icon_name, colour = {
            "error": ("error", t.bad), "critical": ("error", t.bad), "warning": ("warning", t.warn),
            "success": ("success", t.ok),
        }.get(level, ("info", t.info))
        glyph = QLabel()
        glyph.setPixmap(icons.pixmap(icon_name, 22, colour))
        glyph.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(glyph)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        head = QLabel(title)
        head.setObjectName("ToastTitle")
        head.setWordWrap(True)
        body = QLabel(message)
        body.setWordWrap(True)
        body.setProperty("muted", True)
        texts.addWidget(head)
        if message:
            texts.addWidget(body)
        layout.addLayout(texts, 1)
        close = QPushButton()
        close.setIcon(icons.icon("close", t.muted))
        close.setFlat(True)
        close.setFixedSize(24, 24)
        close.setStyleSheet("QPushButton { border: none; background: transparent; padding: 0; }")
        close.clicked.connect(self._close)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        self.setFixedWidth(380)
        if timeout_ms > 0:
            # Owned by the toast, so it dies with it: a free-standing single-shot timer would
            # fire after the user (or a newer toast) closed this one and touch a deleted widget.
            self._expiry = QTimer(self)
            self._expiry.setSingleShot(True)
            self._expiry.timeout.connect(self._close)
            self._expiry.start(timeout_ms)

    # Signals are connected to methods, never to lambdas that capture ``self``: such a lambda
    # keeps the widget alive (a reference cycle through Qt that Python cannot collect) until
    # the interpreter shuts down, and destroying widgets that late crashes PySide.
    def _close(self) -> None:
        self.closed.emit(self)


class ToastArea(QObject):
    """Stacks toasts in the bottom-right corner of a window."""

    MAX = 4

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self.host = host
        self.toasts: list[Toast] = []
        host.installEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.host and event.type() == QEvent.Type.Resize:
            self._layout()
        return False

    def show(self, level: str, title: str, message: str = "", timeout_ms: int | None = None) -> None:
        if timeout_ms is None:
            timeout_ms = {"info": 5000, "success": 7000, "warning": 9000}.get(level, 0)
        toast = Toast(self.host, level, title, message, timeout_ms)
        toast.closed.connect(self._close)
        self.toasts.insert(0, toast)
        while len(self.toasts) > self.MAX:
            self._close(self.toasts[-1])
        toast.adjustSize()
        toast.show()
        toast.raise_()
        self._layout()

    def _close(self, toast: Toast) -> None:
        if toast in self.toasts:
            self.toasts.remove(toast)
            toast.hide()
            toast.deleteLater()
            self._layout()

    def _layout(self) -> None:
        """Newest at the bottom-right, older ones stacked above it."""
        y = self.host.height() - 56
        for toast in self.toasts:
            toast.adjustSize()
            y -= toast.height()
            x = self.host.width() - toast.width() - 18
            toast.move(QPoint(x, max(8, y)))
            toast.raise_()
            y -= 8

    def clear(self) -> None:
        for toast in list(self.toasts):
            self._close(toast)


class _ChoiceButton(QPushButton):
    chosen = Signal(object)  # Choice

    def __init__(self, text: str, choice: Choice) -> None:
        super().__init__(text)
        self.choice = choice
        self.clicked.connect(self._emit_choice)

    def _emit_choice(self) -> None:
        self.chosen.emit(self.choice)


class Banner(QFrame):
    """Red/amber bar for situations that need the user (disconnects, full disk)."""

    answered = Signal(object, object)  # PendingDecision, Choice

    LABELS = {Choice.RETRY: "Retry", Choice.SKIP: "Skip this file", Choice.ABORT: "Stop"}

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Banner")
        self.pending: PendingDecision | None = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 12, 12)
        layout.setSpacing(12)
        self.glyph = QLabel()
        layout.addWidget(self.glyph, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.title = QLabel()
        font = self.title.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 1)
        self.title.setFont(font)
        self.message = QLabel()
        self.message.setWordWrap(True)
        texts.addWidget(self.title)
        texts.addWidget(self.message)
        layout.addLayout(texts, 1)
        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(8)
        layout.addLayout(self.buttons)
        self.hide()

    def ask(self, pending: PendingDecision) -> None:
        self.pending = pending
        iv = pending.intervention
        critical = iv.code in ("LB-120", "LB-121", "LB-304")
        self.setProperty("level", "critical" if critical else "warning")
        self.style().unpolish(self)
        self.style().polish(self)
        self.glyph.setPixmap(icons.pixmap("warning", 28, "#ffffff"))
        waiting = " Waiting for it…" if iv.auto_retry is not None else ""
        self.title.setText(f"{iv.title}{waiting}")
        self.message.setText(f"{iv.message}  [{iv.code}]")
        while self.buttons.count():
            item = self.buttons.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for option in iv.options:
            button = _ChoiceButton(self.LABELS.get(option, option.value.title()), option)
            button.chosen.connect(self._answer)
            self.buttons.addWidget(button)
        self.show()

    def _answer(self, choice: Choice) -> None:
        pending = self.pending
        self.hide()
        self.pending = None
        if pending is not None:
            pending.answer_with(choice)
            self.answered.emit(pending, choice)

    def resolve(self, pending: PendingDecision) -> None:
        if self.pending is pending:
            self.hide()
            self.pending = None


# ------------------------------------------------------------------- preview
class PreviewPane(QFrame):
    """Details of the selected item plus a picture / text / hex preview."""

    preview_requested = Signal(object)
    unlock_requested = Signal(object)    # a locked (encrypted) volume's node

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Panel")
        self.node: Node | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)
        self.title = QLabel("No file selected")
        self.title.setObjectName("SectionTitle")
        self.title.setWordWrap(True)
        layout.addWidget(self.title)
        self.facts = QLabel()
        self.facts.setWordWrap(True)
        self.facts.setTextFormat(Qt.TextFormat.RichText)
        self.facts.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.facts)
        self.stack = QStackedWidget()
        self.picture = QLabel()
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.picture.setMinimumHeight(160)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.text.setFont(mono)
        self.message = QLabel()
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setWordWrap(True)
        self.message.setProperty("muted", True)
        self.stack.addWidget(self.message)
        self.stack.addWidget(self.picture)
        self.stack.addWidget(self.text)
        layout.addWidget(self.stack, 1)
        self.button = QPushButton(icons.icon("eye"), "Preview")
        self.button.clicked.connect(self._preview_clicked)
        layout.addWidget(self.button)
        self.unlock = QPushButton(icons.icon("lock", "#ffffff"), "Unlock\u2026")
        self.unlock.setProperty("primary", True)
        self.unlock.setToolTip("Enter the volume's password to read its files")
        self.unlock.clicked.connect(self._unlock_clicked)
        self.unlock.setVisible(False)
        layout.addWidget(self.unlock)
        self.show_node(None)

    def _preview_clicked(self) -> None:
        if self.node is not None:
            self.preview_requested.emit(self.node)

    def _unlock_clicked(self) -> None:
        if self.node is not None:
            self.unlock_requested.emit(self.node)

    def show_node(self, node: Node | None) -> None:
        self.node = node
        self._rows: list[tuple[str, str]] = []
        locked = node.ref if node is not None and isinstance(node.ref, LockedVolume) else None
        self.unlock.setVisible(locked is not None)
        self.button.setVisible(locked is None)
        if node is None:
            self.title.setText("No file selected")
            self.facts.setText("<span>Select a file to see its details.</span>")
            self.stack.setCurrentWidget(self.message)
            self.message.setText("")
            self.button.setEnabled(False)
            return
        if locked is not None:
            self._show_locked(node, locked)
            return
        self.title.setText(node.name or "(root)")
        rows = []
        if node.children is None:
            text, colour = chance(node)
            rows.append(("Size", f"{format_size(node.size)} ({node.size:,} bytes)"))
            rows.append(("Status", status_text(node)))
            rows.append(("Recovery chance", f"<b style='color:{colour}'>{text}</b>"))
            rows.append(("Type", category(node)))
        else:
            rows.append(("Files inside", f"{node.total:,} shown, {node.sel_count:,} ticked"))
        if node.mtime:
            rows.append(("Modified", format_timestamp(node.mtime)))
        if node.ctime:
            rows.append(("Created", format_timestamp(node.ctime)))
        # Where the file lies on the drive (fragments, problems) is shown after Preview: finding it
        # can mean reading the drive, which never happens on the window's own thread.
        self._rows = rows
        self._show_rows(node)
        self.stack.setCurrentWidget(self.message)
        self.message.setText("Press Preview to look inside the file." if node.children is None else "")
        self.button.setEnabled(node.children is None and node.size > 0)

    def _show_locked(self, node: Node, locked: LockedVolume) -> None:
        t = current()
        self.title.setText(node.name or locked.name or "Encrypted volume")
        self._rows = [("Status", f"<b style='color:{t.warn}'>Encrypted, locked</b>"),
                      ("Format", "APFS (FileVault or \u201cAPFS (Encrypted)\u201d)")]
        if locked.hint:
            self._rows.append(("Password hint", html.escape(locked.hint)))
        self._show_rows(node)
        self.stack.setCurrentWidget(self.message)
        self.message.setText("The files of this Mac volume are encrypted. Unlock it with the password used on "
                             "the Mac to see and recover them.\n\nThe password stays in memory until Lifeboat "
                             "closes; nothing is written to the drive.")

    def _show_rows(self, node: Node) -> None:
        t = current()
        html_rows = "".join(
            f"<tr><td style='color:{t.muted};padding-right:10px'>{k}</td><td>{v}</td></tr>" for k, v in self._rows)
        self.facts.setText(f"<table>{html_rows}</table><div style='color:{t.muted}'>{html.escape(node.path())}</div>")

    def show_layout(self, node: Node, fragments: int, problems: list[str], unsupported: str | None) -> None:
        """Details found while reading the file for the preview."""
        if node is not self.node:
            return
        if fragments:
            self._rows.append(("Fragments", f"{fragments:,}"))
        if problems:
            self._rows.append(("Notes", "<br>".join(problems)))
        if unsupported:
            self._rows.append(("Cannot recover", unsupported))
        self._show_rows(node)

    def show_busy(self) -> None:
        self.stack.setCurrentWidget(self.message)
        self.message.setText("Reading…")

    def show_unavailable(self, reason: str) -> None:
        self.stack.setCurrentWidget(self.message)
        self.message.setText(reason)

    def show_content(self, node: Node, data: bytes, complete: bool) -> None:
        if node is not self.node:
            return
        note = "" if complete else "⚠ Parts of this file could not be read (shown as zeros).\n\n"
        image = QImage.fromData(data)
        if not image.isNull():
            pix = QPixmap.fromImage(image)
            target = self.stack.size() - QSize(8, 8)
            self.picture.setPixmap(pix.scaled(target, Qt.AspectRatioMode.KeepAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation))
            self.picture.setToolTip(note.strip() or f"{image.width()} × {image.height()} pixels")
            self.stack.setCurrentWidget(self.picture)
            return
        sample = data[:65536]
        printable = sum(1 for b in sample if 32 <= b < 127 or b in (9, 10, 13))
        if sample and printable / len(sample) > 0.9:
            text = sample.decode("utf-8", "replace")
        else:
            lines = []
            for offset in range(0, min(len(data), 4096), 16):
                chunk = data[offset:offset + 16]
                hexes = " ".join(f"{b:02x}" for b in chunk)
                ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                lines.append(f"{offset:08x}  {hexes:<47}  {ascii_}")
            text = "\n".join(lines)
        self.text.setPlainText(note + text)
        self.stack.setCurrentWidget(self.text)


class StatCard(QFrame):
    def __init__(self, label: str, value: str = "-") -> None:
        super().__init__()
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)
        self.value = QLabel(value)
        self.value.setObjectName("StatValue")
        self.label = QLabel(label)
        self.label.setProperty("muted", True)
        layout.addWidget(self.value)
        layout.addWidget(self.label)

    def set(self, value: str, colour: str | None = None) -> None:
        self.value.setText(value)
        self.value.setStyleSheet(f"color: {colour};" if colour else "")


class Sparkline(QWidget):
    """The last minutes of a value (the reading speed), as a small area chart."""

    def __init__(self, samples: int = 120) -> None:
        super().__init__()
        self.values: deque[float] = deque(maxlen=samples)
        self.setFixedHeight(26)
        self.setMinimumWidth(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def add(self, value: float) -> None:
        self.values.append(max(0.0, float(value)))
        self.update()

    def clear(self) -> None:
        self.values.clear()
        self.update()

    def paintEvent(self, _event: QEvent) -> None:
        if len(self.values) < 2:
            return
        t = current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        top = max(self.values) or 1.0
        width, height = self.width() - 2, self.height() - 3
        step = width / (len(self.values) - 1)
        start = 0.0
        line = QPainterPath()
        for index, value in enumerate(self.values):
            point = QPointF(1 + start + index * step, 1 + height - value / top * height)
            if index:
                line.lineTo(point)
            else:
                line.moveTo(point)
        area = QPainterPath(line)
        area.lineTo(QPointF(1 + width, 1 + height))
        area.lineTo(QPointF(1 + start, 1 + height))
        area.closeSubpath()
        fill = QColor(t.accent)
        fill.setAlphaF(0.18)
        painter.fillPath(area, fill)
        painter.setPen(QPen(QColor(t.accent), 1.6))
        painter.drawPath(line)


class _Metric(QWidget):
    def __init__(self, label: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.value = QLabel("-")
        self.value.setObjectName("MetricValue")
        self.label = QLabel(label)
        self.label.setObjectName("MetricLabel")
        layout.addWidget(self.value)
        layout.addWidget(self.label)

    def set(self, text: str, colour: str | None = None) -> None:
        self.value.setText(text)
        self.value.setStyleSheet(f"color:{colour};" if colour else "")


class JobPanel(QFrame):
    """The running job, big and clear: what it does, how far it is, how fast, how long to go."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("JobPanel")
        self._last_sample = 0.0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 14, 12)
        outer.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(12)
        self.glyph = QLabel()
        top.addWidget(self.glyph, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        self.title = QLabel()
        self.title.setObjectName("JobTitle")
        self.phase = QLabel()
        self.phase.setProperty("muted", True)
        texts.addWidget(self.title)
        texts.addWidget(self.phase)
        top.addLayout(texts, 1)
        self.controls = QHBoxLayout()
        self.controls.setSpacing(8)
        top.addLayout(self.controls)
        outer.addLayout(top)
        bar_row = QHBoxLayout()
        bar_row.setSpacing(12)
        self.percent = QLabel("0%")
        self.percent.setObjectName("JobPercent")
        self.percent.setMinimumWidth(96)
        self.bar = QProgressBar()
        self.bar.setObjectName("JobBar")
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1000)
        bar_row.addWidget(self.percent)
        bar_row.addWidget(self.bar, 1)
        outer.addLayout(bar_row)
        metrics = QHBoxLayout()
        metrics.setSpacing(28)
        self.speed = _Metric("Speed")
        self.graph = Sparkline()
        self.graph.setToolTip("Reading speed over the last two minutes")
        self.left = _Metric("Time left")
        self.done = _Metric("Done")
        self.items = _Metric("Files")
        self.bad = _Metric("Unreadable")
        self.graph.setFixedWidth(220)
        metrics.addWidget(self.speed)
        metrics.addWidget(self.graph)
        metrics.addStretch(1)
        for metric in (self.left, self.done, self.items, self.bad):
            metrics.addWidget(metric)
        outer.addLayout(metrics)
        self.setVisible(False)

    def add_button(self, button: QWidget) -> None:
        self.controls.addWidget(button)

    def start(self, title: str, icon_name: str) -> None:
        t = current()
        self.glyph.setPixmap(icons.pixmap(icon_name, 34, t.accent))
        self.title.setText(title)
        self.phase.setText("Starting\u2026")
        self.percent.setText("")
        self.bar.setRange(0, 0)  # busy until the first progress report
        self.graph.clear()
        self._last_sample = 0.0
        for metric in (self.speed, self.left, self.done, self.items, self.bad):
            metric.set("-")
        self.items.setVisible(False)
        self.setVisible(True)

    def update_progress(self, p: Progress, paused: bool = False) -> None:
        t = current()
        phase = p.phase
        if p.pass_count > 1 and not phase.startswith("Pass"):
            phase = f"Pass {p.pass_index} of {p.pass_count}: {phase}"
        if paused:
            phase += " (paused)"
        self.phase.setText(f"{phase} \u2014 {p.item}" if p.item else phase)
        if p.total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(p.fraction * 1000))
            self.percent.setText(f"{p.fraction * 100:.1f}%" if p.fraction < 0.999 else "100%")
            self.done.set(f"{format_size(p.done)} of {format_size(p.total)}")
        else:
            self.bar.setRange(0, 0)
            self.percent.setText("")
        self.speed.set(format_rate(p.rate) if p.rate else "-")
        now = time.monotonic()
        if p.rate and now - self._last_sample >= 1.0:
            self._last_sample = now
            self.graph.add(p.rate)
        self.left.set(format_duration(p.eta) if p.eta is not None and p.eta > 0 else "-")
        self.items.setVisible(bool(p.items_total))
        if p.items_total:
            self.items.set(f"{p.items_done:,} of {p.items_total:,}")
        self.bad.set(format_size(p.bad_bytes) if p.bad_bytes else "None",
                     t.bad if p.bad_bytes else t.ok)

    def finish(self) -> None:
        self.setVisible(False)


class CategoryChips(QWidget):
    """One-click file type filters, each with how many files of that type the scan found."""

    changed = Signal(str)

    def __init__(self, names: list[str]) -> None:
        super().__init__()
        self.names = names
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.chips: dict[str, QToolButton] = {}
        for name in names:
            chip = QToolButton()
            chip.setObjectName("Chip")
            chip.setCheckable(True)
            chip.setText(name)
            chip.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            glyph = category_icon(name)
            if glyph:
                chip.setIcon(icons.icon(glyph))
            self.group.addButton(chip)
            self.chips[name] = chip
            layout.addWidget(chip)
            chip.toggled.connect(self._toggled)
        layout.addStretch(1)
        self.chips[names[0]].setChecked(True)

    def _toggled(self, checked: bool) -> None:
        if checked:
            self.changed.emit(self.currentText())

    def currentText(self) -> str:
        for name, chip in self.chips.items():
            if chip.isChecked():
                return name
        return self.names[0]

    def setCurrentText(self, name: str) -> None:
        chip = self.chips.get(name)
        if chip is not None:
            chip.setChecked(True)

    def set_counts(self, counts: dict[str, tuple[int, int]]) -> None:
        """counts: category -> (files, bytes). Types with no files are hidden (unless selected)."""
        total_files = sum(c for c, _b in counts.values())
        total_bytes = sum(b for _c, b in counts.values())
        for name, chip in self.chips.items():
            files, size = (total_files, total_bytes) if name == self.names[0] else counts.get(name, (0, 0))
            short = "All" if name == self.names[0] else name.split(" & ")[0]
            chip.setText(f"{short}  {files:,}")
            chip.setToolTip(f"{name}: {files:,} files, {format_size(size)}")
            chip.setVisible(bool(files) or name == self.names[0] or chip.isChecked())

    def reset(self) -> None:
        self.setCurrentText(self.names[0])
        for name, chip in self.chips.items():
            chip.setText("All" if name == self.names[0] else name.split(" & ")[0])
            chip.setVisible(True)


class Donut(QWidget):
    """A ring chart with a number in the middle (the recovery summary)."""

    def __init__(self, parts: list[tuple[float, str]], centre: str, caption: str = "") -> None:
        super().__init__()
        self.parts = [(v, c) for v, c in parts if v > 0]
        self.centre = centre
        self.caption = caption
        self.setFixedSize(132, 132)

    def paintEvent(self, _event: QEvent) -> None:
        t = current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ring = QRectF(10, 10, self.width() - 20, self.height() - 20)
        painter.setPen(QPen(QColor(t.border), 14, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        painter.drawArc(ring, 0, 360 * 16)
        total = sum(v for v, _c in self.parts)
        angle = 90 * 16
        for value, colour in self.parts:
            span = -int(round(value / total * 360 * 16)) if total else 0
            painter.setPen(QPen(QColor(colour), 14, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            painter.drawArc(ring, angle, span)
            angle += span
        painter.setPen(QColor(t.text))
        font = QFont(self.font())
        font.setPointSizeF(font.pointSizeF() * 1.6)
        font.setBold(True)
        painter.setFont(font)
        centre = QRectF(ring).adjusted(0, -8 if self.caption else 0, 0, -8 if self.caption else 0)
        painter.drawText(centre, Qt.AlignmentFlag.AlignCenter, self.centre)
        if self.caption:
            font.setPointSizeF(self.font().pointSizeF() * 0.85)
            font.setBold(False)
            painter.setFont(font)
            painter.setPen(QColor(t.muted))
            painter.drawText(QRectF(ring).adjusted(0, 22, 0, 22), Qt.AlignmentFlag.AlignCenter, self.caption)


def toast_level(event: Event) -> str:
    return {Level.SUCCESS: "success", Level.WARNING: "warning", Level.ERROR: "error",
            Level.CRITICAL: "critical"}.get(event.level, "info")


def beep(kind: str) -> None:
    """Short system sound: 'error', 'warning' or 'done'."""
    try:
        import winsound

        flag = {"error": winsound.MB_ICONHAND,  # type: ignore[attr-defined]
                "warning": winsound.MB_ICONEXCLAMATION}.get(kind, winsound.MB_OK)  # type: ignore[attr-defined]
        winsound.MessageBeep(flag)  # type: ignore[attr-defined]
    except (ImportError, RuntimeError):
        QApplication.beep()


__all__ = [
    "Banner",
    "DiskMap",
    "F",
    "MapLegend",
    "PreviewPane",
    "SourceList",
    "StatCard",
    "Toast",
    "ToastArea",
    "beep",
    "device_icon_name",
    "device_subtitle",
    "toast_level",
]
