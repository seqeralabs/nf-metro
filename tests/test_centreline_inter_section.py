"""Inter-section simple-shape routes are built via the centreline templates.

The straight, L-shape, single-corner and vertical-drop inter-section handlers
construct their routes by describing a centreline and fanning it with
``build_concentric_bundle`` (``layout/routing/centrelines.py``) rather than
assembling per-line ``points`` / ``curve_radii`` by hand.  A bundle built that
way is offset-baked (:attr:`OffsetRegime.BAKED`) and correct by construction --
its corners stay concentric and its lines keep a constant side-of-travel order.

These tests pin that on the fixtures that exercise each shape: a regression to
a hand-rolled, render-time-offset path would leave the multi-line inter-section
bundles :attr:`OffsetRegime.DEFERRED`, and a flat or mis-signed radius would
trip the render-path curve guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nf_metro.layout.routing.inter_section_handlers as inter_handlers
from nf_metro.api import prepare_graph
from nf_metro.layout.constants import CURVE_RADIUS, DIAGONAL_RUN
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import (
    OffsetRegime,
    compute_station_offsets,
    route_edges,
)
from nf_metro.layout.routing.common import Direction
from nf_metro.layout.routing.context import _build_routing_context
from nf_metro.layout.routing.corners import l_shape_stagger
from nf_metro.layout.routing.invariants import (
    assert_render_curve_invariants,
    check_bundle_order_preserved,
    check_concentric_bundle_corners,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

# Fixtures whose inter-section routing exercises the simple shapes migrated
# onto the centreline templates: straight runs, plain L-shapes, the bottom-exit
# junction corner, and TB perpendicular / bottom-exit drops.
MIGRATED_SHAPE_FIXTURES = [
    EXAMPLES / "topologies" / "asymmetric_tree.mmd",
    EXAMPLES / "topologies" / "complex_multipath.mmd",
    EXAMPLES / "topologies" / "fold_stacked_branch.mmd",
    EXAMPLES / "rnaseq_sections.mmd",
    EXAMPLES / "variantbenchmarking_auto.mmd",
]


def _route(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, offsets, routes


@pytest.mark.parametrize("path", MIGRATED_SHAPE_FIXTURES, ids=lambda p: p.stem)
def test_inter_section_bundles_are_concentric_and_unflipped(path: Path) -> None:
    graph, offsets, routes = _route(path)
    assert check_concentric_bundle_corners(graph, routes, offsets) == []
    assert check_bundle_order_preserved(routes) == []
    # The render path's always-on guard must accept the laid-out routes.
    assert_render_curve_invariants(graph, routes, offsets)


@pytest.mark.parametrize("path", MIGRATED_SHAPE_FIXTURES, ids=lambda p: p.stem)
def test_multi_line_inter_section_bundles_are_offset_baked(path: Path) -> None:
    """A multi-line inter-section bundle routed via a centreline bakes offsets.

    The centreline templates emit ``offsets_applied`` routes (the per-line
    offset is in the points, not deferred to the renderer's heuristic).  A
    revert to a hand-rolled, render-time-offset L-shape would leave the
    multi-line inter-section bundles un-baked and red this assertion.
    """
    _graph, _offsets, routes = _route(path)
    bundles: dict[tuple[str, str], list] = {}
    for r in routes:
        if r.is_inter_section:
            bundles.setdefault((r.edge.source, r.edge.target), []).append(r)
    multiline = {k: v for k, v in bundles.items() if len({r.line_id for r in v}) > 1}
    assert multiline, f"{path.stem}: expected a multi-line inter-section bundle"
    assert all(
        r.offset_regime is OffsetRegime.BAKED for rs in multiline.values() for r in rs
    )


FROZEN_FUZZ = REPO_ROOT / "tests" / "fixtures" / "hash_seed_determinism"

WESTWARD_JUNCTION_FAN_L_SHAPES = [
    ("seed_15.mmd", "__junction_21", "__merge_9", "l0"),
    ("seed_15.mmd", "__junction_27", "__merge_11", "l0"),
    ("seed_15.mmd", "__junction_27", "__merge_12", "l2"),
    ("seed_77.mmd", "__junction_37", "__merge_11", "l0"),
]


@pytest.mark.parametrize(
    ("name", "source", "target", "line_id"),
    WESTWARD_JUNCTION_FAN_L_SHAPES,
    ids=lambda value: str(value),
)
def test_westward_junction_fan_l_shape_lands_on_its_endpoint_offsets(
    name: str, source: str, target: str, line_id: str
) -> None:
    """A staggered junction-fan L-shape run leftward starts and ends on its lanes.

    The member sits ``-delta`` along the bundle's right-hand normal, which is
    ``-y`` on a westward run, so the centreline's horizontal legs must be lifted
    by the same signed amount for the member to land on the source and target
    offsets rather than ``2 * delta`` beside them.
    """
    path = FROZEN_FUZZ / name
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    ctx = _build_routing_context(graph, DIAGONAL_RUN, CURVE_RADIUS, offsets)
    edge = ctx.edge_by_key[(source, target, line_id)]
    fan = ctx.junction_fan_info[(source, target, line_id)]
    src, tgt = graph.stations[source], graph.stations[target]
    geometry = inter_handlers._l_shape_fan_source_turn(edge, src, tgt, fan, ctx)
    delta = l_shape_stagger(*fan, geometry.turn_direction, ctx.offset_step)
    assert geometry.run_direction is Direction.L
    assert delta != pytest.approx(0.0)

    route = inter_handlers._route_l_shape_fan(edge, src, tgt, fan, ctx)

    assert route.points[0][1] == pytest.approx(src.y + offsets[(source, line_id)])
    assert route.points[-1][1] == pytest.approx(tgt.y + offsets[(target, line_id)])
