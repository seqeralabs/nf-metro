"""Internal layout of a single section: tracks, labels, terminus icons, RL mirroring."""

from __future__ import annotations

import math

from nf_metro.layout.constants import (
    CURVE_RADIUS,
    DIAGONAL_LABEL_OFFSET,
    ENTRY_SHIFT_LR,
    ENTRY_SHIFT_TB,
    ENTRY_SHIFT_TB_CROSS,
    EXIT_GAP_MULTIPLIER,
    FONT_HEIGHT,
    ICON_CAPTION_FONT_HEIGHT,
    ICON_CAPTION_GAP,
    ICON_HALF_HEIGHT,
    ICON_INTER_GAP,
    LABEL_BBOX_MARGIN,
    LABEL_LINE_HEIGHT,
    LABEL_MARGIN,
    LABEL_OFFSET,
    LABEL_PAD,
    LINE_GAP,
    MIN_STATION_FLAT_LENGTH,
    TB_LINE_Y_OFFSET,
    TERMINUS_ICON_CLEARANCE,
    TERMINUS_ICON_CLEARANCE_V,
    TERMINUS_WIDTH,
)
from nf_metro.layout.labels import _label_text_height, label_text_width
from nf_metro.layout.layers import assign_layers
from nf_metro.layout.ordering import assign_tracks
from nf_metro.layout.phases._common import _build_section_subgraph
from nf_metro.layout.phases.off_track import (
    _align_phantom_pass_throughs,
    _compute_fork_join_gaps,
    _insert_phantom_pass_throughs,
)
from nf_metro.parser.model import MetroGraph, PortSide, Section, Station


def _align_terminus_to_upstream(graph: MetroGraph) -> None:
    """Pin a single downstream terminus to its sole upstream's Y.

    After ``_recenter_full_bundle_columns`` re-pitches fanned columns,
    a single-station downstream column (e.g. a ``file`` terminus
    consuming the fanned station's output) can be left at its pre-fan Y,
    so the connecting line and the icon caption drift away from the
    source station.  When the downstream station has exactly one in-
    section predecessor, snap it back onto the source's Y so its file
    icon sits level with the station it follows.

    Skips the pin when the target Y is already occupied by a sibling
    in the same X column: when a source fans out to a chain station
    (``bundle -> bundle_zip``) AND a terminus (``report_html``), pulling
    the terminus to the source's Y collides with the chain station that
    sits there.  Leaving the terminus at its grid Y preserves visual
    separation; the diagonal connector to the source is acceptable.
    """
    for section in graph.sections.values():
        if section.direction not in ("LR", "RL"):
            continue
        sec_sids = set(section.station_ids)
        for sid in section.station_ids:
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.off_track:
                continue
            if not st.is_terminus:
                continue
            preds = {
                e.source
                for e in graph.edges_to(sid)
                if e.source in sec_sids and not graph.stations[e.source].is_port
            }
            if len(preds) != 1:
                continue
            src = graph.stations[next(iter(preds))]
            if abs(src.y - st.y) < 0.5:
                continue
            collision = False
            for sib_sid in section.station_ids:
                if sib_sid == sid:
                    continue
                sib = graph.stations.get(sib_sid)
                if sib is None or sib.is_port or sib.is_hidden:
                    continue
                if abs(sib.x - st.x) > 0.5:
                    continue
                if abs(sib.y - src.y) < 0.5:
                    collision = True
                    break
            if collision:
                continue
            st.y = src.y


