"""Finds text the user can't read in full: labels and buttons narrower than their text, wrapped text
shorter than its lines, drop-downs too narrow for their choice, list items cut short, widgets
spilling out of their parent, content cut off on the right of a page, and boxes that scroll
although there is room. (Text shortened with "…" on purpose, like a long folder path, must show
whole in its tooltip.)

Run as a program it opens every window, in every state, at the sizes that matter on a screen, in
normal and large text, with long names, paths and messages where they can appear, and prints what
it finds (exit code 1 if anything):

    QT_QPA_PLATFORM=offscreen:configfile=screen.json python -m tests.ui_audit laptop HFS.img APFS_LOCKED.img

test_ui_audit.py runs it for a laptop, a laptop at 150% and a monitor (AUDIT_FONT picks the font).
"""

from __future__ import annotations

import os
import shutil
import sys
import time

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractScrollArea,
    QComboBox,
    QHeaderView,
    QLabel,
    QListWidget,
    QScrollArea,
    QTableView,
    QTreeView,
    QWidget,
)

SLACK = 1          # pixels of rounding allowed


def _scrolls(widget: QWidget, top: QWidget) -> bool:
    """Inside a scroll area (its content may go on past what is shown)."""
    parent = widget.parentWidget()
    while parent is not None and parent is not top:
        if isinstance(parent, QAbstractScrollArea):
            return True
        parent = parent.parentWidget()
    return False


def _name(widget: QWidget) -> str:
    text = ""
    if isinstance(widget, (QLabel, QAbstractButton)):
        text = widget.text()
    elif isinstance(widget, QComboBox):
        text = widget.currentText()
    label = widget.objectName() or type(widget).__name__
    return f"{label} {' '.join(text.split())[:50]!r}"


def _fixed_columns(view: QTableView | QTreeView) -> list[str]:
    """Columns the user can't widen (a fixed width) must fit their title and their text."""
    header = view.horizontalHeader() if isinstance(view, QTableView) else view.header()
    model = view.model()
    if model is None:
        return []
    found = []
    text = view.fontMetrics()
    bold = QFont(header.font())
    bold.setWeight(QFont.Weight.DemiBold)
    titles = QFontMetrics(bold)
    for column in range(model.columnCount()):
        if header.isSectionHidden(column) or header.sectionResizeMode(column) != QHeaderView.ResizeMode.Fixed:
            continue
        room = header.sectionSize(column)
        title = str(model.headerData(column, Qt.Orientation.Horizontal) or "")
        if header.isVisible() and titles.horizontalAdvance(title) + 16 > room + SLACK:     # padding 8 + 8
            found.append(f"{_name(view)} column {title!r}: its title needs "
                         f"{titles.horizontalAdvance(title) + 16}px, it has {room}px")
        for row in range(min(model.rowCount(), 30)):
            value = model.data(model.index(row, column))
            need = text.horizontalAdvance(value) + 14 if isinstance(value, str) and value else 0    # padding
            if need > room + SLACK:
                found.append(f"{_name(view)} column {title!r}: {value!r} needs {need}px, it has {room}px")
                break
    return found


