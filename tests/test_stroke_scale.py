"""``stroke_scale``: coarsened ink, and the pitch that has to track it."""

from __future__ import annotations

import warnings
from dataclasses import fields
from pathlib import Path

import pytest

from nf_metro.api import RenderConfig, prepare_graph, render_graph, resolve_theme
from nf_metro.layout.constants import (
    DEFAULT_LINE_WIDTH,
    DIRECTIONAL_MARKER_HALF_EXTENT,
    EDGE_TO_BUNDLE_CLEARANCE,
    OFFSET_STEP,
    STATION_RADIUS_APPROX,
    WIDEST_THEME_LINE_WIDTH,
    graph_offset_step,
    resolve_offset_step,
)
from nf_metro.layout.pass_metrics import station_radius_approx, stroke_scale_context
from nf_metro.layout.route_plan import build_route_plan_query
from nf_metro.layout.route_reservations import (
    CANVAS_EDGE_ON_NEGATIVE_SIDE,
    CanvasRegion,
    CanvasSide,
)
from nf_metro.parser import parse_metro_mermaid
from nf_metro.render.constants import RAIL_KNOB_RADIUS_RATIO
from nf_metro.render.svg import (
    _scale_theme_fonts,
    _scale_theme_strokes,
    build_observed_render_plan,
)

_SRC = """%%metro title: Bundle
%%metro line: a | A | #e41a1c
%%metro line: b | B | #377eb8
%%metro line: c | C | #4daf4a

graph LR
    in[Input] -->|a,b,c| split[Split]
    split -->|a| one[One]
    split -->|b| two[Two]
    split -->|c| three[Three]
"""


def _graph(**opts: object):
    return prepare_graph(_SRC, layout_options=opts)


@pytest.mark.parametrize("track_gap", [None, 0.0, 1.0, 2.5])
def test_offset_step_unchanged_at_unit_scale(track_gap: float | None) -> None:
    """Unit scale must reproduce the unscaled pitch on both resolution paths.

    The whole corpus rendering byte-identically by default rests on this: a
    graph that never sets ``stroke_scale`` has to land on exactly the pitch
    ``resolve_offset_step`` yields for the same inputs.
    """
    graph = parse_metro_mermaid("graph LR\n")
    graph.track_gap = track_gap

    assert graph_offset_step(graph) == resolve_offset_step(track_gap)
    # The render path passes its theme's drawn width; unit scale leaves it be.
    assert graph_offset_step(graph, 4.0) == resolve_offset_step(track_gap, 4.0)


@pytest.mark.parametrize("scale", [1.3, 1.6, 2.0])
def test_pitch_scales_with_stroke(scale: float) -> None:
    """Gap and stroke coarsen together, so bundle lines stay separable.

    Scaling the stroke alone would hold the gap at its absolute default and
    close it up under the downscale the option exists to survive.
    """
    graph = parse_metro_mermaid("graph LR\n")

    graph.track_gap = None
    graph.stroke_scale = scale
    assert graph_offset_step(graph) == pytest.approx(OFFSET_STEP * scale)

    graph.track_gap = 2.0
    assert graph_offset_step(graph) == pytest.approx((2.0 + DEFAULT_LINE_WIDTH) * scale)


def test_pitch_clears_the_drawn_stroke() -> None:
    """The reserved pitch must stay wider than the stroke painted in it.

    A pitch that fell below the drawn width would paint adjacent lines of a
    bundle over each other.
    """
    graph = parse_metro_mermaid("graph LR\n")
    for scale in (1.0, 1.3, 1.6, 2.0, 3.0):
        graph.stroke_scale = scale
        for base_width in (3.0, 4.0):
            drawn = base_width * scale
            for gap in (None, 0.0, 1.0, 2.0):
                graph.track_gap = gap
                assert graph_offset_step(graph, drawn) >= drawn


def test_scaled_theme_coarsens_every_ink_dimension() -> None:
    graph = _graph()
    theme = resolve_theme(None, graph)
    scaled = _scale_theme_strokes(theme, 2.0)

    assert scaled.line_width == pytest.approx(theme.line_width * 2.0)
    assert scaled.station_stroke_width == pytest.approx(
        theme.station_stroke_width * 2.0
    )
    assert scaled.station_radius == pytest.approx(theme.station_radius * 2.0)


def test_the_two_scales_own_disjoint_theme_fields() -> None:
    """No theme field may be scaled by both, or the two compound on one render.

    ``render_svg`` applies them in sequence, so a field claimed by each ends up
    multiplied by ``font_scale * stroke_scale`` -- the label halo, which belongs
    to the text, read 2.08x on a map setting 1.3 font and 1.6 stroke.
    """
    theme = resolve_theme(None, _graph())
    font_only = _scale_theme_fonts(theme, 2.0)
    stroke_only = _scale_theme_strokes(theme, 2.0)

    moved_by_font = {
        f.name
        for f in fields(theme)
        if getattr(font_only, f.name) != getattr(theme, f.name)
    }
    moved_by_stroke = {
        f.name
        for f in fields(theme)
        if getattr(stroke_only, f.name) != getattr(theme, f.name)
    }

    assert moved_by_font & moved_by_stroke == set()
    assert "label_halo_width" in moved_by_font


