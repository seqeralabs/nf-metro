"""Merge bundle dropping into a folded sink's TOP-entry port (#1142).

When ``fold_threshold`` relocates a convergence sink onto a lower row, the
sink is fed through a TOP entry port: the converging branches' bundle descends
the inter-row corridor, crosses the boundary, and turns into the merge station.

The per-line drop must cross the boundary on the *feeder's section lane* -- the
exact X ``inter_section_handlers._route_tb_bottom_exit`` lands at -- so two
things hold:

* the inter-section approach and the intra-section drop meet at one coordinate
  per line (no sideways jog at the port boundary), and
* the descending lines all sit on the turn-out side of the merge station, so
  the drop->turn corner nests concentrically instead of straddling the station
  and pinching through the bend.

Anchoring the drop on each line's index in the *whole* cross-boundary bundle
(every converging feeder's every line) instead would splay the few descending
lines past the feeder lane (a jog at the port) and straddle the merge station
(a non-concentric corner).

The fold directive is injected at test time rather than shipped as a fixture;
this test scopes itself to the corner and seam geometry, which is exercised
regardless.
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
    check_bundle_order_preserved,
    check_concentric_bundle_corners,
    check_seam_segments_meet_at_port,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

TOPOLOGIES_DIR = Path(__file__).parent.parent / "examples" / "topologies"

# ``shared_sink_parallel`` is a three-branch parallel fan into one sink. A
# tightened fold relocates the sink onto a lower row, where it is fed through a
# TOP entry port -- the geometry that exercised the bug. The bug shows at every
# fold that relocates the sink, so exercise a range to guard the whole band.
BASE_FIXTURE = "shared_sink_parallel"
# Folds 1-3 relocate the sink to a TOP-entry merge (the bug geometry); 4+ leave
# it a side entry. ``test_folded_sink_is_fed_through_top_entry`` guards the band.
FOLDS = [1, 2, 3]


def _folded_text(fold: int) -> str:
    text = (TOPOLOGIES_DIR / f"{BASE_FIXTURE}.mmd").read_text()
    return text.replace("graph LR", f"%%metro fold_threshold: {fold}\ngraph LR", 1)


def _route_at_fold(fold: int):
    graph = parse_metro_mermaid(_folded_text(fold))
    graph.source_dir = str(TOPOLOGIES_DIR)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    return graph, routes, offsets


@pytest.mark.parametrize("fold", FOLDS)
def test_folded_sink_top_entry_merge_corner_is_concentric(fold: int) -> None:
    graph, routes, offsets = _route_at_fold(fold)

    pinches = check_concentric_bundle_corners(graph, routes, offsets)
    assert not pinches, "\n".join(v.message() for v in pinches)

    flips = check_bundle_order_preserved(routes)
    assert not flips, "\n".join(v.message() for v in flips)


@pytest.mark.parametrize("fold", FOLDS)
def test_folded_sink_top_entry_drop_meets_feeder_at_port(fold: int) -> None:
    graph, routes, offsets = _route_at_fold(fold)

    gaps = check_seam_segments_meet_at_port(graph, routes, offsets)
    assert not gaps, "\n".join(g.message() for g in gaps)


@pytest.mark.parametrize("fold", FOLDS)
def test_folded_sink_is_fed_through_top_entry(fold: int) -> None:
    """The fold relocates the sink so it is fed through a TOP entry port.

    Guards that the parametrised folds actually exercise the merge-into-top-entry
    geometry rather than silently passing on a layout where the sink stays a
    side entry (which would make the corner assertions vacuous).
    """
    graph, _routes, _offsets = _route_at_fold(fold)
    sink_top_entries = [
        pid
        for pid, port in graph.ports.items()
        if port.section_id == "sink" and port.is_entry and "entry_top" in pid
    ]
    assert sink_top_entries, f"fold {fold}: sink not fed through a TOP entry"


@pytest.mark.parametrize("fold", FOLDS)
def test_folded_sink_render_passes_curve_self_check(fold: int) -> None:
    """The render-time curve self-check passes on the folded layout.

    ``assert_render_curve_invariants`` is the backstop that aborts the render
    when a bundle curve is defective; it must not fire for the folded sink's
    merge corner.
    """
    graph, routes, offsets = _route_at_fold(fold)
    assert_render_curve_invariants(graph, routes, offsets)
