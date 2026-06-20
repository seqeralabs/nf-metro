"""Channel and trunk normalization passes run after edge routing."""

from __future__ import annotations

import functools
import itertools
from collections import defaultdict
from dataclasses import dataclass
from typing import NamedTuple

from nf_metro.layout.constants import (
    BUNDLE_TO_BUNDLE_CLEARANCE,
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    EDGE_TO_BUNDLE_CLEARANCE,
    INTER_ROW_HEADER_CLEARANCE,
    MIN_CORRIDOR_Y_OVERLAP,
    NEXT_ROW_HEADER_BADGE_CLEARANCE,
    OFFSET_STEP,
    SECTION_HEADER_PROTRUSION,
)
from nf_metro.layout.routing.common import (
    Direction,
    GapSlot,
    HTrunkSeg,
    RoutedPath,
    _grid_row_bands,
    column_gap_edges,
    initial_fanout_descent_span,
    iter_horizontal_trunks,
    iter_inter_row_gaps,
    iter_port_peeloff_bundles,
    iter_vertical_segments,
    peeloff_target_slots,
    seat_peeloff_port_y,
    symmetric_bundle_midpoint,
    tail_on_slot,
    trunk_depths_contiguous,
    trunk_segments_cross,
)
from nf_metro.layout.routing.context import (
    _resolve_section_col,
    _RoutingCtx,
)
from nf_metro.layout.routing.corners import (
    concentric_corner_radius_at,
    corner_radius,
    l_shape_radii,
)
from nf_metro.parser.model import MetroGraph, PortSide


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
            if any(
                min(ch.y_hi, o.y_hi) - max(ch.y_lo, o.y_lo) > MIN_CORRIDOR_Y_OVERLAP
                for o in g
            ):
                g.append(ch)
                placed = True
                break
        if not placed:
            groups.append([ch])
    return groups


def _section_intrudes(graph: MetroGraph, x: float, y_lo: float, y_hi: float) -> bool:
    """True if a re-stacked channel at ``x`` would land inside any section bbox."""
    for s in graph.sections.values():
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


def _layout_gap_bundle(
    bundles: list[tuple[bool, list[_VChannel]]],
    gap_left: float,
    gap_right: float,
    ctx: _RoutingCtx,
) -> None:
    """Lay out one ``(gap, row)``'s bundles concentrically, centred in the gap."""
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
        if lone:
            mid = (gap_left + gap_right) / 2
        else:
            mid = symmetric_bundle_midpoint(gap_left, gap_right, widths, bi)
        n = len(order)
        # line_id -> (slot index, x); every segment of that line overlays
        # at its single slot rather than claiming an OFFSET_STEP each.
        line_slot = {
            lid: (i, mid + (i - (n - 1) / 2) * step) for i, lid in enumerate(order)
        }
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

    The grouping is read from the declared slots rather than rediscovered from
    raw geometry; the concentric layout and flanking-radius recompute are the
    same per-gap logic a single handler cannot do alone (it needs every leg in
    the gap at once).
    """
    graph = ctx.graph
    by_gap: dict[tuple[int, int | None], list[_VChannel]] = defaultdict(list)
    for rp in routes:
        if rp.normalize_exempt:
            continue
        for slot in rp.gap_slots:
            ch = _locate_slot_channel(rp, slot, graph)
            if ch is not None:
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
        # source edge.
        for r, band in bands.items():
            if not any(c.y_lo < band[1] and band[0] < c.y_hi for c in chans):
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
        _layout_gap_bundle(bundles, gap_left, gap_right, ctx)


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


def _snap_group(group: _Coincidence) -> None:
    """Snap every channel in a coincidence group onto its shared reference X."""
    for ch in group.channels:
        if abs(ch.x - group.ref_x) > COORD_TOLERANCE:
            _set_vchannel_x(ch, group.ref_x)


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
        _snap_group(group)
    for group in _divergent_source_groups(routes):
        _snap_group(group)
    for group in _merge_feeder_groups(routes, ctx):
        _snap_group(group)
    _join_fanout_upstream_tails(routes, ctx)


def _reconcile_port_peeloff_risers(routes: list[RoutedPath], ctx: _RoutingCtx) -> None:
    """Re-stack peel-off risers onto the slot their settled trunk depth earns.

    The riser order is assigned during gap materialisation from the trunk
    depths known then, but the later trunk-slot pass can repack those depths --
    a hand-authored grid can stagger them against their source columns -- which
    can leave a riser on a slot a different depth earns: the braid
    :func:`check_peeloff_concentric` flags.  Running after the trunk pass, this
    reads the settled depths and permutes each off-slot riser onto the
    depth-earned slot -- its peel-x via the standard :func:`_restack_channel`
    path and its port-slot Y to match -- the per-line slot assignment the guard
    certifies.  The in-section continuation leaves the port at its base Y, so
    this only re-seats the concentric stagger at the port, never the section
    linkage.
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
            _restack_channel(ch, slot.peel_x, slot.rank, n, step, ctx.curve_radius)
            seat_peeloff_port_y(rp, slot.port_y)


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
    by_port: dict[tuple[str, str, bool], list[_VChannel]] = defaultdict(list)
    for rp in routes:
        if not rp.is_inter_section:
            continue
        ch = _final_port_approach(rp)
        if ch is None:
            continue
        # Group by the destination port, not its exact terminal (x, y): two
        # same-line descents into one port can land a per-line offset apart
        # before render offsets are applied, and keying on the raw endpoint
        # would split that single convergence into two.
        target = entry_port_for.get(rp.edge.target, rp.edge.target)
        by_port[(target, rp.line_id, ch.down)].append(ch)

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


