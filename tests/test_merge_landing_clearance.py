"""A merge feeder's straight landing run clears every box it passes beneath.

A feeder into a merge on a LEFT or RIGHT entry drops from its junction and
lands along the port's Y.  Where that run passes beneath a box from an earlier
grid row, the run keeps its straight shape and the map makes room for it: the
run sits at least the inter-row edge clearance below the box's bottom edge.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from nf_metro.layout.constants import SETTLEMENT_QUANTUM
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.pass_metrics import (
    graph_radius_context,
    inter_row_edge_clearance,
)
from nf_metro.layout.routing.common import RoutedPath, merge_junction_ids
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, PortSide
from nf_metro.render import svg as svg_module
from nf_metro.themes import THEMES

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
TOPOLOGIES = EXAMPLES / "topologies"

FIXTURES = [
    EXAMPLES / "genomeassembly_staggered.mmd",
    EXAMPLES / "genomeassembly.mmd",
    TOPOLOGIES / "merge_adjacent_feeder.mmd",
    TOPOLOGIES / "convergent_offrow_exit_climb.mmd",
]


def _render_settled(
    path: Path, radius: float, monkeypatch: pytest.MonkeyPatch
) -> tuple[MetroGraph, list[RoutedPath]]:
    graph = parse_metro_mermaid(path.read_text(), max_station_columns=15)
    graph.source_dir = str(path.parent)
    graph.curve_radius_ratio = radius / THEMES["default"].line_width
    settled: dict[str, object] = {}
    real = svg_module._settle_render_geometry

    def capture(
        drawn: MetroGraph, *args: object, **kwargs: object
    ) -> svg_module._SettledRenderGeometry:
        result = real(drawn, *args, **kwargs)  # type: ignore[arg-type]
        settled.update(graph=drawn, routes=result.routes)
        return result

    monkeypatch.setattr(svg_module, "_settle_render_geometry", capture)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        compute_layout(graph, validate=True)
        svg_module.render_svg(graph, THEMES["default"])
    return settled["graph"], settled["routes"]  # type: ignore[return-value]


def _landing_shortfalls(graph: MetroGraph, routes: list[RoutedPath]) -> list[str]:
    merges = merge_junction_ids(graph)
    with graph_radius_context(graph):
        required = inter_row_edge_clearance()
    shortfalls: list[str] = []
    for route in routes:
        if route.edge.target not in merges or len(route.points) < 2:
            continue
        (x0, y0), (x1, y1) = route.points[-2], route.points[-1]
        port = graph.ports.get(next(iter(graph.edges_from(route.edge.target))).target)
        if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        if abs(y0 - y1) > 1e-6:
            continue
        target = graph.section_for_port(port)
        lo_x, hi_x = sorted((x0, x1))
        for box in graph.sections.values():
            bottom = box.bbox_y + box.bbox_h
            if (
                box.id == target.id
                or box.bbox_h <= 0
                or box.grid_row >= target.grid_row
                or box.bbox_x >= hi_x
                or box.bbox_x + box.bbox_w <= lo_x
                or bottom > y1
            ):
                continue
            if y1 - bottom < required - 0.01:
                shortfalls.append(
                    f"{route.edge.source}->{route.edge.target} at y={y1:.1f} "
                    f"is {y1 - bottom:.1f} below {box.id} (needs {required:.1f})"
                )
    return shortfalls


@pytest.mark.parametrize("radius", [10.0, 12.0])
@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_merge_landing_run_clears_the_boxes_it_passes_beneath(
    path: Path, radius: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph, routes = _render_settled(path, radius, monkeypatch)
    assert routes
    assert _landing_shortfalls(graph, routes) == []


@pytest.mark.parametrize("radius", [10.0, 12.0])
def test_staggered_assemblies_landing_stays_straight(
    radius: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph, routes = _render_settled(
        EXAMPLES / "genomeassembly_staggered.mmd", radius, monkeypatch
    )
    entry = graph.stations["genome_statistics__entry_left_7"]
    scaffolding = graph.sections["scaffolding"]
    with graph_radius_context(graph):
        required = inter_row_edge_clearance()
    clearance = entry.y - (scaffolding.bbox_y + scaffolding.bbox_h)
    assert required <= clearance < required + SETTLEMENT_QUANTUM
    feeders = [route for route in routes if route.edge.target == "__merge_5"]
    assert len(feeders) == 3
    for route in feeders:
        assert len(route.points) == 4
        assert route.points[-2][1] == pytest.approx(entry.y)
