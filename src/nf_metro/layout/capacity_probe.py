"""Counterfactual boundary-capacity probe for compatibility route systems.

``attribute_compatibility_systems`` measures whether a *translation* changes the
distance between the two coordinates a convergence planner recorded as its
conflict.  That is a statement about two points already drawn.  #1657's exit
criteria ask a different question: whether a boundary with enough room would let
the planner allocate every member of the system, which no measurement of drawn
geometry can answer because the planner never ran against that geometry.

This module answers it directly.  For one compatibility system it copies the
settled graph, translates whole rows and columns to widen the boundaries the
system is measured at, re-runs convergence planning on the copy, and reads the
disposition that comes back.  A system the planner plans once it has room was
held by an envelope allocation; a system that stays on the compatibility path
across every capacity granted was held by something no allocation supplies.

Four properties make the answer usable as evidence.

*Read-only.*  Every grant runs on ``copy.deepcopy`` of the graph and the plan is
only read, so no probe geometry, plan, reservation, or offset can reach the map
that gets drawn.  Nothing here is called from the render path.

*Faithful.*  A grant only says something about capacity if the geometry it hands
the planner is geometry settlement could hand it.  A translation moves whole
sections and the render path derives the coordinates that follow from where they
sit before it routes again, so the grant derives them too
(:func:`translate_boundaries`); a grant that skipped the step would report a
planner decision taken on a map with no drawn counterpart.

*Controlled.*  Re-planning is only meaningful if it reproduces the disposition
the map already has.  Each system is first re-planned on an untouched copy, and
a system whose control does not come back on the compatibility path with the
conflict it published is reported as ``CONTROL_DIVERGED`` rather than measured:
its grants would be differences against an unknown baseline.

*Falsifiable.*  A probe that can only ever report "no allocation reaches this"
would be indistinguishable from a probe that does nothing, so the result has to
be reachable in both directions.  It is: the corpus contains a system the probe
reports planned, and ``tests/test_capacity_probe.py`` also starves a system that
the planner plans on its own geometry and watches the probe hand its capacity
back.

The planner's answer need not be monotone in capacity: moving whole rows and
columns changes which runs overlap as well as how much room they have, so one
planned grant can be a coincidence of alignment rather than a threshold.  A
single grant is therefore not evidence.  The verdict is taken from a *tail*: a
system counts as reached only when it is planned at some granted capacity and at
every larger one, which no isolated coincidence satisfies.

A grant has three outcomes, not two.  The re-plan can own the system, leave it
where the control left it, or come back describing neither -- the system gone, or
split across both dispositions.  That third outcome is recorded as its own
``GrantOutcome`` and taken out of the verdict, because "the planner wants more
room here" and "the planner is not talking about this system" are different
findings and only the first bears on allocation.  A system every grant diverges
on has no interpretable counterfactual at all, which is ``GRANTS_DIVERGED``
rather than the negative result.

The probe reaches into settlement for the translation itself (``ROW_AXIS``,
``COLUMN_AXIS``, ``translation_ownership``, ``apply_translation``) rather than
reimplementing it, so what a grant hands the planner is the geometry settlement
would hand it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum

from nf_metro.layout.constants import CURVE_RADIUS
from nf_metro.layout.envelope_settlement import (
    COLUMN_AXIS,
    ROW_AXIS,
    SettlementAxisGeometry,
    apply_translation,
    quantised_allocation,
    translation_ownership,
)
from nf_metro.layout.route_plan import (
    ConvergenceConflictKind,
    ConvergenceDisposition,
    ConvergencePlan,
    RoutePlan,
    RouteSystemId,
)
from nf_metro.layout.route_reservations import ColumnGapRegion, RowGapRegion
from nf_metro.parser.model import MetroGraph

CAPACITY_MULTIPLES: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)
"""Multiples of a system's own derived capacity unit that each get granted.

