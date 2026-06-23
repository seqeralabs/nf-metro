"""Unit tests for the ``AxisFrame`` axis-vocabulary primitive."""

from __future__ import annotations

import pytest

from nf_metro.layout.geometry import (
    AxisFrame,
    lane_delta,
    lane_delta_to_normal_offset,
    lanes_run_along_y,
    station_lane_coord,
)
from nf_metro.parser.model import Station

X_SPACING = 60.0
Y_SPACING = 40.0


def _station() -> Station:
    return Station(id="s", label="S")


def test_lr_primary_is_x_secondary_is_y() -> None:
    frame = AxisFrame.for_direction("LR", X_SPACING, Y_SPACING)
    assert frame.primary.name == "x"
    assert frame.secondary.name == "y"
    assert frame.primary.step == X_SPACING
    assert frame.secondary.step == Y_SPACING
    assert frame.primary_sign == 1.0


def test_tb_transposes_axes() -> None:
    frame = AxisFrame.for_direction("TB", X_SPACING, Y_SPACING)
    assert frame.primary.name == "y"
    assert frame.secondary.name == "x"
    assert frame.primary.step == Y_SPACING
    assert frame.secondary.step == X_SPACING
    assert frame.primary_sign == 1.0


def test_rl_shares_lr_axes_but_reverses_primary_sign() -> None:
    frame = AxisFrame.for_direction("RL", X_SPACING, Y_SPACING)
    assert frame.primary.name == "x"
    assert frame.secondary.name == "y"
    assert frame.primary_sign == -1.0


@pytest.mark.parametrize(
    "direction, expected",
    [("LR", ("x", "y")), ("RL", ("x", "y")), ("TB", ("y", "x")), ("BT", ("y", "x"))],
)
def test_axes_for_direction_names_vertical_and_horizontal_flow(
    direction: str, expected: tuple[str, str]
) -> None:
    assert AxisFrame.axes_for_direction(direction) == expected


@pytest.mark.parametrize("direction", ["LR", "RL", "TB", "BT"])
def test_for_direction_matches_axes_for_direction(direction: str) -> None:
    frame = AxisFrame.for_direction(direction, X_SPACING, Y_SPACING)
    assert (frame.primary.name, frame.secondary.name) == AxisFrame.axes_for_direction(
        direction
    )


@pytest.mark.parametrize(
    "direction, on_y",
    [("LR", True), ("RL", True), ("TB", False), ("BT", False)],
)
def test_lanes_run_along_y_tracks_secondary_axis(direction: str, on_y: bool) -> None:
    # A section's lines stack on its secondary axis; the row passes only own
    # the Y (lane) axis, so they include exactly the lanes-on-Y directions.
    assert lanes_run_along_y(direction) is on_y


@pytest.mark.parametrize(
    "direction, expected",
    [("LR", 1.0), ("RL", 1.0), ("TB", -1.0), ("BT", 1.0)],
)
def test_secondary_sign_fans_tb_lanes_opposite_lr(
    direction: str, expected: float
) -> None:
    # A 90-degree-CW rotation maps LR's +Y lane to -X, so TB alone fans to -X;
    # RL reverses only the primary, and BT keeps the inert +1 reserved for
    # true BT support.
    assert AxisFrame.for_direction(direction, X_SPACING, Y_SPACING).secondary_sign == (
        expected
    )


@pytest.mark.parametrize("offset", [0.0, 4.0, 12.5])
@pytest.mark.parametrize("direction", ["LR", "RL", "TB", "BT"])
def test_station_lane_coord_applies_sign_at_draw_accessor(
    direction: str, offset: float
) -> None:
    frame = AxisFrame.for_direction(direction, X_SPACING, Y_SPACING)
    station = _station()
    station.x = 100.0
    station.y = 200.0

    coord = station_lane_coord(frame, station, offset)

    base = frame.secondary.get(station)
    assert coord == base + frame.secondary_sign * offset
    # A positive offset moves a TB lane to a smaller X (screen-left), the
    # rotation image of LR moving it to a larger Y.
    if direction == "TB":
        assert coord == station.x - offset
    elif frame.secondary.name == "y":
        assert coord == station.y + offset


@pytest.mark.parametrize("offset", [4.0, 12.5])
@pytest.mark.parametrize("direction, travel", [("LR", (1.0, 0.0)), ("TB", (0.0, 1.0))])
def test_lane_delta_round_trips_to_positive_builder_offset(
    direction: str, travel: tuple[float, float], offset: float
) -> None:
    # The bundle builder fans along the right-normal of travel and expects
    # positive offsets.  LR and TB are the same forward flow rotated 90 degrees,
    # so a positive lane offset maps back to the same positive builder offset for
    # both -- TB's -1 lane sign and its rotated travel cancel.  This is why the
    # stored offset never negates.
    frame = AxisFrame.for_direction(direction, X_SPACING, Y_SPACING)
    delta = lane_delta(frame, offset)
    assert delta == frame.secondary_sign * offset

    normal_offset = lane_delta_to_normal_offset(delta, travel)
    assert normal_offset == pytest.approx(offset)


def test_lane_delta_to_normal_offset_is_sign_lookup_for_axis_aligned_travel() -> None:
    # For axis-aligned travel the projection collapses to a +/-1 sign lookup,
    # invariant to the leg's length.
    assert lane_delta_to_normal_offset(5.0, (3.0, 0.0)) == pytest.approx(5.0)
    assert lane_delta_to_normal_offset(5.0, (0.0, 7.0)) == pytest.approx(-5.0)


@pytest.mark.parametrize("direction", ["LR", "RL", "TB"])
def test_accessors_read_and_write_named_coordinate(direction: str) -> None:
    frame = AxisFrame.for_direction(direction, X_SPACING, Y_SPACING)
    station = _station()
    station.x = 11.0
    station.y = 22.0

    frame.primary.set(station, 5.0)
    frame.secondary.set(station, 7.0)

    assert frame.primary.get(station) == 5.0
    assert frame.secondary.get(station) == 7.0
    assert {frame.primary.name, frame.secondary.name} == {"x", "y"}
    assert getattr(station, frame.primary.name) == 5.0
    assert getattr(station, frame.secondary.name) == 7.0
