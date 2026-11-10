"""The plan-time landing deal of a bundle's lines into an entry port.

Concentric turns keep each line on its own side of travel, so a bundle lands
in the order it leaves its source; the plan deals that order over the port's
own landing slots and every reconcile rises in it.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from cohort_shadow_oracle import shadow_failure, shadow_observation

from nf_metro.api import render_string
from nf_metro.layout.routing import core, invariants, member_geometry
from nf_metro.layout.routing import corridor_cohort_integration as cci
from nf_metro.layout.routing.common import (
    PeeloffTail,
    PortPeeloffBundle,
    peeloff_target_slots,
)
from nf_metro.layout.routing.context import _RoutingCtx
from nf_metro.layout.routing.landing_deal import (
    LandingDealRefusal,
    bundle_landing_deals,
    deal_by_exit_order,
    landing_order,
    planned_landing_orders,
)
from nf_metro.parser.model import MetroGraph, PortSide

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "regressions"
    / "cohort_top_exit_landing_order.mmd"
)
SHAPES = Path(__file__).parent / "data" / "landing_deal"
FIXTURE_PORT = "sec1__entry_left_1"


@dataclass
class _DealCall:
    ctx: _RoutingCtx
    targets: tuple[cci.CorridorCohortTarget, ...]
    decided: Mapping[str, Mapping[str, float]]
    refused: Mapping[str, LandingDealRefusal]


def _render(path: Path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        render_string(path.read_text(), source_dir=str(path.parent))


def _deal_calls(monkeypatch: pytest.MonkeyPatch, path: Path) -> list[_DealCall]:
    calls: list[_DealCall] = []

    def spy(
        ctx: _RoutingCtx, targets: Sequence[cci.CorridorCohortTarget]
    ) -> tuple[Any, Any]:
        decided, refused = bundle_landing_deals(ctx, targets)
        calls.append(_DealCall(ctx, tuple(targets), decided, refused))
        return decided, refused

    monkeypatch.setattr(member_geometry, "bundle_landing_deals", spy)
    _render(path)
    assert calls
    return calls


def _own_slots(call: _DealCall, port_id: str) -> dict[str, float]:
    return {
        target.edge_key[2]: target.endpoint_lane_coordinate
        for target in call.targets
        if target.edge_key[1] == port_id and target.endpoint_lane_coordinate is not None
    }


def test_cohort_final_compile_lands_the_bundle_in_exit_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three lines leave ``sec0``'s TOP exit left to right as l0, l1, l2 and
    turn right into ``sec1``'s LEFT entry, so they land top to bottom in that
    order; the port's own lane order would put l2 above l1."""
    plans: list[cci.CorridorCohortPlan] = []
    compile_plan = member_geometry.compile_corridor_cohort_plan

    def capture(*args: Any, **kwargs: Any) -> cci.CorridorCohortPlan:
        plan = compile_plan(*args, **kwargs)
        plans.append(plan)
        return plan

    # Patched outside the shadow observation so that each patch restores the
    # function it replaced: inside it, teardown would reinstate the shadow's
    # publishing compile for every later test in the process.
    monkeypatch.setattr(member_geometry, "compile_corridor_cohort_plan", capture)
    with shadow_observation():
        _render(FIXTURE)
    final = plans[-1]
    landings = {
        landing.edge_key[2]: landing.coordinate
        for landing in final.landings
        if landing.edge_key[1] == FIXTURE_PORT
    }

    assert sorted(landings, key=landings.__getitem__) == ["l0", "l1", "l2"]


def test_the_fixture_draws_as_planned() -> None:
    assert shadow_failure(FIXTURE) is None


