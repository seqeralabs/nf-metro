"""Folded LEFT-exit -> RIGHT-entry run into a relocated side-stack.

When ``fold_threshold`` relocates a downstream section to a row below its
feeder, an inter-section bundle leaves the feeder's LEFT exit port and runs
west into the relocated section's RIGHT entry port.  Two things must hold:

* **Bundle order is preserved across the run.** The exit and entry ports stack
  the bundle in the same order, so no line crosses a bundle-mate.  The order is
  set upstream: a reconvergence section fed by a single multi-line feeder whose
  lines originate at *separate* single-line producers has no well-defined
  delivered order (the producers each sit on a local slot 0, so two lines
  collide on one offset), and settling the section on that ambiguous order
  desynchronises its exit port from the relocated section's entry port.

* **The exit aligns to the target's settled entry Y.** When the relocated
  target spans several sub-rows its entry Y keeps descending as those sub-rows
  settle, after the exit was first aligned to it.  The exit follows the entry
  down so the inter-section run stays straight rather than ending a sub-row
  above it (a jog).

``fold_left_exit_right_entry`` is the committed minimal fixture (its
``fold_threshold`` directive bakes the relocation in).  ``epitopeprediction``
at fold 7 -- a real nf-core pipeline whose Reporting section relocates below a
multi-sub-row Binding Prediction -- is the motivating case.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro import api
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.layout.routing.invariants import (
    assert_render_curve_invariants,
    check_bundle_order_preserved,
    check_seam_segments_meet_at_port,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "examples/topologies/fold_left_exit_right_entry.mmd"
EPITOPEPREDICTION = REPO_ROOT / "examples/epitopeprediction.mmd"


def _route(path: Path):
    graph = api.prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    return graph, routes, offsets


def _route_fixture():
    return _route(FIXTURE)


def test_bundle_order_preserved() -> None:
    _graph, routes, _offsets = _route_fixture()
    flips = check_bundle_order_preserved(routes)
    assert not flips, "\n".join(v.message() for v in flips)


def test_seams_meet_at_port() -> None:
    graph, routes, offsets = _route_fixture()
    gaps = check_seam_segments_meet_at_port(graph, routes, offsets)
    assert not gaps, "\n".join(g.message() for g in gaps)


def test_render_passes_curve_self_check() -> None:
    graph, routes, offsets = _route_fixture()
    assert_render_curve_invariants(graph, routes, offsets)


def test_exit_aligns_to_relocated_target_entry() -> None:
    """The fold relocates ``report`` to a lower row and the exit follows it.

    ``report`` sits in a row below ``middle`` (so the alignment is not
    vacuously satisfied by a same-row connector), and the LEFT exit ends at the
    RIGHT entry's settled Y so the run is straight.
    """
    graph, _routes, _offsets = _route_fixture()
    exit_port = graph.stations.get("middle__exit_left_1")
    entry_port = graph.stations.get("report__entry_right_3")
    assert exit_port is not None and entry_port is not None
    assert graph.sections["report"].grid_row > graph.sections["middle"].grid_row, (
        "report did not relocate to a lower row"
    )
    assert abs(entry_port.y - exit_port.y) < 1.0, (
        f"exit y={exit_port.y:.1f} != entry y={entry_port.y:.1f}: the "
        f"inter-section run is not straight"
    )


def test_motivating_epitopeprediction_is_clean() -> None:
    """The real motivating case: ``epitopeprediction`` at the fold that relocates
    one of its three sections renders its binding_prediction -> reporting run
    straight, in bundle order, with no curve defect."""
    graph = api.prepare_graph(
        EPITOPEPREDICTION.read_text(),
        layout_options={"fold_threshold": 7},
        source_dir=str(EPITOPEPREDICTION.parent),
    )
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)

    assert_render_curve_invariants(graph, routes, offsets)

    lines = ("vcf", "protein", "peptide")

    def order(port: str) -> list[str]:
        return sorted(lines, key=lambda lid: offsets[(port, lid)])

    assert order("binding_prediction__exit_left_1") == order(
        "reporting__entry_right_3"
    ), "exit and entry ports must stack the bundle in the same order"

    exit_port = graph.stations["binding_prediction__exit_left_1"]
    entry_port = graph.stations["reporting__entry_right_3"]
    assert abs(entry_port.y - exit_port.y) < 1.0, (
        f"exit y={exit_port.y:.1f} != entry y={entry_port.y:.1f}: the "
        f"binding_prediction -> reporting run is not straight"
    )


# (fixture text, layout options, exit-section id, target-section id) for each
# folded LEFT-exit -> RIGHT-entry straight run whose two sections should clear
# the connector by the same distance.
_STRAIGHT_RUN_PAIRS = [
    pytest.param(
        FIXTURE,
        None,
        "middle",
        "report",
        id="fold_left_exit_right_entry",
    ),
    pytest.param(
        EPITOPEPREDICTION,
        {"fold_threshold": 7},
        "binding_prediction",
        "reporting",
        id="epitopeprediction",
    ),
]


@pytest.mark.parametrize("path, opts, exit_sec, target_sec", _STRAIGHT_RUN_PAIRS)
def test_straight_run_sections_share_bbox_bottom(
    path: Path, opts: dict | None, exit_sec: str, target_sec: str
) -> None:
    """The two sections joined by the straight run clear it by the same amount.

    A folded TB section's LEFT/RIGHT exit runs straight west into a relocated
    target's RIGHT/LEFT entry.  If the two sections' bbox bottoms differ, the
    connector sits a different distance above each section's bottom edge and
    reads as lopsided even though the run itself is straight.  Their bottoms
    must line up so the clearance is balanced.
    """
    graph = api.prepare_graph(
        path.read_text(), layout_options=opts, source_dir=str(path.parent)
    )
    a = graph.sections[exit_sec]
    b = graph.sections[target_sec]
    assert b.grid_row != a.grid_row, (
        f"{target_sec} did not relocate to a different row from {exit_sec}; "
        "the cross-row bottom-alignment case is not exercised"
    )
    a_bottom = a.bbox_y + a.bbox_h
    b_bottom = b.bbox_y + b.bbox_h
    assert abs(a_bottom - b_bottom) < 1.0, (
        f"{exit_sec} bottom={a_bottom:.1f} != {target_sec} bottom={b_bottom:.1f}: "
        f"the straight inter-section run clears the two sections by different "
        f"distances (delta {abs(a_bottom - b_bottom):.1f}px)"
    )
