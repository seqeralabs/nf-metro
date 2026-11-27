"""The canvas holds ink drawn outside the section-box envelope.

A route that runs left of, or above, every section box is outside the envelope
the boxes were placed with, so the margin that envelope leaves says nothing
about how much room the route has: on the two sides whose edge sits at the
coordinate origin the ink has nowhere to go, and is drawn against -- or beyond
-- the canvas edge instead.  These tests hold that such a route is given the
room the layout gives the first section box on that side, so the map reads one
margin per side whichever of the two is outermost.

They also hold that the boundary those margins settle on is the one a
content-framed decoration is placed against: a legend the author left unpinned
sits on the edge of the drawn content, whether a box or a run defines it.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from nf_metro.api import prepare_graph
from nf_metro.layout.constants import CANVAS_ORIGIN_MARGIN
from nf_metro.parser.model import MetroGraph, PermissiveGuardWarning
from nf_metro.render.svg import RenderPlan, build_render_plan
from nf_metro.themes import resolve_theme

ROOT = Path(__file__).parents[1]

# The two arms of the defect, plus corpus fixtures that escape the envelope on
# each side, so the invariant is exercised beyond its own reproducers.
LEFT_ESCAPING = (
    "examples/topologies/wrap_return_canvas_margin.mmd",
    "examples/topologies/inter_row_wrap_clearance.mmd",
    "examples/diagonal_labels.mmd",
)
TOP_ESCAPING = (
    "examples/topologies/lr_perp_top_exit_perp_entry.mmd",
    "examples/topologies/tb_lr_exit_left.mmd",
    "examples/topologies/cross_col_top_entry.mmd",
)


def _plan(
    path: Path, *, strict: bool = False, legend_position: str | None = None
) -> RenderPlan:
    """Build *path*'s render plan, refusing any downgraded geometry guard.

    Only that category is judged, so unrelated warnings stay visible instead of
    being swallowed by a blanket filter.
    """
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    graph.strict = strict
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        plan = build_render_plan(
            graph, resolve_theme(graph), legend_position=legend_position
        )
    downgraded = [
        item for item in caught if issubclass(item.category, PermissiveGuardWarning)
    ]
    assert not downgraded, [str(item.message) for item in downgraded]
    return plan


def _box_envelope(graph: MetroGraph) -> tuple[float, float]:
    """Top-left corner of the union of the drawn section boxes."""
    boxes = [section for section in graph.sections.values() if section.bbox_w > 0]
    return (
        min(section.bbox_x for section in boxes),
        min(section.bbox_y for section in boxes),
    )


def _drawn_ink_origin(plan: RenderPlan) -> tuple[float, float]:
    """Leftmost and topmost coordinate any route or station is drawn at."""
    points = [point for polyline in plan.route_polylines for point in polyline]
    stations = [
        station
        for station in plan.graph.stations.values()
        if not station.is_port and not station.is_hidden
    ]
    return (
        min([point[0] for point in points] + [station.x for station in stations]),
        min([point[1] for point in points] + [station.y for station in stations]),
    )


@pytest.mark.parametrize("name", LEFT_ESCAPING)
def test_ink_left_of_the_box_envelope_keeps_the_canvas_margin(name: str) -> None:
    plan = _plan(ROOT / name)
    envelope_x, _ = _box_envelope(plan.graph)
    ink_x, _ = _drawn_ink_origin(plan)
    assert ink_x < envelope_x, f"{name} draws nothing left of its box envelope"
    assert ink_x >= CANVAS_ORIGIN_MARGIN[0], (
        f"{name} draws ink at x={ink_x:.1f}, inside the "
        f"{CANVAS_ORIGIN_MARGIN[0]:.0f}px the layout leaves between the canvas "
        f"edge and a first section box"
    )


@pytest.mark.parametrize("name", TOP_ESCAPING)
def test_ink_above_the_box_envelope_keeps_the_canvas_margin(name: str) -> None:
    plan = _plan(ROOT / name)
    _, envelope_y = _box_envelope(plan.graph)
    _, ink_y = _drawn_ink_origin(plan)
    assert ink_y < envelope_y, f"{name} draws nothing above its box envelope"
    assert ink_y >= CANVAS_ORIGIN_MARGIN[1], (
        f"{name} draws ink at y={ink_y:.1f}, inside the "
        f"{CANVAS_ORIGIN_MARGIN[1]:.0f}px the layout leaves between the canvas "
        f"edge and a first section header"
    )


def _content_boundary(plan: RenderPlan) -> tuple[float, float]:
    """Left and top edges of the drawn content: the box envelope or the ink."""
    envelope = _box_envelope(plan.graph)
    ink = _drawn_ink_origin(plan)
    return min(envelope[0], ink[0]), min(envelope[1], ink[1])


@pytest.mark.parametrize("name", LEFT_ESCAPING)
def test_an_unpinned_legend_starts_at_the_left_content_boundary(name: str) -> None:
    """A ``bottom`` legend's left edge is the left edge of the drawn content.

    Each of these maps runs a line left of every section box, so the boundary
    is the run's, and a legend placed against a box edge instead is indented
    from the content above it.
    """
    plan = _plan(ROOT / name)
    assert plan.show_legend
    assert plan.graph.legend_at is None, f"{name} pins its legend"
    left, _ = _content_boundary(plan)
    assert plan.legend_x == pytest.approx(left)


@pytest.mark.parametrize("name", TOP_ESCAPING)
def test_an_unpinned_legend_starts_at_the_top_content_boundary(name: str) -> None:
    """A ``right`` legend's top edge is the top edge of the drawn content.

    The side placement is what reads the top boundary; each of these maps runs
    a line above every section box, so the boundary is the run's.
    """
    plan = _plan(ROOT / name, legend_position="right")
    assert plan.show_legend
    assert plan.graph.legend_at is None, f"{name} pins its legend"
    _, top = _content_boundary(plan)
    assert plan.legend_y == pytest.approx(top)


@pytest.mark.parametrize("name", (LEFT_ESCAPING[0], TOP_ESCAPING[0]))
def test_a_canvas_margin_corridor_holds_its_claim_on_both_arms(name: str) -> None:
    """The strict canvas-corridor guard passes, rather than raising.

    The reproducers each seat a whole bundle in a canvas margin, which is the
    arrangement the guard refuses when the margin cannot hold it.
    """
    _plan(ROOT / name, strict=True)