The unit is what one competing pair of runs costs, so the top of the ladder is
sixteen of them stacked into a boundary that holds a handful.  A system still on
the compatibility path there is not short of room.
"""


class CapacityScope(Enum):
    """Which boundaries one grant widens."""

    CLAIMED_BOUNDARIES = "claimed-boundaries"
    """Only the row and column boundaries the system's own gap reservations are
    filed against, which is where settlement would ever allocate for it."""

    EVERY_BOUNDARY = "every-boundary"
    """Every row and column boundary in the grid, so a conflict whose relief
    lies outside the system's own claims is still offered the room."""


class CapacityVerdict(Enum):
    """What granting boundary capacity did to one compatibility system."""

    ALLOCATION_REACHES = "allocation-reaches"
    """The planner returns a planned convergence at some granted capacity and at
    every larger one.  The limitation is an envelope allocation."""

    ALLOCATION_UNSTABLE = "allocation-unstable"
    """The planner returns a planned convergence at some granted capacity but
    not at the largest, so capacity changes the answer without a threshold above
    which it holds."""

    BEYOND_ALLOCATION = "beyond-allocation"
    """No granted capacity makes the planner plan the system.  This is the
    evidence #1657's exit criteria ask for."""

    CONTROL_DIVERGED = "control-diverged"
    """Re-planning the untouched copy did not reproduce the disposition the map
    publishes, so nothing measured against it would mean anything."""

    GRANTS_DIVERGED = "grants-diverged"
    """Every granted capacity took the system somewhere neither disposition
    describes, so the probe holds no counterfactual it can read an answer off."""


class GrantOutcome(Enum):
    """What a re-plan decided about the whole system.

    Read of the control re-plan as well as of each granted capacity, so the
    baseline and the counterfactuals are described in one vocabulary.
    """

    PLANNED = "planned"
    """The re-plan owns every member of the system."""

    COMPATIBLE = "compatible"
    """The re-plan leaves every member of the system on the compatibility path,
    which is the same disposition the control reproduced."""

    DIVERGED = "diverged"
    """The re-plan lost the system or split it across both dispositions, so this
    capacity says nothing about whether room is what the system lacks.  Reading
    it as compatible would let a grant that stopped describing the system count
    as one that described it and found the room wanting."""


@dataclass(frozen=True, slots=True)
class CapacityGrant:
    """One counterfactual: this much room at these boundaries, and the answer."""

    scope: CapacityScope
    capacity: float
    outcome: GrantOutcome

    @property
    def planned(self) -> bool:
        return self.outcome is GrantOutcome.PLANNED

    @property
    def diverged(self) -> bool:
        return self.outcome is GrantOutcome.DIVERGED


