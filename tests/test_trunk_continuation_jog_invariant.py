"""Tests for the TB trunk-continuation straight-drop invariant.

In a TB fan-out where one line continues straight down the trunk column (sharing
the hub's X with its child) while a sibling peels off to another column, the
continuing line must drop straight.  The TB axis draws a line at its offset
*reversed* against a per-station bundle max; that max shrinks from the hub (two
lines) to the continuation's solo child (one line), so the continuing line's
drawn X changes across the edge and the router emits a one-step diagonal jog
instead of a straight drop (issue #929).  The LR mirror of the same shape keeps
the continuing line straight because LR does not reverse offsets.

The same shape arises at a fan-in: a feeder whose source is collinear with a
section's terminal merge should drop straight while a sibling arrives diagonally
from another column.  The merge's per-station bundle max is larger than the solo
feeder's, so the collinear feeder is again drawn off its lane unless re-slotted
(the convergence mirror, ``_slot_convergence_continuation_lines``).

Covers:

* Happy-path: every gallery example and topology fixture (including
  ``tb_trunk_through_fan`` and ``tb_convergence_straight_drop``, the reported
  defects) routes every same-lane continuation edge as a straight run.
* Meaningfulness: with the fan-out and fan-in continuation slotting each
  disabled in turn, the checker fires on the matching fixture, so the invariant
  genuinely encodes both bugs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nf_metro.layout.routing.offsets as routing_offsets
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import (
    check_trunk_continuation_drops_straight,
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
def test_no_trunk_continuation_jog_in_gallery(path: Path) -> None:
    """Every shipped example and topology routes a same-lane continuation edge
    as a straight run, never a one-step diagonal jog off the trunk."""
    graph, routes, offsets = _route(path)
    violations = check_trunk_continuation_drops_straight(graph, routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)


def test_checker_fires_without_continuation_slotting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling the trunk-continuation slotting reproduces the off-lane jog the
    invariant is meant to catch, proving the check is not vacuous."""
    monkeypatch.setattr(
        routing_offsets, "_slot_trunk_continuation_lines", lambda ctx: None
    )
    graph, routes, offsets = _route(EXAMPLE_TOPOLOGIES / "tb_trunk_through_fan.mmd")
    violations = check_trunk_continuation_drops_straight(graph, routes, offsets)
    assert violations, "expected a trunk-continuation jog with the slotting off"


def test_checker_fires_without_convergence_slotting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling the convergence slotting reproduces the collinear-feeder jog at
    a terminal merge, proving the fan-in arm of the invariant is not vacuous."""
    monkeypatch.setattr(
        routing_offsets, "_slot_convergence_continuation_lines", lambda ctx: None
    )
    graph, routes, offsets = _route(
        EXAMPLE_TOPOLOGIES / "tb_convergence_straight_drop.mmd"
    )
    violations = check_trunk_continuation_drops_straight(graph, routes, offsets)
    assert violations, "expected a convergence-continuation jog with the slotting off"
