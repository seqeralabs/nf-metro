"""Shared types and helper functions for edge routing."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import AbstractSet, NamedTuple

from nf_metro.layout.constants import (
    BUNDLE_TO_BUNDLE_CLEARANCE,
    BYPASS_CLEARANCE,
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    CURVE_RADIUS,
    DEFAULT_LINE_PRIORITY,
    EDGE_TO_BUNDLE_CLEARANCE,
    HEADER_CLEARANCE,
    INTER_ROW_EDGE_CLEARANCE,
    INTER_ROW_HEADER_CLEARANCE,
    MIN_CORRIDOR_Y_OVERLAP,
    OFFSET_STEP,
    SECTION_HEADER_PROTRUSION,
    graph_offset_step,
)
from nf_metro.layout.geometry import (
    AxisFrame,
    cotravelling_lane_clearance,
    cotravelling_lanes_fuse,
    lanes_run_along_x,
    lanes_run_along_y,
    spans_share_corridor,
)
from nf_metro.layout.route_topology import (
    convergence_junction_ids,
    merge_fanout_junction_ids,
)
from nf_metro.layout.routing.reserved_bands import (
    ReservedBand,
    ReservedBands,
    corridor_clearance_band,
    held_in_reserved_band,
)
from nf_metro.parser.model import Edge, MetroGraph, Port, PortSide, Section, Station


class OffsetRegime(Enum):
    """When a route's parallel-line separations are applied.

    A diagram routes lines on two separation regimes, and any pass reasoning
    about spacing must know which one a given route is in:

    ``DEFERRED``
        The stored points sit on the trunk centreline; the per-line separation
        is applied at render by :func:`apply_route_offsets` as a lateral (Y)
        shift of the endpoints.  The default for plain LR/RL runs.
    ``BAKED``
        The stored points already carry the separation -- a TB X-stagger, a
        rail's per-line Y, or a bundle's concentric corner fan -- because it is
        geometry a uniform endpoint Y-shift cannot express.  Render-time
        offsetting is skipped so the separation is not applied twice.
    """

    DEFERRED = "deferred"
    BAKED = "baked"


class Direction(Enum):
    """Cardinal travel direction for a horizontal or vertical run."""

    R = "R"  # east, +x
    L = "L"  # west, -x
    U = "U"  # north, -y
    D = "D"  # south, +y

    @property
    def sign(self) -> float:
        """``+1.0`` for R / D (positive axis), ``-1.0`` for L / U."""
        return 1.0 if self in (Direction.R, Direction.D) else -1.0


@dataclass(frozen=True, slots=True)
class SourceTurnout:
    """A rounded source junction shared across consecutive route members."""

    incoming_source_id: str
    continuing_target_id: str | None
    incoming_direction: Direction
    outgoing_direction: Direction
    radius: float

    def __post_init__(self) -> None:
        if not self.incoming_source_id:
            raise ValueError("a source turnout names its incoming member")
        if self.continuing_target_id == "":
            raise ValueError("a continuing source turnout names its target")
        if self.incoming_direction not in (Direction.R, Direction.L):
            raise ValueError("a source turnout enters horizontally")
        if self.outgoing_direction not in (Direction.U, Direction.D):
            raise ValueError("a source turnout leaves vertically")
        if not math.isfinite(self.radius) or self.radius <= 0:
            raise ValueError("a source turnout has a positive finite radius")


def right_normal_axis_sign(direction: Direction) -> int:
    """Return the screen-axis sign of the right-hand normal to *direction*."""
    return 1 if direction in (Direction.R, Direction.U) else -1


def horizontal_direction(dx: float) -> Direction:
    """``Direction.R`` if ``dx > 0`` else ``Direction.L`` (ties resolve to L)."""
    return Direction.R if dx > 0 else Direction.L


def vertical_direction(dy: float) -> Direction:
    """``Direction.D`` if ``dy > 0`` else ``Direction.U`` (ties resolve to U)."""
    return Direction.D if dy > 0 else Direction.U


def segment_direction(
    start: tuple[float, float], end: tuple[float, float]
) -> Direction | None:
    """The direction the axis-aligned leg from *start* to *end* travels.

    ``None`` when the leg is diagonal or has collapsed to a point: those carry
    no single direction.  Reading the leg's own two endpoints is what keeps a
    caller from having to know which axis it lies on before asking.
    """
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    if abs(dy) <= COORD_TOLERANCE and abs(dx) > COORD_TOLERANCE:
        return Direction.R if dx > 0 else Direction.L
    if abs(dx) <= COORD_TOLERANCE and abs(dy) > COORD_TOLERANCE:
        return Direction.D if dy > 0 else Direction.U
    return None


def is_orthogonal_turn(
    p0: tuple[float, float], p1: tuple[float, float], p2: tuple[float, float]
) -> bool:
    """True when the legs meeting at *p1* are one horizontal and one vertical.

    A 90-degree bend carries a rounded corner; a straight pass-through (both
    legs on the same axis) or a diagonal leg does not.
    """

    def axis(a: tuple[float, float], b: tuple[float, float]) -> str | None:
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        if dy <= COORD_TOLERANCE and dx > COORD_TOLERANCE:
            return "h"
        if dx <= COORD_TOLERANCE and dy > COORD_TOLERANCE:
            return "v"
        return None

    axis_in, axis_out = axis(p0, p1), axis(p1, p2)
    return axis_in is not None and axis_out is not None and axis_in != axis_out


def vertical_flow_sections(graph: MetroGraph) -> set[str]:
    """IDs of sections whose flow runs along Y (the vertical-flow directions).

    Both TB and BT stack their layers down the column and fan lines along X, so
    the routing handlers, offset assignment and reversal detection that key on a
    vertical flow treat the two identically; only their flow sign and lane sign
    (carried by :class:`~nf_metro.layout.geometry.AxisFrame`) differ.
    """
    return {sid for sid, s in graph.sections.items() if lanes_run_along_x(s.direction)}


def trailing_perp_side(direction: str) -> PortSide:
    """The TOP/BOTTOM side a vertical-flow section's trunk continues out through.

    A downward (TB) flow runs its trunk to the BOTTOM edge; its upward (BT)
    image runs it to the TOP.  Read from the frame's flow sign so the
    leading/trailing distinction follows the rotation, not a direction literal.
    Only meaningful for a vertical-flow (TB/BT) section.
    """
    return PortSide.BOTTOM if AxisFrame.flow_sign(direction) > 0 else PortSide.TOP


def perp_entry_consumer(graph: MetroGraph, port_id: str) -> Station | None:
    """The internal station a perpendicular entry port turns into."""
    for edge in graph.edges_from(port_id):
        consumer = graph.station_for_edge_target(edge)
        if not consumer.is_port:
            return consumer
    return None


def needs_perp_approach_fan(graph: MetroGraph, port_id: str) -> bool:
    """Whether *port_id* needs its distinct lines fanned onto parallel channels.

    True for a perpendicular (TOP/BOTTOM) entry into a *horizontal-flow* (LR/RL)
    section where two or more exit ports each contribute a *disjoint* line set --
    every line crosses the port via exactly one feeder.  Under a fold the feeders
    all sit on one column trunk, so without intervention each line drops on the
    port's single trunk X and the distinct lines overlay one vertical channel
    (and, where they bundle along a shared run, draw it as a zero-offset collinear
    bundle).  Each must instead fan onto its own approach channel by bundle index.

    Excludes the cases where collapsing onto one channel is correct or harmless:

    * redundant feeders each carrying the *same* full bundle (a parallel fan
      reconverging -- a line shared across feeders has no unique approach
      channel; each whole bundle drops on its feeder lane);
    * a single feeder (nothing to separate);
    * a vertical-flow (TB/BT) consumer, whose shared run is the perpendicular
      drop and so separates in X off the per-station offsets regardless.
    """
    port = graph.ports.get(port_id)
    if (
        port is None
        or not port.is_entry
        or port.side not in (PortSide.TOP, PortSide.BOTTOM)
    ):
        return False
    section = graph.section_for_port(port)
    if not lanes_run_along_y(section.direction):
        return False
    feeders_by_line: dict[str, set[str]] = {}
    for edge in graph.edges_to(port_id):
        src = graph.station_for_edge_source(edge)
        sp = graph.ports.get(edge.source)
        if not src.is_port or sp is None or sp.is_entry:
            continue
        feeders_by_line.setdefault(edge.line_id, set()).add(edge.source)
    feeder_sources = {fid for sources in feeders_by_line.values() for fid in sources}
    if len(feeder_sources) < 2:
        return False
    return all(len(sources) == 1 for sources in feeders_by_line.values())


def tb_right_entry_sections(graph: MetroGraph) -> set[str]:
    """IDs of TB sections that have a RIGHT entry port.

    A RIGHT-entry TB section runs its internal column in raw priority order;
    every other TB section runs it reversed.  Both the offset assignment and
    the section-reversal detection key on this distinction.
    """
    return {
        port.section_id
        for port in graph.ports.values()
        if port.is_entry
        and port.side == PortSide.RIGHT
        and graph.section_for_port(port).direction == "TB"
    }


# ---------------------------------------------------------------------------
# Grid-position helpers
# ---------------------------------------------------------------------------
# These replace repeated ``for s in graph.sections.values() if s.grid_col == X``
# patterns scattered across routing and layout modules.


def _sections_in_col(
    graph: MetroGraph,
    col: int | None,
    row: int | None = None,
) -> list[Section]:
    """Sections in a specific grid column with non-zero width.

    When *row* is given, restrict to sections occupying that grid row
    (honouring ``grid_row_span``).  An inter-section diversion travelling
    in one row must measure the gap against that row's sections only,
    otherwise a section stacked in another row of the same column (e.g. a
    wide output section below) corrupts the gap edges.
    """
    secs = [s for s in graph.sections.values() if s.grid_col == col and s.bbox_w > 0]
    if row is not None:
        secs = [
            s for s in secs if s.grid_row <= row <= s.grid_row + s.grid_row_span - 1
        ]
    return secs


def _sections_in_row(graph: MetroGraph, row: int | None) -> list[Section]:
    """Sections in a specific grid row with non-zero height."""
    return [s for s in graph.sections.values() if s.grid_row == row and s.bbox_h > 0]


def col_right_edge(
    graph: MetroGraph, col: int, default: float = 0.0, row: int | None = None
) -> float:
    """Rightmost X extent of sections in *col* (optionally a single *row*)."""
    secs = _sections_in_col(graph, col, row)
    return max((s.bbox_x + s.bbox_w for s in secs), default=default)


def col_left_edge(
    graph: MetroGraph, col: int | None, default: float = 0.0, row: int | None = None
) -> float:
    """Leftmost X extent of sections in *col* (optionally a single *row*)."""
    secs = _sections_in_col(graph, col, row)
    return min((s.bbox_x for s in secs), default=default)


def row_bottom_edge(
    graph: MetroGraph, row: int | None, default: float = 0.0, col: int | None = None
) -> float:
    """Bottommost Y extent of sections in *row* (optionally a single *col*).

    When *col* is given, restrict to sections in that grid column so an
    inter-row diversion travelling within one column isn't pushed down by a
    tall row-span section stacked in a different column of the same row.
    """
    secs = _sections_in_row(graph, row)
    if col is not None:
        secs = [s for s in secs if s.grid_col == col]
    return max((s.bbox_y + s.bbox_h for s in secs), default=default)


def row_top_edge(
    graph: MetroGraph, row: int, default: float = 0.0, col: int | None = None
) -> float:
    """Topmost Y extent of sections in *row* (optionally a single *col*)."""
    secs = _sections_in_row(graph, row)
    if col is not None:
        secs = [s for s in secs if s.grid_col == col]
    return min((s.bbox_y for s in secs), default=default)


def lowest_section_bottom_crossing_span(
    graph: MetroGraph,
    span_lo: float,
    span_hi: float,
    *,
    above_y: float,
    exclude: frozenset[str] = frozenset(),
) -> float | None:
    """Lowest bbox bottom edge among sections that cross a horizontal span.

    A section crosses the span when its (label-grown) x-range overlaps
    ``[span_lo, span_hi]``.  Only sections lying entirely above ``above_y`` are
    considered, so this answers "how far down does the row above dip into the
    band a horizontal run at some Y just above ``above_y`` would occupy" -- the
    caller drops the run below the returned edge to clear it.  Sections in
    ``exclude`` and zero-width (unplaced) sections are skipped.  ``None`` when
    no section crosses the span.  Unlike :func:`row_bottom_edge` this keys on
    geometric x-overlap rather than a grid column, so it also catches a box
    grown past its cell by an angled label.
    """
    lowest: float | None = None
    for sec in graph.sections.values():
        if sec.id in exclude or sec.bbox_w <= 0:
            continue
        s_bottom = sec.bbox_y + sec.bbox_h
        if s_bottom > above_y + COORD_TOLERANCE:
            continue
        s_left = sec.bbox_x
        s_right = s_left + sec.bbox_w
        if s_right <= span_lo + COORD_TOLERANCE or s_left >= span_hi - COORD_TOLERANCE:
            continue
        lowest = s_bottom if lowest is None else max(lowest, s_bottom)
    return lowest


def iter_inter_row_gaps(graph: MetroGraph) -> Iterator[tuple[int, float, float]]:
    """Yield ``(upper_row, top, bottom)`` for each inter-row gap, top to bottom.

    The gap between adjacent grid rows ``upper_row`` and ``upper_row + 1`` spans
    ``[top, bottom]`` (the upper row's bottom edge to the lower row's top edge).
    A row pair where either edge is absent (no section in that row) is skipped.
    """
    rows = sorted({s.grid_row for s in graph.sections.values()})
    for upper, lower in zip(rows, rows[1:]):
        top = row_bottom_edge(graph, upper, default=None)  # type: ignore[arg-type]
        bottom = row_top_edge(graph, lower, default=None)  # type: ignore[arg-type]
        if top is None or bottom is None:
            continue
        yield upper, top, bottom


def inter_row_gap_upper_row(graph: MetroGraph, y: float) -> int | None:
    """Grid row directly above the inter-row gap that contains *y*.

    Returns the upper of the two rows bounding the gap whose
    ``[row_bottom, next_row_top]`` band holds *y*; ``None`` when *y* falls in no
    gap (e.g. a deep dive below every row).  A handler declares this row-pair
    identity on a :class:`TrunkSlot` so the materialization pass groups trunks
    by gap without re-deriving it from their Ys.
    """
    for upper, top, bottom in iter_inter_row_gaps(graph):
        if top - COORD_TOLERANCE <= y <= bottom + COORD_TOLERANCE:
            return upper
    return None


def max_grid_row_with_content(graph: MetroGraph) -> int | None:
    """Bottommost grid row occupied by a section with rendered width.

    The single definition of "the bottom row" shared by the routing
    decision to bypass in the gap *above* a bottommost-row target
    (:func:`bypass_bottom_y`) and the placement reservation that keeps that
    gap wide enough (``_merge_trunk_row_minimums``); ``None`` when no section
    has width yet.
    """
    rows = [s.grid_row for s in graph.sections.values() if s.bbox_w > 0]
    return max(rows) if rows else None


def header_corridor_y(
    graph: MetroGraph,
    row: int,
    *,
    below: bool,
    base_radius: float,
    default: float = 0.0,
    col: int | None = None,
) -> float:
    """Y of an inter-row routing channel that clears a row's header band.

    Above the row (``below=False``) the channel sits a header band above the
    top edge; below it sits a route's clearance under the bottom edge.  The
    full :data:`INTER_ROW_HEADER_CLEARANCE` applies only when a section
    occupies the gap above the row (contributing a header badge); the topmost
    row has only the canvas-top title band, so the smaller
    :data:`EDGE_TO_BUNDLE_CLEARANCE` keeps the channel from overshooting it.

    When *col* is given the channel clears only that grid column's sections, so
    a corridor leg confined to one column isn't pushed past a tall section
    stacked in a different column of the same row.
    """
    if below:
        return (
            row_bottom_edge(graph, row, default=default, col=col)
            + EDGE_TO_BUNDLE_CLEARANCE
            + base_radius
        )
    clearance = (
        INTER_ROW_HEADER_CLEARANCE
        if section_exists_above_row(graph, row)
        else EDGE_TO_BUNDLE_CLEARANCE
    )
    return row_top_edge(graph, row, default=default, col=col) - clearance - base_radius


def section_exists_above_row(graph: MetroGraph, row: int) -> bool:
    """True if any section lies entirely above grid *row* (its bottom row is
    a higher row than *row*).

    Distinguishes a row with a genuine inter-row gap above it (a section
    contributes a header badge there) from the topmost row, which has only
    the canvas-top padding above.
    """
    return any(s.grid_row + s.grid_row_span - 1 < row for s in graph.sections.values())


def column_gap_midpoint(
    graph: MetroGraph, col_a: int, col_b: int, row: int | None = None
) -> float:
    """X midpoint of the gap between two columns (optionally within *row*)."""
    right, left = column_gap_edges(graph, col_a, col_b, row)
    return (right + left) / 2


def column_gap_edges(
    graph: MetroGraph,
    col_a: int,
    col_b: int,
    row: int | None = None,
    *,
    require_both_columns: bool = True,
) -> tuple[float, float]:
    """Return ``(left_edge, right_edge)`` of the gap between two columns.

    *left_edge* is the right boundary of the lower-column sections;
    *right_edge* is the left boundary of the higher-column sections.

    When *row* is given, only sections occupying that grid row bound the
    gap, so a diversion travelling in one row isn't pushed off-centre by a
    section stacked in another row of the same column.

    A gap needs a section on each side to bound it.  Where a bounding column has
    no section in *row*, the default reports a degenerate pair (``right <=
    left``) that callers read as "no gap here", because standing in a default
    edge would describe a span reaching past the columns beyond the absent one --
    and a channel anywhere inside that span would be *matched* to this gap and
    re-seated in it.

    Pass ``require_both_columns=False`` to get that spanning corridor anyway.
    Placement callers want it: an initial channel X only has to land in roughly
    the right region, and the band-clearance pass then settles it against
    whichever box the row actually puts in the way.  Callers that *identify*
    which gap a channel occupies must keep the default.
    """
    lo, hi = min(col_a, col_b), max(col_a, col_b)
    if require_both_columns and not _sections_in_col(graph, lo, row):
        edge = col_left_edge(graph, hi, row=row)
        return edge, edge
    right = col_right_edge(graph, lo, row=row)
    left = col_left_edge(graph, hi, default=right, row=row)
    return right, left


def off_grid_gap_bundle_midpoint(
    graph: MetroGraph, lo: int, row: int | None, bundle_width: float
) -> float | None:
    """X midline of a bundle in gap ``(lo, lo + 1)`` when the grid has one side.

    ``None`` unless exactly one of the two columns carries a section anywhere
    on the grid, which is the case that leaves the gap with no facing edge to
    measure at any row: the caller then reads :func:`column_gap_edges` and
    centres the bundle between the two edges as usual.

    The bundle sits :data:`EDGE_TO_BUNDLE_CLEARANCE` off that one real edge --
    the floor :func:`symmetric_bundle_midpoint` holds against a bounded gap's
    edges -- rather than centred across a span whose absent side
    :func:`col_left_edge` and :func:`col_right_edge` report as the coordinate
    origin, because a midpoint measured against the origin is set by the map's
    overall size instead of by the box the bundle hugs.  *row* narrows the real
    edge to the bundle's own row where the column reaches it, and the column's
    full extent covers the rest.
    """
    lo_on_grid = bool(_sections_in_col(graph, lo))
    hi_on_grid = bool(_sections_in_col(graph, lo + 1))
    if lo_on_grid == hi_on_grid:
        return None
    reach = EDGE_TO_BUNDLE_CLEARANCE + bundle_width / 2
    if lo_on_grid:
        anywhere = col_right_edge(graph, lo)
        return col_right_edge(graph, lo, default=anywhere, row=row) + reach
    anywhere = col_left_edge(graph, lo + 1)
    return col_left_edge(graph, lo + 1, default=anywhere, row=row) - reach


def row_local_gap_bundle_midpoint(
    graph: MetroGraph, lo: int, row: int | None, bundle_width: float
) -> float | None:
    """X midline of a bundle in gap ``(lo, lo + 1)`` when the lower column is on
    the grid but has no section in *row* while the upper column does.

    ``None`` outside that one case.  :func:`column_gap_edges` bounds a gap by
    computing the lower column's right edge first and passing it as the upper
    column's default, so a gap whose *upper* column is missing from *row* hugs
    the lower column's real edge.  The mirror is where the defect lives:
    when the *lower* column is absent from *row*, its :func:`col_right_edge`
    lookup finds nothing and falls to ``0.0``, and the bundle centres against
    the coordinate origin instead of real geometry -- a midline the map's
    overall size sets rather than the box the channel hugs.

    The upper column carries a real edge in *row*, so the bundle seats
    :data:`EDGE_TO_BUNDLE_CLEARANCE` off it, exactly the ``hi``-on-grid branch
    of :func:`off_grid_gap_bundle_midpoint`.  A lower column absent from the
    whole grid is that function's job, not this one.
    """
    lo_absent = not _sections_in_col(graph, lo, row)
    hi_present = bool(_sections_in_col(graph, lo + 1, row))
    if not (lo_absent and hi_present and _sections_in_col(graph, lo)):
        return None
    reach = EDGE_TO_BUNDLE_CLEARANCE + bundle_width / 2
    return col_left_edge(graph, lo + 1, row=row) - reach


def packed_cell_neighbor_edges(
    graph: MetroGraph, section_id: str, side: PortSide
) -> tuple[float, float] | None:
    """Gap edges between *section_id* and its nearest packed cell-mate on *side*.

    A packed cell (``%%metro grid: a, b | col,row``) can place a cell-mate
    directly between a section and the rest of its grid column, so the
    column-level gap (:func:`column_gap_edges`) reaches past that cell-mate
    instead of stopping at it. Returns ``None`` when *section_id* has no
    cell-mate on that side, so the caller falls back to the column-edge gap.
    """
    sec = graph.sections[section_id]
    members = graph.cell_packs.get((sec.grid_col, sec.grid_row))
    if not members or len(members) < 2:
        return None
    sign = 1 if side is PortSide.RIGHT else -1
    own_edge = sec.bbox_x + sec.bbox_w if side is PortSide.RIGHT else sec.bbox_x

    def facing_edge(m: Section) -> float:
        return m.bbox_x if side is PortSide.RIGHT else m.bbox_x + m.bbox_w

    facing = [
        m
        for mid in members
        if mid != section_id
        for m in [graph.sections[mid]]
        if m.bbox_w > 0 and sign * (facing_edge(m) - own_edge) >= -COORD_TOLERANCE
    ]
    if not facing:
        return None
    nearest_edge = facing_edge(min(facing, key=lambda m: sign * facing_edge(m)))
    return (
        (own_edge, nearest_edge) if side is PortSide.RIGHT else (nearest_edge, own_edge)
    )


def _grid_row_bands(graph: MetroGraph) -> dict[int, tuple[float, float]]:
    """Per grid-row vertical band ``(top, bottom)`` spanned by its sections."""
    bands: dict[int, tuple[float, float]] = {}
    for s in graph.sections.values():
        if s.bbox_h <= 0:
            continue
        for r in range(s.grid_row, s.grid_row + max(1, s.grid_row_span)):
            top, bot = bands.get(r, (s.bbox_y, s.bbox_y + s.bbox_h))
            bands[r] = (min(top, s.bbox_y), max(bot, s.bbox_y + s.bbox_h))
    return bands


@dataclass(frozen=True)
class GapLookupGeometry:
    """Static section geometry shared by repeated inter-column gap lookups."""

    cols: tuple[int, ...]
    rows: tuple[int, ...]
    row_bands: Mapping[int, tuple[float, float]]


def gap_lookup_geometry(graph: MetroGraph) -> GapLookupGeometry:
    """Build the static lookup state used by :func:`gap_lo_for_x`."""
    sections = [section for section in graph.sections.values() if section.bbox_w > 0]
    return GapLookupGeometry(
        cols=tuple(sorted({section.grid_col for section in sections})),
        rows=tuple(sorted({section.grid_row for section in sections})),
        row_bands=_grid_row_bands(graph),
    )


def gap_lo_for_x(
    graph: MetroGraph,
    x: float,
    y_lo: float,
    y_hi: float,
    tol: float = COORD_TOLERANCE,
    *,
    lookup: GapLookupGeometry | None = None,
) -> tuple[int, int | None] | None:
    """``(lower column, row)`` of the inter-column gap a vertical leg occupies.

    Lets a handler that has just placed a vertical channel name the gap it sits
    in, so it can declare a :class:`GapSlot` without the post-routing pass having
    to rediscover it from raw geometry.  A leg at *x* spanning ``[y_lo, y_hi]``
    is matched to the row whose gap edges bracket *x* AND whose vertical band the
    leg overlaps; failing that, to any row whose edges bracket *x*; failing that,
    to the row-agnostic union (``row = None``).  ``None`` when *x* sits outside
    every inter-column gap.
    """
    if lookup is None:
        lookup = gap_lookup_geometry(graph)
    bracket: tuple[int, int | None] | None = None
    for r in lookup.rows:
        for lo, hi in zip(lookup.cols, lookup.cols[1:]):
            if hi != lo + 1:
                continue
            left, right = column_gap_edges(graph, lo, hi, row=r)
            if not (right > left and left - tol <= x <= right + tol):
                continue
            if bracket is None:
                bracket = (lo, r)
            band = lookup.row_bands.get(r)
            if band is not None and y_lo < band[1] and band[0] < y_hi:
                return lo, r
    if bracket is not None:
        return bracket
    for lo, hi in zip(lookup.cols, lookup.cols[1:]):
        if hi != lo + 1:
            continue
        left, right = column_gap_edges(graph, lo, hi, row=None)
        if right > left and left - tol <= x <= right + tol:
            return lo, None
    return None


def iter_vertical_segments(
    rp: RoutedPath,
) -> Iterator[tuple[int, float, float, float, bool]]:
    """Yield ``(idx, x, y_lo, y_hi, down)`` for each vertical leg of *rp*.

    ``idx`` is the segment's start index in ``rp.points`` and ``down`` is True
    when the leg travels in increasing Y.  A leg is a segment that holds X
    constant while changing Y by more than :data:`COORD_TOLERANCE`.
    """
    pts = rp.points
    for k in range(len(pts) - 1):
        x0, y0 = pts[k]
        x1, y1 = pts[k + 1]
        if abs(x1 - x0) < COORD_TOLERANCE and abs(y1 - y0) > COORD_TOLERANCE:
            yield k, x0, min(y0, y1), max(y0, y1), y1 > y0


def symmetric_bundle_midpoint(
    gap_left: float,
    gap_right: float,
    bundle_widths: list[float],
    bundle_index: int,
    edge_clearance: float = EDGE_TO_BUNDLE_CLEARANCE,
    inter_bundle: float = BUNDLE_TO_BUNDLE_CLEARANCE,
) -> float:
    """X midline of one bundle when several share an inter-section gap.

    Implements the symmetric placement described in the inter-section
    gap design contract::

        - ``W = gap_right - gap_left``
        - ``WT = sum(bundle_widths) + (N - 1) * B``
        - The leftmost line of the leftmost bundle sits at
          ``gap_left + (W - WT) / 2``.
        - Bundles are separated by exactly ``B``; only the
          edge-to-bundle distance grows when ``W`` exceeds the minimum.

    Returns the midline x for bundle ``bundle_index`` (0-indexed from
    the leftmost).  ``bundle_widths[k]`` is the visual span of bundle
    ``k`` (typically ``(n_k - 1) * OFFSET_STEP``).

    When ``W`` is smaller than the required minimum the function still
    returns the symmetric midline as if the gap were exactly that
    minimum; the caller is responsible for widening the gap (handled
    by ``_enforce_min_column_gaps`` during section placement).
    """
    n = len(bundle_widths)
    if n == 0:
        return (gap_left + gap_right) / 2
    if bundle_index < 0 or bundle_index >= n:
        raise IndexError(f"bundle_index {bundle_index} out of range [0,{n})")

    W = gap_right - gap_left
    WT = sum(bundle_widths) + (n - 1) * inter_bundle
    # If the gap is wider than the minimum (2A + WT), the extra space
    # is distributed equally to both edges; the symmetric leftmost-line
    # offset from gap_left is (W - WT) / 2.
    leftmost_offset = max(edge_clearance, (W - WT) / 2)
    # Position of the leftmost line of the leftmost bundle.
    cursor = gap_left + leftmost_offset
    for k in range(bundle_index):
        cursor += bundle_widths[k] + inter_bundle
    # cursor is now the leftmost line of bundle bundle_index;
    # the midline is cursor + width/2.
    return cursor + bundle_widths[bundle_index] / 2


def bundle_width(n_lines: int, offset_step: float = OFFSET_STEP) -> float:
    """Visual span of a bundle of *n_lines* parallel lines."""
    return max(0, n_lines - 1) * offset_step


@dataclass(frozen=True, slots=True)
class GapSlot:
    """A symbolic position for a vertical channel run within a gap bundle.

    A handler declares *where* a vertical run intends to sit -- which line of
    which bundle, in which inter-column corridor, travelling which way -- without
    committing to a concrete X coordinate.  A single materialization pass later
    resolves the slot to final geometry, replacing the compute-then-renormalize
    chain in which handlers emit ``_get_offset`` Xs that a post-pass discards and
    re-derives.

    The corridor is the inter-column gap bounded by the adjacent grid columns
    ``gap_lo_col`` and ``gap_hi_col`` (``gap_hi_col == gap_lo_col + 1``); the run
    traverses grid ``row`` in ``direction`` (:attr:`Direction.U` or
    :attr:`Direction.D`).  ``row`` is ``None`` for a channel that is matched to
    the row-agnostic gap union (a leg whose row could not be pinned to a single
    grid row).  ``slot_index`` is this line's 0-based rank among the ``n_slots``
    lines sharing the same gap and direction.
    """

    gap_lo_col: int
    gap_hi_col: int
    row: int | None
    direction: Direction
    slot_index: int
    n_slots: int


@dataclass(frozen=True, slots=True)
class TrunkSlot:
    """The inter-row gap a route's horizontal bypass trunk runs in.

    The trunk twin of :class:`GapSlot`.  A U-shaped bypass route runs its
    interior horizontal leg through an inter-row gap; a handler declares *which*
    gap without committing to a concrete Y, and :func:`_materialize_trunk_slots`
    groups every declared trunk by gap and fans the co-travelling lines into a
    concentric band.

    ``gap_upper_row`` is the grid row directly above the gap (the gap separates
    rows ``gap_upper_row`` and ``gap_upper_row + 1``), or ``None`` for a deep
    cross-row dive that clears every row and so sits in no single inter-row gap.
    A present-but-``None`` slot thus distinguishes a trunk in no gap from a route
    with no trunk at all (``trunk_slot is None``).  The trunk's traversal
    direction and its rank within the band are read from the routed geometry at
    materialization, so they are not declared here.
    """

    gap_upper_row: int | None


@dataclass
class RoutedPath:
    """A routed path for an edge, consisting of (x, y) waypoints."""

    edge: Edge
    line_id: str
    points: list[tuple[float, float]]
    is_inter_section: bool = False
    curve_radii: list[float] | None = None
    offset_regime: OffsetRegime = OffsetRegime.DEFERRED
    """Which separation regime this route is in (see :class:`OffsetRegime`)."""
    normalize_exempt: bool = False
    """Skip this route in the gap-channel normalization post-pass.

    Set by wrap / around-section / TOP-entry handlers whose vertical
    channels follow a special concentric loop (all corners share one
    radius) that the standard L-shape re-stacking would break."""
    gap_slots: list[GapSlot] = field(default_factory=list)
    """Symbolic gap-relative slots for this route's vertical channel runs.

    Empty until a handler declares placement symbolically.  A route may own
    more than one (a U-shaped bypass declares both its descent and its ascent
    channel); :func:`_materialize_gap_slots` resolves each to a concrete X."""
    trunk_slot: TrunkSlot | None = None
    """Symbolic inter-row gap for this route's horizontal bypass trunk.

    ``None`` until a handler that emits a U-shaped bypass declares which gap its
    trunk runs in; :func:`_materialize_trunk_slots` resolves it to a concrete Y.
    A route owns at most one trunk, so this is a single slot, not a list."""
    exit_turn_plan_id: str | None = None
    """Pre-routing plan that owns this route's source turn, when applicable."""
    exit_turn_member_id: str | None = None
    """Semantic emission member bound to the planned source turn."""
    exit_turn_family_id: str | None = None
    """Production family that consumed the planned assignment."""
    exit_turn_axis_id: str | None = None
    """Shared planned axis used by the source turn, when the route turns."""
    fan_plan_id: str | None = None
    """Immutable fan plan that exclusively owns this route, when applicable."""
    fan_route_emitter: str | None = None
    """Planned fan emitter that produced this route."""
    route_system_id: str | None = None
    """Canonical semantic system that owns this inter-section emission."""
    emission_member_id: str | None = None
    """Canonical physical member represented by this route."""
    route_system_disposition: str | None = None
    """Whole-system disposition attributed at emission."""
    route_plan_ids: tuple[str, ...] = ()
    """Immutable child plans contributing to the route-system decision."""
    member_geometry_plan_id: str | None = None
    """Member-geometry plan that owns this route's frozen channel segments."""
    route_reservation_ids: tuple[str, ...] = ()
    """Realised reservation records claimed by this emission member."""
    convergence_plan_id: str | None = None
    """Immutable convergence plan that owns this route's terminal geometry."""
    convergence_member_id: str | None = None
    """Semantic emission member bound to the planned convergence."""
    convergence_owned_segment_ranks: tuple[int, ...] = ()
    """Segments whose final geometry is owned by the convergence plan."""
    route_system_owned_segment_ranks: tuple[int, ...] = ()
    """Gap-channel segments frozen by the route-system member plan."""
    exit_turn_segment_rank: int | None = None
    """Index of the owned turn segment's first waypoint."""
    concentric_corner_offsets_by_segment: dict[
        int, tuple[float | None, float | None]
    ] = field(default_factory=dict)
    """Signed offsets supplied to the standard concentric corner machinery."""
    concentric_corner_bases_by_segment: dict[int, tuple[float | None, float | None]] = (
        field(default_factory=dict)
    )
    """Reference radii supplied with the standard concentric corner offsets."""
    exit_lane_transition_plan_id: str | None = None
    """Plan that owns this explicit compact-lane hand-off."""
    source_turnout: SourceTurnout | None = None
    """Frozen cross-member curve into a vertical arm at its source junction."""

    def record_concentric_corner(
        self, radius_index: int, offset: float, base_radius: float
    ) -> None:
        """Publish one standard corner input to both adjacent segment views."""
        for segment_rank, tuple_index in (
            (radius_index, 1),
            (radius_index + 1, 0),
        ):
            if not 0 < segment_rank < len(self.points) - 1:
                continue
            pair = list(
                self.concentric_corner_offsets_by_segment.get(
                    segment_rank, (None, None)
                )
            )
            pair[tuple_index] = offset
            self.concentric_corner_offsets_by_segment[segment_rank] = (
                pair[0],
                pair[1],
            )
            bases = list(
                self.concentric_corner_bases_by_segment.get(segment_rank, (None, None))
            )
            bases[tuple_index] = base_radius
            self.concentric_corner_bases_by_segment[segment_rank] = (
                bases[0],
                bases[1],
            )

    def declare_gap_slot(
        self,
        *,
        lo_col: int,
        hi_col: int,
        row: int | None,
        direction: Direction,
        slot_index: int,
        n_slots: int,
    ) -> None:
        """Record that one of this route's vertical legs runs in a gap bundle.

        Handlers call this where they place a vertical channel; ``slot_index``
        / ``n_slots`` are the line's provisional rank among the siblings the
        handler can see.  :func:`_materialize_gap_slots` groups every declared
        slot by ``(lo_col, row, direction)`` and assigns the final concentric X,
        re-ranking each gap bundle from the routed geometry rather than from the
        provisional rank.
        """
        self.gap_slots.append(
            GapSlot(
                gap_lo_col=lo_col,
                gap_hi_col=hi_col,
                row=row,
                direction=direction,
                slot_index=slot_index,
                n_slots=n_slots,
            )
        )

    def declare_trunk_slot(self, *, gap_upper_row: int | None) -> None:
        """Record the inter-row gap this route's horizontal bypass trunk runs in.

        :func:`_materialize_trunk_slots` groups every declared trunk by
        ``gap_upper_row`` and assigns the final concentric Y, reading each
        trunk's direction and band rank from the routed geometry.
        """
        self.trunk_slot = TrunkSlot(gap_upper_row=gap_upper_row)


