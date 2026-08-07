"""Canonical whole-system dispatch and final emission attribution."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

import nf_metro.layout.routing.normalize as normalize
import nf_metro.layout.routing.planning as planning
import nf_metro.layout.routing.system_emission as system_emission
from nf_metro.api import prepare_graph
from nf_metro.layout.route_plan import (
    EmissionMemberId,
    RouteSystemDisposition,
    RouteSystemId,
    build_route_semantic_scaffold,
)
from nf_metro.layout.route_reservations import reservation_ids_by_claimant_member
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.core import observe_route_edges
from nf_metro.layout.routing.offsets import compute_station_offsets
from nf_metro.layout.routing.system_emission import (
    build_route_system_emission_execution,
    classify_route_system_dispositions,
    validate_published_route_attribution,
    validate_route_system_emission,
)
from nf_metro.parser.model import Edge
from nf_metro.parser.route_topology import ResolvedEdge

ROOT = Path(__file__).parents[1]


def _atomic_execution():
    carrier_id = EmissionMemberId("carrier")
    covered_id = EmissionMemberId("covered")
    carrier_edge = ResolvedEdge("a", "b", "line")
    covered_edge = ResolvedEdge("b", "c", "line")
    carrier = system_emission.RouteSystemEmissionMember(
        carrier_id, carrier_edge, reservation_ids=("carrier-reservation",)
    )
    covered = system_emission.RouteSystemEmissionMember(
        covered_id, covered_edge, reservation_ids=("covered-reservation",)
    )
    system = system_emission.RouteSystemEmission(
        RouteSystemId("system"),
        ("connector",),
        (carrier, covered),
        RouteSystemDisposition.PLANNED,
        (),
        (),
        ("carrier-reservation", "covered-reservation"),
    )
    execution = system_emission.RouteSystemEmissionExecution(
        (system,),
        MappingProxyType({carrier_edge: system, covered_edge: system}),
        MappingProxyType({carrier_edge: carrier, covered_edge: covered}),
        MappingProxyType({covered_id: carrier_id}),
    )
    route = RoutedPath(Edge("a", "b", "line"), "line", [(0.0, 0.0), (1.0, 0.0)])
    execution.attribute_route(route)
    return execution, route


def _observe(path: Path):
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    return observe_route_edges(graph, station_offsets=compute_station_offsets(graph))


def test_atomic_emission_accepts_one_emitted_or_covered_binding() -> None:
    execution, route = _atomic_execution()

    validate_route_system_emission([route], execution)

    postpass_coverage = system_emission.RouteSystemEmissionExecution(
        execution.systems,
        execution._by_edge,
        execution._member_by_edge,
        MappingProxyType({}),
    )
    validate_route_system_emission(
        [route],
        postpass_coverage,
        covered_routes=((("b", "c", "line"), ("a", "b", "line")),),
    )
    assert route.route_reservation_ids == ("carrier-reservation",)


def test_atomic_emission_rejects_cross_system_coverage() -> None:
    execution, route = _atomic_execution()
    carrier_system = execution.system_for_edge(ResolvedEdge("a", "b", "line"))
    assert carrier_system is not None
    foreign_id = EmissionMemberId("foreign-covered")
    foreign_edge = ResolvedEdge("x", "y", "line")
    foreign_member = system_emission.RouteSystemEmissionMember(foreign_id, foreign_edge)
    foreign_system = system_emission.RouteSystemEmission(
        RouteSystemId("foreign-system"),
        ("foreign-connector",),
        (foreign_member,),
        RouteSystemDisposition.PLANNED,
        (),
        (),
    )
    cross_system_execution = system_emission.RouteSystemEmissionExecution(
        (carrier_system, foreign_system),
        MappingProxyType(
            {
                **execution._by_edge,
                foreign_edge: foreign_system,
            }
        ),
        MappingProxyType(
            {
                **execution._member_by_edge,
                foreign_edge: foreign_member,
            }
        ),
        MappingProxyType(
            {
                **execution._covered_by_member,
                foreign_id: EmissionMemberId("carrier"),
            }
        ),
    )

    with pytest.raises(RuntimeError, match="foreign-system.*from route system system"):
        validate_route_system_emission([route], cross_system_execution)


def test_atomic_emission_rejects_missing_duplicate_and_double_bound_members() -> None:
    execution, route = _atomic_execution()

    with pytest.raises(RuntimeError, match="carrier.*0 emitted routes"):
        validate_route_system_emission([], execution)
    with pytest.raises(RuntimeError, match="carrier.*2 emitted routes"):
        validate_route_system_emission([route, route], execution)

    covered_route = RoutedPath(Edge("b", "c", "line"), "line", [(1.0, 0.0), (2.0, 0.0)])
    execution.attribute_route(covered_route)
    with pytest.raises(RuntimeError, match="covered member covered also emitted"):
        validate_route_system_emission([route, covered_route], execution)


def test_atomic_emission_rejects_attributed_unaccounted_route() -> None:
    execution, route = _atomic_execution()
    unknown = RoutedPath(
        Edge("outside", "system", "line"),
        "line",
        [(0.0, 0.0), (1.0, 0.0)],
        route_system_id="system",
        emission_member_id="unknown",
    )

    with pytest.raises(RuntimeError, match="no canonical emission member"):
        validate_route_system_emission([route, unknown], execution)


@pytest.mark.parametrize(
    "path",
    (
        ROOT / "examples" / "topologies" / "exit_run_three_drop_columns.mmd",
        ROOT / "examples" / "topologies" / "funcprofiler_upstream.mmd",
        ROOT / "examples" / "topologies" / "merge_trunk_out_of_range_section.mmd",
        ROOT / "tests" / "fixtures" / "genomeassembly_organellar.mmd",
    ),
)
def test_migrated_convergence_systems_have_one_planned_emission(path: Path) -> None:
    observation = _observe(path)
    reservation_ids_by_member = reservation_ids_by_claimant_member(
        observation.plan.reservations
    )
    systems = {
        plan.system_id: next(
            system for system in observation.plan.systems if system.id == plan.system_id
        )
        for plan in observation.plan.convergence_plans
    }

    assert systems
    assert {system.disposition for system in systems.values()} == {
        RouteSystemDisposition.PLANNED
    }
    assert all(not system.compatibility_reasons for system in systems.values())
    emitted_order = tuple(
        dict.fromkeys(
            route.route_system_id
            for route in observation.routes
            if route.route_system_id is not None
        )
    )
    expected_order = tuple(
        str(system.id)
        for system in observation.plan.systems
        if str(system.id) in emitted_order
    )
    assert emitted_order == expected_order
    for route in observation.routes:
        if route.route_system_id not in {str(item) for item in systems}:
            continue
        assert route.route_system_disposition == RouteSystemDisposition.PLANNED.value
        assert route.emission_member_id is not None
        assert route.route_plan_ids
        assert route.route_reservation_ids == reservation_ids_by_member.get(
            EmissionMemberId(route.emission_member_id), ()
        )


def test_compatibility_emission_has_one_explicit_reason_and_no_plan_owner() -> None:
    observation = _observe(
        ROOT / "examples" / "topologies" / "aligner_row_pinned_continuation.mmd"
    )
    reservation_ids_by_member = reservation_ids_by_claimant_member(
        observation.plan.reservations
    )
    compatible = tuple(
        system
        for system in observation.plan.systems
        if system.disposition is RouteSystemDisposition.COMPATIBILITY
    )

    assert compatible
    assert all(system.compatibility_reasons for system in compatible)
    assert all(not system.member_geometry_plan_ids for system in compatible)
    compatibility_system_ids = {system.id for system in compatible}
    assert all(
        plan.system_id not in compatibility_system_ids
        for plan in observation.plan.member_geometry_plans
    )
    assert all(
        reason.justification and reason.follow_up
        for system in compatible
        for reason in system.compatibility_reasons
    )
    compatible_ids = {str(system.id) for system in compatible}
    routes = tuple(
        route for route in observation.routes if route.route_system_id in compatible_ids
    )
    assert routes
    assert all(route.route_system_disposition == "compatibility" for route in routes)
    assert all(not route.route_plan_ids for route in routes)
    assert all(
        route.route_reservation_ids
        == reservation_ids_by_member.get(
            EmissionMemberId(route.emission_member_id or ""), ()
        )
        for route in routes
    )


def test_attribution_failure_names_the_complete_ownership_chain() -> None:
    path = ROOT / "examples" / "topologies" / "exit_run_three_drop_columns.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    observation = observe_route_edges(
        graph, station_offsets=compute_station_offsets(graph)
    )
    scaffold = build_route_semantic_scaffold(graph)
    assert scaffold is not None
    execution = build_route_system_emission_execution(
        scaffold,
        exit_turn_plans=observation.plan.exit_turn_plans,
        fan_plans=observation.plan.fan_plans,
        convergence_plans=observation.plan.convergence_plans,
        reservation_ids_by_member=reservation_ids_by_claimant_member(
            observation.plan.reservations
        ),
    )
    for system in execution.systems:
        expected = tuple(
            dict.fromkeys(
                reservation_id
                for member in system.members
                for reservation_id in member.reservation_ids
            )
        )
        assert system.reservation_ids == expected
        if system.disposition is RouteSystemDisposition.COMPATIBILITY:
            assert not system.reservation_ids
            assert all(not member.reservation_ids for member in system.members)
    route = next(
        item for item in observation.routes if item.route_system_id is not None
    )
    route.route_plan_ids = ("corrupt-plan",)

    with pytest.raises(RuntimeError) as caught:
        validate_route_system_emission(observation.routes, execution)

    message = str(caught.value)
    assert "route system " in message
    assert " connectors " in message
    assert " member " in message
    assert " plans " in message
    assert " reservations " in message


@pytest.mark.parametrize("member_id", (None, "unknown-member"))
def test_published_attribution_rejects_unknown_member_without_reservations(
    member_id: str | None,
) -> None:
    observation = _observe(
        ROOT / "examples" / "topologies" / "aligner_row_pinned_continuation.mmd"
    )
    route = next(
        item
        for item in observation.routes
        if item.route_system_id is not None and not item.route_reservation_ids
    )
    route.emission_member_id = member_id

    with pytest.raises(RuntimeError, match="unknown emission member"):
        validate_published_route_attribution(observation.routes, observation.plan)


def test_published_attribution_rejects_unknown_system() -> None:
    observation = _observe(
        ROOT / "examples" / "topologies" / "aligner_row_pinned_continuation.mmd"
    )
    route = next(
        item for item in observation.routes if item.route_system_id is not None
    )
    route.route_system_id = "unknown-system"

    with pytest.raises(RuntimeError, match="unknown route system"):
        validate_published_route_attribution(observation.routes, observation.plan)


def test_compatibility_reason_registry_is_closed() -> None:
    with pytest.raises(ValueError, match="unregistered compatibility reason"):
        system_emission._compatibility_reason("exit-turn-plan", "new-fallback")


def test_lightweight_dispositions_match_final_execution_in_canonical_order() -> None:
    path = ROOT / "examples" / "topologies" / "aligner_row_pinned_continuation.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    observation = observe_route_edges(
        graph, station_offsets=compute_station_offsets(graph)
    )
    scaffold = build_route_semantic_scaffold(graph)
    assert scaffold is not None
    decisions = classify_route_system_dispositions(
        scaffold,
        exit_turn_plans=observation.plan.exit_turn_plans,
        fan_plans=observation.plan.fan_plans,
        convergence_plans=observation.plan.convergence_plans,
    )
    execution = build_route_system_emission_execution(
        scaffold,
        exit_turn_plans=observation.plan.exit_turn_plans,
        fan_plans=observation.plan.fan_plans,
        convergence_plans=observation.plan.convergence_plans,
    )

    assert tuple(item.system_id for item in decisions) == scaffold.ordered_system_ids
    assert tuple(
        (item.system_id, item.disposition, item.compatibility_reasons)
        for item in decisions
    ) == tuple(
        (item.system_id, item.disposition, item.compatibility_reasons)
        for item in execution.systems
    )


def test_routing_constructs_only_the_final_emission_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = ROOT / "examples" / "topologies" / "exit_run_three_drop_columns.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    real_build = planning.build_route_system_emission_execution
    calls = 0

    def record_build(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_build(*args, **kwargs)

    monkeypatch.setattr(planning, "build_route_system_emission_execution", record_build)
    observe_route_edges(graph, station_offsets=compute_station_offsets(graph))

    assert calls == 1


def test_planned_convergence_never_enters_compatibility_merge_snap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = normalize._snap_merge_feeder_group
    observed_dispositions: list[tuple[str | None, ...]] = []

    def record_snap(group, graph):
        dispositions = tuple(
            channel.route.route_system_disposition for channel in group.channels
        )
        observed_dispositions.append(dispositions)
        assert "planned" not in dispositions
        original(group, graph)

    monkeypatch.setattr(normalize, "_snap_merge_feeder_group", record_snap)

    _observe(ROOT / "examples" / "topologies" / "merge_feeder_shared_channel_gap.mmd")

    assert observed_dispositions
    assert any(None in dispositions for dispositions in observed_dispositions)
