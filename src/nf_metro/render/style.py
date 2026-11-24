"""The theme schema: every visual setting a theme can carry.

:class:`Theme` is the single definition of what a theme is. Each field's
metadata records what it means (``doc``), which reference group it belongs to
(``group``), whether its value is a colour (``color``), its allowed values
(``choices``), and, for the chrome colours a host page may recolour, which SVG
class and property it paints and the ``--nfm-map-*`` custom property that
overrides it (``chrome``, ``css_var``). Theme files are validated against this
schema, the light/dark chrome CSS is generated from it, and the field reference
in ``docs/theming.mdx`` is checked against it.
"""

from __future__ import annotations

__all__ = [
    "GLYPH_PILL",
    "GLYPH_RING",
    "GLYPH_TICK",
    "RING_CHAIN",
    "RING_GROW",
    "TERMINUS_BAR",
    "TERMINUS_NUB",
    "ChromeBinding",
    "ChromeTarget",
    "EMIT_CONDITIONS",
    "Theme",
    "adaptive_value",
    "authored_fields",
    "chrome_bindings",
    "color_pair",
    "MUTED_FILL_CLASS",
    "MUTED_STROKE_CLASS",
    "binding_pair",
    "is_adaptive",
    "muted_adapts",
    "muted_paint_class",
    "draws_rings",
    "opaque_or_white",
    "target_applies",
    "theme_color",
]

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Any, NamedTuple

from nf_metro.layout.constants import (
    ICON_HALF_HEIGHT,
    STATION_RADIUS_APPROX,
    TERMINUS_WIDTH,
)
from nf_metro.render.constants import (
    FALLBACK_LINE_COLOR,
    LEGEND_BORDER_RADIUS,
    SECTION_BOX_RADIUS,
    SECTION_STROKE_WIDTH,
)
from nf_metro.render.ns import ns as _ns

_MISSING: Any = dataclasses.MISSING

GLYPH_PILL = "pill"
GLYPH_TICK = "tick"
GLYPH_RING = "ring"
RING_CHAIN = "chain"
RING_GROW = "grow"
TERMINUS_NUB = "nub"
TERMINUS_BAR = "bar"


class ChromeTarget(NamedTuple):
    """One CSS property a chrome colour paints on one element class.

    ``when`` names the condition in :data:`EMIT_CONDITIONS` under which the
    element is drawn at all (empty: always); ``muted`` gives the class a rule
    that greys it when every line touching it is inactive.
    """

    cls: str
    prop: str
    when: str = ""
    muted: bool = False


def _field(
    default: Any = _MISSING,  # noqa: ANN401
    *,
    doc: str,
    group: str,
    color: bool = False,
    choices: tuple[str, ...] = (),
    chrome: tuple[ChromeTarget | tuple[str, str], ...] = (),
    css_var: str = "",
    fallback: str = "",
    opaque: bool = False,
) -> Any:  # noqa: ANN401
    return dataclasses.field(
        default=default,
        metadata={
            "doc": doc,
            "group": group,
            "color": color,
            "choices": choices,
            "chrome": tuple(ChromeTarget(*target) for target in chrome),
            "css_var": css_var,
            "fallback": fallback,
            "opaque": opaque,
        },
    )


def _derived(default: Any) -> Any:  # noqa: ANN401
    return dataclasses.field(default=default, metadata={"derived": True})


