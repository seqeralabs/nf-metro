"""The theme schema: every visual setting a theme can carry.

:class:`Theme` is the single definition of what a theme is. Each field's
metadata records what it means (``doc``), which reference group it belongs to
(``group``), whether its value is a colour (``color``), and its allowed values
(``choices``, ``pattern``, ``maximum``). Theme files are validated against the
JSON Schema generated from it, and the field reference in ``docs/theming.mdx``
is generated from that schema. :data:`CHROME` says which colours reach the
stylesheet, where a host page may recolour them.
"""

from __future__ import annotations

__all__ = [
    "CHROME",
    "FONT_FAMILY_PATTERN",
    "DASHARRAY_PATTERN",
    "FONT_WEIGHTS",
    "GLYPH_PILL",
    "GLYPH_RING",
    "GLYPH_TICK",
    "MUTED_FILL_CLASS",
    "MUTED_STROKE_CLASS",
    "RING_CHAIN",
    "RING_GROW",
    "TERMINUS_BAR",
    "TERMINUS_NUB",
    "Chrome",
    "Theme",
    "adaptive_value",
    "authored_fields",
    "color_pair",
    "colors_adapt",
    "is_adaptive",
    "muted_paint_class",
]

import dataclasses
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
    ANIMATION_BALL_OPACITY,
    BRIDGE_GAP_HALF,
    CANVAS_PADDING,
    CAPTION_FILL,
    CAPTION_FONT_SIZE,
    FALLBACK_LINE_COLOR,
    GROUP_LABEL_UNDERLINE_OPACITY,
    GROUP_LABEL_UNDERLINE_WIDTH,
    ICON_BANNER_FILL,
    ICON_BANNER_TEXT_COLOR,
    LEGEND_BORDER_RADIUS,
    LEGEND_LINE_HEIGHT,
    LEGEND_PADDING,
    LEGEND_SWATCH_WIDTH,
    SECTION_BOX_RADIUS,
    SECTION_NUM_FONT_SIZE,
    SECTION_STROKE_WIDTH,
    STROKE_DASHARRAY,
    TERMINUS_FONT_COLOR,
    WATERMARK_FILL,
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

# Free text that lands in SVG attributes, held to what CSS needs.
FONT_FAMILY_PATTERN = r"^[\w\s,'.-]+$"
DASHARRAY_PATTERN = r"^\s*(\d+(\.\d+)?([\s,]+\d+(\.\d+)?)*\s*)?$"
FONT_WEIGHTS = (
    "normal",
    "bold",
    "bolder",
    "lighter",
    *(f"{w}00" for w in range(1, 10)),
)


def _field(
    default: Any = _MISSING,  # noqa: ANN401
    *,
    doc: str,
    group: str,
    color: bool = False,
    choices: tuple[str, ...] = (),
    pattern: str = "",
    maximum: float | None = None,
) -> Any:  # noqa: ANN401
    return dataclasses.field(
        default=default,
        metadata={
            "doc": doc,
            "group": group,
            "color": color,
            "choices": choices,
            "pattern": pattern,
            "maximum": maximum,
        },
    )


def _font_weight(default: Any = _MISSING, *, doc: str, group: str) -> Any:  # noqa: ANN401
    return _field(default, doc=doc, group=group, choices=FONT_WEIGHTS)


def _derived(default: Any) -> Any:  # noqa: ANN401
    return dataclasses.field(default=default, metadata={"derived": True})


