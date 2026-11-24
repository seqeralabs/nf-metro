"""SVG generation for metro maps using drawsvg."""

from __future__ import annotations

__all__ = [
    "ObservedRenderPlan",
    "build_observed_render_plan",
    "build_render_plan",
    "emit_render_plan",
    "render_svg",
]

import bisect
import copy
import hashlib
import html
import math
import pickle
import re
import textwrap
import warnings
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
from typing import Any, Literal, NamedTuple, cast

import drawsvg as draw

from nf_metro.layout.constants import (
    COORD_TOLERANCE_FINE,
    LOGO_HEIGHT,
    LOGO_Y_STANDALONE,
    OFFTRACK_TERMINUS_NUB_CLEARANCE,
    SAME_COORD_TOLERANCE,
    SECTION_Y_GAP,
    graph_offset_step,
)
from nf_metro.layout.envelope_settlement import (
    EnvelopeSettlement,
    attach_reroute_ledger_delta,
    attach_settlement_diagnostics,
    drawn_corridor_clearance_requirements,
    measure_boundary_clearance_requirements,
    measure_drawn_corridor_clearance,
    settle_route_envelopes,
)
from nf_metro.layout.fan_plans import FanRouteInvariantError
from nf_metro.layout.geometry import lanes_run_along_x, segment_intersects_bbox
from nf_metro.layout.labels import (
    LabelPlacement,
    _label_bbox,
    _label_ink_quad,
    place_labels,
)
from nf_metro.layout.pass_metrics import font_scale_context, stroke_scale_context
from nf_metro.layout.phases._common import _station_bundle_offset_span
from nf_metro.layout.phases.bbox import measure_row_gap_clearance
from nf_metro.layout.phases.canvas import translate_graph
from nf_metro.layout.phases.guards import (
    FoldThresholdError,
    LayoutInvariantError,
    _guard_canvas_margin_settled,
    assert_canvas_corridors_hold_their_claims,
    assert_render_header_clearance,
    assert_render_layout_invariants,
    assert_reservations_are_settled,
    iter_opposing_line_overlaps,
    render_header_collision,
)
from nf_metro.layout.phases.junctions import reanchor_junctions
from nf_metro.layout.phases.ports import (
    carry_ports_with_section_edges,
    hold_port_anchored_edges,
    section_box_edges,
)
from nf_metro.layout.phases.spacing import _bypass_label_obstacles
from nf_metro.layout.route_plan import (
    ConvergencePlan,
    DemandAxis,
    ExitTurnPlan,
    GridSpan,
    RouteFamilyId,
    RouteMemberGeometryPlan,
    RoutePlan,
    RouteSystem,
    SettlementStage,
    SettlementStageTrace,
    grid_span_for_sections,
    register_settlement_stage,
)
from nf_metro.layout.route_reservations import (
    CanvasRect,
    CanvasRegion,
    ColumnGapRegion,
    CorridorOrientation,
    FinalCanvasGeometry,
    GapCorridorBand,
    ReservationCoordinateTranslation,
    RowGapRegion,
    adopt_route_reservation_ledger,
    attach_reservation_ids_to_routes,
    gap_corridor_clearance_band,
    project_reservation_coordinate,
    realise_route_reservations,
)
from nf_metro.layout.routing import (
    RoutedPath,
    apply_route_offsets,
    compute_station_offsets,
    observe_route_edges_centred,
)
from nf_metro.layout.routing.convergences import ConvergenceInvariantError
from nf_metro.layout.routing.corners import (
    corner_arc_at,
    corner_arc_controls,
    cubic_point,
    curve_tangents,
    resolve_curve_radii,
)
from nf_metro.layout.routing.exit_turns import ExitTurnInvariantError
from nf_metro.layout.routing.families import BYPASS_ROUTE_FAMILIES
from nf_metro.layout.routing.invariants import (
    CurveInvariantError,
    assert_render_curve_invariants,
)
from nf_metro.layout.routing.reserved_bands import build_reserved_corridors
from nf_metro.layout.routing.reversal import tb_positive_fan_sections
from nf_metro.layout.routing.system_emission import (
    validate_published_route_attribution,
)
from nf_metro.layout.settlement_demand import BoundaryClearanceDemand
from nf_metro.manifest import node_data_attrs
from nf_metro.parser.model import (
    ICON_TYPE_DIR,
    ICON_TYPE_FILE,
    ICON_TYPE_FILES,
    MARKER_FILL_OPEN,
    MARKER_FILL_SOLID,
    MARKER_SHAPE_PILL,
    Edge,
    Interchange,
    LayoutGeometryWarning,
    MarkerStyle,
    MetroGraph,
    Section,
    Station,
)
from nf_metro.render.bridges import BridgeBreak, compute_bridges
from nf_metro.render.constants import (
    CANVAS_ORIGIN_MARGIN,
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
    FILES_ICON_OFFSET_RATIO,
    GROUP_LABEL_BAND_PADDING,
    GROUP_LABEL_FONT_SCALE,
    GROUP_LABEL_GAP,
    GROUP_LABEL_LABEL_CLEARANCE,
    GROUP_LABEL_TICK_LENGTH,
    GROUP_LABEL_UNDERLINE_GAP,
    GROUP_LABEL_UNDERLINE_OPACITY,
    GROUP_LABEL_UNDERLINE_WIDTH,
    ICON_BANNER_FILL,
    ICON_BANNER_TEXT_COLOR,
    ICON_BANNER_TEXT_COLOR_MUTED,
    ICON_BBOX_MARGIN,
    ICON_CLEARANCE_MARGIN,
    ICON_INTER_GAP,
    ICON_NAME_FONT_SCALE,
    ICON_NAME_GAP,
    ICON_STATION_GAP,
    LEGEND_GAP,
    LEGEND_INSET,
    LEGEND_ROUTE_CLEARANCE,
    MARKER_PILL_LENGTH_RATIO,
    RAIL_KNOB_RADIUS_RATIO,
    RAIL_LINK_HALF_WIDTH_RATIO,
    SECTION_HEADER_ROUTE_PAD,
    SECTION_NUM_CIRCLE_R_LARGE,
    SECTION_NUM_FONT_SIZE,
    SECTION_STROKE_WIDTH,
    SVG_CURVE_RADIUS,
    TERMINUS_FONT_COLOR,
    TEXT_VCENTER_DY,
    TEXT_VCENTER_EM,
    TITLE_ASCENT_RATIO,
    TITLE_CANVAS_SAFETY_MARGIN,
    TITLE_LOGO_GAP,
    WATERMARK_BARE_X_INSET,
    WATERMARK_FILL,
    WATERMARK_FONT_SIZE,
    WATERMARK_PADDING_RATIO,
    WATERMARK_Y_INSET,
    effective_line_color,
    line_style_kwargs,
    station_is_muted,
    title_baseline_y,
)
from nf_metro.render.icons import (
    render_file_icon,
    render_files_icon,
    render_folder_icon,
)
from nf_metro.render.legend import (
    compute_legend_dimensions,
    logo_image_kwargs,
    marker_corner_radius,
    marker_fill_color,
    marker_stroke_color,
    open_logo_image,
    render_legend,
    resolve_logo_file,
)
from nf_metro.render.manifest import build_manifest, manifest_metadata_svg
from nf_metro.render.ns import adaptive_logo_mask_ids as _adaptive_logo_mask_ids
from nf_metro.render.ns import class_prefix_context
from nf_metro.render.ns import ns as _ns
from nf_metro.render.path_geometry import materialize_source_turnout_paths
from nf_metro.render.plan import (
    _RENDER_GRAPH_EXCLUDED_FIELDS,
    ChevronVertices,
    FrozenGraph,
    FrozenMap,
    FrozenRecord,
    RenderPlan,
    freeze_render_value,
    thaw_render_value,
)
from nf_metro.render.section_header import (
    HeaderLettering,
    SectionHeaderBandError,
    SectionHeaderClashError,
    SectionHeaderOverflowError,
    SectionHeaderPlacement,
    check_section_headers_clear_routes,
    check_section_headers_fit_box_width,
    check_section_headers_hold_the_reserved_band,
    header_lettering_context,
    resolve_all_section_headers,
)
from nf_metro.render.style import (
    GLYPH_RING,
    GLYPH_TICK,
    MUTED_FILL_CLASS,
    MUTED_STROKE_CLASS,
    RING_GROW,
    TERMINUS_BAR,
    Theme,
    adaptive_value,
    baked_binding,
    binding_pair,
    chrome_bindings,
    color_pair,
    is_adaptive,
    muted_adapts,
    muted_paint_class,
    opaque_or_white,
)
from nf_metro.text_metrics import (
    DEFAULT_TEXT_METRICS,
    MetricsFace,
    TextRole,
    active_metrics_face,
    metrics_face_context,
    metrics_face_for_portability,
    text_style,
)


def _drawn_stations(graph: MetroGraph, debug: bool) -> list[Station]:
    """The stations the canvas is sized around; ports and hidden ones are not drawn.

    A graph made only of ports has to be given some extent, so it falls back to
    every station.
    """
    if debug:
        return list(graph.stations.values())
    visible = [s for s in graph.stations.values() if not s.is_port and not s.is_hidden]
    return visible if visible else list(graph.stations.values())


def _compute_canvas_bounds(
    graph: MetroGraph,
    routes: list[RoutedPath],
    debug: bool = False,
    header_placements: dict[str, SectionHeaderPlacement] | None = None,
) -> tuple[float, float]:
    """Compute max X/Y from stations, section boxes, route waypoints, and headers."""
    all_stations = _drawn_stations(graph, debug)

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

    if header_placements:
        # Scoped to wrapped (multi-line) headers only.  Folding a
        # single-line header's overhang in as well would move everything
        # anchored to the canvas bounds (watermark, legend) for every
        # section carrying a header, not just the ones whose title
        # actually reaches past the box.
        for placement in header_placements.values():
            if len(placement.label_lines) > 1:
                max_x = max(max_x, placement.keepout[2])
                max_y = max(max_y, placement.keepout[3])

    return max_x, max_y


_CANVAS_MARGIN_MOVES = 3
"""Times a render may move the map to clear the left and top canvas margins.

The first move is measured against runs held against the canvas edge, and
releasing them can uncover a few more pixels of demand, so the measurement is
taken again on the moved geometry.  Each move strictly increases the room those
runs have, so the demand cannot grow indefinitely; the cap bounds the re-routing
a pathological map can provoke rather than expressing a geometric limit."""


def _outboard_shortfall(envelope: float, ink: float, margin: float) -> float:
    """Room owed to *ink* drawn outboard of *envelope*, towards a zero edge.

    Ink inside the envelope is covered by the margin the envelope itself was
    placed with; ink outside it has only the distance to the canvas edge at
    zero, which is nobody's reservation.
    """
    if ink >= envelope - SAME_COORD_TOLERANCE:
        return 0.0
    return max(0.0, margin - ink)


def _drawn_ink_origin(
    graph: MetroGraph,
    route_polylines: Sequence[Sequence[tuple[float, float]]],
    debug: bool,
) -> tuple[float, float]:
    """Leftmost and topmost coordinate a station or a run is drawn at."""
    stations = _drawn_stations(graph, debug)
    points = [point for polyline in route_polylines for point in polyline]
    return (
        min([point[0] for point in points] + [station.x for station in stations]),
        min([point[1] for point in points] + [station.y for station in stations]),
    )


def _content_origin(
    graph: MetroGraph,
    route_polylines: Sequence[Sequence[tuple[float, float]]],
    debug: bool,
    *,
    padding: float,
) -> tuple[float, float]:
    """Left and top edges of the drawn content, box or run.

    The section boxes frame the content, and a run drawn outside that envelope
    carries the frame out with it -- to exactly where
    :func:`_canvas_margin_shift` seats it, since the two read the same ink.  A
    decoration placed against this boundary therefore sits flush with the
    content whichever of the two defines the edge.

    A map with no boxes has no such frame, and is never moved for one either,
    so its content edge is the canvas padding.  Station markers and labels
    reach past a bare station's coordinate, so reading one as an edge would
    place a decoration inside the content it frames.  This therefore reads
    station coordinates on purpose, unlike :func:`_leftmost_ink`, which reads
    the ink actually painted.
    """
    boxes = [section for section in graph.sections.values() if section.bbox_w > 0]
    if not boxes:
        return padding, padding
    ink_x, ink_y = _drawn_ink_origin(graph, route_polylines, debug)
    return (
        min(ink_x, min(section.bbox_x for section in boxes)),
        min(ink_y, min(section.bbox_y for section in boxes)),
    )


def _canvas_margin_shift(
    graph: MetroGraph,
    route_polylines: Sequence[Sequence[tuple[float, float]]],
    debug: bool,
    *,
    margins: tuple[float, float],
) -> tuple[float, float]:
    """How far to move the map so ink outside the box envelope clears *margins*.

    The canvas is a first-quadrant frame: it grows past the rightmost and
    bottommost ink by a margin, while its left and top edges are pinned at
    zero.  A run outside the section-box envelope on one of those two sides --
    an inter-row return band wrapping left of the first box, a bundle rising
    over the top of one -- therefore has only whatever room the box placement
    happened to leave, and a wide enough bundle is drawn through it and off the
    canvas.  Moving the whole map away from the edge by the shortfall seats that
    run on the line the layout seats a first section against
    (:data:`CANVAS_ORIGIN_MARGIN`), and the far margins absorb the move by
    growing the canvas.

    The top margin is measured from the map's own canvas top
    (``graph.title_band_lift``), which sits below any title/logo band.

    Zero on both axes for a map that draws nothing outside its box envelope.
    """
    boxes = [section for section in graph.sections.values() if section.bbox_w > 0]
    if not boxes:
        return 0.0, 0.0
    ink_x, ink_y = _drawn_ink_origin(graph, route_polylines, debug)
    top = graph.title_band_lift
    return (
        _outboard_shortfall(min(s.bbox_x for s in boxes), ink_x, margins[0]),
        _outboard_shortfall(
            min(s.bbox_y for s in boxes) - top, ink_y - top, margins[1]
        ),
    )


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
    header_placements: dict[str, SectionHeaderPlacement],
    content_origin: tuple[float, float],
) -> tuple[float, float, float, float, bool]:
    """Compute legend position and dimensions.

    Returns (legend_x, legend_y, legend_w, legend_h, show_legend).

    ``content_origin`` is the top-left corner of the drawn content
    (:func:`_content_origin`), which a content-framed keyword anchors against.

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
            legend_x, legend_y, legend_w, legend_h, graph, routes, header_placements
        ):
            warnings.warn(
                f"legend placed at {graph.legend_at} overlaps a section or route.",
                category=LayoutGeometryWarning,
                stacklevel=2,
            )
        return legend_x, legend_y, legend_w, legend_h, show_legend

    pos = legend_position
    gap = LEGEND_GAP
    inset = LEGEND_INSET
    content_left, content_top = content_origin

    # Frame the keyword anchor against the drawn content (default) or the canvas
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
            legend_x, legend_y, legend_w, legend_h, graph, routes, header_placements
        ):
            warnings.warn(
                f"legend pinned at '{pos}' overlaps a section or route.",
                category=LayoutGeometryWarning,
                stacklevel=2,
            )
    elif pos not in ("bottom", "right") and _legend_overlaps_content(
        legend_x, legend_y, legend_w, legend_h, graph, routes, header_placements
    ):
        legend_x = content_left
        legend_y = max_y + gap

    return legend_x, legend_y, legend_w, legend_h, show_legend


def _terminus_stacked_pad(theme: Theme, has_stacked: bool) -> float:
    """Extent a stacked-files icon's back sheet peeks past the nominal edge.

    ``render_files_icon`` offsets the back sheet by ``terminus_width *
    FILES_ICON_OFFSET_RATIO`` along each axis; anything reserving space around
    the drawn icon adds this so the back sheet is covered. Zero for a
    single-sheet icon.
    """
    return theme.terminus_width * FILES_ICON_OFFSET_RATIO if has_stacked else 0.0


@dataclass(frozen=True)
class _TerminusFlowContext:
    """Flow-axis facts for placing a terminus station's icon(s).

    ``section_dir`` derives from ``section``; ``is_vertical_flow`` from
    ``section_dir``; ``is_source`` from the station's incoming edges; and
    ``flow_sign`` from ``section_dir`` and ``is_source``.
    """

    section: Section | None
    section_dir: str
    is_vertical_flow: bool
    is_source: bool
    flow_sign: float


def _terminus_flow_context(station: Station, graph: MetroGraph) -> _TerminusFlowContext:
    """Build the flow-axis context for one of *station*'s terminus icons."""
    section = graph.sections.get(station.section_id) if station.section_id else None
    section_dir = section.direction if section else "LR"
    is_vertical_flow = lanes_run_along_x(section_dir)
    is_source = not graph.edges_to(station.id)
    flow_sign = _terminus_icon_flow_sign(section_dir, is_source)
    return _TerminusFlowContext(
        section=section,
        section_dir=section_dir,
        is_vertical_flow=is_vertical_flow,
        is_source=is_source,
        flow_sign=flow_sign,
    )


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
        stacked_pad = _terminus_stacked_pad(theme, has_stacked)
        icon_half_w = theme.terminus_width / 2 + stacked_pad
        icon_half_h = theme.terminus_height / 2 + stacked_pad

        x_min = min(cx for cx, _ in centers) - icon_half_w
        x_max = max(cx for cx, _ in centers) + icon_half_w
        y_min = min(cy for _, cy in centers) - icon_half_h
        y_max = max(cy for _, cy in centers) + icon_half_h

        # Captions render on whichever flow side the renderer draws them, so
        # extend the box that way to cover them and keep neighbouring labels
        # at a distance. Vertical-flow sources (and flow-reversed sinks) hang
        # the caption above the icon; everything else hangs it below.
        caption_line_count = station.terminus_caption_line_count
        if caption_line_count:
            ctx = _terminus_flow_context(station, graph)
            caption_extent = ICON_NAME_GAP + (
                caption_line_count * theme.label_font_size * ICON_NAME_FONT_SCALE
            )
            if _terminus_caption_hangs_down(ctx.is_vertical_flow, ctx.flow_sign):
                y_max += caption_extent
            else:
                y_min -= caption_extent

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
    baked_mode: str | None = None,
    bare: bool = False,
    inactive_line_ids: frozenset[str] | None = None,
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
    an external rasterizer that cannot parse ``var()``/``light-dark()``) or any consumer
    without CSS custom-property support - pick the concrete mode via the theme.

    ``self_color_scheme``: when True the root ``<svg>`` declares a
    ``color-scheme`` so a standalone file (opened directly or via ``<img>``)
    resolves ``light-dark()`` against the viewer's OS preference.  The value
    is ``light dark`` (adaptive) unless *baked_mode* is set.
    Set False when inlining into a page that owns the theme (the docs site,
    the playground): the SVG then inherits the page's ``color-scheme`` so a
    manual light/dark toggle drives it. No effect for single-palette themes.

    ``baked_mode``: when ``"light"`` or ``"dark"``, the root ``<svg>`` declares
    ``color-scheme: <mode>`` instead of the adaptive ``color-scheme: light dark``.
    This ensures ``light-dark()`` in the chrome CSS resolves to the correct
    palette in rasterizers that respect the OS color-scheme (e.g. one running
    in a dark-mode macOS session).  Set this to the explicit ``--mode`` value
    when rendering for PNG export; leave ``None`` for browser-adaptive SVG
    output.

    ``bare``: when True, omits the title and outer padding so the canvas
    hugs the diagram content.  The attribution watermark is kept.  Use for
    embedding the SVG inside a host page that supplies its own frame and
    heading.

    ``inactive_line_ids``: the set of line IDs to render muted grey.  ``None``
    (default) resolves the map's own declared-inactive set
    (``graph.default_inactive_line_ids()``), so a ``%%metro line: ... inactive``
    directive takes effect.  An explicit set overrides that declaration for this
    render; an empty set forces every line active.
    """
    if not graph.stations:
        return '<svg xmlns="http://www.w3.org/2000/svg"></svg>'

    if animate is None:
        animate = graph.animate

    effective_inactive = graph.resolve_inactive_line_ids(inactive_line_ids)

    metrics_face = metrics_face_for_portability(font_portability)
    with class_prefix_context(svg_class_prefix):
        plan = build_render_plan(
            graph,
            theme,
            width=width,
            height=height,
            padding=padding,
            debug=debug,
            legend_position=legend_position,
            chrome_css=chrome_css,
            bare=bare,
            metrics_face=metrics_face,
            inactive_line_ids=effective_inactive,
        )
        svg = emit_render_plan(
            plan,
            animate=animate,
            responsive=responsive,
            inject_dark_mode_css=inject_dark_mode_css,
            self_color_scheme=self_color_scheme,
            baked_mode=baked_mode,
        )

    from nf_metro.render.font_embed import apply_font_portability

    return apply_font_portability(svg, font_portability)


class ObservedRenderPlan(NamedTuple):
    """A RenderPlan paired with the final production routing observation."""

    plan: RenderPlan
    route_plan: RoutePlan


@dataclass(frozen=True, slots=True)
class _FinalPublishedGeometry(FinalCanvasGeometry):
    """One source for every geometry-bearing value published by RenderPlan."""

    theme: Theme
    metrics_face: MetricsFace
    edge_route_indices: tuple[int, ...]
    bridge_breaks: Sequence[Sequence[BridgeBreak]]
    labels: Sequence[LabelPlacement]
    header_placements: Mapping[str, SectionHeaderPlacement]
    group_bands: Sequence[_GroupBand]
    positive_fan_sections: frozenset[str]
    padding: float
    legend_x: float
    legend_y: float
    legend_w: float
    legend_h: float
    show_legend: bool
    show_logo: bool
    logo_in_legend: bool
    adaptive_logo: bool
    effective_logo: str
    resolved_logo_light: str
    resolved_logo_dark: str
    logo_x: float
    logo_y: float
    logo_w: float
    logo_h: float
    title_x: float
    title_y: float
    legend_logo_size: tuple[float, float] | None
    manifest: Mapping[str, object] | None
    debug: bool
    chrome_css: bool
    bare: bool
    draws_standalone_title: bool


_FINAL_RENDER_PLAN_FINGERPRINT_SOURCES = {
    "theme": "published.theme",
    "graph": "graph",
    "metrics_face": "published.metrics_face",
    "station_offsets": "station_offsets",
    "routes": "routes",
    "route_polylines": "published.route_polylines",
    "route_curve_radii": "published.route_curve_radii",
    "route_segment_shifts": "published.route_segment_shifts",
    "edge_route_indices": "published.edge_route_indices",
    "bridge_breaks": "published.bridge_breaks",
    "labels": "published.labels",
    "header_placements": "published.header_placements",
    "group_bands": "published.group_bands",
    "positive_fan_sections": "published.positive_fan_sections",
    "svg_width": "published.width",
    "svg_height": "published.height",
    "padding": "published.padding",
    "legend_x": "published.legend_x",
    "legend_y": "published.legend_y",
    "legend_w": "published.legend_w",
    "legend_h": "published.legend_h",
    "show_legend": "published.show_legend",
    "show_logo": "published.show_logo",
    "logo_in_legend": "published.logo_in_legend",
    "adaptive_logo": "published.adaptive_logo",
    "effective_logo": "published.effective_logo",
    "resolved_logo_light": "published.resolved_logo_light",
    "resolved_logo_dark": "published.resolved_logo_dark",
    "logo_x": "published.logo_x",
    "logo_y": "published.logo_y",
    "logo_w": "published.logo_w",
    "logo_h": "published.logo_h",
    "title_x": "published.title_x",
    "title_y": "published.title_y",
    "legend_logo_size": "published.legend_logo_size",
    "manifest": "published.manifest",
    "debug": "published.debug",
    "chrome_css": "published.chrome_css",
    "bare": "published.bare",
    "draws_standalone_title": "published.draws_standalone_title",
    "inactive_line_ids": "inactive_line_ids",
    "directional_chevrons": "directional_chevrons",
}


def _build_render_plan_result(
    graph: MetroGraph,
    theme: Theme,
    width: int | None = None,
    height: int | None = None,
    padding: float = CANVAS_PADDING,
    *,
    debug: bool = False,
    legend_position: str | None = None,
    chrome_css: bool = True,
    bare: bool = False,
    metrics_face: MetricsFace = MetricsFace.FALLBACK,
    inactive_line_ids: frozenset[str] = frozenset(),
) -> tuple[RenderPlan, RoutePlan]:
    """Build a render plan alongside the routing observation that settled it."""
    scaled_theme = _scale_theme_strokes(
        _scale_theme_fonts(theme, graph.font_scale), graph.stroke_scale
    )
    with (
        font_scale_context(graph.font_scale),
        stroke_scale_context(graph.stroke_scale),
        metrics_face_context(metrics_face),
    ):
        try:
            plan, route_plan = _build_render_plan_scaled(
                graph,
                scaled_theme,
                width=width if width is not None else graph.width,
                height=height if height is not None else graph.height,
                padding=padding,
                debug=debug,
                legend_position=legend_position,
                chrome_css=chrome_css,
                bare=bare,
                inactive_line_ids=inactive_line_ids,
            )
        except (
            ConvergenceInvariantError,
            CurveInvariantError,
            ExitTurnInvariantError,
            FanRouteInvariantError,
            LayoutInvariantError,
            SectionHeaderClashError,
        ) as exc:
            reframed = _fold_threshold_error(graph)
            if reframed is not None:
                raise reframed from exc
            raise

    if _fold_back_under_compression(graph):
        reframed = _fold_threshold_error(graph)
        if reframed is not None:
            raise reframed
    return plan, route_plan


def build_render_plan(
    graph: MetroGraph,
    theme: Theme,
    width: int | None = None,
    height: int | None = None,
    padding: float = CANVAS_PADDING,
    *,
    debug: bool = False,
    legend_position: str | None = None,
    chrome_css: bool = True,
    bare: bool = False,
    metrics_face: MetricsFace = MetricsFace.FALLBACK,
    inactive_line_ids: frozenset[str] = frozenset(),
) -> RenderPlan:
    """Build an immutable render plan without changing the caller's graph."""
    plan, _route_plan = _build_render_plan_result(
        graph,
        theme,
        width,
        height,
        padding,
        debug=debug,
        legend_position=legend_position,
        chrome_css=chrome_css,
        bare=bare,
        metrics_face=metrics_face,
        inactive_line_ids=inactive_line_ids,
    )
    return plan


