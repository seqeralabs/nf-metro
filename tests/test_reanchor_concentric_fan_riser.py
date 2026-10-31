"""Re-anchoring a concentric fan must not starve the other corner of a riser.

Two corners joined by one riser split its length in proportion to their
declared radii.  A riser that is exactly as long as those two radii have no
slack, so raising the fan corner's radius takes length from its partner, which
then resolves below the radius it declares.
"""

from __future__ import annotations

import pytest

from nf_metro.layout.constants import COORD_TOLERANCE_FINE, CURVE_RADIUS
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.corners import resolve_curve_radii
from nf_metro.layout.routing.normalize import _reanchor_concentric_corner_fans
from nf_metro.parser.model import Edge


def _route(
    line_id: str, points: list[tuple[float, float]], radii: list[float]
) -> RoutedPath:
    return RoutedPath(
        edge=Edge(f"source_{line_id}", f"target_{line_id}", line_id),
        line_id=line_id,
        points=points,
        is_inter_section=True,
        curve_radii=radii,
    )


# Each case pairs a 20px riser (corners 14 + 6) with an outer lane nesting
# 4px wider around the 6px corner, so the fan's innermost lane sits below
# CURVE_RADIUS and the re-anchor tries to raise it.
_CASES = {
    "fan_at_lower_corner": (
        ([(0.0, 0.0), (100.0, 0.0), (100.0, 20.0), (300.0, 20.0)], [14.0, 6.0], 1),
        ([(0.0, -4.0), (96.0, -4.0), (96.0, 24.0), (300.0, 24.0)], [14.0, 10.0]),
    ),
    "fan_at_upper_corner": (
        ([(0.0, 0.0), (100.0, 0.0), (100.0, 20.0), (300.0, 20.0)], [6.0, 14.0], 0),
        ([(0.0, -4.0), (104.0, -4.0), (104.0, 24.0), (300.0, 24.0)], [10.0, 14.0]),
    ),
}


@pytest.mark.parametrize("case", sorted(_CASES))
def test_reanchor_keeps_zero_slack_riser_partner_at_its_declared_radius(
    case: str,
) -> None:
    (inner_points, inner_radii, fan_index), (outer_points, outer_radii) = _CASES[case]
    inner = _route("inner", inner_points, inner_radii)
    outer = _route("outer", outer_points, outer_radii)
    partner_index = 1 - fan_index

    _reanchor_concentric_corner_fans([inner, outer], {}, CURVE_RADIUS)

    assert inner.curve_radii is not None
    assert inner.curve_radii[fan_index] > inner_radii[fan_index]
    for route in (inner, outer):
        assert route.curve_radii is not None
        resolved = resolve_curve_radii(route.points, route.curve_radii)
        for declared, drawn in zip(route.curve_radii, resolved):
            assert drawn >= declared - COORD_TOLERANCE_FINE, (
                f"{case}: {route.line_id} corner declares {declared:.4f} "
                f"but resolves {drawn:.4f}"
            )
    assert inner.curve_radii[partner_index] == inner_radii[partner_index]
