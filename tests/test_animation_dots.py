"""Animated dots share each fork fairly and cover every edge of every line.

A dot is one route through a line's graph.  Branches that rejoin take turns
(one wave of dots per departure, rotating through the alternatives), branches
that never rejoin each get a dot every wave.  These tests drive the planner on
its own and the CSS the renderer writes, through an emulator that evaluates
the emitted duration, delay and keyframe stops the way a browser does.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import sys
from bisect import bisect_left
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import drawsvg as draw
import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.animate import (
    _flatten_path,
    build_animation_timeline,
    render_animation,
)
from nf_metro.themes import THEMES

REPO = Path(__file__).parent.parent
EXAMPLES_DIR = REPO / "examples"
TOPOLOGIES_DIR = EXAMPLES_DIR / "topologies"
FIXTURES = Path(__file__).parent / "fixtures" / "animation"
THEME = THEMES["nfcore"]

REJOIN_FIXTURES = [
    FIXTURES / "fork_rejoin_vs_branch_2.mmd",
    FIXTURES / "fork_rejoin_vs_branch_3.mmd",
    FIXTURES / "fork_rejoin_vs_branch_4.mmd",
]
TIMELINE_FIXTURES = [
    EXAMPLES_DIR / "rnaseq_sections.mmd",
    EXAMPLES_DIR / "epitopeprediction.mmd",
    EXAMPLES_DIR / "hlatyping.mmd",
    TOPOLOGIES_DIR / "fan_in_merge.mmd",
    TOPOLOGIES_DIR / "section_diamond.mmd",
    *REJOIN_FIXTURES,
    FIXTURES / "nested_rejoins.mmd",
    FIXTURES / "multi_root.mmd",
    FIXTURES / "clone_tree.mmd",
]


def _ids(paths: list[Path]) -> list[str]:
    return [p.stem for p in paths]


def _routed(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    graph.source_dir = str(path.parent)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, routes, offsets


def _timeline(path: Path):
    graph, routes, offsets = _routed(path)
    return build_animation_timeline(graph, routes, offsets, THEME)


def _animation_svg(path: Path) -> tuple[str, object]:
    graph, routes, offsets = _routed(path)
    drawing = draw.Drawing(100, 100)
    render_animation(drawing, graph, routes, offsets, THEME)
    return drawing.as_svg(), graph


def _line_edges(path: Path) -> dict[str, list]:
    graph = parse_metro_mermaid(path.read_text())
    by_line: dict[str, list] = {}
    for edge in graph.edges:
        if edge.line_id in graph.lines:
            by_line.setdefault(edge.line_id, []).append(edge)
    return by_line


def _plan(path: Path, line_id: str = "l1"):
    from nf_metro.render.animation_waves import plan_line_waves

    plan = plan_line_waves(_line_edges(path)[line_id])
    assert plan is not None
    return plan


def _edge_keys(route) -> list[tuple[str, str]]:
    return [(e.source, e.target) for e in route]


# --- the CSS emulator ------------------------------------------------------

_CIRCLE_STYLE = re.compile(r"<circle[^>]*style=\"([^\"]*)\"")
_KEYFRAMES = re.compile(r"@keyframes\s+([\w-]+)\{(.*?\})\}")
_STOP = re.compile(r"(from|to|[\d.]+%)\{([^}]*)\}")
_OFFSET_PATH = re.compile(r"offset-path:\s*path\('([^']*)'\)")
_ANIMATION = re.compile(r"animation:\s*([\w-]+)\s+([\d.]+)s\s+linear\s+infinite")
_DELAY = re.compile(r"animation-delay:\s*(-?[\d.]+)s")


@dataclass(frozen=True)
class _CssDot:
    d_attr: str
    duration: float
    delay: float
    stops: tuple[tuple[float, float, float | None], ...]
    points: tuple[tuple[float, float], ...]
    distances: tuple[float, ...]

    def state_at(self, t: float) -> tuple[bool, tuple[float, float]]:
        """Return (visible, centre) *t* seconds into the page's life."""
        progress = round((t - self.delay) / self.duration, 9) % 1.0
        index = max(
            i for i, (position, _, _) in enumerate(self.stops) if position <= progress
        )
        if index == len(self.stops) - 1:
            _, distance, opacity = self.stops[index]
        else:
            (p0, d0, o0), (p1, d1, o1) = self.stops[index], self.stops[index + 1]
            u = (progress - p0) / (p1 - p0)
            distance = d0 + (d1 - d0) * u
            opacity = None if o0 is None else o0 + (o1 - o0) * u  # type: ignore[operator]
        visible = opacity is None or opacity > 0
        return visible, _sample(self.points, self.distances, distance)


