"""Immutable non-convergence member templates shared by planning and emission."""

from __future__ import annotations

import math
import operator
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, TypeVar

from nf_metro.layout.constants import (
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    CURVE_RADIUS,
    EDGE_TO_BUNDLE_CLEARANCE,
    MIN_CORRIDOR_Y_OVERLAP,
)
from nf_metro.layout.geometry import (
    cotravelling_lane_clearance,
    grid_spans_overlap,
    lane_delta,
    measured_distance,
    section_column_span,
    section_row_span,
    spans_share_corridor,
)
from nf_metro.layout.route_plan import (
    ConvergenceDisposition,
    ConvergencePlan,
    EmissionMemberId,
    EmissionRole,
    ExitTurnAssignment,
    ExitTurnAxisId,
    ExitTurnPlanId,
    FanPlanId,
    RouteMemberGapChannel,
    RouteMemberGeometryPlan,
    RouteMemberGeometryPlanId,
    RouteSemanticScaffold,
    RouteSystemId,
)
from nf_metro.layout.route_reservations import ColumnGapRegion, RowGapRegion
from nf_metro.layout.routing.common import (
    Direction,
    GapSlot,
    OffsetRegime,
    RoutedPath,
    SourceTurnout,
    _points_coincide,
    column_gap_edges,
    convergence_owns_segment_boundary,
    feasible_same_destination_approach_proposals,
    iter_plannable_short_same_destination_bundles,
    same_destination_approach_slots,
    segment_direction,
)
from nf_metro.layout.routing.context import (
    SettledExitTurn,
    _get_offset,
    _RoutingCtx,
    _section_lane_frame,
    port_lane_coord,
)
from nf_metro.layout.routing.corners import (
    concentric_corner_radius_at,
    resolve_curve_radius_at,
)
from nf_metro.layout.routing.corridor_cohort_integration import (
    CorridorCohortCompilationError,
    CorridorCohortFailure,
    CorridorCohortLedger,
    CorridorCohortPlan,
    CorridorCohortTarget,
    CorridorScalarRequest,
    _network_id,
    compile_corridor_cohort_plan,
)
from nf_metro.layout.routing.families import RouteFamilyId
from nf_metro.layout.routing.inter_section_handlers import (
    _build_inter_facts,
    _route_inter_section,
    packed_cell_handoff_carrier,
)
from nf_metro.layout.routing.normalize import (
    _bundle_divergent_distinct_traverses,
    _bundle_same_destination_tails,
    _coincide_fanout_opening_descents,
    _coincide_same_line_fanout_traverses,
    _coincide_same_line_tracks,
    _hold_runs_in_corridor_clearance,
    _locate_slot_channel_with_slot,
    _materialize_gap_slots,
    _materialize_trunk_slots,
    _reanchor_concentric_corner_fans,
    _reconcile_port_peeloff_risers,
    _rederive_semantic_end_corners,
    _reseat_concentric_flanking,
    _route_endpoint_section_ids,
    _segment_claim_band,
    _separate_fused_cotravelling_runs,
    _separate_opposing_inter_row_trunks,
    _set_vchannel_x,
    _settle_entry_wrap_leadouts,
    _settle_same_destination_approach_bundles,
    _stagger_convergent_distinct_lines,
    _unify_coincident_corner_radii,
    _VChannel,
)
from nf_metro.layout.routing.orientation import direction_axis, lateral_order_sign
from nf_metro.layout.routing.perp import _perp_entry_crossing_x
from nf_metro.layout.routing.reserved_bands import (
    ReservedBand,
    bundle_travel,
    corridor_clearance_band,
    resolved_band,
)
from nf_metro.layout.settlement_demand import (
    BoundaryClearanceRequirement,
    BoundaryClearanceRequirementKind,
    SettlementAxis,
)
from nf_metro.parser.model import Edge, MetroGraph, Port, PortSide, Section
from nf_metro.parser.route_topology import (
    ConnectorId,
    EndpointGroupId,
    ResolvedEdge,
    semantic_route_id,
)

if TYPE_CHECKING:
    from nf_metro.layout.route_reservations import GapCorridorBand


class MemberGeometryDeclinedError(RuntimeError):
    """A classified non-convergence member produced no canonical template."""


_IdT = TypeVar("_IdT")
_IndexedItem = TypeVar("_IndexedItem")
_SystemGapKey = tuple[RouteSystemId, tuple[int, int | None]]


@dataclass(frozen=True, slots=True)
class PreliminaryGapChannelClaim:
    """A convergence-owned leg that member allocation must account for."""

    system_id: RouteSystemId
    coordinate: float
    y_lo: float
    y_hi: float
    down: bool
    gap: tuple[int, int | None]
    line_ids: frozenset[str]
    source_junction_ids: frozenset[str] = frozenset()
    connector_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class _MemberCandidate:
    route: RoutedPath
    family_id: RouteFamilyId
    system_id: RouteSystemId
    carrier_id: str
    connector_ids: tuple[ConnectorId, ...]
    packed_cell_handoff: tuple[ResolvedEdge, float, bool] | None = None


@dataclass(frozen=True, slots=True)
class _MaterializedChannel:
    candidate: _MemberCandidate
    channel: _VChannel
    slot: GapSlot

    @property
    def gap(self) -> tuple[int, int | None]:
        return self.slot.gap_lo_col, self.slot.row

    @property
    def key(self) -> tuple[RouteSystemId, ResolvedEdge, int]:
        route = self.candidate.route
        return (
            self.candidate.system_id,
            ResolvedEdge(route.edge.source, route.edge.target, route.line_id),
            self.channel.idx,
        )


@dataclass(frozen=True, slots=True)
class _ChannelBounds:
    gap_lo: float
    gap_hi: float
    lo: float
    hi: float
    band: ReservedBand | None


@dataclass(frozen=True, slots=True)
class _SourceTurnoutCandidate:
    route: RoutedPath
    incoming: RoutedPath
    continuing: RoutedPath | None
    incoming_direction: Direction
    outgoing_direction: Direction


def _eligible_preliminary_gap_claims(
    claims: tuple[PreliminaryGapChannelClaim, ...],
    failure_reasons: Mapping[RouteSystemId, str],
) -> tuple[PreliminaryGapChannelClaim, ...]:
    """Drop convergence claims whose member system cannot own geometry."""
    return tuple(claim for claim in claims if claim.system_id not in failure_reasons)


@dataclass(frozen=True, slots=True)
class MemberGeometryExecution:
    plans: tuple[RouteMemberGeometryPlan, ...]
    failure_reasons: Mapping[RouteSystemId, str]
    _by_edge: Mapping[ResolvedEdge, RouteMemberGeometryPlan]
    settled_exit_turns: Mapping[tuple[str, str, str], SettledExitTurn] = field(
        default_factory=lambda: MappingProxyType({})
    )
    _semantic_corner_templates: Mapping[
        ResolvedEdge,
        tuple[
            tuple[float, ...] | None,
            tuple[tuple[int, tuple[float | None, float | None]], ...],
            tuple[tuple[int, tuple[float | None, float | None]], ...],
        ],
    ] = field(default_factory=lambda: MappingProxyType({}))
    clearance_requirements: tuple[BoundaryClearanceRequirement, ...] = ()
    corridor_cohorts: CorridorCohortPlan | None = None

    def plan_for_edge(
        self, edge: Edge | ResolvedEdge
    ) -> RouteMemberGeometryPlan | None:
        resolved = (
            edge
            if isinstance(edge, ResolvedEdge)
            else ResolvedEdge(edge.source, edge.target, edge.line_id)
        )
        return self._by_edge.get(resolved)

    def gap_channels(
        self, *, excluding: frozenset[ResolvedEdge] = frozenset()
    ) -> tuple[tuple[frozenset[str], RouteMemberGapChannel], ...]:
        """Every provisional member channel outside *excluding*."""
        return tuple(
            (frozenset((plan.edge.line_id,)), channel)
            for plan in self.plans
            if plan.edge not in excluding
            for channel in plan.gap_channels
        )

    def apply_semantic_corner_template(self, route: RoutedPath) -> None:
        """Apply the pre-publication corner cohort decision for *route*."""
        resolved = ResolvedEdge(route.edge.source, route.edge.target, route.line_id)
        template = self._semantic_corner_templates.get(resolved)
        if template is None:
            return
        radii, offsets, bases = template
        route.curve_radii = None if radii is None else list(radii)
        route.concentric_corner_offsets_by_segment = dict(offsets)
        route.concentric_corner_bases_by_segment = dict(bases)


def empty_member_geometry_execution() -> MemberGeometryExecution:
    return MemberGeometryExecution((), MappingProxyType({}), MappingProxyType({}))


def _member_geometry_plan_id(
    scaffold: RouteSemanticScaffold, candidate: _MemberCandidate
) -> RouteMemberGeometryPlanId:
    route = candidate.route
    resolved = ResolvedEdge(route.edge.source, route.edge.target, route.line_id)
    return RouteMemberGeometryPlanId(
        semantic_route_id(
            "route-member-geometry",
            candidate.system_id,
            scaffold.member_id_by_edge[resolved],
            candidate.family_id.value,
        )
    )


def _entry_port_crossing_coord(
    ctx: _RoutingCtx,
    port: Port,
    line_id: str,
) -> float:
    """Absolute coordinate where *line_id* crosses an entry-port boundary.

    LEFT/RIGHT ports expose their Y lane through the section lane frame.
    TOP/BOTTOM ports use the same perpendicular crossing calculation as the
    route builders, including approach fans and feeder bundle-index channels.
    """
    station = ctx.graph.stations[port.id]
    if port.side in (PortSide.LEFT, PortSide.RIGHT):
        return port_lane_coord(
            ctx.graph,
            station,
            line_id,
            ctx.station_offsets or {},
            ctx.positive_fan,
        )
    crossing = _perp_entry_crossing_x(ctx, port.id, line_id, station.x)
    if crossing is not None:
        return crossing
    offset = _get_offset(ctx, port.id, line_id)
    section = ctx.graph.sections.get(port.section_id) if port.section_id else None
    # Ride the section's lane frame directly rather than through the
    # direction-keyed _tb_x_offset helper, whose name the direction-predicate
    # ratchet caps at its existing call sites; a section-less port keeps its
    # unframed -offset lane.
    lane = (
        -offset
        if section is None
        else lane_delta(
            _section_lane_frame(ctx.graph, section, ctx.positive_fan), offset
        )
    )
    return station.x + lane


def _corridor_cohort_target(
    candidate: _MemberCandidate,
    scaffold: RouteSemanticScaffold,
    ctx: _RoutingCtx,
    *,
    mutable: bool,
) -> CorridorCohortTarget:
    route = candidate.route
    resolved = ResolvedEdge(route.edge.source, route.edge.target, route.line_id)
    port = ctx.graph.ports.get(resolved.target)
    station = ctx.graph.stations.get(resolved.target)
    endpoint_lane_axis: int | None = None
    endpoint_lane_coordinate: float | None = None
    if port is not None and port.is_entry and station is not None:
        endpoint_lane_axis = int(port.side in (PortSide.LEFT, PortSide.RIGHT))
        endpoint_lane_coordinate = _entry_port_crossing_coord(ctx, port, route.line_id)
        if route.offset_regime is OffsetRegime.DEFERRED and endpoint_lane_axis == 1:
            endpoint_lane_coordinate -= (ctx.station_offsets or {}).get(
                (resolved.target, route.line_id),
                0.0,
            )
    return CorridorCohortTarget(
        str(scaffold.member_id_by_edge[resolved]),
        str(_member_geometry_plan_id(scaffold, candidate)),
        (resolved.source, resolved.target, resolved.line_id),
        candidate.family_id,
        candidate.connector_ids,
        route,
        mutable,
        endpoint_lane_axis,
        endpoint_lane_coordinate,
        _network_id(candidate.connector_ids, scaffold),
        legal_crossing_segment_ranks=frozenset(route.convergence_owned_segment_ranks),
    )


def _corridor_cohort_targets(
    candidates: Sequence[_MemberCandidate],
    context_candidates: Sequence[_MemberCandidate],
    scaffold: RouteSemanticScaffold,
    ctx: _RoutingCtx,
) -> tuple[CorridorCohortTarget, ...]:
    selected: dict[tuple[str, tuple[str, str, str]], CorridorCohortTarget] = {}

    def add(population: Sequence[_MemberCandidate], *, mutable: bool) -> None:
        seen: dict[tuple[str, tuple[str, str, str]], CorridorCohortTarget] = {}
        for candidate in population:
            target = _corridor_cohort_target(
                candidate,
                scaffold,
                ctx,
                mutable=mutable,
            )
            key = target.member_id, target.edge_key
            existing = seen.get(key)
            if existing is not None:
                if existing.route is target.route:
                    continue
                raise RuntimeError(
                    "corridor cohort target identity is ambiguous in one population"
                )
            seen[key] = target
            if mutable or key not in selected:
                selected[key] = target

    add(candidates, mutable=True)
    add(context_candidates, mutable=False)
    return tuple(selected.values())


