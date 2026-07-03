"""Render constants used across render modules.

Centralizes magic numbers from svg.py, legend.py, animate.py, and icons.py.
Theme-dependent values remain in style.py.
"""

from nf_metro.layout.constants import CURVE_RADIUS, ICON_CAPTION_GAP
from nf_metro.layout.constants import ICON_INTER_GAP as ICON_INTER_GAP  # re-export
from nf_metro.layout.constants import (
    RAIL_KNOB_RADIUS_RATIO as RAIL_KNOB_RADIUS_RATIO,  # re-export
)
from nf_metro.layout.constants import TERMINUS_WIDTH as TERMINUS_WIDTH  # re-export

# ---------------------------------------------------------------------------
# Canvas
# ---------------------------------------------------------------------------
CANVAS_PADDING: float = 60.0
"""Default padding around the entire SVG canvas."""

LEGEND_GAP: float = 30.0
"""Gap between content area and legend (bottom/right positions)."""

LEGEND_INSET: float = 10.0
"""Inset from content edge for corner legend positions (tl/tr/bl/br)."""

LEGEND_ROUTE_CLEARANCE: float = 6.0
"""Clearance kept between the legend box and routed lines when testing overlap."""

LOGO_Y_STANDALONE: float = 5.0
"""Y offset for standalone logo (no legend)."""

LOGO_HEIGHT_DEFAULT: float = 80.0
"""Default logo display height."""

# ---------------------------------------------------------------------------
# Legend
# ---------------------------------------------------------------------------
LEGEND_LINE_HEIGHT: float = 24.0
"""Vertical height per line entry in legend."""

LEGEND_PADDING: float = 12.0
"""Internal padding of legend box."""

LEGEND_SWATCH_WIDTH: float = 24.0
"""Width of color swatch line in legend."""

LEGEND_TEXT_GAP: float = 12.0
"""Gap between swatch end and label text."""

LEGEND_CHAR_WIDTH_RATIO: float = 0.55
"""Character width as a fraction of font size for legend text sizing."""

LOGO_SCALE_FACTOR: float = 0.95
"""Logo scale factor relative to content height."""

LOGO_GAP: float = 12.0
"""Gap between logo and line entries in legend."""

LEGEND_BORDER_RADIUS: int = 6
"""Corner radius for legend background rectangle."""

LEGEND_MARKER_GAP: float = 10.0
"""Vertical gap between the line key and the marker key."""

LEGEND_MARKER_RADIUS: float = 6.0
"""Half-size of a marker-key swatch glyph."""

LEGEND_MARKER_PILL_RATIO: float = 1.7
"""Half-width of a ``pill`` swatch as a multiple of the swatch half-size."""

MARKER_PILL_LENGTH_RATIO: float = 4.0
"""Length of a ``pill`` marker along the line, as a multiple of the station radius."""

# RAIL_KNOB_RADIUS_RATIO is re-exported from layout.constants (see top imports):
# label placement and the renderer share one knob-radius ratio.

RAIL_LINK_HALF_WIDTH_RATIO: float = 0.7
"""Rail-interchange connector bar half-width, as a multiple of the station radius."""

# ---------------------------------------------------------------------------
# SVG drawing
# ---------------------------------------------------------------------------
SVG_CURVE_RADIUS: float = CURVE_RADIUS
"""Default corner radius for edge path smoothing.

Derived from the layout CURVE_RADIUS so routing and rendering agree."""

SECTION_NUM_CIRCLE_R: int = 8
"""Radius of section number circle background (small variant)."""

SECTION_NUM_CIRCLE_R_LARGE: int = 11
"""Radius of section number circle background (large variant)."""

SECTION_NUM_FONT_SIZE: int = 12
"""Font size for section number text inside the circle."""

SECTION_NUM_Y_OFFSET: int = 4
"""Y offset of section number circle from section top."""

SECTION_LABEL_TEXT_OFFSET: int = 5
"""Text X offset from section number circle."""

SECTION_LABEL_CHAR_WIDTH_RATIO: float = 0.6
"""Per-character width of a bold section label as a fraction of its font size.

Used to estimate the horizontal extent of a section header (badge + title) so
the header can be relocated off a route that would otherwise cross it.  Sized a
touch wider than the legend/caption ratio (0.55) because section labels render
bold.  An over-estimate is the safe direction: it only ever moves the header
*earlier*, never leaves a clash undetected."""

