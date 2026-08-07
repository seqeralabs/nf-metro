"""Gap corridors the reservation ledger allocates, as the router reads them.

A ``RouteReservation`` in a :class:`~nf_metro.layout.route_reservations.RowGapRegion`
or :class:`~nf_metro.layout.route_reservations.ColumnGapRegion` names the grid
boundary its corridor crosses, the blockers that bound it over the corridor's own
declared span, and the clearance it must keep from each of them.  Measuring that
record against live geometry yields the clear span a channel in that gap may
occupy.  The router places its channel inside that span rather than deriving one
from the row or column edges it happens to have in hand: those edges are a proxy
that over-states the obstruction wherever a section spans the boundary or sits
outside the corridor's run, and a proxy narrow enough to hold no channel at all
is what drives the header-biased fallback in ``_center_inter_row_channel``.

Both axes read the same way -- a boundary index, a clear span, and the same
clamp -- so one measurement serves rows and columns, and
:class:`ReservedCorridors` is only the pair of axis results.

A boundary is only readable this way once a ledger exists, which is after the
first routing pass has published one, so only the re-route
(``_settle_render_geometry``) consumes bands.  Where envelope settlement has
translated rows or columns to make a corridor fit, re-deriving the band would
discard the allocation it was just given; where the corridor already fitted, the
band remains a measurement over the corridor's own span rather than over the two
rows or columns as a whole.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeAlias

from nf_metro.layout.constants import COORD_TOLERANCE

if TYPE_CHECKING:
    from nf_metro.layout.route_plan import RoutePlan
    from nf_metro.layout.route_reservations import (
        CorridorRegion,
        GapCorridorBand,
        ReservationCoordinateTranslation,
        RouteReservation,
    )
    from nf_metro.parser.model import MetroGraph


@dataclass(frozen=True, slots=True)
class ReservedBand:
    """The clear span a boundary's reservations leave for a channel."""

    lo: float
    hi: float

    def __post_init__(self) -> None:
        if self.hi < self.lo - COORD_TOLERANCE:
            raise ValueError("a reserved band cannot be narrower than nothing")

    def hold(self, coordinate: float) -> float:
        """*coordinate* itself when it is inside the band, else its nearer edge."""
        return min(max(coordinate, self.lo), self.hi)

    def place(self, offset: float) -> float:
        """The channel coordinate at *offset* from the band centre.

        The band says where a bundle may run; *offset* is one lane's place
        within that bundle.  A band narrower than the stagger cannot hold every
        lane, and clamping each in turn would seat them all on one coordinate,
        drawing co-travelling distinct lines as a single stroke and hiding one of
        them.  So the stagger is kept and the overrun is left for the closing
        guard, which is the same choice :func:`_hold_bundle_in_claim_band` makes
        for a bundle whose band admits no travel.  Containment of the bundle as a
        whole belongs to the caller that knows its width; a lone run with no
        stagger goes through :meth:`hold` instead.
        """
        return (self.lo + self.hi) / 2 + offset


def held_in_reserved_band(coordinate: float, band: ReservedBand | None) -> float:
    """*coordinate* held inside *band*; *coordinate* itself where *band* is absent.

    The router derives a channel's clearance from the section edges it has to
    hand, which is a proxy for the blockers a reservation measured over the
    corridor's own span.  Where the two disagree the reservation wins, so a
    proxy-derived clearance floor is applied through this rather than to the
    bare coordinate.
    """
    return coordinate if band is None else band.hold(coordinate)


def corridor_clearance_band(
    graph: MetroGraph,
    *,
    axis: int,
    section_ids: Sequence[str],
    coordinate: float,
    run_start: float,
    run_end: float,
) -> GapCorridorBand | None:
    """The gap boundary a run occupies, and the clearance band it owes there.

    Read from live geometry through the ledger's own arithmetic
    (:func:`~nf_metro.layout.route_reservations.gap_corridor_clearance_band`), so
    a run held inside the band satisfies the reservation raised over it on either
    routing pass -- including the first, which publishes the ledger and so has
    none to read.

    *axis* is the run's own coordinate index, which is what picks the boundary's
    orientation: a horizontal run (``1``) sits in a row gap, a vertical one
    (``0``) in a column gap.  *section_ids* are the run's own endpoint sections,
    whose grid extent is the span the boundary's blockers are selected over; at
    least one is required, since a run between no sections spans no grid.
    """
    from nf_metro.layout.route_plan import grid_span_for_sections
    from nf_metro.layout.route_reservations import (
        CorridorOrientation,
        canvas_corridor_clearance_band,
        gap_corridor_clearance_band,
    )

    orientation = (
        CorridorOrientation.HORIZONTAL if axis == 1 else CorridorOrientation.VERTICAL
    )
    run_lo, run_hi = sorted((run_start, run_end))
    gap = gap_corridor_clearance_band(
        graph,
        orientation,
        grid_span_for_sections(graph, section_ids),
        coordinate,
        run_lo,
        run_hi,
    )
    return gap or canvas_corridor_clearance_band(
        graph,
        orientation,
        coordinate,
        run_lo,
        run_hi,
    )


