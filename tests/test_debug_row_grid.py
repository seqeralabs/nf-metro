"""The debug row grid must mark placement-row anchors, not an assumed pitch (#589).

A station is positioned by ``station.y`` -- the row anchor (the offset-0 slot,
the top of its line bundle), not the centre of its rendered pill, which is
offset downward by the bundle mid.  The ``--debug`` row grid exists to show
where the engine placed each row, so every line must sit on a real ``station.y``
and every occupied row must have one.  Drawing an inferred uniform pitch instead
let lines drift off the anchors -- missing real rows and adding phantom lines
where no station sits.  These tests pin each line to an actual anchor.
"""

import re
from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.constants import DEBUG_ROW_GRID_COLOR
from nf_metro.render.svg import compute_station_offsets, render_svg, station_marker_box
from nf_metro.themes import NFCORE_THEME

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"

# Fixtures whose rows are not on a single uniform pitch, so an inferred-pitch
# grid drifts off the anchors -- the condition that motivated #589.
# bypass_label_rake_wide pins a row anchored only by a hidden bypass junction:
# its line must survive (an inferred pitch dropped that lowest row).
GRID_FIXTURES = [
    "differentialabundance.mmd",
    "differentialabundance_default.mmd",
    "diagonal_labels.mmd",
    "sarek_metro.mmd",
    "topologies/bypass_label_rake_wide.mmd",
]

EPS = 0.5


def _laid_out(name: str):
    graph = parse_metro_mermaid((EXAMPLES_DIR / name).read_text())
    compute_layout(graph)
    return graph


def _row_grid_line_ys(svg: str) -> list[float]:
    """Y of every horizontal debug row-grid line (drawsvg emits Line as path)."""
    rgb = DEBUG_ROW_GRID_COLOR.split("(")[1].rsplit(",", 1)[0]
    ys: list[float] = []
    for d, stroke in re.findall(r'<path d="([^"]+)" stroke="([^"]+)"', svg):
        if rgb not in stroke:
            continue
        m = re.fullmatch(r"M[\d.]+,([\d.]+) L[\d.]+,([\d.]+)", d)
        if m and abs(float(m.group(1)) - float(m.group(2))) < EPS:
            ys.append(float(m.group(1)))
    return ys


def _anchor_ys(graph) -> set[float]:
    return {round(st.y, 1) for st in graph.stations.values() if not st.is_port}


@pytest.mark.parametrize("fixture", GRID_FIXTURES)
def test_debug_row_grid_marks_placement_anchors(fixture):
    """Every occupied row has a grid line on its anchor, and no grid line sits
    where no station is placed."""
    graph = _laid_out(fixture)
    svg = render_svg(graph, NFCORE_THEME, debug=True, chrome_css=False)
    grid_ys = _row_grid_line_ys(svg)
    assert grid_ys, "debug render drew no row-grid lines"
    anchors = _anchor_ys(graph)

    for a in anchors:
        assert any(abs(a - g) <= EPS for g in grid_ys), (
            f"{fixture}: row anchor y={a} has no grid line; "
            f"lines={sorted(set(grid_ys))}"
        )
    for g in grid_ys:
        assert any(abs(a - g) <= EPS for a in anchors), (
            f"{fixture}: grid line y={g} sits at no station anchor {sorted(anchors)}"
        )


def test_debug_grid_sits_at_anchor_not_pill_centre():
    """The headline #589 case: the nine bundled qc_report stations share one
    anchor line at station.y, and their pills hang below it (centre != anchor)."""
    graph = _laid_out("rnaseq_sections_manual.mmd")
    svg = render_svg(graph, NFCORE_THEME, debug=True, chrome_css=False)
    grid_ys = _row_grid_line_ys(svg)
    offsets = compute_station_offsets(graph)

    qc = [
        st
        for st in graph.stations.values()
        if st.section_id == "qc_report" and not st.is_port and not st.is_hidden
    ]
    trunk_y = min(st.y for st in qc)
    trunk = [st for st in qc if abs(st.y - trunk_y) < EPS]
    assert len(trunk) > 1, "expected a bundled trunk row in qc_report"

    grid_at = [g for g in grid_ys if abs(g - trunk_y) <= EPS]
    assert grid_at, f"no grid line at the qc_report anchor y={trunk_y}"

    for st in trunk:
        _cx, cy, _w, _h, _r = station_marker_box(graph, NFCORE_THEME, st, offsets)
        # The bundle offset puts the drawn pill centre below its anchor line.
        assert cy > trunk_y + EPS
        assert not any(abs(cy - g) <= EPS for g in grid_at)
