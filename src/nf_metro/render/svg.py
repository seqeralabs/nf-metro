"""SVG generation for metro maps using drawsvg."""

from __future__ import annotations

__all__ = ["apply_route_offsets", "render_svg"]

import html
import textwrap
from pathlib import Path

import drawsvg as draw

from nf_metro.layout.constants import LABEL_LINE_HEIGHT
from nf_metro.layout.labels import LabelPlacement, place_labels
from nf_metro.layout.routing import RoutedPath, compute_station_offsets, route_edges
from nf_metro.layout.routing.corners import resolve_curve_radii
from nf_metro.parser.model import (
    ICON_TYPE_DIR,
    ICON_TYPE_FILE,
    ICON_TYPE_FILES,
    MetroGraph,
    PortSide,
    Section,
    Station,
)
from nf_metro.render.constants import (
    CANVAS_PADDING,
    DEBUG_DIAMOND_RADIUS,
    DEBUG_ENTRY_PORT_COLOR,
    DEBUG_EXIT_PORT_COLOR,
    DEBUG_FONT_SIZE,
    DEBUG_HIDDEN_LABEL_OFFSET,
    DEBUG_HIDDEN_STATION_COLOR,
    DEBUG_LABEL_OFFSET,
    DEBUG_ROW_GRID_COLOR,
    DEBUG_STROKE_WIDTH,
    DEBUG_WAYPOINT_COLOR,
    DEBUG_WAYPOINT_RADIUS,
    FALLBACK_LINE_COLOR,
    FILES_ICON_OFFSET_RATIO,
    ICON_BBOX_MARGIN,
    ICON_CLEARANCE_MARGIN,
    ICON_INTER_GAP,
    ICON_NAME_FONT_SCALE,
    ICON_NAME_GAP,
    ICON_STATION_GAP,
    LEGEND_GAP,
    LEGEND_INSET,
    LOGO_Y_STANDALONE,
    SECTION_BOX_RADIUS,
    SECTION_LABEL_REGION_RATIO,
    SECTION_LABEL_TEXT_OFFSET,
    SECTION_NUM_CIRCLE_R_LARGE,
    SECTION_NUM_FONT_SIZE,
    SECTION_NUM_Y_OFFSET,
    SECTION_STROKE_WIDTH,
    SVG_CURVE_RADIUS,
    TERMINUS_FONT_COLOR,
    TEXT_VCENTER_DY,
    TITLE_Y_OFFSET,
    WATERMARK_FONT_SIZE,
    WATERMARK_PADDING_RATIO,
    WATERMARK_Y_INSET,
    line_style_kwargs,
)
from nf_metro.render.icons import (
    render_file_icon,
    render_files_icon,
    render_folder_icon,
)
from nf_metro.render.legend import compute_legend_dimensions, render_legend
from nf_metro.render.style import Theme


def apply_route_offsets(
    route: RoutedPath,
    station_offsets: dict[tuple[str, str], float],
) -> list[tuple[float, float]]:
    """Apply per-line Y offsets to a route's waypoints.

    If the route already has offsets applied (e.g. TB section routes),
    returns a copy of its points unchanged. Otherwise, shifts source-side
    waypoints by the source offset and target-side waypoints by the target
    offset, with intermediate points assigned to whichever end is closer.
    """
    if route.offsets_applied:
        return list(route.points)

    src_off = station_offsets.get((route.edge.source, route.line_id), 0.0)
    tgt_off = station_offsets.get((route.edge.target, route.line_id), 0.0)

    orig_sy = route.points[0][1]
    orig_ty = route.points[-1][1]
    pts: list[tuple[float, float]] = []
    for i, (x, y) in enumerate(route.points):
        if i == 0:
            pts.append((x, y + src_off))
        elif i == len(route.points) - 1:
            pts.append((x, y + tgt_off))
        elif abs(y - orig_sy) <= abs(y - orig_ty):
            pts.append((x, y + src_off))
        else:
            pts.append((x, y + tgt_off))
    return pts


def _compute_canvas_bounds(
    graph: MetroGraph,
    routes: list[RoutedPath],
    debug: bool = False,
) -> tuple[float, float]:
    """Compute max X/Y from stations, section boxes, and route waypoints."""
    if debug:
        visible_stations = list(graph.stations.values())
    else:
        visible_stations = [
            s for s in graph.stations.values() if not s.is_port and not s.is_hidden
        ]
    all_stations = (
        visible_stations if visible_stations else list(graph.stations.values())
    )

    max_x = max(s.x for s in all_stations)
    max_y = max(s.y for s in all_stations)

    for section in graph.sections.values():
        if section.bbox_w > 0:
            max_x = max(max_x, section.bbox_x + section.bbox_w)
            max_y = max(max_y, section.bbox_y + section.bbox_h)

    for route in routes:
        for px, py in route.points:
            if px > max_x:
                max_x = px
            if py > max_y:
                max_y = py

    return max_x, max_y


