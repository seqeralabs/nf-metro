"""Lock the cross-constant geometric orderings enforced at import.

``constants._check_constant_relations`` runs at module import and turns a
mis-tuned constant (one whose value is only correct relative to another)
into an immediate, located failure rather than a silent layout regression.
"""

import pytest

from nf_metro.layout import constants as c


def test_relations_hold_on_current_values():
    c._check_constant_relations()


def test_coordinate_tolerance_tiers_strictly_ordered():
    assert c.COORD_TOLERANCE_FINE < c.SAME_COORD_TOLERANCE < c.COORD_TOLERANCE


def test_same_coord_tolerance_below_offset_step():
    assert c.SAME_COORD_TOLERANCE < c.OFFSET_STEP


def test_bypass_clearance_holds_two_corner_radii():
    assert c.BYPASS_CLEARANCE >= 2 * c.CURVE_RADIUS


def test_offset_step_below_bypass_nest_step():
    assert c.OFFSET_STEP < c.BYPASS_NEST_STEP


def test_station_elbow_tolerance_at_least_offset_step():
    assert c.STATION_ELBOW_TOLERANCE >= c.OFFSET_STEP


@pytest.mark.parametrize(
    "attr, bad_value",
    [
        ("SAME_COORD_TOLERANCE", 5.0),  # exceeds OFFSET_STEP and COORD_TOLERANCE
        ("BYPASS_CLEARANCE", 1.0),  # below 2*CURVE_RADIUS
        ("OFFSET_STEP", 99.0),  # exceeds BYPASS_NEST_STEP
        ("STATION_ELBOW_TOLERANCE", 0.0),  # below OFFSET_STEP
    ],
)
def test_violation_raises(monkeypatch, attr, bad_value):
    monkeypatch.setattr(c, attr, bad_value)
    with pytest.raises(AssertionError):
        c._check_constant_relations()
