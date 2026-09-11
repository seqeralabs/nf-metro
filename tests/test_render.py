"""Tests for SVG rendering."""

import pathlib
import re
import xml.etree.ElementTree as ET
from dataclasses import replace
from functools import lru_cache

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.geometry import lanes_run_along_x
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Station
from nf_metro.render.constants import FILES_ICON_OFFSET_RATIO
from nf_metro.render.svg import (
    _label_halo_color,
    _terminus_icon_centers,
    _terminus_icon_centers_for,
    _terminus_icon_flow_sign,
    render_svg,
)
from nf_metro.themes import (
    LIGHT_THEME,
    NFCORE_DARK_THEME,
    SEQERA_DARK_THEME,
    SEQERA_LIGHT_THEME,
)


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
    return render_svg(graph, NFCORE_DARK_THEME)


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


def test_render_caption_appears_in_svg():
    graph = parse_metro_mermaid(
        "%%metro caption: Example attribution text\n"
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    a[Input] -->|main| b[Output]\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_DARK_THEME)
    assert "Example attribution text" in svg


def test_render_no_caption_by_default():
    svg = _render_simple()
    assert "created with nf-metro" in svg
    assert svg.count("created with nf-metro") == 1


def test_label_angle_emits_rotate_transform():
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro label_angle: 45\n"
        "graph LR\n"
        "    a[Alpha]\n"
        "    b[Beta]\n"
        "    a -->|main| b\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_DARK_THEME)
    assert "rotate(45" in svg


def test_label_angle_default_no_rotate():
    svg = _render_simple()
    assert "rotate(" not in svg


def test_render_nfcore_theme_background():
    svg = _render_simple()
    assert NFCORE_DARK_THEME.background_color in svg


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


def test_render_seqera_theme():
    graph = parse_metro_mermaid(
        "%%metro title: Seqera Test\n"
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    a -->|main| b\n"
    )
    compute_layout(graph)
    for theme in (SEQERA_LIGHT_THEME, SEQERA_DARK_THEME):
        svg = render_svg(graph, theme, chrome_css=False)
        assert f'fill="{theme.background_color}"' in svg
        assert theme.title_color in svg


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
    svg = render_svg(graph, NFCORE_DARK_THEME)
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
    svg = render_svg(graph, NFCORE_DARK_THEME)
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
    svg = render_svg(graph, NFCORE_DARK_THEME)
    assert "stroke-dasharray" not in svg


def test_render_empty_graph():
    graph = parse_metro_mermaid("graph LR\n")
    svg = render_svg(graph, NFCORE_DARK_THEME)
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
    _, h_default = compute_legend_dimensions(graph_default, NFCORE_DARK_THEME)

    min_h_text = (
        "%%metro legend_min_height: 120\n"
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    a[A]\n    b[B]\n"
        "    a -->|main| b\n"
    )
    graph_min = parse_metro_mermaid(min_h_text)
    _, h_min = compute_legend_dimensions(graph_min, NFCORE_DARK_THEME)

    assert h_min > h_default
    # content_height should be at least the minimum
    from nf_metro.render.constants import LEGEND_PADDING

    assert h_min >= 120 + 2 * LEGEND_PADDING


def test_logo_scale_enlarges_bundled_logo():
    """`logo_scale` grows the logo (and the legend box) within the joint block."""
    from nf_metro.render.legend import compute_legend_dimensions

    base = (
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    a[A]\n    b[B]\n"
        "    a -->|main| b\n"
    )
    logo = (320.0, 120.0)  # original (w, h) aspect carrier

    g1 = parse_metro_mermaid(base)
    w1, h1 = compute_legend_dimensions(g1, NFCORE_DARK_THEME, logo_size=logo)

    g2 = parse_metro_mermaid("%%metro logo_scale: 2.0\n" + base)
    w2, h2 = compute_legend_dimensions(g2, NFCORE_DARK_THEME, logo_size=logo)

    # A larger logo widens the block and, once it exceeds the text block,
    # grows the legend height to contain it.
    assert w2 > w1
    assert h2 > h1


def test_logo_scale_default_no_change():
    """Without `logo_scale`, legend dimensions match the historical default."""
    from nf_metro.render.legend import compute_legend_dimensions

    base = (
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n    a[A]\n    b[B]\n    a -->|main| b\n"
    )
    logo = (320.0, 120.0)
    g = parse_metro_mermaid(base)
    assert g.logo_scale == 1.0
    # Should not raise and should produce a positive-size legend.
    w, h = compute_legend_dimensions(g, NFCORE_DARK_THEME, logo_size=logo)
    assert w > 0 and h > 0


def test_legend_logo_gap_widens_block():
    """`legend_logo_gap` adds horizontal room between the logo and the entries."""
    from nf_metro.render.legend import LOGO_GAP, compute_legend_dimensions

    base = (
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n    a[A]\n    b[B]\n    a -->|main| b\n"
    )
    logo = (320.0, 120.0)

    g1 = parse_metro_mermaid(base)
    w1, _ = compute_legend_dimensions(g1, NFCORE_DARK_THEME, logo_size=logo)

    gap = LOGO_GAP + 30.0
    g2 = parse_metro_mermaid(f"%%metro legend_logo_gap: {gap}\n" + base)
    w2, _ = compute_legend_dimensions(g2, NFCORE_DARK_THEME, logo_size=logo)

    assert w2 == pytest.approx(w1 + 30.0)


