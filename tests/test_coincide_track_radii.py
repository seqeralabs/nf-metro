"""Tests for the coincide-pass corner-radius re-derivation.

When :func:`_coincide_same_line_tracks` fuses several same-line vertical legs
onto one shared reference X, :func:`_set_vchannel_x` moves each member's
channel and re-derives its two flanking corner radii.  A fused track is a
single stroke with no concentric nesting, so each corner is the zero-offset
concentric radius for its *final* waypoints -- derived through the central
:func:`concentric_corner_radius_at` helper, not hand-set to a fixed value.

The bundle-corner concentricity guard (``check_concentric_bundle_corners``)
covers every multi-line bundle corner on the final paths.  This coincide
corner is the one place that guard does not reach, and a corpus-wide runtime
guard cannot cover it: shipping fixtures legitimately route two same-line
tracks through a coincident corner with *unequal* (concentrically nested)
radii -- e.g. ``longread_variant_calling`` -- so an "equal radii at a
coincident corner" invariant would red on ``main``.  The regression is
locked at the source instead: every corner the coincide pass touches must
match the central zero-offset derivation for the route's final geometry.

Covers:

* Unit: :func:`_set_vchannel_x` re-derives both flanking corners from the
  moved waypoints via the central helper.
* Corpus: across every shipped fixture, each corner the coincide pass snaps
  matches the central derivation.
* Meaningfulness: a hand-set radius (instead of re-deriving) makes the corpus
  check fire, so it is not vacuous.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nf_metro.layout.routing.normalize as normalize
from nf_metro.layout.constants import CURVE_RADIUS
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import (
    OffsetRegime,
    compute_station_offsets,
    route_edges,
)
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.corners import concentric_corner_radius_at
from nf_metro.layout.routing.normalize import _set_vchannel_x, _VChannel
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge

REPO_ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = REPO_ROOT / "tests" / "fixtures" / "topologies"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
EXAMPLES = REPO_ROOT / "examples"

_RADIUS_TOLERANCE = 1e-6

# The production re-derivation, captured before any test patches the module so
# the corpus spy can wrap it (and the meaningfulness test can swap it out).
_PROD_SET_VCHANNEL_X = normalize._set_vchannel_x

# Fixtures whose layout actually drives the coincide pass; the gallery sweep
# below covers the rest vacuously (no corner snapped -> nothing to check).
COINCIDE_FIXTURES = [
    "variantbenchmarking.mmd",
    "variantbenchmarking_auto.mmd",
    "topologies/convergence_stacked_sink.mmd",
    "topologies/divergent_fanout_split.mmd",
    "topologies/merge_pullaway.mmd",
    "topologies/merge_right_entry.mmd",
    "topologies/merge_around_below_leftmost.mmd",
    "topologies/merge_trunk_out_of_range_section.mmd",
]


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    paths.extend(sorted(TOPOLOGIES.glob("*.mmd")))
    paths.extend(sorted(FIXTURES.glob("*.mmd")))
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
    paths.extend(sorted((EXAMPLES / "topologies").glob("*.mmd")))
    return paths


def _touched_corner_mismatches(
    path: Path, monkeypatch: pytest.MonkeyPatch, impl=_PROD_SET_VCHANNEL_X
) -> list[tuple[str, int, float, float]]:
    """Route *path*, recording every corner the coincide pass snaps via *impl*.

    Returns, for each snapped corner, any disagreement between the stored
    radius and the central zero-offset concentric derivation for the route's
    final waypoints: ``(line_id, radius_index, stored, expected)``.
    """
    touched: list[tuple[RoutedPath, int]] = []

    def spy(ch: _VChannel, new_x: float) -> None:
        impl(ch, new_x)
        touched.append((ch.route, ch.idx))

    monkeypatch.setattr(normalize, "_set_vchannel_x", spy)
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    route_edges(graph, station_offsets=offsets)

    mismatches: list[tuple[str, int, float, float]] = []
    for rp, k in touched:
        radii = rp.curve_radii
        if radii is None:
            continue
        pts = rp.points
        for radius_idx in (k - 1, k):
            if not 0 <= radius_idx < len(radii):
                continue
            expected = concentric_corner_radius_at(
                pts[radius_idx], pts[radius_idx + 1], pts[radius_idx + 2], 0.0
            )
            if abs(radii[radius_idx] - expected) > _RADIUS_TOLERANCE:
                mismatches.append((rp.line_id, radius_idx, radii[radius_idx], expected))
    return mismatches


def test_set_vchannel_x_rederives_flanking_corners_from_waypoints() -> None:
    """Moving a fused channel re-derives both corners through the central helper.

    A pre-set non-base radius is replaced by the zero-offset concentric radius
    for the moved geometry, which a single stroke resolves to the base radius.
    """
    points = [(0.0, 0.0), (50.0, 0.0), (50.0, 100.0), (120.0, 100.0)]
    route = RoutedPath(
        edge=Edge(source="s", target="t", line_id="l"),
        line_id="l",
        points=points,
        is_inter_section=True,
        offset_regime=OffsetRegime.BAKED,
        curve_radii=[19.0, 19.0],
    )
    channel = _VChannel(route=route, idx=1, x=50.0, y_lo=0.0, y_hi=100.0, down=True)

    _set_vchannel_x(channel, 55.0)

    assert route.points[1] == (55.0, 0.0)
    assert route.points[2] == (55.0, 100.0)
    expected = [
        concentric_corner_radius_at(
            route.points[i], route.points[i + 1], route.points[i + 2], 0.0
        )
        for i in (0, 1)
    ]
    assert route.curve_radii == expected
    assert route.curve_radii == [CURVE_RADIUS, CURVE_RADIUS]


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_coincide_pass_corners_match_central_derivation(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every corner the coincide pass snaps matches the central derivation."""
    mismatches = _touched_corner_mismatches(path, monkeypatch)
    assert not mismatches, (
        f"{path.name}: {len(mismatches)} coincided corner(s) disagree with the "
        f"central derivation; first: {mismatches[0]}"
    )


