"""Tests for the concentric corner geometry helpers.

These tests directly verify the invariants that are easy to accidentally
break when modifying routing logic:

1. Radii are always ``base_radius + k * offset_step`` (never variable).
2. The outermost line at every corner gets the largest radius.
3. Bundle ordering is preserved through L-shapes (no crossings).
4. ``vertical=Direction.D`` and ``vertical=Direction.U`` are mirror-symmetric.
"""

from __future__ import annotations

import pytest

from nf_metro.layout.constants import CURVE_RADIUS, OFFSET_STEP
from nf_metro.layout.routing.common import Direction
from nf_metro.layout.routing.corners import (
    bypass_stagger,
    concentric_corner_radius,
    corner_radius,
    l_shape_radii,
    l_shape_stagger,
    reference_anchored_radius,
    resolve_curve_radii,
    reversed_offset,
)

# Unit travel vectors for the four cardinal directions (screen coords, +y down).
RIGHT = (1.0, 0.0)
LEFT = (-1.0, 0.0)
DOWN = (0.0, 1.0)
UP = (0.0, -1.0)

# The eight axis-aligned 90-degree turns, as (turn_in, turn_out).
ALL_TURNS = [
    (RIGHT, DOWN),
    (RIGHT, UP),
    (LEFT, DOWN),
    (LEFT, UP),
    (DOWN, RIGHT),
    (DOWN, LEFT),
    (UP, RIGHT),
    (UP, LEFT),
]

# ---------------------------------------------------------------------------
# reversed_offset
# ---------------------------------------------------------------------------


class TestReversedOffset:
    def test_zero_becomes_max(self):
        assert reversed_offset(0.0, 6.0) == 6.0

    def test_max_becomes_zero(self):
        assert reversed_offset(6.0, 6.0) == 0.0

    def test_middle_stays(self):
        assert reversed_offset(3.0, 6.0) == 3.0

    def test_involutory(self):
        """Reversing twice gives back the original offset."""
        for off in [0.0, 1.5, 3.0, 4.5, 6.0]:
            assert reversed_offset(reversed_offset(off, 6.0), 6.0) == pytest.approx(off)

    def test_zero_bundle(self):
        """Single line: offset and max are both 0."""
        assert reversed_offset(0.0, 0.0) == 0.0


# ---------------------------------------------------------------------------
# corner_radius
# ---------------------------------------------------------------------------


class TestCornerRadius:
    """Test the unified corner radius primitive."""

    def test_outside_uses_raw_offset(self):
        assert corner_radius(3.0, 6.0, outside=True) == CURVE_RADIUS + 3.0

    def test_inside_uses_reversed_offset(self):
        assert corner_radius(3.0, 6.0, outside=False) == CURVE_RADIUS + 3.0
        # Middle offset reverses to itself

    def test_inside_endpoints(self):
        # offset=0 (inner edge) reversed to max -> largest radius
        assert corner_radius(0.0, 6.0, outside=False) == CURVE_RADIUS + 6.0
        # offset=max reversed to 0 -> base radius
        assert corner_radius(6.0, 6.0, outside=False) == CURVE_RADIUS

    def test_outside_endpoints(self):
        assert corner_radius(0.0, 6.0, outside=True) == CURVE_RADIUS
        assert corner_radius(6.0, 6.0, outside=True) == CURVE_RADIUS + 6.0

    def test_single_line(self):
        assert corner_radius(0.0, 0.0, outside=True) == CURVE_RADIUS
        assert corner_radius(0.0, 0.0, outside=False) == CURVE_RADIUS

    def test_custom_base(self):
        assert corner_radius(3.0, 6.0, outside=True, base_radius=5.0) == 8.0

    @pytest.mark.parametrize("vertical", [Direction.D, Direction.U])
    @pytest.mark.parametrize("n", [1, 2, 3, 5])
    def test_matches_l_shape_radii(self, n: int, vertical: Direction):
        """corner_radius must produce the same values as l_shape_radii."""
        for i in range(n):
            _, r1, r2 = l_shape_radii(i, n, vertical)
            off = (n - 1 - i) * OFFSET_STEP
            max_off = (n - 1) * OFFSET_STEP
            if vertical is Direction.D:
                assert corner_radius(off, max_off, outside=True) == pytest.approx(r1)
                assert corner_radius(off, max_off, outside=False) == pytest.approx(r2)
            else:
                assert corner_radius(off, max_off, outside=False) == pytest.approx(r1)
                assert corner_radius(off, max_off, outside=True) == pytest.approx(r2)


