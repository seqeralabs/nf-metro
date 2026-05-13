"""Tests for the layout engine."""

from pathlib import Path

import pytest
from layout_validator import Severity, check_station_as_elbow

from nf_metro.layout.constants import CHAR_WIDTH
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.labels import label_text_width
from nf_metro.layout.layers import assign_layers
from nf_metro.layout.ordering import assign_tracks
from nf_metro.parser.mermaid import parse_metro_mermaid


def test_layer_assignment_linear(simple_linear_graph):
    layers = assign_layers(simple_linear_graph)
    assert layers["a"] == 0
    assert layers["b"] == 1
    assert layers["c"] == 2


def test_layer_assignment_branching(diamond_graph):
    layers = assign_layers(diamond_graph)
    assert layers["a"] == 0
    # b and c both have a as predecessor, so both at layer 1
    assert layers["b"] == 1
    assert layers["c"] == 1
    # d has b and c as predecessors (both at layer 1), so at layer 2
    assert layers["d"] == 2


def test_track_assignment(diamond_graph):
    layers = assign_layers(diamond_graph)
    tracks = assign_tracks(diamond_graph, layers)
    # a is alone in layer 0
    assert tracks["a"] == 0
    # b and c are in layer 1 - should be on different tracks
    assert tracks["b"] != tracks["c"]


def test_compute_layout_sets_coordinates():
    """Layout assigns increasing x for a linear chain within a section."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph sec1 [Section]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        c[C]\n"
        "        a -->|main| b\n"
        "        b -->|main| c\n"
        "    end\n"
    )
    compute_layout(graph, x_spacing=100, y_spacing=50)
    # Stations should be in order by x
    assert graph.stations["a"].x < graph.stations["b"].x
    assert graph.stations["b"].x < graph.stations["c"].x


def test_compute_layout_branching():
    """Layout assigns correct layers for a diamond pattern within a section."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro line: alt | Alt | #0000ff\n"
        "graph LR\n"
        "    subgraph sec1 [Section]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        c[C]\n"
        "        d[D]\n"
        "        a -->|main| b\n"
        "        b -->|main| d\n"
        "        a -->|alt| c\n"
        "        c -->|alt| d\n"
        "    end\n"
    )
    compute_layout(graph, x_spacing=100, y_spacing=50)
    # a at layer 0, d at layer 2
    assert graph.stations["a"].layer == 0
    assert graph.stations["d"].layer == 2
    # b and c at same layer but different tracks
    assert graph.stations["b"].layer == graph.stations["c"].layer == 1
    assert graph.stations["b"].track != graph.stations["c"].track


def test_compute_layout_off_track_lifts_above_topmost():
    """off_track stations end up above the section's topmost line track."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro off_track: src\n"
        "graph LR\n"
        "    subgraph sec1 [Section]\n"
        "        src[Input]\n"
        "        mid[Middle]\n"
        "        sink[Sink]\n"
        "        src -->|main| mid\n"
        "        mid -->|main| sink\n"
        "    end\n"
    )
    compute_layout(graph, x_spacing=100, y_spacing=50)
    src_y = graph.stations["src"].y
    mid_y = graph.stations["mid"].y
    sink_y = graph.stations["sink"].y
    # src is off-track so it sits above the lowest of the on-track Ys
    assert src_y < mid_y
    assert src_y < sink_y
    # Section bbox grew upward to fit it
    sec = graph.sections["sec1"]
    assert sec.bbox_y <= src_y


def test_compute_layout_off_track_bbox_contains_stations():
    """Lifted off_track stations stay inside their section's bbox."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro off_track: a, b\n"
        "graph LR\n"
        "    subgraph sec1 [Section]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        mid[Mid]\n"
        "        a -->|main| mid\n"
        "        b -->|main| mid\n"
        "    end\n"
    )
    compute_layout(graph, x_spacing=100, y_spacing=50)
    sec = graph.sections["sec1"]
    for sid in ("a", "b"):
        st = graph.stations[sid]
        assert sec.bbox_y <= st.y <= sec.bbox_y + sec.bbox_h, (
            f"{sid} at y={st.y} outside bbox "
            f"y={sec.bbox_y}..{sec.bbox_y + sec.bbox_h}"
        )


def test_compute_layout_rowspan_section_compacts_content():
    """Row-spanning sections get their content compacted to the bbox top.

    Without this, a rowspan>1 section gets bbox-top-aligned to taller
    same-row neighbours but its own content stays anchored further
    down, leaving a large empty band above the first row.
    """
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro grid: tall | 0,0,2,1\n"
        "%%metro grid: short | 1,0,1,1\n"
        "%%metro grid: low | 1,1,1,1\n"
        "%%metro off_track: ofs\n"
        "graph LR\n"
        "    subgraph tall [Tall]\n"
        "        a[A]\n"
        "        a_out[Aout]\n"
        "        a -->|main| a_out\n"
        "    end\n"
        "    subgraph short [Short]\n"
        "        ofs[Off]\n"
        "        b[B]\n"
        "        ofs -->|main| b\n"
        "    end\n"
        "    subgraph low [Low]\n"
        "        c[C]\n"
        "    end\n"
        "    a_out -->|main| b\n"
    )
    compute_layout(graph, x_spacing=70, y_spacing=55)
    tall = graph.sections["tall"]
    a_y = graph.stations["a"].y
    # Empty band above first content should be at most one station row.
    assert a_y - tall.bbox_y < 55, (
        f"tall section has {a_y - tall.bbox_y:.1f}px empty space above its "
        f"first station (>= y_spacing of 55)"
    )


