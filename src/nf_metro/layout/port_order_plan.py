"""Plan-time line order at every section port, decided from structure alone.

A :class:`PortOrderPlan` states, once per offsets run and before any lane
offset is written, the order each port's lines cross its edge.  Orders are read
across the direction of travel through the port, from the traveller's left to
their right: an entry port's travel is its inward normal, an exit port's its
outward normal.  A nested bundle keeps each line on the same side of its travel
through every turn, so this frame makes one port's order directly comparable
with the order of the port its lines came from, and it is a rotation invariant:
a map turned a quarter turn plans the same orders, and a reflected map plans
each lateral-decided order reversed.

The inputs are the graph's structure (port sides, section flow directions, line
sets, edges and junctions), line declaration priority, section grid cells and
placed station coordinates.  Nothing here reads lane offsets, routes, drafts or
any later phase's output.

Rule, per line at a port, as a sort key with the earlier field deciding:

1. *lateral* - where the line's reference node sits across travel.  An entry
   port's reference node is the line's upstream source, walked back through
   junctions to the first node that is not one (normally the feeding exit
   port); it is measured by its section's grid cell against the port's, across
   the port's own travel, because a rotated map maps cells exactly but not the
   coordinates that text extents move.  An exit port's reference node is the
   in-section station feeding it, measured by placed coordinates across the
   exit's own travel; at a perpendicular exit, across the section's flow, which
   the line rides before it turns into the port.  A line
   with several reference nodes rides the one nearest the port's lane.
2. *upstream* - the line's rank in its reference port's planned order, so lines
   sharing a source keep that source's order across the seam.
3. *priority* - line declaration order.

A port whose order needs an input the plan cannot supply (a reference node for
one of its lines, the grid cells either side of an entry's seam, or a decided
order for a reference port it inherits from, including one round a section
cycle) is undecided and names that input; it never takes a value a
later phase wrote.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from functools import partial
from types import MappingProxyType

from nf_metro.layout.constants import SAME_Y_TOLERANCE
from nf_metro.layout.geometry import flow_entry_side, perpendicular_port_sides
from nf_metro.layout.routing.common import (
    ENTRY_SIDE_DIRECTION,
    EXIT_SIDE_DIRECTION,
    Direction,
)
from nf_metro.layout.routing.orientation import (
    get_point_coordinate,
    lateral_axis,
    lateral_order_sign,
)
from nf_metro.parser.model import MetroGraph, Port

_Point = tuple[float, float]


class PortClass(Enum):
    """The structural kind of a port, by which inputs its order depends on.

    Precedence runs top to bottom: a port takes the first kind it matches.
    """

    PERPENDICULAR = "perpendicular"
    """On a section edge parallel to the flow, so its lines turn into or out of
    the flow at the port."""
    TWO_SIDED = "two-sided"
    """Reference nodes on both sides of the port's lane."""
    SHARED = "shared"
    """Two or more distinct reference nodes, all on one side or level."""
    JUNCTION = "junction"
    """One reference node, joined to the port's far side through a junction."""
    SINGLE_FEEDER = "flow-side single-feeder"
    """One reference node and a direct seam."""


class OrderBasis(Enum):
    """The weakest sort field the port's order needed to separate two lines."""

    SINGLE = "single"
    LATERAL = "lateral"
    UPSTREAM = "upstream"
    PRIORITY = "priority"


@dataclass(frozen=True, slots=True)
class PortOrder:
    """One port's planned line order, or the input an undecided order lacks."""

    port_id: str
    is_entry: bool
    port_class: PortClass
    lines: tuple[str, ...] | None
    """Travel-left to travel-right; ``None`` when undecided."""
    basis: OrderBasis | None
    missing: str | None = None
    """The input an undecided order lacks; ``None`` when decided."""

    @property
    def decided(self) -> bool:
        return self.lines is not None


@dataclass(frozen=True)
class PortOrderPlan:
    """Every port's :class:`PortOrder`, keyed by port id."""

    orders: Mapping[str, PortOrder]

    def __iter__(self) -> Iterator[PortOrder]:
        return iter(self.orders.values())


