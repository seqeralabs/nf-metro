"""Monotone row and column envelope settlement around route reservations.

Local station geometry, section bboxes, header keep-outs, and the immutable
``RouteReservation`` ledger are all final before this runs.  Settlement owns one
thing only: the global row and column offsets that give every boundary the width
it owes.  It never resizes a box, moves a station inside its section, or revisits
a route decision.

Two kinds of demand say what a boundary owes, and both are measured, frozen
inputs to the one sweep below.  A ``RouteReservation`` states the width a *run*
crossing the boundary needs there.  A ``BoundaryClearanceDemand`` states the
clearance the boundary owes between the boxes facing across it, which a local
box resize can eat without any run being involved.  A boundary carrying both is
widened once, by the larger: the two are deficits at the same coordinate, so one
translation pays them both and neither needs a second owner behind this stage.

Termination is structural, and it depends on settling against ONE ledger.  Each
adjacent-index boundary (the gap between row ``b-1`` and row ``b``, or between
column ``b-1`` and column ``b``) is visited exactly once in ascending order, and
translating everything from ``b`` onward has three effects and no others:

* boundaries before ``b`` keep both blockers stationary, so they are unchanged;
* boundary ``b`` widens by exactly the translated amount;
* boundaries after ``b`` move both blockers together, so they are unchanged.

A section belongs to the band holding its grid start, so boundary ``b`` owns
every section starting at or beyond it.  A section straddling ``b`` starts above
it and stays: carrying it would take its upper portion into the gap above and
narrow that separation, and no step here may shrink a satisfied gap.  Holding it
is sound exactly when it does not bound a corridor the translation widened -- if
it did, the widening never reached that corridor.  Both halves of that are
asserted on the settled geometry rather than argued: every facing pair is
re-measured for monotonicity, and every straddling section is checked against the
blockers of each corridor its boundary settled.  The sweep is therefore a single
directional pass over a finite set of boundaries.

The two axes settle in sequence -- every row boundary, then every column
boundary -- and the row phase is never revisited.  That is sound because a
column translation writes only x, so it cannot move an edge a row corridor is
measured between; it can reach a row corridor only by changing which sections
the corridor's run overlaps.  A corridor scoped to its topology span selects by
grid index, which no translation moves.  A run-scoped corridor selects by
x-overlap, and sections at or beyond the translated boundary move together with
whatever part of the run reaches them, so a section clear of the run cannot be
drawn into it -- unless it spans across the translated boundary, in which case it
stays put while a crossing run lengthens.  That single exception is checked
rather than assumed: every row corridor is re-measured after the column phase
and a narrowed one fails.  Nothing is retried.

Re-routing the settled geometry can produce a *different* ledger -- corridors
appear, vanish, and change their required width -- so this function never
iterates over successive ledgers.  Each invocation settles exactly the ledger
it was handed.  The renderer has one bounded exception around a provisional
convergence-clearance grant: it performs a strict fresh observation after the
grant, measures that observation's drawn containment once, and settles that
final ledger before a consuming re-route.  No clearance requirement may be
published by either strict observation.

Every row- and column-gap claim this stage is handed is therefore allocatable:
the measurement bounds a boundary by the sections that lie wholly on each side
of it, so a boundary every relevant section straddles has no side to measure and
is never chosen as a corridor's region.  A deficit that survives the sweep is an
infeasible arrangement, not a claim outside this stage's reach, and the closing
guard refuses it on the strict path.  A clearance demand is allocatable by the
same lemma: every box it measures *from* ends above the boundary and every box it
measures *to* starts at or beyond it, which are the two halves of
``translation_ownership``.

The axis vocabulary the sweep is written against -- ``ROW_AXIS``, ``COLUMN_AXIS``,
``SettlementAxisGeometry``, ``translation_ownership`` and ``apply_translation`` --
states the row and column steps as one parameterised write.  Anything that needs
to know what a translation moves reads it from here, so a second definition
cannot drift away from the one the sweep applies.

Every failure this module raises is an engine self-check rather than an authoring
diagnostic: each condition is one the reasoning above establishes, so a violation
says the reasoning and the code have come apart.  They raise
``PhaseInvariantError`` and are not downgraded on any render mode, because a
best-effort diagram drawn past a broken allocation lemma is a diagram whose
geometry nothing vouches for.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace

from nf_metro.layout.constants import (
    COORD_TOLERANCE,
    SAME_COORD_TOLERANCE,
    SETTLEMENT_QUANTUM,
)
from nf_metro.layout.geometry import measured_distance, shift_section
from nf_metro.layout.phases.guards import PhaseInvariantError
from nf_metro.layout.route_plan import (
    DemandAxis,
    EmissionMemberId,
    RoutePlan,
    RoutePlanDiagnostic,
)
from nf_metro.layout.route_reservations import (
    SECTION_BOTTOM_BLOCKER,
    ColumnGapRegion,
    ReservationCoordinateTranslation,
    RouteReservation,
    RouteReservationId,
    RowGapRegion,
    drawn_corridor_containment,
    realise_reservation,
)
from nf_metro.layout.settlement_demand import (
    BoundaryClearanceDemand,
    BoundaryClearanceRequirement,
    ClearanceMeasurement,
    SettlementAxis,
)
from nf_metro.parser.model import MetroGraph, Section

__all__ = [
    "COLUMN_AXIS",
    "ROW_AXIS",
    "BoundaryClearanceDemand",
    "BoundaryClearanceRequirement",
    "ClearanceMeasurement",
    "DrawnCorridorClearanceRequirement",
    "EnvelopeSettlement",
    "SettlementAxis",
    "SettlementAxisGeometry",
    "SettlementShortfall",
    "SettlementTranslation",
    "TranslationOwnership",
    "apply_translation",
    "attach_reroute_ledger_delta",
    "attach_settlement_diagnostics",
    "drawn_corridor_clearance_requirements",
    "measure_boundary_clearance_requirements",
    "measure_drawn_corridor_clearance",
    "settle_route_envelopes",
    "translation_ownership",
]


def measure_boundary_clearance_requirements(
    graph: MetroGraph,
    requirements: tuple[BoundaryClearanceRequirement, ...],
) -> tuple[BoundaryClearanceDemand, ...]:
    """Measure stable pairwise section requirements on the live graph."""
    demands: list[BoundaryClearanceDemand] = []
    for requirement in requirements:
        negative = [graph.sections[item] for item in requirement.negative_section_ids]
        positive = [graph.sections[item] for item in requirement.positive_section_ids]
        if requirement.axis is SettlementAxis.ROW:
            negative_edge = max(item.bbox_y + item.bbox_h for item in negative)
            positive_edge = min(item.bbox_y for item in positive)
        else:
            negative_edge = max(item.bbox_x + item.bbox_w for item in negative)
            positive_edge = min(item.bbox_x for item in positive)
        deficit = requirement.required - measured_distance(negative_edge, positive_edge)
        if deficit <= SAME_COORD_TOLERANCE:
            continue
        demands.append(
            BoundaryClearanceDemand(
                requirement.axis,
                requirement.boundary,
                requirement.required,
                deficit,
                requirement.negative_section_ids,
                requirement.description,
                owner_id=requirement.owner_id,
            )
        )
    return tuple(demands)


@dataclass(frozen=True, slots=True)
class DrawnCorridorClearanceRequirement:
    """Stable width required by one strict route's drawn corridor."""

    reservation: RouteReservation
    required: float