def _position_legend(
    graph: MetroGraph,
    theme: Theme,
    max_x: float,
    max_y: float,
    padding: float,
    logo_in_legend: bool,
    logo_w: float,
    logo_h: float,
    legend_position: str,
) -> tuple[float, float, float, float, bool]:
    """Compute legend position and dimensions.

    Returns (legend_x, legend_y, legend_w, legend_h, show_legend).
    """
    legend_logo_size = (logo_w, logo_h) if logo_in_legend else None
    legend_w, legend_h = compute_legend_dimensions(
        graph, theme, logo_size=legend_logo_size
    )
    show_legend = legend_position != "none" and legend_w > 0
    legend_x = 0.0
    legend_y = 0.0

    if not show_legend:
        return legend_x, legend_y, legend_w, legend_h, show_legend

    pos = legend_position
    gap = LEGEND_GAP
    inset = LEGEND_INSET
    content_left = min(
        (s.bbox_x for s in graph.sections.values() if s.bbox_w > 0),
        default=padding,
    )
    content_right = max_x
    content_top = min(
        (s.bbox_y for s in graph.sections.values() if s.bbox_w > 0),
        default=padding,
    )
    content_bottom = max_y

    if pos == "bl":
        legend_x = content_left
        legend_y = content_bottom - legend_h
    elif pos == "br":
        legend_x = content_right - legend_w - inset
        legend_y = content_bottom - legend_h - inset
    elif pos == "tl":
        legend_x = content_left + inset
        legend_y = content_top + inset
    elif pos == "tr":
        legend_x = content_right - legend_w - inset
        legend_y = content_top + inset
    elif pos == "bottom":
        legend_x = content_left
        legend_y = content_bottom + gap
    elif pos == "right":
        legend_x = content_right + gap
        legend_y = content_top

    if pos not in ("bottom", "right") and _legend_overlaps_sections(
        legend_x, legend_y, legend_w, legend_h, graph
    ):
        legend_x = content_left
        legend_y = content_bottom + gap

    return legend_x, legend_y, legend_w, legend_h, show_legend


def _compute_icon_obstacles(
    graph: MetroGraph,
    theme: Theme,
    station_offsets: dict[tuple[str, str], float],
) -> list[tuple[float, float, float, float]]:
    """Compute bounding boxes for terminus file icons.

    These are passed to the label placer as obstacles so labels maintain
    clearance from adjacent icons.
    """
    obstacles: list[tuple[float, float, float, float]] = []
    margin = ICON_CLEARANCE_MARGIN

    for station in graph.stations.values():
        if not station.is_terminus or not station.terminus_labels:
            continue

        r = theme.station_radius

        # Determine if source (no incoming edges) or sink
        is_source = not graph.edges_to(station.id)

        section = graph.sections.get(station.section_id) if station.section_id else None
        section_dir = section.direction if section else "LR"

        if section_dir == "RL":
            icons_go_right = is_source
        else:
            icons_go_right = not is_source

        n = len(station.terminus_labels)
        icon_gap = r + ICON_STATION_GAP
        total_w = n * theme.terminus_width + (n - 1) * ICON_INTER_GAP

        # Stacked-files icons extend beyond nominal size by the offset
        has_stacked = ICON_TYPE_FILES in (station.terminus_icon_types or [])
        stacked_pad = (
            theme.terminus_width * FILES_ICON_OFFSET_RATIO if has_stacked else 0.0
        )

        line_offs = [
            station_offsets.get((station.id, lid), 0.0)
            for lid in graph.station_lines(station.id)
        ]
        min_off = min(line_offs) if line_offs else 0.0
        max_off = max(line_offs) if line_offs else 0.0
        icon_cy = station.y + (min_off + max_off) / 2

        if icons_go_right:
            x_min = station.x + icon_gap - stacked_pad
            x_max = x_min + total_w + 2 * stacked_pad
        else:
            x_max = station.x - icon_gap + stacked_pad
            x_min = x_max - total_w - 2 * stacked_pad

        y_min = icon_cy - theme.terminus_height / 2 - stacked_pad
        y_max = icon_cy + theme.terminus_height / 2 + stacked_pad

        # Extend the obstacle downward to cover any caption text rendered
        # below the icon, so neighbouring labels keep their distance.
        if any(station.terminus_names or []):
            y_max += ICON_NAME_GAP + theme.label_font_size * ICON_NAME_FONT_SCALE

        obstacles.append(
            (
                x_min - margin,
                y_min - margin,
                x_max + margin,
                y_max + margin,
            )
        )

    return obstacles