@dataclass(frozen=True, slots=True)
class CapacityProbe:
    """What boundary capacity does to one system on the compatibility path.

    The grants are the record and the verdict is read back off them, so the
    published conclusion cannot drift from the counterfactuals behind it.
    """

    system_id: RouteSystemId
    unit: float
    """The largest single distance the system's own limit is measured against:
    the widest corridor it reserves, the offset step between lanes, or the turn
    radius two runs need between them."""
    capacity: float
    """``unit`` plus the separation the conflict recorded, quantised to a
    translation settlement could express.  Each grant is a multiple of this."""
    grants: tuple[CapacityGrant, ...]
    control_reproduced: bool
    control_conflict: ConvergenceConflictKind | None

    @property
    def measured_grants(self) -> tuple[CapacityGrant, ...]:
        """The grants whose re-plan describes the system.

        The verdict is read off these alone.  A diverged grant is a
        counterfactual the probe could not interpret, which is a different thing
        from a counterfactual that came back short of room, and only the second
        is evidence about allocation.
        """
        return tuple(item for item in self.grants if not item.diverged)

    @property
    def diverged_grants(self) -> tuple[CapacityGrant, ...]:
        """The grants whose re-plan neither owned nor kept the whole system."""
        return tuple(item for item in self.grants if item.diverged)

    @property
    def sufficient(self) -> tuple[CapacityScope, float] | None:
        """The cheapest capacity planned at, and at every larger one granted."""
        return _least_sufficient(self.measured_grants)

    @property
    def verdict(self) -> CapacityVerdict:
        if not self.control_reproduced:
            return CapacityVerdict.CONTROL_DIVERGED
        if self.sufficient is not None:
            return CapacityVerdict.ALLOCATION_REACHES
        if any(item.planned for item in self.measured_grants):
            return CapacityVerdict.ALLOCATION_UNSTABLE
        if not self.measured_grants:
            return CapacityVerdict.GRANTS_DIVERGED
        return CapacityVerdict.BEYOND_ALLOCATION

    @property
    def quoted(self) -> tuple[CapacityScope, float] | None:
        """The scope and capacity the published verdict rests on.

        The tail's threshold where there is a tail, and otherwise the cheapest
        grant that was planned at all.  ``None`` where nothing was planned, which
        is the one verdict with no capacity to quote.
        """
        if self.sufficient is not None:
            return self.sufficient
        planned = [item for item in self.measured_grants if item.planned]
        if not planned:
            return None
        cheapest = min(planned, key=lambda item: (item.capacity, item.scope.value))
        return cheapest.scope, cheapest.capacity

    @property
    def message(self) -> str:
        if self.verdict is CapacityVerdict.CONTROL_DIVERGED:
            return (
                f"route system {self.system_id} could not be probed: re-planning "
                f"its settled geometry unchanged did not reproduce the "
                f"compatibility disposition the map publishes"
            )
        held = (
            "nothing it recorded"
            if self.control_conflict is None
            else self.control_conflict.reason
        )
        if self.verdict is CapacityVerdict.GRANTS_DIVERGED:
            return (
                f"route system {self.system_id} could not be probed: all "
                f"{len(self.grants)} boundary allocations re-planned to something "
                f"that neither owns the whole system nor leaves the whole of it on "
                f"compatibility, so none of them says whether room is what it lacks"
            )
        aside = (
            ""
            if not self.diverged_grants
            else (
                f", setting aside {len(self.diverged_grants)} that re-planned to "
                f"neither disposition"
            )
        )
        granted = max((item.capacity for item in self.measured_grants), default=0.0)
        if self.verdict is CapacityVerdict.BEYOND_ALLOCATION:
            return (
                f"route system {self.system_id} stays on compatibility under "
                f"every capacity this probe granted, up to {granted:.2f}px at "
                f"{len(self.measured_grants)} boundary allocations{aside}; what "
                f"holds it is {held}, and no envelope allocation supplies it"
            )
        quoted = self.quoted
        assert quoted is not None
        scope, capacity = quoted
        if self.verdict is CapacityVerdict.ALLOCATION_REACHES:
            return (
                f"route system {self.system_id} is planned once its "
                f"{scope.value} carry {capacity:.2f}px "
                f"more, and at every larger capacity granted, so what holds it "
                f"({held}) is an envelope allocation and not a decision to "
                f"attribute elsewhere"
            )
        return (
            f"route system {self.system_id} is planned at "
            f"{capacity:.2f}px more across its "
            f"{scope.value} but not at the largest capacity "
            f"granted, so capacity changes what the planner decides about it "
            f"({held}) without a threshold above which the decision holds"
        )


def probe_settlement_capacity(
    graph: MetroGraph, plan: RoutePlan
) -> tuple[CapacityProbe, ...]:
    """Ask what boundary capacity would do to every compatibility system in *plan*.

    *graph* is the settled geometry the map draws and is never written to: each
    counterfactual runs on its own deep copy.
    """
    compatibility = _ordered_compatibility_systems(plan)
    if not compatibility:
        return ()
    control, offset_step = _replan(copy.deepcopy(graph))
    baseline = _Baseline(
        graph,
        plan,
        control,
        offset_step,
        tuple(sorted({item.grid_row for item in graph.sections.values()})),
        tuple(sorted({item.grid_col for item in graph.sections.values()})),
    )
    return tuple(
        _probe_system(baseline, system_id, conflict)
        for system_id, conflict in compatibility
    )


@dataclass(frozen=True, slots=True)
class _Baseline:
    """What every system's counterfactuals are measured against.

    The control re-plan and the grid's boundaries are properties of the map
    rather than of one system, so they are established once and read by each.
    """

    graph: MetroGraph
    plan: RoutePlan
    control: dict[RouteSystemId, tuple[ConvergencePlan, ...]]
    offset_step: float
    every_row: tuple[int, ...]
    every_column: tuple[int, ...]


