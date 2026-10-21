"""Same-row fans and over-the-top joins that keep their lines apart.

* A fan whose branches all stay on the source row opens toward the side its
  perpendicular entry faces, farthest-reaching outermost, so each nearer branch
  peels off inside the siblings that run on, and a same-direction feeder
  delivers the bundle already in that order.  A fan with no perpendicular
  entry keeps its lines' order, so a bypass leaving under the row stays below
  the branch that runs flat.
* A line looping over a section into its flow-side entry joins any line already
  on the loop on its inside, stays inside through both corners, and lands on
  the lane farthest from the port's flat band.
* A fan's opening channels that cannot take their turn order nest one step
  apart in the order they stand in rather than fuse; channels already a step
  apart keep their places.
"""

from __future__ import annotations

import itertools
import warnings
from pathlib import Path

import pytest
from conftest import curve_abort_softened, known_curve_abort

import nf_metro.layout.routing.invariants as invariants
from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.normalize import _fanout_descent_order_key, _VChannel
from nf_metro.parser.model import Edge
from nf_metro.themes import resolve_theme

REPO = Path(__file__).resolve().parent.parent
REPROS = REPO / "tests" / "fixtures" / "curve_invariant_repros"
OVER_TOP_FAN = REPO / "examples" / "topologies" / "fan_over_top_reach_order.mmd"
FLOW_FEEDER_FAN = REPROS / "fan_over_top_flow_feeder.mmd"
BLOCKED_ASCENT_FAN = REPROS / "ascent_fan_blocked_turn_order.mmd"
REVERSED_FOLD = REPO / "examples" / "topologies" / "junction_entry_reversed_fold.mmd"
BYPASS_UNDER_FAN = REPROS / "samerow_fan_bypass_under.mmd"

Point = tuple[float, float]


def _render(path: Path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        plan = render_graph_result(
            graph, resolve_theme(None, graph, "light"), RenderConfig()
        ).plan
    return graph, list(zip(plan.routes, plan.route_polylines))


def _proper_crossing(a: Point, b: Point, c: Point, d: Point) -> Point | None:
    (x1, y1), (x2, y2), (x3, y3), (x4, y4) = a, b, c, d
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denom) < 1e-9:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
    u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / denom
    if not (0.0 < t < 1.0 and 0.0 < u < 1.0):
        return None
    return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))


def _distinct_line_crossings(path: Path) -> list[tuple[str, str, Point]]:
    graph, polylines = _render(path)
    stations = [
        (s.x, s.y) for s in graph.stations.values() if not s.is_port and not s.is_hidden
    ]
    found = []
    for (ra, pa), (rb, pb) in itertools.combinations(polylines, 2):
        if ra.line_id == rb.line_id:
            continue
        for a, b in zip(pa, pa[1:]):
            for c, d in zip(pb, pb[1:]):
                point = _proper_crossing(a, b, c, d)
                if point is not None and not any(
                    abs(point[0] - x) < 3 and abs(point[1] - y) < 3 for x, y in stations
                ):
                    found.append((ra.line_id, rb.line_id, point))
    return found


@pytest.mark.parametrize(
    "path",
    [OVER_TOP_FAN, FLOW_FEEDER_FAN, REVERSED_FOLD, BYPASS_UNDER_FAN],
    ids=lambda p: p.stem,
)
def test_fan_renders_without_crossing_lines(path: Path) -> None:
    assert _distinct_line_crossings(path) == []


@pytest.mark.parametrize("path", [OVER_TOP_FAN, FLOW_FEEDER_FAN], ids=lambda p: p.stem)
def test_port_to_junction_legs_run_level(path: Path) -> None:
    _graph, polylines = _render(path)
    legs = [
        (route.line_id, points)
        for route, points in polylines
        if "__exit_" in route.edge.source and route.edge.target.startswith("__junction")
    ]
    assert legs
    for line_id, points in legs:
        assert {y for _x, y in points} == {points[0][1]}, (line_id, points)


@pytest.mark.parametrize(
    ("path", "outer", "inner"),
    [(OVER_TOP_FAN, "l1", "l2"), (FLOW_FEEDER_FAN, "l1", "l2")],
    ids=lambda v: v.stem if isinstance(v, Path) else v,
)
def test_over_top_joiner_rides_inside_the_loop(
    path: Path, outer: str, inner: str
) -> None:
    graph, polylines = _render(path)
    loop = {
        route.line_id: points
        for route, points in polylines
        if route.edge.target in graph.ports
        and graph.ports[route.edge.target].is_entry
        and len(points) == 6
        and route.line_id in (outer, inner)
    }
    assert set(loop) == {outer, inner}, sorted(loop)
    # Over the top, right down the far side, then back into the port.
    top = {line_id: points[2][1] for line_id, points in loop.items()}
    descent = {line_id: points[3][0] for line_id, points in loop.items()}
    landing = {line_id: points[-1][1] for line_id, points in loop.items()}
    assert top[inner] > top[outer]
    assert descent[inner] < descent[outer]
    assert landing[inner] < landing[outer]


