"""Grow and shrink section bboxes to fit content and predicted bypass spans."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from functools import partial

from nf_metro.layout.constants import (
    BYPASS_CLEARANCE,
    COORD_TOLERANCE,
    CURVE_RADIUS,
    DIAGONAL_RUN,
    MIN_BUNDLE_EDGE_CLEARANCE,
    MIN_INTER_SECTION_GAP,
    MIN_STATION_FLAT_LENGTH,
    MIN_STRAIGHT_EDGE,
    MIN_STRAIGHT_PORT,
    SAME_COORD_TOLERANCE,
    SECTION_HEADER_PROTRUSION,
)
from nf_metro.layout.geometry import (
    grid_spans_overlap,
    measured_distance,
    sections_share_a_column,
    shift_section,
)
from nf_metro.layout.labels import label_text_width
from nf_metro.layout.pass_metrics import font_scale_context, stroke_scale_context
from nf_metro.layout.phases._common import (
    _bbox_cols_overlap,
    _column_contiguous_row_groups,
    _content_station_ids,
    _content_station_ys,
    _is_side_entered_vertical_section,
    _pull_section_ports_to_edge,
    _ref_y,
    _side_entered_vertical_feeder_pairs,
    _station_bundle_offset_span,
    _trunk_symmetric_fan_ids,
    grow_section_bbox_max_edge,
    grow_section_bbox_min_edge,
    grow_section_bbox_to_anchor,
    move_section_bbox_min_edge,
    port_bundle_edge_reach,
    port_edge_inset,
    section_anchor_edge,
)
from nf_metro.layout.phases.junctions import _position_junctions
from nf_metro.layout.phases.single_section import (
    _terminus_y_overhang,
    angled_label_reach,
)
from nf_metro.layout.settlement_demand import (
    BoundaryClearanceDemand,
    SettlementAxis,
)
from nf_metro.parser.model import MetroGraph, PortSide, Section, Station, is_bypass_v


def _predicted_bypass_bottom_in_row(
    graph: MetroGraph, row: int
) -> dict[tuple[int, int], float]:
    """Predict bypass U-route bottom Ys for edges anchored in *row*.

    Mirrors ``layout.routing.common.bypass_bottom_y`` for layout-time
    prediction: returns ``{(lo, hi): max(intervening_bottoms) + BYPASS_CLEARANCE}``
    for each edge whose endpoints (after walking junctions) resolve to
    same-row sections spanning more than one column with at least one
    intervening section.  Empty when *row* has no bypass-eligible edges.
    """
    sections_in_row = [
        s for s in graph.sections.values() if s.grid_row == row and s.bbox_w > 0
    ]
    if not sections_in_row:
        return {}

    def _node_section(node_id: str) -> Section | None:
        st = graph.stations.get(node_id) or graph.ports.get(node_id)
        if st is None:
            return None
        sec_id = getattr(st, "section_id", None)
        return graph.sections.get(sec_id) if sec_id else None

    resolve_cache: dict[tuple[str, bool], Section | None] = {}

    def _resolve(
        node_id: str, upstream: bool, visited: set[str] | None = None
    ) -> Section | None:
        key = (node_id, upstream)
        if key in resolve_cache:
            return resolve_cache[key]
        if visited is None:
            visited = set()
        if node_id in visited:
            return None
        visited.add(node_id)
        sec = _node_section(node_id)
        if sec is None:
            edges = graph.edges_to(node_id) if upstream else graph.edges_from(node_id)
            for e in edges:
                nb = e.source if upstream else e.target
                sec = _resolve(nb, upstream, visited)
                if sec is not None:
                    break
        resolve_cache[key] = sec
        return sec

    per_span: dict[tuple[int, int], float] = {}
    for edge in graph.edges:
        src_sec = _resolve(edge.source, upstream=True)
        tgt_sec = _resolve(edge.target, upstream=False)
        if src_sec is None or tgt_sec is None:
            continue
        if src_sec.grid_row != row or tgt_sec.grid_row != row:
            continue
        if abs(src_sec.grid_col - tgt_sec.grid_col) <= 1:
            continue
        lo, hi = sorted((src_sec.grid_col, tgt_sec.grid_col))
        intervening = [s for s in sections_in_row if lo < s.grid_col < hi]
        if not intervening:
            continue
        bot = max(s.bbox_y + s.bbox_h for s in intervening) + BYPASS_CLEARANCE
        if bot > per_span.get((lo, hi), 0.0):
            per_span[(lo, hi)] = bot
    return per_span


def _aggregate_bypass_spans(
    graph: MetroGraph,
    upper_sections: list[Section],
    memo: dict[int, dict[tuple[int, int], float]] | None = None,
) -> dict[tuple[int, int], float]:
    """Aggregate bypass span->bottom predictions across upper sections.

    A row-spanning section carries its bypass routes from its start row
    down to the row below its end row, so the prediction must key off
    ``grid_row`` (start), not the end row.

    The per-row prediction reads section boxes and nothing else, so *memo* may
    carry it between calls that share one frozen geometry; a caller that moves a
    box between calls must not pass the same one.
    """
    predicted = {} if memo is None else memo
    combined: dict[tuple[int, int], float] = {}
    for upper_start_row in {s.grid_row for s in upper_sections}:
        if upper_start_row not in predicted:
            predicted[upper_start_row] = _predicted_bypass_bottom_in_row(
                graph, upper_start_row
            )
        for span, bot in predicted[upper_start_row].items():
            if bot > combined.get(span, 0.0):
                combined[span] = bot
    return combined


def _shift_rows_from(
    graph: MetroGraph,
    from_row: int,
    deficit: float,
    *,
    reposition_junctions: bool = True,
) -> None:
    """Shift every section at or below *from_row* down by *deficit*.

    Negative amounts pull those rows up.  This is the one global translation
    primitive the row-compensation passes above share: each of them measures a
    per-boundary amount locally and then hands it here, so what those passes
    write outside their own boxes is exactly the calls to this function.

    Moves the sections' bboxes and their stations/ports together.  With
    *reposition_junctions*, junction placement is re-derived from the moved
    ports: a junction's coordinates are a function of the ports it joins (a
    fan-out junction is pinned to its exit port's Y, a merge junction to its
    entry port's Y), so a translation that left junctions behind would strand
    them off the bundle they belong to.  A caller running before any routing
    pass reads a junction coordinate can leave them for routing to recompute.
    """
    for s in graph.sections.values():
        if s.grid_row >= from_row:
            shift_section(graph, s, dy=deficit)
    if reposition_junctions:
        _position_junctions(graph)


def measure_row_gap_clearance(
    graph: MetroGraph, section_y_gap: float
) -> tuple[BoundaryClearanceDemand, ...]:
    """How far short each row boundary is of the clearance it owes.

    A boundary owes ``section_y_gap``, raised to whatever an inter-row run or a
    bottommost merge trunk's envelope needs there.  What that clearance is
    measured against is three things, and a boundary owes the largest:

    * the bbox bottoms of sections *ending* at row ``r - 1`` whose column spans
      overlap a section starting at row ``r``.  Two sections sharing a vertical
      edge in column space must keep the gap between them; sections in
      different columns can sit closer without visual interference;
    * the depth a bypass route dips below the intervening bboxes it passes.
      Those need no column overlap with the upper-row endpoint bbox, only with
      the lower section they would otherwise crowd against;
    * the two row envelopes, for a bottommost-row merge trunk whose channel is
      bounded by them rather than by the columns it travels.

    Pure measurement: the caller decides whether to translate.  Every blocker
    the deficit is measured from lies wholly above the boundary, and every box
    it is measured to starts at or beyond it, so the deficits at different
    boundaries are independent -- widening one moves both sides of every other
    together.
    """
    if not graph.sections:
        return ()

    from nf_metro.layout.section_placement import (
        _inter_row_routing_minimums,
        _merge_trunk_row_minimums,
    )

    routing_min = _inter_row_routing_minimums(graph)
    envelope_min = _merge_trunk_row_minimums(graph)

    sections_by_row_start: dict[int, list[Section]] = defaultdict(list)
    for s in graph.sections.values():
        sections_by_row_start[s.grid_row].append(s)
    if not sections_by_row_start:
        return ()
    max_row = max(s.grid_row + s.grid_row_span - 1 for s in graph.sections.values())
    bypass_memo: dict[int, dict[tuple[int, int], float]] = {}

    demands: list[BoundaryClearanceDemand] = []
    for r in range(1, max_row + 1):
        lower = sections_by_row_start.get(r, [])
        if not lower:
            continue
        ending_at_prev = [
            s
            for s in graph.sections.values()
            if s.grid_row + s.grid_row_span - 1 == r - 1 and s.bbox_h > 0
        ]
        if not ending_at_prev:
            continue
        bypass_by_span = _aggregate_bypass_spans(graph, ending_at_prev, bypass_memo)
        target_gap = max(section_y_gap, routing_min.get((r - 1, r), 0.0))

        # (deficit, the clearance it is measured against, its blockers, its
        # description).  The leading entry is what an unblocked boundary owes, so
        # the widest wins outright; ``max`` keeps the first of equal deficits,
        # which is the order they are measured in.
        candidates: list[tuple[float, float, tuple[str, ...], str]] = [
            (0.0, target_gap, (), "")
        ]
        for us in ending_at_prev:
            for ls in lower:
                if ls.bbox_h <= 0 or not sections_share_a_column(us, ls):
                    continue
                candidates.append(
                    (
                        target_gap
                        - measured_distance(us.bbox_y + us.bbox_h, ls.bbox_y),
                        target_gap,
                        (us.id,),
                        "the box above it",
                    )
                )
        for (lo, hi), bypass_bot in bypass_by_span.items():
            for ls in lower:
                ls_columns = (ls.grid_col, ls.grid_col + ls.grid_col_span - 1)
                if ls.bbox_h <= 0 or not grid_spans_overlap(ls_columns, (lo, hi)):
                    continue
                candidates.append(
                    (
                        target_gap - measured_distance(bypass_bot, ls.bbox_y),
                        target_gap,
                        (),
                        f"a bypass route across columns {lo}-{hi}",
                    )
                )
        envelope_gap = envelope_min.get((r - 1, r), 0.0)
        if envelope_gap:
            envelope_bottom = max(s.bbox_y + s.bbox_h for s in ending_at_prev)
            envelope_top = min(ls.bbox_y for ls in lower if ls.bbox_h > 0)
            candidates.append(
                (
                    envelope_gap - measured_distance(envelope_bottom, envelope_top),
                    envelope_gap,
                    tuple(
                        s.id
                        for s in ending_at_prev
                        if abs(s.bbox_y + s.bbox_h - envelope_bottom) <= COORD_TOLERANCE
                    ),
                    "the row envelope above it",
                )
            )
        deficit, required, blockers, source = max(candidates, key=lambda item: item[0])
        if deficit <= SAME_COORD_TOLERANCE:
            continue
        demands.append(
            BoundaryClearanceDemand(
                SettlementAxis.ROW,
                r,
                required,
                deficit,
                tuple(sorted(blockers)),
                f"the {required:.2f}px clearance row boundary {r} owes {source}",
            )
        )
    return tuple(demands)


def push_lower_rows_after_bbox_grow(graph: MetroGraph, section_y_gap: float) -> bool:
    """Push lower-row sections down when an upper-row bbox grows.

    Shared helper called by layout stages that may grow a section's ``bbox_h``
    downward after row offsets are already fixed (``_shift_and_propagate_loop_
    stations`` at Stage 6.14, and the Stage 6.15a top-padding restore).  Row
    offsets were fixed earlier by ``_compute_section_offsets`` from pre-grow
    bbox heights, so the section below a grown one can end up sitting closer
    than the gap the boundary owes.

    Measures with :func:`measure_row_gap_clearance` and pays each deficit by
    shifting that row and below down (sections + stations + ports + the
    junctions those ports anchor).  Returns ``True`` when any row was shifted.

    Callers that run after routing has published its reservation ledger hand the
    measurement to envelope settlement instead, so that one translation settles a
    boundary's corridor and clearance demands together.

    Each boundary is paid at most once, lowest first, re-measuring in between so
    a boundary a previous shift already relieved is not charged twice.
    """
    shifted = False
    paid: set[int] = set()
    while True:
        pending = [
            demand
            for demand in measure_row_gap_clearance(graph, section_y_gap)
            if demand.boundary not in paid
        ]
        if not pending:
            return shifted
        demand = min(pending, key=lambda item: item.boundary)
        paid.add(demand.boundary)
        _shift_rows_from(graph, demand.boundary, demand.deficit)
        shifted = True


def _loop_corner_x(
    a: Station,
    b: Station,
    fork_stations: set[str],
    join_stations: set[str],
    role: str,
) -> float | None:
    """Compute the diagonal corner X for a single edge a->b.

    Mirrors ``_compute_diagonal_placement`` in
    ``layout/routing/core.py``: places the diagonal centred near the
    fork (when a is a fork station) or near the join (when b is a
    join station), with MIN_STRAIGHT endpoint clearance and optional
    label clearance.  Returns the corner X on the side opposite to
    ``role``: ``role='src'`` returns the corner near b (target side
    of edge a->b, i.e. the LEFT corner of the loop b is part of);
    ``role='tgt'`` returns the corner near a (source side of edge
    a->b, i.e. the RIGHT corner of the loop a is part of).
    """
    sx, _ = a.x, a.y
    tx, _ = b.x, b.y
    if abs(tx - sx) < 1e-6:
        return None
    sign = 1.0 if tx > sx else -1.0
    src_min = CURVE_RADIUS + MIN_STRAIGHT_PORT if a.is_port else MIN_STRAIGHT_EDGE
    tgt_min = CURVE_RADIUS + MIN_STRAIGHT_PORT if b.is_port else MIN_STRAIGHT_EDGE
    # Label clearance at fork/join stations (per _route_diagonal).
    if a.id in fork_stations and a.label.strip():
        src_min = max(src_min, label_text_width(a.label) / 2)
    if b.id in join_stations and b.label.strip():
        tgt_min = max(tgt_min, label_text_width(b.label) / 2)
    half_diag = DIAGONAL_RUN / 2
    is_fork = a.id in fork_stations
    is_join = b.id in join_stations
    if is_fork:
        mid = sx + sign * (src_min + half_diag)
    elif is_join:
        mid = tx - sign * (tgt_min + half_diag)
    else:
        mid = (sx + tx) / 2.0
    # Clamp to keep minimum straight endpoint runs.
    if sign > 0:
        diag_start = max(mid - half_diag, sx + src_min)
        diag_end = min(mid + half_diag, tx - tgt_min)
    else:
        diag_start = min(mid - sign * half_diag, sx - src_min)
        diag_end = max(mid + sign * half_diag, tx + tgt_min)
    # role='src' returns the END of the diagonal (corner near b),
    # role='tgt' returns the START of the diagonal (corner near a).
    return diag_end if role == "src" else diag_start


def _lift_would_cause_uturn(
    graph: MetroGraph, station_id: str, section_id: str, anchor_y: float
) -> bool:
    """Return True when lifting *station_id* above ``anchor_y`` would
    force its incoming bundle to make a U-turn.

    A station U-turns when every external feeder sits at Y >= anchor_y:
    the line bundle has to climb from the section's entry port (anchored
    at the row's trunk Y) up to the lifted station, then back down to
    rejoin the trunk for downstream stations.  When two or more feeders
    share that situation, the upward climb visibly bends the bundle
    against the trunk and may cross sibling routes that stay at trunk Y.

    Returns False when there's no risk (no feeders, single feeder, or
    any feeder sits above the anchor giving the bundle a reason to climb).
    """
    junction_ids = graph.junction_ids
    seen: set[str] = set()
    feeder_ys: list[float] = []

    def _collect(node_id: str) -> None:
        for edge in graph.edges_to(node_id):
            src_id = edge.source
            if src_id in seen:
                continue
            seen.add(src_id)
            if src_id in junction_ids:
                _collect(src_id)
                continue
            src = graph.stations.get(src_id)
            if src is None:
                continue
            if src.is_port:
                _collect(src_id)
                continue
            if src.section_id == section_id:
                continue
            feeder_ys.append(_ref_y(graph, src_id))

    _collect(station_id)
    if len(feeder_ys) < 2:
        return False
    return all(y >= anchor_y - SAME_COORD_TOLERANCE for y in feeder_ys)


def _shrink_and_tighten_rows(
    graph: MetroGraph,
    section_y_padding: float,
    section_y_gap: float,
) -> None:
    """Shrink section bbox bottoms to content, then pull lower rows up
    to close any slack the shrink revealed.

    Two-phase unified helper:

    Phase 1 - shrink:
      Resize each section's ``bbox_h`` so the bottom sits
      ``section_y_padding`` below the bottom-most station / port,
      shrinking when content rose during earlier passes
      (``_fan_source_inputs_upward``, ``_recenter_full_bundle_columns``)
      and growing when ``_snap_all_y_to_grid`` snapped a station
      downward.  Station Ys are unchanged so trunk alignment is
      preserved.  Never trims past the maximum bbox bottom of any
      row-mate (another section whose ``grid_row`` equals this
      section's starting row, accounting for the other section's
      ``grid_row_span``); trimming below a row-mate would undo
      intentional bottom alignment from Stage 6.5 or TB-rowspan
      neighbours.  The check is keyed on this section's STARTING row
      rather than its full row-span -- a rowspan>1 LR sidebar whose
      content fits in one row is not pinned to neighbours in the
      claimed-but-unfilled extra rows.

    Phase 2 - tighten:
      ``_compute_section_offsets`` sizes ``row_heights[r]`` from the
      pre-shrink bbox heights, and a rowspan section that ends at row
      ``r`` inflates the height further to fit its (then-tall) bbox.
      Once phase 1 collapses bbox bottoms to actual content, row
      ``r + 1`` can sit below empty space.  For each row pair, close
      any slack beyond ``section_y_gap`` by shifting lower rows
      (sections + stations + ports) upward.  The tighten step needs
      every row's shrink to finish first so the row-gap deficit is
      measurable against the final bbox bottoms, which is why this
      runs as a second pass over the same graph rather than per
      section.
    """
    _shrink_bboxes_to_content_bottom(graph, section_y_padding)
    _tighten_lower_rows_after_shrink(graph, section_y_gap)


def _bundle_edge_padding(
    section_y_padding: float, edge_reach: float, in_fan: bool
) -> float:
    """Padding to reserve, beyond ``station.y``, toward one edge of a
    multi-line bundle's drawn pill.

    ``edge_reach`` is how far the pill extends past the anchor toward that
    edge (:func:`_station_bundle_offset_span`; 0 for a non-bundle or
    vertical-flow station).  A station in a Y-mirrored fan pair (``in_fan``,
    :func:`_trunk_symmetric_fan_ids`) gets the reach added under the full
    ``section_y_padding``, so a symmetric diamond's two branches get equal
    breathing room; any other multi-line station gets it added under the
    smaller ``MIN_BUNDLE_EDGE_CLEARANCE`` floor, enough for a label off an
    unmirrored bundle (a flat run, a fold) to clear the box edge.  Shared by
    :func:`_predict_section_content_bottom` and
    :func:`_section_content_hug_top` so the fan-vs-floor split cannot drift
    between the two directions.
    """
    return max(
        section_y_padding,
        edge_reach + (section_y_padding if in_fan else MIN_BUNDLE_EDGE_CLEARANCE),
    )


def _bypass_v_lane_reach(
    graph: MetroGraph,
    sid: str,
    offsets: dict[tuple[str, str], float],
    is_horizontal: bool,
) -> tuple[float, float]:
    """How far above and below its anchor lane the curve drawn through
    bypass-V helper ``sid`` reaches, as two non-negative distances.

    A helper has no marker pill, but the diversion curve through it is drawn
    on its line's offset lane (:func:`_station_bundle_offset_span`), which a
    multi-line bundle can put a whole offset step off the anchor.  The
    curve-clearance the section edge owes the helper is owed to that lane --
    the bypass counterpart of what :func:`_bundle_edge_padding` does for a
    marker's drawn pill.  A vertical-flow section separates its lines in X
    instead, so both reaches are 0 there.
    """
    if not is_horizontal:
        return 0.0, 0.0
    min_off, max_off = _station_bundle_offset_span(graph, sid, offsets)
    return max(0.0, -min_off), max(0.0, max_off)


def _predict_section_content_bottom(
    graph: MetroGraph,
    section: Section,
    section_y_padding: float,
    offsets: dict[tuple[str, str], float] | None = None,
) -> float | None:
    """Lowest Y the section's content requires, by the bbox-shrink rule.

    The single per-section content-bottom rule shared by the bbox shrink
    (:func:`_shrink_bboxes_to_content_bottom`) and the structural-extent
    snapshot (:func:`_snapshot_struct_heights_below_top`): the max over
    non-port, non-``__bypass_`` stations of ``y + max(bundle_edge_padding,
    terminus_overhang)``, then raised to keep bypass-curve helpers and ports
    inside.  Returns ``None`` when the section has no real content.

    ``bundle_edge_padding`` (see :func:`_bundle_edge_padding`) is the
    bottom-edge case of the bundle-span correction: how far a station's
    drawn bundle pill extends below its anchor lane
    (:func:`_station_bundle_offset_span`) folded into the padding target.
    A bypass helper takes the same correction against its drawn curve
    (:func:`_bypass_v_lane_reach`) under the smaller curve clearance.
    Pass a pre-computed ``offsets``
    (:func:`nf_metro.layout.routing.compute_station_offsets`) to avoid
    recomputing it once per section in a per-section caller's loop.

    Named for its role in the snapshot: captured before the opportunistic
    Pass C content-compaction phases run so the inter-row cascade can stack
    from a structural extent.
    """
    section_dir = section.direction or "LR"
    # Angled labels (#527) hang below LR/RL stations; their reach must be
    # part of the content bottom so the shrink phase and the inter-row
    # cascade keep the row below clear.  0 for horizontal-label layouts.
    label_angle = graph.label_angle or 0.0 if section_dir in ("LR", "RL") else 0.0
    is_horizontal = section_dir in ("LR", "RL")
    fan_ids = _trunk_symmetric_fan_ids(graph, section) if is_horizontal else ()
    # In a rail panel a single-rail station on the top rail labels *above* the
    # bundle (:func:`labels._rail_label_side`), so its angled label hangs up
    # rather than down and adds no downward reach; only the below-hanging labels
    # anchor the bottom.  :func:`_guard_rail_above_label_band` covers the upward
    # footprint of these stations.
    rail_above_ids: set[str] = set()
    if label_angle:
        # Function-local: a module-level import would close a layout import cycle.
        from nf_metro.layout.rail_mode import rail_above_label_ids

        rail_above_ids = rail_above_label_ids(graph, section)
    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)
    # The label-reach and marker-footprint metrics scale with the graph's font
    # and stroke; bind both from the graph so the prediction is reproducible
    # whether or not a layout-wide scale context is active at the call site.
    with (
        font_scale_context(graph.font_scale),
        stroke_scale_context(graph.stroke_scale),
    ):
        content_bots = [
            graph.stations[sid].y
            + max(
                _bundle_edge_padding(
                    section_y_padding,
                    (
                        max(0.0, _station_bundle_offset_span(graph, sid, offsets)[1])
                        if is_horizontal and offsets is not None
                        else 0.0
                    ),
                    sid in fan_ids,
                ),
                _terminus_y_overhang(graph.stations[sid], section_dir, graph)[1],
                angled_label_reach(
                    graph.stations[sid],
                    0.0 if sid in rail_above_ids else label_angle,
                ),
            )
            for sid in _content_station_ids(graph, section)
        ]
    if not content_bots:
        return None
    content_bot = max(content_bots)
    bypass_max_ys = [
        graph.stations[sid].y
        + _bypass_v_lane_reach(graph, sid, offsets, is_horizontal)[1]
        for sid in section.station_ids
        if sid in graph.stations and is_bypass_v(sid)
    ]
    port_max_ys = [
        graph.stations[sid].y
        + port_edge_inset(
            graph.ports.get(sid),
            section_dir,
            "y",
            port_bundle_edge_reach(graph, sid, offsets, "y")[1],
        )
        for sid in section.station_ids
        if sid in graph.stations and graph.stations[sid].is_port
    ]
    v_curve_clearance = CURVE_RADIUS + MIN_STATION_FLAT_LENGTH / 2
    if bypass_max_ys:
        content_bot = max(content_bot, max(bypass_max_ys) + v_curve_clearance)
    if port_max_ys:
        content_bot = max(content_bot, max(port_max_ys))
    return content_bot


def _snapshot_struct_heights_below_top(
    graph: MetroGraph, section_y_padding: float
) -> None:
    """Record each section's structural height below its bbox top.

    Captures ``_predict_section_content_bottom(...) - section.bbox_y`` per
    section into ``graph._struct_height_below_top``.  Called after Stage
    6.15a so the stored heights reflect the fully settled bbox tops.  The
    inter-row cascade (Stage 6.13 phase 2) reads Phase 1's content-hugging
    bbox directly; this snapshot records the settled extents for
    structural-extent fidelity checks.
    """
    from nf_metro.layout.routing import compute_station_offsets

    offsets = compute_station_offsets(graph)
    graph._struct_height_below_top = {}
    for section in graph.sections.values():
        if section.bbox_h <= 0:
            continue
        bottom = _predict_section_content_bottom(
            graph, section, section_y_padding, offsets
        )
        if bottom is not None:
            graph._struct_height_below_top[section.id] = bottom - section.bbox_y


def _shrink_bboxes_to_content_bottom(
    graph: MetroGraph, section_y_padding: float
) -> None:
    """Phase 1 of :func:`_shrink_and_tighten_rows`.

    Resize each section's ``bbox_h`` so the bottom sits
    ``section_y_padding`` below the bottom-most station / port.  See
    the parent helper's docstring for the full contract; this
    function is split out so the runtime guard at "after Stage 6.13"
    still bisects to a meaningful intermediate state.
    """

    def _shares_bottom_with_row_mate(section: Section) -> bool:
        # A bottom edge shared with a row-mate is deliberate -- Stage 6.5
        # bottom-aligns row-mates, a TB fold's bbox is grown by
        # ``section_y_gap`` so it reaches its target's bottom, and a rowspan
        # section meets the bottom of the row it spans into -- so the shrink
        # must leave it intact.  A row-mate that is merely deeper shares no
        # edge with this section and must not hold its bottom off its content.
        #
        # Membership itself has two policies.  LR/RL sections use ONLY their
        # STARTING grid row: counting this section's rowspan would pull in
        # sections from rows the rowspan claims but doesn't fill -- a
        # rowspan=2 LR sidebar whose content fits in row 0 must not be pinned
        # to a row-1 neighbour just because its declared span overlaps row 1.
        # TB sections (folds), and any section without grid coords, take any
        # section at the shared bottom: a fold's partner is by design in the
        # next grid row down.
        my_grid_row = section.grid_row if section.grid_row >= 0 else None
        my_y_bot = section.bbox_y + section.bbox_h
        use_grid = section.direction != "TB" and my_grid_row is not None
        for other in graph.sections.values():
            if other.id == section.id or other.bbox_h <= 0:
                continue
            o_y_bot = other.bbox_y + other.bbox_h
            if abs(o_y_bot - my_y_bot) > SAME_COORD_TOLERANCE:
                continue
            if not use_grid or my_grid_row is None or other.grid_row < 0:
                return True
            o_grid_bot = other.grid_row + max(1, other.grid_row_span)
            if other.grid_row <= my_grid_row < o_grid_bot:
                return True
        return False

    from nf_metro.layout.routing import compute_station_offsets

    offsets = compute_station_offsets(graph)
    for section in graph.sections.values():
        if section.bbox_h <= 0:
            continue
        content_bot = _predict_section_content_bottom(
            graph, section, section_y_padding, offsets
        )
        if content_bot is None:
            continue
        current_bot = section.bbox_y + section.bbox_h
        if content_bot > current_bot + SAME_COORD_TOLERANCE:
            section.bbox_h = content_bot - section.bbox_y
            _pull_section_ports_to_edge(
                graph, section, PortSide.BOTTOM, section.bbox_y + section.bbox_h
            )
            continue
        if _shares_bottom_with_row_mate(section):
            continue
        new_h = content_bot - section.bbox_y
        if new_h < section.bbox_h - SAME_COORD_TOLERANCE:
            section.bbox_h = max(0.0, new_h)
            _pull_section_ports_to_edge(
                graph, section, PortSide.BOTTOM, section.bbox_y + section.bbox_h
            )


def _section_fit_top(
    graph: MetroGraph,
    section: Section,
    section_y_padding: float,
    section_y_gap: float,
    offsets: dict[tuple[str, str], float] | None = None,
) -> float | None:
    """Return the content-hug bbox top for ``section``.

    :func:`_section_content_hug_top` over the shared content set, then
    bounded by the row above.

    The row-above bound reserves ``section_y_gap +
    SECTION_HEADER_PROTRUSION`` (the header badge protrudes above the
    bbox top and inter-section routes dip into the gap).  It is a
    grow-direction ceiling: it can lower the returned top but never raise
    it above the content-hug position, so a caller hugging content
    downward applies it as a bound, not as the target.

    ``offsets`` is forwarded to :func:`_section_content_hug_top`; see its
    docstring for the bundle-span adjustment it enables.

    Returns ``None`` when the section has no real content to anchor to.
    """
    target = _section_content_hug_top(graph, section, section_y_padding, offsets)
    if target is None:
        return None

    above_bots: list[float] = []
    for other in graph.sections.values():
        if other.id == section.id or other.bbox_w <= 0 or other.bbox_h <= 0:
            continue
        if other.grid_row + max(1, other.grid_row_span) != section.grid_row:
            continue
        if not _bbox_cols_overlap(other, section):
            continue
        above_bots.append(other.bbox_y + other.bbox_h)
    if above_bots:
        target = max(
            target, max(above_bots) + section_y_gap + SECTION_HEADER_PROTRUSION
        )
    return target


def _section_content_hug_top(
    graph: MetroGraph,
    section: Section,
    section_y_padding: float,
    offsets: dict[tuple[str, str], float] | None = None,
) -> float | None:
    """Ceiling-free content-hug top for ``section``.

    The shrink twin of :func:`_section_fit_top`: the same content-hug
    (``section_y_padding`` above the highest content marker, clamped to
    keep bypass helpers and ports inside) but WITHOUT the row-above grow
    ceiling.  The ceiling is a grow-direction bound only -- lowering a
    too-tall top moves it away from the row above, so it never binds when
    hugging content downward.

    The padding above each station folds in the top-edge case of the
    bundle-span correction (see :func:`_bundle_edge_padding`): how far a
    multi-line bundle's drawn pill extends above its anchor lane
    (:func:`_station_bundle_offset_span`), which can be nonzero even when
    no fan-out lifted the station itself.  A bypass helper takes the same
    correction against its drawn curve (:func:`_bypass_v_lane_reach`) under
    the smaller curve clearance.  Pass a pre-computed ``offsets``
    (:func:`nf_metro.layout.routing.compute_station_offsets`) to avoid
    recomputing it once per section in a per-section caller's loop.

    Returns ``None`` when the section has no real content to anchor to.
    """
    content_ids = _content_station_ids(graph, section)
    if not content_ids:
        return None
    section_dir = section.direction or "LR"
    is_horizontal = section_dir in ("LR", "RL")
    fan_ids = _trunk_symmetric_fan_ids(graph, section) if is_horizontal else ()
    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)
    # A rail section's above-labelled top-rail stations get less clearance
    # than a generic station: see rail_above_label_top_pad's docstring for
    # why, and why this must match rail_mode's own reservation exactly.
    from nf_metro.layout.rail_mode import rail_above_label_top_pad

    rail_pad = rail_above_label_top_pad(graph, section)

    def _content_min_y(sid: str) -> float:
        if sid in rail_pad:
            return graph.stations[sid].y - rail_pad[sid]
        return graph.stations[sid].y - max(
            _bundle_edge_padding(
                section_y_padding,
                (
                    max(0.0, -_station_bundle_offset_span(graph, sid, offsets)[0])
                    if is_horizontal and offsets is not None
                    else 0.0
                ),
                sid in fan_ids,
            ),
            # A vertical-flow terminus's file icon reaches above its marker
            # (a top-entering source), so the box must clear the icon, not
            # just a padding band above the marker.
            _terminus_y_overhang(graph.stations[sid], section_dir, graph)[0],
        )

    content_min_ys = [_content_min_y(sid) for sid in content_ids]
    bypass_min_ys = [
        graph.stations[sid].y
        - _bypass_v_lane_reach(graph, sid, offsets, is_horizontal)[0]
        for sid in section.station_ids
        if sid in graph.stations and is_bypass_v(sid)
    ]
    port_min_ys = [
        graph.stations[sid].y
        - port_edge_inset(
            graph.ports.get(sid),
            section_dir,
            "y",
            port_bundle_edge_reach(graph, sid, offsets, "y")[0],
        )
        for sid in section.station_ids
        if sid in graph.stations and graph.stations[sid].is_port
    ]
    v_curve_clearance = CURVE_RADIUS + MIN_STATION_FLAT_LENGTH / 2
    target = min(content_min_ys)
    if bypass_min_ys:
        target = min(target, min(bypass_min_ys) - v_curve_clearance)
    if port_min_ys:
        target = min(target, min(port_min_ys))
    return target


def _reserve_perp_port_edge_inset(graph: MetroGraph) -> bool:
    """Hold a section's left and right edges clear of its TOP/BOTTOM ports.

    A TOP or BOTTOM port is pinned to a horizontal edge and free along X, so it
    crosses the left and right edges and owes them ``PERP_PORT_EDGE_INSET`` --
    the rotation of what :func:`_section_content_hug_top` and
    :func:`_predict_section_content_bottom` fold into the Y extent for a LEFT or
    RIGHT port.  Which side the port sits on settles this, not the direction the
    section flows, since a seam joins a BOTTOM exit to a TOP entry at one X and
    the two halves owe that X the same room.

    X sizing measures real stations only, so a port seated past the trailing
    station or dragged onto a drop column lands inside the padding band with
    nothing to push the edge out.  Growing the edge is the only move available:
    pulling the port back inside would cost it the elbow runway it was seated for.

    Each port owes its facing edges the inset independently.  The two are not
    levelled against each other, because an edge held further out by content or a
    routing band is not the port's doing, and mirroring it onto the opposite edge
    would buy symmetry with dead space.

    Measured from each port's outermost drawn lane rather than the port station
    (:func:`port_bundle_edge_reach`), so a staggered bundle gets the whole inset
    and not the inset less its own width.

    Returns whether any box grew, so the caller can re-check inter-column gaps.
    """
    grew = False
    offsets: dict[tuple[str, str], float] | None = None
    for section in graph.sections.values():
        if section.bbox_w <= 0:
            continue
        perp_ids = [
            pid
            for pid in section.port_ids
            if (port := graph.ports.get(pid)) is not None
            and port.side in (PortSide.TOP, PortSide.BOTTOM)
            and pid in graph.stations
        ]
        if not perp_ids:
            continue
        if offsets is None:
            from nf_metro.layout.routing import compute_station_offsets

            offsets = compute_station_offsets(graph)
        sec_dir = section.direction or "LR"
        lanes = [
            (
                graph.stations[pid].x,
                graph.ports[pid],
                port_bundle_edge_reach(graph, pid, offsets, "x"),
            )
            for pid in perp_ids
        ]
        lo = min(
            x - port_edge_inset(port, sec_dir, "x", reach[0])
            for x, port, reach in lanes
        )
        hi = max(
            x + port_edge_inset(port, sec_dir, "x", reach[1])
            for x, port, reach in lanes
        )
        before = (section.bbox_x, section.bbox_w)
        grow_section_bbox_min_edge(graph, section, "x", lo)
        grow_section_bbox_max_edge(graph, section, "x", hi)
        if (section.bbox_x, section.bbox_w) != before:
            grew = True
    return grew


def _column_neighbour_anchor_limit(
    graph: MetroGraph, section: Section, sign: float
) -> float:
    """Furthest X ``section``'s *sign*-anchored edge may reach past a neighbour.

    A section already clear on that side of ``section`` and sharing vertical
    extent with it (headers included, since a badge protrudes above its box top)
    keeps ``MIN_INTER_SECTION_GAP`` of routing corridor.  Inter-column gaps are
    enforced per overlapping row band rather than per column, so the column's
    outermost box being clear says nothing about a row-mate further down.
    Infinite in the growth direction when that side is empty.
    """
    top = section.bbox_y - SECTION_HEADER_PROTRUSION
    bottom = section.bbox_y + section.bbox_h
    here = section_anchor_edge(section, "x", sign)
    limit = float("-inf") * sign
    for other in graph.sections.values():
        if other is section or other.bbox_w <= 0:
            continue
        facing = section_anchor_edge(other, "x", -sign)
        if (facing - here) * sign > SAME_COORD_TOLERANCE:
            continue
        if (
            other.bbox_y - SECTION_HEADER_PROTRUSION >= bottom
            or top >= other.bbox_y + other.bbox_h
        ):
            continue
        corridor = facing + MIN_INTER_SECTION_GAP * sign
        limit = max(limit, corridor) if sign > 0 else min(limit, corridor)
    return limit


def _shared_anchor_runway_runs(
    graph: MetroGraph, group: list[Section], sign: float
) -> list[list[Section]]:
    """Split ``group`` into runs whose boxes start their content at one X.

    A grid row's sections share a trunk Y, so a levelled box top always frames
    the same thing in each and lines up something a viewer reads.  A grid
    column's sections share no trunk X: the space between a box's anchored edge
    and the content nearest it is that section's runway, and two column mates
    only mean the same thing by it when that content stands at one X.  Level
    those and the shared edge reads as one runway; level a mate whose content
    starts further in and it gains an empty band the width of the difference
    instead.

    Two kinds of section break the run either way.  A rail-flagged one, because
    its internal geometry is re-derived from its bbox downstream
    (:func:`...engine._retrofit_section_rails_phase`), so growing its anchored
    edge slides its stations along with it rather than widening a runway in front
    of them.  And one whose exit port rides the edge under test, because that
    port's coordinate is where the inter-section route leaves and the clearances
    downstream of it are measured from there -- a cosmetic levelling must not
    move it.
    """
    runs: list[list[Section]] = []
    current: list[Section] = []
    anchor = 0.0
    edge_side = PortSide.LEFT if sign > 0 else PortSide.RIGHT
    for section in group:
        xs = [graph.stations[sid].x for sid in _content_station_ids(graph, section)]
        exits_on_edge = any(
            (port := graph.ports.get(pid)) is not None and port.side == edge_side
            for pid in section.exit_ports
        )
        if graph.is_rail_section(section.id) or exits_on_edge or not xs:
            if len(current) >= 2:
                runs.append(current)
            current = []
            continue
        first = min(xs) if sign > 0 else max(xs)
        if current and abs(first - anchor) <= SAME_COORD_TOLERANCE:
            current.append(section)
            continue
        if len(current) >= 2:
            runs.append(current)
        current, anchor = [section], first
    if len(current) >= 2:
        runs.append(current)
    return runs


def level_group_anchor_edges(
    graph: MetroGraph,
    run: list[Section],
    axis: str,
    sign: float,
    limit: Callable[[Section], float] | None = None,
) -> None:
    """Grow every box in *run* out to the run's outermost *sign*-anchored edge.

    The one levelling primitive both grid axes use: a grid row levels its boxes'
    tops (``axis="y"``, ``sign=+1``), a grid column each of its members' X edges
    (:func:`_level_column_anchor_edges`).  Only the edge named by *sign* and the
    box's size move, so interiors and the opposite edge stay as they were, and
    the ports riding the moved edge are carried with it.

    *limit* bounds each box's reach on the anchored side, for callers whose
    growth can eat into a neighbour's corridor; ``None`` leaves it unbounded.
    """
    outermost = [section_anchor_edge(s, axis, sign) for s in run]
    target = min(outermost) if sign > 0 else max(outermost)
    for section in run:
        reach = target
        if limit is not None:
            bound = limit(section)
            reach = max(target, bound) if sign > 0 else min(target, bound)
        grow_section_bbox_to_anchor(graph, section, axis, sign, reach)


COLUMN_ANCHOR_SIGNS = (1.0, -1.0)


def _level_column_anchor_edges(graph: MetroGraph) -> None:
    """Level each X bbox edge across the column mates anchored to it.

    The X half of :func:`level_group_anchor_edges`.  A grid row levels one edge,
    its tops, because it is the header badge -- text, which a rotation does not
    carry with it -- that rides the box top.  Neither X edge is privileged that
    way, so both are levelled, each across the runs whose content nearest it
    stands at one X (:func:`_shared_anchor_runway_runs`).  A neighbour whose own
    row band the growth would eat into holds a box short of the shared edge
    (:func:`_column_neighbour_anchor_limit`).
    """
    for group in _column_contiguous_row_groups(graph):
        for sign in COLUMN_ANCHOR_SIGNS:
            limit = partial(_column_neighbour_anchor_limit, graph, sign=sign)
            for run in _shared_anchor_runway_runs(graph, group, sign):
                level_group_anchor_edges(graph, run, "x", sign, limit)


def _section_band_is_empty(graph: MetroGraph, section: Section) -> bool:
    """True when the band above ``section``'s topmost content is empty.

    Empty means no ``is_port`` station and no ``__bypass_`` helper sits
    above the highest content marker.  Such a band carries nothing, so
    the bbox top can be lowered to hug content.  A port or bypass helper
    above content is intentional runway for that port's approach and must
    not be shrunk into.
    """
    content_min_ys = _content_station_ys(graph, section)
    if not content_min_ys:
        return False
    # The side entry's approach occupies the band above the first station, and
    # Stage 6.16 has not yet lifted the entry port off that station when this
    # shrink runs at Stage 6.15a, so key off structure rather than the port Y.
    if _is_side_entered_vertical_section(graph, section):
        return False
    topmost = min(content_min_ys)
    for sid in section.station_ids:
        st = graph.stations.get(sid)
        if st is None:
            continue
        if (st.is_port or is_bypass_v(sid)) and st.y < topmost - SAME_COORD_TOLERANCE:
            return False
    return True


def _reserve_row_gap_for_top_padding(
    graph: MetroGraph,
    section_y_padding: float,
    section_y_gap: float,
    section_ids: set[str] | None = None,
) -> None:
    """Push a stacked row down when the row above blocks its top padding.

    A fan-redistribution pass can lift a section's highest marker above the
    content-top line its bbox was sized for.  :func:`_fit_bboxes_to_content_top`
    then tries to grow the bbox top back to a full ``section_y_padding`` band,
    but the row-above ceiling (``section_y_gap + SECTION_HEADER_PROTRUSION``)
    forbids the grow when a same-column section sits directly above, leaving the
    padding short.  Growing up is blocked, so widen the inter-row gap instead:
    shift the short section's row (and every row below) down by the shortfall so
    the subsequent restore reaches the full band while header clearance to the
    row above is preserved.

    Scoped to sections whose highest marker sits within ``section_y_padding`` of
    the box top: a section already at or above the full-padding line is
    untouched, so the push fires only where a fan-lift left a section crowded
    against the box top.  The full-band target and its row-above ceiling both come
    from :func:`_section_fit_top` (``fit_top - hug`` is the shortfall the ceiling
    imposed), so the ceiling formula lives in one place.

    ``section_ids`` narrows which sections may raise a deficit, so a caller
    downstream of a scoped late placement widens a row only for the content
    that placement owns.  The shift stays whole-row either way: the widened gap
    belongs to the row boundary, not to one box.
    """
    from nf_metro.layout.routing import compute_station_offsets

    offsets = compute_station_offsets(graph)
    max_row = max(
        (s.grid_row + s.grid_row_span - 1 for s in graph.sections.values()),
        default=0,
    )
    for r in range(1, max_row + 1):
        deficit = 0.0
        for sec in graph.sections.values():
            if sec.grid_row != r or sec.bbox_h <= 0:
                continue
            if section_ids is not None and sec.id not in section_ids:
                continue
            port_ids = set(sec.entry_ports) | set(sec.exit_ports)
            marker_ys = [
                graph.stations[sid].y
                for sid in sec.station_ids
                if sid in graph.stations
                and sid not in port_ids
                and not graph.stations[sid].is_hidden
            ]
            if not marker_ys:
                continue
            if min(marker_ys) - sec.bbox_y >= section_y_padding - SAME_COORD_TOLERANCE:
                continue
            hug = _section_content_hug_top(graph, sec, section_y_padding, offsets)
            fit_top = _section_fit_top(
                graph, sec, section_y_padding, section_y_gap, offsets
            )
            if hug is None or fit_top is None:
                continue
            deficit = max(deficit, fit_top - hug)
        if deficit <= SAME_COORD_TOLERANCE:
            continue
        _shift_rows_from(graph, r, deficit)


def _fit_bboxes_to_content_top(
    graph: MetroGraph,
    section_y_padding: float,
    section_y_gap: float,
) -> None:
    """Fit section bbox tops to content: grow to keep a full padding band,
    and shrink to reclaim a genuinely empty flush band.

    Grow side (issue #406): fan-redistribution passes (Stages 4.9 / 4.10 /
    6.7 / 6.11) can lift a branch above the content-top line the bbox was
    sized for, crowding the topmost marker against the bbox top while the
    bottom keeps its full ``section_y_padding`` band.  Growing the top to
    :func:`_section_fit_top` (content-hug, bounded by the row-above
    ceiling) restores the band.

    Shrink side: the transient row-top flush
    (:func:`_top_align_row_bboxes_only`) can leave a short section's top
    flushed up to a tall fan/off-track row-mate with empty space above its
    content.  When that band carries nothing
    (:func:`_section_band_is_empty`), lower the top to the ceiling-free
    :func:`_section_content_hug_top`.  A band holding a port or bypass
    helper is left intact.

    The grow branch keeps precedence, so a section whose top the row-above
    ceiling pushed down is grown rather than shrunk into the badge.  Both
    moves go through the bidirectional :func:`move_section_bbox_min_edge`
    (TOP ports follow the new edge).

    """
    from nf_metro.layout.routing import compute_station_offsets

    offsets = compute_station_offsets(graph)
    for section in graph.sections.values():
        if section.bbox_h <= 0:
            continue
        target = _section_fit_top(
            graph, section, section_y_padding, section_y_gap, offsets
        )
        if target is not None and target < section.bbox_y - SAME_COORD_TOLERANCE:
            move_section_bbox_min_edge(graph, section, "y", target)
            continue
        hug = _section_content_hug_top(graph, section, section_y_padding, offsets)
        if (
            hug is not None
            and hug > section.bbox_y + SAME_COORD_TOLERANCE
            and _section_band_is_empty(graph, section)
        ):
            move_section_bbox_min_edge(graph, section, "y", hug)


def refit_empty_section_tops_to_content(
    graph: MetroGraph,
    section_ids: set[str],
    section_y_padding: float,
    offsets: dict[tuple[str, str], float] | None = None,
) -> None:
    """Remove empty top slack after a scoped late content placement.

    A late semantic owner may move section content after the corpus-wide bbox
    fit.  Only sections named by that owner are eligible here, and only when the
    band above their highest content carries no port or bypass approach.
    """
    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)

    for section_id in section_ids:
        section = graph.sections.get(section_id)
        if (
            section is None
            or section.bbox_h <= 0
            or not _section_band_is_empty(graph, section)
        ):
            continue
        hug = _section_content_hug_top(graph, section, section_y_padding, offsets)
        if hug is not None and hug > section.bbox_y + SAME_COORD_TOLERANCE:
            move_section_bbox_min_edge(graph, section, "y", hug)


def grow_section_bands_to_content(
    graph: MetroGraph,
    section_ids: set[str],
    section_y_padding: float,
    section_y_gap: float,
    offsets: dict[tuple[str, str], float] | None = None,
) -> None:
    """Restore the padding band of sections a late content placement resized.

    The corpus-wide top fit and bottom shrink size each box around where the
    content stood when they ran.  A later owner that re-seats content inside a
    named section leaves the box stating the old band, so the marker it moved
    outward crowds an edge.  Growing each named box back to the same two
    targets the padding contract is written in -- the row-bounded content top
    and the pill-aware content bottom -- restores the band without moving
    anything the placement decided.
    """
    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)

    for section_id in section_ids:
        section = graph.sections.get(section_id)
        if section is None or section.bbox_h <= 0:
            continue
        top = _section_fit_top(
            graph, section, section_y_padding, section_y_gap, offsets
        )
        if top is not None:
            grow_section_bbox_min_edge(graph, section, "y", top)
        bottom = _predict_section_content_bottom(
            graph, section, section_y_padding, offsets
        )
        if bottom is not None:
            grow_section_bbox_max_edge(graph, section, "y", bottom)


def refit_tops_after_entry_resnap(
    graph: MetroGraph,
    section_ids: set[str],
    section_y_padding: float,
    offsets: dict[tuple[str, str], float] | None = None,
) -> None:
    """Give back top slack a re-snapped perpendicular entry port no longer needs.

    Stage 6.16 is the last mover of these ports.  A section whose port it shifts
    *down* keeps the taller top it was given while the port sat higher, so it ends
    up carrying more space above its content than a mirror-image row-mate whose
    port -- being an exit -- that stage never touches.

    Lowers the top to :func:`_section_content_hug_top`, which reserves
    ``PERP_PORT_EDGE_INSET`` beyond the port itself, so this cannot shrink into
    the port's own approach.  Clamped to the feeder row-mate's top, because a
    side-entered vertical section's top must not drop below it
    (:func:`_guard_side_entered_vertical_top_not_below_feeder`), and never raises
    the top: growing is Stage 6.15a's job, bounded there by the row above.
    """
    if offsets is None:
        from nf_metro.layout.routing import compute_station_offsets

        offsets = compute_station_offsets(graph)

    feeder_tops = {
        section.id: neighbour.bbox_y
        for section, neighbour in _side_entered_vertical_feeder_pairs(graph)
    }
    for sid in section_ids:
        section = graph.sections.get(sid)
        if section is None or section.bbox_h <= 0:
            continue
        hug = _section_content_hug_top(graph, section, section_y_padding, offsets)
        if hug is None:
            continue
        feeder_top = feeder_tops.get(sid)
        if feeder_top is not None:
            hug = min(hug, feeder_top)
        if hug > section.bbox_y + SAME_COORD_TOLERANCE:
            move_section_bbox_min_edge(graph, section, "y", hug)


def _top_align_side_entered_vertical_to_feeder(graph: MetroGraph) -> None:
    """Grow a side-entered vertical section's bbox top up to its feeder
    row-mate's top.

    When a horizontal-flow row-mate grows its top to fit a branch fanned
    above its trunk (:func:`_fit_bboxes_to_content_top`), a side-entered
    vertical (TB/BT) section beside it keeps its content-hugged top, dropping
    its number badge below the rest of the grid row.  The band above such a
    section's first station carries the perpendicular entry approach, so
    growing the top upward (stations stay put) re-levels the badge without
    disturbing routing.  Growth is upward only, so a section already at or
    above its feeder is untouched, and empty-band sections with no side entry
    keep their content-hug.

    A no-op unless ``graph.row_align == "top"``: the default content-hugging
    mode leaves a side-entered vertical section's badge at its own content top.
    """
    if graph.row_align != "top":
        return
    for section, neighbour in _side_entered_vertical_feeder_pairs(graph):
        if section.bbox_y - neighbour.bbox_y > SAME_COORD_TOLERANCE:
            grow_section_bbox_min_edge(graph, section, "y", neighbour.bbox_y)


def _tighten_lower_rows_after_shrink(graph: MetroGraph, section_y_gap: float) -> None:
    """Phase 2 of :func:`_shrink_and_tighten_rows`.

    Pull lower-row sections up to close the slack revealed once
    phase 1 collapsed bbox bottoms.  For each row ``r >= 1``, measure
    the gap between row ``r``'s current top and the max bbox bottom
    of sections that *end* at row ``r - 1``.  Rowspan sections that
    *extend into* row ``r`` are excluded -- their bbox bottom is now
    content-bounded, not row-bounded, so they no longer constrain
    row ``r``'s top.  Any slack beyond ``section_y_gap`` is closed
    by shifting sections in row ``r`` and below (along with their
    stations and ports) upward by that amount.  Junctions live in
    inter-section space and routing recomputes after layout, so
    their positions are left alone.
    """
    if not graph.sections:
        return

    from nf_metro.layout.section_placement import (
        _inter_row_routing_minimums,
        _merge_trunk_row_minimums,
    )

    # A horizontal run an inter-row gap must host -- an entry-wrap bundle or a
    # bottommost-row merge-trunk channel -- needs a wider gap than the bare
    # ``section_y_gap`` so it clears both bounding sections; honour that
    # minimum here so tightening doesn't reclaim the space
    # ``_enforce_min_row_gaps`` reserved at placement.
    routing_min = _inter_row_routing_minimums(graph)
    envelope_min = _merge_trunk_row_minimums(graph)

    sections_by_start_row: dict[int, list[Section]] = defaultdict(list)
    sections_by_end_row: dict[int, list[Section]] = defaultdict(list)
    for s in graph.sections.values():
        if s.bbox_h <= 0:
            continue
        sections_by_start_row[s.grid_row].append(s)
        sections_by_end_row[s.grid_row + s.grid_row_span - 1].append(s)
    if not sections_by_start_row:
        return
    max_row = max(sections_by_end_row)
    connected_sections: dict[str, set[str]] = defaultdict(set)
    for edge in graph.edges:
        source_section = graph.section_for_station(edge.source)
        target_section = graph.section_for_station(edge.target)
        if (
            source_section is None
            or target_section is None
            or source_section == target_section
        ):
            continue
        connected_sections[source_section].add(target_section)
        connected_sections[target_section].add(source_section)
    struct = graph._struct_height_below_top

    def _structural_bottom(section: Section) -> float:
        """Return the settled bottom, honouring an earlier height snapshot."""
        height = section.bbox_h
        if struct:
            height = max(struct.get(section.id, height), height)
        return section.bbox_y + height

    def _sections_constrain(upper: Section, lower: Section) -> bool:
        upper_hi = upper.grid_col + upper.grid_col_span - 1
        lower_hi = lower.grid_col + lower.grid_col_span - 1
        grid_overlap = not (upper_hi < lower.grid_col or lower_hi < upper.grid_col)
        bbox_overlap = not (
            upper.bbox_x + upper.bbox_w <= lower.bbox_x + SAME_COORD_TOLERANCE
            or lower.bbox_x + lower.bbox_w <= upper.bbox_x + SAME_COORD_TOLERANCE
        )
        return (
            grid_overlap
            or bbox_overlap
            or lower.id in connected_sections.get(upper.id, ())
        )

    upper_sections: list[Section] = []
    for r in range(1, max_row + 1):
        upper_sections.extend(sections_by_end_row.get(r - 1, []))
        lower = sections_by_start_row.get(r, [])
        ending_at_prev = sections_by_end_row.get(r - 1, [])
        if not lower or not ending_at_prev:
            continue

        # Bypass routes dip below intervening bboxes into the inter-row
        # gap; tightening must not pull lower rows up into them.
        bypass_spans = _aggregate_bypass_spans(graph, ending_at_prev)
        target_gap = max(section_y_gap, routing_min.get((r - 1, r), 0.0))
        # The whole row shifts up by one amount, so it can rise only as far as
        # its most-constrained section allows -- hence the min over ``lower``.
        # Each lower section's floor counts only earlier-row sections and bypass
        # spans whose columns it actually sits under.  Looking through every
        # earlier row is necessary because a tall bbox can extend past its
        # immediate lower neighbour.  A tall box or route in another column
        # does not reserve empty vertical space here.  Because the whole row
        # moves by one amount, unconstrained members can be omitted whenever
        # another member supplies a real column constraint.  Fall back to the
        # preceding row's global floor only when the row has no local constraint
        # at all.
        global_floor = max(_structural_bottom(us) for us in ending_at_prev)
        constrained: list[tuple[Section, float]] = []
        for ls in lower:
            ls_lo = ls.grid_col
            ls_hi = ls.grid_col + ls.grid_col_span - 1
            overlapping_floors = [
                _structural_bottom(us)
                for us in upper_sections
                if _sections_constrain(us, ls)
            ]
            floors = list(overlapping_floors)
            for (lo, hi), bypass_bot in bypass_spans.items():
                if ls_hi < lo or ls_lo > hi:
                    continue
                floors.append(bypass_bot)
            if floors:
                constrained.append((ls, max(floors)))
        if not constrained:
            constrained = [(ls, global_floor) for ls in lower]
        slack = min(ls.bbox_y - (floor + target_gap) for ls, floor in constrained)
        envelope_gap = envelope_min.get((r - 1, r), 0.0)
        if envelope_gap:
            # A merge trunk's channel spans the boundary between the two row
            # envelopes, so no column-overlapping pair bounds it -- and the
            # parser rewrote its connectors through fan and merge nodes, so no
            # section pair records the two rows as related at all.
            envelope_top = min(ls.bbox_y for ls in lower)
            slack = min(slack, envelope_top - (global_floor + envelope_gap))
        if slack <= SAME_COORD_TOLERANCE:
            continue

        _shift_rows_from(graph, r, -slack, reposition_junctions=False)


def _min_section_bbox_top(graph: MetroGraph, default: float) -> float:
    """Smallest ``bbox_y`` among non-empty sections, or ``default``."""
    return min(
        (s.bbox_y for s in graph.sections.values() if s.bbox_h > 0),
        default=default,
    )


def _min_drawn_section_bbox_top(graph: MetroGraph) -> float | None:
    """Smallest ``bbox_y`` among sections that draw a visible box, or ``None``.

    Excludes implicit holders, which draw neither a box nor a header badge and
    so cannot crowd the map title.
    """
    return min(
        (s.bbox_y for s in graph.real_sections.values() if s.bbox_h > 0),
        default=None,
    )
