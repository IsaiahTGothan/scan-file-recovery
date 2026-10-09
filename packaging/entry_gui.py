"""PyInstaller entry point for Lifeboat.exe (the desktop app)."""

import sys

from lifeboat.ui.app import main

if __name__ == "__main__":
    sys.exit(main())
