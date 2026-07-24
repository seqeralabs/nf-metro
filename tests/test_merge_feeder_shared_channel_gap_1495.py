"""Fused merge feeders keep a truthful gap-slot declaration (#1495).

When two feeders reach one merge junction and a packed cell-mate pushes their
shared descent into an inter-column gap, :func:`_coincide_same_line_tracks`
fuses the branch feeder's opening descent onto the trunk feeder's descent
column.  The relocation crossed a gap boundary but left the branch feeder's
:class:`GapSlot` declared at its pre-fusion column, so
:func:`check_gap_channels_materialized` saw the relocated leg in a gap with no
matching slot and aborted the render.

The repro is a valid map: two fan sources (``src_fanA``/``src_fanB``) each feed
both ``target`` and ``side_a``; a packed ``left_mate`` cell-mate reshapes the
columns so the shared merge channel lands inside a gap.  The remaining fixtures
are gallery topologies with their own merge-feeder descents, so the invariant
generalises beyond the repro.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.layout.routing.invariants import check_gap_channels_materialized
from nf_metro.parser.mermaid import parse_metro_mermaid

ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = ROOT / "examples" / "topologies"

FIXTURES = [
    TOPOLOGIES / "merge_feeder_shared_channel_gap.mmd",
    TOPOLOGIES / "wide_fan_in.mmd",
    TOPOLOGIES / "merge_trunk_out_of_range_section.mmd",
]
IDS = [p.stem for p in FIXTURES]


@pytest.mark.parametrize("path", FIXTURES, ids=IDS)
def test_no_undeclared_gap_channel(path: Path) -> None:
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    violations = check_gap_channels_materialized(graph, routes)
    assert not violations, "\n".join(v.message() for v in violations)
