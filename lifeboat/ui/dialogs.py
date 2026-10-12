"""Dialogs: recover, image, deep scan, settings, about, summary, errors."""

from __future__ import annotations

import html
import os

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import build_info
from ..branding import APP_FULL_NAME, PUBLISHER, PUBLISHER_URL, SETTINGS_ORG, TAGLINE
from ..device.base import DeviceInfo
from ..errors import E_DEST_SPACE
from ..fs.carving import GROUPS
from ..imaging import check_image_destination, inspect_existing_image
from ..logsetup import log_dir
from ..recover import PreflightReport, RecoverySummary, Status, check_destination, find_resumable
from ..util import format_duration, format_size, plural
from . import icons
from .theme import current
from .widgets import Donut, ScrollBody, StatCard, keep_text_whole

APP = os.environ.get("LIFEBOAT_SETTINGS_NAME", "Lifeboat")  # the test-suite uses its own settings

# Every preference and its default, in one place (Settings > Restore defaults uses these).
DEFAULTS: dict[str, object] = {
    "theme": "dark",                # dark / light / system
    "accent": "lifeboat",           # theme.ACCENTS
    "map_animation": "smooth",      # smooth / light / off
    "text_size": "normal",          # normal / large
    "density": "comfortable",       # comfortable / compact
    "size_units": "system",         # system / binary (1 KB = 1,024 bytes) / decimal (1,000)
    "date_format": "ymd",           # util.TIME_STYLES
    "status_filter": "all",         # files shown after a scan: all / existing / deleted
    "deep_partitions": True,        # Deep scan: look for lost partitions
    "deep_carve": True,             # Deep scan: find files by signature
    "deep_groups": ",".join(GROUPS),  # ... of these kinds
    "sounds": True,
    "tray": True,                   # system notifications while Lifeboat is in the background
    "toasts": "all",                # pop-ups in the window: all / errors / off
    "flash_taskbar": True,
    "thoroughness": "standard",
    "job_folder": True,
    "verify": True,
    "preserve_times": True,
    "mark_damaged": False,
    "open_when_done": False,
    "open_report": False,
    "read_timeout": 15,             # seconds before one read is given up
    "wake_seconds": 30,             # seconds a drive that stops answering gets to come back
    "slow_seconds": 3.0,            # reads slower than this skip ahead (the area is read last)
    "metadata_retry": 180,          # seconds spent re-reading damaged file tables during a scan
    "keep_awake": True,
    "show_system": False,
    "ask_admin": True,              # offer to restart with administrator rights at start
    "log_detail": "detailed",       # detailed / normal
}


def settings() -> QSettings:
    return QSettings(SETTINGS_ORG, APP)


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


def pref(key: str) -> object:
    """A preference, with its default from ``DEFAULTS``."""
    return setting(key, DEFAULTS[key])


def pref_float(key: str) -> float:
    value = pref(key)
    return float(value) if isinstance(value, int | float) else float(DEFAULTS[key])  # type: ignore[arg-type]


def apply_appearance(app: object) -> None:
    """Theme, accent colour, text size, row spacing, how sizes and dates are written, and how much
    is logged, from the settings."""
    import sys

    from ..logsetup import set_detail
    from ..util import set_size_units, set_time_style
    from . import theme

    theme.apply(app, str(pref("theme")), str(pref("text_size")), str(pref("density")),  # type: ignore[arg-type]
                str(pref("accent")))
    units = str(pref("size_units"))
    set_size_units(units == "decimal" or (units == "system" and sys.platform == "darwin"))
    set_time_style(str(pref("date_format")))
    set_detail(str(pref("log_detail")) != "normal")


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


FIT_RESULT = 2  # RecoverDialog's "Tick only what fits here": untick what doesn't fit, then open it again


