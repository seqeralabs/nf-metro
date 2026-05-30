"""Layout phase: junctions (extracted from engine.py, see #451)."""

from __future__ import annotations

from nf_metro.layout.constants import (
    JUNCTION_MARGIN,
)
from nf_metro.parser.model import MetroGraph, PortSide, Station


def _required_junction_margin(n: int) -> float:
    """Margin needed so an n-line fan's leftmost lead-in clears the source.

    For an n-line concentric fan-out the per-line ``fan_delta`` stagger
    and per-line ``r_wrap`` curve radius cancel exactly: every line's
    first-corner curve start lands at ``junction.x``.  The required
    clearance therefore depends only on the lead-in length immediately
    before the curve (``CURVE_RADIUS``), not on the fan width.

    Returns ``JUNCTION_MARGIN`` directly - the baseline already exceeds
    the curve-start clearance requirement for any reasonable ``n``.
    The signature keeps a per-junction ``n`` so future routing layouts
    that genuinely depend on fan width can override it without changing
    every call site.
    """
    del n  # currently unused; see docstring
    return JUNCTION_MARGIN


def _junction_outgoing_line_count(graph: MetroGraph, jid: str) -> int:
    """Return the number of distinct line_ids fanning out of *jid*."""
    return len({e.line_id for e in graph.edges_from(jid)}) or 1


def _junction_incoming_line_count(graph: MetroGraph, jid: str) -> int:
    """Return the number of distinct line_ids merging into *jid*."""
    return len({e.line_id for e in graph.edges_to(jid)}) or 1


def _position_junctions(graph: MetroGraph) -> None:
    """Position junction stations at the midpoint of the inter-section gap.

    A junction is where bundled lines diverge to different downstream sections.
    It sits horizontally between the exit port and the entry ports, at the
    exit port's Y coordinate so lines travel straight from exit to junction.

    Merge junctions (N>1 predecessors, 1 entry port successor) are positioned
    at ``max(pred.x) + _required_junction_margin(n)``, y = entry_port.y to
    create a visible single-line segment from merge point to entry.
    """
    for jid in graph.junctions:
        junction = graph.stations.get(jid)
        if not junction:
            continue

        # Collect predecessors and successors
        predecessors: list[Station] = []
        successor_ports: list[Station] = []
        exit_port_id: str | None = None

        for edge in graph.edges_to(jid):
            src = graph.stations.get(edge.source)
            if src:
                predecessors.append(src)
                if src.is_port:
                    exit_port_id = edge.source
        for edge in graph.edges_from(jid):
            tgt = graph.stations.get(edge.target)
            if tgt and tgt.is_port:
                successor_ports.append(tgt)

        # Merge junction: N>1 predecessors, 1 entry port successor
        if len(predecessors) > 1 and len(successor_ports) == 1:
            entry_port = successor_ports[0]
            entry_port_obj = graph.ports.get(entry_port.id)
            if entry_port_obj and entry_port_obj.is_entry:
                _position_merge_junction(
                    junction,
                    predecessors,
                    entry_port,
                    n=_junction_incoming_line_count(graph, jid),
                )
                continue

        # Fan-out junction: 1 exit port predecessor, N>1 entry port successors
        exit_port_x: float | None = None
        exit_port_y: float | None = None
        entry_port_xs: list[float] = []

        for pred in predecessors:
            if pred.is_port:
                exit_port_x = pred.x
                exit_port_y = pred.y

        for succ in successor_ports:
            entry_port_xs.append(succ.x)

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
                junction.x = exit_port_x + direction * margin
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
    n: int = 1,
) -> None:
    """Position a merge junction near the entry port it feeds.

    Places at x = max(predecessor.x) + _required_junction_margin(n),
    y = entry_port.y so all converging lines share a visible single-line
    segment into the entry port.  *n* is the number of distinct lines
    merging at the junction; passing 1 falls back to the baseline margin.
    """
    max_pred_x = max(p.x for p in predecessors)
    margin = _required_junction_margin(n)
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
            s2 = graph.stations.get(e2.source)
            if s2 and s2.section_id:
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
        exit_st = graph.stations.get(e.source)
        if not exit_st or not exit_st.is_port:
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
        elif exit_port_obj.side == PortSide.RIGHT:
            return exit_st.x + margin, exit_st.y
        elif exit_port_obj.side == PortSide.LEFT:
            return exit_st.x - margin, exit_st.y
        else:
            return exit_st.x + margin, exit_st.y

    # Recurse through chained junctions to find the underlying exit port.
    for js in chained:
        resolved = _resolve_source_xy(graph, js, junction_ids, _seen)
        if resolved is not None and resolved != (0.0, 0.0):
            return resolved

    # Fallback: use junction station's current coordinates.
    return src.x, src.y
