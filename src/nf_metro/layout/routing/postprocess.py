"""Post-routing passes: diagonal bundle spread and bubble-station centring."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass

from nf_metro.layout.constants import (
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    CURVE_RADIUS,
    DIAGONAL_RUN,
    LABEL_BBOX_MARGIN,
    MIN_STATION_FLAT_LENGTH,
    MIN_STRAIGHT_EDGE,
    MIN_STRAIGHT_PORT,
    STATION_MOVE_TOLERANCE,
)
from nf_metro.layout.geometry import segment_intersects_bbox
from nf_metro.layout.routing.common import (
    RoutedPath,
)
from nf_metro.layout.routing.context import (
    _RoutingCtx,
)
from nf_metro.parser.model import (
    MetroGraph,
    Station,
    is_bypass_v,
)


def _is_diagonal_route(rp: RoutedPath) -> bool:
    """True if *rp* is a 4-point diagonal (horizontal-diagonal-horizontal).

    L-shapes also have 4 points with different Y at indices 1-2, but their
    middle points share the same X (vertical segment).  A true diagonal
    changes both X and Y between points 1 and 2.
    """
    if len(rp.points) != 4:
        return False
    dx = abs(rp.points[1][0] - rp.points[2][0])
    dy = abs(rp.points[1][1] - rp.points[2][1])
    return dx >= COORD_TOLERANCE and dy >= COORD_TOLERANCE_FINE


def _spread_diagonal_bundles(routes: list[RoutedPath], ctx: _RoutingCtx) -> None:
    """Spread bundled diagonals so co-travelling lines keep a perpendicular gap.

    A per-line offset is a fixed-axis displacement (Y for the render-time LR
    regime, baked X for TB / inter-section routes), so on a diagonal its
    perpendicular component shrinks to ``OFFSET_STEP * sin(theta)`` and the lines
    collapse onto one stroke.  This post-pass adds a complementary translation
    along the *other* axis to each diagonal's waypoints, restoring the full
    ``OFFSET_STEP`` perpendicular separation while keeping the lines parallel.

    Same-edge bundles (exactly parallel) are spread first on their own geometry;
    the fan / convergence hubs then spread their remaining single-line-per-edge
    branches apart.
    """
    if ctx.station_offsets is None:
        return

    # Track routes already spread so we don't shift one twice.
    spread: set[tuple[str, str, str]] = set()

    def _edge_key(rp: RoutedPath) -> tuple[str, str, str]:
        return (rp.edge.source, rp.edge.target, rp.line_id)

    # A multi-line same-edge diagonal is a co-travelling bundle whose lines are
    # exactly parallel, so it spreads to the full perpendicular gap on its own
    # geometry.  Do these first; a fan/convergence hub's branches to *different*
    # targets are not parallel, so spreading them together (below) under one
    # representative slope only approximates -- this claims the precise case so
    # the hub pass is left with the genuinely cross-target branches.
    edge_groups: dict[tuple[str, str], list[RoutedPath]] = defaultdict(list)
    for rp in routes:
        if not _is_diagonal_route(rp):
            continue
        edge_groups[(rp.edge.source, rp.edge.target)].append(rp)
    for (src, _tgt), group in edge_groups.items():
        if len(group) < 2:
            continue
        _apply_diagonal_spread(group, src, ctx=ctx)
        spread.update(_edge_key(rp) for rp in group)

    # Spread the remaining single-line-per-edge fan / convergence branches apart
    # at their shared hub so they do not overlap leaving (or entering) it.  Baked
    # routes are already handled per-edge above; the hub pass is render-only.
    fork_groups: dict[str, list[RoutedPath]] = defaultdict(list)
    join_groups: dict[str, list[RoutedPath]] = defaultdict(list)
    for rp in routes:
        if not _is_diagonal_route(rp) or rp.offsets_applied:
            continue
        if _edge_key(rp) in spread:
            continue
        if rp.edge.source in ctx.fork_stations:
            fork_groups[rp.edge.source].append(rp)
        if rp.edge.target in ctx.join_stations:
            join_groups[rp.edge.target].append(rp)

    for station_id, group in list(fork_groups.items()) + list(join_groups.items()):
        unseen = [rp for rp in group if _edge_key(rp) not in spread]
        if len(unseen) < 2:
            continue
        # Sub-group by diagonal direction (up vs down) so the scale
        # factor and sign are correct for each route.
        by_dir: dict[bool, list[RoutedPath]] = defaultdict(list)
        for rp in unseen:
            by_dir[rp.points[2][1] >= rp.points[1][1]].append(rp)
        for subgroup in by_dir.values():
            if len(subgroup) >= 2:
                _apply_diagonal_spread(subgroup, station_id, ctx=ctx)
        spread.update(_edge_key(rp) for rp in unseen)


def _stub_minimums(rp: RoutedPath) -> tuple[float, float]:
    """Minimum flat run at a route's (source, target) ends after spreading.

    A bypass-V helper must keep a visible horizontal flat through it
    (``MIN_STATION_FLAT_LENGTH``), longer than the ``MIN_STRAIGHT_EDGE`` that a
    plain corner needs, so the spread cannot pull its diagonal in far enough to
    seat V on the curve apex.
    """
    src_min = (
        MIN_STATION_FLAT_LENGTH if is_bypass_v(rp.edge.source) else MIN_STRAIGHT_EDGE
    )
    tgt_min = (
        MIN_STATION_FLAT_LENGTH if is_bypass_v(rp.edge.target) else MIN_STRAIGHT_EDGE
    )
    return src_min, tgt_min


def _fit_spread_translation(
    group: list[RoutedPath], deltas: list[float], sign: float, ai: int
) -> float | None:
    """A uniform shift along axis *ai* keeping every flat stub above the minimum.

    The per-line ``deltas`` already encode the full perpendicular separation; a
    rigid translation of the whole sub-group preserves that separation while
    repositioning it.  When a line's diagonal start sits at the
    ``MIN_STRAIGHT_EDGE`` floor (a fan-out hub with a short lead-in), the
    symmetric spread would drive some lines' stubs below the floor; sliding the
    bundle toward the side with room restores the full separation that a
    per-line clamp would otherwise null.  ``ai`` is the translation axis (0 for
    the X-shifted LR regime, 1 for the Y-shifted baked regime); the bounding
    stubs run along that same axis.

    Returns the shift closest to zero within the feasible band (zero when the
    raw deltas already fit, so an un-clamped bundle is untouched), or ``None``
    when no shift fits -- the cramped case the caller falls back to clamping.
    """
    los: list[float] = []
    his: list[float] = []
    for rp, delta in zip(group, deltas):
        src_min, tgt_min = _stub_minimums(rp)
        # sign * (P1[ai] + delta + c - bound_src) >= 0  and
        # sign * (bound_tgt - (P2[ai] + delta + c)) >= 0
        a = rp.points[1][ai] + delta - (rp.points[0][ai] + sign * src_min)
        b = (rp.points[3][ai] - sign * tgt_min) - (rp.points[2][ai] + delta)
        if sign > 0:
            los.append(-a)
            his.append(b)
        else:
            los.append(b)
            his.append(-a)
    lo, hi = max(los), min(his)
    if lo > hi + COORD_TOLERANCE_FINE:
        return None
    return min(max(0.0, lo), hi)


def _baked_spread_deltas(
    group: list[RoutedPath],
    offsets: list[float],
    center: float,
    d_a: float,
    d_b: float,
    hypot: float,
    bi: int,
) -> list[float]:
    """Per-line Y deltas that lift a baked bundle to a full perpendicular gap.

    A baked route already carries its offset baked along X, so its lines start
    with a perpendicular separation of ``OFFSET_STEP * sin(theta)`` on the
    diagonal.  Each line's current signed perpendicular offset is read straight
    from that baked X displacement, then a Y delta is added that drives the
    separation up to the full ``|off - center|`` while preserving the line
    ordering -- the sign of the baked offset is honoured rather than assumed, so
    whichever way the builder baked the fan, the lines splay apart.
    """
    # Baked diagonals spread along Y (ai == 1), so the baked baseline sits on X.
    dx_diag, dy_diag = d_b, d_a
    centroid_x = sum(rp.points[1][bi] for rp in group) / len(group)
    deltas: list[float] = []
    for rp, off in zip(group, offsets):
        rx = rp.points[1][bi] - centroid_x
        # Keep each line on the side its baked offset already places it (the
        # sign of its current perpendicular, rx * dy_diag), scaled up to a full
        # gap.
        target = (1.0 if rx * dy_diag >= 0 else -1.0) * abs(off - center)
        if abs(dx_diag) <= COORD_TOLERANCE_FINE:
            deltas.append(0.0)
        else:
            deltas.append((rx * dy_diag - target * hypot) / dx_diag)
    return deltas


def _shift_diagonal(rp: RoutedPath, delta: float, ai: int) -> None:
    """Translate a route's two diagonal waypoints by *delta* along axis *ai*."""
    for idx in (1, 2):
        p = rp.points[idx]
        rp.points[idx] = (p[0] + delta, p[1]) if ai == 0 else (p[0], p[1] + delta)


