"""Section meta-graph layout: place sections on the canvas.

Uses the section DAG (built once by auto_layout and stored on
MetroGraph) for topological layering of column assignment and
row stacking within columns.  Grid overrides can pin sections
to specific positions.
"""

from __future__ import annotations

__all__ = ["place_sections", "position_ports"]

import warnings
from collections import defaultdict, deque

from nf_metro.layout.constants import (
    CURVE_RADIUS,
    MERGE_GAP_MIN,
    MIN_INTER_SECTION_GAP,
    MIN_INTER_SECTION_ROW_GAP,
    MIN_PORT_STATION_GAP,
    OFFSET_STEP,
    PLACEMENT_X_GAP,
    PLACEMENT_Y_GAP,
    PORT_MIN_GAP,
    SECTION_HEADER_PROTRUSION,
    SECTION_X_PADDING,
)
from nf_metro.parser.model import MetroGraph, PortSide, Section


def _assign_grid_layout(
    graph: MetroGraph,
    section_edges: set[tuple[str, str]],
) -> tuple[dict[str, int], dict[str, int]]:
    """Assign grid columns and rows to each section.

    Returns (col_assign, row_assign) dicts mapping section IDs to positions.
    """
    section_ids = list(graph.sections.keys())

    # Topological layering (columns)
    in_degree: dict[str, int] = {sid: 0 for sid in section_ids}
    adj: dict[str, list[str]] = {sid: [] for sid in section_ids}
    for src, tgt in section_edges:
        adj[src].append(tgt)
        in_degree[tgt] += 1

    # BFS topological sort for column assignment
    col_assign: dict[str, int] = {}
    queue: deque[str] = deque()
    for sid in section_ids:
        if in_degree[sid] == 0:
            queue.append(sid)
            col_assign[sid] = 0

    while queue:
        sid = queue.popleft()
        for tgt in adj[sid]:
            new_col = col_assign[sid] + 1
            if tgt not in col_assign or new_col > col_assign[tgt]:
                col_assign[tgt] = new_col
            in_degree[tgt] -= 1
            if in_degree[tgt] == 0:
                queue.append(tgt)

    # Handle any sections not reached (disconnected)
    for sid in section_ids:
        if sid not in col_assign:
            col_assign[sid] = 0

    # Apply grid overrides
    for sid, (col, row, rowspan, colspan) in graph.grid_overrides.items():
        if sid in graph.sections:
            graph.sections[sid].grid_col = col
            graph.sections[sid].grid_row = row
            graph.sections[sid].grid_row_span = rowspan
            graph.sections[sid].grid_col_span = colspan
            col_assign[sid] = col

    # Group sections by column
    col_groups: dict[int, list[str]] = defaultdict(list)
    for sid, col in col_assign.items():
        col_groups[col].append(sid)

    # Assign rows within each column (order by section number, then by id)
    row_assign: dict[str, int] = {}
    for col, sids in sorted(col_groups.items()):
        explicit = [
            (sid, graph.sections[sid].grid_row)
            for sid in sids
            if graph.sections[sid].grid_row >= 0
        ]
        auto = [sid for sid in sids if graph.sections[sid].grid_row < 0]

        auto.sort(key=lambda s: graph.sections[s].number)

        used_rows: set[int] = set()
        for sid, row in explicit:
            row_assign[sid] = row
            span = graph.sections[sid].grid_row_span
            for r in range(row, row + span):
                used_rows.add(r)

        next_row = 0
        for sid in auto:
            while next_row in used_rows:
                next_row += 1
            row_assign[sid] = next_row
            used_rows.add(next_row)
            next_row += 1

    return col_assign, row_assign


