"""Dots, station-number markers, legend bullets and section codes, as drawn."""

from __future__ import annotations

import textwrap
import xml.etree.ElementTree as ET

import pytest
from theme_helpers import RNASEQ, SVG_NS, classed, write

from nf_metro.api import render_string
from nf_metro.layout.constants import TB_LABEL_H_SPACING
from nf_metro.layout.labels import beside_label_gap
from nf_metro.layout.pass_metrics import metrics_context
from nf_metro.parser import parse_metro_mermaid
from nf_metro.render.constants import readable_text_color
from nf_metro.render.style import ordinal_code
from nf_metro.themes import resolve_stack

_MAP = RNASEQ.read_text()

TWO_LINES = textwrap.dedent("""\
    %%metro line: main | Main | #ff0000
    %%metro line: alt | Alt | #0000ff
    graph LR
        subgraph s1 [First]
            a[A] -->|main,alt| b[B]
            b -->|main| c[C]
            b -->|alt| d[D]
        end
""")


def _svg(text: str, theme: str, mode: str = "light") -> ET.Element:
    return ET.fromstring(
        render_string(text, theme=theme, mode=mode, source_dir=str(RNASEQ.parent))
    )


def _station(root: ET.Element, station_id: str) -> ET.Element:
    for el in root.iter(f"{SVG_NS}g"):
        if el.get("data-station-id") == station_id and "nf-metro-station" in (
            el.get("class") or ""
        ):
            return el
    raise AssertionError(f"no glyph group for {station_id!r}")


@pytest.fixture(scope="module")
def nyc() -> ET.Element:
    return _svg(TWO_LINES, "nyc-subway")


def test_a_dots_interchange_draws_a_dot_on_each_line(nyc):
    theme = resolve_stack("nyc-subway").light
    dots = list(_station(nyc, "b").iter(f"{SVG_NS}circle"))
    assert len(dots) == 2
    ys = sorted(float(dot.get("cy")) for dot in dots)
    pitch = theme.line_width + resolve_stack("nyc-subway").layout["track_gap"]
    assert ys[1] - ys[0] == pytest.approx(pitch)


def test_an_outline_half_the_diameter_wide_draws_a_solid_dot(nyc):
    theme = resolve_stack("nyc-subway").light
    outer = theme.dot_diameter * theme.line_width / 2
    dot = next(_station(nyc, "c").iter(f"{SVG_NS}circle"))
    assert float(dot.get("r")) == pytest.approx(outer / 2)
    assert float(dot.get("stroke-width")) == pytest.approx(outer)
    assert dot.get("stroke") == theme.station_stroke


def test_washington_dots_are_white_ringed_in_black_as_wide_as_the_line():
    theme = resolve_stack("washington-metro").light
    dot = next(
        _station(_svg(TWO_LINES, "washington-metro"), "c").iter(f"{SVG_NS}circle")
    )
    lw = theme.line_width
    assert float(dot.get("r")) + float(dot.get("stroke-width")) / 2 == pytest.approx(
        lw / 2
    )
    assert float(dot.get("stroke-width")) == pytest.approx(0.26 * lw)
    assert (dot.get("fill"), dot.get("stroke")) == ("#ffffff", "#000000")


def test_a_line_ringed_dot_takes_its_line_colour():
    theme = resolve_stack("paris-metro").light
    root = _svg(TWO_LINES, "paris-metro")
    alt = parse_metro_mermaid(TWO_LINES).lines["alt"]
    dot = next(_station(root, "d").iter(f"{SVG_NS}circle"))
    assert dot.get("stroke") == theme.line_colors[alt.palette_index]


def test_a_ring_line_end_draws_the_dots_interchange(tmp_path):
    text = TWO_LINES.replace(
        "b -->|alt| d[D]", "b -->|alt| d[D]\n        z[ ] -->|main,alt| a"
    )
    root = _svg(text, "nyc-subway")
    assert len(list(_station(root, "z").iter(f"{SVG_NS}circle"))) == 2


@pytest.fixture(scope="module")
def tokyo() -> ET.Element:
    return _svg(_MAP, "tokyo-metro")


def _texts(el: ET.Element) -> list[str]:
    return [t.text or "" for t in el.iter(f"{SVG_NS}text")]


def test_a_one_line_station_is_numbered_in_its_section_ringed_in_its_line(tokyo):
    theme = resolve_stack("tokyo-metro").light
    graph = parse_metro_mermaid(_MAP)
    rsem = _station(tokyo, "rsem")
    assert _texts(rsem) == ["B", "04"]
    face = next(rsem.iter(f"{SVG_NS}rect"))
    line = graph.lines["star_rsem"]
    assert face.get("stroke") == theme.line_colors[line.palette_index]
    assert face.get("fill") == theme.station_fill


def test_a_station_on_several_lines_is_one_marker_ringed_in_the_station_colour(
    tokyo,
):
    theme = resolve_stack("tokyo-metro").light
    star = _station(tokyo, "star")
    faces = list(star.iter(f"{SVG_NS}rect"))
    assert len(faces) == 1
    assert faces[0].get("stroke") == theme.station_stroke
    assert _texts(star) == ["B", "01"]


def test_numbers_count_along_the_flow_of_each_section(tokyo):
    codes = {
        sid: "".join(_texts(_station(tokyo, sid)))
        for sid in ("cat_fastq", "fastqc_raw", "infer_strandedness")
    }
    assert codes == {
        "cat_fastq": "A01",
        "fastqc_raw": "A02",
        "infer_strandedness": "A03",
    }


