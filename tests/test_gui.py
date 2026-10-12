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
        def __init__(self, parent, summary, *rest):
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
    window.reader.policy.wake_seconds = 0.2  # this test unplugs the drive for real
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
    # On a slow machine the burst can outlast the gap, so another problem gets its own pop-up;
    # either way the summary counts exactly the ones that didn't.
    own = sum(1 for t in toasts if not t[1].endswith("more problems"))
    assert toasts[-1][1] == f"{300 - own:,} more problems"
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


def test_source_list_changes_during_a_job_do_not_loop(app, tmp_path, monkeypatch):
    """While a job runs: a refreshed drive list or a click on another drive must neither switch
    the source nor re-enter select_source endlessly (that crashed the app mid-recovery)."""
    import sys
    from types import SimpleNamespace

    import lifeboat.ui.main_window as mw
    from lifeboat.ui.widgets import ROLE_INFO

    unhandled = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, value, tb: unhandled.append(value))
    image = tmp_path / "a.img"
    image.write_bytes(bytes(1 << 20))
    window = mw.MainWindow()
    window.show()
    info = DeviceInfo(path=str(image), kind="image", size=1 << 20, model="Disk image")
    window.images.append(info)
    window.sources.set_devices(window.devices + window.images, info.identity)
    window.select_source(info)
    assert window.info is info
    calls = []
    original = window._select_source
    window._select_source = lambda i: (calls.append(i), original(i))
    window.job = SimpleNamespace(kind="recover")  # a recovery is running
    try:
        # A new drive appears (the drive list refreshes while the job runs).
        disk = DeviceInfo(path=r"\\.\PhysicalDrive8", kind="disk", size=1 << 30, model="New drive", serial="N1")
        window._devices_ready([disk])
        assert calls == []  # re-highlighting the current source is not a selection
        # The user clicks the new drive.
        row = next(r for r in range(window.sources.count()) if window.sources.item(r).data(ROLE_INFO) is disk)
        window.sources.setCurrentRow(row)
        _pump(app, 0.1)
        assert len(calls) == 1 and calls[0] is disk  # asked once, refused once
        assert window.info is info  # still recovering from the same source
        current = window.sources.currentItem().data(ROLE_INFO)
        assert current is info  # the list shows the source being recovered again
    finally:
        window.job = None
    assert not unhandled, unhandled
    window.close()


def _check_results():
    from lifeboat.device.check import CheckResult

    return [CheckResult("Healthy SSD", size=1 << 30, read_ok=True, read_seconds=0.01, path="/dev/sdd"),
            CheckResult("Seagate Portable", size=0, connection="USB", path="/dev/sdb"),
            CheckResult("JMicron bridge", kind="adapter", connection="USB")]


def _wait_for(app, condition, timeout=10):
    end = time.time() + timeout
    while not condition() and time.time() < end:
        _pump(app, 0.02)
    assert condition()


def test_drive_check_window(app):
    from lifeboat.ui.drive_check import DriveCheckDialog

    results = _check_results()
    calls = []

    def runner(progress, in_use):
        calls.append(dict(in_use))
        for index, result in enumerate(results):
            progress(index, len(results), result.name)
        return results

    dialog = DriveCheckDialog(None, {"/dev/sdx": 123}, runner=runner)
    dialog.show()
    _wait_for(app, lambda: dialog.table.rowCount() == 3)
    assert calls == [{"/dev/sdx": 123}]
    assert dialog.status.text() == "3 devices found · 1 readable · 2 need attention"
    # It opens on the first device that needs attention, with its advice.
    assert dialog.table.currentRow() == 1
    assert "doesn't answer" in dialog.details.toPlainText()
    assert dialog.table.item(1, 3).text() == "No" and dialog.table.item(1, 2).text() == "Not reported"
    assert not dialog.btn_use.isEnabled()
    dialog.table.setCurrentCell(0, 0)
    assert dialog.btn_use.isEnabled()
    for expected in ("Healthy SSD", "Seagate Portable", "Adapter only, no drive", "What to do:"):
        assert expected in dialog.report()
    dialog._copy()
    assert "Seagate Portable" in QtWidgets.QApplication.clipboard().text()
    dialog._use()
    assert dialog.chosen_path == "/dev/sdd" and dialog.result() == dialog.DialogCode.Accepted


