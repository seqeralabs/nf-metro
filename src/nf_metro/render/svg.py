"""SVG generation for metro maps using drawsvg."""

from __future__ import annotations

__all__ = ["apply_route_offsets", "render_svg"]

import html
import math
import re
import textwrap
import warnings
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, NamedTuple

import drawsvg as draw

from nf_metro.layout.constants import (
    LABEL_LINE_HEIGHT,
    OFFTRACK_TERMINUS_NUB_CLEARANCE,
    SAME_COORD_TOLERANCE,
)
from nf_metro.layout.geometry import lanes_run_along_x, segment_intersects_bbox
from nf_metro.layout.labels import (
    LabelPlacement,
    _label_bbox,
    font_scale_context,
    place_labels,
)
from nf_metro.layout.phases.guards import (
    FoldThresholdError,
    assert_render_layout_invariants,
    iter_opposing_line_overlaps,
)
from nf_metro.layout.routing import (
    RoutedPath,
    apply_route_offsets,
    compute_station_offsets,
    route_edges_centred,
)
from nf_metro.layout.routing.corners import (
    curve_tangents,
    resolve_curve_radii,
)
from nf_metro.layout.routing.invariants import (
    CurveInvariantError,
    assert_render_curve_invariants,
)
from nf_metro.layout.routing.reversal import tb_positive_fan_sections
from nf_metro.manifest import node_data_attrs
from nf_metro.parser.model import (
    ICON_TYPE_DIR,
    ICON_TYPE_FILE,
    ICON_TYPE_FILES,
    MARKER_FILL_OPEN,
    MARKER_FILL_SOLID,
    MARKER_SHAPE_PILL,
    Interchange,
    MarkerStyle,
    MetroGraph,
    Section,
    Station,
)
from nf_metro.render.bridges import BridgeBreak, compute_bridges
from nf_metro.render.constants import (
    CANVAS_PADDING,
    CAPTION_FILL,
    CAPTION_FONT_SIZE,
    DEBUG_DIAMOND_RADIUS,
    DEBUG_ENTRY_PORT_COLOR,
    DEBUG_EXIT_PORT_COLOR,
    DEBUG_FONT_SIZE,
    DEBUG_GRID_COLOR,
    DEBUG_GRID_COLOR_LIGHT,
    DEBUG_HIDDEN_LABEL_OFFSET,
    DEBUG_HIDDEN_STATION_COLOR,
    DEBUG_LABEL_OFFSET,
    DEBUG_ROW_GRID_COLOR,
    DEBUG_ROW_GRID_COLOR_LIGHT,
    DEBUG_STROKE_WIDTH,
    DEBUG_WAYPOINT_COLOR,
    DEBUG_WAYPOINT_COLOR_LIGHT,
    DEBUG_WAYPOINT_RADIUS,
    FALLBACK_LINE_COLOR,
    FILES_ICON_OFFSET_RATIO,
    GROUP_LABEL_BAND_PADDING,
    GROUP_LABEL_FONT_SCALE,
    GROUP_LABEL_GAP,
    GROUP_LABEL_LABEL_CLEARANCE,
    GROUP_LABEL_TICK_LENGTH,
    GROUP_LABEL_UNDERLINE_GAP,
    GROUP_LABEL_UNDERLINE_OPACITY,
    GROUP_LABEL_UNDERLINE_WIDTH,
    ICON_BBOX_MARGIN,
    ICON_CLEARANCE_MARGIN,
    ICON_INTER_GAP,
    ICON_NAME_FONT_SCALE,
    ICON_NAME_GAP,
    ICON_STATION_GAP,
    LEGEND_GAP,
    LEGEND_INSET,
    LEGEND_ROUTE_CLEARANCE,
    LOGO_Y_STANDALONE,
    MARKER_PILL_LENGTH_RATIO,
    RAIL_KNOB_RADIUS_RATIO,
    RAIL_LINK_HALF_WIDTH_RATIO,
    SECTION_BOX_RADIUS,
    SECTION_HEADER_ROUTE_PAD,
    SECTION_NUM_CIRCLE_R_LARGE,
    SECTION_NUM_FONT_SIZE,
    SECTION_STROKE_WIDTH,
    SVG_CURVE_RADIUS,
    TERMINUS_FONT_COLOR,
    TEXT_VCENTER_DY,
    TITLE_Y_OFFSET,
    WATERMARK_BARE_X_INSET,
    WATERMARK_FILL,
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
from nf_metro.render.legend import (
    compute_legend_dimensions,
    marker_corner_radius,
    marker_fill_color,
    marker_stroke_color,
    render_legend,
)
from nf_metro.render.manifest import build_manifest, manifest_metadata_svg
from nf_metro.render.ns import adaptive_logo_mask_ids as _adaptive_logo_mask_ids
from nf_metro.render.ns import class_prefix_context
from nf_metro.render.ns import ns as _ns
from nf_metro.render.section_header import (
    SectionHeaderClashError,
    SectionHeaderPlacement,
    check_section_headers_clear_routes,
    resolve_all_section_headers,
)
from nf_metro.render.style import Theme


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
    routes: list[RoutedPath],
) -> tuple[float, float, float, float, bool]:
    """Compute legend position and dimensions.

    Returns (legend_x, legend_y, legend_w, legend_h, show_legend).

    ``legend_position`` is passed in because callers may override it per render
    (only ``"none"`` is overridden in practice); the placement modifiers
    (``legend_anchor``/``legend_offset``/``legend_at``) are read from ``graph``.

    A casual corner keyword auto-relocates to the bottom-left when it would
    overlap a section or a routed line. An explicit pin (canvas anchor, offset,
    or absolute coordinates) is honoured as placed, but warns on overlap.
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

    # Absolute placement (legend: x,y) pins the block top-left exactly.
    if graph.legend_at is not None:
        legend_x, legend_y = graph.legend_at
        if _legend_overlaps_content(
            legend_x, legend_y, legend_w, legend_h, graph, routes
        ):
            warnings.warn(
                f"legend placed at {graph.legend_at} overlaps a section or route.",
                stacklevel=2,
            )
        return legend_x, legend_y, legend_w, legend_h, show_legend

    pos = legend_position
    gap = LEGEND_GAP
    inset = LEGEND_INSET
    content_left = min(
        (s.bbox_x for s in graph.sections.values() if s.bbox_w > 0),
        default=padding,
    )
    content_top = min(
        (s.bbox_y for s in graph.sections.values() if s.bbox_w > 0),
        default=padding,
    )

    # Frame the keyword anchor against the section bbox (default) or the canvas
    # margin. The canvas frame lets a corner fill the empty drawing margin.
    if graph.legend_anchor == "canvas":
        left, top = padding, padding
    else:
        left, top = content_left, content_top
    right, bottom = max_x, max_y

    if pos == "bl":
        legend_x = left
        legend_y = bottom - legend_h
    elif pos == "br":
        legend_x = right - legend_w - inset
        legend_y = bottom - legend_h - inset
    elif pos == "tl":
        legend_x = left + inset
        legend_y = top + inset
    elif pos == "tr":
        legend_x = right - legend_w - inset
        legend_y = top + inset
    elif pos == "bottom":
        legend_x = left
        legend_y = bottom + gap
    elif pos == "right":
        legend_x = right + gap
        legend_y = top

    # An explicit pin (canvas anchor or offset) means the author placed the
    # block deliberately, so don't auto-relocate it; warn instead if it
    # overlaps. A casual corner keyword relocates to the bottom-left when it
    # would overlap a section or a routed line.
    explicit_pin = graph.legend_anchor == "canvas" or graph.legend_offset is not None
    if graph.legend_offset is not None:
        legend_x += graph.legend_offset[0]
        legend_y += graph.legend_offset[1]

    if explicit_pin:
        if _legend_overlaps_content(
            legend_x, legend_y, legend_w, legend_h, graph, routes
        ):
            warnings.warn(
                f"legend pinned at '{pos}' overlaps a section or route.",
                stacklevel=2,
            )
    elif pos not in ("bottom", "right") and _legend_overlaps_content(
        legend_x, legend_y, legend_w, legend_h, graph, routes
    ):
        legend_x = content_left
        legend_y = max_y + gap

    return legend_x, legend_y, legend_w, legend_h, show_legend


def _icon_obstacles_by_station(
    graph: MetroGraph,
    theme: Theme,
    station_offsets: dict[tuple[str, str], float],
) -> dict[str, tuple[float, float, float, float]]:
    """Compute each terminus file icon's bounding box, keyed by station id.

    The box covers the icon row(s) and any caption text beneath, with a
    clearance margin -- the same geometry the label placer treats as an
    obstacle.  Keyed by station so a caller can attribute a box to the
    station whose icon it is (e.g. to exempt that station's own terminating
    segment from a line-crosses-icon check).
    """
    obstacles: dict[str, tuple[float, float, float, float]] = {}
    margin = ICON_CLEARANCE_MARGIN

    for station in graph.stations.values():
        if not station.is_terminus or not station.terminus_labels:
            continue

        # Reuse the renderer's own icon-placement geometry so the obstacle
        # tracks the *drawn* icons.  Icons march along the section's flow
        # axis -- horizontally for LR/RL, vertically for TB/BT -- so a
        # box that always assumed a horizontal row sat beside the wrong
        # axis on a vertical-flow terminus (a line could rake the real
        # icon while the box reported clear).
        line_offs = [
            station_offsets.get((station.id, lid), 0.0)
            for lid in graph.station_lines(station.id)
        ]
        min_off = min(line_offs) if line_offs else 0.0
        max_off = max(line_offs) if line_offs else 0.0
        centers = _terminus_icon_centers_for(station, graph, theme, min_off, max_off)
        if not centers:
            continue

        # Stacked-files icons extend beyond nominal size by the offset.
        has_stacked = ICON_TYPE_FILES in (station.terminus_icon_types or [])
        stacked_pad = (
            theme.terminus_width * FILES_ICON_OFFSET_RATIO if has_stacked else 0.0
        )
        icon_half_w = theme.terminus_width / 2 + stacked_pad
        icon_half_h = theme.terminus_height / 2 + stacked_pad

        x_min = min(cx for cx, _ in centers) - icon_half_w
        x_max = max(cx for cx, _ in centers) + icon_half_w
        y_min = min(cy for _, cy in centers) - icon_half_h
        y_max = max(cy for _, cy in centers) + icon_half_h

        # Captions render below the icon row, so extend the box downward to
        # cover them and keep neighbouring labels at a distance.
        if any(station.terminus_names or []):
            y_max += ICON_NAME_GAP + theme.label_font_size * ICON_NAME_FONT_SCALE

        obstacles[station.id] = (
            x_min - margin,
            y_min - margin,
            x_max + margin,
            y_max + margin,
        )

    return obstacles


def _compute_icon_obstacles(
    graph: MetroGraph,
    theme: Theme,
    station_offsets: dict[tuple[str, str], float],
) -> list[tuple[float, float, float, float]]:
    """Bounding boxes for terminus file icons, as label-placement obstacles."""
    return list(_icon_obstacles_by_station(graph, theme, station_offsets).values())


def render_svg(
    graph: MetroGraph,
    theme: Theme,
    width: int | None = None,
    height: int | None = None,
    padding: float = CANVAS_PADDING,
    animate: bool | None = None,
    debug: bool = False,
    legend_position: str | None = None,
    responsive: bool = False,
    font_portability: Literal["embed", "paths"] | None = None,
    svg_class_prefix: str = "",
    inject_dark_mode_css: bool = True,
    chrome_css: bool = True,
    self_color_scheme: bool = True,
    bare: bool = False,
) -> str:
    """Render a metro map graph to an SVG string.

    ``width``, ``height``, and ``animate`` fall back to the graph's fields
    (set by directive or CLI flag) when left unset.

    If ``legend_position`` is given it overrides ``graph.legend_position``
    for this render only, without mutating the graph.

    If ``responsive`` is True the root ``<svg>`` element omits fixed
    ``width``/``height`` attributes and adds
    ``preserveAspectRatio="xMinYMin meet"``, so a host page can scale the
    diagram with CSS (e.g. ``width: 100%; height: auto``).

    ``font_portability`` controls how the SVG handles fonts on foreign hosts:

    - ``"embed"``: inlines a subset of Inter as a base64 ``@font-face`` block.
    - ``"paths"``: converts all text to vector paths, removing font
      dependencies entirely (requires ``fontTools[woff]``).
    - ``None`` (default): bare ``font-family`` reference, resolved by the host renderer.

    ``svg_class_prefix``: when non-empty, every SVG presentation class (e.g.
    ``nf-metro-station``, ``metro-line-<id>``) is prefixed with
    ``<svg_class_prefix>-``.  Use distinct prefixes for each map on a shared
    page to prevent CSS collisions between maps or with host-page styles.
    ``data-*`` attributes and manifest element ids are not affected.

    ``inject_dark_mode_css``: when False, the ``prefers-color-scheme: dark``
    ``<style>`` block is omitted.  Useful when a host page manages its own
    theme and the injected media query would fight it.

    ``chrome_css``: when False, the chrome ``--nfm-map-*`` CSS custom-property
    ``<style>`` block is omitted.  Chrome elements carry the resolved mode's
    concrete colors as presentation attributes regardless, so the map still
    renders; what False drops is the live ``var()`` recolor hook and the
    ``light-dark()`` mode adaptation.  Set this False for raster export (e.g.
    cairosvg, which cannot parse ``var()``/``light-dark()``) or any consumer
    without CSS custom-property support - pick the concrete mode via the theme.

    ``self_color_scheme``: when True the root ``<svg>`` declares
    ``color-scheme: light dark`` so a standalone file (opened directly or via
    ``<img>``) resolves ``light-dark()`` against the viewer's OS preference.
    Set False when inlining into a page that owns the theme (the docs site,
    the playground): the SVG then inherits the page's ``color-scheme`` so a
    manual light/dark toggle drives it. No effect for single-palette themes.

    ``bare``: when True, omits the title and outer padding so the canvas
    hugs the diagram content.  The attribution watermark is kept.  Use for
    embedding the SVG inside a host page that supplies its own frame and
    heading.
    """
    if not graph.stations:
        return '<svg xmlns="http://www.w3.org/2000/svg"></svg>'

    if width is None:
        width = graph.width
    if height is None:
        height = graph.height
    if animate is None:
        animate = graph.animate

    scaled_theme = _scale_theme_fonts(theme, graph.font_scale)
    with class_prefix_context(svg_class_prefix), font_scale_context(graph.font_scale):
        try:
            svg = _render_svg_scaled(
                graph,
                scaled_theme,
                width=width,
                height=height,
                padding=padding,
                animate=animate,
                debug=debug,
                legend_position=legend_position,
                responsive=responsive,
                inject_dark_mode_css=inject_dark_mode_css,
                chrome_css=chrome_css,
                self_color_scheme=self_color_scheme,
                bare=bare,
            )
        except (CurveInvariantError, SectionHeaderClashError) as exc:
            reframed = _fold_threshold_error(graph)
            if reframed is not None:
                raise reframed from exc
            raise

    if _fold_back_under_compression(graph):
        reframed = _fold_threshold_error(graph)
        if reframed is not None:
            raise reframed

    if font_portability == "paths":
        from nf_metro.render.font_embed import text_to_paths as _text_to_paths

        return _text_to_paths(svg)

    if font_portability == "embed":
        from nf_metro.render.font_embed import embed_font as _embed_font

        return _embed_font(svg)

    return svg


def _fold_threshold_error(graph: MetroGraph) -> FoldThresholdError | None:
    """Reframe a fold-induced routing abort as an authoring error.

    Returns ``None`` when the section grid was not compressed by a user-set
    fold threshold, so the original internal invariant propagates (it is a
    genuine engine self-check at the map's natural width).  Otherwise the
    compacted geometry is what the router could not resolve, so return a
    :class:`FoldThresholdError` naming the directive and the fix.
    """
    relocated = graph._fold_compressed_sections
    if not relocated:
        return None
    sections = ", ".join(sorted(relocated))
    return FoldThresholdError(
        f"fold_threshold={graph._fold_threshold_effective} is too small for "
        f"this map: it folds section(s) {sections} into a tighter grid than "
        f"their natural layout, leaving the router no room to separate the "
        f"bundle curves. Raise --fold-threshold (or the %%metro fold_threshold "
        f"directive), or remove it to render at the default width."
    )


def _fold_back_under_compression(graph: MetroGraph) -> bool:
    """True when a fold-compressed grid leaves a line folding back over itself.

    A too-small fold can collapse an inter-section fan onto a single row where a
    line runs out along a track and straight back along it.  The doubled-back
    legs draw collinear on the trunk, so no curve self-check fires and the map
    renders silently tangled (a station in the overlap is read out of flow
    order).  Detecting it here lets the render chokepoint reframe the tangle as
    a :class:`FoldThresholdError`, the same authoring error a fold-induced curve
    abort raises, instead of emitting the tangled map.

    Guarded on ``_fold_compressed_sections`` so the default (un-folded) render
    pays nothing: at the natural width the set is empty and this short-circuits.
    """
    if not graph._fold_compressed_sections:
        return False
    return any(iter_opposing_line_overlaps(graph))


def _scale_theme_fonts(theme: Theme, scale: float) -> Theme:
    """Return a theme with every text size multiplied by ``scale``.

    Returns the theme unchanged at ``scale == 1.0`` so the default render
    is identical to the unscaled theme.
    """
    if scale == 1.0:
        return theme
    return replace(
        theme,
        label_halo_width=theme.label_halo_width * scale,
        label_font_size=theme.label_font_size * scale,
        title_font_size=theme.title_font_size * scale,
        section_label_font_size=theme.section_label_font_size * scale,
        legend_font_size=theme.legend_font_size * scale,
        terminus_font_size=theme.terminus_font_size * scale,
    )


def _render_svg_scaled(
    graph: MetroGraph,
    theme: Theme,
    *,
    width: int | None,
    height: int | None,
    padding: float,
    animate: bool,
    debug: bool,
    legend_position: str | None,
    responsive: bool = False,
    inject_dark_mode_css: bool = True,
    chrome_css: bool = True,
    self_color_scheme: bool = True,
    bare: bool = False,
) -> str:
    """Render body, run with ``theme`` fonts and label metrics already scaled."""
    effective_legend_position = (
        legend_position if legend_position is not None else graph.legend_position
    )

    station_offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=station_offsets)
    assert_render_curve_invariants(graph, routes, station_offsets)
    assert_render_layout_invariants(graph, routes, station_offsets, strict=graph.strict)
    header_polylines = [apply_route_offsets(route, station_offsets) for route in routes]

    # Compute labels early so section bbox expansions are applied
    # before section boxes are drawn and canvas bounds are computed.
    icon_obstacles = _compute_icon_obstacles(graph, theme, station_offsets)
    labels = place_labels(
        graph,
        station_offsets=station_offsets,
        icon_obstacles=icon_obstacles,
        routes=routes,
        label_angle=graph.label_angle or 0.0,
    )

    # Per-station rendered label (top, bottom) Y, so group bands clear the
    # (possibly diagonal) station labels rather than just the markers.
    label_extents: dict[str, tuple[float, float]] = {}
    for p in labels:
        if p.station_id:
            _, ly0, _, ly1 = _label_bbox(p)
            label_extents[p.station_id] = (ly0, ly1)

    group_bands = (
        _group_bands(graph, theme, station_offsets, label_extents)
        if graph.groups
        else []
    )

    # Reserve room inside section boxes for below group bands before bboxes
    # feed the section render and the canvas-bounds computation.
    if group_bands:
        _reserve_section_space_for_groups(graph, group_bands)

    # Resolve headers against the final section bboxes (label and group
    # reservations above can grow a box, moving where its header sits).
    header_placements = resolve_all_section_headers(
        graph, theme.section_label_font_size, header_polylines
    )
    _guard_section_headers_clear_routes(header_placements, header_polylines)

    max_x, max_y = _compute_canvas_bounds(graph, routes, debug)

    # Group captions can extend below/right of the content; grow the canvas
    # so they are not clipped.
    if group_bands:
        g_max_x, g_max_y = _group_caption_bounds(group_bands)
        max_x = max(max_x, g_max_x)
        max_y = max(max_y, g_max_y)

    # Compute legend and logo dimensions
    adaptive_logo = chrome_css and _is_adaptive_mode(graph)
    show_logo, logo_w, logo_h, effective_logo = _resolve_logo(graph, adaptive_logo)

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
        routes,
    )

    if show_legend:
        max_x = max(max_x, legend_x + legend_w)
        max_y = max(max_y, legend_y + legend_h)

    # Standalone logo positioning (only when no legend to embed it in)
    logo_x = 0.0
    logo_y = 0.0
    if show_logo and not show_legend:
        logo_w *= graph.logo_scale
        logo_h *= graph.logo_scale
        logo_x = padding
        logo_y = LOGO_Y_STANDALONE
        max_x = max(max_x, logo_x + logo_w)

    # Right margin: one padding width in full mode; none in bare mode so the
    # canvas hugs the content.  Bottom margin is always just enough room for
    # the watermark text.
    auto_width = max_x + (0.0 if bare else padding)
    auto_height = max_y + WATERMARK_Y_INSET * 2 + WATERMARK_FONT_SIZE

    # A relocated header may sit past the box; let it use the margins already
    # added above and only stretch the canvas for the part that overflows them,
    # so a below/side header adds no needless blank space.
    for placement in header_placements.values():
        if placement.mode == "above":
            continue
        _, _, hx1, hy1 = placement.keepout
        auto_width = max(auto_width, hx1 + SECTION_HEADER_ROUTE_PAD)
        auto_height = max(auto_height, hy1 + SECTION_HEADER_ROUTE_PAD)

    svg_width = width or int(auto_width)
    svg_height = height or int(auto_height)

    # Local import: themes imports render.style, so a module-level import here
    # would close a render <-> themes cycle depending on first-import order.
    from nf_metro.themes import mode_pair

    root_attrs: dict[str, str] = {}
    if responsive:
        root_attrs["preserveAspectRatio"] = "xMinYMin meet"
    if self_color_scheme and mode_pair(theme) is not None:
        root_attrs["style"] = "color-scheme: light dark"
    d = draw.Drawing(svg_width, svg_height, **root_attrs)

    positive_fan = tb_positive_fan_sections(graph)

    # Embed the machine-readable manifest first, so the file is a durable,
    # self-describing contract regardless of what is drawn below it.
    if graph.embed_manifest:
        boxes: dict[str, dict[str, float]] = {}
        for s in graph.stations.values():
            if s.is_port or s.is_hidden:
                continue
            cx, cy, w, h, rx = station_marker_box(
                graph, theme, s, station_offsets, positive_fan
            )
            boxes[s.id] = {"x": cx, "y": cy, "w": w, "h": h, "rx": rx}
        manifest = build_manifest(
            graph,
            width=svg_width,
            height=svg_height,
            station_radius=theme.station_radius,
            extra_node_data=boxes,
        )
        d.append(draw.Raw(manifest_metadata_svg(manifest)))

    # Chrome CSS: custom properties so hosts can recolor without re-rendering.
    # Injected before the background rect so browser parsing order is correct.
    if chrome_css:
        _inject_chrome_css(d, theme)

    # Dark-mode CSS for transparent-background themes so that elements
    # rendered directly on the canvas (section labels, number badges,
    # title) remain readable when the browser provides a dark background.
    # Must follow the chrome CSS so the media-query rule wins by source order.
    if inject_dark_mode_css and (
        not theme.background_color or theme.background_color == "none"
    ):
        _inject_dark_mode_style(d)

    # Background (skip for transparent themes)
    if theme.background_color and theme.background_color != "none":
        d.append(
            draw.Rectangle(
                0,
                0,
                svg_width,
                svg_height,
                fill=theme.background_color,
                class_=_ns("nf-metro-bg"),
            )
        )

    # Title / Logo (omitted in bare mode; standalone logo only when not in legend)
    if not bare:
        if show_logo and not logo_in_legend:
            if adaptive_logo:
                _render_adaptive_logo(
                    d,
                    graph.logo_path_light,
                    graph.logo_path_dark,
                    logo_x,
                    logo_y,
                    logo_w,
                    logo_h,
                )
            else:
                _render_logo(d, effective_logo, logo_x, logo_y, logo_w, logo_h)
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
                    **{"class": _ns("nf-metro-title")},
                )
            )

    # Sections
    if graph.sections:
        _render_first_class_sections(d, graph, theme, header_placements)

    # Draw edges (lines) behind stations
    _render_edges(d, graph, routes, station_offsets, theme)

    # Directional chevrons ride on top of the lines but behind stations.
    if graph.directional:
        _render_directional_markers(d, graph, routes, station_offsets, theme)

    # Animation (after edges, before stations so balls travel behind station markers)
    if animate:
        from nf_metro.render.animate import render_animation

        render_animation(d, graph, routes, station_offsets, theme)

    # Draw stations (all circles, skip ports)
    _render_stations(d, graph, theme, station_offsets, positive_fan)

    # Draw labels
    _render_labels(d, labels, theme)

    # Annotative intra-section group captions.
    if group_bands:
        _render_station_groups(d, theme, group_bands)

    # Debug overlay (ports, hidden stations, edge waypoints)
    if debug:
        _render_debug_overlay(d, graph, routes, station_offsets, theme)

    # Legend (with embedded logo if present)
    if show_legend:
        _in_legend = logo_in_legend
        render_legend(
            d,
            graph,
            theme,
            legend_x,
            legend_y,
            logo_path=effective_logo if (_in_legend and not adaptive_logo) else None,
            logo_path_light=(
                graph.logo_path_light if (adaptive_logo and _in_legend) else None
            ),
            logo_path_dark=(
                graph.logo_path_dark if (adaptive_logo and _in_legend) else None
            ),
            logo_size=legend_logo_size,
        )

    # Attribution watermark
    watermark_x_inset = (
        WATERMARK_BARE_X_INSET if bare else padding * WATERMARK_PADDING_RATIO
    )
    d.append(
        draw.Text(
            f"created with nf-metro {_version_string()}",
            WATERMARK_FONT_SIZE,
            svg_width - watermark_x_inset,
            svg_height - WATERMARK_Y_INSET,
            fill=WATERMARK_FILL,
            font_family=theme.label_font_family,
            text_anchor="end",
        )
    )

    if graph.caption:
        d.append(
            draw.Text(
                graph.caption,
                CAPTION_FONT_SIZE,
                watermark_x_inset,
                svg_height - WATERMARK_Y_INSET,
                fill=CAPTION_FILL,
                font_family=theme.label_font_family,
                text_anchor="start",
            )
        )

    svg = d.as_svg()
    if responsive:
        svg = _strip_svg_dimensions(svg)
    return svg


_SVG_OPEN_TAG_RE = re.compile(r"(<svg\b[^>]*?>)", re.DOTALL)
_SVG_WH_ATTR_RE = re.compile(r'\s+(?:width|height)="[^"]*"')


def _strip_svg_dimensions(svg: str) -> str:
    """Remove fixed width/height attributes from the root <svg> opening tag."""
    return _SVG_OPEN_TAG_RE.sub(
        lambda m: _SVG_WH_ATTR_RE.sub("", m.group(1)), svg, count=1
    )


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


def _legend_overlaps_routes(
    lx: float,
    ly: float,
    lw: float,
    lh: float,
    routes: list[RoutedPath],
    margin: float,
) -> bool:
    """Check if a legend rectangle (grown by *margin*) crosses any route."""
    bbox = (lx - margin, ly - margin, lx + lw + margin, ly + lh + margin)
    for route in routes:
        pts = route.points
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            if segment_intersects_bbox(x1, y1, x2, y2, bbox):
                return True
    return False


def _legend_overlaps_content(
    lx: float,
    ly: float,
    lw: float,
    lh: float,
    graph: MetroGraph,
    routes: list[RoutedPath],
) -> bool:
    """Whether the legend rect overlaps a section box or a routed line."""
    return _legend_overlaps_sections(lx, ly, lw, lh, graph) or _legend_overlaps_routes(
        lx, ly, lw, lh, routes, LEGEND_ROUTE_CLEARANCE
    )


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


def _effective_logo_path(graph: MetroGraph) -> str:
    """Return the logo path appropriate for the graph's style.

    Used for non-adaptive (static) rendering such as PNG export.  When the
    directive was ``%%metro logo: light.png | dark.png``, the light variant is
    returned for ``style: light`` and the dark variant otherwise.  Falls back
    to the single-path ``logo_path`` for backwards compatibility.
    """
    is_light = graph.style.strip().lower() == "light"
    if is_light and graph.logo_path_light:
        return graph.logo_path_light
    if not is_light and graph.logo_path_dark:
        return graph.logo_path_dark
    return graph.logo_path


def _is_adaptive_mode(graph: MetroGraph) -> bool:
    """True when the %%metro logo: directive used pipe syntax (one or both variants)."""
    return bool(graph.logo_path_light) or bool(graph.logo_path_dark)


def _has_adaptive_logos(graph: MetroGraph) -> bool:
    """Return True when both logo variants are set and their files exist."""
    return (
        bool(graph.logo_path_light)
        and Path(graph.logo_path_light).is_file()
        and bool(graph.logo_path_dark)
        and Path(graph.logo_path_dark).is_file()
    )


def _resolve_logo_file(raw: str, source_dir: str) -> str:
    """Return an absolute path to *raw* if it resolves, else empty string.

    Tries *raw* as-is first, then relative to *source_dir*.
    """
    if Path(raw).is_file():
        return raw
    if source_dir:
        candidate = Path(source_dir) / raw
        if candidate.is_file():
            return str(candidate)
    return ""


def _resolve_logo(graph: MetroGraph, adaptive: bool) -> tuple[bool, float, float, str]:
    """Return (show_logo, logo_w, logo_h, effective_logo_path).

    ``adaptive`` is True when the %%metro logo: directive used pipe syntax.
    In adaptive mode the dimensions come from whichever variant file exists.
    In single-path mode ``effective_logo`` is the path to render.
    """
    if adaptive:
        raw_candidates = (graph.logo_path_dark, graph.logo_path_light)
        dim_path = next(
            (_resolve_logo_file(p, graph.source_dir) for p in raw_candidates if p),
            "",
        )
        if dim_path:
            w, h = compute_logo_dimensions(dim_path)
            return True, w, h, ""
        set_paths = [p for p in raw_candidates if p]
        if set_paths:
            warnings.warn(
                f"%%metro logo: path(s) {set_paths} not found; logo will be omitted",
                UserWarning,
                stacklevel=2,
            )
        return False, 0.0, 0.0, ""
    effective = _effective_logo_path(graph)
    if not effective:
        return False, 0.0, 0.0, ""
    resolved = _resolve_logo_file(effective, graph.source_dir)
    if resolved:
        w, h = compute_logo_dimensions(resolved)
        return True, w, h, resolved
    warnings.warn(
        f"%%metro logo: path {effective!r} not found; logo will be omitted",
        UserWarning,
        stacklevel=2,
    )
    return False, 0.0, 0.0, effective


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


def _render_adaptive_logo(
    d: draw.Drawing,
    light_path: str,
    dark_path: str,
    x: float,
    y: float,
    logo_w: float,
    logo_h: float,
) -> None:
    """Embed logo variant(s) using SVG masks driven by light-dark().

    Either path may be absent; only variants whose files exist are rendered,
    each masked to its respective mode. ``light-dark()`` inherits color-scheme
    from the host document so logos follow a page's dark/light toggle, not
    only the OS media preference.
    """
    key_path = dark_path or light_path
    dark_mask_id, light_mask_id = _adaptive_logo_mask_ids(key_path)
    has_dark = bool(dark_path) and Path(dark_path).is_file()
    has_light = bool(light_path) and Path(light_path).is_file()
    defs_parts = []
    if has_dark:
        defs_parts.append(
            f'<mask id="{dark_mask_id}" maskContentUnits="objectBoundingBox">'
            f'<rect width="1" height="1" fill="light-dark(#000,#fff)"/>'
            f"</mask>"
        )
    if has_light:
        defs_parts.append(
            f'<mask id="{light_mask_id}" maskContentUnits="objectBoundingBox">'
            f'<rect width="1" height="1" fill="light-dark(#fff,#000)"/>'
            f"</mask>"
        )
    if not defs_parts:
        return
    d.append(draw.Raw(f"<defs>{''.join(defs_parts)}</defs>"))
    if has_dark:
        d.append(
            draw.Image(
                x,
                y,
                logo_w,
                logo_h,
                path=dark_path,
                embed=True,
                mask=f"url(#{dark_mask_id})",
            )
        )
    if has_light:
        d.append(
            draw.Image(
                x,
                y,
                logo_w,
                logo_h,
                path=light_path,
                embed=True,
                mask=f"url(#{light_mask_id})",
            )
        )


def _inject_chrome_css(d: draw.Drawing, theme: Theme) -> None:
    """Inject CSS custom properties for chrome colors.

    Defines ``--nfm-map-*`` properties on the chrome element classes so a host
    can recolor the map's non-semantic surfaces (background, labels, section
    boxes, legend) by setting those properties on a wrapping element.  The
    fallback for each property is the mode-adaptive ``light-dark()`` of the
    theme's light and dark palettes (or the theme's single baked value when it
    has no light/dark family), so the map follows the viewer's ``color-scheme``
    with no host intervention.  Line/route colors carry semantic meaning and
    remain as baked presentation attributes.
    """
    from nf_metro.themes import mode_pair

    pair = mode_pair(theme)
    light, dark = pair if pair is not None else (theme, theme)

    def _adapt(light_val: str, dark_val: str) -> str:
        return light_val if light is dark else f"light-dark({light_val}, {dark_val})"

    def _prop(var: str, attr: str) -> str:
        return f"var({var}, {_adapt(getattr(light, attr), getattr(dark, attr))})"

    def _rule(cls: str, props: str) -> str:
        return f".{_ns(cls)} {{ {props}; }}"

    section_label = "--nfm-map-section-label-color"
    lines: list[str] = []
    if theme.background_color and theme.background_color != "none":
        lines.append(
            _rule("nf-metro-bg", f"fill: {_prop('--nfm-map-bg', 'background_color')}")
        )
    lines += [
        _rule(
            "nf-metro-title", f"fill: {_prop('--nfm-map-title-color', 'title_color')}"
        ),
        _rule(
            "nf-metro-station-label",
            f"fill: {_prop('--nfm-map-label-color', 'label_color')}",
        ),
        _rule(
            "nf-metro-section-box",
            f"fill: {_prop('--nfm-map-section-fill', 'section_fill')};"
            f" stroke: {_prop('--nfm-map-section-stroke', 'section_stroke')}",
        ),
        _rule(
            "nf-metro-section-label",
            f"fill: {_prop(section_label, 'section_label_color')}",
        ),
        _rule(
            "nf-metro-group-label",
            f"fill: {_prop(section_label, 'section_label_color')}",
        ),
        _rule(
            "nf-metro-group-underline",
            f"stroke: {_prop(section_label, 'section_label_color')}",
        ),
        _rule(
            "nf-metro-legend-bg",
            f"fill: {_prop('--nfm-map-legend-bg', 'legend_background')}",
        ),
        _rule(
            "nf-metro-legend-text",
            f"fill: {_prop('--nfm-map-legend-text-color', 'legend_text_color')}",
        ),
    ]
    _ms_adapt = _adapt(
        light.marker_stroke or light.station_stroke,
        dark.marker_stroke or dark.station_stroke,
    )
    lines.append(
        _rule(
            "nf-metro-marker-stroke",
            f"stroke: var(--nfm-map-marker-stroke, {_ms_adapt})",
        )
    )
    # The label halo is a knockout of the background, so it tracks --nfm-map-bg
    # and must flip with the mode too; otherwise a dark-baked halo blots out
    # labels in light mode. Disabled-halo themes draw no halo, so emit nothing.
    halo_light, halo_dark = _label_halo_color(light), _label_halo_color(dark)
    if halo_light is not None and halo_dark is not None:
        halo_val = f"var(--nfm-map-bg, {_adapt(halo_light, halo_dark)})"
        lines.append(
            _rule("nf-metro-label-halo", f"fill: {halo_val}; stroke: {halo_val}")
        )
    d.append(draw.Raw(f"<style>{chr(10).join(lines)}</style>"))


def _inject_dark_mode_style(d: draw.Drawing) -> None:
    """Inject CSS for dark-mode browsers viewing a transparent-background SVG.

    When the SVG has no opaque background, elements rendered directly on the
    canvas (section labels, numbered badges, title) can become invisible if the
    browser supplies a dark page background.  A ``prefers-color-scheme: dark``
    media query adjusts those elements so they remain readable.  CSS rules
    override SVG presentation attributes, so we only need class selectors.
    """
    sl = _ns("nf-metro-section-label")
    sc = _ns("nf-metro-section-num-circle")
    ti = _ns("nf-metro-title")
    css = textwrap.dedent(f"""\
        @media (prefers-color-scheme: dark) {{
            .{sl} {{ fill: #d0d0d0; }}
            .{sc} {{ fill: #777777; }}
            .{ti} {{ fill: #ffffff; }}
        }}
    """)
    d.append(draw.Raw(f"<style>{css}</style>"))


def _guard_section_headers_clear_routes(
    placements: dict[str, SectionHeaderPlacement],
    polylines: list[list[tuple[float, float]]],
) -> None:
    """Fail loudly if any section header was placed over a routed line."""
    clashes = check_section_headers_clear_routes(placements, polylines)
    if clashes:
        raise SectionHeaderClashError("; ".join(c.message() for c in clashes))


def _render_first_class_sections(
    d: draw.Drawing,
    graph: MetroGraph,
    theme: Theme,
    header_placements: dict[str, SectionHeaderPlacement],
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
                class_=_ns("nf-metro-section-box"),
                **section_data,
            )
        )

        # Place the header (number badge + title) clear of any route that would
        # otherwise cross it; the resolver never moves a route to do so.
        placement = header_placements[section.id]

        d.append(
            draw.Circle(
                placement.badge_cx,
                placement.badge_cy,
                SECTION_NUM_CIRCLE_R_LARGE,
                fill=theme.station_stroke,
                **{
                    "class": _ns("nf-metro-section-num-circle"),
                    "data-section-id": section.id,
                },
            )
        )
        d.append(
            draw.Text(
                str(section.number),
                SECTION_NUM_FONT_SIZE,
                placement.badge_cx,
                placement.badge_cy,
                fill=theme.station_fill,
                font_family=theme.label_font_family,
                font_weight="bold",
                text_anchor="middle",
                dy=TEXT_VCENTER_DY,
                **{"data-section-id": section.id},
            )
        )

        label_kwargs: dict[str, object] = {
            "class": _ns("nf-metro-section-label"),
            "data-section-id": section.id,
        }
        if placement.label_rotation:
            label_kwargs["transform"] = (
                f"rotate({placement.label_rotation} "
                f"{placement.label_x} {placement.label_y})"
            )
        d.append(
            draw.Text(
                section.name,
                theme.section_label_font_size,
                placement.label_x,
                placement.label_y,
                fill=theme.section_label_color,
                font_family=theme.label_font_family,
                font_weight="bold",
                dy=TEXT_VCENTER_DY,
                **label_kwargs,
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

    polylines = [apply_route_offsets(route, station_offsets) for route in routes]
    bridges: dict[int, list[BridgeBreak]] = (
        compute_bridges(graph, routes, polylines, curve_radius=curve_radius)
        if theme.bridge_glyph
        else {}
    )

    for route, pts in zip(routes, polylines):
        line = graph.lines.get(route.line_id)
        color = line.color if line else FALLBACK_LINE_COLOR
        style_kw = line_style_kwargs(line.style) if line else {}
        class_name = _ns(f"metro-line-{route.line_id}")
        breaks = bridges.get(id(route))

        if breaks:
            _render_bridged_edge(
                d, pts, route, breaks, color, style_kw, class_name, theme, curve_radius
            )
        elif len(pts) == 2:
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
            before, after, curved = _curve_tangents(pts, resolved)

            for i in range(1, len(pts) - 1):
                if curved[i]:
                    path.L(*before[i])
                    path.Q(pts[i][0], pts[i][1], *after[i])
                else:
                    path.L(*pts[i])

            path.L(*pts[-1])
            d.append(path)


def _render_directional_markers(
    d: draw.Drawing,
    graph: MetroGraph,
    routes: list[RoutedPath],
    station_offsets: dict[tuple[str, str], float],
    theme: Theme,
) -> None:
    """Draw static chevrons along each route, pointing source to target.

    The flow direction is the order of the routed point sequence, so each
    chevron simply rides the polyline at the local segment direction. Markers
    are spaced by arc length and kept sparse and subtle by default, in the
    spirit of a one-way transit line's direction-of-travel arrows.
    """
    size = theme.directional_marker_size
    spacing = max(theme.directional_marker_spacing, 1.0)
    opacity = theme.directional_marker_opacity
    # A route shorter than one chevron carries no useful direction cue.
    min_length = 2 * size
    stroke_width = max(theme.line_width * 0.5, 1.0)

    for route in routes:
        pts = apply_route_offsets(route, station_offsets)
        if len(pts) < 2:
            continue
        line = graph.lines.get(route.line_id)
        color = theme.directional_marker_color or (
            line.color if line else FALLBACK_LINE_COLOR
        )
        class_name = _ns(f"metro-direction-{route.line_id}")
        for point, heading in _chevron_samples(pts, spacing, min_length):
            _draw_chevron(
                d,
                point,
                heading,
                color,
                size,
                stroke_width,
                opacity,
                class_name,
                route.line_id,
            )


def _chevron_samples(
    pts: list[tuple[float, float]], spacing: float, min_length: float
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Sample (point, unit-heading) pairs evenly along a polyline.

    Chevrons are centred on the polyline so a route reads symmetrically. A
    route between ``min_length`` and ``spacing`` in length carries a single
    chevron at its midpoint.
    """
    segments = [
        (a, b, length)
        for a, b in zip(pts, pts[1:])
        if (length := math.hypot(b[0] - a[0], b[1] - a[1])) > 0
    ]
    total = sum(length for _, _, length in segments)
    if total < min_length:
        return []

    count = max(1, int(total // spacing))
    start = (total - (count - 1) * spacing) / 2

    samples: list[tuple[tuple[float, float], tuple[float, float]]] = []
    targets = [start + i * spacing for i in range(count)]
    travelled = 0.0
    ti = 0
    for (ax, ay), (bx, by), length in segments:
        ux, uy = (bx - ax) / length, (by - ay) / length
        while ti < len(targets) and targets[ti] <= travelled + length:
            offset = targets[ti] - travelled
            samples.append(((ax + ux * offset, ay + uy * offset), (ux, uy)))
            ti += 1
        travelled += length
    return samples


def _draw_chevron(
    d: draw.Drawing,
    point: tuple[float, float],
    heading: tuple[float, float],
    color: str,
    size: float,
    stroke_width: float,
    opacity: float,
    class_name: str,
    line_id: str,
) -> None:
    """Draw one open ``>`` chevron centred at *point*, apex along *heading*."""
    px, py = point
    ux, uy = heading
    perp = (-uy, ux)
    apex = (px + ux * size, py + uy * size)
    back = (px - ux * size, py - uy * size)
    wing1 = (back[0] + perp[0] * size, back[1] + perp[1] * size)
    wing2 = (back[0] - perp[0] * size, back[1] - perp[1] * size)

    path = draw.Path(
        stroke=color,
        stroke_width=stroke_width,
        fill="none",
        stroke_linecap="round",
        stroke_linejoin="round",
        opacity=opacity,
        class_=class_name,
        **{"data-line-id": line_id},
    )
    path.M(*wing1)
    path.L(*apex)
    path.L(*wing2)
    d.append(path)


def _curve_tangents(
    pts: list[tuple[float, float]], resolved: list[float]
) -> tuple[
    dict[int, tuple[float, float]], dict[int, tuple[float, float]], dict[int, bool]
]:
    """Curve entry/exit points for each interior vertex.

    Returns ``(before, after, curved)`` keyed by vertex index ``i`` in
    ``1..len(pts)-2``: ``before[i]``/``after[i]`` are the points where the
    smoothing curve leaves and rejoins the polyline; ``curved[i]`` is False
    for a degenerate corner (zero-length neighbour), where both collapse to
    the vertex itself.
    """
    before: dict[int, tuple[float, float]] = {}
    after: dict[int, tuple[float, float]] = {}
    curved: dict[int, bool] = {}
    for i, tan in enumerate(curve_tangents(pts, resolved), start=1):
        before[i] = tan.before
        after[i] = tan.after
        curved[i] = tan.curved
    return before, after, curved


def _render_bridged_edge(
    d: draw.Drawing,
    pts: list[tuple[float, float]],
    route: RoutedPath,
    breaks: list[BridgeBreak],
    color: str,
    style_kw: dict[str, str],
    class_name: str,
    theme: Theme,
    curve_radius: float,
) -> None:
    """Render an under-line interrupted by a short gap at each crossing.

    The line is drawn exactly as the continuous case except that, on every
    straight run carrying a crossing, the pen lifts across a small gap so the
    over-line reads as passing over the top.
    """
    resolved = resolve_curve_radii(pts, route.curve_radii, default_radius=curve_radius)
    m = len(pts) - 1
    before, after, _ = _curve_tangents(pts, resolved)

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
    for s in range(m):
        run_start = pts[0] if s == 0 else after[s]
        run_end = pts[m] if s + 1 == m else before[s + 1]
        seg_breaks = sorted(
            (bk for bk in breaks if bk.seg_index == s),
            key=lambda bk: (
                (bk.cut_a[0] - run_start[0]) ** 2 + (bk.cut_a[1] - run_start[1]) ** 2
            ),
        )
        for bk in seg_breaks:
            path.L(*bk.cut_a)
            path.M(*bk.cut_b)
        path.L(*run_end)
        if s + 1 <= m - 1:
            path.Q(pts[s + 1][0], pts[s + 1][1], *after[s + 1])
    d.append(path)


def _drawn_bundle_span(
    graph: MetroGraph,
    station: Station,
    station_offsets: dict[tuple[str, str], float],
    positive_fan: set[str],
) -> tuple[float, float]:
    """Min/max of a station's per-line offsets *as drawn*.

    A vertical-flow (TB) section is the 90-degree rotation of a horizontal one:
    a line rides ``x - offset`` where an LR line rides ``y + offset`` (matching
    :func:`_tb_x_offset`).  Spanning the marker over the drawn offsets keeps it
    centred on the lines that actually pass through the station, so a one-line or
    off-trunk-subset station does not leave its glyph beside its own track.
    """
    raw = [
        station_offsets.get((station.id, lid), 0.0)
        for lid in graph.station_lines(station.id)
    ]
    if not raw:
        return 0.0, 0.0
    sec = graph.sections.get(station.section_id) if station.section_id else None
    if sec is not None and sec.direction == "TB":
        sign = 1.0 if station.section_id in positive_fan else -1.0
        drawn = [sign * off for off in raw]
    else:
        drawn = raw
    return min(drawn), max(drawn)


def _pill_box(
    station: Station,
    r: float,
    min_off: float,
    max_off: float,
    is_tb_vert: bool,
    flow_len: float | None = None,
) -> tuple[float, float, float, float]:
    """Return ``(x, y, w, h)`` for the bundle-spanning pill at a station.

    The pill covers every line passing through the station: it spans the line
    bundle across the section's flow axis (wide for TB sections where lines
    arrive vertically, tall otherwise) and is centred on the bundle mid-offset.
    ``flow_len`` overrides the extent along the flow axis (default ``2 * r``),
    elongating the glyph along the line.
    """
    span = max_off - min_off
    mid = (min_off + max_off) / 2
    flow = r * 2 if flow_len is None else flow_len
    if is_tb_vert:
        w, h = span + r * 2, flow
        cx, cy = station.x + mid, station.y
    else:
        w, h = flow, span + r * 2
        cx, cy = station.x, station.y + mid
    return cx - w / 2, cy - h / 2, w, h


def station_marker_box(
    graph: MetroGraph,
    theme: Theme,
    station: Station,
    station_offsets: dict[tuple[str, str], float] | None,
    positive_fan: set[str] | None = None,
) -> tuple[float, float, float, float, float]:
    """The drawn marker's bounding box as ``(cx, cy, w, h, rx)``.

    Mirrors the bundle-span / orientation logic of :func:`_render_station_into`
    and defers to :func:`_pill_box` for the box itself, so an overlay can place
    a shape that matches a station's pill (a circle for one line, a capsule
    spanning the bundle for several) without re-running the renderer. Glyph
    stations (rail interchanges, explicit markers) fall back to the same
    bundle-span box, a reasonable footprint for those too.

    ``positive_fan`` lets a caller iterating over stations pass that set once
    rather than re-deriving its reversal fixed-point per station.
    """
    r = theme.station_radius
    is_tb_vert = bool(
        station.section_id
        and (sec := graph.sections.get(station.section_id))
        and sec.direction == "TB"
    )
    if positive_fan is None:
        positive_fan = tb_positive_fan_sections(graph)
    if station.rail_top_y is not None and station.rail_bottom_y is not None:
        used = station.rail_used_ys or [station.y]
        min_off, max_off = min(used) - station.y, max(used) - station.y
    elif station_offsets and not graph.station_is_rail(station.id):
        min_off, max_off = _drawn_bundle_span(
            graph, station, station_offsets, positive_fan
        )
    else:
        min_off = max_off = 0.0

    x, y, w, h = _pill_box(station, r, min_off, max_off, is_tb_vert)
    return x + w / 2, y + h / 2, w, h, r


def _append_terminus_icons(
    d: draw.Drawing,
    station: Station,
    graph: MetroGraph,
    theme: Theme,
    r: float,
    min_off: float,
    max_off: float,
) -> None:
    """Render a station's terminus icons into their own data-tagged group."""
    icon_group = draw.Group(**{"data-station-id": station.id})
    _render_terminus_icons(icon_group, station, graph, theme, r, min_off, max_off)
    d.append(icon_group)


def _render_marker_station(
    d: draw.Drawing,
    marker: MarkerStyle,
    theme: Theme,
    station: Station,
    r: float,
    min_off: float,
    max_off: float,
    is_tb_vert: bool,
    station_data: dict[str, str],
) -> None:
    """Draw a shape/fill marker glyph over the station's line bundle.

    ``circle`` and ``square`` keep the bundle-spanning pill geometry, varying
    only the corner rounding. ``pill`` is elongated along the line (a capsule
    in the opposite orientation to the default station pill) while still
    growing across the bundle to cover every track it spans.
    """
    flow_len = (
        r * MARKER_PILL_LENGTH_RATIO if marker.shape == MARKER_SHAPE_PILL else None
    )
    x, y, w, h = _pill_box(station, r, min_off, max_off, is_tb_vert, flow_len=flow_len)
    rx = marker_corner_radius(marker.shape, r)
    marker_data = {
        **station_data,
        "class_": f"{station_data['class_']} {_ns('nf-metro-marker-stroke')}",
    }
    d.append(
        draw.Rectangle(
            x,
            y,
            w,
            h,
            rx=rx,
            ry=rx,
            fill=marker_fill_color(marker.fill, theme),
            stroke=marker_stroke_color(theme),
            stroke_width=theme.station_stroke_width,
            **marker_data,
        )
    )


def _station_data_attrs(graph: MetroGraph, station: Station) -> dict[str, str]:
    """SVG data-attributes shared by every station marker.

    Values that flow from user content (label, section name) are HTML-escaped
    because drawsvg does not escape unknown kwargs.
    """
    data = {
        "class_": _ns("nf-metro-station"),
        "data-station-id": station.id,
        "data-station-lines": ",".join(graph.station_lines(station.id)),
        "data-station-label": html.escape(station.label or station.id),
    }
    if station.section_id:
        data["data-section-id"] = station.section_id
        sec_obj = graph.sections.get(station.section_id)
        if sec_obj:
            data["data-section-name"] = html.escape(sec_obj.name)
    return data


def _rail_marker_fill(marker: MarkerStyle | None, theme: Theme) -> str | None:
    """Interior tint for a spanning rail interchange carrying a marker.

    Only a marker with a literal colour fill (not the ``open`` / ``solid``
    keywords) tints the interchange; keyword fills and unmarked stations keep
    the default interior so their glyph is unchanged.
    """
    if marker is None or marker.fill in (MARKER_FILL_OPEN, MARKER_FILL_SOLID):
        return None
    return marker_fill_color(marker.fill, theme)


def _draw_interchange_glyph(
    d: draw.Group | draw.Drawing,
    x: float,
    knobs: list[tuple[str, float]],
    theme: Theme,
    r: float,
    *,
    interior_fill: str,
    outline: str,
    station_data: dict[str, str],
    data_station_id: str,
) -> None:
    """Draw the metro interchange idiom: a knob on each used rail joined by a
    link bar spanning them (the classic metro / nf-core-sarek glyph).

    ``knobs`` is one ``(line_id, y)`` per rail the glyph stops on.  It is built
    in two stacked layers so the outline stays continuous where the link meets a
    knob: an outline layer (link bar + discs grown by the stroke width) paints
    the union's outer boundary, then an interior layer at the true radii fills
    it.  The link is the *fill* colour, not the stroke, so it reads as joining
    the knobs rather than cutting across them.
    """
    if not knobs:
        return
    ys = [y for _, y in knobs]
    top_y, bot_y = min(ys), max(ys)
    sw = theme.station_stroke_width
    # A spanning interchange draws each rail's knob slightly larger than the bare
    # marker, bulging out of a narrower link bar.  A single-knob stop has nothing
    # to bulge from, so it uses the standard marker radius.
    is_spanning = (bot_y - top_y) > SAME_COORD_TOLERANCE
    knob_r = r * RAIL_KNOB_RADIUS_RATIO if is_spanning else r
    bar_half = r * RAIL_LINK_HALF_WIDTH_RATIO

    def _link_bar(width: float, stroke: str, **extra: object) -> None:
        # A round-capped line of the given width is a capsule; used here only
        # for its straight body between the top and bottom knobs (the caps are
        # covered by the circles), so it joins them with no seam.
        if bot_y - top_y <= SAME_COORD_TOLERANCE:
            return
        d.append(
            draw.Line(
                x,
                top_y,
                x,
                bot_y,
                stroke=stroke,
                stroke_width=width,
                stroke_linecap="round",
                **extra,
            )
        )

    def _knobs(radius: float, fill: str, **extra: object) -> None:
        for lid, y in knobs:
            d.append(
                draw.Circle(
                    x,
                    y,
                    radius,
                    fill=fill,
                    stroke=fill,
                    stroke_width=0,
                    **{**extra, "data-line-id": lid},
                )
            )

    _link_bar(
        (bar_half + sw) * 2,
        outline,
        **{**station_data, "class_": _ns("nf-metro-rail-connector")},
    )
    _knobs(
        knob_r + sw,
        outline,
        **{
            "class_": _ns("nf-metro-rail-knob-outline"),
            "data-station-id": data_station_id,
        },
    )
    _link_bar(bar_half * 2, interior_fill)
    _knobs(
        knob_r,
        interior_fill,
        **{"class_": _ns("nf-metro-rail-knob"), "data-station-id": data_station_id},
    )


def _render_rail_pill(
    d: draw.Drawing,
    graph: MetroGraph,
    station: Station,
    theme: Theme,
    r: float,
    fill_override: str | None = None,
) -> None:
    """Render a rail-mode multi-rail station as the metro interchange glyph.

    ``fill_override`` tints the interior (link bar + knobs) with a marker fill
    colour while keeping the interchange shape, so a spanning rail station can
    carry its ``%%metro marker:`` colour; a tinted interchange takes the light
    marker outline so the fill reads against the dark background.
    """
    interior_fill = fill_override if fill_override is not None else theme.station_fill
    outline = (
        marker_stroke_color(theme)
        if fill_override is not None
        else theme.station_stroke
    )
    # rail_used_ys is recorded parallel to the line-definition order (see
    # rail_mode._layout_section_rails); zip the knobs against that same order.  A
    # rail within the connector's span but not used by the station gets no knob.
    served = graph.station_lines_ordered(station.id)
    knobs = list(zip(served, station.rail_used_ys))
    _draw_interchange_glyph(
        d,
        station.x,
        knobs,
        theme,
        r,
        interior_fill=interior_fill,
        outline=outline,
        station_data=_station_data_attrs(graph, station),
        data_station_id=station.id,
    )


def _render_interchange(
    d: draw.Group | draw.Drawing,
    graph: MetroGraph,
    ic: Interchange,
    theme: Theme,
    station_offsets: dict[tuple[str, str], float] | None,
    r: float,
) -> None:
    """Draw a cross-track interchange as one glyph across its member stations.

    Each member sub-station sits at the same X on its own track; the glyph is a
    knob where each line crosses (its laid-out, offset-applied Y) joined by a
    link bar spanning the members, built in the same two stacked layers as the
    rail-mode interchange so the outline stays continuous.
    """
    members = [graph.stations[mid] for mid in ic.member_ids if mid in graph.stations]
    if not members:
        return
    offs = station_offsets or {}
    # One knob per (member, line it carries) at the line's rendered Y.
    knobs = [
        (lid, m.y + offs.get((m.id, lid), 0.0))
        for m in members
        for lid in graph.station_lines_ordered(m.id)
    ]
    # A marker on the anchor tints the glyph and takes the light marker outline,
    # mirroring a marked rail-mode interchange.
    fill_override = _rail_marker_fill(members[0].marker, theme)
    _draw_interchange_glyph(
        d,
        members[0].x,
        knobs,
        theme,
        r,
        interior_fill=fill_override
        if fill_override is not None
        else theme.station_fill,
        outline=(
            marker_stroke_color(theme)
            if fill_override is not None
            else theme.station_stroke
        ),
        station_data=_station_data_attrs(graph, members[0]),
        data_station_id=ic.node_id,
    )


def _draw_blank_terminus_nub(
    d: draw.Drawing,
    station: Station,
    r: float,
    min_off: float,
    max_off: float,
    station_data: dict[str, str],
    theme: Theme,
    is_tb_vert: bool = False,
) -> None:
    """Draw a blank terminus's unrounded nub (a sharp-cornered station rect)."""
    bx, by, bw, bh = _pill_box(station, r, min_off, max_off, is_tb_vert)
    d.append(
        draw.Rectangle(
            bx,
            by,
            bw,
            bh,
            fill=theme.station_fill,
            stroke=theme.station_stroke,
            stroke_width=theme.station_stroke_width,
            **station_data,
        )
    )


def _station_group_attrs(
    graph: MetroGraph,
    theme: Theme,
    station: Station,
    station_offsets: dict[tuple[str, str], float] | None = None,
    positive_fan: set[str] | None = None,
) -> dict[str, Any]:
    """Attributes for a station's wrapping ``<g>`` element.

    Makes each station one addressable DOM node carrying its identity and
    geometry, so a consumer can restyle or replace it without the manifest or
    a re-render.  ``data-node-id`` is the join key: it equals the station's
    ``id`` in the embedded manifest.  The geometry attributes mirror the
    manifest's ``x``/``y``/``r`` (absolute SVG user units, rounded to 1dp) so an
    overlay can position against either half interchangeably.  A metro line is a
    manifest group and a section is a manifest region.  When ``station_offsets``
    is supplied the full pill box (``w``/``h``/``rx``) is also emitted so an
    overlay can reproduce the exact marker shape.
    """
    section = graph.sections.get(station.section_id) if station.section_id else None
    section_id = (
        station.section_id if section is not None and not section.is_implicit else None
    )
    cx: float = station.x
    cy: float = station.y
    w: float | None = None
    h: float | None = None
    rx: float | None = None
    if station_offsets is not None:
        cx, cy, w, h, rx = station_marker_box(
            graph, theme, station, station_offsets, positive_fan
        )
    return {
        "class_": _ns("nf-metro-station-group"),
        **node_data_attrs(
            id=station.id,
            x=cx,
            y=cy,
            r=theme.station_radius,
            groups=graph.station_lines(station.id),
            region=section_id,
            w=w,
            h=h,
            rx=rx,
        ),
    }


def _render_stations(
    d: draw.Drawing,
    graph: MetroGraph,
    theme: Theme,
    station_offsets: dict[tuple[str, str], float] | None = None,
    positive_fan: set[str] | None = None,
) -> None:
    """Render stations as pill shapes.

    Normal stations get vertical pills (tall, narrow). Stations in a TB
    section get horizontal pills (wide, short) since the lines run
    vertically through them.

    When the manifest is embedded, each station's glyph (pill/marker/rail
    interchange plus any terminus icons) is wrapped in its own ``<g>`` carrying
    ``data-node-*`` identity and geometry, so the station is a single
    addressable element; with ``--no-manifest`` the glyphs are drawn directly
    with no wrapper. Skips port stations (is_port=True).
    """
    if positive_fan is None:
        positive_fan = tb_positive_fan_sections(graph)
    for station in graph.stations.values():
        if station.is_port or station.is_hidden:
            continue
        if graph.embed_manifest:
            attrs = _station_group_attrs(
                graph, theme, station, station_offsets, positive_fan
            )
            g = draw.Group(**attrs)
            _render_station_into(
                g, graph, theme, station, station_offsets, positive_fan
            )
            d.append(g)
        else:
            _render_station_into(
                d, graph, theme, station, station_offsets, positive_fan
            )


def _render_station_into(
    d: draw.Group | draw.Drawing,
    graph: MetroGraph,
    theme: Theme,
    station: Station,
    station_offsets: dict[tuple[str, str], float] | None,
    positive_fan: set[str],
) -> None:
    """Draw one station's glyph and terminus icons into a container.

    The container is the station's wrapping ``<g>`` when the manifest is
    embedded, or the drawing itself under ``--no-manifest``.
    """
    r = theme.station_radius

    # Cross-track interchange: the whole glyph is drawn once, anchored on the
    # node the interchange kept (its id equals interchange_id); the other member
    # sub-stations contribute knobs but draw nothing of their own.  A marker on
    # the anchor tints the glyph (like a rail-mode interchange) rather than
    # suppressing it.
    if station.interchange_id is not None:
        if station.id == station.interchange_id:
            ic = next(
                (c for c in graph.interchanges if c.node_id == station.interchange_id),
                None,
            )
            if ic is not None and ic.member_ids:
                _render_interchange(d, graph, ic, theme, station_offsets, r)
        return

    # Rail mode: a blank terminus terminates its converged bundle exactly
    # like any other render -- the standard unrounded nub (via _pill_box)
    # spanning the bundle, plus the file icon -- rather than a rail-specific
    # glyph.  The only difference is the span comes from the rail bundle
    # (rail mode does not use station_offsets).  An off-track terminus parks
    # off the rails and its lines converge onto a single vertical stub, so its
    # nub is the bundle-centred square (zero span) seating the buffer stop
    # where the stub meets the file icon, matching on-rail file termini.
    if graph.station_is_rail(station.id) and station.is_blank_terminus:
        if station.off_track:
            t_min = t_max = 0.0
        else:
            used = station.rail_used_ys or [station.y]
            t_min = min(used) - station.y
            t_max = max(used) - station.y
        _draw_blank_terminus_nub(
            d, station, r, t_min, t_max, _station_data_attrs(graph, station), theme
        )
        _append_terminus_icons(d, station, graph, theme, r, t_min, t_max)
        return

    # Rail mode: a multi-rail station draws as a spanning interchange; a
    # single-rail station draws as one knob centred on its rail.  Both go
    # through _render_rail_pill (its link bar self-suppresses with no span),
    # so a single stop sits exactly on the rail rather than being shifted by
    # the normal-mode parallel-line station_offsets, which don't apply once
    # a line is pinned to a fixed rail.  Marked stations keep their glyph.
    if (
        graph.station_is_rail(station.id)
        and station.marker is None
        and not station.is_terminus
        and not station.off_track
    ):
        _render_rail_pill(d, graph, station, theme, r)
        return
    if station.rail_top_y is not None and station.rail_bottom_y is not None:
        _render_rail_pill(
            d, graph, station, theme, r, _rail_marker_fill(station.marker, theme)
        )
        return

    # A vertical-flow (TB/BT) station draws a horizontal (rotated) pill.
    is_tb_vert = False
    if station.section_id:
        sec = graph.sections.get(station.section_id)
        if sec and lanes_run_along_x(sec.direction):
            is_tb_vert = True

    # A rail station is pinned to its rail Y; the parallel-line bundle
    # offsets do not apply (the rail-pill path above ignores them too), so a
    # marked single-rail station's glyph must seat on the rail rather than
    # ride the bundle's mid-offset.
    if station_offsets and not graph.station_is_rail(station.id):
        min_off, max_off = _drawn_bundle_span(
            graph, station, station_offsets, positive_fan
        )
    else:
        min_off = max_off = 0.0

    station_data = _station_data_attrs(graph, station)

    if graph.station_is_rail(station.id) and (min_off, max_off) != (0.0, 0.0):
        raise AssertionError(
            f"rail station {station.id!r} marker glyph offset "
            f"({min_off}, {max_off}) would lift it off its rail"
        )

    if station.marker is not None:
        _render_marker_station(
            d,
            station.marker,
            theme,
            station,
            r,
            min_off,
            max_off,
            is_tb_vert,
            station_data,
        )
        if station.is_terminus:
            _append_terminus_icons(d, station, graph, theme, r, min_off, max_off)
        return

    x, y, w, h = _pill_box(station, r, min_off, max_off, is_tb_vert)

    # Blank terminus stations get an unrounded nub; everything else a pill.
    if station.is_blank_terminus:
        _draw_blank_terminus_nub(
            d, station, r, min_off, max_off, station_data, theme, is_tb_vert
        )
    else:
        d.append(
            draw.Rectangle(
                x,
                y,
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
        _append_terminus_icons(d, station, graph, theme, r, min_off, max_off)


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


def _terminus_icon_marching(
    theme: Theme, names: list[str], is_vertical_flow: bool
) -> tuple[float, list[float]]:
    """Per-icon centre-to-centre step along the flow axis, and caption widths.

    TB/BT stack icons by height (plus a caption row when captioned); LR/RL
    march by a caption-aware width.  Returns the step and each caption's
    estimated width, shared by the icon-placement helper and the renderer's
    caption-stagger logic so the two stay in lockstep.
    """
    caption_font_size = theme.label_font_size * ICON_NAME_FONT_SCALE
    name_widths = [len(n) * caption_font_size * 0.55 if n else 0.0 for n in names]
    if is_vertical_flow:
        caption_room = caption_font_size + ICON_NAME_GAP if any(names) else 0.0
        step = theme.terminus_height + ICON_INTER_GAP + caption_room
    else:
        step = caption_aware_icon_step(names, name_widths, theme.terminus_width)
    return step, name_widths


def _terminus_icon_centers_for(
    station: Station,
    graph: MetroGraph,
    theme: Theme,
    min_off: float,
    max_off: float,
) -> list[tuple[float, float]]:
    """Drawn centre of each of *station*'s terminus icons.

    Single source of truth for terminus-icon placement, shared by the
    renderer (to draw the icons) and the obstacle / clearance logic (to
    reason about where they land).  Derives the flow axis, marching step,
    and bundle offset the same way the renderer does, then delegates to
    :func:`_terminus_icon_centers`.  ``min_off``/``max_off`` carry the
    station's bundle span so each caller supplies the offsets it already
    has.  Returns an empty list for non-terminus stations.
    """
    if not station.is_terminus or not station.terminus_labels:
        return []

    section = graph.sections.get(station.section_id) if station.section_id else None
    is_source = not graph.edges_to(station.id)
    section_dir = section.direction if section else "LR"
    is_vertical_flow = lanes_run_along_x(section_dir)

    r = theme.station_radius
    icon_gap = r + ICON_STATION_GAP
    icon_half_w = theme.terminus_width / 2
    icon_half_h = theme.terminus_height / 2
    icon_half_flow = icon_half_h if is_vertical_flow else icon_half_w

    bundle_center = (min_off + max_off) / 2

    names = station.terminus_names or [""] * len(station.terminus_labels)
    icon_step, _ = _terminus_icon_marching(theme, names, is_vertical_flow)

    is_rail = graph.station_is_rail(station.id)
    offtrack_nub_lift = (
        OFFTRACK_TERMINUS_NUB_CLEARANCE
        if (station.off_track and is_rail and station.is_captioned_terminus)
        else 0.0
    )

    return _terminus_icon_centers(
        station,
        section_dir,
        is_source,
        len(station.terminus_labels),
        icon_gap + icon_half_flow + offtrack_nub_lift,
        icon_step,
        bundle_center,
        is_rail=is_rail,
    )


def _terminus_icon_centers(
    station: Station,
    section_dir: str,
    is_source: bool,
    n: int,
    first_offset: float,
    step: float,
    bundle_center: float,
    is_rail: bool = False,
) -> list[tuple[float, float]]:
    """Centre coordinates for a terminus station's file icons.

    Icons march away from the station along the section's *flow* axis
    (X for LR/RL, Y for TB/BT) and stay centred on the station's bundle
    on the *cross* axis.  Sinks extend in the forward flow direction,
    sources in the reverse; RL/BT mirror that so icons always point to
    the outside of the diagram.
    """
    is_vertical_flow = lanes_run_along_x(section_dir)
    # A rail-mode off-track input parks above the rails and feeds straight down
    # into its consumer's rail (see routing/rail.py), so its icon sits directly
    # on the station coordinate (centred on the drop X) rather than marching
    # sideways.  Gated on the rail flag so normal-mode off-track feeders (which
    # the standard router handles) are untouched.
    if station.off_track and is_rail:
        return [(station.x, station.y - (first_offset + i * step)) for i in range(n)]
    # Sinks sit at the end of the flow and extend forwards; sources sit at
    # the start and extend backwards.  RL/BT reverse the forward direction.
    extends_forward = is_source if section_dir in ("RL", "BT") else not is_source
    sign = 1.0 if extends_forward else -1.0
    centers: list[tuple[float, float]] = []
    for i in range(n):
        flow = sign * (first_offset + i * step)
        if is_vertical_flow:
            centers.append((station.x + bundle_center, station.y + flow))
        else:
            centers.append((station.x + flow, station.y + bundle_center))
    return centers


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

    Multiple icons march away from the station along the section's flow
    axis (a horizontal row for LR/RL, a vertical stack for TB/BT), with
    the first icon closest to the station pill.
    """
    section: Section | None = (
        graph.sections.get(station.section_id) if station.section_id else None
    )
    section_dir = section.direction if section else "LR"
    is_vertical_flow = lanes_run_along_x(section_dir)
    icon_half_w = theme.terminus_width / 2
    icon_half_h = theme.terminus_height / 2

    icon_types = station.terminus_icon_types or [ICON_TYPE_FILE] * len(
        station.terminus_labels
    )
    names = station.terminus_names or [""] * len(station.terminus_labels)
    banners = station.terminus_icon_banners or [False] * len(station.terminus_labels)

    caption_font_size = theme.label_font_size * ICON_NAME_FONT_SCALE
    icon_step, name_widths = _terminus_icon_marching(theme, names, is_vertical_flow)

    centers = _terminus_icon_centers_for(station, graph, theme, min_off, max_off)

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
        banner = banners[i] if i < len(banners) else False

        icon_cx, icon_cy = centers[i]

        # Clamp to stay within the section bbox, on whichever axis the
        # icons march along.
        if section and is_vertical_flow and section.bbox_h > 0:
            top = section.bbox_y + icon_half_h + ICON_BBOX_MARGIN
            bottom = section.bbox_y + section.bbox_h - icon_half_h - ICON_BBOX_MARGIN
            icon_cy = max(top, min(icon_cy, bottom))
        elif section and not is_vertical_flow and section.bbox_w > 0:
            icon_right = (
                section.bbox_x + section.bbox_w - icon_half_w - ICON_BBOX_MARGIN
            )
            icon_cx = max(
                section.bbox_x + icon_half_w + ICON_BBOX_MARGIN,
                min(icon_cx, icon_right),
            )

        common: dict[str, Any] = dict(
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
            render_files_icon(
                d, **common, fold_size=theme.terminus_fold_size, banner=banner
            )
        else:
            render_file_icon(
                d, **common, fold_size=theme.terminus_fold_size, banner=banner
            )

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


def _label_halo_color(theme: Theme) -> str | None:
    """Resolve the halo colour, or ``None`` when haloing is disabled.

    Haloing is disabled by a non-positive ``label_halo_width`` or an explicit
    ``label_halo_color`` of ``"none"`` (matching the SVG ``fill="none"``
    convention). An empty colour resolves to the background, so the name
    punches a knockout through any route it crosses, or white on transparent
    themes.
    """
    if theme.label_halo_width <= 0 or theme.label_halo_color == "none":
        return None
    if theme.label_halo_color:
        return theme.label_halo_color
    bg = theme.background_color
    return bg if bg and bg != "none" else "#ffffff"


def _render_labels(
    d: draw.Drawing,
    labels: list[LabelPlacement],
    theme: Theme,
) -> None:
    """Render station name labels."""
    halo_color = _label_halo_color(theme)

    def emit(text: str, x: float, y: float, **style: object) -> None:
        # The halo is a stroked copy drawn underneath the glyph fill. A second
        # paint pass (rather than paint-order on a single element) keeps the
        # knockout correct in renderers that ignore the paint-order property.
        if halo_color is not None:
            d.append(
                draw.Text(
                    text,
                    theme.label_font_size,
                    x,
                    y,
                    fill=halo_color,
                    stroke=halo_color,
                    stroke_width=theme.label_halo_width,
                    stroke_linejoin="round",
                    font_family=theme.label_font_family,
                    font_weight=theme.label_font_weight,
                    line_height=LABEL_LINE_HEIGHT,
                    aria_hidden="true",
                    class_=_ns("nf-metro-label-halo"),
                    **style,
                )
            )
        d.append(
            draw.Text(
                text,
                theme.label_font_size,
                x,
                y,
                fill=theme.label_color,
                font_family=theme.label_font_family,
                font_weight=theme.label_font_weight,
                line_height=LABEL_LINE_HEIGHT,
                **style,
                **label_data,
            )
        )

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
            label_data["class_"] = _ns("nf-metro-station-label")

        if label.angle:
            # Diagonal labels: anchor at the pill and rotate about
            # the anchor.  text-anchor=start so the tilted text trails
            # away from the station.
            emit(
                text,
                label.x,
                label.y,
                text_anchor=label.text_anchor or "start",
                dominant_baseline="auto",
                transform=f"rotate({label.angle},{label.x},{label.y})",
            )
        elif label.dominant_baseline:
            # Custom placement (e.g. TB vertical stations: right-side labels)
            emit(
                text,
                label.x,
                y,
                text_anchor=label.text_anchor,
                dominant_baseline=label.dominant_baseline,
            )
        else:
            baseline = "auto" if label.above else "hanging"
            emit(
                text,
                label.x,
                y,
                text_anchor="middle",
                dominant_baseline=baseline,
            )


def _station_marker_extent(
    station: Station,
    graph: MetroGraph,
    theme: Theme,
    station_offsets: dict[tuple[str, str], float],
) -> tuple[float, float, float, float]:
    """Return ``(x_left, x_right, y_top, y_bottom)`` of a station's marker.

    Mirrors the pill geometry in ``_render_stations`` so group captions can
    clear the actual rendered marker, including the per-line offset spread.
    """
    r = theme.station_radius
    is_vertical_flow = False
    if station.section_id:
        sec = graph.sections.get(station.section_id)
        if sec and lanes_run_along_x(sec.direction):
            is_vertical_flow = True

    line_offsets = [
        station_offsets.get((station.id, lid), 0.0)
        for lid in graph.station_lines(station.id)
    ]
    min_off = min(line_offsets) if line_offsets else 0.0
    max_off = max(line_offsets) if line_offsets else 0.0

    x, y, w, h = _pill_box(station, r, min_off, max_off, is_vertical_flow)
    return (x, x + w, y, y + h)


class _GroupBand(NamedTuple):
    """Resolved render geometry for one annotative station-group caption.

    ``rule_y`` is the bracket rule (drawn directly against the spanned run);
    ``tick_dy`` is the signed length of the inward end-ticks (pointing back
    towards the stations); ``text_y``/``baseline`` place the caption clear of
    the rule on the far side; ``band_far_y`` is the band's outermost edge
    (largest ``y`` for a below band, smallest for an above band) used for
    bbox/canvas reservation.
    """

    label: str
    x_left: float
    x_right: float
    rule_y: float
    tick_dy: float
    text_y: float
    baseline: str
    band_far_y: float
    section_id: str | None


class _RawGroup(NamedTuple):
    """A group's resolved horizontal span and outer reference Y, before the
    bracket gap and common-rule alignment are applied."""

    label: str
    x_left: float
    x_right: float
    section_id: str | None
    position: str
    ref: float
    ref_is_label: bool


def _group_bands(
    graph: MetroGraph,
    theme: Theme,
    station_offsets: dict[tuple[str, str], float],
    label_extents: dict[str, tuple[float, float]] | None = None,
) -> list[_GroupBand]:
    """Resolve per-group band geometry.

    For a ``below`` band the bracket rule hugs the bottom of the spanned
    run, its end-ticks point up towards the stations, and the caption sits
    beneath the rule; an ``above`` band mirrors this. Groups whose members
    are all missing/hidden are skipped.

    ``label_extents`` maps a station id to its rendered label's ``(top, bottom)``
    Y.  When given, a below band is dropped beneath the deepest member label
    (and an above band lifted above the highest), so diagonal station labels
    that hang past the markers do not collide with the band.
    """
    caption_font = theme.label_font_size * GROUP_LABEL_FONT_SCALE

    # First pass: resolve each group's horizontal span and the natural outer
    # reference (deepest member label/marker for a below band, highest for an
    # above band) before the bracket gap is applied.
    raw: list[_RawGroup] = []
    for group in graph.groups:
        members = [
            graph.stations[sid]
            for sid in group.station_ids
            if sid in graph.stations
            and not graph.stations[sid].is_port
            and not graph.stations[sid].is_hidden
        ]
        if not members:
            continue
        extents = [
            _station_marker_extent(s, graph, theme, station_offsets) for s in members
        ]

        x_left = min(e[0] for e in extents)
        x_right = max(e[1] for e in extents)
        section_id = members[0].section_id

        # Member label extremes, so the band clears the (possibly diagonal)
        # station labels rather than just the markers.
        member_label_tops = [
            label_extents[s.id][0]
            for s in members
            if label_extents and s.id in label_extents
        ]
        member_label_bottoms = [
            label_extents[s.id][1]
            for s in members
            if label_extents and s.id in label_extents
        ]

        if group.position == "above":
            marker_top = min(e[2] for e in extents)
            label_ref = min(member_label_tops) if member_label_tops else None
            ref = min([marker_top, *member_label_tops])
            ref_is_label = label_ref is not None and label_ref <= marker_top
        else:
            marker_bottom = max(e[3] for e in extents)
            label_ref = max(member_label_bottoms) if member_label_bottoms else None
            ref = max([marker_bottom, *member_label_bottoms])
            ref_is_label = label_ref is not None and label_ref >= marker_bottom
        raw.append(
            _RawGroup(
                label=group.label,
                x_left=x_left,
                x_right=x_right,
                section_id=section_id,
                position=group.position,
                ref=ref,
                ref_is_label=ref_is_label,
            )
        )

    # Align all bands sharing a (section, side) to a common rule line, so a
    # row of group captions reads off one level rather than stepping with each
    # group's own member-label depth.  Below bands align to the deepest ref,
    # above bands to the highest.  Track whether that common extreme is a
    # station label (vs a bare marker): a label already carries its full
    # footprint, so the band hugs it with a small clearance instead of the
    # wider marker-row gap, which over-shoots for diagonal labels.
    common_ref: dict[tuple[str | None, str], float] = {}
    common_is_label: dict[tuple[str | None, str], bool] = {}
    for rec in raw:
        key = (rec.section_id, rec.position)
        ref = rec.ref
        more_extreme = (
            key not in common_ref
            or (rec.position == "above" and ref < common_ref[key])
            or (rec.position != "above" and ref > common_ref[key])
        )
        if rec.position == "above":
            common_ref[key] = min(common_ref.get(key, ref), ref)
        else:
            common_ref[key] = max(common_ref.get(key, ref), ref)
        if more_extreme:
            common_is_label[key] = rec.ref_is_label

    out: list[_GroupBand] = []
    for rec in raw:
        key = (rec.section_id, rec.position)
        ref = common_ref[key]
        gap = (
            GROUP_LABEL_LABEL_CLEARANCE if common_is_label.get(key) else GROUP_LABEL_GAP
        )
        if rec.position == "above":
            rule_y = ref - gap
            tick_dy = GROUP_LABEL_TICK_LENGTH  # ticks point down to stations
            text_y = rule_y - GROUP_LABEL_UNDERLINE_GAP
            baseline = "auto"
            band_far_y = text_y - caption_font
        else:
            rule_y = ref + gap
            tick_dy = -GROUP_LABEL_TICK_LENGTH  # ticks point up to stations
            text_y = rule_y + GROUP_LABEL_UNDERLINE_GAP
            baseline = "hanging"
            band_far_y = text_y + caption_font
        out.append(
            _GroupBand(
                rec.label,
                rec.x_left,
                rec.x_right,
                rule_y,
                tick_dy,
                text_y,
                baseline,
                band_far_y,
                rec.section_id,
            )
        )
    return out


def _group_caption_bounds(bands: list[_GroupBand]) -> tuple[float, float]:
    """Return the max ``(x, y)`` reached by any group band."""
    max_x = 0.0
    max_y = 0.0
    for band in bands:
        max_x = max(max_x, band.x_right)
        max_y = max(max_y, band.rule_y, band.text_y, band.band_far_y)
    return max_x, max_y


def _reserve_section_space_for_groups(
    graph: MetroGraph,
    bands: list[_GroupBand],
) -> None:
    """Grow section bboxes so each ``below`` group band sits inside its box.

    Annotative bands are placed at render time from station coordinates, so
    the layout engine has no chance to reserve room for them.  Without this
    the caption and bracket overlap (or fall outside) the section's bottom
    edge.
    """
    needed_bottom: dict[str, float] = {}
    for band in bands:
        if band.section_id is None or band.tick_dy >= 0:
            continue  # above bands grow upward; handled via canvas bounds
        needed_bottom[band.section_id] = max(
            needed_bottom.get(band.section_id, band.band_far_y),
            band.band_far_y,
        )
    for sid, far_y in needed_bottom.items():
        sec = graph.sections.get(sid)
        if sec is None or sec.is_implicit:
            continue
        target_bottom = far_y + GROUP_LABEL_BAND_PADDING
        if target_bottom > sec.bbox_y + sec.bbox_h:
            sec.bbox_h = target_bottom - sec.bbox_y


def _reserve_rail_space_for_termini(graph: MetroGraph, theme: Theme) -> None:
    """Grow rail-section boxes to contain their terminus file icons.

    Rail-section bboxes are sized to the station columns only, so a terminus
    station's icons (which march outward past the last station) can be cut by
    the box edge.  Grow the box so the icons sit inside with clearance.  Gated
    on rail sections, leaving normal-mode fixtures byte-identical.
    """
    if not graph.has_rail_sections:
        return
    clearance = ICON_STATION_GAP * 2
    r = theme.station_radius
    hw = theme.terminus_width / 2
    hh = theme.terminus_height / 2
    caption_font_size = theme.label_font_size * ICON_NAME_FONT_SCALE
    for station in graph.stations.values():
        if not (graph.station_is_rail(station.id) and station.is_terminus):
            continue
        sec = graph.sections.get(station.section_id) if station.section_id else None
        if sec is None or sec.is_implicit:
            continue
        n = len(station.terminus_labels)
        names = station.terminus_names or [""] * n
        name_widths = [
            len(nm) * caption_font_size * 0.55 if nm else 0.0 for nm in names
        ]
        is_vertical_flow = lanes_run_along_x(sec.direction)
        if is_vertical_flow:
            step = theme.terminus_height + ICON_INTER_GAP
            half_flow = hh
        else:
            step = caption_aware_icon_step(names, name_widths, theme.terminus_width)
            half_flow = hw
        centers = _terminus_icon_centers(
            station,
            sec.direction,
            not graph.edges_to(station.id),
            n,
            r + ICON_STATION_GAP + half_flow,
            step,
            0.0,
            is_rail=True,
        )
        for cx, cy in centers:
            left, right = cx - hw - clearance, cx + hw + clearance
            top, bot = cy - hh - clearance, cy + hh + clearance
            if left < sec.bbox_x:
                sec.bbox_w = sec.bbox_x + sec.bbox_w - left
                sec.bbox_x = left
            if right > sec.bbox_x + sec.bbox_w:
                sec.bbox_w = right - sec.bbox_x
            if top < sec.bbox_y:
                sec.bbox_h = sec.bbox_y + sec.bbox_h - top
                sec.bbox_y = top
            if bot > sec.bbox_y + sec.bbox_h:
                sec.bbox_h = bot - sec.bbox_y


def _render_station_groups(
    d: draw.Drawing,
    theme: Theme,
    bands: list[_GroupBand],
) -> None:
    """Render annotative captions spanning groups of stations.

    Each group's caption is centred on the x-extent of its (visible) member
    stations.  A bracket rule (with inward end-ticks) hugs the spanned run so
    the band reads as embracing exactly those stations, with the caption set
    clear of the rule on the far side.  Coordinates are read-only: this never
    moves stations.
    """
    caption_font = theme.label_font_size * GROUP_LABEL_FONT_SCALE
    for band in bands:
        cx = (band.x_left + band.x_right) / 2
        # Bracket: a horizontal rule across the run with short inward end-ticks
        # pointing back towards the stations.
        bracket = draw.Path(
            stroke=theme.section_label_color,
            stroke_width=GROUP_LABEL_UNDERLINE_WIDTH,
            stroke_opacity=GROUP_LABEL_UNDERLINE_OPACITY,
            fill="none",
            stroke_linecap="round",
            stroke_linejoin="round",
            class_=_ns("nf-metro-group-underline"),
        )
        bracket.M(band.x_left, band.rule_y + band.tick_dy)
        bracket.L(band.x_left, band.rule_y)
        bracket.L(band.x_right, band.rule_y)
        bracket.L(band.x_right, band.rule_y + band.tick_dy)
        d.append(bracket)
        d.append(
            draw.Text(
                band.label,
                caption_font,
                cx,
                band.text_y,
                fill=theme.section_label_color,
                font_family=theme.label_font_family,
                font_weight="bold",
                text_anchor="middle",
                dominant_baseline=band.baseline,
                class_=_ns("nf-metro-group-label"),
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
        x0, x1 = sec.bbox_x, sec.bbox_x + sec.bbox_w
        y0, y1 = sec.bbox_y, sec.bbox_y + sec.bbox_h
        if sec.grid_col_span == 1:
            cx0, cx1 = col_bounds.get(sec.grid_col, (x0, x1))
            col_bounds[sec.grid_col] = (min(cx0, x0), max(cx1, x1))
        if sec.grid_row_span == 1:
            ry0, ry1 = row_bounds.get(sec.grid_row, (y0, y1))
            row_bounds[sec.grid_row] = (min(ry0, y0), max(ry1, y1))
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
    """Return row-boundary segments as ``(row_a, row_b, x_start, x_end, y)``.

    For each consecutive grid-row pair, pick a boundary Y and draw a
    canvas-wide horizontal line at that Y, broken around bboxes the
    line would cut:

    1. If rows don't overlap globally (max bbox-bottom of row a is
       above min bbox-top of row b), use the global midpoint - the
       natural row separator that sits below every section in the
       upper row and above every section in the lower row.
    2. Otherwise (a fold extends one row into the other's band), emit
       one per-column segment at each cell's local midpoint, dropping
       cells where the bboxes locally overlap.  Fold columns and
       columns missing one of the two rows produce visible gaps.
    """
    if not col_bounds:
        return []
    sec_by_cell = _sections_by_grid_cell(sections)
    canvas_x0 = min(b[0] for b in col_bounds.values()) - 20
    canvas_x1 = max(b[1] for b in col_bounds.values()) + 20
    sections_by_row: dict[int, list[Section]] = {}
    for sec in sections:
        if sec.grid_row_span == 1:
            sections_by_row.setdefault(sec.grid_row, []).append(sec)
    rows = sorted(sections_by_row)
    segments: list[tuple[int, int, float, float, float]] = []
    for i in range(len(rows) - 1):
        ra, rb = rows[i], rows[i + 1]
        row_a_secs = sections_by_row.get(ra, [])
        row_b_secs = sections_by_row.get(rb, [])
        if not row_a_secs or not row_b_secs:
            continue
        max_bottom = max(s.bbox_y + s.bbox_h for s in row_a_secs)
        min_top = min(s.bbox_y for s in row_b_secs)
        if max_bottom < min_top:
            # Rows are globally separable: one canvas-wide line at the
            # natural midpoint (below every row a section, above every
            # row b section).  No bbox is cut by construction.
            y = (max_bottom + min_top) / 2
            segments.append((ra, rb, canvas_x0, canvas_x1, y))
            continue
        # Rows overlap (a fold extends one row into the other's band).
        # Emit per-column segments only at cells where both rows have
        # a non-spanning section and the sections don't locally overlap,
        # so the line appears only where the boundary is unambiguous.
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


def _compute_col_boundary_xs(
    col_bounds: dict[int, tuple[float, float]],
    sections: list[Section] | None = None,
) -> list[tuple[int, int, float]]:
    """Return ``(col_a, col_b, mid_x)`` triples for consecutive grid columns
    whose bbox X ranges don't overlap.

    Columns don't overlap like rows do within a single shared grid (there's
    no column-axis analogue of a fold), so a single canvas-spanning line per
    pair normally suffices.  When the section graph splits into independent
    components, each component is placed in its own local column grid and the
    components reuse the same ``grid_col`` indices in different X bands; a
    midpoint computed from the merged per-column X bounds can then land
    inside another component's section.  Any such boundary is dropped so the
    overlay never cuts a section bbox.
    """
    result: list[tuple[int, int, float]] = []
    sorted_cols = sorted(col_bounds)
    for i in range(len(sorted_cols) - 1):
        ca, cb = sorted_cols[i], sorted_cols[i + 1]
        right = col_bounds[ca][1]
        left = col_bounds[cb][0]
        if right >= left:
            continue
        mid_x = (right + left) / 2
        if sections is not None and any(
            sec.grid_col_span == 1 and sec.bbox_x < mid_x < sec.bbox_x + sec.bbox_w
            for sec in sections
        ):
            continue
        result.append((ca, cb, mid_x))
    return result


def _row_grid_anchor_ys(graph: MetroGraph, station_ids: Iterable[str]) -> list[float]:
    """The distinct placement-row Ys among the given stations.

    A station is positioned by ``station.y`` -- the row anchor (the offset-0
    slot, the top of the bundle), not the centre of its rendered pill, which is
    offset downward by the bundle mid.  The debug grid marks these anchors so a
    line shows where the engine placed a row; same-row stations share their
    anchor and collapse to one line, while a station the engine drifted onto a
    slightly different Y splits off its own line.  Hidden stations (bypass
    junctions and the like) occupy real rows too, so they anchor a line; only
    ports, which ride a neighbouring station's row, are skipped.
    """
    return sorted(
        {
            round(st.y, 1)
            for sid in station_ids
            if (st := graph.stations.get(sid)) and not st.is_port
        }
    )


def _draw_debug_y_grid(
    d: draw.Drawing,
    *,
    x_start: float,
    x_end: float,
    row_ys: list[float],
    label: str,
    debug_font: str,
    debug_font_size: float,
    color: str = DEBUG_ROW_GRID_COLOR,
) -> None:
    """Draw one horizontal line per row Y, labelling the topmost one."""
    for i, y in enumerate(row_ys):
        d.append(
            draw.Line(
                x_start,
                y,
                x_end,
                y,
                stroke=color,
                stroke_width=0.75,
                stroke_dasharray="4,6",
            )
        )
        if i == 0:
            d.append(
                draw.Text(
                    label,
                    debug_font_size,
                    x_start - 4,
                    y,
                    fill=color,
                    font_family=debug_font,
                    text_anchor="end",
                )
            )


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
    is_light = theme.mode == "light"
    waypoint_color = DEBUG_WAYPOINT_COLOR_LIGHT if is_light else DEBUG_WAYPOINT_COLOR
    grid_color = DEBUG_GRID_COLOR_LIGHT if is_light else DEBUG_GRID_COLOR
    row_grid_color = DEBUG_ROW_GRID_COLOR_LIGHT if is_light else DEBUG_ROW_GRID_COLOR

    # Edge waypoints: small filled circles at intermediate points
    for route in routes:
        if len(route.points) <= 2:
            continue
        pts = apply_route_offsets(route, station_offsets)
        # Draw intermediate waypoints (skip first/last which are at stations)
        for px, py in pts[1:-1]:
            d.append(draw.Circle(px, py, DEBUG_WAYPOINT_RADIUS, fill=waypoint_color))

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

        # Global extents for full-canvas column lines and row label anchor.
        if not col_bounds or not row_bounds:
            all_x0 = min(s.bbox_x for s in sections) - 20
            all_y0 = min(s.bbox_y for s in sections) - 20
            all_y1 = max(s.bbox_y + s.bbox_h for s in sections) + 20
        else:
            all_x0 = min(b[0] for b in col_bounds.values()) - 20
            all_y0 = min(b[0] for b in row_bounds.values()) - 20
            all_y1 = max(b[1] for b in row_bounds.values()) + 20
        # grid_color already computed above from theme.mode

        for ca, cb, mid_x in _compute_col_boundary_xs(col_bounds, sections):
            d.append(
                draw.Line(
                    mid_x,
                    all_y0,
                    mid_x,
                    all_y1,
                    stroke=grid_color,
                    stroke_width=1,
                    stroke_dasharray="6,4",
                )
            )
            d.append(
                draw.Text(
                    f"col {ca}|{cb}",
                    debug_font_size,
                    mid_x,
                    all_y0 - 4,
                    fill=grid_color,
                    font_family=debug_font,
                    text_anchor="middle",
                )
            )

        row_segments = _compute_row_boundary_segments(sections, col_bounds)
        for _ra, _rb, x_start, x_end, y in row_segments:
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
        # One label per row pair, anchored at the first segment's Y.
        row_pair_label_ys: dict[tuple[int, int], float] = {}
        for ra, rb, _xs, _xe, y in row_segments:
            row_pair_label_ys.setdefault((ra, rb), y)
        for (ra, rb), y in row_pair_label_ys.items():
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

    # Horizontal row lines at each section's placement-row anchors (the distinct
    # station.y values, see _row_grid_anchor_ys). Multi-section row bands share
    # one full-width set of lines; every other section draws its own.
    grouped_sec_ids: set[str] = set()
    for row, info in graph._row_y_grid_info.items():
        ref_secs = [
            graph.sections[sid] for sid in info["section_ids"] if sid in graph.sections
        ]
        if not ref_secs:
            continue
        grouped_sec_ids.update(s.id for s in ref_secs)
        row_ys = _row_grid_anchor_ys(
            graph, (sid for sec in ref_secs for sid in sec.station_ids)
        )
        if not row_ys:
            continue
        _draw_debug_y_grid(
            d,
            x_start=min(s.bbox_x for s in ref_secs) - 10,
            x_end=max(s.bbox_x + s.bbox_w for s in ref_secs) + 10,
            row_ys=row_ys,
            label=f"row {row} grid",
            debug_font=debug_font,
            debug_font_size=debug_font_size,
            color=row_grid_color,
        )

    for sec in sections:
        if sec.id in grouped_sec_ids:
            continue
        row_ys = _row_grid_anchor_ys(graph, sec.station_ids)
        if not row_ys:
            continue
        _draw_debug_y_grid(
            d,
            x_start=sec.bbox_x - 10,
            x_end=sec.bbox_x + sec.bbox_w + 10,
            row_ys=row_ys,
            label=f"row {sec.grid_row} grid",
            debug_font=debug_font,
            debug_font_size=debug_font_size,
            color=row_grid_color,
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
