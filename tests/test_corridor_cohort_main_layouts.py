"""The corridor-cohort compile seats layouts the router already draws.

Each fixture is a minimised generated map.  Every test renders it through the
default ``render_string`` path and asserts the seating property the compile
must establish there, not only that the render completes.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import pytest

from nf_metro.api import render_string
from nf_metro.layout.routing import corridor_cohort_integration as cci
from nf_metro.layout.routing import member_geometry
from nf_metro.layout.routing.corridor_cohorts import (
    CorridorAllocationProblem,
    CorridorAllocationResult,
    CorridorAllocationStatus,
)
from nf_metro.layout.settlement_demand import BoundaryClearanceRequirement
from nf_metro.parser.model import PermissiveGuardWarning
from nf_metro.render import svg

REGRESSIONS = Path(__file__).parent / "fixtures" / "regressions"


class _Captured:
    def __init__(self) -> None:
        self.compiles = 0
        self.plans: list[cci.CorridorCohortPlan] = []
        self.granted: set[str] = set()
        self.apertures: list[BoundaryClearanceRequirement] = []

    @property
    def components(self) -> list[cci.CorridorCohortComponentPlan]:
        return [component for plan in self.plans for component in plan.components]

    @property
    def overlaid(self) -> list[cci.CorridorCohortComponentPlan]:
        return [
            component
            for component in self.components
            if component.compatibility is not None
            and component.compatibility.reason
            is cci.CorridorCompatibilityReason.OVERLAID_ENDPOINT_SLOTS
        ]


def _render(
    monkeypatch: pytest.MonkeyPatch,
    fixture: str,
    layout_options: dict[str, Any] | None = None,
) -> _Captured:
    captured = _Captured()
    compile_plan = member_geometry.compile_corridor_cohort_plan
    granted_ids = svg._corridor_grant_moved_plan_ids
    aperture_requirements = member_geometry._corridor_cohort_aperture_requirements

    def capture_plan(*args: Any, **kwargs: Any) -> cci.CorridorCohortPlan:
        captured.compiles += 1
        plan = compile_plan(*args, **kwargs)
        captured.plans.append(plan)
        return plan

    def capture_granted(plan: Any) -> frozenset[str]:
        ids = granted_ids(plan)
        captured.granted.update(ids)
        return ids

    def capture_apertures(*args: Any) -> tuple[BoundaryClearanceRequirement, ...]:
        requirements = aperture_requirements(*args)
        captured.apertures.extend(requirements)
        return requirements

    monkeypatch.setattr(member_geometry, "compile_corridor_cohort_plan", capture_plan)
    monkeypatch.setattr(
        member_geometry, "_corridor_cohort_aperture_requirements", capture_apertures
    )
    monkeypatch.setattr(svg, "_corridor_grant_moved_plan_ids", capture_granted)
    path = REGRESSIONS / fixture
    render_string(
        path.read_text(), layout_options=layout_options, source_dir=str(path.parent)
    )
    return captured


@pytest.mark.parametrize(
    "fixture",
    [
        "cohort_pinned_lane_forced_crossing.mmd",
        "cohort_line_split_branches.mmd",
        "cohort_reverse_frame_seating.mmd",
    ],
)
def test_compile_seats_every_component(
    monkeypatch: pytest.MonkeyPatch, fixture: str
) -> None:
    captured = _render(monkeypatch, fixture)
    assert captured.compiles == len(captured.plans)
    planned = [
        component
        for component in captured.components
        if component.status is CorridorAllocationStatus.PLANNED
    ]
    assert planned
    assert not [
        component.component_id
        for component in captured.components
        if component.status is CorridorAllocationStatus.FAILURE
    ]
    for component in planned:
        for problem, result in zip(component.problems, component.results):
            _assert_allocation_satisfies(problem, result)


def _assert_allocation_satisfies(
    problem: CorridorAllocationProblem, result: CorridorAllocationResult
) -> None:
    obstacles = {obstacle.obstacle_id: obstacle for obstacle in problem.obstacles}
    at = {
        **{key: obstacle.realised_coordinate for key, obstacle in obstacles.items()},
        **dict(result.allocations),
    }
    tolerance = 1e-6
    for separation in problem.separations:
        gap = abs(at[separation.left_member_id] - at[separation.right_member_id])
        assert gap >= separation.distance - tolerance, separation
    for directed in problem.directed_separations:
        assert (
            at[directed.lower_member_id] + directed.distance
            <= at[directed.upper_member_id] + tolerance
        ), directed
    for domain in problem.domains:
        if domain.minimum_coordinate is not None:
            assert at[domain.member_id] >= domain.minimum_coordinate - tolerance
        if domain.maximum_coordinate is not None:
            assert at[domain.member_id] <= domain.maximum_coordinate + tolerance
    for fixed in problem.fixed_equalities:
        expected = obstacles[fixed.obstacle_id].realised_coordinate + fixed.delta
        assert at[fixed.member_id] == pytest.approx(expected, abs=tolerance), fixed


def test_junction_fed_gap_names_both_facing_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _render(
        monkeypatch,
        "cohort_junction_fed_gap_boundary.mmd",
        {"compact_offsets": False, "stroke_scale": 1.5, "line_order": "span"},
    )
    assert captured.apertures
    assert all(
        requirement.negative_section_ids and requirement.positive_section_ids
        for requirement in captured.apertures
    )


def test_vertical_drops_land_on_distinct_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _render(monkeypatch, "cohort_tb_drop_drawn_landing.mmd")
    assert captured.components
    assert not captured.overlaid


def test_overlaid_entry_slots_keep_drawn_geometry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _render(monkeypatch, "entry_slot_overlay_from_exit_forward.mmd")
    overlaid = captured.overlaid
    assert overlaid
    assert all(
        component.status is CorridorAllocationStatus.COMPATIBILITY
        and not component.problems
        and not component.allocations
        for component in overlaid
    )


def test_grant_is_proven_against_its_ungranted_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant moves a trunk beside runs pre-freeze seating moves too; the
    render's grant guard passes only when it diffs against the ungranted
    observation."""
    captured = _render(monkeypatch, "cohort_grant_beside_seated_runs.mmd")
    assert captured.granted


def test_grant_keeps_its_trunk_inside_the_reserved_corridor() -> None:
    """A trunk's own covered continuation is the recipe's to move, so it does
    not push the granted trunk out of the row-gap corridor its reservation
    realises, at any stroke scale."""
    path = Path(__file__).parents[1] / "examples/topologies"
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        render_string(
            (path / "merge_feeder_shared_channel_gap.mmd").read_text(),
            layout_options={"stroke_scale": 2.0},
            source_dir=str(path),
        )
    assert not [
        str(item.message)
        for item in seen
        if issubclass(item.category, PermissiveGuardWarning)
    ]
