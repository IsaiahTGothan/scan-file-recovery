"""Settings: every preference, in pages of cards, with a search box.

Each setting either works right away (after Save) or is marked "coming soon" and can't be
changed yet. Nothing is stored until Save.
"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
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

from .. import plans
from ..fs.carving import GROUPS
from ..logsetup import log_dir
from . import icons
from .dialogs import DEFAULTS, pref, settings
from .theme import ACCENTS, current
from .widgets import ScrollArea, Toggle, pill

# (key, label) choices of each list setting, in display order
CHOICES: dict[str, list[tuple[str, str]]] = {
    "theme": [("dark", "Dark"), ("light", "Light"), ("system", "Match the system")],
    "accent": [(key, name) for key, (name, *_colours) in ACCENTS.items()],
    "map_animation": [("smooth", "Smooth (recommended)"), ("light", "Light: fewer frames"), ("off", "Off")],
    "text_size": [("normal", "Normal"), ("large", "Large")],
    "density": [("comfortable", "Comfortable"), ("compact", "Compact: more rows on screen")],
    "size_units": [("system", "Match this computer"), ("binary", "1 KB = 1,024 bytes (as Windows shows them)"),
                   ("decimal", "1 KB = 1,000 bytes (as macOS shows them)")],
    "date_format": [("ymd", "2026-10-11 22:15"), ("mdy", "10/11/2026 10:15 PM"), ("dmy", "11/10/2026 22:15"),
                    ("dmy_dots", "11.10.2026 22:15")],
    "status_filter": [("all", "Existing and deleted"), ("existing", "Existing only"),
                      ("deleted", "Deleted and found only")],
    "toasts": [("all", "Every problem"), ("errors", "Errors only"), ("off", "Off (the Problems tab still lists all)")],
    "thoroughness": [("quick", "Quick"), ("standard", "Standard (recommended)"), ("maximum", "Maximum")],
    "log_detail": [("detailed", "Detailed (recommended: helps support)"), ("normal", "Main events only")],
}
_SYSTEM_NAME = "Windows" if sys.platform == "win32" else "macOS" if sys.platform == "darwin" else "the system"
SHORTCUTS = [
    ("Ctrl+R", "Scan the selected drive or image"),
    ("Ctrl+S", "Recover the ticked files"),
    ("Ctrl+O", "Open a disk image"),
    ("F5", "Look for drives again"),
    ("Ctrl+,", "Settings"),
    ("Ctrl+F", "Search the files found"),
    ("Enter", "Search (in the search box)"),
    ("Space", "Tick or untick the selected file"),
    ("Double-click", "Open a folder, preview a file, unlock an encrypted volume"),
]


def _swatch(colour: str) -> QIcon:
    """A round sample of a colour, for the accent color list."""
    pixmap = QPixmap(14, 14)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(colour))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(1, 1, 12, 12)
    painter.end()
    return QIcon(pixmap)


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("SettingHint")
    label.setWordWrap(True)
    return label


class _Section(QFrame):
    """A card of settings: a title, then one row per setting (name and help on the left, the
    control on the right)."""

    def __init__(self, title: str, hint: str = "") -> None:
        super().__init__()
        self.setObjectName("SettingsCard")
        self.rows: list[tuple[QWidget, str]] = []
        self.dividers: dict[int, QFrame] = {}         # row widget id -> the line above it
        self.column = QVBoxLayout(self)
        self.column.setContentsMargins(18, 14, 18, 14)
        self.column.setSpacing(10)
        head = QLabel(title)
        head.setObjectName("SectionTitle")
        self.column.addWidget(head)
        if hint:
            self.column.addWidget(_hint(hint))
        self.title = title

    def row(self, name: str, control: QWidget, hint: str = "", soon: bool = False, wide: bool = False,
            usable: bool = False) -> QWidget:
        """One setting. ``soon``: marked "coming soon" and disabled (unless ``usable``: it can be
        tried, and explains itself)."""
        line = None
        if self.rows:
            line = QFrame()
            line.setObjectName("Divider")
            self.column.addWidget(line)
        box = QWidget()
        if line is not None:
            self.dividers[id(box)] = line
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(16)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        head = QHBoxLayout()
        head.setSpacing(8)
        label = QLabel(name)
        label.setObjectName("SettingName")
        label.setWordWrap(True)
        head.addWidget(label, 1 if not soon else 0)
        if soon:
            head.addWidget(pill("COMING SOON", "SoonPill"))
            head.addStretch(1)
            control.setEnabled(usable)
        texts.addLayout(head)
        if hint:
            texts.addWidget(_hint(hint))
        layout.addLayout(texts, 1)
        if wide:
            control.setMinimumWidth(300)
        layout.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
        self.column.addWidget(box)
        self.rows.append((box, f"{self.title} {name} {hint}".lower()))
        return box

    def add(self, widget: QWidget, words: str = "") -> None:
        self.column.addWidget(widget)
        self.rows.append((widget, f"{self.title} {words}".lower()))

    def show_only(self, words: str, everything: bool) -> int:
        """Show the rows containing ``words`` (all of them with ``everything``); return how many."""
        visible = 0
        for widget, text in self.rows:
            match = everything or not words or words in text
            widget.setVisible(match)
            line = self.dividers.get(id(widget))
            if line is not None:
                line.setVisible(match and visible > 0)     # no line above the first row shown
            visible += match
        self.setVisible(visible > 0)
        return visible


class _Page(ScrollArea):
    def __init__(self, title: str, intro: str = "") -> None:
        inner = QWidget()
        super().__init__(inner)          # follows the rows a search shows and hides
        self.column = QVBoxLayout(inner)
        self.column.setContentsMargins(4, 0, 10, 8)
        self.column.setSpacing(14)
        head = QLabel(title)
        head.setObjectName("BigTitle")
        self.column.addWidget(head)
        if intro:
            self.column.addWidget(_hint(intro))
        self.sections: list[_Section] = []
        self.title = title

    def section(self, title: str, hint: str = "") -> _Section:
        section = _Section(title, hint)
        self.column.addWidget(section)
        self.sections.append(section)
        return section

    def finish(self) -> None:
        self.column.addStretch(1)

    def filter(self, words: str) -> int:
        """Show only the rows that contain ``words``; return how many are shown."""
        everything = bool(words) and words in self.title.lower()
        return sum(section.show_only(words, everything) for section in self.sections)


class SettingsDialog(QDialog):
    """Every preference, in pages. Nothing is stored until Save."""

    PAGES = ("Appearance", "Scanning", "Notifications", "Recovery", "Reading drives", "System",
             "Account & license", "Keyboard shortcuts")

    def __init__(self, parent: QWidget | None, page: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(940, 640)
        self.combos: dict[str, QComboBox] = {}
        self.checks: dict[str, Toggle] = {}
        self.spins: dict[str, QSpinBox | QDoubleSpinBox] = {}
        self.groups: dict[str, QCheckBox] = {}
        self.notice = ""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        body = QHBoxLayout()
        body.setSpacing(16)
        side = QVBoxLayout()
        side.setSpacing(10)
        self.search = QLineEdit()
        self.search.setObjectName("SettingsSearch")
        self.search.setPlaceholderText("Search settings")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icons.icon("search", current().muted), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self._search)
        self.nav = QListWidget()
        self.nav.setObjectName("SettingsNav")
        self.nav.setFixedWidth(220)
        side.addWidget(self.search)
        side.addWidget(self.nav, 1)
        self.pages = QStackedWidget()
        body.addLayout(side)
        body.addWidget(self.pages, 1)
        outer.addLayout(body, 1)
        self.page_list: list[_Page] = []
        for icon_name, page_widget in (("palette", self._appearance()), ("search", self._scanning()),
                                       ("bell", self._notifications()), ("recover", self._recovery()),
                                       ("hdd", self._reading()), ("sliders", self._system()),
                                       ("key", self._account()), ("keyboard", self._shortcuts())):
            item = QListWidgetItem(icons.icon(icon_name), page_widget.title)
            self.nav.addItem(item)
            self.pages.addWidget(page_widget)
            self.page_list.append(page_widget)
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(self.PAGES.index(page) if page in self.PAGES else 0)
        buttons = QHBoxLayout()
        restore = QPushButton("Restore defaults")
        icons.put(restore, "refresh")
        restore.setToolTip("Put every setting back to how Lifeboat comes (nothing is saved until you press Save)")
        restore.clicked.connect(self.restore_defaults)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        icons.put(save, "check", "white")
        save.setProperty("primary", True)
        save.setDefault(True)
        save.clicked.connect(self.save)
        buttons.addWidget(restore)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        outer.addLayout(buttons)
        self.load()

    # ------------------------------------------------------------------ controls
    def _combo(self, key: str) -> QComboBox:
        combo = QComboBox()
        for value, label in CHOICES[key]:
            if key == "accent":
                combo.addItem(_swatch(ACCENTS[value][1]), label)
            else:
                combo.addItem(label)
        combo.setMinimumWidth(240)
        self.combos[key] = combo
        return combo

    def _check(self, key: str, text: str = "") -> Toggle:
        toggle = Toggle(text)
        self.checks[key] = toggle
        return toggle

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
        spin.setMinimumWidth(120)
        spin.setMaximumWidth(170)
        self.spins[key] = spin
        return spin

    # --------------------------------------------------------------------- pages
    def _appearance(self) -> _Page:
        page = _Page("Appearance")
        look = page.section("Look")
        look.row("Theme", self._combo("theme"))
        look.row("Accent color", self._combo("accent"), "Buttons, highlights and progress bars.")
        look.row("Text size", self._combo("text_size"))
        look.row("Row spacing", self._combo("density"))
        disk = page.section("Disk map")
        disk.row("Animation", self._combo("map_animation"),
                 "Blocks fade in as they are read, and the block being read glows. It runs only while the disk "
                 "map is on screen and something changes, and stops when Lifeboat is minimized, so even a 12-hour "
                 "job costs next to nothing. It never slows the recovery: the drive is read separately.")
        numbers = page.section("Sizes and dates")
        numbers.row("File sizes", self._combo("size_units"),
                    "Windows counts 1 KB as 1,024 bytes, macOS as 1,000: pick the one the sizes you know come from.")
        numbers.row("Dates", self._combo("date_format"), "Reports always use year-month-day.")
        page.finish()
        return page

    def _scanning(self) -> _Page:
        page = _Page("Scanning", "What a scan shows, and how Deep scan starts. Deep scan's window still lets you "
                                 "change everything before it starts.")
        browse = page.section("Browsing the results")
        browse.row("Show after a scan", self._combo("status_filter"))
        browse.row("Show system files", self._check("show_system", "Show system files"),
                   "NTFS metadata ($MFT, …) and alternate data streams. Rarely needed.")
        scan = page.section("Scan")
        scan.row("Re-read damaged file tables for", self._spin("metadata_retry", 0, 3600, " s"),
                 "Time spent re-reading unreadable parts of a file table before using what was found. "
                 "0 = don't retry.")
        deep = page.section("Deep scan")
        deep.row("Search for lost partitions", self._check("deep_partitions", "Search for lost partitions"))
        deep.row("Find files by signature", self._check("deep_carve", "Find files by signature"),
                 "Photos, videos, documents and more, even when the file tables are gone.")
        kinds = QWidget()
        grid = QGridLayout(kinds)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(18)
        for i, group in enumerate(GROUPS):
            box = QCheckBox(group)
            self.groups[group] = box
            grid.addWidget(box, i // 4, i % 4)
        deep.add(kinds, "file types photos videos audio documents archives e-mail databases pictures")
        self.checks["deep_carve"].toggled.connect(self._carve_toggled)
        page.finish()
        return page

    def _notifications(self) -> _Page:
        page = _Page("Notifications", "Lifeboat always shows a red banner when it needs you, and lists every problem "
                                      "in the Problems tab and the report.")
        when = page.section("When something happens")
        when.row("Play a sound", self._check("sounds", "Play a sound"), "When a job finishes or needs attention.")
        when.row(f"{_SYSTEM_NAME} notifications", self._check("tray", "System notifications"),
                 "When Lifeboat is in the background.")
        when.row("Flash in the taskbar", self._check("flash_taskbar", "Flash in the taskbar"),
                 "When a job needs attention.")
        when.row("Pop-ups in the window", self._combo("toasts"))
        page.finish()
        return page

    def _recovery(self) -> _Page:
        page = _Page("Recovery", "How the Recover window starts. You can still change everything there.")
        how = page.section("Copying")
        how.row("How hard to try", self._combo("thoroughness"),
                "Quick never retries. Standard goes back for skipped and damaged areas at the end. Maximum also "
                "tries every bad sector three more times.")
        for key, name, hint in (
                ("job_folder", "Put the files in a new dated folder", "Keeps each recovery separate."),
                ("verify", "Verify every file after copying", "Recommended: every copy is read back and compared."),
                ("preserve_times", "Keep the original dates and times", ""),
                ("mark_damaged", "Add \u201c[DAMAGED]\u201d to partly recovered files",
                 "So they stand out in a folder.")):
            how.row(name, self._check(key, name), hint)
        where = page.section("Saving")
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        self.destination = QLineEdit()
        self.destination.setPlaceholderText("The folder used last time")
        self.destination.setMinimumWidth(260)
        browse = QPushButton("Browse…")
        icons.put(browse, "open")
        browse.clicked.connect(self._browse)
        line.addWidget(self.destination, 1)
        line.addWidget(browse)
        where.row("Save to", row, "Always a different, healthy drive.")
        done = page.section("When a recovery is done")
        done.row("Open the folder", self._check("open_when_done", "Open the folder"))
        done.row("Open the report", self._check("open_report", "Open the report"),
                 "The HTML report: every file, what happened to it, and why.")
        page.finish()
        return page

    def _reading(self) -> _Page:
        page = _Page("Reading drives", "Lifeboat reads healthy areas first and comes back for difficult ones at the "
                                       "end. These limits decide when an area counts as difficult.")
        limits = page.section("Limits")
        limits.row("Give up on one read after", self._spin("read_timeout", 3, 120, " s"),
                   "A read that takes longer is stopped and the area is retried later in smaller pieces.")
        limits.row("Skip ahead when a read takes over", self._spin("slow_seconds", 0.5, 60, " s", decimals=1),
                   "Slow areas usually mean weak spots: skipping them first gets the healthy data off sooner.")
        limits.row("Wait for a drive that stops answering", self._spin("wake_seconds", 0, 600, " s"),
                   "A drive waking up from sleep needs a few seconds. After this time it counts as disconnected, "
                   "and Lifeboat waits for you to reconnect it.")
        page.finish()
        return page

    def _system(self) -> _Page:
        page = _Page("System")
        power = page.section("Power and permissions")
        power.row("Keep the computer awake while a job runs", self._check("keep_awake", "Keep the computer awake"),
                  "Only sleep is held back: the screen can still turn off. Without this, the computer may sleep "
                  "during a long job and USB drives can drop off.")
        power.row("Offer to restart as administrator", self._check("ask_admin", "Offer administrator rights"),
                  "At start, when Lifeboat can't read drives directly (Windows and macOS).")
        logs = page.section("Logs")
        logs.row("Log detail", self._combo("log_detail"))
        open_logs = QPushButton("Open the log folder")
        icons.put(open_logs, "open")
        open_logs.clicked.connect(self._open_logs)
        logs.row("Log files", open_logs, "Send them with a support request.")
        later = page.section("Language and updates")
        language = QComboBox()
        language.addItem("English")
        later.row("Language", language, "More languages are on the way.", soon=True)
        updates = Toggle("Check for updates")
        updates.setChecked(True)
        later.row("Check for updates automatically", updates, "Lifeboat will check zays.us for new versions.",
                  soon=True)
        channel = QComboBox()
        channel.addItems(["Stable", "Beta"])
        later.row("Update channel", channel, soon=True)
        page.finish()
        return page

    def _account(self) -> _Page:
        status = plans.current()
        page = _Page("Account & license")
        plan = page.section("Your plan")
        card = QWidget()
        row = QHBoxLayout(card)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        text = QLabel(f"<b>{status.name}</b><br>{status.detail}")
        text.setWordWrap(True)
        see = QPushButton("See plans")
        icons.put(see, "star", "accent")
        see.setProperty("upgrade", True)
        see.clicked.connect(self._plans)
        row.addWidget(pill(status.badge), 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(text, 1)
        row.addWidget(see)
        plan.add(card, "plan early access upgrade pro technician trial subscribe")
        key = page.section("License key", "Bought Lifeboat? Your key arrives by e-mail after checkout.")
        entry = QWidget()
        line = QHBoxLayout(entry)
        line.setContentsMargins(0, 0, 0, 0)
        self.license = QLineEdit()
        self.license.setPlaceholderText("XXXXX-XXXXX-XXXXX-XXXXX")
        self.license.setMinimumWidth(260)
        activate = QPushButton("Activate")
        icons.put(activate, "key")
        activate.clicked.connect(self._activate)
        line.addWidget(self.license, 1)
        line.addWidget(activate)
        key.row("Enter your key", entry, soon=not plans.STORE_OPEN, usable=True)   # Activate says it's coming
        restore = QPushButton("Restore purchase")
        restore.clicked.connect(self._activate)
        key.row("Already activated on another computer?", restore, soon=not plans.STORE_OPEN, usable=True)
        self.account_notice = QLabel()
        self.account_notice.setObjectName("SettingHint")
        self.account_notice.setWordWrap(True)
        self.account_notice.setVisible(False)
        key.add(self.account_notice, "license key activate restore")
        manage = QPushButton("Manage subscription")
        key.row("Billing and invoices", manage, "Change plan, payment or cancel, on zays.us.",
                soon=not plans.STORE_OPEN)
        page.finish()
        return page

    def _shortcuts(self) -> _Page:
        page = _Page("Keyboard shortcuts")
        keys = page.section("In the main window")
        for combo, what in SHORTCUTS:
            native = combo if combo in ("Enter", "Space", "Double-click") else \
                QKeySequence(combo).toString(QKeySequence.SequenceFormat.NativeText)
            label = QLabel(native)
            label.setObjectName("Keycap")
            keys.row(what, label)
        page.finish()
        return page

    # ------------------------------------------------------------------- actions
    def _carve_toggled(self, on: bool) -> None:
        for box in self.groups.values():
            box.setEnabled(on)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Default folder for recovered files",
                                                  self.destination.text() or os.path.expanduser("~"))
        if folder:
            self.destination.setText(os.path.normpath(folder))

    def _open_logs(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir())))

    def _activate(self) -> None:
        self.notice = ("Coming soon: license keys arrive with the store at launch. Until then every feature is "
                       "unlocked, so there's nothing to activate.")
        self.account_notice.setText(self.notice)
        self.account_notice.setVisible(True)

    def _plans(self) -> None:
        from .plans_dialog import PlansDialog

        dialog = PlansDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def _search(self, text: str) -> None:
        words = text.strip().lower()
        first = -1
        for index, page in enumerate(self.page_list):
            shown = page.filter(words)
            self.nav.item(index).setHidden(shown == 0)
            if shown and first < 0:
                first = index
        current_row = self.nav.currentRow()
        if first >= 0 and (current_row < 0 or self.nav.item(current_row).isHidden()):
            self.nav.setCurrentRow(first)

    # -------------------------------------------------------------------- values
    def _show(self, values: dict[str, object]) -> None:
        for key, combo in self.combos.items():
            keys = [k for k, _label in CHOICES[key]]
            value = str(values[key])
            combo.setCurrentIndex(keys.index(value) if value in keys else keys.index(str(DEFAULTS[key])))
        for key, box in self.checks.items():
            box.setChecked(bool(values[key]))
        for key, spin in self.spins.items():
            spin.setValue(float(values[key]) if isinstance(spin, QDoubleSpinBox) else int(values[key]))  # type: ignore[arg-type]
        wanted = set(str(values["deep_groups"]).split(","))
        for group, kind in self.groups.items():
            kind.setChecked(group in wanted)
        self._carve_toggled(self.checks["deep_carve"].isChecked())

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
        out["deep_groups"] = ",".join(g for g, b in self.groups.items() if b.isChecked())
        return out

    def save(self) -> None:
        store = settings()
        for key, value in self.values().items():
            store.setValue(key, value)
        store.setValue("last_destination", self.destination.text().strip())
        self.accept()
