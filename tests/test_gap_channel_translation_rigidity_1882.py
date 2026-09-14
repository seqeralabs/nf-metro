"""Inter-section gap channels anchor to real geometry, never the origin (#1882).

Routing is a pure function of the map's relative geometry: translating every
laid-out section, station and port by a fixed vector, then re-routing, must move
every routed point by that same vector.  A channel that anchors partly against
the coordinate origin instead of a real section edge breaks this -- a whole-map
translation of ``D`` shifts such a channel by only ``D / 2``.

``fan_bypass_shared_band`` exposes it: the ``long`` line's target-side (gap2)
channel routes into a column that is present on the grid but absent from the row
it descends through, so ``col_right_edge`` there defaulted to the origin and the
channel centred against literal 0.  The other fixtures carry deep inter-section
bypasses whose channels already anchor to real edges; they guard against a
regression that would re-introduce an origin-anchored channel elsewhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
TOPOLOGIES = EXAMPLES / "topologies"

FIXTURES = [
    TOPOLOGIES / "fan_bypass_shared_band.mmd",
    TOPOLOGIES / "funcprofiler_upstream.mmd",
    TOPOLOGIES / "bypass_gap2_rightward_overflow.mmd",
    TOPOLOGIES / "upward_bypass.mmd",
    TOPOLOGIES / "bypass_leftward_overflow.mmd",
    EXAMPLES / "differentialabundance.mmd",
]

TRANSLATION = 137.0


def _translate(graph: MetroGraph, delta: float) -> None:
    """Shift every laid-out section, station and port by ``delta`` on both axes."""
    for section in graph.sections.values():
        section.bbox_x += delta
        section.bbox_y += delta
    for station in graph.stations.values():
        station.x += delta
        station.y += delta
    for port in graph.ports.values():
        port.x += delta
        port.y += delta


def _routes_by_key(
    graph: MetroGraph,
) -> dict[tuple[str, str, str], list[tuple[float, float]]]:
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return {(r.line_id, r.edge.source, r.edge.target): list(r.points) for r in routes}


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_gap_channels_translate_rigidly(path: Path) -> None:
    base_graph = parse_metro_mermaid(path.read_text())
    base_graph.source_dir = str(path.parent)
    compute_layout(base_graph)
    base = _routes_by_key(base_graph)

    shifted_graph = parse_metro_mermaid(path.read_text())
    shifted_graph.source_dir = str(path.parent)
    compute_layout(shifted_graph)
    _translate(shifted_graph, TRANSLATION)
    shifted = _routes_by_key(shifted_graph)

    assert set(base) == set(shifted), (
        f"{path.stem}: route set changed under translation"
    )

    worst = 0.0
    for key, base_points in base.items():
        shifted_points = shifted[key]
        assert len(base_points) == len(shifted_points), (
            f"{path.stem}: {key} point count changed"
        )
        for (bx, by), (sx, sy) in zip(base_points, shifted_points):
            worst = max(
                worst, abs((sx - bx) - TRANSLATION), abs((sy - by) - TRANSLATION)
            )

    assert worst < 0.5, (
        f"{path.stem}: a routed point moved by other than the {TRANSLATION}px "
        f"whole-map translation (worst deviation {worst:.2f}px) -- a channel is "
        f"anchored against the coordinate origin instead of a real section edge"
    )
