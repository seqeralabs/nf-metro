"""A divergence junction rides the lanes its feeding exit port settled on.

When a bundle is shifted wholesale at an exit port so the port's own feeder run
comes out level, the divergence junction a few pixels downstream has to take the
same lanes.  Where it keeps an earlier set instead, the short run between the
two is spent climbing off the port's lane, and every branch beyond the junction
spends it again coming back: two shallow slants across runs far too short to
form a turn on, either side of a junction that is invisible in the render.

``junction_entry_lane_step`` places such a junction ten pixels past the exit
port feeding it, with one branch continuing along the row and one leaving it, so
both halves of that mismatch are drawn.

A pass that reverses a whole section's lane order has to carry the junctions
riding its exit ports along, or the bundle is transposed across those same ten
pixels instead and every branch leaves the vertex on the far side of the lane
its feed arrived on.  ``seed_41`` reverses three such sections.

Riding the port has a limit.  A port whose bundle came out of the shift in a
different order is offering the junction a transposition rather than the drop it
took, and swapping which branch leaves the vertex on which side can push a
branch out across a section it never calls at.  ``fold_stacked_branch`` folded
to one station column packs the grid tightly enough to draw that.
"""

from __future__ import annotations

from pathlib import Path

from nf_metro.api import prepare_graph
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases._common import routes_through_unrelated_sections
from nf_metro.layout.route_topology import divergence_junction_exit_ports
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import apply_route_offsets
from nf_metro.parser.mermaid import parse_metro_mermaid

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = "examples/topologies/junction_entry_lane_step.mmd"
FOLD_FIXTURE = "examples/topologies/fold_stacked_branch.mmd"
REVERSED_FIXTURE = "tests/fixtures/hash_seed_determinism/seed_41.mmd"

_TOLERANCE = 0.5


def _routed() -> tuple[object, dict[tuple[str, str], float], list]:
    graph = parse_metro_mermaid((ROOT / FIXTURE).read_text(), max_station_columns=15)
    compute_layout(graph)
    offsets = dict(compute_station_offsets(graph))
    return graph, offsets, route_edges(graph, station_offsets=offsets)


def _drawn(route, offsets) -> list[tuple[float, float]]:
    return apply_route_offsets(route, offsets)


def test_junction_holds_the_lanes_of_the_exit_port_feeding_it() -> None:
    """Every line the junction shares with its feeder sits on the feeder's lane."""
    graph, offsets, _routes = _routed()
    feeders = divergence_junction_exit_ports(graph)
    assert feeders, f"{FIXTURE} no longer carries a junction fed by an exit port"
    mismatched = {
        (junction_id, line_id): (offsets.get((port_id, line_id)), junction_lane)
        for junction_id, port_id in feeders.items()
        for line_id in graph.station_lines(junction_id)
        if (junction_lane := offsets.get((junction_id, line_id))) is not None
        and offsets.get((port_id, line_id)) is not None
        and abs(offsets[(port_id, line_id)] - junction_lane) > _TOLERANCE
    }
    assert not mismatched


def test_the_run_from_the_exit_port_into_the_junction_is_level() -> None:
    """The stub between a port and the junction it feeds draws flat."""
    graph, offsets, routes = _routed()
    feeders = divergence_junction_exit_ports(graph)
    slanted = [
        f"{route.edge.source}->{route.edge.target} ({route.line_id}): {points}"
        for route in routes
        if feeders.get(route.edge.target) == route.edge.source
        for points in [_drawn(route, offsets)]
        if max(y for _x, y in points) - min(y for _x, y in points) > _TOLERANCE
    ]
    assert not slanted, "\n".join(slanted)


def test_the_branch_continuing_along_the_junction_row_stays_level() -> None:
    """A branch whose target shares the junction's row never leaves it."""
    graph, offsets, routes = _routed()
    junctions = set(divergence_junction_exit_ports(graph))
    continuing = [
        (route, _drawn(route, offsets))
        for route in routes
        if route.edge.source in junctions
        and abs(
            graph.stations[route.edge.source].y - graph.stations[route.edge.target].y
        )
        <= _TOLERANCE
    ]
    assert continuing, f"{FIXTURE} no longer carries a branch along the junction row"
    slanted = [
        f"{route.edge.source}->{route.edge.target} ({route.line_id}): {points}"
        for route, points in continuing
        if max(y for _x, y in points) - min(y for _x, y in points) > _TOLERANCE
    ]
    assert not slanted, "\n".join(slanted)


def test_no_branch_of_a_junction_is_plotted_over_a_section_it_never_calls_at() -> None:
    """Every branch leaving a divergence junction stays out of foreign section boxes.

    ``fold_stacked_branch`` at a fold threshold below its natural width stacks
    the sections a fan-out junction serves directly under the section feeding
    it, so a branch that leaves the vertex on the wrong lane descends across
    that feeding section's box instead of along its trunk.
    """
    graph = prepare_graph(
        (ROOT / FOLD_FIXTURE).read_text(), layout_options={"fold_threshold": 1}
    )
    offsets = dict(compute_station_offsets(graph))
    routes = route_edges(graph, station_offsets=offsets)
    junctions = set(divergence_junction_exit_ports(graph))
    assert junctions, f"{FOLD_FIXTURE} no longer carries a divergence junction"
    through = [
        f"{route.edge.source}->{route.edge.target} ({route.line_id}) "
        f"crosses {section_id}"
        for route, section_id in routes_through_unrelated_sections(
            graph, routes=routes, offsets=offsets
        )
        if route.edge.source in junctions
    ]
    assert not through, "\n".join(through)


def test_a_reversed_section_carries_the_junctions_riding_its_exit_ports() -> None:
    """No junction ranks its feeder's lines backwards after the feeder reverses.

    Ranked by the lane each line takes at the junction, the lanes the same lines
    take at the exit port feeding it rank the same way.  Where they run
    backwards the bundle is transposed over the few pixels between the two, so
    each branch leaves the vertex on the opposite side of the bundle from the
    feed that arrived on it.  ``seed_41`` is the corpus's only map whose
    sections are reversed end-to-end by a fan-out junction's drop into a
    same-column entry, which is the pass that strands a junction this way.
    """
    graph = parse_metro_mermaid((ROOT / REVERSED_FIXTURE).read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    feeders = divergence_junction_exit_ports(graph)
    assert feeders, f"{REVERSED_FIXTURE} no longer carries a junction fed by a port"
    transposed = []
    for junction_id, port_id in feeders.items():
        ranked = sorted(
            (offsets[(junction_id, line_id)], offsets[(port_id, line_id)])
            for line_id in graph.station_lines(junction_id)
            if (junction_id, line_id) in offsets and (port_id, line_id) in offsets
        )
        offered = [port_lane for _held_lane, port_lane in ranked]
        if offered != sorted(offered):
            transposed.append(f"{junction_id} <- {port_id}: {ranked}")
    assert not transposed, "\n".join(transposed)