def problems(top: QWidget, roomy: bool = False) -> list[str]:
    """What can't be read in full in ``top``. ``roomy``: the window is big enough for everything,
    so nothing may need to scroll either (a box that scrolls while there's room hides text too)."""
    from lifeboat.ui.widgets import ElidedLabel, ScrollBody, SourceList, Toggle

    found: list[str] = []
    for widget in top.findChildren(QWidget):
        if not widget.isVisibleTo(top) or widget.width() <= 0 or widget.height() <= 0:
            continue
        if roomy and isinstance(widget, ScrollBody) and widget.verticalScrollBar().isVisible():
            found.append(f"{_name(widget)}: scrolls although there is room "
                         f"({widget.height()}px of {widget.heightForWidth(widget.width())}px)")
        if isinstance(widget, (QTableView, QTreeView)):
            found.extend(_fixed_columns(widget))
        if isinstance(widget, QScrollArea) and widget.widget() is not None:
            content, room = widget.widget().width(), widget.viewport().width()
            if content > room + SLACK:
                found.append(f"{_name(widget)}: its content is {content}px wide, it shows {room}px")
        if isinstance(widget, ElidedLabel):
            if widget.shown_text() != widget.text() and widget.toolTip() != widget.text():
                found.append(f"{_name(widget)}: shortened, and its tooltip doesn't show it whole")
            if widget.sizeHint().height() > widget.height() + SLACK:
                found.append(f"{_name(widget)}: needs {widget.sizeHint().height()}px high, has {widget.height()}px")
        elif isinstance(widget, QLabel):
            pixmap = widget.pixmap()
            if (pixmap is not None and not pixmap.isNull()) or not widget.text().strip():
                pass
            elif widget.wordWrap():
                need = widget.heightForWidth(widget.width())
                if need > widget.height() + SLACK:
                    found.append(f"{_name(widget)}: its lines need {need}px, it has {widget.height()}px")
            else:
                hint = widget.sizeHint()
                if hint.width() > widget.width() + SLACK:
                    found.append(f"{_name(widget)}: needs {hint.width()}px wide, has {widget.width()}px")
                elif hint.height() > widget.height() + SLACK:
                    found.append(f"{_name(widget)}: needs {hint.height()}px high, has {widget.height()}px")
        elif isinstance(widget, QAbstractButton) and widget.text().strip():
            hint = widget.sizeHint()
            if hint.width() > widget.width() + SLACK:
                found.append(f"{_name(widget)}: needs {hint.width()}px wide, has {widget.width()}px")
        elif isinstance(widget, QAbstractButton) and not isinstance(widget, Toggle) and not widget.toolTip().strip():
            found.append(f"{_name(widget)} in {_name(parent)}: only an icon, and no tooltip says what it does"
                         if (parent := widget.parentWidget()) is not None else f"{_name(widget)}: no tooltip")
        elif isinstance(widget, QListWidget) and not isinstance(widget, SourceList):
            # Short lists of words (Settings' pages): each item whole. (The drive list shortens
            # long paths with "…" on purpose, and shows them whole in its tooltips.)
            room = widget.viewport().width()
            for row in range(widget.count()):
                item = widget.item(row)
                if item.isHidden():
                    continue
                need = widget.fontMetrics().horizontalAdvance(item.text()) + 24
                if not item.icon().isNull():
                    need += widget.iconSize().width() + 8
                if need > room + SLACK:
                    found.append(f"{_name(widget)} item {item.text()!r}: needs {need}px, has {room}px")
        elif isinstance(widget, QComboBox) and widget.currentText():
            need = widget.fontMetrics().horizontalAdvance(widget.currentText()) + 40   # padding and arrow
            if widget.itemIcon(widget.currentIndex()).isNull() is False:
                need += 22
            if need > widget.width() + SLACK:
                found.append(f"{_name(widget)}: needs {need}px wide, has {widget.width()}px")
        parent = widget.parentWidget()
        if parent is not None and parent is not top and not _scrolls(widget, top):
            box = widget.geometry()
            if (box.right() > parent.width() - 1 + SLACK or box.bottom() > parent.height() - 1 + SLACK
                    or box.left() < -SLACK or box.top() < -SLACK):
                found.append(f"{_name(widget)}: spills out of {_name(parent)}")
    return found


# ---------------------------------------------------------------------------- the tour
# Window sizes on each screen: the smallest it allows, and maximized (the taskbar and title bar take
# about 70 px). "laptop150" is a 1920x1080 laptop at 150% scaling, as Windows sets many of them up.
SIZES = {"laptop": [(980, 1), (980, 640), (1366, 700)], "laptop150": [(1280, 650)],
         "monitor": [(1360, 860), (1920, 1040)]}