@pytest.mark.parametrize(
    ("exit_side", "entry_side", "expected"),
    [
        pytest.param(
            PortSide.RIGHT,
            PortSide.LEFT,
            {"a": 120.0, "b": 124.0, "c": 128.0},
            id="LR",
        ),
        pytest.param(
            PortSide.LEFT,
            PortSide.RIGHT,
            {"a": 120.0, "b": 124.0, "c": 128.0},
            id="RL",
        ),
        pytest.param(
            PortSide.BOTTOM,
            PortSide.TOP,
            {"a": 120.0, "b": 124.0, "c": 128.0},
            id="TB",
        ),
        pytest.param(
            PortSide.TOP,
            PortSide.BOTTOM,
            {"a": 120.0, "b": 124.0, "c": 128.0},
            id="BT",
        ),
        pytest.param(
            PortSide.RIGHT,
            PortSide.TOP,
            {"a": 128.0, "b": 124.0, "c": 120.0},
            id="LR-turning-down",
        ),
        pytest.param(
            PortSide.TOP,
            PortSide.LEFT,
            {"a": 120.0, "b": 124.0, "c": 128.0},
            id="BT-turning-right",
        ),
    ],
)
def test_a_bundle_lands_on_the_side_of_travel_it_leaves_on(
    exit_side: PortSide, entry_side: PortSide, expected: dict[str, float]
) -> None:
    """A straight run keeps each line on its screen coordinate.  A turn keeps
    it on its side of travel: the right of rightward travel is +y and of
    downward travel -x, so turning down maps the lowest line to the left;
    the right of upward travel is +x, so turning right keeps the screen ends."""
    laterals = {"a": 0.0, "b": 4.0, "c": 8.0}

    assert deal_by_exit_order(
        exit_side, laterals, entry_side, (128.0, 120.0, 124.0)
    ) == (expected)


@pytest.mark.parametrize(
    ("shape", "port_id", "expected_order"),
    [
        pytest.param(
            "perpendicular_bottom_exit",
            "sec2__entry_right_2",
            ("l2", "l0"),
            id="LR-bottom-exit",
        ),
        pytest.param(
            "perpendicular_right_exit",
            "sec2__entry_left_3",
            ("l2", "l1", "l0"),
            id="TB-right-exit",
        ),
        pytest.param(
            "junction_source",
            "sec1__entry_right_3",
            ("l3", "l2", "l1"),
            id="junction-source",
        ),
        pytest.param(
            "multi_source",
            "sec3__entry_right_5",
            ("l3", "l1", "l2"),
            id="multi-source",
        ),
    ],
)
def test_the_deal_permutes_the_ports_own_slots(
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    port_id: str,
    expected_order: tuple[str, ...],
) -> None:
    """A perpendicular exit leaves at its riser lateral, a junction at its own
    offsets, and a port fed from several sources is dealt per source over that
    source's slots; every decision only permutes the port's own slots."""
    for call in _deal_calls(monkeypatch, SHAPES / f"{shape}.mmd"):
        deal = call.decided[port_id]

        assert landing_order(deal) == expected_order
        assert sorted(deal.values()) == sorted(_own_slots(call, port_id).values())


def test_a_multi_source_port_keeps_a_lone_sources_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for call in _deal_calls(monkeypatch, SHAPES / "multi_source.mmd"):
        own = _own_slots(call, "sec3__entry_right_5")

        assert call.decided["sec3__entry_right_5"]["l2"] == own["l2"]


@pytest.mark.parametrize(
    ("shape", "port_id", "reason"),
    [
        pytest.param(
            "line_from_two_sources",
            "z3__entry_left_7",
            LandingDealRefusal.LINE_FROM_TWO_SOURCES,
            id="line-from-two-sources",
        ),
        pytest.param(
            "two_sided",
            "sec3__entry_left_5",
            LandingDealRefusal.TWO_SIDED,
            id="two-sided",
        ),
    ],
)
def test_a_port_the_deal_cannot_order_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    port_id: str,
    reason: LandingDealRefusal,
) -> None:
    for call in _deal_calls(monkeypatch, SHAPES / f"{shape}.mmd"):
        assert call.refused[port_id] is reason
        assert port_id not in call.decided


@pytest.fixture
def fixture_call(monkeypatch: pytest.MonkeyPatch) -> Iterator[_DealCall]:
    yield _deal_calls(monkeypatch, FIXTURE)[0]


def test_lines_tied_at_the_exit_are_refused(fixture_call: _DealCall) -> None:
    offsets = dict(fixture_call.ctx.station_offsets or {})
    for line in ("l0", "l1", "l2"):
        offsets[("sec0__exit_top_0", line)] = 0.0
    ctx = replace(fixture_call.ctx, station_offsets=offsets)

    _decided, refused = bundle_landing_deals(ctx, fixture_call.targets)

    assert refused[FIXTURE_PORT] is LandingDealRefusal.EXIT_TIE


_COMPASS = {"N": (0.0, -1.0), "E": (1.0, 0.0), "S": (0.0, 1.0), "W": (-1.0, 0.0)}


