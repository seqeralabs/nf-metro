"""The corridor-cohort compile seats layouts the router already draws.

Each fixture is a minimised generated map.  Every test renders it through the
default ``render_string`` path and asserts the seating property the compile
must establish there, not only that the render completes.
"""

from __future__ import annotations

import inspect
import warnings
from pathlib import Path
from typing import Any

import pytest
from cohort_shadow_oracle import shadow_observation
from conftest import curve_abort_softened, is_curve_abort_fixture

from nf_metro.api import prepare_graph, render_string
from nf_metro.layout.phases.guards import FoldThresholdError
from nf_metro.layout.route_reservations import ColumnGapRegion
from nf_metro.layout.routing import corridor_cohort_integration as cci
from nf_metro.layout.routing import member_geometry
from nf_metro.layout.routing.context import _RoutingCtx
from nf_metro.layout.routing.corridor_cohorts import (
    CorridorAllocationProblem,
    CorridorAllocationResult,
    CorridorAllocationStatus,
    CorridorCornerOrderTier,
)
from nf_metro.layout.settlement_demand import BoundaryClearanceRequirement
from nf_metro.parser.model import PermissiveGuardWarning
from nf_metro.render import svg

REGRESSIONS = Path(__file__).parent / "fixtures" / "regressions"
DATA = Path(__file__).parent / "data"


class _Captured:
    def __init__(self) -> None:
        self.compiles = 0
        self.plans: list[cci.CorridorCohortPlan] = []
        self.compiled: list[tuple[_RoutingCtx, cci.CorridorCohortPlan]] = []
        self.ctx: _RoutingCtx | None = None
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
    fixture: str | Path,
    layout_options: dict[str, Any] | None = None,
) -> _Captured:
    captured = _Captured()
    compile_cohorts = member_geometry._compile_corridor_cohorts
    compile_cohorts_signature = inspect.signature(compile_cohorts)
    compile_plan = member_geometry.compile_corridor_cohort_plan
    granted_ids = svg._corridor_grant_moved_plan_ids
    aperture_requirements = member_geometry._corridor_cohort_aperture_requirements

    def capture_ctx(*args: Any, **kwargs: Any) -> Any:
        bound = compile_cohorts_signature.bind(*args, **kwargs)
        captured.ctx = bound.arguments["ctx"]
        return compile_cohorts(*args, **kwargs)

    def capture_plan(*args: Any, **kwargs: Any) -> cci.CorridorCohortPlan:
        captured.compiles += 1
        plan = compile_plan(*args, **kwargs)
        captured.plans.append(plan)
        assert captured.ctx is not None
        captured.compiled.append((captured.ctx, plan))
        return plan

    def capture_granted(plan: Any) -> frozenset[str]:
        ids = granted_ids(plan)
        captured.granted.update(ids)
        return ids

    def capture_apertures(*args: Any) -> tuple[BoundaryClearanceRequirement, ...]:
        requirements = aperture_requirements(*args)
        captured.apertures.extend(requirements)
        return requirements

    monkeypatch.setattr(member_geometry, "_compile_corridor_cohorts", capture_ctx)
    monkeypatch.setattr(member_geometry, "compile_corridor_cohort_plan", capture_plan)
    monkeypatch.setattr(
        member_geometry, "_corridor_cohort_aperture_requirements", capture_apertures
    )
    monkeypatch.setattr(svg, "_corridor_grant_moved_plan_ids", capture_granted)
    path = fixture if isinstance(fixture, Path) else REGRESSIONS / fixture
    with warnings.catch_warnings(), curve_abort_softened(is_curve_abort_fixture(path)):
        warnings.simplefilter("ignore")
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
        "cohort_pinned_member_past_fixed_lane.mmd",
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
    for equality in problem.equalities:
        gap = abs(at[equality.right_member_id] - at[equality.left_member_id])
        assert gap == pytest.approx(abs(equality.delta), abs=tolerance), equality
    for interval in problem.forbidden_intervals:
        coordinate = at[interval.member_id]
        assert not (
            interval.minimum_coordinate + tolerance
            < coordinate
            < interval.maximum_coordinate - tolerance
        ), interval
    for domain in problem.domains:
        if domain.minimum_coordinate is not None:
            assert at[domain.member_id] >= domain.minimum_coordinate - tolerance
        if domain.maximum_coordinate is not None:
            assert at[domain.member_id] <= domain.maximum_coordinate + tolerance
    for fixed in problem.fixed_equalities:
        expected = obstacles[fixed.obstacle_id].realised_coordinate + fixed.delta
        assert at[fixed.member_id] == pytest.approx(expected, abs=tolerance), fixed


