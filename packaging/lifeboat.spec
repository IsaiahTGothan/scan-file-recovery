# PyInstaller build: one folder with Lifeboat.exe (GUI, asks for admin) and
# lifeboat-cli.exe (console).  Run from the repository root:
#     pyinstaller packaging/lifeboat.spec --noconfirm
# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821 - provided by PyInstaller
sys.path.insert(0, str(ROOT))
from lifeboat import __version__  # noqa: E402
from lifeboat.branding import APP_FULL_NAME, PUBLISHER  # noqa: E402

parts = [int(p) for p in __version__.split(".")[:3]] + [0]
version_file = ROOT / "build" / "version_info.txt"
version_file.parent.mkdir(parents=True, exist_ok=True)
version_file.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={tuple(parts)}, prodvers={tuple(parts)}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', '{PUBLISHER}'),
      StringStruct('FileDescription', '{APP_FULL_NAME}'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', 'Lifeboat'),
      StringStruct('LegalCopyright', 'Copyright (c) {PUBLISHER}'),
      StringStruct('OriginalFilename', 'Lifeboat.exe'),
      StringStruct('ProductName', '{APP_FULL_NAME}'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")

ICON = str(ROOT / "lifeboat" / "assets" / "lifeboat.ico")
EXCLUDES = [
    "tkinter", "unittest", "pydoc_data", "test", "lib2to3", "numpy", "PIL", "pytest",
    "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtSql",
    "PySide6.QtTest", "PySide6.QtXml", "PySide6.QtDBus", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtPrintSupport", "PySide6.QtConcurrent", "PySide6.QtHelp", "PySide6.QtDesigner",
    "PySide6.QtUiTools", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtMultimedia",
    "PySide6.QtCharts", "PySide6.Qt3DCore", "PySide6.QtPdf", "PySide6.QtSvgWidgets",
]
HIDDEN = ["lifeboat.device.windows", "lifeboat.recover.winverify", "lifeboat.device.posix"]

gui = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry_gui.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "lifeboat" / "assets"), "lifeboat/assets")],
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
    gui_pyz, gui.scripts, [], exclude_binaries=True, name="Lifeboat", console=False, icon=ICON,
    uac_admin=True, version=str(version_file), upx=False,
)
cli_exe = EXE(  # noqa: F821
    cli_pyz, cli.scripts, [], exclude_binaries=True, name="lifeboat-cli", console=True, icon=ICON,
    version=str(version_file), upx=False,
)
COLLECT(  # noqa: F821
    gui_exe, gui.binaries, gui.datas,
    cli_exe, cli.binaries, cli.datas,
    strip=False, upx=False, name="Lifeboat",
)
