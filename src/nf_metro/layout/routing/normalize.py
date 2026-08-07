"""Channel and trunk normalization passes run after edge routing."""

from __future__ import annotations

import functools
import itertools
import math
from collections import defaultdict, deque
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from math import inf, isfinite
from typing import NamedTuple, TypeVar

from nf_metro.layout.constants import (
    BUNDLE_TO_BUNDLE_CLEARANCE,
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    CURVE_RADIUS,
    EDGE_TO_BUNDLE_CLEARANCE,
    INTER_ROW_EDGE_CLEARANCE,
    INTER_ROW_HEADER_CLEARANCE,
    MIN_CORRIDOR_Y_OVERLAP,
    NEXT_ROW_HEADER_BADGE_CLEARANCE,
    SECTION_HEADER_PROTRUSION,
    graph_offset_step,
)
from nf_metro.layout.geometry import (
    cotravelling_lane_clearance,
    cotravelling_lanes_fuse,
    spans_share_corridor,
)
from nf_metro.layout.routing.centrelines import (
    fan_offsets,
)
from nf_metro.layout.routing.common import (
    CorridorLane,
    Direction,
    GapSlot,
    HTrunkSeg,
    OffsetRegime,
    RoutedPath,
    _grid_row_bands,
    _h_segment_penetrates_section,
    column_gap_edges,
    convergence_owns_segment_boundary,
    corridor_lanes,
    corridor_runs,
    gap_lo_for_x,
    initial_fanout_descent_span,
    inter_row_gap_upper_row,
    is_orthogonal_turn,
    iter_eligible_destination_tail_bundles,
    iter_horizontal_trunks,
    iter_inter_row_gaps,
    iter_opposing_entry_confluences,
    iter_port_peeloff_bundles,
    iter_vertical_segments,
    merge_fanout_pivot_reference,
    opposing_entry_confluence_slots,
    packed_cell_neighbor_edges,
    peeloff_target_slots,
    perp_peeloff_off_horizontal_junction,
    planner_owns_segment,
    route_system_owns_segment_boundary,
    seat_peeloff_port_y,
    symmetric_bundle_midpoint,
    tail_on_slot,
    trunk_depths_contiguous,
    trunk_segments_cross,
)
from nf_metro.layout.routing.context import (
    _get_offset,
    _MergeRouting,
    _resolve_section_col,
    _RoutingCtx,
)
from nf_metro.layout.routing.corners import (
    concentric_corner_radius_at,
    concentric_reference_radius_at,
    corner_radius,
    resolve_curve_radii,
    resolve_curve_radius_at,
    widest_coincident_radius,
)
from nf_metro.layout.routing.families import RouteFamilyId
from nf_metro.layout.routing.offsets import (
    cross_row_convergence_channel_order,
)
from nf_metro.layout.routing.reserved_bands import (
    ReservedBand,
    corridor_clearance_band,
    resolved_band,
)
from nf_metro.parser.model import MetroGraph, Port, PortSide


@dataclass
class _VChannel:
    """One vertical channel segment of a routed inter-section path.

    Records the route, the segment's start index in ``route.points`` (so
    ``points[idx]`` and ``points[idx+1]`` are the channel endpoints), its
    current x, vertical span and direction, plus the indices of any
    flanking corners in ``route.curve_radii`` and whether each corner is
    on the OUTSIDE of its turn for this line (recomputed after re-stack).
    """

    route: RoutedPath
    idx: int
    x: float
    y_lo: float
    y_hi: float
    down: bool


_GroupKey = TypeVar("_GroupKey")
_GroupItem = TypeVar("_GroupItem")


def _group_channels_by(
    routes: list[RoutedPath],
    keyed_spans: Callable[[RoutedPath], Iterable[tuple[_GroupKey, _GroupItem]]],
) -> defaultdict[_GroupKey, list[_GroupItem]]:
    """Bucket per-route channel spans over the inter-section routes.

    ``keyed_spans`` yields ``(bucket_key, item)`` pairs for one route -- none,
    one, or several.  The fan reconciliation passes all share this
    iterate-the-inter-section-routes-and-bucket loop and differ only in which
    span they extract and how they key it, so each supplies its own
    ``keyed_spans`` and keeps its reseat/ordering/guard logic separate.

    Buckets preserve route order (and, within a route, span order): every pass
    reads a bucket's members positionally -- picking a reference member,
    ordering by rank -- so the collection order is part of each pass's contract.
    """
    groups: defaultdict[_GroupKey, list[_GroupItem]] = defaultdict(list)
    for rp in routes:
        if not rp.is_inter_section:
            continue
        for key, item in keyed_spans(rp):
            groups[key].append(item)
    return groups


def _split_corridors(chans: list[_VChannel]) -> list[list[_VChannel]]:
    """Split a bucket into corridors of substantially y-overlapping channels.

    Only channels whose y-spans overlap by more than
    :data:`MIN_CORRIDOR_Y_OVERLAP` share a true corridor; independent
    vertical runs at different heights - including two stacked descents that
    merely touch at a shared elbow band - must NOT be merged, so the gap
    layout can distribute them across the gap width instead of packing their
    opposing elbows together.
    """
    chans = sorted(chans, key=lambda c: (c.y_lo, c.y_hi))
    groups: list[list[_VChannel]] = []
    for ch in chans:
        placed = False
        for g in groups:
            if any(spans_share_corridor(ch.y_lo, ch.y_hi, o.y_lo, o.y_hi) for o in g):
                g.append(ch)
                placed = True
                break
        if not placed:
            groups.append([ch])
    return groups


def _section_intrudes(
    graph: MetroGraph,
    x: float,
    y_lo: float,
    y_hi: float,
    *,
    exclude: frozenset[str | None] = frozenset(),
) -> bool:
    """True if a channel at ``x`` over ``[y_lo, y_hi]`` lands inside a section bbox.

    Sections in ``exclude`` are skipped, for a channel that legitimately meets
    its own endpoints' sections.
    """
    for s in graph.sections.values():
        if s.id in exclude:
            continue
        if s.bbox_w <= 0:
            continue
        sx_l = s.bbox_x
        sx_r = s.bbox_x + s.bbox_w
        if sx_l - COORD_TOLERANCE < x < sx_r + COORD_TOLERANCE:
            sy_t = s.bbox_y
            sy_b = s.bbox_y + s.bbox_h
            if y_lo < sy_b and sy_t < y_hi:
                return True
    return False


def _anchored_bundle_midpoint(
    order: list[str],
    pins: dict[tuple[bool, str], float],
    down: bool,
    step: float,
    gap_left: float,
    gap_right: float,
) -> float | None:
    """Midpoint seating *order* so its pinned line lands on its owned column.

    A line whose column in this gap is already owned by a handler that keeps its
    own geometry cannot be moved by the concentric layout, yet a later
    coincidence fusion pulls this bundle's leg of that same line onto it.
    Centring the bundle on the gap instead leaves the fused leg crossing the
    siblings it was nested against, so seat the bundle on the pin.

    Multiple pins can anchor one bundle when they imply the same midpoint.
    ``None`` means the pins conflict, no line is pinned, or the anchored bundle
    would fall outside the gap; the caller then centres it as usual.
    """
    slots = fan_offsets(len(order), step)
    anchored = [
        (slots[i], pins[(down, lid)])
        for i, lid in enumerate(order)
        if (down, lid) in pins
    ]
    if not anchored:
        return None
    candidates = tuple(column - offset for offset, column in anchored)
    if any(
        abs(candidate - candidates[0]) > COORD_TOLERANCE for candidate in candidates[1:]
    ):
        return None
    mid = candidates[0]
    half = slots[-1]
    if mid - half < gap_left - COORD_TOLERANCE:
        return None
    if mid + half > gap_right + COORD_TOLERANCE:
        return None
    return mid


def _segment_claim_band(
    ctx: _RoutingCtx, route: RoutedPath, idx: int
) -> ReservedBand | None:
    """The reserved band the ledger claims for this route segment, if any."""
    return ctx.reserved_bands.for_segment(
        route.edge.source, route.edge.target, route.line_id, idx
    )


def _bundle_claim_band(
    ctx: _RoutingCtx, segments: Iterable[tuple[RoutedPath, int]]
) -> ReservedBand | None:
    """The band a bundle's own claimed segments allocate, if consistent.

    A bundle moves as one unit, so every claimed member's band must admit the
    shared placement: the bands intersect, and an empty intersection (or a
    bundle with no claimed member) publishes nothing, leaving the caller on
    its gap-edge derivation.
    """
    bands = [
        band
        for route, idx in segments
        if (band := _segment_claim_band(ctx, route, idx)) is not None
    ]
    if not bands:
        return None
    return resolved_band(max(b.lo for b in bands), min(b.hi for b in bands))


def _hold_bundle_in_claim_band(
    ctx: _RoutingCtx,
    mid: float,
    chans: list[_VChannel],
    width: float,
) -> float:
    """*mid* held so a *width*-wide bundle sits inside its own claim's band.

    A reservation states where its corridor may run, not where in that span it
    should sit, so a placement the gap edges already put inside the band is
    kept.  The whole bundle has to fit, so the midpoint may travel a half-width
    inboard of each edge, which for a band narrower than the bundle is no travel
    at all: it centres, leaving the deficit for the closing guard to report
    rather than picking an edge to overhang.
    """
    band = _bundle_claim_band(ctx, ((ch.route, ch.idx) for ch in chans))
    if band is None:
        return mid
    centre = (band.lo + band.hi) / 2
    reach = max(band.hi - band.lo - width, 0.0) / 2
    return ReservedBand(centre - reach, centre + reach).hold(mid)


def _layout_gap_bundle(
    bundles: list[tuple[bool, list[_VChannel]]],
    gap_left: float,
    gap_right: float,
    ctx: _RoutingCtx,
    pins: dict[tuple[bool, str], float] | None = None,
) -> None:
    """Lay out one ``(gap, row)``'s bundles concentrically, centred in the gap.

    A bundle whose members claim a reserved corridor is then held inside that
    claim's own band, which settlement sized for exactly this bundle over the
    corridor's own span -- the shared gap edges name whichever sections happen
    to sit in the two columns, which is a different and wider set of blockers.
    """
    step = ctx.offset_step
    # Stable left-to-right order: by current bundle centre.
    bundles.sort(key=lambda b: sum(c.x for c in b[1]) / len(b[1]))
    # Distinct-line count per bundle drives the bundle width and the
    # per-line slotting: multiple segments sharing one line_id (a fan
    # whose line feeds several targets) overlay at a single x rather
    # than each claiming an OFFSET_STEP slot.
    line_orders = [
        _convergence_line_order(c, ctx.graph) or _distinct_line_order(c)
        for _, c in bundles
    ]
    # Skip a lone bundle carrying a single distinct line: nothing to
    # re-bundle and centring risks disturbing wrap geometry.
    if len(bundles) == 1 and len(line_orders[0]) <= 1:
        return
    widths = [max(0, len(o) - 1) * step for o in line_orders]
    # A lone bundle centres on the true gap midpoint (symmetric clearance
    # both sides) rather than flooring one edge at A, which would push the
    # bundle off-centre when the gap is sized tighter than 2A + width.
    # Multi-bundle gaps keep the symmetric A/B layout.
    lone = len(bundles) == 1
    for bi, (_down, chans) in enumerate(bundles):
        order = line_orders[bi]
        # The concentric outside/inside assignment mirrors with the trunk's
        # travel direction: a leftward bypass's descent is the mirror of a
        # rightward one, so its largest radius sits on the LEFT.  Read the leg
        # direction from geometry; fall back to the bundle's vertical sense.
        anchored = _anchored_bundle_midpoint(
            order, pins or {}, _down, step, gap_left, gap_right
        )
        if anchored is not None:
            mid = anchored
        elif lone:
            mid = (gap_left + gap_right) / 2
        else:
            mid = symmetric_bundle_midpoint(gap_left, gap_right, widths, bi)
        mid = _hold_bundle_in_claim_band(ctx, mid, chans, widths[bi])
        n = len(order)
        # line_id -> (slot index, x); every segment of that line overlays
        # at its single slot rather than claiming an OFFSET_STEP each.
        slots = fan_offsets(n, step)
        line_slot = {lid: (i, mid + slots[i]) for i, lid in enumerate(order)}
        targets = [(ch, line_slot[ch.route.line_id]) for ch in chans]
        # Intrusion guard: if any target x would land inside a section bbox
        # (e.g. the gap bounds came from another row), leave this bundle
        # untouched rather than route through a section.
        if any(
            _section_intrudes(ctx.graph, nx, ch.y_lo, ch.y_hi)
            for ch, (_li, nx) in targets
        ):
            continue
        for ch, (li, nx) in targets:
            _restack_channel(ch, nx, li, n, step, ctx.curve_radius)
            ch.x = nx


def _required_channel_clearance(
    a: _VChannel, b: _VChannel, curve_radius: float
) -> float:
    """Required spacing for counter-running channels in one corridor.

    Same-direction channels are nested by the bundle passes rather than spaced
    here, so this asks nothing of them.
    """
    if a.down is b.down:
        return 0.0
    overlap = min(a.y_hi, b.y_hi) - max(a.y_lo, b.y_lo)
    if overlap <= MIN_CORRIDOR_Y_OVERLAP:
        return 0.0
    return cotravelling_lane_clearance(
        same_line=a.route.line_id == b.route.line_id,
        counter_running=True,
        curve_radius=curve_radius,
    )


def _overlays_distinct_line(
    channel: _VChannel, x: float, obstacle: _VChannel, step: float
) -> bool:
    """Whether seating *channel* at *x* would fuse it onto *obstacle*'s stroke.

    The candidate-column veto reads the shared fusion predicate at a whole
    coordinate tolerance: it is choosing between candidate columns rather than
    judging drawn geometry, so a column within a tolerance of the full step is
    close enough to count as taken.
    """
    return (
        channel.down is obstacle.down
        and channel.route.line_id != obstacle.route.line_id
        and cotravelling_lanes_fuse(
            x,
            obstacle.x,
            (channel.y_lo, channel.y_hi),
            (obstacle.y_lo, obstacle.y_hi),
            step,
            slack=COORD_TOLERANCE,
        )
    )


def _channels_form_symmetric_divergence(a: _VChannel, b: _VChannel) -> bool:
    """Whether same-line siblings split up and down from one trunk endpoint."""
    if (
        a.down is b.down
        or a.route.line_id != b.route.line_id
        or a.route.edge.source != b.route.edge.source
    ):
        return False
    overlap = min(a.y_hi, b.y_hi) - max(a.y_lo, b.y_lo)
    if abs(overlap) > COORD_TOLERANCE:
        return False

    def _opening_trunk(
        channel: _VChannel,
    ) -> tuple[tuple[float, float], float, int] | None:
        if channel.idx == 0:
            return None
        start = channel.route.points[channel.idx - 1]
        turn = channel.route.points[channel.idx]
        dx = turn[0] - start[0]
        if abs(turn[1] - start[1]) > COORD_TOLERANCE or abs(dx) <= COORD_TOLERANCE:
            return None
        return start, turn[1], 1 if dx > 0 else -1

    a_trunk = _opening_trunk(a)
    b_trunk = _opening_trunk(b)
    if a_trunk is None or b_trunk is None:
        return False
    a_start, a_y, a_direction = a_trunk
    b_start, b_y, b_direction = b_trunk
    return (
        abs(a_start[0] - b_start[0]) <= COORD_TOLERANCE
        and abs(a_start[1] - b_start[1]) <= COORD_TOLERANCE
        and abs(a_y - b_y) <= COORD_TOLERANCE
        and a_direction == b_direction
    )


def _anchor_same_direction_fixed_channels(
    bundles: list[tuple[bool, list[_VChannel]]],
    fixed: list[_VChannel],
    ctx: _RoutingCtx,
) -> None:
    """Extend movable same-direction bundles from their fixed member's column."""
    movable = [ch for _down, group in bundles for ch in group]
    for down, chans in bundles:
        if any(
            _channels_form_symmetric_divergence(ch, sibling)
            for ch in chans
            for sibling in movable
            if sibling is not ch
        ):
            continue
        anchors = [
            obstacle
            for obstacle in fixed
            if obstacle.down is down
            and all(obstacle.route.line_id != ch.route.line_id for ch in chans)
            and any(
                spans_share_corridor(ch.y_lo, ch.y_hi, obstacle.y_lo, obstacle.y_hi)
                for ch in chans
            )
        ]
        if not anchors:
            continue
        combined = [*anchors, *chans]
        order = _distinct_line_order(combined)
        rank = {line_id: i for i, line_id in enumerate(order)}
        bases = [
            anchor.x - rank[anchor.route.line_id] * ctx.offset_step
            for anchor in anchors
        ]
        if any(abs(base - bases[0]) > COORD_TOLERANCE for base in bases[1:]):
            continue
        base = bases[0]
        for ch in chans:
            target = base + rank[ch.route.line_id] * ctx.offset_step
            if abs(target - ch.x) <= COORD_TOLERANCE:
                continue
            _set_vchannel_x(ch, target)
            ch.x = target


def _separate_opposing_gap_bundles(
    bundles: list[tuple[bool, list[_VChannel]]],
    fixed: list[_VChannel],
    gap_left: float,
    gap_right: float,
    ctx: _RoutingCtx,
) -> None:
    """Translate movable bundles clear of counter-running fixed channels.

    Exempt handlers own their channel geometry, so their declared gap legs are
    immutable obstacles. Movable bundles retain their internal line spacing and
    are translated by the smallest feasible amount that gives every
    counter-running channel the bundle clearance. Bundles settled earlier in
    the gap become obstacles for later bundles, which also separates two
    movable counter-running streams.
    """
    settled = list(fixed)
    for down, chans in bundles:
        if not chans:
            continue
        join_candidates = [
            sibling.x - ch.x
            for ch in chans
            for sibling in settled
            if _channels_form_symmetric_divergence(ch, sibling)
        ]
        join_delta = None
        if join_candidates and all(
            abs(candidate - join_candidates[0]) <= COORD_TOLERANCE
            for candidate in join_candidates[1:]
        ):
            join_delta = join_candidates[0]
        obstacles = [
            obstacle
            for obstacle in settled
            if any(
                _required_channel_clearance(ch, obstacle, ctx.curve_radius) > 0
                for ch in chans
            )
        ]
        if not obstacles and join_delta is None:
            settled.extend(chans)
            continue

        candidates = {0.0}
        for ch in chans:
            for obstacle in obstacles:
                clearance = _required_channel_clearance(ch, obstacle, ctx.curve_radius)
                if clearance <= 0:
                    continue
                candidates.add(obstacle.x - clearance - ch.x)
                candidates.add(obstacle.x + clearance - ch.x)

        def feasible(delta: float) -> bool:
            targets = [(ch, ch.x + delta) for ch in chans]
            usable_left = gap_left + EDGE_TO_BUNDLE_CLEARANCE
            usable_right = gap_right - EDGE_TO_BUNDLE_CLEARANCE
            if any(
                x < usable_left - COORD_TOLERANCE
                or x > usable_right + COORD_TOLERANCE
                or _section_intrudes(ctx.graph, x, ch.y_lo, ch.y_hi)
                for ch, x in targets
            ):
                return False
            if any(
                _overlays_distinct_line(ch, x, obstacle, ctx.offset_step)
                for ch, x in targets
                for obstacle in settled
            ):
                return False
            return all(
                (
                    clearance := _required_channel_clearance(
                        ch, obstacle, ctx.curve_radius
                    )
                )
                <= 0
                or abs(x - obstacle.x) >= clearance - COORD_TOLERANCE
                for ch, x in targets
                for obstacle in obstacles
            )

        delta = join_delta if join_delta is not None and feasible(join_delta) else None
        if delta is None:
            delta = next(
                (
                    candidate
                    for candidate in sorted(candidates, key=lambda d: (abs(d), d))
                    if feasible(candidate)
                ),
                None,
            )
        if delta is None or abs(delta) <= COORD_TOLERANCE:
            settled.extend(chans)
            continue

        order = _convergence_line_order(chans, ctx.graph) or _distinct_line_order(chans)
        rank = {line_id: i for i, line_id in enumerate(order)}
        for ch in chans:
            new_x = ch.x + delta
            _restack_channel(
                ch,
                new_x,
                rank[ch.route.line_id],
                len(order),
                ctx.offset_step,
                ctx.curve_radius,
            )
            ch.x = new_x
        settled.extend(chans)


def _locate_slot_channel(
    rp: RoutedPath, slot: GapSlot, graph: MetroGraph
) -> _VChannel | None:
    """Find the vertical leg on *rp* that *slot* describes, or ``None``.

    The leg is the route segment running ``slot.direction`` whose x sits inside
    the gap ``slot`` names; a handler declares at most one slot per physical
    leg, so direction plus gap membership identifies it uniquely.  Returns
    ``None`` when no segment matches (the leg was nudged out of the gap, so the
    materialization leaves it where the handler placed it).
    """
    left, right = column_gap_edges(
        graph, slot.gap_lo_col, slot.gap_hi_col, row=slot.row
    )
    if right <= left:
        return None
    down = slot.direction is Direction.D
    for k, x, y_lo, y_hi, seg_down in iter_vertical_segments(rp):
        if seg_down is down and left - COORD_TOLERANCE <= x <= right + COORD_TOLERANCE:
            return _VChannel(route=rp, idx=k, x=x, y_lo=y_lo, y_hi=y_hi, down=down)
    return None


def _planner_owns_channel(channel: _VChannel) -> bool:
    """Whether a pre-routing plan owns this channel's final geometry."""
    return planner_owns_segment(channel.route, channel.idx)


def _fused_sibling_spans(
    routes: list[RoutedPath], chans: list[_VChannel]
) -> list[tuple[float, float]]:
    """Vertical spans of descents that will fuse onto this gap's channels.

    :func:`_coincide_same_line_tracks` later snaps every same-source, same-line
    opening descent onto one shared channel -- including a deep wrap the
    materialization pass never sees, because its handler owns it
    (``normalize_exempt``).  A shallow gap bundle centred for its own extent can
    then be fused-onto by such a sibling whose descent runs on through a section
    below, plotting the shared stroke over it.  Report those siblings' spans so
    the gap can be narrowed against the rows they cross too.
    """
    keys = {(c.route.edge.source, c.route.line_id, c.down) for c in chans}
    have = {id(c.route) for c in chans}
    spans: list[tuple[float, float]] = []
    for rp in routes:
        if id(rp) in have or not rp.is_inter_section:
            continue
        ch = _initial_fanout_descent(rp)
        if ch is None:
            continue
        if (rp.edge.source, rp.line_id, ch.down) in keys:
            spans.append((ch.y_lo, ch.y_hi))
    return spans


