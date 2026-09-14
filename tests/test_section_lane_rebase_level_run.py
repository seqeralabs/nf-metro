"""A section that compacts its bundle keeps its feeder's run level.

A section carrying only part of the global line order compacts its lines onto
consecutive lanes, so its trunk draws tight instead of reserving a slot for
every line it never sees.  Where that compacted block sits on the trunk is
free, and the choice is geometric: a line handed over from a junction on the
section's own row draws a level run only if the section carries it on the lane
the junction hands it over on.  Anchoring the block at lane 0 regardless tips
that run into a shallow slant -- too gentle to read as a turn, too steep to
read as level, and carrying a chevron pointing off-axis.

Both halves are load-bearing, so they are pinned separately.  Keeping runs
level by declining to compact widens the section's own bundle instead, which
leaves the routes that join it hanging in open space.

``junction_entry_lane_rebase`` is the minimal fixture: a four-line order split
so the transcript-discovery section carries priorities 1 and 3 while its
row-mate carries the missing 2.  The remaining fixtures are shipped maps whose
junction hand-overs are already level, so the invariant is exercised beyond the
one topology that motivated it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import graph_offset_step
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing import offsets as offsets_module
from nf_metro.layout.routing.common import apply_route_offsets
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph

ROOT = Path(__file__).resolve().parent.parent

FIXTURES = [
    "examples/topologies/junction_entry_lane_rebase.mmd",
    "examples/topologies/junction_entry_reversed_fold.mmd",
    "examples/topologies/seed72_cross_family_fan.mmd",
    "examples/rnaseq_auto.mmd",
    "examples/rnaseq_sections.mmd",
    "examples/differentialabundance.mmd",
    "examples/longread_variant_calling.mmd",
    "examples/guide/03b_fan_in_merge.mmd",
]
FIXTURE_IDS = [Path(f).stem for f in FIXTURES]

REBASE_FIXTURE = "examples/topologies/junction_entry_lane_rebase.mmd"

_SAME_LANE_TOLERANCE = 0.5


def _settled(fixture: str) -> tuple[MetroGraph, dict[tuple[str, str], float]]:
    graph = parse_metro_mermaid((ROOT / fixture).read_text(), max_station_columns=15)
    compute_layout(graph)
    return graph, dict(compute_station_offsets(graph))


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_row_mate_junction_hands_over_on_one_lane(fixture: str) -> None:
    """A junction and the entry port it feeds on one row share the line's lane.

    Two endpoints on the same row are drawn at ``row + lane``, so unequal lanes
    are the slant: the connector has to climb the difference over the width of
    the inter-section gap.
    """
    graph, offsets = _settled(fixture)
    slanted = []
    for edge in graph.edges:
        if edge.source not in graph.junctions:
            continue
        port = graph.ports.get(edge.target)
        if port is None or not port.is_entry:
            continue
        source = graph.stations[edge.source]
        target = graph.stations[edge.target]
        if abs(source.y - target.y) > _SAME_LANE_TOLERANCE:
            continue
        upstream = offsets.get((edge.source, edge.line_id), 0.0)
        downstream = offsets.get((edge.target, edge.line_id), 0.0)
        if abs(upstream - downstream) > _SAME_LANE_TOLERANCE:
            slanted.append(
                f"{edge.source}->{edge.target} ({edge.line_id}): "
                f"lane {upstream} hands over to lane {downstream}"
            )
    assert not slanted, "\n".join(slanted)


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_section_bundles_occupy_consecutive_lanes(fixture: str) -> None:
    """No section reserves a lane for a line it does not carry.

    An unclaimed lane inside a section's own bundle spreads its stations wider
    than the routes that join them expect, which strands those routes off the
    markers they should meet.
    """
    graph, offsets = _settled(fixture)
    step = graph_offset_step(graph)
    lanes_by_section: dict[str, set[float]] = {}
    for (station_id, _line_id), offset in offsets.items():
        station = graph.stations[station_id]
        if station.is_port or station.section_id is None:
            continue
        lanes_by_section.setdefault(station.section_id, set()).add(round(offset, 1))
    gapped = [
        f"{section_id}: lanes {sorted(lanes)}"
        for section_id, lanes in lanes_by_section.items()
        if any(
            second - first > step + _SAME_LANE_TOLERANCE
            for first, second in zip(sorted(lanes), sorted(lanes)[1:])
        )
    ]
    assert not gapped, "\n".join(gapped)


def test_gapped_lane_block_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bundle that leaves a lane unclaimed meets the closing guard, not the
    canvas."""
    monkeypatch.setattr(
        offsets_module,
        "_level_run_lane_block",
        lambda ctx, sec_id, ordered, section_local: {
            lid: ctx.line_priority.get(lid, 0) for lid in ordered
        },
    )
    graph = parse_metro_mermaid(
        (ROOT / REBASE_FIXTURE).read_text(), max_station_columns=15
    )
    with pytest.raises(offsets_module.OffsetAnchorError, match="off the trunk"):
        compute_layout(graph)
        compute_station_offsets(graph)


def test_compacted_bundle_draws_its_feeder_run_flat() -> None:
    """The re-based section's inbound run is one flat segment, not a slope."""
    graph, offsets = _settled(REBASE_FIXTURE)
    entry_ports = graph.sections["novel_transcripts"].entry_ports
    inbound = [
        apply_route_offsets(route, offsets)
        for route in route_edges(graph, station_offsets=offsets)
        if route.line_id == "rnaseq" and route.edge.target in entry_ports
    ]
    assert len(inbound) == 1
    levels = {round(y, 3) for _x, y in inbound[0]}
    assert len(levels) == 1, f"inbound run is not level: {inbound[0]}"