def build_observed_render_plan(
    graph: MetroGraph,
    theme: Theme,
    width: int | None = None,
    height: int | None = None,
    padding: float = CANVAS_PADDING,
    *,
    debug: bool = False,
    legend_position: str | None = None,
    chrome_css: bool = True,
    bare: bool = False,
    metrics_face: MetricsFace = MetricsFace.FALLBACK,
) -> ObservedRenderPlan:
    """Build a plan and observe the final centred route invocation it used."""
    plan, route_plan = _build_render_plan_result(
        graph,
        theme,
        width,
        height,
        padding,
        debug=debug,
        legend_position=legend_position,
        chrome_css=chrome_css,
        bare=bare,
        metrics_face=metrics_face,
    )
    return ObservedRenderPlan(plan, route_plan)


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
        f"fold_threshold={graph.layout_provenance.effective_fold_threshold} "
        "is too small for "
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

    The terminus icon's own footprint (width/height/fold/corner-radius)
    scales alongside its font: the icon-body label shrinks to fit inside
    the icon otherwise, defeating most of the intended enlargement.
    ``layout.pass_metrics.terminus_width_approx()`` /
    ``icon_half_height_approx()`` track the same multiplier so the space
    reserved around a terminus station keeps pace with the bigger icon.
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
        terminus_width=theme.terminus_width * scale,
        terminus_height=theme.terminus_height * scale,
        terminus_fold_size=theme.terminus_fold_size * scale,
        terminus_corner_radius=theme.terminus_corner_radius * scale,
    )


def _scale_theme_strokes(theme: Theme, scale: float) -> Theme:
    """Return a theme with every stroke weight multiplied by ``scale``.

    The station pill radius scales with the strokes so a coarsened map keeps its
    marker proportions.  Layout reserves label clearance and marker footprints
    through ``station_radius_approx()``, which tracks the same scale, so the
    grown pill is reserved for rather than eating that clearance.
    """
    if scale == 1.0:
        return theme
    return replace(
        theme,
        line_width=theme.line_width * scale,
        station_radius=theme.station_radius * scale,
        station_stroke_width=theme.station_stroke_width * scale,
    )


def _route_segment_signature(
    points: list[tuple[float, float]],
) -> tuple[tuple[str, int, int], ...]:
    """Describe a route's turns without retaining absolute coordinates."""
    result: list[tuple[str, int, int]] = []
    for start, end in zip(points, points[1:], strict=False):
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        x_sign = 0 if abs(dx) <= SAME_COORD_TOLERANCE else (1 if dx > 0 else -1)
        y_sign = 0 if abs(dy) <= SAME_COORD_TOLERANCE else (1 if dy > 0 else -1)
        kind = "point" if not x_sign and not y_sign else "segment"
        result.append((kind, x_sign, y_sign))
    return tuple(result)


def _route_decision_fingerprint(routes: list[RoutedPath]) -> tuple[object, ...]:
    """Route topology and ownership facts a settlement translation cannot change.

    Corner radii are deliberately absent: a re-route across widened gaps sizes
    each formed curve from the runway it actually has, and holding it to the
    pre-settlement radius would undo exactly that.
    """
    return tuple(
        (
            route.edge.source,
            route.edge.target,
            route.line_id,
            route.is_inter_section,
            _route_segment_signature(route.points),
            route.offset_regime,
            route.normalize_exempt,
            tuple(route.gap_slots),
            route.trunk_slot is not None,
            route.route_system_id,
            route.emission_member_id,
            route.route_system_disposition,
            route.route_plan_ids,
            route.route_system_owned_segment_ranks,
            route.exit_turn_plan_id,
            route.exit_turn_member_id,
            route.exit_turn_family_id,
            route.exit_turn_axis_id,
            route.fan_plan_id,
            route.fan_route_emitter,
            route.convergence_plan_id,
            route.convergence_member_id,
            route.convergence_owned_segment_ranks,
            route.exit_turn_segment_rank,
            route.exit_lane_transition_plan_id,
            route.source_turnout,
        )
        for route in routes
    )


def _member_geometry_decision(
    plan: RouteMemberGeometryPlan,
) -> tuple[object, ...]:
    """Member-owned geometry without translated coordinates or resources."""
    return (
        plan.id,
        plan.system_id,
        plan.member_id,
        plan.edge,
        plan.connector_ids,
        plan.family_id,
        _route_segment_signature(list(plan.points)),
        plan.offset_regime,
        plan.normalize_exempt,
        plan.gap_slots,
        plan.trunk_slot is not None,
        tuple(
            (
                channel.segment_rank,
                channel.gap_lo_col,
                channel.row,
                channel.direction,
            )
            for channel in plan.gap_channels
        ),
        plan.owned_segment_ranks,
        plan.exit_turn_plan_id,
        plan.exit_turn_member_id,
        plan.exit_turn_family_id,
        plan.exit_turn_axis_id,
        plan.exit_turn_segment_rank,
        plan.exit_lane_transition_plan_id,
        plan.source_turnout,
        plan.fan_plan_id,
        plan.fan_route_emitter,
        plan.coordinate_regime,
    )


def _route_system_decision(system: RouteSystem) -> RouteSystem:
    """Exclude resource indexes while retaining semantic system membership."""
    return replace(
        system,
        shared_reference_ids=(),
        demand_ids=(),
        reservation_ids=(),
    )


def _exit_turn_decision(plan: ExitTurnPlan) -> tuple[object, ...]:
    """An exit-turn decision without its translated coordinates.

    Runway lengths are measured distances between launch points and turn
    axes, so like curve radii they re-derive from wherever the reserved
    corridor actually seats the axis; only the ownership and ordering facts
    around them are decisions.
    """
    source_lanes = tuple(
        (
            item.line_id,
            item.rank,
            item.member_ids,
            item.station_ids,
            item.planned_offset,
        )
        for item in plan.source_lanes
    )
    lane_transitions = tuple(
        (
            item.edge,
            item.claimant_member_ids,
            item.source_offset,
            item.target_offset,
            item.source_lane_offset,
            item.target_lane_offset,
            item.run_direction,
            item.placement,
            item.coordinate_regime,
        )
        for item in plan.lane_transitions
    )
    axes = tuple(
        (
            item.id,
            item.line_id,
            item.axis,
            item.rank,
            item.claimant_member_ids,
            item.fixed_anchor_id,
            item.fixed_anchor_offset,
            item.coordinate_regime,
        )
        for item in plan.axes
    )
    assignments = tuple(
        (
            item.member_id,
            item.entry_group_id,
            item.destination_section_id,
            item.destination_column,
            item.destination_row,
            item.destination_side,
            item.source_lane_rank,
            item.planned_family_id,
            item.roles,
            item.run_direction,
            item.turn_direction,
            item.handedness,
            item.axis_id,
        )
        for item in plan.assignments
    )
    return (
        plan.id,
        plan.system_id,
        plan.exit_group_id,
        plan.exit_port_id,
        plan.divergence_id,
        plan.source_id,
        plan.source_run_direction,
        plan.source_axis,
        plan.connector_ids,
        plan.system_member_ids,
        plan.member_ids,
        source_lanes,
        plan.lane_order_source,
        lane_transitions,
        axes,
        assignments,
        plan.unclassified_member_ids,
        plan.spacing,
        plan.disposition,
        plan.legacy_reason,
        plan.provenance,
    )


def _convergence_decision(plan: ConvergencePlan) -> tuple[object, ...]:
    """Convergence ownership and ordering without absolute geometry.

    The recorded conflict is not part of the decision: its kind is a function
    of ``legacy_reason``, and its sites and separation are measured on
    whichever frame the planner last saw, which a translation legitimately
    moves.
    """
    trunk = (
        None
        if plan.trunk_axis is None
        else (
            plan.trunk_axis.axis,
            plan.trunk_axis.direction,
            plan.trunk_axis.coordinate_regime,
        )
    )
    landings = tuple(
        (
            item.member_id,
            item.edge,
            item.source_junction_id,
            item.approach_axis,
            item.approach_direction,
            item.source_column,
            item.source_row,
            item.lane_rank,
            item.order,
            item.corner_handedness,
            item.opening_turn_coordinate is not None,
            item.bypass,
            item.long_haul,
            item.multiple_row,
        )
        for item in plan.landings
    )
    continuations = tuple(
        (
            item.member_id,
            item.edge,
            item.entry_port_id,
            item.lane_rank,
            item.covered_by_member_id,
        )
        for item in plan.outgoing_continuations
    )
    endpoint_ownership = tuple(
        (
            item.member_id,
            item.edge,
            item.connector_ids,
            item.role,
            item.covered_by_member_id,
        )
        for item in plan.endpoint_ownership
    )
    return (
        plan.id,
        plan.system_id,
        plan.convergence_ids,
        plan.entry_group_ids,
        plan.merge_junction_ids,
        plan.target_entry_port_ids,
        plan.connector_ids,
        plan.member_ids,
        plan.resolved_member_paths,
        plan.resolved_member_edges,
        plan.line_ids,
        plan.upstream_exit_turn_plan_ids,
        plan.upstream_fan_plan_ids,
        plan.primary_trunk_member_id,
        plan.primary_trunk_reason,
        trunk,
        landings,
        continuations,
        plan.lane_order,
        endpoint_ownership,
        plan.disposition,
        plan.legacy_reason,
    )


def _plan_decision_fingerprint(plan: RoutePlan) -> tuple[object, ...]:
    """The immutable semantic decisions in a route observation."""
    return (
        tuple(_route_system_decision(item) for item in plan.systems),
        plan.endpoint_groups,
        plan.divergences,
        plan.convergences,
        plan.members,
        plan.branches,
        plan.feeders,
        tuple(_exit_turn_decision(item) for item in plan.exit_turn_plans),
        plan.fan_plans,
        tuple(_convergence_decision(item) for item in plan.convergence_plans),
        tuple(_member_geometry_decision(item) for item in plan.member_geometry_plans),
        plan.bindings,
        plan.provenance,
        plan.boundary_clearance_owner_ids,
    )


def _assert_settlement_decisions_frozen(
    frozen_routes: list[RoutedPath],
    frozen_plan: RoutePlan,
    realised_routes: list[RoutedPath],
    realised_plan: RoutePlan,
) -> None:
    """Reject a settlement re-route that changed semantic routing.

    Settlement may translate coordinates and nothing else: plan membership,
    dispositions, lane orders, and turn, fan, convergence, and bundle frames
    must all survive.  Comparing decision fingerprints makes that a checked
    invariant of every settled render rather than a design intention.
    """
    if _route_decision_fingerprint(realised_routes) != _route_decision_fingerprint(
        frozen_routes
    ):
        raise LayoutInvariantError(
            "envelope settlement changed route topology or geometry ownership"
        )
    if _plan_decision_fingerprint(realised_plan) != _plan_decision_fingerprint(
        frozen_plan
    ):
        raise LayoutInvariantError(
            "envelope settlement changed route-system planning decisions"
        )


def _attach_published_reservation_attribution(
    routes: list[RoutedPath], plan: RoutePlan
) -> None:
    """Attach the adopted ledger's claimant-exact reservations to each route."""
    attach_reservation_ids_to_routes(routes, plan.reservations)
    validate_published_route_attribution(routes, plan)


def _ledger_changes_live_derived_band(
    graph: MetroGraph,
    plan: RoutePlan,
    routes: list[RoutedPath],
    coordinate_translations: tuple[ReservationCoordinateTranslation, ...] = (),
) -> bool:
    """Whether consuming *plan* can change any route-system placement.

    Gap corridors compare the ledger bands with the bands derived from the live
    route legs.  Canvas bypasses and top-entry columns also consume frozen
    route-system state before a claim segment exists, while standard L-shaped
    columns consume the reserved band's centre.  Those placements therefore
    need their route-family checks in addition to the segment comparisons.
    """
    if any(
        (
            isinstance(reservation.region, CanvasRegion)
            and not BYPASS_ROUTE_FAMILIES.isdisjoint(reservation.route_family_ids)
        )
        or (
            isinstance(reservation.region, ColumnGapRegion)
            and RouteFamilyId.TOP_ENTRY_L_SHAPE in reservation.route_family_ids
        )
        for reservation in plan.reservations
    ):
        return True
    corridors = build_reserved_corridors(graph, plan)
    spans: dict[int, GridSpan | None] = {}
    live_bands: dict[tuple[int, int, CorridorOrientation], GapCorridorBand | None] = {}

    def route_span(path_rank: int) -> GridSpan | None:
        if path_rank in spans:
            return spans[path_rank]
        route = routes[path_rank]
        section_ids = tuple(
            station.section_id
            for station_id in (route.edge.source, route.edge.target)
            if (station := graph.stations.get(station_id)) is not None
            and station.section_id in graph.sections
        )
        spans[path_rank] = (
            grid_span_for_sections(graph, section_ids) if section_ids else None
        )
        return spans[path_rank]

    def live_band(
        path_rank: int, rank: int, orientation: CorridorOrientation
    ) -> GapCorridorBand | None:
        key = (path_rank, rank, orientation)
        if key in live_bands:
            return live_bands[key]
        span = route_span(path_rank)
        if span is None:
            return None
        start, end = routes[path_rank].points[rank : rank + 2]
        horizontal = orientation is CorridorOrientation.HORIZONTAL
        live_bands[key] = gap_corridor_clearance_band(
            graph,
            orientation,
            span,
            start[1] if horizontal else start[0],
            *sorted((start[0], end[0]) if horizontal else (start[1], end[1])),
        )
        return live_bands[key]

    for reservation in plan.reservations:
        region = reservation.region
        if not isinstance(region, RowGapRegion | ColumnGapRegion):
            continue
        boundary = (
            region.lower_row
            if isinstance(region, RowGapRegion)
            else region.right_column
        )
        if (
            isinstance(region, ColumnGapRegion)
            and RouteFamilyId.STANDARD_L_SHAPE in reservation.route_family_ids
            and (band := corridors.columns.at(boundary)) is not None
        ):
            occupied = [claim.allocation_coordinate for claim in reservation.claims]
            if (band.lo + band.hi) / 2 != (min(occupied) + max(occupied)) / 2:
                return True
        for claim in reservation.claims:
            route = routes[claim.path_rank]
            for rank in range(claim.segment_rank, claim.segment_end_rank + 1):
                derived = live_band(claim.path_rank, rank, reservation.orientation)
                ledger = corridors.for_segment(
                    route.edge.source, route.edge.target, route.line_id, rank
                )
                allocation_coordinate = project_reservation_coordinate(
                    claim.allocation_coordinate,
                    DemandAxis.Y
                    if reservation.orientation is CorridorOrientation.HORIZONTAL
                    else DemandAxis.X,
                    claim.member_id,
                    coordinate_translations,
                )
                if ledger is not None and (
                    derived is None
                    or derived.boundary != boundary
                    or derived.lo != ledger.lo
                    or derived.hi != ledger.hi
                    # Only a translation can make a claim's coordinate a
                    # consequence of consuming the ledger.  A run that hugs the
                    # box edge its entry port sits on is outside the clearance
                    # band by construction, so reading an untranslated claim
                    # against the band would re-route for a position the frozen
                    # pass had already chosen.
                    or (
                        allocation_coordinate != claim.allocation_coordinate
                        and not ledger.lo - COORD_TOLERANCE_FINE
                        <= allocation_coordinate
                        <= ledger.hi + COORD_TOLERANCE_FINE
                    )
                ):
                    return True

    for path_rank, route in enumerate(routes):
        if not route.is_inter_section:
            continue
        if route_span(path_rank) is None:
            continue
        for rank, (start, end) in enumerate(zip(route.points, route.points[1:])):
            dx = abs(end[0] - start[0])
            dy = abs(end[1] - start[1])
            if (dx > SAME_COORD_TOLERANCE) == (dy > SAME_COORD_TOLERANCE):
                continue
            horizontal = dx > SAME_COORD_TOLERANCE
            derived = live_band(
                path_rank,
                rank,
                CorridorOrientation.HORIZONTAL
                if horizontal
                else CorridorOrientation.VERTICAL,
            )
            if derived is None:
                continue
            reserved = (corridors.rows if horizontal else corridors.columns).at(
                derived.boundary
            )
            if reserved is not None and (
                reserved.lo != derived.lo or reserved.hi != derived.hi
            ):
                return True
    return False


