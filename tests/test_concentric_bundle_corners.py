"""Tests for the bundle-corner concentricity invariant.

Covers:

* Happy-path: every gallery fixture and example routes without a
  non-concentric wholesale bundle corner.
* Route-level positive/negative: hand-built bundles exercise the
  wholesale-vs-transition discriminator and the arc-centre test, so the
  invariant is shown to catch a real pinch rather than passing by accident.

The correctness check here is the one the corner-radius *source* ratchet
(``tests/test_corner_radius_ratchet.py``) explicitly cannot perform.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.invariants import check_concentric_bundle_corners
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge

REPO_ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = REPO_ROOT / "tests" / "fixtures" / "topologies"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
EXAMPLES = REPO_ROOT / "examples"


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    paths.extend(sorted(TOPOLOGIES.glob("*.mmd")))
    paths.extend(sorted(FIXTURES.glob("*.mmd")))
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
    paths.extend(sorted((EXAMPLES / "topologies").glob("*.mmd")))
    return paths


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_no_non_concentric_bundle_corners_in_gallery(path: Path) -> None:
    """Every shipped fixture must route with concentric wholesale corners.

    A handler that sizes a wholesale-translated bundle corner with a base
    or hand-signed radius (instead of the geometry-derived concentric one)
    surfaces here as a failing fixture, even when that radius traces to an
    approved helper and so slips past the source ratchet.
    """
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    violations = check_concentric_bundle_corners(graph, routes, offsets)
    assert violations == [], (
        f"{path.name}: {len(violations)} non-concentric corner(s); "
        f"first: {violations[0].message() if violations else ''}"
    )


# ---------------------------------------------------------------------------
# Route-level positive/negative tests
# ---------------------------------------------------------------------------


def _route(
    line_id: str,
    points: list[tuple[float, float]],
    radii: list[float] | None = None,
) -> RoutedPath:
    """A bundled ``RoutedPath`` (shared src/tgt) with baked geometry."""
    return RoutedPath(
        edge=Edge(source="__src__", target="__tgt__", line_id=line_id),
        line_id=line_id,
        points=points,
        is_inter_section=True,
        offsets_applied=True,
        curve_radii=radii,
    )


# A down->right corner offset wholesale by (3, -3): the concentric radii are
# 10 (inner) and 7 (outer) so both arc centres land at (10, 90).
_CONCENTRIC_A = _route("a", [(0.0, 0.0), (0.0, 100.0), (50.0, 100.0)], [10.0])
_CONCENTRIC_B = _route("b", [(3.0, 0.0), (3.0, 97.0), (50.0, 97.0)], [7.0])


def test_concentric_wholesale_corner_passes() -> None:
    """A wholesale-translated corner with correctly nested radii is clean."""
    assert (
        check_concentric_bundle_corners(None, [_CONCENTRIC_A, _CONCENTRIC_B], {}) == []
    )


def test_non_concentric_wholesale_corner_is_caught() -> None:
    """The same geometry with a base (un-nested) outer radius pinches."""
    bad_b = _route("b", [(3.0, 0.0), (3.0, 97.0), (50.0, 97.0)], [10.0])
    violations = check_concentric_bundle_corners(None, [_CONCENTRIC_A, bad_b], {})
    assert len(violations) == 1
    assert violations[0].centre_spread > 1.0


def test_transition_corner_with_one_pinned_leg_is_skipped() -> None:
    """A converging corner (vertical legs offset, horizontals coincident) is
    a transition, not a wholesale translation, so non-concentric is allowed.
    """
    a = _route("a", [(0.0, 0.0), (0.0, 100.0), (50.0, 100.0)], [10.0])
    # b's vertical leg is offset 3px but both horizontals share y=100.
    b = _route("b", [(3.0, 0.0), (3.0, 100.0), (50.0, 100.0)], [10.0])
    assert check_concentric_bundle_corners(None, [a, b], {}) == []


def test_diagonal_leg_is_not_a_corner() -> None:
    """A 45-degree diagonal leg carries no orthogonal corner to nest."""
    a = _route("a", [(0.0, 0.0), (100.0, 0.0), (130.0, 117.0), (180.0, 117.0)])
    b = _route("b", [(0.0, 3.0), (97.0, 3.0), (127.0, 120.0), (180.0, 120.0)])
    assert check_concentric_bundle_corners(None, [a, b], {}) == []


def test_single_line_bundle_is_skipped() -> None:
    """One line has no bundle-mate to be concentric with."""
    assert check_concentric_bundle_corners(None, [_CONCENTRIC_A], {}) == []
