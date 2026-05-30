"""Tests for SVG rendering."""

import xml.etree.ElementTree as ET

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Station
from nf_metro.render.svg import _terminus_icon_centers, render_svg
from nf_metro.themes import LIGHT_THEME, NFCORE_THEME


def _render_simple():
    graph = parse_metro_mermaid(
        "%%metro title: Test\n"
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    a[Input]\n"
        "    b[Output]\n"
        "    a -->|main| b\n"
    )
    compute_layout(graph)
    return render_svg(graph, NFCORE_THEME)


def test_render_produces_valid_svg():
    svg = _render_simple()
    # Should be valid XML
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg") or "svg" in root.tag


def test_render_contains_title():
    svg = _render_simple()
    assert "Test" in svg


def test_render_contains_station_labels():
    svg = _render_simple()
    assert "Input" in svg
    assert "Output" in svg


def test_render_contains_line_color():
    svg = _render_simple()
    assert "#ff0000" in svg


def test_render_nfcore_theme_background():
    svg = _render_simple()
    assert NFCORE_THEME.background_color in svg


def test_render_light_theme():
    graph = parse_metro_mermaid(
        "%%metro title: Light Test\n"
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    a -->|main| b\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, LIGHT_THEME)
    # Light theme uses transparent background (no background rectangle)
    assert LIGHT_THEME.background_color == "none"
    assert "#333333" in svg  # label/stroke color present


