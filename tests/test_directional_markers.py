"""Tests for static directional chevrons (``--directional``)."""

import pathlib

import pytest

from nf_metro.api import RenderConfig, prepare_graph, render_graph, resolve_theme
from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid_file
from nf_metro.render import build_render_plan
from nf_metro.render.svg import _chevron_samples, _drawn_pieces, render_svg
from nf_metro.render.validate import check_chevrons_on_stroke, parse_chevrons
from nf_metro.themes import NFCORE_DARK_THEME

EXAMPLES_DIR = pathlib.Path(__file__).parent.parent / "examples"

DIRECTION_FIXTURES = [
    "simple_pipeline",
    "rnaseq_auto",
    "genomic_pipeline",
    "longread_variant_calling",
]


def _laid_out(stem: str):
    graph = parse_metro_mermaid_file(EXAMPLES_DIR / f"{stem}.mmd")
    compute_layout(graph)
    return graph


def _render(stem: str, *, directional: bool) -> str:
    graph = _laid_out(stem)
    graph.directional = directional
    return render_svg(graph, NFCORE_DARK_THEME)


def test_directional_off_by_default_emits_no_chevrons():
    svg = _render("simple_pipeline", directional=False)
    assert "metro-direction-" not in svg


@pytest.mark.parametrize("stem", DIRECTION_FIXTURES)
def test_directional_flag_emits_chevrons(stem):
    svg = _render(stem, directional=True)
    assert "metro-direction-" in svg


@pytest.mark.parametrize("stem", DIRECTION_FIXTURES)
def test_chevron_headings_point_downstream(stem):
    """Every chevron points along its route's drawn stroke, source to target.

    Samples come from the render plan's drawn geometry (the polylines and
    corner radii the stroke is painted from), so each must lie on one of its
    route's drawn pieces, a rounded corner included, and head along that
    piece's source-to-target tangent there.
    """
    graph = _laid_out(stem)
    graph.directional = True
    plan = build_render_plan(graph, NFCORE_DARK_THEME)

    spacing = NFCORE_DARK_THEME.directional_marker_spacing
    min_length = 2 * NFCORE_DARK_THEME.directional_marker_size

    checked = 0
    for pts, radii in zip(plan.route_polylines, plan.route_curve_radii, strict=True):
        pieces = _drawn_pieces(pts, radii)
        for point, heading in _chevron_samples(pieces, spacing, min_length):
            assert _heads_forward_on_pieces(pieces, point, heading)
            checked += 1

    assert checked > 0, f"{stem} produced no chevrons to check"


def _piece_chords(piece, steps=64):
    if piece.control is None:
        return [(piece.start, piece.end)]
    (ax, ay), (cx, cy), (bx, by) = piece.start, piece.control, piece.end
    points = [
        (
            (1 - t) ** 2 * ax + 2 * (1 - t) * t * cx + t * t * bx,
            (1 - t) ** 2 * ay + 2 * (1 - t) * t * cy + t * t * by,
        )
        for t in (k / steps for k in range(steps + 1))
    ]
    return list(zip(points, points[1:]))


def _heads_forward_on_pieces(pieces, point, heading, tol=0.05):
    """A chevron heading must run forward along a drawn chord it lies on.

    Quadratic corners are flattened finely enough that a point on the curve
    lies within *tol* of a chord whose direction matches the curve's tangent
    there; a reversed or leg-aligned heading inside a corner matches none.
    """
    px, py = point
    ux, uy = heading
    for piece in pieces:
        for (ax, ay), (bx, by) in _piece_chords(piece):
            seg_len_sq = (bx - ax) ** 2 + (by - ay) ** 2
            if seg_len_sq == 0:
                continue
            t = ((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / seg_len_sq
            if not -0.01 <= t <= 1.01:
                continue
            cx, cy = ax + t * (bx - ax), ay + t * (by - ay)
            if abs(cx - px) >= tol or abs(cy - py) >= tol:
                continue
            forward = ((bx - ax) * ux + (by - ay) * uy) / seg_len_sq**0.5
            if forward > 0.999:
                return True
    return False


# Tight rounded corners under a heavy stroke, so a chevron sampled on the
# pre-smoothing polyline sits visibly off the painted curve.
FLOATING_CHEVRONS = """\
%%metro title: Floating chevrons
%%metro line: a | Line A | #e6007e
%%metro line: b | Line B | #1f3a93
%%metro line: c | Line C | #29b6f6
%%metro stroke_scale: 2.5
%%metro grid: s1 | 0,0
%%metro grid: s2 | 0,1

graph LR
    subgraph s1 [Section 1]
        a1[A1]
        a2[A2]
        a3[A3]
        a1 -->|a,b,c| a2
        a2 -->|a,b,c| a3
    end
    subgraph s2 [Section 2]
        b1[B1]
        b2[B2]
        b3[B3]
        b1 -->|a,b,c| b2
        b2 -->|a,b,c| b3
    end
    a3 -->|a,b,c| b1
"""


def _example_text(rel: str, prefix: str = "") -> tuple[str, str]:
    path = EXAMPLES_DIR / rel
    return prefix + path.read_text(), str(path.parent)


# Rounded corners at several stroke scales, plus maps whose routes carry
# bridge gaps where a chevron would otherwise land.
DRAWN_STROKE_CASES = {
    "floating_chevrons": lambda: (FLOATING_CHEVRONS, ""),
    "rnaseq_sections_stroke_1_5": lambda: _example_text(
        "rnaseq_sections.mmd", "%%metro stroke_scale: 1.5\n"
    ),
    "riboseq_metro": lambda: _example_text("riboseq_metro.mmd"),
    "genomic_pipeline": lambda: _example_text("genomic_pipeline.mmd"),
    "differentialabundance_default": lambda: _example_text(
        "differentialabundance_default.mmd"
    ),
    "longread_variant_calling": lambda: _example_text("longread_variant_calling.mmd"),
    "junction_entry_lane_rebase": lambda: _example_text(
        "topologies/junction_entry_lane_rebase.mmd"
    ),
    "same_destination_vertical_convergence": lambda: _example_text(
        "topologies/same_destination_vertical_convergence.mmd"
    ),
    "same_destination_short_overlap": lambda: _example_text(
        "topologies/same_destination_short_overlap.mmd"
    ),
}


def _directional_svg(text: str, source_dir: str) -> str:
    graph = prepare_graph(text, source_dir=source_dir)
    graph.directional = True
    return render_graph(graph, resolve_theme(None, graph), RenderConfig())


@pytest.mark.parametrize("case", sorted(DRAWN_STROKE_CASES))
def test_chevrons_ride_their_drawn_stroke(case):
    """Every chevron sits on, and points along, its own line's painted stroke.

    The stroke is read back from the SVG with its quadratic corners, so a
    chevron inside a rounded corner is measured against the curve, and one
    reaching into a bridge gap is caught as well.
    """
    svg = _directional_svg(*DRAWN_STROKE_CASES[case]())
    assert parse_chevrons(svg), f"{case} drew no chevrons"
    findings = check_chevrons_on_stroke(svg)
    assert not findings, [f.message for f in findings]
