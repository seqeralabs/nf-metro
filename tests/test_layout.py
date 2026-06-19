"""Tests for the layout engine."""

from pathlib import Path

import pytest
from layout_validator import Severity, check_station_as_elbow

from nf_metro.layout.constants import CHAR_WIDTH, SECTION_Y_PADDING
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.labels import label_text_width
from nf_metro.layout.layers import assign_layers
from nf_metro.layout.ordering import assign_tracks
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import is_bypass_v


def _patch_layout_helper(monkeypatch, name, replacement):
    """Rebind a layout helper everywhere it is bound for call-time lookup.

    Phase helpers live in ``nf_metro.layout.phases.*`` and are re-exported
    from ``engine``.  A bare-name call resolves through the *defining*
    module's namespace, while ``engine.<name>`` only intercepts calls made
    from ``engine`` itself.  Patch the name in every loaded ``layout`` module
    whose binding is the original object so the corruption takes effect
    regardless of which module the call site lives in.
    """
    import sys

    from nf_metro.layout import engine

    original = getattr(engine, name)
    patched = []
    for modname, mod in list(sys.modules.items()):
        if not modname.startswith("nf_metro.layout"):
            continue
        if getattr(mod, name, None) is original:
            monkeypatch.setattr(mod, name, replacement)
            patched.append(modname)
    assert patched, f"{name!r} was not bound in any layout module"


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
            f"{sid} at y={st.y} outside bbox y={sec.bbox_y}..{sec.bbox_y + sec.bbox_h}"
        )