def _has_horizontal_predecessor_section(graph: MetroGraph, section: Section) -> bool:
    """True if any entry-port predecessor lives in an LR/RL section."""
    for pid in section.entry_ports:
        for edge in graph.edges_to(pid):
            src_port = graph.ports.get(edge.source)
            if not src_port:
                continue
            src_sec = graph.sections.get(src_port.section_id)
            if src_sec and src_sec.direction in ("LR", "RL"):
                return True
    return False


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
    entry_top = section.direction in (
        "LR",
        "RL",
    ) and _has_horizontal_predecessor_section(graph, section)

    tracks = assign_tracks(sub, layers, entry_top=entry_top)

    if not layers:
        return None

    # Snap phantom pass-throughs' successors to the pass-through track
    # so the trunk line stays horizontal past bypassed stations.
    _align_phantom_pass_throughs(sub, tracks)

    # Compact tracks so widely-spaced line priorities don't inflate
    # the vertical spread.  Gaps larger than LINE_GAP get capped so
    # distant line base tracks don't create excessive whitespace.
    # Off-track stations carry a placeholder track that will be
    # overwritten by Stage 5.2's lift-to-consumer pass, so they must not
    # influence the rank compaction of the on-track stations.
    unique_tracks = sorted(
        {tracks[sid] for sid in tracks if not sub.stations[sid].off_track}
    )
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
        # Off-track stations get rank 0 here as a placeholder; Stage 5.2
        # overwrites their Y to ``consumer.y - n*y_spacing``.  On-track
        # stations must have a track that made it into the rank map.
        if not station.off_track:
            assert station.track in track_rank, (
                f"on-track station {sid!r} has track {station.track} "
                f"missing from rank map {sorted(track_rank)}"
            )
        rank = track_rank.get(station.track, 0.0)
        if section.direction == "TB":
            station.x = rank * x_spacing
            station.y = station.layer * y_spacing + layer_extra.get(station.layer, 0)
        else:
            station.x = station.layer * x_spacing + layer_extra.get(station.layer, 0)
            station.y = rank * effective_y_spacing

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

    # Bypass V helpers (``__bypass_``) have no rendered marker.  Use
    # them to extend the bbox only when V sits beyond the real-station
    # extent, and only by enough for the diversion curve to clear the
    # section edge (~CURVE_RADIUS + half a station flat) - much less
    # than the full station_y_padding (which is reserved for label
    # clearance around real stations).
    real_for_bbox = [
        s for s in sub.stations.values() if not s.id.startswith("__bypass_")
    ]
    if not real_for_bbox:
        real_for_bbox = list(sub.stations.values())
    bypass_v_ys = [s.y for s in sub.stations.values() if s.id.startswith("__bypass_")]
    xs = [s.x for s in real_for_bbox]
    ys = [s.y for s in real_for_bbox]
    extra_label_h = _multiline_label_padding(sub)
    y_pad = section_y_padding + extra_label_h
    v_curve_clearance = CURVE_RADIUS + MIN_STATION_FLAT_LENGTH / 2
    y_min = min(ys)
    y_max = max(ys)
    section.bbox_x = min(xs) - section_x_padding
    section.bbox_w = (max(xs) - min(xs)) + section_x_padding * 2
    bbox_top = y_min - y_pad
    bbox_bot = y_max + y_pad
    # Angled labels (#527) hang below the lowest stations; reserve their
    # vertical reach so section/row placement keeps the row below clear.
    if section.direction in ("LR", "RL"):
        label_angle = graph.label_angle or 0.0
        angled_pad = _angled_label_bottom_padding(sub, label_angle)
        if angled_pad > 0:
            bbox_bot = max(bbox_bot, y_max + section_y_padding + angled_pad)
        # The angled label of the rightmost station overhangs to the right;
        # grow the bbox right edge so it matches the rendered box and the
        # inter-section feeder routes outside it (#527).
        angled_right_edge = _angled_label_right_edge(sub, label_angle)
        if angled_right_edge > 0:
            right_edge = max(
                section.bbox_x + section.bbox_w, angled_right_edge + LABEL_BBOX_MARGIN
            )
            section.bbox_w = right_edge - section.bbox_x
    if bypass_v_ys:
        # When V sits beyond the real-station extent, use curve-only
        # clearance rather than full label padding: V has no marker,
        # no label, just a curve corner to render past.
        v_min = min(bypass_v_ys)
        v_max = max(bypass_v_ys)
        if v_min < y_min:
            bbox_top = min(bbox_top, v_min - v_curve_clearance)
        if v_max > y_max:
            bbox_bot = v_max + v_curve_clearance
    section.bbox_y = bbox_top
    section.bbox_h = bbox_bot - bbox_top

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
    by_primary: dict[float, list[Station]] = {}
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


def angled_label_reach(station: Station, label_angle: float) -> float:
    """Vertical reach below a station's marker of its hanging angled label.

    Angled labels (#527) anchor below the pill and tilt down, so a long
    name reaches well below the marker by ``anchor_drop + width*sin(angle)``.
    Returns 0 when the angle is 0 or the station carries no name label, so
    horizontal-label layouts are unaffected.
    """
    if not label_angle:
        return 0.0
    if station.is_port or station.is_hidden or station.is_terminus:
        return 0.0
    if not station.label.strip():
        return 0.0
    sin_a = abs(math.sin(math.radians(label_angle)))
    return (
        LABEL_OFFSET + DIAGONAL_LABEL_OFFSET + label_text_width(station.label) * sin_a
    )