# A file in a photo library: about as long as paths get in practice.
LONG_PATH = ("Users/alexandra.rodriguez-whitfield/Pictures/Photos Library.photoslibrary/originals/7/"
             "7F3A9C21-5B8E-4D2A-9E61-0C4B7D2E9A11 (edited, exported for the family album).heic")


def tour(screen: str, hfs_image: str, locked_image: str) -> dict[str, list[str]]:
    """Every window and state, checked; returns problem -> where it was seen."""
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    from PySide6.QtWidgets import QApplication

    def quiet(kind: QtMsgType, _context: object, message: str) -> None:
        if "propagateSizeHints" not in message:       # the offscreen platform says so for every window
            sys.stderr.write(f"{message}\n")

    qInstallMessageHandler(quiet)
    app = QApplication.instance() or QApplication([])
    if os.environ.get("AUDIT_FONT"):             # e.g. "DejaVu Sans", the font of GitHub's Linux runners
        from PySide6.QtGui import QFont

        app.setFont(QFont(os.environ["AUDIT_FONT"]))
    from lifeboat.device.base import DeviceInfo
    from lifeboat.errors import Cancelled
    from lifeboat.events import Choice, Intervention, Progress
    from lifeboat.recover import RecoverySummary
    from lifeboat.ui import dialogs as dl
    from lifeboat.ui import main_window as mw
    from lifeboat.ui import theme
    from lifeboat.ui.drive_check import DriveCheckDialog
    from lifeboat.ui.plans_dialog import PlansDialog
    from lifeboat.ui.settings_dialog import SettingsDialog

    report: dict[str, list[str]] = {}
    # As most people start it: without administrator rights (the home page then says so).
    mw.is_admin = lambda: False  # type: ignore[attr-defined]

    def pump(seconds: float = 0.25) -> None:
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.005)

    def expect(reached: bool, label: str) -> None:
        if not reached:          # the tour didn't get the window into the state it meant to check
            report.setdefault("the tour didn't reach this state", []).append(label)

    tallest = max(height for _width, height in SIZES[screen])     # a maximized window's height

    def check(label: str, widget: QWidget) -> None:
        pump(0.15)
        # Finish the layout passes still queued (new text takes a few, one after the other), so
        # a slow machine is measured in the same state as a fast one.
        for _ in range(12):
            app.sendPostedEvents(None, QEvent.Type.LayoutRequest.value)
            app.processEvents()
        # Room for everything: dialogs on a big screen, and a full-HD main window.
        roomy = screen == "monitor" and (not isinstance(widget, mw.MainWindow) or widget.height() >= 1000)
        for item in problems(widget, roomy=roomy):
            report.setdefault(item, []).append(label)
        # The main window grew to fit its content. (Dialogs keep within the screen by themselves:
        # fit_height. This screen has no taskbar, so they may be taller than the main window.)
        opened = widget.property("audit_width")
        if isinstance(widget, mw.MainWindow) and opened and widget.width() > int(opened) + SLACK:
            report.setdefault(f"MainWindow: made itself wider than it was ({opened}px to {widget.width()}px)",
                              []).append(label)
        if isinstance(widget, mw.MainWindow) and widget.height() > tallest:
            report.setdefault(f"{type(widget).__name__}: taller than the screen allows ({widget.height()}px of "
                              f"{tallest}px)", []).append(label)

    def scan(window: mw.MainWindow, path: str) -> None:
        info = DeviceInfo(path=path, kind="image", size=os.path.getsize(path), model="Disk image")
        window.images.append(info)
        window.sources.set_devices(window.devices + window.images, info.identity)
        window.select_source(info)
        window.quick_scan()
        end = time.time() + 120
        while window.job is not None and time.time() < end:
            pump(0.05)
        pump(0.3)
        window.toasts.clear()

    def settle(window: mw.MainWindow) -> None:
        """Until the drive list has come in (on a Mac, diskutil can take a second: measured while
        the page was taking in its note, the audit saw it half laid out) and the layout followed it."""
        end = time.time() + 20
        while window._enum_running and time.time() < end:
            pump(0.05)
        pump(0.3)
        window.setProperty("audit_width", window.width())

    def deepest_folder(window: mw.MainWindow):  # noqa: ANN202
        folders = [n for n in window.result.root.walk() if n.children is not None]
        return max(folders, key=lambda n: len(n.path_parts()))

    def recovering(window: mw.MainWindow) -> None:
        """A recovery as it runs: the file being copied has a long path, and the drive stops
        answering (the banner asks what to do)."""
        def work(job):  # noqa: ANN001, ANN202
            job.report(Progress("Recovering files", 412 << 30, 2000 << 30, rate=87e6, eta=17890,
                                item=LONG_PATH, items_done=4123, items_total=8508, bad_bytes=3 << 20))
            job.handler.request(Intervention(
                "LB-120", "Source drive disconnected",
                "The drive stopped answering while copying files. Reconnect it (try another USB port or cable). "
                "Lifeboat resumes automatically when it is back.", (Choice.RETRY, Choice.ABORT),
                auto_retry=lambda: False))
            while not job.control.cancelled:
                time.sleep(0.02)
            raise Cancelled()

        window._start("recover", "Recovering 8,508 files from Seagate Portable Drive (2.0 TB, USB) to "
                      "Backup Plus (E:)\u2026", work)

    def stop(window: mw.MainWindow) -> None:
        if window.job is not None:
            window.job.control.cancel()
            end = time.time() + 10
            while window.job is not None and time.time() < end:
                pump(0.05)
        window.toasts.clear()

    def dialog(label: str, window: QWidget, before=None, reached=None) -> None:  # noqa: ANN001
        window.show()
        pump(0.3)
        if before is not None:
            before(window)
            pump(0.7)                  # after the dialogs' checks, which wait for typing to pause
        if reached is not None:
            expect(reached(window), label)
        check(label, window)
        window.close()
        window.deleteLater()

    disk = DeviceInfo(path="/dev/sdz", kind="disk", size=2_000_398_934_016, model="Seagate Portable Drive",
                      serial="NA9XYZ12", bus="USB")
    for text_size in ("normal", "large"):
        theme.apply(app, "dark", text_size)
        tag = f"[{text_size} text]"
        for width, height in SIZES[screen]:
            window = mw.MainWindow()
            window.resize(width, height)
            window.show()
            settle(window)
            size = f"{tag} {window.width()}x{window.height()}"
            check(f"home {size}", window)
            scan(window, hfs_image)
            window.pages.setCurrentIndex(1)
            window._show_overview()
            check(f"drive overview {size}", window)
            window.pages.setCurrentIndex(2)
            window.preview.show_node(next(n for n in window.result.root.walk() if n.name == "readme.txt"))
            check(f"results {size}", window)
            for tab in range(1, window.tabs.count()):          # Problems, Results, Disk map
                window.tabs.setCurrentIndex(tab)
                check(f"{window.tabs.tabText(tab).split(' (')[0]} tab {size}", window)
            window.tabs.setCurrentIndex(0)
            folder = deepest_folder(window)
            window.tree.setCurrentIndex(window.folder_model.index_of(folder))
            window.crumb.setText(window._crumb_text(folder) + "  ›  " + "  ›  ".join(LONG_PATH.split("/")[:-1]))
            expect(len(window.crumb.text()) > 150, f"deep folder {size}")
            check(f"deep folder {size}", window)
            window.job_panel.start("Deep scan of Seagate Portable Drive (2.0 TB, USB)\u2026", "radar")
            window.job_panel.update_progress(Progress(
                "Deep scan: searching the whole drive", 412 << 30, 2000 << 30, rate=87e6, eta=17890,
                item="8,508 files found by signature, 3 partition candidates", items_done=4123, items_total=8508,
                bad_bytes=3 << 20))
            window.toasts.show("error", "File could not be recovered", "Not recovered: DCIM/100CANON/IMG_4021.CR3 "
                               "(the drive stopped answering while it was read)")
            window.toasts.show("success", "Scan finished", "486 files found, 30 of them deleted.")
            check(f"job running {size}", window)
            window.job_panel.finish()
            window.toasts.clear()
            recovering(window)
            pump(0.5)
            expect(window.banner.isVisible() and window.job_panel.active
                   and LONG_PATH in window.status_text.text(), f"recovering, drive disconnected {size}")
            check(f"recovering, drive disconnected {size}", window)
            stop(window)
            window.close()
            window.deleteLater()
            pump(0.1)
        for width, height in (SIZES[screen][0], SIZES[screen][-1]):        # smallest and maximized
            window = mw.MainWindow()
            window.resize(width, height)
            window.show()
            settle(window)
            scan(window, locked_image)
            check(f"locked Mac volume {tag} {window.width()}x{window.height()}", window)
            window.close()
            window.deleteLater()
        # 3.4 TB to the home folder: not enough room (on most computers), so it offers to tick only what fits
        home, size = os.path.expanduser("~"), 3_410_000_000_000
        dialog(f"recover {tag}", dl.RecoverDialog(None, disk, 8508, size, 12, 3, 9_000_000_000),
               lambda d: d.dest.setText(home),
               lambda d: d.space_row.isVisible() or shutil.disk_usage(home).free >= size)
        dialog(f"disk image {tag}", dl.ImageDialog(None, disk),
               lambda d: d.path.setText(os.path.join(os.path.expanduser("~"), "drive.img")))
        dialog(f"deep scan {tag}", dl.DeepScanDialog(None, disk))
        dialog(f"unlock {tag}", dl.UnlockDialog(None, "Macintosh HD - Data", "the name of our first dog",
                                                "That password doesn't open this volume. Check the hint, the "
                                                "keyboard layout and Caps Lock, then try again."))
        dialog(f"about {tag}", dl.AboutDialog(None))
        dialog(f"licenses {tag}", dl.LicensesDialog(None))
        dialog(f"what's new {tag}", dl.WhatsNewDialog(None))
        dialog(f"summary {tag}", dl.SummaryDialog(None, RecoverySummary("/media/Recovered/Lifeboat Recovery", [],
                                                                         3600.0), 120, 5_000_000_000))
        dialog(f"error {tag}", dl.ErrorDialog(None, "Unexpected error", "The scan stopped because of an unexpected "
                                              "error: [Errno 5] Input/output error", "Traceback ...\n" * 5,
                                              "Restart Lifeboat; if it happens again, send the log file."))
        dialog(f"drive check {tag}", DriveCheckDialog(None))
        for period in ("monthly", "yearly", "lifetime"):
            dialog(f"plans {period} {tag}", PlansDialog(None),
                   lambda d, p=period: (d.periods[p].click(), d.cards["pro"].trial.click()))
        settings = SettingsDialog(None)
        settings.show()
        pump(0.3)
        for row in range(settings.nav.count()):
            settings.nav.setCurrentRow(row)
            check(f"settings: {settings.PAGES[row]} {tag}", settings)
        settings._activate()
        settings.nav.setCurrentRow(settings.PAGES.index("Account & license"))
        check(f"settings: license notice {tag}", settings)
        settings.close()
        settings.deleteLater()
    return report


def main(argv: list[str]) -> int:
    screen, hfs_image, locked_image = argv
    report = tour(screen, hfs_image, locked_image)
    for item, where in sorted(report.items()):
        print(f"- {item}\n    seen in: {', '.join(sorted(set(where)))}")
    print(f"{len(report)} problem(s) on the {screen} screen")
    return 1 if report else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
