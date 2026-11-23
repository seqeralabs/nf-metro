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
import re
from bisect import bisect_left
from dataclasses import dataclass

import drawsvg as draw

from nf_metro.layout.routing import RoutedPath
from nf_metro.layout.routing.common import (
    SourceTurnout,
    apply_route_offsets,
    point_on_polyline,
    segment_direction,
)
from nf_metro.layout.routing.corners import (
    corner_arc_controls,
    corner_from_arc,
    curve_tangents,
    resolve_curve_radii,
)
from nf_metro.parser.model import Edge, MetroGraph
from nf_metro.render.animation_waves import LineWaves, Route, plan_line_waves
from nf_metro.render.constants import (
    ANIMATION_BALL_OPACITY,
    ANIMATION_CURVE_RADIUS,
    EDGE_CONNECT_TOLERANCE,
    MIN_ANIMATION_DURATION,
)
from nf_metro.render.ns import ns
from nf_metro.render.path_geometry import materialize_source_turnout
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
        graph, routes, station_offsets, theme, curve_radius
    )
    ball_prefix = _ball_prefix(theme)
    keyframes: dict[str, str] = {}
    balls: list[str] = []

    for dot in timeline.dots:
        travel, hidden_from = dot.stops(timeline.loop)
        if hidden_from is None:
            kf_name = _travel_keyframes(travel, keyframes)
        else:
            kf_name = _visibility_keyframes(travel, hidden_from, keyframes)
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
        f'opacity="{ANIMATION_BALL_OPACITY}"{stroke_attr} '
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
    route_polylines: dict[tuple[str, str, str], list[tuple[float, float]]]
    line_polylines: list[list[tuple[float, float]]]
    line_turnouts: tuple[tuple[tuple[float, float], SourceTurnout], ...]


def build_animation_timeline(
    graph: MetroGraph,
    routes: list[RoutedPath],
    station_offsets: dict[tuple[str, str], float],
    theme: Theme,
    curve_radius: float = ANIMATION_CURVE_RADIUS,
) -> AnimationTimeline:
    """Build the dots, their departures and the shared loop for *graph*.

    Every dot is visible for one cycle (the longest route's at-speed travel
    time): it travels at the theme's speed, holds at its terminus, then is
    hidden.  A line whose waves need several cycles to share its forks fairly
    stretches the loop to a whole number of cycles, never past
    ``_MAX_LOOP_SECONDS`` unless one cycle alone is longer.  Each line sends
    whole rounds of waves, departures spread evenly over the loop, so the loop
    seam is exact.
    """
    lines = _plan_lines(graph, routes, station_offsets)
    geometry: dict[tuple[str, tuple[tuple[str, str], ...]], list[str]] = {}

    def pieces(line: _LineRoutes, route: Route) -> list[str]:
        key = (line.line_id, tuple((e.source, e.target) for e in route))
        if key not in geometry:
            geometry[key] = _route_pieces(line, route, curve_radius)
        return geometry[key]

    durations: dict[str, float] = {}

    def travel_time(d_attr: str) -> float:
        if d_attr not in durations:
            durations[d_attr] = max(
                _compute_path_length(d_attr) / theme.animation_speed,
                MIN_ANIMATION_DURATION,
            )
        return durations[d_attr]

    def route_time(line: _LineRoutes, route: Route) -> float:
        return sum(travel_time(d) for d in pieces(line, route))

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
                    [(d, travel_time(d)) for d in pieces(line, route)],
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
    station_offsets: dict[tuple[str, str], float],
) -> list[_LineRoutes]:
    """Plan the waves of every animated line and gather its route geometry."""
    route_polylines: dict[tuple[str, str, str], list[tuple[float, float]]] = {}
    for route in routes:
        key = (route.edge.source, route.edge.target, route.line_id)
        route_polylines[key] = apply_route_offsets(route, station_offsets)

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
                route_polylines=route_polylines,
                line_polylines=[
                    pts for key, pts in route_polylines.items() if key[2] == line_id
                ],
                line_turnouts=tuple(
                    (
                        route_polylines[
                            (route.edge.source, route.edge.target, route.line_id)
                        ][0],
                        route.source_turnout,
                    )
                    for route in routes
                    if route.line_id == line_id and route.source_turnout is not None
                ),
            )
        )
    return lines


def _route_pieces(line: _LineRoutes, route: Route, curve_radius: float) -> list[str]:
    """Return the motion paths of one route: one per contiguous chunk of it."""
    pieces: list[str] = []
    for chunk in _chain_edge_points(
        list(route), line.route_polylines, line.line_polylines
    ):
        if len(chunk) < 2:
            continue
        radii = None
        for corner, turnout in line.line_turnouts:
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
        d_attr = _points_to_svg_path(chunk, curve_radius, route_curve_radii=radii)
        if d_attr:
            pieces.append(d_attr)
    return pieces