def drawn_corridor_clearance_requirements(
    graph: MetroGraph,
    plan: RoutePlan,
    route_polylines: Sequence[Sequence[tuple[float, float]]],
) -> tuple[DrawnCorridorClearanceRequirement, ...]:
    """Freeze widths owed by strict routes drawn past a corridor edge."""
    requirements: list[DrawnCorridorClearanceRequirement] = []
    for reservation in plan.reservations:
        region = reservation.region
        if not isinstance(region, RowGapRegion | ColumnGapRegion):
            continue
        realised = realise_reservation(graph, reservation)
        if realised is None:
            continue
        containment = drawn_corridor_containment(
            reservation, realised, route_polylines, reservation.claims
        )
        deficit = -containment.positive_side_slack
        if deficit <= COORD_TOLERANCE:
            continue
        requirements.append(
            DrawnCorridorClearanceRequirement(
                reservation, realised.available_width + deficit
            )
        )
    return tuple(requirements)


def measure_drawn_corridor_clearance(
    graph: MetroGraph,
    requirements: Iterable[DrawnCorridorClearanceRequirement],
) -> tuple[BoundaryClearanceDemand, ...]:
    """Re-measure strict drawn-corridor requirements on live box edges."""
    demands: list[BoundaryClearanceDemand] = []
    for requirement in requirements:
        reservation = requirement.reservation
        realised = realise_reservation(graph, reservation)
        if realised is None:
            continue
        deficit = requirement.required - realised.available_width
        if deficit <= COORD_TOLERANCE:
            continue
        region = reservation.region
        axis = ROW_AXIS if isinstance(region, RowGapRegion) else COLUMN_AXIS
        demands.append(
            BoundaryClearanceDemand(
                axis.axis,
                axis.boundary_of(reservation),
                requirement.required,
                deficit,
                (),
                f"the drawn corridor claimed by {reservation.id}",
                owner_id=str(reservation.id),
            )
        )
    return tuple(demands)