def angled_label_right_reach(station: Station, label_angle: float) -> float:
    """Horizontal reach to the right of a station's marker of its angled label.

    An angled label (#527) is anchored at the station X and tilted clockwise,
    so its rotated text box extends right of the marker.  The renderer grows
    the section bbox to contain that box; computing the same reach here lets
    layout finalise the section's right edge *before* routing runs, so the
    inter-section feeder turns down outside the drawn box rather than crossing
    its bottom edge.  Matches the rotated-AABB right extent used by the
    renderer (``width*cos(angle) + height*sin(angle)``).  Returns 0 when the
    angle is 0 or the station carries no name label.
    """
    if not label_angle:
        return 0.0
    if station.is_port or station.is_hidden or station.is_terminus:
        return 0.0
    if not station.label.strip():
        return 0.0
    rad = math.radians(label_angle)
    return label_text_width(station.label) * abs(math.cos(rad)) + _label_text_height(
        station.label
    ) * abs(math.sin(rad))


def _angled_label_right_edge(sub: MetroGraph, label_angle: float) -> float:
    """Rightmost X any station's angled label reaches (absolute, not a delta).

    Each station's reach is measured from its own X, so the section's required
    right edge is ``max(station.x + reach)``.  Returns 0 when no labels are
    angled.
    """
    return max(
        (s.x + angled_label_right_reach(s, label_angle) for s in sub.stations.values()),
        default=0.0,
    )


def _angled_label_bottom_padding(sub: MetroGraph, label_angle: float) -> float:
    """Worst-case angled-label reach below the lowest station in a section.

    Used during single-section layout to reserve the vertical extent the
    hanging angled labels need, so section/row placement keeps the row below
    clear.  0 when no labels are angled.
    """
    return max(
        (angled_label_reach(s, label_angle) for s in sub.stations.values()),
        default=0.0,
    )


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
        for edge in graph.edges_to(pid):
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
            e.target for e in graph.edges_from(pid) if e.target in section.station_ids
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
    # is needed and the gap can be skipped.  When the feeder is a bypass
    # V helper (``__bypass_`` id), trace back to its visible predecessor
    # so the diagonal at the V is collapsed back onto the predecessor's
    # Y - the V exists only because the line couldn't cross a consumer
    # marker, but the diagonal still terminates at a visible station.
    feeder_ys: set[float] = set()
    real_ids = set(sub.stations)
    for pid in flow_exit_port_ids:
        for edge in graph.edges_to(pid):
            if edge.source not in real_ids:
                continue
            src_id = edge.source
            if src_id.startswith("__bypass_"):
                pred_y = None
                for pe in graph.edges_to(src_id):
                    if pe.source in real_ids and not pe.source.startswith("__bypass_"):
                        ps = sub.stations.get(pe.source)
                        if ps is not None:
                            pred_y = ps.y
                            break
                if pred_y is None:
                    continue
                feeder_ys.add(pred_y)
            else:
                feeder_ys.add(sub.stations[src_id].y)

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


def _terminus_icon_clearance(
    n_icons: int,
    names: list[str] | None = None,
) -> float:
    """Compute clearance needed for *n_icons* file icons side-by-side.

    The base ``TERMINUS_ICON_CLEARANCE`` covers one icon (station_radius +
    gap + icon_width + margin).  Each additional icon adds the per-icon
    centre-to-centre step computed by the renderer's
    ``caption_aware_icon_step`` -- widened when adjacent captions would
    overrun the default ``ICON_INTER_GAP`` step.

    Layout doesn't know the theme, so caption widths are estimated
    using the default label size (14px, matches built-in themes).
    Slight over-budget is harmless: bbox just gets a few extra px of
    right padding.
    """
    if n_icons <= 1:
        return TERMINUS_ICON_CLEARANCE
    from nf_metro.render.constants import ICON_NAME_FONT_SCALE
    from nf_metro.render.svg import caption_aware_icon_step

    safe_names = names or [""] * n_icons
    caption_font_size = 14.0 * ICON_NAME_FONT_SCALE
    name_widths = [len(n) * caption_font_size * 0.55 if n else 0.0 for n in safe_names]
    step = caption_aware_icon_step(safe_names, name_widths, TERMINUS_WIDTH)
    extra = (n_icons - 1) * step
    return TERMINUS_ICON_CLEARANCE + extra


