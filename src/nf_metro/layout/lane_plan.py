"""The lane plan record: every planned lane decision, sealed once per layout.

A :class:`LanePlan` is decided at the first offsets run of a layout whose
placement has positioned the junctions (stage 5.1), from structure and that
placement alone, and kept on the graph for the rest of the layout.  A new
layout (each :func:`~nf_metro.layout.engine._layout_once`: the spread loop's
attempts, the row-pitch and strike-clearance relays, the off-track and
geometric-bypass relayouts) starts without one and seals its own; a record is
never carried from one layout's placement to another's.

The record holds:

- the :class:`~nf_metro.layout.port_order_plan.PortOrderPlan`: every port's
  line order and every same-band entry approach;
- each port's *lane sense*: whether its travel-left to travel-right order runs
  up or down the section's lane index.  A port reads its order in its own
  travel, a perpendicular port in the section flow's; a perpendicular entry
  whose lines all turn to one consumer reads it in the travel of that turn;
- each perpendicular entry's *turns*: per in-section consumer, the lines it
  takes and the direction along the section's flow they turn to reach it;
- *far-side approaches*: a connector from a flow-side exit to an off-band
  entry that the same line also reaches by a bypass from the entry's own band
  on the side facing the exit leaves the exit's band on its far side, so one
  line keeps one path per grid row.

Nothing in layout, routing or rendering reads the record.
"""

from __future__ import annotations

from dataclasses import dataclass

from nf_metro.layout.constants import SAME_Y_TOLERANCE
from nf_metro.layout.departure_order import JUNCTIONS_POSITIONED
from nf_metro.layout.geometry import (
    AxisFrame,
    flow_entry_side,
    perpendicular_port_sides,
)
from nf_metro.layout.port_order_plan import (
    ApproachKind,
    PortOrderPlan,
    _Builder,
    _port_travel,
    plan_builder,
)
from nf_metro.layout.routing.common import (
    ENTRY_SIDE_DIRECTION,
    EXIT_SIDE_DIRECTION,
    Direction,
)
from nf_metro.layout.routing.context import _section_lane_frame
from nf_metro.layout.routing.orientation import direction_vector, lateral_order_sign
from nf_metro.parser.model import MetroGraph, Section, Station

_LEVEL_TURN = 1e-6


@dataclass(frozen=True, slots=True)
class ConsumerTurn:
    """The lines a perpendicular entry hands one in-section consumer."""

    consumer_id: str
    lines: tuple[str, ...]
    turn: Direction | None
    """Along the section's flow from the port to the consumer; ``None`` when
    the consumer is level with the port."""


@dataclass(frozen=True)
class LanePlan:
    """One layout's planned lane decisions (see the module docstring)."""

    port_orders: PortOrderPlan
    order_travels: dict[str, Direction]
    """The travel each port's order is read across."""
    lane_senses: dict[str, int]
    """``+1`` when a port's travel-left to travel-right order ascends the
    section's lane index, else ``-1``."""
    perpendicular_turns: dict[str, tuple[ConsumerTurn, ...]]
    far_sides: dict[tuple[str, str, str], int]
    """Keyed ``(entry, exit, line)``: the side, across the exit's travel, the
    connector leaves the exit's band on."""


class _Frames:
    def __init__(self, graph: MetroGraph) -> None:
        self.graph = graph
        self.frames: dict[str, AxisFrame] = {}

    def __call__(self, section_id: str) -> AxisFrame:
        frame = self.frames.get(section_id)
        if frame is None:
            frame = _section_lane_frame(self.graph, self.graph.sections[section_id])
            self.frames[section_id] = frame
        return frame


def _toward(
    section: Section, frame: AxisFrame, a: Station, b: Station
) -> Direction | None:
    """Direction along *section*'s flow axis from *a* to *b*."""
    delta = frame.primary.get(b) - frame.primary.get(a)
    if abs(delta) < _LEVEL_TURN:
        return None
    side = flow_entry_side(section.direction)
    downstream = ENTRY_SIDE_DIRECTION[side]
    return downstream if delta * downstream.sign > 0 else EXIT_SIDE_DIRECTION[side]


def _sense(frame: AxisFrame, travel: Direction) -> int:
    return 1 if frame.secondary_sign * lateral_order_sign(travel) > 0 else -1


def _perpendicular_turns(
    graph: MetroGraph, frames: _Frames
) -> dict[str, tuple[ConsumerTurn, ...]]:
    out: dict[str, tuple[ConsumerTurn, ...]] = {}
    for port_id in sorted(graph.ports):
        port = graph.ports[port_id]
        section = graph.section_for_port(port)
        if not port.is_entry or port.side not in perpendicular_port_sides(
            section.direction
        ):
            continue
        by_consumer: dict[str, set[str]] = {}
        for edge in graph.edges_from(port_id):
            if graph.stations[edge.target].section_id == port.section_id:
                by_consumer.setdefault(edge.target, set()).add(edge.line_id)
        frame = frames(section.id)
        out[port_id] = tuple(
            ConsumerTurn(
                consumer,
                tuple(sorted(lines)),
                _toward(
                    section, frame, graph.stations[port_id], graph.stations[consumer]
                ),
            )
            for consumer, lines in sorted(by_consumer.items())
        )
    return out


