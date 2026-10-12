"""Every window, in every state, at the sizes that matter, in normal and large text: no text cut
short, squeezed or hidden (tests/ui_audit.py). Run in a separate process, so it can have a screen
of its own: a 1366x768 laptop, a 1920x1080 laptop at 150% (1280x720) and a 1920x1080 monitor."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import UI_FONT

ROOT = Path(__file__).resolve().parent.parent
SCREENS = {"laptop": (1366, 768), "laptop150": (1280, 720), "monitor": (1920, 1080)}


@pytest.mark.parametrize("screen", sorted(SCREENS))
def test_no_text_is_cut_short(disks, tmp_path, screen):
    pytest.importorskip("PySide6.QtWidgets")
    if "hfsplus" not in disks or "apfs_locked" not in disks:
        pytest.skip("test disks not made yet")
    width, height = SCREENS[screen]
    (tmp_path / "screen.json").write_text(json.dumps({"screens": [
        {"name": screen, "x": 0, "y": 0, "width": width, "height": height, "logicalDpi": 96, "logicalBpp": 32,
         "dpr": 1}]}), encoding="utf-8")
    env = dict(os.environ)
    # A file name without a folder: on Windows, "C:" would end the platform's parameters early.
    env["QT_QPA_PLATFORM"] = "offscreen:configfile=screen.json"
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), env.get("PYTHONPATH", "")])
    env["LIFEBOAT_SETTINGS_NAME"] = "Lifeboat UI audit"
    if UI_FONT:
        env.setdefault("AUDIT_FONT", UI_FONT)
    run = subprocess.run([sys.executable, "-m", "tests.ui_audit", screen, str(disks["hfsplus"][0]),
                          str(disks["apfs_locked"][0])], cwd=tmp_path, env=env, capture_output=True, text=True,
                         timeout=900)
    assert run.returncode == 0, f"{run.stdout.strip()}\n{run.stderr[-3000:]}"
