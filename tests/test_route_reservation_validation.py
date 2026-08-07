"""Route-plan queries reject contradictory reservation ledger records."""

from __future__ import annotations

import warnings
from dataclasses import replace
from pathlib import Path

import pytest

from nf_metro.api import prepare_graph
from nf_metro.layout.route_plan import (
    CoordinateRegime,
    DemandAxis,
    GridSpan,
    ReservationDecisionKind,
    SharedReferenceKind,
    build_route_plan_query,
)
from nf_metro.layout.route_reservations import (
    CanvasRegion,
    CanvasSide,
    ColumnGapRegion,
    CorridorKind,
    CorridorMeasurementScope,
    CorridorOrientation,
    RouteReservationId,
    RowGapRegion,
)
from nf_metro.layout.routing import compute_station_offsets, observe_route_edges
from nf_metro.layout.routing.common import Direction
from nf_metro.layout.routing.families import RouteFamilyId

ROOT = Path(__file__).parents[1]
REPORT_HO = ROOT / "tests" / "fixtures" / "route_reservations" / "reportho.metro"
TOPOLOGIES = ROOT / "examples" / "topologies"
# A shortfall record exists only where a corridor exceeds the unsettled
# geometry's capacity, which is what the diagnostic checks below mutate.
DEFICIT_MAP = TOPOLOGIES / "convergence_sink_fold.mmd"


def _plan(path: Path = REPORT_HO):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        return observe_route_edges(
            graph, station_offsets=compute_station_offsets(graph)
        ).plan


def _replace_record(records, replacement, *, id_field: str = "id"):
    replacement_id = getattr(replacement, id_field)
    return tuple(
        replacement if getattr(item, id_field) == replacement_id else item
        for item in records
    )


@pytest.mark.parametrize("field", ("path_id", "path_rank"))
def test_query_rejects_claims_that_disagree_with_emitted_bindings(field: str) -> None:
    plan = _plan()
    reservation = plan.reservations[0]
    claim = reservation.claims[0]
    value = "wrong-path" if field == "path_id" else claim.path_rank + 1
    malformed_claim = replace(claim, **{field: value})
    malformed = replace(reservation, claims=(malformed_claim, *reservation.claims[1:]))

    with pytest.raises(ValueError, match="disagrees with its emitted binding"):
        build_route_plan_query(
            replace(plan, reservations=_replace_record(plan.reservations, malformed))
        )


def test_query_rejects_incomplete_connector_attribution() -> None:
    plan = _plan()
    reservation = next(
        item for item in plan.reservations if len(item.connector_ids) > 1
    )
    malformed = replace(reservation, connector_ids=reservation.connector_ids[:-1])

    with pytest.raises(ValueError, match="connector attribution is incomplete"):
        build_route_plan_query(
            replace(plan, reservations=_replace_record(plan.reservations, malformed))
        )


@pytest.mark.parametrize(
    ("field", "mutate"),
    (
        (
            "axis",
            lambda demand: (
                DemandAxis.X if demand.axis is DemandAxis.Y else DemandAxis.Y
            ),
        ),
        ("minimum_size", lambda demand: demand.minimum_size + 1.0),
        ("minimum_size_regime", lambda _demand: CoordinateRegime.SETTLED_GRID),
        ("keep_out_classes", lambda demand: tuple(reversed(demand.keep_out_classes))),
        ("provenance", lambda _demand: ()),
    ),
)
def test_query_rejects_inconsistent_symbolic_demands(field, mutate) -> None:
    plan = _plan()
    demand_id = plan.reservations[0].demand_ids[0]
    demand = next(item for item in plan.demands if item.id == demand_id)
    malformed = replace(demand, **{field: mutate(demand)})

    with pytest.raises(ValueError, match="symbolic demand is inconsistent"):
        build_route_plan_query(
            replace(plan, demands=_replace_record(plan.demands, malformed))
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("kind", SharedReferenceKind.TRUNK),
        ("coordinate_regime", CoordinateRegime.LAYOUT_CANVAS),
        ("provenance", ()),
    ),
)
def test_query_rejects_inconsistent_shared_references(field, value) -> None:
    plan = _plan()
    reservation = plan.reservations[0]
    reference = next(
        item for item in plan.shared_references if item.id == reservation.reference_id
    )
    malformed = replace(reference, **{field: value})

    with pytest.raises(ValueError, match="shared reference is inconsistent"):
        build_route_plan_query(
            replace(
                plan,
                shared_references=_replace_record(plan.shared_references, malformed),
            )
        )