def test_compute_layout_rowspan_section_compacts_content():
    """Row-spanning sections compact to share the row trunk Y.

    A row-spanning section without its own off-track content should
    sit on the row's trunk Y so a straight inter-section bundle passes
    through it without kinking.  Its bbox top hugs its own content
    rather than following an off-track row-mate's raised top, so the two
    do not share a bbox top.
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
    short = graph.sections["short"]
    a_y = graph.stations["a"].y
    a_out_y = graph.stations["a_out"].y
    b_y = graph.stations["b"].y
    # The trunk a -> a_out -> b must stay horizontal across sections.
    assert a_y == pytest.approx(a_out_y), (
        f"a y={a_y} and a_out y={a_out_y} must share trunk Y"
    )
    assert a_out_y == pytest.approx(b_y), (
        f"a_out y={a_out_y} and b y={b_y} must share trunk Y across sections"
    )
    # Each section hugs its own content, so the rowspan top sits below
    # the off-track row-mate's raised top instead of matching it.
    tall_content_top = min(
        graph.stations[sid].y
        for sid in tall.station_ids
        if not graph.stations[sid].is_port and not is_bypass_v(sid)
    )
    assert tall.bbox_y == pytest.approx(tall_content_top - SECTION_Y_PADDING), (
        f"tall bbox_y={tall.bbox_y} should hug its content "
        f"(top {tall_content_top} - padding {SECTION_Y_PADDING})"
    )
    assert tall.bbox_y > short.bbox_y + 1, (
        f"tall bbox_y={tall.bbox_y} should sit below short's off-track-raised "
        f"top ({short.bbox_y})"
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


def test_no_upward_inter_section_route_across_rowspan_neighbour():
    """Inter-section bundles must not detour upward over rowspan neighbours.

    Repro of the PR #271 regression on 04_directions / rnaseq_auto /
    fold_fan_across: a TB-direction grid section with grid_row_span>1
    sat next to row-mate LR sections.  Compaction lifted the TB
    section's entry port upward (out of trunk Y) so the inter-section
    bundle had to route upward across the section gap rather than
    continuing horizontally along the row-mate trunk Y.

    For every cross-section port-to-port edge that bridges adjacent
    grid columns within the same grid row, the route's vertical
    deflection at the boundary (exit_y - entry_y) must be bounded:
    the bundle may step down to reach a target below it, but it
    cannot step UP across the gap by more than one ``y_spacing`` --
    that's the visible "lines route up unnecessarily" pattern.

    Skips edges where the target is below the source (no upward
    detour) and edges where the source section spans multiple grid
    rows in its TB direction (which legitimately drops content down).
    """
    text = Path(__file__).parent.parent.joinpath("examples/guide/04_directions.mmd")
    graph = parse_metro_mermaid(text.read_text())
    y_spacing = 40.0
    compute_layout(graph, y_spacing=y_spacing)

    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if src is None or tgt is None:
            continue
        if not (src.is_port and tgt.is_port):
            continue
        ssec = graph.sections.get(src.section_id)
        tsec = graph.sections.get(tgt.section_id)
        if ssec is None or tsec is None or ssec is tsec:
            continue
        # Only check adjacent same-row neighbours (where the trunk Y is
        # expected to flow horizontally).  Skip TB rowspan source
        # sections because they legitimately drop content down across
        # multiple grid rows.
        if ssec.grid_row != tsec.grid_row or abs(ssec.grid_col - tsec.grid_col) > 1:
            continue
        if ssec.direction == "TB" and ssec.grid_row_span > 1:
            continue
        # Upward detour: src.y > tgt.y means the bundle has to climb
        # from exit to entry across the gap.  Anything bigger than a
        # half-spacing rounds up to "visibly kinked upward".
        upward = src.y - tgt.y
        threshold = y_spacing / 2
        assert upward <= threshold, (
            f"edge {edge.source}->{edge.target} routes upward by {upward:.1f}px "
            f"between adjacent same-row sections {ssec.id}->{tsec.id} "
            f"(exit_y={src.y:.1f}, entry_y={tgt.y:.1f}, "
            f"threshold={threshold}); "
            f"inter-section bundle should stay roughly horizontal"
        )


def test_section_content_y_stable_under_neutral_layout():
    """TB rowspan>1 content must keep its natural Y under compaction.

    Sanity check for the PR #271 regression: when a TB section spans
    multiple grid rows, its content stations occupy a vertical column
    spanning the row range.  Compaction must NOT lift the column up to
    the bbox top, because doing so:

      1. moves the LAST TB station above the bottom-row trunk Y where
         it should align with the row-mate entry port,
      2. forces the row-0 row-mates to route their bundle upward to
         meet the TB section's lifted entry.

    Uses the 04_directions fixture (TB rowspan=2 postprocessing
    section).  The TB section's middle/last stations should stay at
    their natural rowspan-aligned Ys: the column's Y span must match
    the row trunk-Y span (top row trunk Y to bottom row trunk Y),
    not be compacted to the bbox top.
    """
    text = Path(__file__).parent.parent.joinpath("examples/guide/04_directions.mmd")
    graph = parse_metro_mermaid(text.read_text())
    y_spacing = 40.0
    compute_layout(graph, y_spacing=y_spacing)
    post = graph.sections["postprocessing"]
    assert post.grid_row_span == 2, "fixture invariant: postprocessing rowspan=2"
    assert post.direction == "TB", "fixture invariant: postprocessing TB"
    # rna_analysis (row 0) and dna_analysis (row 1) are the LR
    # row-mates that share the inter-section bundle with postprocessing.
    row0_trunk = graph.stations["star"].y  # rna_analysis row 0 trunk
    row1_trunk = graph.stations["bwa"].y  # dna_analysis row 1 trunk
    # The last TB station (bedtools) lands at the bottom of the column;
    # natural placement puts it at or below the row 1 trunk Y so the
    # inter-section bundle from row 0 to postprocessing.last doesn't
    # have to route upward.  Compaction lifts bedtools above row 1
    # trunk Y, which is the visible regression.
    bedtools_y = graph.stations["bedtools"].y
    # Tolerance: bedtools may be slightly above row 1 trunk by less
    # than y_spacing/2 due to bbox padding accounting, but anything
    # more than y_spacing above is the compaction regression.
    above_row1 = row1_trunk - bedtools_y
    assert above_row1 <= y_spacing, (
        f"TB rowspan section's bottom station shifted upward: "
        f"bedtools.y={bedtools_y:.1f} row1_trunk={row1_trunk:.1f} "
        f"(row0_trunk={row0_trunk:.1f}, y_spacing={y_spacing}); "
        f"bedtools is {above_row1:.1f}px above row1_trunk -- "
        f"compaction lifted TB content above its natural row span"
    )


def test_rowspan_trim_doesnt_misalign_tb_bbox_bottom():
    """``_shrink_bboxes_to_content_bottom`` (Stage 6.13) must not undo
    ``_align_tb_section_bbox_bottoms`` (Stage 6.5), nor trim a
    row-spanning TB section's bbox bottom above a known row-mate it
    visually shares a bottom edge with.

    Anchored on the two fixtures where this regression was first
    observed:

    - ``fold_double``: section #4 (TB ``calling``) feeds into RL row
      ``hard_filter`` (#5); their bbox bottoms must match.  Section
      #8 (TB ``integration``) feeds into LR row ``reporting`` (#9);
      their bbox bottoms must match too.
    - ``04_directions``: section #4 (TB ``postprocessing``,
      ``grid_row_span=2``) shares a bottom edge with ``reporting``
      (#5) one grid row below.
    """
    root = Path(__file__).parent.parent

    def _bots(graph, *sids):
        return {
            sid: graph.sections[sid].bbox_y + graph.sections[sid].bbox_h for sid in sids
        }

    fold = parse_metro_mermaid(
        (root / "examples/topologies/fold_double.mmd").read_text()
    )
    compute_layout(fold)
    fb = _bots(fold, "calling", "hard_filter", "integration", "reporting")
    assert fb["calling"] >= fb["hard_filter"] - 0.5, (
        f"fold_double: calling (TB #4) bbox bottom {fb['calling']:.1f} above "
        f"row-mate hard_filter (#5) bottom {fb['hard_filter']:.1f}"
    )
    assert fb["integration"] >= fb["reporting"] - 0.5, (
        f"fold_double: integration (TB #8) bbox bottom {fb['integration']:.1f} "
        f"above row-mate reporting (#9) bottom {fb['reporting']:.1f}"
    )

    directions = parse_metro_mermaid(
        (root / "examples/guide/04_directions.mmd").read_text()
    )
    compute_layout(directions)
    db = _bots(directions, "postprocessing", "reporting")
    assert db["postprocessing"] >= db["reporting"] - 0.5, (
        f"04_directions: postprocessing (TB #4) bbox bottom "
        f"{db['postprocessing']:.1f} above row-mate reporting (#5) bottom "
        f"{db['reporting']:.1f}"
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


def test_uneven_reconverging_diamond_short_branch_on_trunk():
    """An uneven reconverging diamond keeps its short branch on the trunk.

    samtools_vc forks into a one-hop branch (varlociraptor) and a three-hop
    branch (finalise -> normalise -> consensus) that both rejoin at merge.
    The short branch shares the trunk Y with the fork and join nodes, and
    the long branch sits exactly one y-spacing below the trunk.
    """
    text = (
        Path(__file__).resolve().parent / "fixtures" / "uneven_diamond.mmd"
    ).read_text()
    graph = parse_metro_mermaid(text)
    y_spacing = 40.0
    compute_layout(graph, y_spacing=y_spacing)

    trunk_y = graph.stations["samtools_vc"].y
    assert graph.stations["varlociraptor"].y == trunk_y
    assert graph.stations["merge"].y == trunk_y
    for long_branch in ("finalise", "normalise", "consensus"):
        assert graph.stations[long_branch].y == trunk_y + y_spacing


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
    """Terminal section's full-bundle column fans symmetrically with --center-ports."""
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
        f"a and b should be 2*y_spacing apart: |b-a|={abs(by - ay)}"
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
    """Each off-track input sits above its consumer.

    When two off-track inputs in the same section feed different
    consumer stations, each input must sit above (smaller Y than) its
    consumer.  When the two consumers share a Y - so the section's
    trunk is straight - the inputs stack above the shared trunk row at
    consecutive ``y_spacing`` slots so they don't overlap.  When the
    consumers sit at different Ys (e.g. parallel branches), each input
    anchors to its own consumer at ``consumer_y - y_spacing``.
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
    # Each input sits above its consumer (smaller Y).
    assert in_a_y < upper_y, f"in_a y={in_a_y} should sit above upper y={upper_y}"
    assert in_b_y < lower_y, f"in_b y={in_b_y} should sit above lower y={lower_y}"
    # Inputs sit at different Ys (not a uniform band).
    assert in_a_y != in_b_y
    # Inputs stack at ``y_spacing`` pitch (one slot apart).
    assert abs(abs(in_a_y - in_b_y) - 55) < 0.5, (
        f"in_a y={in_a_y}, in_b y={in_b_y} should differ by one y_spacing slot"
    )


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
    assert ys[0] == pytest.approx(mid_y - 2 * 55), (
        f"Topmost input y={ys[0]} should be mid_y - 2*y_spacing = {mid_y - 2 * 55}"
    )
    assert ys[1] == pytest.approx(mid_y - 55), (
        f"Lower input y={ys[1]} should be mid_y - y_spacing = {mid_y - 55}"
    )


def test_off_track_convergence_keeps_consumer_on_trunk():
    """The off_track_convergence gallery fixture: four off-track inputs all
    consumed by one in-section station (`align`) must not pull `align` off
    the row trunk.

    Locks in the fix for the displacement bug: prior to the off-track
    exclusion in ``assign_tracks`` / ``_layout_single_section``, ``align``
    sat ~230 px below the row trunk because the in-section track grouping
    treated the four file inputs as ordinary on-track stations on the same
    line and pushed the consumer off-track.
    """
    fixture = (
        Path(__file__).resolve().parent.parent
        / "examples"
        / "topologies"
        / "off_track_convergence.mmd"
    )
    graph = parse_metro_mermaid(fixture.read_text())
    compute_layout(graph, x_spacing=70, y_spacing=55)
    # Reads -> Prepare -> Align -> Annotate -> Report should sit on one
    # horizontal trunk Y.
    reads_y = graph.stations["reads"].y
    prep_y = graph.stations["prep"].y
    align_y = graph.stations["align"].y
    annotate_y = graph.stations["annotate"].y
    report_y = graph.stations["report"].y
    assert reads_y == pytest.approx(prep_y), f"reads y={reads_y} vs prep y={prep_y}"
    assert prep_y == pytest.approx(align_y), f"prep y={prep_y} vs align y={align_y}"
    assert align_y == pytest.approx(annotate_y), (
        f"align y={align_y} vs annotate y={annotate_y}"
    )
    assert annotate_y == pytest.approx(report_y), (
        f"annotate y={annotate_y} vs report y={report_y}"
    )
    # The four file inputs stack above align at consecutive y_spacing slots.
    icons = sorted(
        graph.stations[s].y for s in ("ref_in", "gtf_in", "vcf_in", "bed_in")
    )
    # Each icon sits above the consumer; consecutive icons differ by one
    # y_spacing slot.
    for icon_y in icons:
        assert icon_y < align_y, (
            f"icon y={icon_y} should sit above consumer align y={align_y}"
        )
    for a, b in zip(icons, icons[1:]):
        assert b - a == pytest.approx(55), (
            f"icons at y={a} and y={b} should stack at y_spacing=55 apart"
        )


def test_cli_diamond_style_default(tmp_path):
    """diamond_style defaults to straight (no flag needed)."""
    from click.testing import CliRunner

    from nf_metro.cli import cli

    mmd = tmp_path / "diamond.mmd"
    mmd.write_text(_diamond_section_text())
    out = tmp_path / "out.svg"
    runner = CliRunner()
    result = runner.invoke(cli, ["render", str(mmd), "-o", str(out)])
    assert result.exit_code == 0, result.output


def test_cli_diamond_style_symmetric(tmp_path):
    """--diamond-style symmetric reverts to symmetric behaviour."""
    from click.testing import CliRunner

    from nf_metro.cli import cli

    mmd = tmp_path / "diamond.mmd"
    mmd.write_text(_diamond_section_text())
    out = tmp_path / "out.svg"
    runner = CliRunner()
    result = runner.invoke(
        cli, ["render", str(mmd), "-o", str(out), "--diamond-style", "symmetric"]
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


# --- Port-terminus spacing (Stage 4.5) ---


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
    """Stage 4.5 must not introduce station-as-elbow violations.

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
    assert not errors, "station-as-elbow violations after Stage 4.5:\n" + "\n".join(
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

    from nf_metro.layout.section_placement import _min_gap_for_bundles

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
    min_needed = _min_gap_for_bundles([5])
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
# Stage-boundary guard tests
# ---------------------------------------------------------------------------

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
TOPOLOGIES_DIR = EXAMPLES_DIR / "topologies"


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

    def test_differentialabundance(self):
        """``differentialabundance.mmd`` is the only gallery fixture that
        combines ``center_ports: true`` with off-track terminus inputs
        and a sparse loop-side station; exercises the bisection guard
        phase-gating policy.
        """
        self._layout_validated((EXAMPLES_DIR / "differentialabundance.mmd").read_text())

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


class TestShiftGraphIntoCanvas:
    """Behavioural invariants of ``_shift_graph_into_canvas``.

    The helper is called explicitly from three Pass C sub-step
    sites in ``_compute_section_layout``.  Each call must be safe
    regardless of whether the preceding bbox-growing helper actually
    grew anything; the helper's own no-op guard makes the call
    idempotent.
    """

    def test_idempotent_when_already_in_canvas(self):
        from nf_metro.layout import engine
        from nf_metro.parser.model import MetroGraph, Section, Station

        # Section already sits above its padding zone; the shift is a no-op.
        graph = MetroGraph()
        graph.sections["s1"] = Section(
            id="s1", name="S1", bbox_x=0, bbox_y=200, bbox_w=100, bbox_h=80
        )
        graph.stations["a"] = Station(id="a", label="A", x=10, y=220)

        engine._shift_graph_into_canvas(graph, section_y_padding=20.0)

        # No shift applied, coords unchanged.
        assert graph.stations["a"].y == 220
        assert graph.sections["s1"].bbox_y == 200

    def test_shifts_overflowing_section_down_to_padding(self):
        from nf_metro.layout import engine
        from nf_metro.parser.model import MetroGraph, Section, Station

        graph = MetroGraph()
        graph.sections["s1"] = Section(
            id="s1", name="S1", bbox_x=0, bbox_y=-30, bbox_w=100, bbox_h=80
        )
        graph.stations["a"] = Station(id="a", label="A", x=10, y=10)

        engine._shift_graph_into_canvas(graph, section_y_padding=20.0)

        # bbox_y was -30, padding 20 -> shift = 20 - (-30) = 50.
        assert graph.sections["s1"].bbox_y == 20
        assert graph.stations["a"].y == 60

    def test_double_call_is_safe(self):
        from nf_metro.layout import engine
        from nf_metro.parser.model import MetroGraph, Section, Station

        graph = MetroGraph()
        graph.sections["s1"] = Section(
            id="s1", name="S1", bbox_x=0, bbox_y=-30, bbox_w=100, bbox_h=80
        )
        graph.stations["a"] = Station(id="a", label="A", x=10, y=10)

        engine._shift_graph_into_canvas(graph, section_y_padding=20.0)
        first_bbox_y = graph.sections["s1"].bbox_y
        first_station_y = graph.stations["a"].y

        engine._shift_graph_into_canvas(graph, section_y_padding=20.0)

        # Second call is a no-op (section is now in canvas).
        assert graph.sections["s1"].bbox_y == first_bbox_y
        assert graph.stations["a"].y == first_station_y


class TestPassCBisection:
    """Bisection guards fired at each Pass C bisection boundary must
    identify the offending sub-stage, not the catch-all 'after final'
    label.

    Without bisection, a regression introduced by e.g. Stage 6.14 surfaces
    as ``after final: position clash: ...``; the maintainer
    must manually bisect by toggling phases to find the culprit.  With
    bisection, the same regression surfaces immediately as
    ``after Stage 6.14: position clash: ...``.
    """

    # One representative fixture per check: simple two-station section
    # with no off-track inputs so the corruption-induced overlap is the
    # first guard violation encountered, regardless of which phase is
    # being tested.
    _MMD = (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro line: alt | Alt | #00ff00\n"
        "graph LR\n"
        "    subgraph s1 [S1]\n"
        "        a[A]\n"
        "        a2[A2]\n"
        "        a -->|main| a2\n"
        "    end\n"
        "    subgraph s2 [S2]\n"
        "        b[B]\n"
        "        b2[B2]\n"
        "        b -->|main| b2\n"
        "    end\n"
        "    a2 -->|main,alt| b\n"
    )

    # Corruption-phase -> expected surfacing checkpoint.  When the
    # corruption phase is at or after the overlap-guard's first valid
    # checkpoint (``after Stage 6.4``), bisection localises to the
    # corruption phase itself.  Corruptions injected before Stage 6.4
    # surface at ``after Stage 6.4`` (the first un-gated overlap check).
    @pytest.mark.parametrize(
        "corruption_helper,expected_phase",
        [
            ("_lift_off_track_stations", "after Stage 6.4"),
            ("_top_align_row_bboxes_only", "after Stage 6.4"),
            ("_compact_row_content_to_bbox_top", "after Stage 6.4"),
            ("_snap_all_y_to_grid", "after Stage 6.4"),
            ("_align_terminus_to_upstream", "after Stage 6.10"),
            ("_shrink_bboxes_to_content_bottom", "after Stage 6.13"),
            ("_snap_canvas_y_to_grid", "after Stage 6.15"),
        ],
    )
    def test_overlap_localises_to_phase(
        self, monkeypatch, corruption_helper, expected_phase
    ):
        from nf_metro.layout import engine

        original = getattr(engine, corruption_helper)

        def corrupt(graph, *args, **kwargs):
            result = original(graph, *args, **kwargs)
            # Move 'a' to land on 'a2', producing an overlap that
            # ``_guard_no_station_overlap`` must catch at the next
            # un-gated bisection checkpoint.
            a = graph.stations.get("a")
            a2 = graph.stations.get("a2")
            if a is not None and a2 is not None:
                a.x = a2.x
                a.y = a2.y
            return result

        _patch_layout_helper(monkeypatch, corruption_helper, corrupt)

        graph = parse_metro_mermaid(self._MMD)
        with pytest.raises(engine.PhaseInvariantError) as excinfo:
            compute_layout(graph, validate=True)

        msg = str(excinfo.value)
        assert msg.startswith(expected_phase + ":"), (
            f"Expected bisection to surface at {expected_phase!r}, "
            f"but error was: {msg!r}"
        )

    def test_clean_layout_does_not_fire_bisection_guards(self):
        """Sanity check: an unpatched layout must pass all bisection
        checkpoints, confirming the guard set is genuinely empty-render-
        diff for the gallery's normal-shape topologies.
        """
        graph = parse_metro_mermaid(self._MMD)
        compute_layout(graph, validate=True)

    # Minimal center_ports fixture: enters the `if graph.center_ports:`
    # block at Stage 6.7 so the "after Stage 6.9" checkpoint actually
    # fires.  Plain enough not to need full-bundle reanchoring, so the
    # helpers there are effective no-ops and the checkpoint is reached
    # cleanly.
    _MMD_CENTER_PORTS = (
        "%%metro center_ports: true\n"
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph s1 [S1]\n"
        "        a[A]\n"
        "        a2[A2]\n"
        "        a -->|main| a2\n"
        "    end\n"
        "    subgraph s2 [S2]\n"
        "        b[B]\n"
        "    end\n"
        "    a2 -->|main| b\n"
    )

    def test_phase_13h_subphase_checkpoint_fires_on_center_ports(self, monkeypatch):
        """Stage 6.9's bisection checkpoint surfaces a regression
        introduced by ``_top_align_row_bboxes_only`` inside the
        ``if center_ports:`` branch.

        This exercises a code path the default ``_MMD`` fixture (no
        ``center_ports``) cannot reach.  The corruption runs *after*
        the Stage 5.3 checkpoint to avoid a false positive there: we
        corrupt only on the second invocation of
        ``_top_align_row_bboxes_only``, which lands inside Stage 6.9.
        """
        from nf_metro.layout import engine

        original = engine._top_align_row_bboxes_only
        call_count = {"n": 0}

        def corrupt(graph, *args, **kwargs):
            result = original(graph, *args, **kwargs)
            call_count["n"] += 1
            # First call is Stage 5.3 (always runs); second is Stage 6.9
            # (only on center_ports graphs).  Corrupt only on the second
            # to hit the Stage 6.9 checkpoint specifically.
            if call_count["n"] >= 2:
                a = graph.stations.get("a")
                a2 = graph.stations.get("a2")
                if a is not None and a2 is not None:
                    a.x = a2.x
                    a.y = a2.y
            return result

        _patch_layout_helper(monkeypatch, "_top_align_row_bboxes_only", corrupt)

        graph = parse_metro_mermaid(self._MMD_CENTER_PORTS)
        with pytest.raises(engine.PhaseInvariantError) as excinfo:
            compute_layout(graph, validate=True)

        msg = str(excinfo.value)
        assert msg.startswith("after Stage 6.9:"), (
            f"Expected bisection to identify Stage 6.9, but error was: {msg!r}"
        )
        assert call_count["n"] == 2, (
            f"Expected exactly 2 calls to _top_align_row_bboxes_only "
            f"(Stage 5.3 + Stage 6.9), got {call_count['n']}"
        )

    def test_gated_overlap_guard_fires_at_later_bisection_checkpoints(
        self, monkeypatch
    ):
        """An overlap injected at Stage 6.15 must surface at the
        ``after Stage 6.15`` bisection checkpoint (not deferred to the
        final block).

        Confirms ``_BISECTION_FIRST_VALID`` only delays a guard's
        first valid checkpoint -- once past the threshold (``after
        Stage 6.4`` for the overlap guard), the guard fires at every
        subsequent bisection checkpoint.
        """
        from nf_metro.layout import engine

        original = engine._snap_canvas_y_to_grid

        def corrupt(graph, *args, **kwargs):
            result = original(graph, *args, **kwargs)
            a = graph.stations.get("a")
            a2 = graph.stations.get("a2")
            if a is not None and a2 is not None:
                a.x = a2.x
                a.y = a2.y
            return result

        _patch_layout_helper(monkeypatch, "_snap_canvas_y_to_grid", corrupt)

        graph = parse_metro_mermaid(self._MMD)
        with pytest.raises(engine.PhaseInvariantError) as excinfo:
            compute_layout(graph, validate=True)

        msg = str(excinfo.value)
        assert msg.startswith("after Stage 6.15:"), (
            f"Expected bisection to surface at 'after Stage 6.15' "
            f"(overlap guard runs at every checkpoint from Stage 6.4 "
            f"onward), but error was: {msg!r}"
        )

    @pytest.mark.parametrize(
        "guard_name,first_valid",
        [
            ("_guard_stations_in_sections", "after Stage 5.3"),
            ("_guard_no_station_overlap", "after Stage 6.4"),
            ("_guard_no_coincident_station_coords", "after Stage 6.4"),
            ("_guard_no_line_crosses_non_consumer", "after Stage 6.14"),
        ],
    )
    def test_bisection_first_valid_threshold(self, guard_name, first_valid):
        """``_BISECTION_FIRST_VALID`` thresholds must reference real
        Pass C bisection checkpoints; ``_bisection_should_run`` must
        skip the guard at the preceding checkpoint and run it at the
        threshold.
        """
        from nf_metro.layout import engine

        assert first_valid in engine._PASS_C_BISECTION_ORDER, (
            f"Threshold {first_valid!r} not a known Pass C checkpoint"
        )
        assert engine._BISECTION_FIRST_VALID[guard_name] == first_valid

        idx = engine._PASS_C_BISECTION_ORDER.index(first_valid)
        assert engine._bisection_should_run(guard_name, first_valid) is True
        if idx > 0:
            prev_phase = engine._PASS_C_BISECTION_ORDER[idx - 1]
            assert engine._bisection_should_run(guard_name, prev_phase) is False
        # Final-block phase is not in _PASS_C_BISECTION_ORDER; guard always runs.
        assert engine._bisection_should_run(guard_name, "after final") is True


def test_bundles_in_gap_dedupes_fanout_to_multiple_columns():
    """A line fanning from one source to several target columns occupies a
    single coalesced channel in the source-side gap, so it must be counted
    as ONE bundle, not one per target column.

    Regression: differentialabundance fans rnaseq/affy/geo/maxquant from a
    single junction to functional (col 2), plots (col 2) and reporting
    (col 3).  Keying down-channels by (src_col, tgt_col) counted the
    col1->col2 and col1->col3 channels as two bundles of the same four
    lines, over-widening the col1|col2 gap and pushing the (single) bundle
    off-centre.
    """
    from nf_metro.layout.section_placement import _bundles_in_gap

    path = (
        Path(__file__).resolve().parent.parent
        / "examples"
        / "differentialabundance.mmd"
    )
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    col_assign = {sid: s.grid_col for sid, s in graph.sections.items()}

    bundles = _bundles_in_gap(graph, col_assign, 1, 2)
    assert bundles == [4], (
        f"expected one coalesced 4-line down-bundle in the col1|col2 gap, "
        f"got {bundles} (the four data lines fanning to cols 2 and 3 were "
        f"double-counted)"
    )


def test_bundles_in_gap_dedupes_synthetic_fanout():
    """Minimal synthetic: one source fanning the same line to an adjacent
    and a bypass column must count as a single source-side bundle."""
    from nf_metro.layout.section_placement import _bundles_in_gap

    mmd = (
        "%%metro line: a | A | #ff0000\n"
        "%%metro line: b | B | #00ff00\n"
        "%%metro grid: s0 | 0,0\n"
        "%%metro grid: s1 | 1,0\n"
        "%%metro grid: s2 | 2,0\n"
        "graph LR\n"
        "    subgraph s0 [S0]\n"
        "        n0[N0]\n"
        "    end\n"
        "    subgraph s1 [S1]\n"
        "        n1[N1]\n"
        "    end\n"
        "    subgraph s2 [S2]\n"
        "        n2[N2]\n"
        "    end\n"
        "    n0 -->|a,b| n1\n"
        "    n0 -->|a,b| n2\n"
    )
    graph = parse_metro_mermaid(mmd)
    compute_layout(graph)
    col_assign = {sid: s.grid_col for sid, s in graph.sections.items()}
    # s0 -> s1 (adjacent) and s0 -> s2 (bypass) share lines a,b and coalesce
    # in the s0|s1 gap: one 2-line bundle, not two.
    bundles = _bundles_in_gap(graph, col_assign, 0, 1)
    assert bundles == [2], f"expected one coalesced 2-line bundle, got {bundles}"


@pytest.mark.parametrize(
    "fixture", ["differentialabundance.mmd", "differentialabundance_default.mmd"]
)
def test_single_bundle_inter_section_gap_is_centered(fixture):
    """A lone vertical bundle in an inter-section gap sits centred: the
    clearance from the left section edge equals the clearance to the right
    section edge (the gap is x + bundle_width + y with x == y).

    Fails when the gap is over-sized for a phantom second bundle, which
    pushes the real bundle hard against the source side.
    """
    from nf_metro.layout.routing import route_edges
    from nf_metro.layout.routing.common import column_gap_edges

    path = Path(__file__).resolve().parent.parent / "examples" / fixture
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    routes = route_edges(graph)

    # functional is col 2; inspect the gap immediately to its left.
    gap_left, gap_right = column_gap_edges(graph, 1, 2)
    seg_xs = set()
    for rp in routes:
        for (x0, y0), (x1, y1) in zip(rp.points, rp.points[1:]):
            if abs(x1 - x0) < 0.5 and abs(y1 - y0) > 5:
                xm = (x0 + x1) / 2
                if gap_left - 2 <= xm <= gap_right + 2:
                    seg_xs.add(round(xm, 1))
    assert seg_xs, "expected a vertical bundle in the col1|col2 gap"
    lo, hi = min(seg_xs), max(seg_xs)
    left_clear = lo - gap_left
    right_clear = gap_right - hi
    assert abs(left_clear - right_clear) <= 3.0, (
        f"{fixture}: bundle off-centre in col1|col2 gap — "
        f"left clearance {left_clear:.1f}px vs right {right_clear:.1f}px "
        f"(gap W={gap_right - gap_left:.1f})"
    )


def _gap_vertical_channels(graph, routes, col_a, col_b):
    """Return ``[(x, down, line_id)]`` for vertical channel segments in a gap.

    Picks every vertical channel of an inter-section route whose x falls
    inside the ``(col_a, col_b)`` inter-column gap, tagged with its
    direction (``down`` True for a southbound segment) and line id.
    """
    from nf_metro.layout.constants import COORD_TOLERANCE
    from nf_metro.layout.routing.common import column_gap_edges

    gap_left, gap_right = column_gap_edges(graph, col_a, col_b)
    out: list[tuple[float, bool, str]] = []
    for rp in routes:
        if not rp.is_inter_section:
            continue
        for (x0, y0), (x1, y1) in zip(rp.points, rp.points[1:]):
            if abs(x1 - x0) < COORD_TOLERANCE and abs(y1 - y0) > COORD_TOLERANCE:
                if gap_left - 2 <= x0 <= gap_right + 2:
                    out.append((round(x0, 2), y1 > y0, rp.line_id))
    return out


@pytest.mark.parametrize(
    "fixture, col_a, col_b, n_distinct",
    [
        # differentialabundance: four data lines fan downward in the gap
        # left of `functional`, each feeding BOTH plots and reporting.  The
        # two segments of a given line OVERLAY at one x, so the bundle is
        # FOUR distinct lines (not eight separate channels), consecutive
        # distinct lines OFFSET_STEP apart.
        ("examples/differentialabundance.mmd", 1, 2, 4),
    ],
)
def test_same_direction_lines_in_gap_bundle_at_offset_step(
    fixture, col_a, col_b, n_distinct
):
    """Same-direction inter-section channels sharing a gap form one
    concentric bundle keyed on DISTINCT line ids: distinct lines are
    OFFSET_STEP apart, and every segment carrying the same line id overlays
    at that line's single x (so a line feeding several targets does not
    claim multiple slots)."""
    from nf_metro.layout.constants import OFFSET_STEP
    from nf_metro.layout.routing import route_edges

    path = Path(__file__).resolve().parent.parent / fixture
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    routes = route_edges(graph)

    down = [
        (x, lid)
        for x, is_down, lid in _gap_vertical_channels(graph, routes, col_a, col_b)
        if is_down
    ]
    # Every segment of a given line must share that line's single x.
    per_line: dict[str, set[float]] = {}
    for x, lid in down:
        per_line.setdefault(lid, set()).add(x)
    for lid, xs in per_line.items():
        assert len(xs) == 1, (
            f"{fixture}: line {lid} occupies multiple xs {sorted(xs)} in "
            f"col{col_a}|col{col_b} gap; same-line segments must overlay"
        )

    distinct_xs = sorted(next(iter(xs)) for xs in per_line.values())
    assert len(distinct_xs) == n_distinct, (
        f"{fixture}: expected {n_distinct} distinct downward lines in "
        f"col{col_a}|col{col_b} gap, got {len(distinct_xs)} at {distinct_xs}"
    )
    gaps = [b - a for a, b in zip(distinct_xs, distinct_xs[1:])]
    assert all(abs(g - OFFSET_STEP) < 0.5 for g in gaps), (
        f"{fixture}: distinct downward lines not bundled at OFFSET_STEP "
        f"({OFFSET_STEP}px); spacings {gaps} at xs {distinct_xs}"
    )


@pytest.mark.parametrize(
    "fixture, gap",
    [("section_diamond", (0, 1)), ("mixed_port_sides", (0, 1))],
)
def test_junction_sourced_bundle_centers_in_gap(fixture, gap):
    """A vertical channel sourced from a junction (not a section station)
    centres in its inter-column gap, same as a section-sourced one.

    Before: ``inter_column_channel_x`` only centred when both endpoints had
    a section; junction sources fell back to near-source placement and hugged
    one edge (~4px off in section_diamond / mixed_port_sides).  Now junction
    columns are resolved (when adjacent to the target) and the channel centres.
    """
    from nf_metro.layout.routing import route_edges
    from nf_metro.layout.routing.common import column_gap_edges

    p = None
    for d in ("topologies", ""):
        cand = (
            Path(__file__).resolve().parent.parent / "examples" / d / f"{fixture}.mmd"
        )
        if cand.is_file():
            p = cand
            break
    assert p is not None, f"fixture {fixture} not found"

    graph = parse_metro_mermaid(p.read_text())
    compute_layout(graph)
    routes = route_edges(graph)
    gl, gr = column_gap_edges(graph, *gap)
    xs = set()
    for rp in routes:
        for (x0, y0), (x1, y1) in zip(rp.points, rp.points[1:]):
            if abs(x1 - x0) < 0.5 and abs(y1 - y0) > 5:
                xm = (x0 + x1) / 2
                if gl - 2 <= xm <= gr + 2:
                    xs.add(round(xm, 1))
    assert xs, f"{fixture}: expected a vertical bundle in gap {gap}"
    lo, hi = min(xs), max(xs)
    left_clear, right_clear = lo - gl, gr - hi
    assert abs(left_clear - right_clear) <= 3.0, (
        f"{fixture}: junction-sourced bundle off-centre in gap {gap} — "
        f"left {left_clear:.1f}px vs right {right_clear:.1f}px"
    )


def _all_segments(routes):
    """Return ``[(line_id, p0, p1)]`` for every segment of every route."""
    out = []
    for rp in routes:
        for p0, p1 in zip(rp.points, rp.points[1:]):
            out.append((rp.line_id, p0, p1))
    return out


def _count_inter_line_crossings(routes):
    """Count proper (interior) crossings between segments of distinct lines."""

    def ccw(a, b, c):
        return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])

    def crosses(p1, p2, p3, p4):
        d1, d2 = ccw(p3, p4, p1), ccw(p3, p4, p2)
        d3, d4 = ccw(p1, p2, p3), ccw(p1, p2, p4)
        return ((d1 > 1e-6 and d2 < -1e-6) or (d1 < -1e-6 and d2 > 1e-6)) and (
            (d3 > 1e-6 and d4 < -1e-6) or (d3 < -1e-6 and d4 > 1e-6)
        )

    segs = _all_segments(routes)
    n = 0
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            if segs[i][0] == segs[j][0]:
                continue
            if crosses(segs[i][1], segs[i][2], segs[j][1], segs[j][2]):
                n += 1
    return n