def _materialize_gap_slots(routes: list[RoutedPath], ctx: _RoutingCtx) -> None:
    """Resolve every declared :class:`GapSlot` to a concentric channel X.

    Handlers annotate each vertical inter-section leg with the gap it occupies
    (:meth:`RoutedPath.declare_gap_slot`); this pass groups the legs by that
    declared ``(gap, row)`` and lays each gap out under the uniform contract:

    * All same-direction channels sharing one inter-column gap collapse into
      ONE concentric bundle, ``OFFSET_STEP`` apart, centred.
    * A downward bundle and an upward bundle sharing a gap are held
      ``BUNDLE_TO_BUNDLE_CLEARANCE`` (B) apart, centred as a group.
    * A lone bundle centres in its gap with at least
      ``EDGE_TO_BUNDLE_CLEARANCE`` (A) from each bounding section edge.
    * A bundle carrying a line whose column here is owned by an exempt handler
      seats on that column instead of centring
      (:func:`_anchored_bundle_midpoint`), so the coincidence fusion that later
      pulls this bundle's leg onto it does not drag it across a sibling.

    The grouping is read from the declared slots rather than rediscovered from
    raw geometry; the concentric layout and flanking-radius recompute are the
    same per-gap logic a single handler cannot do alone (it needs every leg in
    the gap at once).
    """
    graph = ctx.graph
    by_gap: dict[tuple[int, int | None], list[_VChannel]] = defaultdict(list)
    # Columns this pass cannot move: an exempt handler owns its own channel, but
    # it declared the gap it sits in, so a bundle carrying the same line here can
    # be seated on it instead of discovering the clash after the fusion.
    owned: dict[tuple[int, int | None], dict[tuple[bool, str], float]] = defaultdict(
        dict
    )
    for rp in routes:
        for slot in rp.gap_slots:
            ch = _locate_slot_channel(rp, slot, graph)
            if ch is None:
                continue
            if rp.normalize_exempt or _planner_owns_channel(ch):
                owned[(slot.gap_lo_col, slot.row)][(ch.down, rp.line_id)] = ch.x
            else:
                by_gap[(slot.gap_lo_col, slot.row)].append(ch)

    bands = _grid_row_bands(graph)
    for (lo, row), chans in by_gap.items():
        gap_left, gap_right = column_gap_edges(graph, lo, lo + 1, row=row)
        if gap_right <= gap_left:
            continue
        # A channel crossing several rows must clear sections in ALL of them, so
        # narrow the gap to the intersection of every crossed row's edges -- else
        # a leg climbing through a row whose section edge sits further out than a
        # sibling row's would centre in the wider gap and step back behind its
        # source edge.  The spans include any deeper same-source, same-line
        # descent that the coincidence pass will fuse onto this channel, so the
        # shared stroke clears the rows that sibling crosses as well.
        crossed_spans = [(c.y_lo, c.y_hi) for c in chans]
        crossed_spans += _fused_sibling_spans(routes, chans)
        for r, band in bands.items():
            if not any(
                y_lo < band[1] and band[0] < y_hi for y_lo, y_hi in crossed_spans
            ):
                continue
            r_left, r_right = column_gap_edges(graph, lo, lo + 1, row=r)
            if r_right > r_left:
                gap_left = max(gap_left, r_left)
                gap_right = min(gap_right, r_right)
        bundles: list[tuple[bool, list[_VChannel]]] = []
        for down in (True, False):
            same = [c for c in chans if c.down is down]
            for corridor in _split_corridors(same):
                bundles.append((down, corridor))
        _layout_gap_bundle(bundles, gap_left, gap_right, ctx, owned.get((lo, row)))


def _separate_declared_opposing_gap_bundles(
    routes: list[RoutedPath], ctx: _RoutingCtx
) -> None:
    """Separate settled counter-running gap bundles around exempt obstacles."""
    graph = ctx.graph
    movable: dict[tuple[int, int | None], list[_VChannel]] = defaultdict(list)
    fixed: dict[tuple[int, int | None], list[_VChannel]] = defaultdict(list)
    seen: set[tuple[tuple[int, int | None], int, int]] = set()
    for rp in routes:
        for slot in rp.gap_slots:
            key = (slot.gap_lo_col, slot.row)
            ch = _locate_slot_channel(rp, slot, graph)
            if ch is None or (key, id(rp), ch.idx) in seen:
                continue
            seen.add((key, id(rp), ch.idx))
            (fixed if rp.normalize_exempt or _planner_owns_channel(ch) else movable)[
                key
            ].append(ch)

    bands = _grid_row_bands(graph)
    for (lo, row), chans in movable.items():
        gap_left, gap_right = column_gap_edges(graph, lo, lo + 1, row=row)
        if gap_right <= gap_left:
            continue
        crossed_spans = [(c.y_lo, c.y_hi) for c in chans]
        crossed_spans += [(c.y_lo, c.y_hi) for c in fixed.get((lo, row), [])]
        for r, band in bands.items():
            if not any(
                y_lo < band[1] and band[0] < y_hi for y_lo, y_hi in crossed_spans
            ):
                continue
            r_left, r_right = column_gap_edges(graph, lo, lo + 1, row=r)
            if r_right > r_left:
                gap_left = max(gap_left, r_left)
                gap_right = min(gap_right, r_right)
        bundles = [
            (down, corridor)
            for down in (True, False)
            for corridor in _split_corridors([c for c in chans if c.down is down])
        ]
        _anchor_same_direction_fixed_channels(bundles, fixed.get((lo, row), []), ctx)
        _separate_opposing_gap_bundles(
            bundles,
            fixed.get((lo, row), []),
            gap_left,
            gap_right,
            ctx,
        )


@dataclass
class _HTrunk:
    """One horizontal bypass-trunk segment of an inter-section route.

    The trunk is the interior horizontal leg of a U-shaped bypass
    (``points[k] -> points[k+1]``), flanked by a vertical descent on each
    side.  ``y`` is its current channel Y, ``x_lo``/``x_hi`` its X span,
    and ``dips_down`` records whether the U dips below its flanking legs
    (the common case: source/target sit above the trunk).
    """

    route: RoutedPath
    idx: int
    y: float
    x_lo: float
    x_hi: float
    dips_down: bool
    sign_x: int  # traversal direction along the trunk: +1 left->right, -1 right->left


def _collect_htrunks(
    routes: list[RoutedPath], *, include_exempt: bool = False
) -> list[_HTrunk]:
    """Find every horizontal bypass-trunk segment in inter-section routes.

    A trunk is an interior horizontal segment (not the first or last leg)
    whose two flanking neighbours are both vertical, i.e. the bottom (or
    top) leg of a U-shaped :func:`_route_bypass` route.

    With *include_exempt*, ``normalize_exempt`` routes are collected too;
    callers use these as read-only obstacles (their geometry is owned by
    their own handler and must not be restacked).
    """
    out: list[_HTrunk] = []
    for rp in routes:
        if not rp.is_inter_section:
            continue
        if rp.normalize_exempt and not include_exempt:
            continue
        for k, seg in iter_horizontal_trunks(rp):
            out.append(
                _HTrunk(
                    route=rp,
                    idx=k,
                    y=seg.y,
                    x_lo=seg.x_lo,
                    x_hi=seg.x_hi,
                    dips_down=seg.before_y < seg.y - COORD_TOLERANCE,
                    sign_x=1 if seg.xb > seg.xa else -1,
                )
            )
    return out


def _bundle_same_destination_tails(routes: list[RoutedPath], ctx: _RoutingCtx) -> None:
    """Seat eligible same-port destination tails on one eager concentric band."""
    for _bundle, trunks, targets in iter_eligible_destination_tail_bundles(
        routes, ctx.graph, ctx.offset_step, ctx.curve_radius
    ):
        for line_id, trunk in trunks.items():
            if route_system_owns_segment_boundary(trunk.route, trunk.idx):
                continue
            target_y = targets[line_id]
            if abs(trunk.y - target_y) <= COORD_TOLERANCE:
                continue
            _set_htrunk_y(
                trunk.route,
                trunk.idx,
                target_y,
                offset_in=0.0,
                offset_out=0.0,
            )


def _declared_htrunks(routes: list[RoutedPath]) -> list[_HTrunk]:
    """Every horizontal bypass trunk whose handler declared a :class:`TrunkSlot`.

    The trunks the materialization pass owns: exempt and non-exempt alike,
    filtered to those carrying a declared slot so an undeclared leg (which would
    have no gap to fan into) is left to :func:`_dogleg_off_exempt_trunks`.
    """
    return [
        t
        for t in _collect_htrunks(routes, include_exempt=True)
        if t.route.trunk_slot is not None
        and not route_system_owns_segment_boundary(t.route, t.idx)
    ]


def _group_channel_trunks(trunks: list[_HTrunk], step: float) -> list[list[_HTrunk]]:
    """Group horizontal bypass trunks that visually share one channel.

    Trunks belong together when they share a dip direction and transitively
    overlap in X within one channel.  Channel membership is decided two ways:

    - When both trunks declare the SAME inter-row gap (the ``gap_upper_row`` on
      their :class:`TrunkSlot`), they share that channel however far apart their
      current Ys sit.  Several bypass routes that dip into one inter-row gap are
      one visual channel even when their per-bundle ``nest_offset`` left them a
      smear of distinct Ys, so they must fan into a single tight ``OFFSET_STEP``
      bundle rather than separate loose groups.
    - For a deep cross-row dive declaring no gap (``gap_upper_row is None``)
      membership falls back to proximity to the NEAREST current member: such
      trunks arrive pre-stacked by their per-bundle ``nest_offset``, so a trunk
      one ``step`` deeper than the group's current deepest member still belongs.
      A genuinely separate channel a full row away (Ys far outside the chain)
      then starts its own group.

    The shared X-overlap requirement keeps distinct corridors in the same gap
    band - different X regions that never overlap - in separate groups.
    """
    band = max(step, COORD_TOLERANCE)

    def _same_channel(o: _HTrunk, t: _HTrunk) -> bool:
        go, gt = o.route.trunk_slot, t.route.trunk_slot
        if (
            go is not None
            and gt is not None
            and go.gap_upper_row is not None
            and go.gap_upper_row == gt.gap_upper_row
        ):
            return True
        return abs(o.y - t.y) <= band

    groups: list[list[_HTrunk]] = []
    for t in sorted(trunks, key=lambda t: (t.dips_down, t.y, t.x_lo)):
        placed = False
        for grp in groups:
            if grp[0].dips_down != t.dips_down:
                continue
            if not any(_same_channel(o, t) for o in grp):
                continue
            if any(t.x_lo < o.x_hi and o.x_lo < t.x_hi for o in grp):
                grp.append(t)
                placed = True
                break
        if not placed:
            groups.append([t])
    return groups


def _final_port_approach(rp: RoutedPath) -> _VChannel | None:
    """The final vertical descent into a port, when the tail ends on a vertical.

    A converging port approach usually ends ``... (vx, y) -> (vx, ey) ->
    (ex, ey)``: a vertical leg into the entry Y, then a short horizontal lead
    into the port (``idx`` points at ``points[-3]``).  When the feeder is
    aligned on the port's own X it lands as a bare vertical drop with no
    horizontal lead -- ``... (vx, y) -> (vx, ey)`` -- and the final segment
    itself is the descent (``idx`` points at ``points[-2]``).  Returns the
    ``_VChannel`` for that vertical, or ``None`` when the tail does not end on
    one.
    """
    pts = rp.points
    if len(pts) < 2:
        return None
    x1, y1 = pts[-1]
    x2, y2 = pts[-2]
    if abs(x2 - x1) <= COORD_TOLERANCE and abs(y2 - y1) > COORD_TOLERANCE:
        return _VChannel(
            route=rp,
            idx=len(pts) - 2,
            x=x1,
            y_lo=min(y1, y2),
            y_hi=max(y1, y2),
            down=y1 > y2,
        )
    if len(pts) < 3:
        return None
    x3, y3 = pts[-3]
    if abs(y2 - y1) > COORD_TOLERANCE or abs(x2 - x1) <= COORD_TOLERANCE:
        return None  # last segment is not a horizontal lead
    if abs(x3 - x2) > COORD_TOLERANCE or abs(y3 - y2) <= COORD_TOLERANCE:
        return None  # second-to-last segment is not a vertical descent
    return _VChannel(
        route=rp,
        idx=len(pts) - 3,
        x=x2,
        y_lo=min(y2, y3),
        y_hi=max(y2, y3),
        down=y2 > y3,
    )


class _Coincidence(NamedTuple):
    """A set of same-line vertical legs to fuse, and the X they share."""

    channels: list[_VChannel]
    ref_x: float


def _reconcile_moved_gap_slot(ch: _VChannel, new_x: float, graph: MetroGraph) -> None:
    """Retarget a fused leg's :class:`GapSlot` to the gap it lands in.

    A coincidence fusion snaps a vertical leg onto a shared reference X that can
    lie in a different inter-column gap than the one the handler declared.  The
    declaration is spent for placement (:func:`_materialize_gap_slots` runs
    earlier), but the render backstop :func:`check_gap_channels_materialized`
    reads it: a leg whose declared gap column disagrees with the gap it occupies
    reads as an undeclared channel and aborts the render.  Point the leg's slot
    at the gap the fused X falls in so the declaration matches the geometry.  A
    fused X outside every gap needs no action -- the backstop only checks in-gap
    legs, so a stale slot describing no leg is inert.  Coordinates are untouched;
    only the symbolic declaration moves.
    """
    new = gap_lo_for_x(graph, new_x, ch.y_lo, ch.y_hi)
    old = gap_lo_for_x(graph, ch.x, ch.y_lo, ch.y_hi)
    if new is None or new == old:
        return
    down = ch.down
    new_lo, new_row = new
    ch.route.gap_slots = [
        s
        for s in ch.route.gap_slots
        if not (
            old is not None
            and s.gap_lo_col == old[0]
            and (s.direction is Direction.D) == down
        )
    ]
    ch.route.declare_gap_slot(
        lo_col=new_lo,
        hi_col=new_lo + 1,
        row=new_row,
        direction=Direction.D if down else Direction.U,
        slot_index=0,
        n_slots=1,
    )


def _snap_group(
    group: _Coincidence,
    graph: MetroGraph,
    *,
    validate_planned_axes: bool = True,
) -> None:
    """Snap every channel in a coincidence group onto its shared reference X."""
    planned = [channel for channel in group.channels if _planner_owns_channel(channel)]
    ref_x = planned[0].x if planned else group.ref_x
    if any(abs(channel.x - ref_x) > COORD_TOLERANCE for channel in planned[1:]):
        if validate_planned_axes:
            raise ValueError("one coincidence group contains conflicting planned axes")
        return
    for ch in group.channels:
        if _planner_owns_channel(ch):
            continue
        if abs(ch.x - ref_x) > COORD_TOLERANCE:
            _reconcile_moved_gap_slot(ch, ref_x, graph)
            _set_vchannel_x(ch, ref_x)


def _snap_merge_feeder_group(group: _Coincidence, graph: MetroGraph) -> None:
    """Snap merge-feeder branches onto the trunk descent, carrying each tail.

    A feeder routed as a merge branch (:func:`_route_merge_branch`) opens with a
    descent and turns onto a short tail that overlaps the trunk's converging run
    toward the entry port.  Snapping the descent onto the trunk's shared column
    moves the descent leg but leaves the tail's far end at its routing-time X, so
    a descent that travels far to reach the trunk drags the tail's terminus past
    the trunk's own turn as a dead stub.  Translate the tail by the same shift
    the descent took, preserving its intended short overlap so it terminates on
    the trunk rather than overshooting it.
    """
    ref_x = group.ref_x
    for ch in group.channels:
        _seat_merge_feeder_opening(ch.route, ref_x, graph)


def _seat_merge_feeder_opening(
    route: RoutedPath,
    coordinate: float,
    graph: MetroGraph,
    *,
    planned: bool = False,
    carry_tail: bool = True,
) -> None:
    """Seat a merge feeder's opening turn on its planned shared axis.

    ``carry_tail`` translates everything past the opening with it, which is what
    keeps a route whose whole run hangs off that turn intact.  A caller whose
    plan states the rest of the run independently passes ``False``: carrying the
    tail there would slide geometry the plan has already positioned.
    """
    channel = (
        _opening_fanout_descent(route) if planned else _initial_fanout_descent(route)
    )
    if channel is None:
        return
    delta = coordinate - channel.x
    if abs(delta) <= COORD_TOLERANCE:
        return
    tail_start = channel.idx + 2
    _reconcile_moved_gap_slot(channel, coordinate, graph)
    _set_vchannel_x(channel, coordinate)
    if not carry_tail:
        return
    route.points = [
        (x + delta, y) if rank >= tail_start else (x, y)
        for rank, (x, y) in enumerate(route.points)
    ]


def _route_first_vertical(rp: RoutedPath) -> _VChannel | None:
    """The first vertical leg of a route, whichever way it turns, or ``None``.

    Direction-agnostic (unlike :func:`_initial_fanout_descent`, which requires a
    horizontal lead into a downward descent): a fan-out arm that turns up off its
    lead-out is as much an opening pivot as one that turns down.  ``None`` when
    the route runs straight into its target with no vertical turn.
    """
    return next(
        (
            _VChannel(route=rp, idx=k, x=x, y_lo=y_lo, y_hi=y_hi, down=down)
            for k, x, y_lo, y_hi, down in iter_vertical_segments(rp)
        ),
        None,
    )


def _merge_fanout_pivot_spans(
    rp: RoutedPath, fanouts: set[str], merges: set[str]
) -> Iterable[tuple[tuple[str, bool], _VChannel]]:
    """A merge-fanout branch's opening pivot, keyed by source and turn direction.

    Yields nothing for a route that is not a merge-fanout branch into a merge
    junction, or that opens with no vertical turn.
    """
    if rp.edge.source not in fanouts or rp.edge.target not in merges:
        return
    ch = _route_first_vertical(rp)
    if ch is not None:
        yield (rp.edge.source, ch.down), ch


def _coincide_merge_fanout_pivots(routes: list[RoutedPath], ctx: _RoutingCtx) -> None:
    """Fuse a merge fan-out's branch first corners onto one shared pivot column.

    A merge fan-out's branches leave one source together and each turns off its
    lead-out through a first corner (:func:`merge_fanout_junctions`).  Handlers
    place those corners independently, so same-direction branches can open a few
    pixels apart -- the lead-out reads as forking into two columns.  Snap them
    onto the corner nearest the source so the fork pivots through one column and
    splits only where each branch turns off.

    Branches are grouped by turn direction: an up-turning arm and a down-turning
    arm of one fork never share a column (one shared column would fold the line
    back over itself), so only same-direction arms fuse.  Only branches INTO
    merge junctions are considered; a co-travelling branch to another target
    keeps its own handler channel.
    """
    fanouts = ctx.merge_fanouts
    if not fanouts:
        return
    merges = ctx.merge.junctions
    groups = _group_channels_by(
        routes, lambda rp: _merge_fanout_pivot_spans(rp, fanouts, merges)
    )
    for (src, _down), chans in groups.items():
        planned = [channel.x for channel in chans if _planner_owns_channel(channel)]
        if planned and max(planned) - min(planned) > COORD_TOLERANCE:
            continue
        source_x = ctx.graph.stations[src].x
        ref = merge_fanout_pivot_reference(
            [c.x for c in chans], source_x, COORD_TOLERANCE
        )
        if ref is not None:
            _snap_group(
                _Coincidence(chans, ref),
                ctx.graph,
                validate_planned_axes=ctx.validate_final_route_frames,
            )


def _coincide_fanout_opening_descents(
    routes: list[RoutedPath], ctx: _RoutingCtx
) -> None:
    """Own the opening-descent column of every fan-out, one owner for both intents.

    A junction that fans branches out through different handlers leaves each
    branch to open its own vertical descent, which :func:`_materialize_gap_slots`
    then re-stacks per gap -- so branches of one fan open a few pixels apart and
    read as separate strokes off the source.  This is the single pass that settles
    a fan's opening-descent column: it fuses each line's same-source descents onto
    the track nearest the source (:func:`_divergent_source_groups`), then nests the
    distinct lines one ``OFFSET_STEP`` apart until each turns off
    (:func:`_bundle_divergent_distinct_descents`).

    It runs *after* :func:`_coincide_same_line_tracks` so convergence has already
    settled the perpendicular drops: a branch that peels straight down the
    junction's own column into a TOP/BOTTOM port (rounded later by
    :func:`_round_junction_perp_peeloff`) lands on the junction column during
    convergence, presenting here as a bare vertical drop rather than a
    horizontal-then-vertical opening, so it stays clear of an L-shaped sibling
    that genuinely diverges to another column.
    """
    for group in _divergent_source_groups(routes):
        planned = [
            channel.x for channel in group.channels if _planner_owns_channel(channel)
        ]
        if planned and max(planned) - min(planned) > COORD_TOLERANCE:
            continue
        _snap_group(
            group,
            ctx.graph,
            validate_planned_axes=ctx.validate_final_route_frames,
        )
    _bundle_divergent_distinct_descents(routes, ctx)


