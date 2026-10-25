"""Feeders of one target that ride one source row together share one diagonal.

Each feeder is its own edge, so each picks its own diagonal window.  When a
line on the far side of the drop dives first it crosses its neighbour's
straight, and the neighbour crosses it back on the target row.  The group must
dive together instead, and a group whose lanes never cross twice must render
exactly as its members would alone.
"""

from __future__ import annotations

import itertools
import math
import warnings
from pathlib import Path

import pytest

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.layout.constants import COORD_TOLERANCE
from nf_metro.layout.routing import core
from nf_metro.layout.routing.co_row import (
    CoRowFeeder,
    DiagonalWindow,
    shared_co_row_window,
)
from nf_metro.layout.routing.common import segments_properly_cross
from nf_metro.render.plan import RenderPlan
from nf_metro.themes import resolve_theme

TOPOLOGIES = Path(__file__).resolve().parent.parent / "examples" / "topologies"
SHARED_DIAGONAL = TOPOLOGIES / "co_row_feeders_shared_diagonal.mmd"
SHARED_DIAGONAL_BT = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "co_column_feeders_shared_diagonal_bt.mmd"
)

# An entry-port feeder and a bypass-helper feeder of one station leave one row
# in opposite lane order to the one they arrive in, so they cross exactly once
# whatever their windows.
SINGLE_FORCED_CROSSING = """\
%%metro title: Co-row feeders with one forced crossing
%%metro line: l0 | Polish_Pca | #e6194b
%%metro line: l1 | Normalize | #3cb44b
%%metro line: l2 | Annotate | #4363d8
%%metro line: l3 | Qc | #f58231
%%metro line: l4 | Gather_Call | #911eb4

graph LR
    subgraph sec0 [Dedup_Vcf_Sort 0]
        %%metro exit: right | l0, l1, l2, l3, l4
        s0n0[dedup_fastqc]
        s0n1[align_normalize_assemble]
        s0n0 -->|l0,l1,l3| s0n1
    end
    subgraph sec1 [Summarise_Index_Filter 1]
        %%metro exit: top | l0, l1, l2, l3, l4
        s1n0[assemble_cluster_count]
        s1n1[BAM]
        s1n2[dedup_index]
        s1n0 -->|l0| s1n2
        s1n0 -->|l1,l3,l4| s1n1
        s1n1 -->|l1,l3,l4| s1n2
    end
    subgraph sec2 [Summarise_Index 2]
        s2n0[markdup_index]
        s2n1[gather]
        s2n2[count_cluster]
        s2n3[vcf_markdup]
        s2n4[classify]
        s2n0 -->|l0,l3,l4| s2n1
        s2n1 -->|l0| s2n2
        s2n0 -->|l2| s2n3
        s2n3 -->|l2,l3| s2n4
        s2n1 -->|l3| s2n3
    end
    s0n1 -->|l0,l1,l3,l4| s1n0
    s1n2 -->|l0,l2,l3,l4| s2n0
    s1n2 -->|l0| s2n4
    s1n0 -->|l1| s2n0
    s0n1 -->|l2| s1n2
    s1n0 -->|l3| s2n4
"""


def _render(text: str, source_dir: Path) -> tuple[RenderPlan, str]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(text, source_dir=str(source_dir))
        result = render_graph_result(
            graph, resolve_theme(None, graph, "light"), RenderConfig()
        )
    return result.plan, result.content


def _render_without_groups(
    monkeypatch: pytest.MonkeyPatch, text: str, source_dir: Path
) -> str:
    with monkeypatch.context() as patch:
        patch.setattr(core, "plan_co_row_diagonal_groups", lambda *_a, **_k: {})
        return _render(text, source_dir)[1]


def _diagonal_span(points: list[tuple[float, float]]) -> tuple[float, float]:
    """The x-span over which a route moves from its source row to its target row."""
    source_y, target_y = points[0][1], points[-1][1]
    leave = next(
        here[0]
        for here, after in itertools.pairwise(points)
        if abs(after[1] - source_y) > COORD_TOLERANCE
    )
    arrive = next(x for x, y in points if abs(y - target_y) <= COORD_TOLERANCE)
    return leave, arrive


