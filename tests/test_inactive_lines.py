"""Tests for the --inactive-lines / inactive_line_ids muting feature."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from nf_metro import RenderConfig, UnknownInactiveLineError, render_string
from nf_metro.api import prepare_graph
from nf_metro.errors import NfMetroError
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.constants import (
    FALLBACK_LINE_COLOR,
    ICON_BANNER_FILL,
    ICON_BANNER_TEXT_COLOR,
    ICON_BANNER_TEXT_COLOR_MUTED,
    effective_line_color,
    station_is_muted,
)
from nf_metro.render.plan import FrozenRecord, freeze_render_value
from nf_metro.render.svg import _muted_line_theme, render_svg
from nf_metro.themes import NFCORE_DARK_THEME, resolve_theme

MUTED = NFCORE_DARK_THEME.muted_line_color

# Two lines, three stations: x touches only line a, z only line b, y both.
TWO_LINE_MAP = (
    "%%metro line: a | Line A | #ff0000\n"
    "%%metro line: b | Line B | #0000ff\n"
    "graph LR\n"
    "    x[X] -->|a| y[Y]\n"
    "    y -->|b| z[Z]\n"
)


def _svg(src, **kwargs):
    return render_string(src, **kwargs)


def _rects_by_station(svg):
    """Map station id -> set of stroke colours on its ``rect`` glyphs."""
    root = ET.fromstring(svg)
    out: dict[str, set[str]] = {}
    for el in root.iter():
        if not el.tag.endswith("rect"):
            continue
        sid = el.get("data-station-id")
        if sid is not None:
            out.setdefault(sid, set()).add(el.get("stroke"))
    return out


def _label_fill_by_station(svg):
    """Map station id -> fill colour of its name label text."""
    root = ET.fromstring(svg)
    out: dict[str, str] = {}
    for el in root.iter():
        if not el.tag.endswith("text"):
            continue
        sid = el.get("data-station-id")
        cls = el.get("class") or ""
        if sid is not None and "label" in cls:
            out[sid] = el.get("fill")
    return out


def _icon_label_fill(svg, station_id, label):
    """Fill of the ``label`` text drawn inside ``station_id``'s terminus icon."""
    root = ET.fromstring(svg)
    for group in root.iter():
        if group.get("data-node-id") != station_id:
            continue
        for el in group.iter():
            if el.tag.endswith("text") and el.text == label:
                return el.get("fill")
    return None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def test_muted_line_color_defaults_to_fallback_grey():
    assert NFCORE_DARK_THEME.muted_line_color == FALLBACK_LINE_COLOR


def test_effective_line_color_mutes_inactive_only():
    graph = parse_metro_mermaid(TWO_LINE_MAP)
    a = graph.lines["a"]
    b = graph.lines["b"]
    inactive = frozenset({"a"})
    assert effective_line_color(a, NFCORE_DARK_THEME, inactive) == MUTED
    assert effective_line_color(b, NFCORE_DARK_THEME, inactive) == b.color
    assert (
        effective_line_color(None, NFCORE_DARK_THEME, inactive) == FALLBACK_LINE_COLOR
    )


def test_station_is_muted_guards_zero_line_station():
    graph = parse_metro_mermaid(TWO_LINE_MAP)
    # y touches both lines; x only a; a station with no edges is vacuously empty.
    assert station_is_muted(graph, "x", frozenset({"a"})) is True
    assert station_is_muted(graph, "y", frozenset({"a"})) is False
    assert station_is_muted(graph, "z", frozenset({"a"})) is False
    assert station_is_muted(graph, "x", frozenset({"a", "b"})) is True
    # An isolated / unknown station has no touching lines and is never muted.
    assert station_is_muted(graph, "__nonexistent__", frozenset({"a"})) is False


# ---------------------------------------------------------------------------
# Edge / chevron / legend strokes
# ---------------------------------------------------------------------------


def test_inactive_line_edge_and_legend_muted_active_line_kept():
    svg = _svg(TWO_LINE_MAP, config=RenderConfig(inactive_line_ids=frozenset({"a"})))
    # The active line keeps its own colour somewhere in the render.
    assert "#0000ff" in svg
    # The inactive line's own colour never appears as a stroke.
    assert 'stroke="#ff0000"' not in svg
    # The muted grey does appear (edge + legend swatch for line a).
    assert f'stroke="{MUTED}"' in svg


