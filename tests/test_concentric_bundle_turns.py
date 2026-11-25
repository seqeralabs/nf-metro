"""Bundled lanes turn any angle about one shared centre."""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path

import pytest

from nf_metro.api import prepare_graph
from nf_metro.layout.constants import graph_offset_step
from nf_metro.layout.routing.corners import resolve_curve_radii, turn_shift
from nf_metro.render.svg import _drawn_pieces, _piece_at, build_render_plan
from nf_metro.themes import resolve_theme

_ROOT = Path(__file__).resolve().parents[1]
_STEP = 4.0


def _lane(offset: float, turn_degrees: float) -> list[tuple[float, float]]:
    """A lane of a bundle turning *turn_degrees* left, shifted *offset* outward."""
    phi = math.radians(turn_degrees)
    corner = (100.0 + math.tan(phi / 2) * offset, 100.0 + offset)
    end = (corner[0] + 100 * math.cos(phi), corner[1] - 100 * math.sin(phi))
    return [(0.0, 100.0 + offset), corner, end]


def _samples(points, radii, n=400):
    out = []
    for piece in _drawn_pieces(points, radii):
        for k in range(n + 1):
            out.append((_piece_at(piece, k / n)[0], piece.control is not None))
    return out


def _centre(points, arm):
    (p0, v, p2) = points
    u_in = ((v[0] - p0[0]) / math.dist(p0, v), (v[1] - p0[1]) / math.dist(p0, v))
    u_out = ((p2[0] - v[0]) / math.dist(v, p2), (p2[1] - v[1]) / math.dist(v, p2))
    phi = math.acos(max(-1.0, min(1.0, u_in[0] * u_out[0] + u_in[1] * u_out[1])))
    radius = arm / math.tan(phi / 2)
    before = (v[0] - u_in[0] * arm, v[1] - u_in[1] * arm)
    turn = u_in[0] * u_out[1] - u_in[1] * u_out[0]
    normal = (-u_in[1], u_in[0]) if turn > 0 else (u_in[1], -u_in[0])
    return (before[0] + normal[0] * radius, before[1] + normal[1] * radius)


def _pair(turn_degrees: float):
    inner, outer = _lane(0.0, turn_degrees), _lane(_STEP, turn_degrees)
    displacement = (outer[1][0] - inner[1][0], outer[1][1] - inner[1][1])
    return inner, outer, 10.0 + turn_shift(*outer, displacement)


def _gap(inner, inner_arm, outer, outer_arm) -> float:
    inner_pts = [q for q, _ in _samples(inner, [inner_arm])]
    return max(
        min(math.dist(q, s) for s in inner_pts) - _STEP
        for q, on_curve in _samples(outer, [outer_arm])
        if on_curve
    )


@pytest.mark.parametrize("turn_degrees", [45.0, 61.0, 90.0, 120.0, 135.0])
def test_adjacent_lanes_share_a_centre_at_any_turn(turn_degrees):
    inner, outer, outer_arm = _pair(turn_degrees)
    assert outer_arm > 10.0
    assert _centre(inner, 10.0) == pytest.approx(_centre(outer, outer_arm), abs=1e-6)


@pytest.mark.parametrize("turn_degrees", [45.0, 61.0, 90.0, 120.0, 135.0])
def test_adjacent_lanes_stay_touching_through_a_diagonal_turn(turn_degrees):
    inner, outer, outer_arm = _pair(turn_degrees)
    assert _gap(inner, 10.0, outer, outer_arm) <= 0.1


def test_right_angle_turns_keep_the_existing_concentric_rule():
    inner = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)]
    outer = [(0.0, -4.0), (104.0, -4.0), (104.0, 100.0)]
    assert 10.0 + turn_shift(*outer, (4.0, -4.0)) == pytest.approx(14.0)
    assert 10.0 + turn_shift(*inner, (0.0, 0.0)) == pytest.approx(10.0)


@pytest.mark.parametrize(
    ("name", "prefix"),
    [("rnaseq_sections.mmd", "%%metro track_gap: 0\n"), ("rnaseq_london_tube.mmd", "")],
)
def test_touching_fan_corners_leave_no_seam(name, prefix):
    path = _ROOT / "examples" / name
    _graph, theme, plan = _render(prefix + path.read_text(), path.parent)
    edge = ("umi_tools_extract", "trimgalore")
    lanes = _fan_lanes(plan, {edge})[edge]
    assert len(lanes) == 6
    assert _worst_curve_seam(lanes, theme.line_width) <= 0.1


