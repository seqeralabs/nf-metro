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
reflected map plans each lateral-decided order reversed and each corridor side
flipped.

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
across the port's travel passes on the side the exit faces, so it never crosses
the target section.  One whose exit heads along or against the travel turns
round left-handed, so its loop lies on the entry traveller's right, unless a
section on the grid stands between the ends of every edge of its connector.
Such a bypass U-turn takes a corridor, ranked in the travel frame from the
placed boxes: only a side whose corridor crosses no other section and no
other line into the entry is a candidate; among the candidates the side the
entry's other same-band U-turns pass on wins (one bundle), then the shorter
detour, then the side whose corridor runs in free room the layout already has
rather than at the edge of the map, then the turn-round side.  Room is not free
where another line of the hop's fork or of the target section ends beyond the
traverse.  Detour and room read each box's lateral faces no further than the
section padding beyond its content, so the bundle clearance later sizers add for
the planned lanes cannot change the ranking.  Bypass U-turns sharing
a line and travelling opposite ways over overlapping spans of one band are
decided together, so no two of them take one screen side; a U-turn outside that
ranked set constrains none of them.
A direct turn-round whose two sides are clear and equally long takes the side
with free room.  Any other U-turn keeps the screen-below side.

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

**Exit order**: the exit's lines in the lanes its section's track plan gives
them at the port, read across the exit's travel.  The builder asks the track
plan for them (:data:`ExitOrder`); the track plan in turn reads its section's
entry orders from the builder, so sections are decided upstream first.

**Seam pins**: an entry some of whose lines cross a straight seam takes the
order of the lanes the track plan keeps them on there, the entry's other
lines filled round them in the order above (:data:`SeamOrder`).

A port whose order needs an input the plan cannot supply (a reference node for
one of an entry's lines, the grid cells either side of an entry's seam, a
decided order for a reference port it inherits from, or an exit's lanes,
including either round a section cycle) is undecided and names that input; it
never takes a value a later phase wrote.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from functools import partial
from itertools import combinations, product
from types import MappingProxyType
from typing import Literal

from nf_metro.layout.constants import (
    COORD_TOLERANCE,
    CURVE_RADIUS,
    INTER_ROW_HEADER_CLEARANCE,
    SAME_Y_TOLERANCE,
    SECTION_ROUTE_CLEARANCE,
    SECTION_X_PADDING,
    SECTION_Y_PADDING,
    graph_offset_step,
)
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
    resolve_section_colrow,
    right_normal_axis_sign,
)
from nf_metro.layout.routing.context import (
    BypassNeed,
    _intervening_section_obstructs,
    _packed_cell_mate_obstructs,
    boxed_fanout_junction,
    packed_cell_same_row_bypass,
)
from nf_metro.layout.routing.orientation import (
    direction_vector,
    get_point_coordinate,
    lateral_axis,
)
from nf_metro.parser.model import Edge, MetroGraph, Port, PortSide, Section

_Point = tuple[float, float]
_Cell = tuple[float, float, float, float]
"""``(col0, col1, row0, row1)`` grid extent."""
_Span = tuple[float, float]
_Box = tuple[float, float, float, float]
"""``(along0, along1, lateral0, lateral1)`` in a travel frame."""
_ORIGIN: _Point = (0.0, 0.0)

_BYPASS_SPREAD = 0.25
_PARTING = 0.1
"""Lateral within which an entry's consumer counts as level with it."""
_U_TURN_SPREAD = 0.75
_REACH_STEP = 0.01
_CORRIDOR_CLEAR = INTER_ROW_HEADER_CLEARANCE + CURVE_RADIUS
"""How far a U-turn's traverse runs past the extreme of the band it rounds."""


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
    """The loop turns round: from an exit heading along the port's axis,
    left-handed onto the entry traveller's right; from an exit heading across
    it, on the side the exit faces, never through the target section."""
    CORRIDOR = "corridor"
    """The side the corridor ranking (module docstring, **Approach**) gives a
    bypass U-turn, or the side with free room a direct turn-round takes when
    its two clear sides are equally long; read in the entry's travel frame, so
    a reflection flips it."""
    FORK_NEST = "fork-nest"
    """A fork's branch off an exit heading across the entry's travel, holding the
    outermost lanes of its turn there, passes on the side the exit faces; a
    branch inside other lanes passes on the other side, crossing none of them."""


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
class _Corridor:
    """One side's corridor round a bypass U-turn's band, in the travel frame."""

    detour: float
    """How far the loop runs out beyond the two ends and back."""
    feasible: bool
    """Whether its lead, traverse and descent legs cross no section but the
    hop's two ends, and no other line into the entry starts beyond the
    traverse, where it would have to cross the loop to land."""
    room: bool
    """Whether the corridor runs in free room the layout already has rather
    than at the edge of the map: a section lies beyond the band on this side
    within the hop's span, and no other line of the hop's fork or of the
    target section ends beyond the traverse."""