def _compute_section_offsets(
    graph: MetroGraph,
    col_assign: dict[str, int],
    row_assign: dict[str, int],
    section_x_gap: float,
    section_y_gap: float,
) -> tuple[int, int]:
    """Compute pixel offsets for each section from grid assignments.

    Returns (min_col, max_col) for use by downstream gap enforcement.
    """
    min_col = min(col_assign.values()) if col_assign else 0
    max_col = max(col_assign.values()) if col_assign else 0
    for sid in graph.sections:
        cspan = graph.sections[sid].grid_col_span
        col = col_assign.get(sid, 0)
        min_col = min(min_col, col)
        max_col = max(max_col, col + cspan - 1)

    # Right reach re-anchored to the standard left edge so that bbox_x
    # pushed further left (e.g. by terminus-icon clearance) doesn't
    # inflate the column.  Phase 3b of compute_layout absorbs the
    # leftward overhang via a global x_offset bump.
    def _effective_width(section: Section) -> float:
        return section.bbox_x + section.bbox_w + SECTION_X_PADDING

    col_widths: dict[int, float] = defaultdict(float)
    for sid, section in graph.sections.items():
        if section.grid_col_span == 1:
            col = col_assign.get(sid, 0)
            col_widths[col] = max(col_widths[col], _effective_width(section))

    for c in range(min_col, max_col + 1):
        if c not in col_widths:
            col_widths[c] = 0.0

    # Expand columns if a spanning section exceeds spanned column widths
    for sid, section in graph.sections.items():
        cspan = section.grid_col_span
        if cspan <= 1:
            continue
        start_col = col_assign.get(sid, 0)
        spanned = sum(col_widths[c] for c in range(start_col, start_col + cspan))
        spanned += (cspan - 1) * section_x_gap
        eff_w = _effective_width(section)
        if eff_w > spanned:
            deficit = eff_w - spanned
            col_widths[start_col + cspan - 1] += deficit

    # Cumulative x offsets
    col_offsets: dict[int, float] = {}
    cumulative_x = 0.0
    for col in range(min_col, max_col + 1):
        col_offsets[col] = cumulative_x
        cumulative_x += col_widths.get(col, 0) + section_x_gap

    # Global row heights (only single-row non-TB sections)
    max_row = max(row_assign.values()) if row_assign else 0
    for sid in graph.sections:
        span = graph.sections[sid].grid_row_span
        row = row_assign.get(sid, 0)
        max_row = max(max_row, row + span - 1)

    row_heights: dict[int, float] = defaultdict(float)
    for sid, section in graph.sections.items():
        if section.grid_row_span == 1 and section.direction != "TB":
            row = row_assign.get(sid, 0)
            row_heights[row] = max(row_heights[row], section.bbox_h)

    for r in range(max_row + 1):
        if r not in row_heights:
            row_heights[r] = 0.0

    # Expand rows if a spanning section exceeds spanned row heights
    for sid, section in graph.sections.items():
        rspan = section.grid_row_span
        if rspan <= 1:
            continue
        start_row = row_assign.get(sid, 0)
        spanned = sum(row_heights[r] for r in range(start_row, start_row + rspan))
        spanned += (rspan - 1) * section_y_gap
        if section.bbox_h > spanned:
            deficit = section.bbox_h - spanned
            row_heights[start_row + rspan - 1] += deficit

    # Cumulative y offsets per row
    row_offsets: dict[int, float] = {}
    cumulative_y = 0.0
    for r in range(max_row + 1):
        row_offsets[r] = cumulative_y
        cumulative_y += row_heights[r] + section_y_gap

    # TB fold sections visually span into the next row
    tb_sections = sorted(
        [
            (sid, section)
            for sid, section in graph.sections.items()
            if section.direction == "TB" and section.grid_row_span == 1
        ],
        key=lambda x: row_assign.get(x[0], 0),
    )
    for sid, section in tb_sections:
        row = row_assign.get(sid, 0)
        next_row = row + 1
        if next_row not in row_offsets:
            continue
        section.bbox_h += section_y_gap
        tb_bottom = row_offsets[row] + section.bbox_h
        next_row_bottom = row_offsets[next_row] + row_heights[next_row]
        if tb_bottom > next_row_bottom:
            delta = tb_bottom - next_row_bottom
            for r in range(next_row, max_row + 1):
                if r in row_offsets:
                    row_offsets[r] += delta
        next_row_bottom = row_offsets[next_row] + row_heights[next_row]
        section.bbox_h = next_row_bottom - row_offsets[row]

    # Right-align columns containing RL or TB sections
    right_align_cols: set[int] = set()
    for sid, section in graph.sections.items():
        if section.direction in ("RL", "TB") and section.grid_col_span == 1:
            right_align_cols.add(col_assign.get(sid, 0))

    # Set section offsets and adjust for spanning
    for sid, section in graph.sections.items():
        section.grid_col = col_assign.get(sid, 0)
        section.grid_row = row_assign.get(sid, 0)
        section.offset_x = col_offsets.get(section.grid_col, 0)
        section.offset_y = row_offsets.get(section.grid_row, 0)

        if section.grid_col_span == 1 and (
            section.direction in ("RL", "TB") or section.grid_col in right_align_cols
        ):
            col_w = col_widths.get(section.grid_col, 0)
            if col_w > section.bbox_w:
                section.offset_x += col_w - section.bbox_w

    # Align left edges of spanning sections with their starting column.
    # A section that spans multiple columns may have a different local
    # bbox_x from internal layout, causing its left edge to be offset
    # from single-span sections in the same starting column.
    for section in graph.sections.values():
        if section.grid_col_span <= 1:
            continue
        col = section.grid_col
        # Find the representative left edge from single-span sections
        # in the same column.
        peers = [
            s
            for s in graph.sections.values()
            if s.grid_col == col and s.grid_col_span == 1
        ]
        if not peers:
            continue
        target_left = min(s.offset_x + s.bbox_x for s in peers)
        current_left = section.offset_x + section.bbox_x
        if abs(current_left - target_left) > 0.5:
            section.offset_x -= current_left - target_left

        rspan = section.grid_row_span
        if rspan > 1:
            start_row = section.grid_row
            spanned_height = sum(
                row_heights[r] for r in range(start_row, start_row + rspan)
            )
            spanned_height += (rspan - 1) * section_y_gap
            section.bbox_h = spanned_height

        cspan = section.grid_col_span
        if cspan > 1:
            start_col = section.grid_col
            spanned_width = sum(
                col_widths[c] for c in range(start_col, start_col + cspan)
            )
            spanned_width += (cspan - 1) * section_x_gap
            section.bbox_w = spanned_width

    return min_col, max_col