@dataclass(frozen=True, slots=True)
class ReservedBands:
    """Realised gap corridors on one axis, keyed by the boundary they cross."""

    bands: Mapping[int, ReservedBand] = field(default_factory=dict)

    def at(self, boundary: int | None) -> ReservedBand | None:
        """The band reserved at *boundary*, or ``None`` when unclaimed.

        A boundary is named by the higher of the two grid indices it separates:
        the lower row of a row gap, the right column of a column gap.
        """
        if boundary is None:
            return None
        return self.bands.get(boundary)


ClaimSegmentKey: TypeAlias = tuple[str, str, str, int]
"""One emitted path segment: the path's edge key plus a point-pair rank.

The edge key ``(source, target, line_id)`` identifies the emitted path across
the whole routing pipeline -- unlike the path's list rank, it survives the
covered-hop drop at the end of the pass -- and the rank names one
``points[rank] .. points[rank + 1]`` pair of that path.
"""


EdgeKey: TypeAlias = tuple[str, str, str]
"""An emitted path's ``(source, target, line_id)`` identity."""


@dataclass(frozen=True, slots=True)
class ReservedCorridors:
    """Both axes' realised gap corridors, plus each claim's own band.

    ``rows`` / ``columns`` answer "what is clear at this boundary" -- the
    intersection of every claim crossing it -- which serves unclaimed geometry
    and the single-channel handlers.  ``per_claim`` answers "what band does
    this specific emitted segment own": several independent corridors crossing
    one boundary each keep their own reservation's band, so a pass allocating
    them together reads each bundle's allocation instead of one boundary-wide
    intersection.  ``row_bands_by_edge`` answers the same question before the
    segment rank exists -- a bypass trunk whose depth is computed ahead of the
    route that carries it -- and is decisive only where the edge's row claims
    agree on one band.
    """

    rows: ReservedBands = field(default_factory=ReservedBands)
    columns: ReservedBands = field(default_factory=ReservedBands)
    per_claim: Mapping[ClaimSegmentKey, ReservedBand] = field(default_factory=dict)
    row_bands_by_edge: Mapping[EdgeKey, tuple[ReservedBand, ...]] = field(
        default_factory=dict
    )
    column_bands_by_edge: Mapping[EdgeKey, tuple[ReservedBand, ...]] = field(
        default_factory=dict
    )

    def for_segment(
        self, source: str, target: str, line_id: str, rank: int
    ) -> ReservedBand | None:
        """The band the claim covering this emitted segment realises, if any."""
        return self.per_claim.get((source, target, line_id, rank))

    def claimed_row_band(
        self, source: str, target: str, line_id: str
    ) -> ReservedBand | None:
        """The edge's row-gap band, when it claims exactly one row corridor."""
        bands = self.row_bands_by_edge.get((source, target, line_id), ())
        return bands[0] if len(bands) == 1 else None

    def claimed_column_bands(
        self, source: str, target: str, line_id: str
    ) -> tuple[ReservedBand, ...]:
        """Every distinct column-gap band claimed by one emitted edge."""
        return self.column_bands_by_edge.get((source, target, line_id), ())


def _measured_gap_bands(
    graph: MetroGraph,
    plan: RoutePlan,
    translations: tuple[ReservationCoordinateTranslation, ...],
) -> Iterator[tuple[RouteReservation, float, float]]:
    """Every gap reservation in *plan* with the band it leaves clear.

    A band is the corridor region inset by each side's clearance, which is what
    a channel placed there may occupy.  Canvas corridors are excluded: their
    region is a margin against the canvas edge rather than a boundary a channel
    is allocated within.
    """
    from nf_metro.layout.route_reservations import (
        ColumnGapRegion,
        RowGapRegion,
        realise_reservation,
    )

    for reservation in plan.reservations:
        if not isinstance(reservation.region, RowGapRegion | ColumnGapRegion):
            continue
        realised = realise_reservation(
            graph, reservation, coordinate_translations=translations
        )
        if realised is None:
            continue
        yield (
            reservation,
            realised.region_start + reservation.negative_side_clearance,
            realised.region_end - reservation.positive_side_clearance,
        )


def resolved_band(lo: float, hi: float) -> ReservedBand | None:
    """``ReservedBand(lo, hi)``, or ``None`` where the two claims disagree.

    Every intersection of claims in this module resolves through here, so the
    one case that publishes nothing has one definition.  An inverted pair means
    two claims over one corridor demand disjoint bands: the corridor is sized for
    fewer lanes than it carries, and no coordinate satisfies both.

    Publishing nothing is not licence to place a claimed corridor from the row or
    column edges instead.  Containment of the drawn geometry does not rest on
    these views at all -- it is closed by
    :func:`~nf_metro.layout.routing.normalize._hold_runs_in_corridor_clearance`,
    which measures each leg's own gap directly -- and a conflict that survives
    is reported by name, with both claimants and both widths, by
    :func:`~nf_metro.layout.phases.guards.assert_reservations_are_settled`.
    """
    return ReservedBand(lo, hi) if hi >= lo - COORD_TOLERANCE else None


