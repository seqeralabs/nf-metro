"""Layout coordinator: combines layer assignment, ordering, and coordinate mapping.

Section-first layout: sections are laid out independently, then placed on a meta-graph.
"""

from __future__ import annotations

__all__ = ["PhaseInvariantError", "compute_layout"]

import math
from collections import Counter, defaultdict

from nf_metro.layout.constants import (
    CURVE_RADIUS,
    DIAGONAL_RUN,
    ENTRY_SHIFT_LR,
    ENTRY_SHIFT_TB,
    ENTRY_SHIFT_TB_CROSS,
    EXIT_GAP_MULTIPLIER,
    FONT_HEIGHT,
    GUARD_TOLERANCE,
    ICON_INTER_GAP,
    JUNCTION_MARGIN,
    LABEL_BBOX_MARGIN,
    LABEL_LINE_HEIGHT,
    LABEL_MARGIN,
    LABEL_OFFSET,
    LABEL_PAD,
    LINE_GAP,
    MAX_PORT_ALIGN_BBOX_EXPANSION_FRAC,
    MIN_PORT_STATION_GAP,
    OFFSET_STEP,
    ROW_GAP,
    SECTION_GAP,
    SECTION_X_GAP,
    SECTION_X_PADDING,
    SECTION_Y_GAP,
    SECTION_Y_PADDING,
    STATION_ELBOW_TOLERANCE,
    STATION_RADIUS_APPROX,
    TB_LINE_Y_OFFSET,
    TERMINUS_ICON_CLEARANCE,
    TERMINUS_WIDTH,
    X_OFFSET,
    X_SPACING,
    Y_OFFSET,
    Y_SPACING,
)
from nf_metro.layout.labels import label_text_width
from nf_metro.layout.layers import assign_layers
from nf_metro.layout.ordering import assign_tracks
from nf_metro.parser.model import Edge, MetroGraph, PortSide, Section, Station

# ---------------------------------------------------------------------------
# Phase-boundary guards
# ---------------------------------------------------------------------------

_VALIDATE_DEFAULT = False
"""Set to True to enable phase-boundary invariant checks.

Controlled by the ``validate`` parameter on ``compute_layout``.
Tests pass ``validate=True`` to catch cross-phase corruption that would
otherwise only surface as subtle visual defects.
"""


class PhaseInvariantError(Exception):
    """Raised when a layout phase produces invalid intermediate state."""


def _guard_coordinates_finite(graph: MetroGraph, phase: str) -> None:
    """After Phase 4+: all laid-out stations must have finite coordinates."""
    junction_ids = set(graph.junctions)
    for sid, st in graph.stations.items():
        if st.section_id and not st.is_port and sid not in junction_ids:
            if math.isnan(st.x) or math.isnan(st.y):
                raise PhaseInvariantError(
                    f"{phase}: station {sid!r} has NaN coordinates (x={st.x}, y={st.y})"
                )
            if math.isinf(st.x) or math.isinf(st.y):
                raise PhaseInvariantError(
                    f"{phase}: station {sid!r} has infinite coordinates "
                    f"(x={st.x}, y={st.y})"
                )


def _guard_stations_in_sections(graph: MetroGraph, phase: str) -> None:
    """After Phase 4+: internal stations must be within their section bbox."""
    junction_ids = set(graph.junctions)
    for sid, st in graph.stations.items():
        sec = graph.sections.get(st.section_id or "")
        if not sec or st.is_port or sid in junction_ids or sec.bbox_w == 0:
            continue
        if not (
            sec.bbox_x <= st.x <= sec.bbox_x + sec.bbox_w
            and sec.bbox_y <= st.y <= sec.bbox_y + sec.bbox_h
        ):
            raise PhaseInvariantError(
                f"{phase}: station {sid!r} at ({st.x:.1f}, {st.y:.1f}) "
                f"outside section {st.section_id!r} bbox "
                f"({sec.bbox_x:.1f}, {sec.bbox_y:.1f}, "
                f"w={sec.bbox_w:.1f}, h={sec.bbox_h:.1f})"
            )


def _guard_ports_on_boundaries(graph: MetroGraph, phase: str) -> None:
    """After Phase 5+: ports must sit on their section's bounding box edge."""
    tolerance = GUARD_TOLERANCE
    for pid, port in graph.ports.items():
        st = graph.stations.get(pid)
        sec = graph.sections.get(st.section_id or "") if st else None
        if not st or not sec or sec.bbox_w == 0:
            continue
        on_left = abs(st.x - sec.bbox_x) <= tolerance
        on_right = abs(st.x - (sec.bbox_x + sec.bbox_w)) <= tolerance
        on_top = abs(st.y - sec.bbox_y) <= tolerance
        on_bottom = abs(st.y - (sec.bbox_y + sec.bbox_h)) <= tolerance
        if not (on_left or on_right or on_top or on_bottom):
            raise PhaseInvariantError(
                f"{phase}: port {pid!r} at ({st.x:.1f}, {st.y:.1f}) "
                f"not on any edge of section {st.section_id!r} bbox "
                f"({sec.bbox_x:.1f}, {sec.bbox_y:.1f}, "
                f"w={sec.bbox_w:.1f}, h={sec.bbox_h:.1f})"
            )


def _guard_section_bboxes_positive(graph: MetroGraph, phase: str) -> None:
    """After Phase 2+: non-empty sections must have positive-size bboxes."""
    for sid, sec in graph.sections.items():
        if not sec.station_ids:
            continue
        if sec.bbox_w < 0 or sec.bbox_h < 0:
            raise PhaseInvariantError(
                f"{phase}: section {sid!r} has negative bbox "
                f"(w={sec.bbox_w:.1f}, h={sec.bbox_h:.1f})"
            )


def compute_layout(
    graph: MetroGraph,
    x_spacing: float = X_SPACING,
    y_spacing: float = Y_SPACING,
    x_offset: float = X_OFFSET,
    y_offset: float = Y_OFFSET,
    row_gap: float = ROW_GAP,
    section_gap: float = SECTION_GAP,
    section_x_padding: float = SECTION_X_PADDING,
    section_y_padding: float = SECTION_Y_PADDING,
    section_x_gap: float = SECTION_X_GAP,
    section_y_gap: float = SECTION_Y_GAP,
    validate: bool = _VALIDATE_DEFAULT,
) -> None:
    """Compute layout positions for all stations in the graph.

    When *validate* is True, phase-boundary invariant checks run after
    key phases.  Violations raise ``PhaseInvariantError`` instead of
    silently producing broken layouts.
    """
    # Optionally reorder lines by section span before layout.
    # Must happen here (on the full graph) before section subgraphs are
    # built, since subgraphs share graph.lines via reference.
    if graph.line_order == "span" and graph.lines:
        from nf_metro.layout.ordering import _reorder_by_span

        new_order = _reorder_by_span(graph, list(graph.lines.keys()))
        graph.lines = {lid: graph.lines[lid] for lid in new_order}

    if not graph.sections:
        _compute_flat_layout(
            graph,
            x_spacing=x_spacing,
            y_spacing=y_spacing,
            x_offset=x_offset,
            y_offset=y_offset,
        )
        return

    _compute_section_layout(
        graph,
        x_spacing=x_spacing,
        y_spacing=y_spacing,
        x_offset=x_offset,
        y_offset=y_offset,
        section_x_padding=section_x_padding,
        section_y_padding=section_y_padding,
        section_x_gap=section_x_gap,
        section_y_gap=section_y_gap,
        validate=validate,
    )


def _compute_flat_layout(
    graph: MetroGraph,
    x_spacing: float = X_SPACING,
    y_spacing: float = Y_SPACING,
    x_offset: float = X_OFFSET,
    y_offset: float = Y_OFFSET,
) -> None:
    """Flat layout for sectionless pipelines.

    Runs layer/track assignment directly on the full graph and maps
    to coordinates without section boxes or port routing.
    """
    layers = assign_layers(graph)
    tracks = assign_tracks(graph, layers)

    if not layers:
        return

    # When tracks is empty (e.g. no named lines), default all to track 0.
    if not tracks:
        tracks = {sid: 0 for sid in layers}

    unique_tracks = sorted(set(tracks.values()))
    track_rank = {t: i for i, t in enumerate(unique_tracks)}

    layer_extra = _compute_fork_join_gaps(graph, layers, tracks, x_spacing)

    for sid, station in graph.stations.items():
        station.layer = layers.get(sid, 0)
        station.track = tracks.get(sid, 0)
        station.x = (
            x_offset + station.layer * x_spacing + layer_extra.get(station.layer, 0)
        )
        station.y = y_offset + track_rank[station.track] * y_spacing


def _compute_section_layout(
    graph: MetroGraph,
    x_spacing: float = X_SPACING,
    y_spacing: float = Y_SPACING,
    x_offset: float = X_OFFSET,
    y_offset: float = Y_OFFSET,
    section_x_padding: float = SECTION_X_PADDING,
    section_y_padding: float = SECTION_Y_PADDING,
    section_x_gap: float = SECTION_X_GAP,
    section_y_gap: float = SECTION_Y_GAP,
    validate: bool = False,
) -> None:
    """Section-first layout pipeline.

    Phase 1: Parse & partition (already done by parser)
    Phase 2: Internal section layout (per section, real stations only)
    Phase 3: Section placement (meta-graph)
    Phase 4: Global coordinate mapping

    Pass A - Port initialisation & section geometry:
      Phase 5:  Port positioning on section boundaries
      Phase 6:  Align entry ports to incoming source Y/X
      Phase 7:  Shift LR/RL perp-entry internal stations (X only)
      Phase 8:  Align fold-section exit ports (may push target sections)
      Phase 9:  Top-align sections within each grid row

    Pass B - Downstream alignment (single pass):
      Phase 10: Align exit-entry port pairs to downstream stations
      Phase 11: Space ports from terminus stations

    Pass C - Junction positioning (single pass):
      Phase 12: Position junction stations in inter-section gaps
      Phase 13: Lift off-track stations above section's top track
      Phase 13a: Re-align bbox tops within each row (bbox-only)
    """
    from nf_metro.layout.section_placement import place_sections, position_ports

    # Phase 2: Lay out each section independently (real stations only, no ports)
    section_subgraphs: dict[str, MetroGraph] = {}
    for sec_id, section in graph.sections.items():
        sub = _layout_single_section(
            graph, section, x_spacing, y_spacing, section_x_padding, section_y_padding
        )
        if sub is not None:
            section_subgraphs[sec_id] = sub

    if validate:
        _guard_section_bboxes_positive(graph, "after Phase 2")

    # Phase 2.5: Align Y grids across same-row, same-direction sections
    _align_row_y_grids(graph, section_subgraphs, y_spacing, section_y_padding)

    # Phase 3: Place sections on the canvas
    place_sections(graph, section_x_gap, section_y_gap)

    # Phase 3a: Renumber sections by visual reading order (row, col)
    _renumber_sections_by_grid(graph)

    # Phase 3b: Adapt x/y_offset for left/top overshoot.
    # Section bboxes extend left of the local origin by at least
    # section_x_padding; x_offset normally absorbs this with margin to
    # spare (standard margin = x_offset - section_x_padding).  When
    # terminus-icon clearance expands bbox_x far enough that
    # offset_x + bbox_x + x_offset < 0, content clips off the canvas.
    # Increase x_offset to restore the standard margin and let the canvas
    # grow on the right (via auto_width = max_x + CANVAS_PADDING in
    # render).  Same logic for y_offset.
    local_lefts = [
        section.offset_x + section.bbox_x
        for section in graph.sections.values()
        if section.bbox_w > 0
    ]
    if local_lefts:
        min_local_left = min(local_lefts)
        global_left = min_local_left + x_offset
        if global_left < 0:
            standard_margin = x_offset - section_x_padding
            x_offset += standard_margin - global_left

    local_tops = [
        section.offset_y + section.bbox_y
        for section in graph.sections.values()
        if section.bbox_h > 0
    ]
    if local_tops:
        min_local_top = min(local_tops)
        global_top = min_local_top + y_offset
        if global_top < 0:
            standard_margin = y_offset - section_y_padding
            y_offset += standard_margin - global_top

    # Phase 4: Translate local coords to global coords (real stations)
    for sec_id, section in graph.sections.items():
        sub = section_subgraphs.get(sec_id)
        if not sub:
            continue

        for sid, local_station in sub.stations.items():
            if sid in graph.stations:
                graph.stations[sid].layer = local_station.layer
                graph.stations[sid].track = local_station.track
                graph.stations[sid].x = local_station.x + section.offset_x + x_offset
                graph.stations[sid].y = local_station.y + section.offset_y + y_offset

        # Update section bbox to global coords
        section.bbox_x += section.offset_x + x_offset
        section.bbox_y += section.offset_y + y_offset

    if validate:
        _guard_coordinates_finite(graph, "after Phase 4")
        _guard_stations_in_sections(graph, "after Phase 4")
        _guard_section_bboxes_positive(graph, "after Phase 4")

    # ---- Pass A: Port initialisation & section geometry adjustments ------
    # Position ports on bbox edges, align entry ports, shift internal
    # stations for perp entries, align fold exits, then top-align.
    # Top-align runs last so it corrects any bbox shifts from fold-exit
    # alignment.

    # Phase 5: Position ports on section boundaries (after bbox is in global coords)
    for sec_id, section in graph.sections.items():
        position_ports(section, graph)

    if validate:
        _guard_ports_on_boundaries(graph, "after Phase 5")

    # Phase 6: Align LEFT/RIGHT entry ports with their incoming
    # connection's Y so inter-section horizontal runs are straight.
    # Uses _resolve_source_xy() to derive junction coordinates
    # on-the-fly, removing the dependency on pre-positioned junctions.
    _align_entry_ports(graph)

    # Phase 7: Shift internal stations in LR/RL sections with
    # perpendicular (TOP/BOTTOM) entry away from the port.  Needs the
    # aligned port X from Phase 6; only moves internal station X, not
    # ports or bboxes.
    _shift_lr_perp_entry_stations(graph, x_spacing)

    # Phase 8: Align LEFT/RIGHT exit ports on row-spanning (fold)
    # sections with their target's Y so the exit is at the return row.
    # May push target sections down (via _resolve_tb_exit_y), which
    # top-align in the next step corrects.
    _align_exit_ports(graph)

    # Phase 9: Top-align sections within each grid row.
    # Runs after fold-exit alignment so it corrects any bbox_y shifts
    # from Phase 8's target-section push.  Same-row port pairs shift
    # by the same delta, preserving entry-port alignment.
    _top_align_row_sections(graph)

    if validate:
        _guard_ports_on_boundaries(graph, "after top-align")

    # ---- Pass B: Downstream alignment (single pass) --------------------
    # Downstream alignment and terminus spacing run on finalised section
    # geometry (after top-align), so they don't need re-running.

    # Phase 10: For non-fold LR/RL sections, pull exit-entry port pairs
    # toward the downstream section's stations so lines flow directly.
    _align_ports_to_downstream(graph)

    # Phase 10b: When a port-connected station is the sole occupant of its
    # layer, snap it to the port Y so the connection is horizontal.
    _snap_sole_layer_stations_to_ports(graph)

    # Phase 10c: For grid-group sections (where 10b is skipped), snap
    # entry ports to the Y of their first connected internal station.
    # This produces a straight horizontal port-to-station connection
    # instead of a diagonal from the upstream junction Y.
    _snap_grid_group_entry_ports(graph)

    # Phase 10d: Mirror of 10c for exit ports.  Move exit ports of
    # grid-group sections to the Y of the downstream entry port (which
    # 10c already snapped to a grid station).  This eliminates detours
    # where lines leave at the section midpoint then route back.
    _snap_grid_group_exit_ports(graph)

    # Phase 11: Ensure ports maintain at least y_spacing from terminus
    # stations in their section so file icons don't overlap routed lines.
    _space_ports_from_termini(graph, y_spacing)

    # Phase 11b: Recompute bboxes for grid-aligned sections.  Earlier
    # phases (6, 8, 11) may have expanded bboxes for temporary port
    # positions that were later corrected (e.g. Phase 10 pulls ports
    # back toward downstream stations).  Recompute with symmetric
    # padding around the final non-port station range.
    _recompute_grid_group_bboxes(graph)

    # Phase 11c: Re-run top-align after Phase 11 may have shifted
    # individual section bbox_y values (via _expand_bbox_for_y) so
    # bbox tops within each row stay flush after port-terminus spacing.
    _top_align_row_sections(graph)

    # Phase 11ca: Align trunk Ys across same-row sections.  Shifts
    # content downward in shallower sections so the inter-section bundle
    # passes through at a single Y per row.  Bbox tops are preserved.
    _align_row_trunk_ys(graph)

    # Phase 11d: When --center-ports is on, redistribute fan-out siblings
    # of a section's trunk junction symmetrically around the trunk Y.
    # Scoped to fan-out side branches only: linear chains, fan-in
    # structures, and file inputs are left in place.
    _redistribute_fanout_siblings(graph, y_spacing)

    # ---- Pass C: Junction positioning (single pass) --------------------
    # All port positions are now final; position junctions once.

    # Phase 12: Position junction stations in the inter-section gap.
    _position_junctions(graph)

    # Phase 13: Lift off_track stations above their section's top track.
    # Runs last so it operates on finalised station Ys and bboxes.
    _lift_off_track_stations(graph, y_spacing, section_y_padding)

    # Phase 13a: Re-align bbox tops within each grid row after off-track
    # lifting expanded some sections upward.  Unlike Phase 9/11c which
    # shifts stations with the bbox, this only grows the bbox upward so
    # the empty input-band space lines up across the row.  Station Ys
    # in unlifted sections are preserved.
    _top_align_row_bboxes_only(graph)

    # Phase 13b: Compact row-mate sections so content sits just inside
    # the bbox top edge.  Shifts an entire row's column group up by the
    # smallest above-content slack, preserving trunk alignment.  Bbox
    # heights shrink correspondingly so the empty top space disappears.
    _compact_row_content_to_bbox_top(graph, section_y_padding, y_spacing)


    if validate:
        _guard_coordinates_finite(graph, "after Phase 12 (final)")
        _guard_section_bboxes_positive(graph, "after Phase 12 (final)")
        _guard_stations_in_sections(graph, "after Phase 12 (final)")
        _guard_ports_on_boundaries(graph, "after Phase 12 (final)")


