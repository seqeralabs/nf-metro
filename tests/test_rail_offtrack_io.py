"""Rail-mode off-track input/output render with the corpus terminus idiom (issue #744).

In rail mode an off-track ``%%metro file:`` node must carry a buffer-stop nub at
the rail-side end of its vertical stub (like the on-rail CRAM/VCF termini), the
nub must clear the under-icon caption, and a *plain* (non-file) off-track node
must render a visible station marker rather than a bare line end.  An off-track
input's label must also not collide with its drop or a neighbouring station's
label.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.labels import find_label_overlaps, place_labels
from nf_metro.layout.routing import compute_station_offsets
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.svg import render_svg
from nf_metro.themes.nfcore import NFCORE_THEME

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
TOPOLOGIES = EXAMPLES / "topologies"
NS = "{http://www.w3.org/2000/svg}"


def _find_fixture(stem: str) -> Path:
    for d in (EXAMPLES, TOPOLOGIES):
        p = d / f"{stem}.mmd"
        if p.exists():
            return p
    raise FileNotFoundError(stem)


def _laid_out(stem: str):
    graph = parse_metro_mermaid(_find_fixture(stem).read_text())
    compute_layout(graph)
    return graph


def _station_rects(svg: str, station_id: str) -> list[ET.Element]:
    """Station-glyph ``<rect>`` elements (nub/pill) for a station id.

    Excludes the file-icon body, which is not tagged as an
    ``nf-metro-station`` glyph.
    """
    root = ET.fromstring(svg)
    return [
        el
        for el in root.iter(f"{NS}rect")
        if el.get("data-station-id") == station_id
        and "nf-metro-station" in (el.get("class") or "")
    ]


# Fixture -> off-track file termini that must each carry a buffer-stop nub.
_FILE_TERMINUS_FIXTURES = {
    "rail_offtrack_io": ["aux_in", "side_out"],
    "rail_offtrack_fan": ["meta_csv", "report_csv"],
    "rail_mode": ["samples_csv"],
}


@pytest.mark.parametrize(
    ("stem", "term_id"),
    [(stem, t) for stem, terms in _FILE_TERMINUS_FIXTURES.items() for t in terms],
)
def test_offtrack_file_terminus_has_buffer_stop_nub(stem: str, term_id: str) -> None:
    graph = _laid_out(stem)
    station = graph.stations[term_id]
    assert station.off_track and station.is_blank_terminus
    svg = render_svg(graph, NFCORE_THEME)
    nubs = _station_rects(svg, term_id)
    assert nubs, f"{stem}: off-track file terminus {term_id!r} drew no buffer-stop nub"
    # The nub seats at the rail-side stub end (the station coordinate), not up at
    # the icon.
    r = NFCORE_THEME.station_radius
    assert any(
        abs(float(n.get("y")) + float(n.get("height")) / 2 - station.y) <= r + 1.0
        for n in nubs
    ), f"{stem}: {term_id!r} nub is not seated at the stub end (y={station.y})"


def test_offtrack_file_terminus_nub_clears_caption() -> None:
    """The buffer-stop nub must not sit on the under-icon caption."""
    stem = "rail_offtrack_io"
    graph = _laid_out(stem)
    svg = render_svg(graph, NFCORE_THEME)
    root = ET.fromstring(svg)
    captions = {el.text: el for el in root.iter(f"{NS}text") if el.text in ("Targets",)}
    assert "Targets" in captions, "expected the 'Targets' caption to render"
    cap = captions["Targets"]
    cap_baseline = float(cap.get("y"))
    nubs = _station_rects(svg, "aux_in")
    nub_top = min(float(n.get("y")) for n in nubs)
    assert nub_top > cap_baseline, (
        f"buffer-stop nub top ({nub_top}) overlaps the caption baseline "
        f"({cap_baseline})"
    )


@pytest.mark.parametrize("node_id", ["aux", "qc"])
def test_plain_offtrack_node_renders_marker(node_id: str) -> None:
    """A plain (non-file) off-track input/output must draw a station marker."""
    graph = _laid_out("rail_offtrack_plain_io")
    station = graph.stations[node_id]
    assert station.off_track and not station.is_blank_terminus
    svg = render_svg(graph, NFCORE_THEME)
    markers = _station_rects(svg, node_id)
    assert markers, (
        f"plain off-track node {node_id!r} rendered no marker (bare line end)"
    )
    # The marker seats on the node coordinate (the line end), not elsewhere.
    assert any(
        abs(float(m.get("x")) + float(m.get("width")) / 2 - station.x) <= 1.0
        and abs(float(m.get("y")) + float(m.get("height")) / 2 - station.y) <= 1.0
        for m in markers
    ), f"{node_id!r} marker is not seated on the node ({station.x},{station.y})"


def test_plain_offtrack_input_label_does_not_overlap() -> None:
    """The plain off-track input label clears its drop and its neighbour."""
    graph = _laid_out("rail_offtrack_plain_io")
    offsets = compute_station_offsets(graph)
    placements = place_labels(graph, station_offsets=offsets)
    overlaps = find_label_overlaps(graph, placements, offsets)
    assert not overlaps, f"off-track plain input label overlaps: {overlaps}"
