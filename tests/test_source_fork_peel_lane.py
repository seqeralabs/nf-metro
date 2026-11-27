"""A source station's sibling stays off the lane its fork peels a branch into.

Two predecessor-less stations share a source column: a fork whose second line
peels off to its own branch station, and an unrelated source whose line runs
past that branch to a later station.  If the sibling is seated on the lane the
peel opens into, its line runs flat through the branch station's marker.  The
outcome must not depend on whether the sibling's line is declared before or
after the peeled line.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import networkx as nx
import pytest
from orientation_transform import Orientation, all_orientations, transform_source

from nf_metro.api import RenderConfig, prepare_graph, render_graph
from nf_metro.layout.engine import _station_marker_bbox
from nf_metro.layout.geometry import segment_intersects_bbox
from nf_metro.layout.ordering import _claimed_peel_lane
from nf_metro.layout.phases.guards import PermissiveGuardWarning
from nf_metro.layout.routing import compute_station_offsets
from nf_metro.parser import parse_metro_mermaid
from nf_metro.render.validate import parse_route_polylines
from nf_metro.themes import THEMES

FIXTURE = (
    Path(__file__).parent.parent
    / "examples"
    / "topologies"
    / "source_sibling_vs_fork_peel_lane.mmd"
)
SIBLING_LINE = "ref"
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


@pytest.mark.parametrize("orientation", list(all_orientations()), ids=lambda o: o.name)
@pytest.mark.parametrize("order", sorted(DECLARATION_ORDERS))
def test_sibling_line_clears_peel_branch_marker(
    order: str, orientation: Orientation
) -> None:
    source = transform_source(
        DECLARATION_ORDERS[order](FIXTURE.read_text()), orientation
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        graph = prepare_graph(source)
        svg = render_graph(graph, THEMES["nfcore"], RenderConfig())

    downgrades = [
        str(w.message) for w in caught if issubclass(w.category, PermissiveGuardWarning)
    ]
    assert not downgrades, downgrades

    bbox = _station_marker_bbox(
        graph, PEEL_STATION, offsets=compute_station_offsets(graph)
    )
    assert bbox is not None
    hits = [
        (p, q)
        for line_id, subpaths in parse_route_polylines(svg)
        if line_id == SIBLING_LINE
        for sub in subpaths
        for p, q in zip(sub, sub[1:])
        if segment_intersects_bbox(p[0], p[1], q[0], q[1], bbox)
    ]
    assert not hits, (
        f"line {SIBLING_LINE!r} crosses {PEEL_STATION!r} marker {bbox}: {hits}"
    )


@pytest.mark.parametrize("with_sibling", [True, False])
def test_a_fork_claims_a_peel_lane_only_beside_a_sibling_source(
    with_sibling: bool,
) -> None:
    graph = parse_metro_mermaid(FIXTURE.read_text())
    section = nx.DiGraph(
        (e.source, e.target)
        for e in graph.edges
        if graph.stations[e.source].section_id == "analysis"
        and graph.stations[e.target].section_id == "analysis"
    )
    if not with_sibling:
        section.remove_node("genome")
    line_base = {"main": 0.0, "qc": 1.0, "ref": 2.0}

    lane = _claimed_peel_lane(
        PEEL_STATION, 1.0, section, {"reads": 0.0}, graph, line_base
    )

    assert lane == (1.0 if with_sibling else None)
