"""A same-row bypass joining a flat bundle takes a lane beside it, not inside it.

At a shared LR/RL entry port, a line from a section on the port's own row can
arrive perpendicular: intervening sections force it round below them, so it
runs back up into the port on a riser.  Its feeder shares the port's row, yet
it is not a flat co-traveller.  Slotted among the lines that do run straight in,
it takes a lane one of them holds upstream; that line has to step aside just
outside the port, and the riser crosses it on the way up.

Each case names a port where a same-row bypass meets at least one flat feeder,
in LR and RL, with one and with several flat lines, and with the flat lines'
upstream section sharing its bundle with the port's section.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from nf_metro.api import prepare_graph
from nf_metro.render.svg import build_render_plan
from nf_metro.themes import resolve_theme

ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = ROOT / "examples" / "topologies"

CASES = [
    ("samerow_bypass_joins_flat_bundle", "report__entry_left_3"),
    ("samerow_bypass_joins_flat_bundle_rl", "report__entry_right_3"),
    ("junction_entry_lane_step", "orf_calling__entry_left_3"),
    ("disjoint_sameline_trunks", "secC__entry_left_5"),
    ("disjoint_sameline_trunks", "secE__entry_left_7"),
    ("folded_corridor_distinct_lanes", "realignment__entry_right_9"),
]
CASE_IDS = [f"{fixture}:{port}" for fixture, port in CASES]

# The fold hands ``realignment`` its bundle with ``rna`` above ``dna``, and its
# trunk must stand level with ``normalization``'s, which carries the same bundle
# on the same row.  With ``rna`` rising from below, ``dna`` cannot keep its lane
# into the port: it steps once, on its connector into the port.
FORCED_STEPS = {("folded_corridor_distinct_lanes", "realignment__entry_right_9"): 1}

_TOL = 0.5
Point = tuple[float, float]


def _drawn_routes(fixture: str):  # noqa: ANN202
    path = TOPOLOGIES / f"{fixture}.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    plan = build_render_plan(graph, resolve_theme("nfcore", graph, "light"))
    return graph, list(zip(plan.routes, plan.route_polylines))


def _proper_crossing(a: tuple[Point, Point], b: tuple[Point, Point]) -> Point | None:
    (x1, y1), (x2, y2) = a
    (x3, y3), (x4, y4) = b
    denom = (x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3)
    if abs(denom) < 1e-9:
        return None
    t = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / denom
    u = ((x3 - x1) * (y2 - y1) - (y3 - y1) * (x2 - x1)) / denom
    eps = 1e-6
    if eps < t < 1 - eps and eps < u < 1 - eps:
        return (round(x1 + t * (x2 - x1), 1), round(y1 + t * (y2 - y1), 1))
    return None


def _segments(points: list[Point]) -> list[tuple[Point, Point]]:
    return list(zip(points, points[1:]))


def _lane_steps(points: list[Point]) -> list[tuple[Point, Point]]:
    """Segments that shift sideways by less than a turn: a lane step, not a riser."""
    return [
        (a, b)
        for a, b in _segments(points)
        if abs(b[0] - a[0]) > _TOL and _TOL < abs(b[1] - a[1]) < 10.0
    ]


@pytest.mark.parametrize(("fixture", "port_id"), CASES, ids=CASE_IDS)
def test_routes_into_the_port_do_not_cross(fixture: str, port_id: str) -> None:
    _graph, routes = _drawn_routes(fixture)
    arriving = [
        (route.line_id, points)
        for route, points in routes
        if route.edge.target == port_id
    ]
    crossings = [
        (la, lb, hit)
        for (la, pa), (lb, pb) in itertools.combinations(arriving, 2)
        if la != lb
        for sa in _segments(pa)
        for sb in _segments(pb)
        if (hit := _proper_crossing(sa, sb)) is not None
    ]
    assert not crossings, crossings


@pytest.mark.parametrize(("fixture", "port_id"), CASES, ids=CASE_IDS)
def test_flat_feeders_run_level_into_the_port(fixture: str, port_id: str) -> None:
    """Every row-level connector a flat feeder rides up to the port stays level.

    Covers the whole flat approach back along the row, so a lane step cannot
    simply move to a boundary further upstream.  Where a step is forced (see
    ``FORCED_STEPS``) there is exactly that many, each a 45-degree diagonal on
    the connector into the port.
    """
    graph, routes = _drawn_routes(fixture)
    row_y = graph.stations[port_id].y
    by_target: dict[str, list] = {}
    for route, points in routes:
        by_target.setdefault(route.edge.target, []).append((route, points))

    def on_row(station_id: str) -> bool:
        return abs(graph.stations[station_id].y - row_y) <= _TOL

    def level(points: list[Point]) -> bool:
        return not any(
            abs(b[0] - a[0]) <= _TOL and abs(b[1] - a[1]) > _TOL
            for a, b in _segments(points)
        )

    flat_lines = {
        route.line_id
        for route, points in by_target.get(port_id, [])
        if on_row(route.edge.source) and level(points)
    }
    assert flat_lines, f"{port_id} has no flat feeder"

    steps = []
    for line_id in sorted(flat_lines):
        frontier, seen = [port_id], {port_id}
        while frontier:
            target = frontier.pop()
            for route, points in by_target.get(target, []):
                if route.line_id != line_id or not on_row(route.edge.source):
                    continue
                if not level(points):
                    continue
                steps.extend(
                    (line_id, route.edge.source, route.edge.target, seg)
                    for seg in _lane_steps(points)
                )
                if route.edge.source not in seen:
                    seen.add(route.edge.source)
                    frontier.append(route.edge.source)
    forced = FORCED_STEPS.get((fixture, port_id), 0)
    assert len(steps) == forced, steps
    for _line_id, _source, target, (a, b) in steps:
        assert target == port_id, steps
        assert abs(abs(b[0] - a[0]) - abs(b[1] - a[1])) <= _TOL, steps