def test_legend_logo_gap_default_is_logo_gap():
    """Without the directive (and font_scale 1.0) the gap is the base LOGO_GAP."""
    from nf_metro.render.legend import LOGO_GAP, _logo_gap

    g = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n    a[A]\n    b[B]\n    a -->|main| b\n"
    )
    assert g.legend_logo_gap is None
    assert _logo_gap(g) == pytest.approx(LOGO_GAP)


def _font_sizes(svg):
    """Distinct numeric font-size values appearing in an SVG string."""
    return {float(v) for v in re.findall(r'font-size="([0-9.]+)"', svg)}


def _load_font_scale_fixture(scale=None):
    text = pathlib.Path(__file__).parent.joinpath("fixtures/font_scale.mmd").read_text()
    if scale is not None:
        text = f"%%metro font_scale: {scale}\n" + text
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    return graph


def test_font_scale_multiplies_all_text_sizes():
    """`font_scale: N` renders every text class at N times the default size."""
    scale = 2.0
    g1 = _load_font_scale_fixture()
    svg1 = render_svg(g1, NFCORE_DARK_THEME)
    g2 = _load_font_scale_fixture(scale)
    svg2 = render_svg(g2, NFCORE_DARK_THEME)

    for size in (
        NFCORE_DARK_THEME.label_font_size,
        NFCORE_DARK_THEME.title_font_size,
        NFCORE_DARK_THEME.section_label_font_size,
        NFCORE_DARK_THEME.legend_font_size,
        NFCORE_DARK_THEME.terminus_font_size,
    ):
        assert size in _font_sizes(svg1)
        assert size * scale in _font_sizes(svg2)


def test_font_scale_widens_label_driven_layout():
    """A larger font reserves proportionally more layout room.

    Label-width metrics must scale with the font so bigger text doesn't
    overflow its box: a scaled render's section is wider and each station
    reserves a wider label.
    """
    from nf_metro.layout.labels import label_text_width
    from nf_metro.layout.pass_metrics import font_scale_context

    scale = 2.0
    g1 = _load_font_scale_fixture()
    g2 = _load_font_scale_fixture(scale)

    sec1 = g1.sections["proc"]
    sec2 = g2.sections["proc"]
    assert sec2.bbox_w > sec1.bbox_w

    label = "Load Samples"
    with font_scale_context(1.0):
        base_w = label_text_width(label)
    with font_scale_context(scale):
        scaled_w = label_text_width(label)
    assert scaled_w == base_w * scale


def test_font_scale_terminus_icon_stays_in_section_bbox():
    """A scaled-up terminus icon must stay within its section's bbox.

    ``font_scale`` grows the icon body so its label isn't shrink-to-fit
    clamped back down; the layout-side reservation around the icon has to
    grow by the same amount, or ``_guard_stations_in_sections`` raises.
    """
    text = pathlib.Path(__file__).parent.joinpath("fixtures/font_scale.mmd").read_text()
    graph = parse_metro_mermaid("%%metro font_scale: 2\n" + text)
    compute_layout(graph, validate=True)


_STACKED_INPUTS_MMD = """%%metro font_scale: 2
%%metro line: main | Main | #3b82f6
%%metro files: a_in | FASTQ
%%metro files: b_in | BAM
graph LR
    subgraph sec [Section]
        a_in[ ]
        b_in[ ]
        step[Step]
        a_in -->|main| step
        b_in -->|main| step
    end
"""

_RAILS_SOURCE_ICON_MMD = """%%metro font_scale: 2
%%metro line_spread: rails
%%metro line: one | One | #3b82f6
%%metro line: two | Two | #e63946
%%metro files: src | CRAM
graph LR
    subgraph sec [Section]
        src[ ]
        a[Analyse]
        b[Report]
        src -->|one,two| a
        a -->|one,two| b
    end
"""


def test_font_scale_stacked_terminus_icons_do_not_overlap():
    """Two terminus icons stacked in one column must clear each other.

    The grid pitch has to cover the upper icon's downward reach plus the
    lower one's upward reach.  Both scale with ``font_scale`` -- including a
    stacked ``files:`` back page peeking up past its front page -- so a pitch
    derived from the unscaled icon lets an enlarged pair collide.
    """
    from nf_metro.layout.pass_metrics import font_scale_context
    from nf_metro.layout.phases.single_section import terminus_cross_extents

    graph = parse_metro_mermaid(_STACKED_INPUTS_MMD)
    compute_layout(graph, validate=True)
    upper, lower = sorted(
        (graph.stations["a_in"], graph.stations["b_in"]), key=lambda s: s.y
    )
    with font_scale_context(graph.font_scale):
        _, upper_below = terminus_cross_extents(upper)
        lower_above, _ = terminus_cross_extents(lower)
    gap = lower.y - upper.y
    assert gap >= upper_below + lower_above, (
        f"icons overlap by {upper_below + lower_above - gap:.1f}px: "
        f"{upper.id} reaches {upper_below:.1f} below y={upper.y:.1f}, "
        f"{lower.id} reaches {lower_above:.1f} above y={lower.y:.1f}"
    )