@dataclass(kw_only=True, frozen=True)
class Theme:
    """Visual theme for a metro map, resolved for one display mode.

    Fields without a default are set by the base theme every stack starts
    from (``nfcore``). A colour whose doc says it takes another field when
    empty is filled in when the stack is resolved, so a resolved theme's
    fields are all concrete.
    """

    # The palette this theme is resolved for, set by the resolver.
    mode: str = _derived("dark")
    # ``(field, light value, dark value)`` for every colour field.
    mode_colors: tuple[tuple[str, str, str], ...] = _derived(())

    background_fill: str = _field(
        doc="Canvas fill. `none` leaves the canvas transparent.",
        group="Canvas and text",
        color=True,
    )
    title_color: str = _field(
        doc="Map title text.", group="Canvas and text", color=True
    )
    title_font_size: float = _field(doc="Map title size, px.", group="Canvas and text")
    title_font_weight: str = _font_weight(
        "bold", doc="Map title weight.", group="Canvas and text"
    )
    label_color: str = _field(
        doc="Station label text.", group="Canvas and text", color=True
    )
    label_font_family: str = _field(
        doc="CSS font stack for station labels, and for every other text element.",
        group="Canvas and text",
        pattern=FONT_FAMILY_PATTERN,
    )
    label_font_size: float = _field(
        doc="Station label size, px.", group="Canvas and text"
    )
    label_font_weight: str = _font_weight(
        doc="Station label weight (`bold`, `normal`, `600`, ...).",
        group="Canvas and text",
    )
    label_halo_width: float = _field(
        3.0,
        doc="Width of the knockout halo behind station labels, px. 0 disables it.",
        group="Canvas and text",
    )
    knockout_fill: str = _field(
        "",
        doc="Halo behind station labels and the inside of `open` markers. Empty "
        "takes the background, or white on a transparent canvas.",
        group="Canvas and text",
        color=True,
    )
    caption_color: str = _field(
        CAPTION_FILL,
        doc="`%%metro caption:` text.",
        group="Canvas and text",
        color=True,
    )
    caption_font_size: float = _field(
        CAPTION_FONT_SIZE, doc="Caption size, px.", group="Canvas and text"
    )
    watermark_color: str = _field(
        WATERMARK_FILL,
        doc="The attribution line.",
        group="Canvas and text",
        color=True,
    )
    canvas_padding: float = _field(
        CANVAS_PADDING,
        doc="Margin around the map on every side, px.",
        group="Canvas and text",
    )

    section_fill: str = _field(
        doc="Section box fill. `transparent` draws an outline only.",
        group="Sections",
        color=True,
    )
    section_stroke: str = _field(
        doc="Section box outline.", group="Sections", color=True
    )
    section_label_color: str = _field(
        doc="Section titles and group captions.", group="Sections", color=True
    )
    section_label_font_size: float = _field(
        doc="Section title size, px.", group="Sections"
    )
    section_label_font_weight: str = _font_weight(
        "bold", doc="Section title weight.", group="Sections"
    )
    uppercase_section_labels: bool = _field(
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
        doc="SVG dash pattern for the section outline, px (`2 4`). Empty is solid.",
        group="Sections",
        pattern=DASHARRAY_PATTERN,
    )
    section_corner_radius: float = _field(
        SECTION_BOX_RADIUS, doc="Section box corner radius, px.", group="Sections"
    )
    number_sections: bool = _field(
        True,
        doc="Draw the numbered badge beside each section title. Off, titles "
        "start where the badge would be.",
        group="Sections",
    )
    section_badge_fill: str = _field(
        "",
        doc="Number badge fill. Empty takes `station_stroke`.",
        group="Sections",
        color=True,
    )
    section_badge_text_color: str = _field(
        "",
        doc="Number badge digit. Empty takes `station_fill`.",
        group="Sections",
        color=True,
    )
    section_badge_font_size: float = _field(
        SECTION_NUM_FONT_SIZE, doc="Number badge digit size, px.", group="Sections"
    )
    group_rule_width: float = _field(
        GROUP_LABEL_UNDERLINE_WIDTH,
        doc="Width of the rule under a station group's caption, px.",
        group="Sections",
    )
    group_rule_opacity: float = _field(
        GROUP_LABEL_UNDERLINE_OPACITY,
        doc="Opacity of the rule under a station group's caption, 0 to 1.",
        group="Sections",
        maximum=1,
    )

    legend_fill: str = _field(doc="Legend box fill.", group="Legend", color=True)
    legend_stroke: str = _field(
        "",
        doc="Legend box outline. Empty draws none.",
        group="Legend",
        color=True,
    )
    legend_text_color: str = _field(doc="Legend text.", group="Legend", color=True)
    legend_font_size: float = _field(doc="Legend text size, px.", group="Legend")
    legend_stroke_width: float = _field(
        0.0, doc="Legend outline width, px. 0 draws none.", group="Legend"
    )
    legend_corner_radius: float = _field(
        LEGEND_BORDER_RADIUS, doc="Legend box corner radius, px.", group="Legend"
    )
    legend_padding: float = _field(
        LEGEND_PADDING, doc="Space inside the legend box, px.", group="Legend"
    )
    legend_line_height: float = _field(
        LEGEND_LINE_HEIGHT, doc="Height of each legend row, px.", group="Legend"
    )
    legend_swatch_width: float = _field(
        LEGEND_SWATCH_WIDTH, doc="Length of each line swatch, px.", group="Legend"
    )

    station_fill: str = _field(
        doc="Inside of station capsules, interchange rings and the strip "
        "bridging a chain of them.",
        group="Stations",
        color=True,
    )
    station_stroke: str = _field(
        doc="Outline of station capsules and interchange rings, and the `bar` "
        "line end.",
        group="Stations",
        color=True,
    )
    station_stroke_width: float = _field(
        doc="Station capsule outline width, px.", group="Stations"
    )
    station_radius: float = _field(
        STATION_RADIUS_APPROX, doc="Station capsule end radius, px.", group="Stations"
    )
    marker_stroke: str = _field(
        "",
        doc="Outline of `%%metro marker:` glyphs and their legend swatches. "
        "Empty takes `station_stroke`.",
        group="Stations",
        color=True,
    )
    station_glyph: str = _field(
        GLYPH_PILL,
        doc="How a station on one line is drawn. `pill` draws a capsule. "
        "`tick` draws a tab in the line's colour on the label's side, "
        "and a bar across the line's end (a buffer stop) at a terminus.",
        group="Stations",
        choices=(GLYPH_PILL, GLYPH_TICK),
    )
    interchange_glyph: str = _field(
        GLYPH_PILL,
        doc="How a station on several lines is drawn. `pill` draws a capsule "
        "across them. `ring` draws one ring centred on the lines, or a chain "
        "of touching rings bridged by a strip when the lines are too wide for "
        "one.",
        group="Stations",
        choices=(GLYPH_PILL, GLYPH_RING),
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
        "× `line_width`.",
        group="Stations",
    )
    station_tick_length: float = _field(
        0.66,
        doc="How far a `tick` tab reaches past the edge of its line, and how "
        "far a terminus bar overhangs each side, × `line_width`.",
        group="Stations",
    )
    ring_inner_diameter: float = _field(
        2.0,
        doc="Inside diameter of an interchange ring, × `line_width`.",
        group="Stations",
    )
    ring_stroke_width: float = _field(
        0.5,
        doc="Interchange ring outline width, × `line_width`.",
        group="Stations",
    )
    ring_bridge_width: float = _field(
        0.5,
        doc="Width of the strip bridging a chain of rings, × `line_width`.",
        group="Stations",
    )
    ring_max_lines: int = _field(
        3,
        doc="Most lines on one track a single ring of `ring_inner_diameter` "
        "covers, a count; a wider bundle follows `ring_overflow`.",
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
    line_cap: str = _field(
        "round",
        doc="End caps of route strokes: `round` or `butt`.",
        group="Lines",
        choices=("round", "butt"),
    )
    dashed_dasharray: str = _field(
        STROKE_DASHARRAY["dashed"],
        doc="SVG dash pattern of a `dashed` line, px.",
        group="Lines",
        pattern=DASHARRAY_PATTERN,
    )
    dotted_dasharray: str = _field(
        STROKE_DASHARRAY["dotted"],
        doc="SVG dash pattern of a `dotted` line, px.",
        group="Lines",
        pattern=DASHARRAY_PATTERN,
    )
    muted_line_color: str = _field(
        FALLBACK_LINE_COLOR,
        doc="Grey for inactive lines and the stations, labels and icons only "
        "they touch.",
        group="Lines",
        color=True,
    )
    bridge_lines: bool = _field(
        True,
        doc="Break the lower line where two unrelated lines cross.",
        group="Lines",
    )
    bridge_gap: float = _field(
        2 * BRIDGE_GAP_HALF,
        doc="Gap a line leaves either side of a line it hops over, px.",
        group="Lines",
    )
    chevron_size: float = _field(
        4.0,
        doc="Half-length and half-width of `--directional` chevrons, px.",
        group="Lines",
    )
    chevron_spacing: float = _field(
        64.0, doc="Distance between chevrons along a route, px.", group="Lines"
    )
    chevron_opacity: float = _field(
        0.75, doc="Chevron opacity, 0 to 1.", group="Lines", maximum=1
    )
    chevron_color: str = _field(
        "",
        doc="Chevron colour. Empty takes the line's colour.",
        group="Lines",
        color=True,
    )

    icon_width: float = _field(
        TERMINUS_WIDTH, doc="File icon width, px.", group="File icons"
    )
    icon_height: float = _field(
        2 * ICON_HALF_HEIGHT, doc="File icon height, px.", group="File icons"
    )
    icon_fold_size: float = _field(
        8.0, doc="Size of the folded corner, px.", group="File icons"
    )
    icon_corner_radius: float = _field(
        2.0, doc="Icon corner radius, px.", group="File icons"
    )
    icon_fill: str = _field(
        "",
        doc="Icon fill. Empty takes `station_fill`.",
        group="File icons",
        color=True,
    )
    icon_stroke: str = _field(
        "",
        doc="Icon outline. Empty takes `station_stroke`.",
        group="File icons",
        color=True,
    )
    icon_stroke_width: float = _field(
        1.5, doc="Icon outline width, px.", group="File icons"
    )
    icon_font_size: float = _field(
        7.5, doc="Size of the format label inside the icon, px.", group="File icons"
    )
    icon_text_color: str = _field(
        TERMINUS_FONT_COLOR,
        doc="Format label inside the icon.",
        group="File icons",
        color=True,
    )
    icon_banner_fill: str = _field(
        ICON_BANNER_FILL,
        doc="Fill of the band across a file icon.",
        group="File icons",
        color=True,
    )
    icon_banner_text_color: str = _field(
        ICON_BANNER_TEXT_COLOR,
        doc="File type text on the icon band.",
        group="File icons",
        color=True,
    )

    animation_ball_radius: float = _field(
        3.0, doc="Radius of `--animate` balls, px.", group="Animation"
    )
    animation_ball_fill: str = _field(
        "#ffffff", doc="Ball fill.", group="Animation", color=True
    )
    animation_ball_opacity: float = _field(
        ANIMATION_BALL_OPACITY,
        doc="Ball opacity, 0 to 1.",
        group="Animation",
        maximum=1,
    )
    animation_ball_stroke: str = _field(
        "", doc="Ball outline. Empty draws none.", group="Animation", color=True
    )
    animation_ball_stroke_width: float = _field(
        1.0, doc="Ball outline width, px.", group="Animation"
    )
    animation_balls_per_line: int = _field(
        1, doc="Balls travelling along each route, a count.", group="Animation"
    )
    animation_speed: float = _field(
        80.0, doc="Ball speed, px per second.", group="Animation"
    )


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


class Chrome(NamedTuple):
    """How a chrome colour reaches the stylesheet.

    ``targets`` are the ``(class, property, muted)`` rules it paints; a muted
    target also gets a rule greying it when every line touching it is
    inactive. An ``adaptive_only`` colour gets its rules only when its light
    and dark values differ, and is otherwise left to the baked attribute.
    """

    css_var: str
    targets: tuple[tuple[str, str, bool], ...]
    adaptive_only: bool = False


#: Every chrome colour, in the order its CSS rules are emitted.
CHROME: MappingProxyType[str, Chrome] = MappingProxyType(
    {
        "background_fill": Chrome("--nfm-map-bg", (("nf-metro-bg", "fill", False),)),
        "title_color": Chrome(
            "--nfm-map-title-color", (("nf-metro-title", "fill", False),)
        ),
        "label_color": Chrome(
            "--nfm-map-label-color", (("nf-metro-station-label", "fill", True),)
        ),
        "caption_color": Chrome(
            "--nfm-map-caption-color", (("nf-metro-caption", "fill", False),), True
        ),
        "section_fill": Chrome(
            "--nfm-map-section-fill", (("nf-metro-section-box", "fill", False),)
        ),
        "section_stroke": Chrome(
            "--nfm-map-section-stroke", (("nf-metro-section-box", "stroke", False),)
        ),
        "section_label_color": Chrome(
            "--nfm-map-section-label-color",
            (
                ("nf-metro-section-label", "fill", False),
                ("nf-metro-group-label", "fill", False),
                ("nf-metro-group-underline", "stroke", False),
            ),
        ),
        "legend_fill": Chrome(
            "--nfm-map-legend-bg", (("nf-metro-legend-bg", "fill", False),)
        ),
        "legend_stroke": Chrome(
            "--nfm-map-legend-stroke", (("nf-metro-legend-bg", "stroke", False),)
        ),
        "legend_text_color": Chrome(
            "--nfm-map-legend-text-color", (("nf-metro-legend-text", "fill", False),)
        ),
        "marker_stroke": Chrome(
            "--nfm-map-marker-stroke", (("nf-metro-marker-stroke", "stroke", True),)
        ),
        "station_stroke": Chrome(
            "--nfm-map-station-stroke",
            (
                ("nf-metro-ring", "stroke", True),
                ("nf-metro-ring-link", "fill", True),
                ("nf-metro-terminus-bar", "fill", True),
                ("nf-metro-capsule", "stroke", True),
                ("nf-metro-capsule-outline", "stroke", True),
                ("nf-metro-capsule-outline", "fill", True),
            ),
            True,
        ),
        "station_fill": Chrome(
            "--nfm-map-station-fill",
            (
                ("nf-metro-ring-hole", "fill", False),
                ("nf-metro-ring-bridge", "fill", False),
                ("nf-metro-capsule", "fill", False),
                ("nf-metro-capsule-core", "stroke", False),
                ("nf-metro-capsule-core", "fill", False),
            ),
            True,
        ),
    }
)


def adaptive_value(light: str, dark: str) -> str:
    """One CSS value for a light/dark pair: ``light-dark()`` unless they match."""
    if light == dark:
        return light
    return f"light-dark({light}, {dark})"


def color_pair(theme: Theme, name: str) -> tuple[str, str]:
    """``(light, dark)`` values of colour field *name*."""
    for field, light, dark in theme.mode_colors:
        if field == name:
            return light, dark
    value: str = getattr(theme, name)
    return value, value


def colors_adapt(theme: Theme, *names: str) -> bool:
    """Whether any of colour fields *names* differs between the two palettes."""
    return any(len(set(color_pair(theme, name))) > 1 for name in names)


def is_adaptive(theme: Theme) -> bool:
    """Whether any of *theme*'s colours differs between its two palettes, so
    chrome can follow the viewer."""
    return any(light != dark for _, light, dark in theme.mode_colors)


MUTED_STROKE_CLASS = "nf-metro-muted-stroke"
MUTED_FILL_CLASS = "nf-metro-muted-fill"


def muted_paint_class(theme: Theme, muted: bool, prop: str) -> str:
    """The class that lets a muted line ``stroke`` or glyph ``fill`` follow the
    viewer's mode, or ``""`` when the inactive grey is the same in both."""
    if not (muted and colors_adapt(theme, "muted_line_color")):
        return ""
    return _ns(MUTED_STROKE_CLASS if prop == "stroke" else MUTED_FILL_CLASS)
