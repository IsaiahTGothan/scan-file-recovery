"""Exception hierarchy and the catalogue of user-facing error codes.

Every problem Lifeboat reports carries a stable code (``LB-xxx``) so that a
message in the log, the error list, the recovery report and the
notification popups can all be matched up.  ``describe(code)`` returns the
plain-language explanation and the suggested next step.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    title: str
    hint: str


_CATALOGUE: dict[str, ErrorInfo] = {}


def _register(code: str, title: str, hint: str) -> str:
    _CATALOGUE[code] = ErrorInfo(code, title, hint)
    return code


# --- Source drive -----------------------------------------------------------
E_OPEN_DENIED = _register(
    "LB-101",
    "Access to the drive was denied",
    "Run Lifeboat as administrator. Raw disk access needs administrator rights.",
)
E_OPEN_FAILED = _register(
    "LB-102",
    "The drive or image could not be opened",
    "Check that the drive is still connected and appears in Disk Management.",
)
E_READ_BAD = _register(
    "LB-110",
    "Unreadable sectors on the source drive",
    "Lifeboat skipped this area and will come back to it after the readable "
    "data is safe. Nothing else needs to be done.",
)
E_READ_TIMEOUT = _register(
    "LB-111",
    "The drive took too long to answer a read",
    "Slow areas are skipped on the first pass and retried at the end.",
)
E_SOURCE_GONE = _register(
    "LB-120",
    "The source drive disconnected",
    "Reconnect the drive (try another USB port or cable). Lifeboat resumes "
    "automatically as soon as the same drive is detected again.",
)
E_SOURCE_HUNG = _register(
    "LB-121",
    "The source drive stopped responding",
    "Wait a minute. If it does not recover, unplug the drive, wait 10 seconds "
    "and reconnect it; Lifeboat resumes where it stopped.",
)
E_SOURCE_CHANGED = _register(
    "LB-122",
    "A different drive was connected",
    "The reconnected drive does not match the one being recovered. Connect the "
    "original drive.",
)

# --- Partitions / filesystems -------------------------------------------------
E_NO_PARTITIONS = _register(
    "LB-201",
    "No partition table or filesystem was found",
    "Run a Deep Scan to search for lost partitions and files by signature.",
)
E_FS_UNSUPPORTED = _register(
    "LB-202",
    "Unsupported filesystem",
    "Lifeboat cannot browse this filesystem. A Deep Scan can still recover "
    "common file types by signature.",
)
E_BOOT_BACKUP = _register(
    "LB-203",
    "Boot sector damaged, used the backup copy",
    "No action needed. This is common on failing drives.",
)
E_META_UNREADABLE = _register(
    "LB-204",
    "Part of the file table could not be read",
    "Files described by the unreadable area may be missing or appear in "
    "'Lost files'. A Deep Scan can find more.",
)
E_BITLOCKER = _register(
    "LB-205",
    "BitLocker-encrypted volume",
    "Unlock the volume in Windows with its password or recovery key, then "
    "select its drive letter in Lifeboat's source list to read decrypted data.",
)
E_META_CORRUPT = _register(
    "LB-206",
    "Filesystem metadata is corrupt",
    "Lifeboat recovered what it could. Results in this area may be incomplete.",
)
E_FS_REGION_OUTSIDE = _register(
    "LB-207",
    "A partition extends past the end of the drive",
    "The drive may be reporting a wrong size (common with failing USB "
    "adapters). Try connecting it with a different adapter.",
)

# --- Destination --------------------------------------------------------------
E_DEST_ON_SOURCE = _register(
    "LB-301",
    "The destination is on the drive being recovered",
    "Never write to a failing drive. Choose a folder on a different, healthy drive.",
)
E_DEST_SPACE = _register(
    "LB-302",
    "Not enough free space on the destination",
    "Choose a larger destination drive or select fewer files.",
)
E_DEST_FULL = _register(
    "LB-303",
    "The destination drive is full",
    "Free up space or choose another destination, then press Resume.",
)
E_DEST_GONE = _register(
    "LB-304",
    "The destination is not available",
    "Reconnect the destination drive, then press Resume.",
)
E_DEST_FAT32 = _register(
    "LB-305",
    "File too large for a FAT32 destination",
    "FAT32 cannot store files of 4 GB or more. Use an NTFS or exFAT destination.",
)
E_DEST_WRITE = _register(
    "LB-306",
    "Writing to the destination failed",
    "Check the destination drive. Lifeboat retries the file once automatically.",
)
E_VERIFY = _register(
    "LB-307",
    "Verification failed: the copy does not match what was read",
    "The destination drive may be unreliable. Use a different destination.",
)
E_DEST_NOT_WRITABLE = _register(
    "LB-308",
    "The destination folder is not writable",
    "Choose another folder or check its permissions.",
)

# --- Per-file outcomes -------------------------------------------------------------
E_FILE_PARTIAL = _register(
    "LB-401",
    "File recovered with unreadable parts",
    "The unreadable parts were filled with zeros. Many file types still open; "
    "the report lists the damaged byte ranges.",
)
E_FILE_LOST = _register(
    "LB-402",
    "File could not be recovered",
    "None of the file's data could be read, or its location is unknown.",
)
E_FILE_ENCRYPTED = _register(
    "LB-403",
    "File is encrypted with Windows EFS",
    "EFS files can only be decrypted with the original user's certificate. "
    "The raw encrypted data was not copied.",
)
E_FILE_OVERWRITTEN = _register(
    "LB-404",
    "Deleted file whose space was reused",
    "Its data was probably overwritten by newer files; the copy may be garbage.",
)
E_FILE_UNSUPPORTED = _register(
    "LB-405",
    "File uses a storage format Lifeboat cannot decode",
    "The file was skipped. It is listed in the report.",
)

# --- Internal --------------------------------------------------------------------
E_INTERNAL = _register(
    "LB-500",
    "Unexpected internal error",
    "Lifeboat kept running. Please keep the log file (Help > Open log folder).",
)
E_CANCELLED = _register("LB-900", "Stopped by user", "")


def describe(code: str) -> ErrorInfo:
    """Return the catalogue entry for ``code`` (unknown codes get a generic one)."""
    info = _CATALOGUE.get(code)
    if info is None:
        return ErrorInfo(code, "Error", "")
    return info


def all_codes() -> list[ErrorInfo]:
    return sorted(_CATALOGUE.values(), key=lambda item: item.code)


class LifeboatError(Exception):
    """Base class for errors that are reported to the user."""

    default_code = E_INTERNAL

    def __init__(self, message: str, *, code: str | None = None, details: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.details = details

    def __str__(self) -> str:
        return self.message


class Cancelled(LifeboatError):
    default_code = E_CANCELLED

    def __init__(self, message: str = "Stopped by user") -> None:
        super().__init__(message)


class DeviceError(LifeboatError):
    default_code = E_OPEN_FAILED


class DeviceOpenError(DeviceError):
    default_code = E_OPEN_FAILED


class ReadErrorKind(enum.Enum):
    MEDIA = "media"            # bad sector / CRC / unrecovered read error
    TIMEOUT = "timeout"        # no answer within the time limit
    GONE = "gone"              # device disconnected
    OUT_OF_RANGE = "range"     # request past the end of the device
    OTHER = "other"


class ReadError(DeviceError):
    """A single device read failed.  ``kind`` tells the rescue logic what to do."""

    default_code = E_READ_BAD

    def __init__(
        self,
        message: str,
        *,
        kind: ReadErrorKind = ReadErrorKind.MEDIA,
        offset: int = 0,
        length: int = 0,
        os_code: int | None = None,
    ) -> None:
        code = E_READ_TIMEOUT if kind is ReadErrorKind.TIMEOUT else E_READ_BAD
        if kind is ReadErrorKind.GONE:
            code = E_SOURCE_GONE
        super().__init__(message, code=code)
        self.kind = kind
        self.offset = offset
        self.length = length
        self.os_code = os_code


class DeviceGoneError(DeviceError):
    default_code = E_SOURCE_GONE


class DeviceHungError(DeviceError):
    default_code = E_SOURCE_HUNG


class DestinationError(LifeboatError):
    default_code = E_DEST_WRITE


class DestinationFullError(DestinationError):
    default_code = E_DEST_FULL


class DestinationGoneError(DestinationError):
    default_code = E_DEST_GONE


class FilesystemError(LifeboatError):
    default_code = E_META_CORRUPT
