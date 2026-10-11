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

__version__ = "1.2.0"


def build_info() -> str:
    """The version, plus the build number and commit for copies built by CI: "1.2.0 (build 19, a8fa2e7)"."""
    try:
        from ._build import BUILD, COMMIT  # written by packaging/stamp_build.py
    except ImportError:
        return __version__
    return f"{__version__} (build {BUILD}, {COMMIT})"
