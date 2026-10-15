"""Fan-junction positioning and source-section resolution."""

from __future__ import annotations

from nf_metro.layout.constants import (
    EDGE_TO_BUNDLE_CLEARANCE,
    JUNCTION_MARGIN,
)
from nf_metro.layout.geometry import sections_share_a_column
from nf_metro.parser.model import LineSpread, MetroGraph, PortSide, Station
from nf_metro.parser.route_topology import build_route_topology_query


def _required_junction_margin(n: int) -> float:
    """Margin needed so an n-line fan's leftmost lead-in clears the source.

    For an n-line concentric fan-out the per-line ``fan_delta`` stagger
    and per-line ``r_wrap`` curve radius cancel exactly: every line's
    first-corner curve start lands at ``junction.x``.  The required
    clearance therefore depends only on the lead-in length immediately
    before the curve (``CURVE_RADIUS``), not on the fan width.

    Returns ``JUNCTION_MARGIN`` directly: the baseline already exceeds the
    curve-start clearance at any fan width.
    """
    del n
    return JUNCTION_MARGIN


def _drops_down_the_junction_column(
    graph: MetroGraph, jid: str, exit_port_id: str | None
) -> bool:
    """Whether a branch leaves *jid* as a plain vertical down the junction's X.

    A TOP/BOTTOM entry port inherits the X of the junction feeding it only when
    the two stand in one grid column, with the entry section stacked beyond the
    feeder on the port's own side (``_align_tb_entry_port``); the branch then
    travels straight into the port, and the junction's own column is that
    channel's column.  A perpendicular entry outside the feeder's column is
    reached round a corner a curve runway further on, so the channel stands in
    the corner's column and the junction merely feeds it.
    """
    if exit_port_id is None:
        return False
    exit_port = graph.stations.get(exit_port_id)
    if exit_port is None or exit_port.section_id is None:
        return False
    feeder = graph.sections.get(exit_port.section_id)
    if feeder is None:
        return False
    for edge in graph.edges_from(jid):
        port = graph.ports.get(edge.target)
        if port is None or port.side not in (PortSide.TOP, PortSide.BOTTOM):
            continue
        entry_section_id = graph.stations[edge.target].section_id
        if entry_section_id is None:
            continue
        entry = graph.sections.get(entry_section_id)
        if entry is None:
            continue
        if not sections_share_a_column(feeder, entry):
            continue
        if (
            entry.grid_row > feeder.grid_row
            if port.side is PortSide.TOP
            else entry.grid_row < feeder.grid_row
        ):
            return True
    return False


def _flow_side_junction_margin(
    graph: MetroGraph, jid: str, exit_port_id: str | None, baseline: float
) -> float:
    """Distance a junction on a LEFT/RIGHT exit keeps from the section wall.

    The exit port sits on that wall, so the margin is also the clearance the
    junction's column has from it.  Where a branch drops straight down that
    column (:func:`_drops_down_the_junction_column`) the column is a channel
    running the inter-column gap, and a channel in a gap owes
    ``EDGE_TO_BUNDLE_CLEARANCE`` from the edges bounding it -- more than the
    curve runway *baseline* covers.  Every other branch turns a corner a
    runway past the junction, which leaves the channel a whole radius clear of
    the wall on the baseline; widening there would buy nothing and spend the
    gap another lane is nested in.
    """
    if not _drops_down_the_junction_column(graph, jid, exit_port_id):
        return baseline
    return max(baseline, EDGE_TO_BUNDLE_CLEARANCE)


def _junction_outgoing_line_count(graph: MetroGraph, jid: str) -> int:
    """Return the number of distinct line_ids fanning out of *jid*."""
    return len({e.line_id for e in graph.edges_from(jid)}) or 1


def _junction_incoming_line_count(graph: MetroGraph, jid: str) -> int:
    """Return the number of distinct line_ids merging into *jid*."""
    return len({e.line_id for e in graph.edges_to(jid)}) or 1


def reanchor_junctions(graph: MetroGraph) -> None:
    """Derive every junction from the section geometry it joins.

    A junction's coordinates are a function of its ports rather than independent
    data, so anything that moves a section leaves the junctions around it
    describing geometry nothing stands at, and every reader of the result -- a
    router, a planner, a guard -- is reading a map with no drawn counterpart.  A
    graph-wide rail layout anchors its junctions on per-line rail coordinates by
    a rule this placement does not reproduce, so it keeps the ones it has.
    """
    if graph.line_spread is not LineSpread.RAILS:
        _position_junctions(graph)