def _renumber_sections_by_grid(graph: MetroGraph) -> None:
    """Renumber sections by visual reading order.

    Groups sections into flow sweeps separated by fold boundaries:
    each left-to-right (or right-to-left) run is one sweep, with
    TB fold sections belonging to the sweep they terminate.  Within
    each sweep, sections are numbered by (grid_col, grid_row) so
    columns go left-to-right and stacked sections go top-to-bottom.
    All numbers in sweep N+1 are greater than those in sweep N.
    """
    from collections import deque

    import networkx as nx

    dag = nx.DiGraph()
    for sid in graph.sections:
        dag.add_node(sid)
    if graph.section_dag:
        for src, tgt in graph.section_dag.section_edges:
            if src in graph.sections and tgt in graph.sections:
                dag.add_edge(src, tgt)

    secs = graph.sections

    def _is_direction_change(src: str, tgt: str) -> bool:
        """True when flow direction reverses between two sections."""
        sd, td = secs[src].direction, secs[tgt].direction
        # TB->LR/RL: only counts if the TB's predecessors flowed
        # the opposite way (i.e. TB is a fold boundary).
        if sd == "TB" and td in ("LR", "RL"):
            for pred in dag.predecessors(src):
                pd = secs[pred].direction
                if pd in ("LR", "RL") and pd != td:
                    return True
            return False
        if sd in ("LR", "RL") and td in ("LR", "RL") and sd != td:
            return True
        return False

    sweep: dict[str, int] = {}
    roots = [n for n in dag.nodes() if dag.in_degree(n) == 0]
    q: deque[str] = deque()
    for r in roots:
        sweep[r] = 0
        q.append(r)

    while q:
        node = q.popleft()
        for succ in dag.successors(node):
            new_depth = sweep[node]
            if _is_direction_change(node, succ):
                new_depth = sweep[node] + 1
            if succ not in sweep or new_depth < sweep[succ]:
                sweep[succ] = new_depth
                q.append(succ)

    for sid in graph.sections:
        if sid not in sweep:
            sweep[sid] = 0

    # Disconnected components: number each flow fully before the next,
    # ordered by the root's grid_row so top flows come first.
    comp_idx: dict[str, int] = {}
    for rank, comp in enumerate(
        sorted(
            nx.weakly_connected_components(dag),
            key=lambda c: min(graph.sections[sid].grid_row for sid in c),
        )
    ):
        for sid in comp:
            comp_idx[sid] = rank

    # Determine flow direction for each sweep: RL sweeps number
    # columns right-to-left (descending grid_col) to match the flow.
    sweep_is_rl: dict[int, bool] = {}
    for sid, s in graph.sections.items():
        sw = sweep[sid]
        if sw not in sweep_is_rl and s.direction == "RL":
            sweep_is_rl[sw] = True
        elif sw not in sweep_is_rl and s.direction == "LR":
            sweep_is_rl[sw] = False

    def _sort_key(s):
        sw = sweep[s.id]
        col = -s.grid_col if sweep_is_rl.get(sw, False) else s.grid_col
        return (comp_idx.get(s.id, 0), sw, col, s.grid_row)

    sorted_sections = sorted(graph.sections.values(), key=_sort_key)
    for i, section in enumerate(sorted_sections, start=1):
        section.number = i


def _grid_group_section_ids(graph: MetroGraph) -> set[str]:
    """Return the set of section IDs that participated in grid alignment."""
    grid_info = graph._row_y_grid_info
    result: set[str] = set()
    for info in grid_info.values():
        result.update(info["section_ids"])
    return result


def _classify_multi_station_ys(
    sub: MetroGraph,
) -> tuple[dict[int, list[float]], set[float]]:
    """Classify Y values by layer and identify multi-station-layer Ys.

    Returns (layer_stations, multi_layer_ys) where layer_stations maps
    layer -> list of Y values, and multi_layer_ys is the set of Y values
    that appear in layers with >1 station.
    """
    layer_stations: dict[int, list[float]] = defaultdict(list)
    for s in sub.stations.values():
        layer_stations[s.layer].append(s.y)
    multi_layer_ys: set[float] = set()
    for ys_at_layer in layer_stations.values():
        if len(ys_at_layer) > 1:
            multi_layer_ys.update(ys_at_layer)
    return layer_stations, multi_layer_ys


def _max_stations_per_layer(sub: MetroGraph) -> int:
    """Return the maximum number of distinct Y positions at any single layer."""
    layer_ys: dict[int, set[float]] = defaultdict(set)
    for s in sub.stations.values():
        layer_ys[s.layer].add(s.y)
    return max((len(ys) for ys in layer_ys.values()), default=1)


def _align_row_y_grids(
    graph: MetroGraph,
    section_subgraphs: dict[str, MetroGraph],
    y_spacing: float,
    section_y_padding: float,
) -> None:
    """Snap station Y coordinates to a shared grid within each row.

    For same-direction sections in the same grid row, determines the
    maximum stations-per-layer across all sections and builds a shared
    Y grid with that many slots at *y_spacing* pitch.

    Constraints preserved:

    1. **Isolated stations** (sole occupant of their layer, with a Y
       value not found at any multi-station layer) keep their original Y.
       This preserves hub-station centering (e.g. bench_hub).
    2. **Bbox dimensions** (bbox_w, bbox_h) are unchanged.  Stations may
       shift within their bbox but the box itself keeps its Phase-2 size.
    3. **y_pad compensation**: a uniform shift of ``max_y_pad - y_pad``
       is applied to every station so that after Phase 9 top-aligns
       bbox_y, the first-station Y matches across sections despite
       differing ``_multiline_label_padding``.

    Stores grid metadata on ``graph._row_y_grid_info`` for the debug
    overlay to render shared Y grid lines.

    Runs between Phase 2 and Phase 3, operating in local coordinates.
    """
    from nf_metro.layout.section_placement import _assign_grid_layout

    # Pre-compute grid rows (not yet set on Section objects at this point)
    section_edges = graph.section_dag.section_edges if graph.section_dag else set()
    _, row_assign = _assign_grid_layout(graph, section_edges)

    # Group sections by (row, direction), skipping TB sections
    groups: dict[tuple[int, str], list[str]] = defaultdict(list)
    for sec_id in section_subgraphs:
        section = graph.sections[sec_id]
        row = row_assign.get(sec_id, -1)
        if row < 0 or section.direction == "TB":
            continue
        groups[(row, section.direction)].append(sec_id)

    # Store grid info for debug overlay
    grid_info: dict[int, dict] = {}

    for (row, _direction), sec_ids in groups.items():
        if len(sec_ids) < 2:
            continue

        # Grid size = max stations stacked at any single layer across
        # all sections in the group.
        grid_slots = 0
        for sec_id in sec_ids:
            sub = section_subgraphs[sec_id]
            grid_slots = max(grid_slots, _max_stations_per_layer(sub))

        if grid_slots <= 1:
            continue

        # Compute max y_pad across group for compensation
        max_y_pad = 0.0
        for sec_id in sec_ids:
            sub = section_subgraphs[sec_id]
            y_pad = section_y_padding + _multiline_label_padding(sub)
            max_y_pad = max(max_y_pad, y_pad)

        # Scale effective y_spacing when stations carry many lines.
        # Per-line offsets spread the rendered line bundle vertically;
        # when the spread + label height exceeds the base y_spacing,
        # labels on adjacent tracks overlap.  We inflate the grid
        # pitch just enough to guarantee clearance.
        #
        # Only count stations at multi-station layers (i.e. Y values
        # in remap_ys).  Isolated hub stations (sole layer occupant,
        # e.g. bench_hub with 6 lines) don't represent inter-track
        # crowding and should not inflate spacing for the entire row.
        #
        max_lines = 0
        for sec_id in sec_ids:
            sub = section_subgraphs[sec_id]
            _, _multi_ys = _classify_multi_station_ys(sub)
            for st in sub.stations.values():
                if not st.is_port and st.y in _multi_ys:
                    max_lines = max(max_lines, len(graph.station_lines(st.id)))
        min_track_gap = (
            (max_lines - 1) * OFFSET_STEP
            + 2 * STATION_RADIUS_APPROX
            + LABEL_OFFSET
            + FONT_HEIGHT
        )
        effective_y_spacing = max(y_spacing, min_track_gap)

        grid_info[row] = {
            "section_ids": list(sec_ids),
            "slot_count": grid_slots,
            "slot_spacing": effective_y_spacing,
            "max_y_pad": max_y_pad,
        }

        # Remap each section's stations to the shared grid
        for sec_id in sec_ids:
            sub = section_subgraphs[sec_id]
            section = graph.sections[sec_id]

            layer_stations, multi_layer_ys = _classify_multi_station_ys(sub)

            # Remap multi-station-layer Y values first.
            if multi_layer_ys:
                remap_ys = sorted(multi_layer_ys)
            else:
                remap_ys = sorted(set(s.y for s in sub.stations.values()))

            # Check for diamond patterns: isolated Y values that sit
            # between adjacent remap_ys indicate a join/fork hub.
            # These need at least 2 grid slots between tracks so the
            # hub has visual room.  Only for small fan-outs; large
            # fan-outs (>3 per layer) keep their original spacing.
            max_layer_size = max((len(ys) for ys in layer_stations.values()), default=0)
            all_ys = sorted(set(s.y for s in sub.stations.values()))
            isolated_ys = set(all_ys) - set(remap_ys)
            has_diamond = False
            if max_layer_size <= 3 and len(remap_ys) >= 2 and isolated_ys:
                for iso_y in isolated_ys:
                    if remap_ys[0] < iso_y < remap_ys[-1]:
                        has_diamond = True
                        break

            # Map Y values to grid slots.
            #
            # Uniform spacing preservation: when all input gaps are
            # equal (e.g. [0, 68.8, 137.6] with gap 68.8), map to
            # equally-spaced slots so the output gaps stay uniform.
            # This avoids asymmetric compression that causes label
            # clashes (e.g. floor gives [0,40,120] = gaps 40,80).
            #
            # Diamond gap enforcement: when a fork/join hub sits
            # between tracks, ensure at least 2-slot gap so the hub
            # has visual room.
            y_map: dict[float, float] = {}

            # Detect uniform input spacing
            gaps = [remap_ys[i + 1] - remap_ys[i] for i in range(len(remap_ys) - 1)]
            uniform_gap = len(gaps) >= 1 and all(abs(g - gaps[0]) < 1.0 for g in gaps)

            if uniform_gap and len(gaps) >= 1:
                # Floor the uniform gap to a whole number of grid
                # slots (minimum 1).  Using floor instead of round
                # prevents inflation (e.g. a 68.8px gap with 40px
                # spacing stays at 1 slot, not 2).
                slot_gap = max(1, int(math.floor(gaps[0] / effective_y_spacing)))
                if has_diamond and slot_gap < 2:
                    slot_gap = 2
                for i, old_y in enumerate(remap_ys):
                    y_map[old_y] = i * slot_gap * effective_y_spacing
            else:
                # Build set of Y-value pairs that MUST occupy different
                # grid slots because they co-occur at the same layer.
                # Unlike the previous "check only previous remap_y"
                # approach, we check against ALL values already
                # assigned to the candidate slot, so non-adjacent
                # pairs (e.g. sortmerna and ribodetector separated by
                # trimgalore in remap_ys) are still caught.
                must_separate: set[tuple[float, float]] = set()
                for ys_at_layer in layer_stations.values():
                    unique_ys = sorted(
                        set(y for y in ys_at_layer if y in multi_layer_ys)
                    )
                    for a_idx in range(len(unique_ys)):
                        for b_idx in range(a_idx + 1, len(unique_ys)):
                            must_separate.add((unique_ys[a_idx], unique_ys[b_idx]))

                slot_for_y: dict[float, int] = {}
                prev_slot = 0
                for i, old_y in enumerate(remap_ys):
                    raw_slot = int(math.floor(old_y / effective_y_spacing))
                    slot = max(raw_slot, prev_slot)
                    # Check if this Y must be separated from any value
                    # already assigned to the same slot.  Re-check after
                    # each bump in case the new slot also conflicts.
                    _changed = True
                    while _changed:
                        _changed = False
                        for other_y, other_slot in slot_for_y.items():
                            if other_slot != slot:
                                continue
                            pair = (min(other_y, old_y), max(other_y, old_y))
                            if pair in must_separate:
                                slot += 1
                                _changed = True
                                break
                    # Diamond: ensure at least 2-slot gap from previous
                    if has_diamond and prev_slot > 0 and slot - prev_slot < 2:
                        slot = prev_slot + 2
                    elif has_diamond and prev_slot == 0 and slot < 2 and old_y > 0:
                        slot = 2
                    y_map[old_y] = slot * effective_y_spacing
                    slot_for_y[old_y] = slot
                    prev_slot = slot

            # Multi-line label clearance: when a multi-line label station
            # is sandwiched between same-layer neighbors above AND below,
            # the label must fit within the gap.  Enforce a 2-slot gap
            # from the preceding Y so the label text doesn't overlap.
            # Stations at the top or bottom of their column can extend
            # outward and don't need the extra gap.
            layer_at_y: dict[tuple[int, float], bool] = {}
            for st in sub.stations.values():
                if not st.is_port:
                    layer_at_y[(st.layer, st.y)] = True
            _needs_gap_ys: set[float] = set()
            for st in sub.stations.values():
                if st.is_port or not st.label or "\n" not in st.label:
                    continue
                if st.y not in y_map:
                    continue
                has_above = any(
                    (st.layer, ry) in layer_at_y for ry in remap_ys if ry < st.y - 0.5
                )
                has_below = any(
                    (st.layer, ry) in layer_at_y for ry in remap_ys if ry > st.y + 0.5
                )
                if has_above and has_below:
                    _needs_gap_ys.add(st.y)
            if _needs_gap_ys:
                sorted_mapped = sorted(y_map.items(), key=lambda kv: kv[1])
                for idx in range(1, len(sorted_mapped)):
                    old_y, new_y = sorted_mapped[idx]
                    prev_y = sorted_mapped[idx - 1][1]
                    if old_y not in _needs_gap_ys:
                        continue
                    gap_slots = round((new_y - prev_y) / effective_y_spacing)
                    if gap_slots < 2:
                        extra = (2 - gap_slots) * effective_y_spacing
                        for j in range(idx, len(sorted_mapped)):
                            k = sorted_mapped[j][0]
                            y_map[k] += extra
                        sorted_mapped = sorted(y_map.items(), key=lambda kv: kv[1])

            # Snap isolated Y values to the nearest grid slot (any
            # multiple of effective_y_spacing, not just mapped slots).
            # This keeps diamond join points between tracks on-grid
            # without collapsing them onto a track endpoint.  Skip for
            # large fan-outs where snapping disrupts routing geometry.
            if max_layer_size <= 3:
                for old_y in all_ys:
                    if old_y not in y_map:
                        slot = round(old_y / effective_y_spacing)
                        y_map[old_y] = slot * effective_y_spacing

            for station in sub.stations.values():
                if station.y in y_map:
                    station.y = y_map[station.y]

            # y_pad compensation: shift all stations so that the
            # distance from the top of the Y range to the first
            # station equals max_y_pad.  After Phase 9 top-aligns
            # bbox_y, this makes first_station_y consistent across
            # sections with different multiline label padding.
            y_pad = section_y_padding + _multiline_label_padding(sub)
            shift = max_y_pad - y_pad
            if shift > 0:
                for station in sub.stations.values():
                    station.y += shift

            # Recompute bbox to match remapped + shifted positions.
            ys = [s.y for s in sub.stations.values()]
            section.bbox_y = min(ys) - max_y_pad
            section.bbox_h = (max(ys) - min(ys)) + max_y_pad * 2

    graph._row_y_grid_info = grid_info


