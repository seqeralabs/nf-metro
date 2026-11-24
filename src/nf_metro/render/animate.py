"""Animation support: animated dots traveling along metro lines."""

from __future__ import annotations

__all__ = [
    "FRAME_SLOT",
    "AnimationTimeline",
    "BallTrack",
    "Dot",
    "animation_frame_markup",
    "build_animation_timeline",
    "render_animation",
]

import math
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple

import drawsvg as draw

from nf_metro.layout.routing import RoutedPath
from nf_metro.layout.routing.common import apply_route_offsets, point_on_polyline
from nf_metro.layout.routing.corners import (
    corner_arc_controls,
    curve_tangents,
    resolve_curve_radii,
)
from nf_metro.parser.model import Edge, MetroGraph
from nf_metro.render.animation_waves import LineWaves, Route, plan_line_waves
from nf_metro.render.constants import (
    ANIMATION_CURVE_RADIUS,
    EDGE_CONNECT_TOLERANCE,
    MIN_ANIMATION_DURATION,
)
from nf_metro.render.ns import ns
from nf_metro.render.path_geometry import (
    flatten_path,
    materialize_source_turnout_paths,
)
from nf_metro.render.style import Theme

# A dot whose travel fraction is within this of its whole lifetime gets a plain
# two-stop keyframe (no terminus hold), avoiding a degenerate hold of ~0s.
_FULL_CYCLE_EPSILON = 0.001

# Longest loop the dots are spread over when a line's routes need several
# cycles to share a fork fairly; a single cycle longer than this is its own loop.
_MAX_LOOP_SECONDS = 60.0

# The CSS writes seconds to two decimals and keyframe percentages to two
# decimals of a percent; the timeline holds exactly those values so a sampled
# frame and the browser agree.
_SECONDS_DECIMALS = 2
_PERCENT_DECIMALS = 4
_PERCENT_STEP = 10**-_PERCENT_DECIMALS


def _fills_the_cycle(move_frac: float) -> bool:
    """Return whether a dot travels for (near enough) its whole lifetime."""
    return move_frac >= 1.0 - _FULL_CYCLE_EPSILON


def _seconds(value: float) -> float:
    return round(value, _SECONDS_DECIMALS)


# Segments each rounded corner is flattened into when sampling a ball's
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
    """The geometry one dot travels: its drawn ``d`` and the flattened polyline.

    ``points``/``distances`` are the polyline the rounded corners flatten to
    and its cumulative arc length, which :meth:`point_at` samples the way a
    browser samples ``offset-distance``.
    """

    d_attr: str
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


@dataclass(frozen=True)
class Dot:
    """One dot: a piece of a route, when it leaves and how long it is visible.

    The dot departs ``depart`` seconds into the loop, travels its track for
    ``move_frac`` of its ``lifetime`` and then holds at the terminus until the
    lifetime ends and it is hidden.  The loop wraps, so a dot departing late
    may still be visible after the loop restarts.
    """

    line_id: str
    track: BallTrack
    depart: float
    lifetime: float
    move_frac: float

    def stops(self, loop: float) -> tuple[float, float | None]:
        """Return the (travel end, hidden from) fractions of *loop* the CSS
        keyframes are written with; ``None`` when the dot is never hidden.

        The dot is shown on ``[0, hidden_from)`` of its own animation: it may
        linger at its terminus up to two keyframe steps past its lifetime
        (the last step being the fade to nothing) but is never hidden before
        it ends.  The travel ends at a keyframe distinct from, and before, the
        last one fully shown.
        """
        step = _PERCENT_STEP
        hidden_from = (math.ceil(self.lifetime / loop / step - 1e-9) + 1) * step
        if hidden_from >= 1.0 - step / 2:
            return self.move_frac, None
        last_shown = round(hidden_from - step, _PERCENT_DECIMALS)
        travel = round(self.move_frac * self.lifetime / loop, _PERCENT_DECIMALS)
        if travel >= round(last_shown - step, _PERCENT_DECIMALS):
            travel = last_shown
        return max(travel, step), round(hidden_from, _PERCENT_DECIMALS)