def render_svg(
    graph: MetroGraph,
    theme: Theme,
    width: int | None = None,
    height: int | None = None,
    padding: float = CANVAS_PADDING,
    animate: bool = False,
    debug: bool = False,
    legend_position: str | None = None,
) -> str:
    """Render a metro map graph to an SVG string.

    If ``legend_position`` is given it overrides ``graph.legend_position``
    for this render only, without mutating the graph.
    """
    if not graph.stations:
        return '<svg xmlns="http://www.w3.org/2000/svg"></svg>'

    effective_legend_position = (
        legend_position if legend_position is not None else graph.legend_position
    )

    station_offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=station_offsets)

    # Compute labels early so section bbox expansions are applied
    # before section boxes are drawn and canvas bounds are computed.
    icon_obstacles = _compute_icon_obstacles(graph, theme, station_offsets)
    labels = place_labels(
        graph,
        station_offsets=station_offsets,
        icon_obstacles=icon_obstacles,
        routes=routes,
    )

    max_x, max_y = _compute_canvas_bounds(graph, routes, debug)

    # Compute legend and logo dimensions
    logo_w, logo_h = (0.0, 0.0)
    show_logo = graph.logo_path and Path(graph.logo_path).is_file()
    if show_logo:
        logo_w, logo_h = compute_logo_dimensions(graph.logo_path)

    logo_in_legend = show_logo and effective_legend_position != "none"
    legend_logo_size = (logo_w, logo_h) if logo_in_legend else None

    legend_x, legend_y, legend_w, legend_h, show_legend = _position_legend(
        graph,
        theme,
        max_x,
        max_y,
        padding,
        logo_in_legend,
        logo_w,
        logo_h,
        effective_legend_position,
    )

    if show_legend:
        max_x = max(max_x, legend_x + legend_w)
        max_y = max(max_y, legend_y + legend_h)

    # Standalone logo positioning (only when no legend to embed it in)
    logo_x = 0.0
    logo_y = 0.0
    if show_logo and not show_legend:
        logo_x = padding
        logo_y = LOGO_Y_STANDALONE
        max_x = max(max_x, logo_x + logo_w)

    # Right margin: use one padding width (content origin already provides
    # the left margin).  Bottom margin: just enough room for the watermark
    # text below the last content element.
    auto_width = max_x + padding
    auto_height = max_y + WATERMARK_Y_INSET * 2 + WATERMARK_FONT_SIZE

    svg_width = width or int(auto_width)
    svg_height = height or int(auto_height)

    d = draw.Drawing(svg_width, svg_height)

    # Dark-mode CSS for transparent-background themes so that elements
    # rendered directly on the canvas (section labels, number badges,
    # title) remain readable when the browser provides a dark background.
    if not theme.background_color or theme.background_color == "none":
        _inject_dark_mode_style(d)

    # Background (skip for transparent themes)
    if theme.background_color and theme.background_color != "none":
        d.append(
            draw.Rectangle(0, 0, svg_width, svg_height, fill=theme.background_color)
        )

    # Title / Logo (standalone logo only when not embedded in legend)
    if show_logo and not logo_in_legend:
        _render_logo(d, graph.logo_path, logo_x, logo_y, logo_w, logo_h)
    elif graph.title and not logo_in_legend:
        d.append(
            draw.Text(
                graph.title,
                theme.title_font_size,
                padding,
                TITLE_Y_OFFSET,
                fill=theme.title_color,
                font_family=theme.label_font_family,
                font_weight="bold",
                **{"class": "nf-metro-title"},
            )
        )

    # Sections
    if graph.sections:
        _render_first_class_sections(d, graph, theme)

    # Draw edges (lines) behind stations
    _render_edges(d, graph, routes, station_offsets, theme)

    # Animation (after edges, before stations so balls travel behind station markers)
    if animate:
        from nf_metro.render.animate import render_animation

        render_animation(d, graph, routes, station_offsets, theme)

    # Draw stations (all circles, skip ports)
    _render_stations(d, graph, theme, station_offsets)

    # Draw labels
    _render_labels(d, labels, theme)

    # Debug overlay (ports, hidden stations, edge waypoints)
    if debug:
        _render_debug_overlay(d, graph, routes, station_offsets, theme)

    # Legend (with embedded logo if present)
    if show_legend:
        render_legend(
            d,
            graph,
            theme,
            legend_x,
            legend_y,
            logo_path=graph.logo_path if logo_in_legend else None,
            logo_size=legend_logo_size,
        )

    # Attribution watermark
    d.append(
        draw.Text(
            f"created with nf-metro {_version_string()}",
            WATERMARK_FONT_SIZE,
            svg_width - padding * WATERMARK_PADDING_RATIO,
            svg_height - WATERMARK_Y_INSET,
            fill="rgba(150, 150, 150, 0.6)",
            font_family=theme.label_font_family,
            text_anchor="end",
        )
    )

    return d.as_svg()


def _legend_overlaps_sections(
    lx: float, ly: float, lw: float, lh: float, graph: MetroGraph
) -> bool:
    """Check if a legend rectangle overlaps any section bounding box."""
    for section in graph.sections.values():
        if section.bbox_w <= 0:
            continue
        if (
            lx < section.bbox_x + section.bbox_w
            and lx + lw > section.bbox_x
            and ly < section.bbox_y + section.bbox_h
            and ly + lh > section.bbox_y
        ):
            return True
    return False


def _version_string() -> str:
    """Return version string, appending '+dev' for editable/non-release installs."""
    from nf_metro import __version__

    try:
        import importlib.metadata
        import json

        dist = importlib.metadata.distribution("nf-metro")
        direct_url = dist.read_text("direct_url.json")
        if direct_url:
            data = json.loads(direct_url)
            if data.get("dir_info", {}).get("editable"):
                return f"v{__version__}+dev"
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        importlib.metadata.PackageNotFoundError,
    ):
        pass
    return f"v{__version__}"


def compute_logo_dimensions(
    logo_path: str,
    logo_height: float = 80.0,
) -> tuple[float, float]:
    """Compute logo display dimensions preserving aspect ratio."""
    from PIL import Image as PILImage

    img = PILImage.open(logo_path)
    aspect = img.width / img.height
    return logo_height * aspect, logo_height


def _render_logo(
    d: draw.Drawing,
    logo_path: str,
    x: float,
    y: float,
    logo_w: float,
    logo_h: float,
) -> None:
    """Embed a logo image at the given position."""
    d.append(
        draw.Image(
            x,
            y,
            logo_w,
            logo_h,
            path=logo_path,
            embed=True,
        )
    )


def _inject_dark_mode_style(d: draw.Drawing) -> None:
    """Inject CSS for dark-mode browsers viewing a transparent-background SVG.

    When the SVG has no opaque background, elements rendered directly on the
    canvas (section labels, numbered badges, title) can become invisible if the
    browser supplies a dark page background.  A ``prefers-color-scheme: dark``
    media query adjusts those elements so they remain readable.  CSS rules
    override SVG presentation attributes, so we only need class selectors.
    """
    css = textwrap.dedent("""\
        @media (prefers-color-scheme: dark) {
            .nf-metro-section-label { fill: #d0d0d0; }
            .nf-metro-section-num-circle { fill: #777777; }
            .nf-metro-title { fill: #ffffff; }
        }
    """)
    d.append(draw.Raw(f"<style>{css}</style>"))


