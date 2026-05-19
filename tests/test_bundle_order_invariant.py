"""Tests for the bundle-order-preservation invariant.

Covers:

* Happy-path: every gallery fixture and example yields zero violations
  when passed through :func:`check_bundle_order_preserved`.
* Helper-level negative: the per-pair side-relation primitive returns
  the expected LEFT / RIGHT / COINCIDENT verdicts for hand-built
  inputs.
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
    _left_of,
    _relative_side,
    _segment_direction,
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
# Helper-level unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "p1, p2, expected",
    [
        ((0.0, 0.0), (10.0, 0.0), Direction.R),
        ((10.0, 0.0), (0.0, 0.0), Direction.L),
        ((0.0, 0.0), (0.0, 10.0), Direction.D),
        ((0.0, 10.0), (0.0, 0.0), Direction.U),
        ((0.0, 0.0), (10.0, 10.0), None),  # diagonal, not cardinal
        ((0.0, 0.0), (0.0, 0.0), None),  # degenerate
    ],
)
def test_segment_direction(p1, p2, expected) -> None:
    """``_segment_direction`` returns the cardinal direction for an
    axis-aligned segment and ``None`` otherwise.

    The fine-tolerance test is implicit in the inputs: a segment with
    any non-trivial off-axis component returns ``None``.
    """
    assert _segment_direction(p1, p2) is expected


@pytest.mark.parametrize(
    "tangent, expected_left",
    [
        (Direction.R, Direction.U),
        (Direction.U, Direction.L),
        (Direction.L, Direction.D),
        (Direction.D, Direction.R),
    ],
)
def test_left_of_is_quarter_turn_ccw(
    tangent: Direction, expected_left: Direction
) -> None:
    """``_left_of`` is a quarter-turn anti-clockwise in screen coords."""
    assert _left_of(tangent) is expected_left


@pytest.mark.parametrize(
    "a, b, side_dir, expected",
    [
        # +x axis (R): A LEFT iff A.x > B.x
        ((5.0, 0.0), (0.0, 0.0), Direction.R, Side.LEFT),
        ((0.0, 0.0), (5.0, 0.0), Direction.R, Side.RIGHT),
        ((0.0, 0.0), (0.0, 0.0), Direction.R, Side.COINCIDENT),
        # -y axis (U): A LEFT iff A.y < B.y
        ((0.0, 0.0), (0.0, 5.0), Direction.U, Side.LEFT),
        ((0.0, 5.0), (0.0, 0.0), Direction.U, Side.RIGHT),
        # +y axis (D): A LEFT iff A.y > B.y
        ((0.0, 5.0), (0.0, 0.0), Direction.D, Side.LEFT),
        # -x axis (L): A LEFT iff A.x < B.x
        ((0.0, 0.0), (5.0, 0.0), Direction.L, Side.LEFT),
    ],
)
def test_relative_side(a, b, side_dir, expected) -> None:
    """``_relative_side`` projects ``a - b`` onto the unit vector
    pointing in ``side_dir`` and returns LEFT for positive projection,
    RIGHT for negative, COINCIDENT when within tolerance.
    """
    assert _relative_side(a, b, side_dir) == expected


# ---------------------------------------------------------------------------
# Route-level negative test: a synthetic flipped corner is caught
# ---------------------------------------------------------------------------


def _synthetic_route(
    line_id: str, points: list[tuple[float, float]]
) -> RoutedPath:
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

    Construction: two L-shape routes whose corners sit within
    ``_CLUSTER_TOLERANCE`` (= ``COORD_TOLERANCE``, 1 px) of each
    other - tight enough that real bundles (offset by
    ``OFFSET_STEP`` = 3 px) never cluster together, loose enough
    that this sub-pixel-offset synthetic case does.

    Line A's elbow is at ``(100, 100)``, line B's is at
    ``(100.5, 100.5)``.  The approach segments are both R (going
    east, dy=0 at the elbow); the exit segments are both D (going
    south, dx=0).  Because the elbows differ in *both* x and y by
    half a pixel, A and B sit on opposite sides of each other on
    the incoming run (B is below A) AND opposite sides on the
    outgoing run (B is right of A).

    The expected verdict per ``_left_of`` semantics:

    * incoming R, left = U (smaller y is LEFT): A.y=100 < B.y=100.5
      so A is LEFT before.
    * outgoing D, left = R (larger x is LEFT): A.x=100 < B.x=100.5
      so A is RIGHT after.

    LEFT->RIGHT is exactly the flip the invariant exists to catch.
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
    assert violations, (
        "expected a synthetic bundle-order violation; got an empty list"
    )
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
