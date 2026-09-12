"""Internal layout of a single section: tracks, labels, terminus icons, RL mirroring."""

from __future__ import annotations

import math

from nf_metro.layout.constants import (
    CURVE_RADIUS,
    DIAGONAL_LABEL_OFFSET,
    ENTRY_SHIFT_LR,
    ENTRY_SHIFT_TB,
    EXIT_GAP_MULTIPLIER,
    FONT_HEIGHT,
    ICON_CAPTION_GAP,
    ICON_HALF_HEIGHT,
    ICON_INTER_GAP,
    LABEL_BBOX_MARGIN,
    LABEL_MARGIN,
    LABEL_OFFSET,
    LABEL_PAD,
    LINE_GAP,
    MIN_STATION_FLAT_LENGTH,
    SAME_COORD_TOLERANCE,
    TB_LINE_Y_OFFSET,
    TERMINUS_ICON_CLEARANCE,
    TERMINUS_ICON_CLEARANCE_V,
    TERMINUS_ICON_GAP,
    TERMINUS_WIDTH,
)
from nf_metro.layout.geometry import (
    Axis,
    AxisFrame,
    lanes_run_along_x,
    lanes_run_along_y,
    section_lane_sign,
)
from nf_metro.layout.labels import (
    _label_text_height,
    label_text_width,
)
from nf_metro.layout.layers import assign_layers
from nf_metro.layout.ordering import assign_tracks
from nf_metro.layout.pass_metrics import (
    active_font_scale,
    icon_half_height_approx,
    station_radius_approx,
    terminus_width_approx,
)
from nf_metro.layout.phases._common import (
    _build_section_subgraph,
    _exit_reaching_nodes,
    perp_entry_lands_left,
    section_exit_lines,
)
from nf_metro.layout.phases.off_track import (
    _align_phantom_pass_throughs,
    _compute_fork_join_gaps,
    _insert_phantom_pass_throughs,
    _space_off_track_outputs,
)
from nf_metro.layout.phases.planned_fans import (
    apply_planned_fans_to_section_subgraph,
)
from nf_metro.parser.model import (
    ICON_TYPE_FILES,
    MetroGraph,
    PortSide,
    Section,
    Station,
    is_bypass_v,
)
from nf_metro.text_metrics import (
    DEFAULT_TEXT_METRICS,
    MetricsFace,
    TextRole,
    text_style,
)


