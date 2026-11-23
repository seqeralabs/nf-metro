"""Theme stacks, theme files, theme layout options, and the shape fields they set."""

from __future__ import annotations

import re
import textwrap
import xml.etree.ElementTree as ET
from dataclasses import replace
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


def _glyph_by_station(root: ET.Element, tag: str) -> dict[str, ET.Element]:
    return {
        el.get("data-station-id"): el
        for el in root.iter(f"{_SVG_NS}{tag}")
        if "nf-metro-station" in (el.get("class") or "").split()
    }


def test_one_line_stations_are_track_weight_tabs_in_their_line_colour(tube_svg):
    rsem = _station_glyphs(tube_svg)["rsem"]
    assert float(rsem.get("width")) == pytest.approx(1.0 * 4.0)
    assert float(rsem.get("height")) == pytest.approx(1.5 * 4.0)
    assert rsem.get("fill") == "#0570b0"


def test_interchanges_are_rings(tube_svg):
    star = _glyph_by_station(tube_svg, "g")["star"]
    rings = list(star.iter(f"{_SVG_NS}circle"))
    assert len(rings) == 1
    assert float(rings[0].get("r")) == pytest.approx(4.0 + 1.0)
    assert float(rings[0].get("stroke-width")) == pytest.approx(2.0)
    assert rings[0].get("stroke") == THEMES["london-tube-dark"].ring_color


def test_a_six_line_terminus_is_a_bridged_ring_chain(tube_svg):
    fastq = _glyph_by_station(tube_svg, "g")["fastq_in"]
    assert len(list(fastq.iter(f"{_SVG_NS}circle"))) == 3
    assert [
        el
        for el in fastq.iter(f"{_SVG_NS}rect")
        if "nf-metro-ring-bridge" in (el.get("class") or "")
    ]


# ---------------------------------------------------------------------------
# Tab, terminus bar and ring geometry, in multiples of the line width
# ---------------------------------------------------------------------------


class _At:
    def __init__(self, x: float, y: float) -> None:
        self.x, self.y = x, y


_LW = 4.0
_GEOM = replace(NFCORE_LIGHT_THEME, line_width=_LW)


@pytest.mark.parametrize("label_below", [True, False])
def test_tab_sits_on_the_label_side_of_the_line(label_below):
    from nf_metro.render.svg import _Bundle, station_tab_box

    label = (100.0, 60.0 if label_below else 40.0)
    x, y, w, h = station_tab_box(
        _Bundle(_At(100.0, 50.0), 0.0, 0.0, False), _GEOM, label
    )
    assert (w, h) == pytest.approx((0.66 * _LW, 0.66 * _LW))
    assert x + w / 2 == pytest.approx(100.0)
    if label_below:
        assert y == pytest.approx(50.0 + _LW / 2)
    else:
        assert y + h == pytest.approx(50.0 - _LW / 2)


def test_tab_width_and_length_are_separate_multiples_of_the_line():
    from nf_metro.render.svg import _Bundle, station_tab_box

    theme = replace(_GEOM, station_tick_width=1.0, station_tick_length=1.5)
    _, _, w, h = station_tab_box(
        _Bundle(_At(100.0, 50.0), 0.0, 0.0, False), theme, None
    )
    assert (w, h) == pytest.approx((1.0 * _LW, 1.5 * _LW))


def test_tab_in_a_vertical_section_faces_its_label_sideways():
    from nf_metro.render.svg import _Bundle, station_tab_box

    x, y, w, h = station_tab_box(
        _Bundle(_At(100.0, 50.0), 0.0, 0.0, True), _GEOM, (80.0, 50.0)
    )
    assert x + w == pytest.approx(100.0 - _LW / 2)
    assert y + h / 2 == pytest.approx(50.0)


@pytest.mark.parametrize("vertical", [False, True])
def test_terminus_bar_crosses_the_line_end_and_overhangs_both_sides(vertical):
    from nf_metro.render.svg import _Bundle, terminus_bar_box

    theme = replace(_GEOM, station_tick_width=1.0, station_tick_length=1.5)
    axis = 100.0 if vertical else 50.0
    x, y, w, h = terminus_bar_box(_Bundle(_At(100.0, 50.0), 0.0, 0.0, vertical), theme)
    thick, across = 1.0 * _LW, _LW + 2 * 1.5 * _LW
    if vertical:
        assert (w, h) == pytest.approx((across, thick))
        assert x + w / 2 == pytest.approx(axis)
    else:
        assert (w, h) == pytest.approx((thick, across))
        assert y + h / 2 == pytest.approx(axis)


