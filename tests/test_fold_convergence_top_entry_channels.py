"""Distinct lines converging into a folded sink's TOP-entry port (#1144).

When ``fold_threshold`` relocates a convergence sink onto a lower row of the
same column its branches occupy, the sink is fed through a TOP entry port by
two (or more) distinct single-line feeders -- one per branch -- that bundle
together inside the sink section.

Two separations must survive the fold:

* the converging feeders, and the intra-section drop that continues them into
  the merge station, ride **parallel X channels** into the port (the
  merge-approach slot), rather than collapsing onto the port's single trunk X
  and overlaying each other on one vertical channel; and
* the multi-line run inside the sink (``merge -> report``) keeps its per-line
  Y offset, rather than collapsing to a zero-offset collinear bundle.

The fold directive is injected at test time rather than shipped as a fixture so
the corpus-wide invariant suite is not bound to a folded layout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import (
    compute_station_offsets,
    route_edges_centred,
)
from nf_metro.layout.routing.invariants import (
    assert_render_curve_invariants,
    check_collinear_distinct_lines,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

TOPOLOGIES_DIR = Path(__file__).parent.parent / "examples" / "topologies"

BASE_FIXTURE = "convergence_fold_diamond"
# Folds 1-4 relocate ``finish`` onto row 2 of column 1 (below both branches),
# feeding it through a TOP entry convergence; 5+ leave it a side entry. The
# shipped fixture bakes fold 4; the band guards the whole relocating range.
FOLDS = [1, 2, 3, 4]


def _folded_text(fold: int) -> str:
    text = (TOPOLOGIES_DIR / f"{BASE_FIXTURE}.mmd").read_text()
    return text.replace(
        "%%metro fold_threshold: 4", f"%%metro fold_threshold: {fold}", 1
    )


def _route_at_fold(fold: int):
    graph = parse_metro_mermaid(_folded_text(fold))
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    return graph, routes, offsets


@pytest.mark.parametrize("fold", FOLDS)
def test_folded_sink_is_fed_through_top_entry_convergence(fold: int) -> None:
    """The fold relocates ``finish`` so it is a TOP-entry convergence.

    Guards that the parametrised folds actually exercise the
    merge-into-top-entry convergence rather than silently passing on a layout
    where the sink stays a side entry (which would make the channel assertions
    vacuous).
    """
    graph, _routes, _offsets = _route_at_fold(fold)
    convergence_top_entries = [
        pid
        for pid, port in graph.ports.items()
        if port.section_id == "finish"
        and port.is_entry
        and "entry_top" in pid
        and len({e.source for e in graph.edges_to(pid)}) >= 2
        and len({e.target for e in graph.edges_from(pid)}) == 1
    ]
    assert convergence_top_entries, f"fold {fold}: finish not a TOP-entry convergence"


@pytest.mark.parametrize("fold", FOLDS)
def test_folded_convergence_channels_do_not_collapse(fold: int) -> None:
    graph, routes, offsets = _route_at_fold(fold)

    violations = check_collinear_distinct_lines(
        graph, routes, offsets, scopes=("inter", "intra")
    )
    assert not violations, "\n".join(v.message() for v in violations)


@pytest.mark.parametrize("fold", FOLDS)
def test_folded_intra_bundle_keeps_per_line_offset(fold: int) -> None:
    """The ``merge -> report`` run carries its two lines on distinct slots."""
    graph, _routes, offsets = _route_at_fold(fold)
    for station in ("merge", "report"):
        line_offsets = {
            lid: offsets.get((station, lid), 0.0)
            for lid in graph.station_lines(station)
        }
        assert len(set(line_offsets.values())) == len(line_offsets), (
            f"fold {fold}: {station} bundle collapsed to one slot: {line_offsets}"
        )


@pytest.mark.parametrize("fold", FOLDS)
def test_folded_convergence_render_passes_curve_self_check(fold: int) -> None:
    graph, routes, offsets = _route_at_fold(fold)
    assert_render_curve_invariants(graph, routes, offsets)
