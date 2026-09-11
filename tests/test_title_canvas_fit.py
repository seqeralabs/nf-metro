"""The canvas is wide enough to hold the map title.

The title is authored text drawn at ``x=padding``; a canvas sized from section
content alone clips it at the right edge with no warning. These tests hold that
the title's drawn right edge stays within the canvas width.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.api import prepare_graph
from nf_metro.render.svg import RenderPlan, build_render_plan
from nf_metro.text_metrics import (
    DEFAULT_TEXT_METRICS,
    MetricsFace,
    TextRole,
    metrics_face_context,
    text_style,
)
from nf_metro.themes import resolve_theme

ROOT = Path(__file__).parents[1]

# Both narrow-map reproducers from the issue, plus a wide gallery fixture whose
# content already exceeds its title, so the invariant is exercised on a map the
# fix must leave alone as well as on the two it must grow.
TITLE_FIXTURES = (
    "examples/topologies/lr_perp_top_entry_bottom_exit.mmd",
    "examples/topologies/lr_to_tb_top_drop_two_lines.mmd",
    "examples/topologies/convergent_offrow_exit_climb.mmd",
)


def _plan(path: Path) -> RenderPlan:
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    return build_render_plan(graph, resolve_theme(None, graph))


def _title_right_edge(plan: RenderPlan) -> float:
    """Right edge of the title as drawn: ``x=padding`` plus its true advance."""
    with metrics_face_context(MetricsFace.FALLBACK):
        advance = DEFAULT_TEXT_METRICS.advance(
            plan.graph.title,
            text_style(plan.theme.title_font_size, "bold"),
            TextRole.TITLE,
        )
    return plan.padding + advance


@pytest.mark.parametrize("name", TITLE_FIXTURES)
def test_title_fits_within_canvas_width(name: str) -> None:
    plan = _plan(ROOT / name)
    assert plan.graph.title, f"{name} carries no title to test"
    assert not plan.show_logo and not plan.logo_in_legend and not plan.bare, (
        f"{name} does not draw a standalone title"
    )
    assert _title_right_edge(plan) <= plan.svg_width, (
        f"{name} clips its title: right edge {_title_right_edge(plan):.1f} "
        f"exceeds canvas width {plan.svg_width}"
    )
