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
    CURVE_RADIUS,
    EDGE_TO_BUNDLE_CLEARANCE,
    INTER_ROW_HEADER_CLEARANCE,
    MIN_CORRIDOR_Y_OVERLAP,
    NEXT_ROW_HEADER_BADGE_CLEARANCE,
    OFFSET_STEP,
    SECTION_HEADER_PROTRUSION,
)
from nf_metro.layout.routing.common import (
    Direction,
    HTrunkSeg,
    RoutedPath,
    column_gap_edges,
    initial_fanout_descent_span,
    iter_horizontal_trunks,
    row_bottom_edge,
    row_top_edge,
    symmetric_bundle_midpoint,
    trunk_segments_cross,
)
from nf_metro.layout.routing.context import (
    _RoutingCtx,
)
from nf_metro.layout.routing.corners import (
    corner_radius,
    l_shape_radii,
    reference_anchored_radius,
)
from nf_metro.parser.model import (
    MetroGraph,
    PortSide,
)


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


def _collect_vchannels(routes: list[RoutedPath]) -> list[_VChannel]:
    """Find every vertical channel segment in inter-section routes."""
    out: list[_VChannel] = []
    for rp in routes:
        if not rp.is_inter_section or rp.normalize_exempt:
            continue
        pts = rp.points
        for k in range(len(pts) - 1):
            x0, y0 = pts[k]
            x1, y1 = pts[k + 1]
            if abs(x1 - x0) < COORD_TOLERANCE and abs(y1 - y0) > COORD_TOLERANCE:
                out.append(
                    _VChannel(
                        route=rp,
                        idx=k,
                        x=x0,
                        y_lo=min(y0, y1),
                        y_hi=max(y0, y1),
                        down=y1 > y0,
                    )
                )
    return out


def _build_gap_intervals(
    graph: MetroGraph,
) -> dict[int | None, list[tuple[int, float, float]]]:
    """Per-row list of ``(lo_col, gap_left, gap_right)`` for adjacent columns.

    The row key is the grid row; a single combined ``None`` entry is also
    produced (row-agnostic union) as a fallback for channels whose row
    can't be matched precisely.
    """
    cols = sorted({s.grid_col for s in graph.sections.values() if s.bbox_w > 0})
    rows = sorted({s.grid_row for s in graph.sections.values() if s.bbox_w > 0})
    intervals: dict[int | None, list[tuple[int, float, float]]] = {}
    for row in list(rows) + [None]:
        per_row: list[tuple[int, float, float]] = []
        for lo, hi in zip(cols, cols[1:]):
            if hi != lo + 1:
                continue
            left, right = column_gap_edges(graph, lo, hi, row=row)
            if right > left:
                per_row.append((lo, left, right))
        intervals[row] = per_row
    return intervals


def _build_row_bands(graph: MetroGraph) -> dict[int, tuple[float, float]]:
    """Per grid-row vertical band (top/bottom Y) spanned by its sections.

    Lets a channel be matched to the row whose gap it actually travels in,
    not merely the first row whose x-interval brackets it: two channels in
    the same column gap but different grid rows (e.g. a row-0 fan and a
    row-1 bypass) must NOT merge into one bundle.
    """
    row_bands: dict[int, tuple[float, float]] = {}
    for s in graph.sections.values():
        if s.bbox_h <= 0:
            continue
        for r in range(s.grid_row, s.grid_row + max(1, s.grid_row_span)):
            top, bot = row_bands.get(r, (s.bbox_y, s.bbox_y + s.bbox_h))
            row_bands[r] = (min(top, s.bbox_y), max(bot, s.bbox_y + s.bbox_h))
    return row_bands


