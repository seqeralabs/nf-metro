"""Symbolic gap slots (#845-B).

``GapSlot`` lets a handler declare *where* a vertical channel run sits in a gap
bundle -- which inter-column corridor, which row, travelling which way -- and the
single :func:`_materialize_gap_slots` pass groups the declared slots by ``(gap,
row, direction)`` and assigns the concentric X.
"""

from __future__ import annotations

import pytest
from conftest import content_corpus

from nf_metro.convert import convert_nextflow_dag
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, normalize, route_edges
from nf_metro.layout.routing.common import Direction, GapSlot, RoutedPath
from nf_metro.layout.routing.invariants import check_gap_channels_materialized
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge

CORPUS = content_corpus()


def test_gap_slot_carries_documented_fields():
    slot = GapSlot(
        gap_lo_col=2,
        gap_hi_col=3,
        row=1,
        direction=Direction.D,
        slot_index=1,
        n_slots=3,
    )
    assert (slot.gap_lo_col, slot.gap_hi_col) == (2, 3)
    assert slot.row == 1
    assert slot.direction is Direction.D
    assert (slot.slot_index, slot.n_slots) == (1, 3)


def test_routed_path_gap_slots_default_to_empty():
    rp = RoutedPath(
        edge=Edge(source="a", target="b", line_id="l1"),
        line_id="l1",
        points=[(0.0, 0.0), (10.0, 0.0)],
    )
    assert rp.gap_slots == []


def test_normalize_gap_channels_is_gone():
    """The geometric-rediscovery pass is fully replaced by materialization."""
    assert not hasattr(normalize, "_normalize_gap_channels")
    assert not hasattr(normalize, "_collect_vchannels")
    assert not hasattr(normalize, "_match_channel_gap")
    assert not hasattr(normalize, "_bucket_gap_channels")
    assert hasattr(normalize, "_materialize_gap_slots")


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
def test_every_gap_channel_is_declared(fixture):
    """No non-exempt inter-section leg sits in a gap without a GapSlot.

    The materialization only re-stacks declared legs, so an undeclared in-gap
    leg would escape concentric placement -- the regression the migration must
    not allow.
    """
    graph, routes = _route_corpus(fixture)
    violations = check_gap_channels_materialized(graph, routes)
    assert not violations, "; ".join(v.message() for v in violations[:3])


@pytest.mark.parametrize(
    "fixture",
    [f for f in CORPUS if f[0] in {"differentialabundance", "genomeassembly"}],
    ids=lambda f: f[0],
)
def test_handlers_emit_gap_slots(fixture):
    """A multi-section fixture exercises the handlers' slot declarations."""
    _graph, routes = _route_corpus(fixture)
    assert any(r.gap_slots for r in routes), (
        f"{fixture[0]}: no route declared a GapSlot; the handlers must annotate "
        f"their inter-section gap channels"
    )


def test_guard_flags_an_undeclared_gap_channel():
    """A route with an in-gap vertical leg but no slot is caught by the guard."""

    class _Sec:
        def __init__(self, col, x, w):
            self.grid_col = col
            self.grid_row = 0
            self.grid_row_span = 1
            self.bbox_x = x
            self.bbox_y = 0.0
            self.bbox_w = w
            self.bbox_h = 100.0

    class _Graph:
        sections = {"a": _Sec(0, 0.0, 100.0), "b": _Sec(1, 200.0, 100.0)}

    rp = RoutedPath(
        edge=Edge(source="a", target="b", line_id="l1"),
        line_id="l1",
        points=[(0.0, 50.0), (150.0, 50.0), (150.0, 250.0)],
        is_inter_section=True,
    )
    assert check_gap_channels_materialized(_Graph(), [rp]), (
        "guard should flag the undeclared descent at x=150 in gap (0,1)"
    )
    rp.declare_gap_slot(
        lo_col=0,
        hi_col=1,
        row=0,
        direction=Direction.D,
        slot_index=0,
        n_slots=1,
    )
    assert not check_gap_channels_materialized(_Graph(), [rp])
