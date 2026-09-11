"""Animation support: animated balls traveling along metro lines."""

from __future__ import annotations

__all__ = [
    "FRAME_SLOT",
    "AnimationTimeline",
    "BallTrack",
    "animation_frame_markup",
    "build_animation_timeline",
    "render_animation",
]

import math
import re
from bisect import bisect_left
from collections import deque
from dataclasses import dataclass

import drawsvg as draw

from nf_metro.layout.routing import RoutedPath
from nf_metro.layout.routing.common import (
    apply_route_offsets,
    point_on_polyline,
    segment_direction,
)
from nf_metro.layout.routing.corners import curve_tangents, resolve_curve_radii
from nf_metro.parser.model import Edge, MetroGraph
from nf_metro.render.constants import (
    ANIMATION_BALL_OPACITY,
    ANIMATION_CURVE_RADIUS,
    EDGE_CONNECT_TOLERANCE,
    MIN_ANIMATION_DURATION,
)
from nf_metro.render.ns import ns
from nf_metro.render.path_geometry import materialize_source_turnout
from nf_metro.render.style import Theme

# A line whose travel fraction is within this of a full cycle gets a plain
# two-stop keyframe (no terminus hold), avoiding a degenerate hold of ~0s.
_FULL_CYCLE_EPSILON = 0.001


def _fills_the_cycle(move_frac: float) -> bool:
    """Return whether a track travels for (near enough) the whole cycle."""
    return move_frac >= 1.0 - _FULL_CYCLE_EPSILON


# Segments each quadratic corner is flattened into when sampling a ball's
# position for a raster frame.  The corners are ~10px arcs, so eight chords
# put the sampled point within a small fraction of a pixel of the drawn curve.
_CURVE_FLATTEN_STEPS = 8

# Marker emitted in place of the animated balls when a caller asks for a
# frame slot: the SVG is rendered once, then each raster frame substitutes
# this comment for that frame's static circles (see nf_metro.render.video).
# A comment keeps the slot inert in any SVG that reaches a viewer unfilled.
FRAME_SLOT = "<!--nf-metro-animation-frame-->"


@dataclass(frozen=True)
class BallTrack:
    """One ball path: its drawn ``d``, cycle share, and flattened geometry.

    ``move_frac`` is the fraction of the shared cycle this track's ball spends
    travelling; past it the ball holds at the terminus (see
    :func:`_travel_keyframes`).  ``points``/``distances`` are the polyline the
    quadratic corners flatten to and its cumulative arc length, which
    :meth:`point_at` samples the way a browser samples ``offset-distance``.
    """

    d_attr: str
    move_frac: float
    points: tuple[tuple[float, float], ...]
    distances: tuple[float, ...]

    def point_at(self, frac: float) -> tuple[float, float]:
        """Return the point *frac* (0-1) of the way along the track."""
        total = self.distances[-1]
        if total <= 0:
            return self.points[0]
        target = min(max(frac, 0.0), 1.0) * total
        index = bisect_left(self.distances, target)
        if index <= 0:
            return self.points[0]
        span = self.distances[index] - self.distances[index - 1]
        t = 1.0 if span <= 0 else (target - self.distances[index - 1]) / span
        (x0, y0), (x1, y1) = self.points[index - 1], self.points[index]
        return (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)

    def distance_frac(self, progress: float) -> float:
        """Map an animation *progress* (0-1) to a fraction of the track.

        Reads the keyframes :func:`_travel_keyframes` writes for this track: a
        linear ramp over ``move_frac`` of the cycle, then a hold at the
        terminus for the remainder -- or, for a track that fills the cycle, a
        ramp across the whole of it and no hold at all.
        """
        if _fills_the_cycle(self.move_frac):
            return progress
        if self.move_frac <= 0:
            return 1.0
        return min(progress / self.move_frac, 1.0)


@dataclass(frozen=True)
class AnimationTimeline:
    """The ball geometry and timing one animated map cycles through.

    Built from the same motion paths the CSS animation uses, so a frame
    sampled here places every ball where a browser would draw it at that
    moment of the cycle.
    """

    cycle: float
    balls_per_line: int
    tracks: tuple[BallTrack, ...]

    def ball_positions(self, phase: float) -> list[tuple[float, float]]:
        """Return every ball's centre *phase* (0-1) through one cycle.

        The balls on a track are spread evenly across the cycle by the same
        negative animation delays the CSS uses, so ``phase`` 0 and 1 give the
        same picture and a frame sequence over ``[0, 1)`` loops seamlessly.
        """
        centres: list[tuple[float, float]] = []
        for track in self.tracks:
            for index in range(self.balls_per_line):
                progress = (phase + index / self.balls_per_line) % 1.0
                centres.append(track.point_at(track.distance_frac(progress)))
        return centres