SECTION_HEADER_ROUTE_PAD: float = 4.0
"""Slack between a section header's extent and a route, below which the route
counts as clashing with the header.

The placement chain leaves the chosen header position clear of every route by at
least this margin, so the render-time header guard (which checks the raw header
extent) always passes with breathing room."""

SECTION_HEADER_SIDE_GAP: float = 6.0
"""Gap between a section box edge and a rotated side-placed header column."""

SECTION_LABEL_HALF_HEIGHT_RATIO: float = 0.8
"""Half the visual height of a section label as a fraction of its font size,
used to extend the header keep-out band above/below the text baseline."""

SECTION_LABEL_LINE_HEIGHT_RATIO: float = 1.2
"""Line spacing for a wrapped section title, as a multiple of its font size.

Mirrors the layout side's ``LABEL_LINE_HEIGHT`` for station labels."""

HEADER_WRAP_CLEARANCE: float = 8.0
"""Minimum visible gap a wrapped header's extra lines leave before whatever
is nearest in their growth direction (the map title, another section's box,
or the canvas edge).  Matches the buffer ``TITLE_BAND_CLEARANCE`` adds on the
layout side for the same single-line-header clearance."""

TEXT_VCENTER_DY: str = "0.3em"
"""Downward dy shift applied to text that must be visually centered on
a companion graphic (badge circles, legend swatches).  Using dy instead
of ``dominant-baseline: central`` gives more consistent results across
browsers and rasterisers (CairoSVG, resvg, etc.).  Value determined by
pixel-level measurement across CairoSVG and Chromium renderers."""

ICON_STATION_GAP: float = 6.0
"""Gap between terminus station pill and file icon."""

# ICON_INTER_GAP is imported from layout.constants (re-exported for render use)

ICON_BBOX_MARGIN: float = 2.0
"""Margin around icon bounding box for clamping."""

ICON_NAME_GAP: float = ICON_CAPTION_GAP
"""Gap between the bottom of a terminus icon and its name caption.

Aliased from ``layout.constants.ICON_CAPTION_GAP`` so layout spacing
math and render placement share a single source of truth."""

ICON_NAME_FONT_SCALE: float = 0.6
"""Caption font size as a fraction of the theme label font size."""

# ---------------------------------------------------------------------------
# Animation
# ---------------------------------------------------------------------------
ANIMATION_CURVE_RADIUS: float = CURVE_RADIUS
"""Default curve radius for animation motion paths.

Derived from the layout CURVE_RADIUS for consistency."""

MIN_ANIMATION_DURATION: float = 2.0
"""Minimum duration in seconds for ball animation."""

EDGE_CONNECT_TOLERANCE: float = 1.0
"""Tolerance for detecting connected edge endpoints."""

# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------
TRAIN_ICON_SIZE: float = 12.0
"""Default size of train icon placeholder."""

# ---------------------------------------------------------------------------
# Debug overlay
# ---------------------------------------------------------------------------
DEBUG_FONT_SIZE: int = 7
"""Font size for debug overlay labels."""

DEBUG_DIAMOND_RADIUS: int = 5
"""Radius of diamond markers in debug overlay."""

DEBUG_STROKE_WIDTH: float = 1.5
"""Stroke width for hidden station markers in debug mode."""

# ---------------------------------------------------------------------------
# Section box
# ---------------------------------------------------------------------------
SECTION_BOX_RADIUS: int = 8
"""Corner radius for section bounding box rectangles."""

SECTION_STROKE_WIDTH: float = 1.0
"""Stroke width for section bounding box outlines."""

# ---------------------------------------------------------------------------
# Title / watermark
# ---------------------------------------------------------------------------
TITLE_Y_OFFSET: float = 30.0
"""Y position for the title text."""

WATERMARK_FONT_SIZE: int = 8
"""Font size for the attribution watermark."""

WATERMARK_PADDING_RATIO: float = 0.5
"""Fraction of canvas padding used for watermark X inset from right edge."""

WATERMARK_Y_INSET: float = 8.0
"""Y distance from bottom edge for watermark text."""

WATERMARK_BARE_X_INSET: float = 8.0
"""X distance from right/left canvas edge for watermark text in bare mode."""

