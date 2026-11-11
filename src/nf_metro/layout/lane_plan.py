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
  line keeps one path per grid row;
- the :class:`TrackPlan`: every in-section ``(node, line)``'s lane, from which
  every exit's order is read;
- the :class:`SeamJoint`: the lanes either side of every straight seam.

**The track plan.**  Sections are decided upstream first: a section's lanes
need its entries' orders, and an exit's order is its lines' lanes at the port
read across its travel.

**Runs.**  A section's nodes joined by edges whose ends share the lane
coordinate form level runs.  A run the flow leaves and re-enters is split
at each re-entry node (a run node fed from outside the run by a node the
run reaches, a bypass-V helper standing for the edge it splits): a node's
part is the number of re-entry nodes it is, or lies downstream of.  Runs
are decided in the order of the section's run graph, the nearest the flow
start first among runs ready together or on a cycle.

**Seams.**  A line reaching a flow-side entry through junctions only from a
facing flow-side exit, the two ports on one lane coordinate with their grid
cells level in one band and no section between, crosses a *straight seam*:
it is pinned to the entry lane on its exit lane's coordinate, below lane zero
too, when that lands on the entry's lane grid and its other straight seams
agree.  A pin on a line another feeding bundle also carries holds only where
every such bundle carries its pinned lines in its own lane order.  The
entry's other lines fill round the pins in its order (by line id where that
order is undecided): those before the first pinned line take the free lanes
below it, nearest first, each later one the first free lane past the line
before it, so a line ordered between two adjacent pins goes past the later.
The entry's order is those lanes read across its travel.  A perpendicular
port every line of which crosses a straight seam, past no section box, to a
facing flow-side port centred on it records each line's offset from the port
on the flow-side port's lane, across the seam's travel.

**Section order.**  The merge of every entry's order read up the lane
index, line priority deciding between lines no entry orders.  A line on no
entry of the section is *born* in it.  A born line that leaves a flow-side
exit, on whose level run it is born, beside lines that entered the section
moves past each of them whose nearest consumer lies, across the exit's
travel, beyond its own: to the end of the order when that holds of all of
them on one side, else just past the last it lies beyond.  A consumer's side
is its grid cell's lateral from the exit (reversed for a far-side approach);
one on the exit's band takes the approach the exit records to it, or the
entry's own when it states a side across a parallel travel.  Where no
consumer separates them, the born line lies on the side it joins its
consumer entries from (level from the exit, else each other source's side in
the entry's travel), when all its sides lie at or past all of theirs and
differ.  The born lines of an exit opening one divergence fan take the fan's
peel order, read in the section flow's frame; two or more other born lines
leaving one exit take the order of the sides they head for where their
bundle first parts, followed through sections each of them enters and leaves
by single ports, unless one line forking right past that exit has sides
straddling another's.  A line's *section slot* is one past the highest slot
of an earlier line it coexists with in the section.

**Run order.**  The run's lines sort by their arrival key: zero for a line
riding an entry of the run or fed by nothing off it, else the lane-frame
lateral (to 0.1, nearest first, node id breaking ties) of the nearest node
off the run feeding its first node in its own travel along the run (a node
of another part of the same level run, read through bypass-V helpers, is not
off it), or for a line nothing feeds there, of the nearest consumer its last
node leaves to.  A line a perpendicular entry of the section turns into the
run upstream of its first node keeps a near-zero key on its port's side.
Lines arriving from one side at one lateral nest by where they leave their
sources: the farther upstream, the farther from that side.  A line that parts
from the run to the side opposite the one it arrived from, where another of
its lines carries on, ranks by the side it leaves to, outside the lines
arriving there.  Ties fall to the source's lane, then the section slot.  A
later part keeps every bundle entering it from an earlier part in that part's
lane order.  A run moves its born lines and applies its section's peel and
parting orders as the section order does.  An exit carries, on lines it
shares with an upstream exit it descends from, that exit's order where both
reach one entry.

**Lanes.**  A fan plan's carriers keep their slot; a line riding a level edge
into a part from an earlier part of its run keeps that lane, and a line
riding a seam-pinned entry of the run its entry lane, unless a carrier's slot
differs (pins).  The other lines take lanes in run order: one past the highest
lane of an earlier line they coexist with; with none, the lane of an earlier
line they hand over to at a shared station (on a side, the lowest or highest
such lane, else the latest-ordered), else the run's floor (the lowest section
slot among the run's lines; in a pinned run, the line's own slot).  A pinned
run's floor drops below zero as far as a pin needs to fit the free lines
ordered before it; a hand-over lane below that floor is not taken.  A free
line in a pinned run stays below the next pinned line it coexists with, a
lane left for each free line between.  Where bundles enter a pinned part, a
pin ordered after a bundle-mate pinned no lower is dropped, and each free line
takes its section slot among the part's lines, lifted past its bundle-mates
laned before it and the free line before it, clamped below its pinned
bundle-mates after it.  A free line never takes a lane a line it coexists with
holds, nor one beside a line that runs from the run's entry port past one of
its stations where the other stops (a *breeze*): it steps up past both.

**Lines in a seam-pinned run.**  Outside a part entered by bundles, a line
riding no entry of a run holding a seam-pinned entry lies beside the fixed
lines it is born among that carry on past its first station: on the side,
in lanes, it heads for, where all of them head for the other (where it
leaves the run's coordinate; at a flow-side exit, its nearest consumer's side,
a consumer level with the exit no side).  Where it or one of them forks
right past an exit, within the exit's first grid step, it heads for every
branch's side and lies past them only when all its sides lie at or past all
of each one's and differ.  With no side, a line joining the run from an
in-section source on one side takes that side of the fixed lines at the
station it joins.  It then takes the first lane, past those lines on that
side (below them, a lane left for each later line placed below the same
lines), else up from the lowest fixed lane, that no line it coexists with
holds, stepping away from a breeze to the first free lane clear of it.

Two lines *coexist* on a run when the flow spans of their connected pieces
overlap; touching at one coordinate is a hand-over when one terminates
there arriving from upstream and the other is born there leaving
downstream.

Nothing in layout, routing or rendering reads the record.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from heapq import heapify, heappop, heappush
from typing import NamedTuple

from nf_metro.layout.constants import (
    SAME_COORD_TOLERANCE,
    SAME_Y_TOLERANCE,
    graph_offset_step,
)
from nf_metro.layout.departure_order import JUNCTIONS_POSITIONED
from nf_metro.layout.fan_ordering import fanout_divergence_peel_order
from nf_metro.layout.geometry import (
    AxisFrame,
    flow_entry_side,
    perpendicular_port_sides,
)
from nf_metro.layout.port_order_plan import (
    ApproachKind,
    PortOrderBuilder,
    PortOrderPlan,
    decide_ports,
    port_travel,
)
from nf_metro.layout.routing.common import (
    ENTRY_SIDE_DIRECTION,
    EXIT_SIDE_DIRECTION,
    Direction,
)
from nf_metro.layout.routing.context import _section_lane_frame
from nf_metro.layout.routing.orientation import direction_vector, lateral_order_sign
from nf_metro.parser.model import MetroGraph, Section, Station, is_bypass_v

_LEVEL_TURN = 1e-6


@dataclass(frozen=True, slots=True)
class ConsumerTurn:
    """The lines a perpendicular entry hands one in-section consumer."""

    consumer_id: str
    lines: tuple[str, ...]
    turn: Direction | None
    """Along the section's flow from the port to the consumer; ``None`` when
    the consumer is level with the port."""


@dataclass(frozen=True, slots=True)
class ForcedCrossing:
    """Lines whose bundles a section or a run cannot all keep in order: the
    bundles' orders form a cycle, broken by the merge's tie-break."""

    section_id: str
    run: str | None
    """The level run the crossing lies in; ``None`` for the section order."""
    lines: tuple[str, ...]


@dataclass(frozen=True)
class TrackPlan:
    """Every in-section ``(node, line)``'s planned lane (see the module
    docstring)."""

    lanes: dict[tuple[str, str], int]
    """Lane index in the node's section lane frame: lane ``k`` lies ``k``
    offset steps along the frame's secondary sign from the node.  A junction
    takes the lane its feeding exit gives the line."""
    forced: tuple[ForcedCrossing, ...]
    cycle_sections: tuple[str, ...]
    """Sections reached again while their own lanes were being decided; an
    exit of such a section asked for round the cycle is undecided."""


@dataclass(frozen=True)
class SeamJoint:
    """The lanes either side of every straight seam (see the module
    docstring)."""

    entries: dict[str, dict[str, int]]
    """Per flow-side entry some line crosses a straight seam into: every
    line's lane at the entry by its pins and the fill round them, in its
    section lane frame.  The entry's seam order is read from these lanes.
    The track plan gives the entry these lanes, except a line's whose lane in
    the entry's run a fan plan's carrier slot, or the lane the line keeps
    from an earlier part of its level run, fixes otherwise."""
    pins: dict[tuple[str, str], str]
    """Keyed ``(entry, line)``: the exit a pinned line crosses straight from."""
    perpendicular: dict[str, dict[str, int]]
    """Per perpendicular port every line of which crosses a straight seam to a
    facing flow-side port: each line's offset from the port, in offset steps
    to the right of the seam's travel, that keeps it straight."""


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
    tracks: TrackPlan
    seams: SeamJoint


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
            out[port_id] = port_travel(port)
    return out