def _apply_diagonal_spread(
    group: list[RoutedPath],
    station_id: str,
    *,
    ctx: _RoutingCtx,
) -> None:
    """Spread a diagonal sub-group so its lines keep a true perpendicular gap.

    All routes in *group* share one diagonal direction.  Each line's signed
    offset becomes a translation of both diagonal waypoints that restores the
    full ``OFFSET_STEP`` perpendicular separation the fixed-axis offset loses on
    a diagonal.  The translation runs along the axis the baseline offset does
    *not* use: X for the render-time Y-offset (LR) regime, Y for the baked X
    regime (TB / inter-section), so on either the lines fan to a real gap rather
    than collapsing onto one stroke.
    """
    # Only reached from _spread_diagonal_bundles, which returns early on None.
    assert ctx.station_offsets is not None
    offsets = [ctx.station_offsets.get((station_id, rp.line_id), 0.0) for rp in group]
    center = sum(offsets) / len(offsets)

    rep = group[0]
    # Baked routes carry their offset along X already, so the complementary
    # spread runs along Y; render-time routes are the mirror image.
    ai = 1 if rep.offsets_applied else 0
    bi = 1 - ai
    d_a = rep.points[2][ai] - rep.points[1][ai]
    d_b = rep.points[2][bi] - rep.points[1][bi]
    sign = 1.0 if d_a >= 0 else -1.0
    hypot = math.hypot(d_a, d_b)

    if rep.offsets_applied:
        deltas = _baked_spread_deltas(group, offsets, center, d_a, d_b, hypot, bi)
    else:
        # A Y-only offset under-separates a diagonal: its perpendicular component
        # is only OFFSET_STEP times the sine of the diagonal's angle to the Y
        # axis.  This scale restores the full step; for a 45-degree leg it is
        # sqrt(2) - 1 ~ 0.414.
        abs_b = abs(d_b)
        scale = (hypot - abs(d_a)) / abs_b if abs_b > COORD_TOLERANCE_FINE else 0.0
        cross_sign = -1.0 if d_b > 0 else 1.0
        deltas = [cross_sign * (off - center) * scale * sign for off in offsets]

    shift = _fit_spread_translation(group, deltas, sign, ai)

    if shift is not None:
        for rp, delta in zip(group, deltas):
            _shift_diagonal(rp, delta + shift, ai)
        return

    # No uniform shift fits both stubs: the lead-in is too short for the full
    # separation, so clamp each line back to the floor for the best partial.
    for rp, delta in zip(group, deltas):
        src_min, tgt_min = _stub_minimums(rp)
        bound_src = rp.points[0][ai] + sign * src_min
        bound_tgt = rp.points[3][ai] - sign * tgt_min
        overshoot = max(
            sign * (bound_src - (rp.points[1][ai] + delta)),
            sign * ((rp.points[2][ai] + delta) - bound_tgt),
        )
        if overshoot > 0 and abs(delta) > COORD_TOLERANCE_FINE:
            delta *= max(0.0, 1.0 - overshoot / abs(delta))
        _shift_diagonal(rp, delta, ai)


