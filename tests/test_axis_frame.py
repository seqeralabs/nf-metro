"""Unit tests for the ``AxisFrame`` axis-vocabulary primitive."""

from __future__ import annotations

import pytest

from nf_metro.layout.geometry import AxisFrame
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