def test_inactive_chevron_muted():
    src = TWO_LINE_MAP + "%%metro directional: true\n"
    svg = _svg(src, config=RenderConfig(inactive_line_ids=frozenset({"a"})))
    root = ET.fromstring(svg)
    a_chevron_strokes = set()
    for el in root.iter():
        cls = el.get("class") or ""
        if "metro-direction-a" in cls:
            a_chevron_strokes.add(el.get("stroke"))
    assert a_chevron_strokes == {MUTED}


# ---------------------------------------------------------------------------
# Station / label muting
# ---------------------------------------------------------------------------


def test_station_touched_by_active_line_not_muted():
    svg = _svg(TWO_LINE_MAP, config=RenderConfig(inactive_line_ids=frozenset({"a"})))
    rects = _rects_by_station(svg)
    labels = _label_fill_by_station(svg)
    # x is touched only by the inactive line -> muted stroke + label.
    assert rects["x"] == {MUTED}
    assert labels["x"] == MUTED
    # y and z each touch an active line -> full-strength stroke + label.
    assert MUTED not in rects["y"]
    assert MUTED not in rects["z"]
    assert labels["y"] == NFCORE_DARK_THEME.label_color
    assert labels["z"] == NFCORE_DARK_THEME.label_color


def test_muted_station_keeps_fill():
    svg = _svg(TWO_LINE_MAP, config=RenderConfig(inactive_line_ids=frozenset({"a"})))
    root = ET.fromstring(svg)
    for el in root.iter():
        if el.tag.endswith("rect") and el.get("data-station-id") == "x":
            # Fill is the theme station fill, not the muted grey.
            assert el.get("fill") == NFCORE_DARK_THEME.station_fill


TERMINUS_MAP = (
    "%%metro line: a | Line A | #ff0000 | solid | inactive\n"
    "%%metro line: b | Line B | #0000ff\n"
    "%%metro file: a_out | SF\n"
    "%%metro file: b_out | BAM\n"
    "graph LR\n"
    "    x[X] -->|a| a_out[ ]\n"
    "    x -->|b| b_out[ ]\n"
)


def test_inactive_only_terminus_icon_label_muted():
    svg = _svg(TERMINUS_MAP)
    active = _icon_label_fill(svg, "b_out", "BAM")
    assert _icon_label_fill(svg, "a_out", "SF") == MUTED
    # A shared constant on both sides of the pair would let one wrong value
    # satisfy both, so the active fill is pinned to a literal.
    assert active == "#000000"
    assert active != MUTED


BANNER_MAP = (
    "%%metro line: a | Line A | #ff0000 | solid | inactive\n"
    "%%metro line: b | Line B | #0000ff\n"
    "%%metro file: a_out | BAM |  | banner\n"
    "%%metro file: b_out | SAM |  | banner\n"
    "graph LR\n"
    "    x[X] -->|a| a_out[ ]\n"
    "    x -->|b| b_out[ ]\n"
)


def _icon_banner(svg, station_id):
    """``(band fill, band text fill)`` of ``station_id``'s banner terminus icon.

    The banner band is the icon group's only ``rect`` (the document body is a
    path), and with no caption its only ``text`` is the banner label.
    """
    root = ET.fromstring(svg)
    for group in root.iter():
        if group.get("data-station-id") != station_id:
            continue
        band_fill = text_fill = None
        for el in group.iter():
            if el.tag.endswith("rect") and el.get("stroke") == "none":
                band_fill = el.get("fill")
            elif el.tag.endswith("text") and (el.text or "").strip():
                text_fill = el.get("fill")
        if band_fill is not None:
            return band_fill, text_fill
    return None, None


def test_inactive_only_banner_band_muted_as_a_unit():
    svg = _svg(BANNER_MAP)
    # The icon touched only by the inactive line mutes both band fill and text,
    # as one unit -- grey text on an unmuted black band would read worse than
    # the bug it replaces.
    muted_fill, muted_text = _icon_banner(svg, "a_out")
    assert muted_fill == MUTED
    assert muted_text == ICON_BANNER_TEXT_COLOR_MUTED
    assert muted_fill != ICON_BANNER_FILL
    assert muted_text != ICON_BANNER_TEXT_COLOR
    # The icon touched by the active line keeps the full-strength banner.
    active_fill, active_text = _icon_banner(svg, "b_out")
    assert active_fill == ICON_BANNER_FILL
    assert active_text == ICON_BANNER_TEXT_COLOR


RAIL_MARKER_FILL = (
    Path(__file__).parent / "fixtures" / "rail_marker_fill.mmd"
).read_text()

