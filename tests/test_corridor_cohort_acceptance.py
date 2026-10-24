"""The accepted corridor-cohort plan is persisted once and read, never recompiled.

``_settle_render_geometry`` runs the corridor-cohort compiler at exactly one
observation, the post-settlement aperture pass.  The plan that pass accepts is
stored on the ``RoutePlan`` it publishes; every later observation in the same
settle call reads it.
"""

from __future__ import annotations

import copy
import json
import math
import os
import pickle
import subprocess
import sys
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from packed_cell_aperture import PACKED_CELL, direct_assembled_past_reference

from nf_metro.api import prepare_graph, resolve_theme
from nf_metro.layout import route_plan as route_plan_module
from nf_metro.layout.constants import COORD_TOLERANCE_FINE
from nf_metro.layout.route_plan import RoutePlan, serialize_route_plan
from nf_metro.layout.routing import (
    core,
    corridor_cohort_integration,
    member_geometry,
    planning,
)
from nf_metro.render import svg

ROOT = Path(__file__).parents[1]

MERGE_RIGHT_ENTRY = "examples/topologies/merge_right_entry.mmd"

GRANTED_FIXTURES = (
    MERGE_RIGHT_ENTRY,
    "examples/topologies/plan_owned_distinct_lane_separation.mmd",
    "tests/fixtures/curve_invariant_repros/trunk_lane_taken_by_turning_line.mmd",
    "tests/fixtures/regressions/perp_entry_column_merge_feeder.mmd",
    "tests/fixtures/regressions/plan_owned_distinct_lane_separation_reordered.mmd",
)


@dataclass
class _SettleCall:
    observations: int = 0
    compile_observations: list[int] = field(default_factory=list)
    solve_observations: list[int] = field(default_factory=list)
    executions: list[planning.RoutePlanningExecution] = field(default_factory=list)

    @property
    def accepting(self) -> list[planning.RoutePlanningExecution]:
        return [
            execution
            for execution in self.executions
            if execution.accepted_corridor_cohort_plan is not None
        ]


@dataclass
class _Trace:
    calls: list[_SettleCall] = field(default_factory=list)
    compiling_plans: list[RoutePlan] = field(default_factory=list)


@contextmanager
def _traced(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Trace]:
    """Count compiles, solves and planning passes per settle call."""
    trace = _Trace()
    stack: list[_SettleCall] = []

    real_settle = svg._settle_render_geometry

    def settle(*args, **kwargs):
        call = _SettleCall()
        stack.append(call)
        trace.calls.append(call)
        try:
            return real_settle(*args, **kwargs)
        finally:
            stack.pop()

    real_observe = svg.observe_route_edges_centred

    def observe(*args, **kwargs):
        if stack:
            stack[-1].observations += 1
        return real_observe(*args, **kwargs)

    real_compile = member_geometry._compile_corridor_cohorts

    def compile_cohorts(*args, **kwargs):
        stack[-1].compile_observations.append(stack[-1].observations)
        return real_compile(*args, **kwargs)

    real_solve = corridor_cohort_integration.solve_corridor_cohorts

    def solve(*args, **kwargs):
        stack[-1].solve_observations.append(stack[-1].observations)
        return real_solve(*args, **kwargs)

    real_prepare = planning.prepare_route_system_planning

    def prepare(*args, **kwargs):
        execution = real_prepare(*args, **kwargs)
        stack[-1].executions.append(execution)
        return execution

    real_moved = svg._corridor_grant_moved_plan_ids

    def moved(plan):
        trace.compiling_plans.append(plan)
        return real_moved(plan)

    monkeypatch.setattr(svg, "_settle_render_geometry", settle)
    monkeypatch.setattr(svg, "observe_route_edges_centred", observe)
    monkeypatch.setattr(member_geometry, "_compile_corridor_cohorts", compile_cohorts)
    monkeypatch.setattr(corridor_cohort_integration, "solve_corridor_cohorts", solve)
    monkeypatch.setattr(planning, "prepare_route_system_planning", prepare)
    monkeypatch.setattr(core, "prepare_route_system_planning", prepare)
    monkeypatch.setattr(svg, "_corridor_grant_moved_plan_ids", moved)
    yield trace


def _render(path: str) -> RoutePlan:
    """Render *path* and return the plan the render publishes."""
    source = ROOT / path
    graph = prepare_graph(source.read_text(), source_dir=str(source.parent))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        observed = svg.build_observed_render_plan(graph, resolve_theme(None, graph))
    return observed.route_plan