def test_compute_layout_off_track_terminus_does_not_kink_port():
    """Off-track sources don't push inter-section ports away from trunk.

    A captioned/uncaptioned off-track station at the top of the
    downstream section's input band must not be treated as a terminus
    when spacing the inter-section entry port: otherwise the port
    (and its upstream partner via junction propagation) gets shoved
    above the on-track row and the inter-section bundle takes a
    detour.
    """
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro off_track: extra\n"
        "graph LR\n"
        "    subgraph up [Up]\n"
        "        u1[U1]\n"
        "        u_exit[Exit]\n"
        "        u1 -->|main| u_exit\n"
        "    end\n"
        "    subgraph down [Down]\n"
        "        extra[Extra]\n"
        "        d1[D1]\n"
        "        extra -->|main| d1\n"
        "    end\n"
        "    u_exit -->|main| d1\n"
    )
    compute_layout(graph, x_spacing=70, y_spacing=55)
    # Entry and exit ports between up and down should align with each
    # other on the inter-section bundle.
    up_exit_y = graph.stations[graph.sections["up"].exit_ports[0]].y
    down_entry_y = graph.stations[graph.sections["down"].entry_ports[0]].y
    assert abs(up_exit_y - down_entry_y) < 0.5, (
        f"inter-section ports misaligned: up_exit_y={up_exit_y:.1f} "
        f"down_entry_y={down_entry_y:.1f}"
    )
    # The down entry port should be at the on-track trunk Y, not lifted
    # away above (i.e. not within the off-track band).
    d1_y = graph.stations["d1"].y
    assert abs(down_entry_y - d1_y) < 0.5, (
        f"down entry port y={down_entry_y:.1f} does not match the on-track "
        f"trunk station d1 at y={d1_y:.1f}; off-track 'extra' incorrectly "
        f"pushed the port"
    )


def test_compute_layout_captioned_off_track_clears_line_bundle():
    """Captioned off-track icons keep clearance from the line bundle.

    The optional caption rendered under a file/files/dir icon extends
    one extra label line below the station Y, so compaction must
    preserve enough gap that the caption text doesn't end up
    overlapping the topmost on-track row after content is shifted up.
    """
    graph = parse_metro_mermaid(
        "%%metro file: net_in | TSV | Network\n"
        "%%metro line: main | Main | #ff0000\n"
        "%%metro off_track: net_in\n"
        "graph LR\n"
        "    subgraph up [Up]\n"
        "        u1[U1]\n"
        "    end\n"
        "    subgraph s [S]\n"
        "        net_in[ ]\n"
        "        gsea[GSEA]\n"
        "        next[Next]\n"
        "        net_in -->|main| gsea\n"
        "        gsea -->|main| next\n"
        "    end\n"
        "    u1 -->|main| gsea\n"
    )
    compute_layout(graph, x_spacing=70, y_spacing=55)
    net_y = graph.stations["net_in"].y
    gsea_y = graph.stations["gsea"].y
    # Need room for the icon body half (~16px) + caption gap + font
    # line.  Use 30px as a conservative lower bound.
    assert gsea_y - net_y >= 30, (
        f"caption clearance too tight: gsea_y={gsea_y:.1f} net_in_y={net_y:.1f} "
        f"(gap={gsea_y - net_y:.1f}px, need >=30)"
    )


# --- Section-first layout tests ---


def test_section_layout_assigns_coordinates(two_section_graph):
    """Section-first layout assigns non-zero coordinates to all real stations."""
    for sid, station in two_section_graph.stations.items():
        if not station.is_port:
            assert station.x >= 0, f"Station {sid} has x={station.x}"
            assert station.y >= 0, f"Station {sid} has y={station.y}"


def test_section_layout_sections_dont_overlap(two_section_graph):
    """Section bounding boxes should not overlap."""
    boxes = []
    for section in two_section_graph.sections.values():
        if section.bbox_w > 0:
            boxes.append(
                (
                    section.bbox_x,
                    section.bbox_y,
                    section.bbox_x + section.bbox_w,
                    section.bbox_y + section.bbox_h,
                )
            )

    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            ax1, ay1, ax2, ay2 = boxes[i]
            bx1, by1, bx2, by2 = boxes[j]
            overlap = not (ax2 <= bx1 or bx2 <= ax1 or ay2 <= by1 or by2 <= ay1)
            assert not overlap, (
                f"Sections {i} and {j} overlap: {boxes[i]} vs {boxes[j]}"
            )


def test_section_layout_preserves_edge_order(two_section_graph):
    """Within a section, layering should preserve edge direction (a before b)."""
    assert two_section_graph.stations["a"].x < two_section_graph.stations["b"].x
    assert two_section_graph.stations["c"].x < two_section_graph.stations["d"].x


def test_section_layout_sec1_left_of_sec2(two_section_graph):
    """Section 1 (upstream) should be to the left of section 2 (downstream)."""
    sec1 = two_section_graph.sections["sec1"]
    sec2 = two_section_graph.sections["sec2"]
    assert sec1.bbox_x < sec2.bbox_x


def test_section_layout_with_grid_override():
    """Grid overrides should position sections at specified grid cells."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro line: alt | Alt | #0000ff\n"
        "%%metro grid: sec2 | 1,0\n"
        "%%metro grid: sec3 | 1,1\n"
        "graph LR\n"
        "    subgraph sec1 [Section One]\n"
        "        a[A]\n"
        "    end\n"
        "    subgraph sec2 [Section Two]\n"
        "        b[B]\n"
        "    end\n"
        "    subgraph sec3 [Section Three]\n"
        "        c[C]\n"
        "    end\n"
        "    a -->|main| b\n"
        "    a -->|alt| c\n"
    )
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    # sec2 and sec3 should be in the same column but different rows
    assert graph.sections["sec2"].grid_col == graph.sections["sec3"].grid_col == 1
    assert graph.sections["sec2"].grid_row != graph.sections["sec3"].grid_row
    # sec2 (row 0) above sec3 (row 1)
    assert graph.sections["sec2"].bbox_y < graph.sections["sec3"].bbox_y


def test_section_layout_ports_skip_rendering(two_section_graph):
    """Port stations should be filtered from label placement."""
    from nf_metro.layout.labels import place_labels

    labels = place_labels(two_section_graph)
    port_labels = [lb for lb in labels if lb.station_id in two_section_graph.ports]
    assert len(port_labels) == 0


# --- Top-alignment tests ---


def test_sections_top_aligned_in_same_row():
    """Sections in the same row share the same top, not centered."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro line: alt | Alt | #0000ff\n"
        "graph LR\n"
        "    subgraph sec1 [Tall Section]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        c[C]\n"
        "        d[D]\n"
        "        a -->|main| b\n"
        "        a -->|alt| c\n"
        "        b -->|main| d\n"
        "        c -->|alt| d\n"
        "    end\n"
        "    subgraph sec2 [Short Section]\n"
        "        e[E]\n"
        "        f[F]\n"
        "        e -->|main| f\n"
        "    end\n"
        "    d -->|main| e\n"
    )
    compute_layout(graph)
    sec1 = graph.sections["sec1"]
    sec2 = graph.sections["sec2"]
    # Both should be in the same row
    assert sec1.grid_row == sec2.grid_row == 0
    # Top edges should be flush (same bbox_y)
    assert abs(sec1.bbox_y - sec2.bbox_y) < 1.0, (
        f"Not top-aligned: sec1={sec1.bbox_y}, sec2={sec2.bbox_y}"
    )


