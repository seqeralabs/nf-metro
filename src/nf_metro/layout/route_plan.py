"""Immutable semantic route decisions and observations at the routing boundary.

The records in this module describe ownership, pre-routing decisions, and final
emission coverage. :class:`RoutePlanObserver` is a transient companion to the
production dispatcher: it copies scalar facts from the settled graph, carries
the complete exit-turn decisions consumed by routing, records the family
selected for each resolved inter-section leg, and binds the final route set
without retaining graph objects.
"""

from __future__ import annotations

import dataclasses
import json
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, NewType, TypeAlias, TypeVar

from nf_metro.layout.constants import COORD_TOLERANCE
from nf_metro.layout.fan_geometry import fan_lane_offsets
from nf_metro.layout.geometry import AxisFrame
from nf_metro.layout.routing.common import (
    Direction,
    GapSlot,
    OffsetRegime,
    TrunkSlot,
    right_normal_axis_sign,
)
from nf_metro.layout.routing.families import RouteFamilyId
from nf_metro.options import LineOrder
from nf_metro.parser.commitments import FlowDirection
from nf_metro.parser.model import MetroGraph, PortSide, Station, is_bypass_v
from nf_metro.parser.provenance import (
    ConnectorEndpointRole,
    DecisionOrigin,
    DecisionReason,
    EffectiveDecision,
    FoldThresholdSource,
    GridCell,
    LineOrderSource,
)
from nf_metro.parser.route_topology import (
    BundleId,
    ConnectorId,
    ConvergenceId,
    DivergenceId,
    EndpointGroupId,
    ResolvedEdge,
    RouteConnector,
    RouteTopology,
    RouteTopologyQuery,
    build_route_topology_query,
    semantic_route_id,
)

if TYPE_CHECKING:
    from nf_metro.layout.route_reservations import (
        RealisedRouteReservation,
        RouteReservation,
        RouteReservationDiagnostic,
        RouteReservationId,
    )
    from nf_metro.layout.routing.common import RoutedPath
    from nf_metro.layout.routing.context import _EdgeKey, _RoutingCtx
    from nf_metro.layout.routing.system_emission import RouteSystemEmissionExecution


RouteSystemId = NewType("RouteSystemId", str)
EmissionMemberId = NewType("EmissionMemberId", str)
EmittedPathId = NewType("EmittedPathId", str)
RouteBranchId = NewType("RouteBranchId", str)
RouteFeederId = NewType("RouteFeederId", str)
SharedReferenceId = NewType("SharedReferenceId", str)
DemandId = NewType("DemandId", str)
ExitTurnPlanId = NewType("ExitTurnPlanId", str)
ExitTurnAxisId = NewType("ExitTurnAxisId", str)
FanPlanId = NewType("FanPlanId", str)
FanBranchPlanId = NewType("FanBranchPlanId", str)
ConvergencePlanId = NewType("ConvergencePlanId", str)
RouteMemberGeometryPlanId = NewType("RouteMemberGeometryPlanId", str)
_T = TypeVar("_T")


def convergence_resource_ids(
    plan_id: ConvergencePlanId,
) -> tuple[tuple[SharedReferenceId, ...], tuple[DemandId, ...]]:
    return (
        (
            SharedReferenceId(semantic_route_id("convergence-trunk", plan_id)),
            SharedReferenceId(semantic_route_id("convergence-landings", plan_id)),
        ),
        (
            DemandId(semantic_route_id("convergence-band", plan_id)),
            DemandId(semantic_route_id("convergence-runway", plan_id)),
        ),
    )


class CoordinateRegime(str, Enum):
    """Coordinate system used by a coordinate-bearing record."""

    SETTLED_GRID = "settled-grid"
    LAYOUT_CANVAS = "layout-canvas"
    RELATIVE_FRAME = "relative-frame"


@dataclass(frozen=True, slots=True)
class RouteMemberGapChannel:
    """One immutable inter-column leg owned by a member geometry plan."""

    segment_rank: int
    start: tuple[float, float]
    end: tuple[float, float]
    gap_lo_col: int
    row: int | None
    direction: Direction

    def __post_init__(self) -> None:
        if self.segment_rank < 0:
            raise ValueError("member gap channel rank must be non-negative")
        if (
            not all(
                math.isfinite(value)
                for point in (self.start, self.end)
                for value in point
            )
            or abs(self.start[0] - self.end[0]) > COORD_TOLERANCE
            or abs(self.start[1] - self.end[1]) <= COORD_TOLERANCE
        ):
            raise ValueError("member gap channel must be a finite vertical segment")
        expected = Direction.D if self.end[1] > self.start[1] else Direction.U
        if self.direction is not expected:
            raise ValueError("member gap channel direction disagrees with its segment")


@dataclass(frozen=True, slots=True)
class RouteMemberGeometryPlan:
    """Pre-normalization template with immutable declared channel ownership.

    ``points`` and ``curve_radii`` seed production emission. Downstream global
    normalization may adjust geometry outside :attr:`owned_segment_ranks`.
    ``gap_channels`` are the exact geometry this plan owns through those passes.
    """

    id: RouteMemberGeometryPlanId
    system_id: RouteSystemId
    member_id: EmissionMemberId
    edge: ResolvedEdge
    connector_ids: tuple[ConnectorId, ...]
    family_id: RouteFamilyId
    points: tuple[tuple[float, float], ...]
    curve_radii: tuple[float, ...] | None
    offset_regime: OffsetRegime
    normalize_exempt: bool
    gap_slots: tuple[GapSlot, ...]
    trunk_slot: TrunkSlot | None
    gap_channels: tuple[RouteMemberGapChannel, ...]
    exit_turn_plan_id: str | None = None
    exit_turn_member_id: str | None = None
    exit_turn_family_id: str | None = None
    exit_turn_axis_id: str | None = None
    exit_turn_segment_rank: int | None = None
    exit_lane_transition_plan_id: str | None = None
    fan_plan_id: str | None = None
    fan_route_emitter: str | None = None
    consumed_reservation_ids: tuple[str, ...] = ()
    coordinate_regime: CoordinateRegime = CoordinateRegime.LAYOUT_CANVAS

    def __post_init__(self) -> None:
        if not self.connector_ids or len(set(self.connector_ids)) != len(
            self.connector_ids
        ):
            raise ValueError("member geometry plan connector ownership is incomplete")
        if len(self.points) < 2 or not all(
            math.isfinite(value) for point in self.points for value in point
        ):
            raise ValueError("member geometry plan requires finite path geometry")
        claims = tuple(
            (
                channel.segment_rank,
                channel.gap_lo_col,
                channel.row,
                channel.direction,
            )
            for channel in self.gap_channels
        )
        if len(set(claims)) != len(claims):
            raise ValueError("member geometry plan repeats a symbolic gap claim")
        if any(
            channel.segment_rank >= len(self.points) - 1
            for channel in self.gap_channels
        ):
            raise ValueError("member geometry plan channel exceeds its path segments")
        if any(
            any(
                abs(actual - expected) > COORD_TOLERANCE
                for actual, expected in zip(
                    (*channel.start, *channel.end),
                    (
                        *self.points[channel.segment_rank],
                        *self.points[channel.segment_rank + 1],
                    ),
                    strict=True,
                )
            )
            for channel in self.gap_channels
        ):
            raise ValueError("member geometry plan channel disagrees with its segment")

    @property
    def owned_segment_ranks(self) -> tuple[int, ...]:
        """Physical segments whose coordinates remain immutable after emission."""
        return tuple(dict.fromkeys(item.segment_rank for item in self.gap_channels))


class EmissionRole(str, Enum):
    """Semantic role played by a physical resolved leg."""

    CONTINUATION = "continuation"
    PEEL_OFF = "peel-off"
    BYPASS = "bypass"
    TERMINAL = "terminal"


class ExitTurnDisposition(str, Enum):
    """Whether one complete exit group uses planned or legacy geometry."""

    PLANNED = "planned"
    LEGACY = "legacy"


class FanPlanDisposition(str, Enum):
    """Whether one complete structural fan owns its geometry."""

    PLANNED = "planned"
    LEGACY = "legacy"


class ConvergenceDisposition(str, Enum):
    """Whether one complete route system uses planned convergence geometry."""

    PLANNED = "planned"
    LEGACY = "legacy"


class RouteSystemDisposition(str, Enum):
    """Whether emission consumes every available plan for one route system."""

    PLANNED = "planned"
    COMPATIBILITY = "compatibility"


ROUTE_SYSTEM_COMPATIBILITY_REASONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "exit-turn-plan": frozenset(
            {
                "ambiguous-continuation",
                "ambiguous-source-lane-boundary",
                "continuation-transition-has-no-runway",
                "family-changed-after-lane-compaction",
                "fixed-anchor-owned-by-another-plan",
                "fixed-axis-conflict",
                "insufficient-fixed-runway",
                "insufficient-structural-runway",
                "invalid-source-turn-requirement",
                "lane-transition-order-inversion",
                "linear-entry-frame-ownership-conflict",
                "missing-or-ambiguous-source-order",
                "missing-outbound-member",
                "missing-production-family",
                "missing-source-turn",
                "multiple-destinations",
                "opposed-source-run",
                "overlapping-planned-turn-axes",
                "planned-axis-overlaps-compatibility-channel",
                "shared-source-ownership-conflict",
                "shared-station-lane-collision",
                "single-member-group",
                "source-lane-transition-has-no-runway",
                "unresolved-perpendicular-entry-seam",
                "unsupported-family:bottom-exit-junction",
                "unsupported-family:bypass-family",
                "unsupported-family:left-entry-wrap-family",
                "unsupported-family:merge-branch",
                "unsupported-family:merge-trunk",
                "unsupported-family:near-vertical-same-col-junction",
                "unsupported-family:perp-exit",
                "unsupported-family:perp-exit-far-side-entry-wrap",
                "unsupported-family:right-entry-plough-bypass",
                "unsupported-family:right-entry-wrap",
                "unsupported-family:tb-perp-exit-over",
                "unsupported-subshape:degenerate-horizontal-straight",
                "unsupported-subshape:left-exit-right-entry-step",
                "unsupported-subshape:merge-entry-straight",
                "unsupported-subshape:nonvertical-tb-exit",
                "unsupported-subshape:opposed-horizontal-straight",
                "unsupported-subshape:unaligned-perpendicular-entry",
                "unsupported-subshape:vertical-source-horizontal-straight",
            }
        ),
        "fan-plan": frozenset(
            {
                "ambiguous-branch-to-join",
                "ambiguous-resolved-branch-tail",
                "ambiguous-resolved-fork",
                "ambiguous-resolved-join",
                "empty-resolved-member-path",
                "fan-route-system-has-no-emission-member",
                "local-layout-has-foreign-owner",
                "missing-centreline-anchor",
                "missing-resolved-extra-output-path",
                "missing-resolved-member-path",
                "off-track-layout-owns-fan-geometry",
                "offset-carrier-has-unowned-line",
                "overlapping-branch-lane-ownership",
                "overlapping-fan-ownership",
                "rail-layout-owns-fan-geometry",
                "same-line-open-fan-layout-owns-geometry",
                "unsupported-branch-line-transition",
                "unsupported-fan-direction",
            }
        ),
        "convergence-plan": frozenset(
            {
                "convergence alignment conflicts with an upstream exit turn",
                "convergence landing conflicts with an upstream exit turn",
                "convergence landing has no approach",
                "convergence template declined its member",
                "covered continuation is absent from its carrier",
                "direct convergence has no emitted terminal approach",
                "feeder template declined its member",
                "planned convergence approaches and trunks have no settlement room",
                "planned convergence member has no routing family",
                "planned trunk has no drawable segment",
                "primary trunk template declined its member",
                "primary trunk template emitted no shared run",
                "unsupported convergence shape",
            }
        ),
        "member-geometry-plan": frozenset(
            {
                "canonical-template-declined-member",
                "missing-emission-edge",
                "missing-production-family",
            }
        ),
    }
)
ROUTE_SYSTEM_COMPATIBILITY_JUSTIFICATION = (
    "The owning planner has no complete geometry for this named family; "
    "the established compatibility templates remain its explicit emitter."
)


def route_system_compatibility_follow_up(owner: str, reason: str) -> str:
    return f"Teach {owner} to own {reason!r}, then remove this registry entry."


class ConvergenceTrunkReason(str, Enum):
    """Structural evidence selecting a convergence's primary trunk member."""

    LONGEST_BYPASS = "longest-bypass"
    OUTGOING_CONTINUATION = "outgoing-continuation"
    SHARED_TERMINAL_APPROACH = "shared-terminal-approach"


class ConflictRelief(str, Enum):
    """What would resolve a convergence conflict, in terms of distance."""

    SHARED_CHANNEL = "shared-channel"
    """The two runs need to become one channel, side, or owner.  A larger
    distance between them is the wrong direction."""

    CLEARANCE = "clearance"
    """The two runs need to stand further apart than the planner could put
    them."""


class ConvergenceConflictKind(Enum):
    """The class of decision one convergence system could not make.

    Each member carries the reason the planner records, the direction relief
    would have to come from, and the decision the system is short of, so all
    three follow from the check that fired.  Deriving the reason from the kind
    rather than classifying a reason string keeps the wording a presentation
    detail of one structural fact.
    """

    SHARED_TRUNK_CHANNEL = (
        "planned convergence trunks require one shared channel decision",
        ConflictRelief.SHARED_CHANNEL,
        "plan-driven shared-channel emission (#1658)",
    )
    SHARED_APPROACH_CHANNEL = (
        "planned convergence feeder approaches require one shared channel decision",
        ConflictRelief.SHARED_CHANNEL,
        "plan-driven shared-channel emission (#1658)",
    )
    OPPOSING_OPENING_CHANNEL = (
        "planned fan arms require opposing opening channels",
        ConflictRelief.SHARED_CHANNEL,
        "plan-driven opposing-opening emission (#1658)",
    )
    NO_APPROACH_SETTLEMENT_ROOM = (
        "planned convergence approaches and trunks have no settlement room",
        ConflictRelief.CLEARANCE,
        "plan-driven chained-convergence emission (#1658)",
    )
    CHAINED_SAME_LINE = (
        "chained same-line convergences require one shared system settlement",
        ConflictRelief.SHARED_CHANNEL,
        "plan-driven chained-convergence emission (#1658)",
    )
    UNOWNED_MEMBER_CORRIDOR = (
        "planned convergence corridor conflicts with unowned route-system member",
        ConflictRelief.CLEARANCE,
        "plan-driven whole-system emission (#1658)",
    )
    UNOWNED_MEMBER_GROUP = (
        "planned convergence corridor conflicts with unowned route-system members",
        ConflictRelief.SHARED_CHANNEL,
        "plan-driven whole-system emission (#1658)",
    )

    def __init__(self, reason: str, relief: ConflictRelief, owner: str) -> None:
        self.reason = reason
        self.relief = relief
        self.owner = owner


@dataclass(frozen=True, slots=True)
class ConvergenceConflict:
    """The geometry one convergence system's compatibility limit was measured on.

    ``sites`` are the two runs the failing check compared, in absolute
    coordinates, and ``separation`` is how far apart it found them along
    ``axis``.  Publishing the measurement rather than only its verdict lets a
    later stage decide whether the limit is one it can move, without
    re-classifying the wording of a reason.
    """

    kind: ConvergenceConflictKind
    axis: DemandAxis
    sites: tuple[
        tuple[tuple[float, float], tuple[float, float]],
        tuple[tuple[float, float], tuple[float, float]],
    ]
    separation: float
    line_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.axis is DemandAxis.BOTH:
            raise ValueError("a convergence conflict is measured along one axis")
        if not all(
            math.isfinite(value)
            for site in self.sites
            for point in site
            for value in point
        ):
            raise ValueError("convergence conflict sites must be finite")
        if not math.isfinite(self.separation) or self.separation < 0.0:
            raise ValueError("convergence conflict separation must be a distance")
        if not self.line_ids or len(set(self.line_ids)) != len(self.line_ids):
            raise ValueError("convergence conflict line membership is incomplete")

    @property
    def measurement(self) -> str:
        first, second = self.sites
        lines = ", ".join(self.line_ids)
        return (
            f"{self.kind.reason} for line(s) {lines}, measured "
            f"{self.separation:.2f}px apart along {self.axis.value} between "
            f"{_site_text(first)} and {_site_text(second)}"
        )


def _site_text(site: tuple[tuple[float, float], tuple[float, float]]) -> str:
    (start_x, start_y), (end_x, end_y) = site
    return f"({start_x:.1f},{start_y:.1f})-({end_x:.1f},{end_y:.1f})"


class ConvergenceEndpointRole(str, Enum):
    """Geometry owned at one convergence member's terminal endpoint."""

    FEEDER = "feeder"
    TRUNK = "trunk"
    CONTINUATION = "continuation"
    COVERED_CONTINUATION = "covered-continuation"


class FanAppearancePolicy(str, Enum):
    """Authored branch-shape policy frozen before fan layout."""

    STRAIGHT = "straight"
    SYMMETRIC = "symmetric"


class FanRouteEmitter(str, Enum):
    """Routing template that exclusively emits one planned fan edge."""

    BOTTOM_EXIT_RIGHT_LANDINGS = "bottom-exit-right-landings"


class TurnHandedness(str, Enum):
    """Screen-space handedness of one perpendicular cardinal turn."""

    CLOCKWISE = "clockwise"
    COUNTERCLOCKWISE = "counterclockwise"


def turn_handedness(run: Direction, turn: Direction) -> TurnHandedness:
    """Return the screen-space handedness of a perpendicular cardinal turn."""
    vectors = {
        Direction.R: (1, 0),
        Direction.L: (-1, 0),
        Direction.U: (0, -1),
        Direction.D: (0, 1),
    }
    run_x, run_y = vectors[run]
    turn_x, turn_y = vectors[turn]
    cross_product = run_x * turn_y - run_y * turn_x
    if cross_product == 0:
        raise ValueError("run and turn directions must be perpendicular")
    return (
        TurnHandedness.CLOCKWISE
        if cross_product > 0
        else TurnHandedness.COUNTERCLOCKWISE
    )


class ExitLaneTransitionPlacement(str, Enum):
    """Which end of a lane hand-off keeps only its minimum runway."""

    SOURCE = "source"
    TARGET = "target"


class ExitLaneOrderSource(str, Enum):
    """Evidence used to order one exit group's active source lanes."""

    STATION_OFFSETS = "station-offsets"
    FRAME_CONSTRAINTS = "frame-constraints"
    GRAPH_LINE_ORDER_FALLBACK = "graph-line-order-fallback"


class BindingKind(str, Enum):
    """How an emission member is represented in the final route set."""

    EMITTED = "emitted"
    MERGE_SKIP = "merge-skip"
    COVERED_MERGE_HOP = "covered-merge-hop"
    UNROUTED = "unrouted"


class CoverageReason(str, Enum):
    """Why another emitted member completely represents a resolved leg."""

    MERGE_TRUNK_COVERS_ENTRY_HOP = "merge-trunk-covers-entry-hop"


class SharedReferenceKind(str, Enum):
    """Vocabulary for geometry shared by members of one route system."""

    CENTRELINE = "centreline"
    TRUNK = "trunk"
    BAND = "band"
    RUNWAY = "runway"
    ORDERED_TURNS = "ordered-turns"
    LANDING_SEQUENCE = "landing-sequence"


class DemandKind(str, Enum):
    """Kinds of symbolic space a later planning stage may reserve."""

    SPAN = "span"
    LANES = "lanes"
    RUNWAY = "runway"
    ORDERED_TURNS = "ordered-turns"
    KEEP_OUT = "keep-out"


class DemandAxis(str, Enum):
    X = "x"
    Y = "y"
    BOTH = "both"

    @property
    def point_index(self) -> int:
        """Index of this axis's coordinate in an ``(x, y)`` point.

        ``BOTH`` names no single coordinate and so has no index of its own; it
        answers 0, leaving a caller that can receive it to rule it out first.
        """
        return 1 if self is DemandAxis.Y else 0


class KeepOutClass(str, Enum):
    """Obstacle classes a symbolic allocation must clear."""

    SECTION = "section"
    HEADER = "header"
    LABEL = "label"
    MARKER = "marker"
    CANVAS = "canvas"


class ReservationDecisionKind(str, Enum):
    """Layout decision referenced by a reservation or symbolic demand."""

    SECTION_GRID = "section-grid"
    SECTION_DIRECTION = "section-direction"
    CONNECTOR_SIDE = "connector-side"
    FOLD_THRESHOLD = "fold-threshold"
    LANE_ORDER = "lane-order"


class ReservationDecisionSource(str, Enum):
    """Who supplied a reservation-affecting layout decision."""

    AUTHOR = "author"
    CALLER = "caller"
    INFERENCE = "inference"


@dataclass(frozen=True, slots=True)
class ReservationDecisionRef:
    """Typed reference to one existing effective layout decision."""

    kind: ReservationDecisionKind
    subject_id: str
    decision: ReservationEffectiveDecision
    role: ConnectorEndpointRole | None = None

    def __post_init__(self) -> None:
        endpoint = self.kind is ReservationDecisionKind.CONNECTOR_SIDE
        if endpoint != (self.role is not None):
            raise ValueError("only connector-side decisions have an endpoint role")
        value = self.decision.value
        if self.kind is ReservationDecisionKind.SECTION_GRID:
            valid_grid = (
                isinstance(value, tuple)
                and len(value) == 4
                and all(isinstance(item, int) for item in value)
            )
            if not valid_grid:
                raise ValueError("section-grid decision requires a four-integer value")
        elif self.kind is ReservationDecisionKind.FOLD_THRESHOLD:
            if not isinstance(value, int):
                raise ValueError("fold-threshold decision requires an integer value")
        elif self.kind is ReservationDecisionKind.CONNECTOR_SIDE:
            if not isinstance(value, PortSide):
                raise ValueError("connector-side decision requires a PortSide value")
        elif not isinstance(value, str):
            raise ValueError(f"{self.kind.value} decision requires a string value")

    @property
    def source(self) -> ReservationDecisionSource:
        if self.decision.reason in {
            DecisionReason.CALLER_FOLD_THRESHOLD,
            DecisionReason.CALLER_LINE_ORDER,
            DecisionReason.CALLER_COMMITMENT,
        }:
            return ReservationDecisionSource.CALLER
        if self.decision.origin is DecisionOrigin.AUTHORED:
            return ReservationDecisionSource.AUTHOR
        return ReservationDecisionSource.INFERENCE


