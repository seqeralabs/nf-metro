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

See issue #1228 (companion to #1211, which fixed the equivalent
separate-cell case).
"""

from __future__ import annotations

from pathlib import Path

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases._common import routes_through_unrelated_sections
from nf_metro.layout.routing.core import route_edges_centred
from nf_metro.layout.routing.offsets import compute_station_offsets
from nf_metro.parser import parse_metro_mermaid

TOPOLOGIES = Path(__file__).parent.parent / "examples" / "topologies"
FIXTURE = "packed_cell_cellmate_bypass"


def _layout():
    graph = parse_metro_mermaid((TOPOLOGIES / f"{FIXTURE}.mmd").read_text())
    compute_layout(graph)
    return graph


def test_no_line_routes_through_cell_mate() -> None:
    """No routed line crosses the interior of a packed cell-mate it never touches."""
    graph = _layout()
    offenders = routes_through_unrelated_sections(graph)
    assert not offenders, "; ".join(
        f"{rp.line_id} {rp.edge.source}->{rp.edge.target} through {sid!r}"
        for rp, sid in offenders
    )


def test_rna_bypass_clears_realign_on_both_sides() -> None:
    """The ``rna`` bypass traverses below ``realign``, not collapsed to a point.

    ``rna`` leaves ``consensus`` and must reach ``reporting``, its packed
    cell-mate ``realign`` sitting between them.  The below-row traverse must
    actually span clear of ``realign``'s full width (both its descent, right
    of the box, and its rise, left of the box) rather than the two collapsing
    onto the same x and leaving the final approach to cut through the box.
    """
    graph = _layout()
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