def _corridor_boundary_sections(
    graph: MetroGraph,
    region: ColumnGapRegion | RowGapRegion,
    claim_sections: tuple[Section, ...],
    blocker_sections: tuple[Section, ...],
) -> tuple[tuple[Section, ...], tuple[Section, ...]]:
    """Split every implicated section onto the region's own negative/positive side.

    Classifies each candidate section purely by its own grid position against
    the region's ``left_column``/``right_column`` (or row equivalent) --
    never by whether it reached this function through *claim_sections* or
    *blocker_sections* -- so a claim and the obstacle blocking it can anchor
    either side; nothing here reads which end of an authored connector is its
    source or its target. A side with no direct candidate is derived by
    searching every section at that side's grid index for one that shares the
    cross-axis span the known side already occupies.
    """
    if isinstance(region, ColumnGapRegion):
        start_attr = "grid_col"
        span_attr = "grid_col_span"
        cross_span_of = section_row_span
        bbox_start_attr = "bbox_x"
        bbox_size_attr = "bbox_w"
        negative_index = region.left_column
        positive_index = region.right_column
        axis_name = "column"
    else:
        start_attr = "grid_row"
        span_attr = "grid_row_span"
        cross_span_of = section_column_span
        bbox_start_attr = "bbox_y"
        bbox_size_attr = "bbox_h"
        negative_index = region.upper_row
        positive_index = region.lower_row
        axis_name = "row"

    def start(section: Section) -> int:
        return int(getattr(section, start_attr))

    def span(section: Section) -> int:
        return int(getattr(section, span_attr))

    def overlaps(left: Section, right: Section) -> bool:
        return grid_spans_overlap(cross_span_of(left), cross_span_of(right))

    def gap_facing(section: Section) -> float:
        return float(getattr(section, bbox_start_attr)) + float(
            getattr(section, bbox_size_attr)
        )

    def near_gap(section: Section) -> float:
        return float(getattr(section, bbox_start_attr))

    def cross_contains(outer: Section, inner: Section) -> bool:
        outer_span = cross_span_of(outer)
        inner_span = cross_span_of(inner)
        return outer_span[0] <= inner_span[0] and outer_span[1] >= inner_span[1]

    def only_bordering(
        sections: tuple[Section, ...],
        facing: Callable[[Section], float],
        nearer_than: Callable[[float, float], bool],
    ) -> tuple[Section, ...]:
        """Drop any section fully shadowed from the gap by a nearer same-side one.

        A section is dropped when another same-side candidate spans across it
        in the cross axis and sits strictly nearer the gap (*nearer_than*
        compares the two gap-facing coordinates): the shadowed section then
        lies wholly behind that neighbour and touches the boundary nowhere, so
        listing it is a false attribution. The nearest section is never
        shadowed, so a non-empty side stays non-empty.
        """

        def shadowed(section: Section) -> bool:
            return any(
                other is not section
                and cross_contains(other, section)
                and nearer_than(facing(other), facing(section))
                for other in sections
            )

        return tuple(section for section in sections if not shadowed(section))

    candidates = {
        section.id: section for section in (*claim_sections, *blocker_sections)
    }
    negative_sections = tuple(
        section
        for section in candidates.values()
        if start(section) + span(section) - 1 == negative_index
    )
    positive_sections = tuple(
        section for section in candidates.values() if start(section) == positive_index
    )
    if negative_sections and not positive_sections:
        positive_sections = tuple(
            section
            for section in graph.sections.values()
            if start(section) == positive_index
            and any(overlaps(section, negative) for negative in negative_sections)
        )
    elif positive_sections and not negative_sections:
        negative_sections = tuple(
            section
            for section in graph.sections.values()
            if start(section) + span(section) - 1 == negative_index
            and any(overlaps(section, positive) for positive in positive_sections)
        )
    if not negative_sections or not positive_sections:
        raise ValueError(
            f"clearance boundary has no facing section pair on the {axis_name} axis"
        )
    negative_sections = only_bordering(negative_sections, gap_facing, operator.gt)
    positive_sections = only_bordering(positive_sections, near_gap, operator.lt)
    return negative_sections, positive_sections


def _local_section_ids(
    graph: MetroGraph, edge_key: tuple[str, str, str] | None
) -> frozenset[str]:
    """Section ids the claim's own local hop touches, from live station identity.

    Reads each endpoint's already-resolved ``section_id`` rather than an
    authored connector's source or target section, which can name a station
    many hops upstream of the boundary this specific segment actually sits
    at (a junction station has no section and contributes nothing).
    """
    if edge_key is None:
        return frozenset()
    ids = set()
    for station_id in (edge_key[0], edge_key[1]):
        station = graph.stations.get(station_id)
        if station is not None and station.section_id is not None:
            ids.add(station.section_id)
    return frozenset(ids)


class _UnresolvedAperture(Exception):
    """One failure is not an aperture candidate and is skipped, not fatal.

    Raised for a consistent failure that simply has no aperture to request --
    no clearance shortfall at all, or a well-formed shortfall with no facing
    section pair to map to an adjacent grid boundary. These are collected and
    skipped per-failure so one sibling's silence never voids another's demand.
    """


class _ApertureIntegrityError(Exception):
    """One failure's shortfall is internally inconsistent; the batch fails closed.

    Raised for a shortfall whose own data contradicts itself. One case -- an
    undirected boundary side -- repeats an invariant the plan compile already
    enforces, so reaching it here means that earlier guard was bypassed. The
    other three are this producer's own: a shortfall naming an unknown claim,
    a solver axis that disagrees with the corridor boundary, and a blocker
    whose typed provenance does not match its live route target. Any of them
    must raise even when a sibling in the batch resolves; discarding it
    silently would hide a real defect.
    """


def _corridor_cohort_aperture_requirements(
    graph: MetroGraph,
    ledger: CorridorCohortLedger,
    targets: Sequence[CorridorCohortTarget],
    scalar_requests: Sequence[CorridorScalarRequest],
    error: CorridorCohortCompilationError,
) -> tuple[BoundaryClearanceRequirement, ...]:
    """Map every resolvable failure to its own typed requirement, independently.

    Each entry in ``error.failures`` is a separate corridor component that
    failed to allocate; one that is simply not an aperture candidate
    (``_UnresolvedAperture``) says nothing about whether its siblings are, so
    it is skipped rather than voiding every requirement the batch could
    otherwise produce, and a batch where nothing resolves at all raises.

    An internally inconsistent shortfall (``_ApertureIntegrityError``) is a
    different matter: it raises immediately, even when a sibling in the same
    batch resolves, so a corrupt failure is never masked by a healthy one.
    """
    claims_by_id = {claim.claim_id: claim for claim in ledger.claims}
    scalar_requests_by_id = {
        request.variable.variable_id: request for request in scalar_requests
    }
    targets_by_key = {(target.member_id, target.edge_key): target for target in targets}
    requirements: dict[
        tuple[SettlementAxis, int, tuple[str, ...], tuple[str, ...]],
        BoundaryClearanceRequirement,
    ] = {}
    skip_reasons: list[str] = []

    def resolve(failure: CorridorCohortFailure) -> BoundaryClearanceRequirement:
        shortfall = failure.clearance_shortfall
        if shortfall is None:
            raise _UnresolvedAperture(
                "allocation failure has no typed clearance shortfall"
            )
        if shortfall.required_shift_sign not in (-1, 1):
            raise _ApertureIntegrityError(
                "clearance shortfall has no directed boundary side"
            )
        claims = tuple(
            claims_by_id[claim_id]
            for claim_id in shortfall.claim_ids
            if claim_id in claims_by_id
        )
        scalar_claims = tuple(
            scalar_requests_by_id[claim_id]
            for claim_id in shortfall.claim_ids
            if claim_id in scalar_requests_by_id
        )
        if len(claims) + len(scalar_claims) != len(shortfall.claim_ids):
            raise _ApertureIntegrityError("clearance shortfall names an unknown claim")
        regions = {claim.region for claim in claims}
        for request in scalar_claims:
            request_region = request.region
            if request_region is None:
                raise _UnresolvedAperture(
                    "clearance claims do not name one corridor region"
                )
            regions.add(request_region)
        if not shortfall.claim_ids or len(regions) != 1:
            raise _UnresolvedAperture(
                "clearance claims do not name one corridor region"
            )
        region = next(iter(regions))
        if isinstance(region, ColumnGapRegion):
            axis = SettlementAxis.COLUMN
            boundary = region.right_column
            expected_axis = 0
        elif isinstance(region, RowGapRegion):
            axis = SettlementAxis.ROW
            boundary = region.lower_row
            expected_axis = 1
        else:
            raise _UnresolvedAperture(
                "clearance claim is not on an adjacent grid boundary"
            )
        if shortfall.axis != expected_axis:
            raise _ApertureIntegrityError("solver axis and corridor boundary disagree")

        claim_section_ids: set[str] = set()
        for claim in claims:
            claim_section_ids |= _local_section_ids(graph, claim.edge_key)
        for request in scalar_claims:
            claim_section_ids |= _local_section_ids(graph, request.variable.edge_key)
        claim_sections = tuple(
            graph.sections[section_id] for section_id in sorted(claim_section_ids)
        )
        typed_obstacles = {
            item.obstacle_id: item for item in failure.blocking_obstacles
        }
        if set(typed_obstacles) != set(shortfall.blocking_obstacle_ids):
            raise _ApertureIntegrityError("active blockers lack exact typed provenance")
        blocker_section_ids: set[str] = set()
        for obstacle in typed_obstacles.values():
            target = targets_by_key.get((obstacle.member_id, obstacle.edge_key))
            if target is None or target.connector_ids != obstacle.connector_ids:
                raise _ApertureIntegrityError(
                    "active blocker does not match its current route target"
                )
            blocker_section_ids |= _local_section_ids(graph, obstacle.edge_key)
        blocker_sections = tuple(
            graph.sections[section_id] for section_id in sorted(blocker_section_ids)
        )
        # Neither pool needs to be non-empty here: a claim or obstacle whose
        # local hop touches no section (a junction-to-junction convergence
        # trunk edge, say) is fine as long as the other pool anchors one side
        # for _corridor_boundary_sections to derive the rest from.
        try:
            negative_sections, positive_sections = _corridor_boundary_sections(
                graph, region, claim_sections, blocker_sections
            )
        except ValueError as mapping_error:
            raise _UnresolvedAperture(str(mapping_error)) from mapping_error
        if isinstance(region, ColumnGapRegion):
            negative_edge = max(
                section.bbox_x + section.bbox_w for section in negative_sections
            )
            positive_edge = min(section.bbox_x for section in positive_sections)
        else:
            negative_edge = max(
                section.bbox_y + section.bbox_h for section in negative_sections
            )
            positive_edge = min(section.bbox_y for section in positive_sections)
        negative_ids = tuple(sorted(section.id for section in negative_sections))
        positive_ids = tuple(sorted(section.id for section in positive_sections))
        required = max(0.0, positive_edge - negative_edge) + shortfall.deficit
        return BoundaryClearanceRequirement(
            axis,
            boundary,
            f"{failure.component_id}|result:{failure.result_rank}",
            required,
            negative_ids,
            positive_ids,
            f"corridor cohort aperture at {axis.value} boundary {boundary}",
            BoundaryClearanceRequirementKind.CORRIDOR_COHORT_APERTURE,
        )

    for failure in error.failures:
        try:
            requirement = resolve(failure)
        except _ApertureIntegrityError as integrity:
            raise CorridorCohortCompilationError(
                f"{error}; corridor aperture handoff hit an integrity violation on "
                f"{failure.component_id}/result:{failure.result_rank}: {integrity}",
                error.failures,
            ) from integrity
        except _UnresolvedAperture as unresolved:
            skip_reasons.append(
                f"{failure.component_id}/result:{failure.result_rank}: {unresolved}"
            )
            continue
        key = (
            requirement.axis,
            requirement.boundary,
            requirement.negative_section_ids,
            requirement.positive_section_ids,
        )
        held = requirements.get(key)
        if held is None or requirement.required > held.required:
            requirements[key] = requirement
    if not requirements:
        raise CorridorCohortCompilationError(
            f"{error}; corridor aperture handoff resolved no failure: "
            + "; ".join(skip_reasons),
            error.failures,
        ) from error
    return tuple(
        requirements[key]
        for key in sorted(
            requirements,
            key=lambda item: (item[0].value, item[1], item[2], item[3]),
        )
    )


def _compile_corridor_cohorts(
    ledger: CorridorCohortLedger | None,
    candidates: Sequence[_MemberCandidate],
    context_candidates: Sequence[_MemberCandidate],
    scaffold: RouteSemanticScaffold,
    ctx: _RoutingCtx,
    *,
    allow_clearance_requirements: bool,
    additional_targets: Sequence[CorridorCohortTarget] = (),
    scalar_requests: Sequence[CorridorScalarRequest] = (),
) -> tuple[
    CorridorCohortPlan | None,
    dict[ResolvedEdge, tuple[int, ...]],
    tuple[BoundaryClearanceRequirement, ...],
]:
    if ledger is None:
        return None, {}, ()
    member_targets = _corridor_cohort_targets(
        candidates,
        context_candidates,
        scaffold,
        ctx,
    )
    targets = (*member_targets, *additional_targets)
    try:
        cohort_plan = compile_corridor_cohort_plan(
            ledger,
            targets,
            scalar_requests=scalar_requests,
        )
    except CorridorCohortCompilationError as error:
        if not allow_clearance_requirements:
            raise
        return (
            None,
            {},
            _corridor_cohort_aperture_requirements(
                ctx.graph,
                ledger,
                targets,
                scalar_requests,
                error,
            ),
        )
    targets_by_key = {(target.member_id, target.edge_key): target for target in targets}
    ranks_by_edge: defaultdict[ResolvedEdge, list[int]] = defaultdict(list)
    for allocation in cohort_plan.allocations:
        target = targets_by_key.get((allocation.member_id, allocation.edge_key))
        if target is None or not target.mutable:
            raise RuntimeError(
                "corridor cohort allocation has no mutable current target"
            )
        edge = ResolvedEdge(*allocation.edge_key)
        ranks_by_edge[edge].append(allocation.segment_rank)
    for landing in cohort_plan.landings:
        target = targets_by_key.get((landing.member_id, landing.edge_key))
        if target is None or not target.mutable:
            raise RuntimeError("corridor cohort landing has no mutable current target")
        ranks_by_edge[ResolvedEdge(*landing.edge_key)].append(landing.segment_rank)
    return (
        cohort_plan,
        {edge: tuple(sorted(set(ranks))) for edge, ranks in ranks_by_edge.items()},
        (),
    )


