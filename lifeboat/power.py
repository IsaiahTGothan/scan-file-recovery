"""Keep the computer from going to sleep while a long job runs.

A scan or recovery of a big drive takes hours; if the computer sleeps, the job pauses and USB
drives can drop off. The screen may still turn off: only system sleep is held back, and only
while a job runs.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger("lifeboat")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    """``start()`` when a job begins, ``stop()`` when it ends. Safe to call repeatedly.

    On Windows the request belongs to the calling thread, so call both from the same,
    long-lived thread (the GUI thread).
    """

    def __init__(self) -> None:
        self.active = False
        self._process: subprocess.Popen[bytes] | None = None

    def start(self, reason: str = "Lifeboat is recovering data") -> bool:
        if self.active:
            return True
        try:
            if sys.platform == "win32":
                import ctypes

                self.active = bool(ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
            elif sys.platform == "darwin":
                # caffeinate ends on its own if Lifeboat quits (-w: wait for this process).
                self._process = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.active = True
            elif shutil.which("systemd-inhibit"):
                self._process = subprocess.Popen(
                    ["systemd-inhibit", "--what=sleep:idle", "--who=Lifeboat", f"--why={reason}", "--mode=block",
                     "sleep", "infinity"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.active = True
        except (OSError, AttributeError, ValueError) as exc:
            log.info("Could not keep the computer awake: %s", exc)
            self.active = False
        return self.active

    def stop(self) -> None:
        if not self.active:
            return
        self.active = False
        try:
            if sys.platform == "win32":
                import ctypes

                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            elif self._process is not None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
        except (OSError, AttributeError) as exc:
            log.info("Could not release the keep-awake request: %s", exc)
        finally:
            self._process = None