def _routed(
    call: _DealCall,
    monkeypatch: pytest.MonkeyPatch,
    exit_side: PortSide,
    entry_side: PortSide,
    legs: str,
) -> tuple[cci.CorridorCohortTarget, ...]:
    """The fixture's bundle leaving *exit_side* and entering *entry_side*, with
    every member running the compass *legs* in turn."""
    ports = call.ctx.graph.ports
    monkeypatch.setattr(ports["sec0__exit_top_0"], "side", exit_side)
    monkeypatch.setattr(ports[FIXTURE_PORT], "side", entry_side)
    points = [(0.0, 0.0)]
    for leg in legs:
        dx, dy = _COMPASS[leg]
        x, y = points[-1]
        points.append((x + 10.0 * dx, y + 10.0 * dy))
    return tuple(
        replace(target, route=replace(target.route, points=list(points)))
        if target.edge_key[1] == FIXTURE_PORT
        else target
        for target in call.targets
    )


@pytest.mark.parametrize(
    ("exit_side", "entry_side", "legs"),
    [
        pytest.param(PortSide.RIGHT, PortSide.LEFT, "WSE", id="LR-departs-against"),
        pytest.param(PortSide.BOTTOM, PortSide.TOP, "NES", id="TB-departs-against"),
        pytest.param(PortSide.TOP, PortSide.LEFT, "NW", id="LR-arrives-against"),
        pytest.param(PortSide.LEFT, PortSide.TOP, "WN", id="TB-arrives-against"),
        pytest.param(PortSide.TOP, PortSide.RIGHT, "NESE", id="LR-over-the-top"),
        pytest.param(PortSide.LEFT, PortSide.BOTTOM, "WSES", id="TB-over-the-top"),
    ],
)
def test_a_source_travelling_against_its_exit_or_entry_side_is_refused(
    fixture_call: _DealCall,
    monkeypatch: pytest.MonkeyPatch,
    exit_side: PortSide,
    entry_side: PortSide,
    legs: str,
) -> None:
    """The order across travel is read at both ends from the sides' nominal
    travel, which says nothing about a bundle whose first leg runs against its
    exit side or whose last leg runs against its entry side."""
    targets = _routed(fixture_call, monkeypatch, exit_side, entry_side, legs)

    decided, refused = bundle_landing_deals(fixture_call.ctx, targets)

    assert refused[FIXTURE_PORT] is LandingDealRefusal.AGAINST_SIDE
    assert FIXTURE_PORT not in decided


@pytest.mark.parametrize(
    ("exit_side", "entry_side", "legs"),
    [
        pytest.param(PortSide.RIGHT, PortSide.LEFT, "E", id="LR"),
        pytest.param(PortSide.BOTTOM, PortSide.TOP, "S", id="TB"),
        pytest.param(PortSide.TOP, PortSide.LEFT, "NE", id="LR-turning"),
        pytest.param(PortSide.LEFT, PortSide.TOP, "WS", id="TB-turning"),
    ],
)
def test_a_source_travelling_along_its_exit_and_entry_sides_is_dealt(
    fixture_call: _DealCall,
    monkeypatch: pytest.MonkeyPatch,
    exit_side: PortSide,
    entry_side: PortSide,
    legs: str,
) -> None:
    targets = _routed(fixture_call, monkeypatch, exit_side, entry_side, legs)

    decided, refused = bundle_landing_deals(fixture_call.ctx, targets)

    assert FIXTURE_PORT not in refused
    assert FIXTURE_PORT in decided


@pytest.mark.parametrize(
    ("shape", "port_id"),
    [
        pytest.param(
            "arrives_against_entry_over_the_top",
            "sec1__entry_right_1",
            id="top-exit-over-the-top",
        ),
        pytest.param("arrives_against_entry_bt", "sec1__entry_left_1", id="BT"),
    ],
)
def test_a_bundle_crossing_its_section_to_a_far_edge_port_is_refused(
    monkeypatch: pytest.MonkeyPatch, shape: str, port_id: str
) -> None:
    """A TOP exit routed over the top onto the far edge of its destination
    section arrives against that entry side."""
    for call in _deal_calls(monkeypatch, SHAPES / f"{shape}.mmd"):
        assert call.refused[port_id] is LandingDealRefusal.AGAINST_SIDE


def test_a_bundle_arriving_against_its_entry_side_draws_as_planned() -> None:
    assert shadow_failure(SHAPES / "arrives_against_entry_over_the_top.mmd") is None