@dataclass
class _BubbleCtx:
    """Pre-computed indexes for bubble-centering logic."""

    # Fork/join adjacency from the full edge list
    all_sources: dict[str, set[str]]
    all_targets: dict[str, set[str]]
    # 4-point diagonal routes indexed by station
    incoming: dict[str, list[RoutedPath]]
    outgoing: dict[str, list[RoutedPath]]
    # 2-point flat routes indexed by station
    flat_incoming: dict[str, list[RoutedPath]]
    flat_outgoing: dict[str, list[RoutedPath]]
    # Physically distinct diagonal convergence/divergence points
    diag_in_sources: dict[str, set[str]]
    diag_out_targets: dict[str, set[str]]
    # Snapshot of station X before any moves
    original_x: dict[str, float]
    # True fan-out divergence hubs (matches engine._divergence_target_ys):
    # >= 2 outbound real-station targets at distinct Ys, with at least one
    # above and one below the station's own Y.
    divergence_anchors: set[str]


def _build_bubble_ctx(routes: list[RoutedPath], graph: MetroGraph) -> _BubbleCtx:
    """Build indexes for bubble-station centering."""
    # Imported here to avoid a top-level cycle (engine does not depend on
    # routing, so this one-way import is safe).
    from nf_metro.layout.engine import _divergence_target_ys

    all_sources: dict[str, set[str]] = defaultdict(set)
    all_targets: dict[str, set[str]] = defaultdict(set)
    for edge in graph.edges:
        all_targets[edge.source].add(edge.target)
        all_sources[edge.target].add(edge.source)

    incoming: dict[str, list[RoutedPath]] = defaultdict(list)
    outgoing: dict[str, list[RoutedPath]] = defaultdict(list)
    flat_incoming: dict[str, list[RoutedPath]] = defaultdict(list)
    flat_outgoing: dict[str, list[RoutedPath]] = defaultdict(list)
    diag_in_sources: dict[str, set[str]] = defaultdict(set)
    diag_out_targets: dict[str, set[str]] = defaultdict(set)

    for rp in routes:
        if len(rp.points) == 2:
            flat_incoming[rp.edge.target].append(rp)
            flat_outgoing[rp.edge.source].append(rp)
            continue
        if not _is_diagonal_route(rp):
            continue
        incoming[rp.edge.target].append(rp)
        outgoing[rp.edge.source].append(rp)
        diag_in_sources[rp.edge.target].add(rp.edge.source)
        diag_out_targets[rp.edge.source].add(rp.edge.target)

    original_x = {sid: s.x for sid, s in graph.stations.items() if not s.is_port}

    return _BubbleCtx(
        all_sources=all_sources,
        all_targets=all_targets,
        incoming=incoming,
        outgoing=outgoing,
        flat_incoming=flat_incoming,
        flat_outgoing=flat_outgoing,
        diag_in_sources=diag_in_sources,
        diag_out_targets=diag_out_targets,
        original_x=original_x,
        divergence_anchors=_divergence_target_ys(graph),
    )