def _channel(points: list[Point], *, down: bool) -> _VChannel:
    route = RoutedPath(edge=Edge("a", "b", "l"), line_id="l", points=points)
    ys = (points[1][1], points[2][1])
    return _VChannel(
        route=route, idx=1, x=points[1][0], y_lo=min(ys), y_hi=max(ys), down=down
    )


@pytest.mark.parametrize("turn", [-1, 1], ids=["turns_left", "turns_right"])
def test_ascent_ranks_turns_in_the_mirror_of_a_descent(turn: int) -> None:
    """The earliest turn sits outermost on the side it turns to, either way up."""

    def keys(down: bool) -> tuple[tuple[int, float], tuple[int, float]]:
        sign = 1 if down else -1
        early, late = (
            _channel(
                [
                    (0.0, 0.0),
                    (10.0, 0.0),
                    (10.0, sign * depth),
                    (10 + turn * 20.0, sign * depth),
                ],
                down=down,
            )
            for depth in (40.0, 80.0)
        )
        return _fanout_descent_order_key(early), _fanout_descent_order_key(late)

    for down in (True, False):
        early, late = keys(down)
        assert (early < late) == (turn < 0), (down, early, late)


def test_blocked_turn_order_nests_the_fan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found: list[str] = []
    check = invariants.check_no_fused_cotravelling_lines

    def spy(graph, routes, offsets):  # noqa: ANN001, ANN202 - forwards the check
        violations = check(graph, routes, offsets)
        found.extend(v.message() for v in violations)
        return violations

    monkeypatch.setattr(invariants, "check_no_fused_cotravelling_lines", spy)
    _render(BLOCKED_ASCENT_FAN)
    assert found == []


PEELOFF_FAN_MMD = """\
%%metro line: l0 | Bin_Call | #e6194b
%%metro line: l1 | Markdup_Filter_Fastqc | #3cb44b
%%metro line: l2 | Dedup | #4363d8
%%metro line: l3 | Count | #f58231
%%metro grid: sec0 | 3,0
%%metro grid: sec1 | 2,0

graph LR
    subgraph sec0 [Assemble_Export 0]
        %%metro direction: RL
        s0n0[call_stats_classify]
        s0n3 -->|l0,l3| s0n4
    end
    subgraph sec1 [Index_Merge 1]
        %%metro direction: RL
        s1n0 -->|l0| s1n3
        s1n0 -->|l2| s1n1
        s1n1 -->|l2| s1n3
    end
    subgraph sec2 [Summarise 2]
        %%metro exit: bottom | l0, l1, l2, l3
        s2n0[summarise_pca]
        s2n4[assemble_markdup]
    end
    subgraph sec3 [Stats_Trim_Bin 3]
        %%metro entry: top | l0, l1, l2, l3
        s3n0[sort]
        s3n1[assemble]
    end
    s0n4 -->|l0,l3| s1n0
    s1n3 -->|l0| s2n4
    s2n4 -->|l0,l3| s3n0
    s0n3 -->|l0| s3n1
    s0n4 -->|l1| s1n3
    s1n3 -->|l1| s2n0
    s0n0 -->|l2| s1n0
    s1n3 -->|l2| s3n0
    s0n0 -->|l2| s2n0
    s1n0 -->|l3| s2n0
"""


def test_unfused_blocked_fan_keeps_its_peel_off_band(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked fan already a step apart is not re-nested across its peel-off.

    Most of the fan drops into one entry port, whose peel-off band nests by trunk
    depth; packing the fan into its standing order instead would braid that band.
    """
    found: list[str] = []
    check = invariants.check_peeloff_concentric

    def spy(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202 - forwards the check
        violations = check(*args, **kwargs)
        found.extend(v.message() for v in violations)
        return violations

    monkeypatch.setattr(invariants, "check_peeloff_concentric", spy)
    with warnings.catch_warnings(), curve_abort_softened():
        warnings.simplefilter("ignore")
        graph = prepare_graph(PEELOFF_FAN_MMD)
        render_graph_result(graph, resolve_theme(None, graph, "light"), RenderConfig())
    assert found == []


@known_curve_abort(
    "#2148",
    "#2149",
    "#2125",
    reason="sec3's TOP entry crosses l0 over l2 and l3 in its own turn and "
    "overlays l2 and l3 on its drop; the junction-fed side entries into sec1 and "
    "sec2 cross their bundles through non-concentric corners; l2's turn at "
    "__junction_6 into sec1 starves to radius 1 and doubles its corner",
)
def test_peeloff_fan_map_renders_without_a_curve_abort() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(PEELOFF_FAN_MMD)
        render_graph_result(graph, resolve_theme(None, graph, "light"), RenderConfig())
