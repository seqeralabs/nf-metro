"""The router places a reserved corridor from its reservation, on either axis.

A ``RouteReservation`` measures the blockers that bound its corridor over the
corridor's own declared span.  The re-route has to land the channel in the band
that reservation realises rather than re-deriving one from the row or column
edges it happens to have in hand -- those edges name whichever sections sit in
the two grid rows or columns, which is a different, and here a wrong, set of
blockers.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from nf_metro.api import prepare_graph, resolve_theme
from nf_metro.layout.constants import (
    COORD_TOLERANCE,
    INTER_ROW_EDGE_CLEARANCE,
    INTER_ROW_HEADER_CLEARANCE,
)
from nf_metro.layout.route_plan import build_route_plan_query
from nf_metro.layout.route_reservations import (
    ColumnGapRegion,
    RowGapRegion,
    drawn_corridor_containment,
)
from nf_metro.layout.routing import common
from nf_metro.layout.routing.common import (
    _center_inter_row_channel,
    centre_inter_column_channel,
    column_gap_midpoint,
)
from nf_metro.layout.routing.reserved_bands import (
    ReservedBand,
    ReservedBands,
    build_reserved_corridors,
    resolved_band,
)
from nf_metro.render.svg import build_observed_render_plan

ROOT = Path(__file__).parents[1]

# A planned fold publishes row corridors on both boundaries crossed by its
# descending branch. The router must consume those ledger bands directly.
ROW_CORRIDOR_FIXTURE = ROOT / "examples" / "topologies" / "convergence_fold_diamond.mmd"
ROW_CORRIDOR_BOUNDARY = 2

ROW_CORRIDOR_BOUNDING_BOXES = {
    1: (("branch_left",), ("branch_right",)),
    2: (("branch_right",), ("finish",)),
}

# A same-row bypass trunk whose gap the ledger sizes to exactly the bundle it
# carries, so its band is one coordinate wide, and the section bottoms the trunk
# would size its own depth from sit an OFFSET_STEP shallower than that.
OFF_BAND_TRUNK_FIXTURE = ROOT / "examples" / "topologies" / "merge_pullaway.mmd"
OFF_BAND_TRUNK_BOUNDARY = 1

# A fold whose branch column is entered from a section spanning the boundary,
# so the corridor's own blockers sit 7.5px inboard of the raw column midpoint.
RESERVED_COLUMN_FIXTURE = (
    ROOT / "examples" / "topologies" / "convergence_fold_diamond.mmd"
)
RESERVED_COLUMN_BOUNDARY = 1


def _rendered(path: Path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        return build_observed_render_plan(graph, resolve_theme(None, graph))


def _containment(observed, reservation, realised):
    """Where *reservation*'s drawn run sits inside the band it realises."""
    return drawn_corridor_containment(
        reservation, realised, observed.plan.route_polylines, reservation.claims
    )


def _column_gap_realisations(route_plan, right_column: int):
    query = build_route_plan_query(route_plan)
    for reservation in route_plan.reservations:
        region = reservation.region
        if (
            not isinstance(region, ColumnGapRegion)
            or region.right_column != right_column
        ):
            continue
        realised = query.realised_reservation(reservation.id)
        if realised is not None:
            yield reservation, realised


def _row_gap_realisations(route_plan, lower_row: int):
    query = build_route_plan_query(route_plan)
    for reservation in route_plan.reservations:
        region = reservation.region
        if not isinstance(region, RowGapRegion) or region.lower_row != lower_row:
            continue
        realised = query.realised_reservation(reservation.id)
        if realised is not None:
            yield reservation, realised


def test_reserved_row_corridor_lands_on_the_band_its_reservation_realises() -> None:
    observed = _rendered(ROW_CORRIDOR_FIXTURE)
    found = list(_row_gap_realisations(observed.route_plan, ROW_CORRIDOR_BOUNDARY))
    assert found, "fixture no longer reserves the corridor under test"
    for reservation, realised in found:
        drawn = _containment(observed, reservation, realised)
        assert (drawn.drawn_start + drawn.drawn_end) / 2 == pytest.approx(
            (drawn.band_start + drawn.band_end) / 2, abs=0.01
        )