def apply_route_offsets(
    route: RoutedPath,
    station_offsets: Mapping[tuple[str, str], float],
) -> list[tuple[float, float]]:
    """The route's final render geometry, with its line separation applied.

    The single place a route's stored points become drawable coordinates, so
    every spacing-aware pass (the renderer, the label-strike search, the render
    invariants) reads one regime-aware result instead of re-deriving it.

    A :attr:`~OffsetRegime.BAKED` route already carries its separation, so its
    points are returned verbatim.  A :attr:`~OffsetRegime.DEFERRED` route is
    shifted in Y: the source-side waypoints by the source offset, the
    target-side by the target offset, each interior point assigned to whichever
    end it is closer to.
    """
    if route.offset_regime is OffsetRegime.BAKED:
        return list(route.points)

    src_off = station_offsets.get((route.edge.source, route.line_id), 0.0)
    tgt_off = station_offsets.get((route.edge.target, route.line_id), 0.0)
    orig_sy = route.points[0][1]
    orig_ty = route.points[-1][1]
    last = len(route.points) - 1
    pts: list[tuple[float, float]] = []
    for i, (x, y) in enumerate(route.points):
        if i == 0:
            pts.append((x, y + src_off))
        elif i == last:
            pts.append((x, y + tgt_off))
        elif abs(y - orig_sy) <= abs(y - orig_ty):
            pts.append((x, y + src_off))
        else:
            pts.append((x, y + tgt_off))
    return pts