class _DigestSink:
    """Fold a pickle stream into a digest without retaining the bytes."""

    def __init__(self) -> None:
        self._digest = hashlib.sha256()

    def write(self, data: bytes) -> None:
        self._digest.update(data)

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def _pickled_digest(observed: object) -> str:
    """Digest a tree of primitives and tuples by pickling it into a hash.

    Serialising the tree costs a fraction of rendering it as text, which is
    what a settlement fingerprint would otherwise pay for on every stage.

    The pickler runs without its memo. A memoised stream replaces the second
    occurrence of a value with a back-reference to the first *object*, so it
    names which equal values happen to share storage -- a property of upstream
    allocation rather than of the geometry, and one that moves with
    ``PYTHONHASHSEED``. Un-memoised, equal trees serialise alike.
    """
    sink = _DigestSink()
    pickler = pickle.Pickler(sink, protocol=5)
    pickler.fast = True
    pickler.dump(observed)
    return sink.hexdigest()


def _settlement_geometry_fingerprint(
    graph: MetroGraph,
    station_offsets: dict[tuple[str, str], float],
    routes: list[RoutedPath],
) -> str:
    """Identify the geometry and route ownership visible at a settlement stage."""
    sections = tuple(
        (
            section_id,
            section.bbox_x,
            section.bbox_y,
            section.bbox_w,
            section.bbox_h,
        )
        for section_id, section in sorted(graph.sections.items())
    )
    stations = tuple(
        (station_id, station.x, station.y)
        for station_id, station in sorted(graph.stations.items())
    )
    final_routes = tuple(
        (
            tuple(route.points),
            None if route.curve_radii is None else tuple(route.curve_radii),
            route.route_reservation_ids,
            route.member_geometry_plan_id,
        )
        for route in routes
    )
    observed = (
        sections,
        stations,
        tuple(sorted(station_offsets.items())),
        _route_decision_fingerprint(routes),
        final_routes,
    )
    return _pickled_digest(observed)


# Empty on purpose: a RoutedPath contributes every field it declares, and
# saying so keeps the four record types a single readable table.
_FINAL_ROUTE_FINGERPRINT_EXCLUDED_FIELDS: frozenset[str] = frozenset()
_FINAL_EDGE_FINGERPRINT_EXCLUDED_FIELDS = frozenset({"source_line"})
_FINAL_PLAN_FINGERPRINT_EXCLUDED_FIELDS = frozenset({"settlement_trace"})
_FINAL_GRAPH_FINGERPRINT_EXCLUDED_FIELDS = frozenset(_RENDER_GRAPH_EXCLUDED_FIELDS)


_FINAL_FINGERPRINT_VERBATIM_TYPES = frozenset(
    {type(None), bool, int, float, str, bytes}
)


_FINAL_VALUE_TYPE_IDS: dict[type, str] = {}
_FINAL_FINGERPRINT_FIELD_NAMES: dict[type, tuple[str, ...]] = {}

_FINAL_ENUM_CANONICAL: dict[int, tuple[Enum, object]] = {}


def _final_value_type_id(value_type: type) -> str:
    cached = _FINAL_VALUE_TYPE_IDS.get(value_type)
    if cached is None:
        cached = f"{value_type.__module__}.{value_type.__qualname__}"
        _FINAL_VALUE_TYPE_IDS[value_type] = cached
    return cached


def _final_fingerprint_field_names(value_type: type) -> tuple[str, ...]:
    """The dataclass fields one type contributes, in declaration order."""
    cached = _FINAL_FINGERPRINT_FIELD_NAMES.get(value_type)
    if cached is not None:
        return cached
    excluded = (
        _FINAL_GRAPH_FINGERPRINT_EXCLUDED_FIELDS
        if issubclass(value_type, MetroGraph)
        else _FINAL_PLAN_FINGERPRINT_EXCLUDED_FIELDS
        if issubclass(value_type, RoutePlan)
        else _FINAL_ROUTE_FINGERPRINT_EXCLUDED_FIELDS
        if issubclass(value_type, RoutedPath)
        else _FINAL_EDGE_FINGERPRINT_EXCLUDED_FIELDS
        if issubclass(value_type, Edge)
        else frozenset()
    )
    names = tuple(
        field.name for field in fields(value_type) if field.name not in excluded
    )
    _FINAL_FINGERPRINT_FIELD_NAMES[value_type] = names
    return names


class _CanonicalMemo:
    """The values one canonicalisation pass has already projected.

    Published state is a graph rather than a tree - the same routes, sections
    and plans are reachable from several of an observation's roots - so results
    are keyed by ``id``. ``_alive`` retains every keyed object for the memo's
    lifetime: an object freed mid-pass would let a later one inherit its
    address and, with it, its cached projection.
    """

    __slots__ = ("_alive", "_projected")

    def __init__(self) -> None:
        self._projected: dict[int, object] = {}
        self._alive: list[object] = []

    def projected(self, value: object) -> object | None:
        """The projection already recorded for *value*, or ``None``."""
        return self._projected.get(id(value))

    def record(self, value: object, projection: object) -> object:
        """Record *projection* as *value*'s and return it."""
        self._projected[id(value)] = projection
        self._alive.append(value)
        return projection


def _canonical_final_value(value: object, memo: _CanonicalMemo | None = None) -> object:
    """Project final render state into deterministic, address-free values.

    Runs once per field of every published record, so the per-type facts it
    needs -- the type id and the fields that type contributes -- are cached
    against the type rather than re-derived at each value, and *memo* (when a
    caller shares one across an observation's roots) projects each container
    and record once rather than once per path that reaches it.

    Exact-type tests come before the abstract ones because plain containers
    dominate the walk; a ``Mapping`` or sequence subclass falls through to the
    ``isinstance`` arms below and is projected identically.
    """
    if memo is None:
        memo = _CanonicalMemo()
    value_type = type(value)
    if value_type in _FINAL_FINGERPRINT_VERBATIM_TYPES:
        return value
    if type(value) is tuple or type(value) is list:
        recorded = memo.projected(value)
        if recorded is not None:
            return recorded
        return memo.record(
            value, tuple(_canonical_final_value(item, memo) for item in value)
        )
    if type(value) is dict:
        recorded = memo.projected(value)
        if recorded is not None:
            return recorded
        return memo.record(
            value,
            tuple(
                (
                    _canonical_final_value(key, memo),
                    _canonical_final_value(item, memo),
                )
                for key, item in value.items()
            ),
        )
    if isinstance(value, Enum):
        # Members of two ``(str, Enum)`` classes sharing a value compare equal
        # and hash alike, so the cache is keyed by address.  Each entry holds a
        # reference to the member it was filled from, which pins that address
        # for as long as the entry lives; the identity test states the contract
        # that reference upholds rather than catching a recycled address.
        cached = _FINAL_ENUM_CANONICAL.get(id(value))
        if cached is not None and cached[0] is value:
            return cached[1]
        projection = (
            _final_value_type_id(value_type),
            _canonical_final_value(value.value, memo),
        )
        _FINAL_ENUM_CANONICAL[id(value)] = (value, projection)
        return projection
    if isinstance(value, Mapping):
        return tuple(
            (_canonical_final_value(key, memo), _canonical_final_value(item, memo))
            for key, item in value.items()
        )
    if isinstance(value, (set, frozenset)):
        return tuple(
            sorted((_canonical_final_value(item, memo) for item in value), key=repr)
        )
    if isinstance(value, (list, tuple)):
        return tuple(_canonical_final_value(item, memo) for item in value)
    if is_dataclass(value):
        recorded = memo.projected(value)
        if recorded is not None:
            return recorded
        return memo.record(
            value,
            (
                _final_value_type_id(value_type),
                tuple(
                    (name, _canonical_final_value(getattr(value, name), memo))
                    for name in _final_fingerprint_field_names(value_type)
                ),
            ),
        )
    raise TypeError(
        f"unsupported final fingerprint value type: {_final_value_type_id(value_type)}"
    )


def _final_settlement_geometry_observation(
    graph: MetroGraph,
    station_offsets: dict[tuple[str, str], float],
    routes: list[RoutedPath],
    route_plan: RoutePlan,
    published_geometry: _FinalPublishedGeometry,
) -> tuple[object, ...]:
    """Read every published geometry and planning field after realisation."""
    memo = _CanonicalMemo()
    return (
        _canonical_final_value(graph, memo),
        _canonical_final_value(station_offsets, memo),
        _canonical_final_value(published_geometry, memo),
        _canonical_final_value(routes, memo),
        _canonical_final_value(route_plan, memo),
    )


def _final_settlement_geometry_digest(observed: tuple[object, ...]) -> str:
    """Name one published-state observation for the settlement trace.

    ``_canonical_final_value`` refuses anything but the primitives and tuples it
    projects onto, so the observation is a finite tree that pickles by
    construction.
    """
    return _pickled_digest(observed)


def _assert_final_canvas_read_only_guards(
    graph: MetroGraph,
    station_offsets: dict[tuple[str, str], float],
    routes: list[RoutedPath],
    route_plan: RoutePlan,
    published_geometry: _FinalPublishedGeometry,
) -> None:
    """Run guards that inspect the realised final canvas without changing it."""
    del station_offsets, routes, published_geometry
    assert_canvas_corridors_hold_their_claims(
        route_plan, strict=graph.strict and not graph.permissive
    )


class _SettledRenderGeometry(NamedTuple):
    """What :func:`_settle_render_geometry` derives from a laid-out graph."""

    station_offsets: dict[tuple[str, str], float]
    routes: list[RoutedPath]
    route_polylines: list[list[tuple[float, float]]]
    labels: list[LabelPlacement]
    route_plan: RoutePlan
    settlement: EnvelopeSettlement


def _settle_render_geometry(
    graph: MetroGraph,
    theme: Theme,
    offset_step: float,
    section_y_gap: float,
) -> _SettledRenderGeometry:
    """Route, place labels, and reconcile a header collision for the render.

    Returns settled station offsets, routes, the drawable polylines those routes
    become, labels, the route plan, and the envelope settlement those claims are
    frozen ahead of.  The polylines are built here because the settlement guard
    scores the coordinate a viewer sees, so it and the renderer have to read one
    set of points rather than two derivations of them.

    Label wrapping needs the theme's font/icon metrics, so it runs here rather
    than in ``compute_layout``; when it grows a section's bbox downward it can
    push the lower section's header badge up into the box above.  Only that
    genuine collision is reconciled -- re-settle so routes and labels track the
    shifted sections.

    Routing is observed so its ``RouteReservation`` ledger can drive
    :func:`settle_route_envelopes`, which widens any row or column boundary that
    the settled envelopes left short of what it owes: too narrow for a reserved
    corridor, or below the clearance its facing boxes owe each other after those
    bites out of the gaps.  One translation pays both, so the row gap a label
    wrap ate is restored by the same move that seats a corridor.  Settlement runs
    last of the geometry steps; a translation moves whole rows and columns, so
    routes and labels are derived again from the result.

    The re-route is handed that same ledger.  A boundary settlement widened for
    a corridor is one the router must place that corridor inside, so it reads
    the reserved band back rather than deriving one from the row edges the
    translation just moved.

    Settlement runs exactly once, against one ledger.  Re-routing publishes a
    different ledger -- corridors appear, vanish, and change their required
    width -- so settling again against it would be a fixpoint search over a
    moving constraint set rather than an allocation against fixed demand.  The
    plan published here is therefore the frozen ledger projected through the
    translations, and the closing guard measures that; where the re-routed
    ledger's gap demand differs from it,
    :func:`attach_reroute_ledger_delta` records the difference as a non-blocking
    diagnostic, so a demand this stage cannot chase is named rather than
    invisible.

    Rail-mode sections run a separate layout pipeline whose per-line centrelines
    are anchored during ``compute_layout`` and cannot be re-derived from a
    render-time row shift, so a collision involving one is left for the guard to
    surface rather than reflowed into kinked tracks.

    The Tier-A layout guards judge this result from outside
    (:func:`_assert_settled_geometry_is_drawable`), so they read the final
    settled pass, the only one describing geometry a viewer sees.  Label growth
    legitimately carries a bbox edge past a port, so
    ``carry_ports_with_section_edges`` moves the port with the edge it is
    anchored to and the port guards see a consistent pair.

    A ``group`` caption band below a section grows that section's bottom edge,
    and that edge is the blocker bounding the row corridor beneath it, so the
    growth happens here rather than at draw time: settlement then widens the
    boundary to keep the corridor it certified.  A separate draw-time assertion
    (``_assert_group_band_room_drawn``) covers the box this reservation grows: it
    rejects a band drawn past the room reserved for it.

    Junction coordinates are a function of the ports they join rather than
    independent data, so they are re-derived from the incoming section geometry
    before anything is routed.  That keeps routing self-consistent no matter
    which stage last moved a section: a junction left at a stale Y sits off the
    bundle it belongs to and the connector into it degenerates into a
    sub-radius dogleg.  A graph-wide rail layout anchors its junctions on
    per-line rail Ys instead, by a rule the port-derived placement does not
    reproduce, so it is exempt.
    """

    # Non-empty exactly while a label pass has carried ports off the box edges
    # it grew and no routing pass has been observed against their new positions;
    # `carried_from` holds the edges that pass started from.
    carried_ports: tuple[str, ...] = ()
    carried_from: dict[str, tuple[float, float, float, float]] = {}
    settlement_trace = SettlementStageTrace()

    def _place(
        station_offsets: dict[tuple[str, str], float], routes: list[RoutedPath]
    ) -> list[LabelPlacement]:
        nonlocal carried_ports, carried_from
        icon_obstacles = _compute_icon_obstacles(graph, theme, station_offsets)
        edges = section_box_edges(graph)
        placements = place_labels(
            graph,
            station_offsets=station_offsets,
            icon_obstacles=icon_obstacles,
            routes=routes,
            label_angle=graph.label_angle or 0.0,
        )
        # Seating a label grows its section box outward; a port anchored to the
        # edge that moved travels with it, so the box keeps bounding the
        # geometry its own ports describe.
        carried = carry_ports_with_section_edges(graph, edges)
        if carried:
            carried_ports, carried_from = carried, edges
        return placements

    def _route(
        station_offsets: dict[tuple[str, str], float],
        reservations: RoutePlan | None = None,
        reservation_translations: tuple[ReservationCoordinateTranslation, ...] = (),
        *,
        stage: SettlementStage,
        allow_clearance_requirements: bool = False,
    ) -> tuple[list[RoutedPath], RoutePlan]:
        nonlocal settlement_trace
        observation = observe_route_edges_centred(
            graph,
            station_offsets=station_offsets,
            offset_step=offset_step,
            reservations=reservations,
            reservation_translations=reservation_translations,
            allow_convergence_clearance_requirements=allow_clearance_requirements,
        )
        settlement_trace = register_settlement_stage(
            settlement_trace,
            stage,
            geometry_fingerprint=_settlement_geometry_fingerprint(
                graph, station_offsets, observation.routes
            ),
        )
        return observation.routes, observation.plan

    def _resettle(
        reservations: RoutePlan | None = None,
        reservation_translations: tuple[ReservationCoordinateTranslation, ...] = (),
        *,
        allow_clearance_requirements: bool = False,
    ) -> tuple[dict[tuple[str, str], float], list[RoutedPath], RoutePlan]:
        nonlocal carried_ports
        # The shift moved section-anchored geometry; refresh the bypass-label
        # obstacle boxes (derived from station Ys, read by the router) so the
        # re-route does not seat a bypass corner against a stale box.
        graph.bypass_label_obstacles = _bypass_label_obstacles(graph)
        reanchor_junctions(graph)
        offsets = compute_station_offsets(graph, offset_step=offset_step)
        moved_routes, moved_plan = _route(
            offsets,
            reservations,
            reservation_translations,
            stage=SettlementStage.GENERAL_SETTLEMENT,
            allow_clearance_requirements=allow_clearance_requirements,
        )
        carried_ports = ()
        return offsets, moved_routes, moved_plan

    def _reconcile_carried_ports(
        station_offsets: dict[tuple[str, str], float],
        routes: list[RoutedPath],
        route_plan: RoutePlan,
        labels: list[LabelPlacement],
    ) -> tuple[
        dict[tuple[str, str], float], list[RoutedPath], RoutePlan, list[LabelPlacement]
    ]:
        """Re-observe the routes once, when a label pass has carried a port.

        A route terminates on its port, so a carried port no routing pass has
        seen leaves the drawn run overshooting into the box interior.  The
        re-observation is a single step, not a fixpoint: routing centres a
        station marker on its flat run, and lengthening that run by moving the
        port moves the marker, hence the label, hence the box edge, hence the
        port again.  On some topologies that relation has unit gain, so
        iterating it walks the edge outward without ever settling; a pass whose
        own carry no re-observation follows gives that growth back instead
        (:func:`hold_port_anchored_edges`).
        """
        if carried_ports:
            station_offsets, routes, route_plan = _resettle(
                allow_clearance_requirements=bool(
                    route_plan.boundary_clearance_requirements
                )
            )
            labels = _place(station_offsets, routes)
            assert_render_curve_invariants(graph, routes, station_offsets)
        return station_offsets, routes, route_plan, labels

    reanchor_junctions(graph)
    station_offsets = compute_station_offsets(graph, offset_step=offset_step)
    routes, route_plan = _route(
        station_offsets,
        stage=SettlementStage.DISCOVERY,
        allow_clearance_requirements=True,
    )
    assert_render_curve_invariants(graph, routes, station_offsets)

    labels = _place(station_offsets, routes)
    station_offsets, routes, route_plan, labels = _reconcile_carried_ports(
        station_offsets, routes, route_plan, labels
    )
    _reserve_group_band_space(graph, theme, station_offsets, labels)
    if render_header_collision(graph) and not graph.has_rail_sections:
        station_offsets, routes, route_plan = _resettle(
            allow_clearance_requirements=bool(
                route_plan.boundary_clearance_requirements
            )
        )
        labels = _place(station_offsets, routes)
        assert_render_curve_invariants(graph, routes, station_offsets)
        station_offsets, routes, route_plan, labels = _reconcile_carried_ports(
            station_offsets, routes, route_plan, labels
        )

    frozen_routes = routes
    frozen_plan = route_plan

    # A rail layout pitches its rows to the interchange idiom rather than to
    # ``section_y_gap``, and holds runs between them collinear; widening one of
    # its row boundaries to the declared gap turns a flat run into a staircase.
    # So the clearance a boundary owes is a demand only where the gap is what
    # sets the pitch.
    def _measure_clearance(
        graph: MetroGraph,
    ) -> tuple[BoundaryClearanceDemand, ...]:
        row_demands = (
            ()
            if graph.has_rail_sections
            else measure_row_gap_clearance(graph, section_y_gap)
        )
        return row_demands + measure_boundary_clearance_requirements(
            graph, frozen_plan.boundary_clearance_requirements
        )

    grant_settlement: EnvelopeSettlement | None = None
    settlement = settle_route_envelopes(
        graph, frozen_plan, clearance=_measure_clearance
    )
    if settlement.translations or _ledger_changes_live_derived_band(
        graph,
        frozen_plan,
        frozen_routes,
        settlement.coordinate_translations,
    ):
        grant_settlement = settlement
        pending_clearance = bool(frozen_plan.boundary_clearance_requirements)
        station_offsets, routes, routed_plan = _resettle(
            frozen_plan,
            settlement.coordinate_translations,
        )
        if pending_clearance and routed_plan.boundary_clearance_requirements:
            raise LayoutInvariantError(
                "envelope settlement did not satisfy convergence boundary "
                "clearance requirements"
            )
        labels = _place(station_offsets, routes)
        assert_render_curve_invariants(graph, routes, station_offsets)
        if pending_clearance:
            granted_routes = routes
            granted_plan = routed_plan
            granted_polylines = [
                apply_route_offsets(route, station_offsets) for route in granted_routes
            ]
            containment_requirements = drawn_corridor_clearance_requirements(
                graph, granted_plan, granted_polylines
            )
            settlement = settle_route_envelopes(
                graph,
                granted_plan,
                clearance=lambda live_graph: measure_drawn_corridor_clearance(
                    live_graph, containment_requirements
                ),
            )
            station_offsets, routes, consumed_plan = _resettle(
                granted_plan, settlement.coordinate_translations
            )
            labels = _place(station_offsets, routes)
            assert_render_curve_invariants(graph, routes, station_offsets)
            _assert_settlement_decisions_frozen(
                granted_routes, granted_plan, routes, consumed_plan
            )
            route_plan = attach_reroute_ledger_delta(
                adopt_route_reservation_ledger(
                    granted_plan,
                    graph,
                    coordinate_translations=settlement.coordinate_translations,
                ),
                granted_plan,
                consumed_plan,
            )
            routed_plan = consumed_plan
        else:
            _assert_settlement_decisions_frozen(
                frozen_routes, frozen_plan, routes, routed_plan
            )
        # The published ledger is the one settlement consumed.  Re-observing
        # would let corridors appear, vanish, and change their required width
        # across the very step whose contract is coordinate translation only.
        # The routed observation exists to draw and to prove the decisions
        # frozen.
        if not pending_clearance:
            route_plan = attach_reroute_ledger_delta(
                adopt_route_reservation_ledger(
                    frozen_plan,
                    graph,
                    coordinate_translations=settlement.coordinate_translations,
                ),
                frozen_plan,
                routed_plan,
            )
        # adopt_route_reservation_ledger rebuilds the published plan from the
        # frozen pass, which never saw the re-route's own diagnostics; carry
        # forward the one class that names a decision the re-route was itself
        # forced to discard (_adopt_prior_dispositions).
        adopted_dispositions = (
            ()
            if pending_clearance
            else tuple(
                item
                for item in routed_plan.diagnostics
                if item.code == "exit-turn-disposition-adopted"
            )
        )
        if adopted_dispositions:
            route_plan = replace(
                route_plan, diagnostics=route_plan.diagnostics + adopted_dispositions
            )
        _attach_published_reservation_attribution(routes, route_plan)
    # Settlement measured its corridors against these box edges, so a label
    # pass behind it gives back whatever it grew them by: a port carried past
    # the run that lands on it, on an edge a realised reservation is measured
    # from, would spend clearance the corridor has already been promised.
    if carried_ports:
        hold_port_anchored_edges(graph, carried_from, carried_ports)
    route_plan = attach_settlement_diagnostics(route_plan, settlement)
    if grant_settlement is not None and grant_settlement is not settlement:
        route_plan = attach_settlement_diagnostics(route_plan, grant_settlement)
    route_polylines = [apply_route_offsets(route, station_offsets) for route in routes]
    route_plan = replace(route_plan, settlement_trace=settlement_trace)
    return _SettledRenderGeometry(
        station_offsets,
        routes,
        route_polylines,
        labels,
        route_plan,
        settlement,
    )


