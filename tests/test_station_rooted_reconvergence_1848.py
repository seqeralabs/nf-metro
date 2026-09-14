"""A trunkless symmetric fan that reconverges onto a hidden merge node sits on
the fan's served-span centreline even when the fan is rooted at an internal
station rather than a section entry port.

``orf_merge`` (entry-port-rooted) and this join are the same shape: several
branches diverge and fully reconverge, and under ``diamond_style: symmetric``
the join belongs on the midpoint of the branches it merges.  A hidden merge
node carries no glyph and is skipped by every on-track placement mechanism, so
without this rule it stays on whatever grid row its topological layer occupied
(the bottom branch's row here), not the span midpoint (issue #1848).

A *visible* internal join is deliberately excluded: the general placement
pipeline already seats it on its midpoint, and re-seating it from the grid
snap's intermediate coordinates displaces it.
"""

from __future__ import annotations

from pathlib import Path

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases.fan_bundles import _symmetric_reconvergence_joins
from nf_metro.layout.phases.off_track import _off_track_output_below
from nf_metro.parser.mermaid import parse_metro_mermaid

FIXTURES = Path(__file__).parent / "fixtures" / "curve_invariant_repros"

_HIDDEN_FIXTURE = FIXTURES / "riboseq_inter_row_corridor.mmd"
_HIDDEN_JOIN = "__converge_te_out_1"
_HIDDEN_BRANCHES = ("anota2seq", "deltate", "dotseq")

_VISIBLE_FIXTURE = FIXTURES / "inter_row_corridor_overflow.mmd"
_VISIBLE_JOIN = "step_f5"
_VISIBLE_BRANCHES = ("step_f2", "step_f3", "step_f4")

TOL = 2.0


def _layout(fixture):
    graph = parse_metro_mermaid(fixture.read_text())
    compute_layout(graph)
    return graph


def _fan_midpoint(graph, branches):
    branch_ys = [graph.stations[b].y for b in branches]
    return (min(branch_ys) + max(branch_ys)) / 2.0


def test_hidden_station_rooted_join_is_admitted():
    graph = _layout(_HIDDEN_FIXTURE)
    joins = _symmetric_reconvergence_joins(graph)
    assert _HIDDEN_JOIN in joins, sorted(joins)
    assert graph.stations[_HIDDEN_JOIN].is_hidden
    assert set(joins[_HIDDEN_JOIN]) == set(_HIDDEN_BRANCHES)


def test_hidden_station_rooted_join_on_fan_centreline():
    graph = _layout(_HIDDEN_FIXTURE)
    midpoint = _fan_midpoint(graph, _HIDDEN_BRANCHES)
    join_y = graph.stations[_HIDDEN_JOIN].y
    assert abs(join_y - midpoint) < TOL, (
        f"hidden reconvergence join {_HIDDEN_JOIN} y={join_y} not on fan "
        f"centreline {midpoint}"
    )


def test_visible_station_rooted_join_is_excluded():
    # A station-rooted fan whose join is visible must NOT be seated here: the
    # general placement pipeline already sits it on its midpoint, and re-seating
    # it from the grid snap's intermediate coordinates would drag it off.
    graph = _layout(_VISIBLE_FIXTURE)
    joins = _symmetric_reconvergence_joins(graph)
    assert not graph.stations[_VISIBLE_JOIN].is_hidden
    assert _VISIBLE_JOIN not in joins, sorted(joins)
    midpoint = _fan_midpoint(graph, _VISIBLE_BRANCHES)
    join_y = graph.stations[_VISIBLE_JOIN].y
    assert abs(join_y - midpoint) < TOL, (join_y, midpoint)


def test_hidden_station_rooted_branches_registered_half_grid():
    """The branches of a station-rooted reconvergence on a half-pitch spine are
    recorded in ``graph.half_grid_station_ids``.

    The join's section carries a half-grid LR entry port, so the spine the join
    sits on - and the branch column mirrored about it - lands half a pitch off
    the section's trunk grid.  The entry-port form of this fan records its
    centred port there (``_register_half_grid_entry_fan_ports``); the
    station-rooted form must likewise record its branches, so the grid-alignment
    invariants read the offsets as the fan's spine grid rather than stray
    off-grid placement (issue #1848).
    """
    graph = _layout(_HIDDEN_FIXTURE)
    trunk_anchor = graph.stations[_HIDDEN_JOIN].y
    pitch = graph.stations["dotseq"].y - graph.stations["deltate"].y
    for branch in _HIDDEN_BRANCHES:
        st = graph.stations[branch]
        offset = (st.y - trunk_anchor) / pitch
        rides_spine_grid = abs(offset - round(offset)) * pitch < TOL
        assert rides_spine_grid, (
            f"{branch} y={st.y} does not sit a whole pitch from spine "
            f"{trunk_anchor}; the fixture no longer exercises the half-grid gap"
        )
        assert branch in graph.post_layout_half_grid_station_ids, (
            f"{branch} rides the reconvergence spine at half pitch but is not "
            f"registered in graph.post_layout_half_grid_station_ids"
        )


def test_settled_spine_mark_leaves_off_track_classification_alone():
    """Recording the settled spine does not re-classify an off-track output.

    ``_off_track_output_below`` reads ``graph.half_grid_station_ids`` to pick a
    section's trunk baseline: with every trunk station marked it abandons the
    dominant station row and anchors on the LR port instead, on the premise that
    a symmetric entry fork has vacated the trunk.  This fan's branches are real,
    populated rows, so that premise must not hold here.  The mark also lands
    after Stage 6.6/6.8 have placed the section's off-track outputs, so a
    classifier that saw it would answer one way during placement and the
    opposite way on the settled graph, and replaying the stage would move
    ``te_out`` a row (issue #1874).
    """
    graph = _layout(_HIDDEN_FIXTURE)
    producer_y = graph.stations[_HIDDEN_JOIN].y
    te_out = graph.stations["te_out"]
    assert te_out.off_track
    assert te_out.y > producer_y + TOL, (
        f"te_out y={te_out.y} no longer sits below its producer "
        f"{_HIDDEN_JOIN} y={producer_y}; the fixture no longer exercises a "
        f"downward off-track output"
    )
    assert "te_out" in _off_track_output_below(graph), (
        "te_out was placed below its producer but the settled graph classifies "
        "it as lifting above one"
    )
