"""Align trunk Ys and bboxes across sections sharing a serpentine row."""

from __future__ import annotations

import math
from collections import defaultdict

from nf_metro.layout.constants import (
    FONT_HEIGHT,
    LABEL_OFFSET,
    SAME_COORD_TOLERANCE,
    STATION_RADIUS_APPROX,
    resolve_offset_step,
)
from nf_metro.layout.geometry import (
    lanes_run_along_x,
    lanes_run_along_y,
    shift_section,
)
from nf_metro.layout.labels import active_font_scale
from nf_metro.layout.phases._common import (
    _classify_multi_station_ys,
    _classify_section_station_ys,
    _max_stations_per_layer,
    _pull_section_ports_to_edge,
    _row_contiguous_column_groups,
    _section_bundle_lines,
    _section_trunk_y,
    iter_stacked_rows_in_rowspan_band,
)
from nf_metro.layout.phases.ports import _set_port_y
from nf_metro.layout.phases.single_section import _multiline_label_padding
from nf_metro.parser.model import MetroGraph, PortSide, RowGridInfo, Section


def _group_sections_by_row(
    graph: MetroGraph,
    section_subgraphs: dict[str, MetroGraph],
    row_assign: dict[str, int],
) -> dict[tuple[int, str], list[str]]:
    """Group lane-on-Y sections by their (grid row, direction).

    Vertical-flow (TB/BT) sections separate their lines along X, not Y, so
    they share no row Y-grid and are left out of the grouping.
    """
    groups: dict[tuple[int, str], list[str]] = defaultdict(list)
    for sec_id in section_subgraphs:
        section = graph.sections[sec_id]
        row = row_assign.get(sec_id, -1)
        if row < 0 or not lanes_run_along_y(section.direction):
            continue
        groups[(row, section.direction)].append(sec_id)
    return groups


def _row_group_grid_spacing(
    graph: MetroGraph,
    section_subgraphs: dict[str, MetroGraph],
    sec_ids: list[str],
    section_y_padding: float,
    y_spacing: float,
) -> (
    tuple[int, float, float, dict[str, tuple[dict[int, list[float]], set[float]]]]
    | None
):
    """Shared grid params for a row group, plus its per-section Y classification.

    Returns ``(grid_slots, max_y_pad, effective_y_spacing, section_class)``, or
    ``None`` when the group needs no shared grid (one slot or fewer) -- bailing
    before the per-section classification so a skipped group does no extra work.
    The pitch is inflated past ``y_spacing`` when stations at multi-station
    layers carry enough lines that their rendered bundle plus label height
    would otherwise overlap an adjacent track.  Isolated hub stations (sole
    layer occupant) are excluded so they don't inflate the whole row.
    """
    grid_slots = 0
    for sec_id in sec_ids:
        grid_slots = max(grid_slots, _max_stations_per_layer(section_subgraphs[sec_id]))
    if grid_slots <= 1:
        return None

    section_class = {
        sec_id: _classify_multi_station_ys(section_subgraphs[sec_id])
        for sec_id in sec_ids
    }

    max_y_pad = 0.0
    for sec_id in sec_ids:
        y_pad = section_y_padding + _multiline_label_padding(section_subgraphs[sec_id])
        max_y_pad = max(max_y_pad, y_pad)

    max_lines = 0
    for sec_id in sec_ids:
        sub = section_subgraphs[sec_id]
        multi_ys = section_class[sec_id][1]
        for st in sub.stations.values():
            if not st.is_port and st.y in multi_ys:
                max_lines = max(max_lines, len(graph.station_lines(st.id)))
    offset_step = resolve_offset_step(graph.track_gap)
    min_track_gap = (
        (max_lines - 1) * offset_step
        + 2 * STATION_RADIUS_APPROX
        + LABEL_OFFSET
        + FONT_HEIGHT * active_font_scale()
    )
    effective_y_spacing = max(y_spacing, min_track_gap)
    return grid_slots, max_y_pad, effective_y_spacing, section_class