@dataclass(kw_only=True, frozen=True)
class Theme:
    """Visual theme for a metro map, resolved for one display mode.

    Fields without a default are set by the base theme every stack starts
    from (``nfcore``). The chrome fields are declared in the order their CSS
    rules are emitted, which keeps the generated stylesheet stable.
    """

    # The palette this theme is resolved for, set by the resolver.
    mode: str = _derived("dark")
    # ``(field, light value, dark value)`` for every colour field when the
    # theme has both palettes; empty for a single-palette theme.
    mode_colors: tuple[tuple[str, str, str], ...] = _derived(())

    background_color: str = _field(
        doc="Canvas fill. `none` leaves the canvas transparent.",
        group="Canvas and text",
        color=True,
        chrome=(("nf-metro-bg", "fill"),),
        css_var="--nfm-map-bg",
    )
    title_color: str = _field(
        doc="Map title text.",
        group="Canvas and text",
        color=True,
        chrome=(("nf-metro-title", "fill"),),
        css_var="--nfm-map-title-color",
    )
    title_font_size: float = _field(doc="Map title size, px.", group="Canvas and text")
    label_color: str = _field(
        doc="Station label text.",
        group="Canvas and text",
        color=True,
        chrome=(ChromeTarget("nf-metro-station-label", "fill", muted=True),),
        css_var="--nfm-map-label-color",
    )
    label_font_family: str = _field(
        doc="CSS font stack for station labels, and for every other text element.",
        group="Canvas and text",
    )
    label_font_size: float = _field(
        doc="Station label size, px. Layout reserves label room from the map's "
        "own settings, so keep this near 13 and use the `font_scale` layout "
        "option to grow text.",
        group="Canvas and text",
    )
    label_font_weight: str = _field(
        doc="Station label weight (`bold`, `normal`, `600`, ...).",
        group="Canvas and text",
    )
    label_halo_width: float = _field(
        3.0,
        doc="Width of the knockout halo behind station labels, px. 0 disables it.",
        group="Canvas and text",
    )
    label_halo_color: str = _field(
        "",
        doc="Halo colour. Empty takes the background, or white on a transparent "
        "canvas.",
        group="Canvas and text",
        color=True,
    )

    section_fill: str = _field(
        doc="Section box fill. `transparent` draws an outline only.",
        group="Sections",
        color=True,
        chrome=(("nf-metro-section-box", "fill"),),
        css_var="--nfm-map-section-fill",
    )
    section_stroke: str = _field(
        doc="Section box outline.",
        group="Sections",
        color=True,
        chrome=(("nf-metro-section-box", "stroke"),),
        css_var="--nfm-map-section-stroke",
    )
    section_label_color: str = _field(
        doc="Section titles and group captions.",
        group="Sections",
        color=True,
        chrome=(
            ("nf-metro-section-label", "fill"),
            ("nf-metro-group-label", "fill"),
            ("nf-metro-group-underline", "stroke"),
        ),
        css_var="--nfm-map-section-label-color",
    )
    section_label_font_size: float = _field(
        doc="Section title size, px.", group="Sections"
    )
    section_label_font_weight: str = _field(
        "bold", doc="Section title weight.", group="Sections"
    )
    section_label_uppercase: bool = _field(
        False, doc="Draw section titles in capitals.", group="Sections"
    )
    section_label_letter_spacing: float = _field(
        0.0, doc="Extra space between section title letters, px.", group="Sections"
    )
    section_stroke_width: float = _field(
        SECTION_STROKE_WIDTH, doc="Section box outline width, px.", group="Sections"
    )
    section_stroke_dasharray: str = _field(
        "",
        doc="SVG dash pattern for the section outline (`2 4`). Empty is solid.",
        group="Sections",
    )
    section_corner_radius: float = _field(
        SECTION_BOX_RADIUS, doc="Section box corner radius, px.", group="Sections"
    )
    section_badges: bool = _field(
        True,
        doc="Draw the numbered circle beside each section title. Off, titles "
        "start where the badge would be.",
        group="Sections",
    )

    legend_background: str = _field(
        doc="Legend box fill.",
        group="Legend",
        color=True,
        chrome=(("nf-metro-legend-bg", "fill"),),
        css_var="--nfm-map-legend-bg",
    )
    legend_stroke: str = _field(
        "",
        doc="Legend box outline. Empty draws none.",
        group="Legend",
        color=True,
        chrome=(("nf-metro-legend-bg", "stroke"),),
        css_var="--nfm-map-legend-stroke",
    )
    legend_text_color: str = _field(
        doc="Legend text.",
        group="Legend",
        color=True,
        chrome=(("nf-metro-legend-text", "fill"),),
        css_var="--nfm-map-legend-text-color",
    )
    legend_font_size: float = _field(doc="Legend text size, px.", group="Legend")
    legend_stroke_width: float = _field(
        0.0, doc="Legend outline width, px. 0 draws none.", group="Legend"
    )
    legend_corner_radius: float = _field(
        LEGEND_BORDER_RADIUS, doc="Legend box corner radius, px.", group="Legend"
    )

    marker_stroke: str = _field(
        "",
        doc="Outline of `%%metro marker:` glyphs and their legend swatches. "
        "Empty takes `station_stroke`.",
        group="Stations",
        color=True,
        chrome=(ChromeTarget("nf-metro-marker-stroke", "stroke", muted=True),),
        css_var="--nfm-map-marker-stroke",
        fallback="station_stroke",
    )
    marker_open_fill: str = _field(
        "",
        doc="Inside of `open` markers. Empty takes the background, or white on "
        "a transparent canvas.",
        group="Stations",
        color=True,
    )
    glyph_stroke: str = _field(
        "",
        doc="Outline of interchange rings and their links, the `bar` terminus, "
        "and station capsules, in colours that follow the viewer's light or dark "
        "mode. Empty takes `station_stroke`, and capsules keep it in both modes.",
        group="Stations",
        color=True,
        chrome=(
            ChromeTarget("nf-metro-ring", "stroke", "rings", muted=True),
            ChromeTarget("nf-metro-ring-link", "fill", "rings", muted=True),
            ChromeTarget("nf-metro-terminus-bar", "fill", "bar", muted=True),
            ChromeTarget("nf-metro-capsule", "stroke", "set", muted=True),
            ChromeTarget("nf-metro-capsule-outline", "stroke", "set", muted=True),
            ChromeTarget("nf-metro-capsule-outline", "fill", "set", muted=True),
        ),
        css_var="--nfm-map-glyph-stroke",
        fallback="station_stroke",
    )
    ring_fill: str = _field(
        "",
        doc="Inside of interchange rings and the strip bridging a chain of "
        "them. Empty takes the background, or white on a transparent canvas.",
        group="Stations",
        color=True,
        chrome=(
            ChromeTarget("nf-metro-ring-hole", "fill", "rings"),
            ChromeTarget("nf-metro-ring-bridge", "fill", "rings"),
        ),
        css_var="--nfm-map-ring-fill",
        fallback="background_color",
        opaque=True,
    )
    station_glyph: str = _field(
        "pill",
        doc="How a station on one line is drawn. `pill` draws a capsule. "
        "`tick` draws a tab in the line's colour on the label's side, "
        "and a bar across the line's end (a buffer stop) at a terminus.",
        group="Stations",
        choices=(GLYPH_PILL, GLYPH_TICK),
    )
    interchange_glyph: str = _field(
        "pill",
        doc="How a station on several lines is drawn. `pill` draws a capsule "
        "across them. `ring` draws one ring centred on the lines, or a chain "
        "of touching rings bridged by a strip when the lines are too wide for "
        "one.",
        group="Stations",
        choices=(GLYPH_PILL, GLYPH_RING),
    )
    station_fill: str = _field(
        doc="Station capsule fill.", group="Stations", color=True
    )
    station_stroke: str = _field(
        doc="Station capsule outline; also the default marker and file-icon outline.",
        group="Stations",
        color=True,
    )
    station_stroke_width: float = _field(
        doc="Station capsule outline width, px.", group="Stations"
    )
    capsule_fill: str = _field(
        "",
        doc="Inside of station capsules and interchanges, in colours that follow "
        "the viewer's light or dark mode. Empty takes `station_fill` in both "
        "modes.",
        group="Stations",
        color=True,
        chrome=(
            ChromeTarget("nf-metro-capsule", "fill", "set"),
            ChromeTarget("nf-metro-capsule-core", "stroke", "set"),
            ChromeTarget("nf-metro-capsule-core", "fill", "set"),
        ),
        css_var="--nfm-map-capsule-fill",
        fallback="station_fill",
    )
    station_radius: float = _field(
        STATION_RADIUS_APPROX, doc="Station capsule end radius, px.", group="Stations"
    )
    terminus_glyph: str = _field(
        TERMINUS_NUB,
        doc="How a line end shared by several lines is drawn. `nub` is a "
        "square-cornered box across the lines in the station colours; `bar` is "
        "a buffer-stop bar across the whole bundle, as thick as a tab and "
        "overhanging the outermost lines by a tab's length; `ring` draws the "
        "interchange glyph. A line end with one line follows `station_glyph`.",
        group="Stations",
        choices=(TERMINUS_NUB, TERMINUS_BAR, GLYPH_RING),
    )
    station_tick_width: float = _field(
        0.66,
        doc="Thickness of a `tick` tab along its line, and of a terminus bar, "
        "in multiples of `line_width`.",
        group="Stations",
    )
    station_tick_length: float = _field(
        0.66,
        doc="How far a `tick` tab reaches past the edge of its line, and how "
        "far a terminus bar overhangs each side, in multiples of `line_width`.",
        group="Stations",
    )
    ring_inner_diameter: float = _field(
        2.0,
        doc="Inside diameter of an interchange ring, in multiples of `line_width`.",
        group="Stations",
    )
    ring_stroke_width: float = _field(
        0.5,
        doc="Interchange ring outline width, in multiples of `line_width`.",
        group="Stations",
    )
    ring_bridge_width: float = _field(
        0.5,
        doc="Width of the strip bridging a chain of rings, in multiples of "
        "`line_width`.",
        group="Stations",
    )
    ring_max_lines: int = _field(
        3,
        doc="Most lines on one track a single ring of `ring_inner_diameter` "
        "covers; a wider bundle follows `ring_overflow`.",
        group="Stations",
    )
    ring_overflow: str = _field(
        RING_CHAIN,
        doc="What a bundle wider than `ring_max_lines` gets: `chain`, a bridged "
        "chain of rings, or `grow`, one ring whose inside widens to cover it.",
        group="Stations",
        choices=(RING_CHAIN, RING_GROW),
    )

    line_width: float = _field(doc="Route stroke width, px.", group="Lines")
    muted_line_color: str = _field(
        FALLBACK_LINE_COLOR,
        doc="Grey for inactive lines and the stations, labels and icons only "
        "they touch. A two-palette SVG switches it with the viewer's mode.",
        group="Lines",
        color=True,
    )
    bridge_glyph: bool = _field(
        True,
        doc="Break the lower line where two unrelated lines cross.",
        group="Lines",
    )
    directional_marker_size: float = _field(
        4.0, doc="Half-size of `--directional` chevrons, px.", group="Lines"
    )
    directional_marker_spacing: float = _field(
        64.0, doc="Distance between chevrons along a route, px.", group="Lines"
    )
    directional_marker_opacity: float = _field(
        0.75, doc="Chevron opacity, 0 to 1.", group="Lines"
    )
    directional_marker_color: str = _field(
        "",
        doc="Chevron colour. Empty takes the line's colour.",
        group="Lines",
        color=True,
    )

    terminus_width: float = _field(
        TERMINUS_WIDTH, doc="File icon width, px.", group="File icons"
    )
    terminus_height: float = _field(
        2 * ICON_HALF_HEIGHT, doc="File icon height, px.", group="File icons"
    )
    terminus_fold_size: float = _field(
        8.0, doc="Size of the folded corner, px.", group="File icons"
    )
    terminus_fill: str = _field(
        "",
        doc="Icon fill. Empty takes `station_fill`.",
        group="File icons",
        color=True,
        fallback="station_fill",
    )
    terminus_stroke: str = _field(
        "",
        doc="Icon outline. Empty takes `station_stroke`.",
        group="File icons",
        color=True,
        fallback="station_stroke",
    )
    terminus_stroke_width: float = _field(
        1.5, doc="Icon outline width, px.", group="File icons"
    )
    terminus_corner_radius: float = _field(
        2.0, doc="Icon corner radius, px.", group="File icons"
    )
    terminus_font_size: float = _field(
        7.5, doc="Size of the format label inside the icon, px.", group="File icons"
    )
    terminus_font_color: str = _field(
        "",
        doc="Format label colour. Empty takes the built-in dark ink.",
        group="File icons",
        color=True,
    )

    animation_ball_radius: float = _field(
        3.0, doc="Radius of `--animate` balls, px.", group="Animation"
    )
    animation_ball_color: str = _field(
        "#ffffff", doc="Ball fill.", group="Animation", color=True
    )
    animation_ball_stroke: str = _field(
        "", doc="Ball outline. Empty draws none.", group="Animation", color=True
    )
    animation_ball_stroke_width: float = _field(
        1.0, doc="Ball outline width, px.", group="Animation"
    )
    animation_balls_per_line: int = _field(
        1, doc="Balls travelling along each route.", group="Animation"
    )
    animation_speed: float = _field(
        80.0, doc="Ball speed, px per second.", group="Animation"
    )