class TestReferenceAnchoredRadius:
    """Reference-anchored concentric radius for the TOP-entry staircase.

    Unlike ``corner_radius`` (innermost line at base, radii always >= base),
    this anchors a *reference* line at base and offsets every other line by its
    signed perpendicular displacement, so inside-of-turn lines fall below base.
    """

    def test_reference_line_is_base(self):
        assert reference_anchored_radius(0.0) == CURVE_RADIUS

    def test_outside_adds_offset(self):
        assert reference_anchored_radius(3.0) == CURVE_RADIUS + 3.0

    def test_inside_goes_below_base(self):
        assert reference_anchored_radius(-3.0) == CURVE_RADIUS - 3.0

    def test_custom_base(self):
        assert reference_anchored_radius(-2.0, base_radius=5.0) == 3.0

    def test_min_radius_floors_a_tight_jog(self):
        # base - offset would be negative; the floor keeps it renderable.
        assert reference_anchored_radius(-12.0, base_radius=10.0, min_radius=0.1) == 0.1

    def test_min_radius_inactive_when_above_floor(self):
        assert reference_anchored_radius(2.0, base_radius=10.0, min_radius=0.1) == 12.0

    def test_concentricity_invariant(self):
        # radius - signed_offset == base for every line: arcs share a centre.
        base = CURVE_RADIUS
        for so in (-6.0, -3.0, 0.0, 3.0, 6.0):
            assert reference_anchored_radius(so, base) - so == pytest.approx(base)

    def test_matches_offset_bundle_arithmetic(self):
        # The four #484 offset-bundle radii (East lead, lead_sign=+1).
        base = CURVE_RADIUS
        for offset in (0.0, OFFSET_STEP, 2 * OFFSET_STEP):
            assert reference_anchored_radius(-1.0 * offset, base) == base - offset
            assert reference_anchored_radius(offset, base) == base + offset
            assert reference_anchored_radius(-offset, base) == base - offset


# ---------------------------------------------------------------------------
# l_shape_radii: invariant tests
# ---------------------------------------------------------------------------


