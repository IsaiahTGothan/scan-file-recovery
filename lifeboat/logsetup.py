"""Log files and crash capture."""

from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

from . import __version__

_FAULT_FILE = None


def app_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Lifeboat"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Lifeboat"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "lifeboat"
    base.mkdir(parents=True, exist_ok=True)
    return base


def log_dir() -> str:
    folder = app_data_dir() / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    return str(folder)


def setup_logging(verbose: bool = False) -> str:
    """Log to a rotating file (always) and to stderr.  Returns the log file path."""
    global _FAULT_FILE
    folder = Path(log_dir())
    log_file = folder / "lifeboat.log"
    root = logging.getLogger()
    if any(getattr(h, "_lifeboat", False) for h in root.handlers):
        return str(log_file)
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=10 << 20, backupCount=5,
                                                        encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)
    file_handler._lifeboat = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        console.setLevel(logging.DEBUG if verbose else logging.WARNING)
        console._lifeboat = True  # type: ignore[attr-defined]
        root.addHandler(console)
    logging.getLogger("lifeboat").info("Lifeboat %s starting (Python %s, %s)", __version__,
                                       sys.version.split()[0], sys.platform)
    try:
        _FAULT_FILE = open(folder / "crash.log", "a", encoding="utf-8")  # noqa: SIM115
        _FAULT_FILE.write(f"\n--- session {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        _FAULT_FILE.flush()
        faulthandler.enable(_FAULT_FILE)
    except OSError:
        pass
    return str(log_file)
