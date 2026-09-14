"""Confirm that metrics and validators inspect the geometry drawn in the SVG."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

import pytest
from conftest import parse_and_layout
from layout_metrics import measured_geometry

from nf_metro.layout.routing.common import RoutedPath, apply_route_offsets
from nf_metro.parser.model import MetroGraph
from nf_metro.render import build_render_plan, emit_render_plan
from nf_metro.render.plan import RenderPlan
from nf_metro.render.validate import _expected_line_segments, parse_route_polylines
from nf_metro.themes import THEMES

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
FIXTURES = [
    EXAMPLES / "sarek_metro.mmd",
    EXAMPLES / "diagonal_labels.mmd",
    EXAMPLES / "longread_variant_calling.mmd",
    EXAMPLES / "guide" / "04_directions.mmd",
    EXAMPLES / "topologies" / "bypass_leftward_far_side_entry.mmd",
    EXAMPLES / "simple_pipeline.mmd",
    EXAMPLES / "rnaseq_auto.mmd",
]
_PRECISION = 3
Point = tuple[float, float]
VerticesByLine = dict[str, set[Point]]
SegmentsByLine = dict[str, list[tuple[Point, Point]]]


def _render(path: Path) -> tuple[MetroGraph, RenderPlan, str]:
    """Build a plan and SVG for one example map."""
    graph = parse_and_layout(path.read_text(), source_dir=str(path.parent))
    theme = THEMES[graph.style if graph.style in THEMES else "nfcore"]
    plan = build_render_plan(graph, theme)
    return graph, plan, emit_render_plan(plan)


def _vertices_by_line(
    runs: Iterable[tuple[str, Iterable[Point]]],
) -> VerticesByLine:
    """Group rounded route vertices by metro line."""
    out: VerticesByLine = defaultdict(set)
    for line_id, points in runs:
        out[line_id].update(
            (round(x, _PRECISION), round(y, _PRECISION)) for x, y in points
        )
    return out


def _ink_vertices(svg: str) -> VerticesByLine:
    """Read all route vertices from the rendered SVG."""
    return _vertices_by_line(
        (line_id, subpath)
        for line_id, subpaths in parse_route_polylines(svg)
        for subpath in subpaths
    )


def _route_vertices(
    offsets: dict[tuple[str, str], float], routes: list[RoutedPath]
) -> VerticesByLine:
    """Apply route offsets and group the result by metro line."""
    return _vertices_by_line(
        (route.line_id, apply_route_offsets(route, offsets)) for route in routes
    )


def _segment_vertices(segments: SegmentsByLine) -> VerticesByLine:
    """Group segment endpoints by metro line."""
    return _vertices_by_line(
        (line_id, segment) for line_id, values in segments.items() for segment in values
    )


def _assert_contained(measured: VerticesByLine, svg: str, source: str) -> None:
    """Assert that every measured vertex appears in the rendered SVG."""
    ink = _ink_vertices(svg)
    stray = {
        line_id: sorted(vertices - ink[line_id])
        for line_id, vertices in measured.items()
        if vertices - ink[line_id]
    }
    assert not stray, (
        f"{source} contains {sum(len(points) for points in stray.values())} "
        "route vertices that do not appear in the SVG: "
        + "; ".join(
            f"{line_id}: {points[:3]}" for line_id, points in sorted(stray.items())
        )
    )


@pytest.mark.parametrize("path", FIXTURES, ids=lambda path: path.stem)
def test_scorecard_measures_emitted_plan(path: Path) -> None:
    """Every route vertex scored by the metrics appears in the SVG."""
    graph, plan, svg = _render(path)
    _assert_contained(
        _route_vertices(*measured_geometry(graph, plan)),
        svg,
        "layout metrics",
    )


@pytest.mark.parametrize("path", FIXTURES, ids=lambda path: path.stem)
def test_offset_oracle_measures_emitted_plan(path: Path) -> None:
    """Every route vertex checked for collapse appears in the SVG."""
    _graph, plan, svg = _render(path)
    _assert_contained(
        _segment_vertices(_expected_line_segments(plan)),
        svg,
        "offset-collapse validation",
    )


def test_plan_geometry_is_independent_of_source_graph() -> None:
    """Changing the source graph cannot change an existing plan."""
    graph, plan, _svg = _render(EXAMPLES / "sarek_metro.mmd")
    before = plan.offset_polylines()
    next(iter(graph.stations.values())).x += 1000
    assert plan.offset_polylines() == before


@pytest.mark.parametrize("path", FIXTURES, ids=lambda path: path.stem)
def test_reading_plan_geometry_is_pure(path: Path) -> None:
    """Metrics and validation do not change the graph or plan."""
    graph, plan, _svg = _render(path)
    before = {station_id: (s.x, s.y) for station_id, s in graph.stations.items()}
    offsets, _ = measured_geometry(graph, plan)
    _expected_line_segments(plan)
    assert {
        station_id: (s.x, s.y) for station_id, s in graph.stations.items()
    } == before
    assert measured_geometry(graph, plan)[0] == offsets