def _flatten_path(
    d_attr: str,
) -> tuple[tuple[tuple[float, float], ...], tuple[float, ...]]:
    """Flatten an M/L/C path to a polyline and its cumulative arc lengths.

    :func:`_compute_path_length` stays the authority on a track's *duration*
    (changing it would retime every animated map); a sampled position needs
    the curve walked rather than approximated by its chord.
    """
    tokens = re.findall(r"[MLC]|[-+]?\d*\.?\d+", d_attr)
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
        elif token == "C":
            c1 = (float(tokens[i + 1]), float(tokens[i + 2]))
            c2 = (float(tokens[i + 3]), float(tokens[i + 4]))
            end = (float(tokens[i + 5]), float(tokens[i + 6]))
            for step in range(1, _CURVE_FLATTEN_STEPS + 1):
                t = step / _CURVE_FLATTEN_STEPS
                u = 1.0 - t
                push(
                    (
                        u**3 * cx
                        + 3 * u * u * t * c1[0]
                        + 3 * u * t * t * c2[0]
                        + t**3 * end[0],
                        u**3 * cy
                        + 3 * u * u * t * c1[1]
                        + 3 * u * t * t * c2[1]
                        + t**3 * end[1],
                    )
                )
            cx, cy = end
            i += 7
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
    travel: float, hidden_from: float, registry: dict[str, str]
) -> str:
    """Return the @keyframes name for a dot that travels for *travel* of the
    loop, holds at the terminus, and is hidden from *hidden_from* on.

    The dot is shown from the start of its own animation, so each dot's delay
    places that start at its departure.  The hide is a step written as a
    ``0.01%`` fade so every stop lies at a distinct percentage.
    """
    name = ns("nfm-dot-" + f"{travel:.4f}-{hidden_from:.4f}".replace(".", "_"))
    if name not in registry:
        shown = f"offset-distance:100%;opacity:{ANIMATION_BALL_OPACITY}"
        last_shown = round(hidden_from - _PERCENT_STEP, _PERCENT_DECIMALS)
        stops = [f"0%{{offset-distance:0%;opacity:{ANIMATION_BALL_OPACITY}}}"]
        if travel < last_shown:
            stops.append(f"{travel * 100:.2f}%{{{shown}}}")
        stops.append(f"{last_shown * 100:.2f}%{{{shown}}}")
        gone = "offset-distance:100%;opacity:0"
        stops.append(f"{hidden_from * 100:.2f}%{{{gone}}}")
        stops.append(f"100%{{{gone}}}")
        registry[name] = f"@keyframes {name}{{{''.join(stops)}}}"
    return name


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
            c1, c2 = corner_arc_controls(tan.before, tan.corner, tan.after)
            parts.append(
                f"L {tan.before[0]:.2f} {tan.before[1]:.2f} "
                f"C {c1[0]:.2f} {c1[1]:.2f} {c2[0]:.2f} {c2[1]:.2f} "
                f"{tan.after[0]:.2f} {tan.after[1]:.2f}"
            )
        else:
            parts.append(f"L {tan.corner[0]:.2f} {tan.corner[1]:.2f}")

    parts.append(f"L {pts[-1][0]:.2f} {pts[-1][1]:.2f}")

    return " ".join(parts)


def _compute_path_length(d_attr: str) -> float:
    """Approximate the length of an SVG path from its commands.

    Parses M, L, and C commands and sums segment lengths. A C corner is
    measured as the mean of its chord and the two tangent arms through the
    corner vertex its tangents meet at, so a track's duration depends only on
    its waypoints and radii.
    """
    # Extract all numbers from the path
    tokens = re.findall(r"[MLC]|[-+]?\d*\.?\d+", d_attr)

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
        elif token == "C":
            c1 = (float(tokens[i + 1]), float(tokens[i + 2]))
            c2 = (float(tokens[i + 3]), float(tokens[i + 4]))
            ex = float(tokens[i + 5])
            ey = float(tokens[i + 6])
            qcx, qcy = corner_from_arc((cx, cy), c1, c2, (ex, ey))
            leg1 = math.hypot(qcx - cx, qcy - cy)
            leg2 = math.hypot(ex - qcx, ey - qcy)
            chord = math.hypot(ex - cx, ey - cy)
            total += (chord + leg1 + leg2) / 2
            cx, cy = ex, ey
            i += 7
        else:
            i += 1

    return total
