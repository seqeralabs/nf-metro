"""Immutable semantic route plans observe production routing without owning it."""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import subprocess
import sys
import warnings
from collections import Counter
from enum import Enum
from pathlib import Path

import networkx as nx
import pytest
from layout_metrics import compute_metrics

import nf_metro.layout.routing.inter_section_handlers as inter_section_handlers
from nf_metro.api import apply_layout_overrides, prepare_graph, resolve_theme
from nf_metro.layout.route_plan import (
    BindingKind,
    ConvergenceDisposition,
    CoordinateRegime,
    CoverageReason,
    DemandAxis,
    DemandKind,
    EmissionBinding,
    EmissionRole,
    ExitTurnDisposition,
    RouteFamilyId,
    RouteMemberGeometryPlan,
    RouteMemberGeometryPlanId,
    RouteSystemCompatibilityReason,
    RouteSystemDisposition,
    SharedReferenceKind,
    build_route_plan_query,
    serialize_route_plan,
)
from nf_metro.layout.routing import (
    compute_station_offsets,
    observe_route_edges,
    route_edges,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge, MetroGraph, Port, Section, Station
from nf_metro.parser.provenance import (
    ConnectorEndpointRole,
    DecisionOrigin,
    DecisionReason,
    LineOrderSource,
)
from nf_metro.parser.route_topology import (
    ConnectorId,
    ResolvedEdge,
    RouteTopologyQuery,
    build_route_topology_query,
)
from nf_metro.render.manifest import read_manifest
from nf_metro.render.plan import freeze_render_value
from nf_metro.render.svg import build_render_plan, emit_render_plan

ROOT = Path(__file__).parents[1]
EXAMPLES = ROOT / "examples"
FROZEN = ROOT / "tests" / "fixtures" / "hash_seed_determinism"
ROUTABLE_CORPUS = tuple(
    path
    for base in (ROOT / "examples", ROOT / "tests" / "fixtures")
    for path in sorted(base.rglob("*.mmd"))
    if "nextflow" not in path.parts and "invalid" not in path.parts
)

_REORDERABLE = """\
%%metro line: top | Top | #d62728
%%metro line: mid | Mid | #2db572
%%metro line: bot | Bot | #f5c542
{directive}graph LR
    subgraph s [S]
        top_in[ ]
        mid_in[ ]
        bot_in[ ]
        hub[Hub]
        mid_step[Mid]
        top_out[ ]
        mid_out[ ]
        bot_out[ ]
        top_in -->|top| hub
        bot_in -->|bot| hub
        mid_in -->|mid| mid_step
        hub -->|top| top_out
        hub -->|bot| bot_out
        mid_step -->|mid| mid_out
    end
"""


def _observe(path: Path):
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    observation = observe_route_edges(graph, station_offsets=offsets)
    return graph, observation.routes, observation.plan


_ExpectedMember = tuple[
    ResolvedEdge,
    tuple[tuple[ConnectorId, int, int], ...],
]


def _expected_inter_section_members(
    graph: MetroGraph,
) -> tuple[RouteTopologyQuery, tuple[_ExpectedMember, ...]]:
    query = build_route_topology_query(graph)
    assert query is not None
    assert graph.route_topology is not None
    refs_by_edge: dict[ResolvedEdge, list[tuple[ConnectorId, int, int]]] = {}
    junction_ids = graph.junction_ids
    for connector in graph.route_topology.connectors:
        for path_rank, resolved_path in enumerate(query.resolved_paths(connector.id)):
            for leg_rank, edge in enumerate(resolved_path):
                source = graph.stations[edge.source]
                target = graph.stations[edge.target]
                is_inter_section = (source.is_port or edge.source in junction_ids) and (
                    target.is_port or edge.target in junction_ids
                )
                if is_inter_section:
                    refs_by_edge.setdefault(edge, []).append(
                        (connector.id, path_rank, leg_rank)
                    )
    return query, tuple((edge, tuple(refs)) for edge, refs in refs_by_edge.items())


def _assert_recursively_immutable(value: object) -> None:
    retained_graph_types = (MetroGraph, Station, Section, Port, Edge, nx.Graph)
    assert not isinstance(value, retained_graph_types)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        params = type(value).__dataclass_params__
        assert params.frozen
        assert "__slots__" in vars(type(value))
        for field in dataclasses.fields(value):
            _assert_recursively_immutable(getattr(value, field.name))
    elif isinstance(value, tuple):
        for item in value:
            _assert_recursively_immutable(item)
    elif not isinstance(value, (str, int, float, bool, Enum, type(None))):
        pytest.fail(f"retained unsupported {type(value).__name__}")


def test_route_plan_records_are_recursively_immutable_graph_free_values() -> None:
    graph, _routes, plan = _observe(EXAMPLES / "guide" / "03b_fan_in_merge.mmd")

    _assert_recursively_immutable(plan)
    encoded = serialize_route_plan(plan)
    graph.stations[next(iter(graph.stations))].x += 1000
    graph.lines.clear()
    assert serialize_route_plan(plan) == encoded
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.systems = ()  # type: ignore[misc]
    assert "route_plan" not in {field.name for field in dataclasses.fields(MetroGraph)}


def test_route_systems_are_a_maximal_exact_connector_partition() -> None:
    graph = prepare_graph(
        """\
%%metro line: red | Red | #f00
%%metro line: blue | Blue | #00f
%%metro line: green | Green | #0f0
graph LR
    subgraph source [Source]
        a[A]
    end
    subgraph first [First]
        b[B]
    end
    subgraph second [Second]
        c[C]
    end
    subgraph independent_source [Independent source]
        d[D]
    end
    subgraph independent_target [Independent target]
        e[E]
    end
    a -->|red,blue| b
    a -->|red| b
    a -->|red| c
    d -->|green| e
"""
    )
    observation = observe_route_edges(
        graph, station_offsets=compute_station_offsets(graph)
    )
    assert graph.route_topology is not None
    plan = observation.plan

    connector_ids = tuple(connector.id for connector in graph.route_topology.connectors)
    assert (
        tuple(
            connector_id
            for system in plan.systems
            for connector_id in system.connector_ids
        )
        == connector_ids
    )
    assert len(
        {
            connector_id
            for system in plan.systems
            for connector_id in system.connector_ids
        }
    ) == len(connector_ids)
    assert tuple(system.connector_ids for system in plan.systems) == (
        connector_ids[:4],
        connector_ids[4:],
    )

    duplicate_ids = connector_ids[:3:2]
    shared_members = [
        member
        for member in plan.members
        if all(connector_id in member.connector_ids for connector_id in duplicate_ids)
    ]
    assert shared_members
    assert any(len(member.connector_ids) > 1 for member in shared_members)
    assert any(
        len(member.leg_refs) > len(member.connector_ids) - 1
        for member in shared_members
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        "guide/03_fan_out.mmd",
        "guide/03b_fan_in_merge.mmd",
        "topologies/fan_in_merge.mmd",
        "topologies/lr_to_tb_top_two_lines.mmd",
        "topologies/packed_cell_cellmate_bypass.mmd",
        "topologies/u_turn_fold.mmd",
    ],
)
def test_every_member_has_one_emitted_or_explicit_covered_binding(
    relative_path: str,
) -> None:
    graph, routes, plan = _observe(EXAMPLES / relative_path)
    query = build_route_plan_query(plan)

    assert len(plan.bindings) == len(plan.members)
    assert {binding.member_id for binding in plan.bindings} == {
        member.id for member in plan.members
    }
    assert all(len(query.bindings_for(member.id)) == 1 for member in plan.members)
    assert all(
        binding.path_rank is not None
        and 0 <= binding.path_rank < len(routes)
        and binding.path_id is not None
        if binding.kind is BindingKind.EMITTED
        else binding.kind in {BindingKind.MERGE_SKIP, BindingKind.COVERED_MERGE_HOP}
        and binding.covering_member_id is not None
        and binding.coverage_reason is CoverageReason.MERGE_TRUNK_COVERS_ENTRY_HOP
        for binding in plan.bindings
    )
    assert not [item for item in plan.diagnostics if item.blocking]
    assert graph.route_topology is not None
    assert {connector.id for connector in graph.route_topology.connectors} == {
        connector_id for system in plan.systems for connector_id in system.connector_ids
    }


