"""Bridge detection for non-merging line crossings.

Two distinct metro lines may cross at a point that is *not* a shared
station, port, junction, or merge.  Drawn naively that reads as an
interchange.  A *bridge* disambiguates it: the lines of the "under" bundle
are interrupted by a short gap where they pass beneath the continuous
"over" bundle.

This module is the detection half: it finds genuine non-merging crossings
on the *rendered* polylines (offsets already applied) and reports, per
under-route, the gap span to break.  The drawing half lives in ``svg.py``.

A crossing is genuine only when:

* the two lines differ;
* the two crossing edges share no endpoint node (fan-out/fan-in/diamond
  reordering emanates from a shared fork and is not a crossing);
* the intersection is not within ``BRIDGE_NODE_TOLERANCE`` of any node the
  layout places (interchanges happen *at* nodes);
* the two segments are not near-parallel (offset bundle slivers barely
  intersect but do not visually cross);
* the under-line has room to break clear of its smoothed corners.

Crossings are grouped into clusters (one bundle crossing another counts as
one event).  Within a cluster the routes split cleanly into two bundles
(a 2-colouring of the crossing graph); every line of the *under* bundle is
broken by a single gap spanning the full width of the *over* bundle, so a
bundle reads as passing under as a whole rather than line by line.

Over/under is decided per cluster: when the two bundles use different lines
the senior (earlier-defined) bundle stays over, matching paint order; when
they share lines (e.g. a vertical merge bus crossing its own horizontal
connector) the more-horizontal bundle goes under, so the continuous bus is
not chopped at every connector it crosses.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from nf_metro.parser.model import Edge, MetroGraph
from nf_metro.render.constants import (
    BRIDGE_BUNDLE_GAP,
    BRIDGE_CLUSTER_RADIUS,
    BRIDGE_CORNER_CLEARANCE,
    BRIDGE_GAP_HALF,
    BRIDGE_MIN_ANGLE_DEG,
    BRIDGE_NODE_TOLERANCE,
    BRIDGE_PARALLEL_ANGLE_DEG,
)

__all__ = ["BridgeBreak", "compute_bridges"]

Point = tuple[float, float]


@dataclass(frozen=True)
class BridgeBreak:
    """A gap on an under-route's polyline where it passes under a crossing.

    The pen lifts between ``cut_a`` and ``cut_b`` (both on the straight run
    of segment ``seg_index``, ``cut_a`` nearer the segment start).
    """

    seg_index: int
    cut_a: Point
    cut_b: Point


@dataclass
class _Crossing:
    """An intersection of route ``a`` (on segment ``seg_a``) with route ``b``
    (on segment ``seg_b``), where ``a``/``b`` are indices into ``routes``."""

    a: int
    seg_a: int
    b: int
    seg_b: int
    point: Point


class BridgeInvariantError(AssertionError):
    """A computed bridge violated a structural invariant."""


def _segment_intersection(p1: Point, p2: Point, p3: Point, p4: Point) -> Point | None:
    """Proper interior intersection of segments p1p2 and p3p4, or None."""
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    x4, y4 = p4
    denom = (x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3)
    if abs(denom) < 1e-9:
        return None
    t = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / denom
    u = ((x3 - x1) * (y2 - y1) - (y3 - y1) * (x2 - x1)) / denom
    eps = 1e-6
    if eps < t < 1 - eps and eps < u < 1 - eps:
        return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))
    return None


def _segment_angle_deg(p: Point, q: Point) -> float:
    """Orientation of segment p->q in [0, 180)."""
    return math.degrees(math.atan2(q[1] - p[1], q[0] - p[0])) % 180.0


def _angle_between(a: float, b: float) -> float:
    """Acute angle between two orientations in [0, 90]."""
    diff = abs(a - b) % 180.0
    return min(diff, 180.0 - diff)


def _horizontalness(angle: float) -> float:
    """1 for horizontal, 0 for vertical."""
    return 1.0 - _angle_between(angle, 0.0) / 90.0


def _shares_endpoint(e1: Edge, e2: Edge) -> bool:
    return bool({e1.source, e1.target} & {e2.source, e2.target})


def _near_node(pt: Point, node_xy: list[Point]) -> bool:
    return any(
        abs(pt[0] - nx) < BRIDGE_NODE_TOLERANCE
        and abs(pt[1] - ny) < BRIDGE_NODE_TOLERANCE
        for nx, ny in node_xy
    )


def compute_bridges(
    graph: MetroGraph,
    routes: list,
    polylines: list[list[Point]],
    *,
    curve_radius: float,
) -> dict[int, list[BridgeBreak]]:
    """Find non-merging crossings and report per-under-route gap spans.

    ``routes`` and ``polylines`` are aligned: ``polylines[i]`` is the
    offset-applied geometry actually drawn for ``routes[i]``.  Returns a map
    from ``id(route)`` of each under-route to the gaps to break on it.
    """
    node_xy = [(s.x, s.y) for s in graph.stations.values()]
    crossings = _find_crossings(routes, polylines, node_xy)
    clusters = _cluster_crossings(crossings)

    line_priority = {lid: i for i, lid in enumerate(graph.lines)}
    back = len(line_priority)
    corner_clear = curve_radius + BRIDGE_CORNER_CLEARANCE

    by_line: dict[str, list[int]] = defaultdict(list)
    for idx, r in enumerate(routes):
        by_line[r.line_id].append(idx)

    breaks: dict[int, list[BridgeBreak]] = defaultdict(list)
    seen: dict[int, set[tuple]] = defaultdict(set)

    def add(route_idx: int, seg: int, span: tuple[Point, Point]) -> None:
        key = (
            seg,
            round(span[0][0]),
            round(span[0][1]),
            round(span[1][0]),
            round(span[1][1]),
        )
        if key in seen[route_idx]:
            return
        seen[route_idx].add(key)
        breaks[id(routes[route_idx])].append(
            BridgeBreak(seg_index=seg, cut_a=span[0], cut_b=span[1])
        )

    for cluster in clusters:
        for route_idx, seg, span in _cluster_gaps(
            cluster, routes, polylines, line_priority, back, corner_clear
        ):
            # Break every collinear route of this line through the span, not
            # just the one that crossed - sibling routes (same line, same
            # corridor) would otherwise fill the gap and hide the break.
            line_id = routes[route_idx].line_id
            for sib in by_line[line_id]:
                sib_seg = _segment_containing(polylines[sib], span, corner_clear)
                if sib_seg is not None:
                    add(sib, sib_seg, span)

    result = dict(breaks)
    _guard_bridges(routes, polylines, node_xy, result)
    return result


def _guard_bridges(
    routes: list,
    polylines: list[list[Point]],
    node_xy: list[Point],
    breaks: dict[int, list[BridgeBreak]],
) -> None:
    """Fail loudly if a bridge lands on a node (an interchange, not a
    crossing) or its gap is not collinear with the segment it breaks."""
    poly_by_id = {id(r): p for r, p in zip(routes, polylines)}
    for rid, bk_list in breaks.items():
        poly = poly_by_id[rid]
        for bk in bk_list:
            mx = (bk.cut_a[0] + bk.cut_b[0]) / 2
            my = (bk.cut_a[1] + bk.cut_b[1]) / 2
            if _near_node((mx, my), node_xy):
                raise BridgeInvariantError(
                    f"bridge gap at ({mx:.0f},{my:.0f}) sits on a node - "
                    "interchanges must not be bridged"
                )
            a, b = poly[bk.seg_index], poly[bk.seg_index + 1]
            for pt in (bk.cut_a, bk.cut_b):
                if not _point_on_segment(pt, a, b, 0.0):
                    raise BridgeInvariantError(
                        f"bridge gap end {pt} is not on segment "
                        f"{bk.seg_index} of its route"
                    )


def _find_crossings(
    routes: list, polylines: list[list[Point]], node_xy: list[Point]
) -> list[_Crossing]:
    """All genuine non-merging segment crossings between distinct lines."""
    out: list[_Crossing] = []
    for a in range(len(routes)):
        ra, pa = routes[a], polylines[a]
        for b in range(a + 1, len(routes)):
            rb, pb = routes[b], polylines[b]
            if ra.line_id == rb.line_id or _shares_endpoint(ra.edge, rb.edge):
                continue
            for i in range(len(pa) - 1):
                for j in range(len(pb) - 1):
                    pt = _segment_intersection(pa[i], pa[i + 1], pb[j], pb[j + 1])
                    if pt is None or _near_node(pt, node_xy):
                        continue
                    ang = _angle_between(
                        _segment_angle_deg(pa[i], pa[i + 1]),
                        _segment_angle_deg(pb[j], pb[j + 1]),
                    )
                    if ang < BRIDGE_MIN_ANGLE_DEG:
                        continue
                    out.append(_Crossing(a=a, seg_a=i, b=b, seg_b=j, point=pt))
    return out


def _cluster_crossings(crossings: list[_Crossing]) -> list[list[_Crossing]]:
    """Group crossings whose points lie within ``BRIDGE_CLUSTER_RADIUS``."""
    parent = list(range(len(crossings)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(crossings)):
        for j in range(i + 1, len(crossings)):
            pi, pj = crossings[i].point, crossings[j].point
            if (
                abs(pi[0] - pj[0]) <= BRIDGE_CLUSTER_RADIUS
                and abs(pi[1] - pj[1]) <= BRIDGE_CLUSTER_RADIUS
            ):
                parent[find(i)] = find(j)

    groups: dict[int, list[_Crossing]] = defaultdict(list)
    for i, c in enumerate(crossings):
        groups[find(i)].append(c)
    return list(groups.values())


def _two_colour(nodes: set[int], adj: dict[int, set[int]]) -> dict[int, int] | None:
    """2-colour the crossing graph; None if it is not bipartite."""
    colour: dict[int, int] = {}
    for start in sorted(nodes):
        if start in colour:
            continue
        colour[start] = 0
        queue = [start]
        while queue:
            n = queue.pop()
            for m in adj[n]:
                if m not in colour:
                    colour[m] = colour[n] ^ 1
                    queue.append(m)
                elif colour[m] == colour[n]:
                    return None
    return colour


Gap = tuple[Point, Point]


def _cluster_gaps(
    cluster: list[_Crossing],
    routes: list,
    polylines: list[list[Point]],
    line_priority: dict[str, int],
    back: int,
    corner_clear: float,
) -> list[tuple[int, int, Gap]]:
    """For one cluster, return (route_index, seg_index, gap) for every line of
    the under bundle.  The gap is the same span (in the shared crossing
    direction) for all parallel under-lines, so the bundle breaks with one
    aligned, uniform-width gap rather than ragged per-line gaps."""
    adj: dict[int, set[int]] = defaultdict(set)
    nodes: set[int] = set()
    seg_of: dict[int, int] = {}
    for c in cluster:
        adj[c.a].add(c.b)
        adj[c.b].add(c.a)
        nodes |= {c.a, c.b}
        seg_of[c.a] = c.seg_a
        seg_of[c.b] = c.seg_b

    colour = _two_colour(nodes, adj)
    if colour is None:
        return _per_pair_gaps(
            cluster, routes, polylines, line_priority, back, seg_of, corner_clear
        )

    group = {
        0: [n for n in nodes if colour[n] == 0],
        1: [n for n in nodes if colour[n] == 1],
    }
    under = _under_group(group, routes, polylines, line_priority, back, seg_of)
    over = nodes - under
    cross_pts = [
        c.point
        for c in cluster
        if (c.a in under and c.b in over) or (c.b in under and c.a in over)
    ]
    if not cross_pts:
        return []

    # A lone under-line travelling in the over-line's own bundle is a branch
    # diverging from that bundle, not an independent crossing - bridging it
    # leaves a single line broken beside its continuous bundle-mate.
    if len({routes[n].line_id for n in under}) == 1 and _bundled_with_over(
        sorted(under)[0],
        {routes[n].line_id for n in over},
        routes,
        polylines,
        seg_of,
        cross_pts,
    ):
        return []
    return _uniform_gaps(sorted(under), seg_of, polylines, cross_pts, corner_clear)


def _bundled_with_over(
    under_idx: int,
    over_line_ids: set[str],
    routes: list,
    polylines: list[list[Point]],
    seg_of: dict[int, int],
    cross_pts: list[Point],
) -> bool:
    """True if a route of the over-line runs parallel and adjacent to the
    under-line through the crossing (the over-line branches out of the
    under-line's bundle)."""
    poly = polylines[under_idx]
    seg = seg_of[under_idx]
    u_ang = _segment_angle_deg(poly[seg], poly[seg + 1])
    cx = sum(p[0] for p in cross_pts) / len(cross_pts)
    cy = sum(p[1] for p in cross_pts) / len(cross_pts)
    for ri, p in enumerate(polylines):
        if ri == under_idx or routes[ri].line_id not in over_line_ids:
            continue
        for i in range(len(p) - 1):
            if (
                _angle_between(_segment_angle_deg(p[i], p[i + 1]), u_ang)
                > BRIDGE_PARALLEL_ANGLE_DEG
            ):
                continue
            ax, ay = p[i]
            dx, dy = p[i + 1][0] - ax, p[i + 1][1] - ay
            length_sq = dx * dx + dy * dy
            if length_sq == 0:
                continue
            t = ((cx - ax) * dx + (cy - ay) * dy) / length_sq
            if not 0.0 <= t <= 1.0:
                continue
            if math.hypot(ax + t * dx - cx, ay + t * dy - cy) <= BRIDGE_BUNDLE_GAP:
                return True
    return False


def _uniform_gaps(
    under: list[int],
    seg_of: dict[int, int],
    polylines: list[list[Point]],
    cross_pts: list[Point],
    corner_clear: float,
) -> list[tuple[int, int, Gap]]:
    """Break every under-line at the same gap, centred on the over bundle.

    The gap is centred on the crossing midpoint and given equal padding on
    both sides; corner clearance shrinks both sides by the same amount (the
    most-constrained under-line decides), so the gap stays symmetric about
    the over-line and identical across the bundle rather than ragged."""
    rep = polylines[under[0]]
    rseg = seg_of[under[0]]
    ux, uy = _unit(rep[rseg], rep[rseg + 1])
    projs = [p[0] * ux + p[1] * uy for p in cross_pts]
    cmin, cmax = min(projs), max(projs)
    centre = (cmin + cmax) / 2.0
    over_half = (cmax - cmin) / 2.0

    geom = []
    sym_half = over_half + BRIDGE_GAP_HALF
    for ri in under:
        seg = seg_of[ri]
        a, b = polylines[ri][seg], polylines[ri][seg + 1]
        seg_len = math.hypot(b[0] - a[0], b[1] - a[1])
        if seg_len == 0:
            continue
        a_u = a[0] * ux + a[1] * uy
        sym_half = min(
            sym_half,
            centre - (a_u + corner_clear),
            a_u + seg_len - corner_clear - centre,
        )
        geom.append(
            (ri, seg, a, (b[0] - a[0]) / seg_len, (b[1] - a[1]) / seg_len, a_u, seg_len)
        )

    # Prefer a gap centred on the over bundle (symmetric, identical across the
    # bundle).  Where a segment is too short to centre a covering gap, fall
    # back to clamping each line independently so the crossing still breaks.
    symmetric = sym_half >= over_half and sym_half > 0

    out: list[tuple[int, int, Gap]] = []
    for ri, seg, a, sux, suy, a_u, seg_len in geom:
        if symmetric:
            lo_s = centre - sym_half - a_u
            hi_s = centre + sym_half - a_u
        else:
            lo_s = max(corner_clear, cmin - a_u - BRIDGE_GAP_HALF)
            hi_s = min(seg_len - corner_clear, cmax - a_u + BRIDGE_GAP_HALF)
            if lo_s >= hi_s:
                continue
        out.append(
            (
                ri,
                seg,
                (
                    (a[0] + sux * lo_s, a[1] + suy * lo_s),
                    (a[0] + sux * hi_s, a[1] + suy * hi_s),
                ),
            )
        )
    return out


def _under_group(
    group: dict[int, list[int]],
    routes: list,
    polylines: list[list[Point]],
    line_priority: dict[str, int],
    back: int,
    seg_of: dict[int, int],
) -> set[int]:
    """Pick which colour group is the under bundle (the one to break)."""
    lines0 = {routes[n].line_id for n in group[0]}
    lines1 = {routes[n].line_id for n in group[1]}

    def min_prio(lines: set[str]) -> int:
        return min((line_priority.get(lid, back) for lid in lines), default=back)

    if lines0.isdisjoint(lines1):
        # Distinct lines: senior (earlier-defined) bundle stays over.
        under_colour = 1 if min_prio(lines0) <= min_prio(lines1) else 0
    else:
        # Shared lines (e.g. a vertical bus crossing its own horizontal
        # connector): the more-horizontal bundle goes under, so the
        # continuous bus is not chopped at every connector it crosses.
        h0 = _group_horizontalness(group[0], polylines, seg_of)
        h1 = _group_horizontalness(group[1], polylines, seg_of)
        under_colour = 0 if h0 >= h1 else 1
    return set(group[under_colour])


def _group_horizontalness(
    members: list[int], polylines: list[list[Point]], seg_of: dict[int, int]
) -> float:
    if not members:
        return 0.0
    total = 0.0
    for n in members:
        seg = seg_of[n]
        p = polylines[n]
        total += _horizontalness(_segment_angle_deg(p[seg], p[seg + 1]))
    return total / len(members)


def _per_pair_gaps(
    cluster: list[_Crossing],
    routes: list,
    polylines: list[list[Point]],
    line_priority: dict[str, int],
    back: int,
    seg_of: dict[int, int],
    corner_clear: float,
) -> list[tuple[int, int, Gap]]:
    """Fallback for a non-bipartite cluster: break the lower-priority line of
    each crossing pair individually."""
    per: dict[int, list[Point]] = defaultdict(list)
    for c in cluster:
        pa = line_priority.get(routes[c.a].line_id, back)
        pb = line_priority.get(routes[c.b].line_id, back)
        under = c.a if pa >= pb else c.b
        per[under].append(c.point)
    out: list[tuple[int, int, Gap]] = []
    for n, pts in per.items():
        out.extend(_uniform_gaps([n], seg_of, polylines, pts, corner_clear))
    return out


def _unit(a: Point, b: Point) -> Point:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    return (dx / length, dy / length) if length else (1.0, 0.0)


def _segment_containing(
    poly: list[Point], span: tuple[Point, Point], corner_clear: float
) -> int | None:
    """Index of the segment of ``poly`` that contains the gap ``span``
    collinearly and clear of its corners, or None."""
    a, b = span
    for i in range(len(poly) - 1):
        p, q = poly[i], poly[i + 1]
        if _point_on_segment(a, p, q, corner_clear) and _point_on_segment(
            b, p, q, corner_clear
        ):
            return i
    return None


def _point_on_segment(pt: Point, p: Point, q: Point, corner_clear: float) -> bool:
    """True if ``pt`` lies on segment p->q (within 1px perpendicular) and at
    least ``corner_clear`` from either end."""
    dx, dy = q[0] - p[0], q[1] - p[1]
    length = math.hypot(dx, dy)
    if length == 0:
        return False
    ux, uy = dx / length, dy / length
    proj = (pt[0] - p[0]) * ux + (pt[1] - p[1]) * uy
    if proj < corner_clear or proj > length - corner_clear:
        return False
    perp = abs((pt[0] - p[0]) * (-uy) + (pt[1] - p[1]) * ux)
    return perp <= 1.0
