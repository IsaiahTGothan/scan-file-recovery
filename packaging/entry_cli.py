"""PyInstaller entry point for lifeboat-cli.exe."""

import sys

from lifeboat.cli import main

if __name__ == "__main__":
    sys.exit(main())
