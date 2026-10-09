"""Small LRU page cache for metadata reads (directory blocks, FAT pages...)."""

from __future__ import annotations

import threading
from collections import OrderedDict

PAGE = 4096


class PageCache:
    def __init__(self, capacity_bytes: int = 32 << 20) -> None:
        self.capacity = max(0, capacity_bytes // PAGE)
        self._pages: OrderedDict[int, bytes] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, start: int, end: int) -> bytes | None:
        if self.capacity == 0 or end <= start:
            return None
        first = start // PAGE
        last = (end - 1) // PAGE
        with self._lock:
            pieces = []
            for page in range(first, last + 1):
                data = self._pages.get(page)
                if data is None:
                    self.misses += 1
                    return None
                self._pages.move_to_end(page)
                pieces.append(data)
            self.hits += 1
        blob = pieces[0] if len(pieces) == 1 else b"".join(pieces)
        offset = start - first * PAGE
        return blob[offset:offset + (end - start)]

    def put(self, start: int, data: bytes) -> None:
        if self.capacity == 0:
            return
        end = start + len(data)
        first = -(-start // PAGE)
        last = end // PAGE  # exclusive
        if first >= last:
            return
        with self._lock:
            for page in range(first, last):
                offset = page * PAGE - start
                self._pages[page] = data[offset:offset + PAGE]
                self._pages.move_to_end(page)
            while len(self._pages) > self.capacity:
                self._pages.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._pages.clear()
