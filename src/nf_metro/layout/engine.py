"""Layout coordinator: combines layer assignment, ordering, and coordinate mapping.

Section-first layout: sections are laid out independently, then placed on a meta-graph.
"""

from __future__ import annotations

__all__ = [
    "BackwardFlowError",
    "MixedEntryDirectionError",
    "PhaseInvariantError",
    "compute_layout",
    "compute_min_y_spacing",
]

import warnings
from collections.abc import Callable

from nf_metro.layout.constants import (
    DESCENDER_CLEARANCE,
    FONT_HEIGHT,
    ICON_CAPTION_FONT_HEIGHT,
    ICON_CAPTION_GAP,
    ICON_HALF_HEIGHT,
    ICON_STACK_LABEL_CLEARANCE,
    LABEL_OFFSET,
    MIN_Y_SPACING_FLOOR,
    ROW_GAP,
    SECTION_GAP,
    SECTION_X_GAP,
    SECTION_X_PADDING,
    SECTION_Y_GAP,
    SECTION_Y_PADDING,
    X_OFFSET,
    X_SPACING,
    Y_OFFSET,
    Y_SPACING,
)
from nf_metro.layout.layers import assign_layers
from nf_metro.layout.ordering import assign_tracks
from nf_metro.layout.phases._common import (  # noqa: F401
    _bbox_cols_overlap,
    _build_section_subgraph,
    _canvas_width,
    _classify_multi_station_ys,
    _classify_section_station_ys,
    _expand_bbox_for_y,
    _fan_offsets,
    _grid_group_section_ids,
    _grid_rows_top_to_bottom,
    _grow_section_bbox_downward,
    _grow_section_bbox_upward,
    _max_stations_per_layer,
    _route_crosses_section_boundary,
    _row_contiguous_column_groups,
    _scoped_sections,
    _section_bundle_lines,
    _section_trunk_y,
    _snapshot_placement_refs,
    _station_marker_bbox,
    first_vertical_leg_x,
    is_loop_side_branch_station,
)
from nf_metro.layout.phases.balancing import (  # noqa: F401
    _balance_direct_external_feeder_ys,
    _balance_section_content_around_trunk,
    _compact_below_trunk_band,
    _fan_free_content_upward,
    _fan_source_inputs_upward,
    _recenter_loop_side_stations,
    _shift_and_propagate_loop_stations,
    _shift_linear_consumer_chain,
    _shift_sparse_loop_stations_to_clear_bundle,
    _snap_inter_section_port_pairs,
)
from nf_metro.layout.phases.bbox import (  # noqa: F401
    _aggregate_bypass_spans,
    _fit_bboxes_to_content_top,
    _lift_would_cause_uturn,
    _loop_corner_x,
    _min_section_bbox_top,
    _predicted_bypass_bottom_in_row,
    _push_lower_rows_after_bbox_grow,
    _section_fit_top,
    _shrink_and_tighten_rows,
    _shrink_bboxes_to_content_bottom,
    _snapshot_struct_heights_below_top,
    _tighten_lower_rows_after_shrink,
)
from nf_metro.layout.phases.canvas import (  # noqa: F401
    _renumber_sections_by_grid,
    _shift_graph_into_canvas,
    _translate_graph_y,
)
from nf_metro.layout.phases.fan_bundles import (  # noqa: F401
    _apply_half_grid_2branch_symfan,
    _convergence_source_ys,
    _divergence_target_ys,
    _recenter_full_bundle_columns,
    _redistribute_fanout_siblings,
    _redistribute_full_bundle_columns,
    _section_symfan_uses_half_grid,
)
from nf_metro.layout.phases.grid_snap import (  # noqa: F401
    _snap_all_y_to_grid,
    _snap_canvas_y_to_grid,
)
from nf_metro.layout.phases.guards import (  # noqa: F401
    _BISECTION_FIRST_VALID,
    _FLOW_ALIGNED_SIDES,
    _PASS_C_BISECTION_ORDER,
    BackwardFlowError,
    MixedEntryDirectionError,
    PhaseInvariantError,
    _bbox_guarded_stations,
    _bisection_should_run,
    _ensure_routes,
    _guard_anchors_frozen_during_placement,
    _guard_bundle_order_preserved,
    _guard_bypass_port_no_slot_gaps,
    _guard_bypass_v_flat_visible,
    _guard_centered_line_spread_balanced,
    _guard_concentric_bundle_corners,
    _guard_coordinates_finite,
    _guard_entry_approach_from_port_side,
    _guard_entry_port_fed_only_by_ports,
    _guard_exit_inherits_entry_bundle_order,
    _guard_explicit_grid_directions,
    _guard_fan_bundles_coincide_or_separate,
    _guard_fanout_junction_resolves_upstream,
    _guard_fanout_junction_shares_exit_port_y,
    _guard_fanout_tail_join,
    _guard_feeder_exits_section_through_side,
    _guard_file_icon_no_name_label,
    _guard_flow_exit_anchored_to_carrier,
    _guard_independent_components_disjoint,
    _guard_inter_row_run_clearance,
    _guard_inter_section_descent_edge_clearance,
    _guard_inter_section_route_no_backtrack,
    _guard_inter_section_route_no_full_width_backtrack,
    _guard_inter_section_routes_in_row_band,
    _guard_interchange_bar_clears_non_members,
    _guard_merge_port_approach_side,
    _guard_merge_port_outgoing_side_preserved,
    _guard_no_artefactual_counter_flow,
    _guard_no_coincident_station_coords,
    _guard_no_collinear_distinct_lines,
    _guard_no_diagonal_strikes_horizontal_label,
    _guard_no_dogleg_crosses_exempt_trunk,
    _guard_no_intra_section_collinear_distinct_lines,
    _guard_no_label_overlap,
    _guard_no_line_crosses_file_icon,
    _guard_no_line_crosses_non_consumer,
    _guard_no_line_strikes_label,
    _guard_no_mixed_entry_directions,
    _guard_no_negative_grid_columns,
    _guard_no_opposing_line_overlap,
    _guard_no_route_through_section,
    _guard_no_same_line_parallel_descents,
    _guard_no_same_row_backward_feed,
    _guard_no_split_same_line_fanout_descents,
    _guard_no_stacked_elbow_graze,
    _guard_no_station_overlap,
    _guard_no_wrapped_label_trunk_strike,
    _guard_off_track_clear_of_anchor,
    _guard_off_track_consumer_on_trunk,
    _guard_off_track_input_column_stack,
    _guard_off_track_output_clears_non_producer,
    _guard_partial_branch_offset_gaps,
    _guard_perp_entry_boundary_consistent,
    _guard_perp_entry_feed_not_collinear,
    _guard_perp_exit_over_leadin_no_overdip,
    _guard_ports_on_boundaries,
    _guard_rail_above_label_band,
    _guard_rail_one_station_per_column,
    _guard_rail_stations_seat_on_rails,
    _guard_right_entry_drop_in_when_clear,
    _guard_routes_enter_sections_at_ports,
    _guard_row_gaps,
    _guard_row_trunk_cy_consistent,
    _guard_section_bboxes_positive,
    _guard_section_top_padding,
    _guard_serpentine_no_backtrack,
    _guard_single_trunk_off_track_step,
    _guard_station_x_column_drift,
    _guard_stations_in_sections,
    _guard_stations_within_bbox,
    _guard_tall_anchor_stack_well_formed,
    _guard_tb_exit_corner_column_order,
    _guard_tb_top_entry_drop_hugs_top,
    _guard_terminus_icons_within_bbox,
    _guard_topmost_row_top_entry_hugs_section,
    _guard_trunk_bands_crossing_optimal,
    _inter_section_backtrack_legs,
    _port_anchor_snapshot,
    _route_exit_side,
    _run_pass_c_guards,
    _section_lacks_flow_aligned_port,
    inter_section_route_backtrack_legs,
    iter_line_label_strikes,
    run_validate_guards,
)
from nf_metro.layout.phases.junctions import (  # noqa: F401
    _junction_incoming_line_count,
    _junction_outgoing_line_count,
    _position_junctions,
    _position_merge_junction,
    _required_junction_margin,
    _resolve_source_section_id,
    _resolve_source_xy,
)
from nf_metro.layout.phases.off_track import (  # noqa: F401
    _align_phantom_pass_throughs,
    _bump_off_track_clear_of_trunks,
    _compute_fork_join_gaps,
    _insert_phantom_pass_throughs,
    _lift_off_track_stations,
    _line_crossed_file_icon_sinks,
    _off_track_groups,
    _off_track_output_below,
    _place_off_track_relative_to_anchors,
    _reanchor_off_track_to_consumer,
)
from nf_metro.layout.phases.ports import (  # noqa: F401
    _align_entry_ports,
    _align_exit_ports,
    _align_lr_entry_port,
    _align_lr_exit_port,
    _align_ports_to_downstream,
    _align_tb_entry_port,
    _align_tb_section_bbox_bottoms,
    _clamp_tb_entry_port,
    _nudge_port_from_stations,
    _propagate_through_junctions,
    _push_ports_from_termini,
    _push_termini_from_port,
    _resolve_downstream_entry_y,
    _resolve_tb_exit_y,
    _set_port_x,
    _set_port_y,
    _snap_grid_group_entry_ports,
    _snap_grid_group_exit_ports,
    _snap_sole_layer_stations_to_ports,
    _space_ports_from_termini,
)
from nf_metro.layout.phases.row_align import (  # noqa: F401
    _align_row_trunk_ys,
    _align_row_y_grids,
    _compact_row_content_to_bbox_top,
    _recompute_grid_group_bboxes,
    _top_align_row_bboxes_only,
    _top_align_row_sections,
)
from nf_metro.layout.phases.single_section import (  # noqa: F401
    _adjust_lr_entry_inset,
    _adjust_lr_exit_gap,
    _adjust_lr_label_clearance,
    _adjust_tb_entry_shifts,
    _adjust_tb_labels,
    _adjust_terminus_icon_clearance,
    _align_terminus_to_upstream,
    _enforce_min_extent,
    _has_horizontal_predecessor_section,
    _layout_single_section,
    _mirror_rl,
    _multiline_label_padding,
    _multiline_track_spacing,
    _normalize_min,
    _resolve_station_collisions,
    _shift_lr_perp_entry_stations,
    _terminus_icon_clearance,
    _terminus_icon_clearance_vertical,
    _terminus_icons_extend_forward,
    _terminus_y_overhang,
)
from nf_metro.layout.phases.snapshots import (
    capture_phase_snapshot,
    phase_snapshots_enabled,
)
from nf_metro.layout.phases.spacing import (  # noqa: F401
    _MAX_SPREAD_ITERS,
    _SPREAD_SLACK,
    _bypass_label_obstacles,
    _label_clearance_issues,
    _residual_label_overlaps,
    _spread_bump,
    _struck_stations_and_collinear,
)
from nf_metro.parser.model import LineSpread, MetroGraph, Section, is_bypass_v
from nf_metro.parser.validate import (
    CyclicGraphError,
    find_cycle,
    format_cycle_error,
)