def _recompute_grid_group_bboxes(graph: MetroGraph) -> None:
    """Recompute bboxes for sections in grid groups after port finalisation.

    Earlier phases may temporarily expand bboxes for port positions that
    are later corrected.  This step resets each grid-group section's bbox
    to symmetric ``max_y_pad`` padding around the final non-port station
    Y range, then expands for any ports that fall outside.
    """
    grid_info = graph._row_y_grid_info
    for _row, info in grid_info.items():
        max_y_pad = info["max_y_pad"]
        for sec_id in info["section_ids"]:
            section = graph.sections.get(sec_id)
            if not section:
                continue
            non_port_ys = [
                graph.stations[sid].y
                for sid in section.station_ids
                if sid in graph.stations and not graph.stations[sid].is_port
            ]
            if not non_port_ys:
                continue
            min_y = min(non_port_ys)
            max_y = max(non_port_ys)
            section.bbox_y = min_y - max_y_pad
            section.bbox_h = (max_y - min_y) + max_y_pad * 2
            # Expand for ports that landed outside the symmetric bbox.
            # Use bare containment (no extra padding) to avoid
            # inflating the bbox asymmetrically for off-grid ports.
            top = section.bbox_y
            bot = section.bbox_y + section.bbox_h
            for sid in section.station_ids:
                st = graph.stations.get(sid)
                if st and st.is_port:
                    if st.y < top:
                        section.bbox_h += top - st.y
                        section.bbox_y = st.y
                    elif st.y > bot:
                        section.bbox_h = st.y - section.bbox_y
                    top = section.bbox_y
                    bot = section.bbox_y + section.bbox_h


def _top_align_row_sections(graph: MetroGraph) -> None:
    """Shift sections up so bbox tops align within each grid row.

    Only aligns sections that form contiguous column groups within the
    row.  Sections separated by a column gap (e.g. reporting at col 3
    vs dna_analysis at col 1 with no row-mate at col 2) are aligned
    independently so structurally-determined positions aren't disturbed.
    """
    row_sections: dict[int, list[Section]] = defaultdict(list)
    for section in graph.sections.values():
        if section.bbox_h > 0 and section.grid_row >= 0:
            row_sections[section.grid_row].append(section)

    for row, sections in row_sections.items():
        if len(sections) < 2:
            continue
        # Group into contiguous column runs
        sections_by_col = sorted(sections, key=lambda s: s.grid_col)
        groups: list[list[Section]] = [[sections_by_col[0]]]
        for s in sections_by_col[1:]:
            if s.grid_col - groups[-1][-1].grid_col <= 1:
                groups[-1].append(s)
            else:
                groups.append([s])

        for group in groups:
            if len(group) < 2:
                continue
            min_top = min(s.bbox_y for s in group)
            for section in group:
                delta = section.bbox_y - min_top
                if delta <= 0:
                    continue
                for sid in section.station_ids:
                    station = graph.stations.get(sid)
                    if station:
                        station.y -= delta
                section.bbox_y -= delta


def _row_contiguous_column_groups(
    graph: MetroGraph,
) -> list[list[Section]]:
    """Group laid-out sections by grid row into contiguous column runs.

    Each returned group has at least 2 sections sitting in adjacent
    grid columns (gap <= 1) within the same row.  Sections with no
    bbox or unassigned row are skipped, matching the precondition
    used by the row-alignment callers in this module.
    """
    by_row: dict[int, list[Section]] = defaultdict(list)
    for section in graph.sections.values():
        if section.bbox_h > 0 and section.grid_row >= 0:
            by_row[section.grid_row].append(section)

    result: list[list[Section]] = []
    for row in by_row.values():
        if len(row) < 2:
            continue
        row_sorted = sorted(row, key=lambda s: s.grid_col)
        group = [row_sorted[0]]
        for s in row_sorted[1:]:
            if s.grid_col - group[-1].grid_col <= 1:
                group.append(s)
            else:
                if len(group) >= 2:
                    result.append(group)
                group = [s]
        if len(group) >= 2:
            result.append(group)
    return result



def _top_align_row_bboxes_only(graph: MetroGraph) -> None:
    """Align bbox tops within each row by growing bboxes upward.

    Unlike ``_top_align_row_sections`` (which shifts stations together
    with their bbox), this phase only moves ``bbox_y`` and grows
    ``bbox_h`` so the section background extends upward to match the
    row's topmost bbox.  Station, port and junction Ys inside the
    section are left in place, producing empty space at the top of
    sections that didn't have off-track inputs to lift.

    Used after ``_lift_off_track_stations`` so off-track expansion in
    one section doesn't leave other row-mates with misaligned bbox
    tops.
    """
    for group in _row_contiguous_column_groups(graph):
        min_top = min(s.bbox_y for s in group)
        for section in group:
            delta = section.bbox_y - min_top
            if delta <= 0:
                continue
            section.bbox_y = min_top
            section.bbox_h += delta


def _section_trunk_y(graph: MetroGraph, section: Section) -> float | None:
    """Topmost Y of a full-bundle internal station connected to an LR port.

    This Y is what neighbouring sections must line up with for the row
    bundle to flow horizontally.  Returns ``None`` when no full-bundle
    internal station is directly connected to any LR port.
    """
    if section.direction not in ("LR", "RL"):
        return None
    bundle = _section_bundle_lines(graph, section)
    if not bundle:
        return None
    port_ids = set(section.entry_ports) | set(section.exit_ports)
    internal_ids = set(section.station_ids) - port_ids
    trunk_ys: set[float] = set()
    for pid in port_ids:
        p = graph.ports.get(pid)
        if p is None or p.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        for edge in graph.edges:
            other_id = (
                edge.target
                if edge.source == pid and edge.target in internal_ids
                else edge.source
                if edge.target == pid and edge.source in internal_ids
                else None
            )
            if other_id is None:
                continue
            st = graph.stations.get(other_id)
            if st and not st.is_port and set(graph.station_lines(other_id)) == bundle:
                trunk_ys.add(round(st.y, 3))
    return min(trunk_ys) if trunk_ys else None


def _align_row_trunk_ys(graph: MetroGraph) -> None:
    """Shift sections vertically so trunk Ys align within each grid row.

    Sections in a row's contiguous column run whose trunk Y sits above
    the row's deepest trunk shift down to match.  Bbox tops are
    preserved (heights grow downward).  Row-spanning sections
    (grid_row_span > 1) are skipped to avoid disturbing cross-row
    vertical relationships.
    """
    row_sections: dict[int, list[Section]] = defaultdict(list)
    for section in graph.sections.values():
        if (
            section.bbox_h <= 0
            or section.grid_row < 0
            or section.direction not in ("LR", "RL")
            or section.grid_row_span > 1
        ):
            continue
        row_sections[section.grid_row].append(section)

    for sections in row_sections.values():
        if len(sections) < 2:
            continue
        sections_by_col = sorted(sections, key=lambda s: s.grid_col)
        groups: list[list[Section]] = [[sections_by_col[0]]]
        for s in sections_by_col[1:]:
            if s.grid_col - groups[-1][-1].grid_col <= 1:
                groups[-1].append(s)
            else:
                groups.append([s])

        for group in groups:
            if len(group) < 2:
                continue
            # Only realign when every LR-bearing section in the group
            # shares the same bundle.  Differing bundles mean the row
            # has no single trunk crossing all sections, so forcing a
            # common Y just shifts content downward without any
            # geometric gain.
            bundles = [_section_bundle_lines(graph, s) for s in group]
            non_empty = [b for b in bundles if b]
            if not non_empty or any(b != non_empty[0] for b in non_empty):
                continue
            trunks = {
                s.id: t for s in group if (t := _section_trunk_y(graph, s)) is not None
            }
            if len(trunks) < 2:
                continue
            target_y = max(trunks.values())
            shifted: set[str] = set()
            for section in group:
                ty = trunks.get(section.id)
                if ty is None:
                    continue
                delta = target_y - ty
                if delta < 0.5:
                    continue
                for sid in section.station_ids:
                    st = graph.stations.get(sid)
                    if st:
                        st.y += delta
                    port = graph.ports.get(sid)
                    if port:
                        port.y += delta
                section.bbox_h += delta
                shifted.add(section.id)

            # Re-snap each shifted section's LR ports to target_y when
            # they have a single internal station at target_y.  Skip
            # ports with 2+ distinct internal Ys (fan-in centering).
            for section in group:
                if section.id not in shifted:
                    continue
                port_set = set(section.entry_ports) | set(section.exit_ports)
                internal_ids = set(section.station_ids) - port_set
                for pid in port_set:
                    p = graph.ports.get(pid)
                    port_st = graph.stations.get(pid)
                    if (
                        p is None
                        or port_st is None
                        or p.side not in (PortSide.LEFT, PortSide.RIGHT)
                        or abs(port_st.y - target_y) < 0.5
                    ):
                        continue
                    connected_ys: set[float] = set()
                    target_aligned = False
                    for edge in graph.edges:
                        other_id = (
                            edge.target
                            if edge.source == pid and edge.target in internal_ids
                            else edge.source
                            if edge.target == pid and edge.source in internal_ids
                            else None
                        )
                        if other_id is None:
                            continue
                        st = graph.stations.get(other_id)
                        if st and not st.is_port:
                            connected_ys.add(round(st.y, 1))
                            if abs(st.y - target_y) < 0.5:
                                target_aligned = True
                    if len(connected_ys) < 2 and target_aligned:
                        _set_port_y(graph, pid, target_y)


def _compact_row_content_to_bbox_top(
    graph: MetroGraph, section_y_padding: float, y_spacing: float
) -> None:
    """Pull row-mate sections up and shrink bottoms so content fits snugly.

    Two-step compaction within each grid row's contiguous column run:

    1. Per section, compute the allowable upward shift of on-track
       content:

       * bounded above by ``min(on_track_y) - bbox_y - section_y_padding``
         so on-track content stays inside the bbox padding zone;
       * bounded above by ``min(on_track_y) - max(off_track_y) - y_spacing_floor``
         so on-track content stays clear of any lifted off-track band.

       The uniform shift applied to the group is the minimum allowable
       shift across same-row sections; that preserves the trunk-Y
       alignment established by Phase 11ca.  Only on-track stations
       and ports move; off-track stations stay anchored to the lifted
       band Phase 13 placed them on.
    2. Shrink each section's ``bbox_h`` so the bottom slack matches
       ``section_y_padding`` (clamped so ports inside the section stay
       within the bbox).

    Sections with ``grid_row_span > 1`` are excluded because their
    content spans multiple rows and the per-row frame doesn't apply.
    """

    def _is_off_track(sid: str) -> bool:
        st = graph.stations.get(sid)
        return bool(st and getattr(st, "off_track", False))

    row_sections: dict[int, list[Section]] = defaultdict(list)
    for section in graph.sections.values():
        if (
            section.bbox_h <= 0
            or section.grid_row < 0
            or section.grid_row_span > 1
        ):
            continue
        row_sections[section.grid_row].append(section)

    # Minimum clearance between the off-track lift band and any
    # on-track content shifted upward toward it.  Phase 13 installs a
    # full ``y_spacing`` gap; compaction may shrink it to roughly one
    # station-row's worth of clearance so labels still avoid the
    # topmost line track.
    off_track_gap = max(FONT_HEIGHT + STATION_RADIUS_APPROX * 2, y_spacing / 2)

    for sections in row_sections.values():
        if not sections:
            continue
        sections_by_col = sorted(sections, key=lambda s: s.grid_col)
        groups: list[list[Section]] = [[sections_by_col[0]]]
        for s in sections_by_col[1:]:
            if s.grid_col - groups[-1][-1].grid_col <= 1:
                groups[-1].append(s)
            else:
                groups.append([s])

        for group in groups:
            allowed_shifts: list[float] = []
            for section in group:
                on_track_ys = [
                    graph.stations[sid].y
                    for sid in section.station_ids
                    if sid in graph.stations
                    and not graph.stations[sid].is_port
                    and not _is_off_track(sid)
                ]
                if not on_track_ys:
                    continue
                on_track_min = min(on_track_ys)
                shift = on_track_min - section.bbox_y - section_y_padding
                off_track_ys = [
                    graph.stations[sid].y
                    for sid in section.station_ids
                    if sid in graph.stations and _is_off_track(sid)
                ]
                if off_track_ys:
                    clear_shift = on_track_min - max(off_track_ys) - off_track_gap
                    shift = min(shift, clear_shift)
                allowed_shifts.append(max(0.0, shift))
            delta = min(allowed_shifts) if allowed_shifts else 0.0
            if delta >= 0.5:
                for section in group:
                    for sid in section.station_ids:
                        if _is_off_track(sid):
                            continue
                        st = graph.stations.get(sid)
                        if st:
                            st.y -= delta
                        port = graph.ports.get(sid)
                        if port:
                            port.y -= delta
                    section.bbox_h = max(0.0, section.bbox_h - delta)

            for section in group:
                content_max_ys = [
                    graph.stations[sid].y
                    for sid in section.station_ids
                    if sid in graph.stations
                    and not graph.stations[sid].is_port
                ]
                port_max_ys = [
                    graph.stations[sid].y
                    for sid in section.station_ids
                    if sid in graph.stations and graph.stations[sid].is_port
                ]
                if not content_max_ys:
                    continue
                desired_bot = max(content_max_ys) + section_y_padding
                if port_max_ys:
                    desired_bot = max(desired_bot, max(port_max_ys))
                new_h = desired_bot - section.bbox_y
                if new_h < section.bbox_h - 0.5:
                    section.bbox_h = max(0.0, new_h)


def _section_bundle_lines(graph: MetroGraph, section: Section) -> set[str]:
    """Return the set of line IDs crossing a section's LEFT/RIGHT ports."""
    bundle: set[str] = set()
    for pid in list(section.entry_ports) + list(section.exit_ports):
        port = graph.ports.get(pid)
        if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        for edge in graph.edges:
            if edge.source == pid or edge.target == pid:
                bundle.add(edge.line_id)
    return bundle