def _match_channel_gap(
    ch: _VChannel,
    gap_intervals: dict[int | None, list[tuple[int, float, float]]],
    row_bands: dict[int, tuple[float, float]],
) -> tuple[int, int | None, float, float] | None:
    """Match a channel to ``(lo_col, row, gap_left, gap_right)``.

    Prefer the row whose x-interval brackets the channel AND whose vertical
    band the channel overlaps; fall back to any bracketing row, then to the
    row-agnostic union.

    A channel that vertically crosses several rows must clear sections in
    ALL of them, so its gap is narrowed to the intersection of every crossed
    row's gap in the same column.  Otherwise a fan climbing out of a row
    whose section edge sits further out than a sibling row's would centre in
    the wider sibling gap and step back behind its source section edge (#386).
    """
    x = ch.x
    overlap_match: tuple[int, int | None, float, float] | None = None
    bracket_match: tuple[int, int | None, float, float] | None = None
    for row in gap_intervals:
        if row is None:
            continue
        for lo, left, right in gap_intervals[row]:
            if not (left - COORD_TOLERANCE <= x <= right + COORD_TOLERANCE):
                continue
            if bracket_match is None:
                bracket_match = (lo, row, left, right)
            band = row_bands.get(row)
            if band is not None and ch.y_lo < band[1] and band[0] < ch.y_hi:
                if overlap_match is None:
                    overlap_match = (lo, row, left, right)
    match = overlap_match or bracket_match
    if match is not None:
        lo, row, left, right = match
        for r, band in row_bands.items():
            if not (ch.y_lo < band[1] and band[0] < ch.y_hi):
                continue
            for rlo, rleft, rright in gap_intervals.get(r, []):
                if rlo == lo:
                    left = max(left, rleft)
                    right = min(right, rright)
        return (lo, row, left, right)
    for lo, left, right in gap_intervals.get(None, []):
        if left - COORD_TOLERANCE <= x <= right + COORD_TOLERANCE:
            return (lo, None, left, right)
    return None


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


def _bucket_gap_channels(
    channels: list[_VChannel],
    gap_intervals: dict[int | None, list[tuple[int, float, float]]],
    row_bands: dict[int, tuple[float, float]],
) -> tuple[
    dict[tuple[int, int | None, bool], list[_VChannel]],
    dict[tuple[int, int | None], tuple[float, float]],
]:
    """Bucket channels per ``(gap lo_col, row, direction)`` with shared bounds.

    A channel is only a candidate when its x lands strictly inside the gap
    interior (so a near-vertical drop hugging a section edge is left
    untouched).  Bundles sharing a ``(gap, row)`` are laid out together in
    one x-range, so the shared bound is narrowed to the intersection of every
    member's crossed rows rather than letting the last channel win.
    """
    buckets: dict[tuple[int, int | None, bool], list[_VChannel]] = defaultdict(list)
    gap_bounds: dict[tuple[int, int | None], tuple[float, float]] = {}
    for ch in channels:
        gap = _match_channel_gap(ch, gap_intervals, row_bands)
        if gap is None:
            continue
        lo, row, left, right = gap
        if not (left + COORD_TOLERANCE < ch.x < right - COORD_TOLERANCE):
            # x sits on / outside a section edge: not a clean gap channel.
            if not (left <= ch.x <= right):
                continue
        prev = gap_bounds.get((lo, row))
        if prev is not None:
            left = max(left, prev[0])
            right = min(right, prev[1])
        gap_bounds[(lo, row)] = (left, right)
        buckets[(lo, row, ch.down)].append(ch)
    return buckets, gap_bounds


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
    line_orders = [_distinct_line_order(c) for _, c in bundles]
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


def _normalize_gap_channels(routes: list[RoutedPath], ctx: _RoutingCtx) -> None:
    """Re-bundle inter-section vertical channels sharing a gap + direction.

    Post-routing pass that enforces the uniform inter-section gap geometry
    regardless of which handler placed each leg:

    * All same-direction channels sharing one inter-column gap collapse
      into ONE concentric bundle, ``OFFSET_STEP`` apart, centred.
    * A downward bundle and an upward bundle sharing a gap are held
      ``BUNDLE_TO_BUNDLE_CLEARANCE`` (B) apart, centred as a group.
    * A lone bundle centres in its gap with at least
      ``EDGE_TO_BUNDLE_CLEARANCE`` (A) from each bounding section edge.

    Only channels whose current x already lands inside a real inter-column
    gap are touched, so wrap / around-section legs that deliberately sit
    outside the immediate gap are left alone.  Corner radii flanking each
    re-stacked channel are recomputed so the bundle stays concentric.
    """
    graph = ctx.graph
    channels = _collect_vchannels(routes)
    if not channels:
        return
    gap_intervals = _build_gap_intervals(graph)
    row_bands = _build_row_bands(graph)

    buckets, gap_bounds = _bucket_gap_channels(channels, gap_intervals, row_bands)

    # Assemble bundles per (gap, row): one per corridor, both directions,
    # laid out together so a down/up pair sharing a gap is B-separated.
    by_gap: dict[tuple[int, int | None], list[tuple[bool, list[_VChannel]]]]
    by_gap = defaultdict(list)
    for (lo, row, down), chans in buckets.items():
        for corridor in _split_corridors(chans):
            by_gap[(lo, row)].append((down, corridor))

    for (lo, _row), bundles in by_gap.items():
        gap_left, gap_right = gap_bounds[(lo, _row)]
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


