"""Upright label/icon spacing is reserved per the section's AxisFrame.

Labels and file icons never rotate, but a rotated section changes which axis
must reserve room around them.  A vertical-flow section (TB *and* BT) stacks its
lines along X and places labels beside the pill, so it reserves lane-axis (X)
extent; a horizontal-flow section reserves Y.  These tests pin that the
reservation is driven by the frame, so BT behaves identically to TB rather than
falling through a bare ``direction == "TB"`` branch.
"""

from __future__ import annotations

import pytest

from nf_metro.layout.phases.single_section import (
    _adjust_tb_labels,
    _terminus_y_overhang,
)
from nf_metro.parser.model import MetroGraph, Section, Station


def _vertical_section(direction: str) -> tuple[MetroGraph, Section]:
    """A minimal vertical-flow section with two labelled stations on one column."""
    graph = MetroGraph()
    section = Section(id="sec", name="Sec", direction=direction)
    section.bbox_x = 0.0
    section.bbox_y = 0.0
    section.bbox_w = 60.0
    section.bbox_h = 200.0
    graph.sections["sec"] = section
    for i, label in enumerate(("AlphaStationName", "BetaStationName")):
        station = Station(id=f"s{i}", label=label, section_id="sec")
        station.x = 30.0
        station.y = 40.0 + i * 40.0
        graph.add_station(station)
        section.station_ids.append(station.id)
    return graph, section


@pytest.mark.parametrize("direction", ["TB", "BT"])
def test_vertical_flow_reserves_lane_axis_for_side_labels(direction: str) -> None:
    graph, section = _vertical_section(direction)
    width_before = section.bbox_w
    x_before = [s.x for s in graph.stations.values()]

    _adjust_tb_labels(graph, section)

    assert section.bbox_w > width_before, (
        f"{direction} section reserved no X extent for its side-placed labels"
    )
    assert [s.x for s in graph.stations.values()] != x_before, (
        f"{direction} section did not shift stations to clear the label extent"
    )


def test_tb_and_bt_reserve_identical_label_extent() -> None:
    tb_graph, tb_section = _vertical_section("TB")
    bt_graph, bt_section = _vertical_section("BT")

    _adjust_tb_labels(tb_graph, tb_section)
    _adjust_tb_labels(bt_graph, bt_section)

    assert tb_section.bbox_w == bt_section.bbox_w
    assert [s.x for s in tb_graph.stations.values()] == [
        s.x for s in bt_graph.stations.values()
    ]


@pytest.mark.parametrize("direction", ["TB", "BT"])
def test_vertical_flow_terminus_icons_overhang_on_flow_axis(direction: str) -> None:
    graph = MetroGraph()
    section = Section(id="sec", name="Sec", direction=direction)
    graph.sections["sec"] = section
    sink = Station(id="t", label="Out", section_id="sec")
    sink.terminus_labels = ["bam"]
    graph.add_station(sink)
    section.station_ids.append(sink.id)

    above, below = _terminus_y_overhang(sink, direction, graph)
    assert (above, below) != (0.0, 0.0), (
        f"{direction} terminus reserved no flow-axis (Y) overhang for its icons"
    )
