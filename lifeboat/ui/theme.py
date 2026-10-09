"""Colours, palette and style sheet (dark and light)."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

from ..branding import ACCENT


@dataclass(frozen=True)
class Theme:
    name: str
    window: str
    panel: str
    panel_alt: str
    raised: str
    border: str
    text: str
    muted: str
    accent: str
    accent_text: str
    ok: str
    warn: str
    bad: str
    info: str
    selection: str
    map_untried: str
    map_good: str
    map_partial: str
    map_skipped: str
    map_failed: str
    map_bad: str


DARK = Theme(
    name="dark",
    window="#0d1520", panel="#131e2c", panel_alt="#172435", raised="#1c2b3e", border="#24364c",
    text="#e7edf5", muted="#93a6bd", accent=ACCENT, accent_text="#ffffff",
    ok="#3fbf7f", warn="#f2b035", bad="#f05252", info="#5aa9ff", selection="#25456b",
    map_untried="#2a3a4f", map_good="#2f9e66", map_partial="#7fd1a6", map_skipped="#c9a227",
    map_failed="#f08a24", map_bad="#e5484d",
)

LIGHT = Theme(
    name="light",
    window="#eef2f6", panel="#ffffff", panel_alt="#f6f8fb", raised="#ffffff", border="#d6dee8",
    text="#122033", muted="#5b6b80", accent=ACCENT, accent_text="#ffffff",
    ok="#16803c", warn="#a15c00", bad="#c62828", info="#1f6fd1", selection="#cfe3ff",
    map_untried="#d9e1ea", map_good="#2f9e66", map_partial="#9fdcbc", map_skipped="#e2c04f",
    map_failed="#f08a24", map_bad="#d93036",
)

_current = DARK


def current() -> Theme:
    return _current


def _palette(t: Theme) -> QPalette:
    p = QPalette()
    c = QColor
    p.setColor(QPalette.ColorRole.Window, c(t.window))
    p.setColor(QPalette.ColorRole.WindowText, c(t.text))
    p.setColor(QPalette.ColorRole.Base, c(t.panel))
    p.setColor(QPalette.ColorRole.AlternateBase, c(t.panel_alt))
    p.setColor(QPalette.ColorRole.ToolTipBase, c(t.raised))
    p.setColor(QPalette.ColorRole.ToolTipText, c(t.text))
    p.setColor(QPalette.ColorRole.Text, c(t.text))
    p.setColor(QPalette.ColorRole.Button, c(t.raised))
    p.setColor(QPalette.ColorRole.ButtonText, c(t.text))
    p.setColor(QPalette.ColorRole.BrightText, c("#ffffff"))
    p.setColor(QPalette.ColorRole.Highlight, c(t.selection))
    p.setColor(QPalette.ColorRole.HighlightedText, c(t.text))
    p.setColor(QPalette.ColorRole.Link, c(t.info))
    p.setColor(QPalette.ColorRole.PlaceholderText, c(t.muted))
    for group in (QPalette.ColorGroup.Disabled,):
        p.setColor(group, QPalette.ColorRole.Text, c(t.muted))
        p.setColor(group, QPalette.ColorRole.ButtonText, c(t.muted))
        p.setColor(group, QPalette.ColorRole.WindowText, c(t.muted))
    return p


def stylesheet(t: Theme) -> str:
    return f"""
QWidget {{ font-size: 10pt; }}
QToolTip {{ color: {t.text}; background: {t.raised}; border: 1px solid {t.border}; padding: 6px; border-radius: 6px; }}
QMainWindow, QDialog {{ background: {t.window}; }}
#HeaderBar {{ background: {t.panel}; border-bottom: 1px solid {t.border}; }}
#AppTitle {{ font-size: 15pt; font-weight: 700; color: {t.text}; }}
#AppSubtitle {{ color: {t.muted}; font-size: 9pt; }}
#Panel, #Card {{ background: {t.panel}; border: 1px solid {t.border}; border-radius: 10px; }}
#SectionTitle {{ font-size: 11pt; font-weight: 700; color: {t.text}; }}
#Muted, QLabel[muted="true"] {{ color: {t.muted}; }}
#BigTitle {{ font-size: 20pt; font-weight: 700; }}
#StatValue {{ font-size: 17pt; font-weight: 700; }}
QPushButton {{ background: {t.raised}; color: {t.text}; border: 1px solid {t.border}; border-radius: 8px;
  padding: 7px 14px; }}