def _worst_curve_seam(lanes, line_width: float) -> float:
    """Widest gap between neighbouring touching lanes anywhere on a curve."""
    worst = 0.0
    ordered = _by_start(lanes)
    for (pa, ra), (pb, rb) in zip(ordered, ordered[1:]):
        other = [q for q, _ in _samples(pb, rb, 150)]
        for q, on_curve in _samples(pa, ra, 150):
            if on_curve:
                worst = max(worst, min(math.dist(q, s) for s in other) - line_width)
    return worst


_PREPROCESSING_FAN = (
    ("umi_tools_extract", "trimgalore"),
    ("trimgalore", "fastqc_trimmed"),
    ("fastqc_trimmed", "sortmerna"),
    ("fastqc_trimmed", "ribodetector"),
    ("sortmerna", "fastqc_filtered"),
    ("ribodetector", "fastqc_filtered"),
)


def _pitch_error(inner, outer, pitch: float) -> float:
    """Largest departure from *pitch* of the distance between two lanes,
    measured from every point of *outer*: straights, diagonals and arcs."""
    targets = [q for q, _ in _samples(*inner, 150)]
    return max(
        abs(min(math.dist(q, s) for s in targets) - pitch)
        for q, _ in _samples(*outer, 150)
    )


def _arc_centres(points, radii):
    resolved = resolve_curve_radii(points, radii)
    return [
        _centre(points[k - 1 : k + 2], resolved[k - 1])
        for k in range(1, len(points) - 1)
    ]


def _render(text: str, source_dir: Path, theme_file: Path | None = None):
    if theme_file is not None:
        text = f"%%metro style: {theme_file}\n" + text
    graph = prepare_graph(text, source_dir=str(source_dir))
    theme = resolve_theme(graph)
    return graph, theme, build_render_plan(graph, theme)


def _fan_lanes(plan, edges):
    """Each of *edges*' lanes as ``(points, radii)``, keyed by edge."""
    lanes = defaultdict(list)
    for route, points, radii in zip(
        plan.routes, plan.route_polylines, plan.route_curve_radii
    ):
        key = (route.edge.source, route.edge.target)
        if key in edges:
            lanes[key].append((list(points), None if radii is None else list(radii)))
    return lanes


def _by_start(lanes):
    return sorted(lanes, key=lambda lane: (lane[0][0][1], lane[0][0][0]))


def _assert_concentric(graph, theme, plan, edges) -> None:
    """Neighbouring lanes of every bundle in *edges* keep one pitch apart along
    the whole edge and turn each corner about one centre."""
    bundles = _fan_lanes(plan, edges)
    assert set(bundles) == set(edges)
    pitch = graph_offset_step(graph, theme.line_width)
    pitch_errors: dict[tuple[str, str], float] = {}
    centre_errors: dict[tuple[str, str], float] = {}
    for key, lanes in bundles.items():
        ordered = _by_start(lanes)
        pitch_errors[key] = round(
            max(_pitch_error(a, b, pitch) for a, b in zip(ordered, ordered[1:])), 2
        )
        worst = 0.0
        for a, b in zip(ordered, ordered[1:]):
            assert len(a[0]) == len(b[0]), key
            for ca, cb in zip(_arc_centres(*a), _arc_centres(*b)):
                worst = max(worst, math.dist(ca, cb))
        centre_errors[key] = round(worst, 2)
    assert max(pitch_errors.values()) <= 0.1, pitch_errors
    assert max(centre_errors.values()) <= 0.1, centre_errors


def _theme_file(tmp_path: Path, theme_text: str) -> Path:
    theme_file = tmp_path / "theme.yaml"
    theme_file.write_text(theme_text)
    return theme_file


def _assert_rnaseq_fan_concentric(theme_text: str, prefix: str, tmp_path) -> None:
    path = _ROOT / "examples" / "rnaseq_sections.mmd"
    rendered = _render(
        prefix + path.read_text(), path.parent, _theme_file(tmp_path, theme_text)
    )
    _assert_concentric(*rendered, _PREPROCESSING_FAN)


@pytest.mark.parametrize("ratio", [None, 2.0, 3.0, 4.5])
@pytest.mark.parametrize("line_width", [3, 5, 6, 9])
def test_wide_fan_lanes_stay_concentric(tmp_path, line_width, ratio):
    """A fan whose diagonals differ in steepness, at any line width and
    corner radius."""
    layout = "" if ratio is None else f"layout:\n  curve_radius_ratio: {ratio}\n"
    _assert_rnaseq_fan_concentric(
        f"name: wide\nline_width: {line_width}\n{layout}",
        "%%metro track_gap: 0\n",
        tmp_path,
    )


