"""An entry-frame carrier's marker must span every line it serves (#1833).

``_cache_linear_entry_pill_lines`` narrows a carrier station's drawn marker
span to the cohort of lines inherited from its linear-entry feeder, on the
premise that the marker's round end-cap already reaches one adjacent lane
beyond that cohort.  That premise only holds when the inherited cohort itself
spans two or more lanes: a single-line cohort's cap covers a lead-in that is a
real, separately-originating bundle member, and narrowing to it silently drops
that line from the marker.

The invariant: a cached entry-frame cohort must contain at least two lines,
and a carrier's drawn bundle span must cover the full offset range of the lines
it serves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import STATION_RADIUS_APPROX
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases._common import (
    _linear_entry_pill_lines,
    _station_bundle_offset_span,
)
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


def test_cached_entry_cohort_never_single_line(riboseq_layout) -> None:
    graph, offsets = riboseq_layout
    for sid in graph.stations:
        inherited = _linear_entry_pill_lines(graph, sid, offsets)
        if inherited is None:
            continue
        assert len(inherited) >= 2, (
            f"{sid} has a single-line cached entry cohort {inherited}; "
            "narrowing its marker span drops a genuine bundle member"
        )
