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
from nf_metro.layout.routing.invariants import (
    check_perp_exit_over_leadin_clears_only_spanned_sections,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

# Fixtures whose perp exit crosses to the inter-column gap to reach a
# perpendicular entry on the far side of the target (the ``crosses_box`` lead-in).
CROSS_COLUMN_PERP_FIXTURES = [
    EXAMPLES / "topologies" / "cross_column_perp_drop_far_exit.mmd",
]


def _exercises_crosses_box(graph, routes) -> bool:
    """True if some route takes the up-and-over lead-in: a TOP/BOTTOM exit
    feeding a TOP/BOTTOM entry as a six-waypoint inter-section centreline."""
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
            return True
    return False


@pytest.mark.parametrize("path", CROSS_COLUMN_PERP_FIXTURES, ids=lambda p: p.stem)
def test_perp_exit_over_leadin_does_not_dip_below_far_section(path: Path) -> None:
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    routes = route_edges(graph, station_offsets=compute_station_offsets(graph))

    assert _exercises_crosses_box(graph, routes), (
        f"{path.stem}: expected a crosses_box perp-exit lead-in route"
    )

    overdips = check_perp_exit_over_leadin_clears_only_spanned_sections(graph, routes)
    assert not overdips, "\n".join(v.message() for v in overdips)