def test_a_nameless_line_end_takes_a_bar_rather_than_a_number(tokyo):
    bars = classed(tokyo, "rect", "nf-metro-terminus-bar")
    assert {bar.get("data-station-id") for bar in bars} >= {"fastq_in"}
    assert not any(t == "00" for t in _texts(tokyo))


def test_section_badges_carry_the_section_letter(tokyo):
    badge_texts = {
        el.get("data-section-id"): el.text
        for el in tokyo.iter(f"{SVG_NS}text")
        if el.get("data-section-id") and not el.get("class")
    }
    assert badge_texts["preprocessing"] == "A"


def test_the_station_code_keeps_its_own_colour_in_dark_mode():
    dark = resolve_stack("tokyo-metro").dark
    assert dark.station_code_color == "#000000"
    assert dark.label_color != dark.station_code_color


def test_the_station_code_takes_the_station_stroke_when_unset(tmp_path):
    path = write(tmp_path, "t.yaml", "name: t\nstation_stroke: '#123456'\n")
    assert resolve_stack(str(path)).light.station_code_color == "#123456"


def test_legend_bullets_carry_each_line_code_in_a_readable_colour():
    root = _svg(_MAP, "nyc-subway")
    theme = resolve_stack("nyc-subway").light
    graph = parse_metro_mermaid(_MAP)
    texts = {
        el.text: el.get("fill")
        for el in root.iter(f"{SVG_NS}text")
        if el.text in {"A", "B", "C", "D", "E", "F"}
    }
    assert set(texts) == {"A", "B", "C", "D", "E", "F"}
    yellow = theme.line_colors[graph.lines["bowtie2_salmon"].palette_index]
    assert yellow == "#F6BC26"
    assert texts["D"] == "#000000"
    assert texts["A"] == "#ffffff"


def test_paris_bullets_number_the_lines():
    root = _svg(_MAP, "paris-metro")
    assert {"1", "6"} <= {el.text for el in root.iter(f"{SVG_NS}text")}


@pytest.mark.parametrize(
    ("index", "style", "code"),
    [
        (1, "letters", "A"),
        (26, "letters", "Z"),
        (27, "letters", "AA"),
        (53, "letters", "BA"),
        (12, "numbers", "12"),
    ],
)
def test_ordinal_codes(index, style, code):
    assert ordinal_code(index, style) == code


@pytest.mark.parametrize(
    ("backgrounds", "text"),
    [
        (("#FFD100",), "#000000"),
        (("#0019A8",), "#ffffff"),
        (("#000000", "#F0F0F0"), "#ffffff"),
    ],
)
def test_bullet_text_takes_the_better_worst_case_contrast(backgrounds, text):
    assert readable_text_color(*backgrounds) == text


def test_a_beside_label_gap_widens_only_with_a_larger_label_offset():
    assert beside_label_gap() == pytest.approx(TB_LABEL_H_SPACING)
    with metrics_context(label_offset=16.0):
        assert beside_label_gap() == pytest.approx(TB_LABEL_H_SPACING + 5.0)
    with metrics_context(label_offset=4.0):
        assert beside_label_gap() == pytest.approx(TB_LABEL_H_SPACING)


@pytest.mark.parametrize(
    "theme", ["nyc-subway", "paris-metro", "tokyo-metro", "washington-metro"]
)
@pytest.mark.parametrize("mode", ["light", "dark"])
def test_each_transit_theme_draws_the_rnaseq_map_in_its_palette(theme, mode):
    root = _svg(_MAP, theme, mode)
    palette = set(getattr(resolve_stack(theme), mode).line_colors)
    strokes = {
        el.get("stroke")
        for el in root.iter(f"{SVG_NS}path")
        if el.get("data-line-id") and el.get("fill") == "none"
    }
    assert strokes and strokes <= palette


def test_only_a_numbered_theme_styles_station_codes():
    rule = ".nf-metro-station-code"
    assert rule not in render_string(TWO_LINES)
    assert rule not in render_string(TWO_LINES, theme="nyc-subway")
    assert rule in render_string(
        _MAP, theme="tokyo-metro", source_dir=str(RNASEQ.parent)
    )


def test_a_line_ringed_dot_greys_with_its_own_inactive_line(tmp_path):
    from nf_metro.api import RenderConfig

    theme = write(
        tmp_path,
        "dots.yaml",
        "name: d\nextends: nyc-subway\ndot_ring: line\ndot_ring_width: 0.1\n",
    )
    light = resolve_stack(str(theme)).light
    root = ET.fromstring(
        render_string(
            TWO_LINES,
            theme=str(theme),
            mode="light",
            config=RenderConfig(inactive_line_ids=frozenset({"alt"})),
        )
    )
    strokes = sorted(
        dot.get("stroke") for dot in _station(root, "b").iter(f"{SVG_NS}circle")
    )
    graph = parse_metro_mermaid(TWO_LINES)
    main = light.line_colors[graph.lines["main"].palette_index]
    assert strokes == sorted([main, light.muted_line_color])


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_a_greyed_bullet_reads_against_both_modes_greys(mode):
    from nf_metro.api import RenderConfig

    nyc = resolve_stack("nyc-subway")
    grey_pair = (nyc.light.muted_line_color, nyc.dark.muted_line_color)
    svg = render_string(
        _MAP,
        theme="nyc-subway",
        mode=mode,
        source_dir=str(RNASEQ.parent),
        config=RenderConfig(inactive_line_ids=frozenset({"star_rsem"})),
    )
    bullet = next(
        el for el in ET.fromstring(svg).iter(f"{SVG_NS}text") if el.text == "A"
    )
    assert bullet.get("fill") == readable_text_color(*grey_pair)
