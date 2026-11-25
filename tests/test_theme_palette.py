"""Theme line palettes: ``line_colors`` picks, per mode, and in the drawing."""

from __future__ import annotations

import math
import textwrap
import xml.etree.ElementTree as ET

import pytest
from theme_helpers import MMD, SVG_NS, write

from nf_metro.api import render_string
from nf_metro.parser import parse_metro_mermaid
from nf_metro.render.style import line_color_pair
from nf_metro.themes import ThemeError, resolve_stack

THREE_LINES = textwrap.dedent("""\
    %%metro line: a | A | #111111
    %%metro line: b | B | #222222
    %%metro line: c | C | #333333
    graph LR
        s -->|a,b,c| t
""")


def _palette_theme(tmp_path, body: str) -> str:
    return str(write(tmp_path, "palette.yaml", "name: p\n" + textwrap.dedent(body)))


def _edge_strokes(svg: str) -> dict[str, str]:
    root = ET.fromstring(svg)
    return {
        el.get("data-line-id"): el.get("stroke")
        for tag in ("path", "line")
        for el in root.iter(f"{SVG_NS}{tag}")
        if el.get("data-line-id") and el.get("fill") in (None, "none")
    }


def test_lines_take_the_palette_in_declaration_order_then_new_colours(tmp_path):
    theme = _palette_theme(tmp_path, 'line_colors: ["#aa0000", "#00aa00"]\n')
    strokes = _edge_strokes(render_string(THREE_LINES, theme=theme, mode="light"))
    assert (strokes["a"], strokes["b"]) == ("#aa0000", "#00aa00")
    assert strokes["c"] not in {"#aa0000", "#00aa00", "#111111", "#222222", "#333333"}


def test_declaration_order_survives_a_layout_reordering_of_the_lines():
    graph = parse_metro_mermaid(THREE_LINES)
    graph.lines = dict(reversed(graph.lines.items()))
    assert [ln.palette_index for ln in graph.lines.values()] == [2, 1, 0]


def test_without_a_palette_lines_keep_their_own_colours():
    strokes = _edge_strokes(render_string(THREE_LINES, mode="light"))
    assert strokes == {"a": "#111111", "b": "#222222", "c": "#333333"}


def test_a_later_layer_with_an_empty_palette_restores_the_map_colours(tmp_path):
    reset = write(tmp_path, "reset.yaml", "name: r\nline_colors: []\n")
    strokes = _edge_strokes(
        render_string(THREE_LINES, theme=f"london-tube, {reset}", mode="light")
    )
    assert strokes == {"a": "#111111", "b": "#222222", "c": "#333333"}


def test_each_mode_bakes_its_own_palette(tmp_path):
    theme = _palette_theme(
        tmp_path,
        """\
        light:
          line_colors: ["#000000", "#550000"]
        dark:
          line_colors: ["#ffffff", "#ffaaaa"]
        """,
    )
    for mode, colours in (
        ("light", {"main": "#000000", "alt": "#550000"}),
        ("dark", {"main": "#ffffff", "alt": "#ffaaaa"}),
    ):
        strokes = _edge_strokes(render_string(MMD, theme=theme, mode=mode))
        assert strokes == colours


def test_a_palette_set_for_one_mode_leaves_the_other_with_the_map_colours(
    tmp_path,
):
    theme = resolve_stack(
        _palette_theme(tmp_path, 'light:\n  line_colors: ["#abcdef"]\n')
    )
    line = parse_metro_mermaid(MMD).lines["main"]
    assert line_color_pair(theme.light, line) == ("#abcdef", "#ff0000")


def test_lines_whose_palettes_differ_follow_the_viewer_mode(tmp_path):
    theme = _palette_theme(
        tmp_path,
        """\
        line_colors: ["#0019a8", "#00aa00"]
        dark:
          line_colors: ["#2948ff", "#00aa00"]
        """,
    )
    svg = render_string(MMD, theme=theme)
    assert (
        ".nf-metro-line-paint-0-stroke { stroke: light-dark(#0019a8, #2948ff); }" in svg
    )
    assert "nf-metro-line-paint-1" not in svg
    main = ET.fromstring(svg).find(f".//{SVG_NS}path[@data-line-id='main']")
    if main is None:
        main = ET.fromstring(svg).find(f".//{SVG_NS}line[@data-line-id='main']")
    assert "nf-metro-line-paint-0-stroke" in main.get("class").split()