_StationMoveCandidate = tuple[
    float, list[RoutedPath], list[RoutedPath], list[RoutedPath], list[RoutedPath]
]


def _is_internal_station(graph: MetroGraph, sid: str) -> bool:
    """A visible, non-port internal station."""
    st = graph.stations.get(sid)
    return st is not None and not st.is_port and not st.is_hidden


def _is_chain_predecessor(graph: MetroGraph, ctx: _BubbleCtx, sid: str) -> bool:
    """Internal upstream station that acts as a flat-chain predecessor.

    When a station being considered for centring has a flat-side connection
    coming FROM ``sid``, this predicate decides whether ``sid`` should block
    centring.  Normal internal stations do block it.  A true fan-out
    divergence hub (matching ``engine._divergence_target_ys``: >= 2 outbound
    real-station targets at distinct Ys, with at least one above and one below
    the hub's own Y) is exempt: its flat-side connection to one branch is
    incidental (induced by grid snapping the hub onto that branch's track), not
    a topological chain.  Without this exemption the branch's column would fail
    to centre.

    Exemption applies only to the upstream/source side of a flat connection.
    Downstream chain predecessors (an anchor sitting as the target of a flat
    connection from the station being centred) reflect a natural same-Y chain,
    not a snap artefact, and are still treated as chain-internal.
    """
    if not _is_internal_station(graph, sid):
        return False
    return sid not in ctx.divergence_anchors