def _redistribute_fanout_siblings(graph: MetroGraph, y_spacing: float) -> None:
    """Symmetrically distribute fan-out siblings around a trunk junction.

    Active when ``graph.center_ports`` is True.  For each LR/RL section
    in the grid, iterate by column: a column qualifies as a fan-out
    junction when it has exactly one station whose line set equals the
    section's full LEFT/RIGHT bundle (the trunk junction) AND at least
    one sibling whose line set is a strict subset of the bundle.

    In those columns, the trunk station is pinned at its current Y and
    the strict-subset siblings are redistributed in alternating slots
    ``+1, -1, +2, -2, ...`` at ``y_spacing`` pitch above and below it.

    Strict scoping: only stations in a trunk-junction column AND with
    a strict-subset line set are moved.  File inputs, processing
    chains, fan-in stations, columns without a unique trunk, and
    siblings carrying the full bundle (linear pass-throughs) are left
    in place so non-fan-out topologies keep their natural Y ordering.

    No-op when ``--no-center-ports`` is set, when a section has no
    qualifying trunk-junction column, or when there are no
    strict-subset siblings.
    """
    if not graph.center_ports:
        return
    grid_sec_ids = _grid_group_section_ids(graph)
    if not grid_sec_ids:
        return

    for section in graph.sections.values():
        if (
            section.id not in grid_sec_ids
            or section.direction not in ("LR", "RL")
            or section.bbox_h <= 0
        ):
            continue
        bundle = _section_bundle_lines(graph, section)
        if not bundle:
            continue
        port_ids = set(section.entry_ports) | set(section.exit_ports)

        # Group non-port stations by column x.
        cols: dict[float, list[str]] = defaultdict(list)
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None:
                continue
            cols[round(st.x, 3)].append(sid)

        for sids in cols.values():
            # Identify trunk station in this column: lines == bundle, unique.
            trunks = [s for s in sids if set(graph.station_lines(s)) == bundle]
            if len(trunks) != 1:
                continue
            trunk_sid = trunks[0]
            trunk_y = graph.stations[trunk_sid].y
            # Fan-out siblings: strict subset of bundle (skip full-bundle
            # pass-throughs and orphan stations with no lines).
            siblings = [
                s
                for s in sids
                if s != trunk_sid
                and set(graph.station_lines(s))
                and set(graph.station_lines(s)) < bundle
            ]
            if not siblings:
                continue
            siblings.sort(key=lambda s: graph.stations[s].y)
            for i, sid in enumerate(siblings, 1):
                k = (i + 1) // 2
                sign = 1 if (i % 2 == 1) else -1
                graph.stations[sid].y = trunk_y + sign * k * y_spacing


def _layout_single_section(
    graph: MetroGraph,
    section: Section,
    x_spacing: float,
    y_spacing: float,
    section_x_padding: float,
    section_y_padding: float,
) -> MetroGraph | None:
    """Lay out a single section's internal stations and compute its bbox.

    Runs layer/track assignment on the section's real stations, applies
    direction-specific adjustments (RL mirror, TB label extent, entry shifts),
    and computes the section bounding box. Returns the section subgraph with
    positioned stations, or None if the section has no layoutable stations.
    """
    sub = _build_section_subgraph(graph, section)
    if not sub.stations:
        return None

    # Insert phantom pass-throughs into the subgraph (not the main graph)
    # so that lines entering at a deep layer get their own track.
    _insert_phantom_pass_throughs(graph, section, sub)

    layers = assign_layers(sub)

    # Use entry-top ordering when the immediate predecessor section is
    # horizontal (LR/RL), so the entry-connected station stays at the
    # top and aligns with the upstream exit station (#165).  Skip for
    # TB predecessors where vertical entry makes top-biasing inappropriate.
    entry_top = False
    if section.entry_ports and section.direction in ("LR", "RL"):
        for pid in section.entry_ports:
            for edge in graph.edges:
                if edge.target == pid:
                    src_port = graph.ports.get(edge.source)
                    if src_port:
                        src_sec = graph.sections.get(src_port.section_id)
                        if src_sec and src_sec.direction in ("LR", "RL"):
                            entry_top = True
                            break
            if entry_top:
                break

    tracks = assign_tracks(sub, layers, entry_top=entry_top)

    if not layers:
        return None

    # Snap phantom pass-throughs' successors to the pass-through track
    # so the trunk line stays horizontal past bypassed stations.
    _align_phantom_pass_throughs(sub, tracks)

    # Compact tracks so widely-spaced line priorities don't inflate
    # the vertical spread.  Gaps larger than LINE_GAP get capped so
    # distant line base tracks don't create excessive whitespace.
    unique_tracks = sorted(set(tracks.values()))
    track_rank: dict[float, float] = {}
    if unique_tracks:
        track_rank[unique_tracks[0]] = 0.0
        for idx in range(1, len(unique_tracks)):
            gap = unique_tracks[idx] - unique_tracks[idx - 1]
            track_rank[unique_tracks[idx]] = track_rank[unique_tracks[idx - 1]] + min(
                gap, LINE_GAP
            )

    # Detect fork/join layers and add extra spacing so stations
    # aren't too close to divergence/convergence points.
    section_sids = set(section.station_ids)
    layer_extra = _compute_fork_join_gaps(
        sub, layers, tracks, x_spacing, graph, section_sids
    )

    # Widen track spacing when multi-line labels need more vertical room
    effective_y_spacing = _multiline_track_spacing(sub, y_spacing)

    # Assign local coordinates based on section direction
    for sid, station in sub.stations.items():
        station.layer = layers.get(sid, 0)
        station.track = tracks.get(sid, 0)
        if section.direction == "TB":
            station.x = track_rank[station.track] * x_spacing
            station.y = station.layer * y_spacing + layer_extra.get(station.layer, 0)
        else:
            station.x = station.layer * x_spacing + layer_extra.get(station.layer, 0)
            station.y = track_rank[station.track] * effective_y_spacing

    # Resolve same-cell station collisions: two stations on the same line
    # priority can land on identical (x,y) when the track allocator collapses
    # distinct line tracks at a layer with only one occupant per line.
    _resolve_station_collisions(sub, section, x_spacing, effective_y_spacing)

    # Normalize Y so minimum is 0 (raw tracks can be negative)
    _normalize_min(sub, axis="y")

    # RL: mirror X so layer 0 is rightmost
    if section.direction == "RL":
        _mirror_rl(sub)

    # Normalize local X so leftmost station is at x=0
    _normalize_min(sub, axis="x")

    # Ensure minimum inner extent so stations sit on visible track
    _enforce_min_extent(sub, section, x_spacing, y_spacing)

    # Compute section bounding box from real stations only.
    # Extra Y padding for multi-line labels (outermost stations' labels
    # extend beyond the normal padding).
    xs = [s.x for s in sub.stations.values()]
    ys = [s.y for s in sub.stations.values()]
    extra_label_h = _multiline_label_padding(sub)
    y_pad = section_y_padding + extra_label_h
    section.bbox_x = min(xs) - section_x_padding
    section.bbox_y = min(ys) - y_pad
    section.bbox_w = (max(xs) - min(xs)) + section_x_padding * 2
    section.bbox_h = (max(ys) - min(ys)) + y_pad * 2

    # Apply direction-specific bbox adjustments
    _adjust_tb_labels(sub, section, graph)
    _adjust_tb_entry_shifts(section, sub, graph, y_spacing)
    _adjust_lr_entry_inset(sub, section, graph, x_spacing)
    _adjust_lr_exit_gap(sub, section, graph, layers, x_spacing)
    _adjust_lr_label_clearance(sub, section)
    _adjust_terminus_icon_clearance(sub, section, graph)

    return sub


def _resolve_station_collisions(
    sub: MetroGraph,
    section: Section,
    x_spacing: float,
    y_spacing: float,
) -> None:
    """Push stations apart when track compaction collides them in the same cell.

    The track allocator can return identical track values for two stations on
    different lines when each is the sole occupant of its line at a given
    layer (e.g. side-by-side terminus branches). After coordinate assignment
    they end up at the same (x, y), causing visual overlap. This pass detects
    such collisions and shifts the later-defined station along the section's
    secondary axis by one spacing unit, repeating until the cell is unique.
    """
    if section.direction == "TB":
        primary, secondary, primary_step, step = "y", "x", y_spacing, x_spacing
    else:
        primary, secondary, primary_step, step = "x", "y", x_spacing, y_spacing

    EPS = 0.5
    real = [s for s in sub.stations.values() if not s.is_port and not s.is_hidden]
    if len(real) < 2:
        return

    # Group stations by primary-axis bucket (layer column for LR/RL,
    # row for TB).  Use the primary-axis step size; the bucket spans a
    # half-step either side of a layer centre so off-grid layer_extra
    # offsets stay in the same bucket as their layer peers.
    primary_step_norm = max(primary_step, 1.0)
    by_primary: dict[float, list] = {}
    for s in real:
        bucket = round(getattr(s, primary) / primary_step_norm)
        by_primary.setdefault(bucket, []).append(s)

    # Stable tiebreaker so the earlier-defined station keeps its slot
    # when two share a secondary coord (insertion order in sub.stations).
    order = {sid: i for i, sid in enumerate(sub.stations)}

    for stations in by_primary.values():
        if len(stations) < 2:
            continue
        stations.sort(key=lambda s: (getattr(s, secondary), order.get(s.id, 0)))
        used: list[float] = []
        for s in stations:
            pos = getattr(s, secondary)
            while any(abs(pos - u) < step - EPS for u in used):
                pos += step
            if pos != getattr(s, secondary):
                setattr(s, secondary, pos)
            used.append(pos)


def _multiline_track_spacing(sub: MetroGraph, y_spacing: float) -> float:
    """Return effective Y track spacing, widened for multi-line labels.

    When labels from adjacent tracks face each other (one below, one
    above due to layer alternation) the track gap must be large enough
    for both labels plus clearance.  Returns *y_spacing* unchanged when
    no multi-line labels are present.
    """
    max_text_h = FONT_HEIGHT
    for s in sub.stations.values():
        n = s.label.count("\n")
        if n > 0:
            h = FONT_HEIGHT + n * FONT_HEIGHT * LABEL_LINE_HEIGHT
            max_text_h = max(max_text_h, h)

    if max_text_h <= FONT_HEIGHT:
        return y_spacing  # no multi-line labels

    # Worst case: adjacent tracks with labels facing inward.
    # Each side needs label_offset + its text height.
    min_gap = LABEL_OFFSET + max_text_h + LABEL_OFFSET + FONT_HEIGHT + LABEL_MARGIN
    return max(y_spacing, min_gap)


def _multiline_label_padding(sub: MetroGraph) -> float:
    """Return extra bbox Y padding for the tallest multi-line label."""
    max_extra = 0.0
    for s in sub.stations.values():
        n = s.label.count("\n")
        if n > 0:
            extra = n * FONT_HEIGHT * LABEL_LINE_HEIGHT
            max_extra = max(max_extra, extra)
    return max_extra


def _normalize_min(sub: MetroGraph, axis: str) -> None:
    """Shift all stations so the minimum coordinate on the given axis is 0."""
    vals = [getattr(s, axis) for s in sub.stations.values()]
    if vals:
        min_val = min(vals)
        if min_val != 0:
            for s in sub.stations.values():
                setattr(s, axis, getattr(s, axis) - min_val)


def _mirror_rl(sub: MetroGraph) -> None:
    """Mirror X coordinates for RL sections so layer 0 is rightmost.

    Anchors on non-terminus stations so adding terminus layers
    extends leftward without shifting the entry point.
    """
    non_term = [
        s for s in sub.stations.values() if not (s.is_terminus and not s.label.strip())
    ]
    anchor_stations = non_term if non_term else list(sub.stations.values())
    max_x_val = max(s.x for s in anchor_stations)
    for s in sub.stations.values():
        s.x = max_x_val - s.x


def _enforce_min_extent(
    sub: MetroGraph,
    section: Section,
    x_spacing: float,
    y_spacing: float,
) -> None:
    """Ensure minimum inner extent so stations sit on visible track."""
    xs = [s.x for s in sub.stations.values()]
    ys = [s.y for s in sub.stations.values()]
    if section.direction == "TB":
        inner_h = max(ys) - min(ys)
        min_inner_h = y_spacing
        if inner_h < min_inner_h:
            shift = (min_inner_h - inner_h) / 2
            for station in sub.stations.values():
                station.y += shift
    else:
        inner_w = max(xs) - min(xs)
        min_inner_w = x_spacing
        if inner_w < min_inner_w:
            shift = (min_inner_w - inner_w) / 2
            for station in sub.stations.values():
                station.x += shift


def _adjust_tb_labels(
    sub: MetroGraph,
    section: Section,
    graph: MetroGraph,
) -> None:
    """TB sections: expand bbox and shift stations right so labels fit.

    Labels extend leftward from the station (text_anchor=end).
    """
    if section.direction != "TB":
        return

    xs = [s.x for s in sub.stations.values()]
    max_label_extent = 0.0
    for sid, s in sub.stations.items():
        if s.label.strip():
            n_lines = len(sub.station_lines(sid))
            offset_span = (n_lines - 1) * TB_LINE_Y_OFFSET
            extent = offset_span / 2 + 11 + label_text_width(s.label)
            max_label_extent = max(max_label_extent, extent)
    need_left = max_label_extent + LABEL_PAD
    have_left = min(xs) - section.bbox_x
    if need_left > have_left:
        extra = need_left - have_left
        for s in sub.stations.values():
            s.x += extra
        section.bbox_w += extra


def _adjust_tb_entry_shifts(
    section: Section,
    sub: MetroGraph,
    graph: MetroGraph,
    y_spacing: float,
) -> None:
    """Apply TB section entry shifts for perpendicular and cross-column entries."""
    if section.direction != "TB":
        return

    # Perpendicular entry: shift stations down so first station isn't
    # at the entry port (avoiding station-as-elbow).
    has_perp_entry = any(
        graph.ports[pid].side in (PortSide.LEFT, PortSide.RIGHT)
        for pid in section.entry_ports
        if pid in graph.ports
    )
    if has_perp_entry:
        entry_shift = y_spacing * ENTRY_SHIFT_TB
        for s in sub.stations.values():
            s.y += entry_shift
        section.bbox_h += entry_shift

    # Cross-column TOP entry: shift stations down for L-shape routing room.
    has_cross_col_top_entry = False
    for pid in section.entry_ports:
        port = graph.ports.get(pid)
        if not port or port.side != PortSide.TOP:
            continue
        for edge in graph.edges:
            if edge.target == pid:
                src = graph.stations.get(edge.source)
                if src and src.section_id:
                    src_sec = graph.sections.get(src.section_id)
                    if src_sec and src_sec.grid_col != section.grid_col:
                        has_cross_col_top_entry = True
                        break
        if has_cross_col_top_entry:
            break
    if has_cross_col_top_entry:
        entry_shift = y_spacing * ENTRY_SHIFT_TB_CROSS
        for s in sub.stations.values():
            s.y += entry_shift
        section.bbox_h += entry_shift


def _adjust_lr_entry_inset(
    sub: MetroGraph,
    section: Section,
    graph: MetroGraph,
    x_spacing: float,
) -> None:
    """LR/RL sections: add extra bbox width when entry has curves."""
    if section.direction not in ("LR", "RL"):
        return

    has_perp_entry = any(
        graph.ports[pid].side in (PortSide.TOP, PortSide.BOTTOM)
        for pid in section.entry_ports
        if pid in graph.ports
    )
    if has_perp_entry:
        # Reserve enough width for the perp-entry station shift that creates
        # a gap between the perpendicular entry port and the first station.
        # This ensures the grid column is sized correctly before the shift.
        entry_inset = x_spacing * ENTRY_SHIFT_LR
        section.bbox_w += entry_inset
        return

    # Flow-side entry that fans out to multiple internal stations at
    # different Y positions needs extra room for the diagonal transitions.
    for pid in section.entry_ports:
        if pid not in graph.ports:
            continue
        flow_side = PortSide.LEFT if section.direction == "LR" else PortSide.RIGHT
        if graph.ports[pid].side != flow_side:
            continue
        targets = {
            e.target
            for e in graph.edges
            if e.source == pid and e.target in section.station_ids
        }
        if len(targets) > 1:
            entry_inset = x_spacing * EXIT_GAP_MULTIPLIER
            # For single-layer sections the asymmetry is very visible,
            # so split the inset between both sides to keep stations
            # visually centered (same logic as _adjust_lr_exit_gap).
            n_layers = len({s.layer for s in sub.stations.values()})
            shift = entry_inset / 2 if n_layers <= 1 else entry_inset
            for s in sub.stations.values():
                s.x += shift
            section.bbox_w += entry_inset
            return


