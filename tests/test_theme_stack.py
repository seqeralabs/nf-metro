"""Theme stacks, theme files, theme layout options, and the shape fields they set."""

from __future__ import annotations

import re
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from click.testing import CliRunner

from nf_metro.api import prepare_graph, render_string, resolve_theme
from nf_metro.cli import cli
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.style import color_pair
from nf_metro.themes import (
    NFCORE_DARK_THEME,
    NFCORE_LIGHT_THEME,
    PAIRED_THEMES,
    SEQERA_LIGHT_THEME,
    THEMES,
    ThemeError,
    check_stack,
    resolve_stack,
)

_ROOT = Path(__file__).resolve().parents[1]
_RNASEQ = _ROOT / "examples" / "rnaseq_sections.mmd"
_TUBE = {"light": THEMES["london-tube-light"], "dark": THEMES["london-tube-dark"]}

_MMD = textwrap.dedent("""\
    %%metro line: main | Main | #ff0000
    %%metro line: alt | Alt | #0000ff
    graph LR
        a -->|main,alt| b
        b -->|main| c
        b -->|alt| d
""")


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body))
    return path


# ---------------------------------------------------------------------------
# Built-in themes
# ---------------------------------------------------------------------------


def test_single_builtin_names_resolve_to_the_registered_objects():
    graph = parse_metro_mermaid(_MMD)
    assert resolve_theme("nfcore", graph, mode="light") is NFCORE_LIGHT_THEME
    assert resolve_theme("seqera", graph, mode="light") is SEQERA_LIGHT_THEME
    assert resolve_theme(None, graph, mode="dark") is NFCORE_DARK_THEME


def test_default_themes_set_no_layout_options():
    for name in ("nfcore", "seqera", "light", "london-tube"):
        assert resolve_stack(name).layout == {}


def test_london_tube_sets_its_look_and_inherits_the_rest_from_nfcore():
    light = _TUBE["light"]
    assert light.station_glyph == "tick"
    assert light.section_badges is False
    assert light.section_label_uppercase is True
    assert light.label_color == "#1c3f94"
    assert light.title_font_size == NFCORE_LIGHT_THEME.title_font_size
    assert light.terminus_width == NFCORE_LIGHT_THEME.terminus_width
    assert color_pair(light, "label_color") == ("#1c3f94", "#dfe6ff")


# ---------------------------------------------------------------------------
# Stacking: left to right, later wins per property, modes merge separately
# ---------------------------------------------------------------------------


def test_later_entry_wins_per_property():
    over_nfcore = resolve_stack("nfcore, london-tube").light
    assert over_nfcore.station_glyph == "tick"
    assert over_nfcore.label_color == _TUBE["light"].label_color

    under_nfcore = resolve_stack("london-tube, nfcore").light
    assert under_nfcore.label_color == NFCORE_LIGHT_THEME.label_color
    assert under_nfcore.station_glyph == "tick", "nfcore does not set the glyph"


def test_properties_a_later_entry_leaves_unset_come_from_the_earlier_one():
    stacked = resolve_stack("seqera, london-tube").light
    assert stacked.station_glyph == "tick"
    assert stacked.legend_font_size == SEQERA_LIGHT_THEME.legend_font_size
    assert stacked.label_font_size == SEQERA_LIGHT_THEME.label_font_size


def test_mixed_builtin_and_file_stack(tmp_path):
    path = _write(
        tmp_path,
        "purple.yaml",
        """\
        name: purple
        light:
          label_color: "#5a2d82"
        """,
    )
    stack = resolve_stack(f"seqera, london-tube, {path}")
    assert stack.light.label_color == "#5a2d82"
    assert stack.light.station_glyph == "tick"
    assert stack.light.legend_font_size == SEQERA_LIGHT_THEME.legend_font_size
    assert stack.dark.label_color == _TUBE["dark"].label_color


def test_file_entries_resolve_relative_to_the_base_dir(tmp_path):
    _write(tmp_path, "glyph.yaml", "name: glyph\nstation_glyph: tick\n")
    assert resolve_stack("glyph.yaml", tmp_path).light.station_glyph == "tick"


def test_extends_is_shorthand_for_stacking_the_base_first(tmp_path):
    path = _write(
        tmp_path,
        "house.yaml",
        """\
        name: house
        extends: london-tube
        light:
          label_color: "#5a2d82"
        """,
    )
    via_extends = resolve_stack(str(path))
    via_stack = resolve_stack(f"london-tube, {path}")
    for mode in ("light", "dark"):
        a, b = getattr(via_extends, mode), getattr(via_stack, mode)
        assert {k: v for k, v in vars(a).items() if k not in ("name", "brand")} == {
            k: v for k, v in vars(b).items() if k not in ("name", "brand")
        }


def test_top_level_fields_apply_to_both_modes_and_per_mode_blocks_win(tmp_path):
    path = _write(
        tmp_path,
        "modes.yaml",
        """\
        name: modes
        label_color: "#111111"
        dark:
          label_color: "#eeeeee"
        """,
    )
    stack = resolve_stack(str(path))
    assert stack.light.label_color == "#111111"
    assert stack.dark.label_color == "#eeeeee"