ReservationEffectiveDecision: TypeAlias = (
    EffectiveDecision[GridCell]
    | EffectiveDecision[str]
    | EffectiveDecision[int]
    | EffectiveDecision[PortSide]
    | EffectiveDecision[LineOrder]
)


@dataclass(frozen=True, slots=True)
class GridSpan:
    """Inclusive complete grid extent for a symbolic claim."""

    min_column: int
    max_column: int
    min_row: int
    max_row: int
    coordinate_regime: CoordinateRegime = CoordinateRegime.SETTLED_GRID

    def overlaps(self, other: GridSpan) -> bool:
        """Whether two inclusive grid extents intersect."""
        return not (
            self.max_column < other.min_column
            or other.max_column < self.min_column
            or self.max_row < other.min_row
            or other.max_row < self.min_row
        )


def grid_span_for_sections(
    graph: MetroGraph,
    section_ids: Iterable[str],
) -> GridSpan:
    """Return the inclusive grid extent of the named sections."""
    sections = tuple(graph.sections[section_id] for section_id in section_ids)
    if not sections:
        raise ValueError("grid span has no sections")
    return GridSpan(
        min(section.grid_col for section in sections),
        max(section.grid_col + section.grid_col_span - 1 for section in sections),
        min(section.grid_row for section in sections),
        max(section.grid_row + section.grid_row_span - 1 for section in sections),
    )


@dataclass(frozen=True, slots=True)
class EndpointFact:
    """Settled scalar facts for one physical leg endpoint."""

    station_id: str
    section_id: str | None
    port_id: str | None
    side: PortSide | None
    column: int | None
    row: int | None
    coordinate_regime: CoordinateRegime


@dataclass(frozen=True, slots=True)
class ConnectorLegRef:
    """One connector path occurrence attributed to a physical resolved leg."""

    connector_id: ConnectorId
    path_rank: int
    leg_rank: int


@dataclass(frozen=True, slots=True)
class SectionDecisionFacts:
    section_id: str
    grid: EffectiveDecision[GridCell] | None
    direction: EffectiveDecision[str] | None


@dataclass(frozen=True, slots=True)
class ConnectorDecisionFacts:
    connector_id: ConnectorId
    line_id: str
    bundle_id: BundleId
    exit_side: EffectiveDecision[PortSide] | None
    entry_side: EffectiveDecision[PortSide] | None


@dataclass(frozen=True, slots=True)
class LaneOrderFacts:
    policy: EffectiveDecision[LineOrder]
    source: LineOrderSource
    realised_line_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoutePlanProvenance:
    sections: tuple[SectionDecisionFacts, ...]
    connectors: tuple[ConnectorDecisionFacts, ...]
    fold_threshold: EffectiveDecision[int] | None
    fold_threshold_source: FoldThresholdSource
    lane_order: LaneOrderFacts


@dataclass(frozen=True, slots=True)
class ResolvedEndpointGroup:
    """One topology endpoint group and its resolved boundary port."""

    id: EndpointGroupId
    system_id: RouteSystemId
    role: ConnectorEndpointRole
    section_id: str
    side: PortSide
    port_id: str
    connector_ids: tuple[ConnectorId, ...]


@dataclass(frozen=True, slots=True)
class RouteDivergence:
    """One topology divergence and its resolved fan-out junction."""

    id: DivergenceId
    system_id: RouteSystemId
    junction_id: str
    exit_group_id: EndpointGroupId
    entry_group_ids: tuple[EndpointGroupId, ...]
    connector_ids: tuple[ConnectorId, ...]


@dataclass(frozen=True, slots=True)
class RouteConvergence:
    """One topology convergence and its resolved merge junction."""

    id: ConvergenceId
    system_id: RouteSystemId
    junction_id: str
    entry_group_id: EndpointGroupId
    source_junction_ids: tuple[str, ...]
    divergence_ids: tuple[DivergenceId, ...]
    connector_ids: tuple[ConnectorId, ...]
    line_id: str


@dataclass(frozen=True, slots=True)
class EmissionMember:
    """One unique physical resolved inter-section leg."""

    id: EmissionMemberId
    system_id: RouteSystemId
    source: EndpointFact
    target: EndpointFact
    line_id: str
    line_rank: int
    connector_ids: tuple[ConnectorId, ...]
    leg_refs: tuple[ConnectorLegRef, ...]
    bundle_ids: tuple[BundleId, ...]
    exit_group_ids: tuple[EndpointGroupId, ...]
    entry_group_ids: tuple[EndpointGroupId, ...]
    divergence_ids: tuple[DivergenceId, ...]
    convergence_ids: tuple[ConvergenceId, ...]
    roles: tuple[EmissionRole, ...]
    family_id: RouteFamilyId | None

    @property
    def edge(self) -> ResolvedEdge:
        """Return the scalar final-edge key represented by this member."""
        return ResolvedEdge(
            self.source.station_id, self.target.station_id, self.line_id
        )


@dataclass(frozen=True, slots=True)
class RouteBranch:
    id: RouteBranchId
    system_id: RouteSystemId
    divergence_id: DivergenceId
    entry_group_id: EndpointGroupId
    connector_ids: tuple[ConnectorId, ...]
    line_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RouteFeeder:
    id: RouteFeederId
    system_id: RouteSystemId
    convergence_id: ConvergenceId
    divergence_id: DivergenceId
    connector_ids: tuple[ConnectorId, ...]
    line_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SharedReference:
    """A shared geometry identity populated by its owning child planner."""

    id: SharedReferenceId
    system_id: RouteSystemId
    kind: SharedReferenceKind
    claimant_member_ids: tuple[EmissionMemberId, ...]
    coordinate_regime: CoordinateRegime
    provenance: tuple[ReservationDecisionRef, ...]


@dataclass(frozen=True, slots=True)
class SymbolicDemand:
    """A complete symbolic allocation claim with no absolute geometry."""

    id: DemandId
    system_id: RouteSystemId
    claimant_member_ids: tuple[EmissionMemberId, ...]
    kind: DemandKind
    axis: DemandAxis
    span: GridSpan
    lane_count: int
    minimum_size: float | None
    minimum_size_regime: CoordinateRegime | None
    ordered_reference_ids: tuple[SharedReferenceId, ...]
    keep_out_classes: tuple[KeepOutClass, ...]
    provenance: tuple[ReservationDecisionRef, ...]

    def __post_init__(self) -> None:
        if (self.minimum_size is None) is not (self.minimum_size_regime is None):
            raise ValueError(
                "minimum_size and minimum_size_regime must be provided together"
            )


@dataclass(frozen=True, slots=True)
class ExitSourceLane:
    """One active source lane in compact visual order."""

    line_id: str
    rank: int
    member_ids: tuple[EmissionMemberId, ...]
    station_ids: tuple[str, ...]
    input_offset: float
    planned_offset: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.input_offset) or not math.isfinite(
            self.planned_offset
        ):
            raise ValueError("exit source-lane offsets must be finite")