@dataclass(frozen=True, slots=True)
class SettlementTranslation:
    """One applied global translation of everything from *boundary* onward.

    ``coordinate`` is where the translated band started before the move -- the
    smallest origin among the moved sections -- so a frozen claim coordinate can
    be told apart as sitting inside or ahead of the band.

    One boundary takes one translation, and exactly one of two demands sized it:
    ``clearance`` names the boundary's own clearance demand, or
    ``reservation_id`` and ``claimant_member_ids`` name the corridor claim.  The
    two are mutually exclusive -- a clearance-sized move carries no claimant, a
    claim-sized move carries no clearance -- so a reader may take whichever is
    set as the cause.  ``reservation_ids`` is every claim the move settled at that
    boundary, whether or not one of them sized it.
    """

    axis: SettlementAxis
    boundary: int
    coordinate: float
    amount: float
    reservation_id: RouteReservationId | None
    claimant_member_ids: tuple[EmissionMemberId, ...]
    blocker_ids: tuple[str, ...]
    section_ids: tuple[str, ...]
    reservation_ids: tuple[RouteReservationId, ...]
    spanning_section_ids: tuple[str, ...] = ()
    """Sections straddling the boundary, which this translation cannot own."""
    clearance: BoundaryClearanceDemand | None = None
    """The boundary's clearance demand, when that is what sized this move."""

    @property
    def message(self) -> str:
        if self.clearance is not None:
            demand = self.clearance.description
        else:
            claimants = ", ".join(sorted(self.claimant_member_ids))
            demand = f"the corridor claimed by {claimants}"
        blockers = ", ".join(sorted(self.blocker_ids))
        held = (
            f", holding {', '.join(self.spanning_section_ids)} in place across it"
            if self.spanning_section_ids
            else ""
        )
        return (
            f"{self.axis.value} boundary {self.boundary} widened by "
            f"{self.amount:.2f}px for {demand}, "
            f"held from below by {blockers}; it moved "
            f"{len(self.section_ids)} section(s){held} and settled "
            f"{len(self.reservation_ids)} claim(s)"
        )


@dataclass(frozen=True, slots=True)
class SettlementShortfall:
    """A demand settlement was handed and did not meet.

    Measured against the ledger settlement was given, on the geometry it left
    behind.  Re-routing publishes a different ledger, so a deficit found there
    says something about the new constraint set, not about whether settlement
    honoured its own.
    """

    reservation_id: RouteReservationId
    claimant_member_ids: tuple[EmissionMemberId, ...]
    required_width: float
    available_width: float

    @property
    def message(self) -> str:
        claimants = ", ".join(sorted(self.claimant_member_ids))
        return (
            f"the corridor claimed by {claimants} still has "
            f"{self.available_width:.2f}px against the {self.required_width:.2f}px "
            f"its reservation requires"
        )


@dataclass(frozen=True, slots=True)
class EnvelopeSettlement:
    """What one settlement pass moved, and what it could not.

    ``coordinate_translations`` is the same set of moves expressed as they act
    on frozen claim coordinates, for every later measurement of the ledger
    settlement consumed.
    """

    translations: tuple[SettlementTranslation, ...]
    shortfalls: tuple[SettlementShortfall, ...] = ()
    coordinate_translations: tuple[ReservationCoordinateTranslation, ...] = ()


@dataclass(frozen=True, slots=True)
class SettlementAxisGeometry:
    """The per-axis differences between row and column settlement.

    One of :data:`ROW_AXIS` or :data:`COLUMN_AXIS`: every step of the sweep is
    written once against this, so which grid index a boundary separates, which
    coordinate a translation writes, and which section edge bounds it are read
    from here rather than branched on.
    """

    axis: SettlementAxis
    boundary_of: Callable[[RouteReservation], int]
    start_index: Callable[[Section], int]
    span: Callable[[Section], int]
    origin: Callable[[Section], float]
    shift: Callable[[MetroGraph, Section, float], None]


@dataclass(frozen=True, slots=True)
class TranslationOwnership:
    """Which sections one boundary translation owns, and which it cannot.

    A section belongs to the band holding its grid start, so a boundary
    translation owns every section starting at or beyond it.  A section
    straddling the boundary starts above it and stays: carrying it would take
    its upper portion into the gap above and narrow that separation, which no
    settlement step may do.  Naming both sets makes the ownership a computed
    fact rather than a side effect of the comparison that applies it.
    """

    moved_section_ids: tuple[str, ...]
    spanning_section_ids: tuple[str, ...]


def translation_ownership(
    graph: MetroGraph, axis: SettlementAxisGeometry, boundary: int
) -> TranslationOwnership:
    """Split *graph*'s sections into the ones *boundary* may carry and the rest."""
    moved: list[str] = []
    spanning: list[str] = []
    for key, section in graph.sections.items():
        start = axis.start_index(section)
        if start >= boundary:
            moved.append(key)
        elif start + axis.span(section) > boundary:
            spanning.append(key)
    return TranslationOwnership(tuple(sorted(moved)), tuple(sorted(spanning)))