_THEME_FIELDS: MappingProxyType[str, dataclasses.Field[Any]] = MappingProxyType(
    {f.name: f for f in dataclasses.fields(Theme)}
)


class ChromeBinding(NamedTuple):
    """One chrome colour: the field, its custom property, and what it paints.

    ``fallback`` names the field an empty value takes; ``opaque`` turns a
    transparent value into white.
    """

    field: str
    css_var: str
    targets: tuple[ChromeTarget, ...]
    fallback: str
    opaque: bool


def draws_rings(theme: object) -> bool:
    """Whether *theme* draws interchange rings, at interchanges or line ends."""
    return GLYPH_RING in (
        getattr(theme, "interchange_glyph"),
        getattr(theme, "terminus_glyph"),
    )


EMIT_CONDITIONS: MappingProxyType[str, Callable[[object], bool]] = MappingProxyType(
    {
        "rings": draws_rings,
        "bar": lambda theme: getattr(theme, "terminus_glyph") == TERMINUS_BAR,
    }
)


def target_applies(theme: object, binding: ChromeBinding, target: ChromeTarget) -> bool:
    """Whether *theme* draws the elements *target* paints.

    ``set`` targets apply only when the theme sets the colour itself, so a
    theme that leaves it empty keeps fixed colours there.
    """
    if not target.when:
        return True
    if target.when == "set":
        return bool(getattr(theme, binding.field))
    return EMIT_CONDITIONS[target.when](theme)


