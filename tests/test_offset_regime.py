"""Tests for the unified offset regime.

A routed path carries its parallel-line separation in one of two regimes
(:class:`OffsetRegime`): ``DEFERRED`` routes leave it to the renderer, which
shifts their endpoints in Y; ``BAKED`` routes already carry it in their points
(a TB X-stagger, a rail per-line Y, a bundle's concentric fan).  Mixing the two
is the §4.8 fragility: every spacing-aware pass must know which regime a route
is in.

Covers:

* ``apply_route_offsets`` is the single regime-aware applier: it shifts a
  deferred route's endpoints and returns a baked route's points verbatim.
* Happy-path: every shipped fixture's deferred routes apply their separation
  laterally (no deferred route carries an offset along a vertical terminal
  segment).
* Always-on + meaningfulness: a deferred route planted with a non-zero offset
  on a vertical terminal segment is caught by the check and aborts the render
  path, while the clean routing does not.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import (
    OffsetRegime,
    apply_route_offsets,
    compute_station_offsets,
    route_edges,
)
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.invariants import (
    CurveInvariantError,
    assert_render_curve_invariants,
    check_deferred_offsets_apply_laterally,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge, PermissiveGuardWarning

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


def _edge() -> Edge:
    return Edge(source="a", target="b", line_id="L")


def test_apply_route_offsets_shifts_deferred_endpoints() -> None:
    """A deferred route's endpoints shift by their station offset; interior
    points go to whichever end they are nearer."""
    route = RoutedPath(
        edge=_edge(),
        line_id="L",
        points=[(0.0, 0.0), (50.0, 0.0), (100.0, 0.0)],
        offset_regime=OffsetRegime.DEFERRED,
    )
    offsets = {("a", "L"): 5.0, ("b", "L"): -3.0}
    assert apply_route_offsets(route, offsets) == [
        (0.0, 5.0),
        (50.0, 5.0),  # nearer the source end
        (100.0, -3.0),
    ]


def test_apply_route_offsets_returns_baked_points_verbatim() -> None:
    """A baked route already carries its separation, so its points are
    returned verbatim and any offset entry is ignored."""
    points = [(0.0, 0.0), (0.0, 100.0)]
    route = RoutedPath(
        edge=_edge(),
        line_id="L",
        points=list(points),
        offset_regime=OffsetRegime.BAKED,
    )
    offsets = {("a", "L"): 5.0, ("b", "L"): -3.0}
    assert apply_route_offsets(route, offsets) == points


def test_default_regime_is_deferred() -> None:
    """A route with no regime declared defers its offset to render time."""
    assert (
        RoutedPath(
            edge=_edge(), line_id="L", points=[(0.0, 0.0), (1.0, 0.0)]
        ).offset_regime
        is OffsetRegime.DEFERRED
    )


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_deferred_offsets_apply_laterally_in_gallery(path: Path) -> None:
    """No shipped fixture defers an offset onto a vertical terminal segment."""
    _graph, routes, offsets = _route(path)
    violations = check_deferred_offsets_apply_laterally(routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)


def _plant_vertical_deferred_offset(routes, offsets):
    """Force a deferred route's last segment vertical and give its target a
    non-zero offset, so the render-time Y-shift would run along the line's own
    travel.  Returns the mutated route's index."""
    for i, r in enumerate(routes):
        if r.offset_regime is not OffsetRegime.DEFERRED or len(r.points) < 2:
            continue
        x, y = r.points[-1]
        # A clean vertical terminal segment whose endpoint stays put (so the
        # endpoint remains anchored and only the regime check fires).
        routes[i] = dataclasses.replace(r, points=[(x, y - 50.0), (x, y)])
        offsets[(r.edge.target, r.line_id)] = 8.0
        return i
    raise AssertionError("no deferred route available to plant on")


def test_planted_vertical_deferred_offset_is_caught() -> None:
    """A deferred route with an offset on a vertical terminal segment is
    reported, and the unmutated routing is not -- the check is neither vacuous
    nor a false positive on the clean render."""
    _graph, routes, offsets = _route(FIXTURES / "rnaseq_sections.mmd")
    assert not check_deferred_offsets_apply_laterally(routes, offsets)

    idx = _plant_vertical_deferred_offset(routes, offsets)
    planted = routes[idx]
    violations = check_deferred_offsets_apply_laterally(routes, offsets)
    assert any(
        v.source == planted.edge.source
        and v.target == planted.edge.target
        and v.which == "target"
        for v in violations
    ), "expected the planted vertical deferred offset to be reported"


def test_planted_vertical_deferred_offset_aborts_render_path() -> None:
    """The check runs on the always-on render path: the planted route aborts
    with ``CurveInvariantError`` independent of ``compute_layout``'s validate
    block."""
    graph, routes, offsets = _route(FIXTURES / "rnaseq_sections.mmd")
    assert_render_curve_invariants(graph, routes, offsets)  # clean: no raise

    _plant_vertical_deferred_offset(routes, offsets)
    with pytest.raises(CurveInvariantError, match="deferred route offset"):
        assert_render_curve_invariants(graph, routes, offsets)


def test_planted_vertical_deferred_offset_permissive_downgrades_to_warning() -> None:
    """``graph.permissive`` downgrades the same defect to a warning instead of
    aborting, letting a caller render the defective geometry best-effort."""
    graph, routes, offsets = _route(FIXTURES / "rnaseq_sections.mmd")
    _plant_vertical_deferred_offset(routes, offsets)

    graph.permissive = True
    with pytest.warns(PermissiveGuardWarning, match="deferred route offset"):
        assert_render_curve_invariants(graph, routes, offsets)