def _group_channel_trunks(
    trunks: list[_HTrunk], step: float, ctx: _RoutingCtx | None = None
) -> list[list[_HTrunk]]:
    """Group horizontal bypass trunks that visually share one channel.

    Trunks belong together when they share a dip direction and transitively
    overlap in X within one channel.  Channel membership is decided two ways:

    - When *ctx* is given and both trunks fall inside the SAME inter-row gap
      (the ``[row_bottom, next_row_top]`` envelope from
      :func:`_inter_row_gap_band`), they share that channel however far apart
      their current Ys sit.  Several bypass routes that dip into one inter-row
      gap are one visual channel even when their per-bundle ``nest_offset``
      left them a smear of distinct Ys, so they must fan into a single tight
      ``OFFSET_STEP`` bundle rather than separate loose groups.
    - Otherwise (no ctx, or a trunk outside every inter-row gap) membership
      falls back to proximity to the NEAREST current member: trunks arrive
      pre-stacked by their per-bundle ``nest_offset``, so a trunk one ``step``
      deeper than the group's current deepest member still belongs.  A
      genuinely separate channel a full row away (Ys far outside the chain)
      then starts its own group.

    The shared X-overlap requirement keeps distinct corridors in the same gap
    band - different X regions that never overlap - in separate groups.
    """
    band = max(step, COORD_TOLERANCE)
    gap_of = {id(t): _inter_row_gap_band(ctx, t.y) for t in trunks} if ctx else {}

    def _same_channel(o: _HTrunk, t: _HTrunk) -> bool:
        go, gt = gap_of.get(id(o)), gap_of.get(id(t))
        if go is not None and go == gt:
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


class _PeeloffTail(NamedTuple):
    """A riser peeling off a horizontal trunk into an entry port."""

    trunk_y: float
    peel_x: float
    port_y: float
    trunk_sign: int  # +1 trunk runs left->right toward the peel, -1 right->left


def _port_peeloff_tail(rp: RoutedPath) -> _PeeloffTail | None:
    """The peel-off tail of a riser ending at an entry port, or ``None``.

    A peel-off-into-port tail ends ``... (tx, trunk_y) -> (peel_x, trunk_y)
    -> (peel_x, port_y) -> (ex, port_y)``: a horizontal trunk, an upward
    vertical riser, then a short horizontal lead into the port (the port sits
    above the trunk).  ``trunk_sign`` records the trunk's traversal direction
    toward the peel corner.  Returns ``None`` for any other tail.
    """
    pts = rp.points
    if len(pts) < 4:
        return None
    (x4, y4), (x3, y3), (x2, y2), (x1, y1) = pts[-4], pts[-3], pts[-2], pts[-1]
    if abs(y2 - y1) > COORD_TOLERANCE or abs(x2 - x1) <= COORD_TOLERANCE:
        return None  # port lead is not horizontal
    if abs(x3 - x2) > COORD_TOLERANCE or abs(y3 - y2) <= COORD_TOLERANCE:
        return None  # riser is not vertical
    if abs(y4 - y3) > COORD_TOLERANCE or abs(x4 - x3) <= COORD_TOLERANCE:
        return None  # trunk is not horizontal
    if y2 >= y3 - COORD_TOLERANCE:
        return None  # not an upward riser (port not above the trunk)
    return _PeeloffTail(y3, x3, y2, 1 if x3 > x4 else -1)


