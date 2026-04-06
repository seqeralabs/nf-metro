"""Icon helpers for metro map rendering."""

from __future__ import annotations

__all__ = ["render_file_icon", "render_files_icon", "render_folder_icon"]

import drawsvg as draw

from nf_metro.render.constants import (
    FILES_ICON_OFFSET_RATIO,
    FOLDER_TAB_HEIGHT_RATIO,
    FOLDER_TAB_WIDTH_RATIO,
    ICON_FOLD_CREASE_RATIO,
    ICON_FOLD_OVERLAY_OPACITY,
    ICON_TEXT_OFFSET_RATIO,
    TEXT_VCENTER_DY,
)


def train_icon_path(x: float, y: float, size: float = 12.0) -> str:
    """Generate an SVG path string for a small train icon. Placeholder for future."""
    # Simple diamond shape as placeholder
    hs = size / 2
    return f"M {x} {y - hs} L {x + hs} {y} L {x} {y + hs} L {x - hs} {y} Z"


def render_file_icon(
    d: draw.Drawing,
    cx: float,
    cy: float,
    width: float,
    height: float,
    fold_size: float,
    fill: str,
    stroke: str,
    stroke_width: float,
    corner_radius: float,
    label: str,
    font_size: float,
    font_color: str,
    font_family: str,
) -> None:
    """Render a file/document icon with a dog-ear fold at top-right.

    The icon is centered on (cx, cy). The shape is a rectangle with the
    top-right corner replaced by a diagonal fold.
    """
    hw = width / 2
    hh = height / 2
    x0 = cx - hw
    y0 = cy - hh
    x1 = cx + hw
    y1 = cy + hh
    r = corner_radius
    f = fold_size

    # Main document shape: rectangle with top-right dog-ear
    # Start at top-left + corner radius, go clockwise
    path = draw.Path(
        fill=fill,
        stroke=stroke,
        stroke_width=stroke_width,
        stroke_linejoin="round",
    )
    # Top edge: from top-left corner to fold start
    path.M(x0 + r, y0)
    path.L(x1 - f, y0)
    # Diagonal fold
    path.L(x1, y0 + f)
    # Right edge down to bottom-right corner
    path.L(x1, y1 - r)
    # Bottom-right corner
    path.Q(x1, y1, x1 - r, y1)
    # Bottom edge
    path.L(x0 + r, y1)
    # Bottom-left corner
    path.Q(x0, y1, x0, y1 - r)
    # Left edge
    path.L(x0, y0 + r)
    # Top-left corner
    path.Q(x0, y0, x0 + r, y0)
    path.Z()
    d.append(path)

    # Fold triangle (slightly darker overlay)
    fold_path = draw.Path(
        fill=stroke,
        opacity=ICON_FOLD_OVERLAY_OPACITY,
        stroke="none",
    )
    fold_path.M(x1 - f, y0)
    fold_path.L(x1 - f, y0 + f)
    fold_path.L(x1, y0 + f)
    fold_path.Z()
    d.append(fold_path)

    # Fold crease line
    crease = draw.Path(
        fill="none",
        stroke=stroke,
        stroke_width=stroke_width * ICON_FOLD_CREASE_RATIO,
    )
    crease.M(x1 - f, y0)
    crease.L(x1 - f, y0 + f)
    crease.L(x1, y0 + f)
    d.append(crease)

    # Extension label centered in the body (shifted down slightly to
    # account for fold taking up top-right space)
    text_y = cy + f * ICON_TEXT_OFFSET_RATIO
    d.append(
        draw.Text(
            label,
            font_size,
            cx,
            text_y,
            fill=font_color,
            font_family=font_family,
            font_weight="bold",
            text_anchor="middle",
            dy=TEXT_VCENTER_DY,
        )
    )


def render_files_icon(
    d: draw.Drawing,
    cx: float,
    cy: float,
    width: float,
    height: float,
    fold_size: float,
    fill: str,
    stroke: str,
    stroke_width: float,
    corner_radius: float,
    label: str,
    font_size: float,
    font_color: str,
    font_family: str,
) -> None:
    """Render a stacked-files icon (two overlapping documents).

    The icon is centered on (cx, cy). A slightly offset back page is drawn
    first, then a front page (identical to the single file icon) on top.
    """
    off = width * FILES_ICON_OFFSET_RATIO

    # Back page (offset up-left)
    render_file_icon(
        d,
        cx=cx - off,
        cy=cy - off,
        width=width,
        height=height,
        fold_size=fold_size,
        fill=fill,
        stroke=stroke,
        stroke_width=stroke_width,
        corner_radius=corner_radius,
        label="",
        font_size=font_size,
        font_color=font_color,
        font_family=font_family,
    )

    # Front page (main position)
    render_file_icon(
        d,
        cx=cx + off,
        cy=cy + off,
        width=width,
        height=height,
        fold_size=fold_size,
        fill=fill,
        stroke=stroke,
        stroke_width=stroke_width,
        corner_radius=corner_radius,
        label=label,
        font_size=font_size,
        font_color=font_color,
        font_family=font_family,
    )


def render_folder_icon(
    d: draw.Drawing,
    cx: float,
    cy: float,
    width: float,
    height: float,
    fill: str,
    stroke: str,
    stroke_width: float,
    corner_radius: float,
    label: str,
    font_size: float,
    font_color: str,
    font_family: str,
) -> None:
    """Render a folder icon with a tab on the top-left.

    The icon is centered on (cx, cy). The shape is a rectangle with a
    smaller tab rectangle protruding from the top-left corner.
    """
    hw = width / 2
    hh = height / 2
    r = corner_radius

    tab_h = height * FOLDER_TAB_HEIGHT_RATIO
    tab_w = width * FOLDER_TAB_WIDTH_RATIO

    # The body sits below the tab
    body_top = cy - hh + tab_h
    x0 = cx - hw
    x1 = cx + hw
    y1 = cy + hh

    # Tab shape (top-left rectangle with rounded top corners)
    tab = draw.Path(
        fill=fill,
        stroke=stroke,
        stroke_width=stroke_width,
        stroke_linejoin="round",
    )
    tab_top = cy - hh
    tab_right = x0 + tab_w
    tab.M(x0 + r, tab_top)
    tab.L(tab_right - r, tab_top)
    tab.Q(tab_right, tab_top, tab_right, tab_top + r)
    tab.L(tab_right, body_top)
    tab.L(x0, body_top)
    tab.L(x0, tab_top + r)
    tab.Q(x0, tab_top, x0 + r, tab_top)
    tab.Z()
    d.append(tab)

    # Body rectangle (rounded bottom corners + top-right corner)
    body = draw.Path(
        fill=fill,
        stroke=stroke,
        stroke_width=stroke_width,
        stroke_linejoin="round",
    )
    body.M(x0, body_top)
    body.L(x1 - r, body_top)
    body.Q(x1, body_top, x1, body_top + r)
    body.L(x1, y1 - r)
    body.Q(x1, y1, x1 - r, y1)
    body.L(x0 + r, y1)
    body.Q(x0, y1, x0, y1 - r)
    body.L(x0, body_top)
    body.Z()
    d.append(body)

    # Label centered in the body
    text_y = (body_top + y1) / 2
    d.append(
        draw.Text(
            label,
            font_size,
            cx,
            text_y,
            fill=font_color,
            font_family=font_family,
            font_weight="bold",
            text_anchor="middle",
            dy=TEXT_VCENTER_DY,
        )
    )
