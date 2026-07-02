"""Fork/join diamond branch label placement.

Column-parity alternation picks a label's side by column index alone, with
no awareness of diamond geometry: for a 2-way fork/join diamond it can
coincidentally point one or both branch labels at the other branch,
squeezing them inside the bubble between the two routes. The outside of a
two-way bubble usually reads better, but an on-trunk branch's outward side
shares its row with neighbouring trunk stations, so forcing it outward
unconditionally can shove a neighbour's label aside. The fix only flips a
branch outward when that side is free of collisions with every other
already-placed label, checked once the whole placement list has settled.
"""

from nf_metro.layout.constants import LABEL_OFFSET
from nf_metro.layout.labels import (
    _prefer_diamond_labels_outward,
    _try_place,
    place_labels,
)
from nf_metro.parser.model import Edge, MetroGraph, Section, Station

_SEC_ID = "pipe"


def _station(station_id, x, y, layer, label=None):
    return Station(
        id=station_id,
        label=label or station_id,
        section_id=_SEC_ID,
        x=x,
        y=y,
        layer=layer,
    )


def _build_graph(stations, edges):
    graph = MetroGraph()
    graph.sections[_SEC_ID] = Section(id=_SEC_ID, name="Pipeline", direction="LR")
    for station in stations:
        graph.stations[station.id] = station
        graph.sections[_SEC_ID].station_ids.append(station.id)
    graph.edges = list(edges)
    return graph


def _above(placements, station_id):
    for placement in placements:
        if placement.station_id == station_id:
            return placement.above
    raise AssertionError(f"no placement for {station_id!r}")


def test_flips_outward_when_nothing_else_is_there():
    on_trunk = _station("on_trunk", 190, 160, layer=0, label="Diamond Branch")
    off_trunk = _station("off_trunk", 190, 200, layer=0, label="Off Trunk")
    graph = _build_graph([on_trunk, off_trunk], [])
    placement = _try_place(on_trunk, LABEL_OFFSET, False, [])

    _prefer_diamond_labels_outward(
        [placement], graph, {"on_trunk": off_trunk}, None, LABEL_OFFSET
    )

    assert placement.above is True


def test_stays_put_when_outward_would_collide_with_a_neighbour():
    on_trunk = _station("on_trunk", 190, 160, layer=0, label="Very Long Branch Label")
    off_trunk = _station("off_trunk", 190, 200, layer=0, label="Off Trunk")
    neighbour = _station("neighbour", 60, 160, layer=1, label="Neighbour")
    graph = _build_graph([on_trunk, off_trunk, neighbour], [])

    on_trunk_placement = _try_place(on_trunk, LABEL_OFFSET, False, [])
    neighbour_placement = _try_place(neighbour, LABEL_OFFSET, True, [])
    placements = [on_trunk_placement, neighbour_placement]

    _prefer_diamond_labels_outward(
        placements, graph, {"on_trunk": off_trunk}, None, LABEL_OFFSET
    )

    assert on_trunk_placement.above is False
    assert neighbour_placement.x == 60
    assert neighbour_placement.above is True


def test_full_pipeline_prefers_outward_for_a_clean_diamond():
    stations = [
        _station("raw", 80, 160, layer=0, label="Raw Reads"),
        _station("trim_galore", 180.5, 160, layer=1, label="Trim Galore!"),
        _station("fastp", 180.5, 200, layer=1, label="fastp"),
        _station("align", 276.5, 160, layer=2, label="Align"),
    ]
    edges = [
        Edge(source="raw", target="trim_galore", line_id="a"),
        Edge(source="raw", target="fastp", line_id="b"),
        Edge(source="trim_galore", target="align", line_id="a"),
        Edge(source="fastp", target="align", line_id="b"),
    ]
    placements = place_labels(_build_graph(stations, edges))

    assert _above(placements, "trim_galore") is True
    assert _above(placements, "fastp") is False
