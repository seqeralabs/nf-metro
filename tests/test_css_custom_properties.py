"""Tests for CSS custom property injection for chrome color theming.

Chrome colors (background, labels, section, title, legend) are driven through
CSS custom properties so a host can recolor without re-rendering.  Line/route
colors remain baked as presentation attributes (semantic).
"""

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.svg import render_svg
from nf_metro.themes import LIGHT_THEME, NFCORE_THEME

_MMD = (
    "%%metro title: Test\n"
    "%%metro line: main | Main | #ff0000\n"
    "%%metro line: other | Other | #00bbcc\n"
    "graph LR\n"
    "    subgraph s1 [Step One]\n"
    "        a[Input]\n"
    "    end\n"
    "    subgraph s2 [Step Two]\n"
    "        b[Output]\n"
    "    end\n"
    "    a -->|main| b\n"
    "    a -->|other| b\n"
)


def _make_graph():
    g = parse_metro_mermaid(_MMD)
    compute_layout(g)
    return g


# ---------------------------------------------------------------------------
# CSS custom property presence
# ---------------------------------------------------------------------------


def test_svg_contains_nfm_bg_property():
    """SVG output should declare --nfm-bg as a CSS custom property."""
    svg = render_svg(_make_graph(), NFCORE_THEME)
    assert "--nfm-bg" in svg


def test_svg_contains_nfm_label_color_property():
    """SVG output should declare --nfm-label-color for station labels."""
    svg = render_svg(_make_graph(), NFCORE_THEME)
    assert "--nfm-label-color" in svg


def test_svg_contains_nfm_title_color_property():
    """SVG output should declare --nfm-title-color for the diagram title."""
    svg = render_svg(_make_graph(), NFCORE_THEME)
    assert "--nfm-title-color" in svg


def test_svg_contains_nfm_section_fill_property():
    """SVG output should declare --nfm-section-fill for section boxes."""
    svg = render_svg(_make_graph(), NFCORE_THEME)
    assert "--nfm-section-fill" in svg


def test_svg_contains_nfm_section_stroke_property():
    """SVG output should declare --nfm-section-stroke for section box borders."""
    svg = render_svg(_make_graph(), NFCORE_THEME)
    assert "--nfm-section-stroke" in svg


def test_svg_contains_nfm_section_label_color_property():
    """SVG output should declare --nfm-section-label-color for section names."""
    svg = render_svg(_make_graph(), NFCORE_THEME)
    assert "--nfm-section-label-color" in svg


def test_svg_contains_nfm_legend_bg_property():
    """SVG output should declare --nfm-legend-bg for the legend panel."""
    svg = render_svg(_make_graph(), NFCORE_THEME)
    assert "--nfm-legend-bg" in svg


def test_svg_contains_nfm_legend_text_color_property():
    """SVG output should declare --nfm-legend-text-color for legend labels."""
    svg = render_svg(_make_graph(), NFCORE_THEME)
    assert "--nfm-legend-text-color" in svg


# ---------------------------------------------------------------------------
# Fallback values match theme colors
# ---------------------------------------------------------------------------


def test_chrome_css_fallbacks_match_nfcore_theme():
    """CSS custom property fallbacks must match the nfcore theme's baked colors."""
    theme = NFCORE_THEME
    svg = render_svg(_make_graph(), theme)
    assert f"--nfm-bg, {theme.background_color}" in svg
    assert f"--nfm-label-color, {theme.label_color}" in svg
    assert f"--nfm-title-color, {theme.title_color}" in svg
    assert f"--nfm-section-fill, {theme.section_fill}" in svg
    assert f"--nfm-section-stroke, {theme.section_stroke}" in svg
    assert f"--nfm-section-label-color, {theme.section_label_color}" in svg
    assert f"--nfm-legend-bg, {theme.legend_background}" in svg
    assert f"--nfm-legend-text-color, {theme.legend_text_color}" in svg


