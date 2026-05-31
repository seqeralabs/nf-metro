"""Grow and shrink section bboxes to fit content and predicted bypass spans."""

from __future__ import annotations

from collections import defaultdict

from nf_metro.layout.constants import (
    BYPASS_CLEARANCE,
    CURVE_RADIUS,
    DIAGONAL_RUN,
    MIN_STATION_FLAT_LENGTH,
    MIN_STRAIGHT_EDGE,
    MIN_STRAIGHT_PORT,
    SECTION_HEADER_PROTRUSION,
)
from nf_metro.layout.labels import label_text_width
from nf_metro.layout.phases._common import (
    _bbox_cols_overlap,
    _content_station_ys,
    _set_section_bbox_top,
)
from nf_metro.layout.phases.single_section import _terminus_y_overhang
from nf_metro.parser.model import MetroGraph, Section, Station


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

    def _node_section(node_id: str):
        st = graph.stations.get(node_id) or graph.ports.get(node_id)
        if st is None:
            return None
        sec_id = getattr(st, "section_id", None)
        return graph.sections.get(sec_id) if sec_id else None

    resolve_cache: dict[tuple[str, bool], Section | None] = {}

    def _resolve(node_id: str, upstream: bool, visited: set[str] | None = None):
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
    graph: MetroGraph, upper_sections: list[Section]
) -> dict[tuple[int, int], float]:
    """Aggregate bypass span->bottom predictions across upper sections.

    A row-spanning section carries its bypass routes from its start row
    down to the row below its end row, so the prediction must key off
    ``grid_row`` (start), not the end row.
    """
    combined: dict[tuple[int, int], float] = {}
    for upper_start_row in {s.grid_row for s in upper_sections}:
        for span, bot in _predicted_bypass_bottom_in_row(
            graph, upper_start_row
        ).items():
            if bot > combined.get(span, 0.0):
                combined[span] = bot
    return combined


