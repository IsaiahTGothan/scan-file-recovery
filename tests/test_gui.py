"""GUI tests (offscreen): build the window, scan an image, tick files, recover."""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from lifeboat.device.base import DeviceInfo  # noqa: E402
from tests.conftest import image_path  # noqa: E402


@pytest.fixture(scope="module")
def app():
    instance = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from lifeboat.ui import theme

    theme.apply(instance, "dark")
    yield instance


@pytest.fixture(autouse=True)
def _free_windows(app):
    """Destroy every window a test opened while Qt is fully alive (as the app does on exit)."""
    yield
    import shiboken6

    for widget in QtWidgets.QApplication.topLevelWidgets():
        if shiboken6.isValid(widget) and widget.parent() is None:
            widget.close()
            shiboken6.delete(widget)
    app.processEvents()


def _pump(app, seconds=0.3):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def _wait(app, window, timeout=60):
    end = time.time() + timeout
    while window.job is not None and time.time() < end:
        _pump(app, 0.05)
    assert window.job is None, "job did not finish"


def _fake_dialogs(monkeypatch, destination):
    """Answer the recovery dialogs without showing them; record any error dialog."""
    import lifeboat.ui.main_window as mw
    from lifeboat.ui import dialogs

    class FakeDialog:
        DialogCode = dialogs.RecoverDialog.DialogCode

        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return dialogs.RecoverDialog.DialogCode.Accepted

        def values(self):
            return {"destination": str(destination), "job_folder": True, "verify": True,
                    "thoroughness": "standard", "preserve_times": True, "mark_damaged": False, "resume": False}

        def deleteLater(self):
            pass

    class FakeSummary:
        def __init__(self, parent, summary):
            self.summary = summary
            self.show_problems = False

        def exec(self):
            return 1

        def deleteLater(self):
            pass

    error_dialogs = []

    class FakeError:
        def __init__(self, *args, **kwargs):
            error_dialogs.append(args)

        def exec(self):
            return 0

        def deleteLater(self):
            pass

    monkeypatch.setattr(mw, "RecoverDialog", FakeDialog)
    monkeypatch.setattr(mw, "SummaryDialog", FakeSummary)
    monkeypatch.setattr(mw, "ErrorDialog", FakeError)
    return error_dialogs


def test_window_scan_select_recover(app, images, tmp_path, monkeypatch):
    from lifeboat.ui.main_window import MainWindow

    window = MainWindow()
    window.show()
    _pump(app)
    path = str(image_path("fat16"))
    info = DeviceInfo(path=path, kind="image", size=os.path.getsize(path), model="Disk image")
    window.images.append(info)
    window.sources.set_devices(window.devices + window.images, info.identity)
    window.select_source(info)
    assert window.reader is not None
    window.quick_scan()
    _wait(app, window)
    assert window.result is not None
    assert window.pages.currentIndex() == 2
    volume_root = window.result.volumes[0].root
    window.selection.toggle(volume_root.child("DCIM"), True)
    assert window.selection.count == 2
    assert "Recover 2 files" in window.btn_recover.text()
    # filters
    window.search.setText("*.jpg")
    window._apply_filter()
    assert window.list_model.search_mode and len(window.list_model.rows) >= 2
    window.search.setText("")
    window._apply_filter()

    error_dialogs = _fake_dialogs(monkeypatch, tmp_path)
    window.recover()
    _wait(app, window)
    _pump(app)
    summary = window.last_summary
    assert summary is not None and summary.outcome == "success"
    assert summary.count("ok") == 2
    assert window.results_model.rowCount() == 0  # "problems only" view is empty
    window.results_model.set_mode("all")
    assert window.results_model.rowCount() == 2
    # every event reached the activity log
    assert any("Recovery finished" in e.message for e in window.activity_model.events)
    assert not error_dialogs
    window.close()


