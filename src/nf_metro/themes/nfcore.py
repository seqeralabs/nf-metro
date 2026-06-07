"""nf-core dark grey theme (matching reference metro map style)."""

from nf_metro.render.style import Theme

NFCORE_THEME = Theme(
    name="nfcore",
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
    section_label_color="#aaaaaa",
    section_label_font_size=16.0,
    legend_background="rgba(0, 0, 0, 0.3)",
    legend_text_color="#e0e0e0",
    legend_font_size=14.0,
)
