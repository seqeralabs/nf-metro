"""Lines that part at a fork take their own order there.

A junction feeder whose lines reach an entry port in an order its bundle does
not carry concentrically gets a per-departure lane-order record
(:class:`DepartureLaneOrder`): the recorded lines cross once, at the fork, and
every routing consumer relabels its natural ranks through the record.  Offsets
phase 7b' decides a record port once per positioning pass and replays the
decision in every later offsets run of that pass.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nf_metro.api import render_string
from nf_metro.layout.departure_order import (
    DepartureLaneOrder,
    DepartureRecordDriftError,
    PortOrderDecision,
    hold_departure_records,
    relabel_departures,
)
from nf_metro.layout.routing import offsets as offsets_module
from nf_metro.layout.routing.invariants import (
    BundleOrderViolation,
    CurveInvariantError,
    _outside_recorded_forks,
    check_bundle_order_preserved,
)
from nf_metro.parser.model import Edge, MetroGraph
from nf_metro.render import svg

REGRESSIONS = Path(__file__).parent / "fixtures" / "regressions"
FORK_PORT_STOP = REGRESSIONS / "departure_order_fork_port_stop.mmd"
FORK_TWO_FEEDERS = REGRESSIONS / "departure_order_fork_two_feeders.mmd"
SHARED_RUN = REGRESSIONS / "departure_order_shared_run_no_record.mmd"
FORK_PORT_STOP_TB = REGRESSIONS / "departure_order_fork_port_stop_tb.mmd"
PERPENDICULAR_SEAM = REGRESSIONS / "departure_order_perpendicular_seam.mmd"


class _Drawn:
    def __init__(self) -> None:
        self.graph: MetroGraph | None = None
        self.routes: list[Any] = []
        self.runs: list[dict[tuple[str, str], DepartureLaneOrder]] = []
        self.decided_in: list[int] = []


def _draw(
    monkeypatch: pytest.MonkeyPatch, path: Path, *, may_abort: bool = False
) -> _Drawn:
    drawn = _Drawn()
    check = svg.assert_render_curve_invariants
    run = offsets_module._run_offset_phases
    decide = offsets_module._feeder_departure_orders

    def capture(graph: MetroGraph, routes: list[Any], offsets: Any) -> None:
        drawn.graph, drawn.routes = graph, routes
        check(graph, routes, offsets)

    def spy_run(graph: MetroGraph, resolved: float) -> Any:
        ctx = run(graph, resolved)
        drawn.runs.append(dict(graph._departure_lane_orders))
        return ctx

    def spy_decide(*args: Any) -> Any:
        drawn.decided_in.append(len(drawn.runs))
        return decide(*args)

    monkeypatch.setattr(svg, "assert_render_curve_invariants", capture)
    monkeypatch.setattr(offsets_module, "_run_offset_phases", spy_run)
    monkeypatch.setattr(offsets_module, "_feeder_departure_orders", spy_decide)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            render_string(path.read_text(), source_dir=str(path.parent))
        except Exception:
            if not may_abort:
                raise
    return drawn


@pytest.mark.parametrize(
    ("path", "junction", "port", "pair"),
    [
        (FORK_PORT_STOP, "__junction_4", "sec2__entry_left_3", ("l0", "l3")),
        (FORK_TWO_FEEDERS, "__junction_4", "sec2__entry_left_2", ("l1", "l2")),
    ],
    ids=["port-stop", "two-feeders"],
)
def test_a_fork_whose_lines_part_renders_with_its_departure_record(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    junction: str,
    port: str,
    pair: tuple[str, str],
) -> None:
    """The junction sends both lines on to another section as well, so they
    part there; the port needs them in the opposite order to the one their
    bundle carries, and the departure records the swap."""
    drawn = _draw(monkeypatch, path)

    assert drawn.graph is not None
    record = drawn.graph._departure_lane_orders[(junction, port)]
    assert record.reorders(*pair)


def test_the_recorded_pair_crosses_only_on_its_departures_first_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drawn = _draw(monkeypatch, FORK_PORT_STOP)

    assert drawn.graph is not None
    flips = [
        violation
        for violation in check_bundle_order_preserved(drawn.routes)
        if (violation.edge_source, violation.edge_target)
        == ("__junction_4", "sec2__entry_left_3")
    ]
    assert flips
    assert all(violation.segment_index == 1 for violation in flips)
    assert not _outside_recorded_forks(drawn.graph, flips)


def test_a_record_port_is_decided_once_per_positioning_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every offsets run after the deciding one replays its record, so the
    record never changes within a pass and the decision is taken once."""
    drawn = _draw(monkeypatch, FORK_PORT_STOP)

    with_record = [run for run in drawn.runs if run]
    assert with_record
    assert all(run == with_record[0] for run in with_record)
    first = next(i for i, run in enumerate(drawn.runs) if run)
    assert first in drawn.decided_in
    assert len(drawn.decided_in) < len(drawn.runs) - first