@lru_cache(maxsize=1)
def authored_fields() -> MappingProxyType[str, dataclasses.Field[Any]]:
    """The fields a theme file may set, in declaration order."""
    return MappingProxyType(
        {
            f.name: f
            for f in dataclasses.fields(Theme)
            if not f.metadata.get("derived", False)
        }
    )


@lru_cache(maxsize=1)
def chrome_bindings() -> tuple[ChromeBinding, ...]:
    """Every chrome colour, in the order its CSS rules are emitted."""
    return tuple(
        ChromeBinding(
            f.name,
            f.metadata["css_var"],
            f.metadata["chrome"],
            f.metadata["fallback"],
            f.metadata["opaque"],
        )
        for f in dataclasses.fields(Theme)
        if f.metadata.get("chrome")
    )


def opaque_or_white(color: str) -> str:
    """*color*, or white when it is empty or ``none``."""
    return color if color and color != "none" else "#ffffff"


def adaptive_value(light: str, dark: str) -> str:
    """One CSS value for a light/dark pair: ``light-dark()`` unless they match."""
    if light == dark:
        return light
    return f"light-dark({light}, {dark})"


def is_adaptive(theme: object) -> bool:
    """Whether *theme* carries both palettes, so chrome can follow the viewer."""
    return bool(getattr(theme, "mode_colors"))