@pytest.mark.parametrize(
    "fixture",
    [
        "examples/differentialabundance.mmd",
        "examples/variant_calling.mmd",
        "examples/variant_calling_tuned.mmd",
        "examples/variantprioritization.mmd",
        "examples/genomeassembly.mmd",
    ],
)
def test_normalization_adds_no_inter_line_crossings(fixture):
    """The gap-channel normalization post-pass must never INTRODUCE a
    crossing between two different lines that the un-normalized routing did
    not already have.  (It may remove crossings; it must not add any.)"""
    import nf_metro.layout.routing.core as core
    from nf_metro.layout.routing import route_edges

    path = Path(__file__).resolve().parent.parent / fixture
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)

    orig = core._materialize_gap_slots
    core._materialize_gap_slots = lambda *a, **k: None
    try:
        pre = _count_inter_line_crossings(route_edges(graph))
    finally:
        core._materialize_gap_slots = orig
    post = _count_inter_line_crossings(route_edges(graph))

    assert post <= pre, (
        f"{fixture}: normalization introduced crossings "
        f"(pre-normalize {pre}, post-normalize {post})"
    )


@pytest.mark.parametrize(
    "fixture",
    [
        "examples/differentialabundance.mmd",
        "examples/genomeassembly.mmd",
    ],
)
def test_same_line_segments_in_gap_bundle_share_x(fixture):
    """Within an inter-column gap, every vertical channel carrying the same
    line id sits at one x (overlaid), so a fan whose line feeds several
    targets does not occupy several parallel slots in the bundle."""
    from nf_metro.layout.constants import COORD_TOLERANCE
    from nf_metro.layout.routing import route_edges
    from nf_metro.layout.routing.common import column_gap_edges

    path = Path(__file__).resolve().parent.parent / fixture
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    routes = route_edges(graph)

    cols = sorted({s.grid_col for s in graph.sections.values() if s.bbox_w > 0})
    channels_seen = 0
    for lo, hi in zip(cols, cols[1:]):
        if hi != lo + 1:
            continue
        gl, gr = column_gap_edges(graph, lo, hi)
        # (line_id, down) -> set of xs of its channel segments in this gap
        per: dict[tuple[str, bool], set[float]] = {}
        for rp in routes:
            if not rp.is_inter_section:
                continue
            for (x0, y0), (x1, y1) in zip(rp.points, rp.points[1:]):
                if abs(x1 - x0) >= COORD_TOLERANCE or abs(y1 - y0) <= COORD_TOLERANCE:
                    continue
                if gl - 2 <= x0 <= gr + 2:
                    per.setdefault((rp.line_id, y1 > y0), set()).add(round(x0, 2))
        for (lid, _down), xs in per.items():
            channels_seen += 1
            # Same-line segments must overlay (allow sub-pixel rounding noise).
            spread = max(xs) - min(xs)
            assert spread < COORD_TOLERANCE, (
                f"{fixture}: line {lid} occupies {sorted(xs)} in gap "
                f"{lo}|{hi}; same-line segments must overlay"
            )
    assert channels_seen, f"{fixture}: expected at least one gap channel (sanity check)"


