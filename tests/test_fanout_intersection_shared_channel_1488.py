"""Fan-out feed routes keep OFFSET_STEP on a shared inter-section channel (#1488).

When a fan-out junction feeds two or more downstream sections and the feed
routes co-travel a shared axis-aligned inter-section channel before peeling off
to their targets, the co-travelling distinct lines must stay one ``OFFSET_STEP``
apart, the same as an ordinary bundle.  A bundle-offset defect collapsed them to
roughly half a step (~2px) on the shared run -- an overlay the always-on
``check_collinear_distinct_lines`` guard misses because its 1px lateral tolerance
treats a 2px gap as "distinct".

The regression fixture is a source that fans out to two stacked sections whose
feeds share a channel and peel into a top entry and a left entry; the assertion
measures the minimum lateral gap of every distinct-line inter-section co-run and
requires it to be at least one ``OFFSET_STEP`` (a real overlay lands near 2px).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import COORD_TOLERANCE_FINE, OFFSET_STEP
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.layout.routing.common import apply_route_offsets
from nf_metro.parser.mermaid import parse_metro_mermaid

ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = ROOT / "examples" / "topologies"

# Fixtures whose inter-section routing runs distinct lines together along a
# shared channel: the fan-out regression fixture plus gallery topologies that
# fan a junction into stacked or side-entered targets, so the invariant
# generalises beyond the single repro.
FIXTURES = [
    TOPOLOGIES / "fanout_intersection_shared_channel.mmd",
    TOPOLOGIES / "packed_cell_consumer_drop_in.mmd",
    TOPOLOGIES / "straddling_fanout_junction.mmd",
]
IDS = [p.stem for p in FIXTURES]

_LATERAL_BAND = OFFSET_STEP  # only pairs within a step count as one co-run
_MIN_SPAN = 30.0


def _axis_aligned(p1, p2):
    """Return ``(axis, coord, lo, hi)`` for a horizontal/vertical segment."""
    (x1, y1), (x2, y2) = p1, p2
    if abs(x1 - x2) < COORD_TOLERANCE_FINE and abs(y1 - y2) > 1.0:
        return "V", (x1 + x2) * 0.5, *sorted((y1, y2))
    if abs(y1 - y2) < COORD_TOLERANCE_FINE and abs(x1 - x2) > 1.0:
        return "H", (y1 + y2) * 0.5, *sorted((x1, x2))
    return None


def _shared_channel_gaps(graph, routes, offsets):
    """Minimum lateral gap of each distinct-line inter-section co-run.

    Yields ``(line_a, line_b, gap, span)`` for every pair of different-line
    inter-section axis-aligned segments that share an axis, overlap by more than
    ``_MIN_SPAN``, sit within ``_LATERAL_BAND`` of each other, and do not merely
    converge onto a shared endpoint port.
    """
    segs = []
    for rp in routes:
        if not rp.is_inter_section:
            continue
        pts = apply_route_offsets(rp, offsets)
        for p1, p2 in zip(pts, pts[1:]):
            aligned = _axis_aligned(p1, p2)
            if aligned is not None:
                segs.append((rp, *aligned))

    for i in range(len(segs)):
        rp_a, ax_a, c_a, lo_a, hi_a = segs[i]
        for j in range(i + 1, len(segs)):
            rp_b, ax_b, c_b, lo_b, hi_b = segs[j]
            if rp_a.line_id == rp_b.line_id or ax_a != ax_b:
                continue
            gap = abs(c_a - c_b)
            if gap <= COORD_TOLERANCE_FINE or gap >= _LATERAL_BAND + 0.5:
                continue
            span = min(hi_a, hi_b) - max(lo_a, lo_b)
            if span <= _MIN_SPAN:
                continue
            shared = {rp_a.edge.source, rp_a.edge.target} & {
                rp_b.edge.source,
                rp_b.edge.target,
            }
            if shared & set(graph.ports):
                continue
            yield rp_a.line_id, rp_b.line_id, gap, span


@pytest.mark.parametrize("fixture", FIXTURES, ids=IDS)
def test_fanout_shared_channel_keeps_offset_step(fixture: Path) -> None:
    graph = parse_metro_mermaid(fixture.read_text())
    compute_layout(graph, validate=False)
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)

    too_close = [
        f"lines {a!r}/{b!r} co-travel {span:.0f}px only {gap:.1f}px apart "
        f"(need >= {OFFSET_STEP:.1f}px)"
        for a, b, gap, span in _shared_channel_gaps(graph, routes, offsets)
        if gap < OFFSET_STEP - COORD_TOLERANCE_FINE
    ]
    assert not too_close, "; ".join(too_close)
