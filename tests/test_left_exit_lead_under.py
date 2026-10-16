"""A LEFT exit's right-entry wrap leads out under its own section, or declines.

``perp_exit_single_feeder_landing`` exits Gather 2 through a LEFT port to a
RIGHT entry far to the right.  The wrap leads out leftward, drops below the box
and runs right under it to the turn column.  It declines when the port is not a
LEFT exit on its box's edge, when the turn column is not past the box, or when a
leg of the detour would cross another section.  The same exit side decides
whether a bottom-row edge may climb along its own row.
"""

from __future__ import annotations

import dataclasses
import warnings
from pathlib import Path

import pytest

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.layout.constants import COORD_TOLERANCE
from nf_metro.layout.routing import inter_section_handlers as handlers
from nf_metro.layout.routing.common import Direction
from nf_metro.parser.model import PortSide
from nf_metro.themes import resolve_theme

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "regressions"
    / "perp_exit_single_feeder_landing.mmd"
)
EXIT_PORT = "sec2__exit_left_2"
FAR_SECTION = "sec4"
BOTTOM_ROW = 1
UPPER_ROW = 0


def _boxes(graph) -> dict:
    return {
        sid: (s.bbox_x, s.bbox_y, s.bbox_w, s.bbox_h)
        for sid, s in graph.sections.items()
    }


@pytest.fixture(scope="module")
def landing():
    """The facts and arguments of the final lead-under query, as it saw them.

    Layout moves boxes and ports after the query, so the geometry the query
    read is put back before the tests probe it.
    """
    calls = []
    real = handlers._left_exit_lead_under

    def spy(f, pos_n, corner_x, channel_y):
        if f.edge.source == EXIT_PORT:
            calls.append((f, pos_n, corner_x, channel_y, _boxes(f.graph), f.src.x))
        return real(f, pos_n, corner_x, channel_y)

    with pytest.MonkeyPatch.context() as patch, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        patch.setattr(handlers, "_left_exit_lead_under", spy)
        graph = prepare_graph(FIXTURE.read_text(), source_dir=str(FIXTURE.parent))
        render_graph_result(
            graph, resolve_theme(None, graph, mode=None), RenderConfig()
        )
    f, pos_n, corner_x, channel_y, boxes, src_x = calls[-1]
    for sid, (x, y, w, h) in boxes.items():
        section = f.graph.sections[sid]
        section.bbox_x, section.bbox_y, section.bbox_w, section.bbox_h = x, y, w, h
    seated = dataclasses.replace(f, src=dataclasses.replace(f.src, x=src_x))
    return seated.graph, (seated, pos_n, corner_x, channel_y)


def _box(graph, facts):
    section = graph.sections[facts.src.section_id]
    return section, section.bbox_x + section.bbox_w, section.bbox_y + section.bbox_h


def test_lead_under_leaves_leftward_and_runs_below_the_box(landing) -> None:
    graph, (f, pos_n, corner_x, channel_y) = landing
    section, _right, bottom = _box(graph, f)
    lead = handlers._left_exit_lead_under(f, pos_n, corner_x, channel_y)
    assert lead is not None
    lx, uy = lead
    assert lx < f.src.x
    assert uy > bottom


def test_turn_column_must_be_past_the_box(landing) -> None:
    graph, (f, pos_n, _corner_x, _channel_y) = landing
    section, right, _bottom = _box(graph, f)
    # A channel inside the source row keeps the third leg clear of every section.
    channel_y = section.bbox_y + section.bbox_h / 2
    assert handlers._left_exit_lead_under(f, pos_n, right, channel_y) is not None
    assert handlers._left_exit_lead_under(f, pos_n, right - 1.0, channel_y) is None


def test_a_leg_through_another_section_declines(landing) -> None:
    graph, (f, pos_n, _corner_x, _channel_y) = landing
    far = graph.sections[FAR_SECTION]
    column = far.bbox_x + far.bbox_w / 2
    through = far.bbox_y + far.bbox_h / 2
    clear_of = far.bbox_y + far.bbox_h + 10.0
    assert handlers._left_exit_lead_under(f, pos_n, column, through) is None
    assert handlers._left_exit_lead_under(f, pos_n, column, clear_of) is not None


def test_only_a_left_exit_leads_under(landing) -> None:
    graph, (f, pos_n, corner_x, channel_y) = landing
    for side in (PortSide.RIGHT, PortSide.TOP, PortSide.BOTTOM):
        moved = dataclasses.replace(
            f, src_port=dataclasses.replace(f.src_port, side=side)
        )
        assert handlers._left_exit_lead_under(moved, pos_n, corner_x, channel_y) is None


def test_the_exit_must_sit_on_the_boxes_left_edge(landing) -> None:
    graph, (f, pos_n, corner_x, channel_y) = landing
    section, _right, _bottom = _box(graph, f)
    on_edge = dataclasses.replace(f.src, x=section.bbox_x + COORD_TOLERANCE)
    inside = dataclasses.replace(f.src, x=section.bbox_x + COORD_TOLERANCE + 0.5)
    seated = dataclasses.replace(f, src=on_edge)
    interior = dataclasses.replace(f, src=inside)
    assert handlers._left_exit_lead_under(seated, pos_n, corner_x, channel_y)
    assert handlers._left_exit_lead_under(interior, pos_n, corner_x, channel_y) is None


def test_the_return_leg_is_declared_from_the_underpass(landing) -> None:
    """A channel level with the box leaves the return leg rising from below it."""
    graph, (f, pos_n, corner_x, channel_y) = landing
    section, _right, bottom = _box(graph, f)
    level = (f.src.y + bottom) / 2
    route = handlers._build_right_entry_wrap_route(f, level, normalize_exempt=False)
    assert [slot.direction for slot in route.gap_slots] == [Direction.U]


EXIT_FACES_AWAY = [
    (PortSide.LEFT, 0, 2, True),
    (PortSide.LEFT, 3, 1, False),
    (PortSide.RIGHT, 0, 2, False),
    (PortSide.RIGHT, 3, 1, True),
    (PortSide.LEFT, 2, 2, False),
    (PortSide.RIGHT, 2, 2, False),
    (PortSide.BOTTOM, 0, 2, False),
    (PortSide.BOTTOM, 3, 1, False),
    (PortSide.TOP, 3, 1, False),
]


@pytest.mark.parametrize(
    ("side", "src_col", "tgt_col", "faces_away"),
    EXIT_FACES_AWAY,
    ids=lambda v: v.name if isinstance(v, PortSide) else str(v),
)
def test_a_row_climb_never_leaves_through_an_exit_facing_away(
    landing, monkeypatch, side, src_col, tgt_col, faces_away
) -> None:
    graph, (f, *_rest) = landing
    port = graph.ports[EXIT_PORT]
    monkeypatch.setitem(graph.ports, EXIT_PORT, dataclasses.replace(port, side=side))
    assert handlers._bottom_row_climb_corridor_clear(
        graph, BOTTOM_ROW, UPPER_ROW, src_col, tgt_col
    )
    climbs = handlers._is_row_level_bottom_row_climb(
        graph, f.edge, BOTTOM_ROW, UPPER_ROW, src_col, tgt_col
    )
    assert climbs is not faces_away


def test_a_row_climb_from_a_station_has_no_exit_side_to_face_away(landing) -> None:
    graph, (f, *_rest) = landing
    from_station = dataclasses.replace(f.edge, source="s2n4")
    assert handlers._is_row_level_bottom_row_climb(
        graph, from_station, BOTTOM_ROW, UPPER_ROW, 0, 2
    )