@dataclass(frozen=True, slots=True)
class _CorridorHop:
    """A bypass U-turn (:meth:`PortOrderBuilder.corridor_scope`) and its two
    corridors."""

    key: tuple[str, str]
    travel: Direction
    right: _Corridor
    left: _Corridor
    span: _Span
    """Extent along the travel between the source and the entry."""
    band: _Span
    lines: frozenset[str]
    held: tuple[int, SideRule] | None
    """The side and rule a bundle or a single clear corridor fixes."""

    def corridor(self, side: int) -> _Corridor:
        return self.right if side == 1 else self.left


@dataclass(frozen=True, slots=True)
class PortOrder:
    """One port's planned line order, or the input an undecided order lacks."""

    port_id: str
    is_entry: bool
    lines: tuple[str, ...] | None
    """Travel-left to travel-right; ``None`` when undecided."""
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "orders", MappingProxyType(dict(self.orders)))
        object.__setattr__(self, "approaches", MappingProxyType(dict(self.approaches)))

    def __iter__(self) -> Iterator[PortOrder]:
        return iter(self.orders.values())

    def __deepcopy__(self, memo: dict[int, object]) -> PortOrderPlan:
        del memo
        return self


def _lateral(point: _Point, origin: _Point, travel: Direction) -> float:
    """Signed distance of *point* from *origin* across *travel*, positive to the
    traveller's right."""
    axis = lateral_axis(travel)
    return right_normal_axis_sign(travel) * (
        get_point_coordinate(point, axis) - get_point_coordinate(origin, axis)
    )


def _behind(point: _Point, origin: _Point, travel: Direction) -> float:
    """Distance of *point* behind *origin* along *travel*."""
    vx, vy = direction_vector(travel)
    return -((point[0] - origin[0]) * vx + (point[1] - origin[1]) * vy)


def _sign(value: float) -> int:
    return 1 if value > 0 else -1


def _along(point: _Point, travel: Direction) -> float:
    return -_behind(point, _ORIGIN, travel)


def _box(section: Section, travel: Direction) -> _Box:
    """*section*'s box as ``(along0, along1, lateral0, lateral1)`` in *travel*'s
    frame."""
    corners = [
        (x, y)
        for x in (section.bbox_x, section.bbox_x + section.bbox_w)
        for y in (section.bbox_y, section.bbox_y + section.bbox_h)
    ]
    along = [_along(c, travel) for c in corners]
    lateral = [_lateral(c, _ORIGIN, travel) for c in corners]
    return (min(along), max(along), min(lateral), max(lateral))


def _face_span(graph: MetroGraph, section: Section, travel: Direction) -> _Span:
    """*section*'s lateral span across *travel*, each face read no further than
    the section padding beyond its content: its stations and its ports on the
    faces *travel* crosses.  Ports on the lateral faces are left out, since they
    sit on the faces the bundle sizers move; with no content, the box."""
    lateral = lateral_axis(travel)
    content = [
        _lateral((station.x, station.y), _ORIGIN, travel)
        for node_id, station in graph.stations.items()
        if station.section_id == section.id
        and (
            (port := graph.ports.get(node_id)) is None
            or lateral_axis(ENTRY_SIDE_DIRECTION[port.side]) == lateral
        )
    ]
    _a0, _a1, lo, hi = _box(section, travel)
    if not content:
        return lo, hi
    pad = SECTION_Y_PADDING if lateral == "y" else SECTION_X_PADDING
    return max(lo, min(content) - pad), min(hi, max(content) + pad)


