"""nf-core brand theme (the reference metro-map look), in light and dark modes.

Brand identity (fonts, weights, line/station geometry) is shared across modes;
the light variant is the dark one with only its chrome palette - the surfaces
and ink behind the routes - swapped, so the two stay in lockstep by construction.
"""

from dataclasses import replace

from nf_metro.render.style import Theme

NFCORE_DARK_THEME = Theme(
    name="nfcore-dark",
    brand="nfcore",
    mode="dark",
    background_color="#2b2b2b",
    station_fill="#ffffff",
    station_stroke="#333333",
    marker_stroke="#f0f0f0",
    station_stroke_width=1.5,
    line_width=3.0,
    label_color="#e0e0e0",
    label_font_family="'Helvetica Neue', Helvetica, Arial, sans-serif",
    label_font_size=13.0,
    label_font_weight="bold",
    title_color="#ffffff",
    title_font_size=24.0,
    section_fill="#373737",
    section_stroke="#595959",
    section_label_color="#c8c8c8",
    section_label_font_size=16.0,
    legend_background="rgba(0, 0, 0, 0.3)",
    legend_text_color="#e0e0e0",
    legend_font_size=14.0,
)

NFCORE_LIGHT_THEME = replace(
    NFCORE_DARK_THEME,
    name="nfcore-light",
    mode="light",
    background_color="#f5f5f5",
    marker_stroke="#333333",
    label_color="#333333",
    title_color="#111111",
    section_fill="#ededed",
    section_stroke="#e6e6e6",
    section_label_color="#666666",
    legend_background="rgba(255, 255, 255, 0.8)",
    legend_text_color="#333333",
)

NFCORE_THEME = NFCORE_DARK_THEME