@pytest.mark.parametrize(
    "relative_path",
    (
        "topologies/merge_feeders_three_columns.mmd",
        "guide/03b_fan_in_merge.mmd",
    ),
)
def test_planned_merge_suppression_occurs_before_dispatch(
    relative_path: str,
) -> None:
    _graph, _routes, plan = _observe(EXAMPLES / relative_path)
    covered = [
        binding for binding in plan.bindings if binding.kind is BindingKind.MERGE_SKIP
    ]

    assert covered
    binding = covered[0]
    assert binding.covering_member_id is not None
    assert binding.coverage_reason is CoverageReason.MERGE_TRUNK_COVERS_ENTRY_HOP


def test_binding_records_reject_partial_emission_and_coverage_states() -> None:
    _graph, _routes, plan = _observe(EXAMPLES / "genomeassembly.mmd")
    member_id = plan.members[0].id

    with pytest.raises(ValueError, match="invalid emitted"):
        EmissionBinding(member_id, BindingKind.EMITTED)
    with pytest.raises(ValueError, match="invalid merge-skip"):
        EmissionBinding(member_id, BindingKind.MERGE_SKIP)


def test_whole_graph_rail_routes_bind_through_the_final_route_set() -> None:
    _graph, routes, plan = _observe(EXAMPLES / "topologies" / "rail_inter_section.mmd")
    rail_members = [
        member
        for member in plan.members
        if member.family_id is RouteFamilyId.RAIL_INTER_SECTION
    ]

    assert rail_members
    query = build_route_plan_query(plan)
    for member in rail_members:
        assert member.family_id is RouteFamilyId.RAIL_INTER_SECTION
        (binding,) = query.bindings_for(member.id)
        assert binding.kind is BindingKind.EMITTED
        assert binding.path_rank is not None
        emitted = routes[binding.path_rank]
        assert emitted.line_id == member.line_id
        assert emitted.route_system_id == str(member.system_id)
        assert emitted.emission_member_id == str(member.id)
        assert emitted.route_system_disposition == RouteSystemDisposition.PLANNED.value
        assert emitted.route_plan_ids == ()
        assert emitted.route_reservation_ids == tuple(
            str(reservation.id)
            for reservation in plan.reservations
            if member.id in reservation.claimant_member_ids
        )

    member = rail_members[0]
    (binding,) = query.bindings_for(member.id)
    assert binding.path_rank is not None
    route = routes[binding.path_rank]
    fake_plan = RouteMemberGeometryPlan(
        RouteMemberGeometryPlanId("fake-rail-member-plan"),
        member.system_id,
        member.id,
        member.edge,
        member.connector_ids,
        RouteFamilyId.RAIL_INTER_SECTION,
        tuple(route.points),
        None if route.curve_radii is None else tuple(route.curve_radii),
        route.offset_regime,
        route.normalize_exempt,
        tuple(route.gap_slots),
        route.trunk_slot,
        (),
    )
    systems = tuple(
        dataclasses.replace(
            system,
            member_geometry_plan_ids=(fake_plan.id,),
        )
        if system.id == member.system_id
        else system
        for system in plan.systems
    )
    malformed = dataclasses.replace(
        plan,
        systems=systems,
        member_geometry_plans=(fake_plan,),
    )
    with pytest.raises(ValueError, match="rail emitter cannot"):
        build_route_plan_query(malformed)


