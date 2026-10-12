"""Desktop application entry point."""

from __future__ import annotations

import logging
import sys
import threading
import traceback
from types import TracebackType
from typing import TYPE_CHECKING

from .. import __version__
from ..branding import APP_FULL_NAME, APP_ID, SETTINGS_ORG
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
    """Without administrator rights (Windows, macOS), offer to restart with them. True = quit now."""
    from ..elevate import can_elevate, relaunch_elevated

    if not can_elevate():
        return False
    from .dialogs import pref

    if not bool(pref("ask_admin")):          # Settings > System > Offer to restart as administrator
        return False
    from PySide6.QtWidgets import QMessageBox

    from ..device.enumerate import is_admin

    if is_admin():
        return False
    box = QMessageBox()
    box.setWindowTitle(APP_FULL_NAME)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setText("<b>Lifeboat needs administrator rights to read drives directly.</b>")
    asks = "macOS asks for an administrator password." if sys.platform == "darwin" else ""
    box.setInformativeText(f"Without them you can still open disk image files. Restart with administrator rights "
                           f"now? {asks}".strip())
    restart = box.addButton("Restart as administrator", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Continue without", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    if box.clickedButton() is not restart:
        return False
    return relaunch_elevated(sys.argv[1:])


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
    app.setOrganizationName(SETTINGS_ORG)
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(True)

    from . import icons
    from .dialogs import ErrorDialog, apply_appearance

    apply_appearance(app)
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

    if smoke:
        # Packaging check, part 1: the decoders and the AES used to unlock Mac volumes are bundled.
        from ..fs.apfs_crypto import self_test as crypto_test
        from ..fs.applecomp import self_test as compression_test

        try:
            crypto_test()
            compression_test()
        except Exception as exc:  # reported as a failed check
            log.exception("Smoke test: self-test failed")
            print(f"Lifeboat smoke test FAILED: {exc}")
            return 3
    window = MainWindow()
    window.show()
    if smoke:
        # Packaging check, part 2: build the whole window, open and close every dialog, run the
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
        LicensesDialog,
        RecoverDialog,
        SummaryDialog,
        UnlockDialog,
    )
    from .drive_check import DriveCheckDialog
    from .plans_dialog import PlansDialog
    from .settings_dialog import SettingsDialog

    disk = DeviceInfo(path=r"\\.\PhysicalDrive9", kind="disk", size=1 << 30, model="Smoke test", serial="0")
    dialogs = [
        RecoverDialog(window, disk, 3, 3000, 0, 0, 1000), ImageDialog(window, disk), DeepScanDialog(window, disk),
        SettingsDialog(window), AboutDialog(window), ErrorDialog(window, "Smoke test", "Not a real error."),
        SummaryDialog(window, RecoverySummary("", [], 0.0)), DriveCheckDialog(window),
        UnlockDialog(window, "Smoke test", "a hint", "Not a real error."), PlansDialog(window),
        LicensesDialog(window), SettingsDialog(window, "Account & license"),
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