@dataclass(frozen=True)
class AnimationTimeline:
    """The dots and timing one animated map loops through.

    Built from the same motion paths the CSS animation uses, so a frame
    sampled here places every dot within a small fraction of a pixel of
    where a browser would draw it at that moment of the loop (see the
    corner-flattening note on ``_CURVE_FLATTEN_STEPS``).  ``cycle`` is the
    time the longest route takes to travel; ``loop`` is the whole animation,
    a whole number of cycles long.
    """

    cycle: float
    loop: float
    dots: tuple[Dot, ...]

    def ball_positions(self, phase: float) -> list[tuple[float, float]]:
        """Return the centre of every visible dot *phase* (0-1) through the loop.

        Phase 0 and 1 give the same picture, so a frame sequence over
        ``[0, 1)`` loops seamlessly.
        """
        now = (phase % 1.0) * self.loop
        centres: list[tuple[float, float]] = []
        for dot in self.dots:
            travel, hidden_from = dot.stops(self.loop)
            # Rounded before wrapping so a departure that lands on this instant
            # is not read as the end of the previous lap by float noise.
            age = round((now - dot.depart) / self.loop, 9) % 1.0
            if hidden_from is not None and age >= hidden_from:
                continue
            if _fills_the_cycle(travel):
                progress = age
            elif travel <= 0:
                progress = 1.0
            else:
                progress = min(age / travel, 1.0)
            centres.append(dot.track.point_at(progress))
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
    route_polylines: Sequence[Sequence[tuple[float, float]]] | None = None,
    route_curve_radii: Sequence[Sequence[float] | None] | None = None,
) -> None:
    """Add animated dots traveling along each metro line.

    Each dot is a ``<circle>`` driven along its route by a CSS ``offset-path``
    animation; :func:`build_animation_timeline` decides which routes the dots
    take and when they depart.

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
        graph,
        routes,
        station_offsets,
        theme,
        curve_radius,
        route_polylines=route_polylines,
        route_curve_radii=route_curve_radii,
    )
    ball_prefix = _ball_prefix(theme)
    keyframes: dict[str, str] = {}
    balls: list[str] = []

    for dot in timeline.dots:
        travel, hidden_from = dot.stops(timeline.loop)
        if hidden_from is None:
            kf_name = _travel_keyframes(travel, keyframes)
        else:
            kf_name = _visibility_keyframes(
                travel, hidden_from, keyframes, theme.animation_ball_opacity
            )
        delay = _seconds(dot.depart - timeline.loop)
        motion = (
            f"offset-path: path('{dot.track.d_attr}'); offset-rotate: 0deg; "
            f"animation: {kf_name} {timeline.loop:.2f}s linear infinite; "
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
        f'opacity="{theme.animation_ball_opacity}"{stroke_attr} '
    )


def animation_frame_markup(
    timeline: AnimationTimeline, theme: Theme, phase: float
) -> str:
    """Return the static balls to substitute for :data:`FRAME_SLOT` at *phase*.

    Same circles the CSS animation drives, pinned at the centres they hold
    *phase* (0-1) of the way through the loop, so a rasterised frame closely
    approximates the moment a browser would draw.
    """
    ball_prefix = _ball_prefix(theme)
    return "".join(
        f'{ball_prefix}cx="{x:.2f}" cy="{y:.2f}"/>'
        for x, y in timeline.ball_positions(phase)
    )


@dataclass(frozen=True)
class _LineRoutes:
    """A line's wave plan and the geometry of the routes it sends dots down."""

    line_id: str
    plan: LineWaves
    route_tracks: dict[tuple[str, str, str], _Track]
    line_tracks: list[_Track]


class _Track(NamedTuple):
    """Waypoints a dot passes and the drawn corner radius at each of them.

    The radius is 0 at the ends, and wherever two separately drawn routes
    meet, since neither route's stroke rounds that vertex.
    """

    points: list[tuple[float, float]]
    radii: list[float]


def _join_tracks(a: _Track, b: _Track) -> _Track:
    """*a* continued by *b*, which starts where *a* ends."""
    return _Track(a.points + b.points[1:], a.radii[:-1] + [0.0] + b.radii[1:])