@pytest.mark.parametrize("scale", [1.0, 1.6, 2.0])
def test_layout_reserves_against_the_drawn_pill(scale: float) -> None:
    """The radius layout reserves against must match the one the render draws.

    A pill grown only at render time would eat the label clearance and marker
    footprints that layout sized for an unscaled marker.
    """
    graph = _graph(stroke_scale=scale)
    drawn = _scale_theme_strokes(resolve_theme(None, graph), scale)

    with stroke_scale_context(scale):
        assert station_radius_approx() == pytest.approx(drawn.station_radius)


def test_interchange_glyph_keeps_its_proportions() -> None:
    """A coarsened spanning interchange enlarges rather than squashes.

    Its length comes from the rail pitch and its width from the stroke scale, so
    a pitch left fixed while the marker widens turns the glyph into a stub.  The
    ratio of the two is what has to hold, whatever the scale.
    """
    src = Path("examples/sarek_metro.mmd").read_text()

    def aspect(scale: float) -> float:
        graph = prepare_graph(
            src, source_dir="examples", layout_options={"stroke_scale": scale}
        )
        rail_ys = sorted(graph._rail_y["calling"].values())
        span = rail_ys[-1] - rail_ys[0]
        knob_width = 2.0 * STATION_RADIUS_APPROX * scale * RAIL_KNOB_RADIUS_RATIO
        return (span + knob_width) / knob_width

    unscaled = aspect(1.0)
    for scale in (1.3, 1.6, 2.0):
        assert aspect(scale) == pytest.approx(unscaled)


def test_pass_scales_do_not_leak() -> None:
    """Each scale is scoped to its pass, so an unscaled graph is unaffected."""
    with stroke_scale_context(2.0):
        assert station_radius_approx() == pytest.approx(STATION_RADIUS_APPROX * 2.0)
    assert station_radius_approx() == pytest.approx(STATION_RADIUS_APPROX)


def test_a_coarsened_canvas_margin_reads_back_outside_its_pass() -> None:
    """A coarsened canvas claim survives a query built with no scale in effect.

    The stroke half-width a canvas-margin corridor reserves tracks the scale, so
    the ledger's clearance for one is the only policy term a reader outside the
    measuring pass cannot re-derive.  A query is exactly such a reader.
    """
    path = Path("examples/topologies/around_section_below.mmd")
    graph = prepare_graph(
        path.read_text(),
        source_dir=str(path.parent),
        layout_options={"stroke_scale": 2.0},
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        plan = build_observed_render_plan(graph, resolve_theme(None, graph)).route_plan
    query = build_route_plan_query(plan)

    canvas = [
        item for item in plan.reservations if isinstance(item.region, CanvasRegion)
    ]
    assert canvas
    scaled = DIRECTIONAL_MARKER_HALF_EXTENT + WIDEST_THEME_LINE_WIDTH * 2.0 / 2
    assert {
        item.negative_side_clearance
        if item.region.side in CANVAS_EDGE_ON_NEGATIVE_SIDE
        else item.positive_side_clearance
        for item in canvas
    } == {scaled}
    assert all(query.realised_reservation(item.id) is not None for item in canvas)

    # The map's left channel is placed for the unscaled stroke, so a coarsened
    # render of it is genuinely short of the wider margin -- and the guard's
    # attribution carries the scaled demand rather than the constant.
    left = next(item for item in canvas if item.region.side is CanvasSide.LEFT)
    assert left.minimum_width == pytest.approx(
        scaled + left.bundle_width + EDGE_TO_BUNDLE_CLEARANCE
    )
    margin = next(
        str(item.message)
        for item in caught
        if "left canvas margin" in str(item.message)
    )
    assert f"requires {left.minimum_width:.1f}px" in margin


def test_unit_scale_returns_the_same_theme() -> None:
    theme = resolve_theme(None, _graph())
    assert _scale_theme_strokes(theme, 1.0) is theme


def test_render_emits_coarser_tracks() -> None:
    """The scale reaches the drawn SVG, not just the layout reservation."""

    plain = render_graph(_graph(), resolve_theme(None, _graph()), RenderConfig())
    coarse_graph = _graph(stroke_scale=2.0)
    coarse = render_graph(
        coarse_graph, resolve_theme(None, coarse_graph), RenderConfig()
    )
    assert plain != coarse
    assert 'stroke-width="6' in coarse
