"""Planned crossing columns for multi-source perpendicular entries.

A TOP/BOTTOM entry port on a horizontal-flow (LR/RL) section is crossed at
``port_x + offset`` by every feeder that lands on it and by the drop that leaves
it, and that drop turns once into the section's trunk.  Where two or more
feeders reach the port, nothing upstream fixes which line rides which column
channel, so the column is dealt here from the trunk read back through the turn:
the corner then nests, and every feeder lands on the channel its line departs
from.

A :class:`PerpEntryColumn` is that column.  The station-offset pass writes it
onto the port, and routing re-derives it from the settled offsets so the
crossing X (:func:`perp._perp_entry_crossing_x`) and the up-and-over landing
(:func:`inter_section_handlers._perp_exit_over_geometry`) read the same
channels.  A port whose settled offsets do not carry the dealt column has no
record, so routing applies its own crossing rules there.

The column is only dealt where every feeder already delivers its lines in the
column's side order.  A nested bundle keeps each line on the same side of its
travel through every turn, so a feeder's side order at its departure is the
order it lands in; a column that disagrees could only be honoured by crossing
two of that feeder's lines inside one of its corners, so such a port keeps the
column the other offset passes gave it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from nf_metro.layout.constants import COORD_TOLERANCE, COORD_TOLERANCE_FINE
from nf_metro.layout.geometry import (
    lanes_run_along_x,
    lanes_run_along_y,
    station_lane_coord,
)
from nf_metro.layout.routing.common import (
    EXIT_SIDE_DIRECTION,
    Direction,
    junction_source_ports,
    merge_junction_ids,
    needs_perp_approach_fan,
    offsets_hold,
    perp_entry_consumer,
    resolve_section,
)
from nf_metro.layout.routing.corners import reversed_offset
from nf_metro.layout.routing.orientation import lateral_order_sign
from nf_metro.parser.model import MetroGraph, Port, PortSide

_Offsets = Mapping[tuple[str, str], float]

_LATERAL_TIE_PLACES = 3
"""Decimal places two departure laterals must agree to before they count as a
tie: finer than any offset step, coarse enough to absorb float noise."""


@dataclass(frozen=True, slots=True)
class PerpEntryColumn:
    """The per-line channel offsets a perpendicular entry port is crossed on.

    ``offsets`` is ``None`` where a feeder departs in an order the trunk's
    column disagrees with: the port keeps the offsets and crossing rules the
    rest of the offset and routing passes give it, and no feeder is landed on
    a column for it.
    """

    port_id: str
    offsets: tuple[tuple[str, float], ...] | None

    def offset(self, line_id: str) -> float:
        assert self.offsets is not None
        return next(offset for lid, offset in self.offsets if lid == line_id)


def feeder_sources(graph: MetroGraph, port_id: str) -> set[str]:
    """The distinct stations with an edge into *port_id*."""
    return {edge.source for edge in graph.edges_to(port_id)}


def perp_corridor_feeder(graph: MetroGraph, port_id: str) -> Port | None:
    """The perpendicular exit port feeding entry *port_id* over the corridor.

    A TOP/BOTTOM exit port on a same-row section rises into the inter-row
    corridor band and runs across to feed a perpendicular entry (the
    up-and-over shape); the exit and entry ports then share the corridor Y.
    The drop out of that entry must continue the corridor's per-line descent
    order, so it needs the feeding exit port's side.

    A perpendicular exit that drops across rows into the entry below (the
    LR -> TB top-drop) is *not* collinear with the entry and keeps its own
    drop convention, so it is excluded by the shared-Y test.

    This mirrors the exemption in ``_guard_perp_entry_feed_not_collinear``:
    that guard permits exactly the collinear perp-exit feed this routes.
    """
    entry_y = graph.stations[port_id].y
    for feed in graph.edges_to(port_id):
        port = graph.ports.get(feed.source)
        if (
            port is not None
            and not port.is_entry
            and port.side in (PortSide.TOP, PortSide.BOTTOM)
            and abs(graph.station_for_edge_source(feed).y - entry_y) <= COORD_TOLERANCE
        ):
            return port
    return None


def _trunk_lanes(
    graph: MetroGraph, port_id: str, offsets: _Offsets
) -> dict[str, float] | None:
    """Each line's lane coordinate on the station the port's drop turns into.

    ``None`` unless every line leaves the port for one in-section station.
    """
    from nf_metro.layout.routing.context import _section_lane_frame

    port = graph.ports[port_id]
    frame = _section_lane_frame(graph, graph.section_for_port(port))
    lanes: dict[str, set[float]] = {}
    for edge in graph.edges_from(port_id):
        target = graph.station_for_edge_target(edge)
        if target.is_port:
            return None
        lanes.setdefault(edge.line_id, set()).add(
            station_lane_coord(
                frame, target, offsets.get((edge.target, edge.line_id), 0.0)
            )
        )
    if set(lanes) != set(graph.station_lines(port_id)):
        return None
    if any(len(coords) != 1 for coords in lanes.values()):
        return None
    return {line_id: next(iter(coords)) for line_id, coords in lanes.items()}


def _consumer_is_right_of_port(graph: MetroGraph, port_id: str) -> bool:
    """Whether the station *port_id*'s drop turns into stands at larger X."""
    consumer = perp_entry_consumer(graph, port_id)
    return (
        consumer is not None
        and consumer.x > graph.stations[port_id].x + COORD_TOLERANCE_FINE
    )