def _reorder_convergence_peeloff(routes: list[RoutedPath], ctx: _RoutingCtx) -> None:
    """Order one concentric bundle peeling off a shared trunk into a port.

    Several inter-section lines ride one bypass trunk - a single concentric
    bundle, ``OFFSET_STEP`` apart - below a LEFT entry port's row and rise
    into that common port.  ``_normalize_bypass_trunks`` stacks the trunk by
    spatial approach (the nearer source's lines on top), but the riser peel
    x-order and the port-slot Ys are assigned in line-declaration order by
    independent passes.  When the two orders disagree, a line on the bottom of
    the trunk rises on the near side and cuts across the lines stacked above it
    just before the port.

    Re-slot each line's peel-off x and its port-slot Y by trunk depth so the
    bundle turns into the port concentrically.  The trunk Ys are untouched; the
    peel x and the port-slot Y are permuted among the slots the bundle already
    occupies, so spacing is preserved.  The shallowest trunk line takes the
    slot nearest the trunk's near end - the inner slot for a left-to-right
    trunk, the outer slot for a right-to-left one - so the bend nests crossing
    free whichever end the bundle peels at.  Restricted to a single contiguous
    bundle (every member within the bundle's own ``OFFSET_STEP`` width of a
    neighbour): lines that reach the port on separate trunks rows apart are not
    one concentric turn and their order is owned by their own corridors.
    """
    by_port: dict[str, list[tuple[RoutedPath, _PeeloffTail]]] = defaultdict(list)
    for rp in routes:
        tail = _port_peeloff_tail(rp)
        if tail is None:
            continue
        port = ctx.graph.ports.get(rp.edge.target)
        if port is None or not port.is_entry or port.side is not PortSide.LEFT:
            continue
        by_port[rp.edge.target].append((rp, tail))

    step = ctx.offset_step
    for port_id, entries in by_port.items():
        # One representative tail per distinct line (a line feeding several
        # risers shares a single slot, so its risers move together).
        per_line: dict[str, _PeeloffTail] = {}
        for rp, t in entries:
            per_line.setdefault(rp.edge.line_id, t)
        n = len(per_line)
        if n < 2:
            continue
        trunk_ys = sorted(t.trunk_y for t in per_line.values())
        if trunk_ys[-1] - trunk_ys[0] <= COORD_TOLERANCE:
            continue  # no distinct trunk depths to order by
        if trunk_ys[-1] - trunk_ys[0] > (n - 1) * step + COORD_TOLERANCE:
            continue  # not one contiguous concentric bundle
        signs = {t.trunk_sign for t in per_line.values()}
        if len(signs) != 1:
            continue  # lines peel at different trunk ends; ambiguous
        reverse = signs.pop() < 0

        x_slots = sorted(t.peel_x for t in per_line.values())
        y_slots = sorted(t.port_y for t in per_line.values())
        ranked = sorted(per_line, key=lambda lid: per_line[lid].trunk_y)
        slot = list(range(n - 1, -1, -1)) if reverse else list(range(n))
        target_x = {lid: x_slots[slot[i]] for i, lid in enumerate(ranked)}
        target_y = {lid: y_slots[slot[i]] for i, lid in enumerate(ranked)}
        peel_rank = {lid: slot[i] for i, lid in enumerate(ranked)}
        if all(
            abs(target_x[lid] - per_line[lid].peel_x) <= COORD_TOLERANCE
            and abs(target_y[lid] - per_line[lid].port_y) <= COORD_TOLERANCE
            for lid in ranked
        ):
            continue  # already in trunk-depth order
        if not _section_reorderable(ctx, port_id, set(per_line)):
            continue

        for rp, _t in entries:
            lid = rp.edge.line_id
            nx, ny = target_x[lid], target_y[lid]
            pts = rp.points
            pts[-3] = (nx, pts[-3][1])
            pts[-2] = (nx, ny)
            pts[-1] = (pts[-1][0], ny)
            _set_peeloff_radii(rp, peel_rank[lid], n, step, ctx.curve_radius, reverse)

        # Propagate the slot order into the consumer section so its internal
        # bundle matches the port; otherwise the crossing the riser reorder
        # removes simply re-forms between the port and the first station.
        port_rank = {
            lid: r
            for r, lid in enumerate(sorted(ranked, key=lambda lid: target_y[lid]))
        }
        _apply_section_bundle_order(ctx, port_id, port_rank, step)


