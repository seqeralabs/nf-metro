"""A bypass-V curve clears its section edge on the lane it is drawn on.

A bypass helper carries no marker and no label, so its section box only has to
clear the diversion curve rendered through it: ``CURVE_RADIUS +
MIN_STATION_FLAT_LENGTH / 2``.  That curve is drawn on the helper's per-line
lane, and a multi-line bundle puts that lane a whole offset step or more away
from the helper's anchor Y, so the clearance only holds if it is measured
against the drawn lane rather than the anchor.

Measured from routed geometry with the render's own offset transform
(:func:`apply_route_offsets`), independent of the bbox prediction that reserves
the room.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import CURVE_RADIUS, MIN_STATION_FLAT_LENGTH
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import apply_route_offsets
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, is_bypass_v

_ROOT = Path(__file__).resolve().parent.parent
_V_CURVE_CLEARANCE = CURVE_RADIUS + MIN_STATION_FLAT_LENGTH / 2
_TOL = 0.5

# Every tracked fixture carrying a bypass-V helper in a section whose bbox the
# helper can bind, plus fixtures whose helpers sit well inside their box as
# controls that the floor is a floor and not a target.
FIXTURES = [
    "examples/guide/05_file_icons.mmd",
    "examples/guide/06a_without_hidden.mmd",
    "examples/guide/06b_with_hidden.mmd",
    "examples/topologies/bypass_label_rake.mmd",
    "examples/topologies/bypass_label_rake_left.mmd",
    "examples/topologies/bypass_label_rake_wide.mmd",
    "examples/topologies/bypass_v_tight.mmd",
    "examples/topologies/inrow_skip_breeze.mmd",
    "examples/topologies/rowmate_tb_side_entry_top_align_grow.mmd",
    "examples/topologies/fan_branch_additional_outputs.mmd",
    "examples/topologies/same_destination_short_overlap.mmd",
    "tests/fixtures/da_pipeline.mmd",
]


def _layout(path: str) -> MetroGraph:
    graph = parse_metro_mermaid((_ROOT / path).read_text())
    compute_layout(graph)
    return graph


def _lane_clearances(graph: MetroGraph) -> list[tuple[str, str, float, float, float]]:
    """``(section, helper, lane_y, top_clearance, bottom_clearance)`` per helper.

    ``lane_y`` is where the diversion curve is actually drawn at the helper's
    column, taken from the two routes the helper joins.
    """
    offsets = compute_station_offsets(graph)
    drawn = [
        ((route.edge.source, route.edge.target), apply_route_offsets(route, offsets))
        for route in route_edges(graph, station_offsets=offsets)
    ]
    rows = []
    for section in graph.sections.values():
        if section.bbox_h <= 0:
            continue
        top = section.bbox_y
        bottom = section.bbox_y + section.bbox_h
        for sid in section.station_ids:
            station = graph.stations.get(sid)
            if station is None or not is_bypass_v(sid):
                continue
            lane_ys = [
                y
                for ends, points in drawn
                if sid in ends
                for x, y in points
                if abs(x - station.x) <= _TOL
            ]
            if not lane_ys:
                continue
            rows.append(
                (
                    section.id,
                    sid,
                    max(lane_ys),
                    min(lane_ys) - top,
                    bottom - max(lane_ys),
                )
            )
    return rows


@pytest.mark.parametrize("fixture", FIXTURES, ids=[Path(f).stem for f in FIXTURES])
def test_bypass_v_curve_clears_section_edges_on_its_drawn_lane(fixture: str):
    graph = _layout(fixture)
    rows = _lane_clearances(graph)
    assert rows, f"{fixture} carries no measurable bypass-V helper"
    offences = [
        f"{section}/{helper}: top {top:.1f}, bottom {bot:.1f}"
        for section, helper, _lane, top, bot in rows
        if top < _V_CURVE_CLEARANCE - _TOL or bot < _V_CURVE_CLEARANCE - _TOL
    ]
    assert not offences, (
        f"{fixture}: bypass-V curve drawn within {_V_CURVE_CLEARANCE:.0f}px of a "
        f"section edge: " + "; ".join(offences)
    )


def test_offset_lane_is_what_binds_the_box():
    """The lock is offset-sensitive, not just a restatement of the anchor rule.

    ``processing``'s lower helper carries a bundle offset, so its drawn lane
    sits below its anchor and the box has to reserve for the lane.  Without a
    fixture where the two differ, the clearance assertion above would pass on
    an anchor-only rule.
    """
    graph = _layout("examples/guide/06a_without_hidden.mmd")
    rows = {
        helper: (lane, bot)
        for section, helper, lane, _top, bot in _lane_clearances(graph)
        if section == "processing"
    }
    lane, bottom_clearance = rows["__bypass_quant_search_2"]
    anchor = graph.stations["__bypass_quant_search_2"].y
    assert lane > anchor + _TOL, (lane, anchor)
    assert bottom_clearance >= _V_CURVE_CLEARANCE - _TOL
