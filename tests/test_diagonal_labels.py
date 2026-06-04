"""Layout/placement behaviour for opt-in diagonal station labels (#527)."""

from __future__ import annotations

from nf_metro.layout.constants import DIAGONAL_LABEL_OFFSET, LABEL_OFFSET
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.labels import find_label_overlaps, place_labels
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid

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
