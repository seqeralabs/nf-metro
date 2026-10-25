"""The corridor-cohort plan sizes the corners its own patches move.

A cohort patch moves member segments, so the corners at either end of a moved
segment re-form.  The compile sizes them with the freeze's own corner rules,
run on copies at the patched points: a moved corner nests concentrically with
the corners it turns beside, an unpatched mate in the same corner cohort is
re-sized with it, and a compile that moves no corner leaves every corner as it
found it.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import cohort_shadow_oracle
import pytest

from nf_metro.api import render_string
from nf_metro.layout.routing import corridor_cohort_integration as cci
from nf_metro.layout.routing import member_geometry
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.member_geometry import _corner_state

ROOT = Path(__file__).resolve().parents[1]
EdgeKey = tuple[str, str, str]


class _Compile:
    def __init__(self, targets: Sequence[cci.CorridorCohortTarget]) -> None:
        self.targets = tuple(targets)
        self.states_before = {
            id(target.route): _corner_state(target.route) for target in targets
        }
        self.plan: cci.CorridorCohortPlan | None = None

    @property
    def patches(self) -> dict[EdgeKey, cci._RoutePatch]:
        assert self.plan is not None
        return {_edge_key(patch.route): patch for patch in self.plan.route_patches}

    @property
    def planned_member_keys(self) -> set[EdgeKey]:
        assert self.plan is not None
        return {item.edge_key for item in self.plan.allocations} | {
            item.edge_key for item in self.plan.landings
        }


def _edge_key(route: RoutedPath) -> EdgeKey:
    return route.edge.source, route.edge.target, route.line_id


def _patch_state(patch: cci._RoutePatch) -> tuple[Any, ...]:
    return (
        None if patch.radii is None else tuple(patch.radii),
        tuple(sorted(patch.corner_offsets.items())),
        tuple(sorted(patch.corner_bases.items())),
        patch.source_turnout,
    )


def _moves_a_point(patch: cci._RoutePatch) -> bool:
    return any(
        tuple(old) != tuple(new)
        for old, new in zip(patch.route.points, patch.points, strict=True)
    )


def _compiles(monkeypatch: pytest.MonkeyPatch, relative_path: str) -> list[_Compile]:
    compiles: list[_Compile] = []
    compile_plan = member_geometry.compile_corridor_cohort_plan

    def capture(
        ledger: Any, targets: Sequence[cci.CorridorCohortTarget], **kwargs: Any
    ) -> cci.CorridorCohortPlan:
        record = _Compile(targets)
        record.plan = compile_plan(ledger, targets, **kwargs)
        compiles.append(record)
        return record.plan

    monkeypatch.setattr(member_geometry, "compile_corridor_cohort_plan", capture)
    path = ROOT / relative_path
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        render_string(path.read_text(), source_dir=str(path.parent))
    assert compiles, f"{relative_path} compiled no corridor cohort plan"
    return compiles


def _landing_arc_centre(
    points: Sequence[tuple[float, float]], radius: float
) -> tuple[float, float]:
    before, corner, after = points[-3:]
    incoming = _unit(before, corner)
    outgoing = _unit(corner, after)
    return (
        corner[0] + radius * (outgoing[0] - incoming[0]),
        corner[1] + radius * (outgoing[1] - incoming[1]),
    )


def _unit(start: tuple[float, float], end: tuple[float, float]) -> tuple[float, float]:
    length = math.dist(start, end)
    return (end[0] - start[0]) / length, (end[1] - start[1]) / length


def test_moved_landing_corner_nests_concentrically_with_its_mate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both feeders into benchmarking's right entry are moved by the patch, the
    truth descent to x=1407 and the test descent to x=1411, so their landing
    corners turn 4px apart: the outer test corner takes radius 14 around the
    inner truth corner's 10, and the two arcs share one centre."""
    truth_key = ("__junction_14", "benchmarking__entry_right_11", "truth")
    test_key = ("__junction_15", "benchmarking__entry_right_11", "test")
    compiles = _compiles(monkeypatch, "examples/variantbenchmarking.mmd")
    moved = [
        record
        for record in compiles
        if truth_key in record.patches and _moves_a_point(record.patches[truth_key])
    ]
    assert moved

    for record in moved:
        truth = record.patches[truth_key]
        test = record.patches[test_key]
        assert truth.points[-2] == (1407.0, 531.0)
        assert test.points[-2] == (1411.0, 535.0)
        assert truth.radii[-1] == pytest.approx(10.0)
        assert test.radii[-1] == pytest.approx(14.0)
        assert (
            math.dist(
                _landing_arc_centre(truth.points, truth.radii[-1]),
                _landing_arc_centre(test.points, test.radii[-1]),
            )
            <= 1.0
        )