# ---------------------------------------------------------------------------
# Stage-boundary guards
# ---------------------------------------------------------------------------

_VALIDATE_DEFAULT = False
"""Set to True to enable stage-boundary invariant checks.

Controlled by the ``validate`` parameter on ``compute_layout``.
Tests pass ``validate=True`` to catch cross-phase corruption that would
otherwise only surface as subtle visual defects.
"""


def compute_min_y_spacing(
    graph: MetroGraph, floor: float = MIN_Y_SPACING_FLOOR
) -> float:
    """Return the minimum global ``y_spacing`` the graph's content needs.

    Scans every LR/RL section and asks, for any pair of stations that
    could land in vertically-adjacent grid slots in the same column:
    what centre-to-centre pitch is needed for their labels / captioned
    file icons not to collide?

    The four worst-case vertical extents considered are:

    * captioned file-icon below the marker: ``ICON_HALF_HEIGHT +
      ICON_CAPTION_GAP + ICON_CAPTION_FONT_HEIGHT``
    * captioned file-icon above the marker: ``ICON_HALF_HEIGHT``
    * labeled station, label below: ``LABEL_OFFSET + FONT_HEIGHT +
      DESCENDER_CLEARANCE``
    * labeled station, label above: ``LABEL_OFFSET + FONT_HEIGHT +
      DESCENDER_CLEARANCE``

    Required pitch for two stacked elements is
    ``upper.below_extent + lower.above_extent +
    ICON_STACK_LABEL_CLEARANCE``.  We take the worst case across all
    candidate pairs in every LR/RL section, then clamp to ``floor`` so
    a label-light graph stays at the historical default pitch.

    Label-only stations alternate above/below within a column at the
    default pitch, so they're not the binding constraint on their own.
    Captioned file icons can't alternate (caption placement is fixed
    under the icon), so the widening fires when icons enter the mix.

    The result is applied uniformly to the whole render -- the grid
    stays global, no per-section overrides.
    """
    from nf_metro.layout.labels import active_font_scale

    scale = active_font_scale()
    icon_below = ICON_HALF_HEIGHT + ICON_CAPTION_GAP + ICON_CAPTION_FONT_HEIGHT * scale
    icon_above = ICON_HALF_HEIGHT
    label_extent = LABEL_OFFSET + FONT_HEIGHT * scale + DESCENDER_CLEARANCE
    clearance = ICON_STACK_LABEL_CLEARANCE

    pitch_icon_icon = icon_above + icon_below + clearance
    # icon_over_label uses icon_below (the larger extent), so it
    # subsumes the label-over-icon case which uses icon_above.
    pitch_icon_over_label = icon_below + label_extent + clearance

    required = floor
    if not graph.sections:
        return required

    for section in graph.sections.values():
        if section.direction not in ("LR", "RL"):
            continue
        captioned = 0
        labeled = 0
        for sid in section.station_ids:
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden:
                continue
            has_caption = st.is_terminus and any(
                bool(n) for n in (st.terminus_names or [])
            )
            has_label = bool(st.label) and not st.is_terminus
            if has_caption:
                captioned += 1
            elif has_label:
                labeled += 1
        if captioned >= 2:
            required = max(required, pitch_icon_icon)
        if captioned >= 1 and labeled >= 1:
            required = max(required, pitch_icon_over_label)

    return required