def _feeds_on_its_own_lane(graph: MetroGraph, source_id: str) -> bool:
    """Whether *source_id*'s section stacks its lanes along the port's edge."""
    section = resolve_section(graph, graph.stations[source_id], prefer_upstream=True)
    return section is not None and lanes_run_along_x(section.direction)


def _departure_laterals(
    graph: MetroGraph,
    source_id: str,
    line_ids: list[str],
    offsets: _Offsets,
) -> tuple[Direction, dict[str, float]] | None:
    """The run direction and per-line side coordinate *line_ids* leave *source_id* on.

    The same lateral the exit-turn planner ranks a source by: ``y + offset`` on a
    horizontal run, ``x`` plus the riser lateral on a TOP/BOTTOM riser.  ``None``
    where the departure is not one of those shapes.
    """
    from nf_metro.layout.routing.context import _section_lane_frame, port_lane_coord

    source = graph.stations[source_id]
    exit_port = graph.ports.get(source_id)
    if exit_port is None:
        feeding = list(junction_source_ports(graph, source_id))
        if len(feeding) != 1:
            return None
        exit_port = feeding[0]
    direction = EXIT_SIDE_DIRECTION[exit_port.side]
    if exit_port.side in (PortSide.LEFT, PortSide.RIGHT):
        section = graph.section_for_port(exit_port)
        if source.is_port:
            return direction, {
                lid: port_lane_coord(graph, source, lid, offsets) for lid in line_ids
            }
        frame = _section_lane_frame(graph, section)
        return direction, {
            lid: station_lane_coord(frame, source, offsets.get((source_id, lid), 0.0))
            for lid in line_ids
        }
    if not source.is_port:
        return None
    max_offset = max(
        (offsets.get((source_id, lid), 0.0) for lid in graph.station_lines(source_id)),
        default=0.0,
    )

    def riser(line_id: str) -> float:
        offset = offsets.get((source_id, line_id), 0.0)
        if exit_port.side is PortSide.BOTTOM:
            return reversed_offset(offset, max_offset)
        return offset

    return direction, {lid: source.x + riser(lid) for lid in line_ids}


def _feeder_keeps_its_own_crossing(
    graph: MetroGraph, port_id: str, source_id: str
) -> bool:
    """Whether *source_id* reaches *port_id* on geometry a column would not move.

    The column re-lands each feeder's final leg on its channel, but three
    feeders draw more than that leg from the port's own crossing, so a column
    moving a line off it exposes a stub or a jump there:

    * a merge, whose own feeders land on the crossing;
    * a fan-out junction fed by a perpendicular exit on the side opposite the
      port, whose line wraps round to the port along a run its fan places;
    * a perpendicular exit dropping straight onto the port, whose leg out of
      its own section stays on that section's lane.
    """
    port = graph.ports[port_id]
    source = graph.stations[source_id]
    if source_id in graph.ports:
        return abs(source.x - graph.stations[port_id].x) <= COORD_TOLERANCE and {
            edge.target for edge in graph.edges_from(source_id)
        } == {port_id}
    if source_id in merge_junction_ids(graph):
        return True
    opposite = PortSide.BOTTOM if port.side is PortSide.TOP else PortSide.TOP
    return any(
        exit_port.side is opposite
        for exit_port in junction_source_ports(graph, source_id)
    )


def _side_order(laterals: Mapping[str, float], direction: Direction) -> list[str]:
    sign = lateral_order_sign(direction)
    return sorted(laterals, key=lambda lid: laterals[lid] * sign)


