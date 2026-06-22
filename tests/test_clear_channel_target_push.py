"""Target-aware push direction for ``clear_channel_of_section_edge`` (#736).

When a fan-out descent channel grazes a wider section stacked in a lower row of
the source column, ``clear_channel_of_section_edge`` nudges the channel clear of
that section's edge.  The push must head toward the side the route's target sits
on; pushing toward the nearer edge regardless of the target sends a line on a
far-side detour across the canvas when the nearer edge faces away from its
target.

Covers:

* The function pushes a grazing channel toward whichever side carries the
  route's target, and only falls back to the nearer edge when the target sits
  within the grazed section's own X span (genuinely ambiguous).
* The repro topology routes its ``rna`` descent on the target's side of the
  wide block instead of detouring to the far-left margin.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import route_edges
from nf_metro.layout.routing.common import clear_channel_of_section_edge
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, Section

REPO_ROOT = Path(__file__).resolve().parent.parent
REPRO = REPO_ROOT / "examples" / "topologies" / "clear_channel_target_aware_push.mmd"

# A wide blocking section the descent channel grazes; the channel midline at 212
# sits nearer this section's left edge (182px) than its right edge (218px).
_BLOCK = Section(
    id="block",
    name="Block",
    bbox_x=30.0,
    bbox_y=248.0,
    bbox_w=400.0,
    bbox_h=100.0,
)
_GRAZING_MID_X = 212.0
_HALF_WIDTH = 2.0


def _block_graph() -> MetroGraph:
    return MetroGraph(sections={"block": _BLOCK})


@pytest.mark.parametrize(
    "target_x, expect_right",
    [
        (480.0, True),  # target right of the block -> push right toward it
        (-50.0, False),  # target left of the block -> push left toward it
        (200.0, False),  # target inside the block span -> nearer-edge fallback
        (None, False),  # no target hint -> nearer-edge fallback (legacy)
    ],
)
def test_push_direction_follows_target(
    target_x: float | None, expect_right: bool
) -> None:
    """The grazing channel is pushed onto the target's side of the block, and
    onto the nearer edge only when the target's X is ambiguous."""
    adjusted = clear_channel_of_section_edge(
        _block_graph(),
        _GRAZING_MID_X,
        _HALF_WIDTH,
        120.0,
        448.0,
        [],
        target_x=target_x,
    )
    right_edge = _BLOCK.bbox_x + _BLOCK.bbox_w
    if expect_right:
        assert adjusted >= right_edge, (
            f"expected push right of block ({right_edge}), got {adjusted}"
        )
    else:
        assert adjusted <= _BLOCK.bbox_x, (
            f"expected push left of block ({_BLOCK.bbox_x}), got {adjusted}"
        )


def test_repro_rna_descent_routes_toward_target() -> None:
    """The hub's ``rna`` fan to a down-and-right target descends on the target's
    side of the wide ``block`` rather than detouring to the far-left margin."""
    graph = parse_metro_mermaid(REPRO.read_text())
    compute_layout(graph)
    routes = route_edges(graph)

    block = graph.sections["block"]
    block_right = block.bbox_x + block.bbox_w

    descents = [
        seg
        for r in routes
        if r.line_id == "rna"
        for seg in zip(r.points, r.points[1:])
        if abs(seg[0][1] - seg[1][1]) > abs(seg[0][0] - seg[1][0])
    ]
    assert descents, "expected a vertical descent segment on the rna line"
    # Every rna vertical run sits at or right of the block's right edge, on the
    # side its down-and-right target lies.
    for (x1, _), (x2, _) in descents:
        assert x1 >= block_right - 1.0 and x2 >= block_right - 1.0, (
            f"rna descent at x={x1} is on the far side of the block "
            f"(right edge {block_right}); expected target side"
        )