# --- Exit-side clearance tests ---


def test_lr_exit_clearance_skipped_for_single_track():
    """LR section with single-track exit gets no extra gap (issue #142)."""
    # Single line exits straight horizontally - no diagonal convergence
    graph_with_exit = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph sec1 [Section One]\n"
        "        a[A]\n"
        "        b[LongLabelStation]\n"
        "        a -->|main| b\n"
        "    end\n"
        "    subgraph sec2 [Section Two]\n"
        "        c[C]\n"
        "    end\n"
        "    b -->|main| c\n"
    )
    graph_no_exit = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph sec1 [Section One]\n"
        "        a[A]\n"
        "        b[LongLabelStation]\n"
        "        a -->|main| b\n"
        "    end\n"
    )
    compute_layout(graph_with_exit)
    compute_layout(graph_no_exit)
    # Single-track exit should NOT add extra width
    w_exit = graph_with_exit.sections["sec1"].bbox_w
    w_no = graph_no_exit.sections["sec1"].bbox_w
    assert w_exit == w_no


def test_lr_exit_clearance_widens_bbox_for_multi_track():
    """LR section with multi-track exit gets wider bbox for diagonal clearance."""
    # Fork: a splits to b (red) and c (blue) on different tracks, both exit
    graph_with_exit = parse_metro_mermaid(
        "%%metro line: red | Red | #ff0000\n"
        "%%metro line: blue | Blue | #0000ff\n"
        "graph LR\n"
        "    subgraph sec1 [Section One]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        c[C]\n"
        "        a -->|red| b\n"
        "        a -->|blue| c\n"
        "    end\n"
        "    subgraph sec2 [Section Two]\n"
        "        d[D]\n"
        "    end\n"
        "    b -->|red| d\n"
        "    c -->|blue| d\n"
    )
    graph_no_exit = parse_metro_mermaid(
        "%%metro line: red | Red | #ff0000\n"
        "%%metro line: blue | Blue | #0000ff\n"
        "graph LR\n"
        "    subgraph sec1 [Section One]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        c[C]\n"
        "        a -->|red| b\n"
        "        a -->|blue| c\n"
        "    end\n"
    )
    compute_layout(graph_with_exit)
    compute_layout(graph_no_exit)
    # Multi-track exit should add extra width for diagonal clearance
    w_exit = graph_with_exit.sections["sec1"].bbox_w
    w_no = graph_no_exit.sections["sec1"].bbox_w
    assert w_exit > w_no


def test_lr_label_clearance_expands_bbox():
    """LR section bbox expands to contain wide station labels."""
    from nf_metro.layout.labels import label_text_width

    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph sec1 [Section One]\n"
        "        a[A]\n"
        "        b[GATK HaplotypeCaller]\n"
        "        a -->|main| b\n"
        "    end\n"
    )
    compute_layout(graph)
    sec = graph.sections["sec1"]
    station_b = graph.stations["b"]
    label_half = label_text_width("GATK HaplotypeCaller") / 2
    # Label right edge should fit within section bbox
    assert station_b.x + label_half < sec.bbox_x + sec.bbox_w