def test_a_stacked_theme_carries_both_palettes():
    graph = parse_metro_mermaid(_MMD)
    light = resolve_theme("seqera, london-tube", graph, mode="light")
    assert color_pair(light, "label_color") == (
        _TUBE["light"].label_color,
        _TUBE["dark"].label_color,
    )
    svg = render_string(_MMD, theme="seqera, london-tube")
    assert "light-dark(#1c3f94, #dfe6ff)" in svg


# ---------------------------------------------------------------------------
# Invalid entries and invalid files
# ---------------------------------------------------------------------------


def test_unknown_builtin_in_a_stack_is_rejected():
    with pytest.raises(ThemeError, match="unknown theme 'bogus'"):
        check_stack("nfcore, bogus")


def test_missing_theme_file_is_rejected(tmp_path):
    with pytest.raises(ThemeError, match="not found"):
        resolve_stack(f"nfcore, {tmp_path / 'absent.yaml'}")


@pytest.mark.parametrize(
    "body,message",
    [
        ("name: x\nstation_colour: red\n", "unknown theme field 'station_colour'"),
        ("name: x\nlabel_color: '#12345'\n", "label_color is not a colour"),
        ("name: x\nline_width: thick\n", "line_width must be a number"),
        ("name: x\nsection_badges: 1\n", "section_badges must be true or false"),
        ("name: x\nstation_glyph: dot\n", "station_glyph must be one of"),
        ("name: x\nlayout:\n  fold_threshold: 3\n", "not a layout option"),
        ("name: x\nlayout:\n  track_gap: wide\n", "layout track_gap expects"),
        ("station_glyph: tick\n", "needs a non-empty 'name'"),
        ("- name\n", "must be a YAML mapping"),
    ],
)
def test_invalid_theme_files_name_the_problem(tmp_path, body, message):
    path = _write(tmp_path, "bad.yaml", body)
    with pytest.raises(ThemeError, match=re.escape(message)):
        resolve_stack(str(path))


def test_extends_cycle_is_rejected(tmp_path):
    _write(tmp_path, "a.yaml", "name: a\nextends: b.yaml\n")
    _write(tmp_path, "b.yaml", "name: b\nextends: a.yaml\n")
    with pytest.raises(ThemeError, match="cycle"):
        resolve_stack("a.yaml", tmp_path)


def test_directive_with_an_unknown_entry_warns_and_keeps_the_default():
    with pytest.warns(UserWarning, match="ignoring"):
        graph = parse_metro_mermaid("%%metro style: nfcore, bogus\n" + _MMD)
    assert graph.style == "dark"


def test_directive_accepts_a_stack():
    graph = parse_metro_mermaid("%%metro style: seqera, london-tube\n" + _MMD)
    assert resolve_theme(None, graph, mode="light").station_glyph == "tick"


def test_cli_rejects_an_unknown_entry(tmp_path):
    src = _write(tmp_path, "m.mmd", _MMD)
    result = CliRunner().invoke(
        cli, ["render", str(src), "-o", str(tmp_path / "m.svg"), "--theme", "x,y"]
    )
    assert result.exit_code != 0
    assert "unknown theme" in result.output


# ---------------------------------------------------------------------------
# Layout options: CLI flag > directive > theme > built-in default
# ---------------------------------------------------------------------------


@pytest.fixture
def spacing_theme(tmp_path):
    return _write(
        tmp_path,
        "spacing.yaml",
        """\
        name: spacing
        layout:
          x_spacing: 91
          y_spacing: 47
        """,
    )


def test_theme_layout_option_applies_when_the_map_sets_none(spacing_theme):
    graph = prepare_graph(_MMD, theme=str(spacing_theme))
    assert graph.x_spacing == 91
    assert graph.y_spacing == 47


def test_directive_beats_theme_layout_option(spacing_theme):
    graph = prepare_graph("%%metro x_spacing: 70\n" + _MMD, theme=str(spacing_theme))
    assert graph.x_spacing == 70
    assert graph.y_spacing == 47


def test_cli_option_beats_directive_and_theme(spacing_theme):
    graph = prepare_graph(
        "%%metro x_spacing: 70\n" + _MMD,
        theme=str(spacing_theme),
        layout_options={"x_spacing": 55, "y_spacing": 33},
    )
    assert graph.x_spacing == 55
    assert graph.y_spacing == 33


def test_directive_theme_file_sets_layout_relative_to_the_map(spacing_theme):
    text = f"%%metro style: nfcore, {spacing_theme.name}\n" + _MMD
    graph = prepare_graph(text, source_dir=str(spacing_theme.parent))
    assert graph.x_spacing == 91


def test_without_a_theme_option_layout_keeps_the_builtin_default():
    assert prepare_graph(_MMD).x_spacing is None


# ---------------------------------------------------------------------------
# What the london-tube shape fields draw
# ---------------------------------------------------------------------------


_SVG_NS = "{http://www.w3.org/2000/svg}"