def apply_translation(
    graph: MetroGraph,
    axis: SettlementAxisGeometry,
    ownership: TranslationOwnership,
    amount: float,
) -> None:
    """Move every section *ownership* names by *amount* along *axis*.

    The whole of settlement's write, so a caller that widens a boundary the way
    settlement would -- a counterfactual asking what a wider boundary would let
    the planner do -- moves the same sections by the same shift rather than a
    lookalike of it.
    """
    for section_id in ownership.moved_section_ids:
        axis.shift(graph, graph.sections[section_id], amount)


ROW_AXIS = SettlementAxisGeometry(
    SettlementAxis.ROW,
    lambda reservation: _row_region(reservation).lower_row,
    lambda section: section.grid_row,
    lambda section: section.grid_row_span,
    lambda section: section.bbox_y,
    lambda graph, section, amount: shift_section(graph, section, dy=amount),
)
"""Row settlement: boundaries between grid rows, translating y."""

COLUMN_AXIS = SettlementAxisGeometry(
    SettlementAxis.COLUMN,
    lambda reservation: _column_region(reservation).right_column,
    lambda section: section.grid_col,
    lambda section: section.grid_col_span,
    lambda section: section.bbox_x,
    lambda graph, section, amount: shift_section(graph, section, dx=amount),
)
"""Column settlement: boundaries between grid columns, translating x."""


def _row_region(reservation: RouteReservation) -> RowGapRegion:
    assert isinstance(reservation.region, RowGapRegion)
    return reservation.region


def _column_region(reservation: RouteReservation) -> ColumnGapRegion:
    assert isinstance(reservation.region, ColumnGapRegion)
    return reservation.region


def _reservations_on(
    plan: RoutePlan, region_type: type
) -> tuple[RouteReservation, ...]:
    return tuple(
        reservation
        for reservation in plan.reservations
        if isinstance(reservation.region, region_type)
    )


def _reservation_coordinate_translation(
    translation: SettlementTranslation,
    plan: RoutePlan,
) -> ReservationCoordinateTranslation:
    """Express one applied settlement move as it acts on frozen claim coordinates.

    A member whose endpoints both sit in moved sections had its whole run
    carried, so every coordinate it claimed moves; a member with one endpoint
    in the moved band was stretched across the boundary, so only the
    coordinates at or beyond the band's start move.
    """
    section_ids = frozenset(translation.section_ids)
    fully_owned: list[EmissionMemberId] = []
    crossing: list[EmissionMemberId] = []
    for member in plan.members:
        source_owned = member.source.section_id in section_ids
        target_owned = member.target.section_id in section_ids
        if source_owned and target_owned:
            fully_owned.append(member.id)
        elif source_owned != target_owned:
            crossing.append(member.id)
    return ReservationCoordinateTranslation(
        DemandAxis.Y if translation.axis is SettlementAxis.ROW else DemandAxis.X,
        translation.coordinate,
        translation.amount,
        tuple(fully_owned),
        tuple(crossing),
    )


def _clearance_at(
    graph: MetroGraph,
    axis: SettlementAxisGeometry,
    clearance: ClearanceMeasurement | None,
) -> dict[int, BoundaryClearanceDemand]:
    """This axis's clearance demands, keyed by boundary, on the live boxes."""
    if clearance is None:
        return {}
    return _clearance_demands_at(clearance(graph), axis)


def _clearance_demands_at(
    measured: Iterable[BoundaryClearanceDemand],
    axis: SettlementAxisGeometry,
) -> dict[int, BoundaryClearanceDemand]:
    """Select the largest measured deficit at each boundary on one axis."""
    demands: dict[int, BoundaryClearanceDemand] = {}
    for demand in measured:
        current = demands.get(demand.boundary)
        if demand.axis is axis.axis and (
            current is None or demand.deficit > current.deficit
        ):
            demands[demand.boundary] = demand
    return demands


def quantised_allocation(deficit: float) -> float:
    """The translation that settles *deficit*, in whole ``SETTLEMENT_QUANTUM``.

    Two claims are on this, and both are needed.  It never allocates less than
    it was asked for, which is the premise of the ownership lemma: a boundary
    widened by this much has met the demand outright, so the pass visits each
    boundary once and stops.  A positive allocation is at least two quanta so
    it clears the coordinate-tolerance floor enforced by the translation
    ledger.  And it is a function of the deficit alone, so the same arrangement
    allocates the same width wherever on the canvas it sits.

    The second does not come from here -- a ceiling amplifies whatever it is
    handed, turning a 1e-13 arithmetic residue into a whole quantum of map -- but
    from the deficit arriving as a distance rather than as the remains of a
    subtraction of two large coordinates.
    :func:`nf_metro.layout.route_reservations.measured_distance` is where that is
    established.
    """
    quanta = math.ceil(deficit / SETTLEMENT_QUANTUM)
    if quanta <= 0:
        return 0.0
    return max(2, quanta) * SETTLEMENT_QUANTUM


