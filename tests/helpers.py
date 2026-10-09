from __future__ import annotations

from lifeboat.device.base import BlockDevice
from lifeboat.device.image import ImageDevice
from lifeboat.events import EventBus
from lifeboat.fs.content import OK, FileContentReader
from lifeboat.fs.detect import mount, probe
from lifeboat.fs.model import F, Node, Volume
from lifeboat.rescue.reader import ReadMode, ReadPolicy, RescueReader


def open_volume(path_or_device, offset: int = 0, size: int | None = None, events: EventBus | None = None,
                policy: ReadPolicy | None = None) -> Volume:
    device = path_or_device if isinstance(path_or_device, BlockDevice) else ImageDevice(path_or_device)
    reader = RescueReader(device, policy or ReadPolicy(timeout=1.0), events=events)
    found = probe(reader, offset, size or device.size - offset)
    assert found.supported, found
    volume = mount(reader, offset, found, events)
    assert volume is not None
    volume.load()  # type: ignore[attr-defined]
    return volume


def tree_index(root: Node) -> dict[str, list[Node]]:
    """Map volume-relative path -> nodes (several when names repeat, e.g. deleted + live)."""
    out: dict[str, list[Node]] = {}
    stack = [(root, "")]
    while stack:
        node, prefix = stack.pop()
        for child in node.children or ():
            path = f"{prefix}/{child.name}" if prefix else child.name
            out.setdefault(path, []).append(child)
            if child.children is not None:
                stack.append((child, path))
    return out


def read_all(volume: Volume, node: Node, modes=(ReadMode.FAST,)) -> tuple[bytes, list]:
    layout = volume.layout(node)
    reader = FileContentReader(layout, volume.reader)
    data = b""
    states = []
    for mode in modes:
        chunk = reader.read(0, layout.size, mode)
        data = bytes(chunk.data)
        states = chunk.states
    return data, states


def is_complete(states) -> bool:
    return all(st == OK for _s, _e, st in states)


def find(index: dict[str, list[Node]], path: str, deleted: bool | None = None) -> Node | None:
    for node in index.get(path, []):
        if deleted is None or bool(node.flags & F.DELETED) == deleted:
            return node
    if deleted:
        # FAT loses the first character of deleted 8.3 names ("gone.jpg" -> "_one.jpg").
        parent, _, name = path.rpartition("/")
        for candidate, nodes in index.items():
            c_parent, _, c_name = candidate.rpartition("/")
            if c_parent.lower() != parent.lower() or c_name[1:].lower() != name[1:].lower():
                continue
            for node in nodes:
                if node.flags & F.DELETED and node.flags & F.NAME_GUESSED:
                    return node
    return None
