"""Plan-time landing order of the lines a bundle carries into an entry port.

Concentric turns keep each line on its own side of travel, so the order of a
bundle's lines across its direction of travel where it leaves its source is
their order where it lands.  :func:`bundle_landing_deals` deals that order over
each port's own landing slots (:func:`_entry_port_landing`), which only changes
which line takes which slot, never the slot set.  Every input is plan-time
state: station offsets, port sides, the source section's lane frame, the
perpendicular riser lateral, recorded departure orders, and the direction
sequence (never the coordinates) of the routes as emitted.

A port the deal cannot order is refused, once, with the reason; while the plan
is compiled a refused port keeps its own lane order and the reconcile's
trunk-depth riser order.  The reconciles after emission read
:func:`planned_landing_orders` instead, which takes a port as decided when the
plan lands every edge into it once, on one axis, with no line from two
sources, and otherwise leaves it to trunk depth; the two decided sets can
differ.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from enum import Enum
from itertools import groupby
from typing import TYPE_CHECKING

from nf_metro.layout.geometry import lane_delta
from nf_metro.layout.routing.common import (
    ENTRY_SIDE_DIRECTION,
    EXIT_SIDE_DIRECTION,
    Direction,
    OffsetRegime,
    leg_direction_sequence,
    right_normal_axis_sign,
)
from nf_metro.layout.routing.context import _get_offset, _section_lane_frame
from nf_metro.layout.routing.corridor_cohort_integration import (
    AcceptedCorridorOwnerKind,
)
from nf_metro.layout.routing.orientation import direction_vector
from nf_metro.layout.routing.perp import _perp_riser_lateral
from nf_metro.parser.model import PortSide

if TYPE_CHECKING:
    from nf_metro.layout.routing.context import _RoutingCtx
    from nf_metro.layout.routing.corridor_cohort_integration import (
        AcceptedCorridorCohortPlan,
        CorridorCohortTarget,
    )
    from nf_metro.parser.model import MetroGraph, Port

_SAME_SLOT = 1e-9

LandingDeals = Mapping[str, Mapping[str, float]]
"""Per decided entry port, each of its lines' landing coordinate."""


class LandingDealRefusal(Enum):
    """Why the deal leaves an entry port to its own lane order."""

    INCOMPLETE = "incomplete"
    LINE_FROM_TWO_SOURCES = "line-from-two-sources"
    JUNCTION_MULTI_UPSTREAM = "junction-multi-upstream"
    JUNCTION_TWO_RECORDS = "junction-two-records"
    SOURCE_NOT_EXIT = "source-not-exit"
    TWO_SIDED = "two-sided"
    DEFERRED = "deferred"
    SPLIT_LANDING = "split-landing"
    EXIT_TIE = "exit-tie"
    RECORD_MISMATCH = "record-mismatch"
    IMMUTABLE_MOVED = "immutable-moved"
    NOTHING_TO_DEAL = "nothing-to-deal"
    AGAINST_SIDE = "against-side"
    INTERLEAVED_SOURCES = "interleaved-sources"