def _push_lower_rows_after_bbox_grow(graph: MetroGraph, section_y_gap: float) -> None:
    """Push lower-row sections down when an upper-row bbox grows.

    Shared helper called by stages that may grow a section's
    ``bbox_h`` downward after row offsets are already fixed (e.g.
    ``_shift_and_propagate_loop_stations`` at Stage 6.14, the sparse
    loop-station shift).  Row offsets were fixed earlier by
    ``_compute_section_offsets`` from pre-grow bbox heights, so the
    section below a grown one can end up sitting closer than
    ``section_y_gap`` from the new bbox bottom.

    For each row ``r >= 1``, measure the deficit between the lowest
    bbox bottom of sections ending at row ``r - 1`` and the top of
    sections at row ``r``, but only count pairs whose column spans
    overlap.  Two sections that share a vertical edge in column space
    must keep ``section_y_gap`` between them; sections in different
    columns can sit with smaller (or no) vertical separation without
    visual interference.  If a positive deficit remains, shift row
    ``r`` and below downward by that deficit (sections + stations +
    ports).  Junctions live in inter-section space and are reproduced
    by routing.
    """
    if not graph.sections:
        return

    sections_by_row_start: dict[int, list[Section]] = defaultdict(list)
    for s in graph.sections.values():
        sections_by_row_start[s.grid_row].append(s)
    if not sections_by_row_start:
        return
    max_row = max(s.grid_row + s.grid_row_span - 1 for s in graph.sections.values())

    def _cols_overlap(a: Section, b: Section) -> bool:
        a_start = a.grid_col
        a_end = a_start + a.grid_col_span - 1
        b_start = b.grid_col
        b_end = b_start + b.grid_col_span - 1
        return not (a_end < b_start or b_end < a_start)

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
        bypass_by_span = _aggregate_bypass_spans(graph, ending_at_prev)

        # Only consider column-overlapping (upper, lower) pairs for
        # deficit computation: a tall upper-row bbox that lives in a
        # different column from the lower-row content does not need
        # additional vertical clearance to satisfy the row gap.
        deficit = 0.0
        for us in ending_at_prev:
            for ls in lower:
                if ls.bbox_h <= 0:
                    continue
                if not _cols_overlap(us, ls):
                    continue
                upper_bot = us.bbox_y + us.bbox_h
                lower_top = ls.bbox_y
                d = (upper_bot + section_y_gap) - lower_top
                if d > deficit:
                    deficit = d
        # Bypass routes do not need column overlap with the upper-row
        # endpoint bbox; they only need column overlap with the lower
        # section they would otherwise crowd against.
        for (lo, hi), bypass_bot in bypass_by_span.items():
            for ls in lower:
                if ls.bbox_h <= 0:
                    continue
                ls_lo = ls.grid_col
                ls_hi = ls.grid_col + ls.grid_col_span - 1
                if ls_hi < lo or ls_lo > hi:
                    continue
                d = (bypass_bot + section_y_gap) - ls.bbox_y
                if d > deficit:
                    deficit = d
        if deficit <= 0.5:
            continue

        shifted_section_ids = {
            sid for sid, s in graph.sections.items() if s.grid_row >= r
        }
        for sid in shifted_section_ids:
            graph.sections[sid].bbox_y += deficit
        shifted_station_ids = set()
        for sid in shifted_section_ids:
            shifted_station_ids.update(graph.sections[sid].station_ids)
        for stid in shifted_station_ids:
            st = graph.stations.get(stid)
            if st is not None:
                st.y += deficit
            port = graph.ports.get(stid)
            if port is not None:
                port.y += deficit


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
            feeder_ys.append(src.y)

    _collect(station_id)
    if len(feeder_ys) < 2:
        return False
    return all(y >= anchor_y - 0.5 for y in feeder_ys)


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

    def _row_mate_bottoms(section: Section) -> list[float]:
        # Two policies depending on this section's direction:
        #
        # TB sections (folds) get their bbox grown by ``section_y_gap``
        # in section_placement so they visually span into the next row's
        # target.  Their intended bottom is the target row-mate's bottom,
        # which is in a different grid row but Y-overlapping.  Honour
        # Y-overlap for these so the bottom-alignment from Stage 6.5 /
        # the fold extension survives.
        #
        # LR/RL sections use ONLY their STARTING grid row to find
        # row-mates.  Counting this section's rowspan would pull in
        # sections from rows the rowspan claims but doesn't fill -- a
        # rowspan=2 LR sidebar whose content fits in row 0 must not be
        # pinned to a row-1 neighbour just because its declared span
        # overlaps row 1.  Y-overlap is intentionally excluded for
        # LR/RL: a stale pre-shrink bbox would otherwise be
        # self-protecting (the overlap blocks the shrink that would
        # remove the overlap).
        my_grid_row = section.grid_row if section.grid_row >= 0 else None
        my_y_top = section.bbox_y
        my_y_bot = section.bbox_y + section.bbox_h
        # LR/RL sections with grid coords match on starting row only;
        # TB sections (and any unplaced section) fall back to bbox-Y
        # overlap.  See block comment above for the why.
        use_grid = section.direction != "TB" and my_grid_row is not None
        out: list[float] = []
        for other in graph.sections.values():
            if other.id == section.id or other.bbox_h <= 0:
                continue
            o_y_bot = other.bbox_y + other.bbox_h
            if use_grid and other.grid_row >= 0:
                o_grid_top = other.grid_row
                o_grid_bot = other.grid_row + max(1, other.grid_row_span)
                mate = o_grid_top <= my_grid_row < o_grid_bot
            else:
                mate = other.bbox_y < my_y_bot and o_y_bot > my_y_top
            if mate:
                out.append(o_y_bot)
        return out

    v_curve_clearance = CURVE_RADIUS + MIN_STATION_FLAT_LENGTH / 2
    for section in graph.sections.values():
        if section.bbox_h <= 0:
            continue
        section_dir = section.direction or "LR"
        # Each non-port station reserves at least ``section_y_padding`` below
        # its marker; a TB/BT terminus whose icons hang below reserves their
        # full vertical extent instead.  (Overhang is 0 for LR/RL, so this
        # stays byte-identical there.)
        content_bots = [
            graph.stations[sid].y
            + max(
                section_y_padding,
                _terminus_y_overhang(graph.stations[sid], section_dir, graph)[1],
            )
            for sid in section.station_ids
            if (
                sid in graph.stations
                and not graph.stations[sid].is_port
                and not sid.startswith("__bypass_")
            )
        ]
        bypass_max_ys = [
            graph.stations[sid].y
            for sid in section.station_ids
            if sid in graph.stations and sid.startswith("__bypass_")
        ]
        port_max_ys = [
            graph.stations[sid].y
            for sid in section.station_ids
            if sid in graph.stations and graph.stations[sid].is_port
        ]
        if not content_bots:
            continue
        content_bot = max(content_bots)
        if bypass_max_ys:
            content_bot = max(content_bot, max(bypass_max_ys) + v_curve_clearance)
        if port_max_ys:
            content_bot = max(content_bot, max(port_max_ys))
        current_bot = section.bbox_y + section.bbox_h
        if content_bot > current_bot + 0.5:
            section.bbox_h = content_bot - section.bbox_y
            continue
        desired_bot = content_bot
        mate_bots = _row_mate_bottoms(section)
        if mate_bots:
            desired_bot = max(desired_bot, max(mate_bots))
        new_h = desired_bot - section.bbox_y
        if new_h < section.bbox_h - 0.5:
            section.bbox_h = max(0.0, new_h)


