"""Station offsets: the lane plan's lanes, as every routing pass reads them.

:func:`compute_station_offsets` writes the layout's lane plan record
(:mod:`nf_metro.layout.lane_plan`) for every station line, each lane in offset
steps along its section's lane frame and each perpendicular port's offsets
across its travel as its drawing reads them.  The record is sealed at the
layout's first run at or after stage 5.1; a run before that, or one inside
validation, writes the record its current placement plans.

The module also keeps the same-band entry classifiers the invariants and
routing read: :func:`same_row_bypass_entry`, :func:`_convergence_feeders` and
the corridor predicates.
"""

from __future__ import annotations

from collections.abc import (
    Mapping,
)
from dataclasses import dataclass
from typing import Literal

from nf_metro.layout.constants import (
    SAME_Y_TOLERANCE,
    graph_offset_step,
)
from nf_metro.layout.geometry import (
    AxisFrame,
)
from nf_metro.layout.lane_plan import (
    approach_source,
    lane_plan_of,
    planned_offsets,
    recorded_lane_plan,
)
from nf_metro.layout.port_order_plan import Approach, port_travel
from nf_metro.layout.routing.common import (
    bypass_bottom_y,
    merge_junction_ids,
)
from nf_metro.layout.routing.context import (
    _has_intervening_sections,
    _resolve_section_colrow,
)
from nf_metro.layout.routing.orientation import lateral_order_sign
from nf_metro.parser.model import (
    Edge,
    LineSpread,
    MetroGraph,
    Station,
)

_SAME_Y_TOLERANCE: float = SAME_Y_TOLERANCE


def compute_station_offsets(
    graph: MetroGraph,
    offset_step: float | None = None,
) -> dict[tuple[str, str], float]:
    """Every ``(station, line)``'s offset from the layout's lane plan.

    Rail mode bakes absolute rail Ys into the route points and the pill span,
    so per-line offsets are not used; the map is empty.
    """
    if graph.line_spread is LineSpread.RAILS:
        return {}
    resolved = offset_step if offset_step is not None else graph_offset_step(graph)
    return planned_offsets(graph, lane_plan_of(graph), resolved)


def _convergence_feeders(
    graph: MetroGraph, port_id: str
) -> list[tuple[str, int, bool]] | None:
    """Classify a LEFT entry port's bypass-convergence feeders.

    Returns ``[(line_id, source_col, is_bypass), ...]`` when several lines
    converge into *port_id* and need approach-depth slotting; ``None`` otherwise.

    All-bypass bundles qualify. Mixed bundles require one line per source
    column, then qualify when either at least two feeders bypass intervening
    sections or a single bypass and its nearer feeders descend from one
    off-target row. A single bypass joined by a flat same-row feeder does not
    form a climbing bundle and remains in its ordinary lane order.  A feeder
    that loops round the section into the port
    (:func:`loops_round_entry_section`) has no approach depth on this side, so
    its port is left to :func:`same_row_bypass_entry`.
    """
    target_col, target_row = _resolve_section_colrow(graph, graph.stations[port_id])
    if target_col is None:
        return None

    feeders: list[tuple[str, int, bool]] = []
    source_rows: set[int | None] = set()
    for edge in graph.edges_to(port_id):
        source = graph.station_for_edge_source(edge)
        source_col, source_row = _resolve_section_colrow(graph, source)
        if source_col is None or loops_round_entry_section(
            graph, source, graph.stations[port_id]
        ):
            return None
        bypass = abs(target_col - source_col) > 1 and _has_intervening_sections(
            graph, source_col, target_col, source_row
        )
        feeders.append((edge.line_id, source_col, bypass))
        source_rows.add(source_row)

    source_cols = {source_col for _line_id, source_col, _bypass in feeders}
    if len(source_cols) < 2:
        return None
    if all(bypass for _line_id, _source_col, bypass in feeders):
        return feeders
    if len(source_cols) != len(feeders) or len({f[0] for f in feeders}) != len(feeders):
        return None

    bypass_count = sum(bypass for _line_id, _source_col, bypass in feeders)
    if bypass_count >= 2:
        return feeders
    if bypass_count == 1 and len(source_rows) == 1 and target_row not in source_rows:
        return feeders
    return None