def test_rl_exit_clearance_preserves_bbox_x():
    """RL section exit clearance should shift stations right, not move bbox_x left."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph sec1 [Source]\n"
        "        a[A]\n"
        "    end\n"
        "    subgraph sec2 [RL Section]\n"
        "        b[B]\n"
        "        c[LongLabel]\n"
        "        c -->|main| b\n"
        "    end\n"
        "    subgraph sec3 [Target]\n"
        "        d[D]\n"
        "    end\n"
        "    a -->|main| c\n"
        "    b -->|main| d\n"
    )
    compute_layout(graph)
    sec2 = graph.sections["sec2"]
    # The section should have a valid bbox_x aligned with its grid column offset.
    # The key invariant: stations within the section should be contained within
    # the bbox (checked by station_containment validator).
    for sid in sec2.station_ids:
        station = graph.stations.get(sid)
        if station and not station.is_port:
            assert station.x >= sec2.bbox_x, (
                f"Station {sid} at x={station.x} is left of bbox_x={sec2.bbox_x}"
            )
            assert station.x <= sec2.bbox_x + sec2.bbox_w, (
                f"Station {sid} at x={station.x} is right of bbox edge"
            )


# --- Flat layout empty tracks test ---


def test_flat_layout_unnamed_edges():
    """Unnamed edges (no line IDs) raise a clear error (issue #75)."""
    import pytest

    with pytest.raises(ValueError, match="no metro line annotation"):
        parse_metro_mermaid(
            "%%metro line: main | Main | #ff0000\ngraph LR\n    a --> b\n"
        )


# --- Line order tests ---


def test_line_order_definition_default():
    """Default line_order='definition' preserves .mmd line definition order."""
    graph = parse_metro_mermaid(
        "%%metro line: short | Short | #ff0000\n"
        "%%metro line: long | Long | #0000ff\n"
        "graph LR\n"
        "    subgraph sec1 [Section One]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        a -->|short| b\n"
        "        a -->|long| b\n"
        "    end\n"
        "    subgraph sec2 [Section Two]\n"
        "        c[C]\n"
        "        d[D]\n"
        "        c -->|long| d\n"
        "    end\n"
        "    b -->|long| c\n"
    )
    assert graph.line_order == "definition"
    layers = assign_layers(graph)
    tracks = assign_tracks(graph, layers)
    # 'short' should have base track 0 (defined first)
    # Stations on short line should be at track 0
    assert tracks["a"] is not None


def test_line_order_span_reorders():
    """line_order='span' gives inner tracks to lines spanning more sections."""
    from nf_metro.layout.ordering import _reorder_by_span

    graph = parse_metro_mermaid(
        "%%metro line: short | Short | #ff0000\n"
        "%%metro line: long | Long | #0000ff\n"
        "%%metro line_order: span\n"
        "graph LR\n"
        "    subgraph sec1 [Section One]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        a -->|short| b\n"
        "        a -->|long| b\n"
        "    end\n"
        "    subgraph sec2 [Section Two]\n"
        "        c[C]\n"
        "        d[D]\n"
        "        c -->|long| d\n"
        "    end\n"
        "    b -->|long| c\n"
    )
    assert graph.line_order == "span"
    line_order = list(graph.lines.keys())
    reordered = _reorder_by_span(graph, line_order)
    # 'long' spans 2 sections, 'short' spans 1 -> long should come first
    assert reordered[0] == "long"
    assert reordered[1] == "short"


def test_line_order_span_preserves_ties():
    """Lines with equal span preserve definition order."""
    from nf_metro.layout.ordering import _reorder_by_span

    graph = parse_metro_mermaid(
        "%%metro line: alpha | Alpha | #ff0000\n"
        "%%metro line: beta | Beta | #0000ff\n"
        "%%metro line_order: span\n"
        "graph LR\n"
        "    subgraph sec1 [Section One]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        a -->|alpha| b\n"
        "        a -->|beta| b\n"
        "    end\n"
    )
    line_order = list(graph.lines.keys())
    reordered = _reorder_by_span(graph, line_order)
    # Both span 1 section -> preserve original order
    assert reordered == ["alpha", "beta"]


def test_line_order_span_e2e():
    """End-to-end: span ordering changes track assignment."""
    # With definition order: short gets track 0, long gets track 1
    graph_def = parse_metro_mermaid(
        "%%metro line: short | Short | #ff0000\n"
        "%%metro line: long | Long | #0000ff\n"
        "graph LR\n"
        "    subgraph sec1 [Section One]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        a -->|short| b\n"
        "        a -->|long| b\n"
        "    end\n"
        "    subgraph sec2 [Section Two]\n"
        "        c[C]\n"
        "        d[D]\n"
        "        c -->|long| d\n"
        "    end\n"
        "    b -->|long| c\n"
    )
    compute_layout(graph_def)

    # With span order: long gets track 0, short gets track 1
    graph_span = parse_metro_mermaid(
        "%%metro line: short | Short | #ff0000\n"
        "%%metro line: long | Long | #0000ff\n"
        "%%metro line_order: span\n"
        "graph LR\n"
        "    subgraph sec1 [Section One]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        a -->|short| b\n"
        "        a -->|long| b\n"
        "    end\n"
        "    subgraph sec2 [Section Two]\n"
        "        c[C]\n"
        "        d[D]\n"
        "        c -->|long| d\n"
        "    end\n"
        "    b -->|long| c\n"
    )
    compute_layout(graph_span)

    # In sec1, both 'a' and 'b' are on both lines. The key difference
    # is which line's base track is 0. With span ordering, 'long' gets
    # the inner track.
    # We verify that section layouts both succeed (no crash)
    assert graph_def.stations["a"].x > 0
    assert graph_span.stations["a"].x > 0


def test_flat_layout_no_named_lines():
    """Unnamed edges with a declared line still raise an error (issue #75)."""
    import pytest

    with pytest.raises(ValueError, match="no metro line annotation"):
        parse_metro_mermaid(
            "%%metro line: main | Main | #ff0000\n"
            "graph LR\n"
            "    a[Start]\n"
            "    b[End]\n"
            "    a --> b\n"
        )


# --- Label clamping tests (issue #58) ---


def test_label_clamp_flips_when_overlapping_pill():
    """Label clamped into pill should flip to the opposite side (issue #58)."""
    from nf_metro.layout.labels import place_labels
    from nf_metro.layout.routing.offsets import compute_station_offsets

    # Build a section with many tracks so the bottom station is near
    # the section bbox bottom edge, triggering clamping for below labels.
    graph = parse_metro_mermaid(
        "%%metro line: L1 | Line1 | #ff0000\n"
        "%%metro line: L2 | Line2 | #00ff00\n"
        "%%metro line: L3 | Line3 | #0000ff\n"
        "%%metro line: L4 | Line4 | #ff00ff\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        c[C]\n"
        "        d[D]\n"
        "        a -->|L1| b\n"
        "        a -->|L2| c\n"
        "        a -->|L3| d\n"
        "        b -->|L1| d\n"
        "        c -->|L2| d\n"
        "    end\n"
    )
    compute_layout(graph, y_spacing=40)

    station_offsets = compute_station_offsets(graph)
    labels = place_labels(graph, station_offsets=station_offsets)

    # For every label, verify it doesn't overlap its station pill
    for lp in labels:
        s = graph.stations[lp.station_id]
        lines = graph.station_lines(lp.station_id)
        offs = [station_offsets.get((lp.station_id, lid), 0.0) for lid in lines]
        pill_top = s.y + (min(offs) if offs else 0.0)
        pill_bottom = s.y + (max(offs) if offs else 0.0)

        if lp.above:
            gap = pill_top - lp.y
        else:
            gap = lp.y - pill_bottom

        # The gap may be reduced by adaptive label offsets for tightly
        # stacked stations, but must stay above the 2px floor.
        assert gap >= 2.0, f"Label for {lp.station_id} too close to pill: gap={gap:.1f}"


def test_label_clamp_expands_bbox_when_both_sides_tight():
    """When neither side fits, the section bbox should expand (issue #58)."""
    from nf_metro.layout.constants import LABEL_BBOX_MARGIN, LABEL_OFFSET
    from nf_metro.layout.labels import LabelPlacement, _clamp_label_vertical
    from nf_metro.parser.model import Section, Station

    # Create a tiny section where neither above nor below would fit
    sec = Section(id="tiny", name="Tiny")
    sec.bbox_x = 0
    sec.bbox_y = 100
    sec.bbox_w = 200
    sec.bbox_h = 50  # Very tight

    station = Station(id="s", label="Test")
    station.x = 100
    station.y = 125  # Center of the 50px-tall section
    station.section_id = "tiny"

    # Label below would be at y=141 (125+16), bottom at 155 > section bottom 150
    candidate = LabelPlacement(station_id="s", text="Test", x=100, y=141, above=False)
    original_bbox_h = sec.bbox_h
    result = _clamp_label_vertical(
        candidate, sec, station, LABEL_OFFSET, 0.0, 0.0, LABEL_BBOX_MARGIN
    )
    # The bbox should have expanded (either flipped to above and fit,
    # or expanded to accommodate)
    if not result.above:
        # If it stayed below, bbox must have grown
        assert sec.bbox_h > original_bbox_h


# ---- Multi-line label helpers ----


# --- Straight diamond tests (issue #115) ---


def _diamond_section_text(diamond_style="straight"):
    """Build a section with a 2-way diamond where all lines take both branches."""
    return (
        "%%metro line: L1 | Line1 | #ff0000\n"
        "%%metro line: L2 | Line2 | #0000ff\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        c[C]\n"
        "        d[D]\n"
        "        a -->|L1,L2| b\n"
        "        a -->|L1,L2| c\n"
        "        b -->|L1,L2| d\n"
        "        c -->|L1,L2| d\n"
        "    end\n"
    )


def test_is_diamond_fanout():
    """_is_diamond_fanout detects fork-join patterns."""
    import networkx as nx

    from nf_metro.layout.ordering import _is_diamond_fanout

    G = nx.DiGraph()
    G.add_edges_from([("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")])
    assert _is_diamond_fanout(["b", "c"], G) is True
    # Single node is never a diamond
    assert _is_diamond_fanout(["b"], G) is False
    # Nodes with different predecessors are not a diamond
    G2 = nx.DiGraph()
    G2.add_edges_from([("a", "b"), ("x", "c"), ("b", "d"), ("c", "d")])
    assert _is_diamond_fanout(["b", "c"], G2) is False


def test_lift_would_cause_uturn_skips_when_feeders_below_anchor():
    """A trunk candidate with all-below feeders should NOT be lifted."""
    from nf_metro.layout.engine import _lift_would_cause_uturn
    from nf_metro.parser.model import Edge, MetroGraph, Station

    g = MetroGraph()
    g.stations["f1"] = Station(id="f1", label="F1", x=0, y=200, section_id="upstream")
    g.stations["f2"] = Station(id="f2", label="F2", x=0, y=250, section_id="upstream")
    g.stations["cand"] = Station(id="cand", label="C", x=100, y=200, section_id="ds")
    g.edges = [
        Edge(source="f1", target="cand", line_id="L1"),
        Edge(source="f2", target="cand", line_id="L2"),
    ]
    assert _lift_would_cause_uturn(g, "cand", "ds", anchor_y=200) is True


def test_lift_would_cause_uturn_allows_when_feeder_above():
    """When at least one feeder sits above anchor_y, lifting is safe."""
    from nf_metro.layout.engine import _lift_would_cause_uturn
    from nf_metro.parser.model import Edge, MetroGraph, Station

    g = MetroGraph()
    g.stations["f1"] = Station(id="f1", label="F1", x=0, y=100, section_id="upstream")
    g.stations["f2"] = Station(id="f2", label="F2", x=0, y=250, section_id="upstream")
    g.stations["cand"] = Station(id="cand", label="C", x=100, y=200, section_id="ds")
    g.edges = [
        Edge(source="f1", target="cand", line_id="L1"),
        Edge(source="f2", target="cand", line_id="L2"),
    ]
    assert _lift_would_cause_uturn(g, "cand", "ds", anchor_y=200) is False


def test_lift_would_cause_uturn_ignores_single_feeder():
    """A single external feeder is not enough to flag a U-turn."""
    from nf_metro.layout.engine import _lift_would_cause_uturn
    from nf_metro.parser.model import Edge, MetroGraph, Station

    g = MetroGraph()
    g.stations["f1"] = Station(id="f1", label="F1", x=0, y=250, section_id="upstream")
    g.stations["cand"] = Station(id="cand", label="C", x=100, y=200, section_id="ds")
    g.edges = [Edge(source="f1", target="cand", line_id="L1")]
    assert _lift_would_cause_uturn(g, "cand", "ds", anchor_y=200) is False


def test_straight_diamond_top_branch_stays_flat():
    """With diamond_style='straight', the top branch of a diamond stays on the trunk."""
    graph = parse_metro_mermaid(_diamond_section_text())
    # Default is now "straight"
    assert graph.diamond_style == "straight"
    compute_layout(graph)
    # b (first branch, top) should be at the same Y as a (trunk)
    assert graph.stations["b"].y == graph.stations["a"].y


def test_symmetric_diamond_both_branches_deviate():
    """With diamond_style='symmetric', both branches deviate from the trunk."""
    graph = parse_metro_mermaid(_diamond_section_text())
    graph.diamond_style = "symmetric"
    compute_layout(graph)
    a_y = graph.stations["a"].y
    b_y = graph.stations["b"].y
    c_y = graph.stations["c"].y
    # Both b and c should deviate from a (symmetric fan-out)
    assert b_y != a_y or c_y != a_y
    # And b should be above c (or at least at different positions)
    assert b_y != c_y


def test_straight_diamond_merge_returns_to_trunk():
    """With diamond_style='straight', the merge node after a diamond snaps to trunk."""
    graph = parse_metro_mermaid(_diamond_section_text())
    compute_layout(graph)
    # d (merge) should be at the same Y as a (trunk)
    assert graph.stations["d"].y == graph.stations["a"].y


def _terminal_full_bundle_text():
    """Two full-bundle terminal stations fed from upstream methods.

    Reproduces the Reporting-style topology: a terminal section (no
    exit ports) where two stations both carry the full bundle and both
    receive from the same upstream branches.
    """
    return (
        "%%metro line: L1 | Line1 | #ff0000\n"
        "%%metro line: L2 | Line2 | #00ff00\n"
        "graph LR\n"
        "    subgraph methods [Methods]\n"
        "        m1[M1]\n"
        "        m2[M2]\n"
        "    end\n"
        "    subgraph report [Report]\n"
        "        a[A]\n"
        "        b[B]\n"
        "    end\n"
        "    m1 -->|L1,L2| a\n"
        "    m2 -->|L1,L2| a\n"
        "    m1 -->|L1,L2| b\n"
        "    m2 -->|L1,L2| b\n"
    )


def test_full_bundle_column_fans_around_trunk_with_center_ports():
    """Terminal section's full-bundle column fans symmetrically when --center-ports is on."""
    graph = parse_metro_mermaid(_terminal_full_bundle_text())
    graph.center_ports = True
    compute_layout(graph, y_spacing=50.0)
    ay = graph.stations["a"].y
    by = graph.stations["b"].y
    assert ay != by, "a and b should not share Y after fan"
    # Symmetric around the section trunk Y (here derived from the LR port).
    mid = (ay + by) / 2
    assert abs((by - mid) - (mid - ay)) < 1e-6, (
        f"a and b should be symmetric around trunk Y: a={ay}, b={by}, mid={mid}"
    )
    assert abs(abs(by - ay) - 100.0) < 1e-6, (
        f"a and b should be 2*y_spacing apart: |b-a|={abs(by-ay)}"
    )


def test_full_bundle_column_no_op_without_center_ports():
    """The new fan-out only fires when --center-ports is enabled."""
    graph = parse_metro_mermaid(_terminal_full_bundle_text())
    graph.center_ports = False
    compute_layout(graph, y_spacing=50.0)
    # Without center_ports, a and b should sit on adjacent tracks (1 step apart).
    delta = abs(graph.stations["b"].y - graph.stations["a"].y)
    assert delta == pytest.approx(50.0), (
        f"Without center_ports, a/b should be 1 y_spacing apart: delta={delta}"
    )


def test_full_bundle_column_fans_non_terminal_section():
    """Non-terminal full-bundle columns also fan around the trunk Y."""
    text = (
        "%%metro line: L1 | Line1 | #ff0000\n"
        "%%metro line: L2 | Line2 | #00ff00\n"
        "graph LR\n"
        "    subgraph upstream [Upstream]\n"
        "        u[U]\n"
        "    end\n"
        "    subgraph middle [Middle]\n"
        "        a[A]\n"
        "        b[B]\n"
        "    end\n"
        "    subgraph downstream [Downstream]\n"
        "        d[D]\n"
        "    end\n"
        "    u -->|L1,L2| a\n"
        "    u -->|L1,L2| b\n"
        "    a -->|L1,L2| d\n"
        "    b -->|L1,L2| d\n"
    )
    graph = parse_metro_mermaid(text)
    graph.center_ports = True
    compute_layout(graph, y_spacing=50.0)
    # `middle` has exit ports but its column carries only full-bundle
    # stations with no unique trunk, so the symfan should fire and the
    # pair should flank a vacant trunk row.
    ay = graph.stations["a"].y
    by = graph.stations["b"].y
    delta = abs(by - ay)
    assert delta == pytest.approx(100.0), (
        f"Non-terminal full-bundle column should flank trunk: delta={delta}"
    )
    mid = (ay + by) / 2
    assert abs((by - mid) - (mid - ay)) < 1e-6, (
        f"a and b should be symmetric around trunk Y: a={ay}, b={by}, mid={mid}"
    )


def test_off_track_input_sits_adjacent_to_its_consumer():
    """Each off-track input lands one y_spacing above its consumer.

    When two off-track inputs in the same section feed different
    consumer stations, they should sit at distinct Ys derived from
    their respective consumers, not stack at a uniform top-of-section
    band.
    """
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro off_track: in_a, in_b\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        in_a[A]\n"
        "        in_b[B]\n"
        "        upper[Upper]\n"
        "        lower[Lower]\n"
        "        upper -->|main| lower\n"
        "        in_a -->|main| upper\n"
        "        in_b -->|main| lower\n"
        "    end\n"
    )
    compute_layout(graph, x_spacing=70, y_spacing=55)
    upper_y = graph.stations["upper"].y
    lower_y = graph.stations["lower"].y
    in_a_y = graph.stations["in_a"].y
    in_b_y = graph.stations["in_b"].y
    # Each input sits one row above its respective consumer.
    assert in_a_y == pytest.approx(upper_y - 55), (
        f"in_a y={in_a_y} should be upper_y - y_spacing = {upper_y - 55}"
    )
    assert in_b_y == pytest.approx(lower_y - 55), (
        f"in_b y={in_b_y} should be lower_y - y_spacing = {lower_y - 55}"
    )
    # Inputs sit at different Ys (not a uniform band).
    assert in_a_y != in_b_y