def _assert_settled_geometry_is_drawable(
    graph: MetroGraph, settled: _SettledRenderGeometry
) -> None:
    """Judge the settled routes and boxes a renderer is about to draw.

    Deferred route-dependent guards join the Tier-A set here, so every guard
    judges the geometry the renderer is handed rather than an intermediate a
    later step moves; the strictness override preserves hard-fail semantics for
    validation-deferred guards.
    """
    deferred_final_route_guards = bool(
        graph._validate_active and graph._final_route_guards_deferred
    )
    effective_strict = (graph.strict or bool(graph._fold_compressed_sections)) and not (
        graph.permissive
    )
    assert_reservations_are_settled(
        graph,
        settled.route_plan,
        settled.settlement,
        settled.route_polylines,
        strict=effective_strict,
    )
    assert_render_layout_invariants(
        graph,
        settled.routes,
        settled.station_offsets,
        strict=effective_strict or deferred_final_route_guards,
        include_deferred_final=deferred_final_route_guards,
    )
    assert_render_header_clearance(graph, strict=effective_strict)


def _settle_clear_of_the_canvas_margins(
    source_graph: MetroGraph,
    theme: Theme,
    offset_step: float,
    section_y_gap: float,
    *,
    margins: tuple[float, float],
    debug: bool = False,
) -> tuple[MetroGraph, _SettledRenderGeometry]:
    """Settle a copy of *source_graph*, moved clear of the left and top edges.

    Where a run lands is only known once it is routed, so a map that routes
    outside its box envelope is re-derived on a copy moved away from the edge by
    :func:`_canvas_margin_shift`.  The move is rigid, so the re-route answers it
    differently only where a run was being held against the canvas edge, and
    releasing such a run can uncover a little more demand than the first
    measurement saw -- hence a re-measurement rather than a single move.

    The settled-geometry guards read the final pass, the only one describing
    geometry a viewer sees.

    A shortfall under :data:`SAME_COORD_TOLERANCE` is nothing a viewer can
    resolve, and moving for one would spend a re-route on a sub-pixel residue,
    so the measurement settles there rather than at exact zero.
    """
    graph = _copy_graph_for_render(source_graph)
    settled = _settle_render_geometry(graph, theme, offset_step, section_y_gap)
    moved = 0.0, 0.0
    for moves in range(_CANVAS_MARGIN_MOVES + 1):
        shift = _canvas_margin_shift(
            graph, settled.route_polylines, debug, margins=margins
        )
        if max(shift) <= SAME_COORD_TOLERANCE:
            break
        if moves == _CANVAS_MARGIN_MOVES:
            _guard_canvas_margin_settled(
                graph, "render", shift=shift, margins=margins, moves=moves
            )
            break
        moved = moved[0] + shift[0], moved[1] + shift[1]
        graph = _copy_graph_for_render(source_graph)
        translate_graph(graph, *moved)
        settled = _settle_render_geometry(graph, theme, offset_step, section_y_gap)
    _assert_settled_geometry_is_drawable(graph, settled)
    return graph, settled


def _settled_render_graph(source_graph: MetroGraph, theme: Theme) -> MetroGraph:
    """A private copy of *source_graph* carrying the geometry a render draws.

    ``render_svg`` finishes geometry -- label wrapping, the header reconcile,
    envelope settlement, the move clear of the canvas margins -- on a copy, so
    the caller's graph keeps the coordinates ``compute_layout`` left.  Callers
    that need to reason about what was actually drawn ask for the settled copy
    rather than re-deriving those steps.
    """
    scaled_theme = _scale_theme_strokes(
        _scale_theme_fonts(theme, source_graph.font_scale), source_graph.stroke_scale
    )
    section_y_gap = (
        source_graph.section_y_gap
        if source_graph.section_y_gap is not None
        else SECTION_Y_GAP
    )
    with (
        font_scale_context(source_graph.font_scale),
        stroke_scale_context(source_graph.stroke_scale),
    ):
        graph, _settled = _settle_clear_of_the_canvas_margins(
            source_graph,
            scaled_theme,
            graph_offset_step(source_graph, scaled_theme.line_width),
            section_y_gap,
            margins=CANVAS_ORIGIN_MARGIN,
        )
    return graph


def _copy_graph_for_render(source_graph: MetroGraph) -> MetroGraph:
    """Copy mutable render state while sharing frozen parser metadata."""
    immutable_metadata = (
        source_graph.route_topology,
        source_graph.route_resolution,
    )
    return copy.deepcopy(
        source_graph,
        {id(value): value for value in immutable_metadata if value is not None},
    )


def _build_render_plan_scaled(
    source_graph: MetroGraph,
    theme: Theme,
    *,
    width: int | None,
    height: int | None,
    padding: float,
    debug: bool,
    legend_position: str | None,
    chrome_css: bool = True,
    bare: bool = False,
    inactive_line_ids: frozenset[str] = frozenset(),
) -> tuple[RenderPlan, RoutePlan]:
    """Finish render geometry on a private graph copy and freeze the result."""
    effective_legend_position = (
        legend_position if legend_position is not None else source_graph.legend_position
    )

    offset_step = graph_offset_step(source_graph, theme.line_width)
    section_y_gap = (
        source_graph.section_y_gap
        if source_graph.section_y_gap is not None
        else SECTION_Y_GAP
    )
    graph, settled = _settle_clear_of_the_canvas_margins(
        source_graph,
        theme,
        offset_step,
        section_y_gap,
        margins=(0.0, 0.0) if bare else CANVAS_ORIGIN_MARGIN,
        debug=debug,
    )
    station_offsets = settled.station_offsets
    routes = settled.routes
    route_polylines = settled.route_polylines
    labels = settled.labels
    route_plan = settled.route_plan
    # A canvas claim is measured only once the render has sized its canvas, so
    # it needs this projection to land on the geometry the canvas encloses.
    settlement_translations = settled.settlement.coordinate_translations
    route_polylines, route_curve_radii, route_segment_shifts = (
        materialize_source_turnout_paths(
            routes,
            route_polylines,
            default_radius=SVG_CURVE_RADIUS,
        )
    )
    line_priority = {line_id: index for index, line_id in enumerate(graph.lines)}
    edge_routes = sorted(
        routes, key=lambda route: -line_priority.get(route.line_id, -1)
    )
    route_indices = {id(route): index for index, route in enumerate(routes)}
    edge_route_indices = tuple(route_indices[id(route)] for route in edge_routes)
    edge_polylines = [route_polylines[index] for index in edge_route_indices]
    edge_radii = [route_curve_radii[index] for index in edge_route_indices]
    bridges = (
        compute_bridges(
            graph,
            edge_routes,
            edge_polylines,
            curve_radius=SVG_CURVE_RADIUS,
            curve_radii=edge_radii,
        )
        if theme.bridge_glyph
        else {}
    )
    bridge_breaks = tuple(tuple(bridges.get(id(route), ())) for route in edge_routes)

    header_polylines = route_polylines

    # Drawn against the settled coordinates; the box room a below band needs was
    # reserved before settlement, which measured the grown edge.
    group_bands = _resolve_group_bands(graph, theme, station_offsets, labels)
    _assert_group_band_room_drawn(graph, group_bands)

    # Resolve headers against the final section bboxes (label and group
    # reservations above can grow a box, moving where its header sits).
    lettering = HeaderLettering(
        theme.section_label_uppercase, theme.section_label_letter_spacing
    )
    with header_lettering_context(lettering):
        header_placements = resolve_all_section_headers(
            graph,
            theme.section_label_font_size,
            header_polylines,
            theme.title_font_size,
            badges=theme.section_badges,
        )
        _guard_section_headers_clear_routes(header_placements, header_polylines)
        _guard_section_headers_fit_box_width(graph, header_placements)
        _guard_section_headers_hold_the_reserved_band(
            graph,
            header_placements,
            theme.section_label_font_size,
            theme.title_font_size,
        )

    max_x, max_y = _compute_canvas_bounds(graph, routes, debug, header_placements)

    # Group captions can extend below/right of the content; grow the canvas
    # so they are not clipped.
    if group_bands:
        g_max_x, g_max_y = _group_caption_bounds(group_bands, theme)
        max_x = max(max_x, g_max_x)
        max_y = max(max_y, g_max_y)

    # Compute legend and logo dimensions
    adaptive_logo = chrome_css and _is_adaptive_mode(graph)
    show_logo, logo_w, logo_h, effective_logo = _resolve_logo(
        graph, adaptive_logo, theme.mode
    )
    resolved_logo_light = (
        resolve_logo_file(graph.logo_path_light, graph.source_dir)
        if graph.logo_path_light
        else ""
    )
    resolved_logo_dark = (
        resolve_logo_file(graph.logo_path_dark, graph.source_dir)
        if graph.logo_path_dark
        else ""
    )

    logo_in_legend = show_logo and effective_legend_position != "none"
    legend_logo_size = (logo_w, logo_h) if logo_in_legend else None

    # Whether this render draws the map title as standalone chrome: the one
    # predicate that both the canvas-sizing term and the title-draw block read,
    # so the two cannot drift and reintroduce a clipped or unmeasured title.
    draws_standalone_title = not bare and bool(graph.title)

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
        header_placements,
        _content_origin(graph, route_polylines, debug, padding=padding),
    )

    if show_legend:
        max_x = max(max_x, legend_x + legend_w)
        max_y = max(max_y, legend_y + legend_h)

    # Standalone logo positioning (only when no legend to embed it in)
    positive_fan = frozenset(tb_positive_fan_sections(graph))
    directional_chevrons = (
        _directional_chevrons(
            routes,
            route_polylines,
            route_curve_radii,
            dict(zip(edge_route_indices, bridge_breaks, strict=True)),
            _invisible_stations(graph),
            theme,
        )
        if graph.directional
        else ()
    )
    block_left = _leftmost_ink(
        graph,
        theme,
        route_polylines=route_polylines,
        directional_chevrons=directional_chevrons,
        station_offsets=station_offsets,
        positive_fan=positive_fan,
        labels=labels,
        header_placements=header_placements,
        group_bands=group_bands,
        legend_left=legend_x if show_legend else None,
        caption_left=_watermark_x_inset(bare, padding) if graph.caption else None,
        debug=debug,
    )
    block_left = padding if block_left is None else max(block_left, 0.0)
    logo_x = 0.0
    logo_y = 0.0
    standalone_logo_box: tuple[float, float, float, float] | None = None
    if show_logo and not show_legend:
        logo_w *= graph.logo_scale
        logo_h *= graph.logo_scale
        logo_x = block_left
        logo_y = LOGO_Y_STANDALONE
        max_x = max(max_x, logo_x + logo_w)
        standalone_logo_box = (logo_x, logo_y, logo_w, logo_h)

    # Right margin: one padding width in full mode; none in bare mode so the
    # canvas hugs the content.  Bottom margin is always just enough room for
    # the watermark text.
    auto_width = max_x + (0.0 if bare else padding)
    auto_height = max_y + WATERMARK_Y_INSET * 2 + WATERMARK_FONT_SIZE

    title_x, title_y = _title_origin(
        block_left, theme.title_font_size, standalone_logo_box
    )

    # Content bounds ignore the title, so a title wider than the map would be
    # clipped at the right edge: grow the canvas to its true glyph advance.
    if draws_standalone_title:
        title_advance = DEFAULT_TEXT_METRICS.advance(
            graph.title,
            text_style(theme.title_font_size, "bold"),
            TextRole.TITLE,
        )
        auto_width = max(
            auto_width, title_x + title_advance + TITLE_CANVAS_SAFETY_MARGIN
        )

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

    manifest = None
    if graph.embed_manifest:
        boxes: dict[str, dict[str, float]] = {}
        for s in graph.stations.values():
            if s.is_port or s.is_hidden:
                continue
            cx, cy, w, h, rx = station_marker_box(
                graph, theme, s, station_offsets, set(positive_fan)
            )
            boxes[s.id] = {"x": cx, "y": cy, "w": w, "h": h, "rx": rx}
        manifest = build_manifest(
            graph,
            width=svg_width,
            height=svg_height,
            station_radius=theme.station_radius,
            extra_node_data=boxes,
        )

    published_geometry = _FinalPublishedGeometry(
        width=svg_width,
        height=svg_height,
        header_keepouts={
            section_id: CanvasRect(*placement.keepout)
            for section_id, placement in header_placements.items()
        },
        route_polylines=route_polylines,
        route_curve_radii=route_curve_radii,
        route_segment_shifts=route_segment_shifts,
        theme=theme,
        metrics_face=active_metrics_face(),
        edge_route_indices=edge_route_indices,
        bridge_breaks=bridge_breaks,
        labels=labels,
        header_placements=header_placements,
        group_bands=group_bands,
        positive_fan_sections=positive_fan,
        padding=padding,
        legend_x=legend_x,
        legend_y=legend_y,
        legend_w=legend_w,
        legend_h=legend_h,
        show_legend=show_legend,
        show_logo=show_logo,
        logo_in_legend=logo_in_legend,
        adaptive_logo=adaptive_logo,
        effective_logo=effective_logo,
        resolved_logo_light=resolved_logo_light,
        resolved_logo_dark=resolved_logo_dark,
        logo_x=logo_x,
        logo_y=logo_y,
        logo_w=logo_w,
        logo_h=logo_h,
        title_x=title_x,
        title_y=title_y,
        legend_logo_size=legend_logo_size,
        manifest=manifest,
        debug=debug,
        chrome_css=chrome_css,
        bare=bare,
        draws_standalone_title=draws_standalone_title,
    )
    route_plan = realise_route_reservations(
        route_plan,
        graph,
        final_canvas=published_geometry,
        coordinate_translations=settlement_translations,
    )
    final_observation = _final_settlement_geometry_observation(
        graph, station_offsets, routes, route_plan, published_geometry
    )
    final_fingerprint = _final_settlement_geometry_digest(final_observation)
    settlement_trace = register_settlement_stage(
        route_plan.settlement_trace,
        SettlementStage.COHORT_FINAL,
        geometry_fingerprint=final_fingerprint,
    )
    route_plan = replace(route_plan, settlement_trace=settlement_trace)
    _assert_final_canvas_read_only_guards(
        graph, station_offsets, routes, route_plan, published_geometry
    )
    # The read-only guard must leave the geometry where it found it, so the
    # published state is read a second time to check.  Re-observing is cheap
    # and digesting is not, so the digest is re-derived only when the second
    # observation differs, which is the only case the trace rejects.
    revalidated_observation = _final_settlement_geometry_observation(
        graph, station_offsets, routes, route_plan, published_geometry
    )
    settlement_trace = register_settlement_stage(
        settlement_trace,
        SettlementStage.VALIDATION,
        geometry_fingerprint=(
            final_fingerprint
            if revalidated_observation == final_observation
            else _final_settlement_geometry_digest(revalidated_observation)
        ),
    )
    route_plan = replace(route_plan, settlement_trace=settlement_trace)
    frozen_graph = freeze_render_value(graph)
    assert isinstance(frozen_graph, FrozenGraph)
    frozen_theme = freeze_render_value(published_geometry.theme)
    assert isinstance(frozen_theme, FrozenRecord)
    return RenderPlan(
        theme=frozen_theme,
        graph=frozen_graph,
        metrics_face=published_geometry.metrics_face,
        station_offsets=freeze_render_value(station_offsets),
        routes=freeze_render_value(routes),
        route_polylines=freeze_render_value(published_geometry.route_polylines),
        route_curve_radii=freeze_render_value(published_geometry.route_curve_radii),
        route_segment_shifts=tuple(published_geometry.route_segment_shifts),
        edge_route_indices=published_geometry.edge_route_indices,
        bridge_breaks=freeze_render_value(published_geometry.bridge_breaks),
        labels=freeze_render_value(published_geometry.labels),
        header_placements=freeze_render_value(published_geometry.header_placements),
        group_bands=freeze_render_value(published_geometry.group_bands),
        positive_fan_sections=published_geometry.positive_fan_sections,
        svg_width=int(published_geometry.width),
        svg_height=int(published_geometry.height),
        padding=published_geometry.padding,
        legend_x=published_geometry.legend_x,
        legend_y=published_geometry.legend_y,
        legend_w=published_geometry.legend_w,
        legend_h=published_geometry.legend_h,
        show_legend=published_geometry.show_legend,
        show_logo=published_geometry.show_logo,
        logo_in_legend=published_geometry.logo_in_legend,
        adaptive_logo=published_geometry.adaptive_logo,
        effective_logo=published_geometry.effective_logo,
        resolved_logo_light=published_geometry.resolved_logo_light,
        resolved_logo_dark=published_geometry.resolved_logo_dark,
        logo_x=published_geometry.logo_x,
        logo_y=published_geometry.logo_y,
        logo_w=published_geometry.logo_w,
        logo_h=published_geometry.logo_h,
        title_x=published_geometry.title_x,
        title_y=published_geometry.title_y,
        legend_logo_size=published_geometry.legend_logo_size,
        manifest=(
            freeze_render_value(published_geometry.manifest)
            if published_geometry.manifest is not None
            else None
        ),
        debug=published_geometry.debug,
        chrome_css=published_geometry.chrome_css,
        bare=published_geometry.bare,
        draws_standalone_title=published_geometry.draws_standalone_title,
        inactive_line_ids=inactive_line_ids,
        directional_chevrons=directional_chevrons,
    ), route_plan


def emit_render_plan(
    plan: RenderPlan,
    *,
    animate: bool = False,
    animation_frame_slot: bool = False,
    responsive: bool = False,
    inject_dark_mode_css: bool = True,
    dark_mode_css_follows_color_scheme: bool = False,
    self_color_scheme: bool = True,
    baked_mode: str | None = None,
) -> str:
    """Create an SVG string from an immutable render plan.

    ``dark_mode_css_follows_color_scheme`` keys the transparent-theme label
    fallback on the inherited ``color-scheme`` (``light-dark()``) instead of
    the OS-level ``prefers-color-scheme``, for a host that owns the canvas.
    """
    with metrics_face_context(plan.metrics_face):
        return _emit_render_plan(
            plan,
            animate=animate,
            animation_frame_slot=animation_frame_slot,
            responsive=responsive,
            inject_dark_mode_css=inject_dark_mode_css,
            dark_mode_css_follows_color_scheme=dark_mode_css_follows_color_scheme,
            self_color_scheme=self_color_scheme,
            baked_mode=baked_mode,
        )


