"""A channel in the corridor past the end of the grid hugs the last column.

Most vertical channels sit in a gap with a section column on either side, and
are centred between the two facing box edges.  A channel beyond the outermost
column has only one such edge: the other side is open canvas, and the gap-edge
lookup answers it with the coordinate origin.  Centring across that span puts
the channel halfway to the origin, so its distance from the box it belongs to
is a function of where the map as a whole sits -- move the map and the channel
follows only half as far.

These tests hold the channel a fixed clearance off its one real edge at both
ends of the grid, which makes it move rigidly with the map.  Rigid motion is
what the render's canvas-margin settlement assumes when it moves a map clear of
the canvas edge and re-routes: a channel that answers a move with half of it
leaves that settlement chasing a target it never reaches.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from riboseq_map import RIBOSEQ_MMD

from nf_metro.api import prepare_graph
from nf_metro.layout.constants import EDGE_TO_BUNDLE_CLEARANCE, graph_offset_step
from nf_metro.layout.phases.canvas import translate_graph
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import col_left_edge, col_right_edge
from nf_metro.layout.routing.normalize import _gap_channel_base
from nf_metro.parser.model import MetroGraph, PermissiveGuardWarning
from nf_metro.render.svg import build_render_plan
from nf_metro.themes import resolve_theme

ROOT = Path(__file__).resolve().parent.parent

# Maps of different shapes and column counts, so the anchor is judged on more
# than the one arrangement that reaches it through a routed edge.
ANCHOR_FIXTURES = (
    "examples/rnaseq_sections.mmd",
    "examples/topologies/fan_bypass_shared_band.mmd",
    "examples/topologies/merge_right_entry.mmd",
)

# Corpus maps whose routes are already rigid, alongside the riboseq map, which
# draws the off-grid channel this module anchors.
RIGID_FIXTURES = (
    "examples/topologies/merge_right_entry.mmd",
    "examples/topologies/packed_cell_cellmate_bypass.mmd",
    "examples/rnaseq_sections.mmd",
)

_MOVE = 137.0


def _layout(name: str) -> MetroGraph:
    path = ROOT / name
    return prepare_graph(path.read_text(), source_dir=str(path.parent))


def _outermost_columns(graph: MetroGraph) -> tuple[int, int]:
    cols = sorted({s.grid_col for s in graph.sections.values() if s.bbox_w > 0})
    return cols[0], cols[-1]


def _off_grid_bases(graph: MetroGraph, n_lines: int) -> tuple[float, float]:
    """Channel base in the corridor before the first column and after the last."""
    first, last = _outermost_columns(graph)
    step = graph_offset_step(graph)
    return (
        _gap_channel_base(graph, first - 1, None, n_lines, step),
        _gap_channel_base(graph, last, None, n_lines, step),
    )


@pytest.mark.parametrize("name", ANCHOR_FIXTURES)
@pytest.mark.parametrize("n_lines", (1, 3))
def test_an_off_grid_corridor_seats_its_bundle_off_the_outermost_column(
    name: str, n_lines: int
) -> None:
    graph = _layout(name)
    first, last = _outermost_columns(graph)
    half_bundle = (n_lines - 1) * graph_offset_step(graph) / 2
    before, after = _off_grid_bases(graph, n_lines)
    assert before == pytest.approx(
        col_left_edge(graph, first) - EDGE_TO_BUNDLE_CLEARANCE - half_bundle
    )
    assert after == pytest.approx(
        col_right_edge(graph, last) + EDGE_TO_BUNDLE_CLEARANCE + half_bundle
    )


@pytest.mark.parametrize("name", ANCHOR_FIXTURES)
def test_an_off_grid_channel_moves_with_the_map(name: str) -> None:
    graph = _layout(name)
    before = _off_grid_bases(graph, 3)
    translate_graph(graph, _MOVE, 0.0)
    after = _off_grid_bases(graph, 3)
    assert after == pytest.approx(tuple(base + _MOVE for base in before))


def _routed_points(graph: MetroGraph) -> list[tuple[tuple[float, float], ...]]:
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return [tuple(route.points) for route in routes]


def _assert_routes_move_rigidly(source: str, source_dir: str = "") -> None:
    fixed = _routed_points(prepare_graph(source, source_dir=source_dir))
    moved_graph = prepare_graph(source, source_dir=source_dir)
    translate_graph(moved_graph, _MOVE, 0.0)
    moved = _routed_points(moved_graph)

    assert [len(route) for route in moved] == [len(route) for route in fixed]
    for route, shifted in zip(fixed, moved):
        for (x, y), (moved_x, moved_y) in zip(route, shifted):
            assert (moved_x, moved_y) == pytest.approx((x + _MOVE, y))


def test_routing_the_moved_riboseq_map_moves_every_route_with_it() -> None:
    """The map whose leftmost ink is an off-grid channel routes rigidly.

    The render moves a map clear of the canvas edge and re-routes it, so a
    coordinate that answers the move with less than the move leaves the render
    chasing a target it cannot reach.
    """
    _assert_routes_move_rigidly(RIBOSEQ_MMD)


@pytest.mark.parametrize("name", RIGID_FIXTURES)
def test_routing_a_moved_corpus_map_moves_every_route_with_it(name: str) -> None:
    path = ROOT / name
    _assert_routes_move_rigidly(path.read_text(), source_dir=str(path.parent))


def test_the_riboseq_map_settles_clear_of_the_canvas_margin() -> None:
    """The riboseq map's leftmost ink settles inside the render's move budget.

    That ink is the off-grid channel this module anchors, and it is the only
    thing the map draws left of its box envelope, so the settlement's
    convergence rests on it moving rigidly.
    """
    graph = prepare_graph(RIBOSEQ_MMD)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        build_render_plan(graph, resolve_theme(None, graph))
    downgraded = [
        item for item in caught if issubclass(item.category, PermissiveGuardWarning)
    ]
    assert not downgraded, [str(item.message) for item in downgraded]
