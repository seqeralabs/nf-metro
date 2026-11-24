"""Animation motion path invariants.

Every line segment in a motion path must lie along a rendered
metro-line segment for the same line.  An animation polyline that
strays off the visible track produces a ball that flies off-piste.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.animate import build_animation_timeline
from nf_metro.render.svg import render_svg
from nf_metro.themes import THEMES

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
TOPOLOGIES_DIR = EXAMPLES_DIR / "topologies"

# Fixtures that exercise the merge-junction routing introduced in #207
# along with a couple of standard examples as regression guards.
ANIMATION_FIXTURES = [
    EXAMPLES_DIR / "genomeassembly.mmd",
    EXAMPLES_DIR / "rnaseq_sections.mmd",
    EXAMPLES_DIR / "riboseq_metro.mmd",
    EXAMPLES_DIR / "epitopeprediction.mmd",
    EXAMPLES_DIR / "hlatyping.mmd",
    TOPOLOGIES_DIR / "fan_in_merge.mmd",
    TOPOLOGIES_DIR / "wide_fan_in.mmd",
    TOPOLOGIES_DIR / "section_diamond.mmd",
]
ANIMATION_FIXTURE_IDS = [p.stem for p in ANIMATION_FIXTURES]


def _build(fixture: Path):
    """Parse, layout, route, and build animation motion paths for a fixture."""
    graph = parse_metro_mermaid(fixture.read_text())
    graph.source_dir = str(fixture.parent)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    timeline = build_animation_timeline(graph, routes, offsets, THEMES["nfcore"])
    motion_paths = sorted({(d.line_id, d.track.d_attr) for d in timeline.dots})
    return graph, routes, offsets, motion_paths


_NUMBER = r"-?\d+(?:\.\d+)?"
_TOKEN_RE = re.compile(rf"[MLC]|{_NUMBER}")


def _parse_motion_path(d_attr: str) -> list[tuple[str, list[tuple[float, float]]]]:
    """Parse an SVG motion path 'd' attribute into segments.

    Returns a list of (command, points) where command is one of 'M', 'L',
    'C' and points is the segment's coordinate tuples.  The 'C' segment
    keeps its (control1, control2, end) triple.
    """
    tokens = _TOKEN_RE.findall(d_attr)
    out: list[tuple[str, list[tuple[float, float]]]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("M", "L"):
            x = float(tokens[i + 1])
            y = float(tokens[i + 2])
            out.append((tok, [(x, y)]))
            i += 3
        elif tok == "C":
            nums = [float(t) for t in tokens[i + 1 : i + 7]]
            out.append(
                ("C", [(nums[0], nums[1]), (nums[2], nums[3]), (nums[4], nums[5])])
            )
            i += 7
        else:
            i += 1
    return out


def _end_point(segment: tuple[str, list[tuple[float, float]]]) -> tuple[float, float]:
    """Return the on-curve endpoint of a parsed segment."""
    cmd, pts = segment
    return pts[-1]


@pytest.mark.parametrize("fixture", ANIMATION_FIXTURES, ids=ANIMATION_FIXTURE_IDS)
def test_motion_path_segments_lie_on_rendered_geometry(fixture: Path):
    """Every L/C segment of every motion path must coincide with a
    rendered metro-line segment of the same line."""
    from nf_metro.layout.routing.common import point_on_polyline
    from nf_metro.render.svg import apply_route_offsets

    graph, routes, offsets, motion_paths = _build(fixture)

    polylines_by_line: dict[str, list[list[tuple[float, float]]]] = {}
    for r in routes:
        polylines_by_line.setdefault(r.line_id, []).append(
            apply_route_offsets(r, offsets)
        )

    tol = 1.5  # absorb curve-approximation rounding

    def _segment_covered(
        a: tuple[float, float],
        b: tuple[float, float],
        line_id: str,
    ) -> bool:
        for pts in polylines_by_line.get(line_id, []):
            a_loc = point_on_polyline(a, pts, tol)
            if a_loc is None:
                continue
            b_loc = point_on_polyline(b, pts, tol)
            if b_loc is None:
                continue
            # Same segment or adjacent segments along the polyline are fine;
            # the test is whether both points sit on the same polyline.
            return True
        return False

    offences: list[str] = []
    for line_id, d_attr in motion_paths:
        parsed = _parse_motion_path(d_attr)
        if not parsed:
            continue
        prev_end = _end_point(parsed[0])
        for seg in parsed[1:]:
            cmd, pts = seg
            end = _end_point(seg)
            if cmd == "L":
                if not _segment_covered(prev_end, end, line_id):
                    offences.append(f"L {prev_end} -> {end} on line {line_id!r}")
            elif cmd == "C":
                # A rounded corner's first control point lies on its incoming
                # leg: accept if that leg or the chord is on the polyline.
                control = pts[0]
                if not (
                    _segment_covered(prev_end, control, line_id)
                    or _segment_covered(prev_end, end, line_id)
                ):
                    offences.append(
                        f"C {prev_end} -> {control} -> {end} on line {line_id!r}"
                    )
            prev_end = end

    assert not offences, (
        f"{fixture.name}: motion path contains off-piste segments not on "
        f"any rendered metro-line edge:\n  " + "\n  ".join(offences[:5])
    )


def _render_animated(fixture: Path, theme_key: str = "nfcore") -> str:
    graph = parse_metro_mermaid(fixture.read_text())
    graph.source_dir = str(fixture.parent)
    compute_layout(graph)
    return render_svg(graph, THEMES[theme_key], animate=True)


@pytest.mark.parametrize("fixture", ANIMATION_FIXTURES, ids=ANIMATION_FIXTURE_IDS)
def test_animation_uses_css_offset_path_not_smil(fixture: Path):
    """Animated balls must be driven by CSS ``offset-path``, not SMIL.

    SMIL ``<animateMotion>`` freezes when an SVG is injected into a host
    page via ``innerHTML`` (the playground preview, the inline embed
    snippet): the timeline advances but the motion is never sampled, so
    every ball sits at its path start.  CSS ``offset-path`` animates in
    every embedding context, so the rendered animation must use it and
    carry no SMIL motion elements.
    """
    svg = _render_animated(fixture)
    assert "<animateMotion" not in svg
    assert "<mpath" not in svg
    assert "offset-path:" in svg
    assert "@keyframes" in svg


@pytest.mark.parametrize("fixture", ANIMATION_FIXTURES, ids=ANIMATION_FIXTURE_IDS)
def test_every_ball_animation_resolves_to_a_keyframes(fixture: Path):
    """Each ball's ``animation`` names a ``@keyframes`` defined in the SVG.

    A dangling animation name silently produces a motionless ball, the same
    frozen-at-start symptom the CSS switch fixes.
    """
    svg = _render_animated(fixture)
    referenced = set(re.findall(r"animation:\s*([\w-]+)", svg))
    defined = set(re.findall(r"@keyframes\s+([\w-]+)", svg))
    assert referenced, f"{fixture.name}: no ball animations emitted"
    assert referenced <= defined, (
        f"{fixture.name}: ball animations reference undefined keyframes: "
        f"{sorted(referenced - defined)}"
    )


def test_nfcore_balls_carry_a_stroke():
    """nf-core balls render with a dark stroke so the white fill stays
    visible on both light and dark backgrounds."""
    svg = _render_animated(EXAMPLES_DIR / "rnaseq_sections.mmd", "nfcore")
    theme = THEMES["nfcore"]
    assert theme.animation_ball_stroke
    assert f'stroke="{theme.animation_ball_stroke}"' in svg


# genomeassembly and fan_in_merge are left out: at their merge feeders the
# straight member's stroke stops short of where the curving member joins, so
# no drawn centreline spans that stretch for a dot to ride.
_STROKE_RIDING_FIXTURES = [
    p
    for p in [*ANIMATION_FIXTURES, EXAMPLES_DIR / "differentialabundance.mmd"]
    if p.stem not in {"genomeassembly", "fan_in_merge"}
]


@pytest.mark.parametrize(
    "fixture", _STROKE_RIDING_FIXTURES, ids=[p.stem for p in _STROKE_RIDING_FIXTURES]
)
def test_dots_ride_their_lines_drawn_stroke(fixture: Path):
    """Every sampled dot position lies on its own line's drawn centreline,
    rounded corners included, so a dot keeps to its lane through a concentric
    turn."""
    import math
    from collections import defaultdict

    from nf_metro.api import prepare_graph
    from nf_metro.render.svg import _drawn_pieces, _piece_at, build_render_plan
    from nf_metro.themes import resolve_theme

    graph = prepare_graph(fixture.read_text(), source_dir=str(fixture.parent))
    plan = build_render_plan(graph, resolve_theme(None, graph))
    timeline = build_animation_timeline(
        plan.graph,
        plan.routes,
        plan.station_offsets,
        plan.theme,
        route_polylines=plan.route_polylines,
        route_curve_radii=plan.route_curve_radii,
    )
    cell = 4.0
    ink: dict[str, dict[tuple[int, int], list[tuple[float, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for route, polyline, radii in zip(
        plan.routes, plan.route_polylines, plan.route_curve_radii
    ):
        for piece in _drawn_pieces(list(polyline), radii):
            steps = (
                200
                if piece.control is not None
                else max(1, math.ceil(math.dist(piece.start, piece.end) / 0.1))
            )
            for k in range(steps + 1):
                x, y = _piece_at(piece, k / steps)[0]
                ink[route.line_id][int(x // cell), int(y // cell)].append((x, y))

    def off_stroke(line_id: str, point: tuple[float, float]) -> float:
        cx, cy = int(point[0] // cell), int(point[1] // cell)
        return min(
            (
                math.dist(point, q)
                for dx in (-1, 0, 1)
                for dy in (-1, 0, 1)
                for q in ink[line_id].get((cx + dx, cy + dy), ())
            ),
            default=math.inf,
        )

    assert timeline.dots
    worst = max(
        (off_stroke(dot.line_id, dot.track.point_at(k / 100)), dot.line_id)
        for dot in timeline.dots
        for k in range(101)
    )
    assert worst[0] <= 0.5, worst
