# PyInstaller build for macOS: Lifeboat.app, with the desktop app and lifeboat-cli inside
# (Contents/MacOS). Run from the repository root:
#     pyinstaller packaging/lifeboat_mac.spec --noconfirm
# -*- mode: python ; coding: utf-8 -*-
import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821 - provided by PyInstaller
sys.path.insert(0, str(ROOT))
from lifeboat import __version__, build_info  # noqa: E402
from lifeboat.branding import APP_FULL_NAME, CREDIT, PUBLISHER  # noqa: E402


def oldest_macos() -> str:
    """The oldest macOS the bundled Qt runs on, read from the platform tag of its wheel."""
    from importlib.metadata import PackageNotFoundError, distribution

    for name in ("PySide6_Essentials", "PySide6-Essentials", "shiboken6"):
        try:
            wheel = distribution(name).read_text("WHEEL") or ""
        except PackageNotFoundError:
            continue
        found = [(int(a), int(b)) for a, b in re.findall(r"macosx_(\d+)_(\d+)", wheel)]
        if found:
            major, minor = min(found)
            return f"{major}.{minor}"
    return "12.0"


def bundle_version() -> str:
    """CFBundleVersion: the CI build number when this build was stamped, else the version."""
    try:
        from lifeboat._build import BUILD
    except ImportError:
        return __version__
    return BUILD if BUILD.isdigit() else __version__


ICON = str(ROOT / "lifeboat" / "assets" / "lifeboat.icns")
EXCLUDES = [
    "tkinter", "unittest", "pydoc_data", "test", "lib2to3", "numpy", "PIL", "pytest",
    "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtSql",
    "PySide6.QtTest", "PySide6.QtXml", "PySide6.QtDBus", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtPrintSupport", "PySide6.QtConcurrent", "PySide6.QtHelp", "PySide6.QtDesigner",
    "PySide6.QtUiTools", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtMultimedia",
    "PySide6.QtCharts", "PySide6.Qt3DCore", "PySide6.QtPdf", "PySide6.QtSvgWidgets",
]
HIDDEN = ["lifeboat.device.macos", "lifeboat.device.check_mac", "lifeboat.device.posix", "lifeboat._build"]

gui = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry_gui.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "lifeboat" / "assets"), "lifeboat/assets"), (str(ROOT / "THIRD_PARTY_NOTICES.md"), ".")],
    hiddenimports=HIDDEN + ["PySide6.QtSvg"],
    excludes=EXCLUDES,
    noarchive=False,
)
cli = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry_cli.py")],
    pathex=[str(ROOT)],
    datas=[],
    hiddenimports=HIDDEN,
    excludes=EXCLUDES + ["PySide6", "shiboken6"],
    noarchive=False,
)
gui_pyz = PYZ(gui.pure)  # noqa: F821
cli_pyz = PYZ(cli.pure)  # noqa: F821
gui_exe = EXE(  # noqa: F821
    gui_pyz, gui.scripts, [], exclude_binaries=True, name="Lifeboat", console=False, icon=ICON, upx=False,
    argv_emulation=False,
)
cli_exe = EXE(  # noqa: F821
    cli_pyz, cli.scripts, [], exclude_binaries=True, name="lifeboat-cli", console=True, upx=False,
)
coll = COLLECT(  # noqa: F821
    gui_exe, gui.binaries, gui.datas,
    cli_exe, cli.binaries, cli.datas,
    strip=False, upx=False, name="Lifeboat",
)
BUNDLE(  # noqa: F821
    coll,
    name="Lifeboat.app",
    icon=ICON,
    bundle_identifier="us.zays.lifeboat",
    version=__version__,
    info_plist={
        "CFBundleName": "Lifeboat",
        "CFBundleDisplayName": APP_FULL_NAME,
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": bundle_version(),
        "CFBundleGetInfoString": f"{APP_FULL_NAME} {build_info()}, {CREDIT}",
        "NSHumanReadableCopyright": f"Copyright © {PUBLISHER}",
        "LSMinimumSystemVersion": oldest_macos(),
        "LSApplicationCategoryType": "public.app-category.utilities",
        # The bundle also holds the console tool, which would make PyInstaller mark the whole
        # app "background only" (no Dock icon, no menu bar): it is a normal app.
        "LSBackgroundOnly": False,
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
    },
)
