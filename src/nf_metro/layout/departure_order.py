"""The per-departure lane-order record and the plan that decides it.

Lines that part at a fork take their own order there.  When a junction's lines
reach one entry port in an order the bundle they leave in does not carry
concentrically, the recorded lines change sides once, at the fork, and hold
their new order all the way to the port.  Station offsets keep one lane per
line per station, so they cannot state this; a :class:`DepartureLaneOrder`
does, as a permutation of the natural (concentric) order, so every consumer
relabels its own natural ranks and none reads a frame sign.

Offsets phase 7b' (``_order_entry_ports_by_approach``) decides the records
into a :class:`DepartureOrderPlan`; which parts are decided once per layout
and which once per positioning pass is set out under "Departure record
lifetime" in ``layout/CONTRACT.md``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from nf_metro.parser.model import MetroGraph

_T = TypeVar("_T")

# The stage after which junctions stand where routing will find them, so a
# departure's order can be decided.
JUNCTIONS_POSITIONED = "5.1"


@dataclass(frozen=True)
class DepartureLaneOrder:
    """On the departure *station_id* -> *target_id*, each recorded line takes
    the lane its natural order gives another.

    ``takes_lane_of`` holds ``(line, natural line)`` pairs, in departure
    order: *line* rides the lane *natural line* would hold.  The natural order
    is the source station's order carried concentrically into the port
    (``_natural_port_order`` in the offsets phases), so the permutation is
    stated relative to the direction of travel.
    """

    station_id: str
    target_id: str
    takes_lane_of: tuple[tuple[str, str], ...]

    @property
    def key(self) -> tuple[str, str]:
        return (self.station_id, self.target_id)

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(line for line, _ in self.takes_lane_of)

    def natural_line(self, line: str) -> str:
        """The line whose natural lane *line* takes."""
        return dict(self.takes_lane_of)[line]

    def natural_rank(self, line: str, rank: Mapping[str, int]) -> int | None:
        """The *rank* of the line whose lane *line* takes, or ``None`` when
        *line* is not recorded or *rank* misses a recorded line."""
        if line not in self.lines or any(other not in rank for other in self.lines):
            return None
        return rank[self.natural_line(line)]

    def reorders(self, a: str, b: str) -> bool:
        """Whether *a* and *b* both depart here, in the opposite order to
        their natural one."""
        natural = {line: i for i, (_, line) in enumerate(self.takes_lane_of)}
        departing = {line: i for i, (line, _) in enumerate(self.takes_lane_of)}
        if a not in departing or b not in departing:
            return False
        return (departing[a] < departing[b]) != (natural[a] < natural[b])


def departure_records(
    records: Iterable[DepartureLaneOrder],
) -> dict[tuple[str, str], DepartureLaneOrder]:
    return {record.key: record for record in records}


def relabel_departures(
    records: Mapping[tuple[str, str], DepartureLaneOrder],
    values: dict[tuple[str, str, str], _T],
    take: Callable[[_T, _T], _T],
) -> None:
    """Relabel per-edge *values* through every record in place.

    *values* is keyed ``(source, target, line)``.  Each recorded line's value
    becomes ``take(own, natural)``, where *natural* is the value its natural
    line held; a record whose lines are not all present is left alone.
    """
    for record in records.values():
        keys = {
            line: (record.station_id, record.target_id, line) for line in record.lines
        }
        if not all(key in values for key in keys.values()):
            continue
        natural = {line: values[key] for line, key in keys.items()}
        for line, natural_line in record.takes_lane_of:
            values[keys[line]] = take(natural[line], natural[natural_line])


@dataclass(frozen=True)
class PortOrderDecision:
    """7b''s verdict on one entry port: the lane *order* it deals, the
    departures that reach it in that order, and whether the order is carried
    along the section's bundle (*carry_full*) or stops at the port."""

    order: tuple[str, ...]
    records: tuple[DepartureLaneOrder, ...]
    carry_full: bool


@dataclass
class DepartureOrderPlan:
    """One layout's 7b' decisions, keyed by entry port.

    A port maps to ``None`` when it was decided to take no record.  The plan is
    *sealed* once the first offsets run after junction positioning completes.
    *pass_carry* holds the carry extents of record ports with two or more
    feeders for the current positioning pass.  The decide-once and per-pass
    rules are under "Departure record lifetime" in ``layout/CONTRACT.md``.
    """

    decisions: dict[str, PortOrderDecision | None] = field(default_factory=dict)
    sealed: bool = False
    pass_carry: dict[str, bool] = field(default_factory=dict)

    def start_pass(self) -> None:
        """Forget the carry extents a previous positioning pass decided."""
        self.pass_carry.clear()

    def deciding(self, stages: Sequence[str]) -> bool:
        """Whether an offsets run at *stages* decides ports."""
        return not self.sealed and self.replaying(stages)

    def replaying(self, stages: Sequence[str]) -> bool:
        """Whether an offsets run at *stages* applies the decisions taken."""
        return JUNCTIONS_POSITIONED in stages

    def seal(self, stages: Sequence[str]) -> None:
        """Seal the plan once a run at *stages* has decided."""
        if self.replaying(stages):
            self.sealed = True


class DepartureRecordDriftError(RuntimeError):
    """A positioning pass ended holding other departure records than the
    layout's first pass.

    A record one pass keeps and another drops would leave its trace (spacing,
    bypass helpers) on a layout the final pass draws without it.
    """

    def __init__(self, changed: Iterable[tuple[str, str]]) -> None:
        self.changed = tuple(sorted(changed))
        super().__init__(
            "departure lane-order records differ between positioning passes: "
            + ", ".join(f"{source} -> {target}" for source, target in self.changed)
        )


def hold_departure_records(graph: MetroGraph) -> None:
    """Fail closed when the pass just ended holds other departure records
    than the first pass of this layout did."""
    held = frozenset(graph._departure_lane_orders)
    if graph._departure_record_keys is None:
        graph._departure_record_keys = held
    elif held != graph._departure_record_keys:
        raise DepartureRecordDriftError(held ^ graph._departure_record_keys)
