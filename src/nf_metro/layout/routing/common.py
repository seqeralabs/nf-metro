"""Shared types and helper functions for edge routing."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import NamedTuple

from nf_metro.layout.constants import (
    BUNDLE_TO_BUNDLE_CLEARANCE,
    BYPASS_CLEARANCE,
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    DEFAULT_LINE_PRIORITY,
    EDGE_TO_BUNDLE_CLEARANCE,
    HEADER_CLEARANCE,
    INTER_ROW_EDGE_CLEARANCE,
    INTER_ROW_HEADER_CLEARANCE,
    OFFSET_STEP,
    SECTION_HEADER_PROTRUSION,
    SECTION_ROUTE_CLEARANCE,
)
from nf_metro.layout.geometry import AxisFrame, lanes_run_along_x, lanes_run_along_y
from nf_metro.parser.model import Edge, MetroGraph, PortSide, Section, Station


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


def horizontal_direction(dx: float) -> Direction:
    """``Direction.R`` if ``dx > 0`` else ``Direction.L`` (ties resolve to L)."""
    return Direction.R if dx > 0 else Direction.L


def vertical_direction(dy: float) -> Direction:
    """``Direction.D`` if ``dy > 0`` else ``Direction.U`` (ties resolve to U)."""
    return Direction.D if dy > 0 else Direction.U


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
    section = graph.sections.get(port.section_id)
    if section is None or not lanes_run_along_y(section.direction):
        return False
    feeders_by_line: dict[str, set[str]] = {}
    for edge in graph.edges_to(port_id):
        src = graph.stations.get(edge.source)
        sp = graph.ports.get(edge.source)
        if src is None or not src.is_port or sp is None or sp.is_entry:
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
        and graph.sections.get(port.section_id) is not None
        and graph.sections[port.section_id].direction == "TB"
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
    if not secs:
        return default
    return max((s.bbox_x + s.bbox_w for s in secs), default=default)


def col_left_edge(
    graph: MetroGraph, col: int | None, default: float = 0.0, row: int | None = None
) -> float:
    """Leftmost X extent of sections in *col* (optionally a single *row*)."""
    secs = _sections_in_col(graph, col, row)
    return min((s.bbox_x for s in secs), default=default) if secs else default


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
    if not secs:
        return default
    return max((s.bbox_y + s.bbox_h for s in secs), default=default)


def row_top_edge(
    graph: MetroGraph, row: int, default: float = 0.0, col: int | None = None
) -> float:
    """Topmost Y extent of sections in *row* (optionally a single *col*)."""
    secs = _sections_in_row(graph, row)
    if col is not None:
        secs = [s for s in secs if s.grid_col == col]
    return min((s.bbox_y for s in secs), default=default) if secs else default


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
    :data:`SECTION_ROUTE_CLEARANCE` keeps the channel from overshooting it.

    When *col* is given the channel clears only that grid column's sections, so
    a corridor leg confined to one column isn't pushed past a tall section
    stacked in a different column of the same row.
    """
    if below:
        return (
            row_bottom_edge(graph, row, default=default, col=col)
            + SECTION_ROUTE_CLEARANCE
            + base_radius
        )
    clearance = (
        INTER_ROW_HEADER_CLEARANCE
        if section_exists_above_row(graph, row)
        else SECTION_ROUTE_CLEARANCE
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
    graph: MetroGraph, col_a: int, col_b: int, row: int | None = None
) -> tuple[float, float]:
    """Return ``(left_edge, right_edge)`` of the gap between two columns.

    *left_edge* is the right boundary of the lower-column sections;
    *right_edge* is the left boundary of the higher-column sections.

    When *row* is given, only sections occupying that grid row bound the
    gap, so a diversion travelling in one row isn't pushed off-centre by a
    section stacked in another row of the same column.
    """
    lo, hi = min(col_a, col_b), max(col_a, col_b)
    right = col_right_edge(graph, lo, row=row)
    left = col_left_edge(graph, hi, default=right, row=row)
    return right, left


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