def color_pair(theme: object, name: str) -> tuple[str, str]:
    """``(light, dark)`` values of colour field *name*; the baked value twice
    for a single-palette theme."""
    for field, light, dark in getattr(theme, "mode_colors"):
        if field == name:
            return light, dark
    value: str = getattr(theme, name)
    return value, value


def binding_pair(theme: object, binding: ChromeBinding) -> tuple[str, str]:
    """A chrome colour's ``(light, dark)`` values after its fallback rules."""
    light, dark = color_pair(theme, binding.field)
    if binding.fallback:
        fb_light, fb_dark = color_pair(theme, binding.fallback)
        light, dark = light or fb_light, dark or fb_dark
    if binding.opaque:
        light, dark = opaque_or_white(light), opaque_or_white(dark)
    return light, dark


def theme_color(theme: object, name: str) -> str:
    """Colour field *name* of *theme* after the fallback and opacity rules its
    schema metadata declares."""
    meta = _THEME_FIELDS[name].metadata
    value: str = getattr(theme, name)
    if not value and meta["fallback"]:
        value = getattr(theme, meta["fallback"])
    return opaque_or_white(value) if meta["opaque"] else value


MUTED_STROKE_CLASS = "nf-metro-muted-stroke"
MUTED_FILL_CLASS = "nf-metro-muted-fill"


def muted_adapts(theme: object) -> bool:
    """Whether *theme*'s inactive grey differs between its light and dark palettes.

    Only then do muted line strokes and glyph fills need a class for the chrome
    CSS to switch them; otherwise the baked attribute is right in both modes.
    """
    light, dark = color_pair(theme, "muted_line_color")
    return light != dark


def muted_paint_class(theme: object, muted: bool, prop: str) -> str:
    """The class that lets a muted line ``stroke`` or glyph ``fill`` follow the
    viewer's mode, or ``""`` when nothing needs it."""
    if not (muted and muted_adapts(theme)):
        return ""
    return _ns(MUTED_STROKE_CLASS if prop == "stroke" else MUTED_FILL_CLASS)
