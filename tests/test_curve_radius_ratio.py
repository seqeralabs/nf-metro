"""The corner radius is a per-render quantity, sized to the line it bends.

A theme (or a map, or the CLI) states the radius as ``curve_radius_ratio``, a
multiple of the theme's line width measured at the line centreline; unset, the
engine's base radius applies.  ``stroke_scale`` scales either.  Every
clearance that exists to make room for a corner grows with the radius, so a
larger radius lays out, routes and validates like the base one.
"""

from __future__ import annotations

import ast
import math
import re
import warnings
from pathlib import Path

import pytest

from nf_metro.api import prepare_graph, render_string
from nf_metro.layout.constants import CURVE_RADIUS, INTER_ROW_EDGE_CLEARANCE
from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render import render_svg
from nf_metro.render.path_geometry import iter_path_commands
from nf_metro.themes import THEMES

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
TOPOLOGIES = EXAMPLES / "topologies"
SRC = ROOT / "src" / "nf_metro"

_METRO_PATH = re.compile(r'<path d="([^"]*)"[^>]*class="metro-line-')


def _circumradius(
    a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]
) -> float:
    (ax, ay), (bx, by), (cx, cy) = a, b, c
    det = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(det) < 1e-9:
        return math.inf
    ab, bc, ca = math.dist(a, b), math.dist(b, c), math.dist(c, a)
    return ab * bc * ca / abs(det)


def drawn_corner_radii(svg: str) -> list[float]:
    """Centreline radius of every corner a metro line is drawn with.

    Each corner is one cubic; the circle through its two ends and its midpoint
    is the arc it approximates.
    """
    radii: list[float] = []
    for d in _METRO_PATH.findall(svg):
        pen: tuple[float, float] | None = None
        for command, nums in iter_path_commands(d):
            if command in "ML":
                pen = (nums[-2], nums[-1])
                continue
            assert pen is not None
            c1, c2, end = (nums[0], nums[1]), (nums[2], nums[3]), (nums[4], nums[5])
            mid = tuple(
                0.125 * pen[i] + 0.375 * c1[i] + 0.375 * c2[i] + 0.125 * end[i]
                for i in range(2)
            )
            radii.append(_circumradius(pen, (mid[0], mid[1]), end))
            pen = end
    return radii


def _render(path: Path, **kwargs: object) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return render_string(path.read_text(), source_dir=str(path.parent), **kwargs)  # type: ignore[arg-type]


def _laid_out(path: Path, ratio: float | None) -> object:
    graph = parse_metro_mermaid(path.read_text(), max_station_columns=15)
    graph.source_dir = str(path.parent)
    graph.curve_radius_ratio = ratio
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        compute_layout(graph, validate=True)
    return graph


@pytest.mark.parametrize(
    "path",
    [
        EXAMPLES / "rnaseq_london_tube.mmd",
        EXAMPLES / "genomic_pipeline.mmd",
        TOPOLOGIES / "fan_in_merge.mmd",
    ],
    ids=lambda p: p.stem,
)
def test_london_tube_bends_at_three_line_widths(path: Path) -> None:
    theme = THEMES["london-tube"]
    radii = drawn_corner_radii(_render(path, theme="london-tube"))
    assert radii
    assert min(radii) == pytest.approx(3.0 * theme.line_width, abs=0.1)


def test_the_default_theme_bends_at_the_base_radius() -> None:
    radii = drawn_corner_radii(_render(EXAMPLES / "rnaseq_sections.mmd"))
    assert min(radii) == pytest.approx(CURVE_RADIUS, abs=0.1)


@pytest.mark.parametrize(
    ("theme", "expected"),
    [("default", 1.6 * CURVE_RADIUS), ("london-tube", 1.6 * 3.0 * 4.0)],
)
def test_stroke_scale_scales_the_drawn_radius(theme: str, expected: float) -> None:
    radii = drawn_corner_radii(
        _render(
            EXAMPLES / "rnaseq_sections.mmd",
            theme=theme,
            layout_options={"stroke_scale": 1.6},
        )
    )
    assert min(radii) == pytest.approx(expected, abs=0.1)


def test_the_radius_follows_the_bound_metrics() -> None:
    from nf_metro.layout.pass_metrics import active_curve_radius, metrics_context

    assert active_curve_radius() == CURVE_RADIUS
    with metrics_context(stroke_scale=2.0):
        assert active_curve_radius() == 2 * CURVE_RADIUS
    with metrics_context(curve_radius_ratio=3.0, line_width=4.0):
        assert active_curve_radius() == 12.0
        with metrics_context(stroke_scale=1.5):
            assert active_curve_radius() == 18.0


@pytest.mark.parametrize("radius", [10.0, 12.0, 13.0, 20.0, 40.0])
def test_a_bypass_channel_holds_two_corners_at_any_radius(radius: float) -> None:
    from nf_metro.layout.pass_metrics import (
        active_curve_radius,
        bypass_clearance,
        inter_row_edge_clearance,
        metrics_context,
    )

    with metrics_context(curve_radius_ratio=radius / 4.0, line_width=4.0):
        assert bypass_clearance() >= 2 * active_curve_radius()
        assert bypass_clearance() == inter_row_edge_clearance()
        assert inter_row_edge_clearance() >= INTER_ROW_EDGE_CLEARANCE