def test_font_scale_rails_source_icon_fits_section_bbox():
    """A rails-mode source terminus's icons must fit inside the section box.

    Rail mode derives station X from the section's padding, so a source whose
    icons march *leftward* out of the first column needs that padding topped up
    to the icons' drawn reach -- which grows with ``font_scale``.
    """
    from nf_metro.layout.pass_metrics import font_scale_context, stroke_scale_context
    from nf_metro.layout.phases.single_section import terminus_icon_flow_reach

    graph = parse_metro_mermaid(_RAILS_SOURCE_ICON_MMD)
    compute_layout(graph, validate=True)
    st = graph.stations["src"]
    section = graph.sections[st.section_id]
    with font_scale_context(graph.font_scale), stroke_scale_context(graph.stroke_scale):
        reach = terminus_icon_flow_reach(st)
    available = st.x - section.bbox_x
    assert available >= reach, (
        f"icon run reaches {reach:.1f}px left of {st.id} but the section box "
        f"only leaves {available:.1f}px"
    )


def test_title_baseline_clears_its_own_ascent():
    """An enlarged title drops its baseline instead of ascending off-canvas."""
    from nf_metro.render.constants import (
        TITLE_ASCENT_RATIO,
        TITLE_Y_OFFSET,
        title_baseline_y,
    )

    assert title_baseline_y(20.0) == TITLE_Y_OFFSET
    big = TITLE_Y_OFFSET / TITLE_ASCENT_RATIO * 2
    assert title_baseline_y(big) == pytest.approx(big * TITLE_ASCENT_RATIO)


def test_font_scale_default_is_noop():
    """Without `font_scale`, the graph and render match the unscaled default."""
    g = _load_font_scale_fixture()
    assert g.font_scale == 1.0
    svg_default = render_svg(g, NFCORE_DARK_THEME)
    g_explicit = _load_font_scale_fixture(1.0)
    svg_explicit = render_svg(g_explicit, NFCORE_DARK_THEME)
    assert svg_default == svg_explicit


def test_render_file_size():
    """SVG output should be reasonably small."""
    graph = parse_metro_mermaid(
        "%%metro title: Size Test\n"
        "%%metro line: main | Main | #ff0000\n"
        "%%metro line: alt | Alt | #0000ff\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        a[A]\n    b[B]\n    c[C]\n    d[D]\n    e[E]\n"
        "        a -->|main| b\n    b -->|main| c\n    c -->|main| d\n"
        "        d -->|main| e\n"
        "        a -->|alt| c\n    c -->|alt| e\n"
        "    end\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_DARK_THEME)
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
    svg = render_svg(graph, NFCORE_DARK_THEME)
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
    svg = render_svg(graph, NFCORE_DARK_THEME)
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
    svg = render_svg(graph, NFCORE_DARK_THEME)
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
    graph.source_dir = str(examples)
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_DARK_THEME)
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
    svg = render_svg(graph, NFCORE_DARK_THEME)
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
    svg = render_svg(graph, NFCORE_DARK_THEME)
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
    svg = render_svg(graph, NFCORE_DARK_THEME)
    # Caption name and inner type label should both appear
    assert "Samples" in svg
    assert "CSV" in svg
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg") or "svg" in root.tag


def test_render_icon_caption_with_linebreak():
    from nf_metro.render.plan import freeze_render_value

    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro file: source | DATA\\nFILE | input\\nfile\n"
        "graph LR\n"
        "    source[ ]\n"
        "    source -->|main| node[Node]\n"
    )
    compute_layout(graph)

    svg = render_svg(graph, NFCORE_DARK_THEME)
    root = ET.fromstring(svg)
    tspan_values = [
        element.text for element in root.iter() if element.tag.endswith("tspan")
    ]

    assert tspan_values == ["DATA", "FILE", "input", "file"]
    assert (
        freeze_render_value(graph.stations["source"]).terminus_caption_line_count == 2
    )
    assert r"DATA\nFILE" not in svg
    assert r"input\nfile" not in svg


@pytest.mark.parametrize("direction", ["LR", "RL", "TB", "BT"])
def test_multiline_icon_caption_fits_render_obstacle(direction):
    from nf_metro.layout.routing.offsets import compute_station_offsets
    from nf_metro.render.constants import (
        ICON_CLEARANCE_MARGIN,
        ICON_NAME_FONT_SCALE,
        ICON_NAME_GAP,
    )
    from nf_metro.render.svg import (
        _icon_obstacles_by_station,
        _terminus_icon_centers_for,
    )

    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro file: source | DATA | input\\nfile\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        f"        %%metro direction: {direction}\n"
        "        source[ ]\n"
        "        source -->|main| node[Node]\n"
        "    end\n"
    )
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    station = graph.stations["source"]
    line_offsets = [
        offsets.get((station.id, line_id), 0.0)
        for line_id in graph.station_lines(station.id)
    ]
    centers = _terminus_icon_centers_for(
        station,
        graph,
        NFCORE_DARK_THEME,
        min(line_offsets, default=0.0),
        max(line_offsets, default=0.0),
    )
    obstacle = _icon_obstacles_by_station(graph, NFCORE_DARK_THEME, offsets)[station.id]
    caption_height = 2 * NFCORE_DARK_THEME.label_font_size * ICON_NAME_FONT_SCALE
    caption_reach = (
        NFCORE_DARK_THEME.terminus_height / 2
        + ICON_NAME_GAP
        + caption_height
        + ICON_CLEARANCE_MARGIN
    )
    is_vertical_flow = lanes_run_along_x(direction)
    flow_sign = _terminus_icon_flow_sign(direction, is_source=True)
    if is_vertical_flow and flow_sign < 0:
        # TB/BT source draws its icon above the station, so the caption and
        # its clearance grow the obstacle's top edge, not the bottom.
        assert obstacle[1] <= min(cy for _, cy in centers) - caption_reach
    else:
        assert obstacle[3] >= max(cy for _, cy in centers) + caption_reach


