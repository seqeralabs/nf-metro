"""Flow-start side entries re-dealt by descent corridor, measured as drawn.

Re-dealing a side entry's descending feeders by descent corridor nests their
turn-ins at the port, but the lanes it deals are carried on through the
consumer section.  The re-deal must not buy a crossing-free entry with
crossings inside that section, and the lanes it moves must not reach stations
the moved lines never travel to from the port.
"""

from __future__ import annotations

import itertools
import re
import warnings
from collections.abc import Mapping
from pathlib import Path

import pytest

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.layout.labels import label_glyph_ink_bbox
from nf_metro.layout.routing import offsets
from nf_metro.layout.routing.common import segments_properly_cross
from nf_metro.parser.model import MetroGraph
from nf_metro.render.plan import RenderPlan
from nf_metro.render.validate import parse_station_labels
from nf_metro.themes import resolve_theme

TOPOLOGIES = Path(__file__).resolve().parent.parent / "examples" / "topologies"
INNER_BYPASS = TOPOLOGIES / "descent_entry_inner_bypass.mmd"
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
            graph, resolve_theme(None, graph, "light"), RenderConfig()
        )
    return graph, result.plan, result.content


def _line_crossings_inside(plan: RenderPlan, box: Box) -> list[tuple[str, str]]:
    x0, y0, x1, y1 = box
    found = []
    polylines = list(zip(plan.routes, plan.route_polylines, strict=True))
    for (ra, pa), (rb, pb) in itertools.combinations(polylines, 2):
        if ra.line_id == rb.line_id:
            continue
        for a, b in itertools.pairwise(pa):
            for c, d in itertools.pairwise(pb):
                point = segments_properly_cross(
                    a, b, c, d, reject_collinear_touch=False
                )
                if point is not None and x0 <= point[0] <= x1 and y0 <= point[1] <= y1:
                    found.append((ra.line_id, rb.line_id))
    return found


@pytest.mark.parametrize(
    "layout_options",
    [
        None,
        {"section_x_gap": 80.0},
        {"x_spacing": 80.0},
        {"line_order": "span", "center_ports": True},
    ],
    ids=["default", "section_x_gap", "x_spacing", "span_centred"],
)
def test_descent_reorder_keeps_bypassing_line_on_its_side(
    layout_options: Mapping[str, object] | None,
) -> None:
    """``merge``'s RIGHT entry is fed from above by ``trim`` (nearer descent)
    and ``annotate``; inside the section ``trim`` bows under ``qc`` while
    ``annotate`` stops there.  Lifting ``trim`` onto the top lane would nest the
    two turn-ins but send it back down across ``annotate`` on both sides of the
    bow."""
    graph, plan, _svg = _render(INNER_BYPASS, layout_options)
    section = graph.sections["merge"]
    box = (
        section.bbox_x,
        section.bbox_y,
        section.bbox_x + section.bbox_w,
        section.bbox_y + section.bbox_h,
    )
    assert _line_crossings_inside(plan, box) == []


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


def test_split_check_flags_only_an_order_reversed_at_its_part(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At ``merge``'s entry the held lanes part cleanly at ``combine``: keeping
    them is never an inversion, and once ``trim``'s bypass bow is placed,
    swapping ``annotate`` and ``trim`` is."""
    verdicts: list[tuple[bool, bool]] = []
    real = offsets._order_flow_start_side_entry_by_descent_corridor

    def judge(ctx) -> None:
        real(ctx)
        port_id = next(
            pid
            for pid, port in ctx.graph.ports.items()
            if port.section_id == "merge" and port.is_entry
        )
        held = {lid: ctx.offsets[(port_id, lid)] for lid in ("annotate", "trim")}
        swapped = {"annotate": held["trim"], "trim": held["annotate"]}

        def inverts(new_offs: dict[str, float]) -> bool:
            walk = offsets._bundle_walk(ctx, port_id, "merge", new_offs)
            return offsets._bundle_reslot_inverts_split(ctx, port_id, new_offs, walk)

        verdicts.append((inverts(held), inverts(swapped)))

    monkeypatch.setattr(
        offsets, "_order_flow_start_side_entry_by_descent_corridor", judge
    )
    _render(INNER_BYPASS, None)
    assert not any(kept for kept, _swapped in verdicts)
    assert (False, True) in verdicts