def _separate_branches_across_trunk(
    must_separate: set[tuple[float, float]],
    layer_stations: dict[int, list[float]],
    remap_ys: list[float],
) -> None:
    """Force a symmetric diamond's branches onto slots either side of its trunk.

    A same-layer branch pair (already in *must_separate*) placed symmetrically
    about a higher-span trunk row -- one recurring across more layers, the
    fork's through-line bracketed at the pair's midpoint -- must keep all three
    rows on distinct, ordered slots.  Without this the narrower of two fans
    sharing a section can let its lower branch collapse onto the trunk slot,
    breaking a symmetric diamond.

    Gated on the trunk sitting at the pair's midpoint so only symmetric
    diamonds are affected: an asymmetric fork (one branch already on the trunk)
    is left free to share a slot, as is a row that merely falls between a wider
    fan's pair.
    """
    layer_span: dict[float, int] = defaultdict(int)
    for ys_at_layer in layer_stations.values():
        for y in set(ys_at_layer):
            layer_span[y] += 1
    for a, b in list(must_separate):
        midpoint = (a + b) / 2
        for m in remap_ys:
            if (
                a < m < b
                and abs(m - midpoint) <= SAME_COORD_TOLERANCE
                and layer_span[m] > layer_span[a]
                and layer_span[m] > layer_span[b]
            ):
                must_separate.add((a, m))
                must_separate.add((m, b))


def _assign_nonuniform_slots(
    layer_stations: dict[int, list[float]],
    multi_layer_ys: set[float],
    remap_ys: list[float],
    effective_y_spacing: float,
    has_diamond: bool,
    symmetric_diamonds: bool,
) -> dict[float, float]:
    """Map non-uniformly-spaced Y values to grid slots.

    Y values that co-occur at the same layer are forced onto different slots
    (checked against every value already in the candidate slot, so non-adjacent
    same-layer pairs are caught).  A diamond hub keeps at least a 2-slot gap.
    """
    must_separate: set[tuple[float, float]] = set()
    for ys_at_layer in layer_stations.values():
        unique_ys = sorted(set(y for y in ys_at_layer if y in multi_layer_ys))
        for a_idx in range(len(unique_ys)):
            for b_idx in range(a_idx + 1, len(unique_ys)):
                must_separate.add((unique_ys[a_idx], unique_ys[b_idx]))
    if symmetric_diamonds:
        _separate_branches_across_trunk(must_separate, layer_stations, remap_ys)

    y_map: dict[float, float] = {}
    slot_for_y: dict[float, int] = {}
    prev_slot = 0
    for old_y in remap_ys:
        raw_slot = int(math.floor(old_y / effective_y_spacing))
        slot = max(raw_slot, prev_slot)
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
        if has_diamond and prev_slot > 0 and slot - prev_slot < 2:
            slot = prev_slot + 2
        elif has_diamond and prev_slot == 0 and slot < 2 and old_y > 0:
            slot = 2
        y_map[old_y] = slot * effective_y_spacing
        slot_for_y[old_y] = slot
        prev_slot = slot
    return y_map