def _emit_render_plan(
    plan: RenderPlan,
    *,
    animate: bool = False,
    animation_frame_slot: bool = False,
    responsive: bool = False,
    inject_dark_mode_css: bool = True,
    dark_mode_css_follows_color_scheme: bool = False,
    self_color_scheme: bool = True,
    baked_mode: str | None = None,
) -> str:
    graph: Any = plan.graph
    theme: Any = plan.theme
    debug = plan.debug
    chrome_css = plan.chrome_css
    bare = plan.bare
    station_offsets: Any = plan.station_offsets
    routes: Any = plan.routes
    edge_routes: Any = plan.edge_routes
    edge_polylines = tuple(
        plan.route_polylines[index] for index in plan.edge_route_indices
    )
    edge_radii = tuple(
        plan.route_curve_radii[index] for index in plan.edge_route_indices
    )
    bridge_breaks: Any = plan.bridge_breaks
    labels: Any = plan.labels
    header_placements: Any = plan.header_placements
    group_bands: Any = plan.group_bands
    positive_fan: Any = plan.positive_fan_sections
    inactive_line_ids = plan.inactive_line_ids
    svg_width, svg_height = plan.svg_width, plan.svg_height
    padding = plan.padding
    legend_x, legend_y = plan.legend_x, plan.legend_y
    show_legend = plan.show_legend
    show_logo = plan.show_logo
    logo_in_legend = plan.logo_in_legend
    adaptive_logo = plan.adaptive_logo
    effective_logo = plan.effective_logo
    resolved_logo_light = plan.resolved_logo_light
    resolved_logo_dark = plan.resolved_logo_dark
    logo_x, logo_y = plan.logo_x, plan.logo_y
    logo_w, logo_h = plan.logo_w, plan.logo_h
    legend_logo_size = plan.legend_logo_size

    root_attrs: dict[str, str] = {}
    if responsive:
        root_attrs["preserveAspectRatio"] = "xMinYMin meet"
    if self_color_scheme and is_adaptive(theme):
        color_scheme = baked_mode if baked_mode is not None else "light dark"
        root_attrs["style"] = f"color-scheme: {color_scheme}"
    d = draw.Drawing(svg_width, svg_height, **root_attrs)

    if plan.manifest is not None:
        d.append(draw.Raw(manifest_metadata_svg(thaw_render_value(plan.manifest))))

    # Chrome CSS: custom properties so hosts can recolor without re-rendering.
    # Injected before the background rect so browser parsing order is correct.
    if chrome_css:
        _inject_chrome_css(d, theme, bool(inactive_line_ids))

    # Dark-mode CSS for transparent-background themes so that elements
    # rendered directly on the canvas (section labels, number badges,
    # title) remain readable when the browser provides a dark background.
    # Must follow the chrome CSS so the media-query rule wins by source order.
    if inject_dark_mode_css and (
        not theme.background_color or theme.background_color == "none"
    ):
        _inject_dark_mode_style(
            d, theme, follow_color_scheme=dark_mode_css_follows_color_scheme
        )

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
        standalone_logo_box = (
            (logo_x, logo_y, logo_w, logo_h)
            if show_logo and not logo_in_legend
            else None
        )
        if standalone_logo_box is not None:
            if adaptive_logo:
                _render_adaptive_logo(
                    d,
                    resolved_logo_light,
                    resolved_logo_dark,
                    logo_x,
                    logo_y,
                    logo_w,
                    logo_h,
                )
            else:
                _render_logo(d, effective_logo, logo_x, logo_y, logo_w, logo_h)
        if plan.draws_standalone_title:
            d.append(
                draw.Text(
                    graph.title,
                    theme.title_font_size,
                    plan.title_x,
                    plan.title_y,
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
    _render_edges(
        d,
        graph,
        edge_routes,
        edge_polylines,
        edge_radii,
        bridge_breaks,
        theme,
        inactive_line_ids=inactive_line_ids,
    )

    # Directional chevrons ride on top of the lines but behind stations.
    if graph.directional:
        _render_directional_markers(
            d, graph, routes, plan.directional_chevrons, theme, inactive_line_ids
        )

    # Animation (after edges, before stations so balls travel behind station markers)
    if animate:
        from nf_metro.render.animate import render_animation

        render_animation(
            d,
            graph,
            routes,
            station_offsets,
            theme,
            frame_slot=animation_frame_slot,
        )

    # Draw stations (all circles, skip ports)
    label_points: dict[str, tuple[float, float]] = {}
    for label in labels:
        label_points.setdefault(label.station_id, (label.x, label.y))
    _render_stations(
        d,
        graph,
        theme,
        station_offsets,
        positive_fan,
        inactive_line_ids,
        label_points,
    )

    # Draw labels
    _render_labels(d, labels, theme, graph, inactive_line_ids)

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
            inactive_line_ids=inactive_line_ids,
            logo_path=effective_logo if (_in_legend and not adaptive_logo) else None,
            logo_path_light=(
                resolved_logo_light if (adaptive_logo and _in_legend) else None
            ),
            logo_path_dark=(
                resolved_logo_dark if (adaptive_logo and _in_legend) else None
            ),
            logo_size=legend_logo_size,
        )

    # Attribution watermark
    watermark_x_inset = _watermark_x_inset(bare, padding)
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


def _legend_overlaps_headers(
    lx: float,
    ly: float,
    lw: float,
    lh: float,
    header_placements: dict[str, SectionHeaderPlacement],
) -> bool:
    """Check if a legend rectangle overlaps a wrapped section header's keepout.

    A wrapped (multi-line) header can extend below its own section's box
    (``below`` mode) or above it (``above``/``nudge``), reaching outside the
    section bbox that :func:`_legend_overlaps_sections` checks.  A single-line
    header is excluded to match :func:`_compute_canvas_bounds`, which sizes the
    canvas around wrapped headers alone.
    """
    for placement in header_placements.values():
        if len(placement.label_lines) <= 1:
            continue
        kx0, ky0, kx1, ky1 = placement.keepout
        if lx < kx1 and lx + lw > kx0 and ly < ky1 and ly + lh > ky0:
            return True
    return False


def _legend_overlaps_content(
    lx: float,
    ly: float,
    lw: float,
    lh: float,
    graph: MetroGraph,
    routes: list[RoutedPath],
    header_placements: dict[str, SectionHeaderPlacement],
) -> bool:
    """Whether the legend rect overlaps a section box, a header, or a routed line."""
    return (
        _legend_overlaps_sections(lx, ly, lw, lh, graph)
        or _legend_overlaps_headers(lx, ly, lw, lh, header_placements)
        or _legend_overlaps_routes(lx, ly, lw, lh, routes, LEGEND_ROUTE_CLEARANCE)
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


def _effective_logo_path(graph: MetroGraph, mode: str) -> str:
    """Return the logo path appropriate for the resolved display *mode*.

    Used for non-adaptive (static) rendering such as PNG export.  When the
    directive was ``%%metro logo: light.png | dark.png``, the light variant is
    returned for a light *mode* and the dark variant otherwise. *mode* is the
    resolved light/dark display mode - an axis independent of the brand style
    (``%%metro style:``) - so callers should pass a theme's resolved
    ``theme.mode`` rather than ``graph.style``. Falls back to the single-path
    ``logo_path`` for backwards compatibility.
    """
    is_light = mode.strip().lower() == "light"
    if is_light and graph.logo_path_light:
        return graph.logo_path_light
    if not is_light and graph.logo_path_dark:
        return graph.logo_path_dark
    return graph.logo_path


def _watermark_x_inset(bare: bool, padding: float) -> float:
    """Distance of the watermark and the caption from the canvas side edges."""
    return WATERMARK_BARE_X_INSET if bare else padding * WATERMARK_PADDING_RATIO


def _leftmost_ink(
    graph: MetroGraph,
    theme: Theme,
    *,
    route_polylines: Sequence[Sequence[tuple[float, float]]],
    directional_chevrons: Sequence[Sequence[ChevronVertices]],
    station_offsets: dict[tuple[str, str], float],
    positive_fan: set[str] | frozenset[str],
    labels: Sequence[LabelPlacement],
    header_placements: dict[str, SectionHeaderPlacement],
    group_bands: Sequence[_GroupBand],
    legend_left: float | None,
    caption_left: float | None,
    debug: bool,
) -> float | None:
    """Smallest x the settled geometry paints, or ``None`` when it paints nothing.

    Reads every drawn element but the map title, a standalone logo, the
    attribution watermark and the debug overlay: section boxes and headers,
    line strokes, station markers, station labels, group captions, the caption
    and the legend.  A stroke reaches half its width past its path, and a
    rounded corner stays inside the triangle of its tangent points and vertex,
    so a polyline's vertices bound its curves.  Terminus icons are held inside their
    section box by a layout guard, so the box covers them.
    """
    reach: list[float] = []
    for section in graph.sections.values():
        if not section.is_drawn_box:
            continue
        reach.append(section.bbox_x - SECTION_STROKE_WIDTH / 2)
        placement = header_placements[section.id]
        if section.has_badge and theme.section_badges:
            reach.append(placement.badge_cx - SECTION_NUM_CIRCLE_R_LARGE)
        reach.append(
            placement.keepout[0] if placement.label_rotation else placement.label_x
        )
    reach.extend(x - theme.line_width / 2 for line in route_polylines for x, _ in line)
    reach.extend(
        x - _chevron_stroke_width(theme) / 2
        for chevrons in directional_chevrons
        for vertices in chevrons
        for x, _ in vertices
    )
    fan_sections = set(positive_fan)
    for station in _drawn_stations(graph, debug):
        cx, _, width, _, _ = station_marker_box(
            graph, theme, station, station_offsets, fan_sections
        )
        reach.append(cx - width / 2 - theme.station_stroke_width / 2)
    halo = max(theme.label_halo_width, 0.0) / 2
    reach.extend(x - halo for label in labels for x, _ in _label_ink_quad(label))
    caption_style = text_style(theme.label_font_size * GROUP_LABEL_FONT_SCALE, "bold")
    for band in group_bands:
        caption_width = DEFAULT_TEXT_METRICS.advance(
            band.label, caption_style, TextRole.GROUP_CAPTION
        )
        reach.append(band.x_left - GROUP_LABEL_UNDERLINE_WIDTH / 2)
        reach.append((band.x_left + band.x_right - caption_width) / 2)
    reach.extend(x for x in (legend_left, caption_left) if x is not None)
    return min(reach, default=None)


def _title_origin(
    left: float,
    title_font_size: float,
    standalone_logo: tuple[float, float, float, float] | None,
) -> tuple[float, float]:
    """``(x, baseline_y)`` of the map title.

    Top-left at ``left`` on its own; beside a standalone logo (given as
    ``(x, y, w, h)``) it sits to the logo's right, vertically centred on it,
    with its baseline kept low enough that the glyphs never rise off the top.
    """
    if standalone_logo is None:
        return left, title_baseline_y(title_font_size)
    logo_x, logo_y, logo_w, logo_h = standalone_logo
    centred = logo_y + logo_h / 2 + title_font_size * TEXT_VCENTER_EM
    return (
        logo_x + logo_w + TITLE_LOGO_GAP,
        max(centred, title_font_size * TITLE_ASCENT_RATIO),
    )


def _is_adaptive_mode(graph: MetroGraph) -> bool:
    """True when the %%metro logo: directive used pipe syntax (one or both variants)."""
    return bool(graph.logo_path_light) or bool(graph.logo_path_dark)


def _has_adaptive_logos(graph: MetroGraph) -> bool:
    """Return True when both logo variants are set and their files exist."""
    return bool(resolve_logo_file(graph.logo_path_light, graph.source_dir)) and bool(
        resolve_logo_file(graph.logo_path_dark, graph.source_dir)
    )


def _resolve_logo(
    graph: MetroGraph, adaptive: bool, mode: str
) -> tuple[bool, float, float, str]:
    """Return (show_logo, logo_w, logo_h, effective_logo_path).

    ``adaptive`` is True when the %%metro logo: directive used pipe syntax.
    In adaptive mode the dimensions come from whichever variant file exists.
    In single-path mode ``effective_logo`` is the path to render, chosen for
    the resolved display *mode* (see :func:`_effective_logo_path`).
    """
    if adaptive:
        raw_candidates = (graph.logo_path_dark, graph.logo_path_light)
        dim_path = next(
            (resolve_logo_file(p, graph.source_dir) for p in raw_candidates if p),
            "",
        )
        if dim_path:
            w, h = compute_logo_dimensions(dim_path)
            return True, w, h, ""
        if any(raw_candidates):
            raise ValueError(
                f"%%metro logo: path(s) {[p for p in raw_candidates if p]} not found"
            )
        return False, 0.0, 0.0, ""
    effective = _effective_logo_path(graph, mode)
    if not effective:
        return False, 0.0, 0.0, ""
    resolved = resolve_logo_file(effective, graph.source_dir)
    if resolved:
        w, h = compute_logo_dimensions(resolved)
        return True, w, h, resolved
    raise ValueError(f"%%metro logo: path {effective!r} not found")


def compute_logo_dimensions(
    logo_path: str,
    logo_height: float = LOGO_HEIGHT,
) -> tuple[float, float]:
    """Compute logo display dimensions preserving aspect ratio."""
    img = open_logo_image(logo_path)
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
            **logo_image_kwargs(logo_path),
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

    *light_path*/*dark_path* must already be resolved (data URI or an
    existing file path); either may be absent. ``light-dark()`` inherits
    color-scheme from the host document so logos follow a page's dark/light
    toggle, not only the OS media preference.
    """
    dark_mask_id, light_mask_id = _adaptive_logo_mask_ids()
    defs_parts = []
    if dark_path:
        defs_parts.append(
            f'<mask id="{dark_mask_id}" maskContentUnits="objectBoundingBox">'
            f'<rect width="1" height="1" fill="light-dark(#000,#fff)"/>'
            f"</mask>"
        )
    if light_path:
        defs_parts.append(
            f'<mask id="{light_mask_id}" maskContentUnits="objectBoundingBox">'
            f'<rect width="1" height="1" fill="light-dark(#fff,#000)"/>'
            f"</mask>"
        )
    if not defs_parts:
        return
    d.append(draw.Raw(f"<defs>{''.join(defs_parts)}</defs>"))
    if dark_path:
        d.append(
            draw.Image(
                x,
                y,
                logo_w,
                logo_h,
                mask=f"url(#{dark_mask_id})",
                **logo_image_kwargs(dark_path),
            )
        )
    if light_path:
        d.append(
            draw.Image(
                x,
                y,
                logo_w,
                logo_h,
                mask=f"url(#{light_mask_id})",
                **logo_image_kwargs(light_path),
            )
        )


_MUTED_CLASS = "nf-metro-muted"
"""Class marking an element greyed because every line touching it is inactive."""


def _maybe_muted_class(name: str, muted: bool) -> str:
    """Return *name*'s class attribute, carrying the muted marker when *muted*."""
    cls = _ns(name)
    return f"{cls} {_ns(_MUTED_CLASS)}" if muted else cls


def _capsule_adapts(theme: Theme) -> bool:
    """Whether *theme* gives station capsules colours that follow the mode."""
    return bool(theme.capsule_stroke or theme.capsule_fill)


def _capsule_attrs(
    theme: Theme, station_data: dict[str, str], muted: bool
) -> dict[str, Any]:
    """Paint and data attributes of a station capsule or blank-terminus nub."""
    attrs: dict[str, Any] = {
        "fill": theme.capsule_fill or theme.station_fill,
        "stroke": theme.capsule_stroke or theme.station_stroke,
        "stroke_width": theme.station_stroke_width,
        **station_data,
    }
    if _capsule_adapts(theme):
        capsule = _maybe_muted_class("nf-metro-capsule", muted)
        attrs["class_"] = f"{station_data['class_']} {capsule}"
    return attrs


def _inject_chrome_css(d: draw.Drawing, theme: Theme, any_inactive: bool) -> None:
    """Inject CSS custom properties for chrome colors.

    Defines ``--nfm-map-*`` properties on the chrome element classes so a host
    can recolor the map's non-semantic surfaces (background, labels, section
    boxes, legend) by setting those properties on a wrapping element.  The
    fallback for each property is the mode-adaptive ``light-dark()`` of the
    theme's light and dark values (or the baked value for a single-palette
    theme), so the map follows the viewer's ``color-scheme`` with no host
    intervention.  The rules come from the chrome bindings declared on the
    :class:`~nf_metro.render.style.Theme` schema.  Line/route colors carry
    semantic meaning and are baked presentation attributes.

    ``any_inactive`` adds the muted-state rules; a map with no inactive line
    has nothing for them to match, so it ships without them.
    """

    def _rule(cls: str, props: str, *also: str) -> str:
        selector = "".join(f".{_ns(c)}" for c in (cls, *also))
        return f"{selector} {{ {props}; }}"

    rules: dict[str, list[str]] = {}
    for binding in chrome_bindings():
        if binding.emit_if and getattr(theme, binding.emit_if[0]) != binding.emit_if[1]:
            continue
        baked = baked_binding(theme, binding)
        if not baked or baked == "none":
            continue
        value = (
            f"var({binding.css_var}, {adaptive_value(*binding_pair(theme, binding))})"
        )
        for cls, prop in binding.targets:
            rules.setdefault(cls, []).append(f"{prop}: {value}")
    lines = [_rule(cls, "; ".join(props)) for cls, props in rules.items()]
    # The label halo is a knockout of the background, so it tracks --nfm-map-bg
    # and must flip with the mode too; otherwise a dark-baked halo blots out
    # labels in light mode. Disabled-halo themes draw no halo, so emit nothing.
    bg_light, bg_dark = color_pair(theme, "background_color")
    halo_light_set, halo_dark_set = color_pair(theme, "label_halo_color")
    halo_light = _label_halo_color_for(theme.label_halo_width, halo_light_set, bg_light)
    halo_dark = _label_halo_color_for(theme.label_halo_width, halo_dark_set, bg_dark)
    if halo_light is not None and halo_dark is not None:
        halo_val = f"var(--nfm-map-bg, {adaptive_value(halo_light, halo_dark)})"
        lines.append(
            _rule("nf-metro-label-halo", f"fill: {halo_val}; stroke: {halo_val}")
        )
    # A muted element carries its grey as a presentation attribute, which any
    # author rule outranks, so the full-strength rules above would repaint it.
    # Each muted state therefore needs a selector that outranks its own plain
    # rule, and its own property so a host can recolor the two states apart.
    if any_inactive:
        muted_val = (
            "var(--nfm-map-muted-color, "
            f"{adaptive_value(*color_pair(theme, 'muted_line_color'))})"
        )
        lines += [
            _rule("nf-metro-station-label", f"fill: {muted_val}", _MUTED_CLASS),
            _rule("nf-metro-marker-stroke", f"stroke: {muted_val}", _MUTED_CLASS),
        ]
        if theme.terminus_glyph == TERMINUS_BAR:
            lines.append(
                _rule("nf-metro-terminus-bar", f"fill: {muted_val}", _MUTED_CLASS)
            )
        if _capsule_adapts(theme):
            lines += [
                _rule("nf-metro-capsule", f"stroke: {muted_val}", _MUTED_CLASS),
                _rule(
                    "nf-metro-capsule-outline",
                    f"stroke: {muted_val}; fill: {muted_val}",
                    _MUTED_CLASS,
                ),
            ]
        if theme.interchange_glyph == GLYPH_RING:
            lines += [
                _rule("nf-metro-ring", f"stroke: {muted_val}", _MUTED_CLASS),
                _rule("nf-metro-ring-link", f"fill: {muted_val}", _MUTED_CLASS),
            ]
        if muted_adapts(theme):
            lines += [
                _rule(MUTED_STROKE_CLASS, f"stroke: {muted_val}"),
                _rule(MUTED_FILL_CLASS, f"fill: {muted_val}"),
            ]
    d.append(draw.Raw(f"<style>{chr(10).join(lines)}</style>"))


def _inject_dark_mode_style(
    d: draw.Drawing, theme: Theme, *, follow_color_scheme: bool = False
) -> None:
    """Inject CSS for dark-mode browsers viewing a transparent-background SVG.

    When the SVG has no opaque background, elements rendered directly on the
    canvas (section labels, numbered badges, title) can become invisible if the
    browser supplies a dark page background.  A ``prefers-color-scheme: dark``
    media query adjusts those elements so they remain readable.  CSS rules
    override SVG presentation attributes, so we only need class selectors.

    ``follow_color_scheme`` swaps the media query for ``light-dark()`` pairs,
    so the labels track the inherited ``color-scheme`` of a host that paints
    the canvas itself, which the OS preference need not match.
    """
    sl = _ns("nf-metro-section-label")
    sc = _ns("nf-metro-section-num-circle")
    ti = _ns("nf-metro-title")
    if follow_color_scheme:
        label = theme.section_label_color
        css = (
            f".{sl} {{ fill: var(--nfm-map-section-label-color,"
            f" light-dark({label}, #d0d0d0)); }}\n"
            f".{sc} {{ fill: light-dark({theme.station_stroke}, #777777); }}\n"
            f".{ti} {{ fill: var(--nfm-map-title-color,"
            f" light-dark({theme.title_color}, #ffffff)); }}\n"
        )
    else:
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


def _guard_section_headers_fit_box_width(
    graph: MetroGraph,
    placements: dict[str, SectionHeaderPlacement],
) -> None:
    """Fail loudly if a header's wrapped title overhangs its box width."""
    overflowing = check_section_headers_fit_box_width(graph, placements)
    if overflowing:
        raise SectionHeaderOverflowError(
            "section header(s) overhang their box width after wrapping: "
            + ", ".join(sorted(overflowing))
        )


def _guard_section_headers_hold_the_reserved_band(
    graph: MetroGraph,
    placements: dict[str, SectionHeaderPlacement],
    label_font_size: float,
    title_font_size: float,
) -> None:
    """Fail loudly if a caption reaches past the band its own side leaves free."""
    overreaching = check_section_headers_hold_the_reserved_band(
        graph, placements, label_font_size, title_font_size
    )
    if overreaching:
        raise SectionHeaderBandError(
            "section caption(s) reach past the band the layout leaves free on "
            "the side they hang off: " + ", ".join(overreaching)
        )


def _draw_section_badge(
    d: draw.Drawing,
    section: Section,
    placement: SectionHeaderPlacement,
    theme: Theme,
) -> None:
    """Draw the section's number badge; an unnumbered section draws none."""
    if not (section.has_badge and theme.section_badges):
        return
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
        if not section.draws_box:
            continue

        section_lines: set[str] = set()
        for sid in section.station_ids:
            section_lines.update(graph.station_lines(sid))
        section_data = {
            "data-section-id": section.id,
            "data-section-lines": ",".join(sorted(section_lines)),
        }

        box_style: dict[str, object] = {}
        if theme.section_stroke_dasharray:
            box_style["stroke_dasharray"] = theme.section_stroke_dasharray
        d.append(
            draw.Rectangle(
                section.bbox_x,
                section.bbox_y,
                section.bbox_w,
                section.bbox_h,
                rx=theme.section_corner_radius,
                ry=theme.section_corner_radius,
                fill=theme.section_fill,
                stroke=theme.section_stroke,
                stroke_width=theme.section_stroke_width,
                class_=_ns("nf-metro-section-box"),
                **section_data,
                **box_style,
            )
        )

        # Place the header (number badge + title) clear of any route that would
        # otherwise cross it; the resolver never moves a route to do so.
        placement = header_placements[section.id]

        _draw_section_badge(d, section, placement, theme)

        label_kwargs: dict[str, object] = {
            "class": _ns("nf-metro-section-label"),
            "data-section-id": section.id,
        }
        if placement.label_rotation:
            label_kwargs["transform"] = (
                f"rotate({placement.label_rotation} "
                f"{placement.label_x} {placement.label_y})"
            )
        if theme.section_label_letter_spacing:
            label_kwargs["letter_spacing"] = theme.section_label_letter_spacing
        label_lines = placement.label_lines
        if theme.section_label_uppercase:
            label_lines = tuple(line.upper() for line in label_lines)
        section_label_style = text_style(theme.section_label_font_size, "bold")
        d.append(
            draw.Text(
                "\n".join(label_lines),
                theme.section_label_font_size,
                placement.label_x,
                placement.label_y,
                fill=theme.section_label_color,
                font_family=theme.label_font_family,
                font_weight=theme.section_label_font_weight,
                dy=TEXT_VCENTER_DY,
                line_height=(
                    DEFAULT_TEXT_METRICS.line_height(
                        section_label_style, TextRole.SECTION_HEADER
                    )
                    / theme.section_label_font_size
                ),
                **label_kwargs,
            )
        )


class _DrawnPiece(NamedTuple):
    """One piece of a route's stroke, in drawing order.

    A straight run has ``control`` None and lies on polyline leg ``leg``; a
    corner is the circular arc from ``start`` to ``end`` tangent to the legs
    meeting at ``control`` (its vertex), has no ``leg``, and carries ``curved`` from
    :class:`~nf_metro.layout.routing.corners.CornerTangent`.
    """

    start: tuple[float, float]
    end: tuple[float, float]
    control: tuple[float, float] | None = None
    curved: bool = True
    leg: int | None = None


def _drawn_pieces(
    pts: Sequence[tuple[float, float]],
    radii: Sequence[float] | None,
    curve_radius: float = SVG_CURVE_RADIUS,
) -> list[_DrawnPiece]:
    """The straight runs and rounded corners a route's stroke is drawn as.

    Runs and corners alternate, starting and ending with a run; each corner
    meets its neighbouring runs at the shared
    :func:`~nf_metro.layout.routing.corners.curve_tangents` points.  A route
    of fewer than two points has no pieces.
    """
    pts = list(pts)
    resolved = resolve_curve_radii(
        pts, None if radii is None else list(radii), default_radius=curve_radius
    )
    tangents = curve_tangents(pts, resolved)
    starts = [*pts[:1], *(tan.after for tan in tangents)]
    ends = [*(tan.before for tan in tangents), *pts[-1:]]
    pieces: list[_DrawnPiece] = []
    for leg, (start, end) in enumerate(zip(starts, ends[: len(pts) - 1])):
        pieces.append(_DrawnPiece(start, end, leg=leg))
        pieces.extend(
            _DrawnPiece(tan.before, tan.after, tan.corner, tan.curved)
            for tan in tangents[leg : leg + 1]
        )
    return pieces


def _run_breaks(piece: _DrawnPiece, breaks: Sequence[BridgeBreak]) -> list[BridgeBreak]:
    """The bridge breaks on straight run *piece*, in drawing order."""
    sx, sy = piece.start
    return sorted(
        (bk for bk in breaks if bk.seg_index == piece.leg),
        key=lambda bk: (bk.cut_a[0] - sx) ** 2 + (bk.cut_a[1] - sy) ** 2,
    )


def _render_edges(
    d: draw.Drawing,
    graph: MetroGraph,
    routes: list[RoutedPath],
    polylines: Sequence[Sequence[tuple[float, float]]],
    drawable_radii: Sequence[Sequence[float] | None],
    bridge_breaks: tuple[tuple[BridgeBreak, ...], ...],
    theme: Theme,
    curve_radius: float = SVG_CURVE_RADIUS,
    *,
    inactive_line_ids: frozenset[str] = frozenset(),
) -> None:
    """Render metro line edges with smooth curves at direction changes."""

    for route, pts, route_radii, breaks in zip(
        routes, polylines, drawable_radii, bridge_breaks, strict=True
    ):
        pts = list(pts)
        route_radii = None if route_radii is None else list(route_radii)
        line = graph.lines.get(route.line_id)
        color = effective_line_color(line, theme, inactive_line_ids)
        style_kw = line_style_kwargs(line.style) if line else {}
        class_name = _ns(f"metro-line-{route.line_id}")
        paint = muted_paint_class(theme, route.line_id in inactive_line_ids, "stroke")
        if paint:
            class_name = f"{class_name} {paint}"

        if breaks:
            _render_bridged_edge(
                d,
                pts,
                route,
                list(breaks),
                color,
                style_kw,
                class_name,
                theme,
                curve_radius,
                route_radii,
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
            for piece in _drawn_pieces(pts, route_radii, curve_radius):
                if piece.control is None:
                    path.L(*piece.end)
                elif piece.curved:
                    _draw_corner(path, piece)
            d.append(path)


def _render_directional_markers(
    d: draw.Drawing,
    graph: MetroGraph,
    routes: Sequence[RoutedPath],
    route_chevrons: Sequence[Sequence[ChevronVertices]],
    theme: Theme,
    inactive_line_ids: frozenset[str] = frozenset(),
) -> None:
    """Draw each route's static chevrons, pointing source to target.

    Kept sparse and subtle by default, in the spirit of a one-way transit
    line's direction-of-travel arrows.
    """
    for route, chevrons in zip(routes, route_chevrons, strict=True):
        line = graph.lines.get(route.line_id)
        color = effective_line_color(
            line, theme, inactive_line_ids, theme.directional_marker_color
        )
        class_name = _ns(f"metro-direction-{route.line_id}")
        paint = muted_paint_class(theme, route.line_id in inactive_line_ids, "stroke")
        if paint:
            class_name = f"{class_name} {paint}"
        for vertices in chevrons:
            _draw_chevron(
                d,
                vertices,
                color,
                _chevron_stroke_width(theme),
                theme.directional_marker_opacity,
                class_name,
                route.line_id,
            )


def _chevron_stroke_width(theme: Theme) -> float:
    return max(theme.line_width * 0.5, 1.0)


# Chords per rounded corner when measuring its arc length; the chevron is
# then placed on the exact curve, so this bounds only the spacing error.
_CORNER_ARC_CHORDS = 32


# Positions and offsets (px) closer than this are one point of one drawn
# track: same-line strokes that share ink are laid down from identical
# coordinates, so anything wider apart is a distinct stroke.
_TRACK_TOL = 0.01
# Directions whose dot product is at least this run along one carrier line.
_TRACK_HEADING_COS = 1.0 - 1e-8
# Arc lengths (px) this close to a piece or stretch boundary lie on it.
_ARC_EPS = 1e-9
# A stretch ending this close to its route's end reaches that end.
_ROUTE_END_TOL = 1e-6


@dataclass
class _RouteTrack:
    """One route's drawn stroke, measured by arc length from its start."""

    index: int
    source: str
    target: str
    pieces: list[_DrawnPiece]
    profiles: list[list[tuple[float, float]]]
    starts: list[float]
    gaps: list[tuple[float, float]]
    total: float

    def at(self, arc: float) -> tuple[tuple[float, float], tuple[float, float]] | None:
        """Point and unit heading at *arc*; None at a corner apex with no tangent."""
        for piece, profile, start in zip(
            self.pieces, self.profiles, self.starts, strict=True
        ):
            length = profile[-1][0]
            if length <= 0 or arc > start + length + _ARC_EPS:
                continue
            local = max(arc - start, 0.0)
            arcs = [a for a, _ in profile]
            k = min(max(bisect.bisect_left(arcs, local), 1), len(profile) - 1)
            (arc0, t0), (arc1, t1) = profile[k - 1], profile[k]
            t = t0 + (t1 - t0) * ((local - arc0) / (arc1 - arc0) if arc1 > arc0 else 0)
            point, (dx, dy) = _piece_at(piece, t)
            norm = math.hypot(dx, dy)
            return (point, (dx / norm, dy / norm)) if norm else None
        return None


@dataclass
class _Stretch:
    """A drawn run covered by one fixed set of a line's routes.

    ``spans`` maps each covering route index, ascending, to the arc interval
    that run occupies on that route; every one of them paints the same ink.
    """

    spans: dict[int, tuple[float, float]]

    @property
    def owner(self) -> int:
        return next(iter(self.spans))

    @property
    def length(self) -> float:
        start, end = self.spans[self.owner]
        return end - start


class _PieceSlot(NamedTuple):
    """Where a piece lies: a corner's index, or a carrier's with its ``s`` span."""

    corner: bool
    where: int
    s0: float = 0.0
    s1: float = 0.0


class _AtomKey(NamedTuple):
    """The ink an atom paints: a corner, or the carrier span above cut ``cut``."""

    corner: bool
    where: int
    cut: int


class _Atom(NamedTuple):
    key: _AtomKey
    start: float
    end: float


class _CoverRun(NamedTuple):
    """Consecutive atoms of one route that share a covering set."""

    routes: frozenset[int]
    first: _AtomKey
    start: float
    end: float


# (track, piece, 0 for the piece's start or 1 for its end)
_CutKey = tuple[int, int, int]


def _carrier_of(
    carriers: list[tuple[tuple[float, float], tuple[float, float]]],
    piece: _DrawnPiece,
) -> int:
    """Index of the line carrying straight *piece*, adding one when none does."""
    length = math.dist(piece.start, piece.end)
    ux = (piece.end[0] - piece.start[0]) / length
    uy = (piece.end[1] - piece.start[1]) / length
    for k, ((ox, oy), (vx, vy)) in enumerate(carriers):
        if ux * vx + uy * vy >= _TRACK_HEADING_COS and all(
            abs((px - ox) * vy - (py - oy) * vx) <= _TRACK_TOL
            for px, py in (piece.start, piece.end)
        ):
            return k
    carriers.append((piece.start, (ux, uy)))
    return len(carriers) - 1


def _same_corner(a: _DrawnPiece, b: _DrawnPiece) -> bool:
    assert a.control is not None and b.control is not None
    return all(
        math.dist(p, q) <= _TRACK_TOL
        for p, q in ((a.start, b.start), (a.control, b.control), (a.end, b.end))
    )


def _index_pieces(
    tracks: Sequence[_RouteTrack],
) -> tuple[dict[tuple[int, int], _PieceSlot], dict[int, list[tuple[float, _CutKey]]]]:
    """Each piece's carrier or corner, and the endpoints that may cut a carrier."""
    carriers: list[tuple[tuple[float, float], tuple[float, float]]] = []
    corners: list[_DrawnPiece] = []
    slots: dict[tuple[int, int], _PieceSlot] = {}
    marks: dict[int, list[tuple[float, _CutKey]]] = {}
    for ti, track in enumerate(tracks):
        for pi, (piece, profile) in enumerate(
            zip(track.pieces, track.profiles, strict=True)
        ):
            if profile[-1][0] <= _ARC_EPS:
                continue
            if piece.control is not None:
                same = (c for c, rep in enumerate(corners) if _same_corner(rep, piece))
                if (found := next(same, None)) is None:
                    corners.append(piece)
                    found = len(corners) - 1
                slots[ti, pi] = _PieceSlot(True, found)
                continue
            k = _carrier_of(carriers, piece)
            (ox, oy), (vx, vy) = carriers[k]
            s0, s1 = (
                (x - ox) * vx + (y - oy) * vy for x, y in (piece.start, piece.end)
            )
            slots[ti, pi] = _PieceSlot(False, k, s0, s1)
            marks.setdefault(k, []).extend([(s0, (ti, pi, 0)), (s1, (ti, pi, 1))])
    return slots, marks


def _assign_cuts(
    marks: Mapping[int, list[tuple[float, _CutKey]]],
) -> tuple[dict[_CutKey, int], dict[int, list[float]]]:
    """Merge endpoints within tolerance into cuts; each endpoint's cut index."""
    cut_of: dict[_CutKey, int] = {}
    cuts: dict[int, list[float]] = {}
    for k, found in marks.items():
        reps = cuts.setdefault(k, [])
        for s, owner in sorted(found):
            if not reps or s > reps[-1] + _TRACK_TOL:
                reps.append(s)
            cut_of[owner] = len(reps) - 1
    return cut_of, cuts


def _track_atoms(
    tracks: Sequence[_RouteTrack],
    slots: Mapping[tuple[int, int], _PieceSlot],
    cut_of: Mapping[_CutKey, int],
    cuts: Mapping[int, list[float]],
) -> list[list[_Atom]]:
    """Each route's pieces split at the cuts of its carriers, in arc order."""
    atoms_of: list[list[_Atom]] = []
    for ti, track in enumerate(tracks):
        atoms: list[_Atom] = []
        for pi, (profile, start) in enumerate(
            zip(track.profiles, track.starts, strict=True)
        ):
            slot = slots.get((ti, pi))
            if slot is None:
                continue
            length = profile[-1][0]
            if slot.corner:
                atoms.append(
                    _Atom(_AtomKey(True, slot.where, 0), start, start + length)
                )
                continue
            lo, hi = cut_of[ti, pi, 0], cut_of[ti, pi, 1]
            at = cuts[slot.where]
            scale = length / (slot.s1 - slot.s0) if slot.s1 > slot.s0 else 0.0
            for c in range(lo, hi):
                a = start if c == lo else start + (at[c] - slot.s0) * scale
                b = (
                    start + length
                    if c + 1 == hi
                    else start + (at[c + 1] - slot.s0) * scale
                )
                atoms.append(_Atom(_AtomKey(False, slot.where, c), a, b))
        atoms_of.append(atoms)
    return atoms_of


def _group_stretches(
    tracks: Sequence[_RouteTrack], atoms_of: Sequence[Sequence[_Atom]]
) -> list[_Stretch]:
    """Merge each route's consecutive atoms of one covering set into stretches."""
    members: dict[_AtomKey, set[int]] = {}
    for track, atoms in zip(tracks, atoms_of, strict=True):
        for atom in atoms:
            members.setdefault(atom.key, set()).add(track.index)
    cover = {key: frozenset(routes) for key, routes in members.items()}

    grouped: dict[tuple[tuple[int, ...], _AtomKey], dict[int, tuple[float, float]]] = {}
    for track, atoms in zip(tracks, atoms_of, strict=True):
        runs: list[_CoverRun] = []
        for atom in atoms:
            routes = cover[atom.key]
            if runs and runs[-1].routes == routes:
                runs[-1] = runs[-1]._replace(end=atom.end)
            else:
                runs.append(_CoverRun(routes, atom.key, atom.start, atom.end))
        for run in runs:
            key = (tuple(sorted(run.routes)), run.first)
            grouped.setdefault(key, {})[track.index] = (run.start, run.end)
    stretches = [_Stretch(dict(sorted(spans.items()))) for spans in grouped.values()]
    stretches.sort(key=lambda s: (s.owner, s.spans[s.owner][0]))
    return stretches


def _line_stretches(tracks: Sequence[_RouteTrack]) -> list[_Stretch]:
    """Split one line's routes into stretches of drawn ink with a common cover.

    Straight pieces on one carrier line are cut at every other piece's
    endpoints into atoms, and corners match by their geometry; the routes
    covering each atom are then known, and a route's consecutive atoms with
    the same cover form one stretch.
    """
    slots, marks = _index_pieces(tracks)
    cut_of, cuts = _assign_cuts(marks)
    return _group_stretches(tracks, _track_atoms(tracks, slots, cut_of, cuts))


def _seamless(
    ending: _Stretch, starting: _Stretch, tracks: Sequence[_RouteTrack]
) -> bool:
    """Whether *ending*'s far end and *starting*'s near end are one drawn point.

    They must also share a heading, so the joined run reads as one straight or
    smoothly curving stroke rather than a jog or a turn.
    """
    tail = tracks[ending.owner].at(ending.spans[ending.owner][1])
    head = tracks[starting.owner].at(starting.spans[starting.owner][0])
    if tail is None or head is None:
        return False
    (tp, (tx, ty)), (hp, (hx, hy)) = tail, head
    return math.dist(tp, hp) <= _TRACK_TOL and tx * hx + ty * hy >= _TRACK_HEADING_COS


def _stretches_at_stations(
    stretches: Sequence[_Stretch], tracks: Sequence[_RouteTrack], *, ending: bool
) -> dict[str, list[int]]:
    """Stretch indices that end (or start) at a station, by that station.

    A stretch counts only when every route covering it ends (or starts) there.
    """
    found: dict[str, list[int]] = {}
    for index, stretch in enumerate(stretches):
        members = [tracks[r] for r in stretch.spans]
        if ending:
            reaches = all(
                stretch.spans[t.index][1] >= t.total - _ROUTE_END_TOL for t in members
            )
            stations = {t.target for t in members}
        else:
            reaches = all(stretch.spans[t.index][0] <= _ROUTE_END_TOL for t in members)
            stations = {t.source for t in members}
        if reaches and len(stations) == 1:
            found.setdefault(next(iter(stations)), []).append(index)
    return found


def _joined_runs(
    stretches: Sequence[_Stretch],
    tracks: Sequence[_RouteTrack],
    invisible: frozenset[str],
) -> list[list[_Stretch]]:
    """Chain stretches end to end through a line's invisible port or junction.

    Stretch *a* continues into *b* only where *a* is the single stretch of the
    line ending at that station, *b* the single one starting there, and the
    two meet at one point with one heading. Every other stretch begins and
    ends its own run.
    """
    ending = _stretches_at_stations(stretches, tracks, ending=True)
    starting = _stretches_at_stations(stretches, tracks, ending=False)
    follows: dict[int, int] = {}
    for station, tails in ending.items():
        heads = starting.get(station, [])
        if station in invisible and len(tails) == 1 and len(heads) == 1:
            if _seamless(stretches[tails[0]], stretches[heads[0]], tracks):
                follows[tails[0]] = heads[0]
    continued = set(follows.values())
    runs: list[list[_Stretch]] = []
    seen: set[int] = set()
    for first in [*(i for i in range(len(stretches)) if i not in continued), *follows]:
        if first in seen:
            continue
        chain = [first]
        seen.add(first)
        while chain[-1] in follows and follows[chain[-1]] not in seen:
            chain.append(follows[chain[-1]])
            seen.add(chain[-1])
        runs.append([stretches[i] for i in chain])
    return runs


class _Placed(NamedTuple):
    """A chevron on a run: its position, stretch, owning route and pose."""

    position: float
    stretch: int
    owner: int
    arc: float
    point: tuple[float, float]
    heading: tuple[float, float]


class _Run:
    """A joined run: its stretches laid end to end on one arc-length axis."""

    def __init__(
        self,
        stretches: Sequence[_Stretch],
        tracks: Sequence[_RouteTrack],
        reach: float,
    ) -> None:
        self.stretches = list(stretches)
        self.tracks = tracks
        self.offsets: list[float] = []
        total = 0.0
        for stretch in self.stretches:
            self.offsets.append(total)
            total += stretch.length
        self.length = total
        # The ink of a chevron within *reach* of a bridge gap of any route
        # painting this stretch would cover the over-line the gap reveals.
        self.blocked = [
            (lo - b0 + offset - reach, hi - b0 + offset + reach)
            for stretch, offset in zip(self.stretches, self.offsets, strict=True)
            for route, (b0, b1) in stretch.spans.items()
            for lo, hi in tracks[route].gaps
            if hi > b0 - reach and lo < b1 + reach
        ]

    @property
    def routes(self) -> set[int]:
        return {route for stretch in self.stretches for route in stretch.spans}

    def place(self, targets: Iterable[float]) -> list[_Placed]:
        """A chevron at each unblocked target position along the run.

        A chevron rides the lowest-indexed covering route's own pieces, which
        are the same ink as every other covering route's.
        """
        found = []
        for target in targets:
            if any(lo <= target <= hi for lo, hi in self.blocked):
                continue
            for index, (stretch, offset) in enumerate(
                zip(self.stretches, self.offsets, strict=True)
            ):
                if target <= offset + stretch.length + _ARC_EPS:
                    arc = stretch.spans[stretch.owner][0] + target - offset
                    if (sample := self.tracks[stretch.owner].at(arc)) is not None:
                        found.append(
                            _Placed(target, index, stretch.owner, arc, *sample)
                        )
                    break
        return found


def _route_tracks(
    polylines: Sequence[Sequence[tuple[float, float]]],
    radii: Sequence[Sequence[float] | None],
    routes: Sequence[RoutedPath],
    breaks_by_route: Mapping[int, Sequence[BridgeBreak]],
) -> list[_RouteTrack]:
    """Each route's drawn pieces with arc-length bookkeeping, in route order."""
    tracks: list[_RouteTrack] = []
    for index, (route, pts, route_radii) in enumerate(
        zip(routes, polylines, radii, strict=True)
    ):
        pieces = _drawn_pieces(pts, route_radii)
        profiles = [_arc_profile(piece) for piece in pieces]
        lengths = [profile[-1][0] for profile in profiles]
        tracks.append(
            _RouteTrack(
                index,
                route.edge.source,
                route.edge.target,
                pieces,
                profiles,
                [sum(lengths[:i]) for i in range(len(lengths))],
                _gap_spans(pieces, profiles, breaks_by_route.get(index, ())),
                sum(lengths),
            )
        )
    return tracks


def _line_placements(
    runs: Sequence[_Run], spacing: float, reach: float, min_length: float
) -> list[_Placed]:
    """Every chevron of one line: each run's even set, then cues for bare stretches.

    A stretch the set skips takes a chevron at its middle, longest first, when
    it is long enough for its routes (half a spacing once they are cued by a
    chevron on one of their runs, else one chevron long), keeps half a spacing
    along its run from the run's chevrons, clears the ink of every same-line
    chevron, and is not in a bridge gap.
    """
    on_runs = []
    for run in runs:
        count = int(run.length // spacing)
        start = (run.length - (count - 1) * spacing) / 2
        on_runs.append(run.place(start + i * spacing for i in range(count)))
    cued = {
        route
        for run, on_run in zip(runs, on_runs, strict=True)
        if on_run
        for route in run.routes
    }
    bare = sorted(
        (
            (run_index, index)
            for run_index, run in enumerate(runs)
            for index in range(len(run.stretches))
            if all(p.stretch != index for p in on_runs[run_index])
        ),
        key=lambda c: -runs[c[0]].stretches[c[1]].length,
    )
    half = spacing / 2
    # Same-line chevrons closer than 2*reach would overpaint each other, as at a
    # crossing.
    ink_clear = 2 * reach
    for run_index, index in bare:
        run, on_run = runs[run_index], on_runs[run_index]
        stretch = run.stretches[index]
        if stretch.length < (half if cued.issuperset(stretch.spans) else min_length):
            continue
        middle = run.offsets[index] + stretch.length / 2
        if any(abs(p.position - middle) < half - _ARC_EPS for p in on_run):
            continue
        for chevron in run.place([middle]):
            if all(
                math.dist(chevron.point, other.point) >= ink_clear
                for others in on_runs
                for other in others
            ):
                on_run.append(chevron)
                cued |= run.routes
    return [chevron for on_run in on_runs for chevron in on_run]


def _by_owner(
    placed: Sequence[_Placed], count: int, size: float
) -> tuple[tuple[ChevronVertices, ...], ...]:
    """Chevron vertices on each of *count* routes, in arc order along the route."""
    buckets: list[list[tuple[float, ChevronVertices]]] = [[] for _ in range(count)]
    for chevron in placed:
        vertices = _chevron_vertices(chevron.point, chevron.heading, size)
        buckets[chevron.owner].append((chevron.arc, vertices))
    return tuple(
        tuple(vertices for _, vertices in sorted(bucket, key=lambda p: p[0]))
        for bucket in buckets
    )


def _invisible_stations(graph: MetroGraph) -> frozenset[str]:
    """Ports and junctions: stations a line passes through without a marker."""
    return frozenset(
        {sid for sid, station in graph.stations.items() if station.is_port}
        | graph.junction_ids
    )


def _directional_chevrons(
    routes: Sequence[RoutedPath],
    polylines: Sequence[Sequence[tuple[float, float]]],
    radii: Sequence[Sequence[float] | None],
    breaks_by_route: Mapping[int, Sequence[BridgeBreak]],
    invisible: frozenset[str],
    theme: Theme,
) -> tuple[tuple[ChevronVertices, ...], ...]:
    """Each route's chevrons, riding its stroke exactly as it is drawn.

    *polylines* and *radii* are the drawn route geometry, one entry per route;
    *breaks_by_route* holds each bridged route's gaps by that route index.
    Same-line routes that paint the same ink share one set of chevrons, so no
    two chevrons of a line sit closer than half a spacing along a run.
    """
    size = theme.directional_marker_size
    spacing = max(theme.directional_marker_spacing, 1.0)
    reach = size + _chevron_stroke_width(theme) / 2
    # A route shorter than one chevron carries no useful direction cue.
    min_length = 2 * size

    tracks = _route_tracks(polylines, radii, routes, breaks_by_route)
    by_line: dict[str, list[_RouteTrack]] = {}
    for route, track in zip(routes, tracks, strict=True):
        by_line.setdefault(route.line_id, []).append(track)
    placed: list[_Placed] = []
    for line_tracks in by_line.values():
        runs = [
            _Run(chain, tracks, reach)
            for chain in _joined_runs(_line_stretches(line_tracks), tracks, invisible)
        ]
        placed.extend(_line_placements(runs, spacing, reach, min_length))
    return _by_owner(placed, len(tracks), size)


def _gap_spans(
    pieces: Sequence[_DrawnPiece],
    profiles: Sequence[Sequence[tuple[float, float]]],
    breaks: Sequence[BridgeBreak],
) -> list[tuple[float, float]]:
    """Arc-length interval of each bridge gap along the drawn *pieces*."""
    spans: list[tuple[float, float]] = []
    travelled = 0.0
    for piece, profile in zip(pieces, profiles, strict=True):
        length = profile[-1][0]
        if piece.leg is not None and length > 0:
            (sx, sy), (ex, ey) = piece.start, piece.end
            ux, uy = (ex - sx) / length, (ey - sy) / length
            for bk in _run_breaks(piece, breaks):
                cuts = [
                    travelled + (cx - sx) * ux + (cy - sy) * uy
                    for cx, cy in (bk.cut_a, bk.cut_b)
                ]
                spans.append((min(cuts), max(cuts)))
        travelled += length
    return spans


def _arc_profile(piece: _DrawnPiece) -> list[tuple[float, float]]:
    """``(arc length, curve parameter)`` stations from a piece's start to end."""
    if piece.control is None:
        return [(0.0, 0.0), (math.dist(piece.start, piece.end), 1.0)]
    c1, c2 = corner_arc_controls(piece.start, piece.control, piece.end)
    profile = [(0.0, 0.0)]
    prev = piece.start
    for k in range(1, _CORNER_ARC_CHORDS + 1):
        t = k / _CORNER_ARC_CHORDS
        point = cubic_point(piece.start, c1, c2, piece.end, t)
        profile.append((profile[-1][0] + math.dist(prev, point), t))
        prev = point
    return profile


def _piece_at(
    piece: _DrawnPiece, t: float
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Point and (unnormalised) forward tangent of *piece* at parameter *t*."""
    (ax, ay), (bx, by) = piece.start, piece.end
    if piece.control is None:
        return (ax + (bx - ax) * t, ay + (by - ay) * t), (bx - ax, by - ay)
    return corner_arc_at(piece.start, piece.control, piece.end, t)


def _draw_corner(path: draw.Path, piece: _DrawnPiece) -> None:
    """Append *piece*'s rounded corner to *path* as the arc it bends around."""
    assert piece.control is not None
    c1, c2 = corner_arc_controls(piece.start, piece.control, piece.end)
    path.C(*c1, *c2, *piece.end)


def _chevron_vertices(
    point: tuple[float, float], heading: tuple[float, float], size: float
) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]]:
    """``(wing, apex, wing)`` of an open chevron at *point* pointing along *heading*."""
    px, py = point
    ux, uy = heading
    perp = (-uy, ux)
    apex = (px + ux * size, py + uy * size)
    back = (px - ux * size, py - uy * size)
    wing1 = (back[0] + perp[0] * size, back[1] + perp[1] * size)
    wing2 = (back[0] - perp[0] * size, back[1] - perp[1] * size)
    return wing1, apex, wing2


def _draw_chevron(
    d: draw.Drawing,
    vertices: tuple[tuple[float, float], tuple[float, float], tuple[float, float]],
    color: str,
    stroke_width: float,
    opacity: float,
    class_name: str,
    line_id: str,
) -> None:
    """Draw one open ``>`` chevron through *vertices*."""
    wing1, apex, wing2 = vertices
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
    curve_radii: list[float] | None,
) -> None:
    """Render an under-line interrupted by a short gap at each crossing.

    The line is drawn exactly as the continuous case except that, on every
    straight run carrying a crossing, the pen lifts across a small gap so the
    over-line reads as passing over the top.
    """
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
    for piece in _drawn_pieces(pts, curve_radii, curve_radius):
        if piece.control is not None:
            _draw_corner(path, piece)
            continue
        for bk in _run_breaks(piece, breaks):
            path.L(*bk.cut_a)
            path.M(*bk.cut_b)
        path.L(*piece.end)
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
    raw_min, raw_max = _station_bundle_offset_span(graph, station.id, station_offsets)
    if not graph.station_lines(station.id):
        return 0.0, 0.0
    sec = graph.sections.get(station.section_id) if station.section_id else None
    if sec is not None and sec.direction == "TB":
        sign = 1.0 if station.section_id in positive_fan else -1.0
        drawn = [sign * raw_min, sign * raw_max]
    else:
        drawn = [raw_min, raw_max]
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
    axis = _station_axis(station, is_tb_vert)
    across = max_off - min_off + r * 2
    return axis.box(
        r * 2 if flow_len is None else flow_len,
        axis.base + (min_off + max_off) / 2 - across / 2,
        across,
    )


class _CrossAxis(NamedTuple):
    """Positions across a run of lines at one point along the flow.

    ``along`` is the coordinate along the flow, ``base`` the cross coordinate
    of offset 0, and ``across_x`` says the cross axis runs along x.
    """

    along: float
    base: float
    across_x: bool

    def point(self, offset: float) -> tuple[float, float]:
        """The position *offset* across the lines from ``base``."""
        if self.across_x:
            return self.base + offset, self.along
        return self.along, self.base + offset

    def box(
        self, along_len: float, across_lo: float, across_len: float
    ) -> tuple[float, float, float, float]:
        """``(x, y, w, h)`` of a box centred on ``along`` along the flow and
        spanning ``[across_lo, across_lo + across_len]`` across it."""
        along_lo = self.along - along_len / 2
        if self.across_x:
            return across_lo, along_lo, across_len, along_len
        return along_lo, across_lo, along_len, across_len

    def strip(
        self, a: float, b: float, half: float
    ) -> tuple[float, float, float, float]:
        """``(x, y, w, h)`` of a strip ``2 * half`` thick from offset *a* to *b*."""
        pa = self.point(a)[0 if self.across_x else 1]
        pb = self.point(b)[0 if self.across_x else 1]
        return self.box(2 * half, min(pa, pb), abs(pb - pa))


def _station_axis(station: Station, is_tb_vert: bool) -> _CrossAxis:
    """The cross axis through *station*, offsets measured from its position."""
    if is_tb_vert:
        return _CrossAxis(station.y, station.x, True)
    return _CrossAxis(station.x, station.y, False)


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
    muted: bool,
) -> None:
    """Render a station's terminus icons into their own data-tagged group."""
    icon_group = draw.Group(**{"data-station-id": station.id})
    _render_terminus_icons(
        icon_group, station, graph, theme, r, min_off, max_off, muted
    )
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
    muted: bool,
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
        "class_": f"{station_data['class_']} "
        f"{_maybe_muted_class('nf-metro-marker-stroke', muted)}",
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


class _InterchangePaint(NamedTuple):
    """Colours of an interchange glyph, and whether they follow the mode."""

    interior: str
    outline: str
    adaptive: bool


def _interchange_paint(
    fill_override: str | None, theme: Theme, muted: bool
) -> _InterchangePaint:
    """Paint for an interchange glyph's link bar and knobs.

    An untinted interchange keeps the capsule colours (muted or not, fills
    carry background rather than line identity), and follows the mode when the
    theme gives capsules mode-adaptive colours. A marker tint is a line
    identity, so when the station is muted it greys with the rest of the map,
    overriding the marker-fill exemption; otherwise it keeps the declared tint,
    with the light marker outline so it reads against a dark background.
    """
    if fill_override is None:
        return _InterchangePaint(
            theme.capsule_fill or theme.station_fill,
            theme.capsule_stroke or theme.station_stroke,
            _capsule_adapts(theme),
        )
    return _InterchangePaint(
        theme.muted_line_color if muted else fill_override,
        marker_stroke_color(theme),
        False,
    )


def _draw_interchange_glyph(
    d: draw.Group | draw.Drawing,
    x: float,
    knobs: list[tuple[str, float]],
    theme: Theme,
    r: float,
    *,
    paint: _InterchangePaint,
    station_data: dict[str, str],
    data_station_id: str,
    muted: bool = False,
) -> None:
    """Draw an interchange across the rails in *knobs*, one ``(line_id, y)``
    per rail it stops on: a ring chain under a ``ring`` theme, otherwise the
    knob-and-link glyph."""
    if not knobs:
        return
    if theme.interchange_glyph == GLYPH_RING and len(knobs) > 1:
        ys = [y for _, y in knobs]
        _draw_ring_chain(
            d,
            ring_groups(ys, theme),
            _CrossAxis(x, 0.0, False),
            theme,
            station_data,
            muted,
        )
        return
    _draw_knob_glyph(d, x, knobs, theme, r, paint, station_data, data_station_id, muted)


def _draw_knob_glyph(
    d: draw.Group | draw.Drawing,
    x: float,
    knobs: list[tuple[str, float]],
    theme: Theme,
    r: float,
    paint: _InterchangePaint,
    station_data: dict[str, str],
    data_station_id: str,
    muted: bool,
) -> None:
    """Draw the metro interchange idiom: a knob on each used rail joined by a
    link bar spanning them (the classic metro / nf-core-sarek glyph).

    It is built in two stacked layers so the outline stays continuous where the
    link meets a knob: an outline layer (link bar + discs grown by the stroke
    width) paints the union's outer boundary, then an interior layer at the true
    radii fills it.  The link is the *fill* colour, not the stroke, so it reads
    as joining the knobs rather than cutting across them.
    """
    ys = [y for _, y in knobs]
    top_y, bot_y = min(ys), max(ys)
    sw = theme.station_stroke_width
    # A spanning interchange draws each rail's knob slightly larger than the bare
    # marker, bulging out of a narrower link bar.  A single-knob stop has nothing
    # to bulge from, so it uses the standard marker radius.
    is_spanning = (bot_y - top_y) > SAME_COORD_TOLERANCE
    knob_r = r * RAIL_KNOB_RADIUS_RATIO if is_spanning else r
    bar_half = r * RAIL_LINK_HALF_WIDTH_RATIO

    def _link_bar(width: float, stroke: str, cls: str) -> None:
        # A round-capped line of the given width is a capsule; used here only
        # for its straight body between the top and bottom knobs (the caps are
        # covered by the circles), so it joins them with no seam.
        if not is_spanning:
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
                **{**station_data, "class_": cls},
            )
        )

    def _knobs(radius: float, fill: str, cls: str) -> None:
        for lid, y in knobs:
            d.append(
                draw.Circle(
                    x,
                    y,
                    radius,
                    fill=fill,
                    stroke=fill,
                    stroke_width=0,
                    class_=cls,
                    **{"data-station-id": data_station_id, "data-line-id": lid},
                )
            )

    def _cls(name: str, layer: str, layer_muted: bool = False) -> str:
        if not paint.adaptive:
            return _ns(name)
        return f"{_ns(name)} {_maybe_muted_class(layer, layer_muted)}"

    outline, core = "nf-metro-capsule-outline", "nf-metro-capsule-core"
    _link_bar(
        (bar_half + sw) * 2,
        paint.outline,
        _cls("nf-metro-rail-connector", outline, muted),
    )
    _knobs(
        knob_r + sw, paint.outline, _cls("nf-metro-rail-knob-outline", outline, muted)
    )
    _link_bar(
        bar_half * 2, paint.interior, _cls("nf-metro-rail-connector-interior", core)
    )
    _knobs(knob_r, paint.interior, _cls("nf-metro-rail-knob", core))


def _render_rail_pill(
    d: draw.Drawing,
    graph: MetroGraph,
    station: Station,
    theme: Theme,
    r: float,
    fill_override: str | None = None,
    muted: bool = False,
) -> None:
    """Render a rail-mode multi-rail station as the metro interchange glyph.

    ``fill_override`` tints the interior (link bar + knobs) with a marker fill
    colour while keeping the interchange shape, so a spanning rail station can
    carry its ``%%metro marker:`` colour; a tinted interchange takes the light
    marker outline so the fill reads against the dark background. When ``muted``
    the marker tint greys to ``muted_line_color`` so the interior recedes with
    the rest of the muted map rather than keeping its full-strength colour.
    """
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
        paint=_interchange_paint(fill_override, theme, muted),
        station_data=_station_data_attrs(graph, station),
        data_station_id=station.id,
        muted=muted,
    )


def _render_interchange(
    d: draw.Group | draw.Drawing,
    graph: MetroGraph,
    ic: Interchange,
    theme: Theme,
    station_offsets: dict[tuple[str, str], float] | None,
    r: float,
    muted: bool = False,
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
        paint=_interchange_paint(fill_override, theme, muted),
        station_data=_station_data_attrs(graph, members[0]),
        data_station_id=ic.node_id,
        muted=muted,
    )


def _draw_station_capsule(
    d: draw.Group | draw.Drawing,
    station: Station,
    r: float,
    min_off: float,
    max_off: float,
    station_data: dict[str, str],
    theme: Theme,
    is_tb_vert: bool = False,
    muted: bool = False,
    *,
    rounded: bool = True,
) -> None:
    """Draw the station capsule spanning a bundle, or with *rounded* off a blank
    terminus's sharp-cornered nub."""
    corners: dict[str, float] = {"rx": r, "ry": r} if rounded else {}
    d.append(
        draw.Rectangle(
            *_pill_box(station, r, min_off, max_off, is_tb_vert),
            **corners,
            **_capsule_attrs(theme, station_data, muted),
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
        station.section_id if section is not None and section.draws_box else None
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


def _muted_line_theme(theme: FrozenRecord) -> FrozenRecord:
    """Return *theme* with every line-identity stroke/text set to the muted grey.

    Station stroke, marker outline, terminus icon stroke, and label/terminus
    text resolve to ``muted_line_color`` so a station touched only by inactive
    lines reads as greyed; fills are deliberately untouched (they carry
    background, not line identity).

    A render plan carries its theme as a :class:`FrozenRecord`, which cannot be
    :func:`dataclasses.replace`-d, so rebuild the record with the muted fields.
    """
    muted = theme.muted_line_color
    overrides: dict[str, Any] = {
        "station_stroke": muted,
        "capsule_stroke": muted if theme.capsule_stroke else "",
        "marker_stroke": muted,
        "ring_color": muted,
        "terminus_stroke": muted,
        "terminus_font_color": muted,
        "label_color": muted,
    }
    entries = tuple(
        (key, overrides.get(key, value)) for key, value in theme.values.entries
    )
    return FrozenRecord(kind=theme.kind, values=FrozenMap(entries))


def _render_stations(
    d: draw.Drawing,
    graph: MetroGraph,
    theme: FrozenRecord,
    station_offsets: dict[tuple[str, str], float] | None = None,
    positive_fan: set[str] | None = None,
    inactive_line_ids: frozenset[str] = frozenset(),
    label_points: Mapping[str, tuple[float, float]] | None = None,
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
    theme_view = cast(Theme, theme)
    muted_theme = cast(Theme, _muted_line_theme(theme))
    for station in graph.stations.values():
        if station.is_port or station.is_hidden:
            continue
        muted = station_is_muted(graph, station.id, inactive_line_ids)
        station_theme = muted_theme if muted else theme_view
        label_point = (label_points or {}).get(station.id)
        if graph.embed_manifest:
            attrs = _station_group_attrs(
                graph, theme_view, station, station_offsets, positive_fan
            )
            g = draw.Group(**attrs)
            _render_station_into(
                g,
                graph,
                station_theme,
                station,
                station_offsets,
                positive_fan,
                muted,
                label_point,
            )
            d.append(g)
        else:
            _render_station_into(
                d,
                graph,
                station_theme,
                station,
                station_offsets,
                positive_fan,
                muted,
                label_point,
            )


def _render_station_into(
    d: draw.Group | draw.Drawing,
    graph: MetroGraph,
    theme: Theme,
    station: Station,
    station_offsets: dict[tuple[str, str], float] | None,
    positive_fan: set[str],
    muted: bool,
    label_point: tuple[float, float] | None = None,
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
                _render_interchange(d, graph, ic, theme, station_offsets, r, muted)
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
        _draw_station_capsule(
            d,
            station,
            r,
            t_min,
            t_max,
            _station_data_attrs(graph, station),
            theme,
            muted=muted,
            rounded=False,
        )
        _append_terminus_icons(d, station, graph, theme, r, t_min, t_max, muted)
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
            d,
            graph,
            station,
            theme,
            r,
            _rail_marker_fill(station.marker, theme),
            muted,
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
            muted,
        )
        if station.is_terminus:
            _append_terminus_icons(d, station, graph, theme, r, min_off, max_off, muted)
        return

    bundle = _Bundle(station, min_off, max_off, is_tb_vert)
    if _draw_line_station_glyph(
        d, graph, theme, bundle, station_data, muted, label_point
    ):
        pass
    else:
        _draw_station_capsule(
            d,
            station,
            r,
            min_off,
            max_off,
            station_data,
            theme,
            is_tb_vert,
            muted,
            rounded=not station.is_blank_terminus,
        )

    if station.is_terminus:
        _append_terminus_icons(d, station, graph, theme, r, min_off, max_off, muted)


class _Bundle(NamedTuple):
    """A station and the span of its lines across the flow, as drawn.

    Offsets run along y in an LR/RL section and along x in a TB/BT one.
    """

    station: Station
    min_off: float
    max_off: float
    is_tb_vert: bool

    @property
    def axis(self) -> _CrossAxis:
        """The cross axis through the station."""
        return _station_axis(self.station, self.is_tb_vert)

    def point(self, offset: float) -> tuple[float, float]:
        """The station's position shifted *offset* across its lines."""
        return self.axis.point(offset)


def station_tab_box(
    bundle: _Bundle, theme: Theme, label_point: tuple[float, float] | None
) -> tuple[float, float, float, float]:
    """``(x, y, w, h)`` of a one-line station's tab.

    The tab is ``station_tick_width`` line widths thick along the line and
    reaches ``station_tick_length`` line widths past the edge of the line
    facing the station's label. With no label to face it sits above (LR/RL)
    or left (TB/BT).
    """
    lw = theme.line_width
    reach = theme.station_tick_length * lw
    line_x, line_y = bundle.point(bundle.min_off)
    line_axis = line_x if bundle.is_tb_vert else line_y
    label_axis = None
    if label_point is not None:
        label_axis = label_point[0] if bundle.is_tb_vert else label_point[1]
    forward = label_axis is not None and label_axis > line_axis
    lo = line_axis + lw / 2 if forward else line_axis - lw / 2 - reach
    return bundle.axis.box(theme.station_tick_width * lw, lo, reach)


def bundle_end_bar_box(
    bundle: _Bundle, theme: Theme
) -> tuple[float, float, float, float]:
    """``(x, y, w, h)`` of a ``bar`` terminus (a buffer stop) across a bundle.

    As thick as a station tab, spanning every line's stroke and overhanging
    the outermost lines by a tab's length on each side; a one-line bundle gets
    the bar across its one line.
    """
    lw = theme.line_width
    lo_x, lo_y = bundle.point(bundle.min_off)
    hi_x, hi_y = bundle.point(bundle.max_off)
    lo, hi = (lo_x, hi_x) if bundle.is_tb_vert else (lo_y, hi_y)
    overhang = lw / 2 + theme.station_tick_length * lw
    return bundle.axis.box(
        theme.station_tick_width * lw,
        min(lo, hi) - overhang,
        abs(hi - lo) + 2 * overhang,
    )


class RingLayout(NamedTuple):
    """Ring centres along a bundle's cross axis, and their inside radius."""

    centres: list[float]
    inner: float


def ring_layout(lo: float, hi: float, lines: int, theme: Theme) -> RingLayout:
    """The rings for *lines* lines on one track, spanning *lo* to *hi*.

    Up to ``ring_max_lines`` lines get one ring of ``ring_inner_diameter``
    centred on them. A wider bundle gets, under ``ring_overflow: chain``, the
    fewest rings (at least two) that cover the bundle's outer edges, centred on
    the bundle and spaced so each ring's outer edge meets the next one's inner
    edge. That spacing never changes, so every chain has the same neck, and a
    chain may reach a little past the outermost lines. Under ``grow`` the
    bundle gets one ring whose inside widens to cover it.
    """
    lw = theme.line_width
    inner = theme.ring_inner_diameter * lw / 2
    stroke = theme.ring_stroke_width * lw
    span = hi - lo
    mid = (lo + hi) / 2
    if lines <= theme.ring_max_lines or span <= COORD_TOLERANCE_FINE:
        return RingLayout([mid], inner)
    if theme.ring_overflow == RING_GROW:
        return RingLayout([mid], max(inner, (span + lw) / 2))
    step = 2 * inner + stroke
    outer = inner + stroke
    count = max(2, math.ceil((span + lw - 2 * outer) / step - COORD_TOLERANCE_FINE) + 1)
    return RingLayout([mid + (k - (count - 1) / 2) * step for k in range(count)], inner)


def ring_groups(positions: Sequence[float], theme: Theme) -> list[RingLayout]:
    """Rings for lines at *positions*, one layout per track.

    Lines closer than a ring chain's own spacing share a track; a wider gap
    starts another track, which the drawing joins with a dumbbell link.
    """
    lw = theme.line_width
    spacing = (theme.ring_inner_diameter + theme.ring_stroke_width) * lw
    ordered = sorted(positions)
    groups: list[list[float]] = [[ordered[0]]]
    for pos in ordered[1:]:
        if pos - groups[-1][-1] > spacing + COORD_TOLERANCE_FINE:
            groups.append([pos])
        else:
            groups[-1].append(pos)
    return [ring_layout(g[0], g[-1], len(g), theme) for g in groups]


def _draw_ring_chain(
    d: draw.Group | draw.Drawing,
    groups: Sequence[RingLayout],
    axis: _CrossAxis,
    theme: Theme,
    station_data: Mapping[str, str],
    muted: bool,
) -> None:
    """Draw interchange rings, one :class:`RingLayout` per track.

    Drawn in layers so a chain reads as one outline around joined centres:
    dumbbell links between tracks, every ring's outline, every ring's inside
    (which covers any neighbouring outline crossing it), then a strip in the
    ring fill from the first centre to the last, through the outlines where
    rings meet. Ring centres are offsets along *axis*.
    """
    lw = theme.line_width
    stroke = theme.ring_stroke_width * lw
    half_bridge = theme.ring_bridge_width * lw / 2
    fill = theme.ring_fill or opaque_or_white(theme.background_color)
    color = theme.ring_color or theme.station_stroke

    group_el = draw.Group(**station_data)
    for prev, nxt in zip(groups, groups[1:]):
        group_el.append(
            draw.Rectangle(
                *axis.strip(prev.centres[-1], nxt.centres[0], half_bridge + stroke),
                fill=color,
                class_=_maybe_muted_class("nf-metro-ring-link", muted),
            )
        )
    rings = [(off, layout.inner) for layout in groups for off in layout.centres]
    for off, inner in rings:
        group_el.append(
            draw.Circle(
                *axis.point(off),
                inner + stroke / 2,
                fill="none",
                stroke=color,
                stroke_width=stroke,
                class_=_maybe_muted_class("nf-metro-ring", muted),
            )
        )
    for off, inner in rings:
        group_el.append(
            draw.Circle(
                *axis.point(off), inner, fill=fill, class_=_ns("nf-metro-ring-hole")
            )
        )
    if len(rings) > 1:
        group_el.append(
            draw.Rectangle(
                *axis.strip(rings[0][0], rings[-1][0], half_bridge),
                fill=fill,
                class_=_ns("nf-metro-ring-bridge"),
            )
        )
    d.append(group_el)


def _draw_line_station_glyph(
    d: draw.Group | draw.Drawing,
    graph: MetroGraph,
    theme: Theme,
    bundle: _Bundle,
    station_data: dict[str, str],
    muted: bool,
    label_point: tuple[float, float] | None,
) -> bool:
    """Draw a station as a tab, terminus bar or ring when its theme asks for one.

    Returns whether anything was drawn; a ``pill`` theme draws nothing here.
    """
    station = bundle.station
    lines = graph.station_lines(station.id)
    if theme.station_glyph == GLYPH_TICK and len(lines) == 1:
        color = effective_line_color(
            graph.lines.get(lines[0]),
            theme,
            frozenset(lines) if muted else frozenset(),
        )
        box = (
            bundle_end_bar_box(bundle, theme)
            if station.is_terminus
            else station_tab_box(bundle, theme, label_point)
        )
        paint = muted_paint_class(theme, muted, "fill")
        extra = {"class_": f"{station_data['class_']} {paint}"} if paint else {}
        d.append(draw.Rectangle(*box, fill=color, **{**station_data, **extra}))
        return True
    if station.is_blank_terminus and len(lines) >= 2:
        if theme.terminus_glyph == TERMINUS_BAR:
            d.append(
                draw.Rectangle(
                    *bundle_end_bar_box(bundle, theme),
                    fill=theme.terminus_bar_color or theme.station_stroke,
                    **{
                        **station_data,
                        "class_": station_data["class_"]
                        + " "
                        + _maybe_muted_class("nf-metro-terminus-bar", muted),
                    },
                )
            )
            return True
        if theme.terminus_glyph != GLYPH_RING:
            return False
        _draw_bundle_rings(d, bundle, len(lines), theme, station_data, muted)
        return True
    if theme.interchange_glyph == GLYPH_RING and len(lines) >= 2:
        _draw_bundle_rings(d, bundle, len(lines), theme, station_data, muted)
        return True
    return False


def _draw_bundle_rings(
    d: draw.Group | draw.Drawing,
    bundle: _Bundle,
    line_count: int,
    theme: Theme,
    station_data: Mapping[str, str],
    muted: bool,
) -> None:
    """Draw the ring, or chain of rings, across a station's bundle."""
    layout = ring_layout(bundle.min_off, bundle.max_off, line_count, theme)
    _draw_ring_chain(d, [layout], bundle.axis, theme, station_data, muted)


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
    theme: Theme, station: Station, is_vertical_flow: bool
) -> tuple[float, list[float]]:
    """Per-icon centre-to-centre step along the flow axis, and caption widths.

    TB/BT stack icons by height (plus a caption row when captioned); LR/RL
    march by a caption-aware width.  Returns the step and each caption's
    estimated width, shared by the icon-placement helper and the renderer's
    caption-stagger logic so the two stay in lockstep.
    """
    names = station.terminus_names or [""] * len(station.terminus_labels)
    caption_font_size = theme.label_font_size * ICON_NAME_FONT_SCALE
    caption_style = text_style(caption_font_size, theme.label_font_weight)
    name_widths = [
        DEFAULT_TEXT_METRICS.reserve_width(name, caption_style, TextRole.ICON_CAPTION)
        for name in names
    ]
    if is_vertical_flow:
        caption_line_count = station.terminus_caption_line_count
        caption_room = (
            caption_font_size * caption_line_count + ICON_NAME_GAP
            if caption_line_count
            else 0.0
        )
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

    ctx = _terminus_flow_context(station, graph)

    r = theme.station_radius
    icon_gap = r + ICON_STATION_GAP
    icon_half_w = theme.terminus_width / 2
    icon_half_h = theme.terminus_height / 2
    icon_half_flow = icon_half_h if ctx.is_vertical_flow else icon_half_w

    bundle_center = (min_off + max_off) / 2

    icon_step, _ = _terminus_icon_marching(theme, station, ctx.is_vertical_flow)

    is_rail = graph.station_is_rail(station.id)
    offtrack_nub_lift = (
        OFFTRACK_TERMINUS_NUB_CLEARANCE
        if (station.off_track and is_rail and station.is_captioned_terminus)
        else 0.0
    )

    return _terminus_icon_centers(
        station,
        ctx.section_dir,
        ctx.is_source,
        len(station.terminus_labels),
        icon_gap + icon_half_flow + offtrack_nub_lift,
        icon_step,
        bundle_center,
        is_rail=is_rail,
    )


def _terminus_icon_flow_sign(section_dir: str, is_source: bool) -> float:
    """+1 if a terminus icon extends in the forward flow direction, else -1.

    Sinks sit at the end of the flow and extend forwards; sources sit at
    the start and extend backwards.  RL/BT reverse the forward direction so
    icons always point to the outside of the diagram.
    """
    extends_forward = is_source if section_dir in ("RL", "BT") else not is_source
    return 1.0 if extends_forward else -1.0


def _terminus_caption_hangs_down(is_vertical_flow: bool, flow_sign: float) -> bool:
    """Whether a terminus caption hangs below its icon rather than above.

    Vertical-flow sources and flow-reversed sinks draw the icon above the
    station, so the caption hangs above it too; every other flow hangs it
    below.
    """
    return not is_vertical_flow or flow_sign > 0


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
    sign = _terminus_icon_flow_sign(section_dir, is_source)
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
    muted: bool,
) -> None:
    """Render file icon(s) adjacent to a terminus station.

    Multiple icons march away from the station along the section's flow
    axis (a horizontal row for LR/RL, a vertical stack for TB/BT), with
    the first icon closest to the station pill.
    """
    ctx = _terminus_flow_context(station, graph)
    section = ctx.section
    icon_half_w = theme.terminus_width / 2
    icon_half_h = theme.terminus_height / 2

    icon_types = station.terminus_icon_types or [ICON_TYPE_FILE] * len(
        station.terminus_labels
    )
    names = station.terminus_names or [""] * len(station.terminus_labels)
    banners = station.terminus_icon_banners or [False] * len(station.terminus_labels)

    caption_font_size = theme.label_font_size * ICON_NAME_FONT_SCALE
    icon_step, name_widths = _terminus_icon_marching(
        theme, station, ctx.is_vertical_flow
    )

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
        if section and ctx.is_vertical_flow and section.bbox_h > 0:
            top = section.bbox_y + icon_half_h + ICON_BBOX_MARGIN
            bottom = section.bbox_y + section.bbox_h - icon_half_h - ICON_BBOX_MARGIN
            icon_cy = max(top, min(icon_cy, bottom))
        elif section and not ctx.is_vertical_flow and section.bbox_w > 0:
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
            font_color=theme.terminus_font_color or TERMINUS_FONT_COLOR,
            font_family=theme.label_font_family,
        )

        banner_fill = theme.muted_line_color if muted else ICON_BANNER_FILL
        banner_text_color = (
            ICON_BANNER_TEXT_COLOR_MUTED if muted else ICON_BANNER_TEXT_COLOR
        )

        if icon_type == ICON_TYPE_DIR:
            render_folder_icon(d, **common)
        elif icon_type == ICON_TYPE_FILES:
            back_dx_sign, back_dy_sign = (
                (1.0, ctx.flow_sign) if ctx.is_vertical_flow else (ctx.flow_sign, -1.0)
            )
            render_files_icon(
                d,
                **common,
                fold_size=theme.terminus_fold_size,
                banner=banner,
                back_dx_sign=back_dx_sign,
                back_dy_sign=back_dy_sign,
                banner_fill=banner_fill,
                banner_text_color=banner_text_color,
            )
        else:
            render_file_icon(
                d,
                **common,
                fold_size=theme.terminus_fold_size,
                banner=banner,
                banner_fill=banner_fill,
                banner_text_color=banner_text_color,
            )

        # Optional caption rendered clear of the icon so the type chip
        # inside the icon stays readable.
        if name:
            caption_hangs_down = _terminus_caption_hangs_down(
                ctx.is_vertical_flow, ctx.flow_sign
            )
            # A stacked-files icon's back sheet peeks past the nominal edge
            # along the flow axis -- toward the caption for a vertical flow, but
            # off-axis (away from it) for a horizontal flow -- so only a
            # vertical flow needs the extra clearance to keep the caption off
            # the drawn icon.
            stacked_pad = _terminus_stacked_pad(
                theme, ctx.is_vertical_flow and icon_type == ICON_TYPE_FILES
            )
            icon_edge_gap = theme.terminus_height / 2 + stacked_pad + ICON_NAME_GAP
            stagger_step = caption_font_size * 1.4
            # When adjacent icon captions would overlap horizontally
            # (their estimated width exceeds the per-icon X step), drop
            # odd-indexed captions to a further row so each name is legible.
            staggered = stagger_captions and i % 2 == 1
            if caption_hangs_down:
                caption_y = icon_cy + icon_edge_gap
                if staggered:
                    caption_y += stagger_step
            else:
                # The hanging baseline anchors the text's top edge, so drop
                # the caption height to seat the whole glyph box above the icon.
                caption_height = (name.count("\n") + 1) * caption_font_size
                caption_y = icon_cy - icon_edge_gap - caption_height
                if staggered:
                    caption_y -= stagger_step
            caption_cx = icon_cx
            if section and section.bbox_w > 0:
                # Estimate caption width and clamp so it stays inside the
                # section bbox right edge (and left edge for symmetry).
                approx_w = name_widths[i]
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
                    class_=_maybe_muted_class("nf-metro-station-label", muted),
                )
            )


def _label_halo_color_for(width: float, color: str, background: str) -> str | None:
    """Resolve a halo colour, or ``None`` when haloing is disabled.

    Haloing is disabled by a non-positive *width* or an explicit *color* of
    ``"none"`` (matching the SVG ``fill="none"`` convention). An empty colour
    resolves to the *background*, so the name punches a knockout through any
    route it crosses, or white on a transparent canvas.
    """
    if width <= 0 or color == "none":
        return None
    if color:
        return color
    return background if background and background != "none" else "#ffffff"


def _render_labels(
    d: draw.Drawing,
    labels: list[LabelPlacement],
    theme: Theme,
    graph: MetroGraph,
    inactive_line_ids: frozenset[str] = frozenset(),
) -> None:
    """Render station name labels."""
    halo_color = _label_halo_color_for(
        theme.label_halo_width, theme.label_halo_color, theme.background_color
    )
    label_style = text_style(theme.label_font_size, theme.label_font_weight)
    line_height_ratio = (
        DEFAULT_TEXT_METRICS.line_height(label_style, TextRole.STATION_LABEL)
        / theme.label_font_size
    )

    def emit(text: str, x: float, y: float, fill: str, **style: object) -> None:
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
                    line_height=line_height_ratio,
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
                fill=fill,
                font_family=theme.label_font_family,
                font_weight=theme.label_font_weight,
                line_height=line_height_ratio,
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
            line_spacing = DEFAULT_TEXT_METRICS.line_height(
                label_style, TextRole.STATION_LABEL
            )
            if label.dominant_baseline == "central":
                # Center the block vertically on y
                y -= (n_lines - 1) * line_spacing / 2
            elif label.above:
                # Keep the bottom line near the station
                y -= (n_lines - 1) * line_spacing

        muted = bool(label.station_id) and station_is_muted(
            graph, label.station_id, inactive_line_ids
        )

        # Skip emitting data-station-id for synthetic obstacle placements.
        label_data: dict[str, str] = {}
        if label.station_id and not label.station_id.startswith("__"):
            label_data["data-station-id"] = label.station_id
            label_data["class_"] = _maybe_muted_class("nf-metro-station-label", muted)

        label_fill = theme.muted_line_color if muted else theme.label_color

        if label.angle:
            # Diagonal labels: anchor at the pill and rotate about
            # the anchor.  text-anchor=start so the tilted text trails
            # away from the station.
            emit(
                text,
                label.x,
                label.y,
                fill=label_fill,
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
                fill=label_fill,
                text_anchor=label.text_anchor,
                dominant_baseline=label.dominant_baseline,
            )
        else:
            baseline = "auto" if label.above else "hanging"
            emit(
                text,
                label.x,
                y,
                fill=label_fill,
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


def _group_caption_bounds(bands: list[_GroupBand], theme: Theme) -> tuple[float, float]:
    """Return the max ``(x, y)`` reached by any group band."""
    max_x = 0.0
    max_y = 0.0
    caption_style = text_style(theme.label_font_size * GROUP_LABEL_FONT_SCALE, "bold")
    for band in bands:
        caption_width = DEFAULT_TEXT_METRICS.reserve_width(
            band.label, caption_style, TextRole.GROUP_CAPTION
        )
        caption_right = (band.x_left + band.x_right + caption_width) / 2
        max_x = max(max_x, band.x_right, caption_right)
        max_y = max(max_y, band.rule_y, band.text_y, band.band_far_y)
    return max_x, max_y


def _resolve_group_bands(
    graph: MetroGraph,
    theme: Theme,
    station_offsets: dict[tuple[str, str], float],
    labels: list[LabelPlacement],
) -> list[_GroupBand]:
    """Band geometry for *graph*'s station groups against the given coordinates.

    A band clears the (possibly diagonal) station labels rather than just the
    markers, so it is resolved from the rendered label extents.
    """
    if not graph.groups:
        return []
    label_extents: dict[str, tuple[float, float]] = {}
    for placement in labels:
        if placement.station_id:
            _, low, _, high = _label_bbox(placement)
            label_extents[placement.station_id] = (low, high)
    return _group_bands(graph, theme, station_offsets, label_extents)


def _reserve_group_band_space(
    graph: MetroGraph,
    theme: Theme,
    station_offsets: dict[tuple[str, str], float],
    labels: list[LabelPlacement],
) -> None:
    """Grow section boxes for their ``below`` group bands, before settlement.

    Settlement translates whole rows rigidly, so a band holds its offset inside
    its own box and one reservation covers the drawn geometry.
    """
    bands = _resolve_group_bands(graph, theme, station_offsets, labels)
    if bands:
        _reserve_section_space_for_groups(graph, bands)


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


def _assert_group_band_room_drawn(graph: MetroGraph, bands: list[_GroupBand]) -> None:
    """Reject a ``below`` band drawn past the room ``_reserve_group_band_space``
    grew for it.

    The reservation is sized from the label placement in effect when it ran;
    a section relabelled afterwards (a header-collision resettle re-places
    labels without re-reserving) can grow a band this box was never widened
    for.
    """
    for band in bands:
        if band.section_id is None or band.tick_dy >= 0:
            continue
        sec = graph.sections.get(band.section_id)
        if sec is None or sec.is_implicit:
            continue
        overhang = (
            band.band_far_y + GROUP_LABEL_BAND_PADDING - (sec.bbox_y + sec.bbox_h)
        )
        if overhang > SAME_COORD_TOLERANCE:
            raise LayoutInvariantError(
                f"section '{band.section_id}' group band overhangs the room "
                f"reserved for it by {overhang:.2f}px"
            )


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
            y = (a_bot + b_top) / 2
            if any(
                sec.grid_row_span == 1
                and sec.grid_row in (ra, rb)
                and max(x_start, sec.bbox_x) < min(x_end, sec.bbox_x + sec.bbox_w)
                and sec.bbox_y < y < sec.bbox_y + sec.bbox_h
                for sec in sections
            ):
                continue
            segments.append((ra, rb, x_start, x_end, y))
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

    for sec in graph.sections.values():
        if not sec.is_hidden or sec.bbox_w <= 0 or sec.bbox_h <= 0:
            continue
        d.append(
            draw.Rectangle(
                sec.bbox_x,
                sec.bbox_y,
                sec.bbox_w,
                sec.bbox_h,
                fill="none",
                stroke=DEBUG_HIDDEN_STATION_COLOR,
                stroke_width=DEBUG_STROKE_WIDTH,
                stroke_dasharray="6,4",
            )
        )
        d.append(
            draw.Text(
                sec.id,
                debug_font_size,
                sec.bbox_x + DEBUG_HIDDEN_LABEL_OFFSET,
                sec.bbox_y - DEBUG_HIDDEN_LABEL_OFFSET,
                fill=DEBUG_HIDDEN_STATION_COLOR,
                font_family=debug_font,
                dominant_baseline="auto",
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