@pytest.mark.parametrize("direction", ["TB", "BT"])
def test_vertical_icon_stack_reserves_multiline_caption(direction):
    from nf_metro.render.constants import (
        ICON_INTER_GAP,
        ICON_NAME_FONT_SCALE,
        ICON_NAME_GAP,
    )
    from nf_metro.render.svg import _terminus_icon_centers_for

    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro file: source | A, B | one\\ntwo\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        f"        %%metro direction: {direction}\n"
        "        source[ ]\n"
        "        source -->|main| node[Node]\n"
        "    end\n"
    )
    compute_layout(graph)
    centers = _terminus_icon_centers_for(
        graph.stations["source"], graph, NFCORE_DARK_THEME, 0.0, 0.0
    )
    caption_height = 2 * NFCORE_DARK_THEME.label_font_size * ICON_NAME_FONT_SCALE
    expected_step = (
        NFCORE_DARK_THEME.terminus_height
        + ICON_INTER_GAP
        + ICON_NAME_GAP
        + caption_height
    )

    assert abs(centers[1][1] - centers[0][1]) == pytest.approx(expected_step)


def test_render_file_icon_with_outgoing_edge():
    fixture = (
        pathlib.Path(__file__).parent.parent
        / "examples/topologies/file_node_with_outgoing_edge.mmd"
    )
    graph = parse_metro_mermaid(fixture.read_text())
    compute_layout(graph)
    root = ET.fromstring(render_svg(graph, NFCORE_DARK_THEME))
    ns = {"svg": "http://www.w3.org/2000/svg"}
    visible_text = [
        "".join(element.itertext()) for element in root.findall(".//svg:text", ns)
    ]

    assert "GTF" in visible_text
    assert "Hybrid GTF" in visible_text
    assert "made_gtf" not in visible_text
    icon_group = next(
        element
        for element in root.findall(".//svg:g", ns)
        if element.attrib.get("data-station-id") == "made_gtf"
    )
    assert icon_group.find("svg:path", ns) is not None


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
    svg = render_svg(graph, NFCORE_DARK_THEME)
    label_size = NFCORE_DARK_THEME.label_font_size
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
    svg = render_svg(graph, NFCORE_DARK_THEME)
    # The station has no label and there's no caption directive, so the only
    # text inside the terminus block should be the type chip.
    assert "FASTQ" in svg


def test_wide_file_icon_label_wraps_within_icon_width():
    """A file-icon label wider than the glyph wraps onto stacked lines, each
    of which fits the icon width rather than overflowing it."""
    from pathlib import Path

    from nf_metro.render.constants import ICON_LABEL_CLEARANCE
    from nf_metro.text_metrics import DEFAULT_TEXT_METRICS, TextRole, text_style

    fixture = Path(__file__).parent / "fixtures" / "icon_caption_wrap.mmd"
    graph = parse_metro_mermaid(fixture.read_text())
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_DARK_THEME)

    pieces = re.findall(r'font-size="([0-9.]+)"[^>]*>([^<]*BAM[^<]*|CRAM)</text>', svg)
    assert pieces, "wrapped BAM/CRAM label pieces not found in SVG"
    assert all("BAM/CRAM" not in text for _, text in pieces), (
        "label must wrap rather than render as a single over-wide line"
    )

    max_width = NFCORE_DARK_THEME.terminus_width - 2 * ICON_LABEL_CLEARANCE
    tolerance = 1.0
    for font_size, text in pieces:
        line_width = DEFAULT_TEXT_METRICS.reserve_width(
            text, text_style(float(font_size), "bold"), TextRole.ICON_LABEL
        )
        assert line_width <= max_width + tolerance, (
            f"wrapped line {text!r} width {line_width:.1f} exceeds "
            f"icon usable width {max_width:.1f}"
        )


def test_icon_label_wrap_keeps_separators():
    """Wrapping keeps a ``/`` joined to its left token and restores the space
    between whitespace-separated words; labels that fit or have no break point
    stay on one line."""
    from nf_metro.render.icons import _wrap_icon_label

    assert _wrap_icon_label("BAM/CRAM", 12.0, 40.0) == ["BAM/", "CRAM"]
    assert _wrap_icon_label("FASTQ to BAM", 12.0, 70.0) == ["FASTQ to", "BAM"]
    assert _wrap_icon_label("BAM/CRAM", 12.0, 999.0) == ["BAM/CRAM"]
    assert _wrap_icon_label("Results", 12.0, 40.0) == ["Results"]