@pytest.mark.parametrize("path", GRANTED_FIXTURES)
def test_accepted_cohort_plan_is_persisted_once(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _traced(monkeypatch) as trace:
        published = _render(path)

    record = published.accepted_corridor_cohort_plan
    assert record is not None, "the published plan carries no accepted cohort plan"
    assert all(len(call.accepting) <= 1 for call in trace.calls)
    (accepting,) = trace.calls[-1].accepting
    assert accepting.accepted_corridor_cohort_plan == record
    trunk = corridor_cohort_integration.AcceptedCorridorOwnerKind.CONVERGENCE_TRUNK
    assert any(
        owner.kind is trunk and abs(owner.coordinate_delta) > 0
        for owner in record.owners
    )


def test_serialized_published_plan_carries_the_accepted_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _traced(monkeypatch):
        published = _render(MERGE_RIGHT_ENTRY)

    record = published.accepted_corridor_cohort_plan
    assert record is not None
    text = serialize_route_plan(published)
    stored = json.loads(text)["accepted_corridor_cohort_plan"]
    assert stored["plan_id"] == record.plan_id
    assert len(stored["owners"]) == len(record.owners)
    assert stored["coordinate_translations"] == []
    assert serialize_route_plan(copy.deepcopy(published)) == text
    assert serialize_route_plan(pickle.loads(pickle.dumps(published))) == text


def test_frozenset_serializes_as_a_list_sorted_by_json_form() -> None:
    value = frozenset({("b", 2), ("a", 10), ("a", 9)})
    assert route_plan_module._json_value(value) == [["a", 10], ["a", 9], ["b", 2]]
    assert route_plan_module._json_value(frozenset({"z", "m", "a"})) == [
        "a",
        "m",
        "z",
    ]


def test_serialized_plan_is_independent_of_the_hash_seed() -> None:
    script = (
        "import hashlib, warnings\n"
        "from pathlib import Path\n"
        "from nf_metro.api import prepare_graph, resolve_theme\n"
        "from nf_metro.layout.route_plan import serialize_route_plan\n"
        "from nf_metro.render import svg\n"
        f"path = Path({str(ROOT / MERGE_RIGHT_ENTRY)!r})\n"
        "graph = prepare_graph(path.read_text(), source_dir=str(path.parent))\n"
        "observed = svg.build_observed_render_plan(graph, resolve_theme(None, graph))\n"
        "text = serialize_route_plan(observed.route_plan)\n"
        "print(hashlib.sha256(text.encode()).hexdigest())\n"
    )
    digests = set()
    for seed in ("0", "1", "4242"):
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PYTHONHASHSEED": seed,
                "PYTHONPATH": str(ROOT / "src"),
            },
            cwd=ROOT,
        )
        assert completed.returncode == 0, completed.stderr[-400:]
        digests.add(completed.stdout.strip())
    assert len(digests) == 1


def test_moved_grant_selects_its_plan_without_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_moved = svg._corridor_grant_moved_plan_ids
    with _traced(monkeypatch) as trace:
        _render(MERGE_RIGHT_ENTRY)

    moved = [plan for plan in trace.compiling_plans if real_moved(plan)]
    assert moved, "no observation applied a convergence corridor grant"
    for plan in moved:
        record = plan.accepted_corridor_cohort_plan
        assert record is not None
        stripped = replace(plan, diagnostics=())
        assert real_moved(stripped) == {
            frame.owner_id
            for frame in record.trunk_grants
            if abs(frame.coordinate_delta) > COORD_TOLERANCE_FINE
        }
        assert real_moved(plan) == real_moved(stripped)


def _packed_cell_aperture_trace(monkeypatch: pytest.MonkeyPatch) -> _Trace:
    direct_assembled_past_reference(monkeypatch)
    with _traced(monkeypatch) as trace:
        _render(PACKED_CELL)
    return trace


