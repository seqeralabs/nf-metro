"""Routing invariants for the packed-cell handoff topology."""

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing.common import apply_route_offsets, column_gap_edges
from nf_metro.layout.routing.core import route_edges
from nf_metro.layout.routing.invariants import check_packed_cell_same_line_handoff
from nf_metro.layout.routing.offsets import compute_station_offsets
from nf_metro.layout.section_placement import _inter_row_routing_minimums
from nf_metro.parser.mermaid import parse_metro_mermaid

TOPOLOGY = (
    Path(__file__).parents[1]
    / "examples"
    / "topologies"
    / "packed_cell_right_exit_left_entry_wrap.mmd"
)


def _routed():
    graph = parse_metro_mermaid(TOPOLOGY.read_text())
    compute_layout(graph, validate=True)
    offsets = compute_station_offsets(graph)
    routed_paths = route_edges(graph, station_offsets=offsets)
    routes = {
        (route.edge.source, route.edge.target, route.line_id): apply_route_offsets(
            route, offsets
        )
        for route in routed_paths
    }
    return graph, routes, routed_paths, offsets


def test_adjacent_continuation_stays_level_while_branch_peels_below() -> None:
    _graph, routes, _routed_paths, _offsets = _routed()
    ont = routes[("__junction_11", "assemble__entry_left_5", "ont")]
    hifi = routes[("__junction_11", "assemble__entry_left_5", "hifi")]
    short = routes[("__junction_11", "qc__entry_left_6", "short")]

    assert ont[0][1] == pytest.approx(ont[-1][1])
    assert hifi[0][1] == pytest.approx(hifi[-1][1])
    assert hifi[0][1] - ont[0][1] == pytest.approx(5.0)
    assert short[0][1] > hifi[0][1]
    assert short[1][1] == pytest.approx(short[0][1])
    assert short[2][1] > short[1][1]


def test_far_packed_cell_branch_reuses_near_sibling_corridor() -> None:
    graph, routes, _routed_paths, _offsets = _routed()
    qc = routes[("__junction_10", "qc__entry_left_6", "reference")]
    annotation = routes[("__junction_10", "annot__entry_left_9", "reference")]
    qc_section = graph.sections["qc"]

    assert [c for p in annotation[:3] for c in p] == pytest.approx(
        [c for p in qc[:3] for c in p]
    )
    assert annotation[3] == pytest.approx(qc[3])
    gap_left, gap_right = column_gap_edges(graph, 1, 2, row=0)
    assert gap_left <= min(qc[3][0], annotation[3][0])
    assert max(qc[3][0], annotation[3][0]) <= gap_right
    assert min(y for _x, y in annotation) >= (
        graph.stations["annot__entry_left_9"].y - 0.1
    )

    qc_bottom = qc_section.bbox_y + qc_section.bbox_h
    under_qc = [
        (a, b)
        for a, b in zip(annotation, annotation[1:])
        if a[1] == pytest.approx(b[1])
        and a[1] > qc_bottom
        and min(a[0], b[0]) < qc_section.bbox_x
        and max(a[0], b[0]) > qc_section.bbox_x + qc_section.bbox_w
    ]
    assert under_qc


def test_packed_cell_handoff_guard_rejects_opposite_opening_directions() -> None:
    graph, _routes, routed_paths, offsets = _routed()

    assert check_packed_cell_same_line_handoff(graph, routed_paths, offsets) == []

    annotation = next(
        route
        for route in routed_paths
        if (
            route.edge.source,
            route.edge.target,
            route.line_id,
        )
        == ("__junction_10", "annot__entry_left_9", "reference")
    )
    turn_x, turn_y = annotation.points[1]
    annotation.points[2] = (turn_x, turn_y - 40.0)

    violations = check_packed_cell_same_line_handoff(graph, routed_paths, offsets)
    assert len(violations) == 1
    assert violations[0].near_target == "qc__entry_left_6"
    assert violations[0].far_target == "annot__entry_left_9"


def test_straight_polish_diamond_uses_a_branch_as_its_trunk() -> None:
    graph, _routes, _routed_paths, _offsets = _routed()

    source_y = graph.stations["assembly_file_in"].y
    join_y = graph.stations["polished_file"].y
    upper_branch_y = graph.stations["polish_short"].y
    trunk_branch_y = graph.stations["polish_ont"].y

    assert source_y == pytest.approx(trunk_branch_y)
    assert join_y == pytest.approx(trunk_branch_y)
    assert upper_branch_y < trunk_branch_y


def test_top_row_boxes_share_one_header_line() -> None:
    graph, _routes, _routed_paths, _offsets = _routed()

    top_row = [
        section
        for section in graph.sections.values()
        if section.grid_row == 0 and section.bbox_h > 0
    ]

    assert len({round(section.bbox_y, 6) for section in top_row}) == 1


def test_lower_row_closes_only_to_its_overlapping_upper_sections() -> None:
    graph, _routes, _routed_paths, _offsets = _routed()
    qc = graph.sections["qc"]
    scaffold = graph.sections["scaffold"]
    required = _inter_row_routing_minimums(graph)[(0, 1)]

    actual = scaffold.bbox_y - (qc.bbox_y + qc.bbox_h)

    assert actual == pytest.approx(required)