def _render_first_class_sections(
    d: draw.Drawing,
    graph: MetroGraph,
    theme: Theme,
) -> None:
    """Render first-class sections using pre-computed bounding boxes."""
    for section in graph.sections.values():
        if section.bbox_w <= 0 or section.bbox_h <= 0:
            continue
        if section.is_implicit:
            continue

        section_lines: set[str] = set()
        for sid in section.station_ids:
            section_lines.update(graph.station_lines(sid))
        section_data = {
            "data-section-id": section.id,
            "data-section-lines": ",".join(sorted(section_lines)),
        }

        d.append(
            draw.Rectangle(
                section.bbox_x,
                section.bbox_y,
                section.bbox_w,
                section.bbox_h,
                rx=SECTION_BOX_RADIUS,
                ry=SECTION_BOX_RADIUS,
                fill=theme.section_fill,
                stroke=theme.section_stroke,
                stroke_width=SECTION_STROKE_WIDTH,
                class_="nf-metro-section-box",
                **section_data,
            )
        )

        # Determine whether the label should go below the section box.
        # When a section has a TOP entry port near the left edge (where the
        # label sits) but no BOTTOM exit, placing the label above would
        # overlap with incoming lines.  Only flip when the nearest top port
        # is close enough to actually conflict with the label.
        label_region_max_x = (
            section.bbox_x + section.bbox_w * SECTION_LABEL_REGION_RATIO
        )
        has_nearby_top_entry = any(
            graph.ports.get(pid)
            and graph.ports[pid].side == PortSide.TOP
            and graph.ports[pid].x <= label_region_max_x
            for pid in section.entry_ports
        )
        has_bottom_exit = any(
            graph.ports.get(pid) and graph.ports[pid].side == PortSide.BOTTOM
            for pid in section.exit_ports
        )
        label_below = has_nearby_top_entry and not has_bottom_exit

        # Numbered circle, left-aligned
        circle_r = SECTION_NUM_CIRCLE_R_LARGE
        cx = section.bbox_x + circle_r
        if label_below:
            cy = section.bbox_y + section.bbox_h + circle_r + SECTION_NUM_Y_OFFSET
        else:
            cy = section.bbox_y - circle_r - SECTION_NUM_Y_OFFSET

        d.append(
            draw.Circle(
                cx,
                cy,
                circle_r,
                fill=theme.station_stroke,
                **{
                    "class": "nf-metro-section-num-circle",
                    "data-section-id": section.id,
                },
            )
        )
        d.append(
            draw.Text(
                str(section.number),
                SECTION_NUM_FONT_SIZE,
                cx,
                cy,
                fill=theme.station_fill,
                font_family=theme.label_font_family,
                font_weight="bold",
                text_anchor="middle",
                dy=TEXT_VCENTER_DY,
                **{"data-section-id": section.id},
            )
        )

        # Section name to the right of the circle
        d.append(
            draw.Text(
                section.name,
                theme.section_label_font_size,
                cx + circle_r + SECTION_LABEL_TEXT_OFFSET,
                cy,
                fill=theme.section_label_color,
                font_family=theme.label_font_family,
                font_weight="bold",
                dy=TEXT_VCENTER_DY,
                **{"class": "nf-metro-section-label", "data-section-id": section.id},
            )
        )


def _render_edges(
    d: draw.Drawing,
    graph: MetroGraph,
    routes: list[RoutedPath],
    station_offsets: dict[tuple[str, str], float],
    theme: Theme,
    curve_radius: float = SVG_CURVE_RADIUS,
) -> None:
    """Render metro line edges with smooth curves at direction changes."""

    # Group routes by metro line so each line's paths are contiguous in
    # document order, then any two lines have the same relative paint order
    # at every overlap.  Reverse-of-definition order makes the first-defined
    # line paint last (on top everywhere).  Unknown line_ids sort to the
    # back (painted last); Python's stable sort preserves within-group order.
    line_priority = {lid: i for i, lid in enumerate(graph.lines)}
    routes = sorted(routes, key=lambda r: -line_priority.get(r.line_id, -1))

    for route in routes:
        line = graph.lines.get(route.line_id)
        color = line.color if line else FALLBACK_LINE_COLOR
        style_kw = line_style_kwargs(line.style) if line else {}
        class_name = f"metro-line-{route.line_id}"

        pts = apply_route_offsets(route, station_offsets)

        if len(pts) == 2:
            d.append(
                draw.Line(
                    pts[0][0],
                    pts[0][1],
                    pts[1][0],
                    pts[1][1],
                    stroke=color,
                    stroke_width=theme.line_width,
                    stroke_linecap="round",
                    class_=class_name,
                    **{"data-line-id": route.line_id},
                    **style_kw,
                )
            )
        elif len(pts) >= 3:
            path = draw.Path(
                stroke=color,
                stroke_width=theme.line_width,
                fill="none",
                stroke_linecap="round",
                stroke_linejoin="round",
                class_=class_name,
                **{"data-line-id": route.line_id},
                **style_kw,
            )
            path.M(*pts[0])

            resolved = resolve_curve_radii(
                pts, route.curve_radii, default_radius=curve_radius
            )

            for i in range(1, len(pts) - 1):
                prev = pts[i - 1]
                curr = pts[i]
                nxt = pts[i + 1]

                dx1 = curr[0] - prev[0]
                dy1 = curr[1] - prev[1]
                len1 = (dx1**2 + dy1**2) ** 0.5

                dx2 = nxt[0] - curr[0]
                dy2 = nxt[1] - curr[1]
                len2 = (dx2**2 + dy2**2) ** 0.5

                r = resolved[i - 1]

                if len1 > 0 and len2 > 0:
                    before_x = curr[0] - (dx1 / len1) * r
                    before_y = curr[1] - (dy1 / len1) * r
                    after_x = curr[0] + (dx2 / len2) * r
                    after_y = curr[1] + (dy2 / len2) * r

                    path.L(before_x, before_y)
                    path.Q(curr[0], curr[1], after_x, after_y)
                else:
                    path.L(*curr)

            path.L(*pts[-1])
            d.append(path)