def test_around_section_diversion_up_leg_centers_in_row_gap():
    """A U-shaped diversion routing below a section centres its up-leg in the
    *row-aware* inter-section gap, not against the column-wide extent.

    Regression: variantbenchmarking diverts the truth/test lines below
    Preprocessing from Inputs to Variant Normalization.  output_processing
    sits in the same column (row 1) and extends far right, so the column-wide
    gap centre was ~873 and the up-leg hugged Normalization (~738).  With
    row-aware gap edges + symmetric placement the up-leg centres on the
    row-0 Preprocessing|Normalization gap (~714).
    """
    from nf_metro.layout.routing import route_edges
    from nf_metro.layout.routing.common import column_gap_midpoint

    path = (
        Path(__file__).resolve().parent.parent / "examples" / "variantbenchmarking.mmd"
    )
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    routes = route_edges(graph)

    up_leg_x = None
    for rp in routes:
        edge = getattr(rp, "edge", None)
        if (
            not edge
            or edge.source != "__junction_13"
            or "normalization" not in edge.target
        ):
            continue
        # the up-leg is the rightmost vertical segment of the diversion
        for (x0, y0), (x1, y1) in zip(rp.points, rp.points[1:]):
            if abs(x1 - x0) < 0.5 and abs(y1 - y0) > 5:
                xm = (x0 + x1) / 2
                if up_leg_x is None or xm > up_leg_x:
                    up_leg_x = xm
    assert up_leg_x is not None, "expected a diversion to normalization"

    row0_center = column_gap_midpoint(graph, 1, 2, row=0)
    assert abs(up_leg_x - row0_center) <= 4.0, (
        f"diversion up-leg x={up_leg_x:.1f} not centred on row-0 gap "
        f"center {row0_center:.1f} (off {abs(up_leg_x - row0_center):.1f})"
    )


