"""Dialogs: recover, image, deep scan, settings, about, summary, errors."""

from __future__ import annotations

import os

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..branding import APP_FULL_NAME, PUBLISHER, PUBLISHER_URL, TAGLINE, owner_logo
from ..device.base import DeviceInfo
from ..errors import E_DEST_SPACE
from ..fs.carving import GROUPS
from ..imaging import check_image_destination
from ..logsetup import log_dir
from ..recover import PreflightReport, RecoverySummary, Status, check_destination, find_resumable
from ..util import format_duration, format_size
from . import icons
from .theme import current
from .widgets import StatCard

ORG = "Zays"
APP = "Lifeboat"


def settings() -> QSettings:
    return QSettings(ORG, APP)


def setting_bool(key: str, default: bool) -> bool:
    return bool(setting(key, default))


def setting_int(key: str, default: int) -> int:
    value = setting(key, default)
    return value if isinstance(value, int) else default


def setting_float(key: str, default: float) -> float:
    value = setting(key, default)
    return float(value) if isinstance(value, int | float) else default


def setting_str(key: str, default: str) -> str:
    value = setting(key, default)
    return str(value) if value is not None else default


def setting(key: str, default: object) -> object:
    value = settings().value(key, default)
    if isinstance(default, bool):
        return value in (True, "true", "1", 1)
    if isinstance(default, int):
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return default
    return value


def _primary(button: QPushButton) -> QPushButton:
    button.setProperty("primary", True)
    return button


def _issue_row(kind: str, text: str) -> QWidget:
    t = current()
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 2, 0, 2)
    layout.setSpacing(8)
    glyph = QLabel()
    name, colour = {"error": ("error", t.bad), "warning": ("warning", t.warn),
                    "ok": ("success", t.ok)}.get(kind, ("info", t.info))
    glyph.setPixmap(icons.pixmap(name, 18, colour))
    glyph.setAlignment(Qt.AlignmentFlag.AlignTop)
    label = QLabel(text)
    label.setWordWrap(True)
    layout.addWidget(glyph)
    layout.addWidget(label, 1)
    return row


THOROUGHNESS = [
    ("quick", "Quick", "Copy what reads easily and never retry. Fastest; damaged parts stay empty."),
    ("standard", "Standard (recommended)", "Then go back for skipped and damaged areas, sector by sector."),
    ("maximum", "Maximum", "Standard, plus three more tries on every bad sector. Slowest."),
]