def test_route_system_records_require_exact_public_partitions() -> None:
    _graph, _routes, plan = _observe(EXAMPLES / "genomeassembly.mmd")

    with pytest.raises(ValueError, match="duplicate route-system"):
        build_route_plan_query(
            dataclasses.replace(
                plan,
                systems=(*plan.systems, plan.systems[0]),
            )
        )

    owning_system = next(system for system in plan.systems if system.member_ids)
    missing_member = dataclasses.replace(
        owning_system, member_ids=owning_system.member_ids[1:]
    )
    with pytest.raises(ValueError, match="emission-member partition"):
        build_route_plan_query(
            dataclasses.replace(
                plan,
                systems=tuple(
                    missing_member if system.id == owning_system.id else system
                    for system in plan.systems
                ),
            )
        )

    missing_connector = dataclasses.replace(
        owning_system, connector_ids=owning_system.connector_ids[1:]
    )
    with pytest.raises(ValueError, match="connector ownership"):
        build_route_plan_query(
            dataclasses.replace(
                plan,
                systems=tuple(
                    missing_connector if system.id == owning_system.id else system
                    for system in plan.systems
                ),
            )
        )

    reordered_connectors = dataclasses.replace(
        owning_system, connector_ids=tuple(reversed(owning_system.connector_ids))
    )
    with pytest.raises(ValueError, match="connector index is not canonical"):
        build_route_plan_query(
            dataclasses.replace(
                plan,
                systems=tuple(
                    reordered_connectors if system.id == owning_system.id else system
                    for system in plan.systems
                ),
            )
        )

    missing_exit_group = dataclasses.replace(
        owning_system, exit_group_ids=owning_system.exit_group_ids[1:]
    )
    with pytest.raises(ValueError, match="ownership indexes"):
        build_route_plan_query(
            dataclasses.replace(
                plan,
                systems=tuple(
                    missing_exit_group if system.id == owning_system.id else system
                    for system in plan.systems
                ),
            )
        )

    assert len(owning_system.bundle_ids) > 1
    reordered_bundles = dataclasses.replace(
        owning_system, bundle_ids=tuple(reversed(owning_system.bundle_ids))
    )
    with pytest.raises(ValueError, match="bundle index"):
        build_route_plan_query(
            dataclasses.replace(
                plan,
                systems=tuple(
                    reordered_bundles if system.id == owning_system.id else system
                    for system in plan.systems
                ),
            )
        )

    _fan_graph, _fan_routes, fan_plan = _observe(
        EXAMPLES / "topologies" / "fan_in_merge.mmd"
    )
    for label, records, index_name, collection_name in (
        ("branch", fan_plan.branches, "branch_ids", "branches"),
        ("feeder", fan_plan.feeders, "feeder_ids", "feeders"),
    ):
        assert records
        duplicate = records[0]
        owner = next(
            system for system in fan_plan.systems if system.id == duplicate.system_id
        )
        duplicated_owner = dataclasses.replace(
            owner,
            **{index_name: (*getattr(owner, index_name), duplicate.id)},
        )
        with pytest.raises(ValueError, match=f"duplicate route {label}"):
            build_route_plan_query(
                dataclasses.replace(
                    fan_plan,
                    systems=tuple(
                        duplicated_owner if system.id == owner.id else system
                        for system in fan_plan.systems
                    ),
                    **{collection_name: (*records, duplicate)},
                )
            )


