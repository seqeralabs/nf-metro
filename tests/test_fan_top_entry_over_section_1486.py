"""Fan top-entry trunk clears a tall row-above section (#1486, horizontal).

A fan-out junction with mixed-handler branches shares one inter-row traverse
band (:class:`FanCorridor`), measured in the junction's own grid column so a
tall row-span section in a *different* column does not collapse it.  When one
branch is a TOP-entry drop into a section stacked below a *different* column,
its horizontal trunk leg runs at that band Y across the crossed column -- and
lands inside a taller row-above section there, skimming its interior.  This is
the Tier-A ``_guard_no_route_through_section`` defect (issue #1486, horizontal
channel variant).

The first fixture is the minimal repro; the rest are multi-row gallery
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
    TOPOLOGIES / "fan_top_entry_over_tall_section.mmd",
    TOPOLOGIES / "packed_cell_cellmate_bypass.mmd",
    TOPOLOGIES / "serpentine_grid_tall_bundle.mmd",
]
IDS = [p.stem for p in FIXTURES]


@pytest.mark.parametrize("path", FIXTURES, ids=IDS)
def test_fan_top_entry_trunk_clears_row_above_section(path: Path) -> None:
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
