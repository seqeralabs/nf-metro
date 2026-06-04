"""Tests for the combined-lines legend entry (%%metro legend_combo:)."""

from __future__ import annotations

import warnings

import drawsvg as draw

from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.legend import render_legend
from nf_metro.themes import NFCORE_THEME

_BASE = (
    "%%metro line: normal | Normal | #2196F3\n"
    "%%metro line: tumor | Tumor | #E53935\n"
    "%%metro line: qc | Quality Control | #4CAF50 | dashed\n"
)
_GRAPH = "graph LR\n    a[A]\n    a -->|normal| b\n"


def _parse(directives: str = ""):
    return parse_metro_mermaid(_BASE + directives + _GRAPH)


def _render_swatches_and_texts(graph):
    """Return (stroke_colors, label_texts) from a rendered legend SVG."""
    d = draw.Drawing(400, 400)
    render_legend(d, graph, NFCORE_THEME, 0.0, 0.0)
    svg = d.as_svg()
    colors = [
        line.split('stroke="', 1)[1].split('"', 1)[0]
        for line in svg.splitlines()
        if line.startswith("<path")
    ]
    texts = [
        line.split(">", 1)[1].rsplit("</text>", 1)[0]
        for line in svg.splitlines()
        if line.startswith("<text")
    ]
    return colors, texts


def test_combo_row_renders_multicolour_swatch_and_label():
    graph = _parse("%%metro legend_combo: normal, tumor | Tumor-normal pair\n")
    colors, texts = _render_swatches_and_texts(graph)
    # The combo label appears exactly once.
    assert texts.count("Tumor-normal pair") == 1
    # Both constituent colours are drawn as swatch stripes.
    assert "#2196F3" in colors
    assert "#E53935" in colors


def test_combo_constituent_lines_suppressed_from_individual_rows():
    graph = _parse("%%metro legend_combo: normal, tumor | Tumor-normal pair\n")
    _colors, texts = _render_swatches_and_texts(graph)
    # The individual line names are NOT rendered as their own rows.
    assert "Normal" not in texts
    assert "Tumor" not in texts
    assert "Tumor-normal pair" in texts


def test_non_combo_line_still_gets_its_row():
    graph = _parse("%%metro legend_combo: normal, tumor | Tumor-normal pair\n")
    _colors, texts = _render_swatches_and_texts(graph)
    # The QC line is not in any combo and keeps its own row.
    assert "Quality Control" in texts


def test_default_off_byte_identical_legend():
    """With no legend_combo directive the legend SVG is byte-identical."""
    graph = _parse()  # no combo directive
    d = draw.Drawing(400, 400)
    render_legend(d, graph, NFCORE_THEME, 0.0, 0.0)
    svg = d.as_svg()
    # Each line renders exactly one swatch + one label, in definition order.
    colors, texts = _render_swatches_and_texts(graph)
    assert texts == ["Normal", "Tumor", "Quality Control"]
    assert colors == ["#2196F3", "#E53935", "#4CAF50"]
    # And nothing combined leaked in.
    assert "Tumor-normal pair" not in svg


def test_combo_with_unknown_line_warns_and_ignores():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        graph = _parse("%%metro legend_combo: normal, nope | Pair\n")
    # Unknown member dropped; fewer than two known lines -> combo ignored.
    assert graph.legend_combos == []
    assert any("unknown" in str(w.message).lower() for w in caught)


def test_combo_drops_unknown_keeps_known_members():
    graph = _parse("%%metro legend_combo: normal, tumor, ghost | Trio\n")
    assert len(graph.legend_combos) == 1
    line_ids, label = graph.legend_combos[0]
    assert line_ids == ("normal", "tumor")
    assert label == "Trio"


def test_combo_requires_two_lines_and_label():
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        g1 = _parse("%%metro legend_combo: normal | Solo\n")
        g2 = _parse("%%metro legend_combo: normal, tumor |\n")
    assert g1.legend_combos == []
    assert g2.legend_combos == []
