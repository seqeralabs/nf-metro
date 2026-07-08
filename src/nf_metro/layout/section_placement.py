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
from collections.abc import Callable, Iterator

from nf_metro.layout.constants import (
    BUNDLE_TO_BUNDLE_CLEARANCE,
    CURVE_RADIUS,
    EDGE_TO_BUNDLE_CLEARANCE,
    INTER_ROW_HEADER_CLEARANCE,
    MERGE_GAP_MIN,
    MIN_INTER_SECTION_GAP,
    MIN_INTER_SECTION_ROW_GAP,
    MIN_PORT_STATION_GAP,
    MIN_STATION_FLAT_LENGTH,
    OFFSET_STEP,
    PLACEMENT_X_GAP,
    PLACEMENT_Y_GAP,
    PORT_MIN_GAP,
    SAME_COORD_TOLERANCE,
    SECTION_HEADER_PROTRUSION,
    SECTION_X_PADDING,
    resolve_offset_step,
)
from nf_metro.layout.geometry import (
    AxisFrame,
    lanes_run_along_x,
    lanes_run_along_y,
    shift_section,
)
from nf_metro.layout.routing.common import (
    inter_row_wrap_band,
    max_grid_row_with_content,
    resolve_section,
    section_exists_above_row,
)
from nf_metro.parser.model import (
    Edge,
    MetroGraph,
    Port,
    PortSide,
    Section,
    Station,
    is_bypass_v,
)


