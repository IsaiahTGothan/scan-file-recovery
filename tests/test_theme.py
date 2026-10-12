"""Colours that can be read: text reaches WCAG AA contrast (4.5:1) on every background it's drawn on,
in the dark and light themes and with every accent colour."""

from __future__ import annotations

from dataclasses import replace

import pytest

pytest.importorskip("PySide6.QtGui")

from lifeboat.ui.theme import ACCENTS, DARK, LIGHT, contrast, readable  # noqa: E402

AA = 4.5


@pytest.mark.parametrize("base", [DARK, LIGHT], ids=["dark", "light"])
def test_text_colours_are_readable(base):
    backgrounds = (base.window, base.panel, base.panel_alt, base.raised)
    for name in ("text", "muted", "ok", "warn", "bad", "info"):
        worst = min(contrast(getattr(base, name), background) for background in backgrounds)
        assert worst >= AA, f"{base.name} {name}: {worst:.2f}"


@pytest.mark.parametrize("base", [DARK, LIGHT], ids=["dark", "light"])
@pytest.mark.parametrize("accent", sorted(ACCENTS))
def test_accent_text_is_readable(base, accent):
    _name, colour, hover, end = ACCENTS[accent]
    theme = readable(replace(base, accent=colour, accent_hover=hover, accent_2=end))
    # White text on buttons, badges and the chosen billing period.
    assert contrast(theme.accent_fill, theme.accent_text) >= AA
    assert contrast(theme.accent_fill_hover, theme.accent_text) >= AA
    # The accent as text (step numbers, badges) on every panel.
    for background in (theme.window, theme.panel, theme.panel_alt, theme.raised):
        assert contrast(theme.accent_ink, background) >= AA


def test_contrast_matches_wcag():
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#777777", "#ffffff") == pytest.approx(4.48, abs=0.01)
    assert contrast("#123456", "#123456") == pytest.approx(1.0)
