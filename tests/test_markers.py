"""Tests for per-station marker styles and the marker legend."""

import xml.etree.ElementTree as ET
from dataclasses import replace

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MarkerStyle
from nf_metro.render.constants import MARKER_PILL_LENGTH_RATIO
from nf_metro.render.legend import (
    marker_corner_radius,
    marker_fill_color,
    marker_stroke_color,
)
from nf_metro.render.svg import render_svg
from nf_metro.themes import NFCORE_THEME

_BASE = (
    "%%metro line: a | Line A | #4CAF50\n"
    "graph LR\n"
    "    n1[One]\n"
    "    n2[Two]\n"
    "    n3[Three]\n"
    "    n1 -->|a| n2\n"
    "    n2 -->|a| n3\n"
)


def _parse(extra: str = ""):
    return parse_metro_mermaid(extra + _BASE)


# --- Parser: per-station marker directive ------------------------------------


def test_marker_directive_sets_shape_and_fill():
    g = _parse("%%metro marker: n2 | square, open\n")
    assert g.stations["n2"].marker == MarkerStyle(shape="square", fill="open")


def test_marker_directive_literal_colour_fill():
    g = _parse("%%metro marker: n2 | square, #4CAF50\n")
    assert g.stations["n2"].marker == MarkerStyle(shape="square", fill="#4CAF50")


def test_marker_directive_defaults_circle_solid():
    g = _parse("%%metro marker: n2 |\n")
    assert g.stations["n2"].marker == MarkerStyle(shape="circle", fill="solid")


def test_marker_directive_unmarked_station_has_no_marker():
    g = _parse("%%metro marker: n2 | square, solid\n")
    assert g.stations["n1"].marker is None
    assert g.stations["n3"].marker is None


def test_marker_directive_unknown_shape_warns_and_ignores():
    with pytest.warns(UserWarning):
        g = _parse("%%metro marker: n2 | triangle, solid\n")
    assert g.stations["n2"].marker is None


def test_marker_directive_precedes_node_definition():
    # Directive can appear before the node is parsed.
    g = parse_metro_mermaid(
        "%%metro line: a | Line A | #4CAF50\n"
        "%%metro marker: n2 | square, open\n"
        "graph LR\n"
        "    n1[One]\n"
        "    n2[Two]\n"
        "    n1 -->|a| n2\n"
    )
    assert g.stations["n2"].marker == MarkerStyle(shape="square", fill="open")


# --- Parser: marker legend directive -----------------------------------------


def test_marker_legend_directive_collects_entries():
    g = _parse(
        "%%metro marker_legend: square, solid | Mandatory\n"
        "%%metro marker_legend: circle, open | Optional\n"
    )
    assert [e.caption for e in g.marker_legend] == ["Mandatory", "Optional"]
    assert g.marker_legend[0].style == MarkerStyle(shape="square", fill="solid")


def test_marker_legend_directive_requires_caption():
    with pytest.warns(UserWarning):
        g = _parse("%%metro marker_legend: square, solid\n")
    assert g.marker_legend == []


def test_marker_legend_default_empty():
    g = _parse()
    assert g.marker_legend == []


# --- Fill resolution ---------------------------------------------------------


def test_marker_fill_color_keywords_and_literal():
    assert marker_fill_color("solid", NFCORE_THEME) == NFCORE_THEME.station_fill
    assert marker_fill_color("#abcdef", NFCORE_THEME) == "#abcdef"
    # open falls back to the background colour on the dark theme.
    assert marker_fill_color("open", NFCORE_THEME) == NFCORE_THEME.background_color


# --- Pill shape --------------------------------------------------------------


def test_marker_corner_radius_per_shape():
    r = 5.0
    assert marker_corner_radius("circle", r) == r
    assert marker_corner_radius("pill", r) == r
    assert marker_corner_radius("square", r) == 0.0


def test_pill_marker_directive_parses():
    g = _parse("%%metro marker: n2 | pill, solid\n")
    assert g.stations["n2"].marker == MarkerStyle(shape="pill", fill="solid")


def test_pill_marker_is_capsule_elongated_along_the_line():
    # On a single-line LR station a pill is a horizontal capsule: a fully
    # rounded rect (rx == r) wider than it is tall, elongated along the line.
    g = _parse("%%metro marker: n2 | pill, #4CAF50\n")
    compute_layout(g)
    svg = render_svg(g, NFCORE_THEME)
    root = ET.fromstring(svg)
    ns = "{http://www.w3.org/2000/svg}"
    pills = [
        el
        for el in root.iter(f"{ns}rect")
        if el.get("data-station-id") == "n2" and el.get("fill") == "#4CAF50"
    ]
    assert pills, "expected a pill marker rect"
    r = NFCORE_THEME.station_radius
    for el in pills:
        w, h = float(el.get("width")), float(el.get("height"))
        assert float(el.get("rx")) == r
        assert w == r * MARKER_PILL_LENGTH_RATIO
        assert w > h  # flat-edged capsule lying along the line


