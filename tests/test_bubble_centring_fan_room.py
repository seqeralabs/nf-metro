"""Bubble centring leaves every fanned diagonal its stagger room in all phases."""

from __future__ import annotations

import pytest

from nf_metro.layout.phases.fan_stagger import graph_fan_stagger_needs
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.postprocess import _center_bubble_stations
from nf_metro.parser.model import Edge, MetroGraph, Section, Station

_FAN = ("l1", "l2")


class _Map:
    """A one-section LR graph plus the routes bubble centring reads."""

    def __init__(self) -> None:
        self.graph = MetroGraph()
        self.graph.add_section(Section(id="s", name="s", direction="LR"))
        self.routes: list[RoutedPath] = []

    def station(self, sid: str, x: float, y: float, *, port: bool = False) -> None:
        self.graph.add_station(
            Station(id=sid, label=sid, x=x, y=y, section_id="s", is_port=port)
        )
        self.graph.sections["s"].station_ids.append(sid)

    def flat(self, source: str, target: str, lines=_FAN) -> None:
        self._route(source, target, lines, ())

    def diagonal(
        self, source: str, target: str, corners: tuple[float, float], lines=_FAN
    ) -> None:
        self._route(source, target, lines, corners)

    def _route(self, source, target, lines, corners) -> None:
        src, tgt = self.graph.stations[source], self.graph.stations[target]
        bends = [(corners[0], src.y), (corners[1], tgt.y)] if corners else []
        for line in lines:
            edge = Edge(source=source, target=target, line_id=line)
            self.graph.add_edge(edge)
            points = [(src.x, src.y), *bends, (tgt.x, tgt.y)]
            self.routes.append(RoutedPath(edge=edge, line_id=line, points=points))

    def centre(self) -> tuple[dict[str, float], dict[tuple[str, str], float]]:
        need = graph_fan_stagger_needs(self.graph)
        moves = _center_bubble_stations(self.routes, self.graph)
        settled = {sid: moves.get(sid, st.x) for sid, st in self.graph.stations.items()}
        return settled, need


def _fan_need() -> float:
    probe = _Map()
    probe.station("a", 0.0, 0.0)
    probe.station("b", 500.0, 40.0)
    probe.diagonal("a", "b", (200.0, 300.0))
    return graph_fan_stagger_needs(probe.graph)[("a", "b")]


def _assert_room(settled, need) -> None:
    for (source, target), room in need.items():
        assert settled[target] - settled[source] >= room - 1e-6, (source, target)


def test_both_ends_of_a_fanned_diagonal_moving_keep_its_room():
    """``a`` and ``b`` each centre toward the other, and either move alone
    leaves ``a -> b`` its room, but the two together would not."""
    n = _fan_need()
    span = 1.75 * n
    m = _Map()
    m.station("p", 0.0, 0.0, port=True)
    m.station("a", 5.0, 0.0)
    m.station("b", span - 5.0, 40.0)
    m.station("q", span, 40.0, port=True)
    m.flat("p", "a")
    m.diagonal("a", "b", (span / 2 - 1.0, span / 2 + 1.0))
    m.flat("b", "q")

    settled, need = m.centre()

    assert settled["a"] > 5.0 and settled["b"] < span - 5.0
    _assert_room(settled, need)


def test_sibling_alignment_keeps_a_fanned_diagonals_room():
    """``d`` shares a column with ``c1``-``c3`` but already sits as close to
    ``e`` as the fanned ``d -> e`` allows; aligning the column on ``c1`` and
    ``c2`` must not drag it closer."""
    n = _fan_need()
    m = _Map()
    for i, (sid, corner) in enumerate((("c1", 200.0), ("c2", 200.0), ("c3", 240.0))):
        y = 100.0 * i
        m.station(f"p{i}", 60.0, y, port=True)
        m.station(sid, 100.0, y)
        m.station(f"q{i}", 300.0, y + 20.0, port=True)
        m.flat(f"p{i}", sid, ("x",))
        m.diagonal(sid, f"q{i}", (corner, corner + 20.0), ("x",))
    e_x = 100.0 + n
    m.station("pd", 80.0, 300.0, port=True)
    m.station("d", 100.0, 300.0)
    m.station("e", e_x, 340.0)
    m.station("f", e_x + 60.0, 340.0)
    m.flat("pd", "d")
    m.diagonal("d", "e", (e_x - 12.0, e_x - 2.0))
    m.flat("e", "f")

    settled, need = m.centre()

    assert settled["c3"] == pytest.approx(settled["c1"])
    _assert_room(settled, need)
