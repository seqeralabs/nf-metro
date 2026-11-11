"""Plan-time line order at every section port, decided from structure alone.

A :class:`PortOrderPlan` states the order each port's lines cross its edge, and
the approach each entry's same-band sources take to reach it.  Orders are read
across the direction of travel through the port, from the traveller's left to
their right: an entry port's travel is its inward normal, an exit port's its
outward normal, and a perpendicular exit's the section flow its lines ride
before they turn into the port.  A nested bundle keeps each line on the same
side of its travel through every turn, so this frame makes one port's order
directly comparable with the order of the port its lines came from, and it is a
rotation invariant: a map turned a quarter turn plans the same orders, and a
reflected map plans each lateral-decided order reversed.

The inputs are the graph's structure (port sides, section flow directions, line
sets, edges and junctions), line declaration priority, section grid cells and
cell packs, and placed station and section-box coordinates.  Nothing here reads
lane offsets, routes, drafts or any later phase's output.

**Grid cells.**  A node is measured by its section's grid cell.  A section
packed into a shared cell sits at its own slot of the cell, the cell's column
extent split among the pack's members in visual order.  An entry port of a
section spanning several grid bands across its lanes lies in the one spanned
band whose extent holds its coordinate, a band's extent being the boxes of the
sections occupying that band alone; with no such band, or several, it lies on
the whole span.  Two nodes share a band when their sections' grid extents across
the travel overlap; their lateral is then zero.

**Approach.**  A source sharing an entry's band reaches it one of three ways
(:class:`ApproachKind`): LEVEL, straight along the band; BYPASS, round a section
standing between them on the band; U_TURN, from ahead of the port, turning back
into its travel.  A bypass passes screen-below, except a same-row bypass into
the LEFT entry of an LR section whose hop a packed cell-mate obstructs, which
runs the row-top corridor (:class:`SideRule`).  A U-turn whose source exit heads
along or against the port's travel turns round left-handed, so its loop lies on
the entry traveller's right; any other U-turn keeps the screen-below side.

**Entry order**, as a sort key with the earlier field deciding:

1. *lateral* - the line's reference source across the port's travel: its grid
   lateral, or for a same-band source its approach (a bypass a quarter lane
   out on its side, a U-turn three quarters out, nearer sources inner).  A line
   fed from several sources rides the one most of the port's other lines come
   from.  A perpendicular entry whose off-band sources all lie level with or
   ahead of it is reached round its own side: within each side the nearer
   source joins the channel later and lands outermost.
2. *refined* - between lines tied on lateral: the source's placed lateral, then
   the side the source joins the shared approach from (the one further along
   the travel joins later on its own side; concentric U-turn loops put the
   nearer source inner), then the line's rank in the source exit's order.
3. *priority* - line declaration order.

Every feeding exit's bundle keeps its order across the seam: the entry order is
the merge of those orders, the key above deciding only between lines no bundle
orders.

**Exit order**: the in-section feeder's placed lateral; lines tied at one
feeder carry the section's order back along the line, the predecessors'
laterals to the first divergence and then the entry port's planned rank.

A port whose order needs an input the plan cannot supply (a reference node for
one of its lines, the grid cells either side of an entry's seam, or a decided
order for a reference port it inherits from, including one round a section
cycle) is undecided and names that input; it never takes a value a later phase
wrote.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from functools import partial

from nf_metro.layout.constants import SAME_Y_TOLERANCE
from nf_metro.layout.geometry import (
    flow_entry_side,
    flow_port_sides,
    packed_section_visual_order,
    perpendicular_port_sides,
)
from nf_metro.layout.routing.common import (
    ENTRY_SIDE_DIRECTION,
    EXIT_SIDE_DIRECTION,
    Direction,
    resolve_section,
    resolve_section_colrow,
)
from nf_metro.layout.routing.context import _packed_cell_mate_obstructs
from nf_metro.layout.routing.orientation import (
    direction_vector,
    get_point_coordinate,
    lateral_axis,
    lateral_order_sign,
)
from nf_metro.parser.model import MetroGraph, Port, PortSide, Section

_Point = tuple[float, float]
_Cell = tuple[float, float, float, float]
"""``(col0, col1, row0, row1)`` grid extent."""

_BYPASS_SPREAD = 0.25
_U_TURN_SPREAD = 0.75
_REACH_STEP = 0.01


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
    REFINED = "refined"
    PRIORITY = "priority"
    BUNDLE = "bundle"
    """A feeding exit's bundle order overrode the sort key."""


