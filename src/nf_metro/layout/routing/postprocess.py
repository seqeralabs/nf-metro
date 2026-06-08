"""Post-routing passes: diagonal bundle spread and bubble-station centring."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass

from nf_metro.layout.constants import (
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    MIN_STRAIGHT_EDGE,
    STATION_MOVE_TOLERANCE,
)
from nf_metro.layout.routing.common import (
    RoutedPath,
)
from nf_metro.layout.routing.context import (
    _RoutingCtx,
)
from nf_metro.parser.model import (
    MetroGraph,
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
    """Translate diagonal start/end X per-line so bundled diagonals spread apart.

    For L-shapes the ``delta`` from :func:`l_shape_radii` translates each
    line's vertical channel X, giving perpendicular separation.  Diagonals
    lack this: all lines share the same ``diag_start_x`` / ``diag_end_x``,
    so the only separation is the Y offset (~2.1 px perpendicular on a 45-
    degree line).  This post-pass adds a complementary X translation derived
    from the per-line Y offset so that bundled diagonals are parallel but
    horizontally spread.
    """
    if ctx.station_offsets is None:
        return

    # Collect diagonal routes grouped by shared fork / join station.
    fork_groups: dict[str, list[RoutedPath]] = defaultdict(list)
    join_groups: dict[str, list[RoutedPath]] = defaultdict(list)

    for rp in routes:
        if not _is_diagonal_route(rp):
            continue
        # Skip bypass V hops: the two legs (P -> V and V -> T) are
        # spread independently and the V-side MIN_STRAIGHT_EDGE bound
        # forces asymmetric clamping, producing a visible kink at V.
        # Bypass V routes are short and the perpendicular separation
        # from per-line Y offsets alone is sufficient for visibility.
        if rp.edge.source.startswith("__bypass_") or rp.edge.target.startswith(
            "__bypass_"
        ):
            continue
        if rp.edge.source in ctx.fork_stations:
            fork_groups[rp.edge.source].append(rp)
        if rp.edge.target in ctx.join_stations:
            join_groups[rp.edge.target].append(rp)

    # Track routes already spread so we don't double-shift a route that
    # appears in both a fork and a join group.
    spread: set[tuple[str, str, str]] = set()

    def _edge_key(rp: RoutedPath) -> tuple[str, str, str]:
        return (rp.edge.source, rp.edge.target, rp.line_id)

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


def _apply_diagonal_spread(
    group: list[RoutedPath],
    station_id: str,
    *,
    ctx: _RoutingCtx,
) -> None:
    """Compute and apply per-line X deltas to a diagonal sub-group.

    All routes in *group* share the same diagonal direction (up or down).
    The delta translates both diagonal waypoints (indices 1 and 2) so
    the diagonal segments are parallel but horizontally spread.
    """
    # Only reached from _spread_diagonal_bundles, which returns early on None.
    assert ctx.station_offsets is not None
    offsets = [ctx.station_offsets.get((station_id, rp.line_id), 0.0) for rp in group]
    center = sum(offsets) / len(offsets)

    rep = group[0]
    dx = rep.points[2][0] - rep.points[1][0]
    dy = rep.points[2][1] - rep.points[1][1]
    sign = 1.0 if dx >= 0 else -1.0
    down_sign = -1.0 if dy > 0 else 1.0

    # On a diagonal at angle theta, Y-only offset gives reduced
    # perpendicular separation (OFFSET_STEP * cos(theta)).  This scale
    # restores the full OFFSET_STEP: (hypot - |dx|) / |dy|.
    # For 45 degrees: sqrt(2) - 1 ~ 0.414.
    hypot = math.hypot(dx, dy)
    abs_dy = abs(dy)
    spread_scale = (hypot - abs(dx)) / abs_dy if abs_dy > COORD_TOLERANCE_FINE else 0.0

    for rp, offset in zip(group, offsets):
        delta = down_sign * (offset - center) * spread_scale * sign

        # Clamp so the horizontal runs don't collapse below minimum.
        bound_src = rp.points[0][0] + sign * MIN_STRAIGHT_EDGE
        bound_tgt = rp.points[3][0] - sign * MIN_STRAIGHT_EDGE
        overshoot = max(
            sign * (bound_src - (rp.points[1][0] + delta)),
            sign * ((rp.points[2][0] + delta) - bound_tgt),
        )
        if overshoot > 0 and abs(delta) > COORD_TOLERANCE_FINE:
            delta *= max(0.0, 1.0 - overshoot / abs(delta))

        rp.points[1] = (rp.points[1][0] + delta, rp.points[1][1])
        rp.points[2] = (rp.points[2][0] + delta, rp.points[2][1])


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


def _collect_centering_candidates(
    graph: MetroGraph, ctx: _BubbleCtx
) -> dict[str, _StationMoveCandidate]:
    """First pass: shift simple diagonals and collect station-move candidates.

    For stations with a single diagonal on each side and no bundle
    conflicts, shifts both diagonals to equalise the flat runs.
    For more complex cases (shared bundles, flat+diagonal mixes),
    collects a station-move candidate for the second pass.
    """
    station_move_candidates: dict[str, _StationMoveCandidate] = {}

    def _is_internal(sid: str) -> bool:
        st = graph.stations.get(sid)
        return st is not None and not st.is_port and not st.is_hidden

    def _is_chain_predecessor(sid: str) -> bool:
        """Internal upstream station that acts as a flat-chain predecessor.

        When a station being considered for centring has a flat-side
        connection coming FROM ``sid``, this predicate decides whether
        ``sid`` should block centring.  Normal internal stations do
        block it.  A true fan-out divergence hub (matching
        ``engine._divergence_target_ys``: >= 2 outbound real-station
        targets at distinct Ys, with at least one above and one below
        the hub's own Y) is exempt: its flat-side connection to one
        branch is incidental (induced by grid snapping the hub onto
        that branch's track), not a topological chain.  Without this
        exemption the branch's column would fail to centre.

        Exemption applies only to the upstream/source side of a flat
        connection.  Downstream chain predecessors (an anchor sitting
        as the target of a flat connection from the station being
        centred) reflect a natural same-Y chain, not a snap artefact,
        and are still treated as chain-internal.
        """
        if not _is_internal(sid):
            return False
        return sid not in ctx.divergence_anchors

    for sid, station in graph.stations.items():
        if station.is_port:
            continue
        if station.is_hidden and not sid.startswith("__bypass_"):
            continue

        in_routes = ctx.incoming.get(sid, [])
        out_routes = ctx.outgoing.get(sid, [])
        flat_in = ctx.flat_incoming.get(sid, [])
        flat_out = ctx.flat_outgoing.get(sid, [])

        is_fork_join = (
            len(ctx.all_targets.get(sid, set())) > 1
            or len(ctx.all_sources.get(sid, set())) > 1
        )

        # Determine which routes bound the station's flat segment.
        in_rp = None
        out_rp = None
        flat_in_rp = None
        flat_out_rp = None

        # Count physically distinct edges (unique source-target pairs).
        n_unique_in = len(set((rp.edge.source, rp.edge.target) for rp in in_routes))
        n_unique_out = len(set((rp.edge.source, rp.edge.target) for rp in out_routes))
        n_unique_flat_in = len(set((rp.edge.source, rp.edge.target) for rp in flat_in))
        n_unique_flat_out = len(
            set((rp.edge.source, rp.edge.target) for rp in flat_out)
        )

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
            continue
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
            continue

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
            continue
        if abs(in_flat - out_flat) < 1:
            continue

        has_flat_side = flat_in_rp is not None or flat_out_rp is not None

        # Guard: skip when a flat connection goes to/from an internal
        # chain station.  Upstream sources may be fork-hub-exempted (a
        # snap-induced flat from a true divergence anchor does not
        # represent a real chain).  Downstream targets are checked
        # strictly: a same-Y predecessor->successor pair on a downstream
        # internal station is a natural chain regardless of whether the
        # successor happens to be a divergence anchor.
        if has_flat_side or multi_diag:
            flat_to_internal = False
            if flat_in_rp and _is_chain_predecessor(flat_in_rp.edge.source):
                flat_to_internal = True
            if flat_out_rp and _is_internal(flat_out_rp.edge.target):
                flat_to_internal = True
            if multi_diag:
                for r in flat_in:
                    if _is_chain_predecessor(r.edge.source):
                        flat_to_internal = True
                for r in flat_out:
                    if _is_internal(r.edge.target):
                        flat_to_internal = True
            if flat_to_internal:
                continue

        if shared_source or shared_target or has_flat_side or multi_diag:
            new_x = (in_diag_end_x + out_diag_start_x) / 2
            station_move_candidates[sid] = (
                new_x,
                in_routes,
                flat_in,
                out_routes,
                flat_out,
            )
            continue

        # Simple case: shift both diagonals to equalise the flats.
        shift = (in_flat - out_flat) / 2

        if abs(shift) > min(abs(in_flat), abs(out_flat)):
            continue

        # Guard: don't shift in convergence/divergence bundles.  Bypass
        # V helpers have no marker so the convergence-guard doesn't apply.
        is_bypass_v = sid.startswith("__bypass_")
        if not is_bypass_v:
            if out_rp and len(ctx.diag_in_sources.get(out_rp.edge.target, set())) > 1:
                continue
            if in_rp and len(ctx.diag_out_targets.get(in_rp.edge.source, set())) > 1:
                continue

        for rp in in_routes:
            rp.points[1] = (rp.points[1][0] + shift, rp.points[1][1])
            rp.points[2] = (rp.points[2][0] + shift, rp.points[2][1])
        for rp in out_routes:
            rp.points[1] = (rp.points[1][0] + shift, rp.points[1][1])
            rp.points[2] = (rp.points[2][0] + shift, rp.points[2][1])

    return station_move_candidates


def _apply_station_moves(
    graph: MetroGraph,
    candidates: dict[str, _StationMoveCandidate],
    original_x: dict[str, float],
) -> None:
    """Second pass: apply station-move candidates with companion consensus.

    Only moves a station when all column companions (visible stations at
    the same original X in the same section) are also candidates.  This
    preserves column alignment when only some stations want to centre.
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
        skip_companion_check = sid.startswith("__bypass_")
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

        station.x = new_x
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
) -> None:
    """Post-pass: drag unmoved stations to match their centered siblings.

    Groups stations by (section, original_x).  Only drags unmoved stations
    when a clear majority (>50%) of the group already moved to the same X.
    """
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
            if abs(graph.stations[sid].x - original_x[sid]) > STATION_MOVE_TOLERANCE
        ]
        unmoved = [
            sid
            for sid in group
            if abs(graph.stations[sid].x - original_x[sid]) <= STATION_MOVE_TOLERANCE
        ]
        if not moved:
            continue

        moved_xs = [graph.stations[sid].x for sid in moved]
        if max(moved_xs) - min(moved_xs) > 1.0:
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
        else:
            if not unmoved:
                continue
            if len(moved) <= len(unmoved):
                continue
            target_x = sum(moved_xs) / len(moved_xs)

        for sid in unmoved:
            old_x = graph.stations[sid].x
            graph.stations[sid].x = target_x
            for rp in routes_by_src.get(sid, []):
                if abs(rp.points[0][0] - old_x) < STATION_MOVE_TOLERANCE:
                    rp.points[0] = (target_x, rp.points[0][1])
            for rp in routes_by_tgt.get(sid, []):
                if abs(rp.points[-1][0] - old_x) < STATION_MOVE_TOLERANCE:
                    rp.points[-1] = (target_x, rp.points[-1][1])


def _center_bubble_stations(routes: list[RoutedPath], graph: MetroGraph) -> None:
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
    2. **Station moves** - applies moves only when all column companions
       also want to move (preserving column alignment).
    3. **Sibling alignment** - drags remaining unmoved stations to match
       the majority of their centered column group.
    """
    ctx = _build_bubble_ctx(routes, graph)
    candidates = _collect_centering_candidates(graph, ctx)
    _apply_station_moves(graph, candidates, ctx.original_x)
    _align_uncentered_siblings(routes, graph, ctx.original_x)
