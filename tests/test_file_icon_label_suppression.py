"""Invariant: a file-icon station owns its own labelling (issue #524).

`%%metro file:` stations render their caption(s) beneath the icon
(``terminus_names``).  Per #93, the file directive should *entirely*
own the station's labelling, so such a station must never also receive
a separate node-name label from ``place_labels`` - that second label
overprints the caption and the converging tracks.

The clean corpus idiom is a blank node label (``node[ ]``), which
side-steps the candidate filter.  These tests pin the stronger
invariant: a non-blank label on a file station is suppressed too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.labels import place_labels
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, Station

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

# Fixtures containing `%%metro file:` stations.  file_icon_fanin gives the
# offending case a non-blank node label; the two examples use the clean
# blank-label idiom and guard against a regression in the common path.
_FILE_ICON_FIXTURES = [
    FIXTURES / "file_icon_fanin.mmd",
    EXAMPLES / "differentialabundance_default.mmd",
    EXAMPLES / "genomeassembly_staggered.mmd",
]


def _placed_label_ids(graph: MetroGraph) -> set[str]:
    """Return station ids that ``place_labels`` emits a name label for."""
    compute_layout(graph)
    station_offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=station_offsets)
    placements = place_labels(graph, station_offsets=station_offsets, routes=routes)
    return {p.station_id for p in placements if p.station_id}


@pytest.mark.parametrize(
    "fixture", _FILE_ICON_FIXTURES, ids=lambda p: p.name
)
def test_file_icon_stations_have_no_name_label(fixture: Path) -> None:
    """A file-icon station must not also receive a node-name label."""
    graph = parse_metro_mermaid(fixture.read_text())
    terminus_ids = {s.id for s in graph.stations.values() if s.is_terminus}
    assert terminus_ids, f"{fixture.name} has no file-icon stations to exercise"

    labelled = _placed_label_ids(graph)
    offenders = sorted(terminus_ids & labelled)
    assert not offenders, (
        f"{fixture.name}: file-icon stations also got a name label "
        f"(overlaps caption/tracks): {offenders}"
    )


def test_terminus_label_suppressed_even_with_nonblank_label() -> None:
    """Unit: a terminus station with a non-blank label is filtered out."""
    graph = MetroGraph()
    graph.stations["f"] = Station(
        id="f",
        label="FASTA",
        x=100.0,
        y=100.0,
        terminus_labels=["FASTA"],
        terminus_names=["Reference"],
    )
    graph.stations["s"] = Station(id="s", label="Align", x=200.0, y=100.0)

    placements = place_labels(graph)
    placed = {p.station_id for p in placements if p.station_id}
    assert "f" not in placed
    assert "s" in placed