def build_animation_timeline(
    graph: MetroGraph,
    routes: list[RoutedPath],
    station_offsets: dict[tuple[str, str], float],
    theme: Theme,
    curve_radius: float = ANIMATION_CURVE_RADIUS,
    *,
    route_polylines: Sequence[Sequence[tuple[float, float]]] | None = None,
    route_curve_radii: Sequence[Sequence[float] | None] | None = None,
) -> AnimationTimeline:
    """Build the dots, their departures and the shared loop for *graph*.

    Dots ride the drawn strokes: *route_polylines* and *route_curve_radii* are
    each route's drawn waypoints and corner radii, aligned with *routes*, as
    the static render materializes them.

    Every dot is visible for one cycle (the longest route's at-speed travel
    time): it travels at the theme's speed, holds at its terminus, then is
    hidden.  A line whose waves need several cycles to share its forks fairly
    stretches the loop to a whole number of cycles, never past
    ``_MAX_LOOP_SECONDS`` unless one cycle alone is longer.  Each line sends
    whole rounds of waves, departures spread evenly over the loop, so the loop
    seam is exact.
    """
    if route_polylines is None or route_curve_radii is None:
        route_polylines, route_curve_radii, _ = materialize_source_turnout_paths(
            routes,
            [apply_route_offsets(route, station_offsets) for route in routes],
            default_radius=curve_radius,
        )
    lines = _plan_lines(graph, routes, route_polylines, route_curve_radii, curve_radius)
    geometry: dict[tuple[str, tuple[tuple[str, str], ...]], list[_MotionPath]] = {}

    def pieces(line: _LineRoutes, route: Route) -> list[_MotionPath]:
        key = (line.line_id, tuple((e.source, e.target) for e in route))
        if key not in geometry:
            geometry[key] = _route_pieces(line, route)
        return geometry[key]

    def travel_time(piece: _MotionPath) -> float:
        return max(piece.length / theme.animation_speed, MIN_ANIMATION_DURATION)

    def route_time(line: _LineRoutes, route: Route) -> float:
        return sum(travel_time(piece) for piece in pieces(line, route))

    balls = max(theme.animation_balls_per_line, 0)
    longest = max(
        (
            route_time(line, route)
            for line in lines
            for wave in line.plan.waves(line.plan.round_waves)
            for route in wave
        ),
        default=MIN_ANIMATION_DURATION,
    )
    # The waves a line sends depend on the loop, which depends on the cycle,
    # which must cover every route those waves use; raise the cycle until the
    # routes of the waves actually sent fit in it.
    while True:
        cycle = _seconds(longest)
        rounds = max((line.plan.round_waves for line in lines), default=1)
        cycles = max(1, min(rounds, math.floor(_MAX_LOOP_SECONDS / cycle + 1e-9)))
        sent = {
            id(line): line.plan.waves(
                math.ceil(cycles / line.plan.round_waves)
                * line.plan.round_waves
                * balls
            )
            for line in lines
        }
        needed = max(
            (
                route_time(line, route)
                for line in lines
                for wave in sent[id(line)]
                for route in wave
            ),
            default=MIN_ANIMATION_DURATION,
        )
        if needed <= longest + 1e-9:
            break
        longest = needed
    loop = _seconds(cycles * cycle)

    dots: list[Dot] = []
    tracks: dict[str, BallTrack] = {}
    for line in lines:
        waves = sent[id(line)]
        for index, wave in enumerate(waves):
            departure = _seconds(index * loop / len(waves))
            for route in wave:
                _route_dots(
                    dots,
                    line.line_id,
                    [(p.d_attr, travel_time(p)) for p in pieces(line, route)],
                    departure,
                    longest,
                    loop,
                    tracks,
                )
    return AnimationTimeline(cycle=cycle, loop=loop, dots=tuple(dots))


def _route_dots(
    dots: list[Dot],
    line_id: str,
    pieces: list[tuple[str, float]],
    departure: float,
    cycle: float,
    loop: float,
    tracks: dict[str, BallTrack],
) -> None:
    """Append the dots of one route: each piece departs as the one before it
    arrives, and only the last holds at its terminus for the rest of the cycle."""
    spent = 0.0
    start = departure
    for number, (d_attr, travel) in enumerate(pieces):
        if d_attr not in tracks:
            points, distances = _flatten_path(d_attr)
            tracks[d_attr] = BallTrack(d_attr, points, distances)
        if number == len(pieces) - 1:
            remaining = cycle - spent
            lifetime = _seconds(remaining)
            move_frac = min(travel / remaining, 1.0) if remaining > 0 else 1.0
        else:
            lifetime = _seconds(travel)
            move_frac = 1.0
        dots.append(
            Dot(
                line_id,
                tracks[d_attr],
                _seconds(start % loop),
                lifetime,
                round(move_frac, _PERCENT_DECIMALS),
            )
        )
        spent += travel
        start = _seconds(start + lifetime)