def _align_straight_diamond_trunks(
    sub: MetroGraph,
    tracks: dict[str, float],
) -> None:
    """Put a straight diamond's fork and join on its nearest branch track.

    Phantom entry tracks can push both branches to one side of the track that
    ``assign_tracks`` chose for the fork and join.  Leaving the shared trunk
    outside the branch envelope makes both arms bend in the same direction and
    consumes an otherwise empty track.  A clean one-layer fork/join diamond can
    instead use its nearest branch as the straight arm.

    The join's sole same-track continuation follows it so a convergence helper
    and its visible endpoint remain one horizontal trunk.
    """
    for fork_id, fork in sub.stations.items():
        if fork.is_hidden or fork_id not in tracks:
            continue
        branches = {
            edge.target
            for edge in sub.edges_from(fork_id)
            if edge.target in tracks and not sub.stations[edge.target].is_hidden
        }
        if len(branches) != 2:
            continue
        if any(
            {
                edge.source
                for edge in sub.edges_to(branch_id)
                if not sub.stations[edge.source].is_hidden
            }
            != {fork_id}
            for branch_id in branches
        ):
            continue

        branch_successors = [
            {edge.target for edge in sub.edges_from(branch_id) if edge.target in tracks}
            for branch_id in branches
        ]
        if (
            len(branch_successors[0]) != 1
            or branch_successors[0] != branch_successors[1]
        ):
            continue

        join_id = next(iter(branch_successors[0]))
        join = sub.stations[join_id]
        if join.is_port or join.is_hidden:
            continue
        if {edge.source for edge in sub.edges_to(join_id)} != branches:
            continue
        branch_tracks = [tracks[branch_id] for branch_id in branches]
        branch_lo = min(branch_tracks)
        branch_hi = max(branch_tracks)
        if branch_hi - branch_lo < SAME_COORD_TOLERANCE:
            continue

        trunk_track = tracks[fork_id]
        if (
            branch_lo - SAME_COORD_TOLERANCE
            <= trunk_track
            <= (branch_hi + SAME_COORD_TOLERANCE)
        ):
            continue
        if abs(tracks[join_id] - trunk_track) > SAME_COORD_TOLERANCE:
            continue

        branch_track = branch_lo if trunk_track < branch_lo else branch_hi
        tracks[fork_id] = branch_track
        tracks[join_id] = branch_track

        current = join_id
        while True:
            successors = {edge.target for edge in sub.edges_from(current)}
            if len(successors) != 1:
                break
            successor = next(iter(successors))
            if successor not in tracks:
                break
            predecessors = {edge.source for edge in sub.edges_to(successor)}
            if predecessors != {current}:
                break
            if abs(tracks[successor] - trunk_track) > SAME_COORD_TOLERANCE:
                break
            tracks[successor] = branch_track
            current = successor


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
            if abs(src.y - st.y) < SAME_COORD_TOLERANCE:
                continue
            collision = False
            for sib_sid in section.station_ids:
                if sib_sid == sid:
                    continue
                sib = graph.stations.get(sib_sid)
                if sib is None or sib.is_port or sib.is_hidden:
                    continue
                if abs(sib.x - st.x) > SAME_COORD_TOLERANCE:
                    continue
                if abs(sib.y - src.y) < SAME_COORD_TOLERANCE:
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
    continuation_predecessors: dict[str, str],
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

    # A terminal spur carries only lines that never leave the section, so its
    # chain ends inside it.  The section subgraph cannot tell a spur from a
    # through-line node -- it omits the exit-port edges -- so classify against
    # the full-graph line set and the section's exit lines.
    exit_lines = section_exit_lines(graph, section)
    terminal_nodes = frozenset(
        sid
        for sid, st in sub.stations.items()
        if not st.is_port
        and (lines := set(graph.station_lines(sid)))
        and not (lines & exit_lines)
    )
    # Internal stations with a forward path to one of the section's exit ports:
    # the section's genuine through-line.  The subgraph omits the exit-port
    # edges, so this too is classified against the full graph (walking backward
    # from the exit ports through in-section feeders).
    exit_reaching = _exit_reaching_nodes(graph, section)
    tracks = assign_tracks(
        sub,
        layers,
        entry_top=entry_top,
        continuation_predecessors=continuation_predecessors,
        terminal_nodes=terminal_nodes,
        exit_reaching=exit_reaching,
    )

    if not layers:
        return None

    lead_extra = {
        pid: cols * x_spacing for pid, cols in section.off_track_lead_extra.items()
    }
    output_extra, output_layer_push = _space_off_track_outputs(
        sub, layers, tracks, x_spacing, lead_extra=lead_extra
    )

    # Snap phantom pass-throughs' successors to the pass-through track
    # so the trunk line stays horizontal past bypassed stations.
    _align_phantom_pass_throughs(sub, tracks)
    if graph.diamond_style == "straight":
        _align_straight_diamond_trunks(sub, tracks)

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
    flow_spacing = x_spacing if section.direction in ("LR", "RL") else y_spacing
    layer_extra = _compute_fork_join_gaps(
        sub,
        layers,
        tracks,
        flow_spacing,
        graph,
        section_sids,
        direction=section.direction,
    )

    # Label-strike clearance: a gap declared before layer ``L`` pushes ``L`` and
    # every downstream layer along the flow axis, lengthening the flat run into
    # the struck station so its own descent/ascent diagonal seats clear of its
    # name.  The strike-clearance loop populates this need-driven, so it is
    # empty (a no-op) for every layout that draws no strike.
    if section.label_strike_layer_gaps:
        all_layers = set(layers.values())
        for gap_layer, cols in section.label_strike_layer_gaps.items():
            extra = cols * x_spacing
            for layer in all_layers:
                if layer >= gap_layer:
                    layer_extra[layer] = layer_extra.get(layer, 0.0) + extra

    # Widen track spacing when multi-line labels need more vertical room
    effective_y_spacing = _multiline_track_spacing(sub, y_spacing)

    # Assign local coordinates along the section's layer (primary) and
    # track (secondary) axes; TB transposes which screen axis each maps to.
    frame = AxisFrame.for_direction(section.direction, x_spacing, y_spacing)
    # Track ranks widen for multi-line labels only when the track axis is Y
    # (the LR/RL regime); a TB section stacks lines along X at the base step.
    rank_step = (
        effective_y_spacing if frame.secondary.name == "y" else frame.secondary.step
    )
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
        output_offset = output_extra.get(sid, 0.0)
        layer_push = output_layer_push.get(station.layer, 0.0)
        frame.primary.set(
            station,
            station.layer * frame.primary.step
            + layer_extra.get(station.layer, 0)
            + layer_push
            + output_offset,
        )
        frame.secondary.set(station, rank * rank_step)

    # Resolve same-cell station collisions: two stations on the same line
    # priority can land on identical (x,y) when the track allocator collapses
    # distinct line tracks at a layer with only one occupant per line.
    _resolve_station_collisions(sub, section, x_spacing, effective_y_spacing)

    apply_planned_fans_to_section_subgraph(graph, sub, section)

    # Normalize Y so minimum is 0 (raw tracks can be negative)
    _normalize_min(sub, axis="y")

    # RL runs the primary axis in reverse: mirror it so layer 0 is at the far end.
    if frame.primary_sign < 0:
        _mirror_primary(sub, frame.primary)

    # Normalize local X so leftmost station is at x=0
    _normalize_min(sub, axis="x")

    # Ensure minimum inner extent so stations sit on visible track
    _enforce_min_extent(sub, section, x_spacing, y_spacing)

    # Put each bypass V on the side its carried line is drawn, so the fork
    # from the bypassed station's feeder reaches it without crossing the trunk.
    _align_bypass_v_to_lane_side(sub, section, graph)

    # Bypass V helpers (``__bypass_``) have no rendered marker.  Use
    # them to extend the bbox only when V sits beyond the real-station
    # extent, and only by enough for the diversion curve to clear the
    # section edge (~CURVE_RADIUS + half a station flat) - much less
    # than the full station_y_padding (which is reserved for label
    # clearance around real stations).
    real_for_bbox = [s for s in sub.stations.values() if not is_bypass_v(s.id)]
    if not real_for_bbox:
        real_for_bbox = list(sub.stations.values())
    bypass_v_ys = [s.y for s in sub.stations.values() if is_bypass_v(s.id)]
    bypass_v_xs = [s.x for s in sub.stations.values() if is_bypass_v(s.id)]
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
    bbox_top = min(
        bbox_top,
        *(
            station.y
            - (
                icon_half_height_approx()
                if station.off_track or station.is_terminus
                else station_radius_approx()
            )
            for station in real_for_bbox
        ),
    )
    bbox_bot = max(
        bbox_bot,
        *(
            station.y
            + (
                icon_half_height_approx()
                if station.off_track or station.is_terminus
                else station_radius_approx()
            )
            for station in real_for_bbox
        ),
    )
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
    # A TB section offsets its bypass V laterally (in X) rather than
    # vertically, so grow the bbox along X by the same curve-only clearance
    # when V sits beyond the real-station horizontal extent.
    if bypass_v_xs:
        x_left = section.bbox_x
        x_right = section.bbox_x + section.bbox_w
        v_xmin = min(bypass_v_xs)
        v_xmax = max(bypass_v_xs)
        if v_xmin - v_curve_clearance < x_left:
            x_left = v_xmin - v_curve_clearance
        if v_xmax + v_curve_clearance > x_right:
            x_right = v_xmax + v_curve_clearance
        section.bbox_x = x_left
        section.bbox_w = x_right - x_left
    section.bbox_y = bbox_top
    section.bbox_h = bbox_bot - bbox_top

    # Apply direction-specific bbox adjustments
    _adjust_tb_labels(sub, section)
    _adjust_tb_entry_shifts(section, sub, graph, y_spacing)
    _adjust_lr_entry_inset(sub, section, graph, x_spacing)
    _adjust_lr_exit_gap(sub, section, graph, layers, x_spacing)
    _apply_label_strike_runway(sub, section, x_spacing)
    _adjust_lr_label_clearance(sub, section)
    _adjust_terminus_icon_clearance(sub, section, graph)

    return sub