QPushButton:hover {{ border-color: {t.accent}; }}
QPushButton:pressed {{ background: {t.panel_alt}; }}
QPushButton:disabled {{ color: {t.muted}; background: {t.panel}; border-color: {t.border}; }}
QPushButton[primary="true"] {{ background: {t.accent}; color: {t.accent_text}; border: 1px solid {t.accent};
  font-weight: 700; }}
QPushButton[primary="true"]:hover {{ background: #ff7d36; }}
QPushButton[primary="true"]:disabled {{ background: {t.raised}; color: {t.muted}; border-color: {t.border}; }}
QPushButton[danger="true"] {{ color: {t.bad}; border-color: {t.bad}; }}
QToolButton {{ background: transparent; color: {t.text}; border: 1px solid transparent; border-radius: 8px;
  padding: 6px 10px; }}
QToolButton:hover {{ background: {t.raised}; border-color: {t.border}; }}
QToolButton:checked {{ background: {t.selection}; }}
QToolButton:disabled {{ color: {t.muted}; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background: {t.panel_alt}; color: {t.text};
  border: 1px solid {t.border}; border-radius: 7px; padding: 5px 8px; selection-background-color: {t.selection}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: {t.accent}; }}
QComboBox QAbstractItemView {{ background: {t.raised}; color: {t.text}; border: 1px solid {t.border};
  selection-background-color: {t.selection}; }}
QTreeView, QTableView, QListView, QPlainTextEdit, QTextBrowser {{ background: {t.panel}; color: {t.text};
  border: 1px solid {t.border}; border-radius: 8px; alternate-background-color: {t.panel_alt};
  selection-background-color: {t.selection}; selection-color: {t.text}; }}
QTreeView::item, QTableView::item {{ padding: 3px 4px; }}
QHeaderView::section {{ background: {t.panel_alt}; color: {t.muted}; border: none;
  border-bottom: 1px solid {t.border}; padding: 6px 8px; font-weight: 600; }}
QTabWidget::pane {{ border: 1px solid {t.border}; border-radius: 8px; top: -1px; background: {t.panel}; }}
QTabBar::tab {{ background: transparent; color: {t.muted}; padding: 7px 14px; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {t.text}; border-bottom: 2px solid {t.accent}; }}
QTabBar::tab:hover {{ color: {t.text}; }}
QProgressBar {{ background: {t.panel_alt}; border: 1px solid {t.border}; border-radius: 6px; text-align: center;
  color: {t.text}; height: 16px; }}
QProgressBar::chunk {{ background: {t.accent}; border-radius: 5px; }}
QSplitter::handle {{ background: {t.window}; }}
QStatusBar {{ background: {t.panel}; border-top: 1px solid {t.border}; }}
QCheckBox, QRadioButton {{ spacing: 8px; }}
QGroupBox {{ border: 1px solid {t.border}; border-radius: 8px; margin-top: 14px; padding: 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {t.muted}; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t.border}; border-radius: 4px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {t.border}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QMenu {{ background: {t.raised}; color: {t.text}; border: 1px solid {t.border}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {t.selection}; }}
#Banner {{ border-radius: 10px; }}
#Banner[level="critical"] {{ background: {t.bad}; }}
#Banner[level="warning"] {{ background: {t.warn}; }}
#Banner QLabel {{ color: #ffffff; }}
#Banner QPushButton {{ background: rgba(255,255,255,0.18); color: #ffffff; border: 1px solid rgba(255,255,255,0.45); }}
#Banner QPushButton:hover {{ background: rgba(255,255,255,0.28); }}
#Toast {{ background: {t.raised}; border: 1px solid {t.border}; border-radius: 10px; }}
#Toast[level="error"], #Toast[level="critical"] {{ border-left: 4px solid {t.bad}; }}
#Toast[level="warning"] {{ border-left: 4px solid {t.warn}; }}
#Toast[level="success"] {{ border-left: 4px solid {t.ok}; }}
#Toast[level="info"] {{ border-left: 4px solid {t.info}; }}
#ToastTitle {{ font-weight: 700; }}
#Pill {{ border-radius: 9px; padding: 1px 8px; font-size: 9pt; font-weight: 600; }}
"""


def _indicator_files(t: Theme) -> dict[str, str]:
    """Write check box / radio indicator images for the theme; return their paths."""
    from ..logsetup import app_data_dir

    folder = app_data_dir() / "theme" / t.name
    folder.mkdir(parents=True, exist_ok=True)
    box = '<rect x="1" y="1" width="16" height="16" rx="4" fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>'
    svgs = {
        "unchecked": box.format(fill=t.panel_alt, stroke=t.muted),
        "checked": box.format(fill=t.accent, stroke=t.accent)
        + '<path d="M5 9.4l2.6 2.6L13 6.4" fill="none" stroke="#fff" stroke-width="2.2" '
          'stroke-linecap="round" stroke-linejoin="round"/>',
        "partial": box.format(fill=t.accent, stroke=t.accent)
        + '<path d="M5.2 9h7.6" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/>',
        "unchecked_off": box.format(fill=t.panel, stroke=t.border),
        "checked_off": box.format(fill=t.border, stroke=t.border)
        + '<path d="M5 9.4l2.6 2.6L13 6.4" fill="none" stroke="{c}" stroke-width="2.2" stroke-linecap="round" '
          'stroke-linejoin="round"/>'.replace("{c}", t.muted),
        "radio": f'<circle cx="9" cy="9" r="7.4" fill="{t.panel_alt}" stroke="{t.muted}" stroke-width="1.6"/>',
        "radio_on": f'<circle cx="9" cy="9" r="7.4" fill="{t.panel_alt}" stroke="{t.accent}" stroke-width="1.6"/>'
                    f'<circle cx="9" cy="9" r="4" fill="{t.accent}"/>',
    }
    paths = {}
    for key, body in svgs.items():
        path = folder / f"{key}.svg"
        text = f'<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 18 18">{body}</svg>'
        try:
            if not path.exists() or path.read_text() != text:
                path.write_text(text)
        except OSError:
            continue
        paths[key] = path.as_posix()
    return paths


def _indicator_rules(paths: dict[str, str]) -> str:
    if len(paths) < 7:
        return ""
    views = "QTreeView::indicator, QTableView::indicator, QListView::indicator, QCheckBox::indicator"
    return f"""
{views} {{ width: 16px; height: 16px; }}
QCheckBox::indicator:unchecked, QTreeView::indicator:unchecked, QTableView::indicator:unchecked,
QListView::indicator:unchecked {{ image: url("{paths['unchecked']}"); }}
QCheckBox::indicator:checked, QTreeView::indicator:checked, QTableView::indicator:checked,
QListView::indicator:checked {{ image: url("{paths['checked']}"); }}
QCheckBox::indicator:indeterminate, QTreeView::indicator:indeterminate, QTableView::indicator:indeterminate,
QListView::indicator:indeterminate {{ image: url("{paths['partial']}"); }}
QCheckBox::indicator:unchecked:disabled {{ image: url("{paths['unchecked_off']}"); }}
QCheckBox::indicator:checked:disabled {{ image: url("{paths['checked_off']}"); }}
QRadioButton::indicator {{ width: 16px; height: 16px; }}
QRadioButton::indicator:unchecked {{ image: url("{paths['radio']}"); }}
QRadioButton::indicator:checked {{ image: url("{paths['radio_on']}"); }}
"""


def apply(app: QApplication, name: str = "dark") -> Theme:
    global _current
    if name == "system":
        hints = app.styleHints()
        try:
            from PySide6.QtCore import Qt

            name = "dark" if hints.colorScheme() == Qt.ColorScheme.Dark else "light"
        except AttributeError:
            name = "dark"
    theme = LIGHT if name == "light" else DARK
    _current = theme
    app.setStyle("Fusion")
    app.setPalette(_palette(theme))
    try:
        indicators = _indicator_rules(_indicator_files(theme))
    except Exception:  # noqa: BLE001 - fall back to the default indicators
        indicators = ""
    app.setStyleSheet(stylesheet(theme) + indicators)
    font = app.font()
    if font.pointSizeF() < 9.5:
        font.setPointSizeF(10)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)
    return theme