def _coincide_same_line_tracks(routes: list[RoutedPath], ctx: _RoutingCtx) -> None:
    """Fuse same-line vertical legs that should read as a single stroke.

    Handlers route each edge independently, so one metro line carried by
    several routes that share a source, an entry port, or a merge can descend
    as several near-parallel same-colour tracks a few pixels apart -- redundant
    duplicate strokes of one line.  Each such group should read as ONE track
    that splits only where the routes genuinely diverge.

    Four kinds of same-line track contribute. Three fuse near-parallel
    VERTICAL legs onto a shared reference X:

    * convergent -- final descents into one entry port;
    * divergent -- opening descents leaving one source;
    * merge feeders -- a merge's same-column feeders, onto the trunk's descent.

    They are fused in that order so a route touched by more than one kind (a
    short merge feeder whose opening descent is also its final approach) settles
    on the last group's reference X; each member snaps onto its group's X,
    resetting its flanking corners since a single track has no concentric
    nesting.

    The fourth, :func:`_join_fanout_upstream_tails`, closes the HORIZONTAL
    handoff seam at a fan-out junction: it extends the upstream tail so it
    meets the paired downstream route's start. It runs last because the
    downstream start X it reads is the materialised value the earlier passes
    (and the vertical fusions above) leave behind, not a routing-time value
    the handler could have anticipated.
    """
    for group in _convergent_port_groups(routes, ctx):
        _snap_group(
            group,
            ctx.graph,
            validate_planned_axes=ctx.validate_final_route_frames,
        )
    for group in _merge_feeder_groups(routes, ctx):
        compatibility_channels = [
            channel
            for channel in group.channels
            if channel.route.route_system_disposition == "compatibility"
        ]
        if compatibility_channels:
            _snap_merge_feeder_group(
                _Coincidence(compatibility_channels, group.ref_x), ctx.graph
            )
    _join_fanout_upstream_tails(routes, ctx)


def _clear_merge_trunk_opposite_arm(routes: list[RoutedPath], ctx: _RoutingCtx) -> None:
    """Slide a merge down-trunk clear of an opposite up-arm it folds onto.

    A fork can send one line down a merge trunk and the same line up to a second
    merge.  When the trunk's shared descent column and the up-arm's ascent column
    settle within a curve radius while overlapping in Y, the two opposite-going
    legs of one line draw as a fold-back (:func:`_guard_no_opposing_line_overlap`
    forbids it).  The down-trunk already clears the fork's exit by a curve radius
    of runway, so slide its whole descent column -- the trunk and every feeder
    fused onto it -- clear past the up-arm (a curve radius plus one offset step,
    so the two columns read as distinctly separate rather than a doubled corner),
    re-forming each corner through :func:`_set_vchannel_x`.  Reads the settled
    columns and fires only on the actual overlap, so well-separated opposite arms
    are left untouched.
    """
    merge = ctx.merge
    if not merge.trunk_source or not ctx.merge_fanouts:
        return
    radius = ctx.curve_radius
    clearance = radius + ctx.offset_step
    arms = [
        ch
        for rp in routes
        if rp.is_inter_section
        and rp.edge.source in ctx.merge_fanouts
        and rp.edge.target in merge.junctions
        and (ch := _route_first_vertical(rp)) is not None
        and not _planner_owns_channel(ch)
    ]
    downs = [ch for ch in arms if ch.down]
    ups = [ch for ch in arms if not ch.down]
    if not downs or not ups:
        return
    moved: set[float] = set()
    for descent in downs:
        col = round(descent.x, 1)
        if col in moved:
            continue
        target_x: float | None = None
        for up in ups:
            if up.route.line_id != descent.route.line_id:
                continue
            if abs(up.x - descent.x) > radius:
                continue
            overlap = min(up.y_hi, descent.y_hi) - max(up.y_lo, descent.y_lo)
            if overlap <= COORD_TOLERANCE:
                continue
            cand = max(descent.x, up.x) + clearance
            target_x = cand if target_x is None else max(target_x, cand)
        if target_x is None:
            continue
        moved.add(col)
        for ch in downs:
            if abs(ch.x - descent.x) <= COORD_TOLERANCE:
                _set_vchannel_x(ch, target_x)


class _TraverseLeg(NamedTuple):
    """One same-line interior horizontal trunk considered for band fusion."""

    route: RoutedPath
    idx: int
    seg: HTrunkSeg


def _fanout_traverse_spans(
    rp: RoutedPath,
) -> Iterable[tuple[tuple[str, bool], _TraverseLeg]]:
    """The horizontal leg a fan-out route turns onto after its opening descent.

    A branch that leaves a source ``H`` then ``V`` and then turns to run along a
    corridor has that corridor leg at ``descent.idx + 1`` -- an interior trunk
    flanked by the descent and the onward riser.  Key it by
    ``(source, descent-direction)`` so a fan's same-direction traverses nest
    together, the same grouping :func:`_bundle_divergent_distinct_descents` uses.
    """
    desc = _initial_fanout_descent(rp)
    if desc is None:
        return
    for k, seg in iter_horizontal_trunks(rp):
        if k == desc.idx + 1:
            yield (rp.edge.source, desc.down), _TraverseLeg(rp, k, seg)
            break


def _fanout_traverse_legs(
    routes: list[RoutedPath],
) -> defaultdict[tuple[str, bool], list[_TraverseLeg]]:
    """Fan-out traverse legs bucketed by source and descent direction."""
    return _group_channels_by(routes, _fanout_traverse_spans)


def _coincide_same_line_fanout_traverses(
    routes: list[RoutedPath], ctx: _RoutingCtx
) -> None:
    """Fuse same-line fan traverses that turn onto one riser column."""
    for members in _fanout_traverse_legs(routes).values():
        groups: list[list[_TraverseLeg]] = []
        for member in members:
            group = next(
                (
                    group
                    for group in groups
                    if group[0].route.line_id == member.route.line_id
                    and abs(group[0].seg.xb - member.seg.xb) <= COORD_TOLERANCE
                ),
                None,
            )
            if group is None:
                groups.append([member])
            else:
                group.append(member)

        for group in groups:
            if len(group) < 2:
                continue
            bands = tuple(
                band
                for member in group
                if (
                    band := ctx.reserved_bands.for_segment(
                        member.route.edge.source,
                        member.route.edge.target,
                        member.route.line_id,
                        member.idx,
                    )
                )
                is not None
            )
            lower = max((band.lo for band in bands), default=float("-inf"))
            upper = min((band.hi for band in bands), default=float("inf"))
            if lower > upper + COORD_TOLERANCE_FINE:
                raise RuntimeError(
                    "same-line fan traverses have disjoint reserved bands"
                )
            target = min(member.seg.y for member in group)
            target = min(max(target, lower), upper)
            exempt = {
                section
                for member in group
                if (section := ctx.graph.section_for_station(member.route.edge.target))
                is not None
            }
            if any(
                _h_segment_crosses_other_section(
                    ctx.graph,
                    member.seg.xa,
                    member.seg.xb,
                    target,
                    exempt,
                )
                for member in group
            ):
                continue
            for member in group:
                if abs(member.seg.y - target) > COORD_TOLERANCE_FINE:
                    _set_htrunk_y(member.route, member.idx, target)


def _clear_compatibility_entry_wrap_leadouts(
    routes: list[RoutedPath], ctx: _RoutingCtx
) -> None:
    """Seat wrap openings beyond same-line descents regardless of emit order."""
    for route in routes:
        if len(route.points) != 6 or not route.is_inter_section:
            continue
        p0, p1, p2, p3 = route.points[:4]
        if (
            abs(p0[1] - p1[1]) > COORD_TOLERANCE
            or abs(p1[0] - p2[0]) > COORD_TOLERANCE
            or abs(p2[1] - p3[1]) > COORD_TOLERANCE
        ):
            continue
        target = ctx.graph.ports.get(route.edge.target)
        source = ctx.graph.stations.get(route.edge.source)
        if (
            target is None
            or not target.is_entry
            or target.side is not PortSide.RIGHT
            or source is None
            or source.section_id is None
            or target.section_id is None
        ):
            continue
        source_section = ctx.graph.sections[source.section_id]
        target_section = ctx.graph.sections[target.section_id]
        if source_section.grid_row == target_section.grid_row:
            continue
        source_col = source_section.grid_col
        if source_col is None:
            continue
        _gap_left, gap_right = column_gap_edges(
            ctx.graph,
            source_col,
            source_col + 1,
            row=target_section.grid_row,
        )
        opening = _VChannel(
            route,
            1,
            p1[0],
            min(p1[1], p2[1]),
            max(p1[1], p2[1]),
            p2[1] > p1[1],
        )
        if route_system_owns_segment_boundary(route, opening.idx):
            continue
        if (
            ctx.reserved_bands.for_segment(
                route.edge.source,
                route.edge.target,
                route.line_id,
                opening.idx,
            )
            is not None
        ):
            continue
        sibling_xs = [
            x
            for sibling in routes
            if sibling is not route
            and sibling.is_inter_section
            and sibling.line_id == route.line_id
            and sibling.edge.source != route.edge.source
            for _idx, axis, x, start, end, _turning in _iter_axis_aligned_legs(sibling)
            if axis == 0
            for y_lo, y_hi in [(min(start, end), max(start, end))]
            if opening.x - COORD_TOLERANCE <= x <= gap_right + COORD_TOLERANCE
            and min(opening.y_hi, y_hi) - max(opening.y_lo, y_lo) > COORD_TOLERANCE
        ]
        if not sibling_xs:
            continue
        new_x = gap_right - ctx.curve_radius - ctx.offset_step
        if new_x <= max(sibling_xs) + ctx.curve_radius + COORD_TOLERANCE:
            continue
        excluded = frozenset((source.section_id, target.section_id))
        if _section_intrudes(
            ctx.graph,
            new_x,
            opening.y_lo,
            opening.y_hi,
            exclude=excluded,
        ):
            continue
        _set_vchannel_x(opening, new_x, base_radius=ctx.curve_radius)


def _bundle_divergent_distinct_traverses(
    routes: list[RoutedPath], ctx: _RoutingCtx
) -> None:
    """Nest distinct-line fan-out traverses one step apart until they fork.

    The horizontal counterpart of :func:`_bundle_divergent_distinct_descents`.
    Even once a fan's opening descents nest one ``OFFSET_STEP`` apart, the corridor
    each line turns onto may sit on an independently-sized band several px from its
    siblings, so the shared run reads as separate strokes rather than one bundle.
    Nest the near-parallel traverses one step apart, ordered so a line's
    traverse-Y rank matches its descent-X rank through the shared corner (a
    left-turning bundle puts its leftmost descent on top, a right-turning one
    mirrors it), holding a constant bundle width until each line turns off.
    """
    step = ctx.offset_step
    for members in _fanout_traverse_legs(routes).values():
        if any(
            convergence_owns_segment_boundary(member.route, member.idx)
            for member in members
        ):
            continue
        by_line: dict[str, list[_TraverseLeg]] = defaultdict(list)
        for m in members:
            by_line[m.route.line_id].append(m)
        if len(by_line) < 2:
            continue
        # One representative band per line (same-line traverses are already fused).
        rep = {lid: ms[0].seg for lid, ms in by_line.items()}
        ys = [s.y for s in rep.values()]
        if max(ys) - min(ys) <= step * (len(by_line) - 1) + COORD_TOLERANCE:
            continue
        # Every member of a fan turns the same way; skip a mixed group rather than
        # nest legs whose corners face opposite directions.
        turns = {s.xb < s.xa for s in rep.values()}
        if len(turns) != 1:
            continue
        left_turn = next(iter(turns))
        ordered = sorted(
            rep, key=lambda lid: rep[lid].xa if left_turn else -rep[lid].xa
        )
        base = min(ys)
        targets = {lid: base + i * step for i, lid in enumerate(ordered)}
        rank_off = {lid: i * step for i, lid in enumerate(ordered)}
        moves = [
            (m, targets[m.route.line_id], rank_off[m.route.line_id])
            for m in members
            if abs(m.seg.y - targets[m.route.line_id]) > COORD_TOLERANCE
        ]
        # Never re-band a traverse across a foreign section; the fan's own targets
        # are exempt (each traverse legitimately ends at one).
        exempt = {
            sec
            for m in members
            if (sec := ctx.graph.section_for_station(m.route.edge.target)) is not None
        }
        if any(
            _h_segment_crosses_other_section(ctx.graph, m.seg.xa, m.seg.xb, ty, exempt)
            for m, ty, _off in moves
        ):
            continue
        # The band nests one step per rank off the innermost, so its incoming
        # corner off the shared descent sizes concentrically; the outgoing corner
        # is where the line peels off alone, so it keeps the base radius.
        for m, ty, off in moves:
            _set_htrunk_y(m.route, m.idx, ty, off, 0.0)


def _drop_covered_merge_entry_hops(
    routes: list[RoutedPath],
    ctx: _RoutingCtx,
) -> tuple[tuple[tuple[str, str, str], tuple[str, str, str]], ...]:
    """Drop a compatibility merge -> entry hop covered by its feeders.

    The merge station is placed at ``max(feeder.x) + margin``, which is not the
    column the feeders' channels finally converge in, so the hop drawn from it
    starts short of their shared corner and overhangs it.  The hop earns its
    place only while some feeder stops at the merge station rather than carrying
    on to the port; once they all reach the port, the overhang is the only part
    of the hop that is not already drawn.

    A feeder that stops at the merge station is the evidence that keeps the hop,
    and the hop's own two ends are where to look for it: it runs from the merge
    station to the port on the converging line's own track, so a feeder arriving
    at either lands on one of them without any offset arithmetic.  Dropping it
    needs both -- no feeder waiting at the merge station, and one already at the
    port to carry the line in.

    Runs after the coincidence passes, since only the settled channels say where
    the feeders converge.
    """
    if not ctx.merge.junctions:
        return ()
    feeders_by_merge: dict[str, list[RoutedPath]] = defaultdict(list)
    hop_by_merge: dict[str, RoutedPath] = {}
    for rp in routes:
        if rp.edge.target in ctx.merge.junctions:
            feeders_by_merge[rp.edge.target].append(rp)
        elif ctx.merge.entry_port_for.get(rp.edge.source) == rp.edge.target:
            hop_by_merge[rp.edge.source] = rp

    def ends_at(rp: RoutedPath, point: tuple[float, float]) -> bool:
        return (
            abs(rp.points[-1][0] - point[0]) <= COORD_TOLERANCE
            and abs(rp.points[-1][1] - point[1]) <= COORD_TOLERANCE
        )

    covered: set[int] = set()
    coverage_records: list[tuple[tuple[str, str, str], tuple[str, str, str]]] = []
    for merge_id, hop in hop_by_merge.items():
        feeders = feeders_by_merge.get(merge_id)
        if not feeders or any(ends_at(route, hop.points[0]) for route in feeders):
            continue
        carrier = next(
            (route for route in feeders if ends_at(route, hop.points[-1])), None
        )
        if carrier is None:
            continue
        covered.add(id(hop))
        coverage_records.append(
            (
                (hop.edge.source, hop.edge.target, hop.line_id),
                (carrier.edge.source, carrier.edge.target, carrier.line_id),
            )
        )
    if not covered:
        return ()
    routes[:] = [route for route in routes if id(route) not in covered]
    return tuple(coverage_records)


def _unify_coincident_corner_radii(routes: list[RoutedPath]) -> None:
    """Give same-line turns shared by several legs one radius.

    Fusing same-line legs onto one channel leaves each with the flanking radius
    its handler assigned -- a solo leg the base radius, a leg that is the outer
    member of a concentric multi-line bundle a wider one.  Where fused legs share
    a turn vertex the two arcs draw concentrically a few pixels apart: the
    doubled corner :func:`check_coincident_corner_radii` forbids.  Snap each
    shared turn to the widest radius every coincident leg can resolve from its
    available runway, so the fused stroke reads as one clean arc.  A wider
    desired radius is not shared geometry when one short lead clamps it.

    Bundle-mates of different lines sit ``OFFSET_STEP`` apart and never share a
    vertex, so their concentric nesting is untouched; only truly coincident
    same-line turns collapse.
    """
    buckets: dict[tuple[str, int, int], list[tuple[RoutedPath, int]]] = defaultdict(
        list
    )
    for rp in routes:
        radii = rp.curve_radii
        if radii is None:
            continue
        pts = rp.points
        for k in range(1, len(pts) - 1):
            if k - 1 >= len(radii) or not is_orthogonal_turn(
                pts[k - 1], pts[k], pts[k + 1]
            ):
                continue
            key = (rp.line_id, round(pts[k][0]), round(pts[k][1]))
            buckets[key].append((rp, k - 1))

    # Establish the concentric family's preferred outer radius first.  The
    # resolved pass below only reduces a member when its waypoint runway cannot
    # draw that common preference.
    for members in buckets.values():
        if len(members) < 2:
            continue
        desired: list[float] = []
        for route, i in members:
            assert route.curve_radii is not None
            desired.append(route.curve_radii[i])
        widest = widest_coincident_radius(desired)
        for route, i in members:
            assert route.curve_radii is not None
            route.curve_radii[i] = widest

    # Lower only the members that resolve wider than the bucket's limiting leg.
    # A radius on an adjacent corner shares segment budget, so repeat until a
    # correction can no longer alter a neighbouring coincident bucket.
    for _ in range(max(1, len(buckets))):
        changed = False
        resolved_by_route: dict[int, list[float]] = {}

        def resolved_vector(route: RoutedPath) -> list[float]:
            key = id(route)
            resolved = resolved_by_route.get(key)
            if resolved is None:
                resolved = resolve_curve_radii(route.points, route.curve_radii)
                resolved_by_route[key] = resolved
            return resolved

        for members in buckets.values():
            if len(members) < 2:
                continue
            effective = [resolved_vector(route)[i] for route, i in members]
            common = min(effective)
            for (route, i), actual in zip(members, effective, strict=True):
                if actual - common <= COORD_TOLERANCE_FINE:
                    continue
                radii = route.curve_radii
                assert radii is not None
                low, high = 0.0, radii[i]
                for _step in range(64):
                    candidate = (low + high) / 2
                    radii[i] = candidate
                    resolved = resolve_curve_radius_at(route.points, radii, i)
                    if resolved < common:
                        low = candidate
                    else:
                        high = candidate
                radii[i] = high
                resolved_by_route.pop(id(route), None)
                changed = True
        if not changed:
            break


def _reconcile_port_peeloff_risers(routes: list[RoutedPath], ctx: _RoutingCtx) -> None:
    """Re-stack port approaches onto the slots their settled trunk depth earns.

    The riser order is assigned during gap materialisation from the trunk
    depths known then, but the later trunk-slot pass can repack those depths --
    a hand-authored grid can stagger them against their source columns -- which
    can leave a riser on a slot a different depth earns: the braid
    :func:`check_peeloff_concentric` flags.  Running after the trunk pass, this
    reads the settled depths and permutes each off-slot approach onto the
    depth-earned peel-X and port-Y slots.  Those ranks are independent for a
    half-turn: reversing horizontal direction transposes the port order without
    necessarily reversing the vertical channels.  The in-section continuation
    leaves the port at its base Y, so this only re-seats the concentric stagger
    at the port, never the section linkage.
    """
    step = ctx.offset_step
    for bundle in iter_port_peeloff_bundles(routes, ctx.graph, step):
        targets = peeloff_target_slots(bundle)
        n = len(bundle.per_line)
        for rp, tail in bundle.entries:
            slot = targets[rp.edge.line_id]
            if tail_on_slot(tail, slot):
                continue
            ch = _VChannel(
                route=rp,
                idx=len(rp.points) - 3,  # riser leg points[-3] -> points[-2]
                x=tail.peel_x,
                y_lo=min(tail.trunk_y, tail.port_y),
                y_hi=max(tail.trunk_y, tail.port_y),
                down=tail.port_y > tail.trunk_y,
            )
            if _planner_owns_channel(ch):
                continue
            _restack_channel(
                ch,
                slot.peel_x,
                slot.rank,
                n,
                step,
                ctx.curve_radius,
            )
            seat_peeloff_port_y(rp, slot.port_y)


def _stagger_convergent_distinct_lines(
    routes: list[RoutedPath], ctx: _RoutingCtx
) -> None:
    """Seat distinct-line final port descents in destination lane order.

    The distinct-line counterpart to :func:`_coincide_same_line_tracks`: two
    feeders of DIFFERENT lines converging on one entry port can be forced down
    the same vertical channel -- the only inter-column gap left of a wide
    row-span target admits just one -- drawing one stroke on top of the other,
    the overlay ``check_collinear_distinct_lines`` forbids.  Both feeds are
    self-contained concentric loops (``normalize_exempt``), so neither the
    gap-slot pass nor the same-line coincidence pass separates them.

    Re-stack each coincident distinct-line cluster ``OFFSET_STEP`` apart. A
    complete port group whose feeders approach from opposite horizontal sides
    is handled atomically even when its channels are already adjacent, because
    assigning its members in separate clusters can reverse the order twice
    before the port. Both cases map the descent-X sequence to the per-line
    entry-Y order needed by the concentric port-approach corner. All routes of
    one line share a descent X, so the same-line tracks the coincidence pass
    already fused stay fused.
    """
    entry_port_for = ctx.merge.entry_port_for
    by_port: dict[tuple[str, bool], list[tuple[RoutedPath, _VChannel]]] = defaultdict(
        list
    )
    for rp in routes:
        if not rp.is_inter_section:
            continue
        ch = _final_port_approach(rp)
        if ch is None:
            continue
        target = entry_port_for.get(rp.edge.target, rp.edge.target)
        by_port[(target, ch.down)].append((rp, ch))

    opposing = {
        (bundle.port_id, bundle.vertical_sign > 0): bundle
        for bundle in iter_opposing_entry_confluences(
            routes,
            ctx.graph,
            ctx.offset_step,
        )
    }

    for (port_id, down), entries in by_port.items():
        if any(_planner_owns_channel(channel) for _route, channel in entries):
            continue
        port = ctx.graph.ports.get(port_id)
        if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        opposing_bundle = opposing.get((port_id, down))
        if opposing_bundle is not None and {
            id(route) for route, _tail in opposing_bundle.entries
        } != {id(route) for route, _channel in entries}:
            opposing_bundle = None
        if opposing_bundle is not None:
            slots = opposing_entry_confluence_slots(
                opposing_bundle,
                ctx.graph,
                ctx.offset_step,
            )
            if any(
                _planner_owns_channel(channel)
                or _descent_crosses_section(
                    ctx.graph,
                    channel,
                    slots[route.line_id].peel_x,
                )
                for route, channel in entries
            ):
                continue
            realised_xs = [slot.peel_x for slot in slots.values()]
            inner_x = (
                max(realised_xs) if port.side is PortSide.LEFT else min(realised_xs)
            )
            for route, channel in entries:
                slot = slots[route.line_id]
                _reseat_concentric_flanking(
                    route,
                    channel.idx,
                    slot.peel_x,
                    axis=0,
                    offset_in=0.0,
                    offset_out=slot.peel_x - inner_x,
                )
            continue
        forced_order = _cross_row_convergence_channel_order(port_id, entries, ctx)
        if forced_order is not None:
            _stack_distinct_port_descents(entries, port, ctx, line_order=forced_order)
            continue
        entries.sort(key=lambda e: e[1].x)
        cluster: list[tuple[RoutedPath, _VChannel]] = []
        for rp, ch in entries:
            if cluster and abs(ch.x - cluster[-1][1].x) > COORD_TOLERANCE + 1.0:
                _stack_distinct_port_descents(cluster, port, ctx)
                cluster = []
            cluster.append((rp, ch))
        _stack_distinct_port_descents(cluster, port, ctx)