def _align_bypass_v_to_lane_side(
    sub: MetroGraph, section: Section, graph: MetroGraph
) -> None:
    """Seat a bypass V on its section's lane side of the trunk.

    A bypass V (``__bypass_`` helper) carries one line around the station it
    bypasses.  In a vertical-flow (TB/BT) section every lane is drawn on the
    section's lane-sign side of the trunk -- left for a downward (TB) section,
    right for an upward (BT) one or a positive-fan section.  But
    :func:`assign_tracks` slots the V by its line's priority index, which for a
    TB section is the mirror of that side, so a V left on the priority slot
    lands on the wrong side and the fork from the bypassed station's feeder
    crosses the trunk lane (#1163).

    Snap the V onto the lane-side grid column its bypass run should share with
    section content (the nearest real-station column on that side), so the run
    sits on the section's grid rather than drifting to an off-grid lane.  With
    no lane-side station to share, reflect the V across the bypassed station so
    it at least leaves on the side its line is drawn.  Horizontal (LR/RL)
    sections stack lanes on Y in the same order :func:`assign_tracks` uses, so
    they never transpose.
    """
    if not lanes_run_along_x(section.direction):
        return
    bypass_vs = [v for v in sub.stations.values() if is_bypass_v(v.id)]
    if not bypass_vs:
        return

    from nf_metro.layout.routing.reversal import tb_positive_fan_sections

    lane_sign = section_lane_sign(section, tb_positive_fan_sections(graph))
    real_xs = [
        s.x
        for s in sub.stations.values()
        if not is_bypass_v(s.id) and not s.is_port and not s.off_track
    ]
    for v in bypass_vs:
        # The subgraph station is a coordinate-only copy; the bypassed-station
        # link lives on the full-graph station.
        full = graph.stations.get(v.id)
        bypassed_id = full.bypasses_station_id if full else None
        anchor = sub.stations.get(bypassed_id) if bypassed_id else None
        if anchor is None:
            continue
        side_cols = [
            x for x in real_xs if (x - anchor.x) * lane_sign > SAME_COORD_TOLERANCE
        ]
        if side_cols:
            v.x = min(side_cols, key=lambda x: abs(x - v.x))
        elif (v.x - anchor.x) * lane_sign < 0.0:
            v.x = anchor.x - (v.x - anchor.x)


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
    frame = AxisFrame.for_direction(section.direction, x_spacing, y_spacing)
    primary, secondary = frame.primary, frame.secondary

    EPS = SAME_COORD_TOLERANCE
    # Off-track stations carry a placeholder Y here (the off-track lift in
    # Stage 5.2 overwrites it); letting that placeholder occupy a cell would
    # cascade on-track siblings off their true rows.
    real = [
        s
        for s in sub.stations.values()
        if not s.is_port and not s.is_hidden and not s.off_track
    ]
    if len(real) < 2:
        return

    # Group stations by primary-axis bucket (layer column for LR/RL,
    # row for TB).  Use the primary-axis step size; the bucket spans a
    # half-step either side of a layer centre so off-grid layer_extra
    # offsets stay in the same bucket as their layer peers.
    primary_step_norm = max(primary.step, 1.0)
    by_primary: dict[float, list[Station]] = {}
    for s in real:
        bucket = round(primary.get(s) / primary_step_norm)
        by_primary.setdefault(bucket, []).append(s)

    # Stable tiebreaker so the earlier-defined station keeps its slot
    # when two share a secondary coord (insertion order in sub.stations).
    order = {sid: i for i, sid in enumerate(sub.stations)}

    for stations in by_primary.values():
        if len(stations) < 2:
            continue
        stations.sort(key=lambda s: (secondary.get(s), order.get(s.id, 0)))
        # A cell blocks a newcomer only when the two cannot share it: markerless
        # bypass-V helpers carrying one tight exit-bound bundle may stack on a
        # single track (the offset engine spreads their lines by track_gap),
        # but a bypass-V and a real marker, or two real markers, may not.
        used: list[tuple[float, bool]] = []
        for s in stations:
            pos = secondary.get(s)
            s_is_v = is_bypass_v(s.id)
            while any(
                abs(pos - u) < secondary.step - EPS and not (s_is_v and u_is_v)
                for u, u_is_v in used
            ):
                pos += secondary.step
            if pos != secondary.get(s):
                secondary.set(s, pos)
            used.append((pos, s_is_v))