@pytest.fixture(scope="module")
def tube_svg() -> ET.Element:
    svg = render_string(
        _RNASEQ.read_text(), theme="london-tube", source_dir=str(_RNASEQ.parent)
    )
    return ET.fromstring(svg)


def _station_glyphs(root: ET.Element) -> dict[str, ET.Element]:
    return {
        el.get("data-station-id"): el
        for el in root.iter(f"{_SVG_NS}rect")
        if "nf-metro-station" in (el.get("class") or "").split()
    }


def _labels(root: ET.Element) -> dict[str, tuple[float, float]]:
    return {
        el.get("data-station-id"): (float(el.get("x")), float(el.get("y")))
        for el in root.iter(f"{_SVG_NS}text")
        if "nf-metro-station-label" in (el.get("class") or "").split()
    }


def test_ticks_point_at_their_labels(tube_svg):
    glyphs = _station_glyphs(tube_svg)
    labels = _labels(tube_svg)
    ticks = {
        sid: el
        for sid, el in glyphs.items()
        if len(el.get("data-station-lines", "").split(",")) == 1
        and sid in labels
        and el.get("rx") is None
    }
    assert {"rsem", "hisat2_align", "bowtie2_align"} <= set(ticks)
    for sid, tick in ticks.items():
        y, h = float(tick.get("y")), float(tick.get("height"))
        x, w = float(tick.get("x")), float(tick.get("width"))
        label_x, label_y = labels[sid]
        if h > w:
            line_y = y if label_y > y + h / 2 else y + h
            assert (label_y > line_y) == (y >= line_y - 0.01), sid
        else:
            line_x = x if label_x > x + w / 2 else x + w
            assert (label_x > line_x) == (x >= line_x - 0.01), sid


def test_ticks_take_their_line_colour(tube_svg):
    glyphs = _station_glyphs(tube_svg)
    assert glyphs["rsem"].get("fill") == "#0570b0"


def test_interchanges_are_outlined_capsules(tube_svg):
    star = _station_glyphs(tube_svg)["star"]
    assert star.get("fill") == "#ffffff"
    assert star.get("stroke") == "#000000"
    assert float(star.get("stroke-width")) == 2.5
    assert star.get("rx") is not None


def test_sections_are_unboxed_with_plain_uppercase_titles(tube_svg):
    assert not [
        el
        for el in tube_svg.iter(f"{_SVG_NS}circle")
        if "nf-metro-section-num-circle" in (el.get("class") or "")
    ]
    boxes = [
        el
        for el in tube_svg.iter(f"{_SVG_NS}rect")
        if "nf-metro-section-box" in (el.get("class") or "")
    ]
    assert boxes and all(b.get("stroke-dasharray") == "6 4" for b in boxes)
    titles = [
        el.text
        for el in tube_svg.iter(f"{_SVG_NS}text")
        if "nf-metro-section-label" in (el.get("class") or "")
    ]
    assert "PRE-PROCESSING" in titles


def test_nfcore_render_is_untouched_by_the_shape_fields():
    root = ET.fromstring(
        render_string(_RNASEQ.read_text(), source_dir=str(_RNASEQ.parent))
    )
    assert _station_glyphs(root)["rsem"].get("rx") is not None
    assert [
        el
        for el in root.iter(f"{_SVG_NS}circle")
        if "nf-metro-section-num-circle" in (el.get("class") or "")
    ]


def test_every_builtin_name_is_in_the_flat_registry():
    for brand in PAIRED_THEMES:
        assert {brand, f"{brand}-light", f"{brand}-dark"} <= set(THEMES)


# ---------------------------------------------------------------------------
# Names, single-palette themes, and schema-driven chrome
# ---------------------------------------------------------------------------


def test_aliases_name_the_same_theme():
    assert resolve_stack("light") is resolve_stack("transparent")
    assert resolve_stack("dark") is resolve_stack("nfcore")
    assert THEMES["light"] is THEMES["transparent"]


def test_pinned_names_share_their_theme_objects():
    assert THEMES["nfcore-light"] is resolve_stack("nfcore").light
    assert resolve_stack("nfcore-light").for_mode("dark") is THEMES["nfcore-light"]


def test_fixed_mode_file_renders_one_palette_for_every_mode(tmp_path):
    path = _write(
        tmp_path,
        "flat.yaml",
        """\
        name: flat
        fixed_mode: light
        light:
          label_color: "#123456"
        """,
    )
    stack = resolve_stack(str(path))
    for mode in ("light", "dark"):
        theme = stack.for_mode(mode)
        assert theme.label_color == "#123456"
        assert not theme.mode_colors


def test_new_chrome_colours_follow_the_viewer():
    svg = render_string(_MMD, theme="london-tube")
    assert "stroke: var(--nfm-map-legend-stroke, light-dark(#000000, #ffffff))" in svg


def test_every_chrome_binding_reaches_the_stylesheet():
    from nf_metro.render.style import chrome_bindings

    svg = render_string(_MMD, theme="london-tube")
    for binding in chrome_bindings():
        assert binding.css_var in svg, binding.css_var