class TestLShapeRadii:
    """Test the standard inter-section L-shape (horiz -> vert -> horiz)."""

    @pytest.mark.parametrize("vertical", [Direction.D, Direction.U])
    @pytest.mark.parametrize("n", [1, 2, 3, 5])
    def test_radii_are_concentric(self, n: int, vertical: Direction):
        """All radii must be base_radius + k * offset_step for integer k."""
        for i in range(n):
            _delta, r1, r2 = l_shape_radii(i, n, vertical)
            # Check r1 is an exact multiple of offset_step above base
            k1 = (r1 - CURVE_RADIUS) / OFFSET_STEP
            assert k1 == pytest.approx(round(k1)), (
                f"r_first={r1} is not base + k*step for i={i}, n={n}, v={vertical}"
            )
            k2 = (r2 - CURVE_RADIUS) / OFFSET_STEP
            assert k2 == pytest.approx(round(k2)), (
                f"r_second={r2} not base+k*step i={i} n={n} v={vertical}"
            )

    @pytest.mark.parametrize("vertical", [Direction.D, Direction.U])
    @pytest.mark.parametrize("n", [2, 3, 5])
    def test_radii_cover_full_range(self, n: int, vertical: Direction):
        """The set of radii for a bundle must span [base, base + (n-1)*step]."""
        r1s = []
        r2s = []
        for i in range(n):
            _, r1, r2 = l_shape_radii(i, n, vertical)
            r1s.append(r1)
            r2s.append(r2)
        expected_min = CURVE_RADIUS
        expected_max = CURVE_RADIUS + (n - 1) * OFFSET_STEP
        assert min(r1s) == pytest.approx(expected_min)
        assert max(r1s) == pytest.approx(expected_max)
        assert min(r2s) == pytest.approx(expected_min)
        assert max(r2s) == pytest.approx(expected_max)

    @pytest.mark.parametrize("vertical", [Direction.D, Direction.U])
    @pytest.mark.parametrize("n", [2, 3, 5])
    def test_all_radii_distinct(self, n: int, vertical: Direction):
        """Each line in the bundle must get a distinct radius at each corner."""
        r1s = set()
        r2s = set()
        for i in range(n):
            _, r1, r2 = l_shape_radii(i, n, vertical)
            r1s.add(round(r1, 6))
            r2s.add(round(r2, 6))
        assert len(r1s) == n
        assert len(r2s) == n

    @pytest.mark.parametrize("vertical", [Direction.D, Direction.U])
    @pytest.mark.parametrize("n", [2, 3, 5])
    def test_r_first_and_r_second_are_complementary(self, n: int, vertical: Direction):
        """For each line, r_first + r_second must equal 2*base + (n-1)*step.

        This ensures that the line on the outside of corner 1 is on the
        inside of corner 2 (and vice versa), which prevents crossings.
        """
        expected_sum = 2 * CURVE_RADIUS + (n - 1) * OFFSET_STEP
        for i in range(n):
            _, r1, r2 = l_shape_radii(i, n, vertical)
            assert r1 + r2 == pytest.approx(expected_sum), (
                f"r1+r2={r1 + r2} != {expected_sum} for i={i}, n={n}, v={vertical}"
            )

    @pytest.mark.parametrize("n", [2, 3, 5])
    def test_mirror_symmetry(self, n: int):
        """Direction.D and Direction.U must produce mirror-symmetric results.

        Direction.D line i=0 is rightmost; Direction.U line n-1 is also
        rightmost.  So Direction.D[i] must match Direction.U[n-1-i] with
        the same delta, same r_first, and same r_second - they occupy
        the same spatial position in the vertical channel.
        """
        for i in range(n):
            d_down, r1_down, r2_down = l_shape_radii(i, n, vertical=Direction.D)
            d_up, r1_up, r2_up = l_shape_radii(n - 1 - i, n, vertical=Direction.U)
            assert d_down == pytest.approx(d_up), (
                f"delta mismatch: down[{i}]={d_down}, up[{n - 1 - i}]={d_up}"
            )
            assert r1_down == pytest.approx(r1_up)
            assert r2_down == pytest.approx(r2_up)

    @pytest.mark.parametrize("vertical", [Direction.D, Direction.U])
    @pytest.mark.parametrize("n", [2, 3, 5])
    def test_no_crossing_in_vertical_channel(self, n: int, vertical: Direction):
        """Lines must not cross in the vertical channel.

        The delta offsets must be strictly monotonic (either all
        increasing or all decreasing with i).
        """
        deltas = [l_shape_radii(i, n, vertical)[0] for i in range(n)]
        diffs = [deltas[j + 1] - deltas[j] for j in range(n - 1)]
        # All diffs must have the same sign (strictly monotonic)
        assert all(d > 0 for d in diffs) or all(d < 0 for d in diffs), (
            f"Deltas not monotonic: {deltas} for n={n}, v={vertical}"
        )

    @pytest.mark.parametrize("vertical", [Direction.D, Direction.U])
    def test_outermost_gets_largest_radius_corner1(self, vertical: Direction):
        """At corner 1, the spatially outermost line gets the largest radius.

        Direction.D: rightmost (largest delta) should have largest r_first.
        Direction.U: leftmost (smallest delta) should have smallest r_first
        (because leftmost is on the inside of the CCW turn).
        """
        n = 4
        results = [l_shape_radii(i, n, vertical) for i in range(n)]
        deltas = [r[0] for r in results]
        r_firsts = [r[1] for r in results]

        if vertical is Direction.D:
            # CW turn: the line with the largest (most positive) delta is
            # outermost and should have the largest r_first.
            outermost_idx = deltas.index(max(deltas))
            assert r_firsts[outermost_idx] == max(r_firsts)
        else:
            # CCW turn: the line with the most negative delta (leftmost)
            # is on the inside and should have the smallest r_first.
            innermost_idx = deltas.index(min(deltas))
            assert r_firsts[innermost_idx] == min(r_firsts)

    def test_single_line(self):
        """A single-line bundle should get base_radius at both corners."""
        delta, r1, r2 = l_shape_radii(0, 1, vertical=Direction.D)
        assert delta == 0.0
        assert r1 == CURVE_RADIUS
        assert r2 == CURVE_RADIUS


