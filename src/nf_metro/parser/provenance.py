"""Typed provenance for authored and effective layout commitments."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Generic, TypeVar

from nf_metro.options import LineOrder

if TYPE_CHECKING:
    from nf_metro.parser.model import MetroGraph, PortSide
    from nf_metro.parser.route_topology import AuthoredRouteCapture, ConnectorId

T = TypeVar("T")
GridCell = tuple[int, int, int, int]


class DecisionOrigin(str, Enum):
    """Who selected an effective layout value."""

    AUTHORED = "authored"
    INFERRED = "inferred"


class DecisionState(str, Enum):
    """User-facing state derived from origin and re-inference locking."""

    AUTHORED = "authored"
    INFERRED = "inferred"
    INFERRED_THEN_PINNED = "inferred-then-pinned"


class DecisionReason(str, Enum):
    """Stable causes for effective layout decisions and transitions."""

    AUTHOR_DIRECTIVE = "author-directive"
    CALLER_FOLD_THRESHOLD = "caller-fold-threshold"
    DEFAULT_FOLD_THRESHOLD = "default-fold-threshold"
    CALLER_LINE_ORDER = "caller-line-order"
    DEFAULT_LINE_ORDER = "default-line-order"
    AUTO_GRID = "auto-grid"
    AUTO_DIRECTION = "auto-direction"
    EXPLICIT_GRID_DEFAULT_DIRECTION = "explicit-grid-default-direction"
    TALL_ANCHOR_GRID = "tall-anchor-grid"
    TALL_ANCHOR_DIRECTION = "tall-anchor-direction"
    FOLD_BRIDGE_DIRECTION = "fold-bridge-direction"
    FLOW_REORIENTED_DIRECTION = "flow-reoriented-direction"
    AUTO_ENTRY_SIDE = "auto-entry-side"
    AUTO_EXIT_SIDE = "auto-exit-side"
    SHARED_CONNECTOR_ENTRY_SIDE = "shared-connector-entry-side"
    RESOLUTION_SIDE_SELECTION = "resolution-side-selection"
    FOLD_RELOCATED_SIDE = "fold-relocated-side"
    FLOW_REANCHORED_SIDE = "flow-reanchored-side"
    CALLER_COMMITMENT = "caller-commitment"
    CANDIDATE_COMMITMENT = "candidate-commitment"


class FoldThresholdSource(str, Enum):
    """Precedence source that supplied the effective fold threshold."""

    CALLER = "caller"
    DIRECTIVE = "directive"
    DEFAULT = "default"


class LineOrderSource(str, Enum):
    """Precedence source that supplied the effective line-order policy."""

    CALLER = "caller"
    DIRECTIVE = "directive"
    DEFAULT = "default"


class ConnectorEndpointRole(str, Enum):
    """Which section boundary of a semantic connector is described."""

    EXIT = "exit"
    ENTRY = "entry"


@dataclass(frozen=True, slots=True)
class EffectiveDecision(Generic[T]):
    """One effective value with independent ownership and lock state."""

    value: T
    origin: DecisionOrigin
    locked: bool
    reason: DecisionReason
    authored_values: tuple[T, ...] = ()

    @property
    def is_author_owned(self) -> bool:
        """Whether the author selected this exact effective commitment."""
        return self.origin is DecisionOrigin.AUTHORED

    @property
    def is_reinference_locked(self) -> bool:
        """Whether a later inference pass must preserve this effective value."""
        return self.locked

    @property
    def state(self) -> DecisionState:
        """Return the compact author-facing state for this decision."""
        if self.origin is DecisionOrigin.AUTHORED:
            return DecisionState.AUTHORED
        if self.locked:
            return DecisionState.INFERRED_THEN_PINNED
        return DecisionState.INFERRED


@dataclass(frozen=True, slots=True)
class SectionIntent(Generic[T]):
    """One authored section-level layout value."""

    section_id: str
    value: T


@dataclass(frozen=True, slots=True)
class ConnectorEndpointKey:
    """Stable identity of one semantic connector boundary."""

    connector_id: ConnectorId
    role: ConnectorEndpointRole


@dataclass(frozen=True, slots=True)
class ConnectorSideIntent:
    """One authored side option for a semantic connector boundary."""

    endpoint: ConnectorEndpointKey
    value: PortSide


@dataclass(frozen=True, slots=True)
class PortHintIntent:
    """One entry or exit directive exactly as accepted by the parser."""

    section_id: str
    role: ConnectorEndpointRole
    side: PortSide
    line_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FoldThresholdIntent:
    """Authored threshold inputs and the precedence-selected source."""

    directive_value: int | None
    caller_value: int | None
    selected_value: int
    selected_source: FoldThresholdSource


@dataclass(frozen=True, slots=True)
class LineOrderIntent:
    """Line-order inputs and definition-order line ids before inference."""

    directive_value: LineOrder | None
    caller_value: LineOrder | None
    selected_value: LineOrder
    selected_source: LineOrderSource
    authored_line_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AuthoredLayoutIntent:
    """Immutable pre-inference snapshot of accepted author layout input."""

    grids: tuple[SectionIntent[GridCell], ...]
    directions: tuple[SectionIntent[str], ...]
    port_hints: tuple[PortHintIntent, ...]
    connector_sides: tuple[ConnectorSideIntent, ...]
    fold_threshold: FoldThresholdIntent
    line_order: LineOrderIntent

    def endpoint_values(self, endpoint: ConnectorEndpointKey) -> tuple[PortSide, ...]:
        """Return authored side options for *endpoint* in source order."""
        return tuple(
            item.value for item in self.connector_sides if item.endpoint == endpoint
        )

    def endpoint_values_index(
        self,
    ) -> dict[ConnectorEndpointKey, tuple[PortSide, ...]]:
        """Index authored side options by semantic connector endpoint."""
        values: dict[ConnectorEndpointKey, list[PortSide]] = {}
        for item in self.connector_sides:
            values.setdefault(item.endpoint, []).append(item.value)
        return {endpoint: tuple(sides) for endpoint, sides in values.items()}

    def port_hint_index(
        self,
    ) -> dict[tuple[str, ConnectorEndpointRole, str], tuple[PortSide, ...]]:
        """Index authored side options by section, endpoint role, and line."""
        return _index_port_hints(self.port_hints)


def _index_port_hints(
    port_hints: tuple[PortHintIntent, ...] | list[PortHintIntent],
) -> dict[tuple[str, ConnectorEndpointRole, str], tuple[PortSide, ...]]:
    values: dict[tuple[str, ConnectorEndpointRole, str], list[PortSide]] = {}
    for hint in port_hints:
        for line_id in hint.line_ids:
            values.setdefault((hint.section_id, hint.role, line_id), []).append(
                hint.side
            )
    return {key: tuple(sides) for key, sides in values.items()}


@dataclass(frozen=True, slots=True)
class EndpointSideSelection:
    """A typed side selected while connector endpoints are resolved."""

    side: PortSide
    origin: DecisionOrigin
    locked: bool
    reason: DecisionReason


@dataclass(frozen=True, slots=True)
class EndpointSideTransition:
    """A resolver mutation applied to a set of line-specific endpoints."""

    section_id: str
    role: ConnectorEndpointRole
    line_ids: tuple[str, ...]
    previous: PortSide
    effective: PortSide
    reason: DecisionReason


@dataclass(slots=True)
class LayoutProvenance:
    """Authored snapshot and effective layout decisions for one graph."""

    authored: AuthoredLayoutIntent | None = None
    grids: dict[str, EffectiveDecision[GridCell]] = field(default_factory=dict)
    directions: dict[str, EffectiveDecision[str]] = field(default_factory=dict)
    connector_sides: dict[ConnectorEndpointKey, EffectiveDecision[PortSide]] = field(
        default_factory=dict
    )
    fold_threshold_decision: EffectiveDecision[int] | None = None
    line_order_decision: EffectiveDecision[LineOrder] | None = None

    def record_authored_line_order(self, value: LineOrder) -> None:
        """Record an accepted ``line_order:`` directive."""
        self.line_order_decision = EffectiveDecision(
            value,
            DecisionOrigin.AUTHORED,
            True,
            DecisionReason.AUTHOR_DIRECTIVE,
            (value,),
        )

    def record_caller_line_order(self, value: LineOrder) -> None:
        """Record an explicit caller override without losing author intent."""
        self.line_order_decision = EffectiveDecision(
            value,
            DecisionOrigin.AUTHORED,
            True,
            DecisionReason.CALLER_LINE_ORDER,
            (value,),
        )
        if self.authored is not None:
            current = self.authored.line_order
            self.authored = replace(
                self.authored,
                line_order=LineOrderIntent(
                    directive_value=current.directive_value,
                    caller_value=value,
                    selected_value=value,
                    selected_source=LineOrderSource.CALLER,
                    authored_line_ids=current.authored_line_ids,
                ),
            )

    def record_authored_grid(self, section_id: str, value: GridCell) -> None:
        """Record an accepted ``grid:`` directive."""
        self.grids[section_id] = EffectiveDecision(
            value,
            DecisionOrigin.AUTHORED,
            True,
            DecisionReason.AUTHOR_DIRECTIVE,
            (value,),
        )

    def record_authored_direction(self, section_id: str, value: str) -> None:
        """Record an accepted ``direction:`` directive."""
        self.directions[section_id] = EffectiveDecision(
            value,
            DecisionOrigin.AUTHORED,
            True,
            DecisionReason.AUTHOR_DIRECTIVE,
            (value,),
        )

    def capture_authored_intent(
        self,
        graph: MetroGraph,
        routes: AuthoredRouteCapture,
        caller_fold_threshold: int | None,
        caller_line_order: LineOrder | None = None,
    ) -> None:
        """Freeze accepted layout input before any graph rewrite or inference."""
        if self.authored is not None:
            raise RuntimeError("authored layout intent has already been captured")

        grids = tuple(
            SectionIntent(section_id, grid_decision.value)
            for section_id in routes.section_ids
            if (grid_decision := self.grids.get(section_id)) is not None
            and grid_decision.origin is DecisionOrigin.AUTHORED
        )
        directions = tuple(
            SectionIntent(section_id, direction_decision.value)
            for section_id in routes.section_ids
            if (direction_decision := self.directions.get(section_id)) is not None
            and direction_decision.origin is DecisionOrigin.AUTHORED
        )

        port_hints: list[PortHintIntent] = []
        for section_id, section in graph.sections.items():
            for role, hints in (
                (ConnectorEndpointRole.EXIT, section.exit_hints),
                (ConnectorEndpointRole.ENTRY, section.entry_hints),
            ):
                port_hints.extend(
                    PortHintIntent(section_id, role, side, tuple(line_ids))
                    for side, line_ids in hints
                )

        hint_index = _index_port_hints(port_hints)
        connector_sides: list[ConnectorSideIntent] = []
        for fact in routes.edges:
            if (
                fact.source_section is None
                or fact.target_section is None
                or fact.source_section == fact.target_section
            ):
                continue
            for role, section_id in (
                (ConnectorEndpointRole.EXIT, fact.source_section),
                (ConnectorEndpointRole.ENTRY, fact.target_section),
            ):
                endpoint = ConnectorEndpointKey(fact.key.id, role)
                connector_sides.extend(
                    ConnectorSideIntent(endpoint, side)
                    for side in hint_index.get((section_id, role, fact.key.line_id), ())
                )

        directive = graph.fold_threshold
        if caller_fold_threshold is not None:
            selected_value = caller_fold_threshold
            source = FoldThresholdSource.CALLER
            fold_decision = EffectiveDecision(
                selected_value,
                DecisionOrigin.AUTHORED,
                True,
                DecisionReason.CALLER_FOLD_THRESHOLD,
                (selected_value,),
            )
        elif directive is not None:
            selected_value = directive
            source = FoldThresholdSource.DIRECTIVE
            fold_decision = EffectiveDecision(
                selected_value,
                DecisionOrigin.AUTHORED,
                True,
                DecisionReason.AUTHOR_DIRECTIVE,
                (selected_value,),
            )
        else:
            selected_value = 15
            source = FoldThresholdSource.DEFAULT
            fold_decision = EffectiveDecision(
                selected_value,
                DecisionOrigin.INFERRED,
                False,
                DecisionReason.DEFAULT_FOLD_THRESHOLD,
            )

        line_decision = self.line_order_decision
        directive_line_order = (
            line_decision.value
            if line_decision is not None
            and line_decision.reason is DecisionReason.AUTHOR_DIRECTIVE
            else None
        )
        line_value: LineOrder
        if caller_line_order is not None:
            line_value = caller_line_order
            line_source = LineOrderSource.CALLER
            line_decision = EffectiveDecision(
                line_value,
                DecisionOrigin.AUTHORED,
                True,
                DecisionReason.CALLER_LINE_ORDER,
                (line_value,),
            )
        elif directive_line_order is not None:
            line_value = directive_line_order
            line_source = LineOrderSource.DIRECTIVE
        else:
            line_value = "definition"
            line_source = LineOrderSource.DEFAULT
            line_decision = EffectiveDecision(
                line_value,
                DecisionOrigin.INFERRED,
                False,
                DecisionReason.DEFAULT_LINE_ORDER,
            )

        self.authored = AuthoredLayoutIntent(
            grids=grids,
            directions=directions,
            port_hints=tuple(port_hints),
            connector_sides=tuple(connector_sides),
            fold_threshold=FoldThresholdIntent(
                directive_value=directive,
                caller_value=caller_fold_threshold,
                selected_value=selected_value,
                selected_source=source,
            ),
            line_order=LineOrderIntent(
                directive_value=directive_line_order,
                caller_value=caller_line_order,
                selected_value=line_value,
                selected_source=line_source,
                authored_line_ids=routes.line_ids,
            ),
        )
        self.fold_threshold_decision = fold_decision
        self.line_order_decision = line_decision

    @staticmethod
    def endpoint_key(
        connector_id: ConnectorId, role: ConnectorEndpointRole
    ) -> ConnectorEndpointKey:
        """Build the canonical key for one connector boundary."""
        return ConnectorEndpointKey(connector_id, role)

    def authored_endpoint_values(
        self, endpoint: ConnectorEndpointKey
    ) -> tuple[PortSide, ...]:
        """Return the snapshot's authored side options for *endpoint*."""
        if self.authored is None:
            return ()
        return self.authored.endpoint_values(endpoint)

    def record_inferred_grid(
        self,
        section_id: str,
        value: GridCell,
        reason: DecisionReason = DecisionReason.AUTO_GRID,
        *,
        locked: bool = False,
    ) -> None:
        """Record an engine-selected grid cell unless the author owns it."""
        current = self.grids.get(section_id)
        if current is not None and current.is_author_owned:
            if current.value != value:
                raise ValueError(
                    f"cannot replace author-owned grid for {section_id!r}: "
                    f"{current.value!r} -> {value!r}"
                )
            return
        authored_values = current.authored_values if current is not None else ()
        self.grids[section_id] = EffectiveDecision(
            value, DecisionOrigin.INFERRED, locked, reason, authored_values
        )

    def record_inferred_direction(
        self,
        section_id: str,
        value: str,
        reason: DecisionReason = DecisionReason.AUTO_DIRECTION,
        *,
        locked: bool = False,
    ) -> None:
        """Record an engine-selected direction and its lock state."""
        current = self.directions.get(section_id)
        if current is not None and current.is_author_owned:
            if current.value != value:
                raise ValueError(
                    f"cannot replace author-owned direction for {section_id!r}: "
                    f"{current.value!r} -> {value!r}"
                )
            return
        authored_values = current.authored_values if current is not None else ()
        self.directions[section_id] = EffectiveDecision(
            value, DecisionOrigin.INFERRED, locked, reason, authored_values
        )

    def record_committed_grid(
        self,
        section_id: str,
        value: GridCell,
        origin: DecisionOrigin,
        reason: DecisionReason,
    ) -> None:
        """Record a caller or candidate grid pin before inference."""
        current = self.grids.get(section_id)
        authored_values = current.authored_values if current is not None else ()
        self.grids[section_id] = EffectiveDecision(
            value, origin, True, reason, authored_values
        )

    def record_committed_direction(
        self,
        section_id: str,
        value: str,
        origin: DecisionOrigin,
        reason: DecisionReason,
    ) -> None:
        """Record a caller or candidate direction pin before inference."""
        current = self.directions.get(section_id)
        authored_values = current.authored_values if current is not None else ()
        self.directions[section_id] = EffectiveDecision(
            value, origin, True, reason, authored_values
        )

    def record_candidate_fold_threshold(self, value: int) -> None:
        """Record a candidate fold pin when no author or caller owns it."""
        current = self.fold_threshold_decision
        authored_values = current.authored_values if current is not None else ()
        self.fold_threshold_decision = EffectiveDecision(
            value,
            DecisionOrigin.INFERRED,
            True,
            DecisionReason.CANDIDATE_COMMITMENT,
            authored_values,
        )

    def record_candidate_line_order(self, value: LineOrder) -> None:
        """Record a candidate line-order pin when no author or caller owns it."""
        current = self.line_order_decision
        authored_values = current.authored_values if current is not None else ()
        self.line_order_decision = EffectiveDecision(
            value,
            DecisionOrigin.INFERRED,
            True,
            DecisionReason.CANDIDATE_COMMITMENT,
            authored_values,
        )

    def record_endpoint_selection(
        self,
        endpoint: ConnectorEndpointKey,
        selection: EndpointSideSelection,
        authored_values: tuple[PortSide, ...],
    ) -> None:
        """Record the typed resolver selection for one connector boundary."""
        origin = selection.origin
        locked = selection.locked
        reason = selection.reason
        authored_matches = bool(authored_values) and all(
            value is selection.side for value in authored_values
        )
        caller_owned = reason is DecisionReason.CALLER_COMMITMENT
        if (
            origin is DecisionOrigin.AUTHORED
            and not authored_matches
            and not caller_owned
        ):
            origin = DecisionOrigin.INFERRED
            locked = False
            reason = DecisionReason.RESOLUTION_SIDE_SELECTION
        self.connector_sides[endpoint] = EffectiveDecision(
            selection.side, origin, locked, reason, authored_values
        )

    def grid_decision(self, section_id: str) -> EffectiveDecision[GridCell] | None:
        return self.grids.get(section_id)

    def direction_decision(self, section_id: str) -> EffectiveDecision[str] | None:
        return self.directions.get(section_id)

    def endpoint_decision(
        self, endpoint: ConnectorEndpointKey
    ) -> EffectiveDecision[PortSide] | None:
        return self.connector_sides.get(endpoint)

    @property
    def effective_fold_threshold(self) -> int:
        """Return the selected threshold after caller/directive precedence."""
        if self.fold_threshold_decision is None:
            return 15
        return self.fold_threshold_decision.value

    def author_owns_grid(self, section_id: str) -> bool:
        decision = self.grid_decision(section_id)
        return decision is not None and decision.is_author_owned

    def has_authored_grids(self) -> bool:
        """Whether any effective grid commitment is author-owned."""
        return any(decision.is_author_owned for decision in self.grids.values())

    def author_owns_direction(self, section_id: str) -> bool:
        decision = self.direction_decision(section_id)
        return decision is not None and decision.is_author_owned

    def direction_is_locked(self, section_id: str) -> bool:
        decision = self.direction_decision(section_id)
        return decision is not None and decision.is_reinference_locked

    def direction_has_reason(self, section_id: str, reason: DecisionReason) -> bool:
        decision = self.direction_decision(section_id)
        return decision is not None and decision.reason is reason

    def complete_section_inference(
        self,
        graph: MetroGraph,
        fold_sections: set[str],
    ) -> None:
        """Record all effective grid and direction decisions in section order."""
        for section_id, section in graph.sections.items():
            if section_id in graph.grid_overrides:
                current_grid = self.grid_decision(section_id)
                if current_grid is None:
                    self.record_inferred_grid(
                        section_id, graph.grid_overrides[section_id]
                    )

            current_direction = self.direction_decision(section_id)
            if current_direction is not None:
                continue
            if self.author_owns_grid(section_id):
                self.record_inferred_direction(
                    section_id,
                    section.direction,
                    DecisionReason.EXPLICIT_GRID_DEFAULT_DIRECTION,
                )
            else:
                reason = (
                    DecisionReason.FOLD_BRIDGE_DIRECTION
                    if section_id in fold_sections
                    else DecisionReason.AUTO_DIRECTION
                )
                self.record_inferred_direction(section_id, section.direction, reason)

    def validate_complete(self, graph: MetroGraph) -> None:
        """Raise when an effective layout commitment lacks typed provenance."""
        if (
            self.authored is None
            or self.fold_threshold_decision is None
            or self.line_order_decision is None
        ):
            raise ValueError("layout provenance was not captured before inference")
        missing_directions = [
            sid for sid in graph.sections if sid not in self.directions
        ]
        missing_grids = [
            sid
            for sid in graph.grid_overrides
            if sid in graph.sections and sid not in self.grids
        ]
        missing_endpoints: list[ConnectorEndpointKey] = []
        topology = graph.route_topology
        if topology is not None:
            for connector in topology.connectors:
                for role in ConnectorEndpointRole:
                    endpoint = ConnectorEndpointKey(connector.id, role)
                    if endpoint not in self.connector_sides:
                        missing_endpoints.append(endpoint)
        if missing_directions or missing_grids or missing_endpoints:
            raise ValueError(
                "incomplete layout provenance: "
                f"directions={missing_directions}, grids={missing_grids}, "
                f"connector_endpoints={missing_endpoints}"
            )