def _render_stations(
    d: draw.Drawing,
    graph: MetroGraph,
    theme: Theme,
    station_offsets: dict[tuple[str, str], float] | None = None,
) -> None:
    """Render stations as pill shapes.

    Normal stations get vertical pills (tall, narrow). Stations in a TB
    section get horizontal pills (wide, short) since the lines run
    vertically through them.

    Skips port stations (is_port=True).
    """
    for station in graph.stations.values():
        if station.is_port or station.is_hidden:
            continue

        r = theme.station_radius

        # Determine if this is a TB vertical station (rotated pill)
        is_tb_vert = False
        if station.section_id:
            sec = graph.sections.get(station.section_id)
            if sec and sec.direction == "TB":
                is_tb_vert = True

        if station_offsets:
            line_offsets = [
                station_offsets.get((station.id, lid), 0.0)
                for lid in graph.station_lines(station.id)
            ]
            if line_offsets:
                min_off = min(line_offsets)
                max_off = max(line_offsets)
            else:
                min_off = max_off = 0.0
        else:
            min_off = max_off = 0.0

        span = max_off - min_off

        # Hand-escape values that flow from user content into XML attributes.
        # drawsvg does not escape unknown kwargs, so an unescaped "&" or "<"
        # in a section name or station label breaks XML well-formedness.
        station_data = {
            "class_": "nf-metro-station",
            "data-station-id": station.id,
            "data-station-lines": ",".join(graph.station_lines(station.id)),
            "data-station-label": html.escape(station.label or station.id),
        }
        if station.section_id:
            station_data["data-section-id"] = station.section_id
            sec_obj = graph.sections.get(station.section_id)
            if sec_obj:
                station_data["data-section-name"] = html.escape(sec_obj.name)

        # Non-process terminus stations: filled rectangle
        # (same size as pill, no rounding)
        is_blank_terminus = station.is_terminus and not station.label.strip()
        if is_blank_terminus:
            w = r * 2
            h = span + r * 2
            cy = station.y + (min_off + max_off) / 2
            d.append(
                draw.Rectangle(
                    station.x - w / 2,
                    cy - h / 2,
                    w,
                    h,
                    fill=theme.station_fill,
                    stroke=theme.station_stroke,
                    stroke_width=theme.station_stroke_width,
                    **station_data,
                )
            )
        elif is_tb_vert:
            # Horizontal pill: lines spread along X axis
            w = span + r * 2
            h = r * 2
            cx = station.x + (min_off + max_off) / 2
            d.append(
                draw.Rectangle(
                    cx - w / 2,
                    station.y - h / 2,
                    w,
                    h,
                    rx=r,
                    ry=r,
                    fill=theme.station_fill,
                    stroke=theme.station_stroke,
                    stroke_width=theme.station_stroke_width,
                    **station_data,
                )
            )
        else:
            # Vertical pill: lines spread along Y axis
            w = r * 2
            h = span + r * 2
            cy = station.y + (min_off + max_off) / 2
            d.append(
                draw.Rectangle(
                    station.x - w / 2,
                    cy - h / 2,
                    w,
                    h,
                    rx=r,
                    ry=r,
                    fill=theme.station_fill,
                    stroke=theme.station_stroke,
                    stroke_width=theme.station_stroke_width,
                    **station_data,
                )
            )

        if station.is_terminus:
            icon_group = draw.Group(**{"data-station-id": station.id})
            _render_terminus_icons(
                icon_group, station, graph, theme, r, min_off, max_off
            )
            d.append(icon_group)


def caption_aware_icon_step(
    names: list[str],
    name_widths: list[float],
    terminus_width: float,
) -> float:
    """Return the horizontal centre-to-centre step for adjacent icons.

    The default step is ``terminus_width + ICON_INTER_GAP``.  When two
    adjacent icons both carry a caption whose estimated width would
    overrun that step (causing captions to overlap on the same row),
    widen the step so the wider of the two captions fits with a small
    visual gap on each side.  The widened step is shared by every icon
    in the row, keeping spacing uniform.
    """
    default_step = terminus_width + ICON_INTER_GAP
    required = default_step
    for i in range(len(names) - 1):
        if not names[i] or not names[i + 1]:
            continue
        pair_max = max(name_widths[i], name_widths[i + 1])
        # Allow a small gap each side of the wider caption before its
        # neighbour caption starts.  ICON_INTER_GAP gives us a uniform
        # min visual breathing room.
        needed = pair_max + ICON_INTER_GAP
        if needed > required:
            required = needed
    return required