def test_route_system_compatibility_reason_registry_is_public_schema() -> None:
    with pytest.raises(ValueError, match="unregistered compatibility reason"):
        RouteSystemCompatibilityReason(
            owner="member-geometry-plan",
            reason="arbitrary-private-fallback",
            justification="not registered",
            follow_up="not applicable",
        )
    with pytest.raises(ValueError, match="metadata is not canonical"):
        RouteSystemCompatibilityReason(
            owner="member-geometry-plan",
            reason="missing-emission-edge",
            justification="custom explanation",
            follow_up="custom follow-up",
        )


def test_route_families_and_roles_come_from_production_dispatch() -> None:
    _graph, _routes, plan = _observe(EXAMPLES / "topologies" / "fan_in_merge.mmd")

    by_family = {member.family_id: member for member in plan.members}
    assert RouteFamilyId.SAME_Y_STRAIGHT in by_family
    assert RouteFamilyId.MERGE_TRUNK in by_family
    bypass = by_family[RouteFamilyId.BYPASS_FAMILY]
    assert EmissionRole.BYPASS in bypass.roles
    assert EmissionRole.TERMINAL in bypass.roles
    skipped = [member for member in plan.members if member.family_id is None]
    assert skipped
    query = build_route_plan_query(plan)
    assert all(
        query.bindings_for(member.id)[0].kind is BindingKind.MERGE_SKIP
        for member in skipped
    )
    assert all(
        EmissionRole.CONTINUATION not in member.roles
        and EmissionRole.PEEL_OFF not in member.roles
        for member in plan.members
    )
    assert all(
        member.source.coordinate_regime is CoordinateRegime.SETTLED_GRID
        for member in plan.members
    )
    assert all(
        member.target.coordinate_regime is CoordinateRegime.SETTLED_GRID
        for member in plan.members
    )