def _sample(points, distances, frac: float) -> tuple[float, float]:
    total = distances[-1]
    target = min(max(frac, 0.0), 1.0) * total
    index = bisect_left(distances, target)
    if total <= 0 or index <= 0:
        return points[0]
    span = distances[index] - distances[index - 1]
    t = 1.0 if span <= 0 else (target - distances[index - 1]) / span
    (x0, y0), (x1, y1) = points[index - 1], points[index]
    return (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)


def _parse_stops(body: str) -> tuple[tuple[float, float, float | None], ...]:
    stops = []
    for position, declarations in _STOP.findall(body):
        fraction = {"from": 0.0, "to": 1.0}.get(position)
        if fraction is None:
            fraction = float(position.rstrip("%")) / 100
        distance = float(re.search(r"offset-distance:([\d.]+)%", declarations).group(1))  # type: ignore[union-attr]
        opacity = re.search(r"opacity:([\d.]+)", declarations)
        stops.append(
            (fraction, distance / 100, float(opacity.group(1)) if opacity else None)
        )
    return tuple(stops)


def _css_dots(svg: str) -> list[_CssDot]:
    keyframes = {name: _parse_stops(body) for name, body in _KEYFRAMES.findall(svg)}
    dots = []
    for style in _CIRCLE_STYLE.findall(svg):
        d_attr = _OFFSET_PATH.search(style).group(1)  # type: ignore[union-attr]
        name, duration = _ANIMATION.search(style).groups()  # type: ignore[union-attr]
        delay = float(_DELAY.search(style).group(1))  # type: ignore[union-attr]
        points, distances = _flatten_path(d_attr)
        dots.append(
            _CssDot(d_attr, float(duration), delay, keyframes[name], points, distances)
        )
    return dots


def _sample_times(loop: float) -> Iterator[float]:
    for phase in (0.0, 0.037, 0.113, 0.29, 0.5, 0.62, 0.731, 0.9, 0.991):
        yield phase * loop


def _end_station(d_attr: str, graph, names: tuple[str, ...]) -> str:
    x, y = (float(v) for v in re.findall(r"-?\d+\.?\d*", d_attr)[-2:])
    nearest = min(
        names,
        key=lambda n: (graph.stations[n].x - x) ** 2 + (graph.stations[n].y - y) ** 2,
    )
    station = graph.stations[nearest]
    assert abs(station.x - x) < 12 and abs(station.y - y) < 12, (nearest, x, y)
    return nearest


# --- turn taking at rejoining forks ----------------------------------------


@pytest.mark.parametrize("fixture", REJOIN_FIXTURES, ids=_ids(REJOIN_FIXTURES))
def test_the_rejoining_branches_and_the_lone_branch_get_equal_dots(fixture: Path):
    """A dot per branch would hand the rejoining side three dots to the lone
    side's one.  Each wave sends one dot down each side, so over a round the
    arrivals match and at any instant the dots in flight match."""
    svg, graph = _animation_svg(fixture)
    dots = _css_dots(svg)
    assert dots
    ends = {d.d_attr: _end_station(d.d_attr, graph, ("a_end", "b_end")) for d in dots}

    arrivals = Counter(ends[d.d_attr] for d in dots)
    assert arrivals["a_end"] == arrivals["b_end"] > 0

    loop = dots[0].duration
    for t in _sample_times(loop):
        in_flight = Counter(ends[d.d_attr] for d in dots if d.state_at(t)[0])
        assert in_flight["a_end"] == in_flight["b_end"] > 0, (t, in_flight)


def test_the_rejoining_branches_rotate_through_every_alternative():
    svg, graph = _animation_svg(FIXTURES / "fork_rejoin_vs_branch_3.mmd")
    dots = _css_dots(svg)
    via_a = Counter(
        d.d_attr
        for d in dots
        if _end_station(d.d_attr, graph, ("a_end", "b_end")) == "a_end"
    )
    assert len(via_a) == 3
    assert len(set(via_a.values())) == 1