def cross_row_convergence_channel_order(
    graph: MetroGraph, port_id: str
) -> list[str] | None:
    """Outer-to-inner channels for a single-bypass off-row convergence."""
    feeders = _convergence_feeders(graph, port_id)
    if feeders is None:
        return None
    if sum(bypass for _, _, bypass in feeders) != 1:
        return None
    return [line_id for line_id, _col, _bypass in sorted(feeders, key=lambda f: f[1])]


def _arrives_on_same_row_bypass(
    graph: MetroGraph,
    source: Station,
    port_station: Station,
    *,
    source_colrow: tuple[int | None, int | None] | None = None,
    target_colrow: tuple[int | None, int | None] | None = None,
) -> bool:
    """Whether a feeder level with *port_station* reaches it on a vertical leg.

    A source on the port's own grid row that hops past intervening sections in
    that row detours off the row and turns back into the port, so its connector
    is not flat even though both ends share a Y.  The below-row channel is read
    from :func:`bypass_bottom_y`, the lane the router lays the usual detour on; a
    channel no lower than the port is a straight run instead.  Which side the
    detour takes is the approach record's (:func:`_recorded_approach`).
    Callers that already resolved either grid cell pass it in.
    """
    target_col, target_row = target_colrow or _resolve_section_colrow(
        graph, port_station
    )
    source_col, source_row = source_colrow or _resolve_section_colrow(graph, source)
    if target_col is None or source_col is None or source_row != target_row:
        return False
    if abs(target_col - source_col) <= 1 or not _has_intervening_sections(
        graph, source_col, target_col, source_row
    ):
        return False
    channel_y = bypass_bottom_y(
        graph, source_col, target_col, src_row=source_row, tgt_row=target_row
    )
    return channel_y > port_station.y + _SAME_Y_TOLERANCE


def loops_round_entry_section(
    graph: MetroGraph, source: Station, port_station: Station
) -> bool:
    """Whether *source* lies past *port_station* along its section's flow.

    The port faces away from such a source, so the connector has to loop round
    the section's top or bottom and turn back into the port: a U-turn.
    """
    port = graph.ports.get(port_station.id)
    if port is None:
        return False
    direction = graph.section_for_port(port).direction
    flow_axis = AxisFrame.axes_for_direction(direction)[0]
    past = getattr(source, flow_axis) - getattr(port_station, flow_axis)
    return past * AxisFrame.flow_sign(direction) > _SAME_Y_TOLERANCE


def _feeder_is_corridor(
    graph: MetroGraph, source: Station, port_station: Station, tol: float
) -> bool:
    """Whether *source* reaches *port_station* on a vertical leg, not level.

    Either its base Y lies more than *tol* off the port's, or it shares the
    port's Y but detours through a same-row bypass
    (:func:`_arrives_on_same_row_bypass`).
    """
    return abs(source.y - port_station.y) > tol or _arrives_on_same_row_bypass(
        graph, source, port_station
    )


_Approach = Literal["flat", "below", "above"]


@dataclass(frozen=True)
class SameRowBypassEntry:
    """A flow-side entry where same-row bypass lines join other feeders.

    ``feeders`` names each line's same-row feeder station and ``bypass_depth``
    each bypass line's column distance to the port; every line not in
    ``bypass_depth`` arrives flat.  ``descending`` holds the bypass lines that
    come down into the port from above; the rest climb in from below.
    ``u_turn`` holds the bypass lines whose feeder lies past the port along the
    section's flow, so they loop round the section itself rather than detour
    past the sections before it.
    """

    feeders: Mapping[str, str]
    bypass_depth: Mapping[str, int]
    descending: frozenset[str] = frozenset()
    u_turn: frozenset[str] = frozenset()

    @property
    def rising(self) -> frozenset[str]:
        return frozenset(self.bypass_depth) - self.descending

    @property
    def flat(self) -> frozenset[str]:
        return frozenset(self.feeders) - frozenset(self.bypass_depth)


def _approach_record(graph: MetroGraph, edge: Edge) -> Approach | None:
    """The lane plan's approach record for *edge*'s connector into its entry."""
    plan = recorded_lane_plan(graph)
    assert plan is not None
    source = approach_source(graph, edge.source, edge.line_id)
    return plan.port_orders.approaches.get((edge.target, source))


def _recorded_approach(graph: MetroGraph, edge: Edge) -> _Approach:
    """The screen side the lane plan's approach record sends *edge* round
    its entry's band on; ``"flat"`` where it records no side or no record
    exists."""
    approach = _approach_record(graph, edge)
    if approach is None or approach.side == 0:
        return "flat"
    travel = port_travel(graph.ports[edge.target])
    return "below" if approach.side * lateral_order_sign(travel) > 0 else "above"