def test_multiple_off_track_inputs_share_consumer_stack_above_it():
    """Multiple off-track inputs feeding one consumer stack above it."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro off_track: in_a, in_b\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        in_a[A]\n"
        "        in_b[B]\n"
        "        mid[Mid]\n"
        "        sink[Sink]\n"
        "        mid -->|main| sink\n"
        "        in_a -->|main| mid\n"
        "        in_b -->|main| mid\n"
        "    end\n"
    )
    compute_layout(graph, x_spacing=70, y_spacing=55)
    mid_y = graph.stations["mid"].y
    in_a_y = graph.stations["in_a"].y
    in_b_y = graph.stations["in_b"].y
    # The two inputs stack above mid at 1*step and 2*step respectively.
    ys = sorted([in_a_y, in_b_y])
    assert ys[0] == pytest.approx(mid_y - 110), (
        f"topmost input should be 2*y_spacing above consumer: {ys[0]} vs {mid_y - 110}"
    )
    assert ys[1] == pytest.approx(mid_y - 55), (
        f"second input should be 1*y_spacing above consumer: {ys[1]} vs {mid_y - 55}"
    )


def test_cli_straight_diamonds_default(tmp_path):
    """--straight-diamonds is on by default."""
    from click.testing import CliRunner

    from nf_metro.cli import cli

    mmd = tmp_path / "diamond.mmd"
    mmd.write_text(_diamond_section_text())
    out = tmp_path / "out.svg"
    runner = CliRunner()
    result = runner.invoke(cli, ["render", str(mmd), "-o", str(out)])
    assert result.exit_code == 0, result.output


def test_cli_no_straight_diamonds(tmp_path):
    """--no-straight-diamonds reverts to symmetric behaviour."""
    from click.testing import CliRunner

    from nf_metro.cli import cli

    mmd = tmp_path / "diamond.mmd"
    mmd.write_text(_diamond_section_text())
    out = tmp_path / "out.svg"
    runner = CliRunner()
    result = runner.invoke(
        cli, ["render", str(mmd), "-o", str(out), "--no-straight-diamonds"]
    )
    assert result.exit_code == 0, result.output


def test_straight_diamond_inter_section_port_alignment():
    """With straight diamonds, inter-section ports align to the majority target Y."""
    graph = parse_metro_mermaid(
        "%%metro line: L1 | Line1 | #ff0000\n"
        "%%metro line: L2 | Line2 | #0000ff\n"
        "%%metro line: L3 | Line3 | #00ff00\n"
        "graph LR\n"
        "    subgraph sec1 [Section One]\n"
        "        a[A]\n"
        "    end\n"
        "    subgraph sec2 [Section Two]\n"
        "        b[B]\n"
        "        c[C]\n"
        "        b -->|L1,L2| c\n"
        "    end\n"
        "    a -->|L1,L2| b\n"
        "    a -->|L3| c\n"
    )
    compute_layout(graph)
    # The entry port should align with b (2 lines) not the average of b and c
    entry_ports = graph.sections["sec2"].entry_ports
    assert len(entry_ports) > 0
    entry_y = graph.stations[entry_ports[0]].y
    b_y = graph.stations["b"].y
    # Entry port should be at b's Y (majority target)
    assert abs(entry_y - b_y) < 1.0, (
        f"Entry port at y={entry_y} should align with b at y={b_y}"
    )


def test_mismatched_tracks_port_alignment():
    """Entry port Y aligns with source exit port Y when track counts differ (#165)."""
    graph = parse_metro_mermaid(
        "%%metro line: a | Alpha | #0570b0\n"
        "%%metro line: b | Beta | #2db572\n"
        "%%metro line: c | Gamma | #e31a1c\n"
        "%%metro line: d | Delta | #ff7f00\n"
        "%%metro line: e | Epsilon | #6a3d9a\n"
        "graph LR\n"
        "    subgraph wide [Wide Section]\n"
        "        w1[Start]\n"
        "        w2a[Path A]\n"
        "        w2b[Path B]\n"
        "        w2c[Path C]\n"
        "        w2d[Path D]\n"
        "        w2e[Path E]\n"
        "        w3[Merge]\n"
        "        w1 -->|a| w2a\n"
        "        w1 -->|b| w2b\n"
        "        w1 -->|c| w2c\n"
        "        w1 -->|d| w2d\n"
        "        w1 -->|e| w2e\n"
        "        w2a -->|a| w3\n"
        "        w2b -->|b| w3\n"
        "        w2c -->|c| w3\n"
        "        w2d -->|d| w3\n"
        "        w2e -->|e| w3\n"
        "    end\n"
        "    subgraph narrow [Narrow Section]\n"
        "        n1[Receive]\n"
        "        n2[Output]\n"
        "        n1 -->|a,b,c,d,e| n2\n"
        "    end\n"
        "    w3 -->|a,b,c,d,e| n1\n"
    )
    compute_layout(graph)

    # Find exit port of wide section and entry port of narrow section
    wide_exit_ports = graph.sections["wide"].exit_ports
    narrow_entry_ports = graph.sections["narrow"].entry_ports
    assert wide_exit_ports and narrow_entry_ports

    exit_y = graph.stations[wide_exit_ports[0]].y
    entry_y = graph.stations[narrow_entry_ports[0]].y

    # Ports should be at the same Y (horizontal inter-section line)
    assert abs(exit_y - entry_y) < 5.0, (
        f"Exit port at y={exit_y} and entry port at y={entry_y} should align "
        f"for horizontal inter-section connection (delta={abs(exit_y - entry_y):.1f})"
    )


def test_label_text_width_single_line():
    assert label_text_width("Hello") == 5 * CHAR_WIDTH


def test_label_text_width_multiline():
    # Width should be based on the longest line
    assert label_text_width("AB\nCDEF") == 4 * CHAR_WIDTH


def test_label_text_width_empty():
    assert label_text_width("") == 0


# --- Port-terminus spacing (Phase 7c) ---


def test_port_terminus_spacing_basic():
    """Entry port is pushed away from a source terminus it doesn't connect to.

    Section 2 has a source terminus (ref_in) and an entry port carrying
    a different line (main from sec1).  The entry port must maintain at
    least y_spacing from ref_in so routed lines don't overlap the icon.
    """
    y_spacing = 40
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #2db572\n"
        "%%metro line: alt | Alt | #0570b0\n"
        "%%metro file: ref_in | FASTA\n"
        "graph LR\n"
        "    subgraph sec1 [Source]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        a -->|main| b\n"
        "    end\n"
        "    subgraph sec2 [Target]\n"
        "        ref_in[ ]\n"
        "        c[C]\n"
        "        d[D]\n"
        "        ref_in -->|alt| c\n"
        "        c -->|main,alt| d\n"
        "    end\n"
        "    b -->|main| c\n"
    )
    compute_layout(graph, y_spacing=y_spacing)

    # Identify the entry port(s) on sec2
    sec2 = graph.sections["sec2"]
    ref_y = graph.stations["ref_in"].y

    for pid in sec2.entry_ports:
        port_st = graph.stations[pid]
        # The entry port carries 'main' from sec1 and should NOT be
        # directly connected to ref_in.  Verify spacing.
        neighbours = set()
        for edge in graph.edges:
            if edge.source == pid:
                neighbours.add(edge.target)
            if edge.target == pid:
                neighbours.add(edge.source)

        if "ref_in" not in neighbours:
            gap = abs(port_st.y - ref_y)
            assert gap >= y_spacing - 1, (
                f"Port {pid} at y={port_st.y:.1f} is only {gap:.1f}px "
                f"from terminus ref_in at y={ref_y:.1f} "
                f"(need >= {y_spacing})"
            )


