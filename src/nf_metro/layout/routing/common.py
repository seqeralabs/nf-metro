"""Shared types and helper functions for edge routing."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum

from nf_metro.layout.constants import (
    BUNDLE_TO_BUNDLE_CLEARANCE,
    BYPASS_CLEARANCE,
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    DEFAULT_LINE_PRIORITY,
    EDGE_TO_BUNDLE_CLEARANCE,
    HEADER_CLEARANCE,
    INTER_ROW_HEADER_CLEARANCE,
    OFFSET_STEP,
    SECTION_HEADER_PROTRUSION,
)
from nf_metro.parser.model import Edge, MetroGraph, Section, Station


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


# ---------------------------------------------------------------------------
# Grid-position helpers
# ---------------------------------------------------------------------------
# These replace repeated ``for s in graph.sections.values() if s.grid_col == X``
# patterns scattered across routing and layout modules.


def _sections_in_col(
    graph: MetroGraph, col: int, row: int | None = None
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


def _sections_in_row(graph: MetroGraph, row: int) -> list[Section]:
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
    graph: MetroGraph, col: int, default: float = 0.0, row: int | None = None
) -> float:
    """Leftmost X extent of sections in *col* (optionally a single *row*)."""
    secs = _sections_in_col(graph, col, row)
    return min((s.bbox_x for s in secs), default=default) if secs else default


def row_bottom_edge(graph: MetroGraph, row: int, default: float = 0.0) -> float:
    """Bottommost Y extent of sections in *row*."""
    secs = _sections_in_row(graph, row)
    if not secs:
        return default
    return max((s.bbox_y + s.bbox_h for s in secs), default=default)


def row_top_edge(graph: MetroGraph, row: int, default: float = 0.0) -> float:
    """Topmost Y extent of sections in *row*."""
    secs = _sections_in_row(graph, row)
    return min((s.bbox_y for s in secs), default=default) if secs else default


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


@dataclass
class RoutedPath:
    """A routed path for an edge, consisting of (x, y) waypoints."""

    edge: Edge
    line_id: str
    points: list[tuple[float, float]]
    is_inter_section: bool = False
    curve_radii: list[float] | None = None
    offsets_applied: bool = False
    normalize_exempt: bool = False
    """Skip this route in the gap-channel normalization post-pass.

    Set by wrap / around-section / TOP-entry handlers whose vertical
    channels follow a special concentric loop (all corners share one
    radius) that the standard L-shape re-stacking would break."""


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
    corridor_groups: dict[tuple, list[tuple[Edge, float, float, float, float]]] = (
        defaultdict(list)
    )

    for item in inter_edges:
        edge, sx, sy, tx, ty = item
        dx = tx - sx
        dy = ty - sy

        if abs(dy) < COORD_TOLERANCE_FINE:
            continue  # Horizontal edges don't need bundling

        v_dir = 1 if dy > 0 else -1

        if abs(dx) < COORD_TOLERANCE:
            # Vertical: group by shared X position
            key = ("V", round(sx), v_dir)
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
            if src_sec and tgt_sec and src_sec.grid_col != tgt_sec.grid_col:
                col_key = (src_sec.grid_col, tgt_sec.grid_col)
            elif tgt_sec:
                # Source is a junction: include target column so edges
                # to different columns get separate bundles.
                col_key = (round(sx), tgt_sec.grid_col)
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
    src,
    tgt,
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


def bypass_bottom_y(
    graph: MetroGraph,
    src_col: int,
    tgt_col: int,
    clearance: float = BYPASS_CLEARANCE,
    src_row: int | None = None,
    cross_row: bool = False,
) -> float:
    """Bottom Y for a bypass route around intervening sections.

    When *cross_row* is True, the route must clear ALL sections in
    the column range (regardless of grid row) so it goes cleanly
    below everything.  Otherwise, when *src_row* is provided, only
    sections in the same row are considered so that bypass routes
    stay within their row.

    When there are no intervening sections (adjacent-column bypass),
    falls back to the shorter of the source/target endpoint sections
    so the route hugs the smaller box rather than being pushed down
    by a tall neighbour.
    """
    lo, hi = min(src_col, tgt_col), max(src_col, tgt_col)

    if cross_row:
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

    # Keep the bypass at least HEADER_CLEARANCE above any different-row
    # section header_top; the stacked-line bundle otherwise crowds the
    # badge.  Midpoint fallback for inter-row gaps too tight to satisfy
    # both clearances (layout placement should normally prevent this).
    if src_row is not None:
        for s in graph.sections.values():
            if s.bbox_w > 0 and lo <= s.grid_col <= hi and s.grid_row != src_row:
                header_top = s.bbox_y - SECTION_HEADER_PROTRUSION
                row_bottom = candidate - clearance
                safe_cap = header_top - HEADER_CLEARANCE
                if candidate > safe_cap:
                    if safe_cap >= row_bottom:
                        candidate = safe_cap
                    else:
                        candidate = (row_bottom + header_top) / 2

    return candidate


# ---------------------------------------------------------------------------
# Section resolution + inter-row channel placement
# ---------------------------------------------------------------------------


def resolve_section(
    graph: MetroGraph,
    station: Station,
    prefer_upstream: bool = True,
) -> Section | None:
    """Resolve a station's section, tracing through junctions if needed.

    For stations with a ``section_id``, returns that section directly.
    For junctions (``section_id is None``), traces edges to find a
    connected port's section.

    When *prefer_upstream* is True (default), incoming edges are checked
    first so the junction resolves to the upstream section.  When False,
    both directions are scanned in a single pass with no preference.
    """
    if station.section_id:
        return graph.sections.get(station.section_id)

    if prefer_upstream:
        for e in graph.edges_to(station.id):
            other = graph.stations.get(e.source)
            if other and other.section_id:
                sec = graph.sections.get(other.section_id)
                if sec:
                    return sec
        for e in graph.edges_from(station.id):
            other = graph.stations.get(e.target)
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


def inter_row_channel_y(
    graph: MetroGraph,
    src: Station,
    tgt: Station,
    sy: float,
    ty: float,
    dy: float,
    max_r: float,
) -> float:
    """Compute Y for a horizontal channel in an inter-row gap.

    Vertical equivalent of ``inter_column_channel_x``: places the
    channel in the inter-row gap, clear of section headers (numbered
    circle + label rendered above/below bbox_y).
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
            return _center_inter_row_channel(upper_bottom, lower_top)

        # Multi-row crossing: an intervening row sits between source and
        # target.  Keep the legacy midpoint so ``_route_around_section_below``
        # still detects the section in the channel's path and routes around
        # it rather than the run being lifted into a gap it can't reach.
        if dy > 0:
            bottom = row_bottom_edge(graph, src_row, default=sy)
            top = row_top_edge(graph, tgt_row, default=ty)
            return (bottom + (top - HEADER_CLEARANCE)) / 2
        else:
            top = row_top_edge(graph, src_row, default=sy)
            bottom = row_bottom_edge(graph, tgt_row, default=ty)
            return (top + (bottom + HEADER_CLEARANCE)) / 2

    # Fallback: place near target, clearing the header zone
    if dy > 0:
        return ty - HEADER_CLEARANCE - max_r
    else:
        return ty + HEADER_CLEARANCE + max_r


def _center_inter_row_channel(upper_bottom: float, lower_top: float) -> float:
    """Y for a horizontal channel in the gap between two stacked rows.

    The channel is centred in the band that keeps
    :data:`EDGE_TO_BUNDLE_CLEARANCE` ("constant A") above the bbox bottom
    of the row above and :data:`INTER_ROW_HEADER_CLEARANCE` above the row
    below -- the latter clears the *header badge* (numbered circle +
    label) rather than just the bbox edge, so the run doesn't graze the
    next-row label.  When the gap is too narrow to satisfy both margins
    the channel biases to ``hi`` so it still clears the badge.
    """
    lo = upper_bottom + EDGE_TO_BUNDLE_CLEARANCE
    hi = lower_top - INTER_ROW_HEADER_CLEARANCE
    if lo <= hi:
        return (lo + hi) / 2
    # Gap too narrow for both margins (typically a heterogeneous-row case
    # where the global row edges over-state the obstruction at this x).
    # Bias to ``hi`` so the run still clears the next-row header badge --
    # the visually intrusive side -- and the source side keeps whatever
    # the gap allows, rather than the geometric midpoint that grazes both.
    return hi