def _overlap(a: _Span, b: _Span) -> float:
    return min(a[1], b[1]) - max(a[0], b[0])


def _legs_cross(
    boxes: Sequence[_Box],
    lead: float,
    descent: float,
    start: float,
    end: float,
    channel: float,
) -> bool:
    """Whether a loop leaving lateral *start* at along *lead*, traversing at
    lateral *channel* and descending at along *descent* to lateral *end*
    crosses one of *boxes*."""
    a0, a1 = sorted((lead, descent))
    for b0, b1, c0, c1 in boxes:
        if a0 < b1 and b0 < a1 and c0 < channel < c1:
            return True
        for along, lateral in ((lead, start), (descent, end)):
            m0, m1 = sorted((lateral, channel))
            if b0 < along < b1 and m0 < c1 and c0 < m1:
                return True
    return False


def _screen_side(side: int, travel: Direction) -> int:
    return side * right_normal_axis_sign(travel)


def _runs_opposite(a: _CorridorHop, b: _CorridorHop) -> bool:
    """Whether two bypass U-turns share a line and travel opposite ways over
    overlapping spans of one band."""
    va, vb = direction_vector(a.travel), direction_vector(b.travel)
    return (
        va == (-vb[0], -vb[1])
        and bool(a.lines & b.lines)
        and _overlap(a.span, (-b.span[1], -b.span[0])) > SAME_Y_TOLERANCE
        and _overlap(a.band, b.band) > SAME_Y_TOLERANCE
    )


def approach_source(graph: MetroGraph, node: str, line_id: str) -> str:
    """The node an approach record keys *line_id*'s connector from *node*
    by: up through junctions each fed by one edge of the line."""
    seen: set[str] = set()
    while node in graph.junction_ids and node not in seen:
        seen.add(node)
        ups = [e.source for e in graph.edges_to(node) if e.line_id == line_id]
        if len(ups) != 1:
            return node
        node = ups[0]
    return node


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


