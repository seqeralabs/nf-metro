"""Tests for the bundle-order-preservation invariant.

Covers:

* Happy-path: every gallery fixture and example yields zero violations
  when passed through :func:`check_bundle_order_preserved`.
* Route-level negative: a synthetic ``RoutedPath`` pair with a
  hand-crafted flipped corner correctly surfaces as a violation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import Direction, RoutedPath
from nf_metro.layout.routing.invariants import (
    BundleOrderViolation,
    Side,
    check_bundle_order_preserved,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge

REPO_ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = REPO_ROOT / "tests" / "fixtures" / "topologies"
EXAMPLES = REPO_ROOT / "examples"

# Fixtures with KNOWN bundle-order violations that the criterion
# correctly surfaces.  These are real bugs we xfail rather than blunt
# the criterion to hide them.
_KNOWN_VIOLATION_FIXTURES: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# Happy-path: every fixture and example must pass the invariant
# ---------------------------------------------------------------------------


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    paths.extend(sorted(TOPOLOGIES.glob("*.mmd")))
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
    paths.extend(sorted((EXAMPLES / "topologies").glob("*.mmd")))
    return paths


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_no_bundle_order_violations_in_gallery(path: Path) -> None:
    """Every shipped topology and example must route without a
    bundle-order violation.

    This is the corpus-level happy-path check.  A regression to a
    routing handler that creates a flipped concentric bundle would
    cause exactly one fixture to start failing here.

    Fixtures listed in :data:`_KNOWN_VIOLATION_FIXTURES` are
    xfailed: they have real bundle-order bugs at the Plots-entry
    corner that the criterion correctly catches, and we'd rather
    track those as known failures than silently blunt the criterion.
    """
    if path.name in _KNOWN_VIOLATION_FIXTURES:
        pytest.xfail(
            f"{path.name} has a known bundle-order violation at the "
            "Plots-entry corner; the criterion correctly catches it."
        )
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    violations = check_bundle_order_preserved(routes)
    assert violations == [], (
        f"{path.name}: {len(violations)} bundle-order violation(s); "
        f"first: {violations[0].message() if violations else ''}"
    )


# ---------------------------------------------------------------------------
# Cross-column perpendicular-exit -> perpendicular-entry bundles
# ---------------------------------------------------------------------------

_CROSS_COL_PERP_ENTRY_FIXTURES = [
    "lr_perp_top_exit_perp_entry",
    "lr_perp_bottom_exit_perp_entry",
    "lr_perp_top_exit_perp_entry_diverging",
]


@pytest.mark.parametrize("stem", _CROSS_COL_PERP_ENTRY_FIXTURES)
def test_cross_column_perp_entry_preserves_bundle_order(stem: str) -> None:
    """A co-travelling bundle taken over the corridor from a perpendicular
    exit on one LR section into the perpendicular entry of another LR
    section in a different column keeps a single left/right order through
    the whole riser -> corridor -> entry-drop chain.

    The entry-drop leg's per-line channel order must agree with the
    corridor's descent order; a disagreement flips the bundle at the
    entry -> first-station corner, which both trips the runtime guard
    and renders a crossover at the drop.
    """
    path = EXAMPLES / "topologies" / f"{stem}.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    violations = check_bundle_order_preserved(routes)
    assert violations == [], (
        f"{stem}: {len(violations)} bundle-order violation(s); "
        f"first: {violations[0].message() if violations else ''}"
    )


# ---------------------------------------------------------------------------
# Route-level negative test: a synthetic flipped corner is caught
# ---------------------------------------------------------------------------


def _synthetic_route(line_id: str, points: list[tuple[float, float]]) -> RoutedPath:
    """Build a ``RoutedPath`` from a points list for testing.

    Source/target IDs are fixed (``'__src__'``, ``'__tgt__'``) so the
    paths share a bundle key.  The ``Edge`` carries the line id; the
    rest of the routing metadata is irrelevant to
    :func:`check_bundle_order_preserved`.
    """
    return RoutedPath(
        edge=Edge(source="__src__", target="__tgt__", line_id=line_id),
        line_id=line_id,
        points=points,
        is_inter_section=True,
        offsets_applied=True,
    )


def test_check_skips_clean_bundle() -> None:
    """Two paths that share waypoints exactly produce zero violations:
    the COINCIDENT path-pair has nothing to compare on either side.
    """
    pts = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (200.0, 100.0)]
    routes = [_synthetic_route("A", pts), _synthetic_route("B", pts)]
    assert check_bundle_order_preserved(routes) == []


def test_check_skips_single_line_bundle() -> None:
    """A bundle with only one line has no pairs to compare; no
    violation is possible.
    """
    pts = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (200.0, 100.0)]
    routes = [_synthetic_route("A", pts)]
    assert check_bundle_order_preserved(routes) == []


def test_synthetic_flipped_corner_is_caught() -> None:
    """A hand-crafted bundle with a deliberate flip at a near-shared
    corner surfaces as a :class:`BundleOrderViolation`.

    Two L-shape routes whose elbows are half a pixel apart on both
    axes: A is on the LEFT of B going east, then on the RIGHT going
    south.  LEFT -> RIGHT is exactly the flip the invariant exists to
    catch.
    """
    a_pts = [
        (0.0, 100.0),
        (100.0, 100.0),
        (100.0, 200.0),
    ]
    b_pts = [
        (0.0, 100.5),
        (100.5, 100.5),
        (100.5, 200.0),
    ]
    routes = [_synthetic_route("A", a_pts), _synthetic_route("B", b_pts)]
    violations = check_bundle_order_preserved(routes)
    assert violations, "expected a synthetic bundle-order violation; got an empty list"
    v = violations[0]
    assert v.line_a == "A" and v.line_b == "B"
    assert v.in_tangent is Direction.R
    assert v.out_tangent is Direction.D
    assert {v.before, v.after} == {Side.LEFT, Side.RIGHT}, v.message()


def test_violation_message_self_describing() -> None:
    """The violation's ``message()`` includes the corner xy, line ids,
    tangent directions, and the offending before/after sides - the
    fields downstream callers (the engine guard and CI logs) rely on
    for diagnosis.
    """
    v = BundleOrderViolation(
        edge_source="src",
        edge_target="tgt",
        line_a="alpha",
        line_b="beta",
        corner_xy=(100.0, 200.0),
        in_tangent=Direction.D,
        out_tangent=Direction.L,
        before=Side.LEFT,
        after=Side.RIGHT,
    )
    msg = v.message()
    assert "100.0" in msg and "200.0" in msg
    assert "alpha" in msg and "beta" in msg
    assert "D" in msg and "L" in msg
    assert "LEFT" in msg and "RIGHT" in msg