def test_lines_that_leave_a_junction_together_take_no_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two lines that leave the junction only for the port travel on together:
    reordering them would be a lane swap along the run, so no record is
    written."""
    verdicts: list[bool] = []
    parts = offsets_module._parts_at_fork

    def spy(*args: Any) -> bool:
        verdicts.append(parts(*args))
        return verdicts[-1]

    monkeypatch.setattr(offsets_module, "_parts_at_fork", spy)
    drawn = _draw(monkeypatch, SHARED_RUN)

    assert False in verdicts
    assert not any(drawn.runs)


@pytest.mark.xfail(
    raises=CurveInvariantError,
    strict=True,
    reason=(
        "#1792: the TB twin of the port-stop fork aborts on main; its entry "
        "already holds the approach order, so it takes no record"
    ),
)
def test_the_tb_twin_of_a_parting_fork_renders_with_its_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drawn = _draw(monkeypatch, FORK_PORT_STOP_TB)

    assert drawn.graph is not None
    assert drawn.graph._departure_lane_orders


def _fork(*edges: tuple[str, str, str]) -> MetroGraph:
    graph = MetroGraph()
    for source, target, line in edges:
        graph.add_edge(Edge(source, target, line))
    return graph


@pytest.mark.parametrize(
    ("edges", "parts"),
    [
        ((("j", "port", "a"), ("j", "port", "b"), ("j", "other", "a")), True),
        ((("j", "port", "a"), ("j", "port", "b"), ("j", "other", "c")), False),
    ],
    ids=["one-line-leaves-elsewhere", "both-leave-only-for-the-port"],
)
def test_a_reordered_pair_must_part_at_the_fork(
    edges: tuple[tuple[str, str, str], ...], parts: bool
) -> None:
    graph = _fork(*edges)

    assert (
        offsets_module._parts_at_fork(graph, "j", "port", ["a", "b"], ["b", "a"])
        is parts
    )
    assert offsets_module._parts_at_fork(graph, "j", "port", ["a", "b"], ["a", "b"])


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


def _decisions(drawn: _Drawn) -> Iterator[PortOrderDecision]:
    assert drawn.graph is not None
    plan = drawn.graph._departure_order_plan
    assert plan is not None
    yield from (d for d in plan.decisions.values() if d is not None)


def test_the_port_stop_fork_keeps_its_order_at_the_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full carry would put a line through a skipped station's marker on a
    same-row straight, so the carry stops at the port."""
    drawn = _draw(monkeypatch, FORK_PORT_STOP)

    (decision,) = _decisions(drawn)
    assert not decision.carry_full


def test_a_carry_that_reorders_a_perpendicular_seam_takes_no_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single-feeder departure's carry would move one line across another at
    Zone 2's exit, whose connector feeds Zone 3's BOTTOM entry.  That entry's
    other feeder holds its order, so the seam would change order between the
    sections: the carry stops at the port, and the departure, whose order the
    whole section has to take, gets no record."""
    added: list[set[tuple[str, str, str, str]]] = []
    flips = offsets_module._perpendicular_seam_flips

    def spy(*args: Any) -> set[tuple[str, str, str, str]]:
        found = flips(*args)
        added.append(found)
        return found

    monkeypatch.setattr(offsets_module, "_perpendicular_seam_flips", spy)
    drawn = _draw(monkeypatch, PERPENDICULAR_SEAM, may_abort=True)

    assert any(
        (exit_id, entry_id) == ("z2__exit_right_2", "z3__entry_bottom_6")
        for found in added
        for exit_id, entry_id, _a, _b in found
    )
    assert not any(drawn.runs)


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