class ApproachKind(Enum):
    """How a source sharing an entry's grid band reaches the entry."""

    LEVEL = "level"
    BYPASS = "bypass"
    U_TURN = "u-turn"


class SideRule(Enum):
    """Which rule decided the side an approach passes on."""

    LEVEL = "level"
    """No side: the connector runs straight along the band."""
    SCREEN_BELOW = "screen-below"
    """The convention: the connector passes below its band on screen."""
    ROW_TOP = "row-top"
    """A packed cell-mate obstructs the hop: the row-top corridor."""
    TURN_ROUND = "turn-round"
    """The source exit heads along the port's axis: the loop turns round
    left-handed, onto the entry traveller's right."""


@dataclass(frozen=True, slots=True)
class Approach:
    """How *source_id* reaches entry *entry_id* along their shared band."""

    entry_id: str
    source_id: str
    kind: ApproachKind
    side: int
    """Travel-frame side the connector passes on: ``+1`` the traveller's
    right, ``-1`` their left, ``0`` level."""
    rule: SideRule
    reach: float
    """Grid distance of the source behind the port along its travel;
    negative ahead of it."""

    @property
    def lateral(self) -> float:
        """The source's effective lateral in the entry's order key."""
        if self.kind is ApproachKind.U_TURN:
            return self.side * (_U_TURN_SPREAD + _REACH_STEP * abs(self.reach))
        if self.kind is ApproachKind.BYPASS:
            return self.side * (_BYPASS_SPREAD + _REACH_STEP * self.reach)
        return 0.0


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
    """Every port's :class:`PortOrder` and every same-band entry approach."""

    orders: Mapping[str, PortOrder]
    approaches: Mapping[tuple[str, str], Approach]
    """Keyed ``(entry port, upstream source)``."""

    def __iter__(self) -> Iterator[PortOrder]:
        return iter(self.orders.values())


def _lateral(point: _Point, origin: _Point, travel: Direction) -> float:
    """Signed distance of *point* from *origin* across *travel*, positive to the
    traveller's right."""
    axis = lateral_axis(travel)
    return lateral_order_sign(travel) * (
        get_point_coordinate(point, axis) - get_point_coordinate(origin, axis)
    )


def _behind(point: _Point, origin: _Point, travel: Direction) -> float:
    """Distance of *point* behind *origin* along *travel*."""
    vx, vy = direction_vector(travel)
    return -((point[0] - origin[0]) * vx + (point[1] - origin[1]) * vy)


def _sign(value: float) -> int:
    return 1 if value > 0 else -1


def port_travel(port: Port) -> Direction:
    sides = ENTRY_SIDE_DIRECTION if port.is_entry else EXIT_SIDE_DIRECTION
    return sides[port.side]


def _across_rows(travel: Direction) -> bool:
    """Whether a lateral across *travel* is measured in grid rows."""
    return lateral_axis(travel) == "y"


def _section_extent(section: Section, rows: bool) -> tuple[float, float]:
    if rows:
        return (section.grid_row, section.grid_row + section.grid_row_span)
    return (section.grid_col, section.grid_col + section.grid_col_span)


def _merge_orders(
    planned: list[str], chains: Sequence[Sequence[str]], lines: Sequence[str]
) -> list[str]:
    """Merge *chains* into one order, *planned* deciding between free lines."""
    pos = {lid: i for i, lid in enumerate(planned)}
    succ: dict[str, set[str]] = {lid: set() for lid in lines}
    indeg = dict.fromkeys(lines, 0)
    for chain in chains:
        for a, b in zip(chain, chain[1:]):
            if b not in succ[a]:
                succ[a].add(b)
                indeg[b] += 1
    out: list[str] = []
    left = set(lines)
    while left:
        ready = [lid for lid in left if indeg[lid] == 0] or [
            min(left, key=pos.__getitem__)
        ]
        lid = min(ready, key=pos.__getitem__)
        left.discard(lid)
        out.append(lid)
        for nxt in succ[lid]:
            indeg[nxt] -= 1
    return out