def place_sections(
    graph: MetroGraph,
    section_x_gap: float = PLACEMENT_X_GAP,
    section_y_gap: float = PLACEMENT_Y_GAP,
) -> None:
    """Place sections on the canvas by computing offsets.

    Builds a meta-graph of section dependencies, assigns columns
    via topological layering, assigns rows within columns, then
    computes pixel offsets for each section.
    """
    if not graph.sections:
        return

    section_edges = graph.section_dag.section_edges
    col_assign, row_assign = _assign_grid_layout(graph, section_edges)
    min_col, max_col = _compute_section_offsets(
        graph, col_assign, row_assign, section_x_gap, section_y_gap
    )
    _enforce_min_column_gaps(
        graph, col_assign, min_col, max_col, requested_gap=section_x_gap
    )
    _enforce_min_row_gaps(graph, row_assign, requested_gap=section_y_gap)


def _rows_overlap(a: Section, b: Section) -> bool:
    """Return True if two sections occupy overlapping grid rows."""
    a_start = a.grid_row
    a_end = a_start + a.grid_row_span - 1
    b_start = b.grid_row
    b_end = b_start + b.grid_row_span - 1
    return a_start <= b_end and b_start <= a_end


def _count_lines_between_columns(
    graph: MetroGraph,
    col_assign: dict[str, int],
    col_a: int,
    col_b: int,
) -> int:
    """Count distinct lines routing between two adjacent columns.

    Examines inter-section edges (those crossing section boundaries via
    ports and junctions) to find how many lines will need to route through
    the gap between *col_a* and *col_b*.
    """
    junction_ids = graph.junction_ids
    lines: set[str] = set()

    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if not src or not tgt:
            continue

        # Only inter-section edges (port-to-port or via junctions)
        is_inter = (src.is_port or edge.source in junction_ids) and (
            tgt.is_port or edge.target in junction_ids
        )
        if not is_inter:
            continue

        # Resolve source/target section columns
        src_col = _station_column(graph, src, col_assign, junction_ids)
        tgt_col = _station_column(graph, tgt, col_assign, junction_ids)
        if src_col is None or tgt_col is None:
            continue

        # Check if this edge crosses between col_a and col_b
        lo, hi = min(src_col, tgt_col), max(src_col, tgt_col)
        if lo <= col_a and hi >= col_b:
            lines.add(edge.line_id)

    return len(lines)