def _terminus_icon_clearance_vertical(
    n_icons: int,
    names: list[str] | None = None,
) -> float:
    """Vertical clearance for *n_icons* file icons stacked along the flow axis.

    TB/BT counterpart of ``_terminus_icon_clearance``: icons stack along Y,
    so each additional icon adds the icon height plus (when captions are
    present) a caption row, matching the renderer's TB step.
    """
    if n_icons <= 1:
        return TERMINUS_ICON_CLEARANCE_V
    caption_room = (
        ICON_CAPTION_GAP + ICON_CAPTION_FONT_HEIGHT if names and any(names) else 0.0
    )
    step = 2 * ICON_HALF_HEIGHT + ICON_INTER_GAP + caption_room
    return TERMINUS_ICON_CLEARANCE_V + (n_icons - 1) * step


def _terminus_icons_extend_forward(is_source: bool, section_dir: str) -> bool:
    """Whether a terminus's icons extend in the section's forward flow.

    Sinks extend forwards (down for TB, right for LR), sources backwards;
    RL/BT mirror that.  Single source of truth for the rule that
    ``render.svg._terminus_icon_centers`` applies on the render side.
    """
    return is_source if section_dir in ("RL", "BT") else not is_source


def _terminus_y_overhang(
    station: Station, section_dir: str, graph: MetroGraph
) -> tuple[float, float]:
    """(above, below) px a TB/BT terminus's icons extend past its marker.

    Returns ``(0.0, 0.0)`` for non-terminus stations and for LR/RL
    sections (whose icons extend horizontally), so content-extent callers
    stay byte-identical there.
    """
    if not station.is_terminus or section_dir not in ("TB", "BT"):
        return 0.0, 0.0
    is_source = not graph.edges_to(station.id)
    extent = _terminus_icon_clearance_vertical(
        len(station.terminus_labels), station.terminus_names
    )
    if _terminus_icons_extend_forward(is_source, section_dir):  # below
        return 0.0, extent
    return extent, 0.0


def _adjust_terminus_icon_clearance(
    sub: MetroGraph,
    section: Section,
    graph: MetroGraph,
) -> None:
    """Expand bbox when terminus file icons would be too close to the edge.

    Terminus icons march along the section's flow axis (horizontally for
    LR/RL, vertically for TB/BT), on the station's "outside": forwards for
    sinks, backwards for sources, with RL/BT mirrored.  When the section
    padding doesn't leave enough room, grow the bbox on the affected side.
    """
    section_dir = section.direction or "LR"
    is_tb = section_dir in ("TB", "BT")

    for station in sub.stations.values():
        if not station.is_terminus:
            continue

        n_icons = len(station.terminus_labels)
        is_source = not graph.edges_to(station.id)
        extends_forward = _terminus_icons_extend_forward(is_source, section_dir)

        if is_tb:
            needed = _terminus_icon_clearance_vertical(n_icons, station.terminus_names)
            if extends_forward:  # icons below the station
                clearance = section.bbox_y + section.bbox_h - station.y
                if clearance < needed:
                    section.bbox_h += needed - clearance
            else:  # icons above the station
                clearance = station.y - section.bbox_y
                if clearance < needed:
                    expand = needed - clearance
                    section.bbox_y -= expand
                    section.bbox_h += expand
        else:
            needed = _terminus_icon_clearance(n_icons, station.terminus_names)
            if not extends_forward:  # icons left of the station
                clearance = station.x - section.bbox_x
                if clearance < needed:
                    expand = needed - clearance
                    section.bbox_x -= expand
                    section.bbox_w += expand
            else:  # icons right of the station
                clearance = section.bbox_x + section.bbox_w - station.x
                if clearance < needed:
                    section.bbox_w += needed - clearance


def _shift_lr_perp_entry_stations(
    graph: MetroGraph,
    x_spacing: float,
) -> None:
    """Shift internal stations in LR/RL sections with perpendicular entry.

    Mirrors ``_adjust_tb_entry_shifts`` for horizontal-flow sections.
    In TB sections the station shift is applied in Stage 1.1, and entry-port
    alignment later overrides the port Y with the upstream source Y,
    creating a gap.  For LR/RL sections no such port-X override exists,
    so we shift stations after port initialisation (Stage 3.1) while ports
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
        port_ids = section.port_ids
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
        # Stage 1.1 (_adjust_lr_entry_inset) already reserved bbox space
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
