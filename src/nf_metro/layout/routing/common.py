"""Shared types and helper functions for edge routing."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum

from nf_metro.layout.constants import (
    BYPASS_CLEARANCE,
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    DEFAULT_LINE_PRIORITY,
    HEADER_CLEARANCE,
    SECTION_HEADER_PROTRUSION,
)
from nf_metro.parser.model import Edge, MetroGraph, Section, Station


class Direction(Enum):
    """Cardinal travel direction for a horizontal or vertical run.

    Used by the inter-section descriptor scaffolding (see
    ``inter_section.py``) to characterise corner in/out tangents in a
    direction-agnostic way.  Not yet wired into runtime routing; the
    routing code still operates on raw signed deltas.
    """

    R = "R"  # east, +x
    L = "L"  # west, -x
    U = "U"  # north, -y
    D = "D"  # south, +y

# ---------------------------------------------------------------------------
# Grid-position helpers
# ---------------------------------------------------------------------------
# These replace repeated ``for s in graph.sections.values() if s.grid_col == X``
# patterns scattered across routing and layout modules.


def _sections_in_col(graph: MetroGraph, col: int) -> list[Section]:
    """Sections in a specific grid column with non-zero width."""
    return [s for s in graph.sections.values() if s.grid_col == col and s.bbox_w > 0]


def _sections_in_row(graph: MetroGraph, row: int) -> list[Section]:
    """Sections in a specific grid row with non-zero height."""
    return [s for s in graph.sections.values() if s.grid_row == row and s.bbox_h > 0]


def col_right_edge(graph: MetroGraph, col: int, default: float = 0.0) -> float:
    """Rightmost X extent of sections in *col*."""
    secs = _sections_in_col(graph, col)
    if not secs:
        return default
    return max((s.bbox_x + s.bbox_w for s in secs), default=default)


def col_left_edge(graph: MetroGraph, col: int, default: float = 0.0) -> float:
    """Leftmost X extent of sections in *col*."""
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


def column_gap_midpoint(graph: MetroGraph, col_a: int, col_b: int) -> float:
    """X midpoint of the gap between two columns."""
    lo, hi = min(col_a, col_b), max(col_a, col_b)
    right = col_right_edge(graph, lo)
    left = col_left_edge(graph, hi, default=right)
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
    """
    src_sec = graph.sections.get(src.section_id) if src.section_id else None
    tgt_sec = graph.sections.get(tgt.section_id) if tgt.section_id else None

    if src_sec and tgt_sec and src_sec.grid_col != tgt_sec.grid_col:
        # Find the rightmost/leftmost edges of the source and target
        # columns (accounting for sibling sections that may be wider).
        src_col = src_sec.grid_col
        tgt_col = tgt_sec.grid_col

        if dx > 0:
            right = col_right_edge(graph, src_col, default=sx)
            left = col_left_edge(graph, tgt_col, default=tx)
            return (right + left) / 2
        else:
            left = col_left_edge(graph, src_col, default=sx)
            right = col_right_edge(graph, tgt_col, default=tx)
            return (left + right) / 2


    # Junction at L-shape elbow (src is a junction with no section_id):
    # When the junction is at the corner of a clockwise/counter-clockwise
    # L, the channel should sit at the junction's x so the L pivots
    # cleanly through the junction.  Apply only for genuine L-shapes
    # (significant dy AND dx) to avoid disturbing degenerate near-vertical
    # or near-horizontal routes that the old fallback handled correctly.
    # See docs/dev/authoring_misfires.md #2 / #10.
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

    # Final safety: the iterative inter-row clamping above can land the
    # candidate INSIDE an intervening section when no real gap exists
    # (e.g. a wide colspan section blocks every column in the bypass's
    # span).  Detect that and fall back to routing BELOW every section
    # in the column range - the only universally-safe alternative when
    # there is no inter-row gap to slot into.
    #
    # Restrict the check to STRICTLY-INTERVENING columns (lo < col < hi):
    # the source and target sections at the endpoints aren't crossed by
    # the bypass channel - the route exits/enters them through their
    # ports.  Including endpoint columns would push every same-row
    # bypass below the taller endpoint (e.g. wide fan-out section
    # bypassing a short adjacent neighbour to reach the next column).
    blocking = [
        s
        for s in graph.sections.values()
        if s.bbox_w > 0
        and lo < s.grid_col < hi
        and s.bbox_y - SECTION_HEADER_PROTRUSION <= candidate <= s.bbox_y + s.bbox_h
    ]
    if blocking:
        return max(s.bbox_y + s.bbox_h for s in blocking) + clearance

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
    channel in the inter-row gap, above the target section's header
    (number badge + label rendered above bbox_y).
    """
    # Keep the channel clear of section headers (numbered circle + label)
    # that protrude above/below bbox_y.

    # Resolve sections for junction stations (section_id is None for
    # junctions; trace through edges to find a connected port's section).
    src_sec = resolve_section(graph, src)
    tgt_sec = resolve_section(graph, tgt)

    if src_sec and tgt_sec and src_sec.grid_row != tgt_sec.grid_row:
        src_row = src_sec.grid_row
        tgt_row = tgt_sec.grid_row

        if dy > 0:
            # Going down: gap between bottom of source row and top of target row
            bottom = row_bottom_edge(graph, src_row, default=sy)
            top = row_top_edge(graph, tgt_row, default=ty)
            # Place above the header zone
            header_top = top - HEADER_CLEARANCE
            return (bottom + header_top) / 2
        else:
            # Going up: gap between top of source row and bottom of target row
            top = row_top_edge(graph, src_row, default=sy)
            bottom = row_bottom_edge(graph, tgt_row, default=ty)
            header_bottom = bottom + HEADER_CLEARANCE
            return (top + header_bottom) / 2

    # Fallback: place near target, clearing the header zone
    if dy > 0:
        return ty - HEADER_CLEARANCE - max_r
    else:
        return ty + HEADER_CLEARANCE + max_r
