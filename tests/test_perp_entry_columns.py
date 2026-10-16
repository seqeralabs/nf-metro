"""Feeders landing on a perpendicular entry's column.

A TOP/BOTTOM entry on a horizontal-flow section is crossed on one column by
every feeder and by the drop that turns from it into the trunk.  Each line must
cross on the channel its in-section drop departs from, and two lines must never
share a descent into the port.

``perp_entry_column_two_feeders`` and the gallery map
``bottom_entry_two_feeders_own_lanes`` have two feeders reaching one BOTTOM
entry, so the column is dealt from the trunk the drop turns into.
``perp_exit_single_feeder_landing`` has one up-and-over feeder, which lands on
the crossing its own lines set.  ``perp_entry_column_feeder_conflict`` has a
junction delivering two lines in the opposite side order to the column its
trunk would deal; honouring that column would cross them inside the
junction's corners, so the port keeps the column the other passes give it.
The ``perp_entry_column_merge_feeder``, ``..._opposite_side_fan`` and
``..._straight_drop`` fixtures each have a feeder drawn from the port's own
crossing beyond its final leg (a merge, a fan wrapping round from the far
side, a perpendicular exit dropping straight in), so they keep that crossing
too.
"""

from __future__ import annotations

import itertools
import warnings
from pathlib import Path

import pytest

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.layout.routing import compute_station_offsets
from nf_metro.layout.routing.perp_columns import deal_perp_entry_column
from nf_metro.themes import resolve_theme

ROOT = Path(__file__).resolve().parent.parent
REGRESSIONS = ROOT / "tests" / "fixtures" / "regressions"
TOPOLOGIES = ROOT / "examples" / "topologies"

LANDING_CASES = [
    (REGRESSIONS / "perp_entry_column_two_feeders.mmd", "sec4__entry_bottom_7"),
    (REGRESSIONS / "perp_exit_single_feeder_landing.mmd", "sec4__entry_bottom_4"),
    (TOPOLOGIES / "bottom_entry_two_feeders_own_lanes.mmd", "classify__entry_bottom_5"),
]

CONFLICT = REGRESSIONS / "perp_entry_column_feeder_conflict.mmd"
CONFLICT_PORT = "sec3__entry_top_5"

Point = tuple[float, float]


def _layout(path: Path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        result = render_graph_result(
            graph, resolve_theme(None, graph, mode=None), RenderConfig()
        )
    return graph, list(zip(result.plan.routes, result.plan.route_polylines))


def _port_routes(path: Path, port_id: str):
    graph, routes = _layout(path)
    incoming = [
        (route.edge.source, route.line_id, [tuple(p) for p in points])
        for route, points in routes
        if route.edge.target == port_id
    ]
    outgoing = [
        (route.line_id, [tuple(p) for p in points])
        for route, points in routes
        if route.edge.source == port_id
    ]
    return graph, incoming, outgoing


def _final_descent(points: list[Point]) -> tuple[float, float, float]:
    """``(x, lo_y, hi_y)`` of the vertical run ending at the port."""
    x, end_y = points[-1]
    start_y = end_y
    for px, py in reversed(points[:-1]):
        if abs(px - x) > 0.5:
            break
        start_y = py
    return x, min(start_y, end_y), max(start_y, end_y)


@pytest.mark.parametrize(("path", "port_id"), LANDING_CASES, ids=lambda v: str(v))
def test_each_line_crosses_on_its_departure_channel(path: Path, port_id: str) -> None:
    _graph, incoming, outgoing = _port_routes(path, port_id)
    departures: dict[str, set[float]] = {}
    for line_id, points in outgoing:
        departures.setdefault(line_id, set()).add(round(points[0][0], 1))
    assert incoming
    steps = [
        (source, line_id, points[-1][0], sorted(departures[line_id]))
        for source, line_id, points in incoming
        if all(abs(points[-1][0] - x) > 0.5 for x in departures[line_id])
    ]
    assert not steps, steps


@pytest.mark.parametrize(("path", "port_id"), LANDING_CASES, ids=lambda v: str(v))
def test_distinct_lines_never_share_a_descent(path: Path, port_id: str) -> None:
    _graph, incoming, _outgoing = _port_routes(path, port_id)
    shared = []
    for (src_a, line_a, pts_a), (src_b, line_b, pts_b) in itertools.combinations(
        incoming, 2
    ):
        if line_a == line_b:
            continue
        xa, lo_a, hi_a = _final_descent(pts_a)
        xb, lo_b, hi_b = _final_descent(pts_b)
        if abs(xa - xb) < 0.5 and min(hi_a, hi_b) - max(lo_a, lo_b) > 1.0:
            shared.append(((src_a, line_a), (src_b, line_b), xa))
    assert not shared, shared


def test_two_feeder_port_deals_its_column() -> None:
    graph, _routes = _layout(LANDING_CASES[0][0])
    column = deal_perp_entry_column(
        graph, LANDING_CASES[0][1], compute_station_offsets(graph)
    )
    assert column is not None and column.offsets is not None


def test_feeder_order_conflict_declines_the_column() -> None:
    graph, _routes = _layout(CONFLICT)
    column = deal_perp_entry_column(
        graph, CONFLICT_PORT, compute_station_offsets(graph)
    )
    assert column is not None and column.offsets is None


KEEPS_OWN_CROSSING = [
    (REGRESSIONS / "perp_entry_column_merge_feeder.mmd", "sec3__entry_bottom_6"),
    (REGRESSIONS / "perp_entry_column_opposite_side_fan.mmd", "sec2__entry_bottom_3"),
    (REGRESSIONS / "perp_entry_column_straight_drop.mmd", "sec4__entry_top_6"),
]


@pytest.mark.parametrize(
    ("path", "port_id"), KEEPS_OWN_CROSSING, ids=lambda v: getattr(v, "stem", v)
)
def test_feeder_drawn_from_the_port_crossing_declines_the_column(
    path: Path, port_id: str
) -> None:
    graph, _routes = _layout(path)
    column = deal_perp_entry_column(graph, port_id, compute_station_offsets(graph))
    assert column is not None and column.offsets is None


def _proper_crossings(a: list[Point], b: list[Point]) -> int:
    def orient(p: Point, q: Point, r: Point) -> int:
        v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return 0 if abs(v) < 1e-6 else (1 if v > 0 else -1)

    count = 0
    for p1, p2 in itertools.pairwise(a):
        for q1, q2 in itertools.pairwise(b):
            if (
                orient(p1, p2, q1) * orient(p1, p2, q2) < 0
                and orient(q1, q2, p1) * orient(q1, q2, p2) < 0
            ):
                count += 1
    return count


def test_conflicting_junction_feed_keeps_its_lines_uncrossed() -> None:
    _graph, incoming, _outgoing = _port_routes(CONFLICT, CONFLICT_PORT)
    junction = {
        line_id: points
        for source, line_id, points in incoming
        if source == "__junction_7"
    }
    assert set(junction) == {"l0", "l3"}
    assert _proper_crossings(junction["l0"], junction["l3"]) == 0