def _render_terminus_icons(
    d: draw.Drawing,
    station: Station,
    graph: MetroGraph,
    theme: Theme,
    r: float,
    min_off: float,
    max_off: float,
) -> None:
    """Render file icon(s) adjacent to a terminus station.

    Multiple icons are arranged in a horizontal row, extending away from
    the station (i.e. the first icon is closest to the station pill).
    """
    section: Section | None = (
        graph.sections.get(station.section_id) if station.section_id else None
    )
    # Detect if station is a source (no incoming edges) or sink.
    is_source = not graph.edges_to(station.id)
    # Place icons on the "outside" of the flow
    icon_gap = r + ICON_STATION_GAP
    icon_half_w = theme.terminus_width / 2
    section_dir = section.direction if section else "LR"

    # Determine direction: icons extend leftward for sources (LR/TB)
    # or rightward for sinks, inverted for RL.
    if section_dir == "RL":
        icons_go_right = is_source
    else:
        icons_go_right = not is_source

    icon_cy = station.y + (min_off + max_off) / 2

    icon_types = station.terminus_icon_types or [ICON_TYPE_FILE] * len(
        station.terminus_labels
    )
    names = station.terminus_names or [""] * len(station.terminus_labels)

    # Compute per-icon X step.  When adjacent captions would overlap
    # at the default step, widen it so both fit on the same row.
    caption_font_size = theme.label_font_size * ICON_NAME_FONT_SCALE
    name_widths = [len(n) * caption_font_size * 0.55 if n else 0.0 for n in names]
    icon_step = caption_aware_icon_step(names, name_widths, theme.terminus_width)

    # Base X for the first (nearest) icon center
    if icons_go_right:
        base_cx = station.x + icon_gap + icon_half_w
    else:
        base_cx = station.x - icon_gap - icon_half_w

    # Captions sitting at the same Y overlap when their estimated
    # widths exceed icon_step; in that case, every other caption is
    # dropped to the next row.
    stagger_captions = False
    for i in range(len(names) - 1):
        if not names[i] or not names[i + 1]:
            continue
        max_w = max(name_widths[i], name_widths[i + 1])
        if max_w > icon_step - 2.0:
            stagger_captions = True
            break

    for i, label in enumerate(station.terminus_labels):
        icon_type = icon_types[i] if i < len(icon_types) else ICON_TYPE_FILE
        name = names[i] if i < len(names) else ""

        if icons_go_right:
            icon_cx = base_cx + i * icon_step
        else:
            icon_cx = base_cx - i * icon_step

        # Clamp to stay within section bbox
        if section and section.bbox_w > 0:
            icon_right = (
                section.bbox_x + section.bbox_w - icon_half_w - ICON_BBOX_MARGIN
            )
            icon_cx = max(
                section.bbox_x + icon_half_w + ICON_BBOX_MARGIN,
                min(icon_cx, icon_right),
            )

        common = dict(
            cx=icon_cx,
            cy=icon_cy,
            width=theme.terminus_width,
            height=theme.terminus_height,
            fill=theme.terminus_fill or theme.station_fill,
            stroke=theme.terminus_stroke or theme.station_stroke,
            stroke_width=theme.terminus_stroke_width,
            corner_radius=theme.terminus_corner_radius,
            label=label,
            font_size=theme.terminus_font_size,
            font_color=TERMINUS_FONT_COLOR,
            font_family=theme.label_font_family,
        )

        if icon_type == ICON_TYPE_DIR:
            render_folder_icon(d, **common)
        elif icon_type == ICON_TYPE_FILES:
            render_files_icon(d, **common, fold_size=theme.terminus_fold_size)
        else:
            render_file_icon(d, **common, fold_size=theme.terminus_fold_size)

        # Optional caption rendered below the icon so the type chip
        # inside the icon stays readable.
        if name:
            caption_y = icon_cy + theme.terminus_height / 2 + ICON_NAME_GAP
            # When adjacent icon captions would overlap horizontally
            # (their estimated width exceeds the per-icon X step), drop
            # odd-indexed captions to a second row so each name is
            # legible.
            if stagger_captions and i % 2 == 1:
                caption_y += caption_font_size * 1.4
            caption_cx = icon_cx
            if section and section.bbox_w > 0:
                # Estimate caption width and clamp so it stays inside the
                # section bbox right edge (and left edge for symmetry).
                approx_w = len(name) * caption_font_size * 0.55
                left_bound = section.bbox_x + approx_w / 2 + ICON_BBOX_MARGIN
                right_bound = (
                    section.bbox_x + section.bbox_w - approx_w / 2 - ICON_BBOX_MARGIN
                )
                if right_bound > left_bound:
                    caption_cx = max(left_bound, min(caption_cx, right_bound))
            d.append(
                draw.Text(
                    name,
                    caption_font_size,
                    caption_cx,
                    caption_y,
                    fill=theme.label_color,
                    font_family=theme.label_font_family,
                    font_weight=theme.label_font_weight,
                    text_anchor="middle",
                    dominant_baseline="hanging",
                )
            )


def _render_labels(
    d: draw.Drawing,
    labels: list[LabelPlacement],
    theme: Theme,
) -> None:
    """Render station name labels."""
    for label in labels:
        text = label.text
        n_lines = text.count("\n") + 1

        # For multi-line labels, adjust y so the text block stays on
        # the correct side of the station.
        y = label.y
        if n_lines > 1:
            line_spacing = theme.label_font_size * LABEL_LINE_HEIGHT
            if label.dominant_baseline == "central":
                # Center the block vertically on y
                y -= (n_lines - 1) * line_spacing / 2
            elif label.above:
                # Keep the bottom line near the station
                y -= (n_lines - 1) * line_spacing

        # Skip emitting data-station-id for synthetic obstacle placements.
        label_data: dict[str, str] = {}
        if label.station_id and not label.station_id.startswith("__"):
            label_data["data-station-id"] = label.station_id
            label_data["class_"] = "nf-metro-station-label"

        if label.dominant_baseline:
            # Custom placement (e.g. TB vertical stations: right-side labels)
            d.append(
                draw.Text(
                    text,
                    theme.label_font_size,
                    label.x,
                    y,
                    fill=theme.label_color,
                    font_family=theme.label_font_family,
                    font_weight=theme.label_font_weight,
                    text_anchor=label.text_anchor,
                    dominant_baseline=label.dominant_baseline,
                    line_height=LABEL_LINE_HEIGHT,
                    **label_data,
                )
            )
        else:
            baseline = "auto" if label.above else "hanging"
            d.append(
                draw.Text(
                    text,
                    theme.label_font_size,
                    label.x,
                    y,
                    fill=theme.label_color,
                    font_family=theme.label_font_family,
                    font_weight=theme.label_font_weight,
                    text_anchor="middle",
                    dominant_baseline=baseline,
                    line_height=LABEL_LINE_HEIGHT,
                    **label_data,
                )
            )