def render_animation(
    d: draw.Drawing,
    graph: MetroGraph,
    routes: list[RoutedPath],
    station_offsets: dict[tuple[str, str], float],
    theme: Theme,
    curve_radius: float = ANIMATION_CURVE_RADIUS,
    *,
    frame_slot: bool = False,
) -> None:
    """Add animated balls traveling along each metro line.

    For each metro line, builds a continuous SVG path from its chained
    edges, then emits a <circle> per ball driven along that path by a CSS
    ``offset-path`` animation.

    CSS animation is used rather than SMIL ``<animateMotion>`` because SMIL
    does not run when an SVG is injected into a host page via ``innerHTML``
    (the playground preview, the embeddable inline snippet, any host that
    inlines an exported map): the SMIL timeline advances but the motion is
    never sampled onto the element, freezing every ball at its path start.
    CSS ``offset-path`` animates reliably whether the SVG is opened
    standalone, referenced from ``<img>``, or inlined into a document.

    With *frame_slot* set, :data:`FRAME_SLOT` is emitted here instead of the
    balls and their keyframes, leaving one SVG a raster caller fills per frame
    (see :mod:`nf_metro.render.video`).  The slot sits exactly where the
    animated balls would, so a frame keeps the same stacking: behind the
    station markers, in front of the lines.
    """
    if frame_slot:
        # The caller fills this per frame; building the timeline is its job.
        d.append(draw.Raw(FRAME_SLOT))
        return

    timeline = build_animation_timeline(
        graph, routes, station_offsets, theme, curve_radius
    )
    ball_prefix = _ball_prefix(theme)
    max_dur = timeline.cycle
    n_balls = timeline.balls_per_line
    keyframes: dict[str, str] = {}
    balls: list[str] = []

    for track in timeline.tracks:
        kf_name = _travel_keyframes(track.move_frac, keyframes)
        for i in range(n_balls):
            delay = -i * max_dur / n_balls
            motion = (
                f"offset-path: path('{track.d_attr}'); offset-rotate: 0deg; "
                f"animation: {kf_name} {max_dur:.2f}s linear infinite; "
                f"animation-delay: {delay:.2f}s;"
            )
            balls.append(f'{ball_prefix}style="{motion}"/>')

    # @keyframes must precede the inline animations that reference them.
    if keyframes:
        d.append(draw.Raw("<style>" + "".join(keyframes.values()) + "</style>"))
    for ball in balls:
        d.append(draw.Raw(ball))


def _ball_prefix(theme: Theme) -> str:
    """Return the opening of a ball ``<circle>``, up to its placement attrs."""
    stroke_attr = ""
    if theme.animation_ball_stroke:
        stroke_attr = (
            f' stroke="{theme.animation_ball_stroke}"'
            f' stroke-width="{theme.animation_ball_stroke_width}"'
        )
    return (
        f'<circle r="{theme.animation_ball_radius}" '
        f'fill="{theme.animation_ball_color}" '
        f'opacity="{ANIMATION_BALL_OPACITY}"{stroke_attr} '
    )


def animation_frame_markup(
    timeline: AnimationTimeline, theme: Theme, phase: float
) -> str:
    """Return the static balls to substitute for :data:`FRAME_SLOT` at *phase*.

    Same circles the CSS animation drives, pinned at the centres they hold
    *phase* (0-1) of the way through the cycle, so a rasterised frame matches
    the moment a browser would draw.
    """
    ball_prefix = _ball_prefix(theme)
    return "".join(
        f'{ball_prefix}cx="{x:.2f}" cy="{y:.2f}"/>'
        for x, y in timeline.ball_positions(phase)
    )


def build_animation_timeline(
    graph: MetroGraph,
    routes: list[RoutedPath],
    station_offsets: dict[tuple[str, str], float],
    theme: Theme,
    curve_radius: float = ANIMATION_CURVE_RADIUS,
) -> AnimationTimeline:
    """Build the ball paths and shared cycle for *graph*'s animation.

    All balls share one cycle (the longest line's at-speed duration) so they
    stay in sync; a shorter line covers its path in the first part of the
    cycle and holds at the terminus for the rest (see :func:`_travel_keyframes`),
    rather than restarting mid-track while a longer line runs on.
    """
    line_paths = _build_line_motion_paths(graph, routes, station_offsets, curve_radius)
    durations = [
        max(
            _compute_path_length(d_attr) / theme.animation_speed, MIN_ANIMATION_DURATION
        )
        for _, d_attr in line_paths
    ]
    max_dur = max(durations, default=MIN_ANIMATION_DURATION)

    tracks: list[BallTrack] = []
    for (_, d_attr), natural_dur in zip(line_paths, durations):
        points, distances = _flatten_path(d_attr)
        tracks.append(
            BallTrack(
                d_attr=d_attr,
                move_frac=min(natural_dur / max_dur, 1.0) if max_dur > 0 else 1.0,
                points=points,
                distances=distances,
            )
        )
    return AnimationTimeline(
        cycle=max_dur,
        balls_per_line=theme.animation_balls_per_line,
        tracks=tuple(tracks),
    )