def test_an_inactive_line_greys_instead_of_taking_its_palette_colour(tmp_path):
    theme = _palette_theme(
        tmp_path,
        """\
        line_colors: ["#0019a8", "#00aa00"]
        dark:
          line_colors: ["#2948ff", "#00aa00"]
        """,
    )
    from nf_metro.api import RenderConfig

    svg = render_string(
        MMD,
        theme=theme,
        mode="light",
        config=RenderConfig(inactive_line_ids=frozenset({"main"})),
    )
    assert "nf-metro-line-paint-0" not in svg
    grey = resolve_stack(theme).light.muted_line_color
    assert _edge_strokes(svg)["main"] == grey


def test_london_tube_draws_the_tfl_colours_and_keeps_northern_visible_in_dark():
    tube = resolve_stack("london-tube")
    assert tube.light.line_colors[:3] == ("#DC241F", "#0019A8", "#007D32")
    assert tube.light.line_colors[-1] == "#000000"
    assert tube.dark.line_colors[-1] == "#F0F0F0"
    assert len(tube.light.line_colors) == len(tube.dark.line_colors)


def test_a_palette_entry_must_be_a_colour(tmp_path):
    with pytest.raises(ThemeError, match="is not a colour"):
        resolve_stack(_palette_theme(tmp_path, 'line_colors: ["#zz0000"]\n'))


def test_the_html_legend_and_manifest_carry_the_theme_colours():
    from nf_metro.api import RenderConfig
    from nf_metro.render.manifest import build_manifest

    tube = resolve_stack("london-tube")
    graph = parse_metro_mermaid(THREE_LINES)
    html = render_string(
        THREE_LINES,
        theme="london-tube",
        config=RenderConfig(output_format="html"),
    )
    light, dark = tube.light.mode_line_colors
    assert f"light-dark({light[1]}, {dark[1]})" in html
    assert light[0] in html
    assert '"#111111"' not in html
    manifest = build_manifest(
        graph,
        width=10,
        height=10,
        station_radius=1.0,
        line_colors={lid: dark[ln.palette_index] for lid, ln in graph.lines.items()},
    )
    assert [g["color"] for g in manifest["groups"]] == list(dark[:3])


@pytest.mark.parametrize(
    "name",
    ["london-tube", "nyc-subway", "paris-metro", "tokyo-metro", "washington-metro"],
)
def test_every_dark_line_colour_reads_against_its_background(name):
    from nf_metro.render.palette import contrast_ratio

    dark = resolve_stack(name).dark
    for colour in dark.line_colors:
        assert contrast_ratio(colour, dark.background_fill) >= 3.0, colour


def _many_lines(count: int) -> str:
    ids = [f"l{i}" for i in range(count)]
    decl = "".join(f"%%metro line: {i} | {i.upper()} | #123456\n" for i in ids)
    return decl + "graph LR\n    s -->|" + ",".join(ids) + "| t\n"


def _wcag(a: str, b: str) -> float:
    from nf_metro.render.palette import contrast_ratio

    return contrast_ratio(a, b)


def test_lines_past_the_palette_get_distinct_readable_colours():
    from nf_metro.render.palette import _oklab, _rgb, line_color_pair

    wmata = resolve_stack("washington-metro")
    graph = parse_metro_mermaid(_many_lines(10))
    pairs = [line_color_pair(wmata.light, ln) for ln in graph.lines.values()]
    light = [p[0] for p in pairs]
    dark = [p[1] for p in pairs]
    assert light[:6] == list(wmata.light.line_colors)
    assert len(set(light)) == 10
    labs = [_oklab(_rgb(c)) for c in light]
    nearest = min(math.dist(a, b) for i, a in enumerate(labs) for b in labs[i + 1 :])
    assert nearest > 0.05
    for colour in light[6:]:
        assert _wcag(colour, wmata.light.background_fill) >= 3.0
    for colour in dark[6:]:
        assert _wcag(colour, wmata.dark.background_fill) >= 3.0


def test_a_generated_colour_keeps_its_hue_in_dark_mode():
    import colorsys

    from nf_metro.render.palette import _rgb, line_color_pair

    wmata = resolve_stack("washington-metro")
    line = list(parse_metro_mermaid(_many_lines(8)).lines.values())[7]
    light, dark = line_color_pair(wmata.light, line)
    hue_light = colorsys.rgb_to_hls(*_rgb(light))[0]
    hue_dark = colorsys.rgb_to_hls(*_rgb(dark))[0]
    assert hue_light == pytest.approx(hue_dark, abs=0.02)


def test_generated_colours_are_the_same_on_every_render():
    text = _many_lines(9)
    first = render_string(text, theme="washington-metro", mode="light")
    assert render_string(text, theme="washington-metro", mode="light") == first
