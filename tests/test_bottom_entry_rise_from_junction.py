"""A bundle rising out of a junction into a BOTTOM entry port.

The lines keep their order through every turn of the rise: a fan traverse
turned onto from an ascent nests in the mirror order of one turned onto from a
descent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.api import render_string
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import check_bundle_order_preserved
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph

REPROS = Path(__file__).resolve().parent / "fixtures" / "curve_invariant_repros"
RISE_FROM_JUNCTION = REPROS / "bt_bottom_entry_rise_from_junction.mmd"


def _laid_out(path: Path) -> MetroGraph:
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    return graph


@pytest.mark.parametrize("path", [RISE_FROM_JUNCTION], ids=lambda p: p.stem)
def test_render_completes(path: Path) -> None:
    render_string(path.read_text(), source_dir=str(path.parent))


def test_bundle_rising_into_bottom_entry_keeps_its_order() -> None:
    graph = _laid_out(RISE_FROM_JUNCTION)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    violations = check_bundle_order_preserved(routes)
    assert violations == [], violations[0].message()
