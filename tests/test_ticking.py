"""Choosing files to tick: disk order, halves, what fits, what isn't recovered yet."""

from __future__ import annotations

import os

import pytest

from lifeboat.errors import Cancelled, DeviceGoneError
from lifeboat.fs.model import Extent, F, FileLayout, Node
from lifeboat.recover.engine import Status
from lifeboat.ui import ticking


class FakeVolume:
    """Files sit at the offset given in ``ref``; ``fail_at`` makes one lookup fail like a dying drive."""

    def __init__(self, fail_at: int | None = None, error: type[Exception] = DeviceGoneError) -> None:
        self.calls = 0
        self.fail_at = fail_at
        self.error = error

    def layout(self, node: Node) -> FileLayout:
        self.calls += 1
        if self.fail_at is not None and self.calls == self.fail_at:
            raise self.error("gone")
        return FileLayout(node.size, [Extent(0, node.size, node.ref)])


def files(*specs, volume=None):
    """specs: (name, size, disk offset)."""
    volume = volume or FakeVolume()
    return [Node(name, 0, size, volume=volume, ref=offset) for name, size, offset in specs]


def test_disk_order_puts_unknown_positions_last_in_their_order():
    a, b, c, d = files(("a", 10, 300), ("b", 10, 100), ("c", 10, 0), ("d", 10, 200))
    positions = {id(a): 300, id(b): 100, id(c): -1, id(d): 200}
    assert ticking.disk_order([a, b, c, d], positions) == [b, d, a, c]


def test_halves_are_about_the_same_size_and_cover_everything():
    nodes = files(("a", 5, 0), ("b", 1, 10), ("c", 1, 20), ("d", 1, 30), ("e", 2, 40))
    first, second = ticking.halves(nodes)
    assert [n.name for n in first] == ["a"] and [n.name for n in second] == ["b", "c", "d", "e"]
    big = files(*[(f"f{i}", 1000 + i, i * 10_000) for i in range(101)])
    first, second = ticking.halves(big)
    assert first + second == big
    assert abs(sum(n.size for n in first) - sum(n.size for n in second)) <= 1100
    one = files(("only", 7, 0))
    assert ticking.halves(one) == (one, [])
    assert ticking.halves([]) == ([], [])


def test_fill_takes_files_in_disk_order_and_passes_over_files_that_dont_fit():
    nodes = files(("a", 4096, 0), ("big", 10 * 4096, 1), ("b", 4096, 2), ("c", 1, 3))
    chosen = ticking.fill(nodes, 3 * 4096)
    assert [n.name for n in chosen] == ["a", "b", "c"]  # 1 byte takes a whole cluster
    assert ticking.fill(nodes, 0) == []
    assert ticking.size_on_disk(Node("x", 0, 4097)) == 8192


def test_fill_budget_keeps_the_recover_dialogs_margin():
    from lifeboat.recover import check_destination  # noqa: F401 - same rule as the dialog

    assert ticking.fill_budget(10 << 30) == (10 << 30) - (10 << 30) // 100
    assert ticking.fill_budget(1 << 30) == (1 << 30) - (64 << 20)
    assert ticking.fill_budget(10 << 20) == 0
    assert ticking.fill_budget(-1) == 0


def test_positions_stop_asking_a_drive_that_stopped_answering():
    volume = FakeVolume(fail_at=2)
    nodes = files(("a", 10, 500), ("b", 10, 400), ("c", 10, 300), ("d", 10, 200), volume=volume)
    reports = []
    positions, stuck = ticking.positions_for(nodes, report=lambda i, n: reports.append((i, n)))
    assert stuck
    assert [positions[id(n)] for n in nodes] == [500, -1, -1, -1]
    assert volume.calls == 2  # not asked again after the drive stopped answering
    assert reports[-1] == (4, 4)


def test_positions_survive_odd_files_and_honour_stop():
    volume = FakeVolume(fail_at=1, error=ValueError)
    nodes = files(("a", 10, 500), ("b", 10, 400), volume=volume)
    empty = Node("empty", 0, 0, volume=volume, ref=0)
    positions, stuck = ticking.positions_for([*nodes, empty])
    assert not stuck and positions[id(nodes[0])] == -1 and positions[id(nodes[1])] == 400
    assert positions[id(empty)] == -1

    def stop():
        raise Cancelled()

    with pytest.raises(Cancelled):
        ticking.positions_for(nodes, check=stop)


def test_not_recovered_counts_damaged_files_as_done():
    a, b, c, d = files(("a", 1, 0), ("b", 1, 1), ("c", 1, 2), ("d", 1, 3))

    class Info:
        def __init__(self, status):
            self.status = status

    results = {id(a): Info(Status.OK), id(b): Info(Status.PARTIAL), id(c): Info(Status.FAILED)}
    assert ticking.not_recovered([a, b, c, d], results) == [c, d]


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    return widgets.QApplication.instance() or widgets.QApplication([])


def test_selection_apply_ticks(qt_app):
    from lifeboat.ui.models import Selection, ViewFilter

    root = Node("root", F.DIR)
    photos = root.add(Node("Photos", F.DIR))
    docs = root.add(Node("Docs", F.DIR))
    jpgs = [photos.add(Node(f"{i}.jpg", 0, 100)) for i in range(3)]
    txt = docs.add(Node("a.txt", 0, 50))
    selection = Selection()
    selection.set_root(root)
    changes = []
    selection.changed.connect(lambda: changes.append(1))
    selection.apply_ticks(tick=jpgs[:2])
    assert selection.count == 2 and selection.bytes == 200 and len(changes) == 1
    assert photos.sel_count == 2 and root.sel_count == 2
    selection.apply_ticks(tick=[txt], untick=[jpgs[0]])
    assert selection.count == 2 and {n.name for n in selection.selected_files()} == {"1.jpg", "a.txt"}
    selection.apply_ticks(tick=[jpgs[2]], only=True)
    assert [n.name for n in selection.selected_files()] == ["2.jpg"] and selection.bytes == 100
    # Filters decide what is shown; ticks outside the filter are kept and counted as hidden.
    selection.apply_ticks(tick=[txt])
    selection.set_filter(ViewFilter(category="Photos & pictures"))
    assert selection.shown_files() == jpgs and selection.hidden_selected == 1
    assert len(selection.files()) == 4
