"""Row and column envelopes settle monotonically around route reservations."""

from __future__ import annotations

import warnings
from copy import deepcopy
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest import mock

import pytest

from nf_metro.api import prepare_graph, resolve_theme
from nf_metro.layout import envelope_settlement, route_reservations
from nf_metro.layout.constants import (
    CANVAS_EDGE_CLEARANCE,
    COORD_TOLERANCE,
    CURVE_RADIUS,
    DIRECTIONAL_MARKER_HALF_EXTENT,
    INTER_ROW_EDGE_CLEARANCE,
    SECTION_Y_GAP,
    WIDEST_THEME_LINE_WIDTH,
)
from nf_metro.layout.envelope_settlement import (
    BoundaryClearanceDemand,
    EnvelopeSettlement,
    SettlementAxis,
    SettlementShortfall,
    attribute_compatibility_systems,
    quantised_allocation,
    settle_route_envelopes,
)
from nf_metro.layout.geometry import cotravelling_lane_clearance, shift_section
from nf_metro.layout.pass_metrics import canvas_edge_clearance, stroke_scale_context
from nf_metro.layout.phases.bbox import measure_row_gap_clearance
from nf_metro.layout.phases.guards import (
    LayoutInvariantError,
    PhaseInvariantError,
    assert_canvas_corridors_hold_their_claims,
    assert_reservations_are_settled,
)
from nf_metro.layout.phases.junctions import reanchor_junctions
from nf_metro.layout.route_plan import (
    EmissionMemberId,
    GridSpan,
    build_route_plan_query,
)
from nf_metro.layout.route_reservations import (
    CanvasRegion,
    CanvasSide,
    ColumnGapRegion,
    CorridorMeasurementScope,
    RowGapRegion,
    canvas_edge_slack,
    measured_distance,
    realise_reservation,
    reservation_ids_by_claimant_member,
)
from nf_metro.layout.routing import compute_station_offsets, observe_route_edges
from nf_metro.layout.routing.common import _inter_row_band_fits, apply_route_offsets
from nf_metro.render import svg as render_svg_module
from nf_metro.render.svg import (
    _assert_settlement_decisions_frozen,
    _attach_published_reservation_attribution,
    _route_decision_fingerprint,
    _settled_render_graph,
    build_observed_render_plan,
)
from nf_metro.themes import THEMES

ROOT = Path(__file__).parents[1]
TOPOLOGIES = ROOT / "examples" / "topologies"
REPORT_HO = ROOT / "tests" / "fixtures" / "route_reservations" / "reportho.metro"
REGRESSIONS = ROOT / "tests" / "fixtures" / "regressions"

# Fixtures whose reservations carry a capacity deficit on unsettled geometry, so
# settlement has real work to do.  Every member's deficit falls on a row
# boundary, which is where the corpus puts one; ``COLUMN_DEFICIT_CORPUS`` carries
# the column axis, and the ownership lemma below is measured on both.
DEFICIT_CORPUS = (
    TOPOLOGIES / "convergence_fold_diamond.mmd",
    TOPOLOGIES / "convergence_sink_fold.mmd",
    TOPOLOGIES / "fold_split_targets.mmd",
)

# This fixture naturally publishes a starved planned column corridor, so the
# column settlement path is exercised from observation through final validation.
COLUMN_DEFICIT_CORPUS = (
    ROOT / "tests" / "fixtures" / "hash_seed_determinism" / "seed_15.mmd",
)

# Offsets a whole map is moved by to check the allocation reads the deficit and
# not the coordinates it is measured at.  None is a whole pixel and none is
# representable in binary64, so the difference of two moved coordinates lands
# beside the difference of the two unmoved ones; the magnitudes span sub-pixel to
# four figures in both directions, because the error a subtraction leaves scales
# with its operands and not with its answer.
ORIGIN_OFFSETS = (0.1, 0.3, 1.0 / 3.0, 7.7, 1000.1, -0.1, -7.7)

# Planned fixtures whose settlement allocates and whose routes move rigidly when
# the whole map is translated to a different canvas origin.
ORIGIN_CORPUS = (
    TOPOLOGIES / "convergence_fold_diamond.mmd",
    TOPOLOGIES / "convergence_sink_fold.mmd",
    TOPOLOGIES / "fold_split_targets.mmd",
)

# Planned maps covering all four canvas sides.  The margin each corridor keeps
# against the edge is measured separately from room banked on its content side.
CANVAS_CLEARANCE_CORPUS = (
    TOPOLOGIES / "fanout_bundle_plus_spurs.mmd",
    TOPOLOGIES / "fan_in_merge.mmd",
    TOPOLOGIES / "bottom_exit_stacked_right_entry_fan.mmd",
    TOPOLOGIES / "around_section_below.mmd",
    TOPOLOGIES / "lr_perp_top_exit_perp_entry_diverging.mmd",
)

SPANNING_CORPUS = (TOPOLOGIES / "convergence_fold_diamond.mmd",)

# Fixtures with no positive deficit anywhere: settlement must not touch them.
SETTLED_CORPUS = (
    ROOT / "examples" / "rnaseq_sections.mmd",
    ROOT / "examples" / "rnaseq_auto.mmd",
    ROOT / "examples" / "hlatyping.mmd",
    ROOT / "examples" / "epitopeprediction.mmd",
)

LEDGER_REROUTE_CORPUS = (
    (ROOT / "examples" / "rnaseq_sections.mmd", 0),
    (ROOT / "examples" / "genomeassembly.mmd", 1),
    (ROOT / "examples" / "variantbenchmarking.mmd", 0),
    (ROOT / "examples" / "differentialabundance_default.mmd", 0),
    (TOPOLOGIES / "merge_around_below_leftmost.mmd", 1),
    (TOPOLOGIES / "bypass_left_entry_from_right.mmd", 1),
    (TOPOLOGIES / "junction_entry_align.mmd", 1),
)

# One fixture per supported flow direction, so the single axis-based
# implementation is exercised under rotation and reflection.
DIRECTION_CORPUS = {
    "LR": TOPOLOGIES / "convergence_fold_diamond.mmd",
    "RL": TOPOLOGIES / "bottom_exit_stacked_right_entry_fan.mmd",
    "TB": TOPOLOGIES / "bottom_exit_junction_collinear_top_entry.mmd",
    "BT": TOPOLOGIES / "bt_perp_left_entry_right_exit.mmd",
}


def _observe_drawn(path: Path):
    """*path*'s graph and plan, plus the polylines its observation would draw."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        offsets = compute_station_offsets(graph)
        observation = observe_route_edges(graph, station_offsets=offsets)
    polylines = [apply_route_offsets(route, offsets) for route in observation.routes]
    return graph, observation.plan, polylines


def _observe(path: Path):
    graph, plan, _polylines = _observe_drawn(path)
    return graph, plan


def _observe_planned_straddler(path: Path):
    """A planned deficit with an unrelated section spanning its row boundary."""
    directives = """\
%%metro grid: start | 0,0,3,1
%%metro grid: branch_left | 1,0
%%metro grid: branch_right | 1,1
%%metro grid: finish | 1,2
"""
    source = path.read_text().replace("graph LR", f"{directives}\ngraph LR")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(source, source_dir=str(path.parent))
        offsets = compute_station_offsets(graph)
        observation = observe_route_edges(graph, station_offsets=offsets)
    reservation = next(
        item
        for item in observation.plan.reservations
        if isinstance(item.region, RowGapRegion) and item.region.lower_row == 2
    )
    _narrow_reservation(graph, reservation)
    return graph, observation.plan


def _observe_moved(path: Path, delta: float):
    """*path* laid out, moved bodily by *delta* on both axes, then routed.

    Every section moves by the one amount, so no pair of boxes changes its
    separation and every boundary owes exactly what it owed: the arrangement is
    the same one, described at a different canvas origin.  Junctions are a
    function of the ports they join rather than independent data, so they are
    re-derived after the move the way the render path re-derives them before each
    route, not carried across it.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        for section in graph.sections.values():
            shift_section(graph, section, dx=delta, dy=delta)
        reanchor_junctions(graph)
        offsets = compute_station_offsets(graph)
        observation = observe_route_edges(graph, station_offsets=offsets)
    polylines = [apply_route_offsets(route, offsets) for route in observation.routes]
    return graph, observation.plan, polylines


def _allocations(graph, plan) -> dict[tuple[SettlementAxis, int], float]:
    """What settling *plan* on *graph* widens each boundary by.

    Both demand kinds are supplied, as the render path supplies them: a boundary
    reached only through the clearance its facing boxes owe would otherwise go
    unmeasured here, and its deficit is stated by the same arithmetic.
    """
    return {
        (item.axis, item.boundary): item.amount
        for item in settle_route_envelopes(
            graph, plan, clearance=_clearance()
        ).translations
    }


def _rendered_plan(path: Path, *, permissive: bool = False):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        graph.permissive = permissive
        return build_observed_render_plan(graph, resolve_theme(None, graph))


@pytest.mark.parametrize(("path", "expected_ledger_reroutes"), LEDGER_REROUTE_CORPUS)
def test_settlement_reroutes_only_when_ledger_changes_derived_band(
    path: Path, expected_ledger_reroutes: int
) -> None:
    original = render_svg_module.observe_route_edges_centred
    ledger_reroutes = 0

    def observe(*args, **kwargs):
        nonlocal ledger_reroutes
        if kwargs.get("reservations") is not None:
            ledger_reroutes += 1
        return original(*args, **kwargs)

    with mock.patch.object(render_svg_module, "observe_route_edges_centred", observe):
        _rendered_plan(path)

    assert ledger_reroutes == expected_ledger_reroutes