def test_gapped_wide_fan_lanes_stay_concentric(tmp_path):
    """A wide butt-capped line with a one-pixel gap between lanes."""
    _assert_rnaseq_fan_concentric(
        "name: wide\nline_width: 9\nline_cap: butt\n"
        "layout:\n  curve_radius_ratio: 2.0\n",
        "%%metro track_gap: 1\n",
        tmp_path,
    )


_HUB_FAN = (
    _ROOT
    / "tests"
    / "fixtures"
    / "curve_invariant_repros"
    / "hub_fan_mixed_drop_bundles.mmd"
)
_HUB_FAN_BUNDLES = (
    ("hub", "t2"),
    ("hub", "t3"),
    ("t2", "sink"),
    ("t3", "sink"),
)


# Every (line width, ratio) at which each direction aborts when each of the
# hub's bundles reserves only its own stagger.
_HUB_FAN_ABORTS = {
    "LR": {
        6: (4.5,),
        7: (3.5, 4.0, 4.5),
        8: (3.0, 3.5, 4.0, 4.5),
        9: (2.5, 3.0, 3.5, 4.0, 4.5),
    },
    "RL": {
        6: (4.5,),
        7: (3.5, 4.0, 4.5),
        8: (3.0, 3.5, 4.0, 4.5),
        9: (2.5, 3.0, 3.5, 4.0, 4.5),
    },
    "TB": {6: (4.0, 4.5), 7: (3.5, 4.0, 4.5), 8: (3.0, 3.5), 9: (2.5, 3.0)},
}
_HUB_FAN_CASES = sorted(
    {
        (direction, width, ratio)
        for direction in _HUB_FAN_ABORTS
        for width in (3, 6, 9)
        for ratio in (2.0, 3.0, 4.5)
    }
    | {
        (direction, width, ratio)
        for direction, by_width in _HUB_FAN_ABORTS.items()
        for width, ratios in by_width.items()
        for ratio in ratios
    }
)


@pytest.mark.parametrize(("direction", "line_width", "ratio"), _HUB_FAN_CASES)
def test_hub_fanning_bundles_of_mixed_drop_stay_concentric(
    tmp_path, direction, line_width, ratio
):
    """A hub spreads every lane it fans one way as one group, so each of its
    bundles needs room for the whole group's stagger, not only its own.  The
    parity check that Stage 1.2's ``_spread_groups`` matches the spread pass."""
    text = _HUB_FAN.read_text()
    assert "direction: LR" in text
    theme = _theme_file(
        tmp_path,
        f"name: wide\nline_width: {line_width}\n"
        f"layout:\n  curve_radius_ratio: {ratio}\n",
    )
    rendered = _render(
        "%%metro track_gap: 0\n"
        + text.replace("direction: LR", f"direction: {direction}"),
        _HUB_FAN.parent,
        theme,
    )
    _assert_concentric(*rendered, _HUB_FAN_BUNDLES)


@pytest.mark.parametrize(
    ("name", "line_width", "ratio", "edges"),
    [
        pytest.param(
            "rnaseq_metro.mmd",
            lw,
            ratio,
            (("fastp", "fastqc_trimmed"), ("trimgalore", "fastqc_trimmed")),
            id=f"rnaseq_metro-lw{lw}-r{ratio}",
        )
        for lw, ratio in ((6, 4.5), (9, 3.0), (9, 4.5))
    ]
    + [
        pytest.param(
            "genomic_pipeline.mmd",
            9,
            4.5,
            (("applybqsr", "mosdepth"), ("applybqsr", "ngscheckmate")),
            id="genomic_pipeline-applybqsr",
        ),
        pytest.param(
            "genomeassembly_staggered.mmd",
            9,
            4.5,
            (("scaffolding_bwamem2", "yahs"), ("scaffolding_minimap2", "yahs")),
            id="genomeassembly_staggered-yahs",
        ),
    ],
)
def test_gallery_fans_keep_their_stagger_room(tmp_path, name, line_width, ratio, edges):
    """Stations settled after Stage 1.2 (loop-side recentring, bubble centring)
    and the Stage 6.4 grid snap leave each fan the room its stagger needs, and
    a diagonal too short for both of its corners at the base radius stays
    concentric at each end."""
    path = _ROOT / "examples" / name
    theme = _theme_file(
        tmp_path,
        f"name: wide\nline_width: {line_width}\n"
        f"layout:\n  curve_radius_ratio: {ratio}\n",
    )
    rendered = _render("%%metro track_gap: 0\n" + path.read_text(), path.parent, theme)
    _assert_concentric(*rendered, edges)


