"""A flow-side exit seated on its downstream bundle keeps that level run.

Stage 3.1 places an LR/RL exit port on the Y its bundle takes in the next
section.  Rounding that exit onto its own section's grid would break the run by
up to half a pitch, which is shorter than the two full-radius turns of an S, so
the Stage 6.4 snap leaves it out of the rounding and seats it on the bundle's
settled Y instead.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from nf_metro.api import prepare_graph
from nf_metro.layout import engine
from nf_metro.layout.phases import grid_snap
from nf_metro.layout.phases.canvas import translate_graph
from nf_metro.layout.section_placement import _find_downstream_bundle_y
from nf_metro.parser.model import MetroGraph

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "regressions"
    / "approach_order_coincident_corners.mmd"
)
EXIT = "z2__exit_right_2"
# Whole and fractional pitches of the 40 px row grid, so each move renumbers
# the slots and shifts the exit's own grid residue against its consumer's.
OFFSETS = (0.0, 40.0, -80.0, 13.0, 20.0, -7.5, 100 / 7)


def _pre_snap_graph() -> tuple[MetroGraph, float]:
    """The fixture's graph as it reached the Stage 6.4 snap."""
    calls: list[tuple[MetroGraph, float]] = []
    original = engine._snap_all_y_to_grid

    def record(graph: MetroGraph, y_spacing: float) -> None:
        calls.append(deepcopy((graph, y_spacing)))
        original(graph, y_spacing)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(engine, "_snap_all_y_to_grid", record)
        prepare_graph(FIXTURE.read_text(), source_dir=str(FIXTURE.parent), bare=True)
    return calls[-1]


def _bundle_y(graph: MetroGraph) -> float | None:
    section = graph.sections[graph.ports[EXIT].section_id]
    return _find_downstream_bundle_y(EXIT, section, graph)


@pytest.mark.parametrize("dy", OFFSETS)
def test_anchored_exit_takes_its_bundle_settled_y(dy: float) -> None:
    graph, y_spacing = _pre_snap_graph()
    assert EXIT in graph.downstream_anchored_exit_ids
    translate_graph(graph, 0.0, dy)
    assert grid_snap._exits_level_with_their_bundle(graph) == {EXIT}

    grid_snap._snap_all_y_to_grid(graph, y_spacing)

    assert graph.stations[EXIT].y == pytest.approx(_bundle_y(graph))
    assert graph.ports[EXIT].y == graph.stations[EXIT].y


def test_exit_off_its_bundle_is_rounded_onto_its_own_grid() -> None:
    graph, y_spacing = _pre_snap_graph()
    graph.stations[EXIT].y += 3.0
    graph.ports[EXIT].y += 3.0
    assert not grid_snap._exits_level_with_their_bundle(graph)
    section_ids = [graph.ports[EXIT].section_id]
    origin_info = grid_snap._group_grid_origin(graph, section_ids, y_spacing)
    assert origin_info is not None

    grid_snap._snap_all_y_to_grid(graph, y_spacing)

    residue = (graph.stations[EXIT].y - origin_info[0]) % y_spacing
    assert min(residue, y_spacing - residue) == pytest.approx(0.0, abs=1e-3)
