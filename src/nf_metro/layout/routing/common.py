"""Shared types and helper functions for edge routing."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from nf_metro.layout.constants import (
    BYPASS_CLEARANCE,
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    DEFAULT_LINE_PRIORITY,
    SECTION_HEADER_PROTRUSION,
)
from nf_metro.parser.model import Edge, MetroGraph, Section

# ---------------------------------------------------------------------------
# Grid-position helpers
# ---------------------------------------------------------------------------
# These replace repeated ``for s in graph.sections.values() if s.grid_col == X``
# patterns scattered across routing and layout modules.


def _sections_in_col(
    graph: MetroGraph, col: int, row: int | None = None
) -> list[Section]:
    """Sections in a specific grid column with non-zero width.

    When *row* is provided, the result is further narrowed to that row.
    """
    return [
        s
        for s in graph.sections.values()
        if s.grid_col == col and s.bbox_w > 0 and (row is None or s.grid_row == row)
    ]


def _sections_in_row(graph: MetroGraph, row: int) -> list[Section]:
    """Sections in a specific grid row with non-zero height."""
    return [s for s in graph.sections.values() if s.grid_row == row and s.bbox_h > 0]


def col_right_edge(
    graph: MetroGraph, col: int, default: float = 0.0, row: int | None = None
) -> float:
    """Rightmost X extent of sections in *col*.

    When *row* is provided, the lookup is narrowed to that row, falling
    back to the full column when no section sits in (col, row).
    """
    secs = _sections_in_col(graph, col, row=row)
    if not secs and row is not None:
        secs = _sections_in_col(graph, col)
    if not secs:
        return default
    return max((s.bbox_x + s.bbox_w for s in secs), default=default)


def col_left_edge(
    graph: MetroGraph, col: int, default: float = 0.0, row: int | None = None
) -> float:
    """Leftmost X extent of sections in *col*.

    When *row* is provided, the lookup is narrowed to that row, falling
    back to the full column when no section sits in (col, row).
    """
    secs = _sections_in_col(graph, col, row=row)
    if not secs and row is not None:
        secs = _sections_in_col(graph, col)
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
    """X midpoint of the gap between two columns.

    When *row* is provided, the column edges are computed from sections
    in that row only so a wider section in another row can't pull the
    midpoint off the row's natural inter-section gap.
    """
    lo, hi = min(col_a, col_b), max(col_a, col_b)
    right = col_right_edge(graph, lo, row=row)
    left = col_left_edge(graph, hi, default=right, row=row)
    return (right + left) / 2


@dataclass
class RoutedPath:
    """A routed path for an edge, consisting of (x, y) waypoints."""

    edge: Edge
    line_id: str
    points: list[tuple[float, float]]
    is_inter_section: bool = False
    curve_radii: list[float] | None = None
    offsets_applied: bool = False


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

    When src and tgt sit in the same grid row, the gap is computed
    from same-row column edges so a wider section in another row
    can't pull the channel off the row's natural inter-section gap.
    """
    src_sec = graph.sections.get(src.section_id) if src.section_id else None
    tgt_sec = graph.sections.get(tgt.section_id) if tgt.section_id else None

    if src_sec and tgt_sec and src_sec.grid_col != tgt_sec.grid_col:
        # Find the rightmost/leftmost edges of the source and target
        # columns (accounting for sibling sections that may be wider).
        # For same-row bundles, narrow to that row so a taller off-row
        # neighbour doesn't drag the channel across the gap.
        src_col = src_sec.grid_col
        tgt_col = tgt_sec.grid_col
        row = src_sec.grid_row if src_sec.grid_row == tgt_sec.grid_row else None

        if dx > 0:
            right = col_right_edge(graph, src_col, default=sx, row=row)
            left = col_left_edge(graph, tgt_col, default=tx, row=row)
            return (right + left) / 2
        else:
            left = col_left_edge(graph, src_col, default=sx, row=row)
            right = col_right_edge(graph, tgt_col, default=tx, row=row)
            return (left + right) / 2

    # Fallback: place near source
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
    for edge in graph.edges:
        if edge.target == port_id:
            src = graph.stations.get(edge.source)
            if src and not src.is_port:
                line_y[edge.line_id] = src.y
    return line_y


def adjacent_column_gap_x(
    graph: MetroGraph,
    col_a: int,
    col_b: int,
    row: int | None = None,
) -> float:
    """X midpoint between two adjacent columns.

    Finds the right edge of col_a and left edge of col_b (assuming
    col_a < col_b) and returns the midpoint.  When *row* is provided,
    the lookup is restricted to that row so off-row neighbours can't
    drag the midpoint off the row's natural inter-section gap.
    """
    return column_gap_midpoint(graph, col_a, col_b, row=row)


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
    max_intervening = (
        max((s.bbox_y + s.bbox_h for s in intervening), default=0.0)
        if intervening
        else 0.0
    )

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

    # When row-filtering is active, the candidate may land in the
    # inter-row gap too close to a section header in another row.
    # The header (number badge + label) extends ~26px above bbox_y.
    # Cap the bypass at the midpoint of the gap to keep equal spacing.
    if src_row is not None:
        for s in graph.sections.values():
            if s.bbox_w > 0 and lo <= s.grid_col <= hi and s.grid_row != src_row:
                header_top = s.bbox_y - SECTION_HEADER_PROTRUSION
                if candidate > header_top:
                    # Place bypass at midpoint between row bottom and header top
                    row_bottom = candidate - clearance
                    candidate = (row_bottom + header_top) / 2

    return candidate


def line_incoming_y_at_entry_port(
    port_id: str,
    graph: MetroGraph,
    exit_offsets: dict[tuple[str, str], float],
) -> dict[str, float]:
    """Map line_id -> effective Y of incoming connection at an entry port.

    Uses the source station's Y + its already-computed station offset
    for the line, so the entry port ordering matches the bundle ordering
    from the source section.
    """
    line_y: dict[str, float] = {}
    for edge in graph.edges:
        if edge.target == port_id:
            src = graph.stations.get(edge.source)
            if src and src.is_port:
                src_off = exit_offsets.get((edge.source, edge.line_id), 0)
                line_y[edge.line_id] = src.y + src_off
    return line_y
