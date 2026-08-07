"""Tests for the fused co-travelling distinct-line invariant.

Two DIFFERENT lines running the same way along one corridor nest a full
``OFFSET_STEP`` apart, which is what leaves a hairline of background showing
between their strokes.  Closed to less than that they paint one two-tone
stripe and one of the two lines is not there to read.

The defect only appears on the settled re-route, because that is the pass the
reservation ledger reaches, so the reported fixtures are exercised through the
render chokepoint rather than a single ``route_edges`` call.

The checker reports a pair only where at least one of the two tracks can be
re-seated.  A pair both of whose tracks a pre-routing plan owns holds the
coordinates those plans state, which nothing on the render path may move, so
reporting it would abort every render carrying a defect the chokepoint has no way
to get repaired.  That exemption hides a real population, which is pinned below
by identity so it can only shrink.

Covers:

* Happy-path: every shipped topology and example routes with no fused pair.
* Targeted: the three corridors a reservation band pulled together
  (``rl_return_row_convergence``, ``convergence_fold_diamond``,
  ``seed72_cross_family_fan``) keep the full step on the settled geometry.
* Meaningfulness: on the fixtures whose bands leave the pair short of the step
  the checker fires once the separation pass is disabled, and the pass lands
  each of those pairs exactly on the step rather than merely clear of the
  check, so the invariant genuinely encodes the defect.
* Exemption: the fused pairs the checker declines to report are exactly the
  recorded ones, over the whole fixture corpus.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

import nf_metro.layout.routing.core as routing_core
import nf_metro.layout.routing.invariants as invariants
from nf_metro.layout.constants import graph_offset_step
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import check_no_fused_cotravelling_lines
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = REPO_ROOT / "tests" / "fixtures" / "topologies"
EXAMPLES = REPO_ROOT / "examples"
EXAMPLE_TOPOLOGIES = EXAMPLES / "topologies"
CURVE_REPROS = REPO_ROOT / "tests" / "fixtures" / "curve_invariant_repros"
REGRESSIONS = REPO_ROOT / "tests" / "fixtures" / "regressions"

REPORTED = [
    CURVE_REPROS / "rl_return_row_convergence.mmd",
    EXAMPLE_TOPOLOGIES / "convergence_fold_diamond.mmd",
    EXAMPLE_TOPOLOGIES / "seed72_cross_family_fan.mmd",
]

# Fixtures whose reservation band seats the pair closer than one step, so the
# separation pass is what puts them back on it.  Read off the corpus by routing
# it with the pass disabled and collecting the fixtures the checker reports.
FUSED_WITHOUT_THE_PASS = [
    EXAMPLE_TOPOLOGIES / "packed_multiline_serpentine_grid.mmd",
    REGRESSIONS / "entry_trunk_row_bow.mmd",
]


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    paths.extend(sorted(TOPOLOGIES.glob("*.mmd")))
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
    return paths


def _corpus() -> list[Path]:
    paths = sorted(EXAMPLES.rglob("*.mmd"))
    paths += sorted((REPO_ROOT / "tests" / "fixtures").rglob("*.mmd"))
    paths += sorted((REPO_ROOT / "tests" / "fixtures").rglob("*.metro"))
    return paths


_CORPUS = _corpus()

_ExemptPair = tuple[str, str, str, str, float, float]

# Every pair of distinct lines the corpus draws as one stroke and the checker
# stays silent about, because a pre-routing plan owns both of their coordinates.
# Recorded as ``(fixture, axis, the two line ids sorted, and each line's
# coordinate)``: two lines painted over each other is a line the reader cannot
# see, so the population the exemption covers is enumerated rather than trusted
# to stay small.  All four sit at 0.00px separation against a 4.00px nesting
# step, over 76px to 762px of shared corridor, and all three fixtures abort on
# `CurveInvariantError` before a render of them reaches a caller.  An entry may
# be removed when its pair separates; adding one is a decision to argue for.
EXEMPT_FUSED_PAIRS: frozenset[_ExemptPair] = frozenset(
    {
        (
            "tests/fixtures/hash_seed_determinism/seed_15.mmd",
            "Y",
            "l0",
            "l2",
            624.0,
            624.0,
        ),
        (
            "tests/fixtures/hash_seed_determinism/seed_41.mmd",
            "Y",
            "l0",
            "l2",
            840.0,
            840.0,
        ),
    }
)


def _route(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, routes, offsets


def _settled(path: Path, monkeypatch: pytest.MonkeyPatch):
    """The geometry the renderer draws, plus the violations the chokepoint saw.

    The check is replaced by a recording stand-in that reports nothing, so the
    render runs to completion on a fixture carrying the defect and the test can
    measure its final geometry rather than only catch the abort.
    """
    from nf_metro.api import prepare_graph, resolve_theme
    from nf_metro.render.svg import build_observed_render_plan

    final: list[tuple] = []

    def spy(graph, routes, offsets):
        found = check_no_fused_cotravelling_lines(graph, routes, offsets)
        final.clear()
        final.append((graph, routes, offsets, found))
        return []

    monkeypatch.setattr(invariants, "check_no_fused_cotravelling_lines", spy)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        build_observed_render_plan(graph, resolve_theme(None, graph))
    assert final, "the render chokepoint never ran the check"
    return final[0]


def _pair_separations(routes, offsets) -> dict[tuple[str, str, str], float]:
    """Lateral separation of every co-travelling distinct-line track pair."""
    from nf_metro.layout.routing.common import (
        apply_route_offsets,
        corridor_lanes,
        corridor_runs,
    )

    lanes = corridor_lanes(
        run
        for rp in routes
        if rp.is_inter_section
        for run in corridor_runs(rp, apply_route_offsets(rp, offsets))
    )
    out: dict[tuple[str, str, str], float] = {}
    for i, first in enumerate(lanes):
        for second in lanes[i + 1 :]:
            if first.axis != second.axis or first.sign != second.sign:
                continue
            if first.line_id == second.line_id:
                continue
            axis = "X" if first.axis == 0 else "Y"
            key = tuple(sorted((first.line_id, second.line_id))) + (axis,)
            separation = abs(first.coord - second.coord)
            if key not in out or separation < out[key]:
                out[key] = separation  # type: ignore[index]
    return out  # type: ignore[return-value]


def _pair_identity(
    axis: str, first: tuple[str, float], second: tuple[str, float]
) -> tuple[str, str, str, float, float]:
    """One fused pair, ordered by line id so either scan order names it alike."""
    low, high = sorted((first, second))
    return (axis, low[0], high[0], round(low[1], 4), round(high[1], 4))


def _unreported_fused_pairs(path: Path) -> set[tuple[str, str, str, float, float]]:
    """The fused pairs the checker sees at *path*'s chokepoint and does not report.

    Measured as the difference between every co-travelling distinct-line pair
    drawn within one nesting step and the pairs the checker returns, so what is
    pinned is the checker's own silence rather than a restatement of the
    predicate producing it.  Read on the geometry the chokepoint is handed, which
    a fixture reaches whether or not the render then aborts on another guard.
    """
    from nf_metro.layout.routing.common import (
        apply_route_offsets,
        corridor_lanes,
        corridor_runs,
    )

    found: set[tuple[str, str, str, float, float]] = set()
    real = invariants.check_no_fused_cotravelling_lines

    def spy(graph, routes, offsets):
        step = graph_offset_step(graph)
        lanes = corridor_lanes(
            run
            for rp in routes
            if rp.is_inter_section
            for run in corridor_runs(rp, apply_route_offsets(rp, offsets))
        )
        fused = {
            _pair_identity(
                "X" if first.axis == 0 else "Y",
                (first.line_id, first.coord),
                (second.line_id, second.coord),
            )
            for i, first in enumerate(lanes)
            for second in lanes[i + 1 :]
            if first.fused_span(second, step) is not None
        }
        violations = real(graph, routes, offsets)
        found.update(
            fused
            - {
                _pair_identity(
                    item.axis,
                    (item.first_line, item.first_coord),
                    (item.second_line, item.second_coord),
                )
                for item in violations
            }
        )
        return violations

    from nf_metro.api import prepare_graph, resolve_theme
    from nf_metro.render.svg import build_observed_render_plan

    invariants.check_no_fused_cotravelling_lines = spy
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
            build_observed_render_plan(graph, resolve_theme(None, graph))
    except Exception:  # noqa: BLE001 - the check ran before any abort
        pass
    finally:
        invariants.check_no_fused_cotravelling_lines = real
    return found


@pytest.mark.parametrize(
    "path", _CORPUS, ids=[str(p.relative_to(REPO_ROOT)) for p in _CORPUS]
)
def test_the_fused_pairs_the_checker_exempts_are_the_recorded_ones(path: Path) -> None:
    """The population the plan-owned exemption hides does not grow unnoticed.

    The checker's guarantee is "no two distinct lines are drawn as one stroke",
    minus the pairs it cannot get repaired.  Left unenumerated, that subtraction
    can absorb new fused pairs while the suite stays green and the guarantee
    reads as unconditional.
    """
    rel = str(path.relative_to(REPO_ROOT))
    found = {(rel, *item) for item in _unreported_fused_pairs(path)}
    expected = {item for item in EXEMPT_FUSED_PAIRS if item[0] == rel}
    assert found == expected, (
        "the fused pairs the checker declines to report are not the ones "
        f"recorded: unrecorded {sorted(found - expected)}, recorded but now "
        f"separated {sorted(expected - found)}. A new one is two lines drawn "
        "over each other that no guard will mention; one that separated means "
        "dropping its EXEMPT_FUSED_PAIRS entry"
    )


def test_every_recorded_exempt_pair_names_a_corpus_fixture() -> None:
    """A stale entry would silently excuse a fixture that no longer exists."""
    corpus = {str(item.relative_to(REPO_ROOT)) for item in _CORPUS}
    named = {item[0] for item in EXEMPT_FUSED_PAIRS}
    assert named <= corpus, named - corpus


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_no_fused_cotravelling_lines_in_gallery(path: Path) -> None:
    """No shipped topology or example paints two distinct lines as one stroke."""
    graph, routes, offsets = _route(path)
    violations = check_no_fused_cotravelling_lines(graph, routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)


@pytest.mark.parametrize("path", REPORTED, ids=lambda p: p.stem)
def test_reported_corridors_keep_the_nesting_step(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The corridors a reservation band pulled together keep the full step."""
    _graph, _routes, _offsets, violations = _settled(path, monkeypatch)
    assert not violations, "\n".join(v.message() for v in violations)


