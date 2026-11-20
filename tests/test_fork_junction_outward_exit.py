"""A fork off a TOP or BOTTOM exit stands outside its section and leaves it there.

One rule for every side: the fork junction stands one margin outward of its
exit on the exit's column, the leg into it continues the exit's own lanes, and
no branch runs back through the exit's section.  A branch whose target lies back
across the exit's row traverses the channel on the exit's side of that row,
clear of the row's header badges.  A merge trunk into a TOP or BOTTOM entry
takes the staircase the port's other feeders take instead of reaching the port's
edge from beyond the section.

Which side of the row a branch passes is the lane plan's: the branch holding the
outermost lanes of its turn at the exit passes on the side the exit faces, a
branch inside other lanes on the other side.  Every branch is drawn on the side
the record states.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.layout.constants import COORD_TOLERANCE, INTER_ROW_HEADER_CLEARANCE
from nf_metro.layout.geometry import segment_intersects_bbox
from nf_metro.layout.lane_plan import recorded_lane_plan
from nf_metro.layout.port_order_plan import SideRule, port_travel
from nf_metro.layout.routing.common import (
    right_normal_axis_sign,
    segments_properly_cross,
)
from nf_metro.parser.model import PortSide
from nf_metro.themes import resolve_theme

ROOT = Path(__file__).resolve().parents[1]
REGRESSIONS = ROOT / "tests" / "fixtures" / "regressions"
TOPOLOGIES = ROOT / "examples" / "topologies"

LR_TOP = REGRESSIONS / "perp_entry_column_merge_feeder.mmd"
LR_BOTTOM = REGRESSIONS / "approach_order_draft_off_lane.mmd"
BT_TOP = REGRESSIONS / "approach_order_carry_sandwich.mmd"
TB_BOTTOM = TOPOLOGIES / "fold_stacked_branch.mmd"
BT_TOP_LEFTWARD = REGRESSIONS / "approach_order_feeders_not_interleaved.mmd"
SEAM = REGRESSIONS / "departure_order_perpendicular_seam.mmd"
MULTI_LINE = REGRESSIONS / "approach_order_multi_line_feeder.mmd"
OVERRIDDEN = REGRESSIONS / "approach_order_overridden_later.mmd"

FORK_FIXTURES = [LR_TOP, LR_BOTTOM, BT_TOP, TB_BOTTOM]
INSET = 2.0


def _render(path: Path):
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    plan = render_graph_result(
        graph, resolve_theme(None, graph, "light"), RenderConfig()
    ).plan
    drawn = list(zip(plan.routes, plan.route_polylines, strict=True))
    _RECORDS[path] = recorded_lane_plan(graph)
    return _DrawnGraph(plan.graph, graph.junction_ids), drawn


_RECORDS: dict[Path, object] = {}


class _DrawnGraph:
    """The render plan's graph, in the frame its polylines are drawn in."""

    def __init__(self, graph, junction_ids) -> None:
        self._graph, self.junction_ids = graph, junction_ids

    def __getattr__(self, name):
        return getattr(self._graph, name)


def _perp_forks(graph) -> dict[str, str]:
    """``{fork junction: the TOP/BOTTOM exit port feeding it}``."""
    forks = {}
    for jid in graph.junction_ids:
        for edge in graph.edges_to(jid):
            port = graph.ports.get(edge.source)
            if (
                port is not None
                and not port.is_entry
                and port.side
                in (
                    PortSide.TOP,
                    PortSide.BOTTOM,
                )
            ):
                if len({e.target for e in graph.edges_from(jid)}) > 1:
                    forks[jid] = edge.source
    return forks


def _inset_box(section) -> tuple[float, float, float, float]:
    return (
        section.bbox_x + INSET,
        section.bbox_y + INSET,
        section.bbox_x + section.bbox_w - INSET,
        section.bbox_y + section.bbox_h - INSET,
    )


def _crosses(line, box) -> bool:
    return any(segment_intersects_bbox(*a, *b, box) for a, b in pairwise(line))


def _ids(paths: list[Path]) -> list[str]:
    return [p.stem for p in paths]


