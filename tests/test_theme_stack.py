"""Theme stacks: names, files, merge order, and the layout options they set."""

from __future__ import annotations

import re

import pytest
from click.testing import CliRunner
from theme_helpers import MMD, ROOT, write

from nf_metro.api import prepare_graph, render_string
from nf_metro.cli import cli
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.style import color_pair, is_adaptive
from nf_metro.themes import (
    DEFAULT_DARK_THEME,
    DEFAULT_LIGHT_THEME,
    SEQERA_LIGHT_THEME,
    STYLE_NAMES,
    THEMES,
    ThemeError,
    bound_stack,
    check_stack,
    resolve_stack,
    resolve_theme,
)

_TUBE = resolve_stack("london-tube")


# ---------------------------------------------------------------------------
# Built-in themes and names
# ---------------------------------------------------------------------------


def test_a_bound_stack_resolves_to_the_registered_objects():
    graph = parse_metro_mermaid(MMD)
    assert resolve_theme(graph, mode="dark") is DEFAULT_DARK_THEME
    bound_stack(graph, "seqera")
    assert resolve_theme(graph, mode="light") is SEQERA_LIGHT_THEME


def test_the_registry_holds_every_built_in():
    assert set(THEMES) == STYLE_NAMES


@pytest.mark.parametrize(
    "name", ["dark", "light", "nfcore", "nfcore-light", "seqera-dark"]
)
def test_modes_are_not_theme_names(name):
    with pytest.raises(ThemeError, match="unknown theme"):
        check_stack(name)


@pytest.mark.parametrize(
    ("name", "replacement"),
    [
        ("nfcore", "use 'default'"),
        ("nfcore-dark", "'%%metro mode: dark'"),
        ("seqera-light", "use 'seqera'"),
        ("dark", "it is a mode"),
        ("light", "use 'transparent'"),
    ],
)
def test_a_retired_theme_name_says_what_replaces_it(name, replacement):
    with pytest.raises(ThemeError, match="no longer a theme name") as excinfo:
        check_stack(name)
    assert replacement in str(excinfo.value)


def test_a_retired_style_directive_warns_with_its_replacement():
    with pytest.warns(UserWarning, match="no longer a theme name: use 'default'"):
        parse_metro_mermaid(
            "%%metro style: nfcore\n%%metro line: m | M | #f00\n"
            "graph LR\n    a -->|m| b\n"
        )


def test_an_unknown_theme_name_has_no_rename_hint():
    with pytest.raises(ThemeError) as excinfo:
        check_stack("bogus")
    assert "no longer" not in str(excinfo.value)


def test_only_london_tube_sets_layout_options():
    for name in STYLE_NAMES - {"london-tube"}:
        assert resolve_stack(name).layout == {}
    assert resolve_stack("london-tube").layout == {"curve_radius_ratio": 3.5}


def test_london_tube_sets_its_look_and_inherits_the_rest_from_default():
    light = _TUBE.light
    assert light.station_glyph == "tick"
    assert light.number_sections is False
    assert light.uppercase_section_labels is True
    assert light.label_color == "#0019A8"
    assert light.title_font_size == DEFAULT_LIGHT_THEME.title_font_size
    assert light.icon_width == DEFAULT_LIGHT_THEME.icon_width
    assert color_pair(light, "label_color") == ("#0019A8", "#dfe6ff")


def test_a_theme_of_top_level_colours_has_one_palette():
    transparent = resolve_stack("transparent")
    assert not is_adaptive(transparent.light)
    assert transparent.light.label_color == transparent.dark.label_color


def test_empty_derived_colours_take_their_source_field(tmp_path):
    stack = resolve_stack(
        str(write(tmp_path, "s.yaml", 'name: s\nstation_stroke: "#123456"\n'))
    )
    for theme in (stack.light, stack.dark):
        assert theme.icon_stroke == theme.section_badge_fill == "#123456"
        assert theme.icon_fill == theme.station_fill
    assert stack.light.marker_stroke == DEFAULT_LIGHT_THEME.marker_stroke


# ---------------------------------------------------------------------------
# Stacking: left to right, later wins per property, modes merge separately
# ---------------------------------------------------------------------------


def test_later_entry_wins_per_property():
    seqera = resolve_stack("seqera").light
    over_seqera = resolve_stack("seqera, london-tube").light
    assert over_seqera.station_glyph == "tick"
    assert over_seqera.label_color == _TUBE.light.label_color

    under_seqera = resolve_stack("london-tube, seqera").light
    assert under_seqera.label_color == seqera.label_color
    assert under_seqera.station_glyph == "tick", "seqera does not set the glyph"


def test_properties_a_later_entry_leaves_unset_come_from_the_earlier_one():
    stacked = resolve_stack("seqera, london-tube").light
    assert stacked.station_glyph == "tick"
    assert stacked.legend_font_size == SEQERA_LIGHT_THEME.legend_font_size
    assert stacked.label_font_size == SEQERA_LIGHT_THEME.label_font_size