def test_failing_drive_problems_are_visible(app, images, tmp_path, monkeypatch):
    """Bad sectors, and a drive that drops off USB in the middle of a recovery.

    Every notification channel must fire (pop-ups, the red banner, the Problems tab and
    its badge), the job must continue by itself once the drive is back, the damaged file
    must be reported with its exact unreadable range, and nothing may raise.
    """
    import sys

    import lifeboat.ui.main_window as mw
    from lifeboat.device.image import ImageDevice
    from lifeboat.device.simulated import FaultPlan, SimulatedFailingDevice
    from lifeboat.recover import Status

    unhandled = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, value, tb: unhandled.append(value))
    error_dialogs = _fake_dialogs(monkeypatch, tmp_path)
    path = str(image_path("exfat"))
    dev = SimulatedFailingDevice(ImageDevice(path), FaultPlan())
    window = mw.MainWindow()
    window._open = lambda info: dev
    toasts = []
    show = window.toasts.show

    def record(level, title, message="", timeout_ms=None):
        toasts.append((level, title, message))
        show(level, title, message, timeout_ms)

    window.toasts.show = record
    window.show()
    _pump(app)
    info = DeviceInfo(path=path, kind="image", size=os.path.getsize(path), model="Disk image")
    window.images.append(info)
    window.select_source(info)
    window.quick_scan()
    _wait(app, window)
    root = window.result.volumes[0].root
    video = root.child("DCIM").child("100MEDIA").child("DJI_0001.MP4")
    extent = video.volume.layout(video).extents[0]
    assert extent.length > (1 << 20) + 4096
    bad_at = extent.disk_offset + (1 << 20)
    dev.plan.bad = [(bad_at, bad_at + 4096)]
    window.selection.toggle(video, True)
    window.selection.toggle(root.child("many"), True)
    count = window.selection.count
    problems_before = window.problems_model.rowCount()
    dev.plan.disconnect_after_reads = dev.reads + 12

    window.recover()
    banner = None
    end = time.time() + 120
    while window.job is not None and time.time() < end:
        _pump(app, 0.05)
        if banner is None and window.banner.pending is not None:
            banner = (window.banner.isVisible(), window.banner.title.text(), window.banner.message.text())
            dev.reconnect()  # plug the drive back in: the job must notice by itself
    assert window.job is None, "recovery did not finish"
    _pump(app)

    # The red banner asked for the drive, then went away on its own.
    assert banner is not None, "no banner for the disconnected drive"
    visible, title, message = banner
    assert visible and "disconnected" in title.lower() and "LB-120" in message
    assert not window.banner.isVisible()
    assert any(t[1] == "Continuing" for t in toasts)
    # Problems tab, badge and pop-ups.
    codes = [e.code for e in window.problems_model.events[problems_before:]]
    assert "LB-120" in codes and "LB-110" in codes
    assert window.tabs.tabText(1).startswith("Problems (")
    assert "problem" in window.problem_badge.text()
    assert any(level == "critical" and "disconnected" in title.lower() for level, title, _m in toasts)
    assert any(title == "Recovery finished with problems" for _l, title, _m in toasts)
    # The result: one damaged file with exactly the bad 4 KiB, everything else intact.
    summary = window.last_summary
    assert summary is not None and summary.outcome == "warning"
    assert len(summary.tasks) == count
    damaged = [t for t in summary.tasks if t.status != Status.OK]
    assert [t.node for t in damaged] == [video]
    assert damaged[0].status == Status.PARTIAL
    assert damaged[0].damaged_ranges() == [(1 << 20, (1 << 20) + 4096)]
    window.results_model.set_mode("problems")
    assert window.results_model.rowCount() == 1
    assert not error_dialogs, error_dialogs
    assert not unhandled, unhandled
    window.close()


def test_toast_closed_early_does_not_raise(app, monkeypatch):
    """A pop-up closed (by the user or a newer pop-up) before it expires must not crash later."""
    import sys

    from PySide6.QtCore import QCoreApplication, QEvent

    from lifeboat.ui.widgets import ToastArea

    unhandled = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, value, tb: unhandled.append(value))
    host = QtWidgets.QWidget()
    host.resize(800, 600)
    host.show()
    area = ToastArea(host)
    area.show("info", "closed by the user", timeout_ms=150)
    area.clear()
    for i in range(ToastArea.MAX + 3):
        area.show("warning", f"burst {i}", timeout_ms=100)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    _pump(app, 0.5)
    assert not unhandled, unhandled
    assert area.toasts == []
    host.close()


