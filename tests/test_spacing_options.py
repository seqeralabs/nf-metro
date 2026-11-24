"""Section padding, label offset and diagonal run as map and theme options."""

from __future__ import annotations

import pytest

from nf_metro.api import prepare_graph
from nf_metro.layout.engine import compute_layout

_MAP = """\
%%metro line: a | A | #ff0000
%%metro line: b | B | #0000ff
graph LR
    subgraph s1 [One]
        x[X] -->|a,b| y[Y]
        y -->|a| z[Z]
        y -->|b| w[W]
    end
"""


def _laid(directive: str = "", theme: str | None = None):
    graph = prepare_graph(directive + _MAP, theme=theme)
    compute_layout(graph)
    return graph


def test_section_padding_grows_the_section_box():
    plain = _laid().sections["s1"]
    padded = _laid("%%metro section_y_padding: 80\n").sections["s1"]
    assert padded.bbox_h > plain.bbox_h


def test_diagonal_run_sets_the_transition_length():
    from nf_metro.render.svg import build_render_plan
    from nf_metro.themes import resolve_theme

    def runs(directive: str) -> list[float]:
        graph = prepare_graph(directive + _MAP)
        plan = build_render_plan(graph, resolve_theme(None, graph))
        return [
            abs(p1[0] - p0[0])
            for polyline in plan.route_polylines
            for p0, p1 in zip(polyline, polyline[1:])
            if abs(p1[0] - p0[0]) > 1 and abs(p1[1] - p0[1]) > 1
        ]

    assert runs("") == [pytest.approx(30.0)]
    assert runs("%%metro diagonal_run: 45\n") == [pytest.approx(45.0)]


def test_label_offset_moves_labels_off_their_stations():
    from nf_metro.api import render_string

    plain = render_string(_MAP)
    offset = render_string("%%metro label_offset: 25\n" + _MAP)
    assert plain != offset


@pytest.mark.parametrize(
    "option", ["label_offset", "diagonal_run", "section_x_padding"]
)
def test_a_theme_can_set_the_option(tmp_path, option):
    theme = tmp_path / "t.yaml"
    theme.write_text(f"name: t\nlayout:\n  {option}: 40\n")
    graph = prepare_graph(_MAP, theme=str(theme))
    assert getattr(graph, option) == 40
