"""Halloween seasonal theme, in light and dark modes.

A novelty brand: pumpkin-orange title and animation balls, bone-white stations
and ink, and witchy purple section chrome. The dark variant is a midnight
graveyard; the light variant is the same palette on aged parchment. Geometry
and type follow the nf-core reference look so layouts render identically.
"""

from dataclasses import replace

from nf_metro.render.style import Theme

HALLOWEEN_DARK_THEME = Theme(
    name="halloween-dark",
    brand="halloween",
    mode="dark",
    background_color="#14101c",
    station_fill="#fff1d6",
    station_stroke="#1a1022",
    marker_stroke="#ffb35c",
    station_stroke_width=1.5,
    line_width=3.0,
    label_color="#f3e6d0",
    label_font_family="'Helvetica Neue', Helvetica, Arial, sans-serif",
    label_font_size=13.0,
    label_font_weight="bold",
    title_color="#ff7518",
    title_font_size=24.0,
    section_fill="#211830",
    section_stroke="#4a2f5e",
    section_label_color="#b48ad6",
    section_label_font_size=16.0,
    legend_background="rgba(10, 6, 16, 0.6)",
    legend_text_color="#f3e6d0",
    legend_font_size=14.0,
    animation_ball_color="#ff7518",
    animation_ball_stroke="#1a1022",
    animation_ball_stroke_width=1.5,
)

HALLOWEEN_LIGHT_THEME = replace(
    HALLOWEEN_DARK_THEME,
    name="halloween-light",
    mode="light",
    background_color="#fbf3e4",
    station_fill="#ffffff",
    station_stroke="#2a1a33",
    marker_stroke="#2a1a33",
    label_color="#2a1a33",
    title_color="#c2410c",
    section_fill="#f3e5cc",
    section_stroke="#e2c9a0",
    section_label_color="#7a4b94",
    legend_background="rgba(251, 243, 228, 0.9)",
    legend_text_color="#2a1a33",
    animation_ball_stroke="#2a1a33",
)
