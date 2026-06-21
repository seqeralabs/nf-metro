"""Routing invariant: a bottommost-row source climbing to a higher-row target
over a clear corridor runs at its own row level, not below it.

When the source section sits in the bottommost grid row, its target is in a
higher row, and no same-row section occupies the columns the rightward run
crosses, the sections that classified the edge as a bypass are all in higher
rows (above a run at the source's Y). Diving below the source row toward the
canvas floor and climbing back up is then a gratuitous downward dogleg.

The invariant is gated on the corridor being clear: a climb whose corridor is
blocked by a same-row section genuinely needs to clear it and may dip.

Regression lock for #878 defect 2 (the `other` line from `ont-spectre
CNVCaller` dropping below the CNVs section before running right to Reports).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.context import _resolve_section_row
from nf_metro.layout.routing.inter_section_handlers import (
    _bottom_row_climb_corridor_clear,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

TOPOLOGIES_DIR = Path(__file__).parent.parent / "examples" / "topologies"


def _clear_corridor_climb_dive(mmd: str) -> tuple[str, float, float] | None:
    """First inter-section route whose source is in the bottommost row, climbs
    to a higher row over a clear corridor, yet dips below the source section's
    bbox bottom; ``None`` if every such route stays at its row level."""
    graph = parse_metro_mermaid(mmd)
    compute_layout(graph)
    routes = route_edges(graph, station_offsets=compute_station_offsets(graph))

    for r in routes:
        if not r.is_inter_section:
            continue
        src = graph.stations.get(r.edge.source)
        tgt = graph.stations.get(r.edge.target)
        if src is None or tgt is None:
            continue
        src_sec = graph.sections.get(src.section_id)
        src_row = _resolve_section_row(graph, src)
        tgt_row = _resolve_section_row(graph, tgt)
        if src_sec is None or src_row is None or tgt_row is None:
            continue
        if not _bottom_row_climb_corridor_clear(
            graph, src_row, tgt_row, src_sec.grid_col, _resolve_col(graph, tgt)
        ):
            continue
        src_bottom = src_sec.bbox_y + src_sec.bbox_h
        max_y = max(y for _, y in r.points)
        if max_y > src_bottom + 1.0:
            return (f"{r.edge.source}->{r.edge.target}", max_y, src_bottom)
    return None


def _resolve_col(graph, station) -> int:
    sec = graph.sections.get(station.section_id)
    return sec.grid_col if sec is not None else -1


def _generated_bottom_row_pipeline(span: int) -> str:
    """Top row of ``span`` sections (col 0..span-1); a single bottommost-row
    section at col 1 whose side line climbs to the rightmost top section over a
    clear corridor (no same-row section in the spanned columns)."""
    tops = [f"top{i}" for i in range(span)]
    grids = "\n".join(f"%%metro grid: {s} | {i},0" for i, s in enumerate(tops))
    grids += "\n%%metro grid: low | 1,1"
    top_blocks = ""
    for s in tops:
        top_blocks += f"    subgraph {s} [{s.title()}]\n        {s}_a[{s}]\n    end\n"
    chain = "\n".join(
        f"    {tops[i]}_a -->|main| {tops[i + 1]}_a" for i in range(span - 1)
    )
    return (
        "%%metro line: main | Main | #2db572\n"
        "%%metro line: side | Side | #ff8c00\n"
        f"{grids}\n\ngraph LR\n{top_blocks}"
        "    subgraph low [Low]\n        x[X]\n    end\n"
        f"{chain}\n"
        "    top0_a -->|side| x\n"
        f"    x -->|side| {tops[-1]}_a\n"
    )


def test_static_fixture_clear_corridor_climb_stays_at_row_level():
    mmd = (TOPOLOGIES_DIR / "bottom_row_climb_clear_corridor.mmd").read_text()
    dive = _clear_corridor_climb_dive(mmd)
    assert dive is None, (
        f"route {dive[0]} dips to y={dive[1]:.0f}, below source section "
        f"bottom={dive[2]:.0f} (gratuitous dogleg over a clear corridor)"
    )


@pytest.mark.parametrize("span", [4, 5], ids=["span4", "span5"])
def test_generated_clear_corridor_climb_stays_at_row_level(span):
    dive = _clear_corridor_climb_dive(_generated_bottom_row_pipeline(span))
    assert dive is None, (
        f"route {dive[0]} dips to y={dive[1]:.0f}, below source section "
        f"bottom={dive[2]:.0f} (gratuitous dogleg over a clear corridor)"
    )
