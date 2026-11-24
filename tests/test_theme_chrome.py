"""Theme colours in the drawing and the chrome stylesheet."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import replace

from theme_helpers import MMD, RNASEQ, ROOT, classed, station_glyphs, write

from nf_metro.api import prepare_graph, render_string
from nf_metro.render import render_svg
from nf_metro.render.style import CHROME, colors_adapt
from nf_metro.themes import DEFAULT_DARK_THEME, THEMES, resolve_stack

_TUBE = resolve_stack("london-tube")
_MAP = RNASEQ.read_text()

_MMD_TWO_STATIONS = MMD + "    d -->|alt| e\n"


def _all_inactive(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"%%metro line: (\w+)", text))


def _css(svg: str) -> str:
    return re.search(r"<style>(.*?)</style>", svg, re.S).group(1)


def test_every_applicable_chrome_colour_reaches_the_stylesheet():
    svg = render_string(MMD, theme="london-tube")
    theme = THEMES["london-tube"]
    for name, chrome in CHROME.items():
        baked = getattr(theme, name)
        expected = (
            bool(baked)
            and baked != "none"
            and (not chrome.adaptive_only or colors_adapt(theme, name))
        )
        assert (chrome.css_var in svg) == expected, chrome.css_var


def test_capsules_switch_colour_with_the_mode():
    css = _css(render_string(_MAP, theme="london-tube", source_dir=str(RNASEQ.parent)))
    assert "stroke: var(--nfm-map-station-stroke, light-dark(#000000, #ffffff))" in css
    assert "fill: var(--nfm-map-station-fill, light-dark(#ffffff, #10141c))" in css


def test_station_colours_fixed_in_both_modes_stay_attributes():
    svg = render_string(_MAP, source_dir=str(RNASEQ.parent))
    assert "capsule" not in svg
    assert "nf-metro-ring" not in svg
    assert "nf-metro-terminus-bar" not in svg


def test_ring_line_ends_follow_the_mode_under_pill_interchanges(tmp_path):
    theme = write(
        tmp_path,
        "t.yaml",
        "name: t\nextends: london-tube\ninterchange_glyph: pill\n"
        "terminus_glyph: ring\n",
    )
    for mode in ("light", "dark"):
        css = _css(
            render_string(
                _MAP,
                theme=str(theme),
                mode=mode,
                source_dir=str(RNASEQ.parent),
                inactive_line_ids=_all_inactive(_MAP),
            )
        )
        assert (
            ".nf-metro-ring { stroke: var(--nfm-map-station-stroke, "
            "light-dark(#000000, #ffffff))"
        ) in css
        assert ".nf-metro-ring-hole { fill: var(--nfm-map-station-fill," in css
        assert ".nf-metro-ring.nf-metro-muted { stroke:" in css
        assert ".nf-metro-ring-link.nf-metro-muted { fill:" in css


def test_new_chrome_colours_follow_the_viewer():
    svg = render_string(MMD, theme="london-tube")
    assert "stroke: var(--nfm-map-legend-stroke, light-dark(#000000, #ffffff))" in svg


def test_a_caption_colour_that_differs_by_mode_follows_it(tmp_path):
    theme = write(
        tmp_path,
        "cap.yaml",
        "name: cap\n"
        "light:\n  caption_color: '#111111'\n"
        "dark:\n  caption_color: '#eeeeee'\n",
    )
    text = "%%metro caption: hello\n" + MMD
    svg = render_string(text, theme=str(theme))
    assert "fill: var(--nfm-map-caption-color, light-dark(#111111, #eeeeee))" in svg
    assert "nf-metro-caption" not in render_string(text)


# ---------------------------------------------------------------------------
# Inactive lines
# ---------------------------------------------------------------------------


def test_inactive_lines_take_the_theme_muted_grey():
    svg = render_string(
        MMD, theme="london-tube", mode="light", inactive_line_ids=frozenset({"alt"})
    )
    assert 'stroke="#b3b3b3"' in svg


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
    assert "nf-metro-muted-fill" in station_glyphs(root)["d"].get("class")


def test_default_inactive_render_adds_no_switching_classes():
    svg = render_string(MMD, theme="default", inactive_line_ids=frozenset({"alt"}))
    assert "nf-metro-muted-stroke" not in svg
    assert "nf-metro-muted-fill" not in svg


def test_a_muted_bar_terminus_greys_in_a_baked_render():
    svg = render_string(
        _MAP,
        theme="london-tube",
        mode="dark",
        chrome_css=False,
        source_dir=str(RNASEQ.parent),
        inactive_line_ids=_all_inactive(_MAP),
    )
    bar = station_glyphs(ET.fromstring(svg))["fastq_in"]
    assert "nf-metro-terminus-bar" in bar.get("class")
    assert bar.get("fill") == _TUBE.dark.muted_line_color


# ---------------------------------------------------------------------------
# Fields that reach the drawing
# ---------------------------------------------------------------------------


def test_visual_fields_reach_the_drawing(tmp_path):
    theme = write(
        tmp_path,
        "visual.yaml",
        "name: visual\n"
        "title_font_weight: normal\n"
        "watermark_color: '#123456'\n"
        "section_badge_fill: '#aa0000'\n"
        "section_badge_text_color: '#00aa00'\n"
        "section_badge_font_size: 9\n"
        "dashed_dasharray: '3,3'\n"
        "line_cap: butt\n"
        "canvas_padding: 90\n",
    )
    text = "%%metro title: T\n" + RNASEQ.read_text()
    default = render_string(text, source_dir=str(RNASEQ.parent))
    svg = render_string(text, theme=str(theme), source_dir=str(RNASEQ.parent))
    root = ET.fromstring(svg)
    title = classed(root, "text", "nf-metro-title")[0]
    assert title.get("font-weight") == "normal"
    assert 'fill="#123456"' in svg
    badge = classed(root, "circle", "nf-metro-section-num-circle")[0]
    assert badge.get("fill") == "#aa0000"
    assert 'font-size="9.0"' in svg and 'fill="#00aa00"' in svg
    assert 'stroke-linecap="butt"' in svg and 'stroke-linecap="round"' in default
    width = float(root.get("width"))
    assert width > float(ET.fromstring(default).get("width"))


def test_dash_patterns_come_from_the_theme(tmp_path):
    theme = write(tmp_path, "dash.yaml", "name: dash\ndashed_dasharray: '3,3'\n")
    text = (
        "%%metro line: a | A | #ff0000 | dashed\n"
        "graph LR\n    x -->|a| y\n    y -->|a| z\n"
    )
    assert 'stroke-dasharray="3,3"' in render_string(text, theme=str(theme))
    assert 'stroke-dasharray="8,4"' in render_string(text)


def test_animation_balls_take_the_theme_opacity(tmp_path):
    theme = write(tmp_path, "ball.yaml", "name: ball\nanimation_ball_opacity: 0.5\n")
    text = "%%metro animate: true\n" + MMD
    svg = render_string(text, theme=str(theme))
    assert 'opacity="0.5"' in svg and 'opacity="0.9"' not in svg


def test_file_icon_banners_take_the_theme_colours(tmp_path):
    theme = write(
        tmp_path,
        "banner.yaml",
        "name: banner\n"
        "icon_banner_fill: '#0000aa'\n"
        "icon_banner_text_color: '#ffff00'\n",
    )
    path = ROOT / "examples" / "file_icons.mmd"
    svg = render_string(path.read_text(), theme=str(theme), source_dir=str(path.parent))
    assert 'fill="#0000aa"' in svg and 'fill="#ffff00"' in svg


def test_free_text_reaches_svg_attributes_escaped():
    theme = replace(
        DEFAULT_DARK_THEME,
        label_font_family='x"/><script>alert(1)</script>',
        dashed_dasharray='4"/><script>',
        title_font_weight="bold</style><script>",
    )
    text = "%%metro title: T\n%%metro line: a | A | #ff0000 | dashed\n" + MMD
    svg = render_svg(prepare_graph(text), theme)
    assert "<script" not in svg
    ET.fromstring(svg)