def _position_junctions(graph: MetroGraph) -> None:
    """Position junction stations at the midpoint of the inter-section gap.

    A junction is where bundled lines diverge to different downstream sections.
    It sits horizontally between the exit port and the entry ports, at the
    exit port's Y coordinate so lines travel straight from exit to junction.

    Merge junctions (N>1 predecessors, 1 entry port successor) are positioned by
    :func:`_position_merge_junction`, a margin back along the lead-in their entry
    port receives, so the merge point and the port are joined by a visible
    single-line segment.
    """
    topology = build_route_topology_query(graph)
    for jid in graph.junctions:
        junction = graph.stations.get(jid)
        if not junction:
            continue

        convergence = (
            topology.convergence_for_junction(jid) if topology is not None else None
        )
        divergence = (
            topology.divergence_for_junction(jid) if topology is not None else None
        )

        # Collect predecessors and successors
        predecessors: list[Station] = []
        successor_ports: list[Station] = []
        exit_port_id: str | None = None

        for edge in graph.edges_to(jid):
            src = graph.station_for_edge_source(edge)
            predecessors.append(src)
            if src.is_port:
                exit_port_id = edge.source
        for edge in graph.edges_from(jid):
            tgt = graph.station_for_edge_target(edge)
            if tgt.is_port:
                successor_ports.append(tgt)

        # Merge junction: N>1 predecessors, 1 entry port successor
        is_legacy_merge = (
            topology is None and len(predecessors) > 1 and len(successor_ports) == 1
        )
        if convergence is not None or is_legacy_merge:
            entry_port = (
                graph.stations[convergence.entry_port_id]
                if convergence is not None
                else successor_ports[0]
            )
            entry_port_obj = graph.ports.get(entry_port.id)
            if entry_port_obj and entry_port_obj.is_entry:
                _position_merge_junction(
                    junction,
                    predecessors,
                    entry_port,
                    entry_side=entry_port_obj.side,
                    n=_junction_incoming_line_count(graph, jid),
                )
                continue

        # Fan-out junction: 1 exit port predecessor, N>1 entry port successors
        if topology is not None and divergence is None:
            continue
        if divergence is not None:
            exit_port_id = divergence.exit_port_id
            exit_port = graph.stations[exit_port_id]
            exit_port_x = exit_port.x
            exit_port_y = exit_port.y
            entry_port_xs = [
                graph.stations[port_id].x for port_id in divergence.entry_port_ids
            ]
        else:
            exit_port_x = None
            exit_port_y = None
            entry_port_xs = []
            for pred in predecessors:
                if pred.is_port:
                    exit_port_x = pred.x
                    exit_port_y = pred.y
            for successor in successor_ports:
                entry_port_xs.append(successor.x)

        if exit_port_x is not None and exit_port_y is not None and entry_port_xs:
            margin = _required_junction_margin(
                _junction_outgoing_line_count(graph, jid)
            )
            exit_port_obj = graph.ports.get(exit_port_id) if exit_port_id else None
            if exit_port_obj and exit_port_obj.side == PortSide.BOTTOM:
                junction.x = exit_port_x
                junction.y = exit_port_y + margin
            elif exit_port_obj and exit_port_obj.side in (
                PortSide.RIGHT,
                PortSide.LEFT,
            ):
                direction = 1.0 if exit_port_obj.side == PortSide.RIGHT else -1.0
                junction.x = exit_port_x + direction * _flow_side_junction_margin(
                    graph, jid, exit_port_id, margin
                )
                junction.y = exit_port_y
            else:
                nearest_entry_x = min(entry_port_xs, key=lambda x: abs(x - exit_port_x))
                direction = 1.0 if nearest_entry_x > exit_port_x else -1.0
                junction.x = exit_port_x + direction * margin
                junction.y = exit_port_y


