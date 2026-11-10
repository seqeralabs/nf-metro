"""Plan-time line order at every section port, decided from structure alone.

A :class:`PortOrderPlan` states, once per offsets run and before any lane
offset is written, the order each port's lines cross its edge.  Orders are read
across the direction of travel through the port, from the traveller's left to
their right: an entry port's travel is its inward normal, an exit port's its
outward normal.  A nested bundle keeps each line on the same side of its travel
through every turn, so this frame makes one port's order directly comparable
with the order of the port its lines came from, and it is a rotation invariant:
a map turned a quarter turn plans the same orders, and a reflected map plans
each order reversed.

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
   section's flow, which the line rides before any turn into the port.  A line
   with several reference nodes rides the one nearest the port's lane.
2. *upstream* - the line's rank in its reference port's planned order, so lines
   sharing a source keep that source's order across the seam.
3. *priority* - line declaration order.

A port whose order needs an input the plan cannot supply (the order of a
reference port that depends on it round a section cycle, or a reference node for
one of its lines) is undecided and names that input; it never takes a value a
later phase wrote.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType

from nf_metro.layout.constants import SAME_Y_TOLERANCE
from nf_metro.layout.geometry import AxisFrame, perpendicular_port_sides
from nf_metro.layout.routing.common import ENTRY_SIDE_DIRECTION, EXIT_SIDE_DIRECTION
from nf_metro.layout.routing.orientation import direction_vector
from nf_metro.parser.model import MetroGraph, Port

_Vector = tuple[float, float]


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
    section_id: str
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

    orders: Mapping[str, PortOrder] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __iter__(self) -> Iterator[PortOrder]:
        return iter(self.orders.values())

    def undecided(self) -> tuple[PortOrder, ...]:
        return tuple(order for order in self if not order.decided)


def _sub(a: _Vector, b: _Vector) -> _Vector:
    return (a[0] - b[0], a[1] - b[1])


def _dot(a: _Vector, b: _Vector) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _right_normal(travel: _Vector) -> _Vector:
    """Screen right-hand normal of *travel* (Y grows downward)."""
    return (-travel[1], travel[0])


def _flow_vector(direction: str) -> _Vector:
    primary, _secondary = AxisFrame.axes_for_direction(direction)
    sign = AxisFrame.flow_sign(direction)
    return (sign, 0.0) if primary == "x" else (0.0, sign)


def _port_travel(port: Port) -> _Vector:
    sides = ENTRY_SIDE_DIRECTION if port.is_entry else EXIT_SIDE_DIRECTION
    dx, dy = direction_vector(sides[port.side])
    return (float(dx), float(dy))


class _Builder:
    """Decides each port once, depth-first through the ports it inherits from."""

    def __init__(self, graph: MetroGraph) -> None:
        self.graph = graph
        self.junctions = graph.junction_ids
        self.priority = {lid: i for i, lid in enumerate(graph.lines)}
        self.decided: dict[str, PortOrder] = {}
        self.in_progress: set[str] = set()

    def point(self, node_id: str) -> _Vector:
        station = self.graph.stations[node_id]
        return (station.x, station.y)

    def cell_centre(self, node_id: str) -> _Vector | None:
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
        if port.is_entry:
            normal = _right_normal(_port_travel(port))
        else:
            normal = _right_normal(_flow_vector(section.direction))
        lines = graph.station_lines(port_id)
        here = self.cell_centre(port_id)

        refs: dict[str, str] = {}
        laterals: dict[str, float] = {}
        ref_nodes: set[str] = set()
        ref_laterals: list[float] = []
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
                    n: _dot(_sub(c, here), normal)
                    for n, c in cells.items()
                    if c is not None
                }
            else:
                measured = {n: _dot(_sub(self.point(n), origin), normal) for n in nodes}
            ref_nodes.update(nodes)
            ref_laterals.extend(measured.values())
            ref = min(nodes, key=lambda n: (abs(measured[n]), measured[n], n))
            refs[lid] = ref
            laterals[lid] = measured[ref]
        if not port.is_entry:
            via_junction = self.feeds_junction(port_id)
        port_class = _classify(
            port, section.direction, ref_nodes, ref_laterals, via_junction
        )

        def order(
            lines: tuple[str, ...] | None,
            basis: OrderBasis | None,
            missing: str | None = None,
        ) -> PortOrder:
            return PortOrder(
                port_id, section.id, port.is_entry, port_class, lines, basis, missing
            )

        if missing is not None:
            return order(None, None, missing)
        if len(lines) < 2:
            return order(tuple(lines), OrderBasis.SINGLE)

        lateral_rank = _tolerance_ranks(laterals)
        upstream: dict[str, int] = {}
        for lid in lines:
            ref = refs[lid]
            rank = lateral_rank[lid]
            tied = [o for o in lines if o != lid and lateral_rank[o] == rank]
            # A rank orders lines only against lines of the same port.
            shared = all(refs[o] == ref for o in tied)
            if ref not in graph.ports or not tied or not shared:
                upstream[lid] = 0
                continue
            held = self.order_of(ref)
            if held is None or held.lines is None:
                why = "on a section cycle" if held is None else "undecided"
                return order(None, None, f"order of reference port {ref!r} ({why})")
            upstream[lid] = held.lines.index(lid) if lid in held.lines else 0
        keys = {
            lid: (lateral_rank[lid], upstream[lid], self.priority.get(lid, 0))
            for lid in lines
        }
        ordered = sorted(lines, key=lambda lid: (keys[lid], lid))
        return order(tuple(ordered), _basis(ordered, keys))


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
    port: Port,
    direction: str,
    ref_nodes: set[str],
    ref_laterals: Sequence[float],
    via_junction: bool,
) -> PortClass:
    if port.side in perpendicular_port_sides(direction):
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