def _classify_centering_routes(
    ctx: _BubbleCtx,
    sid: str,
    in_routes: list[RoutedPath],
    out_routes: list[RoutedPath],
    flat_in: list[RoutedPath],
    flat_out: list[RoutedPath],
) -> (
    tuple[
        RoutedPath | None, RoutedPath | None, RoutedPath | None, RoutedPath | None, bool
    ]
    | None
):
    """Pick the routes bounding the flat segment, or None if not centerable.

    Returns ``(in_rp, out_rp, flat_in_rp, flat_out_rp, multi_diag)``.
    """
    is_fork_join = (
        len(ctx.all_targets.get(sid, set())) > 1
        or len(ctx.all_sources.get(sid, set())) > 1
    )

    in_rp = None
    out_rp = None
    flat_in_rp = None
    flat_out_rp = None

    # Count physically distinct edges (unique source-target pairs).
    n_unique_in = len(set((rp.edge.source, rp.edge.target) for rp in in_routes))
    n_unique_out = len(set((rp.edge.source, rp.edge.target) for rp in out_routes))
    n_unique_flat_in = len(set((rp.edge.source, rp.edge.target) for rp in flat_in))
    n_unique_flat_out = len(set((rp.edge.source, rp.edge.target) for rp in flat_out))

    multi_diag = False
    if not is_fork_join and (
        (n_unique_in + n_unique_flat_in) >= 1
        and n_unique_out >= 1
        and (
            n_unique_in > 1
            or n_unique_out > 1
            or (n_unique_in >= 1 and n_unique_flat_in >= 1)
        )
    ):
        in_rp = in_routes[0] if in_routes else None
        flat_in_rp = flat_in[0] if (not in_routes and flat_in) else None
        out_rp = out_routes[0]
        multi_diag = True
    elif is_fork_join:
        return None
    elif n_unique_in == 1 and n_unique_out == 1:
        in_rp = in_routes[0]
        out_rp = out_routes[0]
    elif n_unique_in == 0 and n_unique_flat_in == 1 and n_unique_out == 1:
        flat_in_rp = flat_in[0]
        out_rp = out_routes[0]
    elif n_unique_in == 1 and n_unique_out == 0 and n_unique_flat_out == 1:
        in_rp = in_routes[0]
        flat_out_rp = flat_out[0]
    else:
        return None
    return in_rp, out_rp, flat_in_rp, flat_out_rp, multi_diag


def _flat_connects_to_internal_chain(
    graph: MetroGraph,
    ctx: _BubbleCtx,
    multi_diag: bool,
    flat_in_rp: RoutedPath | None,
    flat_out_rp: RoutedPath | None,
    flat_in: list[RoutedPath],
    flat_out: list[RoutedPath],
) -> bool:
    """True when a flat connection ties the station into an internal chain.

    Upstream sources may be fork-hub-exempted (a snap-induced flat from a true
    divergence anchor does not represent a real chain).  Downstream targets are
    checked strictly: a same-Y predecessor->successor pair on a downstream
    internal station is a natural chain regardless of whether the successor
    happens to be a divergence anchor.
    """
    if flat_in_rp and _is_chain_predecessor(graph, ctx, flat_in_rp.edge.source):
        return True
    if flat_out_rp and _is_internal_station(graph, flat_out_rp.edge.target):
        return True
    if multi_diag:
        if any(_is_chain_predecessor(graph, ctx, r.edge.source) for r in flat_in):
            return True
        if any(_is_internal_station(graph, r.edge.target) for r in flat_out):
            return True
    return False


