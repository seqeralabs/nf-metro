"""The corridor-cohort compile reads peel-off risers on their depth-earned slots.

After emission, ``_reconcile_port_peeloff_risers`` re-seats every peel-off
riser the freeze leaves unowned onto the slot its settled trunk depth earns.
The compile reads a snapshot in which that rule has already run, on copies, so
the riser coordinate it accepts is the one drawn while no live route moves
before a plan is published.
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from conftest import curve_abort_softened, is_curve_abort_fixture

from nf_metro.api import render_string
from nf_metro.layout.routing import corridor_cohort_integration as cci
from nf_metro.layout.routing import member_geometry
from nf_metro.layout.routing.common import RoutedPath, port_peeloff_tail
from nf_metro.render import svg

ROOT = Path(__file__).resolve().parents[1]

EdgeKey = tuple[str, str, str]


def _edge_key(route: RoutedPath) -> EdgeKey:
    return route.edge.source, route.edge.target, route.line_id


def _render(path: Path) -> None:
    with warnings.catch_warnings(), curve_abort_softened(is_curve_abort_fixture(path)):
        warnings.simplefilter("ignore")
        render_string(path.read_text(), source_dir=str(path.parent))


@pytest.mark.parametrize(
    ("relative_path", "expected"),
    [
        (
            "examples/topologies/fan_over_top_reach_order.mmd",
            {
                ("__junction_4", "c__entry_right_3", "l1"): 690.0,
                ("b__exit_right_1", "c__entry_right_3", "l2"): 686.0,
            },
        ),
        (
            "tests/fixtures/curve_invariant_repros/fan_over_top_flow_feeder.mmd",
            {
                ("__junction_6", "sec3__entry_right_5", "l1"): 1390.0,
                ("sec2__exit_right_2", "sec3__entry_right_5", "l2"): 1386.0,
            },
        ),
        (
            "tests/fixtures/curve_invariant_repros/inter_row_corridor_overflow.mmd",
            {
                ("__junction_12", "calling__entry_left_8", "la"): 34.0,
                ("__junction_12", "calling__entry_left_8", "lc"): 30.0,
            },
        ),
        (
            "tests/fixtures/regressions/riboseq_row_trunk_subrun.mmd",
            {
                ("__junction_12", "orf_calling__entry_left_8", "riboseq"): 34.0,
                ("__junction_12", "orf_calling__entry_left_8", "tiseq"): 30.0,
            },
        ),
    ],
)
def test_compiled_peeloff_riser_agrees_with_the_drawn_riser(
    monkeypatch: pytest.MonkeyPatch, relative_path: str, expected: dict[EdgeKey, float]
) -> None:
    """The cohort-final compile accepts each peel-off riser where it is drawn."""
    plans: list[cci.CorridorCohortPlan] = []
    drawn: dict[EdgeKey, float] = {}
    compile_plan = member_geometry.compile_corridor_cohort_plan
    drawable = svg._assert_settled_geometry_is_drawable

    def capture_plan(*args: Any, **kwargs: Any) -> cci.CorridorCohortPlan:
        plan = compile_plan(*args, **kwargs)
        plans.append(plan)
        return plan

    def capture_drawn(graph: Any, settled: Any) -> None:
        drawable(graph, settled)
        drawn.update(
            {
                _edge_key(route): tail.peel_x
                for route in settled.routes
                if (tail := port_peeloff_tail(route)) is not None
            }
        )

    monkeypatch.setattr(member_geometry, "compile_corridor_cohort_plan", capture_plan)
    monkeypatch.setattr(svg, "_assert_settled_geometry_is_drawable", capture_drawn)
    _render(ROOT / relative_path)

    assert plans
    risers = {
        allocation.edge_key: allocation.coordinate
        for allocation in plans[-1].allocations
        if allocation.axis == 0
        and allocation.edge_key in expected
        and allocation.segment_rank == 3
    }
    assert risers == expected
    assert {key: drawn[key] for key in expected} == expected


def _route_state(route: RoutedPath) -> tuple[Any, ...]:
    return (
        tuple(map(tuple, route.points)),
        None if route.curve_radii is None else tuple(route.curve_radii),
        tuple(sorted(route.concentric_corner_offsets_by_segment.items())),
        tuple(sorted(route.concentric_corner_bases_by_segment.items())),
        route.source_turnout,
        route.route_system_owned_segment_ranks,
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        "examples/topologies/fan_over_top_reach_order.mmd",
        "tests/fixtures/curve_invariant_repros/inter_row_corridor_overflow.mmd",
        "tests/fixtures/regressions/riboseq_row_trunk_subrun.mmd",
    ],
)
def test_peeloff_snapshot_leaves_the_live_routes_untouched(
    monkeypatch: pytest.MonkeyPatch, relative_path: str
) -> None:
    """Seating the snapshot's risers writes no live route, and every patch the
    compile returns, a re-sized mate's included, names a live route."""
    calls: list[tuple[bool, bool]] = []
    compile_cohorts = member_geometry._compile_corridor_cohorts

    def check(
        ledger: Any,
        candidates: Sequence[Any],
        context_candidates: Sequence[Any],
        scaffold: Any,
        ctx: Any,
        **kwargs: Any,
    ) -> Any:
        live = {
            id(route): route
            for route in (
                *(candidate.route for candidate in candidates),
                *(candidate.route for candidate in context_candidates),
                *(target.route for target in kwargs.get("additional_targets", ())),
                *kwargs.get("sizing_population", ()),
            )
        }
        before = {route_id: _route_state(route) for route_id, route in live.items()}
        result = compile_cohorts(
            ledger, candidates, context_candidates, scaffold, ctx, **kwargs
        )
        plan = result[0]
        patches = () if plan is None else plan.route_patches
        calls.append(
            (
                all(_route_state(route) == before[i] for i, route in live.items()),
                all(live.get(id(patch.route)) is patch.route for patch in patches),
            )
        )
        return result

    monkeypatch.setattr(member_geometry, "_compile_corridor_cohorts", check)
    _render(ROOT / relative_path)

    assert calls
    assert all(untouched for untouched, _live in calls)
    assert all(live for _untouched, live in calls)