def test_declined_migrated_dispatch_cannot_open_a_compatibility_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = EXAMPLES / "topologies" / "fan_in_merge.mmd"
    graph, _routes, baseline = _observe(path)
    declined = next(
        member.edge
        for member in baseline.members
        if member.family_id is RouteFamilyId.SAME_Y_STRAIGHT
    )
    rule = next(
        rule
        for rule in inter_section_handlers._INTER_SECTION_RULES
        if rule.family_id is RouteFamilyId.SAME_Y_STRAIGHT
    )

    def decline_one(facts):
        edge = facts.edge
        if (edge.source, edge.target, edge.line_id) == declined:
            return None
        return rule.route(facts)

    monkeypatch.setattr(
        inter_section_handlers,
        "_INTER_SECTION_RULES",
        [
            dataclasses.replace(candidate, route=decline_one)
            if candidate is rule
            else candidate
            for candidate in inter_section_handlers._INTER_SECTION_RULES
        ],
    )
    observation = observe_route_edges(
        graph, station_offsets=compute_station_offsets(graph)
    )
    (system,) = observation.plan.systems
    assert system.disposition is RouteSystemDisposition.COMPATIBILITY
    assert tuple(
        (reason.owner, reason.reason) for reason in system.compatibility_reasons
    ) == (("member-geometry-plan", "canonical-template-declined-member"),)
    assert not system.member_geometry_plan_ids
    assert not system.exit_turn_plan_ids
    assert not system.shared_reference_ids
    assert not system.demand_ids
    assert not system.reservation_ids
    assert not tuple(
        plan for plan in observation.plan.exit_turn_plans if plan.system_id == system.id
    )
    convergence_plans = tuple(
        plan
        for plan in observation.plan.convergence_plans
        if plan.system_id == system.id
    )
    assert convergence_plans
    assert system.convergence_plan_ids == tuple(plan.id for plan in convergence_plans)
    assert all(
        plan.disposition is ConvergenceDisposition.LEGACY
        and not plan.shared_reference_ids
        and not plan.demand_ids
        and not plan.endpoint_ownership
        for plan in convergence_plans
    )

    routes = tuple(
        route for route in observation.routes if route.route_system_id == str(system.id)
    )
    assert routes
    assert all(route.route_system_disposition == "compatibility" for route in routes)
    assert all(
        not route.route_plan_ids
        and route.exit_turn_plan_id is None
        and route.fan_plan_id is None
        and route.convergence_plan_id is None
        and not route.route_system_owned_segment_ranks
        for route in routes
    )

    query = build_route_plan_query(observation.plan)
    assert all(
        not query.fan_plans_for_member(member_id) for member_id in system.member_ids
    )
    assert all(
        not query.reservations_for_member(member_id) for member_id in system.member_ids
    )
    bindings = {
        member_id: query.bindings_for(member_id) for member_id in system.member_ids
    }
    assert all(len(items) == 1 for items in bindings.values())
    emitted_member_ids = {
        member_id
        for member_id, (binding,) in bindings.items()
        if binding.kind is BindingKind.EMITTED
    }
    covered = tuple(
        binding
        for (binding,) in bindings.values()
        if binding.kind is BindingKind.COVERED_MERGE_HOP
    )
    assert {
        route.emission_member_id for route in routes if route.emission_member_id
    } == {str(member_id) for member_id in emitted_member_ids}
    assert all(binding.covering_member_id in emitted_member_ids for binding in covered)


def test_schema_names_every_future_reference_and_demand_kind() -> None:
    assert set(SharedReferenceKind) == {
        SharedReferenceKind.CENTRELINE,
        SharedReferenceKind.TRUNK,
        SharedReferenceKind.BAND,
        SharedReferenceKind.RUNWAY,
        SharedReferenceKind.ORDERED_TURNS,
        SharedReferenceKind.LANDING_SEQUENCE,
    }
    assert set(DemandKind) == {
        DemandKind.SPAN,
        DemandKind.LANES,
        DemandKind.RUNWAY,
        DemandKind.ORDERED_TURNS,
        DemandKind.KEEP_OUT,
    }
    assert set(DemandAxis) == {DemandAxis.X, DemandAxis.Y, DemandAxis.BOTH}