def _consumers(builder: PortOrderBuilder, exit_id: str, line_id: str) -> list[str]:
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
    builder: PortOrderBuilder, exit_id: str, entry_id: str, line_id: str, lateral: float
) -> bool:
    graph = builder.graph
    travel = EXIT_SIDE_DIRECTION[graph.ports[exit_id].side]
    entry_travel = port_travel(graph.ports[entry_id])
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


def _far_sides(builder: PortOrderBuilder) -> dict[tuple[str, str, str], int]:
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


_Spans = dict[str, list[tuple[float, float]]]


_Conflict = Callable[[str, str], bool]
_TURNED_IN = 1e-4
_SIDED = 0.05
_OUTSIDE = 0.01
_ON_GRID = 0.05
_PARTING_SEAMS = 8


class _Arrival(NamedTuple):
    key: float
    neighbour: int | None
    nest: float | None
    joins: str | None
    """The run station a line from an in-section source joins at."""


class _Reach(NamedTuple):
    """Where a line leaving an exit heads for one consumer."""

    lateral: float
    """Across the exit's travel, positive to the traveller's right."""
    ahead: float | None
    """Grid distance along the travel to an off-band consumer's cell."""


_Mates = tuple[str, ...]


@dataclass
class _Section:
    """One section's nodes, in-section edges and level runs."""

    id: str
    nodes: list[str]
    ins: dict[tuple[str, str], list[str]]
    outs: dict[tuple[str, str], list[str]]
    runs: dict[str, list[str]]
    run_of: dict[str, str]
    level_run_of: dict[str, str]
    """The unsplit level run each node lies in."""
    later_parts: set[str]


@dataclass
class _Run:
    """One level run (or part of one) of a section."""

    sec: _Section
    key: str
    members: list[str]
    member_set: set[str]
    present: list[str]
    lateral: float
    level_run: str
    passing: list[tuple[str, float, float, str, str]] = field(default_factory=list)
    """``(line, from, to, source, target)``: each edge from an entry port of
    the run to another of its nodes, with its flow span."""