def _grid_bbox_bounds(
    sections: list[Section],
) -> tuple[dict[int, tuple[float, float]], dict[int, tuple[float, float]]]:
    """Collect per-column X bounds and per-row Y bounds from section bboxes.

    Spanning sections (``grid_col_span > 1`` or ``grid_row_span > 1``) are
    excluded from the corresponding axis - their extent across multiple
    cells would distort a single-cell measurement.
    """
    col_bounds: dict[int, tuple[float, float]] = {}
    row_bounds: dict[int, tuple[float, float]] = {}
    for sec in sections:
        c, r = sec.grid_col, sec.grid_row
        x0, x1 = sec.bbox_x, sec.bbox_x + sec.bbox_w
        y0, y1 = sec.bbox_y, sec.bbox_y + sec.bbox_h
        if sec.grid_col_span == 1:
            if c not in col_bounds:
                col_bounds[c] = (x0, x1)
            else:
                col_bounds[c] = (
                    min(col_bounds[c][0], x0),
                    max(col_bounds[c][1], x1),
                )
        if sec.grid_row_span == 1:
            if r not in row_bounds:
                row_bounds[r] = (y0, y1)
            else:
                row_bounds[r] = (
                    min(row_bounds[r][0], y0),
                    max(row_bounds[r][1], y1),
                )
    return col_bounds, row_bounds


def _sections_by_grid_cell(
    sections: list[Section],
) -> dict[tuple[int, int], Section]:
    """Index non-spanning sections by ``(grid_col, grid_row)``."""
    out: dict[tuple[int, int], Section] = {}
    for sec in sections:
        if sec.grid_col_span == 1 and sec.grid_row_span == 1:
            out[(sec.grid_col, sec.grid_row)] = sec
    return out


def _compute_row_boundary_segments(
    sections: list[Section],
    col_bounds: dict[int, tuple[float, float]],
) -> list[tuple[int, int, float, float, float]]:
    """Return per-column row-boundary segments as
    ``(row_a, row_b, x_start, x_end, y)`` tuples.

    For each pair of consecutive grid rows and each column where both
    rows have a non-spanning section, emit a horizontal segment at the
    local midpoint between the upper section's bottom and the lower
    section's top.  If a fold extends the upper section's bbox past
    the lower section's top (local overlap), no segment is emitted for
    that column - the row boundary visibly breaks at the fold column
    instead of being plotted at a Y that would cut through bboxes.
    """
    sec_by_cell = _sections_by_grid_cell(sections)
    rows = sorted({r for _, r in sec_by_cell})
    segments: list[tuple[int, int, float, float, float]] = []
    for i in range(len(rows) - 1):
        ra, rb = rows[i], rows[i + 1]
        for c, (x_start, x_end) in col_bounds.items():
            sa = sec_by_cell.get((c, ra))
            sb = sec_by_cell.get((c, rb))
            if sa is None or sb is None:
                continue
            a_bot = sa.bbox_y + sa.bbox_h
            b_top = sb.bbox_y
            if a_bot >= b_top:
                continue
            segments.append((ra, rb, x_start, x_end, (a_bot + b_top) / 2))
    return segments


def _compute_col_boundary_segments(
    sections: list[Section],
    row_bounds: dict[int, tuple[float, float]],
) -> list[tuple[int, int, float, float, float]]:
    """Return per-row column-boundary segments as
    ``(col_a, col_b, y_start, y_end, x)`` tuples.

    Mirrors :func:`_compute_row_boundary_segments` along the other
    axis - per-row vertical segments at the local midpoint between
    horizontally adjacent sections, skipped where bboxes locally
    overlap in X.
    """
    sec_by_cell = _sections_by_grid_cell(sections)
    cols = sorted({c for c, _ in sec_by_cell})
    segments: list[tuple[int, int, float, float, float]] = []
    for j in range(len(cols) - 1):
        ca, cb = cols[j], cols[j + 1]
        for r, (y_start, y_end) in row_bounds.items():
            sa = sec_by_cell.get((ca, r))
            sb = sec_by_cell.get((cb, r))
            if sa is None or sb is None:
                continue
            a_right = sa.bbox_x + sa.bbox_w
            b_left = sb.bbox_x
            if a_right >= b_left:
                continue
            segments.append((ca, cb, y_start, y_end, (a_right + b_left) / 2))
    return segments