def _assign_grid_layout(
    graph: MetroGraph,
    section_edges: set[tuple[str, str]],
) -> tuple[dict[str, int], dict[str, int]]:
    """Assign grid columns and rows to each section.

    Returns (col_assign, row_assign) dicts mapping section IDs to positions.
    Only sections in ``graph.sections`` participate; callers laying out a
    single weakly-connected component scope ``graph.sections`` to it (via
    ``_scoped_sections``) so its grid is computed in isolation.
    """
    section_ids = list(graph.sections.keys())

    # Topological layering (columns).  ``section_edges`` is a set, so iterate
    # it in a stable (sorted) order: the adjacency lists it builds drive the
    # BFS queue and the column-group ordering below, and a hash-randomised
    # traversal order would otherwise make row assignment depend on
    # PYTHONHASHSEED.
    section_set = set(section_ids)
    in_degree: dict[str, int] = {sid: 0 for sid in section_ids}
    adj: dict[str, list[str]] = {sid: [] for sid in section_ids}
    for src, tgt in sorted(section_edges):
        if src not in section_set or tgt not in section_set:
            continue
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
        if sid not in section_ids:
            continue
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
    for col, col_sids in sorted(col_groups.items()):
        explicit = [
            (sid, graph.sections[sid].grid_row)
            for sid in col_sids
            if graph.sections[sid].grid_row >= 0
        ]
        auto = [sid for sid in col_sids if graph.sections[sid].grid_row < 0]

        # Pack auto-row sections by definition number, with the section id as
        # a deterministic tie-break.  ``col_sids`` derives from a set-built
        # adjacency, so sorting on number alone would leave equal-numbered
        # sections in hash-randomised order and flip their rows run-to-run.
        auto.sort(key=lambda s: (graph.sections[s].number, s))

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

    The set of sections handled is exactly ``col_assign``'s keys.  In the
    single-grid path that is every section; in the per-component path it is
    one weakly-connected component, so other components' sections are left
    untouched and never inflate this component's column widths.
    """
    scoped = {sid: graph.sections[sid] for sid in col_assign if sid in graph.sections}
    packs = _scoped_cell_packs(graph, scoped)
    packed_members = {m for members in packs.values() for m in members}

    min_col = min(col_assign.values()) if col_assign else 0
    max_col = max(col_assign.values()) if col_assign else 0
    for sid in scoped:
        cspan = scoped[sid].grid_col_span
        col = col_assign.get(sid, 0)
        min_col = min(min_col, col)
        max_col = max(max_col, col + cspan - 1)

    col_widths = _compute_col_widths(
        scoped, col_assign, min_col, max_col, section_x_gap, packs
    )

    # Cumulative x offsets
    col_offsets: dict[int, float] = {}
    cumulative_x = 0.0
    for col in range(min_col, max_col + 1):
        col_offsets[col] = cumulative_x
        cumulative_x += col_widths.get(col, 0) + section_x_gap

    max_row = max(row_assign.values()) if row_assign else 0
    for sid in scoped:
        span = scoped[sid].grid_row_span
        row = row_assign.get(sid, 0)
        max_row = max(max_row, row + span - 1)

    row_heights = _compute_row_heights(scoped, row_assign, max_row, section_y_gap)

    # Cumulative y offsets per row
    row_offsets: dict[int, float] = {}
    cumulative_y = 0.0
    for r in range(max_row + 1):
        row_offsets[r] = cumulative_y
        cumulative_y += row_heights[r] + section_y_gap

    _apply_tb_fold_spans(
        scoped, row_assign, row_offsets, row_heights, max_row, section_y_gap
    )

    # Right-align columns containing RL or TB sections
    right_align_cols: set[int] = set()
    for sid, section in scoped.items():
        if section.direction in ("RL", "TB") and section.grid_col_span == 1:
            right_align_cols.add(col_assign.get(sid, 0))

    # Set section offsets and adjust for spanning.  Packed members keep their
    # row offset here but defer their column offset to ``_pack_cells``, which
    # lays them side-by-side within the shared cell.
    for sid, section in scoped.items():
        section.grid_col = col_assign.get(sid, 0)
        section.grid_row = row_assign.get(sid, 0)
        section.offset_y = row_offsets.get(section.grid_row, 0)
        if sid in packed_members:
            continue
        section.offset_x = col_offsets.get(section.grid_col, 0)

        if section.grid_col_span == 1 and (
            section.direction in ("RL", "TB") or section.grid_col in right_align_cols
        ):
            col_w = col_widths.get(section.grid_col, 0)
            if col_w > section.bbox_w:
                section.offset_x += col_w - section.bbox_w

    _pack_cells(scoped, packs, col_offsets, col_widths, right_align_cols, section_x_gap)

    _finalize_spanning_sections(
        scoped, col_widths, row_heights, section_x_gap, section_y_gap
    )

    return min_col, max_col


def _effective_section_width(section: Section) -> float:
    """Section right reach re-anchored to the standard left edge.

    Keeps bbox_x pushed further left (e.g. by terminus-icon clearance) from
    inflating the column; Stage 1.5 of compute_layout absorbs the leftward
    overhang via a global x_offset bump.
    """
    return section.bbox_x + section.bbox_w + SECTION_X_PADDING


def _scoped_cell_packs(
    graph: MetroGraph,
    scoped: dict[str, Section],
) -> dict[tuple[int, int], list[str]]:
    """Multi-section cells whose members all fall within ``scoped``.

    A cell with only one in-scope member needs no packing and is dropped, so
    every returned list has at least two members laid out as a unit.
    """
    packs: dict[tuple[int, int], list[str]] = {}
    for cell, members in graph.cell_packs.items():
        present = [m for m in members if m in scoped]
        if len(present) > 1:
            packs[cell] = present
    return packs


def _packed_content_width(members: list[Section], section_x_gap: float) -> float:
    """Total width a packed cell occupies: members' effective widths plus the
    inter-section gaps between them.

    The packer (:func:`_pack_cells`) and the column-width reservation
    (:func:`_compute_col_widths`) must agree on this, or the pack overruns the
    width its column reserved and the overlap guard fires.
    """
    return (
        sum(_effective_section_width(s) for s in members)
        + (len(members) - 1) * section_x_gap
    )


def _pack_cells(
    scoped: dict[str, Section],
    packs: dict[tuple[int, int], list[str]],
    col_offsets: dict[int, float],
    col_widths: dict[int, float],
    right_align_cols: set[int],
    section_x_gap: float,
) -> None:
    """Lay each multi-section cell's members side-by-side along the flow axis.

    Members advance with the same cumulative-offset rule as columns do (each
    reserves its effective width plus the inter-section gap).  A pack in a
    right-aligned column hugs that column's right edge; otherwise it
    left-aligns.  A cell whose flow runs leftward (``RL``) reverses its members
    so the flow-leading one ends up rightmost.
    """
    for (col, _row), member_ids in packs.items():
        members = [scoped[m] for m in member_ids]
        content_w = _packed_content_width(members, section_x_gap)
        cell_left = col_offsets.get(col, 0.0)
        col_w = col_widths.get(col, content_w)
        start_x = cell_left + (col_w - content_w if col in right_align_cols else 0.0)
        lead = members[0].direction
        flows_leftward = lanes_run_along_y(lead) and AxisFrame.flow_sign(lead) < 0
        visual = list(reversed(members)) if flows_leftward else members
        x = start_x
        for section in visual:
            section.offset_x = x
            x += _effective_section_width(section) + section_x_gap


def _compute_col_widths(
    scoped: dict[str, Section],
    col_assign: dict[str, int],
    min_col: int,
    max_col: int,
    section_x_gap: float,
    packs: dict[tuple[int, int], list[str]],
) -> dict[int, float]:
    """Per-column width: widest single-span cell, expanded for spans.

    A single-section cell contributes its own effective width; a multi-section
    cell contributes the combined width of its members plus the inter-section
    gaps between them.
    """
    col_widths: dict[int, float] = defaultdict(float)
    packed_members = {m for members in packs.values() for m in members}
    for sid, section in scoped.items():
        if section.grid_col_span == 1 and sid not in packed_members:
            col = col_assign.get(sid, 0)
            col_widths[col] = max(col_widths[col], _effective_section_width(section))

    for (col, _row), member_ids in packs.items():
        combined = _packed_content_width([scoped[m] for m in member_ids], section_x_gap)
        col_widths[col] = max(col_widths[col], combined)

    for c in range(min_col, max_col + 1):
        if c not in col_widths:
            col_widths[c] = 0.0

    for sid, section in scoped.items():
        cspan = section.grid_col_span
        if cspan <= 1:
            continue
        start_col = col_assign.get(sid, 0)
        spanned = sum(col_widths[c] for c in range(start_col, start_col + cspan))
        spanned += (cspan - 1) * section_x_gap
        eff_w = _effective_section_width(section)
        if eff_w > spanned:
            col_widths[start_col + cspan - 1] += eff_w - spanned
    return col_widths


def _compute_row_heights(
    scoped: dict[str, Section],
    row_assign: dict[str, int],
    max_row: int,
    section_y_gap: float,
) -> dict[int, float]:
    """Per-row height: tallest single-row lane-on-Y section, expanded for spans."""
    row_heights: dict[int, float] = defaultdict(float)
    for sid, section in scoped.items():
        if section.grid_row_span == 1 and lanes_run_along_y(section.direction):
            row = row_assign.get(sid, 0)
            row_heights[row] = max(row_heights[row], section.bbox_h)

    for r in range(max_row + 1):
        if r not in row_heights:
            row_heights[r] = 0.0

    for sid, section in scoped.items():
        rspan = section.grid_row_span
        if rspan <= 1:
            continue
        start_row = row_assign.get(sid, 0)
        spanned = sum(row_heights[r] for r in range(start_row, start_row + rspan))
        spanned += (rspan - 1) * section_y_gap
        if section.bbox_h > spanned:
            row_heights[start_row + rspan - 1] += section.bbox_h - spanned
    return row_heights


def _apply_tb_fold_spans(
    scoped: dict[str, Section],
    row_assign: dict[str, int],
    row_offsets: dict[int, float],
    row_heights: dict[int, float],
    max_row: int,
    section_y_gap: float,
) -> None:
    """Push lower rows down where a TB fold section spills into the next row."""
    tb_sections = sorted(
        [
            (sid, section)
            for sid, section in scoped.items()
            if section.direction == "TB" and section.grid_row_span == 1
        ],
        key=lambda x: row_assign.get(x[0], 0),
    )
    for sid, section in tb_sections:
        row = row_assign.get(sid, 0)
        next_row = row + 1
        if next_row not in row_offsets:
            continue
        # A BOTTOM-only exit routes straight down; no sideways fold routing
        # through the inter-row gap, so no fold-span extension is needed.
        _exit_sides = {s for s, _ in section.exit_hints}
        if PortSide.BOTTOM in _exit_sides and not _exit_sides & {
            PortSide.LEFT,
            PortSide.RIGHT,
        }:
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


def _finalize_spanning_sections(
    scoped: dict[str, Section],
    col_widths: dict[int, float],
    row_heights: dict[int, float],
    section_x_gap: float,
    section_y_gap: float,
) -> None:
    """Align spanning-section left edges to their column and size their bbox.

    A section that spans multiple columns may have a different local bbox_x
    from internal layout, offsetting its left edge from single-span sections
    in the same starting column.
    """
    for section in scoped.values():
        if section.grid_col_span <= 1:
            continue
        col = section.grid_col
        peers = [
            s for s in scoped.values() if s.grid_col == col and s.grid_col_span == 1
        ]
        if not peers:
            continue
        target_left = min(s.offset_x + s.bbox_x for s in peers)
        current_left = section.offset_x + section.bbox_x
        if abs(current_left - target_left) > SAME_COORD_TOLERANCE:
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


def _weakly_connected_components(
    graph: MetroGraph,
    section_edges: set[tuple[str, str]],
) -> list[set[str]]:
    """Partition sections into weakly-connected components.

    Treats inter-section edges as undirected.  Each returned set is one
    group of sections with no inter-section edge to any other group.
    """
    import networkx as nx

    meta: nx.Graph[str] = nx.Graph()
    meta.add_nodes_from(graph.sections)
    for src, tgt in sorted(section_edges):
        if src in graph.sections and tgt in graph.sections:
            meta.add_edge(src, tgt)
    # ``nx.connected_components`` yields ``set`` objects in a traversal order
    # that depends on hash iteration; return them in a stable order (by
    # smallest member id) so any downstream iteration that does not re-sort
    # is still deterministic.  Callers that need a particular stacking order
    # re-sort on their own key.
    return sorted(
        (set(c) for c in nx.connected_components(meta)),
        key=lambda c: min(c),
    )


def _component_extent(sections: list[Section]) -> tuple[float, float, float]:
    """Global ``(left, top, bottom)`` bounding extent of placed sections.

    Coordinates are in canvas space (``offset`` + local ``bbox``), so the
    extent is comparable across components placed in separate local grids.
    """
    left = min(s.offset_x + s.bbox_x for s in sections)
    top = min(s.offset_y + s.bbox_y for s in sections)
    bottom = max(s.offset_y + s.bbox_y + s.bbox_h for s in sections)
    return left, top, bottom


def _place_section_group(
    graph: MetroGraph,
    section_edges: set[tuple[str, str]],
    section_x_gap: float,
    section_y_gap: float,
    sids: set[str] | None,
) -> None:
    """Run column-grid placement for one section group (or all sections).

    With ``sids`` set, ``graph.sections`` is temporarily restricted to that
    weakly-connected component so its column widths and row heights are
    computed in isolation from the other components.  ``None`` places every
    section in one shared grid.
    """
    from contextlib import nullcontext

    from nf_metro.layout.phases._common import _scoped_sections

    scope = _scoped_sections(graph, sorted(sids)) if sids is not None else nullcontext()
    with scope:
        col_assign, row_assign = _assign_grid_layout(graph, section_edges)
        min_col, max_col = _compute_section_offsets(
            graph, col_assign, row_assign, section_x_gap, section_y_gap
        )
        _enforce_min_column_gaps(
            graph, col_assign, min_col, max_col, requested_gap=section_x_gap
        )
        _enforce_min_row_gaps(
            graph,
            row_assign,
            requested_gap=section_y_gap,
            wrap_min_by_pair=_inter_row_routing_minimums(graph),
        )


def place_sections(
    graph: MetroGraph,
    section_x_gap: float = PLACEMENT_X_GAP,
    section_y_gap: float = PLACEMENT_Y_GAP,
) -> None:
    """Place sections on the canvas by computing offsets.

    Builds a meta-graph of section dependencies, assigns columns
    via topological layering, assigns rows within columns, then
    computes pixel offsets for each section.

    Disconnected graphs (two or more weakly-connected components in the
    section meta-graph) are placed one component at a time, each in its own
    local column grid, then stacked vertically so a wide component never
    inflates another component's column widths.  Components are stacked in
    a deterministic order -- ascending minimum original grid row, then
    descending section count, then the lexicographically smallest section
    id as a final tie-break -- left-aligned and separated by
    ``section_y_gap``.  A single-component graph, or any graph carrying an
    explicit ``%%metro grid:`` override (where the author may have
    deliberately interleaved components in one shared grid), falls back to
    the legacy single-grid placement and is unaffected.
    """
    if not graph.sections:
        return

    # auto_layout always populates section_dag before placement runs.
    assert graph.section_dag is not None
    section_edges = graph.section_dag.section_edges

    components = _weakly_connected_components(graph, section_edges)
    if len(components) <= 1 or graph._explicit_grid:
        _place_section_group(graph, section_edges, section_x_gap, section_y_gap, None)
        _reserve_over_top_headroom(graph)
        return

    # Deterministic stacking order: top-most original row first, then the
    # larger components, then a stable id tie-break.  ``grid_row`` is the
    # row auto_layout assigns before placement runs; it is valid here even
    # though per-component placement has not yet recomputed offsets.
    def _min_row(comp: set[str]) -> int:
        return min(graph.sections[sid].grid_row for sid in comp)

    ordered = sorted(
        components,
        key=lambda comp: (_min_row(comp), -len(comp), min(comp)),
    )

    # Anchor the stack to the first (top) component's natural top-left, so it
    # keeps the origin a single-grid layout would give it; later components are
    # left-aligned to that edge and stacked below.
    origin_left = 0.0
    stack_y = 0.0
    next_grid_row = 0
    for idx, comp in enumerate(ordered):
        _place_section_group(graph, section_edges, section_x_gap, section_y_gap, comp)
        # Sort members so the translation never depends on set hash order.
        comp_secs = [graph.sections[sid] for sid in sorted(comp)]
        left, top, bottom = _component_extent(comp_secs)
        if idx == 0:
            origin_left = left
            stack_y = top
        dx = origin_left - left
        dy = stack_y - top
        for s in comp_secs:
            s.offset_y += dy
            s.offset_x += dx
        stack_y += (bottom - top) + section_y_gap

        # The inter-row cascade reasons by ``grid_row`` and assumes it rises
        # monotonically (and contiguously) with Y.  Per-component placement
        # leaves rows in disjoint local numberings that can collide, so
        # renormalise each component onto a contiguous global band matching
        # the physical stack order.
        row_shift = next_grid_row - min(s.grid_row for s in comp_secs)
        for s in comp_secs:
            s.grid_row += row_shift
        next_grid_row = max(s.grid_row + s.grid_row_span - 1 for s in comp_secs) + 1
    _reserve_over_top_headroom(graph)


def _reserve_over_top_headroom(graph: MetroGraph) -> None:
    """Shift rows down to clear the canvas title for an over-the-top loop.

    A RIGHT-entry section fed by a same-row, lower-column source is reached by
    a loop over the section's top (see ``_route_right_entry_over_top``), whether
    the entry is a vertical-flow section's cross-axis port or a reversed (RL)
    section's leading port.  When no section sits above that section's row, the
    loop's horizontal would climb into the canvas-top title band.  Reserve a
    header-clearance band above the topmost such row -- pushing it and every
    lower row down -- so the loop runs between the title and the header badge.
    """
    headroom = INTER_ROW_HEADER_CLEARANCE + CURVE_RADIUS
    target_rows: set[int] = set()
    for port in graph.ports.values():
        if not (port.is_entry and port.side == PortSide.RIGHT):
            continue
        psec = graph.sections.get(port.section_id)
        if psec is None:
            continue
        for edge in graph.edges_to(port.id):
            src_port = graph.ports.get(edge.source)
            ssec = graph.sections.get(src_port.section_id) if src_port else None
            if ssec is None:
                continue
            if ssec.grid_row == psec.grid_row and _section_precedes(graph, ssec, psec):
                if not section_exists_above_row(graph, psec.grid_row):
                    target_rows.add(psec.grid_row)
                break
    if not target_rows:
        return
    min_target = min(target_rows)
    for section in graph.sections.values():
        if section.grid_row >= min_target:
            section.offset_y += headroom


def _rows_overlap(a: Section, b: Section) -> bool:
    """Return True if two sections occupy overlapping grid rows."""
    a_start = a.grid_row
    a_end = a_start + a.grid_row_span - 1
    b_start = b.grid_row
    b_end = b_start + b.grid_row_span - 1
    return a_start <= b_end and b_start <= a_end


def _station_column(
    graph: MetroGraph,
    station: Station | None,
    col_assign: dict[str, int],
    junction_ids: set[str],
) -> int | None:
    """Resolve a station's grid column via its section or junction chain."""
    if station is None:
        return None
    if station.section_id and station.section_id in col_assign:
        return col_assign[station.section_id]
    # Junction: trace back to its source port's section
    if station.id in junction_ids:
        for edge in graph.edges_to(station.id):
            src = graph.stations.get(edge.source)
            if src and src.section_id and src.section_id in col_assign:
                return col_assign[src.section_id]
    return None