def _centering_candidate(
    graph: MetroGraph, ctx: _BubbleCtx, sid: str, station: Station
) -> _StationMoveCandidate | None:
    """Centre one station's diagonals in place, or return a move candidate.

    Simple single-diagonal-per-side cases shift both diagonals to equalise the
    flat runs and return None.  Complex cases (shared bundles, flat+diagonal
    mixes) return a station-move candidate for the second pass.
    """
    in_routes = ctx.incoming.get(sid, [])
    out_routes = ctx.outgoing.get(sid, [])
    flat_in = ctx.flat_incoming.get(sid, [])
    flat_out = ctx.flat_outgoing.get(sid, [])

    classified = _classify_centering_routes(
        ctx, sid, in_routes, out_routes, flat_in, flat_out
    )
    if classified is None:
        return None
    in_rp, out_rp, flat_in_rp, flat_out_rp, multi_diag = classified

    # Check bundle convergence/divergence at neighbours.
    shared_source = False
    shared_target = False
    if out_rp:
        out_tgt = graph.stations.get(out_rp.edge.target)
        if len(ctx.diag_in_sources.get(out_rp.edge.target, set())) > 1 and not (
            out_tgt and out_tgt.is_port
        ):
            shared_target = True
    if in_rp:
        in_src = graph.stations.get(in_rp.edge.source)
        if len(ctx.diag_out_targets.get(in_rp.edge.source, set())) > 1 and not (
            in_src and in_src.is_port
        ):
            shared_source = True

    # Determine X extent of the flat segment at station Y.
    if multi_diag:
        in_xs = [r.points[2][0] for r in in_routes]
        in_xs += [r.points[0][0] for r in flat_in]
        out_xs = [r.points[1][0] for r in out_routes]
        in_diag_end_x = max(in_xs) if in_xs else station.x
        out_diag_start_x = min(out_xs) if out_xs else station.x
    elif in_rp:
        in_diag_end_x = in_rp.points[2][0]
    else:
        assert flat_in_rp is not None
        in_diag_end_x = flat_in_rp.points[0][0]

    if not multi_diag:
        if out_rp:
            out_diag_start_x = out_rp.points[1][0]
        else:
            assert flat_out_rp is not None
            out_diag_start_x = flat_out_rp.points[-1][0]

    in_flat = station.x - in_diag_end_x
    out_flat = out_diag_start_x - station.x

    if abs(in_flat) < 1 or abs(out_flat) < 1:
        return None
    if abs(in_flat - out_flat) < 1:
        return None

    has_flat_side = flat_in_rp is not None or flat_out_rp is not None

    if (has_flat_side or multi_diag) and _flat_connects_to_internal_chain(
        graph, ctx, multi_diag, flat_in_rp, flat_out_rp, flat_in, flat_out
    ):
        return None

    if shared_source or shared_target or has_flat_side or multi_diag:
        new_x = (in_diag_end_x + out_diag_start_x) / 2
        return (new_x, in_routes, flat_in, out_routes, flat_out)

    # Simple case: shift both diagonals to equalise the flats.
    shift = (in_flat - out_flat) / 2

    if abs(shift) > min(abs(in_flat), abs(out_flat)):
        return None

    # Guard: don't shift in convergence/divergence bundles.  Bypass V
    # helpers have no marker so the convergence-guard doesn't apply.
    if not is_bypass_v(sid):
        if out_rp and len(ctx.diag_in_sources.get(out_rp.edge.target, set())) > 1:
            return None
        if in_rp and len(ctx.diag_out_targets.get(in_rp.edge.source, set())) > 1:
            return None

    for rp in in_routes:
        rp.points[1] = (rp.points[1][0] + shift, rp.points[1][1])
        rp.points[2] = (rp.points[2][0] + shift, rp.points[2][1])
    for rp in out_routes:
        rp.points[1] = (rp.points[1][0] + shift, rp.points[1][1])
        rp.points[2] = (rp.points[2][0] + shift, rp.points[2][1])
    return None


def _collect_centering_candidates(
    graph: MetroGraph, ctx: _BubbleCtx
) -> dict[str, _StationMoveCandidate]:
    """First pass: shift simple diagonals and collect station-move candidates.

    For stations with a single diagonal on each side and no bundle conflicts,
    shifts both diagonals to equalise the flat runs.  For more complex cases
    (shared bundles, flat+diagonal mixes), collects a station-move candidate
    for the second pass.
    """
    station_move_candidates: dict[str, _StationMoveCandidate] = {}
    for sid, station in graph.stations.items():
        if station.is_port:
            continue
        if station.is_hidden and not is_bypass_v(sid):
            continue
        candidate = _centering_candidate(graph, ctx, sid, station)
        if candidate is not None:
            station_move_candidates[sid] = candidate
    return station_move_candidates


def _apply_station_moves(
    graph: MetroGraph,
    candidates: dict[str, _StationMoveCandidate],
    original_x: dict[str, float],
    moves: dict[str, float],
) -> None:
    """Second pass: record station-move requests with companion consensus.

    Only moves a station when all column companions (visible stations at
    the same original X in the same section) are also candidates.  This
    preserves column alignment when only some stations want to centre.

    The X-target is recorded in ``moves`` (a request the render path applies);
    the routes bounding the station are adjusted here, since those are routing
    output.  ``graph.stations`` is left untouched so routing is placement-pure.
    """
    for sid, (
        new_x,
        in_routes,
        flat_in,
        out_routes,
        flat_out,
    ) in candidates.items():
        station = graph.stations[sid]
        # Hidden bypass V helpers have no marker, so column alignment
        # with visible companions isn't a visible concern - centre them
        # without requiring companion consensus.
        skip_companion_check = is_bypass_v(sid)
        if not skip_companion_check and abs(new_x - station.x) > STATION_MOVE_TOLERANCE:
            ox = original_x.get(sid, station.x)
            companions = []
            for other_sid, other_ox in original_x.items():
                if other_sid == sid:
                    continue
                if abs(other_ox - ox) > 1:
                    continue
                other = graph.stations.get(other_sid)
                if not other or other.is_port or other.is_hidden:
                    continue
                if other.section_id != station.section_id:
                    continue
                if abs(other.y - station.y) > 1:
                    companions.append(other_sid)
            if companions:
                if any(c not in candidates for c in companions):
                    continue

        moves[sid] = new_x
        for r in in_routes:
            r.points[-1] = (new_x, r.points[-1][1])
        for r in flat_in:
            r.points[-1] = (new_x, r.points[-1][1])
        for r in out_routes:
            r.points[0] = (new_x, r.points[0][1])
        for r in flat_out:
            r.points[0] = (new_x, r.points[0][1])


