"""TOP-entry staircase routing: centreline-built, concentric, non-flipping.

The TOP-entry staircase handler (``_route_top_entry_l_shape``) and its former
multi-line variant are folded onto the centreline builder
(:func:`build_tapered_bundle`): the handler describes the staircase centreline
and the builder fans every co-travelling line as a parallel offset with
geometry-derived corner radii.  This locks the properties that follow from
that construction, across every fixture whose routing reaches the handler:

* every wholesale-translated bundle corner is concentric (no hand-signed
  radius that nests non-concentrically and pinches the bundle);
* co-travelling lines keep a constant side-of-travel order (no flip/crossing);
* the always-on render-path guard accepts the routes;
* each staircase route bakes its per-line offset
  (:attr:`OffsetRegime.BAKED`), the signature of a builder-fanned bundle
  rather than a renderer-offset one.

See issue #793.
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

# Fixtures whose routing reaches the TOP-entry staircase handler.
_FIXTURES = [
    "examples/topologies/cross_col_top_entry.mmd",
    "examples/topologies/cross_row_gap_wrap.mmd",
    "examples/topologies/lr_to_tb_top_cross_col.mmd",
    "examples/topologies/lr_to_tb_top_near_vertical.mmd",
    "examples/topologies/lr_to_tb_top_two_lines.mmd",
    "examples/topologies/merge_trunk_out_of_range_section.mmd",
    "examples/variantbenchmarking.mmd",
]


def _routed(rel: str):
    graph = parse_metro_mermaid((REPO_ROOT / rel).read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    return graph, offsets, route_edges(graph, station_offsets=offsets)


def _top_entry_routes(graph, routes):
    out = []
    for rp in routes:
        port = graph.ports.get(rp.edge.target)
        if (
            rp.is_inter_section
            and port is not None
            and port.is_entry
            and port.side is not None
            and port.side.name == "TOP"
        ):
            out.append(rp)
    return out


@pytest.mark.parametrize("rel", _FIXTURES, ids=lambda p: Path(p).name)
def test_top_entry_staircase_is_concentric_and_unflipped(rel: str) -> None:
    graph, offsets, routes = _routed(rel)
    assert check_concentric_bundle_corners(graph, routes, offsets) == []
    assert check_bundle_order_preserved(routes) == []
    assert_render_curve_invariants(graph, routes, offsets)


@pytest.mark.parametrize("rel", _FIXTURES, ids=lambda p: Path(p).name)
def test_top_entry_routes_are_builder_fanned(rel: str) -> None:
    """Each staircase route bakes its offset, as a centreline-built bundle does.

    A bundle fanned by the centreline builder bakes each line's per-line offset
    into its points (:attr:`OffsetRegime.BAKED`); a route that defers the offset
    to the renderer stays :attr:`OffsetRegime.DEFERRED`.
    """
    graph, _offsets, routes = _routed(rel)
    staircases = _top_entry_routes(graph, routes)
    assert staircases, f"{rel}: expected at least one TOP-entry route"
    for rp in staircases:
        assert rp.offset_regime is OffsetRegime.BAKED, (
            f"{rel}: {rp.line_id} {rp.edge.source}->{rp.edge.target} "
            "TOP-entry route did not bake its offset (not builder-fanned)"
        )
