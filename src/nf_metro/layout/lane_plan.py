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
  every exit's order is read.

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

**Section order.**  The merge of every entry's order read up the lane
index, line priority deciding between lines no entry orders.  A line's
*section slot* is one past the highest slot of an earlier line it coexists
with in the section.

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
lane order.  An exit carries, on lines it shares with an upstream exit it
descends from, that exit's order where both reach one entry.

**Lanes.**  A fan plan's carriers keep their slot; a line riding a level edge
into a part from an earlier part of its run keeps that lane (a pin).  The
other lines take lanes in run order: one past the highest lane of an earlier
line they coexist with; with none, the lane of an earlier line they hand over
to at a shared station (on a side, the lowest or highest such lane, else the
latest-ordered), else the run's floor (the lowest section slot among the run's
lines; in a pinned run, the line's own slot).  A free line in a pinned run
stays below the next pinned line it coexists with, a lane left for each free
line between.  Where bundles enter a pinned part, a pin ordered after a
bundle-mate pinned no lower is dropped, and each free line takes its section
slot among the part's lines, lifted past its bundle-mates laned before it and
the free line before it, clamped below its pinned bundle-mates after it.  A
free line never takes a lane a line it coexists with holds.

Two lines *coexist* on a run when the flow spans of their connected pieces
overlap; touching at one coordinate is a hand-over when one terminates
there arriving from upstream and the other is born there leaving
downstream.

Nothing in layout, routing or rendering reads the record.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from heapq import heapify, heappop, heappush

from nf_metro.layout.constants import SAME_Y_TOLERANCE
from nf_metro.layout.departure_order import JUNCTIONS_POSITIONED
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
        self.builder = PortOrderBuilder(graph, self.exit_order)
        self.lanes: dict[tuple[str, str], int] = {}
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
            self._place_run(sec, run, entry_orders, order, slots, carried)
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
    ) -> None:
        graph = self.graph
        members = sec.runs[key]
        run = _Run(
            sec,
            key,
            members,
            set(members),
            sorted({lid for n in members for lid in graph.station_lines(n)}),
            self.lateral(members[0], sec.id),
            sec.level_run_of[members[0]],
        )
        riding = {lid for n in members for lid in entry_orders.get(n, ())}
        arrival: dict[str, float] = {}
        neighbour: dict[str, int] = {}
        nest: dict[str, float] = {}
        for lid in run.present:
            if lid in riding:
                arrival[lid] = 0.0
                continue
            arrival[lid], lane, nested = self._arrival(run, lid)
            if lane is not None:
                neighbour[lid] = lane
            if nested is not None:
                nest[lid] = nested

        def rank(lid: str) -> tuple[float, int, int, str]:
            return (arrival[lid], neighbour.get(lid, -1), slots[lid], lid)

        order = sorted(run.present, key=rank)
        if nest:
            order = _nest_by_source(order, arrival, nest, rank)
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
            lanes = self._fill(run, order, slots, fixed, bool(pins), arrival, conflict)
        for n in members:
            for lid in graph.station_lines(n):
                self.lanes[(n, lid)] = lanes[lid]

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

    def _arrival(self, run: _Run, lid: str) -> tuple[float, int | None, float | None]:
        """*lid*'s arrival key, neighbour lane and nest key on *run*."""
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
            return 0.0, None, None
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
            return side * _TURNED_IN, neighbour, None
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
                return (
                    parting + (_OUTSIDE if parting > 0 else -_OUTSIDE),
                    neighbour,
                    None,
                )
        return side, neighbour, nest

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
    ) -> dict[str, int]:
        lanes = dict(fixed)
        for i, lid in enumerate(order):
            if lid in lanes:
                continue
            below = [lanes[m] for m in order[:i] if m in lanes and conflict(lid, m)]
            floor = max(below) + 1 if below else 0
            lane = floor
            if not below:
                lane = slots[lid] if pinned else min(slots[x] for x in run.present)
                partner = self._partner_lane(
                    run, lid, order[:i], lanes, conflict, arrival[lid]
                )
                if partner is not None and (partner >= 0 or not pinned):
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
            while any(lanes[m] == lane and conflict(lid, m) for m in lanes):
                lane += 1
            lanes[lid] = lane
        return lanes

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
    return LanePlan(
        port_orders=decide_ports(planner.builder),
        order_travels=travels,
        lane_senses=senses,
        perpendicular_turns=turns,
        far_sides=_far_sides(planner.builder),
        tracks=tracks,
    )


def start_layout(graph: MetroGraph) -> None:
    """Forget the previous layout's record: this layout seals its own."""
    graph._lane_plan = None


def seal_lane_plan(graph: MetroGraph) -> None:
    """Seal the layout's record at its first offsets run at or after 5.1."""
    if graph._lane_plan is None and JUNCTIONS_POSITIONED in graph._stages_completed:
        graph._lane_plan = build_lane_plan(graph)