def test_unpatched_mate_in_a_moved_corner_cohort_is_resized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The patch moves m3's descent into z2's left entry, re-forming the landing
    cohort the exit-fed m1 and m2 leads turn in.  Neither lead is allocated, so
    each takes a radius-only patch: its points stay put, its landing corner
    re-nests around the moved one."""
    mate_radii = {
        ("z1__exit_right_1", "z2__entry_left_3", "m1"): (10.0, 18.0),
        ("z1__exit_right_1", "z2__entry_left_3", "m2"): (14.0, 14.0),
    }
    compiles = _compiles(
        monkeypatch, "examples/topologies/entry_cohort_interleaved_fixed_lane.mmd"
    )
    moved = [
        record
        for record in compiles
        if any(_moves_a_point(patch) for patch in record.patches.values())
    ]
    assert moved

    for record in moved:
        for key, radii in mate_radii.items():
            assert key not in record.planned_member_keys
            mate = record.patches.get(key)
            assert mate is not None, f"{key} has no radius-only patch"
            assert mate.points == list(mate.route.points)
            assert tuple(mate.radii) == pytest.approx(radii)
            assert tuple(mate.route.curve_radii) != pytest.approx(radii)


@pytest.mark.parametrize(
    "relative_path",
    [
        "examples/topologies/disjoint_sameline_trunks.mmd",
        "tests/fixtures/curve_invariant_repros/inter_row_corridor_overflow.mmd",
    ],
)
def test_compile_moving_no_corner_keeps_every_corner(
    monkeypatch: pytest.MonkeyPatch, relative_path: str
) -> None:
    """The corner rules read the radii they size, so sizing a cohort no patch
    moved would drift it: a compile whose patches move no point keeps every
    patched route's radii, corner inputs and source turnout as they are."""
    still = [
        record
        for record in _compiles(monkeypatch, relative_path)
        if record.patches
        and not any(_moves_a_point(patch) for patch in record.patches.values())
    ]
    assert still

    for record in still:
        for key, patch in record.patches.items():
            assert _patch_state(patch) == _corner_state(patch.route), key


@pytest.mark.parametrize(
    "relative_path",
    [
        "examples/variantbenchmarking.mmd",
        "examples/topologies/entry_cohort_interleaved_fixed_lane.mmd",
    ],
)
def test_sizing_leaves_the_live_routes_untouched(
    monkeypatch: pytest.MonkeyPatch, relative_path: str
) -> None:
    """Two corner rules write into a route's own radius list and corner-input
    dicts, so the sizing runs on copies: compiling, even when it sizes a
    corner, changes no live route; only publishing a plan does."""
    sized: list[tuple[cci.CorridorCornerSizing, ...]] = []
    unchanged: list[bool] = []
    size = member_geometry._size_patched_corners

    def check_isolation(
        population: Sequence[RoutedPath], *args: Any
    ) -> tuple[cci.CorridorCornerSizing, ...]:
        before = [(list(route.points), _corner_state(route)) for route in population]
        result = size(population, *args)
        unchanged.append(
            before
            == [(list(route.points), _corner_state(route)) for route in population]
        )
        sized.append(result)
        return result

    monkeypatch.setattr(member_geometry, "_size_patched_corners", check_isolation)
    compiles = _compiles(monkeypatch, relative_path)

    assert any(sized)
    assert all(unchanged)
    for record in compiles:
        for target in record.targets:
            assert (
                _corner_state(target.route) == record.states_before[id(target.route)]
            ), _edge_key(target.route)


def _sizing_calls(
    monkeypatch: pytest.MonkeyPatch, relative_path: str
) -> list[tuple[Any, ...]]:
    """The arguments of every sizer call on *relative_path* that sized a corner."""
    calls: list[tuple[Any, ...]] = []
    size = member_geometry._size_patched_corners

    def record_call(*args: Any) -> tuple[cci.CorridorCornerSizing, ...]:
        result = size(*args)
        if result:
            calls.append(args)
        return result

    monkeypatch.setattr(member_geometry, "_size_patched_corners", record_call)
    _compiles(monkeypatch, relative_path)
    monkeypatch.setattr(member_geometry, "_size_patched_corners", size)
    assert calls, f"{relative_path} sized no corner"
    return calls