def test_pill_marker_grows_across_multiple_tracks():
    # When the station carries several lines, the pill widens across the
    # bundle (its cross-axis extent) instead of staying one track tall.
    multi = (
        "%%metro line: a | A | #4CAF50\n"
        "%%metro line: b | B | #e63946\n"
        "%%metro marker: n2 | pill, solid\n"
        "graph LR\n"
        "    n1[One]\n"
        "    n2[Two]\n"
        "    n3[Three]\n"
        "    n1 -->|a,b| n2\n"
        "    n2 -->|a,b| n3\n"
    )
    g = parse_metro_mermaid(multi)
    compute_layout(g)
    svg = render_svg(g, NFCORE_THEME)
    root = ET.fromstring(svg)
    ns = "{http://www.w3.org/2000/svg}"
    pills = [el for el in root.iter(f"{ns}rect") if el.get("data-station-id") == "n2"]
    assert pills, "expected a pill marker rect"
    # Two tracks => taller than a single-track pill (> 2r).
    assert any(
        float(el.get("height")) > 2 * NFCORE_THEME.station_radius for el in pills
    )


# --- Rendering ---------------------------------------------------------------


def test_marker_station_renders_valid_svg():
    g = _parse(
        "%%metro marker: n2 | square, #4CAF50\n"
        "%%metro marker_legend: square, #4CAF50 | Accelerated\n"
    )
    compute_layout(g)
    svg = render_svg(g, NFCORE_THEME)
    ET.fromstring(svg)  # well-formed
    assert "#4CAF50" in svg
    assert "Accelerated" in svg


def test_no_marker_directives_byte_identical():
    # A diagram with no marker directives must render exactly as before the
    # feature: markers default-off.
    g_plain = _parse()
    g_with = _parse("%%metro marker: n2 | square, solid\n")
    # Remove the marker so the only difference is the (now-cleared) directive.
    g_with.stations["n2"].marker = None
    compute_layout(g_plain)
    compute_layout(g_with)
    assert render_svg(g_plain, NFCORE_THEME) == render_svg(g_with, NFCORE_THEME)


# --- Marker outline visibility ----------------------------------------------


def test_marker_stroke_color_uses_theme_marker_stroke():
    # The dark theme sets a dedicated light marker outline so dark-filled
    # markers stay visible against the background.
    assert NFCORE_THEME.marker_stroke
    assert marker_stroke_color(NFCORE_THEME) == NFCORE_THEME.marker_stroke
    assert marker_stroke_color(NFCORE_THEME) != NFCORE_THEME.station_stroke


def test_marker_stroke_color_falls_back_to_station_stroke():
    # A theme that does not set marker_stroke inherits station_stroke.
    theme = replace(NFCORE_THEME, marker_stroke="")
    assert marker_stroke_color(theme) == theme.station_stroke


def test_dark_marker_glyph_has_light_outline():
    # A dark-filled marker pill must be drawn with the light marker stroke,
    # not the dark station stroke, so it stands out on the dark background.
    g = _parse("%%metro marker: n2 | square, #1f4e79\n")
    compute_layout(g)
    svg = render_svg(g, NFCORE_THEME)
    root = ET.fromstring(svg)
    ns = "{http://www.w3.org/2000/svg}"
    marker_rects = [
        el
        for el in root.iter(f"{ns}rect")
        if el.get("data-station-id") == "n2" and el.get("fill") == "#1f4e79"
    ]
    assert marker_rects, "expected a dark-filled marker rect"
    for el in marker_rects:
        assert el.get("stroke") == NFCORE_THEME.marker_stroke
        assert el.get("stroke") != NFCORE_THEME.station_stroke


def test_marker_legend_swatch_has_light_outline():
    # The legend key swatch for a dark marker must also carry the light
    # outline so the legend matches the map.
    g = _parse("%%metro marker_legend: square, #1f4e79 | Accelerated\n")
    compute_layout(g)
    svg = render_svg(g, NFCORE_THEME)
    root = ET.fromstring(svg)
    ns = "{http://www.w3.org/2000/svg}"
    swatches = [el for el in root.iter(f"{ns}rect") if el.get("fill") == "#1f4e79"]
    assert swatches, "expected a dark-filled legend swatch"
    for el in swatches:
        assert el.get("stroke") == NFCORE_THEME.marker_stroke