@pytest.mark.parametrize(
    "path", [*GRANTED_FIXTURES, "aperture"], ids=lambda value: Path(value).stem
)
def test_cohorts_compile_at_most_twice_per_settle_call_and_never_after_intent(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lock: the compiler and solver run at one observation per settle call."""
    if path == "aperture":
        trace = _packed_cell_aperture_trace(monkeypatch)
    else:
        with _traced(monkeypatch) as trace:
            _render(path)

    compiled = [call for call in trace.calls if call.compile_observations]
    assert compiled
    for call in trace.calls:
        assert len(call.compile_observations) <= 2
        if not call.compile_observations:
            continue
        intent = call.compile_observations[0]
        assert all(rank <= intent for rank in call.compile_observations)
        assert all(rank <= intent for rank in call.solve_observations)


def _accepted_inputs(monkeypatch: pytest.MonkeyPatch, path: str = MERGE_RIGHT_ENTRY):
    with _traced(monkeypatch) as trace:
        published = _render(path)
    (execution,) = trace.calls[-1].accepting
    cohorts = execution.member_geometry.corridor_cohorts
    owned = planning._owned_scalar_grants(
        cohorts, planning._compatibility_scalar_ids(cohorts)
    )
    return published.accepted_corridor_cohort_plan, cohorts, owned


def test_accepted_plan_identity_survives_input_permutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock: the record's identity and order depend on content alone."""
    record, cohorts, owned = _accepted_inputs(
        monkeypatch, "examples/topologies/plan_owned_distinct_lane_separation.mmd"
    )
    permuted = replace(
        cohorts,
        allocations=cohorts.allocations[::-1],
        landings=cohorts.landings[::-1],
    )
    rebuilt = corridor_cohort_integration.accept_corridor_cohort_plan(
        permuted, owned[::-1], record.coordinate_translations
    )
    assert rebuilt == record
    assert [frame.identity for frame in rebuilt.owners] == [
        frame.identity for frame in record.owners
    ]
    assert len(record.owners) > 3


def test_accepted_plan_records_every_moved_coordinate_and_no_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock: one frame per allocation, landing and owned grant."""
    record, cohorts, owned = _accepted_inputs(monkeypatch)
    assert len(record.owners) == (
        len(cohorts.allocations) + len(cohorts.landings) + len(owned)
    )
    (trunk,) = record.trunk_grants
    assert trunk.recipe is not None
    assert trunk.recipe.controlled_points


def _rebuilt(record, owners):
    """A record over *owners* carrying a digest that matches them."""
    translations = record.coordinate_translations
    return corridor_cohort_integration.AcceptedCorridorCohortPlan(
        corridor_cohort_integration._accepted_plan_id(owners, translations),
        owners,
        translations,
    )


def _member_segment(record):
    kind = corridor_cohort_integration.AcceptedCorridorOwnerKind.MEMBER_SEGMENT
    return next(frame for frame in record.owners if frame.kind is kind)


def _drop_owner(record):
    return replace(record, owners=record.owners[:-1])


def _duplicate_owner(record):
    return _rebuilt(record, (*record.owners, record.owners[0]))


def _duplicate_segment(record):
    first = _member_segment(record)
    twin = replace(first, identity=first.identity + "|twin")
    return _rebuilt(record, (*record.owners, twin))


def _change_owner(record):
    first = record.owners[0]
    moved = replace(first, accepted_coordinate=first.accepted_coordinate + 1.0)
    return replace(record, owners=(moved, *record.owners[1:]))


def _reorder_owners(record):
    return replace(record, owners=record.owners[::-1])


def _recipe_of_another_owner(record):
    (trunk,) = record.trunk_grants
    return replace(trunk, owner_id=trunk.owner_id + "|other")


def _nan_coordinate(record):
    return replace(record.owners[0], accepted_coordinate=math.nan)


def _infinite_extent(record):
    return replace(_member_segment(record), longitudinal_extent=(0.0, math.inf))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        pytest.param(_drop_owner, "does not match its identity", id="dropped-owner"),
        pytest.param(_duplicate_owner, "names an owner twice", id="duplicate-owner"),
        pytest.param(
            _duplicate_segment, "claims a segment twice", id="duplicate-segment"
        ),
        pytest.param(_change_owner, "does not match its identity", id="changed-owner"),
        pytest.param(
            _reorder_owners, "does not match its identity", id="reordered-owners"
        ),
        pytest.param(
            _recipe_of_another_owner,
            "recipe belonging to another owner",
            id="recipe-of-another-owner",
        ),
        pytest.param(_nan_coordinate, "non-finite coordinate", id="nan-coordinate"),
        pytest.param(_infinite_extent, "non-finite coordinate", id="infinite-extent"),
    ],
)
def test_accepted_plan_constructor_fails_closed(
    mutate, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lock: a record that is not exactly one coherent decision cannot exist."""
    record, _cohorts, _owned = _accepted_inputs(monkeypatch)
    with pytest.raises(
        corridor_cohort_integration.CorridorCohortCompilationError, match=message
    ):
        mutate(record)


def test_an_observation_holding_an_accepted_plan_may_not_compile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock: re-observing against a record that may compile is refused."""
    outcomes: list[str] = []
    real_observe = svg.observe_route_edges_centred

    def observe(graph, **kwargs):
        observation = real_observe(graph, **kwargs)
        prior = kwargs.get("reservations")
        accepted = observation.plan.accepted_corridor_cohort_plan
        if accepted is None or prior is None:
            return observation
        holding = replace(prior, accepted_corridor_cohort_plan=accepted)
        with pytest.raises(corridor_cohort_integration.CorridorCohortCompilationError):
            real_observe(graph, **{**kwargs, "reservations": holding})
        reading = real_observe(
            graph,
            **{
                **kwargs,
                "reservations": holding,
                "allow_convergence_clearance_requirements": False,
            },
        )
        assert reading.plan.accepted_corridor_cohort_plan is None
        outcomes.append("refused")
        return observation

    monkeypatch.setattr(svg, "observe_route_edges_centred", observe)
    _render(MERGE_RIGHT_ENTRY)
    assert outcomes == ["refused"]
