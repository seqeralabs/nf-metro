"""Layout/placement behaviour for opt-in diagonal station labels (#527)."""

from __future__ import annotations

import re
from pathlib import Path

from nf_metro.layout.constants import DIAGONAL_LABEL_OFFSET, LABEL_OFFSET
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.labels import find_label_overlaps, place_labels
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.svg import render_svg
from nf_metro.themes.nfcore import NFCORE_THEME

_EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "diagonal_labels.mmd"

_DENSE_TRUNK = """\
%%metro line: main | Main | #ff0000
graph LR
    subgraph s [Section]
        a[AlignmentStep]
        b[MarkDuplicates]
        c[BaseRecalibrator]
        d[NGSCheckmate]
        e[VariantCaller]
        a -->|main| b
        b -->|main| c
        c -->|main| d
        d -->|main| e
    end
"""


def _trunk_pitch(label_angle: float) -> float:
    """Mean adjacent-station X pitch for the dense trunk at a given angle."""
    text = (
        f"%%metro label_angle: {label_angle}\n{_DENSE_TRUNK}"
        if label_angle
        else _DENSE_TRUNK
    )
    graph = parse_metro_mermaid(text)
    compute_layout(graph, validate=True)
    xs = sorted(
        s.x
        for s in graph.stations.values()
        if not s.is_port and not s.is_hidden and s.label.strip()
    )
    deltas = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
    return sum(deltas) / len(deltas)


def test_angled_trunk_packs_tighter_than_horizontal():
    """Angled labels must let a dense trunk pack tighter horizontally (#527).

    The whole point of diagonal labels: their narrow horizontal footprint
    lets closely-spaced stations share one line, so the angled trunk's
    column pitch is strictly smaller than the horizontal-label equivalent.
    """
    horizontal = _trunk_pitch(0.0)
    angled = _trunk_pitch(45.0)
    assert angled < horizontal, (
        f"angled pitch {angled:.1f} not tighter than horizontal {horizontal:.1f}"
    )


def test_angled_label_offset_clears_pill():
    """Angled labels anchor below the pill with the extra diagonal drop (#3)."""
    graph = parse_metro_mermaid(f"%%metro label_angle: 45\n{_DENSE_TRUNK}")
    compute_layout(graph, validate=True)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    placements = place_labels(
        graph, station_offsets=offsets, routes=routes, label_angle=45.0
    )
    by_sid = {p.station_id: p for p in placements}
    for sid in ("a", "b", "c", "d", "e"):
        lp = by_sid[sid]
        st = graph.stations[sid]
        assert lp.angle == 45.0
        # Anchor sits at least LABEL_OFFSET + DIAGONAL_LABEL_OFFSET below the
        # marker centre, so there is a visible gap between pill and text.
        assert lp.y - st.y >= LABEL_OFFSET + DIAGONAL_LABEL_OFFSET - 0.5


def test_angled_labels_reserve_room_above_row_below():
    """A section below an angled-label trunk must clear the hanging labels (#2).

    The dense trunk's tilted names hang well below their markers; the engine
    must reserve that vertical extent so the lower section's bbox starts
    below the trunk section's bbox (which has grown to contain the labels).
    """
    text = """\
%%metro label_angle: 45
%%metro line: main | Main | #ff0000
%%metro grid: top | 0,0
%%metro grid: bottom | 0,1
graph LR
    subgraph top [Top]
        a[AlignmentStep]
        b[MarkDuplicates]
        c[BaseRecalibrator]
        d[NGSCheckmate]
        a -->|main| b
        b -->|main| c
        c -->|main| d
    end
    subgraph bottom [Bottom]
        %%metro entry: left | main
        e[Downstream]
        f[Output]
        e -->|main| f
    end
    d -->|main| e
"""
    graph = parse_metro_mermaid(text)
    compute_layout(graph, validate=True)

    # No label overlaps the row below at the angle the renderer draws.
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    placements = place_labels(
        graph, station_offsets=offsets, routes=routes, label_angle=45.0
    )
    assert not find_label_overlaps(graph, placements, offsets)

    # The grown trunk bbox (containing the hanging labels) clears the bottom.
    top = graph.sections["top"]
    for p in placements:
        st = graph.stations.get(p.station_id)
        if st is None or st.section_id != "top":
            continue
        _x0, _y0, _x1, label_bottom = _label_box(p)
        bottom = graph.sections["bottom"]
        assert label_bottom <= bottom.bbox_y + 1.0, (
            f"angled label {p.station_id!r} hangs into the row below"
        )
    assert top.bbox_y + top.bbox_h <= graph.sections["bottom"].bbox_y + 1.0


def _label_box(placement):
    from nf_metro.layout.labels import _label_bbox

    return _label_bbox(placement)