def _adjust_lr_exit_gap(
    sub: MetroGraph,
    section: Section,
    graph: MetroGraph,
    layers: dict[str, int],
    x_spacing: float,
) -> None:
    """LR/RL sections with flow-side exit: add label clearance gap.

    The gap is only added when lines converge from different Y tracks to
    the exit port (requiring diagonal routing).  When all feeder stations
    share the same Y, lines exit straight horizontally and no extra space
    is needed.
    """
    if section.direction not in ("LR", "RL"):
        return

    flow_exit_side = PortSide.RIGHT if section.direction == "LR" else PortSide.LEFT
    flow_exit_port_ids = {
        pid
        for pid in section.exit_ports
        if pid in graph.ports and graph.ports[pid].side == flow_exit_side
    }
    if not flow_exit_port_ids or not layers:
        return

    # Collect Y positions of internal stations that feed into flow-side
    # exit ports.  If they all share the same Y, no diagonal convergence
    # is needed and the gap can be skipped.
    feeder_ys: set[float] = set()
    real_ids = set(sub.stations)
    for edge in graph.edges:
        if edge.target in flow_exit_port_ids and edge.source in real_ids:
            feeder_ys.add(sub.stations[edge.source].y)

    if len(feeder_ys) <= 1:
        return

    exit_gap = x_spacing * EXIT_GAP_MULTIPLIER

    # For single-layer sections the asymmetry is very visible, so split the
    # gap between both sides to keep the station visually centered.  For
    # multi-layer sections the gap belongs entirely on the exit side.
    n_layers = len(set(layers.values()))
    center = n_layers <= 1

    if section.direction == "LR":
        if center:
            half_gap = exit_gap / 2
            for s in sub.stations.values():
                s.x += half_gap
        section.bbox_w += exit_gap
    else:
        shift = exit_gap / 2 if center else exit_gap
        for s in sub.stations.values():
            s.x += shift
        section.bbox_w += exit_gap


def _adjust_lr_label_clearance(
    sub: MetroGraph,
    section: Section,
) -> None:
    """LR/RL sections: expand bbox so station labels fit within the box.

    Labels are centered on their station. If any label extends past the
    section bbox edge, expand the bbox (and shift stations if needed) so
    that section placement can equalize column widths correctly.
    """
    if section.direction not in ("LR", "RL"):
        return

    margin = LABEL_BBOX_MARGIN
    for s in sub.stations.values():
        if not s.label.strip():
            continue
        half_w = label_text_width(s.label) / 2
        label_left = s.x - half_w - margin
        label_right = s.x + half_w + margin

        if label_left < section.bbox_x:
            deficit = section.bbox_x - label_left
            # Shift all stations right and expand bbox on the left.
            # This moves the current station too, so we recompute
            # label_right below.  Later stations get more left-side
            # clearance, which is safe (they can only trigger further
            # right-side expansion, not undo this shift).
            for st in sub.stations.values():
                st.x += deficit
            section.bbox_w += deficit

        # Recompute after possible left-side shift
        label_right = s.x + half_w + margin
        bbox_right = section.bbox_x + section.bbox_w
        if label_right > bbox_right:
            section.bbox_w = label_right - section.bbox_x


def _terminus_icon_clearance(n_icons: int) -> float:
    """Compute clearance needed for *n_icons* file icons side-by-side.

    The base ``TERMINUS_ICON_CLEARANCE`` covers one icon (station_radius +
    gap + icon_width + margin).  Each additional icon adds icon_width + inter-
    icon gap.
    """
    if n_icons <= 1:
        return TERMINUS_ICON_CLEARANCE
    extra = (n_icons - 1) * (TERMINUS_WIDTH + ICON_INTER_GAP)
    return TERMINUS_ICON_CLEARANCE + extra


def _adjust_terminus_icon_clearance(
    sub: MetroGraph,
    section: Section,
    graph: MetroGraph,
) -> None:
    """Expand bbox when terminus file icons would be too close to the edge.

    Terminus stations display file icon(s) on their "outside" (flow-entry for
    sources, flow-exit for sinks).  The icon(s) extend horizontally from the
    station center.  If SECTION_X_PADDING doesn't provide enough room, we
    grow the bbox on the affected side.
    """
    for station in sub.stations.values():
        if not station.is_terminus:
            continue

        n_icons = len(station.terminus_labels)
        needed = _terminus_icon_clearance(n_icons)

        # Determine source vs sink from the full graph's edges
        is_source = not any(e.target == station.id for e in graph.edges)

        section_dir = section.direction or "LR"

        # Icon is always placed horizontally (left or right of station),
        # even for TB/BT sections.
        if section_dir in ("LR", "TB"):
            icon_on_left = is_source
        else:  # RL, BT
            icon_on_left = not is_source

        if icon_on_left:
            clearance = station.x - section.bbox_x
            if clearance < needed:
                expand = needed - clearance
                section.bbox_x -= expand
                section.bbox_w += expand
        else:
            bbox_right = section.bbox_x + section.bbox_w
            clearance = bbox_right - station.x
            if clearance < needed:
                expand = needed - clearance
                section.bbox_w += expand


def _shift_lr_perp_entry_stations(
    graph: MetroGraph,
    x_spacing: float,
) -> None:
    """Shift internal stations in LR/RL sections with perpendicular entry.

    Mirrors ``_adjust_tb_entry_shifts`` for horizontal-flow sections.
    In TB sections the station shift is applied in Phase 2, and entry-port
    alignment later overrides the port Y with the upstream source Y,
    creating a gap.  For LR/RL sections no such port-X override exists,
    so we shift stations after port initialisation (Phase 5) while ports
    stay put and internal stations move inward.

    The shift is only applied when the gap between the perpendicular entry
    port and the nearest entry-side internal station is smaller than the
    desired gap.  Sections where the gap is already sufficient are left
    untouched.
    """
    desired_gap = x_spacing * ENTRY_SHIFT_LR

    for section in graph.sections.values():
        if section.direction not in ("LR", "RL"):
            continue

        # Collect perpendicular entry port positions
        perp_port_xs: list[float] = []
        for pid in section.entry_ports:
            port = graph.ports.get(pid)
            if port and port.side in (PortSide.TOP, PortSide.BOTTOM):
                perp_port_xs.append(graph.stations[pid].x)
        if not perp_port_xs:
            continue

        # Collect internal station X positions
        port_ids = set(section.entry_ports) | set(section.exit_ports)
        internal_xs: list[float] = []
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            s = graph.stations.get(sid)
            if s and not s.is_port:
                internal_xs.append(s.x)
        if not internal_xs:
            continue

        # Compute the current gap between port and nearest entry-side station
        if section.direction == "LR":
            # Entry is LEFT: port is left of stations
            nearest_x = min(internal_xs)
            port_x = min(perp_port_xs)
            current_gap = nearest_x - port_x
        else:
            # RL: entry is RIGHT: port is right of stations
            nearest_x = max(internal_xs)
            port_x = max(perp_port_xs)
            current_gap = port_x - nearest_x

        shift = desired_gap - current_gap
        if shift <= 0:
            continue  # gap is already sufficient

        # Shift internal stations away from the entry side.
        # Phase 2 (_adjust_lr_entry_inset) already reserved bbox space
        # for this shift, so no bbox expansion is needed here.
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            s = graph.stations.get(sid)
            if not s or s.is_port:
                continue
            if section.direction == "LR":
                s.x += shift
            else:
                s.x -= shift


def _position_junctions(graph: MetroGraph) -> None:
    """Position junction stations at the midpoint of the inter-section gap.

    A junction is where bundled lines diverge to different downstream sections.
    It sits horizontally between the exit port and the entry ports, at the
    exit port's Y coordinate so lines travel straight from exit to junction.

    Merge junctions (N>1 predecessors, 1 entry port successor) are positioned
    at max(pred.x) + JUNCTION_MARGIN, y = entry_port.y to create a visible
    single-line segment from merge point to entry.
    """
    for jid in graph.junctions:
        junction = graph.stations.get(jid)
        if not junction:
            continue

        # Collect predecessors and successors
        predecessors: list[Station] = []
        successor_ports: list[Station] = []
        exit_port_id: str | None = None

        for edge in graph.edges:
            if edge.target == jid:
                src = graph.stations.get(edge.source)
                if src:
                    predecessors.append(src)
                    if src.is_port:
                        exit_port_id = edge.source
            if edge.source == jid:
                tgt = graph.stations.get(edge.target)
                if tgt and tgt.is_port:
                    successor_ports.append(tgt)

        # Merge junction: N>1 predecessors, 1 entry port successor
        if len(predecessors) > 1 and len(successor_ports) == 1:
            entry_port = successor_ports[0]
            entry_port_obj = graph.ports.get(entry_port.id)
            if entry_port_obj and entry_port_obj.is_entry:
                _position_merge_junction(junction, predecessors, entry_port)
                continue

        # Fan-out junction: 1 exit port predecessor, N>1 entry port successors
        exit_port_x: float | None = None
        exit_port_y: float | None = None
        entry_port_xs: list[float] = []

        for pred in predecessors:
            if pred.is_port:
                exit_port_x = pred.x
                exit_port_y = pred.y

        for succ in successor_ports:
            entry_port_xs.append(succ.x)

        if exit_port_x is not None and exit_port_y is not None and entry_port_xs:
            margin = JUNCTION_MARGIN
            exit_port_obj = graph.ports.get(exit_port_id) if exit_port_id else None
            if exit_port_obj and exit_port_obj.side == PortSide.BOTTOM:
                junction.x = exit_port_x
                junction.y = exit_port_y + margin
            elif exit_port_obj and exit_port_obj.side in (
                PortSide.RIGHT,
                PortSide.LEFT,
            ):
                direction = 1.0 if exit_port_obj.side == PortSide.RIGHT else -1.0
                junction.x = exit_port_x + direction * margin
                junction.y = exit_port_y
            else:
                nearest_entry_x = min(entry_port_xs, key=lambda x: abs(x - exit_port_x))
                direction = 1.0 if nearest_entry_x > exit_port_x else -1.0
                junction.x = exit_port_x + direction * margin
                junction.y = exit_port_y


def _position_merge_junction(
    junction: Station,
    predecessors: list[Station],
    entry_port: Station,
) -> None:
    """Position a merge junction near the entry port it feeds.

    Places at x = max(predecessor.x) + JUNCTION_MARGIN, y = entry_port.y
    so all converging lines share a visible single-line segment into the
    entry port.
    """
    max_pred_x = max(p.x for p in predecessors)
    junction.x = max_pred_x + JUNCTION_MARGIN
    junction.y = entry_port.y


def _resolve_source_section_id(
    graph: MetroGraph, edge_source: str, junction_ids: set[str]
) -> str | None:
    """Resolve the section ID of an edge's source, tracing through junctions.

    For port stations, returns section_id directly. For junctions, follows
    edges backward to find the connected port's section.
    """
    src = graph.stations.get(edge_source)
    if not src:
        return None
    src_section_id = src.section_id
    if edge_source in junction_ids:
        for e2 in graph.edges:
            if e2.target == edge_source:
                s2 = graph.stations.get(e2.source)
                if s2 and s2.section_id:
                    src_section_id = s2.section_id
                    break
    return src_section_id


def _resolve_source_xy(
    graph: MetroGraph,
    edge_source: str,
    junction_ids: set[str],
    _seen: set[str] | None = None,
) -> tuple[float, float] | None:
    """Return effective (x, y) for an edge source.

    For port stations, returns coordinates directly.  For junctions,
    derives coordinates from the feeding exit port, mirroring
    ``_position_junctions`` logic so that entry-port alignment does
    not depend on junctions being pre-positioned.  Recurses through
    chained junctions (junction-to-junction edges) to find the
    underlying exit port.
    """
    src = graph.stations.get(edge_source)
    if not src:
        return None
    if edge_source not in junction_ids:
        return src.x, src.y

    if _seen is None:
        _seen = set()
    if edge_source in _seen:
        return src.x, src.y
    _seen.add(edge_source)

    # Junction: find the feeding exit port and compute placement.
    chained: list[str] = []
    for e in graph.edges:
        if e.target != edge_source:
            continue
        if e.source in junction_ids:
            chained.append(e.source)
            continue
        exit_st = graph.stations.get(e.source)
        if not exit_st or not exit_st.is_port:
            continue
        exit_port_obj = graph.ports.get(e.source)
        if not exit_port_obj:
            return exit_st.x, exit_st.y
        if exit_port_obj.side == PortSide.BOTTOM:
            return exit_st.x, exit_st.y + JUNCTION_MARGIN
        elif exit_port_obj.side == PortSide.RIGHT:
            return exit_st.x + JUNCTION_MARGIN, exit_st.y
        elif exit_port_obj.side == PortSide.LEFT:
            return exit_st.x - JUNCTION_MARGIN, exit_st.y
        else:
            return exit_st.x + JUNCTION_MARGIN, exit_st.y

    # Recurse through chained junctions to find the underlying exit port.
    for js in chained:
        resolved = _resolve_source_xy(graph, js, junction_ids, _seen)
        if resolved is not None and resolved != (0.0, 0.0):
            return resolved

    # Fallback: use junction station's current coordinates.
    return src.x, src.y


def _set_port_y(graph: MetroGraph, port_id: str, y: float) -> None:
    """Set the Y coordinate on both the station and port objects."""
    station = graph.stations.get(port_id)
    port = graph.ports.get(port_id)
    if station:
        station.y = y
    if port:
        port.y = y


def _set_port_x(graph: MetroGraph, port_id: str, x: float) -> None:
    """Set the X coordinate on both the station and port objects."""
    station = graph.stations.get(port_id)
    port = graph.ports.get(port_id)
    if station:
        station.x = x
    if port:
        port.x = x


def _align_entry_ports(graph: MetroGraph) -> None:
    """Align entry ports with their incoming connection's coordinates.

    LEFT/RIGHT ports: align Y for straight horizontal runs.
    TOP/BOTTOM ports: align X for vertical drops or Y for cross-column.
    """
    junction_ids = set(graph.junctions)

    for port_id, port in graph.ports.items():
        if not port.is_entry:
            continue

        entry_section = graph.sections.get(port.section_id)
        if not entry_section:
            continue

        if port.side in (PortSide.LEFT, PortSide.RIGHT):
            _align_lr_entry_port(graph, port_id, port, entry_section, junction_ids)
        elif port.side in (PortSide.TOP, PortSide.BOTTOM):
            _align_tb_entry_port(graph, port_id, port, entry_section, junction_ids)