def _station_column(
    graph: MetroGraph,
    station,
    col_assign: dict[str, int],
    junction_ids: set[str],
) -> int | None:
    """Resolve a station's grid column via its section or junction chain."""
    if station.section_id and station.section_id in col_assign:
        return col_assign[station.section_id]
    # Junction: trace back to its source port's section
    if station.id in junction_ids:
        for edge in graph.edges_to(station.id):
            src = graph.stations.get(edge.source)
            if src and src.section_id and src.section_id in col_assign:
                return col_assign[src.section_id]
    return None


def _min_gap_for_bundle(
    n_lines: int,
    curve_radius: float = CURVE_RADIUS,
    offset_step: float = OFFSET_STEP,
) -> float:
    """Compute the minimum inter-section gap needed for *n_lines* in a bundle.

    The outermost line in an L-shape bundle has a curve radius of
    ``curve_radius + (n-1) * offset_step``.  The gap must accommodate
    the outermost arc from both sides of the channel midpoint.
    """
    if n_lines <= 0:
        return MIN_INTER_SECTION_GAP
    max_radius = curve_radius + max(n_lines - 1, 0) * offset_step
    return max(2 * max_radius, MIN_INTER_SECTION_GAP)


def _has_merge_routing_in_gap(
    graph: MetroGraph,
    col_assign: dict[str, int],
    col_a: int,
    col_b: int,
) -> bool:
    """Check if any merge junction has bypass routes crossing this gap.

    Returns True when both a branch descent AND a trunk ascent will
    route through the gap between *col_a* and *col_b*, requiring
    extra width for symmetric spacing.
    """
    merge_ids = {j for j in graph.junctions if j.startswith("__merge_")}
    if not merge_ids:
        return False

    junction_ids = graph.junction_ids
    lo, hi = min(col_a, col_b), max(col_a, col_b)

    for mjid in merge_ids:
        mst = graph.stations.get(mjid)
        if not mst:
            continue
        tgt_col = col_assign.get(mst.section_id, -1) if mst.section_id else -1
        # Check if any bypass predecessor crosses this gap
        for edge in graph.edges_to(mjid):
            src_col = _station_column(
                graph,
                graph.stations.get(edge.source),
                col_assign,
                junction_ids,
            )
            if src_col is not None and tgt_col >= 0:
                edge_lo = min(src_col, tgt_col)
                edge_hi = max(src_col, tgt_col)
                if edge_lo <= lo and edge_hi >= hi and edge_hi - edge_lo > 1:
                    return True

    return False


def _enforce_min_column_gaps(
    graph: MetroGraph,
    col_assign: dict[str, int],
    min_col: int,
    max_col: int,
    min_gap: float = MIN_INTER_SECTION_GAP,
    requested_gap: float | None = None,
) -> None:
    """Shift columns rightward so adjacent section bboxes have enough room.

    For each adjacent column pair, computes the minimum gap needed to
    accommodate the routing bundle (based on line count) and the static
    floor (MIN_INTER_SECTION_GAP).  If the physical gap is too narrow,
    columns are shifted rightward.  Warns when the gap had to be widened
    beyond the user's requested value.
    """
    if max_col <= min_col:
        return

    # Group sections by their assigned column
    col_sections: dict[int, list[Section]] = defaultdict(list)
    for sid, section in graph.sections.items():
        col = col_assign.get(sid, 0)
        col_sections[col].append(section)

    for col in range(min_col, max_col):
        left_secs = col_sections.get(col, [])
        right_secs = col_sections.get(col + 1, [])
        if not left_secs or not right_secs:
            continue

        # Compute bundle-aware minimum for this column pair
        n_lines = _count_lines_between_columns(graph, col_assign, col, col + 1)
        bundle_min = _min_gap_for_bundle(n_lines)
        effective_min = max(min_gap, bundle_min)

        # Widen further for gaps with merge junction routing
        if _has_merge_routing_in_gap(graph, col_assign, col, col + 1):
            effective_min = max(effective_min, MERGE_GAP_MIN)

        # Find the tightest gap among row-overlapping section pairs
        worst_gap: float | None = None
        for ls in left_secs:
            for rs in right_secs:
                if not _rows_overlap(ls, rs):
                    continue
                left_edge = ls.offset_x + ls.bbox_x + ls.bbox_w
                right_edge = rs.offset_x + rs.bbox_x
                gap = right_edge - left_edge
                if worst_gap is None or gap < worst_gap:
                    worst_gap = gap

        if worst_gap is None or worst_gap >= effective_min:
            continue

        # Warn if we're overriding the user's requested gap
        if requested_gap is not None and effective_min > requested_gap:
            warnings.warn(
                f"Section gap between columns {col} and {col + 1} "
                f"widened from {requested_gap:.0f}px to {effective_min:.0f}px "
                f"to accommodate {n_lines} routing line(s)",
                stacklevel=2,
            )

        deficit = effective_min - worst_gap
        # Shift all sections in columns > col rightward
        for shift_col in range(col + 1, max_col + 1):
            for s in col_sections.get(shift_col, []):
                s.offset_x += deficit