def test_render_multi_icon_fixture():
    """The 05b_multi_icons.mmd example renders without errors."""
    from pathlib import Path

    examples = Path(__file__).parent.parent / "examples" / "guide"
    text = (examples / "05b_multi_icons.mmd").read_text()
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_DARK_THEME)
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
    svg = render_svg(graph, NFCORE_DARK_THEME)
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
    svg = render_svg(graph, NFCORE_DARK_THEME)
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
    svg = render_svg(graph, NFCORE_DARK_THEME)
    assert "FASTA" in svg
    assert "FASTQ" in svg
    assert "Results" in svg
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg") or "svg" in root.tag


def test_file_icon_banner_option():
    """The `| banner` option flips the per-icon banner flag and styling."""
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "%%metro file: aln_out | BAM | Alignments | banner\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        run[Run]\n"
        "        aln_out[ ]\n"
        "        run -->|main| aln_out\n"
        "    end\n"
    )
    station = graph.stations["aln_out"]
    assert station.terminus_icon_banners == [True]
    # The caption (third field) is still parsed alongside the banner option.
    assert station.terminus_names == ["Alignments"]
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_DARK_THEME)
    from nf_metro.render.constants import ICON_BANNER_FILL

    assert ICON_BANNER_FILL in svg


def test_file_icon_no_banner_by_default():
    """A plain %%metro file: directive does not enable the banner."""
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
    station = graph.stations["reads_in"]
    assert station.terminus_icon_banners == [False]


def test_render_icon_type_guide_fixtures():
    """Guide examples for files and dir icons render without errors."""
    from pathlib import Path

    examples = Path(__file__).parent.parent / "examples" / "guide"
    for fname in (
        "05c_files_icon.mmd",
        "05d_folder_icon.mmd",
        "05f_banner_labels.mmd",
    ):
        fpath = examples / fname
        assert fpath.exists(), f"Missing fixture: {fpath}"
        text = fpath.read_text()
        graph = parse_metro_mermaid(text)
        compute_layout(graph)
        svg = render_svg(graph, NFCORE_DARK_THEME)
        root = ET.fromstring(svg)
        assert root.tag.endswith("svg") or "svg" in root.tag


# --- Terminus icon orientation ---


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

    In a TB section the line arrives from above/below, so icons must be
    displaced along Y (the flow axis), not along X as for LR.
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


def _files_terminus_mmd(direction: str, role: str) -> str:
    """A minimal graph with one `files:` terminus, station `t`, as source or sink."""
    other_edge = "t -->|main| other" if role == "source" else "other -->|main| t"
    return (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro files: t | FASTQ | Reads\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        f"        %%metro direction: {direction}\n"
        "        t[ ]\n"
        "        other[Other]\n"
        f"        {other_edge}\n"
        "    end\n"
    )


@pytest.mark.parametrize("direction", ["LR", "RL", "TB", "BT"])
@pytest.mark.parametrize("role", ["source", "sink"])
def test_stacked_files_icon_back_page_does_not_crowd_marker(direction, role):
    """A `files:` terminus's back page must sit at least as far from the
    station marker as the front page, on every flow direction and role (#1460).
    """
    graph = parse_metro_mermaid(_files_terminus_mmd(direction, role))
    compute_layout(graph)
    station = graph.stations["t"]
    section = graph.sections["sec"]
    assert section.direction == direction
    is_source = role == "source"

    front_cx, front_cy = _terminus_icon_centers_for(
        station, graph, NFCORE_DARK_THEME, 0.0, 0.0
    )[0]
    off = NFCORE_DARK_THEME.terminus_width * FILES_ICON_OFFSET_RATIO
    flow_sign = _terminus_icon_flow_sign(direction, is_source)
    is_vertical_flow = lanes_run_along_x(direction)

    if is_vertical_flow:
        marker_flow, front_flow = station.y, front_cy
        back_flow = front_cy + off * flow_sign
    else:
        marker_flow, front_flow = station.x, front_cx
        back_flow = front_cx + off * flow_sign

    front_gap = abs(marker_flow - front_flow)
    back_gap = abs(marker_flow - back_flow)
    assert back_gap >= front_gap, (
        f"{direction}/{role}: back page gap {back_gap:.1f} < front page gap "
        f"{front_gap:.1f} -- back page crowds the marker"
    )


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
    svg = render_svg(graph, NFCORE_DARK_THEME)
    assert "HTML" in svg
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg") or "svg" in root.tag