def _settled(path: Path):
    """The geometry a render of *path* draws, and the plan drawn on it."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        graph.permissive = True
        theme = resolve_theme(None, graph)
        route_plan = build_observed_render_plan(graph, theme).route_plan
        return _settled_render_graph(graph, theme), route_plan


def _sole(items: tuple):
    assert len(items) == 1, f"expected one compatibility system, found {len(items)}"
    return items[0]


def _capacity_deficits(plan) -> dict[str, float]:
    """Reservation id -> negative capacity slack, for gap-region corridors."""
    query = build_route_plan_query(plan)
    deficits: dict[str, float] = {}
    for reservation in plan.reservations:
        if not isinstance(reservation.region, RowGapRegion | ColumnGapRegion):
            continue
        realised = query.realised_reservation(reservation.id)
        if realised is not None and realised.capacity_slack < -0.01:
            deficits[str(reservation.id)] = realised.capacity_slack
    return deficits


def _geometry(graph) -> dict[str, tuple[float, ...]]:
    return {
        **{
            f"section:{key}": (
                section.bbox_x,
                section.bbox_y,
                section.bbox_w,
                section.bbox_h,
            )
            for key, section in graph.sections.items()
        },
        **{
            f"station:{key}": (station.x, station.y)
            for key, station in graph.stations.items()
        },
        **{f"port:{key}": (port.x, port.y) for key, port in graph.ports.items()},
    }


def _section_local_geometry(graph) -> dict[str, tuple]:
    """Each section's size and its content's position within it.

    Rounded to a hundredth of a pixel: a rigid translation of a whole row is
    exact in intent but not bit-exact once the same offset is added to and then
    subtracted from two different coordinates.
    """
    return {
        key: (
            round(section.bbox_w, 2),
            round(section.bbox_h, 2),
            tuple(
                (
                    round(graph.stations[item].x - section.bbox_x, 2),
                    round(graph.stations[item].y - section.bbox_y, 2),
                )
                for item in sorted(section.station_ids)
                if item in graph.stations
            ),
        )
        for key, section in graph.sections.items()
    }


def _row_gaps(graph) -> dict[tuple[int, int], float]:
    """Vertical separation between each adjacent pair of grid rows."""
    return _axis_gaps(graph, SettlementAxis.ROW)


def _column_gaps(graph) -> dict[tuple[int, int], float]:
    return _axis_gaps(graph, SettlementAxis.COLUMN)


def _axis_gaps(graph, axis: SettlementAxis) -> dict[tuple[int, int], float]:
    starts: dict[int, list[float]] = {}
    ends: dict[int, list[float]] = {}
    for section in graph.sections.values():
        if section.bbox_w <= 0 or section.bbox_h <= 0:
            continue
        if axis is SettlementAxis.ROW:
            index = section.grid_row
            last = section.grid_row + section.grid_row_span - 1
            lo, hi = section.bbox_y, section.bbox_y + section.bbox_h
        else:
            index = section.grid_col
            last = section.grid_col + section.grid_col_span - 1
            lo, hi = section.bbox_x, section.bbox_x + section.bbox_w
        starts.setdefault(index, []).append(lo)
        ends.setdefault(last, []).append(hi)
    gaps: dict[tuple[int, int], float] = {}
    for index in sorted(starts):
        if index - 1 not in ends:
            continue
        gaps[(index - 1, index)] = min(starts[index]) - max(ends[index - 1])
    return gaps


def test_a_gap_is_its_width_not_the_coordinates_it_lies_between() -> None:
    """The arithmetic behind the property below, at the numbers that expose it.

    ``differentialabundance``'s ``functional`` / ``plots`` row gap runs from 458.8
    to 534.8 and owes 90.0, so it is short by exactly 14.  Neither edge is
    representable in binary64, and subtracting them reports the gap 6e-14 narrow
    and therefore the deficit 6e-14 wide -- on the far side of an integer from
    where a whole quantum lands, which is the difference between the boundary
    being widened by 14px and by 15px.
    """
    required = 90.0
    assert 534.8 - 458.8 != 76.0
    assert measured_distance(458.8, 534.8) == 76.0

    assert required - (534.8 - 458.8) == 14.000000000000057
    assert required - measured_distance(458.8, 534.8) == 14.0

    assert quantised_allocation(14.000000000000057) == 15.0
    assert quantised_allocation(14.0) == 14.0


@pytest.mark.parametrize("delta", ORIGIN_OFFSETS)
@pytest.mark.parametrize("path", ORIGIN_CORPUS, ids=lambda item: item.name)
def test_the_allocation_is_a_function_of_the_deficit_not_the_canvas_origin(
    path: Path, delta: float
) -> None:
    """One map at two canvas origins allocates its boundaries identically.

    A deficit is what a boundary owes less the gap it has, and the gap is the
    distance between two box edges.  Taken as a bare subtraction that carries an
    error set by the magnitude of the two coordinates rather than by the distance
    between them, so one arrangement measured at two origins states two
    deficits, and :func:`quantised_allocation` rounding the wider of them up
    spends a whole pixel of map on 1e-13 of arithmetic.  A boundary's width
    follows from the layout, and a canvas origin is not part of the layout.

    Holding this together with ``amount >= deficit`` is why the resolution
    belongs to the measurement rather than to the ceiling: the ceiling allocates
    no less than the deficit it is handed, which is the ownership lemma's
    premise, and the deficit it is handed is the one the geometry states.
    """
    reference_graph, reference_plan, reference_polylines = _observe_moved(path, 0.0)
    reference = _allocations(reference_graph, reference_plan)
    assert reference, "fixture must carry a deficit for settlement to allocate"

    graph, plan, polylines = _observe_moved(path, delta)
    assert len(polylines) == len(reference_polylines)
    for before, after in zip(reference_polylines, polylines, strict=True):
        # The premise: the router drew the same shape at the new origin. Where it
        # did not, whatever settlement then allocates says nothing about the
        # quantiser, so this has to be established rather than assumed.
        assert len(after) == len(before)
        moved = {
            (round(x2 - x1 - delta, 6), round(y2 - y1 - delta, 6))
            for (x1, y1), (x2, y2) in zip(before, after, strict=True)
        }
        assert moved == {(0.0, 0.0)}, f"{path.name} routes differently at {delta}"

    assert _allocations(graph, plan) == reference


@pytest.mark.parametrize("path", DEFICIT_CORPUS, ids=lambda item: item.name)
def test_settlement_meets_the_demands_it_was_handed(path: Path) -> None:
    """Settlement's own contract, measured against its own input.

    The ledger a settled re-route publishes is a different set of claims --
    corridors appear and vanish across a translation -- so measuring there
    answers a different question than "did settlement satisfy what it was
    given".  This measures the input ledger on the geometry settlement left.
    """
    graph, plan = _observe(path)
    settlement = settle_route_envelopes(graph, plan)
    assert settlement.translations
    assert settlement.shortfalls == ()


@pytest.mark.parametrize("path", DEFICIT_CORPUS, ids=lambda item: item.name)
def test_the_rerouted_ledger_is_also_free_of_deficits(path: Path) -> None:
    """A separate, weaker statement than the contract above: the claims the
    settled geometry goes on to publish are satisfied too."""
    observed = _rendered_plan(path)
    assert _capacity_deficits(observed.route_plan) == {}


@pytest.mark.parametrize("path", COLUMN_DEFICIT_CORPUS, ids=lambda item: item.name)
def test_the_column_phase_settles_a_column_deficit(path: Path) -> None:
    """A starved column gap is settled by translating the column that bounds it.

    Held separately from ``DEFICIT_CORPUS`` because a fixture large enough to
    starve a column gap also carries unrelated row-corridor defects, so the
    statement worth making here is only about the axis that translates: each
    column deficit is answered by a translation of its own right-hand column,
    and a row translation could not have widened it, writing only y.
    """
    graph, plan = _observe(path)
    column_deficits = {
        str(item.id): item
        for item in plan.reservations
        if isinstance(item.region, ColumnGapRegion)
        and str(item.id) in _capacity_deficits(plan)
    }
    assert column_deficits

    settlement = settle_route_envelopes(graph, plan)
    assert settlement.shortfalls == ()
    translated_columns = {
        item.boundary
        for item in settlement.translations
        if item.axis is SettlementAxis.COLUMN
    }
    assert translated_columns >= {
        item.region.right_column
        for item in column_deficits.values()
        if isinstance(item.region, ColumnGapRegion)
    }
    for reservation_id in column_deficits:
        reservation = next(
            item for item in plan.reservations if str(item.id) == reservation_id
        )
        realised = realise_reservation(
            graph,
            reservation,
            coordinate_translations=settlement.coordinate_translations,
        )
        assert realised is not None
        assert realised.capacity_slack >= -0.01


GROUP_BAND_MAP = ROOT / "tests" / "fixtures" / "group_band_over_row_corridor.mmd"


def test_a_group_caption_band_does_not_eat_a_settled_row_corridor() -> None:
    """A ``below`` band grows the bottom edge that bounds the corridor under it.

    ``source`` sits in row 0 above ``middle`` in row 1 with a 78px inter-row
    bundle between them, and its two captions claim 14.35px of the box, so the
    band and the corridor compete for the same pixels.
    """
    graph, plan = _settled(GROUP_BAND_MAP)
    source, middle = graph.sections["source"], graph.sections["middle"]
    assert source.bbox_h > 100.0, "the band has to have grown the box"

    query = build_route_plan_query(plan)
    reservation = next(
        item
        for item in plan.reservations
        if isinstance(item.region, RowGapRegion) and item.region.upper_row == 0
    )
    realised = query.realised_reservation(reservation.id)
    assert realised is not None
    assert realised.capacity_slack >= 0.0
    assert middle.bbox_y - (source.bbox_y + source.bbox_h) >= reservation.minimum_width


def test_a_group_band_render_is_gated_by_the_geometry_it_draws() -> None:
    """Strict renders certify the boxes the group bands grew, not the boxes
    layout left, so a band that starves a corridor cannot pass unmeasured."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(
            GROUP_BAND_MAP.read_text(), source_dir=str(GROUP_BAND_MAP.parent)
        )
        graph.strict = True
        theme = resolve_theme(None, graph)
        drawn = _settled_render_graph(graph, theme)
        plan = build_observed_render_plan(graph, theme).route_plan

    assert drawn.sections["source"].bbox_h == pytest.approx(
        graph.sections["source"].bbox_h + 14.35, abs=0.01
    )
    assert _capacity_deficits(plan) == {}


PLANNED_GAP_CORRIDOR_CORPUS = {
    "row-deficit": TOPOLOGIES / "convergence_fold_diamond.mmd",
    "two-row-deficits": TOPOLOGIES / "convergence_sink_fold.mmd",
    "row-and-column": TOPOLOGIES / "bottom_exit_junction_collinear_top_entry.mmd",
    "column-bundle": TOPOLOGIES / "asymmetric_tree.mmd",
    "fold-rows": TOPOLOGIES / "convergence_fold_diamond.mmd",
    "fold-targets": TOPOLOGIES / "fold_split_targets.mmd",
    **{f"flow-{name}": path for name, path in DIRECTION_CORPUS.items()},
}

LEMMA_CORPUS = {
    **PLANNED_GAP_CORRIDOR_CORPUS,
    **{f"deficit-{path.name}": path for path in DEFICIT_CORPUS},
    **{f"column-{path.name}": path for path in COLUMN_DEFICIT_CORPUS},
}