@dataclass(frozen=True, slots=True)
class ExitLaneTransition:
    """Template input for one 45-degree compact-lane hand-off."""

    edge: ResolvedEdge
    claimant_member_ids: tuple[EmissionMemberId, ...]
    source_point: tuple[float, float]
    target_point: tuple[float, float]
    source_offset: float
    target_offset: float
    source_lane_offset: float
    target_lane_offset: float
    run_direction: Direction
    placement: ExitLaneTransitionPlacement
    diagonal_run: float
    source_runway: float
    target_runway: float
    coordinate_regime: CoordinateRegime = CoordinateRegime.LAYOUT_CANVAS

    def __post_init__(self) -> None:
        if not self.claimant_member_ids or len(set(self.claimant_member_ids)) != len(
            self.claimant_member_ids
        ):
            raise ValueError(
                "exit lane-transition claimants must be unique and nonempty"
            )
        values = (
            *self.source_point,
            *self.target_point,
            self.source_offset,
            self.target_offset,
            self.source_lane_offset,
            self.target_lane_offset,
            self.diagonal_run,
            self.source_runway,
            self.target_runway,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("exit lane-transition geometry must be finite")


@dataclass(frozen=True, slots=True)
class ExitTurnAxis:
    """One shared turn axis assigned to every arm of one source lane."""

    id: ExitTurnAxisId
    line_id: str
    axis: DemandAxis
    coordinate: float
    rank: int
    claimant_member_ids: tuple[EmissionMemberId, ...]
    fixed_anchor_id: str | None
    fixed_anchor_coordinate: float | None
    fixed_anchor_offset: float | None
    coordinate_regime: CoordinateRegime = CoordinateRegime.LAYOUT_CANVAS

    def __post_init__(self) -> None:
        if not math.isfinite(self.coordinate):
            raise ValueError("exit-turn axis coordinate must be finite")
        anchor_values = (
            self.fixed_anchor_id,
            self.fixed_anchor_coordinate,
            self.fixed_anchor_offset,
        )
        if any(item is None for item in anchor_values) and any(
            item is not None for item in anchor_values
        ):
            raise ValueError("exit-turn fixed-axis anchor is incomplete")
        if self.fixed_anchor_coordinate is not None and not math.isfinite(
            self.fixed_anchor_coordinate
        ):
            raise ValueError("exit-turn fixed-axis anchor must be finite")
        if self.fixed_anchor_offset is not None and not math.isfinite(
            self.fixed_anchor_offset
        ):
            raise ValueError("exit-turn fixed-axis offset must be finite")


@dataclass(frozen=True, slots=True)
class ExitTurnAssignment:
    """Planned source-side geometry for one outbound emission member."""

    member_id: EmissionMemberId
    entry_group_id: EndpointGroupId
    destination_section_id: str
    destination_column: int
    destination_row: int
    destination_side: PortSide
    source_lane_rank: int
    planned_family_id: RouteFamilyId
    roles: tuple[EmissionRole, ...]
    run_direction: Direction | None
    turn_direction: Direction | None
    launch_coordinate: float | None
    minimum_runway: float | None
    handedness: TurnHandedness | None
    axis_id: ExitTurnAxisId | None


@dataclass(frozen=True, slots=True)
class ExitTurnPlan:
    """Complete immutable source-bundle decision made before route emission."""

    id: ExitTurnPlanId
    system_id: RouteSystemId
    exit_group_id: EndpointGroupId
    exit_port_id: str
    divergence_id: DivergenceId | None
    source_id: str
    source_run_direction: Direction | None
    source_axis: DemandAxis
    connector_ids: tuple[ConnectorId, ...]
    system_member_ids: tuple[EmissionMemberId, ...]
    member_ids: tuple[EmissionMemberId, ...]
    source_lanes: tuple[ExitSourceLane, ...]
    lane_order_source: ExitLaneOrderSource
    lane_transitions: tuple[ExitLaneTransition, ...]
    axes: tuple[ExitTurnAxis, ...]
    assignments: tuple[ExitTurnAssignment, ...]
    unclassified_member_ids: tuple[EmissionMemberId, ...]
    spacing: float
    minimum_runway: float
    reference_id: SharedReferenceId | None
    demand_ids: tuple[DemandId, ...]
    foreign_reference_ids: tuple[SharedReferenceId, ...]
    disposition: ExitTurnDisposition
    legacy_reason: str | None
    provenance: tuple[ReservationDecisionRef, ...]

    def __post_init__(self) -> None:
        planned = self.disposition is ExitTurnDisposition.PLANNED
        if not isinstance(self.lane_order_source, ExitLaneOrderSource):
            raise ValueError("exit-turn lane-order provenance must be typed")
        if (
            not math.isfinite(self.spacing)
            or not math.isfinite(self.minimum_runway)
            or self.spacing <= 0
            or self.minimum_runway <= 0
        ):
            raise ValueError(
                "exit-turn geometry requirements must be finite and positive"
            )
        if planned != (self.legacy_reason is None):
            raise ValueError("exit-turn disposition and legacy reason disagree")
        if (self.reference_id is not None) != (planned and bool(self.axes)):
            raise ValueError("only planned turn axes own a shared reference")
        expected_axis = (
            DemandAxis.X
            if self.source_run_direction in {Direction.R, Direction.L}
            else DemandAxis.Y
            if self.source_run_direction in {Direction.U, Direction.D}
            else None
        )
        if self.source_axis not in {DemandAxis.X, DemandAxis.Y}:
            raise ValueError("exit turn has no source axis")
        if planned and self.source_axis is not expected_axis:
            raise ValueError("planned exit turn has inconsistent source orientation")


@dataclass(frozen=True, slots=True)
class FanBranchPlan:
    """One canonical branch of a structural fan."""

    id: FanBranchPlanId
    rank: int
    landing_rank: int
    opening_rank: int
    root_station_id: str
    tail_station_id: str
    continuation_edge_ids: tuple[ConnectorId, ...]
    continuation_resolved_paths: tuple[tuple[ResolvedEdge, ...], ...]
    connector_ids: tuple[ConnectorId, ...]
    member_ids: tuple[EmissionMemberId, ...]
    line_ids: tuple[str, ...]
    extra_output_edge_ids: tuple[ConnectorId, ...]
    extra_output_resolved_paths: tuple[tuple[ResolvedEdge, ...], ...]
    landing_port_ids: tuple[str, ...]
    lane_station_ids: tuple[str, ...]
    is_trunk_continuation: bool
    terminal: bool
    lane_offset: float | None
    diagonal_runway: float | None

    def __post_init__(self) -> None:
        if self.rank < 0:
            raise ValueError("fan branch rank must be non-negative")
        if self.landing_rank < 0:
            raise ValueError("fan branch landing rank must be non-negative")
        if self.opening_rank < 0:
            raise ValueError("fan branch opening rank must be non-negative")
        if not self.continuation_edge_ids:
            raise ValueError("fan branch has no authored members")
        if len(set(self.connector_ids)) != len(self.connector_ids):
            raise ValueError("fan branch repeats a route connector")
        if not set(self.connector_ids).issubset(self.authored_edge_ids):
            raise ValueError("fan branch connector lies outside authored membership")
        if len(set(self.member_ids)) != len(self.member_ids):
            raise ValueError("fan branch repeats an emission member")
        if not self.line_ids:
            raise ValueError("fan branch has no line membership")
        if self.lane_offset is not None and not math.isfinite(self.lane_offset):
            raise ValueError("fan branch lane offset must be finite")
        if self.diagonal_runway is not None and (
            not math.isfinite(self.diagonal_runway) or self.diagonal_runway <= 0
        ):
            raise ValueError("fan branch diagonal runway must be positive")

    @property
    def authored_edge_ids(self) -> tuple[ConnectorId, ...]:
        """Complete branch membership, with the continuation first."""
        return (*self.continuation_edge_ids, *self.extra_output_edge_ids)

    @property
    def resolved_paths(self) -> tuple[tuple[ResolvedEdge, ...], ...]:
        """Complete resolved branch membership in authored order."""
        return (*self.continuation_resolved_paths, *self.extra_output_resolved_paths)


@dataclass(frozen=True, slots=True)
class FanOffsetAssignment:
    """One line's signed slot in an immutable fan offset frame."""

    line_id: str
    slot: int

    def __post_init__(self) -> None:
        if not self.line_id:
            raise ValueError("fan offset assignment has no line")
        if type(self.slot) is not int:
            raise ValueError("fan offset assignment slot must be an integer")


@dataclass(frozen=True, slots=True)
class FanOffsetCarrier:
    """Exact station and signed line slots carrying a planned permutation."""

    station_id: str
    assignments: tuple[FanOffsetAssignment, ...]

    def __post_init__(self) -> None:
        if not self.station_id or not self.assignments:
            raise ValueError("fan offset carrier is incomplete")
        if len(set(self.line_ids)) != len(self.line_ids):
            raise ValueError("fan offset carrier repeats a line")
        if len({assignment.slot for assignment in self.assignments}) != len(
            self.assignments
        ):
            raise ValueError("fan offset carrier repeats a slot")

    @property
    def line_ids(self) -> tuple[str, ...]:
        return tuple(assignment.line_id for assignment in self.assignments)


@dataclass(frozen=True, slots=True)
class FanRouteEmission:
    """Exact resolved edge assigned to one planned routing template."""

    edge: ResolvedEdge
    branch_id: FanBranchPlanId
    emitter: FanRouteEmitter


@dataclass(frozen=True, slots=True)
class FanRouteExpectation:
    """One resolved fan edge, optionally bound to an emission member."""

    edge: ResolvedEdge
    member_id: EmissionMemberId | None
    branch_ids: tuple[FanBranchPlanId, ...]


@dataclass(frozen=True, slots=True)
class FanCentrelineAnchor:
    """Frozen station source and offset defining a fan centreline."""

    station_id: str
    lane_offset: float = 0.0

    def __post_init__(self) -> None:
        if not self.station_id:
            raise ValueError("fan centreline anchor has no station")
        if not math.isfinite(self.lane_offset):
            raise ValueError("fan centreline anchor offset must be finite")

    def coordinate(
        self, frame: AxisFrame, station: Station, *, lane_sign: float
    ) -> float:
        """Resolve the centreline from the anchor station in ``frame``."""
        return frame.secondary.get(station) - lane_sign * self.lane_offset


def fan_has_vacant_trunk(
    appearance_policy: FanAppearancePolicy,
    authored_join_station_id: str | None,
    branches: Iterable[FanBranchPlan],
) -> bool:
    """Whether a straight reconvergence reserves an unoccupied centreline."""
    return (
        appearance_policy is FanAppearancePolicy.STRAIGHT
        and authored_join_station_id is not None
        and sum(branch.is_trunk_continuation for branch in branches) > 1
    )


@dataclass(frozen=True, slots=True)
class FanPlan:
    """Complete immutable decision for one authored fan or diamond.

    A plan owns every branch member or owns no geometry.  ``LEGACY`` records
    retain the structural evidence and deterministic reason so callers never
    have to infer partial ownership from missing branch records.
    """

    id: FanPlanId
    system_id: RouteSystemId | None
    authored_source_id: str
    authored_join_station_id: str | None
    fork_station_id: str
    direction: FlowDirection | None
    join_station_id: str | None
    appearance_policy: FanAppearancePolicy
    appearance_centreline_branch_id: FanBranchPlanId | None
    appearance_lane_pitch: float | None
    appearance_lane_sign: float | None
    branches: tuple[FanBranchPlan, ...]
    offset_line_order: tuple[str, ...]
    authored_edge_ids: tuple[ConnectorId, ...]
    connector_ids: tuple[ConnectorId, ...]
    member_ids: tuple[EmissionMemberId, ...]
    resolved_member_paths: tuple[tuple[ResolvedEdge, ...], ...]
    resolved_member_edges: tuple[ResolvedEdge, ...]
    entry_seam_paths: tuple[tuple[ResolvedEdge, ...], ...]
    exit_seam_paths: tuple[tuple[ResolvedEdge, ...], ...]
    resolved_seam_edges: tuple[ResolvedEdge, ...]
    entry_handoff_edge_ids: tuple[ConnectorId, ...]
    exit_handoff_edge_ids: tuple[ConnectorId, ...]
    entry_handoff_paths: tuple[tuple[ResolvedEdge, ...], ...]
    exit_handoff_paths: tuple[tuple[ResolvedEdge, ...], ...]
    offset_carriers: tuple[FanOffsetCarrier, ...]
    route_expectations: tuple[FanRouteExpectation, ...]
    route_emissions: tuple[FanRouteEmission, ...]
    centreline_port_ids: tuple[str, ...]
    entry_port_ids: tuple[str, ...]
    exit_port_ids: tuple[str, ...]
    trunk_follower_ids: tuple[str, ...]
    entry_runway: float | None
    exit_runway: float | None
    centreline_reference_id: SharedReferenceId | None
    demand_ids: tuple[DemandId, ...]
    bundle_handoff_ids: tuple[BundleId, ...]
    convergence_handoff_ids: tuple[ConvergenceId, ...]
    owned_station_ids: tuple[str, ...]
    centreline_station_ids: tuple[str, ...]
    centreline_anchor: FanCentrelineAnchor | None
    local_frame_anchor: FanCentrelineAnchor | None
    frame: AxisFrame | None
    disposition: FanPlanDisposition
    legacy_reason: str | None

    def __post_init__(self) -> None:
        planned = self.disposition is FanPlanDisposition.PLANNED
        self._validate_branches()
        self._validate_membership()
        self._validate_disposition(planned)
        self._validate_layout_ownership(planned)

    def _validate_branches(self) -> None:
        if len(self.branches) < 2:
            raise ValueError("fan plan requires at least two branches")
        if tuple(branch.rank for branch in self.branches) != tuple(
            range(len(self.branches))
        ):
            raise ValueError("fan branch ranks are not canonical")
        if tuple(sorted(branch.landing_rank for branch in self.branches)) != tuple(
            range(len(self.branches))
        ):
            raise ValueError("fan branch landing ranks are not canonical")
        if tuple(sorted(branch.opening_rank for branch in self.branches)) != tuple(
            range(len(self.branches))
        ):
            raise ValueError("fan branch opening ranks are not canonical")
        if len(set(self.offset_line_order)) != len(self.offset_line_order):
            raise ValueError("fan offset order repeats a line")
        branch_line_ids = {
            line_id for branch in self.branches for line_id in branch.line_ids
        }
        if not set(self.offset_line_order).issubset(branch_line_ids):
            raise ValueError("fan offset order names a line outside its branches")

    def _validate_membership(self) -> None:
        if not isinstance(self.appearance_policy, FanAppearancePolicy):
            raise ValueError("fan appearance policy is not canonical")
        if self.appearance_centreline_branch_id is not None and (
            self.appearance_centreline_branch_id
            not in {branch.id for branch in self.branches}
        ):
            raise ValueError("fan appearance centreline names an unknown branch")
        if len(set(self.authored_edge_ids)) != len(self.authored_edge_ids):
            raise ValueError("fan plan repeats an authored member")
        if len(set(self.connector_ids)) != len(self.connector_ids):
            raise ValueError("fan plan repeats a route connector")
        if not set(self.connector_ids).issubset(self.authored_edge_ids):
            raise ValueError("fan connector ownership lies outside authored membership")
        if len(set(self.member_ids)) != len(self.member_ids):
            raise ValueError("fan plan repeats an emission member")
        if self.system_id is None and (self.connector_ids or self.member_ids):
            raise ValueError("layout-only fan claims canonical route ownership")
        if self.system_id is not None and not self.connector_ids:
            raise ValueError("route-owned fan has no canonical connector")
        expected_authored_edge_ids = tuple(
            dict.fromkeys(
                edge_id
                for branch in self.branches
                for edge_id in branch.authored_edge_ids
            )
        )
        if self.authored_edge_ids != expected_authored_edge_ids:
            raise ValueError("fan authored membership is not canonical")
        expected_member_paths = (
            *self.entry_seam_paths,
            *(path for branch in self.branches for path in branch.resolved_paths),
            *self.exit_seam_paths,
        )
        if self.resolved_member_paths != expected_member_paths:
            raise ValueError("fan resolved paths are not canonical")
        expected_member_edges = tuple(
            dict.fromkeys(edge for path in expected_member_paths for edge in path)
        )
        if self.resolved_member_edges != expected_member_edges:
            raise ValueError("fan resolved edge membership is not canonical")
        expectation_edges = tuple(item.edge for item in self.route_expectations)
        if self.disposition is FanPlanDisposition.PLANNED:
            if expectation_edges != self.resolved_member_edges:
                raise ValueError("planned fan route expectations are incomplete")
        elif self.route_expectations:
            raise ValueError("legacy fan owns route expectations")
        expectation_member_ids = tuple(
            item.member_id
            for item in self.route_expectations
            if item.member_id is not None
        )
        if (
            self.disposition is FanPlanDisposition.PLANNED
            and expectation_member_ids != self.member_ids
        ):
            raise ValueError("fan route expectations disagree with emission members")
        branch_ids = {branch.id for branch in self.branches}
        if any(
            not set(item.branch_ids).issubset(branch_ids)
            for item in self.route_expectations
        ):
            raise ValueError("fan route expectation names an unknown branch")
        expected_seam_edges = tuple(
            dict.fromkeys(
                edge
                for path in (*self.entry_seam_paths, *self.exit_seam_paths)
                for edge in path
            )
        )
        if self.resolved_seam_edges != expected_seam_edges:
            raise ValueError("fan resolved seam membership is not canonical")
        if len({item.edge for item in self.route_emissions}) != len(
            self.route_emissions
        ):
            raise ValueError("fan plan repeats a route emission")
        if any(item.branch_id not in branch_ids for item in self.route_emissions):
            raise ValueError("fan route emission names an unknown branch")
        if any(
            item.edge not in self.resolved_member_edges for item in self.route_emissions
        ):
            raise ValueError("fan route emission lies outside complete membership")
        if any(
            item.edge.source != self.fork_station_id for item in self.route_emissions
        ):
            raise ValueError("fan route emission does not leave its fork")

    def _validate_disposition(self, planned: bool) -> None:
        if planned and self.direction is None:
            raise ValueError("fan plan has an unsupported direction")
        if (
            planned
            and self.authored_join_station_id is not None
            and self.join_station_id is None
        ):
            raise ValueError("planned reconvergence has no resolved join")
        local_frame_owned = bool(self.layout_station_ids)
        has_appearance_centreline = self.appearance_centreline_branch_id is not None
        has_vacant_trunk = fan_has_vacant_trunk(
            self.appearance_policy,
            self.authored_join_station_id,
            self.branches,
        )
        if (
            planned
            and self.appearance_policy is FanAppearancePolicy.STRAIGHT
            and has_appearance_centreline
            != (local_frame_owned and not has_vacant_trunk)
        ):
            raise ValueError(
                "straight local fan requires one centreline branch or a vacant trunk"
            )
        if (
            self.appearance_policy is FanAppearancePolicy.SYMMETRIC
            and has_appearance_centreline
        ):
            raise ValueError("symmetric fan cannot name a straight centreline branch")
        if not planned and has_appearance_centreline:
            raise ValueError("legacy fan cannot own an appearance centreline")
        if planned != (self.appearance_lane_pitch is not None):
            raise ValueError("planned fan appearance lane pitch is missing")
        if self.appearance_lane_pitch is not None and (
            not math.isfinite(self.appearance_lane_pitch)
            or self.appearance_lane_pitch <= 0.0
        ):
            raise ValueError("fan appearance lane pitch must be finite and positive")
        if planned != (self.appearance_lane_sign is not None):
            raise ValueError("planned fan appearance lane sign is missing")
        if self.appearance_lane_sign is not None and self.appearance_lane_sign not in (
            -1.0,
            1.0,
        ):
            raise ValueError("fan appearance lane sign must be -1 or +1")
        if has_appearance_centreline:
            lane_offsets = tuple(branch.lane_offset for branch in self.branches)
            if (
                sum(offset == 0.0 for offset in lane_offsets) != 1
                or any(offset is None or offset < 0.0 for offset in lane_offsets)
                or next(
                    branch.lane_offset
                    for branch in self.branches
                    if branch.id == self.appearance_centreline_branch_id
                )
                != 0.0
            ):
                raise ValueError(
                    "straight local fan must have one non-negative centreline lane"
                )
        if planned != (self.frame is not None and self.legacy_reason is None):
            raise ValueError("fan disposition and geometry ownership disagree")
        if planned and any(branch.lane_offset is None for branch in self.branches):
            raise ValueError("planned fan branch has no lane offset")
        if planned:
            assert self.appearance_lane_pitch is not None
            lane_offsets = tuple(branch.lane_offset for branch in self.branches)
            expected_lane_offsets = fan_lane_offsets(
                tuple(branch.id for branch in self.branches),
                self.appearance_lane_pitch,
                self.appearance_centreline_branch_id,
            )
            if any(
                actual is None or abs(actual - expected) > 1e-9
                for actual, expected in zip(
                    lane_offsets, expected_lane_offsets, strict=True
                )
            ):
                raise ValueError("fan lane offsets disagree with appearance pitch")
        if planned and any(branch.diagonal_runway is None for branch in self.branches):
            raise ValueError("planned fan branch has no diagonal runway")
        if not planned and any(
            branch.lane_offset is not None or branch.diagonal_runway is not None
            for branch in self.branches
        ):
            raise ValueError("legacy fan branch owns relative geometry")
        if planned != (self.entry_runway is not None and self.exit_runway is not None):
            raise ValueError("fan disposition and runway ownership disagree")
        has_any_resource = self.centreline_reference_id is not None or bool(
            self.demand_ids
        )
        has_complete_resources = self.centreline_reference_id is not None and bool(
            self.demand_ids
        )
        if has_any_resource != has_complete_resources:
            raise ValueError("fan shared resources are incomplete")
        if (planned and self.system_id is not None) != has_complete_resources:
            raise ValueError("fan route ownership and shared resources disagree")
        for runway in (self.entry_runway, self.exit_runway):
            if runway is not None and (not math.isfinite(runway) or runway <= 0):
                raise ValueError("fan runway must be finite and positive")
        if self.frame is not None:
            assert self.direction is not None
            expected_axes = AxisFrame.axes_for_direction(self.direction)
            if (self.frame.primary.name, self.frame.secondary.name) != expected_axes:
                raise ValueError("fan frame axes disagree with its direction")
            if self.frame.primary_sign != AxisFrame.flow_sign(self.direction):
                raise ValueError("fan frame flow sign disagrees with its direction")
            if self.frame.secondary_sign != AxisFrame.secondary_sign_for(
                self.direction
            ):
                raise ValueError("fan frame lane sign disagrees with its direction")

    def appearance_coordinate(self, centreline: float, lane_offset: float) -> float:
        """Map one canonical appearance offset onto the section's track axis."""
        if self.appearance_lane_sign is None:
            raise ValueError("legacy fan has no appearance coordinate")
        return centreline + self.appearance_lane_sign * lane_offset

    def appearance_centreline_coordinate(
        self, anchor: FanCentrelineAnchor, station: Station
    ) -> float:
        """Resolve the fan centreline from an appearance-owned anchor."""
        if self.frame is None or self.appearance_lane_sign is None:
            raise ValueError("legacy fan has no appearance centreline")
        return anchor.coordinate(
            self.frame,
            station,
            lane_sign=self.appearance_lane_sign,
        )

    def _validate_layout_ownership(self, planned: bool) -> None:
        layout_station_ids = self.layout_station_ids
        if len(set(layout_station_ids)) != len(layout_station_ids):
            raise ValueError("fan plan repeats a layout-owned station")
        if any(
            station_id not in self.owned_station_ids
            for station_id in layout_station_ids
        ):
            raise ValueError("fan layout ownership lies outside complete membership")
        if len({carrier.station_id for carrier in self.offset_carriers}) != len(
            self.offset_carriers
        ):
            raise ValueError("fan plan repeats an offset carrier")
        if any(
            carrier.station_id not in self.owned_station_ids
            for carrier in self.offset_carriers
        ):
            raise ValueError("fan offset carrier lies outside complete ownership")
        branch_line_ids = {
            line_id for branch in self.branches for line_id in branch.line_ids
        }
        if any(
            not set(carrier.line_ids).issubset(branch_line_ids)
            for carrier in self.offset_carriers
        ):
            raise ValueError("fan offset carrier names a line outside its branches")
        if self.offset_carriers and not self.offset_line_order:
            raise ValueError("fan offset carriers have no canonical line order")
        if any(
            not set(carrier.line_ids).issubset(self.offset_line_order)
            for carrier in self.offset_carriers
        ):
            raise ValueError("fan offset carrier names a line outside its offset order")
        if any(
            abs(assignment.slot) >= len(self.offset_line_order)
            for carrier in self.offset_carriers
            for assignment in carrier.assignments
        ):
            raise ValueError("fan offset carrier slot lies outside its offset frame")
        if not planned and self.offset_carriers:
            raise ValueError("legacy fan owns offset carriers")
        if not planned and self.route_emissions:
            raise ValueError("legacy fan owns route emissions")
        if len(set(self.centreline_port_ids)) != len(self.centreline_port_ids):
            raise ValueError("fan plan repeats a centreline port")
        if any(
            port_id not in {*self.entry_port_ids, *self.exit_port_ids}
            for port_id in self.centreline_port_ids
        ):
            raise ValueError("fan centreline port lies outside port membership")
        if any(
            port_id not in self.owned_station_ids
            for port_id in self.centreline_port_ids
        ):
            raise ValueError("fan centreline port lies outside complete ownership")
        if not planned and self.centreline_port_ids:
            raise ValueError("legacy fan owns centreline ports")
        needs_centreline_anchor = bool(layout_station_ids or self.centreline_port_ids)
        if planned and needs_centreline_anchor != (self.centreline_anchor is not None):
            raise ValueError("planned fan centreline anchor is incomplete")
        if not planned and self.centreline_anchor is not None:
            raise ValueError("legacy fan owns a centreline anchor")
        if (
            self.centreline_anchor is not None
            and self.centreline_anchor.station_id
            not in {
                *self.owned_station_ids,
                *self.entry_port_ids,
                *self.exit_port_ids,
            }
        ):
            raise ValueError("fan centreline anchor lies outside complete membership")
        if (
            self.local_frame_anchor is not None
            and self.local_frame_anchor.station_id not in layout_station_ids
        ):
            raise ValueError("fan local frame anchor lies outside layout ownership")
        if planned and bool(layout_station_ids) != (
            self.local_frame_anchor is not None
        ):
            raise ValueError(
                "planned fan local frame anchor disagrees with layout ownership"
            )
        if not planned and self.local_frame_anchor is not None:
            raise ValueError("legacy fan owns a local frame anchor")

    @property
    def layout_station_ids(self) -> tuple[str, ...]:
        """Stations whose secondary coordinate is owned by this plan."""
        return (
            *self.centreline_station_ids,
            *(
                station_id
                for branch in self.branches
                for station_id in branch.lane_station_ids
            ),
        )

    @property
    def owns_geometry(self) -> bool:
        """Whether this complete fan uses its immutable geometry plan."""
        return self.disposition is FanPlanDisposition.PLANNED

    @property
    def has_vacant_trunk(self) -> bool:
        """Whether a straight reconvergence reserves an unoccupied centreline."""
        return fan_has_vacant_trunk(
            self.appearance_policy,
            self.authored_join_station_id,
            self.branches,
        )


@dataclass(frozen=True, slots=True)
class ConvergenceTrunkAxis:
    """Shared trunk axis and inclusive extent in layout-canvas coordinates."""

    axis: DemandAxis
    coordinate: float
    extent_start: float
    extent_end: float
    direction: Direction
    source_flank_coordinate: float
    target_flank_coordinate: float
    source_endpoint_coordinate: float | None = None
    target_endpoint_coordinate: float | None = None
    coordinate_regime: CoordinateRegime = CoordinateRegime.LAYOUT_CANVAS
    claimant_member_ids: tuple[EmissionMemberId, ...] = ()
    """The members that travel the trunk: its trunk member and every feeder that
    lands on it.  A continuation leaves the trunk at a point of its own and
    states that point itself, so it stands on the trunk without claiming its
    coordinates."""

    def __post_init__(self) -> None:
        if self.axis is DemandAxis.BOTH:
            raise ValueError("convergence trunk requires one scalar travel axis")
        if not all(
            math.isfinite(value)
            for value in (
                self.coordinate,
                self.extent_start,
                self.extent_end,
                self.source_flank_coordinate,
                self.target_flank_coordinate,
                *(
                    value
                    for value in (
                        self.source_endpoint_coordinate,
                        self.target_endpoint_coordinate,
                    )
                    if value is not None
                ),
            )
        ):
            raise ValueError("convergence trunk geometry must be finite")
        if self.extent_end - self.extent_start <= COORD_TOLERANCE:
            raise ValueError("convergence trunk requires a positive extent")
        if (self.source_endpoint_coordinate is None) != (
            self.target_endpoint_coordinate is None
        ):
            raise ValueError("convergence trunk endpoint extent is incomplete")
        horizontal = self.axis is DemandAxis.X
        if horizontal != (self.direction in {Direction.R, Direction.L}):
            raise ValueError("convergence trunk direction disagrees with its axis")


@dataclass(frozen=True, slots=True)
class ConvergenceLanding:
    """One feeder's ordered approach and exact endpoint on shared geometry."""

    member_id: EmissionMemberId
    edge: ResolvedEdge
    source_junction_id: str
    approach_axis: DemandAxis
    approach_direction: Direction
    source_column: int | None
    source_row: int | None
    lane_rank: int
    order: int
    join_point: tuple[float, float]
    corner_handedness: TurnHandedness | None
    minimum_runway: float
    opening_turn_coordinate: float | None
    opening_turn_segment: tuple[tuple[float, float], tuple[float, float]] | None
    bypass: bool
    long_haul: bool
    multiple_row: bool

    def __post_init__(self) -> None:
        if self.approach_axis is DemandAxis.BOTH:
            raise ValueError("convergence feeder requires one approach axis")
        horizontal = self.approach_axis is DemandAxis.X
        if horizontal != (self.approach_direction in {Direction.R, Direction.L}):
            raise ValueError("convergence approach direction disagrees with its axis")
        if self.lane_rank < 0 or self.order < 0:
            raise ValueError("convergence feeder ranks must be non-negative")
        if self.minimum_runway <= 0 or not math.isfinite(self.minimum_runway):
            raise ValueError("convergence feeder runway must be positive and finite")
        if self.opening_turn_coordinate is not None and not math.isfinite(
            self.opening_turn_coordinate
        ):
            raise ValueError("convergence feeder opening turn must be finite")
        if (self.opening_turn_coordinate is None) != (
            self.opening_turn_segment is None
        ):
            raise ValueError("convergence feeder opening turn is incomplete")
        if self.opening_turn_segment is not None:
            assert self.opening_turn_coordinate is not None
            start, end = self.opening_turn_segment
            if (
                not all(
                    math.isfinite(value) for point in (start, end) for value in point
                )
                or abs(start[0] - end[0]) > COORD_TOLERANCE
                or abs(start[0] - self.opening_turn_coordinate) > COORD_TOLERANCE
                or abs(start[1] - end[1]) <= COORD_TOLERANCE
            ):
                raise ValueError("convergence feeder opening turn is invalid")
        if not all(math.isfinite(value) for value in self.join_point):
            raise ValueError("convergence feeder join point must be finite")


@dataclass(frozen=True, slots=True)
class ConvergenceContinuation:
    """Outgoing member beginning on the geometry shared by all feeders."""

    member_id: EmissionMemberId
    edge: ResolvedEdge
    entry_port_id: str
    lane_rank: int
    start_point: tuple[float, float]
    end_point: tuple[float, float]
    covered_by_member_id: EmissionMemberId | None

    def __post_init__(self) -> None:
        if self.lane_rank < 0:
            raise ValueError("convergence continuation lane rank must be non-negative")
        if not all(
            math.isfinite(value)
            for point in (self.start_point, self.end_point)
            for value in point
        ):
            raise ValueError("convergence continuation endpoints must be finite")


@dataclass(frozen=True, slots=True)
class ConvergenceEndpointOwnership:
    """Exact emission or coverage owner for one convergence member endpoint."""

    member_id: EmissionMemberId
    edge: ResolvedEdge
    connector_ids: tuple[ConnectorId, ...]
    role: ConvergenceEndpointRole
    endpoint: tuple[float, float]
    covered_by_member_id: EmissionMemberId | None = None

    def __post_init__(self) -> None:
        if not self.connector_ids or len(set(self.connector_ids)) != len(
            self.connector_ids
        ):
            raise ValueError("convergence endpoint connector ownership is incomplete")
        if not all(math.isfinite(value) for value in self.endpoint):
            raise ValueError("convergence owned endpoint must be finite")
        covered = self.role is ConvergenceEndpointRole.COVERED_CONTINUATION
        if covered != (self.covered_by_member_id is not None):
            raise ValueError("convergence coverage ownership is incomplete")
        if self.covered_by_member_id == self.member_id:
            raise ValueError("convergence member cannot cover itself")


@dataclass(frozen=True, slots=True)
class ConvergencePlan:
    """Complete immutable decision for one convergence in a route system."""

    id: ConvergencePlanId
    system_id: RouteSystemId
    convergence_ids: tuple[ConvergenceId, ...]
    entry_group_ids: tuple[EndpointGroupId, ...]
    merge_junction_ids: tuple[str, ...]
    target_entry_port_ids: tuple[str, ...]
    connector_ids: tuple[ConnectorId, ...]
    member_ids: tuple[EmissionMemberId, ...]
    resolved_member_paths: tuple[tuple[ResolvedEdge, ...], ...]
    resolved_member_edges: tuple[ResolvedEdge, ...]
    line_ids: tuple[str, ...]
    upstream_exit_turn_plan_ids: tuple[ExitTurnPlanId, ...]
    upstream_fan_plan_ids: tuple[FanPlanId, ...]
    primary_trunk_member_id: EmissionMemberId | None
    primary_trunk_reason: ConvergenceTrunkReason | None
    trunk_axis: ConvergenceTrunkAxis | None
    landings: tuple[ConvergenceLanding, ...]
    outgoing_continuations: tuple[ConvergenceContinuation, ...]
    lane_order: tuple[str, ...]
    endpoint_ownership: tuple[ConvergenceEndpointOwnership, ...]
    shared_reference_ids: tuple[SharedReferenceId, ...]
    demand_ids: tuple[DemandId, ...]
    foreign_reference_ids: tuple[SharedReferenceId, ...]
    disposition: ConvergenceDisposition
    legacy_reason: str | None
    conflict: ConvergenceConflict | None = None

    def __post_init__(self) -> None:
        planned = self.disposition is ConvergenceDisposition.PLANNED
        if (
            self.conflict is not None
            and self.conflict.kind.reason != self.legacy_reason
        ):
            raise ValueError("convergence conflict does not explain its own reason")
        unique_fields = (
            self.convergence_ids,
            self.entry_group_ids,
            self.merge_junction_ids,
            self.target_entry_port_ids,
            self.connector_ids,
            self.member_ids,
            self.resolved_member_edges,
            self.line_ids,
            self.shared_reference_ids,
            self.demand_ids,
            self.foreign_reference_ids,
            self.lane_order,
        )
        if any(len(set(values)) != len(values) for values in unique_fields):
            raise ValueError("convergence plan contains duplicate membership")
        if not self.convergence_ids or not self.connector_ids or not self.member_ids:
            raise ValueError("convergence plan requires complete semantic membership")
        path_edges = tuple(
            dict.fromkeys(edge for path in self.resolved_member_paths for edge in path)
        )
        if path_edges != self.resolved_member_edges:
            raise ValueError("convergence resolved edge membership is not canonical")
        landing_ids = tuple(item.member_id for item in self.landings)
        continuation_ids = tuple(item.member_id for item in self.outgoing_continuations)
        ownership_ids = tuple(item.member_id for item in self.endpoint_ownership)
        if len(set(landing_ids)) != len(landing_ids):
            raise ValueError("convergence plan repeats a feeder landing")
        if tuple(item.order for item in self.landings) != tuple(
            range(len(self.landings))
        ):
            raise ValueError("convergence feeder order is not canonical")
        if planned and (
            set(ownership_ids) != set(self.member_ids)
            or len(ownership_ids) != len(self.member_ids)
        ):
            raise ValueError("convergence endpoint ownership is incomplete")
        if not set((*landing_ids, *continuation_ids)).issubset(self.member_ids):
            raise ValueError("convergence geometry lies outside member ownership")
        lane_rank_by_line = {
            line_id: rank for rank, line_id in enumerate(self.lane_order)
        }
        landing_lane_order_mismatch = any(
            item.edge.line_id not in lane_rank_by_line
            or item.lane_rank != lane_rank_by_line[item.edge.line_id]
            for item in self.landings
        )
        continuation_lane_order_mismatch = any(
            item.edge.line_id not in lane_rank_by_line
            or item.lane_rank != lane_rank_by_line[item.edge.line_id]
            for item in self.outgoing_continuations
        )
        if planned and (
            not set(self.line_ids).issubset(lane_rank_by_line)
            or landing_lane_order_mismatch
            or continuation_lane_order_mismatch
        ):
            raise ValueError("convergence lane order is inconsistent")
        if any(item.edge not in self.resolved_member_edges for item in self.landings):
            raise ValueError("convergence landing lies outside resolved membership")
        if any(
            item.edge not in self.resolved_member_edges
            for item in self.outgoing_continuations
        ):
            raise ValueError(
                "convergence continuation lies outside resolved membership"
            )
        if any(
            item.edge not in self.resolved_member_edges
            for item in self.endpoint_ownership
        ):
            raise ValueError("convergence endpoint lies outside resolved membership")
        if any(
            not set(item.connector_ids).issubset(self.connector_ids)
            for item in self.endpoint_ownership
        ):
            raise ValueError("convergence endpoint connector lies outside membership")
        if planned:
            if (
                self.primary_trunk_member_id is None
                or self.primary_trunk_reason is None
                or self.trunk_axis is None
                or not self.landings
                or not self.outgoing_continuations
                or len(self.shared_reference_ids) != 2
                or not self.demand_ids
                or self.legacy_reason is not None
            ):
                raise ValueError("planned convergence geometry is incomplete")
            if self.primary_trunk_member_id not in self.member_ids:
                raise ValueError("convergence primary trunk lies outside membership")
        elif any(
            (
                self.primary_trunk_member_id is not None,
                self.primary_trunk_reason is not None,
                self.trunk_axis is not None,
                bool(self.landings),
                bool(self.outgoing_continuations),
                bool(self.lane_order),
                bool(self.endpoint_ownership),
                bool(self.shared_reference_ids),
                bool(self.demand_ids),
                bool(self.foreign_reference_ids),
                self.legacy_reason is None,
            )
        ):
            raise ValueError("legacy convergence owns geometry or lacks a reason")

    @property
    def owns_geometry(self) -> bool:
        return self.disposition is ConvergenceDisposition.PLANNED


@dataclass(frozen=True, slots=True)
class RouteSystem:
    """One maximal semantically coupled authored connector component."""

    id: RouteSystemId
    connector_ids: tuple[ConnectorId, ...]
    line_ids: tuple[str, ...]
    bundle_ids: tuple[BundleId, ...]
    exit_group_ids: tuple[EndpointGroupId, ...]
    entry_group_ids: tuple[EndpointGroupId, ...]
    divergence_ids: tuple[DivergenceId, ...]
    convergence_ids: tuple[ConvergenceId, ...]
    member_ids: tuple[EmissionMemberId, ...]
    branch_ids: tuple[RouteBranchId, ...]
    feeder_ids: tuple[RouteFeederId, ...]
    exit_turn_plan_ids: tuple[ExitTurnPlanId, ...]
    fan_plan_ids: tuple[FanPlanId, ...]
    convergence_plan_ids: tuple[ConvergencePlanId, ...]
    member_geometry_plan_ids: tuple[RouteMemberGeometryPlanId, ...]
    shared_reference_ids: tuple[SharedReferenceId, ...]
    demand_ids: tuple[DemandId, ...]
    reservation_ids: tuple[RouteReservationId, ...]
    disposition: RouteSystemDisposition
    compatibility_reasons: tuple[RouteSystemCompatibilityReason, ...]

    def __post_init__(self) -> None:
        compatible = self.disposition is RouteSystemDisposition.COMPATIBILITY
        if compatible != bool(self.compatibility_reasons):
            raise ValueError("route-system disposition and compatibility disagree")
        if len(set(self.compatibility_reasons)) != len(self.compatibility_reasons):
            raise ValueError("route-system compatibility reasons are not unique")


@dataclass(frozen=True, slots=True)
class RouteSystemCompatibilityReason:
    """One deterministic reason a complete system uses compatibility emission."""

    owner: str
    reason: str
    justification: str
    follow_up: str

    def __post_init__(self) -> None:
        if not all((self.owner, self.reason, self.justification, self.follow_up)):
            raise ValueError("route-system compatibility reason is incomplete")
        if self.reason not in ROUTE_SYSTEM_COMPATIBILITY_REASONS.get(self.owner, ()):
            raise ValueError(
                f"unregistered compatibility reason {self.owner}:{self.reason}"
            )
        if (
            self.justification != ROUTE_SYSTEM_COMPATIBILITY_JUSTIFICATION
            or self.follow_up
            != route_system_compatibility_follow_up(self.owner, self.reason)
        ):
            raise ValueError("route-system compatibility metadata is not canonical")


@dataclass(frozen=True, slots=True)
class EmissionBinding:
    """Final observational binding for one emission member."""

    member_id: EmissionMemberId
    kind: BindingKind
    path_id: EmittedPathId | None = None
    path_rank: int | None = None
    covering_member_id: EmissionMemberId | None = None
    coverage_reason: CoverageReason | None = None

    def __post_init__(self) -> None:
        emitted = self.kind is BindingKind.EMITTED
        covered = self.kind in {
            BindingKind.MERGE_SKIP,
            BindingKind.COVERED_MERGE_HOP,
        }
        if emitted:
            valid = (
                self.path_id is not None
                and self.path_rank is not None
                and self.path_rank >= 0
                and self.covering_member_id is None
                and self.coverage_reason is None
            )
        elif covered:
            valid = (
                self.path_id is None
                and self.path_rank is None
                and self.covering_member_id is not None
                and self.coverage_reason is not None
            )
        else:
            valid = (
                self.path_id is None
                and self.path_rank is None
                and self.covering_member_id is None
                and self.coverage_reason is None
            )
        if not valid:
            raise ValueError(f"invalid {self.kind.value} emission binding")


@dataclass(frozen=True, slots=True)
class RoutePlanDiagnostic:
    member_id: EmissionMemberId | None
    code: str
    message: str
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class RoutePlan:
    systems: tuple[RouteSystem, ...]
    endpoint_groups: tuple[ResolvedEndpointGroup, ...]
    divergences: tuple[RouteDivergence, ...]
    convergences: tuple[RouteConvergence, ...]
    members: tuple[EmissionMember, ...]
    branches: tuple[RouteBranch, ...]
    feeders: tuple[RouteFeeder, ...]
    exit_turn_plans: tuple[ExitTurnPlan, ...]
    fan_plans: tuple[FanPlan, ...]
    convergence_plans: tuple[ConvergencePlan, ...]
    member_geometry_plans: tuple[RouteMemberGeometryPlan, ...]
    shared_references: tuple[SharedReference, ...]
    demands: tuple[SymbolicDemand, ...]
    reservations: tuple[RouteReservation, ...]
    realised_reservations: tuple[RealisedRouteReservation, ...]
    reservation_diagnostics: tuple[RouteReservationDiagnostic, ...]
    bindings: tuple[EmissionBinding, ...]
    provenance: RoutePlanProvenance
    diagnostics: tuple[RoutePlanDiagnostic, ...] = ()


@dataclass(slots=True)
class RouteObservation:
    """Mutable route output paired with an immutable context-local plan."""

    routes: list[RoutedPath]
    plan: RoutePlan


def _ordered_unique(values: Iterable[_T]) -> tuple[_T, ...]:
    seen: set[_T] = set()
    result: list[_T] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _inter_section_leg(graph: MetroGraph, edge: ResolvedEdge) -> bool:
    source = graph.stations.get(edge.source)
    target = graph.stations.get(edge.target)
    if source is None or target is None:
        return False
    junction_ids = graph.junction_ids
    return (source.is_port or edge.source in junction_ids) and (
        target.is_port or edge.target in junction_ids
    )


def _resolved_member_refs(
    graph: MetroGraph,
    topology: RouteTopology,
    query: RouteTopologyQuery,
) -> tuple[
    dict[ResolvedEdge, list[ConnectorLegRef]],
    tuple[ResolvedEdge, ...],
]:
    refs_by_edge: dict[ResolvedEdge, list[ConnectorLegRef]] = defaultdict(list)
    edge_order: list[ResolvedEdge] = []
    for connector in topology.connectors:
        for path_rank, path in enumerate(query.resolved_paths(connector.id)):
            for leg_rank, edge in enumerate(path):
                if not _inter_section_leg(graph, edge):
                    continue
                if edge not in refs_by_edge:
                    edge_order.append(edge)
                refs_by_edge[edge].append(
                    ConnectorLegRef(connector.id, path_rank, leg_rank)
                )
    return refs_by_edge, tuple(edge_order)


def _semantic_components(
    topology: RouteTopology,
    refs_by_edge: Mapping[ResolvedEdge, list[ConnectorLegRef]],
    coupled_connector_groups: tuple[tuple[ConnectorId, ...], ...] = (),
) -> tuple[tuple[ConnectorId, ...], ...]:
    ordered_ids = tuple(connector.id for connector in topology.connectors)
    parent = {connector_id: connector_id for connector_id in ordered_ids}
    rank = {connector_id: index for index, connector_id in enumerate(ordered_ids)}

    def root(connector_id: ConnectorId) -> ConnectorId:
        while parent[connector_id] != connector_id:
            parent[connector_id] = parent[parent[connector_id]]
            connector_id = parent[connector_id]
        return connector_id

    def join(connector_ids: tuple[ConnectorId, ...]) -> None:
        if not connector_ids:
            return
        winner = min((root(item) for item in connector_ids), key=rank.__getitem__)
        for connector_id in connector_ids:
            parent[root(connector_id)] = winner

    for records in (
        topology.bundles,
        topology.exit_groups,
        topology.entry_groups,
        topology.divergences,
        topology.convergences,
    ):
        for record in records:
            join(record.connector_ids)

    for refs in refs_by_edge.values():
        join(_ordered_unique(ref.connector_id for ref in refs))

    for connector_ids in coupled_connector_groups:
        if any(connector_id not in parent for connector_id in connector_ids):
            raise ValueError("route-system coupling names an unknown connector")
        join(connector_ids)

    members: dict[ConnectorId, list[ConnectorId]] = defaultdict(list)
    for connector_id in ordered_ids:
        members[root(connector_id)].append(connector_id)
    return tuple(tuple(values) for values in members.values())


def _endpoint_fact(graph: MetroGraph, station_id: str) -> EndpointFact:
    station = graph.stations[station_id]
    port = graph.ports.get(station_id)
    section_id = port.section_id if port is not None else station.section_id
    section = graph.sections.get(section_id) if section_id is not None else None
    column = section.grid_col if section is not None and section.grid_col >= 0 else None
    row = section.grid_row if section is not None and section.grid_row >= 0 else None
    return EndpointFact(
        station_id=station_id,
        section_id=section_id,
        port_id=station_id if port is not None else None,
        side=port.side if port is not None else None,
        column=column,
        row=row,
        coordinate_regime=CoordinateRegime.SETTLED_GRID,
    )


def _plan_provenance(
    graph: MetroGraph, connectors: tuple[RouteConnector, ...]
) -> RoutePlanProvenance:
    provenance = graph.layout_provenance
    sections = tuple(
        SectionDecisionFacts(
            section_id,
            provenance.grid_decision(section_id),
            provenance.direction_decision(section_id),
        )
        for section_id in graph.sections
    )
    connector_facts = tuple(
        ConnectorDecisionFacts(
            connector.id,
            connector.line_id,
            connector.bundle_id,
            provenance.endpoint_decision(
                provenance.endpoint_key(connector.id, ConnectorEndpointRole.EXIT)
            ),
            provenance.endpoint_decision(
                provenance.endpoint_key(connector.id, ConnectorEndpointRole.ENTRY)
            ),
        )
        for connector in connectors
    )
    fold_source = (
        provenance.authored.fold_threshold.selected_source
        if provenance.authored is not None
        else FoldThresholdSource.DEFAULT
    )
    line_order = provenance.line_order_decision
    if line_order is None:
        raise ValueError("line-order provenance was not captured")
    line_source = (
        provenance.authored.line_order.selected_source
        if provenance.authored is not None
        else LineOrderSource.DEFAULT
    )
    return RoutePlanProvenance(
        sections,
        connector_facts,
        provenance.fold_threshold_decision,
        fold_source,
        LaneOrderFacts(
            line_order,
            line_source,
            tuple(graph.lines),
        ),
    )


def reservation_decision_refs(
    provenance: RoutePlanProvenance,
    connector_ids: tuple[ConnectorId, ...],
    span: GridSpan,
) -> tuple[ReservationDecisionRef, ...]:
    """Return the settled decisions governing one complete geometry claim."""
    records: list[ReservationDecisionRef] = []
    for section_fact in provenance.sections:
        grid = section_fact.grid
        if grid is None:
            continue
        column, row, row_span, column_span = grid.value
        if not (
            span.min_column <= column + column_span - 1
            and column <= span.max_column
            and span.min_row <= row + row_span - 1
            and row <= span.max_row
        ):
            continue
        for kind, decision in (
            (ReservationDecisionKind.SECTION_GRID, section_fact.grid),
            (ReservationDecisionKind.SECTION_DIRECTION, section_fact.direction),
        ):
            if decision is not None:
                records.append(
                    ReservationDecisionRef(kind, section_fact.section_id, decision)
                )
    connector_set = set(connector_ids)
    for connector_fact in provenance.connectors:
        if connector_fact.connector_id not in connector_set:
            continue
        for role, side_decision in (
            (ConnectorEndpointRole.EXIT, connector_fact.exit_side),
            (ConnectorEndpointRole.ENTRY, connector_fact.entry_side),
        ):
            if side_decision is not None:
                records.append(
                    ReservationDecisionRef(
                        ReservationDecisionKind.CONNECTOR_SIDE,
                        str(connector_fact.connector_id),
                        side_decision,
                        role,
                    )
                )
    if provenance.fold_threshold is not None:
        records.append(
            ReservationDecisionRef(
                ReservationDecisionKind.FOLD_THRESHOLD,
                "layout",
                provenance.fold_threshold,
            )
        )
    records.append(
        ReservationDecisionRef(
            ReservationDecisionKind.LANE_ORDER,
            "line-order",
            provenance.lane_order.policy,
        )
    )
    return tuple(records)


@dataclass(slots=True)
class RoutePlanObserver:
    """Transient route-plan collector attached to one routing invocation."""

    graph: MetroGraph
    context: _RoutingCtx | None
    scaffold: RouteSemanticScaffold | None = None
    route_systems: RouteSystemEmissionExecution | None = None
    exit_turn_plans: tuple[ExitTurnPlan, ...] = ()
    exit_turn_references: tuple[SharedReference, ...] = ()
    exit_turn_demands: tuple[SymbolicDemand, ...] = ()
    exit_turn_diagnostics: tuple[RoutePlanDiagnostic, ...] = ()
    convergence_plans: tuple[ConvergencePlan, ...] = ()
    convergence_references: tuple[SharedReference, ...] = ()
    convergence_demands: tuple[SymbolicDemand, ...] = ()
    convergence_diagnostics: tuple[RoutePlanDiagnostic, ...] = ()
    member_geometry_plans: tuple[RouteMemberGeometryPlan, ...] = ()
    _family_by_edge: dict[_EdgeKey, RouteFamilyId] = field(default_factory=dict)
    _merge_skips: dict[_EdgeKey, _EdgeKey | None] = field(default_factory=dict)
    _covered_hops: dict[_EdgeKey, _EdgeKey | None] = field(default_factory=dict)

    def record_dispatch(self, edge: _EdgeKey, family_id: RouteFamilyId) -> None:
        self._family_by_edge[edge] = family_id

    def record_rail_routes(self, routes: Iterable[RoutedPath]) -> None:
        for route in routes:
            self._family_by_edge[
                (route.edge.source, route.edge.target, route.line_id)
            ] = RouteFamilyId.RAIL_INTER_SECTION

    def record_merge_skip(self, edge: _EdgeKey, covering_edge: _EdgeKey | None) -> None:
        self._merge_skips[edge] = covering_edge

    def covering_edge(self, edge: _EdgeKey) -> _EdgeKey | None:
        """Return the merge-trunk member that covers one entry hop."""
        if self.context is None:
            return None
        return _covering_edge(self.context, edge)

    def record_covered_merge_hops(
        self, records: tuple[tuple[_EdgeKey, _EdgeKey | None], ...]
    ) -> None:
        self._covered_hops.update(records)

    def finish(self, routes: list[RoutedPath]) -> RoutePlan:
        return _build_route_plan(self, routes)


def _covering_edge(context: _RoutingCtx, edge: _EdgeKey) -> _EdgeKey | None:
    source, _target, line_id = edge
    trunk_source = context.merge.trunk_source.get(source)
    if trunk_source is None:
        return None
    return trunk_source, source, line_id


def build_route_plan_observer(
    graph: MetroGraph,
    context: _RoutingCtx | None,
    *,
    scaffold: RouteSemanticScaffold | None = None,
    route_systems: RouteSystemEmissionExecution | None = None,
    exit_turn_plans: tuple[ExitTurnPlan, ...] = (),
    exit_turn_references: tuple[SharedReference, ...] = (),
    exit_turn_demands: tuple[SymbolicDemand, ...] = (),
    exit_turn_diagnostics: tuple[RoutePlanDiagnostic, ...] = (),
    convergence_plans: tuple[ConvergencePlan, ...] = (),
    convergence_references: tuple[SharedReference, ...] = (),
    convergence_demands: tuple[SymbolicDemand, ...] = (),
    convergence_diagnostics: tuple[RoutePlanDiagnostic, ...] = (),
    member_geometry_plans: tuple[RouteMemberGeometryPlan, ...] = (),
) -> RoutePlanObserver:
    """Create one transient observer after settled routing context construction."""
    return RoutePlanObserver(
        graph=graph,
        context=context,
        scaffold=scaffold,
        route_systems=route_systems,
        exit_turn_plans=exit_turn_plans,
        exit_turn_references=exit_turn_references,
        exit_turn_demands=exit_turn_demands,
        exit_turn_diagnostics=exit_turn_diagnostics,
        convergence_plans=convergence_plans,
        convergence_references=convergence_references,
        convergence_demands=convergence_demands,
        convergence_diagnostics=convergence_diagnostics,
        member_geometry_plans=member_geometry_plans,
    )


def _member_roles(
    graph: MetroGraph,
    edge: ResolvedEdge,
    family: RouteFamilyId | None,
) -> tuple[EmissionRole, ...]:
    roles: set[EmissionRole] = set()
    if (
        family
        in {
            RouteFamilyId.BYPASS_FAMILY,
            RouteFamilyId.RIGHT_ENTRY_PLOUGH_BYPASS,
        }
        or is_bypass_v(edge.source)
        or is_bypass_v(edge.target)
    ):
        roles.add(EmissionRole.BYPASS)
    target_port = graph.ports.get(edge.target)
    if target_port is not None and target_port.is_entry:
        roles.add(EmissionRole.TERMINAL)
    return tuple(role for role in EmissionRole if role in roles)


@dataclass(frozen=True, slots=True)
class _ResolutionRecords:
    endpoint_groups: tuple[ResolvedEndpointGroup, ...]
    divergences: tuple[RouteDivergence, ...]
    convergences: tuple[RouteConvergence, ...]
    exit_group_ids_by_system: Mapping[RouteSystemId, tuple[EndpointGroupId, ...]]
    entry_group_ids_by_system: Mapping[RouteSystemId, tuple[EndpointGroupId, ...]]
    divergence_ids_by_system: Mapping[RouteSystemId, tuple[DivergenceId, ...]]
    convergence_ids_by_system: Mapping[RouteSystemId, tuple[ConvergenceId, ...]]
    divergence_ids_by_connector: Mapping[ConnectorId, tuple[DivergenceId, ...]]
    convergence_ids_by_connector: Mapping[ConnectorId, tuple[ConvergenceId, ...]]


def _build_resolution_records(
    topology: RouteTopology,
    query: RouteTopologyQuery,
    system_for: Callable[[tuple[ConnectorId, ...]], RouteSystemId],
) -> _ResolutionRecords:
    endpoint_groups: list[ResolvedEndpointGroup] = []
    exit_group_ids_by_system: dict[RouteSystemId, list[EndpointGroupId]] = defaultdict(
        list
    )
    entry_group_ids_by_system: dict[RouteSystemId, list[EndpointGroupId]] = defaultdict(
        list
    )
    for role, groups in (
        (ConnectorEndpointRole.EXIT, topology.exit_groups),
        (ConnectorEndpointRole.ENTRY, topology.entry_groups),
    ):
        for endpoint_group in groups:
            system_id = system_for(endpoint_group.connector_ids)
            port_id = (
                query.exit_port(endpoint_group.id)
                if role is ConnectorEndpointRole.EXIT
                else query.entry_port(endpoint_group.id)
            )
            endpoint_groups.append(
                ResolvedEndpointGroup(
                    id=endpoint_group.id,
                    system_id=system_id,
                    role=role,
                    section_id=endpoint_group.section_id,
                    side=endpoint_group.side,
                    port_id=port_id,
                    connector_ids=endpoint_group.connector_ids,
                )
            )
            target = (
                exit_group_ids_by_system
                if role is ConnectorEndpointRole.EXIT
                else entry_group_ids_by_system
            )
            target[system_id].append(endpoint_group.id)

    divergences: list[RouteDivergence] = []
    divergence_ids_by_system: dict[RouteSystemId, list[DivergenceId]] = defaultdict(
        list
    )
    divergence_ids_by_connector: dict[ConnectorId, list[DivergenceId]] = defaultdict(
        list
    )
    for divergence_view in query.divergences:
        divergence_group = divergence_view.group
        system_id = system_for(divergence_group.connector_ids)
        divergences.append(
            RouteDivergence(
                id=divergence_group.id,
                system_id=system_id,
                junction_id=divergence_view.junction_id,
                exit_group_id=divergence_group.exit_group_id,
                entry_group_ids=divergence_group.entry_group_ids,
                connector_ids=divergence_group.connector_ids,
            )
        )
        divergence_ids_by_system[system_id].append(divergence_group.id)
        for connector_id in divergence_group.connector_ids:
            divergence_ids_by_connector[connector_id].append(divergence_group.id)

    convergences: list[RouteConvergence] = []
    convergence_ids_by_system: dict[RouteSystemId, list[ConvergenceId]] = defaultdict(
        list
    )
    convergence_ids_by_connector: dict[ConnectorId, list[ConvergenceId]] = defaultdict(
        list
    )
    for convergence_view in query.convergences:
        convergence_group = convergence_view.group
        system_id = system_for(convergence_group.connector_ids)
        convergences.append(
            RouteConvergence(
                id=convergence_group.id,
                system_id=system_id,
                junction_id=convergence_view.junction_id,
                entry_group_id=convergence_group.entry_group_id,
                source_junction_ids=convergence_view.source_junction_ids,
                divergence_ids=convergence_group.divergence_ids,
                connector_ids=convergence_group.connector_ids,
                line_id=convergence_group.line_id,
            )
        )
        convergence_ids_by_system[system_id].append(convergence_group.id)
        for connector_id in convergence_group.connector_ids:
            convergence_ids_by_connector[connector_id].append(convergence_group.id)

    connector_ids = tuple(item.id for item in topology.connectors)
    system_ids = _ordered_unique(system_for((item,)) for item in connector_ids)

    return _ResolutionRecords(
        tuple(endpoint_groups),
        tuple(divergences),
        tuple(convergences),
        MappingProxyType(
            {item: tuple(exit_group_ids_by_system.get(item, ())) for item in system_ids}
        ),
        MappingProxyType(
            {
                item: tuple(entry_group_ids_by_system.get(item, ()))
                for item in system_ids
            }
        ),
        MappingProxyType(
            {item: tuple(divergence_ids_by_system.get(item, ())) for item in system_ids}
        ),
        MappingProxyType(
            {
                item: tuple(convergence_ids_by_system.get(item, ()))
                for item in system_ids
            }
        ),
        MappingProxyType(
            {
                item: tuple(divergence_ids_by_connector.get(item, ()))
                for item in connector_ids
            }
        ),
        MappingProxyType(
            {
                item: tuple(convergence_ids_by_connector.get(item, ()))
                for item in connector_ids
            }
        ),
    )


@dataclass(frozen=True, slots=True)
class RouteSemanticScaffold:
    """Canonical semantic identities shared by planning and final observation."""

    topology: RouteTopology
    query: RouteTopologyQuery
    refs_by_edge: Mapping[ResolvedEdge, tuple[ConnectorLegRef, ...]]
    edge_order: tuple[ResolvedEdge, ...]
    components: tuple[tuple[ConnectorId, ...], ...]
    ordered_system_ids: tuple[RouteSystemId, ...]
    system_by_connector: Mapping[ConnectorId, RouteSystemId]
    resolution: _ResolutionRecords
    member_id_by_edge: Mapping[ResolvedEdge, EmissionMemberId]

    def connector_ids_for_edge(self, edge: ResolvedEdge) -> tuple[ConnectorId, ...]:
        """Return the edge's connectors in canonical topology order."""
        return _ordered_unique(ref.connector_id for ref in self.refs_by_edge[edge])

    def system_for(self, connector_ids: tuple[ConnectorId, ...]) -> RouteSystemId:
        if not connector_ids:
            raise ValueError("route-plan ownership record has no connectors")
        system_id = self.system_by_connector[connector_ids[0]]
        if any(
            self.system_by_connector[item] != system_id for item in connector_ids[1:]
        ):
            raise ValueError("one topology record spans multiple route systems")
        return system_id

    def system_for_edge(self, edge: ResolvedEdge) -> RouteSystemId:
        """Return the canonical route system containing *edge*."""
        return self.system_for(self.connector_ids_for_edge(edge))


def build_route_semantic_scaffold(
    graph: MetroGraph,
    query: RouteTopologyQuery | None = None,
    *,
    coupled_connector_groups: tuple[tuple[ConnectorId, ...], ...] = (),
) -> RouteSemanticScaffold | None:
    """Build stable route-system and member identities before route emission."""
    topology = graph.route_topology
    if query is None:
        query = build_route_topology_query(graph)
    if topology is None or query is None:
        return None

    mutable_refs, edge_order = _resolved_member_refs(graph, topology, query)
    refs_by_edge = {edge: tuple(refs) for edge, refs in mutable_refs.items()}
    components = _semantic_components(
        topology,
        mutable_refs,
        coupled_connector_groups,
    )
    system_by_connector: dict[ConnectorId, RouteSystemId] = {}
    ordered_system_ids: list[RouteSystemId] = []
    for connector_ids in components:
        system_id = RouteSystemId(semantic_route_id("route-system", *connector_ids))
        ordered_system_ids.append(system_id)
        for connector_id in connector_ids:
            system_by_connector[connector_id] = system_id

    def system_for(connector_ids: tuple[ConnectorId, ...]) -> RouteSystemId:
        if not connector_ids:
            raise ValueError("route-plan ownership record has no connectors")
        system_id = system_by_connector[connector_ids[0]]
        if any(system_by_connector[item] != system_id for item in connector_ids[1:]):
            raise ValueError("one topology record spans multiple route systems")
        return system_id

    resolution = _build_resolution_records(topology, query, system_for)
    member_id_by_edge: dict[ResolvedEdge, EmissionMemberId] = {}
    for edge in edge_order:
        connector_ids = _ordered_unique(ref.connector_id for ref in refs_by_edge[edge])
        system_id = system_for(connector_ids)
        member_id_by_edge[edge] = EmissionMemberId(
            semantic_route_id(
                "emission-member", system_id, edge.source, edge.target, edge.line_id
            )
        )

    return RouteSemanticScaffold(
        topology,
        query,
        MappingProxyType(refs_by_edge),
        edge_order,
        components,
        tuple(ordered_system_ids),
        MappingProxyType(system_by_connector),
        resolution,
        MappingProxyType(member_id_by_edge),
    )


def _bind_member(
    observer: RoutePlanObserver,
    edge: ResolvedEdge,
    member_id: EmissionMemberId,
    route_ranks: list[int],
    member_id_by_edge: Mapping[ResolvedEdge, EmissionMemberId],
    family: RouteFamilyId | None,
) -> tuple[EmissionBinding, tuple[RoutePlanDiagnostic, ...]]:
    if len(route_ranks) == 1:
        rank = route_ranks[0]
        binding = EmissionBinding(
            member_id,
            BindingKind.EMITTED,
            EmittedPathId(semantic_route_id("emitted-path", member_id, rank)),
            rank,
        )
        if family is not None:
            return binding, ()
        return binding, (
            RoutePlanDiagnostic(
                member_id,
                "production-family",
                f"{edge.source}->{edge.target} ({edge.line_id}) emitted without "
                "an observed production family",
            ),
        )

    edge_key = (edge.source, edge.target, edge.line_id)
    suppression = None
    if not route_ranks and edge_key in observer._merge_skips:
        suppression = BindingKind.MERGE_SKIP, observer._merge_skips[edge_key]
    elif not route_ranks and edge_key in observer._covered_hops:
        suppression = BindingKind.COVERED_MERGE_HOP, observer._covered_hops[edge_key]
    if suppression is not None:
        kind, covering_edge = suppression
        covering_member_id = (
            member_id_by_edge.get(ResolvedEdge(*covering_edge))
            if covering_edge is not None
            else None
        )
        if covering_member_id is not None and covering_member_id != member_id:
            binding = EmissionBinding(
                member_id,
                kind,
                covering_member_id=covering_member_id,
                coverage_reason=CoverageReason.MERGE_TRUNK_COVERS_ENTRY_HOP,
            )
            if kind is not BindingKind.COVERED_MERGE_HOP or family is not None:
                return binding, ()
            return binding, (
                RoutePlanDiagnostic(
                    member_id,
                    "production-family",
                    f"{edge.source}->{edge.target} ({edge.line_id}) was removed "
                    "after dispatch without a recorded family",
                ),
            )
        return EmissionBinding(member_id, BindingKind.UNROUTED), (
            RoutePlanDiagnostic(
                member_id,
                "coverage-carrier",
                f"{edge.source}->{edge.target} ({edge.line_id}) has no resolved "
                "carrying emission member",
            ),
        )

    detail = "no final route" if not route_ranks else f"{len(route_ranks)} final routes"
    return EmissionBinding(member_id, BindingKind.UNROUTED), (
        RoutePlanDiagnostic(
            member_id,
            "emission-coverage",
            f"{edge.source}->{edge.target} ({edge.line_id}) has {detail}",
        ),
    )


def _fan_plan_span(graph: MetroGraph, fan_plan: FanPlan) -> GridSpan:
    section_ids = _ordered_unique(
        station.section_id
        for station_id in fan_plan.owned_station_ids
        if (station := graph.stations.get(station_id)) is not None
        and station.section_id is not None
    )
    if not section_ids:
        raise ValueError(f"planned fan {fan_plan.id!r} has no settled section span")
    return grid_span_for_sections(graph, section_ids)


def _build_fan_plan_resources(
    graph: MetroGraph,
    provenance: RoutePlanProvenance,
    fan_plans: tuple[FanPlan, ...],
) -> tuple[tuple[SharedReference, ...], tuple[SymbolicDemand, ...]]:
    """Publish each planned fan's relative centreline and runway claims."""
    references: list[SharedReference] = []
    demands: list[SymbolicDemand] = []

    for fan_plan in fan_plans:
        if fan_plan.disposition is not FanPlanDisposition.PLANNED:
            continue
        system_id = fan_plan.system_id
        if system_id is None:
            raise ValueError(f"route fan {fan_plan.id!r} has no canonical system")
        frame = fan_plan.frame
        reference_id = fan_plan.centreline_reference_id
        if frame is None or reference_id is None:
            raise ValueError(f"planned fan {fan_plan.id!r} has incomplete resources")
        if len(fan_plan.demand_ids) != len(fan_plan.branches) + 2:
            raise ValueError(f"planned fan {fan_plan.id!r} has incomplete runway ids")

        owner_connector_ids = fan_plan.connector_ids
        claimant_member_ids = fan_plan.member_ids
        span = _fan_plan_span(graph, fan_plan)
        claim_provenance = reservation_decision_refs(
            provenance, owner_connector_ids, span
        )
        references.append(
            SharedReference(
                reference_id,
                system_id,
                SharedReferenceKind.CENTRELINE,
                claimant_member_ids,
                CoordinateRegime.RELATIVE_FRAME,
                claim_provenance,
            )
        )

        all_line_ids = _ordered_unique(
            line_id for branch in fan_plan.branches for line_id in branch.line_ids
        )
        demand_specs: list[
            tuple[DemandId, tuple[EmissionMemberId, ...], int, float | None]
        ] = [
            (
                fan_plan.demand_ids[0],
                claimant_member_ids,
                len(all_line_ids),
                fan_plan.entry_runway,
            ),
            (
                fan_plan.demand_ids[1],
                claimant_member_ids,
                len(all_line_ids),
                fan_plan.exit_runway,
            ),
        ]
        for demand_id, branch in zip(
            fan_plan.demand_ids[2:], fan_plan.branches, strict=True
        ):
            demand_specs.append(
                (
                    demand_id,
                    branch.member_ids,
                    len(set(branch.line_ids)),
                    branch.diagonal_runway,
                )
            )

        for demand_id, claimants, lane_count, minimum_size in demand_specs:
            if minimum_size is None:
                raise ValueError(
                    f"planned fan demand {demand_id!r} has no runway requirement"
                )
            demands.append(
                SymbolicDemand(
                    demand_id,
                    system_id,
                    claimants,
                    DemandKind.RUNWAY,
                    DemandAxis(frame.primary.name),
                    span,
                    lane_count,
                    minimum_size,
                    CoordinateRegime.RELATIVE_FRAME,
                    (reference_id,),
                    (KeepOutClass.SECTION, KeepOutClass.MARKER),
                    claim_provenance,
                )
            )

    return tuple(references), tuple(demands)


def _fan_plan_diagnostics(
    fan_plans: tuple[FanPlan, ...],
) -> tuple[RoutePlanDiagnostic, ...]:
    return tuple(
        RoutePlanDiagnostic(
            None,
            "fan-plan-legacy",
            f"fan {fan_plan.id} uses legacy layout: {fan_plan.legacy_reason}",
            blocking=False,
        )
        for fan_plan in fan_plans
        if fan_plan.disposition is FanPlanDisposition.LEGACY
    )


def _build_route_plan(
    observer: RoutePlanObserver, routes: list[RoutedPath]
) -> RoutePlan:
    graph = observer.graph
    fan_plans = graph.fan_plans
    route_fan_plans = tuple(
        fan_plan for fan_plan in graph.fan_plans if fan_plan.system_id is not None
    )
    fan_diagnostics = _fan_plan_diagnostics(fan_plans)
    context_query = observer.context.topology if observer.context is not None else None
    scaffold = observer.scaffold or build_route_semantic_scaffold(
        graph,
        context_query,
        coupled_connector_groups=tuple(
            fan_plan.connector_ids
            for fan_plan in graph.fan_plans
            if fan_plan.connector_ids
        ),
    )
    if scaffold is None:
        return RoutePlan(
            systems=(),
            endpoint_groups=(),
            divergences=(),
            convergences=(),
            members=(),
            branches=(),
            feeders=(),
            exit_turn_plans=(),
            fan_plans=fan_plans,
            convergence_plans=(),
            member_geometry_plans=(),
            shared_references=(),
            demands=(),
            reservations=(),
            realised_reservations=(),
            reservation_diagnostics=(),
            bindings=(),
            provenance=_plan_provenance(graph, ()),
            diagnostics=fan_diagnostics,
        )

    topology = scaffold.topology
    query = scaffold.query
    refs_by_edge = scaffold.refs_by_edge
    edge_order = scaffold.edge_order
    components = scaffold.components
    ordered_system_ids = scaffold.ordered_system_ids
    system_for = scaffold.system_for

    bundle_ids_by_system: dict[RouteSystemId, list[BundleId]] = defaultdict(list)
    for bundle in topology.bundles:
        bundle_ids_by_system[system_for(bundle.connector_ids)].append(bundle.id)
    resolution = scaffold.resolution
    member_id_by_edge = scaffold.member_id_by_edge

    route_ranks: dict[ResolvedEdge, list[int]] = defaultdict(list)
    for path_rank, route in enumerate(routes):
        edge = ResolvedEdge(route.edge.source, route.edge.target, route.line_id)
        if edge in member_id_by_edge:
            route_ranks[edge].append(path_rank)

    line_rank = {line_id: rank for rank, line_id in enumerate(graph.lines)}
    diagnostics = list(fan_diagnostics)
    members: list[EmissionMember] = []
    member_ids_by_system: dict[RouteSystemId, list[EmissionMemberId]] = defaultdict(
        list
    )
    bindings: list[EmissionBinding] = []
    endpoint_facts: dict[str, EndpointFact] = {}
    for edge in edge_order:
        leg_refs = tuple(refs_by_edge[edge])
        connector_ids = scaffold.connector_ids_for_edge(edge)
        connectors = tuple(query.connector(item) for item in connector_ids)
        system_id = scaffold.system_for_edge(edge)
        member_id = member_id_by_edge[edge]
        family = observer._family_by_edge.get(edge)
        ranks = route_ranks.get(edge, [])

        for station_id in (edge.source, edge.target):
            if station_id not in endpoint_facts:
                endpoint_facts[station_id] = _endpoint_fact(graph, station_id)

        members.append(
            EmissionMember(
                id=member_id,
                system_id=system_id,
                source=endpoint_facts[edge.source],
                target=endpoint_facts[edge.target],
                line_id=edge.line_id,
                line_rank=line_rank.get(edge.line_id, len(line_rank)),
                connector_ids=connector_ids,
                leg_refs=leg_refs,
                bundle_ids=_ordered_unique(item.bundle_id for item in connectors),
                exit_group_ids=_ordered_unique(
                    item.exit_group_id for item in connectors
                ),
                entry_group_ids=_ordered_unique(
                    item.entry_group_id for item in connectors
                ),
                divergence_ids=_ordered_unique(
                    item
                    for connector_id in connector_ids
                    for item in resolution.divergence_ids_by_connector[connector_id]
                ),
                convergence_ids=_ordered_unique(
                    item
                    for connector_id in connector_ids
                    for item in resolution.convergence_ids_by_connector[connector_id]
                ),
                roles=_member_roles(graph, edge, family),
                family_id=family,
            )
        )
        member_ids_by_system[system_id].append(member_id)
        binding, binding_diagnostics = _bind_member(
            observer,
            edge,
            member_id,
            ranks,
            member_id_by_edge,
            family,
        )
        bindings.append(binding)
        diagnostics.extend(binding_diagnostics)

    branches: list[RouteBranch] = []
    branch_ids_by_system: dict[RouteSystemId, list[RouteBranchId]] = defaultdict(list)
    for divergence in topology.divergences:
        connectors_by_entry: dict[EndpointGroupId, list[ConnectorId]] = defaultdict(
            list
        )
        for connector_id in divergence.connector_ids:
            entry_group_id = query.connector(connector_id).entry_group_id
            connectors_by_entry[entry_group_id].append(connector_id)
        for entry_group_id in divergence.entry_group_ids:
            connector_ids = tuple(connectors_by_entry[entry_group_id])
            system_id = system_for(connector_ids)
            branch_id = RouteBranchId(
                semantic_route_id(
                    "route-branch", system_id, divergence.id, entry_group_id
                )
            )
            branches.append(
                RouteBranch(
                    branch_id,
                    system_id,
                    divergence.id,
                    entry_group_id,
                    connector_ids,
                    _ordered_unique(
                        query.connector(item).line_id for item in connector_ids
                    ),
                )
            )
            branch_ids_by_system[system_id].append(branch_id)
    feeders: list[RouteFeeder] = []
    feeder_ids_by_system: dict[RouteSystemId, list[RouteFeederId]] = defaultdict(list)
    for convergence in topology.convergences:
        connectors_by_divergence: dict[DivergenceId, list[ConnectorId]] = defaultdict(
            list
        )
        convergence_divergences = set(convergence.divergence_ids)
        for connector_id in convergence.connector_ids:
            for divergence_id in resolution.divergence_ids_by_connector[connector_id]:
                if divergence_id in convergence_divergences:
                    connectors_by_divergence[divergence_id].append(connector_id)
        for divergence_id in convergence.divergence_ids:
            connector_ids = tuple(connectors_by_divergence[divergence_id])
            system_id = system_for(connector_ids)
            feeder_id = RouteFeederId(
                semantic_route_id(
                    "route-feeder",
                    system_id,
                    convergence.id,
                    divergence_id,
                )
            )
            feeders.append(
                RouteFeeder(
                    feeder_id,
                    system_id,
                    convergence.id,
                    divergence_id,
                    connector_ids,
                    _ordered_unique(
                        query.connector(item).line_id for item in connector_ids
                    ),
                )
            )
            feeder_ids_by_system[system_id].append(feeder_id)

    exit_turn_ids_by_system: dict[RouteSystemId, list[ExitTurnPlanId]] = defaultdict(
        list
    )
    for exit_turn_plan in observer.exit_turn_plans:
        exit_turn_ids_by_system[exit_turn_plan.system_id].append(exit_turn_plan.id)
    fan_ids_by_system: dict[RouteSystemId, list[FanPlanId]] = defaultdict(list)
    for fan_plan in route_fan_plans:
        fan_system_id = fan_plan.system_id
        if fan_system_id is None:
            raise ValueError(f"route fan {fan_plan.id!r} has no canonical system")
        fan_ids_by_system[fan_system_id].append(fan_plan.id)
    convergence_plan_ids_by_system: dict[RouteSystemId, list[ConvergencePlanId]] = (
        defaultdict(list)
    )
    for convergence_plan in observer.convergence_plans:
        convergence_plan_ids_by_system[convergence_plan.system_id].append(
            convergence_plan.id
        )

    system_emission = observer.route_systems
    if system_emission is None and observer.context is not None:
        system_emission = observer.context.route_systems
    if system_emission is None:
        from nf_metro.layout.routing.system_emission import (
            build_route_system_emission_execution,
        )

        system_emission = build_route_system_emission_execution(
            scaffold,
            exit_turn_plans=observer.exit_turn_plans,
            fan_plans=route_fan_plans,
            convergence_plans=observer.convergence_plans,
        )
    emission_by_system = {item.system_id: item for item in system_emission.systems}
    planned_system_ids = {
        item.system_id
        for item in system_emission.systems
        if item.disposition is RouteSystemDisposition.PLANNED
    }
    provenance = _plan_provenance(graph, topology.connectors)
    fan_references, fan_demands = _build_fan_plan_resources(
        graph,
        provenance,
        tuple(plan for plan in route_fan_plans if plan.system_id in planned_system_ids),
    )
    shared_references = (
        *observer.exit_turn_references,
        *fan_references,
        *observer.convergence_references,
    )
    demands = (
        *observer.exit_turn_demands,
        *fan_demands,
        *observer.convergence_demands,
    )
    member_geometry_plans = tuple(
        plan
        for plan in observer.member_geometry_plans
        if plan.system_id in planned_system_ids
    )
    member_geometry_plan_ids_by_system: dict[
        RouteSystemId, list[RouteMemberGeometryPlanId]
    ] = defaultdict(list)
    reference_ids_by_system: dict[RouteSystemId, list[SharedReferenceId]] = defaultdict(
        list
    )
    demand_ids_by_system: dict[RouteSystemId, list[DemandId]] = defaultdict(list)
    for reference in shared_references:
        reference_ids_by_system[reference.system_id].append(reference.id)
    for demand in demands:
        demand_ids_by_system[demand.system_id].append(demand.id)
    for member_geometry_plan in member_geometry_plans:
        member_geometry_plan_ids_by_system[member_geometry_plan.system_id].append(
            member_geometry_plan.id
        )

    systems: list[RouteSystem] = []
    for system_id, connector_ids in zip(ordered_system_ids, components, strict=True):
        emission = emission_by_system[system_id]
        systems.append(
            RouteSystem(
                system_id,
                connector_ids,
                _ordered_unique(
                    query.connector(connector_id).line_id
                    for connector_id in connector_ids
                ),
                tuple(bundle_ids_by_system[system_id]),
                tuple(resolution.exit_group_ids_by_system[system_id]),
                tuple(resolution.entry_group_ids_by_system[system_id]),
                tuple(resolution.divergence_ids_by_system[system_id]),
                tuple(resolution.convergence_ids_by_system[system_id]),
                tuple(member_ids_by_system[system_id]),
                tuple(branch_ids_by_system[system_id]),
                tuple(feeder_ids_by_system[system_id]),
                tuple(exit_turn_ids_by_system[system_id]),
                tuple(fan_ids_by_system[system_id]),
                tuple(convergence_plan_ids_by_system[system_id]),
                tuple(member_geometry_plan_ids_by_system[system_id]),
                tuple(reference_ids_by_system[system_id]),
                tuple(demand_ids_by_system[system_id]),
                (),
                emission.disposition,
                emission.compatibility_reasons,
            )
        )

    plan = RoutePlan(
        systems=tuple(systems),
        endpoint_groups=tuple(resolution.endpoint_groups),
        divergences=tuple(resolution.divergences),
        convergences=tuple(resolution.convergences),
        members=tuple(members),
        branches=tuple(branches),
        feeders=tuple(feeders),
        exit_turn_plans=observer.exit_turn_plans,
        fan_plans=fan_plans,
        convergence_plans=observer.convergence_plans,
        member_geometry_plans=member_geometry_plans,
        shared_references=shared_references,
        demands=demands,
        reservations=(),
        realised_reservations=(),
        reservation_diagnostics=(),
        bindings=tuple(bindings),
        provenance=provenance,
        diagnostics=(
            tuple(diagnostics)
            + observer.exit_turn_diagnostics
            + observer.convergence_diagnostics
        ),
    )
    from nf_metro.layout.route_reservations import attach_route_reservations

    return attach_route_reservations(
        plan,
        graph,
        routes,
        observer.context.station_offsets if observer.context is not None else None,
    )


@dataclass(frozen=True, slots=True)
class RoutePlanQuery:
    """Transient read-only indexes over canonical route-plan tuples."""

    plan: RoutePlan
    _endpoint_groups: Mapping[EndpointGroupId, ResolvedEndpointGroup]
    _divergences: Mapping[DivergenceId, RouteDivergence]
    _convergences: Mapping[ConvergenceId, RouteConvergence]
    _members: Mapping[EmissionMemberId, EmissionMember]
    _bindings: Mapping[EmissionMemberId, tuple[EmissionBinding, ...]]
    _exit_turn_plans: Mapping[ExitTurnPlanId, ExitTurnPlan]
    _exit_turns_by_source: Mapping[str, tuple[ExitTurnPlan, ...]]
    _exit_turns_by_member: Mapping[EmissionMemberId, tuple[ExitTurnPlan, ...]]
    _fan_plans: Mapping[FanPlanId, FanPlan]
    _fan_plans_by_system: Mapping[RouteSystemId, tuple[FanPlan, ...]]
    _fan_plans_by_member: Mapping[EmissionMemberId, tuple[FanPlan, ...]]
    _convergence_plans: Mapping[ConvergencePlanId, ConvergencePlan]
    _convergence_plans_by_system: Mapping[RouteSystemId, tuple[ConvergencePlan, ...]]
    _convergence_plans_by_convergence: Mapping[
        ConvergenceId, tuple[ConvergencePlan, ...]
    ]
    _convergence_plans_by_connector: Mapping[ConnectorId, tuple[ConvergencePlan, ...]]
    _convergence_plans_by_member: Mapping[EmissionMemberId, tuple[ConvergencePlan, ...]]
    _convergence_plans_by_path: Mapping[
        tuple[ResolvedEdge, ...], tuple[ConvergencePlan, ...]
    ]
    _shared_references: Mapping[SharedReferenceId, SharedReference]
    _demands: Mapping[DemandId, SymbolicDemand]
    _reservations: Mapping[RouteReservationId, RouteReservation]
    _realisations: Mapping[RouteReservationId, RealisedRouteReservation]
    _reservations_by_system: Mapping[RouteSystemId, tuple[RouteReservation, ...]]
    _reservations_by_member: Mapping[EmissionMemberId, tuple[RouteReservation, ...]]

    def endpoint_group(self, group_id: EndpointGroupId) -> ResolvedEndpointGroup:
        return self._endpoint_groups[group_id]

    def divergence(self, divergence_id: DivergenceId) -> RouteDivergence:
        return self._divergences[divergence_id]

    def convergence(self, convergence_id: ConvergenceId) -> RouteConvergence:
        return self._convergences[convergence_id]

    def member(self, member_id: EmissionMemberId) -> EmissionMember:
        return self._members[member_id]

    def bindings_for(self, member_id: EmissionMemberId) -> tuple[EmissionBinding, ...]:
        return self._bindings.get(member_id, ())

    def exit_turn_plan(self, plan_id: ExitTurnPlanId) -> ExitTurnPlan:
        return self._exit_turn_plans[plan_id]

    def exit_turn_plans_for_source(self, source_id: str) -> tuple[ExitTurnPlan, ...]:
        return self._exit_turns_by_source.get(source_id, ())

    def exit_turn_plans_for_member(
        self, member_id: EmissionMemberId
    ) -> tuple[ExitTurnPlan, ...]:
        return self._exit_turns_by_member.get(member_id, ())

    def fan_plan(self, plan_id: FanPlanId) -> FanPlan:
        return self._fan_plans[plan_id]

    def fan_plans_for_system(self, system_id: RouteSystemId) -> tuple[FanPlan, ...]:
        return self._fan_plans_by_system.get(system_id, ())

    def fan_plans_for_member(self, member_id: EmissionMemberId) -> tuple[FanPlan, ...]:
        return self._fan_plans_by_member.get(member_id, ())

    def convergence_plan(self, plan_id: ConvergencePlanId) -> ConvergencePlan:
        return self._convergence_plans[plan_id]

    def convergence_plans_for_system(
        self, system_id: RouteSystemId
    ) -> tuple[ConvergencePlan, ...]:
        return self._convergence_plans_by_system.get(system_id, ())

    def convergence_plans_for_convergence(
        self, convergence_id: ConvergenceId
    ) -> tuple[ConvergencePlan, ...]:
        return self._convergence_plans_by_convergence.get(convergence_id, ())

    def convergence_plans_for_connector(
        self, connector_id: ConnectorId
    ) -> tuple[ConvergencePlan, ...]:
        return self._convergence_plans_by_connector.get(connector_id, ())

    def convergence_plans_for_member(
        self, member_id: EmissionMemberId
    ) -> tuple[ConvergencePlan, ...]:
        return self._convergence_plans_by_member.get(member_id, ())

    def convergence_plans_for_resolved_path(
        self, path: tuple[ResolvedEdge, ...]
    ) -> tuple[ConvergencePlan, ...]:
        return self._convergence_plans_by_path.get(path, ())

    def shared_reference(self, reference_id: SharedReferenceId) -> SharedReference:
        return self._shared_references[reference_id]

    def demand(self, demand_id: DemandId) -> SymbolicDemand:
        return self._demands[demand_id]

    def reservation(self, reservation_id: RouteReservationId) -> RouteReservation:
        return self._reservations[reservation_id]

    def realised_reservation(
        self, reservation_id: RouteReservationId
    ) -> RealisedRouteReservation | None:
        return self._realisations.get(reservation_id)

    def reservations_for_system(
        self, system_id: RouteSystemId
    ) -> tuple[RouteReservation, ...]:
        return self._reservations_by_system.get(system_id, ())

    def reservations_for_member(
        self, member_id: EmissionMemberId
    ) -> tuple[RouteReservation, ...]:
        return self._reservations_by_member.get(member_id, ())


def _validate_exit_turn_assignment(
    exit_turn_plan: ExitTurnPlan,
    assignment: ExitTurnAssignment,
    members: Mapping[EmissionMemberId, EmissionMember],
    lanes: Mapping[int, ExitSourceLane],
    axes: Mapping[ExitTurnAxisId, ExitTurnAxis],
    endpoint_groups: Mapping[EndpointGroupId, ResolvedEndpointGroup],
    section_grids: Mapping[str, GridCell],
) -> None:
    member = members[assignment.member_id]
    lane = lanes.get(assignment.source_lane_rank)
    entry_group = endpoint_groups.get(assignment.entry_group_id)
    destination_grid = (
        section_grids.get(entry_group.section_id) if entry_group is not None else None
    )
    if (
        assignment.member_id not in exit_turn_plan.member_ids
        or lane is None
        or assignment.member_id not in lane.member_ids
    ):
        raise ValueError("exit-turn assignment has inconsistent lane membership")
    if (
        entry_group is None
        or entry_group.system_id != exit_turn_plan.system_id
        or entry_group.role is not ConnectorEndpointRole.ENTRY
        or assignment.entry_group_id not in member.entry_group_ids
        or destination_grid is None
        or assignment.destination_section_id != entry_group.section_id
        or assignment.destination_column != destination_grid[0]
        or assignment.destination_row != destination_grid[1]
        or assignment.destination_side is not entry_group.side
    ):
        raise ValueError("exit-turn assignment has inconsistent destination")
    semantic_roles = set(assignment.roles) - {
        EmissionRole.CONTINUATION,
        EmissionRole.PEEL_OFF,
    }
    seam_roles = set(assignment.roles) & {
        EmissionRole.CONTINUATION,
        EmissionRole.PEEL_OFF,
    }
    expected_seam_role = (
        EmissionRole.CONTINUATION
        if assignment.turn_direction is None
        else EmissionRole.PEEL_OFF
    )
    seam_roles_are_consistent = (
        seam_roles == {expected_seam_role}
        if assignment.run_direction is not None
        else exit_turn_plan.disposition is ExitTurnDisposition.LEGACY
        and len(seam_roles) == 1
    )
    canonical_roles = tuple(
        role for role in EmissionRole if role in set(assignment.roles)
    )
    expected_handedness = (
        turn_handedness(assignment.run_direction, assignment.turn_direction)
        if assignment.turn_direction is not None
        and assignment.run_direction is not None
        else None
    )
    has_turn_requirement = (
        assignment.launch_coordinate is not None
        and assignment.minimum_runway is not None
        and math.isfinite(assignment.launch_coordinate)
        and math.isfinite(assignment.minimum_runway)
        and assignment.minimum_runway > 0
    )
    if (
        assignment.planned_family_id is not member.family_id
        or semantic_roles != set(member.roles)
        or not seam_roles_are_consistent
        or assignment.roles != canonical_roles
        or (
            assignment.run_direction not in set(Direction)
            and not (
                exit_turn_plan.disposition is ExitTurnDisposition.LEGACY
                and assignment.run_direction is None
            )
        )
        or assignment.handedness is not expected_handedness
        or (assignment.turn_direction is not None) != has_turn_requirement
    ):
        raise ValueError("exit-turn assignment has inconsistent semantics")
    axis = axes.get(assignment.axis_id) if assignment.axis_id is not None else None
    if exit_turn_plan.disposition is ExitTurnDisposition.PLANNED and (
        assignment.turn_direction is None
    ) != (axis is None):
        raise ValueError("exit-turn assignment has incomplete turn geometry")
    if (
        exit_turn_plan.disposition is ExitTurnDisposition.LEGACY
        and assignment.axis_id is not None
    ):
        raise ValueError("legacy exit-turn assignment cannot own an axis")
    if axis is not None and (
        assignment.member_id not in axis.claimant_member_ids
        or axis.line_id != lane.line_id
        or axis.rank != lane.rank
        or axis.axis is not exit_turn_plan.source_axis
        or axis.coordinate_regime is not CoordinateRegime.LAYOUT_CANVAS
    ):
        raise ValueError("exit-turn axis has inconsistent assignment geometry")


def _validate_exit_turn_demands(
    exit_turn_plan: ExitTurnPlan,
    expected_span: GridSpan,
    turning_assignment_ids: tuple[EmissionMemberId, ...],
    ordered_turn_span: float,
    demands: Mapping[DemandId, SymbolicDemand],
) -> None:
    if any(demand_id not in demands for demand_id in exit_turn_plan.demand_ids):
        raise ValueError("exit-turn plan has an unknown symbolic demand")
    owned_demands = tuple(demands[item] for item in exit_turn_plan.demand_ids)
    turn_demand_count = 2 if exit_turn_plan.axes else 0
    if len(owned_demands) != turn_demand_count + len(exit_turn_plan.lane_transitions):
        raise ValueError("exit-turn symbolic demand is inconsistent")
    if exit_turn_plan.axes:
        ordered_demand, runway_demand = owned_demands[:2]
        common_facts = (
            exit_turn_plan.system_id,
            exit_turn_plan.source_axis,
            expected_span,
            CoordinateRegime.LAYOUT_CANVAS,
            (exit_turn_plan.reference_id,),
            (KeepOutClass.SECTION, KeepOutClass.MARKER),
            exit_turn_plan.provenance,
        )

        def demand_facts(demand: SymbolicDemand) -> tuple[object, ...]:
            return (
                demand.system_id,
                demand.axis,
                demand.span,
                demand.minimum_size_regime,
                demand.ordered_reference_ids,
                demand.keep_out_classes,
                demand.provenance,
            )

        if (
            ordered_demand.kind is not DemandKind.ORDERED_TURNS
            or ordered_demand.claimant_member_ids != turning_assignment_ids
            or ordered_demand.lane_count != len(exit_turn_plan.axes)
            or ordered_demand.minimum_size != ordered_turn_span
            or demand_facts(ordered_demand) != common_facts
            or runway_demand.kind is not DemandKind.RUNWAY
            or runway_demand.claimant_member_ids != turning_assignment_ids
            or runway_demand.lane_count != len(exit_turn_plan.axes)
            or runway_demand.minimum_size != exit_turn_plan.minimum_runway
            or demand_facts(runway_demand) != common_facts
        ):
            raise ValueError("exit-turn symbolic demand is inconsistent")
    for transition, demand in zip(
        exit_turn_plan.lane_transitions,
        owned_demands[turn_demand_count:],
        strict=True,
    ):
        if (
            demand.system_id != exit_turn_plan.system_id
            or demand.claimant_member_ids != transition.claimant_member_ids
            or demand.kind is not DemandKind.RUNWAY
            or demand.axis
            is not (
                DemandAxis.X
                if transition.run_direction in {Direction.R, Direction.L}
                else DemandAxis.Y
            )
            or demand.span != expected_span
            or demand.lane_count != 1
            or demand.minimum_size
            != transition.source_runway
            + transition.diagonal_run
            + transition.target_runway
            or demand.minimum_size_regime is not CoordinateRegime.LAYOUT_CANVAS
            or demand.ordered_reference_ids
            or demand.keep_out_classes != (KeepOutClass.SECTION, KeepOutClass.MARKER)
            or demand.provenance != exit_turn_plan.provenance
        ):
            raise ValueError("exit-turn lane-transition demand is inconsistent")


def _validate_planned_exit_turn_resources(
    plan: RoutePlan,
    exit_turn_plan: ExitTurnPlan,
    exit_group: ResolvedEndpointGroup,
    members: Mapping[EmissionMemberId, EmissionMember],
    endpoint_groups: Mapping[EndpointGroupId, ResolvedEndpointGroup],
    section_grids: Mapping[str, GridCell],
    references: Mapping[SharedReferenceId, SharedReference],
    demands: Mapping[DemandId, SymbolicDemand],
) -> None:
    source_run_direction = exit_turn_plan.source_run_direction
    if source_run_direction not in set(Direction):
        raise ValueError("planned exit-turn plan has no source direction")
    claimed_sections = {exit_group.section_id} | {
        endpoint_groups[item.entry_group_id].section_id
        for item in exit_turn_plan.assignments
    }
    if any(item not in section_grids for item in claimed_sections):
        raise ValueError("exit-turn plan has an unknown section grid")
    claimed_cells = [section_grids[item] for item in claimed_sections]
    expected_span = GridSpan(
        min(item[0] for item in claimed_cells),
        max(item[0] + item[3] - 1 for item in claimed_cells),
        min(item[1] for item in claimed_cells),
        max(item[1] + item[2] - 1 for item in claimed_cells),
    )
    if exit_turn_plan.provenance != reservation_decision_refs(
        plan.provenance,
        exit_turn_plan.connector_ids,
        expected_span,
    ):
        raise ValueError("exit-turn plan has inconsistent provenance")
    offsets = tuple(lane.planned_offset for lane in exit_turn_plan.source_lanes)
    if any(
        abs(abs(right - left) - exit_turn_plan.spacing) > 1e-6
        for left, right in zip(offsets, offsets[1:])
    ):
        raise ValueError("planned exit-turn source lanes are not compact")
    turning_assignment_ids = tuple(
        item.member_id
        for item in exit_turn_plan.assignments
        if item.axis_id is not None
    )
    reference = (
        references.get(exit_turn_plan.reference_id)
        if exit_turn_plan.reference_id is not None
        else None
    )
    if exit_turn_plan.axes:
        if (
            reference is None
            or reference.system_id != exit_turn_plan.system_id
            or reference.kind is not SharedReferenceKind.ORDERED_TURNS
            or reference.claimant_member_ids != turning_assignment_ids
            or reference.coordinate_regime is not CoordinateRegime.LAYOUT_CANVAS
            or reference.provenance != exit_turn_plan.provenance
        ):
            raise ValueError("exit-turn shared reference is inconsistent")
    elif exit_turn_plan.reference_id is not None:
        raise ValueError("axis-free exit-turn plan has a shared reference")
    turning_assignments = tuple(
        item for item in exit_turn_plan.assignments if item.axis_id is not None
    )
    cohort_ranks: dict[tuple[Direction, Direction], set[int]] = defaultdict(set)
    for assignment in turning_assignments:
        if assignment.run_direction is None or assignment.turn_direction is None:
            raise ValueError("exit-turn assignment has incomplete directions")
        cohort_ranks[assignment.run_direction, assignment.turn_direction].add(
            assignment.source_lane_rank
        )
    ordered_turn_span = max(
        ((len(ranks) - 1) * exit_turn_plan.spacing for ranks in cohort_ranks.values()),
        default=0.0,
    )
    if any(
        member_id not in exit_turn_plan.member_ids
        or members[member_id].system_id != exit_turn_plan.system_id
        or members[member_id].line_id != transition.edge.line_id
        for transition in exit_turn_plan.lane_transitions
        for member_id in transition.claimant_member_ids
    ):
        raise ValueError("exit-turn lane transition has inconsistent claimants")
    _validate_exit_turn_demands(
        exit_turn_plan,
        expected_span,
        turning_assignment_ids,
        ordered_turn_span,
        demands,
    )
    axis_claimants = {
        axis.id: tuple(
            item.member_id
            for item in exit_turn_plan.assignments
            if item.axis_id == axis.id
        )
        for axis in exit_turn_plan.axes
    }
    if any(
        axis.claimant_member_ids != axis_claimants[axis.id]
        for axis in exit_turn_plan.axes
    ):
        raise ValueError("exit-turn axis has inconsistent claimants")
    if len(exit_turn_plan.axes) != len(
        {
            (
                assignment.run_direction,
                assignment.turn_direction,
                assignment.source_lane_rank,
            )
            for assignment in turning_assignments
        }
    ):
        raise ValueError("exit-turn axes have inconsistent cohort membership")
    if any(
        (right.input_offset - left.input_offset)
        * (right.planned_offset - left.planned_offset)
        <= 0
        or abs(abs(right.planned_offset - left.planned_offset) - exit_turn_plan.spacing)
        > 1e-6
        for left, right in zip(
            exit_turn_plan.source_lanes,
            exit_turn_plan.source_lanes[1:],
        )
    ):
        raise ValueError("exit-turn source lanes do not preserve travel order")
    axis_by_id = {axis.id: axis for axis in exit_turn_plan.axes}
    for (run_direction, turn_direction), _ranks in cohort_ranks.items():
        cohort_axes = tuple(
            axis_by_id[assignment.axis_id]
            for assignment in turning_assignments
            if assignment.run_direction is run_direction
            and assignment.turn_direction is turn_direction
            and assignment.axis_id is not None
        )
        unique_axes = tuple(dict.fromkeys(cohort_axes))
        unique_axes = tuple(sorted(unique_axes, key=lambda axis: axis.rank))
        progression = right_normal_axis_sign(turn_direction)
        if any(
            abs(
                right.coordinate
                - left.coordinate
                - progression * exit_turn_plan.spacing
            )
            > 1e-6
            for left, right in zip(unique_axes, unique_axes[1:])
        ):
            raise ValueError("exit-turn axes do not preserve planned lane spacing")
    if any(
        assignment.axis_id is not None
        and (
            assignment.run_direction is None
            or assignment.launch_coordinate is None
            or assignment.minimum_runway is None
            or (
                axis_by_id[assignment.axis_id].coordinate - assignment.launch_coordinate
            )
            * assignment.run_direction.sign
            < assignment.minimum_runway - 1e-6
        )
        for assignment in turning_assignments
    ):
        raise ValueError("exit-turn axis does not satisfy its source runway")
    if any(
        axis.fixed_anchor_id is not None
        and axis.fixed_anchor_id
        not in {
            station_id
            for member_id in axis.claimant_member_ids
            for station_id in (
                members[member_id].source.station_id,
                members[member_id].target.station_id,
            )
        }
        for axis in exit_turn_plan.axes
    ):
        raise ValueError("exit-turn axis has an inconsistent fixed anchor")


def _validate_exit_turn_identity(
    exit_turn_plan: ExitTurnPlan,
    systems: Mapping[RouteSystemId, RouteSystem],
    endpoint_groups: Mapping[EndpointGroupId, ResolvedEndpointGroup],
    divergences: Mapping[DivergenceId, RouteDivergence],
    members: Mapping[EmissionMemberId, EmissionMember],
) -> ResolvedEndpointGroup:
    system = systems.get(exit_turn_plan.system_id)
    if system is None:
        raise ValueError("exit-turn plan has an unknown route system")
    if exit_turn_plan.spacing <= 0 or exit_turn_plan.minimum_runway <= 0:
        raise ValueError("exit-turn plan has invalid geometry requirements")
    exit_group = endpoint_groups.get(exit_turn_plan.exit_group_id)
    if (
        exit_group is None
        or exit_group.system_id != exit_turn_plan.system_id
        or exit_group.role is not ConnectorEndpointRole.EXIT
        or exit_group.port_id != exit_turn_plan.exit_port_id
        or exit_group.connector_ids != exit_turn_plan.connector_ids
    ):
        raise ValueError("exit-turn plan has inconsistent exit-group ownership")
    expected_source_run_direction = (
        Direction.R
        if exit_group.side is PortSide.RIGHT
        else Direction.L
        if exit_group.side is PortSide.LEFT
        else Direction.U
        if exit_group.side is PortSide.TOP
        else Direction.D
        if exit_group.side is PortSide.BOTTOM
        else None
    )
    expected_source_axis = (
        DemandAxis.X
        if expected_source_run_direction in {Direction.R, Direction.L}
        else DemandAxis.Y
    )
    if (
        exit_turn_plan.source_run_direction is not expected_source_run_direction
        or exit_turn_plan.source_axis is not expected_source_axis
    ):
        raise ValueError("exit-turn plan has inconsistent source-run direction")
    divergence = None
    if exit_turn_plan.divergence_id is None:
        if exit_turn_plan.source_id != exit_turn_plan.exit_port_id:
            raise ValueError("exit-turn plan has an inconsistent source")
    else:
        divergence = divergences.get(exit_turn_plan.divergence_id)
        if (
            divergence is None
            or divergence.system_id != exit_turn_plan.system_id
            or divergence.exit_group_id != exit_turn_plan.exit_group_id
            or divergence.junction_id != exit_turn_plan.source_id
        ):
            raise ValueError("exit-turn plan has an inconsistent divergence")
    if not exit_turn_plan.connector_ids or any(
        item not in system.connector_ids for item in exit_turn_plan.connector_ids
    ):
        raise ValueError("exit-turn plan has inconsistent connector ownership")
    if not exit_turn_plan.member_ids or any(
        item not in members or members[item].system_id != exit_turn_plan.system_id
        for item in exit_turn_plan.member_ids
    ):
        raise ValueError("exit-turn plan has inconsistent member ownership")
    expected_member_ids = tuple(
        member.id
        for member in members.values()
        if exit_turn_plan.exit_group_id in member.exit_group_ids
        and (
            member.source.station_id == exit_turn_plan.source_id
            or (
                divergence is not None
                and member.source.station_id == exit_turn_plan.exit_port_id
                and member.target.station_id == exit_turn_plan.source_id
            )
        )
    )
    if exit_turn_plan.member_ids != expected_member_ids:
        raise ValueError("exit-turn plan does not cover its complete exit group")
    if exit_turn_plan.system_member_ids != system.member_ids:
        raise ValueError("exit-turn plan does not cover its complete route system")
    return exit_group


def _validate_exit_source_lanes(
    exit_turn_plan: ExitTurnPlan,
    members: Mapping[EmissionMemberId, EmissionMember],
    station_owners: dict[tuple[str, str], ExitTurnPlanId],
) -> tuple[dict[int, ExitSourceLane], dict[str, ExitSourceLane]]:
    if tuple(lane.rank for lane in exit_turn_plan.source_lanes) != tuple(
        range(len(exit_turn_plan.source_lanes))
    ):
        raise ValueError("exit-turn source lanes are not compactly ranked")
    if (
        exit_turn_plan.disposition is ExitTurnDisposition.PLANNED
        and exit_turn_plan.lane_order_source
        is ExitLaneOrderSource.GRAPH_LINE_ORDER_FALLBACK
    ):
        raise ValueError("planned exit-turn source order has fallback provenance")
    if len({lane.line_id for lane in exit_turn_plan.source_lanes}) != len(
        exit_turn_plan.source_lanes
    ):
        raise ValueError("exit-turn plan contains duplicate source lanes")
    lane_by_rank = {lane.rank: lane for lane in exit_turn_plan.source_lanes}
    lane_members = tuple(
        member_id
        for lane in exit_turn_plan.source_lanes
        for member_id in lane.member_ids
    )
    if Counter(lane_members) != Counter(exit_turn_plan.member_ids) or any(
        count != 1 for count in Counter(lane_members).values()
    ):
        raise ValueError("exit-turn source lanes do not partition all members")
    if any(
        members[member_id].line_id != lane.line_id
        for lane in exit_turn_plan.source_lanes
        for member_id in lane.member_ids
    ):
        raise ValueError("exit-turn source lane has inconsistent line ownership")
    for lane in exit_turn_plan.source_lanes:
        if len(set(lane.station_ids)) != len(lane.station_ids):
            raise ValueError("exit-turn source lane repeats a station owner")
        if exit_turn_plan.disposition is ExitTurnDisposition.PLANNED:
            if not lane.station_ids:
                raise ValueError("planned exit-turn source lane has no station owner")
            if (
                exit_turn_plan.exit_port_id not in lane.station_ids
                or exit_turn_plan.source_id not in lane.station_ids
            ):
                raise ValueError("planned exit-turn source lane misses its boundary")
            for station_id in lane.station_ids:
                key = (station_id, lane.line_id)
                owner = station_owners.setdefault(key, exit_turn_plan.id)
                if owner != exit_turn_plan.id:
                    raise ValueError("exit-turn station lane has more than one owner")
        elif lane.station_ids or lane.planned_offset != lane.input_offset:
            raise ValueError("legacy exit-turn source lane cannot own offsets")
    lane_by_line = {lane.line_id: lane for lane in exit_turn_plan.source_lanes}
    return lane_by_rank, lane_by_line


def _validate_exit_turn_assignments(
    exit_turn_plan: ExitTurnPlan,
    members: Mapping[EmissionMemberId, EmissionMember],
    lane_by_rank: Mapping[int, ExitSourceLane],
    endpoint_groups: Mapping[EndpointGroupId, ResolvedEndpointGroup],
    section_grids: Mapping[str, GridCell],
) -> dict[ExitTurnAxisId, ExitTurnAxis]:
    assignment_ids = {item.member_id for item in exit_turn_plan.assignments}
    if len(assignment_ids) != len(exit_turn_plan.assignments):
        raise ValueError("exit-turn plan contains duplicate assignments")
    expected_assignment_ids = {
        member_id
        for member_id in exit_turn_plan.member_ids
        if members[member_id].source.station_id == exit_turn_plan.source_id
    }
    unclassified_ids = set(exit_turn_plan.unclassified_member_ids)
    if (
        len(unclassified_ids) != len(exit_turn_plan.unclassified_member_ids)
        or assignment_ids & unclassified_ids
        or assignment_ids | unclassified_ids != expected_assignment_ids
    ):
        raise ValueError("exit-turn assignments do not cover every outbound member")
    if exit_turn_plan.disposition is ExitTurnDisposition.PLANNED and unclassified_ids:
        raise ValueError("planned exit-turn plan contains unclassified members")
    axes = {axis.id: axis for axis in exit_turn_plan.axes}
    if len(axes) != len(exit_turn_plan.axes):
        raise ValueError("exit-turn plan contains duplicate axes")
    for assignment in exit_turn_plan.assignments:
        _validate_exit_turn_assignment(
            exit_turn_plan,
            assignment,
            members,
            lane_by_rank,
            axes,
            endpoint_groups,
            section_grids,
        )
    return axes


def _validate_exit_lane_transitions(
    exit_turn_plan: ExitTurnPlan,
    lane_by_line: Mapping[str, ExitSourceLane],
    transition_owners: dict[ResolvedEdge, ExitTurnPlanId],
) -> None:
    for transition in exit_turn_plan.lane_transitions:
        lane = lane_by_line.get(transition.edge.line_id)
        source_lateral = (
            transition.source_point[1]
            if transition.run_direction in {Direction.R, Direction.L}
            else transition.source_point[0]
        ) + transition.source_offset
        target_lateral = (
            transition.target_point[1]
            if transition.run_direction in {Direction.R, Direction.L}
            else transition.target_point[0]
        ) + transition.target_offset
        source_owned = lane is not None and transition.edge.source in lane.station_ids
        target_owned = lane is not None and transition.edge.target in lane.station_ids
        expected_placement = (
            ExitLaneTransitionPlacement.SOURCE
            if source_owned and not target_owned
            else ExitLaneTransitionPlacement.TARGET
            if target_owned and not source_owned
            else None
        )
        if (
            exit_turn_plan.disposition is not ExitTurnDisposition.PLANNED
            or lane is None
            or expected_placement is None
            or transition.placement is not expected_placement
            or transition.coordinate_regime is not CoordinateRegime.LAYOUT_CANVAS
            or abs(transition.diagonal_run - abs(target_lateral - source_lateral))
            > COORD_TOLERANCE
            or transition.diagonal_run <= 0
            or transition.source_runway <= 0
            or transition.target_runway <= 0
            or (source_owned and transition.source_lane_offset != lane.planned_offset)
            or (target_owned and transition.target_lane_offset != lane.planned_offset)
        ):
            raise ValueError("exit-turn lane transition is inconsistent")
        owner = transition_owners.setdefault(transition.edge, exit_turn_plan.id)
        if owner != exit_turn_plan.id:
            raise ValueError("exit-turn lane transition has more than one owner")


def _validate_exit_turn_diagnostics(plan: RoutePlan) -> None:
    actual = Counter(
        item for item in plan.diagnostics if item.code == "exit-turn-legacy"
    )
    expected = Counter(
        RoutePlanDiagnostic(
            item.member_ids[0] if item.member_ids else None,
            "exit-turn-legacy",
            f"exit group {item.exit_group_id} uses legacy routing: "
            f"{item.legacy_reason}",
            blocking=False,
        )
        for item in plan.exit_turn_plans
        if item.disposition is ExitTurnDisposition.LEGACY
    )
    if actual != expected:
        raise ValueError("exit-turn legacy diagnostics are inconsistent")


def _validate_exit_turn_records(
    plan: RoutePlan,
    members: Mapping[EmissionMemberId, EmissionMember],
) -> tuple[
    dict[ExitTurnPlanId, ExitTurnPlan],
    dict[str, list[ExitTurnPlan]],
    dict[EmissionMemberId, list[ExitTurnPlan]],
]:
    systems = {system.id: system for system in plan.systems}
    endpoint_groups = {item.id: item for item in plan.endpoint_groups}
    divergences = {item.id: item for item in plan.divergences}
    section_grids = {
        item.section_id: item.grid.value
        for item in plan.provenance.sections
        if item.grid is not None
    }
    references = {item.id: item for item in plan.shared_references}
    demands = {item.id: item for item in plan.demands}
    exit_turn_plans = {item.id: item for item in plan.exit_turn_plans}
    if len(exit_turn_plans) != len(plan.exit_turn_plans):
        raise ValueError("route plan contains duplicate exit-turn plan ids")
    by_source: dict[str, list[ExitTurnPlan]] = defaultdict(list)
    by_member: dict[EmissionMemberId, list[ExitTurnPlan]] = defaultdict(list)
    station_owners: dict[tuple[str, str], ExitTurnPlanId] = {}
    transition_owners: dict[ResolvedEdge, ExitTurnPlanId] = {}
    for exit_turn_plan in plan.exit_turn_plans:
        exit_group = _validate_exit_turn_identity(
            exit_turn_plan,
            systems,
            endpoint_groups,
            divergences,
            members,
        )
        lane_by_rank, lane_by_line = _validate_exit_source_lanes(
            exit_turn_plan,
            members,
            station_owners,
        )
        _validate_exit_turn_assignments(
            exit_turn_plan,
            members,
            lane_by_rank,
            endpoint_groups,
            section_grids,
        )
        _validate_exit_lane_transitions(
            exit_turn_plan,
            lane_by_line,
            transition_owners,
        )
        if exit_turn_plan.disposition is ExitTurnDisposition.PLANNED:
            _validate_planned_exit_turn_resources(
                plan,
                exit_turn_plan,
                exit_group,
                members,
                endpoint_groups,
                section_grids,
                references,
                demands,
            )
        elif (
            exit_turn_plan.axes
            or exit_turn_plan.demand_ids
            or exit_turn_plan.lane_transitions
        ):
            raise ValueError("legacy exit-turn plans cannot own geometry")
        by_source[exit_turn_plan.source_id].append(exit_turn_plan)
        for member_id in exit_turn_plan.member_ids:
            by_member[member_id].append(exit_turn_plan)

    for system in plan.systems:
        expected = tuple(
            item.id for item in plan.exit_turn_plans if item.system_id == system.id
        )
        if system.exit_turn_plan_ids != expected:
            raise ValueError("route system exit-turn index is inconsistent")
    if any(len(owners) != 1 for owners in by_member.values()):
        raise ValueError("exit-turn member has more than one owning plan")
    _validate_exit_turn_diagnostics(plan)
    from nf_metro.layout.route_reservations import (
        expected_exit_turn_foreign_references,
    )

    expected_foreign = expected_exit_turn_foreign_references(plan)
    if any(
        item.foreign_reference_ids != expected_foreign[item.id]
        for item in plan.exit_turn_plans
    ):
        raise ValueError("exit-turn foreign-reference index is inconsistent")
    return exit_turn_plans, by_source, by_member


def _validate_fan_records(
    plan: RoutePlan,
    members: Mapping[EmissionMemberId, EmissionMember],
    bindings: Mapping[EmissionMemberId, list[EmissionBinding]],
) -> tuple[
    dict[FanPlanId, FanPlan],
    dict[RouteSystemId, list[FanPlan]],
    dict[EmissionMemberId, list[FanPlan]],
]:
    systems = {system.id: system for system in plan.systems}
    references = {item.id: item for item in plan.shared_references}
    demands = {item.id: item for item in plan.demands}
    fan_plans = {item.id: item for item in plan.fan_plans}
    if len(fan_plans) != len(plan.fan_plans):
        raise ValueError("route plan contains duplicate fan plan ids")
    by_system: dict[RouteSystemId, list[FanPlan]] = defaultdict(list)
    by_member: dict[EmissionMemberId, list[FanPlan]] = defaultdict(list)
    for fan_plan in plan.fan_plans:
        system_id = fan_plan.system_id
        if system_id is None:
            if (
                fan_plan.connector_ids
                or fan_plan.member_ids
                or fan_plan.centreline_reference_id is not None
                or fan_plan.demand_ids
                or any(
                    item.member_id is not None for item in fan_plan.route_expectations
                )
            ):
                raise ValueError("layout-only fan claims canonical route ownership")
            continue
        system = systems.get(system_id)
        if system is None:
            raise ValueError("fan plan names an unknown route system")
        if not set(fan_plan.connector_ids).issubset(system.connector_ids):
            raise ValueError("fan connectors disagree with route-system ownership")
        if not set(fan_plan.member_ids).issubset(system.member_ids):
            raise ValueError("fan members disagree with route-system ownership")
        for branch in fan_plan.branches:
            if not set(branch.connector_ids).issubset(fan_plan.connector_ids):
                raise ValueError("fan branch connectors lie outside fan ownership")
            if not set(branch.member_ids).issubset(fan_plan.member_ids):
                raise ValueError("fan branch members lie outside fan ownership")
        for expectation in fan_plan.route_expectations:
            member_id = expectation.member_id
            if member_id is None:
                continue
            member = members.get(member_id)
            if (
                member is None
                or member.system_id != system_id
                or (
                    member.source.station_id,
                    member.target.station_id,
                    member.line_id,
                )
                != (
                    expectation.edge.source,
                    expectation.edge.target,
                    expectation.edge.line_id,
                )
            ):
                raise ValueError("fan route expectation has inconsistent membership")
            member_bindings = bindings.get(member_id, [])
            if (
                len(member_bindings) != 1
                or member_bindings[0].kind is BindingKind.UNROUTED
            ):
                raise ValueError("planned fan member has no final emission binding")
        owns_emission_resources = (
            fan_plan.disposition is FanPlanDisposition.PLANNED
            and system.disposition is RouteSystemDisposition.PLANNED
        )
        if owns_emission_resources:
            reference_id = fan_plan.centreline_reference_id
            reference = (
                references.get(reference_id) if reference_id is not None else None
            )
            fan_demands = tuple(demands.get(item) for item in fan_plan.demand_ids)
            if (
                reference is None
                or reference.system_id != system_id
                or reference.claimant_member_ids != fan_plan.member_ids
                or any(item is None for item in fan_demands)
                or any(
                    item is not None and item.system_id != system_id
                    for item in fan_demands
                )
            ):
                raise ValueError("planned fan resources have inconsistent ownership")
        by_system[system_id].append(fan_plan)
        if owns_emission_resources:
            for member_id in fan_plan.member_ids:
                by_member[member_id].append(fan_plan)

    for system in plan.systems:
        expected = tuple(item.id for item in by_system.get(system.id, ()))
        if system.fan_plan_ids != expected:
            raise ValueError("route system fan-plan index is inconsistent")
    if any(len(owners) != 1 for owners in by_member.values()):
        raise ValueError("fan member has more than one owning plan")
    actual_diagnostics = Counter(
        item for item in plan.diagnostics if item.code == "fan-plan-legacy"
    )
    expected_diagnostics = Counter(
        RoutePlanDiagnostic(
            None,
            "fan-plan-legacy",
            f"fan {item.id} uses legacy layout: {item.legacy_reason}",
            blocking=False,
        )
        for item in plan.fan_plans
        if item.disposition is FanPlanDisposition.LEGACY
    )
    if actual_diagnostics != expected_diagnostics:
        raise ValueError("fan legacy diagnostics are inconsistent")
    return fan_plans, by_system, by_member


def _validate_convergence_resources(
    route_plan: RoutePlan,
    convergence: ConvergencePlan,
    references: Mapping[SharedReferenceId, SharedReference],
    demands: Mapping[DemandId, SymbolicDemand],
) -> None:
    expected_reference_ids, expected_demand_ids = convergence_resource_ids(
        convergence.id
    )
    if (
        convergence.shared_reference_ids != expected_reference_ids
        or convergence.demand_ids != expected_demand_ids
    ):
        raise ValueError("planned convergence resource identities are inconsistent")

    trunk_reference = references.get(expected_reference_ids[0])
    landing_reference = references.get(expected_reference_ids[1])
    lanes_demand = demands.get(expected_demand_ids[0])
    runway_demand = demands.get(expected_demand_ids[1])
    if any(
        item is None
        for item in (
            trunk_reference,
            landing_reference,
            lanes_demand,
            runway_demand,
        )
    ):
        raise ValueError("planned convergence resources are missing")
    assert trunk_reference is not None
    assert landing_reference is not None
    assert lanes_demand is not None
    assert runway_demand is not None
    assert convergence.trunk_axis is not None

    trunk_axis = convergence.trunk_axis.axis
    if trunk_axis is DemandAxis.X:
        lane_axis = DemandAxis.Y
    elif trunk_axis is DemandAxis.Y:
        lane_axis = DemandAxis.X
    else:
        raise ValueError("planned convergence trunk has an invalid demand axis")
    landing_member_ids = tuple(item.member_id for item in convergence.landings)
    expected_provenance = reservation_decision_refs(
        route_plan.provenance,
        convergence.connector_ids,
        lanes_demand.span,
    )
    if (
        trunk_reference.system_id != convergence.system_id
        or trunk_reference.kind is not SharedReferenceKind.TRUNK
        or trunk_reference.claimant_member_ids != convergence.member_ids
        or trunk_reference.coordinate_regime is not CoordinateRegime.LAYOUT_CANVAS
        or trunk_reference.provenance != expected_provenance
        or landing_reference.system_id != convergence.system_id
        or landing_reference.kind is not SharedReferenceKind.LANDING_SEQUENCE
        or landing_reference.claimant_member_ids != convergence.member_ids
        or landing_reference.coordinate_regime is not CoordinateRegime.LAYOUT_CANVAS
        or landing_reference.provenance != expected_provenance
    ):
        raise ValueError("planned convergence shared references are inconsistent")
    if (
        lanes_demand.system_id != convergence.system_id
        or lanes_demand.claimant_member_ids != convergence.member_ids
        or lanes_demand.kind is not DemandKind.LANES
        or lanes_demand.axis is not lane_axis
        or lanes_demand.lane_count != len(convergence.lane_order)
        or lanes_demand.minimum_size is not None
        or lanes_demand.minimum_size_regime is not None
        or lanes_demand.ordered_reference_ids != (expected_reference_ids[0],)
        or lanes_demand.keep_out_classes != (KeepOutClass.SECTION, KeepOutClass.MARKER)
        or lanes_demand.provenance != expected_provenance
        or runway_demand.system_id != convergence.system_id
        or runway_demand.claimant_member_ids != landing_member_ids
        or runway_demand.kind is not DemandKind.RUNWAY
        or runway_demand.axis is not trunk_axis
        or runway_demand.span != lanes_demand.span
        or runway_demand.lane_count != len(convergence.landings)
        or runway_demand.minimum_size
        != max(item.minimum_runway for item in convergence.landings)
        or runway_demand.minimum_size_regime is not CoordinateRegime.LAYOUT_CANVAS
        or runway_demand.ordered_reference_ids != expected_reference_ids
        or runway_demand.keep_out_classes != (KeepOutClass.SECTION, KeepOutClass.MARKER)
        or runway_demand.provenance != expected_provenance
    ):
        raise ValueError("planned convergence symbolic demands are inconsistent")


def _validate_convergence_records(
    plan: RoutePlan,
    members: Mapping[EmissionMemberId, EmissionMember],
    bindings: Mapping[EmissionMemberId, list[EmissionBinding]],
) -> tuple[
    dict[ConvergencePlanId, ConvergencePlan],
    dict[RouteSystemId, list[ConvergencePlan]],
    dict[ConvergenceId, list[ConvergencePlan]],
    dict[ConnectorId, list[ConvergencePlan]],
    dict[EmissionMemberId, list[ConvergencePlan]],
    dict[tuple[ResolvedEdge, ...], list[ConvergencePlan]],
]:
    systems = {system.id: system for system in plan.systems}
    convergences = {item.id: item for item in plan.convergences}
    endpoint_groups = {item.id: item for item in plan.endpoint_groups}
    references = {item.id: item for item in plan.shared_references}
    demands = {item.id: item for item in plan.demands}
    plans = {item.id: item for item in plan.convergence_plans}
    if len(plans) != len(plan.convergence_plans):
        raise ValueError("route plan contains duplicate convergence plan ids")
    by_system: dict[RouteSystemId, list[ConvergencePlan]] = defaultdict(list)
    by_convergence: dict[ConvergenceId, list[ConvergencePlan]] = defaultdict(list)
    by_connector: dict[ConnectorId, list[ConvergencePlan]] = defaultdict(list)
    by_member: dict[EmissionMemberId, list[ConvergencePlan]] = defaultdict(list)
    by_path: dict[tuple[ResolvedEdge, ...], list[ConvergencePlan]] = defaultdict(list)
    member_rank = {member.id: rank for rank, member in enumerate(plan.members)}
    members_by_convergence: dict[ConvergenceId, list[EmissionMember]] = defaultdict(
        list
    )
    for member in plan.members:
        for convergence_id in member.convergence_ids:
            members_by_convergence[convergence_id].append(member)
    disposition_by_system: dict[RouteSystemId, ConvergenceDisposition] = {}
    for item in plan.convergence_plans:
        system = systems.get(item.system_id)
        if system is None:
            raise ValueError("convergence plan names an unknown route system")
        if not set(item.convergence_ids).issubset(system.convergence_ids):
            raise ValueError("convergence identity lies outside its route system")
        if not set(item.connector_ids).issubset(system.connector_ids):
            raise ValueError("convergence connectors lie outside their route system")
        if not set(item.member_ids).issubset(system.member_ids):
            raise ValueError("convergence members lie outside their route system")
        if any(
            convergence_id not in convergences
            or convergences[convergence_id].system_id != item.system_id
            for convergence_id in item.convergence_ids
        ):
            raise ValueError("convergence plan has inconsistent semantic identity")
        semantic_records = tuple(
            convergences[convergence_id] for convergence_id in item.convergence_ids
        )
        expected_entry_groups = _ordered_unique(
            record.entry_group_id for record in semantic_records
        )
        expected_merge_junctions = _ordered_unique(
            record.junction_id for record in semantic_records
        )
        expected_target_ports = tuple(
            endpoint_groups[group_id].port_id for group_id in expected_entry_groups
        )
        expected_connectors = _ordered_unique(
            connector_id
            for record in semantic_records
            for connector_id in record.connector_ids
        )
        expected_lines = _ordered_unique(record.line_id for record in semantic_records)
        if (
            item.entry_group_ids != expected_entry_groups
            or item.merge_junction_ids != expected_merge_junctions
            or item.target_entry_port_ids != expected_target_ports
            or item.connector_ids != expected_connectors
            or item.line_ids != expected_lines
        ):
            raise ValueError("convergence plan semantic fields are inconsistent")
        if len(item.member_ids) != len(item.resolved_member_edges) or any(
            member_id not in members
            or members[member_id].system_id != item.system_id
            or members[member_id].edge != edge
            or not set(item.convergence_ids).intersection(
                members[member_id].convergence_ids
            )
            for member_id, edge in zip(
                item.member_ids, item.resolved_member_edges, strict=True
            )
        ):
            raise ValueError("convergence plan has inconsistent emission membership")
        merge_junctions = set(item.merge_junction_ids)
        candidate_members = {
            member.id: member
            for convergence_id in item.convergence_ids
            for member in members_by_convergence.get(convergence_id, ())
        }
        expected_member_ids = tuple(
            member.id
            for member in sorted(
                candidate_members.values(), key=lambda member: member_rank[member.id]
            )
            if member.system_id == item.system_id
            and (
                member.edge.source in merge_junctions
                or member.edge.target in merge_junctions
            )
        )
        if item.member_ids != expected_member_ids:
            raise ValueError("convergence plan emission membership is incomplete")
        prior = disposition_by_system.setdefault(item.system_id, item.disposition)
        if prior is not item.disposition:
            raise ValueError("one route system mixes convergence dispositions")
        if item.disposition is ConvergenceDisposition.PLANNED:
            _validate_convergence_resources(plan, item, references, demands)
            for ownership in item.endpoint_ownership:
                (binding,) = bindings[ownership.member_id]
                if (
                    ownership.connector_ids
                    != members[ownership.member_id].connector_ids
                ):
                    raise ValueError(
                        "convergence endpoint ownership connectors disagree with member"
                    )
                if ownership.role is ConvergenceEndpointRole.COVERED_CONTINUATION:
                    if (
                        binding.kind
                        not in {
                            BindingKind.MERGE_SKIP,
                            BindingKind.COVERED_MERGE_HOP,
                        }
                        or binding.covering_member_id != ownership.covered_by_member_id
                    ):
                        raise ValueError(
                            "convergence coverage disagrees with final binding"
                        )
                elif binding.kind is not BindingKind.EMITTED:
                    raise ValueError(
                        "convergence endpoint owner has no emitted binding"
                    )
        by_system[item.system_id].append(item)
        for convergence_id in item.convergence_ids:
            by_convergence[convergence_id].append(item)
        for connector_id in item.connector_ids:
            by_connector[connector_id].append(item)
        for member_id in item.member_ids:
            by_member[member_id].append(item)
        for path in item.resolved_member_paths:
            by_path[path].append(item)
    for system in plan.systems:
        system_plans = tuple(by_system.get(system.id, ()))
        expected_plan_ids = tuple(item.id for item in system_plans)
        if system.convergence_plan_ids != expected_plan_ids:
            raise ValueError("route system convergence-plan index is inconsistent")
        planned_convergence_ids = tuple(
            convergence_id
            for item in system_plans
            for convergence_id in item.convergence_ids
        )
        if planned_convergence_ids != system.convergence_ids:
            raise ValueError("route system convergence-plan coverage is inconsistent")
    actual_diagnostics = Counter(
        item for item in plan.diagnostics if item.code == "convergence-plan-legacy"
    )
    expected_diagnostics = Counter(
        RoutePlanDiagnostic(
            None,
            "convergence-plan-legacy",
            f"convergence system {item.system_id} uses legacy routing: "
            f"{item.legacy_reason}",
            blocking=False,
        )
        for item in plan.convergence_plans
        if item.disposition is ConvergenceDisposition.LEGACY
    )
    if actual_diagnostics != expected_diagnostics:
        raise ValueError("convergence legacy diagnostics are inconsistent")
    from nf_metro.layout.route_reservations import (
        expected_convergence_foreign_references,
    )

    expected_foreign = expected_convergence_foreign_references(plan)
    if any(
        item.foreign_reference_ids != expected_foreign[item.id]
        for item in plan.convergence_plans
    ):
        raise ValueError("convergence foreign-reference index is inconsistent")
    return plans, by_system, by_convergence, by_connector, by_member, by_path


def _validate_member_geometry_records(
    plan: RoutePlan,
    members: Mapping[EmissionMemberId, EmissionMember],
    bindings: Mapping[EmissionMemberId, list[EmissionBinding]],
) -> None:
    """Cross-check immutable member templates against canonical membership."""
    records = {item.id: item for item in plan.member_geometry_plans}
    if len(records) != len(plan.member_geometry_plans):
        raise ValueError("route plan contains duplicate member geometry plan ids")
    member_ids = tuple(item.member_id for item in plan.member_geometry_plans)
    if len(set(member_ids)) != len(member_ids):
        raise ValueError("emission member has more than one member geometry plan")
    systems = {system.id: system for system in plan.systems}
    by_system: defaultdict[RouteSystemId, list[RouteMemberGeometryPlan]] = defaultdict(
        list
    )
    for record in plan.member_geometry_plans:
        system = systems.get(record.system_id)
        member = members.get(record.member_id)
        if system is None or system.disposition is not RouteSystemDisposition.PLANNED:
            raise ValueError("member geometry plan names a non-planned route system")
        if (
            member is None
            or member.system_id != record.system_id
            or member.id not in system.member_ids
            or member.family_id is None
            or bindings[member.id][0].kind is not BindingKind.EMITTED
        ):
            raise ValueError("member geometry plan names a non-planned emission member")
        if member.family_id is RouteFamilyId.RAIL_INTER_SECTION:
            raise ValueError("rail emitter cannot have a member geometry plan")
        if (
            record.edge != member.edge
            or record.family_id != member.family_id
            or record.connector_ids != member.connector_ids
        ):
            raise ValueError("member geometry plan identity disagrees with its member")
        by_system[record.system_id].append(record)
    for system in plan.systems:
        expected = tuple(item.id for item in by_system.get(system.id, ()))
        if system.member_geometry_plan_ids != expected:
            raise ValueError("route system member-geometry index is inconsistent")


def _validate_final_geometry_ownership(
    plan: RoutePlan,
    members: Mapping[EmissionMemberId, EmissionMember],
    bindings: Mapping[EmissionMemberId, list[EmissionBinding]],
) -> None:
    """Require one complete geometry owner for every planned emitted member."""
    systems = {system.id: system for system in plan.systems}
    expected = {
        member.id
        for member in members.values()
        if systems[member.system_id].disposition is RouteSystemDisposition.PLANNED
        and bindings[member.id][0].kind is BindingKind.EMITTED
    }
    owners = Counter(item.member_id for item in plan.member_geometry_plans)
    owners.update(
        ownership.member_id
        for convergence in plan.convergence_plans
        if convergence.disposition is ConvergenceDisposition.PLANNED
        for ownership in convergence.endpoint_ownership
        if bindings[ownership.member_id][0].kind is BindingKind.EMITTED
    )
    owners.update(
        member.id
        for member in members.values()
        if member.family_id is RouteFamilyId.RAIL_INTER_SECTION
        and systems[member.system_id].disposition is RouteSystemDisposition.PLANNED
        and bindings[member.id][0].kind is BindingKind.EMITTED
    )
    if set(owners) != expected or any(count != 1 for count in owners.values()):
        raise ValueError("planned emitted member geometry ownership is incomplete")


def _validate_route_system_indexes(
    plan: RoutePlan,
    connector_owner: Mapping[ConnectorId, RouteSystemId],
) -> None:
    provenance_connector_ids = tuple(
        connector.connector_id for connector in plan.provenance.connectors
    )
    if set(provenance_connector_ids) != set(connector_owner):
        raise ValueError("route-system connector index disagrees with provenance")
    expected_system_order = _ordered_unique(
        connector_owner[connector_id] for connector_id in provenance_connector_ids
    )
    if tuple(system.id for system in plan.systems) != expected_system_order:
        raise ValueError("route systems are not in canonical connector order")

    def record_ids_for(
        records: Iterable[
            ResolvedEndpointGroup
            | RouteDivergence
            | RouteConvergence
            | RouteBranch
            | RouteFeeder
        ],
        system_id: RouteSystemId,
    ) -> tuple[object, ...]:
        return tuple(record.id for record in records if record.system_id == system_id)

    for system in plan.systems:
        connector_facts = tuple(
            connector
            for connector in plan.provenance.connectors
            if connector_owner[connector.connector_id] == system.id
        )
        if system.connector_ids != tuple(
            connector.connector_id for connector in connector_facts
        ):
            raise ValueError("route-system connector index is not canonical")
        if system.line_ids != _ordered_unique(
            connector.line_id for connector in connector_facts
        ):
            raise ValueError("route-system line index disagrees with records")
        if system.bundle_ids != _ordered_unique(
            connector.bundle_id for connector in connector_facts
        ):
            raise ValueError("route-system bundle index disagrees with records")
        expected_indexes = (
            record_ids_for(
                (
                    record
                    for record in plan.endpoint_groups
                    if record.role is ConnectorEndpointRole.EXIT
                ),
                system.id,
            ),
            record_ids_for(
                (
                    record
                    for record in plan.endpoint_groups
                    if record.role is ConnectorEndpointRole.ENTRY
                ),
                system.id,
            ),
            record_ids_for(plan.divergences, system.id),
            record_ids_for(plan.convergences, system.id),
            record_ids_for(plan.branches, system.id),
            record_ids_for(plan.feeders, system.id),
        )
        actual_indexes = (
            system.exit_group_ids,
            system.entry_group_ids,
            system.divergence_ids,
            system.convergence_ids,
            system.branch_ids,
            system.feeder_ids,
        )
        if actual_indexes != expected_indexes:
            raise ValueError("route-system ownership indexes disagree with records")


def _validate_route_system_records(
    plan: RoutePlan,
    members: Mapping[EmissionMemberId, EmissionMember],
) -> None:
    systems = {system.id: system for system in plan.systems}
    if len(systems) != len(plan.systems):
        raise ValueError("route plan contains duplicate route-system ids")
    for label, records in (("branch", plan.branches), ("feeder", plan.feeders)):
        if len({record.id for record in records}) != len(records):
            raise ValueError(f"route plan contains duplicate route {label} ids")

    connector_owner: dict[ConnectorId, RouteSystemId] = {}
    member_owner: dict[EmissionMemberId, RouteSystemId] = {}
    for system in plan.systems:
        if len(set(system.connector_ids)) != len(system.connector_ids):
            raise ValueError(f"route system {system.id} repeats a connector id")
        if len(set(system.member_ids)) != len(system.member_ids):
            raise ValueError(f"route system {system.id} repeats an emission member id")
        for connector_id in system.connector_ids:
            prior = connector_owner.setdefault(connector_id, system.id)
            if prior != system.id:
                raise ValueError("one connector belongs to multiple route systems")
        for member_id in system.member_ids:
            prior = member_owner.setdefault(member_id, system.id)
            if prior != system.id:
                raise ValueError(
                    "one emission member belongs to multiple route systems"
                )

    if set(member_owner) != set(members):
        raise ValueError("route-system emission-member partition is incomplete")
    members_by_system: dict[RouteSystemId, list[EmissionMemberId]] = defaultdict(list)
    referenced_connectors: set[ConnectorId] = set()
    for member in plan.members:
        if member.system_id not in systems:
            raise ValueError(f"emission member {member.id} has an unknown route system")
        if member_owner[member.id] != member.system_id:
            raise ValueError("route-system emission-member ownership disagrees")
        members_by_system[member.system_id].append(member.id)
        for connector_id in member.connector_ids:
            if connector_owner.get(connector_id) != member.system_id:
                raise ValueError("emission member connector ownership disagrees")
            referenced_connectors.add(connector_id)
    for system in plan.systems:
        if tuple(members_by_system[system.id]) != system.member_ids:
            raise ValueError(
                "route-system emission-member index disagrees with records"
            )

    ownership_records: tuple[
        ResolvedEndpointGroup
        | RouteDivergence
        | RouteConvergence
        | RouteBranch
        | RouteFeeder,
        ...,
    ] = (
        *plan.endpoint_groups,
        *plan.divergences,
        *plan.convergences,
        *plan.branches,
        *plan.feeders,
    )
    for record in ownership_records:
        if record.system_id not in systems:
            raise ValueError("route ownership record has an unknown route system")
        for connector_id in record.connector_ids:
            if connector_owner.get(connector_id) != record.system_id:
                raise ValueError("route ownership record connector ownership disagrees")
            referenced_connectors.add(connector_id)
    if referenced_connectors != set(connector_owner):
        raise ValueError("route-system connector partition is incomplete")
    _validate_route_system_indexes(plan, connector_owner)


def build_route_plan_query(plan: RoutePlan) -> RoutePlanQuery:
    endpoint_groups = {item.id: item for item in plan.endpoint_groups}
    divergences = {item.id: item for item in plan.divergences}
    convergences = {item.id: item for item in plan.convergences}
    members = {member.id: member for member in plan.members}
    for label, index, records in (
        ("endpoint group", endpoint_groups, plan.endpoint_groups),
        ("divergence", divergences, plan.divergences),
        ("convergence", convergences, plan.convergences),
    ):
        if len(index) != len(records):
            raise ValueError(f"route plan contains duplicate {label} ids")
    if len(members) != len(plan.members):
        raise ValueError("route plan contains duplicate emission member ids")
    _validate_route_system_records(plan, members)
    exit_turn_plans, exit_turns_by_source, exit_turns_by_member = (
        _validate_exit_turn_records(plan, members)
    )
    bindings: dict[EmissionMemberId, list[EmissionBinding]] = defaultdict(list)
    for binding in plan.bindings:
        if binding.member_id not in members:
            raise ValueError(f"binding has unknown member {binding.member_id!r}")
        if (
            binding.covering_member_id is not None
            and binding.covering_member_id not in members
        ):
            raise ValueError(
                f"binding has unknown carrier {binding.covering_member_id!r}"
            )
        member = members[binding.member_id]
        family_required = binding.kind in {
            BindingKind.EMITTED,
            BindingKind.COVERED_MERGE_HOP,
        }
        if family_required != (member.family_id is not None):
            raise ValueError(
                f"{binding.kind.value} member has inconsistent production family"
            )
        bindings[binding.member_id].append(binding)
    if set(bindings) != set(members) or any(
        len(member_bindings) != 1 for member_bindings in bindings.values()
    ):
        raise ValueError("every emission member must have exactly one binding")
    for binding in plan.bindings:
        if binding.covering_member_id is None:
            continue
        member = members[binding.member_id]
        carrier = members[binding.covering_member_id]
        if carrier.id == member.id or carrier.system_id != member.system_id:
            raise ValueError("covered members require a distinct same-system carrier")
        (carrier_binding,) = bindings[carrier.id]
        if carrier_binding.kind is not BindingKind.EMITTED:
            raise ValueError("covered members require an emitted carrier")

    _validate_member_geometry_records(plan, members, bindings)
    fan_plans, fan_plans_by_system, fan_plans_by_member = _validate_fan_records(
        plan,
        members,
        bindings,
    )
    (
        convergence_plans,
        convergence_plans_by_system,
        convergence_plans_by_convergence,
        convergence_plans_by_connector,
        convergence_plans_by_member,
        convergence_plans_by_path,
    ) = _validate_convergence_records(plan, members, bindings)
    _validate_final_geometry_ownership(plan, members, bindings)

    from nf_metro.layout.route_reservations import build_reservation_query_indexes

    reservation_indexes = build_reservation_query_indexes(plan, members, bindings)
    return RoutePlanQuery(
        plan,
        MappingProxyType(endpoint_groups),
        MappingProxyType(divergences),
        MappingProxyType(convergences),
        MappingProxyType(members),
        MappingProxyType({key: tuple(value) for key, value in bindings.items()}),
        MappingProxyType(exit_turn_plans),
        MappingProxyType(
            {key: tuple(value) for key, value in exit_turns_by_source.items()}
        ),
        MappingProxyType(
            {key: tuple(value) for key, value in exit_turns_by_member.items()}
        ),
        MappingProxyType(fan_plans),
        MappingProxyType(
            {key: tuple(value) for key, value in fan_plans_by_system.items()}
        ),
        MappingProxyType(
            {key: tuple(value) for key, value in fan_plans_by_member.items()}
        ),
        MappingProxyType(convergence_plans),
        MappingProxyType(
            {key: tuple(value) for key, value in convergence_plans_by_system.items()}
        ),
        MappingProxyType(
            {
                key: tuple(value)
                for key, value in convergence_plans_by_convergence.items()
            }
        ),
        MappingProxyType(
            {key: tuple(value) for key, value in convergence_plans_by_connector.items()}
        ),
        MappingProxyType(
            {key: tuple(value) for key, value in convergence_plans_by_member.items()}
        ),
        MappingProxyType(
            {key: tuple(value) for key, value in convergence_plans_by_path.items()}
        ),
        MappingProxyType(reservation_indexes.references),
        MappingProxyType(reservation_indexes.demands),
        MappingProxyType(reservation_indexes.reservations),
        MappingProxyType(reservation_indexes.realisations),
        MappingProxyType(
            {key: tuple(value) for key, value in reservation_indexes.by_system.items()}
        ),
        MappingProxyType(
            {key: tuple(value) for key, value in reservation_indexes.by_member.items()}
        ),
    )


def _json_value(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _json_value(getattr(value, item.name))
            for item in dataclasses.fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    raise TypeError(f"route plan contains unsupported {type(value).__name__}")


def serialize_route_plan(plan: RoutePlan) -> str:
    """Return the canonical JSON representation of one immutable plan."""
    return json.dumps(_json_value(plan), sort_keys=True, separators=(",", ":"))