def _sizings_by_edge(
    sizings: Sequence[cci.CorridorCornerSizing],
) -> dict[EdgeKey, tuple[Any, ...]]:
    return {
        _edge_key(sizing.route): (
            sizing.radii,
            sizing.corner_offsets,
            sizing.corner_bases,
            sizing.source_turnout,
        )
        for sizing in sizings
    }


@pytest.mark.parametrize(
    "relative_path",
    [
        "examples/variantbenchmarking.mmd",
        "examples/topologies/entry_cohort_interleaved_fixed_lane.mmd",
    ],
)
def test_immutable_route_is_never_sized(
    monkeypatch: pytest.MonkeyPatch, relative_path: str
) -> None:
    """A convergence context route is fixed before the cohorts compile, so a
    corner rule may not re-size it: no patch targets one, and a move that would
    change an immutable route's radius is left unsized altogether, its mutable
    cohort mates included."""
    for record in _compiles(monkeypatch, relative_path):
        immutable = {
            id(target.route) for target in record.targets if not target.mutable
        }
        assert not [
            key for key, patch in record.patches.items() if id(patch.route) in immutable
        ]

    for population, mutable, *rest in _sizing_calls(monkeypatch, relative_path):
        size = member_geometry._size_patched_corners
        resized = [
            sizing
            for sizing in size(population, mutable, *rest)
            if sizing.radii != tuple(sizing.route.curve_radii or ())
        ]
        assert resized
        assert size(population, mutable - {id(resized[0].route)}, *rest) == ()


def test_sizing_leaves_cohorts_no_moved_corner_touches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The corner rules read the radii they size, so they would re-size any
    cohort whose corners sit off its own sizing.  Preprocess's exit fan into
    normalization shares no cohort with the moved benchmarking descents: with
    its test lead's source corner seated 4px off, the rules re-size that fan,
    yet the move adopts exactly what it adopts with the fan left as drawn."""
    distractor = ("preprocess__exit_right_1", "normalization__entry_left_7", "test")
    population, mutable, *rest = _sizing_calls(
        monkeypatch, "examples/variantbenchmarking.mmd"
    )[0]
    (index,) = [
        rank for rank, route in enumerate(population) if _edge_key(route) == distractor
    ]
    original = population[index]
    off_sizing = member_geometry._corner_sizing_copy(original)
    assert off_sizing.curve_radii is not None
    off_sizing.curve_radii[0] += 4.0
    perturbed = [*population[:index], off_sizing, *population[index + 1 :]]
    perturbed_mutable = (mutable - {id(original)}) | {id(off_sizing)}
    size = member_geometry._size_patched_corners

    with monkeypatch.context() as unscoped:
        unscoped.setattr(
            member_geometry,
            "_corner_sizing_scope",
            lambda copies, *_args: frozenset(id(copy) for copy in copies),
        )
        assert distractor in _sizings_by_edge(size(perturbed, perturbed_mutable, *rest))

    assert _sizings_by_edge(size(perturbed, perturbed_mutable, *rest)) == (
        _sizings_by_edge(size(population, mutable, *rest))
    )


def test_oracle_reports_a_sized_corner_drawn_off_its_radius() -> None:
    """The shadow oracle holds each corner the plan sized, a radius-only mate's
    included, to its sized radius on the drawn route."""
    mate = ("z1__exit_right_1", "z2__entry_left_3", "m2")
    drawn = cast(
        RoutedPath,
        SimpleNamespace(
            edge=SimpleNamespace(source=mate[0], target=mate[1]),
            line_id=mate[2],
            curve_radii=[14.0, 16.0],
        ),
    )

    mismatches = cohort_shadow_oracle.sized_radius_mismatches

    assert mismatches([drawn], {(mate, 1): 16.0}) == []
    (mismatch,) = mismatches([drawn], {(mate, 1): 14.0})
    assert "planned radius 14.0 drawn 16.0" in mismatch
    (undrawn,) = mismatches([], {(mate, 1): 14.0})
    assert "drawn None" in undrawn