class _Refused(Exception):
    def __init__(self, reason: LandingDealRefusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


def deal_by_exit_order(
    exit_side: PortSide,
    exit_laterals: Mapping[str, float],
    entry_side: PortSide,
    slots: Sequence[float],
) -> dict[str, float]:
    """Deal *slots* to the lines of *exit_laterals* in their order across travel.

    *exit_laterals* are each line's coordinate on the screen axis lateral to
    the exit; the line furthest to the right of travel there takes the slot
    furthest to the right of travel at the entry.
    """
    exit_sign = right_normal_axis_sign(EXIT_SIDE_DIRECTION[exit_side])
    entry_sign = right_normal_axis_sign(ENTRY_SIDE_DIRECTION[entry_side])
    order = sorted(exit_laterals, key=lambda line: exit_sign * exit_laterals[line])
    ranked_slots = sorted(slots, key=lambda slot: entry_sign * slot)
    return dict(zip(order, ranked_slots, strict=True))


def bundle_landing_deals(
    ctx: _RoutingCtx,
    targets: Sequence[CorridorCohortTarget],
) -> tuple[LandingDeals, Mapping[str, LandingDealRefusal]]:
    """The landing deal for every entry port *targets* land in.

    Returns the decided ports, each line's landing (a port whose deal is its
    own lane order among them), and the refused ports with their reason.
    """
    by_port: defaultdict[str, list[CorridorCohortTarget]] = defaultdict(list)
    for target in targets:
        if (
            target.endpoint_lane_axis is not None
            and target.endpoint_lane_coordinate is not None
        ):
            by_port[target.edge_key[1]].append(target)
    decided: dict[str, Mapping[str, float]] = {}
    refused: dict[str, LandingDealRefusal] = {}
    for port_id, port_targets in by_port.items():
        port = ctx.graph.ports.get(port_id)
        if port is None or not port.is_entry:
            continue
        try:
            decided[port_id] = _port_deal(ctx, port, port_targets)
        except _Refused as refusal:
            refused[port_id] = refusal.reason
    return decided, refused


def _port_deal(
    ctx: _RoutingCtx, port: Port, targets: Sequence[CorridorCohortTarget]
) -> dict[str, float]:
    graph = ctx.graph
    by_line: defaultdict[str, list[CorridorCohortTarget]] = defaultdict(list)
    for target in targets:
        by_line[target.edge_key[2]].append(target)
    edges = graph.edges_to(port.id)
    if set(by_line) != {edge.line_id for edge in edges}:
        raise _Refused(LandingDealRefusal.INCOMPLETE)
    lines_by_source: defaultdict[str, set[str]] = defaultdict(set)
    for edge in edges:
        lines_by_source[edge.source].add(edge.line_id)
    sourced = [line for lines in lines_by_source.values() for line in lines]
    if len(sourced) != len(set(sourced)):
        raise _Refused(LandingDealRefusal.LINE_FROM_TWO_SOURCES)
    landing: dict[str, float] = {}
    for line, line_targets in by_line.items():
        coordinate = line_targets[0].endpoint_lane_coordinate
        assert coordinate is not None
        landing[line] = coordinate
    # Each source is dealt over its own slots, so the order between sources
    # is the port's own; it is one only while no source straddles another.
    source_of = {
        line: source for source, lines in lines_by_source.items() for line in lines
    }
    own_order = sorted(landing, key=landing.__getitem__)
    runs = [source for source, _lines in groupby(own_order, key=source_of.get)]
    if len(runs) != len(lines_by_source):
        raise _Refused(LandingDealRefusal.INTERLEAVED_SOURCES)
    dealt: dict[str, float] = {}
    for source, lines in lines_by_source.items():
        if len(lines) > 1:
            dealt.update(_source_deal(ctx, port, source, sorted(lines), by_line))
    if not dealt:
        raise _Refused(LandingDealRefusal.NOTHING_TO_DEAL)
    for line, coordinate in landing.items():
        dealt.setdefault(line, coordinate)
    if any(
        not target.mutable
        and target.endpoint_lane_coordinate is not None
        and abs(dealt[line] - target.endpoint_lane_coordinate) > _SAME_SLOT
        for line, line_targets in by_line.items()
        for target in line_targets
    ):
        raise _Refused(LandingDealRefusal.IMMUTABLE_MOVED)
    return dealt


def _source_deal(
    ctx: _RoutingCtx,
    port: Port,
    source: str,
    lines: list[str],
    by_line: Mapping[str, Sequence[CorridorCohortTarget]],
) -> dict[str, float]:
    """Deal one source's *lines* over that source's own landing slots.

    A junction source is walked up to the exit port feeding it; its lines
    leave at the junction's own offsets, permuted by the junction's recorded
    departure order where one is held.
    """
    graph = ctx.graph
    records = graph._departure_lane_orders
    record = None
    root = source
    walked: set[str] = set()
    while root not in graph.ports and root not in walked:
        walked.add(root)
        upstream = {
            edge.source for edge in graph.edges_to(root) if edge.line_id in lines
        }
        if len(upstream) != 1:
            raise _Refused(LandingDealRefusal.JUNCTION_MULTI_UPSTREAM)
        held = records.get((root, port.id))
        if held is not None:
            if record is not None:
                raise _Refused(LandingDealRefusal.JUNCTION_TWO_RECORDS)
            record = held
        root = next(iter(upstream))
    exit_port = graph.ports.get(root)
    if exit_port is None or exit_port.is_entry or exit_port.section_id is None:
        raise _Refused(LandingDealRefusal.SOURCE_NOT_EXIT)
    sequences = {
        leg_direction_sequence(by_line[line][0].route.points) for line in lines
    }
    if len(sequences) != 1:
        raise _Refused(LandingDealRefusal.TWO_SIDED)
    (sequence,) = sequences
    # The order across travel is read at each end from its side's direction
    # of travel, which says nothing about a bundle that leaves or lands
    # against it.
    if sequence and (
        _runs_against(sequence[0], EXIT_SIDE_DIRECTION[exit_port.side])
        or _runs_against(sequence[-1], ENTRY_SIDE_DIRECTION[port.side])
    ):
        raise _Refused(LandingDealRefusal.AGAINST_SIDE)
    for line in lines:
        line_targets = by_line[line]
        if any(
            target.route.offset_regime is OffsetRegime.DEFERRED
            for target in line_targets
        ):
            raise _Refused(LandingDealRefusal.DEFERRED)
        if len({target.endpoint_lane_coordinate for target in line_targets}) != 1:
            raise _Refused(LandingDealRefusal.SPLIT_LANDING)
    frame = _section_lane_frame(
        graph, graph.sections[exit_port.section_id], ctx.positive_fan
    )

    def exit_lateral(line: str) -> float:
        offset = _get_offset(ctx, source, line)
        if exit_port.side in (PortSide.LEFT, PortSide.RIGHT):
            return offset
        if frame.secondary.name == "y":
            return _perp_riser_lateral(ctx, root, line, exit_port.side)
        return lane_delta(frame, offset)

    laterals = {line: exit_lateral(line) for line in lines}
    if len(set(laterals.values())) != len(lines):
        raise _Refused(LandingDealRefusal.EXIT_TIE)
    slots = []
    for line in lines:
        coordinate = by_line[line][0].endpoint_lane_coordinate
        assert coordinate is not None
        slots.append(coordinate)
    dealt = deal_by_exit_order(exit_port.side, laterals, port.side, slots)
    if record is not None:
        if set(record.lines) - set(lines):
            raise _Refused(LandingDealRefusal.RECORD_MISMATCH)
        natural = dict(dealt)
        for line, natural_line in record.takes_lane_of:
            dealt[line] = natural[natural_line]
    return dealt


def _runs_against(leg: tuple[int, int], travel: Direction) -> bool:
    dx, dy = direction_vector(travel)
    return leg == (-dx, -dy)


def landing_order(deal: Mapping[str, float]) -> tuple[str, ...]:
    """A decided port's lines in ascending landing coordinate."""
    return tuple(sorted(deal, key=deal.__getitem__))


def landing_orders_of(deals: LandingDeals) -> dict[str, tuple[str, ...]]:
    """Each decided port's lines in ascending landing coordinate."""
    return {port_id: landing_order(deal) for port_id, deal in deals.items()}


def planned_landing_orders(
    graph: MetroGraph, plan: AcceptedCorridorCohortPlan | None
) -> dict[str, tuple[str, ...]]:
    """Each entry port's lines in ascending landing coordinate, as *plan* lands them.

    A port counts only when the plan lands every edge entering it, once each,
    on one axis, and no line enters it from two sources: such a line has no
    single place in the order, which is why the deal refuses it too.  The
    reconciles after emission take such a port's order from here instead of
    re-deriving it from trunk depth.
    """
    if plan is None:
        return {}
    by_port: defaultdict[str, list[tuple[tuple[str, str], int, float]]] = defaultdict(
        list
    )
    for frame in plan.owners:
        if frame.kind is AcceptedCorridorOwnerKind.ENDPOINT_LANDING:
            source, port_id, line = frame.segment[1]
            by_port[port_id].append(
                ((source, line), frame.axis, frame.accepted_coordinate)
            )
    orders: dict[str, tuple[str, ...]] = {}
    for port_id, landed in by_port.items():
        port = graph.ports.get(port_id)
        if port is None or not port.is_entry:
            continue
        edges = sorted((edge.source, edge.line_id) for edge in graph.edges_to(port_id))
        if sorted(edge for edge, _axis, _coordinate in landed) != edges:
            continue
        if len({line for _source, line in edges}) != len(edges):
            continue
        if len({axis for _edge, axis, _coordinate in landed}) != 1:
            continue
        orders[port_id] = landing_order(
            {line: coordinate for (_source, line), _axis, coordinate in landed}
        )
    return orders
