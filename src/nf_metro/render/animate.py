"""Animation support: animated balls traveling along metro lines."""

from __future__ import annotations

__all__ = ["render_animation"]

import math
import re

import drawsvg as draw

from nf_metro.layout.routing import RoutedPath
from nf_metro.layout.routing.common import point_on_polyline
from nf_metro.layout.routing.corners import resolve_curve_radii
from nf_metro.parser.model import MetroGraph
from nf_metro.render.constants import (
    ANIMATION_BALL_OPACITY,
    ANIMATION_CURVE_RADIUS,
    EDGE_CONNECT_TOLERANCE,
    MIN_ANIMATION_DURATION,
)
from nf_metro.render.style import Theme
from nf_metro.render.svg import apply_route_offsets


def render_animation(
    d: draw.Drawing,
    graph: MetroGraph,
    routes: list[RoutedPath],
    station_offsets: dict[tuple[str, str], float],
    theme: Theme,
    curve_radius: float = ANIMATION_CURVE_RADIUS,
) -> None:
    """Add animated balls traveling along each metro line.

    For each metro line, builds a continuous SVG path from its chained
    edges, then injects invisible <path> elements and <circle> elements
    with <animateMotion> to create the traveling ball effect.
    """
    line_paths = _build_line_motion_paths(
        graph,
        routes,
        station_offsets,
        theme,
        curve_radius,
    )

    # Compute a global animation cycle duration so all balls loop in
    # sync.  Without this, short lines restart while long lines are
    # still mid-track, making it look like some lines lose their balls
    # on alternating cycles.  Each ball travels at the same speed
    # (theme.animation_speed px/s) and then holds at the endpoint
    # until the cycle restarts, using keyTimes/keyPoints.
    max_dur = MIN_ANIMATION_DURATION
    for _, d_attr in line_paths:
        path_length = _compute_path_length(d_attr)
        dur = max(path_length / theme.animation_speed, MIN_ANIMATION_DURATION)
        if dur > max_dur:
            max_dur = dur

    for idx, (line_id, d_attr) in enumerate(line_paths):
        path_id = f"motion-path-{line_id}-{idx}"

        # Invisible path for animateMotion to follow
        d.append(
            draw.Raw(f'<path id="{path_id}" d="{d_attr}" fill="none" stroke="none"/>')
        )

        # Natural duration at constant speed
        path_length = _compute_path_length(d_attr)
        natural_dur = max(path_length / theme.animation_speed, MIN_ANIMATION_DURATION)

        # Fraction of the global cycle spent moving vs holding at end
        move_frac = natural_dur / max_dur if max_dur > 0 else 1.0
        move_frac = min(move_frac, 1.0)

        # keyPoints: travel 0->1 during move phase, hold at 1 for rest
        # keyTimes: timestamps matching the keyPoints
        if move_frac < 0.999:
            key_times = f"0;{move_frac:.4f};1"
            key_points = "0;1;1"
            kp_attrs = (
                f'keyPoints="{key_points}" keyTimes="{key_times}" calcMode="linear" '
            )
        else:
            kp_attrs = ""

        n_balls = theme.animation_balls_per_line
        for i in range(n_balls):
            begin_offset = -i * max_dur / n_balls
            stroke_attr = ""
            if theme.animation_ball_stroke:
                stroke_attr = (
                    f' stroke="{theme.animation_ball_stroke}"'
                    f' stroke-width="{theme.animation_ball_stroke_width}"'
                )
            d.append(
                draw.Raw(
                    f'<circle r="{theme.animation_ball_radius}" '
                    f'fill="{theme.animation_ball_color}" '
                    f'opacity="{ANIMATION_BALL_OPACITY}"'
                    f"{stroke_attr}>"
                    f'<animateMotion dur="{max_dur:.2f}s" '
                    f"{kp_attrs}"
                    f'repeatCount="indefinite" '
                    f'begin="{begin_offset:.2f}s">'
                    f'<mpath href="#{path_id}"/>'
                    f"</animateMotion>"
                    f"</circle>"
                )
            )


