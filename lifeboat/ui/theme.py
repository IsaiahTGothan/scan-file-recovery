"""Colours, palette and style sheet (dark and light)."""

from __future__ import annotations

from dataclasses import dataclass, replace

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QFocusEvent, QFont, QPalette
from PySide6.QtWidgets import QAbstractButton, QApplication

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
    accent_hover: str = "#ff7d36"
    accent_2: str = "#ffb03a"          # where accent gradients end
    # Set by readable(): the accent behind accent_text (buttons), and as text on the panels.
    accent_fill: str = ""
    accent_fill_hover: str = ""
    accent_ink: str = ""
    banner_bad: str = ""               # the red and amber banners, dark enough for their white text
    banner_warn: str = ""


# Accent colour choices (Settings > Appearance): key -> (name, accent, hover, gradient end).
ACCENTS: dict[str, tuple[str, str, str, str]] = {
    "lifeboat": ("Lifeboat orange", ACCENT, "#ff7d36", "#ffb03a"),
    "ocean": ("Ocean blue", "#2f7ff0", "#4b92f4", "#4fc3f7"),
    "emerald": ("Emerald", "#12a877", "#22bc89", "#7be0b5"),
    "violet": ("Violet", "#8b5cf6", "#9d74f8", "#c9b6fc"),
    "rose": ("Rose", "#e8457a", "#ee6290", "#f9a8c4"),
}


def _luminance(hex_value: str) -> float:
    colour = QColor(hex_value)

    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(colour.red()) + 0.7152 * channel(colour.green()) + 0.0722 * channel(colour.blue())


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two colours (1 to 21; text needs 4.5)."""
    light, dark = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _toward(hex_value: str, target: str, against: tuple[str, ...], ratio: float) -> str:
    """``hex_value`` mixed with ``target`` (black or white) just enough to reach ``ratio`` against
    every colour in ``against``."""
    start, end = QColor(hex_value), QColor(target)
    for step in range(41):
        f = step / 40
        mixed = QColor(round(start.red() + (end.red() - start.red()) * f),
                       round(start.green() + (end.green() - start.green()) * f),
                       round(start.blue() + (end.blue() - start.blue()) * f)).name()
        if all(contrast(mixed, other) >= ratio for other in against):
            return mixed
    return target


def readable(t: Theme) -> Theme:
    """The accent as it can be read: buttons dark enough for their white text, and accent-coloured
    text that stands out from the panels (WCAG AA, 4.5:1). Borders, bars and icons keep the accent."""
    fill = _toward(t.accent, "#000000", (t.accent_text,), 4.6)
    backgrounds = (t.window, t.panel, t.panel_alt, t.raised)
    away = "#ffffff" if t.name == "dark" else "#000000"
    return replace(t, accent_fill=fill, accent_fill_hover=_toward(fill, "#000000", (t.accent_text,), 5.6),
                   accent_ink=_toward(t.accent, away, backgrounds, 4.6),
                   banner_bad=_toward(t.bad, "#000000", ("#ffffff",), 4.6),
                   banner_warn=_toward(t.warn, "#000000", ("#ffffff",), 4.6))


def rgba(hex_value: str, alpha: float) -> str:
    """A colour with transparency, for style sheets: rgba(r, g, b, a)."""
    colour = QColor(hex_value)
    return f"rgba({colour.red()}, {colour.green()}, {colour.blue()}, {alpha:.2f})"


DARK = Theme(
    name="dark",
    window="#0d1520", panel="#131e2c", panel_alt="#172435", raised="#1c2b3e", border="#24364c",
    text="#e7edf5", muted="#93a6bd", accent=ACCENT, accent_text="#ffffff",
    ok="#3fbf7f", warn="#f2b035", bad="#f26363", info="#5aa9ff", selection="#25456b",
    map_untried="#2a3a4f", map_good="#2f9e66", map_partial="#7fd1a6", map_skipped="#c9a227",
    map_failed="#f08a24", map_bad="#e5484d",
)

LIGHT = Theme(
    name="light",
    window="#eef2f6", panel="#ffffff", panel_alt="#f6f8fb", raised="#ffffff", border="#d6dee8",
    text="#122033", muted="#5b6b80", accent=ACCENT, accent_text="#ffffff",
    ok="#147536", warn="#a15c00", bad="#c62828", info="#1c66c2", selection="#cfe3ff",
    map_untried="#d9e1ea", map_good="#2f9e66", map_partial="#9fdcbc", map_skipped="#e2c04f",
    map_failed="#f08a24", map_bad="#d93036",
)

_current = readable(DARK)   # every colour set, even before apply() runs


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


TEXT_SIZES = {"normal": 10.0, "large": 11.5}


def stylesheet(t: Theme, text_size: str = "normal", density: str = "comfortable") -> str:
    points = TEXT_SIZES.get(text_size, 10.0)
    row_pad = "1px 4px" if density == "compact" else "3px 4px"
    return f"""
