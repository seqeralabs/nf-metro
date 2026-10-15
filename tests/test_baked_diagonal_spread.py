"""Distinct edges riding one diagonal in a vertical section spread as one bundle.

A vertical-flow route bakes its line offset on X, so on a diagonal two lines
stand only ``OFFSET_STEP * sin(theta)`` apart and the spread pass restores the
full gap.  A horizontal section's hub pass spreads every diagonal leaving a fork
station together; a vertical section's lines on distinct edges that leave a
station on one diagonal must spread together too, or they run a fraction of a
pixel apart and the render aborts on the collinear-overlay check.
"""

from __future__ import annotations

import math

import pytest

from nf_metro.api import render_string
from nf_metro.layout.constants import graph_offset_step
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import apply_route_offsets
from nf_metro.parser.mermaid import parse_metro_mermaid

_CODIAGONAL = """\
%%metro line: m0 | Line 0 | #e6194b
%%metro line: m1 | Line 1 | #3cb44b
%%metro line: m2 | Line 2 | #4363d8
%%metro line: m3 | Line 3 | #f58231

graph LR
    subgraph z0 [Zone 0]
        q0x0[BWA]
    end
    subgraph z1 [Zone 1]
        %%metro direction: {direction}
        q1x0[DESeq2]
        q1x1[BWA]
        q1x2[MultiQC]
        q1x0 -->|m2,m3| q1x1
        q1x1 -->|m1,m2| q1x2
        q1x0 -->|m3| q1x2
    end
    q0x0 -->|m0,m1,m2| q1x0
    q0x0 -->|m0| q1x1
"""


def _diagonal(points: list[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    """The first segment of *points* that changes both coordinates."""
    for a, b in zip(points, points[1:]):
        if abs(a[0] - b[0]) > 0.5 and abs(a[1] - b[1]) > 0.5:
            return a, b
    raise AssertionError(f"no diagonal in {points}")


@pytest.mark.parametrize("direction", ["BT", "TB"])
def test_codiagonal_distinct_edges_keep_the_bundle_gap(direction: str) -> None:
    text = _CODIAGONAL.format(direction=direction)
    render_string(text)

    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    drawn = {
        (r.edge.target, r.line_id): apply_route_offsets(r, offsets)
        for r in route_edges(graph, station_offsets=offsets)
        if r.edge.source == "q1x0"
    }
    (ax, ay), (bx, by) = _diagonal(drawn["q1x1", "m2"])
    (px, py), _ = _diagonal(drawn["q1x2", "m3"])
    gap = abs((bx - ax) * (ay - py) - (ax - px) * (by - ay)) / math.hypot(
        bx - ax, by - ay
    )
    assert gap >= graph_offset_step(graph) - 0.01, (
        f"m2 and m3 leave q1x0 on one diagonal only {gap:.2f}px apart"
    )