def _build_line_motion_paths(
    graph: MetroGraph,
    routes: list[RoutedPath],
    station_offsets: dict[tuple[str, str], float],
    theme: Theme,
    curve_radius: float = ANIMATION_CURVE_RADIUS,
) -> list[tuple[str, str]]:
    """Build continuous SVG motion paths for each metro line.

    At diamond/bubble patterns (fork-join), produces separate paths for
    each branch so balls travel both alternatives (e.g., FastP and
    TrimGalore). Returns list of (line_id, d_attr) pairs -- a line_id
    may appear multiple times when it has forking branches.
    """
    # Single offset-applied polyline per route, reused below.
    route_polylines: dict[tuple[str, str, str], list[tuple[float, float]]] = {}
    for route in routes:
        key = (route.edge.source, route.edge.target, route.line_id)
        route_polylines[key] = apply_route_offsets(route, station_offsets)

    # Group edges by line
    edges_by_line: dict[str, list] = {}
    for edge in graph.edges:
        edges_by_line.setdefault(edge.line_id, []).append(edge)

    result: list[tuple[str, str]] = []

    for line_id, edges in edges_by_line.items():
        if line_id not in graph.lines:
            continue

        # Build adjacency: source -> list of (target, edge)
        adj: dict[str, list] = {}
        incoming: set[str] = set()
        for edge in edges:
            adj.setdefault(edge.source, []).append((edge.target, edge))
            incoming.add(edge.target)

        # Find root nodes (no incoming edges for this line)
        all_sources = set(adj.keys())
        roots = all_sources - incoming
        if not roots:
            continue

        # Build edge-disjoint paths: one greedy root-to-sink path
        # first, then short paths for remaining diamond branches.
        # This avoids combinatorial explosion (N diamonds -> 2^N paths)
        # and ensures each edge is traversed by exactly one ball.
        all_paths = _find_edge_disjoint_paths(roots, adj)

        if not all_paths:
            continue

        line_polylines = [
            pts for key, pts in route_polylines.items() if key[2] == line_id
        ]

        for path_edges in all_paths:
            chunks = _chain_edge_points(
                path_edges,
                route_polylines,
                line_polylines,
            )
            for chunk in chunks:
                if len(chunk) < 2:
                    continue
                d_attr = _points_to_svg_path(chunk, curve_radius)
                if d_attr:
                    result.append((line_id, d_attr))

    return result


def _find_edge_disjoint_paths(
    roots: set[str],
    adj: dict[str, list],
) -> list[list]:
    """Build one full root-to-sink path per unique branch.

    Instead of the cartesian product of all diamonds (which explodes
    combinatorially), this produces:

    1. One canonical path following the first branch at every fork.
    2. For each alternative branch at each fork, one full root-to-sink
       path that diverges only at that specific fork and follows the
       canonical (first) branch everywhere else.

    Result: for N forks with B_i branches each, produces
    1 + sum(B_i - 1) paths instead of product(B_i).
    E.g. 2 binary + 1 ternary fork -> 1+1+1+2 = 5 instead of 12.
    """
    # First find the canonical path (first branch at every fork)
    canonical = _first_path(sorted(roots)[0] if roots else "", adj)
    if not canonical:
        return []

    paths: list[list] = [canonical]

    # Build a set of canonical edge choices at each fork for quick lookup
    canonical_set: set[int] = {id(e) for e in canonical}

    # Find fork points: nodes in adj with >1 outgoing edge
    for node, targets in adj.items():
        if len(targets) <= 1:
            continue
        # The canonical path takes targets[0] (first branch).
        # Create a variant path for each alternative branch.
        for alt_target, alt_edge in targets:
            if id(alt_edge) in canonical_set:
                continue
            # Build a full root-to-sink path that follows canonical
            # everywhere except at this fork, where it takes alt_edge.
            variant = _variant_path(
                sorted(roots)[0] if roots else "",
                adj,
                fork_node=node,
                forced_edge=alt_edge,
                forced_target=alt_target,
            )
            if variant:
                paths.append(variant)

    return paths