def _flatten_path(
    d_attr: str,
) -> tuple[tuple[tuple[float, float], ...], tuple[float, ...]]:
    """Flatten an M/L/Q path to a polyline and its cumulative arc lengths.

    :func:`_compute_path_length` stays the authority on a track's *duration*
    (changing it would retime every animated map); this is the geometry a
    frame samples a ball's position from, where a curve has to be walked
    rather than approximated by its chord.
    """
    tokens = re.findall(r"[MLQ]|[-+]?\d*\.?\d+", d_attr)
    points: list[tuple[float, float]] = []

    def push(point: tuple[float, float]) -> None:
        # Repeated waypoints are common (a zero-length corner); a duplicate
        # would add a zero-length segment for point_at to divide by.
        if not points or points[-1] != point:
            points.append(point)

    cx, cy = 0.0, 0.0
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "M":
            cx, cy = float(tokens[i + 1]), float(tokens[i + 2])
            push((cx, cy))
            i += 3
        elif token == "L":
            cx, cy = float(tokens[i + 1]), float(tokens[i + 2])
            push((cx, cy))
            i += 3
        elif token == "Q":
            qcx, qcy = float(tokens[i + 1]), float(tokens[i + 2])
            ex, ey = float(tokens[i + 3]), float(tokens[i + 4])
            for step in range(1, _CURVE_FLATTEN_STEPS + 1):
                t = step / _CURVE_FLATTEN_STEPS
                u = 1.0 - t
                push(
                    (
                        u * u * cx + 2 * u * t * qcx + t * t * ex,
                        u * u * cy + 2 * u * t * qcy + t * t * ey,
                    )
                )
            cx, cy = ex, ey
            i += 5
        else:
            i += 1

    if not points:
        points.append((0.0, 0.0))
    distances = [0.0]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        distances.append(distances[-1] + math.hypot(x1 - x0, y1 - y0))
    return tuple(points), tuple(distances)


def _travel_keyframes(move_frac: float, registry: dict[str, str]) -> str:
    """Return the @keyframes name for a ball that travels for *move_frac* of
    the shared cycle then holds at the terminus, registering its CSS in
    *registry*.  Lines sharing a *move_frac* reuse one block; the name is
    namespaced (``ns``) so maps inlined on the same page don't collide.
    """
    name = ns("nfm-travel-" + f"{move_frac:.4f}".replace(".", "_"))
    if name not in registry:
        if not _fills_the_cycle(move_frac):
            registry[name] = (
                f"@keyframes {name}{{"
                f"0%{{offset-distance:0%}}"
                f"{move_frac * 100:.2f}%{{offset-distance:100%}}"
                f"100%{{offset-distance:100%}}}}"
            )
        else:
            registry[name] = (
                f"@keyframes {name}{{"
                f"from{{offset-distance:0%}}to{{offset-distance:100%}}}}"
            )
    return name


