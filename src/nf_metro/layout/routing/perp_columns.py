"""Feeders of a perpendicular entry port.

A TOP/BOTTOM entry port is crossed by the feeders that land on it; this
names the perpendicular exit continuing a corridor run at the port's own
coordinate.
"""

from __future__ import annotations

from nf_metro.layout.constants import COORD_TOLERANCE
from nf_metro.parser.model import MetroGraph, Port, PortSide


def perp_corridor_feeder(graph: MetroGraph, port_id: str) -> Port | None:
    """The perpendicular exit port feeding entry *port_id* over the corridor.

    A TOP/BOTTOM exit port on a same-row section rises into the inter-row
    corridor band and runs across to feed a perpendicular entry (the
    up-and-over shape); the exit and entry ports then share the corridor Y.
    The drop out of that entry must continue the corridor's per-line descent
    order, so it needs the feeding exit port's side.

    A perpendicular exit that drops across rows into the entry below (the
    LR -> TB top-drop) is *not* collinear with the entry and keeps its own
    drop convention, so it is excluded by the shared-Y test.

    This mirrors the exemption in ``_guard_perp_entry_feed_not_collinear``:
    that guard permits exactly the collinear perp-exit feed this routes.
    """
    entry_y = graph.stations[port_id].y
    for feed in graph.edges_to(port_id):
        port = graph.ports.get(feed.source)
        if (
            port is not None
            and not port.is_entry
            and port.side in (PortSide.TOP, PortSide.BOTTOM)
            and abs(graph.station_for_edge_source(feed).y - entry_y) <= COORD_TOLERANCE
        ):
            return port
    return None