def _axis_bands(
    measured: Sequence[tuple[RouteReservation, float, float]],
    boundary_of: Callable[[CorridorRegion], int | None],
) -> ReservedBands:
    """Reduce *measured* to the band each boundary *boundary_of* recognises.

    Several corridors can claim one boundary over different spans, so a
    boundary's band is the intersection of what each of them leaves clear: a
    channel there has to satisfy every claim, not the most generous one.  A
    boundary whose claims :func:`resolved_band` cannot reconcile publishes
    nothing.
    """
    spans: dict[int, tuple[float, float]] = {}
    for reservation, lo, hi in measured:
        boundary = boundary_of(reservation.region)
        if boundary is None:
            continue
        held = spans.get(boundary)
        spans[boundary] = (
            (lo, hi) if held is None else (max(held[0], lo), min(held[1], hi))
        )
    return ReservedBands(
        {
            boundary: band
            for boundary, (lo, hi) in sorted(spans.items())
            if (band := resolved_band(lo, hi)) is not None
        }
    )


@dataclass(frozen=True, slots=True)
class _ClaimViews:
    """Claim-keyed lookup tables over one plan's realised gap reservations."""

    per_claim: dict[ClaimSegmentKey, ReservedBand]
    row_bands_by_edge: dict[EdgeKey, tuple[ReservedBand, ...]]
    column_bands_by_edge: dict[EdgeKey, tuple[ReservedBand, ...]]


def _claim_views(
    plan: RoutePlan, measured: Sequence[tuple[RouteReservation, float, float]]
) -> _ClaimViews:
    """Each gap claim's own realised band, keyed by the segments it covers.

    Two claims naming one segment must both hold there, so a duplicate key keeps
    the intersection, and :func:`resolved_band` decides what an irreconcilable
    pair publishes.  The per-edge view collects each edge's distinct row bands;
    equal bands collapse, so an edge whose corridor several reservations describe
    alike reads as one allocation.
    """
    from nf_metro.layout.route_reservations import RowGapRegion

    edge_by_member = {member.id: member.edge for member in plan.members}
    spans: dict[ClaimSegmentKey, tuple[float, float]] = {}
    # Keyed by the band quantised to the comparison tolerance, so two
    # reservations describing one corridor alike collapse to a single band.
    row_by_edge: dict[EdgeKey, dict[tuple[int, int], tuple[float, float]]] = {}
    column_by_edge: dict[EdgeKey, dict[tuple[int, int], tuple[float, float]]] = {}
    for reservation, lo, hi in measured:
        is_row = isinstance(reservation.region, RowGapRegion)
        band_key = (round(lo / COORD_TOLERANCE), round(hi / COORD_TOLERANCE))
        for claim in reservation.claims:
            edge = edge_by_member[claim.member_id]
            edge_key = (edge.source, edge.target, edge.line_id)
            bands_by_edge = row_by_edge if is_row else column_by_edge
            bands_by_edge.setdefault(edge_key, {}).setdefault(band_key, (lo, hi))
            for rank in range(claim.segment_rank, claim.segment_end_rank + 1):
                key = (*edge_key, rank)
                held = spans.get(key)
                spans[key] = (
                    (lo, hi) if held is None else (max(held[0], lo), min(held[1], hi))
                )
    per_claim = {
        key: band
        for key, (lo, hi) in spans.items()
        if (band := resolved_band(lo, hi)) is not None
    }
    row_bands = {
        edge_key: tuple(
            band
            for lo, hi in bands.values()
            if (band := resolved_band(lo, hi)) is not None
        )
        for edge_key, bands in row_by_edge.items()
    }
    column_bands = {
        edge_key: tuple(
            band
            for lo, hi in bands.values()
            if (band := resolved_band(lo, hi)) is not None
        )
        for edge_key, bands in column_by_edge.items()
    }
    return _ClaimViews(per_claim, row_bands, column_bands)


def build_reserved_corridors(
    graph: MetroGraph,
    plan: RoutePlan,
    translations: tuple[ReservationCoordinateTranslation, ...] = (),
) -> ReservedCorridors:
    """Measure *plan*'s row- and column-gap reservations against live *graph*.

    *plan* is the frozen ledger settlement consumed, so its claim coordinates
    are projected through *translations* before measurement.  Every view here
    reduces one measurement of the ledger against live geometry, so it is taken
    once: re-deriving it per view would re-measure each corridor's blockers
    against every section three times over.
    """
    from nf_metro.layout.route_reservations import ColumnGapRegion, RowGapRegion

    measured = tuple(_measured_gap_bands(graph, plan, translations))
    views = _claim_views(plan, measured)
    return ReservedCorridors(
        _axis_bands(
            measured,
            lambda region: (
                region.lower_row if isinstance(region, RowGapRegion) else None
            ),
        ),
        _axis_bands(
            measured,
            lambda region: (
                region.right_column if isinstance(region, ColumnGapRegion) else None
            ),
        ),
        views.per_claim,
        views.row_bands_by_edge,
        views.column_bands_by_edge,
    )
