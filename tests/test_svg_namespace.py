"""Tests for SVG class namespacing and dark-mode CSS opt-out."""

import re

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.svg import render_svg
from nf_metro.themes import LIGHT_THEME, NFCORE_THEME

_MMD = (
    "%%metro title: Test\n"
    "%%metro line: main | Main | #ff0000\n"
    "graph LR\n"
    "    subgraph s1 [Step One]\n"
    "        a[Input]\n"
    "    end\n"
    "    subgraph s2 [Step Two]\n"
    "        b[Output]\n"
    "    end\n"
    "    a -->|main| b\n"
)


def _make_graph():
    g = parse_metro_mermaid(_MMD)
    compute_layout(g)
    return g


# ---------------------------------------------------------------------------
# Dark-mode CSS injection
# ---------------------------------------------------------------------------


def test_dark_mode_css_present_for_transparent_theme_by_default():
    """LIGHT_THEME (background_color=none) injects the dark-mode block by default."""
    svg = render_svg(_make_graph(), LIGHT_THEME)
    assert "prefers-color-scheme" in svg


def test_dark_mode_css_absent_for_opaque_theme():
    """NFCORE_THEME (opaque background) does not inject the dark-mode block."""
    svg = render_svg(_make_graph(), NFCORE_THEME)
    assert "prefers-color-scheme" not in svg


def test_inject_dark_mode_css_false_suppresses_block():
    """inject_dark_mode_css=False suppresses the media query regardless of theme."""
    svg = render_svg(_make_graph(), LIGHT_THEME, inject_dark_mode_css=False)
    assert "prefers-color-scheme" not in svg


def test_inject_dark_mode_css_true_matches_default():
    """inject_dark_mode_css=True is equivalent to the default for transparent themes."""
    svg_default = render_svg(_make_graph(), LIGHT_THEME)
    svg_explicit = render_svg(_make_graph(), LIGHT_THEME, inject_dark_mode_css=True)
    assert svg_default == svg_explicit


# ---------------------------------------------------------------------------
# SVG class namespacing
# ---------------------------------------------------------------------------

_CLASS_PATTERN = re.compile(r'class="([^"]*)"')
_ALL_KNOWN_CLASSES = {
    "nf-metro-title",
    "nf-metro-section-box",
    "nf-metro-section-num-circle",
    "nf-metro-section-label",
    "nf-metro-station",
    "nf-metro-station-group",
    "nf-metro-station-label",
    "nf-metro-marker-stroke",
    "nf-metro-group-underline",
    "nf-metro-group-label",
    "nf-metro-rail-connector",
    "nf-metro-rail-knob-outline",
    "nf-metro-rail-knob",
}


def _extract_classes(svg: str) -> set[str]:
    return {cls for m in _CLASS_PATTERN.finditer(svg) for cls in m.group(1).split()}


def test_default_render_uses_unprefixed_classes():
    """Without a prefix the class names must be the documented bare names."""
    svg = render_svg(_make_graph(), LIGHT_THEME)
    classes = _extract_classes(svg)
    # At minimum title, section-box, section-label, station, station-label present
    assert "nf-metro-title" in classes
    assert "nf-metro-section-box" in classes
    assert "nf-metro-station" in classes
    # No class should start with an extra dash-prefix
    assert not any(c.startswith("abc-nf-metro-") for c in classes)


def test_svg_class_prefix_applied_to_all_classes():
    """svg_class_prefix='abc' prepends abc- to every SVG presentation class."""
    prefix = "abc"
    svg = render_svg(_make_graph(), LIGHT_THEME, svg_class_prefix=prefix)
    classes = _extract_classes(svg)

    # Unprefixed presentation classes must not appear
    assert "nf-metro-title" not in classes
    assert "nf-metro-section-box" not in classes
    assert "nf-metro-station" not in classes

    # Prefixed versions must be present
    assert "abc-nf-metro-title" in classes
    assert "abc-nf-metro-section-box" in classes
    assert "abc-nf-metro-station" in classes


def test_svg_class_prefix_applied_to_metro_line_class():
    """metro-line-<id> is also prefixed."""
    prefix = "mymap"
    svg = render_svg(_make_graph(), LIGHT_THEME, svg_class_prefix=prefix)
    assert "mymap-metro-line-main" in svg
    # Unprefixed form absent
    assert 'class="metro-line-main"' not in svg


def test_svg_class_prefix_applied_to_dark_mode_css_selectors():
    """Dark-mode CSS selectors are also prefixed so the block scopes correctly."""
    prefix = "ns1"
    svg = render_svg(_make_graph(), LIGHT_THEME, svg_class_prefix=prefix)
    assert "prefers-color-scheme" in svg
    # Selectors in the <style> block must use the prefix
    assert ".ns1-nf-metro-section-label" in svg
    assert ".ns1-nf-metro-title" in svg
    # Bare selectors must not be present in the CSS block
    style_block = re.search(r"<style>(.*?)</style>", svg, re.DOTALL)
    assert style_block is not None
    css = style_block.group(1)
    assert ".nf-metro-section-label" not in css
    assert ".nf-metro-title" not in css


def test_two_renders_with_different_prefixes_dont_share_presentation_classes():
    """Demonstrates the embedding isolation property."""
    g = _make_graph()
    svg1 = render_svg(g, LIGHT_THEME, svg_class_prefix="map1")
    svg2 = render_svg(g, LIGHT_THEME, svg_class_prefix="map2")

    classes1 = {c for c in _extract_classes(svg1) if c.startswith("map")}
    classes2 = {c for c in _extract_classes(svg2) if c.startswith("map")}

    assert classes1
    assert classes2
    assert classes1.isdisjoint(classes2)


def test_empty_prefix_is_identical_to_no_prefix():
    """Passing svg_class_prefix='' is the same as omitting it."""
    g = _make_graph()
    svg_default = render_svg(g, LIGHT_THEME)
    svg_empty = render_svg(g, LIGHT_THEME, svg_class_prefix="")
    assert svg_default == svg_empty


def test_manifest_ids_and_data_attrs_not_prefixed():
    """data-node-*, data-station-id, data-schema-version are not class names
    and must not be modified by the prefix."""
    prefix = "scoped"
    svg = render_svg(_make_graph(), LIGHT_THEME, svg_class_prefix=prefix)
    assert "data-station-id" in svg
    assert "scoped-data-station-id" not in svg
    assert "data-section-id" in svg
