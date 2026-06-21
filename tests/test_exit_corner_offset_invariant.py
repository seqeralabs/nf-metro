"""Routing invariant: a flow-aligned exit keeps its per-line offset on the
onward run instead of stepping back to the bare port-marker row at the corner.

When a section's exit port sits on a multi-line bundle, a passing line runs
through the section on its own per-line offset track. If that line then bypasses
a higher row to climb to a far target, the row-level traverse must stay on the
line's offset track so the line leaves the exit port straight. Dropping the
offset at the exit corner (running the traverse on the bare port-marker row)
produces a gratuitous ~one-offset-step vertical jog right after the port.

Regression lock for #939.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import (
    apply_route_offsets,
    compute_station_offsets,
    route_edges,
)
from nf_metro.layout.routing.invariants import (
    check_bottom_row_climb_run_on_source_track,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

TOPOLOGIES_DIR = Path(__file__).parent.parent / "examples" / "topologies"

TOL = 1.0


def _exit_corner_step(mmd: str) -> tuple[str, float, float] | None:
    """First exit-port route whose offset-applied traverse leaves the line's
    track, returned as ``(edge, traverse_y, exit_y)``; ``None`` if every exit
    leaves straight.

    The long horizontal traverse must sit at the route's exit-port Y (the line's
    offset track). A traverse at a different Y is the off-track corner jog.
    """
    graph = parse_metro_mermaid(mmd)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    for r in routes:
        if not r.is_inter_section:
            continue
        port = graph.ports.get(r.edge.source)
        if port is None or port.is_entry:
            continue
        pts = apply_route_offsets(r, offsets)
        if len(pts) < 2:
            continue
        exit_y = pts[0][1]
        best_dx = 0.0
        traverse_y = exit_y
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if abs(y1 - y0) <= TOL and abs(x1 - x0) > best_dx:
                best_dx = abs(x1 - x0)
                traverse_y = y0
        if abs(traverse_y - exit_y) > TOL:
            return (f"{r.edge.source}->{r.edge.target}", traverse_y, exit_y)
    return None


def _generated_offset_exit_climb(span: int) -> str:
    """A bottommost-row ``mid`` fed by two lines from ``src`` (so the passing
    line ``l2`` runs through on an offset track), then bypassing ``span`` higher
    blocker sections to climb to ``dst``."""
    blockers = [f"blk{i}" for i in range(span)]
    grids = "%%metro grid: src | 0,0\n%%metro grid: mid | 1,1\n"
    grids += "".join(f"%%metro grid: {b} | {i + 2},0\n" for i, b in enumerate(blockers))
    grids += f"%%metro grid: dst | {span + 2},0"
    blocks = "".join(
        f"    subgraph {b} [{b.title()}]\n        {b}_a[{b}]\n    end\n"
        for b in blockers
    )
    chain = f"    a1 -->|l1| {blockers[0]}_a\n"
    chain += "".join(
        f"    {blockers[i]}_a -->|l1| {blockers[i + 1]}_a\n" for i in range(span - 1)
    )
    chain += f"    {blockers[-1]}_a -->|l1| d1\n"
    return (
        "%%metro title: gen\n%%metro style: dark\n"
        "%%metro line: l1 | L1 | #f5c542\n"
        "%%metro line: l2 | L2 | #e63946\n"
        f"{grids}\n\ngraph LR\n"
        "    subgraph src [Src]\n        a1[A1]\n    end\n"
        "    subgraph mid [Mid]\n        m1[Mid]\n    end\n"
        f"{blocks}"
        "    subgraph dst [Dst]\n        d1[Dst]\n    end\n"
        "    a1 -->|l1| m1\n    a1 -->|l2| m1\n    m1 -->|l2| d1\n"
        f"{chain}"
    )


def test_static_fixture_exit_leaves_on_offset_track():
    mmd = (TOPOLOGIES_DIR / "exit_corner_offset_dogleg.mmd").read_text()
    step = _exit_corner_step(mmd)
    assert step is None, (
        f"exit route {step[0]} runs its traverse at y={step[1]:.0f}, off the "
        f"exit-port track y={step[2]:.0f} (off-track jog at the exit corner)"
    )


@pytest.mark.parametrize("span", [1, 2, 3], ids=["span1", "span2", "span3"])
def test_generated_exit_leaves_on_offset_track(span):
    step = _exit_corner_step(_generated_offset_exit_climb(span))
    assert step is None, (
        f"exit route {step[0]} runs its traverse at y={step[1]:.0f}, off the "
        f"exit-port track y={step[2]:.0f} (off-track jog at the exit corner)"
    )


def test_runtime_guard_clean_on_fixture():
    mmd = (TOPOLOGIES_DIR / "exit_corner_offset_dogleg.mmd").read_text()
    graph = parse_metro_mermaid(mmd)
    compute_layout(graph)
    routes = route_edges(graph, station_offsets=compute_station_offsets(graph))
    assert check_bottom_row_climb_run_on_source_track(graph, routes) == []
