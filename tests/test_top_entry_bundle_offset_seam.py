"""A line carrying a bundle offset must meet its perpendicular port at one X.

When a line splits off a shared multi-line trunk it carries a non-zero
within-bundle offset.  If that line then crosses into a section through a
``entry: top`` or ``entry: bottom`` port, its inter-section leg and the
section's intra-section drop share that single port marker.  The approach must
therefore land on the X that drop departs from; landing on the inbound offset
instead parts the two legs at the section edge -- a boundary jitter where the
stroke steps sideways as it crosses.

``top_entry_bundle_offset_seam`` is the committed minimal fixture: line ``b``
splits off the ``a,b,c`` trunk at a junction (giving it a non-zero offset) and
drops into ``dst`` through its ``entry: top`` port, whose own drop departs from
the port's bare X, so the inbound offset has to taper away.
``fold_left_exit_right_entry`` carries an offset-free top/side entry and guards
the zero-offset case.  ``bottom_entry_same_row_boundary`` and
``entry_hint_shared_edge`` carry a two-line bundle into a ``entry: bottom``
port whose drop *does* fan the lines apart, and so pin the opposite sign: a
BOTTOM approach is reached ascending, so the same landing X reads as the
opposite lateral to a TOP one.
``lr_perpendicular_ports_overflow`` guards a feeder leaving through a RIGHT
exit: the descent cannot occupy the exit's own column, so the port stands in
the column the descent occupies.
"""

from __future__ import annotations

import pytest

from nf_metro import api
from nf_metro.layout.phases.guards import _entry_approach_offenders
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.layout.routing.common import apply_route_offsets
from nf_metro.layout.routing.corners import outer_lane_radius
from nf_metro.layout.routing.invariants import (
    check_perp_entry_boundary_consistent,
    check_seam_segments_meet_at_port,
)
from nf_metro.parser.model import PortSide

FIXTURES = [
    "examples/topologies/top_entry_bundle_offset_seam.mmd",
    "examples/topologies/fold_left_exit_right_entry.mmd",
    "examples/topologies/straight_drop_below.mmd",
    "examples/topologies/peeloff_straight_drop_near_wall.mmd",
    "examples/topologies/lr_perpendicular_ports_overflow.mmd",
    "examples/topologies/bottom_entry_same_row_boundary.mmd",
    "examples/topologies/entry_hint_shared_edge.mmd",
]

# A TOP entry stacked directly under the RIGHT exit that feeds it: the feeder
# leaves horizontally, so its descent column stands a turn's runway out from
# the exit and the port has to stand there with it.
HORIZONTAL_EXIT_REPRO = "examples/topologies/lr_perpendicular_ports_overflow.mmd"

FAR_SIDE_DIRECTION_CASES = [
    (direction, exit_side, entry_side)
    for direction in ("LR", "RL")
    for exit_side in ("top", "bottom")
    for entry_side in ("left", "right")
] + [
    (direction, exit_side, entry_side)
    for direction, exit_side in (("TB", "bottom"), ("BT", "top"))
    for entry_side in ("left", "right")
]

REPRO = "examples/topologies/top_entry_bundle_offset_seam.mmd"

# A two-line bundle crossing a BOTTOM entry port: every line's approach lands on
# the X the port's own drop into the section departs from, so the pair crosses
# the bottom edge as two parallel strokes rather than swapping lanes at it.
BOTTOM_BUNDLE = "examples/topologies/bottom_entry_same_row_boundary.mmd"

# Junctions feeding a TOP entry port directly below them (shared X): the drop
# descends as one constant-X vertical into the port, with no lateral
# lead-out-and-jog straddling the section boundary.  ``straight_drop_below``
# drops in a column open on one side; ``peeloff_straight_drop_near_wall`` drops
# in a two-sided gap running one curve radius outside the flanking boxes' walls.
STRAIGHT_DROPS = [
    "examples/topologies/straight_drop_below.mmd",
    "examples/topologies/peeloff_straight_drop_near_wall.mmd",
]


def _route(path: str):
    graph = api.prepare_graph(open(path).read())
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    return graph, routes, offsets


@pytest.mark.parametrize("path", FIXTURES)
def test_seams_meet_at_port(path: str) -> None:
    graph, routes, offsets = _route(path)
    gaps = check_seam_segments_meet_at_port(graph, routes, offsets)
    assert not gaps, "\n".join(g.message() for g in gaps)


@pytest.mark.parametrize("path", FIXTURES)
def test_perp_entry_boundary_consistent(path: str) -> None:
    graph, routes, _offsets = _route(path)
    violations = check_perp_entry_boundary_consistent(graph, routes)
    assert not violations, "\n".join(v.message() for v in violations)


def test_top_entry_descent_lands_on_port_x() -> None:
    """The descent into ``dst``'s top port ends at the port's own X.

    Line ``b`` reaches the port through ``s1 -> d1`` (its offset-bearing
    inter-section descent).  With the offset tapered away its final vertical
    leg shares the port marker's X, so the intra-section drop out of the port
    continues the same stroke.
    """
    graph, routes, offsets = _route(REPRO)
    port = graph.ports["dst__entry_top_3"]
    descent = next(
        r for r in routes if r.line_id == "b" and r.edge.target == "dst__entry_top_3"
    )
    landing_x = apply_route_offsets(descent, offsets)[-1][0]
    assert landing_x == pytest.approx(port.x, abs=1.0)


