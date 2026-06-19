"""Tests for the same-line parallel-run invariant.

A single metro line that fans out from one source to several targets (or
converges on one port from several feeds) must travel the span its branches
share as ONE trunk, splitting only where each branch turns off.  When the
branches instead descend in adjacent offset slots they render as two parallel
same-colour tracks that read as two distinct routes.

Covers:

* Happy-path: every gallery fixture and example routes with no same-line
  parallel descent.
* Targeted: ``variantbenchmarking`` / ``variantbenchmarking_auto`` (the
  reported defect) route their ``test`` and ``truth`` fans as single trunks.
* Meaningfulness: with the merge passes disabled the checker fires on the
  reported fixtures, so the invariant genuinely encodes the bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nf_metro.layout.routing.core as routing_core
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import (
    check_no_same_line_parallel_descents,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = REPO_ROOT / "tests" / "fixtures" / "topologies"
EXAMPLES = REPO_ROOT / "examples"


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    paths.extend(sorted(TOPOLOGIES.glob("*.mmd")))
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
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
def test_no_same_line_parallel_descents_in_gallery(path: Path) -> None:
    """Every shipped topology and example routes without a same-line line
    descending as two parallel adjacent tracks."""
    graph, routes, offsets = _route(path)
    violations = check_no_same_line_parallel_descents(graph, routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)


@pytest.mark.parametrize(
    "fixture",
    ["variantbenchmarking.mmd", "variantbenchmarking_auto.mmd"],
)
def test_variantbenchmarking_fans_are_single_trunks(fixture: str) -> None:
    """The reported fan-out/fan-in duplications route as single trunks."""
    graph, routes, offsets = _route(EXAMPLES / fixture)
    violations = check_no_same_line_parallel_descents(graph, routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)


@pytest.mark.parametrize(
    "fixture",
    ["variantbenchmarking.mmd", "variantbenchmarking_auto.mmd"],
)
def test_checker_fires_without_coincidence_pass(
    fixture: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabling same-line track coincidence reproduces the doubled descents the
    invariant is meant to catch, proving the check is not vacuous."""
    monkeypatch.setattr(
        routing_core, "_coincide_same_line_tracks", lambda routes, ctx: None
    )
    graph, routes, offsets = _route(EXAMPLES / fixture)
    violations = check_no_same_line_parallel_descents(graph, routes, offsets)
    assert violations, "expected doubled same-line descents with merge passes off"