def _settle_axis(
    graph: MetroGraph,
    plan: RoutePlan,
    reservations: tuple[RouteReservation, ...],
    axis: SettlementAxisGeometry,
    prior_translations: tuple[ReservationCoordinateTranslation, ...] = (),
    clearance: ClearanceMeasurement | None = None,
) -> tuple[
    list[SettlementTranslation],
    list[ReservationCoordinateTranslation],
]:
    by_boundary: dict[int, list[RouteReservation]] = {}
    for reservation in reservations:
        by_boundary.setdefault(axis.boundary_of(reservation), []).append(reservation)

    translations: list[SettlementTranslation] = []
    coordinate_translations = list(prior_translations)
    boundaries = set(by_boundary) | set(_clearance_at(graph, axis, clearance))
    for boundary in sorted(boundaries):
        projected_prefix = tuple(coordinate_translations)
        claims: list[tuple[float, RouteReservation, tuple[str, ...]]] = []
        for reservation in sorted(by_boundary.get(boundary, ()), key=lambda i: i.id):
            realised = realise_reservation(
                graph, reservation, coordinate_translations=projected_prefix
            )
            if realised is None:
                continue
            deficit = -realised.capacity_slack
            if deficit <= COORD_TOLERANCE:
                continue
            claims.append((deficit, reservation, realised.negative_blocker_ids))
        demand = _clearance_at(graph, axis, clearance).get(boundary)
        widest_claim = max(claims, key=lambda item: item[0], default=None)
        claim_deficit = 0.0 if widest_claim is None else widest_claim[0]
        demand_deficit = 0.0 if demand is None else demand.deficit
        # One boundary, one translation: the wider of the two demands pays for
        # both, and which one won is recorded so the move has a named cause.
        if demand is not None and demand_deficit > claim_deficit:
            deficit = demand_deficit
            reservation_id: RouteReservationId | None = None
            claimants: tuple[EmissionMemberId, ...] = ()
            blockers = tuple(
                f"{SECTION_BOTTOM_BLOCKER}:{item}"
                for item in demand.blocker_section_ids
            )
            driving_demand: BoundaryClearanceDemand | None = demand
        elif widest_claim is not None:
            deficit, driving_reservation, blockers = widest_claim
            reservation_id = driving_reservation.id
            claimants = driving_reservation.claimant_member_ids
            driving_demand = None
        else:
            continue
        amount = quantised_allocation(deficit)
        ownership = translation_ownership(graph, axis, boundary)
        if not ownership.moved_section_ids:
            raise PhaseInvariantError(
                f"{axis.axis.value} boundary {boundary} has no translation "
                "owner: nothing sits at or beyond it to move"
            )
        band_start = min(
            axis.origin(graph.sections[section_id])
            for section_id in ownership.moved_section_ids
        )
        apply_translation(graph, axis, ownership, amount)
        translations.append(
            SettlementTranslation(
                axis=axis.axis,
                boundary=boundary,
                coordinate=band_start,
                amount=amount,
                reservation_id=reservation_id,
                claimant_member_ids=claimants,
                blocker_ids=blockers,
                section_ids=ownership.moved_section_ids,
                reservation_ids=tuple(item[1].id for item in claims),
                spanning_section_ids=ownership.spanning_section_ids,
                clearance=driving_demand,
            )
        )
        coordinate_translations.append(
            _reservation_coordinate_translation(translations[-1], plan)
        )
    return (
        translations,
        coordinate_translations[len(prior_translations) :],
    )


def attach_settlement_diagnostics(
    plan: RoutePlan, settlement: EnvelopeSettlement
) -> RoutePlan:
    """Publish what settlement moved, into *plan*.

    A record nobody can read is not evidence.  Emitting each translation as a
    non-blocking plan diagnostic gives every widened row and column boundary a
    named cause in the published plan, so a reader can tell which demand moved
    the map without re-deriving the sweep.
    """
    added = tuple(
        RoutePlanDiagnostic(
            None, "envelope-settlement-translation", item.message, blocking=False
        )
        for item in settlement.translations
    )
    if not added:
        return plan
    return replace(plan, diagnostics=plan.diagnostics + added)


