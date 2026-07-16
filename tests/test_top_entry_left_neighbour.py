"""TOP entry fed from a left-neighbour section (same row, adjacent column).

When a section carries a ``entry: top`` port but is fed by a section to its
LEFT in the same grid row, the connector must approach the port from ABOVE the
boundary: rise in the inter-section gap to just above the target's top edge,
run across to the port's column, then drop straight in.

Two defects this locks against:

* **Port stranded inside the bbox.** Growing the target's bbox upward to align
  row-mate tops (Stage 5.3 ``_top_align_row_bboxes_only``) must carry the TOP
  port to the new top edge, not leave it a row-pitch inside -- an interior
  entry that trips ``_guard_ports_on_boundaries``.
* **Up-leg overshoot.** The rise clears only the target's own header badge, not
  the full inter-row header band, so the vertical leg does not shoot far past
  the port before turning back down.

See issue #1485.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import (
    CURVE_RADIUS,
    INTER_ROW_HEADER_CLEARANCE,
)
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = "examples/topologies/top_entry_left_neighbour.mmd"

# A clean drop-in clears only the target's own header badge; rising a full
# inter-row header band above the top edge is the overshoot into the shared
# channel.  The bound is that full band.
_MAX_RISE_ABOVE_TOP = INTER_ROW_HEADER_CLEARANCE


def _layout(*, validate: bool):
    graph = parse_metro_mermaid((REPO_ROOT / FIXTURE).read_text())
    compute_layout(graph, validate=validate)
    return graph


def _top_entry_route(graph, routes):
    for rp in routes:
        port = graph.ports.get(rp.edge.target)
        if (
            rp.is_inter_section
            and port is not None
            and port.is_entry
            and port.side is not None
            and port.side.name == "TOP"
        ):
            return rp
    raise AssertionError(f"{FIXTURE}: no TOP-entry inter-section route found")


def test_top_entry_port_sits_on_top_edge() -> None:
    """The TOP entry port must lie on its section's top edge, not inside it."""
    graph = _layout(validate=True)
    port_st = next(
        graph.stations[pid]
        for pid, p in graph.ports.items()
        if p.is_entry and p.side is not None and p.side.name == "TOP"
    )
    sec = graph.sections[port_st.section_id]
    assert port_st.y == pytest.approx(sec.bbox_y, abs=1.0), (
        f"{FIXTURE}: TOP entry port y={port_st.y:.1f} is not on the section "
        f"top edge y={sec.bbox_y:.1f} (interior entry)"
    )


def test_top_entry_left_feed_approaches_from_above_without_overshoot() -> None:
    """The left-fed connector rises just above the boundary, then drops in."""
    graph = _layout(validate=True)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    rp = _top_entry_route(graph, routes)

    port_y = graph.stations[rp.edge.target].y
    apex = min(pt[1] for pt in rp.points)

    # Approaches from above: the up-leg rises above the port before dropping in.
    assert apex < port_y - CURVE_RADIUS, (
        f"{FIXTURE}: route apex y={apex:.1f} does not rise above the "
        f"TOP port y={port_y:.1f}"
    )
    # No overshoot: the rise stays within the target-header clearance rather
    # than shooting into the shared inter-row channel.
    rise = port_y - apex
    assert rise <= _MAX_RISE_ABOVE_TOP, (
        f"{FIXTURE}: up-leg rises {rise:.1f}px above the TOP port "
        f"(> {_MAX_RISE_ABOVE_TOP:.1f}px) -- overshoots into the inter-row channel"
    )