@pytest.mark.parametrize("shape", ["arrives_against_entry_bt", "interleaved_sources"])
def test_a_refused_port_renders(shape: str) -> None:
    _render(SHAPES / f"{shape}.mmd")


def _with_source_slots(
    call: _DealCall, port_id: str, interleave: bool
) -> tuple[cci.CorridorCohortTarget, ...]:
    """*port_id*'s targets with its own slots re-assigned so that one source's
    lines either straddle another source's line or sit side by side."""
    lines_by_source: dict[str, list[str]] = {}
    for edge in call.ctx.graph.edges_to(port_id):
        lines_by_source.setdefault(edge.source, []).append(edge.line_id)
    pair = next(sorted(lines) for lines in lines_by_source.values() if len(lines) > 1)
    lone = next(lines[0] for lines in lines_by_source.values() if lines[0] not in pair)
    slots = sorted(_own_slots(call, port_id).values())
    order = [pair[0], lone, pair[1]] if interleave else [pair[0], pair[1], lone]
    others = [line for line in sorted(_own_slots(call, port_id)) if line not in order]
    coordinate = dict(zip(order + others, slots, strict=True))
    return tuple(
        replace(target, endpoint_lane_coordinate=coordinate[target.edge_key[2]])
        if target.edge_key[1] == port_id
        else target
        for target in call.targets
    )


@pytest.mark.parametrize(
    ("interleave", "refusal"),
    [
        pytest.param(True, LandingDealRefusal.INTERLEAVED_SOURCES, id="interleaved"),
        pytest.param(False, None, id="contiguous"),
    ],
)
def test_a_port_whose_sources_interleave_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    interleave: bool,
    refusal: LandingDealRefusal | None,
) -> None:
    """The deal orders lines within each source; the order between sources
    is the port's own, which a source straddling another does not give."""
    port_id = "sec3__entry_right_5"
    call = _deal_calls(monkeypatch, SHAPES / "multi_source.mmd")[0]
    targets = _with_source_slots(call, port_id, interleave)

    decided, refused = bundle_landing_deals(call.ctx, targets)

    assert refused.get(port_id) is refusal
    assert (port_id in decided) is (refusal is None)


def test_a_port_fed_between_another_sources_lines_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for call in _deal_calls(monkeypatch, SHAPES / "interleaved_sources.mmd"):
        assert (
            call.refused["sec5__entry_left_6"] is LandingDealRefusal.INTERLEAVED_SOURCES
        )


def test_a_deal_moving_an_immutable_member_is_refused(
    fixture_call: _DealCall,
) -> None:
    targets = tuple(
        replace(target, mutable=False) if target.edge_key[1] == FIXTURE_PORT else target
        for target in fixture_call.targets
    )

    _decided, refused = bundle_landing_deals(fixture_call.ctx, targets)

    assert refused[FIXTURE_PORT] is LandingDealRefusal.IMMUTABLE_MOVED


def test_a_deal_that_moves_no_immutable_member_stands(
    fixture_call: _DealCall,
) -> None:
    targets = tuple(
        replace(target, mutable=False)
        if target.edge_key[1] == FIXTURE_PORT and target.edge_key[2] == "l0"
        else target
        for target in fixture_call.targets
    )

    decided, _refused = bundle_landing_deals(fixture_call.ctx, targets)

    assert landing_order(decided[FIXTURE_PORT]) == ("l0", "l1", "l2")


def _peeloff_bundle(trunk_sign: int) -> PortPeeloffBundle:
    """Lines a, b, c peel off one trunk shallowest first into a LEFT port."""
    per_line = {
        line: PeeloffTail(
            0.0, 10.0 + 4 * rank, 100.0 + 4 * rank, 200.0 + 4 * rank, trunk_sign, 1, 1
        )
        for rank, line in enumerate("abc")
    }
    return PortPeeloffBundle("port", [], per_line, trunk_sign, 1, 1)


@pytest.mark.parametrize("trunk_sign", [1, -1], ids=["trunk-leads", "half-turn"])
def test_a_decided_port_seats_rank_and_port_y_in_the_decided_order(
    trunk_sign: int,
) -> None:
    """The decided order disagrees with trunk depth; each line takes the whole
    slot, port Y and peel-X rank alike, that its decided position holds."""
    bundle = _peeloff_bundle(trunk_sign)
    by_depth = peeloff_target_slots(bundle, 4.0, {})
    depth_line_at = sorted(by_depth, key=lambda line: by_depth[line].port_y)
    decided_order = ("c", "a", "b")

    slots = peeloff_target_slots(bundle, 4.0, {}, {"port": decided_order})

    assert sorted(slots, key=lambda line: slots[line].port_y) == list(decided_order)
    for position, line in enumerate(decided_order):
        assert slots[line] == by_depth[depth_line_at[position]]