def _corridor_cohort_context_candidates(
    context_routes: Sequence[RoutedPath],
    candidate_route_ids: frozenset[int],
    scaffold: RouteSemanticScaffold,
    family_by_edge: Mapping[ResolvedEdge, RouteFamilyId],
) -> tuple[_MemberCandidate, ...]:
    """Wrap each settled convergence-context route as an immutable cohort target."""
    context_candidates: list[_MemberCandidate] = []
    for route in context_routes:
        if id(route) in candidate_route_ids:
            continue
        resolved = ResolvedEdge(route.edge.source, route.edge.target, route.line_id)
        family = family_by_edge.get(resolved)
        if family is None or resolved not in scaffold.member_id_by_edge:
            continue
        context_candidates.append(
            _MemberCandidate(
                route,
                family,
                scaffold.system_for_edge(resolved),
                semantic_route_id(
                    "member-channel-carrier", resolved.source, resolved.target
                ),
                tuple(scaffold.connector_ids_for_edge(resolved)),
            )
        )
    return tuple(context_candidates)


def _validate_corridor_cohort_outcome(
    corridor_cohorts: CorridorCohortPlan | None,
    requirements: tuple[BoundaryClearanceRequirement, ...],
) -> None:
    """Fail loudly if the corridor-cohort compile output is self-inconsistent.

    A cohort compile either allocates every corridor (returning a plan and no
    requirement) or fails one and publishes its typed aperture requirement
    (returning no plan); it never does both, and every requirement it publishes
    is an aperture requirement naming two disjoint boundary sides.  These hold by
    construction today, so a violation is a wiring regression, not a layout
    condition a caller can recover from.
    """
    aperture = BoundaryClearanceRequirementKind.CORRIDOR_COHORT_APERTURE
    if corridor_cohorts is not None and requirements:
        raise RuntimeError(
            "corridor cohort compile published both an allocation plan and an "
            "aperture requirement"
        )
    for requirement in requirements:
        if requirement.kind is not aperture:
            raise RuntimeError(
                "corridor cohort compile published a non-aperture requirement: "
                f"{requirement.kind.value}"
            )
        both_sides = set(requirement.negative_section_ids) & set(
            requirement.positive_section_ids
        )
        if both_sides:
            raise RuntimeError(
                "corridor cohort aperture requirement names a section on both sides "
                f"of boundary {requirement.boundary}"
            )


def _allocated_turn(
    route: RoutedPath,
    run_direction: Direction | None,
    turn_direction: Direction | None,
) -> tuple[Direction, Direction, int] | None:
    if run_direction is None or turn_direction is None:
        if len(route.points) < 3:
            return None
        run_direction = segment_direction(route.points[0], route.points[1])
        turn_direction = segment_direction(route.points[1], route.points[2])
    if run_direction is None or turn_direction is None:
        return None
    if (run_direction in {Direction.R, Direction.L}) == (
        turn_direction in {Direction.R, Direction.L}
    ):
        return None
    candidate_ranks = tuple(
        index
        for index in range(1, len(route.points) - 1)
        if segment_direction(route.points[index - 1], route.points[index])
        is run_direction
        and segment_direction(route.points[index], route.points[index + 1])
        is turn_direction
    )
    if not candidate_ranks:
        return None
    preferred_rank = route.exit_turn_segment_rank
    rank = (
        candidate_ranks[0]
        if preferred_rank is None
        else min(candidate_ranks, key=lambda item: abs(item - preferred_rank))
    )
    return run_direction, turn_direction, rank


@dataclass(frozen=True, slots=True)
class _SettledTurnHeading:
    """The source bundle one deferred turn leaves on."""

    plan_id: ExitTurnPlanId
    source_id: str
    run_direction: Direction
    turn_direction: Direction
    pinning_group_id: EndpointGroupId | None


@dataclass(frozen=True, slots=True)
class _SettledTurnLadder:
    """One heading's arms that a single origin holds laterally apart."""

    heading: _SettledTurnHeading
    id: EndpointGroupId


@dataclass(frozen=True, slots=True)
class _SettledTurnCandidate:
    """One deferred source turn as the jointly seated population draws it."""

    route: RoutedPath
    edge_key: tuple[str, str, str]
    heading: _SettledTurnHeading
    entry_group_id: EndpointGroupId
    rank: int
    lane_rank: int
    launch_coordinate: float
    required_runway: float
    axis_coordinate: float
    corner_offsets: tuple[float | None, float | None]
    validate_corner_radii: bool

    @property
    def run_direction(self) -> Direction:
        return self.heading.run_direction

    @property
    def turn_direction(self) -> Direction:
        return self.heading.turn_direction


def _lane_connected_entry_groups(
    lanes_by_group: Mapping[EndpointGroupId, set[int]],
) -> Iterator[set[EndpointGroupId]]:
    """Yield the entry groups a lane joins, directly or transitively."""
    groups_by_lane: defaultdict[int, set[EndpointGroupId]] = defaultdict(set)
    for group_id, lanes in lanes_by_group.items():
        for lane in lanes:
            groups_by_lane[lane].add(group_id)
    unseated = set(lanes_by_group)
    while unseated:
        component: set[EndpointGroupId] = set()
        frontier = [min(unseated)]
        while frontier:
            group_id = frontier.pop()
            if group_id in component:
                continue
            component.add(group_id)
            frontier.extend(
                other
                for lane in lanes_by_group[group_id]
                for other in groups_by_lane[lane]
            )
        unseated -= component
        yield component


def _settled_turn_ladders(
    candidates: Iterable[_SettledTurnCandidate],
) -> Mapping[tuple[_SettledTurnHeading, EndpointGroupId], _SettledTurnLadder]:
    """Name the ladder each of a heading's entry groups is seated on.

    A ladder states one axis per lane, so entry groups that put members on a
    common lane are held to one origin.  Entry groups sharing no lane state
    nothing about each other's axes: they stand at their own destinations'
    depths, and holding them to one origin drags the nearer one out to the
    furthest one's lane.
    """
    lanes_by_group: defaultdict[
        _SettledTurnHeading, defaultdict[EndpointGroupId, set[int]]
    ] = defaultdict(lambda: defaultdict(set))
    for candidate in candidates:
        lanes_by_group[candidate.heading][candidate.entry_group_id].add(
            candidate.lane_rank
        )
    return {
        (heading, group_id): _SettledTurnLadder(heading, min(component))
        for heading, lanes_by_group_id in lanes_by_group.items()
        for component in _lane_connected_entry_groups(lanes_by_group_id)
        for group_id in sorted(component)
    }


def _turn_corridor_band(
    candidate: _SettledTurnCandidate, coordinate: float, ctx: _RoutingCtx
) -> ReservedBand | GapCorridorBand | None:
    """The clearance band the run leaving *candidate*'s turn owes its corridor.

    A claim over the leg is read ahead of live geometry, because settlement
    sized the boundary for that corridor and re-deriving from the drawn
    coordinate would spend the allocation instead of consuming it.

    The run leaving the turn travels along the turn direction, so the
    coordinate it holds is the one the *run into* the turn advances along --
    hence the band's axis is the run's, and its extent the perpendicular.
    """
    claimed = _segment_claim_band(ctx, candidate.route, candidate.rank)
    if claimed is not None:
        return claimed
    axis = direction_axis(candidate.run_direction).point_index
    section_ids = _route_endpoint_section_ids(ctx.graph, candidate.route)
    if not section_ids:
        return None
    span = (
        candidate.route.points[candidate.rank][1 - axis],
        candidate.route.points[candidate.rank + 1][1 - axis],
    )
    return corridor_clearance_band(
        ctx.graph,
        axis=axis,
        section_ids=section_ids,
        coordinate=coordinate,
        run_start=min(span),
        run_end=max(span),
    )


def _ladder_origin(
    ladder: _SettledTurnLadder,
    candidates: Sequence[_SettledTurnCandidate],
    lane_offsets: Mapping[tuple[str, str, str], float],
    ctx: _RoutingCtx,
) -> float:
    """The coordinate a ladder's lane offsets are measured out from.

    Members whose seated axes already read as one ladder state the origin
    between them, and it travels only far enough to give the deepest corner its
    radius -- or, on a lone member, the runway that member declares.  Members
    whose axes disagree describe no single ladder, so the origin is the furthest
    of what any of them holds or requires.
    """
    run_sign = ladder.heading.run_direction.sign
    actual_origins = [
        candidate.axis_coordinate - lane_offsets[candidate.edge_key]
        for candidate in candidates
    ]
    if any(
        abs(origin - actual_origins[0]) > COORD_TOLERANCE
        for origin in actual_origins[1:]
    ):
        required_origins = [
            candidate.launch_coordinate
            + run_sign * candidate.required_runway
            - lane_offsets[candidate.edge_key]
            for candidate in candidates
        ]
        origins = (*actual_origins, *required_origins)
        return max(origins) if run_sign > 0 else min(origins)
    reference_axis = min(
        (candidate.axis_coordinate for candidate in candidates),
        key=lambda coordinate: coordinate * run_sign,
    )
    radius_deficit = max(
        (
            concentric_corner_radius_at(
                candidate.route.points[candidate.rank - 1],
                candidate.route.points[candidate.rank],
                candidate.route.points[candidate.rank + 1],
                candidate.axis_coordinate - reference_axis,
                ctx.curve_radius,
            )
            - (candidate.axis_coordinate - candidate.launch_coordinate) * run_sign
            for candidate in candidates
        ),
        default=0.0,
    )
    # A member declares its own minimum runway, but one origin serves the whole
    # ladder: raising it to one member's minimum carries every mate past theirs.
    # The concentric radius their mutual displacement implies is the demand the
    # ladder states jointly, so a declared minimum joins it only on a lone
    # member, and only when short by a coordinate this frame can express.
    if len(candidates) == 1:
        (solo,) = candidates
        shortfall = (
            solo.required_runway
            - (solo.axis_coordinate - solo.launch_coordinate) * run_sign
        )
        if shortfall > COORD_TOLERANCE:
            radius_deficit = max(radius_deficit, shortfall)
    return actual_origins[0] + run_sign * max(0.0, radius_deficit)


def _corridor_hold_shift(
    candidates: Sequence[_SettledTurnCandidate],
    origin: float,
    lane_offsets: Mapping[tuple[str, str, str], float],
    ctx: _RoutingCtx,
) -> float:
    """How far a ladder must travel to stand inside its members' corridors.

    The general corridor hold runs over this population moments earlier and
    declines a planned turn (:func:`planner_owns_segment`), so the ladder's
    origin is the only point at which the clearance can be applied.

    The ladder travels rigidly, so one shift has to satisfy every member.
    Where none does, the corridors it crosses are sized for fewer lanes than
    they carry, and the origin stands as the ladder's own geometry states it.
    """
    lo_shift = -math.inf
    hi_shift = math.inf
    for candidate in candidates:
        seated = origin + lane_offsets[candidate.edge_key]
        band = _turn_corridor_band(candidate, seated, ctx)
        if band is None:
            continue
        lo_shift = max(lo_shift, band.lo - seated)
        hi_shift = min(hi_shift, band.hi - seated)
    allowed = resolved_band(lo_shift, hi_shift)
    return 0.0 if allowed is None else allowed.hold(0.0)


def _settled_exit_turns(
    routes: Iterable[RoutedPath],
    ctx: _RoutingCtx,
    pending_plan_ids: frozenset[ExitTurnPlanId],
) -> Mapping[tuple[str, str, str], SettledExitTurn]:
    """Read deferred source turns from the jointly seated route population."""
    if ctx.exit_turns is None or not pending_plan_ids:
        return MappingProxyType({})
    candidates: list[_SettledTurnCandidate] = []

    for route in routes:
        membership = ctx.exit_turns.membership_for_edge(route.edge)
        if membership is None or membership.plan.id not in pending_plan_ids:
            continue
        assignment = membership.assignment
        if assignment is None:
            continue
        allocated_turn = _allocated_turn(
            route, assignment.run_direction, assignment.turn_direction
        )
        if allocated_turn is None:
            continue
        run_direction, turn_direction, rank = allocated_turn
        axis = direction_axis(run_direction).point_index
        launch_coordinate = route.points[rank - 1][axis]
        axis_coordinate = route.points[rank][axis]
        runway = abs(axis_coordinate - launch_coordinate)
        candidates.append(
            _SettledTurnCandidate(
                route=route,
                edge_key=(route.edge.source, route.edge.target, route.line_id),
                heading=_SettledTurnHeading(
                    membership.plan.id,
                    route.edge.source,
                    run_direction,
                    turn_direction,
                    (
                        membership.axis.pinning_group_id
                        if membership.axis is not None
                        else None
                    ),
                ),
                entry_group_id=assignment.entry_group_id,
                rank=rank,
                lane_rank=assignment.source_lane_rank,
                launch_coordinate=launch_coordinate,
                required_runway=(
                    assignment.minimum_runway
                    if assignment.minimum_runway is not None
                    else runway
                ),
                axis_coordinate=axis_coordinate,
                corner_offsets=route.concentric_corner_offsets_by_segment.get(
                    rank, (None, None)
                ),
                validate_corner_radii=EmissionRole.TERMINAL in assignment.roles,
            )
        )
    ladders = _settled_turn_ladders(candidates)
    members: defaultdict[_SettledTurnLadder, list[_SettledTurnCandidate]] = defaultdict(
        list
    )
    for candidate in candidates:
        members[ladders[(candidate.heading, candidate.entry_group_id)]].append(
            candidate
        )
    lane_offsets: dict[tuple[str, str, str], float] = {}
    for cohort_candidates in members.values():
        lane_index = {
            lane_rank: index
            for index, lane_rank in enumerate(
                sorted({candidate.lane_rank for candidate in cohort_candidates})
            )
        }
        for candidate in cohort_candidates:
            lane_offsets[candidate.edge_key] = (
                lateral_order_sign(candidate.turn_direction)
                * lane_index[candidate.lane_rank]
                * ctx.offset_step
            )
    origin_by_ladder: dict[_SettledTurnLadder, float] = {}
    for ladder, cohort_candidates in members.items():
        origin = _ladder_origin(ladder, cohort_candidates, lane_offsets, ctx)
        origin_by_ladder[ladder] = origin + _corridor_hold_shift(
            cohort_candidates, origin, lane_offsets, ctx
        )
    translated_axes = {
        candidate.edge_key: (
            origin_by_ladder[ladders[(candidate.heading, candidate.entry_group_id)]]
            + lane_offsets[candidate.edge_key]
        )
        for candidate in candidates
    }
    reference_axes = {
        ladder: min(
            (translated_axes[candidate.edge_key] for candidate in cohort_candidates),
            key=lambda coordinate: coordinate * ladder.heading.run_direction.sign,
        )
        for ladder, cohort_candidates in members.items()
    }
    settled: dict[tuple[str, str, str], SettledExitTurn] = {}
    for candidate in candidates:
        route = candidate.route
        rank = candidate.rank
        translated_axis = translated_axes[candidate.edge_key]
        existing_bases = route.concentric_corner_bases_by_segment.get(
            rank, (None, None)
        )
        offset_out = (
            candidate.corner_offsets[1]
            if candidate.corner_offsets[1] is not None
            else 0.0
        )
        base_radius_out = (
            existing_bases[1]
            if existing_bases[1] is not None
            else (
                route.curve_radii[rank]
                if route.curve_radii is not None and rank < len(route.curve_radii)
                else ctx.curve_radius
            )
        )
        ladder = ladders[(candidate.heading, candidate.entry_group_id)]
        _reseat_concentric_flanking(
            route,
            rank,
            translated_axis,
            axis=direction_axis(candidate.run_direction).point_index,
            offset_in=translated_axis - reference_axes[ladder],
            offset_out=offset_out,
            base_radius=ctx.curve_radius,
            base_radius_out=base_radius_out,
        )
        settled[candidate.edge_key] = SettledExitTurn(
            candidate.run_direction,
            candidate.turn_direction,
            candidate.launch_coordinate,
            abs(translated_axis - candidate.launch_coordinate),
            translated_axis,
            route.concentric_corner_offsets_by_segment.get(
                rank, candidate.corner_offsets
            ),
            candidate.validate_corner_radii,
        )
    return MappingProxyType(settled)


