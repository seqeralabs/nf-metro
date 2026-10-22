"""A convergence trunk whose trial lane lies on a member run of its own bundle.

``qc``'s return trunk to Export and ``merge``'s skip to Summarise share the
corridor under Annotate, and both trial lanes land on its floor.  Above
``merge`` the trunk crosses only ``merge``'s rise into Summarise; below it, both
``qc`` descents into the trunk cross ``merge``'s run.  The floor is the box-edge
clearance under Annotate, so the trunk can only take the cheaper side if
``merge`` yields it the floor.
"""

from __future__ import annotations

import itertools
import warnings
from collections.abc import Mapping
from pathlib import Path

import pytest

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.layout.constants import (
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    INTER_ROW_EDGE_CLEARANCE,
)
from nf_metro.layout.routing import normalize
from nf_metro.layout.routing.common import segments_properly_cross
from nf_metro.parser.model import MetroGraph
from nf_metro.render.plan import RenderPlan
from nf_metro.themes import resolve_theme

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "examples" / "topologies" / "return_trunk_over_member_run.mmd"

LAYOUT_OPTIONS = pytest.mark.parametrize(
    "layout_options",
    [None, {"track_gap": 3}],
    ids=["default", "track_gap_3"],
)


def _render(
    layout_options: Mapping[str, object] | None,
) -> tuple[MetroGraph, RenderPlan]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(
            FIXTURE.read_text(),
            source_dir=str(FIXTURE.parent),
            layout_options=layout_options,
        )
        result = render_graph_result(
            graph, resolve_theme(None, graph, "light"), RenderConfig()
        )
    return graph, result.plan


def _crossing_count(plan: RenderPlan) -> int:
    polylines = list(zip(plan.routes, plan.route_polylines, strict=True))
    return sum(
        segments_properly_cross(a, b, c, d, reject_collinear_touch=False) is not None
        for (ra, pa), (rb, pb) in itertools.combinations(polylines, 2)
        if ra.line_id != rb.line_id
        for a, b in itertools.pairwise(pa)
        for c, d in itertools.pairwise(pb)
    )


@LAYOUT_OPTIONS
def test_trunk_takes_the_side_its_feeders_cross_least(
    layout_options: Mapping[str, object] | None,
) -> None:
    _graph, plan = _render(layout_options)
    # qc's three legs cross merge's row track; merge's rise crosses the trunk.
    assert _crossing_count(plan) <= 4


@LAYOUT_OPTIONS
def test_corridor_runs_keep_the_box_edge_clearance_under_annotate(
    layout_options: Mapping[str, object] | None,
) -> None:
    graph, plan = _render(layout_options)
    section = graph.sections["annotate"]
    left, right = section.bbox_x, section.bbox_x + section.bbox_w
    bottom = section.bbox_y + section.bbox_h
    under = [
        (route.line_id, a[1])
        for route, points in zip(plan.routes, plan.route_polylines, strict=True)
        for a, b in itertools.pairwise(points)
        if abs(a[1] - b[1]) <= COORD_TOLERANCE
        and a[1] > bottom
        and min(a[0], b[0]) < right
        and max(a[0], b[0]) > left
    ]
    assert {line_id for line_id, _y in under} == {"qc", "merge"}
    assert [
        (line_id, y)
        for line_id, y in under
        if y - bottom < INTER_ROW_EDGE_CLEARANCE - COORD_TOLERANCE
    ] == []


@pytest.mark.parametrize(
    ("relative_path", "trunk_lines"),
    [
        ("examples/topologies/return_trunk_over_member_run.mmd", {"qc"}),
        ("examples/riboseq_metro.mmd", set()),
        ("tests/fixtures/curve_invariant_repros/riboseq_inter_row_corridor.mmd", set()),
    ],
)
def test_member_yields_only_a_lane_a_plan_states_as_its_trunk(
    relative_path: str,
    trunk_lines: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``annotation``'s riboseq run lies on a ``riboseq`` lane of two convergence
    feeders and a plain run.  The feeders' opening corners pin that lane, but no
    plan states it as a trunk, so settlement never packs it past the member and
    it is no trunk for the member to yield to."""
    coincident: list[str] = []
    yielded_to: set[str] = set()
    real = normalize._coincident_convergence_trunk

    def spy(lane, pinned_obstacles, movable_route_ids):
        trunk = real(lane, pinned_obstacles, movable_route_ids)
        coincident.extend(
            other.line_id
            for other in pinned_obstacles
            if lane.axis == 1 and abs(other.coord - lane.coord) <= COORD_TOLERANCE_FINE
        )
        if trunk is not None:
            yielded_to.add(trunk.line_id)
        return trunk

    monkeypatch.setattr(normalize, "_coincident_convergence_trunk", spy)
    path = ROOT / relative_path
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        render_graph_result(graph, resolve_theme(None, graph, "light"), RenderConfig())
    assert coincident
    assert yielded_to == trunk_lines
