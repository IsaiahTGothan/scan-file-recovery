"""Custom widgets: source list, disk map, notifications, banner, preview."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QModelIndex, QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from ..device.base import DeviceInfo
from ..events import Choice, Event, Level
from ..fs.model import F, Node
from ..rescue.sectormap import PARTIAL, SectorMap, State
from ..util import format_size, format_timestamp
from . import icons
from .bridge import PendingDecision
from .models import category, chance, status_text
from .theme import current

ROLE_INFO = Qt.ItemDataRole.UserRole + 1


# --------------------------------------------------------------------- sources
def device_icon_name(info: DeviceInfo) -> str:
    if info.kind == "image":
        return "image"
    bus = info.bus.upper()
    if "USB" in bus:
        return "usb"
    if bus in ("SD", "MMC", "SD/MMC"):
        return "sd"
    if "NVME" in bus:
        return "ssd"
    return "hdd"


def device_subtitle(info: DeviceInfo) -> str:
    parts = []
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
        tint = t.accent if selected else t.text
        painter.drawPixmap(rect.left() + 10, rect.top() + 12, icons.pixmap(icon_name, 26, tint))
        text_left = rect.left() + 48
        title_font = QFont(option.font)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(t.text))
        title = info.display_name if info.kind != "disk" or info.disk_number is None else \
            f"{info.display_name}"
        title_rect = QRect(text_left, rect.top() + 7, rect.width() - 56, 20)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, title_rect.width()))
        painter.setFont(option.font)
        painter.setPen(QColor(t.muted))
        sub_rect = QRect(text_left, rect.top() + 27, rect.width() - 56, 18)
        subtitle = device_subtitle(info)
        painter.drawText(sub_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(subtitle, Qt.TextElideMode.ElideRight, sub_rect.width()))
        if info.system:
            badge = "SYSTEM DISK" if info.kind == "disk" else "SYSTEM"
            font = QFont(option.font)
            font.setPointSizeF(max(6.5, option.font.pointSizeF() - 2.5))
            font.setBold(True)
            painter.setFont(font)
            width = painter.fontMetrics().horizontalAdvance(badge) + 12
            badge_rect = QRect(rect.right() - width - 6, rect.top() + 8, width, 16)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(t.border))
            painter.drawRoundedRect(badge_rect, 8, 8)
            painter.setPen(QColor(t.muted))
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge)
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
        sections = [("Drives", [d for d in devices if d.kind == "disk"]),
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
        self.blockSignals(False)
        if reselect is not None:
            self.setCurrentItem(reselect)


# --------------------------------------------------------------------- disk map
class DiskMap(QWidget):
    """Grid of cells, one per slice of the drive, coloured by read state."""

    CELL = 9
    GAP = 2

    def __init__(self) -> None:
        super().__init__()
        self.map: SectorMap | None = None
        self.position: int | None = None
        self._cells: list[int] = []
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1000)
        self._version = -1

    def set_map(self, sector_map: SectorMap | None) -> None:
        self.map = sector_map
        self._version = -1
        self.refresh()

    def _grid(self) -> tuple[int, int]:
        step = self.CELL + self.GAP
        cols = max(1, (self.width() - 8) // step)
        rows = max(1, (self.height() - 8) // step)
        return cols, rows

    def refresh(self) -> None:
        if not self.isVisible() or self.map is None:
            return
        if self.map.version == self._version and len(self._cells) == self._grid()[0] * self._grid()[1]:
            return
        self._version = self.map.version
        cols, rows = self._grid()
        self._cells = self.map.summarize(cols * rows)
        self.update()

    def resizeEvent(self, event: QEvent) -> None:
        self._version = -1
        self.refresh()
        super().resizeEvent(event)  # type: ignore[arg-type]

    def paintEvent(self, _event: QEvent) -> None:
        t = current()
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(t.panel))
        if self.map is None or not self._cells:
            painter.setPen(QColor(t.muted))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "The disk map shows which parts of the drive were read, skipped or found bad.")
            return
        colours = {
            int(State.UNTRIED): QColor(t.map_untried), int(State.GOOD): QColor(t.map_good),
            PARTIAL: QColor(t.map_partial), int(State.SKIPPED): QColor(t.map_skipped),
            int(State.FAILED): QColor(t.map_failed), int(State.BAD): QColor(t.map_bad),
        }
        cols, _rows = self._grid()
        step = self.CELL + self.GAP
        for index, value in enumerate(self._cells):
            x = 4 + (index % cols) * step
            y = 4 + (index // cols) * step
            painter.fillRect(x, y, self.CELL, self.CELL, colours.get(value, colours[0]))


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

    def update_totals(self, sector_map: SectorMap | None) -> None:
        if sector_map is None:
            self.totals.setText("")
            return
        totals = sector_map.totals()
        self.totals.setText(
            f"OK {format_size(totals[State.GOOD])} · bad {format_size(totals[State.BAD])} · "
            f"failed {format_size(totals[State.FAILED])} · skipped {format_size(totals[State.SKIPPED])}")


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
        self.show_node(None)

    def _preview_clicked(self) -> None:
        if self.node is not None:
            self.preview_requested.emit(self.node)

    def show_node(self, node: Node | None) -> None:
        self.node = node
        if node is None:
            self.title.setText("No file selected")
            self.facts.setText("<span>Select a file to see its details.</span>")
            self.stack.setCurrentWidget(self.message)
            self.message.setText("")
            self.button.setEnabled(False)
            return
        t = current()
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
        if node.volume is not None and node.children is None:
            try:
                layout = node.volume.layout(node)
                if layout.fragments:
                    rows.append(("Fragments", f"{layout.fragments:,}"))
                if layout.problems:
                    rows.append(("Notes", "<br>".join(layout.problems)))
                if layout.unsupported:
                    rows.append(("Cannot recover", layout.unsupported))
            except Exception:  # noqa: BLE001 - details are best effort
                pass
        html_rows = "".join(
            f"<tr><td style='color:{t.muted};padding-right:10px'>{k}</td><td>{v}</td></tr>" for k, v in rows)
        self.facts.setText(f"<table>{html_rows}</table><div style='color:{t.muted}'>{node.path()}</div>")
        self.stack.setCurrentWidget(self.message)
        self.message.setText("Press Preview to look inside the file." if node.children is None else "")
        self.button.setEnabled(node.children is None and node.size > 0)

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