def test_a_laid_out_graph_routes_at_its_own_radius() -> None:
    """Routing a laid-out graph outside any bound pass (a validator, a test)
    uses the radius the graph was laid out with."""
    from nf_metro.layout.routing import route_edges

    graph = prepare_graph(
        (EXAMPLES / "rnaseq_london_tube.mmd").read_text(),
        source_dir=str(EXAMPLES),
    )
    routes = route_edges(graph)
    assert graph.layout_metrics.curve_radius_ratio == 3.0
    radii = [r for route in routes for r in (route.curve_radii or ())]
    assert radii and min(radii) == pytest.approx(12.0)


def test_an_explicit_radius_is_the_radius_every_accessor_reads() -> None:
    from nf_metro.layout.pass_metrics import (
        active_curve_radius,
        curve_radius_context,
        inter_row_edge_clearance,
        metrics_context,
    )

    with metrics_context(curve_radius_ratio=3.0, line_width=4.0, stroke_scale=1.5):
        with curve_radius_context(30.0):
            assert active_curve_radius() == pytest.approx(30.0)
            assert inter_row_edge_clearance() == pytest.approx(60.0)
        assert active_curve_radius() == pytest.approx(18.0)


def test_an_explicit_routing_radius_binds_the_clearance_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``route_edges(curve_radius=...)`` builds its context at the radius the
    clearance helpers read during the call."""
    from nf_metro.layout.pass_metrics import active_curve_radius
    from nf_metro.layout.routing import core, route_edges

    graph = prepare_graph(
        (EXAMPLES / "rnaseq_london_tube.mmd").read_text(),
        source_dir=str(EXAMPLES),
    )
    seen: list[tuple[float, float]] = []
    build = core._build_routing_context

    def spy(graph, diagonal_run, curve_radius, *args, **kwargs):
        seen.append((curve_radius, active_curve_radius()))
        return build(graph, diagonal_run, curve_radius, *args, **kwargs)

    monkeypatch.setattr(core, "_build_routing_context", spy)
    route_edges(graph, curve_radius=14.0)
    assert seen
    for ctx_radius, helper_radius in seen:
        assert ctx_radius == helper_radius == pytest.approx(14.0)


# Fixtures whose corner clearances at a larger radius rest on reservations
# that have to grow with it.
RADIUS_SENSITIVE = [
    EXAMPLES / "genomeassembly_staggered.mmd",
    TOPOLOGIES / "left_exit_wrap_under_own_section.mmd",
    TOPOLOGIES / "merge_bottom_row_bypass.mmd",
]


@pytest.mark.parametrize("path", RADIUS_SENSITIVE, ids=lambda p: p.stem)
def test_a_larger_radius_lays_out_cleanly(path: Path) -> None:
    line_width = THEMES["default"].line_width
    graph = _laid_out(path, 12.0 / line_width)
    radii = {
        round(r, 1) for r in drawn_corner_radii(render_svg(graph, THEMES["default"]))
    }
    assert 12.0 in radii
    assert CURVE_RADIUS not in radii


REPRESENTATIVE = [
    EXAMPLES / "rnaseq_sections.mmd",
    EXAMPLES / "genomic_pipeline.mmd",
    TOPOLOGIES / "fan_in_merge.mmd",
    TOPOLOGIES / "fold_double.mmd",
    TOPOLOGIES / "bt_fork.mmd",
    TOPOLOGIES / "merge_pullaway.mmd",
]


@pytest.mark.parametrize("radius", [10.0, 12.0, 14.0, 16.0])
@pytest.mark.parametrize("path", REPRESENTATIVE, ids=lambda p: p.stem)
def test_radius_sweep_lays_out_cleanly(path: Path, radius: float) -> None:
    _laid_out(path, radius / THEMES["default"].line_width)


def _names_read(tree: ast.AST, names: frozenset[str]) -> set[str]:
    found = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    found |= {
        alias.name
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
        for alias in n.names
    }
    return found & names


def test_only_the_pass_metrics_read_the_base_radius() -> None:
    """A module reading the base constants directly ignores the theme's radius."""
    base = frozenset(
        {
            "CURVE_RADIUS",
            "INTER_ROW_EDGE_CLEARANCE",
            "MIN_STATION_FLAT_LENGTH",
            "MIN_STRAIGHT_EDGE",
        }
    )
    allowed = {SRC / "layout" / "constants.py", SRC / "layout" / "pass_metrics.py"}
    offenders = {
        str(path.relative_to(SRC)): sorted(read)
        for path in SRC.rglob("*.py")
        if path not in allowed
        and (read := _names_read(ast.parse(path.read_text()), base))
    }
    assert not offenders