class _Tracks:
    """Decides every section's lanes (the module docstring's track plan),
    upstream sections first.
    """

    def __init__(
        self, graph: MetroGraph, frames: _Frames, senses: Mapping[str, int]
    ) -> None:
        self.graph = graph
        self.frames = frames
        self.senses = senses
        self.step = graph_offset_step(graph)
        self.builder = PortOrderBuilder(graph, self.exit_order, self.seam_order)
        self.lanes: dict[tuple[str, str], int] = {}
        self.seam_lanes: dict[str, dict[str, int]] = {}
        self.seam_sources: dict[tuple[str, str], str] = {}
        self.done: set[str] = set()
        self.busy: set[str] = set()
        self.cycles: set[str] = set()
        self.forced: list[ForcedCrossing] = []
        self.descendants: dict[str, set[str]] = {}
        self.fan_slots: dict[tuple[str, str], int] = {}
        for plan in graph.fan_plans:
            if plan.owns_geometry:
                for carrier in plan.offset_carriers:
                    for slot in carrier.assignments:
                        self.fan_slots[(carrier.station_id, slot.line_id)] = slot.slot

    def lateral(self, node: str, section_id: str) -> float:
        return self.frames(section_id).secondary.get(self.graph.stations[node])

    def perpendicular(self, port_id: str) -> bool:
        port = self.graph.ports[port_id]
        return port.side in perpendicular_port_sides(
            self.graph.section_for_port(port).direction
        )

    def along(self, node: str, section_id: str) -> float:
        frame = self.frames(section_id)
        return frame.primary_sign * frame.primary.get(self.graph.stations[node])

    def plan(self) -> TrackPlan:
        graph = self.graph
        for section_id in sorted(graph.sections):
            self.section(section_id)
        lanes = dict(self.lanes)
        for junction in sorted(graph.junction_ids):
            for edge in graph.edges_to(junction):
                key = (edge.source, edge.line_id)
                if key in self.lanes:
                    lanes[(junction, edge.line_id)] = self.lanes[key]
        return TrackPlan(lanes, tuple(self.forced), tuple(sorted(self.cycles)))

    def exit_order(self, port_id: str) -> tuple[str, ...] | None:
        """The exit's lines by lane, read across its travel."""
        lines = self.graph.station_lines(port_id)
        if not self.section(self.graph.ports[port_id].section_id) or any(
            (port_id, lid) not in self.lanes for lid in lines
        ):
            return None
        lines = sorted(lines, key=lambda lid: (self.lanes[(port_id, lid)], lid))
        return tuple(lines if self.senses[port_id] > 0 else reversed(lines))

    # ---- straight seams

    def seam_order(
        self, entry_id: str, order: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        """The entry's lines by the lanes its straight seams pin, read across
        its travel; ``None`` when no line is pinned."""
        graph = self.graph
        entry = graph.ports[entry_id]
        if entry_id not in graph.sections[
            entry.section_id
        ].entry_ports or self.perpendicular(entry_id):
            return None
        pins = self._pins(entry_id)
        if not pins:
            return None
        lines = graph.station_lines(entry_id)
        ascending = list(order) if order is not None else sorted(lines)
        if self.senses[entry_id] < 0:
            ascending.reverse()
        lanes = _fill_round_pins(ascending, {lid: k for lid, (k, _x) in pins.items()})
        self.seam_lanes[entry_id] = lanes
        for lid, (_lane, exit_id) in pins.items():
            self.seam_sources[(entry_id, lid)] = exit_id
        by_lane = sorted(lines, key=lambda lid: (lanes[lid], lid))
        return tuple(by_lane if self.senses[entry_id] > 0 else reversed(by_lane))

    def _pins(self, entry_id: str) -> dict[str, tuple[int, str]]:
        """Per line crossing a straight seam into *entry_id*, the lane that
        keeps it on its exit's coordinate, and that exit."""
        graph, builder = self.graph, self.builder
        pins: dict[str, tuple[int, str]] = {}
        for lid in graph.station_lines(entry_id):
            found = set()
            for exit_id, _via in builder.upstream_sources(entry_id, lid):
                lane = self._straight_lane(entry_id, exit_id, lid)
                if lane is not None:
                    found.add((lane, exit_id))
            if len({lane for lane, _x in found}) == 1:
                pins[lid] = min(found)
        if not pins:
            return pins
        feeding: dict[str, list[str]] = {}
        for lid in graph.station_lines(entry_id):
            for node, _via in builder.upstream_sources(entry_id, lid):
                feeding.setdefault(node, []).append(lid)
        for lid in sorted(pins):
            bundles = [
                (node, carried)
                for node, carried in feeding.items()
                if len(carried) >= 2 and lid in carried and node != pins[lid][1]
            ]
            if bundles and not self._pin_fits_bundles(pins, bundles):
                del pins[lid]
        return pins

    def _straight_lane(self, entry_id: str, exit_id: str, line_id: str) -> int | None:
        """The entry lane on *exit_id*'s coordinate for *line_id*, when the
        exit faces the entry across a level seam on one lane coordinate."""
        graph, builder = self.graph, self.builder
        exit_port = graph.ports.get(exit_id)
        entry = graph.ports[entry_id]
        if (
            exit_port is None
            or exit_port.is_entry
            or self.perpendicular(exit_id)
            or EXIT_SIDE_DIRECTION[exit_port.side] is not port_travel(entry)
        ):
            return None
        frame, exit_frame = (
            self.frames(entry.section_id),
            self.frames(exit_port.section_id),
        )
        if exit_frame.secondary.name != frame.secondary.name:
            return None
        here = self.lateral(entry_id, entry.section_id)
        there = self.lateral(exit_id, exit_port.section_id)
        if (
            abs(there - here) > SAME_Y_TOLERANCE
            or builder.cell_centre(entry_id) is None
            or builder.cell_centre(exit_id) is None
            or abs(builder.cell_lateral(exit_id, entry_id, port_travel(entry)))
            > SAME_Y_TOLERANCE
            or builder.approach(entry_id, exit_id).kind is not ApproachKind.LEVEL
        ):
            return None
        builder.order_of(exit_id)
        lane = self.lanes.get((exit_id, line_id))
        if lane is None:
            return None
        drawn = there + exit_frame.secondary_sign * lane * self.step
        return _on_grid((drawn - here) / (frame.secondary_sign * self.step))

    def _pin_fits_bundles(
        self, pins: Mapping[str, tuple[int, str]], bundles: list[tuple[str, list[str]]]
    ) -> bool:
        """Whether every feeding bundle holds its pinned lines in its own
        lane order, so its other lines can take that order round the pins."""
        for node, carried in bundles:
            if node in self.graph.ports:
                self.builder.order_of(node)
            if any((node, lid) not in self.lanes for lid in carried):
                return False
            pinned = sorted(
                (lid for lid in carried if lid in pins),
                key=lambda lid: self.lanes[(node, lid)],
            )
            if [pins[lid][0] for lid in pinned] != sorted(
                pins[lid][0] for lid in pinned
            ):
                return False
        return True

    # ---- born lines

    def _born_order(
        self, sec: _Section, order: Sequence[str], born: set[str]
    ) -> list[str]:
        """*order* with each born line moved past the lines it leaves an exit
        with to the side its consumer lies, or joins its consumer from."""
        graph = self.graph
        out = list(order)
        exits = [
            x
            for x in sorted(graph.sections[sec.id].exit_ports)
            if x in graph.ports and not self.perpendicular(x)
        ]
        for lid in sorted(born):
            for exit_id in exits:
                if lid not in graph.station_lines(exit_id) or not self._born_beside(
                    sec, exit_id, lid
                ):
                    continue
                kept = [
                    m
                    for m in graph.station_lines(exit_id)
                    if m != lid and m not in born and m in out
                ]
                if not kept:
                    continue
                nearest = {m: self._nearest(exit_id, m) for m in (lid, *kept)}
                laterals = {m: v for m, v in nearest.items() if v is not None}
                if len(laterals) < len(nearest):
                    continue
                mine = laterals.pop(lid)
                sense = self.senses[exit_id]
                higher = [
                    m for m in kept if sense * (laterals[m] - mine) > SAME_Y_TOLERANCE
                ]
                lower = [
                    m for m in kept if sense * (mine - laterals[m]) > SAME_Y_TOLERANCE
                ]
                if not higher and not lower:
                    side = sense * self._joined_side(exit_id, lid, kept)
                    higher, lower = (kept, []) if side < 0 else ([], kept)
                    if side == 0:
                        continue
                out.remove(lid)
                if not lower:
                    out.insert(0, lid)
                elif not higher:
                    out.append(lid)
                else:
                    out.insert(max(out.index(m) for m in lower) + 1, lid)
                break
        return out

    def _born_beside(self, sec: _Section, exit_id: str, lid: str) -> bool:
        """Whether *lid* is born on the run of *exit_id*."""
        graph = self.graph
        return any(
            sec.run_of[n] == sec.run_of[exit_id]
            and lid in graph.station_lines(n)
            and not any(
                e.line_id == lid and graph.stations[e.source].section_id == sec.id
                for e in graph.edges_to(n)
            )
            for n in sec.nodes
        )

    def _joined_side(self, exit_id: str, lid: str, kept: Sequence[str]) -> int:
        """The side, across *exit_id*'s travel, *lid* takes of *kept* by the
        sides each also joins its consumers from; ``0`` when that gives none."""
        mine = self._join_sides(exit_id, lid)
        theirs = [self._join_sides(exit_id, m) for m in kept]
        known = [t for t in theirs if t is not None]
        if mine is None or len(known) < len(theirs):
            return 0
        if len(mine) < 2 and all(len(t) < 2 for t in known):
            return 0
        if all(min(mine) >= max(t) and mine != t for t in known):
            return 1
        if all(max(mine) <= min(t) and mine != t for t in known):
            return -1
        return 0

    def _join_sides(self, exit_id: str, lid: str) -> set[int] | None:
        """The sides, across *exit_id*'s travel, *lid* joins its consumer
        entries from: level for the connector from *exit_id*, and each other
        source's side in the entry's travel (its cell, else its approach)."""
        graph, builder = self.graph, self.builder
        sides = {0}
        for node in _consumers(builder, exit_id, lid):
            port = graph.ports.get(node)
            if port is None or not port.is_entry or self.perpendicular(node):
                continue
            if builder.cell_centre(node) is None:
                return None
            travel = port_travel(port)
            for source, _via in builder.upstream_sources(node, lid):
                if source == exit_id or builder.cell_centre(source) is None:
                    continue
                lateral = builder.cell_lateral(source, node, travel)
                if abs(lateral) <= SAME_Y_TOLERANCE:
                    lateral = builder.approach(node, source).lateral
                sides.add(_sign(lateral))
        return sides

    def _downstream(self, exit_id: str, lid: str) -> list[_Reach] | None:
        """Where *lid* heads for past *exit_id*, per consumer: an off-band
        consumer's cell lateral (a far-side approach reversing it), else the
        approach record read from the exit, or from the consumer entry when it
        states a side across a parallel travel."""
        graph, builder = self.graph, self.builder
        here = builder.cell_centre(exit_id)
        if here is None:
            return None
        travel = EXIT_SIDE_DIRECTION[graph.ports[exit_id].side]
        out = []
        for node in _consumers(builder, exit_id, lid):
            cell = builder.cell_centre(node)
            if cell is None:
                continue
            port = graph.ports.get(node)
            entry = port is not None and port.is_entry and not self.perpendicular(node)
            lateral = builder.cell_lateral(node, exit_id, travel)
            if abs(lateral) > SAME_Y_TOLERANCE:
                if entry and _far_side(builder, exit_id, node, lid, lateral):
                    lateral = -lateral
                out.append(_Reach(lateral, _ahead(cell, here, travel)))
                continue
            lateral = -builder.approach(exit_id, node).lateral
            if entry:
                assert port is not None
                own = builder.approach(node, exit_id)
                parallel = _dot(port_travel(port), travel)
                if own.kind is not ApproachKind.LEVEL and abs(parallel) > 0.5:
                    lateral = own.lateral * parallel
            out.append(_Reach(lateral, None))
        return out or None

    def _nearest(self, exit_id: str, lid: str) -> float | None:
        reaches = self._downstream(exit_id, lid)
        if reaches is None:
            return None
        return min((r.lateral for r in reaches), key=abs)

    def _peel_chains(self, section_id: str, born: set[str]) -> list[list[str]]:
        """Per flow-side exit opening one divergence fan, its born lines in
        the fan's peel order, ascending the section's lanes."""
        graph = self.graph
        section = graph.sections[section_id]
        out = []
        for exit_id in sorted(section.exit_ports):
            if exit_id not in graph.ports or self.perpendicular(exit_id):
                continue
            junctions = sorted(
                {
                    e.target
                    for e in graph.edges_from(exit_id)
                    if e.target in graph.junction_ids
                }
            )
            if len(junctions) != 1:
                continue
            peel = fanout_divergence_peel_order(
                graph, junctions[0], self.builder.priority, None, section.direction
            )
            if not peel or set(peel) != set(graph.station_lines(exit_id)):
                continue
            chain = [lid for lid in peel if lid in born]
            if len(chain) >= 2:
                sign = self.frames(section_id).secondary_sign
                out.append(chain if sign > 0 else chain[::-1])
        return out

    def _forward_chains(
        self, section_id: str, born: set[str], order: Sequence[str]
    ) -> list[list[str]]:
        """Per flow-side exit carrying two or more born lines, those lines in
        the order of the sides they head for where their bundle parts."""
        graph = self.graph
        out = []
        for exit_id in sorted(graph.sections[section_id].exit_ports):
            if exit_id not in graph.ports or self.perpendicular(exit_id):
                continue
            group = sorted(lid for lid in graph.station_lines(exit_id) if lid in born)
            if len(group) < 2:
                continue
            laterals = self._parting(exit_id, group)
            if laterals is None or len({round(v, 3) for v in laterals.values()}) < 2:
                continue
            sense = self.senses[exit_id]
            position = {
                lid: order.index(lid) if lid in order else len(order) for lid in group
            }
            out.append(
                sorted(
                    group,
                    key=lambda lid: (round(sense * laterals[lid], 3), position[lid]),
                )
            )
        return out

    def _parting(self, exit_id: str, group: Sequence[str]) -> dict[str, float] | None:
        """Each line's lateral, across the travel of the exit where the bundle
        leaving *exit_id* first parts, of the side it heads for there;
        ``None`` where the bundle parts inside a section or the sides of a
        line forking right past that exit straddle another's."""
        graph = self.graph
        for _ in range(_PARTING_SEAMS):
            reached = {
                lid: set(_consumers(self.builder, exit_id, lid)) for lid in group
            }
            common = set.intersection(*reached.values())
            if len(common) == 1 and all(len(r) == 1 for r in reached.values()):
                (entry_id,) = common
                port = graph.ports.get(entry_id)
                if port is None or not port.is_entry:
                    return None
                exits = [_exits_reached(graph, entry_id, lid) for lid in group]
                if any(len(x) != 1 for x in exits) or len(set.union(*exits)) != 1:
                    return None
                (exit_id,) = exits[0]
                continue
            reaches = {lid: self._downstream(exit_id, lid) for lid in group}
            sides: dict[str, list[float]] = {}
            for lid, found in reaches.items():
                if found is None:
                    return None
                sides[lid] = [r.lateral for r in found]
            if any(len(v) > 1 for v in sides.values()) and self._near_fork(
                exit_id, group
            ):
                ordered = sorted(group)
                for i, a in enumerate(ordered):
                    for b in ordered[i + 1 :]:
                        if not (
                            max(sides[a]) <= min(sides[b]) + SAME_Y_TOLERANCE
                            or max(sides[b]) <= min(sides[a]) + SAME_Y_TOLERANCE
                        ):
                            return None
            return {lid: min(v, key=abs) for lid, v in sides.items()}
        return None

    def _perpendicular_seams(self) -> dict[str, dict[str, int]]:
        """Per perpendicular port every line of which crosses a straight seam
        to a facing flow-side port centred on it, each line's offset that
        keeps it straight (see :class:`SeamJoint`)."""
        graph = self.graph
        out: dict[str, dict[str, int]] = {}
        for port_id in sorted(graph.ports):
            lines = graph.station_lines(port_id)
            if not self.perpendicular(port_id) or any(
                (port_id, lid) not in self.lanes for lid in lines
            ):
                continue
            straight: dict[str, int] = {}
            clash = False
            for lid in lines:
                found = {
                    k
                    for flow_end in self._facing_ends(port_id, lid)
                    if (k := self._seam_offset(port_id, flow_end, lid)) is not None
                }
                if len(found) == 1:
                    straight[lid] = found.pop()
                clash |= len(found) > 1
            if (
                not clash
                and len(straight) == len(lines)
                and len(set(straight.values())) == len(lines)
            ):
                out[port_id] = straight
        return out

    def _facing_ends(self, port_id: str, lid: str) -> list[str]:
        """The flow-side ports facing perpendicular *port_id* across a seam on
        *lid*, travelling its way."""
        graph = self.graph
        port = graph.ports[port_id]
        if port.is_entry:
            ends = [x for x, _via in self.builder.upstream_sources(port_id, lid)]
        else:
            ends = sorted(_consumers(self.builder, port_id, lid))
        travel = port_travel(port)
        return [
            end
            for end in ends
            if end in graph.ports
            and graph.ports[end].is_entry != port.is_entry
            and not self.perpendicular(end)
            and port_travel(graph.ports[end]) is travel
        ]

    def _seam_offset(self, port_id: str, flow_end: str, lid: str) -> int | None:
        """*lid*'s offset from *port_id* on *flow_end*'s lane, when the two
        face each other on one lane coordinate past no section box."""
        graph = self.graph
        frame = self.frames(graph.stations[flow_end].section_id or "")
        end_at = frame.secondary.get(graph.stations[flow_end])
        port_at = frame.secondary.get(graph.stations[port_id])
        lane = self.lanes.get((flow_end, lid))
        if (
            lane is None
            or abs(end_at - port_at) > SAME_COORD_TOLERANCE
            or _boxes_across(graph, port_id, flow_end, end_at, frame.secondary.name)
        ):
            return None
        drawn = end_at + frame.secondary_sign * lane * self.step
        travel = port_travel(graph.ports[port_id])
        return _on_grid(lateral_order_sign(travel) * (drawn - port_at) / self.step)

    # ---- one section

    def section(self, section_id: str) -> bool:
        """Decide *section_id*'s lanes; ``False`` when reached round a cycle."""
        if section_id in self.done:
            return True
        if section_id in self.busy:
            self.cycles.add(section_id)
            return False
        self.busy.add(section_id)
        graph = self.graph
        entry_orders: dict[str, list[str]] = {}
        for port_id in graph.sections[section_id].entry_ports:
            if port_id not in graph.ports:
                continue
            held = self.builder.order_of(port_id)
            if held is not None and held.lines is not None and len(held.lines) >= 2:
                lines = list(held.lines)
                entry_orders[port_id] = (
                    lines if self.senses[port_id] > 0 else lines[::-1]
                )
        sec = self._runs(section_id)
        section_lines = sorted(
            {lid for n in sec.nodes for lid in graph.station_lines(n)}
        )
        order = self._merge(section_lines, list(entry_orders.values()), section_id)
        entered = {
            lid
            for port_id in graph.sections[section_id].entry_ports
            if port_id in graph.ports
            for lid in graph.station_lines(port_id)
        }
        born = {lid for lid in section_lines if lid not in entered}
        born_chains: list[list[str]] = []
        if born:
            order = self._born_order(sec, order, born)
            born_chains = self._peel_chains(section_id, born)
            peeled = {lid for chain in born_chains for lid in chain}
            born_chains += [
                chain
                for chain in self._forward_chains(section_id, born, order)
                if not set(chain) & peeled
            ]
        for chain in born_chains:
            order = _apply_chain(order, chain)
        conflict = partial(
            self._conflict,
            spans=self._spans(section_id, sec.nodes, section_lines),
            section_id=section_id,
            nodes=sec.nodes,
        )
        slots: dict[str, int] = {}
        for i, lid in enumerate(order):
            slots[lid] = 1 + max(
                (slots[m] for m in order[:i] if conflict(lid, m)), default=-1
            )
        carried = self._carried_orders(section_id)
        for run in self._run_order(sec):
            self._place_run(
                sec, run, entry_orders, order, slots, carried, born, born_chains
            )
        self.busy.discard(section_id)
        self.done.add(section_id)
        return True

    def _runs(self, section_id: str) -> _Section:
        graph = self.graph
        nodes = [n for n, st in graph.stations.items() if st.section_id == section_id]
        node_set = set(nodes)
        parent = {n: n for n in nodes}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        ins: dict[tuple[str, str], list[str]] = {}
        outs: dict[tuple[str, str], list[str]] = {}
        for e in graph.edges:
            if e.source in node_set and e.target in node_set:
                outs.setdefault((e.source, e.line_id), []).append(e.target)
                ins.setdefault((e.target, e.line_id), []).append(e.source)
                if (
                    abs(
                        self.lateral(e.source, section_id)
                        - self.lateral(e.target, section_id)
                    )
                    <= SAME_Y_TOLERANCE
                ):
                    parent[find(e.source)] = find(e.target)
        level: dict[str, list[str]] = {}
        for n in nodes:
            level.setdefault(find(n), []).append(n)
        level_run_of = {n: find(n) for n in nodes}
        succ: dict[str, set[str]] = {}
        pred: dict[str, set[str]] = {}
        for (src, _lid), targets in outs.items():
            succ.setdefault(src, set()).update(targets)
            for t in targets:
                pred.setdefault(t, set()).add(src)

        def downstream(starts: Sequence[str]) -> set[str]:
            seen, stack = set(), list(starts)
            while stack:
                for y in succ.get(stack.pop(), ()):
                    if y not in seen:
                        seen.add(y)
                        stack.append(y)
            return seen

        def edge_source(u: str) -> str:
            seen = set()
            while is_bypass_v(u) and u not in seen:
                seen.add(u)
                feeders = sorted(p for p in pred.get(u, ()) if p in node_set)
                if not feeders:
                    break
                u = feeders[0]
            return u

        runs: dict[str, list[str]] = {}
        run_of = dict(level_run_of)
        later_parts: set[str] = set()
        for root, members in level.items():
            member_set = set(members)
            reached = downstream(members)
            reentries = [
                v
                for v in sorted(members)
                if any(
                    u not in member_set
                    and u in reached
                    and edge_source(u) not in member_set
                    for u in pred.get(v, ())
                )
            ]
            below = {v: downstream([v]) | {v} for v in reentries}
            part = {n: sum(1 for v in reentries if n in below[v]) for n in members}
            if len(set(part.values())) <= 1:
                runs[root] = members
                continue
            for n in members:
                key = root if part[n] == 0 else f"{root}#{part[n]}"
                if part[n]:
                    later_parts.add(key)
                runs.setdefault(key, []).append(n)
                run_of[n] = key
        return _Section(
            section_id, nodes, ins, outs, runs, run_of, level_run_of, later_parts
        )

    def _run_order(self, sec: _Section) -> list[str]:
        first = {
            r: (min(self.along(n, sec.id) for n in members), r)
            for r, members in sec.runs.items()
        }
        preds: dict[str, set[str]] = {r: set() for r in sec.runs}
        for (src, _lid), targets in sec.outs.items():
            for t in targets:
                if sec.run_of[src] != sec.run_of[t]:
                    preds[sec.run_of[t]].add(sec.run_of[src])
        order: list[str] = []
        placed: set[str] = set()
        while len(order) < len(sec.runs):
            waiting = [r for r in sec.runs if r not in placed]
            ready = [r for r in waiting if preds[r] <= placed]
            run = min(ready or waiting, key=first.__getitem__)
            order.append(run)
            placed.add(run)
        return order

    def _carried_orders(self, section_id: str) -> dict[str, list[list[str]]]:
        """Per exit, the orders it carries: on two or more lines it shares
        with an upstream exit it descends from, where both reach one entry."""
        graph = self.graph
        out: dict[str, list[list[str]]] = {}
        for exit_id in graph.sections[section_id].exit_ports:
            if exit_id not in graph.ports:
                continue
            reached: dict[str, set[str]] = {}
            for lid in graph.station_lines(exit_id):
                for node in _consumers(self.builder, exit_id, lid):
                    port = graph.ports.get(node)
                    if port is not None and port.is_entry:
                        reached.setdefault(node, set()).add(lid)
            for entry_id, lines in sorted(reached.items()):
                shared_with: dict[str, set[str]] = {}
                for lid in lines:
                    for node, _via in self.builder.upstream_sources(entry_id, lid):
                        if (
                            node != exit_id
                            and node in graph.ports
                            and exit_id in self._descendants(node)
                        ):
                            shared_with.setdefault(node, set()).add(lid)
                for upstream, shared in sorted(shared_with.items()):
                    held = self.builder.order_of(upstream) if len(shared) >= 2 else None
                    if held is not None and held.lines is not None:
                        out.setdefault(exit_id, []).append(
                            [lid for lid in held.lines if lid in shared]
                        )
        return out

    def _descendants(self, node: str) -> set[str]:
        if node not in self.descendants:
            seen: set[str] = set()
            stack = [node]
            while stack:
                for e in self.graph.edges_from(stack.pop()):
                    if e.target not in seen:
                        seen.add(e.target)
                        stack.append(e.target)
            self.descendants[node] = seen
        return self.descendants[node]

    def _merge(
        self, lines: Sequence[str], chains: Sequence[Sequence[str]], section_id: str
    ) -> list[str]:
        """Merge *chains* into one order, priority deciding between free lines."""
        priority = self.builder.priority
        succ: dict[str, set[str]] = {lid: set() for lid in lines}
        indeg = dict.fromkeys(lines, 0)
        for chain in chains:
            kept = [lid for lid in chain if lid in succ]
            for a, b in zip(kept, kept[1:]):
                if b not in succ[a]:
                    succ[a].add(b)
                    indeg[b] += 1
        return self._kahn(
            lines,
            succ,
            indeg,
            lambda lid: (priority.get(lid, 0), lid),
            section_id,
            None,
        )

    def _kahn(
        self,
        lines: Sequence[str],
        succ: Mapping[str, set[str]],
        indeg: dict[str, int],
        key: Callable[[str], tuple[int, str]],
        section_id: str,
        run: str | None,
    ) -> list[str]:
        heap = [key(lid) for lid in lines if indeg[lid] == 0]
        heapify(heap)
        out: list[str] = []
        left = set(lines)
        while left:
            if not heap:
                stuck = min(left, key=key)
                self.forced.append(ForcedCrossing(section_id, run, tuple(sorted(left))))
                heap = [key(stuck)]
            _, lid = heappop(heap)
            if lid not in left:
                continue
            left.discard(lid)
            out.append(lid)
            for m in succ[lid]:
                indeg[m] -= 1
                if indeg[m] == 0 and m in left:
                    heappush(heap, key(m))
        return out

    # ---- one run

    def _place_run(
        self,
        sec: _Section,
        key: str,
        entry_orders: Mapping[str, list[str]],
        section_order: Sequence[str],
        slots: Mapping[str, int],
        carried: Mapping[str, list[list[str]]],
        born: set[str],
        born_chains: Sequence[Sequence[str]],
    ) -> None:
        graph = self.graph
        members = sec.runs[key]
        member_set = set(members)
        run = _Run(
            sec,
            key,
            members,
            member_set,
            sorted({lid for n in members for lid in graph.station_lines(n)}),
            self.lateral(members[0], sec.id),
            sec.level_run_of[members[0]],
            self._passing(sec.id, member_set),
        )
        riding = {lid for n in members for lid in entry_orders.get(n, ())}
        arrival: dict[str, float] = {}
        neighbour: dict[str, int] = {}
        nest: dict[str, float] = {}
        joins: dict[str, str] = {}
        for lid in run.present:
            if lid in riding:
                arrival[lid] = 0.0
                continue
            found = self._arrival(run, lid)
            arrival[lid] = found.key
            if found.neighbour is not None:
                neighbour[lid] = found.neighbour
            if found.nest is not None:
                nest[lid] = found.nest
            if found.joins is not None:
                joins[lid] = found.joins

        def rank(lid: str) -> tuple[float, int, int, str]:
            return (arrival[lid], neighbour.get(lid, -1), slots[lid], lid)

        order = sorted(run.present, key=rank)
        if nest:
            order = _nest_by_source(order, arrival, nest, rank)
        if born & set(run.present):
            order = self._born_order(sec, order, born & set(run.present))
        for chain in born_chains:
            order = _apply_chain(order, chain)
        chains = self._bundles_into(run) if key in sec.later_parts else []
        if chains:
            order = self._keep_bundles(run, order, chains)
        for exit_id in [n for n in members if n in carried]:
            for chain in carried[exit_id]:
                seq = [
                    lid
                    for lid in (chain if self.senses[exit_id] > 0 else chain[::-1])
                    if lid in order
                ]
                for i, lid in zip(sorted(order.index(lid) for lid in seq), seq):
                    order[i] = lid
        fixed: dict[str, int] = {}
        for n in members:
            for lid in graph.station_lines(n):
                if (n, lid) in self.fan_slots:
                    fixed.setdefault(lid, self.fan_slots[(n, lid)])
        pins = self._part_pins(run, fixed)
        for lid, lane in pins.items():
            fixed.setdefault(lid, lane)
        for n in members:
            for lid, lane in self.seam_lanes.get(n, {}).items():
                if fixed.get(lid, lane) == lane:
                    fixed[lid] = pins[lid] = lane
        conflict = partial(
            self._conflict,
            spans=self._spans(sec.id, members, run.present),
            section_id=sec.id,
            nodes=members,
        )
        if pins and chains:
            lanes = self._fill_bundled(
                run, order, section_order, fixed, pins, chains, conflict
            )
        else:
            lanes = self._fill(
                run, order, slots, fixed, bool(pins), arrival, conflict, riding, joins
            )
        for n in members:
            for lid in graph.station_lines(n):
                self.lanes[(n, lid)] = lanes[lid]

    def _passing(
        self, section_id: str, members: set[str]
    ) -> list[tuple[str, float, float, str, str]]:
        graph = self.graph
        out = []
        for e in graph.edges:
            source = graph.ports.get(e.source)
            if (
                source is None
                or not source.is_entry
                or e.source not in members
                or e.target not in members
            ):
                continue
            lo, hi = sorted(
                (self.along(e.source, section_id), self.along(e.target, section_id))
            )
            out.append((e.line_id, lo, hi, e.source, e.target))
        return out

    def _keep_bundles(
        self, run: _Run, order: Sequence[str], chains: Sequence[Sequence[str]]
    ) -> list[str]:
        """*order* with every chain's lines in the chain's order."""
        position = {lid: i for i, lid in enumerate(order)}
        succ: dict[str, set[str]] = {lid: set() for lid in run.present}
        indeg = dict.fromkeys(run.present, 0)
        for chain in chains:
            for a, b in zip(chain, chain[1:]):
                if b not in succ[a]:
                    succ[a].add(b)
                    indeg[b] += 1
        return self._kahn(
            run.present,
            succ,
            indeg,
            lambda lid: (position[lid], lid),
            run.sec.id,
            run.key,
        )

    def _arrival(self, run: _Run, lid: str) -> _Arrival:
        """*lid*'s arrival key, neighbour lane, nest key and joining station on
        *run*."""
        graph, sec, sid = self.graph, run.sec, run.sec.id
        carriers = sorted(
            (n for n in run.members if lid in graph.station_lines(n)),
            key=lambda n: (self.along(n, sid), n),
        )
        carriers = _travel_order(lid, carriers, sec.ins)

        def on_level(node: str) -> bool:
            return (
                sec.level_run_of.get(node) == run.level_run
                and abs(self.lateral(node, sid) - run.lateral) <= SAME_Y_TOLERANCE
            )

        feeders = sec.ins.get((carriers[0], lid), [])
        near = [
            (0, s)
            for s in feeders
            if s not in run.member_set and not on_level(_edge_source(sec, s, lid))
        ]
        if not near and not any(
            s in run.member_set or on_level(_edge_source(sec, s, lid)) for s in feeders
        ):
            near = [
                (1, t)
                for t in sec.outs.get((carriers[-1], lid), [])
                if t not in run.member_set and not on_level(t)
            ]
        if not near:
            return _Arrival(0.0, None, None, None)
        rank = min(p for p, _ in near)
        source = min(
            (s for p, s in near if p == rank),
            key=lambda s: (round(abs(self.lateral(s, sid) - run.lateral), 1), s),
        )
        sign = self.frames(sid).secondary_sign
        side = round(sign * (self.lateral(source, sid) - run.lateral), 1)
        neighbour = self.lanes.get((source, lid), 0)
        port = graph.ports.get(source)
        if (
            rank == 0
            and port is not None
            and port.is_entry
            and port.section_id == sid
            and port.side in perpendicular_port_sides(graph.sections[sid].direction)
            and self.along(source, sid)
            < self.along(carriers[0], sid) - SAME_Y_TOLERANCE
        ):
            return _Arrival(side * _TURNED_IN, neighbour, None, None)
        nest = None
        if abs(side) > _SIDED and rank == 0 and not is_bypass_v(source):
            nest = round((1 if side > 0 else -1) * self.along(source, sid), 1)
        last = carriers[-1]
        leaving = sec.outs.get((last, lid), [])
        gone = [
            t
            for t in leaving
            if t not in run.member_set
            and abs(self.lateral(t, sid) - run.lateral) > SAME_Y_TOLERANCE
        ]
        carries_on = any(
            m != lid
            and m in graph.station_lines(last)
            and any(
                self.along(n, sid) > self.along(last, sid) + SAME_Y_TOLERANCE
                and m in graph.station_lines(n)
                for n in run.members
            )
            for m in run.present
        )
        if gone and carries_on and not any(t in run.member_set for t in leaving):
            target = min(
                gone, key=lambda t: (abs(self.lateral(t, sid) - run.lateral), t)
            )
            parting = round(sign * (self.lateral(target, sid) - run.lateral), 1)
            if (
                abs(parting) > _SIDED
                and abs(side) > _SIDED
                and (parting > 0) != (side > 0)
            ):
                return _Arrival(
                    parting + (_OUTSIDE if parting > 0 else -_OUTSIDE),
                    neighbour,
                    None,
                    None,
                )
        return _Arrival(side, neighbour, nest, carriers[0] if rank == 0 else None)

    def _part_pins(self, run: _Run, fixed: Mapping[str, int]) -> dict[str, int]:
        """Lanes the run's lines keep from an earlier part of their level run."""
        sec, sid = run.sec, run.sec.id
        pins: dict[str, int] = {}
        for (target, lid), sources in sorted(sec.ins.items()):
            if target not in run.member_set:
                continue
            for node in sources:
                source = _edge_source(sec, node, lid)
                lane = self.lanes.get((source, lid))
                if (
                    lane is None
                    or source in run.member_set
                    or sec.level_run_of[source] != run.level_run
                    or abs(self.lateral(source, sid) - self.lateral(target, sid))
                    > SAME_Y_TOLERANCE
                    or fixed.get(lid, lane) != lane
                    or pins.get(lid, lane) != lane
                ):
                    continue
                pins[lid] = lane
        return pins

    def _bundles_into(self, run: _Run) -> list[list[str]]:
        """Each in-section bundle entering *run*, in its source's lane order."""
        into: dict[str, list[str]] = {}
        for (source, lid), targets in run.sec.outs.items():
            if (
                source not in run.member_set
                and lid in run.present
                and (source, lid) in self.lanes
                and any(t in run.member_set for t in targets)
            ):
                into.setdefault(source, []).append(lid)
        lanes = self.lanes
        return [
            sorted(lines, key=lambda lid: (lanes[(source, lid)], lid))
            for source, lines in into.items()
            if len(lines) >= 2
        ]

    def _fill_bundled(
        self,
        run: _Run,
        order: Sequence[str],
        section_order: Sequence[str],
        fixed: dict[str, int],
        pins: Mapping[str, int],
        chains: Sequence[Sequence[str]],
        conflict: _Conflict,
    ) -> dict[str, int]:
        present = run.present
        before: dict[str, set[str]] = {lid: set() for lid in present}
        for chain in chains:
            for i, a in enumerate(chain):
                for b in chain[i + 1 :]:
                    before[b].add(a)
        for lid in sorted(pins):
            if fixed.get(lid) == pins[lid] and any(
                m in pins and pins[m] >= pins[lid] for m in before[lid]
            ):
                del fixed[lid]
        taken = set(fixed.values())
        lanes = dict(fixed)
        ranked = [lid for lid in section_order if lid in run.present]
        slot: dict[str, int] = {}
        for j, lid in enumerate(ranked):
            slot[lid] = 1 + max(
                (slot[m] for m in ranked[:j] if conflict(lid, m)), default=-1
            )
        last = -1
        for lid in order:
            if lid in lanes:
                continue
            lo = max([lanes[m] + 1 for m in before[lid] if m in lanes] + [last + 1, 0])
            hi = min(
                (
                    fixed[m]
                    - 1
                    - sum(
                        1
                        for x in present
                        if x not in lanes
                        and x != lid
                        and lid in before[x]
                        and x in before[m]
                    )
                    for m in present
                    if lid in before[m] and m in fixed
                ),
                default=None,
            )
            start = max(slot[lid], lo)
            if hi is not None and hi >= lo:
                start = min(start, hi)
            top = (
                hi
                if hi is not None and hi >= start
                else start + len(present) + len(taken)
            )
            lane = next((x for x in range(start, top + 1) if x not in taken), None)
            if lane is None and hi is not None:
                lane = next((x for x in range(hi, lo - 1, -1) if x not in taken), None)
            if lane is None:
                lane = max(start, lo)
                while lane in taken:
                    lane += 1
            lanes[lid] = lane
            taken.add(lane)
            last = lane
        return lanes

    def _fill(
        self,
        run: _Run,
        order: Sequence[str],
        slots: Mapping[str, int],
        fixed: Mapping[str, int],
        pinned: bool,
        arrival: Mapping[str, float],
        conflict: _Conflict,
        riding: set[str],
        joins: Mapping[str, str],
    ) -> dict[str, int]:
        lanes = dict(fixed)
        taken = set(fixed.values())
        base = self._pinned_floor(order, fixed) if pinned else 0
        seamed = pinned and any(n in self.seam_lanes for n in run.members)
        for i, lid in enumerate(order):
            if lid in lanes:
                continue
            if seamed and lid not in riding:
                side, mates = self._born_side(run, lid, fixed)
                if side == 0:
                    side, mates = self._arrival_mates(
                        joins.get(lid), arrival[lid], fixed
                    )
                later = [m for m in order[i + 1 :] if m not in riding]
                lane = self._born_lane(
                    run, lid, later, side, mates, lanes, fixed, conflict
                )
                lanes[lid] = self._clear_breeze(run, lid, lane, lanes, taken)
                taken.add(lanes[lid])
                continue
            below = [lanes[m] for m in order[:i] if m in lanes and conflict(lid, m)]
            floor = max(below) + 1 if below else base
            lane = floor
            if not below:
                lane = slots[lid] if pinned else min(slots[x] for x in run.present)
                partner = self._partner_lane(
                    run, lid, order[:i], lanes, conflict, arrival[lid]
                )
                if partner is not None and (partner >= base or not pinned):
                    lane = partner
            if pinned:
                after = next(
                    (
                        j
                        for j in range(i + 1, len(order))
                        if order[j] in fixed and conflict(lid, order[j])
                    ),
                    None,
                )
                if after is not None:
                    room = sum(
                        1
                        for x in order[i + 1 : after]
                        if x not in fixed and conflict(lid, x)
                    )
                    lane = max(min(lane, fixed[order[after]] - room - 1), floor)
            while any(
                lanes[m] == lane and conflict(lid, m) for m in lanes
            ) or self._breezes(run, lid, lane, lanes):
                lane += 1
            lanes[lid] = lane
            taken.add(lane)
        return lanes

    def _pinned_floor(self, order: Sequence[str], fixed: Mapping[str, int]) -> int:
        """The lowest lane a pinned run fills from: below zero when a fixed lane
        leaves too few lanes above zero for the free lines ordered before it."""
        return min(
            [
                0,
                *(
                    fixed[lid] - sum(1 for m in order[:j] if m not in fixed)
                    for j, lid in enumerate(order)
                    if lid in fixed
                ),
            ]
        )

    # ---- born lines in a seam-pinned run

    def _born_lane(
        self,
        run: _Run,
        lid: str,
        later: Sequence[str],
        side: int,
        mates: _Mates,
        lanes: Mapping[str, int],
        fixed: Mapping[str, int],
        conflict: _Conflict,
    ) -> int:
        """The first lane past *mates* on *side* (else from the lowest fixed
        lane up) no line *lid* coexists with holds; below them, a lane is left
        for each later line placed below the same mates."""

        def held(lane: int) -> bool:
            return any(lanes[m] == lane and conflict(lid, m) for m in lanes)

        step = -1 if side < 0 else 1
        if side > 0:
            lane = 1 + max(fixed[m] for m in mates)
        elif side < 0:
            lane = min(fixed[m] for m in mates) - 1
            lane -= self._born_below(run, later, fixed, mates)
        else:
            lane = min(fixed.values())
        while held(lane):
            lane += step
        return lane

    def _born_below(
        self, run: _Run, later: Sequence[str], fixed: Mapping[str, int], mates: _Mates
    ) -> int:
        return sum(
            1
            for lid in later
            if lid not in fixed and self._born_side(run, lid, fixed) == (-1, mates)
        )

    def _born_side(
        self, run: _Run, lid: str, fixed: Mapping[str, int]
    ) -> tuple[int, _Mates]:
        """The side, in lanes, *lid* takes of the fixed lines it is born
        beside and parts from, with those lines; ``0`` when undecided."""
        graph = self.graph
        start = min(
            (n for n in run.members if lid in graph.station_lines(n)),
            key=lambda n: (self.along(n, run.sec.id), n),
            default=None,
        )
        if start is None:
            return 0, ()
        mates = tuple(
            m
            for m in fixed
            if m in graph.station_lines(start)
            and any(e.line_id == m for e in graph.edges_from(start))
        )
        if not mates:
            return 0, ()
        fork = self._fork_side(run, lid, start, mates)
        if fork is not None:
            return fork
        heading = self._heading(lid, start, run)
        if heading is None:
            return 0, ()
        theirs = [_sign(self._heading(m, start, run)) for m in mates]
        if all(_sign(heading) > other for other in theirs):
            return 1, mates
        if all(_sign(heading) < other for other in theirs):
            return -1, mates
        return 0, ()

    def _fork_side(
        self, run: _Run, lid: str, start: str, mates: _Mates
    ) -> tuple[int, _Mates] | None:
        """Where *lid* or a mate forks right past an exit: the side *lid* lies
        of the mates when every side it heads for lies at or past every side
        each heads for; ``None`` when nothing forks there."""
        mine = self._fork_sides(lid, start, run)
        theirs = [self._fork_sides(m, start, run) for m in mates]
        known = [t for t in theirs if t is not None]
        if mine is None or len(known) < len(theirs):
            return None
        if len(mine) < 2 and all(len(t) < 2 for t in known):
            return None
        if all(min(mine) >= max(t) and mine != t for t in known):
            return 1, mates
        if all(max(mine) <= min(t) and mine != t for t in known):
            return -1, mates
        return 0, ()

    def _arrival_mates(
        self, joins: str | None, side: float, fixed: Mapping[str, int]
    ) -> tuple[int, _Mates]:
        """A line joining the run from an in-section source on one side takes
        that side of the fixed lines at the station it joins."""
        if joins is None or abs(side) <= _SIDED:
            return 0, ()
        mates = tuple(m for m in fixed if m in self.graph.station_lines(joins))
        return (1 if side > 0 else -1, mates) if mates else (0, ())

    def _departure(
        self, lid: str, node: str, run: _Run
    ) -> tuple[float, str | None] | None:
        """Walking *lid* downstream from *node* one edge at a time inside its
        section: its lane offset where it first leaves *run*'s lane
        coordinate, or the flow-side exit it reaches on that coordinate;
        ``None`` where it forks, leaves the section or reaches a perpendicular
        exit first."""
        graph = self.graph
        sid = run.sec.id
        seen: set[str] = set()
        while node not in seen:
            seen.add(node)
            targets = sorted(
                {e.target for e in graph.edges_from(node) if e.line_id == lid}
            )
            if len(targets) != 1:
                return None
            node = targets[0]
            if graph.stations[node].section_id != sid:
                return None
            offset = self.lateral(node, sid) - run.lateral
            if abs(offset) > SAME_Y_TOLERANCE:
                return offset * self.frames(sid).secondary_sign, None
            port = graph.ports.get(node)
            if port is not None and not port.is_entry:
                return None if self.perpendicular(node) else (0.0, node)
        return None

    def _heading(self, lid: str, node: str, run: _Run) -> float | None:
        """The side, in lanes, *lid* heads for from *node*: where it leaves the
        run, or past its exit its nearest consumer; ``0`` for a consumer level
        with the exit."""
        found = self._departure(lid, node, run)
        if found is None:
            return None
        offset, exit_id = found
        if exit_id is None:
            return offset
        lateral = self._nearest(exit_id, lid)
        if lateral is None:
            return None
        if abs(lateral) <= SAME_Y_TOLERANCE:
            return 0.0
        return lateral * self.senses[exit_id]

    def _fork_sides(self, lid: str, node: str, run: _Run) -> set[int] | None:
        """As :meth:`_heading`, but every side: a line forking right past its
        exit heads for each of its consumers' sides."""
        found = self._departure(lid, node, run)
        if found is None:
            return None
        offset, exit_id = found
        if exit_id is None:
            return {_sign(offset)}
        reaches = self._downstream(exit_id, lid)
        if reaches is None:
            return None
        sense = self.senses[exit_id]
        if len(reaches) > 1 and not self._near_fork(exit_id, [lid]):
            return {_sign(min((r.lateral for r in reaches), key=abs) * sense)}
        return {_sign(r.lateral * sense) for r in reaches}

    def _near_fork(self, exit_id: str, lines: Sequence[str]) -> bool:
        """Whether every branch of *lines* leaves within the exit's first grid
        step: a level or approach-record branch, or a cell one step ahead."""
        for lid in lines:
            reaches = self._downstream(exit_id, lid)
            if reaches is None:
                return False
            if any(
                r.ahead is not None and r.ahead > 1.0 + SAME_Y_TOLERANCE
                for r in reaches
            ):
                return False
        return True

    def _breezes(
        self, run: _Run, lid: str, lane: int, lanes: Mapping[str, int]
    ) -> bool:
        """Whether *lane* lies next to a line that runs past one of *run*'s
        stations, without stopping, on an edge from the run's entry port while
        the other of the two stops there."""
        graph = self.graph
        sid = run.sec.id
        for other, held in lanes.items():
            if other == lid or abs(held - lane) != 1:
                continue
            for stops, passes in ((lid, other), (other, lid)):
                for n in run.members:
                    here = graph.station_lines(n)
                    if stops not in here or passes in here:
                        continue
                    at = self.along(n, sid)
                    for line, lo, hi, source, target in run.passing:
                        if (
                            line == passes
                            and lo + SAME_Y_TOLERANCE < at < hi - SAME_Y_TOLERANCE
                            and abs(self.lateral(n, sid) - self.lateral(source, sid))
                            <= SAME_Y_TOLERANCE
                            and abs(self.lateral(n, sid) - self.lateral(target, sid))
                            <= SAME_Y_TOLERANCE
                        ):
                            return True
        return False

    def _clear_breeze(
        self, run: _Run, lid: str, lane: int, lanes: Mapping[str, int], taken: set[int]
    ) -> int:
        """*lane*, stepped away from a line passing beside it to the first
        free lane clear of it."""
        for _ in range(len(lanes) + 2):
            if not self._breezes(run, lid, lane, lanes):
                break
            beside = min(
                lanes[m] for m in lanes if m != lid and abs(lanes[m] - lane) == 1
            )
            step = 1 if lane > beside else -1
            lane += step
            while lane in taken:
                lane += step
        return lane

    def _partner_lane(
        self,
        run: _Run,
        lid: str,
        earlier: Sequence[str],
        lanes: Mapping[str, int],
        conflict: _Conflict,
        side: float,
    ) -> int | None:
        """The lane of an earlier line *lid* hands over to at a shared station,
        on the side *lid* arrives from (the lowest lane below, the highest
        above, else the latest-ordered)."""
        graph = self.graph
        found: list[int] = []
        for m in reversed(earlier):
            if m not in lanes or m == lid or conflict(lid, m):
                continue
            if any(
                lid in graph.station_lines(n) and m in graph.station_lines(n)
                for n in run.members
            ) and not any(lanes[x] == lanes[m] and conflict(lid, x) for x in lanes):
                found.append(lanes[m])
        if not found:
            return None
        if side < 0:
            return min(found)
        return max(found) if side > 0 else found[0]

    # ---- coexistence

    def _spans(
        self, section_id: str, nodes: Sequence[str], lines: Iterable[str]
    ) -> _Spans:
        """Each line's flow span per connected piece of it among *nodes*."""
        graph = self.graph
        out: _Spans = {}
        for lid in lines:
            on = [n for n in nodes if lid in graph.station_lines(n)]
            if not on:
                continue
            parent = {n: n for n in on}

            def find(x: str) -> str:
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            for n in on:
                for e in graph.edges_from(n):
                    if e.line_id == lid and e.target in parent:
                        parent[find(n)] = find(e.target)
            pieces: dict[str, tuple[float, float]] = {}
            for n in on:
                at = self.along(n, section_id)
                lo, hi = pieces.get(find(n), (at, at))
                pieces[find(n)] = (min(lo, at), max(hi, at))
            out[lid] = sorted(pieces.values())
        return out

    def _conflict(
        self,
        lid: str,
        other: str,
        *,
        spans: _Spans,
        section_id: str,
        nodes: Sequence[str],
    ) -> bool:
        """Whether *lid* and *other* coexist among *nodes*."""
        return any(
            self._coexist(lid, p, other, q, section_id, nodes)
            for p in spans[lid]
            for q in spans[other]
        )

    def _coexist(
        self,
        lid: str,
        span: tuple[float, float],
        other: str,
        other_span: tuple[float, float],
        section_id: str,
        nodes: Sequence[str],
    ) -> bool:
        lo, hi = max(span[0], other_span[0]), min(span[1], other_span[1])
        if hi - lo > SAME_Y_TOLERANCE:
            return True
        if hi - lo < -SAME_Y_TOLERANCE:
            return False
        for ending, (a, b), born, (c, d) in (
            (lid, span, other, other_span),
            (other, other_span, lid, span),
        ):
            if abs(b - lo) > SAME_Y_TOLERANCE or abs(c - lo) > SAME_Y_TOLERANCE:
                continue
            if not (
                self._ends_at(ending, lo, section_id, nodes, born=False)
                and self._ends_at(born, lo, section_id, nodes, born=True)
            ):
                continue
            if b - a <= SAME_Y_TOLERANCE and not self._flows(
                ending, lo, section_id, nodes, born=False
            ):
                continue
            if d - c <= SAME_Y_TOLERANCE and not self._flows(
                born, lo, section_id, nodes, born=True
            ):
                continue
            return False
        return True

    def _ends_at(
        self, lid: str, at: float, section_id: str, nodes: Sequence[str], *, born: bool
    ) -> bool:
        """Whether *lid* terminates (or, *born*, starts) at a node at *at*."""
        graph = self.graph
        for n in nodes:
            if (
                lid in graph.station_lines(n)
                and abs(self.along(n, section_id) - at) <= SAME_Y_TOLERANCE
            ):
                edges = graph.edges_to(n) if born else graph.edges_from(n)
                if not any(e.line_id == lid for e in edges):
                    return True
        return False

    def _flows(
        self, lid: str, at: float, section_id: str, nodes: Sequence[str], *, born: bool
    ) -> bool:
        """Whether every edge of *lid* at the nodes at *at* arrives from
        upstream (or, *born*, leaves downstream), and one does."""
        graph = self.graph
        found = False
        for n in nodes:
            if (
                lid not in graph.station_lines(n)
                or abs(self.along(n, section_id) - at) > SAME_Y_TOLERANCE
            ):
                continue
            for e in graph.edges_from(n) if born else graph.edges_to(n):
                if e.line_id != lid:
                    continue
                other = e.target if born else e.source
                if other not in graph.stations:
                    return False
                delta = self.along(other, section_id) - at
                if (
                    (delta <= SAME_Y_TOLERANCE)
                    if born
                    else (delta >= -SAME_Y_TOLERANCE)
                ):
                    return False
                found = True
        return found


def _sign(value: float | None) -> int:
    if value is None or abs(value) <= SAME_Y_TOLERANCE:
        return 0
    return 1 if value > 0 else -1


def _on_grid(lanes: float) -> int | None:
    """*lanes* as a whole number of lanes, or ``None`` when it falls between."""
    nearest = round(lanes)
    return nearest if abs(lanes - nearest) <= _ON_GRID else None


def _dot(a: Direction, b: Direction) -> float:
    ax, ay = direction_vector(a)
    bx, by = direction_vector(b)
    return ax * bx + ay * by


def _ahead(
    point: tuple[float, float], origin: tuple[float, float], travel: Direction
) -> float:
    vx, vy = direction_vector(travel)
    return (point[0] - origin[0]) * vx + (point[1] - origin[1]) * vy


def _boxes_across(graph: MetroGraph, a: str, b: str, at: float, axis: str) -> bool:
    """Whether a section box stands across the straight run between nodes
    *a* and *b* at *at* on lane *axis*."""
    first, second = graph.stations[a], graph.stations[b]
    lo, hi = sorted((first.x, second.x) if axis == "y" else (first.y, second.y))
    for section in graph.sections.values():
        if section.bbox_w <= 0:
            continue
        x0, x1 = section.bbox_x, section.bbox_x + section.bbox_w
        y0, y1 = section.bbox_y, section.bbox_y + section.bbox_h
        (c0, c1), (s0, s1) = (
            ((y0, y1), (x0, x1)) if axis == "y" else ((x0, x1), (y0, y1))
        )
        if (
            c0 - SAME_COORD_TOLERANCE <= at <= c1 + SAME_COORD_TOLERANCE
            and min(s1, hi) - max(s0, lo) > SAME_COORD_TOLERANCE
        ):
            return True
    return False


def _fill_round_pins(order: Sequence[str], pins: Mapping[str, int]) -> dict[str, int]:
    """Lanes for an entry's lines ascending in *order*: pinned lines on their
    pins, lines before the first pinned one on the free lanes below it nearest
    first, and every later line on the first free lane past the line before."""
    lanes = dict(pins)
    taken = set(pins.values())
    first = next(i for i, lid in enumerate(order) if lid in pins)
    lane = pins[order[first]]
    for lid in reversed(order[:first]):
        lane -= 1
        while lane in taken:
            lane -= 1
        lanes[lid] = lane
        taken.add(lane)
    previous = pins[order[first]]
    for lid in order[first:]:
        if lid in pins:
            previous = max(previous, pins[lid])
            continue
        lane = previous + 1
        while lane in taken:
            lane += 1
        lanes[lid] = lane
        taken.add(lane)
        previous = lane
    return lanes


def _apply_chain(order: Sequence[str], chain: Sequence[str]) -> list[str]:
    """*order* with *chain*'s lines, in its places, in *chain*'s order."""
    seq = [lid for lid in chain if lid in order]
    out = list(order)
    for i, lid in zip(sorted(out.index(lid) for lid in seq), seq):
        out[i] = lid
    return out


def _exits_reached(graph: MetroGraph, entry_id: str, line_id: str) -> set[str]:
    """The exits of *entry_id*'s section *line_id* reaches from it inside."""
    section_id = graph.ports[entry_id].section_id
    found: set[str] = set()
    seen: set[str] = set()
    frontier = [entry_id]
    while frontier:
        node = frontier.pop()
        if node in seen:
            continue
        seen.add(node)
        for edge in graph.edges_from(node):
            if edge.line_id != line_id:
                continue
            port = graph.ports.get(edge.target)
            if port is not None and not port.is_entry and port.section_id == section_id:
                found.add(edge.target)
            elif graph.stations[edge.target].section_id == section_id:
                frontier.append(edge.target)
    return found


def _nest_by_source(
    order: Sequence[str],
    arrival: Mapping[str, float],
    nest: Mapping[str, float],
    rank: Callable[[str], tuple[float, int, int, str]],
) -> list[str]:
    """Lines tied on arrival that all arrive from sources, at more than one
    nest key, ordered by it."""
    groups: dict[float, list[str]] = {}
    for lid in order:
        groups.setdefault(arrival[lid], []).append(lid)
    out: list[str] = []
    for _value, lines in sorted(groups.items()):
        if (
            len(lines) > 1
            and all(lid in nest for lid in lines)
            and len({nest[lid] for lid in lines}) > 1
        ):
            lines = sorted(lines, key=lambda lid: (nest[lid], *rank(lid)[1:]))
        out.extend(lines)
    return out


def _edge_source(sec: _Section, node: str, line_id: str) -> str:
    """*node*, or for a bypass-V helper the source of the edge it splits."""
    seen: set[str] = set()
    while is_bypass_v(node) and node not in seen:
        seen.add(node)
        feeders = sorted(sec.ins.get((node, line_id), []))
        if not feeders:
            break
        node = feeders[0]
    return node


def _travel_order(
    line_id: str, carriers: list[str], ins: Mapping[tuple[str, str], list[str]]
) -> list[str]:
    """*carriers* in *line_id*'s own travel along them, the given order
    deciding between nodes the line's edges leave unordered."""
    carrier_set = set(carriers)
    feeders = {
        n: {s for s in ins.get((n, line_id), []) if s in carrier_set and s != n}
        for n in carriers
    }
    out: list[str] = []
    done: set[str] = set()
    while len(out) < len(carriers):
        waiting = [n for n in carriers if n not in done]
        node = next((n for n in waiting if feeders[n] <= done), waiting[0])
        out.append(node)
        done.add(node)
    return out


def build_lane_plan(graph: MetroGraph) -> LanePlan:
    """Decide *graph*'s lane plan from its structure and placement."""
    frames = _Frames(graph)
    turns = _perpendicular_turns(graph, frames)
    travels = _order_travels(graph, turns)
    senses = {
        port_id: _sense(frames(graph.ports[port_id].section_id), travel)
        for port_id, travel in travels.items()
    }
    planner = _Tracks(graph, frames, senses)
    tracks = planner.plan()
    port_orders = decide_ports(planner.builder)
    return LanePlan(
        port_orders=port_orders,
        order_travels=travels,
        lane_senses=senses,
        perpendicular_turns=turns,
        far_sides=_far_sides(planner.builder),
        tracks=tracks,
        seams=SeamJoint(
            dict(sorted(planner.seam_lanes.items())),
            dict(sorted(planner.seam_sources.items())),
            planner._perpendicular_seams(),
        ),
    )


def start_layout(graph: MetroGraph) -> None:
    """Forget the previous layout's record: this layout seals its own."""
    graph._lane_plan = None


def seal_lane_plan(graph: MetroGraph) -> None:
    """Seal the layout's record at its first offsets run at or after 5.1."""
    if graph._lane_plan is None and JUNCTIONS_POSITIONED in graph._stages_completed:
        graph._lane_plan = build_lane_plan(graph)