def _lateral(point: _Point, origin: _Point, travel: Direction) -> float:
    """Signed distance of *point* from *origin* across *travel*, positive to the
    traveller's right."""
    axis = lateral_axis(travel)
    return lateral_order_sign(travel) * (
        get_point_coordinate(point, axis) - get_point_coordinate(origin, axis)
    )


def _port_travel(port: Port) -> Direction:
    sides = ENTRY_SIDE_DIRECTION if port.is_entry else EXIT_SIDE_DIRECTION
    return sides[port.side]


class _Builder:
    """Decides each port once, depth-first through the ports it inherits from."""

    def __init__(self, graph: MetroGraph) -> None:
        self.graph = graph
        self.junctions = graph.junction_ids
        self.priority = {lid: i for i, lid in enumerate(graph.lines)}
        self.decided: dict[str, PortOrder] = {}
        self.in_progress: set[str] = set()

    def point(self, node_id: str) -> _Point:
        station = self.graph.stations[node_id]
        return (station.x, station.y)

    def cell_centre(self, node_id: str) -> _Point | None:
        """Centre of *node_id*'s section's grid cell, in (column, row) units."""
        section_id = self.graph.stations[node_id].section_id
        section = self.graph.sections.get(section_id) if section_id else None
        if section is None or section.grid_col < 0 or section.grid_row < 0:
            return None
        return (
            section.grid_col + section.grid_col_span / 2,
            section.grid_row + section.grid_row_span / 2,
        )

    def upstream_sources(self, port_id: str, line_id: str) -> list[tuple[str, bool]]:
        """Non-junction nodes feeding *line_id* into *port_id*, each with
        whether a junction lies between."""
        found: list[tuple[str, bool]] = []
        seen = {port_id}
        frontier = [
            (e.source, False)
            for e in self.graph.edges_to(port_id)
            if e.line_id == line_id
        ]
        while frontier:
            node, via = frontier.pop()
            if node in seen:
                continue
            seen.add(node)
            if node not in self.junctions:
                found.append((node, via))
                continue
            frontier.extend(
                (e.source, True)
                for e in self.graph.edges_to(node)
                if e.line_id == line_id
            )
        return sorted(found)

    def exit_feeders(self, port_id: str, line_id: str) -> list[str]:
        return sorted(
            {e.source for e in self.graph.edges_to(port_id) if e.line_id == line_id}
        )

    def feeds_junction(self, port_id: str) -> bool:
        return any(e.target in self.junctions for e in self.graph.edges_from(port_id))

    def order_of(self, port_id: str) -> PortOrder | None:
        """*port_id*'s order, or ``None`` while it is being decided (a cycle)."""
        if port_id in self.decided:
            return self.decided[port_id]
        if port_id in self.in_progress:
            return None
        self.in_progress.add(port_id)
        order = self._plan_port(port_id, self.graph.ports[port_id])
        self.in_progress.discard(port_id)
        self.decided[port_id] = order
        return order

    def _plan_port(self, port_id: str, port: Port) -> PortOrder:
        graph = self.graph
        section = graph.section_for_port(port)
        origin = self.point(port_id)
        perpendicular = port.side in perpendicular_port_sides(section.direction)
        if perpendicular and not port.is_entry:
            travel = ENTRY_SIDE_DIRECTION[flow_entry_side(section.direction)]
        else:
            travel = _port_travel(port)
        lines = graph.station_lines(port_id)
        here = self.cell_centre(port_id)

        refs: dict[str, str] = {}
        laterals: dict[str, float] = {}
        measured_nodes: dict[str, float] = {}
        via_junction = False
        missing: str | None = None
        for lid in lines:
            if port.is_entry:
                sources = self.upstream_sources(port_id, lid)
                via_junction |= any(via for _node, via in sources)
                nodes = [node for node, _via in sources]
            else:
                nodes = self.exit_feeders(port_id, lid)
            if not nodes:
                missing = missing or f"reference node of line {lid!r}"
                continue
            if port.is_entry:
                cells = {n: self.cell_centre(n) for n in nodes}
                if here is None or any(c is None for c in cells.values()):
                    missing = missing or f"grid cells at line {lid!r}'s seam"
                    continue
                measured = {
                    n: _lateral(c, here, travel)
                    for n, c in cells.items()
                    if c is not None
                }
            else:
                measured = {n: _lateral(self.point(n), origin, travel) for n in nodes}
            measured_nodes.update(measured)
            ref = min(nodes, key=lambda n: (abs(measured[n]), measured[n], n))
            refs[lid] = ref
            laterals[lid] = measured[ref]
        if not port.is_entry:
            via_junction = self.feeds_junction(port_id)
        port_class = _classify(
            perpendicular,
            set(measured_nodes),
            list(measured_nodes.values()),
            via_junction,
        )
        make = partial(PortOrder, port_id, port.is_entry, port_class)

        if missing is not None:
            return make(None, None, missing)
        if len(lines) < 2:
            return make(tuple(lines), OrderBasis.SINGLE)

        lateral_rank = _tolerance_ranks(laterals)
        upstream: dict[str, int] = {}
        for lid in lines:
            ref = refs[lid]
            rank = lateral_rank[lid]
            tied = [o for o in lines if o != lid and lateral_rank[o] == rank]
            # An upstream rank breaks a lateral tie only between lines that share
            # one reference node.
            shared = all(refs[o] == ref for o in tied)
            if ref not in graph.ports or not tied or not shared:
                upstream[lid] = 0
                continue
            held = self.order_of(ref)
            if held is None or held.lines is None:
                why = "on a section cycle" if held is None else "undecided"
                return make(None, None, f"order of reference port {ref!r} ({why})")
            upstream[lid] = held.lines.index(lid) if lid in held.lines else 0
        keys = {
            lid: (lateral_rank[lid], upstream[lid], self.priority.get(lid, 0))
            for lid in lines
        }
        ordered = sorted(lines, key=lambda lid: (keys[lid], lid))
        return make(tuple(ordered), _basis(ordered, keys))


