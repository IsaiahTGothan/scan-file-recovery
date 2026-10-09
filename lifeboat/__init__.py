"""Lifeboat Data Recovery: get files off failing drives safely.

The package is split into layers that only depend downward:

    device   raw, read-only access to disks and image files
    rescue   bad-sector aware reading (sector map, skipping, retries, timeouts)
    fs       partition tables and filesystem parsers (NTFS, FAT, exFAT, carving)
    scan     orchestration of quick and deep scans
    recover  copying files to a healthy destination, verification, reports
    imaging  ddrescue-style sector-by-sector imaging
    ui       PySide6 desktop application
"""

__version__ = "1.0.0"