def test_mixed_builtin_and_file_stack(tmp_path):
    path = write(
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
    assert stack.dark.label_color == _TUBE.dark.label_color


def test_file_entries_resolve_relative_to_the_base_dir(tmp_path):
    write(tmp_path, "glyph.yaml", "name: glyph\nstation_glyph: tick\n")
    assert resolve_stack("glyph.yaml", tmp_path).light.station_glyph == "tick"


def test_extends_is_shorthand_for_stacking_the_base_first(tmp_path):
    path = write(
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
    assert via_extends.light == via_stack.light
    assert via_extends.dark == via_stack.dark


def test_extends_resolves_against_the_theme_files_own_folder(tmp_path, monkeypatch):
    themes = tmp_path / "themes"
    themes.mkdir()
    write(themes, "base.yaml", 'name: base\nlabel_color: "#123456"\n')
    child = write(themes, "child.yaml", "name: child\nextends: base.yaml\n")
    monkeypatch.chdir(tmp_path)
    assert resolve_stack(str(child)).light.label_color == "#123456"


def test_top_level_fields_apply_to_both_modes_and_per_mode_blocks_win(tmp_path):
    path = write(
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
    light = resolve_stack("seqera, london-tube").light
    assert color_pair(light, "label_color") == (
        _TUBE.light.label_color,
        _TUBE.dark.label_color,
    )
    svg = render_string(MMD, theme="seqera, london-tube")
    assert "light-dark(#0019A8, #dfe6ff)" in svg


# ---------------------------------------------------------------------------
# Unknown entries
# ---------------------------------------------------------------------------


def test_unknown_builtin_in_a_stack_is_rejected():
    with pytest.raises(ThemeError, match="unknown theme 'bogus'"):
        check_stack("seqera, bogus")


def test_missing_theme_file_is_rejected(tmp_path):
    with pytest.raises(ThemeError, match="not found"):
        resolve_stack(f"seqera, {tmp_path / 'absent.yaml'}")


def test_extends_cycle_is_rejected(tmp_path):
    write(tmp_path, "a.yaml", "name: a\nextends: b.yaml\n")
    write(tmp_path, "b.yaml", "name: b\nextends: a.yaml\n")
    with pytest.raises(ThemeError, match="cycle"):
        resolve_stack("a.yaml", tmp_path)


def test_directive_with_an_unknown_entry_warns_and_keeps_the_default():
    with pytest.warns(UserWarning, match="ignoring"):
        graph = parse_metro_mermaid("%%metro style: seqera, bogus\n" + MMD)
    assert graph.style == "default"


def test_directive_accepts_a_stack():
    graph = parse_metro_mermaid("%%metro style: seqera, london-tube\n" + MMD)
    assert resolve_theme(graph, mode="light").station_glyph == "tick"


def test_cli_rejects_an_unknown_entry(tmp_path):
    src = write(tmp_path, "m.mmd", MMD)
    result = CliRunner().invoke(
        cli, ["render", str(src), "-o", str(tmp_path / "m.svg"), "--theme", "x,y"]
    )
    assert result.exit_code != 0
    assert "unknown theme" in result.output


def test_per_output_theme_takes_a_quoted_stack():
    from nf_metro.parser.directives import _parse_output_overrides

    overrides = _parse_output_overrides('theme="seqera, london-tube" mode=light')
    assert overrides == {"theme": "seqera, london-tube", "mode": "light"}


def test_per_output_theme_rejects_an_unknown_name():
    from nf_metro.parser.directives import _parse_output_overrides

    with pytest.warns(UserWarning, match="bad value for theme"):
        assert _parse_output_overrides("theme=nosuchtheme") == {}


def test_playground_lists_the_two_palette_built_ins():
    paired = {name for name in STYLE_NAMES if is_adaptive(THEMES[name])}
    app = (ROOT / "website" / "public" / "playground" / "app.js").read_text()
    keys = re.search(r"const THEME_KEYS = \[(.*?)\];", app).group(1)
    assert {k.strip().strip('"') for k in keys.split(",")} == paired
    page = (ROOT / "website" / "src" / "pages" / "playground.astro").read_text()
    picker = re.search(r'<select id="opt-theme">(.*?)</select>', page, re.S).group(1)
    assert set(re.findall(r'<option value="([^"]+)"', picker)) == paired


# ---------------------------------------------------------------------------
# Layout options: CLI flag > directive > theme > built-in default
# ---------------------------------------------------------------------------


@pytest.fixture
def spacing_theme(tmp_path):
    return write(
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
    graph = prepare_graph(MMD, theme=str(spacing_theme))
    assert graph.x_spacing == 91
    assert graph.y_spacing == 47


def test_directive_beats_theme_layout_option(spacing_theme):
    graph = prepare_graph("%%metro x_spacing: 70\n" + MMD, theme=str(spacing_theme))
    assert graph.x_spacing == 70
    assert graph.y_spacing == 47


def test_cli_option_beats_directive_and_theme(spacing_theme):
    graph = prepare_graph(
        "%%metro x_spacing: 70\n" + MMD,
        theme=str(spacing_theme),
        layout_options={"x_spacing": 55, "y_spacing": 33},
    )
    assert graph.x_spacing == 55
    assert graph.y_spacing == 33


def test_directive_theme_file_sets_layout_relative_to_the_map(spacing_theme):
    text = f"%%metro style: {spacing_theme.name}\n" + MMD
    graph = prepare_graph(text, source_dir=str(spacing_theme.parent))
    assert graph.x_spacing == 91


def test_without_a_theme_option_layout_keeps_the_builtin_default():
    assert prepare_graph(MMD).x_spacing is None