@pytest.mark.parametrize(
    "lines,rings",
    [(2, 1), (3, 1), (4, 3), (6, 3), (8, 4)],
)
def test_ring_chain_length_follows_bundle_width(lines, rings):
    from nf_metro.render.svg import ring_centre_offsets

    span = (lines - 1) * _LW
    centres = ring_centre_offsets(-span / 2, span / 2, _GEOM)
    assert len(centres) == rings
    if rings > 1:
        assert centres[0] == pytest.approx(-span / 2)
        assert centres[-1] == pytest.approx(span / 2)
        gaps = {round(b - a, 6) for a, b in zip(centres, centres[1:])}
        assert max(gaps) <= (2 * 1.0 + 0.5) * _LW + 1e-6


def test_ring_geometry_scales_with_line_width():
    from nf_metro.render.svg import ring_centre_offsets

    wide = replace(_GEOM, line_width=2 * _LW)
    assert len(ring_centre_offsets(0.0, 4 * _LW, _GEOM)) == len(
        ring_centre_offsets(0.0, 8 * _LW, wide)
    )


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


def test_inactive_lines_take_the_theme_muted_grey():
    svg = render_string(
        _MMD, theme="london-tube", mode="light", inactive_line_ids=frozenset({"alt"})
    )
    assert 'stroke="#b3b3b3"' in svg


# ---------------------------------------------------------------------------
# The default two-palette SVG switches every theme colour with the mode
# ---------------------------------------------------------------------------


def test_unset_ring_colours_fall_back_and_still_follow_the_mode(tmp_path):
    path = _write(
        tmp_path,
        "rings.yaml",
        "name: rings\ninterchange_glyph: ring\n",
    )
    svg = render_string(_MMD, theme=f"seqera, {path}")
    stroke_light, stroke_dark = (
        THEMES["seqera-light"].station_stroke,
        THEMES["seqera-dark"].station_stroke,
    )
    assert "--nfm-map-ring-color" in svg
    fill = "var(--nfm-map-ring-fill, light-dark(#f8f9fa, #1a1625))"
    assert fill in svg
    if stroke_light != stroke_dark:
        assert f"light-dark({stroke_light}, {stroke_dark})" in svg


def test_pill_themes_emit_no_ring_rules():
    svg = render_string(_MMD, theme="nfcore")
    assert "nf-metro-ring" not in svg


def test_inactive_styling_follows_the_mode_in_the_default_svg():
    svg = render_string(
        _MMD_TWO_STATIONS, theme="london-tube", inactive_line_ids=frozenset({"alt"})
    )
    switch = "var(--nfm-map-muted-color, light-dark(#b3b3b3, #585a60))"
    assert f".nf-metro-muted-stroke {{ stroke: {switch}; }}" in svg
    assert f".nf-metro-muted-fill {{ fill: {switch}; }}" in svg
    root = ET.fromstring(svg)
    alt_paths = [
        el for el in root.iter() if el.get("data-line-id") == "alt" and el.get("stroke")
    ]
    assert alt_paths
    assert all("nf-metro-muted-stroke" in (el.get("class") or "") for el in alt_paths)
    tab = _station_glyphs(root)["d"]
    assert "nf-metro-muted-fill" in tab.get("class")


def test_nfcore_inactive_render_adds_no_switching_classes():
    svg = render_string(_MMD, theme="nfcore", inactive_line_ids=frozenset({"alt"}))
    assert "nf-metro-muted-stroke" not in svg
    assert "nf-metro-muted-fill" not in svg


_MMD_TWO_STATIONS = textwrap.dedent("""\
    %%metro line: main | Main | #ff0000
    %%metro line: alt | Alt | #0000ff
    graph LR
        a -->|main,alt| b
        b -->|main| c
        b -->|alt| d
        d -->|alt| e
""")


# ---------------------------------------------------------------------------
# Section titles measured as drawn
# ---------------------------------------------------------------------------


