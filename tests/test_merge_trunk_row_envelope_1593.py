"""A bottommost-row merge trunk keeps the inter-row channel it was reserved.

The trunk that collects a merge's cross-row feeders runs a horizontal channel in
the gap above the bottom row, bounded by the row envelopes: it crosses every
upper-row box on its way to a target that need share no column with any of them,
and the parser has rewritten its authored connectors through fan and merge nodes
so no section pair records the relationship at all.  ``_enforce_min_row_gaps``
reserves that gap envelope-wide; the row cascade that runs afterwards
(``_tighten_lower_rows_after_shrink`` and ``push_lower_rows_after_bbox_grow``)
must measure it the same way, or it closes the reservation against a
column-overlapping pair the corridor never travels between and the trunk is left
crossing the box that bounds it (#1593; the resulting pass-through is locked by
``test_inter_row_gap_reserved_1312``).

Within that gap the channel level is where the trunk's own track runs -- the
level its branch feeders drop onto -- so the drawn traverse has to land on it
rather than half a bundle step off it, which is what leaves the settled
corridor's realised coordinate outside the band its reservation allocates.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from nf_metro.api import prepare_graph, resolve_theme
from nf_metro.layout.route_plan import build_route_plan_query
from nf_metro.layout.route_reservations import RowGapRegion
from nf_metro.layout.section_placement import _merge_trunk_row_minimums
from nf_metro.render.svg import build_observed_render_plan

ROOT = Path(__file__).parents[1]
TOPOLOGIES = ROOT / "examples" / "topologies"
REPORT_HO = ROOT / "tests" / "fixtures" / "route_reservations" / "reportho.metro"

# The reported repro plus every other map that reserves an envelope-wide
# inter-row channel for a bottommost-row merge trunk.
FIXTURES = (
    REPORT_HO,
    ROOT / "examples" / "genomic_pipeline.mmd",
    TOPOLOGIES / "exit_run_three_drop_columns.mmd",
    TOPOLOGIES / "merge_around_below_leftmost.mmd",
    TOPOLOGIES / "merge_bottom_row_bypass.mmd",
    TOPOLOGIES / "merge_feeder_shared_channel_gap.mmd",
    TOPOLOGIES / "merge_feeders_three_columns.mmd",
    TOPOLOGIES / "merge_leftmost_sink_branch.mmd",
    ROOT / "tests" / "fixtures" / "ambiguous_exit_continuation.mmd",
    ROOT / "tests" / "fixtures" / "regressions" / "stacked_collector_fanin.mmd",
)
IDS = tuple(path.stem for path in FIXTURES)


def _laid_out(path: Path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return prepare_graph(path.read_text(), source_dir=str(path.parent))


@pytest.mark.parametrize("path", FIXTURES, ids=IDS)
def test_row_envelopes_hold_the_reserved_merge_trunk_channel(path: Path) -> None:
    graph = _laid_out(path)
    reserved = _merge_trunk_row_minimums(graph)
    assert reserved, "fixture no longer reserves an envelope-wide inter-row channel"

    placed = [section for section in graph.sections.values() if section.bbox_h > 0]
    for (upper, lower), required in sorted(reserved.items()):
        bottom = max(
            (
                section.bbox_y + section.bbox_h
                for section in placed
                if section.grid_row + section.grid_row_span - 1 == upper
            ),
            default=None,
        )
        top = min(
            (section.bbox_y for section in placed if section.grid_row == lower),
            default=None,
        )
        if bottom is None or top is None:
            continue
        assert top - bottom >= required - 0.01, (
            f"row {upper}/{lower} envelope gap {top - bottom:.2f}px is below the "
            f"{required:.2f}px reserved for the merge trunk's channel"
        )


def test_the_settled_report_corridor_lands_inside_the_band_it_reserves() -> None:
    """The drawn traverse keeps both clearances the corridor declares.

    Measured on the settled render rather than the laid-out graph, so it covers
    the channel the renderer draws after envelope settlement has moved the rows.
    A coordinate outside the band means the channel level located the bundle
    centreline while the stagger put the ink elsewhere, leaving the run inside
    the clearance it owes the ``report`` header even though its corridor has the
    capacity for it.
    """
    graph = _laid_out(REPORT_HO)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        observed = build_observed_render_plan(graph, resolve_theme(graph))

    plan = observed.route_plan
    query = build_route_plan_query(plan)
    row_gaps = [
        item for item in plan.reservations if isinstance(item.region, RowGapRegion)
    ]
    assert row_gaps, "fixture no longer reserves the report corridor"
    for reservation in row_gaps:
        realised = query.realised_reservation(reservation.id)
        assert realised is not None
        assert realised.capacity_slack >= -0.01
        assert realised.negative_side_slack >= -0.01
        assert realised.positive_side_slack >= -0.01