def test_an_undecided_bundle_keeps_its_trunk_depth_order() -> None:
    bundle = _peeloff_bundle(1)

    assert peeloff_target_slots(bundle, 4.0, {}, {"port": ("a", "b")}) == (
        peeloff_target_slots(bundle, 4.0, {})
    )


def test_every_consumer_after_emission_takes_the_plans_landing_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, list[Mapping[str, Sequence[str]]]] = {
        "reconcile": [],
        "invariant": [],
    }
    reconcile = core._reconcile_port_peeloff_risers
    check = invariants.check_peeloff_concentric

    def spy_reconcile(routes: Any, ctx: Any, orders: Any) -> None:
        received["reconcile"].append(orders)
        reconcile(routes, ctx, orders)

    def spy_check(graph: Any, routes: Any, radius: Any, orders: Any) -> Any:
        received["invariant"].append(orders)
        return check(graph, routes, radius, orders)

    monkeypatch.setattr(core, "_reconcile_port_peeloff_risers", spy_reconcile)
    monkeypatch.setattr(invariants, "check_peeloff_concentric", spy_check)
    _render(FIXTURE)

    for consumer, calls in received.items():
        orders = [call[FIXTURE_PORT] for call in calls if FIXTURE_PORT in call]
        assert orders, consumer
        assert set(orders) == {("l0", "l1", "l2")}, consumer


@pytest.fixture
def published(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MetroGraph, cci.AcceptedCorridorCohortPlan]:
    seen: list[tuple[MetroGraph, cci.AcceptedCorridorCohortPlan]] = []

    def spy(
        graph: MetroGraph, plan: cci.AcceptedCorridorCohortPlan | None
    ) -> dict[str, tuple[str, ...]]:
        if plan is not None:
            seen.append((graph, plan))
        return planned_landing_orders(graph, plan)

    monkeypatch.setattr(core, "planned_landing_orders", spy)
    _render(FIXTURE)
    graph, plan = seen[-1]
    assert planned_landing_orders(graph, plan)[FIXTURE_PORT] == ("l0", "l1", "l2")
    return graph, plan


def _port_landings(
    plan: cci.AcceptedCorridorCohortPlan,
) -> list[cci.AcceptedCorridorOwnerFrame]:
    return [
        frame
        for frame in plan.owners
        if frame.kind is cci.AcceptedCorridorOwnerKind.ENDPOINT_LANDING
        and frame.segment[1][1] == FIXTURE_PORT
    ]


def _with_owners(
    plan: cci.AcceptedCorridorCohortPlan,
    owners: Sequence[cci.AcceptedCorridorOwnerFrame],
) -> cci.AcceptedCorridorCohortPlan:
    translations = plan.coordinate_translations
    return cci.AcceptedCorridorCohortPlan(
        cci._accepted_plan_id(tuple(owners), translations),
        tuple(owners),
        translations,
    )


def test_a_port_the_plan_lands_in_part_is_not_decided(
    published: tuple[MetroGraph, cci.AcceptedCorridorCohortPlan],
) -> None:
    graph, plan = published
    dropped = _port_landings(plan)[0]
    partial = _with_owners(plan, [f for f in plan.owners if f is not dropped])

    assert FIXTURE_PORT not in planned_landing_orders(graph, partial)


def test_a_line_two_members_land_leaves_its_port_undecided(
    published: tuple[MetroGraph, cci.AcceptedCorridorCohortPlan],
) -> None:
    """A second member landing one of the port's lines on that line's own
    coordinate, as a line fed from two sources is landed."""
    graph, plan = published
    landing = _port_landings(plan)[0]
    member, (_source, port_id, line), rank = landing.segment
    second = replace(
        landing,
        identity=landing.identity + "|second-source",
        owner_id=member + "|second-source",
        segment=(member + "|second-source", ("other_source", port_id, line), rank),
    )

    assert FIXTURE_PORT not in planned_landing_orders(
        graph, _with_owners(plan, [*plan.owners, second])
    )


def test_without_a_plan_no_port_is_decided() -> None:
    assert planned_landing_orders(MetroGraph(), None) == {}