def _align_lr_entry_port(
    graph: MetroGraph,
    port_id: str,
    port,
    entry_section: Section,
    junction_ids: set[str],
) -> None:
    """Align a LEFT/RIGHT entry port's Y with its incoming source."""
    for edge in graph.edges:
        if edge.target != port_id:
            continue
        src = graph.stations.get(edge.source)
        if not src or not (src.is_port or edge.source in junction_ids):
            continue

        # Derive effective source coordinates (computes junction
        # placement on-the-fly so we don't need pre-positioned junctions).
        src_xy = _resolve_source_xy(graph, edge.source, junction_ids)
        if src_xy is None:
            continue
        src_x, src_y = src_xy

        src_section_id = _resolve_source_section_id(graph, edge.source, junction_ids)
        src_section = graph.sections.get(src_section_id) if src_section_id else None
        if not src_section:
            continue

        if entry_section.grid_row != src_section.grid_row:
            break

        # Skip alignment if source Y is too far outside entry section bbox.
        # Allow moderate expansion so ports align when adjacent sections
        # have different track counts (#165).
        entry_station = graph.stations.get(port_id)
        if entry_station:
            bbox_top = entry_section.bbox_y
            bbox_bot = entry_section.bbox_y + entry_section.bbox_h
            max_expand = entry_section.bbox_h * MAX_PORT_ALIGN_BBOX_EXPANSION_FRAC
            if src_y < bbox_top - max_expand or src_y > bbox_bot + max_expand:
                break
            # Expand bbox to contain aligned port if needed
            if src_y < bbox_top or src_y > bbox_bot:
                _expand_bbox_for_y(entry_section, src_y)

        target_y = src_y

        # Clamp for TB sections with perpendicular entry
        if entry_section.direction == "TB" and port.side in (
            PortSide.LEFT,
            PortSide.RIGHT,
        ):
            target_y = _clamp_tb_entry_port(
                graph,
                entry_section,
                target_y,
                edge,
                src,
                junction_ids,
            )

        _set_port_y(graph, port_id, target_y)
        break


def _align_tb_entry_port(
    graph: MetroGraph,
    port_id: str,
    port,
    entry_section: Section,
    junction_ids: set[str],
) -> None:
    """Align a TOP/BOTTOM entry port with its incoming sources."""
    # Collect all incoming sources.  Coordinates are derived via
    # _resolve_source_xy so junctions don't need to be pre-positioned.
    sources: list[tuple[float, float, str | None]] = []
    for edge in graph.edges:
        if edge.target != port_id:
            continue
        src = graph.stations.get(edge.source)
        if not src or not (src.is_port or edge.source in junction_ids):
            continue
        src_xy = _resolve_source_xy(graph, edge.source, junction_ids)
        if src_xy is None:
            continue
        src_section_id = _resolve_source_section_id(graph, edge.source, junction_ids)
        sources.append((src_xy[0], src_xy[1], src_section_id))

    if not sources:
        return

    # Check if any source is cross-column
    my_cols = set(
        range(
            entry_section.grid_col,
            entry_section.grid_col + entry_section.grid_col_span,
        )
    )
    is_cross_column = False
    for _, _, src_sid in sources:
        src_sec = graph.sections.get(src_sid) if src_sid else None
        if src_sec:
            src_cols = set(
                range(src_sec.grid_col, src_sec.grid_col + src_sec.grid_col_span)
            )
            if not (src_cols & my_cols):
                is_cross_column = True
                break

    if is_cross_column:
        # Cross-column: set Y to the closest source level
        src_ys = [y for _, y, _ in sources]
        if port.side == PortSide.TOP:
            target_y = min(src_ys)
        else:
            target_y = max(src_ys)
        # Clamp within bbox
        target_y = max(target_y, entry_section.bbox_y)
        target_y = min(target_y, entry_section.bbox_y + entry_section.bbox_h)
        _set_port_y(graph, port_id, target_y)
        # Only nudge X for LR/RL sections where TOP/BOTTOM ports are perpendicular
        if entry_section.direction in ("LR", "RL"):
            _nudge_port_from_stations(port_id, entry_section, graph)
    else:
        # Same-column: align X with source for vertical drop
        src_x, _, _ = sources[0]
        _set_port_x(graph, port_id, src_x)


def _nudge_port_from_stations(
    port_id: str,
    section: Section,
    graph: MetroGraph,
    tolerance: float = STATION_ELBOW_TOLERANCE,
) -> None:
    """Nudge a TOP/BOTTOM port away from any internal station at the same X.

    Moves the port toward the entry side of the section so it doesn't
    visually pass through a station marker (station-as-elbow).
    """
    station = graph.stations.get(port_id)
    port = graph.ports.get(port_id)
    if not station or not port:
        return

    internal_ids = (
        set(section.station_ids) - set(section.entry_ports) - set(section.exit_ports)
    )
    internal_xs = [
        graph.stations[sid].x
        for sid in internal_ids
        if sid in graph.stations and not graph.stations[sid].is_port
    ]
    if not internal_xs:
        return

    # Check if port X coincides with any internal station X
    if not any(abs(station.x - ix) < tolerance for ix in internal_xs):
        return

    # Move port toward the entry side of the section
    # For LR: entry is left, so move port left (toward bbox_x)
    # For RL: entry is right, so move port right (toward bbox_x + bbox_w)
    if section.direction == "RL":
        new_x = max(internal_xs) + tolerance
        # Clamp within bbox
        new_x = min(new_x, section.bbox_x + section.bbox_w - tolerance)
    else:
        new_x = min(internal_xs) - tolerance
        # Clamp within bbox
        new_x = max(new_x, section.bbox_x + tolerance)

    station.x = new_x
    port.x = new_x


def _align_ports_to_downstream(graph: MetroGraph) -> None:
    """Pull exit-entry port pairs toward downstream station positions.

    After entry ports are aligned to their source (exit port), the
    exit-entry pair may sit at a Y that is far from the downstream
    section's internal stations, forcing lines to detour vertically
    between sections.  This pass moves both ports toward the downstream
    section's average station Y when that would reduce the detour.

    Only applies to non-fold LR/RL sections without fan-out junctions
    (fold/TB sections are handled by ``_align_exit_ports``).
    """
    junction_ids = set(graph.junctions)

    for port_id, port in graph.ports.items():
        if port.is_entry:
            continue

        exit_section = graph.sections.get(port.section_id)
        if not exit_section:
            continue

        # Skip fold/TB sections (handled by _align_exit_ports)
        if exit_section.grid_row_span > 1 or exit_section.direction == "TB":
            continue

        if port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue

        # Find the single target entry port (skip fan-out via junctions)
        target_entry_id: str | None = None
        for edge in graph.edges:
            if edge.source != port_id:
                continue
            if edge.target in junction_ids:
                # Fan-out to junction -- don't override
                target_entry_id = None
                break
            tgt = graph.stations.get(edge.target)
            if tgt and tgt.is_port:
                tgt_port = graph.ports.get(edge.target)
                if tgt_port and tgt_port.is_entry:
                    target_entry_id = edge.target
                    # Keep scanning to detect junctions on later edges

        if not target_entry_id:
            continue

        # Locate the downstream section and its internal stations
        entry_port_obj = graph.ports.get(target_entry_id)
        if not entry_port_obj:
            continue
        entry_section = graph.sections.get(entry_port_obj.section_id)
        if not entry_section:
            continue

        # Skip cross-row connections (different grid rows)
        if exit_section.grid_row != entry_section.grid_row:
            continue

        # Skip when entry port is perpendicular to its section's flow.
        # A LEFT port on a TB section must bend, so aligning it with an
        # internal station's Y would route the line through that station.
        _perp = False
        if entry_section.direction == "TB" and entry_port_obj.side in (
            PortSide.LEFT,
            PortSide.RIGHT,
        ):
            _perp = True
        elif entry_section.direction in ("LR", "RL") and entry_port_obj.side in (
            PortSide.TOP,
            PortSide.BOTTOM,
        ):
            _perp = True
        if _perp:
            continue

        internal_ids = (
            set(entry_section.station_ids)
            - set(entry_section.entry_ports)
            - set(entry_section.exit_ports)
        )
        downstream_ys: list[float] = []
        for edge in graph.edges:
            if edge.source == target_entry_id and edge.target in internal_ids:
                downstream_ys.append(graph.stations[edge.target].y)
        if not downstream_ys:
            continue

        if graph.diamond_style == "straight":
            # Snap to the Y that the most lines target, so the majority
            # of lines flow straight.  Ties broken by topmost (smallest Y).
            y_counts: Counter[float] = Counter(downstream_ys)
            target_y = min(y_counts, key=lambda y: (-y_counts[y], y))
        else:
            target_y = sum(downstream_ys) / len(downstream_ys)

        if graph.center_ports:
            # Centre on the shorter section's midpoint.  Skip when
            # centring would create a V-shaped detour (center_y is
            # outside the range spanned by upstream and downstream Ys),
            # but allow it when both Ys match (no existing detour).
            exit_internal_ids = (
                set(exit_section.station_ids)
                - set(exit_section.entry_ports)
                - set(exit_section.exit_ports)
            )
            upstream_ys: list[float] = []
            for edge in graph.edges:
                if edge.target == port_id and edge.source in exit_internal_ids:
                    upstream_ys.append(graph.stations[edge.source].y)
            if upstream_ys:
                upstream_y = sum(upstream_ys) / len(upstream_ys)
                shorter = min(exit_section, entry_section, key=lambda s: s.bbox_h)
                center_y = shorter.bbox_y + shorter.bbox_h / 2
                lo = min(upstream_y, target_y)
                hi = max(upstream_y, target_y)
                if lo <= center_y <= hi or abs(upstream_y - target_y) < 1.0:
                    target_y = center_y

        # Only move if target_y fits within both section bboxes
        exit_top = exit_section.bbox_y
        exit_bot = exit_section.bbox_y + exit_section.bbox_h
        if not (exit_top <= target_y <= exit_bot):
            continue

        entry_top = entry_section.bbox_y
        entry_bot = entry_section.bbox_y + entry_section.bbox_h
        if not (entry_top <= target_y <= entry_bot):
            continue

        _set_port_y(graph, port_id, target_y)
        _set_port_y(graph, target_entry_id, target_y)


def _snap_sole_layer_stations_to_ports(graph: MetroGraph) -> None:
    """Snap port-connected stations to their port Y when alone in their layer.

    After port alignment, a port may sit at a different Y than its
    connected internal station, producing a diagonal.  When that station
    is the only one at its layer within the section, it can safely move
    to the port Y without colliding with layer-siblings.
    """
    # Build set of section IDs that participated in grid alignment.
    # Phase 10b must not override the shared Y grid for these sections.
    grid_group_secs = _grid_group_section_ids(graph)

    for section in graph.sections.values():
        # Only applies to horizontal (LR/RL) sections where ports
        # are on LEFT/RIGHT and the free axis is Y.
        if section.direction not in ("LR", "RL"):
            continue

        # Skip grid-group sections: their stations are on a shared Y
        # grid and must not be pulled off-grid by port alignment.
        if section.id in grid_group_secs:
            continue

        port_ids = set(section.entry_ports) | set(section.exit_ports)
        internal_ids = set(section.station_ids) - port_ids

        # Skip single-station sections: the station is already centred
        # in the section, and snapping it to a port just drags it to
        # an extreme position.
        if len(internal_ids) <= 1:
            continue

        # Build layer -> set of station IDs for collision checking.
        layer_groups: dict[int, set[str]] = {}
        for sid in internal_ids:
            st = graph.stations.get(sid)
            if st and not st.is_port and hasattr(st, "layer"):
                layer_groups.setdefault(st.layer, set()).add(sid)

        # For each LEFT/RIGHT port, check its connected internal stations.
        # Skip TOP/BOTTOM ports - snapping to a boundary port Y would
        # pull stations to the section edge rather than aligning them
        # with a horizontal predecessor.
        for pid in port_ids:
            port_obj = graph.ports.get(pid)
            if not port_obj or port_obj.side not in (PortSide.LEFT, PortSide.RIGHT):
                continue
            port_st = graph.stations.get(pid)
            if not port_st:
                continue
            port_y = port_st.y

            # Collect the distinct internal stations connected to this port.
            connected: set[str] = set()
            for edge in graph.edges:
                if edge.source == pid and edge.target in internal_ids:
                    connected.add(edge.target)
                elif edge.target == pid and edge.source in internal_ids:
                    connected.add(edge.source)

            # Only snap when exactly one station connects to the port
            # (not a fan-in / fan-out bundle).
            if len(connected) != 1:
                continue

            # Snap the port-connected station if it's a sole layer
            # occupant and has no other predecessors/successors on the
            # port side (otherwise snapping would break those connections).
            current = next(iter(connected))
            is_entry = graph.ports[pid].is_entry

            # Skip if the station has internal predecessors (other than
            # the port).  For entry ports this means the station receives
            # from another source inside the section; for exit ports it
            # means internal stations feed into it, so snapping would
            # create a diagonal on those connections.
            has_internal_pred = any(
                edge.target == current
                and edge.source != pid
                and edge.source in internal_ids
                for edge in graph.edges
            )
            if has_internal_pred:
                continue

            visited: set[str] = set()
            while current and current not in visited:
                visited.add(current)
                st = graph.stations[current]

                layer = getattr(st, "layer", None)
                if layer is None:
                    break
                siblings = layer_groups.get(layer, set())
                if len(siblings) > 1:
                    break

                if abs(st.y - port_y) >= 1.0:
                    st.y = port_y

                # Only continue the chain when center_ports is on.
                if not graph.center_ports:
                    break

                # Follow to the next singleton: successors for entry
                # ports (walking inward), predecessors for exit ports.
                nexts: set[str] = set()
                for edge in graph.edges:
                    if is_entry and edge.source == current:
                        if edge.target in internal_ids:
                            nexts.add(edge.target)
                    elif not is_entry and edge.target == current:
                        if edge.source in internal_ids:
                            nexts.add(edge.source)
                current = next(iter(nexts)) if len(nexts) == 1 else None


def _snap_grid_group_entry_ports(graph: MetroGraph) -> None:
    """Snap entry ports of grid-group sections to their connected station Y.

    Phase 6 aligns entry ports to the upstream junction Y (e.g. the
    midpoint of two exit stations in the source section).  When Phase 10b
    is skipped for grid-group sections, the internal station stays on the
    grid but the port keeps the junction-derived Y, creating a diagonal.

    This step corrects that by moving the port to the Y of its first
    connected non-port station inside the section, giving a straight
    horizontal connection.
    """
    grid_group_secs = _grid_group_section_ids(graph)

    if not grid_group_secs:
        return

    for port_id, port in graph.ports.items():
        if not port.is_entry:
            continue
        if port.section_id not in grid_group_secs:
            continue
        if port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue

        port_st = graph.stations.get(port_id)
        if not port_st:
            continue

        section = graph.sections.get(port.section_id)
        if not section or section.direction not in ("LR", "RL"):
            continue

        # Find the first non-port station connected to this port.
        target_y = None
        for edge in graph.edges:
            if edge.source != port_id:
                continue
            tgt = graph.stations.get(edge.target)
            if tgt and not tgt.is_port and tgt.section_id == section.id:
                target_y = tgt.y
                break

        if target_y is not None and abs(port_st.y - target_y) >= 1.0:
            port_st.y = target_y


def _snap_grid_group_exit_ports(graph: MetroGraph) -> None:
    """Snap exit ports of grid-group sections to their connected station Y.

    Mirrors Phase 10c (entry port snap) for exit ports.  When a
    grid-group section's exit port is at a midpoint between internal
    stations (the default centering), move it to the Y of the connected
    internal station that feeds into it.  This eliminates midpoint
    detours (e.g. get_reference exit at y=340 midpoint instead of y=320
    where get_pcgr sits).

    When multiple internal stations feed the exit port, picks the one
    whose Y is closest to the downstream entry port (if resolvable),
    otherwise picks the nearest to the current port position.
    """
    grid_group_secs = _grid_group_section_ids(graph)

    if not grid_group_secs:
        return

    junction_ids = set(graph.junctions)

    for port_id, port in graph.ports.items():
        if port.is_entry:
            continue
        if port.section_id not in grid_group_secs:
            continue
        if port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue

        port_st = graph.stations.get(port_id)
        if not port_st:
            continue

        section = graph.sections.get(port.section_id)
        if not section or section.direction not in ("LR", "RL"):
            continue

        # Collect internal sources that feed this exit port and the
        # line ids carried by the exit edges in a single edge pass.
        source_ys: list[float] = []
        exit_lines: set[str] = set()
        for edge in graph.edges:
            if edge.target != port_id:
                continue
            exit_lines.add(edge.line_id)
            src = graph.stations.get(edge.source)
            if src and not src.is_port and src.section_id == section.id:
                source_ys.append(src.y)

        if not source_ys:
            continue

        ds_y = _resolve_downstream_entry_y(graph, port_id, junction_ids)

        # Already aligned with downstream entry: straight run is correct
        # even if internal sources sit at a different Y.
        if ds_y is not None and abs(port_st.y - ds_y) < 1.0:
            continue

        unique_source_ys = sorted(set(source_ys))
        spread = unique_source_ys[-1] - unique_source_ys[0]
        n_unique = len(unique_source_ys)

        if n_unique == 1 or spread <= 1.0:
            target_y = source_ys[0]
        else:
            # Multi-Y sources: snap onto the source that aligns with the
            # downstream entry so the inter-section run stays horizontal.
            # For 3+ sources this overrides the default centered merge
            # only when the exit carries a multi-line bundle (parallel-
            # redundant rather than a true fan-in).
            if ds_y is None or (n_unique >= 3 and len(exit_lines) < 2):
                continue
            match = next((y for y in unique_source_ys if abs(y - ds_y) < 1.0), None)
            if match is None:
                continue
            target_y = match

        if abs(port_st.y - target_y) >= 1.0:
            port_st.y = target_y


