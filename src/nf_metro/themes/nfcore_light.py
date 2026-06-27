"""nf-core light theme — solid-background counterpart of the dark nfcore theme."""

from nf_metro.render.style import Theme

NFCORE_LIGHT_THEME = Theme(
    name="nfcore-light",
    background_color="#f5f5f5",
    station_fill="#ffffff",
    station_stroke="#333333",
    station_stroke_width=1.5,
    line_width=3.0,
    label_color="#333333",
    label_font_family="'Helvetica Neue', Helvetica, Arial, sans-serif",
    label_font_size=13.0,
    label_font_weight="bold",
    title_color="#111111",
    title_font_size=24.0,
    section_fill="#ededed",
    section_stroke="#e6e6e6",
    section_label_color="#666666",
    section_label_font_size=16.0,
    legend_background="rgba(255, 255, 255, 0.8)",
    legend_text_color="#333333",
    legend_font_size=14.0,
)