def _multiline_track_spacing(sub: MetroGraph, y_spacing: float) -> float:
    """Return effective Y track spacing, widened for multi-line labels.

    When labels from adjacent tracks face each other (one below, one
    above due to layer alternation) the track gap must be large enough
    for both labels plus clearance.  Returns *y_spacing* unchanged when
    no multi-line labels are present.
    """
    font_height = FONT_HEIGHT * active_font_scale()
    max_text_h = font_height
    for s in sub.stations.values():
        if "\n" in s.label:
            max_text_h = max(max_text_h, _label_text_height(s.label))

    if max_text_h <= font_height:
        return y_spacing  # no multi-line labels

    # Worst case: adjacent tracks with labels facing inward.
    # Each side needs label_offset + its text height.
    min_gap = LABEL_OFFSET + max_text_h + LABEL_OFFSET + font_height + LABEL_MARGIN
    return max(y_spacing, min_gap)


def _multiline_label_padding(sub: MetroGraph) -> float:
    """Return extra bbox Y padding for the tallest multi-line label."""
    font_height = FONT_HEIGHT * active_font_scale()
    max_extra = 0.0
    for s in sub.stations.values():
        if "\n" in s.label:
            extra = _label_text_height(s.label) - font_height
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


def _mirror_primary(sub: MetroGraph, axis: Axis) -> None:
    """Mirror the layer (primary) axis so layer 0 sits at the far end (RL).

    Anchors on non-terminus stations so adding terminus layers
    extends outward without shifting the entry point.
    """
    non_term = [s for s in sub.stations.values() if not (s.is_blank_terminus)]
    anchor_stations = non_term if non_term else list(sub.stations.values())
    max_val = max(axis.get(s) for s in anchor_stations)
    for s in sub.stations.values():
        axis.set(s, max_val - axis.get(s))