class PortOrderBuilder:
    """Decides each port once, depth-first through the ports it inherits from."""

    def __init__(self, graph: MetroGraph) -> None:
        self.graph = graph
        self.junctions = graph.junction_ids
        self.priority = {lid: i for i, lid in enumerate(graph.lines)}
        self.decided: dict[str, PortOrder] = {}
        self.in_progress: set[str] = set()
        self.approaches: dict[tuple[str, str], Approach] = {}
        self.approaching: set[tuple[str, str]] = set()

    def point(self, node_id: str) -> _Point:
        station = self.graph.stations[node_id]
        return (station.x, station.y)

    def section_of(self, node_id: str) -> Section | None:
        return self.graph.sections.get(self.graph.stations[node_id].section_id or "")

    # ---- grid cells

    def own_band(self, node_id: str) -> _Cell | None:
        """The one grid band an entry port of a multi-band section lies in."""
        graph = self.graph
        port = graph.ports.get(node_id)
        if port is None or not port.is_entry:
            return None
        section = graph.section_for_port(port)
        if (
            section.grid_col < 0
            or section.grid_row < 0
            or port.side not in flow_port_sides(section.direction)
        ):
            return None
        station = graph.stations[node_id]
        rows = port.side in (PortSide.LEFT, PortSide.RIGHT)
        first, end = _section_extent(section, rows)
        coord = station.y if rows else station.x
        hits = []
        for index in range(int(first), int(end)):
            extents = [
                (other.bbox_y, other.bbox_y + other.bbox_h)
                if rows
                else (other.bbox_x, other.bbox_x + other.bbox_w)
                for other in graph.sections.values()
                if other.id != section.id
                and other.bbox_w > 0
                and other.bbox_h > 0
                and (
                    (other.grid_row, other.grid_row_span)
                    if rows
                    else (other.grid_col, other.grid_col_span)
                )
                == (index, 1)
            ]
            if (
                extents
                and min(lo for lo, _ in extents) - SAME_Y_TOLERANCE
                <= coord
                <= max(hi for _, hi in extents) + SAME_Y_TOLERANCE
            ):
                hits.append(index)
        if len(hits) != 1 or end - first < 2:
            return None
        (index,) = hits
        if rows:
            return (
                section.grid_col,
                section.grid_col + section.grid_col_span,
                index,
                index + 1,
            )
        return (
            index,
            index + 1,
            section.grid_row,
            section.grid_row + section.grid_row_span,
        )

    def section_centre(self, section: Section) -> _Point:
        """Centre of *section*'s grid cell, at its own slot of a packed cell."""
        col = section.grid_col + section.grid_col_span / 2
        row = section.grid_row + section.grid_row_span / 2
        members = self.graph.cell_packs.get((section.grid_col, section.grid_row), ())
        if len(members) < 2:
            return (col, row)
        visual = packed_section_visual_order(self.graph.sections, members)
        if section.id not in visual:
            return (col, row)
        slot = visual.index(section.id)
        return (
            section.grid_col + section.grid_col_span * (slot + 0.5) / len(visual),
            row,
        )

    def cell_centre(self, node_id: str) -> _Point | None:
        """Centre of *node_id*'s grid cell, in (column, row) units."""
        band = self.own_band(node_id)
        if band is not None:
            return ((band[0] + band[1]) / 2, (band[2] + band[3]) / 2)
        section = self.section_of(node_id)
        if section is None or section.grid_col < 0 or section.grid_row < 0:
            return None
        return self.section_centre(section)

    def shared_band(
        self, a: str, b: str, travel: Direction
    ) -> tuple[float, float] | None:
        """The grid band *a*'s and *b*'s sections share across *travel*."""
        sa, sb = self.section_of(a), self.section_of(b)
        if sa is None or sb is None or sa.grid_col < 0 or sb.grid_col < 0:
            return None
        rows = _across_rows(travel)
        spans = []
        for node_id, section in ((a, sa), (b, sb)):
            band = self.own_band(node_id)
            if band is None:
                spans.append(_section_extent(section, rows))
            else:
                spans.append((band[2], band[3]) if rows else (band[0], band[1]))
        lo = max(spans[0][0], spans[1][0])
        hi = min(spans[0][1], spans[1][1])
        return (lo, hi) if hi - lo > SAME_Y_TOLERANCE else None

    def cell_lateral(self, node_id: str, ref_id: str, travel: Direction) -> float:
        """Grid lateral of *node_id* from *ref_id*: zero on a shared band."""
        if self.shared_band(node_id, ref_id, travel) is not None:
            return 0.0
        node, ref = self.cell_centre(node_id), self.cell_centre(ref_id)
        assert node is not None and ref is not None
        return _lateral(node, ref, travel)

    # ---- approach records

    def approach(self, entry_id: str, source_id: str) -> Approach:
        """How *source_id* reaches *entry_id* along their shared band."""
        key = (entry_id, source_id)
        known = self.approaches.get(key)
        if known is not None:
            return known
        travel = port_travel(self.graph.ports[entry_id])
        here = self.cell_centre(entry_id)
        cell = self.cell_centre(source_id)
        assert here is not None and cell is not None
        reach = _behind(cell, here, travel)
        below = _sign(_lateral((0.0, 1.0), (0.0, 0.0), travel) or 1.0)
        self.approaching.add(key)
        if reach < -SAME_Y_TOLERANCE:
            side, rule = self.u_turn_side(entry_id, source_id, below)
            kind = ApproachKind.U_TURN
        elif self.section_between(entry_id, source_id, travel, here, reach):
            side, rule = self.bypass_side(entry_id, source_id, below)
            kind = ApproachKind.BYPASS
        else:
            side, rule, kind = 0, SideRule.LEVEL, ApproachKind.LEVEL
        self.approaching.discard(key)
        record = Approach(entry_id, source_id, kind, side, rule, reach)
        self.approaches[key] = record
        return record

    def section_between(
        self,
        entry_id: str,
        source_id: str,
        travel: Direction,
        here: _Point,
        reach: float,
    ) -> bool:
        """Whether a section on the shared band stands between the two."""
        band = self.shared_band(entry_id, source_id, travel)
        if band is None:
            return False
        lo, hi = sorted((0.0, reach))
        rows = _across_rows(travel)
        for section in self.graph.sections.values():
            if section.bbox_w <= 0 or section.bbox_h <= 0 or section.grid_col < 0:
                continue
            s0, s1 = _section_extent(section, rows)
            if min(s1, band[1]) - max(s0, band[0]) <= SAME_Y_TOLERANCE:
                continue
            along = _behind(self.section_centre(section), here, travel)
            if lo + SAME_Y_TOLERANCE < along < hi - SAME_Y_TOLERANCE:
                return True
        return False

    def u_turn_side(
        self, entry_id: str, source_id: str, below: int
    ) -> tuple[int, SideRule]:
        graph = self.graph
        source = graph.ports.get(source_id)
        if source is None or source.is_entry:
            return below, SideRule.SCREEN_BELOW
        heading = EXIT_SIDE_DIRECTION[source.side]
        if lateral_axis(heading) != lateral_axis(port_travel(graph.ports[entry_id])):
            return below, SideRule.SCREEN_BELOW
        return 1, SideRule.TURN_ROUND

    def bypass_side(
        self, entry_id: str, source_id: str, below: int
    ) -> tuple[int, SideRule]:
        """Dispatch's packed-cell same-row bypass (LEFT entries of LR sections)
        runs the row-top corridor when a packed cell-mate obstructs the hop
        landing on the port, unless the source is a boxed fan-out, the same
        line hands over through a nearer packed sibling whose own hop bypasses,
        or a section from a row above spans down across the hop's columns."""
        graph = self.graph
        port = graph.ports[entry_id]
        target = graph.section_for_port(port)
        source = self.section_of(source_id)
        if (
            port.side is not PortSide.LEFT
            or target.direction != "LR"
            or source is None
            or source.grid_col < 0
            or target.grid_col < 0
            or source.grid_row != target.grid_row
        ):
            return below, SideRule.SCREEN_BELOW
        lines = set(graph.station_lines(entry_id)) & set(graph.station_lines(source_id))
        hops: set[str] = set()
        reach: set[str] = set()
        seen: set[tuple[str, str, str]] = set()
        frontier = [e for e in graph.edges_from(source_id) if e.line_id in lines]
        while frontier:
            edge = frontier.pop()
            if (edge.source, edge.target, edge.line_id) in seen:
                continue
            seen.add((edge.source, edge.target, edge.line_id))
            if edge.target == entry_id:
                hops.add(edge.source)
            if edge.target in self.junctions:
                frontier.extend(
                    e
                    for e in graph.edges_from(edge.target)
                    if e.line_id == edge.line_id
                )
            elif edge.target in graph.ports and graph.ports[edge.target].is_entry:
                reach.add(edge.target)
        if not any(self.hop_obstructed(hop, entry_id) for hop in sorted(hops)):
            return below, SideRule.SCREEN_BELOW
        pack = next(
            (m for m in graph.cell_packs.values() if target.id in m and len(m) >= 2),
            None,
        )
        if pack is not None:
            visual = packed_section_visual_order(graph.sections, pack)
            for sibling_id in sorted(reach):
                sibling = graph.ports[sibling_id]
                if sibling_id == entry_id or sibling.side is not port.side:
                    continue
                home = graph.section_for_port(sibling)
                if (
                    home.id in visual
                    and visual.index(home.id) < visual.index(target.id)
                    and self.cell_centre(sibling_id) is not None
                    and (sibling_id, source_id) not in self.approaching
                    and self.approach(sibling_id, source_id).kind is ApproachKind.BYPASS
                ):
                    return below, SideRule.SCREEN_BELOW
        row = target.grid_row
        c0, c1 = sorted((source.grid_col, target.grid_col))
        for section in graph.sections.values():
            if section.grid_col < 0 or section.id in (target.id, source.id):
                continue
            if (
                section.grid_row < row < section.grid_row + section.grid_row_span
                and section.grid_col < c1 + 1
                and section.grid_col + section.grid_col_span > c0
            ):
                return below, SideRule.SCREEN_BELOW
        return -below, SideRule.ROW_TOP

    def hop_obstructed(self, hop_id: str, entry_id: str) -> bool:
        graph = self.graph
        hop, target = graph.stations[hop_id], graph.stations[entry_id]
        blocks_source, blocks_target = _packed_cell_mate_obstructs(
            graph,
            hop,
            target,
            resolve_section_colrow(graph, hop)[1],
            resolve_section_colrow(graph, target)[1],
        )
        fork = (
            hop_id in self.junctions
            and len({e.target for e in graph.edges_from(hop_id)}) > 1
        )
        home = resolve_section(graph, hop)
        boxed = fork and home is not None and graph.is_packed_section(home.id)
        return (blocks_source or blocks_target) and not (blocks_source and boxed)

    def joining_side(
        self,
        ref: str,
        lateral: float,
        here: _Point,
        travel: Direction,
        kind: ApproachKind | None,
    ) -> float:
        """Between sources tied at one lateral, the one further along the
        travel joins the shared approach later and takes the slot on the side
        it joins from; concentric U-turn loops put the nearer source inner."""
        cell = self.cell_centre(ref)
        if cell is None:
            return 0.0
        if kind is ApproachKind.U_TURN and abs(lateral) > SAME_Y_TOLERANCE:
            return round(-_sign(lateral) * abs(_behind(cell, here, travel)), 3)
        if abs(lateral) <= SAME_Y_TOLERANCE or kind in (
            ApproachKind.LEVEL,
            ApproachKind.U_TURN,
        ):
            return 0.0
        side = _sign(lateral) * (-1 if kind is ApproachKind.BYPASS else 1)
        return round(side * -_behind(cell, here, travel), 3)

    # ---- references

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

    def carried_key(
        self,
        port_id: str,
        feeder: str,
        line_id: str,
        travel: Direction,
        origin: _Point,
        section_id: str,
    ) -> tuple[float, ...]:
        """*line_id*'s walk back from *feeder* inside the section: each
        predecessor's lateral, then the entry port's planned rank."""
        key: list[float] = []
        node, seen = feeder, {port_id}
        graph = self.graph
        while node not in seen:
            seen.add(node)
            key.append(round(_lateral(self.point(node), origin, travel), 1))
            if node in graph.ports and graph.ports[node].is_entry:
                held = self.order_of(node)
                lines = held.lines if held is not None else None
                key.append(lines.index(line_id) if lines and line_id in lines else 0)
                return tuple(key)
            preds = sorted(
                {
                    e.source
                    for e in graph.edges_to(node)
                    if e.line_id == line_id
                    and graph.stations[e.source].section_id == section_id
                }
            )
            if not preds:
                break
            node = min(
                preds,
                key=lambda n: (abs(_lateral(self.point(n), origin, travel)), n),
            )
        return (*key, 0)

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

    def held_rank(self, port_id: str, line_id: str) -> int | None:
        """*line_id*'s rank in *port_id*'s order, ``None`` when undecided."""
        held = self.order_of(port_id)
        if held is None or held.lines is None:
            return None
        return held.lines.index(line_id) if line_id in held.lines else 0

    def _plan_port(self, port_id: str, port: Port) -> PortOrder:
        graph = self.graph
        section = graph.section_for_port(port)
        origin = self.point(port_id)
        perpendicular = port.side in perpendicular_port_sides(section.direction)
        if perpendicular and not port.is_entry:
            travel = ENTRY_SIDE_DIRECTION[flow_entry_side(section.direction)]
        else:
            travel = port_travel(port)
        lines = graph.station_lines(port_id)
        here = self.cell_centre(port_id)
        sources = {lid: self.upstream_sources(port_id, lid) for lid in lines}

        refs: dict[str, str] = {}
        laterals: dict[str, float] = {}
        placed: dict[str, float] = {}
        kinds: dict[str, ApproachKind] = {}
        measured_nodes: dict[str, float] = {}
        via_junction = False
        missing: str | None = None
        for lid in lines:
            if port.is_entry:
                via_junction |= any(via for _node, via in sources[lid])
                nodes = [node for node, _via in sources[lid]]
            else:
                nodes = self.exit_feeders(port_id, lid)
            if not nodes:
                missing = missing or f"reference node of line {lid!r}"
                continue
            if port.is_entry:
                if here is None or any(self.cell_centre(n) is None for n in nodes):
                    missing = missing or f"grid cells at line {lid!r}'s seam"
                    continue
                measured = {n: self.cell_lateral(n, port_id, travel) for n in nodes}
            else:
                measured = {n: _lateral(self.point(n), origin, travel) for n in nodes}
            measured_nodes.update(measured)
            if port.is_entry and len(nodes) > 1:
                share = {
                    n: sum(
                        1
                        for o in lines
                        if o != lid and n in {x for x, _via in sources[o]}
                    )
                    for n in nodes
                }
                ref = min(
                    nodes, key=lambda n: (-share[n], abs(measured[n]), measured[n], n)
                )
            else:
                ref = min(nodes, key=lambda n: (abs(measured[n]), measured[n], n))
            refs[lid] = ref
            laterals[lid] = measured[ref]
            placed[lid] = 0.0
            if port.is_entry and abs(measured[ref]) <= SAME_Y_TOLERANCE:
                approach = self.approach(port_id, ref)
                laterals[lid] = approach.lateral
                kinds[lid] = approach.kind
                placed[lid] = round(_lateral(self.point(ref), origin, travel), 1)
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
        if port.is_entry and perpendicular and len(lines) >= 2:
            self._round_own_side(refs, laterals, kinds, here, travel)
        if len(lines) < 2:
            return make(tuple(lines), OrderBasis.SINGLE)

        rank = _tolerance_ranks(laterals)
        refined: dict[str, tuple[float, ...]] = {}
        for lid in lines:
            tied = [o for o in lines if o != lid and rank[o] == rank[lid]]
            ref = refs[lid]
            if not tied:
                refined[lid] = ()
            elif not port.is_entry:
                refined[lid] = self.carried_key(
                    port_id, ref, lid, travel, origin, port.section_id
                )
            else:
                assert here is not None
                joins = self.joining_side(
                    ref, laterals[lid], here, travel, kinds.get(lid)
                )
                upstream = self.held_rank(ref, lid) if ref in graph.ports else 0
                if upstream is None and all(refs[o] == ref for o in tied):
                    why = (
                        "on a section cycle" if ref in self.in_progress else "undecided"
                    )
                    return make(None, None, f"order of reference port {ref!r} ({why})")
                refined[lid] = (placed[lid], joins, upstream or 0)
        keys = {
            lid: (rank[lid], refined[lid], self.priority.get(lid, 0)) for lid in lines
        }
        keyed = sorted(lines, key=lambda lid: (keys[lid], lid))
        ordered = keyed
        if port.is_entry:
            ordered = self._keep_feeding_bundles(lines, sources, keyed)
        if ordered != keyed:
            return make(tuple(ordered), OrderBasis.BUNDLE)
        return make(tuple(ordered), _basis(ordered, keys))

    def _round_own_side(
        self,
        refs: Mapping[str, str],
        laterals: dict[str, float],
        kinds: Mapping[str, ApproachKind],
        here: _Point | None,
        travel: Direction,
    ) -> None:
        """A perpendicular entry whose off-band sources all lie level with or
        ahead of it along its travel: within each side, nearer outermost."""
        off = {
            lid: refs[lid]
            for lid in laterals
            if abs(laterals[lid]) > SAME_Y_TOLERANCE and lid not in kinds
        }
        if not off or here is None:
            return
        cells = [self.cell_centre(node) for node in off.values()]
        if any(
            cell is None or _behind(cell, here, travel) > SAME_Y_TOLERANCE
            for cell in cells
        ):
            return
        top = 1.0 + max(abs(laterals[lid]) for lid in off)
        for lid in off:
            laterals[lid] = _sign(laterals[lid]) * (top - abs(laterals[lid]))

    def _keep_feeding_bundles(
        self,
        lines: Sequence[str],
        sources: Mapping[str, list[tuple[str, bool]]],
        ordered: list[str],
    ) -> list[str]:
        by_source: dict[str, list[str]] = {}
        for lid in lines:
            for node, _via in sources[lid]:
                by_source.setdefault(node, []).append(lid)
        chains = []
        for node, carried in sorted(by_source.items()):
            if node not in self.graph.ports or len(carried) < 2:
                continue
            held = self.order_of(node)
            if held is not None and held.lines is not None:
                chains.append([lid for lid in held.lines if lid in carried])
        return _merge_orders(ordered, chains, lines) if chains else ordered


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
    ordered: Sequence[str], keys: Mapping[str, tuple[int, tuple[float, ...], int]]
) -> OrderBasis:
    weakest = 0
    for a, b in zip(ordered, ordered[1:]):
        field_index = next(
            (i for i, (x, y) in enumerate(zip(keys[a], keys[b])) if x != y), 2
        )
        weakest = max(weakest, field_index)
    return (OrderBasis.LATERAL, OrderBasis.REFINED, OrderBasis.PRIORITY)[weakest]


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


