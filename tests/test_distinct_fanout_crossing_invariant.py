"""Tests for the distinct-line fan-out divergence invariant.

When distinct lines leave one section through a shared exit junction and drop to
different columns on another row, they should descend as one concentric bundle
and split only where each line peels into its target.  The near line peeling
onto the wrong side of the descent, or a lead-in Y order out of phase with the
descent X order, makes the two colours cross in open space instead of running
parallel (issue #719).

Covers:

* Happy-path: every gallery example and topology fixture (including
  ``dogleg_twoline_fanout``, the reported defect, and the upward variant)
  routes its clean-divergence fan-outs without a crossing.
* Meaningfulness: with the source-section divergence reorder disabled the
  checker fires on the reported fixture, so the invariant genuinely encodes the
  bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nf_metro.layout.routing.offsets as routing_offsets
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import (
    check_no_distinct_line_fanout_crossing,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
EXAMPLE_TOPOLOGIES = EXAMPLES / "topologies"
FIXTURE_TOPOLOGIES = REPO_ROOT / "tests" / "fixtures" / "topologies"


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
    paths.extend(sorted(EXAMPLE_TOPOLOGIES.glob("*.mmd")))
    paths.extend(sorted(FIXTURE_TOPOLOGIES.glob("*.mmd")))
    return paths


def _route(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, routes, offsets


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_no_distinct_line_fanout_crossing_in_gallery(path: Path) -> None:
    """Every shipped example and topology routes its clean-divergence fan-outs
    as one bundle that splits only at each line's peel column."""
    graph, routes, offsets = _route(path)
    violations = check_no_distinct_line_fanout_crossing(graph, routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)


def test_checker_fires_without_divergence_reorder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling the source-section divergence reorder puts the lead-in Y order
    out of phase with the descent X order, reproducing the crossing the
    invariant is meant to catch -- proving the check is not vacuous."""
    monkeypatch.setattr(routing_offsets, "_reorder_fanout_divergence", lambda ctx: None)
    graph, routes, offsets = _route(EXAMPLE_TOPOLOGIES / "dogleg_twoline_fanout.mmd")
    violations = check_no_distinct_line_fanout_crossing(graph, routes, offsets)
    assert violations, "expected a distinct fan-out crossing with the reorder off"