def test_chrome_css_fallbacks_match_light_theme():
    """CSS custom property fallbacks must match the light theme's baked colors.

    The bg rule is omitted for transparent themes (background_color='none').
    """
    theme = LIGHT_THEME
    svg = render_svg(_make_graph(), theme)
    # No bg rule for transparent-background themes
    assert "--nfm-bg" not in svg
    assert f"--nfm-label-color, {theme.label_color}" in svg
    assert f"--nfm-title-color, {theme.title_color}" in svg
    assert f"--nfm-section-fill, {theme.section_fill}" in svg
    assert f"--nfm-section-stroke, {theme.section_stroke}" in svg
    assert f"--nfm-section-label-color, {theme.section_label_color}" in svg


# ---------------------------------------------------------------------------
# Line/route colors remain baked (semantic, not chrome)
# ---------------------------------------------------------------------------


def test_line_colors_not_in_chrome_css_vars():
    """Line colors must NOT appear inside CSS custom property declarations."""
    g = _make_graph()
    svg = render_svg(g, NFCORE_THEME)
    # CSS custom properties live in a <style> block; line colors are baked
    # as stroke= presentation attributes on path/line elements.
    for line in g.lines.values():
        # The color should appear somewhere (baked), but not inside a var(...)
        assert f"--nfm-line-{line.id}" not in svg
        # Verify it IS baked as a presentation attribute
        assert line.color in svg


# ---------------------------------------------------------------------------
# Background rect carries the chrome class
# ---------------------------------------------------------------------------


def test_background_rect_has_nf_metro_bg_class():
    """The canvas background rect should carry the nf-metro-bg class."""
    svg = render_svg(_make_graph(), NFCORE_THEME)
    assert "nf-metro-bg" in svg


# ---------------------------------------------------------------------------
# Class prefix propagates into chrome CSS selectors
# ---------------------------------------------------------------------------


def test_chrome_css_uses_namespaced_class_selectors():
    """With svg_class_prefix, the chrome CSS selectors use the prefixed class names."""
    svg = render_svg(_make_graph(), NFCORE_THEME, svg_class_prefix="mymap")
    # With prefix "mymap", nf-metro-bg becomes mymap-nf-metro-bg
    assert "mymap-nf-metro-bg" in svg
    # The unprefixed class should not appear in the chrome CSS selectors
    assert ".nf-metro-bg" not in svg


# ---------------------------------------------------------------------------
# Legend elements carry chrome classes
# ---------------------------------------------------------------------------


def test_legend_background_has_nf_metro_legend_bg_class():
    """The legend background rect should carry the nf-metro-legend-bg class."""
    svg = render_svg(_make_graph(), NFCORE_THEME)
    assert "nf-metro-legend-bg" in svg


def test_legend_text_has_nf_metro_legend_text_class():
    """Legend text entries should carry the nf-metro-legend-text class."""
    svg = render_svg(_make_graph(), NFCORE_THEME)
    assert "nf-metro-legend-text" in svg


# ---------------------------------------------------------------------------
# chrome_css=False: concrete colors for raster export (cairosvg)
# ---------------------------------------------------------------------------


def test_chrome_css_false_omits_var_references():
    """chrome_css=False emits no var() so non-CSS-custom-property renderers cope."""
    svg = render_svg(_make_graph(), NFCORE_THEME, chrome_css=False)
    assert "var(--nfm" not in svg


def test_chrome_css_false_keeps_concrete_chrome_colors():
    """Dropping the var() block leaves concrete colors baked on chrome elements."""
    svg = render_svg(_make_graph(), NFCORE_THEME, chrome_css=False)
    # The background rect and section boxes keep their theme fills.
    assert f'fill="{NFCORE_THEME.background_color}"' in svg
    assert f'fill="{NFCORE_THEME.section_fill}"' in svg


def test_chrome_css_default_emits_var_block():
    """The default keeps the var() block so a host can recolor the map live."""
    svg = render_svg(_make_graph(), NFCORE_THEME)
    assert "var(--nfm-bg" in svg


def test_chrome_css_false_rasterizes_with_cairosvg():
    """chrome_css=False output is consumable by cairosvg, which cannot parse var()."""
    cairosvg = pytest.importorskip("cairosvg")
    svg = render_svg(_make_graph(), NFCORE_THEME, chrome_css=False)
    png = cairosvg.svg2png(bytestring=svg.encode())
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