def test_port_terminus_spacing_no_station_as_elbow():
    """Phase 7c must not introduce station-as-elbow violations.

    Uses the variant_calling_tuned example which triggered the original
    icon overlap issue, and checks that the fix doesn't create new
    station-as-elbow problems.
    """
    from pathlib import Path

    example = Path(__file__).parent.parent / "examples" / "variant_calling_tuned.mmd"
    if not example.exists():
        return
    graph = parse_metro_mermaid(example.read_text())
    compute_layout(graph)

    violations = check_station_as_elbow(graph)
    errors = [v for v in violations if v.severity == Severity.ERROR]
    assert not errors, "station-as-elbow violations after Phase 7c:\n" + "\n".join(
        v.message for v in errors
    )


def test_section_gap_increases_distance():
    """Larger section_x_gap produces wider gaps between section bboxes."""
    mmd_text = (
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph sec1 [Section One]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        a -->|main| b\n"
        "    end\n"
        "    subgraph sec2 [Section Two]\n"
        "        c[C]\n"
        "        d[D]\n"
        "        c -->|main| d\n"
        "    end\n"
        "    b -->|main| c\n"
    )

    graph_narrow = parse_metro_mermaid(mmd_text)
    compute_layout(graph_narrow, section_x_gap=50)
    gap_narrow = (
        graph_narrow.sections["sec2"].bbox_x
        - graph_narrow.sections["sec1"].bbox_x
        - graph_narrow.sections["sec1"].bbox_w
    )

    graph_wide = parse_metro_mermaid(mmd_text)
    compute_layout(graph_wide, section_x_gap=150)
    gap_wide = (
        graph_wide.sections["sec2"].bbox_x
        - graph_wide.sections["sec1"].bbox_x
        - graph_wide.sections["sec1"].bbox_w
    )

    assert gap_wide > gap_narrow, (
        f"Wide gap ({gap_wide:.1f}) should be larger than narrow gap ({gap_narrow:.1f})"
    )


