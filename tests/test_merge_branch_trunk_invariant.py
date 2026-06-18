"""Tests for the merge-branch-meets-trunk invariant.

At a reconvergence merge (several feeders converging on one entry port), the
non-trunk feeders ("branches") descend to the trunk's bypass channel and turn
into it.  The branch drop level is published by the routing context
(``trunk_by``); the trunk route computes its channel Y independently.  When the
two disagree -- notably when the trunk forces ``cross_row`` to route below
every section but the context did not -- the branches land at a different Y
from where the trunk actually runs and end as stubs hanging in open space.

Covers:

* Happy-path: every shipped example and topology fixture routes with every
  merge feeder connected to its trunk.
* Targeted: ``genomeassembly_organellar`` (the reported defect) routes its
  ``assemblies`` line as connected strokes reaching ``asmstats``.
* Meaningfulness: reverting the context's ``cross_row`` decision reproduces
  the hanging branches, so the invariant genuinely encodes the bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nf_metro.layout.routing.context as routing_context
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import check_merge_branches_meet_trunk
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
FIXTURES = REPO_ROOT / "tests" / "fixtures"


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
    paths.extend(sorted((EXAMPLES / "topologies").glob("*.mmd")))
    paths.extend(sorted((EXAMPLES / "guide").glob("*.mmd")))
    paths.extend(sorted(FIXTURES.glob("*.mmd")))
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
def test_merge_branches_meet_trunk_in_gallery(path: Path) -> None:
    """Every shipped fixture routes with no merge feeder hanging in open space."""
    graph, routes, offsets = _route(path)
    violations = check_merge_branches_meet_trunk(graph, routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)


def test_genomeassembly_organellar_assemblies_connected() -> None:
    """The reported fixture's converging ``assemblies`` feeders all join the
    trunk rather than ending as stubs short of it."""
    graph, routes, offsets = _route(FIXTURES / "genomeassembly_organellar.mmd")
    violations = check_merge_branches_meet_trunk(graph, routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)


def test_checker_fires_when_context_ignores_cross_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reverting the context's ``cross_row`` decision desyncs the branch drop
    level from the trunk channel, reproducing the hanging stubs the invariant
    is meant to catch.  Proves the check is not vacuous."""
    monkeypatch.setattr(
        routing_context,
        "has_other_row_section_in_col_range",
        lambda *args, **kwargs: False,
    )
    graph, routes, offsets = _route(FIXTURES / "genomeassembly_organellar.mmd")
    violations = check_merge_branches_meet_trunk(graph, routes, offsets)
    assert violations, "expected hanging branches when context ignores cross_row"
