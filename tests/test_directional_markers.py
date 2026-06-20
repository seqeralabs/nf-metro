"""Tests for static directional chevrons (``--directional``)."""

import pathlib

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.svg import (
    _chevron_samples,
    apply_route_offsets,
    render_svg,
)
from nf_metro.themes import NFCORE_THEME

EXAMPLES_DIR = pathlib.Path(__file__).parent.parent / "examples"

DIRECTION_FIXTURES = [
    "simple_pipeline",
    "rnaseq_auto",
    "genomic_pipeline",
    "longread_variant_calling",
]


def _laid_out(stem: str):
    graph = parse_metro_mermaid((EXAMPLES_DIR / f"{stem}.mmd").read_text())
    compute_layout(graph)
    return graph


def _render(stem: str, *, directional: bool) -> str:
    graph = _laid_out(stem)
    graph.directional = directional
    return render_svg(graph, NFCORE_THEME)


def test_directional_off_by_default_emits_no_chevrons():
    svg = _render("simple_pipeline", directional=False)
    assert "metro-direction-" not in svg


@pytest.mark.parametrize("stem", DIRECTION_FIXTURES)
def test_directional_flag_emits_chevrons(stem):
    svg = _render(stem, directional=True)
    assert "metro-direction-" in svg


@pytest.mark.parametrize("stem", DIRECTION_FIXTURES)
def test_chevron_headings_point_downstream(stem):
    """Every chevron must point from the route's source toward its target.

    Direction is read from the routed point order, so a chevron heading on any
    segment should agree with that segment's own source-to-target travel.
    """
    graph = _laid_out(stem)
    station_offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=station_offsets)

    spacing = NFCORE_THEME.directional_marker_spacing
    min_length = 2 * NFCORE_THEME.directional_marker_size

    checked = 0
    for route in routes:
        pts = apply_route_offsets(route, station_offsets)
        for point, heading in _chevron_samples(pts, spacing, min_length):
            assert _heading_is_forward_on_polyline(pts, point, heading)
            checked += 1

    assert checked > 0, f"{stem} produced no chevrons to check"


def _heading_is_forward_on_polyline(pts, point, heading, tol=0.5):
    """A chevron heading must run forward along a segment it lies on.

    The point can coincide with a corner vertex shared by two segments; the
    heading is forward-valid if it points in the source-to-target direction of
    either adjoining segment. A reversed heading matches neither.
    """
    px, py = point
    ux, uy = heading
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        seg_len_sq = (bx - ax) ** 2 + (by - ay) ** 2
        if seg_len_sq == 0:
            continue
        t = ((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / seg_len_sq
        if not -0.01 <= t <= 1.01:
            continue
        cx, cy = ax + t * (bx - ax), ay + t * (by - ay)
        if abs(cx - px) >= tol or abs(cy - py) >= tol:
            continue
        seg_len = seg_len_sq**0.5
        forward = ((bx - ax) * ux + (by - ay) * uy) / seg_len
        if forward > 0.999:
            return True
    return False