def _align_uncentered_siblings(
    routes: list[RoutedPath],
    graph: MetroGraph,
    original_x: dict[str, float],
    moves: dict[str, float],
) -> None:
    """Post-pass: drag unmoved stations to match their centered siblings.

    Groups stations by (section, original_x).  Only operates when moved
    stations disagree (spread > 1px): finds the majority X position and
    realigns outliers and unmoved stations to match.

    Reads each station's settled X from ``moves`` (the requests
    :func:`_apply_station_moves` recorded), falling back to ``original_x``;
    its own realignments are recorded back into ``moves`` rather than written
    to ``graph.stations``, so routing stays placement-pure.
    """

    def settled_x(sid: str) -> float:
        return moves.get(sid, original_x[sid])

    col_groups: dict[tuple[str | None, float], list[str]] = defaultdict(list)
    for sid, s in graph.stations.items():
        if s.is_port or s.is_hidden:
            continue
        ox = original_x.get(sid)
        if ox is None:
            continue
        col_groups[(s.section_id, round(ox, 1))].append(sid)

    routes_by_src: dict[str, list[RoutedPath]] = defaultdict(list)
    routes_by_tgt: dict[str, list[RoutedPath]] = defaultdict(list)
    for rp in routes:
        routes_by_src[rp.edge.source].append(rp)
        routes_by_tgt[rp.edge.target].append(rp)

    for group in col_groups.values():
        if len(group) < 3:
            continue
        moved = [
            sid
            for sid in group
            if abs(settled_x(sid) - original_x[sid]) > STATION_MOVE_TOLERANCE
        ]
        unmoved = [
            sid
            for sid in group
            if abs(settled_x(sid) - original_x[sid]) <= STATION_MOVE_TOLERANCE
        ]
        if not moved:
            continue

        moved_xs = [settled_x(sid) for sid in moved]
        if max(moved_xs) - min(moved_xs) <= 1.0:
            continue
        # Moved stations disagree on target X.  Find the majority
        # position and treat outliers as needing alignment too.
        rounded = [round(x, 1) for x in moved_xs]
        ((majority_x, majority_count),) = Counter(rounded).most_common(1)
        if majority_count <= len(moved) / 2:
            continue  # no clear majority, skip
        outliers = [
            sid
            for sid, x in zip(moved, moved_xs)
            if abs(round(x, 1) - majority_x) > 1.0
        ]
        if not outliers:
            continue
        unmoved = unmoved + outliers
        target_x = majority_x

        for sid in unmoved:
            old_x = settled_x(sid)
            moves[sid] = target_x
            for rp in routes_by_src.get(sid, []):
                if abs(rp.points[0][0] - old_x) < STATION_MOVE_TOLERANCE:
                    rp.points[0] = (target_x, rp.points[0][1])
            for rp in routes_by_tgt.get(sid, []):
                if abs(rp.points[-1][0] - old_x) < STATION_MOVE_TOLERANCE:
                    rp.points[-1] = (target_x, rp.points[-1][1])


def _center_bubble_stations(
    routes: list[RoutedPath], graph: MetroGraph
) -> dict[str, float]:
    """Shift diagonals so bubble stations sit centred on their flat segments.

    A "bubble station" branches off the trunk at a different Y, with a
    diagonal on each side.  The fork/join bias in ``_route_diagonal``
    keeps diagonals symmetric at the shared station but leaves the bubble
    station off-centre.  This pass detects such stations and shifts both
    adjacent diagonals by the same amount to equalise the flat runs.

    Runs in three phases:

    1. **Candidate collection** - identifies stations needing centering;
       shifts simple diagonals directly, collects complex cases as
       station-move candidates.
    2. **Station moves** - records moves only when all column companions
       also want to move (preserving column alignment).
    3. **Sibling alignment** - drags remaining unmoved stations to match
       the majority of their centered column group.

    Route points are adjusted in place (routing output); the per-station
    X-targets are returned as ``{station_id: x}`` move requests for the
    render path to apply, so ``route_edges`` leaves ``graph.stations`` intact.
    """
    ctx = _build_bubble_ctx(routes, graph)
    candidates = _collect_centering_candidates(graph, ctx)
    moves: dict[str, float] = {}
    _apply_station_moves(graph, candidates, ctx.original_x, moves)
    _align_uncentered_siblings(routes, graph, ctx.original_x, moves)
    return moves