def _position_merge_junction(
    junction: Station,
    predecessors: list[Station],
    entry_port: Station,
    entry_side: PortSide | None = None,
    n: int = 1,
) -> None:
    """Position a merge junction on the lead-in its entry port receives.

    The junction is where the converging lines become one stroke, so it has to
    stand on the segment that stroke travels: a LEFT/RIGHT port is approached
    horizontally, so the junction shares the port's Y and stands a margin back
    along X; a TOP/BOTTOM port is approached down (or up) its own column, so the
    junction shares the port's X and stands a margin back along Y.  Seating a
    merge for a perpendicular port on the horizontal instead puts the shared
    segment along the section's top or bottom edge rather than in the inter-row
    gap the feeders reserve.  *n* is the number of distinct lines merging at the
    junction; passing 1 falls back to the baseline margin.
    """
    margin = _required_junction_margin(n)
    if entry_side in (PortSide.TOP, PortSide.BOTTOM):
        junction.x = entry_port.x
        junction.y = entry_port.y + (-margin if entry_side is PortSide.TOP else margin)
        return
    max_pred_x = max(p.x for p in predecessors)
    # Normal forward fan-in: merge just past the right-most predecessor on its
    # way into a target to the right.  But when the target sits well to the LEFT
    # of the predecessors (a collector like MultiQC fed from across the map),
    # merging at max_pred_x forces the whole merged bundle to backtrack the full
    # width into the entry.  Merge local to the target instead, so only the
    # individual feeders make the long approach and the merge->entry hop is short.
    if entry_port.x < max_pred_x - margin:
        junction.x = entry_port.x - margin
    else:
        junction.x = max_pred_x + margin
    junction.y = entry_port.y


def _resolve_source_section_id(
    graph: MetroGraph, edge_source: str, junction_ids: set[str]
) -> str | None:
    """Resolve the section ID of an edge's source, tracing through junctions.

    For port stations, returns section_id directly. For junctions, follows
    edges backward to find the connected port's section.
    """
    src = graph.stations.get(edge_source)
    if not src:
        return None
    src_section_id = src.section_id
    if edge_source in junction_ids:
        for e2 in graph.edges_to(edge_source):
            s2 = graph.station_for_edge_source(e2)
            if s2.section_id:
                src_section_id = s2.section_id
                break
    return src_section_id


def _resolve_source_xy(
    graph: MetroGraph,
    edge_source: str,
    junction_ids: set[str],
    _seen: set[str] | None = None,
) -> tuple[float, float] | None:
    """Return effective (x, y) for an edge source.

    For port stations, returns coordinates directly.  For junctions,
    derives coordinates from the feeding exit port, mirroring
    ``_position_junctions`` logic so that entry-port alignment does
    not depend on junctions being pre-positioned.  Recurses through
    chained junctions (junction-to-junction edges) to find the
    underlying exit port.
    """
    src = graph.stations.get(edge_source)
    if not src:
        return None
    if edge_source not in junction_ids:
        return src.x, src.y

    if _seen is None:
        _seen = set()
    if edge_source in _seen:
        return src.x, src.y
    _seen.add(edge_source)

    # Junction: find the feeding exit port and compute placement.
    chained: list[str] = []
    for e in graph.edges_to(edge_source):
        if e.source in junction_ids:
            chained.append(e.source)
            continue
        exit_st = graph.station_for_edge_source(e)
        if not exit_st.is_port:
            continue
        exit_port_obj = graph.ports.get(e.source)
        if not exit_port_obj:
            return exit_st.x, exit_st.y
        # Mirror _position_junctions: the resolved junction X must match
        # what _position_junctions would write so that downstream
        # alignment passes consuming this helper see the same coordinate.
        margin = _required_junction_margin(
            _junction_outgoing_line_count(graph, edge_source)
        )
        if exit_port_obj.side == PortSide.BOTTOM:
            return exit_st.x, exit_st.y + margin
        elif exit_port_obj.side in (PortSide.RIGHT, PortSide.LEFT):
            flow_margin = _flow_side_junction_margin(
                graph, edge_source, e.source, margin
            )
            direction = 1.0 if exit_port_obj.side == PortSide.RIGHT else -1.0
            return exit_st.x + direction * flow_margin, exit_st.y
        else:
            return exit_st.x + margin, exit_st.y

    # Recurse through chained junctions to find the underlying exit port.
    for js in chained:
        resolved = _resolve_source_xy(graph, js, junction_ids, _seen)
        if resolved is not None and resolved != (0.0, 0.0):
            return resolved

    # Fallback: use junction station's current coordinates.
    return src.x, src.y
