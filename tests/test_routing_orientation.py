import pytest

from nf_metro.layout.route_plan import DemandAxis, TurnHandedness
from nf_metro.layout.routing.common import Direction, right_normal_axis_sign
from nf_metro.layout.routing.orientation import (
    direction_axis,
    direction_vector,
    get_point_coordinate,
    lateral_axis,
    turn_handedness,
)


@pytest.mark.parametrize(
    ("direction", "travel_axis", "cross_axis"),
    [
        (Direction.R, DemandAxis.X, DemandAxis.Y),
        (Direction.L, DemandAxis.X, DemandAxis.Y),
        (Direction.U, DemandAxis.Y, DemandAxis.X),
        (Direction.D, DemandAxis.Y, DemandAxis.X),
    ],
)
def test_cardinal_axes(direction, travel_axis, cross_axis):
    assert direction_axis(direction) is travel_axis
    assert lateral_axis(direction) is cross_axis


@pytest.mark.parametrize(
    ("direction", "vector", "order_sign"),
    [
        (Direction.R, (1, 0), 1),
        (Direction.L, (-1, 0), -1),
        (Direction.U, (0, -1), 1),
        (Direction.D, (0, 1), -1),
    ],
)
def test_cardinal_vectors_and_lateral_order(direction, vector, order_sign):
    assert direction_vector(direction) == vector
    assert right_normal_axis_sign(direction) == order_sign


@pytest.mark.parametrize(
    ("run", "turn", "handedness"),
    [
        (Direction.R, Direction.D, TurnHandedness.CLOCKWISE),
        (Direction.D, Direction.L, TurnHandedness.CLOCKWISE),
        (Direction.L, Direction.U, TurnHandedness.CLOCKWISE),
        (Direction.U, Direction.R, TurnHandedness.CLOCKWISE),
        (Direction.R, Direction.U, TurnHandedness.COUNTERCLOCKWISE),
        (Direction.U, Direction.L, TurnHandedness.COUNTERCLOCKWISE),
        (Direction.L, Direction.D, TurnHandedness.COUNTERCLOCKWISE),
        (Direction.D, Direction.R, TurnHandedness.COUNTERCLOCKWISE),
    ],
)
def test_turn_handedness_for_horizontal_and_vertical_runs(run, turn, handedness):
    assert turn_handedness(run, turn) is handedness


@pytest.mark.parametrize(
    ("run", "turn"),
    [
        (Direction.R, Direction.R),
        (Direction.R, Direction.L),
        (Direction.D, Direction.D),
        (Direction.D, Direction.U),
    ],
)
def test_turn_handedness_rejects_non_turns(run, turn):
    with pytest.raises(ValueError, match="must be perpendicular"):
        turn_handedness(run, turn)


def test_point_coordinate_helpers_read_the_selected_axis():
    point = (12.5, 23.5)

    assert get_point_coordinate(point, DemandAxis.X) == 12.5
    assert get_point_coordinate(point, DemandAxis.Y) == 23.5


def test_point_coordinate_helpers_reject_both_axes():
    with pytest.raises(ValueError, match="requires the X or Y axis"):
        get_point_coordinate((1.0, 2.0), DemandAxis.BOTH)