# Fixtures that run a column translation while row corridors exist, so the row
# phase's result is exposed to the column phase.
CROSS_AXIS_CORPUS = (
    TOPOLOGIES / "bottom_exit_junction_collinear_top_entry.mmd",
    TOPOLOGIES / "clear_channel_target_aware_push.mmd",
)


SECTION_EDGE_BLOCKERS = frozenset(
    {
        route_reservations.SECTION_BOTTOM_BLOCKER,
        route_reservations.SECTION_HEADER_BLOCKER,
        route_reservations.SECTION_LEFT_BLOCKER,
        route_reservations.SECTION_RIGHT_BLOCKER,
    }
)


def _blocker_sections(ids: tuple[str, ...]) -> set[str]:
    """The sections named by *ids*; a launch anchor names a station instead."""
    return {
        item.split(":", 1)[1]
        for item in ids
        if item.split(":", 1)[0] in SECTION_EDGE_BLOCKERS
    }


def _axis_for(region) -> tuple:
    """The settlement axis object and boundary index for a gap *region*."""
    if isinstance(region, RowGapRegion):
        return envelope_settlement.ROW_AXIS, region.lower_row
    return envelope_settlement.COLUMN_AXIS, region.right_column


LEMMA_TRANSLATION = 8.0
"""A translation large enough to separate an exact widening from a rounded one."""


@pytest.mark.parametrize("path", LEMMA_CORPUS.values(), ids=tuple(LEMMA_CORPUS))
def test_a_boundary_translation_widens_its_corridor_by_exactly_its_amount(
    path: Path,
) -> None:
    """Why widening a boundary can never fail, whatever the author pinned.

    This is the geometric consequence the ownership rule exists for, so it is
    measured rather than argued from the two index comparisons agreeing: widen
    the boundary a corridor is filed against and re-measure the corridor.  Its
    near edge is a box ending above the boundary, which stays; its far edge is a
    box starting at or beyond it, which travels the full amount.  A layout where
    that is not exact is one where a pin has put a bounding box on the wrong side
    of its own boundary, and no amount of widening would reach the corridor.
    """
    graph, plan = _observe(path)
    query = build_route_plan_query(plan)
    checked = 0
    for reservation in plan.reservations:
        if not isinstance(reservation.region, RowGapRegion | ColumnGapRegion):
            continue
        held = query.realised_reservation(reservation.id)
        if held is None:
            continue
        axis, boundary = _axis_for(reservation.region)
        ownership = envelope_settlement.translation_ownership(graph, axis, boundary)
        state = envelope_settlement._coordinate_state(graph)
        envelope_settlement.apply_translation(graph, axis, ownership, LEMMA_TRANSLATION)
        widened = realise_reservation(graph, reservation)
        envelope_settlement._restore_coordinate_state(graph, state)
        assert widened is not None, (path, reservation.description)
        assert widened.available_width == pytest.approx(
            held.available_width + LEMMA_TRANSLATION, abs=0.01
        ), (path, reservation.description)
        checked += 1
    assert checked, path


@pytest.mark.parametrize(
    "path",
    DEFICIT_CORPUS + COLUMN_DEFICIT_CORPUS + CROSS_AXIS_CORPUS,
    ids=lambda item: item.name,
)
def test_each_translation_widens_the_corridor_it_records_by_its_amount(
    path: Path,
) -> None:
    """The postcondition of one settlement step, on the moves settlement chose.

    Replaying the recorded translations in the order the sweep applied them and
    re-measuring the corridor each one names between the two states is the only
    way to see a step in isolation: the final geometry carries every later move
    as well.
    """
    graph, plan = _observe(path)
    settlement = settle_route_envelopes(graph, plan)
    if not settlement.translations:
        reservation = next(
            item
            for item in plan.reservations
            if isinstance(item.region, RowGapRegion | ColumnGapRegion)
        )
        _narrow_reservation(graph, reservation)
        settlement = settle_route_envelopes(graph, plan)
    assert settlement.translations, path

    replayed, replayed_plan = _observe(path)
    by_id = {item.id: item for item in replayed_plan.reservations}
    projection: list = []
    for translation in settlement.translations:
        axis = (
            envelope_settlement.ROW_AXIS
            if translation.axis is SettlementAxis.ROW
            else envelope_settlement.COLUMN_AXIS
        )
        reservation = by_id[translation.reservation_id]
        before = realise_reservation(
            replayed, reservation, coordinate_translations=tuple(projection)
        )
        envelope_settlement.apply_translation(
            replayed,
            axis,
            envelope_settlement.translation_ownership(
                replayed, axis, translation.boundary
            ),
            translation.amount,
        )
        projection.append(
            envelope_settlement._reservation_coordinate_translation(
                translation, replayed_plan
            )
        )
        after = realise_reservation(
            replayed, reservation, coordinate_translations=tuple(projection)
        )
        assert before is not None and after is not None
        assert after.available_width - before.available_width == pytest.approx(
            translation.amount, abs=0.01
        ), (path, translation.message)


def _sections_wholly_before(graph, axis, boundary: int) -> set[str]:
    """Sections ending strictly above *boundary*, by this test's own reckoning."""
    return {
        key
        for key, section in graph.sections.items()
        if axis.start_index(section) + axis.span(section) <= boundary
    }


def _sections_wholly_after(graph, axis, boundary: int) -> set[str]:
    return {
        key
        for key, section in graph.sections.items()
        if axis.start_index(section) >= boundary
    }


@pytest.mark.parametrize("path", LEMMA_CORPUS.values(), ids=tuple(LEMMA_CORPUS))
def test_each_corridor_blocker_lies_wholly_on_the_side_it_bounds(path: Path) -> None:
    """A boundary is bounded by boxes that clear it, never by ones occupying it.

    The two sets are re-derived here from the grid indices alone rather than read
    back from the measurement, so a box straddling the boundary being counted as
    a blocker is a difference this can see.  A straddling box's far edge lies past
    the boundary and its header before it, so counting it would state a clearance
    the boundary does not offer.
    """
    graph, plan = _observe(path)
    query = build_route_plan_query(plan)
    checked = 0
    for reservation in plan.reservations:
        if not isinstance(reservation.region, RowGapRegion | ColumnGapRegion):
            continue
        realised = query.realised_reservation(reservation.id)
        if realised is None:
            continue
        axis, boundary = _axis_for(reservation.region)
        before = _sections_wholly_before(graph, axis, boundary)
        after = _sections_wholly_after(graph, axis, boundary)
        negative = _blocker_sections(realised.negative_blocker_ids)
        positive = _blocker_sections(realised.positive_blocker_ids)

        assert negative <= before, (path, reservation.description)
        assert positive <= after, (path, reservation.description)
        checked += 1
    assert checked, path


def test_one_translation_settles_every_claim_on_its_boundary() -> None:
    """Several starved claims at one boundary receive one sufficient widening."""
    path = TOPOLOGIES / "opposing_bypass_corridor.mmd"
    graph, plan = _observe(path)
    residents = tuple(
        reservation
        for reservation in plan.reservations
        if isinstance(reservation.region, RowGapRegion)
        and reservation.region.lower_row == 1
    )
    realised = tuple(realise_reservation(graph, item) for item in residents)
    assert residents and all(item is not None for item in realised)
    amount = max(item.capacity_slack for item in realised if item is not None) + 6.0
    _narrow(graph, SettlementAxis.ROW, 1, amount)
    starved = _capacity_deficits(plan)
    assert set(starved) == {str(item.id) for item in residents}

    settlement = settle_route_envelopes(graph, plan)
    (translation,) = settlement.translations
    assert translation.axis is SettlementAxis.ROW
    assert translation.boundary == 1
    assert translation.amount >= max(-value for value in starved.values())
    assert {str(item) for item in translation.reservation_ids} == set(starved)

    for reservation_id in starved:
        reservation = next(
            item for item in plan.reservations if str(item.id) == reservation_id
        )
        realised = realise_reservation(
            graph,
            reservation,
            coordinate_translations=settlement.coordinate_translations,
        )
        assert realised is not None
        assert realised.capacity_slack >= -0.01


def test_a_boundary_is_charged_for_the_unfiled_leg_drawn_in_it() -> None:
    """A stroke takes room whether or not a claim names it.

    ``merge_around_below_leftmost`` draws a merge trunk and that trunk's own
    return leg in row gap 0/1.  The return leg's connector begins and ends in row
    0, so it crosses no boundary and the region search files it against none; it
    is drawn in the gap regardless, and reading the boundary as holding one stroke
    leaves it hugging the box edge above with no clearance the gap can be widened
    to give it.
    """
    path = TOPOLOGIES / "merge_around_below_leftmost.mmd"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        graph.strict = True
        theme = resolve_theme(None, graph)
        drawn = _settled_render_graph(graph, theme)
        observed = build_observed_render_plan(graph, theme)

    upper = max(
        section.bbox_y + section.bbox_h
        for section in drawn.sections.values()
        if section.grid_row == 0
    )
    lower = min(
        section.bbox_y for section in drawn.sections.values() if section.grid_row == 1
    )
    lanes = sorted(
        {
            first[1]
            for points in observed.plan.route_polylines
            for first, second in zip(points, points[1:])
            if abs(first[1] - second[1]) <= COORD_TOLERANCE
            and abs(first[0] - second[0]) > COORD_TOLERANCE
            and upper < first[1] < lower
        }
    )
    assert len(lanes) == 2, lanes
    assert lanes[0] - upper == pytest.approx(INTER_ROW_EDGE_CLEARANCE)
    assert lanes[1] - lanes[0] >= cotravelling_lane_clearance(
        same_line=True, counter_running=True, curve_radius=CURVE_RADIUS
    )

    reservation = next(
        item
        for item in observed.route_plan.reservations
        if isinstance(item.region, RowGapRegion) and item.region.upper_row == 0
    )
    assert reservation.peer_width == pytest.approx(lanes[1] - lanes[0])
    assert reservation.minimum_width == pytest.approx(
        reservation.negative_side_clearance
        + reservation.bundle_width
        + reservation.peer_width
        + reservation.positive_side_clearance
    )
    assert lower - upper >= reservation.minimum_width


@pytest.mark.parametrize("path", LEMMA_CORPUS.values(), ids=tuple(LEMMA_CORPUS))
def test_settlement_never_declines_a_demand_or_raises(path: Path) -> None:
    """The ratchet on the lemma: an unmet demand or a stage failure would mean
    an arrangement settlement cannot widen, which the ownership rule forbids."""
    graph, plan = _observe(path)
    settlement = settle_route_envelopes(graph, plan)
    assert settlement.shortfalls == ()
    for translation in settlement.translations:
        assert translation.amount >= 0.0