class RecoverDialog(QDialog):
    FIT = FIT_RESULT

    def __init__(self, parent: QWidget, source: DeviceInfo, count: int, size: int, hidden: int,
                 files_over_4g: int, largest: int) -> None:
        super().__init__(parent)
        self.fit_free = -1
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
        head = QLabel(f"Recover {plural(count, 'file')} ({format_size(size)})")
        head.setObjectName("BigTitle")
        layout.addWidget(head)
        if hidden:
            note = QLabel(f"Includes {plural(hidden, 'ticked file')} not shown by the current filter.")
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
        self.space_row = QWidget()
        space = QHBoxLayout(self.space_row)
        space.setContentsMargins(0, 4, 0, 0)
        self.fit_button = QPushButton(icons.icon("check"), "Tick only what fits here")
        self.fit_button.setToolTip("Keep ticked, in disk order, only the files that fit on this drive, then come "
                                   "back here. When the recovery is done, recover the rest to another drive "
                                   "from the summary.")
        self.fit_button.clicked.connect(self._fit)
        self.low_space = QCheckBox("Or start anyway: Lifeboat stops and asks when the drive is full")
        self.low_space.toggled.connect(self._update_buttons)
        space.addWidget(self.fit_button)
        space.addWidget(self.low_space, 1)
        self.space_row.setVisible(False)
        dest_layout.addWidget(self.space_row)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(12)
        body_layout.addWidget(dest_box)
        opts = QFrame()
        opts.setObjectName("Card")
        opts_layout = QVBoxLayout(opts)
        opts_layout.setContentsMargins(14, 12, 14, 12)
        title = QLabel("How hard to try")
        title.setObjectName("SectionTitle")
        opts_layout.addWidget(title)
        self.thoroughness = _Thoroughness(str(pref("thoroughness")))
        opts_layout.addWidget(self.thoroughness)
        self.job_folder = QCheckBox("Put the files in a new dated folder (keeps each recovery separate)")
        self.job_folder.setChecked(bool(pref("job_folder")))
        self.verify = QCheckBox("Verify every file after copying (recommended)")
        self.verify.setChecked(bool(pref("verify")))
        self.times = QCheckBox("Keep original dates and times")
        self.times.setChecked(bool(pref("preserve_times")))
        self.mark = QCheckBox("Add \u201c[DAMAGED]\u201d to the names of partly recovered files")
        self.mark.setChecked(bool(pref("mark_damaged")))
        for box in (self.job_folder, self.verify, self.times, self.mark):
            opts_layout.addWidget(box)
        body_layout.addWidget(opts)
        layout.addWidget(ScrollBody(body, least=220), 1)      # scrolls on a screen too short for it
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
        self.dest.textChanged.connect(self._dest_edited)
        self._validate()
        keep_text_whole(self)

    def _dest_edited(self) -> None:
        self._timer.start(400)  # validate once typing pauses

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
            done = plural(resumable[1], "file")
            self.resume.setText(f"Resume the unfinished recovery in this folder ({done} already recovered will "
                                "be skipped)")
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
        if report.ok and report.free >= 0 and not report.space_short:
            fs = f" ({report.filesystem})" if report.filesystem else ""
            self.checks.addWidget(_issue_row("ok", f"{format_size(report.free)} free on the destination{fs}."))
        self.space_row.setVisible(report.space_short)
        self.fit_button.setEnabled(report.space_short and report.free > 0)
        self._update_buttons()

    def _update_buttons(self) -> None:
        report = self.report
        ok = report is not None and report.ok
        if report is not None and report.space_short and not self.low_space.isChecked():
            ok = False
        self.start.setEnabled(bool(ok))

    def _fit(self) -> None:
        if self.report is None or self.report.free <= 0:
            return
        self.fit_free = self.report.free
        settings().setValue("last_destination", self.dest.text().strip())
        self.done(self.FIT)

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
        # Not isVisible(): this is read after the dialog has closed, when no child is visible.
        resume = self.resume_info is not None and self.resume.isChecked()
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
            "Copies the whole drive, sector by sector, to an image file on a healthy drive: the safest thing to do "
            "with a drive that is failing. The healthy areas are copied first; damaged areas are retried at the end. "
            "You can stop at any time and continue later. Afterwards, open the image in Lifeboat to recover files "
            "without touching the failing drive again.")
        text.setWordWrap(True)
        text.setProperty("muted", True)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(12)
        body_layout.addWidget(text)
        form = QFrame()
        form.setObjectName("Card")
        form_layout = QVBoxLayout(form)
        form_layout.setContentsMargins(14, 12, 14, 12)
        row = QHBoxLayout()
        self.path = QLineEdit()
        self.path.setPlaceholderText("Where to save the image (.img)")
        last = str(setting("last_image_folder", ""))
        if last:
            label = source.display_name + (f" {source.serial[-8:]}" if source.serial else "")
            name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in label).strip() or "disk"
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
        self.same_drive = QCheckBox("It is this same drive (for example in another USB adapter): continue the image")
        self.same_drive.setVisible(False)
        self.same_drive.toggled.connect(self._validate)
        form_layout.addWidget(self.same_drive)
        self._other_drive = False
        body_layout.addWidget(form)
        self.thoroughness = _Thoroughness(str(setting("image_thoroughness", "standard")))
        body_layout.addWidget(self.thoroughness)
        layout.addWidget(ScrollBody(body, least=220), 1)      # scrolls on a screen too short for it
        buttons = QDialogButtonBox()
        self.start = _primary(QPushButton(icons.icon("clone", "#ffffff"), "Start imaging"))
        buttons.addButton(self.start, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.path.textChanged.connect(self._validate)
        self._validate()
        keep_text_whole(self)

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
        existing = inspect_existing_image(path, path + ".map", self.source, self.source.size) if ok else None
        self._other_drive = existing is not None and existing.kind == "other-drive"
        self.same_drive.setVisible(self._other_drive)
        if ok:
            problems = check_image_destination(path, self.source, self.source.size)
            for code, message in problems:
                blocking = code != "LB-303"
                self.checks.addWidget(_issue_row("error" if blocking else "warning", f"{message}  [{code}]"))
                if blocking:
                    ok = False
        if existing is not None and existing.kind != "new":
            if existing.kind == "resume":
                self.checks.addWidget(_issue_row("info", existing.message))
            elif existing.kind == "restart" or (self._other_drive and self.same_drive.isChecked()):
                self.checks.addWidget(_issue_row("warning", existing.message))
            else:
                self.checks.addWidget(_issue_row("error", f"{existing.message}  [LB-309]"))
                ok = False
        self.start.setEnabled(ok)

    def _accept(self) -> None:
        settings().setValue("last_image_folder", os.path.dirname(self.path.text().strip()))
        settings().setValue("image_thoroughness", self.thoroughness.value())
        self.accept()

    def values(self) -> dict:
        return {"output": self.path.text().strip(), "thoroughness": self.thoroughness.value(),
                "same_drive": self._other_drive and self.same_drive.isChecked()}


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
            "Reads the entire drive from start to end to find lost or deleted partitions and to recognize files by "
            "their content when the filesystem is too damaged. "
            + (f"On a healthy drive this takes roughly {format_duration(hours * 3600)}; a failing drive takes longer."
               if hours else ""))
        text.setWordWrap(True)
        text.setProperty("muted", True)
        layout.addWidget(text)
        self.partitions = QCheckBox("Search for lost partitions")
        self.partitions.setChecked(bool(pref("deep_partitions")))
        self.carve = QCheckBox("Find files by signature (photos, videos, documents, \u2026)")
        self.carve.setChecked(bool(pref("deep_carve")))
        layout.addWidget(self.partitions)
        layout.addWidget(self.carve)
        grid = QGridLayout()
        grid.setContentsMargins(26, 0, 0, 0)
        self.groups: dict[str, QCheckBox] = {}
        wanted = set(str(pref("deep_groups")).split(","))
        for i, group in enumerate(GROUPS):
            box = QCheckBox(group)
            box.setChecked(group in wanted)
            self.groups[group] = box
            grid.addWidget(box, i // 3, i % 3)
        layout.addLayout(grid)
        self.carve.toggled.connect(self._carve_toggled)
        self._carve_toggled(self.carve.isChecked())
        tip = QLabel("Tip: for a drive that is failing, create a disk image first and deep-scan the image.")
        tip.setWordWrap(True)
        tip.setProperty("muted", True)
        layout.addWidget(tip)
        buttons = QDialogButtonBox()
        start = _primary(QPushButton(icons.icon("radar", "#ffffff"), "Start deep scan"))
        buttons.addButton(start, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        keep_text_whole(self)

    def _carve_toggled(self, on: bool) -> None:
        for box in self.groups.values():
            box.setEnabled(on)

    def _accept(self) -> None:
        # The next deep scan starts with the same choices (Settings > Scanning has them too).
        store = settings()
        store.setValue("deep_partitions", self.partitions.isChecked())
        store.setValue("deep_carve", self.carve.isChecked())
        store.setValue("deep_groups", ",".join(g for g, b in self.groups.items() if b.isChecked()))
        self.accept()

    def values(self) -> dict:
        groups = {g for g, b in self.groups.items() if b.isChecked()}
        return {"find_partitions": self.partitions.isChecked(),
                "carve": self.carve.isChecked() and bool(groups), "groups": groups}


class UnlockDialog(QDialog):
    """Asks for the password of an encrypted Mac (APFS) volume."""

    def __init__(self, parent: QWidget, name: str, hint: str = "", error: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Unlock encrypted volume")
        self.setMinimumWidth(520)
        t = current()
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        head = QHBoxLayout()
        head.setSpacing(14)
        glyph = QLabel()
        glyph.setPixmap(icons.pixmap("lock", 44, t.accent))
        head.addWidget(glyph, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(4)
        big = QLabel(f"Unlock “{name}”")
        big.setObjectName("BigTitle")
        big.setWordWrap(True)
        about = QLabel("This Mac volume is encrypted (FileVault or “APFS (Encrypted)”). Enter the password "
                       "used on the Mac: the disk's password, a user's login password for FileVault, or its "
                       "recovery key.")
        about.setWordWrap(True)
        about.setProperty("muted", True)
        texts.addWidget(big)
        texts.addWidget(about)
        head.addLayout(texts, 1)
        layout.addLayout(head)
        card = QFrame()
        card.setObjectName("Card")
        form = QVBoxLayout(card)
        form.setContentsMargins(14, 12, 14, 12)
        form.setSpacing(8)
        if hint:
            hint_label = QLabel(f"Password hint: <b>{html.escape(hint)}</b>")
            hint_label.setTextFormat(Qt.TextFormat.RichText)
            hint_label.setWordWrap(True)
            form.addWidget(hint_label)
        self.field = QLineEdit()
        self.field.setEchoMode(QLineEdit.EchoMode.Password)
        self.field.setPlaceholderText("Password")
        self.field.setMinimumHeight(34)
        form.addWidget(self.field)
        self.reveal = QCheckBox("Show the password")
        self.reveal.toggled.connect(self._reveal)
        form.addWidget(self.reveal)
        self.error = QLabel(error)
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color:{t.bad}")
        self.error.setVisible(bool(error))
        form.addWidget(self.error)
        layout.addWidget(card)
        note = QLabel("The password stays in this computer's memory until Lifeboat closes. It is never saved, and "
                      "nothing is written to the drive.")
        note.setWordWrap(True)
        note.setProperty("muted", True)
        layout.addWidget(note)
        buttons = QDialogButtonBox()
        self.ok = _primary(QPushButton(icons.icon("lock", "#ffffff"), "Unlock"))
        self.ok.setEnabled(False)
        buttons.addButton(self.ok, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.field.textChanged.connect(self._edited)
        self.field.setFocus()
        keep_text_whole(self)

    def _reveal(self, on: bool) -> None:
        self.field.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)

    def _edited(self, text: str) -> None:
        self.ok.setEnabled(bool(text))

    def password(self) -> str:
        return self.field.text()


class AboutDialog(QDialog):
    PLANS = 2           # closed with "See plans"

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        from .. import plans
        from .widgets import pill

        self.setWindowTitle(f"About {APP_FULL_NAME}")
        self.setMinimumWidth(640)
        status = plans.current()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(14)
        hero = QFrame()
        hero.setObjectName("AboutHero")
        top = QHBoxLayout(hero)
        top.setContentsMargins(18, 18, 18, 18)
        top.setSpacing(16)
        logo = QLabel()
        logo.setPixmap(icons.app_pixmap(88))
        top.addWidget(logo, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(3)
        name = QLabel(APP_FULL_NAME)
        name.setObjectName("BigTitle")
        name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)  # the texts take the room
        version = QLabel(f"Version {build_info()}")
        version.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        version.setProperty("muted", True)
        tag = QLabel(TAGLINE)
        texts.addWidget(name)
        texts.addWidget(version)
        texts.addWidget(tag)
        texts.addSpacing(4)
        texts.addWidget(pill(status.badge))
        texts.addStretch(1)
        top.addLayout(texts, 1)
        layout.addWidget(hero)

        plan_card = QFrame()
        plan_card.setObjectName("Card")
        plan_row = QHBoxLayout(plan_card)
        plan_row.setContentsMargins(14, 10, 10, 10)
        plan_text = QLabel(f"<b>Plan: {status.name}</b><br>{status.detail}")
        plan_text.setWordWrap(True)
        see = QPushButton("See plans")
        icons.put(see, "star", "accent")
        see.clicked.connect(self._plans)
        plan_row.addWidget(plan_text, 1)
        plan_row.addWidget(see)
        layout.addWidget(plan_card)

        reads = QLabel("<b>Reads</b> NTFS, exFAT, FAT32/16/12, Mac HFS+ and APFS (encrypted volumes too), MBR, "
                       "GPT and Apple partition maps, disk images. <b>Finds</b> deleted files, lost partitions "
                       "and over 40 file types by their signature.")
        reads.setWordWrap(True)
        layout.addWidget(reads)
        by = QHBoxLayout()
        mark = icons.owner_pixmap(28)
        if mark is not None:
            owner_label = QLabel()
            owner_label.setPixmap(mark)
            owner_label.setToolTip(PUBLISHER)
            by.addWidget(owner_label)
            by.addSpacing(6)
        publisher = QLabel(f'Developed by <a href="{PUBLISHER_URL}">{PUBLISHER}</a>')
        publisher.setOpenExternalLinks(True)
        by.addWidget(publisher)
        by.addStretch(1)
        layout.addLayout(by)
        credits = QLabel(
            "Lifeboat never writes to the drive it recovers from. Reads are bad-sector aware: healthy data first, "
            "damaged areas later, every problem reported.<br><br>"
            "Built with Python, Qt for Python (PySide6, LGPL v3) and cryptography (Apache 2.0).")
        credits.setWordWrap(True)
        credits.setProperty("muted", True)
        layout.addWidget(credits)
        buttons = QHBoxLayout()
        notices = QPushButton("Licenses")
        icons.put(notices, "doc")
        notices.clicked.connect(self._licenses)
        logs = QPushButton("Open log folder")
        icons.put(logs, "open")
        logs.clicked.connect(self._open_logs)
        updates = QPushButton("Check for updates")
        icons.put(updates, "refresh")
        updates.setEnabled(False)
        updates.setToolTip("Coming soon: Lifeboat will check zays.us for new versions")
        close = _primary(QPushButton("Close"))
        close.clicked.connect(self.accept)
        for button in (notices, logs, updates):
            buttons.addWidget(button)
        buttons.addWidget(pill("COMING SOON", "SoonPill"))       # like every feature not built yet
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        keep_text_whole(self)

    def _plans(self) -> None:
        self.done(self.PLANS)

    def _open_logs(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(log_dir()))

    def _licenses(self) -> None:
        dialog = LicensesDialog(self)
        dialog.exec()
        dialog.deleteLater()


class WhatsNewDialog(QDialog):
    """The highlights of this version (shown once after an update, and from Help)."""

    def __init__(self, parent: QWidget | None) -> None:
        super().__init__(parent)
        from ..whatsnew import latest

        version, notes = latest()
        self.setWindowTitle("What's new")
        self.setFixedWidth(660)
        t = current()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.setSpacing(14)
        head = QHBoxLayout()
        head.setSpacing(14)
        logo = QLabel()
        logo.setPixmap(icons.app_pixmap(56))
        head.addWidget(logo)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        title = QLabel(f"What's new in Lifeboat {version}")
        title.setObjectName("BigTitle")
        sub = QLabel(TAGLINE)
        sub.setProperty("muted", True)
        texts.addWidget(title)
        texts.addWidget(sub)
        head.addLayout(texts, 1)
        layout.addLayout(head)
        body = QWidget()
        cards = QVBoxLayout(body)
        cards.setContentsMargins(0, 0, 0, 0)
        cards.setSpacing(12)
        for icon_name, name, text in notes:
            card = QFrame()
            card.setObjectName("Card")
            row = QHBoxLayout(card)
            row.setContentsMargins(14, 12, 14, 12)
            row.setSpacing(14)
            glyph = QLabel()
            glyph.setPixmap(icons.pixmap(icon_name, 28, t.accent))
            row.addWidget(glyph, 0, Qt.AlignmentFlag.AlignTop)
            words = QLabel(f"<b>{html.escape(name)}</b><br>{html.escape(text)}")
            words.setWordWrap(True)
            words.setTextFormat(Qt.TextFormat.RichText)
            row.addWidget(words, 1)
            cards.addWidget(card)
        layout.addWidget(ScrollBody(body, least=240), 1)
        close = _primary(QPushButton("Got it"))
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)
        keep_text_whole(self)


class LicensesDialog(QDialog):
    """The third-party notices (THIRD_PARTY_NOTICES.md, bundled with the app)."""

    def __init__(self, parent: QWidget | None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QTextBrowser

        from ..branding import notices_file

        self.setWindowTitle("Licenses")
        self.resize(720, 560)
        layout = QVBoxLayout(self)
        text = QTextBrowser()
        text.setOpenExternalLinks(True)
        path = notices_file()
        try:
            text.setMarkdown(path.read_text(encoding="utf-8") if path is not None else "")
        except OSError:
            path = None
        if path is None:
            text.setPlainText("The license notices were not found next to the app.")
        layout.addWidget(text, 1)
        close = _primary(QPushButton("Close"))
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)


class SummaryDialog(QDialog):
    def __init__(self, parent: QWidget, summary: RecoverySummary, rest_count: int = 0, rest_size: int = 0) -> None:
        super().__init__(parent)
        self.summary = summary
        self.show_problems = False
        self.recover_rest = False
        self.setWindowTitle("Recovery finished")
        self.setMinimumWidth(720)
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
        big.setWordWrap(True)
        sub = QLabel(summary.headline())
        sub.setProperty("muted", True)
        sub.setWordWrap(True)
        head_text.addWidget(big)
        head_text.addWidget(sub)
        head.addLayout(head_text, 1)
        done = summary.count(Status.OK)
        total = len(summary.tasks)
        head.addWidget(Donut([(done, t.ok), (summary.count(Status.PARTIAL), t.warn),
                              (summary.count(Status.FAILED), t.bad),
                              (summary.count(Status.SKIPPED) + summary.count(Status.PENDING), t.border)],
                             f"{done * 100 // total if total else 0}%", "recovered"))
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
        if rest_count:
            # The rest of a split recovery, or what this one didn't get to: one click to carry on.
            rest_card = QFrame()
            rest_card.setObjectName("Card")
            row = QHBoxLayout(rest_card)
            row.setContentsMargins(14, 10, 10, 10)
            it, them = ("it", "it") if rest_count == 1 else ("they", "them")
            text = QLabel(f"<b>{plural(rest_count, 'file')} not recovered yet</b> ({format_size(rest_size)}): not "
                          f"ticked because {it} didn't fit, or stopped, skipped or failed. Recover {them} now, for "
                          "example to another drive.")
            text.setWordWrap(True)
            more = _primary(QPushButton(icons.icon("recover", "#ffffff"), "Recover the rest\u2026"))
            more.setToolTip("Ticks exactly what is left and opens the Recover window")
            more.clicked.connect(self._recover_rest)
            row.addWidget(text, 1)
            row.addWidget(more)
            layout.addWidget(rest_card)
        buttons = QHBoxLayout()
        open_folder = QPushButton(icons.icon("open"), "Open folder")
        open_folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(summary.job_dir)))
        report = QPushButton(icons.icon("report"), "Open report")
        report.setEnabled(bool(summary.report_html))
        report.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(summary.report_html)))
        problems = QPushButton(icons.icon("warning"), "Show problems")
        problems.setEnabled(outcome != "success")
        problems.clicked.connect(self._problems)
        close = QPushButton("Close") if rest_count else _primary(QPushButton("Close"))
        close.clicked.connect(self.accept)
        for b in (open_folder, report, problems):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        keep_text_whole(self)

    def _problems(self) -> None:
        self.show_problems = True
        self.accept()

    def _recover_rest(self) -> None:
        self.recover_rest = True
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
        keep_text_whole(self)
