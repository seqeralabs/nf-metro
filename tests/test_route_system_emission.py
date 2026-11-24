"""Canonical whole-system dispatch and final emission attribution."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

import nf_metro.layout.routing.planning as planning
import nf_metro.layout.routing.system_emission as system_emission
from nf_metro.api import prepare_graph
from nf_metro.layout.route_plan import (
    ROUTE_SYSTEM_COMPATIBILITY_REASONS,
    EmissionMemberId,
    RouteSystemDisposition,
    RouteSystemId,
    build_route_semantic_scaffold,
)
from nf_metro.layout.route_reservations import reservation_ids_by_claimant_member
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.core import observe_route_edges
from nf_metro.layout.routing.exit_turns import (
    PLANNED_EXIT_FAMILIES,
)
from nf_metro.layout.routing.inter_section_handlers import (
    CLASSIFIABLE_INTER_SECTION_FAMILIES,
)
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
        system_emission.RouteSystemGeometryOwner.MEMBER_GEOMETRY,
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
        system_emission.RouteSystemGeometryOwner.MEMBER_GEOMETRY,
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


def test_corpus_systems_have_one_planned_emission_path() -> None:
    observation = _observe(
        ROOT / "examples" / "topologies" / "aligner_row_pinned_continuation.mmd"
    )
    assert observation.plan.systems
    assert all(
        system.disposition is RouteSystemDisposition.PLANNED
        and not system.compatibility_reasons
        for system in observation.plan.systems
    )
    assert all(
        route.route_system_disposition == RouteSystemDisposition.PLANNED.value
        for route in observation.routes
        if route.route_system_id is not None
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
        (
            item.system_id,
            item.disposition,
            item.compatibility_reasons,
            item.superseded_verdicts,
        )
        for item in decisions
    ) == tuple(
        (
            item.system_id,
            item.disposition,
            item.compatibility_reasons,
            item.superseded_verdicts,
        )
        for item in execution.systems
    )


def test_a_planned_system_records_the_child_verdict_its_owner_supersedes() -> None:
    observation = _observe(
        ROOT / "examples" / "topologies" / "exit_run_three_drop_columns.mmd"
    )
    recorded = tuple(
        (system, verdict)
        for system in observation.plan.systems
        for verdict in system.superseded_verdicts
    )

    assert recorded
    for system, verdict in recorded:
        assert system.disposition is RouteSystemDisposition.PLANNED
        assert verdict.superseded_by in {
            "member-geometry-plan",
            "convergence-plan",
        }
        assert verdict.owner in {"exit-turn-plan", "fan-plan"}
        assert verdict.reason in ROUTE_SYSTEM_COMPATIBILITY_REASONS[verdict.owner]
    declining_owners = {verdict.owner for _system, verdict in recorded}
    assert declining_owners == {"exit-turn-plan", "fan-plan"}


def test_a_compatibility_reason_is_never_also_recorded_as_superseded() -> None:
    observation = _observe(
        ROOT / "examples" / "topologies" / "aligner_row_pinned_continuation.mmd"
    )

    for system in observation.plan.systems:
        deciding = {reason.owner for reason in system.compatibility_reasons}
        assert not deciding & {verdict.owner for verdict in system.superseded_verdicts}


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


def test_every_reason_a_planner_can_generate_is_registered() -> None:
    """A reason built from an enum value carries the family that owns it.

    The exit-turn planner names an unsupported family and an unsupported merge
    entry from the enum member it read, so the registry holds exactly what those
    two enums can produce. A member missing from it is a route that raises on an
    unattributable reason instead of falling back; a reason present without a
    member is a family carrying a justification nothing can reach.

    The families it can read are the ones classification hands it, which is the
    dispatch table plus the L-shape default -- not every family the enum names.
    """
    registered = set(ROUTE_SYSTEM_COMPATIBILITY_REASONS["exit-turn-plan"])
    generated = {
        f"unsupported-family:{family.value}"
        for family in CLASSIFIABLE_INTER_SECTION_FAMILIES - PLANNED_EXIT_FAMILIES
    } | {"unsupported-subshape:merge-entry-straight"}

    namespaced = {
        reason
        for reason in registered
        if reason.startswith(
            ("unsupported-family:", "unsupported-subshape:merge-entry-")
        )
    }

    assert namespaced == generated


def test_promoted_subcascade_reasons_are_retired() -> None:
    registered = ROUTE_SYSTEM_COMPATIBILITY_REASONS["exit-turn-plan"]
    retired = {
        "unsupported-subshape:bottom-exit-junction-right-landings",
        "unsupported-subshape:bottom-exit-junction-via-gap",
        "unsupported-subshape:left-entry-corridor",
        "unsupported-subshape:merge-entry-around_below",
        "unsupported-subshape:merge-entry-corridor",
        "unsupported-subshape:merge-entry-perpendicular_entry",
        "unsupported-subshape:merge-trunk-around-below",
    }

    assert not retired & registered.keys()


def test_gap_allocation_reasons_are_retired() -> None:
    registered = ROUTE_SYSTEM_COMPATIBILITY_REASONS["exit-turn-plan"]
    retired = {
        "fixed-axis-conflict",
        "seating-group-owns-the-descent-column",
        "planned-axis-overlaps-compatibility-channel",
        "trunk-band-owns-the-chained-same-line-trunk",
    }

    assert not retired & registered.keys()


def test_a_retained_family_names_its_follow_up_or_states_why_it_is_permanent() -> None:
    """Every reason resolves to one authored family, not a generated sentence."""
    for owner, reasons in ROUTE_SYSTEM_COMPATIBILITY_REASONS.items():
        for reason, family in reasons.items():
            assert family.justification, f"{owner}:{reason} states no justification"
            assert reason not in family.justification, (
                f"{owner}:{reason} restates its own reason instead of explaining it"
            )
            assert family.follow_up is None or family.follow_up.startswith(
                "https://github.com/seqeralabs/nf-metro/issues/"
            ), f"{owner}:{reason} names a follow-up that is not an issue"


def test_dispatcher_retirement_has_no_registered_follow_up() -> None:
    umbrella = "https://github.com/seqeralabs/nf-metro/issues/1441"
    tagged = {
        f"{owner}:{reason}"
        for owner, reasons in ROUTE_SYSTEM_COMPATIBILITY_REASONS.items()
        for reason, family in reasons.items()
        if family.follow_up == umbrella
    }

    assert not tagged, f"compatibility reasons still delegated to #1441: {tagged}"
