"""Settings: appearance, notifications, recovery defaults, reading drives, system."""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..logsetup import log_dir
from . import icons
from .dialogs import DEFAULTS, pref, settings

# (key, label) choices of each list setting, in display order
CHOICES: dict[str, list[tuple[str, str]]] = {
    "theme": [("dark", "Dark"), ("light", "Light"), ("system", "Match the system")],
    "map_animation": [("smooth", "Smooth (recommended)"), ("light", "Light: fewer frames"), ("off", "Off")],
    "text_size": [("normal", "Normal"), ("large", "Large")],
    "density": [("comfortable", "Comfortable"), ("compact", "Compact: more rows on screen")],
    "toasts": [("all", "Every problem"), ("errors", "Errors only"), ("off", "Off (the Problems tab still lists all)")],
    "thoroughness": [("quick", "Quick"), ("standard", "Standard (recommended)"), ("maximum", "Maximum")],
}
_SYSTEM_NAME = "Windows" if sys.platform == "win32" else "macOS" if sys.platform == "darwin" else "the system"


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("SettingHint")
    label.setWordWrap(True)
    return label


class _Page(QWidget):
    def __init__(self, title: str, intro: str = "") -> None:
        super().__init__()
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(18, 8, 8, 8)
        self.layout_.setSpacing(10)
        head = QLabel(title)
        head.setObjectName("SectionTitle")
        self.layout_.addWidget(head)
        if intro:
            self.layout_.addWidget(_hint(intro))
        self.form = QFormLayout()
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.form.setVerticalSpacing(10)
        self.form.setHorizontalSpacing(16)
        self.layout_.addLayout(self.form)

    def row(self, label: str, widget: QWidget, hint: str = "") -> None:
        if hint:
            box = QWidget()
            column = QVBoxLayout(box)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(3)
            column.addWidget(widget)
            column.addWidget(_hint(hint))
            widget = box
        self.form.addRow(label, widget)

    def finish(self) -> None:
        self.layout_.addStretch(1)