def attach_reroute_ledger_delta(
    plan: RoutePlan, frozen: RoutePlan, routed: RoutePlan
) -> RoutePlan:
    """Publish where the settled re-route's gap demand differs from the ledger's.

    Settlement allocates against one ledger and deliberately does not iterate, so
    a corridor the re-routed geometry is the first to demand is one no widening
    was sized for.  Naming that difference here is what separates "not chased"
    from "not seen": the published plan carries it, so a future change that starts
    moving demand across this boundary is visible rather than silent.  The plan
    the render draws from is the frozen one either way, which is why these are
    non-blocking records rather than a refusal.

    A corridor is compared by what settlement had to size for it, which is its
    description together with the width it asks for: a boundary whose corridor
    survives the re-route at a different ``minimum_width`` is one the
    translations were sized wrongly for, and a description alone cannot say so.
    The widths of the corridors sharing one description are held as a sorted
    tuple, so a re-route that drops one of a pair of alike corridors differs
    from one that keeps both.
    """

    def gap_demand(source: RoutePlan) -> dict[str, tuple[float, ...]]:
        widths: dict[str, list[float]] = {}
        for item in source.reservations:
            if isinstance(item.region, RowGapRegion | ColumnGapRegion):
                widths.setdefault(item.description, []).append(item.minimum_width)
        return {key: tuple(sorted(value)) for key, value in widths.items()}

    def stated(widths: tuple[float, ...]) -> str:
        return ", ".join(f"{width:.2f}px" for width in widths)

    before, after = gap_demand(frozen), gap_demand(routed)
    added = (
        tuple(
            RoutePlanDiagnostic(
                None,
                "reroute-ledger-demand-appeared",
                f"the settled re-route demands {description} at "
                f"{stated(after[description])}, which the ledger settlement was "
                "handed does not carry, so no widening was sized for it",
                blocking=False,
            )
            for description in sorted(after.keys() - before.keys())
        )
        + tuple(
            RoutePlanDiagnostic(
                None,
                "reroute-ledger-demand-vanished",
                f"the ledger settlement was handed carries {description} at "
                f"{stated(before[description])}, which the settled re-route does "
                "not demand",
                blocking=False,
            )
            for description in sorted(before.keys() - after.keys())
        )
        + tuple(
            RoutePlanDiagnostic(
                None,
                "reroute-ledger-demand-rewidened",
                f"the settled re-route demands {description} at "
                f"{stated(after[description])} against the "
                f"{stated(before[description])} the ledger settlement was handed "
                "carries, so the widening was sized for a different width",
                blocking=False,
            )
            for description in sorted(before.keys() & after.keys())
            if before[description] != after[description]
        )
    )
    if not added:
        return plan
    return replace(plan, diagnostics=plan.diagnostics + added)


def _measured_widths(
    graph: MetroGraph,
    reservations: tuple[RouteReservation, ...],
    coordinate_translations: tuple[ReservationCoordinateTranslation, ...],
) -> dict[RouteReservationId, float]:
    widths: dict[RouteReservationId, float] = {}
    for reservation in reservations:
        realised = realise_reservation(
            graph, reservation, coordinate_translations=coordinate_translations
        )
        if realised is not None:
            widths[reservation.id] = realised.available_width
    return widths


def _assert_the_column_phase_left_the_row_phase_standing(
    before: dict[RouteReservationId, float],
    after: dict[RouteReservationId, float],
) -> None:
    """No row corridor is narrower after the column phase than before it.

    Rows settle first and columns second, with no row recheck, which is sound
    because a column translation writes only x: the edges a row corridor is
    measured between are y values it never touches.  It can reach a row corridor
    only by changing which sections the corridor's own run overlaps, and for a
    corridor scoped to the topology span there is nothing to change -- grid
    indices do not move.  For a run-scoped corridor, sections at or beyond the
    translated column boundary move with the part of the run that reaches them,
    and sections before it move with neither, so a section outside the run
    cannot be drawn into it.

    The exception is a section spanning across the translated column boundary:
    it stays put while a run crossing that boundary lengthens, so such a section
    can end up inside a run that clears it before the translation.  That is a
    real narrowing, which is why the conclusion is checked here on every settled
    layout rather than taken from the argument alone.  A layout that hits it
    fails inside the transactional scope rather than being re-settled: a second
    pass would be settling against a constraint set the first pass moved.
    """
    for reservation_id, width in after.items():
        held = before.get(reservation_id)
        if held is not None and width < held - COORD_TOLERANCE:
            raise PhaseInvariantError(
                f"the column phase narrowed the row corridor {reservation_id} "
                f"from {held:.2f}px to {width:.2f}px: a section spanning across a "
                "translated column boundary was drawn into the corridor's run, "
                "so the two axes are not independent for this layout"
            )


