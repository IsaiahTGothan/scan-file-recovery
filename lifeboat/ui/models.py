"""Qt models for the file browser, logs and results.

Selection rules (kept simple and predictable):

* Filters decide what is *shown*.
* Ticking a folder ticks every shown file inside it.
* Unticking a folder unticks every file inside it, shown or not.
* A folder's box is ticked when all shown files below it are ticked,
  half-ticked when some are.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from PySide6.QtCore import QAbstractItemModel, QAbstractTableModel, QModelIndex, QObject, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont

from ..events import Event, Level
from ..fs.model import F, Node
from ..recover.engine import FileTask, Status
from ..util import format_size, format_timestamp
from . import icons
from .theme import current

CATEGORIES: dict[str, set[str]] = {
    "Photos & pictures": {"jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "heic", "heif", "webp", "cr2", "cr3",
                          "nef", "arw", "dng", "orf", "rw2", "raf", "pef", "srw", "psd", "svg", "ico", "avif", "jfif"},
    "Videos": {"mp4", "mov", "avi", "mkv", "wmv", "m4v", "mts", "m2ts", "3gp", "webm", "flv", "mpg", "mpeg", "vob",
               "mxf", "braw", "r3d", "insv", "lrv"},
    "Audio": {"mp3", "wav", "flac", "aac", "m4a", "ogg", "wma", "aiff", "aif", "opus", "alac", "amr"},
    "Documents": {"doc", "docx", "xls", "xlsx", "xlsm", "ppt", "pptx", "pdf", "txt", "rtf", "odt", "ods", "odp",
                  "csv", "md", "pages", "numbers", "key", "epub", "one", "vsdx", "pub", "xps", "tex", "log",
                  "html", "htm", "xml", "json"},
    "Archives": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso", "dmg", "cab", "tgz"},
    "E-mail": {"pst", "ost", "msg", "eml", "mbox", "emlx"},
    "Databases": {"db", "sqlite", "sqlite3", "accdb", "mdb", "qbw", "qbb", "dbf"},
}
_EXT_CATEGORY = {ext: cat for cat, exts in CATEGORIES.items() for ext in exts}
_CATEGORY_ICON = {
    "Photos & pictures": "photo", "Videos": "video", "Audio": "audio", "Documents": "doc",
    "Archives": "archive", "E-mail": "mail", "Databases": "db",
}


def extension(name: str) -> str:
    _stem, dot, ext = name.rpartition(".")
    return ext.lower() if dot and _stem else ""


def category(node: Node) -> str:
    return _EXT_CATEGORY.get(extension(node.name), "Other")


@dataclass
class ViewFilter:
    text: str = ""
    category: str = "All files"
    status: str = "all"           # all / existing / deleted
    show_system: bool = False
    show_streams: bool = False
    min_size: int = 0

    @property
    def active(self) -> bool:
        return bool(self.text or self.category != "All files" or self.status != "all" or self.min_size)

    def match(self, node: Node) -> bool:
        flags = node.flags
        if not self.show_system and flags & F.SYSTEM:
            return False
        if not self.show_streams and flags & F.STREAM:
            return False
        if self.status == "existing" and flags & (F.DELETED | F.CARVED):
            return False
        if self.status == "deleted" and not flags & (F.DELETED | F.CARVED):
            return False
        if self.min_size and node.size < self.min_size:
            return False
        if self.category != "All files" and category(node) != self.category:
            return False
        if self.text:
            pattern = self.text.lower()
            name = node.name.lower()
            if any(ch in pattern for ch in "*?["):
                if not fnmatch.fnmatchcase(name, pattern):
                    return False
            elif pattern not in name:
                return False
        return True


def chance(node: Node) -> tuple[str, str]:
    flags = node.flags
    t = current()
    if flags & (F.ENCRYPTED | F.UNSUPPORTED):
        return "None", t.bad
    if flags & F.OVERWRITTEN:
        return "Poor", t.bad
    if flags & F.DELETED and flags & F.ASSUMED_CONTIGUOUS:
        return "Fair", t.warn
    if flags & F.DAMAGED_META:
        return "Fair", t.warn
    return "Good", t.ok


def status_text(node: Node) -> str:
    flags = node.flags
    if flags & F.CARVED:
        text = "Found by signature"
    elif flags & F.DELETED:
        text = "Deleted, space reused" if flags & F.OVERWRITTEN else "Deleted"
    elif flags & F.ORPHAN:
        text = "Lost (folder unknown)"
    else:
        text = "Existing"
    extras = []
    if flags & F.ENCRYPTED:
        extras.append("encrypted")
    elif flags & F.UNSUPPORTED:
        extras.append("unsupported format")
    if flags & F.COMPRESSED:
        extras.append("compressed")
    if flags & F.SPARSE:
        extras.append("sparse")
    if flags & F.DAMAGED_META:
        extras.append("damaged record")
    return f"{text} ({', '.join(extras)})" if extras else text


def tooltip(node: Node) -> str:
    flags = node.flags
    lines = [node.path()]
    if flags & F.DELETED and not flags & F.CARVED:
        lines.append("Deleted file or folder.")
    if flags & F.ASSUMED_CONTIGUOUS:
        lines.append("Its location was estimated (the filesystem cleared it on delete).")
    if flags & F.OVERWRITTEN:
        lines.append("Its space has been reused by other files, so the content is probably overwritten.")
    if flags & F.NAME_GUESSED:
        lines.append("The first letter of the name was lost when it was deleted.")
    if flags & F.ORPHAN:
        lines.append("Its original folder no longer exists.")
    if flags & F.CARVED:
        lines.append("Found by its signature; the original name and folder are unknown.")
    if flags & F.ENCRYPTED:
        lines.append("Encrypted with Windows EFS: it cannot be decrypted without the original user's key.")
    if flags & F.DAMAGED_META:
        lines.append("Its file record was partly unreadable.")
    return "\n".join(lines)


def node_icon(node: Node):
    t = current()
    deleted = bool(node.flags & F.DELETED)
    tint = t.bad if node.flags & F.OVERWRITTEN else (t.muted if deleted else t.text)
    if node.flags & F.VOLUME:
        if node.flags & F.UNSUPPORTED:
            return icons.icon("warning", t.warn)
        if node.volume is not None and node.volume.kind == "Carved":
            return icons.icon("radar", t.accent)
        return icons.icon("hdd", t.text)
    if node.flags & F.DIR:
        return icons.icon("folder", t.muted if deleted else t.info)
    if node.flags & F.ENCRYPTED:
        return icons.icon("lock", t.warn)
    return icons.icon(_CATEGORY_ICON.get(category(node), "file"), tint)


class Selection(QObject):
    """Ticked files and the per-folder counters behind the check boxes."""

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.root: Node | None = None
        self.filter = ViewFilter()
        self.count = 0
        self.bytes = 0
        self.version = 0

    def set_root(self, root: Node | None) -> None:
        self.root = root
        self.count = 0
        self.bytes = 0
        if root is not None:
            for node in root.walk():
                node.check = 0
            self.recount()
        self.changed.emit()

    def set_filter(self, view_filter: ViewFilter) -> None:
        self.filter = view_filter
        self.recount()
        self.changed.emit()

    def visible_file(self, node: Node) -> bool:
        return self.filter.match(node)

    def recount(self, top: Node | None = None) -> None:
        """Recompute folder counters (whole tree, or one subtree)."""
        start = top or self.root
        if start is None:
            return
        order = list(start.walk())
        for node in reversed(order):
            if node.children is None:
                continue
            total = 0
            selected = 0
            for child in node.children:
                if child.children is None:
                    if self.filter.match(child):
                        total += 1
                        if child.check:
                            selected += 1
                else:
                    total += child.total
                    selected += child.sel_count
            node.total = total
            node.sel_count = selected
        self.version += 1

    def state(self, node: Node) -> Qt.CheckState:
        if node.children is None:
            return Qt.CheckState.Checked if node.check else Qt.CheckState.Unchecked
        if node.sel_count == 0:
            return Qt.CheckState.Unchecked
        if node.sel_count >= node.total:
            return Qt.CheckState.Checked
        return Qt.CheckState.PartiallyChecked

    def toggle(self, node: Node, checked: bool) -> None:
        if node.children is None:
            if bool(node.check) == checked:
                return
            node.check = 2 if checked else 0
            delta = 1 if checked else -1
            self.count += delta
            self.bytes += node.size * delta
            if self.filter.match(node):
                for parent in node.ancestors():
                    parent.sel_count += delta
        else:
            before = node.sel_count
            for item in node.walk():
                if item.children is not None:
                    continue
                if checked and not item.check and self.filter.match(item):
                    item.check = 2
                    self.count += 1
                    self.bytes += item.size
                elif not checked and item.check:
                    item.check = 0
                    self.count -= 1
                    self.bytes -= item.size
            self.recount(node)
            delta = node.sel_count - before
            if delta:
                for parent in node.ancestors():
                    parent.sel_count += delta
        self.version += 1
        self.changed.emit()

    def select_all(self, checked: bool) -> None:
        if self.root is not None:
            self.toggle(self.root, checked)

    def selected_files(self) -> list[Node]:
        if self.root is None:
            return []
        return [n for n in self.root.walk() if n.children is None and n.check]

    def selected_folders(self) -> list[Node]:
        """Folders whose every shown file is ticked (recreated even when empty)."""
        if self.root is None:
            return []
        return [n for n in self.root.walk() if n.children is not None and n.parent is not None
                and n.total and n.sel_count >= n.total]

    @property
    def hidden_selected(self) -> int:
        if self.root is None:
            return 0
        return max(0, self.count - self.root.sel_count)


class FolderModel(QAbstractItemModel):
    """Folders only, for the tree on the left of the browser."""

    def __init__(self, selection: Selection) -> None:
        super().__init__()
        self.selection = selection
        self.root: Node | None = None
        self._children: dict[int, list[Node]] = {}
        self._rows: dict[int, int] = {}

    def set_root(self, root: Node | None) -> None:
        self.beginResetModel()
        self.root = root
        self._children.clear()
        self._rows.clear()
        self.endResetModel()

    def refilter(self) -> None:
        self.beginResetModel()
        self._children.clear()
        self._rows.clear()
        self.endResetModel()

    def _visible_dirs(self, node: Node) -> list[Node]:
        key = id(node)
        cached = self._children.get(key)
        if cached is None:
            active = self.selection.filter.active
            cached = [c for c in (node.children or ()) if c.children is not None
                      and (not active or c.total or c.flags & F.VOLUME)
                      and (self.selection.filter.show_system or not c.flags & F.SYSTEM)]
            if node.parent is not None:  # keep partitions in disk order; sort folders by name
                cached.sort(key=lambda n: (bool(n.flags & F.VIRTUAL), n.name.lower()))
            self._children[key] = cached
            for row, child in enumerate(cached):
                self._rows[id(child)] = row
        return cached

    def node(self, index: QModelIndex) -> Node | None:
        if not index.isValid():
            return self.root
        return index.internalPointer()  # type: ignore[no-any-return]

    def index_of(self, node: Node) -> QModelIndex:
        if self.root is None or node is self.root or node.parent is None:
            return QModelIndex()
        parent = node.parent
        siblings = self._visible_dirs(parent)
        row = self._rows.get(id(node))
        if row is None or row >= len(siblings) or siblings[row] is not node:
            return QModelIndex()
        return self.createIndex(row, 0, node)

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        node = self.node(parent)
        if node is None:
            return QModelIndex()
        children = self._visible_dirs(node)
        if 0 <= row < len(children) and column == 0:
            return self.createIndex(row, column, children[row])
        return QModelIndex()

    def parent(self, index: QModelIndex = QModelIndex()) -> QModelIndex:  # type: ignore[override]
        if not index.isValid():
            return QModelIndex()
        node: Node = index.internalPointer()
        parent = node.parent
        if parent is None or parent is self.root:
            return QModelIndex()
        return self.index_of(parent)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        node = self.node(parent)
        if node is None or node.children is None:
            return 0
        return len(self._visible_dirs(node))

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 1

    def hasChildren(self, parent: QModelIndex = QModelIndex()) -> bool:
        node = self.node(parent)
        if node is None or not node.children:
            return False
        return any(c.children is not None for c in node.children)

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        node = self.node(index)
        if node is not None and not node.flags & F.UNSUPPORTED:
            base |= Qt.ItemFlag.ItemIsUserCheckable
        return base

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        node = self.node(index)
        if node is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            if node.flags & F.VOLUME or not self.selection.filter.active:
                return node.name or "(root)"
            return f"{node.name}  ({node.total:,})"
        if role == Qt.ItemDataRole.DecorationRole:
            return node_icon(node)
        if role == Qt.ItemDataRole.CheckStateRole:
            return self.selection.state(node)
        if role == Qt.ItemDataRole.ForegroundRole and node.flags & F.DELETED:
            return QBrush(QColor(current().muted))
        if role == Qt.ItemDataRole.ToolTipRole:
            return tooltip(node)
        if role == Qt.ItemDataRole.FontRole and node.flags & F.VOLUME:
            font = QFont()
            font.setBold(True)
            return font
        return None

    def setData(self, index: QModelIndex, value: object, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if role != Qt.ItemDataRole.CheckStateRole:
            return False
        node = self.node(index)
        if node is None:
            return False
        state = Qt.CheckState(value) if not isinstance(value, Qt.CheckState) else value
        self.selection.toggle(node, state != Qt.CheckState.Unchecked)
        return True


LIST_COLUMNS = ["Name", "Size", "Modified", "Created", "Status", "Chance", "Result", "Location"]


@dataclass
class ResultInfo:
    status: str
    message: str = ""


class FileListModel(QAbstractTableModel):
    """Contents of one folder, or a flat list of search results."""

    def __init__(self, selection: Selection) -> None:
        super().__init__()
        self.selection = selection
        self.folder: Node | None = None
        self.rows: list[Node] = []
        self.search_mode = False
        self.results: dict[int, ResultInfo] = {}
        self._sort = (0, Qt.SortOrder.AscendingOrder)

    def set_folder(self, folder: Node | None) -> None:
        self.beginResetModel()
        self.folder = folder
        self.search_mode = False
        self.rows = self._folder_rows(folder) if folder is not None else []
        self._apply_sort()
        self.endResetModel()

    def set_results(self, nodes: list[Node]) -> None:
        self.beginResetModel()
        self.folder = None
        self.search_mode = True
        self.rows = nodes
        self._apply_sort()
        self.endResetModel()

    def refresh(self) -> None:
        if self.search_mode:
            self.layoutChanged.emit()
        else:
            self.set_folder(self.folder)

    def _folder_rows(self, folder: Node) -> list[Node]:
        out = []
        active = self.selection.filter.active
        flt = self.selection.filter
        for child in folder.children or ():
            if child.children is not None:
                if child.flags & F.SYSTEM and not flt.show_system:
                    continue
                if active and not child.total and not child.flags & F.VOLUME:
                    continue
                out.append(child)
            elif flt.match(child):
                out.append(child)
        return out

    def node(self, index: QModelIndex) -> Node | None:
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        return self.rows[index.row()]

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(LIST_COLUMNS) if self.search_mode else len(LIST_COLUMNS) - 1

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return LIST_COLUMNS[section]
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        node = self.node(index)
        if index.column() == 0 and node is not None and not node.flags & F.UNSUPPORTED:
            base |= Qt.ItemFlag.ItemIsUserCheckable
        return base

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        node = self.node(index)
        if node is None:
            return None
        col = index.column()
        is_dir = node.children is not None
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return node.name
            if col == 1:
                return "" if is_dir else format_size(node.size)
            if col == 2:
                return format_timestamp(node.mtime)
            if col == 3:
                return format_timestamp(node.ctime)
            if col == 4:
                return "Folder" if is_dir and not node.flags & F.DELETED else status_text(node)
            if col == 5:
                return "" if is_dir else chance(node)[0]
            if col == 6:
                info = self.results.get(id(node))
                return Status.SHORT.get(info.status, "") if info else ""
            if col == 7:
                return "/".join(node.path_parts()[:-1])
        if role == Qt.ItemDataRole.CheckStateRole and col == 0:
            return self.selection.state(node)
        if role == Qt.ItemDataRole.DecorationRole and col == 0:
            return node_icon(node)
        if role == Qt.ItemDataRole.ForegroundRole:
            t = current()
            if col == 5 and not is_dir:
                return QBrush(QColor(chance(node)[1]))
            if col == 6:
                info = self.results.get(id(node))
                if info is not None:
                    colour = {Status.OK: t.ok, Status.PARTIAL: t.warn, Status.FAILED: t.bad}.get(info.status, t.muted)
                    return QBrush(QColor(colour))
            if node.flags & F.DELETED:
                return QBrush(QColor(t.muted))
        if role == Qt.ItemDataRole.TextAlignmentRole and col == 1:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            info = self.results.get(id(node))
            extra = f"\nLast recovery: {Status.LABELS.get(info.status, '')} {info.message}" if info else ""
            return tooltip(node) + extra
        return None

    def setData(self, index: QModelIndex, value: object, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if role != Qt.ItemDataRole.CheckStateRole or index.column() != 0:
            return False
        node = self.node(index)
        if node is None:
            return False
        state = Qt.CheckState(value) if not isinstance(value, Qt.CheckState) else value
        self.selection.toggle(node, state != Qt.CheckState.Unchecked)
        return True

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        self._sort = (column, order)
        self.layoutAboutToBeChanged.emit()
        self._apply_sort()
        self.layoutChanged.emit()

    def _apply_sort(self) -> None:
        column, order = self._sort
        reverse = order == Qt.SortOrder.DescendingOrder
        keys: dict[int, Callable[[Node], object]] = {
            0: lambda n: n.name.lower(),
            1: lambda n: n.size,
            2: lambda n: n.mtime or 0.0,
            3: lambda n: n.ctime or 0.0,
            4: lambda n: status_text(n),
            5: lambda n: chance(n)[0],
            6: lambda n: self.results[id(n)].status if id(n) in self.results else "",
            7: lambda n: n.path(),
        }
        key = keys.get(column, keys[0])
        dirs = [n for n in self.rows if n.children is not None]
        files = [n for n in self.rows if n.children is None]
        dirs.sort(key=lambda n: n.name.lower())
        files.sort(key=key, reverse=reverse)  # type: ignore[arg-type, return-value]
        self.rows = dirs + files


class EventModel(QAbstractTableModel):
    """Activity log (all events) or problem list (warnings and errors)."""

    HEADERS = ["Time", "", "Message", "Code"]

    def __init__(self, min_level: Level = Level.INFO, limit: int = 20000) -> None:
        super().__init__()
        self.min_level = min_level
        self.limit = limit
        self.events: list[Event] = []

    def add(self, event: Event) -> bool:
        if event.level < self.min_level:
            return False
        if len(self.events) >= self.limit:
            self.beginRemoveRows(QModelIndex(), 0, 999)
            del self.events[:1000]
            self.endRemoveRows()
        row = len(self.events)
        self.beginInsertRows(QModelIndex(), row, row)
        self.events.append(event)
        self.endInsertRows()
        return True

    def clear(self) -> None:
        self.beginResetModel()
        self.events.clear()
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.events)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 4

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        event = self.events[index.row()]
        col = index.column()
        t = current()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                import time as _time

                return _time.strftime("%H:%M:%S", _time.localtime(event.timestamp))
            if col == 2:
                return event.message
            if col == 3:
                return event.code
        if role == Qt.ItemDataRole.DecorationRole and col == 1:
            return level_icon(event.level)
        if role == Qt.ItemDataRole.ToolTipRole:
            parts = [event.message]
            if event.title:
                parts.append(f"{event.code}: {event.title}")
            if event.hint:
                parts.append(event.hint)
            if event.details:
                parts.append(event.details)
            return "\n".join(parts)
        if role == Qt.ItemDataRole.ForegroundRole and col == 2:
            if event.level >= Level.ERROR:
                return QBrush(QColor(t.bad))
            if event.level == Level.WARNING:
                return QBrush(QColor(t.warn))
            if event.level == Level.SUCCESS:
                return QBrush(QColor(t.ok))
        return None


def level_icon(level: Level):
    t = current()
    if level >= Level.ERROR:
        return icons.icon("error", t.bad)
    if level == Level.WARNING:
        return icons.icon("warning", t.warn)
    if level == Level.SUCCESS:
        return icons.icon("success", t.ok)
    return icons.icon("info", t.info)


class ResultsModel(QAbstractTableModel):
    HEADERS = ["Status", "Original location", "Saved as", "Size", "Details"]

    def __init__(self) -> None:
        super().__init__()
        self.all_tasks: list[FileTask] = []
        self.tasks: list[FileTask] = []
        self.mode = "problems"

    def set_tasks(self, tasks: Iterable[FileTask]) -> None:
        self.beginResetModel()
        self.all_tasks = list(tasks)
        self._filter()
        self.endResetModel()

    def set_mode(self, mode: str) -> None:
        self.beginResetModel()
        self.mode = mode
        self._filter()
        self.endResetModel()

    def _filter(self) -> None:
        if self.mode == "problems":
            self.tasks = [t for t in self.all_tasks if t.status not in (Status.OK,)]
        elif self.mode == "ok":
            self.tasks = [t for t in self.all_tasks if t.status == Status.OK]
        else:
            self.tasks = list(self.all_tasks)
        order = {Status.FAILED: 0, Status.PARTIAL: 1, Status.SKIPPED: 2, Status.PENDING: 3, Status.OK: 4}
        self.tasks.sort(key=lambda t: (order.get(t.status, 5), t.source_path.lower()))

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.tasks)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        task = self.tasks[index.row()]
        col = index.column()
        t = current()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return Status.SHORT.get(task.status, task.status)
            if col == 1:
                return task.source_path
            if col == 2:
                return task.rel_path if task.status in (Status.OK, Status.PARTIAL) else ""
            if col == 3:
                return format_size(task.size)
            if col == 4:
                return "; ".join([x for x in [task.message, *task.notes] if x])
        if role == Qt.ItemDataRole.ForegroundRole and col == 0:
            colour = {Status.OK: t.ok, Status.PARTIAL: t.warn, Status.FAILED: t.bad}.get(task.status, t.muted)
            return QBrush(QColor(colour))
        if role == Qt.ItemDataRole.DecorationRole and col == 0:
            name = {Status.OK: ("success", t.ok), Status.PARTIAL: ("warning", t.warn),
                    Status.FAILED: ("error", t.bad)}.get(task.status, ("info", t.muted))
            return icons.icon(*name)
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{task.source_path}\n{task.dest}\n{task.message}"
        return None

    def task(self, index: QModelIndex) -> FileTask | None:
        if not index.isValid():
            return None
        return self.tasks[index.row()]


def open_in_explorer(path: str) -> None:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    if os.name == "nt" and os.path.isfile(path):
        import subprocess

        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        return
    QDesktopServices.openUrl(QUrl.fromLocalFile(path if os.path.isdir(path) else os.path.dirname(path)))


__all__ = [
    "CATEGORIES",
    "EventModel",
    "FileListModel",
    "FolderModel",
    "ResultsModel",
    "Selection",
    "ViewFilter",
    "category",
]