QWidget {{ font-size: {points}pt; }}
QToolTip {{ color: {t.text}; background: {t.raised}; border: 1px solid {t.border}; padding: 6px; border-radius: 6px; }}
QMainWindow, QDialog {{ background: {t.window}; }}
#HeaderBar {{ background: {t.panel}; border-bottom: 1px solid {t.border}; }}
#StatusBar {{ background: {t.panel}; border-top: 1px solid {t.border}; }}
#StatusBar QPushButton, #TabPage QPushButton {{ padding: 3px 10px; }}
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
QPushButton[primary="true"] {{ background: {t.accent_fill}; color: {t.accent_text};
  border: 1px solid {t.accent_fill}; font-weight: 700; }}
QPushButton[primary="true"]:hover {{ background: {t.accent_fill_hover}; border-color: {t.accent}; }}
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
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{ color: {t.muted};
  background: {t.panel}; }}
QComboBox QAbstractItemView {{ background: {t.raised}; color: {t.text}; border: 1px solid {t.border};
  selection-background-color: {t.selection}; }}
QTreeView, QTableView, QListView, QPlainTextEdit, QTextBrowser {{ background: {t.panel}; color: {t.text};
  border: 1px solid {t.border}; border-radius: 8px; alternate-background-color: {t.panel_alt};
  selection-background-color: {t.selection}; selection-color: {t.text}; }}
QTreeView::item, QTableView::item {{ padding: {row_pad}; }}
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
#Banner[level="critical"] {{ background: {t.banner_bad}; }}
#Banner[level="warning"] {{ background: {t.banner_warn}; }}
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
QToolButton#TickButton {{ background: {t.raised}; border: 1px solid {t.border}; border-radius: 8px;
  padding: 6px 26px 6px 10px; }}
QToolButton#TickButton:hover {{ border-color: {t.accent}; }}
QToolButton#TickButton::menu-button {{ border-left: 1px solid {t.border}; width: 20px; }}
#JobPanel {{ background: {t.panel}; border: 1px solid {t.border}; border-left: 4px solid {t.accent};
  border-radius: 10px; }}
#JobPercent {{ font-size: {points * 2.1:.1f}pt; font-weight: 800; color: {t.text}; }}
#JobPercent[compact="true"] {{ font-size: {points * 1.3:.1f}pt; }}
#JobTitle {{ font-size: {points * 1.15:.1f}pt; font-weight: 700; }}
#MetricValue {{ font-size: {points * 1.15:.1f}pt; font-weight: 700; }}
#MetricLabel {{ color: {t.muted}; font-size: {points * 0.85:.1f}pt; }}
QProgressBar#JobBar {{ height: 12px; border-radius: 6px; }}
QProgressBar#JobBar::chunk {{ border-radius: 5px; background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
  stop:0 {t.accent}, stop:1 {t.accent_2}); }}
QToolButton#Chip {{ background: {t.panel_alt}; border: 1px solid {t.border}; border-radius: 14px;
  padding: 4px 12px; color: {t.text}; }}
QToolButton#Chip:hover {{ border-color: {t.accent}; }}
QToolButton#Chip:checked {{ background: {t.selection}; border-color: {t.accent}; font-weight: 700; }}
QListWidget#SettingsNav {{ background: {t.panel}; border: 1px solid {t.border}; border-radius: 10px; padding: 6px; }}
QListWidget#SettingsNav::item {{ padding: 8px 10px; border-radius: 7px; }}
QListWidget#SettingsNav::item:selected {{ background: {t.selection}; color: {t.text}; }}
#SettingHint {{ color: {t.muted}; font-size: {points * 0.9:.1f}pt; }}
#SettingName {{ font-weight: 600; }}
#SettingsCard {{ background: {t.panel}; border: 1px solid {t.border}; border-radius: 12px; }}
#Divider {{ background: {t.border}; border: none; max-height: 1px; min-height: 1px; }}
#SettingsSearch {{ border-radius: 9px; }}
#Badge {{ background: {rgba(t.accent, 0.14)}; color: {t.accent_ink}; border: 1px solid {rgba(t.accent, 0.55)};
  border-radius: 7px; padding: 2px 8px; font-size: {points * 0.75:.1f}pt; font-weight: 800; }}
