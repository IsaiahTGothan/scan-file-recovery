"""Small helpers: size/time formatting and on-disk timestamp conversions."""

from __future__ import annotations

import datetime as _dt
import math

_FILETIME_EPOCH_DELTA = 116444736000000000  # 100 ns ticks from 1601-01-01 to 1970-01-01
_MIN_TS = -2208988800.0  # 1900-01-01
_MAX_TS = 7258118400.0   # 2200-01-01


def format_size(num_bytes: int | float, *, exact: bool = False) -> str:
    """Format a byte count the way Windows Explorer does (1 KB = 1024 bytes)."""
    n = float(num_bytes)
    if n < 1024:
        whole = int(n)
        return f"{whole} byte" if whole == 1 else f"{whole} bytes"
    units = ("KB", "MB", "GB", "TB", "PB", "EB")
    value = n
    unit = "bytes"
    for unit in units:
        value /= 1024.0
        if value < 1024 or unit == units[-1]:
            break
    if value >= 100:
        text = f"{value:.0f} {unit}"
    elif value >= 10:
        text = f"{value:.1f} {unit}"
    else:
        text = f"{value:.2f} {unit}"
    if exact:
        text += f" ({int(num_bytes):,} bytes)"
    return text


def format_capacity(num_bytes: int) -> str:
    """Capacity in decimal units, as printed on drive labels (1 TB = 10^12)."""
    n = float(num_bytes)
    for unit, factor in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= factor:
            value = n / factor
            return f"{value:.0f} {unit}" if value >= 100 else f"{value:.1f} {unit}"
    return f"{int(n)} bytes"


def format_rate(bytes_per_second: float) -> str:
    if bytes_per_second <= 0:
        return "-"
    return f"{format_size(bytes_per_second)}/s"


def format_duration(seconds: float | None) -> str:
    if seconds is None or math.isnan(seconds) or seconds < 0:
        return "-"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {sec:02d} s"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes:02d} min"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h"


def format_timestamp(ts: float | None) -> str:
    if ts is None:
        return ""
    try:
        return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def _sane(ts: float) -> float | None:
    if _MIN_TS <= ts <= _MAX_TS:
        return ts
    return None


def filetime_to_unix(filetime: int) -> float | None:
    """Convert a Windows FILETIME (100 ns since 1601, UTC) to POSIX seconds."""
    if filetime <= 0:
        return None
    return _sane((filetime - _FILETIME_EPOCH_DELTA) / 10_000_000)


def unix_to_filetime(ts: float) -> int:
    return int(round(ts * 10_000_000)) + _FILETIME_EPOCH_DELTA


def fat_datetime(date: int, time_: int = 0, tenths: int = 0) -> float | None:
    """Decode a FAT date/time (local time of the writer) to POSIX seconds.

    FAT does not record a time zone; like Windows, we interpret the value in
    the local time zone of this computer.
    """
    if date == 0:
        return None
    year = 1980 + (date >> 9)
    month = (date >> 5) & 0x0F
    day = date & 0x1F
    hour = time_ >> 11
    minute = (time_ >> 5) & 0x3F
    second = (time_ & 0x1F) * 2
    extra = tenths / 100.0 if 0 <= tenths <= 199 else 0.0
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60 and second < 60):
        return None
    try:
        local = _dt.datetime(year, month, day, hour, minute, second)
        return _sane(local.timestamp() + extra)
    except (ValueError, OverflowError, OSError):
        return None


def exfat_timestamp(stamp: int, increment_10ms: int = 0, utc_offset: int = 0) -> float | None:
    """Decode an exFAT timestamp with its 10 ms increment and UTC offset byte."""
    if stamp == 0:
        return None
    date = stamp >> 16
    time_ = stamp & 0xFFFF
    year = 1980 + (date >> 9)
    month = (date >> 5) & 0x0F
    day = date & 0x1F
    hour = time_ >> 11
    minute = (time_ >> 5) & 0x3F
    second = (time_ & 0x1F) * 2
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60 and second < 60):
        return None
    extra = increment_10ms / 100.0 if 0 <= increment_10ms <= 199 else 0.0
    try:
        if utc_offset & 0x80:
            quarter_hours = utc_offset & 0x7F
            if quarter_hours & 0x40:  # 7-bit two's complement
                quarter_hours -= 0x80
            tz = _dt.timezone(_dt.timedelta(minutes=15 * quarter_hours))
            moment = _dt.datetime(year, month, day, hour, minute, second, tzinfo=tz)
        else:
            moment = _dt.datetime(year, month, day, hour, minute, second)
        return _sane(moment.timestamp() + extra)
    except (ValueError, OverflowError, OSError):
        return None


def align_down(value: int, alignment: int) -> int:
    return value - (value % alignment)


def align_up(value: int, alignment: int) -> int:
    rem = value % alignment
    return value if rem == 0 else value + alignment - rem


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping/adjacent half-open ranges."""
    if not ranges:
        return []
    ordered = sorted(r for r in ranges if r[1] > r[0])
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def ranges_total(ranges: list[tuple[int, int]]) -> int:
    return sum(end - start for start, end in ranges)