def _set_vchannel_x(ch: _VChannel, new_x: float) -> None:
    """Move a vertical channel to *new_x*, re-deriving its flanking corners.

    Fusing same-line descents into one track makes them a single stroke with
    no concentric nesting (zero displacement from itself), so each flanking
    corner is re-derived from the route's *final* waypoints as a zero-offset
    concentric corner via :func:`concentric_corner_radius_at` -- the same
    central helper the routing handlers use -- rather than hand-set to a fixed
    radius after the move.  A zero-offset corner resolves to the base radius.
    """
    rp = ch.route
    pts = rp.points
    k = ch.idx
    pts[k] = (new_x, pts[k][1])
    pts[k + 1] = (new_x, pts[k + 1][1])
    if rp.curve_radii is None:
        return
    for radius_idx in (k - 1, k):
        if 0 <= radius_idx < len(rp.curve_radii):
            prev_pt, corner_pt, next_pt = pts[radius_idx : radius_idx + 3]
            rp.curve_radii[radius_idx] = concentric_corner_radius_at(
                prev_pt, corner_pt, next_pt, 0.0
            )


def _initial_fanout_descent(rp: RoutedPath) -> _VChannel | None:
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
    """
    by_source: dict[tuple[str, str, bool], list[_VChannel]] = defaultdict(list)
    for rp in routes:
        if not rp.is_inter_section:
            continue
        ch = _initial_fanout_descent(rp)
        if ch is None:
            continue
        by_source[(rp.edge.source, rp.line_id, ch.down)].append(ch)

    groups: list[_Coincidence] = []
    for chans in by_source.values():
        if len(chans) < 2:
            continue
        sx = chans[0].route.points[0][0]
        ref_x = min(chans, key=lambda c: abs(c.x - sx)).x
        groups.append(_Coincidence(chans, ref_x))
    return groups


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
    merge = ctx.merge
    if not merge.trunk_source:
        return []
    graph = ctx.graph
    by_key = {
        (r.edge.source, r.edge.target, r.line_id): r
        for r in routes
        if r.is_inter_section
    }
    groups: list[_Coincidence] = []
    for mjid, trunk_src in merge.trunk_source.items():
        trunk_rp: RoutedPath | None = None
        branch_rps: list[RoutedPath] = []
        for e in graph.edges_to(mjid):
            rp = by_key.get((e.source, e.target, e.line_id))
            if rp is None:
                continue
            if e.source == trunk_src:
                trunk_rp = rp
            else:
                branch_rps.append(rp)
        trunk_src_st = graph.stations.get(trunk_src)
        if trunk_rp is None or trunk_src_st is None:
            continue
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
        dips = grp[0].dips_down
        by_dir = {sign: [t for t in grp if t.sign_x == sign] for sign in (1, -1)}
        bands = [b for b in by_dir.values() if b]
        # Order bands top -> bottom by current vertical position so allocation
        # moves each the least and never reorders the two flows (no new
        # crossing).  Slot layouts (and per-band heights) are computed up front.
        bands.sort(key=lambda b: min(t.y for t in b))
        planned = [_plan_trunk_band(b) for b in bands]
        gap = BUNDLE_TO_BUNDLE_CLEARANCE
        total = sum((n - 1) * step for _o, _t, n in planned) + gap * (len(planned) - 1)
        # Stack the bands top -> bottom with a clear gap; anchor at the current
        # cluster top, then slide the whole stack up if its bottom would crowd
        # the next row's header.  Sliding up (into the free upper gap) preserves
        # the inter-band gap without pushing the lower band into the header.
        top = min(t.y for t in grp)
        band_top = _clamp_inter_row_band_top(ctx, top, total)
        for order, track_of, n in planned:
            _restack_trunk_band(order, track_of, n, band_top, dips, step, ctx, bundled)
            band_top += (n - 1) * step + gap

    _dogleg_off_exempt_trunks(routes, ctx, skip=bundled)


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
    trunks = _declared_htrunks(routes)
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

    Starts at the cluster *top* and slides the stack upward if its bottom would
    breach the next row's header clearance (``INTER_ROW_HEADER_CLEARANCE`` below
    the inter-row gap's lower edge), keeping the inter-band gap intact rather
    than crowding the lower band into the header.
    """
    band = _inter_row_gap_band(ctx, top)
    if band is None:
        return top
    _gap_top, gap_bottom = band
    limit = gap_bottom - INTER_ROW_HEADER_CLEARANCE
    if top + total > limit:
        return limit - total
    return top


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
        if id(t.route) in skip:
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
        min_sep = 2 * OFFSET_STEP  # below this the two strokes still fuse
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
        if id(t.route) in skip:
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
    from nf_metro.layout.routing.invariants import (
        _fanout_route_maps,
        fanout_junctions,
    )

    fanouts = fanout_junctions(ctx.graph)
    if not fanouts:
        return

    upstream, downstream = _fanout_route_maps(routes, fanouts)
    for (jid, line_id), up in upstream.items():
        down = downstream.get((jid, line_id))
        if down is None or len(up.points) < 2:
            continue
        p_prev, p_last = up.points[-2], up.points[-1]
        # Only a genuinely-horizontal final segment is extended; extend
        # its X to the downstream start X, keeping the upstream Y so the
        # approach into the bend stays horizontal.
        if abs(p_prev[1] - p_last[1]) <= COORD_TOLERANCE_FINE:
            up.points[-1] = (down.points[0][0], p_last[1])


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
    if not trunk_depths_contiguous(trunk_ys, len(src_cols), OFFSET_STEP):
        return None
    return sorted(_distinct_line_order(chans), key=lambda lid: trunk_depth[lid])


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
        if down:
            # b's deeper vertical crosses a's shallower right-going lead-outs.
            return sum(1 for t in turns[a] if t < deepest[b] - COORD_TOLERANCE)
        # UP: a's deeper vertical crosses b's shallower left-going lead-ins.
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
    exactly like line *i* of an *n*-line standard L-shape, so its two
    flanking corner radii come straight from :func:`l_shape_radii`, which
    encodes the concentric (outermost-line-largest-on-the-outside)
    geometry for both the down- and up-going cases.

    ``l_shape_radii`` assigns ``i = 0`` to the rightmost (DOWN) / leftmost
    (UP) line; the bundle here is ordered left-to-right with ``i`` growing
    rightward, so the index is mapped accordingly.
    """
    rp = ch.route
    pts = rp.points
    k = ch.idx
    pts[k] = (new_x, pts[k][1])
    pts[k + 1] = (new_x, pts[k + 1][1])

    if rp.curve_radii is None:
        return
    vertical = Direction.D if ch.down else Direction.U
    # Map left-to-right index to l_shape_radii's convention.
    li = (n - 1 - i) if ch.down else i
    _, r_first, r_second = l_shape_radii(
        li, n, vertical=vertical, offset_step=step, base_radius=base_radius
    )
    # Lead corner radius lives at curve_radii[k-1]; trail at curve_radii[k].
    if 0 <= k - 1 < len(rp.curve_radii):
        rp.curve_radii[k - 1] = r_first
    if k < len(rp.curve_radii) and k + 2 < len(pts):
        rp.curve_radii[k] = r_second

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
) -> float:
    """Centred midline x for a bundle of *n* lines in gap ``(lo, lo+1)``.

    This is only the initial placement during routing; the post-routing
    :func:`_materialize_gap_slots` pass re-stacks every inter-section
    channel into its final centred / B-separated position, so the value
    here just needs to land the channel in the right gap.
    """
    gap_left, gap_right = column_gap_edges(graph, lo, lo + 1, row=row)
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
    other sections are tested against the segment's open interior.  The
    horizontal segment runs from ``min(x1, x2)`` to ``max(x1, x2)``.

    A section is "crossed" when the segment overlaps its bbox's open
    interior - i.e. the segment penetrates the section rather than just
    grazing its boundary.  ``y`` is considered inside when it falls
    within ``[bbox_y - margin, bbox_y + bbox_h + margin]``.
    """
    lo_x, hi_x = (x1, x2) if x1 <= x2 else (x2, x1)
    for s in graph.sections.values():
        if s.bbox_w <= 0:
            continue
        if s.id in exclude_section_ids:
            continue
        # Strict X interior overlap: segment must enter past the bbox
        # left edge AND not end before reaching past the right edge.
        right = s.bbox_x + s.bbox_w
        if hi_x <= s.bbox_x or lo_x >= right:
            continue
        # Y inside bbox (with optional margin so headers/footers count).
        if s.bbox_y - margin <= y <= s.bbox_y + s.bbox_h + margin:
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