class SettingsDialog(QDialog):
    """Every preference, in pages. Nothing is stored until Save."""

    def __init__(self, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(820, 560)
        self.combos: dict[str, QComboBox] = {}
        self.checks: dict[str, QCheckBox] = {}
        self.spins: dict[str, QSpinBox | QDoubleSpinBox] = {}
        outer = QVBoxLayout(self)
        body = QHBoxLayout()
        self.nav = QListWidget()
        self.nav.setObjectName("SettingsNav")
        self.nav.setFixedWidth(200)
        self.pages = QStackedWidget()
        body.addWidget(self.nav)
        body.addWidget(self.pages, 1)
        outer.addLayout(body, 1)
        for icon_name, title, page in (("eye", "Appearance", self._appearance()),
                                       ("warning", "Notifications", self._notifications()),
                                       ("recover", "Recovery", self._recovery()),
                                       ("hdd", "Reading drives", self._reading()),
                                       ("settings", "System", self._system())):
            self.nav.addItem(QListWidgetItem(icons.icon(icon_name), title))
            self.pages.addWidget(page)
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)
        buttons = QHBoxLayout()
        restore = QPushButton(icons.icon("refresh"), "Restore defaults")
        restore.setToolTip("Put every setting back to how Lifeboat comes (nothing is saved until you press Save)")
        restore.clicked.connect(self.restore_defaults)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton(icons.icon("check", "#ffffff"), "Save")
        save.setProperty("primary", True)
        save.setDefault(True)
        save.clicked.connect(self.save)
        buttons.addWidget(restore)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        outer.addLayout(buttons)
        self.load()

    # ------------------------------------------------------------------ pages
    def _combo(self, key: str) -> QComboBox:
        combo = QComboBox()
        for _value, label in CHOICES[key]:
            combo.addItem(label)
        self.combos[key] = combo
        return combo

    def _check(self, key: str, text: str) -> QCheckBox:
        box = QCheckBox(text)
        self.checks[key] = box
        return box

    def _spin(self, key: str, low: float, high: float, suffix: str, decimals: int = 0) -> QSpinBox | QDoubleSpinBox:
        spin: QSpinBox | QDoubleSpinBox
        if decimals:
            spin = QDoubleSpinBox()
            spin.setDecimals(decimals)
            spin.setSingleStep(0.5)
        else:
            spin = QSpinBox()
        spin.setRange(low, high)  # type: ignore[arg-type]
        spin.setSuffix(suffix)
        spin.setMinimumWidth(110)
        spin.setMaximumWidth(170)
        self.spins[key] = spin
        return spin

    def _appearance(self) -> QWidget:
        page = _Page("Appearance")
        page.row("Theme", self._combo("theme"))
        page.row("Disk map animation", self._combo("map_animation"),
                 "Blocks fade in as they are read, and the block being read glows. It runs only while the "
                 "disk map is on screen and something changes, and stops when Lifeboat is minimized, so even a "
                 "12-hour job costs next to nothing. It never slows the recovery: the drive is read separately.")
        page.row("Text size", self._combo("text_size"))
        page.row("Row spacing", self._combo("density"))
        page.finish()
        return page

    def _notifications(self) -> QWidget:
        page = _Page("Notifications", "Lifeboat always shows a red banner when it needs you, and lists every "
                                      "problem in the Problems tab and the report.")
        page.row("", self._check("sounds", "Play a sound when a job finishes or needs attention"))
        page.row("", self._check("tray", f"Show {_SYSTEM_NAME} notifications when Lifeboat is in the background"))
        page.row("", self._check("flash_taskbar", "Flash Lifeboat in the taskbar when a job needs attention"))
        page.row("Pop-ups in the window", self._combo("toasts"))
        page.finish()
        return page

    def _recovery(self) -> QWidget:
        page = _Page("Recovery", "How the Recover window starts. You can still change everything there.")
        page.row("How hard to try", self._combo("thoroughness"),
                 "Quick never retries. Standard goes back for skipped and damaged areas at the end. Maximum "
                 "also tries every bad sector three more times.")
        for key, text in (("job_folder", "Put the files in a new dated folder"),
                          ("verify", "Verify every file after copying (recommended)"),
                          ("preserve_times", "Keep the original dates and times"),
                          ("mark_damaged", 'Add "[DAMAGED]" to the names of partly recovered files'),
                          ("open_when_done", "Open the folder when a recovery is done")):
            page.row("", self._check(key, text))
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        self.destination = QLineEdit()
        self.destination.setPlaceholderText("The folder used last time")
        browse = QPushButton(icons.icon("open"), "Browse…")
        browse.clicked.connect(self._browse)
        line.addWidget(self.destination, 1)
        line.addWidget(browse)
        page.row("Save to", row)
        page.finish()
        return page

    def _reading(self) -> QWidget:
        page = _Page("Reading drives", "Lifeboat reads healthy areas first and comes back for difficult ones at the "
                                       "end. These limits decide when an area counts as difficult.")
        page.row("Give up on one read after", self._spin("read_timeout", 3, 120, " s"),
                 "A read that takes longer is stopped and the area is retried later in smaller pieces.")
        page.row("Skip ahead when a read takes over", self._spin("slow_seconds", 0.5, 60, " s", decimals=1),
                 "Slow areas usually mean weak spots: skipping them first gets the healthy data off sooner.")
        page.row("Wait for a drive that stops answering", self._spin("wake_seconds", 0, 600, " s"),
                 "A drive waking up from sleep needs a few seconds. After this time it counts as disconnected, "
                 "and Lifeboat waits for you to reconnect it.")
        page.row("Re-read damaged file tables for", self._spin("metadata_retry", 0, 3600, " s"),
                 "During a scan: time spent re-reading unreadable parts of a file table before using what was "
                 "found. 0 = don't retry.")
        page.finish()
        return page

    def _system(self) -> QWidget:
        page = _Page("System")
        page.row("", self._check("keep_awake", "Keep the computer awake while a job runs"))
        page.layout_.addWidget(_hint("Only sleep is held back: the screen can still turn off. Without this, the "
                                     "computer may sleep during a long job and USB drives can drop off."))
        page.row("", self._check("show_system", "Show NTFS system files ($MFT, …) and alternate data streams"))
        logs = QPushButton(icons.icon("open"), "Open the log folder")
        logs.clicked.connect(self._open_logs)
        page.layout_.addWidget(logs, 0, Qt.AlignmentFlag.AlignLeft)
        page.finish()
        return page

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Default folder for recovered files",
                                                  self.destination.text() or os.path.expanduser("~"))
        if folder:
            self.destination.setText(os.path.normpath(folder))

    def _open_logs(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir())))

    # ------------------------------------------------------------------ values
    def _show(self, values: dict[str, object]) -> None:
        for key, combo in self.combos.items():
            keys = [k for k, _label in CHOICES[key]]
            value = str(values[key])
            combo.setCurrentIndex(keys.index(value) if value in keys else keys.index(str(DEFAULTS[key])))
        for key, box in self.checks.items():
            box.setChecked(bool(values[key]))
        for key, spin in self.spins.items():
            spin.setValue(float(values[key]) if isinstance(spin, QDoubleSpinBox) else int(values[key]))  # type: ignore[arg-type]

    def load(self) -> None:
        self._show({key: pref(key) for key in DEFAULTS})
        self.destination.setText(str(settings().value("last_destination", "") or ""))

    def restore_defaults(self) -> None:
        self._show(dict(DEFAULTS))

    def values(self) -> dict[str, object]:
        out: dict[str, object] = {}
        for key, combo in self.combos.items():
            out[key] = CHOICES[key][combo.currentIndex()][0]
        for key, box in self.checks.items():
            out[key] = box.isChecked()
        for key, spin in self.spins.items():
            out[key] = spin.value()
        return out

    def save(self) -> None:
        store = settings()
        for key, value in self.values().items():
            store.setValue(key, value)
        store.setValue("last_destination", self.destination.text().strip())
        self.accept()
