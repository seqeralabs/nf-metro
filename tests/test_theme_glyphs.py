"""Glyph styles: tabs, terminus bars and interchange rings, as sized and drawn."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import replace

import drawsvg as draw
import pytest
from theme_helpers import MMD, RNASEQ, SVG_NS, classed, station_glyphs, write

from nf_metro.api import render_string
from nf_metro.render.svg import (
    _Bundle,
    _CrossAxis,
    _draw_ring_chain,
    bundle_end_bar_box,
    ring_groups,
    ring_layout,
    station_tab_box,
)
from nf_metro.themes import NFCORE_LIGHT_THEME, resolve_stack

_TUBE_DARK = resolve_stack("london-tube").dark
_MAP = RNASEQ.read_text().replace("%%metro style: nfcore", "")


def _render(theme: str, **kwargs) -> ET.Element:
    return ET.fromstring(
        render_string(_MAP, theme=theme, source_dir=str(RNASEQ.parent), **kwargs)
    )


@pytest.fixture(scope="module")
def tube_svg() -> ET.Element:
    return _render("london-tube")


@pytest.fixture(scope="module")
def tube_ring_svg(tmp_path_factory) -> ET.Element:
    theme = tmp_path_factory.mktemp("ring") / "ring.yaml"
    theme.write_text("name: r\nextends: london-tube\ninterchange_glyph: ring\n")
    return _render(str(theme))


def _terminus_svg(tmp_path, glyph: str) -> ET.Element:
    theme = write(
        tmp_path,
        f"{glyph}.yaml",
        f"name: t\nextends: london-tube\nterminus_glyph: {glyph}\n",
    )
    return _render(str(theme), mode="dark")


# ---------------------------------------------------------------------------
# What london-tube draws
# ---------------------------------------------------------------------------


def test_one_line_stations_are_track_weight_tabs_in_their_line_colour(tube_svg):
    rsem = station_glyphs(tube_svg)["rsem"]
    assert float(rsem.get("width")) == pytest.approx(1.0 * 4.0)
    assert float(rsem.get("height")) == pytest.approx(1.5 * 4.0)
    assert rsem.get("fill") == "#0570b0"


def test_interchanges_are_bold_capsules_in_the_station_colours(tube_svg):
    star = station_glyphs(tube_svg)["star"]
    assert "nf-metro-capsule" in star.get("class").split()
    assert float(star.get("rx")) == pytest.approx(float(star.get("width")) / 2)
    assert float(star.get("stroke-width")) == pytest.approx(2.5)
    assert star.get("stroke") == _TUBE_DARK.station_stroke
    assert star.get("fill") == _TUBE_DARK.station_fill


def test_ring_interchanges_are_one_ring_over_two_lines(tube_ring_svg):
    star = station_glyphs(tube_ring_svg, "g")["star"]
    rings = classed(star, "circle", "nf-metro-ring")
    assert len(rings) == 1
    assert float(rings[0].get("r")) == pytest.approx(4.0 + 1.0)
    assert float(rings[0].get("stroke-width")) == pytest.approx(2.0)
    assert rings[0].get("stroke") == _TUBE_DARK.station_stroke


def test_ring_interchanges_chain_over_three_lines(tube_ring_svg):
    samtools = station_glyphs(tube_ring_svg, "g")["samtools"]
    assert len(classed(samtools, "circle", "nf-metro-ring")) == 2


def test_london_tube_ends_a_six_line_bundle_with_a_bar(tube_svg):
    fastq = station_glyphs(tube_svg)["fastq_in"]
    assert "nf-metro-terminus-bar" in fastq.get("class")


def test_a_nub_line_end_takes_the_station_colours(tmp_path):
    fastq = station_glyphs(_terminus_svg(tmp_path, "nub"))["fastq_in"]
    assert fastq.get("rx") is None
    assert fastq.get("fill") == _TUBE_DARK.station_fill
    assert fastq.get("stroke") == _TUBE_DARK.station_stroke
    assert "nf-metro-capsule" in fastq.get("class").split()


def test_a_bar_line_end_spans_the_bundle_at_track_weight(tmp_path):
    bar = station_glyphs(_terminus_svg(tmp_path, "bar"))["fastq_in"]
    assert "nf-metro-terminus-bar" in bar.get("class")
    lw = 4.0
    assert float(bar.get("width")) == pytest.approx(1.0 * lw)
    span = 5 * lw
    assert float(bar.get("height")) == pytest.approx(span + lw + 2 * 1.5 * lw)
    assert bar.get("fill") == _TUBE_DARK.station_stroke


def test_a_ring_line_end_keeps_the_bridged_chain(tmp_path):
    fastq = station_glyphs(_terminus_svg(tmp_path, "ring"), "g")["fastq_in"]
    assert len(classed(fastq, "circle", "nf-metro-ring")) == 3
    assert classed(fastq, "rect", "nf-metro-ring-bridge")


def test_ring_holes_take_the_station_fill(tmp_path):
    rings = write(tmp_path, "rings.yaml", "name: rings\ninterchange_glyph: ring\n")
    root = ET.fromstring(render_string(MMD, theme=f"seqera, {rings}"))
    seqera = resolve_stack("seqera").dark
    holes = classed(root, "circle", "nf-metro-ring-hole")
    assert holes and {h.get("fill") for h in holes} == {seqera.station_fill}
    outlines = classed(root, "circle", "nf-metro-ring")
    assert {o.get("stroke") for o in outlines} == {seqera.station_stroke}


def test_sections_are_unboxed_with_plain_uppercase_titles(tube_svg):
    assert not classed(tube_svg, "circle", "nf-metro-section-num-circle")
    boxes = classed(tube_svg, "rect", "nf-metro-section-box")
    assert boxes and all(b.get("stroke-dasharray") == "6 4" for b in boxes)
    titles = [el.text for el in classed(tube_svg, "text", "nf-metro-section-label")]
    assert "PRE-PROCESSING" in titles


def test_nfcore_render_is_untouched_by_the_shape_fields():
    root = _render("nfcore")
    assert station_glyphs(root)["rsem"].get("rx") is not None
    assert classed(root, "circle", "nf-metro-section-num-circle")


# ---------------------------------------------------------------------------
# Tab, terminus bar and ring geometry, in multiples of the line width
# ---------------------------------------------------------------------------


class _At:
    def __init__(self, x: float, y: float) -> None:
        self.x, self.y = x, y


_LW = 4.0
_GEOM = replace(NFCORE_LIGHT_THEME, line_width=_LW)


@pytest.mark.parametrize("label_below", [True, False])
def test_tab_sits_on_the_label_side_of_the_line(label_below):
    label = (100.0, 60.0 if label_below else 40.0)
    x, y, w, h = station_tab_box(
        _Bundle(_At(100.0, 50.0), 0.0, 0.0, False), _GEOM, label
    )
    assert (w, h) == pytest.approx((0.66 * _LW, 0.66 * _LW))
    assert x + w / 2 == pytest.approx(100.0)
    if label_below:
        assert y == pytest.approx(50.0 + _LW / 2)
    else:
        assert y + h == pytest.approx(50.0 - _LW / 2)


def test_tab_width_and_length_are_separate_multiples_of_the_line():
    theme = replace(_GEOM, station_tick_width=1.0, station_tick_length=1.5)
    _, _, w, h = station_tab_box(
        _Bundle(_At(100.0, 50.0), 0.0, 0.0, False), theme, None
    )
    assert (w, h) == pytest.approx((1.0 * _LW, 1.5 * _LW))


def test_tab_in_a_vertical_section_faces_its_label_sideways():
    x, y, w, h = station_tab_box(
        _Bundle(_At(100.0, 50.0), 0.0, 0.0, True), _GEOM, (80.0, 50.0)
    )
    assert x + w == pytest.approx(100.0 - _LW / 2)
    assert y + h / 2 == pytest.approx(50.0)


@pytest.mark.parametrize("vertical", [False, True])
def test_terminus_bar_crosses_the_line_end_and_overhangs_both_sides(vertical):
    theme = replace(_GEOM, station_tick_width=1.0, station_tick_length=1.5)
    axis = 100.0 if vertical else 50.0
    x, y, w, h = bundle_end_bar_box(
        _Bundle(_At(100.0, 50.0), 0.0, 0.0, vertical), theme
    )
    thick, across = 1.0 * _LW, _LW + 2 * 1.5 * _LW
    if vertical:
        assert (w, h) == pytest.approx((across, thick))
        assert x + w / 2 == pytest.approx(axis)
    else:
        assert (w, h) == pytest.approx((thick, across))
        assert y + h / 2 == pytest.approx(axis)


_STEP = (2 * 1.0 + 0.5) * _LW
_OUTER = (1.0 + 0.5) * _LW


@pytest.mark.parametrize(
    "lines,max_lines,rings",
    [(2, 3, 1), (3, 3, 1), (4, 3, 2), (6, 3, 3), (10, 3, 4), (2, 2, 1), (3, 2, 2)],
)
def test_ring_chain_length_follows_bundle_width(lines, max_lines, rings):
    theme = replace(_GEOM, ring_max_lines=max_lines)
    span = (lines - 1) * _LW
    layout = ring_layout(-span / 2, span / 2, lines, theme)
    assert len(layout.centres) == rings
    assert layout.inner == pytest.approx(_LW)


@pytest.mark.parametrize("lines,rings", [(3, 2), (6, 3), (10, 4)])
def test_chain_rings_sit_one_natural_step_apart_and_cover_the_bundle(lines, rings):
    theme = replace(_GEOM, ring_max_lines=2)
    lo, hi = 7.0, 7.0 + (lines - 1) * _LW
    centres = ring_layout(lo, hi, lines, theme).centres
    assert len(centres) == rings
    assert [b - a for a, b in zip(centres, centres[1:])] == [pytest.approx(_STEP)] * (
        rings - 1
    )
    assert (centres[0] + centres[-1]) / 2 == pytest.approx((lo + hi) / 2)
    assert centres[0] - _OUTER <= lo - _LW / 2 + 1e-9
    assert centres[-1] + _OUTER >= hi + _LW / 2 - 1e-9


def test_every_chain_has_the_same_neck():
    theme = replace(_GEOM, ring_max_lines=2)
    necks = set()
    for lines in (3, 4, 6, 10):
        centres = ring_layout(0.0, (lines - 1) * _LW, lines, theme).centres
        for a, b in zip(centres, centres[1:]):
            half_gap = (b - a) / 2
            necks.add(round(math.sqrt(_OUTER**2 - half_gap**2), 9))
    assert len(necks) == 1


def test_grow_widens_one_ring_over_the_bundle():
    theme = replace(_GEOM, ring_max_lines=2, ring_overflow="grow")
    layout = ring_layout(0.0, 5 * _LW, 6, theme)
    assert layout.centres == [pytest.approx(2.5 * _LW)]
    assert layout.inner == pytest.approx(3 * _LW)


def test_ring_geometry_scales_with_line_width():
    wide = replace(_GEOM, line_width=2 * _LW)
    assert len(ring_layout(0.0, 4 * _LW, 5, _GEOM).centres) == len(
        ring_layout(0.0, 8 * _LW, 5, wide).centres
    )


@pytest.mark.parametrize("lines", [2, 3, 4, 6, 8])
def test_rings_are_centred_on_their_bundle(lines):
    lo = 10.0
    hi = lo + (lines - 1) * _LW
    centres = ring_layout(lo, hi, lines, _GEOM).centres
    assert sum(centres) / len(centres) == pytest.approx((lo + hi) / 2)


def test_separate_tracks_get_separate_ring_groups():
    groups = ring_groups([0.0, _LW, 40.0, 40.0 + _LW], _GEOM)
    assert [g.centres for g in groups] == [
        [pytest.approx(_LW / 2)],
        [pytest.approx(40.0 + _LW / 2)],
    ]


def test_bridge_strip_cuts_through_where_ring_outlines_meet():
    theme = replace(_GEOM, ring_max_lines=2)
    layout = ring_layout(0.0, 2 * _LW, 3, theme)
    canvas = draw.Drawing(100, 100)
    _draw_ring_chain(canvas, [layout], _CrossAxis(0.0, 0.0, False), theme, {}, False)
    root = ET.fromstring(canvas.as_svg())
    strip = next(
        el
        for el in root.iter(f"{SVG_NS}rect")
        if "nf-metro-ring-bridge" in (el.get("class") or "")
    )
    top, bottom = layout.centres
    stroke = 0.5 * _LW
    overlap_lo = bottom - layout.inner - stroke
    overlap_hi = top + layout.inner + stroke
    y, h = float(strip.get("y")), float(strip.get("height"))
    assert y <= overlap_lo and y + h >= overlap_hi
    kinds = [el.get("class") for el in root.iter() if el.get("class")]
    last_outline = max(i for i, k in enumerate(kinds) if k == "nf-metro-ring")
    assert kinds.index("nf-metro-ring-bridge") > last_outline