def compute_layout(
    graph: MetroGraph,
    x_spacing: float | None = None,
    y_spacing: float | None = None,
    x_offset: float = X_OFFSET,
    y_offset: float = Y_OFFSET,
    row_gap: float = ROW_GAP,
    section_gap: float = SECTION_GAP,
    section_x_padding: float = SECTION_X_PADDING,
    section_y_padding: float = SECTION_Y_PADDING,
    section_x_gap: float | None = None,
    section_y_gap: float | None = None,
    validate: bool = _VALIDATE_DEFAULT,
) -> None:
    """Compute layout positions for all stations in the graph.

    The spacing and section-gap arguments default to ``None``, meaning "read
    the graph's field" (``graph.x_spacing`` etc., set by a ``%%metro``
    directive).  An explicit value passed here overrides that field, so the
    cascade is CLI flag > directive > auto/default.

    When ``y_spacing`` resolves to ``None`` it is derived from the graph's
    content via ``compute_min_y_spacing`` so renders adapt to captioned icons
    and labelled stations automatically.

    If an explicit value is below the minimum the content needs, a
    ``UserWarning`` is emitted: the render is honoured at the requested
    pitch, but labels and captioned file-icons may collide.  Omit
    ``y_spacing`` to let the engine pick a safe value.

    When *validate* is True, stage-boundary invariant checks run after
    key phases.  Violations raise ``PhaseInvariantError`` instead of
    silently producing broken layouts.
    """
    witness = find_cycle(graph)
    if witness is not None:
        raise CyclicGraphError(format_cycle_error(witness))

    if x_spacing is None:
        x_spacing = graph.x_spacing
    if y_spacing is None:
        y_spacing = graph.y_spacing
    if section_x_gap is None:
        section_x_gap = (
            graph.section_x_gap if graph.section_x_gap is not None else SECTION_X_GAP
        )
    if section_y_gap is None:
        section_y_gap = (
            graph.section_y_gap if graph.section_y_gap is not None else SECTION_Y_GAP
        )

    # Read the phase-snapshot enable flag once (issue #363) and stash it on
    # the graph so per-stage call sites can snapshot without signature churn.
    # Off by default: when unset, each _snap call is a single attribute read.
    graph._phase_snapshots_enabled = phase_snapshots_enabled()

    from nf_metro.layout.labels import font_scale_context

    with font_scale_context(graph.font_scale):
        _compute_layout_scaled(
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


def _compute_layout_scaled(
    graph: MetroGraph,
    *,
    x_spacing: float | None,
    y_spacing: float | None,
    x_offset: float,
    y_offset: float,
    section_x_padding: float,
    section_y_padding: float,
    section_x_gap: float,
    section_y_gap: float,
    validate: bool,
) -> None:
    """Layout body, run under the graph's active font-scale context."""

    # Diagonal labels are a horizontal-trunk feature: a TB section places its
    # labels beside vertical pills, where the same tilt doesn't read, so an
    # angled graph that also has a TB section would mix tilted and horizontal
    # labels on one map.  Decline the angle outright in that case (warn and
    # fall back to horizontal everywhere) rather than ship a mixed-orientation
    # map.  Mutating graph.label_angle makes both layout and the renderer agree.
    if graph.label_angle and any(
        sec.direction == "TB" for sec in graph.sections.values()
    ):
        warnings.warn(
            f"label_angle={graph.label_angle!r} ignored: diagonal labels are "
            f"not applied to a graph containing a TB section (the whole map "
            f"would need to tilt to stay consistent). Labels stay horizontal.",
            UserWarning,
            stacklevel=2,
        )
        graph.label_angle = 0.0

    # A diagonal label angle drives one graph-wide column pitch shared by every
    # section (rail and normal alike), so spacing is a property of the whole
    # render, not of any one section.  Used as the default x_spacing when the
    # caller didn't pin one.
    from nf_metro.layout.labels import diagonal_label_pitch

    default_x_spacing = diagonal_label_pitch(graph, X_SPACING)

    # Opt-in rail mode runs a dedicated, self-contained layout pipeline and
    # returns early so the normal phase pipeline below is never touched when
    # rail mode is off (default).  See layout/rail_mode.py.
    if graph.line_spread is LineSpread.RAILS:
        from nf_metro.layout.rail_mode import compute_rail_layout

        rail_y = y_spacing if y_spacing is not None else compute_min_y_spacing(graph)
        rail_x = x_spacing if x_spacing is not None else default_x_spacing
        compute_rail_layout(
            graph,
            x_spacing=rail_x,
            y_spacing=rail_y,
            x_offset=x_offset,
            y_offset=y_offset,
            section_x_padding=section_x_padding,
            section_y_padding=section_y_padding,
            section_y_gap=section_y_gap,
        )
        _guard_stations_within_bbox(graph, "final")
        return

    auto_x = x_spacing is None
    auto_y = y_spacing is None
    if y_spacing is None:
        # The base content pitch before the spread loop widens it for
        # diagonal labels.  A single-trunk section's off-track lift step
        # stays at this base so a widened pitch doesn't strand the icon far
        # above the trunk (issue #580).  Only recorded when y_spacing is
        # auto-resolved; an explicit pin is honoured verbatim.
        y_spacing = compute_min_y_spacing(graph)
        graph._base_y_spacing = y_spacing
    else:
        min_required = compute_min_y_spacing(graph)
        if y_spacing < min_required - 1e-6:
            warnings.warn(
                f"explicit y_spacing={y_spacing!r} is below the minimum "
                f"({min_required:.1f}) this graph's content requires; "
                f"labels and captioned file-icons may collide. "
                f"Omit --y-spacing to let the engine pick a safe value.",
                UserWarning,
                stacklevel=2,
            )
    if x_spacing is None:
        x_spacing = default_x_spacing

    # Optionally reorder lines by section span before layout.
    # Must happen here (on the full graph) before section subgraphs are
    # built, since subgraphs share graph.lines via reference.  Done once;
    # the reorder is order-stable across the spread loop below.
    if graph.line_order == "span" and graph.lines:
        from nf_metro.layout.ordering import _reorder_by_span

        new_order = _reorder_by_span(graph, list(graph.lines.keys()))
        graph.lines = {lid: graph.lines[lid] for lid in new_order}

    # Spread loop: lay out, then if labels still collide at this pitch
    # (after wrapping has done what it can), widen the auto-resolved
    # spacing and lay out again.  A clean layout clears on the first pass
    # so nothing is widened; only crowded wide-label graphs iterate.  When
    # the caller pins both spacings explicitly there is nothing to widen,
    # so a single pass runs.
    max_iters = _MAX_SPREAD_ITERS if (auto_x or auto_y) else 1
    for attempt in range(max_iters):
        _layout_once(
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
        if attempt == max_iters - 1:
            break
        residual = _residual_label_overlaps(graph, allow_hyphenation=False)
        if not residual:
            break
        new_x, new_y = _spread_bump(
            graph, residual, x_spacing, y_spacing, auto_x, auto_y
        )
        if new_x <= x_spacing + 1e-6 and new_y <= y_spacing + 1e-6:
            break  # can't widen the binding axis (e.g. pinned) -- give up
        x_spacing, y_spacing = new_x, new_y

    _apply_label_strike_clearance(
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

    # Assure file-icon leaf sinks off the trunk by construction: a leaf icon
    # the laid-out routes rake a line across is taken off-track and the layout
    # re-run once, so the off-track machinery lifts it clear of the passing
    # line.  Keyed on an observed crossing (not on icon presence), so an
    # end-of-chain terminus that already sits clear is never disturbed.
    crossed_sinks = _line_crossed_file_icon_sinks(graph)
    if crossed_sinks:
        for sid in crossed_sinks:
            graph.stations[sid].off_track = True
        _layout_once(
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

    _retrofit_section_rails_phase(
        graph,
        x_spacing=x_spacing,
        y_spacing=y_spacing,
        section_x_padding=section_x_padding,
        section_y_padding=section_y_padding,
    )

    # Record the bypassed-station label box behind each bypass V so the router
    # can seat the V's flat-run corners clear of the label.  Read on the render
    # path, so it is populated independent of ``validate``; computed once the
    # layout has fully settled so the box matches what the renderer draws.
    graph.bypass_label_obstacles = _bypass_label_obstacles(graph)

    # Always-on backstop (independent of ``validate``): the settled layout
    # must never leave a station outside its own section bbox.  Runs on the
    # render path so an unsupported directive combination fails loudly
    # instead of shipping a silently-broken diagram (issue #424).
    _guard_stations_within_bbox(graph, "final")
    _snap(graph, "final")

    if validate:
        _guard_no_label_overlap(graph, "final")
        _guard_no_diagonal_strikes_horizontal_label(graph, "final")
        _guard_no_line_strikes_label(graph, "final")
        _guard_bypass_v_flat_visible(graph, "final")
        _guard_no_wrapped_label_trunk_strike(graph, "final")
        _guard_file_icon_no_name_label(graph, "final")
        _guard_no_line_crosses_file_icon(graph, "final")
        _guard_centered_line_spread_balanced(graph, "final")
        _guard_rail_above_label_band(graph, "final")
        _guard_rail_stations_seat_on_rails(graph, "final")
        _guard_rail_one_station_per_column(graph, "final")
        _guard_single_trunk_off_track_step(graph, "final")
        _guard_off_track_consumer_on_trunk(graph, "final")
        _guard_off_track_input_column_stack(graph, "final")
        _guard_interchange_bar_clears_non_members(graph, "final")
        _guard_tb_top_entry_drop_hugs_top(graph, "final")


def _bypass_label_rakes(graph: MetroGraph) -> set[str]:
    # Stations whose name label a bypass-V leg crosses, per the guard's strike
    # definition: it counts a leg's crossing of the station the V routes around
    # but exempts the leg's own diverging/merging endpoint, isolating the
    # bypassed-middle rake the gap lever relocates.
    return {
        s.station_id
        for s in iter_line_label_strikes(graph)
        if is_bypass_v(s.src) or is_bypass_v(s.tgt)
    }


def _clear_bypass_label_rakes(
    graph: MetroGraph,
    *,
    issues: Callable[[], tuple[set[str], bool, set[tuple[str, str, int]]]],
    adjust: Callable[[tuple[str, str, int], int], None],
    lever_value: Callable[[tuple[str, str, int]], int],
    growable_target: Callable[[str], tuple[Section, int] | None],
    relay: Callable[..., None],
    reseat: Callable[[], None],
) -> None:
    """Push a bypassed node out by whole grid columns to clear a wide-label rake.

    Separate from the fan/convergence loop in ``_apply_label_strike_clearance``
    because it grows the gap before the *bypassed* node's layer (lengthening the
    V's leg) rather than a struck station's own runway, and because its probe
    must read the router's flat-run seating (obstacle boxes kept current by
    ``reseat``) so it fires only for a rake the router could not seat clear.  The
    seated boxes are local: the entry value is restored before returning, so the
    fan/convergence loop reasons about unseated routes.
    """
    if not any(
        v.is_hidden and v.bypasses_station_id is not None
        for v in graph.stations.values()
    ):
        return
    entry_obstacles = dict(graph.bypass_label_obstacles)
    graph.bypass_label_obstacles = _bypass_label_obstacles(graph)
    rake_levers = {
        ("gap", target[0].id, target[1])
        for sid in _bypass_label_rakes(graph)
        if (target := growable_target(sid)) is not None
    }
    struck_pre, collinear_pre, _ = issues() if rake_levers else (set(), False, ())
    if not rake_levers or collinear_pre:
        graph.bypass_label_obstacles = entry_obstacles
        return
    for _ in range(_MAX_SPREAD_ITERS):
        for lever in rake_levers:
            adjust(lever, 1)
        reseat()
        struck_now, collinear, _ = issues()
        if collinear:
            for lever in rake_levers:
                adjust(lever, -1)
            break
        # Done once no rake remains and the shift added no new strike (struck_now
        # is a subset of the pre-shift strikes); a residual the gaps cannot clear
        # rolls back wholesale for the guard backstop.
        if not _bypass_label_rakes(graph) and struck_now <= struck_pre:
            break
    else:
        for lever in rake_levers:
            while lever_value(lever) > 0:
                adjust(lever, -1)
    # The grow steps re-laid against seated boxes; settle the chosen levers
    # against the restored (unseated) boxes the next loop expects.
    graph.bypass_label_obstacles = entry_obstacles
    relay()


def _apply_label_strike_clearance(
    graph: MetroGraph,
    *,
    x_spacing: float,
    y_spacing: float,
    x_offset: float,
    y_offset: float,
    section_x_padding: float,
    section_y_padding: float,
    section_x_gap: float,
    section_y_gap: float,
    validate: bool,
) -> None:
    """Lengthen flat runs at struck stations so diagonals clear their labels.

    A fan-in/fan-out, convergence, or descent diagonal that rakes a station's
    name label is cleared by lengthening the flat run at that station by whole
    grid columns (the pitch stays fixed), seating the transition outside the
    label.  Three grid-quantized levers, each a ``(kind, section, layer)``
    triple: the section's entry-side runway, its exit-side runway, and a
    per-column gap before the struck station's layer.  Need-driven: only
    stations the renderer would draw a strike through grow, so a clean layout
    (every gallery render at its default pitch) is left untouched.  Independent
    of pinned vs auto pitch: the room is local, not the global pitch, so it
    applies even when the caller fixed x_spacing.

    A grow step bumps every lever at each struck station (which one relocates a
    given strike is hard to know in advance), then a minimization pass strips
    each lever that turns out not to be load-bearing, so the settled layout
    carries the least extra width that keeps the labels clear.  A step a
    collinear check rejects, or that fails to reduce the struck count, is rolled
    back, so the loop never ships a layout worse than it found.
    """

    def _relay(*, validate_layout: bool = False) -> None:
        # Growth/shrink steps re-lay unvalidated: an intermediate lever width can
        # carry a transient strike or overlap that a later column clears.  The
        # settled layout is re-laid once with ``validate_layout`` so the caller's
        # stage-boundary checks run on the result the phase ships.
        _layout_once(
            graph,
            x_spacing=x_spacing,
            y_spacing=y_spacing,
            x_offset=x_offset,
            y_offset=y_offset,
            section_x_padding=section_x_padding,
            section_y_padding=section_y_padding,
            section_x_gap=section_x_gap,
            section_y_gap=section_y_gap,
            validate=validate_layout,
        )

    def _reseat() -> None:
        # Refresh the obstacle boxes after re-laying so the rake probe's routes
        # carry the router's flat-run seating against the current geometry.
        _relay()
        graph.bypass_label_obstacles = _bypass_label_obstacles(graph)

    def _growable_target(station_id: str) -> tuple[Section, int] | None:
        st = graph.stations.get(station_id)
        if st is None or not st.section_id:
            return None
        sec = graph.sections.get(st.section_id)
        if sec is None or sec.direction not in ("LR", "RL"):
            return None
        if graph.is_rail_section(sec.id):
            return None
        return sec, st.layer

    def _adjust(lever: tuple[str, str, int], delta: int) -> None:
        kind, sid, layer = lever
        sec = graph.sections[sid]
        if kind == "entry":
            sec.label_strike_entry_cols += delta
        elif kind == "exit":
            sec.label_strike_exit_cols += delta
        else:
            lg = sec.label_strike_layer_gaps
            lg[layer] = lg.get(layer, 0) + delta
            if lg[layer] <= 0:
                del lg[layer]

    def _lever_value(lever: tuple[str, str, int]) -> int:
        kind, sid, layer = lever
        sec = graph.sections[sid]
        if kind == "entry":
            return sec.label_strike_entry_cols
        if kind == "exit":
            return sec.label_strike_exit_cols
        return sec.label_strike_layer_gaps.get(layer, 0)

    # A station a bypass V diverges from is the diagonal's source, so its strike
    # is relocated by lengthening the run *after* it: a gap before the next
    # layer pushes the bypassed node a grid column further out.  Fan-in and
    # convergence strikes land on the diagonal's target, cleared by the gap
    # before the struck station's own layer.
    bypass_divergence_sources = {
        edge.source
        for edge in graph.edges
        if is_bypass_v(edge.target) and not is_bypass_v(edge.source)
    }

    def _issues() -> tuple[set[str], bool, set[tuple[str, str, int]]]:
        struck_, collinear_, flat_gaps_ = _label_clearance_issues(graph)
        flat_levers_ = {("gap", sid, layer) for sid, layer in flat_gaps_}
        return struck_, collinear_, flat_levers_

    _clear_bypass_label_rakes(
        graph,
        issues=_issues,
        adjust=_adjust,
        lever_value=_lever_value,
        growable_target=_growable_target,
        relay=_relay,
        reseat=_reseat,
    )

    struck, _, flat_levers = _issues()
    count = len(struck) + len(flat_levers)
    for _ in range(_MAX_SPREAD_ITERS):
        levers: set[tuple[str, str, int]] = set(flat_levers)
        for sid in struck:
            target = _growable_target(sid)
            if target is None:
                continue
            sec, layer = target
            levers |= {
                ("entry", sec.id, 0),
                ("exit", sec.id, 0),
                ("gap", sec.id, layer),
            }
            if sid in bypass_divergence_sources:
                levers.add(("gap", sec.id, layer + 1))
        if not levers:
            break
        for lever in levers:
            _adjust(lever, 1)
        _relay()
        after, collinear, after_flats = _issues()
        after_count = len(after) + len(after_flats)
        if collinear or after_count >= count:
            for lever in levers:
                _adjust(lever, -1)
            _relay()
            break
        struck, flat_levers, count = after, after_flats, after_count

    # Minimization: shrink each grown lever column by column, keeping a drop only
    # while the labels stay clear, no collinear overlay appears, and no bypass-V
    # flat re-collapses, so every lever lands at its least load-bearing value.
    # Skipped entirely when nothing grew, so a clean layout never pays a re-lay
    # (and is never perturbed by one).
    grown: list[tuple[str, str, int]] = (
        [
            ("entry", sid, 0)
            for sid, sec in graph.sections.items()
            if sec.label_strike_entry_cols
        ]
        + [
            ("exit", sid, 0)
            for sid, sec in graph.sections.items()
            if sec.label_strike_exit_cols
        ]
        + [
            ("gap", sid, layer)
            for sid, sec in graph.sections.items()
            for layer in list(sec.label_strike_layer_gaps)
        ]
    )
    if grown:
        for lever in grown:
            while _lever_value(lever) > 0:
                _adjust(lever, -1)
                _relay()
                still_struck, collinear, still_flat = _label_clearance_issues(graph)
                if still_struck or collinear or still_flat:
                    _adjust(lever, 1)
                    break
        _relay(validate_layout=validate)


def _retrofit_section_rails_phase(
    graph: MetroGraph,
    *,
    x_spacing: float,
    y_spacing: float,
    section_x_padding: float,
    section_y_padding: float,
) -> None:
    """Overwrite each rail-flagged section's internal geometry with rail layout.

    The normal pipeline has positioned every section's bbox and inter-section
    ports; this replaces only the *internal* geometry of rail-flagged sections
    (rails + spanning pills), anchored at the bbox the placement chose.  Non-rail
    sections and all inter-section placement keep the normal machinery.
    """
    rail_section_ids = [
        sid
        for sid, mode in graph.line_spread_overrides.items()
        if mode is LineSpread.RAILS
    ]
    if not rail_section_ids or graph.line_spread is LineSpread.RAILS:
        return

    from nf_metro.layout.labels import diagonal_label_pitch_by_section
    from nf_metro.layout.rail_mode import retrofit_section_rails

    section_pitch_map = diagonal_label_pitch_by_section(graph, x_spacing)
    # Rails sit one base grid pitch apart and their labels hang above or below
    # the bundle, so the diagonal-label widening of the global y_spacing (the
    # spread loop, for between-station label clearance) must not also push the
    # rails apart.  Column X still uses the label-aware pitch, where horizontal
    # label room genuinely matters.
    rail_pitch = getattr(graph, "_base_y_spacing", y_spacing)
    for sid in rail_section_ids:
        section = graph.sections.get(sid)
        if section is None:
            continue
        retrofit_section_rails(
            graph,
            section,
            x_spacing=section_pitch_map.get(sid, x_spacing),
            y_spacing=rail_pitch,
            section_x_padding=section_x_padding,
            section_y_padding=section_y_padding,
        )


def _snap(graph: MetroGraph, phase_id: str) -> None:
    """Capture a per-phase coordinate snapshot when enabled (issue #363).

    The enable flag is read once in ``compute_layout`` and stashed on the
    graph so the per-stage call sites stay signature-free; when disabled
    this is a single attribute read plus an early return.
    """
    capture_phase_snapshot(
        graph, phase_id, getattr(graph, "_phase_snapshots_enabled", False)
    )


def _layout_once(
    graph: MetroGraph,
    *,
    x_spacing: float,
    y_spacing: float,
    x_offset: float,
    y_offset: float,
    section_x_padding: float,
    section_y_padding: float,
    section_x_gap: float,
    section_y_gap: float,
    validate: bool,
) -> None:
    """Run one full positioning pass at the given spacing (idempotent)."""
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

    _snap(graph, "flat")


# Names of the content-placement phases observed running through
# :func:`_run_placement` on the most recent ``_compute_section_layout`` pass.
# ``_run_placement`` is the single chokepoint every content phase flows through
# (directly or via :func:`_run_placement_per_row`), so this set is the ground
# truth for "which phases were actually placed".  The completeness meta-test
# (``test_content_placement_phases_complete``) asserts it equals the guarded
# ``CONTENT_PLACEMENT_PHASES`` set, so a new phase wired through the wrapper but
# left unregistered -- hence outside the purity / anchor-frozen guards -- fails
# CI.  See CONTRACT.md (anchor invariant) and #503.
_PLACEMENT_PHASES_RUN: set[str] = set()


def _run_placement(
    graph: MetroGraph,
    validate: bool,
    stage: str,
    fn: Callable[..., None],
    *args: object,
) -> None:
    """Run a content-placement phase, asserting (under validate) it left every
    port anchor frozen.  See CONTRACT.md (anchor invariant)."""
    _PLACEMENT_PHASES_RUN.add(fn.__name__)
    before = _port_anchor_snapshot(graph) if validate else None
    fn(graph, *args)
    if validate and before is not None:
        _guard_anchors_frozen_during_placement(
            graph, before, f"Stage {stage} {fn.__name__}"
        )


def _run_placement_per_row(
    graph: MetroGraph,
    validate: bool,
    stage: str,
    fn: Callable[..., None],
    *args: object,
) -> None:
    """Run a row-local content-placement phase once per grid row, top to bottom.

    Each row's sections read only coordinates within that row, and the phase
    is translation-invariant, so scoping ``graph.sections`` to one row at a
    time reproduces the whole-graph result.  The anchor guard from
    :func:`_run_placement` runs per row.
    """
    for section_ids in _grid_rows_top_to_bottom(graph):
        with _scoped_sections(graph, section_ids):
            _run_placement(graph, validate, stage, fn, *args)


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

    Quick map of the pipeline's structure.  See ``CONTRACT.md`` for the
    full per-sub-stage table with pre/postconditions and invariant
    coverage, and ``docs/dev/layout_pipeline.md`` for the human-facing
    overview.

    Parsing & partition is already done by the parser.  Six stages
    follow:

    1. **Section construction** (Stages 1.1 to 1.5).  Lay out each
       section internally, snap row Y grids, place on the canvas grid,
       renumber by reading order, correct left/top overshoot.  Coords
       stay local.
    2. **Globalise** (Stage 2.1).  Single-stage coord-regime
       transition: translate stations and bboxes to canvas coordinates.
    3. **Pass A - port positioning** (Stages 3.1 to 3.5).  Place ports
       on bbox edges, align entry ports, shift LR/RL perp-entry
       stations, align fold-section exit ports, top-align rows.
    4. **Pass B - downstream alignment & trunk-Y consolidation**
       (Stages 4.1 to 4.10).  Pull ports toward downstream content,
       snap to grid-group stations, space from termini, recompute
       bboxes, align trunk Ys, redistribute fan-out and full-bundle
       columns.
    5. **Pass C - junctions & off-track lift** (Stages 5.1 to 5.5).
       Position junctions, lift off-track stations, re-align row bbox
       tops, compact, snap inter-section port pairs.
    6. **Pass C - vertical settling & finishing** (Stages 6.1 to 6.15).
       Fan content upward, snap to grid, re-anchor off-track, recenter
       full-bundle columns and restore their invariants, balance content
       around trunk, loop-side X recenter, bbox shrink + row tighten /
       push, captioned-icon pad.

    Inline ``# ---- Stage N - ... ----`` dividers below mark each
    stage's start; ``# Stage X.Y:`` comments above each helper call
    name the sub-stage.
    """
    from nf_metro.layout.section_placement import place_sections, position_ports

    # On-track consumers are not yet grid-snapped; the off-track reanchor
    # (Stage 6.6 / 6.8) refuses to run until Stage 6.4 sets this True.
    graph._consumers_grid_snapped = False

    # ---- Stage 1 - Section construction (local coords) ------------------
    # Lay out each section internally, snap row Y grids, place sections on
    # the canvas grid, renumber by reading order, correct left/top
    # overshoot.  All work in section-local coordinates.

    # Stage 1.1: Lay out each section independently (real stations only, no ports)
    from nf_metro.layout.labels import diagonal_label_pitch_by_section

    section_pitch_map = diagonal_label_pitch_by_section(graph, x_spacing)
    section_subgraphs: dict[str, MetroGraph] = {}
    for sec_id, section in graph.sections.items():
        sub = _layout_single_section(
            graph,
            section,
            section_pitch_map.get(sec_id, x_spacing),
            y_spacing,
            section_x_padding,
            section_y_padding,
        )
        if sub is not None:
            section_subgraphs[sec_id] = sub

    _snap(graph, "1.1")
    _guard_no_same_row_backward_feed(graph)
    _guard_no_mixed_entry_directions(graph)
    if validate:
        _guard_section_bboxes_positive(graph, "after Stage 1.1")
        _guard_no_negative_grid_columns(graph, "after Stage 1.1")
        _guard_explicit_grid_directions(graph, "after Stage 1.1")
        _guard_tall_anchor_stack_well_formed(graph, "after Stage 1.1")

    # Stage 1.2: Align Y grids across same-row, same-direction sections
    _align_row_y_grids(graph, section_subgraphs, y_spacing, section_y_padding)
    _snap(graph, "1.2")

    # Stage 1.3: Place sections on the canvas
    place_sections(graph, section_x_gap, section_y_gap)
    _snap(graph, "1.3")
    if validate:
        _guard_independent_components_disjoint(graph, "after Stage 1.3")

    # Stage 1.4: Renumber sections by visual reading order (row, col)
    _renumber_sections_by_grid(graph)
    _snap(graph, "1.4")

    # Stage 1.5: Adapt x/y_offset for left/top overshoot.
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

    _snap(graph, "1.5")

    # ---- Stage 2 - Globalise (local -> global coords) ------------------
    # The coord-regime transition.  Owns the post-Stage-2.1 guard
    # checkpoint (finite coords, stations-in-sections, bboxes-positive).

    # Stage 2.1: Translate local coords to global coords (real stations)
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

    _snap(graph, "2.1")
    if validate:
        _guard_coordinates_finite(graph, "after Stage 2.1")
        _guard_stations_in_sections(graph, "after Stage 2.1")
        _guard_section_bboxes_positive(graph, "after Stage 2.1")

    # ---- Stage 3 - Pass A: Port initialisation & section geometry --------
    # Position ports on bbox edges, align entry ports, shift internal
    # stations for perp entries, align fold exits, then top-align.
    # Top-align runs last so it corrects any bbox shifts from fold-exit
    # alignment.

    # Stage 3.1: Position ports on section boundaries (after bbox is in global coords)
    for sec_id, section in graph.sections.items():
        position_ports(section, graph)

    _snap(graph, "3.1")
    if validate:
        _guard_ports_on_boundaries(graph, "after Stage 3.1")

    # Stage 3.2: Align LEFT/RIGHT entry ports with their incoming
    # connection's Y so inter-section horizontal runs are straight.
    # Uses _resolve_source_xy() to derive junction coordinates
    # on-the-fly, removing the dependency on pre-positioned junctions.
    _align_entry_ports(graph)
    _snap(graph, "3.2")

    # Stage 3.3: Shift internal stations in LR/RL sections with
    # perpendicular (TOP/BOTTOM) entry away from the port.  Needs the
    # aligned port X from Stage 3.2; only moves internal station X, not
    # ports or bboxes.
    _shift_lr_perp_entry_stations(graph, x_spacing)
    _snap(graph, "3.3")

    # Stage 3.4: Align LEFT/RIGHT exit ports on row-spanning (fold)
    # sections with their target's Y so the exit is at the return row.
    # May push target sections down (via _resolve_tb_exit_y), which
    # top-align in the next step corrects.
    _align_exit_ports(graph)
    _snap(graph, "3.4")

    # Stage 3.5: Top-align sections within each grid row.
    # Runs after fold-exit alignment so it corrects any bbox_y shifts
    # from Stage 3.4's target-section push.  Same-row port pairs shift
    # by the same delta, preserving entry-port alignment.
    _top_align_row_sections(graph)
    _snap(graph, "3.5")

    if validate:
        _guard_ports_on_boundaries(graph, "after top-align")

    # ---- Stage 4 - Pass B: Downstream alignment & trunk-Y consolidation -
    # Stage 4.5's port-terminus spacing can expand bboxes via
    # ``_expand_bbox_for_y``; Stage 4.7 re-runs row top-align to undo
    # the resulting bbox-top drift.  Stages 4.9 / 4.10 run only on
    # ``center_ports`` graphs.

    # Stage 4.1: For non-fold LR/RL sections, pull exit-entry port pairs
    # toward the downstream section's stations so lines flow directly.
    _align_ports_to_downstream(graph)
    _snap(graph, "4.1")

    # Stage 4.2: When a port-connected station is the sole occupant of its
    # layer, snap it to the port Y so the connection is horizontal.
    _snap_sole_layer_stations_to_ports(graph)
    _snap(graph, "4.2")

    # Stage 4.3: For grid-group sections (where Stage 4.2 is skipped), snap
    # entry ports to the Y of their first connected internal station.
    # This produces a straight horizontal port-to-station connection
    # instead of a diagonal from the upstream junction Y.
    _snap_grid_group_entry_ports(graph)
    _snap(graph, "4.3")

    # Stage 4.4: Mirror of Stage 4.3 for exit ports.  Move exit ports of
    # grid-group sections to the Y of the downstream entry port (which
    # Stage 4.3 already snapped to a grid station).  This eliminates detours
    # where lines leave at the section midpoint then route back.
    _snap_grid_group_exit_ports(graph)
    _snap(graph, "4.4")

    # Stage 4.5: Ensure ports maintain at least y_spacing from terminus
    # stations in their section so file icons don't overlap routed lines.
    _space_ports_from_termini(graph, y_spacing)
    _snap(graph, "4.5")

    # Stage 4.6: Recompute bboxes for grid-aligned sections.  Earlier
    # stages (3.2, 3.4, 4.5) may have expanded bboxes for temporary port
    # positions that were later corrected (e.g. Stage 4.1 pulls ports
    # back toward downstream stations).  Recompute with symmetric
    # padding around the final non-port station range.
    _recompute_grid_group_bboxes(graph)
    _snap(graph, "4.6")

    # Stage 4.7: Re-run top-align after Stage 4.5 may have shifted
    # individual section bbox_y values (via _expand_bbox_for_y) so
    # bbox tops within each row stay flush after port-terminus spacing.
    _top_align_row_sections(graph)
    _snap(graph, "4.7")

    # Stage 4.8: Align trunk Ys across same-row sections.  Shifts
    # content downward in shallower sections so the inter-section bundle
    # passes through at a single Y per row.  Bbox tops are preserved.
    _align_row_trunk_ys(graph)
    _snap(graph, "4.8")

    # The trunk anchors (LR/RL port Ys) are now resolved.  The phases below
    # position content around them and must never move one; ``_run_placement``
    # runs each so that, under ``validate``, a guard asserts the anchors stayed
    # frozen.  See CONTRACT.md (anchor invariant).

    # Stage 4.9: When --center-ports is on, redistribute fan-out siblings
    # of a section's trunk junction symmetrically around the trunk Y.
    # Scoped to fan-out side branches only: linear chains, fan-in
    # structures, and file inputs are left in place.
    _run_placement(graph, validate, "4.9", _redistribute_fanout_siblings, y_spacing)
    _snap(graph, "4.9")

    # Stage 4.10: Symmetrically fan a column of full-bundle stations
    # around the trunk Y when no unique trunk exists (e.g. Reporting's
    # Shiny app + Quarto report, both carrying the full bundle).
    #
    # Why both Stage 4.10 and Stage 6.7's recenter: Stage 4.10's
    # symmetric layout is read by Stage 5.2's bbox-growth, compaction,
    # and snap-to-grid passes (an empty trunk row in fanned columns
    # lets Stages 5.4 / 6.13 shrink the section bbox to the compact
    # extent).  Stage 6.7 then re-fans the same columns using the
    # post-row-alignment trunk Y, which can have drifted from Stage
    # 4.10's port-Y anchor.  Skipping Stage 4.10 changes intermediate
    # bbox sizes and is not empty-render-diff; the two passes are
    # load-bearing in combination.
    _run_placement(
        graph, validate, "4.10", _redistribute_full_bundle_columns, y_spacing
    )
    _snap(graph, "4.10")

    _settle_pass_c(
        graph,
        validate=validate,
        y_spacing=y_spacing,
        section_y_padding=section_y_padding,
        section_y_gap=section_y_gap,
    )


def _settle_pass_c(
    graph: MetroGraph,
    *,
    validate: bool,
    y_spacing: float,
    section_y_padding: float,
    section_y_gap: float,
) -> None:
    """Pass C: junction placement, off-track lift, vertical content settling,
    the inter-row cascade, and final canvas/grid snapping.  See CONTRACT.md for
    the per-stage contract."""
    _place_pass_c_content(
        graph,
        validate=validate,
        y_spacing=y_spacing,
        section_y_padding=section_y_padding,
    )
    _stack_rows(
        graph,
        validate=validate,
        y_spacing=y_spacing,
        section_y_padding=section_y_padding,
        section_y_gap=section_y_gap,
    )
    _finalize_layout(
        graph,
        validate=validate,
        y_spacing=y_spacing,
        section_y_padding=section_y_padding,
        section_y_gap=section_y_gap,
    )


def _place_pass_c_content(
    graph: MetroGraph,
    *,
    validate: bool,
    y_spacing: float,
    section_y_padding: float,
) -> None:
    """Stage 5.1 through Stage 6.12: position junctions, lift off-track
    inputs, settle vertical content, and recenter fans / loop-side
    stations within each section."""
    # ---- Stage 5 - Pass C: Junctions & off-track lift ------------------
    # All port positions are now final; Stage 5.1 positions junctions
    # once.  Stage 5.2 lifts off-track stations; Stages 5.3 to 5.5
    # then re-align row bbox tops, compact, and re-snap inter-section
    # port pairs.  Stage 6 below handles the rest of Pass C.

    # A TB/BT section's perpendicular entry port is pinned a fixed offset
    # above its first station; the bbox growth between Stage 3.2's alignment
    # and here can lift the section top past it, leaving the port off its
    # boundary edge.  Re-align before the pass-C guards check boundaries
    # (Stage 6.16 re-aligns again after the late vertical settling).
    _align_entry_ports(graph, tb_only=True)

    # Stage 5.1: Position junction stations in the inter-section gap.
    _position_junctions(graph)
    _snap(graph, "5.1")

    # Stage 5.2: Lift off_track stations above their section's top track.
    # Runs last so it operates on finalised station Ys and bboxes.
    _lift_off_track_stations(graph, y_spacing, section_y_padding)
    # The upward bbox growth above can push the topmost section above
    # the canvas top margin set by Stage 1.5; shift the whole graph
    # down to restore the margin.  No-op when no section overflowed.
    _shift_graph_into_canvas(graph, section_y_padding)
    _snap(graph, "5.2")
    if validate:
        _run_pass_c_guards(graph, "after Stage 5.2")

    # Stage 5.3: Re-align bbox tops within each grid row after off-track
    # lifting expanded some sections upward.  Unlike Stages 3.5 / 4.7 which
    # shifts stations with the bbox, this only grows the bbox upward so
    # the empty input-band space lines up across the row.  Station Ys
    # in unlifted sections are preserved.
    _top_align_row_bboxes_only(graph)
    _snap(graph, "5.3")
    if validate:
        _run_pass_c_guards(graph, "after Stage 5.3")

    # Stage 5.4: Compact row-mate sections so content sits just inside
    # the bbox top edge.  Shifts an entire row's column group up by the
    # smallest above-content slack, preserving trunk alignment.  Bbox
    # heights shrink correspondingly so the empty top space disappears.
    _compact_row_content_to_bbox_top(graph, section_y_padding, y_spacing)
    _snap(graph, "5.4")
    if validate:
        _run_pass_c_guards(graph, "after Stage 5.4")

    # Stage 5.5: Snap inter-section LR/RL port pairs to a common Y so
    # the trunk bundle stays perfectly horizontal across boundaries.
    # Picks the downstream entry port's Y as the anchor since it sits
    # on the row's aligned trunk grid.
    _snap_inter_section_port_pairs(graph)
    _snap(graph, "5.5")
    if validate:
        _run_pass_c_guards(graph, "after Stage 5.5")

    # ---- Stage 6 - Pass C: Vertical settling & finishing ---------------
    # The long settle: fan free / source content upward, half-grid
    # symfan, snap to grid, bbox-bottom and off-track-reanchor
    # post-snap fixups, full-bundle recenter + its invariant-restore
    # sub-phases, terminus pin / auto-balance, loop-side X recenter,
    # bbox shrink + row tighten / push, captioned-icon pad.  Most
    # phases here run unconditionally; a few are gated on
    # ``center_ports`` or on a specific topology (see each comment).

    # Stage 6.1: Fan a section's free content upward when the row's
    # compaction left visible empty space at the bbox top.  Only fires
    # for sections whose internal stations have no upward dependency
    # (no off-track band) and whose trunk Y sits below the bbox top
    # padding by more than one ``y_spacing`` slot.
    # Frozen reference for Stages 6.1 / 6.2 (see _snapshot_placement_refs).
    _snapshot_placement_refs(graph)
    _run_placement_per_row(
        graph, validate, "6.1", _fan_free_content_upward, section_y_padding, y_spacing
    )
    _snap(graph, "6.1")
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.1")

    # Stage 6.2: Companion to Stage 6.1 for source-stack sections.  When the
    # entry column has a single full-bundle trunk plus subset-bundle
    # source inputs (file icons with no inbound edges), lift the
    # nearest-to-trunk sources into the empty top band so the section
    # is bottom- and top-weighted instead of stacked below the trunk.
    _run_placement_per_row(graph, validate, "6.2", _fan_source_inputs_upward, y_spacing)
    _snap(graph, "6.2")
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.2")

    # Stage 6.3: For sections that contain exactly a 2-branch
    # symmetric fan (and no off-track or other constraining content),
    # collapse the fan onto half-pitch offsets so the section consumes
    # one vertical grid unit instead of two.  Marks the branch stations
    # in ``graph.half_grid_station_ids`` so the next snap pass leaves
    # them alone.  Runs before ``_snap_all_y_to_grid`` so the snap-to-
    # row-grid pass doesn't immediately undo the compaction.
    if graph.center_ports:
        _run_placement_per_row(
            graph,
            validate,
            "6.3",
            _apply_half_grid_2branch_symfan,
            y_spacing,
            section_y_padding,
        )
        _snap(graph, "6.3")
        if validate:
            _run_pass_c_guards(graph, "after Stage 6.3")

    # Stage 6.4: Snap all station/port Ys to a per-section y_spacing
    # grid.  Trunk-Y align, port-snap, and the row compaction/fan
    # phases compute shifts that don't respect the grid pitch, leaving
    # coordinates at fractional Ys (e.g. 298.785 when the pitch is 55).
    # This final pass restores clean grid positions before validation.
    _snap_all_y_to_grid(graph, y_spacing)
    # On-track consumer Ys are now final/grid-snapped; the off-track
    # reanchor (Stage 6.6 / 6.8) may run from here on.
    graph._consumers_grid_snapped = True
    _snap(graph, "6.4")
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.4")

    # Stage 6.5: Grow TB-section bbox bottoms so they align with the
    # downstream LR section's bbox bottom.  Without this the TB
    # section's bbox ends right at the inter-section exit port Y,
    # making the line look pinned to the section edge.
    _align_tb_section_bbox_bottoms(graph)
    _snap(graph, "6.5")
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.5")

    # Stage 6.6: Re-anchor off-track inputs to their consumer's final
    # (snapped) Y.  Stage 5.2's lift placed them relative to pre-snap
    # consumer Ys; snapping the consumer to the grid can shift it by
    # up to half a pitch, which would collapse the y_spacing gap above
    # off-track.  Recomputing here pins each off-track at
    # consumer.y - n*y_spacing on the final grid and grows the bbox
    # upward if the new position rises above the padding zone.
    _reanchor_off_track_to_consumer(graph, y_spacing, section_y_padding)
    # Same canvas-fit safeguard as after Stage 5.2: a reanchor-driven
    # bbox grow can push the topmost section above the canvas top.
    _shift_graph_into_canvas(graph, section_y_padding)
    _snap(graph, "6.6")
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.6")

    # Stage 6.7: Re-center full-bundle columns around the row's final
    # trunk Y.  ``_redistribute_full_bundle_columns`` runs early when
    # only local port Ys are available; for terminal sections whose
    # port Y differs from the row's eventual trunk Y, the symmetric
    # fan ends up offset from the trunk row (e.g. Reporting's Shiny at
    # the trunk row, Quarto two slots below, instead of one above and
    # one below).  This re-center uses the final inter-section bundle
    # Y as the anchor so the trunk row stays empty in each fanned
    # column.
    #
    # Stage 6.8 and Stage 6.9 below restore invariants the recenter
    # breaks.
    if graph.center_ports:
        _run_placement_per_row(
            graph, validate, "6.7", _recenter_full_bundle_columns, y_spacing
        )
        _snap(graph, "6.7")

        # Stage 6.8: Re-anchor off-track inputs after the recenter.
        # The recenter moves consumers to the final trunk-anchored Y,
        # which can leave the off-track icon stranded at the old
        # consumer Y (overlapping the consumer station instead of
        # sitting one row above it).  Uses each consumer's post-
        # recenter Y as the new anchor and grows the section bbox
        # upward when the lifted band moves above its current top.
        _reanchor_off_track_to_consumer(graph, y_spacing, section_y_padding)
        _snap(graph, "6.8")

        # Stage 6.9: Re-run row top-align.  A Stage 6.8 reanchor-
        # driven bbox grow leaves the section's bbox above its row
        # mates'; pull row mates' bbox tops up to match so the section
        # row stays flush along its top edge.
        _top_align_row_bboxes_only(graph)
        _snap(graph, "6.9")
        if validate:
            _run_pass_c_guards(graph, "after Stage 6.9")

    # Stage 6.10: After fan-re-centering, single-station downstream
    # columns (e.g. terminus file icons) may have stayed at their
    # pre-fan Y while their sole upstream moved to the trunk.  Pin
    # them back onto the source Y so the connection stays horizontal.
    _align_terminus_to_upstream(graph)
    _snap(graph, "6.10")
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.10")

    # Stage 6.11: Auto-balance pass.  For sections whose final layout
    # still leaves an empty band above the trunk while more siblings
    # sit below than above, lift bottommost movable siblings into the
    # empty top band so content sits symmetrically around the trunk.
    # Runs after re-centering and terminus-Y pinning so it sees the
    # final trunk Y.  U-turn-safe and bbox-bounded.
    # Frozen reference for Stage 6.11 (see _snapshot_placement_refs).
    _snapshot_placement_refs(graph)
    _run_placement_per_row(
        graph,
        validate,
        "6.11",
        _balance_section_content_around_trunk,
        section_y_padding,
        y_spacing,
    )
    _snap(graph, "6.11")
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.11")

    # Stage 6.12: Recenter fan-out side stations on their loop midpoint.
    # The layer-based X assignment places off-trunk siblings (e.g. propd,
    # dream, DESeq2 fanned off limma between section entry and annotate
    # results) at a fixed offset from the section entry that ignores how
    # far the join's diagonal-back corner reaches.  Asymmetric corners
    # leave the station visibly off-centre on its horizontal loop run.
    # Reposition each side station to the midpoint of the two diagonal
    # corner Xs derived from the actual routing geometry.
    _run_placement_per_row(graph, validate, "6.12", _recenter_loop_side_stations)
    _snap(graph, "6.12")
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.12")


def _stack_rows(
    graph: MetroGraph,
    *,
    validate: bool,
    y_spacing: float,
    section_y_padding: float,
    section_y_gap: float,
) -> None:
    """Stage 6.13 and Stage 6.14: the cross-row inter-row stacking -- shrink
    and tighten row-mate bboxes, then shift sparse loop-side stations and
    propagate the resulting bbox growth to lower rows."""
    # Stage 6.13: Shrink rowspan / row-mate bboxes whose content moved
    # up after compact (e.g. ``_fan_source_inputs_upward`` lifted the
    # bottom rows away from the bbox bottom), then pull lower rows up
    # to close the slack the shrink revealed.  Bottom-only shrink, so
    # trunk alignment is unaffected; tighten only fires where a rowspan
    # section's content fell short of its row claim.
    _shrink_and_tighten_rows(graph, section_y_padding, section_y_gap)
    _snap(graph, "6.13")
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.13")

    # Stage 6.14: Shift sparse loop-side stations (e.g. ``grea`` -- one
    # incoming, one outgoing, single-line consumer) onto a half-grid Y
    # when sharing the full-row Y with a busier sibling whose inbound
    # bundle would otherwise cross the sparse station's marker bbox.
    # When the shift grows a section's bbox downward, the helper also
    # pushes lower-row sections down internally to restore
    # ``section_y_gap``.
    _shift_and_propagate_loop_stations(
        graph, y_spacing, section_y_padding, section_y_gap
    )
    _snap(graph, "6.14")
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.14")


def _finalize_layout(
    graph: MetroGraph,
    *,
    validate: bool,
    y_spacing: float,
    section_y_padding: float,
    section_y_gap: float,
) -> None:
    """Stage 6.15a through the final guards: restore symmetric top padding,
    re-snap the canvas to the grid, re-align perpendicular entry ports with
    their feeders, and run the closing Pass C invariant checks."""
    # Stage 6.15a: Restore top padding symmetric with the bottom.  Fan
    # re-distribution (Stages 4.9 / 4.10 / 6.7 / 6.11) can lift a branch
    # above the content-top line the bbox was sized for, crowding the
    # topmost marker against the bbox top while the bottom keeps its full
    # band.  Grow each bbox top to a full ``section_y_padding`` above the
    # highest marker (bounded by the row above) so content fanning above
    # the trunk sits centred in its box.  The upward growth can push the
    # topmost section above the canvas top margin, so re-fit; the
    # re-fit's non-grid shift is then cleaned up by the Stage 6.15
    # canvas snap below.
    _fit_bboxes_to_content_top(graph, section_y_padding, section_y_gap)
    _shift_graph_into_canvas(graph, section_y_padding)
    _snap(graph, "6.15a")
    # Refresh the structural extent snapshot to reflect Stage 6.15a's bbox
    # adjustments.  The cascade (Stage 6.13) already ran using Phase 1's
    # content-hugging bbox as its reference; this re-snapshot records the
    # final settled height-below-top so the test invariant can verify
    # structural-extent fidelity without a separate pre-cascade capture.
    _snapshot_struct_heights_below_top(graph, section_y_padding)

    # Stage 6.15: Restore canvas-wide grid alignment after all settling.
    # Stage 6.4 snaps to a per-row grid; later helpers (notably
    # ``_shift_graph_into_canvas`` shifting by ``section_y_padding -
    # min_top``, which is not a multiple of ``y_spacing`` when padding
    # is not a grid multiple) can introduce a uniform half-grid drift.
    # When every real station shares a single non-zero residue, shift
    # the whole canvas by the smallest signed amount that returns them
    # to integer multiples of ``y_spacing``.  No-op when residues are
    # mixed.
    _snap_canvas_y_to_grid(graph, y_spacing, section_y_padding)
    _snap(graph, "6.15")
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.15")

    # Stage 6.16: Re-align LEFT/RIGHT entry ports with their feeders.  A
    # TB section's perpendicular entry port is pinned a fixed offset above
    # its first internal station, so the late vertical settling (Stages
    # 6.13-6.15) that shifts the section's content also drags the entry
    # port off the upstream feeder Y it was snapped to in Stage 3.2,
    # re-introducing an inter-section S-kink.  Re-running the alignment
    # (TB/BT sections only, to leave settled LR/RL geometry untouched)
    # re-snaps the port to its now-settled feeder.  Junctions live in
    # inter-section space and aren't moved by the settling phases, so
    # re-anchor them to the settled exit/entry port Ys afterwards
    # (otherwise a fan-out bundle dips to a stale junction Y and back).
    _align_entry_ports(graph, tb_only=True)
    _position_junctions(graph)
    _snap(graph, "6.16")
    if validate:
        _run_pass_c_guards(graph, "after Stage 6.16")

    if validate:
        run_validate_guards(
            graph,
            "after final",
            include_final=True,
            section_y_gap=section_y_gap,
            section_y_padding=section_y_padding,
        )
