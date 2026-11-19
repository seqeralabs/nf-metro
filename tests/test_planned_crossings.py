"""A crossing the lane plan decides is recorded, and drawn where it records it.

* Two bundles into one entry carrying two lines in opposite orders: the
  bundle keeps its own order on every leg and takes the entry's lanes in its
  last bend.
* A line whose entry lane lies between the lanes of another feeding bundle
  crosses that bundle; the cohort compile accepts that crossing on the
  connectors that carry it, reached through the exit's fork where it forks.
* A perpendicular exit whose lines all cross into one flow-side entry takes
  that entry's lanes, gaps kept, and its turn columns keep them; columns
  close up over an exit line that turns elsewhere.
* A branch into a TOP or BOTTOM entry never runs inside the target box.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from nf_metro.api import prepare_graph, render_string
from nf_metro.layout.lane_plan import (
    ForcedCrossing,
    recorded_crossing_pairs,
    recorded_lane_plan,
)
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.invariants import check_bundle_order_preserved
from nf_metro.parser.model import MetroGraph
from nf_metro.render import svg
from nf_metro.themes import resolve_theme

REGRESSIONS = Path(__file__).parent / "fixtures" / "regressions"
CARRY_SANDWICH = REGRESSIONS / "approach_order_carry_sandwich.mmd"
FORK_PORT_STOP = REGRESSIONS / "departure_order_fork_port_stop.mmd"
FEEDERS = REGRESSIONS / "approach_order_feeders_not_interleaved.mmd"


def _settled(
    path: Path, *, permissive: bool = False
) -> tuple[MetroGraph, MetroGraph, list[RoutedPath]]:
    """The source graph, the graph copy the render settled and the routes
    it draws."""
    settled: list[tuple[MetroGraph, svg._SettledRenderGeometry]] = []
    real = svg._settle_render_geometry

    def settle(
        graph: MetroGraph, *args: object, **kwargs: object
    ) -> svg._SettledRenderGeometry:
        result = real(graph, *args, **kwargs)  # type: ignore[arg-type]
        settled.append((graph, result))
        return result

    text = path.read_text()
    if permissive:
        text = "%%metro permissive: true\n" + text
    with warnings.catch_warnings(), pytest.MonkeyPatch.context() as mp:
        warnings.simplefilter("ignore")
        mp.setattr(svg, "_settle_render_geometry", settle)
        source = prepare_graph(text, source_dir=str(path.parent))
        svg.build_observed_render_plan(source, resolve_theme(None, source))
    drawn, last = settled[-1]
    return source, drawn, list(last.routes)


def _route(routes: list[RoutedPath], source: str, target: str, line: str):
    return next(
        r
        for r in routes
        if (r.edge.source, r.edge.target, r.line_id) == (source, target, line)
    )


@pytest.mark.parametrize("path", [CARRY_SANDWICH, FORK_PORT_STOP], ids=lambda p: p.stem)
def test_maps_with_a_planned_crossing_render(path: Path) -> None:
    render_string(path.read_text(), source_dir=str(path.parent))


@pytest.mark.parametrize(
    "path",
    [
        Path(__file__).parents[1]
        / "examples/topologies/exit_run_three_drop_columns.mmd",
        REGRESSIONS / "departure_order_shared_run_no_record.mmd",
    ],
    ids=lambda p: p.stem,
)
def test_turn_columns_close_up_over_a_line_turning_elsewhere(path: Path) -> None:
    """Two lines turning off one exit run either side of a line that turns
    elsewhere take adjacent columns: only a planned lane no line takes at the
    exit keeps a column."""
    render_string(path.read_text(), source_dir=str(path.parent))


def test_an_entry_crossing_lies_in_the_bundles_last_bend() -> None:
    """The junction bundle keeps the order it departs in up to its last bend
    and exchanges the recorded pair there, nowhere else."""
    graph, _drawn, routes = _settled(CARRY_SANDWICH)
    assert graph._lane_plan is not None
    assert (
        ForcedCrossing("sec3", None, ("l0", "l1"), "sec3__entry_left_4")
        in graph._lane_plan.tracks.forced
    )
    pair = [
        _route(routes, "__junction_6", "sec3__entry_left_4", line)
        for line in ("l0", "l1")
    ]
    flips = check_bundle_order_preserved(pair)
    assert [(v.segment_index, {v.line_a, v.line_b}) for v in flips] == [
        (len(pair[0].points) - 2, {"l0", "l1"})
    ]


def test_an_interleaved_entry_lane_records_its_crossing() -> None:
    graph, _drawn, _routes = _settled(FORK_PORT_STOP)
    assert graph._lane_plan is not None
    assert graph._lane_plan.tracks.forced == (
        ForcedCrossing(
            "sec1",
            None,
            ("l3", "l0", "l1", "l2"),
            "sec1__entry_left_2",
            turn="s1n1",
        ),
        ForcedCrossing(
            "sec2",
            None,
            ("l0", "l2", "l3", "l4"),
            "sec2__entry_left_3",
            "sec1__exit_right_1",
        ),
    )


def test_a_perpendicular_exit_takes_its_fed_entrys_gaps() -> None:
    """The exit's lines keep the gap their entry holds for the line another
    feeder brings in between them, and the drawn turn columns keep it."""
    graph, _drawn, routes = _settled(FORK_PORT_STOP)
    assert graph._lane_plan is not None
    perpendicular = graph._lane_plan.tracks.perpendicular["sec1__exit_right_1"]
    assert dict(perpendicular) == {"l4": 0, "l2": 1, "l3": 3}
    columns = {
        line: _route(routes, "sec1__exit_right_1", "sec2__entry_left_3", line).points[
            1
        ][0]
        for line in ("l4", "l2", "l3")
    }
    riser = _route(routes, "__junction_4", "sec2__entry_left_3", "l0").points[-2][0]
    step = 4.0
    assert columns["l2"] - columns["l4"] == pytest.approx(step)
    assert columns["l3"] - columns["l2"] == pytest.approx(2 * step)
    assert columns["l2"] < riser < columns["l3"]


def test_a_branch_into_a_bottom_entry_runs_outside_the_target_box() -> None:
    _graph, drawn, routes = _settled(FEEDERS, permissive=True)
    branch = _route(routes, "__junction_9", "sec3__entry_bottom_7", "l3")
    section = drawn.sections["sec3"]
    bottom = section.bbox_y + section.bbox_h
    level = [
        a[1]
        for a, b in zip(branch.points, branch.points[1:])
        if abs(a[1] - b[1]) < 1e-6 and abs(a[0] - b[0]) > 1e-6
    ]
    assert level and all(y > bottom for y in level[1:]), (bottom, branch.points)


@pytest.mark.parametrize(
    "stem",
    [
        "approach_order_carry_sandwich",
        "approach_order_multi_line_feeder",
        "departure_order_shared_run_no_record",
    ],
)
def test_a_crossing_through_a_forked_exit_pairs_the_connectors_it_carries(
    stem: str,
) -> None:
    path = REGRESSIONS / f"{stem}.mmd"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    plan = recorded_lane_plan(graph)
    assert plan is not None
    edges = {(e.source, e.target, e.line_id) for e in graph.edges}
    pairs = recorded_crossing_pairs(graph)
    crossings = [c for c in plan.tracks.forced if c.port and c.across]
    assert crossings
    for crossing in crossings:
        passing, *crossed_lines = crossing.lines
        crossed = {
            other
            for key, other in pairs
            if key[1] == crossing.port
            and key[2] == passing
            and other[2] in crossed_lines
        }
        assert {key[2] for key in crossed} == set(crossed_lines)
        assert crossed <= edges
        assert all(key[0] != crossing.across for key in crossed)