def _unit(a, b):
    length = math.dist(a, b)
    return ((b[0] - a[0]) / length, (b[1] - a[1]) / length)


def _fused_diagonal_turns(plan):
    """Same-line diagonal corners sharing a vertex and one leg.

    Keyed by line, vertex and shared leg; each value lists, per route, how far
    along that leg its curve meets it.
    """
    turns = defaultdict(list)
    for route, points, radii in zip(
        plan.routes, plan.route_polylines, plan.route_curve_radii
    ):
        points = list(points)
        arms = resolve_curve_radii(points, None if radii is None else list(radii))
        for k in range(1, len(points) - 1):
            u_in, u_out = (
                _unit(points[k - 1], points[k]),
                _unit(points[k], points[k + 1]),
            )
            dot = u_in[0] * u_out[0] + u_in[1] * u_out[1]
            if abs(u_in[0] * u_out[1] - u_in[1] * u_out[0]) < 1e-6 or abs(dot) < 1e-6:
                continue
            vertex = (round(points[k][0]), round(points[k][1]))
            corner = points[k]
            for side, leg, sign in (("in", u_in, -1.0), ("out", u_out, 1.0)):
                along = corner[0] * leg[0] + corner[1] * leg[1] + sign * arms[k - 1]
                key = (
                    route.line_id,
                    vertex,
                    side,
                    (round(leg[0], 3), round(leg[1], 3)),
                )
                turns[key].append(((route.edge.source, route.edge.target), along))
    return {key: legs for key, legs in turns.items() if len(legs) > 1}


@pytest.mark.parametrize(
    "name",
    ["genomic_pipeline.mmd", "rnaseq_sections.mmd", "epitopeprediction.mmd"],
)
def test_fused_same_line_corners_meet_their_shared_leg_once(name):
    """One line turning onto (or off) a shared leg at one vertex from several
    edges draws one corner: every curve meets that leg at the same point, even
    where some of the edges bundle with other lines and some run alone."""
    path = _ROOT / "examples" / name
    _graph, _theme, plan = _render(path.read_text(), path.parent)
    spreads = {
        key: round(max(a for _e, a in legs) - min(a for _e, a in legs), 2)
        for key, legs in _fused_diagonal_turns(plan).items()
    }
    assert {key: s for key, s in spreads.items() if s > 0.1} == {}


def test_genomic_hub_join_fuses_lone_and_bundled_legs():
    """The vcfs join takes ten tumor_only legs, two of them running alone."""
    path = _ROOT / "examples" / "genomic_pipeline.mmd"
    _graph, _theme, plan = _render(path.read_text(), path.parent)
    red = [
        legs
        for (line, _v, side, _leg), legs in _fused_diagonal_turns(plan).items()
        if line == "tumor_only" and side == "out" and len(legs) == 10
    ]
    assert len(red) == 1
    assert {edge for edge, _a in red[0]} >= {
        ("lofreq", "vcfs"),
        ("msisensor2", "vcfs"),
        ("freebayes", "vcfs"),
    }


# One cubic per corner holds a circle to 1e-3 of its radius up to a right
# angle, the largest turn routing draws; past it the single cubic's own
# approximation error grows, to about 3.1e-3 at 135 degrees.
@pytest.mark.parametrize(
    ("turn_degrees", "bound"),
    [
        (30.0, 1e-3),
        (45.0, 1e-3),
        (61.0, 1e-3),
        (90.0, 1e-3),
        (120.0, 2e-3),
        (135.0, 3.5e-3),
    ],
)
def test_a_drawn_corner_is_a_circular_arc(turn_degrees, bound):
    """Every point of a drawn corner sits on the circle tangent to both legs."""
    from nf_metro.layout.routing.corners import corner_arc_controls, cubic_point

    phi = math.radians(turn_degrees)
    arm = 10.0
    corner = (100.0, 100.0)
    before = (corner[0] - arm, corner[1])
    after = (corner[0] + arm * math.cos(phi), corner[1] - arm * math.sin(phi))
    radius = arm / math.tan(phi / 2)
    centre = (before[0], before[1] - radius)
    handles = corner_arc_controls(before, corner, after)
    worst = max(
        abs(math.dist(cubic_point(before, *handles, after, k / 200), centre) - radius)
        for k in range(201)
    )
    assert worst <= bound * radius