def _enforce_min_extent(
    sub: MetroGraph,
    section: Section,
    x_spacing: float,
    y_spacing: float,
) -> None:
    """Ensure minimum inner extent so stations sit on visible track."""
    axis = AxisFrame.for_direction(section.direction, x_spacing, y_spacing).primary
    vals = [axis.get(s) for s in sub.stations.values()]
    inner = max(vals) - min(vals)
    if inner < axis.step:
        shift = (axis.step - inner) / 2
        for station in sub.stations.values():
            axis.set(station, axis.get(station) + shift)


def _adjust_tb_labels(sub: MetroGraph, section: Section) -> None:
    """Vertical-flow sections: expand bbox and shift stations along the lane
    axis so side-placed labels fit.

    A vertical flow (TB/BT) stacks its lines along X and places labels beside
    the pill (extending leftward, ``text_anchor=end``), so the lane-axis (X)
    extent must be reserved.  Horizontal flows place labels above/below and
    reserve no extra X here.
    """
    if lanes_run_along_y(section.direction):
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
    """Shift TB section stations down to clear a perpendicular entry port.

    A TB TOP/BOTTOM entry port is flow-aligned, so :func:`position_ports`
    (Stage 3.1) seats it on its connected station's X -- the section trunk X --
    and nothing later pulls it off.  Its drop onto the first station is then a
    clean vertical continuation and the cross-column lead-in turns in the
    header corridor above the box, never inside it, so no in-section room is
    needed for it.  Only a perpendicular (LEFT/RIGHT) entry, whose port would
    otherwise coincide with the first station, needs the stations nudged down.
    On a horizontal-flow section Stage 3.1 seats a perpendicular (TOP/BOTTOM)
    port on a station's X the same way, but there the station-as-elbow
    constraint forbids the coincidence surviving and Stage 3.2's entry-port
    alignment moves it clear."""
    if section.direction != "TB":
        return

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