def test_drive_check_window_closed_while_checking(app):
    import threading

    from lifeboat.ui.drive_check import DriveCheckDialog

    gate = threading.Event()
    reached = []

    def runner(progress, in_use):
        progress(0, 2, "first")
        gate.wait(5)
        reached.append("after")
        progress(1, 2, "second")  # the window is closed: this stops the check
        reached.append("not reached")
        return _check_results()

    dialog = DriveCheckDialog(None, runner=runner)
    dialog.show()
    _pump(app, 0.2)
    assert dialog.running and dialog.status.text() == "Checking first…"
    dialog.reject()
    gate.set()
    dialog._thread.join(5)
    _pump(app, 0.2)
    assert reached == ["after"]
    assert dialog.table.rowCount() == 0  # nothing reported after closing
    dialog.deleteLater()
    _pump(app, 0.1)


def test_drive_check_failure_is_shown(app):
    from lifeboat.ui.drive_check import DriveCheckDialog

    def runner(progress, in_use):
        raise OSError("Windows did not return its device list")

    dialog = DriveCheckDialog(None, runner=runner)
    _wait_for(app, lambda: dialog.btn_again.isEnabled())
    assert dialog.status.text() == "The check couldn't finish"
    assert "device list" in dialog.details.toPlainText()


def test_drive_that_does_not_answer_is_listed_and_explained(app, monkeypatch):
    import lifeboat.ui.main_window as mw
    from lifeboat.device.base import NOT_ANSWERING

    window = mw.MainWindow()
    window.show()
    _pump(app, 0.1)
    drive = DeviceInfo(path="/dev/sdz", kind="disk", size=0, model="Portable", vendor="Seagate", bus="USB",
                       status=NOT_ANSWERING, notes=["Run a Drive check for what to try."])
    window._enum_running = False
    window.devices = []
    window._devices_ready([drive])
    problems_before = window.problems_model.rowCount()
    window.select_source(drive)
    assert window.device is None and window.reader is None and window.info is drive
    assert window.pages.currentIndex() == 1
    assert window.problems_model.rowCount() == problems_before  # explained, not reported as an error
    assert "doesn't answer" in window.ov_notes.toPlainText()
    assert window.card_capacity.value.text() == "Not reported"
    assert not window.ov_check.isHidden() and not window.btn_scan.isEnabled()

    opened = []

    class FakeCheck:
        def __init__(self, parent, in_use):
            opened.append(in_use)
            self.chosen_path = None
            self.restart_requested = False

        def exec(self):
            return 0

        def deleteLater(self):
            pass

    monkeypatch.setattr(mw, "DriveCheckDialog", FakeCheck)
    window.ov_check.click()
    assert opened == [{}]  # no job running: nothing is left out of the check
    window.close()