def _ordered_compatibility_systems(
    plan: RoutePlan,
) -> tuple[tuple[RouteSystemId, ConvergenceConflictKind | None], ...]:
    """Each compatibility system once, in the plan's own system order."""
    found: dict[RouteSystemId, ConvergenceConflictKind | None] = {}
    for convergence in plan.convergence_plans:
        if convergence.legacy_reason is None:
            continue
        if convergence.system_id in found:
            continue
        found[convergence.system_id] = (
            None if convergence.conflict is None else convergence.conflict.kind
        )
    return tuple(sorted(found.items(), key=lambda item: item[0]))


def _probe_system(
    baseline: _Baseline,
    system_id: RouteSystemId,
    conflict: ConvergenceConflictKind | None,
) -> CapacityProbe:
    if _replan_outcome(baseline.control, system_id) is not GrantOutcome.COMPATIBLE:
        return CapacityProbe(
            system_id=system_id,
            unit=0.0,
            capacity=0.0,
            grants=(),
            control_reproduced=False,
            control_conflict=None,
        )
    rows, columns, widths = claimed_boundaries(baseline.plan, system_id)
    unit = max(CURVE_RADIUS, baseline.offset_step, max(widths, default=0.0))
    separation = max(
        (
            item.conflict.separation
            for item in baseline.plan.convergence_plans
            if item.system_id == system_id and item.conflict is not None
        ),
        default=0.0,
    )
    capacity = quantised_allocation(separation + unit)
    grants = tuple(
        CapacityGrant(
            scope,
            amount,
            _grant_outcome(baseline.graph, system_id, at_rows, at_columns, amount),
        )
        for scope, at_rows, at_columns in (
            (CapacityScope.CLAIMED_BOUNDARIES, rows, columns),
            (CapacityScope.EVERY_BOUNDARY, baseline.every_row, baseline.every_column),
        )
        for amount in (round(capacity * item, 6) for item in CAPACITY_MULTIPLES)
    )
    return CapacityProbe(system_id, unit, capacity, grants, True, conflict)


def claimed_boundaries(
    plan: RoutePlan, system_id: RouteSystemId
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[float, ...]]:
    """The boundaries one system's corridors are filed against, and their widths.

    A conflict is always a distance two runs need between them, and a corridor
    the system reserves states a width the ledger sizes a boundary by, so the
    widths are what says how much room one competing pair costs here.
    """
    rows: set[int] = set()
    columns: set[int] = set()
    widths: list[float] = []
    for reservation in plan.reservations:
        if reservation.system_id != system_id:
            continue
        region = reservation.region
        if isinstance(region, RowGapRegion):
            rows.add(region.lower_row)
        elif isinstance(region, ColumnGapRegion):
            columns.add(region.right_column)
        else:
            continue
        widths.append(reservation.minimum_width)
    return tuple(sorted(rows)), tuple(sorted(columns)), tuple(sorted(widths))


def _least_sufficient(
    grants: tuple[CapacityGrant, ...],
) -> tuple[CapacityScope, float] | None:
    """The smallest capacity planned at, and at every larger capacity granted.

    Read per scope, because a grant's meaning depends on which boundaries it
    widened; the answer is the cheapest such capacity across the scopes.
    """
    found: list[tuple[float, CapacityScope]] = []
    for scope in CapacityScope:
        ladder = sorted(
            (item for item in grants if item.scope is scope),
            key=lambda item: item.capacity,
            reverse=True,
        )
        tail: float | None = None
        for item in ladder:
            if not item.planned:
                break
            tail = item.capacity
        if tail is not None:
            found.append((tail, scope))
    if not found:
        return None
    capacity, scope = min(found, key=lambda item: (item[0], item[1].value))
    return scope, capacity