@pytest.mark.parametrize("path", CROSS_AXIS_CORPUS, ids=lambda item: item.name)
def test_the_column_phase_leaves_every_row_corridor_the_width_it_had(
    path: Path,
) -> None:
    """A column translation writes only x, and a row corridor is measured
    between two y edges, so the column phase reaches one only by changing which
    sections its run overlaps.  Asserting each corridor is measurable and equal,
    rather than asserting no corridor narrowed, is what makes a corridor the
    column phase dropped a failure instead of a vacuous pass.
    """
    graph, plan = _observe(path)
    row_reservations = tuple(
        item for item in plan.reservations if isinstance(item.region, RowGapRegion)
    )
    column_reservations = tuple(
        item for item in plan.reservations if isinstance(item.region, ColumnGapRegion)
    )
    assert row_reservations and column_reservations, path

    target_column = column_reservations[0]
    _narrow_reservation(graph, target_column)

    _, row_coordinate = envelope_settlement._settle_axis(
        graph, plan, row_reservations, envelope_settlement.ROW_AXIS
    )
    before = {
        item.id: realise_reservation(
            graph, item, coordinate_translations=tuple(row_coordinate)
        )
        for item in row_reservations
    }
    assert all(value is not None for value in before.values())

    column_translations, column_coordinate = envelope_settlement._settle_axis(
        graph,
        plan,
        column_reservations,
        envelope_settlement.COLUMN_AXIS,
        tuple(row_coordinate),
    )
    assert column_translations, path
    for reservation in row_reservations:
        after = realise_reservation(
            graph, reservation, coordinate_translations=tuple(column_coordinate)
        )
        assert after is not None, (path, reservation.description)
        assert after.available_width == pytest.approx(
            before[reservation.id].available_width
        ), (path, reservation.description)


def test_reportho_report_trunk_keeps_its_authored_inter_row_corridor() -> None:
    """The 12 report feeders share one trunk lane needing 78px between rows.

    Rendered permissively because this map also puts two opposing channels in
    one column gap without separating them, which is a lane-placement defect
    with plenty of measured corridor to spare rather than a corridor width this
    stage owns.  Every reservation the settled geometry publishes is checked
    below, that one included.
    """
    observed = _rendered_plan(REPORT_HO, permissive=True)
    plan = observed.route_plan
    query = build_route_plan_query(plan)
    reservation = max(
        (item for item in plan.reservations if isinstance(item.region, RowGapRegion)),
        key=lambda item: item.minimum_width,
    )
    assert len(reservation.connector_ids) == 12
    realised = query.realised_reservation(reservation.id)
    assert realised is not None
    assert reservation.minimum_width == 78
    assert realised.available_width >= 78.0
    assert realised.capacity_slack >= 0.0
    assert _capacity_deficits(plan) == {}


@pytest.mark.parametrize("path", DEFICIT_CORPUS, ids=lambda item: item.name)
def test_settlement_run_twice_is_an_exact_geometry_no_op(path: Path) -> None:
    """Settlement reaches a fixpoint in one directional pass."""
    graph, plan = _observe(path)
    settle_route_envelopes(graph, plan)
    settled = _geometry(graph)
    second = settle_route_envelopes(graph, plan)
    assert second.translations == ()
    assert _geometry(graph) == settled