def test_lanes_turning_together_seat_in_their_corner_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``m4`` and ``m2`` descend together and turn onto the run to ``z1``'s
    bottom entry beside ``m2``'s branch to ``z3``: every compile seats that run
    in the order the descent turns it."""
    captured = _render(monkeypatch, "cohort_corner_turn_order.mmd")
    cornered = [
        (problem, result)
        for component in captured.components
        for problem, result in zip(component.problems, component.results)
        if problem.corner_orders
    ]
    assert cornered
    for problem, result in cornered:
        assert result.corner_tier is CorridorCornerOrderTier.CORNER_ORDERS
        at = dict(result.allocations)
        for order in problem.corner_orders:
            assert at[order.lower_member_id] + order.distance <= (
                at[order.upper_member_id] + 1e-6
            ), order


@pytest.mark.parametrize(
    "fixture",
    [
        "cohort_region_floor_lift.mmd",
        "cohort_region_exit_riser.mmd",
        "cohort_region_beside_section.mmd",
    ],
)
def test_corridor_hold_draws_every_lane_where_the_plan_seats_it(fixture: str) -> None:
    """Each map's solve, left free of its corridor bands, seats a lane the
    corridor hold then draws elsewhere; seated inside them, the drawn routes
    match the plan."""
    path = DATA / fixture
    with warnings.catch_warnings(), shadow_observation():
        warnings.simplefilter("ignore")
        render_string(path.read_text(), source_dir=str(path.parent))


def test_compile_seats_no_lane_through_a_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red's run from its junction to ``upper_right`` crosses the gap beside
    ``middle``, whose reserved band keeps it out of ``middle`` even where a
    fixed footprint denies it the coordinate it was drawn at."""
    captured = _render(monkeypatch, DATA / "cohort_region_beside_section.mmd")
    assert captured.compiled
    crossings = []
    for ctx, plan in captured.compiled:
        for allocation in plan.allocations:
            run = sorted((allocation.longitudinal_start, allocation.longitudinal_end))
            for section in ctx.graph.sections.values():
                box = (
                    (section.bbox_x, section.bbox_x + section.bbox_w),
                    (section.bbox_y, section.bbox_y + section.bbox_h),
                )
                across, along = box[allocation.axis], box[1 - allocation.axis]
                if (
                    across[0] < allocation.coordinate < across[1]
                    and min(run[1], along[1]) - max(run[0], along[0]) > 1.0
                ):
                    crossings.append((allocation.claim_id, section.id))
    assert not crossings


_JUNCTION_FED_GAP = "cohort_junction_fed_gap_boundary.mmd"
_JUNCTION_FED_GAP_OPTIONS = {
    "compact_offsets": False,
    "stroke_scale": 1.5,
    "line_order": "span",
}


def test_junction_fed_gap_names_both_facing_sections() -> None:
    """The junction-fed claim and its blocker touch only ``sec4``, in column
    0, so neither faces the column 1|2 gap; both sides come from the sections
    at the gap's own columns sharing ``sec4``'s rows."""
    path = REGRESSIONS / _JUNCTION_FED_GAP
    graph = prepare_graph(
        path.read_text(),
        layout_options=_JUNCTION_FED_GAP_OPTIONS,
        source_dir=str(path.parent),
    )
    away = (graph.sections["sec4"],)

    negative, positive = member_geometry._corridor_boundary_sections(
        graph, ColumnGapRegion(1, 2), away, away
    )

    assert [section.id for section in negative] == ["sec3"]
    assert [section.id for section in positive] == ["sec2"]


def test_junction_fed_gap_seats_inside_its_regions_without_an_aperture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A region floor lifts the lanes above it inside their own regions, so
    the gap seats as drawn and asks for no widening."""
    captured = _render(monkeypatch, _JUNCTION_FED_GAP, _JUNCTION_FED_GAP_OPTIONS)
    assert captured.components
    assert not captured.apertures
    assert not [
        component.component_id
        for component in captured.components
        if component.status is CorridorAllocationStatus.FAILURE
    ]


def test_vertical_drops_land_on_distinct_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _render(monkeypatch, "cohort_tb_drop_drawn_landing.mmd")
    assert captured.components
    assert not captured.overlaid


def test_approach_ordered_entry_slots_do_not_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _render(monkeypatch, "entry_slot_overlay_from_exit_forward.mmd")
    assert captured.components
    assert not captured.overlaid


def test_overlaid_entry_slots_keep_drawn_geometry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _render(monkeypatch, "entry_slot_overlay_tied_feeders.mmd")
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


def test_fold_compressed_cohort_refusal_names_the_fold_threshold() -> None:
    """A cohort the compile refuses on a grid the fold threshold compressed is
    an authoring error, so it reports the threshold to raise."""
    path = REGRESSIONS / "cohort_fold_compressed.mmd"
    with pytest.raises(FoldThresholdError, match="Raise --fold-threshold"):
        render_string(
            path.read_text(),
            layout_options={
                "fold_threshold": 4,
                "section_x_gap": 20.0,
                "line_order": "span",
            },
            source_dir=str(path.parent),
        )
