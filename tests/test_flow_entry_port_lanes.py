"""Lines crossing a vertical section's flow-side entry stay on their lanes.

A BOTTOM entry into a BT section (or a TOP entry into a TB one) hands its lines
straight up (or down) the lanes of the station the drop reaches.  Where that
station also carries a line born inside the section, the port's lanes leave that
line's slot open, so every arriving line meets its internal lane at the box edge
instead of stepping sideways onto it.
"""

from __future__ import annotations

import dataclasses
import warnings
from pathlib import Path

import pytest

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.layout.routing import offsets as offsets_module
from nf_metro.parser.model import PortSide
from nf_metro.themes import resolve_theme

ROOT = Path(__file__).resolve().parent.parent
RISE = (
    ROOT
    / "tests"
    / "fixtures"
    / "curve_invariant_repros"
    / "bt_bottom_entry_rise_from_junction.mmd"
)

CASES = [(RISE, "z1__entry_bottom_2")]


@pytest.mark.parametrize(
    ("path", "port_id"), CASES, ids=lambda v: getattr(v, "stem", v)
)
def test_arriving_lines_meet_their_internal_lanes(path: Path, port_id: str) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        plan = render_graph_result(
            graph, resolve_theme(None, graph, mode=None), RenderConfig()
        ).plan
    arrivals: dict[str, float] = {}
    departures: dict[str, float] = {}
    for route, points in zip(plan.routes, plan.route_polylines, strict=True):
        if route.edge.target == port_id:
            arrivals[route.line_id] = points[-1][0]
        elif route.edge.source == port_id:
            departures[route.line_id] = points[0][0]
    assert set(arrivals) == set(departures) == set(graph.station_lines(port_id))
    steps = {
        line_id: (x, departures[line_id])
        for line_id, x in arrivals.items()
        if abs(x - departures[line_id]) > 0.5
    }
    assert not steps, steps


PORT_ID = "z1__entry_bottom_2"
CONSUMER_ID = "q1x0"
STEP = offsets_module.OFFSET_STEP


@pytest.fixture(scope="module")
def settled():
    """The rise-from-junction graph with its final per-station offsets."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(RISE.read_text(), source_dir=str(RISE.parent))
        plan = render_graph_result(
            graph, resolve_theme(None, graph, mode=None), RenderConfig()
        ).plan
    return graph, dict(plan.station_offsets)


def _port_gap_is_consumer_lane(graph, offsets, lines=None) -> bool:
    ctx = offsets_module._OffsetCtx(graph=graph, offsets=offsets, offset_step=STEP)
    lines = graph.station_lines(PORT_ID) if lines is None else lines
    return offsets_module._port_gap_is_consumer_lane(ctx, PORT_ID, list(lines))


def test_port_gap_is_the_consumers_lane_when_lanes_match_and_close_up(settled) -> None:
    graph, offsets = settled
    assert _port_gap_is_consumer_lane(graph, offsets)


def test_a_line_off_the_consumers_lane_is_not_a_consumer_lane(settled) -> None:
    graph, offsets = settled
    tolerance = offsets_module._OFFSET_EQ_TOLERANCE
    inside = {**offsets, (PORT_ID, "m0"): offsets[(PORT_ID, "m0")] + tolerance / 2}
    outside = {**offsets, (PORT_ID, "m0"): offsets[(PORT_ID, "m0")] + tolerance * 2}
    assert _port_gap_is_consumer_lane(graph, inside)
    assert not _port_gap_is_consumer_lane(graph, outside)


def test_a_consumer_with_an_open_lane_of_its_own_is_not_a_consumer_lane(
    settled,
) -> None:
    graph, offsets = settled
    gapped = {**offsets, (CONSUMER_ID, "m1"): offsets[(CONSUMER_ID, "m1")] + 2 * STEP}
    assert not _port_gap_is_consumer_lane(graph, gapped)


def test_a_port_without_one_consumer_has_no_consumer_lane(settled) -> None:
    graph, offsets = settled
    assert not _port_gap_is_consumer_lane(graph, offsets, lines=["m1"])


def test_a_horizontal_section_compacts_its_port(settled, monkeypatch) -> None:
    graph, offsets = settled
    section_id = graph.ports[PORT_ID].section_id
    section = dataclasses.replace(graph.sections[section_id], direction="LR")
    monkeypatch.setitem(graph.sections, section_id, section)
    assert not _port_gap_is_consumer_lane(graph, offsets)


def test_only_the_flow_side_entry_is_a_consumer_lane(settled, monkeypatch) -> None:
    graph, offsets = settled
    port = dataclasses.replace(graph.ports[PORT_ID], side=PortSide.LEFT)
    monkeypatch.setitem(graph.ports, PORT_ID, port)
    assert not _port_gap_is_consumer_lane(graph, offsets)
