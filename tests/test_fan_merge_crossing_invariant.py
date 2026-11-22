"""Tests for the vertical-flow fork/merge partition-crossing invariant.

A vertical-flow (TB/BT) section is the horizontal model rotated 90 degrees, so a
rotation must preserve the horizontal layout's non-crossing.  At a fork or merge,
two distinct lines that each reach exactly one in-section neighbour must leave or
dock in those neighbours' lane (column) order; when the lane sign transposes that
order the two routes cross between the station and where they peel apart.  The
bundle-mate guard cannot see it -- the crossing lines ride different edges -- and
the fan-out-junction guard skips it, since these forks/merges have no junction.

Covers:

* Happy-path: every gallery example and topology fixture routes its
  vertical-flow forks/merges without a partition crossing.
* The seam-free regression trio (``tb_convergence_straight_drop``,
  ``tb_passthrough_continuation``, ``tb_trunk_through_fan``) is crossing-free.
* Meaningfulness: with the seam-free positive-fan classification and the fan-in
  lane-side seating both disabled the checker fires on all three, so the invariant
  genuinely encodes the bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nf_metro.layout.phases.single_section as single_section
import nf_metro.layout.routing.reversal as routing_reversal
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import check_fan_merge_no_partition_crossing
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
EXAMPLE_TOPOLOGIES = EXAMPLES / "topologies"
FIXTURE_TOPOLOGIES = REPO_ROOT / "tests" / "fixtures" / "topologies"

SEAM_FREE_TRIO = (
    "tb_convergence_straight_drop",
    "tb_passthrough_continuation",
    "tb_trunk_through_fan",
)


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
def test_no_fan_merge_partition_crossing_in_gallery(path: Path) -> None:
    """Every shipped example and topology routes its vertical-flow forks and
    merges so partitioning lines stay clear of one another."""
    graph, routes, offsets = _route(path)
    violations = check_fan_merge_no_partition_crossing(graph, routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)


@pytest.mark.parametrize("name", SEAM_FREE_TRIO)
def test_seam_free_trio_is_crossing_free(name: str) -> None:
    """The seam-free TB forks/merges draw their bundle on the side their
    neighbours sit, so no partitioning pair transposes its lane."""
    graph, routes, offsets = _route(EXAMPLE_TOPOLOGIES / f"{name}.mmd")
    violations = check_fan_merge_no_partition_crossing(graph, routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)


@pytest.mark.parametrize("name", SEAM_FREE_TRIO)
def test_checker_fires_without_seam_free_positive_fan(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forcing the seam-free sections back onto the default rotation lane sign
    transposes their fork/merge bundle, reproducing the crossing the invariant is
    meant to catch -- proving the check is not vacuous.  Fan-in seating also
    resolves the transposed sign, so it is disabled alongside."""
    monkeypatch.setattr(
        routing_reversal, "_is_seam_free_section", lambda section: False
    )
    monkeypatch.setattr(
        single_section, "_seat_fan_in_branches_on_lane_side", lambda *args: None
    )
    graph, routes, offsets = _route(EXAMPLE_TOPOLOGIES / f"{name}.mmd")
    violations = check_fan_merge_no_partition_crossing(graph, routes, offsets)
    assert violations, "expected a fork/merge crossing with the seam-free sign off"