def test_line_order_provenance_distinguishes_default_author_and_caller() -> None:
    base = """\
%%metro line: red | Red | #f00
graph LR
    subgraph source [Source]
        a[A]
    end
    subgraph target [Target]
        b[B]
    end
    a -->|red| b
"""
    default = parse_metro_mermaid(base)
    authored = parse_metro_mermaid("%%metro line_order: span\n" + base)
    caller = parse_metro_mermaid("%%metro line_order: span\n" + base)
    apply_layout_overrides(caller, {"line_order": "definition"})

    default_decision = default.layout_provenance.line_order_decision
    authored_decision = authored.layout_provenance.line_order_decision
    caller_decision = caller.layout_provenance.line_order_decision
    assert default_decision is not None
    assert default_decision.origin is DecisionOrigin.INFERRED
    assert default_decision.reason is DecisionReason.DEFAULT_LINE_ORDER
    assert not default_decision.locked
    assert authored_decision is not None
    assert authored_decision.origin is DecisionOrigin.AUTHORED
    assert authored_decision.reason is DecisionReason.AUTHOR_DIRECTIVE
    assert authored_decision.locked
    assert caller_decision is not None
    assert caller_decision.origin is DecisionOrigin.AUTHORED
    assert caller_decision.reason is DecisionReason.CALLER_LINE_ORDER
    assert caller_decision.value == "definition"
    assert caller.layout_provenance.authored is not None
    assert (
        caller.layout_provenance.authored.line_order.selected_source
        is LineOrderSource.CALLER
    )
    assert caller.layout_provenance.authored.line_order.directive_value == "span"

    cases = (
        (
            prepare_graph(base),
            "definition",
            DecisionOrigin.INFERRED,
            LineOrderSource.DEFAULT,
            False,
            DecisionReason.DEFAULT_LINE_ORDER,
            (),
        ),
        (
            prepare_graph("%%metro line_order: span\n" + base),
            "span",
            DecisionOrigin.AUTHORED,
            LineOrderSource.DIRECTIVE,
            True,
            DecisionReason.AUTHOR_DIRECTIVE,
            ("span",),
        ),
        (
            prepare_graph(
                "%%metro line_order: span\n" + base,
                layout_options={"line_order": "definition"},
            ),
            "definition",
            DecisionOrigin.AUTHORED,
            LineOrderSource.CALLER,
            True,
            DecisionReason.CALLER_LINE_ORDER,
            ("definition",),
        ),
    )
    for graph, value, origin, source, locked, reason, authored_values in cases:
        observation = observe_route_edges(
            graph, station_offsets=compute_station_offsets(graph)
        )
        fact = observation.plan.provenance.lane_order.policy
        assert fact.value == value
        assert fact.origin is origin
        assert observation.plan.provenance.lane_order.source is source
        assert fact.locked is locked
        assert fact.reason is reason
        assert fact.authored_values == authored_values
        assert observation.plan.provenance.lane_order.realised_line_ids == ("red",)


def test_caller_line_order_records_input_without_changing_application_timing() -> None:
    source = _REORDERABLE.format(directive="")
    default = prepare_graph(source)
    caller = prepare_graph(source, layout_options={"line_order": "span"})

    assert list(default.lines) == ["top", "bot", "mid"]
    assert list(caller.lines) == ["top", "bot", "mid"]
    assert "hub" in {item.node_id for item in caller.interchanges}
    provenance = caller.layout_provenance
    assert provenance.authored is not None
    intent = provenance.authored.line_order
    assert intent.directive_value is None
    assert intent.caller_value == "span"
    assert intent.selected_source is LineOrderSource.CALLER
    assert intent.authored_line_ids == ("top", "mid", "bot")
    decision = provenance.line_order_decision
    assert decision is not None
    assert decision.origin is DecisionOrigin.AUTHORED
    assert decision.locked
    assert decision.reason is DecisionReason.CALLER_LINE_ORDER

    observation = observe_route_edges(
        caller, station_offsets=compute_station_offsets(caller)
    )
    lane_order = observation.plan.provenance.lane_order
    assert lane_order.policy.value == "span"
    assert lane_order.policy.origin is DecisionOrigin.AUTHORED
    assert lane_order.source is LineOrderSource.CALLER
    assert lane_order.policy.locked
    assert lane_order.policy.reason is DecisionReason.CALLER_LINE_ORDER
    assert lane_order.policy.authored_values == ("span",)
    assert lane_order.realised_line_ids == ("top", "bot", "mid")
    with pytest.raises(dataclasses.FrozenInstanceError):
        intent.selected_value = "definition"  # type: ignore[misc]


def test_invalid_caller_line_order_is_not_recorded_as_typed_provenance() -> None:
    graph = parse_metro_mermaid(_REORDERABLE.format(directive=""))
    decision = graph.layout_provenance.line_order_decision
    apply_layout_overrides(graph, {"line_order": object()})

    assert graph.layout_provenance.line_order_decision is decision
    assert graph.layout_provenance.authored is not None
    assert graph.layout_provenance.authored.line_order.caller_value is None

    with pytest.raises(ValueError, match="unsupported caller line order"):
        parse_metro_mermaid(
            _REORDERABLE.format(directive=""),
            caller_line_order="garbage",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "name", ["seed_15.mmd", "seed_41.mmd", "seed_72.mmd", "seed_77.mmd"]
)
def test_frozen_sources_have_canonical_plan_serialisation(name: str) -> None:
    _graph, _routes, plan = _observe(FROZEN / name)

    encoded = serialize_route_plan(plan)
    assert encoded == serialize_route_plan(plan)
    assert json.loads(encoded)["systems"]