def _svg_rects(svg: str) -> list[tuple[float, float, float, float]]:
    """Parse ``<rect>`` elements as (x0, y0, x1, y1) tuples."""
    rects = []
    for tag in re.findall(r"<rect\b[^>]*?/?>", svg):
        attrs = {
            k: float(v)
            for k in ("x", "y", "width", "height")
            if (m := re.search(rf'\b{k}="(-?[\d.]+)"', tag))
            for v in (m.group(1),)
        }
        if len(attrs) == 4:
            x, y = attrs["x"], attrs["y"]
            rects.append((x, y, x + attrs["width"], y + attrs["height"]))
    return rects


def _svg_path_points(svg: str) -> list[list[tuple[float, float]]]:
    """Parse the polyline vertices of every ``<path>`` in the SVG."""
    paths = []
    for d in re.findall(r'<path\b[^>]*\bd="([^"]+)"', svg):
        pts = [
            (float(a), float(b))
            for a, b in re.findall(r"(-?\d+\.?\d*)[ ,]+(-?\d+\.?\d*)", d)
        ]
        if pts:
            paths.append(pts)
    return paths


def test_feeder_exits_right_of_section_box_not_through_bottom():
    """The inter-section feeder must clear the section's full drawn extent (#1).

    The section bbox grows to the right to contain the rightmost station's
    angled label.  The feeder that descends out of the section to the row
    below must turn down *outside* that grown right edge -- otherwise its
    vertical run sits inside the drawn box and visibly crosses the box's
    bottom edge.  Measured against the rendered SVG (the drawn ``<rect>`` and
    the feeder ``<path>``), not against routing waypoints or ``bbox_*``.
    """
    graph = parse_metro_mermaid(_EXAMPLE.read_text())
    compute_layout(graph, validate=True)
    svg = render_svg(graph, NFCORE_THEME)

    # Section 1 (Pre-processing) is the topmost wide section rect.  Exclude
    # the full-canvas background rect (anchored at the origin).
    section_rects = [
        r
        for r in _svg_rects(svg)
        if (r[2] - r[0]) > 200 and (r[3] - r[1]) > 100 and not (r[0] == 0 and r[1] == 0)
    ]
    section_rects.sort(key=lambda r: r[1])
    rx0, ry0, rx1, ry1 = section_rects[0]

    # Identify the feeder descents: vertical (constant-x) runs that cross the
    # section bottom edge ``ry1`` on their way to the row below, on the right.
    descent_xs: list[float] = []
    for pts in _svg_path_points(svg):
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        if not (max(ys) > ry1 + 10 and min(ys) < ry1 and max(xs) > rx1 - 60):
            continue
        for i in range(len(pts) - 1):
            (x0, y0), (x1, y1) = pts[i], pts[i + 1]
            if abs(x0 - x1) < 0.5 and min(y0, y1) < ry1 < max(y0, y1) + 0.1:
                descent_xs.append(x0)

    assert descent_xs, "no feeder descent crossing the section bottom found"

    # (a) Every descending run is right of the drawn box edge (with clearance).
    for x in descent_xs:
        assert x >= rx1 - 0.5, (
            f"feeder descends at x={x:.1f} inside the drawn box "
            f"(right edge rx1={rx1:.1f}); it must clear the grown right edge"
        )

    # (b) No feeder point lies on the section bottom-edge band while inside the
    #     box's x-range -- i.e. nothing crosses the bottom edge within the box.
    for pts in _svg_path_points(svg):
        for x, y in pts:
            if abs(y - ry1) < 8 and rx0 < x < rx1 - 0.5:
                raise AssertionError(
                    f"feeder point ({x:.1f}, {y:.1f}) crosses the section "
                    f"bottom edge (ry1={ry1:.1f}) inside the box x-range "
                    f"[{rx0:.1f}, {rx1:.1f}]"
                )


def test_angled_trunk_pitch_at_marker_floor():
    """Angled-label trunk packs to the marker+gap floor, not the label width (#2).

    Parallel diagonal labels collide only on their perpendicular separation,
    not their length, so a dense angled trunk packs to the column-pitch floor
    regardless of how long the names are -- and far tighter than the rotated
    label's horizontal-width projection would allow.  Measured on the rendered
    example's trunk station X pitch.
    """
    graph = parse_metro_mermaid(_EXAMPLE.read_text())
    compute_layout(graph, validate=True)

    trunk = [
        "fastqc",
        "fastp",
        "umi",
        "bwa",
        "merge_index",
        "markdup",
        "bqsr",
        "applybqsr",
        "mosdepth",
        "ngscheckmate",
    ]
    xs = [graph.stations[s].x for s in trunk]
    pitches = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
    mean_pitch = sum(pitches) / len(pitches)

    # Well under the rotated-label horizontal projection (~127px for these
    # names): the perpendicular-footprint packing keeps the trunk at the floor.
    assert mean_pitch < 90.0, f"angled trunk pitch {mean_pitch:.1f} too wide"

    # And no two angled labels actually overlap at that pitch.
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    placements = place_labels(
        graph, station_offsets=offsets, routes=routes, label_angle=45.0
    )
    assert not find_label_overlaps(graph, placements, offsets)