def test_horizontal_exit_drop_turns_once_onto_the_port_column() -> None:
    """A TOP entry fed by a RIGHT exit is reached by one turn and one drop.

    A horizontal exit cannot turn down in its own column: the turn needs a run
    beside the box, so the descent occupies the column one outer-lane radius
    out.  The port stands in that column, and the descent is then a lead-in of
    exactly that run, a single turn, and a straight vertical onto the port --
    no lateral step at the boundary, which the intra-section drop leaves at the
    port's own X.
    """
    graph, routes, offsets = _route(HORIZONTAL_EXIT_REPRO)
    exit_st = graph.stations["upstream__exit_right_0"]
    port = graph.ports["annotation__entry_top_2"]
    assert port.x == pytest.approx(exit_st.x + outer_lane_radius(1), abs=1.0)
    descent = next(r for r in routes if r.edge.target == port.id)
    expected = [(exit_st.x, exit_st.y), (port.x, exit_st.y), (port.x, port.y)]
    actual = apply_route_offsets(descent, offsets)
    assert len(actual) == len(expected)
    assert [c for p in actual for c in p] == pytest.approx(
        [c for p in expected for c in p], abs=1.0
    )


def test_perpendicular_exit_wraps_to_left_entry_outward_side() -> None:
    graph, routes, offsets = _route(HORIZONTAL_EXIT_REPRO)
    port = graph.ports["downstream__entry_left_3"]
    approach = next(r for r in routes if r.edge.target == port.id)
    assert not _entry_approach_offenders(graph, [approach])

    points = apply_route_offsets(approach, offsets)
    assert points[-3][0] == pytest.approx(points[-2][0], abs=1.0)
    assert points[-2][0] < graph.sections[port.section_id].bbox_x


@pytest.mark.parametrize(
    ("source_direction", "exit_side", "entry_side"),
    FAR_SIDE_DIRECTION_CASES,
)
def test_perpendicular_exit_far_side_wrap_directional_mirrors(
    source_direction: str,
    exit_side: str,
    entry_side: str,
) -> None:
    target_col = 0 if entry_side == "left" else 1
    source_col = 1 - target_col
    source_row = 0 if exit_side == "bottom" else 1
    target_row = 1 - source_row
    target_direction = "LR" if entry_side == "left" else "RL"
    graph = api.prepare_graph(
        f"""%%metro line: l1 | Line 1 | #e64980 | solid
%%metro grid: source | {source_col},{source_row}
%%metro grid: target | {target_col},{target_row}
graph LR
    subgraph source [Source]
        %%metro direction: {source_direction}
        %%metro exit: {exit_side} | l1
        s1[S1]
        s2[S2]
        s1 -->|l1| s2
    end
    subgraph target [Target]
        %%metro direction: {target_direction}
        %%metro entry: {entry_side} | l1
        t1[T1]
        t2[T2]
        t1 -->|l1| t2
    end
    s2 -->|l1| t1
"""
    )
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    port = next(
        port
        for port in graph.ports.values()
        if port.section_id == "target" and port.is_entry
    )
    approach = next(route for route in routes if route.edge.target == port.id)
    assert not _entry_approach_offenders(graph, [approach])

    points = apply_route_offsets(approach, offsets)
    assert len(points) == 5
    assert points[0][0] == pytest.approx(points[1][0], abs=1.0)
    assert points[-3][0] == pytest.approx(points[-2][0], abs=1.0)
    section = graph.sections[port.section_id]
    if entry_side == "left":
        assert points[-2][0] < section.bbox_x
    else:
        assert points[-2][0] > section.bbox_x + section.bbox_w


def test_bottom_entry_bundle_lands_on_the_drop_lanes() -> None:
    """Each line of a BOTTOM-entry bundle lands on the lane its drop departs on.

    ``proc``'s bottom port fans ``qc`` and ``main`` apart on its way to
    ``fastqc``, so the two approaches land on those same two X values, in that
    same order -- the mirror order would put each line's approach on its
    bundle-mate's lane and cross the pair at the section's bottom edge.
    """
    graph, routes, offsets = _route(BOTTOM_BUNDLE)
    port = graph.ports["proc__entry_bottom_2"]
    landings = {}
    for line_id in ("qc", "main"):
        approach = next(
            r
            for r in routes
            if r.line_id == line_id and r.edge.target == port.id and r.is_inter_section
        )
        departure = next(
            r for r in routes if r.line_id == line_id and r.edge.source == port.id
        )
        landings[line_id] = apply_route_offsets(approach, offsets)[-1][0]
        leaving_x = apply_route_offsets(departure, offsets)[0][0]
        assert landings[line_id] == pytest.approx(leaving_x, abs=1.0)
    assert landings["main"] > landings["qc"]


@pytest.mark.parametrize("path", STRAIGHT_DROPS)
def test_junction_drop_below_is_one_vertical_run(path: str) -> None:
    """A junction's drop into the TOP port directly below is one vertical run.

    Every point from where the descent turns vertical down to the port shares
    the port's X, so the line enters the TOP port from directly above -- no
    lateral lead-out to a parallel channel and jog back onto the port marker.
    """
    graph, routes, offsets = _route(path)
    drop = next(
        r
        for r in routes
        if r.is_inter_section
        and (p := graph.ports.get(r.edge.target)) is not None
        and p.side is PortSide.TOP
        and r.edge.source in graph.junctions
        and abs(graph.stations[r.edge.source].x - p.x) <= 1.0
    )
    port = graph.ports[drop.edge.target]
    pts = apply_route_offsets(drop, offsets)
    assert pts[-1] == pytest.approx((port.x, port.y), abs=1.0)
    # From the first point on the port's column, the run stays on that column.
    descent = [p for p in pts if p[0] == pytest.approx(port.x, abs=1.0)]
    assert len(descent) >= 2
    assert all(x == pytest.approx(port.x, abs=1.0) for x, _ in descent)
    # No point sits on the far side of the port's column (an out-and-back).
    assert max(x for x, _ in pts) <= port.x + 1.0
