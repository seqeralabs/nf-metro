"""Packed-cell left-entry descent lands in a gap lane, not under a neighbour
(#1486, vertical descent variant).

When a section's cell-mate in the row above is wider, the section's left edge
(and its LEFT entry port) can sit under that neighbour's box.  A fan-out
junction feeding that LEFT entry ran its opening descent in the crossed
column's outer gap instead of the inter-cell gap beside the source, so the
source-Y traverse plus descent threaded the cell-neighbour's interior -- the
Tier-A ``_guard_no_route_through_section`` defect.

The first fixture is the minimal repro; the rest are packed-cell gallery
topologies that already satisfy the invariant, so it generalises beyond the one
repro.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases.guards import routes_through_unrelated_sections
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.parser.mermaid import parse_metro_mermaid

ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = ROOT / "examples" / "topologies"

FIXTURES = [
    TOPOLOGIES / "packed_cell_left_entry_under_neighbour.mmd",
    TOPOLOGIES / "packed_cell_cellmate_bypass.mmd",
    TOPOLOGIES / "packed_cell_consumer_drop_in.mmd",
]
IDS = [p.stem for p in FIXTURES]


@pytest.mark.parametrize("path", FIXTURES, ids=IDS)
def test_packed_cell_descent_clears_cell_neighbour(path: Path) -> None:
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    offenders = routes_through_unrelated_sections(graph, routes=routes, offsets=offsets)
    assert not offenders, "\n".join(
        f"line {rp.line_id!r} {rp.edge.source!r}->{rp.edge.target!r} "
        f"passes through section {sid!r}"
        for rp, sid in offenders
    )
