"""Section headers must never be drawn across a routed metro line (issue #774).

A line entering a section through an edge under its top-left header would cross
the title text.  The placement chain relocates the header (below, rotated onto a
side, or nudged past the trunk) instead of routing the line around the title.

Covers:

* Happy-path: every shipped example and topology fixture places every section
  header clear of every route.
* Meaningfulness: with header relocation disabled (the resolver pinned to its
  default above-left position) the new fixtures clash, proving the chain - not
  coincidence - is what keeps them clear.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nf_metro.render.section_header as section_header
from nf_metro.api import resolve_theme
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.section_header import (
    check_section_headers_clear_routes,
    resolve_all_section_headers,
)
from nf_metro.render.svg import apply_route_offsets

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
EXAMPLE_TOPOLOGIES = EXAMPLES / "topologies"
FIXTURE_TOPOLOGIES = REPO_ROOT / "tests" / "fixtures" / "topologies"

RELOCATION_FIXTURES = [
    EXAMPLE_TOPOLOGIES / "top_entry_header_clash.mmd",
    EXAMPLE_TOPOLOGIES / "header_side_rotated.mmd",
    EXAMPLE_TOPOLOGIES / "header_nudge.mmd",
]


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
    paths.extend(sorted(EXAMPLE_TOPOLOGIES.glob("*.mmd")))
    paths.extend(sorted(FIXTURE_TOPOLOGIES.glob("*.mmd")))
    return paths


def _polylines_and_font(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    polylines = [apply_route_offsets(route, offsets) for route in routes]
    font_size = resolve_theme(None, graph).section_label_font_size
    return graph, polylines, font_size


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_no_section_header_route_clashes_in_gallery(path: Path) -> None:
    """Every section header clears every route across the shipped corpus."""
    graph, polylines, font_size = _polylines_and_font(path)
    placements = resolve_all_section_headers(graph, font_size, polylines)
    clashes = check_section_headers_clear_routes(placements, polylines)
    assert not clashes, "\n".join(c.message() for c in clashes)


@pytest.mark.parametrize("path", RELOCATION_FIXTURES, ids=lambda p: p.stem)
def test_default_above_placement_would_clash(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pinning every header to its default above-left position reintroduces the
    clash on the relocation fixtures, so the chain is doing real work."""
    monkeypatch.setattr(section_header, "_placement_clear", lambda *a, **k: True)
    graph, polylines, font_size = _polylines_and_font(path)
    placements = resolve_all_section_headers(graph, font_size, polylines)
    clashes = check_section_headers_clear_routes(placements, polylines)
    assert clashes, "expected an above-left header to clash with the route"