def same_row_bypass_entry(graph: MetroGraph, port_id: str) -> SameRowBypassEntry | None:
    """Classify *port_id* as a same-row bypass entry, or ``None``.

    Every line must reach the port from a feeder level with it on the port's
    grid row, either flat or through a same-row bypass; at least one of each is
    needed.  A bypass looping round the section into the port
    (:func:`loops_round_entry_section`) needs only a second feeder station of
    any kind: the loop turns its lines over against every other approach, so
    the port has an order to settle even with no flat line.  A line fed more than
    once qualifies only when all its feeders approach from the same side --
    flat, from below, or from above -- and either all loop round the section
    or none do, since feeders arriving different ways cannot all be honoured
    by a single lane, so such a port keeps its ordinary lane order.  A
    merge-fed confluence is left to the routing-time band alignment that owns
    it (see
    :func:`_port_fed_through_merge_junction`).  Each bypass's side is the
    approach record's (:func:`_recorded_approach`).
    """
    port_station = graph.stations[port_id]
    target_col, target_row = _resolve_section_colrow(graph, port_station)
    if target_col is None or _port_fed_through_merge_junction(graph, port_id):
        return None
    sides: dict[str, set[_Approach]] = {}
    feeders: dict[str, str] = {}
    depth: dict[str, int] = {}
    bypass_edges: list[Edge] = []
    u_turn: set[str] = set()
    detour: set[str] = set()
    for edge in graph.edges_to(port_id):
        lid = edge.line_id
        line_sides = sides.setdefault(lid, set())
        source = graph.station_for_edge_source(edge)
        source_col, source_row = _resolve_section_colrow(graph, source)
        dy = source.y - port_station.y
        if source_row != target_row or abs(dy) > _SAME_Y_TOLERANCE:
            line_sides.add("below" if dy > 0 else "above")
            continue
        feeders.setdefault(lid, edge.source)
        loops = loops_round_entry_section(graph, source, port_station)
        if source_col is not None and (
            loops
            or _arrives_on_same_row_bypass(
                graph,
                source,
                port_station,
                source_colrow=(source_col, source_row),
                target_colrow=(target_col, target_row),
            )
        ):
            bypass_edges.append(edge)
            depth.setdefault(lid, abs(target_col - source_col))
            (u_turn if loops else detour).add(lid)
        else:
            line_sides.add("flat")
    if set(sides) != set(feeders) or not bypass_edges:
        return None
    if not u_turn and not any("flat" in line_sides for line_sides in sides.values()):
        return None
    # A detour leaves its row by construction (:func:`_arrives_on_same_row_bypass`),
    # so only a loop round the section can turn out to be drawn level.
    approaches = [
        "below" if approach == "flat" and edge.line_id not in u_turn else approach
        for edge, approach in zip(
            bypass_edges, [_recorded_approach(graph, edge) for edge in bypass_edges]
        )
    ]
    for edge, approach in zip(bypass_edges, approaches):
        sides[edge.line_id].add(approach)
    if any(len(line_sides) > 1 for line_sides in sides.values()):
        return None
    level = {lid for lid, line_sides in sides.items() if line_sides == {"flat"}}
    u_turn -= level
    detour -= level
    joined = bool(level) or (bool(u_turn) and len(set(feeders.values())) > 1)
    if not (u_turn or detour) or not joined or not u_turn.isdisjoint(detour):
        return None
    descending = frozenset(
        edge.line_id
        for edge, approach in zip(bypass_edges, approaches)
        if approach == "above"
    )
    bypass_depth = {lid: d for lid, d in depth.items() if lid not in level}
    return SameRowBypassEntry(feeders, bypass_depth, descending, frozenset(u_turn))


def _port_fed_through_merge_junction(graph: MetroGraph, port_id: str) -> bool:
    """Whether a line reaches *port_id* through a merge (reconvergence) junction.

    Such a port is a merge-fed confluence: one leg arrives through the merge
    junction while another descends from a higher row.  The descent geometry of
    that confluence is owned by ``_align_merge_fed_confluence_to_band`` at
    routing time, which seats the descent onto the band's nesting order; a
    port-offset reorder here would fight it and land the descending line on the
    inner internal lane its outer band contradicts.
    """
    merges = merge_junction_ids(graph)
    return any(edge.source in merges for edge in graph.edges_to(port_id))