class _Thoroughness(QWidget):
    def __init__(self, value: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.group = QButtonGroup(self)
        self.buttons: dict[str, QRadioButton] = {}
        for key, title, text in THOROUGHNESS:
            radio = QRadioButton(title)
            radio.setChecked(key == value)
            note = QLabel(text)
            note.setProperty("muted", True)
            row = QHBoxLayout()
            row.setContentsMargins(26, 0, 0, 6)
            row.addWidget(note)
            self.group.addButton(radio)
            self.buttons[key] = radio
            layout.addWidget(radio)
            layout.addLayout(row)

    def value(self) -> str:
        for key, radio in self.buttons.items():
            if radio.isChecked():
                return key
        return "standard"


class RecoverDialog(QDialog):
    def __init__(self, parent: QWidget, source: DeviceInfo, count: int, size: int, hidden: int,
                 files_over_4g: int, largest: int) -> None:
        super().__init__(parent)
        self.setWindowTitle("Recover files")
        self.setMinimumWidth(640)
        self.source = source
        self.count = count
        self.total_size = size
        self.files_over_4g = files_over_4g
        self.largest = largest
        self.report: PreflightReport | None = None
        self.resume_info: tuple[dict, int] | None = None
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        head = QLabel(f"Recover {count:,} file{'s' if count != 1 else ''} ({format_size(size)})")
        head.setObjectName("BigTitle")
        layout.addWidget(head)
        if hidden:
            note = QLabel(f"Includes {hidden:,} ticked file(s) not shown by the current filter.")
            note.setProperty("muted", True)
            layout.addWidget(note)
        dest_box = QFrame()
        dest_box.setObjectName("Card")
        dest_layout = QVBoxLayout(dest_box)
        dest_layout.setContentsMargins(14, 12, 14, 12)
        title = QLabel("Save to")
        title.setObjectName("SectionTitle")
        dest_layout.addWidget(title)
        hint = QLabel("Pick a folder on a different, healthy drive. Never save to the drive you are recovering.")
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        dest_layout.addWidget(hint)
        row = QHBoxLayout()
        self.dest = QLineEdit(str(setting("last_destination", "")))
        self.dest.setPlaceholderText("Choose a destination folder\u2026")
        browse = QPushButton(icons.icon("open"), "Browse\u2026")
        browse.clicked.connect(self._browse)
        row.addWidget(self.dest, 1)
        row.addWidget(browse)
        dest_layout.addLayout(row)
        self.checks = QVBoxLayout()
        self.checks.setSpacing(2)
        dest_layout.addLayout(self.checks)
        self.resume = QCheckBox("Resume the unfinished recovery in this folder")
        self.resume.setVisible(False)
        self.resume.toggled.connect(self._validate)
        dest_layout.addWidget(self.resume)
        self.low_space = QCheckBox("Recover as much as fits (Lifeboat will ask when the drive is full)")
        self.low_space.setVisible(False)
        self.low_space.toggled.connect(self._update_buttons)
        dest_layout.addWidget(self.low_space)
        layout.addWidget(dest_box)
        opts = QFrame()
        opts.setObjectName("Card")
        opts_layout = QVBoxLayout(opts)
        opts_layout.setContentsMargins(14, 12, 14, 12)
        title = QLabel("How hard to try")
        title.setObjectName("SectionTitle")
        opts_layout.addWidget(title)
        self.thoroughness = _Thoroughness(str(setting("thoroughness", "standard")))
        opts_layout.addWidget(self.thoroughness)
        self.job_folder = QCheckBox("Put the files in a new dated folder (keeps each recovery separate)")
        self.job_folder.setChecked(bool(setting("job_folder", True)))
        self.verify = QCheckBox("Verify every file after copying (recommended)")
        self.verify.setChecked(bool(setting("verify", True)))
        self.times = QCheckBox("Keep original dates and times")
        self.times.setChecked(bool(setting("preserve_times", True)))
        self.mark = QCheckBox('Add "[DAMAGED]" to the names of partly recovered files')
        self.mark.setChecked(bool(setting("mark_damaged", False)))
        for box in (self.job_folder, self.verify, self.times, self.mark):
            opts_layout.addWidget(box)
        layout.addWidget(opts)
        buttons = QDialogButtonBox()
        self.start = _primary(QPushButton(icons.icon("recover", "#ffffff"), "Start recovery"))
        buttons.addButton(self.start, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._validate)
        self.dest.textChanged.connect(lambda: self._timer.start(400))
        self._validate()

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose where to save the recovered files",
                                                  self.dest.text() or os.path.expanduser("~"))
        if folder:
            self.dest.setText(os.path.normpath(folder))

    def _clear_checks(self) -> None:
        while self.checks.count():
            item = self.checks.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

    def _validate(self) -> None:
        self._clear_checks()
        self.report = None
        path = self.dest.text().strip()
        if not path:
            self._update_buttons()
            return
        if not os.path.isabs(path):
            self.checks.addWidget(_issue_row("error", "Enter a full folder path, for example D:\\Recovered."))
            self._update_buttons()
            return
        resumable = find_resumable(path, self.source.identity) if os.path.isdir(path) else None
        self.resume_info = resumable
        self.resume.setVisible(resumable is not None)
        if resumable is not None:
            self.resume.setText(f"Resume the unfinished recovery in this folder ({resumable[1]:,} files already "
                                "recovered will be skipped)")
            if not self.resume.isChecked():
                self.resume.blockSignals(True)
                self.resume.setChecked(True)
                self.resume.blockSignals(False)
        self.job_folder.setEnabled(not (resumable and self.resume.isChecked()))
        report = check_destination(path, self.source, self.total_size, self.largest, self.files_over_4g,
                                   allow_low_space=True)
        self.report = report
        for issue in report.issues:
            kind = "error" if issue.blocking else "warning"
            if issue.code == E_DEST_SPACE:
                kind = "error"
            self.checks.addWidget(_issue_row(kind, f"{issue.message}  [{issue.code}]"))
        if report.ok and report.free >= 0:
            fs = f" ({report.filesystem})" if report.filesystem else ""
            self.checks.addWidget(_issue_row("ok", f"{format_size(report.free)} free on the destination{fs}."))
        self.low_space.setVisible(report.space_short)
        self._update_buttons()

    def _update_buttons(self) -> None:
        report = self.report
        ok = report is not None and report.ok
        if report is not None and report.space_short and not self.low_space.isChecked():
            ok = False
        self.start.setEnabled(bool(ok))

    def _accept(self) -> None:
        s = settings()
        s.setValue("last_destination", self.dest.text().strip())
        s.setValue("thoroughness", self.thoroughness.value())
        s.setValue("job_folder", self.job_folder.isChecked())
        s.setValue("verify", self.verify.isChecked())
        s.setValue("preserve_times", self.times.isChecked())
        s.setValue("mark_damaged", self.mark.isChecked())
        self.accept()

    def values(self) -> dict:
        resume = self.resume.isVisible() and self.resume.isChecked()
        return {
            "destination": self.dest.text().strip(),
            "job_folder": self.job_folder.isChecked() and not resume,
            "verify": self.verify.isChecked(),
            "thoroughness": self.thoroughness.value(),
            "preserve_times": self.times.isChecked(),
            "mark_damaged": self.mark.isChecked(),
            "resume": resume,
        }


