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
from nf_metro.layout.phases.spacing import _placed_name_label_station_ids
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
    EXAMPLES / "topologies/file_node_with_outgoing_edge.mmd",
]

# Every gallery-facing map that declares file icons, so a new one is enrolled
# by being added rather than by being remembered here.
_GALLERY_FILE_ICON_EXAMPLES = sorted(
    p for p in EXAMPLES.glob("*.mmd") if "%%metro file:" in p.read_text()
)


@pytest.mark.parametrize("fixture", _FILE_ICON_FIXTURES, ids=lambda p: p.name)
def test_file_icon_stations_have_no_name_label(fixture: Path) -> None:
    """A file-icon station must not also receive a node-name label."""
    graph = parse_metro_mermaid(fixture.read_text())
    terminus_ids = {s.id for s in graph.stations.values() if s.is_terminus}
    assert terminus_ids, f"{fixture.name} has no file-icon stations to exercise"

    compute_layout(graph)
    offenders = sorted(terminus_ids & _placed_name_label_station_ids(graph))
    assert not offenders, (
        f"{fixture.name}: file-icon stations also got a name label "
        f"(overlaps caption/tracks): {offenders}"
    )


@pytest.mark.parametrize("fixture", _GALLERY_FILE_ICON_EXAMPLES, ids=lambda p: p.name)
def test_gallery_file_icon_stations_are_blank_termini(fixture: Path) -> None:
    """A gallery map's file-icon station carries no label of its own.

    Label *suppression* keeps a non-blank label off the canvas, but the marker
    is chosen separately: ``svg.py`` draws the unrounded nub only for a station
    ``is_blank_terminus`` reports on, and a rounded pill for everything else,
    while the icons go on regardless.  So a file station that picks up a label -
    including the implicit one a bare edge reference gives a node named only in
    a ``%%metro file:`` directive, which is its own id - renders as a pill *and*
    an icon, two markers for one station.  ``node[ ]`` is the idiom that avoids
    it.

    Pinned on the maps the docs render rather than the whole corpus: a topology
    stress fixture exercises geometry, where a doubled marker costs nothing.
    """
    graph = parse_metro_mermaid(fixture.read_text())
    termini = [s for s in graph.stations.values() if s.is_terminus]
    assert termini, f"{fixture.name} has no file-icon stations to exercise"
    offenders = sorted((s.id, s.label) for s in termini if not s.is_blank_terminus)
    assert not offenders, (
        f"{fixture.name}: file-icon stations carry a label, so each draws a "
        f"station pill beside its icon; declare them as `id[ ]`: {offenders}"
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