def test_multi_path_gap_separates_by_b_and_centers():
    """When a downward and an upward path share one inter-section gap, they
    sit BUNDLE_TO_BUNDLE_CLEARANCE (B) apart, centred as a group
    (A + w1 + B + w2 + A).

    03b_fan_in_merge's StepA|StepB gap carries a down-path (junction_7) and
    an up-path (junction_6); they previously overlapped (~5px) and now
    separate by B and straddle the gap centre.
    """
    from nf_metro.layout.constants import BUNDLE_TO_BUNDLE_CLEARANCE
    from nf_metro.layout.routing import route_edges
    from nf_metro.layout.routing.common import column_gap_edges

    path = (
        Path(__file__).resolve().parent.parent
        / "examples"
        / "guide"
        / "03b_fan_in_merge.mmd"
    )
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    routes = route_edges(graph)
    gl, gr = column_gap_edges(graph, 1, 2, row=0)
    center = (gl + gr) / 2

    down_x = up_x = None
    for rp in routes:
        for (x0, y0), (x1, y1) in zip(rp.points, rp.points[1:]):
            if abs(x1 - x0) < 0.5 and abs(y1 - y0) > 5:
                xm = (x0 + x1) / 2
                if gl - 3 <= xm <= gr + 3:
                    if y1 > y0:
                        down_x = xm
                    else:
                        up_x = xm
    assert down_x is not None and up_x is not None, "expected a down and an up path"
    sep = abs(up_x - down_x)
    assert sep >= BUNDLE_TO_BUNDLE_CLEARANCE - 1, (
        f"paths only {sep:.1f}px apart; expected >= B={BUNDLE_TO_BUNDLE_CLEARANCE}"
    )
    pair_mid = (down_x + up_x) / 2
    assert abs(pair_mid - center) <= 3.0, (
        f"path pair midpoint {pair_mid:.1f} not centred on gap {center:.1f}"
    )