def _cross_row_convergence_channel_order(
    port_id: str,
    entries: list[tuple[RoutedPath, _VChannel]],
    ctx: _RoutingCtx,
) -> list[str] | None:
    """Outer-to-inner channel order for an off-row convergence bundle."""
    port = ctx.graph.ports.get(port_id)
    if port is None or port.side is not PortSide.LEFT:
        return None
    section = ctx.graph.section_for_port(port)
    if (
        ctx.graph.compact_offsets
        or section.direction != "LR"
        or port.section_id in ctx.reversed_sections
    ):
        return None
    line_ids = {rp.line_id for rp, _channel in entries}
    ordered = cross_row_convergence_channel_order(ctx.graph, port_id)
    if ordered is None or line_ids != set(ordered):
        return None
    return ordered


def _stack_distinct_port_descents(
    cluster: list[tuple[RoutedPath, _VChannel]],
    port: Port,
    ctx: _RoutingCtx,
    *,
    line_order: list[str] | None = None,
) -> None:
    """Re-seat one cluster of distinct-line port descents.

    Lines are placed one ``OFFSET_STEP`` apart from the cluster's outer edge
    inward, ordered longest-descent-first so the feeder that turns down furthest
    from the port nests outermost.  Each feeder reaches its channel by a
    horizontal traverse at its turn-down Y before dropping in; seating the
    longest descent (whose turn spans the widest Y range) on the outer lane
    keeps every traverse clear of the other feeders' descents, so the
    convergent lanes stay parallel rather than crossing (#1326). Ordinary calls
    leave already-separated descents alone; an explicit ``line_order`` reseats
    a classified convergence onto that order.
    """
    if any(
        _planner_owns_channel(channel)
        and not convergence_owns_segment_boundary(channel.route, channel.idx)
        for _route, channel in cluster
    ):
        return
    has_planned_channel = any(
        convergence_owns_segment_boundary(channel.route, channel.idx)
        for _route, channel in cluster
    )
    by_line: dict[str, list[_VChannel]] = defaultdict(list)
    for rp, ch in cluster:
        by_line[rp.line_id].append(ch)
    if len(by_line) < 2:
        return
    offs = ctx.station_offsets or {}
    span = {lid: max(ch.y_hi - ch.y_lo for ch in chs) for lid, chs in by_line.items()}
    ordered = line_order or sorted(
        by_line, key=lambda lid: (-span[lid], offs.get((port.id, lid), 0.0))
    )
    xs = [ch.x for _rp, ch in cluster]
    step = ctx.offset_step
    left = port.side is PortSide.LEFT
    n = len(ordered)
    if line_order is None:
        base = min(xs) if left else max(xs)
    else:
        inner = by_line[ordered[-1]][0].x
        base = inner - (n - 1) * step if left else inner + (n - 1) * step
    # The innermost lane (rank n-1) is the concentric reference, anchored at the
    # base radius; every other lane's corner is sized by its signed X offset from
    # it, so the outer lanes take the wider radius and the convergent arcs hold a
    # constant gap rather than pinching where equal-radius corners a step apart
    # would.  The offset is signed by the seating direction (a LEFT entry seats
    # outward in -X, a RIGHT entry in +X), which the corner helper needs to widen
    # the outer side rather than tighten it.
    x_inner = base + (n - 1) * step if left else base - (n - 1) * step
    target_x_by_line = {
        lid: base + rank * step if left else base - rank * step
        for rank, lid in enumerate(ordered)
    }
    if has_planned_channel and any(
        abs(channel.x - target_x_by_line[line_id]) > COORD_TOLERANCE
        for line_id, channels in by_line.items()
        for channel in channels
    ):
        return
    for rank, lid in enumerate(ordered):
        x = target_x_by_line[lid]
        offset = x - x_inner
        for ch in by_line[lid]:
            if abs(ch.x - x) > COORD_TOLERANCE or abs(offset) > COORD_TOLERANCE:
                _set_vchannel_x(ch, x, offset)


def _nest_bypass_above_over_top_wrap(
    routes: list[RoutedPath], ctx: _RoutingCtx
) -> None:
    """Lift a cross-row inter-row bypass above a same-row over-top wrap it crosses.

    A line reaching the RIGHT-side entry port of a same-row neighbour must loop
    over that neighbour's top -- an over-top *wrap* whose peak is pinned deep in
    the inter-row gap by the neighbour's header clearance.  A longer-haul bypass
    crossing the same gap between two rows centres its channel lower (nearer the
    row), so its horizontal traverse runs *below* the wrap's peak and the wrap's
    riser crosses it.  The through bypass logically belongs further up the gap:
    lift its traverse (and the risers feeding it) above the wrap's peak so the
    local wrap nests beneath the through route rather than crossing it.

    A wrap is an inter-section route with both endpoints below the gap (same
    row) and a horizontal leg inside it; a through route crosses the gap (one
    endpoint above, one below).  The whole crossing through-bundle is lifted by
    one delta so its per-line stagger is preserved.
    """
    for _upper, gap_top, gap_bottom in iter_inter_row_gaps(ctx.graph):
        wrap_peaks: list[tuple[float, float, float]] = []
        through_legs: list[tuple[RoutedPath, int, float, float, float]] = []
        for r in routes:
            span = _route_gap_span(ctx.graph, r, gap_top, gap_bottom)
            if span is None:
                continue
            is_wrap, is_through = span
            for k in range(len(r.points) - 1):
                (x1, y1), (x2, y2) = r.points[k], r.points[k + 1]
                if abs(y1 - y2) > COORD_TOLERANCE:
                    continue
                if not gap_top - COORD_TOLERANCE <= y1 <= gap_bottom + COORD_TOLERANCE:
                    continue
                lo, hi = min(x1, x2), max(x1, x2)
                if is_wrap:
                    wrap_peaks.append((y1, lo, hi))
                elif is_through and not convergence_owns_segment_boundary(r, k):
                    through_legs.append((r, k, y1, lo, hi))
        if not wrap_peaks or not through_legs:
            continue
        crossing: list[tuple[RoutedPath, int, float]] = []
        for r, k, y, lo, hi in through_legs:
            if any(
                wlo < hi and lo < whi and wy <= y + COORD_TOLERANCE
                for wy, wlo, whi in wrap_peaks
            ):
                crossing.append((r, k, y))
        if not crossing:
            continue
        # Lift the whole crossing bundle by one delta so its stagger survives:
        # seat its deepest (largest-Y) leg the edge clearance below the upper
        # row, the deepest lane clear of that row.  Placement reserves a gap
        # wide enough (see ``_inter_row_routing_minimums``) that this lane sits
        # above the wrap's peak, so the local wrap nests beneath the bundle.
        target = gap_top + INTER_ROW_EDGE_CLEARANCE
        max_leg_y = max(y for _r, _k, y in crossing)
        delta = min(target - max_leg_y, 0.0)
        if abs(delta) <= COORD_TOLERANCE:
            continue
        # Translating both endpoints of the horizontal leg by one delta keeps its
        # flanking corners at 90 degrees, so their radii are unchanged -- no need
        # for the corner re-derivation ``_set_vchannel_x`` does for an X move.
        for r, k, _y in crossing:
            r.points = [
                (x, y + delta) if i in (k, k + 1) else (x, y)
                for i, (x, y) in enumerate(r.points)
            ]


def _route_gap_span(
    graph: MetroGraph, r: RoutedPath, gap_top: float, gap_bottom: float
) -> tuple[bool, bool] | None:
    """Classify *r* relative to an inter-row gap: ``(is_wrap, is_through)``.

    A wrap has both port endpoints below the gap (a same-row over-top loop); a
    through route has one endpoint above and one below (it crosses the gap).
    Returns ``None`` for non-inter-section routes or ones that touch neither
    side, so the caller skips them.
    """
    if not r.is_inter_section:
        return None
    src, tgt = graph.edge_endpoints(r.edge)
    src_below = src.y > gap_bottom - COORD_TOLERANCE
    tgt_below = tgt.y > gap_bottom - COORD_TOLERANCE
    src_above = src.y < gap_top + COORD_TOLERANCE
    tgt_above = tgt.y < gap_top + COORD_TOLERANCE
    is_wrap = src_below and tgt_below
    is_through = (src_above and tgt_below) or (src_below and tgt_above)
    if not (is_wrap or is_through):
        return None
    return is_wrap, is_through


def _band_clusters(chans: list[_VChannel], band: float) -> list[list[_VChannel]]:
    """Group X-sorted channels, breaking wherever a left-neighbour gap exceeds *band*.

    Channels closer than *band* share a cluster; a wider gap starts a new one,
    so widely-separated descents stay distinct corridors.
    """
    clusters: list[list[_VChannel]] = []
    for ch in sorted(chans, key=lambda c: c.x):
        if clusters and ch.x - clusters[-1][-1].x <= band:
            clusters[-1].append(ch)
        else:
            clusters.append([ch])
    return clusters


def _convergent_port_groups(
    routes: list[RoutedPath], ctx: _RoutingCtx
) -> list[_Coincidence]:
    """Same-line final descents converging on one entry port, grouped to fuse.

    Several inter-section edges of one line can arrive at an entry port as
    separate near-parallel vertical descents (each turning into the port via
    its own short horizontal lead) a few pixels apart.  Where those descents
    sit in a tight band they are one convergence channel and fuse onto the
    member nearest the port (smallest |vx - ex|), so the line arrives as a
    single track and splits only upstream where each feed peels off at its own
    Y; descents staggered more than ``EDGE_TO_BUNDLE_CLEARANCE`` apart are
    distinct corridors and stay separate.

    A merge trunk's route ends at the entry port but carries the merge junction
    as its edge target; map that to the entry port so the trunk and any sibling
    feed of the same line arriving directly at the port (e.g. an exit-port
    source not folded into the merge) share one approach key and fuse.
    """
    entry_port_for = ctx.merge.entry_port_for

    def spans(
        rp: RoutedPath,
    ) -> Iterable[tuple[tuple[str, str, bool], _VChannel]]:
        ch = _final_port_approach(rp)
        if ch is None:
            return
        # Group by the destination port, not its exact terminal (x, y): two
        # same-line descents into one port can land a per-line offset apart
        # before render offsets are applied, and keying on the raw endpoint
        # would split that single convergence into two.
        target = entry_port_for.get(rp.edge.target, rp.edge.target)
        yield (target, rp.line_id, ch.down), ch

    by_port = _group_channels_by(routes, spans)

    groups: list[_Coincidence] = []
    for chans in by_port.values():
        if len(chans) < 2:
            continue
        ex = chans[0].route.points[-1][0]
        for cluster in _band_clusters(chans, EDGE_TO_BUNDLE_CLEARANCE):
            if len(cluster) < 2:
                continue
            ref_x = min(cluster, key=lambda c: abs(c.x - ex)).x
            groups.append(_Coincidence(cluster, ref_x))
    return groups


def _reseat_concentric_flanking(
    rp: RoutedPath,
    k: int,
    new_coord: float,
    axis: int,
    offset_in: float = 0.0,
    offset_out: float = 0.0,
    base_radius: float = CURVE_RADIUS,
    base_radius_out: float | None = None,
) -> None:
    """Move the ``points[k] -> points[k+1]`` segment onto *new_coord* and re-derive
    its two flanking corners concentrically.

    *axis* selects the moved coordinate: ``0`` slides a vertical channel to a new
    X, ``1`` slides a horizontal trunk to a new Y.  Both segment endpoints take
    *new_coord* on that axis and keep their other coordinate, so the segment stays
    straight and the flanking legs stretch to meet it.

    ``offset_in`` / ``offset_out`` are the signed displacements from the bundle's
    reference line at the incoming (``k-1``) and outgoing (``k``) corners.  The
    two corners may pass different reference radii when they belong to separate
    concentric families; otherwise both use ``base_radius``.
    """
    pts = rp.points
    for j in (k, k + 1):
        px, py = pts[j]
        pts[j] = (new_coord, py) if axis == 0 else (px, new_coord)
    if rp.curve_radii is None:
        return
    radius_out = base_radius if base_radius_out is None else base_radius_out
    for radius_idx, offset, reference in (
        (k - 1, offset_in, base_radius),
        (k, offset_out, radius_out),
    ):
        if 0 <= radius_idx < len(rp.curve_radii):
            prev_pt, corner_pt, next_pt = pts[radius_idx : radius_idx + 3]
            rp.curve_radii[radius_idx] = concentric_corner_radius_at(
                prev_pt, corner_pt, next_pt, offset, reference
            )


def _set_vchannel_x(
    ch: _VChannel,
    new_x: float,
    offset: float = 0.0,
    *,
    offset_out: float | None = None,
    base_radius: float = CURVE_RADIUS,
    base_radius_out: float | None = None,
) -> None:
    """Move a vertical channel to *new_x*, re-deriving its flanking corners.

    Each flanking corner is re-derived from the route's *final* waypoints via
    :func:`concentric_corner_radius_at` -- the same central helper the routing
    handlers use -- rather than hand-set to a fixed radius after the move.

    *offset* is the incoming corner's signed displacement from the reference
    line the bundle nests around.  *offset_out* supplies a distinct displacement
    for the outgoing corner; omitting it applies *offset* to both corners.
    Fusing same-line descents onto one track leaves each a single stroke with no
    nesting, so it passes zero and every corner resolves to the base radius.

    ``base_radius_out`` gives the outgoing corner family its own reference.
    When omitted, both corners use ``base_radius``.
    """
    _reseat_concentric_flanking(
        ch.route,
        ch.idx,
        new_x,
        axis=0,
        offset_in=offset,
        offset_out=offset if offset_out is None else offset_out,
        base_radius=base_radius,
        base_radius_out=base_radius_out,
    )


def _set_htrunk_y(
    rp: RoutedPath,
    k: int,
    new_y: float,
    offset_in: float = 0.0,
    offset_out: float = 0.0,
) -> None:
    """Move an interior horizontal trunk (``points[k]->[k+1]``) to *new_y*.

    The horizontal counterpart of :func:`_set_vchannel_x`: it re-derives the
    trunk's two flanking corners from the moved waypoints via
    :func:`concentric_corner_radius_at`, so a fused same-line trunk draws one arc
    at each end rather than a corner sized for its old band.

    ``offset_in`` / ``offset_out`` are the trunk's signed displacement from the
    bundle's reference line at its incoming (``k-1``) and outgoing (``k``)
    corners.  A fused same-line trunk passes zero on both (one stroke, base
    radius).  A distinct-line corridor traverse nests only where it runs
    alongside its bundle-mates: the incoming corner off the shared descent takes
    the line's rank displacement, but the outgoing corner -- where it peels off
    to its own port, alone -- keeps the base radius (zero).
    """
    _reseat_concentric_flanking(
        rp, k, new_y, axis=1, offset_in=offset_in, offset_out=offset_out
    )


def _opening_fanout_descent(rp: RoutedPath) -> _VChannel | None:
    """The first vertical descent leaving a route's source, when it leads H then V.

    Wraps :func:`initial_fanout_descent_span` in a :class:`_VChannel` whose
    ``idx`` points at ``points[1]``, or ``None`` when the route does not open
    horizontal-then-vertical.
    """
    span = initial_fanout_descent_span(rp)
    if span is None:
        return None
    x, y_lo, y_hi, down = span
    return _VChannel(route=rp, idx=1, x=x, y_lo=y_lo, y_hi=y_hi, down=down)


def _initial_fanout_descent(rp: RoutedPath) -> _VChannel | None:
    channel = _opening_fanout_descent(rp)
    if channel is None:
        return None
    return None if _planner_owns_channel(channel) else channel


def _divergent_source_spans(
    rp: RoutedPath,
) -> Iterable[tuple[tuple[str, str, bool], _VChannel]]:
    """A route's opening fan-out descent, keyed by source, line and direction."""
    ch = _opening_fanout_descent(rp)
    if ch is not None:
        yield (rp.edge.source, rp.line_id, ch.down), ch


def _distinct_descent_spans(
    rp: RoutedPath,
) -> Iterable[tuple[tuple[str, bool], _VChannel]]:
    """A route's opening fan-out descent, keyed by source and direction."""
    channel = _opening_fanout_descent(rp)
    if channel is None:
        return
    yield (rp.edge.source, channel.down), channel


def _divergent_source_groups(routes: list[RoutedPath]) -> list[_Coincidence]:
    """Same-line opening descents leaving one source, grouped to fuse.

    The mirror of :func:`_convergent_port_groups`: where that groups same-line
    descents *arriving* at one port, this groups same-line descents *leaving*
    one source (a junction or exit port).  Several inter-section edges of one
    line fanning out from one source each open with their own horizontal lead
    and vertical channel a few pixels apart; every branch leaves on the same
    source-Y lead, so they share the descent until each turns off -- one trunk
    that split too early.  Left apart they read as parallel same-colour tracks,
    and an inverted split (the farther-reaching branch opening inside the
    nearer one) crosses its sibling's descent.

    Descents are grouped by source endpoint + line + descent direction; every
    group of two or more fuses onto the channel nearest the source, hugging the
    side the branches leave from, and splits off downstream at each own turn Y.
    Unlike the convergent case there is no proximity band: any same-source pair
    overlapping in Y must collapse, however far apart their Xs.

    A descent that opens a multi-line bundle (its source->target edge carries
    more than one line) sits on an X the bundle's concentric fan places: the
    lines nest in a fixed order there, so the fused X must keep every such
    member ordered against its bundle-mates.  The reference is therefore drawn
    from the bundle-locked members when the group has any -- the free same-line
    descents snap onto the bundle rather than dragging a member off its slot
    into a bundle-mate of another line; only an all-free group falls back to the
    descent nearest the source.
    """
    by_source = _group_channels_by(routes, _divergent_source_spans)
    lines_per_edge: dict[tuple[str, str], set[str]] = defaultdict(set)
    for chans in by_source.values():
        for ch in chans:
            e = ch.route.edge
            lines_per_edge[(e.source, e.target)].add(ch.route.line_id)

    def bundle_locked(ch: _VChannel) -> bool:
        e = ch.route.edge
        return len(lines_per_edge[(e.source, e.target)]) > 1

    groups: list[_Coincidence] = []
    for chans in by_source.values():
        if len(chans) < 2:
            continue
        locked = [c for c in chans if bundle_locked(c)]
        candidates = locked if locked else chans
        sx = chans[0].route.points[0][0]
        ref_x = min(candidates, key=lambda c: abs(c.x - sx)).x
        groups.append(_Coincidence(chans, ref_x))
    return groups


def _fanout_descent_order_key(ch: _VChannel) -> tuple[int, float]:
    """Left-to-right seat order for a fan-out descent, by the side it turns to.

    A branch is placed on the side it later turns toward so peeling off never
    crosses a sibling still descending: left-turners (and straight drops) rank
    by turn-Y ascending so the earliest turn sits outermost-left; right-turners
    mirror it.  ``direction`` is the sign of the leg leaving the descent's foot.
    """
    pts = ch.route.points
    j = ch.idx + 1
    dx = pts[j + 1][0] - pts[j][0] if j + 1 < len(pts) else 0.0
    direction = -1 if dx < -COORD_TOLERANCE else (1 if dx > COORD_TOLERANCE else 0)
    turn_y = pts[j][1]
    return (direction, turn_y if direction <= 0 else -turn_y)


def _fan_opening_reference_radii(
    moves: list[tuple[_VChannel, float, float]],
    curve_radius: float,
) -> tuple[float, float]:
    """Smallest references that keep both fan corner families at the floor.

    A ranked channel can be outside one turn and inside the next.  Its signed
    displacement therefore widens one arc while narrowing the other.  Derive a
    separate reference for each side instead of making the source turn inherit
    the extra radius needed only by the peel-off turn.
    """
    references = [curve_radius, curve_radius]
    for channel, target_x, rank_offset in moves:
        route = channel.route
        if route.curve_radii is None:
            continue
        points = list(route.points)
        for index in (channel.idx, channel.idx + 1):
            x, y = points[index]
            points[index] = (target_x, y)
        radius_indices = range(
            channel.idx - 1,
            min(channel.idx + 1, len(route.curve_radii)),
        )
        for side, radius_index in enumerate(radius_indices):
            required_reference = concentric_reference_radius_at(
                points[radius_index],
                points[radius_index + 1],
                points[radius_index + 2],
                rank_offset,
                curve_radius,
            )
            references[side] = max(references[side], required_reference)
    return references[0], references[1]


