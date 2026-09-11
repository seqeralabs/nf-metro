"""Regression tests for issue #1568: a flanked cross-row top-entry riser.

A LEFT/RIGHT exit dropping into a TOP entry stacked in its own grid column runs
a vertical riser in the inter-column gap on its exit side.  When a section
occupies that neighbouring column at the source's own row the gap is walled on
both sides, so a lead-in only a curve radius off the source box climbs the
outside of that box, which the bundle-curve machinery rejects.  The riser must
instead sit a clearance off its exit wall, and the bundles it counter-runs in
that gap must settle clear of it.

Covers:

* The issue reproducer seats the riser at least ``_MIN_RISER_EDGE_CLEARANCE``
  off both flanking walls, and does not abort.
* ``stacked_top_entry_flanked_riser_crossline`` runs a distinct line up the same
  flanked gap, overlapping the riser in Y, and the two counter-running
  centrelines keep ``BUNDLE_TO_BUNDLE_CLEARANCE`` between them.
* A left-exit riser, flanked by a section in the column to its left, seats the
  same clearance off its own exit wall (the mirror of the right-exit shape).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import (
    BUNDLE_TO_BUNDLE_CLEARANCE,
    COORD_TOLERANCE,
    MIN_CORRIDOR_Y_OVERLAP,
)
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import (
    RoutedPath,
    apply_route_offsets,
    column_gap_edges,
    gap_lo_for_x,
)
from nf_metro.layout.routing.invariants import (
    _MIN_RISER_EDGE_CLEARANCE,
    check_gap_channels_materialized,
    check_no_riser_hugs_section_edge,
    check_opposing_gap_channel_clearance,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, PortSide

TOPOLOGIES = Path(__file__).resolve().parents[1] / "examples" / "topologies"
FIXTURES = [
    "stacked_top_entry_flanked_riser",
    "stacked_top_entry_flanked_riser_crossline",
]

LEFT_EXIT_RISER = """\
%%metro title: Flanked left-exit riser
%%metro line: side | Side | #2db572
%%metro grid: a | 1,0
%%metro grid: b | 1,1
%%metro grid: c | 1,2
%%metro grid: d | 0,1

graph LR
    subgraph a [Alpha]
        %%metro exit: left | side
        a1[A one]
        a2[A two]
        a1 -->|side| a2
    end

    subgraph b [Beta]
        %%metro entry: top | side
        %%metro exit: left | side
        b1[B one]
        b2[B two]
        b1 -->|side| b2
    end

    subgraph c [Gamma]
        %%metro entry: top | side
        c1[C one]
        c2[C two]
        c1 -->|side| c2
    end

    subgraph d [Delta]
        %%metro entry: right | side
        d1[D one]
        d2[D two]
        d1 -->|side| d2
    end

    a2 -->|side| b1
    b2 -->|side| c1
    c2 -->|side| d1
"""


def _layout_routes(
    text: str,
) -> tuple[MetroGraph, list[RoutedPath], dict[tuple[str, str], float]]:
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    return graph, route_edges(graph, station_offsets=offsets), offsets


def _route(
    stem: str,
) -> tuple[MetroGraph, list[RoutedPath], dict[tuple[str, str], float]]:
    return _layout_routes((TOPOLOGIES / f"{stem}.mmd").read_text())


def _flanked_gap_riser(
    graph: MetroGraph,
    routes: list[RoutedPath],
    offsets: dict[tuple[str, str], float],
) -> tuple[float, int, int | None]:
    """The TOP-entry riser leg in an inter-column gap: ``(x, gap_lo, row)``."""
    for route in routes:
        if not route.is_inter_section:
            continue
        target_port = graph.ports.get(route.edge.target)
        if target_port is None or target_port.side is not PortSide.TOP:
            continue
        points = apply_route_offsets(route, offsets)
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            if abs(x1 - x0) > COORD_TOLERANCE or abs(y1 - y0) <= COORD_TOLERANCE:
                continue
            match = gap_lo_for_x(graph, x0, min(y0, y1), max(y0, y1))
            if match is not None:
                return x0, match[0], match[1]
    raise AssertionError("no flanked top-entry riser found")


@pytest.mark.parametrize("stem", FIXTURES)
def test_flanked_riser_renders_without_curve_defect(stem: str) -> None:
    """The flanked riser clears both gap walls and trips no gap-channel guard."""
    graph, routes, offsets = _route(stem)

    assert not check_no_riser_hugs_section_edge(graph, routes, offsets)
    assert not check_opposing_gap_channel_clearance(graph, routes, offsets)
    assert not check_gap_channels_materialized(graph, routes)

    x, gap_lo, row = _flanked_gap_riser(graph, routes, offsets)
    left, right = column_gap_edges(graph, gap_lo, gap_lo + 1, row=row)
    assert x - left >= _MIN_RISER_EDGE_CLEARANCE - COORD_TOLERANCE, (stem, x, left)
    assert right - x >= _MIN_RISER_EDGE_CLEARANCE - COORD_TOLERANCE, (stem, x, right)


def test_crossline_counter_running_lines_keep_bundle_clearance() -> None:
    """A distinct line climbing the flanked gap stays a bundle clearance off the
    riser it counter-runs, so joint allocation -- not a coincidental Y gap --
    keeps them apart."""
    graph, routes, offsets = _route("stacked_top_entry_flanked_riser_crossline")

    gap_channels: list[tuple[str, float, float, float, bool]] = []
    for route in routes:
        if not route.is_inter_section:
            continue
        points = apply_route_offsets(route, offsets)
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            if abs(x1 - x0) > COORD_TOLERANCE or abs(y1 - y0) <= COORD_TOLERANCE:
                continue
            if gap_lo_for_x(graph, x0, min(y0, y1), max(y0, y1)) == (0, 1):
                gap_channels.append(
                    (route.line_id, x0, min(y0, y1), max(y0, y1), y1 > y0)
                )

    separations = [
        abs(xa - xb)
        for line_a, xa, alo, ahi, adown in gap_channels
        for line_b, xb, blo, bhi, bdown in gap_channels
        if line_a != line_b
        and adown is not bdown
        and min(ahi, bhi) - max(alo, blo) > MIN_CORRIDOR_Y_OVERLAP
    ]
    assert separations, "expected counter-running distinct lines overlapping in Y"
    assert min(separations) >= BUNDLE_TO_BUNDLE_CLEARANCE - COORD_TOLERANCE


def test_left_exit_flanked_riser_clears_its_exit_wall() -> None:
    """A left-exit riser flanked on its left seats a clearance off its exit wall."""
    graph, routes, offsets = _layout_routes(LEFT_EXIT_RISER)

    assert not check_no_riser_hugs_section_edge(graph, routes, offsets)

    x, gap_lo, row = _flanked_gap_riser(graph, routes, offsets)
    left, right = column_gap_edges(graph, gap_lo, gap_lo + 1, row=row)
    assert x - left >= _MIN_RISER_EDGE_CLEARANCE - COORD_TOLERANCE, (x, left)
    assert right - x >= _MIN_RISER_EDGE_CLEARANCE - COORD_TOLERANCE, (x, right)