def test_reserved_row_corridor_keeps_both_of_its_declared_clearances() -> None:
    """The consequence the raw row edges could not deliver.

    Deriving the band from the row edges leaves the run inside the clearance it
    owes the section that actually bounds it, so the drawn run would sit closer
    to that section than the reservation permits even while the corridor's
    total capacity is ample.
    """
    observed = _rendered(ROW_CORRIDOR_FIXTURE)
    for reservation, realised in _row_gap_realisations(
        observed.route_plan, ROW_CORRIDOR_BOUNDARY
    ):
        drawn = _containment(observed, reservation, realised)
        assert drawn.negative_side_slack >= -0.01
        assert drawn.positive_side_slack >= -0.01


def test_a_reserved_band_is_used_without_consulting_the_raw_gap(monkeypatch) -> None:
    """The narrow-gap fallback is unreachable for a corridor that owns a band.

    The fallback is guarded by ``_inter_row_band_fits`` on the raw edges, so a
    reserved channel that never asks that question can never take it.
    """

    def _refuse(*_args: float) -> bool:
        raise AssertionError("a reserved corridor consulted the raw gap")

    monkeypatch.setattr(common, "_inter_row_band_fits", _refuse)
    # Raw edges far too close together for either clearance: without the
    # reservation this is exactly the case that biases the run to the header.
    placed = _center_inter_row_channel(
        100.0, 110.0, reserved=ReservedBand(200.0, 260.0)
    )
    assert placed == pytest.approx(230.0)


def test_a_reserved_band_keeps_an_oversized_stagger_distinct() -> None:
    """Two lanes of one bundle never resolve onto a single coordinate.

    A boundary band is the intersection of every claim crossing it, so it can be
    narrower than a bundle claiming it, and clamping each lane into it in turn
    would seat them all on the same coordinate: the two lines would draw as one
    stroke and one of them would be invisible.  Distinctness is kept and the
    overrun is what ``assert_reservations_are_settled`` reports.
    """
    band = ReservedBand(200.0, 260.0)
    centre = _center_inter_row_channel(0.0, 0.0, 0.0, reserved=band)
    assert centre == pytest.approx(230.0)
    lanes = [
        _center_inter_row_channel(0.0, 0.0, offset, reserved=band)
        for offset in (-400.0, -4.0, 4.0, 400.0)
    ]
    assert lanes == sorted(lanes)
    assert len(set(lanes)) == len(lanes)
    assert lanes == pytest.approx([-170.0, 226.0, 234.0, 630.0])


def test_a_lone_reserved_run_is_held_inside_its_band() -> None:
    """Containment applies to a run with no stagger to keep distinct."""
    band = ReservedBand(200.0, 260.0)
    assert band.hold(400.0) == band.hi
    assert band.hold(0.0) == band.lo
    assert band.hold(230.0) == pytest.approx(230.0)


def test_a_band_narrower_than_nothing_cannot_be_built() -> None:
    with pytest.raises(ValueError, match="narrower than nothing"):
        ReservedBand(260.0, 200.0)


def test_an_unclaimed_boundary_reports_no_band() -> None:
    bands = ReservedBands({2: ReservedBand(200.0, 260.0)})
    assert bands.at(2) == ReservedBand(200.0, 260.0)
    assert bands.at(3) is None
    assert bands.at(None) is None


def test_published_bands_are_the_reservation_clearances_at_the_boundary() -> None:
    """What the router reads back is the clearance the bounding boxes leave.

    The boxes that bound each of this fixture's two row boundaries are named
    here and the band is derived from their drawn edges, so the expectation is
    the arrangement on the page rather than a second run of the ledger's own
    arithmetic.
    """
    observed = _rendered(ROW_CORRIDOR_FIXTURE)
    graph = observed.plan.graph
    bands = build_reserved_corridors(graph, observed.route_plan).rows
    assert set(bands.bands) == set(ROW_CORRIDOR_BOUNDING_BOXES)
    for lower_row, (above, below) in ROW_CORRIDOR_BOUNDING_BOXES.items():
        band = bands.bands[lower_row]
        expected_lo = (
            max(
                graph.sections[key].bbox_y + graph.sections[key].bbox_h for key in above
            )
            + INTER_ROW_EDGE_CLEARANCE
        )
        expected_hi = (
            min(graph.sections[key].bbox_y for key in below)
            - INTER_ROW_HEADER_CLEARANCE
        )
        assert band.lo == pytest.approx(expected_lo)
        assert band.hi == pytest.approx(expected_hi)
        assert band.hi >= band.lo


