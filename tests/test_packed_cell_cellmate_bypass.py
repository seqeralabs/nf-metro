"""Regression: a non-consumer bypass past a packed-cell cell-mate.

A packed cell (``%%metro grid: a, b | col,row``) places more than one section
side by side in a single grid cell.  A line travelling to one member of the
cell, whose path is blocked by a cell-mate of that member, must bypass around
the cell-mate the same way it bypasses a standalone intervening section.

The defect: ``_has_intervening_sections`` (the bypass-dispatch decision) and
the bypass's own gap-channel placement both reason in grid-column terms. A
cell-mate sharing the target's own grid column is invisible to both, so the
dispatcher never routes the edge through the bypass family, and even once it
does, the gap placed "to the right of the target's column" lands beyond the
cell-mate instead of between the cell-mate and the target.

Three fixtures exercise the same invariant at different source-to-cell spacing
and row relationships:

* ``packed_cell_cellmate_bypass`` - the bypassing line's source sits one
  column past the packed cell, with an empty gap column between (issue #1228).
* ``packed_cell_cellmate_bypass_adjacent`` - the source sits in the column
  immediately adjacent to the packed cell, with no gap column to descend
  through (issue #1233). The dispatch gate keyed on column distance hid the
  cell-mate entirely here, so the line collapsed back to a straight run
  through the cell-mate's interior.
* ``packed_cell_cellmate_bypass_cross_row`` - the source is itself a member
  of the packed cell, and the target sits a row below and a column over. The
  cell-mate obstruction is on the *source*'s row rather than a shared row
  between source and target, so it never registers as "same row" either;
  the plain L-shape's first leg runs the full source-row width before
  turning, straight through the cell-mate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases._common import routes_through_unrelated_sections
from nf_metro.layout.routing.core import route_edges_centred
from nf_metro.layout.routing.offsets import compute_station_offsets
from nf_metro.parser import parse_metro_mermaid

TOPOLOGIES = Path(__file__).parent.parent / "examples" / "topologies"
FIXTURES = (
    "packed_cell_cellmate_bypass",
    "packed_cell_cellmate_bypass_adjacent",
    "packed_cell_cellmate_bypass_cross_row",
)


def _layout(fixture: str):
    graph = parse_metro_mermaid((TOPOLOGIES / f"{fixture}.mmd").read_text())
    compute_layout(graph)
    return graph


@pytest.mark.parametrize("fixture", FIXTURES)
def test_no_line_routes_through_cell_mate(fixture: str) -> None:
    """No routed line crosses the interior of a packed cell-mate it never touches."""
    graph = _layout(fixture)
    offenders = routes_through_unrelated_sections(graph)
    assert not offenders, "; ".join(
        f"{rp.line_id} {rp.edge.source}->{rp.edge.target} through {sid!r}"
        for rp, sid in offenders
    )


@pytest.mark.parametrize(
    "fixture",
    ("packed_cell_cellmate_bypass", "packed_cell_cellmate_bypass_adjacent"),
)
def test_rna_bypass_clears_realign_on_both_sides(fixture: str) -> None:
    """The ``rna`` bypass traverses below ``realign``, not collapsed to a point.

    ``rna`` leaves ``consensus`` and must reach ``reporting``, its packed
    cell-mate ``realign`` sitting between them.  The below-row traverse must
    actually span clear of ``realign``'s full width (both its descent, right
    of the box, and its rise, left of the box) rather than the two collapsing
    onto the same x and leaving the final approach to cut through the box.
    """
    graph = _layout(fixture)
    realign = graph.sections["realign"]
    realign_left = realign.bbox_x
    realign_right = realign.bbox_x + realign.bbox_w
    realign_bottom = realign.bbox_y + realign.bbox_h
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)

    descents = [
        rp
        for rp in routes
        if rp.line_id == "rna"
        and rp.is_inter_section
        and rp.edge.target == "reporting__entry_right_11"
    ]
    assert descents, "expected an inter-section rna route into reporting"
    for rp in descents:
        below_row_xs = [y for _x, y in rp.points if y > realign_bottom - 1.0]
        assert below_row_xs, "expected a leg below realign's bottom edge"
        traverse_xs = [x for x, y in rp.points if y > realign_bottom - 1.0]
        assert min(traverse_xs) <= realign_left + 1.0, (
            f"bypass traverse {traverse_xs} never reaches left of realign "
            f"({realign_left:.1f})"
        )
        assert max(traverse_xs) >= realign_right - 1.0, (
            f"bypass traverse {traverse_xs} never reaches right of realign "
            f"({realign_right:.1f})"
        )


def test_cross_row_bypass_clears_source_row_cell_mate() -> None:
    """The cross-row ``b`` route from ``quant`` to ``sink`` clears ``rep``.

    ``quant`` and ``rep`` are packed into the same grid cell; ``rep`` sits on
    ``quant``'s own row, directly between ``quant`` and ``sink`` (one row
    below, one column over).  No segment of the ``quant``->``sink`` route may
    cross ``rep``'s box, since that route never touches ``rep``.
    """
    graph = _layout("packed_cell_cellmate_bypass_cross_row")
    rep = graph.sections["rep"]
    rep_left = rep.bbox_x
    rep_right = rep.bbox_x + rep.bbox_w
    rep_top = rep.bbox_y
    rep_bottom = rep.bbox_y + rep.bbox_h
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)

    descents = [
        rp
        for rp in routes
        if rp.line_id == "b"
        and rp.is_inter_section
        and rp.edge.target == "sink__entry_left_2"
    ]
    assert descents, "expected an inter-section b route into sink"
    for rp in descents:
        for (ax, ay), (bx, by) in zip(rp.points, rp.points[1:]):
            if abs(ay - by) > 1.0:
                continue  # vertical leg: rep's own column check covers it
            lo_x, hi_x = (ax, bx) if ax <= bx else (bx, ax)
            crosses_x = lo_x < rep_right and hi_x > rep_left
            crosses_y = rep_top <= ay <= rep_bottom
            assert not (crosses_x and crosses_y), (
                f"leg ({ax:.1f},{ay:.1f})->({bx:.1f},{by:.1f}) cuts through rep "
                f"(x={rep_left:.1f}..{rep_right:.1f}, "
                f"y={rep_top:.1f}..{rep_bottom:.1f})"
            )