def test_render_tb_terminus_pill_is_horizontal():
    """A blank terminus nub in a TB section is a horizontal (wide) pill."""
    import re

    graph = parse_metro_mermaid(
        "%%metro line: a | A | #ff0000\n"
        "%%metro line: b | B | #00ff00\n"
        "%%metro file: out | HTML\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        %%metro direction: TB\n"
        "        run[Run]\n"
        "        out[ ]\n"
        "        run -->|a,b| out\n"
        "    end\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_DARK_THEME)
    # The terminus nub <rect> carries data-station-id="out"; in a TB section
    # it must be wider than tall (lines arrive vertically into it).
    m = re.search(r'<rect\b[^>]*data-station-id="out"[^>]*/?>', svg)
    assert m, "terminus nub rect not found"
    rect = m.group(0)
    width = float(re.search(r'width="([0-9.]+)"', rect).group(1))
    height = float(re.search(r'height="([0-9.]+)"', rect).group(1))
    assert width > height, f"TB terminus pill not horizontal: {width=} {height=}"


def test_render_group_label_caption_and_underline():
    """A %%metro group: directive emits a caption and underline; layout
    coordinates are untouched relative to the same graph without groups."""
    base_src = (
        "%%metro line: main | Main | #2db572\n"
        "graph LR\n"
        "    subgraph s [Callers]\n"
        "        a[Alpha]\n"
        "        b[Beta]\n"
        "        a -->|main| b\n"
        "    end\n"
    )
    grouped_src = "%%metro group: Family | a, b\n" + base_src

    base_graph = parse_metro_mermaid(base_src)
    compute_layout(base_graph)
    base_svg = render_svg(base_graph, NFCORE_DARK_THEME)

    grouped_graph = parse_metro_mermaid(grouped_src)
    compute_layout(grouped_graph)
    # Station coordinates must be identical: groups are purely annotative.
    assert {sid: (st.x, st.y) for sid, st in grouped_graph.stations.items()} == {
        sid: (st.x, st.y) for sid, st in base_graph.stations.items()
    }

    grouped_svg = render_svg(grouped_graph, NFCORE_DARK_THEME)
    assert "Family" in grouped_svg
    assert "nf-metro-group-label" in grouped_svg
    assert "nf-metro-group-underline" in grouped_svg
    # The base render has no group-label elements (the class may appear in the
    # <style> block as a CSS selector, but no element should carry it).
    assert not re.search(r'class="[^"]*nf-metro-group-label', base_svg)


def _section_box_bottoms(svg: str) -> dict[str, float]:
    """Map each rendered section box id to its bbox bottom edge (y + h)."""
    bottoms: dict[str, float] = {}
    for m in re.finditer(
        r'<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" '
        r'height="([\d.]+)"[^>]*nf-metro-section-box[^>]*'
        r'data-section-id="(\w+)"',
        svg,
    ):
        _x, y, _w, h, sid = m.groups()
        bottoms[sid] = float(y) + float(h)
    return bottoms


def test_render_group_band_stays_inside_section_box():
    """A below group band (bracket + caption) must sit clear of the section
    box's bottom edge: the engine grows the bbox to reserve room so the band
    never overlaps or crosses the boundary."""
    src = (
        "%%metro line: main | Main | #2db572\n"
        "%%metro group: Callers | a, b, c\n"
        "graph LR\n"
        "    subgraph s [Variant Calling]\n"
        "        a[Alpha]\n"
        "        b[Beta]\n"
        "        c[Gamma]\n"
        "        a -->|main| b\n"
        "        b -->|main| c\n"
        "    end\n"
    )
    graph = parse_metro_mermaid(src)
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_DARK_THEME)

    section_bottom = _section_box_bottoms(svg)["s"]

    # Caption text: hanging baseline, so its bottom edge is text_y + font_size.
    cap = re.search(
        r'<text x="[\d.]+" y="([\d.]+)" font-size="([\d.]+)"'
        r"[^>]*nf-metro-group-label",
        svg,
    )
    assert cap is not None
    caption_bottom = float(cap.group(1)) + float(cap.group(2))

    # Bracket rule + ticks: gather every y coordinate in the path data.
    bracket = re.search(r'<path d="([^"]*)"[^>]*nf-metro-group-underline', svg)
    assert bracket is not None
    ys = [
        float(v.split(",")[1]) for v in re.findall(r"[\d.]+,[\d.]+", bracket.group(1))
    ]
    bracket_bottom = max(ys)

    band_bottom = max(caption_bottom, bracket_bottom)
    assert band_bottom <= section_bottom, (
        f"group band bottom {band_bottom:.1f} crosses section box bottom "
        f"{section_bottom:.1f}"
    )


def test_standalone_nodes_render_as_unlinked_labels():
    """Edge-less nodes in a section render as a compact column, not routed lines.

    A node defined inside a subgraph but referenced by no edge lists a tool
    without joining the line graph: it gets a marker and label, stacks in a
    single column inside the section box, and creates no edge to route.
    """
    standalone = ["samtools", "bcftools", "bwa"]
    src = (
        "%%metro title: Tools\n"
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph sec1 [Alignment]\n"
        "        a[Input]\n"
        "        b[Aligned]\n"
        "        a -->|main| b\n"
        + "".join(f"        {t}[{t}]\n" for t in standalone)
        + "    end\n"
    )
    graph = parse_metro_mermaid(src)

    linked = {e.source for e in graph.edges} | {e.target for e in graph.edges}
    assert not (set(standalone) & linked), "standalone nodes must spawn no edges"
    assert all(graph.stations[t].section_id == "sec1" for t in standalone)

    compute_layout(graph)

    sec = graph.sections["sec1"]
    xs = {round(graph.stations[t].x, 1) for t in standalone}
    ys = [graph.stations[t].y for t in standalone]
    assert len(xs) == 1, f"unlinked tools should share one column, got {xs}"
    assert len(set(ys)) == len(ys), "unlinked tools should occupy distinct rows"
    for t in standalone:
        st = graph.stations[t]
        assert sec.bbox_x <= st.x <= sec.bbox_x + sec.bbox_w
        assert sec.bbox_y <= st.y <= sec.bbox_y + sec.bbox_h

    svg = render_svg(graph, NFCORE_DARK_THEME)
    for t in standalone:
        assert t in svg


