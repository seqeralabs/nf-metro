"""Invariants for multi-section grid cells (issue #1213).

A ``%%metro grid:`` directive may name several comma-separated sections in
its first field; they share one grid cell and pack side-by-side along the
flow axis.  The cell's width is the sum of its members' widths plus the
inter-section gap, and the column's width is the widest cell down the
column -- so a short+long pair in one row aligns top-to-bottom with a
long+short pair packed into the same column below it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import SECTION_X_PADDING
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.section_placement import _pack_cells
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Section

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
TOPO_DIR = EXAMPLES_DIR / "topologies"

TOL = 2.0


def _load(path: Path):
    g = parse_metro_mermaid(path.read_text())
    compute_layout(g)
    return g


def _left(section) -> float:
    return section.bbox_x


def _right(section) -> float:
    return section.bbox_x + section.bbox_w


def test_packed_members_share_their_declared_cell():
    """Every member of a declared cell pack lands in that (col, row)."""
    g = _load(TOPO_DIR / "multi_section_cell.mmd")
    assert g.cell_packs, "fixture declares multi-section cells"
    for (col, row), members in g.cell_packs.items():
        for sid in members:
            sec = g.sections[sid]
            assert (sec.grid_col, sec.grid_row) == (col, row), (
                f"{sid} declared in cell ({col},{row}) but placed at "
                f"({sec.grid_col},{sec.grid_row})"
            )


def test_packed_members_do_not_overlap():
    """Side-by-side members never overlap horizontally."""
    g = _load(TOPO_DIR / "multi_section_cell.mmd")
    for members in g.cell_packs.values():
        secs = sorted((g.sections[m] for m in members), key=_left)
        for upstream, downstream in zip(secs, secs[1:]):
            assert _right(upstream) <= _left(downstream) + TOL, (
                f"packed sections overlap: {upstream.id} right={_right(upstream):.0f} "
                f"> {downstream.id} left={_left(downstream):.0f}"
            )


def test_packed_cells_align_down_their_column():
    """Two packed cells in one column share left and right extents.

    ``short_a+long_a`` (row 0) and ``long_b+short_b`` (row 1) both live in
    column 1 with equal-width members, so the column packs them to the same
    horizontal extent and the rows align top-to-bottom.
    """
    g = _load(TOPO_DIR / "multi_section_cell.mmd")
    top = [g.sections["short_a"], g.sections["long_a"]]
    bottom = [g.sections["long_b"], g.sections["short_b"]]

    top_left = min(_left(s) for s in top)
    bottom_left = min(_left(s) for s in bottom)
    top_right = max(_right(s) for s in top)
    bottom_right = max(_right(s) for s in bottom)

    assert abs(top_left - bottom_left) <= TOL, (
        f"packed cells misaligned on the left: {top_left:.0f} vs {bottom_left:.0f}"
    )
    assert abs(top_right - bottom_right) <= TOL, (
        f"packed cells misaligned on the right: {top_right:.0f} vs {bottom_right:.0f}"
    )


def test_packed_cell_does_not_inflate_its_column():
    """The packed cell, not its widest single member, sets the column width.

    The neighbouring single-section column (``norm``/``cons`` in column 2)
    starts just past the packed column's right edge plus one section gap, so
    a packed column reserves only the width its members actually occupy.
    """
    g = _load(TOPO_DIR / "multi_section_cell.mmd")
    packed_right = max(_right(g.sections[s]) for s in ("short_a", "long_a"))
    norm_left = _left(g.sections["norm"])
    assert norm_left > packed_right, (
        f"column 2 ({norm_left:.0f}) must sit right of the packed column "
        f"({packed_right:.0f})"
    )


def _cell(sid: str, direction: str, width: float) -> Section:
    sec = Section(id=sid, name=sid, direction=direction, bbox_w=width)
    return sec


def test_lr_pack_keeps_listed_order_left_to_right():
    """An LR cell lays members left-to-right in listed order, left-aligned."""
    first, second = _cell("first", "LR", 100), _cell("second", "LR", 200)
    scoped = {"first": first, "second": second}
    _pack_cells(
        scoped, {(1, 0): ["first", "second"]}, {1: 500.0}, {1: 1000.0}, set(), 50.0
    )
    assert first.offset_x == 500.0
    assert second.offset_x > first.offset_x
    assert second.offset_x == 500.0 + (100 + SECTION_X_PADDING) + 50.0


def test_rl_pack_flows_right_to_left_and_right_aligns():
    """An RL cell puts the flow-leading member rightmost and hugs the column's
    right edge even when the column is wider than the packed content."""
    lead, trail = _cell("lead", "RL", 100), _cell("trail", "RL", 200)
    scoped = {"lead": lead, "trail": trail}
    col_w = 1000.0
    _pack_cells(scoped, {(1, 0): ["lead", "trail"]}, {1: 500.0}, {1: col_w}, {1}, 50.0)

    # Flow-leading member sits to the right of the later one.
    assert lead.offset_x > trail.offset_x
    # The pack's right edge hugs the column's right edge (minus the trailing
    # section padding the effective-width advance reserves).
    pack_right = lead.offset_x + lead.bbox_w
    assert abs(pack_right - (500.0 + col_w - SECTION_X_PADDING)) <= TOL


@pytest.mark.parametrize("sid", ["short_a", "long_a", "long_b", "short_b"])
def test_packed_member_stations_inside_bbox(sid):
    """Packing repositions each member's bbox to enclose its own stations."""
    g = _load(TOPO_DIR / "multi_section_cell.mmd")
    sec = g.sections[sid]
    for st_id in sec.station_ids:
        st = g.stations[st_id]
        assert _left(sec) - 5 <= st.x <= _right(sec) + 5, (
            f"{st_id} x={st.x:.0f} outside packed bbox "
            f"[{_left(sec):.0f},{_right(sec):.0f}] of {sid}"
        )