def _bundle_divergent_distinct_descents(
    routes: list[RoutedPath], ctx: _RoutingCtx
) -> None:
    """Bundle distinct-line opening descents leaving one source until they fork.

    The distinct-line counterpart to the divergent group in
    :func:`_coincide_same_line_tracks`: several lines fanning out from one
    source leave the section together and read as one bundle, so they should
    descend on adjacent tracks and split only where each turns off.  Handlers
    route each branch independently, so distinct lines open on their own
    channels several px apart -- reading as separate strokes from the junction.

    Give every such group concentric opening corners, with one ``OFFSET_STEP``
    slot per LINE (a line's several same-colour branches share the fused track
    the coincidence pass gave them).  Lines are ordered on the side they later
    turn toward, outermost by earliest turn, so a branch peeling off never
    crosses a sibling that has not yet turned off.  A wide group moves onto a
    tight adjacent set of tracks; a group already occupying those tracks keeps
    its coordinates while its corner radii are derived from the same ranks.
    """
    by_source = _group_channels_by(routes, _distinct_descent_spans)

    step = ctx.offset_step
    for chans in by_source.values():
        if any(
            _planner_owns_channel(channel)
            and not convergence_owns_segment_boundary(channel.route, channel.idx)
            for channel in chans
        ):
            continue
        # Same-line descents share one X (the coincidence pass snaps them onto a
        # common track), so a line occupies ONE bundle slot however many branches
        # it carries.  Seat per line, not per channel: keying each channel
        # individually spreads a single line across adjacent slots.
        by_line: dict[str, list[_VChannel]] = defaultdict(list)
        for c in chans:
            by_line[c.route.line_id].append(c)
        if len(by_line) < 2:
            continue
        # Distinct lines that all reach ONE shared entry port do not diverge --
        # they converge into that port as a single concentric peel-off bundle,
        # whose slot order :func:`_reconcile_port_peeloff_risers` owns (by trunk
        # depth into the port).  Re-seating them here by their divergent turn-Y
        # order would fight that owner and flip the bundle where the port's
        # offsets step across the section seam: a line reused on non-adjacent
        # fan legs can leave an empty interior lane there, which widens the
        # peel-x span past a tight bundle, so this pass would otherwise claim them.
        targets = {c.route.edge.target for c in chans}
        if len(targets) == 1:
            port = ctx.graph.ports.get(next(iter(targets)))
            if port is not None and port.is_entry:
                continue
        target_sets = {
            frozenset(ch.route.edge.target for ch in line_channels)
            for line_channels in by_line.values()
        }
        if len(target_sets) == 1:
            continue
        xs = [c.x for c in chans]
        base = min(xs)
        line_key = {
            lid: min(_fanout_descent_order_key(c) for c in cs)
            for lid, cs in by_line.items()
        }
        ordered = sorted(by_line, key=line_key.__getitem__)
        tight = max(xs) - min(xs) <= step * (len(by_line) - 1) + COORD_TOLERANCE
        if (
            any(
                convergence_owns_segment_boundary(channel.route, channel.idx)
                for channel in chans
            )
            and not tight
        ):
            continue
        moves = (
            [(ch, ch.x, ch.x - base) for ch in chans]
            if tight
            else [
                (ch, base + rank * step, rank * step)
                for rank, lid in enumerate(ordered)
                for ch in by_line[lid]
            ]
        )
        # Never re-seat a descent into a section it does not belong to; leave the
        # whole group on its handler channels if any target column is obstructed.
        if any(
            _descent_crosses_section(ctx.graph, ch, target_x)
            for ch, target_x, _rank_off in moves
        ):
            continue
        # Rank owns both track position and arc size.  Applying it to an already
        # tight group is necessary because its independent route families can
        # carry base radii that do not share an arc centre.
        base_radius, base_radius_out = _fan_opening_reference_radii(
            moves, ctx.curve_radius
        )
        for ch, target_x, rank_off in moves:
            _set_vchannel_x(
                ch,
                target_x,
                rank_off,
                base_radius=base_radius,
                base_radius_out=base_radius_out,
            )


def _descent_crosses_section(graph: MetroGraph, ch: _VChannel, x: float) -> bool:
    """Whether *ch*'s vertical span at *x* would cross a foreign section box.

    Sections at either end of the channel's route are exempt (the descent
    legitimately meets its own endpoints).
    """
    own = frozenset(
        graph.section_for_station(ep)
        for ep in (ch.route.edge.source, ch.route.edge.target)
    )
    return _section_intrudes(graph, x, ch.y_lo, ch.y_hi, exclude=own)


def _merge_trunks_and_feeders(
    routes: list[RoutedPath], merge: _MergeRouting
) -> Iterator[tuple[str, RoutedPath, list[RoutedPath]]]:
    """Yield ``(merge_id, trunk_route, other_feeder_routes)`` per trunked merge.

    Grouping the routes by target rather than looking each merge edge up by key
    drops the feeders that were never routed as paths of their own: a port
    predecessor is redirected into the merge instead (see
    ``_classify_merge_edges``), so its edge has no route to find.  A merge whose
    trunk carrier itself did not route is skipped: it has no channel for the
    others to converge onto.  An empty feeder list yields normally, leaving
    callers to iterate nothing rather than guard a case.
    """
    if not merge.trunk_source:
        return
    by_target: dict[str, list[RoutedPath]] = defaultdict(list)
    for rp in routes:
        if rp.is_inter_section and rp.edge.target in merge.trunk_source:
            by_target[rp.edge.target].append(rp)
    for mjid, trunk_src in merge.trunk_source.items():
        trunk_rp: RoutedPath | None = None
        others: list[RoutedPath] = []
        for rp in by_target[mjid]:
            if rp.edge.source == trunk_src:
                trunk_rp = rp
            else:
                others.append(rp)
        if trunk_rp is not None:
            yield mjid, trunk_rp, others


def _merge_feeder_groups(
    routes: list[RoutedPath], ctx: _RoutingCtx
) -> list[_Coincidence]:
    """Same-column merge feeders, grouped to fuse onto the trunk's descent.

    A merge with a trunk routes every other feeder as a branch dropping onto
    the trunk's bypass channel.  Feeders sharing the trunk's source column
    descend through the same inter-column gap; left on their own per-route X
    they read as parallel same-colour tracks (and, since both segments
    terminate at the merge, trip the same-line parallel-descent guard).  Each
    same-column feeder's opening descent fuses onto the trunk's so the
    converging line drops as one track, splitting only where each feeder's
    horizontal lead peels off at its own Y.  Feeders in other columns descend
    in their own gap and converge along the shared horizontal channel, so they
    are left alone.
    """
    graph = ctx.graph
    groups: list[_Coincidence] = []
    for mjid, trunk_rp, branch_rps in _merge_trunks_and_feeders(routes, ctx.merge):
        trunk_src_st = graph.stations[ctx.merge.trunk_source[mjid]]
        trunk_ch = _initial_fanout_descent(trunk_rp)
        if trunk_ch is None:
            continue
        trunk_col = _resolve_section_col(graph, trunk_src_st)
        members: list[_VChannel] = []
        for rp in branch_rps:
            src_st = graph.stations.get(rp.edge.source)
            if src_st is None or _resolve_section_col(graph, src_st) != trunk_col:
                continue
            ch = _initial_fanout_descent(rp)
            if ch is None:
                continue
            members.append(ch)
        if members:
            groups.append(_Coincidence(members, trunk_ch.x))
    return groups


def _merge_convergence_run(trunk_rp: RoutedPath, level: float) -> HTrunkSeg | None:
    """The trunk leg a merge's feeders converge onto, or ``None``.

    The feeders were routed toward the channel level the context published
    (``trunk_by``); the leg the trunk finally runs it on is whichever of its
    horizontal trunks sits nearest that level, since the slot materialisation
    that re-stacks the channel reassigns the Y but not which leg it is.
    """
    trunks = [seg for _k, seg in iter_horizontal_trunks(trunk_rp)]
    if not trunks:
        return None
    return min(trunks, key=lambda seg: abs(seg.y - level))


def _land_feeder_on_run(rp: RoutedPath, run: HTrunkSeg, ctx: _RoutingCtx) -> None:
    """Terminate one merge feeder on *run*, the trunk leg it converges onto.

    The feeder arrives as a descent into a short tail (:func:`_route_merge_branch`)
    whose level and length were fixed before the channel passes settled where the
    trunk actually runs, so both need re-deriving from the settled leg:

    * The tail moves onto the run's own Y, so the two lines meet on one
      centreline instead of running an offset step apart.
    * The tail is cut to the run it has left before the trunk turns away, so it
      overlaps the trunk rather than reaching past its corner into open space.

    A feeder with less than a radius of run left to travel cannot form that turn
    at all.  It converges on the corner itself: its descent moves onto the
    trunk's own turn column and runs a radius past the run, onto the leg the
    trunk continues into, so the two read as one unbroken column.  When the
    trunk continues back the way the feeder came, the descent already covers
    that leg and stops on the run.
    """
    radius = ctx.curve_radius
    pts = rp.points
    verticals = list(iter_vertical_segments(rp))
    if not verticals:
        return
    k, lead_x, y_lo, y_hi, down = verticals[-1]
    # A merge feeder's source is a junction, which may also be a fan-out junction
    # -- and _round_junction_perp_peeloff prepends a waypoint to those routes, so
    # the handler's four-point shape does not survive routing as a given.
    if k + 2 != len(pts) - 1 or y_hi - y_lo < radius:
        return
    travel = 1.0 if run.xb >= run.xa else -1.0
    along = (run.xb - lead_x) * travel
    if along >= radius:
        pts[k + 2] = (lead_x + travel * min(2 * radius, along), run.y)
        _set_htrunk_y(rp, k + 1, run.y)
        return
    ch = _VChannel(route=rp, idx=k, x=lead_x, y_lo=y_lo, y_hi=y_hi, down=down)
    overlap = radius if (run.after_y > run.y) == down else 0.0
    del pts[k + 2]
    # The same peel-off rounding pass clears curve_radii outright.
    if rp.curve_radii is not None:
        del rp.curve_radii[k:]
    pts[k + 1] = (lead_x, run.y + overlap * (1.0 if down else -1.0))
    _reconcile_moved_gap_slot(ch, run.xb, ctx.graph)
    _set_vchannel_x(ch, run.xb)


def _land_lane_changing_feeder_on_trunk_riser(
    rp: RoutedPath, run: HTrunkSeg, ctx: _RoutingCtx
) -> None:
    """Terminate a lane-changing straight feeder on the trunk's riser.

    An adjacent feeder that would only detour to reach the trunk's channel runs
    into the merge station instead (:func:`_adjacent_feeder_reaches_merge_directly`),
    as a straight two-vertex run.  The merge station sits one margin past the
    feeder junction, so when the converging line rides a different lane at the
    junction than at the merge, that run has to change lane within the margin --
    far less than the two corners of a lane change need -- and degenerates into a
    bare sloped segment.

    The trunk's riser out of *run* crosses the feeder's lane on its way to the
    entry level, so the feeder keeps its lane and terminates on the riser: the
    converging line reads as one stroke from the join onward, and no direction
    change is asked of a run that has no room to turn.  The merge -> entry hop is
    then redundant -- the trunk covers the entry approach from the riser on -- and
    :func:`_drop_covered_merge_entry_hops` retires it, since no feeder waits at
    the merge station.
    """
    if len(rp.points) != 2:
        return
    lane = _get_offset(ctx, rp.edge.source, rp.line_id)
    if abs(lane - _get_offset(ctx, rp.edge.target, rp.line_id)) <= COORD_TOLERANCE:
        return
    (sx, sy), (tx, _ty) = rp.points
    lane_y = sy + lane
    lo, hi = sorted((run.y, run.after_y))
    riser_spans_lane = lo + COORD_TOLERANCE < lane_y < hi - COORD_TOLERANCE
    riser_ahead = (run.xb - sx) * (tx - sx) > 0
    if not (riser_spans_lane and riser_ahead):
        return
    rp.points = [(sx, lane_y), (run.xb, lane_y)]
    rp.offset_regime = OffsetRegime.BAKED


def _land_merge_feeders_on_trunk(routes: list[RoutedPath], ctx: _RoutingCtx) -> None:
    """Land compatibility merge feeders on the trunk leg they converge onto.

    A merge with a trunk routes its other feeders as branches dropping toward the
    trunk's bypass channel (:func:`_route_merge_branch`), aimed at the level the
    context published rather than the one the trunk ends up on: the slot
    materialisation and coincidence passes re-stack the channel and slide the
    descent columns afterwards, each moving one leg of the feeder without the
    other.  A feeder therefore lands an offset step off the trunk's centreline,
    or carries its tail past the corner where the trunk has already turned away,
    and either way ends in a stroke cap over nothing.

    Compatibility systems settle where a feeder meets its trunk here, after
    every pass that can move either route. Planned systems are immutable and
    bypass this pass.
    """
    merge = ctx.merge
    for mjid, trunk_rp, others in _merge_trunks_and_feeders(routes, merge):
        if trunk_rp.convergence_plan_id is not None:
            continue
        run = _merge_convergence_run(trunk_rp, merge.trunk_by[mjid])
        if run is None:
            continue
        for rp in others:
            if (rp.edge.source, rp.edge.target, rp.line_id) in merge.branch_edges:
                _land_feeder_on_run(rp, run, ctx)
            else:
                _land_lane_changing_feeder_on_trunk_riser(rp, run, ctx)


def _materialize_trunk_slots(routes: list[RoutedPath], ctx: _RoutingCtx) -> None:
    """Resolve every declared :class:`TrunkSlot` to a concentric channel Y.

    The horizontal-trunk twin of :func:`_materialize_gap_slots`.  Handlers that
    emit a U-shaped bypass annotate its trunk with the inter-row gap it occupies
    (:meth:`RoutedPath.declare_trunk_slot`); this pass groups the trunks by that
    declared gap and fans the lines sharing a channel into one concentric
    ``OFFSET_STEP`` bundle, widest-reaching trunk outermost so the nesting
    introduces no crossings.  The gap is taken from the annotation, not the
    trunk's Y, precisely because this pass reassigns that Y.  Each trunk's
    traversal direction and band rank are read from its current geometry.

    Concentric fanning, crossing-minimal slot ordering and the flanking-radius
    recompute are per-gap geometry a single handler cannot do alone (it needs
    every trunk in the gap at once), so they stay here.  A group of only
    handler-owned (``normalize_exempt``) trunks keeps its geometry untouched;
    an exempt trunk sharing a channel with a non-exempt one joins the fan, and a
    non-exempt trunk left fused on an unbundled exempt run is cleared by
    :func:`_dogleg_off_exempt_trunks`.

    Trunks alone in their channel, or already at distinct Ys, are left
    untouched; the flanking corner radii are recomputed for any trunk that
    actually moves so the bundle stays concentric.
    """
    step = ctx.offset_step
    trunks = _declared_htrunks(routes)
    groups = _group_channel_trunks(trunks, step) if len(trunks) >= 2 else []

    # Routes whose trunk this pass has placed into a concentric bundle; the
    # dogleg pass treats exempt trunks as fixed obstacles and shoves nearby
    # trunks clear, which would tear a freshly-fanned 3px bundle apart, so it
    # skips any route already bundled here.
    bundled: set[int] = set()

    for grp in groups:
        # One trunk per distinct route; a shared channel needs >1 to fan.
        if len({id(t.route) for t in grp}) < 2:
            continue
        # Exempt (handler-owned) trunks only join the fan when they share the
        # channel with a non-exempt trunk; a group of only exempt trunks keeps
        # its handler geometry untouched here.
        if not any(not t.route.normalize_exempt for t in grp):
            continue
        # Opposite-direction flows that share one inter-row channel must not be
        # smooshed into one tight bundle (issue #484): a leftward and a rightward
        # bundle interleaved a step apart read as one fat band and can hide a
        # distinct line behind an exempt one.  Split the channel by traversal
        # direction and lay each direction on its own non-overlapping Y band,
        # with a clear visual gap between them; within a band the co-travelling
        # same-direction trunks still fan tight (OFFSET_STEP, concentric).
        by_dir = {sign: [t for t in grp if t.sign_x == sign] for sign in (1, -1)}
        bands = [b for b in by_dir.values() if b]
        # Order bands top -> bottom by current vertical position so allocation
        # moves each the least and never reorders the two flows (no new
        # crossing).
        bands.sort(key=lambda b: min(t.y for t in b))
        _stack_trunk_bands(bands, ctx, step, bundled)

    _dogleg_off_exempt_trunks(routes, ctx, skip=bundled)
    _bundle_same_destination_tails(routes, ctx)
    _separate_declared_opposing_gap_bundles(routes, ctx)


def _stack_trunk_bands(
    bands: list[list[_HTrunk]], ctx: _RoutingCtx, step: float, bundled: set[int]
) -> None:
    """Lay an ordered top -> bottom list of trunk bands into their inter-row gap.

    Each band fans concentrically onto its own packed tracks; the bands stack
    with a clear :data:`BUNDLE_TO_BUNDLE_CLEARANCE` gap between them, anchored at
    the current cluster top and slid up as a block if the stack would breach the
    next row's header clearance (sliding into the free upper gap keeps the
    inter-band gap intact rather than crowding the lower band into the header).
    Each band carries its own dip direction, so a down-dip and an up-dip band
    can stack together; a single-direction caller passes bands that all share
    one dip.
    """
    planned = [_plan_trunk_band(b) for b in bands]
    gap = BUNDLE_TO_BUNDLE_CLEARANCE
    total = sum((n - 1) * step for _o, _t, n in planned) + gap * (len(bands) - 1)
    top = min(t.y for b in bands for t in b)
    band_top = _clamp_inter_row_band_top(ctx, top, total)
    band_top = _hold_stack_in_claim_bands(ctx, band_top, planned, bands, step, gap)
    for (order, track_of, n), band in zip(planned, bands):
        _restack_trunk_band(
            order, track_of, n, band_top, band[0].dips_down, step, ctx, bundled
        )
        band_top += (n - 1) * step + gap


def _hold_stack_in_claim_bands(
    ctx: _RoutingCtx,
    band_top: float,
    planned: list[tuple[list[list[_HTrunk]], dict[int, int], int]],
    bands: list[list[_HTrunk]],
    step: float,
    gap: float,
) -> float:
    """The stack top holding every claimed direction band inside its own band.

    Each direction band sits at a fixed offset within the stack, so a claim on
    one band constrains where the whole stack may start.  The intersection of
    every claimed band's feasible interval holds the stack; jointly infeasible
    claims (or a stack with no claimed trunk) leave *band_top* as derived.
    """
    lo_bound = hi_bound = None
    offset = 0.0
    for (_order, _track_of, n), band in zip(planned, bands):
        height = (n - 1) * step
        claim = _bundle_claim_band(ctx, ((t.route, t.idx) for t in band))
        if claim is not None:
            lo = claim.lo - offset
            hi = claim.hi - height - offset
            lo_bound = lo if lo_bound is None else max(lo_bound, lo)
            hi_bound = hi if hi_bound is None else min(hi_bound, hi)
        offset += height + gap
    if lo_bound is None or hi_bound is None or hi_bound < lo_bound - COORD_TOLERANCE:
        return band_top
    return ReservedBand(lo_bound, hi_bound).hold(band_top)


def _separate_opposing_inter_row_trunks(
    routes: list[RoutedPath], ctx: _RoutingCtx
) -> None:
    """Split counter-running trunks sharing one inter-row gap onto distinct bands.

    :func:`_materialize_trunk_slots` fans same-direction trunks and, within a
    single dip group, splits opposing traversal directions onto separate Y
    bands.  It cannot separate two flows that enter one inter-row gap from
    *opposite rows* -- a drop-in descending from the upper row (``dips_down``)
    and a return leg rising from the lower row (``not dips_down``) -- because
    the two land in different dip groups and, being handler-owned
    (``normalize_exempt``), are left untouched.  Both then centre on the gap
    via :func:`bypass_bottom_y` and collide, drawing the line back over its own
    track (#1520).

    This direction-aware net groups every declared inter-row trunk by the gap
    it occupies and separates every overlapping pair whose dip direction or
    horizontal traversal direction opposes. Down-dip feeds stay above up-dip
    returns; flows with the same dip retain their current vertical order. Each
    direction class receives a full :data:`BUNDLE_TO_BUNDLE_CLEARANCE` band.
    """
    step = ctx.offset_step
    by_gap: dict[int, list[_HTrunk]] = defaultdict(list)
    for t in _declared_htrunks(routes):
        slot = t.route.trunk_slot
        if slot is not None and slot.gap_upper_row is not None:
            by_gap[slot.gap_upper_row].append(t)

    for gtrunks in by_gap.values():
        by_direction: defaultdict[tuple[bool, int], list[_HTrunk]] = defaultdict(list)
        for trunk in gtrunks:
            by_direction[(trunk.dips_down, trunk.sign_x)].append(trunk)

        participating: set[tuple[bool, int]] = set()
        groups = list(by_direction.items())
        for i, (a_key, a_trunks) in enumerate(groups):
            for b_key, b_trunks in groups[i + 1 :]:
                if not any(
                    _x_overlap((a.x_lo, a.x_hi), (b.x_lo, b.x_hi)) > 0
                    and abs(a.y - b.y) < BUNDLE_TO_BUNDLE_CLEARANCE
                    for a in a_trunks
                    for b in b_trunks
                ):
                    continue
                participating.update((a_key, b_key))
        if len(participating) < 2:
            continue

        bands = [(key, by_direction[key]) for key in participating]
        bands.sort(
            key=lambda item: (
                0 if item[0][0] else 1,
                min(trunk.y for trunk in item[1]),
            )
        )
        _stack_trunk_bands([band for _key, band in bands], ctx, step, set())


class _SlotFeatures(NamedTuple):
    """Riser leg xs and trunk x-spans for one slot.

    ``below`` / ``above`` are the xs of risers whose far end drops below /
    rises above the band; ``spans`` is each trunk's ``(x_lo, x_hi)``.
    """

    below: list[float]
    above: list[float]
    spans: list[tuple[float, float]]


def _trunk_slot_features(slot: list[_HTrunk]) -> _SlotFeatures:
    """Riser xs (below / above the band) and x-spans for one line's trunk slot.

    Each horizontal trunk is flanked by two vertical legs; a leg's far endpoint
    sits either below the trunk (continuing toward the lower row or a peel-off
    target) or above it (rising to the source row / junction).  The two legs
    can split (one up, one down at a peel-off), so they are classified
    individually rather than from the trunk's single ``dips_down`` flag.
    """
    below: list[float] = []
    above: list[float] = []
    spans: list[tuple[float, float]] = []
    for t in slot:
        pts = t.route.points
        k = t.idx
        spans.append((t.x_lo, t.x_hi))
        for leg_x, far_y in (
            (pts[k][0], pts[k - 1][1]),
            (pts[k + 1][0], pts[k + 2][1]),
        ):
            if far_y > t.y + COORD_TOLERANCE:
                below.append(leg_x)
            elif far_y < t.y - COORD_TOLERANCE:
                above.append(leg_x)
    return _SlotFeatures(below, above, spans)


