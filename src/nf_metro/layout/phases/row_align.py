"""Align trunk Ys and bboxes across sections sharing a serpentine row."""

from __future__ import annotations

import math
from collections import defaultdict

from nf_metro.layout.constants import (
    FONT_HEIGHT,
    LABEL_OFFSET,
    OFFSET_STEP,
    STATION_RADIUS_APPROX,
)
from nf_metro.layout.phases._common import (
    _classify_multi_station_ys,
    _classify_section_station_ys,
    _max_stations_per_layer,
    _row_contiguous_column_groups,
    _section_bundle_lines,
    _section_trunk_y,
)
from nf_metro.layout.phases.ports import _set_port_y
from nf_metro.layout.phases.single_section import _multiline_label_padding
from nf_metro.parser.model import MetroGraph, PortSide, Section


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
        section_class: dict[str, tuple[dict[int, list[float]], set[float]]] = {
            sec_id: _classify_multi_station_ys(section_subgraphs[sec_id])
            for sec_id in sec_ids
        }
        for sec_id in sec_ids:
            sub = section_subgraphs[sec_id]
            _multi_ys = section_class[sec_id][1]
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

            layer_stations, multi_layer_ys = section_class[sec_id]

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
            # station equals max_y_pad.  After Stage 3.5 top-aligns
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
                port_set = section.port_ids
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
                            if abs(st.y - target_y) < 0.5:
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
        return a_t is not None and b_t is not None and abs(a_t - b_t) < 0.5

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
                allowed_shifts.append(max(0.0, shift))
            delta = min(allowed_shifts) if allowed_shifts else 0.0

            if delta >= 0.5:
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
                if new_h < section.bbox_h - 0.5:
                    section.bbox_h = max(0.0, new_h)