def test_label_halo_color_resolves_to_opaque_background():
    color = _label_halo_color(replace(NFCORE_DARK_THEME, label_halo_color=""))
    assert color == NFCORE_DARK_THEME.background_color


def test_label_halo_color_resolves_to_white_on_transparent_theme():
    color = _label_halo_color(replace(LIGHT_THEME, label_halo_color=""))
    assert color == "#ffffff"


def test_label_halo_color_honours_explicit_colour():
    color = _label_halo_color(replace(NFCORE_DARK_THEME, label_halo_color="#123456"))
    assert color == "#123456"


def test_label_halo_disabled_by_zero_width():
    assert _label_halo_color(replace(NFCORE_DARK_THEME, label_halo_width=0.0)) is None


def test_label_halo_disabled_by_none_colour():
    assert (
        _label_halo_color(replace(NFCORE_DARK_THEME, label_halo_color="none")) is None
    )


def test_label_halo_emits_aria_hidden_backing_copy():
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    a[Input]\n"
        "    b[Output]\n"
        "    a -->|main| b\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_DARK_THEME)

    root = ET.fromstring(svg)
    ns = "{http://www.w3.org/2000/svg}"
    texts = [t for t in root.iter(f"{ns}text") if (t.text or "") == "Input"]
    halo = [t for t in texts if t.get("aria-hidden") == "true"]
    fill = [t for t in texts if t.get("aria-hidden") != "true"]
    assert len(halo) == 1, "expected one aria-hidden halo copy of the label"
    assert len(fill) == 1, "expected one painted label that carries station metadata"
    assert halo[0].get("data-station-id") is None
    assert fill[0].get("data-station-id") == "a"

    # The halo is a stroked knockout: it must paint the resolved halo colour on
    # both fill and stroke at the theme width, and sit under the visible glyph.
    resolved = _label_halo_color(NFCORE_DARK_THEME)
    assert halo[0].get("stroke") == halo[0].get("fill") == resolved
    assert float(halo[0].get("stroke-width")) == NFCORE_DARK_THEME.label_halo_width
    assert texts.index(halo[0]) < texts.index(fill[0]), (
        "halo must precede the visible label in document order so it draws under it"
    )


def test_label_halo_suppressed_when_disabled():
    graph = parse_metro_mermaid(
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    a[Input]\n"
        "    b[Output]\n"
        "    a -->|main| b\n"
    )
    compute_layout(graph)
    svg = render_svg(graph, replace(NFCORE_DARK_THEME, label_halo_width=0.0))

    root = ET.fromstring(svg)
    ns = "{http://www.w3.org/2000/svg}"
    texts = [t for t in root.iter(f"{ns}text") if (t.text or "") == "Input"]
    assert len(texts) == 1, "halo copy should be absent when haloing is disabled"


# ---------------------------------------------------------------------------
# Responsive render mode
# ---------------------------------------------------------------------------


def _graph_for_responsive():
    graph = parse_metro_mermaid(
        "%%metro title: Responsive Test\n"
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    a[Start]\n"
        "    b[End]\n"
        "    a -->|main| b\n"
    )
    compute_layout(graph)
    return graph


def test_responsive_render_omits_fixed_dimensions():
    svg = render_svg(_graph_for_responsive(), NFCORE_DARK_THEME, responsive=True)
    root = ET.fromstring(svg)
    assert root.get("width") is None, "responsive SVG must not carry a fixed width"
    assert root.get("height") is None, "responsive SVG must not carry a fixed height"


def test_responsive_render_has_viewbox_and_aspect_ratio():
    svg = render_svg(_graph_for_responsive(), NFCORE_DARK_THEME, responsive=True)
    root = ET.fromstring(svg)
    assert root.get("viewBox") is not None, "responsive SVG must have a viewBox"
    assert root.get("preserveAspectRatio") == "xMinYMin meet", (
        "responsive SVG must declare preserveAspectRatio"
    )


def test_default_render_retains_fixed_dimensions():
    svg = render_svg(_graph_for_responsive(), NFCORE_DARK_THEME)
    root = ET.fromstring(svg)
    assert root.get("width") is not None, "default SVG must carry a fixed width"
    assert root.get("height") is not None, "default SVG must carry a fixed height"


# ---------------------------------------------------------------------------
# Terminus caption placement vs its own station marker (issue #1972)
# ---------------------------------------------------------------------------