def test_a_round_of_the_rejoin_repro_is_three_waves():
    plan = _plan(FIXTURES / "fork_rejoin_vs_branch_3.mmd")
    assert plan.round_waves == 3
    first, second, third = plan.waves(3)
    middles = {
        next(e.target for e in route if e.source == "src")
        for wave in (first, second, third)
        for route in wave
        if route[-1].target == "a_end"
    }
    assert middles == {"a1", "a2", "a3"}
    assert all(len(wave) == 2 for wave in (first, second, third))


def test_each_turn_taking_level_uses_its_children_equally_per_round():
    plan = _plan(FIXTURES / "nested_rejoins.mmd")
    assert plan.round_waves == 4

    used = Counter(
        key
        for wave in plan.waves(plan.round_waves)
        for route in wave
        for key in _edge_keys(route)
    )
    # Outer turn: the (x1, x2) pair against z; inner turn: x1 against x2.
    assert used[("src", "z")] == used[("src", "x1")] + used[("src", "x2")] == 2
    assert used[("src", "x1")] == used[("src", "x2")] == 1
    # w never rejoins the others, so it gets a dot every wave.
    assert used[("src", "w")] == 4


def test_forks_that_never_rejoin_clone_into_exactly_todays_routes():
    plan = _plan(FIXTURES / "clone_tree.mmd")
    assert plan.round_waves == 1
    routes = {tuple(_edge_keys(r)) for r in plan.waves(1)[0]}
    assert routes == {
        (("src", "a"), ("a", "a1")),
        (("src", "a"), ("a", "a2")),
        (("src", "b"), ("b", "b1")),
    }
    assert plan.waves(3)[0] == plan.waves(3)[2]


# --- every root, every edge ------------------------------------------------


def test_every_root_of_a_line_starts_dots():
    plan = _plan(FIXTURES / "multi_root.mmd")
    started = {
        route[0].source for wave in plan.waves(plan.round_waves) for route in wave
    }
    assert started == {"r1", "r2", "r3"}


def test_every_root_of_a_line_is_animated_in_the_css():
    svg, _ = _animation_svg(FIXTURES / "multi_root.mmd")
    starts = {
        re.match(r"M ([\d.]+) ([\d.]+)", d.d_attr).groups() for d in _css_dots(svg)
    }  # type: ignore[union-attr]
    assert len(starts) >= 3


def _corpus() -> list[Path]:
    return sorted(EXAMPLES_DIR.glob("*.mmd")) + sorted(TOPOLOGIES_DIR.glob("*.mmd"))


def test_every_line_edge_gets_a_dot_each_round_across_the_corpus():
    """Parse-only: the plan depends on a line's edges, not on their geometry."""
    from nf_metro.render.animation_waves import plan_line_waves

    offenders: list[str] = []
    for path in _corpus():
        try:
            by_line = _line_edges(path)
        except Exception:
            continue
        for line_id, edges in by_line.items():
            plan = plan_line_waves(edges)
            if plan is None:
                continue
            wanted = {(e.source, e.target) for e in edges}
            used = {
                key
                for wave in plan.waves(plan.round_waves)
                for route in wave
                for key in _edge_keys(route)
            }
            if used != wanted:
                offenders.append(
                    f"{path.name}:{line_id} misses {sorted(wanted - used)[:3]}"
                )
    assert not offenders, offenders[:10]


# --- the timeline and the CSS agree ----------------------------------------


@pytest.mark.parametrize("fixture", TIMELINE_FIXTURES, ids=_ids(TIMELINE_FIXTURES))
def test_the_css_and_the_sampled_timeline_place_every_dot_alike(fixture: Path):
    graph, routes, offsets = _routed(fixture)
    timeline = build_animation_timeline(graph, routes, offsets, THEME)
    drawing = draw.Drawing(100, 100)
    render_animation(drawing, graph, routes, offsets, THEME)
    dots = _css_dots(drawing.as_svg())
    assert dots
    assert all(d.duration == pytest.approx(timeline.loop) for d in dots)

    for t in _sample_times(timeline.loop):
        emulated = [c for visible, c in (d.state_at(t) for d in dots) if visible]
        sampled = list(timeline.ball_positions(t / timeline.loop))
        assert len(emulated) == len(sampled), (fixture.name, t)
        for x, y in emulated:
            match = next(
                (
                    i
                    for i, (sx, sy) in enumerate(sampled)
                    if abs(sx - x) < 0.01 and abs(sy - y) < 0.01
                ),
                None,
            )
            assert match is not None, (fixture.name, t, (x, y))
            sampled.pop(match)