def _build_grid_y_map(
    sub: MetroGraph,
    section_class_entry: tuple[dict[int, list[float]], set[float]],
    effective_y_spacing: float,
    symmetric_diamonds: bool,
) -> tuple[dict[float, float], list[float], list[float], int]:
    """Map a section's station Ys to the shared grid.

    Returns ``(y_map, remap_ys, all_ys, max_layer_size)``.  Uniform input gaps
    map to equally-spaced slots (avoiding asymmetric compression that clashes
    labels); otherwise per-layer separation drives the slot assignment.  A
    diamond hub (an isolated Y between two tracks for a small fan-out) keeps a
    2-slot gap so it has visual room.
    """
    layer_stations, multi_layer_ys = section_class_entry
    if multi_layer_ys:
        remap_ys = sorted(multi_layer_ys)
    else:
        remap_ys = sorted(set(s.y for s in sub.stations.values()))

    max_layer_size = max((len(ys) for ys in layer_stations.values()), default=0)
    all_ys = sorted(set(s.y for s in sub.stations.values()))
    isolated_ys = set(all_ys) - set(remap_ys)
    has_diamond = False
    if max_layer_size <= 3 and len(remap_ys) >= 2 and isolated_ys:
        for iso_y in isolated_ys:
            if remap_ys[0] < iso_y < remap_ys[-1]:
                has_diamond = True
                break

    gaps = [remap_ys[i + 1] - remap_ys[i] for i in range(len(remap_ys) - 1)]
    uniform_gap = len(gaps) >= 1 and all(abs(g - gaps[0]) < 1.0 for g in gaps)

    if uniform_gap and len(gaps) >= 1:
        slot_gap = max(1, int(math.floor(gaps[0] / effective_y_spacing)))
        if has_diamond and slot_gap < 2:
            slot_gap = 2
        y_map = {
            old_y: i * slot_gap * effective_y_spacing
            for i, old_y in enumerate(remap_ys)
        }
    else:
        y_map = _assign_nonuniform_slots(
            layer_stations,
            multi_layer_ys,
            remap_ys,
            effective_y_spacing,
            has_diamond,
            symmetric_diamonds,
        )
    return y_map, remap_ys, all_ys, max_layer_size


def _enforce_multiline_label_gap(
    sub: MetroGraph,
    y_map: dict[float, float],
    remap_ys: list[float],
    effective_y_spacing: float,
) -> None:
    """Widen the slot gap to 2 for a multi-line label sandwiched both sides.

    A multi-line label station hemmed in by same-layer neighbours above AND
    below needs the gap to fit its text; one at the top or bottom of its column
    can extend outward and is left alone.
    """
    layer_at_y: dict[tuple[int, float], bool] = {}
    for st in sub.stations.values():
        if not st.is_port:
            layer_at_y[(st.layer, st.y)] = True
    needs_gap_ys: set[float] = set()
    for st in sub.stations.values():
        if st.is_port or not st.label or "\n" not in st.label:
            continue
        if st.y not in y_map:
            continue
        has_above = any(
            (st.layer, ry) in layer_at_y
            for ry in remap_ys
            if ry < st.y - SAME_COORD_TOLERANCE
        )
        has_below = any(
            (st.layer, ry) in layer_at_y
            for ry in remap_ys
            if ry > st.y + SAME_COORD_TOLERANCE
        )
        if has_above and has_below:
            needs_gap_ys.add(st.y)
    if not needs_gap_ys:
        return
    sorted_mapped = sorted(y_map.items(), key=lambda kv: kv[1])
    for idx in range(1, len(sorted_mapped)):
        old_y, new_y = sorted_mapped[idx]
        prev_y = sorted_mapped[idx - 1][1]
        if old_y not in needs_gap_ys:
            continue
        gap_slots = round((new_y - prev_y) / effective_y_spacing)
        if gap_slots < 2:
            extra = (2 - gap_slots) * effective_y_spacing
            for j in range(idx, len(sorted_mapped)):
                k = sorted_mapped[j][0]
                y_map[k] += extra
            sorted_mapped = sorted(y_map.items(), key=lambda kv: kv[1])


def _isolated_slot_y(
    old_y: float,
    remap_ys: list[float],
    y_map: dict[float, float],
    effective_y_spacing: float,
) -> float:
    """Grid Y for an isolated row that no multi-station layer pins.

    When the isolated row sits between two kept rows -- the trunk hub of a
    fork-join, with its branches sharing a layer above and below -- map it to
    the proportional point between the kept rows' *remapped* positions before
    snapping.  Rounding the raw Y against the pitch instead can land the hub on
    a branch row when the bundle width inflates the pitch past twice the hub's
    offset, collapsing a symmetric diamond.  Rows outside the kept range fall
    back to nearest-slot rounding.
    """
    lower = [ry for ry in remap_ys if ry < old_y]
    upper = [ry for ry in remap_ys if ry > old_y]
    if lower and upper:
        lo, hi = max(lower), min(upper)
        frac = (old_y - lo) / (hi - lo)
        mapped = y_map[lo] + frac * (y_map[hi] - y_map[lo])
        return round(mapped / effective_y_spacing) * effective_y_spacing
    return round(old_y / effective_y_spacing) * effective_y_spacing