# The marker's declared interior tint; the interchange interior must override it
# when the station is muted, so a shared muted-grey wouldn't falsely satisfy the
# assertions and the declared colour is pinned to a literal.
RAIL_MARKER_DECLARED_FILL = "#1f4e79"


def _interchange_interior_colors(svg, station_id):
    """Interior link-bar stroke and knob-core fills of a rail interchange.

    The interior link bar carries ``nf-metro-rail-connector-interior`` and the
    knob cores ``nf-metro-rail-knob`` (distinct from the ``-outline`` casing
    layers), both tagged with the interchange's station id.
    """
    root = ET.fromstring(svg)
    bar_strokes: set[str] = set()
    knob_fills: set[str] = set()
    for el in root.iter():
        if el.get("data-station-id") != station_id:
            continue
        cls = (el.get("class") or "").split()
        if "nf-metro-rail-connector-interior" in cls:
            bar_strokes.add(el.get("stroke"))
        elif "nf-metro-rail-knob" in cls:
            knob_fills.add(el.get("fill"))
    return bar_strokes, knob_fills


def test_muted_rail_interchange_interior_greys_over_marker_fill():
    graph = prepare_graph(RAIL_MARKER_FILL)
    theme = resolve_theme(None, graph)
    muted = theme.muted_line_color
    svg = render_svg(graph, theme, inactive_line_ids=frozenset({"line_a", "line_b"}))
    bar_strokes, knob_fills = _interchange_interior_colors(svg, "interchange")
    assert bar_strokes == {muted}
    assert knob_fills == {muted}
    assert RAIL_MARKER_DECLARED_FILL not in bar_strokes
    assert RAIL_MARKER_DECLARED_FILL not in knob_fills


def test_active_rail_interchange_interior_keeps_marker_fill():
    graph = prepare_graph(RAIL_MARKER_FILL)
    theme = resolve_theme(None, graph)
    svg = render_svg(graph, theme)
    bar_strokes, knob_fills = _interchange_interior_colors(svg, "interchange")
    # With no muting the declared marker tint is retained (the exemption stands).
    assert bar_strokes == {RAIL_MARKER_DECLARED_FILL}
    assert knob_fills == {RAIL_MARKER_DECLARED_FILL}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_unknown_inactive_line_raises():
    with pytest.raises(UnknownInactiveLineError) as exc:
        _svg(TWO_LINE_MAP, config=RenderConfig(inactive_line_ids=frozenset({"nope"})))
    assert "nope" in str(exc.value)
    assert isinstance(exc.value, (NfMetroError, ValueError))


def test_unknown_inactive_line_raises_before_html_render():
    # The unknown-ID check must fire even for --format html, which is otherwise
    # not muted, so it runs ahead of the html early-return in the pipeline.
    with pytest.raises(UnknownInactiveLineError):
        _svg(
            TWO_LINE_MAP,
            config=RenderConfig(
                output_format="html", inactive_line_ids=frozenset({"nope"})
            ),
        )


# ---------------------------------------------------------------------------
# Additive: empty / default is inert
# ---------------------------------------------------------------------------


def test_default_none_matches_empty_when_no_declared_inactive():
    baseline = _svg(TWO_LINE_MAP)
    forced_active = _svg(
        TWO_LINE_MAP, config=RenderConfig(inactive_line_ids=frozenset())
    )
    assert baseline == forced_active


def test_example_render_byte_identical_with_empty_override():
    src = (
        "%%metro title: Example\n"
        "%%metro line: main | Main | #ff6600 | solid\n"
        "%%metro line: alt | Alt | #3366cc\n"
        "graph LR\n"
        "    subgraph s1 [First]\n"
        "        a[Alpha] -->|main| b[Beta]\n"
        "        a -->|alt| c[Gamma]\n"
        "    end\n"
        "    b -->|main| d[Delta]\n"
        "    c -->|alt| d\n"
    )
    baseline = _svg(src)
    empty = _svg(src, config=RenderConfig(inactive_line_ids=frozenset()))
    assert baseline == empty


# ---------------------------------------------------------------------------
# .mmd directive + precedence
# ---------------------------------------------------------------------------


def test_line_directive_fifth_field_sets_default_inactive():
    graph = parse_metro_mermaid(
        "%%metro line: a | Line A | #ff0000 | solid | inactive\n"
        "%%metro line: b | Line B | #0000ff\n"
        "graph LR\n"
        "    x[X] -->|a| y[Y]\n"
        "    y -->|b| z[Z]\n"
    )
    assert graph.lines["a"].default_inactive is True
    assert graph.lines["b"].default_inactive is False


