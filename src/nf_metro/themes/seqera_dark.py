"""Seqera Platform dark theme.

Colors derived from the Seqera design system, dark-mode counterpart of seqera.py:

  $app-brand-seqera: #160F26   - Seqera brand deep navy (background base)
  Darkened neutrals stepping from the brand navy.

Font: Inter Variable - matching the light seqera theme.
"""

from nf_metro.render.style import Theme

SEQERA_DARK_THEME = Theme(
    name="seqera-dark",
    background_color="#1a1625",
    station_fill="#2d273c",
    station_stroke="#7b6ea6",
    station_stroke_width=2.0,
    line_width=4.0,
    label_color="#e0d9f7",
    label_font_family=(
        "'Inter Variable', Inter, 'Helvetica Neue', Helvetica, Arial, sans-serif"
    ),
    label_font_size=14.0,
    label_font_weight="600",
    title_color="#f0ecff",
    title_font_size=26.0,
    section_fill="#231e30",
    section_stroke="#3d3650",
    section_label_color="#9d94b8",
    section_label_font_size=17.0,
    legend_background="rgba(0, 0, 0, 0.4)",
    legend_text_color="#e0d9f7",
    legend_font_size=16.0,
    animation_ball_color="#2d273c",
    animation_ball_stroke="#7b6ea6",
    animation_ball_stroke_width=1.5,
)