def _plan_lines(
    graph: MetroGraph,
    routes: list[RoutedPath],
    route_polylines: Sequence[Sequence[tuple[float, float]]],
    route_curve_radii: Sequence[Sequence[float] | None],
    curve_radius: float,
) -> list[_LineRoutes]:
    """Plan the waves of every animated line and gather its drawn route geometry."""
    route_tracks: dict[tuple[str, str, str], _Track] = {}
    tracks_by_line: dict[str, list[_Track]] = {}
    for route, polyline, radii in zip(
        routes, route_polylines, route_curve_radii, strict=True
    ):
        points = list(polyline)
        resolved = resolve_curve_radii(
            points,
            None if radii is None else list(radii),
            default_radius=curve_radius,
        )
        ends = [0.0] * min(len(points), 2)
        route_tracks[route.edge.source, route.edge.target, route.line_id] = _Track(
            points, ends[:1] + resolved + ends[1:]
        )
    for (_source, _target, line_id), track in route_tracks.items():
        tracks_by_line.setdefault(line_id, []).append(track)

    edges_by_line: dict[str, list[Edge]] = {}
    for edge in graph.edges:
        edges_by_line.setdefault(edge.line_id, []).append(edge)

    lines: list[_LineRoutes] = []
    for line_id, edges in edges_by_line.items():
        if line_id not in graph.lines:
            continue
        plan = plan_line_waves(edges)
        if plan is None:
            continue
        lines.append(
            _LineRoutes(
                line_id=line_id,
                plan=plan,
                route_tracks=route_tracks,
                line_tracks=tracks_by_line.get(line_id, []),
            )
        )
    return lines


class _MotionPath(NamedTuple):
    """A dot's drawn path and the length its travel time is measured by."""

    d_attr: str
    length: float


def _route_pieces(line: _LineRoutes, route: Route) -> list[_MotionPath]:
    """Return the motion paths of one route: one per contiguous chunk of it."""
    pieces: list[_MotionPath] = []
    for chunk in _chain_edge_tracks(list(route), line.route_tracks, line.line_tracks):
        if len(chunk.points) >= 2:
            pieces.append(_motion_path(chunk))
    return pieces


def _flatten_path(
    d_attr: str,
) -> tuple[tuple[tuple[float, float], ...], tuple[float, ...]]:
    """Flatten an M/L/C path to a polyline and its cumulative arc lengths.

    A sampled position needs the curve walked rather than approximated by its
    chord.
    """
    points: list[tuple[float, float]] = []
    for subpath in flatten_path(d_attr, _CURVE_FLATTEN_STEPS):
        for point in subpath:
            # Repeated waypoints are common (a zero-length corner); a duplicate
            # would add a zero-length segment for point_at to divide by.
            if not points or points[-1] != point:
                points.append(point)

    if not points:
        points.append((0.0, 0.0))
    distances = [0.0]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        distances.append(distances[-1] + math.hypot(x1 - x0, y1 - y0))
    return tuple(points), tuple(distances)