def _clear_bypass_v_label_strikes(routes: list[RoutedPath], ctx: _RoutingCtx) -> None:
    """Lengthen a bypass V's flat run so its diagonal clears the bypassed label.

    A bypass V dips its line below (or rises above) the station it routes around
    and climbs back to the trunk on the far side.  When that station carries a
    wide name label, the climbing diagonal can rake the label's glyph ink on the
    overrun side -- a strike that neither a label side-flip nor a column-pitch
    widening relocates, because the V sits a fixed track offset from the station
    rather than a grid-column multiple.

    For each V whose bypassed-station label box ``compute_layout`` recorded in
    ``graph.bypass_label_obstacles``, this seats the V-side corner of any leg
    whose diagonal crosses that box just outside the box on the overrun side, so
    the diagonal climbs clear of the glyphs.  The far diagonal corner follows
    within the room left before the leg's other endpoint, keeping a drawable
    transition.  Legs whose diagonal already clears the box are untouched, so a
    bypass V beside a narrow (or oppositely-placed) label is left as routed.

    When the room before the leg's endpoint is too tight to fully seat the
    corner clear, the corner is pulled back only as far as a drawable
    ``CURVE_RADIUS`` transition allows, so clearance can be partial; the wired
    ``_guard_no_line_strikes_label`` is the backstop for that residual.
    """
    obstacles = ctx.graph.bypass_label_obstacles
    if not obstacles or ctx.station_offsets is None:
        return

    from nf_metro.render.svg import apply_route_offsets

    legs_by_v: dict[str, list[RoutedPath]] = defaultdict(list)
    for r in routes:
        for nid in (r.edge.source, r.edge.target):
            if nid in obstacles:
                legs_by_v[nid].append(r)

    for vid, legs in legs_by_v.items():
        box = obstacles[vid]
        bx0, _by0, bx1, _by1 = box
        box_cx = (bx0 + bx1) / 2
        for r in legs:
            if not _is_diagonal_route(r):
                continue
            opts = apply_route_offsets(r, ctx.station_offsets)
            (dx1, dy1), (dx2, dy2) = opts[1], opts[2]
            if not segment_intersects_bbox(dx1, dy1, dx2, dy2, box):
                continue
            # On a 4-point bypass leg the V-adjacent corner is index 1 when the
            # V is the source and index 2 when it is the target; the far corner
            # climbs to the leg's other endpoint.
            v_idx, far_idx = (1, 2) if r.edge.source == vid else (2, 1)
            far_node = r.edge.target if v_idx == 1 else r.edge.source
            far_st = ctx.graph.stations.get(far_node)
            far_min = (
                CURVE_RADIUS + MIN_STRAIGHT_PORT
                if far_st is not None and far_st.is_port
                else MIN_STRAIGHT_EDGE
            )
            # Push the corner toward whichever label edge it overruns: the V
            # corner sits on the overrun side, so the half of the box it lies in
            # picks the direction.
            far_end_x = opts[3][0] if v_idx == 1 else opts[0][0]
            if opts[v_idx][0] >= box_cx:
                v_target = bx1 + LABEL_BBOX_MARGIN
                far_target = min(v_target + DIAGONAL_RUN, far_end_x - far_min)
                v_target = min(v_target, far_target - CURVE_RADIUS)
            else:
                v_target = bx0 - LABEL_BBOX_MARGIN
                far_target = max(v_target - DIAGONAL_RUN, far_end_x + far_min)
                v_target = max(v_target, far_target + CURVE_RADIUS)
            # ``apply_route_offsets`` shifts only Y, so these X targets apply
            # directly to the raw waypoints.
            pts = list(r.points)
            pts[v_idx] = (v_target, pts[v_idx][1])
            pts[far_idx] = (far_target, pts[far_idx][1])
            r.points = pts