@pytest.mark.parametrize("path", DEFICIT_CORPUS, ids=lambda item: item.name)
def test_the_whole_route_settle_route_pipeline_reaches_a_fixpoint(path: Path) -> None:
    """Re-running the render's own geometry steps on their output changes nothing.

    Running settlement twice against one ledger only shows that the sweep has no
    second move to make.  The stage that has to be idempotent is the whole
    render boundary -- observe a plan, settle against it, re-route to consume it
    -- because that is what a caller re-entering a settled graph exercises, and
    the re-route publishes a ledger the first pass never saw.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        graph.permissive = True
        theme = resolve_theme(None, graph)
        once = _settled_render_graph(graph, theme)
        twice = _settled_render_graph(once, theme)
    assert _geometry(twice) == _geometry(once)


@pytest.mark.parametrize("path", SETTLED_CORPUS, ids=lambda item: item.name)
def test_settlement_leaves_a_deficit_free_layout_untouched(path: Path) -> None:
    graph, plan = _observe(path)
    before = _geometry(graph)
    settlement = settle_route_envelopes(graph, plan)
    assert settlement.translations == ()
    assert _geometry(graph) == before


@pytest.mark.parametrize("path", DEFICIT_CORPUS, ids=lambda item: item.name)
def test_settlement_never_narrows_a_row_or_column_gap(path: Path) -> None:
    graph, plan = _observe(path)
    before_rows, before_columns = _row_gaps(graph), _column_gaps(graph)
    settle_route_envelopes(graph, plan)
    for key, gap in _row_gaps(graph).items():
        assert gap >= before_rows[key] - 0.01, f"row gap {key} narrowed"
    for key, gap in _column_gaps(graph).items():
        assert gap >= before_columns[key] - 0.01, f"column gap {key} narrowed"


@pytest.mark.parametrize("path", SPANNING_CORPUS, ids=lambda item: item.name)
def test_a_straddling_section_is_named_and_held_by_the_translation(
    path: Path,
) -> None:
    """A boundary translation owns the sections starting at or beyond it.

    A section straddling the boundary starts above it, so the translation
    cannot carry it without narrowing the gap above; it stays, and the record
    names it rather than leaving its exclusion implicit.
    """
    graph, plan = _observe_planned_straddler(path)
    before = {key: section.bbox_y for key, section in graph.sections.items()}
    settlement = settle_route_envelopes(graph, plan)
    straddling = tuple(
        item for item in settlement.translations if item.spanning_section_ids
    )
    assert straddling, "fixture no longer straddles a settled boundary"
    for translation in straddling:
        assert translation.axis is SettlementAxis.ROW
        for section_id in translation.spanning_section_ids:
            section = graph.sections[section_id]
            assert section.grid_row < translation.boundary
            assert section.grid_row + section.grid_row_span > translation.boundary
            assert section_id not in translation.section_ids
            assert section.bbox_y == before[section_id]
        for section_id in translation.section_ids:
            assert graph.sections[section_id].grid_row >= translation.boundary


@pytest.mark.parametrize("path", SPANNING_CORPUS, ids=lambda item: item.name)
def test_a_held_straddling_section_bounds_nothing_the_translation_settled(
    path: Path,
) -> None:
    """What makes holding a straddling section sound.

    The widening cannot reach a corridor the held section bounds, so a
    translation that claims to settle such a corridor is making a false record.
    """
    graph, plan = _observe_planned_straddler(path)
    settlement = settle_route_envelopes(graph, plan)
    reservation_by_id = {item.id: item for item in plan.reservations}
    checked = 0
    for translation in settlement.translations:
        if not translation.spanning_section_ids:
            continue
        for reservation_id in translation.reservation_ids:
            realised = realise_reservation(
                graph,
                reservation_by_id[reservation_id],
                coordinate_translations=settlement.coordinate_translations,
            )
            assert realised is not None
            bounding = {
                blocker.partition(":")[2]
                for blocker in (
                    realised.negative_blocker_ids + realised.positive_blocker_ids
                )
            }
            assert not bounding & set(translation.spanning_section_ids)
            checked += 1
    assert checked, "fixture no longer settles a corridor across a straddled boundary"


def test_a_straddling_section_that_bounds_its_corridor_is_rejected() -> None:
    """The unsound arrangement is rejected rather than recorded as a success.

    Widening a boundary buys a corridor nothing when a section straddling that
    boundary is what bounds the corridor.  The sweep's own measurement rejects
    that arrangement before translating, because a straddling section is
    measured on both sides of the boundary; this covers the case only the final
    geometry reveals, where a column translation moved the horizontal intervals
    a row corridor's blockers are selected by.
    """
    path = SPANNING_CORPUS[0]
    graph, plan = _observe_planned_straddler(path)
    settlement = settle_route_envelopes(graph, plan)
    translation = next(
        item for item in settlement.translations if item.spanning_section_ids
    )
    reservation = next(
        item for item in plan.reservations if item.id in translation.reservation_ids
    )
    realised = realise_reservation(
        graph, reservation, coordinate_translations=settlement.coordinate_translations
    )
    assert realised is not None
    held = translation.spanning_section_ids[0]

    def bound_by_the_straddling_section(target, item, **kwargs):
        return replace(realised, positive_blocker_ids=(f"section-header:{held}",))

    with mock.patch.object(
        envelope_settlement,
        "realise_reservation",
        side_effect=bound_by_the_straddling_section,
    ):
        with pytest.raises(PhaseInvariantError, match="straddle that boundary"):
            envelope_settlement._assert_spanning_sections_bound_nothing_settled(
                graph,
                plan,
                (translation,),
                settlement.coordinate_translations,
            )


def test_settlement_rejects_a_translation_that_narrows_a_separation() -> None:
    """The monotone claim is checked, not assumed.

    Every facing pair of boxes is re-measured after the sweep, so a
    translation that closed a satisfied gap fails instead of shipping.
    """
    path = DEFICIT_CORPUS[0]
    graph, plan = _observe(path)
    before = _geometry(graph)
    real_shift = envelope_settlement.shift_section

    def shift_the_wrong_way(target, section, dx=0.0, dy=0.0):
        real_shift(target, section, dx=-dx, dy=-dy)

    with mock.patch.object(
        envelope_settlement, "shift_section", side_effect=shift_the_wrong_way
    ):
        with pytest.raises(PhaseInvariantError, match="narrowed the row separation"):
            settle_route_envelopes(graph, plan)
    assert _geometry(graph) == before


@pytest.mark.parametrize("path", DEFICIT_CORPUS, ids=lambda item: item.name)
def test_the_column_phase_never_narrows_a_row_corridor(path: Path) -> None:
    """Why the row phase needs no recheck after the columns settle.

    A column translation writes only x, so it cannot move an edge a row corridor
    is measured between; it reaches one only by changing which sections the
    corridor's run overlaps.  Measured on the settled geometry, no row corridor
    comes out narrower than the row phase left it.
    """
    graph, plan = _observe(path)
    row_reservations = tuple(
        item for item in plan.reservations if isinstance(item.region, RowGapRegion)
    )
    assert row_reservations, "fixture publishes no row corridor"
    settlement = settle_route_envelopes(graph, plan)
    row_translations = tuple(
        item for item in settlement.translations if item.axis is SettlementAxis.ROW
    )
    for reservation in row_reservations:
        after = realise_reservation(
            graph,
            reservation,
            coordinate_translations=settlement.coordinate_translations,
        )
        row_only = realise_reservation(
            graph,
            reservation,
            coordinate_translations=tuple(
                envelope_settlement._reservation_coordinate_translation(item, plan)
                for item in row_translations
            ),
        )
        if after is None or row_only is None:
            continue
        assert after.available_width >= row_only.available_width - 0.01


def test_a_column_phase_that_narrows_a_row_corridor_is_rejected() -> None:
    """The one case the independence argument does not cover fails loudly.

    A section spanning across a translated column boundary stays put while a
    run crossing that boundary lengthens, so it can end up inside a corridor
    that cleared it.  Settlement refuses that layout rather than re-settling
    against a constraint set its own column phase moved.
    """
    graph, plan = _observe(DEFICIT_CORPUS[0])
    reservation = next(
        item for item in plan.reservations if isinstance(item.region, RowGapRegion)
    )
    with pytest.raises(PhaseInvariantError, match="narrowed the row corridor"):
        envelope_settlement._assert_the_column_phase_left_the_row_phase_standing(
            {reservation.id: 96.0}, {reservation.id: 48.0}
        )


@pytest.mark.parametrize("path", DEFICIT_CORPUS, ids=lambda item: item.name)
def test_settlement_preserves_frozen_local_geometry(path: Path) -> None:
    """Only whole-row and whole-column offsets move; nothing moves inside a
    section, and no bbox is resized."""
    graph, plan = _observe(path)
    before = _section_local_geometry(graph)
    settle_route_envelopes(graph, plan)
    assert _section_local_geometry(graph) == before


def test_settlement_freezes_semantic_ownership_but_adopts_reservation_ids() -> None:
    path = ROOT / "examples" / "rnaseq_sections.mmd"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        offsets = compute_station_offsets(graph)
        observation = observe_route_edges(graph, station_offsets=offsets)

    frozen_routes = deepcopy(observation.routes)
    route_mutations = {
        "route_system_id": "settlement-mutated-system",
        "emission_member_id": "settlement-mutated-member",
        "route_system_disposition": "settlement-mutated-disposition",
        "route_plan_ids": ("settlement-mutated-plan",),
        "route_system_owned_segment_ranks": (len(frozen_routes[0].points),),
    }
    for attribute, value in route_mutations.items():
        realised_routes = deepcopy(observation.routes)
        owned_route = next(
            route for route in realised_routes if route.route_system_id is not None
        )
        setattr(owned_route, attribute, value)
        with pytest.raises(
            LayoutInvariantError,
            match="changed route topology or geometry ownership",
        ):
            _assert_settlement_decisions_frozen(
                frozen_routes,
                observation.plan,
                realised_routes,
                observation.plan,
            )

    realised_routes = deepcopy(observation.routes)
    geometry_plan = observation.plan.member_geometry_plans[0]
    realised_plan = replace(
        observation.plan,
        member_geometry_plans=(
            replace(geometry_plan, member_id="settlement-mutated-member"),
            *observation.plan.member_geometry_plans[1:],
        ),
    )
    with pytest.raises(
        LayoutInvariantError,
        match="changed route-system planning decisions",
    ):
        _assert_settlement_decisions_frozen(
            frozen_routes,
            observation.plan,
            realised_routes,
            realised_plan,
        )

    for route in realised_routes:
        if route.route_system_id is not None:
            route.route_reservation_ids = ("rerouted-reservation",)
    _assert_settlement_decisions_frozen(
        frozen_routes,
        observation.plan,
        realised_routes,
        observation.plan,
    )
    _attach_published_reservation_attribution(realised_routes, observation.plan)

    reservations_by_member = reservation_ids_by_claimant_member(
        observation.plan.reservations
    )
    for route in realised_routes:
        if route.route_system_id is not None:
            assert route.route_reservation_ids == tuple(
                reservations_by_member.get(
                    EmissionMemberId(route.emission_member_id or ""), ()
                )
            )


@pytest.mark.parametrize(
    "path", tuple(DIRECTION_CORPUS.values()), ids=tuple(DIRECTION_CORPUS)
)
def test_one_axis_based_implementation_covers_every_flow_direction(path: Path) -> None:
    """Narrowing a satisfied corridor is recovered whatever the flow direction.

    Settlement keys on grid rows and columns, never on a section's flow
    direction, so injecting the same deficit into an LR, RL, TB, or BT layout
    must be answered by the same pass.
    """
    graph, plan = _observe(path)
    query = build_route_plan_query(plan)
    # Every corridor at one boundary shares its translation, so squeeze by the
    # tightest of them: any more and a second corridor drives the deficit.
    by_boundary: dict[tuple[SettlementAxis, int], list] = {}
    for reservation in plan.reservations:
        region = reservation.region
        if isinstance(region, RowGapRegion):
            key = (SettlementAxis.ROW, region.lower_row)
        elif isinstance(region, ColumnGapRegion):
            key = (SettlementAxis.COLUMN, region.right_column)
        else:
            continue
        realised = query.realised_reservation(reservation.id)
        if realised is not None:
            by_boundary.setdefault(key, []).append((reservation, realised))

    (axis, boundary), residents = max(
        by_boundary.items(),
        key=lambda item: min(got.capacity_slack for _res, got in item[1]),
    )
    target, tightest = min(residents, key=lambda pair: pair[1].capacity_slack)
    shortfall = 4.0
    _narrow(graph, axis, boundary, tightest.capacity_slack + shortfall)

    settlement = settle_route_envelopes(graph, plan)
    injected = [
        item
        for item in settlement.translations
        if (item.axis, item.boundary) == (axis, boundary)
    ]
    assert len(injected) == 1
    assert injected[0].amount == pytest.approx(shortfall, abs=1.0)
    recovered = realise_reservation(graph, target)
    assert recovered is not None
    assert recovered.capacity_slack >= 0.0


def test_a_section_straddling_a_boundary_bounds_neither_side_of_it() -> None:
    """A straddling section occupies a boundary rather than bounding it.

    Its bottom edge lies below the boundary and its header above, so neither
    edge is a clearance the boundary offers.  Counting it on both sides
    manufactures a width measured from a box to itself, which is negative by
    that box's own height.
    """
    path = ROOT / "tests" / "fixtures" / "genomeassembly_organellar.mmd"
    graph, _plan = _observe(path)
    straddling = {
        key
        for key, section in graph.sections.items()
        if section.grid_row <= 1 < section.grid_row + section.grid_row_span - 1
    }
    assert straddling, "fixture no longer straddles the row 1/2 boundary"
    measurement = route_reservations._row_region_measurement(
        graph,
        RowGapRegion(1, 2),
        CorridorMeasurementScope.TOPOLOGY_SPAN,
        GridSpan(0, 4, 0, 3),
        0.0,
        10_000.0,
    )
    named = {
        blocker.partition(":")[2]
        for blocker in (
            measurement.negative_blocker_ids + measurement.positive_blocker_ids
        )
    }
    assert not named & straddling
    assert measurement.end > measurement.start


def test_a_boundary_every_section_straddles_is_not_a_corridor_region() -> None:
    """A boundary with no section wholly on one side has no width to offer.

    The measurement raises rather than inventing one, which is what tells the
    region search to look for the region the corridor actually runs in.
    """
    path = ROOT / "tests" / "fixtures" / "genomeassembly_organellar.mmd"
    graph, _plan = _observe(path)
    with pytest.raises(ValueError, match="no settled blocker"):
        route_reservations._row_region_measurement(
            graph,
            RowGapRegion(0, 1),
            CorridorMeasurementScope.TOPOLOGY_SPAN,
            GridSpan(0, 4, 0, 3),
            0.0,
            10_000.0,
        )


def test_every_corridor_the_organellar_map_publishes_is_allocatable() -> None:
    """Five sections straddle this map's row 0/1 boundary at once.

    Every row and column corridor it publishes names a boundary with sides to
    measure, and every one of them fits, so settlement declines nothing.
    """
    path = ROOT / "tests" / "fixtures" / "genomeassembly_organellar.mmd"
    graph, plan = _observe(path)
    settlement = settle_route_envelopes(graph, plan)
    assert settlement.shortfalls == ()
    checked = 0
    for reservation in plan.reservations:
        if not isinstance(reservation.region, RowGapRegion | ColumnGapRegion):
            continue
        realised = realise_reservation(
            graph,
            reservation,
            coordinate_translations=settlement.coordinate_translations,
        )
        assert realised is not None
        assert realised.available_width > 0
        assert realised.capacity_slack >= -0.01
        checked += 1
    assert checked


@pytest.mark.parametrize(
    "path",
    (
        TOPOLOGIES / "merge_right_entry.mmd",
        ROOT / "tests" / "fixtures" / "genomeassembly_organellar.mmd",
        ROOT / "examples" / "genomeassembly_staggered.mmd",
    ),
    ids=lambda path: path.name,
)
def test_planned_convergence_reroutes_strictly_with_settled_column_bands(
    path: Path,
) -> None:
    """Settled column-band reroutes preserve strict reservation invariants."""
    _rendered_plan(path)


MIGRATED_SYSTEMS = (
    TOPOLOGIES / "exit_run_three_drop_columns.mmd",
    TOPOLOGIES / "merge_trunk_out_of_range_section.mmd",
    ROOT / "tests" / "fixtures" / "ambiguous_exit_continuation.mmd",
    TOPOLOGIES / "merge_bottom_row_bypass.mmd",
    TOPOLOGIES / "merge_feeder_shared_channel_gap.mmd",
    TOPOLOGIES / "funcprofiler_upstream.mmd",
    TOPOLOGIES / "merge_right_entry.mmd",
    ROOT / "examples" / "genomeassembly.mmd",
    ROOT / "tests" / "fixtures" / "genomeassembly_organellar.mmd",
)

COMPATIBILITY_RESOURCE_FREE_CORPUS = (
    TOPOLOGIES / "off_track_input_above_consumer.mmd",
    TOPOLOGIES / "right_entry_over_top_tall_upstream.mmd",
    ROOT / "examples" / "differentialabundance.mmd",
)


@pytest.mark.parametrize(
    "path", COMPATIBILITY_RESOURCE_FREE_CORPUS, ids=lambda item: item.name
)
def test_compatibility_systems_publish_no_settlement_resources(path: Path) -> None:
    graph, plan = _observe(path)
    compatibility_ids = {
        system.id
        for system in plan.systems
        if system.disposition.value == "compatibility"
    }

    assert compatibility_ids
    assert not [
        reference
        for reference in plan.shared_references
        if reference.system_id in compatibility_ids
    ]
    assert not [
        demand for demand in plan.demands if demand.system_id in compatibility_ids
    ]
    assert not [
        reservation
        for reservation in plan.reservations
        if reservation.system_id in compatibility_ids
    ]
    settlement = settle_route_envelopes(graph, plan)
    planned_reservation_ids = {
        reservation.id
        for reservation in plan.reservations
        if reservation.system_id not in compatibility_ids
    }
    assert {
        reservation_id
        for translation in settlement.translations
        for reservation_id in translation.reservation_ids
    } <= planned_reservation_ids
    if compatibility_ids == {system.id for system in plan.systems}:
        assert settlement.translations == ()


@pytest.mark.parametrize("path", MIGRATED_SYSTEMS, ids=lambda item: item.name)
def test_migrated_systems_are_not_short_of_corridor(path: Path) -> None:
    observed = _rendered_plan(path, permissive=True)
    assert _capacity_deficits(observed.route_plan) == {}


@pytest.mark.parametrize("path", DEFICIT_CORPUS, ids=lambda item: item.name)
def test_a_failed_settlement_leaves_the_graph_as_it_found_it(path: Path) -> None:
    """Settlement writes many sections in sequence; a failure rolls all of it
    back rather than leaving a part-translated graph."""
    graph, plan = _observe(path)
    before = _geometry(graph)
    boom = RuntimeError("settlement failed")

    def explode(*_args, **_kwargs):
        raise boom

    with mock.patch.object(envelope_settlement, "realise_reservation", explode):
        with pytest.raises(RuntimeError) as caught:
            settle_route_envelopes(graph, plan)
    assert caught.value is boom
    assert _geometry(graph) == before


@pytest.mark.parametrize("path", DEFICIT_CORPUS, ids=lambda item: item.name)
def test_settlement_rolls_back_a_failure_that_lands_mid_translation(
    path: Path,
) -> None:
    graph, plan = _observe(path)
    before = _geometry(graph)
    moved: list[float] = []
    real = envelope_settlement.shift_section

    def move_then_fail(*args, **kwargs):
        real(*args, **kwargs)
        moved.append(1.0)
        raise RuntimeError("settlement failed")

    with mock.patch.object(envelope_settlement, "shift_section", move_then_fail):
        with pytest.raises(RuntimeError):
            settle_route_envelopes(graph, plan)
    assert moved, "the fixture never reached a translation, so nothing was rolled back"
    assert _geometry(graph) == before


@pytest.mark.parametrize("path", DEFICIT_CORPUS + SETTLED_CORPUS, ids=lambda i: i.name)
def test_one_pass_settles_everything_one_ledger_asks_for(path: Path) -> None:
    """Settlement allocates against fixed demand, so it needs no second pass.

    A second productive pass against the same ledger would mean the first left
    a deficit it owned, which is the property the single-pass sweep exists to
    rule out.
    """
    graph, plan = _observe(path)
    first = settle_route_envelopes(graph, plan)
    second = settle_route_envelopes(graph, plan)
    assert second.translations == ()
    assert {item.reservation_id for item in second.shortfalls} == {
        item.reservation_id for item in first.shortfalls
    }


@pytest.mark.parametrize("path", DEFICIT_CORPUS, ids=lambda item: item.name)
def test_settlement_does_not_chase_the_ledger_the_reroute_publishes(
    path: Path,
) -> None:
    """Re-routing settled geometry yields a different ledger; that is exactly
    why settlement must not iterate against it."""
    graph, plan = _observe(path)
    before = {item.id for item in plan.reservations}
    assert settle_route_envelopes(graph, plan).translations
    rerouted = observe_route_edges(
        graph, station_offsets=compute_station_offsets(graph)
    ).plan
    after = {item.id for item in rerouted.reservations}
    assert before or after
    # The ledgers may differ; what must not happen is settlement treating the
    # new one as more work to do against the old geometry.
    assert settle_route_envelopes(graph, plan).translations == ()


PLANNED_SYSTEMS = (
    *MIGRATED_SYSTEMS,
    REGRESSIONS / "cross_column_perp_entry_overflow.mmd",
)


@pytest.mark.parametrize("path", PLANNED_SYSTEMS, ids=lambda item: item.name)
def test_a_planned_system_publishes_no_compatibility_exit(path: Path) -> None:
    """A compatibility exit is evidence about a system the planner declined, so a
    fixture whose systems it owns must publish none."""
    graph, plan = _settled(path)
    assert plan.convergence_plans
    assert all(item.legacy_reason is None for item in plan.convergence_plans)
    assert attribute_compatibility_systems(graph, plan) == ()
    assert not [
        item
        for item in _rendered_plan(path, permissive=True).route_plan.diagnostics
        if item.code == "convergence-settlement-exit"
    ]


def _widest_slack_row_reservation(graph, plan):
    """The row reservation with the most spare width, and its boundary.

    Every corridor at one boundary shares its translation, so squeezing the
    roomiest boundary by the least-roomy resident's slack is what makes exactly
    one claim short.
    """
    by_boundary: dict[int, list] = {}
    for reservation in plan.reservations:
        if not isinstance(reservation.region, RowGapRegion):
            continue
        realised = realise_reservation(graph, reservation)
        if realised is not None:
            by_boundary.setdefault(reservation.region.lower_row, []).append(
                (reservation, realised)
            )
    boundary, residents = max(
        by_boundary.items(),
        key=lambda item: min(got.capacity_slack for _res, got in item[1]),
    )
    target, _ = min(residents, key=lambda pair: pair[1].capacity_slack)
    return target, boundary


def test_a_corridor_narrower_than_its_reservation_fails_the_strict_path() -> None:
    """A route drawn through a violated hard clearance is rejected, and the
    rejection names everything needed to act on it: who claimed the corridor,
    the span it claimed, what bounds it, and both widths."""
    graph, plan, polylines = _observe_drawn(TOPOLOGIES / "convergence_fold_diamond.mmd")
    settlement = settle_route_envelopes(graph, plan)
    target, boundary = _widest_slack_row_reservation(graph, plan)
    _narrow_reservation(graph, target)

    squeezed = realise_reservation(graph, target)
    assert squeezed is not None and squeezed.capacity_slack < 0
    with pytest.raises(LayoutInvariantError) as caught:
        assert_reservations_are_settled(graph, plan, settlement, polylines, strict=True)

    message = str(caught.value)
    for claimant in target.claimant_member_ids:
        assert claimant in message
    assert f"columns {target.span.min_column}-{target.span.max_column}" in message
    assert f"rows {target.span.min_row}-{target.span.max_row}" in message
    for blocker in squeezed.negative_blocker_ids + squeezed.positive_blocker_ids:
        assert blocker in message
    assert f"{squeezed.required_width:.1f}px" in message
    assert f"{squeezed.available_width:.1f}px" in message


def test_a_canvas_corridor_narrower_than_it_claims_fails_the_strict_path() -> None:
    """A canvas margin is widenable too, so its deficit is a rejection.

    Growing the demand past the margin stands in for an arrangement that starves
    it: the guard has to refuse the render rather than warn and draw it.
    """
    path = TOPOLOGIES / "fan_in_merge.mmd"
    plan = _rendered_plan(path, permissive=True).route_plan
    target = next(
        item for item in plan.reservations if isinstance(item.region, CanvasRegion)
    )
    realised = next(
        item for item in plan.realised_reservations if item.reservation_id == target.id
    )
    assert realised.capacity_slack >= 0.0
    assert_canvas_corridors_hold_their_claims(plan, strict=True)

    starved = replace(
        realised,
        required_width=realised.available_width + 4.0,
        capacity_slack=-4.0,
    )
    with pytest.raises(LayoutInvariantError, match="less clearance than it reserved"):
        assert_canvas_corridors_hold_their_claims(
            replace(plan, realised_reservations=(starved,)), strict=True
        )


def test_the_canvas_edge_clearance_bounds_every_theme() -> None:
    """No brand draws a stroke the canvas-margin demand fails to cover.

    The clearance is resolved before a theme is chosen, so it has to hold for
    the widest stroke any brand sets rather than the default one.
    """
    widest = max(theme.line_width for theme in THEMES.values())
    assert WIDEST_THEME_LINE_WIDTH == pytest.approx(widest)
    assert DIRECTIONAL_MARKER_HALF_EXTENT == pytest.approx(
        max(theme.directional_marker_size for theme in THEMES.values())
    )
    assert CANVAS_EDGE_CLEARANCE >= DIRECTIONAL_MARKER_HALF_EXTENT + widest / 2


def test_a_coarsened_stroke_widens_the_canvas_margin_it_reserves() -> None:
    """``stroke_scale`` multiplies the stroke, so the margin it needs multiplies.

    The chevron's arms are drawn at a fixed size, so only the half-stroke term of
    the clearance tracks the scale.  Reading the unscaled constant would leave a
    coarsened render short by half the extra stroke weight.
    """
    assert canvas_edge_clearance() == pytest.approx(CANVAS_EDGE_CLEARANCE)
    with stroke_scale_context(2.0):
        assert canvas_edge_clearance() == pytest.approx(
            DIRECTIONAL_MARKER_HALF_EXTENT + WIDEST_THEME_LINE_WIDTH
        )
        assert canvas_edge_clearance() - CANVAS_EDGE_CLEARANCE == pytest.approx(
            WIDEST_THEME_LINE_WIDTH / 2
        )


def test_a_short_canvas_margin_fails_the_strict_path_at_full_capacity() -> None:
    """A corridor with every pixel it reserved can be drawn hard against the edge.

    Total capacity is spent across both of a canvas corridor's sides, so a
    corridor banking its whole surplus on the content side measures as having
    exactly the room it asked for while its ink is drawn through the canvas
    margin.  The margin is what a stroke and a direction chevron are clipped
    against, so it is the number the strict path refuses.
    """
    path = TOPOLOGIES / "around_section_below.mmd"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        plan = build_observed_render_plan(graph, resolve_theme(None, graph)).route_plan
    reservation = next(
        item
        for item in plan.reservations
        if isinstance(item.region, CanvasRegion) and item.region.side is CanvasSide.LEFT
    )
    realised = next(
        item
        for item in plan.realised_reservations
        if item.reservation_id == reservation.id
    )
    hugging = replace(
        realised,
        negative_side_slack=-4.0,
        positive_side_slack=realised.positive_side_slack + 4.0,
    )
    assert hugging.capacity_slack >= 0.0
    assert canvas_edge_slack(reservation.region, hugging) == pytest.approx(-4.0)

    with pytest.raises(LayoutInvariantError) as caught:
        assert_canvas_corridors_hold_their_claims(
            replace(plan, realised_reservations=(hugging,)), strict=True
        )
    message = str(caught.value)
    assert "less clearance than it reserved" in message
    assert "left canvas margin" in message
    assert "-4.0px" in message


@pytest.mark.parametrize(
    ("path", "side"),
    (
        (
            TOPOLOGIES / "lr_perp_top_exit_perp_entry_diverging.mmd",
            CanvasSide.TOP,
        ),
        (TOPOLOGIES / "fan_in_merge.mmd", CanvasSide.BOTTOM),
        (TOPOLOGIES / "fanout_bundle_plus_spurs.mmd", CanvasSide.LEFT),
        (
            TOPOLOGIES / "bottom_exit_stacked_right_entry_fan.mmd",
            CanvasSide.RIGHT,
        ),
    ),
    ids=lambda item: item.name if isinstance(item, Path) else item.value,
)
def test_every_canvas_corridor_holds_its_content_side(
    path: Path, side: CanvasSide
) -> None:
    """The realised claim measures the content drawn beside its own run."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        plan = build_observed_render_plan(graph, resolve_theme(None, graph)).route_plan
    query = build_route_plan_query(plan)
    reservation = next(
        item
        for item in plan.reservations
        if isinstance(item.region, CanvasRegion) and item.region.side is side
    )
    realised = query.realised_reservation(reservation.id)
    assert realised is not None
    edge_is_negative = side in {CanvasSide.TOP, CanvasSide.LEFT}
    content_side_slack = (
        realised.positive_side_slack
        if edge_is_negative
        else realised.negative_side_slack
    )
    assert content_side_slack >= -COORD_TOLERANCE
    assert canvas_edge_slack(reservation.region, realised) >= -0.01
    assert not [
        item
        for item in plan.reservation_diagnostics
        if item.reservation_id == reservation.id and item.code == "reservation-deficit"
    ]


