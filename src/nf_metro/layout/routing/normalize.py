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
    OFFSET_STEP,
    SECTION_HEADER_PROTRUSION,
)
from nf_metro.layout.routing.common import (
    Direction,
    RoutedPath,
    column_gap_edges,
    row_bottom_edge,
    row_top_edge,
    symmetric_bundle_midpoint,
)
from nf_metro.layout.routing.context import (
    _RoutingCtx,
)
from nf_metro.layout.routing.corners import (
    corner_outside_sign,
    corner_radius,
    l_shape_radii,
    reference_anchored_radius,
)
from nf_metro.parser.model import (
    MetroGraph,
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
    step = ctx.offset_step
    channels = _collect_vchannels(routes)
    if not channels:
        return
    gap_intervals = _build_gap_intervals(graph)

    # Per-row vertical band (top/bottom Y) so a channel can be matched to
    # the row whose gap it actually travels in, not merely the first row
    # whose x-interval brackets it.  Two channels in the same column gap
    # but different grid rows (e.g. a row-0 fan and a row-1 bypass) must
    # NOT be merged into one bundle: each centres on its own row's gap.
    row_bands: dict[int, tuple[float, float]] = {}
    for s in graph.sections.values():
        if s.bbox_h <= 0:
            continue
        for r in range(s.grid_row, s.grid_row + max(1, s.grid_row_span)):
            top, bot = row_bands.get(r, (s.bbox_y, s.bbox_y + s.bbox_h))
            row_bands[r] = (min(top, s.bbox_y), max(bot, s.bbox_y + s.bbox_h))

    def _find_gap(ch: _VChannel) -> tuple[int, int | None, float, float] | None:
        """Match a channel to ``(lo_col, row, gap_left, gap_right)``.

        Prefer the row whose x-interval brackets the channel AND whose
        vertical band the channel overlaps; fall back to any bracketing
        row, then to the row-agnostic union.

        A channel that vertically crosses several rows must clear sections
        in ALL of them, so its gap is narrowed to the intersection of every
        crossed row's gap in the same column.  Otherwise a fan climbing out
        of a row whose section edge sits further out than a sibling row's
        would centre in the wider sibling gap and step back behind its source
        section edge (#386).
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

    # Bucket channels per (gap lo_col, row, direction).  A channel is only
    # a candidate when its x lands strictly inside the gap interior (so a
    # near-vertical drop hugging a section edge is left untouched).
    buckets: dict[tuple[int, int | None, bool], list[_VChannel]] = defaultdict(list)
    gap_bounds: dict[tuple[int, int | None], tuple[float, float]] = {}
    for ch in channels:
        gap = _find_gap(ch)
        if gap is None:
            continue
        lo, row, left, right = gap
        if not (left + COORD_TOLERANCE < ch.x < right - COORD_TOLERANCE):
            # x sits on / outside a section edge: not a clean gap channel.
            if not (left <= ch.x <= right):
                continue
        # Bundles sharing a (gap, row) are laid out together in one x-range,
        # so the shared bound must clear every member's crossed rows: narrow
        # to the intersection rather than letting the last channel win.
        prev = gap_bounds.get((lo, row))
        if prev is not None:
            left = max(left, prev[0])
            right = min(right, prev[1])
        gap_bounds[(lo, row)] = (left, right)
        buckets[(lo, row, ch.down)].append(ch)

    # Within a (gap, direction) bucket, split into corridors by vertical
    # overlap: only channels whose y-spans overlap share a true corridor
    # (independent vertical runs at different heights must NOT be merged).
    def _corridors(chans: list[_VChannel]) -> list[list[_VChannel]]:
        chans = sorted(chans, key=lambda c: (c.y_lo, c.y_hi))
        groups: list[list[_VChannel]] = []
        for ch in chans:
            placed = False
            for g in groups:
                if any(
                    ch.y_lo < o.y_hi - COORD_TOLERANCE
                    and o.y_lo < ch.y_hi - COORD_TOLERANCE
                    for o in g
                ):
                    g.append(ch)
                    placed = True
                    break
            if not placed:
                groups.append([ch])
        return groups

    # Assemble bundles per (gap, row): one per corridor, both directions,
    # laid out together so a down/up pair sharing a gap is B-separated.
    by_gap: dict[tuple[int, int | None], list[tuple[bool, list[_VChannel]]]]
    by_gap = defaultdict(list)
    for (lo, row, down), chans in buckets.items():
        for corridor in _corridors(chans):
            by_gap[(lo, row)].append((down, corridor))

    # Intrusion guard (row-agnostic): a re-stacked channel must never land
    # inside any section's bbox.
    def _intrudes(x: float, y_lo: float, y_hi: float) -> bool:
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

    for (lo, _row), bundles in by_gap.items():
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
            continue
        gap_left, gap_right = gap_bounds[(lo, _row)]
        widths = [max(0, len(o) - 1) * step for o in line_orders]
        # A lone bundle centres on the true gap midpoint (symmetric
        # clearance both sides) rather than flooring one edge at A, which
        # would push the bundle off-centre when the gap is sized tighter
        # than 2A + width.  Multi-bundle gaps keep the symmetric A/B
        # layout from symmetric_bundle_midpoint.
        lone = len(bundles) == 1
        for bi, (down, chans) in enumerate(bundles):
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
            # Intrusion guard: if any target x would land inside a section
            # bbox (e.g. the gap bounds came from another row), leave this
            # bundle untouched rather than route through a section.
            if any(_intrudes(nx, ch.y_lo, ch.y_hi) for ch, (_li, nx) in targets):
                continue
            for ch, (li, nx) in targets:
                _restack_channel(ch, nx, li, n, step, ctx.curve_radius)


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
        pts = rp.points
        for k in range(1, len(pts) - 2):
            x0, y0 = pts[k]
            x1, y1 = pts[k + 1]
            if abs(y1 - y0) > COORD_TOLERANCE or abs(x1 - x0) <= COORD_TOLERANCE:
                continue
            # Both flanking neighbours must be vertical legs.
            if abs(pts[k - 1][0] - x0) > COORD_TOLERANCE:
                continue
            if abs(pts[k + 2][0] - x1) > COORD_TOLERANCE:
                continue
            dips_down = pts[k - 1][1] < y0 - COORD_TOLERANCE
            out.append(
                _HTrunk(
                    route=rp,
                    idx=k,
                    y=y0,
                    x_lo=min(x0, x1),
                    x_hi=max(x0, x1),
                    dips_down=dips_down,
                    sign_x=1 if x1 > x0 else -1,
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


def _align_peeloff_riser_gaps(routes: list[RoutedPath], ctx: _RoutingCtx) -> None:
    """Match a peel-off riser bundle's spacing to its shared trunk's spacing.

    When several inter-section lines travel as one concentric bundle along a
    shared horizontal trunk (fanned ``OFFSET_STEP`` apart by
    :func:`_normalize_bypass_trunks`) and a SUBSET of them peels off at the
    same end - rising into a common entry port - the riser legs were
    independently re-stacked by :func:`_normalize_gap_channels` into a
    compacted bundle (adjacent ``OFFSET_STEP`` slots).  Two lines three slots
    apart on the trunk (e.g. the outer two of a four-line trunk) thus rise
    only one slot apart, so the perpendicular gap collapses through the bend
    and the parallel lines pinch instead of staying concentric (issue #484,
    the corner just before the leftmost Reports section).

    This pass restores concentricity at the bend: for each shared trunk
    channel, the risers that peel off at the same turning corner are re-spaced
    so their perpendicular X-gap equals the perpendicular Y-gap they hold on
    the trunk, preserving order (outer trunk -> outer riser).  The flanking
    corner radii are recomputed concentrically.  Only fires when the riser
    spacing actually disagrees with the trunk spacing, so a bundle whose
    members are already contiguous on the trunk is left untouched.
    """
    step = ctx.offset_step
    trunks = _collect_htrunks(routes)
    if len(trunks) < 2:
        return

    for grp in _group_channel_trunks(trunks, step):
        if len({id(t.route) for t in grp}) < 2:
            continue
        # A peel-off riser is the vertical segment immediately adjacent to the
        # trunk on the turning side, followed by a horizontal lead into a port.
        # Collect, per turning side, the (trunk Y, riser channel) of every
        # trunk in this group that turns off there.
        for side in ("hi", "lo"):
            risers: list[tuple[float, _VChannel]] = []
            for t in grp:
                rc = _peeloff_riser(t, side)
                if rc is not None:
                    risers.append((t.y, rc))
            if len(risers) < 2:
                continue
            # Risers that peel off at DIFFERENT X (heading to different ports)
            # are independent bends; only those that turn off at the same place
            # form one bundle whose spacing must be preserved.  Cluster by
            # riser X (a turn-off is shared when the channels sit within the
            # full fanned-trunk width of each other).
            cluster_tol = max(t.y for t in grp) - min(t.y for t in grp) + step
            risers.sort(key=lambda r: r[1].x)
            cluster: list[tuple[float, _VChannel]] = []
            for r in risers + [None]:  # sentinel flush
                if cluster and (r is None or r[1].x - cluster[-1][1].x > cluster_tol):
                    lines = {rc.route.line_id for _, rc in cluster}
                    if len(cluster) >= 2 and len(lines) >= 2:
                        _respace_risers_to_trunk(cluster, step, ctx.curve_radius)
                    cluster = []
                if r is not None:
                    cluster.append(r)


def _peeloff_riser(t: _HTrunk, side: str) -> _VChannel | None:
    """The vertical riser segment turning off a trunk at *side* ('hi'/'lo').

    Returns the ``_VChannel`` for the vertical leg flanking trunk *t* on its
    higher-X (``side == 'hi'``) or lower-X (``'lo'``) end, but only when that
    leg in turn leads into a horizontal segment (the port-approach lead),
    i.e. the trunk peels UP/DOWN and then turns to enter a section.  Returns
    ``None`` when there is no such riser-then-horizontal on that side.
    """
    rp = t.route
    pts = rp.points
    k = t.idx  # trunk is pts[k] -> pts[k+1]
    if side == "lo":
        # Riser precedes the trunk: pts[k-1] -> pts[k]; lead is pts[k-2].
        vi = k - 1
        lead_i = k - 2
    else:
        # Riser follows the trunk: pts[k+1] -> pts[k+2]; lead is pts[k+3].
        vi = k + 1
        lead_i = k + 3
    if vi < 0 or vi + 1 >= len(pts):
        return None
    x0, y0 = pts[vi]
    x1, y1 = pts[vi + 1]
    if abs(x1 - x0) > COORD_TOLERANCE or abs(y1 - y0) <= COORD_TOLERANCE:
        return None
    # The riser must lead into a horizontal segment (the port approach).
    if not (0 <= lead_i < len(pts)):
        return None
    lx = pts[lead_i][0]
    # lead point shares its riser endpoint's Y -> horizontal lead present.
    ly_idx = vi if side == "lo" else vi + 1
    if abs(pts[lead_i][1] - pts[ly_idx][1]) > COORD_TOLERANCE:
        return None
    if abs(lx - pts[ly_idx][0]) <= COORD_TOLERANCE:
        return None
    return _VChannel(
        route=rp,
        idx=vi,
        x=x0,
        y_lo=min(y0, y1),
        y_hi=max(y0, y1),
        down=y1 > y0,
    )


def _respace_risers_to_trunk(
    risers: list[tuple[float, _VChannel]],
    step: float,
    base_radius: float,
) -> None:
    """Re-space a peel-off riser bundle to its trunk's perpendicular spacing.

    *risers* pairs each turning line's trunk Y with its riser channel.  The
    risers all share one port (their lead horizontals converge), so the bundle
    centre is anchored on the current mean riser X; each riser is then offset
    from that centre by the SAME signed magnitude it sits from the trunk-bundle
    centre (its trunk Y minus the bundle's mean Y), preserving the
    outer-trunk -> outer-riser order and the constant perpendicular gap that
    keeps the bend concentric.  The flanking corner radii are recomputed from
    each riser's actual offset so the nested arcs stay an equal gap apart.
    No-op when the riser spacing already matches the trunk spacing.
    """
    # Collapse to one representative per distinct LINE: same-line risers (a
    # fan whose line feeds several targets) share one slot, so they must move
    # together to a single X rather than each claiming a slot.
    per_line: dict[str, tuple[float, float]] = {}
    for ty, rc in risers:
        lid = rc.route.line_id
        if lid not in per_line:
            per_line[lid] = (ty, rc.x)
    if len(per_line) < 2:
        return
    lines = list(per_line)
    trunk_ys = [per_line[lid][0] for lid in lines]
    riser_xs = [per_line[lid][1] for lid in lines]
    riser_mid = sum(riser_xs) / len(lines)
    trunk_mid = sum(trunk_ys) / len(lines)
    # Sign coupling from the current crossing-free geometry: if the currently
    # leftmost line comes from the shallower (smaller-Y) trunk, increasing X
    # tracks increasing trunk Y; otherwise the mapping is flipped.  Mirror the
    # trunk's signed perpendicular offset onto X with that sign so no crossing
    # is introduced.
    order = sorted(range(len(lines)), key=lambda j: riser_xs[j])
    sign = 1.0 if trunk_ys[order[0]] <= trunk_ys[order[-1]] else -1.0
    target_x = {
        lines[j]: riser_mid + sign * (trunk_ys[j] - trunk_mid)
        for j in range(len(lines))
    }
    if all(abs(target_x[lid] - per_line[lid][1]) <= COORD_TOLERANCE for lid in lines):
        return
    max_off = (max(target_x.values()) - min(target_x.values())) / 2
    for _ty, rc in risers:
        _set_riser_x_and_radii(
            rc, target_x[rc.route.line_id], riser_mid, max_off, base_radius
        )


def _set_riser_x_and_radii(
    ch: _VChannel,
    new_x: float,
    centre_x: float,
    max_off: float,
    base_radius: float,
) -> None:
    """Move a riser channel to *new_x* and size its flanking corners.

    Mirrors :func:`_restack_channel` but sizes each flanking corner radius
    from the riser's actual signed offset (``new_x - centre_x``) rather than
    an integer ``OFFSET_STEP`` slot, so a bundle spaced wider than one step -
    inherited from a fanned trunk - keeps its nested arcs an equal
    perpendicular gap apart.  Each corner's handedness is read from the local
    segment directions, so a Z-step riser (whose two corners turn opposite
    ways) gets the correct inner/outer assignment at each end.
    """
    rp = ch.route
    pts = rp.points
    k = ch.idx
    pts[k] = (new_x, pts[k][1])
    pts[k + 1] = (new_x, pts[k + 1][1])
    if rp.curve_radii is None or max_off <= COORD_TOLERANCE:
        return
    off_signed = new_x - centre_x
    v_dir = (0.0, 1.0) if pts[k + 1][1] > pts[k][1] else (0.0, -1.0)
    # Lead corner (incoming H -> V) at pts[k], radius slot k-1.
    # Trail corner (V -> outgoing H) at pts[k+1], radius slot k.
    for corner_idx, radius_idx, is_lead in ((k, k - 1, True), (k + 1, k, False)):
        nbr_idx = corner_idx - 1 if is_lead else corner_idx + 1
        if not (0 <= nbr_idx < len(pts)):
            continue
        # X direction of the horizontal segment, away from the corner.
        hx = 1.0 if pts[nbr_idx][0] > pts[corner_idx][0] else -1.0
        if is_lead:
            turn_in = (-hx, 0.0)  # H travels toward the corner
            turn_out = v_dir
        else:
            turn_in = v_dir
            turn_out = (hx, 0.0)
        side = corner_outside_sign(turn_in, turn_out)
        # Outside line (offset sign matches `side`) gets the largest radius;
        # inner edge gets base.  Anchored on the bundle centre (base + max_off).
        r = reference_anchored_radius(off_signed * side, base_radius + max_off)
        if not (0 <= radius_idx < len(rp.curve_radii)):
            continue
        if not is_lead and not (k + 2 < len(pts)):
            continue
        rp.curve_radii[radius_idx] = r


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
    by_port: dict[tuple[float, float, str, bool], list[_VChannel]] = defaultdict(list)
    for rp in routes:
        if not rp.is_inter_section:
            continue
        ch = _final_port_approach(rp)
        if ch is None:
            continue
        ex, ey = rp.points[-1]
        key = (round(ex, 1), round(ey, 1), rp.line_id, ch.down)
        by_port[key].append(ch)

    band = EDGE_TO_BUNDLE_CLEARANCE
    for (ex, _ey, _lid, _down), chans in by_port.items():
        if len(chans) < 2:
            continue
        # Cluster by descent X proximity; widely-separated descents are
        # distinct corridors and must not be fused.
        chans.sort(key=lambda c: c.x)
        cluster: list[_VChannel] = []

        def _flush(cluster: list[_VChannel]) -> None:
            if len(cluster) < 2:
                return
            merge_x = min(cluster, key=lambda c: abs(c.x - ex)).x
            for c in cluster:
                if abs(c.x - merge_x) > COORD_TOLERANCE:
                    _set_port_approach_x(c, merge_x)

        for ch in chans:
            if cluster and ch.x - cluster[-1].x > band:
                _flush(cluster)
                cluster = []
            cluster.append(ch)
        _flush(cluster)


def _set_port_approach_x(ch: _VChannel, new_x: float) -> None:
    """Move a final port-approach vertical to *new_x*, resetting its corners.

    The fused descents form one track, so both flanking corners take the base
    ``CURVE_RADIUS`` (no concentric nesting remains to size them apart).
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
        heights = [(len(order) - 1) * step for order in planned]
        total = sum(heights) + gap * (len(planned) - 1)
        # Stack the bands top -> bottom with a clear gap; anchor at the current
        # cluster top, then slide the whole stack up if its bottom would crowd
        # the next row's header.  Sliding up (into the free upper gap) preserves
        # the inter-band gap without pushing the lower band into the header.
        top = min(t.y for t in grp)
        band_top = _clamp_inter_row_band_top(ctx, top, total)
        for order, h in zip(planned, heights):
            _restack_trunk_band(order, band_top, dips, step, ctx, bundled)
            band_top += h + gap

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


def _plan_trunk_band(band: list[_HTrunk]) -> list[list[_HTrunk]]:
    """Order one same-direction band into concentric slots.

    Bundle slots are per distinct LINE, not per trunk: two trunks of the SAME
    line whose X-spans overlap are a fan-out/fan-in of one metro line and
    COINCIDE on one slot (issue #484); distinct lines (and disjoint same-line
    trunks) keep their own concentric slots.

    Slots are ordered to minimise crossings between each slot's peel-off risers
    and the others' trunk legs.  Among orderings tied on crossings the
    widest-reaching slot sorts OUTERMOST (deepest into the channel) so a
    slot's flanking verticals never needlessly cross another slot's leg; ties
    beyond that keep incoming order.  The width key is also the tie-break that
    keeps every already-optimal band byte-identical to the prior heuristic.
    """
    slot_groups = _coincident_trunk_slots(band)
    heuristic = sorted(
        slot_groups,
        key=lambda sg: (
            -max(t.x_hi - t.x_lo for t in sg),
            min(t.x_lo for t in sg),
            min(t.y for t in sg),
        ),
    )
    if len(slot_groups) < 2 or len(slot_groups) > _MAX_BAND_PERMUTE:
        return heuristic

    # `_restack_trunk_band` lays slot 0 at the channel-interior extreme: the
    # BOTTOM (largest y) for a downward dip, the TOP for an upward dip.  Score
    # crossings in top-to-bottom space, then convert the winner back to slots.
    dips = band[0].dips_down
    h_ttb = list(reversed(heuristic)) if dips else heuristic
    h_rank = {id(sg): r for r, sg in enumerate(h_ttb)}
    feats = {id(sg): _trunk_slot_features(sg) for sg in slot_groups}

    def _key(perm: list[list[_HTrunk]]) -> tuple[int, ...]:
        # Tie-break by position in the heuristic order: the heuristic itself
        # scores (0, 1, .. m-1), the lexicographically smallest tuple, so an
        # already-optimal band reproduces the heuristic order exactly.
        return (
            _band_order_crossings(perm, feats),
            *(h_rank[id(sg)] for sg in perm),
        )

    best_ttb = min((list(p) for p in itertools.permutations(h_ttb)), key=_key)
    return list(reversed(best_ttb)) if dips else best_ttb


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
    band_top: float,
    dips: bool,
    step: float,
    ctx: _RoutingCtx,
    bundled: set[int],
) -> None:
    """Fan one planned same-direction band into its concentric slots.

    The band occupies ``[band_top, band_top + (n-1)*step]``; the slot closest
    to the channel interior (innermost) sits at the shallow edge.  All trunks
    here -- including exempt ones grouped with a non-exempt mate -- are placed
    so the whole band reads as one tight concentric bundle.
    """
    n = len(order)
    for slot, sg in enumerate(order):
        inner = n - 1 - slot  # 0 = innermost (shallowest); sets the corner radii
        # Depth from ``band_top`` (the band's smallest Y).  For a downward dip
        # the channel interior is above, so the innermost slot sits at the top;
        # for an upward dip the interior is below, so the innermost sits at the
        # bottom -- hence the inner/slot swap.
        depth = inner if dips else slot
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
        # Lower edge reserves the next row's header protrusion.
        band = _inter_row_gap_band(ctx, t.y)
        if band is not None:
            top, bottom = band
            down_room = (bottom - SECTION_HEADER_PROTRUSION) - hit.y
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
        if (t.y >= hit.y and below_ok) or (not above_ok and below_ok):
            new_y = below
        elif above_ok:
            new_y = above
        else:
            continue
        _restack_htrunk(t, new_y, 0, 1, step, ctx.curve_radius)


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
    # deepest reach, and a representative x for stable tie-breaking.
    turns: dict[str, list[float]] = defaultdict(list)
    deepest: dict[str, float] = {}
    rep_x: dict[str, float] = {}
    for ch in chans:
        lid = ch.route.line_id
        turns[lid].append(ch.y_hi)
        deepest[lid] = max(deepest.get(lid, ch.y_hi), ch.y_hi)
        rep_x[lid] = min(rep_x.get(lid, ch.x), ch.x)

    def crossings_if_left(a: str, b: str) -> int:
        # Number of crossings when a is placed LEFT of b.
        if down:
            # b's deeper vertical crosses a's shallower right-going lead-outs.
            return sum(1 for t in turns[a] if t < deepest[b] - COORD_TOLERANCE)
        # UP: a's deeper vertical crosses b's shallower left-going lead-ins.
        return sum(1 for t in turns[b] if t < deepest[a] - COORD_TOLERANCE)

    def cmp(a: str, b: str) -> int:
        ca = crossings_if_left(a, b)  # a left of b
        cb = crossings_if_left(b, a)  # b left of a
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
