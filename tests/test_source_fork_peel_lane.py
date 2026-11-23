"""A separate source's line stays clear of the lane a fork peels a branch into.

A fork on the trunk peels its second line off to a branch station, and an
unrelated source starts a third line whose run passes that branch's layer on its
way to a later trunk station.  The fork may itself be a source sharing the other
source's column, or sit one or two stations down the trunk.  Either way the
source must seat on the side of the trunk away from the branch, in the nearest
row that leaves no empty row in its column, so that its line never runs through
the branch marker or crosses another line, and each station's lanes stack in the
order their lines arrive and leave.  The outcome must not depend on whether the
source's line is declared before or after the peeled line, nor on orientation.
"""

from __future__ import annotations

import warnings
from itertools import combinations
from pathlib import Path

import pytest
from layout_validator import _segments_cross, validate_layout
from orientation_transform import Orientation, all_orientations, transform_source

from nf_metro.api import RenderConfig, prepare_graph, render_graph
from nf_metro.layout.engine import _station_marker_bbox
from nf_metro.layout.geometry import lanes_run_along_y, segment_intersects_bbox
from nf_metro.layout.phases.guards import PermissiveGuardWarning
from nf_metro.layout.routing import compute_station_offsets
from nf_metro.parser.model import MetroGraph
from nf_metro.render.validate import drawn_segments, parse_route_polylines
from nf_metro.themes import THEMES

TOPOLOGIES = Path(__file__).parent.parent / "examples" / "topologies"
FIXTURES = {
    "source_fork": "source_sibling_vs_fork_peel_lane.mmd",
    "fed_fork": "source_vs_fed_fork_peel_lane.mmd",
    "chain_fed_fork": "source_vs_chain_fed_fork_peel_lane.mmd",
}
SIBLING_LINE = "ref"
SIBLING = "genome"
FORK = "reads"
TRUNK_NEXT = "trim"
PEEL_STATION = "qc"

_QC_DECL = "%%metro line: qc | QC | #0570b0\n"
_REF_DECL = "%%metro line: ref | Reference | #f5c542\n"


def _sibling_declared_first(source: str) -> str:
    assert _QC_DECL + _REF_DECL in source
    return source.replace(_QC_DECL + _REF_DECL, _REF_DECL + _QC_DECL)


DECLARATION_ORDERS = {
    "peel_line_first": lambda s: s,
    "sibling_line_first": _sibling_declared_first,
}


def _column_gaps(graph: MetroGraph) -> list[tuple[str, str, float]]:
    """Empty rows between consecutive stations in the sibling's column.

    Measured in the section's own frame: a column is the stations sharing the
    sibling's coordinate along the flow, and a row is one peel pitch (the
    trunk-to-branch distance across the flow).
    """
    section_id = graph.stations[SIBLING].section_id
    assert section_id is not None
    horizontal = lanes_run_along_y(graph.sections[section_id].direction)

    def along(sid: str) -> float:
        station = graph.stations[sid]
        return station.x if horizontal else station.y

    def across(sid: str) -> float:
        station = graph.stations[sid]
        return station.y if horizontal else station.x

    pitch = abs(across(PEEL_STATION) - across(FORK))
    column = sorted(
        (
            sid
            for sid, station in graph.stations.items()
            if not station.is_port and abs(along(sid) - along(SIBLING)) < 1.0
        ),
        key=across,
    )
    return [
        (a, b, (across(b) - across(a)) / pitch)
        for a, b in zip(column, column[1:])
        if across(b) - across(a) > 1.5 * pitch
    ]


def _side_of_trunk(graph: MetroGraph, sid: str) -> float:
    """Signed side of *sid* relative to the trunk run leaving the fork."""
    fork, ahead, station = (graph.stations[n] for n in (FORK, TRUNK_NEXT, sid))
    flow = (ahead.x - fork.x, ahead.y - fork.y)
    rel = (station.x - fork.x, station.y - fork.y)
    return flow[0] * rel[1] - flow[1] * rel[0]


@pytest.mark.parametrize("orientation", list(all_orientations()), ids=lambda o: o.name)
@pytest.mark.parametrize("order", sorted(DECLARATION_ORDERS))
@pytest.mark.parametrize("fixture", sorted(FIXTURES))
def test_sibling_line_clears_peel_branch(
    fixture: str, order: str, orientation: Orientation
) -> None:
    source = transform_source(
        DECLARATION_ORDERS[order]((TOPOLOGIES / FIXTURES[fixture]).read_text()),
        orientation,
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        graph = prepare_graph(source)
        svg = render_graph(graph, THEMES["nfcore"], RenderConfig())

    downgrades = [
        str(w.message) for w in caught if issubclass(w.category, PermissiveGuardWarning)
    ]
    assert not downgrades, downgrades

    assert _side_of_trunk(graph, SIBLING) * _side_of_trunk(graph, PEEL_STATION) < 0, (
        f"{SIBLING!r} sits on the same side of the trunk as {PEEL_STATION!r}"
    )

    routes = parse_route_polylines(svg)
    offsets = compute_station_offsets(graph)
    sibling_segments = [
        (p, q) for line_id, p, q in drawn_segments(routes) if line_id == SIBLING_LINE
    ]
    for sid, station in graph.stations.items():
        if station.is_port or SIBLING_LINE in graph.station_lines(sid):
            continue
        bbox = _station_marker_bbox(graph, sid, offsets=offsets)
        if bbox is None:
            continue
        hits = [
            (p, q)
            for p, q in sibling_segments
            if segment_intersects_bbox(p[0], p[1], q[0], q[1], bbox)
        ]
        assert not hits, f"line {SIBLING_LINE!r} crosses {sid!r} marker {bbox}: {hits}"

    segments = drawn_segments(routes)
    crossings = [
        (a[0], b[0], point)
        for a, b in combinations(segments, 2)
        if a[0] != b[0] and (point := _segments_cross(a[1], a[2], b[1], b[2]))
    ]
    assert not crossings, f"lines cross: {crossings}"

    gaps = _column_gaps(graph)
    assert not gaps, f"empty rows in {SIBLING!r}'s column (rows apart): {gaps}"

    flagged = [
        v.message for v in validate_layout(graph) if v.check == "route_segment_crossing"
    ]
    assert not flagged, flagged
