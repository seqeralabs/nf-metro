"""A carrier station's marker spans every line it serves (#1833).

A pill covers each line that stops at the station, so a carrier whose lead-in
is a separately-originating bundle member keeps that member under its marker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import STATION_RADIUS_APPROX
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases._common import _station_bundle_offset_span
from nf_metro.layout.routing.offsets import compute_station_offsets
from nf_metro.parser import parse_metro_mermaid

FIXTURES = Path(__file__).parent / "fixtures"
RIBOSEQ = FIXTURES / "curve_invariant_repros" / "riboseq_inter_row_corridor.mmd"


@pytest.fixture(name="riboseq_layout")
def _riboseq_layout():
    graph = parse_metro_mermaid(RIBOSEQ.read_text())
    compute_layout(graph, x_spacing=70, validate=False)
    return graph, compute_station_offsets(graph)


def test_hybrid_merge_marker_spans_both_lines(riboseq_layout) -> None:
    graph, offsets = riboseq_layout
    sid = "hybrid_merge"
    served = tuple(graph.station_lines(sid))
    assert set(served) == {"annotation", "rnaseq"}

    lo, hi = _station_bundle_offset_span(graph, sid, offsets)
    served_offs = [offsets[sid, line_id] for line_id in served]
    assert (lo, hi) == (min(served_offs), max(served_offs))

    radius = STATION_RADIUS_APPROX * graph.stroke_scale
    height = (hi - lo) + 2 * radius
    assert height == pytest.approx(14.0)