# ---------------------------------------------------------------------------
# l_shape_stagger
# ---------------------------------------------------------------------------


class TestLShapeStagger:
    """The stagger helper must agree with l_shape_radii's delta exactly."""

    @pytest.mark.parametrize("vertical", [Direction.D, Direction.U])
    @pytest.mark.parametrize("n", [1, 2, 3, 4])
    def test_matches_l_shape_radii_delta(self, n: int, vertical: Direction):
        for i in range(n):
            assert l_shape_stagger(i, n, vertical) == pytest.approx(
                l_shape_radii(i, n, vertical)[0]
            )

    @pytest.mark.parametrize("vertical", [Direction.D, Direction.U])
    @pytest.mark.parametrize("n", [2, 3, 4])
    def test_symmetric_about_zero(self, n: int, vertical: Direction):
        deltas = [l_shape_stagger(i, n, vertical) for i in range(n)]
        assert sum(deltas) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# bypass_stagger
# ---------------------------------------------------------------------------


class TestBypassStagger:
    """The U-shaped bypass's two channel offsets (deltas only)."""

    def test_single_line(self):
        """A single line sits on the channel centre in both gaps."""
        d1, d2 = bypass_stagger(0, 1, 0, 1, horizontal=Direction.R)
        assert d1 == 0.0
        assert d2 == 0.0

    @pytest.mark.parametrize("n", [1, 2, 3])
    def test_horizontal_r_matches_l_shape(self, n: int):
        """Going right, each gap's delta matches l_shape_stagger directly."""
        for i in range(n):
            d1, d2 = bypass_stagger(i, n, i, n, horizontal=Direction.R)
            assert d1 == pytest.approx(l_shape_stagger(i, n, Direction.D))
            assert d2 == pytest.approx(l_shape_stagger(i, n, Direction.U))

    @pytest.mark.parametrize("n", [2, 3])
    def test_left_going_mirrors_indices(self, n: int):
        """Going left reverses the index so the inside/outside sense mirrors."""
        for i in range(n):
            d1, d2 = bypass_stagger(i, n, i, n, horizontal=Direction.L)
            assert d1 == pytest.approx(l_shape_stagger(n - 1 - i, n, Direction.D))
            assert d2 == pytest.approx(l_shape_stagger(n - 1 - i, n, Direction.U))


# ---------------------------------------------------------------------------
# resolve_curve_radii
# ---------------------------------------------------------------------------


