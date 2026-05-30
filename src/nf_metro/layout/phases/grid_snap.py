"""Layout phase: grid_snap (extracted from engine.py, see #451)."""

from __future__ import annotations

from collections import Counter

from nf_metro.layout.constants import (
    CANVAS_GRID_SHIFT_THRESHOLD,
)
from nf_metro.layout.phases.bbox import _min_section_bbox_top
from nf_metro.layout.phases.canvas import _translate_graph_y
from nf_metro.layout.phases.fan_bundles import _convergence_source_ys
from nf_metro.layout.phases.junctions import _position_junctions
from nf_metro.parser.model import MetroGraph, PortSide


def _snap_all_y_to_grid(graph: MetroGraph, y_spacing: float) -> None:
    """Snap every station and port Y to the nearest row-wide grid slot.

    Earlier phases (``_align_row_trunk_ys``, port-snap, downstream
    alignment) compute shifts that don't respect the grid pitch, so
    stations can land at fractional Ys (e.g. ``298.785`` when the pitch
    is 55).  This final pass restores a clean grid by:

    1. Grouping sections by row.  Sections sharing a row from
       ``_align_row_y_grids`` use the row's ``slot_spacing`` as pitch
       and snap to a single origin so trunks stay co-linear across the
       row.  Sections without a row grid entry are treated as their
       own one-section group at the input ``y_spacing``.
    2. Finding the group's grid origin as the mode of ``y % pitch``
       across ALL non-port, on-track stations in the group.  Using a
       global mode prevents per-section origins from drifting (which
       would kink the trunk between sections).
    3. Snapping every station and LEFT/RIGHT port in the group to the
       nearest ``origin + n * pitch``, bounded by half a pitch so
       adjacency cannot flip.

    Two exclusions preserve deliberate non-grid Ys:

    * LR/RL exit ports on TB-direction sections were placed by
      ``_resolve_tb_exit_y`` at the receiving section's entry-port Y
      (in a different row).  Snapping them to the TB's own row grid
      reintroduces the kink the alignment removed.
    * Stations that act as a convergence point for two or more inbound
      sources at different Ys (fan-in midpoint) carry geometric meaning
      that snapping destroys.

    Fan-out divergence hubs (stations whose Y sits strictly between
    targets above and below) are snapped to grid like other stations.
    The downstream column-centring pass identifies the snap-induced
    flat connection from such a fork hub to one of its targets and
    declines to treat it as a chain predecessor, so the target column
    still centres.

    Groups with no on-grid majority are left untouched.
    """
    if y_spacing <= 0:
        return
    # Map each convergence station/port to the set of source Ys it
    # converges (recorded pre-snap so the midpoint can be restored
    # after sources move).
    convergence_sources = _convergence_source_ys(graph)
    # Divergence anchors (fan-out hubs sitting between target Ys) are
    # snapped to grid like everyone else: the routing column-centring
    # pass treats their incidental flat connection (induced by snap)
    # as non-chain so the target column still centres correctly.
    groups: dict[object, tuple[float, list[str]]] = {}
    grouped_ids: set[str] = set()
    for row, info in (graph._row_y_grid_info or {}).items():
        pitch = info.get("slot_spacing", y_spacing)
        sec_ids = list(info.get("section_ids", []))
        groups[("row", row)] = (pitch, sec_ids)
        grouped_ids.update(sec_ids)
    for section in graph.sections.values():
        if section.id not in grouped_ids:
            groups[("solo", section.id)] = (y_spacing, [section.id])

    for pitch, sec_ids in groups.values():
        half = pitch / 2.0
        # Collect non-port, on-track station Ys across the whole group
        # to estimate the row's shared grid offset.  Off-track stations
        # were lifted by Stage 5.2 relative to their consumers; they
        # snap to the same grid (so the y_spacing gap above the
        # consumer is preserved) but don't influence the origin.
        residues: Counter[float] = Counter()
        per_section_ports: dict[str, set[str]] = {}
        half_grid_ids = graph.half_grid_station_ids
        for sec_id in sec_ids:
            section = graph.sections.get(sec_id)
            if section is None or section.bbox_h <= 0:
                continue
            port_ids = section.port_ids
            per_section_ports[sec_id] = port_ids
            for sid in section.station_ids:
                if sid in port_ids:
                    continue
                if sid in half_grid_ids:
                    # Half-grid stations sit at origin + 0.5 * pitch by
                    # design; don't let them shift the row's grid origin.
                    continue
                st = graph.stations.get(sid)
                if st is None or st.off_track:
                    continue
                residues[round(st.y % pitch, 3)] += 1
        if not residues:
            continue
        origin_r, top = residues.most_common(1)[0]
        if top < 2 and len(residues) > 1:
            continue

        def _snap(
            y: float, origin: float = origin_r, p: float = pitch, h: float = half
        ) -> float:
            snapped = origin + round((y - origin) / p) * p
            return snapped if abs(snapped - y) <= h + 1e-6 else y

        for sec_id, port_ids in per_section_ports.items():
            section = graph.sections.get(sec_id)
            if section is None:
                continue
            is_tb_section = section.direction == "TB"
            for sid in section.station_ids:
                if sid in port_ids:
                    continue
                if sid in half_grid_ids:
                    continue
                st = graph.stations.get(sid)
                if st is None:
                    continue
                if sid in convergence_sources:
                    continue
                st.y = _snap(st.y)
            for pid in port_ids:
                port = graph.ports.get(pid)
                port_st = graph.stations.get(pid)
                if port is None or port_st is None:
                    continue
                if port.side not in (PortSide.LEFT, PortSide.RIGHT):
                    continue
                # TB exit ports are anchored to the downstream entry-port
                # Y by _resolve_tb_exit_y; preserve that alignment.
                if is_tb_section and not port.is_entry:
                    continue
                if pid in convergence_sources:
                    continue
                port_st.y = _snap(port_st.y)

    # Restore convergence midpoints after snap: if a convergence
    # target's source Ys moved during snap, re-place the target at the
    # new midpoint so a fan-in still visually converges symmetrically.
    for target_id, src_ids in convergence_sources.items():
        st = graph.stations.get(target_id)
        if st is None or st.off_track:
            continue
        # Convergence sources whose snap displaced them away from their
        # original Y may break the midpoint relationship; recompute
        # from the post-snap source coordinates.
        new_src_ys = [graph.stations[sid].y for sid in src_ids if sid in graph.stations]
        if len(set(round(y, 3) for y in new_src_ys)) < 2:
            continue
        midpoint = (max(new_src_ys) + min(new_src_ys)) / 2.0
        st.y = midpoint