def _order_travels(
    graph: MetroGraph, turns: dict[str, tuple[ConsumerTurn, ...]]
) -> dict[str, Direction]:
    out: dict[str, Direction] = {}
    for port_id in sorted(graph.ports):
        port = graph.ports[port_id]
        section = graph.section_for_port(port)
        consumers = turns.get(port_id, ())
        turn = consumers[0].turn if len(consumers) == 1 else None
        if turn is not None:
            out[port_id] = turn
        elif port.side in perpendicular_port_sides(section.direction):
            out[port_id] = ENTRY_SIDE_DIRECTION[flow_entry_side(section.direction)]
        else:
            out[port_id] = _port_travel(port)
    return out


def _consumers(builder: _Builder, exit_id: str, line_id: str) -> list[str]:
    """The non-junction nodes *line_id* reaches from *exit_id*."""
    graph = builder.graph
    found: list[str] = []
    seen: set[str] = set()
    frontier = [e.target for e in graph.edges_from(exit_id) if e.line_id == line_id]
    while frontier:
        node = frontier.pop()
        if node in seen:
            continue
        seen.add(node)
        if node in builder.junctions:
            frontier.extend(
                e.target for e in graph.edges_from(node) if e.line_id == line_id
            )
        else:
            found.append(node)
    return sorted(found)


def _far_side(
    builder: _Builder, exit_id: str, entry_id: str, line_id: str, lateral: float
) -> bool:
    graph = builder.graph
    travel = EXIT_SIDE_DIRECTION[graph.ports[exit_id].side]
    entry_travel = _port_travel(graph.ports[entry_id])
    ex, ey = direction_vector(travel)
    nx, ny = direction_vector(entry_travel)
    dot = ex * nx + ey * ny
    if dot == 0:
        return False
    for source, _via in builder.upstream_sources(entry_id, line_id):
        if (
            source == exit_id
            or builder.cell_centre(source) is None
            or abs(builder.cell_lateral(source, entry_id, entry_travel))
            > SAME_Y_TOLERANCE
        ):
            continue
        approach = builder.approach(entry_id, source)
        if (
            approach.kind is ApproachKind.BYPASS
            and approach.lateral * dot * lateral < 0
        ):
            return True
    return False


def _far_sides(builder: _Builder) -> dict[tuple[str, str, str], int]:
    graph = builder.graph
    out: dict[tuple[str, str, str], int] = {}
    for exit_id in sorted(graph.ports):
        port = graph.ports[exit_id]
        section = graph.section_for_port(port)
        if port.is_entry or port.side in perpendicular_port_sides(section.direction):
            continue
        if builder.cell_centre(exit_id) is None:
            continue
        travel = EXIT_SIDE_DIRECTION[port.side]
        for line_id in graph.station_lines(exit_id):
            for entry_id in _consumers(builder, exit_id, line_id):
                entry = graph.ports.get(entry_id)
                if (
                    entry is None
                    or not entry.is_entry
                    or builder.cell_centre(entry_id) is None
                    or entry.side
                    in perpendicular_port_sides(graph.section_for_port(entry).direction)
                ):
                    continue
                lateral = builder.cell_lateral(entry_id, exit_id, travel)
                if abs(lateral) <= SAME_Y_TOLERANCE:
                    continue
                if _far_side(builder, exit_id, entry_id, line_id, lateral):
                    out[(entry_id, exit_id, line_id)] = -1 if lateral > 0 else 1
    return out


def build_lane_plan(graph: MetroGraph) -> LanePlan:
    """Decide *graph*'s lane plan from its structure and placement."""
    builder, port_orders = plan_builder(graph)
    frames = _Frames(graph)
    turns = _perpendicular_turns(graph, frames)
    travels = _order_travels(graph, turns)
    return LanePlan(
        port_orders=port_orders,
        order_travels=travels,
        lane_senses={
            port_id: _sense(frames(graph.ports[port_id].section_id), travel)
            for port_id, travel in travels.items()
        },
        perpendicular_turns=turns,
        far_sides=_far_sides(builder),
    )


def start_layout(graph: MetroGraph) -> None:
    """Forget the previous layout's record: this layout seals its own."""
    graph._lane_plan = None


def seal_lane_plan(graph: MetroGraph) -> None:
    """Seal the layout's record at its first offsets run at or after 5.1."""
    if graph._lane_plan is None and JUNCTIONS_POSITIONED in graph._stages_completed:
        graph._lane_plan = build_lane_plan(graph)