def test_cross_col_top_entry_port_on_boundary():
    """A same-row cross-column producer into a TOP entry keeps the port on boundary.

    Regression for issue #740: ``_align_tb_entry_port`` was dragging the TOP
    port's Y to the source level (the section's vertical centre) instead of
    snapping it to the section's top boundary.  The guard
    ``_guard_ports_on_boundaries`` then fired under ``validate=True``.
    """
    mmd = """\
%%metro title: Cross-Column Top Entry
%%metro style: dark
%%metro line: l1 | Line 1 | #0570b0

graph LR
    subgraph producer [Producer]
        %%metro exit: right | l1
        p1[Step P1]
        p2[Step P2]
        p1 -->|l1| p2
    end

    subgraph consumer [Consumer]
        %%metro entry: top | l1
        c1[Step C1]
        c2[Step C2]
        c1 -->|l1| c2
    end

    p2 -->|l1| c1
"""
    from nf_metro.layout.constants import GUARD_TOLERANCE

    graph = parse_metro_mermaid(mmd)
    # validate=True re-runs _guard_ports_on_boundaries after every stage;
    # this must not raise PhaseInvariantError.
    compute_layout(graph, validate=True)

    # Additionally assert the TOP entry port sits exactly on the top boundary.
    for pid, port in graph.ports.items():
        if not port.is_entry:
            continue
        st = graph.stations.get(pid)
        sec = graph.sections.get(st.section_id or "") if st else None
        if sec is None:
            continue
        from nf_metro.parser.model import PortSide

        if port.side == PortSide.TOP:
            assert abs(st.y - sec.bbox_y) <= GUARD_TOLERANCE, (
                f"TOP port {pid!r} at y={st.y:.1f} not on top boundary "
                f"bbox_y={sec.bbox_y:.1f}"
            )
        elif port.side == PortSide.BOTTOM:
            assert abs(st.y - (sec.bbox_y + sec.bbox_h)) <= GUARD_TOLERANCE, (
                f"BOTTOM port {pid!r} at y={st.y:.1f} not on bottom boundary "
                f"bbox_y+bbox_h={sec.bbox_y + sec.bbox_h:.1f}"
            )