def _proper_crossings(
    first: list[tuple[float, float]], second: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    return [
        point
        for a, b in itertools.pairwise(first)
        for c, d in itertools.pairwise(second)
        if (point := segments_properly_cross(a, b, c, d, reject_collinear_touch=False))
        is not None
    ]


@pytest.mark.parametrize(
    "path", [SHARED_DIAGONAL, SHARED_DIAGONAL_BT], ids=lambda path: path.stem
)
def test_feeders_of_one_exit_port_never_cross_twice(path: Path) -> None:
    plan, _svg = _render(path.read_text(), path.parent)
    by_port: dict[str, list[tuple[str, str, list[tuple[float, float]]]]] = {}
    for route, points in zip(plan.routes, plan.route_polylines, strict=True):
        if route.edge.target.startswith("sec0__exit_"):
            by_port.setdefault(route.edge.target, []).append(
                (route.line_id, route.edge.source, points)
            )
    pairs = [
        (a_source, b_source, len(_proper_crossings(a_points, b_points)))
        for feeders in by_port.values()
        for (a_line, a_source, a_points), (
            b_line,
            b_source,
            b_points,
        ) in itertools.combinations(feeders, 2)
        if a_line != b_line and a_source != b_source
    ]
    assert pairs
    assert [pair for pair in pairs if pair[2] > 1] == []


def test_co_row_feeders_turn_together() -> None:
    plan, _svg = _render(SHARED_DIAGONAL.read_text(), SHARED_DIAGONAL.parent)
    feeders = [
        points
        for route, points in zip(plan.routes, plan.route_polylines, strict=True)
        if route.edge.target.startswith("sec0__exit_right")
        and abs(points[0][1] - points[-1][1]) > COORD_TOLERANCE
    ]
    assert len(feeders) == 2

    first, second = feeders
    assert _proper_crossings(first, second) == []
    first_span, second_span = _diagonal_span(first), _diagonal_span(second)
    overlap = min(first_span[1], second_span[1]) - max(first_span[0], second_span[0])
    assert overlap >= min(span[1] - span[0] for span in (first_span, second_span)) / 2


@pytest.mark.parametrize(
    ("text", "source_dir"),
    [
        (SINGLE_FORCED_CROSSING, TOPOLOGIES),
    ],
    ids=["single_forced_crossing"],
)
def test_feeders_that_never_cross_twice_render_unchanged(
    monkeypatch: pytest.MonkeyPatch, text: str, source_dir: Path
) -> None:
    assert _render(text, source_dir)[1] == _render_without_groups(
        monkeypatch, text, source_dir
    )


def test_group_with_a_member_another_handler_draws_is_declined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = SHARED_DIAGONAL.read_text()
    real = core._co_row_feeder

    def helper_drawn_elsewhere(edge, ctx, rail_internal):  # type: ignore[no-untyped-def]
        if edge.source.startswith("__bypass_"):
            return None
        return real(edge, ctx, rail_internal)

    monkeypatch.setattr(core, "_co_row_feeder", helper_drawn_elsewhere)
    declined = _render(text, SHARED_DIAGONAL.parent)[1]
    assert declined == _render_without_groups(monkeypatch, text, SHARED_DIAGONAL.parent)


def _crossing_angles(
    first: list[tuple[float, float]], second: list[tuple[float, float]]
) -> list[float]:
    def heading(a: tuple[float, float], b: tuple[float, float]) -> float:
        return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180

    angles = []
    for a, b in itertools.pairwise(first):
        for c, d in itertools.pairwise(second):
            if segments_properly_cross(a, b, c, d, reject_collinear_touch=False):
                turn = abs(heading(a, b) - heading(c, d))
                angles.append(min(turn, 180 - turn))
    return angles


def test_shared_diagonal_crosses_a_neighbouring_diagonal_steeply() -> None:
    plan, _svg = _render(SHARED_DIAGONAL_BT.read_text(), SHARED_DIAGONAL_BT.parent)
    routes = list(zip(plan.routes, plan.route_polylines, strict=True))
    feeders = [
        (route, points)
        for route, points in routes
        if route.edge.target.startswith("sec0__exit_")
    ]
    angles = [
        angle
        for feeder, feeder_points in feeders
        for route, points in routes
        if route.line_id != feeder.line_id
        for angle in _crossing_angles(feeder_points, points)
    ]
    assert angles
    assert min(angles) >= 20.0


_SOURCE_ROW = 120.0
_TARGET_ROW = 140.0


def _feeder(
    source: str,
    source_run: float,
    window: tuple[float, float],
    lanes: tuple[float, float],
    *,
    floors: tuple[float, float] = (10.0, 10.0),
) -> CoRowFeeder:
    return CoRowFeeder(
        key=(source, "target", f"line_{source}"),
        source_run=source_run,
        window=DiagonalWindow(window[0], window[1], *floors),
        source_lateral=_SOURCE_ROW + lanes[0],
        target_lateral=_TARGET_ROW + lanes[1],
    )


def _solve(*feeders: CoRowFeeder) -> tuple[float, float] | None:
    return shared_co_row_window(
        feeders,
        flow=1.0,
        target_run=366.0,
        lane_step=4.0,
    )


def test_far_side_line_diving_first_adopts_the_nearest_source_window() -> None:
    far = _feeder("far", 88.0, (321.0, 351.0), (8.0, 8.0))
    near = _feeder("near", 280.0, (300.0, 330.0), (4.0, 4.0))
    assert _solve(far, near) == (300.0, 330.0)


def test_mirrored_flow_adopts_the_nearest_source_window() -> None:
    far = _feeder("far", 912.0, (679.0, 649.0), (8.0, 8.0))
    near = _feeder("near", 720.0, (700.0, 670.0), (4.0, 4.0))
    assert shared_co_row_window(
        (far, near),
        flow=-1.0,
        target_run=634.0,
        lane_step=4.0,
    ) == (700.0, 670.0)


def test_near_side_line_diving_first_only_splays() -> None:
    far = _feeder("far", 88.0, (321.0, 351.0), (4.0, 4.0))
    near = _feeder("near", 280.0, (300.0, 330.0), (8.0, 8.0))
    assert _solve(far, near) is None


def test_lane_order_inverted_between_the_rows_is_left_alone() -> None:
    far = _feeder("far", 88.0, (321.0, 351.0), (0.0, 8.0))
    near = _feeder("near", 280.0, (300.0, 330.0), (4.0, 4.0))
    assert _solve(far, near) is None


def test_feeders_that_do_not_share_the_row_are_left_alone() -> None:
    far = _feeder("far", 88.0, (321.0, 351.0), (8.0, 8.0))
    near = _feeder("near", 310.0, (300.0, 330.0), (4.0, 4.0))
    assert _solve(far, near) is None


def test_shared_window_below_a_member_source_floor_is_declined() -> None:
    far = _feeder("far", 88.0, (321.0, 351.0), (8.0, 8.0), floors=(220.0, 10.0))
    near = _feeder("near", 280.0, (300.0, 330.0), (4.0, 4.0))
    assert _solve(far, near) is None


def test_shared_window_inside_a_member_target_floor_is_declined() -> None:
    far = _feeder("far", 88.0, (321.0, 351.0), (8.0, 8.0), floors=(10.0, 30.0))
    near = _feeder("near", 280.0, (300.0, 340.0), (4.0, 4.0))
    assert _solve(far, near) is None


def test_shared_window_running_two_lines_together_is_declined() -> None:
    far = _feeder("far", 88.0, (321.0, 351.0), (4.0, 8.0))
    near = _feeder("near", 280.0, (300.0, 330.0), (4.0, 4.0))
    assert _solve(far, near) is None


def _neighbour(window: tuple[float, float], lanes: tuple[float, float]) -> CoRowFeeder:
    return CoRowFeeder(
        key=("near", "other", "line_near"),
        source_run=280.0,
        window=DiagonalWindow(window[0], window[1], 10.0, 10.0),
        source_lateral=_SOURCE_ROW + lanes[0],
        target_lateral=_SOURCE_ROW + lanes[1],
    )


def _solve_beside(
    neighbour: CoRowFeeder, *, target_run: float
) -> tuple[float, float] | None:
    far = _feeder("far", 88.0, (321.0, 351.0), (8.0, 8.0))
    near = _feeder("near", 280.0, (300.0, 330.0), (4.0, 4.0))
    return shared_co_row_window(
        (far, near),
        flow=1.0,
        target_run=target_run,
        lane_step=4.0,
        neighbours=lambda: (neighbour,),
    )


def test_shared_window_slides_past_a_neighbouring_diagonal() -> None:
    neighbour = _neighbour((300.0, 330.0), (0.0, 30.0))
    assert _solve_beside(neighbour, target_run=400.0) == (330.0, 360.0)


def test_shared_window_that_cannot_clear_a_neighbour_is_declined() -> None:
    neighbour = _neighbour((300.0, 330.0), (0.0, 30.0))
    assert _solve_beside(neighbour, target_run=366.0) is None


def test_neighbour_diagonal_outside_the_shared_window_is_left_alone() -> None:
    neighbour = _neighbour((250.0, 280.0), (-30.0, 0.0))
    assert _solve_beside(neighbour, target_run=366.0) == (300.0, 330.0)


def test_neighbour_diagonal_stepping_the_other_way_is_left_alone() -> None:
    neighbour = _neighbour((300.0, 330.0), (30.0, 0.0))
    assert _solve_beside(neighbour, target_run=366.0) == (300.0, 330.0)
