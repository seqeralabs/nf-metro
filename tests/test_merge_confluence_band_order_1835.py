"""Merge-fed confluence keeps a shared band and its descent on one nesting order.

On the nf-core/riboseq map the ``annotation`` and ``riboseq`` lines converge into
``orf_calling``'s LEFT entry port from different sources -- ``annotation`` around
the section as an exempt wrap, ``riboseq`` through a merge junction -- sharing one
inter-row band and one descent column.  ``check_merge_confluence_band_order``
resolves each peel-off tail's port through the merge chain and flags a
co-travelling distinct-line pair that ranks one way on the band and the other
on the descent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from riboseq_map import RIBOSEQ_MMD

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.layout.routing.common import port_peeloff_tail
from nf_metro.layout.routing.invariants import (
    _terminal_entry_port_id,
    check_merge_confluence_band_order,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
PORT = "orf_calling__entry_left_7"


def _route(text: str):
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    return graph, routes


def test_riboseq_confluence_routes_without_band_descent_crossing() -> None:
    graph, routes = _route(RIBOSEQ_MMD)
    reaching = {
        rp.line_id
        for rp in routes
        if rp.is_inter_section
        and (rp.edge.target == PORT or rp.edge.target.startswith("__merge"))
    }
    assert {"annotation", "riboseq"} <= reaching
    assert check_merge_confluence_band_order(routes, graph) == []


def test_annotation_stays_outer_from_band_through_port_fan() -> None:
    """``annotation`` wraps outer end-to-end into ``orf_calling``'s LEFT port.

    ``annotation`` holds the outer side of the U-wrap: outer (upper) on the
    shared approach band, and outer (lower) on the internal lane once inside the
    port, while ``riboseq`` reaches the same port through a merge junction on the
    inner side.  An internal lane order that seats ``annotation`` inner while its
    band and descent stay outer crosses the two lines in the port throat, so the
    outer approach must carry through to the outer internal lane (#1835).
    """
    graph, routes = _route(RIBOSEQ_MMD)
    offsets = compute_station_offsets(graph)

    band_y: dict[str, float] = {}
    for rp in routes:
        if not rp.is_inter_section or _terminal_entry_port_id(graph, rp) != PORT:
            continue
        tail = port_peeloff_tail(rp)
        if tail is None:
            continue
        band_y[rp.line_id] = min(band_y.get(rp.line_id, tail.trunk_y), tail.trunk_y)

    arrival_y = {
        rp.line_id: rp.points[-1][1]
        for rp in routes
        if rp.is_inter_section and rp.edge.target == PORT
    }

    # Approach band: annotation descends from the row above, so it rides the
    # upper (smaller-Y) band while riboseq's merge trunk sits below it.
    assert band_y["annotation"] < band_y["riboseq"]
    # Arrival at the port: annotation lands on the outer (lower, larger-Y) lane.
    assert arrival_y["annotation"] > arrival_y["riboseq"]
    # Internal fan: the outer approach must continue onto the outer (larger
    # offset) internal lane, or annotation crosses riboseq inside the throat.
    assert offsets[(PORT, "annotation")] > offsets[(PORT, "riboseq")]


def test_check_catches_a_planted_confluence_crossing() -> None:
    graph, routes = _route(RIBOSEQ_MMD)
    annotation = next(
        rp for rp in routes if rp.line_id == "annotation" and rp.edge.target == PORT
    )
    riboseq = next(
        rp
        for rp in routes
        if rp.line_id == "riboseq" and rp.edge.target.startswith("__merge")
    )
    # Re-seat the exempt annotation descent inboard of the riboseq column, onto the
    # port Y riboseq holds, so the band and descent orders disagree at the band's turn.
    rib_peel_x = riboseq.points[-3][0]
    rib_port_y = riboseq.points[-1][1]
    pts = list(annotation.points)
    pts[-3] = (rib_peel_x + 4.0, pts[-3][1])
    pts[-2] = (rib_peel_x + 4.0, rib_port_y - 4.0)
    pts[-1] = (pts[-1][0], rib_port_y - 4.0)
    annotation.points = pts
    violations = check_merge_confluence_band_order(routes, graph)
    assert any(
        {v.line_a, v.line_b} == {"annotation", "riboseq"} and v.port_id == PORT
        for v in violations
    )


def _corpus_fixtures() -> list[Path]:
    paths: list[Path] = []
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
    paths.extend(sorted((EXAMPLES / "topologies").glob("*.mmd")))
    return paths


@pytest.mark.parametrize("fixture", _corpus_fixtures(), ids=lambda p: p.stem)
def test_no_shipped_fixture_trips_the_confluence_oracle(fixture: Path) -> None:
    graph, routes = _route(fixture.read_text())
    assert check_merge_confluence_band_order(routes, graph) == []