#SoonPill {{ background: {rgba(t.warn, 0.14)}; color: {t.warn}; border: 1px solid {rgba(t.warn, 0.45)};
  border-radius: 6px; padding: 1px 7px; font-size: {points * 0.72:.1f}pt; font-weight: 800; }}
#PopularPill {{ background: {t.accent_fill}; color: {t.accent_text}; border-radius: 7px; padding: 2px 9px;
  font-size: {points * 0.75:.1f}pt; font-weight: 800; }}
QPushButton[upgrade="true"] {{ background: {rgba(t.accent, 0.10)}; color: {t.text}; border: 1px solid {t.accent};
  font-weight: 700; }}
QPushButton[upgrade="true"]:hover {{ background: {rgba(t.accent, 0.22)}; }}
#StepNumber {{ color: {t.accent_ink}; font-size: {points * 1.8:.1f}pt; font-weight: 800; }}
#StepCard {{ background: {t.panel_alt}; border: 1px solid {t.border}; border-radius: 12px; }}
#PromoCard {{ border: 1px solid {rgba(t.accent, 0.6)}; border-radius: 12px;
  background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {rgba(t.accent, 0.16)}, stop:0.6 {t.panel},
  stop:1 {t.panel}); }}
#PromoTitle {{ font-size: {points * 1.25:.1f}pt; font-weight: 800; }}
#FormatChip {{ background: {t.panel_alt}; border: 1px solid {t.border}; border-radius: 8px; padding: 2px 10px;
  color: {t.text}; font-size: {points * 0.9:.1f}pt; }}
#ActionTile {{ background: {t.panel_alt}; border: 1px solid {t.border}; border-radius: 12px; }}
#ActionTile[recommended="true"] {{ border: 1px solid {t.accent}; }}
#TileTitle {{ font-size: {points * 1.15:.1f}pt; font-weight: 700; }}
#PlanCard {{ background: {t.panel}; border: 1px solid {t.border}; border-radius: 14px; }}
#PlanCard[popular="true"] {{ border: 2px solid {t.accent};
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {rgba(t.accent, 0.10)}, stop:0.35 {t.panel},
  stop:1 {t.panel}); }}
#PlanName {{ font-size: {points * 1.3:.1f}pt; font-weight: 800; }}
#PlanPrice {{ font-size: {points * 2.6:.1f}pt; font-weight: 800; }}
#Segmented {{ background: {t.panel_alt}; border: 1px solid {t.border}; border-radius: 18px; }}
#Segmented QToolButton {{ border: 1px solid transparent; border-radius: 15px; padding: 5px 17px; color: {t.muted};
  background: transparent; font-weight: 600; }}
#Segmented QToolButton:hover {{ color: {t.text}; }}
#Segmented QToolButton:checked {{ background: {t.accent_fill}; color: {t.accent_text}; font-weight: 700; }}
#Notice {{ background: {rgba(t.info, 0.12)}; border: 1px solid {rgba(t.info, 0.5)}; border-radius: 10px; }}
#AboutHero {{ border-radius: 14px; border: 1px solid {t.border};
  background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {rgba(t.accent, 0.20)}, stop:0.55 {t.panel},
  stop:1 {t.panel}); }}
#LinkButton {{ background: transparent; border: none; color: {t.info}; padding: 2px 0; text-align: left; }}
QToolButton#HelpButton::menu-indicator {{ image: none; width: 0; }}
QScrollArea#Clear {{ background: transparent; border: none; }}
QScrollArea#Clear > QWidget, #ClearInner {{ background: transparent; }}
#Keycap {{ background: {t.panel_alt}; color: {t.text}; border: 1px solid {t.border};
  border-bottom: 2px solid {t.border}; border-radius: 6px; padding: 2px 9px; font-weight: 700; }}