def _section_fit_top(
    graph: MetroGraph,
    section: Section,
    section_y_padding: float,
    section_y_gap: float,
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

    Returns ``None`` when the section has no real content to anchor to.
    """
    target = _section_content_hug_top(graph, section, section_y_padding)
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
) -> float | None:
    """Ceiling-free content-hug top for ``section``.

    The shrink twin of :func:`_section_fit_top`: the same content-hug
    (``section_y_padding`` above the highest content marker, clamped to
    keep bypass helpers and ports inside) but WITHOUT the row-above grow
    ceiling.  The ceiling is a grow-direction bound only -- lowering a
    too-tall top moves it away from the row above, so it never binds when
    hugging content downward.

    Returns ``None`` when the section has no real content to anchor to.
    """
    content_min_ys = _content_station_ys(graph, section)
    if not content_min_ys:
        return None
    bypass_min_ys = [
        graph.stations[sid].y
        for sid in section.station_ids
        if sid in graph.stations and sid.startswith("__bypass_")
    ]
    port_min_ys = [
        graph.stations[sid].y
        for sid in section.station_ids
        if sid in graph.stations and graph.stations[sid].is_port
    ]
    v_curve_clearance = CURVE_RADIUS + MIN_STATION_FLAT_LENGTH / 2
    target = min(content_min_ys) - section_y_padding
    if bypass_min_ys:
        target = min(target, min(bypass_min_ys) - v_curve_clearance)
    if port_min_ys:
        target = min(target, min(port_min_ys))
    return target


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
    topmost = min(content_min_ys)
    for sid in section.station_ids:
        st = graph.stations.get(sid)
        if st is None:
            continue
        if (st.is_port or sid.startswith("__bypass_")) and st.y < topmost - 0.5:
            return False
    return True


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
    moves go through the bidirectional :func:`_set_section_bbox_top` (TOP
    ports follow the new edge).
    """
    for section in graph.sections.values():
        if section.bbox_h <= 0:
            continue
        target = _section_fit_top(graph, section, section_y_padding, section_y_gap)
        if target is not None and target < section.bbox_y - 0.5:
            _set_section_bbox_top(graph, section, target)
            continue
        hug = _section_content_hug_top(graph, section, section_y_padding)
        if (
            hug is not None
            and hug > section.bbox_y + 0.5
            and _section_band_is_empty(graph, section)
        ):
            _set_section_bbox_top(graph, section, hug)


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

    from nf_metro.layout.section_placement import _wrap_bundle_row_minimums

    # An inter-row wrap bundle needs a wider gap than the bare
    # ``section_y_gap`` so its horizontal run clears both bounding
    # sections; honour that minimum here so tightening doesn't reclaim
    # the space ``_enforce_min_row_gaps`` reserved at placement.
    wrap_min = _wrap_bundle_row_minimums(graph)

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

    for r in range(1, max_row + 1):
        lower = sections_by_start_row.get(r, [])
        ending_at_prev = sections_by_end_row.get(r - 1, [])
        if not lower or not ending_at_prev:
            continue
        max_above_bot = max(s.bbox_y + s.bbox_h for s in ending_at_prev)
        # Bypass routes dip below intervening bboxes into the inter-row
        # gap; tightening must not pull lower rows up into them.
        bypass_spans = _aggregate_bypass_spans(graph, ending_at_prev)
        effective_floor = max(max_above_bot, max(bypass_spans.values(), default=0.0))
        current_top = min(s.bbox_y for s in lower)
        target_gap = max(section_y_gap, wrap_min.get((r - 1, r), 0.0))
        slack = current_top - (effective_floor + target_gap)
        if slack <= 0.5:
            continue

        for s in graph.sections.values():
            if s.grid_row < r:
                continue
            s.bbox_y -= slack
            for stid in s.station_ids:
                st = graph.stations.get(stid)
                if st is not None:
                    st.y -= slack


def _min_section_bbox_top(graph: MetroGraph, default: float) -> float:
    """Smallest ``bbox_y`` among non-empty sections, or ``default``."""
    return min(
        (s.bbox_y for s in graph.sections.values() if s.bbox_h > 0),
        default=default,
    )
