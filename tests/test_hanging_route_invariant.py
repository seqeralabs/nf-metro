"""Tests for the general hanging-route invariant.

A routed path that ends in mid-air -- disconnected from any station, port,
junction, terminus icon, or the route it should join -- is among the worst
final-render defects.  Family-specific guards catch it for merge feeders
(:func:`check_merge_branches_meet_trunk`) and rail stubs, but
:func:`check_no_hanging_routes` is the always-on backstop that asserts the
universal property underneath all of them, over every route, regardless of
which handler produced it.

Covers:

* Happy-path: every shipped example and topology fixture routes with no
  endpoint hanging in open space.
* Always-on: a planted hang aborts the render path through
  :func:`assert_render_curve_invariants`, independent of ``validate``.
* Meaningfulness: planting a hanging tail on an ordinary intra-section route
  (outside the merge and rail families) makes the check fire, and the
  unmutated routing does not -- so the check is not vacuous.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import (
    CurveInvariantError,
    assert_render_curve_invariants,
    check_no_hanging_routes,
)
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
def test_no_hanging_routes_in_gallery(path: Path) -> None:
    """Every shipped fixture routes with no endpoint hanging in open space."""
    graph, routes, offsets = _route(path)
    violations = check_no_hanging_routes(graph, routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)


def _plant_hanging_tail(graph, routes):
    """Replace an ordinary (non-rail, non-merge) route's target endpoint with a
    point far out in open space, returning the index of the mutated route.

    Picks a route whose target is a real station rather than a merge junction so
    the planted hang is outside the merge and rail families the dedicated guards
    already cover.
    """
    for i, r in enumerate(routes):
        if graph.station_is_rail(r.edge.source) or graph.station_is_rail(r.edge.target):
            continue
        if r.edge.target in graph.junction_ids:
            continue
        if len(r.points) < 2:
            continue
        far = (r.points[-1][0] + 1000.0, r.points[-1][1] + 1000.0)
        routes[i] = dataclasses.replace(
            r, points=[*r.points[:-1], far], offsets_applied=True
        )
        return i
    raise AssertionError("no ordinary route available to plant a hanging tail")


def test_planted_hanging_tail_is_caught() -> None:
    """Shoving an ordinary route's endpoint into open space makes the check fire,
    naming that route -- and the unmutated routing does not.  Proves the check
    is neither vacuous nor a false-positive on the clean render."""
    graph, routes, offsets = _route(FIXTURES / "rnaseq_sections.mmd")
    assert not check_no_hanging_routes(graph, routes, offsets)

    idx = _plant_hanging_tail(graph, routes)
    planted = routes[idx]
    violations = check_no_hanging_routes(graph, routes, offsets)
    assert any(
        v.source == planted.edge.source
        and v.target == planted.edge.target
        and v.which == "target"
        for v in violations
    ), "expected the planted hanging tail to be reported"


def test_planted_hanging_tail_aborts_render_path() -> None:
    """The guard runs on the always-on render path: a planted hang raises
    ``CurveInvariantError`` even though ``compute_layout``'s ``validate`` block
    is not involved."""
    graph, routes, offsets = _route(FIXTURES / "rnaseq_sections.mmd")
    assert_render_curve_invariants(graph, routes, offsets)  # clean: no raise

    _plant_hanging_tail(graph, routes)
    with pytest.raises(CurveInvariantError, match="hanging in open space"):
        assert_render_curve_invariants(graph, routes, offsets)
