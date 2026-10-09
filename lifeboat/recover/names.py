"""Make every recovered name valid and unique on Windows.

Source filesystems allow names Windows Explorer cannot handle: characters
like ``:`` or ``?`` (NTFS POSIX namespace, Mac/Linux files), names ending in
a dot or space, reserved device names (``CON``, ``aux.txt``), names over 255
characters, and names that differ only in letter case.  Each is turned
into a safe, visible equivalent, and the original is kept in the report.
"""

from __future__ import annotations

INVALID_CHARS = set('<>:"/\\|?*') | {chr(i) for i in range(32)}
RESERVED = {
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
    "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³",
}
MAX_NAME = 255


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def sanitize(name: str) -> str:
    """Return a name that Windows (and exFAT/FAT destinations) accept."""
    out = "".join("_" if ch in INVALID_CHARS else ch for ch in name)
    # Lone surrogates (from damaged UTF-16 names) cannot be written.
    out = out.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    stripped = out.rstrip(" .")
    if stripped != out:
        out = stripped + "_" * (len(out) - len(stripped))
    if not out or out in (".", ".."):
        out = "_"
    stem = out.split(".", 1)[0].rstrip(" ")
    if stem.upper() in RESERVED:
        out = "_" + out
    if _utf16_len(out) > MAX_NAME:
        out = _truncate(out)
    return out


def _truncate(name: str) -> str:
    stem, dot, ext = name.rpartition(".")
    if not dot or len(ext) > 16 or not stem:
        stem, ext = name, ""
    suffix = f".{ext}" if ext else ""
    budget = MAX_NAME - _utf16_len(suffix) - 1
    while _utf16_len(stem) > budget:
        stem = stem[:-1]
    return f"{stem}~{suffix}"


def with_tag(name: str, tag: str) -> str:
    """``name`` with ``tag`` (e.g. " (2)") before its extension, within the 255-character limit."""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or len(ext) > 16:
        stem, ext = name, ""
    suffix = f".{ext}" if ext else ""
    while stem and _utf16_len(f"{stem}{tag}{suffix}") > MAX_NAME:
        stem = stem[:-1]
    return f"{stem}{tag}{suffix}"


class NameSpace:
    """Unique, case-insensitive names within one destination folder."""

    def __init__(self) -> None:
        self._used: dict[str, set[str]] = {}

    def claim(self, folder: str, name: str, *, deleted: bool = False) -> str:
        used = self._used.setdefault(folder.casefold(), set())
        candidate = name
        if candidate.casefold() in used:
            stem, dot, ext = name.rpartition(".")
            if not dot or not stem:
                stem, ext = name, ""
            suffix = f".{ext}" if ext else ""
            label = "deleted" if deleted else ""
            counter = 1
            while True:
                if label:
                    tag = f" ({label})" if counter == 1 else f" ({label} {counter})"
                else:
                    tag = f" ({counter + 1})"
                short_stem = stem
                while short_stem and _utf16_len(f"{short_stem}{tag}{suffix}") > MAX_NAME:
                    short_stem = short_stem[:-1]
                candidate = f"{short_stem}{tag}{suffix}"
                if candidate.casefold() not in used:
                    break
                counter += 1
        used.add(candidate.casefold())
        return candidate

    def taken(self, folder: str, name: str) -> bool:
        return name.casefold() in self._used.get(folder.casefold(), ())

    def reserve(self, folder: str, name: str) -> None:
        self._used.setdefault(folder.casefold(), set()).add(name.casefold())
