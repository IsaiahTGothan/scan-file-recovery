"""Desktop application entry point."""

from __future__ import annotations

import logging
import sys
import threading
import traceback
from types import TracebackType
from typing import TYPE_CHECKING

from .. import __version__
from ..branding import APP_FULL_NAME, APP_ID, PUBLISHER
from ..logsetup import setup_logging

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

log = logging.getLogger("lifeboat.ui")


def _set_windows_app_id() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError):
        pass


def _ask_for_admin(app: object) -> bool:
    """On Windows without admin rights, offer to restart elevated. True = quit now."""
    if sys.platform != "win32":
        return False
    from PySide6.QtWidgets import QMessageBox

    from ..device.enumerate import is_admin

    if is_admin():
        return False
    box = QMessageBox()
    box.setWindowTitle(APP_FULL_NAME)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setText("<b>Lifeboat needs administrator rights to read drives directly.</b>")
    box.setInformativeText("Without them you can still open disk image files. Restart as administrator now?")
    restart = box.addButton("Restart as administrator", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Continue without", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    if box.clickedButton() is not restart:
        return False
    import ctypes

    params = " ".join(f'"{a}"' for a in sys.argv[1:])
    if getattr(sys, "frozen", False):
        exe, args = sys.executable, params
    else:
        exe, args = sys.executable, f"-m lifeboat {params}"
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, None, 1) > 32


def main(argv: list[str] | None = None) -> int:
    log_file = setup_logging()
    _set_windows_app_id()
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("Lifeboat")
    app.setApplicationDisplayName(APP_FULL_NAME)
    app.setOrganizationName(PUBLISHER)
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(True)

    from . import icons, theme
    from .dialogs import ErrorDialog, setting

    theme.apply(app, str(setting("theme", "dark")))
    app.setWindowIcon(icons.app_icon())

    def excepthook(kind: type[BaseException], value: BaseException, tb: TracebackType | None) -> None:
        text = "".join(traceback.format_exception(kind, value, tb))
        log.critical("Unhandled error\n%s", text)
        try:
            ErrorDialog(None, "Unexpected error",
                        f"{kind.__name__}: {value}\n\nLifeboat kept running. The details are in the log file:\n"
                        f"{log_file}", text).exec()
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = excepthook

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        log.critical("Unhandled error in thread %s", args.thread.name if args.thread else "?",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))  # type: ignore[arg-type]

    threading.excepthook = thread_hook
    args = sys.argv[1:] if argv is None else argv[1:]
    smoke = "--smoke-test" in args
    if not smoke and _ask_for_admin(app):
        return 0
    from .main_window import MainWindow

    window = MainWindow()
    window.show()
    if smoke:
        # Packaging check: build the whole window, open and close every dialog, run the
        # event loop briefly, quit - and exit cleanly (a crash on exit fails the check).
        from functools import partial

        from PySide6.QtCore import QTimer

        QTimer.singleShot(500, partial(_exercise_dialogs, window))
        QTimer.singleShot(2500, window.close)
        QTimer.singleShot(3000, app.quit)
        code = app.exec()
        _dispose(window)
        log.info("Smoke test finished (exit %s)", code)
        print("Lifeboat smoke test OK")
        return code
    code = app.exec()
    _dispose(window)
    return code


def _exercise_dialogs(window: QWidget) -> None:
    from ..device.base import DeviceInfo
    from ..recover import RecoverySummary
    from .dialogs import (
        AboutDialog,
        DeepScanDialog,
        ErrorDialog,
        ImageDialog,
        RecoverDialog,
        SettingsDialog,
        SummaryDialog,
    )

    disk = DeviceInfo(path=r"\\.\PhysicalDrive9", kind="disk", size=1 << 30, model="Smoke test", serial="0")
    dialogs = [
        RecoverDialog(window, disk, 3, 3000, 0, 0, 1000), ImageDialog(window, disk), DeepScanDialog(window, disk),
        SettingsDialog(window), AboutDialog(window), ErrorDialog(window, "Smoke test", "Not a real error."),
        SummaryDialog(window, RecoverySummary("", [], 0.0)),
    ]
    for dialog in dialogs:
        dialog.show()
        dialog.close()
        dialog.deleteLater()
    log.info("Smoke test opened and closed %d dialogs", len(dialogs))


def _dispose(window: QWidget) -> None:
    """Destroy the window while Qt is fully alive, not later during interpreter shutdown,
    where the order in which Python frees objects can crash Qt."""
    try:
        import shiboken6

        if shiboken6.isValid(window):
            shiboken6.delete(window)
    except Exception:  # quitting must never fail
        log.exception("Could not close the window cleanly")


if __name__ == "__main__":
    sys.exit(main())