#LinkButton:hover {{ text-decoration: underline; }}
"""


def _indicator_files(t: Theme) -> dict[str, str]:
    """Write check box / radio indicator images for the theme; return their paths."""
    from ..logsetup import app_data_dir

    # One folder per theme and accent: Qt caches images by file name, so a new accent needs new files.
    folder = app_data_dir() / "theme" / f"{t.name}-{t.accent.lstrip('#').lower()}"
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
        "down": f'<path d="M4.5 7l4.5 4.5L13.5 7" fill="none" stroke="{t.muted}" stroke-width="1.8" '
                f'stroke-linecap="round" stroke-linejoin="round"/>',
        "up": f'<path d="M4.5 11l4.5-4.5L13.5 11" fill="none" stroke="{t.muted}" stroke-width="1.8" '
              f'stroke-linecap="round" stroke-linejoin="round"/>',
        "down_off": f'<path d="M4.5 7l4.5 4.5L13.5 7" fill="none" stroke="{t.border}" stroke-width="1.8" '
                    f'stroke-linecap="round" stroke-linejoin="round"/>',
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
    if len(paths) < 10:
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
QComboBox {{ padding-right: 26px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 24px; border: none; }}
QComboBox::down-arrow {{ image: url("{paths['down']}"); width: 14px; height: 14px; }}
QComboBox::down-arrow:disabled {{ image: url("{paths['down_off']}"); }}
QSpinBox, QDoubleSpinBox {{ padding-right: 22px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right;
  width: 20px; border: none; background: transparent; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right;
  width: 20px; border: none; background: transparent; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url("{paths['up']}"); width: 11px; height: 11px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url("{paths['down']}"); width: 11px; height: 11px; }}
"""


def _focus_rules(t: Theme) -> str:
    """The ring around the control the keyboard focus is on (see FocusRings)."""
    return f"""
QPushButton[keyfocus="true"], QToolButton[keyfocus="true"], QToolButton#Chip[keyfocus="true"],
QToolButton#TickButton[keyfocus="true"], #Segmented QToolButton[keyfocus="true"],
QPushButton[primary="true"][keyfocus="true"] {{ border: 1px solid {t.text}; }}
QCheckBox[keyfocus="true"], QRadioButton[keyfocus="true"] {{ background: {rgba(t.accent, 0.18)}; border-radius: 6px; }}
"""


class FocusRings(QObject):
    """Rings the button, check box or radio button the keyboard focus moves to (Tab, a shortcut), as
    Windows and macOS do. A click doesn't ring it. (With a style sheet, Qt shows no focus at all.)"""

    KEYBOARD = (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                Qt.FocusReason.ShortcutFocusReason)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        # It sees every event of the application: it must never fail, not even for a widget
        # that is being destroyed.
        try:
            kind = event.type()
            if kind in (QEvent.Type.FocusIn, QEvent.Type.FocusOut) and isinstance(obj, QAbstractButton):
                ring = (kind == QEvent.Type.FocusIn and isinstance(event, QFocusEvent)
                        and event.reason() in self.KEYBOARD)
                if bool(obj.property("keyfocus")) != ring:
                    obj.setProperty("keyfocus", ring)
                    obj.style().unpolish(obj)
                    obj.style().polish(obj)
        except Exception:  # noqa: BLE001
            pass
        return False


_focus_rings: FocusRings | None = None


def apply(app: QApplication, name: str = "dark", text_size: str = "normal", density: str = "comfortable",
          accent: str = "lifeboat") -> Theme:
    global _current
    if name == "system":
        hints = app.styleHints()
        try:
            from PySide6.QtCore import Qt

            name = "dark" if hints.colorScheme() == Qt.ColorScheme.Dark else "light"
        except AttributeError:
            name = "dark"
    theme = LIGHT if name == "light" else DARK
    if accent in ACCENTS and accent != "lifeboat":
        _name, colour, hover, end = ACCENTS[accent]
        theme = replace(theme, accent=colour, accent_hover=hover, accent_2=end)
    theme = readable(theme)
    _current = theme
    app.setStyle("Fusion")
    app.setPalette(_palette(theme))
    try:
        indicators = _indicator_rules(_indicator_files(theme))
    except Exception:  # noqa: BLE001 - fall back to the default indicators
        indicators = ""
    app.setStyleSheet(stylesheet(theme, text_size, density) + indicators + _focus_rules(theme))
    global _focus_rings
    if _focus_rings is None:
        _focus_rings = FocusRings(app)
        app.installEventFilter(_focus_rings)
    font = app.font()
    font.setPointSizeF(TEXT_SIZES.get(text_size, 10.0))
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)
    return theme