DECLARED_INACTIVE_MAP = (
    "%%metro line: a | Line A | #ff0000 | solid | inactive\n"
    "%%metro line: b | Line B | #0000ff\n"
    "graph LR\n"
    "    x[X] -->|a| y[Y]\n"
    "    y -->|b| z[Z]\n"
)


def test_declared_inactive_muted_without_cli_override():
    svg = _svg(DECLARED_INACTIVE_MAP)  # no override -> use map's declared set
    assert 'stroke="#ff0000"' not in svg
    assert f'stroke="{MUTED}"' in svg
    assert _rects_by_station(svg)["x"] == {MUTED}


def test_cli_override_replaces_declared_set():
    # Map declares 'a' inactive; overriding with 'b' must mute only 'b' and
    # restore 'a' to full colour (full-replace, not union).
    svg = _svg(
        DECLARED_INACTIVE_MAP, config=RenderConfig(inactive_line_ids=frozenset({"b"}))
    )
    assert 'stroke="#ff0000"' in svg  # line a is active again
    assert 'stroke="#0000ff"' not in svg  # line b muted
    rects = _rects_by_station(svg)
    assert MUTED not in rects["x"]  # x (line a) full strength
    assert rects["z"] == {MUTED}  # z (line b) muted


def test_cli_empty_override_forces_all_active():
    svg = _svg(
        DECLARED_INACTIVE_MAP, config=RenderConfig(inactive_line_ids=frozenset())
    )
    assert 'stroke="#ff0000"' in svg
    assert 'stroke="#0000ff"' in svg
    rects = _rects_by_station(svg)
    assert MUTED not in rects["x"]
    assert MUTED not in rects["z"]


def test_render_string_flat_kwarg_overrides():
    svg = _svg(TWO_LINE_MAP, inactive_line_ids=frozenset({"a"}))
    assert _rects_by_station(svg)["x"] == {MUTED}


# ---------------------------------------------------------------------------
# Chrome-CSS cascade
# ---------------------------------------------------------------------------

# An inactive and an active line, each carrying a plain station, a marked station
# and a captioned file terminus.  That reaches every element that takes a muted
# presentation attribute (name labels, a marker outline, a terminus caption) and
# gives each of them a full-strength counterpart.
CASCADE_MAP = (
    "%%metro line: a | Line A | #ff0000 | solid | inactive\n"
    "%%metro line: b | Line B | #00ff00\n"
    "%%metro marker: mid | square, solid\n"
    "%%metro marker: keep | square, solid\n"
    "%%metro file: out | SF | Sizes\n"
    "%%metro file: kept | BAM | Alignments\n"
    "graph LR\n"
    "    x[X] -->|a| mid[Mid]\n"
    "    mid -->|a| out[ ]\n"
    "    y[Y] -->|b| keep[Keep]\n"
    "    keep -->|b| kept[ ]\n"
)

_CSS_RULE = re.compile(r"([^{}]+)\{([^}]*)\}")


def _chrome_rules(svg):
    """Parse the injected chrome stylesheet into ordered declarations.

    Each entry is ``(class names the selector requires, property, value)``.
    """
    blocks = re.findall(r"<style>(.*?)</style>", svg, re.S)
    style = next(b for b in blocks if "--nfm-map-" in b)
    rules = []
    for selector, body in _CSS_RULE.findall(style):
        classes = frozenset(selector.strip().lstrip(".").split("."))
        for decl in body.split(";"):
            if ":" in decl:
                prop, value = decl.split(":", 1)
                rules.append((classes, prop.strip(), value.strip()))
    return rules


def _winning_value(rules, classes, prop):
    """Value of the chrome declaration that wins *prop* on an element in *classes*.

    Every chrome selector is a class chain in a single stylesheet, so the winner
    is the longest chain that matches, and the last declared one at that length.
    """
    winner = None
    for selector, rule_prop, value in rules:
        if rule_prop == prop and selector <= classes:
            if winner is None or len(selector) >= len(winner[0]):
                winner = (selector, value)
    return winner[1] if winner else None


def _mode_colors(value):
    """The ``(light, dark)`` colours a chrome declaration resolves to."""
    var_ref = re.fullmatch(r"var\(--[\w-]+,\s*(.*)\)", value)
    fallback = var_ref.group(1) if var_ref else value
    pair = re.fullmatch(r"light-dark\((.*?),\s*(.*)\)", fallback)
    if pair:
        return pair.group(1).strip(), pair.group(2).strip()
    return fallback, fallback