@pytest.mark.parametrize("path", FUSED_WITHOUT_THE_PASS, ids=lambda p: p.stem)
def test_checker_fires_without_the_separation_pass(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabling the separation pass reproduces the fused pairs the check catches."""
    monkeypatch.setattr(
        routing_core, "_separate_fused_cotravelling_runs", lambda routes, ctx: None
    )
    graph, _routes, _offsets, violations = _settled(path, monkeypatch)
    assert violations, "expected a fused pair with the separation pass off"
    step = graph_offset_step(graph)
    assert all(v.separation < step for v in violations)


@pytest.mark.parametrize("path", FUSED_WITHOUT_THE_PASS, ids=lambda p: p.stem)
def test_separated_pairs_land_on_the_step(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each pair the pass moves ends exactly one step apart, not merely wider.

    The pass restores the pitch a bundle is drawn at; nudging the two lanes only
    far enough to satisfy the check would read as an accidental gap rather than a
    nested pair.
    """
    monkeypatch.setattr(
        routing_core, "_separate_fused_cotravelling_runs", lambda routes, ctx: None
    )
    graph, _routes, _offsets, violations = _settled(path, monkeypatch)
    fused = {
        tuple(sorted((v.first_line, v.second_line))) + (v.axis,) for v in violations
    }
    assert fused, "expected a fused pair with the separation pass off"
    monkeypatch.undo()
    graph, routes, offsets, _violations = _settled(path, monkeypatch)
    separations = _pair_separations(routes, offsets)
    step = graph_offset_step(graph)
    for pair in fused:
        assert pair in separations, f"{pair} no longer shares a corridor"
        assert separations[pair] == pytest.approx(step, abs=1e-6)