def _build_line_motion_paths(
    graph: MetroGraph,
    routes: list[RoutedPath],
    station_offsets: dict[tuple[str, str], float],
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
    edges_by_line: dict[str, list[Edge]] = {}
    for edge in graph.edges:
        edges_by_line.setdefault(edge.line_id, []).append(edge)

    result: list[tuple[str, str]] = []

    for line_id, edges in edges_by_line.items():
        if line_id not in graph.lines:
            continue

        # Build adjacency: source -> list of (target, edge)
        adj: dict[str, list[tuple[str, Edge]]] = {}
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
        line_turnouts = tuple(
            (
                route_polylines[(route.edge.source, route.edge.target, route.line_id)][
                    0
                ],
                route.source_turnout,
            )
            for route in routes
            if route.line_id == line_id and route.source_turnout is not None
        )

        for path_edges in all_paths:
            chunks = _chain_edge_points(
                path_edges,
                route_polylines,
                line_polylines,
            )
            for chunk in chunks:
                if len(chunk) < 2:
                    continue
                radii = None
                for corner, turnout in line_turnouts:
                    corner_index = next(
                        (
                            index
                            for index in range(1, len(chunk) - 1)
                            if _points_match(chunk[index], corner)
                            and segment_direction(chunk[index - 1], chunk[index])
                            is turnout.incoming_direction
                            and segment_direction(chunk[index], chunk[index + 1])
                            is turnout.outgoing_direction
                        ),
                        None,
                    )
                    if corner_index is None:
                        continue
                    chunk, radii, _shift = materialize_source_turnout(
                        chunk,
                        radii,
                        turnout,
                        corner_index=corner_index,
                        default_radius=curve_radius,
                    )
                d_attr = _points_to_svg_path(
                    chunk,
                    curve_radius,
                    route_curve_radii=radii,
                )
                if d_attr:
                    result.append((line_id, d_attr))

    return result


def _find_edge_disjoint_paths(
    roots: set[str],
    adj: dict[str, list[tuple[str, Edge]]],
) -> list[list[Edge]]:
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

    paths: list[list[Edge]] = [canonical]

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


def _first_path(start: str, adj: dict[str, list[tuple[str, Edge]]]) -> list[Edge]:
    """Follow the first outgoing edge at every node from start to sink."""
    path: list[Edge] = []
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
    adj: dict[str, list[tuple[str, Edge]]],
    fork_node: str,
    forced_edge: Edge,
    forced_target: str,
) -> list[Edge]:
    """Build a root-to-sink path that takes forced_edge at fork_node.

    The prefix from *start* to *fork_node* follows the first (canonical)
    branch at every earlier fork. When that canonical walk cannot reach
    *fork_node* -- because the only route to it runs through a non-first
    branch at some earlier fork -- fall back to a breadth-first search over
    the full adjacency to find any prefix that does. Without the fallback,
    *forced_edge* is silently never emitted and its branch is dropped from
    the animation.
    """
    prefix = _canonical_prefix_to(start, adj, fork_node)
    if prefix is None:
        prefix = _bfs_prefix_to(start, adj, fork_node)
        if prefix is None:
            return []

    prefix_edges, visited = prefix
    path = list(prefix_edges)
    path.append(forced_edge)
    current = forced_target
    while current in adj and current not in visited:
        visited.add(current)
        target, edge = adj[current][0]
        path.append(edge)
        current = target
    return path


def _canonical_prefix_to(
    start: str,
    adj: dict[str, list[tuple[str, Edge]]],
    fork_node: str,
) -> tuple[list[Edge], set[str]] | None:
    """Prefix from *start* to *fork_node* via the first branch at every fork.

    Returns the prefix edges and the set of nodes visited (including
    *fork_node*), or ``None`` if the canonical walk dead-ends before
    reaching *fork_node*.
    """
    path: list[Edge] = []
    current = start
    visited: set[str] = set()
    while current in adj and current not in visited:
        visited.add(current)
        if current == fork_node:
            return path, visited
        target, edge = adj[current][0]
        path.append(edge)
        current = target
    return None


def _bfs_prefix_to(
    start: str,
    adj: dict[str, list[tuple[str, Edge]]],
    fork_node: str,
) -> tuple[list[Edge], set[str]] | None:
    """Shortest edge-path from *start* to *fork_node* over the full adjacency.

    Returns the prefix edges and the set of nodes on that path (including
    *fork_node*), or ``None`` if *fork_node* is unreachable from *start*.
    """
    queue: deque[tuple[str, list[Edge]]] = deque([(start, [])])
    seen: set[str] = {start}
    while queue:
        node, edges = queue.popleft()
        if node == fork_node:
            visited = {start}
            for edge in edges:
                visited.add(edge.target)
            return edges, visited
        for target, edge in adj.get(node, []):
            if target not in seen:
                seen.add(target)
                queue.append((target, [*edges, edge]))
    return None


def _chain_edge_points(
    edges: list[Edge],
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

    Shares ``resolve_curve_radii`` and ``curve_tangents`` with the static SVG
    renderer so animation corners round identically to the drawn map.
    """
    if len(pts) < 2:
        return ""

    if len(pts) == 2:
        return f"M {pts[0][0]:.2f} {pts[0][1]:.2f} L {pts[1][0]:.2f} {pts[1][1]:.2f}"

    parts = [f"M {pts[0][0]:.2f} {pts[0][1]:.2f}"]
    resolved = resolve_curve_radii(pts, route_curve_radii, default_radius=curve_radius)

    for tan in curve_tangents(pts, resolved):
        if tan.curved:
            parts.append(
                f"L {tan.before[0]:.2f} {tan.before[1]:.2f} "
                f"Q {tan.corner[0]:.2f} {tan.corner[1]:.2f} "
                f"{tan.after[0]:.2f} {tan.after[1]:.2f}"
            )
        else:
            parts.append(f"L {tan.corner[0]:.2f} {tan.corner[1]:.2f}")

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