def _remap_section_to_grid(
    graph: MetroGraph,
    sub: MetroGraph,
    section: Section,
    section_class_entry: tuple[dict[int, list[float]], set[float]],
    effective_y_spacing: float,
    max_y_pad: float,
    section_y_padding: float,
) -> None:
    """Remap one section's station Ys onto the shared row grid and resize bbox."""
    y_map, remap_ys, all_ys, max_layer_size = _build_grid_y_map(
        sub,
        section_class_entry,
        effective_y_spacing,
        graph.diamond_style == "symmetric",
    )
    _enforce_multiline_label_gap(sub, y_map, remap_ys, effective_y_spacing)

    # Snap isolated Y values to the nearest grid slot, keeping diamond join
    # points on-grid without collapsing them onto a track endpoint.  Skipped
    # for large fan-outs where snapping disrupts routing geometry.
    if max_layer_size <= 3:
        for old_y in all_ys:
            if old_y not in y_map:
                y_map[old_y] = _isolated_slot_y(
                    old_y, remap_ys, y_map, effective_y_spacing
                )

    for station in sub.stations.values():
        if station.y in y_map:
            station.y = y_map[station.y]

    # y_pad compensation: shift so the gap from the top of the Y range to the
    # first station equals max_y_pad, making first_station_y consistent across
    # sections with different multiline label padding after top-alignment.
    y_pad = section_y_padding + _multiline_label_padding(sub)
    shift = max_y_pad - y_pad
    if shift > 0:
        for station in sub.stations.values():
            station.y += shift

    ys = [s.y for s in sub.stations.values()]
    section.bbox_y = min(ys) - max_y_pad
    section.bbox_h = (max(ys) - min(ys)) + max_y_pad * 2


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
       shift within their bbox but the box itself keeps its Stage-1.1 size.
    3. **y_pad compensation**: a uniform shift of ``max_y_pad - y_pad``
       is applied to every station so that after Stage 3.5 top-aligns
       bbox_y, the first-station Y matches across sections despite
       differing ``_multiline_label_padding``.

    Stores grid metadata on ``graph._row_y_grid_info`` for the debug
    overlay to render shared Y grid lines.

    Runs between Stage 1.1 and Stage 1.3, operating in local coordinates.
    """
    from nf_metro.layout.section_placement import _assign_grid_layout

    # Grid rows are not yet set on Section objects at this point.
    section_edges = graph.section_dag.section_edges if graph.section_dag else set()
    _, row_assign = _assign_grid_layout(graph, section_edges)

    groups = _group_sections_by_row(graph, section_subgraphs, row_assign)

    grid_info: dict[int, RowGridInfo] = {}
    for (row, _direction), sec_ids in groups.items():
        if len(sec_ids) < 2:
            continue

        spacing = _row_group_grid_spacing(
            graph, section_subgraphs, sec_ids, section_y_padding, y_spacing
        )
        if spacing is None:
            continue
        grid_slots, max_y_pad, effective_y_spacing, section_class = spacing

        grid_info[row] = {
            "section_ids": list(sec_ids),
            "slot_count": grid_slots,
            "slot_spacing": effective_y_spacing,
            "max_y_pad": max_y_pad,
        }

        for sec_id in sec_ids:
            _remap_section_to_grid(
                graph,
                section_subgraphs[sec_id],
                graph.sections[sec_id],
                section_class[sec_id],
                effective_y_spacing,
                max_y_pad,
                section_y_padding,
            )

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
            # Snap TOP/BOTTOM ports onto the recomputed edges: a perpendicular
            # entry port that lands within the recomputed span (a section
            # entered from more than one side, whose perp port is not the
            # vertical extreme) would otherwise sit off the boundary.
            _pull_section_ports_to_edge(graph, section, PortSide.TOP, section.bbox_y)
            _pull_section_ports_to_edge(
                graph, section, PortSide.BOTTOM, section.bbox_y + section.bbox_h
            )


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


def _distribute_stacked_rows_in_rowspan_band(graph: MetroGraph) -> None:
    """Distribute single-row sections stacked in a column across the band a
    neighbouring rowspan section fills.

    A column can hold several single-row sections stacked one per grid row,
    beside a taller ``grid_row_span > 1`` section that spans those same rows.
    Each single-row section is otherwise positioned on its own row line, so the
    topmost can poke above the band top -- a fan centred on its row line spreads
    upward into the title band -- and the bottommost can stop short of the band
    bottom, leaving slack beneath it.

    When the stack holds exactly one section per band row, distribute the
    sections evenly across the band: the topmost's bbox top meets the band top,
    the bottommost's bbox bottom meets the band bottom, and any middle sections
    sit on equal gaps between.  Only acts when the band has slack to give, so a
    stack already filling its band is left untouched.
    """
    for stack, band_top, band_bot in iter_stacked_rows_in_rowspan_band(
        graph, SAME_COORD_TOLERANCE
    ):
        slack = (band_bot - band_top) - sum(s.bbox_h for s in stack)
        gap = slack / (len(stack) - 1) if len(stack) > 1 else 0.0
        cursor = band_top
        for section in stack:
            delta = cursor - section.bbox_y
            if abs(delta) > SAME_COORD_TOLERANCE:
                shift_section(graph, section, dy=delta)
            cursor += section.bbox_h + gap


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
            or not lanes_run_along_y(section.direction)
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
                if delta < SAME_COORD_TOLERANCE:
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
                port_set = section.port_ids
                internal_ids = set(section.station_ids) - port_set
                for pid in port_set:
                    p = graph.ports.get(pid)
                    port_st = graph.stations.get(pid)
                    if (
                        p is None
                        or port_st is None
                        or p.side not in (PortSide.LEFT, PortSide.RIGHT)
                        or abs(port_st.y - target_y) < SAME_COORD_TOLERANCE
                    ):
                        continue
                    connected_ys: set[float] = set()
                    target_aligned = False
                    neighbours: list[str] = []
                    for edge in graph.edges_from(pid):
                        if edge.target in internal_ids:
                            neighbours.append(edge.target)
                    for edge in graph.edges_to(pid):
                        if edge.source in internal_ids:
                            neighbours.append(edge.source)
                    for other_id in neighbours:
                        st = graph.stations.get(other_id)
                        if st and not st.is_port:
                            connected_ys.add(round(st.y, 1))
                            if abs(st.y - target_y) < SAME_COORD_TOLERANCE:
                                target_aligned = True
                    if len(connected_ys) < 2 and target_aligned:
                        _set_port_y(graph, pid, target_y)


def _compact_row_content_to_bbox_top(
    graph: MetroGraph, section_y_padding: float, y_spacing: float
) -> None:
    """Pull row-mate sections up and shrink bottoms so content fits snugly.

    Two-step compaction within each grid row's contiguous column run:

    1. Per section, compute the allowable upward shift bounded by
       ``min(content_y) - bbox_y - section_y_padding`` so the topmost
       station (on-track or off-track) stays inside the bbox padding
       zone.  The uniform shift applied to the group is the minimum
       allowable shift across same-row sections; that preserves the
       trunk-Y alignment established by Stage 4.8.  Both on-track and
       off-track stations move together so the gap between each
       off-track input and its consumer is preserved.
    2. Shrink each section's ``bbox_h`` so the bottom slack matches
       ``section_y_padding`` (clamped so ports inside the section stay
       within the bbox).

    Row-spanning sections (``grid_row_span > 1``) are only isolated
    from their row-mates when their trunk Y differs (no shared
    horizontal bundle).  When a rowspan section trunks at the row's
    bundle Y, it must compact together with its column neighbours so
    the shared bundle stays straight.
    """
    row_sections: dict[int, list[Section]] = defaultdict(list)
    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.grid_row < 0:
            continue
        row_sections[section.grid_row].append(section)

    def _shares_bundle(a: Section, b: Section) -> bool:
        if a.grid_row_span <= 1 and b.grid_row_span <= 1:
            return True
        a_t = _section_trunk_y(graph, a)
        b_t = _section_trunk_y(graph, b)
        return (
            a_t is not None
            and b_t is not None
            and abs(a_t - b_t) < SAME_COORD_TOLERANCE
        )

    for sections in row_sections.values():
        if not sections:
            continue
        sections_by_col = sorted(sections, key=lambda s: s.grid_col)
        groups: list[list[Section]] = [[sections_by_col[0]]]
        for s in sections_by_col[1:]:
            prev = groups[-1][-1]
            if s.grid_col - prev.grid_col <= 1 and _shares_bundle(prev, s):
                groups[-1].append(s)
            else:
                groups.append([s])

        for group in groups:
            # An isolated rowspan section has no shared bundle to keep
            # straight; compacting it alone yanks its content above the
            # rowspan-1 cohort's trunk Y.
            if len(group) == 1 and group[0].grid_row_span > 1:
                continue
            allowed_shifts: list[float] = []
            for section in group:
                # Use all real (non-port) stations as the top reference so
                # off-track inputs lifted above their consumers also stay
                # inside the bbox padding zone.  Off-track stations now
                # move together with on-track during compaction.
                content_ys = [
                    graph.stations[sid].y
                    for sid in section.station_ids
                    if sid in graph.stations and not graph.stations[sid].is_port
                ]
                if not content_ys:
                    continue
                content_min = min(content_ys)
                shift = content_min - section.bbox_y - section_y_padding
                # A vertical-flow section's LEFT/RIGHT ports run perpendicular
                # to the flow; a cross-row entry sits lifted above the top
                # station.  Pulling content to bbox_y+padding would shift that
                # port above the shrunk top edge, sending its L-shaped entry
                # across the boundary.  Cap the shift so each such port stays
                # inside the box (its own row is the reserved input band).
                if lanes_run_along_x(section.direction):
                    for pid in (*section.entry_ports, *section.exit_ports):
                        p = graph.ports.get(pid)
                        if p is not None and p.side in (PortSide.LEFT, PortSide.RIGHT):
                            shift = min(shift, p.y - section.bbox_y)
                allowed_shifts.append(max(0.0, shift))
            delta = min(allowed_shifts) if allowed_shifts else 0.0

            if delta >= SAME_COORD_TOLERANCE:
                for section in group:
                    # bbox_y is preserved; only bbox_h shrinks.  Clamp
                    # edge-pinned ports so the shift doesn't push them
                    # outside the (now-shorter) bbox.
                    new_bottom = section.bbox_y + max(0.0, section.bbox_h - delta)
                    for sid in section.station_ids:
                        st = graph.stations.get(sid)
                        if st is None:
                            continue
                        new_y = st.y - delta
                        port = graph.ports.get(sid)
                        if port is not None:
                            if port.side == PortSide.TOP:
                                new_y = max(new_y, section.bbox_y)
                            elif port.side == PortSide.BOTTOM:
                                new_y = min(new_y, new_bottom)
                            _set_port_y(graph, sid, new_y)
                        else:
                            st.y = new_y
                    section.bbox_h = max(0.0, section.bbox_h - delta)

            for section in group:
                on_track_ys, off_track_ys, port_ys = _classify_section_station_ys(
                    graph, section
                )
                content_ys = on_track_ys + off_track_ys
                if not content_ys:
                    continue
                desired_bot = max(content_ys) + section_y_padding
                if port_ys:
                    desired_bot = max(desired_bot, max(port_ys))
                new_h = desired_bot - section.bbox_y
                if new_h < section.bbox_h - SAME_COORD_TOLERANCE:
                    section.bbox_h = max(0.0, new_h)
