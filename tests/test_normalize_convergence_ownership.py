"""Convergence ownership boundaries for post-emission normalisation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import nf_metro.layout.routing.normalize as normalize
from nf_metro.layout.routing.common import (
    RoutedPath,
    _v_segment_crosses_other_section,
)
from nf_metro.parser.model import Edge, MetroGraph, Section


def _route(
    line_id: str,
    points: list[tuple[float, float]],
    *,
    owned: tuple[int, ...] = (),
    member_owned: tuple[int, ...] = (),
) -> RoutedPath:
    return RoutedPath(
        edge=Edge("fork", f"target_{line_id}", line_id),
        line_id=line_id,
        points=points,
        is_inter_section=True,
        curve_radii=[10.0] * (len(points) - 2),
        convergence_owned_segment_ranks=owned,
        route_system_owned_segment_ranks=member_owned,
    )


def test_fanout_traverse_bundling_uses_complete_group_ownership() -> None:
    owned = _route(
        "owned",
        [(0.0, 0.0), (10.0, 0.0), (10.0, 40.0), (90.0, 40.0), (90.0, 90.0)],
        owned=(3,),
    )
    sibling = _route(
        "sibling",
        [(0.0, 0.0), (13.0, 0.0), (13.0, 55.0), (80.0, 55.0), (80.0, 90.0)],
    )
    members = normalize._fanout_traverse_legs([owned, sibling])[("fork", True)]

    assert not normalize._fanout_traverse_group_is_movable(members)
    before = [list(route.points) for route in (owned, sibling)]
    normalize._bundle_divergent_distinct_traverses(
        [owned, sibling], SimpleNamespace(offset_step=3.0)
    )
    assert [owned.points, sibling.points] == before


def test_divergent_source_order_excludes_declared_merge_fanouts() -> None:
    routes = [
        RoutedPath(
            edge=Edge("fork", target, line_id),
            line_id=line_id,
            points=[
                (0.0, source_y),
                (x, source_y),
                (x, turn_y),
                (100.0, turn_y),
            ],
            is_inter_section=True,
            curve_radii=[10.0, 10.0],
        )
        for line_id, target, source_y, turn_y, x in (
            ("a", "target_a", 0.0, 30.0, 20.0),
            ("b", "semantic_merge_fanout", 4.0, 60.0, 40.0),
            ("c", "target_c", 8.0, 45.0, 60.0),
        )
    ]
    ctx = SimpleNamespace(
        graph=MetroGraph(),
        offset_step=4.0,
        curve_radius=10.0,
        merge_fanouts={"semantic_merge_fanout"},
    )

    normalize._bundle_divergent_distinct_descents(routes, ctx)

    assert {route.line_id: route.points[1][0] for route in routes} == {
        "a": 28.0,
        "b": 20.0,
        "c": 24.0,
    }


def test_vertical_section_obstruction_preserves_open_segment_endpoints() -> None:
    graph = MetroGraph(
        sections={
            "blocker": Section(
                id="blocker",
                name="Blocker",
                bbox_x=100.0,
                bbox_y=10.0,
                bbox_w=20.0,
                bbox_h=10.0,
            )
        }
    )

    assert not _v_segment_crosses_other_section(graph, 110.0, 0.0, 10.0, set())
    assert _v_segment_crosses_other_section(graph, 99.0, 0.0, 15.0, set(), margin=1.0)
    assert not _v_segment_crosses_other_section(
        graph,
        99.0,
        0.0,
        15.0,
        set(),
        margin=1.0,
        include_margin_boundary=False,
    )


@pytest.mark.parametrize("ownership", ["owned", "member_owned"])
def test_bypass_nesting_ownership_is_per_segment_boundary(
    monkeypatch, ownership: str
) -> None:
    """Neither a convergence plan's leg nor a member plan's is bypass nesting's."""
    wrap = _route("wrap", [(10.0, 40.0), (90.0, 40.0)])
    owned = _route(
        "owned",
        [(0.0, 80.0), (0.0, 60.0), (100.0, 60.0), (100.0, 120.0)],
        **{ownership: (1,)},
    )
    movable = _route(
        "movable",
        [(0.0, 80.0), (0.0, 70.0), (100.0, 70.0), (100.0, 120.0)],
    )
    monkeypatch.setattr(
        normalize,
        "iter_inter_row_gaps",
        lambda _graph: [(0, 0.0, 100.0)],
    )
    monkeypatch.setattr(
        normalize,
        "_route_gap_span",
        lambda _graph, route, _top, _bottom: (
            (True, False) if route is wrap else (False, True)
        ),
    )

    assert not normalize._bypass_nesting_leg_is_movable(owned, 1)
    assert normalize._bypass_nesting_leg_is_movable(movable, 1)
    owned_before = list(owned.points)
    normalize._nest_bypass_above_over_top_wrap(
        [wrap, owned, movable], SimpleNamespace(graph=object())
    )

    assert owned.points == owned_before
    assert movable.points == [
        (0.0, 80.0),
        (0.0, 26.0),
        (100.0, 26.0),
        (100.0, 120.0),
    ]
