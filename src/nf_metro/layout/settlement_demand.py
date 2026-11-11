"""What envelope settlement is asked for, and the grid axis each demand names.

Held apart from :mod:`nf_metro.layout.envelope_settlement` so that the layout
phases which *measure* a demand can state one without importing the routing
stack that settlement's other demand -- the ``RouteReservation`` ledger -- is
built on.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from nf_metro.parser.model import MetroGraph


class SettlementAxis(Enum):
    """The grid axis a boundary separates, and the coordinate it translates."""

    ROW = "row"
    COLUMN = "column"


class BoundaryClearanceRequirementKind(Enum):
    """The planner contract that owns a stable boundary requirement."""

    GENERAL = "general"
    CORRIDOR_COHORT_APERTURE = "corridor-cohort-aperture"


@dataclass(frozen=True, slots=True)
class BoundaryClearanceDemand:
    """Clearance one grid boundary owes between the boxes facing across it.

    A ``RouteReservation`` states what a *run* crossing a boundary needs there,
    and settlement's other demand is this: what the boundary owes whether or not
    a run crosses it at all.  The two are deficits at the same boundary, paid by
    the same translation, so settlement widens once by the larger rather than
    letting a second owner widen it again.

    Measured, not declared.  ``deficit`` is how far short the live boxes are of
    ``required``, and a demand exists only while that is positive, so a satisfied
    boundary states nothing.  ``blocker_section_ids`` names the sections holding
    the demand from above, for attribution.
    """

    axis: SettlementAxis
    boundary: int
    required: float
    deficit: float
    blocker_section_ids: tuple[str, ...]
    description: str
    owner_id: str = ""

    def __post_init__(self) -> None:
        if self.boundary < 1:
            raise ValueError("a boundary clearance demand needs a side above it")
        if not math.isfinite(self.deficit) or self.deficit <= 0:
            raise ValueError("a boundary clearance demand states a positive deficit")
        if not math.isfinite(self.required) or self.required < 0:
            raise ValueError("a boundary clearance demand states a finite requirement")
        if not self.description:
            raise ValueError("a boundary clearance demand states what it is owed for")


@dataclass(frozen=True, slots=True)
class BoundaryClearanceRequirement:
    """Stable clearance target measured between two section sets."""

    axis: SettlementAxis
    boundary: int
    owner_id: str
    required: float
    negative_section_ids: tuple[str, ...]
    positive_section_ids: tuple[str, ...]
    description: str
    kind: BoundaryClearanceRequirementKind = BoundaryClearanceRequirementKind.GENERAL

    def __post_init__(self) -> None:
        if self.boundary < 1:
            raise ValueError("a boundary clearance requirement needs two sides")
        if not self.owner_id:
            raise ValueError("a boundary clearance requirement names its owner")
        if not math.isfinite(self.required) or self.required < 0:
            raise ValueError("a boundary clearance requirement is finite")
        if not self.negative_section_ids or not self.positive_section_ids:
            raise ValueError("a boundary clearance requirement names both sides")
        if not self.description:
            raise ValueError("a boundary clearance requirement states its cause")


ClearanceMeasurement = Callable[[MetroGraph], tuple[BoundaryClearanceDemand, ...]]
"""Re-measures every boundary's clearance demand against the live boxes.

Settlement re-measures a reservation at each boundary it visits rather than
trusting a figure taken before its own earlier translations; a clearance demand
is re-measured the same way and for the same reason.
"""
