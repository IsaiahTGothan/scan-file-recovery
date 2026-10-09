"""The Lifeboat main window."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QModelIndex, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QColor, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QSystemTrayIcon,
    QTableView,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..branding import APP_FULL_NAME, APP_NAME, PUBLISHER, TAGLINE, owner_logo
from ..device.base import BlockDevice, DeviceInfo
from ..device.enumerate import is_admin, list_devices, open_device
from ..device.image import ImageDevice
from ..errors import LifeboatError, describe
from ..events import Event, EventBus, Level, Progress
from ..fs.content import OK, FileContentReader
from ..fs.model import Node
from ..imaging import ImagingJob, ImagingOptions, ImagingSummary
from ..recover import RecoveryJob, RecoveryOptions, RecoverySummary
from ..rescue.reader import ReadMode, ReadPolicy, RescueReader
from ..rescue.sectormap import SectorMap, State
from ..scan import Scanner, ScanOptions, ScanResult
from ..util import format_duration, format_rate, format_size
from . import icons
from .bridge import Bridge, Job, PendingDecision, connect_events
from .dialogs import (
    AboutDialog,
    DeepScanDialog,
    ErrorDialog,
    ImageDialog,
    RecoverDialog,
    SettingsDialog,
    SummaryDialog,
    setting,
)
from .models import (
    CATEGORIES,
    EventModel,
    FileListModel,
    FolderModel,
    ResultInfo,
    ResultsModel,
    Selection,
    ViewFilter,
    category,
)
from .theme import current
from .widgets import (
    Banner,
    DiskMap,
    MapLegend,
    PreviewPane,
    SourceList,
    StatCard,
    ToastArea,
    beep,
    device_icon_name,
    device_subtitle,
    toast_level,
)

log = logging.getLogger("lifeboat.ui")
FOUR_GB = (4 << 30) - 1


def _tool(text: str, icon_name: str, tip: str) -> QToolButton:
    button = QToolButton()
    button.setText(text)
    button.setIcon(icons.icon(icon_name))
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    button.setToolTip(tip)
    return button


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_FULL_NAME)
        self.setWindowIcon(icons.app_icon())
        self.resize(1360, 860)
        self.setMinimumSize(980, 640)
        self.bus = EventBus()
        self.bridge = Bridge()
        connect_events(self.bus, self.bridge)
        self.selection = Selection()
        self.selection.filter.show_system = bool(setting("show_system", False))
        self.selection.filter.show_streams = self.selection.filter.show_system
        self.devices: list[DeviceInfo] = []
        self.images: list[DeviceInfo] = []
        self.info: DeviceInfo | None = None
        self.device: BlockDevice | None = None
        self.reader: RescueReader | None = None
        self.result: ScanResult | None = None
        self.job: Job | None = None
        self.last_summary: RecoverySummary | None = None
        self.open_error: LifeboatError | None = None
        self._problem_count = 0
        self._last_toast = 0.0
        self._enum_running = False
        self._build()
        self._connect()
        self.toasts = ToastArea(self.centralWidget())
        self.tray = QSystemTrayIcon(icons.app_icon(), self)
        self.tray.setToolTip(APP_FULL_NAME)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        self._device_timer = QTimer(self)
        self._device_timer.timeout.connect(lambda: self.refresh_devices(quiet=True))
        self._device_timer.start(6000)
        self.refresh_devices()
        self._update_actions()
        if not is_admin():
            self.bus.warning("Lifeboat is not running as administrator: drives cannot be read directly. "
                             "Disk images still work.", code="LB-101")

    # ================================================================== layout
    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._header())
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(12, 10, 12, 8)
        body_layout.setSpacing(10)
        self.banner = Banner()
        body_layout.addWidget(self.banner)
        self.vsplit = QSplitter(Qt.Orientation.Vertical)
        self.hsplit = QSplitter(Qt.Orientation.Horizontal)
        self.hsplit.addWidget(self._sources_panel())
        self.pages = QStackedWidget()
        self.pages.addWidget(self._welcome_page())
        self.pages.addWidget(self._overview_page())
        self.pages.addWidget(self._browser_page())
        self.hsplit.addWidget(self.pages)
        self.hsplit.setStretchFactor(0, 0)
        self.hsplit.setStretchFactor(1, 1)
        self.hsplit.setSizes([290, 1060])
        self.vsplit.addWidget(self.hsplit)
        self.vsplit.addWidget(self._bottom_tabs())
        self.vsplit.setStretchFactor(0, 3)
        self.vsplit.setStretchFactor(1, 1)
        self.vsplit.setSizes([620, 220])
        body_layout.addWidget(self.vsplit, 1)
        root.addWidget(body, 1)
        root.addWidget(self._status_area())

    def _header(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("HeaderBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(6)
        logo = QLabel()
        logo.setPixmap(icons.app_pixmap(34))
        layout.addWidget(logo)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel(APP_NAME)
        title.setObjectName("AppTitle")
        subtitle = QLabel(TAGLINE)
        subtitle.setObjectName("AppSubtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        layout.addLayout(titles)
        layout.addSpacing(18)
        self.btn_scan = _tool("Scan", "search", "Read the partitions and file tables of the selected source (Ctrl+R)")
        self.btn_deep = _tool("Deep scan", "radar", "Read the whole drive to find lost partitions and files")
        self.btn_image = _tool("Create image", "clone", "Copy the whole drive to an image file first (safest)")
        self.btn_open = _tool("Open image", "image", "Open a disk image file (.img, .dd, .001, .vhd)")
        for b in (self.btn_scan, self.btn_deep, self.btn_image, self.btn_open):
            layout.addWidget(b)
        layout.addStretch(1)
        self.btn_recover = QPushButton(icons.icon("recover", "#ffffff"), "Recover")
        self.btn_recover.setProperty("primary", True)
        self.btn_recover.setMinimumHeight(36)
        self.btn_recover.setToolTip("Copy the ticked files to another drive (Ctrl+S)")
        layout.addWidget(self.btn_recover)
        layout.addSpacing(8)
        owner = owner_logo()
        if owner is not None:
            from PySide6.QtGui import QPixmap

            pix = QPixmap(str(owner))
            if not pix.isNull():
                brand = QLabel()
                brand.setPixmap(pix.scaledToHeight(30, Qt.TransformationMode.SmoothTransformation))
                brand.setToolTip(f"{APP_FULL_NAME} by {PUBLISHER}")
                layout.addWidget(brand)
        self.btn_settings = _tool("", "settings", "Settings")
        self.btn_help = _tool("", "help", "About Lifeboat")
        layout.addWidget(self.btn_settings)
        layout.addWidget(self.btn_help)
        return bar

    def _sources_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 12, 10, 10)
        layout.setSpacing(8)
        head = QHBoxLayout()
        title = QLabel("Sources")
        title.setObjectName("SectionTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.btn_refresh = QToolButton()
        self.btn_refresh.setIcon(icons.icon("refresh"))
        self.btn_refresh.setToolTip("Look for drives again")
        head.addWidget(self.btn_refresh)
        layout.addLayout(head)
        self.sources = SourceList()
        layout.addWidget(self.sources, 1)
        hint = QLabel("Select the drive you want to recover from.")
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        panel.setMinimumWidth(240)
        return panel

    def _welcome_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("Panel")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(36, 30, 36, 30)
        layout.setSpacing(14)
        top = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(icons.app_pixmap(72))
        top.addWidget(logo)
        texts = QVBoxLayout()
        big = QLabel(APP_FULL_NAME)
        big.setObjectName("BigTitle")
        sub = QLabel(TAGLINE + "  Read-only, bad-sector aware, and it tells you about every problem.")
        sub.setWordWrap(True)
        sub.setProperty("muted", True)
        texts.addWidget(big)
        texts.addWidget(sub)
        top.addLayout(texts, 1)
        layout.addLayout(top)
        steps = QGridLayout()
        steps.setSpacing(12)
        items = [
            ("1", "Pick the drive", "Connect the failing drive and select it on the left."),
            ("2", "Scan it", "Scan reads the file tables. Deep scan searches the whole drive when they are damaged."),
            ("3", "Tick your files", "Browse folders, search, filter by type, preview before recovering."),
            ("4", "Recover", "Save to a different, healthy drive. Every copy is verified and reported."),
        ]
        t = current()
        for i, (num, head, text) in enumerate(items):
            card = QFrame()
            card.setObjectName("Card")
            c_layout = QVBoxLayout(card)
            c_layout.setContentsMargins(14, 12, 14, 12)
            n = QLabel(num)
            n.setStyleSheet(f"color:{t.accent}; font-size:18pt; font-weight:800;")
            h = QLabel(head)
            h.setObjectName("SectionTitle")
            body = QLabel(text)
            body.setWordWrap(True)
            body.setProperty("muted", True)
            c_layout.addWidget(n)
            c_layout.addWidget(h)
            c_layout.addWidget(body)
            c_layout.addStretch(1)
            steps.addWidget(card, 0, i)
        layout.addLayout(steps)
        safety = QLabel(
            "<b>Before you start</b><ul style='margin-left:-20px'>"
            "<li>Lifeboat never writes to the drive it reads from.</li>"
            "<li>If Windows offers to <b>format</b> or <b>scan and fix</b> the drive, click <b>Cancel</b>: "
            "both can destroy what is left.</li>"
            "<li>For a drive that clicks, hangs or keeps disconnecting, use <b>Create image</b> first, then "
            "recover from the image.</li>"
            "<li>Keep File Explorer away from the failing drive while Lifeboat works.</li></ul>")
        safety.setWordWrap(True)
        safety.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(safety)
        self.welcome_admin = QLabel()
        self.welcome_admin.setWordWrap(True)
        self.welcome_admin.setStyleSheet(f"color:{t.warn}")
        layout.addWidget(self.welcome_admin)
        layout.addStretch(1)
        return page

    def _overview_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("Panel")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)
        head = QHBoxLayout()
        self.ov_icon = QLabel()
        head.addWidget(self.ov_icon)
        texts = QVBoxLayout()
        self.ov_title = QLabel()
        self.ov_title.setObjectName("BigTitle")
        self.ov_sub = QLabel()
        self.ov_sub.setProperty("muted", True)
        texts.addWidget(self.ov_title)
        texts.addWidget(self.ov_sub)
        head.addLayout(texts, 1)
        layout.addLayout(head)
        cards = QHBoxLayout()
        self.card_capacity = StatCard("Capacity")
        self.card_sectors = StatCard("Sector size (logical / physical)")
        self.card_bus = StatCard("Connection")
        self.card_health = StatCard("Health (SMART)")
        for c in (self.card_capacity, self.card_sectors, self.card_bus, self.card_health):
            cards.addWidget(c)
        layout.addLayout(cards)
        self.ov_notes = QTextBrowser()
        self.ov_notes.setOpenExternalLinks(False)
        self.ov_notes.setMinimumHeight(140)
        layout.addWidget(self.ov_notes, 1)
        actions = QHBoxLayout()
        self.ov_scan = QPushButton(icons.icon("search", "#ffffff"), "Scan this drive")
        self.ov_scan.setProperty("primary", True)
        self.ov_deep = QPushButton(icons.icon("radar"), "Deep scan")
        self.ov_image = QPushButton(icons.icon("clone"), "Create disk image")
        self.ov_admin = QPushButton(icons.icon("lock"), "Restart as administrator")
        self.ov_admin.setVisible(False)
        for b in (self.ov_scan, self.ov_deep, self.ov_image, self.ov_admin):
            b.setMinimumHeight(38)
            actions.addWidget(b)
        actions.addStretch(1)
        layout.addLayout(actions)
        return page

    def _browser_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search names (e.g. *.jpg, invoice) and press Enter")
        self.search.addAction(icons.icon("search", current().muted), QLineEdit.ActionPosition.LeadingPosition)
        self.search.setClearButtonEnabled(True)
        self.category = QComboBox()
        self.category.addItems(["All files", *CATEGORIES.keys(), "Other"])
        self.status_filter = QComboBox()
        self.status_filter.addItems(["Existing and deleted", "Existing only", "Deleted and found only"])
        self.btn_select_all = QPushButton(icons.icon("check"), "Tick all shown")
        self.btn_select_none = QPushButton("Clear ticks")
        bar.addWidget(self.search, 2)
        bar.addWidget(self.category)
        bar.addWidget(self.status_filter)
        bar.addWidget(self.btn_select_all)
        bar.addWidget(self.btn_select_none)
        layout.addLayout(bar)
        self.found_label = QLabel()
        self.found_label.setProperty("muted", True)
        layout.addWidget(self.found_label)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.folder_model = FolderModel(self.selection)
        self.tree = QTreeView()
        self.tree.setModel(self.folder_model)
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setAnimated(True)
        self.tree.setMinimumWidth(180)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        split.addWidget(self.tree)
        middle = QWidget()
        m_layout = QVBoxLayout(middle)
        m_layout.setContentsMargins(0, 0, 0, 0)
        m_layout.setSpacing(4)
        self.crumb = QLabel()
        self.crumb.setProperty("muted", True)
        m_layout.addWidget(self.crumb)
        self.list_model = FileListModel(self.selection)
        self.list = QTreeView()
        self.list.setModel(self.list_model)
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setSortingEnabled(True)
        self.list.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.list.setAlternatingRowColors(True)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header = self.list.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(60)
        for col, width in ((1, 76), (2, 116), (3, 116), (4, 104), (5, 60), (6, 90), (7, 260)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            header.resizeSection(col, width)
        self.list.setColumnHidden(3, True)
        self.list.setColumnHidden(6, True)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._header_menu)
        m_layout.addWidget(self.list, 1)
        split.addWidget(middle)
        self.preview = PreviewPane()
        self.preview.setMinimumWidth(260)
        split.addWidget(self.preview)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 6)
        split.setStretchFactor(2, 2)
        split.setSizes([225, 800, 235])
        layout.addWidget(split, 1)
        return page

    def _bottom_tabs(self) -> QWidget:
        self.tabs = QTabWidget()
        self.activity_model = EventModel(Level.INFO)
        self.activity = self._event_table(self.activity_model)
        self.tabs.addTab(self.activity, icons.icon("list"), "Activity")
        problems = QWidget()
        p_layout = QHBoxLayout(problems)
        p_layout.setContentsMargins(0, 0, 0, 0)
        self.problems_model = EventModel(Level.WARNING)
        self.problems = self._event_table(self.problems_model)
        self.problem_detail = QTextBrowser()
        self.problem_detail.setMaximumWidth(380)
        self.problem_detail.setPlaceholderText("Select a problem to see what it means and what to do.")
        p_layout.addWidget(self.problems, 3)
        p_layout.addWidget(self.problem_detail, 2)
        self.tabs.addTab(problems, icons.icon("warning"), "Problems")
        results = QWidget()
        r_layout = QVBoxLayout(results)
        r_layout.setContentsMargins(0, 4, 0, 0)
        r_bar = QHBoxLayout()
        self.results_mode = QComboBox()
        self.results_mode.addItems(["Problems only", "Recovered only", "All files"])
        self.btn_open_dest = QPushButton(icons.icon("open"), "Open folder")
        self.btn_open_report = QPushButton(icons.icon("report"), "Open report")
        self.results_label = QLabel("No recovery yet.")
        self.results_label.setProperty("muted", True)
        r_bar.addWidget(self.results_mode)
        r_bar.addWidget(self.results_label, 1)
        r_bar.addWidget(self.btn_open_dest)
        r_bar.addWidget(self.btn_open_report)
        r_layout.addLayout(r_bar)
        self.results_model = ResultsModel()
        self.results = QTableView()
        self.results.setModel(self.results_model)
        self.results.verticalHeader().setVisible(False)
        self.results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.results.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.results.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.results.horizontalHeader().resizeSection(0, 140)
        self.results.horizontalHeader().resizeSection(2, 260)
        r_layout.addWidget(self.results, 1)
        self.tabs.addTab(results, icons.icon("report"), "Results")
        disk = QWidget()
        d_layout = QVBoxLayout(disk)
        d_layout.setContentsMargins(6, 6, 6, 6)
        self.legend = MapLegend()
        self.diskmap = DiskMap()
        d_layout.addWidget(self.legend)
        d_layout.addWidget(self.diskmap, 1)
        self.tabs.addTab(disk, icons.icon("grid"), "Disk map")
        return self.tabs

    def _event_table(self, model: EventModel) -> QTableView:
        view = QTableView()
        view.setModel(model)
        view.verticalHeader().setVisible(False)
        view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        view.setWordWrap(False)
        header = view.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(0, 78)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(1, 30)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        view.verticalHeader().setDefaultSectionSize(24)
        return view

    def _status_area(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("HeaderBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 6, 14, 6)
        layout.setSpacing(10)
        self.status_text = QLabel("Ready")
        self.status_text.setMinimumWidth(260)
        self.status_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(260)
        self.progress.setVisible(False)
        self.status_detail = QLabel()
        self.status_detail.setProperty("muted", True)
        self.problem_badge = QPushButton("No problems")
        self.problem_badge.setFlat(True)
        self.problem_badge.setIcon(icons.icon("success", current().ok))
        self.btn_pause = QPushButton(icons.icon("pause"), "Pause")
        self.btn_finish = QPushButton(icons.icon("skip"), "Finish now")
        self.btn_finish.setToolTip("Skip the remaining retry passes and finish with what has been recovered")
        self.btn_stop = QPushButton(icons.icon("stop", current().bad), "Stop")
        self.btn_stop.setProperty("danger", True)
        layout.addWidget(self.status_text, 1)
        layout.addWidget(self.progress)
        layout.addWidget(self.status_detail)
        layout.addWidget(self.problem_badge)
        for b in (self.btn_pause, self.btn_finish, self.btn_stop):
            b.setVisible(False)
            layout.addWidget(b)
        return bar

    # ================================================================ signals
    def _connect(self) -> None:
        b = self.bridge
        b.event.connect(self._on_event)
        b.progress.connect(self._on_progress)
        b.intervention.connect(self._on_intervention)
        b.intervention_resolved.connect(self._on_intervention_resolved)
        b.finished.connect(self._on_finished)
        b.failed.connect(self._on_failed)
        b.cancelled.connect(self._on_cancelled)
        self.banner.answered.connect(lambda _p, _c: self._attention_done())
        self.sources.source_selected.connect(self.select_source)
        self.btn_refresh.clicked.connect(lambda: self.refresh_devices())
        self.btn_open.clicked.connect(self.open_image)
        for button in (self.btn_scan, self.ov_scan):
            button.clicked.connect(self.quick_scan)
        for button in (self.btn_deep, self.ov_deep):
            button.clicked.connect(self.deep_scan)
        for button in (self.btn_image, self.ov_image):
            button.clicked.connect(self.create_image)
        self.ov_admin.clicked.connect(self.restart_as_admin)
        self.btn_recover.clicked.connect(self.recover)
        self.btn_settings.clicked.connect(self.open_settings)
        self.btn_help.clicked.connect(lambda: AboutDialog(self).exec())
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_stop.clicked.connect(self.stop_job)
        self.btn_finish.clicked.connect(self.finish_job)
        self.problem_badge.clicked.connect(lambda: self.tabs.setCurrentIndex(1))
        self.selection.changed.connect(self._selection_changed)
        self.tree.selectionModel().currentChanged.connect(self._folder_changed)
        self.list.doubleClicked.connect(self._list_activated)
        self.list.selectionModel().currentChanged.connect(self._list_current)
        self.list.customContextMenuRequested.connect(self._list_menu)
        self.preview.preview_requested.connect(self.preview_node)
        self.search.returnPressed.connect(self._apply_filter)
        self.search.textChanged.connect(lambda text: text or self._apply_filter())
        self.category.currentIndexChanged.connect(lambda _i: self._apply_filter())
        self.status_filter.currentIndexChanged.connect(lambda _i: self._apply_filter())
        self.btn_select_all.clicked.connect(self._tick_all_shown)
        self.btn_select_none.clicked.connect(lambda: self.selection.select_all(False))
        self.problems.selectionModel().currentChanged.connect(self._problem_selected)
        self.results_mode.currentIndexChanged.connect(
            lambda i: self.results_model.set_mode(["problems", "ok", "all"][i]))
        self.btn_open_dest.clicked.connect(self._open_destination)
        self.btn_open_report.clicked.connect(self._open_report)
        self.results.doubleClicked.connect(self._result_activated)
        for key, slot in (("Ctrl+R", self.quick_scan), ("Ctrl+S", self.recover), ("Ctrl+O", self.open_image),
                          ("F5", lambda: self.refresh_devices())):
            action = QAction(self)
            action.setShortcut(QKeySequence(key))
            action.triggered.connect(slot)
            self.addAction(action)

    # ================================================================ devices
    def refresh_devices(self, quiet: bool = False) -> None:
        if self._enum_running or (quiet and (self.job is not None or not self.isActiveWindow())):
            return
        self._enum_running = True
        job = Job("devices", self.bridge, lambda _job: list_devices())
        job.start()

    def _devices_ready(self, devices: list[DeviceInfo]) -> None:
        self._enum_running = False
        keep = self.info.identity if self.info is not None else None
        fingerprint = [d.identity for d in devices]
        if fingerprint == [d.identity for d in self.devices]:
            return
        new = {d.identity for d in devices} - {d.identity for d in self.devices}
        if self.devices and new:
            for d in devices:
                if d.identity in new and d.kind == "disk":
                    self.toasts.show("info", "Drive connected", f"{d.title} ({d.capacity_text})")
        self.devices = devices
        self.sources.set_devices(devices + self.images, keep)
        admin = is_admin()
        self.welcome_admin.setText(
            "" if admin else "Lifeboat is not running as administrator, so it cannot read drives directly. "
                             "Restart it as administrator (right-click > Run as administrator), or open a disk image.")

    def select_source(self, info: DeviceInfo) -> None:
        if self.job is not None:
            self.toasts.show("warning", "A job is running", "Stop it before switching to another source.")
            if self.info is not None:
                self.sources.set_devices(self.devices + self.images, self.info.identity)
            return
        if self.info is not None and info.identity == self.info.identity and self.device is not None:
            self.pages.setCurrentIndex(2 if self.result is not None else 1)
            return
        self._close_source()
        self.info = info
        self.open_error = None
        try:
            self.device = self._open(info)
            policy = ReadPolicy(timeout=float(setting("read_timeout", 15)))
            self.reader = RescueReader(self.device, policy, events=self.bus)
            self.info = self.device.info if info.kind != "image" else info
            if info.kind == "image":
                info.size = self.device.size
        except LifeboatError as exc:
            self.device = None
            self.reader = None
            self.open_error = exc
            self.bus.error(f"Cannot open {info.title}: {exc.message}", code=exc.code)
        except Exception as exc:
            log.exception("open failed")
            self.device = None
            self.reader = None
            self.open_error = LifeboatError(str(exc))
            self.bus.error(f"Cannot open {info.title}: {exc}")
        self.diskmap.set_map(self.reader.map if self.reader else None)
        self.legend.update_totals(self.reader.map if self.reader else None)
        self._show_overview()
        self.pages.setCurrentIndex(1)
        self._update_actions()
        if self.device is not None and info.kind == "disk" and hasattr(self.device, "health"):
            device = self.device
            Job("health", self.bridge, lambda _j: device.health()).start()  # type: ignore[attr-defined]

    def _open(self, info: DeviceInfo) -> BlockDevice:
        if info.kind == "image":
            map_path = info.path + ".map"
            unreadable: list[tuple[int, int]] = []
            if os.path.exists(map_path):
                size = os.path.getsize(info.path)
                try:
                    sm = SectorMap.load(map_path, size - size % 512)
                    unreadable = sm.ranges([State.BAD, State.FAILED, State.SKIPPED, State.UNTRIED])
                    if unreadable:
                        self.bus.info(f"Loaded the imaging map: {format_size(sum(e - s for s, e in unreadable))} "
                                      "of this image could not be read from the original drive and will be "
                                      "reported as damaged.")
                except (OSError, ValueError) as exc:
                    self.bus.warning(f"Could not read the image's map file: {exc}")
            return ImageDevice(info.path, unreadable=unreadable)
        return open_device(info)

    def _close_source(self) -> None:
        self.selection.set_root(None)
        self.folder_model.set_root(None)
        self.list_model.set_folder(None)
        self.preview.show_node(None)
        self.result = None
        if self.device is not None:
            try:
                self.device.close()
            except Exception:
                log.exception("close failed")
        self.device = None
        self.reader = None

    def _show_overview(self) -> None:
        info = self.info
        if info is None:
            return
        t = current()
        self.ov_icon.setPixmap(icons.pixmap(device_icon_name(info), 48, t.accent))
        self.ov_title.setText(info.title)
        bits = [device_subtitle(info)]
        if info.serial:
            bits.append(f"serial {info.serial}")
        self.ov_sub.setText(" · ".join(b for b in bits if b))
        size = self.device.size if self.device is not None else info.size
        self.card_capacity.set(info.capacity_text if size else "-")
        if self.device is not None:
            self.card_sectors.set(f"{self.device.sector_size} / {self.device.physical_sector_size}")
        else:
            self.card_sectors.set("-")
        self.card_bus.set(info.bus or ("Image file" if info.kind == "image" else "-"))
        self.card_health.set("Checking…" if info.kind == "disk" and self.device is not None else "Not available")
        notes = []
        if self.open_error is not None:
            hint = describe(self.open_error.code).hint
            notes.append(f"<p style='color:{t.bad}'><b>Cannot open this source.</b> {self.open_error.message}</p>")
            if hint:
                notes.append(f"<p>{hint}</p>")
        if info.system:
            notes.append("<p><b>This drive holds the running Windows installation.</b> Scanning it is safe "
                         "(read-only), but recover files to a different drive.</p>")
        if info.kind == "volume":
            notes.append("<p>Reading a drive letter goes through Windows, which decrypts BitLocker volumes that "
                         "you have unlocked. For damaged drives, select the physical drive instead.</p>")
        for note in info.notes:
            notes.append(f"<p>{note}</p>")
        notes.append("<p><b>Recommended:</b> run <b>Scan</b> first. If the drive is slow, makes noises or "
                     "disconnects, stop and <b>Create a disk image</b> instead, then recover from the image.</p>")
        self.ov_notes.setHtml("".join(notes))
        self.ov_admin.setVisible(self.open_error is not None and self.open_error.code == "LB-101"
                                 and sys.platform == "win32")

    def _health_ready(self, health: object) -> None:
        t = current()
        if health is None:
            self.card_health.set("Not available")
            return
        colour = {"Good": t.ok, "Caution": t.warn, "Bad": t.bad}.get(health.status)  # type: ignore[attr-defined]
        self.card_health.set(health.status, colour)  # type: ignore[attr-defined]
        lines = "".join(f"<li>{s}</li>" for s in health.summary)  # type: ignore[attr-defined]
        self.ov_notes.append(f"<p><b>Health ({health.source}):</b></p><ul>{lines}</ul>")  # type: ignore[attr-defined]
        if health.status in ("Caution", "Bad"):  # type: ignore[attr-defined]
            self.bus.warning(f"The drive reports health problems ({health.status}). Image it before anything "  # type: ignore[attr-defined]
                             "else if the files matter.")

    def open_image(self) -> None:
        if self.job is not None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open disk image", str(setting("last_image_folder", "")),
            "Disk images (*.img *.dd *.raw *.bin *.iso *.001 *.vhd *.dsk *.ima);;All files (*)")
        if not path:
            return
        info = DeviceInfo(path=os.path.normpath(path), kind="image", size=os.path.getsize(path),
                          model="Disk image")
        self.images = [i for i in self.images if i.path != info.path] + [info]
        self.sources.set_devices(self.devices + self.images, info.identity)
        self.select_source(info)

    def restart_as_admin(self) -> None:
        if sys.platform != "win32":
            return
        import ctypes

        params = " ".join(f'"{a}"' for a in sys.argv[1:])
        if getattr(sys, "frozen", False):
            exe, args = sys.executable, params
        else:
            exe, args = sys.executable, f'-m lifeboat {params}'
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, None, 1)
        if rc > 32:
            QApplication.quit()

    # ================================================================== jobs
    def _start(self, kind: str, label: str, work) -> Job | None:
        if self.job is not None:
            self.toasts.show("warning", "Already busy", "Wait for the current job to finish or stop it.")
            return None
        job = Job(kind, self.bridge, work)
        self.job = job
        if self.reader is not None:
            self.reader.control = job.control
        self.status_text.setText(label)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.btn_pause.setVisible(kind in ("scan", "deep", "recover", "image"))
        self.btn_pause.setText("Pause")
        self.btn_stop.setVisible(kind in ("scan", "deep", "recover", "image"))
        self.btn_finish.setVisible(False)
        self._update_actions()
        job.start()
        return job

    def _job_done(self) -> None:
        if self.reader is not None:
            self.reader.control = None
        self.job = None
        self.progress.setVisible(False)
        for b in (self.btn_pause, self.btn_stop, self.btn_finish):
            b.setVisible(False)
        self.status_detail.setText("")
        self.status_text.setText("Ready")
        self.setWindowTitle(APP_FULL_NAME)
        self._update_actions()
        if self.reader is not None:
            self.legend.update_totals(self.reader.map)
            self.diskmap.refresh()

    def toggle_pause(self) -> None:
        if self.job is None:
            return
        if self.job.control.paused:
            self.job.control.resume()
            self.btn_pause.setText("Pause")
            self.btn_pause.setIcon(icons.icon("pause"))
            self.status_text.setText(self.status_text.text().replace(" (paused)", ""))
        else:
            self.job.control.pause()
            self.btn_pause.setText("Resume")
            self.btn_pause.setIcon(icons.icon("play"))
            self.status_text.setText(self.status_text.text() + " (paused)")

    def stop_job(self) -> None:
        if self.job is None:
            return
        text = {"recover": "Stop the recovery? Files already recovered are kept and listed in the report.",
                "image": "Stop imaging? Everything copied so far is kept; you can continue later.",
                }.get(self.job.kind, "Stop the current scan?")
        if QMessageBox.question(self, "Stop", text) == QMessageBox.StandardButton.Yes and self.job is not None:
            self.job.control.cancel()
            self.status_text.setText("Stopping…")

    def finish_job(self) -> None:
        if self.job is not None and self.job.engine is not None:
            self.job.engine.finish_early()
            self.status_text.setText("Finishing with what has been recovered…")
            self.btn_finish.setEnabled(False)

    def quick_scan(self) -> None:
        if self.reader is None or self.info is None:
            self.toasts.show("warning", "Select a source", "Choose a drive or image on the left first.")
            return
        reader, info = self.reader, self.info
        self._detach_tree()
        options = ScanOptions(metadata_retry_seconds=float(setting("metadata_retry", 180)))

        def work(job: Job) -> ScanResult:
            scanner = Scanner(reader, info, self.bus, job.control, job.report, job.handler, options)
            return scanner.quick_scan()

        self._start("scan", f"Scanning {info.title}…", work)

    def deep_scan(self) -> None:
        if self.reader is None or self.info is None:
            self.toasts.show("warning", "Select a source", "Choose a drive or image on the left first.")
            return
        dialog = DeepScanDialog(self, self.info)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        values = dialog.values()
        reader, info, previous = self.reader, self.info, self.result
        self._detach_tree()
        options = ScanOptions(find_partitions=values["find_partitions"], carve=values["carve"],
                              carve_groups=values["groups"],
                              metadata_retry_seconds=float(setting("metadata_retry", 180)))

        def work(job: Job) -> ScanResult:
            scanner = Scanner(reader, info, self.bus, job.control, job.report, job.handler, options)
            return scanner.deep_scan(previous)

        self._start("deep", f"Deep scan of {info.title}…", work)
        self.tabs.setCurrentIndex(3)

    def _detach_tree(self) -> None:
        self.folder_model.set_root(None)
        self.list_model.set_folder(None)
        self.preview.show_node(None)

    def _show_result(self, result: ScanResult) -> None:
        self.result = result
        self.selection.set_root(result.root)
        self.folder_model.set_root(result.root)
        self.list_model.results.clear()
        self.pages.setCurrentIndex(2)
        files, deleted, size = result.counts()
        vols = [v for v in result.volumes if v.volume is not None]
        self.found_label.setText(
            f"{files:,} files found ({deleted:,} deleted or found by signature), {format_size(size)} in "
            f"{len(vols)} volume(s).")
        if self.folder_model.rowCount() > 0:
            first = self.folder_model.index(0, 0)
            self.tree.setCurrentIndex(first)
            self.tree.expand(first)
        self._apply_filter()

    def recover(self) -> None:
        if self.reader is None or self.info is None or self.result is None:
            self.toasts.show("warning", "Nothing to recover yet", "Scan a source and tick the files you want.")
            return
        files = self.selection.selected_files()
        if not files:
            self.toasts.show("warning", "No files ticked", "Tick the files or folders you want to recover.")
            return
        size = sum(n.size for n in files)
        over = sum(1 for n in files if n.size > FOUR_GB)
        largest = max((n.size for n in files), default=0)
        dialog = RecoverDialog(self, self.info, len(files), size, self.selection.hidden_selected, over, largest)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        values = dialog.values()
        options = RecoveryOptions(**values)
        folders = self.selection.selected_folders()
        reader, info = self.reader, self.info

        def work(job: Job) -> RecoverySummary:
            engine = RecoveryJob(files, reader, info, options, self.bus, job.control, job.report, job.handler,
                                 folders)
            job.engine = engine
            return engine.run()

        if self._start("recover", f"Recovering {len(files):,} files…", work):
            self.btn_finish.setVisible(True)
            self.btn_finish.setEnabled(True)

    def create_image(self) -> None:
        if self.reader is None or self.info is None:
            self.toasts.show("warning", "Select a source", "Choose a drive on the left first.")
            return
        if self.info.kind == "image":
            self.toasts.show("info", "Already an image", "This source is already a disk image.")
            return
        dialog = ImageDialog(self, self.device.info if self.device else self.info)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        values = dialog.values()
        reader, info = self.reader, self.info

        def work(job: Job) -> ImagingSummary:
            engine = ImagingJob(reader, info, ImagingOptions(values["output"], thoroughness=values["thoroughness"]),
                                self.bus, job.control, job.report, job.handler)
            job.engine = engine
            return engine.run()

        if self._start("image", f"Imaging {info.title}…", work):
            self.btn_finish.setVisible(True)
            self.btn_finish.setEnabled(True)
            self.tabs.setCurrentIndex(3)

    def preview_node(self, node: Node) -> None:
        if self.reader is None or node.volume is None:
            return
        if self.job is not None:
            self.preview.show_unavailable("Preview is paused while a job is running.")
            return
        limit = 40 << 20 if category(node) == "Photos & pictures" else 64 << 10
        length = min(node.size, limit)
        self.preview.show_busy()
        volume = node.volume

        def work(_job: Job) -> tuple[Node, bytes, bool]:
            layout = volume.layout(node)
            chunk = FileContentReader(layout, volume.reader).read(0, length, ReadMode.FAST)
            complete = all(st == OK for _s, _e, st in chunk.states)
            return node, bytes(chunk.data), complete

        self._start("preview", f"Reading {node.name} for preview…", work)

    # ============================================================ job events
    def _on_progress(self, p: Progress) -> None:
        if self.job is None:
            return
        if p.total > 0:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(p.fraction * 1000))
            self.setWindowTitle(f"{p.fraction * 100:.0f}% — {APP_FULL_NAME}")
        else:
            self.progress.setRange(0, 0)
        phase = p.phase
        if p.pass_count > 1:
            phase = f"Pass {p.pass_index}/{p.pass_count}: {phase}" if not phase.startswith("Pass") else phase
        if self.job.control.paused:
            phase += " (paused)"
        self.status_text.setText(f"{phase} — {p.item}" if p.item else phase)
        details = []
        if p.items_total:
            details.append(f"{p.items_done:,}/{p.items_total:,} files")
        if p.total and p.done:
            details.append(f"{format_size(p.done)} of {format_size(p.total)}")
        if p.rate:
            details.append(format_rate(p.rate))
        if p.eta is not None and p.eta > 0:
            details.append(f"{format_duration(p.eta)} left")
        if p.bad_bytes:
            details.append(f"{format_size(p.bad_bytes)} unreadable")
        self.status_detail.setText(" · ".join(details))
        if self.reader is not None and self.tabs.currentIndex() == 3:
            self.legend.update_totals(self.reader.map)

    def _on_finished(self, kind: str, result: object) -> None:
        if kind == "devices":
            self._devices_ready(result)  # type: ignore[arg-type]
            return
        if kind == "health":
            self._health_ready(result)
            return
        self._job_done()
        if kind in ("scan", "deep"):
            assert isinstance(result, ScanResult)
            self._show_result(result)
            files, deleted, _size = result.counts()
            self._notify("success", "Scan finished", f"{files:,} files found, {deleted:,} of them deleted.")
        elif kind == "recover":
            assert isinstance(result, RecoverySummary)
            self._recovery_done(result)
        elif kind == "image":
            assert isinstance(result, ImagingSummary)
            self._imaging_done(result)
        elif kind == "preview":
            node, data, complete = result  # type: ignore[misc]
            self.preview.show_content(node, data, complete)

    def _on_failed(self, kind: str, message: str, details: str) -> None:
        if kind == "devices":
            self._enum_running = False
            return
        if kind == "health":
            self.card_health.set("Not available")
            return
        self._job_done()
        if kind == "preview":
            self.preview.show_unavailable(f"Could not read this file: {message}")
            return
        if kind in ("scan", "deep") and self.result is not None:
            self._show_result(self.result)
        self.bus.critical(f"The {kind} stopped because of an unexpected error: {message}", code="LB-500")
        self._notify("critical", "Something went wrong", message)
        ErrorDialog(self, "Unexpected error", message, details, describe("LB-500").hint).exec()

    def _on_cancelled(self, kind: str) -> None:
        self._job_done()
        if kind in ("scan", "deep"):
            if self.result is not None:
                self._show_result(self.result)
            else:
                self.pages.setCurrentIndex(1)
        self.toasts.show("info", "Stopped", "The job was stopped.")

    def _recovery_done(self, summary: RecoverySummary) -> None:
        self.last_summary = summary
        self.results_model.set_tasks(summary.tasks)
        self.results_label.setText(f"{summary.headline()} — saved in {summary.job_dir}")
        self.list_model.results = {id(t.node): ResultInfo(t.status, t.message) for t in summary.tasks}
        self.list.setColumnHidden(6, False)
        self.list.viewport().update()
        self.tabs.setCurrentIndex(2)
        level = {"success": "success", "warning": "warning", "failed": "critical"}[summary.outcome]
        title = {"success": "Recovery complete", "warning": "Recovery finished with problems",
                 "failed": "Recovery failed"}[summary.outcome]
        self._notify(level, title, summary.headline())
        dialog = SummaryDialog(self, summary)
        dialog.exec()
        if dialog.show_problems:
            self.results_mode.setCurrentIndex(0)
            self.tabs.setCurrentIndex(2)

    def _imaging_done(self, summary: ImagingSummary) -> None:
        level = "success" if summary.outcome == "success" else "warning"
        text = (f"{summary.percent:.2f}% of the drive copied ({format_size(summary.good)}); "
                f"{format_size(summary.bad)} unreadable.")
        self._notify(level, "Imaging finished" if not summary.cancelled else "Imaging stopped", text)
        if summary.error:
            ErrorDialog(self, "Imaging failed", summary.error).exec()
            return
        box = QMessageBox(self)
        box.setWindowTitle("Disk image")
        box.setIcon(QMessageBox.Icon.Information if level == "success" else QMessageBox.Icon.Warning)
        box.setText(f"<b>{text}</b><br><br>Image: {summary.output}<br>Map: {summary.mapfile}")
        box.setInformativeText("Open the image now to scan and recover files from it, without touching the "
                               "failing drive again?")
        open_btn = box.addButton("Open the image", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            info = DeviceInfo(path=summary.output, kind="image", size=summary.size, model="Disk image")
            self.images = [i for i in self.images if i.path != info.path] + [info]
            self.sources.set_devices(self.devices + self.images, info.identity)
            self.select_source(info)

    # ============================================================ notifications
    def _on_event(self, event: Event) -> None:
        self.activity_model.add(event)
        self.activity.scrollToBottom()
        if event.level >= Level.WARNING:
            self.problems_model.add(event)
            self._problem_count += 1
            t = current()
            colour = t.bad if event.level >= Level.ERROR else t.warn
            self.problem_badge.setText(f"{self._problem_count:,} problem{'s' if self._problem_count != 1 else ''}")
            self.problem_badge.setIcon(icons.icon("warning", colour))
            self.problem_badge.setStyleSheet(f"color:{colour}; font-weight:600;")
            self.tabs.setTabText(1, f"Problems ({self._problem_count:,})")
            self.tabs.tabBar().setTabTextColor(1, QColor(colour))
        now = time.monotonic()
        if event.level >= Level.ERROR or (event.level == Level.WARNING and now - self._last_toast > 4):
            self._last_toast = now
            self.toasts.show(toast_level(event), event.title or event.message,
                             event.message if event.title else event.hint)
        if event.level == Level.CRITICAL:
            self._attention("error")

    def _notify(self, level: str, title: str, message: str) -> None:
        self.toasts.show(level, title, message)
        if bool(setting("tray", True)) and not self.isActiveWindow() and self.tray.isVisible():
            icon = {"success": QSystemTrayIcon.MessageIcon.Information,
                    "warning": QSystemTrayIcon.MessageIcon.Warning}.get(level, QSystemTrayIcon.MessageIcon.Critical)
            self.tray.showMessage(f"{APP_NAME}: {title}", message, icon, 15000)
        self._attention("done" if level == "success" else ("warning" if level == "warning" else "error"))

    def _attention(self, kind: str) -> None:
        if bool(setting("sounds", True)):
            beep(kind)
        QApplication.alert(self, 0)

    def _attention_done(self) -> None:
        pass

    def _on_intervention(self, pending: PendingDecision) -> None:
        self.banner.ask(pending)
        iv = pending.intervention
        if bool(setting("tray", True)) and self.tray.isVisible():
            self.tray.showMessage(f"{APP_NAME}: {iv.title}", iv.message, QSystemTrayIcon.MessageIcon.Critical, 30000)
        self._attention("error")

    def _on_intervention_resolved(self, pending: PendingDecision) -> None:
        self.banner.resolve(pending)
        self.toasts.show("success", "Continuing", "The problem cleared up; the job carries on.")

    def _problem_selected(self, current_index: QModelIndex, _prev: QModelIndex) -> None:
        if not current_index.isValid():
            return
        event = self.problems_model.events[current_index.row()]
        info = describe(event.code) if event.code else None
        parts = [f"<p><b>{event.message}</b></p>"]
        if info is not None and info.title:
            parts.append(f"<p><b>{event.code}</b> — {info.title}</p>")
        if info is not None and info.hint:
            parts.append(f"<p><b>What to do:</b> {info.hint}</p>")
        if event.path:
            parts.append(f"<p>File: {event.path}</p>")
        if event.details:
            parts.append(f"<pre style='white-space:pre-wrap'>{event.details}</pre>")
        self.problem_detail.setHtml("".join(parts))

    # ================================================================ browsing
    def _apply_filter(self) -> None:
        flt = ViewFilter(
            text=self.search.text().strip(),
            category=self.category.currentText(),
            status=["all", "existing", "deleted"][self.status_filter.currentIndex()],
            show_system=bool(setting("show_system", False)),
            show_streams=bool(setting("show_system", False)),
        )
        current_folder = self.list_model.folder
        self.selection.set_filter(flt)
        self.folder_model.refilter()
        if flt.text and self.result is not None:
            matches = [n for n in self.result.root.walk() if n.children is None and flt.match(n)]
            self.list_model.set_results(matches[:200000])
            more = " (showing the first 200,000)" if len(matches) > 200000 else ""
            self.crumb.setText(f"Search results: {len(matches):,} files{more}")
        else:
            self.list_model.set_folder(current_folder)
            if current_folder is not None:
                self._select_in_tree(current_folder)
            self.crumb.setText(self._crumb_text(current_folder))
        self._selection_changed()

    def _crumb_text(self, node: Node | None) -> str:
        if node is None:
            return ""
        return "  ›  ".join(node.path_parts())

    def _folder_changed(self, current_index: QModelIndex, _prev: QModelIndex) -> None:
        node = self.folder_model.node(current_index) if current_index.isValid() else None
        if node is None:
            return
        if self.list_model.search_mode:
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
            self._apply_filter()
        self.list_model.set_folder(node)
        self.crumb.setText(self._crumb_text(node))
        self.preview.show_node(node)

    def _select_in_tree(self, node: Node) -> None:
        index = self.folder_model.index_of(node)
        if index.isValid():
            self.tree.blockSignals(True)
            self.tree.setCurrentIndex(index)
            self.tree.blockSignals(False)
            self.tree.scrollTo(index)

    def _list_activated(self, index: QModelIndex) -> None:
        node = self.list_model.node(index)
        if node is None:
            return
        if node.children is not None:
            index = self.folder_model.index_of(node)
            if index.isValid():
                self.tree.setCurrentIndex(index)
                self.tree.expand(index.parent())
            else:
                self.list_model.set_folder(node)
                self.crumb.setText(self._crumb_text(node))
        else:
            self.preview.show_node(node)
            self.preview_node(node)

    def _list_current(self, current_index: QModelIndex, _prev: QModelIndex) -> None:
        self.preview.show_node(self.list_model.node(current_index))

    def _list_menu(self, pos) -> None:
        index = self.list.indexAt(pos)
        nodes = [self.list_model.node(i) for i in self.list.selectionModel().selectedRows()]
        nodes = [n for n in nodes if n is not None]
        if not nodes and index.isValid():
            node = self.list_model.node(index)
            nodes = [node] if node is not None else []
        menu = QMenu(self)
        tick = menu.addAction(icons.icon("check"), "Tick selected")
        untick = menu.addAction("Untick selected")
        menu.addSeparator()
        show = menu.addAction(icons.icon("eye"), "Preview")
        show.setEnabled(len(nodes) == 1 and nodes[0].children is None)
        chosen = menu.exec(self.list.viewport().mapToGlobal(pos))
        if chosen is tick:
            for node in nodes:
                self.selection.toggle(node, True)
        elif chosen is untick:
            for node in nodes:
                self.selection.toggle(node, False)
        elif chosen is show and nodes:
            self.preview_node(nodes[0])

    def _header_menu(self, pos) -> None:
        from .models import LIST_COLUMNS

        menu = QMenu(self)
        actions = {}
        for col in range(1, self.list_model.columnCount()):
            action = menu.addAction(LIST_COLUMNS[col])
            action.setCheckable(True)
            action.setChecked(not self.list.isColumnHidden(col))
            actions[action] = col
        chosen = menu.exec(self.list.header().mapToGlobal(pos))
        if chosen in actions:
            self.list.setColumnHidden(actions[chosen], not chosen.isChecked())

    def _tick_all_shown(self) -> None:
        if self.list_model.search_mode:
            for node in self.list_model.rows:
                if node.children is None:
                    self.selection.toggle(node, True)
        elif self.selection.root is not None:
            self.selection.select_all(True)

    def _selection_changed(self) -> None:
        sel = self.selection
        if sel.count:
            hidden = f" ({sel.hidden_selected:,} not shown)" if sel.hidden_selected else ""
            self.btn_recover.setText(f"Recover {sel.count:,} files · {format_size(sel.bytes)}{hidden}")
        else:
            self.btn_recover.setText("Recover")
        self.tree.viewport().update()
        self.list.viewport().update()
        self._update_actions()

    def _result_activated(self, index: QModelIndex) -> None:
        task = self.results_model.task(index)
        if task is not None and task.dest and os.path.exists(task.dest):
            from .models import open_in_explorer

            open_in_explorer(task.dest)

    def _open_destination(self) -> None:
        if self.last_summary is not None and self.last_summary.job_dir:
            from .models import open_in_explorer

            open_in_explorer(self.last_summary.job_dir)

    def _open_report(self) -> None:
        if self.last_summary is not None and self.last_summary.report_html:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl.fromLocalFile(self.last_summary.report_html))

    # ================================================================ state
    def _update_actions(self) -> None:
        busy = self.job is not None
        has_source = self.reader is not None
        is_image = self.info is not None and self.info.kind == "image"
        for b in (self.btn_scan, self.ov_scan, self.btn_deep, self.ov_deep):
            b.setEnabled(has_source and not busy)
        for b in (self.btn_image, self.ov_image):
            b.setEnabled(has_source and not busy and not is_image)
        self.btn_open.setEnabled(not busy)
        self.btn_recover.setEnabled(not busy and self.selection.count > 0)
        has_results = self.last_summary is not None
        self.btn_open_dest.setEnabled(has_results)
        self.btn_open_report.setEnabled(has_results and bool(self.last_summary and self.last_summary.report_html))

    def open_settings(self) -> None:
        dialog = SettingsDialog(self)
        if dialog.exec() == dialog.DialogCode.Accepted:
            from . import theme as theme_module

            theme_module.apply(QApplication.instance(), str(setting("theme", "dark")))  # type: ignore[arg-type]
            icons.clear_cache()
            if self.reader is not None:
                self.reader.policy.timeout = float(setting("read_timeout", 15))
            self._apply_filter()
            self.toasts.show("success", "Settings saved", "Some changes apply to the next scan.")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.job is not None and self.job.kind in ("scan", "deep", "recover", "image"):
            answer = QMessageBox.question(
                self, "Quit Lifeboat",
                "A job is still running. Quit anyway? Recovered files are kept and an imaging job can be resumed.")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.job.control.cancel()
            self.job.thread.join(timeout=15)
        self.tray.hide()
        self._close_source()
        event.accept()


def run_window() -> MainWindow:
    window = MainWindow()
    window.show()
    return window


__all__ = ["MainWindow", "Path", "run_window"]