def _set_peeloff_radii(
    rp: RoutedPath,
    peel_rank: int,
    n: int,
    step: float,
    base_radius: float,
    reverse: bool,
) -> None:
    """Size a moved peel-off riser's two flanking corners concentrically.

    The riser ``points[-3] -> points[-2]`` is a Z-step: its trunk-side and
    port-side corners turn opposite ways, so the line outermost at one is
    innermost at the other.  Each corner's radii therefore step with peel-x
    rank in opposite directions, ``base_radius`` (innermost) up by one
    ``step`` per rank so the nested arcs stay an equal gap apart.  The
    rank-to-radius direction flips with the trunk's traversal sense
    (``reverse``), matching which trunk end the bundle peels at.
    """
    pts = rp.points
    if rp.curve_radii is None:
        return
    k = len(pts) - 3  # riser is pts[k] -> pts[k+1]
    inner_first = peel_rank if not reverse else (n - 1 - peel_rank)
    offset = inner_first * step
    max_offset = (n - 1) * step
    # Trunk-side corner: outermost peel slot is on the outside of its turn;
    # the port-side corner turns the other way, so the same line is inside.
    trunk_r = corner_radius(offset, max_offset, outside=True, base_radius=base_radius)
    port_r = corner_radius(offset, max_offset, outside=False, base_radius=base_radius)
    if 0 <= k - 1 < len(rp.curve_radii):
        rp.curve_radii[k - 1] = trunk_r
    if k < len(rp.curve_radii) and k + 2 < len(pts):
        rp.curve_radii[k] = port_r


def _section_reorderable(
    ctx: _RoutingCtx, port_id: str, bundle_lines: set[str]
) -> bool:
    """Whether *port_id*'s section can take the bundle's slot order safely.

    The propagation writes one dense ``rank * step`` offset per bundle line to
    every section station, so it is only safe when the consumer section is a
    plain single-row LR section carrying nothing but the bundle's lines.  A
    section with extra lines, more than one row, or a reversed flow needs the
    richer offsets-phase machinery and is left untouched.
    """
    if ctx.station_offsets is None:
        return False
    sec = ctx.graph.sections.get(ctx.graph.ports[port_id].section_id)
    if sec is None or sec.direction != "LR":
        return False
    ys: list[float] = []
    for sid in sec.station_ids:
        st = ctx.graph.stations[sid]
        if st.is_port:
            continue
        ys.append(st.y)
        if any(lid not in bundle_lines for lid in ctx.graph.station_lines(sid)):
            return False  # carries a line outside the bundle
    return not ys or max(ys) - min(ys) <= COORD_TOLERANCE


def _apply_section_bundle_order(
    ctx: _RoutingCtx, port_id: str, port_rank: dict[str, int], step: float
) -> None:
    """Set the per-line offsets of *port_id*'s section to ``port_rank`` order.

    The bundle's order entering the port (``port_rank`` 0 = topmost slot) is
    carried onto every station of the consumer section it reaches, so the
    section's internal bundle stays in the same order as the port and no
    crossing forms just inside the boundary.
    """
    if ctx.station_offsets is None:
        return
    # Section ``station_ids`` already includes the port station.
    for sid in ctx.graph.sections[ctx.graph.ports[port_id].section_id].station_ids:
        for lid in ctx.graph.station_lines(sid):
            if lid in port_rank:
                ctx.station_offsets[(sid, lid)] = port_rank[lid] * step


def _final_port_approach(rp: RoutedPath) -> _VChannel | None:
    """The final vertical descent into a port, when the route ends V then H.

    A converging port approach ends ``... (vx, y) -> (vx, ey) -> (ex, ey)``:
    a vertical leg into the entry Y, then a short horizontal lead into the
    port.  Returns the ``_VChannel`` for that vertical (``idx`` points at
    ``points[-3]``), or ``None`` when the tail is not vertical-then-horizontal.
    """
    pts = rp.points
    if len(pts) < 3:
        return None
    x1, y1 = pts[-1]
    x2, y2 = pts[-2]
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