def _adopt_allocated_pending_paths(
    candidates: Iterable[RoutedPath],
    population: Iterable[RoutedPath],
    ctx: _RoutingCtx,
    pending_plan_ids: frozenset[ExitTurnPlanId],
) -> None:
    """Copy the gap population's final representative onto each pending member."""
    assert ctx.exit_turns is not None
    allocated: dict[tuple[str, str, str], RoutedPath] = {}
    for route in population:
        membership = ctx.exit_turns.membership_for_edge(route.edge)
        if membership is None or membership.plan.id not in pending_plan_ids:
            continue
        assignment = membership.assignment
        if (
            assignment is None
            or _allocated_turn(
                route, assignment.run_direction, assignment.turn_direction
            )
            is None
        ):
            continue
        allocated[(route.edge.source, route.edge.target, route.line_id)] = route
    for route in candidates:
        source = allocated.get((route.edge.source, route.edge.target, route.line_id))
        if source is None or source is route:
            continue
        route.points[:] = source.points
        route.curve_radii = (
            None if source.curve_radii is None else list(source.curve_radii)
        )
        route.gap_slots[:] = source.gap_slots
        route.trunk_slot = source.trunk_slot
        route.concentric_corner_offsets_by_segment = dict(
            source.concentric_corner_offsets_by_segment
        )
        route.concentric_corner_bases_by_segment = dict(
            source.concentric_corner_bases_by_segment
        )


def _route_template(
    edge: Edge,
    family_id: RouteFamilyId,
    ctx: _RoutingCtx,
) -> RoutedPath:
    source, target = ctx.graph.edge_endpoints(edge)
    route = _route_inter_section(
        edge,
        source,
        target,
        ctx,
        planned_family_id=family_id,
    )
    if route is None:
        raise MemberGeometryDeclinedError("canonical family declined its member")
    return route


def _packed_cell_handoff_metadata(
    edge: Edge, family_id: RouteFamilyId, ctx: _RoutingCtx
) -> tuple[ResolvedEdge, float, bool] | None:
    """Resolve the carrier descent copied by a packed-cell handoff template."""
    if family_id is not RouteFamilyId.BYPASS_PACKED_CELL_SAME_ROW:
        return None
    source, target = ctx.graph.edge_endpoints(edge)
    handoff = packed_cell_handoff_carrier(_build_inter_facts(edge, source, target, ctx))
    if handoff is None:
        return None
    carrier, descent = handoff
    return (
        ResolvedEdge(carrier.source, carrier.target, carrier.line_id),
        descent.gap2_x,
        descent.gap2_vertical is Direction.D,
    )


def _convergence_context_route(
    ctx: _RoutingCtx,
    key: tuple[str, str, str],
    family_id: RouteFamilyId | None,
) -> RoutedPath | None:
    """One convergence-owned leg, emitted solely as gap-population context.

    A gap pass seats a channel on the slot its rank among every co-resident
    stroke earns, so a convergence leg has to reach those passes even though it
    contributes no candidate: a rank read from the candidates alone is a
    different rank, and freezing the channel makes it permanent.
    """
    edge = ctx.edge_by_key.get(key)
    if edge is None or family_id is None:
        return None
    return _route_template(edge, family_id, ctx)


def _typed_id(factory: Callable[[str], _IdT], value: str | None) -> _IdT | None:
    """*value* read into its own id space, leaving an absent id absent.

    A route carries its ids as bare strings while a plan record names the space
    each belongs to, so one conversion serves every crossing between the two.
    """
    return None if value is None else factory(value)


def _complete_concentric_corner_description(
    route: RoutedPath,
) -> tuple[
    dict[int, tuple[float | None, float | None]],
    dict[int, tuple[float | None, float | None]],
]:
    """Describe every frozen radius through the standard corner inputs."""
    offsets = dict(route.concentric_corner_offsets_by_segment)
    bases = dict(route.concentric_corner_bases_by_segment)
    for radius_index, radius in enumerate(route.curve_radii or ()):
        prev, corner, nxt = route.points[radius_index : radius_index + 3]
        primary_rank = radius_index + 1
        offset = offsets.get(primary_rank, (None, None))[0]
        base_radius = bases.get(primary_rank, (None, None))[0]
        if (
            offset is None
            or base_radius is None
            or abs(
                concentric_corner_radius_at(
                    prev,
                    corner,
                    nxt,
                    offset,
                    base_radius,
                )
                - radius
            )
            > COORD_TOLERANCE_FINE
        ):
            offset = 0.0
            base_radius = radius
        for segment_rank, tuple_index in (
            (radius_index, 1),
            (radius_index + 1, 0),
        ):
            if not 0 < segment_rank < len(route.points) - 1:
                continue
            pair = list(offsets.get(segment_rank, (None, None)))
            references = list(bases.get(segment_rank, (None, None)))
            pair[tuple_index] = offset
            references[tuple_index] = base_radius
            offsets[segment_rank] = (pair[0], pair[1])
            bases[segment_rank] = (references[0], references[1])
    return offsets, bases


def _semantic_corner_template(
    route: RoutedPath,
) -> tuple[
    tuple[float, ...] | None,
    tuple[tuple[int, tuple[float | None, float | None]], ...],
    tuple[tuple[int, tuple[float | None, float | None]], ...],
]:
    offsets, bases = _complete_concentric_corner_description(route)
    return (
        None if route.curve_radii is None else tuple(route.curve_radii),
        tuple(sorted(offsets.items())),
        tuple(sorted(bases.items())),
    )


def _freeze_plan(
    scaffold: RouteSemanticScaffold,
    candidate: _MemberCandidate,
    ctx: _RoutingCtx,
    reservation_ids_by_member: Mapping[EmissionMemberId, tuple[str, ...]],
    *,
    owns_complete_path: bool = False,
) -> RouteMemberGeometryPlan:
    route = candidate.route
    family_id = candidate.family_id
    system_id = candidate.system_id
    resolved = ResolvedEdge(route.edge.source, route.edge.target, route.line_id)
    member_id = scaffold.member_id_by_edge[resolved]
    channels: list[RouteMemberGapChannel] = []
    channel_claims: set[tuple[int, int, int | None, Direction]] = set()
    localized_slots: list[GapSlot] = []
    for slot in route.gap_slots:
        located = _locate_slot_channel_with_slot(
            route,
            slot,
            ctx.graph,
        )
        if located is None:
            localized_slots.append(slot)
            continue
        channel, localized_slot = located
        localized_slots.append(localized_slot)
        claim = (
            channel.idx,
            localized_slot.gap_lo_col,
            localized_slot.row,
            localized_slot.direction,
        )
        if claim in channel_claims:
            continue
        channel_claims.add(claim)
        channels.append(
            RouteMemberGapChannel(
                channel.idx,
                route.points[channel.idx],
                route.points[channel.idx + 1],
                localized_slot.gap_lo_col,
                localized_slot.row,
                localized_slot.direction,
            )
        )
    plan_id = _member_geometry_plan_id(scaffold, candidate)
    corner_offsets, corner_bases = _complete_concentric_corner_description(route)
    return RouteMemberGeometryPlan(
        plan_id,
        system_id,
        member_id,
        resolved,
        candidate.connector_ids,
        family_id,
        tuple(route.points),
        None if route.curve_radii is None else tuple(route.curve_radii),
        route.offset_regime,
        route.normalize_exempt,
        tuple(localized_slots),
        route.trunk_slot,
        tuple(channels),
        tuple(sorted(corner_offsets.items())),
        tuple(sorted(corner_bases.items())),
        exit_turn_plan_id=_typed_id(ExitTurnPlanId, route.exit_turn_plan_id),
        exit_turn_member_id=_typed_id(EmissionMemberId, route.exit_turn_member_id),
        exit_turn_family_id=route.exit_turn_family_id,
        exit_turn_axis_id=_typed_id(ExitTurnAxisId, route.exit_turn_axis_id),
        exit_turn_segment_rank=route.exit_turn_segment_rank,
        exit_lane_transition_plan_id=_typed_id(
            ExitTurnPlanId, route.exit_lane_transition_plan_id
        ),
        source_turnout=route.source_turnout,
        fan_plan_id=_typed_id(FanPlanId, route.fan_plan_id),
        fan_route_emitter=route.fan_route_emitter,
        consumed_reservation_ids=reservation_ids_by_member.get(member_id, ()),
        owns_complete_path=owns_complete_path,
    )


def _materialized_channels(
    candidates: Iterable[_MemberCandidate], ctx: _RoutingCtx
) -> tuple[_MaterializedChannel, ...]:
    """Every declared member channel, keyed to the row its geometry sits in.

    A slot's declared row is where the leg lands, which can be a row whose
    inter-column gap holds no section on the far side and so measures zero
    width.  Such a leg occupies the physical corridor and owes its neighbours
    their clearance, so its collapsed row is re-keyed to the row the routed
    geometry classifies into; dropping it would seat the corridor's other
    members without it.
    """
    return tuple(
        _MaterializedChannel(candidate, channel, localized_slot)
        for candidate in candidates
        for slot in candidate.route.gap_slots
        if (
            located := _locate_slot_channel_with_slot(
                candidate.route,
                slot,
                ctx.graph,
                reclassify_collapsed=True,
            )
        )
        is not None
        for channel, localized_slot in (located,)
    )


def _index_by_system_gap(
    items: Iterable[_IndexedItem],
    key: Callable[[_IndexedItem], _SystemGapKey],
) -> Mapping[_SystemGapKey, tuple[_IndexedItem, ...]]:
    indexed: defaultdict[_SystemGapKey, list[_IndexedItem]] = defaultdict(list)
    for item in items:
        indexed[key(item)].append(item)
    return MappingProxyType({key: tuple(value) for key, value in indexed.items()})


def _index_claims(
    claims: Iterable[PreliminaryGapChannelClaim],
) -> Mapping[_SystemGapKey, tuple[PreliminaryGapChannelClaim, ...]]:
    return _index_by_system_gap(claims, lambda claim: (claim.system_id, claim.gap))


def _index_materialized_channels(
    channels: Iterable[_MaterializedChannel],
) -> Mapping[_SystemGapKey, tuple[_MaterializedChannel, ...]]:
    return _index_by_system_gap(
        channels, lambda item: (item.candidate.system_id, item.gap)
    )


def _upstream_plan_fixes_channel(
    route: RoutedPath,
    segment_rank: int,
    movable_exit_plan_ids: frozenset[ExitTurnPlanId] = frozenset(),
) -> bool:
    """Whether a plan this allocator did not make already fixes the channel x.

    Deliberately not :func:`planner_owns_segment_or_boundary`.  Three arms
    differ, and each difference is a property of running before the member plan
    is stamped:

    * A fan or exit-lane transition membership resolved the whole corridor
      rather than one segment of it, so it fixes the channel at whichever rank
      the query names.
    * The segments either side of a planned exit turn are fixed too, because
      the turn's runway is measured along them -- the widening
      :func:`convergence_owns_segment_boundary` already applies to a
      convergence boundary.  A turn whose plan the caller listed as movable is
      excluded: re-seating it is the caller's own right.
    * ``route_system_owned_segment_ranks`` is this allocator's own output, and
      every route reaching it is a handler template rather than a stamped
      member route, so that field is empty here by construction.
    """
    if route.exit_lane_transition_plan_id is not None:
        return True
    if route.fan_plan_id is not None or route.fan_route_emitter is not None:
        return True
    if convergence_owns_segment_boundary(route, segment_rank):
        return True
    return (
        route.exit_turn_segment_rank is not None
        and route.exit_turn_plan_id not in movable_exit_plan_ids
        and abs(route.exit_turn_segment_rank - segment_rank) <= 1
    )


