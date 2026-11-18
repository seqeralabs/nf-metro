"""A dead-end branch never sits on a row an exit run holds.

In an LR section, ``summary`` and ``collapse`` both feed the right exit port,
so each rides its own row flat to the section edge.  ``collapse`` also fans
out into two dead-end branches.  Every station carries the one line, so the
crossing guard's line-membership test cannot see a branch seated on an exit
row; the drawn geometry has to keep each marker clear of the other edges.
"""

from __future__ import annotations

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.geometry import segment_intersects_bbox
from nf_metro.layout.phases._common import _station_marker_bbox
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import apply_route_offsets
from nf_metro.parser.mermaid import parse_metro_mermaid

SIBLING_BRANCHES_MMD = """\
%%metro file: b_out | TSV
%%metro file: c_out | TSV
%%metro line: main | Main | #0570b0
%%metro grid: s1 | 0,0
%%metro grid: s2 | 0,1

graph LR
    subgraph s1 [Section one]
        %%metro exit: right | main
        align[align]
        summary[summary]
        collapse[collapse]
        b[branch B]
        b_out[ ]
        c[branch C]
        c_out[ ]
        align -->|main| summary
        align -->|main| collapse
        collapse -->|main| b
        b -->|main| b_out
        collapse -->|main| c
        c -->|main| c_out
    end

    subgraph s2 [Section two]
        %%metro entry: left | main
        qc[qc]
        sort[sort]
    end

    summary -->|main| qc
    collapse -->|main| sort
"""


def _marker_crossings(mmd: str) -> list[tuple[str, str, str]]:
    graph = parse_metro_mermaid(mmd)
    compute_layout(graph, validate=True)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    crossings = []
    for r in routes:
        src, tgt = r.edge.source, r.edge.target
        pts = apply_route_offsets(r, offsets)
        for sid in graph.stations:
            if sid in (src, tgt):
                continue
            bbox = _station_marker_bbox(graph, sid, offsets=offsets)
            if bbox is None:
                continue
            if any(
                segment_intersects_bbox(*p1, *p2, bbox) for p1, p2 in zip(pts, pts[1:])
            ):
                crossings.append((src, tgt, sid))
    return crossings


def test_exit_runs_clear_sibling_branch_markers() -> None:
    """No drawn segment passes through the marker of a station it doesn't end at."""
    assert _marker_crossings(SIBLING_BRANCHES_MMD) == []
