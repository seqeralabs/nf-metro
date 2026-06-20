"""Placement invariants for the bundled legend+logo block.

The ``%%metro legend:`` directive positions the joint block (the legend with
its embedded logo). Bare corner keywords stay content-anchored with the
historical overlap fallback; the explicit ``| canvas``, ``| dx,dy`` and
absolute ``x,y`` forms pin the block precisely and skip that fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render import svg as S
from nf_metro.themes import NFCORE_THEME

EXAMPLES = Path(__file__).parent.parent / "examples"

# rnaseq_sections has content filling its lower-right (so a content `br`
# overlaps a section and historically relocates); differentialabundance has a
# clear lower-right (so `br` lands without relocating). Exercising both proves
# the invariants generalise across the two overlap regimes.
FIXTURES = [
    EXAMPLES / "rnaseq_sections.mmd",
    EXAMPLES / "differentialabundance.mmd",
]


def _with_legend(path: Path, directive: str) -> str:
    """Return the fixture text with its legend directive replaced."""
    body = [
        ln
        for ln in path.read_text().splitlines()
        if not ln.strip().startswith("%%metro legend:")
    ]
    return f"%%metro legend: {directive}\n" + "\n".join(body) + "\n"


def _place(text: str):
    """Run the render-time legend placement and return its geometry.

    Returns (graph, (lx, ly, lw, lh, show), max_x, max_y, content_left).
    """
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    offsets = S.compute_station_offsets(graph)
    routes = S.route_edges_centred(graph, station_offsets=offsets)
    max_x, max_y = S._compute_canvas_bounds(graph, routes, False)
    pos = graph.legend_position
    show_logo = bool(graph.logo_path and Path(graph.logo_path).is_file())
    logo_in_legend = show_logo and pos != "none"
    logo_w, logo_h = (
        S.compute_logo_dimensions(graph.logo_path) if show_logo else (0.0, 0.0)
    )
    res = S._position_legend(
        graph,
        NFCORE_THEME,
        max_x,
        max_y,
        S.CANVAS_PADDING,
        logo_in_legend,
        logo_w,
        logo_h,
        pos,
        routes,
    )
    content_left = min(
        (s.bbox_x for s in graph.sections.values() if s.bbox_w > 0),
        default=S.CANVAS_PADDING,
    )
    return graph, res, max_x, max_y, content_left, routes


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.stem)
def test_legend_absolute_coordinates_land_exactly(fixture):
    """`legend: X,Y` pins the block top-left to exactly (X, Y)."""
    graph, (lx, ly, _lw, _lh, show), *_ = _place(_with_legend(fixture, "137,529"))
    assert graph.legend_at == (137.0, 529.0)
    assert show
    assert lx == pytest.approx(137.0)
    assert ly == pytest.approx(529.0)


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.stem)
def test_legend_offset_applies_and_skips_fallback(fixture):
    """`legend: br | dx,dy` lands at the br anchor plus the offset.

    The block must NOT relocate to bottom-left even when the br anchor would
    overlap a section (the historical fallback is disabled for explicit pins).
    """
    dx, dy = 40.0, -30.0
    _g, (lx, ly, lw, lh, show), max_x, max_y, _cl, _routes = _place(
        _with_legend(fixture, f"br | {dx:+},{dy:+}")
    )
    assert show
    inset = S.LEGEND_INSET
    assert lx == pytest.approx(max_x - lw - inset + dx)
    assert ly == pytest.approx(max_y - lh - inset + dy)


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.stem)
def test_legend_canvas_anchor_pins_bottom_right(fixture):
    """`legend: br | canvas` pins the block to the canvas bottom-right.

    Even when the corner overlaps a section (rnaseq_sections), the block stays
    pinned right rather than relocating to the bottom-left.
    """
    _g, (lx, ly, lw, lh, show), max_x, max_y, content_left, _routes = _place(
        _with_legend(fixture, "br | canvas")
    )
    assert show
    inset = S.LEGEND_INSET
    assert lx == pytest.approx(max_x - lw - inset)
    assert ly == pytest.approx(max_y - lh - inset)
    # Pinned to the right, not dumped at the content-left margin.
    assert lx > content_left + lw


def test_bare_corner_keeps_overlap_fallback():
    """Backward-compat lock: a bare `legend: br` that overlaps still relocates.

    rnaseq_sections' lower-right is occupied, so the casual corner keyword must
    keep falling back to the bottom-left below content (unchanged behaviour).
    """
    fixture = EXAMPLES / "rnaseq_sections.mmd"
    _g, (lx, ly, _lw, lh, show), _max_x, max_y, content_left, _routes = _place(
        _with_legend(fixture, "br")
    )
    assert show
    assert lx == pytest.approx(content_left)
    assert ly > max_y - lh


def test_keyword_legend_never_overlaps_routes():
    """A bare corner keyword relocates so the block clears all routes.

    On legend_logo_placement the QC line runs across the lower-left where a
    bare `bl` would sit, so auto-placement must move the block off the route.
    """
    fixture = EXAMPLES / "legend_logo_placement.mmd"
    _g, (lx, ly, lw, lh, show), _mx, _my, _cl, routes = _place(
        _with_legend(fixture, "bl")
    )
    assert show
    assert not S._legend_overlaps_routes(
        lx, ly, lw, lh, routes, S.LEGEND_ROUTE_CLEARANCE
    )


def test_explicit_pin_warns_on_route_overlap():
    """An explicit pin is honoured as placed but warns when it hits a route."""
    fixture = EXAMPLES / "legend_logo_placement.mmd"
    with pytest.warns(UserWarning, match="overlaps a section or route"):
        _place(_with_legend(fixture, "bl | canvas"))