def _channel_bounds(item: _MaterializedChannel, ctx: _RoutingCtx) -> _ChannelBounds:
    route = item.candidate.route
    left, right = column_gap_edges(
        ctx.graph,
        item.slot.gap_lo_col,
        item.slot.gap_hi_col,
        row=item.slot.row,
    )
    lo = left + EDGE_TO_BUNDLE_CLEARANCE
    hi = right - EDGE_TO_BUNDLE_CLEARANCE
    band = ctx.reserved_bands.for_segment(
        route.edge.source, route.edge.target, route.line_id, item.channel.idx
    )
    if band is not None:
        lo = max(lo, band.lo)
        hi = min(hi, band.hi)
    return _ChannelBounds(left, right, lo, hi, band)


def _runway_candidates(
    item: _MaterializedChannel,
    bounds: _ChannelBounds,
    seeds: Iterable[float],
    ctx: _RoutingCtx,
) -> set[float]:
    route = item.candidate.route
    candidates = {*seeds, bounds.lo, bounds.hi}
    candidates.update(
        route.points[rank][0] + sign * ctx.curve_radius
        for rank in (item.channel.idx - 1, item.channel.idx + 2)
        if 0 <= rank < len(route.points)
        for sign in (-1.0, 1.0)
    )
    return candidates


def _candidate_clears_runway(
    item: _MaterializedChannel,
    bounds: _ChannelBounds,
    candidate: float,
    ctx: _RoutingCtx,
    *,
    shared_carrier: bool = False,
) -> bool:
    """Whether a candidate preserves its corridor and both corner runways.

    An exact same-line convergence claim already owns a physical carrier, so
    extending that stroke may use the whole gap.  It still cannot leave the
    gap, escape a reservation band, or starve either corner.
    """
    route = item.candidate.route
    if bounds.band is not None and not bounds.band.lo <= candidate <= bounds.band.hi:
        return False
    corridor_lo = bounds.gap_lo if shared_carrier else bounds.lo
    corridor_hi = bounds.gap_hi if shared_carrier else bounds.hi
    if (
        candidate < corridor_lo - COORD_TOLERANCE
        or candidate > corridor_hi + COORD_TOLERANCE
    ):
        return False
    return all(
        not (
            0 <= rank < len(route.points)
            and abs(route.points[rank][0] - candidate)
            < ctx.curve_radius - COORD_TOLERANCE
        )
        for rank in (item.channel.idx - 1, item.channel.idx + 2)
    )


def _seat_channel(channel: _VChannel, coordinate: float) -> None:
    """Move *channel* onto *coordinate*, recording it on the channel as well.

    A materialised channel outlives the move: the passes after it read the
    coordinate off the record rather than locating the leg again, so the record
    and the route it describes have to state one coordinate. Each flanking
    corner retains the standard concentric inputs that produced its radius.
    """
    route = channel.route
    offsets = route.concentric_corner_offsets_by_segment.get(channel.idx, (None, None))
    bases = route.concentric_corner_bases_by_segment.get(channel.idx, (None, None))
    radii = route.curve_radii or ()

    def corner_inputs(tuple_index: int, radius_index: int) -> tuple[float, float]:
        offset = offsets[tuple_index]
        base_radius = bases[tuple_index]
        if offset is not None and base_radius is not None:
            return offset, base_radius
        reference = (
            radii[radius_index] if 0 <= radius_index < len(radii) else CURVE_RADIUS
        )
        return 0.0, reference

    offset_in, base_radius = corner_inputs(0, channel.idx - 1)
    offset_out, base_radius_out = corner_inputs(1, channel.idx)
    _set_vchannel_x(
        channel,
        coordinate,
        offset_in,
        offset_out=offset_out,
        base_radius=base_radius,
        base_radius_out=base_radius_out,
    )
    channel.x = coordinate


def _align_packed_cell_handoffs(
    candidates: tuple[_MemberCandidate, ...],
    ctx: _RoutingCtx,
    movable_exit_plan_ids: frozenset[ExitTurnPlanId] = frozenset(),
) -> None:
    """Keep each packed-cell handoff on its canonical carrier descent."""
    by_edge = {
        ResolvedEdge(
            candidate.route.edge.source,
            candidate.route.edge.target,
            candidate.route.line_id,
        ): candidate
        for candidate in candidates
    }
    materialized = _materialized_channels(candidates, ctx)
    for candidate in candidates:
        if candidate.family_id is not RouteFamilyId.BYPASS_PACKED_CELL_SAME_ROW:
            continue
        route = candidate.route
        handoff = candidate.packed_cell_handoff
        if handoff is None:
            continue
        carrier_edge, descent_x, down = handoff
        carrier = by_edge.get(carrier_edge)
        assert carrier is not None, "packed-cell carrier has no member candidate"
        handoff_channels = tuple(
            item
            for item in materialized
            if item.candidate is candidate and item.channel.down is not down
        )
        assert handoff_channels, "packed-cell handoff has no vertical channel"
        handoff_channel = min(
            handoff_channels,
            key=lambda item: abs(item.channel.x - descent_x),
        )
        carrier_channels = tuple(
            item
            for item in materialized
            if item.candidate is carrier and item.channel.down is down
        )
        assert carrier_channels, "packed-cell carrier has no materialized descent"
        carrier_channel = min(
            carrier_channels,
            key=lambda item: abs(item.channel.x - descent_x),
        )
        assert not _upstream_plan_fixes_channel(
            route, handoff_channel.channel.idx, movable_exit_plan_ids
        ), "packed-cell handoff descent has a conflicting plan owner"
        bounds = _channel_bounds(handoff_channel, ctx)
        assert _candidate_clears_runway(
            handoff_channel, bounds, carrier_channel.channel.x, ctx
        ), "packed-cell handoff lies outside its carrier's feasible corridor"
        _seat_channel(handoff_channel.channel, carrier_channel.channel.x)


def _align_same_line_channels(
    materialized: tuple[_MaterializedChannel, ...],
    claims_by_system_gap: Mapping[
        tuple[RouteSystemId, tuple[int, int | None]],
        tuple[PreliminaryGapChannelClaim, ...],
    ],
    ctx: _RoutingCtx,
    movable_exit_plan_ids: frozenset[ExitTurnPlanId] = frozenset(),
) -> None:
    seated: set[tuple[RouteSystemId, ResolvedEdge, int]] = set()
    for item in materialized:
        route = item.candidate.route
        channel = item.channel
        if item.key in seated or _upstream_plan_fixes_channel(
            route, channel.idx, movable_exit_plan_ids
        ):
            continue
        matching = tuple(
            claim
            for claim in claims_by_system_gap.get(
                (item.candidate.system_id, item.gap), ()
            )
            if route.line_id in claim.line_ids
            and _claim_source_compatible(item, claim)
            and spans_share_corridor(channel.y_lo, channel.y_hi, claim.y_lo, claim.y_hi)
        )
        coordinates = {claim.coordinate for claim in matching}
        if len(coordinates) != 1:
            continue
        target = next(iter(coordinates))
        if abs(channel.x - target) <= COORD_TOLERANCE:
            continue
        bounds = _channel_bounds(item, ctx)
        candidates = _runway_candidates(item, bounds, (target,), ctx)
        coordinate = next(
            (
                candidate
                for candidate in sorted(
                    candidates,
                    key=lambda value: (
                        abs(value - target),
                        abs(value - channel.x),
                        value,
                    ),
                )
                if _candidate_clears_runway(
                    item,
                    bounds,
                    candidate,
                    ctx,
                    shared_carrier=abs(candidate - target) <= COORD_TOLERANCE,
                )
            ),
            None,
        )
        if coordinate is not None:
            _seat_channel(channel, coordinate)
            seated.add(item.key)


def _effective_claims(
    claims: tuple[PreliminaryGapChannelClaim, ...],
    channels_by_system_gap: Mapping[
        tuple[RouteSystemId, tuple[int, int | None]],
        tuple[_MaterializedChannel, ...],
    ],
) -> tuple[PreliminaryGapChannelClaim, ...]:
    effective: list[PreliminaryGapChannelClaim] = []
    for claim in claims:
        shared_coordinates = {
            item.channel.x
            for item in channels_by_system_gap.get((claim.system_id, claim.gap), ())
            if item.candidate.route.line_id in claim.line_ids
            and _claim_source_compatible(item, claim)
            and spans_share_corridor(
                item.channel.y_lo,
                item.channel.y_hi,
                claim.y_lo,
                claim.y_hi,
            )
        }
        effective.append(
            replace(claim, coordinate=next(iter(shared_coordinates)))
            if len(shared_coordinates) == 1
            else claim
        )
    return tuple(effective)


def _visible_claims_by_system_gap(
    claims: tuple[PreliminaryGapChannelClaim, ...],
    system_rank: Mapping[RouteSystemId, int],
    candidate_gaps: tuple[tuple[int, int | None], ...] = (),
) -> Mapping[
    tuple[RouteSystemId, tuple[int, int | None]],
    tuple[PreliminaryGapChannelClaim, ...],
]:
    gaps = tuple(dict.fromkeys((*candidate_gaps, *(claim.gap for claim in claims))))
    claims_by_lower_boundary: defaultdict[int, list[PreliminaryGapChannelClaim]] = (
        defaultdict(list)
    )
    for claim in claims:
        claims_by_lower_boundary[claim.gap[0]].append(claim)
    return MappingProxyType(
        {
            (system_id, gap): tuple(
                claim
                for claim in claims_by_lower_boundary.get(gap[0], ())
                if system_rank[claim.system_id] <= rank
            )
            for system_id, rank in system_rank.items()
            for gap in gaps
        }
    )


def _claim_clearance(
    item: _MaterializedChannel,
    claim: PreliminaryGapChannelClaim,
    ctx: _RoutingCtx,
) -> float:
    route = item.candidate.route
    channel = item.channel
    same_line = route.line_id in claim.line_ids
    if same_line and _claim_source_compatible(item, claim):
        return 0.0
    overlap = min(channel.y_hi, claim.y_hi) - max(channel.y_lo, claim.y_lo)
    if overlap <= MIN_CORRIDOR_Y_OVERLAP:
        return 0.0
    return cotravelling_lane_clearance(
        same_line=same_line,
        counter_running=channel.down is not claim.down,
        curve_radius=ctx.curve_radius,
    )


def _claim_source_compatible(
    item: _MaterializedChannel,
    claim: PreliminaryGapChannelClaim,
) -> bool:
    """Whether a same-line member extends the claim's physical carrier."""
    if claim.system_id != item.candidate.system_id:
        return False
    source_id = item.candidate.route.edge.source
    if source_id in claim.source_junction_ids:
        return True
    return not claim.connector_ids.isdisjoint(item.candidate.connector_ids)


def _allocate_bundle_around_claims(
    items: tuple[_MaterializedChannel, ...],
    obstacles: Sequence[PreliminaryGapChannelClaim],
    ctx: _RoutingCtx,
) -> None:
    """Hold one frozen member bundle clear of prior gap-channel claims.

    No per-edge handler can see a sibling member's frozen channel, so the
    clearance a bundle owes its neighbours is only knowable here: the whole
    bundle translates as one body, which keeps its members' lane order and
    keeps any carrier it already shares with a claim intact.
    """
    relevant_by_key = {
        item.key: tuple(
            claim
            for claim in obstacles
            if spans_share_corridor(
                item.channel.y_lo,
                item.channel.y_hi,
                claim.y_lo,
                claim.y_hi,
            )
        )
        for item in items
    }
    crowded = tuple(
        (item, claim, required)
        for item in items
        for claim in relevant_by_key[item.key]
        if (required := _claim_clearance(item, claim, ctx)) > 0.0
        and abs(item.channel.x - claim.coordinate) < required - COORD_TOLERANCE_FINE
    )
    if not crowded:
        return

    bounds_by_key = {item.key: _channel_bounds(item, ctx) for item in items}
    deltas = {0.0}
    deltas.update(
        claim.coordinate + sign * required - item.channel.x
        for item, claim, required in crowded
        for sign in (-1.0, 1.0)
    )
    for item in items:
        deltas.update(
            coordinate - item.channel.x
            for coordinate in _runway_candidates(
                item,
                bounds_by_key[item.key],
                (claim.coordinate for claim in relevant_by_key[item.key]),
                ctx,
            )
        )

    def feasible(delta: float) -> bool:
        for item in items:
            coordinate = item.channel.x + delta
            item_claims = relevant_by_key[item.key]
            shared_carrier = any(
                item.candidate.route.line_id in claim.line_ids
                and _claim_source_compatible(item, claim)
                and abs(coordinate - claim.coordinate) <= COORD_TOLERANCE
                for claim in item_claims
            )
            if not _candidate_clears_runway(
                item,
                bounds_by_key[item.key],
                coordinate,
                ctx,
                shared_carrier=shared_carrier,
            ):
                return False
            for claim in item_claims:
                if item.candidate.route.line_id in claim.line_ids and (
                    _claim_source_compatible(item, claim)
                ):
                    if abs(coordinate - claim.coordinate) > COORD_TOLERANCE:
                        return False
                    continue
                required = _claim_clearance(item, claim, ctx)
                if (
                    required > 0.0
                    and abs(coordinate - claim.coordinate)
                    < required - COORD_TOLERANCE_FINE
                ):
                    return False
        return True

    delta = next(
        (
            candidate
            for candidate in sorted(deltas, key=lambda value: (abs(value), value))
            if feasible(candidate)
        ),
        None,
    )
    if delta is None or abs(delta) <= COORD_TOLERANCE_FINE:
        return
    for item in items:
        _seat_channel(item.channel, item.channel.x + delta)


