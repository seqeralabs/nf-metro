"""Centreline templates for inter-section routes.

A handler describes only the bundle's **centreline** -- the axis-aligned
polyline the bundle's centre follows -- plus the co-travelling lines.
:func:`~nf_metro.layout.routing.bundle.build_concentric_bundle` then fans every
line as a rigid parallel offset of that centreline with concentric corners.  No
handler assembles per-line ``points`` or ``curve_radii`` by hand, so a bundle
can neither flip (the lines keep a constant side-of-travel order) nor pinch
(every corner radius is derived from the turn geometry).

Each builder gathers the bundle for an edge with :func:`gather_bundle`, lays
out the centreline from the handler's named geometry, and returns the single
:class:`RoutedPath` for the calling edge.
"""

from __future__ import annotations

from nf_metro.layout.constants import COORD_TOLERANCE
from nf_metro.layout.routing.bundle import build_concentric_bundle
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.context import _get_offset, _RoutingCtx
from nf_metro.parser.model import Edge

_Vec = tuple[float, float]
_Member = tuple[Edge, str, float]


def gather_bundle(
    ctx: _RoutingCtx, edge: Edge
) -> tuple[list[_Member], float, float]:
    """Collect the bundle of lines co-travelling ``edge.source -> edge.target``.

    Returns ``(members, src_center, tgt_center)``.  ``members`` is one
    ``(edge, line_id, signed_offset)`` per line, with ``signed_offset`` the
    line's station-offset displacement from the bundle's source-side mean -- so
    the bundle is centred on its source mean and a single rigid offset
    reproduces each line's fan position on every leg.  ``src_center`` /
    ``tgt_center`` are the mean source / target offsets: the centreline's own
    displacement from the raw port coordinate on each side.
    """
    member_edges = [
        e for e in ctx.graph.edges_to(edge.target) if e.source == edge.source
    ]
    line_ids = list(dict.fromkeys(e.line_id for e in member_edges))
    edge_by_line = {e.line_id: e for e in member_edges}

    src_offs = {lid: _get_offset(ctx, edge.source, lid) for lid in line_ids}
    tgt_offs = {lid: _get_offset(ctx, edge.target, lid) for lid in line_ids}
    src_center = sum(src_offs.values()) / len(src_offs)
    tgt_center = sum(tgt_offs.values()) / len(tgt_offs)
    members = [
        (edge_by_line[lid], lid, src_offs[lid] - src_center) for lid in line_ids
    ]
    return members, src_center, tgt_center


def route_along(
    edge: Edge,
    members: list[_Member],
    centerline: list[_Vec],
    *,
    base_radius: float,
    min_radius: float | None = None,
    normalize_exempt: bool = True,
) -> RoutedPath | None:
    """Fan *members* along *centerline* and return the route for *edge*.

    The single seam between a handler's named geometry and
    :func:`build_concentric_bundle`: the handler builds the centreline, this
    fans the bundle and picks out the calling edge's line.
    """
    routes = build_concentric_bundle(
        members,
        centerline,
        base_radius=base_radius,
        min_radius=min_radius,
        normalize_exempt=normalize_exempt,
    )
    return next((r for r in routes if r.line_id == edge.line_id), None)


def route_straight(
    edge: Edge,
    ctx: _RoutingCtx,
    p_src: _Vec,
    p_tgt: _Vec,
    *,
    base_radius: float,
    normalize_exempt: bool = False,
) -> RoutedPath | None:
    """Straight connector as a two-vertex centreline.

    The bundle fans perpendicular to the run.  A straight trunk segment must
    keep its bundle on the same screen side as the rest of the line, so the
    centreline is laid out in the canonical travel direction (left-to-right or
    top-to-bottom) and the emitted points reversed back to source-first if the
    edge runs the other way -- otherwise the perpendicular normal would flip the
    fan on a right-to-left or serpentine segment.  A single line whose two ports
    sit at different bundle ranks would need a diagonal centreline (which
    :func:`build_concentric_bundle` forbids); it falls back to a direct segment
    whose per-line offsets the renderer applies.
    """
    members, src_center, tgt_center = gather_bundle(ctx, edge)
    src_pt = (p_src[0], p_src[1] + src_center)
    tgt_pt = (p_tgt[0], p_tgt[1] + tgt_center)
    dx = tgt_pt[0] - src_pt[0]
    dy = tgt_pt[1] - src_pt[1]
    if abs(dx) > COORD_TOLERANCE and abs(dy) > COORD_TOLERANCE:
        return RoutedPath(
            edge=edge,
            line_id=edge.line_id,
            points=[p_src, p_tgt],
            is_inter_section=True,
            normalize_exempt=normalize_exempt,
        )

    reverse = dx < -COORD_TOLERANCE or dy < -COORD_TOLERANCE
    centerline = [tgt_pt, src_pt] if reverse else [src_pt, tgt_pt]
    route = route_along(
        edge,
        members,
        centerline,
        base_radius=base_radius,
        normalize_exempt=normalize_exempt,
    )
    if route is not None and reverse:
        route.points = list(reversed(route.points))
    return route