def _feeders_deliver_column(
    graph: MetroGraph, port_id: str, column: Mapping[str, float], offsets: _Offsets
) -> bool:
    """Whether every feeder departs with its lines in the column's side order.

    Ties at a departure, or a departure shape with no stated lateral, cannot
    vouch for the order and count as disagreement.
    """
    port = graph.ports[port_id]
    drop = Direction.D if port.side is PortSide.TOP else Direction.U
    lines_by_source: dict[str, list[str]] = {}
    for edge in graph.edges_to(port_id):
        lines_by_source.setdefault(edge.source, []).append(edge.line_id)
    for source_id, line_ids in lines_by_source.items():
        if len(line_ids) < 2:
            continue
        departure = _departure_laterals(graph, source_id, line_ids, offsets)
        if departure is None:
            return False
        direction, laterals = departure
        if len({round(v, _LATERAL_TIE_PLACES) for v in laterals.values()}) != len(
            laterals
        ):
            return False
        landing = {lid: column[lid] for lid in line_ids}
        if _side_order(laterals, direction) != _side_order(landing, drop):
            return False
    return True


def deal_perp_entry_column(
    graph: MetroGraph, port_id: str, offsets: _Offsets
) -> PerpEntryColumn | None:
    """Deal *port_id*'s column from its trunk; ``None`` where none is in scope.

    The drop turns once into the trunk, and the corner nests only when the
    column is the trunk order read through that turn: a TOP drop turning toward
    larger X, or a BOTTOM drop turning toward smaller X, lands its topmost trunk
    lane on the column's high-X channel.  The port's own slots are re-dealt in
    that order, so its spread and its place in any wider bundle stand.

    Scoped to ports two or more feeders reach, none of them the corridor run at
    the port's own Y (:func:`perp_corridor_feeder`) nor a vertical-flow section,
    whose lanes set their crossing themselves.  A feeder drawn from the port's
    own crossing beyond its final leg (:func:`_feeder_keeps_its_own_crossing`)
    declines the record.  A distinct-line approach fan
    (:func:`needs_perp_approach_fan`) channels by approach instead.  The record
    is declined (``offsets`` is ``None``) wherever a feeder would have to cross
    its own lines to land on the column.
    """
    port = graph.ports.get(port_id)
    if (
        port is None
        or not port.is_entry
        or port.side not in (PortSide.TOP, PortSide.BOTTOM)
        or not lanes_run_along_y(graph.section_for_port(port).direction)
        or needs_perp_approach_fan(graph, port_id)
    ):
        return None
    sources = feeder_sources(graph, port_id)
    if len(sources) < 2 or any(_feeds_on_its_own_lane(graph, s) for s in sources):
        return None
    if perp_corridor_feeder(graph, port_id) is not None:
        return None
    if any(_feeder_keeps_its_own_crossing(graph, port_id, s) for s in sources):
        return PerpEntryColumn(port_id, None)
    lines = graph.station_lines(port_id)
    lanes = _trunk_lanes(graph, port_id, offsets)
    if len(lines) < 2 or lanes is None:
        return None
    trunk = sorted(lines, key=lambda lid: (lanes[lid], lid))
    high_x_first = (port.side is PortSide.TOP) == _consumer_is_right_of_port(
        graph, port_id
    )
    order = trunk[::-1] if high_x_first else trunk
    slots = sorted(offsets.get((port_id, lid), 0.0) for lid in order)
    column = dict(zip(order, slots, strict=True))
    if not _feeders_deliver_column(graph, port_id, column, offsets):
        return PerpEntryColumn(port_id, None)
    return PerpEntryColumn(port_id, tuple(column.items()))


def settled_perp_entry_columns(
    graph: MetroGraph, offsets: _Offsets
) -> dict[str, PerpEntryColumn]:
    """Each in-scope port's column as the settled *offsets* carry it.

    A dealt column the port's settled offsets do not hold is declined.
    """
    columns: dict[str, PerpEntryColumn] = {}
    for port_id in graph.ports:
        column = deal_perp_entry_column(graph, port_id, offsets)
        if column is None:
            continue
        if column.offsets is not None and not offsets_hold(
            offsets,
            ((port_id, lid, off) for lid, off in column.offsets),
            COORD_TOLERANCE_FINE,
            missing=0.0,
        ):
            column = PerpEntryColumn(port_id, None)
        columns[port_id] = column
    return columns
