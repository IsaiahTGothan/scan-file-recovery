"""``python -m lifeboat`` starts the desktop app; ``python -m lifeboat <command>`` runs the CLI."""

import sys


def _main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in {"devices", "scan", "recover", "image", "--help", "-h",
                                              "--version"}:
        from lifeboat.cli import main

        return main()
    from lifeboat.ui.app import main as gui

    return gui()


if __name__ == "__main__":
    sys.exit(_main())
