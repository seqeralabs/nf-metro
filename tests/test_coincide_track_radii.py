"""Tests for the coincide-pass corner-radius re-derivation.

When a coincide pass moves a vertical leg onto a shared reference X,
:func:`_set_vchannel_x` re-derives its two flanking corner radii through the
central :func:`concentric_corner_radius_at` helper at the displacement it moved
with, rather than hand-set to a fixed value.  Same-line fusion
(:func:`_coincide_same_line_tracks`) moves at zero displacement, so each corner
is the base radius; a distinct-line convergent cluster
(:func:`_stack_distinct_port_descents`) moves each lane at its rank
displacement, so the outer lanes nest concentrically wider.

The bundle-corner concentricity guard (``check_concentric_bundle_corners``)
covers every multi-line bundle corner on the final paths.  This coincide
corner is one place that guard does not reach: it re-derives each fused leg's
own flanking corners to the zero-offset base radius.

A separate turn a fused leg *shares* with a bundle-outer sibling is handled
downstream by :func:`_unify_coincident_corner_radii` (checked by
``check_coincident_corner_radii``), which snaps it to the widest coincident
radius so the shared stroke reads as one arc.  Those shared corners are
therefore excluded here; this file locks the per-leg source re-derivation:
every non-shared corner the coincide pass touches must match the central
zero-offset derivation for the route's final geometry.

Covers:

* Unit: :func:`_set_vchannel_x` re-derives both flanking corners from the
  moved waypoints via the central helper.
* Corpus: across every shipped fixture, each non-shared corner the coincide
  pass snaps matches the central derivation.
* Meaningfulness: a hand-set radius (instead of re-deriving) makes the corpus
  check fire, so it is not vacuous.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from layout_validator import shared_same_line_turn_vertices

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

# The production re-derivations, captured before any test patches the module so
# the corpus spy can wrap them (and the meaningfulness test can swap the
# vertical-channel one out).
_PROD_SET_VCHANNEL_X = normalize._set_vchannel_x
_PROD_SET_HTRUNK_Y = normalize._set_htrunk_y

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
    """Route *path*, recording every corner a coincide/reseat pass snaps.

    Both channel entry points re-derive their flanking corners through the same
    central helper: :func:`_set_vchannel_x` moves a vertical channel and reseats
    its two flanking corners at one displacement, while :func:`_set_htrunk_y`
    moves a horizontal trunk and reseats each flanking corner at its own
    incoming/outgoing displacement.  A corner a fused same-line channel first
    snaps to the base radius can be re-snapped by a later distinct-line traverse
    to its concentric bundle offset, so record the *last* displacement applied to
    each corner and derive the expected radius at that offset -- the corner's
    actual final bundle offset, not whichever pass touched it first.

    Only corners on the *final* routes are checked; the intermediate route
    objects an earlier layout pass discards are filtered out through their
    identity, so a stale corner from a superseded pass cannot report a mismatch.
    Returns any disagreement between the stored radius and the central derivation
    as ``(line_id, radius_index, stored, expected)``.
    """
    # (id(route), radius_index) -> (route, offset); the last reseat wins, so the
    # recorded offset is the one the final radius was derived at.
    touched: dict[tuple[int, int], tuple[RoutedPath, float]] = {}

    def vspy(ch: _VChannel, new_x: float, offset: float = 0.0) -> None:
        impl(ch, new_x, offset)
        for radius_idx in (ch.idx - 1, ch.idx):
            touched[(id(ch.route), radius_idx)] = (ch.route, offset)

    def hspy(
        rp: RoutedPath,
        k: int,
        new_y: float,
        offset_in: float = 0.0,
        offset_out: float = 0.0,
    ) -> None:
        _PROD_SET_HTRUNK_Y(rp, k, new_y, offset_in, offset_out)
        touched[(id(rp), k - 1)] = (rp, offset_in)
        touched[(id(rp), k)] = (rp, offset_out)

    monkeypatch.setattr(normalize, "_set_vchannel_x", vspy)
    monkeypatch.setattr(normalize, "_set_htrunk_y", hspy)
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    shared = shared_same_line_turn_vertices(routes)
    route_ids = {id(r) for r in routes}

    mismatches: list[tuple[str, int, float, float]] = []
    for (rid, radius_idx), (rp, offset) in touched.items():
        if rid not in route_ids:
            continue
        radii = rp.curve_radii
        if radii is None or not 0 <= radius_idx < len(radii):
            continue
        pts = rp.points
        vertex = pts[radius_idx + 1]
        if (rp.line_id, round(vertex[0]), round(vertex[1])) in shared:
            continue  # owned by the coincident-corner unification pass
        expected = concentric_corner_radius_at(
            pts[radius_idx], pts[radius_idx + 1], pts[radius_idx + 2], offset
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


_XFAIL_COINCIDE_CENTRAL_DERIVATION: dict[str, str] = {}


def _coincide_params() -> list:
    params = []
    for p in _gather_fixtures():
        key = p.relative_to(REPO_ROOT).as_posix()
        reason = _XFAIL_COINCIDE_CENTRAL_DERIVATION.get(key)
        marks = (pytest.mark.xfail(reason=reason, strict=True),) if reason else ()
        params.append(pytest.param(p, id=key, marks=marks))
    return params


@pytest.mark.parametrize("path", _coincide_params())
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

    def spy(ch: _VChannel, new_x: float, offset: float = 0.0) -> None:
        _PROD_SET_VCHANNEL_X(ch, new_x, offset)
        touched.append(ch)

    monkeypatch.setattr(normalize, "_set_vchannel_x", spy)
    graph = parse_metro_mermaid((EXAMPLES / fixture).read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    route_edges(graph, station_offsets=offsets)
    assert touched, f"{fixture} no longer exercises the coincide pass"


@pytest.mark.parametrize(
    "fixture", ["variantbenchmarking.mmd", "topologies/divergent_fanout_split.mmd"]
)
def test_reintroduced_hand_clobber_is_detected(
    fixture: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-set radius instead of the central derivation is caught.

    Proves the corpus check has teeth: with the move re-implemented to stamp a
    fixed wrong radius instead of re-deriving, the snapped corners disagree
    with the central derivation.
    """

    def clobber(ch: _VChannel, new_x: float, offset: float = 0.0) -> None:
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
