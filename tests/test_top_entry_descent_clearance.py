"""A TOP-entry descent trunk must not skim an intervening section's interior.

When an inter-section feed drops into a TOP entry port through a squeezed
inter-row gap, its horizontal trunk leg can be seated inside a tall upstream
section that protrudes into the gap the run doubles back across.  A run there
reads as passing through that section (issue #1312).  The handler drops the
trunk below the crossed section's bottom edge; these tests lock that.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases._common import routes_through_unrelated_sections
from nf_metro.layout.routing.inter_section_handlers import (
    _deepest_section_bottom_crossed_by_run,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
PACKED_GRID = EXAMPLES / "topologies" / "packed_multiline_serpentine_grid.mmd"


def _section(sid: str, x: float, y: float, w: float, h: float) -> SimpleNamespace:
    return SimpleNamespace(id=sid, bbox_x=x, bbox_y=y, bbox_w=w, bbox_h=h)


def test_helper_reports_deepest_penetrated_section() -> None:
    graph = SimpleNamespace(
        sections={
            "src": _section("src", 0, 0, 100, 100),
            "tall": _section("tall", 200, 0, 100, 300),
            "short": _section("short", 400, 0, 100, 120),
            "below": _section("below", 200, 400, 100, 100),
        }
    )
    # A run at y=150 spanning x=50..450 penetrates 'tall' (0..300) but not
    # 'short' (0..120) or 'below' (400..500); its bottom is the deepest.
    assert (
        _deepest_section_bottom_crossed_by_run(graph, 50, 450, 150, exclude={"src"})
        == 300
    )
    # A run clear of every box interior returns None.
    assert (
        _deepest_section_bottom_crossed_by_run(graph, 50, 450, 350, exclude={"src"})
        is None
    )
    # Excluded sections are never counted, even when penetrated.
    assert (
        _deepest_section_bottom_crossed_by_run(
            graph, 50, 250, 50, exclude={"src", "tall"}
        )
        is None
    )


def test_top_entry_descent_clears_upstream_section() -> None:
    graph = parse_metro_mermaid(PACKED_GRID.read_text())
    compute_layout(graph, validate=False)
    offenders = routes_through_unrelated_sections(graph)
    top_entry_offenders = [
        (rp, sid) for rp, sid in offenders if "__entry_top" in rp.edge.target
    ]
    assert not top_entry_offenders, "; ".join(
        f"{rp.line_id} {rp.edge.source}->{rp.edge.target} through {sid!r}"
        for rp, sid in top_entry_offenders
    )