WATERMARK_FILL: str = "rgba(150, 150, 150, 0.6)"
"""Fill color for the muted attribution watermark."""

CAPTION_FONT_SIZE: int = 11
"""Font size for the figure caption (%%metro caption:) — readable, not a watermark."""

CAPTION_FILL: str = "rgba(200, 200, 200, 0.85)"
"""Fill color for the figure caption."""

# ---------------------------------------------------------------------------
# Fallback colors
# ---------------------------------------------------------------------------
FALLBACK_LINE_COLOR: str = "#888888"
"""Color used when a line has no explicit color defined."""

TERMINUS_FONT_COLOR: str = "#000000"
"""Font color for terminus file icon labels."""

# ---------------------------------------------------------------------------
# Debug overlay colors
# ---------------------------------------------------------------------------
DEBUG_WAYPOINT_COLOR: str = "rgba(255, 200, 50, 0.6)"
"""Color for edge waypoint markers in debug overlay (dark backgrounds)."""

DEBUG_WAYPOINT_COLOR_LIGHT: str = "rgba(200, 80, 0, 0.8)"
"""Color for edge waypoint markers in debug overlay (light backgrounds)."""

DEBUG_ENTRY_PORT_COLOR: str = "rgba(255, 80, 80, 0.7)"
"""Color for entry port diamond markers in debug overlay."""

DEBUG_EXIT_PORT_COLOR: str = "rgba(80, 180, 255, 0.7)"
"""Color for exit port diamond markers in debug overlay."""

DEBUG_HIDDEN_STATION_COLOR: str = "rgba(180, 80, 255, 0.7)"
"""Color for hidden station markers in debug overlay."""

DEBUG_WAYPOINT_RADIUS: float = 3.0
"""Radius of waypoint circle markers in debug overlay."""

DEBUG_LABEL_OFFSET: float = 3.0
"""Y offset from diamond to label text in debug overlay."""

DEBUG_HIDDEN_LABEL_OFFSET: float = 8.0
"""Y offset from hidden station circle to label text."""

DEBUG_ROW_GRID_COLOR: str = "rgba(80, 255, 180, 0.5)"
"""Color for shared Y grid lines in debug overlay (dark backgrounds)."""

DEBUG_ROW_GRID_COLOR_LIGHT: str = "rgba(0, 140, 90, 0.8)"
"""Color for shared Y grid lines in debug overlay (light backgrounds)."""

DEBUG_GRID_COLOR: str = "rgba(255, 255, 0, 0.5)"
"""Color for column/row boundary grid lines in debug overlay (dark backgrounds)."""

DEBUG_GRID_COLOR_LIGHT: str = "rgba(0, 100, 200, 0.7)"
"""Color for column/row boundary grid lines in debug overlay (light backgrounds)."""

# ---------------------------------------------------------------------------
# Icon styling
# ---------------------------------------------------------------------------
ICON_FOLD_OVERLAY_OPACITY: float = 0.15
"""Opacity of the dog-ear fold overlay triangle."""

ICON_FOLD_CREASE_RATIO: float = 0.6
"""Stroke width ratio for the fold crease line relative to main stroke."""

ICON_TEXT_OFFSET_RATIO: float = 0.15
"""Vertical text offset as a fraction of icon height."""

ICON_LABEL_CHAR_WIDTH_RATIO: float = 0.6
"""Estimated glyph width as a fraction of font size, used to shrink the icon
label font so it keeps clear of the icon's left/right edges."""

ICON_LABEL_CLEARANCE: float = 2.5
"""Minimum horizontal clearance (px per side) between the icon label and the
icon's left/right edges; the label font shrinks to honour it."""

ICON_LABEL_LINE_HEIGHT_RATIO: float = 1.1
"""Baseline-to-baseline spacing of wrapped icon-label lines, as a multiple of
the font size."""

FILES_ICON_OFFSET_RATIO: float = 0.15
"""Offset of the back page as a fraction of icon width/height (stacked files icon)."""

FOLDER_TAB_HEIGHT_RATIO: float = 0.2
"""Height of the folder tab as a fraction of total icon height."""

FOLDER_TAB_WIDTH_RATIO: float = 0.4
"""Width of the folder tab as a fraction of total icon width."""

ICON_BANNER_HEIGHT_RATIO: float = 0.38
"""Height of the banner strip as a fraction of icon height (banner style)."""