def _render_debug_overlay(
    d: draw.Drawing,
    graph: MetroGraph,
    routes: list[RoutedPath],
    station_offsets: dict[tuple[str, str], float],
    theme: Theme,
) -> None:
    """Render debug markers for ports, hidden stations, and edge waypoints."""
    debug_font = theme.label_font_family
    debug_font_size = DEBUG_FONT_SIZE

    # Edge waypoints: small filled circles at intermediate points
    for route in routes:
        if len(route.points) <= 2:
            continue
        pts = apply_route_offsets(route, station_offsets)
        # Draw intermediate waypoints (skip first/last which are at stations)
        for px, py in pts[1:-1]:
            d.append(
                draw.Circle(px, py, DEBUG_WAYPOINT_RADIUS, fill=DEBUG_WAYPOINT_COLOR)
            )

    # Port stations: diamond markers with labels
    for station in graph.stations.values():
        if not station.is_port:
            continue
        port = graph.ports.get(station.id)
        is_entry = port.is_entry if port else True
        color = DEBUG_ENTRY_PORT_COLOR if is_entry else DEBUG_EXIT_PORT_COLOR
        # Diamond (rotated square)
        r = DEBUG_DIAMOND_RADIUS
        diamond = draw.Path(fill=color, stroke="none")
        diamond.M(station.x, station.y - r)
        diamond.L(station.x + r, station.y)
        diamond.L(station.x, station.y + r)
        diamond.L(station.x - r, station.y)
        diamond.Z()
        d.append(diamond)
        # Label: port ID + side
        side_str = port.side.value if port else "?"
        label_text = f"{station.id} ({side_str})"
        d.append(
            draw.Text(
                label_text,
                debug_font_size,
                station.x,
                station.y - r - DEBUG_LABEL_OFFSET,
                fill=color,
                font_family=debug_font,
                text_anchor="middle",
                dominant_baseline="auto",
            )
        )

    # Grid lines: dashed lines at boundaries between grid rows/columns
    sections = list(graph.sections.values())
    if sections:
        col_bounds, row_bounds = _grid_bbox_bounds(sections)

        # Label anchors (top-of-canvas for col labels, left for row labels).
        # All sections spanning means no single-cell bounds exist - fall
        # back to raw bboxes for label placement only.
        if not col_bounds or not row_bounds:
            all_x0 = min(s.bbox_x for s in sections) - 20
            all_y0 = min(s.bbox_y for s in sections) - 20
        else:
            all_x0 = min(b[0] for b in col_bounds.values()) - 20
            all_y0 = min(b[0] for b in row_bounds.values()) - 20
        grid_color = "rgba(255, 255, 0, 0.5)"

        labelled_col_pairs: set[tuple[int, int]] = set()
        for ca, cb, y_start, y_end, x in _compute_col_boundary_segments(
            sections, row_bounds
        ):
            d.append(
                draw.Line(
                    x,
                    y_start,
                    x,
                    y_end,
                    stroke=grid_color,
                    stroke_width=1,
                    stroke_dasharray="6,4",
                )
            )
            if (ca, cb) not in labelled_col_pairs:
                d.append(
                    draw.Text(
                        f"col {ca}|{cb}",
                        debug_font_size,
                        x,
                        all_y0 - 4,
                        fill=grid_color,
                        font_family=debug_font,
                        text_anchor="middle",
                    )
                )
                labelled_col_pairs.add((ca, cb))

        labelled_row_pairs: set[tuple[int, int]] = set()
        for ra, rb, x_start, x_end, y in _compute_row_boundary_segments(
            sections, col_bounds
        ):
            d.append(
                draw.Line(
                    x_start,
                    y,
                    x_end,
                    y,
                    stroke=grid_color,
                    stroke_width=1,
                    stroke_dasharray="6,4",
                )
            )
            if (ra, rb) not in labelled_row_pairs:
                d.append(
                    draw.Text(
                        f"row {ra}|{rb}",
                        debug_font_size,
                        all_x0 - 4,
                        y,
                        fill=grid_color,
                        font_family=debug_font,
                        text_anchor="end",
                    )
                )
                labelled_row_pairs.add((ra, rb))

    # Shared Y grid lines: horizontal lines at each grid slot position
    # within each row group (populated by _align_row_y_grids in engine.py).
    grid_info = graph._row_y_grid_info
    if grid_info and sections:
        for row, info in grid_info.items():
            slot_spacing = info["slot_spacing"]
            sec_ids = info["section_ids"]
            ref_secs = [graph.sections[sid] for sid in sec_ids if sid in graph.sections]
            if not ref_secs:
                continue
            # Collect all non-port station Y positions in the group
            # to determine the actual grid line range.
            all_station_ys: list[float] = []
            for sec in ref_secs:
                for sid in sec.station_ids:
                    st = graph.stations.get(sid)
                    if st and not st.is_port:
                        all_station_ys.append(st.y)
            if not all_station_ys:
                continue
            base_y = min(all_station_ys)
            max_y = max(all_station_ys)
            n_slots = (
                int(round((max_y - base_y) / slot_spacing)) + 1
                if slot_spacing > 0
                else 1
            )
            # X span: from leftmost to rightmost section in the group
            x_start = min(s.bbox_x for s in ref_secs) - 10
            x_end = max(s.bbox_x + s.bbox_w for s in ref_secs) + 10
            for i in range(n_slots):
                y = base_y + i * slot_spacing
                d.append(
                    draw.Line(
                        x_start,
                        y,
                        x_end,
                        y,
                        stroke=DEBUG_ROW_GRID_COLOR,
                        stroke_width=0.75,
                        stroke_dasharray="4,6",
                    )
                )
                if i == 0:
                    d.append(
                        draw.Text(
                            f"row {row} grid",
                            debug_font_size,
                            x_start - 4,
                            y,
                            fill=DEBUG_ROW_GRID_COLOR,
                            font_family=debug_font,
                            text_anchor="end",
                        )
                    )

    # Hidden stations: dashed-outline circles with labels
    for station in graph.stations.values():
        if not station.is_hidden or station.is_port:
            continue
        color = DEBUG_HIDDEN_STATION_COLOR
        d.append(
            draw.Circle(
                station.x,
                station.y,
                DEBUG_DIAMOND_RADIUS,
                fill="none",
                stroke=color,
                stroke_width=DEBUG_STROKE_WIDTH,
                stroke_dasharray="3,2",
            )
        )
        d.append(
            draw.Text(
                station.id,
                debug_font_size,
                station.x,
                station.y - DEBUG_HIDDEN_LABEL_OFFSET,
                fill=color,
                font_family=debug_font,
                text_anchor="middle",
                dominant_baseline="auto",
            )
        )