def _coincide_convergent_port_approaches(routes: list[RoutedPath]) -> None:
    """Fuse same-line vertical approaches converging on one port into one track.

    Several inter-section edges of the SAME metro line can arrive at one entry
    port as separate near-parallel vertical descents (each turning into the
    port via its own short horizontal lead) a few pixels apart -- redundant
    duplicate tracks of one colour into a single convergence point (#484
    follow-up).  Where those final descents already sit in a tight band (so
    they are genuinely the same convergence channel, not legitimately distinct
    corridors arriving from far apart), snap them to one shared X so the line
    arrives as a single track and splits only upstream where each feed's
    horizontal lead peels off at its own Y.

    Channels are clustered by terminal port + line + descent direction; only
    clusters whose members fall within ``EDGE_TO_BUNDLE_CLEARANCE`` of each
    other are fused (the band excludes widely-staggered same-line inputs that
    descend in separate column gaps).  The merge X is the member nearest the
    port (smallest |vx - ex|), keeping the fused track on the side the port is
    already approached from.  Flanking corners reset to the base radius: the
    fused descents are a single track, so the concentric-bundle radii no
    longer apply.
    """
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
        key = (rp.edge.target, rp.line_id, ch.down)
        by_port[key].append(ch)

    band = EDGE_TO_BUNDLE_CLEARANCE
    for chans in by_port.values():
        if len(chans) < 2:
            continue
        ex = chans[0].route.points[-1][0]
        # Cluster by descent X proximity; widely-separated descents are
        # distinct corridors and must not be fused.
        chans.sort(key=lambda c: c.x)
        cluster: list[_VChannel] = []

        def _flush(cluster: list[_VChannel], ex: float = ex) -> None:
            if len(cluster) < 2:
                return
            merge_x = min(cluster, key=lambda c: abs(c.x - ex)).x
            for c in cluster:
                if abs(c.x - merge_x) > COORD_TOLERANCE:
                    _set_vchannel_x(c, merge_x)

        for ch in chans:
            if cluster and ch.x - cluster[-1].x > band:
                _flush(cluster)
                cluster = []
            cluster.append(ch)
        _flush(cluster)