def _channel_bundles(
    materialized: tuple[_MaterializedChannel, ...],
    movable_exit_plan_ids: frozenset[ExitTurnPlanId] = frozenset(),
) -> tuple[tuple[_MaterializedChannel, ...], ...]:
    """Group mutable members that share one semantic carrier and corridor."""
    buckets: defaultdict[
        tuple[RouteSystemId, str, tuple[int, int | None], bool],
        list[_MaterializedChannel],
    ] = defaultdict(list)
    seen: set[tuple[RouteSystemId, ResolvedEdge, int]] = set()
    for item in materialized:
        if item.key in seen or _upstream_plan_fixes_channel(
            item.candidate.route, item.channel.idx, movable_exit_plan_ids
        ):
            continue
        seen.add(item.key)
        buckets[
            (
                item.candidate.system_id,
                item.candidate.carrier_id,
                item.gap,
                item.channel.down,
            )
        ].append(item)

    bundles: list[tuple[_MaterializedChannel, ...]] = []
    for items in buckets.values():
        corridors: list[list[_MaterializedChannel]] = []
        for item in sorted(
            items, key=lambda value: (value.channel.y_lo, value.channel.y_hi)
        ):
            corridor = next(
                (
                    group
                    for group in corridors
                    if any(
                        spans_share_corridor(
                            item.channel.y_lo,
                            item.channel.y_hi,
                            sibling.channel.y_lo,
                            sibling.channel.y_hi,
                        )
                        for sibling in group
                    )
                ),
                None,
            )
            if corridor is None:
                corridors.append([item])
            else:
                corridor.append(item)
        bundles.extend(tuple(group) for group in corridors)
    return tuple(bundles)


def _claim_for_materialized_channel(
    item: _MaterializedChannel,
) -> PreliminaryGapChannelClaim:
    """Freeze a member channel as an obstacle for later carrier allocation."""
    route = item.candidate.route
    start, end = route.points[item.channel.idx : item.channel.idx + 2]
    return PreliminaryGapChannelClaim(
        item.candidate.system_id,
        start[0],
        min(start[1], end[1]),
        max(start[1], end[1]),
        end[1] > start[1],
        item.gap,
        frozenset({route.line_id}),
        frozenset({route.edge.source}),
        frozenset(item.candidate.connector_ids),
    )


def _allocate_member_gap_channels(
    candidates: tuple[_MemberCandidate, ...],
    preliminary_claims: tuple[PreliminaryGapChannelClaim, ...],
    ctx: _RoutingCtx,
    movable_exit_plan_ids: frozenset[ExitTurnPlanId] = frozenset(),
) -> None:
    """Seat distinct semantic carriers jointly before their plans freeze."""
    materialized = _materialized_channels(candidates, ctx)
    effective_claims = _effective_claims(
        preliminary_claims, _index_materialized_channels(materialized)
    )
    obstacles = [
        *effective_claims,
        *(
            _claim_for_materialized_channel(item)
            for item in materialized
            if _upstream_plan_fixes_channel(
                item.candidate.route, item.channel.idx, movable_exit_plan_ids
            )
        ),
    ]
    for channel_bundle in _channel_bundles(materialized, movable_exit_plan_ids):
        _allocate_bundle_around_claims(tuple(channel_bundle), obstacles, ctx)
        obstacles.extend(
            _claim_for_materialized_channel(item) for item in channel_bundle
        )


def _carry_seated_run(
    route: RoutedPath, rank: int, axis: int, coordinate: float
) -> None:
    """Carry one run of a jointly-seated bundle onto *coordinate*.

    A bundle travels as one, so no member's displacement from the reference line
    its bundle nests around changes: each of the two corners the run turns on
    keeps the radius that displacement already gave it, and re-deriving against
    the base radius instead would seat every member on the single-line radius
    and draw the fan flat.  Each corner's own radius is therefore the reference
    the re-derivation anchors on, with no further displacement to apply.
    """
    radii = route.curve_radii
    reference_in = reference_out = CURVE_RADIUS
    if radii is not None:
        if 0 <= rank - 1 < len(radii):
            reference_in = radii[rank - 1]
        if rank < len(radii):
            reference_out = radii[rank]
    _reseat_concentric_flanking(
        route,
        rank,
        coordinate,
        axis=axis,
        base_radius=reference_in,
        base_radius_out=reference_out,
    )


def _seat_claimed_segments_before_freeze(
    candidates: tuple[_MemberCandidate, ...], ctx: _RoutingCtx
) -> None:
    """Seat reservation-owned interior runs before their member plan freezes.

    A member whose exit-turn plan pins its turn column has that column checked
    against the plan by the closing validator; the clearance band a shared gap
    corridor realises can be tighter than the nested ladder the plan seated
    outside it, so seating such a member to the band would slide it off its
    planned axis (and, where the corridor is shared with a bundle bound
    elsewhere, onto that bundle's lane).  A pinned member is left where its plan
    placed it.  The pin is read from the segment rank -- as
    :func:`_upstream_plan_fixes_channel` reads it -- not the plan id: the settled
    two-pass path clears a member's segment rank (restoring it after this pass)
    to hand it to seating, and reads as unpinned here.
    """
    grouped: defaultdict[
        tuple[RouteSystemId, str, int, int],
        list[tuple[RoutedPath, int, float, ReservedBand]],
    ] = defaultdict(list)
    for candidate in candidates:
        route = candidate.route
        if route.exit_turn_segment_rank is not None:
            continue
        for rank, (start, end) in enumerate(zip(route.points, route.points[1:])):
            if not 1 <= rank <= len(route.points) - 3:
                continue
            band = _segment_claim_band(ctx, route, rank)
            if band is None:
                continue
            if abs(start[1] - end[1]) <= COORD_TOLERANCE:
                grouped[(candidate.system_id, candidate.carrier_id, rank, 1)].append(
                    (route, rank, start[1], band)
                )
            elif abs(start[0] - end[0]) <= COORD_TOLERANCE:
                grouped[(candidate.system_id, candidate.carrier_id, rank, 0)].append(
                    (route, rank, start[0], band)
                )

    for (*_identity, axis), items in grouped.items():
        travel = bundle_travel(
            [(band, coordinate) for _route, _rank, coordinate, band in items]
        )
        if abs(travel) <= COORD_TOLERANCE_FINE:
            continue
        for route, rank, coordinate, _band in items:
            _carry_seated_run(route, rank, axis, coordinate + travel)


def _allocate_preliminary_gap_claims(
    candidates: tuple[_MemberCandidate, ...],
    claims: tuple[PreliminaryGapChannelClaim, ...],
    ctx: _RoutingCtx,
    system_rank: Mapping[RouteSystemId, int],
    movable_exit_plan_ids: frozenset[ExitTurnPlanId] = frozenset(),
) -> None:
    """Seat mutable member channels around preliminary convergence claims."""
    if not claims:
        return
    materialized = _materialized_channels(candidates, ctx)
    _align_same_line_channels(
        materialized, _index_claims(claims), ctx, movable_exit_plan_ids
    )
    effective_claims = _effective_claims(
        claims, _index_materialized_channels(materialized)
    )
    visible = _visible_claims_by_system_gap(
        effective_claims,
        system_rank,
        tuple(item.gap for item in materialized),
    )
    for bundle in _channel_bundles(materialized, movable_exit_plan_ids):
        item = bundle[0]
        obstacles = tuple(
            claim
            for claim in visible.get((item.candidate.system_id, item.gap), ())
            if any(
                spans_share_corridor(
                    member.channel.y_lo,
                    member.channel.y_hi,
                    claim.y_lo,
                    claim.y_hi,
                )
                for member in bundle
            )
        )
        if obstacles:
            ordered = sorted(bundle, key=lambda member: member.channel.x)
            movable_run: list[_MaterializedChannel] = []
            for member in ordered:
                anchored = any(
                    member.candidate.route.line_id in claim.line_ids
                    and _claim_source_compatible(member, claim)
                    and spans_share_corridor(
                        member.channel.y_lo,
                        member.channel.y_hi,
                        claim.y_lo,
                        claim.y_hi,
                    )
                    for claim in obstacles
                )
                if anchored:
                    if movable_run:
                        _allocate_bundle_around_claims(
                            tuple(movable_run), obstacles, ctx
                        )
                        movable_run = []
                    continue
                movable_run.append(member)
            if movable_run:
                _allocate_bundle_around_claims(tuple(movable_run), obstacles, ctx)


def _short_destination_clearance_requirement(
    graph: MetroGraph,
    port_id: str,
    owner_id: RouteSystemId,
    shortfall: float,
) -> BoundaryClearanceRequirement | None:
    target = graph.section_for_port(graph.ports[port_id])
    boundary = target.grid_row
    if boundary < 1:
        return None
    negative = tuple(
        section
        for section in graph.sections.values()
        if section.grid_row + section.grid_row_span <= boundary
    )
    if not negative:
        return None
    negative_edge = max(section.bbox_y + section.bbox_h for section in negative)
    blockers = tuple(
        sorted(
            section.id
            for section in negative
            if abs(section.bbox_y + section.bbox_h - negative_edge) <= COORD_TOLERANCE
        )
    )
    current = measured_distance(negative_edge, target.bbox_y)
    return BoundaryClearanceRequirement(
        SettlementAxis.ROW,
        boundary,
        str(owner_id),
        current + shortfall,
        blockers,
        (target.id,),
        f"route system {owner_id} same-destination corner runway",
    )


_BoundaryRequirementKey = tuple[
    SettlementAxis,
    int,
    str,
    tuple[str, ...],
    tuple[str, ...],
    BoundaryClearanceRequirementKind,
]


def _record_boundary_clearance_requirement(
    requirements: dict[_BoundaryRequirementKey, BoundaryClearanceRequirement],
    requirement: BoundaryClearanceRequirement,
) -> None:
    """Keep section-pair demands independent and coalesce exact duplicates.

    The key names ``axis`` and ``kind`` alongside the section pair so a
    ``GENERAL`` and a ``CORRIDOR_COHORT_APERTURE`` requirement (or a
    same-numbered boundary on different axes) never collapse into one
    entry that silently keeps only the first requirement's typed identity.
    """
    key = (
        requirement.axis,
        requirement.boundary,
        requirement.owner_id,
        requirement.negative_section_ids,
        requirement.positive_section_ids,
        requirement.kind,
    )
    current = requirements.setdefault(key, requirement)
    requirements[key] = replace(
        current,
        required=max(current.required, requirement.required),
    )


def _plan_source_turnouts(
    routes: Sequence[RoutedPath],
    graph: MetroGraph,
    curve_radius: float,
) -> None:
    """Freeze cross-member source curves for vertical arms of hidden forks."""
    incoming_by_line_target: defaultdict[tuple[str, str], list[RoutedPath]] = (
        defaultdict(list)
    )
    outgoing_by_line_source: defaultdict[tuple[str, str], list[RoutedPath]] = (
        defaultdict(list)
    )
    routes_by_source: defaultdict[str, list[RoutedPath]] = defaultdict(list)
    for route in routes:
        incoming_by_line_target[route.line_id, route.edge.target].append(route)
        outgoing_by_line_source[route.line_id, route.edge.source].append(route)
        routes_by_source[route.edge.source].append(route)

    candidates: list[_SourceTurnoutCandidate] = []
    for route in routes:
        if not route.is_inter_section or len(route.points) < 2:
            continue
        source = graph.stations.get(route.edge.source)
        target_port = graph.ports.get(route.edge.target)
        outgoing_direction = segment_direction(route.points[0], route.points[1])
        if (
            source is None
            or route.edge.source not in graph.junction_ids
            or not source.is_port
            or bool(source.label)
            or target_port is None
            or not target_port.is_entry
            or target_port.side not in (PortSide.TOP, PortSide.BOTTOM)
            or outgoing_direction not in (Direction.U, Direction.D)
        ):
            continue
        incoming = tuple(
            candidate
            for candidate in incoming_by_line_target[route.line_id, route.edge.source]
            if len(candidate.points) >= 2
            and _points_coincide(candidate.points[-1], route.points[0])
            and segment_direction(candidate.points[-2], candidate.points[-1])
            in (Direction.R, Direction.L)
        )
        if len(incoming) != 1:
            continue
        incoming_direction = segment_direction(
            incoming[0].points[-2], incoming[0].points[-1]
        )
        assert incoming_direction in (Direction.R, Direction.L)
        horizontal_siblings = tuple(
            candidate
            for candidate in outgoing_by_line_source[route.line_id, route.edge.source]
            if candidate is not route
            and len(candidate.points) >= 2
            and _points_coincide(candidate.points[0], route.points[0])
            and segment_direction(candidate.points[0], candidate.points[1])
            in (Direction.R, Direction.L)
        )
        continuing = tuple(
            candidate
            for candidate in horizontal_siblings
            if segment_direction(candidate.points[0], candidate.points[1])
            is incoming_direction
        )
        if horizontal_siblings and not continuing:
            continue
        selected = (
            min(continuing, key=lambda candidate: candidate.edge.target)
            if continuing
            else None
        )
        candidates.append(
            _SourceTurnoutCandidate(
                route,
                incoming[0],
                selected,
                incoming_direction,
                outgoing_direction,
            )
        )

    grouped: defaultdict[
        tuple[str, Direction, Direction], list[_SourceTurnoutCandidate]
    ] = defaultdict(list)
    for candidate in candidates:
        grouped[
            (
                candidate.route.edge.source,
                candidate.incoming_direction,
                candidate.outgoing_direction,
            )
        ].append(candidate)

    direction_vectors = {
        Direction.R: (1.0, 0.0),
        Direction.L: (-1.0, 0.0),
        Direction.U: (0.0, -1.0),
        Direction.D: (0.0, 1.0),
    }
    for members in grouped.values():
        incoming_vector = direction_vectors[members[0].incoming_direction]
        outgoing_vector = direction_vectors[members[0].outgoing_direction]
        radius_axis = (
            incoming_vector[0] - outgoing_vector[0],
            incoming_vector[1] - outgoing_vector[1],
        )
        peer_projections = [
            (route.points[0][0] * radius_axis[0])
            + (route.points[0][1] * radius_axis[1])
            for route in routes_by_source[members[0].route.edge.source]
            if len(route.points) >= 2
            and segment_direction(route.points[0], route.points[1])
            is members[0].outgoing_direction
            and any(
                len(incoming.points) >= 2
                and _points_coincide(incoming.points[-1], route.points[0])
                and segment_direction(incoming.points[-2], incoming.points[-1])
                is members[0].incoming_direction
                for incoming in incoming_by_line_target[
                    route.line_id, route.edge.source
                ]
            )
        ]
        projections = [
            (member.route.points[0][0] * radius_axis[0])
            + (member.route.points[0][1] * radius_axis[1])
            for member in members
        ]
        minimum = min(peer_projections)
        radii = [
            curve_radius + (projection - minimum) / 2.0 for projection in projections
        ]
        centres = [
            (
                member.route.points[0][0] - radius * radius_axis[0],
                member.route.points[0][1] - radius * radius_axis[1],
            )
            for member, radius in zip(members, radii, strict=True)
        ]
        if any(not _points_coincide(centre, centres[0]) for centre in centres[1:]):
            continue
        maximum_radius = max(radii)
        if any(
            member.continuing is not None
            and math.dist(member.continuing.points[0], member.continuing.points[1])
            < maximum_radius
            for member in members
        ):
            continue
        if any(
            math.dist(member.incoming.points[-2], member.incoming.points[-1]) < radius
            or math.dist(member.route.points[0], member.route.points[1]) < radius
            for member, radius in zip(members, radii, strict=True)
        ):
            continue
        for member, radius in zip(members, radii, strict=True):
            member.route.source_turnout = SourceTurnout(
                member.incoming.edge.source,
                (
                    member.continuing.edge.target
                    if member.continuing is not None
                    else None
                ),
                member.incoming_direction,
                member.outgoing_direction,
                radius,
            )


