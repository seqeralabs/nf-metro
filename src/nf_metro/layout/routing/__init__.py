"""Edge routing subpackage for metro map layout.

Public API:
- route_edges: Main edge routing dispatcher (placement-pure)
- route_edges_centred: route_edges + applied bubble-centring marker moves
- RoutedPath: Routed path dataclass
- OffsetRegime: Which line-separation regime a route is in
- apply_route_offsets: A route's final render geometry, separation applied
- GapSlot: Symbolic gap-relative slot for a vertical channel run
- compute_station_offsets: Per-station Y offset computation
"""

from nf_metro.layout.routing.common import (
    GapSlot,
    OffsetRegime,
    RoutedPath,
    apply_route_offsets,
)
from nf_metro.layout.routing.core import route_edges, route_edges_centred
from nf_metro.layout.routing.offsets import compute_station_offsets

__all__ = [
    "GapSlot",
    "OffsetRegime",
    "RoutedPath",
    "apply_route_offsets",
    "compute_station_offsets",
    "route_edges",
    "route_edges_centred",
]
