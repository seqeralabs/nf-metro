"""Stage 1.2 lengthens fanned diagonals the same way in every flow direction."""

from __future__ import annotations

import pytest

from nf_metro.layout.geometry import AxisFrame
from nf_metro.layout.phases.single_section import reserve_fan_stagger_runway
from nf_metro.parser.model import Edge, MetroGraph, Section, Station

_GRID = 40.0

# (flow, cross) positions in a forward frame.  A two-lane bundle from ``src``
# to ``tgt`` passes ``mid``'s layer, far too short for its stagger.
_PASSING = {"src": (0.0, 0.0), "mid": (10.0, 0.0), "tgt": (20.0, 40.0)}
_PASSING_EDGES = (("src", "mid", "a"), ("src", "tgt", "b"), ("src", "tgt", "c"))

# A second short bundle leaves ``mid`` for ``far``, one layer beyond ``tgt``.
_CHAINED = {**_PASSING, "far": (30.0, -40.0)}
_CHAINED_EDGES = (*_PASSING_EDGES, ("mid", "far", "d"), ("mid", "far", "e"))


def _reserve(direction: str, layout, edges) -> dict[str, float]:
    """Flow distance of every station from ``src`` after the reservation."""
    flow, cross = AxisFrame.axes_for_direction(direction)
    sign = AxisFrame.flow_sign(direction)
    span = max(f for f, _ in layout.values())
    sub = MetroGraph()
    for sid, (f, c) in layout.items():
        station = Station(id=sid, label=sid, section_id="s")
        setattr(station, flow, f if sign > 0 else span - f)
        setattr(station, cross, c + 100.0)
        sub.add_station(station)
    for source, target, line in edges:
        sub.add_edge(Edge(source=source, target=target, line_id=line))
    section = Section(id="s", name="s", direction=direction)
    reserve_fan_stagger_runway(sub, sub, section, y_grid=_GRID)
    origin = getattr(sub.stations["src"], flow)
    return {
        sid: sign * (getattr(st, flow) - origin) for sid, st in sub.stations.items()
    }


@pytest.mark.parametrize(("forward", "reversed_"), [("LR", "RL"), ("TB", "BT")])
@pytest.mark.parametrize(
    ("layout", "edges"),
    [(_PASSING, _PASSING_EDGES), (_CHAINED, _CHAINED_EDGES)],
    ids=["passing", "chained"],
)
def test_reversed_flow_reserves_the_mirror_image(forward, reversed_, layout, edges):
    assert _reserve(reversed_, layout, edges) == pytest.approx(
        _reserve(forward, layout, edges)
    )


@pytest.mark.parametrize("direction", ["LR", "RL", "TB", "BT"])
def test_opening_lands_at_the_downstream_end(direction):
    """A station the bundle passes keeps its place."""
    placed = _reserve(direction, _PASSING, _PASSING_EDGES)
    assert placed["mid"] == pytest.approx(10.0)
    assert placed["tgt"] > _PASSING["tgt"][0]


@pytest.mark.parametrize("direction", ["LR", "RL", "TB", "BT"])
def test_each_bundle_opens_after_the_openings_upstream_of_it(direction):
    """``tgt``'s opening already lengthens ``mid -> far``, so ``far`` keeps
    its distance from ``tgt``."""
    placed = _reserve(direction, _CHAINED, _CHAINED_EDGES)
    assert placed["far"] - placed["tgt"] == pytest.approx(10.0)


@pytest.mark.parametrize("direction", ["TB", "BT"])
def test_vertical_flow_opens_whole_grid_steps(direction):
    """Stage 6.4 snaps Y to the grid, so every opening is a multiple of it."""
    placed = _reserve(direction, _CHAINED, _CHAINED_EDGES)
    for sid, (f, _c) in _CHAINED.items():
        assert (placed[sid] - f) % _GRID == pytest.approx(0.0, abs=1e-6)


def test_vertical_flow_requires_the_y_grid():
    sub = MetroGraph()
    with pytest.raises(AssertionError):
        reserve_fan_stagger_runway(sub, sub, Section(id="s", name="s", direction="TB"))


@pytest.mark.parametrize("direction", ["LR", "RL"])
def test_opening_is_exactly_the_shortfall(direction):
    """A short bundle ends at exactly the length it needs, no further."""
    from nf_metro.layout.phases.fan_stagger import fan_stagger_needs

    flow, cross = AxisFrame.axes_for_direction(direction)
    sub = MetroGraph()
    for sid, (f, c) in _PASSING.items():
        station = Station(id=sid, label=sid, section_id="s")
        setattr(station, flow, f)
        setattr(station, cross, c + 100.0)
        sub.add_station(station)
    for source, target, line in _PASSING_EDGES:
        sub.add_edge(Edge(source=source, target=target, line_id=line))
    need = fan_stagger_needs(sub, sub.stations, direction, y_grid=_GRID)
    placed = _reserve(direction, _PASSING, _PASSING_EDGES)
    assert placed["tgt"] == pytest.approx(need[("src", "tgt")])