def _settle_plannable_short_destination_cohorts(
    routes: list[RoutedPath],
    graph: MetroGraph,
    ctx: _RoutingCtx,
    scaffold: RouteSemanticScaffold,
    *,
    allow_clearance_requirements: bool,
    granted_clearance_owner_ids: frozenset[str],
) -> tuple[
    Mapping[tuple[str, str, str], SettledExitTurn],
    tuple[BoundaryClearanceRequirement, ...],
]:
    """Seat complete short-run cohorts and publish their row demand."""
    settled_turns: dict[tuple[str, str, str], SettledExitTurn] = {}

    def settled_turn(
        route: RoutedPath,
        assignment: ExitTurnAssignment,
        points: Sequence[tuple[float, float]],
    ) -> SettledExitTurn:
        assert assignment.run_direction is not None
        assert assignment.turn_direction is not None
        assert assignment.minimum_runway is not None
        rank = len(points) - 3
        return SettledExitTurn(
            assignment.run_direction,
            assignment.turn_direction,
            points[rank - 1][0],
            assignment.minimum_runway,
            points[rank][0],
            route.concentric_corner_offsets_by_segment.get(rank, (None, None)),
            False,
        )

    requirements: dict[_BoundaryRequirementKey, BoundaryClearanceRequirement] = {}
    for bundle in iter_plannable_short_same_destination_bundles(
        routes,
        graph,
        ctx.offset_step,
        ctx.curve_radius,
        require_short_overlap=False,
    ):
        resolved = tuple(
            ResolvedEdge(route.edge.source, route.edge.target, route.line_id)
            for route, _tail in bundle.entries
        )
        system_ids = {scaffold.system_for_edge(edge) for edge in resolved}
        if len(system_ids) != 1:
            continue
        system_id = next(iter(system_ids))
        slots = same_destination_approach_slots(bundle, graph, ctx.offset_step)
        cohort_member_ids = {scaffold.member_id_by_edge[edge] for edge in resolved}
        replannable_plan_ids: set[str] = set()
        memberships = {}
        if ctx.exit_turns is not None:
            for route, _tail in bundle.entries:
                membership = ctx.exit_turns.membership_for_edge(route.edge)
                if membership is None or membership.axis is None:
                    continue
                assignment = membership.assignment
                if (
                    assignment is not None
                    and membership.axis.claimant_member_ids == (membership.member_id,)
                    and membership.member_id in cohort_member_ids
                    and assignment.run_direction in {Direction.R, Direction.L}
                    and assignment.turn_direction in {Direction.U, Direction.D}
                    and route.exit_turn_segment_rank == len(route.points) - 3
                ):
                    replannable_plan_ids.add(str(membership.plan.id))
                    memberships[id(route)] = membership
        clearance_granted = str(system_id) in granted_clearance_owner_ids
        if not allow_clearance_requirements and not clearance_granted:
            continue
        proposals = feasible_same_destination_approach_proposals(
            graph,
            routes,
            bundle,
            slots,
            movable_route_ids=frozenset(id(route) for route, _tail in bundle.entries),
            replannable_exit_turn_plan_ids=frozenset(replannable_plan_ids),
        )
        if proposals is None:
            continue

        vertical_lo = max(
            min(tail.trunk_y, tail.port_y) for tail in bundle.per_line.values()
        )
        vertical_hi = min(
            max(tail.trunk_y, tail.port_y) for tail in bundle.per_line.values()
        )
        shortfall = 2 * ctx.curve_radius - (vertical_hi - vertical_lo)
        requirement = (
            _short_destination_clearance_requirement(
                graph, bundle.port_id, system_id, shortfall
            )
            if allow_clearance_requirements and shortfall > COORD_TOLERANCE
            else None
        )
        if requirement is not None:
            _record_boundary_clearance_requirement(requirements, requirement)
            for proposal in proposals:
                route = proposal.route
                membership = memberships.get(id(route))
                if membership is None or membership.assignment is None:
                    continue
                settled_turns[(route.edge.source, route.edge.target, route.line_id)] = (
                    settled_turn(route, membership.assignment, route.points)
                )
            continue
        if allow_clearance_requirements or shortfall > COORD_TOLERANCE:
            continue

        for proposal in proposals:
            route = proposal.route
            membership = memberships.get(id(route))
            rank = len(proposal.points) - 3
            if (
                membership is not None
                and membership.assignment is not None
                and route.points[rank][0] != proposal.points[rank][0]
            ):
                settled_turns[(route.edge.source, route.edge.target, route.line_id)] = (
                    settled_turn(route, membership.assignment, proposal.points)
                )
            route.points = list(proposal.points)
            route.gap_slots = list(proposal.gap_slots)
    return MappingProxyType(settled_turns), tuple(
        requirements[key] for key in sorted(requirements)
    )


def build_member_geometry_execution(
    graph: MetroGraph,
    ctx: _RoutingCtx,
    scaffold: RouteSemanticScaffold,
    *,
    family_by_edge: Mapping[ResolvedEdge, RouteFamilyId],
    convergence_plans: tuple[ConvergencePlan, ...] = (),
    complete_path_system_ids: frozenset[RouteSystemId] = frozenset(),
    preliminary_gap_claims: tuple[PreliminaryGapChannelClaim, ...] = (),
    reservation_ids_by_member: Mapping[EmissionMemberId, tuple[str, ...]] | None = None,
    pending_exit_turn_plan_ids: frozenset[ExitTurnPlanId] = frozenset(),
    settled_exit_turn_plan_ids: frozenset[ExitTurnPlanId] = frozenset(),
    allow_clearance_requirements: bool = False,
    granted_clearance_owner_ids: frozenset[str] = frozenset(),
    corridor_cohort_ledger: CorridorCohortLedger | None = None,
    corridor_targets: Sequence[CorridorCohortTarget] = (),
    corridor_scalar_requests: Sequence[CorridorScalarRequest] = (),
) -> MemberGeometryExecution:
    """Freeze each eligible non-convergence member's sole production template."""
    cohort_settled_turns: Mapping[tuple[str, str, str], SettledExitTurn] = (
        MappingProxyType({})
    )
    clearance_requirements: tuple[BoundaryClearanceRequirement, ...] = ()
    corridor_cohorts: CorridorCohortPlan | None = None
    convergence_edges = frozenset(
        edge
        for plan in convergence_plans
        if plan.disposition is ConvergenceDisposition.PLANNED
        for edge in plan.resolved_member_edges
    )
    reservation_ids = reservation_ids_by_member or {}
    candidates: list[_MemberCandidate] = []
    context_routes: list[RoutedPath] = []
    failures: dict[RouteSystemId, str] = {}
    edges_by_system: dict[RouteSystemId, list[ResolvedEdge]] = defaultdict(list)
    for resolved in scaffold.edge_order:
        edges_by_system[scaffold.system_for_edge(resolved)].append(resolved)
    built_start = len(ctx.built_routes)
    try:
        for system_id in scaffold.ordered_system_ids:
            system_edges = tuple(edges_by_system.get(system_id, ()))
            system_start = len(ctx.built_routes)
            system_candidates: list[_MemberCandidate] = []
            for resolved in system_edges:
                key = (resolved.source, resolved.target, resolved.line_id)
                if resolved in convergence_edges:
                    context = _convergence_context_route(
                        ctx, key, family_by_edge.get(resolved)
                    )
                    if context is not None:
                        context_routes.append(context)
                    continue
                if key in ctx.skip_edges:
                    continue
                edge = ctx.edge_by_key.get(key)
                if edge is None:
                    failures[system_id] = "missing-emission-edge"
                    break
                family_id = family_by_edge.get(resolved)
                if family_id is None:
                    failures[system_id] = "missing-production-family"
                    break
                try:
                    route = _route_template(edge, family_id, ctx)
                except MemberGeometryDeclinedError:
                    failures[system_id] = "canonical-template-declined-member"
                    break
                system_candidates.append(
                    _MemberCandidate(
                        route,
                        family_id,
                        system_id,
                        semantic_route_id(
                            "member-channel-carrier",
                            resolved.source,
                            resolved.target,
                        ),
                        tuple(scaffold.connector_ids_for_edge(resolved)),
                        _packed_cell_handoff_metadata(edge, family_id, ctx),
                    )
                )
                ctx.built_routes.append(route)
            if system_id not in failures:
                candidates.extend(system_candidates)
                continue

            del ctx.built_routes[system_start:]

        candidate_routes = [candidate.route for candidate in candidates]
        # Each channel pass ranks a gap over its whole population, and the freeze
        # makes that rank permanent, so the immutable convergence strokes travel
        # with the candidates rather than being discovered after the fact.
        member_population = [*candidate_routes, *context_routes]
        deferred_exit_turn_ownership: list[
            tuple[RoutedPath, str | None, int | None]
        ] = []
        if settled_exit_turn_plan_ids:
            for route in candidate_routes:
                if route.exit_turn_plan_id not in settled_exit_turn_plan_ids:
                    continue
                deferred_exit_turn_ownership.append(
                    (route, route.exit_turn_axis_id, route.exit_turn_segment_rank)
                )
                route.exit_turn_axis_id = None
                route.exit_turn_segment_rank = None
        allocation_population = (
            [*ctx.built_routes[built_start:], *context_routes]
            if pending_exit_turn_plan_ids
            else member_population
        )
        normalization_population = member_population
        complete_path_population = [
            route
            for route in normalization_population
            if scaffold.system_for_edge(
                ResolvedEdge(route.edge.source, route.edge.target, route.line_id)
            )
            in complete_path_system_ids
        ]
        complete_path_route_ids = frozenset(
            id(route) for route in complete_path_population
        )
        _materialize_gap_slots(
            allocation_population,
            ctx,
            movable_exit_plan_ids=pending_exit_turn_plan_ids,
        )
        _settle_entry_wrap_leadouts(
            normalization_population,
            ctx,
            movable_route_ids=complete_path_route_ids,
        )
        _materialize_trunk_slots(normalization_population, ctx)
        # Trunk-slot materialization compares dip groups, never two flows that
        # entered one inter-row gap from opposite rows, so counter-running
        # trunks leave it within bundle pitch of each other and read as one
        # bundle.  The freeze is the last word on an owned channel, so the
        # direction bands have to be settled here rather than by the same pass
        # running after emission, which skips a plan-owned trunk.
        _separate_opposing_inter_row_trunks(normalization_population, ctx)
        _reconcile_port_peeloff_risers(complete_path_population, ctx)
        _coincide_same_line_tracks(normalization_population, ctx)
        _coincide_fanout_opening_descents(
            normalization_population, ctx, settle_frozen_arcs=True
        )
        _coincide_same_line_fanout_traverses(normalization_population, ctx)
        _bundle_divergent_distinct_traverses(candidate_routes, ctx)
        # Feeders converging on one entry port from opposite sides only nest
        # concentrically once their descent lanes are ordered by the port lane
        # they land in; the freeze is final, so that order has to be settled
        # here rather than by the same pass running after emission.
        _stagger_convergent_distinct_lines(normalization_population, ctx)
        eligible_claims = _eligible_preliminary_gap_claims(
            preliminary_gap_claims,
            failures,
        )
        _allocate_preliminary_gap_claims(
            tuple(candidates),
            eligible_claims,
            ctx,
            {
                system_id: rank
                for rank, system_id in enumerate(scaffold.ordered_system_ids)
            },
            pending_exit_turn_plan_ids,
        )
        _allocate_member_gap_channels(
            tuple(candidates), eligible_claims, ctx, pending_exit_turn_plan_ids
        )
        _align_packed_cell_handoffs(tuple(candidates), ctx, pending_exit_turn_plan_ids)
        candidate_route_ids = frozenset(id(route) for route in candidate_routes)
        # The freeze is the last word on an owned channel's coordinate: the
        # emission chain's own clearance hold reads the frozen ranks as
        # immovable.  So the corridor clearance has to be closed here, on every
        # pass, and over the same whole gap population the passes above ranked --
        # a bundle carrying an immutable convergence stroke is pinned by it, and
        # holding the candidates alone would slide them off that stroke.
        settled_tail_segments = _bundle_same_destination_tails(
            normalization_population,
            ctx,
            movable_route_ids=complete_path_route_ids,
        )

        settled_approach_segments = _settle_same_destination_approach_bundles(
            normalization_population,
            ctx,
            movable_route_ids=complete_path_route_ids,
        )
        _hold_runs_in_corridor_clearance(
            normalization_population,
            ctx,
            fixed_segment_keys=settled_tail_segments | settled_approach_segments,
        )
        _coincide_same_line_tracks(complete_path_population, ctx)
        cohort_settled_turns, clearance_requirements = (
            _settle_plannable_short_destination_cohorts(
                normalization_population,
                graph,
                ctx,
                scaffold,
                allow_clearance_requirements=allow_clearance_requirements,
                granted_clearance_owner_ids=granted_clearance_owner_ids,
            )
        )
        if pending_exit_turn_plan_ids:
            allocated_turns = _settled_exit_turns(
                allocation_population,
                ctx,
                pending_exit_turn_plan_ids,
            )
            _adopt_allocated_pending_paths(
                candidate_routes,
                allocation_population,
                ctx,
                pending_exit_turn_plan_ids,
            )
        else:
            if reservation_ids_by_member is not None:
                _seat_claimed_segments_before_freeze(tuple(candidates), ctx)
            allocated_turns = MappingProxyType({})
        settled_exit_turns = MappingProxyType(
            {**allocated_turns, **cohort_settled_turns}
        )
        _separate_fused_cotravelling_runs(
            normalization_population,
            ctx,
            movable_route_ids=complete_path_route_ids,
            secondary_movable_route_ids=candidate_route_ids,
            station_offsets=ctx.station_offsets,
            fixed_segment_keys=settled_tail_segments,
        )
        for route, axis_id, segment_rank in deferred_exit_turn_ownership:
            route.exit_turn_axis_id = axis_id
            route.exit_turn_segment_rank = segment_rank
        _rederive_semantic_end_corners(
            normalization_population,
            ctx.curve_radius,
            ctx.station_offsets or {},
            respect_owned_corners=False,
        )
        _plan_source_turnouts(normalization_population, graph, ctx.curve_radius)
        # The freeze is the last word on an owned route's radii, and a
        # post-emission pass skips a frozen or owned route, so the concentric
        # fans a per-corner reference seated off the floor have to be
        # re-anchored to CURVE_RADIUS here, before the plan captures them.
        _reanchor_concentric_corner_fans(
            normalization_population,
            ctx.station_offsets or {},
            ctx.curve_radius,
        )
        # A settled candidate route holds no route-system segment ownership; that
        # is conferred only by the freeze below.  The cohort compiler must read
        # the crossings a candidate has to clear, not the ownership a frozen plan
        # would report, so the aperture shortfall is measured on the candidates.
        # The compiler fails closed on a shortfall it cannot map to a requirement
        # unless the pass permits clearance requirements, so it runs only on a
        # pass that does.
        if corridor_cohort_ledger is not None and allow_clearance_requirements:
            context_candidates = _corridor_cohort_context_candidates(
                context_routes,
                candidate_route_ids,
                scaffold,
                family_by_edge,
            )
            corridor_cohorts, _cohort_ranks, corridor_requirements = (
                _compile_corridor_cohorts(
                    corridor_cohort_ledger,
                    tuple(candidates),
                    context_candidates,
                    scaffold,
                    ctx,
                    allow_clearance_requirements=allow_clearance_requirements,
                    additional_targets=corridor_targets,
                    scalar_requests=corridor_scalar_requests,
                )
            )
            _validate_corridor_cohort_outcome(corridor_cohorts, corridor_requirements)
            clearance_requirements = clearance_requirements + corridor_requirements
        semantic_corner_templates = {
            ResolvedEdge(
                route.edge.source,
                route.edge.target,
                route.line_id,
            ): _semantic_corner_template(route)
            for route in normalization_population
        }
        plans = tuple(
            _freeze_plan(
                scaffold,
                candidate,
                ctx,
                reservation_ids,
                owns_complete_path=candidate.system_id in complete_path_system_ids,
            )
            for candidate in candidates
        )
        semantic_corner_templates.update(
            {
                plan.edge: (
                    plan.curve_radii,
                    plan.concentric_corner_offsets_by_segment,
                    plan.concentric_corner_bases_by_segment,
                )
                for plan in plans
            }
        )
    finally:
        del ctx.built_routes[built_start:]

    return MemberGeometryExecution(
        plans,
        MappingProxyType(failures),
        MappingProxyType({plan.edge: plan for plan in plans}),
        settled_exit_turns,
        MappingProxyType(semantic_corner_templates),
        clearance_requirements,
        corridor_cohorts,
    )


