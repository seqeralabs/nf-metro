"""LEFT-exit -> RIGHT-entry staircase into a folded side-stack (#1143).

When ``fold_threshold`` relocates a downstream section to the left of its
feeder, an inter-section bundle leaves the feeder's LEFT exit port and steps
west -> down -> west into the relocated section's RIGHT entry port (an H-V-H
staircase with two opposite-handed corners).

Two things must hold for that staircase, both of which the unbounded layout
gets for free but a tightened fold broke:

* **Bundle order is preserved across the bend.** The exit and entry ports must
  stack the bundle in the same order, so the descent does not have to permute
  the lines and no line crosses a bundle-mate through a corner.  The order is
  set upstream: a reconvergence section fed by a single multi-line feeder whose
  lines originate at *separate* single-line producers has no well-defined
  delivered order (the producers each sit on a local slot 0, so two lines
  collide on one offset), and settling the section on that ambiguous order
  desynchronises its exit port from the relocated section's entry port.

* **The corners are concentric.** The rigid bundle rides one concentric fan, so
  each corner is sized wholesale rather than per line.

``fold_left_exit_right_entry`` is the committed minimal fixture (its
``fold_threshold`` directive bakes the relocation in).  ``epitopeprediction``
at fold 7 -- a real nf-core pipeline whose Reporting section relocates -- is the
motivating case.
"""

from __future__ import annotations

from nf_metro import api
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.layout.routing.invariants import (
    assert_render_curve_invariants,
    check_bundle_order_preserved,
    check_concentric_bundle_corners,
    check_seam_segments_meet_at_port,
)

FIXTURE = "examples/topologies/fold_left_exit_right_entry.mmd"


def _route(text: str):
    graph = api.prepare_graph(text)
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    return graph, routes, offsets


def _route_fixture():
    return _route(open(FIXTURE).read())


def test_staircase_bundle_order_preserved() -> None:
    _graph, routes, _offsets = _route_fixture()
    flips = check_bundle_order_preserved(routes)
    assert not flips, "\n".join(v.message() for v in flips)


def test_staircase_corners_concentric() -> None:
    graph, routes, offsets = _route_fixture()
    pinches = check_concentric_bundle_corners(graph, routes, offsets)
    assert not pinches, "\n".join(v.message() for v in pinches)


def test_staircase_seams_meet_at_port() -> None:
    graph, routes, offsets = _route_fixture()
    gaps = check_seam_segments_meet_at_port(graph, routes, offsets)
    assert not gaps, "\n".join(g.message() for g in gaps)


def test_staircase_render_passes_curve_self_check() -> None:
    graph, routes, offsets = _route_fixture()
    assert_render_curve_invariants(graph, routes, offsets)


def test_staircase_is_a_cross_row_left_exit_right_entry() -> None:
    """The fold actually produces the staircase the other assertions guard.

    Guards that the committed fixture still relocates ``report`` to a lower row
    reached from ``middle``'s LEFT exit, so the corner assertions are not
    vacuously satisfied by a same-row straight connector.
    """
    graph, _routes, _offsets = _route_fixture()
    exit_port = graph.stations.get("middle__exit_left_1")
    entry_port = graph.stations.get("report__entry_right_3")
    assert exit_port is not None and entry_port is not None
    assert entry_port.y > exit_port.y, "report did not relocate below middle's exit"


def test_motivating_epitopeprediction_staircase_is_clean() -> None:
    """The real motivating case: ``epitopeprediction`` at the fold that relocates
    one of its three sections renders its merge -> reporting staircase without a
    bundle-order flip or a non-concentric corner."""
    graph = api.prepare_graph(
        open("examples/epitopeprediction.mmd").read(),
        layout_options={"fold_threshold": 7},
    )
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)

    assert_render_curve_invariants(graph, routes, offsets)

    lines = ("vcf", "protein", "peptide")

    def order(port: str) -> list[str]:
        return sorted(lines, key=lambda lid: offsets[(port, lid)])

    assert order("binding_prediction__exit_left_1") == order(
        "reporting__entry_right_3"
    ), "exit and entry ports must stack the staircase bundle in the same order"