@pytest.mark.parametrize(
    ("fixture", "expected_blocker"),
    (
        (
            "lr_perp_top_exit_perp_entry_diverging.mmd",
            ("section-top:sec1", "section-top:sec2"),
        ),
    ),
)
def test_a_top_canvas_corridor_is_bounded_by_content_over_its_own_run(
    fixture: str, expected_blocker: tuple[str, ...]
) -> None:
    """A header only bounds the longitudinal interval occupied by its ink."""
    path = TOPOLOGIES / fixture
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        plan = build_observed_render_plan(graph, resolve_theme(None, graph)).route_plan
    reservation = next(
        item
        for item in plan.reservations
        if isinstance(item.region, CanvasRegion) and item.region.side is CanvasSide.TOP
    )
    realised = next(
        item
        for item in plan.realised_reservations
        if item.reservation_id == reservation.id
    )
    assert realised.positive_blocker_ids == expected_blocker


def test_a_short_canvas_content_side_fails_the_strict_path() -> None:
    """A content blocker is as hard a boundary as the canvas edge."""
    path = TOPOLOGIES / "lr_perp_top_exit_perp_entry_diverging.mmd"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        plan = build_observed_render_plan(graph, resolve_theme(None, graph)).route_plan
    reservation = next(
        item
        for item in plan.reservations
        if isinstance(item.region, CanvasRegion) and item.region.side is CanvasSide.TOP
    )
    realised = next(
        item
        for item in plan.realised_reservations
        if item.reservation_id == reservation.id
    )
    short = replace(
        realised,
        capacity_slack=max(realised.capacity_slack, 0.0),
        negative_side_slack=max(realised.negative_side_slack, 0.0),
        positive_side_slack=-4.0,
    )

    with pytest.raises(LayoutInvariantError, match="content"):
        assert_canvas_corridors_hold_their_claims(
            replace(plan, realised_reservations=(short,)), strict=True
        )