class TestResolveCurveRadii:
    """Tests for the shared radius resolution function."""

    def test_no_corners(self):
        """Two-point path has no corners."""
        assert resolve_curve_radii([(0, 0), (100, 0)], None) == []

    def test_single_corner_no_clamping(self):
        """Single corner with plenty of segment length uses desired radius."""
        pts = [(0, 0), (100, 0), (100, 100)]
        result = resolve_curve_radii(pts, [15.0])
        assert result == [15.0]

    def test_single_corner_clamped_by_segment(self):
        """Desired radius exceeding segment length is clamped."""
        pts = [(0, 0), (5, 0), (5, 100)]
        result = resolve_curve_radii(pts, [15.0])
        assert result == [5.0]

    def test_none_radii_uses_default(self):
        """None desired_radii falls back to default_radius."""
        pts = [(0, 0), (100, 0), (100, 100)]
        result = resolve_curve_radii(pts, None, default_radius=8.0)
        assert result == [8.0]

    def test_adjacent_corners_proportional_allocation(self):
        """Two corners sharing a short segment allocate proportionally."""
        # Shared segment is 20px, desired radii are 10 and 10
        # Each gets half = 10, which fits (equal split)
        pts = [(0, 0), (100, 0), (120, 0), (120, 100)]
        result = resolve_curve_radii(pts, [10.0, 10.0])
        assert result[0] == pytest.approx(10.0)
        assert result[1] == pytest.approx(10.0)

    def test_adjacent_corners_unequal_radii(self):
        """Unequal desired radii get proportional shares of shared segment."""
        # Shared segment = 20px, desired r1=5, r2=15
        # r1 gets 20 * 5/(5+15) = 5 -> min(5, 5) = 5
        # r2 gets 20 * 15/(5+15) = 15 -> min(15, 15) = 15
        pts = [(0, 0), (100, 0), (120, 0), (120, 100)]
        result = resolve_curve_radii(pts, [5.0, 15.0])
        assert result[0] == pytest.approx(5.0)
        assert result[1] == pytest.approx(15.0)

    def test_adjacent_corners_tight_segment(self):
        """Very short shared segment clamps both adjacent radii."""
        # Shared segment = 6px, desired r1=10, r2=10
        # r1 budget from shared = 6 * 10/20 = 3 -> clamped to 3
        # r2 budget from shared = 6 * 10/20 = 3 -> clamped to 3
        pts = [(0, 0), (100, 0), (106, 0), (106, 100)]
        result = resolve_curve_radii(pts, [10.0, 10.0])
        assert result[0] == pytest.approx(3.0)
        assert result[1] == pytest.approx(3.0)

    def test_concentric_radii_stay_distinct(self):
        """Bundle lines with different radii remain distinct after resolution."""
        pts = [(0, 0), (100, 0), (100, 100)]
        r1 = resolve_curve_radii(pts, [CURVE_RADIUS])
        r2 = resolve_curve_radii(pts, [CURVE_RADIUS + OFFSET_STEP])
        r3 = resolve_curve_radii(pts, [CURVE_RADIUS + 2 * OFFSET_STEP])
        assert r1[0] < r2[0] < r3[0]

    def test_four_corner_bypass(self):
        """Six-point bypass path resolves 4 corners."""
        pts = [(0, 0), (50, 0), (50, 200), (250, 200), (250, 0), (300, 0)]
        radii = [10.0, 12.0, 12.0, 10.0]
        result = resolve_curve_radii(pts, radii)
        assert len(result) == 4
        # All should be achievable given 50+ px segments
        for r_eff, r_des in zip(result, radii):
            assert r_eff == pytest.approx(r_des)


# ---------------------------------------------------------------------------
# concentric_corner_radius: the direction-driven nestable-corner routine
# ---------------------------------------------------------------------------


def _arc_centre(
    turn_in: tuple[float, float],
    turn_out: tuple[float, float],
    corner: tuple[float, float],
    r: float,
) -> tuple[float, float]:
    """Centre of the rounded-corner arc.

    For a 90-degree corner the inscribed arc of radius *r* is tangent to both
    legs and its centre sits at ``corner + r * (turn_out - turn_in)``.  This is
    the independent ground truth against which concentric radii are checked:
    a bundle's arcs are concentric iff every line's centre coincides.
    """
    ux = turn_out[0] - turn_in[0]
    uy = turn_out[1] - turn_in[1]
    return (corner[0] + r * ux, corner[1] + r * uy)