class ImageDialog(QDialog):
    def __init__(self, parent: QWidget, source: DeviceInfo) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create a disk image")
        self.setMinimumWidth(620)
        self.source = source
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        head = QLabel("Create a disk image")
        head.setObjectName("BigTitle")
        layout.addWidget(head)
        text = QLabel(
            "Copies the whole drive, sector by sector, to an image file on a healthy drive - the safest thing to do "
            "with a drive that is failing. The healthy areas are copied first; damaged areas are retried at the end. "
            "You can stop at any time and continue later. Afterwards, open the image in Lifeboat to recover files "
            "without touching the failing drive again.")
        text.setWordWrap(True)
        text.setProperty("muted", True)
        layout.addWidget(text)
        form = QFrame()
        form.setObjectName("Card")
        form_layout = QVBoxLayout(form)
        form_layout.setContentsMargins(14, 12, 14, 12)
        row = QHBoxLayout()
        self.path = QLineEdit()
        self.path.setPlaceholderText("Where to save the image (.img)")
        last = str(setting("last_image_folder", ""))
        if last:
            name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in source.display_name).strip() or "disk"
            self.path.setText(os.path.join(last, f"{name}.img"))
        browse = QPushButton(icons.icon("open"), "Browse\u2026")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        form_layout.addLayout(row)
        need = QLabel(f"Needs up to {format_size(source.size)} of free space. A progress file (.map) is saved next "
                      "to the image so the copy can be resumed.")
        need.setWordWrap(True)
        need.setProperty("muted", True)
        form_layout.addWidget(need)
        self.checks = QVBoxLayout()
        form_layout.addLayout(self.checks)
        layout.addWidget(form)
        self.thoroughness = _Thoroughness(str(setting("image_thoroughness", "standard")))
        layout.addWidget(self.thoroughness)
        buttons = QDialogButtonBox()
        self.start = _primary(QPushButton(icons.icon("clone", "#ffffff"), "Start imaging"))
        buttons.addButton(self.start, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.path.textChanged.connect(self._validate)
        self._validate()

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save disk image", self.path.text(),
                                              "Disk images (*.img);;All files (*)")
        if path:
            self.path.setText(os.path.normpath(path))

    def _validate(self) -> None:
        while self.checks.count():
            item = self.checks.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        path = self.path.text().strip()
        ok = bool(path) and os.path.isabs(path)
        if ok:
            problems = check_image_destination(path, self.source, self.source.size)
            for code, message in problems:
                blocking = code != "LB-303"
                self.checks.addWidget(_issue_row("error" if blocking else "warning", f"{message}  [{code}]"))
                if blocking:
                    ok = False
            if os.path.exists(path + ".map"):
                self.checks.addWidget(_issue_row("info", "A previous imaging session was found and will be resumed."))
        self.start.setEnabled(ok)

    def _accept(self) -> None:
        settings().setValue("last_image_folder", os.path.dirname(self.path.text().strip()))
        settings().setValue("image_thoroughness", self.thoroughness.value())
        self.accept()

    def values(self) -> dict:
        return {"output": self.path.text().strip(), "thoroughness": self.thoroughness.value()}


