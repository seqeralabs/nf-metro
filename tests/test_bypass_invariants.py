"""Invariants asserting non-consumer marker bypass across guide fixtures.

Companion to ``test_layout_invariants.py``'s
``test_lines_dont_cross_non_consumer_markers``, which only parametrizes
over ``da_pipeline.mmd`` and ``rnaseq_sections.mmd``.  This file
parametrizes the same invariant over the guide-family fixtures whose
topology produces a non-consumer crossing that the differential-abundance
trigger in ``_insert_bypass_stations`` historically did not catch.

The 5 fixtures are:

* ``examples/guide/05_file_icons.mmd`` -- ``qc`` from ``trim`` to
  reporting crosses ``align``'s marker on the way to the section exit.
* ``examples/guide/05c_files_icon.mmd`` -- same pattern as 05.
* ``examples/guide/05d_folder_icon.mmd`` -- same pattern as 05.
* ``examples/guide/06a_without_hidden.mmd`` -- ``prot`` from ``search``
  to reporting crosses ``quant``'s marker as the fan-up to trunk
  begins past ``quant``'s column.
* ``examples/guide/06b_with_hidden.mmd`` -- same pattern as 06a.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from nf_metro.layout.engine import _station_marker_bbox, compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.svg import apply_route_offsets

GUIDE = Path(__file__).resolve().parent.parent / "examples" / "guide"

_GUIDE_BYPASS_FIXTURES = [
    "05_file_icons.mmd",
    "05c_files_icon.mmd",
    "05d_folder_icon.mmd",
    "06a_without_hidden.mmd",
    "06b_with_hidden.mmd",
]


def _layout_guide(name: str):
    text = (GUIDE / name).read_text()
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    return graph


def _seg_crosses_bbox(
    p1: tuple[float, float],
    p2: tuple[float, float],
    bbox: tuple[float, float, float, float],
) -> bool:
    x1, y1 = p1
    x2, y2 = p2
    bx1, by1, bx2, by2 = bbox
    if max(x1, x2) < bx1 or min(x1, x2) > bx2:
        return False
    if max(y1, y2) < by1 or min(y1, y2) > by2:
        return False
    for k in range(21):
        f = k / 20.0
        x = x1 + f * (x2 - x1)
        y = y1 + f * (y2 - y1)
        if bx1 <= x <= bx2 and by1 <= y <= by2:
            return True
    return False


@pytest.mark.parametrize("fixture", _GUIDE_BYPASS_FIXTURES)
def test_guide_lines_dont_cross_non_consumer_markers(fixture):
    """No rendered line segment may pass through the marker bbox of any
    station that neither consumes nor produces that line.
    """
    graph = _layout_guide(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    consumed_by: dict[str, set[str]] = defaultdict(set)
    produced_by: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges:
        consumed_by[e.target].add(e.line_id)
        produced_by[e.source].add(e.line_id)

    for sid in graph.stations:
        bbox = _station_marker_bbox(graph, sid, offsets=offsets)
        if bbox is None:
            continue
        station_lines = consumed_by.get(sid, set()) | produced_by.get(sid, set())
        for r in routes:
            if r.line_id in station_lines:
                continue
            if r.edge.source == sid or r.edge.target == sid:
                continue
            pts = apply_route_offsets(r, offsets)
            for k in range(len(pts) - 1):
                if _seg_crosses_bbox(pts[k], pts[k + 1], bbox):
                    raise AssertionError(
                        f"{fixture}: line {r.line_id!r} on edge "
                        f"{r.edge.source!r} -> {r.edge.target!r} "
                        f"crosses non-consumer station {sid!r} "
                        f"marker bbox "
                        f"({bbox[0]:.1f},{bbox[1]:.1f})-"
                        f"({bbox[2]:.1f},{bbox[3]:.1f}); segment "
                        f"({pts[k][0]:.1f},{pts[k][1]:.1f})->"
                        f"({pts[k + 1][0]:.1f},{pts[k + 1][1]:.1f})"
                    )