def test_query_rejects_consistently_fabricated_reservation_provenance() -> None:
    plan = _plan()
    reservation = plan.reservations[0]
    reference = next(
        item for item in plan.shared_references if item.id == reservation.reference_id
    )
    demand = next(item for item in plan.demands if item.id in reservation.demand_ids)
    malformed_reservation = replace(reservation, provenance=())
    malformed_reference = replace(reference, provenance=())
    malformed_demand = replace(demand, provenance=())

    with pytest.raises(
        ValueError, match="provenance is inconsistent with the route plan"
    ):
        build_route_plan_query(
            replace(
                plan,
                reservations=_replace_record(plan.reservations, malformed_reservation),
                shared_references=_replace_record(
                    plan.shared_references, malformed_reference
                ),
                demands=_replace_record(plan.demands, malformed_demand),
            )
        )


def test_query_rejects_a_consistently_fabricated_complete_span() -> None:
    plan = _plan()
    reservation = next(
        item
        for item in plan.reservations
        if item.span.min_column < item.span.max_column
    )
    fabricated_span = GridSpan(
        reservation.span.min_column,
        reservation.span.min_column,
        reservation.span.min_row,
        reservation.span.min_row,
    )
    included_sections = {
        item.section_id
        for item in plan.provenance.sections
        if item.grid is not None
        and fabricated_span.min_column <= item.grid.value[0] + item.grid.value[3] - 1
        and item.grid.value[0] <= fabricated_span.max_column
        and fabricated_span.min_row <= item.grid.value[1] + item.grid.value[2] - 1
        and item.grid.value[1] <= fabricated_span.max_row
    }
    fabricated_provenance = tuple(
        item
        for item in reservation.provenance
        if item.kind
        not in {
            ReservationDecisionKind.SECTION_GRID,
            ReservationDecisionKind.SECTION_DIRECTION,
        }
        or item.subject_id in included_sections
    )
    reference = next(
        item for item in plan.shared_references if item.id == reservation.reference_id
    )
    demand = next(item for item in plan.demands if item.id in reservation.demand_ids)
    malformed_reservation = replace(
        reservation, span=fabricated_span, provenance=fabricated_provenance
    )
    malformed_reference = replace(reference, provenance=fabricated_provenance)
    malformed_demand = replace(
        demand, span=fabricated_span, provenance=fabricated_provenance
    )

    with pytest.raises(ValueError, match="span is inconsistent"):
        build_route_plan_query(
            replace(
                plan,
                reservations=_replace_record(plan.reservations, malformed_reservation),
                shared_references=_replace_record(
                    plan.shared_references, malformed_reference
                ),
                demands=_replace_record(plan.demands, malformed_demand),
            )
        )


def test_query_rejects_a_consistently_fabricated_clearance_policy() -> None:
    plan = _plan()
    realised_by_id = {item.reservation_id: item for item in plan.realised_reservations}
    reservation = next(item for item in plan.reservations if item.id in realised_by_id)
    demand = next(item for item in plan.demands if item.id in reservation.demand_ids)
    realised = realised_by_id[reservation.id]
    keepouts = tuple(reversed(reservation.keep_out_classes))
    malformed_reservation = replace(
        reservation,
        negative_side_clearance=reservation.negative_side_clearance + 10.0,
        minimum_width=reservation.minimum_width + 10.0,
        keep_out_classes=keepouts,
    )
    malformed_demand = replace(
        demand,
        minimum_size=demand.minimum_size + 10.0,
        keep_out_classes=keepouts,
    )
    malformed_realised = replace(
        realised,
        required_width=realised.required_width + 10.0,
        capacity_slack=realised.capacity_slack - 10.0,
        negative_side_slack=realised.negative_side_slack - 10.0,
    )

    with pytest.raises(ValueError, match="clearance policy is inconsistent"):
        build_route_plan_query(
            replace(
                plan,
                reservations=_replace_record(plan.reservations, malformed_reservation),
                demands=_replace_record(plan.demands, malformed_demand),
                realised_reservations=_replace_record(
                    plan.realised_reservations,
                    malformed_realised,
                    id_field="reservation_id",
                ),
            )
        )