def gap_lo_for_x(
    graph: MetroGraph,
    x: float,
    y_lo: float,
    y_hi: float,
    tol: float = COORD_TOLERANCE,
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
    cols = sorted({s.grid_col for s in graph.sections.values() if s.bbox_w > 0})
    rows = sorted({s.grid_row for s in graph.sections.values() if s.bbox_w > 0})
    bands = _grid_row_bands(graph)
    bracket: tuple[int, int | None] | None = None
    for r in rows:
        for lo, hi in zip(cols, cols[1:]):
            if hi != lo + 1:
                continue
            left, right = column_gap_edges(graph, lo, hi, row=r)
            if not (right > left and left - tol <= x <= right + tol):
                continue
            if bracket is None:
                bracket = (lo, r)
            band = bands.get(r)
            if band is not None and y_lo < band[1] and band[0] < y_hi:
                return lo, r
    if bracket is not None:
        return bracket
    for lo, hi in zip(cols, cols[1:]):
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


@dataclass(frozen=True)
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


@dataclass(frozen=True)
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
    station_offsets: dict[tuple[str, str], float],
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


def initial_fanout_descent_span(
    rp: RoutedPath,
) -> tuple[float, float, float, bool] | None:
    """``(x, y_lo, y_hi, down)`` of the descent leaving a route's source.

    A fan-out branch opens ``(sx, sy) -> (vx, sy) -> (vx, dy) -> ...``: a
    short horizontal lead off the shared source, then a vertical descent in
    its own channel.  Returns ``None`` when the route does not open
    horizontal-then-vertical.
    """
    pts = rp.points
    if len(pts) < 3:
        return None
    (x0, y0), (x1, y1), (x2, y2) = pts[0], pts[1], pts[2]
    if abs(y1 - y0) > COORD_TOLERANCE or abs(x1 - x0) <= COORD_TOLERANCE:
        return None
    if abs(x2 - x1) > COORD_TOLERANCE or abs(y2 - y1) <= COORD_TOLERANCE:
        return None
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
    """A riser peeling off a horizontal trunk into an entry port."""

    trunk_y: float
    peel_x: float
    port_y: float
    trunk_sign: int  # +1 trunk runs left->right toward the peel, -1 right->left


def port_peeloff_tail(rp: RoutedPath) -> PeeloffTail | None:
    """The peel-off tail of a riser ending at an entry port, or ``None``.

    A peel-off-into-port tail ends ``... (tx, trunk_y) -> (peel_x, trunk_y)
    -> (peel_x, port_y) -> (ex, port_y)``: a horizontal trunk, an upward
    vertical riser, then a short horizontal lead into the port (the port sits
    above the trunk).  ``trunk_sign`` records the trunk's traversal direction
    toward the peel corner.  Returns ``None`` for any other tail.
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
    if y2 >= y3 - COORD_TOLERANCE:
        return None  # not an upward riser (port not above the trunk)
    return PeeloffTail(y3, x3, y2, 1 if x3 > x4 else -1)


class PortPeeloffBundle(NamedTuple):
    """One concentric bundle of lines peeling off a shared trunk into a port.

    ``entries`` holds every ``(route, tail)`` rising into the port (a line can
    feed several risers); ``per_line`` is one representative tail per distinct
    line, the unit the slot order is assigned over.  ``reverse`` is the trunk's
    traversal sense toward the peel corner (a right-to-left trunk peels at the
    far end, so its slot order is reversed).
    """

    port_id: str
    entries: list[tuple[RoutedPath, PeeloffTail]]
    per_line: dict[str, PeeloffTail]
    reverse: bool


class PeeloffSlot(NamedTuple):
    """A peel-off line's target peel-x, port-slot Y, and concentric rank."""

    peel_x: float
    port_y: float
    rank: int


def trunk_depths_contiguous(trunk_ys: list[float], n: int, step: float) -> bool:
    """Whether ``n`` trunk depths span at most one concentric bundle width.

    The members of a single concentric bundle sit within ``(n-1)*step`` of one
    another; a wider span is two channels rows apart, not one bundle.
    """
    return max(trunk_ys) - min(trunk_ys) <= (n - 1) * step + COORD_TOLERANCE


def iter_port_peeloff_bundles(
    routes: list[RoutedPath], graph: MetroGraph, step: float
) -> Iterator[PortPeeloffBundle]:
    """Yield each contiguous concentric peel-off bundle into a LEFT entry port.

    A bundle is several inter-section lines riding one shared bypass trunk that
    rise into a common LEFT entry port.  Only the bundles whose ordering is a
    single concentric turn are yielded: at least two distinct lines, distinct
    trunk depths to order by, every member within the bundle's own
    ``OFFSET_STEP`` width of a neighbour (one contiguous bundle, not lines that
    reach the port on trunks rows apart), and all peeling at the same trunk end
    (one traversal sense).  The reordering pass and its runtime guard share this
    membership test.
    """
    by_port: dict[str, list[tuple[RoutedPath, PeeloffTail]]] = defaultdict(list)
    for rp in routes:
        tail = port_peeloff_tail(rp)
        if tail is None:
            continue
        port = graph.ports.get(rp.edge.target)
        if port is None or not port.is_entry or port.side is not PortSide.LEFT:
            continue
        by_port[rp.edge.target].append((rp, tail))

    for port_id, entries in by_port.items():
        # One representative tail per distinct line (a line feeding several
        # risers shares a single slot, so its risers move together).
        per_line: dict[str, PeeloffTail] = {}
        for rp, t in entries:
            per_line.setdefault(rp.edge.line_id, t)
        n = len(per_line)
        if n < 2:
            continue
        trunk_ys = sorted(t.trunk_y for t in per_line.values())
        if trunk_ys[-1] - trunk_ys[0] <= COORD_TOLERANCE:
            continue  # no distinct trunk depths to order by
        if not trunk_depths_contiguous(trunk_ys, n, step):
            continue  # not one contiguous concentric bundle
        signs = {t.trunk_sign for t in per_line.values()}
        if len(signs) != 1:
            continue  # lines peel at different trunk ends; ambiguous
        yield PortPeeloffBundle(port_id, entries, per_line, signs.pop() < 0)


def peeloff_target_slots(bundle: PortPeeloffBundle) -> dict[str, PeeloffSlot]:
    """Map each line of *bundle* to the slot its trunk depth earns.

    The peel-x and port-slot Ys the bundle already occupies are reassigned by
    trunk depth so the bundle turns into the port concentrically: the shallowest
    trunk line takes the slot nearest the trunk's near end (the inner slot for a
    left-to-right trunk, the outer for a right-to-left one).  Spacing is
    preserved -- the slots are permuted among the bundle's existing ones.
    """
    per_line = bundle.per_line
    n = len(per_line)
    x_slots = sorted(t.peel_x for t in per_line.values())
    y_slots = sorted(t.port_y for t in per_line.values())
    ranked = sorted(per_line, key=lambda lid: per_line[lid].trunk_y)
    slot = list(range(n - 1, -1, -1)) if bundle.reverse else list(range(n))
    return {
        lid: PeeloffSlot(x_slots[slot[i]], y_slots[slot[i]], slot[i])
        for i, lid in enumerate(ranked)
    }


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


def trunk_segments_cross(a: HTrunkSeg, b: HTrunkSeg) -> tuple[float, float] | None:
    """Return where trunks *a* and *b* cross, or ``None`` if they don't.

    A crossing is a riser of one trunk piercing the horizontal run of the
    other (the two parallel runs themselves never cross).  Returns the first
    crossing point found.
    """
    for seg, other in ((a, b), (b, a)):
        for vx, vy in ((seg.xa, seg.before_y), (seg.xb, seg.after_y)):
            if _vert_horiz_cross(vx, seg.y, vy, other.y, other.x_lo, other.x_hi):
                return vx, other.y
    return None


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
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if not src or not tgt:
            continue

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
            src_st = graph.stations.get(edge.source)
            tgt_st = graph.stations.get(edge.target)
            src_sec = (
                graph.sections.get(src_st.section_id)
                if src_st and src_st.section_id
                else None
            )
            tgt_sec = (
                graph.sections.get(tgt_st.section_id)
                if tgt_st and tgt_st.section_id
                else None
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
    tx: float,
    dx: float,
    max_r: float,
    offset_step: float,
) -> float:
    """Compute the X position for a vertical channel in an L-shaped route.

    Places the channel in the gap between columns so it doesn't pass
    through sibling sections stacked in the source's column. Falls
    back to near-source placement when section info is unavailable.
    """
    src_sec = graph.sections.get(src.section_id) if src.section_id else None
    tgt_sec = graph.sections.get(tgt.section_id) if tgt.section_id else None

    if src_sec and tgt_sec and src_sec.grid_col != tgt_sec.grid_col:
        return column_gap_midpoint(graph, src_sec.grid_col, tgt_sec.grid_col)

    # Extend the same gap-centred placement to junction endpoints (whose
    # section is found by tracing the junction graph) so a junction-sourced
    # L-shape centres in the inter-column gap instead of hugging one edge.
    # Restrict to ADJACENT resolved columns: in staggered layouts a
    # junction can resolve several columns from its target, and centring in
    # that far span would drag the channel through empty canvas.
    res_src = src_sec or resolve_section(graph, src)
    res_tgt = tgt_sec or resolve_section(graph, tgt)
    if res_src and res_tgt and abs(res_src.grid_col - res_tgt.grid_col) == 1:
        return column_gap_midpoint(graph, res_src.grid_col, res_tgt.grid_col)

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
        src = graph.stations.get(edge.source)
        if src and not src.is_port:
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


def bypass_bottom_y(
    graph: MetroGraph,
    src_col: int,
    tgt_col: int,
    clearance: float = BYPASS_CLEARANCE,
    src_row: int | None = None,
    cross_row: bool = False,
    tgt_row: int | None = None,
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
            return _center_inter_row_channel(upper_bottom, lower_top)
        # Route below ALL sections in the column range.
        all_in_range = [
            s
            for s in graph.sections.values()
            if s.bbox_w > 0 and lo <= s.grid_col <= hi
        ]
        if all_in_range:
            return max(s.bbox_y + s.bbox_h for s in all_in_range) + clearance
        return clearance

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
            return clearance

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

    return candidate


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
        for e in graph.edges_to(station.id):
            other = graph.stations.get(e.source)
            if other and other.section_id:
                sec = graph.sections.get(other.section_id)
                if sec:
                    return sec
    else:
        # Preserve original graph.edges insertion order: callers depend on
        # the first incident edge winning when a junction has neighbours
        # in multiple sections.
        for e in graph.edges:
            other_id = None
            if e.source == station.id:
                other_id = e.target
            elif e.target == station.id:
                other_id = e.source
            if other_id:
                other = graph.stations.get(other_id)
                if other and other.section_id:
                    sec = graph.sections.get(other.section_id)
                    if sec:
                        return sec
    return None


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


def inter_row_channel_y(
    graph: MetroGraph,
    src: Station,
    tgt: Station,
    sy: float,
    ty: float,
    dy: float,
    max_r: float,
    offset: float = 0.0,
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
            return _center_inter_row_channel(upper_bottom, lower_top, offset)

        # Multi-row crossing: an intervening row sits between source and
        # target.  Keep the legacy midpoint so ``_route_around_section_below``
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


def _center_inter_row_channel(
    upper_bottom: float, lower_top: float, offset: float = 0.0
) -> float:
    """Y for a horizontal channel in the gap between two stacked rows.

    The channel is centred in the band that keeps
    :data:`INTER_ROW_EDGE_CLEARANCE` above the bbox bottom of the row
    above and :data:`INTER_ROW_HEADER_CLEARANCE` above the row below --
    the latter clears the *header badge* (numbered circle + label) rather
    than just the bbox edge, so the run doesn't graze the next-row label.
    When the gap is too narrow to satisfy both margins the channel biases
    to ``hi`` so it still clears the badge.

    A non-zero ``offset`` (a per-line bundle stagger) shifts the run off
    centre.  When the band has room it is clamped to stay inside, so a
    stagger sized from a larger bundle than the gap was reserved for can't
    push the run past the box edge or header badge; in the degenerate
    too-narrow band the stagger is applied unclamped so co-travelling lines
    stay distinct rather than collapsing onto one Y.
    """
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