def test_problem_bursts_are_summarised(app, monkeypatch):
    """Hundreds of errors in a burst give a few pop-ups, but every one reaches the Problems tab."""
    import lifeboat.ui.main_window as mw
    from lifeboat.events import Event, Level

    monkeypatch.setattr(mw.MainWindow, "TOAST_GAP_ERROR", 0.3)
    window = mw.MainWindow()
    toasts = []
    show = window.toasts.show
    window.toasts.show = lambda level, title, message="", timeout_ms=None: (
        toasts.append((level, title, message)), show(level, title, message, timeout_ms))
    window.show()
    _pump(app)
    before = window.problems_model.rowCount()
    toasts.clear()
    for i in range(300):
        window.bus.emit(Event(Level.ERROR, f"Not recovered: file {i}", code="LB-402"))
    _pump(app, 1.0)
    assert window.problems_model.rowCount() - before == 300
    assert 1 <= len(toasts) <= 4, toasts
    assert toasts[0][:3] == ("error", "File could not be recovered", "Not recovered: file 0")
    assert toasts[-1][1] == "299 more problems"
    window.close()


def test_selection_counters(app):
    from lifeboat.fs.model import F, Node
    from lifeboat.ui.models import Selection, ViewFilter

    root = Node("disk", F.DIR | F.VIRTUAL)
    vol = root.add(Node("vol", F.DIR | F.VOLUME))
    a = vol.add(Node("a", F.DIR))
    files = [a.add(Node(f"f{i}.jpg" if i % 2 else f"f{i}.txt", 0, 100)) for i in range(10)]
    deleted = a.add(Node("gone.jpg", F.DELETED, 50))
    sel = Selection()
    sel.set_root(root)
    sel.toggle(a, True)
    assert sel.count == 11 and sel.bytes == 1050
    assert sel.state(vol).name == "Checked"
    sel.toggle(files[0], False)
    assert sel.state(a).name == "PartiallyChecked"
    sel.toggle(a, False)
    assert sel.count == 0
    sel.set_filter(ViewFilter(text="*.jpg", status="deleted"))
    sel.toggle(vol, True)
    assert sel.selected_files() == [deleted]
    sel.set_filter(ViewFilter())
    assert sel.hidden_selected == 0 and sel.count == 1


def test_recover_dialog_resume_survives_closing(app, tmp_path):
    """The dialog's answers are read after it closes; Resume must still be on."""
    from lifeboat.recover.journal import Journal
    from lifeboat.ui.dialogs import RecoverDialog

    info = DeviceInfo(path=str(tmp_path / "disk.img"), kind="image", size=1 << 20, model="Disk image")
    job = tmp_path / "Lifeboat Recovery 2026-01-01 10.00"
    journal = Journal(str(job))
    journal.open({"source": info.identity, "source_name": info.title, "files": 3})
    journal.close()
    dialog = RecoverDialog(None, info, 3, 3000, 0, 0, 1000)
    dialog.dest.setText(str(job))
    dialog._validate()
    assert dialog.resume_info is not None and dialog.resume.isChecked()
    dialog.show()
    dialog._accept()  # closes the dialog, as clicking "Start recovery" does
    assert not dialog.isVisible()
    values = dialog.values()
    assert values["resume"] is True
    assert values["job_folder"] is False  # resume in place, not in a new folder inside the old one
    assert values["destination"] == str(job)


def test_image_dialog_guards_existing_images(app, tmp_path):
    from lifeboat.device.image import MemoryDevice
    from lifeboat.imaging import ImagingJob, ImagingOptions
    from lifeboat.rescue.reader import ReadPolicy, RescueReader
    from lifeboat.ui.dialogs import ImageDialog

    data = bytes(range(256)) * 4096  # 1 MiB
    drive_a = DeviceInfo(path=r"\\.\PhysicalDrive7", kind="disk", size=len(data), model="ST1000", serial="AAA111")
    drive_b = DeviceInfo(path=r"\\.\PhysicalDrive7", kind="disk", size=len(data), model="ST1000", serial="BBB222")
    out = tmp_path / "drive.img"
    reader = RescueReader(MemoryDevice(data), ReadPolicy(timeout=1.0))
    assert ImagingJob(reader, drive_a, ImagingOptions(str(out))).run().outcome == "success"

    same = ImageDialog(None, drive_a)
    same.path.setText(str(out))
    assert same.start.isEnabled() and not same.same_drive.isVisibleTo(same)  # resumes its own image

    other = ImageDialog(None, drive_b)
    other.path.setText(str(out))
    assert not other.start.isEnabled()  # another customer's drive: refused...
    assert other.same_drive.isVisibleTo(other)
    other.same_drive.setChecked(True)  # ...unless confirmed to be the same drive
    assert other.start.isEnabled()
    other.show()
    other._accept()
    assert other.values()["same_drive"] is True

    stranger = tmp_path / "holiday.img"
    stranger.write_bytes(b"x" * 4096)
    unknown = ImageDialog(None, drive_a)
    unknown.path.setText(str(stranger))
    assert not unknown.start.isEnabled() and not unknown.same_drive.isVisibleTo(unknown)