def _grant_outcome(
    graph: MetroGraph,
    system_id: RouteSystemId,
    rows: tuple[int, ...],
    columns: tuple[int, ...],
    amount: float,
) -> GrantOutcome:
    """What the planner decides about *system_id* once those boundaries carry *amount*.

    An arbitrary re-plan failure propagates.  A final convergence feasibility
    rejection is a valid planner finding that owns no complete system geometry,
    so the grant is ``DIVERGED`` rather than compatible.  A re-plan that returns
    without describing the whole system is read the same way.
    """
    probe_graph = copy.deepcopy(graph)
    translate_boundaries(probe_graph, rows, columns, amount)
    from nf_metro.layout.routing.convergences import (
        FinalConvergenceFeasibilityError,
    )

    try:
        replanned, _offset_step = _replan(probe_graph)
    except FinalConvergenceFeasibilityError:
        return GrantOutcome.DIVERGED
    return _replan_outcome(replanned, system_id)


def translate_boundaries(
    graph: MetroGraph,
    rows: tuple[int, ...],
    columns: tuple[int, ...],
    amount: float,
) -> None:
    """Move *rows* and *columns* by *amount* and derive what that implies.

    The settled render derives a junction from the ports it joins before it
    routes, so a translation that skips the step offers its reader a map the
    pipeline cannot produce: the arms of one fan stop meeting at a shared
    coordinate because their junction was left behind, not because the boundary
    got wider.  Naming the pair together is what stops a caller taking one
    without the other.
    """
    from nf_metro.layout.phases.junctions import reanchor_junctions

    for axis, boundaries in ((ROW_AXIS, rows), (COLUMN_AXIS, columns)):
        _widen(graph, axis, boundaries, amount)
    reanchor_junctions(graph)


def _widen(
    graph: MetroGraph,
    axis: SettlementAxisGeometry,
    boundaries: tuple[int, ...],
    amount: float,
) -> None:
    """Translate everything at or beyond each boundary, exactly as settlement does.

    Applied in ascending boundary order so a section beyond several of them
    accumulates every one, which is what makes each named boundary wider by
    ``amount`` rather than the last one alone.
    """
    for boundary in sorted(boundaries):
        apply_translation(
            graph, axis, translation_ownership(graph, axis, boundary), amount
        )


def _replan(
    graph: MetroGraph,
) -> tuple[dict[RouteSystemId, tuple[ConvergencePlan, ...]], float]:
    """Plan *graph* against its canonical member geometry and group decisions.

    A capacity probe asks whether the route-system planner can own a changed
    boundary.  Preliminary system classification first removes compatibility
    systems from planning context.  Convergence claims then guide canonical
    member allocation before final convergence feasibility and atomic system
    classification decide ownership.

    Imported here because the probe is a diagnostic consumer of routing, not a
    dependency of it.
    """
    from nf_metro.layout.constants import DIAGONAL_RUN
    from nf_metro.layout.routing import compute_station_offsets
    from nf_metro.layout.routing.context import _build_routing_context
    from nf_metro.layout.routing.planning import prepare_route_system_planning

    context = _build_routing_context(
        graph, DIAGONAL_RUN, CURVE_RADIUS, compute_station_offsets(graph)
    )
    planning = prepare_route_system_planning(
        graph,
        context,
        include_convergence_resources=False,
    )
    by_system: dict[RouteSystemId, tuple[ConvergencePlan, ...]] = {}
    for item in planning.convergences.plans:
        by_system[item.system_id] = (*by_system.get(item.system_id, ()), item)
    return by_system, context.offset_step


def _replan_outcome(
    by_system: dict[RouteSystemId, tuple[ConvergencePlan, ...]],
    system_id: RouteSystemId,
) -> GrantOutcome:
    """What a re-plan made of the whole system.

    A system is planned or compatible as a whole, so a mixed result is a
    disagreement with that rule rather than a capacity answer, and is reported
    as an absent baseline the same way a vanished system is.
    """
    plans = by_system.get(system_id)
    if not plans:
        return GrantOutcome.DIVERGED
    dispositions = {item.disposition for item in plans}
    if dispositions == {ConvergenceDisposition.PLANNED}:
        return GrantOutcome.PLANNED
    if dispositions == {ConvergenceDisposition.LEGACY}:
        return GrantOutcome.COMPATIBLE
    return GrantOutcome.DIVERGED
