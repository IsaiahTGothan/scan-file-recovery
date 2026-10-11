"""Start Lifeboat again with administrator rights, which reading drives directly needs.

Windows shows its UAC prompt; macOS asks for an administrator password in its own dialog.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import sys

log = logging.getLogger("lifeboat")


def can_elevate() -> bool:
    return sys.platform in ("win32", "darwin")


def _command(args: list[str]) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "lifeboat", *args]


def applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def relaunch_elevated(args: list[str]) -> bool:
    """True when a new copy with administrator rights was started (this one should quit)."""
    try:
        if sys.platform == "win32":
            import ctypes

            command = _command(args)
            params = subprocess.list2cmdline(command[1:])
            return int(ctypes.windll.shell32.ShellExecuteW(None, "runas", command[0], params, None, 1)) > 32
        if sys.platform == "darwin":
            # "do shell script" returns once the command's output is closed, hence the redirect and "&".
            shell = " ".join(shlex.quote(part) for part in _command(args)) + " >/dev/null 2>&1 &"
            script = (f"do shell script {applescript_string(shell)} with prompt "
                      f"{applescript_string('Lifeboat needs administrator rights to read drives directly.')} "
                      "with administrator privileges")
            done = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True,
                                  timeout=600, check=False)
            if done.returncode != 0:
                log.info("Restart with administrator rights cancelled or failed: %s", done.stderr.strip())
            return done.returncode == 0
    except (OSError, subprocess.TimeoutExpired, AttributeError) as exc:
        log.warning("Could not restart with administrator rights: %s", exc)
    return False