def test_tick_tools_split_a_recovery_across_drives(app, images, tmp_path, monkeypatch):
    """A recovery bigger than the destination: tick what fits, recover, then recover the rest elsewhere."""
    import lifeboat.ui.main_window as mw
    from lifeboat.ui import dialogs, ticking

    window = mw.MainWindow()
    window.show()
    _pump(app)
    path = str(image_path("fat16"))
    info = DeviceInfo(path=path, kind="image", size=os.path.getsize(path), model="Disk image")
    window.images.append(info)
    window.sources.set_devices(window.devices + window.images, info.identity)
    window.select_source(info)
    window.quick_scan()
    _wait(app, window)
    shown = window._shown_files()
    everything = {id(n) for n in shown}
    assert len(shown) >= 4

    # Halves: disjoint, together everything, and each one stretch of the drive.
    window._tick_first_half()
    _wait(app, window)  # looking up where the files are runs as a job
    first = {id(n) for n in window.selection.selected_files()}
    window._tick_second_half()
    assert window.job is None  # positions are remembered
    second = {id(n) for n in window.selection.selected_files()}
    assert first and second and not first & second and first | second == everything
    pos = window._positions
    assert max(pos[i] for i in first) <= min(pos[i] for i in second if pos[i] >= 0)
    window._invert_ticks()
    assert {id(n) for n in window.selection.selected_files()} == first

    # Everything ticked, but the destination only has room for part of it.
    window._tick_all_shown()
    ordered = ticking.disk_order(shown, pos)
    budget = sum(ticking.size_on_disk(n) for n in ordered[: len(ordered) // 2])
    free = budget + (64 << 20)
    assert ticking.fill_budget(free) == budget
    monkeypatch.setattr(mw, "free_space", lambda folder: free)
    first_drive, second_drive = tmp_path / "drive A", tmp_path / "drive B"
    first_drive.mkdir()
    second_drive.mkdir()
    answers = [("fit", first_drive), ("ok", first_drive), ("ok", second_drive)]
    opened = []

    class ScriptedRecoverDialog:
        FIT = dialogs.RecoverDialog.FIT
        DialogCode = dialogs.RecoverDialog.DialogCode

        def __init__(self, parent, source, count, size, *rest):
            opened.append(count)
            self.answer, self.folder = answers.pop(0)
            self.fit_free = free if self.answer == "fit" else -1

        def exec(self):
            return self.FIT if self.answer == "fit" else self.DialogCode.Accepted

        def values(self):
            return {"destination": str(self.folder), "job_folder": False, "verify": True, "thoroughness": "standard",
                    "preserve_times": True, "mark_damaged": False, "resume": False}

        def deleteLater(self):
            pass

    summaries = []

    class Summary:
        def __init__(self, parent, summary, *rest):
            summaries.append(summary)
            self.show_problems = False
            self.recover_rest = len(summaries) == 1  # after the first drive: "Recover the other files"

        def exec(self):
            return 1

        def deleteLater(self):
            pass

    monkeypatch.setattr(mw, "RecoverDialog", ScriptedRecoverDialog)
    monkeypatch.setattr(mw, "SummaryDialog", Summary)
    window.recover()  # "Tick only what fits here" -> ticks what fits -> the dialog opens again -> recovery
    end = time.time() + 60
    while len(summaries) < 2 and time.time() < end:
        _pump(app, 0.05)
    assert len(summaries) == 2 and not answers
    on_a = {id(t.node) for t in summaries[0].tasks}
    on_b = {id(t.node) for t in summaries[1].tasks}
    assert on_a == {id(n) for n in ticking.fill(ordered, budget)}
    assert opened[0] == len(shown) and opened[1] == len(on_a)
    assert on_a and on_b and not on_a & on_b and on_a | on_b == everything
    assert summaries[0].outcome == "success" and summaries[1].outcome == "success"
    # Both recoveries count: nothing is left to recover, and the results column knows both.
    window._tick_not_recovered()
    assert window.selection.count == 0
    assert all(window.list_model.results[i].status == "ok" for i in everything)
    window.close()


def test_settings_window_saves_restores_and_applies(app):
    import lifeboat.ui.main_window as mw
    from lifeboat.ui.dialogs import DEFAULTS, pref, settings
    from lifeboat.ui.settings_dialog import SettingsDialog

    settings().clear()
    dialog = SettingsDialog(None)
    assert dialog.values() == {k: v for k, v in DEFAULTS.items()}  # every setting has a control
    dialog.combos["map_animation"].setCurrentIndex(2)       # Off
    dialog.combos["text_size"].setCurrentIndex(1)           # Large
    dialog.spins["read_timeout"].setValue(42)
    dialog.spins["wake_seconds"].setValue(90)
    dialog.spins["slow_seconds"].setValue(7.5)
    dialog.checks["keep_awake"].setChecked(False)
    dialog.save()
    assert pref("map_animation") == "off" and pref("text_size") == "large"
    assert pref("read_timeout") == 42 and pref("wake_seconds") == 90 and pref("slow_seconds") == 7.5
    assert pref("keep_awake") is False
    again = SettingsDialog(None)
    assert again.values()["read_timeout"] == 42 and again.values()["map_animation"] == "off"
    again.restore_defaults()
    assert again.values() == {k: v for k, v in DEFAULTS.items()}
    assert pref("read_timeout") == 42  # nothing is stored until Save

    # The window applies them right away: the reading limits of the open source, the map mode.
    window = mw.MainWindow()
    image = DeviceInfo(path=str(image_path("fat16")), kind="image", size=0, model="Disk image")
    if os.path.exists(image.path):
        image.size = os.path.getsize(image.path)
        window.select_source(image)
        assert window.reader.policy.timeout == 42 and window.reader.policy.wake_seconds == 90
        assert window.reader.policy.slow_seconds == 7.5
    assert window.diskmap.mode == "off"
    settings().setValue("read_timeout", 9)
    settings().setValue("map_animation", "light")
    window.apply_settings()
    assert window.diskmap.mode == "light"
    if window.reader is not None:
        assert window.reader.policy.timeout == 9
    window.close()
    settings().clear()
    from lifeboat.ui.dialogs import apply_appearance

    apply_appearance(app)  # back to the defaults for the other tests


def test_disk_map_animates_only_while_something_moves(app):
    from lifeboat.rescue.sectormap import SectorMap, State
    from lifeboat.ui.widgets import DiskMap

    sector_map = SectorMap(1 << 30)
    disk_map = DiskMap()
    disk_map.resize(600, 200)
    disk_map.set_map(sector_map)
    assert not disk_map.animating  # not on screen: nothing runs
    disk_map.show()
    _pump(app, 0.1)
    assert disk_map._cells and not disk_map.animating  # nothing changed yet
    sector_map.set(0, 1 << 28, State.GOOD)
    disk_map.refresh()
    assert disk_map._fades and disk_map.animating  # changed blocks fade in
    _wait_for(app, lambda: not disk_map.animating, timeout=3)  # and then it stops on its own
    assert disk_map.frames_drawn > 3

    disk_map.set_head(1 << 28)
    _pump(app, 0.3)
    assert disk_map.animating  # the block being read glows while a job runs
    disk_map.window().showMinimized()
    _pump(app, 0.2)
    disk_map.window().showNormal()
    disk_map.set_head(None)
    _wait_for(app, lambda: not disk_map.animating, timeout=3)

    disk_map.set_mode("off")
    sector_map.set(1 << 28, 1 << 29, State.BAD)
    disk_map.refresh()
    disk_map.set_head(1 << 29)
    assert not disk_map.animating and not disk_map._fades  # off: no frames at all
    disk_map.set_head(None)

    text = disk_map.describe(0)
    assert text.startswith("Block 1 of") and "Read OK" in text
    last = disk_map.describe(len(disk_map._cells) - 1)
    assert "Not read yet" in last
    assert disk_map.block_bytes() == (1 << 30) // len(disk_map._cells)
    disk_map.close()


def test_job_panel_shows_progress(app):
    from lifeboat.events import Progress
    from lifeboat.ui.widgets import JobPanel

    panel = JobPanel()
    panel.start("Deep scan of Disk 1", "radar")
    assert panel.isVisibleTo(None) or not panel.isHidden()
    panel.update_progress(Progress("Reading the whole drive", done=55, total=100, rate=148e6, eta=3600,
                                   bad_bytes=1 << 20, pass_index=1, pass_count=3, items_done=3, items_total=9))
    assert panel.percent.text() == "55.0%"
    assert panel.phase.text() == "Pass 1 of 3: Reading the whole drive"
    assert panel.left.value.text() == "1 h 00 min"
    assert panel.bad.value.text() == "1.00 MB" and panel.items.value.text() == "3 of 9"
    assert len(panel.graph.values) == 1
    panel.update_progress(Progress("Reading", done=56, total=100, rate=150e6), paused=True)
    assert panel.phase.text().endswith("(paused)")
    assert len(panel.graph.values) == 1  # one speed sample a second
    panel.finish()
    assert panel.isHidden()


def test_unlock_an_encrypted_mac_volume(app, disks, monkeypatch):
    """A scan of a drive with an encrypted Mac volume selects it and offers Unlock; a wrong password
    asks again; the right one fills the volume in, keeping the ticks of the rest."""
    if "apfs_locked" not in disks:
        pytest.skip("test disk not made yet")
    pytest.importorskip("cryptography")
    import lifeboat.ui.main_window as mw

    image, manifest = disks["apfs_locked"]
    secret = manifest["disk"].get("encrypted_volume")
    if not isinstance(secret, dict):
        pytest.skip("macOS didn't make the encrypted volume")
    answers = ["not the password", secret["password"]]
    asked: list[tuple[str, str, str]] = []

    class FakeUnlock:
        DialogCode = QtWidgets.QDialog.DialogCode

        def __init__(self, parent, name, hint="", error=""):
            asked.append((name, hint, error))

        def exec(self):
            return QtWidgets.QDialog.DialogCode.Accepted

        def password(self):
            return answers.pop(0)

        def deleteLater(self):
            pass

    monkeypatch.setattr(mw, "UnlockDialog", FakeUnlock)
    window = mw.MainWindow()
    window.show()
    _pump(app)
    info = DeviceInfo(path=str(image), kind="image", size=os.path.getsize(image), model="Disk image")
    window.images.append(info)
    window.sources.set_devices(window.devices + window.images, info.identity)
    window.select_source(info)
    window.quick_scan()
    _wait(app, window)
    locked = window._locked_volumes(window.result)
    assert len(locked) == 1 and "locked" in window.found_label.text()
    assert window.preview.node is locked[0]                 # selected after the scan, Unlock offered
    assert window.preview.unlock.isVisibleTo(window.preview) and not window.preview.button.isVisibleTo(window.preview)
    readme = next(n for n in window.result.root.walk() if n.name == "readme.txt")
    window.selection.toggle(readme, True)

    window.preview.unlock.click()                           # the wrong password first...
    _wait(app, window)
    _pump(app)                                              # ...so it asks again, with the reason
    _wait(app, window)
    _pump(app)
    assert len(asked) == 2 and not asked[0][2] and "doesn't open" in asked[1][2]
    assert not window._locked_volumes(window.result)
    assert readme.check == 2 and window.selection.count == 1
    assert secret["password"] in window.passwords and "not the password" not in window.passwords
    found = next(n for n in window.result.root.walk() if n.name == secret["file"])
    assert found.volume is not None
    window.close()


def test_plans_dialog_buttons_only_say_coming_soon(app):
    """Every trial, subscribe and buy button says "coming soon" and nothing else: no field to type
    payment details in, nothing sent. The period switch changes the prices."""
    from lifeboat import plans
    from lifeboat.ui.plans_dialog import PlansDialog

    dialog = PlansDialog(None)
    pro, tech = dialog.cards["pro"], dialog.cards["technician"]
    assert dialog.periods["yearly"].isChecked() and pro.price.text() == "$59.99"
    assert not dialog.findChildren(QtWidgets.QLineEdit)          # nowhere to type a card number
    dialog.periods["monthly"].click()
    assert pro.price.text() == "$19.99" and pro.subscribe.text() == "Subscribe"
    dialog.periods["lifetime"].click()
    assert tech.price.text() == "$599" and tech.subscribe.text() == "Buy once"
    assert dialog.cards["free"].price.text() == "$0" and dialog.cards["free"].trial is None
    assert not dialog.notice.isVisibleTo(dialog)
    pro.trial.click()
    assert dialog.notice.isVisibleTo(dialog)
    assert "Coming soon" in dialog.last_notice and f"{plans.TRIAL_DAYS}-day free trial of Pro" in dialog.last_notice
    assert "no payment details" in dialog.last_notice
    tech.subscribe.click()
    assert "Technician for life" in dialog.last_notice
    dialog.periods["yearly"].click()
    tech.subscribe.click()
    assert "Technician subscriptions" in dialog.last_notice
    dialog._license()
    assert dialog.result() == PlansDialog.LICENSE


def test_upgrade_and_trial_buttons_open_the_plans(app, monkeypatch):
    """Upgrade (header), See plans and Start free trial (welcome card), and About > See plans all
    open the plans; "Have a license key?" goes on to Settings > Account & license."""
    import lifeboat.ui.main_window as mw
    from lifeboat.ui.dialogs import AboutDialog

    opened: list[str] = []
    answers = [0, 0, mw.PlansDialog.LICENSE]

    class FakePlans:
        LICENSE = mw.PlansDialog.LICENSE

        def __init__(self, parent):
            self.notice = ""

        def show_notice(self, html):
            self.notice = html

        def exec(self):
            opened.append(self.notice)
            return answers.pop(0) if answers else 0

        def deleteLater(self):
            pass

    pages: list[str] = []

    class FakeSettings:
        DialogCode = QtWidgets.QDialog.DialogCode

        def __init__(self, parent, page=""):
            pages.append(page)

        def exec(self):
            return QtWidgets.QDialog.DialogCode.Rejected

        def deleteLater(self):
            pass

    class FakeAbout:
        PLANS = AboutDialog.PLANS

        def __init__(self, parent):
            pass

        def exec(self):
            return AboutDialog.PLANS

        def deleteLater(self):
            pass

    monkeypatch.setattr(mw, "PlansDialog", FakePlans)
    monkeypatch.setattr(mw, "SettingsDialog", FakeSettings)
    monkeypatch.setattr(mw, "AboutDialog", FakeAbout)
    window = mw.MainWindow()
    assert window.plan_badge.text() == "EARLY ACCESS"
    window.btn_plans.click()
    window.promo_trial.click()
    window.promo_plans.click()                       # answered with "Have a license key?"
    assert opened[0] == "" and "free trial of Pro" in opened[1]
    assert pages == ["Account & license"]
    window._show_about()                             # Help > About > See plans
    assert len(opened) == 4
    texts = [a.text() for a in window.btn_help.menu().actions() if a.text()]
    assert texts[0] == "What's new" and "Plans and pricing" in texts and texts[-1].startswith("About")
    window.close()


def test_new_settings_apply(app):
    """Accent color, file sizes, dates, log detail, what a scan shows first, Deep scan's choices,
    the settings search and the license box (coming soon)."""
    import logging

    from lifeboat.ui import theme
    from lifeboat.ui.dialogs import DEFAULTS, DeepScanDialog, apply_appearance, pref, settings
    from lifeboat.ui.settings_dialog import CHOICES, SettingsDialog
    from lifeboat.util import display_time, format_size, set_size_units, set_time_style

    settings().clear()
    dialog = SettingsDialog(None, "Account & license")
    assert dialog.nav.currentRow() == SettingsDialog.PAGES.index("Account & license")
    dialog._activate()
    assert dialog.notice.startswith("Coming soon")
    dialog.search.setText("accent")
    shown = [dialog.nav.item(i).text() for i in range(dialog.nav.count()) if not dialog.nav.item(i).isHidden()]
    assert shown == ["Appearance"]
    dialog.search.setText("")
    assert all(not dialog.nav.item(i).isHidden() for i in range(dialog.nav.count()))

    def choose(key, value):
        dialog.combos[key].setCurrentIndex([k for k, _label in CHOICES[key]].index(value))

    choose("accent", "ocean")
    choose("size_units", "decimal")
    choose("date_format", "mdy")
    choose("log_detail", "normal")
    choose("status_filter", "deleted")
    dialog.groups["Videos"].setChecked(False)
    dialog.checks["open_report"].setChecked(True)
    dialog.save()
    assert pref("accent") == "ocean" and pref("open_report") is True
    assert "Videos" not in str(pref("deep_groups")).split(",")
    try:
        apply_appearance(app)
        assert theme.current().accent == theme.ACCENTS["ocean"][1]
        assert format_size(1500) == "1.50 KB"                       # 1 KB = 1,000 bytes
        assert "/" in display_time(1_700_000_000) and display_time(1_700_000_000).endswith(("AM", "PM"))
        files = [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]
        assert all(h.level == logging.INFO for h in files if getattr(h, "_lifeboat", False))
        deep = DeepScanDialog(None, DeviceInfo(path="x", kind="image", size=1 << 30, model="Disk image"))
        assert not deep.groups["Videos"].isChecked() and deep.groups["Photos"].isChecked()
        deep.partitions.setChecked(False)
        deep._accept()                                              # remembered for next time
        assert pref("deep_partitions") is False
        again = SettingsDialog(None)
        assert again.values()["deep_partitions"] is False and again.values()["accent"] == "ocean"
        again.restore_defaults()
        assert again.values() == dict(DEFAULTS)
    finally:
        settings().clear()
        apply_appearance(app)
        set_size_units(False)
        set_time_style("ymd")
    assert format_size(1536) == "1.50 KB" and display_time(0).count("-") == 2


def test_status_filter_setting_applies_to_a_new_scan(app, images):
    import lifeboat.ui.main_window as mw
    from lifeboat.ui.dialogs import settings

    settings().setValue("status_filter", "existing")
    try:
        window = mw.MainWindow()
        path = str(image_path("fat16"))
        info = DeviceInfo(path=path, kind="image", size=os.path.getsize(path), model="Disk image")
        window.images.append(info)
        window.sources.set_devices(window.devices + window.images, info.identity)
        window.select_source(info)
        window.quick_scan()
        _wait(app, window)
        assert window.status_filter.currentIndex() == 1
        assert window.selection.filter.status == "existing"
        window.close()
    finally:
        settings().remove("status_filter")


def test_toggle_switch(app):
    from lifeboat.ui.widgets import Toggle

    toggle = Toggle("Keep awake")
    toggle.show()
    assert not toggle.isChecked()
    toggle.click()
    assert toggle.isChecked()
    toggle.setFocus()
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    QTest.keyClick(toggle, Qt.Key.Key_Space)
    assert not toggle.isChecked()
    toggle.setEnabled(False)
    toggle.click()
    assert not toggle.isChecked()
    _pump(app, 0.3)
    toggle.grab()                                   # paints without errors
    toggle.close()


def test_theme_change_redraws_the_icons(app):
    """Switching theme or accent while the window is open redraws its icons in the new colours
    (they used to keep the old ones until Lifeboat was restarted)."""
    import lifeboat.ui.main_window as mw
    from lifeboat.ui import icons
    from lifeboat.ui.dialogs import apply_appearance, settings

    window = mw.MainWindow()
    before = window.btn_scan.icon().pixmap(16, 16).toImage()
    try:
        settings().setValue("theme", "light")
        settings().setValue("accent", "emerald")
        window.apply_settings()
        after = window.btn_scan.icon().pixmap(16, 16).toImage()
        assert before != after
        assert window.btn_plans.property("lb_icon") == "star"
        glyphs = [w for w in window.findChildren(QtWidgets.QLabel) if w.property("lb_glyph")]
        assert glyphs
    finally:
        settings().clear()
        apply_appearance(app)
        icons.clear_cache()
    window.close()


def test_whats_new_shows_once_after_an_update(app, monkeypatch):
    """Not on a first install; once after an update; never twice for the same version."""
    from lifeboat import __version__
    from lifeboat.ui import app as app_module
    from lifeboat.ui.dialogs import WhatsNewDialog, settings
    from lifeboat.whatsnew import latest

    class Window:
        shown = 0

        def show_whats_new(self):
            self.shown += 1

    window = Window()
    settings().remove("last_seen_version")
    try:
        app_module._whats_new_once(window)             # first install: nothing
        assert window.shown == 0 and settings().value("last_seen_version") == __version__
        settings().setValue("last_seen_version", "1.1.0")
        app_module._whats_new_once(window)             # updated: shown
        app_module._whats_new_once(window)             # same version again: not shown
        assert window.shown == 1
    finally:
        settings().remove("last_seen_version")
    dialog = WhatsNewDialog(None)
    version, notes = latest()
    labels = "".join(label.text() for label in dialog.findChildren(QtWidgets.QLabel))
    assert f"What's new in Lifeboat {version}" in labels
    assert len(notes) >= 3