def test_row_gap_clearances_are_the_ones_the_raw_derivation_uses() -> None:
    """The reservation and the raw derivation differ only in their blockers."""
    observed = _rendered(ROW_CORRIDOR_FIXTURE)
    for reservation, _realised in _row_gap_realisations(
        observed.route_plan, ROW_CORRIDOR_BOUNDARY
    ):
        assert reservation.negative_side_clearance == INTER_ROW_EDGE_CLEARANCE
        assert reservation.positive_side_clearance == INTER_ROW_HEADER_CLEARANCE


def test_a_reserved_column_corridor_lands_on_the_band_its_reservation_realises() -> (
    None
):
    """The column-axis twin of the row corridor above.

    The raw column midpoint is bounded by whichever sections occupy the two
    columns; the reservation is bounded by the blockers over the corridor's own
    run, and here the two disagree.
    """
    observed = _rendered(RESERVED_COLUMN_FIXTURE)
    found = list(
        _column_gap_realisations(observed.route_plan, RESERVED_COLUMN_BOUNDARY)
    )
    assert found, "fixture no longer reserves the corridor under test"
    raw = column_gap_midpoint(
        observed.plan.graph, RESERVED_COLUMN_BOUNDARY - 1, RESERVED_COLUMN_BOUNDARY
    )
    for reservation, realised in found:
        drawn = _containment(observed, reservation, realised)
        centre = (drawn.band_start + drawn.band_end) / 2
        assert raw != pytest.approx(centre, abs=0.01), (
            "fixture no longer distinguishes the reservation from the raw gap"
        )
        assert (drawn.drawn_start + drawn.drawn_end) / 2 == pytest.approx(
            centre, abs=0.01
        )


def test_a_reserved_column_band_is_used_without_consulting_the_raw_gap() -> None:
    observed = _rendered(RESERVED_COLUMN_FIXTURE)
    graph = observed.plan.graph
    band = ReservedBand(500.0, 560.0)
    reserved = ReservedBands({RESERVED_COLUMN_BOUNDARY: band})
    placed = centre_inter_column_channel(
        graph,
        RESERVED_COLUMN_BOUNDARY - 1,
        RESERVED_COLUMN_BOUNDARY,
        reserved=reserved,
    )
    assert placed == pytest.approx(530.0)


def test_a_column_corridor_spanning_further_than_one_boundary_keeps_the_raw_gap() -> (
    None
):
    """Only adjacent columns name one boundary, so only they claim a band."""
    observed = _rendered(RESERVED_COLUMN_FIXTURE)
    graph = observed.plan.graph
    reserved = ReservedBands({2: ReservedBand(500.0, 560.0)})
    assert centre_inter_column_channel(graph, 0, 2, reserved=reserved) == pytest.approx(
        column_gap_midpoint(graph, 0, 2)
    )


def test_a_bypass_trunk_is_held_on_the_band_its_reservation_realises() -> None:
    """A trunk depth derived from section bottoms is held inside its band.

    The trunk sizes its own depth from the boxes it has to clear plus a
    clearance, which is a proxy for the blockers the reservation measured; the
    reservation's answer is the one the corridor is allocated.
    """
    observed = _rendered(OFF_BAND_TRUNK_FIXTURE)
    found = list(_row_gap_realisations(observed.route_plan, OFF_BAND_TRUNK_BOUNDARY))
    assert found, "fixture no longer reserves the corridor under test"
    for reservation, realised in found:
        drawn = _containment(observed, reservation, realised)
        assert drawn.negative_side_slack >= -0.01
        assert drawn.positive_side_slack >= -0.01


def test_a_boundary_whose_claims_cannot_be_reconciled_publishes_nothing() -> None:
    """Two claims over one corridor demanding disjoint bands leave no coordinate.

    Sizing a corridor for fewer lanes than it carries is what produces that, and
    a band is the wrong thing to answer it with: any span published here would be
    one no channel can satisfy, and the row or column edges are not an authority
    either.  Naming the conflict is left to the closing guard.
    """
    assert resolved_band(200.0, 260.0) == ReservedBand(200.0, 260.0)
    assert resolved_band(230.0, 230.0) == ReservedBand(230.0, 230.0)
    assert resolved_band(230.0, 230.0 - COORD_TOLERANCE / 2) is not None
    assert resolved_band(260.0, 200.0) is None
