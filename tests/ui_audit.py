"""Finds text the user can't read in full: labels and buttons narrower than their text, wrapped text
shorter than its lines, drop-downs too narrow for their choice, list items cut short, widgets
spilling out of their parent, and boxes that scroll although there is room.

Run as a program it opens every window, in every state, at the sizes that matter on a screen, in
normal and large text, and prints what it finds (exit code 1 if anything):

    QT_QPA_PLATFORM=offscreen:configfile=screen.json python -m tests.ui_audit laptop HFS.img APFS_LOCKED.img

test_ui_audit.py runs it for a 1366x768 laptop screen and a 1920x1080 monitor.
"""

from __future__ import annotations

import os
import sys
import time

from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractScrollArea,
    QComboBox,
    QLabel,
    QListWidget,
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


def problems(top: QWidget, roomy: bool = False) -> list[str]:
    """What can't be read in full in ``top``. ``roomy``: the window is big enough for everything,
    so nothing may need to scroll either (a box that scrolls while there's room hides text too)."""
    from lifeboat.ui.widgets import ScrollBody, SourceList

    found: list[str] = []
    for widget in top.findChildren(QWidget):
        if not widget.isVisibleTo(top) or widget.width() <= 0 or widget.height() <= 0:
            continue
        if roomy and isinstance(widget, ScrollBody) and widget.verticalScrollBar().isVisible():
            found.append(f"{_name(widget)}: scrolls although there is room "
                         f"({widget.height()}px of {widget.heightForWidth(widget.width())}px)")
        if isinstance(widget, QLabel):
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
SIZES = {"laptop": [(980, 1), (980, 640), (1366, 700)], "monitor": [(1360, 860), (1920, 1040)]}


def tour(screen: str, hfs_image: str, locked_image: str) -> dict[str, list[str]]:
    """Every window and state, checked; returns problem -> where it was seen."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from lifeboat.device.base import DeviceInfo
    from lifeboat.events import Progress
    from lifeboat.recover import RecoverySummary
    from lifeboat.ui import dialogs as dl
    from lifeboat.ui import main_window as mw
    from lifeboat.ui import theme
    from lifeboat.ui.drive_check import DriveCheckDialog
    from lifeboat.ui.plans_dialog import PlansDialog
    from lifeboat.ui.settings_dialog import SettingsDialog

    report: dict[str, list[str]] = {}

    def pump(seconds: float = 0.25) -> None:
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.005)

    def check(label: str, widget: QWidget) -> None:
        pump(0.15)
        # Room for everything: dialogs on a big screen, and a full-HD main window.
        roomy = screen == "monitor" and (not isinstance(widget, mw.MainWindow) or widget.height() >= 1000)
        for item in problems(widget, roomy=roomy):
            report.setdefault(item, []).append(label)

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

    def dialog(label: str, window: QWidget, before=None) -> None:  # noqa: ANN001
        window.show()
        pump(0.3)
        if before is not None:
            before(window)
            pump(0.2)
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
            pump(0.4)
            size = f"{tag} {window.width()}x{window.height()}"
            check(f"home {size}", window)
            scan(window, hfs_image)
            window.pages.setCurrentIndex(1)
            window._show_overview()
            check(f"drive overview {size}", window)
            window.pages.setCurrentIndex(2)
            window.preview.show_node(next(n for n in window.result.root.walk() if n.name == "readme.txt"))
            check(f"results {size}", window)
            window.job_panel.start("Deep scan of Seagate Portable Drive (2.0 TB, USB)\u2026", "radar")
            window.job_panel.update_progress(Progress(
                "Deep scan: searching the whole drive", 412 << 30, 2000 << 30, rate=87e6, eta=17890,
                item="8,508 files found by signature, 3 partition candidates", items_done=4123, items_total=8508,
                bad_bytes=3 << 20))
            window.toasts.show("error", "File could not be recovered", "Not recovered: DCIM/100CANON/IMG_4021.CR3 "
                               "(the drive stopped answering while it was read)")
            window.toasts.show("success", "Scan finished", "486 files found, 30 of them deleted.")
            check(f"job running {size}", window)
            window.close()
            window.deleteLater()
            pump(0.1)
        window = mw.MainWindow()
        window.resize(1366, 700)
        window.show()
        pump(0.3)
        scan(window, locked_image)
        check(f"locked Mac volume {tag}", window)
        window.close()
        window.deleteLater()
        dialog(f"recover {tag}", dl.RecoverDialog(None, disk, 8508, 3_410_000_000_000, 12, 3, 9_000_000_000),
               lambda d: d.dest.setText(os.path.expanduser("~")))
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