def test_uppercase_tracked_titles_are_measured_wider():
    from nf_metro.render.section_header import (
        HeaderLettering,
        estimate_section_label_width,
        header_lettering_context,
    )

    plain = estimate_section_label_width("Variant calling", 13.0)
    with header_lettering_context(HeaderLettering(True, 2.0)):
        drawn = estimate_section_label_width("Variant calling", 13.0)
    assert drawn >= plain + 2.0 * len("Variant calling")


def test_neighbouring_london_tube_titles_do_not_overlap():
    from nf_metro.render.svg import build_render_plan

    path = _ROOT / "examples" / "legend_logo_placement.mmd"
    graph = prepare_graph(
        path.read_text(), theme="london-tube", source_dir=str(path.parent)
    )
    plan = build_render_plan(graph, resolve_theme(None, graph))
    keepouts = sorted(
        (p.keepout for p in plan.header_placements.values() if not p.label_rotation),
        key=lambda k: (round(k[1]), k[0]),
    )
    for a, b in zip(keepouts, keepouts[1:]):
        if abs(a[1] - b[1]) < 1.0:
            assert a[2] <= b[0] + 1e-6


# ---------------------------------------------------------------------------
# Rings, fixed modes, theme files and per-output themes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lines", [2, 3, 4, 6, 8])
def test_rings_are_centred_on_their_bundle(lines):
    from nf_metro.render.svg import ring_centre_offsets

    lo = 10.0
    hi = lo + (lines - 1) * _LW
    centres = ring_centre_offsets(lo, hi, _GEOM)
    assert sum(centres) / len(centres) == pytest.approx((lo + hi) / 2)


def test_separate_tracks_get_separate_ring_groups():
    from nf_metro.render.svg import ring_centres

    groups = ring_centres([0.0, _LW, 40.0, 40.0 + _LW], _GEOM)
    assert len(groups) == 2
    assert groups[0] == [pytest.approx(_LW / 2)]
    assert groups[1] == [pytest.approx(40.0 + _LW / 2)]


def test_extends_resolves_against_the_theme_files_own_folder(tmp_path, monkeypatch):
    themes = tmp_path / "themes"
    themes.mkdir()
    _write(themes, "base.yaml", 'name: base\nlabel_color: "#123456"\n')
    child = _write(themes, "child.yaml", "name: child\nextends: base.yaml\n")
    monkeypatch.chdir(tmp_path)
    assert resolve_stack(str(child)).light.label_color == "#123456"


def test_a_later_entry_clears_a_fixed_mode():
    assert resolve_stack("transparent").fixed_mode == "dark"
    later = resolve_stack("transparent, seqera")
    assert later.fixed_mode is None
    assert later.for_mode("light").background_color == (
        THEMES["seqera-light"].background_color
    )


def test_fixed_mode_none_in_a_file_clears_it(tmp_path):
    path = _write(tmp_path, "free.yaml", "name: free\nfixed_mode: none\n")
    assert resolve_stack(f"transparent, {path}").fixed_mode is None


def test_per_output_theme_takes_a_quoted_stack():
    from nf_metro.parser.directives import _parse_output_overrides

    overrides = _parse_output_overrides('theme="seqera, london-tube" mode=light')
    assert overrides == {"theme": "seqera, london-tube", "mode": "light"}


def test_per_output_theme_rejects_an_unknown_name():
    from nf_metro.parser.directives import _parse_output_overrides

    with pytest.warns(UserWarning, match="bad value for theme"):
        assert _parse_output_overrides("theme=nosuchtheme") == {}


def test_playground_lists_the_paired_built_in_themes():
    app = (_ROOT / "website" / "public" / "playground" / "app.js").read_text()
    keys = re.search(r"const THEME_KEYS = \[(.*?)\];", app).group(1)
    assert [k.strip().strip('"') for k in keys.split(",")] == list(PAIRED_THEMES)
    page = (_ROOT / "website" / "src" / "pages" / "playground.astro").read_text()
    picker = re.search(r'<select id="opt-theme">(.*?)</select>', page, re.S).group(1)
    assert re.findall(r'<option value="([^"]+)"', picker) == list(PAIRED_THEMES)