class TestConcentricCornerRadius:
    """The single direction-driven routine for nestable (wholesale-translated)
    90-degree corners, used in every compass orientation."""

    def test_reference_line_is_base(self):
        for turn_in, turn_out in ALL_TURNS:
            assert concentric_corner_radius(turn_in, turn_out, 0.0) == CURVE_RADIUS

    def test_arcs_are_concentric_in_every_orientation(self):
        # The concentric fan direction is turn-specific: translating each line's
        # whole corner along ``(ux, uy) = turn_out - turn_in`` keeps every arc
        # centre fixed.  The routine takes only the X displacement; verified in
        # EVERY turn orientation against the independent centre.
        base = CURVE_RADIUS
        corner0 = (100.0, 100.0)
        for turn_in, turn_out in ALL_TURNS:
            ux = turn_out[0] - turn_in[0]
            uy = turn_out[1] - turn_in[1]
            centres = []
            for k in range(4):
                dx, dy = k * OFFSET_STEP * ux, k * OFFSET_STEP * uy
                r = concentric_corner_radius(turn_in, turn_out, dx, base)
                centre = _arc_centre(
                    turn_in, turn_out, (corner0[0] + dx, corner0[1] + dy), r
                )
                centres.append(centre)
            for c in centres[1:]:
                assert c[0] == pytest.approx(centres[0][0])
                assert c[1] == pytest.approx(centres[0][1])

    def test_bundle_is_nested_step_spaced(self):
        # Adjacent lines differ by exactly one offset step at every corner, so
        # arcs nest and never cross (monotonic radii), in every orientation.
        base = CURVE_RADIUS
        for turn_in, turn_out in ALL_TURNS:
            ux = turn_out[0] - turn_in[0]
            radii = [
                concentric_corner_radius(turn_in, turn_out, k * OFFSET_STEP * ux, base)
                for k in range(4)
            ]
            diffs = [abs(b - a) for a, b in zip(radii, radii[1:])]
            for diff in diffs:
                assert diff == pytest.approx(OFFSET_STEP)

    def test_down_and_right_goes_through_one_routine(self):
        # The user's litmus: a bundle turning between rightward and downward
        # travel (either order) is sized by the SAME routine and is concentric.
        base = CURVE_RADIUS
        for turn_in, turn_out in ((RIGHT, DOWN), (DOWN, RIGHT)):
            ux = turn_out[0] - turn_in[0]
            r_inner = concentric_corner_radius(turn_in, turn_out, 0.0, base)
            r_outer = concentric_corner_radius(
                turn_in, turn_out, OFFSET_STEP * ux, base
            )
            assert r_inner == base
            assert abs(r_outer - r_inner) == pytest.approx(OFFSET_STEP)

    def test_radius_matches_both_axis_projections(self):
        # For a concentric fan (whole corner translated along (ux, uy)) the
        # X- and Y-axis radius derivations agree: base - dx*ux == base - dy*uy.
        # The routine uses the X projection; this pins that they coincide.
        base = CURVE_RADIUS
        for turn_in, turn_out in ALL_TURNS:
            ux = turn_out[0] - turn_in[0]
            uy = turn_out[1] - turn_in[1]
            dx, dy = 2 * OFFSET_STEP * ux, 2 * OFFSET_STEP * uy
            r = concentric_corner_radius(turn_in, turn_out, dx, base)
            assert r == pytest.approx(base - dx * ux)
            assert r == pytest.approx(base - dy * uy)

    def test_min_radius_floors_deep_inside_lines(self):
        # An inside-of-turn line in a deep bundle can drive radius below zero.
        base = CURVE_RADIUS
        # DOWN->RIGHT: ux = +1, so positive dx subtracts -> can go negative.
        r = concentric_corner_radius(DOWN, RIGHT, 100.0, base, min_radius=0.1)
        assert r == 0.1