def _snap_canvas_y_to_grid(
    graph: MetroGraph,
    y_spacing: float,
    section_y_padding: float,
) -> None:
    """Final pass: align canvas-wide so stations land on integer y_spacing.

    The user rule is that real stations sit at integer multiples of
    ``y_spacing`` from a consistent canvas origin.  Earlier phases
    (Stage 6.4 ``_snap_all_y_to_grid`` + Stage 6.4's junction repos)
    produce a per-row grid, but late helpers can still shift the whole
    canvas by a non-grid amount.  Notably ``_shift_graph_into_canvas``
    can shift by ``section_y_padding - min_bbox_y`` which is not a
    multiple of ``y_spacing`` when padding is not a multiple of the
    pitch (default 50 / 40 = half-grid drift).

    Detection: collect the residue ``station.y % y_spacing`` for every
    real (non-port, non-off-track, non-half-grid, non-convergence)
    station.  If a single residue covers
    ``>= CANVAS_GRID_SHIFT_THRESHOLD`` of the population (default 85%),
    the canvas as a whole is uniformly off-grid by that residue.
    Compute the smallest signed shift ``delta`` such that:

      * ``(residue + delta) % y_spacing == 0`` (residue returns to grid)
      * ``min(section.bbox_y) + delta >= section_y_padding`` (top
        margin preserved)

    Apply ``delta`` to every station, port, junction (via bbox + offset
    chain) and section bbox.  If the dominant residue does NOT meet
    threshold, no shift is applied: the per-section snap from Stage
    6.4 is honoured as the best-effort alignment.
    """
    if y_spacing <= 0 or not graph.sections:
        return
    half_grid_ids = graph.half_grid_station_ids
    convergence_sources = _convergence_source_ys(graph)
    residues: Counter[float] = Counter()
    for st in graph.stations.values():
        if st.is_port or st.off_track:
            continue
        if st.id in half_grid_ids or st.id in convergence_sources:
            continue
        residues[round(st.y % y_spacing, 3)] += 1
    total = sum(residues.values())
    if total == 0:
        return
    mode_residue, mode_count = residues.most_common(1)[0]
    if mode_count / total < CANVAS_GRID_SHIFT_THRESHOLD:
        return
    if abs(mode_residue) < 1e-3 or abs(mode_residue - y_spacing) < 1e-3:
        return  # already on grid

    # Two candidate shifts: down by `-mode_residue`, or up by
    # `y_spacing - mode_residue`.  Prefer the one that preserves the
    # top margin; among equal choices prefer the smaller absolute shift.
    min_top = _min_section_bbox_top(graph, section_y_padding)
    shift_down = -mode_residue
    shift_up = y_spacing - mode_residue
    candidates: list[float] = []
    if min_top + shift_down >= section_y_padding - 1e-6:
        candidates.append(shift_down)
    if min_top + shift_up >= section_y_padding - 1e-6:
        candidates.append(shift_up)
    if not candidates:
        # Neither preserves the margin; pick the up-shift since
        # shifting down would clip the canvas.
        candidates.append(shift_up)
    shift = min(candidates, key=abs)
    if abs(shift) < 1e-6:
        return
    _translate_graph_y(graph, shift)
    # Junctions ride the same shift via _position_junctions, which keys
    # off the (now-shifted) exit/entry port Ys.
    _position_junctions(graph)