class _FakeBox:
    """Stands in for the modal QMessageBox shown after imaging; clicks its first button."""

    class Icon:
        Information = Warning = None

    class ButtonRole:
        AcceptRole = RejectRole = None

    def __init__(self, *args):
        self.buttons = []

    def setWindowTitle(self, *a): pass
    def setIcon(self, *a): pass
    def setText(self, *a): pass
    def setInformativeText(self, *a): pass

    def addButton(self, text, role):
        self.buttons.append(text)
        return text

    def exec(self):
        return 0

    def clickedButton(self):
        return self.buttons[0]  # "Open the image"

    def deleteLater(self):
        pass


def test_scan_then_image_then_open_the_image(app, images, tmp_path, monkeypatch):
    """The app's own flow: scan a drive, image it with the same reader, open the image."""
    import lifeboat.ui.main_window as mw
    from lifeboat.device.image import ImageDevice
    from lifeboat.device.simulated import FaultPlan, SimulatedFailingDevice

    source = image_path("mbr_disk")
    dev = SimulatedFailingDevice(ImageDevice(source), FaultPlan())
    dev.info.kind = "disk"  # a physical drive as far as the app is concerned
    output = tmp_path / "drive.img"

    class FakeImageDialog:
        DialogCode = mw.ImageDialog.DialogCode

        def __init__(self, *args):
            pass

        def exec(self):
            return self.DialogCode.Accepted

        def values(self):
            return {"output": str(output), "thoroughness": "standard", "same_drive": False}

        def deleteLater(self):
            pass

    monkeypatch.setattr(mw, "ImageDialog", FakeImageDialog)
    monkeypatch.setattr(mw, "QMessageBox", _FakeBox)
    window = mw.MainWindow()
    window._open = lambda info: dev if info.kind == "disk" else mw.MainWindow._open(window, info)
    window.show()
    window.select_source(dev.info)
    window.quick_scan()
    _wait(app, window)
    files_on_drive = window.result.counts()[0]
    assert files_on_drive > 0
    window.create_image()
    _wait(app, window, timeout=120)
    _pump(app)
    assert output.read_bytes() == source.read_bytes()
    # "Open the image" was clicked: the image is now the source, without the drive.
    assert window.info.kind == "image" and window.info.path == str(output)
    window.quick_scan()
    _wait(app, window)
    assert window.result.counts()[0] == files_on_drive
    window.close()


def test_deep_scan_from_the_window(app, tmp_path, monkeypatch):
    """A drive with no filesystem left: the deep scan finds a photo by its content."""
    import lifeboat.ui.main_window as mw
    from tests.test_carving import _jpeg

    photo = _jpeg(7)
    raw = tmp_path / "wiped.img"
    raw.write_bytes(os.urandom(65536) + photo + bytes(-len(photo) % 512) + os.urandom(65536))

    class FakeDeepDialog:
        DialogCode = mw.DeepScanDialog.DialogCode

        def __init__(self, *args):
            pass

        def exec(self):
            return self.DialogCode.Accepted

        def values(self):
            return {"find_partitions": True, "carve": True, "groups": {"Photos", "Pictures", "Documents"}}

        def deleteLater(self):
            pass

    monkeypatch.setattr(mw, "DeepScanDialog", FakeDeepDialog)
    window = mw.MainWindow()
    window.show()
    info = DeviceInfo(path=str(raw), kind="image", size=raw.stat().st_size, model="Disk image")
    window.images.append(info)
    window.select_source(info)
    window.quick_scan()
    _wait(app, window)
    assert window.result.counts()[0] == 0  # nothing left for a quick scan
    window.deep_scan()
    _wait(app, window, timeout=120)
    assert window.result is not None and window.result.carved == 1
    carved = [n for n in window.result.root.walk() if n.children is None]
    assert len(carved) == 1 and carved[0].size == len(photo)
    assert window.pages.currentIndex() == 2
    window.close()
