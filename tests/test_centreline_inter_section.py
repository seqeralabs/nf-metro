"""Inter-section simple-shape routes are built via the centreline templates.

The straight, L-shape, single-corner and vertical-drop inter-section handlers
construct their routes by describing a centreline and fanning it with
``build_concentric_bundle`` (``layout/routing/centrelines.py``) rather than
assembling per-line ``points`` / ``curve_radii`` by hand.  A bundle built that
way is offset-baked (:attr:`OffsetRegime.BAKED`) and correct by construction --
its corners stay concentric and its lines keep a constant side-of-travel order.

These tests pin that on the fixtures that exercise each shape: a regression to
a hand-rolled, render-time-offset path would leave the multi-line inter-section
bundles :attr:`OffsetRegime.DEFERRED`, and a flat or mis-signed radius would
trip the render-path curve guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import (
    OffsetRegime,
    compute_station_offsets,
    route_edges,
)
from nf_metro.layout.routing.invariants import (
    assert_render_curve_invariants,
    check_bundle_order_preserved,
    check_concentric_bundle_corners,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

# Fixtures whose inter-section routing exercises the simple shapes migrated
# onto the centreline templates: straight runs, plain L-shapes, the bottom-exit
# junction corner, and TB perpendicular / bottom-exit drops.
MIGRATED_SHAPE_FIXTURES = [
    EXAMPLES / "topologies" / "asymmetric_tree.mmd",
    EXAMPLES / "topologies" / "complex_multipath.mmd",
    EXAMPLES / "topologies" / "fold_stacked_branch.mmd",
    EXAMPLES / "rnaseq_sections.mmd",
    EXAMPLES / "variantbenchmarking_auto.mmd",
]


def _route(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, offsets, routes


@pytest.mark.parametrize("path", MIGRATED_SHAPE_FIXTURES, ids=lambda p: p.stem)
def test_inter_section_bundles_are_concentric_and_unflipped(path: Path) -> None:
    graph, offsets, routes = _route(path)
    assert check_concentric_bundle_corners(graph, routes, offsets) == []
    assert check_bundle_order_preserved(routes) == []
    # The render path's always-on guard must accept the laid-out routes.
    assert_render_curve_invariants(graph, routes, offsets)


@pytest.mark.parametrize("path", MIGRATED_SHAPE_FIXTURES, ids=lambda p: p.stem)
def test_multi_line_inter_section_bundles_are_offset_baked(path: Path) -> None:
    """A multi-line inter-section bundle routed via a centreline bakes offsets.

    The centreline templates emit ``offsets_applied`` routes (the per-line
    offset is in the points, not deferred to the renderer's heuristic).  A
    revert to a hand-rolled, render-time-offset L-shape would leave the
    multi-line inter-section bundles un-baked and red this assertion.
    """
    _graph, _offsets, routes = _route(path)
    bundles: dict[tuple[str, str], list] = {}
    for r in routes:
        if r.is_inter_section:
            bundles.setdefault((r.edge.source, r.edge.target), []).append(r)
    multiline = {k: v for k, v in bundles.items() if len({r.line_id for r in v}) > 1}
    assert multiline, f"{path.stem}: expected a multi-line inter-section bundle"
    assert all(
        r.offset_regime is OffsetRegime.BAKED for rs in multiline.values() for r in rs
    )
