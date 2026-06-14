"""Seqera Platform light theme.

Colors sourced from the Seqera Platform design system
(tower-web/src/styles/_variables.scss):

  $app-brand-seqera: #160F26   - Seqera brand dark navy (title color)
  $body-background:  #f8f9fa   - Platform body background
  $app-purple-neutral-1: #F3F3F4  - lightest Seqera-tinted neutral (section fill)
  $app-purple-neutral-2: #E8E7E9  - second step (section border)
  $app-purple-neutral-10: #2D273C - dark brand purple (station stroke)
  $app-purple-neutral-3:  #D0CFD4 - mid neutral (section border)
  $clr-border:       #dee2e6   - Platform default border
  $app-grey-neutral-black: #242424 - Platform primary text
  $app-text-muted:   #6c757d   - Platform muted/secondary text

Font: Inter Variable - tower-web/src/styles/foundations/_typography.scss.
"""

from nf_metro.render.style import Theme

SEQERA_THEME = Theme(
    name="seqera",
    background_color="#f8f9fa",
    station_fill="#ffffff",
    station_stroke="#2D273C",
    station_stroke_width=2.0,
    line_width=4.0,
    label_color="#242424",
    label_font_family=(
        "'Inter Variable', Inter, 'Helvetica Neue', Helvetica, Arial, sans-serif"
    ),
    label_font_size=14.0,
    label_font_weight="600",
    title_color="#160F26",
    title_font_size=26.0,
    section_fill="#F3F3F4",
    section_stroke="#D0CFD4",
    section_label_color="#6c757d",
    section_label_font_size=17.0,
    legend_background="rgba(248, 249, 250, 0.9)",
    legend_text_color="#242424",
    legend_font_size=16.0,
    animation_ball_color="#ffffff",
    animation_ball_stroke="#2D273C",
    animation_ball_stroke_width=1.5,
)
