"""Theme and style constants for metro map rendering."""

from __future__ import annotations

__all__ = ["Theme"]

from dataclasses import dataclass

from nf_metro.layout.constants import (
    ICON_HALF_HEIGHT,
    STATION_RADIUS_APPROX,
    TERMINUS_WIDTH,
)


@dataclass(kw_only=True)
class Theme:
    """Visual theme for a metro map."""

    name: str
    background_color: str
    station_fill: str
    station_stroke: str
    station_radius: float = STATION_RADIUS_APPROX
    station_stroke_width: float
    line_width: float
    label_color: str
    label_font_family: str
    label_font_size: float
    label_font_weight: str
    title_color: str
    title_font_size: float
    section_fill: str
    section_stroke: str
    section_label_color: str
    section_label_font_size: float
    legend_background: str
    legend_text_color: str
    legend_font_size: float
    # Animation settings
    animation_ball_radius: float = 3.0
    animation_ball_color: str = "#ffffff"
    animation_ball_stroke: str = ""
    animation_ball_stroke_width: float = 1.0
    animation_balls_per_line: int = 1
    animation_speed: float = 80.0  # pixels per second
    # Terminus (file icon) settings
    terminus_width: float = TERMINUS_WIDTH
    terminus_height: float = 2 * ICON_HALF_HEIGHT
    terminus_fold_size: float = 8.0
    terminus_fill: str = ""  # empty = inherit station_fill
    terminus_stroke: str = ""  # empty = inherit station_stroke
    terminus_stroke_width: float = 1.5
    terminus_corner_radius: float = 2.0
    terminus_font_size: float = 7.5
    terminus_font_color: str = ""  # empty = inherit label_color
    # Bridge glyph at non-merging line crossings
    bridge_glyph: bool = True
    # Interior fill for "open" markers (%%metro marker: ... | open). Empty
    # falls back to the background colour, or white on transparent themes.
    marker_open_fill: str = ""
    # Outline for marker glyphs (%%metro marker:) and their legend swatches.
    # A light outline keeps dark-filled markers visible against a dark
    # background. Empty inherits station_stroke.
    marker_stroke: str = ""