def _cols_overlap(a: Section, b: Section) -> bool:
    """Return True if two sections overlap horizontally (bbox extent)."""
    a_left = a.offset_x + a.bbox_x
    a_right = a_left + a.bbox_w
    b_left = b.offset_x + b.bbox_x
    b_right = b_left + b.bbox_w
    return a_left < b_right and b_left < a_right


def _enforce_min_row_gaps(
    graph: MetroGraph,
    row_assign: dict[str, int],
    min_gap: float = MIN_INTER_SECTION_ROW_GAP,
    header_protrusion: float = SECTION_HEADER_PROTRUSION,
    requested_gap: float | None = None,
) -> None:
    """Shift rows downward so section headers don't overlap the section above.

    Section headers (number circle + label) protrude above the section
    bbox.  The visual gap is measured from the upper section's bbox
    bottom to the lower section's header top (bbox_y - protrusion).
    Only checks section pairs that share horizontal extent.
    """
    if not row_assign:
        return

    min_row = min(row_assign.values())
    max_row = max(
        row + graph.sections[sid].grid_row_span - 1
        for sid, row in row_assign.items()
        if sid in graph.sections
    )
    if max_row <= min_row:
        return

    # Group sections by their assigned row
    row_sections: dict[int, list[Section]] = defaultdict(list)
    for sid, section in graph.sections.items():
        row = row_assign.get(sid, 0)
        row_sections[row].append(section)

    for row in range(min_row, max_row):
        upper_secs = row_sections.get(row, [])
        lower_secs = row_sections.get(row + 1, [])
        if not upper_secs or not lower_secs:
            continue

        # Find the tightest visual gap among horizontally overlapping pairs.
        # Visual gap = upper bbox bottom to lower section's header top.
        worst_gap: float | None = None
        for us in upper_secs:
            for ls in lower_secs:
                if not _cols_overlap(us, ls):
                    continue
                upper_bottom = us.offset_y + us.bbox_y + us.bbox_h
                lower_header_top = ls.offset_y + ls.bbox_y - header_protrusion
                gap = lower_header_top - upper_bottom
                if worst_gap is None or gap < worst_gap:
                    worst_gap = gap

        if worst_gap is None or worst_gap >= min_gap:
            continue

        # The required bbox-to-bbox distance for the warning message
        required_bbox_gap = min_gap + header_protrusion

        # Warn if we're overriding the user's requested gap
        if requested_gap is not None and required_bbox_gap > requested_gap:
            warnings.warn(
                f"Section gap between rows {row} and {row + 1} "
                f"widened to {required_bbox_gap:.0f}px "
                f"to clear section headers "
                f"(requested {requested_gap:.0f}px)",
                stacklevel=2,
            )

        deficit = min_gap - worst_gap
        for shift_row in range(row + 1, max_row + 1):
            for s in row_sections.get(shift_row, []):
                s.offset_y += deficit