@pytest.mark.parametrize("fixture", FORK_FIXTURES, ids=_ids(FORK_FIXTURES))
def test_a_fork_junction_stands_outside_its_exit_on_the_exit_column(
    fixture: Path,
) -> None:
    graph, _drawn = _render(fixture)
    forks = _perp_forks(graph)
    assert forks
    for jid, exit_id in forks.items():
        junction, port = graph.stations[jid], graph.stations[exit_id]
        outward = 1.0 if graph.ports[exit_id].side is PortSide.BOTTOM else -1.0
        assert abs(junction.x - port.x) <= COORD_TOLERANCE, jid
        assert (junction.y - port.y) * outward > COORD_TOLERANCE, jid


@pytest.mark.parametrize(
    "fixture",
    [*FORK_FIXTURES, BT_TOP_LEFTWARD],
    ids=_ids([*FORK_FIXTURES, BT_TOP_LEFTWARD]),
)
def test_no_line_at_a_fork_junction_runs_inside_its_exit_section(
    fixture: Path,
) -> None:
    graph, drawn = _render(fixture)
    forks = _perp_forks(graph)
    for route, line in drawn:
        jid = route.edge.source if route.edge.source in forks else route.edge.target
        if jid not in forks:
            continue
        exit_section = graph.sections[graph.ports[forks[jid]].section_id]
        assert not _crosses(line, _inset_box(exit_section)), (
            f"{route.edge.source}->{route.edge.target} {route.line_id}: {line}"
        )


@pytest.mark.parametrize(
    "fixture", [LR_TOP, LR_BOTTOM, BT_TOP], ids=_ids([LR_TOP, LR_BOTTOM, BT_TOP])
)
def test_the_leg_into_a_fork_junction_continues_the_exit_lanes(
    fixture: Path,
) -> None:
    graph, drawn = _render(fixture)
    forks = _perp_forks(graph)
    arrival_x = {
        (route.edge.target, route.line_id): line[-1][0]
        for route, line in drawn
        if route.edge.target in forks.values()
    }
    legs = [
        (route, line)
        for route, line in drawn
        if forks.get(route.edge.target) == route.edge.source
    ]
    assert legs
    for route, line in legs:
        assert len(line) == 2 and abs(line[0][0] - line[1][0]) <= COORD_TOLERANCE, line
        expected = arrival_x[(route.edge.source, route.line_id)]
        assert abs(line[0][0] - expected) <= COORD_TOLERANCE, (route.line_id, line)


@pytest.mark.parametrize(
    "fixture",
    [LR_TOP, BT_TOP, REGRESSIONS / "approach_order_overridden_later.mmd"],
    ids=["lr_top", "bt_top", "overridden_later"],
)
def test_a_branch_back_across_the_exit_row_clears_the_header_badges(
    fixture: Path,
) -> None:
    graph, drawn = _render(fixture)
    forks = _perp_forks(graph)
    checked = 0
    for route, line in drawn:
        exit_id = forks.get(route.edge.source)
        if exit_id is None or graph.ports[exit_id].side is not PortSide.TOP:
            continue
        exit_row = graph.sections[graph.ports[exit_id].section_id].grid_row
        row_top = min(
            s.bbox_y for s in graph.sections.values() if s.grid_row == exit_row
        )
        traverse = [
            a[1]
            for a, b in pairwise(line)
            if abs(a[1] - b[1]) <= COORD_TOLERANCE and a[1] < row_top
        ]
        if not traverse:
            continue
        checked += 1
        assert max(traverse) <= row_top - INTER_ROW_HEADER_CLEARANCE, (
            route.line_id,
            line,
        )
    assert checked


def _merge_entries(graph) -> dict[str, str]:
    """``{merge junction: the TOP/BOTTOM entry port it feeds}``."""
    out = {}
    for jid in graph.junction_ids:
        targets = {e.target for e in graph.edges_from(jid)}
        port = graph.ports.get(next(iter(targets))) if len(targets) == 1 else None
        if (
            port is not None
            and port.is_entry
            and port.side
            in (
                PortSide.TOP,
                PortSide.BOTTOM,
            )
        ):
            out[jid] = port.id
    return out