def settle_member_geometry_corner_cohorts(
    execution: MemberGeometryExecution,
    scaffold: RouteSemanticScaffold,
    family_by_edge: Mapping[ResolvedEdge, RouteFamilyId],
    ctx: _RoutingCtx,
) -> MemberGeometryExecution:
    """Freeze exact same-line corner cohorts after every geometry owner settles."""
    routes: list[RoutedPath] = []
    plan_by_edge = {plan.edge: plan for plan in execution.plans}
    built_start = len(ctx.built_routes)
    try:
        for resolved in scaffold.edge_order:
            if resolved not in execution._semantic_corner_templates:
                continue
            edge = ctx.edge_by_key.get(
                (resolved.source, resolved.target, resolved.line_id)
            )
            family_id = family_by_edge.get(resolved)
            if edge is None or family_id is None:
                continue
            plan = plan_by_edge.get(resolved)
            route = (
                fresh_member_route(plan, edge)
                if plan is not None
                else _route_template(edge, family_id, ctx)
            )
            execution.apply_semantic_corner_template(route)
            routes.append(route)
            ctx.built_routes.append(route)
        _unify_coincident_corner_radii(routes, include_owned=True)
        _restore_clear_planned_landing_radii(routes, ctx.curve_radius)
    finally:
        del ctx.built_routes[built_start:]

    route_by_edge = {
        ResolvedEdge(route.edge.source, route.edge.target, route.line_id): route
        for route in routes
    }
    plans = tuple(
        replace(
            plan,
            curve_radii=(
                None
                if route_by_edge[plan.edge].curve_radii is None
                else tuple(route_by_edge[plan.edge].curve_radii or ())
            ),
            concentric_corner_offsets_by_segment=tuple(
                sorted(
                    route_by_edge[
                        plan.edge
                    ].concentric_corner_offsets_by_segment.items()
                )
            ),
            concentric_corner_bases_by_segment=tuple(
                sorted(
                    route_by_edge[plan.edge].concentric_corner_bases_by_segment.items()
                )
            ),
        )
        for plan in execution.plans
    )
    templates = dict(execution._semantic_corner_templates)
    templates.update(
        {
            resolved: _semantic_corner_template(route)
            for resolved, route in route_by_edge.items()
        }
    )
    templates.update(
        {
            plan.edge: (
                plan.curve_radii,
                plan.concentric_corner_offsets_by_segment,
                plan.concentric_corner_bases_by_segment,
            )
            for plan in plans
        }
    )
    return MemberGeometryExecution(
        plans,
        execution.failure_reasons,
        MappingProxyType({plan.edge: plan for plan in plans}),
        execution.settled_exit_turns,
        MappingProxyType(templates),
        execution.clearance_requirements,
        execution.corridor_cohorts,
    )


def _restore_clear_planned_landing_radii(
    routes: Sequence[RoutedPath], curve_radius: float
) -> None:
    """Give a settled fan landing its standard curve when its runway permits."""
    by_plan: defaultdict[str, list[RoutedPath]] = defaultdict(list)
    for route in routes:
        if route.exit_turn_plan_id is not None:
            by_plan[route.exit_turn_plan_id].append(route)
    for members in by_plan.values():
        if len({route.line_id for route in members}) < 2:
            continue
        for route in members:
            rank = route.exit_turn_segment_rank
            if (
                rank is None
                or route.curve_radii is None
                or rank >= len(route.curve_radii)
                or route.curve_radii[rank] >= curve_radius - COORD_TOLERANCE_FINE
            ):
                continue
            desired = list(route.curve_radii)
            desired[rank] = curve_radius
            if (
                resolve_curve_radius_at(route.points, desired, rank)
                < curve_radius - COORD_TOLERANCE_FINE
            ):
                continue
            route.record_concentric_corner(rank, 0.0, curve_radius)
            route.curve_radii[rank] = curve_radius


def fresh_member_route(plan: RouteMemberGeometryPlan, edge: Edge) -> RoutedPath:
    """Materialise a normalizable path carrying its immutable owned segments."""
    route = RoutedPath(
        edge,
        edge.line_id,
        list(plan.points),
        is_inter_section=True,
        curve_radii=None if plan.curve_radii is None else list(plan.curve_radii),
        offset_regime=plan.offset_regime,
        normalize_exempt=plan.normalize_exempt,
        gap_slots=list(plan.gap_slots),
        trunk_slot=plan.trunk_slot,
        concentric_corner_offsets_by_segment=dict(
            plan.concentric_corner_offsets_by_segment
        ),
        concentric_corner_bases_by_segment=dict(
            plan.concentric_corner_bases_by_segment
        ),
        exit_turn_plan_id=plan.exit_turn_plan_id,
        exit_turn_member_id=plan.exit_turn_member_id,
        exit_turn_family_id=plan.exit_turn_family_id,
        exit_turn_axis_id=plan.exit_turn_axis_id,
        fan_plan_id=plan.fan_plan_id,
        fan_route_emitter=plan.fan_route_emitter,
        exit_turn_segment_rank=plan.exit_turn_segment_rank,
        exit_lane_transition_plan_id=plan.exit_lane_transition_plan_id,
        source_turnout=plan.source_turnout,
        member_geometry_plan_id=str(plan.id),
        route_system_owned_segment_ranks=plan.owned_segment_ranks,
    )
    return route


def validate_member_geometry_emission(
    routes: list[RoutedPath], execution: MemberGeometryExecution
) -> None:
    """Require every emitted member to retain its owned channel geometry."""
    for route in routes:
        plan = execution.plan_for_edge(route.edge)
        if plan is None:
            continue
        if tuple(route.route_system_owned_segment_ranks) != plan.owned_segment_ranks:
            raise RuntimeError(f"member geometry plan {plan.id} lost channel ownership")
        for channel in plan.gap_channels:
            actual = tuple(
                route.points[channel.segment_rank : channel.segment_rank + 2]
            )
            if actual != (channel.start, channel.end):
                raise RuntimeError(
                    f"member geometry plan {plan.id} channel geometry changed"
                )
        radii = route.curve_radii or ()
        planned_radii = plan.curve_radii or ()
        for channel in plan.gap_channels:
            offsets = route.concentric_corner_offsets_by_segment.get(
                channel.segment_rank
            )
            bases = route.concentric_corner_bases_by_segment.get(channel.segment_rank)
            first_radius_index = max(0, channel.segment_rank - 1)
            for radius_index in range(
                first_radius_index,
                min(channel.segment_rank + 1, len(planned_radii)),
            ):
                input_index = radius_index - (channel.segment_rank - 1)
                if radius_index >= len(radii):
                    raise RuntimeError(
                        f"member geometry plan {plan.id} lost corner radius at index "
                        f"{radius_index}"
                    )
                offset = None if offsets is None else offsets[input_index]
                base = None if bases is None else bases[input_index]
                if offsets is None or bases is None or offset is None or base is None:
                    raise RuntimeError(
                        f"member geometry plan {plan.id} corner radius at index "
                        f"{radius_index} has no concentric inputs"
                    )
                if radius_index + 2 >= len(route.points):
                    raise RuntimeError(
                        f"member geometry plan {plan.id} corner radius at index "
                        f"{radius_index} has no complete corner points"
                    )
                previous, corner, following = route.points[
                    radius_index : radius_index + 3
                ]
                expected_radius = concentric_corner_radius_at(
                    previous,
                    corner,
                    following,
                    offset,
                    base_radius=base,
                )
                actual_radius = radii[radius_index]
                if abs(actual_radius - expected_radius) > COORD_TOLERANCE_FINE:
                    raise RuntimeError(
                        f"member geometry plan {plan.id} corner radius "
                        f"{actual_radius!r} at index {radius_index} differs from "
                        f"its concentric radius {expected_radius!r}"
                    )
        planned_offsets = dict(plan.concentric_corner_offsets_by_segment)
        planned_bases = dict(plan.concentric_corner_bases_by_segment)
        for radius_index, planned_radius in enumerate(planned_radii):
            corner_rank = radius_index + 1
            if not any(
                owned_rank in plan.owned_segment_ranks
                for owned_rank in (corner_rank - 1, corner_rank)
            ):
                continue
            if radius_index >= len(radii):
                raise RuntimeError(
                    f"member geometry plan {plan.id} lost owned corner radius at "
                    f"index {radius_index}"
                )
            if abs(radii[radius_index] - planned_radius) > COORD_TOLERANCE_FINE:
                raise RuntimeError(
                    f"member geometry plan {plan.id} owned corner radius changed "
                    f"at index {radius_index}"
                )
            for segment_rank, tuple_index in (
                (radius_index, 1),
                (radius_index + 1, 0),
            ):
                expected_offset = planned_offsets.get(segment_rank, (None, None))[
                    tuple_index
                ]
                actual_offset = route.concentric_corner_offsets_by_segment.get(
                    segment_rank, (None, None)
                )[tuple_index]
                expected_base = planned_bases.get(segment_rank, (None, None))[
                    tuple_index
                ]
                actual_base = route.concentric_corner_bases_by_segment.get(
                    segment_rank, (None, None)
                )[tuple_index]
                if actual_offset != expected_offset or actual_base != expected_base:
                    raise RuntimeError(
                        f"member geometry plan {plan.id} owned corner inputs changed "
                        f"at index {radius_index}"
                    )


def plan_corridor_cohorts(
    ledger: CorridorCohortLedger,
    targets: Sequence[CorridorCohortTarget],
    *,
    scalar_requests: Sequence[CorridorScalarRequest] = (),
) -> CorridorCohortPlan:
    """Compile one corridor cohort snapshot into heterogeneous grants.

    Passes member and convergence scalar requests to
    :func:`compile_corridor_cohort_plan` and returns member allocations, exact
    route patches and convergence scalar grants in one plan. It solves without
    publishing: the caller decides whether to apply the returned patches, so no
    geometry moves here.
    """
    return compile_corridor_cohort_plan(
        ledger, targets, scalar_requests=scalar_requests
    )
