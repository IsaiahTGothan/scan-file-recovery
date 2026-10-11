"""Record which build this is (CI run number, commit, date) in lifeboat/_build.py.

Run before packaging. The About box, `lifeboat-cli --version`, the log and the
recovery reports then show for example "1.1.0 (build 19, a8fa2e7)", so two
copies of Lifeboat can always be told apart.
"""

from __future__ import annotations

import datetime
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _commit() -> str:
    sha = os.environ.get("GITHUB_SHA", "")
    if not sha:
        try:
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                                 check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            sha = ""
    return sha[:7] or "unknown"


def main() -> None:
    build = os.environ.get("GITHUB_RUN_NUMBER", "local")
    date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    text = (f'"""Written by packaging/stamp_build.py; not in git."""\n\n'
            f'BUILD = "{build}"\nCOMMIT = "{_commit()}"\nDATE = "{date}"\n')
    (ROOT / "lifeboat" / "_build.py").write_text(text, encoding="utf-8")
    print(f"Stamped build {build}, commit {_commit()}, {date}")


if __name__ == "__main__":
    main()