def position_ports(section: Section, graph: MetroGraph) -> None:
    """Position port stations on section boundaries.

    Entry ports go on the entry side, exit ports on the exit side.
    Port Y/X is aligned with the connected internal station where possible.
    Multiple ports on the same side are spaced evenly along the boundary.
    """
    # Group ports by side
    side_ports: dict[PortSide, list[str]] = defaultdict(list)
    for pid in section.entry_ports + section.exit_ports:
        port = graph.ports.get(pid)
        if port:
            side_ports[port.side].append(pid)

    for side, port_ids in side_ports.items():
        if side == PortSide.LEFT:
            _position_ports_on_boundary(
                port_ids, section.bbox_x, section, graph, fixed_axis="x"
            )
        elif side == PortSide.RIGHT:
            right_x = section.bbox_x + section.bbox_w
            _position_ports_on_boundary(
                port_ids, right_x, section, graph, fixed_axis="x"
            )
        elif side == PortSide.TOP:
            _position_ports_on_boundary(
                port_ids, section.bbox_y, section, graph, fixed_axis="y"
            )
        elif side == PortSide.BOTTOM:
            bottom_y = section.bbox_y + section.bbox_h
            _position_ports_on_boundary(
                port_ids, bottom_y, section, graph, fixed_axis="y"
            )

    # TB sections: move LEFT/RIGHT exit ports just below the last
    # internal station (not the section bottom) so lines don't detour
    # to the bottom when the successor is at a similar Y level.
    if section.direction == "TB":
        exit_set = set(section.exit_ports)
        internal_ids = set(section.station_ids) - set(section.entry_ports) - exit_set
        internal_ys = [
            graph.stations[sid].y
            for sid in internal_ids
            if sid in graph.stations and not graph.stations[sid].is_port
        ]
        last_y = max(internal_ys) if internal_ys else section.bbox_y + section.bbox_h
        target_y = last_y + MIN_PORT_STATION_GAP
        for pid in exit_set:
            port = graph.ports.get(pid)
            if port and port.side in (PortSide.LEFT, PortSide.RIGHT):
                station = graph.stations.get(pid)
                if station:
                    station.y = target_y
                port.y = target_y


def _position_ports_on_boundary(
    port_ids: list[str],
    fixed_coord: float,
    section: Section,
    graph: MetroGraph,
    fixed_axis: str,
) -> None:
    """Position ports along a section boundary.

    Args:
        fixed_axis: "x" for vertical boundaries (LEFT/RIGHT),
                    "y" for horizontal boundaries (TOP/BOTTOM).
    """
    if not port_ids:
        return

    # The "free" axis is the one ports can slide along
    free_axis = "y" if fixed_axis == "x" else "x"

    for pid in port_ids:
        station = graph.stations.get(pid)
        if not station:
            continue

        port = graph.ports.get(pid)
        # LEFT/RIGHT exit ports prefer the downstream bundle Y so the
        # inter-section run stays horizontal; fall back to the local
        # internal-station average for entry ports and fan-in exits.
        anchor: float | None = None
        if free_axis == "y" and port is not None and not port.is_entry:
            anchor = _find_downstream_bundle_y(pid, section, graph)
        if anchor is None:
            anchor = _find_connected_internal_coord(pid, section, graph, free_axis)
        if free_axis == "y":
            default = section.bbox_y + section.bbox_h / 2
        else:
            default = section.bbox_x + section.bbox_w / 2

        setattr(station, fixed_axis, fixed_coord)
        setattr(station, free_axis, anchor if anchor is not None else default)

        if port:
            port.x = station.x
            port.y = station.y

    if free_axis == "y":
        span_start = section.bbox_y
        span_end = section.bbox_y + section.bbox_h
    else:
        span_start = section.bbox_x
        span_end = section.bbox_x + section.bbox_w

    _spread_overlapping_ports(
        port_ids,
        graph,
        axis=free_axis,
        span_start=span_start,
        span_end=span_end,
    )


