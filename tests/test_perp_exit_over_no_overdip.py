"""The cross-column perp-drop lead-in must clear only the sections it passes
under, not the row's deepest section in a far column.

``_route_perp_exit_over``'s ``crosses_box`` branch (a BOTTOM exit feeding a
TOP entry, or the mirror) drops out of the exit, runs across to the
inter-column gap, then rises/descends to the entry-side corridor.  The
exit-side down-leg travels only from the exit X to the gap, so it need clear
just the section(s) it passes under -- the source column -- not the deepest
section anywhere in the row.  Seating it on the whole row's bottom edge makes
it loop to the canvas bottom around a tall section it never crosses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import _sections_in_row
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

# Fixtures whose perp exit crosses to the inter-column gap to reach a
# perpendicular entry on the far side of the target (the ``crosses_box`` lead-in).
CROSS_COLUMN_PERP_FIXTURES = [
    EXAMPLES / "topologies" / "cross_column_perp_drop_far_exit.mmd",
]


def _crosses_box_routes(graph, routes):
    """Routes taking the ``crosses_box`` lead-in: a TOP/BOTTOM exit feeding a
    TOP/BOTTOM entry on the far side of the target (a six-waypoint centreline
    that drops, crosses to the gap, then climbs to the entry-side corridor)."""
    out = []
    for r in routes:
        src_port = graph.ports.get(r.edge.source)
        tgt_port = graph.ports.get(r.edge.target)
        if (
            src_port is not None
            and tgt_port is not None
            and not src_port.is_entry
            and tgt_port.is_entry
            and src_port.side in (PortSide.TOP, PortSide.BOTTOM)
            and tgt_port.side in (PortSide.TOP, PortSide.BOTTOM)
            and r.is_inter_section
            and len(r.points) == 6
        ):
            out.append(r)
    return out


@pytest.mark.parametrize("path", CROSS_COLUMN_PERP_FIXTURES, ids=lambda p: p.stem)
def test_perp_exit_over_leadin_does_not_dip_below_far_section(path: Path) -> None:
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    crossing = _crosses_box_routes(graph, routes)
    assert crossing, f"{path.stem}: expected a crosses_box perp-exit lead-in route"

    tol = 1.0
    for r in crossing:
        src_sec_id = graph.stations[r.edge.source].section_id
        src_sec = graph.sections[src_sec_id]
        row = src_sec.grid_row
        is_bottom = graph.ports[r.edge.source].side == PortSide.BOTTOM

        # The exit-side corridor leg: the corner right after the exit and the
        # horizontal run from it to the inter-column gap.
        (sx, _), (cx, cy), (gx, _gy), *_ = r.points
        assert abs(cx - sx) < tol, f"{path.stem}: first leg is not the exit drop"
        leg_lo, leg_hi = sorted((sx, gx))

        for s in _sections_in_row(graph, row):
            if s.id == src_sec_id:
                continue
            s_lo, s_hi = s.bbox_x, s.bbox_x + s.bbox_w
            if leg_hi > s_lo + tol and s_hi > leg_lo + tol:
                continue  # the leg passes under this section; clearing it is fine
            s_bottom = s.bbox_y + s.bbox_h
            if is_bottom:
                assert cy <= s_bottom + tol, (
                    f"{path.stem}: {r.edge.source}->{r.edge.target} down-leg at "
                    f"y={cy:.0f} dips below section {s.id} (bottom={s_bottom:.0f}) "
                    f"it never passes under"
                )
            else:
                assert cy >= s.bbox_y - tol, (
                    f"{path.stem}: {r.edge.source}->{r.edge.target} up-leg at "
                    f"y={cy:.0f} rises above section {s.id} (top={s.bbox_y:.0f}) "
                    f"it never passes over"
                )