def test_query_rejects_fabricated_route_family_attribution() -> None:
    plan = _plan()
    reservation = plan.reservations[0]
    fabricated = next(
        family for family in RouteFamilyId if family not in reservation.route_family_ids
    )
    malformed = replace(reservation, route_family_ids=(fabricated,))

    with pytest.raises(ValueError, match="route-family attribution is inconsistent"):
        build_route_plan_query(
            replace(plan, reservations=_replace_record(plan.reservations, malformed))
        )


def test_query_rejects_a_noncanonical_reservation_identity() -> None:
    plan = _plan()
    reservation = plan.reservations[0]
    malformed = replace(
        reservation, id=RouteReservationId("route-reservation:fabricated")
    )

    with pytest.raises(ValueError, match="canonical identity is inconsistent"):
        build_route_plan_query(
            replace(
                plan,
                reservations=tuple(
                    malformed if item.id == reservation.id else item
                    for item in plan.reservations
                ),
            )
        )


def test_topology_reservation_rejects_a_gap_outside_connector_crossings() -> None:
    plan = _plan()
    reservation = next(
        item
        for item in plan.reservations
        if item.measurement_scope is CorridorMeasurementScope.TOPOLOGY_SPAN
    )
    if reservation.orientation is CorridorOrientation.HORIZONTAL:
        region = RowGapRegion(
            reservation.span.max_row + 10, reservation.span.max_row + 11
        )
    else:
        region = ColumnGapRegion(
            reservation.span.max_column + 10,
            reservation.span.max_column + 11,
        )
    malformed = replace(reservation, region=region)

    with pytest.raises(ValueError, match="region is not crossed"):
        build_route_plan_query(
            replace(plan, reservations=_replace_record(plan.reservations, malformed))
        )


def test_reservation_rejects_direction_on_the_wrong_axis() -> None:
    plan = _plan()
    reservation = plan.reservations[0]
    direction = (
        Direction.D
        if reservation.orientation is CorridorOrientation.HORIZONTAL
        else Direction.R
    )

    with pytest.raises(ValueError, match="direction and orientation disagree"):
        replace(reservation, direction=direction)


@pytest.mark.parametrize(
    ("name", "side"),
    (
        ("lr_perp_top_exit_perp_entry_diverging.mmd", CanvasSide.TOP),
        ("fan_in_merge.mmd", CanvasSide.BOTTOM),
    ),
)
def test_canvas_reservation_kind_follows_its_side(name: str, side: CanvasSide) -> None:
    plan = _plan(TOPOLOGIES / name)
    reservation = next(
        item
        for item in plan.reservations
        if isinstance(item.region, CanvasRegion) and item.region.side is side
    )
    wrong_kind = (
        CorridorKind.BYPASS_BAND
        if side is CanvasSide.TOP
        else CorridorKind.OVER_TOP_BAND
    )

    with pytest.raises(ValueError, match="canvas side and corridor kind disagree"):
        replace(reservation, kind=wrong_kind)


def test_query_rejects_fabricated_boundary_blockers() -> None:
    plan = _plan()
    realised = plan.realised_reservations[0]
    malformed = replace(realised, negative_blocker_ids=("invented:negative",))

    with pytest.raises(ValueError, match="invalid boundary blocker ids"):
        build_route_plan_query(
            replace(
                plan,
                realised_reservations=_replace_record(
                    plan.realised_reservations,
                    malformed,
                    id_field="reservation_id",
                ),
            )
        )