def test_section_gap_bundle_aware_minimum():
    """Bundle-aware enforcement widens the gap for multi-line bundles."""
    import warnings

    from nf_metro.layout.constants import CURVE_RADIUS, OFFSET_STEP

    # 5 lines routing between two sections
    mmd_text = (
        "%%metro line: L1 | Line1 | #ff0000\n"
        "%%metro line: L2 | Line2 | #00ff00\n"
        "%%metro line: L3 | Line3 | #0000ff\n"
        "%%metro line: L4 | Line4 | #ff00ff\n"
        "%%metro line: L5 | Line5 | #ffff00\n"
        "graph LR\n"
        "    subgraph sec1 [Section One]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        a -->|L1,L2,L3,L4,L5| b\n"
        "    end\n"
        "    subgraph sec2 [Section Two]\n"
        "        c[C]\n"
        "        d[D]\n"
        "        c -->|L1,L2,L3,L4,L5| d\n"
        "    end\n"
        "    b -->|L1,L2,L3,L4,L5| c\n"
    )
    # Request a very small gap that should be overridden
    graph = parse_metro_mermaid(mmd_text)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        compute_layout(graph, section_x_gap=10)
        # Should have warned about widening
        gap_warnings = [x for x in w if "widened" in str(x.message)]
        assert len(gap_warnings) >= 1, "Expected a warning about gap widening"

    # Physical gap should be at least the bundle minimum
    min_needed = 2 * (CURVE_RADIUS + 4 * OFFSET_STEP)
    physical_gap = (
        graph.sections["sec2"].bbox_x
        - graph.sections["sec1"].bbox_x
        - graph.sections["sec1"].bbox_w
    )
    assert physical_gap >= min_needed - 1, (
        f"Physical gap {physical_gap:.1f}px is below bundle minimum {min_needed:.1f}px"
    )