def _first_path(start: str, adj: dict[str, list]) -> list:
    """Follow the first outgoing edge at every node from start to sink."""
    path: list = []
    current = start
    visited: set[str] = set()
    while current in adj and current not in visited:
        visited.add(current)
        target, edge = adj[current][0]
        path.append(edge)
        current = target
    return path


def _variant_path(
    start: str,
    adj: dict[str, list],
    fork_node: str,
    forced_edge: object,
    forced_target: str,
) -> list:
    """Build a root-to-sink path that takes forced_edge at fork_node.

    At every other fork, follows the first (canonical) branch.
    """
    path: list = []
    current = start
    visited: set[str] = set()
    while current in adj and current not in visited:
        visited.add(current)
        if current == fork_node:
            path.append(forced_edge)
            current = forced_target
        else:
            target, edge = adj[current][0]
            path.append(edge)
            current = target
    return path


def _chain_edge_points(
    edges: list,
    route_polylines: dict[tuple[str, str, str], list[tuple[float, float]]],
    line_polylines: list[list[tuple[float, float]]],
) -> list[list[tuple[float, float]]]:
    """Chain edge route polylines into contiguous waypoint chunks.

    When consecutive edges' route endpoints don't coincide -- a
    merge-junction branch route terminates on the trunk bundle rather
    than at the junction station -- the gap is bridged using
    sibling-route geometry on the same line so the motion path stays
    on rendered geometry instead of cutting an off-piste diagonal.
    The bridge may consume the next edge as well, since the stub from
    merge junction to entry port is often already covered by the trunk.
    """
    chunks: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []

    i = 0
    while i < len(edges):
        edge = edges[i]
        pts = route_polylines.get((edge.source, edge.target, edge.line_id))
        if not pts:
            i += 1
            continue

        if not current:
            current = list(pts)
            i += 1
            continue

        if _points_match(current[-1], pts[0]):
            current.extend(pts[1:])
            i += 1
            continue

        bridge = _find_bridge(current[-1], pts[0], line_polylines)
        if bridge is not None:
            current.extend(bridge[1:])
            current.extend(pts[1:])
            i += 1
            continue

        # Try skipping a stub edge whose geometry the trunk already covered.
        if i + 1 < len(edges):
            n = edges[i + 1]
            next_pts = route_polylines.get((n.source, n.target, n.line_id))
            if next_pts:
                if _points_match(current[-1], next_pts[0]):
                    current.extend(next_pts[1:])
                    i += 2
                    continue
                bridge = _find_bridge(current[-1], next_pts[0], line_polylines)
                if bridge is not None:
                    current.extend(bridge[1:])
                    current.extend(next_pts[1:])
                    i += 2
                    continue

        chunks.append(current)
        current = list(pts)
        i += 1

    if current:
        chunks.append(current)
    return chunks