def _wrap_bundle_row_minimums(graph: MetroGraph) -> dict[tuple[int, int], float]:
    """Minimum bbox-to-bbox row gap each inter-row wrap bundle needs.

    An inter-section bundle that crosses grid rows into a horizontal-side
    (LEFT/RIGHT) entry port wraps through the inter-row gap as a
    horizontal run (see ``_route_left_entry_wrap``).  To keep that run
    clear of both bounding obstacles it needs a band of
    ``INTER_ROW_EDGE_CLEARANCE`` below the upper bbox bottom, the bundle
    span, and ``INTER_ROW_HEADER_CLEARANCE`` above the lower row (clearing
    its header badge, not just the bbox edge); a narrow gap squeezes the
    bundle flush against a box.  Returns, per adjacent
    ``(upper_row, lower_row)`` pair, the required bbox-to-bbox gap so the
    placement pass can widen too-tight rows.

    A multi-row-span *source* routes via reversal handling, not this
    channel, and is excluded; a multi-row-span *target* (rowspan grid
    placement) is fine -- only its top edge bounds the gap, so the
    adjacent ``(src_row, tgt_row)`` reservation still applies.
    """
    # (upper_row, lower_row) -> entry_port_id -> set of line ids
    per_gap: dict[tuple[int, int], dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    # LEFT/RIGHT entries wrap; TOP entries drop in via a horizontal lead-in.
    # Both place a horizontal run in the inter-row gap (``inter_row_channel_y``)
    # that must clear the next-row header.
    sides = (PortSide.LEFT, PortSide.RIGHT, PortSide.TOP)
    for edge, port, src_sec, tgt_sec in _entry_wrap_edges(graph, sides):
        # The source must be single-row: its bottom edge is the gap's upper
        # bound, and a multi-row source routes via reversal handling.
        if src_sec.grid_row_span != 1:
            continue
        # A horizontal-side entry only WRAPS (placing a flush run in the
        # inter-row gap) when the source is on the far side of the target
        # from the port.  A LEFT entry reached from a source in the same or
        # a righthand column wraps; one reached from the left is a plain
        # L-shape drop and needs no widening (e.g. preprocessing -> a
        # column-1 section below it).  Mirror for RIGHT.
        if port.side == PortSide.LEFT and _section_precedes(graph, src_sec, tgt_sec):
            continue
        if port.side == PortSide.RIGHT and _section_precedes(graph, tgt_sec, src_sec):
            continue
        src_row, tgt_row = src_sec.grid_row, tgt_sec.grid_row
        if abs(src_row - tgt_row) == 1:
            # Adjacent-row wrap: the run centres in the gap this pair separates.
            gap = (src_row, tgt_row) if tgt_row > src_row else (tgt_row, src_row)
        elif port.side is PortSide.LEFT and src_row < tgt_row:
            # A multi-row LEFT entry reached from a source ABOVE runs its long
            # horizontal in the band abutting the target row
            # (``_route_left_entry_via_gap_above``) when that band is clear;
            # reserve room there so the band fits, else the feed falls to the
            # canvas-bottom dive for want of a channel.
            gap = (tgt_row - 1, tgt_row)
        else:
            # A multi-row crossing routed around below the stack
            # (``_route_around_section_below``) is sized by that path.
            continue
        per_gap[gap][edge.target].add(edge.line_id)

    offset_step = resolve_offset_step(graph.track_gap)
    return {
        gap: inter_row_wrap_band(widest, offset_step)
        for gap, widest in _widest_lines_per_gap(per_gap).items()
    }


def _entry_wrap_edges(
    graph: MetroGraph, sides: tuple[PortSide, ...]
) -> Iterator[tuple[Edge, Port, Section, Section]]:
    """Yield ``(edge, port, src_sec, tgt_sec)`` for entry-port edges on *sides*.

    Shared preamble of the inter-row-gap reservation passes: an edge into an
    entry port whose side is in *sides*, with both its target section and its
    source's section resolvable.
    """
    for edge in graph.edges:
        port = graph.ports.get(edge.target)
        if port is None or not port.is_entry or port.side not in sides:
            continue
        tgt_sec = graph.sections.get(port.section_id)
        src = graph.stations.get(edge.source)
        if tgt_sec is None or src is None:
            continue
        src_sec = resolve_section(graph, src)
        if src_sec is None:
            continue
        yield edge, port, src_sec, tgt_sec


def _widest_lines_per_gap(
    per_gap: dict[tuple[int, int], dict[str, set[str]]],
) -> dict[tuple[int, int], int]:
    """Reduce a ``gap -> port -> line ids`` map to the widest line count per gap."""
    return {
        gap: max(len(lines) for lines in ports.values())
        for gap, ports in per_gap.items()
    }


def _merge_trunk_row_minimums(graph: MetroGraph) -> dict[tuple[int, int], float]:
    """Minimum bbox-to-bbox row gap a bottommost-row merge trunk needs.

    A merge junction whose entry sits in the bottommost grid row is fed by a
    trunk that crosses rows; rather than diving below the whole canvas, its
    bypass channel routes in the inter-row gap *above* the bottom row (see
    ``bypass_bottom_y``).  That channel is a horizontal run spanning the
    trunk's columns, so -- like an inter-row wrap bundle -- it needs
    ``INTER_ROW_EDGE_CLEARANCE`` below the upper row's bbox, the bundle span,
    and ``INTER_ROW_HEADER_CLEARANCE`` above the bottom row's header.  Unlike a
    wrap, the bottom-row target need not share a column with the upper-row
    sections the channel crosses, so the column-overlap test that gates header
    widening misses this gap; the reservation here makes the placement pass
    widen it via the envelope rule instead.

    Returns the required bbox-to-bbox gap for the ``(max_row - 1, max_row)``
    pair when any such merge exists; empty otherwise.
    """
    merge_ids = {j for j in graph.junctions if j.startswith("__merge_")}
    if not merge_ids:
        return {}
    max_row = max_grid_row_with_content(graph)
    if not max_row:
        return {}

    widest = 0
    for mjid in merge_ids:
        mst = graph.stations.get(mjid)
        if mst is None or not mst.section_id:
            continue
        tgt_sec = graph.sections.get(mst.section_id)
        if tgt_sec is None or tgt_sec.grid_row != max_row:
            continue
        lines: set[str] = set()
        crosses_row = False
        for edge in graph.edges_to(mjid):
            src_sec = resolve_section(graph, graph.stations.get(edge.source))
            if src_sec is not None and src_sec.grid_row < max_row:
                crosses_row = True
            lines.add(edge.line_id)
        if crosses_row:
            widest = max(widest, len(lines))

    if widest == 0:
        return {}
    offset_step = resolve_offset_step(graph.track_gap)
    return {(max_row - 1, max_row): inter_row_wrap_band(widest, offset_step)}


def _inter_row_routing_minimums(graph: MetroGraph) -> dict[tuple[int, int], float]:
    """Per-gap bbox-to-bbox minimum for every horizontal run an inter-row gap
    must host: entry-wrap bundles and bottommost-row merge-trunk channels.

    Both place a flush horizontal run in the gap; a pair claimed by both takes
    the wider reservation.
    """
    wrap = _wrap_bundle_row_minimums(graph)
    minimums = dict(wrap)
    for gap, band in _merge_trunk_row_minimums(graph).items():
        minimums[gap] = max(minimums.get(gap, 0.0), band)
    # A same-row over-top wrap (a feed from a section to a horizontal-side entry
    # of a same-row neighbour, looping over the neighbour's top) shares the gap
    # above its row with any longer-haul through bundle already reserved there.
    # The two must stack -- the wrap pinned near the row by its header clearance,
    # the through bundle lifted above it -- so reserve their summed band.
    offset_step = resolve_offset_step(graph.track_gap)
    for gap, over_top_lines in _over_top_wrap_row_minimums(graph).items():
        through_band = wrap.get(gap)
        if through_band is None:
            continue
        # Stack the over-top wrap beneath the through bundle already reserved for
        # the gap: add an inter-bundle separation, the wrap's own span, and a
        # curve radius for the wrap's turn down into its port.
        stacked = (
            through_band
            + BUNDLE_TO_BUNDLE_CLEARANCE
            + max(over_top_lines - 1, 0) * offset_step
            + CURVE_RADIUS
        )
        minimums[gap] = max(minimums.get(gap, 0.0), stacked)
    return minimums


def _over_top_wrap_row_minimums(graph: MetroGraph) -> dict[tuple[int, int], int]:
    """Per gap-above-a-row, the widest same-row over-top wrap bundle it hosts.

    A feed reaching a RIGHT entry from a same-row source to its left (or a LEFT
    entry from a same-row source to its right) loops over the target's top,
    placing a horizontal run in the gap *above* the target's row.  Returns, per
    ``(row-1, row)`` gap, the line count of the widest such wrap, so the gap can
    reserve room for it to stack above any through bundle.
    """
    per_gap: dict[tuple[int, int], dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for edge, port, src_sec, tgt_sec in _entry_wrap_edges(
        graph, (PortSide.LEFT, PortSide.RIGHT)
    ):
        if src_sec.grid_row != tgt_sec.grid_row:
            continue
        src_left = _section_precedes(graph, src_sec, tgt_sec)
        if port.side is PortSide.RIGHT and not src_left:
            continue
        if port.side is PortSide.LEFT and src_left:
            continue
        row = tgt_sec.grid_row
        if row <= 0:
            continue
        per_gap[(row - 1, row)][edge.target].add(edge.line_id)
    return _widest_lines_per_gap(per_gap)


def _section_precedes(graph: MetroGraph, a: Section, b: Section) -> bool:
    """Whether section *a* sits to the left of *b* along the flow axis.

    Grid column orders sections in different cells; two sections packed into one
    cell (:attr:`MetroGraph.cell_packs`) share a column, so their directive order
    (earlier = left) breaks the tie.
    """
    if a.grid_col != b.grid_col:
        return a.grid_col < b.grid_col
    members = graph.cell_packs.get((a.grid_col, a.grid_row))
    if members and a.id in members and b.id in members:
        return members.index(a.id) < members.index(b.id)
    return False


def _bundles_in_gap(
    graph: MetroGraph,
    col_assign: dict[str, int],
    col_a: int,
    col_b: int,
) -> list[int]:
    """Return ``[n_lines, ...]`` for every distinct bundle traversing the gap.

    Each inter-section edge contributes one or two vertical channels:
    - A bypass edge (``|tgt_col - src_col| > 1``) contributes a gap1
      channel in the gap immediately right of its source column, and a
      gap2 channel in the gap immediately left of its target column.
    - An L-shape edge between adjacent columns contributes a single
      channel in the inter-column gap.

    All source-side (gap1) channels in a gap occupy the same x position
    (just right of the source column) and coalesce into one concentric
    bundle, regardless of how far each edge ultimately travels; likewise
    all target-side (gap2) channels coalesce just left of the target
    column.  A line is therefore counted once per side+direction even
    when it fans from one source to several target columns (e.g.
    differentialabundance fans the same lines to an adjacent section and
    a bypass target -- one down-bundle, not one per target).  Keying by
    ``(src_col, tgt_col)`` would split that single physical bundle in two
    and over-widen the gap.

    The returned list contains one entry per distinct bundle; its value
    is the number of distinct lines in that bundle.
    """
    junction_ids = graph.junction_ids
    bundles: dict[tuple[str, int], set[str]] = defaultdict(set)

    lo, hi = min(col_a, col_b), max(col_a, col_b)

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
        src_col = _station_column(graph, src, col_assign, junction_ids)
        tgt_col = _station_column(graph, tgt, col_assign, junction_ids)
        if src_col is None or tgt_col is None:
            continue
        if src_col == tgt_col:
            continue

        edge_lo = min(src_col, tgt_col)
        edge_hi = max(src_col, tgt_col)
        h_dir = 1 if tgt_col > src_col else -1
        # A gap hosts at most two concentric bundles: the source-side
        # (gap1, "D") channel just right of its lower column and the
        # target-side (gap2, "U") channel just left of its upper column.
        # Key by side + horizontal direction only so every edge whose
        # channel lands on that side coalesces into one bundle.
        if edge_hi - edge_lo == 1:
            if lo == edge_lo and hi == edge_hi:
                bundles[("D", h_dir)].add(edge.line_id)
        else:
            if lo == edge_lo and hi == edge_lo + 1:
                bundles[("D", h_dir)].add(edge.line_id)
            if lo == edge_hi - 1 and hi == edge_hi:
                bundles[("U", h_dir)].add(edge.line_id)

    return [len(lines) for lines in bundles.values() if lines]


def _min_gap_for_bundles(
    bundle_line_counts: list[int],
    edge_clearance: float = EDGE_TO_BUNDLE_CLEARANCE,
    inter_bundle: float = BUNDLE_TO_BUNDLE_CLEARANCE,
    offset_step: float = OFFSET_STEP,
) -> float:
    """Required gap width for a list of bundles in one inter-section gap.

    Implements the principled formula::

        gap >= A + Σ bundle_widths + (count - 1) * B + A

    where each ``bundle_width = (n_i - 1) * OFFSET_STEP`` is the visual
    span of the ``n_i`` parallel lines in bundle *i*.  ``A`` is the
    section-edge-to-bundle clearance (:data:`EDGE_TO_BUNDLE_CLEARANCE`)
    and ``B`` is the inter-bundle clearance
    (:data:`BUNDLE_TO_BUNDLE_CLEARANCE`).

    For an empty list, returns 0 (no gap requirement from routing; the
    caller's static :data:`MIN_INTER_SECTION_GAP` floor still applies).
    """
    if not bundle_line_counts:
        return 0.0
    widths = sum(max(0, n - 1) * offset_step for n in bundle_line_counts)
    count = len(bundle_line_counts)
    return 2 * edge_clearance + widths + (count - 1) * inter_bundle


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


def _column_pair_min_gap(
    graph: MetroGraph,
    col_assign: dict[str, int],
    col: int,
    min_gap: float = MIN_INTER_SECTION_GAP,
) -> tuple[float, list[int]]:
    """Minimum inter-section gap needed between columns ``col`` and ``col+1``.

    The effective minimum is the larger of the static floor ``min_gap``, the
    width the traversing routing bundles occupy, and any merge-junction
    minimum.  Also returns the per-bundle line counts that drove the bundle
    width so callers can report why a gap was widened.

    Principled bundle width: A + Σ bundle_widths + (count-1)*B + A.  For a
    single bundle of one line this collapses to 2*A; with the default A=16 px
    that's 32 px, smaller than the static MIN_INTER_SECTION_GAP=40 px so
    single-line gaps are NOT widened past the standard layout column gap.
    Multi-line or multi-bundle corridors deterministically claim only the
    horizontal space their visual width actually occupies.
    """
    bundles = _bundles_in_gap(graph, col_assign, col, col + 1)
    offset_step = resolve_offset_step(graph.track_gap)
    bundle_min = _min_gap_for_bundles(bundles, offset_step=offset_step)
    effective_min = max(min_gap, bundle_min)
    if _has_merge_routing_in_gap(graph, col_assign, col, col + 1):
        effective_min = max(effective_min, MERGE_GAP_MIN)
    return effective_min, bundles


def _apply_column_gap_deficits(
    graph: MetroGraph,
    col_assign: dict[str, int],
    min_col: int,
    max_col: int,
    *,
    min_gap: float,
    right_extent: Callable[[Section], float],
    left_extent: Callable[[Section], float],
    shift: Callable[[Section, float], None],
    requested_gap: float | None = None,
) -> None:
    """Widen each adjacent column pair to its minimum inter-section gap.

    For every column ``col`` in ``[min_col, max_col)``, take the tightest gap
    among row-overlapping section pairs (measured from ``right_extent`` of a
    left-column section to ``left_extent`` of a right-column section) and, if
    it falls short of the pair's :func:`_column_pair_min_gap`, ``shift`` all
    sections in columns ``> col`` rightward by the deficit.

    The extent readers and ``shift`` abstract over the coordinate space:
    ``offset_x``-relative before Stage 2.1, absolute global coordinates after.
    ``requested_gap`` (offset-space callers only) triggers a ``UserWarning``
    whenever a bundle forces the gap wider than the user asked for.
    """
    col_sections: dict[int, list[Section]] = defaultdict(list)
    for sid, section in graph.sections.items():
        col_sections[col_assign.get(sid, 0)].append(section)

    for col in range(min_col, max_col):
        left_secs = col_sections.get(col, [])
        right_secs = col_sections.get(col + 1, [])
        if not left_secs or not right_secs:
            continue

        effective_min, bundles = _column_pair_min_gap(graph, col_assign, col, min_gap)

        worst_gap: float | None = None
        for ls in left_secs:
            for rs in right_secs:
                if not _rows_overlap(ls, rs):
                    continue
                gap = left_extent(rs) - right_extent(ls)
                if worst_gap is None or gap < worst_gap:
                    worst_gap = gap

        if worst_gap is None or worst_gap >= effective_min:
            continue

        if requested_gap is not None and effective_min > requested_gap:
            n_bundles = len(bundles)
            total_lines = sum(bundles)
            warnings.warn(
                f"Section gap between columns {col} and {col + 1} "
                f"widened from {requested_gap:.0f}px to {effective_min:.0f}px "
                f"to accommodate {n_bundles} bundle(s) / "
                f"{total_lines} routing line(s)",
                stacklevel=3,
            )

        deficit = effective_min - worst_gap
        for shift_col in range(col + 1, max_col + 1):
            for s in col_sections.get(shift_col, []):
                shift(s, deficit)


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

    Operates in ``offset_x`` space (Stage 1.3, before global coordinates
    settle), so extents include ``offset_x`` and the shift adds to it.
    """
    if max_col <= min_col:
        return

    def _shift(s: Section, dx: float) -> None:
        s.offset_x += dx

    _apply_column_gap_deficits(
        graph,
        col_assign,
        min_col,
        max_col,
        min_gap=min_gap,
        right_extent=lambda s: s.offset_x + s.bbox_x + s.bbox_w,
        left_extent=lambda s: s.offset_x + s.bbox_x,
        shift=_shift,
        requested_gap=requested_gap,
    )


def reenforce_column_gaps(graph: MetroGraph) -> None:
    """Restore inter-column section gaps after the Stage 3.3 perp-entry shift.

    ``_shift_lr_perp_entry_stations`` can grow a section's bbox into its column
    neighbour, an overlap the Stage 1.3 :func:`_enforce_min_column_gaps` (which
    runs before the shift) never re-checks.  Re-run the same column-gap logic on
    the settled global coordinates: translate each column's sections rightward
    by any remaining gap deficit, keeping the routing gap every adjacent column
    needs.  A no-op where the shift left the gaps intact, so sections that never
    grew are untouched.

    Unlike the Stage 1.3 pass this operates in global coordinates (past Stage
    2.1, ``bbox_x`` and station/port ``x`` are absolute and ``offset_x`` is
    inert), so it shifts the section contents rather than ``offset_x``.
    """
    if not graph.sections:
        return

    col_assign = {sid: s.grid_col for sid, s in graph.sections.items()}
    min_col = min(col_assign.values())
    max_col = max(
        col + graph.sections[sid].grid_col_span - 1 for sid, col in col_assign.items()
    )

    _apply_column_gap_deficits(
        graph,
        col_assign,
        min_col,
        max_col,
        min_gap=MIN_INTER_SECTION_GAP,
        right_extent=lambda s: s.bbox_x + s.bbox_w,
        left_extent=lambda s: s.bbox_x,
        shift=lambda s, dx: shift_section(graph, s, dx=dx),
    )


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
    wrap_min_by_pair: dict[tuple[int, int], float] | None = None,
) -> None:
    """Shift rows downward so section headers don't overlap the section above.

    Section headers (number circle + label) protrude above the section
    bbox.  The visual gap is measured from the upper section's bbox
    bottom to the lower section's header top (bbox_y - protrusion).
    Only checks section pairs that share horizontal extent.

    ``wrap_min_by_pair`` (see :func:`_inter_row_routing_minimums`) adds a
    second, routing-aware constraint: an adjacent-row gap that hosts an
    inter-row horizontal run (an entry-wrap bundle or a bottommost-row
    merge-trunk channel) is widened so the run keeps full clearance from
    both bounding sections.  That requirement is bbox-to-bbox (no header
    protrusion) and spans the row envelope, so it is checked against the
    tightest envelope edges rather than only horizontally-overlapping pairs.
    """
    if not row_assign:
        return
    wrap_min_by_pair = wrap_min_by_pair or {}

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

        # Header-clearance deficit (horizontally-overlapping pairs only).
        header_deficit = 0.0
        if worst_gap is not None and worst_gap < min_gap:
            header_deficit = min_gap - worst_gap

        # Routing deficit: keep an inter-row wrap bundle clear of both
        # bounding sections.  Measured bbox-to-bbox across the whole row
        # envelope (matching ``row_bottom_edge`` / ``row_top_edge``).
        wrap_deficit = 0.0
        wrap_min = wrap_min_by_pair.get((row, row + 1))
        if wrap_min is not None:
            envelope_bottom = max(s.offset_y + s.bbox_y + s.bbox_h for s in upper_secs)
            envelope_top = min(s.offset_y + s.bbox_y for s in lower_secs)
            envelope_gap = envelope_top - envelope_bottom
            if envelope_gap < wrap_min:
                wrap_deficit = wrap_min - envelope_gap

        deficit = max(header_deficit, wrap_deficit)
        if deficit <= 0:
            continue

        # Warn if we're overriding the user's requested gap, naming the
        # constraint that actually drove the widening.
        if wrap_deficit > header_deficit:
            reason = "to fit an inter-row routing bundle"
            # wrap_deficit only exceeds 0 when wrap_min was non-None.
            assert wrap_min is not None
            required_bbox_gap = wrap_min
        else:
            reason = "to clear section headers"
            required_bbox_gap = min_gap + header_protrusion
        if requested_gap is not None and required_bbox_gap > requested_gap:
            warnings.warn(
                f"Section gap between rows {row} and {row + 1} "
                f"widened to {required_bbox_gap:.0f}px {reason} "
                f"(requested {requested_gap:.0f}px)",
                stacklevel=2,
            )

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

    # Vertical-flow (TB/BT) sections: clamp LEFT/RIGHT ports to the station
    # band rather than the (often tall) section edges.  Exit ports sit just
    # beyond the trailing internal station and entry ports just before the
    # leading one -- in the section's flow sense -- so lines flow straight
    # into the station fan instead of running along a trunk near the box edge
    # and jogging back to the pills.  The trailing/leading ends follow the
    # flow sign: a downward (TB) flow trails at the bottom, an upward (BT) one
    # at the top.
    if lanes_run_along_x(section.direction):
        flow = AxisFrame.flow_sign(section.direction)
        entry_set = set(section.entry_ports)
        exit_set = set(section.exit_ports)
        internal_ids = set(section.station_ids) - entry_set - exit_set
        internal_ys = [
            graph.stations[sid].y
            for sid in internal_ids
            if sid in graph.stations and not graph.stations[sid].is_port
        ]
        if internal_ys:
            trailing_y = max(internal_ys) if flow > 0 else min(internal_ys)
            leading_y = min(internal_ys) if flow > 0 else max(internal_ys)
        else:
            box_top = section.bbox_y
            box_bot = section.bbox_y + section.bbox_h
            trailing_y = box_bot if flow > 0 else box_top
            leading_y = box_top if flow > 0 else box_bot
        # A bypass V peeling around the trailing station seats on its row, so
        # its run-out flat is exactly the trailing-station-to-exit gap.  That
        # gap must reach a full station flat, or the V collapses onto the exit
        # corner's curve apex instead of sitting on a visible flat run.
        exit_gap = MIN_PORT_STATION_GAP
        if any(
            is_bypass_v(sid)
            and abs(graph.stations[sid].y - trailing_y) <= SAME_COORD_TOLERANCE
            for sid in internal_ids
            if sid in graph.stations
        ):
            exit_gap = max(exit_gap, MIN_STATION_FLAT_LENGTH)
        exit_target_y = trailing_y + flow * exit_gap
        entry_target_y = leading_y - flow * MIN_PORT_STATION_GAP
        for pid in exit_set:
            port = graph.ports.get(pid)
            if port and port.side in (PortSide.LEFT, PortSide.RIGHT):
                station = graph.stations.get(pid)
                if station:
                    station.y = exit_target_y
                port.y = exit_target_y
        for pid in entry_set:
            port = graph.ports.get(pid)
            if port and port.side in (PortSide.LEFT, PortSide.RIGHT):
                station = graph.stations.get(pid)
                if station:
                    station.y = entry_target_y
                port.y = entry_target_y


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
        # An entry that does not arrive horizontally at its consumer's Y -- a
        # TOP/BOTTOM port (vertical drop) or a LEFT/RIGHT port bending into a
        # TB/BT trunk -- leaves the exit's own trunk Y to govern the lead-in;
        # anchoring to the consumer's Y would pull the exit off its trunk.
        if ep.side in (PortSide.TOP, PortSide.BOTTOM) or (
            not lanes_run_along_y(ds.direction)
            and ep.side in (PortSide.LEFT, PortSide.RIGHT)
        ):
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
        if edge.target in internal_ids and not is_bypass_v(edge.target):
            vals.append(getattr(graph.stations[edge.target], axis))
    for edge in graph.edges_to(port_id):
        if edge.source in internal_ids and not is_bypass_v(edge.source):
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