def opening_horizontal_vertical(
    pts: Sequence[tuple[float, float]],
) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]] | None:
    """The first three points when a polyline opens horizontal-then-vertical.

    The shape ``(sx, sy) -> (vx, sy) -> (vx, dy)``: a horizontal lead off the
    source, then a vertical leg in its own channel.  ``None`` when the polyline
    is shorter than three points or either leg is off-axis, which covers a route
    that leaves on a diagonal, as a bare drop, or straight through.
    """
    if len(pts) < 3:
        return None
    p0, p1, p2 = pts[0], pts[1], pts[2]
    if abs(p1[1] - p0[1]) > COORD_TOLERANCE or abs(p1[0] - p0[0]) <= COORD_TOLERANCE:
        return None
    if abs(p2[0] - p1[0]) > COORD_TOLERANCE or abs(p2[1] - p1[1]) <= COORD_TOLERANCE:
        return None
    return p0, p1, p2


def initial_fanout_descent_span(
    rp: RoutedPath,
) -> tuple[float, float, float, bool] | None:
    """``(x, y_lo, y_hi, down)`` of the descent leaving a route's source.

    A fan-out branch opens with a short horizontal lead off the shared source,
    then a vertical descent in its own channel.  Returns ``None`` when the route
    does not open horizontal-then-vertical.
    """
    opening = opening_horizontal_vertical(rp.points)
    if opening is None:
        return None
    _p0, (x1, y1), (_x2, y2) = opening
    return x1, min(y1, y2), max(y1, y2), y2 > y1


@dataclass(frozen=True)
class HTrunkSeg:
    """One interior horizontal leg of a route, flanked by two vertical legs.

    The trunk runs at ``y`` from ``xa`` to ``xb`` (traversal order, not
    sorted); its two flanking risers stand at those Xs and climb/drop to
    ``before_y`` (at ``xa``) and ``after_y`` (at ``xb``) -- the bottom or top
    of a U-shaped bypass.
    """

    y: float
    xa: float
    xb: float
    before_y: float
    after_y: float

    @property
    def x_lo(self) -> float:
        return min(self.xa, self.xb)

    @property
    def x_hi(self) -> float:
        return max(self.xa, self.xb)


def iter_horizontal_trunks(rp: RoutedPath) -> Iterator[tuple[int, HTrunkSeg]]:
    """Yield ``(waypoint_index, segment)`` for each interior horizontal trunk.

    A trunk is an interior horizontal leg whose two flanking neighbours are
    both vertical, i.e. the bottom (or top) leg of a U-shaped bypass.  The
    index is the trunk leg's first waypoint, ``points[index] -> [index+1]``.
    """
    pts = rp.points
    for k in range(1, len(pts) - 2):
        x0, y0 = pts[k]
        x1, y1 = pts[k + 1]
        if abs(y1 - y0) > COORD_TOLERANCE or abs(x1 - x0) <= COORD_TOLERANCE:
            continue
        if abs(pts[k - 1][0] - x0) > COORD_TOLERANCE:
            continue
        if abs(pts[k + 2][0] - x1) > COORD_TOLERANCE:
            continue
        yield k, HTrunkSeg(y0, x0, x1, pts[k - 1][1], pts[k + 2][1])


class PeeloffTail(NamedTuple):
    """A vertical approach peeling off a horizontal trunk into an entry port."""

    trunk_start_x: float
    trunk_y: float
    peel_x: float
    port_y: float
    trunk_sign: int  # +1 trunk runs left->right toward the peel, -1 right->left
    vertical_sign: int  # +1 approaches down, -1 approaches up
    port_lead_sign: int  # +1 enters rightward, -1 enters leftward

    @property
    def x_lo(self) -> float:
        """Left edge of the destination-facing horizontal trunk."""
        return min(self.trunk_start_x, self.peel_x)

    @property
    def x_hi(self) -> float:
        """Right edge of the destination-facing horizontal trunk."""
        return max(self.trunk_start_x, self.peel_x)


def is_side_entry_port(graph: MetroGraph, port_id: str) -> bool:
    """Whether *port_id* names a LEFT or RIGHT entry port."""
    port = graph.ports.get(port_id)
    return (
        port is not None
        and port.is_entry
        and port.side in (PortSide.LEFT, PortSide.RIGHT)
    )


def port_peeloff_tail(rp: RoutedPath) -> PeeloffTail | None:
    """The peel-off tail ending at an entry port, or ``None``.

    A peel-off-into-port tail ends ``... (tx, trunk_y) -> (peel_x, trunk_y)
    -> (peel_x, port_y) -> (ex, port_y)``: a horizontal trunk, a vertical
    approach, then a short horizontal lead into the port.  The three direction
    signs describe the two turns, including downward approaches whose half-turn
    transposes the bundle between the trunk and the port.  Returns ``None`` for
    any other tail.
    """
    pts = rp.points
    if len(pts) < 4:
        return None
    (x4, y4), (x3, y3), (x2, y2), (x1, y1) = pts[-4], pts[-3], pts[-2], pts[-1]
    if abs(y2 - y1) > COORD_TOLERANCE or abs(x2 - x1) <= COORD_TOLERANCE:
        return None  # port lead is not horizontal
    if abs(x3 - x2) > COORD_TOLERANCE or abs(y3 - y2) <= COORD_TOLERANCE:
        return None  # riser is not vertical
    if abs(y4 - y3) > COORD_TOLERANCE or abs(x4 - x3) <= COORD_TOLERANCE:
        return None  # trunk is not horizontal
    return PeeloffTail(
        trunk_start_x=x4,
        trunk_y=y3,
        peel_x=x3,
        port_y=y2,
        trunk_sign=1 if x3 > x4 else -1,
        vertical_sign=1 if y2 > y3 else -1,
        port_lead_sign=1 if x1 > x2 else -1,
    )


def x_follows_trunk_direction(vertical_sign: int, trunk_sign: int) -> bool:
    """Whether approach-X order follows (not reverses) trunk-depth order.

    The first H-to-V turn decides this independently of the port-Y transpose: a
    downward approach off a rightward trunk reverses the order, an upward one
    keeps it.
    """
    return -vertical_sign == trunk_sign


def tail_overlap(per_line: dict[str, PeeloffTail]) -> float:
    """Length of the destination-facing horizontal corridor every tail shares."""
    return min(t.x_hi for t in per_line.values()) - max(
        t.x_lo for t in per_line.values()
    )


class PortPeeloffBundle(NamedTuple):
    """One concentric bundle of lines peeling off a shared trunk into a port.

    ``entries`` holds every ``(route, tail)`` reaching the port (a line can feed
    several approaches); ``per_line`` is one representative tail per distinct
    line, the unit the slot order is assigned over.  The direction signs are
    common to every member and define how trunk order maps independently onto
    approach-X order and port-Y order.
    """

    port_id: str
    entries: list[tuple[RoutedPath, PeeloffTail]]
    per_line: dict[str, PeeloffTail]
    trunk_sign: int
    vertical_sign: int
    port_lead_sign: int


class OpposingEntryConfluence(NamedTuple):
    """Opposing horizontal feeders sharing one vertical approach to a port."""

    port_id: str
    entries: list[tuple[RoutedPath, PeeloffTail]]
    per_line: dict[str, PeeloffTail]
    vertical_sign: int
    port_lead_sign: int


class SameDestinationVerticalBundle(NamedTuple):
    """Distinct lines sharing the final vertical corridor into one side port."""

    port_id: str
    entries: list[tuple[RoutedPath, PeeloffTail]]
    per_line: dict[str, PeeloffTail]
    trunk_sign: int
    vertical_sign: int
    port_lead_sign: int


class PeeloffSlot(NamedTuple):
    """A peel-off line's target peel-x, port-slot Y, and concentric rank."""

    peel_x: float
    port_y: float
    rank: int


class _SameDestinationCohort(NamedTuple):
    """One same-shape H-V-H group at an entry port, with its measured spans."""

    port_id: str
    trunk_sign: int
    vertical_sign: int
    port_lead_sign: int
    entries: list[tuple[RoutedPath, PeeloffTail]]
    per_line: dict[str, PeeloffTail]
    suffix_run: float
    vertical_overlap: float

    def bundle(self) -> SameDestinationVerticalBundle:
        return SameDestinationVerticalBundle(
            self.port_id,
            self.entries,
            self.per_line,
            self.trunk_sign,
            self.vertical_sign,
            self.port_lead_sign,
        )


def _cohort_gap_ids(
    graph: MetroGraph, per_line: Mapping[str, PeeloffTail]
) -> set[tuple[int, int | None] | None]:
    """The inter-column gap each member's vertical approach classifies into."""
    return {
        gap_lo_for_x(
            graph,
            tail.peel_x,
            min(tail.trunk_y, tail.port_y),
            max(tail.trunk_y, tail.port_y),
        )
        for tail in per_line.values()
    }


def _iter_same_destination_cohorts(
    routes: list[RoutedPath],
    graph: MetroGraph,
    step: float,
    admit: Callable[[RoutedPath, PeeloffTail, Port], bool],
) -> Iterator[_SameDestinationCohort]:
    """Group peel-off tails into complete, distinctly-landed cohorts per port.

    *admit* screens one member against the caller's own shape requirements; the
    cohort-level facts every caller needs -- complete destination-line
    membership, one member per line, distinct landings within the bundle pitch,
    the shared landing suffix and the shared vertical overlap -- are measured
    once here.
    """
    by_shape: dict[tuple[str, int, int, int], list[tuple[RoutedPath, PeeloffTail]]] = (
        defaultdict(list)
    )
    for route in routes:
        tail = port_peeloff_tail(route)
        port = graph.ports.get(route.edge.target)
        if tail is None or port is None or not port.is_entry:
            continue
        if not admit(route, tail, port):
            continue
        by_shape[
            route.edge.target,
            tail.trunk_sign,
            tail.vertical_sign,
            tail.port_lead_sign,
        ].append((route, tail))

    for (
        port_id,
        trunk_sign,
        vertical_sign,
        port_lead_sign,
    ), entries in by_shape.items():
        per_line: dict[str, PeeloffTail] = {}
        for route, tail in entries:
            per_line.setdefault(route.line_id, tail)
        n = len(per_line)
        if (
            n < 2
            or len(entries) != n
            or set(per_line) != set(graph.station_lines(port_id))
        ):
            continue
        port_ys = sorted(tail.port_y for tail in per_line.values())
        if (
            any(
                right - left <= COORD_TOLERANCE
                for left, right in zip(port_ys, port_ys[1:])
            )
            or port_ys[-1] - port_ys[0] > (n - 1) * step + COORD_TOLERANCE
        ):
            continue
        suffix_lo = max(
            min(tail.peel_x, route.points[-1][0]) for route, tail in entries
        )
        suffix_hi = min(
            max(tail.peel_x, route.points[-1][0]) for route, tail in entries
        )
        vertical_lo = max(min(tail.trunk_y, tail.port_y) for tail in per_line.values())
        vertical_hi = min(max(tail.trunk_y, tail.port_y) for tail in per_line.values())
        yield _SameDestinationCohort(
            port_id,
            trunk_sign,
            vertical_sign,
            port_lead_sign,
            entries,
            per_line,
            suffix_hi - suffix_lo,
            vertical_hi - vertical_lo,
        )