def _verify_against_input_ledger(
    graph: MetroGraph,
    plan: RoutePlan,
    coordinate_translations: tuple[ReservationCoordinateTranslation, ...],
) -> tuple[SettlementShortfall, ...]:
    """Re-measure the demands settlement was handed, on the geometry it left.

    This is settlement's own postcondition, and it is the only measurement that
    can state it: the ledger a later re-route publishes is a different set of
    claims, so a deficit found there answers a different question.  Every
    reservation on both axes is measured here with the complete translation
    set, so a row demand whose blockers a later column translation changed is
    caught rather than assumed independent.
    """
    shortfalls: list[SettlementShortfall] = []
    for reservation in plan.reservations:
        if not isinstance(reservation.region, RowGapRegion | ColumnGapRegion):
            continue
        realised = realise_reservation(
            graph, reservation, coordinate_translations=coordinate_translations
        )
        if realised is None or realised.capacity_slack >= -COORD_TOLERANCE:
            continue
        shortfalls.append(
            SettlementShortfall(
                reservation.id,
                reservation.claimant_member_ids,
                realised.required_width,
                realised.available_width,
            )
        )
    return tuple(shortfalls)


def _box_extents(section: Section) -> tuple[tuple[float, float], tuple[float, float]]:
    """One section's box as its horizontal and vertical intervals, in that order."""
    return (
        (section.bbox_x, section.bbox_x + section.bbox_w),
        (section.bbox_y, section.bbox_y + section.bbox_h),
    )


def _axis_gaps(
    graph: MetroGraph, axis: SettlementAxisGeometry
) -> dict[tuple[str, str], float]:
    """The signed clearance between every pair of boxes that face each other.

    Keyed by the ordered pair, so the same pair is comparable before and after a
    translation.  Boxes that do not overlap across the axis never face each
    other, so the distance between them is not a separation this stage owes
    anything to.
    """
    along_index = 1 if axis.axis is SettlementAxis.ROW else 0
    across_index = 1 - along_index
    gaps: dict[tuple[str, str], float] = {}
    sections = sorted(graph.sections.items())
    for first_key, first in sections:
        for second_key, second in sections:
            if first_key >= second_key:
                continue
            first_extents = _box_extents(first)
            second_extents = _box_extents(second)
            first_lo, first_hi = first_extents[across_index]
            second_lo, second_hi = second_extents[across_index]
            if min(first_hi, second_hi) <= max(first_lo, second_lo):
                continue
            first_start, first_end = first_extents[along_index]
            second_start, second_end = second_extents[along_index]
            if first_end <= second_start:
                gaps[first_key, second_key] = second_start - first_end
            elif second_end <= first_start:
                gaps[second_key, first_key] = first_start - second_end
    return gaps


def _assert_no_separation_decreased(
    before: dict[tuple[str, str], float],
    after: dict[tuple[str, str], float],
    axis: SettlementAxisGeometry,
) -> None:
    """Every pair facing each other in both states is at least as far apart.

    The monotone claim this stage rests on.  A pair facing each other in only
    one of the two states has no separation to compare.
    """
    for pair, gap in after.items():
        held = before.get(pair)
        if held is not None and gap < held - COORD_TOLERANCE:
            first, second = pair
            raise PhaseInvariantError(
                f"envelope settlement narrowed the {axis.axis.value} separation "
                f"between sections {first!r} and {second!r} from {held:.2f}px to "
                f"{gap:.2f}px; no settlement step may shrink a satisfied gap"
            )


def _assert_spanning_sections_bound_nothing_settled(
    graph: MetroGraph,
    plan: RoutePlan,
    translations: Iterable[SettlementTranslation],
    coordinate_translations: tuple[ReservationCoordinateTranslation, ...],
) -> None:
    """A section a translation could not move must not bound what it settled.

    This is what makes holding a straddling section in place sound.  If such a
    section bounds one of the corridors the translation widened, the widening
    never reached that corridor and the translation's record of settling it is
    false.  Measured on the final geometry, after both axes, so a row
    translation invalidated by a later column translation -- which moves the
    horizontal intervals a row corridor's blockers are selected by -- is caught
    here rather than assumed away.
    """
    reservation_by_id = {item.id: item for item in plan.reservations}
    for translation in translations:
        if not translation.spanning_section_ids:
            continue
        held = frozenset(translation.spanning_section_ids)
        for reservation_id in translation.reservation_ids:
            reservation = reservation_by_id.get(reservation_id)
            if reservation is None:
                continue
            realised = realise_reservation(
                graph, reservation, coordinate_translations=coordinate_translations
            )
            if realised is None:
                continue
            blockers = {
                blocker_id.partition(":")[2]
                for blocker_id in (
                    realised.negative_blocker_ids + realised.positive_blocker_ids
                )
            }
            trespassing = sorted(held & blockers)
            if trespassing:
                raise PhaseInvariantError(
                    f"{translation.axis.value} boundary {translation.boundary} was "
                    f"widened by {translation.amount:.2f}px for the corridor "
                    f"{reservation.description!r}, but section(s) "
                    f"{', '.join(trespassing)} straddle that boundary and bound "
                    "the corridor, so the translation could not have widened it"
                )


