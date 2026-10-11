"""Drive check window: every storage device the computer detects, and whether it can be read."""

from __future__ import annotations

import html
import logging
import sys
import threading
from collections.abc import Callable, Mapping

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..device.check import CheckResult, report_text, run_check, system_tool
from ..device.enumerate import is_admin
from ..util import format_capacity
from . import icons
from .theme import current

log = logging.getLogger("lifeboat.ui")

Runner = Callable[..., list[CheckResult]]
_GLYPHS = {"good": "success", "warn": "warning", "bad": "error", "info": "info"}


class _Stopped(Exception):
    """The window was closed: stop checking."""


class _Signals(QObject):
    progress = Signal(int, int, str)
    done = Signal(object)        # list[CheckResult]
    failed = Signal(str)


def _device_icon(result: CheckResult) -> str:
    connection = result.connection.upper()
    if "USB" in connection:
        return "usb"
    if "NVME" in connection:
        return "ssd"
    if "SD" in connection:
        return "sd"
    return "hdd"


def _level_colour(level: str) -> str:
    t = current()
    return {"good": t.ok, "warn": t.warn, "bad": t.bad}.get(level, t.info)


class DriveCheckDialog(QDialog):
    """Lists what the computer detects and explains, for each device, what to do next."""

    def __init__(self, parent: QWidget | None, in_use: Mapping[str, int] | None = None,
                 runner: Runner = run_check) -> None:
        super().__init__(parent)
        self.setWindowTitle("Drive check")
        self.resize(1000, 620)
        self._runner = runner
        self._in_use = dict(in_use or {})
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._signals: _Signals | None = None
        self.results: list[CheckResult] = []
        self.chosen_path: str | None = None     # "Use this drive" was clicked
        self.restart_requested = False          # "Restart as administrator" was clicked
        self._build()
        self.start()

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        t = current()
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        top = QHBoxLayout()
        glyph = QLabel()
        glyph.setPixmap(icons.pixmap("pulse", 36, t.accent))
        top.addWidget(glyph, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        title = QLabel("Drive check")
        title.setObjectName("BigTitle")
        source = "what Device Manager lists" if sys.platform == "win32" else "what the system detects"
        intro = QLabel(
            f"Every storage device this computer detects ({source}), whether {system_tool()} can use it, and "
            "whether its first sector can be read. Read-only: nothing is written to any drive.")
        intro.setWordWrap(True)
        intro.setProperty("muted", True)
        texts.addWidget(title)
        texts.addWidget(intro)
        top.addLayout(texts, 1)
        layout.addLayout(top)

        self.admin_row = QWidget()
        admin = QHBoxLayout(self.admin_row)
        admin.setContentsMargins(0, 0, 0, 0)
        note = QLabel(
            "Lifeboat isn't running as administrator: it can see which drives report their size, but can't test "
            "reading them." if sys.platform != "linux" else
            "Lifeboat isn't running as root: it can see which drives report their size, but can't test reading "
            "them. Start it with sudo to test reading.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{t.warn}")
        admin.addWidget(note, 1)
        if sys.platform == "win32":
            restart = QPushButton(icons.icon("lock"), "Restart as administrator")
            restart.clicked.connect(self._restart)
            admin.addWidget(restart)
        self.admin_row.setVisible(not is_admin())
        layout.addWidget(self.admin_row)

        status = QHBoxLayout()
        self.status = QLabel("Checking…")
        self.status.setObjectName("SectionTitle")
        self.progress = QProgressBar()
        self.progress.setFixedWidth(240)
        self.progress.setTextVisible(False)
        status.addWidget(self.status, 1)
        status.addWidget(self.progress)
        layout.addLayout(status)

        split = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Device", "Connection", "Size", f"In {system_tool()}" if sys.platform != "linux" else "Usable",
             "First sector", "Result"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in ((1, 110), (2, 110), (3, 150), (4, 130), (5, 250)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            header.resizeSection(column, width)
        self.table.currentCellChanged.connect(self._row_changed)
        split.addWidget(self.table)
        self.details = QTextBrowser()
        self.details.setOpenExternalLinks(False)
        self.details.setPlaceholderText("Select a device to see what its result means and what to try.")
        split.addWidget(self.details)
        split.setSizes([330, 200])
        layout.addWidget(split, 1)

        buttons = QHBoxLayout()
        self.btn_again = QPushButton(icons.icon("refresh"), "Check again")
        self.btn_again.clicked.connect(self.start)
        self.btn_copy = QPushButton(icons.icon("report"), "Copy report")
        self.btn_copy.setToolTip("Copy every result as text, e.g. for a support request or a customer note")
        self.btn_copy.clicked.connect(self._copy)
        self.btn_use = QPushButton(icons.icon("search", "#ffffff"), "Use this drive")
        self.btn_use.setProperty("primary", True)
        self.btn_use.setToolTip("Select this drive in Sources to scan or image it")
        self.btn_use.setEnabled(False)
        self.btn_use.clicked.connect(self._use)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(self.btn_again)
        buttons.addWidget(self.btn_copy)
        buttons.addStretch(1)
        buttons.addWidget(self.btn_use)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    # ------------------------------------------------------------------ running
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = threading.Event()
        if self._signals is not None:
            self._signals.deleteLater()  # the previous run is over
        # A child of the window, so Qt deletes it with the window, in this thread.
        signals = _Signals(self)
        signals.progress.connect(self._progress)
        signals.done.connect(self._done)
        signals.failed.connect(self._failed)
        self._signals = signals
        self.btn_again.setEnabled(False)
        self.btn_copy.setEnabled(False)
        self.btn_copy.setText("Copy report")
        self.btn_use.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.status.setText("Checking…")
        # The worker gets its own references, never ``self``: a closed window must not be kept
        # alive (or touched) by a check that is still running.
        stop, runner, in_use = self._stop, self._runner, dict(self._in_use)

        def report(index: int, total: int, name: str) -> None:
            if stop.is_set():
                raise _Stopped
            signals.progress.emit(index, total, name)

        def work() -> None:
            try:
                results = runner(report, in_use)
            except _Stopped:
                return
            except Exception as exc:
                if stop.is_set():
                    return  # closed meanwhile (the window may already be gone)
                log.exception("Drive check failed")
                value: object = str(exc) or exc.__class__.__name__
                emit = signals.failed.emit
            else:
                emit, value = signals.done.emit, results
            if stop.is_set():
                return
            try:
                emit(value)
            except RuntimeError:  # the window was closed and deleted in between
                pass

        self._thread = threading.Thread(target=work, name="lifeboat-drive-check", daemon=True)
        self._thread.start()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _progress(self, index: int, total: int, name: str) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(index)
        if name:
            self.status.setText(f"Checking {name}…")

    def _failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self.btn_again.setEnabled(True)
        self.status.setText("The check couldn't finish")
        self.details.setHtml(f"<p style='color:{current().bad}'><b>{html.escape(message)}</b></p>"
                             "<p>Try again. If it keeps failing, the log file has the details.</p>")

    def _done(self, results: list[CheckResult]) -> None:
        self.results = list(results)
        self.progress.setVisible(False)
        self.btn_again.setEnabled(True)
        self.btn_copy.setEnabled(True)
        self.table.setRowCount(len(self.results))
        for row, result in enumerate(self.results):
            self._fill_row(row, result)
        attention = sum(1 for r in self.results if r.level in ("warn", "bad"))
        readable = sum(1 for r in self.results if r.verdict in ("readable", "slow"))
        if not self.results:
            self.status.setText("No storage devices found")
            self.details.setHtml("<p>The computer doesn't detect any storage device. Check the cable, the port "
                                 "and the drive's power supply, then click <b>Check again</b>.</p>")
            return
        noun = "device" if len(self.results) == 1 else "devices"
        summary = f"{len(self.results)} {noun} found · {readable} readable"
        if attention:
            summary += f" · {attention} need{'s' if attention == 1 else ''} attention"
        self.status.setText(summary)
        # Start on the first device that needs attention: it's what the user came for.
        first = next((i for i, r in enumerate(self.results) if r.level in ("bad", "warn")), 0)
        self.table.setCurrentCell(first, 0)
        self._row_changed(first, 0, -1, -1)

    def _fill_row(self, row: int, result: CheckResult) -> None:
        t = current()
        colour = _level_colour(result.level)
        name = QTableWidgetItem(icons.icon(_device_icon(result), t.text), result.name)
        name.setToolTip(result.path or result.instance_id)
        if result.kind == "disk":
            size = format_capacity(result.size) if result.size else "Not reported"
        else:
            size = "-"
        shown = QTableWidgetItem(result.system_text if result.kind == "disk" else "No")
        if not result.seen_by_system:
            shown.setForeground(QColor(t.muted if result.verdict in ("no_media", "in_use") else colour))
        verdict = QTableWidgetItem(icons.icon(_GLYPHS.get(result.level, "info"), colour), result.headline)
        verdict.setForeground(QColor(colour))
        items = [name, QTableWidgetItem(result.connection or "-"), QTableWidgetItem(size), shown,
                 QTableWidgetItem(result.read_text), verdict]
        for column, item in enumerate(items):
            self.table.setItem(row, column, item)

    # ------------------------------------------------------------------ details
    def _row_changed(self, row: int, _column: int, _prev_row: int, _prev_column: int) -> None:
        result = self.results[row] if 0 <= row < len(self.results) else None
        self.btn_use.setEnabled(result is not None and result.kind == "disk" and result.size > 0
                                and bool(result.path) and result.verdict != "not_answering")
        if result is None:
            self.details.clear()
            return
        colour = _level_colour(result.level)
        parts = [f"<h3 style='color:{colour}; margin-bottom:4px'>{html.escape(result.headline)}</h3>",
                 f"<p>{html.escape(result.advice)}</p>"]
        rows = []
        for line in result.report_lines()[1:]:
            label, _sep, value = line.strip().partition(": ")
            if label in ("Result", "What to do"):
                continue
            rows.append(f"<tr><td style='color:{current().muted}; padding-right:14px'>{html.escape(label)}</td>"
                        f"<td>{html.escape(value)}</td></tr>")
        if rows:
            parts.append(f"<table cellspacing='0' cellpadding='2'>{''.join(rows)}</table>")
        self.details.setHtml("".join(parts))

    def report(self) -> str:
        return report_text(self.results)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.report())
        self.btn_copy.setText("Copied")

    def _use(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self.results):
            self.chosen_path = self.results[row].path
            self.accept()

    def _restart(self) -> None:
        self.restart_requested = True
        self.accept()

    def done(self, code: int) -> None:
        self._stop.set()  # a check still running stops at the next device and reports nothing
        super().done(code)