def iter_same_destination_approach_bundles(
    routes: list[RoutedPath],
    graph: MetroGraph,
    step: float,
    *,
    min_common_run: float = 2 * CURVE_RADIUS,
    max_separation: float = EDGE_TO_BUNDLE_CLEARANCE,
) -> Iterator[SameDestinationVerticalBundle]:
    """Yield complete same-direction H-V-H groups eligible for tight bundling."""

    def admit(route: RoutedPath, tail: PeeloffTail, port: Port) -> bool:
        return (
            port.side in (PortSide.LEFT, PortSide.RIGHT)
            and not graph.station_is_rail(route.edge.source)
            and not graph.station_is_rail(route.edge.target)
        )

    for cohort in _iter_same_destination_cohorts(routes, graph, step, admit):
        peel_xs = [tail.peel_x for tail in cohort.per_line.values()]
        if max(peel_xs) - min(peel_xs) > max_separation:
            continue
        if cohort.suffix_run < min_common_run - COORD_TOLERANCE:
            continue
        if cohort.vertical_overlap < min_common_run - COORD_TOLERANCE:
            continue
        gaps = _cohort_gap_ids(graph, cohort.per_line)
        if len(gaps) != 1 or None in gaps:
            continue
        yield cohort.bundle()


def _flow_exit_feeding_route_source(graph: MetroGraph, route: RoutedPath) -> str | None:
    current = route.edge.source
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        port = graph.ports.get(current)
        if port is not None:
            return (
                current
                if not port.is_entry and port.side in (PortSide.LEFT, PortSide.RIGHT)
                else None
            )
        if current not in graph.junctions:
            return None
        predecessors = {
            edge.source
            for edge in graph.edges_to(current)
            if edge.line_id == route.line_id
        }
        if len(predecessors) != 1:
            return None
        current = next(iter(predecessors))
    return None


def iter_plannable_short_same_destination_bundles(
    routes: list[RoutedPath],
    graph: MetroGraph,
    step: float,
    curve_radius: float,
    *,
    require_short_overlap: bool = True,
) -> Iterator[SameDestinationVerticalBundle]:
    """Yield short-run LEFT-entry cohorts with a complete geometric proof.

    These cohorts occupy an open multi-row void that has no single grid-gap
    identity, so the general iterator cannot name their channel.  Eligibility
    instead requires distinct horizontal-flow exits, complete destination-line
    membership, a positive but short shared vertical run, sufficient landing
    suffix, and conflict-free adjacent slots proven by the proposal pass.
    """
    exits: dict[int, str] = {}

    def admit(route: RoutedPath, tail: PeeloffTail, port: Port) -> bool:
        exit_id = _flow_exit_feeding_route_source(graph, route)
        if exit_id is None:
            return False
        exit_port = graph.ports.get(exit_id)
        if (
            port.side is not PortSide.LEFT
            or exit_port is None
            or exit_port.side is not PortSide.RIGHT
            or tail.trunk_sign != 1
            or tail.port_lead_sign != 1
        ):
            return False
        exits[id(route)] = exit_id
        return True

    minimum_run = 2 * curve_radius
    for cohort in _iter_same_destination_cohorts(routes, graph, step, admit):
        if len({exits[id(route)] for route, _tail in cohort.entries}) != len(
            cohort.per_line
        ):
            continue
        if cohort.suffix_run < minimum_run - COORD_TOLERANCE:
            continue
        overlap = cohort.vertical_overlap
        if overlap <= COORD_TOLERANCE or (
            require_short_overlap and overlap >= minimum_run - COORD_TOLERANCE
        ):
            continue
        if _cohort_gap_ids(graph, cohort.per_line) - {None}:
            continue
        yield cohort.bundle()


def same_destination_approach_slots(
    bundle: SameDestinationVerticalBundle,
    graph: MetroGraph,
    step: float,
) -> dict[str, PeeloffSlot]:
    """Map destination lane order onto adjacent final vertical channels."""
    port = graph.ports[bundle.port_id]
    n = len(bundle.per_line)
    x_slots = port_approach_channel_xs(
        (tail.peel_x for tail in bundle.per_line.values()),
        step,
        inner_is_max=port.side is PortSide.LEFT,
    )
    y_slots = sorted(tail.port_y for tail in bundle.per_line.values())
    port_order = sorted(
        bundle.per_line,
        key=lambda line_id: bundle.per_line[line_id].port_y,
    )
    x_follows_port = bundle.vertical_sign == -bundle.port_lead_sign
    return {
        line_id: PeeloffSlot(
            x_slots[rank if x_follows_port else n - rank - 1],
            y_slots[rank],
            rank if x_follows_port else n - rank - 1,
        )
        for rank, line_id in enumerate(port_order)
    }


@dataclass(frozen=True, slots=True)
class SameDestinationApproachProposal:
    """One route's complete proposed geometry and symbolic gap declarations."""

    route: RoutedPath
    points: tuple[tuple[float, float], ...]
    gap_slots: tuple[GapSlot, ...]


def _same_destination_edge_identity(
    route: RoutedPath, route_rank: int
) -> tuple[str, str, str, int]:
    return (
        route.edge.source,
        route.edge.target,
        route.edge.line_id,
        route_rank,
    )


def _conflict_quantise(value: float) -> int:
    return round(value / COORD_TOLERANCE)


def _points_coincide(first: tuple[float, float], second: tuple[float, float]) -> bool:
    return (
        abs(first[0] - second[0]) <= COORD_TOLERANCE
        and abs(first[1] - second[1]) <= COORD_TOLERANCE
    )


def segments_properly_cross(
    a0: tuple[float, float],
    a1: tuple[float, float],
    b0: tuple[float, float],
    b1: tuple[float, float],
    *,
    reject_collinear_touch: bool,
) -> tuple[float, float] | None:
    """The interior intersection of segments ``a0a1`` and ``b0b1``, or ``None``.

    With *reject_collinear_touch* an endpoint sitting on the other segment's
    supporting line counts as no crossing; without it only a strict sign change
    on both sides is required, which admits such a T-touch.
    """

    def cross(
        origin: tuple[float, float],
        end: tuple[float, float],
        point: tuple[float, float],
    ) -> float:
        return (end[0] - origin[0]) * (point[1] - origin[1]) - (end[1] - origin[1]) * (
            point[0] - origin[0]
        )

    d1, d2 = cross(b0, b1, a0), cross(b0, b1, a1)
    d3, d4 = cross(a0, a1, b0), cross(a0, a1, b1)
    if reject_collinear_touch:
        if d1 * d2 >= 0 or d3 * d4 >= 0:
            return None
    elif (d1 > 0) == (d2 > 0) or (d3 > 0) == (d4 > 0):
        return None
    denominator = (a0[0] - a1[0]) * (b0[1] - b1[1]) - (a0[1] - a1[1]) * (b0[0] - b1[0])
    if abs(denominator) <= COORD_TOLERANCE:
        return None
    position = (
        (a0[0] - b0[0]) * (b0[1] - b1[1]) - (a0[1] - b0[1]) * (b0[0] - b1[0])
    ) / denominator
    return (
        a0[0] + position * (a1[0] - a0[0]),
        a0[1] + position * (a1[1] - a0[1]),
    )


def _proper_segment_crossing(
    a0: tuple[float, float],
    a1: tuple[float, float],
    b0: tuple[float, float],
    b1: tuple[float, float],
) -> tuple[float, float] | None:
    """Return a strict interior crossing, excluding every shared endpoint."""
    if any(
        _points_coincide(first, second) for first in (a0, a1) for second in (b0, b1)
    ):
        return None
    return segments_properly_cross(a0, a1, b0, b1, reject_collinear_touch=True)


