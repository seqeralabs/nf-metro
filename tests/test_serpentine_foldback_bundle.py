"""Serpentine fold-back return bundle stays ordered and concentric (#972).

Folding the shipped ``rnaseq_auto`` example to a serpentine (any
``--fold-threshold`` low enough to wrap it, i.e. ``<= 10``) sends the
``star_salmon`` + ``star_rsem`` bundle out of ``genome_align`` on its LEFT
edge, down the inter-row gap, and into ``postprocessing`` from the RIGHT on a
lower row.  This is the same exit-left -> lower-right-entry step #671 fixed for
the synthetic ``tb_left_exit_step`` fixture: the bundle must descend as a
parallel staircase that preserves feed order at both ports and sizes every
corner concentrically.

Before the #671 staircase handler, the corner builder flipped the two lines on
the outgoing run and sized the bend with a hand-signed radius, so
``assert_render_curve_invariants`` aborted the render with a
``CurveInvariantError`` (bundle-order flip + non-concentric corner).  This
fixture-free lock exercises the real shipped example across the documented
fold-threshold range so the crash on that knob cannot return silently.

See issue #972.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import apply_route_offsets
from nf_metro.layout.routing.invariants import (
    assert_render_curve_invariants,
    check_bundle_order_preserved,
    check_concentric_bundle_corners,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

FIXTURE = Path(__file__).parent.parent / "examples" / "rnaseq_auto.mmd"

EXIT_PORT = "genome_align__exit_left_1"
ENTRY_PORT = "postprocessing__entry_right_5"

# The issue documents the crash at every fold-threshold 1..10; default 15 does
# not fold the example so it never builds the serpentine return bundle.
FOLD_THRESHOLDS = [1, 2, 3, 6, 10]


def _laid_out(fold_threshold: int):
    graph = parse_metro_mermaid(FIXTURE.read_text(), max_station_columns=fold_threshold)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, offsets, routes


def _baked(route, offsets):
    if route.offset_regime.name == "DEFERRED":
        return apply_route_offsets(route, offsets)
    return route.points


@pytest.mark.parametrize("fold_threshold", FOLD_THRESHOLDS)
def test_serpentine_renders_without_curve_defect(fold_threshold):
    """The #972 lock: the folded ``rnaseq_auto`` return bundle has no flip/pinch.

    ``assert_render_curve_invariants`` aborts the render on a flipped or
    non-concentric bundle corner, so a clean pass across the documented
    fold-threshold range means the serpentine fold-back keeps every line in
    order through both turns.
    """
    graph, offsets, routes = _laid_out(fold_threshold)
    assert_render_curve_invariants(graph, routes, offsets)
    assert not check_bundle_order_preserved(routes)
    assert not check_concentric_bundle_corners(graph, routes, offsets)


@pytest.mark.parametrize("fold_threshold", FOLD_THRESHOLDS)
def test_foldback_bundle_keeps_feed_order_into_the_entry(fold_threshold):
    """The genome_align -> postprocessing bundle delivers its feed order intact.

    The lines reach the LEFT exit port in a vertical order; the staircase must
    deliver them to the RIGHT entry in that same order, so no line crosses a
    bundle-mate at the fold-back corner.
    """
    _graph, offsets, routes = _laid_out(fold_threshold)
    step = [
        r for r in routes if r.edge.source == EXIT_PORT and r.edge.target == ENTRY_PORT
    ]
    assert len(step) >= 2, "expected a multi-line serpentine return bundle"

    exit_y = {r.line_id: _baked(r, offsets)[0][1] for r in step}
    entry_y = {r.line_id: _baked(r, offsets)[-1][1] for r in step}
    by_exit = sorted(exit_y, key=lambda lid: exit_y[lid])
    by_entry = sorted(entry_y, key=lambda lid: entry_y[lid])
    assert by_exit == by_entry, (
        f"exit order {by_exit} must match entry order {by_entry}; "
        "a mismatch means the fold-back inverted the bundle"
    )