ICON_BANNER_BOTTOM_MARGIN_RATIO: float = 0.16
"""White space left below the banner strip, as a fraction of icon height."""

ICON_BANNER_FILL: str = "#222222"
"""Fill colour of the banner strip drawn across the icon foot (banner style)."""

ICON_BANNER_TEXT_COLOR: str = "#ffffff"
"""Text colour of the bold label on the banner strip (banner style)."""

# ---------------------------------------------------------------------------
# Animation styling
# ---------------------------------------------------------------------------
ANIMATION_BALL_OPACITY: float = 0.9
"""Opacity of animated balls traveling along lines."""

# ---------------------------------------------------------------------------
# Section labels
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Intra-section station group captions
# ---------------------------------------------------------------------------
GROUP_LABEL_FONT_SCALE: float = 0.95
"""Group caption font size as a fraction of the station label font size."""

GROUP_LABEL_GAP: float = 34.0
"""Gap between the spanned stations' marker extent and the group caption.

Wide enough to clear a station-label row placed on the same side as the
caption, so the band reads as a separate annotation."""

GROUP_LABEL_LABEL_CLEARANCE: float = 8.0
"""Gap between the deepest spanned station *label* and the group caption.

Used in place of ``GROUP_LABEL_GAP`` when the band is positioned off the
station labels' own extent (the labels already carry their full footprint, so
only a small clearance is needed) -- keeps the band hugging the labels rather
than the larger marker-row gap, which over-shoots for diagonal labels."""

GROUP_LABEL_UNDERLINE_GAP: float = 5.0
"""Gap between the group caption text and its bracket rule."""

GROUP_LABEL_UNDERLINE_OPACITY: float = 0.45
"""Opacity of the subtle bracket rule drawn for a group caption."""

GROUP_LABEL_UNDERLINE_WIDTH: float = 1.5
"""Stroke width of the group caption bracket rule."""

GROUP_LABEL_TICK_LENGTH: float = 5.0
"""Length of the inward end-ticks that turn a group rule into a bracket.

The ticks point back towards the spanned stations so the bracket reads as
embracing exactly that contiguous run."""

GROUP_LABEL_BAND_PADDING: float = 10.0
"""Clearance reserved between the lowest group-band element and the bottom
edge of the enclosing section box, so the band always sits inside the box."""

ICON_CLEARANCE_MARGIN: float = 4.0
"""Extra clearance around terminus icons when computing section bounds."""

# ---------------------------------------------------------------------------
# Bridge glyph (non-merging line crossings)
# ---------------------------------------------------------------------------
BRIDGE_GAP_HALF: float = 6.0
"""Padding added on each side of the crossing span when breaking the
under-line, so the gap clears the over bundle's outermost lines."""

BRIDGE_NODE_TOLERANCE: float = 14.0
"""A crossing within this distance of any station/junction/port the layout
places is treated as an interchange, not a crossing, and gets no bridge."""

BRIDGE_JOIN_TOLERANCE: float = 30.0
"""A crossing within this distance of a real station that terminates one of
the crossing lines is its approach to a join, not a crossover - no bridge.
Ports and junction/merge nodes are routing artifacts and do not count."""

BRIDGE_MIN_ANGLE_DEG: float = 12.0
"""Minimum angle between two segments for their intersection to count as a
crossing (near-parallel bundle slivers are not crossings)."""

BRIDGE_CLUSTER_RADIUS: float = 30.0
"""Crossings within this distance are treated as one bundle-crossing event:
the whole under bundle breaks with a single gap spanning the over bundle."""

BRIDGE_CORNER_CLEARANCE: float = 2.0
"""A crossing must sit at least ``curve_radius + this`` from a corner of the
under-line, so the break never lands inside a smoothed corner."""

# ---------------------------------------------------------------------------
# Line styles (stroke dash arrays)
# ---------------------------------------------------------------------------
STROKE_DASHARRAY: dict[str, str] = {
    "dashed": "8,4",
    "dotted": "2,4",
}
"""SVG stroke-dasharray values for non-solid line styles."""


def line_style_kwargs(style: str) -> dict[str, str]:
    """Return extra SVG kwargs for a metro line style (dashed/dotted)."""
    dasharray = STROKE_DASHARRAY.get(style)
    if dasharray:
        return {"stroke_dasharray": dasharray}
    return {}