# Each case is a single captioned file terminus in one section, spanning the
# four flow directions plus the horizontal-flow control. The negative-flow
# cases (TB source, BT sink) draw the icon above the station, where a caption
# hung below lands on the marker; the others draw it below.
_CAPTION_MARKER_CASES = {
    "tb_source": (
        "Short reads",
        "input_reads",
        "%%metro files: input_reads | fastq | Short reads | banner\n"
        "graph LR\n"
        "    subgraph input[Input]\n"
        "        %%metro direction: TB\n"
        "        input_reads([Short reads])\n"
        "    end\n",
    ),
    "bt_sink": (
        "Result",
        "out",
        "%%metro line: x | X | #1f9e89\n"
        "%%metro file: out | fastq | Result\n"
        "graph LR\n"
        "    subgraph s[Sec]\n"
        "        %%metro direction: BT\n"
        "        proc[Process]\n"
        "        out[ ]\n"
        "        proc -->|x| out\n"
        "    end\n",
    ),
    "tb_sink": (
        "Result",
        "out",
        "%%metro line: x | X | #1f9e89\n"
        "%%metro file: out | fastq | Result\n"
        "graph LR\n"
        "    subgraph s[Sec]\n"
        "        %%metro direction: TB\n"
        "        proc[Process]\n"
        "        out[ ]\n"
        "        proc -->|x| out\n"
        "    end\n",
    ),
    "bt_source": (
        "Input",
        "inp",
        "%%metro line: x | X | #1f9e89\n"
        "%%metro file: inp | fastq | Input\n"
        "graph LR\n"
        "    subgraph s[Sec]\n"
        "        %%metro direction: BT\n"
        "        inp[ ]\n"
        "        proc[Process]\n"
        "        inp -->|x| proc\n"
        "    end\n",
    ),
    "lr_sink": (
        "Result",
        "out",
        "%%metro line: x | X | #1f9e89\n"
        "%%metro file: out | fastq | Result\n"
        "graph LR\n"
        "    subgraph s[Sec]\n"
        "        proc[Process]\n"
        "        out[ ]\n"
        "        proc -->|x| out\n"
        "    end\n",
    ),
}


def _caption_and_marker_boxes(svg, caption, station_id):
    """Return (caption_y_box, marker_y_box) as (top, bottom) pairs from *svg*.

    The caption is the hanging-baseline text carrying *caption*; its box runs
    from the baseline top down one font-size (the em box). The marker is the
    ``nf-metro-station`` rect for *station_id*.
    """
    ns = {"svg": "http://www.w3.org/2000/svg"}
    root = ET.fromstring(svg)
    caption_box = None
    for text in root.findall(".//svg:text", ns):
        if "".join(text.itertext()).strip() != caption:
            continue
        top = float(text.get("y"))
        caption_box = (top, top + float(text.get("font-size")))
    marker_box = None
    for rect in root.findall(".//svg:rect", ns):
        if rect.get("data-station-id") != station_id:
            continue
        top = float(rect.get("y"))
        marker_box = (top, top + float(rect.get("height")))
    return caption_box, marker_box


@lru_cache(maxsize=None)
def _rendered_caption_case(case):
    """Parse, lay out, and render one ``_CAPTION_MARKER_CASES`` entry, once.

    Shared by the two tests below so each case pays for one parse+layout+
    render pass rather than two.
    """
    _, _, source = _CAPTION_MARKER_CASES[case]
    graph = parse_metro_mermaid(source)
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_DARK_THEME)
    return graph, svg


@pytest.mark.parametrize("case", sorted(_CAPTION_MARKER_CASES))
def test_terminus_caption_clears_its_station_marker(case):
    """A terminus caption never overlaps its own station marker (#1972)."""
    caption, station_id, _ = _CAPTION_MARKER_CASES[case]
    _, svg = _rendered_caption_case(case)

    caption_box, marker_box = _caption_and_marker_boxes(svg, caption, station_id)
    assert caption_box is not None, f"{case}: caption {caption!r} not rendered"
    assert marker_box is not None, f"{case}: marker for {station_id!r} not rendered"

    overlaps = caption_box[1] > marker_box[0] and caption_box[0] < marker_box[1]
    assert not overlaps, (
        f"{case}: caption y={caption_box} struck by marker y={marker_box}"
    )


@pytest.mark.parametrize("case", sorted(_CAPTION_MARKER_CASES))
def test_terminus_caption_sits_outboard_of_its_icon(case):
    """The caption seats on the icon's far side from the station (#1972).

    Placing it between icon and station is what let the marker strike it, so
    the whole caption box must lie beyond the icon's outward flow edge.
    """
    caption, station_id, _ = _CAPTION_MARKER_CASES[case]
    graph, svg = _rendered_caption_case(case)
    theme = NFCORE_DARK_THEME

    station = graph.stations[station_id]
    icon_cx, icon_cy = _terminus_icon_centers_for(station, graph, theme, 0.0, 0.0)[0]
    icon_half_h = theme.terminus_height / 2
    caption_box, _ = _caption_and_marker_boxes(svg, caption, station_id)
    assert caption_box is not None, f"{case}: caption {caption!r} not rendered"

    # Outward is the direction the icon is offset from the station; when the
    # icon shares the station's Y (horizontal flow) the caption hangs below.
    outward = -1.0 if icon_cy < station.y else 1.0
    if outward < 0:
        assert caption_box[1] <= icon_cy - icon_half_h, (
            f"{case}: caption y={caption_box} not clear above icon_cy={icon_cy}"
        )
    else:
        assert caption_box[0] >= icon_cy + icon_half_h, (
            f"{case}: caption y={caption_box} not clear below icon_cy={icon_cy}"
        )
