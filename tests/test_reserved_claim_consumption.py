"""Every realised gap claim is drawn inside the band its reservation realises.

A row-gap or column-gap ``RouteReservation`` allocates a corridor band for the
specific emitted path segments its claims name.  The drawn geometry has to
consume that allocation: each claim's own polyline points, read through the
claim's ``(path_rank, segment_rank .. segment_end_rank + 1)`` identity, must
lie inside ``[region_start + negative_side_clearance, region_end -
positive_side_clearance]``.  A claimed corridor drawn outside that band was
positioned by a geometry-derived fallback instead of its reservation, which is
exactly what the reservation ledger exists to forbid.

The whole corpus satisfies that, and every fixture is held to it with no
exceptions. The slack is a
:func:`~nf_metro.layout.route_reservations.measured_distance`, so a run drawn
flush against a band edge scores as flush rather than as overrunning it by the
floating-point residue from subtracting two canvas coordinates.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from nf_metro.api import prepare_graph, resolve_theme
from nf_metro.layout.constants import COORD_TOLERANCE
from nf_metro.layout.route_plan import build_route_plan_query
from nf_metro.layout.route_reservations import (
    ColumnGapRegion,
    RowGapRegion,
    drawn_corridor_containment,
)
from nf_metro.render.svg import build_observed_render_plan

_ROOT = Path(__file__).parents[1]


def _corpus() -> list[Path]:
    paths = sorted((_ROOT / "examples").rglob("*.mmd"))
    paths += sorted((_ROOT / "tests" / "fixtures").rglob("*.mmd"))
    paths += sorted((_ROOT / "tests" / "fixtures").rglob("*.metro"))
    return paths


_CORPUS = _corpus()

# Fixtures that never reach a route plan at all: fixtures under `invalid/`
# and `nextflow/` are exercised by their own tests for the error they raise,
# and the frozen determinism/topology fixtures abort on a routing invariant
# tracked by other tests. None of them can be held to a claim-consumption
# bound, so their failure to render is not itself a finding here.
KNOWN_NOT_RENDERING = frozenset(
    {
        "tests/fixtures/hash_seed_determinism/seed_15.mmd",
        "tests/fixtures/hash_seed_determinism/seed_41.mmd",
        "tests/fixtures/hash_seed_determinism/seed_77.mmd",
        "tests/fixtures/invalid/backward_feed_rl.mmd",
        "tests/fixtures/invalid/merge_trunk_rightward_source.mmd",
        "tests/fixtures/invalid/mixed_entry_opposing.mmd",
        "tests/fixtures/invalid/mixed_entry_perpendicular.mmd",
        "tests/fixtures/nextflow/duplicate_processes.mmd",
        "tests/fixtures/nextflow/flat_pipeline.mmd",
        "tests/fixtures/nextflow/unquoted_labels.mmd",
        "tests/fixtures/nextflow/variant_calling.mmd",
        "tests/fixtures/nextflow/with_subworkflows.mmd",
        "tests/fixtures/topologies/twoline_fanout_up.mmd",
    }
)


# Claims drawn outside their band by no more than one ``COORD_TOLERANCE``, which
# is this codebase's definition of two coordinates being equal, so they satisfy
# the bound below.  They are enumerated by identity all the same: "zero beyond
# tolerance" is only worth something if the population sitting just inside the
# tolerance cannot grow without anyone noticing.  Adding one here is a decision
# to be argued for, not a side effect.
WITHIN_TOLERANCE_OVERHANGS: frozenset[tuple[str, int, int]] = frozenset()


def _claim_overhangs(path: Path) -> dict[tuple[int, int], tuple[float, str]] | None:
    """*path*'s claims drawn outside their band, or ``None`` if it cannot render.

    Keyed by the claim's own ``(path_rank, segment_rank)`` so a bound names the
    leg, valued by how far outside it is drawn and the geometry that was
    measured.  Every overhang the ledger's own measurement resolves is reported,
    however small, so a caller can hold the ones within tolerance separately from
    the ones beyond it.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
            observed = build_observed_render_plan(graph, resolve_theme(None, graph))
    except Exception:  # noqa: BLE001 - erroring fixtures have their own tests
        return None

    route_plan = observed.route_plan
    if route_plan is None:
        return {}
    query = build_route_plan_query(route_plan)
    polylines = observed.plan.route_polylines
    overhangs: dict[tuple[int, int], tuple[float, str]] = {}
    for reservation in route_plan.reservations:
        if not isinstance(reservation.region, RowGapRegion | ColumnGapRegion):
            continue
        realised = query.realised_reservation(reservation.id)
        if realised is None:
            continue
        for claim in reservation.claims:
            drawn = drawn_corridor_containment(
                reservation, realised, polylines, (claim,)
            )
            short = -min(drawn.negative_side_slack, drawn.positive_side_slack)
            if short <= 0.0:
                continue
            overhangs[claim.path_rank, claim.segment_rank] = (
                short,
                f"{reservation.id} claim {claim.member_id} "
                f"(path {claim.path_rank}, segments {claim.segment_rank}.."
                f"{claim.segment_end_rank}): drawn "
                f"[{drawn.drawn_start:.2f}, {drawn.drawn_end:.2f}] outside band "
                f"[{drawn.band_start:.2f}, {drawn.band_end:.2f}] by {short:.2f}px",
            )
    return overhangs


