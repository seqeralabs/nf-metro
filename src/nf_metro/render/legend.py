"""Legend generation for metro map SVGs."""

from __future__ import annotations

__all__ = ["compute_legend_dimensions", "render_legend"]

from dataclasses import dataclass

import drawsvg as draw

from nf_metro.parser.model import MetroGraph, MetroLine
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


@dataclass
class _LegendRow:
    """One row of the legend: a label plus the line(s) its swatch shows."""

    label: str
    lines: tuple[MetroLine, ...]


def _combo_standalone_members(graph: MetroGraph, line_ids: tuple[str, ...]) -> set[str]:
    """Return combo members that also travel alone somewhere.

    A member is "stand-alone" if it traverses an edge that is not shared by
    every other member of the combo, i.e. the line breaks away from the bundle
    to a destination the rest do not reach. Such a line keeps its own legend
    row in addition to the combo row, so the diagram's lone segment is labelled.
    """
    edges_by_line: dict[str, set[tuple[str, str]]] = {lid: set() for lid in line_ids}
    for e in graph.edges:
        if e.line_id in edges_by_line:
            edges_by_line[e.line_id].add((e.source, e.target))

    nonempty = [edges for edges in edges_by_line.values() if edges]
    shared = set.intersection(*nonempty) if nonempty else set()
    return {lid for lid in line_ids if edges_by_line[lid] - shared}


def _legend_rows(graph: MetroGraph) -> list[_LegendRow]:
    """Return the ordered legend rows for a graph.

    Each metro line that is not part of a ``legend_combo`` gets its own row, in
    definition order. Each combo named in ``graph.legend_combos`` becomes a
    single row whose swatch shows its constituent lines as adjacent stripes. A
    constituent line is suppressed from its own individual row only while it
    travels entirely within the bundle; if it has a stand-alone segment it
    keeps its individual row too (see ``_combo_standalone_members``).
    """
    suppressed_ids: set[str] = set()
    for line_ids, _label in graph.legend_combos:
        suppressed_ids.update(
            set(line_ids) - _combo_standalone_members(graph, line_ids)
        )

    rows: list[_LegendRow] = []
    for ml in graph.lines.values():
        if ml.id in suppressed_ids:
            continue
        rows.append(_LegendRow(label=ml.display_name, lines=(ml,)))

    for line_ids, label in graph.legend_combos:
        members = tuple(graph.lines[lid] for lid in line_ids if lid in graph.lines)
        if members:
            rows.append(_LegendRow(label=label, lines=members))

    return rows


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
    rows: list[_LegendRow],
    logo_size: tuple[float, float] | None,
) -> tuple[float, float, float, float]:
    """Return (text_block_h, content_h, logo_w, logo_h) for the legend.

    ``content_h`` is the inner height of the legend box: the taller of the
    text block and a (possibly enlarged) logo.
    """
    line_height = LEGEND_LINE_HEIGHT
    text_block_h = max(len(rows) * line_height, graph.legend_min_height)
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
    rows: list[_LegendRow] | None = None,
) -> tuple[float, float]:
    """Compute the width and height of the legend without rendering it.

    Returns (width, height). Returns (0, 0) if there are no lines.
    logo_size is the original (width, height) of the logo image if present.
    ``rows`` may be passed by a caller that already built them (see
    ``render_legend``) to avoid recomputing.
    """
    if not graph.lines:
        return (0.0, 0.0)

    if rows is None:
        rows = _legend_rows(graph)
    if not rows:
        return (0.0, 0.0)

    padding = LEGEND_PADDING
    swatch_width = LEGEND_SWATCH_WIDTH
    text_offset = swatch_width + LEGEND_TEXT_GAP

    max_name_len = max(len(row.label) for row in rows)
    char_width = theme.legend_font_size * LEGEND_CHAR_WIDTH_RATIO

    _text_h, content_height, logo_w, _logo_h = _legend_metrics(graph, rows, logo_size)
    logo_gap = LOGO_GAP if logo_size else 0.0

    width = padding * 2 + logo_w + logo_gap + text_offset + max_name_len * char_width
    height = padding * 2 + content_height
    return (width, height)


def _render_swatch(
    d: draw.Drawing,
    row: _LegendRow,
    theme: Theme,
    x0: float,
    entry_y: float,
    swatch_width: float,
) -> None:
    """Draw the colour swatch for a row.

    A single-line row draws one horizontal segment. A combo row draws each
    constituent line as a stripe at a small vertical offset, so the swatch
    reads as a bundle of adjacent lines, each honouring its style.
    """
    n = len(row.lines)
    if n == 1:
        offsets = [0.0]
    else:
        spacing = min(theme.line_width, LEGEND_LINE_HEIGHT / (n + 1))
        offsets = [(i - (n - 1) / 2.0) * spacing for i in range(n)]

    for ml, dy in zip(row.lines, offsets):
        dash_kw = line_style_kwargs(ml.style)
        d.append(
            draw.Line(
                x0,
                entry_y + dy,
                x0 + swatch_width,
                entry_y + dy,
                stroke=ml.color,
                stroke_width=theme.line_width,
                stroke_linecap="round",
                **dash_kw,
            )
        )


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

    rows = _legend_rows(graph)
    if not rows:
        return

    line_height = LEGEND_LINE_HEIGHT
    padding = LEGEND_PADDING
    swatch_width = LEGEND_SWATCH_WIDTH
    text_offset = swatch_width + LEGEND_TEXT_GAP

    text_block_h, content_height, scaled_w, scaled_h = _legend_metrics(
        graph, rows, logo_size
    )

    legend_width, legend_height = compute_legend_dimensions(
        graph, theme, logo_size=logo_size, rows=rows
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
    for i, row in enumerate(rows):
        entry_y = text_top + i * line_height + line_height / 2

        _render_swatch(
            d,
            row,
            theme,
            x + padding + logo_offset,
            entry_y,
            swatch_width,
        )

        # Label
        d.append(
            draw.Text(
                row.label,
                theme.legend_font_size,
                x + padding + logo_offset + text_offset,
                entry_y,
                fill=theme.legend_text_color,
                font_family=theme.label_font_family,
                dy=TEXT_VCENTER_DY,
            )
        )