class DeepScanDialog(QDialog):
    def __init__(self, parent: QWidget, source: DeviceInfo) -> None:
        super().__init__(parent)
        self.setWindowTitle("Deep scan")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        head = QLabel("Deep scan")
        head.setObjectName("BigTitle")
        layout.addWidget(head)
        hours = source.size / (100 * 1000 * 1000) / 3600 if source.size else 0
        text = QLabel(
            "Reads the entire drive from start to end to find lost or deleted partitions and to recognise files by "
            "their content when the filesystem is too damaged. "
            + (f"On a healthy drive this takes roughly {format_duration(hours * 3600)}; a failing drive takes longer."
               if hours else ""))
        text.setWordWrap(True)
        text.setProperty("muted", True)
        layout.addWidget(text)
        self.partitions = QCheckBox("Search for lost partitions")
        self.partitions.setChecked(True)
        self.carve = QCheckBox("Find files by signature (photos, videos, documents, \u2026)")
        self.carve.setChecked(True)
        layout.addWidget(self.partitions)
        layout.addWidget(self.carve)
        grid = QGridLayout()
        grid.setContentsMargins(26, 0, 0, 0)
        self.groups: dict[str, QCheckBox] = {}
        for i, group in enumerate(GROUPS):
            box = QCheckBox(group)
            box.setChecked(True)
            self.groups[group] = box
            grid.addWidget(box, i // 3, i % 3)
        layout.addLayout(grid)
        self.carve.toggled.connect(self._carve_toggled)
        tip = QLabel("Tip: for a drive that is failing, create a disk image first and deep-scan the image.")
        tip.setWordWrap(True)
        tip.setProperty("muted", True)
        layout.addWidget(tip)
        buttons = QDialogButtonBox()
        start = _primary(QPushButton(icons.icon("radar", "#ffffff"), "Start deep scan"))
        buttons.addButton(start, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _carve_toggled(self, on: bool) -> None:
        for box in self.groups.values():
            box.setEnabled(on)

    def values(self) -> dict:
        groups = {g for g, b in self.groups.items() if b.isChecked()}
        return {"find_partitions": self.partitions.isChecked(),
                "carve": self.carve.isChecked() and bool(groups), "groups": groups}


class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.theme = QComboBox()
        self.theme.addItems(["Dark", "Light", "Match Windows"])
        self.theme.setCurrentIndex({"dark": 0, "light": 1, "system": 2}.get(str(setting("theme", "dark")), 0))
        form.addRow("Appearance", self.theme)
        self.timeout = QSpinBox()
        self.timeout.setRange(3, 120)
        self.timeout.setSuffix(" s")
        self.timeout.setValue(setting_int("read_timeout", 15))
        self.timeout.setToolTip("How long to wait for one read before treating it as failed and moving on.")
        form.addRow("Give up on a read after", self.timeout)
        self.meta = QSpinBox()
        self.meta.setRange(0, 3600)
        self.meta.setSuffix(" s")
        self.meta.setValue(setting_int("metadata_retry", 180))
        self.meta.setToolTip("Time spent re-reading unreadable parts of a file table during a scan.")
        form.addRow("Retry damaged file tables for", self.meta)
        self.system_files = QCheckBox("Show NTFS system files ($MFT, \u2026) and alternate data streams")
        self.system_files.setChecked(bool(setting("show_system", False)))
        form.addRow("", self.system_files)
        self.sounds = QCheckBox("Play a sound when a job finishes or needs attention")
        self.sounds.setChecked(bool(setting("sounds", True)))
        form.addRow("", self.sounds)
        self.tray = QCheckBox("Show Windows notifications when Lifeboat is in the background")
        self.tray.setChecked(bool(setting("tray", True)))
        form.addRow("", self.tray)
        layout.addLayout(form)
        logs = QPushButton(icons.icon("open"), "Open log folder")
        logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(log_dir())))
        layout.addWidget(logs, 0, Qt.AlignmentFlag.AlignLeft)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self) -> None:
        s = settings()
        s.setValue("theme", ["dark", "light", "system"][self.theme.currentIndex()])
        s.setValue("read_timeout", self.timeout.value())
        s.setValue("metadata_retry", self.meta.value())
        s.setValue("show_system", self.system_files.isChecked())
        s.setValue("sounds", self.sounds.isChecked())
        s.setValue("tray", self.tray.isChecked())
        self.accept()


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_FULL_NAME}")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        top = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(icons.app_pixmap(84))
        top.addWidget(logo)
        texts = QVBoxLayout()
        name = QLabel(APP_FULL_NAME)
        name.setObjectName("BigTitle")
        version = QLabel(f"Version {__version__}")
        version.setProperty("muted", True)
        tag = QLabel(TAGLINE)
        texts.addWidget(name)
        texts.addWidget(version)
        texts.addWidget(tag)
        top.addLayout(texts, 1)
        layout.addLayout(top)
        owner = owner_logo()
        by = QHBoxLayout()
        if owner is not None:
            pix = QPixmap(str(owner))
            if not pix.isNull():
                owner_label = QLabel()
                owner_label.setPixmap(pix.scaledToHeight(48, Qt.TransformationMode.SmoothTransformation))
                by.addWidget(owner_label)
        publisher = QLabel(f'by <a href="{PUBLISHER_URL}">{PUBLISHER}</a>')
        publisher.setOpenExternalLinks(True)
        by.addWidget(publisher)
        by.addStretch(1)
        layout.addLayout(by)
        credits = QLabel(
            "Lifeboat never writes to the drive it recovers from. Reads are bad-sector aware: healthy data first, "
            "damaged areas later, every problem reported.<br><br>"
            "Built with Python and Qt for Python (PySide6, LGPL v3).")
        credits.setWordWrap(True)
        credits.setProperty("muted", True)
        layout.addWidget(credits)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class SummaryDialog(QDialog):
    def __init__(self, parent: QWidget, summary: RecoverySummary) -> None:
        super().__init__(parent)
        self.summary = summary
        self.show_problems = False
        self.setWindowTitle("Recovery finished")
        self.setMinimumWidth(620)
        t = current()
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        outcome = summary.outcome
        icon_name, colour, title = {
            "success": ("success", t.ok, "All files recovered"),
            "warning": ("warning", t.warn, "Recovery finished with problems"),
            "failed": ("error", t.bad, "Recovery failed"),
        }[outcome]
        if summary.cancelled:
            title = "Recovery stopped"
        head = QHBoxLayout()
        glyph = QLabel()
        glyph.setPixmap(icons.pixmap(icon_name, 48, colour))
        head.addWidget(glyph)
        head_text = QVBoxLayout()
        big = QLabel(title)
        big.setObjectName("BigTitle")
        sub = QLabel(summary.headline())
        sub.setProperty("muted", True)
        head_text.addWidget(big)
        head_text.addWidget(sub)
        head.addLayout(head_text, 1)
        layout.addLayout(head)
        grid = QGridLayout()
        grid.setSpacing(10)
        cards = [
            ("Recovered", f"{summary.count(Status.OK):,}", t.ok),
            ("Damaged", f"{summary.count(Status.PARTIAL):,}", t.warn if summary.count(Status.PARTIAL) else None),
            ("Failed", f"{summary.count(Status.FAILED):,}", t.bad if summary.count(Status.FAILED) else None),
            ("Not processed", f"{summary.count(Status.SKIPPED) + summary.count(Status.PENDING):,}", None),
            ("Data saved", format_size(summary.recovered_bytes), None),
            ("Time", format_duration(summary.seconds), None),
        ]
        for i, (label, value, tint) in enumerate(cards):
            card = StatCard(label, value)
            if tint:
                card.set(value, tint)
            grid.addWidget(card, i // 3, i % 3)
        layout.addLayout(grid)
        if summary.error:
            err = QLabel(f"Error: {summary.error}")
            err.setWordWrap(True)
            err.setStyleSheet(f"color:{t.bad}")
            layout.addWidget(err)
        where = QLabel(f"Saved in: {summary.job_dir}")
        where.setWordWrap(True)
        where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(where)
        buttons = QHBoxLayout()
        open_folder = QPushButton(icons.icon("open"), "Open folder")
        open_folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(summary.job_dir)))
        report = QPushButton(icons.icon("report"), "Open report")
        report.setEnabled(bool(summary.report_html))
        report.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(summary.report_html)))
        problems = QPushButton(icons.icon("warning"), "Show problems")
        problems.setEnabled(outcome != "success")
        problems.clicked.connect(self._problems)
        close = _primary(QPushButton("Close"))
        close.clicked.connect(self.accept)
        for b in (open_folder, report, problems):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def _problems(self) -> None:
        self.show_problems = True
        self.accept()