def _trunk_pair_crossings(upper: _SlotFeatures, lower: _SlotFeatures) -> int:
    """Crossings between two trunk slots when *upper* sits above *lower*.

    *upper*'s downward risers cross *lower*'s trunk leg wherever they pass
    through its x-span; *lower*'s upward risers cross *upper*'s leg likewise.
    Risers grazing a span endpoint (a shared corner) are not crossings.
    """

    def _within(x: float, spans: list[tuple[float, float]]) -> bool:
        return any(lo + COORD_TOLERANCE < x < hi - COORD_TOLERANCE for lo, hi in spans)

    return sum(_within(x, lower.spans) for x in upper.below) + sum(
        _within(x, upper.spans) for x in lower.above
    )


def _band_order_crossings(
    order_top_to_bottom: list[list[_HTrunk]],
    feats: dict[int, _SlotFeatures] | None = None,
) -> int:
    """Total riser/leg crossings for a top-to-bottom ordering of trunk slots.

    *feats* optionally supplies each slot's features keyed by ``id(slot)`` so a
    permutation search extracts them once instead of per candidate ordering.
    """
    if feats is None:
        feats = {id(sg): _trunk_slot_features(sg) for sg in order_top_to_bottom}
    return sum(
        _trunk_pair_crossings(
            feats[id(order_top_to_bottom[i])], feats[id(order_top_to_bottom[j])]
        )
        for i in range(len(order_top_to_bottom))
        for j in range(i + 1, len(order_top_to_bottom))
    )


_MAX_BAND_PERMUTE = 6


_SpanOf = dict[int, tuple[float, float]]


def _slot_span(sg: list[_HTrunk]) -> tuple[float, float]:
    """``(x_lo, x_hi)`` envelope of one coincident-Y slot's trunks."""
    return min(t.x_lo for t in sg), max(t.x_hi for t in sg)


