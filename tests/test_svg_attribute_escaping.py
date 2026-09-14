"""Directive-authored text (line colours, marker fills) reaches SVG attribute
values verbatim through drawsvg, which escapes element text content but not
attribute values. A literal ``"`` in one of these values must not break out
of its attribute; ``html.escape`` at the two chokepoints
(``effective_line_color`` / ``marker_fill_color``) closes that off.
"""

import xml.etree.ElementTree as ET

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.svg import render_svg
from nf_metro.themes import NFCORE_DARK_THEME

_BREAKOUT = 'red" onload="alert(1)'


def _no_injected_attribute(svg: str) -> None:
    root = ET.fromstring(svg)
    for el in root.iter():
        assert "onload" not in el.attrib, f"{el.tag} attrib={el.attrib}"


def test_line_color_breakout_is_escaped_in_edge_and_legend_swatch():
    src = (
        "%%metro title: Test\n"
        f"%%metro line: evil | Evil | {_BREAKOUT}\n"
        "graph LR\n"
        "    a[Input]\n"
        "    b[Output]\n"
        "    a -->|evil| b\n"
    )
    graph = parse_metro_mermaid(src)
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_DARK_THEME)

    _no_injected_attribute(svg)
    assert "alert(1)" in svg
    assert 'onload="alert(1)"' not in svg


def test_marker_fill_breakout_is_escaped_in_station_and_marker_legend():
    src = (
        "%%metro title: Test\n"
        "%%metro line: main | Main | #4caf50\n"
        f"%%metro marker: a | circle, {_BREAKOUT}\n"
        f"%%metro marker_legend: circle, {_BREAKOUT} | Caption\n"
        "graph LR\n"
        "    a[Input]\n"
        "    b[Output]\n"
        "    a -->|main| b\n"
    )
    graph = parse_metro_mermaid(src)
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_DARK_THEME)

    _no_injected_attribute(svg)
    assert "alert(1)" in svg
    assert 'onload="alert(1)"' not in svg


@pytest.mark.parametrize(
    "color",
    ["#4caf50", "red", "rgb(1,2,3)", "var(--x)", "light-dark(#fff,#000)"],
)
def test_legitimate_colors_render_unescaped(color):
    """html.escape must be a no-op on every legitimate CSS colour form."""
    src = (
        "%%metro title: Test\n"
        f"%%metro line: main | Main | {color}\n"
        "graph LR\n"
        "    a[Input]\n"
        "    b[Output]\n"
        "    a -->|main| b\n"
    )
    graph = parse_metro_mermaid(src)
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_DARK_THEME)

    assert color in svg
    assert "&amp;" not in svg
