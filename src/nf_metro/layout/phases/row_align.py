"""Align trunk Ys and bboxes across sections sharing a serpentine row."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterator
from typing import NamedTuple

from nf_metro.layout.constants import (
    FONT_HEIGHT,
    LABEL_OFFSET,
    PERP_PORT_EDGE_INSET,
    SAME_COORD_TOLERANCE,
    graph_offset_step,
)
from nf_metro.layout.geometry import (
    lanes_run_along_x,
    lanes_run_along_y,
    perpendicular_port_sides,
    shift_section,
)
from nf_metro.layout.pass_metrics import active_font_scale, station_radius_approx
from nf_metro.layout.phases._common import (
    _classify_multi_station_ys,
    _classify_section_station_ys,
    _is_fan_branch_leaf,
    _max_stations_per_layer,
    _row_contiguous_column_groups,
    _row_through_and_carrier_lines,
    _section_bundle_lines,
    _section_row_through_lines,
    _section_trunk_y,
    iter_stacked_rows_in_rowspan_band,
)
from nf_metro.layout.phases.bbox import level_group_anchor_edges
from nf_metro.layout.phases.ports import _set_port_y
from nf_metro.layout.phases.single_section import (
    _multiline_label_padding,
    _terminus_y_overhang,
)
from nf_metro.parser.model import (
    MetroGraph,
    PartialTrunkDescent,
    PortSide,
    RowGridInfo,
    Section,
    is_bypass_v,
)


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
    offset_step = graph_offset_step(graph)
    min_track_gap = (
        (max_lines - 1) * offset_step
        + 2 * station_radius_approx()
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
       is applied to every station so that after Stage 3.4 top-aligns
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


def _top_align_row_sections(graph: MetroGraph, rows: set[int] | None = None) -> None:
    """Shift sections up so bbox tops align within each grid row.

    Only aligns sections that form contiguous column groups within the
    row.  Sections separated by a column gap (e.g. reporting at col 3
    vs dna_analysis at col 1 with no row-mate at col 2) are aligned
    independently so structurally-determined positions aren't disturbed.

    When ``rows`` is given, only those grid rows are re-flushed; callers
    that disturbed a known set of rows (see ``_align_exit_ports``) pass it
    to confine the realign to the rows they moved.  ``None`` realigns every
    row; an empty set realigns none.
    """
    if rows is not None and not rows:
        return

    row_sections: dict[int, list[Section]] = defaultdict(list)
    for section in graph.sections.values():
        if section.bbox_h > 0 and section.grid_row >= 0:
            row_sections[section.grid_row].append(section)

    for row, sections in row_sections.items():
        if rows is not None and row not in rows:
            continue
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
                shift_section(graph, section, dy=-delta)


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


def _top_align_row_bboxes_only(
    graph: MetroGraph,
    rows: set[int] | None = None,
) -> None:
    """Align bbox tops within each row by growing bboxes upward.

    Unlike ``_top_align_row_sections`` (which shifts stations together
    with their bbox), this phase only moves ``bbox_y`` and grows
    ``bbox_h`` so the section background extends upward to match the
    row's topmost bbox.  Interior station and junction Ys are left in
    place, producing empty space at the top of sections that didn't have
    off-track inputs to lift; TOP ports, which ride the top edge, are
    carried up to the new top so they stay on the boundary.

    The Y half of :func:`...bbox.level_group_anchor_edges`.  A row's boxes are
    levelled on their tops whichever way their flows run, because it is the
    header badge -- text, which a rotation does not carry with it -- that rides
    the box top.

    Used after ``_lift_off_track_stations`` so off-track expansion in
    one section doesn't leave other row-mates with misaligned bbox
    tops.
    """
    for group in _row_contiguous_column_groups(graph):
        if rows is not None and group[0].grid_row not in rows:
            continue
        level_group_anchor_edges(graph, group, "y", 1.0)


def _packed_row_header_groups(graph: MetroGraph) -> list[list[Section]]:
    """Return row groups whose header line includes a packed cell."""
    packed_members = {
        member
        for members in graph.cell_packs.values()
        if len(members) > 1
        for member in members
    }
    if not packed_members:
        return []
    return [
        group
        for group in _row_contiguous_column_groups(graph)
        if any(section.id in packed_members for section in group)
    ]


def _top_align_packed_row_bboxes(graph: MetroGraph) -> None:
    """Preserve one header line across rows containing packed cells.

    A packed cell places multiple boxes side by side within one grid column.
    Its header line belongs to the surrounding contiguous row, so content-fit
    compaction must not leave those adjacent badges on a staircase.

    A no-op unless ``graph.row_align == "top"``: the default content-hugging
    mode lets each packed-cell row-mate keep its own content-fit top.
    """
    if graph.row_align != "top":
        return
    for group in _packed_row_header_groups(graph):
        level_group_anchor_edges(graph, group, "y", 1.0)


class _Handover(NamedTuple):
    """One partial-to-carrier through-line hand-over and its offset step."""

    partial_port: str
    carrier_port: str
    line_id: str
    partial_y: float
    carrier_y: float
    step: float


def _partial_handovers(
    graph: MetroGraph,
    partial: Section,
    carrier_ports: set[str],
    offsets: dict[tuple[str, str], float],
) -> list[_Handover]:
    """Direct port-to-port through-line hand-overs from *partial* to a carrier.

    A hand-over qualifies when it runs port to port to a carrier one grid column
    away with no junction between them and no cell-mate forcing a bypass -- the
    cases a level connector can be drawn for.  Each carries the offset step
    (destination lane minus source lane) at the current frame.  *carrier_ports*
    is the union of every carrier section's port IDs.
    """
    from nf_metro.layout.routing.context import (
        HopEnd,
        _hop_needs_bypass,
        _resolve_section_colrow,
    )

    junction_ids = graph.junction_ids
    handovers: list[_Handover] = []
    for pid in partial.port_ids:
        p_port = graph.ports.get(pid)
        p_station = graph.stations.get(pid)
        if (
            p_port is None
            or p_station is None
            or p_port.side not in (PortSide.LEFT, PortSide.RIGHT)
        ):
            continue
        p_col, p_row = _resolve_section_colrow(graph, p_station)
        if p_col is None:
            continue
        for edge in (*graph.edges_from(pid), *graph.edges_to(pid)):
            if edge.source in junction_ids or edge.target in junction_ids:
                continue
            other = edge.target if edge.source == pid else edge.source
            if other not in carrier_ports:
                continue
            c_port = graph.ports.get(other)
            c_station = graph.stations.get(other)
            if (
                c_port is None
                or c_station is None
                or c_port.side not in (PortSide.LEFT, PortSide.RIGHT)
            ):
                continue
            c_col, c_row = _resolve_section_colrow(graph, c_station)
            if c_col is None or abs(p_col - c_col) != 1:
                continue
            if _hop_needs_bypass(
                graph,
                HopEnd(p_station, p_col, p_row),
                HopEnd(c_station, c_col, c_row),
            ).needed:
                continue
            lid = edge.line_id
            step = offsets.get((other, lid), 0.0) - offsets.get((pid, lid), 0.0)
            handovers.append(
                _Handover(pid, other, lid, p_station.y, c_station.y, round(step, 3))
            )
    return handovers


def _descent_step_is_stable(
    graph: MetroGraph, partial: Section, handovers: list[_Handover], step: float
) -> bool:
    """Whether *step* survives when the hand-over is levelled and re-derived.

    Mid-Stage-4.8 a carrier's exit port has not yet settled onto the level its
    exiting lines live at, so its per-line offsets -- and hence the raw step --
    can be a transient of the partial and carrier ports sitting at different Ys.
    Placing the partial section so its port lands level with the carrier port
    and re-running :func:`compute_station_offsets` reproduces the offset regime
    the settled hand-over yields: a real lane kink survives as the same step, a
    spurious one collapses.  A rigid section shift can only level every hand-over
    at once when they share one carrier-minus-partial gap; when they do not, the
    step is treated as unstable so the descent is suppressed (a wrong suppress
    degrades to the flat-less hand-over, a wrong keep corrupts the exit port).
    """
    from nf_metro.layout.routing.offsets import compute_station_offsets

    deltas = {round(h.carrier_y - h.partial_y, 3) for h in handovers}
    if len(deltas) != 1:
        return False
    (delta,) = deltas
    pill_cache = dict(graph._linear_entry_pill_lines_cache)
    shift_section(graph, partial, dy=delta)
    try:
        levelled = compute_station_offsets(graph)
    finally:
        shift_section(graph, partial, dy=-delta)
        graph._linear_entry_pill_lines_cache.clear()
        graph._linear_entry_pill_lines_cache.update(pill_cache)
    return all(
        round(
            levelled.get((h.carrier_port, h.line_id), 0.0)
            - levelled.get((h.partial_port, h.line_id), 0.0),
            3,
        )
        == step
        for h in handovers
    )


def _partial_trunk_descent(
    graph: MetroGraph,
    partial: Section,
    carrier_ports: set[str],
    offsets: dict[tuple[str, str], float],
) -> PartialTrunkDescent | None:
    """Extra downward offset seating a partial's trunk on its handover lane.

    A partial row-mate that hands a through-line straight to a carrier one grid
    column away -- port to port, with no junction between them and no cell-mate
    obstructing a level run -- draws that connector flat only when its own trunk
    sits at the line's assigned lane on the shared destination port, one lane
    deeper than the carrier trunk.  The step is the destination lane minus the
    source lane; both ports settle on the carrier trunk Y under the base pass,
    so that difference is the whole vertical gap the connector would otherwise
    jog through.

    Returns a :class:`PartialTrunkDescent` (its two endpoint ports and that step)
    when every qualifying handover agrees on one positive value that is stable
    under :func:`_descent_step_is_stable`, else ``None`` (the partial stays on the
    carrier trunk).
    """
    handovers = _partial_handovers(graph, partial, carrier_ports, offsets)
    steps = {h.step for h in handovers}
    if len(steps) != 1:
        return None
    (step,) = steps
    if step <= 0:
        return None
    if not _descent_step_is_stable(graph, partial, handovers, step):
        return None
    rep = handovers[0]
    return PartialTrunkDescent(rep.partial_port, rep.carrier_port, step)


def _descent_of(
    partial_descents: dict[str, PartialTrunkDescent], section_id: str
) -> float:
    """The descent recorded for *section_id*, or ``0`` when it is not a partial."""
    record = partial_descents.get(section_id)
    return record.descent if record is not None else 0.0


def _row_trunk_alignment(
    graph: MetroGraph, group: list[Section]
) -> tuple[float, dict[str, float], dict[str, PartialTrunkDescent]] | None:
    """A row group's trunk Y, each member's current trunk Y, and partial descents.

    The third element maps a partial row-mate to its
    :class:`PartialTrunkDescent` (:func:`_partial_trunk_descent`); carriers and
    unaffected partials are absent (treated as ``0``).  ``None`` where the group
    has no single trunk to align to.
    """
    # Realigning a row needs one trunk that runs along it: the union of the
    # sections' through-lines -- the lines crossing them horizontally within the
    # row -- must be carried whole by at least one section.  Lines forking to
    # another row via a junction ride a perpendicular runway rather than the
    # trunk and are already excluded (see _section_row_through_lines).  With no
    # such carrier no single trunk spans the group, and forcing a common Y just
    # shifts content downward without geometric gain.
    through, carrier_lines = _row_through_and_carrier_lines(graph, group)
    if not carrier_lines:
        return None
    carriers = {s.id for s in group if set(through[s.id]) == carrier_lines}
    if not carriers:
        return None
    trunks = {s.id: t for s in group if (t := _section_trunk_y(graph, s)) is not None}
    if len(trunks) < 2:
        return None

    partial = [s for s in group if through[s.id] and s.id not in carriers]
    partial_descents: dict[str, PartialTrunkDescent] = {}
    if not partial:
        target_y = max(trunks.values())
    else:
        # A section carrying only part of the row trunk may be pulled onto it
        # but must not define where it sits: moving the full-bundle carriers to
        # suit such a section shifts the row down without straightening any line
        # that reaches them.  So the carriers must already have settled on one Y,
        # that Y is the target, and anything deeper than it stays put.  The trunk
        # also has to actually arrive at each partial section: one of its
        # through-lines must tie it to a carrier, else the alignment would jump a
        # row-mate that is off the trunk and sits between them.
        carrier_ys = {trunks[sid] for sid in carriers if sid in trunks}
        if len(carrier_ys) != 1:
            return None
        if any(not carriers & set().union(*through[s.id].values()) for s in partial):
            return None
        target_y = carrier_ys.pop()
        # A partial that hands a line straight to an adjacent carrier seats one
        # lane deeper so that connector runs level at the line's own lane rather
        # than jogging into it at the shared port.
        from nf_metro.layout.routing.offsets import compute_station_offsets

        offsets = compute_station_offsets(graph)
        carrier_ports = {
            pid for cid in carriers for pid in graph.sections[cid].port_ids
        }
        partial_descents = {
            s.id: d
            for s in partial
            if (d := _partial_trunk_descent(graph, s, carrier_ports, offsets))
            is not None
        }

    # The trunk must already hand over between sections at target_y.  A
    # carrier's LEFT/RIGHT ports are where it does, so a target none of them
    # sits at would put a step into the very hand-over the alignment exists to
    # straighten.
    if not any(
        abs(port.y - target_y) < SAME_COORD_TOLERANCE
        for sid in carriers
        for pid in graph.sections[sid].port_ids
        if (port := graph.ports.get(pid)) is not None
        and port.side in (PortSide.LEFT, PortSide.RIGHT)
    ):
        return None
    return target_y, trunks, partial_descents


def _facing_port_pairs(
    graph: MetroGraph, left: Section, right: Section
) -> Iterator[tuple[str, str]]:
    """Port ids that face each other across the gap between two row neighbours.

    The pair carries the same lines on each side of the boundary, so it is the
    lane the row trunk has to hold level.  Matching by side rather than by
    entry/exit role keeps the pairing valid whichever way the row flows: under
    RL it is the left section's port that receives.
    """
    right_lines = {
        rpid: set(graph.station_lines(rpid))
        for rpid in right.port_ids
        if (rport := graph.ports.get(rpid)) is not None and rport.side is PortSide.LEFT
    }
    for lpid in left.port_ids:
        lport = graph.ports.get(lpid)
        if lport is None or lport.side is not PortSide.RIGHT:
            continue
        lines = set(graph.station_lines(lpid))
        if not lines:
            continue
        for rpid, rlines in right_lines.items():
            if rlines == lines:
                yield lpid, rpid


def _boundary_lane_is_kinked(graph: MetroGraph, left: Section, right: Section) -> bool:
    """True when a facing-port pair between two neighbours splits its lane."""
    for lpid, rpid in _facing_port_pairs(graph, left, right):
        lst = graph.stations.get(lpid)
        rst = graph.stations.get(rpid)
        if lst is None or rst is None:
            continue
        if abs(lst.y - rst.y) > SAME_COORD_TOLERANCE:
            return True
    return False


def _section_straddles_two_lanes(
    graph: MetroGraph, section: Section, lines: set[str]
) -> bool:
    """True when a through-line enters and leaves *section* at different Ys.

    Such a section is caught between two lanes: one boundary holds the row's
    lane while the other holds its own content's, so one of the two runs bends
    at the box edge whatever the ports do.
    """
    port_lines = {pid: set(graph.station_lines(pid)) for pid in section.port_ids}
    for line in lines:
        by_side: dict[PortSide, list[float]] = {}
        for pid in section.port_ids:
            port = graph.ports.get(pid)
            station = graph.stations.get(pid)
            if (
                port is None
                or station is None
                or port.side not in (PortSide.LEFT, PortSide.RIGHT)
                or line not in port_lines[pid]
            ):
                continue
            by_side.setdefault(port.side, []).append(station.y)
        if len(by_side) < 2:
            continue
        left, right = by_side[PortSide.LEFT], by_side[PortSide.RIGHT]
        if min(abs(a - b) for a in left for b in right) > SAME_COORD_TOLERANCE:
            return True
    return False


def _row_trunk_alignment_stretches(
    graph: MetroGraph, group: list[Section]
) -> list[list[Section]]:
    """The stretches of a differing row run that should share one trunk Y.

    Only reached once `_row_trunk_alignment` has declined the run for want of a
    single carrier trunk: a run whose sections all carry the same through-lines
    has a row-wide trunk and is settled there, so this returns nothing for it.

    A trunk spans only neighbours carrying the same through-lines -- the lines
    crossing a section horizontally along the row.  A section may carry extra
    lines that fork off to another row via a junction; those ride a
    perpendicular runway, not the trunk, so they are excluded (see
    ``_section_row_through_lines``).  The run splits into stretches of *adjacent*
    sections, ordered by ``bbox_x`` because sections packed into one grid cell
    all share a ``grid_col``.  Two neighbours join a stretch only when they
    carry the same through-lines *and* their facing ports currently split that
    lane.  A stretch is returned only if one of its members straddles two
    lanes; a stretch whose members already run single-lane end to end is left
    alone.
    """
    through_by_id = {s.id: set(_section_row_through_lines(graph, s)) for s in group}
    non_empty = [t for t in through_by_id.values() if t]
    if not non_empty or all(t == non_empty[0] for t in non_empty):
        return []

    stretches: list[list[Section]] = []
    previous: set[str] | None = None
    for section in sorted(group, key=lambda s: s.bbox_x):
        lines = through_by_id[section.id]
        if (
            lines
            and lines == previous
            and _boundary_lane_is_kinked(graph, stretches[-1][-1], section)
        ):
            stretches[-1].append(section)
        else:
            stretches.append([section])
        previous = lines
    return [
        stretch
        for stretch in stretches
        if len(stretch) > 1
        and any(
            _section_straddles_two_lanes(graph, section, through_by_id[section.id])
            for section in stretch
        )
    ]


def _align_row_trunk_ys(graph: MetroGraph) -> None:
    """Shift sections vertically so trunk Ys align within each grid row.

    Sections in a row's contiguous column run whose trunk Y sits above
    the row's deepest trunk shift down to match.  Bbox tops are
    preserved (heights grow downward).  Row-spanning sections
    (grid_row_span > 1) are skipped to avoid disturbing cross-row
    vertical relationships.

    A partial row-mate handing a line straight to an adjacent carrier is seated
    one handover lane deeper (:func:`_partial_trunk_descent`).  That descent is a
    sub-grid offset the Stage 6.4 grid snap would collapse onto the carrier slot,
    so it is recorded in ``graph._partial_trunk_descents`` (reassigned in full
    each call, empty when nothing descends) for Stage 6.4 to re-add post-snap.
    """
    descents: dict[str, PartialTrunkDescent] = {}
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
            # A terminal section reached purely as a fan-out branch rides its
            # feeding line's trunk through a junction, so pulling it onto the
            # row trunk only wastes canvas -- keep it compact and let it fan
            # off (see _is_fan_branch_leaf, whose full_carrier argument is
            # this section's own through-lines matching the row's carrier
            # lines exactly).  A leaf fed by a private line stays in, else
            # compacting it would drag a shared exit port off the trunk.
            through, carrier_lines = _row_through_and_carrier_lines(graph, group)
            group = [
                s
                for s in group
                if not _is_fan_branch_leaf(
                    graph, s, full_carrier=set(through[s.id]) == carrier_lines
                )
            ]
            if len(group) < 2:
                continue
            alignment = _row_trunk_alignment(graph, group)
            if alignment is None:
                # No single carrier trunk spans the run, so there is no row-wide
                # Y to seat partials against.  A sub-run of neighbours that do
                # share a through-trunk owes it a level lane wherever one of
                # them is caught straddling two of them.
                for stretch in _row_trunk_alignment_stretches(graph, group):
                    _align_stretch_trunk_ys(graph, stretch)
                continue
            target_y, trunks, partial_descents = alignment
            descents.update(partial_descents)
            shifted: set[str] = set()
            for section in group:
                ty = trunks.get(section.id)
                if ty is None:
                    continue
                sec_target = target_y + _descent_of(partial_descents, section.id)
                delta = sec_target - ty
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

            # Re-snap each shifted section's LR ports to its trunk Y when they
            # have a single internal station there.  A port fanning to
            # 2+ distinct internal Ys is centred (fan-in) unless one neighbour
            # is the section trunk (a full-bundle station on the trunk), in which
            # case the port rides that trunk while the others peel off and snaps
            # to it.  A partial seated a lane deeper snaps to its own target, not
            # the carrier trunk, so its handover port lands flat.
            for section in group:
                if section.id not in shifted:
                    continue
                sec_target = target_y + _descent_of(partial_descents, section.id)
                bundle = _section_bundle_lines(graph, section)
                port_set = section.port_ids
                internal_ids = set(section.station_ids) - port_set
                for pid in port_set:
                    p = graph.ports.get(pid)
                    port_st = graph.stations.get(pid)
                    if (
                        p is None
                        or port_st is None
                        or p.side not in (PortSide.LEFT, PortSide.RIGHT)
                        or abs(port_st.y - sec_target) < SAME_COORD_TOLERANCE
                    ):
                        continue
                    connected_ys: set[float] = set()
                    target_aligned = False
                    trunk_at_target = False
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
                            if abs(st.y - sec_target) < SAME_COORD_TOLERANCE:
                                target_aligned = True
                                # A bypass-V helper carries the full bundle but
                                # is a routing artefact, not the trunk (matching
                                # _section_trunk_y's own exclusion).
                                if (
                                    bundle
                                    and not is_bypass_v(other_id)
                                    and set(graph.station_lines(other_id)) == bundle
                                ):
                                    trunk_at_target = True
                    if target_aligned and (len(connected_ys) < 2 or trunk_at_target):
                        _set_port_y(graph, pid, sec_target)

    graph._partial_trunk_descents = descents


def _align_stretch_trunk_ys(graph: MetroGraph, stretch: list[Section]) -> None:
    """Seat every section of one row stretch on its deepest trunk Y."""
    trunks = {s.id: t for s in stretch if (t := _section_trunk_y(graph, s)) is not None}
    if len(trunks) < 2:
        return
    target_y = max(trunks.values())
    shifted: set[str] = set()
    for section in stretch:
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

    # Re-snap each shifted section's LR ports onto target_y where the port's
    # own internal neighbours support it landing there.
    for section in stretch:
        if section.id not in shifted:
            continue
        bundle = _section_bundle_lines(graph, section)
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
            if _port_should_snap_to_trunk(graph, pid, internal_ids, bundle, target_y):
                _set_port_y(graph, pid, target_y)


def _port_should_snap_to_trunk(
    graph: MetroGraph,
    pid: str,
    internal_ids: set[str],
    bundle: set[str],
    target_y: float,
) -> bool:
    """True when the LR port *pid* should land on *target_y*.

    A port fanning to 2+ distinct internal Ys is centred (fan-in) unless one
    of them is the section trunk -- a full-bundle station at *target_y*,
    excluding a bypass-V helper (which carries the full bundle but is a
    routing artefact, not the trunk) -- in which case the port rides that
    trunk while the others peel off.
    """
    connected_ys: set[float] = set()
    target_aligned = False
    trunk_at_target = False
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
                if (
                    bundle
                    and not is_bypass_v(other_id)
                    and set(graph.station_lines(other_id)) == bundle
                ):
                    trunk_at_target = True
    return target_aligned and (len(connected_ys) < 2 or trunk_at_target)


def _perp_port_lead_edge_reserve(
    graph: MetroGraph, section: Section, section_padding: float
) -> float:
    """Room this section's perpendicular ports owe the low edge of its flow axis.

    A section's perpendicular ports are pinned to a lane-axis edge and free
    along the flow axis, so each owns a clearance to the flow-axis edge it faces:
    the top and bottom edges for a vertical (TB/BT) flow, the left and right ones
    for a horizontal (LR/RL) flow.  The high edge's clearance is settled before
    any compaction runs -- that edge trails the outermost content by
    ``section_padding``, and port and content move by the same shift, so no shift
    can change their separation.  Reserving it for the low edge is therefore what
    leaves the two ends of the section alike.

    Balancing outermost port against outermost port keeps this independent of
    which end is the entry, so a reversed flow needs no special case.  The
    rotation -- a horizontal flow's TOP/BOTTOM pair clearing its left and right
    edges -- is Stage 3.5's job, since X sizing has no per-section predictor to
    fold a port term into.

    A section with fewer than two perpendicular ports has no pair to balance and
    keeps the bare ``PERP_PORT_EDGE_INSET`` floor, so it does not pay padding
    for a symmetry it cannot show; the floor also wins outright where a port sits
    closer to its own edge than the floor allows.
    """
    sec_dir = section.direction or "LR"
    perp_sides = perpendicular_port_sides(sec_dir)
    port_ys = []
    perp_ys = []
    for pid in section.port_ids:
        port = graph.ports.get(pid)
        station = graph.stations.get(pid)
        if port is None or station is None:
            continue
        port_ys.append(station.y)
        if port.side in perp_sides:
            perp_ys.append(station.y)
    if len(perp_ys) < 2:
        return PERP_PORT_EDGE_INSET

    content_bottoms = [
        st.y + _terminus_y_overhang(st, sec_dir, graph)[1]
        for sid in section.station_ids
        if (st := graph.stations.get(sid)) is not None and not st.is_port
    ]
    if not content_bottoms:
        return PERP_PORT_EDGE_INSET

    desired_bottom = max(max(content_bottoms) + section_padding, max(port_ys))
    return max(PERP_PORT_EDGE_INSET, desired_bottom - max(perp_ys))


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
                # inside the bbox padding zone.  Off-track stations move
                # together with on-track during compaction.  A vertical-flow
                # terminus's file icon reaches above its marker (the section's
                # top-entering source icons), so the reference is the icon's
                # top edge, not the marker centre.
                sec_dir = section.direction or "LR"
                content_tops = [
                    graph.stations[sid].y
                    - _terminus_y_overhang(graph.stations[sid], sec_dir, graph)[0]
                    for sid in section.station_ids
                    if sid in graph.stations and not graph.stations[sid].is_port
                ]
                if not content_tops:
                    continue
                content_min = min(content_tops)
                shift = content_min - section.bbox_y - section_y_padding
                # A vertical-flow section's LEFT/RIGHT ports run perpendicular
                # to the flow; a cross-row entry sits lifted above the top
                # station.  Pulling content to bbox_y+padding would shift that
                # port above the shrunk top edge, sending its L-shaped entry
                # across the boundary.  Cap the shift so each such port keeps
                # ``_perp_port_lead_edge_reserve`` inside the box: a port flush
                # against the top edge draws its inbound run on the border,
                # leaving the section header no clear above-left position.
                if lanes_run_along_x(section.direction):
                    reserve = _perp_port_lead_edge_reserve(
                        graph, section, section_y_padding
                    )
                    for pid in (*section.entry_ports, *section.exit_ports):
                        p = graph.ports.get(pid)
                        if p is not None and p.side in perpendicular_port_sides(
                            section.direction
                        ):
                            shift = min(shift, p.y - section.bbox_y - reserve)
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
                port_ys = _classify_section_station_ys(graph, section)[2]
                sec_dir = section.direction or "LR"
                # A vertical-flow terminus's file icon reaches below its marker
                # (a bottom sink icon), so the bottom reference is the icon's
                # bottom edge, not the marker centre.  Both on-track and
                # off-track content count.
                content_bots = [
                    graph.stations[sid].y
                    + _terminus_y_overhang(graph.stations[sid], sec_dir, graph)[1]
                    for sid in section.station_ids
                    if sid in graph.stations and not graph.stations[sid].is_port
                ]
                if not content_bots:
                    continue
                desired_bot = max(content_bots) + section_y_padding
                if port_ys:
                    desired_bot = max(desired_bot, max(port_ys))
                new_h = desired_bot - section.bbox_y
                if new_h < section.bbox_h - SAME_COORD_TOLERANCE:
                    section.bbox_h = max(0.0, new_h)