def _set_vchannel_x(ch: _VChannel, new_x: float) -> None:
    """Move a vertical channel to *new_x*, resetting its flanking corners.

    Fusing same-line descents into one track removes the concentric-bundle
    nesting, so both flanking corners take the base ``CURVE_RADIUS``.
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
            rp.curve_radii[radius_idx] = reference_anchored_radius(0.0, CURVE_RADIUS)


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


def _coincide_divergent_fanout_descents(routes: list[RoutedPath]) -> None:
    """Fuse same-line vertical descents leaving one source into one track.

    The mirror of :func:`_coincide_convergent_port_approaches`: where the
    convergent pass merges same-line descents *arriving* at one port, this
    merges same-line descents *leaving* one source (a junction or exit port).
    Several inter-section edges of the SAME line fanning out from one source
    each open with their own horizontal lead and vertical channel a few pixels
    apart.  Every such branch leaves on the same source-Y horizontal lead, so
    they share the descent until each turns off: they are one trunk that split
    too early.  Left apart they read as parallel same-colour tracks, and an
    inverted split (the farther-reaching branch opening inside the nearer one)
    crosses its sibling's descent.

    Descents are grouped by source endpoint + line + descent direction; every
    group of two or more is fused onto the channel nearest the source, hugging
    the side the branches leave from.  Each branch splits off downstream at its
    own turn Y.
    """
    by_source: dict[tuple[str, str, bool], list[_VChannel]] = defaultdict(list)
    for rp in routes:
        if not rp.is_inter_section:
            continue
        ch = _initial_fanout_descent(rp)
        if ch is None:
            continue
        key = (rp.edge.source, rp.line_id, ch.down)
        by_source[key].append(ch)

    for chans in by_source.values():
        if len(chans) < 2:
            continue
        sx = chans[0].route.points[0][0]
        merge_x = min(chans, key=lambda c: abs(c.x - sx)).x
        for c in chans:
            if abs(c.x - merge_x) > COORD_TOLERANCE:
                _set_vchannel_x(c, merge_x)


def _normalize_bypass_trunks(routes: list[RoutedPath], ctx: _RoutingCtx) -> None:
    """Separate horizontal bypass trunks that share one below-row channel.

    Several inter-section bypass routes can dip into the same below-row
    channel and, with their per-line ``nest_offset`` resolved independently
    per bundle, end up drawn at the *same* Y (overlapping) or at a loose
    smear of distinct Ys (issue #484).  This post-pass mirrors
    :func:`_normalize_gap_channels` for the horizontal trunk legs: trunks
    that share a channel (same inter-row gap, same dip direction, overlapping
    X) are fanned ``OFFSET_STEP`` apart into a concentric bundle, with the
    widest-reaching trunk on the outside so the nesting introduces no
    crossings.

    Channel membership uses the inter-row gap envelope, so wrap-route trunks
    placed by their own handler (``normalize_exempt``) that co-travel through
    the same gap join the bundle too; they are only fanned when grouped with a
    non-exempt trunk in that gap (a genuine shared multi-line channel), so a
    pure-exempt run keeps its handler-owned Y and is left to
    :func:`_dogleg_off_exempt_trunks`.

    Trunks already at distinct Ys, or alone in their channel, are left
    untouched; the flanking corner radii are recomputed for any trunk that
    actually moves so the bundle stays concentric.
    """
    step = ctx.offset_step
    trunks = _collect_htrunks(routes, include_exempt=True)
    groups = _group_channel_trunks(trunks, step, ctx) if len(trunks) >= 2 else []

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

    Reconstructs the bands :func:`_normalize_bypass_trunks` reorders, then
    checks each realized top-to-bottom order against the crossing-minimal
    permutation.  An empty result means every band is crossing-optimal.
    """
    trunks = _collect_htrunks(routes, include_exempt=True)
    if len(trunks) < 2:
        return []
    groups = _group_channel_trunks(trunks, ctx.offset_step, ctx)
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
    rows = sorted({s.grid_row for s in ctx.graph.sections.values()})
    for upper, lower in zip(rows, rows[1:]):
        top = row_bottom_edge(ctx.graph, upper, default=None)  # type: ignore[arg-type]
        bottom = row_top_edge(ctx.graph, lower, default=None)  # type: ignore[arg-type]
        if top is None or bottom is None:
            continue
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
      drawn track.  Shifted clear by up to one bundle clearance, picking the
      side with room, so the two flows read as a dogleg/crossroads.
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
        prefer_down = up < min_sep or (down >= min_sep and t.y >= hit.y)
        if prefer_down and down >= min_sep:
            new_y = hit.y + down
        elif up >= min_sep:
            new_y = hit.y - up
        else:
            continue
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

    At a *fan-out* junction (single upstream source, one or more
    inter-section targets), the incoming ``port -> junction`` route and
    the outgoing ``junction -> target`` route are two separate
    :class:`RoutedPath`\\ s.  Their handoff points at the junction don't
    coincide: the downstream route carries the per-line bundle offset
    (and, for L-shape fans, a curve lead-in that starts a ``curve_radius``
    past the junction), while the upstream route ends at the bare junction
    coordinate.  The mismatch renders as a seam / notch where the two
    segments meet end-to-end instead of one continuous flowing line.

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
    :func:`_normalize_gap_channels` pass re-stacks every inter-section
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


def _has_other_row_section_in_col_range(
    graph: MetroGraph,
    src_col: int,
    tgt_col: int,
    src_row: int,
) -> bool:
    """Check if a section in a row OTHER than *src_row* sits anywhere in the
    column range ``[min(src_col, tgt_col), max(src_col, tgt_col)]``.

    Used by :func:`_route_merge_trunk` to decide whether the standard
    same-row bypass channel would visually collide with another row's
    section title text.  When no such other-row section exists in the
    column range, the standard channel sits in empty inter-row space
    and there is nothing to push the trunk further down for - so the
    historical ``cross_row=False`` placement is preferred.
    """
    lo, hi = min(src_col, tgt_col), max(src_col, tgt_col)
    for s in graph.sections.values():
        if s.bbox_w <= 0 or s.grid_row == src_row:
            continue
        if lo <= s.grid_col <= hi:
            return True
    return False


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
