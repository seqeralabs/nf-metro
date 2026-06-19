"""``route_edges`` is pure with respect to station placement (#678).

Routing consumes placement and produces paths; it must not move stations.
The bubble-centring post-pass emits its X-targets as *move requests*:
``route_edges`` adjusts its own route points (its legitimate output) but leaves
``graph.stations`` untouched.  The render path applies the requests explicitly
to settle the markers; every other caller (the Pass C bisection guards, the
label/icon strike guards, introspection tooling) ignores them and gets a route
it can inspect without snapshotting/restoring ``Station.x`` -- closing the #518
trap at its source.

Probe: lay out each corpus fixture, then route it and assert no station's
``(x, y)`` changed.  ``variant_calling`` is the sharpest case: its centring
splits ``gatk``/``deepvariant`` off their shared column in the routes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import content_corpus

from nf_metro.convert import convert_nextflow_dag
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid

CORPUS = content_corpus()


@pytest.mark.parametrize("fixture", CORPUS, ids=[fid for fid, _, _ in CORPUS])
def test_route_edges_does_not_move_stations(fixture):
    fid, path, is_nextflow = fixture
    text = path.read_text()
    if is_nextflow:
        text = convert_nextflow_dag(text)
    graph = parse_metro_mermaid(text)
    compute_layout(graph, validate=False)

    before = {sid: (s.x, s.y) for sid, s in graph.stations.items()}
    offsets = compute_station_offsets(graph)
    route_edges(graph, station_offsets=offsets)
    after = {sid: (s.x, s.y) for sid, s in graph.stations.items()}

    moved = [sid for sid in before if before[sid] != after[sid]]
    sample = {
        sid: (
            tuple(round(v, 2) for v in before[sid]),
            tuple(round(v, 2) for v in after[sid]),
        )
        for sid in moved[:8]
    }
    assert not moved, (
        f"{fid}: route_edges moved {len(moved)} station(s); routing must be "
        f"placement-pure (emit move requests, not mutate graph.stations). "
        f"Moved (before -> after): {sample}"
    )


def test_route_edges_splits_variant_calling_column_only_in_routes():
    """Centring lands in the *routes*, leaving the shared column on the graph.

    ``gatk``/``deepvariant`` share a column after layout.  Routing centres each
    onto its own diagonals (the move the render path applies), but on the
    graph the pair must stay on their shared column: the move is a request, not
    an in-place mutation.
    """
    path = Path(__file__).resolve().parent.parent / "examples" / "variant_calling.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=False)

    shared_x = graph.stations["gatk"].x
    assert graph.stations["deepvariant"].x == pytest.approx(shared_x)

    offsets = compute_station_offsets(graph)
    route_edges(graph, station_offsets=offsets)

    assert graph.stations["gatk"].x == pytest.approx(shared_x), (
        "route_edges moved gatk off its column; the centring X must be a move "
        "request, not an in-place mutation"
    )
    assert graph.stations["deepvariant"].x == pytest.approx(shared_x), (
        "route_edges moved deepvariant off its column; the centring X must be a "
        "move request, not an in-place mutation"
    )