def _same_band_sources(builder: PortOrderBuilder) -> list[tuple[str, str]]:
    """Every ``(entry port, upstream source)`` pair on one grid band."""
    graph = builder.graph
    pairs: set[tuple[str, str]] = set()
    for port_id in sorted(graph.ports):
        if not graph.ports[port_id].is_entry or builder.cell_centre(port_id) is None:
            continue
        travel = port_travel(graph.ports[port_id])
        for lid in graph.station_lines(port_id):
            for node, _via in builder.upstream_sources(port_id, lid):
                if (
                    builder.cell_centre(node) is not None
                    and abs(builder.cell_lateral(node, port_id, travel))
                    <= SAME_Y_TOLERANCE
                ):
                    pairs.add((port_id, node))
    return sorted(pairs)


def plan_builder(graph: MetroGraph) -> tuple[PortOrderBuilder, PortOrderPlan]:
    """Decide every port's order and every same-band approach, keeping the
    builder whose band and cell reads later record rules share."""
    builder = PortOrderBuilder(graph)
    for port_id in sorted(graph.ports):
        builder.order_of(port_id)
    approaches = {key: builder.approach(*key) for key in _same_band_sources(builder)}
    plan = PortOrderPlan(
        {pid: builder.decided[pid] for pid in sorted(graph.ports)}, approaches
    )
    return builder, plan


def build_port_order_plan(graph: MetroGraph) -> PortOrderPlan:
    """Decide every port's line order from *graph*'s structure and placement."""
    return plan_builder(graph)[1]