# The muted rules are built from the theme, and a single-mode theme emits a bare
# colour where a light/dark pair emits ``light-dark()``, so both shapes are worth
# covering.
@pytest.mark.parametrize("brand", ["nfcore", "seqera", "light"])
def test_chrome_css_and_presentation_attribute_agree_on_muting(brand):
    # A presentation attribute loses to every author rule, so what a browser
    # paints is the winning rule, not the attribute.  The two must therefore
    # agree wherever either says muted: a rule that repaints a muted attribute
    # drops the mute, and a rule that greys a full-strength attribute invents
    # one.  Asserting the attribute alone would pass in both cases.
    svg = _svg(CASCADE_MAP, theme=brand)
    rules = _chrome_rules(svg)
    root = ET.fromstring(svg)
    covered = set()
    for el in root.iter():
        classes = frozenset((el.get("class") or "").split())
        for prop in ("fill", "stroke"):
            attr = el.get(prop)
            if attr is None:
                continue
            winner = _winning_value(rules, classes, prop)
            css_mutes = winner is not None and _mode_colors(winner) == (MUTED, MUTED)
            if attr != MUTED and not css_mutes:
                continue
            where = f"{prop} on <{el.tag}> {el.text!r} class={sorted(classes)}"
            assert attr == MUTED, (
                f"{where} is greyed by the chrome rule {winner!r} but renders at {attr}"
            )
            assert winner is None or css_mutes, (
                f"{where} is repainted by the chrome rule {winner!r}"
            )
            covered.add((prop, el.text))
    # Every element the muted rules exist for must have been reached, or the map
    # has drifted away from what this guards: a station name label and a
    # terminus caption for the fill rule, a marker outline for the stroke one.
    assert ("fill", "Mid") in covered
    assert ("fill", "Sizes") in covered
    assert {prop for prop, _ in covered} == {"fill", "stroke"}


# ---------------------------------------------------------------------------
# render_svg direct entry point (playground / live server path)
# ---------------------------------------------------------------------------

# render_svg is the entry point the browser playground and the live-progress
# server call directly, bypassing RenderConfig/render_graph_result. It must
# resolve the map's declared-inactive set itself, or those surfaces silently
# render every line active.


def _render_svg(src, **kwargs):
    graph = prepare_graph(src)
    theme = resolve_theme(None, graph)
    return graph, theme, render_svg(graph, theme, **kwargs)


def test_render_svg_mutes_declared_inactive_by_default():
    graph, theme, svg = _render_svg(DECLARED_INACTIVE_MAP)
    muted = theme.muted_line_color
    assert 'stroke="#ff0000"' not in svg
    assert f'stroke="{muted}"' in svg
    assert _rects_by_station(svg)["x"] == {muted}


def test_render_svg_explicit_override_replaces_declared_set():
    graph = prepare_graph(DECLARED_INACTIVE_MAP)
    theme = resolve_theme(None, graph)
    svg = render_svg(graph, theme, inactive_line_ids=frozenset({"b"}))
    assert 'stroke="#ff0000"' in svg  # line a active
    assert 'stroke="#0000ff"' not in svg  # line b muted
    rects = _rects_by_station(svg)
    assert theme.muted_line_color not in rects["x"]
    assert rects["z"] == {theme.muted_line_color}


def test_render_svg_empty_override_forces_all_active():
    graph = prepare_graph(DECLARED_INACTIVE_MAP)
    theme = resolve_theme(None, graph)
    svg = render_svg(graph, theme, inactive_line_ids=frozenset())
    assert 'stroke="#ff0000"' in svg
    assert 'stroke="#0000ff"' in svg
    rects = _rects_by_station(svg)
    assert theme.muted_line_color not in rects["x"]
    assert theme.muted_line_color not in rects["z"]


def test_render_svg_unknown_inactive_line_raises():
    graph = prepare_graph(DECLARED_INACTIVE_MAP)
    theme = resolve_theme(None, graph)
    with pytest.raises(UnknownInactiveLineError) as exc:
        render_svg(graph, theme, inactive_line_ids=frozenset({"nope"}))
    assert "nope" in str(exc.value)


def test_muted_theme_overrides_every_field_on_a_frozen_theme():
    frozen = freeze_render_value(NFCORE_DARK_THEME)
    assert isinstance(frozen, FrozenRecord)
    muted = _muted_line_theme(frozen)
    assert muted.station_stroke == MUTED
    assert muted.marker_stroke == MUTED
    assert muted.terminus_stroke == MUTED
    assert muted.terminus_font_color == MUTED
    assert muted.label_color == MUTED
    assert muted.station_fill == NFCORE_DARK_THEME.station_fill
