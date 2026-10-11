"""Choosing files to tick: halves, what fits on a drive, what isn't recovered yet.

Everything works in disk order: where each file's data starts on the source drive, which is
also the order a recovery reads the files in. A part chosen this way is one stretch of the
drive, so the parts never overlap and each recovery reads its stretch front to back. That is
how a recovery bigger than one destination drive is split across several drives.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence

from ..errors import Cancelled, DeviceGoneError, DeviceHungError
from ..fs.model import Node
from ..recover.engine import Status

CLUSTER = 4096       # a file takes whole clusters on the destination
DONE = {Status.OK, Status.PARTIAL}  # recovered; damaged files were already read as well as the drive allows


def size_on_disk(node: Node) -> int:
    return -(-node.size // CLUSTER) * CLUSTER


def fill_budget(free: int) -> int:
    """How much of ``free`` bytes to fill: the Recover dialog keeps the same safety margin."""
    return max(0, free - max(64 << 20, free // 100))


def position(node: Node) -> int:
    """Where the file's data starts on the source drive; -1 when unknown."""
    if node.volume is None or not node.size:
        return -1
    return node.volume.layout(node).first_disk_offset()


def positions_for(files: Sequence[Node], check: Callable[[], None] | None = None,
                  report: Callable[[int, int], None] | None = None) -> tuple[dict[int, int], bool]:
    """The disk position of each file (by ``id``), and whether the drive stopped answering.

    Finding a position can mean reading the drive (a FAT, a damaged record). After the first
    sign that the drive stopped answering, the remaining files get no position instead of
    waiting for the drive file after file: they are kept in folder order.
    """
    positions: dict[int, int] = {}
    stuck = False
    for index, node in enumerate(files):
        if check is not None:
            check()
        if report is not None and index % 256 == 0:
            report(index, len(files))
        if stuck:
            positions[id(node)] = -1
            continue
        try:
            positions[id(node)] = position(node)
        except Cancelled:
            raise
        except (DeviceGoneError, DeviceHungError):
            stuck = True
            positions[id(node)] = -1
        except Exception:  # noqa: BLE001 - an unknown position only changes the order
            positions[id(node)] = -1
    if report is not None:
        report(len(files), len(files))
    return positions, stuck


def disk_order(files: Iterable[Node], positions: Mapping[int, int]) -> list[Node]:
    """Files by position on the drive; files without one follow, in their original order."""
    items = list(files)
    known = [n for n in items if positions.get(id(n), -1) >= 0]
    known.sort(key=lambda n: positions[id(n)])
    return known + [n for n in items if positions.get(id(n), -1) < 0]


def halves(ordered: Sequence[Node]) -> tuple[list[Node], list[Node]]:
    """Split files (in disk order) into two parts of about the same total size."""
    total = sum(n.size for n in ordered)
    best_at, best = 0, None
    running = 0
    for at in range(len(ordered) + 1):
        gap = abs(2 * running - total)
        if best is None or gap <= best:
            best_at, best = at, gap
        if at < len(ordered):
            running += ordered[at].size
    return list(ordered[:best_at]), list(ordered[best_at:])


def fill(ordered: Iterable[Node], budget: int, cost: Callable[[Node], int] = size_on_disk) -> list[Node]:
    """The files that fit in ``budget`` bytes, taken in disk order.

    A file too big for the space left is passed over; smaller files after it still go in, so
    the drive fills up as far as possible.
    """
    chosen: list[Node] = []
    left = budget
    for node in ordered:
        need = cost(node)
        if need <= left:
            chosen.append(node)
            left -= need
    return chosen


def not_recovered(files: Iterable[Node], results: Mapping[int, object]) -> list[Node]:
    """Files no recovery in this session has recovered yet (never tried, stopped, skipped or failed)."""
    out = []
    for node in files:
        info = results.get(id(node))
        if info is None or getattr(info, "status", None) not in DONE:
            out.append(node)
    return out
