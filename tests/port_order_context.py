"""A minimal routing context for the port lane-order unit tests."""

from __future__ import annotations

from types import SimpleNamespace

from nf_metro.layout.constants import CURVE_RADIUS
from nf_metro.layout.routing.reserved_bands import ReservedCorridors
from nf_metro.parser.model import MetroGraph

OFFSET_STEP = 4.0


def port_order_ctx(
    graph: MetroGraph,
    station_offsets: dict[tuple[str, str], float],
    positive_fan: set[str] | None = None,
) -> SimpleNamespace:
    """The context fields the band and gap-bundle order passes read."""
    return SimpleNamespace(
        graph=graph,
        station_offsets=station_offsets,
        positive_fan=positive_fan or set(),
        offset_step=OFFSET_STEP,
        curve_radius=CURVE_RADIUS,
        reserved_bands=ReservedCorridors(),
    )
