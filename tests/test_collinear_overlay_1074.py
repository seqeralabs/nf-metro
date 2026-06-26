"""Distinct lines occupy distinct channels in every multi-line bundle (#1074).

Two co-travelling lines that share a bundle must keep parallel slots on every
axis-aligned leg; collapsing them onto one channel draws one line on top of the
other, which the always-on :func:`assert_render_curve_invariants` treats as a
render-aborting defect.

Two routing paths can collapse a bundle:

* a TB BOTTOM exit whose target X is offset from the exit X routes a
  drop-jog-drop, and the horizontal jog leg must fan per line rather than ride
  one shared channel (``tb_bottom_exit_bundle_jog``); and
* a TB LEFT/RIGHT exit port fed by more than one station must give every line a
  distinct exit slot, even when two feeders carry the same internal offset
  (``tb_right_exit_feeder_slots``).

The bottom-exit repro is a clean gallery topology.  The feeder-slot repro needs
a section fed by two stations on stacked LEFT entries, a shape the topology
gallery's seam invariant does not yet model cleanly, so it lives under
``tests/fixtures`` rather than ``examples/topologies``.  The remaining fixtures
are gallery topologies that already exercise both handlers, so the invariant
generalises beyond the repros.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.layout.routing.invariants import (
    check_intra_section_collinear_distinct_lines,
    check_no_collinear_distinct_lines,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = ROOT / "examples" / "topologies"

FIXTURES = [
    TOPOLOGIES / "tb_bottom_exit_bundle_jog.mmd",
    ROOT / "tests" / "fixtures" / "tb_right_exit_feeder_slots.mmd",
    TOPOLOGIES / "fold_double.mmd",
    TOPOLOGIES / "tb_trunk_through_fan.mmd",
    TOPOLOGIES / "wide_fan_in.mmd",
]
IDS = [p.stem for p in FIXTURES]


def _routes(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    return graph, route_edges_centred(graph, station_offsets=offsets), offsets


@pytest.mark.parametrize("path", FIXTURES, ids=IDS)
def test_no_inter_section_collinear_overlay(path: Path) -> None:
    graph, routes, offsets = _routes(path)
    violations = check_no_collinear_distinct_lines(graph, routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)


@pytest.mark.parametrize("path", FIXTURES, ids=IDS)
def test_no_intra_section_collinear_overlay(path: Path) -> None:
    graph, routes, offsets = _routes(path)
    violations = check_intra_section_collinear_distinct_lines(graph, routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)