def test_query_requires_every_non_canvas_realisation() -> None:
    plan = _plan()
    reservation = next(
        item for item in plan.reservations if not isinstance(item.region, CanvasRegion)
    )
    remaining = tuple(
        item
        for item in plan.realised_reservations
        if item.reservation_id != reservation.id
    )

    with pytest.raises(ValueError, match="missing its realisation"):
        build_route_plan_query(replace(plan, realised_reservations=remaining))


def test_reservation_claim_rejects_a_zero_length_interval() -> None:
    plan = _plan()
    claim = plan.reservations[0].claims[0]

    with pytest.raises(ValueError, match="positive travel interval"):
        replace(claim, longitudinal_end=claim.longitudinal_start)


@pytest.mark.parametrize(
    ("field", "mutate"),
    (
        ("required_width", lambda realised: realised.required_width + 1.0),
        ("capacity_slack", lambda realised: realised.capacity_slack + 1.0),
        ("negative_side_slack", lambda realised: realised.negative_side_slack + 1.0),
        ("positive_side_slack", lambda realised: realised.positive_side_slack + 1.0),
        ("coordinate", lambda realised: realised.coordinate + 1.0),
    ),
)
def test_query_rejects_inconsistent_realisation_arithmetic(field, mutate) -> None:
    plan = _plan()
    realised = plan.realised_reservations[0]
    malformed = replace(realised, **{field: mutate(realised)})

    with pytest.raises(ValueError, match="realised reservation is inconsistent"):
        build_route_plan_query(
            replace(
                plan,
                realised_reservations=_replace_record(
                    plan.realised_reservations,
                    malformed,
                    id_field="reservation_id",
                ),
            )
        )


def test_query_rejects_realisation_axes_that_disagree_with_orientation() -> None:
    plan = _plan()
    realised = plan.realised_reservations[0]
    malformed = replace(
        realised,
        allocation_axis=realised.longitudinal_axis,
        longitudinal_axis=realised.allocation_axis,
    )

    with pytest.raises(ValueError, match="realised reservation is inconsistent"):
        build_route_plan_query(
            replace(
                plan,
                realised_reservations=_replace_record(
                    plan.realised_reservations,
                    malformed,
                    id_field="reservation_id",
                ),
            )
        )


@pytest.mark.parametrize(
    ("field", "mutate"),
    (
        ("claimant_member_ids", lambda _diagnostic: ()),
        ("capacity_slack", lambda diagnostic: diagnostic.capacity_slack + 1.0),
        (
            "negative_side_slack",
            lambda diagnostic: diagnostic.negative_side_slack + 1.0,
        ),
        (
            "positive_side_slack",
            lambda diagnostic: diagnostic.positive_side_slack + 1.0,
        ),
        ("message", lambda diagnostic: f"{diagnostic.message} fabricated"),
    ),
)
def test_query_rejects_inconsistent_diagnostic_values(field, mutate) -> None:
    plan = _plan(DEFICIT_MAP)
    diagnostic = plan.reservation_diagnostics[0]
    malformed = replace(diagnostic, **{field: mutate(diagnostic)})

    with pytest.raises(ValueError, match="reservation diagnostic is inconsistent"):
        build_route_plan_query(
            replace(
                plan,
                reservation_diagnostics=_replace_record(
                    plan.reservation_diagnostics,
                    malformed,
                    id_field="reservation_id",
                ),
            )
        )


def test_query_rejects_duplicate_reservation_diagnostics() -> None:
    plan = _plan(DEFICIT_MAP)
    diagnostic = plan.reservation_diagnostics[0]

    with pytest.raises(ValueError, match="duplicate reservation diagnostics"):
        build_route_plan_query(
            replace(plan, reservation_diagnostics=(diagnostic, diagnostic))
        )


def test_query_rejects_reservation_diagnostics_out_of_order() -> None:
    plan = _plan(DEFICIT_MAP)
    assert len(plan.reservation_diagnostics) > 1

    with pytest.raises(ValueError, match="not in reservation order"):
        build_route_plan_query(
            replace(
                plan,
                reservation_diagnostics=tuple(reversed(plan.reservation_diagnostics)),
            )
        )