def _travel_keyframes(move_frac: float, registry: dict[str, str]) -> str:
    """Return the @keyframes name for a ball that travels for *move_frac* of
    its lifetime then holds at the terminus, registering its CSS in
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


def _visibility_keyframes(
    travel: float, hidden_from: float, registry: dict[str, str], opacity: float
) -> str:
    """Return the @keyframes name for a dot that travels for *travel* of the
    loop, holds at the terminus, and is hidden from *hidden_from* on.

    The dot is shown from the start of its own animation, so each dot's delay
    places that start at its departure.  The hide is a step written as a
    ``0.01%`` fade so every stop lies at a distinct percentage.
    """
    name = ns("nfm-dot-" + f"{travel:.4f}-{hidden_from:.4f}".replace(".", "_"))
    if name not in registry:
        shown = f"offset-distance:100%;opacity:{opacity}"
        last_shown = round(hidden_from - _PERCENT_STEP, _PERCENT_DECIMALS)
        stops = [f"0%{{offset-distance:0%;opacity:{opacity}}}"]
        if travel < last_shown:
            stops.append(f"{travel * 100:.2f}%{{{shown}}}")
        stops.append(f"{last_shown * 100:.2f}%{{{shown}}}")
        gone = "offset-distance:100%;opacity:0"
        stops.append(f"{hidden_from * 100:.2f}%{{{gone}}}")
        stops.append(f"100%{{{gone}}}")
        registry[name] = f"@keyframes {name}{{{''.join(stops)}}}"
    return name


def _chain_edge_tracks(
    edges: list[Edge],
    route_tracks: dict[tuple[str, str, str], _Track],
    line_tracks: list[_Track],
) -> list[_Track]:
    """Chain edge route tracks into contiguous chunks.

    When consecutive edges' route endpoints don't coincide -- a
    merge-junction branch route terminates on the trunk bundle rather
    than at the junction station -- the gap is bridged using
    sibling-route geometry on the same line so the motion path stays
    on rendered geometry instead of cutting an off-piste diagonal.
    The bridge may consume the next edge as well, since the stub from
    merge junction to entry port is often already covered by the trunk.
    """
    chunks: list[_Track] = []
    current: _Track | None = None

    def follow(track: _Track) -> _Track | None:
        """*track* appended to ``current`` directly or over a bridge."""
        assert current is not None
        if _points_match(current.points[-1], track.points[0]):
            return _join_tracks(current, track)
        if len(current.points) >= 2 and point_on_polyline(
            track.points[0], current.points[-2:], EDGE_CONNECT_TOLERANCE
        ):
            # A source turnout's curve leaves the incoming route before that
            # route's drawn end, so the dot turns off there.
            trimmed = _Track(current.points[:-1] + [track.points[0]], current.radii)
            return _join_tracks(trimmed, track)
        bridge = _find_bridge(current.points[-1], track.points[0], line_tracks)
        if bridge is None:
            return None
        return _join_tracks(_join_tracks(current, bridge), track)

    i = 0
    while i < len(edges):
        edge = edges[i]
        track = route_tracks.get((edge.source, edge.target, edge.line_id))
        if not track or not track.points:
            i += 1
            continue
        if current is None:
            current = track
            i += 1
            continue
        joined = follow(track)
        if joined is not None:
            current = joined
            i += 1
            continue
        # Try skipping a stub edge whose geometry the trunk already covered.
        if i + 1 < len(edges):
            n = edges[i + 1]
            after = route_tracks.get((n.source, n.target, n.line_id))
            joined = follow(after) if after and after.points else None
            if joined is not None:
                current = joined
                i += 2
                continue
        chunks.append(current)
        current = track
        i += 1

    if current is not None:
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
    tracks: list[_Track],
    tol: float = 2.0,
) -> _Track | None:
    """Return a sub-track from from_pt to to_pt along a sibling route."""
    for track in tracks:
        pts = track.points
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
        bridge = [(from_pt, 0.0)]
        bridge += [(pts[j], track.radii[j]) for j in range(from_idx + 1, to_idx + 1)]
        bridge.append((to_pt, 0.0))
        cleaned = [bridge[0]]
        for point, radius in bridge[1:]:
            if not _points_match(cleaned[-1][0], point):
                cleaned.append((point, radius))
        if len(cleaned) >= 2:
            return _Track(
                [point for point, _ in cleaned],
                [0.0, *(radius for _, radius in cleaned[1:-1]), 0.0],
            )
    return None


def _motion_path(track: _Track) -> _MotionPath:
    """The ``d`` a dot follows along *track*, rounding each corner as the
    static render draws it, and the length its travel is timed by.

    A rounded corner is timed as the mean of its chord and its two tangent
    arms, measured on the unrounded geometry. A joined track can carry a
    sibling's radius onto a leg a bridge or turnout shortened, so the radii are
    clamped to the track's own legs as the renderer clamps a route's.
    """
    pts = track.points
    parts = [f"M {pts[0][0]:.2f} {pts[0][1]:.2f}"]
    length = 0.0
    pen = pts[0]
    for tan in curve_tangents(pts, resolve_curve_radii(pts, track.radii[1:-1])):
        if tan.curved and tan.before != tan.after:
            c1, c2 = corner_arc_controls(tan.before, tan.corner, tan.after)
            parts.append(
                f"L {tan.before[0]:.2f} {tan.before[1]:.2f} "
                f"C {c1[0]:.2f} {c1[1]:.2f} {c2[0]:.2f} {c2[1]:.2f} "
                f"{tan.after[0]:.2f} {tan.after[1]:.2f}"
            )
            arms = math.dist(tan.before, tan.corner) + math.dist(tan.corner, tan.after)
            length += math.dist(pen, tan.before)
            length += (math.dist(tan.before, tan.after) + arms) / 2
            pen = tan.after
        else:
            parts.append(f"L {tan.corner[0]:.2f} {tan.corner[1]:.2f}")
            length += math.dist(pen, tan.corner)
            pen = tan.corner
    parts.append(f"L {pts[-1][0]:.2f} {pts[-1][1]:.2f}")
    length += math.dist(pen, pts[-1])
    return _MotionPath(" ".join(parts), length)