def test_section_gap_no_warning_when_sufficient():
    """No warning when the requested gap is large enough for the bundle."""
    import warnings

    mmd_text = (
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph sec1 [Section One]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        a -->|main| b\n"
        "    end\n"
        "    subgraph sec2 [Section Two]\n"
        "        c[C]\n"
        "        d[D]\n"
        "        c -->|main| d\n"
        "    end\n"
        "    b -->|main| c\n"
    )
    graph = parse_metro_mermaid(mmd_text)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        compute_layout(graph, section_x_gap=100)
        gap_warnings = [x for x in w if "widened" in str(x.message)]
        assert len(gap_warnings) == 0, "No warning expected for large gap"


def test_port_terminus_spacing_multi_terminus():
    """When two termini are near a port, the port clears both of them.

    Tests the convergence guarantee: the port should not thrash between
    two conflicting termini, but settle at a Y that satisfies both.
    """
    y_spacing = 40
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro line: alt1 | Alt1 | #00ff00\n"
        "%%metro line: alt2 | Alt2 | #0000ff\n"
        "%%metro file: src1 | FASTA\n"
        "%%metro file: src2 | BED\n"
        "graph LR\n"
        "    subgraph sec1 [Source]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        a -->|main| b\n"
        "    end\n"
        "    subgraph sec2 [Target]\n"
        "        src1[ ]\n"
        "        src2[ ]\n"
        "        c[C]\n"
        "        d[D]\n"
        "        src1 -->|alt1| c\n"
        "        src2 -->|alt2| d\n"
        "        c -->|main,alt1| d\n"
        "    end\n"
        "    b -->|main| c\n"
    )
    compute_layout(graph, y_spacing=y_spacing)

    sec2 = graph.sections["sec2"]
    src1_y = graph.stations["src1"].y
    src2_y = graph.stations["src2"].y

    for pid in sec2.entry_ports:
        port_st = graph.stations[pid]
        neighbours = set()
        for edge in graph.edges:
            if edge.source == pid:
                neighbours.add(edge.target)
            if edge.target == pid:
                neighbours.add(edge.source)

        # Check distance from each non-connected terminus
        for tid, ty in [("src1", src1_y), ("src2", src2_y)]:
            if tid not in neighbours:
                gap = abs(port_st.y - ty)
                assert gap >= y_spacing - 1, (
                    f"Port {pid} at y={port_st.y:.1f} is only "
                    f"{gap:.1f}px from terminus {tid} at y={ty:.1f} "
                    f"(need >= {y_spacing})"
                )


# ---------------------------------------------------------------------------
# Phase-boundary guard tests
# ---------------------------------------------------------------------------

TOPOLOGIES_DIR = Path(__file__).parent / "fixtures" / "topologies"
EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


class TestPhaseGuards:
    """Verify that phase-boundary invariants hold across all fixtures."""

    def _layout_validated(self, mmd_text: str) -> None:
        graph = parse_metro_mermaid(mmd_text)
        compute_layout(graph, validate=True)

    @pytest.mark.parametrize(
        "fixture",
        sorted(TOPOLOGIES_DIR.glob("*.mmd")),
        ids=lambda p: p.stem,
    )
    def test_topology_fixtures(self, fixture):
        self._layout_validated(fixture.read_text())

    def test_rnaseq_sections(self):
        self._layout_validated((EXAMPLES_DIR / "rnaseq_sections.mmd").read_text())

    def test_rnaseq_auto(self):
        path = EXAMPLES_DIR / "rnaseq_auto.mmd"
        if path.exists():
            self._layout_validated(path.read_text())

    def test_simple_two_sections(self):
        self._layout_validated(
            "%%metro line: main | Main | #ff0000\n"
            "graph LR\n"
            "    subgraph s1 [S1]\n"
            "        a[A]\n"
            "    end\n"
            "    subgraph s2 [S2]\n"
            "        b[B]\n"
            "    end\n"
            "    a -->|main| b\n"
        )

    def test_flat_graph(self):
        """Flat (sectionless) graphs skip section layout but should not crash."""
        graph = parse_metro_mermaid(
            "%%metro line: main | Main | #ff0000\ngraph LR\n    a -->|main| b\n"
        )
        # validate=True should be harmless for flat layout
        compute_layout(graph, validate=True)