def test_a_canvas_edge_clearance_is_what_is_drawn_beside_the_edge() -> None:
    """The margin a canvas corridor needs is its stroke plus a chevron.

    A turn beside the canvas inscribes its arc inboard of the centreline, so
    demanding a turn radius there charges the corridor for room nothing occupies
    -- which flagged seven corpus maps whose runs sit 6px off a canvas edge they
    never cross.
    """
    assert CANVAS_EDGE_CLEARANCE < CURVE_RADIUS

    for path in CANVAS_CLEARANCE_CORPUS:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
            graph.permissive = True
            theme = resolve_theme(None, graph)
            plan = build_observed_render_plan(graph, theme).route_plan
        query = build_route_plan_query(plan)
        checked = 0
        for reservation in plan.reservations:
            if not isinstance(reservation.region, CanvasRegion):
                continue
            realised = query.realised_reservation(reservation.id)
            if realised is None:
                continue
            checked += 1
            assert canvas_edge_slack(reservation.region, realised) >= -0.01, (
                path,
                reservation.description,
            )
        assert checked, path


def test_an_unmet_handed_demand_fails_the_strict_path() -> None:
    """Settlement's own postcondition is enforced, not just reported: a demand
    it was handed and did not meet stops the render rather than being drawn."""
    graph, plan, polylines = _observe_drawn(TOPOLOGIES / "convergence_fold_diamond.mmd")
    reservation = next(
        item
        for item in plan.reservations
        if isinstance(item.region, RowGapRegion | ColumnGapRegion)
    )
    shortfall = SettlementShortfall(
        reservation.id, reservation.claimant_member_ids, 64.0, 12.0
    )
    settlement = EnvelopeSettlement((), (shortfall,))

    with pytest.raises(LayoutInvariantError) as caught:
        assert_reservations_are_settled(graph, plan, settlement, polylines, strict=True)
    message = str(caught.value)
    for claimant in reservation.claimant_member_ids:
        assert claimant in message
    assert "64.00px" in message
    assert "12.00px" in message

    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        assert_reservations_are_settled(
            graph, plan, settlement, polylines, strict=False
        )
    assert any(shortfall.message in str(item.message) for item in caught_warnings)


@pytest.mark.parametrize("path", DEFICIT_CORPUS, ids=lambda item: item.name)
def test_a_reserved_inter_row_corridor_never_forces_the_router_to_improvise(
    path: Path,
) -> None:
    """The router's narrow-band fallback biases a run against the header badge
    it cannot clear.  A corridor that owns a reservation must never leave the
    router in that position, so the band it measures has to fit outright."""
    observed = _rendered_plan(path, permissive=True)
    query = build_route_plan_query(observed.route_plan)
    checked = 0
    for reservation in observed.route_plan.reservations:
        if not isinstance(reservation.region, RowGapRegion):
            continue
        realised = query.realised_reservation(reservation.id)
        if realised is None:
            continue
        checked += 1
        assert _inter_row_band_fits(realised.region_start, realised.region_end)
    assert checked


def _narrow(graph, axis: SettlementAxis, boundary: int, amount: float) -> None:
    """Translate everything from *boundary* onward back toward the content
    above or left of it, eating the corridor settlement then has to restore."""
    for section in graph.sections.values():
        index = section.grid_row if axis is SettlementAxis.ROW else section.grid_col
        if index < boundary:
            continue
        if axis is SettlementAxis.ROW:
            section.bbox_y -= amount
        else:
            section.bbox_x -= amount
        for station_id in section.station_ids:
            for item in (graph.stations.get(station_id), graph.ports.get(station_id)):
                if item is None:
                    continue
                if axis is SettlementAxis.ROW:
                    item.y -= amount
                else:
                    item.x -= amount


def _narrow_reservation(graph, reservation, *, excess: float = 4.0):
    """Make one realised row or column reservation short by ``excess`` pixels."""
    realised = realise_reservation(graph, reservation)
    assert realised is not None
    if isinstance(reservation.region, RowGapRegion):
        axis = SettlementAxis.ROW
        boundary = reservation.region.lower_row
    elif isinstance(reservation.region, ColumnGapRegion):
        axis = SettlementAxis.COLUMN
        boundary = reservation.region.right_column
    else:
        raise TypeError(f"unsupported reservation region: {reservation.region!r}")
    _narrow(graph, axis, boundary, realised.capacity_slack + excess)
    return realised


def _added_diagnostics(published, held):
    return published.diagnostics[len(held.diagnostics) :]


def test_a_corridor_the_reroute_resizes_is_named_rather_than_invisible() -> None:
    """The demand a re-route changes without adding or dropping a corridor.

    Kind, boundary, span and lane count are all the same on both sides, so the
    only thing separating the two ledgers is the width each asks for.  A widening
    sized against the first is not the widening the second wants, which is the
    difference this has to carry.
    """
    path = TOPOLOGIES / "convergence_fold_diamond.mmd"
    _graph, frozen = _observe(path)
    target = next(
        item
        for item in frozen.reservations
        if isinstance(item.region, RowGapRegion | ColumnGapRegion)
    )
    widened = replace(
        target,
        peer_width=target.peer_width + 8.0,
        minimum_width=target.minimum_width + 8.0,
    )
    routed = replace(
        frozen,
        reservations=tuple(
            widened if item.id == target.id else item for item in frozen.reservations
        ),
    )

    unchanged = envelope_settlement.attach_reroute_ledger_delta(frozen, frozen, frozen)
    assert _added_diagnostics(unchanged, frozen) == ()

    published = envelope_settlement.attach_reroute_ledger_delta(frozen, frozen, routed)
    (added,) = _added_diagnostics(published, frozen)
    assert added.code == "reroute-ledger-demand-rewidened"
    assert not added.blocking
    assert target.description in added.message
    assert f"{widened.minimum_width:.2f}px" in added.message
    assert f"{target.minimum_width:.2f}px" in added.message


def test_the_settled_reroute_reports_the_widths_it_asks_for_afresh() -> None:
    """The same difference, on a map that produces it without being staged.

    The planned merge reroutes inside the frozen corridor and publishes its
    independently observed width as a diagnostic rather than changing the
    ledger settlement consumed.
    """
    path = TOPOLOGIES / "merge_around_below_leftmost.mmd"
    route_plan = _rendered_plan(path).route_plan
    resized = [
        item
        for item in route_plan.diagnostics
        if item.code == "reroute-ledger-demand-rewidened"
    ]
    assert len(resized) == 1
    assert all(not item.blocking for item in resized)
    assert all("row gap 0/1" in item.message for item in resized)


# --- the clearance a boundary owes, as settlement's second demand -------------

LABEL_WRAP_ROW_GAP = TOPOLOGIES / "render_labelwrap_row_gap.mmd"

