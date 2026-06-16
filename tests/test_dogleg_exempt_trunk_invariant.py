"""Tests for the reluctant-unbundling dogleg-off-exempt-trunk invariant.

A non-exempt bypass trunk cleared off an ``normalize_exempt`` run of a
different line must land on the side that keeps the two parallel.  Cleared to
the wrong side the movable trunk's riser pierces the exempt run -- and the
exempt riser pierces the movable run -- so the two colours cross twice instead
of running as a tight parallel bundle (issue #702).

Covers:

* Happy-path: every gallery example and topology fixture (including
  ``dogleg_exempt_distinct``, the reported defect) routes without a doglegged
  trunk crossing the exempt run it bundles with.
* Meaningfulness: the checker flags the reported crossing geometry and clears
  the parallel one, so the invariant genuinely encodes the bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.invariants import (
    check_no_dogleg_crosses_exempt_trunk,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge

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
def test_no_dogleg_crosses_exempt_trunk_in_gallery(path: Path) -> None:
    """Every shipped example and topology clears a doglegged trunk to the side
    that keeps it parallel to the exempt run, never the side that crosses it."""
    graph, routes, offsets = _route(path)
    violations = check_no_dogleg_crosses_exempt_trunk(graph, routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)


# Geometry lifted from the reported ``dogleg_exempt_distinct`` render: a blue
# exempt ``wrap`` trunk runs leftward at y=196; the red ``byp`` trunk bundles
# with it in the same inter-row channel.  Below it (y=199) byp's left riser
# pierces wrap's run and wrap's right riser pierces byp's run -- two crossings;
# above it (y=193) byp clears wrap entirely.
_WRAP = [
    (400.0, 298.0),
    (416.0, 298.0),
    (416.0, 196.0),
    (14.0, 196.0),
    (14.0, 120.0),
    (30.0, 120.0),
]
_BYP_BELOW = [
    (190.0, 120.0),
    (209.0, 120.0),
    (209.0, 199.0),
    (419.0, 199.0),
    (419.0, 298.0),
    (450.0, 298.0),
]
_BYP_ABOVE = [
    (190.0, 120.0),
    (209.0, 120.0),
    (209.0, 193.0),
    (419.0, 193.0),
    (419.0, 298.0),
    (450.0, 298.0),
]


def _routes(byp_points: list[tuple[float, float]]) -> list[RoutedPath]:
    return [
        RoutedPath(
            edge=Edge("rs2", "lt1", "wrap"),
            line_id="wrap",
            points=_WRAP,
            is_inter_section=True,
            normalize_exempt=True,
        ),
        RoutedPath(
            edge=Edge("lt2", "bs1", "byp"),
            line_id="byp",
            points=byp_points,
            is_inter_section=True,
        ),
    ]


def test_checker_flags_crossing_dogleg() -> None:
    """The checker fires when the movable trunk sits on the crossing side."""
    violations = check_no_dogleg_crosses_exempt_trunk(None, _routes(_BYP_BELOW), {})
    assert violations, "expected a dogleg crossing when byp runs below wrap"
    assert violations[0].line_id == "byp"
    assert violations[0].exempt_line == "wrap"


def test_checker_passes_parallel_dogleg() -> None:
    """The checker stays silent when the trunk clears to the parallel side."""
    violations = check_no_dogleg_crosses_exempt_trunk(None, _routes(_BYP_ABOVE), {})
    assert not violations, "parallel bundle above the exempt run must not flag"