def _out_of_band_claims(path: Path) -> dict[tuple[int, int], str] | None:
    """*path*'s claims drawn further outside their band than tolerance allows."""
    overhangs = _claim_overhangs(path)
    if overhangs is None:
        return None
    return {
        key: message
        for key, (short, message) in overhangs.items()
        if short > COORD_TOLERANCE
    }


@pytest.mark.parametrize(
    "path", _CORPUS, ids=[str(p.relative_to(_ROOT)) for p in _CORPUS]
)
def test_realised_gap_claims_are_drawn_in_their_reserved_band(path: Path) -> None:
    rel = str(path.relative_to(_ROOT))
    violations = _out_of_band_claims(path)
    if violations is None:
        if rel in KNOWN_NOT_RENDERING:
            pytest.skip("fixture does not render")
        pytest.fail(
            f"{rel} raised while building its render plan. A fixture that stops "
            "rendering cannot be held to a claim-consumption bound; either fix "
            "the regression or add it to KNOWN_NOT_RENDERING with the reason it "
            "cannot render."
        )
    assert not violations, (
        "a claim drawn outside its reserved band was positioned by a "
        "geometry-derived fallback rather than by its reservation:\n"
        + "\n".join(violations[key] for key in sorted(violations))
    )


@pytest.mark.parametrize(
    "path", _CORPUS, ids=[str(p.relative_to(_ROOT)) for p in _CORPUS]
)
def test_claims_drawn_within_one_tolerance_of_their_band_are_the_recorded_ones(
    path: Path,
) -> None:
    """The population sitting just inside the tolerance does not grow unnoticed.

    The bound above is "no claim is drawn more than one ``COORD_TOLERANCE``
    outside its band".  On its own that lets claims accumulate at 0.99 of a
    tolerance without any test reddening, and the guarantee would erode while
    still reading as clean.  This pins the ones that are there by identity.
    """
    rel = str(path.relative_to(_ROOT))
    overhangs = _claim_overhangs(path)
    if overhangs is None:
        pytest.skip("fixture does not render")
    found = {
        (rel, path_rank, segment_rank)
        for (path_rank, segment_rank), (short, _message) in overhangs.items()
        if short <= COORD_TOLERANCE
    }
    expected = {item for item in WITHIN_TOLERANCE_OVERHANGS if item[0] == rel}
    assert found == expected, (
        "the claims drawn within one tolerance of their band are not the ones "
        f"recorded: unrecorded {sorted(found - expected)}, recorded but now "
        f"clean {sorted(expected - found)}. A new one is a claim that stopped "
        "consuming its reservation exactly and got away with it; one that "
        "cleaned up means dropping its WITHIN_TOLERANCE_OVERHANGS entry:\n"
        + "\n".join(
            message
            for (_path_rank, _segment_rank), (short, message) in sorted(
                overhangs.items()
            )
            if short <= COORD_TOLERANCE
        )
    )


def test_every_recorded_within_tolerance_overhang_names_a_corpus_fixture() -> None:
    """A stale entry would silently excuse a fixture that no longer exists."""
    corpus = {str(item.relative_to(_ROOT)) for item in _CORPUS}
    named = {item[0] for item in WITHIN_TOLERANCE_OVERHANGS}
    assert named <= corpus, named - corpus