def _find_downstream_bundle_y(
    exit_port_id: str,
    section: Section,
    graph: MetroGraph,
) -> float | None:
    """Find the Y where this exit port's bundle materialises downstream.

    Traces forward to the downstream entry ports (direct or via a
    fan-out junction) and resolves the Y of the topmost internal
    station each one feeds when the fan-out is a parallel bundle
    (every internal station carries the same line set).  Returns the
    bundle Y when all same-row downstream entries agree and the value
    fits inside this section's bbox, otherwise None so the caller can
    fall back to the local-internal centre.
    """
    junction_ids = graph.junction_ids
    ports = graph.ports
    stations = graph.stations
    sections = graph.sections
    same_row = section.grid_row

    # Fan-in exits stay centred: 2+ distinct internal source Ys means
    # the visual convergence is meaningful and downstream anchoring
    # would collapse the bundle onto one source.
    internal_ids = (
        set(section.station_ids) - set(section.entry_ports) - set(section.exit_ports)
    )
    src_ys: set[float] = set()
    for sid in internal_ids:
        for edge in graph.edges_from(sid):
            if edge.target != exit_port_id:
                continue
            st = stations.get(sid)
            if st and not st.is_port:
                src_ys.add(round(st.y, 1))
                if len(src_ys) >= 2:
                    return None
                break

    entry_ids: list[str] = []
    for edge in graph.edges_from(exit_port_id):
        tgt = edge.target
        if tgt in ports and ports[tgt].is_entry:
            entry_ids.append(tgt)
        elif tgt in junction_ids:
            for e2 in graph.edges_from(tgt):
                if e2.target in ports and ports[e2.target].is_entry:
                    entry_ids.append(e2.target)
    if not entry_ids:
        return None

    candidates: list[float] = []
    for eid in entry_ids:
        ep = ports.get(eid)
        if not ep:
            continue
        ds = sections.get(ep.section_id)
        if not ds or ds.grid_row != same_row:
            continue
        ds_internal = set(ds.station_ids) - set(ds.entry_ports) - set(ds.exit_ports)
        targets: dict[str, set[str]] = {}
        for edge in graph.edges_from(eid):
            if edge.target not in ds_internal:
                continue
            st = stations.get(edge.target)
            if st and not st.is_port:
                targets.setdefault(edge.target, set()).add(edge.line_id)
        if not targets:
            continue
        line_sets = iter(targets.values())
        first = next(line_sets)
        if any(ls != first for ls in line_sets):
            # Branch fan-out: different stations carry different lines.
            return None
        candidates.append(min(stations[sid].y for sid in targets))

    if not candidates or max(candidates) - min(candidates) > 1.0:
        return None
    target_y = candidates[0]
    if not (section.bbox_y <= target_y <= section.bbox_y + section.bbox_h):
        return None
    return target_y


def _find_connected_internal_coord(
    port_id: str,
    section: Section,
    graph: MetroGraph,
    axis: str,
) -> float | None:
    """Find the coordinate to align a port with its connected internal stations.

    Returns the average X or Y (determined by *axis*) of all connected
    internal stations, or None if no connections found.  Bypass V
    helpers (ids starting with ``__bypass_``) are skipped so the port
    anchors to the visible trunk rather than an off-trunk routing aid.
    """
    internal_ids = (
        set(section.station_ids) - set(section.entry_ports) - set(section.exit_ports)
    )
    vals: list[float] = []
    for edge in graph.edges_from(port_id):
        if edge.target in internal_ids and not edge.target.startswith("__bypass_"):
            vals.append(getattr(graph.stations[edge.target], axis))
    for edge in graph.edges_to(port_id):
        if edge.source in internal_ids and not edge.source.startswith("__bypass_"):
            vals.append(getattr(graph.stations[edge.source], axis))
    if vals:
        return sum(vals) / len(vals)
    return None


def _spread_overlapping_ports(
    port_ids: list[str],
    graph: MetroGraph,
    axis: str,
    span_start: float,
    span_end: float,
    min_gap: float = PORT_MIN_GAP,
) -> None:
    """Spread ports that ended up at the same position."""
    if len(port_ids) <= 1:
        return

    # Check for overlap
    positions: list[tuple[str, float]] = []
    for pid in port_ids:
        station = graph.stations.get(pid)
        if station:
            pos = station.y if axis == "y" else station.x
            positions.append((pid, pos))

    positions.sort(key=lambda p: p[1])

    # Check if any ports overlap
    needs_spread = False
    for i in range(1, len(positions)):
        if abs(positions[i][1] - positions[i - 1][1]) < min_gap:
            needs_spread = True
            break

    if not needs_spread:
        return

    # Evenly space ports along the span
    n = len(positions)
    margin = min_gap
    available = (span_end - span_start) - 2 * margin
    step = available / max(n - 1, 1)

    for i, (pid, _) in enumerate(positions):
        new_pos = span_start + margin + i * step
        station = graph.stations.get(pid)
        if station:
            if axis == "y":
                station.y = new_pos
            else:
                station.x = new_pos
            port = graph.ports.get(pid)
            if port:
                if axis == "y":
                    port.y = new_pos
                else:
                    port.x = new_pos