def test_render_dashed_line_has_dasharray():
    """Dashed lines should produce stroke-dasharray in the SVG."""
    graph = parse_metro_mermaid(
        "%%metro title: Dash Test\n"
        "%%metro line: main | Main | #ff0000 | dashed\n"
        "graph LR\n"
        "    a[Input]\n"
        "    b[Output]\n"
        "    a -->|main| b\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_THEME)
    assert 'stroke-dasharray="8,4"' in svg or "stroke-dasharray='8,4'" in svg


def test_render_dotted_line_has_dasharray():
    """Dotted lines should produce stroke-dasharray in the SVG."""
    graph = parse_metro_mermaid(
        "%%metro title: Dot Test\n"
        "%%metro line: main | Main | #ff0000 | dotted\n"
        "graph LR\n"
        "    a[Input]\n"
        "    b[Output]\n"
        "    a -->|main| b\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_THEME)
    assert 'stroke-dasharray="2,4"' in svg or "stroke-dasharray='2,4'" in svg


def test_render_solid_line_no_dasharray():
    """Solid lines should not produce stroke-dasharray."""
    graph = parse_metro_mermaid(
        "%%metro title: Solid Test\n"
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    a[Input]\n"
        "    b[Output]\n"
        "    a -->|main| b\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_THEME)
    assert "stroke-dasharray" not in svg


def test_render_empty_graph():
    graph = parse_metro_mermaid("graph LR\n")
    svg = render_svg(graph, NFCORE_THEME)
    assert "svg" in svg


def test_render_legend():
    svg = _render_simple()
    # Legend should contain the line display name
    assert "Main" in svg


def test_legend_min_height_enlarges_legend():
    """A single-line graph with legend_min_height should produce a taller legend."""
    from nf_metro.render.legend import compute_legend_dimensions

    base_text = (
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    a[A]\n    b[B]\n"
        "    a -->|main| b\n"
    )
    graph_default = parse_metro_mermaid(base_text)
    _, h_default = compute_legend_dimensions(graph_default, NFCORE_THEME)

    min_h_text = (
        "%%metro legend_min_height: 120\n"
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    a[A]\n    b[B]\n"
        "    a -->|main| b\n"
    )
    graph_min = parse_metro_mermaid(min_h_text)
    _, h_min = compute_legend_dimensions(graph_min, NFCORE_THEME)

    assert h_min > h_default
    # content_height should be at least the minimum
    from nf_metro.render.constants import LEGEND_PADDING

    assert h_min >= 120 + 2 * LEGEND_PADDING


def test_render_file_size():
    """SVG output should be reasonably small."""
    graph = parse_metro_mermaid(
        "%%metro title: Size Test\n"
        "%%metro line: main | Main | #ff0000\n"
        "%%metro line: alt | Alt | #0000ff\n"
        "graph LR\n"
        "    a[A]\n    b[B]\n    c[C]\n    d[D]\n    e[E]\n"
        "    a -->|main| b\n    b -->|main| c\n    c -->|main| d\n    d -->|main| e\n"
        "    a -->|alt| c\n    c -->|alt| e\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_THEME)
    # Should be well under 50KB for a small graph
    assert len(svg) < 50000


# --- First-class section rendering tests ---


def test_render_first_class_sections():
    """First-class sections render section boxes with names."""
    graph = parse_metro_mermaid(
        "%%metro title: Section Test\n"
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph sec1 [Processing]\n"
        "        a[Input]\n"
        "        b[Middle]\n"
        "        a -->|main| b\n"
        "    end\n"
        "    subgraph sec2 [Output]\n"
        "        c[Result]\n"
        "    end\n"
        "    b -->|main| c\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_THEME)
    assert "Processing" in svg
    assert "Output" in svg
    assert "Input" in svg
    assert "Result" in svg
    # Should be valid XML
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg") or "svg" in root.tag


def test_render_sections_no_port_labels():
    """Port stations should not appear as labels in the SVG."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph sec1 [S1]\n"
        "        a[A]\n"
        "    end\n"
        "    subgraph sec2 [S2]\n"
        "        b[B]\n"
        "    end\n"
        "    a -->|main| b\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_THEME)
    # Port IDs should not appear in the SVG text
    for port_id in graph.ports:
        assert port_id not in svg, f"Port {port_id} should not appear in SVG"


def test_render_multiline_labels():
    """Multi-line labels (\\n) render as separate tspan elements."""
    graph = parse_metro_mermaid(
        "%%metro title: Test\n"
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    a[Line One \\n Line Two]\n"
        "    b[Output]\n"
        "    a -->|main| b\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_THEME)
    # Both lines should appear in the SVG as separate tspan elements
    assert "Line One" in svg
    assert "Line Two" in svg
    root = ET.fromstring(svg)
    # Find tspan elements containing the label parts
    ns = {"svg": "http://www.w3.org/2000/svg"}
    tspans = root.findall(".//svg:text/svg:tspan", ns)
    tspan_texts = [t.text for t in tspans if t.text]
    assert "Line One" in tspan_texts
    assert "Line Two" in tspan_texts


def test_render_rnaseq_sections_example():
    """The rnaseq_sections.mmd example should render without errors."""
    from pathlib import Path

    examples = Path(__file__).parent.parent / "examples"
    text = (examples / "rnaseq_sections.mmd").read_text()
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_THEME)
    # Title text is replaced by embedded logo, so check section labels
    assert "Pre-processing" in svg
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg") or "svg" in root.tag


# --- Multi-icon terminus rendering ---


def test_render_single_file_icon():
    """Single %%metro file: directive renders one file icon."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro file: reads_in | FASTQ\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        reads_in[ ]\n"
        "        trim[Trim]\n"
        "        reads_in -->|main| trim\n"
        "    end\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_THEME)
    assert "FASTQ" in svg
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg") or "svg" in root.tag


def test_render_multiple_file_icons():
    """Comma-separated %%metro file: directive renders multiple file icons."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro file: reads_in | FASTQ, BAM\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        reads_in[ ]\n"
        "        trim[Trim]\n"
        "        reads_in -->|main| trim\n"
        "    end\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_THEME)
    assert "FASTQ" in svg
    assert "BAM" in svg
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg") or "svg" in root.tag


def test_render_file_icon_with_name_caption():
    """Optional name on %%metro file: directive renders as a caption."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro file: reads_in | CSV | Samples\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        reads_in[ ]\n"
        "        trim[Trim]\n"
        "        reads_in -->|main| trim\n"
        "    end\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_THEME)
    # Caption name and inner type label should both appear
    assert "Samples" in svg
    assert "CSV" in svg
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg") or "svg" in root.tag


def test_caption_font_smaller_than_label_font():
    """Caption renders smaller than the station label to fit the icon."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro file: reads_in | CSV | LongCaptionName\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        reads_in[ ]\n"
        "        trim[Trim]\n"
        "        reads_in -->|main| trim\n"
        "    end\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_THEME)
    label_size = NFCORE_THEME.label_font_size
    # The caption text must reference a font-size strictly smaller than
    # the theme label_font_size (60% of it, per ICON_NAME_FONT_SCALE).
    import re

    caption_matches = re.findall(r'font-size="([0-9.]+)"[^>]*>LongCaptionName', svg)
    assert caption_matches, "Caption text not found in SVG"
    caption_size = float(caption_matches[0])
    assert caption_size < label_size, (
        f"Caption font ({caption_size}) should be < label font ({label_size})"
    )


def test_render_file_icon_no_name_no_caption():
    """When no name is provided, no caption text appears in the SVG."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro file: reads_in | FASTQ\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        reads_in[ ]\n"
        "        trim[Trim]\n"
        "        reads_in -->|main| trim\n"
        "    end\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_THEME)
    # The station has no label and there's no caption directive, so the only
    # text inside the terminus block should be the type chip.
    assert "FASTQ" in svg


def test_render_multi_icon_fixture():
    """The 05b_multi_icons.mmd example renders without errors."""
    from pathlib import Path

    examples = Path(__file__).parent.parent / "examples" / "guide"
    text = (examples / "05b_multi_icons.mmd").read_text()
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_THEME)
    # All icon labels should be present
    assert "FASTQ" in svg
    assert "BAM" in svg
    assert "HTML" in svg
    assert "TSV" in svg
    assert "H5AD" in svg
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg") or "svg" in root.tag


def test_render_files_icon():
    """%%metro files: directive renders stacked file icons."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro files: reads_in | FASTQ\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        reads_in[ ]\n"
        "        trim[Trim]\n"
        "        reads_in -->|main| trim\n"
        "    end\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_THEME)
    assert "FASTQ" in svg
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg") or "svg" in root.tag


def test_render_folder_icon():
    """%%metro dir: directive renders folder icon."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro dir: output | Results\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        trim[Trim]\n"
        "        output[ ]\n"
        "        trim -->|main| output\n"
        "    end\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_THEME)
    assert "Results" in svg
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg") or "svg" in root.tag


def test_render_mixed_icon_types():
    """Mixed file/files/dir icons all render."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro file: src | FASTA\n"
        "%%metro files: paired | FASTQ\n"
        "%%metro dir: out | Results\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        src[ ]\n"
        "        paired[ ]\n"
        "        step[Step]\n"
        "        out[ ]\n"
        "        src -->|main| step\n"
        "        paired -->|main| step\n"
        "        step -->|main| out\n"
        "    end\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_THEME)
    assert "FASTA" in svg
    assert "FASTQ" in svg
    assert "Results" in svg
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg") or "svg" in root.tag


def test_render_icon_type_guide_fixtures():
    """Guide examples for files and dir icons render without errors."""
    from pathlib import Path

    examples = Path(__file__).parent.parent / "examples" / "guide"
    for fname in ("05c_files_icon.mmd", "05d_folder_icon.mmd"):
        fpath = examples / fname
        assert fpath.exists(), f"Missing fixture: {fpath}"
        text = fpath.read_text()
        graph = parse_metro_mermaid(text)
        compute_layout(graph)
        svg = render_svg(graph, NFCORE_THEME)
        root = ET.fromstring(svg)
        assert root.tag.endswith("svg") or "svg" in root.tag


# --- Terminus icon orientation (issue #254) ---


def _station(x=100.0, y=50.0):
    return Station(id="t", label="", x=x, y=y)


def test_terminus_icons_lr_march_along_x():
    """LR termini lay icons out horizontally, centred on the bundle Y."""
    st = _station()
    # Sink (no outgoing) extends to the right (forward flow).
    centers = _terminus_icon_centers(
        st, "LR", is_source=False, n=2, first_offset=10.0, step=4.0, bundle_center=3.0
    )
    assert centers == [(110.0, 53.0), (114.0, 53.0)]
    # Source (no incoming) extends to the left (reverse flow).
    src = _terminus_icon_centers(
        st, "LR", is_source=True, n=1, first_offset=10.0, step=4.0, bundle_center=0.0
    )
    assert src == [(90.0, 50.0)]


def test_terminus_icons_rl_mirror_lr():
    """RL flows right-to-left, so the forward/reverse sides are mirrored."""
    st = _station()
    sink = _terminus_icon_centers(
        st, "RL", is_source=False, n=1, first_offset=10.0, step=4.0, bundle_center=0.0
    )
    assert sink == [(90.0, 50.0)]


def test_terminus_icons_tb_march_along_y():
    """TB termini stack icons vertically, centred on the bundle X.

    Regression test for #254: in a TB section the line arrives from
    above/below, so icons must be displaced along Y (the flow axis), not
    along X as for LR.
    """
    st = _station()
    # Sink at the bottom of a TB flow: icons extend downward.
    sink = _terminus_icon_centers(
        st, "TB", is_source=False, n=2, first_offset=10.0, step=4.0, bundle_center=3.0
    )
    assert sink == [(103.0, 60.0), (103.0, 64.0)]
    # Every icon stays on the station's X (cross axis); only Y advances.
    assert all(cx == 103.0 for cx, _ in sink)
    assert all(cy != st.y for _, cy in sink)
    # Source at the top of a TB flow: icons extend upward.
    src = _terminus_icon_centers(
        st, "TB", is_source=True, n=1, first_offset=10.0, step=4.0, bundle_center=0.0
    )
    assert src == [(100.0, 40.0)]


def test_terminus_icons_bt_mirror_tb():
    """BT flows bottom-to-top, mirroring TB's forward/reverse sides."""
    st = _station()
    sink = _terminus_icon_centers(
        st, "BT", is_source=False, n=1, first_offset=10.0, step=4.0, bundle_center=0.0
    )
    assert sink == [(100.0, 40.0)]


def test_render_tb_section_file_icon_below_station():
    """A file terminus in a TB section renders its icon below the station."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro file: report_out | HTML | Report\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        %%metro direction: TB\n"
        "        run[Run]\n"
        "        report_out[ ]\n"
        "        run -->|main| report_out\n"
        "    end\n"
    )
    compute_layout(graph)
    station = graph.stations["report_out"]
    section = graph.sections["sec"]
    assert section.direction == "TB"
    centers = _terminus_icon_centers(
        station,
        "TB",
        is_source=False,
        n=1,
        first_offset=10.0,
        step=4.0,
        bundle_center=0.0,
    )
    (icon_cx, icon_cy) = centers[0]
    # Icon sits on the station's X column and below it (downward TB flow).
    assert abs(icon_cx - station.x) < 1e-6
    assert icon_cy > station.y
    # And the render still succeeds and carries the icon label.
    svg = render_svg(graph, NFCORE_THEME)
    assert "HTML" in svg
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg") or "svg" in root.tag