def _resolve_downstream_entry_y(
    graph: MetroGraph,
    exit_port_id: str,
    junction_ids: set[str],
) -> float | None:
    """Resolve the downstream entry port Y reachable from an exit port.

    Handles two patterns:
    - Direct: exit_port -> entry_port
    - Via junction: exit_port -> junction -> entry_port(s)

    Returns the nearest downstream entry port Y, or None if not found.
    """
    port_st = graph.stations.get(exit_port_id)
    if not port_st:
        return None

    entry_ys: list[float] = []
    for edge in graph.edges:
        if edge.source != exit_port_id:
            continue
        # Direct exit -> entry connection
        dp = graph.ports.get(edge.target)
        if dp and dp.is_entry:
            ds_st = graph.stations.get(edge.target)
            if ds_st:
                entry_ys.append(ds_st.y)
            continue
        # Via junction
        if edge.target in junction_ids:
            for e2 in graph.edges:
                if e2.source != edge.target:
                    continue
                dp2 = graph.ports.get(e2.target)
                if dp2 and dp2.is_entry:
                    ds_st = graph.stations.get(e2.target)
                    if ds_st:
                        entry_ys.append(ds_st.y)

    if entry_ys:
        return min(entry_ys, key=lambda y: abs(y - port_st.y))
    return None


def _align_exit_ports(graph: MetroGraph) -> None:
    """Align LEFT/RIGHT exit ports on fold sections with their target's Y.

    Applies to sections with grid_row_span > 1 OR TB direction (fold bridges).
    These have exit ports placed near the section bottom, but the target
    section's entry may be at a different Y. Aligning ensures a straight
    horizontal inter-section connection.
    """
    junction_ids = set(graph.junctions)

    for port_id, port in graph.ports.items():
        if port.is_entry:
            continue

        exit_section = graph.sections.get(port.section_id)
        if not exit_section:
            continue
        if exit_section.grid_row_span <= 1 and exit_section.direction != "TB":
            continue

        if port.side in (PortSide.LEFT, PortSide.RIGHT):
            _align_lr_exit_port(graph, port_id, port, exit_section, junction_ids)


def _align_lr_exit_port(
    graph: MetroGraph,
    port_id: str,
    port,
    exit_section: Section,
    junction_ids: set[str],
) -> None:
    """Align a LEFT/RIGHT exit port's Y with its target entry port."""
    for edge in graph.edges:
        if edge.source != port_id:
            continue
        tgt = graph.stations.get(edge.target)
        if not tgt:
            continue

        # Don't align with fan-out junctions
        if edge.target in junction_ids:
            break

        if not tgt.is_port:
            continue

        # Don't align with perpendicular target ports (cross-axis)
        tgt_port_obj = graph.ports.get(tgt.id)
        if tgt_port_obj and tgt_port_obj.side in (PortSide.TOP, PortSide.BOTTOM):
            break

        # Don't pull exit port outside its section bbox
        bbox_top = exit_section.bbox_y
        bbox_bot = exit_section.bbox_y + exit_section.bbox_h
        if not (bbox_top <= tgt.y <= bbox_bot):
            break

        if exit_section.direction == "TB":
            tgt_y = _resolve_tb_exit_y(graph, port, tgt, exit_section)
        else:
            tgt_y = tgt.y

        _set_port_y(graph, port_id, tgt_y)
        break


def _resolve_tb_exit_y(
    graph: MetroGraph,
    port,
    tgt: Station,
    exit_section: Section,
) -> float:
    """Resolve the Y coordinate for a TB section's exit port.

    Mirrors the entry-side gap: finds how far the perpendicular entry
    port sits above the first internal station, and places the exit port
    the same distance below the last internal station. Pushes the target
    section down if needed so the inter-section line is straight.
    """
    internal_ys = [
        graph.stations[sid].y
        for sid in exit_section.station_ids
        if sid in graph.stations and not graph.stations[sid].is_port
    ]
    last_y = max(internal_ys) if internal_ys else port.y
    first_y = min(internal_ys) if internal_ys else port.y

    # Mirror the entry-side gap (distance from entry port to first station)
    entry_gap = MIN_PORT_STATION_GAP
    for pid in exit_section.entry_ports:
        ep = graph.ports.get(pid)
        if ep and ep.side in (PortSide.LEFT, PortSide.RIGHT):
            entry_gap = max(entry_gap, first_y - graph.stations[pid].y)
            break

    # Ensure the gap below the last station is large enough for the
    # exit corner curve (CURVE_RADIUS) plus a straight run so the
    # curve doesn't crowd the station pill.
    min_exit_gap = max(entry_gap, CURVE_RADIUS + MIN_PORT_STATION_GAP)
    min_exit_y = last_y + min_exit_gap
    if tgt.y >= min_exit_y:
        tgt_y = tgt.y
    else:
        # Push target section down to align with exit port
        tgt_y = min_exit_y
        delta = tgt_y - tgt.y

        tgt.y = tgt_y
        tgt_port = graph.ports.get(tgt.id)
        if tgt_port:
            tgt_port.y = tgt_y
            tgt_sec = graph.sections.get(tgt_port.section_id)
            if tgt_sec:
                for sid in tgt_sec.station_ids:
                    s = graph.stations.get(sid)
                    if s and s.id != tgt.id:
                        s.y += delta
                        p = graph.ports.get(sid)
                        if p:
                            p.y += delta
                tgt_sec.bbox_y += delta

    # Extend exit section bbox so padding below the exit port
    # mirrors the padding above the entry port.
    entry_port_y = None
    for pid in exit_section.entry_ports:
        ep = graph.ports.get(pid)
        if ep and ep.side in (PortSide.LEFT, PortSide.RIGHT):
            entry_port_y = graph.stations[pid].y
            break
    if entry_port_y is not None:
        top_pad = entry_port_y - exit_section.bbox_y
        desired_bot = tgt_y + top_pad
        current_bot = exit_section.bbox_y + exit_section.bbox_h
        if desired_bot > current_bot:
            exit_section.bbox_h = desired_bot - exit_section.bbox_y

    return tgt_y


def _clamp_tb_entry_port(
    graph: MetroGraph,
    entry_section: Section,
    target_y: float,
    edge: Edge,
    src: Station,
    junction_ids: set[str],
) -> float:
    """Clamp a TB section's perpendicular entry port above internal stations.

    The entry port must stay above the first internal station so the
    direction-change curve has room. When clamped, also pulls the source
    station/junction up to maintain a straight horizontal run.

    Returns the (possibly clamped) target_y.
    """
    internal_ids = (
        set(entry_section.station_ids)
        - set(entry_section.entry_ports)
        - set(entry_section.exit_ports)
    )
    internal_ys = [
        graph.stations[sid].y
        for sid in internal_ids
        if sid in graph.stations and not graph.stations[sid].is_port
    ]
    if not internal_ys:
        return target_y

    first_y = min(internal_ys)
    max_y = first_y - MIN_PORT_STATION_GAP
    if target_y <= max_y:
        return target_y

    # Prefer the topmost source-side station feeding the exit port
    # so that line exits horizontally.
    exit_pid = edge.source
    if edge.source in junction_ids:
        for e2 in graph.edges:
            if e2.target == edge.source:
                ep = graph.stations.get(e2.source)
                if ep and ep.is_port:
                    exit_pid = e2.source
                    break

    top_src_y = None
    for e3 in graph.edges:
        if e3.target == exit_pid:
            s3 = graph.stations.get(e3.source)
            if s3 and not s3.is_port and e3.source not in junction_ids:
                if top_src_y is None or s3.y < top_src_y:
                    top_src_y = s3.y

    if top_src_y is not None and top_src_y < max_y:
        target_y = top_src_y
    else:
        target_y = max_y

    # Pull source up to maintain straight horizontal run
    src.y = target_y
    if src.is_port and edge.source in graph.ports:
        graph.ports[edge.source].y = target_y
    # If source is a junction, also pull the exit port feeding it
    if edge.source in junction_ids:
        for e2 in graph.edges:
            if e2.target == edge.source:
                ep = graph.stations.get(e2.source)
                if ep and ep.is_port:
                    ep.y = target_y
                    if e2.source in graph.ports:
                        graph.ports[e2.source].y = target_y

    return target_y


def _space_ports_from_termini(
    graph: MetroGraph,
    y_spacing: float,
) -> None:
    """Push ports away from terminus stations so there is a full row gap.

    After port alignment, an entry or exit port may sit very close to a
    terminus station in the same section.  Lines routed from that port
    then overlap the terminus file icon.

    Only entry ports are checked against entry-side (source) termini, and
    exit ports against exit-side (sink) termini, to avoid displacing
    ports on the opposite side of the section.

    Exit ports on fold sections (grid_row_span > 1 or TB direction) are
    skipped because ``_align_exit_ports`` will overwrite them.
    """
    # Pre-compute edge adjacency (used to identify direct connections
    # and to propagate port moves across section boundaries).
    adjacency: dict[str, set[str]] = {}
    successors: dict[str, set[str]] = {}
    predecessors: dict[str, set[str]] = {}
    for edge in graph.edges:
        adjacency.setdefault(edge.source, set()).add(edge.target)
        adjacency.setdefault(edge.target, set()).add(edge.source)
        successors.setdefault(edge.source, set()).add(edge.target)
        predecessors.setdefault(edge.target, set()).add(edge.source)

    for section in graph.sections.values():
        entry_port_ids = set(section.entry_ports)
        exit_port_ids = set(section.exit_ports)
        all_port_ids = entry_port_ids | exit_port_ids
        real_sids = {s for s in section.station_ids if s not in all_port_ids}

        # Skip exit ports on fold sections -- _align_exit_ports handles them.
        is_fold = section.grid_row_span > 1 or section.direction == "TB"

        # Classify termini by side.  A station with no in-section
        # predecessors is an entry-side (source) terminus; one with no
        # in-section successors is an exit-side (sink) terminus.  A
        # station can be both (isolated within the section), but we only
        # add it to entry_termini to avoid conflicting pushes from both
        # the entry and exit port passes.
        entry_termini: list[tuple[str, float]] = []
        exit_termini: list[tuple[str, float]] = []
        for sid in real_sids:
            st = graph.stations.get(sid)
            if not st or not st.is_terminus or st.is_port:
                continue
            preds = predecessors.get(sid, set())
            succs = successors.get(sid, set())
            is_source = not (preds & real_sids)
            is_sink = not (succs & real_sids)
            if is_source:
                entry_termini.append((sid, st.y))
            elif is_sink:
                # Only classify as exit terminus if not already an
                # entry terminus (avoids double-counting isolated nodes).
                exit_termini.append((sid, st.y))

        _push_ports_from_termini(
            graph,
            sorted(entry_port_ids),
            entry_termini,
            section,
            adjacency,
            predecessors,
            y_spacing,
        )
        if not is_fold:
            _push_ports_from_termini(
                graph,
                sorted(exit_port_ids),
                exit_termini,
                section,
                adjacency,
                predecessors,
                y_spacing,
            )


def _push_ports_from_termini(
    graph: MetroGraph,
    port_ids: list[str],
    termini: list[tuple[str, float]],
    section: Section,
    adjacency: dict[str, set[str]],
    predecessors: dict[str, set[str]],
    y_spacing: float,
) -> None:
    """Ensure *y_spacing* between each port and non-connected termini.

    The strategy depends on how the port connects across sections:

    - **Junction link** (fan-out): move the port and propagate through
      the junction to its *upstream* (predecessor) port only, keeping
      the exit-junction-entry chain straight without disturbing other
      fan-out targets.
    - **Direct port-to-port link** (no junction): moving the port would
      cascade to the other section and mis-align its internal stations.
      Instead, push the conflicting *terminus* away from the port.
    - **No cross-section link**: move the port freely.

    *port_ids* must be a sorted list so that results are deterministic
    when multiple ports in the same section conflict with the same
    terminus.
    """
    junction_ids = set(graph.junctions)
    section_port_set = set(port_ids)

    for pid in port_ids:
        port_st = graph.stations.get(pid)
        if not port_st:
            continue
        port_obj = graph.ports.get(pid)
        assert port_obj is not None, f"port {pid} missing from graph.ports"
        neighbours = adjacency.get(pid, set())

        # Classify cross-section connection type.
        has_junction = bool(neighbours & junction_ids)
        has_direct_port = False
        if not has_junction:
            for nb in neighbours:
                if nb in graph.ports and nb not in section_port_set:
                    has_direct_port = True
                    break

        # Collect all termini that are too close and not directly
        # connected to this port.
        conflict_ids: list[str] = []
        conflict_ys: list[float] = []
        for tid, ty in termini:
            if tid in neighbours:
                continue
            if abs(port_st.y - ty) < y_spacing:
                conflict_ids.append(tid)
                conflict_ys.append(ty)

        if not conflict_ys:
            continue

        if has_direct_port:
            # Move the terminus instead of the port so the
            # inter-section line stays straight.
            _push_termini_from_port(graph, conflict_ids, port_st.y, section, y_spacing)
            continue

        # Compute the single best Y that satisfies all conflicts.
        above_candidates = [ty - y_spacing for ty in conflict_ys]
        below_candidates = [ty + y_spacing for ty in conflict_ys]

        best_above = min(above_candidates)
        best_below = max(below_candidates)

        dist_above = abs(port_st.y - best_above)
        dist_below = abs(port_st.y - best_below)
        # Ties go above (smaller Y) to keep ports near the top.
        new_y = best_above if dist_above <= dist_below else best_below

        port_st.y = new_y
        port_obj.y = new_y

        # Propagate through junctions so inter-section lines stay straight.
        _propagate_through_junctions(
            graph,
            pid,
            new_y,
            neighbours,
            junction_ids,
            predecessors,
        )

        # Grow this section's bbox to contain the moved port.
        _expand_bbox_for_y(section, new_y)


def _propagate_through_junctions(
    graph: MetroGraph,
    origin_pid: str,
    new_y: float,
    neighbours: set[str],
    junction_ids: set[str],
    predecessors: dict[str, set[str]],
) -> None:
    """Move connected junctions and their upstream exit ports to *new_y*.

    Only propagates to the junction's upstream (predecessor) ports, not
    to other fan-out targets (entry ports to other sections).
    """
    for nb in neighbours:
        if nb not in junction_ids:
            continue
        nb_st = graph.stations.get(nb)
        if not nb_st:
            continue

        nb_st.y = new_y
        for jnb in predecessors.get(nb, set()):
            if jnb == origin_pid:
                continue
            jnb_st = graph.stations.get(jnb)
            if not jnb_st or not jnb_st.is_port:
                continue
            jnb_st.y = new_y
            jnb_obj = graph.ports.get(jnb)
            if jnb_obj:
                jnb_obj.y = new_y
                jnb_sec = graph.sections.get(jnb_obj.section_id)
                if jnb_sec:
                    _expand_bbox_for_y(jnb_sec, new_y)


