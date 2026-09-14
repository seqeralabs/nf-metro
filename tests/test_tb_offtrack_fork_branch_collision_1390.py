"""A TB off-track output drops clear of a sibling fork branch (#1390).

A fork producer emits an ``off_track`` output plus an on-track branch that fans
to the same side the output is lifted toward.  The output must not seat in the
narrow gap between the trunk and that branch column: the branch's onward
diagonal runs through the gap, and the grid snap can pull the output onto the
branch's row, so a slot there both clips the diagonal and can collide outright
with the branch station.  The output drops to the trunk's clear opposite side.
"""

from __future__ import annotations

from pathlib import Path

from nf_metro.layout import engine
from nf_metro.parser.mermaid import parse_metro_mermaid_file

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "regressions"
    / "tb_offtrack_fork_branch_collision_1390.mmd"
)

OFF_TRACK = "mapped_out"
BRANCH = "markduplicates"
TRUNK = "bam_convert"


def test_tb_offtrack_output_clears_fork_branch_station():
    """The off-track output holds a cell distinct from the sibling fork branch."""
    graph = parse_metro_mermaid_file(FIXTURE)
    engine.compute_layout(graph, validate=True)

    off = graph.stations[OFF_TRACK]
    branch = graph.stations[BRANCH]
    separation = abs(off.x - branch.x) + abs(off.y - branch.y)
    assert separation > 1.0, (
        f"off-track output {OFF_TRACK!r} at ({off.x},{off.y}) collides with fork "
        f"branch {BRANCH!r} at ({branch.x},{branch.y})"
    )


def test_tb_offtrack_output_not_wedged_in_trunk_branch_gap():
    """The output sits outside the trunk-to-branch gap, not squeezed within it."""
    graph = parse_metro_mermaid_file(FIXTURE)
    engine.compute_layout(graph, validate=True)

    off_x = graph.stations[OFF_TRACK].x
    trunk_x = graph.stations[TRUNK].x
    branch_x = graph.stations[BRANCH].x
    gap_lo, gap_hi = sorted((trunk_x, branch_x))
    assert not (gap_lo + 1.0 < off_x < gap_hi - 1.0), (
        f"off-track output {OFF_TRACK!r} at x={off_x} is wedged between trunk "
        f"x={trunk_x} and branch column x={branch_x}, under the branch diagonal"
    )


def test_tb_offtrack_spur_pitch_matches_fork_branch():
    """The spur offsets from the trunk by the same widened pitch as the branch.

    Wide labels drive the section's column pitch past the base value; the fork
    branch tracks that widened pitch and the off-track spur must match it, so
    the two sit symmetric about the trunk rather than at an uneven grid.
    """
    graph = parse_metro_mermaid_file(FIXTURE)
    engine.compute_layout(graph, validate=True)

    trunk_x = graph.stations[TRUNK].x
    branch_offset = abs(graph.stations[BRANCH].x - trunk_x)
    spur_offset = abs(graph.stations[OFF_TRACK].x - trunk_x)
    assert abs(spur_offset - branch_offset) < 1.0, (
        f"off-track spur offset {spur_offset} from trunk differs from fork "
        f"branch offset {branch_offset}; the spur ignored the widened pitch"
    )