def test_cross_col_top_entry_channel_clears_title_band():
    """A topmost-row over-the-top entry channel sits between title and section.

    A same-row producer feeding a TOP entry routes up-and-over into the
    port.  In the topmost grid row the only thing above the section is the
    title, so the channel must sit below the title baseline yet above the
    section's top edge: clear of the title text, while entering the section
    from above as ``entry: top`` requests.
    """
    from nf_metro.layout.routing import route_edges
    from nf_metro.parser.model import PortSide
    from nf_metro.render.constants import TITLE_Y_OFFSET

    mmd = """\
%%metro title: Cross-Column Top Entry
%%metro style: dark
%%metro line: l1 | Line 1 | #0570b0

graph LR
    subgraph producer [Producer]
        %%metro exit: right | l1
        p1[Step P1]
        p2[Step P2]
        p1 -->|l1| p2
    end

    subgraph consumer [Consumer]
        %%metro entry: top | l1
        c1[Step C1]
        c2[Step C2]
        c1 -->|l1| c2
    end

    p2 -->|l1| c1
"""
    graph = parse_metro_mermaid(mmd)
    # validate=True exercises _guard_topmost_row_top_entry_hugs_section.
    compute_layout(graph, validate=True)

    top_port_id = next(
        pid
        for pid, port in graph.ports.items()
        if port.is_entry and port.side == PortSide.TOP
    )
    consumer_top = graph.sections["consumer"].bbox_y

    over_top = next(r for r in route_edges(graph) if r.edge.target == top_port_id)
    channel_y = min(y for _x, y in over_top.points)

    assert channel_y > TITLE_Y_OFFSET, (
        f"over-the-top channel y={channel_y:.1f} is above the title baseline "
        f"{TITLE_Y_OFFSET:.1f}; the route loops through the title text"
    )
    assert channel_y < consumer_top, (
        f"over-the-top channel y={channel_y:.1f} does not rise above the "
        f"consumer top edge {consumer_top:.1f}; the TOP entry is not honoured"
    )
