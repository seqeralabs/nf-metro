"""Inter-row wrap gaps survive late section growth (#1307 / #1312).

A section stacked above another row can grow taller after the row gap is first
enforced (an off-track lift or fan settling inflates its bbox on a later pass).
The row-tighten step (:func:`_tighten_lower_rows_after_shrink`) must not then
pull the lower row up into the grown box: doing so reclaims the clearance the
placement pass reserved for the horizontal run an inter-row wrap bundle threads
through the gap, collapsing it below the section bottom.  The wrap then has no
clear lane and its trunk cuts through the upper section's interior -- the
Tier-A ``_guard_no_route_through_section`` defect reported as #1312 (and, on the
same packed riboseq layout, #1307's ``orf_calling`` feed).

The riboseq fixture is the reported repro (``preprocessing`` inflates ~178px
after its gap is reserved).  It carries an unrelated fan/label defect that
aborts a full render, so it lives under ``tests/fixtures`` rather than the
gallery and is checked at the routing layer here.  The remaining fixtures are
multi-row gallery topologies that already satisfy the invariant, so it
generalises beyond the one repro.
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
FIXTURES_DIR = ROOT / "tests" / "fixtures"

FIXTURES = [
    FIXTURES_DIR / "through_section" / "riboseq_packed_lr.mmd",
    TOPOLOGIES / "packed_cell_cellmate_bypass.mmd",
    TOPOLOGIES / "lr_bottom_exit_rl_top_entry_jog.mmd",
]
IDS = [p.stem for p in FIXTURES]


@pytest.mark.parametrize("path", FIXTURES, ids=IDS)
def test_no_route_through_section_across_inter_row_gap(path: Path) -> None:
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
