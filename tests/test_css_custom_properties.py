"""Tests for CSS custom property injection for chrome color theming.

Chrome colors (background, labels, section, title, legend) are driven through
``--nfm-map-*`` CSS custom properties so a host can recolor without
re-rendering.  Each property's fallback is the ``light-dark()`` of the theme's
light and dark palettes (single value for themes with no light/dark family), so
the map follows the viewer's ``color-scheme``.  Line/route colors remain baked
as presentation attributes (semantic).
"""

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.svg import render_svg
from nf_metro.themes import (
    LIGHT_THEME,
    NFCORE_DARK_THEME,
    NFCORE_LIGHT_THEME,
    NFCORE_THEME,
)

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


@pytest.mark.parametrize(
    "prop",
    [
        "--nfm-map-bg",
        "--nfm-map-label-color",
        "--nfm-map-title-color",
        "--nfm-map-section-fill",
        "--nfm-map-section-stroke",
        "--nfm-map-section-label-color",
        "--nfm-map-legend-bg",
        "--nfm-map-legend-text-color",
    ],
)
def test_svg_declares_chrome_property(prop):
    """Each recolorable chrome surface declares its ``--nfm-map-*`` property."""
    svg = render_svg(_make_graph(), NFCORE_THEME)
    assert prop in svg


# ---------------------------------------------------------------------------
# Fallback values are the light-dark() of both mode palettes
# ---------------------------------------------------------------------------


def test_chrome_css_fallbacks_are_light_dark_of_both_palettes():
    """Fallbacks pair the light and dark palette values via ``light-dark()``."""
    svg = render_svg(_make_graph(), NFCORE_THEME)
    light, dark = NFCORE_LIGHT_THEME, NFCORE_DARK_THEME
    assert (
        f"--nfm-map-bg, light-dark({light.background_color}, {dark.background_color})"
        in svg
    )
    assert (
        f"--nfm-map-label-color, light-dark({light.label_color}, {dark.label_color})"
        in svg
    )
    assert (
        f"--nfm-map-title-color, light-dark({light.title_color}, {dark.title_color})"
        in svg
    )
    assert (
        f"--nfm-map-section-fill, light-dark({light.section_fill}, {dark.section_fill})"
        in svg
    )
    assert (
        "--nfm-map-section-label-color, "
        f"light-dark({light.section_label_color}, {dark.section_label_color})" in svg
    )


def test_root_declares_color_scheme_for_mode_family():
    """A branded theme tags the root <svg> with ``color-scheme: light dark``."""
    svg = render_svg(_make_graph(), NFCORE_THEME)
    assert "color-scheme: light dark" in svg


def test_self_color_scheme_false_omits_root_color_scheme():
    """Inlined renders omit the root color-scheme so the page's choice wins."""
    svg = render_svg(_make_graph(), NFCORE_THEME, self_color_scheme=False)
    assert "color-scheme" not in svg


def test_chrome_css_fallbacks_single_value_for_unfamilied_theme():
    """A theme with no light/dark family falls back to its single baked value.

    Transparent themes (background_color='none') emit no ``.nf-metro-bg`` fill
    rule; the halo knockout references --nfm-map-bg with a solid fallback.
    """
    theme = LIGHT_THEME
    svg = render_svg(_make_graph(), theme)
    assert ".nf-metro-bg {" not in svg
    assert "light-dark(" not in svg
    assert f"--nfm-map-label-color, {theme.label_color}" in svg
    assert f"--nfm-map-title-color, {theme.title_color}" in svg
    assert f"--nfm-map-section-fill, {theme.section_fill}" in svg
    assert f"--nfm-map-section-stroke, {theme.section_stroke}" in svg
    assert f"--nfm-map-section-label-color, {theme.section_label_color}" in svg


# ---------------------------------------------------------------------------
# Line/route colors remain baked (semantic, not chrome)
# ---------------------------------------------------------------------------


def test_line_colors_not_in_chrome_css_vars():
    """Line colors must NOT appear inside CSS custom property declarations."""
    g = _make_graph()
    svg = render_svg(g, NFCORE_THEME)
    for line in g.lines.values():
        assert f"--nfm-map-line-{line.id}" not in svg
        assert line.color in svg


# ---------------------------------------------------------------------------
# Chrome classes on the elements
# ---------------------------------------------------------------------------


def test_background_rect_has_nf_metro_bg_class():
    """The canvas background rect should carry the nf-metro-bg class."""
    svg = render_svg(_make_graph(), NFCORE_THEME)
    assert "nf-metro-bg" in svg


def test_chrome_css_uses_namespaced_class_selectors():
    """With svg_class_prefix, the chrome CSS selectors use the prefixed class names."""
    svg = render_svg(_make_graph(), NFCORE_THEME, svg_class_prefix="mymap")
    assert "mymap-nf-metro-bg" in svg
    assert ".nf-metro-bg" not in svg


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
    assert "light-dark(" not in svg


def test_chrome_css_false_keeps_concrete_chrome_colors():
    """Dropping the var() block leaves the resolved mode's colors baked on chrome."""
    svg = render_svg(_make_graph(), NFCORE_THEME, chrome_css=False)
    assert f'fill="{NFCORE_THEME.background_color}"' in svg
    assert f'fill="{NFCORE_THEME.section_fill}"' in svg


def test_chrome_css_default_emits_var_block():
    """The default keeps the var() block so a host can recolor the map live."""
    svg = render_svg(_make_graph(), NFCORE_THEME)
    assert "var(--nfm-map-bg" in svg


def test_chrome_css_false_rasterizes_with_cairosvg():
    """chrome_css=False output is consumable by cairosvg, which cannot parse var()."""
    cairosvg = pytest.importorskip("cairosvg")
    svg = render_svg(_make_graph(), NFCORE_THEME, chrome_css=False)
    png = cairosvg.svg2png(bytestring=svg.encode())
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