def _adjust_lr_entry_inset(
    sub: MetroGraph,
    section: Section,
    graph: MetroGraph,
    x_spacing: float,
) -> None:
    """LR/RL sections: add extra bbox width when entry has curves."""
    if lanes_run_along_x(section.direction):
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
    if lanes_run_along_x(section.direction):
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
            if is_bypass_v(src_id):
                pred_y = None
                for pe in graph.edges_to(src_id):
                    if pe.source in real_ids and not is_bypass_v(pe.source):
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


def _apply_label_strike_runway(
    sub: MetroGraph,
    section: Section,
    x_spacing: float,
) -> None:
    """LR/RL sections: lengthen the entry and/or exit runway by whole columns.

    A label wider than its station's flat run lets a boundary-fan diagonal seat
    inside the label's x-extent.  Adding grid columns of runway on the struck
    side (the entry side shifts stations along the flow so room opens before the
    first column; the exit side grows the bbox past the last) moves the diagonal
    transition clear of the label, keeping the column pitch fixed.  The
    strike-clearance loop sets these only for the side of a section whose label
    is actually struck, so this is a no-op (zero columns) for every other
    render.
    """
    if section.direction not in ("LR", "RL"):
        return
    entry = section.label_strike_entry_cols * x_spacing
    exit_room = section.label_strike_exit_cols * x_spacing
    # Entry is the flow-source boundary (left for LR, right for RL); shifting
    # stations along the flow opens room between it and the first column.
    if section.direction == "RL":
        entry, exit_room = exit_room, entry
    if entry > 0:
        for s in sub.stations.values():
            s.x += entry
        section.bbox_w += entry
    if exit_room > 0:
        section.bbox_w += exit_room


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

    ``TERMINUS_ICON_CLEARANCE`` bakes in the default (unscaled) icon width;
    under an active ``font_scale`` the icon itself grows to keep its label
    from being shrink-to-fit clamped, so the excess over that default width
    is added back in to keep the reservation matching the drawn icon.
    """
    icon_width_growth = terminus_width_approx() - TERMINUS_WIDTH
    if n_icons <= 1:
        return TERMINUS_ICON_CLEARANCE + icon_width_growth
    extra = (n_icons - 1) * _terminus_icon_step(n_icons, names)
    return TERMINUS_ICON_CLEARANCE + icon_width_growth + extra


def _terminus_icon_step(n_icons: int, names: list[str] | None) -> float:
    """Centre-to-centre step between adjacent icons of one terminus.

    Mirrors the renderer's ``caption_aware_icon_step``, which widens the
    default ``ICON_INTER_GAP`` step when adjacent captions would overrun it.
    Layout doesn't know the theme, so caption widths are estimated using the
    default label size (14px, matches built-in themes); slight over-budget is
    harmless, costing only a few px of section padding.
    """
    from nf_metro.render.constants import ICON_NAME_FONT_SCALE
    from nf_metro.render.svg import caption_aware_icon_step

    safe_names = names or [""] * n_icons
    caption_style = text_style(14.0 * ICON_NAME_FONT_SCALE, "bold")
    name_widths = [
        DEFAULT_TEXT_METRICS.reserve_width(name, caption_style, TextRole.ICON_CAPTION)
        for name in safe_names
    ]
    return caption_aware_icon_step(safe_names, name_widths, terminus_width_approx())


def terminus_icon_flow_reach(station: Station) -> float:
    """Px from a terminus's marker centre to the far edge of its icon run.

    The *drawn* extent along the section's flow axis: the marker radius, the
    marker-to-icon gap, the icon body, a stacked ``files:`` back page's peek
    past it, and one step per additional icon.  Unlike
    :func:`_terminus_icon_clearance` this carries no cosmetic section-border
    margin, so reserving exactly this leaves the icon touching the box edge --
    which is what a caller wants when it is topping up padding that already
    supplies its own visual breathing room.
    """
    from nf_metro.render.constants import FILES_ICON_OFFSET_RATIO

    n_icons = max(1, len(station.terminus_labels))
    width = terminus_width_approx()
    stacked = (
        width * FILES_ICON_OFFSET_RATIO
        if ICON_TYPE_FILES in (station.terminus_icon_types or [])
        else 0.0
    )
    extra = (
        (n_icons - 1) * _terminus_icon_step(n_icons, station.terminus_names)
        if n_icons > 1
        else 0.0
    )
    return station_radius_approx() + TERMINUS_ICON_GAP + width + stacked + extra


def _icon_caption_height(line_count: int = 1) -> float:
    """Drawn height of a terminus caption with *line_count* lines."""
    from nf_metro.render.constants import ICON_NAME_FONT_SCALE

    style = text_style(FONT_HEIGHT * ICON_NAME_FONT_SCALE * active_font_scale(), "bold")
    if style.face is MetricsFace.FALLBACK:
        first_line_height = style.font_size
    else:
        first_line_height = DEFAULT_TEXT_METRICS.ink_bbox(
            "Ag", style, TextRole.ICON_CAPTION
        ).height
    return first_line_height + max(0, line_count - 1) * style.font_size


def _terminus_icon_clearance_vertical(
    n_icons: int,
    caption_line_count: int = 0,
) -> float:
    """Vertical clearance for *n_icons* file icons stacked along the flow axis.

    TB/BT counterpart of ``_terminus_icon_clearance``: icons stack along Y,
    so each additional icon adds the icon height plus (when captions are
    present) a caption row, matching the renderer's TB step.

    ``TERMINUS_ICON_CLEARANCE_V`` bakes in the default (unscaled) icon
    height; under an active ``font_scale`` the icon itself grows, so the
    excess over that default height is added back in (see
    ``_terminus_icon_clearance``'s matching horizontal-axis note).
    """
    extra_caption_height = (
        _icon_caption_height(caption_line_count) - _icon_caption_height()
        if caption_line_count > 1
        else 0.0
    )
    icon_height_growth = 2 * (icon_half_height_approx() - ICON_HALF_HEIGHT)
    base = TERMINUS_ICON_CLEARANCE_V + extra_caption_height + icon_height_growth
    if n_icons <= 1:
        return base
    caption_room = (
        ICON_CAPTION_GAP + _icon_caption_height(caption_line_count)
        if caption_line_count
        else 0.0
    )
    step = 2 * icon_half_height_approx() + ICON_INTER_GAP + caption_room
    return base + (n_icons - 1) * step


def _terminus_icon_flow_overhang(
    n_icons: int,
    caption_line_count: int = 0,
) -> float:
    """Distance from a TB/BT terminus's marker to the far edge of its icons.

    The drawn flow-axis extent of the icon stack (icon body height plus a
    caption row when captioned, times the icon count), matching what
    ``render.svg`` draws.  Unlike :func:`_terminus_icon_clearance_vertical`
    this omits the section-border visual margin: it locates the icon's
    *edge* so an exit corridor can be placed just clear of it, rather than
    reserving bbox padding.
    """
    caption_room = (
        ICON_CAPTION_GAP + _icon_caption_height(caption_line_count)
        if caption_line_count
        else 0.0
    )
    body = station_radius_approx() + TERMINUS_ICON_GAP + 2 * icon_half_height_approx()
    step = 2 * icon_half_height_approx() + ICON_INTER_GAP + caption_room
    return body + caption_room + (n_icons - 1) * step


def terminus_cross_extents(station: Station) -> tuple[float, float]:
    """(above, below) px an LR/RL terminus's icon reaches past its marker centre.

    LR/RL icons march horizontally, so their vertical reach is the icon body's
    half-height plus two asymmetric additions: a stacked ``files:`` icon's back
    page peeks *upward* past the front page, and a captioned icon hangs its
    name -- however many lines it wraps onto -- *below*.  Grid pitch has to
    clear the sum of an upper icon's ``below`` and a lower icon's ``above`` or
    the two collide, which is what an active ``font_scale`` provokes since every
    term here scales with it.
    """
    from nf_metro.render.constants import FILES_ICON_OFFSET_RATIO

    half = icon_half_height_approx()
    above = half
    if ICON_TYPE_FILES in (station.terminus_icon_types or []):
        above += terminus_width_approx() * FILES_ICON_OFFSET_RATIO
    below = half
    line_count = station.terminus_caption_line_count
    if line_count:
        below += ICON_CAPTION_GAP + _icon_caption_height(line_count)
    return above, below


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
    if not station.is_terminus or lanes_run_along_y(section_dir):
        return 0.0, 0.0
    is_source = not graph.edges_to(station.id)
    extent = _terminus_icon_clearance_vertical(
        len(station.terminus_labels), station.terminus_caption_line_count
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
    icons_march_on_y = lanes_run_along_x(section_dir)

    for station in sub.stations.values():
        if not station.is_terminus:
            continue

        n_icons = len(station.terminus_labels)
        is_source = not graph.edges_to(station.id)
        extends_forward = _terminus_icons_extend_forward(is_source, section_dir)

        if icons_march_on_y:
            needed = _terminus_icon_clearance_vertical(
                n_icons, station.terminus_caption_line_count
            )
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
) -> bool:
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

    Returns whether any section's bbox grew: only a grow changes a section's
    footprint, so it is the sole trigger for re-checking inter-column gaps.
    """
    desired_gap = x_spacing * ENTRY_SHIFT_LR
    any_grew = False

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

        # Compute the current gap between port and nearest entry-side station.
        # The perp entry drops on the side of the trunk opposite the flow-axis
        # exit; internal stations then shift away from that side.
        run_lo, run_hi = min(internal_xs), max(internal_xs)
        entry_on_left = perp_entry_lands_left(section, graph)
        if entry_on_left:
            port_x = min(perp_port_xs)
            current_gap = run_lo - port_x
        else:
            port_x = max(perp_port_xs)
            current_gap = port_x - run_hi

        shift = desired_gap - current_gap
        if shift <= 0:
            continue  # gap is already sufficient

        # Shift internal stations away from the entry side.
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            s = graph.stations.get(sid)
            if not s or s.is_port:
                continue
            if entry_on_left:
                s.x += shift
            else:
                s.x -= shift

        # Keep the shifted run inside the bbox. `left_pad` is the run's left
        # clearance before the shift.
        left_pad = run_lo - section.bbox_x
        drop_in_span = run_lo <= port_x <= run_hi
        grew = True
        if not entry_on_left and left_pad < shift:
            # The run shifts left past the section's own left edge, so the box
            # grows left by the shift to preserve `left_pad` there. The entry
            # port is then the rightmost content, and its exit-side clearance
            # mirrors the run's left padding.
            section.bbox_x -= shift
            section.bbox_w = (port_x + left_pad) - section.bbox_x
        elif entry_on_left and port_x > run_hi:
            # The run shifts right of the drop, leaving the entry port as the
            # leftmost content. The left edge anchors on the port with the run's
            # padding; the right edge keeps its pre-shift clearance past the run.
            right_pad = (section.bbox_x + section.bbox_w) - run_hi
            section.bbox_x = port_x - left_pad
            section.bbox_w = (run_hi + shift + right_pad) - section.bbox_x
        elif entry_on_left and drop_in_span and shift > desired_gap:
            # The drop sits within the run; `_adjust_lr_entry_inset` reserves one
            # `desired_gap` of width, so only the shift beyond that reserve adds
            # width on the right.
            section.bbox_w += shift - desired_gap
        else:
            grew = False

        if grew:
            _repin_flow_axis_exit_ports(section, graph)
            any_grew = True

    return any_grew


def _repin_flow_axis_exit_ports(section: Section, graph: MetroGraph) -> None:
    """Snap an LR/RL section's flow-axis exit ports back onto their bbox edge.

    A LEFT/RIGHT exit port's X is fixed on the section edge in Stage 3.1, but
    the Stage 3.3 runway grow moves that edge; without re-pinning the port
    stays on the old edge and its exit leg doubles back over the run.  Only the
    flow-axis X moves -- the port keeps its carrying-station Y, so the
    station-as-elbow constraint is untouched -- and perpendicular (TOP/BOTTOM)
    ports are left alone.
    """
    right_x = section.bbox_x + section.bbox_w
    for pid in section.exit_ports:
        port = graph.ports.get(pid)
        station = graph.stations.get(pid)
        if not port or not station:
            continue
        if port.side == PortSide.LEFT:
            station.x = section.bbox_x
        elif port.side == PortSide.RIGHT:
            station.x = right_x
        else:
            continue
        port.x = station.x