class ErrorDialog(QDialog):
    def __init__(self, parent: QWidget | None, title: str, message: str, details: str = "", hint: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(560)
        t = current()
        layout = QVBoxLayout(self)
        head = QHBoxLayout()
        glyph = QLabel()
        glyph.setPixmap(icons.pixmap("error", 40, t.bad))
        head.addWidget(glyph, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        big = QLabel(title)
        big.setObjectName("SectionTitle")
        msg = QLabel(message)
        msg.setWordWrap(True)
        texts.addWidget(big)
        texts.addWidget(msg)
        if hint:
            hint_label = QLabel(hint)
            hint_label.setWordWrap(True)
            hint_label.setProperty("muted", True)
            texts.addWidget(hint_label)
        head.addLayout(texts, 1)
        layout.addLayout(head)
        if details:
            box = QPlainTextEdit(details)
            box.setReadOnly(True)
            box.setMaximumHeight(220)
            layout.addWidget(box)
        buttons = QHBoxLayout()
        copy = QPushButton("Copy details")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(f"{title}\n{message}\n{details}"))
        logs = QPushButton(icons.icon("open"), "Open log folder")
        logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(log_dir())))
        ok = _primary(QPushButton("OK"))
        ok.clicked.connect(self.accept)
        buttons.addWidget(copy)
        buttons.addWidget(logs)
        buttons.addStretch(1)
        buttons.addWidget(ok)
        layout.addLayout(buttons)