def _x_overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Overlapping X extent of two ``(lo, hi)`` spans; 0 when they don't meet.

    A non-zero result means the two spans share a sub-corridor over that many
    px; the ``COORD_TOLERANCE`` floor treats a shared endpoint as no overlap.
    """
    extent = min(a[1], b[1]) - max(a[0], b[0])
    return extent if extent > COORD_TOLERANCE else 0.0


def _pack_band_tracks(order_s2d: list[list[_HTrunk]], span_of: _SpanOf) -> list[int]:
    """Greedy track index per slot for a shallow->deep slot ordering.

    Each slot takes the shallowest track one deeper than every already-placed
    slot it overlaps in X; a slot that shares no sub-corridor with a shallower
    one reuses that shallower track.  The result packs co-travelling trunks
    onto adjacent tracks instead of reserving a fixed concentric depth across
    the whole channel, so a pair sharing one corridor never has an empty track
    wedged between them by trunks that only appear elsewhere in X.
    """
    tracks: list[int] = []
    for i, sg in enumerate(order_s2d):
        span = span_of[id(sg)]
        tr = 0
        for k in range(i):
            if _x_overlap(span_of[id(order_s2d[k])], span):
                tr = max(tr, tracks[k] + 1)
        tracks.append(tr)
    return tracks


def _packed_track_map(
    order: list[list[_HTrunk]], span_of: _SpanOf
) -> tuple[dict[int, int], int]:
    """Track index per slot (keyed by ``id``) and the band's track count.

    *order* is outermost-first (as returned by :func:`_plan_trunk_band`); the
    packing runs in shallow->deep order (its reverse).
    """
    s2d = list(reversed(order))
    tracks = _pack_band_tracks(s2d, span_of)
    track_of = {id(sg): tr for sg, tr in zip(s2d, tracks)}
    return track_of, (max(tracks) + 1 if tracks else 1)


def _band_looseness(
    order_s2d: list[list[_HTrunk]], tracks: list[int], span_of: _SpanOf
) -> float:
    """Total empty-track span between X-overlapping slots, area-weighted.

    For each overlapping slot pair the depth gap beyond one track is weighted
    by the length they co-travel, so an ordering that leaves a wide bundle
    split across a reserved track scores worse than one that packs it tight.
    """
    total = 0.0
    for i in range(len(order_s2d)):
        for j in range(i + 1, len(order_s2d)):
            ov = _x_overlap(span_of[id(order_s2d[i])], span_of[id(order_s2d[j])])
            gap = tracks[j] - tracks[i] - 1
            if ov and gap > 0:
                total += gap * ov
    return total


def _plan_trunk_band(
    band: list[_HTrunk],
) -> tuple[list[list[_HTrunk]], dict[int, int], int]:
    """Order one same-direction band into concentric slots and pack its tracks.

    Returns the outermost-first slot ``order``, a ``{id(slot): track}`` map
    (track 0 = innermost / shallowest), and the band's track count.

    Bundle slots are per distinct LINE, not per trunk: two trunks of the SAME
    line whose X-spans overlap are a fan-out/fan-in of one metro line and
    COINCIDE on one slot (issue #484); distinct lines (and disjoint same-line
    trunks) keep their own concentric slots.

    Slots are ordered to minimise crossings between each slot's peel-off risers
    and the others' trunk legs.  Among orderings tied on crossings the one
    whose greedy track-packing leaves the least empty space between trunks that
    co-travel a shared sub-corridor wins (so disjoint trunks sharing a corridor
    bundle tight instead of being split by a track reserved for a trunk that
    only appears elsewhere in X); among those the widest-reaching slot sorts
    OUTERMOST.  A fully-overlapping bundle packs to one concentric stack whose
    looseness is zero for every order, so the width-only tie-break alone
    decides it.
    """
    slot_groups = _coincident_trunk_slots(band)
    span_of: _SpanOf = {id(sg): _slot_span(sg) for sg in slot_groups}
    heuristic = sorted(
        slot_groups,
        key=lambda sg: (
            -max(t.x_hi - t.x_lo for t in sg),
            min(t.x_lo for t in sg),
            min(t.y for t in sg),
        ),
    )
    if len(slot_groups) < 2 or len(slot_groups) > _MAX_BAND_PERMUTE:
        return heuristic, *_packed_track_map(heuristic, span_of)

    # `_restack_trunk_band` lays slot 0 at the channel-interior extreme: the
    # BOTTOM (largest y) for a downward dip, the TOP for an upward dip.  Score
    # crossings in top-to-bottom space, then convert the winner back to slots.
    dips = band[0].dips_down
    h_ttb = list(reversed(heuristic)) if dips else heuristic
    h_rank = {id(sg): r for r, sg in enumerate(h_ttb)}
    feats = {id(sg): _trunk_slot_features(sg) for sg in slot_groups}

    def _key(perm: list[list[_HTrunk]]) -> tuple[float, ...]:
        # Crossings first; then packed looseness so tight bundles beat split
        # ones; then heuristic position.  The heuristic scores (.., 0, 1, ..),
        # the smallest tuple, so a crossing- and looseness-optimal band keeps
        # the widest-reaching slot outermost.
        s2d = perm if dips else list(reversed(perm))
        looseness = _band_looseness(s2d, _pack_band_tracks(s2d, span_of), span_of)
        return (
            _band_order_crossings(perm, feats),
            looseness,
            *(h_rank[id(sg)] for sg in perm),
        )

    best_ttb = min((list(p) for p in itertools.permutations(h_ttb)), key=_key)
    order = list(reversed(best_ttb)) if dips else best_ttb
    return order, *_packed_track_map(order, span_of)


def _suboptimal_trunk_bands(
    routes: list[RoutedPath], ctx: _RoutingCtx
) -> list[tuple[float, int, int]]:
    """Same-direction inter-row trunk bands whose realized Y order leaves
    avoidable crossings: ``(band y, current crossings, best achievable)``.

    Reconstructs the bands :func:`_materialize_trunk_slots` reorders, then
    checks each realized top-to-bottom order against the crossing-minimal
    permutation.  An empty result means every band is crossing-optimal.
    """
    destination_owned = {
        id(trunk.route)
        for _bundle, trunks, _targets in iter_eligible_destination_tail_bundles(
            routes, ctx.graph, ctx.offset_step, ctx.curve_radius
        )
        for trunk in trunks.values()
    }
    trunks = [
        trunk
        for trunk in _declared_htrunks(routes)
        if id(trunk.route) not in destination_owned
    ]
    if len(trunks) < 2:
        return []
    groups = _group_channel_trunks(trunks, ctx.offset_step)
    out: list[tuple[float, int, int]] = []
    for grp in groups:
        if len({id(t.route) for t in grp}) < 2:
            continue
        if not any(not t.route.normalize_exempt for t in grp):
            continue  # handler-owned all-exempt groups: the planner leaves them
        for sign in (1, -1):
            band = [t for t in grp if t.sign_x == sign]
            slots = _coincident_trunk_slots(band)
            if len(slots) < 2 or len(slots) > _MAX_BAND_PERMUTE:
                continue
            feats = {id(sg): _trunk_slot_features(sg) for sg in slots}
            realized = sorted(slots, key=lambda sg: min(t.y for t in sg))
            cur = _band_order_crossings(realized, feats)
            best = min(
                _band_order_crossings(list(p), feats)
                for p in itertools.permutations(slots)
            )
            if best < cur:
                out.append((min(t.y for t in band), cur, best))
    return out


def _clamp_inter_row_band_top(ctx: _RoutingCtx, top: float, total: float) -> float:
    """Return the top Y at which to stack a *total*-tall direction-band stack.

    A gap whose corridor owns a reserved band is bounded by that band: the
    reservation measured the blockers over the corridor's own span, so it names
    where this stack may sit even when the raw row edges describe a different,
    narrower gap.  The whole stack is held inside it, and a stack taller than
    the band keeps its bottom on the band's far edge.

    Without a reservation the stack starts at the cluster *top* and slides
    upward if its bottom would breach the next row's header clearance
    (``INTER_ROW_HEADER_CLEARANCE`` below the inter-row gap's lower edge),
    keeping the inter-band gap intact rather than crowding the lower band into
    the header.
    """
    reserved = _reserved_gap_band(ctx, top)
    if reserved is not None:
        return min(max(top, reserved.lo), reserved.hi - total)
    band = _inter_row_gap_band(ctx, top)
    if band is None:
        return top
    _gap_top, gap_bottom = band
    limit = gap_bottom - INTER_ROW_HEADER_CLEARANCE
    if top + total > limit:
        return limit - total
    return top


def _reserved_gap_band(ctx: _RoutingCtx, y: float) -> ReservedBand | None:
    """The reservation band for the inter-row gap holding *y*, when claimed."""
    upper = inter_row_gap_upper_row(ctx.graph, y)
    return None if upper is None else ctx.reserved_bands.rows.at(upper + 1)


def _restack_trunk_band(
    order: list[list[_HTrunk]],
    track_of: dict[int, int],
    n: int,
    band_top: float,
    dips: bool,
    step: float,
    ctx: _RoutingCtx,
    bundled: set[int],
) -> None:
    """Fan one planned same-direction band onto its packed tracks.

    The band occupies ``[band_top, band_top + (n-1)*step]`` across *n* tracks;
    *track_of* gives each slot's track (0 = innermost / shallowest).  Slots
    sharing one sub-corridor pack onto adjacent tracks, so a slot present only
    in part of the channel reuses a track left free where it is absent.  All
    trunks here -- including exempt ones grouped with a non-exempt mate -- are
    placed so each co-travelling bundle reads as one tight concentric run.
    """
    for sg in order:
        inner = track_of[id(sg)]  # 0 = innermost (shallowest); sets corner radii
        # Depth from ``band_top`` (the band's smallest Y).  For a downward dip
        # the channel interior is above, so the innermost track sits at the top;
        # for an upward dip the interior is below, so the innermost sits at the
        # bottom -- hence the inner/(n-1-inner) swap.
        depth = inner if dips else n - 1 - inner
        new_y = band_top + depth * step
        for t in sg:
            bundled.add(id(t.route))
            if abs(new_y - t.y) <= COORD_TOLERANCE:
                continue
            _restack_htrunk(t, new_y, inner, n, step, ctx.curve_radius)


def _inter_row_gap_band(ctx: _RoutingCtx, y: float) -> tuple[float, float] | None:
    """Return the ``(top, bottom)`` Y envelope of the inter-row gap holding *y*.

    Scans adjacent grid rows for the gap whose ``[row_bottom, next_row_top]``
    band contains *y*; returns ``None`` when *y* doesn't fall in any gap.
    """
    for _upper, top, bottom in iter_inter_row_gaps(ctx.graph):
        if top - COORD_TOLERANCE <= y <= bottom + COORD_TOLERANCE:
            return top, bottom
    return None


def _htrunk_seg(t: _HTrunk, y: float) -> HTrunkSeg:
    """Build the geometric trunk segment for *t* with its run placed at *y*.

    The flanking risers stay anchored at their outer endpoints and stretch to
    meet the run at *y*, mirroring :func:`_restack_htrunk`, so crossing tests
    can probe a candidate placement before committing to it.
    """
    pts = t.route.points
    k = t.idx
    return HTrunkSeg(y, pts[k][0], pts[k + 1][0], pts[k - 1][1], pts[k + 2][1])


def _dogleg_off_exempt_trunks(
    routes: list[RoutedPath], ctx: _RoutingCtx, skip: set[int] | None = None
) -> None:
    """Offset a non-exempt trunk drawn collinear with an exempt run.

    ``normalize_exempt`` horizontal runs are placed by their own handler and
    are not restacked, so the channel normaliser never sees them, and a
    non-exempt bypass trunk that ends up overlapping one in X with a near-
    equal Y is left fused on top of it.  This pass treats exempt runs as fixed
    occupants and clears the movable trunk off them in two regimes:

    - SAME line: two opposing flows of one metro line fused into a single
      drawn track.  Shifted clear by up to one bundle clearance onto the
      crossing-free side with room, so the two flows read as a dogleg without
      the moved flow crossing the exempt run.
    - DISTINCT line: a different-colour trunk drawn within a sub-bundle gap of
      the exempt run reads as one stroke (the exempt line painted over it).
      Nudged to a full ``OFFSET_STEP`` gap so both colours show as a tight
      concentric bundle.  Distinct trunks already a bundle-gap or more apart
      are a legitimate bundle and left untouched.

    Both regimes clamp inside the inter-row gap, leaving the next row's header
    protrusion clear so the trunk stays in the envelope.
    """
    skip = skip or set()
    obstacles = [
        t
        for t in _collect_htrunks(routes, include_exempt=True)
        if t.route.normalize_exempt and id(t.route) not in skip
    ]
    if not obstacles:
        return
    clearance = EDGE_TO_BUNDLE_CLEARANCE
    for t in _collect_htrunks(routes):
        if id(t.route) in skip or route_system_owns_segment_boundary(t.route, t.idx):
            continue
        hit = next(
            (
                o
                for o in obstacles
                if o.route.line_id == t.route.line_id
                and abs(o.y - t.y) <= clearance
                and t.x_lo < o.x_hi - COORD_TOLERANCE
                and o.x_lo < t.x_hi - COORD_TOLERANCE
            ),
            None,
        )
        if hit is None:
            continue
        # Lower edge reserves the next row's header badge plus the clearance
        # margin the header-clearance invariant requires; up_room only reserves
        # the upper box edge.
        band = _inter_row_gap_band(ctx, t.y)
        if band is not None:
            top, bottom = band
            header_top = bottom - SECTION_HEADER_PROTRUSION
            down_room = (header_top - NEXT_ROW_HEADER_BADGE_CLEARANCE) - hit.y
            up_room = hit.y - top
        else:
            down_room = up_room = clearance
        down = min(clearance, down_room)
        up = min(clearance, up_room)
        min_sep = 2 * ctx.offset_step  # below this the two strokes still fuse
        down_ok = down >= min_sep
        up_ok = up >= min_sep
        down_y, up_y = hit.y + down, hit.y - up
        # Pick the side that keeps the two flows a crossing-free dogleg: moving
        # onto the side whose riser pierces the exempt run (or whose run the
        # exempt riser pierces) trades one fused stroke for a crossing.  Among
        # crossing-equal sides, lean to the side the trunk already sits toward.
        obstacle = _htrunk_seg(hit, hit.y)
        cross_down = trunk_segments_cross(_htrunk_seg(t, down_y), obstacle)
        cross_up = trunk_segments_cross(_htrunk_seg(t, up_y), obstacle)
        prefer_down = t.y >= hit.y
        if down_ok and up_ok and (cross_down is None) != (cross_up is None):
            use_down = cross_down is None
        elif down_ok and (not up_ok or prefer_down):
            use_down = True
        elif up_ok:
            use_down = False
        else:
            continue
        new_y = down_y if use_down else up_y
        _restack_htrunk(t, new_y, 0, 1, ctx.offset_step, ctx.curve_radius)

    step = ctx.offset_step
    for t in _collect_htrunks(routes):
        if id(t.route) in skip or convergence_owns_segment_boundary(t.route, t.idx):
            continue
        hit = next(
            (
                o
                for o in obstacles
                if o.route.line_id != t.route.line_id
                and abs(o.y - t.y) < step - COORD_TOLERANCE
                and t.x_lo < o.x_hi - COORD_TOLERANCE
                and o.x_lo < t.x_hi - COORD_TOLERANCE
            ),
            None,
        )
        if hit is None:
            continue
        band = _inter_row_gap_band(ctx, t.y)
        below, above = hit.y + step, hit.y - step
        if band is not None:
            top, bottom = band
            below_ok = below <= bottom - SECTION_HEADER_PROTRUSION
            above_ok = above >= top
        else:
            below_ok = above_ok = True
        # Pick the side that keeps the trunk a crossing-free parallel bundle:
        # nudging it onto the side whose riser would pierce the exempt run (or
        # whose run the exempt riser would pierce) trades one fused stroke for
        # two crossings.  Among crossing-equal sides, fall back to the side the
        # trunk already leans toward.
        obstacle = _htrunk_seg(hit, hit.y)
        cross_below = trunk_segments_cross(_htrunk_seg(t, below), obstacle)
        cross_above = trunk_segments_cross(_htrunk_seg(t, above), obstacle)
        prefer_below = t.y >= hit.y
        if below_ok and above_ok and (cross_below is None) != (cross_above is None):
            use_below = cross_below is None
        elif below_ok and (not above_ok or prefer_below):
            use_below = True
        elif above_ok:
            use_below = False
        else:
            continue
        _restack_htrunk(t, below if use_below else above, 0, 1, step, ctx.curve_radius)


def _run_enters_section(
    graph: MetroGraph, axis: int, coord: float, span: tuple[float, float]
) -> bool:
    """Whether a run held at *coord* on *axis* over *span* lands inside a section."""
    for section in graph.sections.values():
        edges = (
            (section.bbox_x, section.bbox_x + section.bbox_w),
            (section.bbox_y, section.bbox_y + section.bbox_h),
        )
        held, crossed = edges[axis], edges[1 - axis]
        if (
            held[0] - COORD_TOLERANCE < coord < held[1] + COORD_TOLERANCE
            and span[0] < crossed[1]
            and crossed[0] < span[1]
        ):
            return True
    return False


def _reseat_lane(lane: CorridorLane, coord: float) -> CorridorLane:
    """Move every run on *lane* to *coord*, re-forming its flanking corners.

    Each run keeps its own corner radius as the reference the central concentric
    helper re-derives from the moved waypoints, so the translation preserves the
    concentric family each corner belongs to.
    """
    for run in lane.runs:
        radius_in, radius_out = run.radii
        _reseat_concentric_flanking(
            run.route,
            run.idx,
            coord,
            axis=run.axis,
            base_radius=radius_in,
            base_radius_out=radius_out,
        )
    return replace(
        lane, coord=coord, runs=tuple(replace(run, coord=coord) for run in lane.runs)
    )


def _separate_fused_cotravelling_runs(
    routes: list[RoutedPath], ctx: _RoutingCtx
) -> None:
    """Restore the nesting step between co-travelling tracks of distinct lines.

    A reserved corridor band states the room a corridor is left, not which lane
    inside it the corridor takes.  Several independently-placed corridors
    crossing one boundary are each held in that one band without any of them
    seeing the others, so two can settle within one ``OFFSET_STEP`` of each
    other; two distinct lines that close draw as a single two-tone stripe with
    no separating hairline, hiding one of the two.

    A fused lane travels to the far side of the full step it already leans
    toward, which restores the pitch without reordering the two.  That move can
    crowd the lane's other neighbour, so the neighbour is reconsidered in turn
    and a whole crowded stack re-nests one lane at a time.  Each lane relocates
    at most once, which bounds the cascade.

    A plan-owned track never moves: a plan is what the closing validators check
    the geometry against.  A handler-owned track is considered only after every
    track the normalisation stage owns, so a fusion between the two is resolved
    by moving the stage's own track and the handler keeps its coordinate wherever
    that is enough.  It does still move when nothing else will: a handler placing
    a run has no way to know a later pass would hold another corridor into its
    lane, and a hidden line costs more than one step of drift.  A move that would
    put a track inside a section is abandoned rather than forced; the closing
    ``check_no_fused_cotravelling_lines`` reports whatever is left.

    Every track is read once, before any move.  Moving one shifts the endpoint of
    the runs flanking it, so a neighbouring track's span can go a step stale --
    never enough to change which corridor two tracks share, which is the only
    thing the span is asked.
    """
    step = ctx.offset_step
    lanes = corridor_lanes(
        run for rp in routes if rp.is_inter_section for run in corridor_runs(rp)
    )
    pending = deque(_reseating_order(lanes))
    relocated: set[int] = set()
    while pending:
        i = pending.popleft()
        if i in relocated:
            continue
        lane = lanes[i]
        obstacle = min(
            (other for other in lanes if lane.fuses_with(other, step)),
            key=lambda other: abs(other.coord - lane.coord),
            default=None,
        )
        if obstacle is None or abs(lane.coord - obstacle.coord) <= COORD_TOLERANCE_FINE:
            continue
        target = obstacle.coord + math.copysign(step, lane.coord - obstacle.coord)
        if any(
            _run_enters_section(ctx.graph, run.axis, target, run.span)
            for run in lane.runs
        ):
            continue
        lanes[i] = _reseat_lane(lane, target)
        relocated.add(i)
        pending.extend(
            j
            for j, other in enumerate(lanes)
            if j not in relocated
            and not other.pinned
            and lanes[i].fuses_with(other, step)
        )


def _reseating_order(lanes: list[CorridorLane]) -> list[int]:
    """Indices of the tracks this pass may re-seat, in the order it considers them.

    Tracks the normalisation stage owns come before handler-owned ones, so a
    fusion between the two is resolved by moving the stage's own track and the
    handler's stays where its handler put it.  Within each group the order is by
    axis then coordinate, which makes the outcome independent of route order.
    """
    return sorted(
        (i for i, lane in enumerate(lanes) if not lane.pinned),
        key=lambda i: (lanes[i].handler_owned, lanes[i].axis, lanes[i].coord),
    )


def _coincident_trunk_slots(grp: list[_HTrunk]) -> list[list[_HTrunk]]:
    """Partition one channel group's trunks into coincident-Y slots.

    Trunks carrying the SAME ``line_id`` whose X-spans overlap belong to one
    metro line's shared path (a fan-out or fan-in) and are placed on ONE
    slot so they coincide along their common span, de-duplicating the line
    into a single drawn track that splits only where the spans diverge
    (issue #484).  Every other trunk is its own slot, so distinct lines -
    and disjoint same-line trunks - keep their separate concentric slots.
    """
    slots: list[list[_HTrunk]] = []
    for t in grp:
        for sg in slots:
            if sg[0].route.line_id != t.route.line_id:
                continue
            # Opposing flows of one line are distinct paths, not a fan to merge.
            if sg[0].sign_x != t.sign_x:
                continue
            if any(t.x_lo < o.x_hi and o.x_lo < t.x_hi for o in sg):
                sg.append(t)
                break
        else:
            slots.append([t])
    return slots


def _restack_htrunk(
    t: _HTrunk,
    new_y: float,
    inner: int,
    n: int,
    step: float,
    base_radius: float,
) -> None:
    """Move one horizontal trunk to *new_y* and recompute its flanking radii.

    Shifts both trunk endpoints (which share Y) to *new_y*; the flanking
    vertical legs stretch to meet them.  ``inner`` is the nesting index
    (0 = innermost / shallowest); the two flanking corners are sized so the
    bundle stays concentric, mirroring :func:`_restack_channel`.
    """
    rp = t.route
    pts = rp.points
    k = t.idx
    pts[k] = (pts[k][0], new_y)
    pts[k + 1] = (pts[k + 1][0], new_y)

    if rp.curve_radii is None:
        return
    max_off = (n - 1) * step
    off = inner * step
    # An innermost trunk turns on the INSIDE of both flanking corners (smaller
    # radius), the outermost on the OUTSIDE (larger); same parity on both
    # corners of a dip.  ``off`` grows from 0 at the innermost line, so the
    # radius is base_radius + off (innermost = base_radius, the tightest) --
    # the concentric nesting.  Using the reversed (outside=False) offset here
    # inverts that, giving the inside line the LARGEST radius and tearing the
    # bundle apart at the dip corners.
    r = corner_radius(off, max_off, outside=True, base_radius=base_radius)
    if 0 <= k - 1 < len(rp.curve_radii):
        rp.curve_radii[k - 1] = r
    if k < len(rp.curve_radii) and k + 2 < len(pts):
        rp.curve_radii[k] = r


def _join_fanout_upstream_tails(routes: list[RoutedPath], ctx: _RoutingCtx) -> None:
    """Snap each fan-out junction's upstream tail onto its downstream start.

    The horizontal-handoff member of the same-line coincidence family (see
    :func:`_coincide_same_line_tracks`): where the three group passes fuse
    near-parallel vertical legs of one line, this closes the seam where the
    line hands off horizontally across a fan-out junction.

    At a *fan-out* junction (single upstream source, one or more
    inter-section targets), the incoming ``port -> junction`` route and
    the outgoing ``junction -> target`` route are two separate
    :class:`RoutedPath`\\ s.  Their handoff points at the junction don't
    coincide: the downstream route carries the per-line bundle offset
    (and, for L-shape fans, a curve lead-in that starts a ``curve_radius``
    past the junction), while the upstream route ends at the bare junction
    coordinate.  The mismatch renders as a seam / notch where the two
    segments meet end-to-end instead of one continuous flowing line.

    The downstream start X read here is the value materialisation leaves
    behind, not a routing-time coordinate: the gap- and trunk-slot passes
    shift it after the handlers run, so this fusion cannot be hoisted into
    the handler that routes the upstream tail.

    This pass extends the upstream route's final, horizontal segment so
    it ends at the X of the paired (same ``line_id``) downstream route's
    first waypoint -- closing the horizontal "bite" at the apex that
    otherwise shows as a notch (the downstream L-shape lead-in starts a
    ``curve_radius`` PAST the junction, leaving a gap along the line's
    travel direction between the upstream tail end and the downstream
    curve start).

    The upstream tail's Y is kept unchanged: when the downstream start
    carries a per-line bundle ``offset`` (the inner concentric-corner
    member), the residual PERPENDICULAR offset between the extended
    upstream end and the downstream start is sub-line-width and hidden
    under the stroke.  Lifting the upstream Y to match would either tilt
    the approach or step it, reintroducing a visible kink at the apex, so
    only the X is extended.  Only the upstream tail is moved; the
    downstream geometry is left untouched.

    Gated to genuine single-upstream-source fan-out junctions.  Merge
    junctions (>1 distinct upstream source) are excluded so their trunk
    routing, which intentionally lands branches on a shared bypass Y, is
    never perturbed.
    """
    from nf_metro.layout.route_topology import divergence_junction_sources
    from nf_metro.layout.routing.invariants import _fanout_route_maps

    fanouts = divergence_junction_sources(ctx.graph, ctx.topology)
    if not fanouts:
        return

    upstream, downstream = _fanout_route_maps(routes, fanouts, ctx.graph)
    for (jid, line_id), up in upstream.items():
        down = downstream.get((jid, line_id))
        if down is None or len(up.points) < 2:
            continue
        if (
            down.exit_turn_family_id
            in {
                RouteFamilyId.TOP_ENTRY_L_SHAPE.value,
                RouteFamilyId.BOTTOM_ENTRY_L_SHAPE.value,
            }
            and down.exit_turn_axis_id is not None
        ):
            continue
        p_prev, p_last = up.points[-2], up.points[-1]
        # Only a genuinely-horizontal final segment is extended; extend
        # its X to the downstream start X, keeping the upstream Y so the
        # approach into the bend stays horizontal.
        if abs(p_prev[1] - p_last[1]) <= COORD_TOLERANCE_FINE:
            up.points[-1] = (down.points[0][0], p_last[1])


def _round_junction_perp_peeloff(routes: list[RoutedPath], ctx: _RoutingCtx) -> None:
    """Round a perpendicular branch that peels off a horizontal junction trunk.

    A fan-out junction whose trunk runs horizontally (a feed arriving along one
    side, a sibling branch continuing along the other) can fan one line to a
    TOP/BOTTOM entry port directly below/above it.  That branch drops straight
    off the junction, so the horizontal-to-vertical turn falls exactly on the
    junction where the incoming trunk and this drop are two separate routes:
    neither owns the corner, so it renders as a hard 90 degrees while every
    other direction change curves.

    Prepend a short horizontal lead-in toward the feed side so the drop owns a
    horizontal segment into the turn -- the corner is then a standard within-
    path curve, landing straight down the port column.  The lead-in overlays
    the incoming trunk (same line, same Y), so it never draws in open space; it
    is clamped to the feeder's own length so it cannot overshoot into the
    source section.

    Runs after :func:`_coincide_same_line_tracks` because the drop's column is
    the port X that convergence fusion settles, not a routing-time coordinate.
    """
    from nf_metro.layout.route_topology import divergence_junction_sources

    fanouts = divergence_junction_sources(ctx.graph, ctx.topology)
    if not fanouts:
        return
    graph = ctx.graph
    radius = ctx.curve_radius
    for rp in routes:
        if not rp.is_inter_section or rp.edge.source not in fanouts:
            continue
        peeloff = perp_peeloff_off_horizontal_junction(graph, routes, rp)
        if peeloff is None:
            continue
        junction, feeder, pts = peeloff
        x0, y0 = pts[0]
        side = -1.0 if feeder.x < junction.x else 1.0
        lead_len = min(radius, abs(feeder.x - junction.x))
        rp.points = [(x0 + side * lead_len, y0), *pts]
        rp.curve_radii = None


def _convergence_line_order(
    chans: list[_VChannel], graph: MetroGraph
) -> list[str] | None:
    """Approach order for a bundle converging into one shared LEFT entry port.

    Several inter-section lines ride one bypass trunk and rise (an UP bundle)
    into a common LEFT entry port from two or more source-section columns.
    Their crossing-free order is by approach depth on the trunk - the shallow,
    port-near trunk Y takes the port-near slot - which the fan/divergence
    crossing-minimiser of :func:`_distinct_line_order` has no model for.
    Ordering by realized trunk depth reproduces that stacking, so the risers
    turn into the port concentrically through the standard
    :func:`_restack_channel` path, matching the slots
    :func:`check_peeloff_concentric` enforces.

    Source column is the usual proxy for trunk depth (the nearer source rides
    the shallower trunk), but a hand-authored grid can stagger the trunks
    against their source columns; ordering by the trunk depth the routing
    actually produced keeps the risers nesting whichever way the trunks landed.

    Returns ``None`` for any bundle that is not such a convergence; the
    standard ordering then applies.  Only the cross-source stacking is set
    here: lines from one source keep their standard relative order.
    """
    if not chans or any(ch.down for ch in chans):
        return None
    targets = {ch.route.edge.target for ch in chans}
    if len(targets) != 1:
        return None
    port = graph.ports.get(next(iter(targets)))
    if port is None or not port.is_entry or port.side is not PortSide.LEFT:
        return None
    src_cols: set[int] = set()
    trunk_depth: dict[str, float] = {}
    for ch in chans:
        src = graph.stations.get(ch.route.edge.source)
        col = _resolve_section_col(graph, src) if src else None
        if col is None:
            return None
        src_cols.add(col)
        lid = ch.route.line_id
        trunk_depth[lid] = min(trunk_depth.get(lid, ch.y_hi), ch.y_hi)
    if len(src_cols) < 2:
        return None
    # The risers must peel off ONE shared trunk: their trunk-side Ys (the
    # bottom of each UP leg, ``y_hi``) cluster within one bundle width.  A
    # cross-row fan-in whose legs start rows apart is a divergence the standard
    # crossing-minimiser orders, not a single-trunk convergence.
    trunk_ys = [ch.y_hi for ch in chans]
    offset_step = graph_offset_step(graph)
    if not trunk_depths_contiguous(trunk_ys, len(src_cols), offset_step):
        return None
    return sorted(_distinct_line_order(chans), key=lambda lid: trunk_depth[lid])


def _corridor_leadout_right(chans: list[_VChannel], default: bool) -> bool:
    """Whether a rigid bundle's deep-end horizontal legs extend rightward.

    Each channel meets the inter-section trunk at its deep (``y_hi``) endpoint;
    the leg there runs toward the trunk's continuation, so its travel direction
    is what the deep-end crossing model must mirror.  A rightward bypass's DOWN
    descent leads its trunk RIGHT and its UP ascent receives it from the LEFT; a
    leftward bypass mirrors both, which a plain ``down`` discriminant cannot see.

    Only a rigid bundle -- every channel one line of a single ``(source,
    target)`` edge -- has such a trunk; a junction fan-out or merge corridor
    diverges to different ends at its deep endpoints, where those legs are the
    peel-offs the ``down``-keyed crossing model already orders, so they keep the
    *default*.  Within a rigid bundle, returns the majority rightward verdict
    over channels that have a flanking horizontal, falling back to *default*
    when none do (a bare vertical drop carries no direction).
    """
    if len({(c.route.edge.source, c.route.edge.target) for c in chans}) != 1:
        return default
    votes: list[bool] = []
    for ch in chans:
        pts = ch.route.points
        deep, far = (ch.idx + 1, ch.idx + 2) if ch.down else (ch.idx, ch.idx - 1)
        if not 0 <= far < len(pts):
            continue
        dx = pts[far][0] - pts[deep][0]
        if abs(dx) > COORD_TOLERANCE:
            votes.append(dx > 0)
    if not votes:
        return default
    return votes.count(True) >= votes.count(False)


def _distinct_line_order(chans: list[_VChannel]) -> list[str]:
    """Left-to-right order of the distinct lines in one gap-bundle corridor.

    Channels sharing a ``line_id`` collapse to a single slot, so the order
    is over distinct lines.  The ordering minimises crossings between each
    line's vertical leg and the others' horizontal lead-outs.

    A line's vertical leg spans the gap from the shared trunk level (near
    the junction) down to its deepest turn-off; each channel segment turns
    off horizontally at its deep endpoint (``y_hi``).  For a DOWN bundle
    that lead-out extends RIGHTWARD toward the target; for an UP bundle the
    lead-in extends LEFTWARD from the source.  When line A sits LEFT of B:

    * DOWN: B's (right-placed, deeper) vertical crosses each A lead-out that
      turns off shallower than B's deepest point.
    * UP: A's (left-placed, deeper) vertical crosses each B lead-in (which
      extends left under A) that attaches shallower than A's deepest point.

    The pairwise comparator picks, for each pair, the side incurring fewer
    crossings; ties keep the incoming x order.  This places a deep bypass
    before a shallow neighbour (variant_calling: qc before main) yet still
    puts a shallow long-reach line before a deeper multi-target fan when
    that strictly reduces crossings (genomeassembly: hic before assemblies),
    and mirrors the rule for UP bundles (subworkflows: the deeper
    preprocess_reporting sits to the RIGHT).
    """
    down = chans[0].down if chans else True
    # The deep-end crossing model nests by the trunk's travel direction, not the
    # vertical's: a leftward bypass leads its DOWN trunk LEFT, so the rightward
    # default (``down``) would mirror the order and twist the bundle at the
    # corner.  Read the leg direction from geometry; fall back to ``down`` for a
    # bare drop with no flanking horizontal.
    lead_right = _corridor_leadout_right(chans, down)

    # Per line: the deep turn-off depths of each segment (always y_hi), the
    # deepest reach, a representative x for stable tie-breaking, and the
    # source-side approach Y of each segment (y_lo for a DOWN bundle, y_hi for
    # UP) plus the line's vertical-span extremes for the approach-side test.
    turns: dict[str, list[float]] = defaultdict(list)
    deepest: dict[str, float] = {}
    rep_x: dict[str, float] = {}
    approach: dict[str, list[float]] = defaultdict(list)
    span_lo: dict[str, float] = {}
    span_hi: dict[str, float] = {}
    for ch in chans:
        lid = ch.route.line_id
        turns[lid].append(ch.y_hi)
        deepest[lid] = max(deepest.get(lid, ch.y_hi), ch.y_hi)
        rep_x[lid] = min(rep_x.get(lid, ch.x), ch.x)
        approach[lid].append(ch.y_lo if down else ch.y_hi)
        span_lo[lid] = min(span_lo.get(lid, ch.y_lo), ch.y_lo)
        span_hi[lid] = max(span_hi.get(lid, ch.y_hi), ch.y_hi)

    def peel_crossings_if_left(a: str, b: str) -> int:
        # Deep-end (divergence) crossings when a is placed LEFT of b.
        if lead_right:
            # b's deeper vertical crosses a's shallower right-going lead-outs.
            return sum(1 for t in turns[a] if t < deepest[b] - COORD_TOLERANCE)
        # Leftward: a's deeper vertical crosses b's shallower lead-outs.
        return sum(1 for t in turns[b] if t < deepest[a] - COORD_TOLERANCE)

    # The approach-weave term models a fan whose lead-ins enter from the LEFT
    # and descend rightward (a bypass overtaking its down-turns on the right).
    # A leftward-descending fan (source to the right of its channels) is the
    # mirror image, where the deep-end ordering already nests the lines; the
    # rightward-only term would mis-order it, so restrict it to fans whose
    # source sits left of every descent channel.
    fan_rightward = all(
        ch.route.points and ch.route.points[0][0] <= ch.x + COORD_TOLERANCE
        for ch in chans
    )

    def approach_crossings_if_left(a: str, b: str) -> int:
        # Source-end (fan) crossings when a is placed LEFT of b: the RIGHT
        # line's lead-in, extending from the shared junction past the LEFT
        # line's vertical, pierces that vertical's span.  This is the weave a
        # bypass makes when it descends on the far side of the fan but
        # approaches from the bundle's near side; ordering it out avoids the
        # tangle the deep-end-only test cannot see.
        if not fan_rightward:
            return 0
        right = b  # a is LEFT, so b sits to the RIGHT
        lo, hi = span_lo[a], span_hi[a]
        return sum(
            1
            for y in approach[right]
            if lo + COORD_TOLERANCE < y < hi - COORD_TOLERANCE
        )

    def cmp(a: str, b: str) -> int:
        # Avoid fan-side weaves first, then deep-end divergence crossings: a
        # crossover at the divergence reads as one clean fork, while a weave
        # at the fan reads as a tangle.
        aa = approach_crossings_if_left(a, b)
        ab = approach_crossings_if_left(b, a)
        if aa != ab:
            return -1 if aa < ab else 1
        ca = peel_crossings_if_left(a, b)
        cb = peel_crossings_if_left(b, a)
        if ca != cb:
            return -1 if ca < cb else 1
        if rep_x[a] != rep_x[b]:
            return -1 if rep_x[a] < rep_x[b] else 1
        return -1 if a < b else (1 if a > b else 0)

    return sorted(turns, key=functools.cmp_to_key(cmp))


def _restack_channel(
    ch: _VChannel,
    new_x: float,
    i: int,
    n: int,
    step: float,
    base_radius: float,
) -> None:
    """Move one vertical channel to *new_x* and recompute its corner radii.

    Shifts the channel's two endpoints (which share x) to *new_x*; the
    flanking horizontal segments stretch.  The re-stacked channel behaves
    exactly like line *i* of an *n*-line standard L-shape. The bundle is
    ordered left-to-right with ``i`` growing rightward, so each corner can find
    its innermost rank directly from its final travel directions and derive the
    radius through :func:`concentric_corner_radius_at`.
    """
    rp = ch.route
    pts = rp.points
    k = ch.idx
    pts[k] = (new_x, pts[k][1])
    pts[k + 1] = (new_x, pts[k + 1][1])

    if rp.curve_radii is None:
        return
    for radius_idx in (k - 1, k):
        if not 0 <= radius_idx < len(rp.curve_radii):
            continue
        prev, corner, nxt = pts[radius_idx : radius_idx + 3]
        if abs(corner[0] - prev[0]) > COORD_TOLERANCE:
            ux = -1.0 if corner[0] > prev[0] else 1.0
        else:
            ux = 1.0 if nxt[0] > corner[0] else -1.0
        inner_rank = n - 1 if ux > 0 else 0
        dx = (i - inner_rank) * step
        rp.curve_radii[radius_idx] = concentric_corner_radius_at(
            prev, corner, nxt, dx, base_radius
        )
    r_first = rp.curve_radii[k - 1] if 0 <= k - 1 < len(rp.curve_radii) else base_radius

    # Unclamp the source-side fan lead-in.  When this channel's lead-in is the
    # route's first segment (a concentric fan corner hugging the junction), it
    # is usually shorter than the outer members' r_first, so resolve_curve_radii
    # clamps the radius down to the lead length and the bundle loses its
    # concentric (shared-centre) spacing.  Extend the lead start back along its
    # own axis so the full r_first fits; the extra length overlaps the upstream
    # same-line tail (re-joined by _join_fanout_upstream_tails), so it is free.
    if k == 1:
        lx, ly = pts[0]
        if abs(ly - pts[1][1]) < COORD_TOLERANCE:
            if lx <= new_x:  # lead approaches from the left (R-going fan)
                pts[0] = (min(lx, new_x - r_first), ly)
            else:  # lead approaches from the right (L-going fan)
                pts[0] = (max(lx, new_x + r_first), ly)


def _gap_channel_base(
    graph: MetroGraph,
    lo: int,
    row: int | None,
    n: int,
    offset_step: float,
    anchor_section_id: str | None = None,
    anchor_side: PortSide | None = None,
) -> float:
    """Centred midline x for a bundle of *n* lines in gap ``(lo, lo+1)``.

    This is only the initial placement during routing; the post-routing
    :func:`_materialize_gap_slots` pass re-stacks every inter-section
    channel into its final centred / B-separated position, so the value
    here just needs to land the channel in the right gap.

    When *anchor_section_id* and *anchor_side* are given, a packed cell-mate
    of that section on *anchor_side* (see :func:`packed_cell_neighbor_edges`)
    takes priority over the column-level gap: the column edge can sit on the
    far side of a cell-mate, well past the section the channel is meant to
    hug.
    """
    edges = None
    if anchor_section_id is not None and anchor_side is not None:
        edges = packed_cell_neighbor_edges(graph, anchor_section_id, anchor_side)
    gap_left, gap_right = edges or column_gap_edges(
        graph, lo, lo + 1, row=row, require_both_columns=False
    )
    return symmetric_bundle_midpoint(
        gap_left, gap_right, [max(0, n - 1) * offset_step], 0
    )


def _clear_channel_x_in_band(
    graph: MetroGraph,
    x: float,
    y_lo: float,
    y_hi: float,
    clearance: float,
    exclude_section_ids: set[str],
    bound_left: float | None = None,
    bound_right: float | None = None,
) -> float:
    """Nudge a vertical channel *x* clear of every section its Y-band pierces.

    A bypass channel placed in the source row's column gap can still pierce
    an oversized section in another row that the descent crosses (its bbox
    extends past the source-row gap edges).  Scan all sections whose bbox
    overlaps the open vertical interval ``(y_lo, y_hi)``; if *x* sits inside
    one, shift it to the nearer cleared edge (``bbox_x - clearance`` or
    ``bbox_x + bbox_w + clearance``).  Iterate so a single shift that lands
    inside an adjacent box is resolved.  ``bound_left`` / ``bound_right``
    cap the search so the channel never leaves the inter-column gap; when a
    clear position can't be found within the bounds the original *x* is
    returned (the normalization pass / overlap guards remain the backstop).
    """
    lo_y, hi_y = (y_lo, y_hi) if y_lo <= y_hi else (y_hi, y_lo)
    for _ in range(8):
        blocker = None
        for s in graph.sections.values():
            if s.bbox_w <= 0 or s.id in exclude_section_ids:
                continue
            sx_l = s.bbox_x
            sx_r = s.bbox_x + s.bbox_w
            if not (sx_l - clearance < x < sx_r + clearance):
                continue
            if lo_y < s.bbox_y + s.bbox_h and s.bbox_y < hi_y:
                blocker = (sx_l, sx_r)
                break
        if blocker is None:
            return x
        sx_l, sx_r = blocker
        left_x = sx_l - clearance
        right_x = sx_r + clearance
        left_ok = bound_left is None or left_x >= bound_left
        right_ok = bound_right is None or right_x <= bound_right
        if left_ok and (not right_ok or abs(left_x - x) <= abs(right_x - x)):
            x = left_x
        elif right_ok:
            x = right_x
        else:
            return x
    return x


def _h_segment_crosses_other_section(
    graph: MetroGraph,
    x1: float,
    x2: float,
    y: float,
    exclude_section_ids: set[str],
    margin: float = 0.0,
) -> bool:
    """Return True if a horizontal segment at *y* crosses any section interior.

    Sections listed in *exclude_section_ids* are skipped entirely.  All
    other sections are tested against the segment's open interior via
    :func:`_h_segment_penetrates_section`.  The horizontal segment runs from
    ``min(x1, x2)`` to ``max(x1, x2)``.
    """
    lo_x, hi_x = (x1, x2) if x1 <= x2 else (x2, x1)
    for s in graph.sections.values():
        if s.id in exclude_section_ids:
            continue
        if _h_segment_penetrates_section(lo_x, hi_x, y, s, margin):
            return True
    return False


def _v_segment_crosses_other_section(
    graph: MetroGraph,
    x: float,
    y1: float,
    y2: float,
    exclude_section_ids: set[str],
    margin: float = 0.0,
) -> bool:
    """Return True if a vertical segment at *x* crosses any section interior.

    The vertical mirror of :func:`_h_segment_crosses_other_section`: sections
    in *exclude_section_ids* are skipped, all others are tested against their
    open interior.  The segment runs from ``min(y1, y2)`` to ``max(y1, y2)``;
    a section is crossed when the segment penetrates its open Y interior while
    *x* falls within ``[bbox_x - margin, bbox_x + bbox_w + margin]``.
    """
    lo_y, hi_y = (y1, y2) if y1 <= y2 else (y2, y1)
    for s in graph.sections.values():
        if s.bbox_w <= 0:
            continue
        if s.id in exclude_section_ids:
            continue
        bottom = s.bbox_y + s.bbox_h
        if hi_y <= s.bbox_y or lo_y >= bottom:
            continue
        if s.bbox_x - margin <= x <= s.bbox_x + s.bbox_w + margin:
            return True
    return False


class _CorridorRun(NamedTuple):
    """One straight leg of a route, and the band its corridor leaves it.

    ``lo``/``hi`` are ``None`` for a leg that may not be reseated -- one whose
    coordinate a plan owns, whose flanks would break, or whose boundary offers no
    coordinate satisfying both clearances -- and for a leg in no gap at all.  It
    takes part all the same: it occupies a stretch of its corridor, and so bounds
    where the legs beside it may sit.

    ``forward`` is whether the leg travels toward increasing coordinates, which
    with its line identity is what says how much room it and a neighbour need.
    """

    route: RoutedPath
    idx: int
    axis: int
    coordinate: float
    run_lo: float
    run_hi: float
    lo: float | None
    hi: float | None
    forward: bool

    @property
    def movable(self) -> bool:
        return self.lo is not None and self.hi is not None


def _iter_axis_aligned_legs(
    rp: RoutedPath,
) -> Iterator[tuple[int, int, float, float, float, bool]]:
    """``(idx, axis, coordinate, run_start, run_end, turning)`` for each straight leg.

    Every axis-aligned leg is yielded: a leg that may not move occupies its
    corridor all the same, and so bounds where the legs around it may sit.
    *axis* is the leg's own coordinate index (``1`` for a horizontal leg's Y,
    ``0`` for a vertical leg's X).

    *turning* says the leg is interior and both its flanking legs run
    perpendicular to it, which together are the condition for moving its
    coordinate: the flanks then stretch along their own length and stay
    straight.  A leg flanked by a diagonal would break that diagonal's angle,
    and an end leg is attached to a port or station marker, so neither may
    move.
    """
    pts = rp.points
    for k in range(len(pts) - 1):
        x0, y0 = pts[k]
        x1, y1 = pts[k + 1]
        along_x = abs(x1 - x0) > COORD_TOLERANCE
        along_y = abs(y1 - y0) > COORD_TOLERANCE
        if along_x is along_y:
            continue
        interior = 1 <= k <= len(pts) - 3
        if along_x:
            turning = interior and (
                abs(pts[k - 1][0] - x0) <= COORD_TOLERANCE
                and abs(pts[k + 2][0] - x1) <= COORD_TOLERANCE
            )
            yield k, 1, y0, x0, x1, turning
        else:
            turning = interior and (
                abs(pts[k - 1][1] - y0) <= COORD_TOLERANCE
                and abs(pts[k + 2][1] - y1) <= COORD_TOLERANCE
            )
            yield k, 0, x0, y0, y1, turning


def _route_endpoint_section_ids(graph: MetroGraph, rp: RoutedPath) -> tuple[str, ...]:
    """The sections this route runs between, which span its corridor claims."""
    return tuple(
        station.section_id
        for station_id in (rp.edge.source, rp.edge.target)
        if (station := graph.stations.get(station_id)) is not None
        and station.section_id in graph.sections
    )


def _corridor_run_band(
    ctx: _RoutingCtx,
    route: RoutedPath,
    idx: int,
    axis: int,
    section_ids: tuple[str, ...],
    coordinate: float,
    run_lo: float,
    run_hi: float,
) -> tuple[float, float] | None:
    """The band this leg must be held inside, or ``None`` where it may not move.

    A leg the ledger claims is held inside *its own claim's* realised band --
    the identity the closing guard scores it against -- so containment consumes
    the reservation rather than confirming wherever the leg already sits.  That
    is the whole point of a reservation: settlement widened the boundary for
    this corridor over the corridor's own declared span, and reading the band
    back off live geometry would discard that allocation and re-derive whatever
    the drawn coordinate happens to fall inside.

    A leg no claim names has no reservation to consume, and its clearance is
    measured from the gap it runs in
    (:func:`~nf_metro.layout.routing.reserved_bands.corridor_clearance_band`).
    That covers the first routing pass, which publishes the ledger and so has
    none to read, and unclaimed geometry on the re-route.
    """
    touches_planned_geometry = any(
        planner_owns_segment(route, rank)
        for rank in (idx - 1, idx, idx + 1)
        if 0 <= rank < len(route.points) - 1
    )
    if not section_ids or touches_planned_geometry:
        return None
    claimed = _segment_claim_band(ctx, route, idx)
    if claimed is not None:
        return claimed.lo, claimed.hi
    band = corridor_clearance_band(
        ctx.graph,
        axis=axis,
        section_ids=section_ids,
        coordinate=coordinate,
        run_start=run_lo,
        run_end=run_hi,
    )
    return None if band is None else (band.lo, band.hi)


def _corridor_runs(routes: list[RoutedPath], ctx: _RoutingCtx) -> list[_CorridorRun]:
    """Every straight leg of every inter-section route, with the band it may hold.

    Legs outside any gap, and legs nothing may reseat, are collected without a
    band: the bundles are read off the drawn geometry, so a leg left out of the
    reading is one whose mates could be moved off it.
    """
    graph = ctx.graph
    out: list[_CorridorRun] = []
    for rp in routes:
        if not rp.is_inter_section:
            continue
        section_ids = _route_endpoint_section_ids(graph, rp)
        for idx, axis, coordinate, start, end, turning in _iter_axis_aligned_legs(rp):
            run_lo, run_hi = min(start, end), max(start, end)
            band = (
                _corridor_run_band(
                    ctx, rp, idx, axis, section_ids, coordinate, run_lo, run_hi
                )
                if turning
                else None
            )
            out.append(
                _CorridorRun(
                    rp,
                    idx,
                    axis,
                    coordinate,
                    run_lo,
                    run_hi,
                    band[0] if band is not None else None,
                    band[1] if band is not None else None,
                    end >= start,
                )
            )
    return out


def _co_travel_reach(step: float) -> float:
    """The furthest apart two legs may sit and still read as one bundle.

    Stated once because two callers depend on it agreeing: the predicate that
    decides whether a pair co-travels, and the ordered scan that stops looking
    once no later leg can reach the one it is testing.  A scan that stopped
    earlier than the predicate accepts would leave pairs it would have grouped
    untested, and they would move independently and break the relationship the
    grouping exists to hold.
    """
    return max(step, BUNDLE_TO_BUNDLE_CLEARANCE) + COORD_TOLERANCE


def _legs_co_travel(first: _CorridorRun, second: _CorridorRun, step: float) -> bool:
    """Whether two legs are drawn as one bundle of co-travelling runs.

    Legs within one bundle-to-bundle clearance of each other over a shared
    stretch of their corridor are what a reader sees as a single bundle: fused
    same-line tracks sit on one coordinate, nested distinct lines sit a *step*
    apart, and separate bundles keep :data:`BUNDLE_TO_BUNDLE_CLEARANCE` between
    them.  Anything closer than that clearance is one group, and every one of
    those relationships is destroyed by moving one member alone.

    Sharing a stretch is :func:`spans_share_corridor`, the same reading
    :func:`_crowding_windows` measures a peer's claim on the gap by.  Two legs
    meeting only across the elbow band that joins them occupy different parts of
    the corridor and owe each other no clearance, so there is no separation
    between them for a rigid group to preserve -- and holding them together
    instead denies each of them the band its own claim was widened for.
    """
    return abs(first.coordinate - second.coordinate) <= _co_travel_reach(step) and (
        spans_share_corridor(first.run_lo, first.run_hi, second.run_lo, second.run_hi)
    )


def _corridor_bundles(
    runs: list[_CorridorRun], step: float
) -> list[list[_CorridorRun]]:
    """Group one axis's legs into the bundles that have to travel together.

    Bundles are the connected components of :func:`_legs_co_travel`, so a bundle
    holds every leg transitively co-travelling with any of its members and moves
    rigidly or not at all.  Grouping on geometry rather than on which boundary
    each leg was assigned is what keeps two fused tracks together when they were
    measured against different boundaries.
    """
    ordered = sorted(runs, key=lambda item: item.coordinate)
    owner = list(range(len(ordered)))

    def root(index: int) -> int:
        while owner[index] != index:
            owner[index] = owner[owner[index]]
            index = owner[index]
        return index

    for i, first in enumerate(ordered):
        for j in range(i + 1, len(ordered)):
            second = ordered[j]
            if second.coordinate - first.coordinate > _co_travel_reach(step):
                break
            if _legs_co_travel(first, second, step):
                owner[root(j)] = root(i)
    grouped: dict[int, list[_CorridorRun]] = {}
    for index, run in enumerate(ordered):
        grouped.setdefault(root(index), []).append(run)
    return sorted(
        grouped.values(), key=lambda bundle: min(run.coordinate for run in bundle)
    )


def _bundle_shift_range(bundle: list[_CorridorRun]) -> tuple[float, float] | None:
    """How far *bundle* may travel with every member inside its own band.

    ``None`` where a member may not move at all: a bundle keeps the spacing that
    draws its members as separate strokes, so one pinned member pins all of them.
    ``None`` too where no single shift satisfies every member, which is a boundary
    narrower than the clearances its corridors ask of it, or two lanes each owed
    one coordinate.  Such a bundle stands where the passes above put it, and the
    shortfall is left to the closing guard to report: seating it against either
    edge picks which blocker to crowd, and the one a later widening of the
    boundary would repair is not the one a reader would forgive -- the positive
    side of a row gap is the next row's title badge.
    """
    if any(not run.movable for run in bundle):
        return None
    lower = max(run.lo - run.coordinate for run in bundle)  # type: ignore[operator]
    upper = min(run.hi - run.coordinate for run in bundle)  # type: ignore[operator]
    if lower > upper + COORD_TOLERANCE_FINE:
        return None
    return lower, upper


def _hold_runs_in_corridor_clearance(
    routes: list[RoutedPath], ctx: _RoutingCtx
) -> None:
    """Hold every gap-crossing run inside the clearance its corridor owes.

    A leg drawn in a row or column gap earns a
    :class:`~nf_metro.layout.route_reservations.RouteReservation` over that
    boundary, and that reservation's band is measurable from live geometry alone
    (:func:`~nf_metro.layout.routing.reserved_bands.corridor_clearance_band`).
    Handlers and the passes above derive a channel's depth from whichever grid
    edges they have to hand, which over-states the obstruction wherever a
    section spans the boundary or sits outside the corridor's run.  This closes
    that difference on the drawn geometry, so a corridor is contained by the
    reservation raised over it on the pass that publishes the ledger as well as
    the pass that reads it.

    A bundle travels only through the shifts its peers leave it, each of them
    taken at its settled position, so no move crowds another lane or leapfrogs
    it into a different order.  Bundles are visited in coordinate order, which is
    what makes an already-moved peer a settled obstacle for the rest.

    A bundle every shift is denied to retries with the peers denying it, as one
    rigid group: two corridors owed one boundary between them are seated by the
    same widening and neither can reach it alone, so allocating them together is
    the whole of what makes that widening usable.  Moving a group rigidly leaves
    every separation inside it exactly as drawn, so the joint move can neither
    fuse two lanes nor reorder them.
    """
    by_axis: defaultdict[int, list[_CorridorRun]] = defaultdict(list)
    for run in _corridor_runs(routes, ctx):
        by_axis[run.axis].append(run)
    for runs in by_axis.values():
        bundles = _corridor_bundles(runs, ctx.offset_step)
        settled = [0.0] * len(bundles)
        for index in range(len(bundles)):
            group = _seatable_group(bundles, settled, index, ctx)
            if group is None:
                continue
            members, shift = group
            for other in members:
                _shift_corridor_bundle(bundles[other], shift, ctx)
                settled[other] += shift


def _remaining_shift_range(
    bundle: list[_CorridorRun], applied: float
) -> tuple[float, float] | None:
    """How much further *bundle* may travel, having already moved by *applied*.

    A run's recorded coordinate is where the passes above left it, so a bundle's
    band-derived range is measured from there; what is left of it is that range
    less the distance already taken.
    """
    allowed = _bundle_shift_range(bundle)
    return None if allowed is None else (allowed[0] - applied, allowed[1] - applied)


def _group_shift(
    bundles: Sequence[list[_CorridorRun]],
    settled: Sequence[float],
    members: Collection[int],
    ctx: _RoutingCtx,
) -> float | None:
    """The least further shift seating every bundle in *members* in its own band.

    The group moves rigidly, so one shift has to satisfy what every member has
    left of its own range and clear every peer outside the group.  Every member
    has a range: the caller checks its own before asking, and
    :func:`_denying_bundles` reports only bundles that can move.
    """
    ranges = [
        _remaining_shift_range(bundles[index], settled[index]) for index in members
    ]
    lower = max(item[0] for item in ranges if item is not None)
    upper = min(item[1] for item in ranges if item is not None)
    if lower > upper + COORD_TOLERANCE_FINE:
        return None
    group = [(run, settled[index]) for index in members for run in bundles[index]]
    peers = [
        (peer, settled[other])
        for other, bundle in enumerate(bundles)
        if other not in members
        for peer in bundle
    ]
    return _least_uncrowded_shift(lower, upper, _crowding_windows(group, peers, ctx))


def _seatable_group(
    bundles: Sequence[list[_CorridorRun]],
    settled: Sequence[float],
    index: int,
    ctx: _RoutingCtx,
) -> tuple[frozenset[int], float] | None:
    """The bundles to move with *index*, and the shift that seats them all.

    ``None`` where the bundle is already where its band wants it, or where no
    group reachable from it has a seating: the shortfall is then left to the
    closing guard to report rather than paid for by crowding a lane.
    """
    if _remaining_shift_range(bundles[index], settled[index]) is None:
        return None
    members = frozenset({index})
    shift = _group_shift(bundles, settled, members, ctx)
    if shift is None:
        blockers = _denying_bundles(bundles, settled, index, ctx)
        if not blockers:
            return None
        members = frozenset({index, *blockers})
        shift = _group_shift(bundles, settled, members, ctx)
    if shift is None or abs(shift) <= COORD_TOLERANCE_FINE:
        return None
    return members, shift


def _denying_bundles(
    bundles: Sequence[list[_CorridorRun]],
    settled: Sequence[float],
    index: int,
    ctx: _RoutingCtx,
) -> frozenset[int]:
    """The bundles that deny *index* every shift its own band asks for.

    Only bundles that can themselves move are reported.  One that cannot is
    denying *index* from a coordinate nothing may change, so taking it into the
    group could not free the shift it denies; it stays a peer, and its window
    keeps denying from there.
    """
    remaining = _remaining_shift_range(bundles[index], settled[index])
    assert remaining is not None
    lower, upper = remaining
    mine = [(run, settled[index]) for run in bundles[index]]
    return frozenset(
        other
        for other, bundle in enumerate(bundles)
        if other != index
        and _remaining_shift_range(bundle, settled[other]) is not None
        and _least_uncrowded_shift(
            lower,
            upper,
            _crowding_windows(mine, [(peer, settled[other]) for peer in bundle], ctx),
        )
        is None
    )


class _CrowdingWindow(NamedTuple):
    """The open range of shifts one peer leg denies a bundle.

    The range reaches away past its peer, so it denies crowding the peer's lane
    and leapfrogging it into another order alike; the finite end is the closest
    the two may settle, and is itself allowed.
    """

    lo: float
    hi: float

    def excludes(self, shift: float) -> bool:
        return self.lo < shift < self.hi

    @property
    def bound(self) -> float:
        return self.hi if isfinite(self.hi) else self.lo


def _crowding_windows(
    bundle: Sequence[tuple[_CorridorRun, float]],
    peers: Sequence[tuple[_CorridorRun, float]],
    ctx: _RoutingCtx,
) -> list[_CrowdingWindow]:
    """One window per pair of legs that would crowd each other, over all *peers*.

    A peer constrains only the legs it shares a stretch of corridor with: legs
    that merely pass each other's ends occupy different parts of the gap.  How
    close a pair may settle is :func:`cotravelling_lane_clearance`, which is the
    one statement of that rule the reservation ledger also sizes its boundaries
    by, so a corridor is never denied a coordinate the ledger allocated it on a
    separation the ledger did not charge for.  A pair whose clearance is nothing
    may close right up: two tracks of one line travelling the same way are one
    stroke by construction, and the window then stops the bundle at its peer's
    lane rather than short of it, which is what keeps the drawn order.

    Both sides carry the shift already applied to them, since a lane is where it
    has settled rather than where the passes above left it, and the windows are
    in terms of the *further* shift the bundle may take from there.
    """
    windows: list[_CrowdingWindow] = []
    for run, moved in bundle:
        for peer, applied in peers:
            if not spans_share_corridor(
                run.run_lo, run.run_hi, peer.run_lo, peer.run_hi
            ):
                continue
            keep = cotravelling_lane_clearance(
                same_line=peer.route.edge.line_id == run.route.edge.line_id,
                counter_running=peer.forward is not run.forward,
                curve_radius=ctx.curve_radius,
            )
            onto = (peer.coordinate + applied) - (run.coordinate + moved)
            windows.append(
                _CrowdingWindow(-inf, onto + keep)
                if onto < 0
                else _CrowdingWindow(onto - keep, inf)
            )
    return windows


def _least_uncrowded_shift(
    lower: float, upper: float, windows: Sequence[_CrowdingWindow]
) -> float | None:
    """The smallest shift in ``[lower, upper]`` no window in *windows* denies.

    ``None`` where every shift the band asks for is denied: the bundle then holds
    its place, and the shortfall is left to the closing guard to report rather
    than paid for by drawing two lanes as one.  The feasible shifts form a closed
    set whose extremes are the band's own ends, the windows' finite bounds and the
    least move the band asks for, so the smallest of those is the smallest of all.
    """
    candidates = {min(max(0.0, lower), upper), lower, upper}
    candidates.update(window.bound for window in windows)
    return min(
        (
            candidate
            for candidate in sorted(candidates)
            if lower - COORD_TOLERANCE_FINE <= candidate <= upper + COORD_TOLERANCE_FINE
            and not any(window.excludes(candidate) for window in windows)
        ),
        key=abs,
        default=None,
    )


def _shift_corridor_bundle(
    bundle: list[_CorridorRun], shift: float, ctx: _RoutingCtx
) -> None:
    """Translate one bundle of co-travelling legs by *shift*.

    A leg's flanking corners are re-derived against the radius each already
    carries as their reference, which is what keeps a concentric loop -- every
    corner sized as one family -- a single family across the move.  Taking the
    base radius instead would pinch two corners of such a loop to a radius the
    rest of the loop does not share.
    """
    for run in bundle:
        radii = run.route.curve_radii
        _reseat_concentric_flanking(
            run.route,
            run.idx,
            run.coordinate + shift,
            axis=run.axis,
            base_radius=_held_corner_radius(radii, run.idx - 1, ctx.curve_radius),
            base_radius_out=_held_corner_radius(radii, run.idx, ctx.curve_radius),
        )


def _held_corner_radius(
    radii: list[float] | None, index: int, fallback: float
) -> float:
    """The radius already drawn at *index*, or *fallback* where there is none."""
    return radii[index] if radii and 0 <= index < len(radii) else fallback