def _coordinate_state(
    graph: MetroGraph,
) -> tuple[tuple[str, float, float], ...]:
    """Every coordinate settlement is allowed to write, as plain values."""
    return (
        *(
            (f"section:{key}", section.bbox_x, section.bbox_y)
            for key, section in graph.sections.items()
        ),
        *(
            (f"station:{key}", station.x, station.y)
            for key, station in graph.stations.items()
        ),
        *((f"port:{key}", port.x, port.y) for key, port in graph.ports.items()),
    )


def _restore_coordinate_state(
    graph: MetroGraph, state: tuple[tuple[str, float, float], ...]
) -> None:
    for key, x, y in state:
        kind, _, item_id = key.partition(":")
        if kind == "section":
            section = graph.sections[item_id]
            section.bbox_x, section.bbox_y = x, y
        elif kind == "station":
            station = graph.stations[item_id]
            station.x, station.y = x, y
        else:
            port = graph.ports[item_id]
            port.x, port.y = x, y


def _assert_clearance_demands_are_met(
    graph: MetroGraph, clearance: ClearanceMeasurement | None
) -> None:
    """No boundary still owes clearance once the sweep has visited it.

    The sweep pays each boundary's demand with ``ceil(deficit / QUANTUM) *
    QUANTUM``, and the ownership lemma applies to a clearance demand exactly as
    it does to a corridor: every box the demand is measured *from* ends above the
    boundary and every box it is measured *to* starts at or beyond it, which are
    the two halves of ``translation_ownership``.  So the translation raises the
    boundary by its full amount and the deficit closes.  Checked rather than
    argued, because the two predicates staying in step is a property a later edit
    could break.
    """
    measured = () if clearance is None else clearance(graph)
    outstanding = tuple(_clearance_demands_at(measured, ROW_AXIS).values()) + tuple(
        _clearance_demands_at(measured, COLUMN_AXIS).values()
    )
    if outstanding:
        stated = "; ".join(
            f"{item.description} is still {item.deficit:.2f}px short"
            for item in sorted(
                outstanding, key=lambda item: (item.axis.value, item.boundary)
            )
        )
        raise PhaseInvariantError(
            f"envelope settlement left a boundary owing clearance: {stated}"
        )


def settle_route_envelopes(
    graph: MetroGraph,
    plan: RoutePlan,
    clearance: ClearanceMeasurement | None = None,
) -> EnvelopeSettlement:
    """Widen row and column boundaries until every demand at one fits.

    Mutates *graph* in place, translating whole rows and whole columns only.
    Returns what moved and any deficit outside this stage's ownership.

    Two kinds of demand are settled, and a boundary carrying both is widened
    once by the larger: the corridors *plan* reserved across it, and the
    clearance *clearance* measures that its facing boxes owe each other.  Taking
    both here is what makes this the single owner of the translation, so a
    boundary that a render-time box grow left short of its declared gap needs no
    second widening from anywhere else.

    The write is transactional.  A pass touches many sections in sequence, so a
    failure part-way through would otherwise leave the graph in a state that is
    neither the one measured nor the one intended; the pre-settlement
    coordinates are restored before the error propagates.  The reservation
    ledger needs no such care: settlement only reads it.
    """
    restore_point = _coordinate_state(graph)
    row_gaps_before = _axis_gaps(graph, ROW_AXIS)
    column_gaps_before = _axis_gaps(graph, COLUMN_AXIS)
    row_reservations = _reservations_on(plan, RowGapRegion)
    try:
        row_translations, row_coordinate = _settle_axis(
            graph, plan, row_reservations, ROW_AXIS, clearance=clearance
        )
        row_widths = _measured_widths(graph, row_reservations, tuple(row_coordinate))
        column_translations, column_coordinate = _settle_axis(
            graph,
            plan,
            _reservations_on(plan, ColumnGapRegion),
            COLUMN_AXIS,
            tuple(row_coordinate),
            clearance=clearance,
        )
        coordinate_translations = tuple(row_coordinate + column_coordinate)
        translations = tuple(row_translations + column_translations)
        _assert_the_column_phase_left_the_row_phase_standing(
            row_widths,
            _measured_widths(graph, row_reservations, coordinate_translations),
        )
        _assert_no_separation_decreased(
            row_gaps_before, _axis_gaps(graph, ROW_AXIS), ROW_AXIS
        )
        _assert_no_separation_decreased(
            column_gaps_before, _axis_gaps(graph, COLUMN_AXIS), COLUMN_AXIS
        )
        _assert_spanning_sections_bound_nothing_settled(
            graph, plan, translations, coordinate_translations
        )
        _assert_clearance_demands_are_met(graph, clearance)
        shortfalls = _verify_against_input_ledger(graph, plan, coordinate_translations)
    except Exception:
        _restore_coordinate_state(graph, restore_point)
        raise
    return EnvelopeSettlement(
        translations,
        shortfalls,
        coordinate_translations,
    )
