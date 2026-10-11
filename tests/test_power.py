"""Keeping the computer awake during long jobs."""

from __future__ import annotations

import subprocess
import sys

import pytest

from lifeboat import build_info, power


@pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="the Linux way: systemd-inhibit")
def test_keep_awake_starts_and_stops_an_inhibitor(monkeypatch):
    started = []

    class FakeProcess:
        def __init__(self, args, **kwargs):
            started.append(args)
            self.terminated = False

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(power.shutil, "which", lambda name: "/usr/bin/systemd-inhibit")
    monkeypatch.setattr(power.subprocess, "Popen", FakeProcess)
    keep = power.KeepAwake()
    assert keep.start("Recovering 12 files") and keep.active
    assert keep.start() and len(started) == 1  # already on: nothing new started
    process = keep._process
    assert started[0][0] == "systemd-inhibit" and "--what=sleep:idle" in started[0]
    assert "--why=Recovering 12 files" in started[0]
    keep.stop()
    assert process.terminated and not keep.active
    keep.stop()  # stopping twice is harmless


@pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="the Linux way: systemd-inhibit")
def test_keep_awake_without_an_inhibitor_is_a_quiet_no(monkeypatch):
    monkeypatch.setattr(power.shutil, "which", lambda name: None)
    keep = power.KeepAwake()
    assert not keep.start() and not keep.active
    keep.stop()


@pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="the Linux way: systemd-inhibit")
def test_keep_awake_survives_a_broken_inhibitor(monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(power.shutil, "which", lambda name: "/usr/bin/systemd-inhibit")
    monkeypatch.setattr(power.subprocess, "Popen", broken)
    keep = power.KeepAwake()
    assert not keep.start() and not keep.active


def test_build_info_names_the_build_when_stamped(monkeypatch):
    import types

    import lifeboat

    monkeypatch.delitem(sys.modules, "lifeboat._build", raising=False)
    assert build_info().startswith(lifeboat.__version__)
    stamp = types.ModuleType("lifeboat._build")
    stamp.BUILD, stamp.COMMIT, stamp.DATE = "42", "abc1234", "2026-10-11"
    monkeypatch.setitem(sys.modules, "lifeboat._build", stamp)
    assert build_info() == f"{lifeboat.__version__} (build 42, abc1234)"


def test_stamp_build_writes_the_module(tmp_path, monkeypatch):
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "packaging" / "stamp_build.py"
    root = tmp_path / "repo"
    (root / "lifeboat").mkdir(parents=True)
    (root / "packaging").mkdir()
    (root / "packaging" / "stamp_build.py").write_text(script.read_text())
    env = {"GITHUB_RUN_NUMBER": "77", "GITHUB_SHA": "0123456789abcdef", "PATH": "/usr/bin:/bin"}
    subprocess.run([sys.executable, str(root / "packaging" / "stamp_build.py")], env=env, check=True,
                   capture_output=True)
    text = (root / "lifeboat" / "_build.py").read_text()
    assert 'BUILD = "77"' in text and 'COMMIT = "0123456"' in text
