"""Flow-start side entries re-dealt by descent corridor, measured as drawn.

Re-dealing a side entry's descending feeders by descent corridor nests their
turn-ins at the port, but the lanes it deals are carried on through the
consumer section.  The re-deal must not buy a crossing-free entry with
crossings inside that section, and the lanes it moves must not reach stations
the moved lines never travel to from the port.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Mapping
from pathlib import Path

import pytest

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.layout.labels import label_glyph_ink_bbox
from nf_metro.parser.model import MetroGraph
from nf_metro.render.plan import RenderPlan
from nf_metro.render.validate import parse_station_labels
from nf_metro.themes import resolve_theme

TOPOLOGIES = Path(__file__).resolve().parent.parent / "examples" / "topologies"
SAME_DESTINATION = TOPOLOGIES / "same_destination_vertical_convergence.mmd"

Box = tuple[float, float, float, float]

_MARKER = re.compile(r'<(rect|circle)\b([^>]*class="nf-metro-station"[^>]*)/?>')


def _render(
    path: Path, layout_options: Mapping[str, object] | None
) -> tuple[MetroGraph, RenderPlan, str]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(
            path.read_text(),
            source_dir=str(path.parent),
            layout_options=layout_options,
        )
        result = render_graph_result(
            graph, resolve_theme(graph, "light"), RenderConfig()
        )
    return graph, result.plan, result.content


def _attr(attrs: str, name: str) -> float:
    match = re.search(rf'\b{name}="([^"]*)"', attrs)
    assert match is not None, name
    return float(match.group(1))


def _drawn_markers(svg: str) -> dict[str, Box]:
    boxes: dict[str, list[Box]] = {}
    for tag, attrs in _MARKER.findall(svg):
        sid = re.search(r'data-station-id="([^"]*)"', attrs)
        assert sid is not None
        half_stroke = _attr(attrs, "stroke-width") / 2
        if tag == "rect":
            x, y = _attr(attrs, "x"), _attr(attrs, "y")
            w, h = _attr(attrs, "width"), _attr(attrs, "height")
            box = (x, y, x + w, y + h)
        else:
            cx, cy, r = _attr(attrs, "cx"), _attr(attrs, "cy"), _attr(attrs, "r")
            box = (cx - r, cy - r, cx + r, cy + r)
        grown = (
            box[0] - half_stroke,
            box[1] - half_stroke,
            box[2] + half_stroke,
            box[3] + half_stroke,
        )
        boxes.setdefault(sid.group(1), []).append(grown)
    return {
        sid: (
            min(b[0] for b in parts),
            min(b[1] for b in parts),
            max(b[2] for b in parts),
            max(b[3] for b in parts),
        )
        for sid, parts in boxes.items()
    }


def _overlaps(a: Box, b: Box) -> bool:
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


@pytest.mark.parametrize(
    "layout_options",
    [None, {"stroke_scale": 1.5, "fold_threshold": 4, "section_y_gap": 20.0}],
    ids=["default", "wide_stroke_folded"],
)
def test_descent_reorder_leaves_downstream_labels_clear(
    layout_options: Mapping[str, object] | None,
) -> None:
    """``s7``'s entry re-deal moves ``upper`` at Sink input, where that stroke
    ends.  ``upper``'s later stroke out of Sink output into Return target keeps
    its own lane, so Return target input's pill stays clear of the Return target
    output name drawn above its neighbour."""
    graph, _plan, svg = _render(SAME_DESTINATION, layout_options)
    markers = _drawn_markers(svg)
    assert {
        sid
        for sid, station in graph.stations.items()
        if not station.is_port and not station.is_hidden
    } <= markers.keys()
    struck = [
        (label.placement.station_id, sid)
        for label in parse_station_labels(svg)
        for sid, box in markers.items()
        if sid != label.placement.station_id
        and _overlaps(label_glyph_ink_bbox(label.placement), box)
    ]
    assert struck == []