@pytest.mark.parametrize("fixture", COINCIDE_FIXTURES)
def test_named_coincide_fixtures_snap_at_least_one_corner(
    fixture: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The named fixtures genuinely drive the coincide pass.

    Guards the corpus check against silently going vacuous if a layout change
    stops these fixtures from fusing same-line tracks.
    """
    touched: list[object] = []

    def spy(ch: _VChannel, new_x: float) -> None:
        _PROD_SET_VCHANNEL_X(ch, new_x)
        touched.append(ch)

    monkeypatch.setattr(normalize, "_set_vchannel_x", spy)
    graph = parse_metro_mermaid((EXAMPLES / fixture).read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    route_edges(graph, station_offsets=offsets)
    assert touched, f"{fixture} no longer exercises the coincide pass"


@pytest.mark.parametrize(
    "fixture", ["variantbenchmarking.mmd", "topologies/merge_right_entry.mmd"]
)
def test_reintroduced_hand_clobber_is_detected(
    fixture: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-set radius instead of the central derivation is caught.

    Proves the corpus check has teeth: with the move re-implemented to stamp a
    fixed wrong radius instead of re-deriving, the snapped corners disagree
    with the central derivation.
    """

    def clobber(ch: _VChannel, new_x: float) -> None:
        rp = ch.route
        pts = rp.points
        k = ch.idx
        pts[k] = (new_x, pts[k][1])
        pts[k + 1] = (new_x, pts[k + 1][1])
        if rp.curve_radii is None:
            return
        for radius_idx in (k - 1, k):
            if 0 <= radius_idx < len(rp.curve_radii):
                rp.curve_radii[radius_idx] = CURVE_RADIUS * 3.0

    mismatches = _touched_corner_mismatches(
        EXAMPLES / fixture, monkeypatch, impl=clobber
    )
    assert mismatches, (
        "expected the clobbered radii to disagree with central derivation"
    )