def merge_orders(
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


ExitOrder = Callable[[str], "tuple[str, ...] | None"]
"""An exit's lines in its section's planned lanes, travel-left to travel-right;
``None`` when the section's lanes are undecided."""

SeamOrder = Callable[[str, "tuple[str, ...] | None"], "tuple[str, ...] | None"]
"""Given an entry and its order by the sort key (``None`` when undecided), the
entry's lines in the lanes its straight seams pin, travel-left to
travel-right; ``None`` when no line is pinned."""


class PortOrderBuilder:
    """Decides each port once, depth-first through the ports it inherits from."""

    def __init__(
        self, graph: MetroGraph, exit_order: ExitOrder, seam_order: SeamOrder
    ) -> None:
        self.graph = graph
        self.exit_order = exit_order
        self.seam_order = seam_order
        self.junctions = graph.junction_ids
        self.priority = {lid: i for i, lid in enumerate(graph.lines)}
        self.decided: dict[str, PortOrder] = {}
        self.in_progress: set[str] = set()
        self.approaches: dict[tuple[str, str], Approach] = {}
        self.approaching: set[tuple[str, str]] = set()
        self.corridor_hops: dict[tuple[str, str], _CorridorHop | None] = {}
        self.ranked_hops: dict[tuple[str, str], _CorridorHop] | None = None
        self.corridor_sides: dict[tuple[str, str], tuple[int, SideRule]] = {}

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
        """How *source_id* reaches *entry_id* along their shared band.  An
        exit's is read as if its lines arrived through its side: how the exit
        sees a downstream node it shares a band with."""
        key = (entry_id, source_id)
        known = self.approaches.get(key)
        if known is not None:
            return known
        travel = ENTRY_SIDE_DIRECTION[self.graph.ports[entry_id].side]
        here = self.cell_centre(entry_id)
        cell = self.cell_centre(source_id)
        assert here is not None and cell is not None
        reach = _behind(cell, here, travel)
        below = _sign(_lateral((0.0, 1.0), _ORIGIN, travel) or 1.0)
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
            if _overlap((s0, s1), band) <= SAME_Y_TOLERANCE:
                continue
            along = _behind(self.section_centre(section), here, travel)
            if lo + SAME_Y_TOLERANCE < along < hi - SAME_Y_TOLERANCE:
                return True
        return False

    def u_turn_side(
        self, entry_id: str, source_id: str, below: int
    ) -> tuple[int, SideRule]:
        """A direct turn-round keeps its turn-round side unless both its sides
        are clear and equally long, when it takes the side with free room
        (:attr:`SideRule.CORRIDOR`); a bypass U-turn takes its corridor's."""
        graph = self.graph
        source = graph.ports.get(source_id)
        if source is None or source.is_entry:
            return below, SideRule.SCREEN_BELOW
        heading = EXIT_SIDE_DIRECTION[source.side]
        entry = graph.ports[entry_id]
        if lateral_axis(heading) != lateral_axis(port_travel(entry)):
            travel = ENTRY_SIDE_DIRECTION[entry.side]
            facing = _lateral(direction_vector(heading), _ORIGIN, travel)
            return _sign(facing), SideRule.TURN_ROUND
        key = (entry_id, source_id)
        if self.corridor_hop(key) is not None:
            if key not in self.corridor_sides:
                self.decide_corridors(self.opposite_group(key))
            return self.corridor_sides[key]
        direct = self.same_axis_u_turn(entry_id, source_id)
        if direct is not None:
            right, left, _span, _band = self.side_corridors(entry_id, source_id, direct)
            if (
                right.feasible
                and left.feasible
                and abs(right.detour - left.detour) <= SAME_Y_TOLERANCE
                and right.room != left.room
            ):
                return (1 if right.room else -1), SideRule.CORRIDOR
        return 1, SideRule.TURN_ROUND

    # ---- bypass U-turn corridors

    def same_axis_u_turn(self, entry_id: str, source_id: str) -> Direction | None:
        """The entry's travel when *source_id* is an exit heading along or
        against it from ahead of the port on a shared band, else ``None``."""
        graph = self.graph
        source, entry = graph.ports.get(source_id), graph.ports.get(entry_id)
        if source is None or source.is_entry or entry is None or not entry.is_entry:
            return None
        travel = ENTRY_SIDE_DIRECTION[entry.side]
        if lateral_axis(EXIT_SIDE_DIRECTION[source.side]) != lateral_axis(travel):
            return None
        return travel if self.ahead_on_band(entry_id, source_id, travel) else None

    def ahead_on_band(self, entry_id: str, origin: str, travel: Direction) -> bool:
        """Whether *origin*'s cell lies ahead of entry *entry_id*'s along
        *travel* on a grid band the two share."""
        here, cell = self.cell_centre(entry_id), self.cell_centre(origin)
        return (
            here is not None
            and cell is not None
            and self.shared_band(entry_id, origin, travel) is not None
            and _behind(cell, here, travel) < -SAME_Y_TOLERANCE
        )

    def connector(self, entry_id: str, source_id: str) -> list[Edge]:
        """The edges into *entry_id* whose approach record keys from *source_id*."""
        graph = self.graph
        return [
            e
            for e in graph.edges_to(entry_id)
            if approach_source(graph, e.source, e.line_id) == source_id
        ]

    def corridor_scope(self, entry_id: str, source_id: str) -> Direction | None:
        """The travel of a same-axis U-turn every edge of whose connector has a
        section standing between its ends on the grid, read along the travel
        by dispatch's own intervening-section test; else ``None``."""
        travel = self.same_axis_u_turn(entry_id, source_id)
        edges = self.connector(entry_id, source_id) if travel is not None else []
        if travel is None or not edges:
            return None
        graph = self.graph
        axis: Literal["x", "y"] = "x" if _across_rows(travel) else "y"
        for edge in edges:
            sc, sr = resolve_section_colrow(graph, graph.stations[edge.source])
            tc, tr = resolve_section_colrow(graph, graph.stations[edge.target])
            if (
                sc is None
                or tc is None
                or not _intervening_section_obstructs(graph, sc, sr, tc, tr, axis)
            ):
                return None
        return travel

    def side_corridors(
        self, entry_id: str, source_id: str, travel: Direction
    ) -> tuple[_Corridor, _Corridor, _Span, _Span]:
        """The U-turn's right and left corridors (:class:`_Corridor`), its span
        along *travel* and its shared band, from the placed boxes.

        Each corridor leads out of the source just past its exit, traverses
        clear of the band's extreme on its side and descends just outside the
        entry, about where the wrap legs run.  The band's extreme is the
        outermost of the two ends, the target box and every gridded section on
        the band overlapping the span.

        Another feeder of the entry starting beyond a side's traverse closes
        that side: it could reach the port only by crossing the loop.  A line
        of the hop's fork (the connector's source nodes) or of the target
        section (an edge into or out of its ports) ending beyond it only
        contests the side's room: whether it crosses the loop depends on the
        lane order at the fork, which the record itself decides.

        The detour and the room read each box's lateral faces no further than
        the section padding beyond its content (:func:`_face_span`), so the
        bundle clearance the sizers add later for the record's own lanes cannot
        change them; the crossing tests read the boxes as placed."""
        graph = self.graph
        band = self.shared_band(entry_id, source_id, travel)
        assert band is not None
        rows = _across_rows(travel)
        source_box = self.section_of(source_id)
        target_box = graph.section_for_port(graph.ports[entry_id])
        start, end = self.point(source_id), self.point(entry_id)
        a_s, a_e = _along(start, travel), _along(end, travel)
        l_s, l_e = _lateral(start, _ORIGIN, travel), _lateral(end, _ORIGIN, travel)
        span = (min(a_s, a_e), max(a_s, a_e))
        step = graph_offset_step(graph)
        target_along = _box(target_box, travel)[:2]
        descent_off = CURVE_RADIUS + step + SECTION_ROUTE_CLEARANCE
        descent = (
            a_e - descent_off
            if abs(a_e - target_along[0]) < abs(a_e - target_along[1])
            else a_e + descent_off
        )
        lead = a_s + (CURVE_RADIUS + step) * (1 if descent > a_s else -1)
        lows, highs = [l_s, l_e], [l_s, l_e]
        face_lows, face_highs = [l_s, l_e], [l_s, l_e]
        others: list[_Box] = []
        beyond: list[_Span] = []
        for section in graph.sections.values():
            if section.bbox_w <= 0 or section.bbox_h <= 0:
                continue
            box = _box(section, travel)
            if section is target_box:
                on_band = True
            elif section is source_box:
                continue
            else:
                others.append(box)
                if _overlap(box[:2], span) <= SAME_Y_TOLERANCE:
                    continue
                s0, s1 = _section_extent(section, rows)
                on_band = (
                    section.grid_col >= 0
                    and _overlap((s0, s1), band) > SAME_Y_TOLERANCE
                )
            face = _face_span(graph, section, travel)
            if section is not target_box:
                beyond.append(face)
            if on_band:
                lows.append(box[2])
                highs.append(box[3])
                face_lows.append(face[0])
                face_highs.append(face[1])
        right_channel = max(highs) + _CORRIDOR_CLEAR
        left_channel = min(lows) - _CORRIDOR_CLEAR
        face_high, face_low = max(face_highs), min(face_lows)
        connector = self.connector(entry_id, source_id)
        hop = {(e.source, e.line_id) for e in connector}
        feeders = [
            _lateral(self.point(e.source), _ORIGIN, travel)
            for e in graph.edges_to(entry_id)
            if (e.source, e.line_id) not in hop
        ]
        contested = self.contested_ends(connector, target_box, travel) if beyond else []
        right = _Corridor(
            2 * face_high - l_s - l_e,
            not _legs_cross(others, lead, descent, l_s, l_e, right_channel)
            and all(lateral <= right_channel for lateral in feeders),
            any(lo >= face_high - COORD_TOLERANCE for lo, _hi in beyond)
            and all(lateral <= right_channel for lateral in contested),
        )
        left = _Corridor(
            l_s + l_e - 2 * face_low,
            not _legs_cross(others, lead, descent, l_s, l_e, left_channel)
            and all(lateral >= left_channel for lateral in feeders),
            any(hi <= face_low + COORD_TOLERANCE for _lo, hi in beyond)
            and all(lateral >= left_channel for lateral in contested),
        )
        return right, left, span, band

    def contested_ends(
        self, connector: Sequence[Edge], target: Section, travel: Direction
    ) -> list[float]:
        """Laterals across *travel* of the ends outside *target* of every edge
        but *connector*'s that leaves one of *connector*'s source nodes or
        enters or leaves one of *target*'s ports."""
        graph = self.graph
        hop = {(e.source, e.target, e.line_id) for e in connector}
        ports = sorted(
            p for p, port in graph.ports.items() if port.section_id == target.id
        )
        touching = [
            *(e for p in ports for e in graph.edges_to(p)),
            *(e for p in ports for e in graph.edges_from(p)),
            *(
                e
                for n in sorted({e.source for e in connector})
                for e in graph.edges_from(n)
            ),
        ]
        return [
            _lateral(self.point(node), _ORIGIN, travel)
            for e in touching
            if (e.source, e.target, e.line_id) not in hop
            for node in (e.source, e.target)
            if graph.stations[node].section_id != target.id
        ]

    def forced_side(
        self, entry_id: str, source_id: str, travel: Direction
    ) -> tuple[int, SideRule] | None:
        """The one side the entry's other same-band U-turns from exits pass
        on, which a bypass U-turn joining their bundle is held to; ``None``
        when they pass on both sides or there are none.  A sibling exit heading
        across the travel holds it to the side that exit faces."""
        graph = self.graph
        held: set[tuple[int, SideRule]] = set()
        for edge in graph.edges_to(entry_id):
            origin = approach_source(graph, edge.source, edge.line_id)
            port = graph.ports.get(origin)
            if (
                origin == source_id
                or port is None
                or port.is_entry
                or self.corridor_scope(entry_id, origin) is not None
            ):
                continue
            if not self.ahead_on_band(entry_id, origin, travel):
                continue
            side, rule = self.u_turn_side(entry_id, origin, 1)
            heading = EXIT_SIDE_DIRECTION[port.side]
            if lateral_axis(heading) != lateral_axis(travel):
                rule = SideRule.CORRIDOR
            held.add((side, rule))
        if len({side for side, _rule in held}) != 1:
            return None
        return min(held, key=lambda item: item[1].value)

    def corridor_hop(self, key: tuple[str, str]) -> _CorridorHop | None:
        """*key*'s bypass U-turn with its corridors and the side it is held
        to, if any; ``None`` outside :meth:`corridor_scope`."""
        if key in self.corridor_hops:
            return self.corridor_hops[key]
        travel = self.corridor_scope(*key)
        hop = None
        if travel is not None:
            right, left, span, band = self.side_corridors(*key, travel)
            held = None
            forced = self.forced_side(*key, travel)
            if forced is not None and (right if forced[0] == 1 else left).feasible:
                held = forced
            elif right.feasible != left.feasible:
                held = (1 if right.feasible else -1), SideRule.CORRIDOR
            elif not right.feasible:
                held = 1, SideRule.TURN_ROUND
            lines = frozenset(e.line_id for e in self.connector(*key))
            hop = _CorridorHop(key, travel, right, left, span, band, lines, held)
        self.corridor_hops[key] = hop
        return hop

    def opposite_group(self, key: tuple[str, str]) -> list[_CorridorHop]:
        """*key*'s hop and every bypass U-turn linked to it, transitively, by
        :func:`_runs_opposite`, in key order."""
        if self.ranked_hops is None:
            graph = self.graph
            self.ranked_hops = {
                (port_id, origin): hop
                for port_id in sorted(graph.ports)
                if graph.ports[port_id].is_entry
                for origin in sorted(
                    {
                        approach_source(graph, e.source, e.line_id)
                        for e in graph.edges_to(port_id)
                    }
                )
                if (hop := self.corridor_hop((port_id, origin))) is not None
            }
        hops = self.ranked_hops
        group, frontier = {key}, [key]
        while frontier:
            here = hops[frontier.pop()]
            for other, there in hops.items():
                if other not in group and _runs_opposite(here, there):
                    group.add(other)
                    frontier.append(other)
        return [hops[k] for k in sorted(group)]

    def decide_corridors(self, group: Sequence[_CorridorHop]) -> None:
        """Decide every hop of an :meth:`opposite_group` at once: a held hop
        keeps its side, and the free hops take the module docstring's ranking
        summed over the group (total detour, then corridors in free room, then
        turn-round sides) among the assignments putting no two linked hops on
        one screen side and each free hop on a clear side; with none, every hop
        keeps the turn-round side."""
        best: tuple[tuple[float, int, tuple[int, ...]], tuple[int, ...]] | None = None
        choices = [[hop.held[0]] if hop.held else [1, -1] for hop in group]
        for combo in product(*choices):
            pairs = zip(group, combo)
            if any(
                _runs_opposite(a, b)
                and _screen_side(sa, a.travel) == _screen_side(sb, b.travel)
                for (a, sa), (b, sb) in combinations(pairs, 2)
            ) or any(
                hop.held is None and not hop.corridor(side).feasible
                for hop, side in zip(group, combo)
            ):
                continue
            rank = (
                round(sum(h.corridor(s).detour for h, s in zip(group, combo)), 6),
                sum(not h.corridor(s).room for h, s in zip(group, combo)),
                tuple(int(side != 1) for side in combo),
            )
            if best is None or rank < best[0]:
                best = (rank, combo)
        if best is None:
            for hop in group:
                self.corridor_sides[hop.key] = (1, SideRule.TURN_ROUND)
            return
        for hop, side in zip(group, best[1]):
            if hop.held is not None:
                self.corridor_sides[hop.key] = (side, hop.held[1])
                continue
            tie = (
                len(group) == 1
                and abs(hop.right.detour - hop.left.detour) <= SAME_Y_TOLERANCE
                and hop.right.room == hop.left.room
            )
            rule = SideRule.TURN_ROUND if tie and side == 1 else SideRule.CORRIDOR
            self.corridor_sides[hop.key] = (side, rule)

    def bypass_side(
        self, entry_id: str, source_id: str, below: int
    ) -> tuple[int, SideRule]:
        """Dispatch's packed-cell same-row bypass (LEFT entries of LR sections)
        runs the row-top corridor when a packed cell-mate obstructs the hop
        landing on the port, unless the source is a boxed fan-out, the same
        line hands over through a nearer packed sibling whose own hop bypasses,
        or a section from a row above spans down across the hop's columns."""
        fork = self.fork_branch_side(entry_id, source_id)
        if fork is not None:
            return fork, SideRule.FORK_NEST
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

    def fork_branch_side(self, entry_id: str, source_id: str) -> int | None:
        """:attr:`SideRule.FORK_NEST`'s side, or ``None`` where *source_id* is no
        exit into a fork heading across the entry's travel, or its lanes are
        undecided."""
        source = self.graph.ports.get(source_id)
        if source is None or source.is_entry or not self.feeds_junction(source_id):
            return None
        heading = EXIT_SIDE_DIRECTION[source.side]
        travel = port_travel(self.graph.ports[entry_id])
        turn = _lateral(direction_vector(travel), _ORIGIN, heading)
        lines = self.exit_order(source_id) if turn else None
        if lines is None:
            return None
        mine = [
            i
            for i, lid in enumerate(lines)
            if (source_id, True) in self.upstream_sources(entry_id, lid)
        ]
        inner = min(mine) > 0 if turn > 0 else max(mine) < len(lines) - 1
        facing = _sign(_lateral(direction_vector(heading), _ORIGIN, travel))
        return -facing if inner else facing

    def hop_obstructed(self, hop_id: str, entry_id: str) -> bool:
        """Whether dispatch takes the hop into *entry_id* as a packed-cell
        same-row bypass (:func:`packed_cell_same_row_bypass`)."""
        graph = self.graph
        hop, target = graph.stations[hop_id], graph.stations[entry_id]
        rows = (
            resolve_section_colrow(graph, hop)[1],
            resolve_section_colrow(graph, target)[1],
        )
        blocks_source, blocks_target = _packed_cell_mate_obstructs(
            graph, hop, target, *rows
        )
        return packed_cell_same_row_bypass(
            graph.ports[entry_id].side,
            BypassNeed(blocks_source or blocks_target, blocks_source, blocks_target),
            rows,
            boxed_fanout_junction(graph, hop_id, graph.is_fanout_junction(hop_id)),
        )

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

    def order_of(self, port_id: str) -> PortOrder | None:
        """*port_id*'s order, or ``None`` while it is being decided (a cycle)."""
        if port_id in self.decided:
            return self.decided[port_id]
        if port_id in self.in_progress:
            return None
        self.in_progress.add(port_id)
        order = self._plan_port(port_id, self.graph.ports[port_id])
        if order.is_entry:
            pinned = self.seam_order(port_id, order.lines)
            if pinned is not None:
                order = PortOrder(port_id, True, pinned)
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
        missing: str | None = None
        for lid in lines:
            if port.is_entry:
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
        make = partial(PortOrder, port_id, port.is_entry)

        if not port.is_entry:
            exit_lines = self.exit_order(port_id)
            if exit_lines is None:
                return make(None, "its section's lanes (on a section cycle)")
            return make(exit_lines)
        if missing is not None:
            return make(None, missing)
        if perpendicular and len(lines) >= 2:
            self._round_own_side(refs, laterals, kinds, here, travel)
        if len(lines) < 2:
            return make(tuple(lines))

        rank = _tolerance_ranks(laterals)
        refined: dict[str, tuple[float, ...]] = {}
        for lid in lines:
            tied = [o for o in lines if o != lid and rank[o] == rank[lid]]
            ref = refs[lid]
            if not tied:
                refined[lid] = ()
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
                    return make(None, f"order of reference port {ref!r} ({why})")
                refined[lid] = (placed[lid], joins, upstream or 0)
        keys = {
            lid: (rank[lid], refined[lid], self.priority.get(lid, 0)) for lid in lines
        }
        keyed = sorted(lines, key=lambda lid: (keys[lid], lid))
        ordered, bundled = self._keep_feeding_bundles(lines, sources, keyed)
        if not perpendicular:
            parted = self._depart(port_id, ordered, bundled, laterals, kinds)
            if parted != ordered:
                return make(tuple(parted))
        return make(tuple(ordered))

    def _depart(
        self,
        entry_id: str,
        ordered: list[str],
        bundled: set[str],
        laterals: Mapping[str, float],
        kinds: Mapping[str, ApproachKind],
    ) -> list[str]:
        """*ordered* with each line that parts at the entry for one side, and
        arrives there by a same-band bypass on that side in no feeding bundle
        of the entry's lines, outermost on that side, nearer consumers inside,
        where the entry's other lines include some running on level."""
        graph = self.graph
        port = graph.ports[entry_id]
        origin, travel = self.point(entry_id), port_travel(port)
        sides: dict[str, float] = {}
        for lid in ordered:
            laterals_out = [
                _lateral(self.point(e.target), origin, travel)
                for e in graph.edges_from(entry_id)
                if e.line_id == lid
                and graph.stations[e.target].section_id == port.section_id
            ]
            if not laterals_out:
                return ordered
            if all(abs(v) <= _PARTING for v in laterals_out):
                sides[lid] = 0.0
            elif all(v > _PARTING for v in laterals_out):
                sides[lid] = min(laterals_out)
            elif all(v < -_PARTING for v in laterals_out):
                sides[lid] = max(laterals_out)
            else:
                return ordered
        if 0.0 not in sides.values() or set(sides.values()) == {0.0}:
            return ordered
        movers = [
            lid
            for lid in ordered
            if sides[lid] != 0.0
            and lid not in bundled
            and kinds.get(lid) is ApproachKind.BYPASS
            and (laterals[lid] > 0) == (sides[lid] > 0)
        ]
        left = sorted(
            (lid for lid in movers if sides[lid] < 0),
            key=lambda lid: (sides[lid], ordered.index(lid)),
        )
        right = sorted(
            (lid for lid in movers if sides[lid] > 0),
            key=lambda lid: (-sides[lid], ordered.index(lid)),
        )[::-1]
        return left + [lid for lid in ordered if lid not in movers] + right

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
    ) -> tuple[list[str], set[str]]:
        """*ordered* keeping every feeding exit's bundle order, and the lines
        those bundles carry."""
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
        bundled = {lid for chain in chains for lid in chain}
        if not chains:
            return ordered, bundled
        return merge_orders(ordered, chains, lines), bundled


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


def decide_ports(builder: PortOrderBuilder) -> PortOrderPlan:
    """Decide every port's order and every same-band approach."""
    graph = builder.graph
    for port_id in sorted(graph.ports):
        builder.order_of(port_id)
    approaches = {key: builder.approach(*key) for key in _same_band_sources(builder)}
    return PortOrderPlan(
        {pid: builder.decided[pid] for pid in sorted(graph.ports)}, approaches
    )
