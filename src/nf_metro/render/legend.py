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
    STRIPE_RIBBON_WIDTH_RATIO,
    TEXT_VCENTER_DY,
    line_style_kwargs,
)
from nf_metro.render.style import Theme


def _scale_logo_to_content(
    logo_size: tuple[float, float], text_block_height: float, scale: float = 1.0
) -> tuple[float, float]:
    """Scale a logo against the legend's text block, preserving aspect ratio.

    The logo is sized to ``LOGO_SCALE_FACTOR`` of the text block height, then
    multiplied by the user ``scale`` (``%%metro logo_scale:``). A scale above 1
    can make the logo taller than the text block, in which case the legend box
    grows to contain it (see ``_legend_metrics``).
    """
    orig_w, orig_h = logo_size
    if orig_h <= 0:
        return (0.0, 0.0)
    target_h = text_block_height * LOGO_SCALE_FACTOR * scale
    aspect = orig_w / orig_h
    return (target_h * aspect, target_h)


def _legend_metrics(
    graph: MetroGraph,
    logo_size: tuple[float, float] | None,
) -> tuple[float, float, float, float]:
    """Return (text_block_h, content_h, logo_w, logo_h) for the legend.

    ``content_h`` is the inner height of the legend box: the taller of the
    text block and a (possibly enlarged) logo.
    """
    line_height = LEGEND_LINE_HEIGHT
    text_block_h = max(len(graph.lines) * line_height, graph.legend_min_height)
    logo_w = logo_h = 0.0
    if logo_size:
        logo_w, logo_h = _scale_logo_to_content(
            logo_size, text_block_h, graph.logo_scale
        )
    content_h = max(text_block_h, logo_h)
    return text_block_h, content_h, logo_w, logo_h


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

    padding = LEGEND_PADDING
    swatch_width = LEGEND_SWATCH_WIDTH
    text_offset = swatch_width + LEGEND_TEXT_GAP

    max_name_len = max(len(ml.display_name) for ml in graph.lines.values())
    char_width = theme.legend_font_size * LEGEND_CHAR_WIDTH_RATIO

    _text_h, content_height, logo_w, _logo_h = _legend_metrics(graph, logo_size)
    logo_gap = LOGO_GAP if logo_size else 0.0

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

    text_block_h, content_height, scaled_w, scaled_h = _legend_metrics(graph, logo_size)

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
        logo_offset = scaled_w + LOGO_GAP

    # Line entries, vertically centred within the content area (which can be
    # taller than the text block when an enlarged logo grows the box).
    text_top = y + padding + (content_height - text_block_h) / 2
    for i, metro_line in enumerate(graph.lines.values()):
        entry_y = text_top + i * line_height + line_height / 2

        # Color swatch (line segment). A striped/composite line draws one
        # thinner segment per colour, stacked to span the normal line width.
        dash_kw = line_style_kwargs(metro_line.style)
        swatch_x0 = x + padding + logo_offset
        swatch_x1 = swatch_x0 + swatch_width
        n_colors = len(metro_line.colors)
        ribbon_w = (
            theme.line_width
            if n_colors == 1
            else theme.line_width * STRIPE_RIBBON_WIDTH_RATIO
        )
        for i, color in enumerate(metro_line.colors):
            ribbon_y = entry_y + (i - (n_colors - 1) / 2.0) * ribbon_w
            d.append(
                draw.Line(
                    swatch_x0,
                    ribbon_y,
                    swatch_x1,
                    ribbon_y,
                    stroke=color,
                    stroke_width=ribbon_w,
                    stroke_linecap="round" if n_colors == 1 else "butt",
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
