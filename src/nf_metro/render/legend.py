"""Legend generation for metro map SVGs."""

from __future__ import annotations

__all__ = ["compute_legend_dimensions", "render_legend"]

import drawsvg as draw

from nf_metro.parser.model import MetroGraph
from nf_metro.render.constants import (
    LEGEND_BORDER_RADIUS,
    LEGEND_CHAR_WIDTH_RATIO,
    LEGEND_LINE_HEIGHT,
    LEGEND_PADDING,
    LEGEND_SWATCH_WIDTH,
    LEGEND_TEXT_GAP,
    LOGO_GAP,
    LOGO_SCALE_FACTOR,
    TEXT_VCENTER_DY,
    line_style_kwargs,
)
from nf_metro.render.style import Theme


def _scale_logo_to_content(
    logo_size: tuple[float, float], content_height: float
) -> tuple[float, float]:
    """Scale logo to fit within content height, preserving aspect ratio.

    Uses 60% of content_height so the logo doesn't dominate the legend.
    """
    orig_w, orig_h = logo_size
    if orig_h <= 0:
        return (0.0, 0.0)
    target_h = content_height * LOGO_SCALE_FACTOR
    aspect = orig_w / orig_h
    return (target_h * aspect, target_h)


def compute_legend_dimensions(
    graph: MetroGraph,
    theme: Theme,
    logo_size: tuple[float, float] | None = None,
) -> tuple[float, float]:
    """Compute the width and height of the legend without rendering it.

    Returns (width, height). Returns (0, 0) if there are no lines.
    logo_size is the original (width, height) of the logo image if present.
    """
    if not graph.lines:
        return (0.0, 0.0)

    line_height = LEGEND_LINE_HEIGHT
    padding = LEGEND_PADDING
    swatch_width = LEGEND_SWATCH_WIDTH
    text_offset = swatch_width + LEGEND_TEXT_GAP

    max_name_len = max(len(ml.display_name) for ml in graph.lines.values())
    char_width = theme.legend_font_size * LEGEND_CHAR_WIDTH_RATIO
    content_height = max(len(graph.lines) * line_height, graph.legend_min_height)

    # Logo scaled to fit content height
    logo_w = 0.0
    logo_gap = 0.0
    if logo_size:
        scaled_w, _ = _scale_logo_to_content(logo_size, content_height)
        logo_w = scaled_w
        logo_gap = LOGO_GAP

    width = padding * 2 + logo_w + logo_gap + text_offset + max_name_len * char_width
    height = padding * 2 + content_height
    return (width, height)


def render_legend(
    d: draw.Drawing,
    graph: MetroGraph,
    theme: Theme,
    x: float,
    y: float,
    logo_path: str | None = None,
    logo_size: tuple[float, float] | None = None,
) -> None:
    """Render a legend showing all metro lines and their colors.

    Positioned at (x, y), rendering downward. If logo_path and logo_size are
    provided, the logo is embedded inside the legend box to the left of the
    line entries.
    """
    if not graph.lines:
        return

    line_height = LEGEND_LINE_HEIGHT
    padding = LEGEND_PADDING
    swatch_width = LEGEND_SWATCH_WIDTH
    text_offset = swatch_width + LEGEND_TEXT_GAP
    content_height = max(len(graph.lines) * line_height, graph.legend_min_height)

    legend_width, legend_height = compute_legend_dimensions(
        graph, theme, logo_size=logo_size
    )

    # Background
    d.append(
        draw.Rectangle(
            x,
            y,
            legend_width,
            legend_height,
            rx=LEGEND_BORDER_RADIUS,
            ry=LEGEND_BORDER_RADIUS,
            fill=theme.legend_background,
        )
    )

    # Logo (left side, vertically centered in content area)
    logo_offset = 0.0
    if logo_path and logo_size:
        scaled_w, scaled_h = _scale_logo_to_content(logo_size, content_height)
        logo_gap = LOGO_GAP
        logo_x = x + padding
        logo_y = y + padding + (content_height - scaled_h) / 2
        d.append(
            draw.Image(
                logo_x,
                logo_y,
                scaled_w,
                scaled_h,
                path=logo_path,
                embed=True,
            )
        )
        logo_offset = scaled_w + logo_gap

    # Line entries
    for i, metro_line in enumerate(graph.lines.values()):
        entry_y = y + padding + i * line_height + line_height / 2

        # Color swatch (line segment)
        dash_kw = line_style_kwargs(metro_line.style)
        d.append(
            draw.Line(
                x + padding + logo_offset,
                entry_y,
                x + padding + logo_offset + swatch_width,
                entry_y,
                stroke=metro_line.color,
                stroke_width=theme.line_width,
                stroke_linecap="round",
                **dash_kw,
            )
        )

        # Label
        d.append(
            draw.Text(
                metro_line.display_name,
                theme.legend_font_size,
                x + padding + logo_offset + text_offset,
                entry_y,
                fill=theme.legend_text_color,
                font_family=theme.label_font_family,
                dy=TEXT_VCENTER_DY,
            )
        )