def _push_termini_from_port(
    graph: MetroGraph,
    terminus_ids: list[str],
    port_y: float,
    section: Section,
    y_spacing: float,
) -> None:
    """Push terminus stations to the nearest station row that clears the port.

    Instead of placing the terminus at the arbitrary ``port_y ± y_spacing``,
    snap it to an existing station Y in the section that satisfies the
    minimum clearance.  This keeps the terminus aligned with an actual
    track row rather than floating at an unrelated Y coordinate.
    """
    # Collect existing station Y values in the section (excluding ports
    # and the termini being moved) as candidate snap targets.
    port_ids = set(section.entry_ports) | set(section.exit_ports)
    tid_set = set(terminus_ids)
    section_ys: set[float] = set()
    for sid in section.station_ids:
        if sid in port_ids or sid in tid_set:
            continue
        st = graph.stations.get(sid)
        if st and not st.is_port:
            section_ys.add(st.y)

    for tid in terminus_ids:
        t_st = graph.stations.get(tid)
        if not t_st:
            continue

        # Determine push direction
        going_down = t_st.y > port_y

        # Find the nearest section row Y that satisfies clearance
        candidates = sorted(
            (y for y in section_ys if abs(y - port_y) >= y_spacing),
            key=lambda y: abs(y - t_st.y),
        )
        if going_down:
            candidates = [y for y in candidates if y >= port_y + y_spacing]
        else:
            candidates = [y for y in candidates if y <= port_y - y_spacing]

        if candidates:
            new_y = candidates[0]
        else:
            # No existing row satisfies clearance; fall back to offset.
            new_y = (port_y + y_spacing) if going_down else (port_y - y_spacing)

        t_st.y = new_y
        _expand_bbox_for_y(section, new_y)


def _expand_bbox_for_y(section: Section, y: float) -> None:
    """Expand *section*'s bbox so *y* sits inside with padding."""
    pad = SECTION_Y_PADDING
    top = section.bbox_y
    bot = section.bbox_y + section.bbox_h
    if y - pad < top:
        section.bbox_h += top - (y - pad)
        section.bbox_y = y - pad
    elif y + pad > bot:
        section.bbox_h = (y + pad) - section.bbox_y


def _build_section_subgraph(graph: MetroGraph, section: Section) -> MetroGraph:
    """Build a temporary MetroGraph containing only a section's real stations and edges.

    Excludes port stations and any edges that touch ports. Ports are positioned
    separately on section boundaries after the internal layout is computed.
    """
    sub = MetroGraph()
    sub.lines = graph.lines  # Share line definitions
    sub.diamond_style = graph.diamond_style

    # Collect port IDs for this section
    port_ids = set(section.entry_ports) | set(section.exit_ports)

    # Add only real (non-port) stations belonging to this section
    real_station_ids: set[str] = set()
    for sid in section.station_ids:
        if sid in port_ids:
            continue
        if sid in graph.stations:
            station = graph.stations[sid]
            if station.is_port:
                continue
            sub.add_station(
                Station(
                    id=station.id,
                    label=station.label,
                    section_id=station.section_id,
                    is_port=False,
                    terminus_labels=list(station.terminus_labels),
                )
            )
            real_station_ids.add(sid)

    # Add only edges between real stations (no port-touching edges)
    for edge in graph.edges:
        if edge.source in real_station_ids and edge.target in real_station_ids:
            sub.add_edge(
                Edge(
                    source=edge.source,
                    target=edge.target,
                    line_id=edge.line_id,
                )
            )

    return sub


def _insert_phantom_pass_throughs(
    graph: MetroGraph,
    section: Section,
    sub: MetroGraph,
) -> None:
    """Insert phantom stations into *sub* so deep-entry lines get own tracks.

    When a line enters a section via an entry port but its first internal
    station is deeper than layer 0, the line would share a track with
    unrelated stations at the early layers.  Adding a hidden phantom at
    layer 0 gives the line a dedicated track for a clear horizontal runway.

    Only modifies the temporary subgraph -- the main graph stays immutable.
    """
    if not sub.stations:
        return

    from nf_metro.layout.layers import assign_layers

    layers = assign_layers(sub)
    if not layers:
        return
    min_layer = min(layers.values())

    entry_port_ids = set(section.entry_ports)

    # Find lines entering from entry ports to deep-layer internal stations.
    entry_targets: dict[str, set[str]] = {}
    for edge in graph.edges:
        if edge.source in entry_port_ids and edge.target in sub.stations:
            entry_targets.setdefault(edge.line_id, set()).add(edge.target)

    for line_id, targets in entry_targets.items():
        target_layers = [layers.get(t, min_layer) for t in targets]
        if all(ly > min_layer for ly in target_layers):
            earliest_target = min(targets, key=lambda t: layers.get(t, 0))
            phantom_id = f"_phantom_{section.id}_{line_id}"

            sub.add_station(
                Station(
                    id=phantom_id,
                    label="",
                    section_id=section.id,
                    is_hidden=True,
                )
            )
            sub.add_edge(
                Edge(source=phantom_id, target=earliest_target, line_id=line_id)
            )


def _align_phantom_pass_throughs(
    sub: MetroGraph,
    tracks: dict[str, float],
) -> None:
    """Snap convergence nodes to their phantom pass-through's track.

    The phantom ensures a dedicated track for the bypassing line.
    Moving the convergence node (the phantom's sole successor) to that
    track keeps the trunk horizontal so the optional branch visually
    "bubbles" away from it.
    """
    import networkx as nx

    G = nx.DiGraph()
    for edge in sub.edges:
        G.add_edge(edge.source, edge.target)

    for sid, station in sub.stations.items():
        if not station.is_hidden or sid not in tracks or sid not in G:
            continue
        succs = list(G.successors(sid))
        if len(succs) == 1 and succs[0] in tracks:
            tracks[succs[0]] = tracks[sid]


def _compute_fork_join_gaps(
    sub: MetroGraph,
    layers: dict[str, int],
    tracks: dict[str, float],
    x_spacing: float,
    full_graph: MetroGraph | None = None,
    section_station_ids: set[str] | None = None,
) -> dict[int, float]:
    """Compute extra X offset per layer at fork/join points.

    Adds a fractional gap after fork layers (where tracks diverge) and
    before join layers (where tracks converge) so labels aren't obscured
    by diagonal crossings.

    When full_graph and section_station_ids are provided, fork/join
    detection uses all edges within the section (including port-touching
    edges). This catches divergences where a station connects to both
    internal stations and exit ports.

    In single-track sections (all stations on the same Y), port-bound
    divergences are suppressed because there are no diagonal transitions
    and the extra spacing is purely wasteful.
    """
    from collections import defaultdict

    out_targets: dict[str, set[str]] = defaultdict(set)
    in_sources: dict[str, set[str]] = defaultdict(set)

    # Use full graph edges for fork/join detection when available,
    # so that edges to/from port stations are counted as divergences.
    if full_graph is not None and section_station_ids is not None:
        for edge in full_graph.edges:
            src_in = edge.source in section_station_ids
            tgt_in = edge.target in section_station_ids
            if src_in and tgt_in:
                out_targets[edge.source].add(edge.target)
                in_sources[edge.target].add(edge.source)
    else:
        for edge in sub.edges:
            out_targets[edge.source].add(edge.target)
            in_sources[edge.target].add(edge.source)

    # Only count forks/joins that span multiple tracks (requiring a
    # diagonal routing transition).  Same-track fan-outs (e.g. a station
    # connecting to both an internal successor and an exit port on the
    # same Y) don't need extra horizontal room.
    #
    # Port stations aren't in ``tracks`` (they're positioned later), so
    # treat them conservatively: if any participant is missing from
    # tracks, assume it may be on a different track and count the
    # fork/join.
    #
    # Exception for **forks** in single-track sections: exit-side ports
    # sit at the far section boundary, so the diagonal from the fork
    # station has ample horizontal room without extra layer spacing.
    # Join gaps are kept even in single-track sections because entry
    # ports are close to the first internal station, and the diagonal
    # from a different-Y entry needs the extra room.
    all_section_tracks = set(tracks.values())
    is_single_track = len(all_section_tracks) <= 1

    fork_layers: set[int] = set()
    for sid, targets in out_targets.items():
        if len(targets) > 1 and sid in layers:
            if any(t not in tracks for t in targets):
                if not is_single_track:
                    fork_layers.add(layers[sid])
            else:
                target_tracks = {tracks[t] for t in targets}
                if len(target_tracks) > 1:
                    fork_layers.add(layers[sid])

    join_layers: set[int] = set()
    for sid, sources in in_sources.items():
        if len(sources) > 1 and sid in layers:
            if any(s not in tracks for s in sources):
                join_layers.add(layers[sid])
            else:
                source_tracks = {tracks[s] for s in sources}
                if len(source_tracks) > 1:
                    join_layers.add(layers[sid])

    if not fork_layers and not join_layers:
        return {}

    max_layer = max(layers.values()) if layers else 0
    base_gap = x_spacing * EXIT_GAP_MULTIPLIER

    # Compute per-layer gap scaled by label width at fork/join stations.
    # The gap must be large enough that the diagonal transition starts
    # past the label text and still has room for the transition itself.
    #
    # For multi-target forks / multi-source joins, bubble station
    # centering is skipped in routing, so the flat run at the bubble
    # end can be very short.  When bubble stations sit on different
    # tracks from the fork/join and have wide labels, add extra space
    # so the flat run accommodates them.
    layer_gap: dict[int, float] = {}
    for layer in fork_layers | join_layers:
        fj_label_half = 0.0
        fj_tracks: set[float] = set()
        for sid, lyr in layers.items():
            if lyr == layer:
                station = sub.stations.get(sid)
                if station and station.label.strip():
                    label_half = label_text_width(station.label) / 2
                    fj_label_half = max(fj_label_half, label_half)
                if sid in tracks:
                    fj_tracks.add(tracks[sid])

        # Check adjacent bubble layer for off-track stations with
        # wide labels.  Only applies for wide fan-outs (3+ off-track
        # targets/sources) where bubble station centering is skipped
        # in routing and middle stations must have inside labels.
        bubble_label_half = 0.0
        is_wide_fork = False
        is_wide_join = False
        if layer in fork_layers:
            for sid, tgts in out_targets.items():
                if layers.get(sid) == layer and sid in tracks:
                    off_track = sum(
                        1 for t in tgts if t in tracks and tracks[t] != tracks[sid]
                    )
                    if off_track >= 3:
                        is_wide_fork = True
                        break
        if layer in join_layers:
            for sid, srcs in in_sources.items():
                if layers.get(sid) == layer and sid in tracks:
                    off_track = sum(
                        1 for s in srcs if s in tracks and tracks[s] != tracks[sid]
                    )
                    if off_track >= 3:
                        is_wide_join = True
                        break
        if is_wide_fork:
            for sid, lyr in layers.items():
                if lyr == layer + 1 and sid in tracks and tracks[sid] not in fj_tracks:
                    station = sub.stations.get(sid)
                    if station and station.label.strip():
                        bubble_label_half = max(
                            bubble_label_half, label_text_width(station.label) / 2
                        )
        if is_wide_join:
            for sid, lyr in layers.items():
                if lyr == layer - 1 and sid in tracks and tracks[sid] not in fj_tracks:
                    station = sub.stations.get(sid)
                    if station and station.label.strip():
                        bubble_label_half = max(
                            bubble_label_half, label_text_width(station.label) / 2
                        )

        # The bubble station is centered on its flat run.  The total
        # space needed is 2 * label_half + DIAGONAL_RUN, but the gap
        # is added on BOTH sides (after fork, before join), so each
        # side contributes half the total requirement.
        bubble_extra = max(
            0.0, (bubble_label_half * 2 + DIAGONAL_RUN - x_spacing) / 1.5
        )
        layer_gap[layer] = max(base_gap, fj_label_half + bubble_extra)

    cumulative = 0.0
    layer_extra: dict[int, float] = {}
    for layer in range(max_layer + 1):
        # Add gap before join layers
        if layer in join_layers:
            cumulative += layer_gap.get(layer, base_gap)
        layer_extra[layer] = cumulative
        # Add gap after fork layers
        if layer in fork_layers:
            cumulative += layer_gap.get(layer, base_gap)

    return layer_extra


def _lift_off_track_stations(
    graph: MetroGraph,
    y_spacing: float,
    section_y_padding: float,
) -> None:
    """Lift off_track stations above their section's topmost line track.

    Off-track stations are file-input nodes that should not consume a
    line-track Y slot.  This phase moves each marked station's Y to a
    new "input band" above the topmost non-off-track station in its
    section, then expands the section bbox upward to fit them.  When
    multiple off-track stations sit at the same X (the normal case for
    sources stacked on layer 0), they are spread horizontally along
    the band so file icons don't overlap.

    Same-section ports already at the top edge are shifted with the
    bbox so their entry coordinates remain on the boundary.
    """
    junction_ids = set(graph.junctions)

    # Group off_track stations by section
    by_section: dict[str, list[Station]] = defaultdict(list)
    for sid, st in graph.stations.items():
        if not st.off_track or st.is_port or sid in junction_ids:
            continue
        if not st.section_id:
            continue
        by_section[st.section_id].append(st)

    # Band step matches y_spacing so each input gets a full slot
    # (matches original station spacing, leaving room for label text).
    # Inputs stay at their original X (layer 0 for sources) but stack
    # vertically in a band above the topmost line track so file icons
    # no longer share Y with the study-type lanes.
    BAND_STEP = y_spacing

    for sec_id, off_stations in by_section.items():
        section = graph.sections.get(sec_id)
        if not section:
            continue

        # Topmost Y of non-off-track real stations in the section
        anchor_ys = [
            graph.stations[sid].y
            for sid in section.station_ids
            if sid in graph.stations
            and not graph.stations[sid].is_port
            and not graph.stations[sid].off_track
            and sid not in junction_ids
        ]
        if not anchor_ys:
            continue
        top_y = min(anchor_ys)

        # Group inputs by X column (layer): each column gets its own
        # vertical stack within the input band, preserving original
        # Y order so e.g. Samples ends up above Contrasts above Matrix.
        by_col: dict[float, list[Station]] = defaultdict(list)
        for st in off_stations:
            by_col[round(st.x, 1)].append(st)
        for stations in by_col.values():
            stations.sort(key=lambda s: s.y)

        # Lowest slot in the band sits one y_spacing above top_y so
        # there's room for the icon plus clearance from the topmost
        # line track.  Higher slots stack upward in BAND_STEP units.
        max_per_col = max(len(v) for v in by_col.values())
        band_bottom = top_y - y_spacing
        band_top = band_bottom - (max_per_col - 1) * BAND_STEP

        for stations in by_col.values():
            # Distribute n stations across the band, topmost first
            for i, st in enumerate(stations):
                st.y = band_top + i * BAND_STEP

        # Expand section bbox upward so the band + label clearance
        # fits inside the section box.
        new_bbox_top = band_top - section_y_padding
        if new_bbox_top < section.bbox_y:
            delta = section.bbox_y - new_bbox_top
            section.bbox_y = new_bbox_top
            section.bbox_h += delta
            # Shift TOP ports back to the (new) top edge so they stay
            # on the boundary.  BOTTOM ports stay put because bbox_h
            # only grew upward.
            for pid in section.entry_ports + section.exit_ports:
                port = graph.ports.get(pid)
                port_st = graph.stations.get(pid)
                if not port or not port_st:
                    continue
                if port.side == PortSide.TOP:
                    port_st.y = section.bbox_y
                    port.y = port_st.y

    if not by_section:
        return

    # Phase 3b ran before our lift, so y_offset doesn't account for the
    # new bbox tops.  Shift the whole graph down so the topmost section
    # sits inside the canvas with the standard margin.
    min_top = min(s.bbox_y for s in graph.sections.values() if s.bbox_h > 0)
    if min_top < section_y_padding:
        shift = section_y_padding - min_top
        for st in graph.stations.values():
            st.y += shift
        for section in graph.sections.values():
            section.bbox_y += shift
        for port in graph.ports.values():
            port.y += shift