# Fixtures whose render-time bbox growth leaves a row boundary short of the
# clearance it owes, with no corridor reserved there to state the demand.
CLEARANCE_CORPUS = (
    LABEL_WRAP_ROW_GAP,
    TOPOLOGIES / "manual_rl_row_nonconsumer_bypass.mmd",
    TOPOLOGIES / "packed_cell_cellmate_bypass.mmd",
    TOPOLOGIES / "packed_cell_cellmate_bypass_adjacent.mmd",
)

# Rail layouts, whose row pitch is the interchange idiom's rather than the
# declared section gap's.
RAIL_CORPUS = (
    ROOT / "examples" / "sarek_metro.mmd",
    ROOT / "tests" / "fixtures" / "rail_pitch_vs_labels.mmd",
)


def _clearance(section_y_gap: float = SECTION_Y_GAP):
    return partial(measure_row_gap_clearance, section_y_gap=section_y_gap)


def _cols_overlap(first, second) -> bool:
    first_end = first.grid_col + first.grid_col_span - 1
    second_end = second.grid_col + second.grid_col_span - 1
    return not (first_end < second.grid_col or second_end < first.grid_col)


def _facing_across_row_boundary(graph, boundary: int):
    """The (upper, lower) box pairs whose columns overlap across *boundary*.

    Only a pair sharing column space owes the boundary anything: two boxes in
    different columns can sit closer vertically without interfering.
    """
    upper = [
        s
        for s in graph.sections.values()
        if s.grid_row + s.grid_row_span - 1 == boundary - 1 and s.bbox_h > 0
    ]
    lower = [
        s for s in graph.sections.values() if s.grid_row == boundary and s.bbox_h > 0
    ]
    return [(u, low) for u in upper for low in lower if _cols_overlap(u, low)]


def _row_boundary_clearance(graph, boundary: int) -> float:
    """The tightest drawn clearance across *boundary*, over the facing pairs."""
    return min(
        low.bbox_y - (u.bbox_y + u.bbox_h)
        for u, low in _facing_across_row_boundary(graph, boundary)
    )


def test_a_boundary_owing_clearance_with_no_corridor_is_still_settled() -> None:
    """The demand a reservation ledger cannot state, and settlement pays anyway.

    ``render_labelwrap_row_gap`` wraps three station labels, which grows its QC
    box downward and leaves the row below it inside the gap that boundary is
    declared.  No route crosses the boundary, so the ledger holds no row-gap
    reservation at all and a sweep driven by reservations alone never visits it.
    The clearance demand is what puts the boundary in the sweep, and it is only
    measurable at the render boundary because the growth is the wrapped text's.
    """
    route_plan = _rendered_plan(LABEL_WRAP_ROW_GAP).route_plan
    assert [
        item
        for item in route_plan.reservations
        if isinstance(item.region, RowGapRegion)
    ] == []

    moves = [
        item
        for item in route_plan.diagnostics
        if item.code == "envelope-settlement-translation"
    ]
    assert len(moves) == 1
    assert "row boundary 1 widened by" in moves[0].message
    assert "clearance row boundary 1 owes" in moves[0].message

    settled, _plan = _settled(LABEL_WRAP_ROW_GAP)
    assert measure_row_gap_clearance(settled, SECTION_Y_GAP) == ()
    assert _row_boundary_clearance(settled, 1) >= SECTION_Y_GAP - 0.01


def test_the_clearance_demand_states_what_a_grown_box_ate() -> None:
    """The measurement half, exercised on geometry that carries the shortfall.

    Growing the upper box's bottom edge by hand is the same input a render-time
    label wrap produces, and lets the demand be read without a render.
    """
    graph, _plan = _observe(LABEL_WRAP_ROW_GAP)
    assert measure_row_gap_clearance(graph, SECTION_Y_GAP) == ()

    upper = max(
        (pair[0] for pair in _facing_across_row_boundary(graph, 1)),
        key=lambda item: item.bbox_y + item.bbox_h,
    )
    held = _row_boundary_clearance(graph, 1)
    upper.bbox_h += held - SECTION_Y_GAP + 12.0

    (demand,) = measure_row_gap_clearance(graph, SECTION_Y_GAP)
    assert demand.axis is SettlementAxis.ROW
    assert demand.boundary == 1
    assert demand.required == SECTION_Y_GAP
    assert demand.deficit == pytest.approx(12.0)
    assert demand.blocker_section_ids == (upper.id,)
    assert "clearance row boundary 1 owes the box above it" in demand.description


@pytest.mark.parametrize("path", CLEARANCE_CORPUS, ids=lambda item: item.name)
def test_no_row_boundary_the_render_draws_still_owes_clearance(path: Path) -> None:
    """Settlement's postcondition for its second demand, on the drawn geometry."""
    settled, _plan = _settled(path)
    assert measure_row_gap_clearance(settled, SECTION_Y_GAP) == ()


def test_a_boundary_owing_both_demands_is_widened_once_by_the_larger() -> None:
    """Two demands at one coordinate are one translation, not two.

    A corridor deficit and a clearance deficit at the same boundary are paid by
    the same move, so the amount is the maximum of the two and never their sum --
    which is what makes settlement the single owner rather than one more owner.
    """
    path = TOPOLOGIES / "convergence_fold_diamond.mmd"

    def settle_with(extra: float | None):
        graph, plan = _observe(path)
        clearance = None
        if extra is not None:
            boundary = min(
                item.region.lower_row
                for item in plan.reservations
                if isinstance(item.region, RowGapRegion)
            )
            # Measured against live geometry, like the real one, so paying it
            # closes it rather than restating a figure the sweep cannot satisfy.
            wanted = _row_boundary_clearance(graph, boundary) + extra

            def clearance(live, boundary=boundary, wanted=wanted):
                deficit = wanted - _row_boundary_clearance(live, boundary)
                if deficit <= 0:
                    return ()
                return (
                    BoundaryClearanceDemand(
                        SettlementAxis.ROW,
                        boundary,
                        wanted,
                        deficit,
                        (),
                        f"a stated clearance at row boundary {boundary}",
                    ),
                )

        return settle_route_envelopes(graph, plan, clearance=clearance)

    def at_lowest_boundary(settlement):
        return min(settlement.translations, key=lambda item: item.boundary)

    corridor_only = at_lowest_boundary(settle_with(None))
    corridor_deficit = corridor_only.amount

    # A clearance demand smaller than the corridor's changes nothing.
    smaller = at_lowest_boundary(settle_with(corridor_deficit / 4))
    assert smaller.boundary == corridor_only.boundary
    assert smaller.amount == corridor_deficit
    assert smaller.clearance is None

    # A larger one takes over the same single translation, and is not added to it.
    larger = at_lowest_boundary(settle_with(corridor_deficit + 12.0))
    assert larger.boundary == corridor_only.boundary
    assert larger.clearance is not None
    assert larger.amount == pytest.approx(corridor_deficit + 12.0)
    assert larger.amount < 2 * corridor_deficit + 12.0
    assert larger.reservation_ids == corridor_only.reservation_ids


def test_a_clearance_demand_settlement_cannot_close_is_refused() -> None:
    """The postcondition is checked, not assumed from the ownership lemma."""
    graph, plan = _observe(LABEL_WRAP_ROW_GAP)
    before = _geometry(graph)

    def insatiable(_graph):
        return (
            BoundaryClearanceDemand(
                SettlementAxis.ROW, 1, SECTION_Y_GAP, 8.0, (), "an unpayable clearance"
            ),
        )

    with pytest.raises(PhaseInvariantError, match="left a boundary owing clearance"):
        settle_route_envelopes(graph, plan, clearance=insatiable)
    assert _geometry(graph) == before


@pytest.mark.parametrize("path", CLEARANCE_CORPUS, ids=lambda item: item.name)
def test_settling_a_clearance_demand_twice_is_an_exact_no_op(path: Path) -> None:
    graph, plan = _observe(path)
    settle_route_envelopes(graph, plan, clearance=_clearance())
    settled = _geometry(graph)
    second = settle_route_envelopes(graph, plan, clearance=_clearance())
    assert second.translations == ()
    assert _geometry(graph) == settled


@pytest.mark.parametrize("path", CLEARANCE_CORPUS, ids=lambda item: item.name)
def test_a_clearance_translation_never_narrows_a_separation(path: Path) -> None:
    graph, plan = _observe(path)
    before_rows, before_columns = _row_gaps(graph), _column_gaps(graph)
    settle_route_envelopes(graph, plan, clearance=_clearance())
    for key, gap in _row_gaps(graph).items():
        assert gap >= before_rows[key] - 0.01, f"row gap {key} narrowed"
    for key, gap in _column_gaps(graph).items():
        assert gap >= before_columns[key] - 0.01, f"column gap {key} narrowed"


@pytest.mark.parametrize("path", RAIL_CORPUS, ids=lambda item: item.name)
def test_a_rail_layout_keeps_the_row_pitch_its_idiom_set(path: Path) -> None:
    """Why the clearance demand is not raised for a rail layout.

    Rail mode pitches adjacent rows so that a line runs between them without
    turning.  Widening one of those boundaries to the declared section gap breaks
    that collinearity -- the inter-row runs acquire a diagonal, which is a route
    topology change and not the coordinate translation this stage is allowed -- so
    the render refuses it outright.  Defeating the exclusion is how that is
    measured rather than asserted, and it is what keeps the exclusion from being
    a vacuous one.
    """

    def observe(graph):
        reanchor_junctions(graph)
        offsets = compute_station_offsets(graph)
        return observe_route_edges(graph, station_offsets=offsets)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        source = prepare_graph(path.read_text(), source_dir=str(path.parent))
        assert source.has_rail_sections
        settled = _settled_render_graph(source, resolve_theme(None, source))
        held = observe(settled)
        before = _route_decision_fingerprint(held.routes)

        # The exclusion is load-bearing: the boundary really does owe clearance.
        (demand,) = measure_row_gap_clearance(settled, SECTION_Y_GAP)
        assert demand.deficit > 0

        settlement = settle_route_envelopes(settled, held.plan, clearance=_clearance())
        after = _route_decision_fingerprint(observe(settled).routes)

    (translation,) = settlement.translations
    assert translation.clearance is not None
    assert translation.boundary == demand.boundary

    # Flat inter-row runs the idiom drew become staircases, which is a decision
    # change the settlement contract forbids rather than a translation.
    changed = [(was, now) for was, now in zip(before, after) if was != now]
    assert changed, "paying the demand changed no route, so nothing was protected"
    for was, now in changed:
        assert len(was[4]) == 1, "the held run was not a single flat segment"
        assert len(now[4]) == 3, "the run did not become a staircase"
