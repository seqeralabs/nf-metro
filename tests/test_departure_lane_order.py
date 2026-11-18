"""The departure lane-order record (:class:`DepartureLaneOrder`) and its readers.

A record states the order a junction feeder's lines leave in where that order
parts from the bundle's own; its readers relabel each line's natural rank
through it, and only a recorded swap on a departure's first segment is exempt
from the bundle-order check.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nf_metro.layout.departure_order import (
    JUNCTIONS_POSITIONED,
    DepartureLaneOrder,
    DepartureOrderPlan,
    DepartureRecordDriftError,
    hold_departure_records,
    relabel_departures,
)
from nf_metro.layout.routing.common import (
    PortPeeloffBundle,
    RoutedPath,
    peeloff_target_slots,
    port_peeloff_tail,
)
from nf_metro.layout.routing.invariants import (
    BundleOrderViolation,
    _outside_recorded_forks,
)
from nf_metro.parser.model import Edge, MetroGraph


@pytest.mark.parametrize(
    "takes_lane_of",
    [(("a", "a"), ("b", "a")), (("a", "b"), ("b", "c"))],
    ids=["line-twice", "lines-differ-from-natural"],
)
def test_a_departure_order_must_permute_its_lines(
    takes_lane_of: tuple[tuple[str, str], ...],
) -> None:
    with pytest.raises(ValueError, match="not a permutation"):
        DepartureLaneOrder("j", "port", takes_lane_of)


def _record(*pairs: tuple[str, str]) -> DepartureLaneOrder:
    return DepartureLaneOrder("j", "port", pairs)


def test_a_record_relabels_each_line_from_the_line_whose_lane_it_takes() -> None:
    values = {
        ("j", "port", "a"): (0, 3),
        ("j", "port", "b"): (1, 3),
        ("j", "port", "c"): (2, 3),
        ("j", "other", "a"): (0, 1),
    }
    records = {("j", "port"): _record(("b", "a"), ("a", "b"), ("c", "c"))}

    relabel_departures(records, values, lambda _own, natural: natural)

    assert values == {
        ("j", "port", "a"): (1, 3),
        ("j", "port", "b"): (0, 3),
        ("j", "port", "c"): (2, 3),
        ("j", "other", "a"): (0, 1),
    }


def test_a_record_reorders_only_the_pairs_it_swaps() -> None:
    record = _record(("b", "a"), ("a", "b"), ("c", "c"))

    assert record.reorders("a", "b")
    assert record.reorders("b", "a")
    assert not record.reorders("a", "c")
    assert not record.reorders("a", "z")


def _flip(source: str, target: str, a: str, b: str, index: int) -> Any:
    return SimpleNamespace(
        edge_source=source,
        edge_target=target,
        line_a=a,
        line_b=b,
        segment_index=index,
    )


def test_only_a_recorded_swap_on_the_first_segment_is_exempt() -> None:
    graph = MetroGraph()
    graph._departure_lane_orders = {("j", "port"): _record(("b", "a"), ("a", "b"))}
    exempt = _flip("j", "port", "a", "b", 1)
    later = _flip("j", "port", "a", "b", 3)
    elsewhere = _flip("j", "other", "a", "b", 1)
    violations: list[BundleOrderViolation] = [exempt, later, elsewhere]

    assert _outside_recorded_forks(graph, violations) == [later, elsewhere]


def test_a_pass_that_ends_with_other_records_than_the_first_fails_closed() -> None:
    graph = MetroGraph()
    record = _record(("b", "a"), ("a", "b"))
    graph._departure_lane_orders = {record.key: record}
    hold_departure_records(graph)
    hold_departure_records(graph)

    graph._departure_lane_orders = {}
    with pytest.raises(DepartureRecordDriftError) as raised:
        hold_departure_records(graph)

    assert raised.value.changed == (("j", "port"),)


def test_a_departure_plan_decides_once_and_replays_after_junctions_are_placed() -> None:
    plan = DepartureOrderPlan()
    before, after = ["1.1"], ["1.1", JUNCTIONS_POSITIONED]

    assert not plan.deciding(before) and not plan.replaying(before)
    assert plan.deciding(after) and plan.replaying(after)
    plan.seal(before)
    assert plan.deciding(after)
    plan.seal(after)
    assert not plan.deciding(after)
    assert plan.replaying(after) and not plan.replaying(before)


def _peeloff_bundle(*routes: RoutedPath) -> PortPeeloffBundle:
    entries = []
    for route in routes:
        tail = port_peeloff_tail(route)
        assert tail is not None
        entries.append((route, tail))
    per_line = {route.line_id: tail for route, tail in entries}
    first = entries[0][1]
    return PortPeeloffBundle(
        "port",
        entries,
        per_line,
        first.trunk_sign,
        first.vertical_sign,
        first.port_lead_sign,
    )


def _riser(source: str, line: str, *points: tuple[float, float]) -> RoutedPath:
    return RoutedPath(Edge(source, "port", line), line, list(points))


def test_a_recorded_departure_takes_the_peeloff_slot_of_the_lane_it_takes() -> None:
    """Both tails leave the junction on their trunk, so the lines change sides
    at the fork itself: each riser and port slot belongs to the line whose lane
    the record gives it, not to its own trunk depth."""
    bundle = _peeloff_bundle(
        _riser("j", "a", (0.0, 100.0), (50.0, 100.0), (50.0, 20.0), (80.0, 20.0)),
        _riser("j", "b", (0.0, 104.0), (54.0, 104.0), (54.0, 24.0), (80.0, 24.0)),
    )
    swap = {("j", "port"): _record(("b", "a"), ("a", "b"))}

    natural = peeloff_target_slots(bundle, 4.0, {})
    recorded = peeloff_target_slots(bundle, 4.0, swap)

    assert (natural["a"].peel_x, natural["b"].peel_x) == (50.0, 54.0)
    assert recorded == {"a": natural["b"], "b": natural["a"]}


def test_a_departure_whose_tail_starts_beyond_its_junction_keeps_its_slots() -> None:
    bundle = _peeloff_bundle(
        _riser(
            "j",
            "a",
            (0.0, 60.0),
            (0.0, 100.0),
            (50.0, 100.0),
            (50.0, 20.0),
            (80.0, 20.0),
        ),
        _riser(
            "j",
            "b",
            (4.0, 60.0),
            (4.0, 104.0),
            (54.0, 104.0),
            (54.0, 24.0),
            (80.0, 24.0),
        ),
    )
    swap = {("j", "port"): _record(("b", "a"), ("a", "b"))}

    assert peeloff_target_slots(bundle, 4.0, swap) == peeloff_target_slots(
        bundle, 4.0, {}
    )