def _points_match(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return (
        abs(a[0] - b[0]) < EDGE_CONNECT_TOLERANCE
        and abs(a[1] - b[1]) < EDGE_CONNECT_TOLERANCE
    )


def _find_bridge(
    from_pt: tuple[float, float],
    to_pt: tuple[float, float],
    polylines: list[list[tuple[float, float]]],
    tol: float = 2.0,
) -> list[tuple[float, float]] | None:
    """Return a sub-polyline from from_pt to to_pt along a sibling route."""
    for pts in polylines:
        from_loc = point_on_polyline(from_pt, pts, tol)
        if from_loc is None:
            continue
        to_loc = point_on_polyline(to_pt, pts, tol)
        if to_loc is None:
            continue
        from_idx, from_t = from_loc
        to_idx, to_t = to_loc
        if to_idx < from_idx or (to_idx == from_idx and to_t < from_t):
            continue
        bridge: list[tuple[float, float]] = [from_pt]
        for j in range(from_idx + 1, to_idx + 1):
            bridge.append(pts[j])
        bridge.append(to_pt)
        cleaned: list[tuple[float, float]] = [bridge[0]]
        for p in bridge[1:]:
            if not _points_match(cleaned[-1], p):
                cleaned.append(p)
        if len(cleaned) >= 2:
            return cleaned
    return None


def _points_to_svg_path(
    pts: list[tuple[float, float]],
    curve_radius: float = ANIMATION_CURVE_RADIUS,
    route_curve_radii: list[float] | None = None,
) -> str:
    """Convert a list of waypoints to an SVG path 'd' attribute.

    Uses resolve_curve_radii for consistent radius clamping with svg.py.
    """
    if len(pts) < 2:
        return ""

    if len(pts) == 2:
        return f"M {pts[0][0]:.2f} {pts[0][1]:.2f} L {pts[1][0]:.2f} {pts[1][1]:.2f}"

    parts = [f"M {pts[0][0]:.2f} {pts[0][1]:.2f}"]
    resolved = resolve_curve_radii(pts, route_curve_radii, default_radius=curve_radius)

    for i in range(1, len(pts) - 1):
        prev = pts[i - 1]
        curr = pts[i]
        nxt = pts[i + 1]

        dx1 = curr[0] - prev[0]
        dy1 = curr[1] - prev[1]
        len1 = math.hypot(dx1, dy1)

        dx2 = nxt[0] - curr[0]
        dy2 = nxt[1] - curr[1]
        len2 = math.hypot(dx2, dy2)

        r = resolved[i - 1]

        if len1 > 0 and len2 > 0:
            before_x = curr[0] - (dx1 / len1) * r
            before_y = curr[1] - (dy1 / len1) * r
            after_x = curr[0] + (dx2 / len2) * r
            after_y = curr[1] + (dy2 / len2) * r

            parts.append(
                f"L {before_x:.2f} {before_y:.2f} "
                f"Q {curr[0]:.2f} {curr[1]:.2f} {after_x:.2f} {after_y:.2f}"
            )
        else:
            parts.append(f"L {curr[0]:.2f} {curr[1]:.2f}")

    parts.append(f"L {pts[-1][0]:.2f} {pts[-1][1]:.2f}")

    return " ".join(parts)


def _compute_path_length(d_attr: str) -> float:
    """Approximate the length of an SVG path from its commands.

    Parses M, L, and Q commands and sums segment lengths.
    For Q (quadratic Bezier), approximates with the chord length.
    """
    # Extract all numbers from the path
    tokens = re.findall(r"[MLQ]|[-+]?\d*\.?\d+", d_attr)

    total = 0.0
    cx, cy = 0.0, 0.0  # current position
    i = 0

    while i < len(tokens):
        token = tokens[i]
        if token == "M":
            cx = float(tokens[i + 1])
            cy = float(tokens[i + 2])
            i += 3
        elif token == "L":
            nx = float(tokens[i + 1])
            ny = float(tokens[i + 2])
            total += math.hypot(nx - cx, ny - cy)
            cx, cy = nx, ny
            i += 3
        elif token == "Q":
            # Q cx cy ex ey - approximate with control point polygon
            qcx = float(tokens[i + 1])
            qcy = float(tokens[i + 2])
            ex = float(tokens[i + 3])
            ey = float(tokens[i + 4])
            # Sum of legs through control point (overestimates slightly)
            leg1 = math.hypot(qcx - cx, qcy - cy)
            leg2 = math.hypot(ex - qcx, ey - qcy)
            chord = math.hypot(ex - cx, ey - cy)
            # Average of chord and polygon for a decent approximation
            total += (chord + leg1 + leg2) / 2
            cx, cy = ex, ey
            i += 5
        else:
            i += 1

    return total
