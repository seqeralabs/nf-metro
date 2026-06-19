"""Symbolic trunk slots (#845-D).

``TrunkSlot`` lets a handler declare *which* inter-row gap a U-shaped bypass
runs its horizontal trunk through, travelling which way, and the single
:func:`_materialize_trunk_slots` pass groups the declared trunks by
``(gap_upper_row, direction)`` and fans them into one concentric band -- the
horizontal-trunk twin of the gap-slot materialization.
"""

from __future__ import annotations

import pytest
from conftest import content_corpus

from nf_metro.convert import convert_nextflow_dag
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, normalize, route_edges
from nf_metro.layout.routing.common import RoutedPath, TrunkSlot
from nf_metro.layout.routing.invariants import check_trunks_declared
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge

CORPUS = content_corpus()


def test_trunk_slot_carries_gap_identity():
    assert TrunkSlot(gap_upper_row=1).gap_upper_row == 1
    assert TrunkSlot(gap_upper_row=None).gap_upper_row is None


def test_routed_path_trunk_slot_defaults_to_none():
    rp = RoutedPath(
        edge=Edge(source="a", target="b", line_id="l1"),
        line_id="l1",
        points=[(0.0, 0.0), (10.0, 0.0)],
    )
    assert rp.trunk_slot is None


def test_normalize_bypass_trunks_is_renamed():
    """The geometric-rediscovery entry point is replaced by materialization."""
    assert not hasattr(normalize, "_normalize_bypass_trunks")
    assert hasattr(normalize, "_materialize_trunk_slots")


def _route_corpus(fixture):
    fid, path, is_nextflow = fixture
    text = path.read_text()
    if is_nextflow:
        text = convert_nextflow_dag(text)
    graph = parse_metro_mermaid(text)
    compute_layout(graph, validate=False)
    offsets = compute_station_offsets(graph)
    return graph, route_edges(graph, station_offsets=offsets)


@pytest.mark.parametrize("fixture", CORPUS, ids=[fid for fid, _, _ in CORPUS])
def test_every_inter_section_trunk_is_declared(fixture):
    """No inter-section bypass trunk reaches the renderer without a TrunkSlot.

    The materialization only fans declared trunks, so an undeclared trunk would
    escape its gap's concentric band and stay fused on a sibling at its raw Y --
    the regression the migration must not allow.
    """
    _graph, routes = _route_corpus(fixture)
    violations = check_trunks_declared(routes)
    assert not violations, "; ".join(v.message() for v in violations[:3])


_TRUNK_FIXTURES = {"differentialabundance", "longread_variant_calling"}


@pytest.mark.parametrize(
    "fixture",
    [f for f in CORPUS if f[0] in _TRUNK_FIXTURES],
    ids=lambda f: f[0],
)
def test_handlers_emit_trunk_slots(fixture):
    """A multi-row fixture with U-shaped bypasses exercises trunk declarations."""
    _graph, routes = _route_corpus(fixture)
    assert any(r.trunk_slot is not None for r in routes), (
        f"{fixture[0]}: no route declared a TrunkSlot; an inter-section handler "
        f"emitting a bypass trunk must annotate it"
    )


def test_guard_flags_an_undeclared_trunk():
    """A U-route with a horizontal trunk but no slot is caught by the guard."""
    rp = RoutedPath(
        edge=Edge(source="a", target="b", line_id="l1"),
        line_id="l1",
        points=[(0.0, 0.0), (0.0, 100.0), (200.0, 100.0), (200.0, 0.0)],
        is_inter_section=True,
    )
    assert check_trunks_declared([rp]), (
        "guard should flag the undeclared trunk running at y=100"
    )
    rp.declare_trunk_slot(gap_upper_row=0)
    assert not check_trunks_declared([rp])
