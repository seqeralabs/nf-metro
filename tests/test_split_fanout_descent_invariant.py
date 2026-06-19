"""Tests for the reluctant-unbundling fan-out descent invariant.

A line that fans out from one source to several targets must descend as ONE
trunk over the span its branches share, splitting only where each branch turns
off.  When two same-line descents leaving one source overlap in their Y span
yet open at distinct Xs, the split has begun before either branch diverges and
the farther-reaching branch peels onto the inside of the nearer one, crossing
its descent (issue #702).

Covers:

* Happy-path: every gallery example and topology fixture (including
  ``divergent_fanout_split``, the reported defect) routes without a split
  same-line fan-out descent.
* Meaningfulness: with the fan-out fuse pass disabled the checker fires on the
  reported fixture, so the invariant genuinely encodes the bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nf_metro.layout.routing.core as routing_core
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import (
    check_no_split_same_line_fanout_descents,
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
def test_no_split_same_line_fanout_descents_in_gallery(path: Path) -> None:
    """Every shipped example and topology routes same-line fan-outs as one
    fused trunk, never as two Y-overlapping descents at distinct Xs."""
    graph, routes, offsets = _route(path)
    violations = check_no_split_same_line_fanout_descents(graph, routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)


def test_checker_fires_without_fuse_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabling the fan-out fuse pass reproduces the split descents the
    invariant is meant to catch, proving the check is not vacuous."""
    monkeypatch.setattr(
        routing_core, "_coincide_same_line_tracks", lambda routes, ctx: None
    )
    graph, routes, offsets = _route(EXAMPLE_TOPOLOGIES / "divergent_fanout_split.mmd")
    violations = check_no_split_same_line_fanout_descents(graph, routes, offsets)
    assert violations, "expected a split fan-out descent with the fuse pass off"