def test_frozen_source_plans_are_identical_across_supported_hash_seeds() -> None:
    paths = tuple(
        FROZEN / name
        for name in ("seed_15.mmd", "seed_41.mmd", "seed_72.mmd", "seed_77.mmd")
    )
    script = """
import json
import warnings
from pathlib import Path
from nf_metro.api import prepare_graph
from nf_metro.layout.route_plan import serialize_route_plan
from nf_metro.layout.routing import compute_station_offsets, observe_route_edges
result = {}
for raw in __import__('sys').argv[1:]:
    path = Path(raw)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        observed = observe_route_edges(
            graph, station_offsets=compute_station_offsets(graph)
        )
    result[path.name] = serialize_route_plan(observed.plan)
print(json.dumps(result, sort_keys=True, separators=(',', ':')))
"""
    outputs = []
    for seed in ("0", "1", "2", "5", "43", "random"):
        env = {
            **os.environ,
            "PYTHONHASHSEED": seed,
            "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT / "tests"))),
        }
        result = subprocess.run(
            [sys.executable, "-c", script, *(str(path) for path in paths)],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(result.stdout)
    assert len(set(outputs)) == 1


def test_every_routable_corpus_connector_and_leg_has_exact_plan_coverage() -> None:
    for path in ROUTABLE_CORPUS:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            graph, routes, plan = _observe(path)
        assert graph.route_topology is not None, path
        topology = graph.route_topology
        assert not [item for item in plan.diagnostics if item.blocking], path
        legacy_diagnostics = [
            item for item in plan.diagnostics if item.code == "exit-turn-legacy"
        ]
        assert len(legacy_diagnostics) == sum(
            item.disposition is ExitTurnDisposition.LEGACY
            for item in plan.exit_turn_plans
        ), path
        connector_ids = tuple(connector.id for connector in topology.connectors)
        observed_connectors = Counter(
            connector_id
            for system in plan.systems
            for connector_id in system.connector_ids
        )
        assert observed_connectors == Counter(connector_ids), path
        assert all(count == 1 for count in observed_connectors.values()), path
        assert len(set(connector_ids)) == len(connector_ids), path
        connector_rank = {
            connector_id: rank for rank, connector_id in enumerate(connector_ids)
        }
        assert tuple(
            connector_rank[system.connector_ids[0]] for system in plan.systems
        ) == tuple(
            sorted(connector_rank[system.connector_ids[0]] for system in plan.systems)
        ), path
        assert all(
            tuple(connector_rank[item] for item in system.connector_ids)
            == tuple(sorted(connector_rank[item] for item in system.connector_ids))
            for system in plan.systems
        ), path

        connector_system = {
            connector_id: system.id
            for system in plan.systems
            for connector_id in system.connector_ids
        }
        for records, field in (
            (topology.bundles, "bundle_ids"),
            (topology.exit_groups, "exit_group_ids"),
            (topology.entry_groups, "entry_group_ids"),
            (topology.divergences, "divergence_ids"),
            (topology.convergences, "convergence_ids"),
        ):
            expected_ids = tuple(record.id for record in records)
            observed = Counter(
                record_id
                for system in plan.systems
                for record_id in getattr(system, field)
            )
            assert observed == Counter(expected_ids), (path, field)
            assert all(count == 1 for count in observed.values()), (path, field)

        topology_query, expected_members = _expected_inter_section_members(graph)
        observed_members = tuple(
            (
                member.edge,
                tuple(
                    (ref.connector_id, ref.path_rank, ref.leg_rank)
                    for ref in member.leg_refs
                ),
            )
            for member in plan.members
        )
        assert observed_members == expected_members, path

        expected_endpoints = tuple(
            (
                group.id,
                role,
                group.section_id,
                group.side,
                (
                    topology_query.exit_port(group.id)
                    if role is ConnectorEndpointRole.EXIT
                    else topology_query.entry_port(group.id)
                ),
                group.connector_ids,
            )
            for role, groups in (
                (ConnectorEndpointRole.EXIT, topology.exit_groups),
                (ConnectorEndpointRole.ENTRY, topology.entry_groups),
            )
            for group in groups
        )
        assert (
            tuple(
                (
                    group.id,
                    group.role,
                    group.section_id,
                    group.side,
                    group.port_id,
                    group.connector_ids,
                )
                for group in plan.endpoint_groups
            )
            == expected_endpoints
        ), path

        assert tuple(
            (
                item.id,
                item.junction_id,
                item.exit_group_id,
                item.entry_group_ids,
                item.connector_ids,
            )
            for item in plan.divergences
        ) == tuple(
            (
                view.group.id,
                view.junction_id,
                view.group.exit_group_id,
                view.group.entry_group_ids,
                view.group.connector_ids,
            )
            for view in topology_query.divergences
        ), path
        assert tuple(
            (
                item.id,
                item.junction_id,
                item.entry_group_id,
                item.source_junction_ids,
                item.divergence_ids,
                item.connector_ids,
                item.line_id,
            )
            for item in plan.convergences
        ) == tuple(
            (
                view.group.id,
                view.junction_id,
                view.group.entry_group_id,
                view.source_junction_ids,
                view.group.divergence_ids,
                view.group.connector_ids,
                view.group.line_id,
            )
            for view in topology_query.convergences
        ), path

        for record in (
            *plan.endpoint_groups,
            *plan.divergences,
            *plan.convergences,
        ):
            expected_system = connector_system[record.connector_ids[0]]
            assert record.system_id == expected_system, path
            assert all(
                connector_system[connector_id] == expected_system
                for connector_id in record.connector_ids
            ), path

        expected_branches = tuple(
            (
                divergence.id,
                entry_group_id,
                tuple(
                    connector_id
                    for connector_id in divergence.connector_ids
                    if topology_query.connector(connector_id).entry_group_id
                    == entry_group_id
                ),
            )
            for divergence in topology.divergences
            for entry_group_id in divergence.entry_group_ids
        )
        assert (
            tuple(
                (branch.divergence_id, branch.entry_group_id, branch.connector_ids)
                for branch in plan.branches
            )
            == expected_branches
        ), path
        expected_feeders = tuple(
            (
                convergence.id,
                divergence_id,
                tuple(
                    connector_id
                    for connector_id in convergence.connector_ids
                    if connector_id
                    in topology_query.divergence_by_id(
                        divergence_id
                    ).group.connector_ids
                ),
            )
            for convergence in topology.convergences
            for divergence_id in convergence.divergence_ids
        )
        assert (
            tuple(
                (feeder.convergence_id, feeder.divergence_id, feeder.connector_ids)
                for feeder in plan.feeders
            )
            == expected_feeders
        ), path

        for records, field in (
            (plan.members, "member_ids"),
            (plan.branches, "branch_ids"),
            (plan.feeders, "feeder_ids"),
        ):
            observed = Counter(
                record_id
                for system in plan.systems
                for record_id in getattr(system, field)
            )
            assert observed == Counter(record.id for record in records), (path, field)
            assert all(count == 1 for count in observed.values()), (path, field)

        assert len(plan.bindings) == len(plan.members), path
        plan_query = build_route_plan_query(plan)
        member_ids = {member.id for member in plan.members}
        assert all(
            binding.kind is not BindingKind.UNROUTED for binding in plan.bindings
        ), path
        for member in plan.members:
            (binding,) = plan_query.bindings_for(member.id)
            if binding.kind is BindingKind.EMITTED:
                assert binding.path_rank is not None, path
                assert binding.path_rank < len(routes), path
                emitted = routes[binding.path_rank]
                assert (
                    emitted.edge.source,
                    emitted.edge.target,
                    emitted.line_id,
                ) == member.edge, path
                assert member.family_id is not None, path
            else:
                assert binding.covering_member_id is not None, path
                assert binding.covering_member_id in member_ids, path
                assert binding.covering_member_id != member.id, path
                carrier = plan_query.member(binding.covering_member_id)
                assert carrier.system_id == member.system_id, path
                (carrier_binding,) = plan_query.bindings_for(carrier.id)
                assert carrier_binding.kind is BindingKind.EMITTED, path


def test_observer_toggle_is_exactly_render_neutral() -> None:
    source = (EXAMPLES / "topologies" / "fan_in_merge.mmd").read_text()

    observed_graph = prepare_graph(source)
    graph_before = copy.deepcopy(observed_graph)
    offsets = compute_station_offsets(observed_graph)
    observation = observe_route_edges(observed_graph, station_offsets=offsets)
    assert observed_graph == graph_before

    plain_graph = prepare_graph(source)
    plain_routes = route_edges(
        plain_graph, station_offsets=compute_station_offsets(plain_graph)
    )
    assert freeze_render_value(observation.routes) == freeze_render_value(plain_routes)

    def render(graph):
        plan = build_render_plan(graph, resolve_theme(None, graph))
        svg = emit_render_plan(plan)
        return (
            freeze_render_value(plan),
            svg,
            read_manifest(svg),
            json.dumps(compute_metrics(graph, plan=plan), sort_keys=True),
        )

    assert render(observed_graph) == render(plain_graph)
