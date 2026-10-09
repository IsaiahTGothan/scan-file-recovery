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


def test_window_scan_select_recover(app, images, tmp_path, monkeypatch):
    from lifeboat.ui import dialogs
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

    class FakeDialog:
        DialogCode = dialogs.RecoverDialog.DialogCode

        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return dialogs.RecoverDialog.DialogCode.Accepted

        def values(self):
            return {"destination": str(tmp_path), "job_folder": True, "verify": True,
                    "thoroughness": "standard", "preserve_times": True, "mark_damaged": False, "resume": False}

    class FakeSummary:
        def __init__(self, parent, summary):
            self.summary = summary
            self.show_problems = False

        def exec(self):
            return 1

    import lifeboat.ui.main_window as mw

    monkeypatch.setattr(mw, "RecoverDialog", FakeDialog)
    monkeypatch.setattr(mw, "SummaryDialog", FakeSummary)
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
