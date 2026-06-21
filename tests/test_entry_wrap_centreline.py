"""The entry-wrap family is built via a centreline + builder.

``_route_left_entry_wrap``, ``_route_right_entry_wrap`` (its cross-row branch),
and ``_build_right_entry_wrap_route`` (the shared body of the gap-above and
around-below RIGHT-entry wraps) each describe their R-D-L-D-R / R-D-R-D-L loop
as a centreline and fan it with the bundle builder, rather than assembling
per-line ``points`` / ``curve_radii`` by hand.  Each declares the co-travelling
bundle via ``bundle_offsets``, so the builder anchors every corner on the
bundle's innermost-of-turn line and no inside-of-turn arc falls below the floor.

These tests pin that on the fixtures whose inter-section routing exercises the
wraps: a LEFT-entry wrap, a cross-row RIGHT-entry wrap, and the gap-above /
around-below RIGHT-entry wraps.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import CURVE_RADIUS
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import (
    OffsetRegime,
    compute_station_offsets,
    route_edges,
)
from nf_metro.layout.routing.invariants import (
    assert_render_curve_invariants,
    check_bundle_order_preserved,
    check_concentric_bundle_corners,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

# Fixtures whose inter-section routing exercises an entry-wrap handler.
WRAP_FIXTURES = [
    EXAMPLES / "topologies" / "around_section_below.mmd",
    EXAMPLES / "topologies" / "junction_entry_align.mmd",
    EXAMPLES / "topologies" / "stacked_lr_serpentine.mmd",
    EXAMPLES / "topologies" / "right_entry_wrap_no_fan.mmd",
    EXAMPLES / "topologies" / "convergence_stacked_sink.mmd",
    EXAMPLES / "topologies" / "right_entry_gap_above_empty_row.mmd",
    EXAMPLES / "longread_variant_calling.mmd",
]


def _route(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, offsets, routes


def _wrap_routes(graph, routes):
    """Inter-section routes that wrap into a side entry port via a 6-point loop.

    The entry-wrap handlers all emit a five-segment R-D-L-D-R / R-D-R-D-L loop
    (six waypoints, four corner radii) into a LEFT or RIGHT entry port from the
    port's own outward side.
    """
    wraps = []
    for r in routes:
        port = graph.ports.get(r.edge.target)
        if (
            port is not None
            and port.is_entry
            and port.side in (PortSide.LEFT, PortSide.RIGHT)
            and r.is_inter_section
            and len(r.points) == 6
            and r.curve_radii
            and len(r.curve_radii) == 4
        ):
            wraps.append(r)
    return wraps


@pytest.mark.parametrize("path", WRAP_FIXTURES, ids=lambda p: p.stem)
def test_entry_wrap_corners_are_concentric_and_unflipped(path: Path) -> None:
    graph, offsets, routes = _route(path)
    assert check_concentric_bundle_corners(graph, routes, offsets) == []
    assert check_bundle_order_preserved(routes) == []
    assert_render_curve_invariants(graph, routes, offsets)


@pytest.mark.parametrize("path", WRAP_FIXTURES, ids=lambda p: p.stem)
def test_entry_wrap_corner_radii_anchored_at_floor(path: Path) -> None:
    """Every wrap corner sits at or above the floor radius.

    The builder anchors the bundle's innermost-of-turn line at ``CURVE_RADIUS``
    from the declared fan, so no inside-of-turn arc falls below it.  A
    hand-rolled radius that pinched an inside corner below the floor (the defect
    this family was migrated to remove) would trip this.
    """
    graph, _offsets, routes = _route(path)
    wraps = _wrap_routes(graph, routes)
    assert wraps, f"{path.stem}: expected at least one entry-wrap route"
    offenders = [
        (r.edge.source, r.edge.target, r.line_id, r.curve_radii)
        for r in wraps
        if any(radius < CURVE_RADIUS - 0.01 for radius in r.curve_radii)
    ]
    assert not offenders, f"{path.stem}: wrap corners below the floor: {offenders}"


@pytest.mark.parametrize("path", WRAP_FIXTURES, ids=lambda p: p.stem)
def test_entry_wrap_routes_are_offset_baked(path: Path) -> None:
    graph, _offsets, routes = _route(path)
    wraps = _wrap_routes(graph, routes)
    assert wraps, f"{path.stem}: expected at least one entry-wrap route"
    assert all(r.offset_regime is OffsetRegime.BAKED for r in wraps)