@pytest.mark.parametrize("fixture", TIMELINE_FIXTURES, ids=_ids(TIMELINE_FIXTURES))
def test_the_loop_is_bounded_and_seamless(fixture: Path):
    timeline = _timeline(fixture)
    assert timeline.loop <= max(timeline.cycle, 60.0) + 1e-9
    assert timeline.loop >= timeline.cycle - 1e-9

    start = sorted(timeline.ball_positions(0.0))
    seam = sorted(timeline.ball_positions(1.0))
    assert len(start) == len(seam)
    for (x0, y0), (x1, y1) in zip(start, seam):
        assert (x0, y0) == pytest.approx((x1, y1))


def test_every_dot_is_visible_for_exactly_one_cycle():
    timeline = _timeline(FIXTURES / "fork_rejoin_vs_branch_3.mmd")
    assert timeline.dots
    assert all(d.depart >= 0 and d.depart < timeline.loop for d in timeline.dots)
    for dot in timeline.dots:
        assert dot.lifetime == pytest.approx(timeline.cycle, abs=0.011)


# --- no dot vanishes early ---------------------------------------------------


def _boundary_phases(timeline) -> list[float]:
    """Every departure and end of life, with one CSS quantum either side."""
    quantum = timeline.loop * 1e-4
    instants = set()
    for dot in timeline.dots:
        for edge in (dot.depart, dot.depart + dot.lifetime):
            for shift in (-quantum, 0.0, quantum):
                instants.add(round((edge + shift) % timeline.loop, 6))
    return sorted(instants)


def _alive_at(timeline, now: float) -> int:
    """Dots whose exact lifetime covers *now* (half-open)."""
    return sum(
        1
        for dot in timeline.dots
        if round((now - dot.depart) % timeline.loop, 6) < round(dot.lifetime, 6)
        or dot.lifetime >= timeline.loop
    )


@pytest.mark.parametrize(
    "fixture",
    [FIXTURES / "fork_rejoin_vs_branch_3.mmd", EXAMPLES_DIR / "rnaseq_sections.mmd"],
    ids=["repro3", "hero"],
)
def test_no_dot_vanishes_before_its_lifetime_ends(fixture: Path):
    """Rounding the hide may leave a dot lingering at its terminus a moment
    past its lifetime, never take it away early: at every departure, every end
    of life and a quantum either side, at least the dots alive are drawn."""
    timeline = _timeline(fixture)
    short = []
    for now in _boundary_phases(timeline):
        phase = now / timeline.loop
        # The frame clock reaches a boundary with float noise either way.
        for sampled in (phase, math.nextafter(phase, 0), math.nextafter(phase, 2)):
            shown = len(timeline.ball_positions(sampled))
            if shown < _alive_at(timeline, now):
                short.append((now, shown, _alive_at(timeline, now)))
    assert not short, short[:5]


def test_a_cycle_boundary_of_the_rejoin_repro_still_shows_its_dots():
    timeline = _timeline(FIXTURES / "fork_rejoin_vs_branch_3.mmd")
    steady = len(timeline.ball_positions(0.5 / 3 + 0.01))
    assert steady == 2
    for k in range(1, 3):
        assert len(timeline.ball_positions(k / 3)) >= steady


# --- determinism -----------------------------------------------------------

_DIGEST_SCRIPT = """
import hashlib, sys
import drawsvg as draw
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.animate import render_animation
from nf_metro.themes import THEMES

digest = hashlib.sha256()
for path in sys.argv[1:]:
    graph = parse_metro_mermaid(open(path).read())
    graph.source_dir = path.rsplit("/", 1)[0]
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    drawing = draw.Drawing(100, 100)
    render_animation(drawing, graph, routes, offsets, THEMES["nfcore"])
    digest.update(drawing.as_svg().encode())
print(digest.hexdigest())
"""


def test_the_animation_does_not_depend_on_the_hash_seed():
    paths = [
        str(FIXTURES / "nested_rejoins.mmd"),
        str(FIXTURES / "multi_root.mmd"),
        str(EXAMPLES_DIR / "epitopeprediction.mmd"),
    ]
    digests = set()
    for seed in ("1", "2", "3"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(
            [sys.executable, "-c", _DIGEST_SCRIPT, *paths],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        digests.add(out.stdout.strip())
    assert len(digests) == 1