@pytest.mark.parametrize("fixture", [LR_TOP, SEAM], ids=["merge_feeder", "seam"])
def test_a_feed_into_a_perpendicular_entry_merge_crosses_no_section(
    fixture: Path,
) -> None:
    graph, drawn = _render(fixture)
    merges = _merge_entries(graph)
    feeds = [(route, line) for route, line in drawn if route.edge.target in merges]
    assert feeds
    boxes = [_inset_box(s) for s in graph.sections.values()]
    for route, line in feeds:
        assert not any(_crosses(line, box) for box in boxes), (
            f"{route.edge.source}->{route.edge.target} {route.line_id}: {line}"
        )


@pytest.mark.parametrize(
    ("fixture", "entry", "exit_port", "side"),
    [
        (MULTI_LINE, "z2__entry_left_2", "z0__exit_top_0", "below"),
        (BT_TOP, "sec2__entry_left_3", "sec0__exit_top_2", "below"),
        (BT_TOP, "sec3__entry_left_4", "sec0__exit_top_2", "below"),
        (OVERRIDDEN, "sec3__entry_left_4", "sec1__exit_top_0", "above"),
        (LR_TOP, "sec4__entry_left_5", "sec2__exit_top_1", "above"),
    ],
    ids=["inner_bundle", "inner_near", "inner_far", "outermost", "lr_outermost"],
)
def test_a_fork_branch_passes_the_row_on_the_side_no_outer_lane_crosses(
    fixture: Path, entry: str, exit_port: str, side: str
) -> None:
    """The outermost branch of a turn passes on the side its TOP exit faces;
    a branch inside other lanes passes the other side."""
    graph, _drawn = _render(fixture)
    approach = _RECORDS[fixture].port_orders.approaches[(entry, exit_port)]  # type: ignore[attr-defined]
    below = approach.side * right_normal_axis_sign(port_travel(graph.ports[entry])) > 0
    assert approach.rule is SideRule.FORK_NEST
    assert ("below" if below else "above") == side


@pytest.mark.parametrize(
    "fixture",
    [MULTI_LINE, BT_TOP, OVERRIDDEN, LR_TOP],
    ids=_ids([MULTI_LINE, BT_TOP, OVERRIDDEN, LR_TOP]),
)
def test_no_two_branches_of_a_fork_cross_each_other(fixture: Path) -> None:
    """Lines leaving one fork for different entries never cross."""
    graph, drawn = _render(fixture)
    forks = _perp_forks(graph)
    branches = [(route, line) for route, line in drawn if route.edge.source in forks]
    assert branches
    crossings = [
        (a.edge.target, a.line_id, b.edge.target, b.line_id, hit)
        for i, (a, line_a) in enumerate(branches)
        for b, line_b in branches[i + 1 :]
        if a.edge.source == b.edge.source
        and a.edge.target != b.edge.target
        and a.line_id != b.line_id
        for p0, p1 in pairwise(line_a)
        for q0, q1 in pairwise(line_b)
        if (hit := segments_properly_cross(p0, p1, q0, q1, reject_collinear_touch=True))
    ]
    assert crossings == []


@pytest.mark.parametrize(
    ("fixture", "entry"),
    [
        (MULTI_LINE, "z2__entry_left_2"),
        (REGRESSIONS / "departure_order_rejoining_rotation.mmd", "sec2__entry_left_3"),
    ],
    ids=["multi_line_feeder", "rejoining_rotation"],
)
def test_a_far_side_branch_hugs_the_sections_it_passes(
    fixture: Path, entry: str
) -> None:
    """A branch round its row's far side runs along the far edges of the
    sections it passes, not round the taller section it forks from."""
    graph, drawn = _render(fixture)
    forks = _perp_forks(graph)
    branch = [
        line
        for route, line in drawn
        if route.edge.source in forks and route.edge.target == entry
    ]
    assert branch
    fork = next(
        r.edge.source
        for r, _line in drawn
        if r.edge.source in forks and r.edge.target == entry
    )
    exit_section = graph.sections[graph.ports[forks[fork]].section_id]
    assert max(y for line in branch for _x, y in line) < (
        exit_section.bbox_y + exit_section.bbox_h
    )
