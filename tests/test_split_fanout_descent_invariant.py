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
    check_concentric_bundle_corners,
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


def _opening_descents(routes) -> list[tuple[str, str, float]]:
    """The opening fan-out descent of each inter-section route: (source, line, x)."""
    from nf_metro.layout.routing.common import initial_fanout_descent_span

    out: list[tuple[str, str, float]] = []
    for rp in routes:
        if not rp.is_inter_section:
            continue
        span = initial_fanout_descent_span(rp)
        if span is not None:
            out.append((rp.edge.source, rp.line_id, span[0]))
    return out


def test_same_line_fan_stays_bundled_beside_distinct_descent() -> None:
    """A source fanning several same-line branches plus a distinct-line branch
    descends as ONE per-line bundle: the same-line branches share a single
    fused track and the distinct line sits exactly one OFFSET_STEP beside it,
    rather than each branch splitting onto its own slot (issue #1409)."""
    from collections import defaultdict

    from nf_metro.layout.constants import OFFSET_STEP

    path = EXAMPLE_TOPOLOGIES / "same_line_fan_distinct_descent.mmd"
    _graph, routes, _offsets = _route(path)

    descents = _opening_descents(routes)
    junctions = {src for src, _line, _x in descents if src.startswith("__junction")}
    assert junctions, "fixture must fan out through a junction"

    for junction in junctions:
        by_line: dict[str, list[float]] = defaultdict(list)
        for src, line_id, x in descents:
            if src == junction:
                by_line[line_id].append(x)
        # Every same-line branch shares one fused descent X.
        for line_id, xs in by_line.items():
            assert max(xs) - min(xs) < OFFSET_STEP / 2, (
                f"line {line_id!r} splits into descents at {sorted(xs)}"
            )
        # Distinct lines sit one step apart -- a tight bundle, not overlaid.
        line_xs = sorted(xs[0] for xs in by_line.values())
        if len(line_xs) >= 2:
            gaps = [b - a for a, b in zip(line_xs, line_xs[1:])]
            assert all(abs(g - OFFSET_STEP) < 1.0 for g in gaps), (
                f"distinct lines not one step apart: {line_xs}"
            )


def test_same_line_fan_traverses_read_as_one_stroke() -> None:
    """The same-line branches wrapping to one column also share their horizontal
    traverse band: two branches that ride the same descent and riser column must
    not run their leftward traverse on parallel same-colour tracks a few px apart
    (issue #1409)."""
    from collections import defaultdict

    from nf_metro.layout.routing.common import iter_horizontal_trunks

    path = EXAMPLE_TOPOLOGIES / "same_line_fan_distinct_descent.mmd"
    _graph, routes, _offsets = _route(path)

    # Group each source's same-line interior traverses by their riser column.
    by_riser: dict[tuple[str, str, int], list[float]] = defaultdict(list)
    for rp in routes:
        if not rp.is_inter_section:
            continue
        for _k, seg in iter_horizontal_trunks(rp):
            by_riser[(rp.edge.source, rp.line_id, round(seg.xb))].append(seg.y)

    shared = {k: ys for k, ys in by_riser.items() if len(ys) >= 2}
    assert shared, "fixture must have two same-line branches sharing a riser column"
    for (src, line_id, riser_x), ys in shared.items():
        assert max(ys) - min(ys) < 1.0, (
            f"line {line_id!r} from {src} runs parallel traverses to riser "
            f"x={riser_x} at {sorted(ys)} instead of one fused band"
        )


def test_distinct_line_fan_traverses_nest_as_one_bundle() -> None:
    """Distinct lines fanning from one source and sharing the corridor they turn
    onto nest their traverses one OFFSET_STEP apart -- a tight bundle -- rather
    than running on independently-sized bands several px apart (issue #1409)."""
    from collections import defaultdict

    from nf_metro.layout.constants import OFFSET_STEP
    from nf_metro.layout.routing.normalize import _fanout_traverse_legs

    path = EXAMPLE_TOPOLOGIES / "same_line_fan_distinct_descent.mmd"
    _graph, routes, _offsets = _route(path)

    nested = False
    for legs in _fanout_traverse_legs(routes).values():
        per_line = defaultdict(list)
        for leg in legs:
            per_line[leg.route.line_id].append(leg.seg.y)
        if len(per_line) < 2:
            continue
        band_ys = sorted(min(ys) for ys in per_line.values())
        gaps = [b - a for a, b in zip(band_ys, band_ys[1:])]
        assert all(abs(g - OFFSET_STEP) < 1.0 for g in gaps), (
            f"distinct-line traverses from one source not one step apart: {band_ys}"
        )
        nested = True
    assert nested, "fixture must fan distinct lines onto a shared corridor"


def _same_line_fan_sources(routes) -> set[str]:
    """Sources that fan ONE line to >= 2 distinct targets (a peel-off fan).

    These are exactly the branches whose turn vertices diverge as each peels
    off to its own port, so grouping them together would splay their shared
    corners apart.
    """
    from collections import defaultdict

    by_source_line: dict[tuple[str, str], set[str]] = defaultdict(set)
    for rp in routes:
        by_source_line[(rp.edge.source, rp.line_id)].add(rp.edge.target)
    return {src for (src, _line), tgts in by_source_line.items() if len(tgts) >= 2}


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_concentric_guard_never_compares_across_targets(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime concentric-corner guard only ever compares routes on the
    same ``(source, target)`` edge, so a same-line fan peeling to ports at
    different depths is never cross-compared and cannot trip a false
    delamination abort (issue #1417).

    ``check_concentric_bundle_corners`` bundles strictly by exact edge, so the
    divergent branches of a peel-off fan land in separate bundles.  This is the
    always-on render-path counterpart of the peel-off break the #1409 test
    oracle (``TestConcentricArcCenters``) added: once two branches' turn
    vertices diverge they are separate routes, not a delaminating bundle.  A
    regression that loosened the grouping (e.g. by source alone) would compare
    the divergent branches and is caught here.
    """
    import nf_metro.layout.routing.invariants as inv

    compared: list[tuple[tuple[str, str], tuple[str, str]]] = []
    real = inv._pair_corner_violation

    def spy(src_id, tgt_id, ra, pa, radii_a, rb, pb, radii_b):
        compared.append(
            ((ra.edge.source, ra.edge.target), (rb.edge.source, rb.edge.target))
        )
        return real(src_id, tgt_id, ra, pa, radii_a, rb, pb, radii_b)

    monkeypatch.setattr(inv, "_pair_corner_violation", spy)

    graph, routes, offsets = _route(path)
    inv.check_concentric_bundle_corners(graph, routes, offsets)

    for edge_a, edge_b in compared:
        assert edge_a == edge_b, (
            f"{path.name}: concentric guard compared routes across edges "
            f"{edge_a} vs {edge_b}; distinct-target branches must never nest"
        )


def test_peeloff_fan_arms_the_concentric_guard_trap() -> None:
    """The isolation test above is a genuine trap, not vacuous: this fixture has
    a same-line fan peeling to two different-depth targets, so loosening the
    guard's grouping to compare same-source branches would immediately produce a
    cross-target comparison.  The guard, as shipped, stays silent on it."""
    graph, routes, offsets = _route(EXAMPLE_TOPOLOGIES / "divergent_fanout_split.mmd")
    assert _same_line_fan_sources(routes), "fixture lost its peel-off fan"
    assert not check_concentric_bundle_corners(graph, routes, offsets)