def _tolerance_ranks(values: Mapping[str, float]) -> dict[str, int]:
    """Rank *values* ascending, values within ``SAME_Y_TOLERANCE`` of the
    previous one sharing its rank."""
    ranks: dict[str, int] = {}
    rank = -1
    previous: float | None = None
    for key in sorted(values, key=lambda k: (values[k], k)):
        if previous is None or values[key] - previous > SAME_Y_TOLERANCE:
            rank += 1
        ranks[key] = rank
        previous = values[key]
    return ranks


def _basis(
    ordered: Sequence[str], keys: Mapping[str, tuple[int, int, int]]
) -> OrderBasis:
    weakest = 0
    for a, b in zip(ordered, ordered[1:]):
        field_index = next(
            (i for i, (x, y) in enumerate(zip(keys[a], keys[b])) if x != y), 2
        )
        weakest = max(weakest, field_index)
    return (OrderBasis.LATERAL, OrderBasis.UPSTREAM, OrderBasis.PRIORITY)[weakest]


def _classify(
    perpendicular: bool,
    ref_nodes: set[str],
    ref_laterals: Sequence[float],
    via_junction: bool,
) -> PortClass:
    if perpendicular:
        return PortClass.PERPENDICULAR
    if len(ref_nodes) >= 2:
        below = any(v < -SAME_Y_TOLERANCE for v in ref_laterals)
        above = any(v > SAME_Y_TOLERANCE for v in ref_laterals)
        return PortClass.TWO_SIDED if below and above else PortClass.SHARED
    if via_junction:
        return PortClass.JUNCTION
    return PortClass.SINGLE_FEEDER


def build_port_order_plan(graph: MetroGraph) -> PortOrderPlan:
    """Decide every port's line order from *graph*'s structure and placement."""
    builder = _Builder(graph)
    for port_id in sorted(graph.ports):
        builder.order_of(port_id)
    return PortOrderPlan(
        MappingProxyType({pid: builder.decided[pid] for pid in sorted(graph.ports)})
    )