def _positive_collinear_overlap(
    a0: tuple[float, float],
    a1: tuple[float, float],
    b0: tuple[float, float],
    b1: tuple[float, float],
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Return the positive-length overlap endpoints of two collinear segments."""
    dx, dy = a1[0] - a0[0], a1[1] - a0[1]
    if abs(dx) <= COORD_TOLERANCE and abs(dy) <= COORD_TOLERANCE:
        return None
    cross_b0 = dx * (b0[1] - a0[1]) - dy * (b0[0] - a0[0])
    cross_b1 = dx * (b1[1] - a0[1]) - dy * (b1[0] - a0[0])
    if abs(cross_b0) > COORD_TOLERANCE or abs(cross_b1) > COORD_TOLERANCE:
        return None
    axis = 0 if abs(dx) >= abs(dy) else 1
    lo = max(min(a0[axis], a1[axis]), min(b0[axis], b1[axis]))
    hi = min(max(a0[axis], a1[axis]), max(b0[axis], b1[axis]))
    if hi - lo <= COORD_TOLERANCE:
        return None
    delta = dx if axis == 0 else dy

    def point_at(coordinate: float) -> tuple[float, float]:
        fraction = (coordinate - a0[axis]) / delta
        return (
            a0[0] + fraction * dx,
            a0[1] + fraction * dy,
        )

    return point_at(lo), point_at(hi)


def _same_destination_conflicts(
    routes: Sequence[RoutedPath],
    points_by_route: Mapping[int, Sequence[tuple[float, float]]],
    relevant_route_ids: frozenset[int],
) -> set[tuple[object, ...]]:
    """Stable conflicts involving a proposed route against the full population."""
    conflicts: set[tuple[object, ...]] = set()
    for route_rank, route in enumerate(routes):
        points = points_by_route.get(id(route), route.points)
        identity = _same_destination_edge_identity(route, route_rank)
        if id(route) in relevant_route_ids:
            for first_rank, (a0, a1) in enumerate(zip(points, points[1:])):
                for second_rank in range(first_rank + 2, len(points) - 1):
                    b0, b1 = points[second_rank : second_rank + 2]
                    if crossing := _proper_segment_crossing(a0, a1, b0, b1):
                        conflicts.add(
                            (
                                "self-cross",
                                identity,
                                first_rank,
                                second_rank,
                                *map(_conflict_quantise, crossing),
                            )
                        )
                    if overlap := _positive_collinear_overlap(a0, a1, b0, b1):
                        conflicts.add(
                            (
                                "self-overlap",
                                identity,
                                first_rank,
                                second_rank,
                                tuple(
                                    tuple(map(_conflict_quantise, point))
                                    for point in overlap
                                ),
                            )
                        )

        for other_rank in range(route_rank + 1, len(routes)):
            other = routes[other_rank]
            if (
                id(route) not in relevant_route_ids
                and id(other) not in relevant_route_ids
            ):
                continue
            other_points = points_by_route.get(id(other), other.points)
            other_identity = _same_destination_edge_identity(other, other_rank)
            for first_rank, (a0, a1) in enumerate(zip(points, points[1:])):
                for second_rank, (b0, b1) in enumerate(
                    zip(other_points, other_points[1:])
                ):
                    if crossing := _proper_segment_crossing(a0, a1, b0, b1):
                        conflicts.add(
                            (
                                "cross",
                                identity,
                                first_rank,
                                other_identity,
                                second_rank,
                                *map(_conflict_quantise, crossing),
                            )
                        )
                    if route.line_id == other.line_id:
                        continue
                    if overlap := _positive_collinear_overlap(a0, a1, b0, b1):
                        conflicts.add(
                            (
                                "overlap",
                                identity,
                                first_rank,
                                other_identity,
                                second_rank,
                                tuple(
                                    tuple(map(_conflict_quantise, point))
                                    for point in overlap
                                ),
                            )
                        )
    return conflicts


def feasible_same_destination_approach_proposals(
    graph: MetroGraph,
    routes: Sequence[RoutedPath],
    bundle: SameDestinationVerticalBundle,
    slots: Mapping[str, PeeloffSlot],
    *,
    movable_route_ids: frozenset[int] | None = None,
    segment_move_is_blocked: Callable[[RoutedPath, int, float], bool] | None = None,
    replannable_exit_turn_plan_ids: frozenset[str] = frozenset(),
) -> tuple[SameDestinationApproachProposal, ...] | None:
    """Return an atomic tail move when it introduces no ownership or geometry defect."""
    proposals: list[SameDestinationApproachProposal] = []
    proposed_points: dict[int, Sequence[tuple[float, float]]] = {}

    def tail_segment_is_held(
        route: RoutedPath, segment_rank: int, target_x: float
    ) -> bool:
        """Whether some frozen decision forbids moving this tail's riser.

        Held by the caller's own movable set, by an exit-turn plan that no
        replan may disturb, by the route system's segment ownership, by a
        reservation, or by the caller's own veto.
        """
        return (
            (movable_route_ids is not None and id(route) not in movable_route_ids)
            or (
                planner_owns_segment(route, segment_rank)
                and (
                    route.exit_turn_plan_id not in replannable_exit_turn_plan_ids
                    or convergence_owns_segment_boundary(route, segment_rank)
                    or route.fan_route_emitter is not None
                    or segment_rank in route.route_system_owned_segment_ranks
                )
            )
            or route_system_owns_segment_boundary(route, segment_rank)
            or bool(route.route_reservation_ids)
            or (
                segment_move_is_blocked is not None
                and segment_move_is_blocked(route, segment_rank, target_x)
            )
        )

    for route, tail in bundle.entries:
        segment_rank = len(route.points) - 3
        target_x = slots[route.line_id].peel_x
        moves = abs(tail.peel_x - target_x) > COORD_TOLERANCE
        if moves and tail_segment_is_held(route, segment_rank, target_x):
            return None
        own_sections = {
            graph.section_for_station(route.edge.source),
            graph.section_for_station(route.edge.target),
        }
        if moves and _section_intrudes(
            graph,
            target_x,
            min(tail.trunk_y, tail.port_y),
            max(tail.trunk_y, tail.port_y),
            exclude=own_sections,
            margin=EDGE_TO_BUNDLE_CLEARANCE,
        ):
            return None

        points = list(route.points)
        points[segment_rank] = (target_x, points[segment_rank][1])
        points[segment_rank + 1] = (target_x, points[segment_rank + 1][1])
        y_lo = min(tail.trunk_y, tail.port_y)
        y_hi = max(tail.trunk_y, tail.port_y)
        old_gap = gap_lo_for_x(graph, tail.peel_x, y_lo, y_hi)
        new_gap = gap_lo_for_x(graph, target_x, y_lo, y_hi)
        if old_gap is not None and new_gap is None:
            return None
        corridor_band = corridor_clearance_band(
            graph,
            axis=0,
            section_ids=tuple(
                section_id for section_id in own_sections if section_id is not None
            ),
            coordinate=target_x,
            run_start=y_lo,
            run_end=y_hi,
        )
        if corridor_band is not None and not (
            corridor_band.lo - COORD_TOLERANCE
            <= target_x
            <= corridor_band.hi + COORD_TOLERANCE
        ):
            return None
        gap_slots = list(route.gap_slots)
        if new_gap is not None and new_gap != old_gap:
            gap_slots = [
                slot
                for slot in gap_slots
                if not (
                    old_gap is not None
                    and slot.gap_lo_col == old_gap[0]
                    and (slot.direction is Direction.D) == (tail.vertical_sign > 0)
                )
            ]
            gap_slots.append(
                GapSlot(
                    new_gap[0],
                    new_gap[0] + 1,
                    new_gap[1],
                    Direction.D if tail.vertical_sign > 0 else Direction.U,
                    0,
                    1,
                )
            )
        proposal = SameDestinationApproachProposal(
            route,
            tuple(points),
            tuple(gap_slots),
        )
        proposals.append(proposal)
        proposed_points[id(route)] = proposal.points

    if all(proposal.points == tuple(proposal.route.points) for proposal in proposals):
        # A proposal that reproduces its route's own points cannot fail either
        # scan below: the clearance walk reads geometry already drawn, and the
        # conflict pair is a delta against that same geometry.
        return tuple(proposals)

    relevant_route_ids = frozenset(proposed_points)
    offset_step = graph_offset_step(graph)
    for route, _tail in bundle.entries:
        proposal_points = proposed_points[id(route)]
        segment_rank = len(proposal_points) - 3
        start, end = proposal_points[segment_rank : segment_rank + 2]
        candidate_lo, candidate_hi = sorted((start[1], end[1]))
        candidate_down = end[1] > start[1]
        for other in routes:
            other_points = proposed_points.get(id(other), other.points)
            for other_rank, (other_start, other_end) in enumerate(
                zip(other_points, other_points[1:])
            ):
                if other is route and other_rank == segment_rank:
                    continue
                if abs(other_start[0] - other_end[0]) > COORD_TOLERANCE:
                    continue
                other_lo, other_hi = sorted((other_start[1], other_end[1]))
                if (
                    min(candidate_hi, other_hi) - max(candidate_lo, other_lo)
                    <= MIN_CORRIDOR_Y_OVERLAP
                ):
                    continue
                required = cotravelling_lane_clearance(
                    same_line=route.line_id == other.line_id,
                    counter_running=candidate_down
                    is not (other_end[1] > other_start[1]),
                    curve_radius=CURVE_RADIUS,
                    offset_step=offset_step,
                )
                if (
                    required > 0.0
                    and abs(start[0] - other_start[0]) < required - COORD_TOLERANCE_FINE
                ):
                    return None

    baseline_conflicts = _same_destination_conflicts(routes, {}, relevant_route_ids)
    proposed_conflicts = _same_destination_conflicts(
        routes, proposed_points, relevant_route_ids
    )
    if proposed_conflicts - baseline_conflicts:
        return None
    return tuple(proposals)


def iter_opposing_entry_confluences(
    routes: list[RoutedPath],
    graph: MetroGraph,
    step: float,
    *,
    min_common_approach: float = 2 * CURVE_RADIUS,
) -> Iterator[OpposingEntryConfluence]:
    """Yield complete entry groups that bundle after approaching from both sides.

    The routes must enter one side port through the same vertical direction,
    occupy one contiguous channel band, and share a substantial vertical run.
    Requiring the complete port line set keeps route-system ownership atomic.
    """
    by_shape: dict[
        tuple[str, int, int],
        list[tuple[RoutedPath, PeeloffTail]],
    ] = defaultdict(list)
    for route in routes:
        tail = port_peeloff_tail(route)
        port = graph.ports.get(route.edge.target)
        if (
            tail is None
            or port is None
            or not port.is_entry
            or port.side not in (PortSide.LEFT, PortSide.RIGHT)
        ):
            continue
        by_shape[route.edge.target, tail.vertical_sign, tail.port_lead_sign].append(
            (route, tail)
        )

    for (port_id, vertical_sign, port_lead_sign), entries in by_shape.items():
        per_line: dict[str, PeeloffTail] = {}
        for route, tail in entries:
            per_line.setdefault(route.line_id, tail)
        n = len(per_line)
        if n < 2 or len(entries) != n:
            continue
        if len({tail.trunk_sign for tail in per_line.values()}) < 2:
            continue
        if set(per_line) != set(graph.station_lines(port_id)):
            continue
        port_ys = sorted(tail.port_y for tail in per_line.values())
        if any(
            right - left <= COORD_TOLERANCE for left, right in zip(port_ys, port_ys[1:])
        ):
            continue
        if port_ys[-1] - port_ys[0] > (n - 1) * step + COORD_TOLERANCE:
            continue
        peel_xs = [tail.peel_x for tail in per_line.values()]
        if max(peel_xs) - min(peel_xs) > (n - 1) * step + COORD_TOLERANCE:
            continue
        shared_lo = max(min(tail.trunk_y, tail.port_y) for tail in per_line.values())
        shared_hi = min(max(tail.trunk_y, tail.port_y) for tail in per_line.values())
        if shared_hi - shared_lo < min_common_approach - COORD_TOLERANCE:
            continue
        yield OpposingEntryConfluence(
            port_id,
            entries,
            per_line,
            vertical_sign,
            port_lead_sign,
        )


def port_approach_channel_xs(
    realised_xs: Iterable[float], step: float, *, inner_is_max: bool
) -> list[float]:
    """Ascending *step*-pitch channel band for one port-approach bundle.

    The band is anchored on the realised channel nearest the port, whose
    standoff from the port's own box edge is what the emitting handler sized;
    the remaining lanes pack in beside it at the bundle pitch.
    """
    xs = list(realised_xs)
    n = len(xs)
    if inner_is_max:
        inner_x = max(xs)
        return [inner_x - (n - rank - 1) * step for rank in range(n)]
    inner_x = min(xs)
    return [inner_x + rank * step for rank in range(n)]


def opposing_entry_confluence_slots(
    bundle: OpposingEntryConfluence,
    graph: MetroGraph,
    step: float,
) -> dict[str, PeeloffSlot]:
    """Map port lane order onto the bundle's preceding vertical channels."""
    port = graph.ports[bundle.port_id]
    n = len(bundle.per_line)
    x_slots = port_approach_channel_xs(
        (tail.peel_x for tail in bundle.per_line.values()),
        step,
        inner_is_max=port.side is PortSide.LEFT,
    )
    y_slots = sorted(tail.port_y for tail in bundle.per_line.values())
    port_order = sorted(
        bundle.per_line,
        key=lambda line_id: bundle.per_line[line_id].port_y,
    )
    x_follows_port = bundle.vertical_sign == -bundle.port_lead_sign
    return {
        line_id: PeeloffSlot(
            x_slots[rank if x_follows_port else n - rank - 1],
            y_slots[rank],
            rank if x_follows_port else n - rank - 1,
        )
        for rank, line_id in enumerate(port_order)
    }


class DestinationTailTrunk(NamedTuple):
    """Horizontal trunk geometry for one destination-tail bundle member."""

    route: RoutedPath
    idx: int
    y: float
    x_lo: float
    x_hi: float
    sign_x: int


def trunk_depths_contiguous(trunk_ys: list[float], n: int, step: float) -> bool:
    """Whether ``n`` trunk depths span at most one concentric bundle width.

    The members of a single concentric bundle sit within ``(n-1)*step`` of one
    another; a wider span is two channels rows apart, not one bundle.
    """
    return max(trunk_ys) - min(trunk_ys) <= (n - 1) * step + COORD_TOLERANCE


def iter_port_peeloff_bundles(
    routes: list[RoutedPath],
    graph: MetroGraph,
    step: float,
    *,
    require_contiguous: bool = True,
    min_common_suffix: float = 2 * CURVE_RADIUS,
) -> Iterator[PortPeeloffBundle]:
    """Yield each destination-facing peel-off bundle into a side entry port.

    Members terminate at one port through the same ``H-V-H`` tail shape and
    share at least ``min_common_suffix`` of destination-facing horizontal
    corridor.  With ``require_contiguous`` they must already occupy one tight
    track-step band; the runtime guard disables that filter so a missing eager
    bundling pass cannot make the check vacuous.
    """
    by_shape: dict[
        tuple[str, int, int, int],
        list[tuple[RoutedPath, PeeloffTail]],
    ] = defaultdict(list)
    for rp in routes:
        tail = port_peeloff_tail(rp)
        if tail is None:
            continue
        port = graph.ports.get(rp.edge.target)
        if (
            port is None
            or not port.is_entry
            or port.side not in (PortSide.LEFT, PortSide.RIGHT)
        ):
            continue
        by_shape[
            (
                rp.edge.target,
                tail.trunk_sign,
                tail.vertical_sign,
                tail.port_lead_sign,
            )
        ].append((rp, tail))

    for shape, entries in by_shape.items():
        port_id, trunk_sign, vertical_sign, port_lead_sign = shape
        # One representative tail per distinct line (a line feeding several
        # approaches shares a single slot, so its approaches move together).
        per_line: dict[str, PeeloffTail] = {}
        for rp, t in entries:
            per_line.setdefault(rp.edge.line_id, t)
        n = len(per_line)
        if n < 2:
            continue
        if tail_overlap(per_line) < min_common_suffix - COORD_TOLERANCE:
            continue
        trunk_ys = sorted(t.trunk_y for t in per_line.values())
        if trunk_ys[-1] - trunk_ys[0] <= COORD_TOLERANCE:
            continue  # no distinct trunk depths to order by
        if require_contiguous and not trunk_depths_contiguous(trunk_ys, n, step):
            continue  # not one contiguous concentric bundle
        yield PortPeeloffBundle(
            port_id,
            entries,
            per_line,
            trunk_sign,
            vertical_sign,
            port_lead_sign,
        )


def peeloff_trunk_line_order(bundle: PortPeeloffBundle) -> list[str]:
    """Top-to-bottom trunk order implied by the destination port's line slots."""
    port_order = sorted(bundle.per_line, key=lambda lid: bundle.per_line[lid].port_y)
    if bundle.trunk_sign == bundle.port_lead_sign:
        return port_order
    return list(reversed(port_order))


def peeloff_target_slots(
    bundle: PortPeeloffBundle, step: float
) -> dict[str, PeeloffSlot]:
    """Map each line of *bundle* to the slot its trunk depth earns.

    Peel-X and port-Y use separate ranks.  A half-turn reverses the horizontal
    direction and therefore transposes port order relative to trunk order,
    while the first H-to-V turn independently decides whether approach X
    follows or reverses trunk order.

    Risers spread wider than one bundle width close up onto the pitch rather
    than merely permuting the Xs already realised: only this bundle's lines
    descend the corridor, so a band sized by the wider fan that emitted them
    holds lanes nothing runs in.  Closing up stays inside the bundle's own
    corridor; opening a *narrower* band up needs room outboard of it, which the
    separation stages own, so a fused band keeps its realised channels.  Port Y
    is the destination port's own line stack rather than this bundle's to
    choose, and stays as realised throughout.
    """
    per_line = bundle.per_line
    n = len(per_line)
    realised_xs = sorted(t.peel_x for t in per_line.values())
    x_slots = (
        port_approach_channel_xs(
            realised_xs, step, inner_is_max=bundle.port_lead_sign > 0
        )
        if realised_xs[-1] - realised_xs[0] > (n - 1) * step + COORD_TOLERANCE
        else realised_xs
    )
    y_slots = sorted(t.port_y for t in per_line.values())
    ranked = sorted(per_line, key=lambda lid: per_line[lid].trunk_y)
    x_follows_trunk = x_follows_trunk_direction(bundle.vertical_sign, bundle.trunk_sign)
    y_follows_trunk = bundle.port_lead_sign == bundle.trunk_sign
    return {
        lid: PeeloffSlot(
            x_slots[i if x_follows_trunk else n - 1 - i],
            y_slots[i if y_follows_trunk else n - 1 - i],
            i if x_follows_trunk else n - 1 - i,
        )
        for i, lid in enumerate(ranked)
    }


def iter_eligible_destination_tail_bundles(
    routes: list[RoutedPath],
    graph: MetroGraph,
    step: float,
    curve_radius: float,
) -> Iterator[
    tuple[PortPeeloffBundle, dict[str, DestinationTailTrunk], dict[str, float]]
]:
    """Yield same-port tails that can occupy one collision-free trunk band.

    Every member has the same H-V-H tail directions, a destination-facing
    common suffix at least two curve radii long, a unique line, and a candidate
    tight band that clears every section across its trunk and both flanking
    verticals. Rail routes retain their independent tracks.
    """
    all_trunks: list[DestinationTailTrunk] = []
    for route in routes:
        if not route.is_inter_section:
            continue
        for idx, segment in iter_horizontal_trunks(route):
            all_trunks.append(
                DestinationTailTrunk(
                    route=route,
                    idx=idx,
                    y=segment.y,
                    x_lo=segment.x_lo,
                    x_hi=segment.x_hi,
                    sign_x=1 if segment.xb > segment.xa else -1,
                )
            )
    trunks_by_route = {id(trunk.route): trunk for trunk in all_trunks}

    for bundle in iter_port_peeloff_bundles(
        routes,
        graph,
        step,
        require_contiguous=False,
        min_common_suffix=2 * curve_radius,
    ):
        if len(bundle.entries) != len(bundle.per_line):
            continue
        if any(
            graph.station_is_rail(route.edge.source)
            or graph.station_is_rail(route.edge.target)
            for route, _tail in bundle.entries
        ):
            continue
        trunks = {
            route.line_id: trunks_by_route[id(route)]
            for route, _tail in bundle.entries
            if id(route) in trunks_by_route
        }
        if len(trunks) != len(bundle.per_line):
            continue
        if (
            all(route.normalize_exempt for route, _tail in bundle.entries)
            and len({route.edge.source for route, _tail in bundle.entries}) == 1
        ):
            continue

        order = peeloff_trunk_line_order(bundle)
        rank_of = {line_id: rank for rank, line_id in enumerate(order)}
        group_routes = {id(trunk.route) for trunk in trunks.values()}
        pinned_bases: list[float] = []
        for line_id, trunk in trunks.items():
            if any(
                id(sibling.route) not in group_routes
                and sibling.route.line_id == line_id
                and sibling.sign_x == trunk.sign_x
                and abs(sibling.y - trunk.y) <= COORD_TOLERANCE
                and min(sibling.x_hi, trunk.x_hi) - max(sibling.x_lo, trunk.x_lo)
                > COORD_TOLERANCE
                for sibling in all_trunks
            ):
                pinned_bases.append(trunk.y - rank_of[line_id] * step)
        if pinned_bases and any(
            abs(base - pinned_bases[0]) > COORD_TOLERANCE for base in pinned_bases[1:]
        ):
            continue
        base = pinned_bases[0] if pinned_bases else min(t.y for t in trunks.values())
        targets = {line_id: base + rank * step for rank, line_id in enumerate(order)}

        clear = True
        for route, _tail in bundle.entries:
            trunk = trunks[route.line_id]
            target_y = targets[route.line_id]
            points = route.points
            k = trunk.idx
            if k == 0 or k + 2 >= len(points):
                clear = False
                break
            own_sections = {
                section_id
                for station_id in (route.edge.source, route.edge.target)
                if (section_id := graph.section_for_station(station_id)) is not None
            }
            xa, xb = points[k][0], points[k + 1][0]
            source_section_id = graph.section_for_station(route.edge.source)
            source_section = (
                graph.sections.get(source_section_id) if source_section_id else None
            )
            horizontal_blocked = source_section is not None and (
                _h_segment_penetrates_section(
                    min(xa, xb),
                    max(xa, xb),
                    target_y,
                    source_section,
                    0.0,
                )
            )
            if not horizontal_blocked:
                horizontal_blocked = any(
                    section.id not in own_sections
                    and _h_segment_penetrates_section(
                        min(xa, xb), max(xa, xb), target_y, section, 0.0
                    )
                    for section in graph.sections.values()
                )

            def vertical_blocked(x: float, y1: float, y2: float) -> bool:
                y_lo, y_hi = sorted((y1, y2))
                return any(
                    section.bbox_w > 0
                    and section.id not in own_sections
                    and y_lo < section.bbox_y + section.bbox_h
                    and section.bbox_y < y_hi
                    and section.bbox_x <= x <= section.bbox_x + section.bbox_w
                    for section in graph.sections.values()
                )

            if (
                horizontal_blocked
                or vertical_blocked(xa, points[k - 1][1], target_y)
                or vertical_blocked(xb, target_y, points[k + 2][1])
            ):
                clear = False
                break
        if clear:
            yield bundle, trunks, targets


def tail_on_slot(tail: PeeloffTail, slot: PeeloffSlot) -> bool:
    """Whether a peel-off riser already sits on its depth-earned slot.

    True when the riser's realized peel-x and port-slot Y both match *slot*
    within tolerance.  The reordering pass skips a bundle whose every member is
    on slot; the runtime guard flags a member that is not.
    """
    return (
        abs(slot.peel_x - tail.peel_x) <= COORD_TOLERANCE
        and abs(slot.port_y - tail.port_y) <= COORD_TOLERANCE
    )


def seat_peeloff_port_y(rp: RoutedPath, port_y: float) -> None:
    """Move a peel-off riser's port lead onto *port_y*.

    The riser turn and the lead into the port -- the last two waypoints of a
    :func:`port_peeloff_tail` (``... -> (peel_x, port_y) -> (ex, port_y)``) --
    drop to *port_y*, keeping their Xs.  Owns the tail's waypoint layout so a
    caller re-seating the port slot need not index into the points.
    """
    pts = rp.points
    pts[-2] = (pts[-2][0], port_y)
    pts[-1] = (pts[-1][0], port_y)


def _vert_horiz_cross(
    vx: float, vy0: float, vy1: float, hy: float, hx0: float, hx1: float
) -> bool:
    """True when a vertical segment crosses a horizontal one in their interior.

    Shared-endpoint touches (T-junctions, corners) are excluded: the crossing
    point must lie strictly inside both segments.
    """
    lo, hi = min(vy0, vy1), max(vy0, vy1)
    xlo, xhi = min(hx0, hx1), max(hx0, hx1)
    return (
        xlo + COORD_TOLERANCE < vx < xhi - COORD_TOLERANCE
        and lo + COORD_TOLERANCE < hy < hi - COORD_TOLERANCE
    )


def trunk_segment_crossings(
    a: HTrunkSeg, b: HTrunkSeg
) -> tuple[tuple[float, float], ...]:
    """Return every riser/trunk crossing between two horizontal trunks."""
    crossings: list[tuple[float, float]] = []
    for seg, other in ((a, b), (b, a)):
        for vx, vy in ((seg.xa, seg.before_y), (seg.xb, seg.after_y)):
            crossing = (vx, other.y)
            if (
                _vert_horiz_cross(vx, seg.y, vy, other.y, other.x_lo, other.x_hi)
                and crossing not in crossings
            ):
                crossings.append(crossing)
    return tuple(crossings)


def trunk_segments_cross(a: HTrunkSeg, b: HTrunkSeg) -> tuple[float, float] | None:
    """Return the first crossing between trunks *a* and *b*, if any.

    A crossing is a riser of one trunk piercing the horizontal run of the
    other (the two parallel runs themselves never cross).  Returns the first
    crossing point found.
    """
    return next(iter(trunk_segment_crossings(a, b)), None)


def inter_row_gap_band(graph: MetroGraph, y: float) -> tuple[float, float] | None:
    """Return the ``(top, bottom)`` Y envelope of the inter-row gap holding *y*.

    Scans adjacent grid rows for the gap whose ``[row_bottom, next_row_top]``
    band contains *y*; returns ``None`` when *y* does not fall in any gap.
    """
    for _upper, top, bottom in iter_inter_row_gaps(graph):
        if top - COORD_TOLERANCE <= y <= bottom + COORD_TOLERANCE:
            return top, bottom
    return None


class ExemptDoglegLanes(NamedTuple):
    """The two candidate placements of a movable trunk off an exempt run.

    A trunk fused onto a ``normalize_exempt`` run can seat one corridor
    ``separation`` below or above it, each clamped to the inter-row gap band.
    ``below_crossing``/``above_crossing`` hold that placement's first
    riser/run crossing with the exempt run, or ``None`` when the placement is a
    clean parallel bundle; ``below_ok``/``above_ok`` record whether the band
    admits it.
    """

    below_y: float
    above_y: float
    below_ok: bool
    above_ok: bool
    below_crossing: tuple[float, float] | None
    above_crossing: tuple[float, float] | None


def exempt_dogleg_lanes(
    movable: HTrunkSeg,
    exempt: HTrunkSeg,
    *,
    separation: float,
    band: tuple[float, float] | None,
) -> ExemptDoglegLanes:
    """Resolve the below/above lanes a movable trunk can take off an exempt run.

    The two candidate placements sit one *separation* either side of the exempt
    run at ``exempt.y``; *band* clamps each to the inter-row gap so a placement
    that would foul the next row's header badge is marked infeasible.
    ``_dogleg_off_exempt_trunks`` picks the crossing-free side from this, and the
    dogleg-crossing invariant reads it to tell a forced transit (both sides
    cross) from an avoidable wrong-side landing.
    """
    below = exempt.y + separation
    above = exempt.y - separation
    if band is not None:
        top, bottom = band
        below_ok = below <= bottom - SECTION_HEADER_PROTRUSION
        above_ok = above >= top
    else:
        below_ok = above_ok = True
    below_seg = HTrunkSeg(
        below, movable.xa, movable.xb, movable.before_y, movable.after_y
    )
    above_seg = HTrunkSeg(
        above, movable.xa, movable.xb, movable.before_y, movable.after_y
    )
    return ExemptDoglegLanes(
        below_y=below,
        above_y=above,
        below_ok=below_ok,
        above_ok=above_ok,
        below_crossing=trunk_segments_cross(below_seg, exempt),
        above_crossing=trunk_segments_cross(above_seg, exempt),
    )


def compute_bundle_info(
    graph: MetroGraph,
    junction_ids: set[str],
    line_priority: dict[str, int],
    bottom_exit_junctions: set[str] | None = None,
) -> dict[tuple[str, str, str], tuple[int, int]]:
    """Pre-compute bundle assignments for inter-section edges.

    Groups inter-section edges that share the same geometric corridor
    (same vertical channel position and direction) and assigns consistent
    per-line positions within each bundle. This ensures lines traveling
    between sections are visually parallel with proper spacing, rather
    than overlapping at the same X coordinate.

    Returns dict mapping (source_id, target_id, line_id) -> (index, count).
    """
    # Collect all inter-section edges with their geometry
    inter_edges: list[tuple[Edge, float, float, float, float]] = []
    for edge in graph.edges:
        src, tgt = graph.edge_endpoints(edge)

        is_inter = (src.is_port or edge.source in junction_ids) and (
            tgt.is_port or edge.target in junction_ids
        )
        if not is_inter:
            continue

        inter_edges.append((edge, src.x, src.y, tgt.x, tgt.y))

    # Group by corridor: edges sharing the same vertical channel
    # Key: (route_type, rounded_channel_position, vertical_direction)
    corridor_groups: dict[
        tuple[object, ...], list[tuple[Edge, float, float, float, float]]
    ] = defaultdict(list)

    for item in inter_edges:
        edge, sx, sy, tx, ty = item
        dx = tx - sx
        dy = ty - sy

        if abs(dy) < COORD_TOLERANCE_FINE:
            continue  # Horizontal edges don't need bundling

        v_dir = 1 if dy > 0 else -1

        if abs(dx) < COORD_TOLERANCE:
            # Vertical: group by shared X position
            key: tuple[object, ...] = ("V", round(sx), v_dir)
        else:
            # L-shaped: group by the inter-column gap the vertical
            # channel will occupy.  Use (src_col, tgt_col) when
            # section info is available so that edges from different
            # ports in the same column share one bundle and get
            # proper offsets.  Fall back to round(sx) for junctions
            # or edges without section info.
            h_dir = 1 if dx > 0 else -1
            src_st, tgt_st = graph.edge_endpoints(edge)
            src_sec = (
                graph.sections.get(src_st.section_id) if src_st.section_id else None
            )
            tgt_sec = (
                graph.sections.get(tgt_st.section_id) if tgt_st.section_id else None
            )
            col_key: int | tuple[int, ...]
            if src_sec and tgt_sec and src_sec.grid_col != tgt_sec.grid_col:
                # Include both rows: two cross-column wraps sharing a column pair
                # but stacked in different inter-row gaps (a serpentine taller
                # than 2x2) descend the same channel X at different Y bands and
                # are distinct corridors, not one interleaved bundle.
                col_key = (
                    src_sec.grid_col,
                    tgt_sec.grid_col,
                    src_sec.grid_row,
                    tgt_sec.grid_row,
                )
            elif tgt_sec:
                # Source is a junction: include target column AND row so
                # edges to different sections get separate bundles.  A
                # junction can fan to two targets in the same column but
                # different rows; those are distinct corridors and must not be
                # conflated into one over-wide interleaved bundle.
                col_key = (round(sx), tgt_sec.grid_col, tgt_sec.grid_row)
            else:
                col_key = round(sx)
            key = ("L", col_key, v_dir, h_dir)

        corridor_groups[key].append(item)

    # Assign per-line positions within each corridor
    assignments: dict[tuple[str, str, str], tuple[int, int]] = {}

    for _key, group in corridor_groups.items():
        # Sort by spatial ordering so the bundle's visual position
        # is preserved around corners.
        source_ids = {e[0].source for e in group}
        if len(source_ids) == 1:
            exit_port_id = group[0][0].source
            if bottom_exit_junctions and exit_port_id in bottom_exit_junctions:
                # Vertical-first: longest drop (largest target Y) is
                # outermost (i=0) to prevent crossings at corners.
                group.sort(
                    key=lambda e: (
                        -e[4],
                        line_priority.get(e[0].line_id, DEFAULT_LINE_PRIORITY),
                    )
                )
            elif (port := graph.ports.get(exit_port_id)) and not port.is_entry:
                source_y = line_source_y_at_port(exit_port_id, graph)
                group.sort(
                    key=lambda e: (
                        source_y.get(e[0].line_id, 0),
                        line_priority.get(e[0].line_id, DEFAULT_LINE_PRIORITY),
                    )
                )
            else:
                group.sort(
                    key=lambda e: line_priority.get(e[0].line_id, DEFAULT_LINE_PRIORITY)
                )
        else:
            # Fan-in: edges from different source ports. Sort by
            # actual source Y position to preserve spatial ordering
            # around the L-shaped corner.
            group.sort(
                key=lambda e: (
                    e[2],
                    line_priority.get(e[0].line_id, DEFAULT_LINE_PRIORITY),
                )
            )

        n = len(group)
        for i, (edge, *_rest) in enumerate(group):
            assignments[(edge.source, edge.target, edge.line_id)] = (i, n)

    return assignments


def inter_column_channel_x(
    graph: MetroGraph,
    src: Station,
    tgt: Station,
    sx: float,
    dx: float,
    max_r: float,
    offset_step: float,
    reserved: ReservedBands | None = None,
) -> float:
    """Compute the X position for a vertical channel in an L-shaped route.

    Places the channel in the gap between columns so it doesn't pass
    through sibling sections stacked in the source's column. Falls
    back to near-source placement when section info is unavailable.
    """
    src_sec = graph.sections.get(src.section_id) if src.section_id else None
    tgt_sec = graph.sections.get(tgt.section_id) if tgt.section_id else None

    if src_sec and tgt_sec and src_sec.grid_col != tgt_sec.grid_col:
        return centre_inter_column_channel(
            graph, src_sec.grid_col, tgt_sec.grid_col, reserved=reserved
        )

    # Extend the same gap-centred placement to junction endpoints (whose
    # section is found by tracing the junction graph) so a junction-sourced
    # L-shape centres in the inter-column gap instead of hugging one edge.
    # Restrict to ADJACENT resolved columns: in staggered layouts a
    # junction can resolve several columns from its target, and centring in
    # that far span would drag the channel through empty canvas.
    res_src = src_sec or resolve_section(graph, src)
    res_tgt = tgt_sec or resolve_section(graph, tgt)
    if res_src and res_tgt and abs(res_src.grid_col - res_tgt.grid_col) == 1:
        return centre_inter_column_channel(
            graph, res_src.grid_col, res_tgt.grid_col, reserved=reserved
        )

    # Fallback: place near source (no resolvable adjacent column info)
    if dx > 0:
        return sx + max_r + offset_step
    else:
        return sx - max_r - offset_step


def endpoint_port_xs(graph: MetroGraph, edge: Edge) -> list[float]:
    """X of any port stations at *edge*'s endpoints (for edge-graze checks)."""
    xs: list[float] = []
    for sid in (edge.source, edge.target):
        st = graph.stations.get(sid)
        if st is not None and st.is_port:
            xs.append(st.x)
    return xs


def clear_channel_of_section_edge(
    graph: MetroGraph,
    mid_x: float,
    half_width: float,
    y_lo: float,
    y_hi: float,
    port_xs: list[float],
    edge_clearance: float = EDGE_TO_BUNDLE_CLEARANCE,
    port_tol: float = COORD_TOLERANCE,
    target_x: float | None = None,
) -> float:
    """Nudge a vertical channel out of an *incidental* section-edge graze.

    A descent channel of an inter-section route legitimately sits on a
    section edge when that edge carries a port at one of the route's
    endpoints (a port-to-port drop).  When the channel instead lands
    within *edge_clearance* of a section's bbox edge on the interior
    side, with no endpoint port at that x, the graze is incidental and
    the lines visibly cross the section border.

    *mid_x* is the channel's midline; the bundle's nearest line to a
    section edge sits at most *half_width* from *mid_x*.  *y_lo*/*y_hi*
    bound the vertical run so only sections it actually passes are
    considered.  Returns *mid_x* shifted just enough that the nearest
    line clears every incidentally-grazed edge by *edge_clearance*,
    pushing OUTWARD (away from the section interior).  Channels that
    coincide with an endpoint port (within *port_tol*) are left
    untouched.

    *target_x*, when given, is the route's target X.  The channel is
    pushed onto whichever side of the grazed section carries the target
    so the descent keeps heading toward it; the nearer edge is used only
    as a fallback when the target's X falls within the section's own span
    (so neither side is closer to it) or no target is supplied.

    Opposing sections stacked closer than ``2 * (edge_clearance +
    half_width)`` leave no midline that clears both, so the per-edge outward
    pushes cannot satisfy both and the channel ends up skimming one wall.  When
    the skimmed wall and the nearest wall on the far side bound a real
    (non-overlapping) gap, the channel is re-centred on that gap so the
    unavoidable shortfall is shared evenly rather than dumped on one edge.
    """
    adjusted = mid_x
    for sec in graph.sections.values():
        if sec.bbox_w <= 0:
            continue
        if y_hi < sec.bbox_y or y_lo > sec.bbox_y + sec.bbox_h:
            continue  # channel does not span this section's Y range
        left = sec.bbox_x
        right = left + sec.bbox_w
        if any(abs(adjusted - px) <= port_tol for px in port_xs):
            continue  # legitimate port-to-port drop on this edge
        # Bundle span (outermost lines either side of the midline).
        bundle_lo = adjusted - half_width
        bundle_hi = adjusted + half_width
        # A graze means the bundle's nearest line toward an edge does not
        # clear that edge by ``edge_clearance``.  Distance of the nearest
        # line to the right edge (positive = outside, to the right) and to
        # the left edge (positive = outside, to the left).
        clear_of_right = bundle_lo - right
        clear_of_left = left - bundle_hi
        # Only act when the bundle is near or inside this section's span;
        # a bundle comfortably outside both edges is fine.
        if clear_of_right >= edge_clearance or clear_of_left >= edge_clearance:
            continue
        # Push OUTWARD onto the target's side so the descent keeps heading
        # toward it; if the target sits within this section's span (or is
        # unknown) neither side is closer to it, so fall back to the nearer
        # edge.  Pushing right clears the right edge with the leftmost line;
        # pushing left clears the left edge with the rightmost line.
        push_right = (
            target_x >= right
            if target_x is not None and (target_x <= left or target_x >= right)
            else right - adjusted <= adjusted - left
        )
        if push_right:
            adjusted += edge_clearance - clear_of_right
        else:
            adjusted -= edge_clearance - clear_of_left

    if adjusted == mid_x:
        return adjusted  # nothing grazed, so nothing is being skimmed

    # A second pass over the *settled* position: which wall did the per-edge
    # pushes leave the channel skimming, and is there a real gap to re-centre in?
    bundle_lo = adjusted - half_width
    bundle_hi = adjusted + half_width
    left_skim = -math.inf  # nearest left wall the bundle skims within clearance
    right_skim = math.inf  # nearest right wall the bundle skims within clearance
    left_wall = -math.inf  # nearest wall to the channel's left, any distance
    right_wall = math.inf  # nearest wall to the channel's right, any distance
    for sec in graph.sections.values():
        if sec.bbox_w <= 0:
            continue
        if y_hi < sec.bbox_y or y_lo > sec.bbox_y + sec.bbox_h:
            continue
        left = sec.bbox_x
        right = left + sec.bbox_w
        if 0 <= bundle_lo - right < edge_clearance:
            left_skim = max(left_skim, right)
        elif 0 <= left - bundle_hi < edge_clearance:
            right_skim = min(right_skim, left)
        if right <= adjusted:
            left_wall = max(left_wall, right)
        if left >= adjusted:
            right_wall = min(right_wall, left)

    if left_skim > -math.inf and right_wall > left_skim:
        return (left_skim + right_wall) / 2
    if right_skim < math.inf and left_wall < right_skim:
        return (left_wall + right_skim) / 2
    return adjusted


def line_source_y_at_port(
    port_id: str,
    graph: MetroGraph,
) -> dict[str, float]:
    """Map line_id -> Y of connected internal station at an exit port.

    For an exit port, looks at edges going TO the port (station -> port)
    and returns the source station's Y position for each line.
    """
    line_y: dict[str, float] = {}
    for edge in graph.edges_to(port_id):
        src = graph.station_for_edge_source(edge)
        if not src.is_port:
            line_y[edge.line_id] = src.y
    return line_y


def point_on_polyline(
    point: tuple[float, float],
    pts: list[tuple[float, float]],
    tol: float = COORD_TOLERANCE,
) -> tuple[int, float] | None:
    """Locate *point* on a polyline within *tol* perpendicular distance.

    Returns ``(segment_idx, t)`` where ``segment_idx`` is the index of
    the segment's start vertex and ``t`` is the parameter along the
    segment in [0, 1].  Returns None when no segment covers the point.
    """
    for i in range(len(pts) - 1):
        ax, ay = pts[i]
        bx, by = pts[i + 1]
        dx, dy = bx - ax, by - ay
        seg_len2 = dx * dx + dy * dy
        if seg_len2 == 0:
            if abs(point[0] - ax) <= tol and abs(point[1] - ay) <= tol:
                return (i, 0.0)
            continue
        t = ((point[0] - ax) * dx + (point[1] - ay) * dy) / seg_len2
        if t < -0.01 or t > 1.01:
            continue
        t = max(0.0, min(1.0, t))
        proj_x = ax + t * dx
        proj_y = ay + t * dy
        if abs(point[0] - proj_x) <= tol and abs(point[1] - proj_y) <= tol:
            return (i, t)
    return None


def section_header_top(section: Section) -> float:
    """Y of a section's header badge top, which protrudes above the bbox."""
    return section.bbox_y - SECTION_HEADER_PROTRUSION


def section_header_safe_cap(section: Section) -> float:
    """Lowest Y a routing channel may occupy that clears the section's header
    badge by ``HEADER_CLEARANCE``."""
    return section_header_top(section) - HEADER_CLEARANCE


def section_ids_of_stations(graph: MetroGraph, *stations: Station) -> tuple[str, ...]:
    """The placed sections *stations* belong to, in order.

    A junction or free-standing endpoint belongs to no section and is dropped.
    Stated once because a corridor's grid span is measured over exactly these
    sections whether it is read from a route's endpoints after emission or from
    the two stations a handler holds before it: a handler seating a run against
    a span the settling pass would measure differently would seat it in the
    wrong band.
    """
    return tuple(
        station.section_id
        for station in stations
        if station.section_id in graph.sections
    )


def bypass_bottom_y(
    graph: MetroGraph,
    src_col: int,
    tgt_col: int,
    clearance: float = BYPASS_CLEARANCE,
    src_row: int | None = None,
    cross_row: bool = False,
    tgt_row: int | None = None,
    reserved: ReservedBands | None = None,
    claim: ReservedBand | None = None,
) -> float:
    """Bottom Y for a bypass route around intervening sections.

    When *cross_row* is True, the route must clear ALL sections in
    the column range (regardless of grid row) so it goes cleanly
    below everything.  Otherwise, when *src_row* is provided, only
    sections in the same row are considered so that bypass routes
    stay within their row.

    When *tgt_row* is the bottommost grid row, there is nothing below
    it to clear: routing below "everything" would dive past the canvas
    floor and then loop back up to the target's entry port.  In that
    case the channel is placed in the inter-row gap ABOVE the target
    row instead, so the route descends into that gap and approaches the
    entry without overshooting.

    When there are no intervening sections (adjacent-column bypass),
    falls back to the shorter of the source/target endpoint sections
    so the route hugs the smaller box rather than being pushed down
    by a tall neighbour.

    *claim* is the band this route's own reservation realises for its trunk.
    It holds the result unconditionally: the section edges consulted here are
    a proxy for the blockers the reservation measured over the corridor's own
    run, and where the two disagree -- including a candidate so deep it falls
    in no inter-row gap at all -- the allocation wins.
    """

    lo, hi = min(src_col, tgt_col), max(src_col, tgt_col)

    if cross_row:
        max_content_row = max_grid_row_with_content(graph)
        if tgt_row is not None and tgt_row == max_content_row and tgt_row > 0:
            # Target is in the bottommost row: route in the gap ABOVE it
            # rather than below the whole canvas (which would overshoot
            # and loop back up to the entry port).
            upper_bottom = row_bottom_edge(graph, tgt_row - 1, default=0.0)
            lower_top = row_top_edge(graph, tgt_row, default=upper_bottom)
            return held_in_reserved_band(
                _center_inter_row_channel(
                    upper_bottom,
                    lower_top,
                    reserved=None if reserved is None else reserved.at(tgt_row),
                ),
                claim,
            )
        # Route below ALL sections in the column range.
        all_in_range = [
            s
            for s in graph.sections.values()
            if s.bbox_w > 0 and lo <= s.grid_col <= hi
        ]
        if all_in_range:
            return held_in_reserved_band(
                max(s.bbox_y + s.bbox_h for s in all_in_range) + clearance, claim
            )
        return held_in_reserved_band(clearance, claim)

    def _in_row(s: Section) -> bool:
        return src_row is None or s.grid_row == src_row

    # Intervening sections (columns strictly between endpoints)
    intervening = [
        s
        for s in graph.sections.values()
        if s.bbox_w > 0 and lo < s.grid_col < hi and _in_row(s)
    ]
    max_intervening = max((s.bbox_y + s.bbox_h for s in intervening), default=0.0)

    if max_intervening > 0:
        candidate = max_intervening + clearance
    else:
        # No intervening sections: use the shorter endpoint section so
        # the bypass hugs tight instead of being pushed by the tall one.
        endpoints = [
            s
            for s in graph.sections.values()
            if s.bbox_w > 0 and s.grid_col in (lo, hi) and _in_row(s)
        ]
        if endpoints:
            candidate = max(s.bbox_y + s.bbox_h for s in endpoints) + clearance
        else:
            return held_in_reserved_band(clearance, claim)

    # Keep the bypass at least HEADER_CLEARANCE above any LOWER-row
    # section header_top; the stacked-line bundle otherwise crowds the
    # badge.  Midpoint fallback for inter-row gaps too tight to satisfy
    # both clearances (layout placement should normally prevent this).
    # Only sections in rows BELOW the source row constrain a bypass that
    # runs below the source row -- sections in rows above it sit far over
    # the bypass and clamping toward them would shove the channel up
    # through every intervening row.
    if src_row is not None:
        for s in graph.sections.values():
            if s.bbox_w > 0 and lo <= s.grid_col <= hi and s.grid_row > src_row:
                header_top = section_header_top(s)
                row_bottom = candidate - clearance
                safe_cap = section_header_safe_cap(s)
                if candidate > safe_cap:
                    if safe_cap >= row_bottom:
                        candidate = safe_cap
                    else:
                        candidate = (row_bottom + header_top) / 2

    return held_in_reserved_band(
        held_in_reserved_gap_band(graph, candidate, reserved), claim
    )


def held_in_reserved_gap_band(
    graph: MetroGraph, y: float, reserved: ReservedBands | None
) -> float:
    """*y* held inside the band reserved for whichever inter-row gap holds it.

    Section bottoms plus a clearance describe where a horizontal run may sit
    given the boxes stacked in the two rows; the reservation for the gap it
    lands in describes where the corridor's own blockers leave it room.  Where
    the two disagree the reservation wins.  A run that falls in no inter-row gap
    (a dive below the bottom row) claims no reservation and is left alone.
    """
    if reserved is None:
        return y
    upper = inter_row_gap_upper_row(graph, y)
    return y if upper is None else held_in_reserved_band(y, reserved.at(upper + 1))


def merge_trunk_force_cross_row(
    graph: MetroGraph,
    src_col: int,
    tgt_col: int,
    src_row: int | None,
    tgt_row: int | None,
) -> bool:
    """Whether a same-row merge trunk must route its bypass below ALL sections.

    A same-row trunk normally bypasses in the inter-row gap just below its
    row.  That gap also holds the next row's section title badges, so the
    shallow channel is forced below everything only when a lower-row section
    actually pokes up into it -- i.e. ``bypass_bottom_y``'s header-clearance
    clamp cannot keep the shallow channel clear of the section header.  When
    the gap has room, the shallow channel clears the header and diving below
    the whole canvas would loop needlessly deep.

    Both the routing context (branch drop level) and the trunk route consult
    this, so branches land at the Y the trunk actually runs.
    """
    if src_row is None or tgt_row != src_row:
        return False
    shallow = bypass_bottom_y(
        graph,
        src_col,
        tgt_col,
        BYPASS_CLEARANCE,
        src_row=src_row,
        cross_row=False,
        tgt_row=tgt_row,
    )
    # A lower section grazes the shallow channel only where bypass_bottom_y's
    # own clamp could not pull the channel down to section_header_safe_cap; the
    # tolerance keeps a sub-pixel near-miss from forcing a needless deep dive.
    lo, hi = min(src_col, tgt_col), max(src_col, tgt_col)
    return any(
        s.bbox_w > 0
        and s.grid_row > src_row
        and lo <= s.grid_col <= hi
        and shallow > section_header_safe_cap(s) + COORD_TOLERANCE
        for s in graph.sections.values()
    )


# ---------------------------------------------------------------------------
# Section resolution + inter-row channel placement
# ---------------------------------------------------------------------------


def junction_source_ports(graph: MetroGraph, junction_id: str) -> Iterator[Port]:
    """Yield, in edge order, the source ports feeding a fan-out junction.

    The single backwards hop through a fan-out junction (``section_id is None``)
    to the upstream exit ports on its incoming edges; a non-port source (a
    chained junction) is skipped, so each yielded port carries the side / entry
    / section a caller reads.
    """
    for edge in graph.edges_to(junction_id):
        port = graph.ports.get(edge.source)
        if port is not None:
            yield port


def _h_segment_penetrates_section(
    lo_x: float, hi_x: float, y: float, section: Section, margin: float = 0.0
) -> bool:
    """Whether a horizontal segment ``[lo_x, hi_x]`` at *y* penetrates *section*.

    Open-interior test: the segment must enter past the left edge and reach
    past the right edge (a boundary graze does not count).  ``y`` is inside
    when it falls within ``[bbox_y - margin, bbox_y + bbox_h + margin]``.
    """
    if section.bbox_w <= 0:
        return False
    right = section.bbox_x + section.bbox_w
    if hi_x <= section.bbox_x or lo_x >= right:
        return False
    return section.bbox_y - margin <= y <= section.bbox_y + section.bbox_h + margin


def _v_segment_crosses_other_section(
    graph: MetroGraph,
    x: float,
    y1: float,
    y2: float,
    exclude_section_ids: AbstractSet[str | None],
    margin: float = 0.0,
    *,
    include_margin_boundary: bool = True,
) -> bool:
    """Whether a vertical segment at *x* crosses a non-exempt section.

    The segment and each section use open Y interiors. The X test includes the
    expanded box boundary by default; callers policing a prospective move can
    request an open expanded boundary without changing the segment endpoints.
    """
    lo_y, hi_y = (y1, y2) if y1 <= y2 else (y2, y1)
    for section in graph.sections.values():
        if section.bbox_w <= 0 or section.id in exclude_section_ids:
            continue
        bottom = section.bbox_y + section.bbox_h
        if hi_y <= section.bbox_y or lo_y >= bottom:
            continue
        left = section.bbox_x - margin
        right = section.bbox_x + section.bbox_w + margin
        inside_x = left <= x <= right if include_margin_boundary else left < x < right
        if inside_x:
            return True
    return False


def _section_intrudes(
    graph: MetroGraph,
    x: float,
    y_lo: float,
    y_hi: float,
    *,
    exclude: AbstractSet[str | None] = frozenset(),
    margin: float = COORD_TOLERANCE,
) -> bool:
    """True if a channel at ``x`` over ``[y_lo, y_hi]`` lands inside a section bbox.

    Sections in ``exclude`` are skipped, for a channel that legitimately meets
    its own endpoints' sections.  The expanded boundary is open, so a channel
    seated exactly *margin* clear of a section does not intrude.
    """
    return _v_segment_crosses_other_section(
        graph,
        x,
        y_lo,
        y_hi,
        exclude,
        margin=margin,
        include_margin_boundary=False,
    )


def resolve_section(
    graph: MetroGraph,
    station: Station | None,
    prefer_upstream: bool = True,
) -> Section | None:
    """Resolve a station's section, tracing through junctions if needed.

    For stations with a ``section_id``, returns that section directly.
    For junctions (``section_id is None``), traces edges to find a
    connected port's section.

    When *prefer_upstream* is True (default), the junction is resolved
    through its incoming edges, yielding the upstream section.  When False,
    both directions are scanned in a single ``graph.edges`` pass with no
    preference.

    A ``None`` station (e.g. an unresolved lookup) yields ``None``.
    """
    if station is None:
        return None
    if station.section_id:
        return graph.sections.get(station.section_id)

    if prefer_upstream:
        for port in junction_source_ports(graph, station.id):
            sec = graph.sections.get(port.section_id)
            if sec:
                return sec
    else:
        # Preserve original graph.edges insertion order: callers depend on
        # the first incident edge winning when a junction has neighbours
        # in multiple sections.
        for e in graph.edges:
            if e.source == station.id:
                other = graph.station_for_edge_target(e)
            elif e.target == station.id:
                other = graph.station_for_edge_source(e)
            else:
                continue
            if other.section_id:
                sec = graph.sections.get(other.section_id)
                if sec:
                    return sec
    return None


def resolve_section_colrow(
    graph: MetroGraph, station: Station | None
) -> tuple[int | None, int | None]:
    """Resolve a port or junction to its effective grid column and row."""
    section = resolve_section(graph, station, prefer_upstream=False)
    if section is None:
        return None, None
    override = graph.grid_overrides.get(section.id)
    col = (
        section.grid_col
        if section.grid_col >= 0
        else override[0]
        if override is not None
        else None
    )
    row = (
        section.grid_row
        if section.grid_row >= 0
        else override[1]
        if override is not None
        else None
    )
    return col, row


def inter_row_wrap_band(n_lines: int, offset_step: float = OFFSET_STEP) -> float:
    """Bbox-to-bbox row gap a wrap bundle of *n_lines* needs.

    A horizontal inter-row run keeps :data:`INTER_ROW_EDGE_CLEARANCE` below
    the upper box edge and :data:`INTER_ROW_HEADER_CLEARANCE` above the next
    row's header badge, with the bundle's ``(n_lines - 1) * offset_step``
    stagger between.  Section placement reserves this band
    (:func:`~nf_metro.layout.section_placement._wrap_bundle_row_minimums`)
    and the corridor checks against it (``_corridor_is_viable``); the single
    definition keeps the two in lockstep.
    """
    span = max(n_lines - 1, 0) * offset_step
    return INTER_ROW_EDGE_CLEARANCE + span + INTER_ROW_HEADER_CLEARANCE


def reserved_row_band_between(
    reserved: ReservedBands | None, src_row: int, tgt_row: int
) -> ReservedBand | None:
    """The band reserved in the gap a run between these two rows travels.

    Only adjacent rows share one boundary; a run crossing further spans several
    gaps at once and so consumes no single reservation.
    """
    if reserved is None or abs(src_row - tgt_row) != 1:
        return None
    return reserved.at(max(src_row, tgt_row))


def inter_row_channel_y(
    graph: MetroGraph,
    src: Station,
    tgt: Station,
    sy: float,
    ty: float,
    dy: float,
    max_r: float,
    offset: float = 0.0,
    reserved: ReservedBands | None = None,
) -> float:
    """Compute Y for a horizontal channel in an inter-row gap.

    Vertical equivalent of ``inter_column_channel_x``: places the
    channel in the inter-row gap, clear of section headers (numbered
    circle + label rendered above/below bbox_y).

    ``offset`` shifts the run by a caller's per-line bundle stagger.  In
    the adjacent-row case it is clamped inside the clearance band (see
    :func:`_center_inter_row_channel`) so an over-sized stagger can't lift
    the run past the box edge.
    """
    src_sec = resolve_section(graph, src)
    tgt_sec = resolve_section(graph, tgt)

    if src_sec and tgt_sec and src_sec.grid_row != tgt_sec.grid_row:
        src_row = src_sec.grid_row
        tgt_row = tgt_sec.grid_row

        if abs(src_row - tgt_row) == 1:
            # Adjacent-row wrap: centre the run in a symmetric clearance
            # band so it clears the source bbox bottom and the next row's
            # header badge equally.  The bounding rows are the two this
            # gap separates.
            if dy > 0:
                upper_bottom = row_bottom_edge(graph, src_row, default=sy)
                lower_top = row_top_edge(graph, tgt_row, default=ty)
            else:
                upper_bottom = row_bottom_edge(graph, tgt_row, default=ty)
                lower_top = row_top_edge(graph, src_row, default=sy)
            return _center_inter_row_channel(
                upper_bottom,
                lower_top,
                offset,
                reserved=reserved_row_band_between(reserved, src_row, tgt_row),
            )

        # Multi-row crossing: an intervening row sits between source and
        # target. Keep the midpoint so ``_route_around_section_below``
        # still detects the section in the channel's path and routes around
        # it rather than the run being lifted into a gap it can't reach.
        if dy > 0:
            bottom = row_bottom_edge(graph, src_row, default=sy)
            top = row_top_edge(graph, tgt_row, default=ty)
            return (bottom + (top - HEADER_CLEARANCE)) / 2 + offset
        else:
            top = row_top_edge(graph, src_row, default=sy)
            bottom = row_bottom_edge(graph, tgt_row, default=ty)
            return (top + (bottom + HEADER_CLEARANCE)) / 2 + offset

    # Fallback: place near target, clearing the header zone
    if dy > 0:
        return ty - HEADER_CLEARANCE - max_r + offset
    else:
        return ty + HEADER_CLEARANCE + max_r + offset


def _inter_row_band_fits(upper_bottom: float, lower_top: float) -> bool:
    """Whether a horizontal run fits between two stacked rows with clearance.

    True when the band keeps :data:`INTER_ROW_EDGE_CLEARANCE` below the upper
    row's bottom edge and :data:`INTER_ROW_HEADER_CLEARANCE` above the lower
    row's header badge.  When it does not, a centred run grazes one edge, so a
    route prefers a different channel (the around-below loop / canvas-bottom
    dive) over this band.
    """
    return (
        upper_bottom + INTER_ROW_EDGE_CLEARANCE
        <= lower_top - INTER_ROW_HEADER_CLEARANCE
    )


def fan_corridor_band(
    upper_bottom: float, lower_top: float, span: float
) -> float | None:
    """Centre Y of the routing band between two stacked rows, or ``None``.

    The band keeps :data:`INTER_ROW_EDGE_CLEARANCE` below the upper row's bottom
    and :data:`INTER_ROW_HEADER_CLEARANCE` above the lower row's header badge.  A
    :class:`FanCorridor` shares one inter-row traverse band across a junction
    fan's branches, so the band must hold the whole nested bundle: returns the centre
    only when the clearance band is at least *span* wide (the bundle's full
    lateral extent), else ``None`` so those branches keep their own per-line
    clamped runs rather than nest past a box edge in a too-narrow gap.
    """
    lo = upper_bottom + INTER_ROW_EDGE_CLEARANCE
    hi = lower_top - INTER_ROW_HEADER_CLEARANCE
    if hi - lo < span:
        return None
    return (lo + hi) / 2


def merge_junction_ids(graph: MetroGraph) -> set[str]:
    """Resolved convergence junctions selected by semantic route identity."""
    return set(convergence_junction_ids(graph))


def merge_fanout_junctions(
    graph: MetroGraph, merges: set[str] | None = None
) -> set[str]:
    """Resolved divergences feeding two or more same-line convergences."""
    return set(merge_fanout_junction_ids(graph, convergence_ids=merges))


def merge_fanout_pivot_reference(
    junction_xs: list[float],
    source_x: float,
    tol: float,
) -> float | None:
    """Shared first-corner X for one merge fan-out junction's same-direction pivots.

    ``junction_xs`` are the first-corner Xs of one source's branches that turn
    the same way.  They leave the source together, so they fuse onto the corner
    nearest the source (the one hugging the fork) and the free branches snap onto
    it.  Returns ``None`` when there is nothing to share -- fewer than two
    branches, or they already coincide.
    """
    if len(junction_xs) < 2 or max(junction_xs) - min(junction_xs) <= tol:
        return None
    return min(junction_xs, key=lambda x: abs(x - source_x))


def _center_inter_row_channel(
    upper_bottom: float,
    lower_top: float,
    offset: float = 0.0,
    *,
    reserved: ReservedBand | None = None,
) -> float:
    """Y for a horizontal channel in the gap between two stacked rows.

    A *reserved* band is the allocation the corridor's own
    :class:`~nf_metro.layout.route_reservations.RouteReservation` realises,
    already carrying its side clearances.  It wins outright: the reservation
    measured the blockers that actually bound this corridor over its declared
    span, whereas ``upper_bottom`` / ``lower_top`` are whichever row edges the
    caller had to hand.  A *reserved* band here is
    :meth:`ReservedBands.at`'s boundary-wide intersection, which every claim
    crossing the boundary has to satisfy at once and so can be narrower than any
    one of them, down to a single coordinate.  :meth:`ReservedBand.place`
    therefore keeps the stagger rather
    than collapsing it, and a bundle wider than the intersection overruns it,
    which the closing guard reports.

    Without a reservation the channel is centred in the band that keeps
    :data:`INTER_ROW_EDGE_CLEARANCE` above the bbox bottom of the row
    above and :data:`INTER_ROW_HEADER_CLEARANCE` above the row below --
    the latter clears the *header badge* (numbered circle + label) rather
    than just the bbox edge, so the run doesn't graze the next-row label.
    When the gap is too narrow to satisfy both margins the channel biases
    to ``hi``, which keeps the badge clear at the source side's expense.

    A non-zero ``offset`` (a per-line bundle stagger) shifts the run off centre.
    Where both margins fit it is clamped inside them, so a stagger sized from a
    larger bundle than the gap allows cannot push the run past the box edge or
    header badge; in the degenerate too-narrow gap the stagger is applied
    unclamped so co-travelling lines stay distinct rather than collapsing onto
    one Y.
    """
    if reserved is not None:
        return reserved.place(offset)
    lo = upper_bottom + INTER_ROW_EDGE_CLEARANCE
    hi = lower_top - INTER_ROW_HEADER_CLEARANCE
    if _inter_row_band_fits(upper_bottom, lower_top):
        return min(max((lo + hi) / 2 + offset, lo), hi)
    # Gap too narrow for both margins (typically a heterogeneous-row case
    # where the global row edges over-state the obstruction at this x).
    # Bias to ``hi`` so the run still clears the next-row header badge --
    # the visually intrusive side -- and the source side keeps whatever
    # the gap allows, rather than the geometric midpoint that grazes both.
    return hi + offset


def centre_inter_column_channel(
    graph: MetroGraph,
    col_a: int,
    col_b: int,
    row: int | None = None,
    offset: float = 0.0,
    *,
    reserved: ReservedBands | None = None,
) -> float:
    """X for a vertical channel in the gap between two columns.

    The horizontal twin of :func:`_center_inter_row_channel`, and it reads its
    *reserved* band the same way: a band is the allocation the corridor's own
    :class:`~nf_metro.layout.route_reservations.RouteReservation` realises,
    already carrying its side clearances, and it wins outright because the
    reservation measured the blockers that actually bound this corridor over its
    declared span.  Only adjacent columns name one boundary, so a channel
    spanning further keeps the raw midpoint of :func:`column_gap_midpoint`,
    which is bounded by whichever sections happen to sit in the two columns.
    """
    band = (
        reserved.at(max(col_a, col_b))
        if reserved is not None and abs(col_a - col_b) == 1
        else None
    )
    if band is not None:
        return band.place(offset)
    return column_gap_midpoint(graph, col_a, col_b, row) + offset


def _segment_set_owns_boundary(owned_ranks: Sequence[int], rank: int) -> bool:
    return any(item in owned_ranks for item in (rank - 1, rank, rank + 1))


def convergence_owns_segment_boundary(route: RoutedPath, rank: int) -> bool:
    """Whether a convergence plan owns the boundary at or beside *rank*.

    A plan that owns a segment boundary owns the corner there, so a pass moving
    either of the two segments meeting at it would contradict the plan the
    closing validators check the geometry against.
    """
    return _segment_set_owns_boundary(route.convergence_owned_segment_ranks, rank)


def member_plan_owns_segment_boundary(route: RoutedPath, rank: int) -> bool:
    """Whether a member geometry plan owns the segment at or beside *rank*."""
    return _segment_set_owns_boundary(route.route_system_owned_segment_ranks, rank)


def route_system_owns_segment_boundary(route: RoutedPath, rank: int) -> bool:
    """Whether convergence or member geometry owns this segment boundary."""
    return convergence_owns_segment_boundary(
        route, rank
    ) or member_plan_owns_segment_boundary(route, rank)


def planner_owns_segment(route: RoutedPath, rank: int) -> bool:
    """Whether a pre-routing plan fixes the coordinate of one route segment.

    A convergence-owned segment boundary, a fan emission and a planned exit turn
    are all resolved against a plan the closing validators check the geometry
    against, so their coordinate is not a normalisation pass's to choose.

    Stated once because the passes that move a coordinate and the guards that
    refuse the result both have to agree on which coordinates are theirs: a pass
    reading a wider rule than its guard would move geometry the guard then
    refuses, and a narrower one would leave a defect neither reports.

    A pass that translates a whole segment wants
    :func:`planner_owns_segment_or_boundary` instead, which is the reading the
    normalisation passes and the closing guards share.  Only the convergence arm
    here reaches a boundary beside *rank*, because a trunk axis states a run
    whose two corners it fixes as well; a member plan and an exit turn each
    enumerate the segments they own, so those arms match *rank* exactly.  A
    caller wanting the widening on one side only says which side, as
    ``_corridor_run_band`` does for the leg feeding a planned turn.
    """
    return (
        convergence_owns_segment_boundary(route, rank)
        or route.fan_route_emitter is not None
        or rank in route.route_system_owned_segment_ranks
        or (
            route.exit_turn_axis_id is not None and route.exit_turn_segment_rank == rank
        )
    )


def planner_owns_segment_or_boundary(route: RoutedPath, rank: int) -> bool:
    """Whether a plan fixes this segment or a corner at either end of it.

    Translating a segment stretches its two flanking legs to meet it, so both
    of its corners re-form: a segment beside a route-system-owned boundary is
    as unavailable to a pass as an owned segment is.  The normalisation passes
    and closing guards that read this wider rule read it from here.
    """
    return planner_owns_segment(route, rank) or route_system_owns_segment_boundary(
        route, rank
    )


@dataclass(frozen=True, slots=True)
class CorridorRun:
    """One straight interior run of a route, read on whichever axis holds it.

    ``axis`` indexes the coordinate the run holds constant -- ``0`` for a
    vertical run, ``1`` for a horizontal one -- so ``coord`` and ``span`` read
    the same way on both and select the moved coordinate for a caller that
    translates the run.  ``sign`` is the direction of travel along the other
    axis.
    """

    route: RoutedPath
    idx: int
    axis: int
    coord: float
    span: tuple[float, float]
    sign: int

    @property
    def radii(self) -> tuple[float, float]:
        """The run's incoming and outgoing flanking corner radii."""
        stored = self.route.curve_radii or []
        return tuple(
            stored[i] if 0 <= i < len(stored) else CURVE_RADIUS
            for i in (self.idx - 1, self.idx)
        )  # type: ignore[return-value]


def corridor_runs(
    rp: RoutedPath, points: Sequence[tuple[float, float]] | None = None
) -> Iterator[CorridorRun]:
    """Every interior straight run of *rp* that turns at both ends.

    A run bounded by two perpendicular legs is one a pass may translate whole:
    both flanking legs stretch to meet it and its two corners re-form.  The
    route's opening and closing legs are excluded because they are pinned to an
    endpoint that moving the run would tear away from.

    *points* reads the run coordinates from a projection of the route rather
    than its waypoints, so a caller judging what is drawn passes the
    offset-applied polyline while a pass about to move a waypoint omits it.
    """
    pts = rp.points if points is None else points
    for k in range(1, len(pts) - 2):
        start, end = pts[k], pts[k + 1]
        axis = 0 if abs(end[0] - start[0]) <= abs(end[1] - start[1]) else 1
        along = 1 - axis
        if abs(end[axis] - start[axis]) > COORD_TOLERANCE:
            continue
        travel = end[along] - start[along]
        if abs(travel) <= COORD_TOLERANCE:
            continue
        if any(
            abs(pts[flank][along] - pts[corner][along]) > COORD_TOLERANCE
            or abs(pts[flank][axis] - pts[corner][axis]) <= COORD_TOLERANCE
            for flank, corner in ((k - 1, k), (k + 2, k + 1))
        ):
            continue
        yield CorridorRun(
            route=rp,
            idx=k,
            axis=axis,
            coord=start[axis],
            span=(min(start[along], end[along]), max(start[along], end[along])),
            sign=1 if travel > 0 else -1,
        )


@dataclass(frozen=True, slots=True)
class CorridorLane:
    """The runs of one line that share a track through one corridor.

    A line's fan-out legs are fused onto a single drawn track before this, so a
    lane -- not a run -- is the unit that may be re-seated: moving one leg of a
    fused track alone would split the line into two parallel same-colour runs.
    """

    line_id: str
    axis: int
    sign: int
    coord: float
    runs: tuple[CorridorRun, ...]

    @property
    def pinned(self) -> bool:
        """Whether a pre-routing plan fixes this track's coordinate."""
        return any(planner_owns_segment(run.route, run.idx) for run in self.runs)

    @property
    def handler_owned(self) -> bool:
        """Whether every run on this track carries the coordinate its handler set.

        A track that also carries a normalisation-owned run is a shared channel
        the normalisation stage already places, so it is not handler-owned --
        the rule trunk-slot materialisation applies when an exempt trunk shares
        a channel with a non-exempt one.
        """
        return all(run.route.normalize_exempt for run in self.runs)

    def _corridor_run_pairs(
        self, other: CorridorLane
    ) -> Iterator[tuple[CorridorRun, CorridorRun]]:
        """Run pairs to compare when checking whether this lane co-travels ``other``.

        Empty when the lanes cannot corridor-share at all: different axes or
        travel directions, or the same line.
        """
        if (
            self.axis != other.axis
            or self.sign != other.sign
            or self.line_id == other.line_id
        ):
            return
        for mine in self.runs:
            for theirs in other.runs:
                yield mine, theirs

    def fused_span(
        self, other: CorridorLane, step: float
    ) -> tuple[float, float] | None:
        """The widest stretch over which the two lanes draw as one stroke.

        ``None`` when they do not: different axes or travel directions, one
        line, or every shared stretch already carrying the full nesting step.
        """
        return max(
            (
                (max(mine.span[0], theirs.span[0]), min(mine.span[1], theirs.span[1]))
                for mine, theirs in self._corridor_run_pairs(other)
                if cotravelling_lanes_fuse(
                    self.coord, other.coord, mine.span, theirs.span, step
                )
            ),
            key=lambda span: span[1] - span[0],
            default=None,
        )

    def fuses_with(self, other: CorridorLane, step: float) -> bool:
        """Whether the two lanes' strokes close into one over a shared corridor."""
        return self.fused_span(other, step) is not None

    def shares_corridor_with(self, other: CorridorLane) -> bool:
        """Whether any run pair between the two lanes overlaps in the same corridor."""
        return any(
            spans_share_corridor(*mine.span, *theirs.span)
            for mine, theirs in self._corridor_run_pairs(other)
        )


def corridor_lanes(runs: Iterable[CorridorRun]) -> list[CorridorLane]:
    """Group *runs* into the tracks they are drawn on.

    Two runs share a track when they carry the same line in the same direction
    at the same coordinate and overlap along one corridor; runs of one line at
    the same coordinate in unrelated corridors stay separate lanes so a move
    here cannot drag geometry a corridor away.
    """
    tracks: list[list[CorridorRun]] = []
    for run in runs:
        member = next(
            (
                track
                for track in tracks
                if track[0].axis == run.axis
                and track[0].sign == run.sign
                and track[0].route.line_id == run.route.line_id
                and abs(track[0].coord - run.coord) <= COORD_TOLERANCE_FINE
                and any(spans_share_corridor(*run.span, *held.span) for held in track)
            ),
            None,
        )
        if member is None:
            tracks.append([run])
        else:
            member.append(run)
    return [
        CorridorLane(
            line_id=track[0].route.line_id,
            axis=track[0].axis,
            sign=track[0].sign,
            coord=track[0].coord,
            runs=tuple(track),
        )
        for track in tracks
    ]
