"""Folding ``rnaseq_auto`` keeps inter-section flow forward (#972, #1080).

The shipped ``rnaseq_auto`` example has a side-branch section (``pseudo_align``,
a sink) sharing the ``genome_align`` topo column.  At any ``--fold-threshold``
low enough to wrap the chain, the serpentine packer must not fold that branch
column: doing so makes ``genome_align`` a TB bridge and strands its consumer
``postprocessing`` behind it, so the inter-section bundle reads backward and
leans on the #671/#972 fold-back staircase to recover.

Auto-layout instead folds at a spine link (``postprocessing``), so
``genome_align`` leads ``postprocessing`` in grid order and the bundle flows
forward across the whole documented fold-threshold range.  The fold-back
staircase routing itself stays locked by ``test_left_exit_right_entry_step``
and the ``fold_double`` / ``u_turn_fold`` topology fixtures.

See issues #972 and #1080.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import assert_render_curve_invariants
from nf_metro.parser.mermaid import parse_metro_mermaid

FIXTURE = Path(__file__).parent.parent / "examples" / "rnaseq_auto.mmd"

# The default threshold (15) does not fold the example; every lower value wraps
# it into a serpentine and so exercises the fold-point choice.
FOLD_THRESHOLDS = [1, 2, 3, 6, 10]


def _laid_out(fold_threshold: int):
    graph = parse_metro_mermaid(FIXTURE.read_text(), max_station_columns=fold_threshold)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, offsets, routes


@pytest.mark.parametrize("fold_threshold", FOLD_THRESHOLDS)
def test_fold_keeps_genome_ahead_of_postprocessing(fold_threshold):
    """``genome_align`` is never folded behind its consumer ``postprocessing``."""
    graph, _offsets, _routes = _laid_out(fold_threshold)
    genome = graph.sections["genome_align"]
    post = graph.sections["postprocessing"]
    assert genome.grid_col <= post.grid_col, (
        f"genome_align (col {genome.grid_col}) must not sit ahead of its "
        f"consumer postprocessing (col {post.grid_col}) at fold "
        f"threshold {fold_threshold}"
    )


@pytest.mark.parametrize("fold_threshold", FOLD_THRESHOLDS)
def test_folded_render_has_no_curve_defect(fold_threshold):
    """The folded layout draws without a flipped or non-concentric corner."""
    graph, offsets, routes = _laid_out(fold_threshold)
    assert_render_curve_invariants(graph, routes, offsets)
