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
from conftest import known_curve_abort

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.layout.constants import COORD_TOLERANCE
from nf_metro.layout.routing import compute_station_offsets, exit_turns
from nf_metro.layout.routing.families import RouteFamilyId
from nf_metro.layout.routing.perp_columns import deal_perp_entry_column
from nf_metro.themes import resolve_theme

ROOT = Path(__file__).resolve().parent.parent
REGRESSIONS = ROOT / "tests" / "fixtures" / "regressions"
TOPOLOGIES = ROOT / "examples" / "topologies"


SINGLE_FEEDER_LANDING_ABORT = known_curve_abort(
    "#2124", "#2092", reason="Assemble 3's RL BOTTOM exit crosses its bundle"
)
FEEDER_CONFLICT_ABORT = known_curve_abort(
    "#2131",
    "#2130",
    reason="feeders share an approach channel into sec3; bypass overlays in "
    "sec0 and sec2; deferred offset on __bypass_s2n4_s2n3_6 is untracked",
)

LANDING_CASES = [
    (REGRESSIONS / "perp_entry_column_two_feeders.mmd", "sec4__entry_bottom_3"),
    pytest.param(
        REGRESSIONS / "perp_exit_single_feeder_landing.mmd",
        "sec4__entry_bottom_4",
        marks=SINGLE_FEEDER_LANDING_ABORT,
    ),
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


@FEEDER_CONFLICT_ABORT
def test_feeder_order_conflict_declines_the_column() -> None:
    graph, _routes = _layout(CONFLICT)
    column = deal_perp_entry_column(
        graph, CONFLICT_PORT, compute_station_offsets(graph)
    )
    assert column is not None and column.offsets is None


KEEPS_OWN_CROSSING = [
    (REGRESSIONS / "perp_entry_column_merge_feeder.mmd", "sec3__entry_bottom_6"),
    pytest.param(
        REGRESSIONS / "perp_entry_column_opposite_side_fan.mmd",
        "sec2__entry_bottom_3",
        marks=known_curve_abort(
            "#2124",
            "#2125",
            "#2127",
            reason="RL TOP exit and fan traverse corners cross; s1n2->s1n3 "
            "collinear pair is untracked",
        ),
    ),
    pytest.param(
        REGRESSIONS / "perp_entry_column_straight_drop.mmd",
        "sec4__entry_top_6",
        marks=known_curve_abort(
            "#2121",
            "#2119",
            reason="BT TOP exit turns back through its own section into the "
            "TOP entry below",
        ),
    ),
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


@FEEDER_CONFLICT_ABORT
def test_conflicting_junction_feed_keeps_its_lines_uncrossed() -> None:
    _graph, incoming, _outgoing = _port_routes(CONFLICT, CONFLICT_PORT)
    junction = {
        line_id: points
        for source, line_id, points in incoming
        if source == "__junction_7"
    }
    assert set(junction) == {"l0", "l3"}
    assert _proper_crossings(junction["l0"], junction["l3"]) == 0


JUNCTION_STRAIGHT_DROP = """\
%%metro line: l1 | One | #3cb44b
%%metro line: l2 | Two | #4363d8
%%metro line: l3 | Three | #f58231
%%metro line: l4 | Four | #911eb4
%%metro grid: sec3 | 0,3
%%metro grid: sec4 | 0,4

graph LR
    subgraph sec2 [Top]
        s2n2[polish]
        s2n3[quant_merge]
        s2n2 -->|l4| s2n3
    end
    subgraph sec3 [Middle]
        s3n0[bin_index]
        s3n2[polish]
    end
    subgraph sec4 [Bottom]
        %%metro entry: bottom | l3, l4
        s4n0[pca_annotate]
    end
    s2n3 -->|l1,l2| s3n0
    s3n2 -->|l3| s4n0
    s2n3 -->|l4| s4n0
"""


def test_junction_straight_drop_turns_onto_the_dealt_column(tmp_path: Path) -> None:
    """A junction standing over a dealt BOTTOM port turns its drop on that column.

    The port's two feeders deal it a column, which puts ``l4``'s channel a lane
    off the port's own X.  The exit-turn plan for the junction's straight drop
    must pin the turn on that channel, where the drop is drawn.
    """
    path = tmp_path / "junction_straight_drop.mmd"
    path.write_text(JUNCTION_STRAIGHT_DROP)
    graph, routes = _layout(path)
    port = graph.stations["sec4__entry_bottom_3"]
    drop = next(
        [tuple(p) for p in points]
        for route, points in routes
        if route.edge.source == "__junction_4"
        and route.edge.target == port.id
        and route.line_id == "l4"
    )
    departure = next(
        points[0][0]
        for route, points in routes
        if route.edge.source == port.id and route.line_id == "l4"
    )
    assert abs(_final_descent(drop)[0] - departure) <= 0.5


UNDEALT_JUNCTION_DROPS = {
    "single_feeder": JUNCTION_STRAIGHT_DROP.replace(
        "    s3n2 -->|l3| s4n0\n", ""
    ).replace("entry: bottom | l3, l4", "entry: bottom | l4"),
    "declined_column": JUNCTION_STRAIGHT_DROP.replace(
        "    s2n3 -->|l4| s4n0\n", "    s2n3 -->|l4| s4n0\n    s2n3 -->|l2| s4n0\n"
    ).replace("entry: bottom | l3, l4", "entry: bottom | l2, l3, l4"),
}


def _junction_drop_turn_offsets(source: str, tmp_path: Path, monkeypatch) -> set[float]:
    """Turn-axis minus port X for each planned drop from a junction over its port."""
    deltas: set[float] = set()
    real = exit_turns._source_turn_requirement

    def spy(edge, family_id, source_run_direction, ctx, exit_port_id=None):
        requirement = real(edge, family_id, source_run_direction, ctx, exit_port_id)
        src, tgt = ctx.graph.stations[edge.source], ctx.graph.stations[edge.target]
        if (
            family_id is RouteFamilyId.BOTTOM_ENTRY_L_SHAPE
            and edge.source in ctx.graph.junctions
            and abs(src.x - tgt.x) <= COORD_TOLERANCE
            and requirement.fixed_axis is not None
        ):
            deltas.add(round(requirement.fixed_axis - tgt.x, 3))
        return requirement

    monkeypatch.setattr(exit_turns, "_source_turn_requirement", spy)
    path = tmp_path / "junction_drop.mmd"
    path.write_text(source)
    _layout(path)
    return deltas


def test_junction_straight_drop_turns_off_the_dealt_column_offset(
    tmp_path: Path, monkeypatch
) -> None:
    deltas = _junction_drop_turn_offsets(JUNCTION_STRAIGHT_DROP, tmp_path, monkeypatch)
    assert deltas and 0.0 not in deltas


@pytest.mark.parametrize(
    "name",
    [
        "single_feeder",
        pytest.param(
            "declined_column",
            marks=known_curve_abort(
                "#2119", reason="sec4's drop crosses l3 over l2 and l4 in its turn"
            ),
        ),
    ],
)
def test_junction_straight_drop_without_a_dealt_column_turns_on_the_port(
    name: str, tmp_path: Path, monkeypatch
) -> None:
    deltas = _junction_drop_turn_offsets(
        UNDEALT_JUNCTION_DROPS[name], tmp_path, monkeypatch
    )
    assert deltas == {0.0}
