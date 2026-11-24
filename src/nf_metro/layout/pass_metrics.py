"""Pass-scoped metrics the layout reserves against.

The layout engine reserves room for the theme it lays out for (its station
radius and outline, label size, file-icon size, line width and chevron size)
and for the map's spacing options. The ``font_scale`` and ``stroke_scale``
directives multiply what the renderer actually draws, so those sizes are read
through the same multiplier or a scaled render collides with reservations made
for unscaled ink.

:func:`~nf_metro.layout.engine.compute_layout` records the metrics it laid out
with on the graph, and every later measurement of that graph (render geometry
settlement, validators) binds the recorded metrics through
:func:`graph_layout_context`, whatever theme it is drawn in.

The metrics are ambient for the duration of a pass rather than a parameter
because the readers are geometry helpers taking a ``Station`` or a bare
coordinate, far from any caller holding the ``MetroGraph``.

The corner radius is one of them (:func:`active_curve_radius`), and so is
every clearance that exists to make room for a corner.  Geometry follows the
metrics the graph was *laid out* with: drawn in a theme with a different line
width, a map keeps its laid-out radius, so the drawn ratio to the line differs.
"""

from __future__ import annotations

__all__ = [
    "LayoutMetrics",
    "active_curve_radius",
    "active_font_scale",
    "active_metrics",
    "active_stroke_scale",
    "bypass_clearance",
    "canvas_edge_clearance",
    "diagonal_label_offset",
    "exit_corridor_icon_clearance",
    "graph_layout_context",
    "graph_radius_context",
    "icon_half_height_approx",
    "icon_width_approx",
    "inter_row_edge_clearance",
    "inter_row_header_clearance",
    "label_font_size_approx",
    "label_height_approx",
    "merge_gap_min",
    "merge_route_margin",
    "metrics_context",
    "min_corridor_y_overlap",
    "min_inter_section_gap",
    "min_station_flat_length",
    "min_straight_edge",
    "perp_port_edge_clearance",
    "row_band_slack",
    "station_radius_approx",
    "station_stroke_approx",
]

from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Iterator, NamedTuple

from nf_metro.layout.constants import (
    CURVE_RADIUS,
    DEFAULT_LINE_WIDTH,
    DIAGONAL_LABEL_OFFSET,
    DIAGONAL_RUN,
    DIRECTIONAL_MARKER_HALF_EXTENT,
    FONT_HEIGHT,
    ICON_HALF_HEIGHT,
    INTER_ROW_EDGE_CLEARANCE,
    LABEL_FONT_SIZE,
    LABEL_OFFSET,
    MIN_STATION_FLAT_LENGTH,
    MIN_STRAIGHT_EDGE,
    SECTION_HEADER_PROTRUSION,
    SECTION_X_PADDING,
    SECTION_Y_PADDING,
    STATION_RADIUS_APPROX,
    STATION_STROKE_APPROX,
    TERMINUS_WIDTH,
    WIDEST_THEME_LINE_WIDTH,
    Y_SPACING,
)

if TYPE_CHECKING:
    from nf_metro.parser.model import MetroGraph
    from nf_metro.render.style import Theme


class LayoutMetrics(NamedTuple):
    """The sizes a layout pass reserves against.

    Theme sizes are unscaled; the accessors below apply ``font_scale`` and
    ``stroke_scale``. The defaults are the base theme's and the engine's, for a
    pass run with nothing bound.
    """

    font_scale: float = 1.0
    stroke_scale: float = 1.0
    station_radius: float = STATION_RADIUS_APPROX
    station_stroke_width: float = STATION_STROKE_APPROX
    icon_width: float = TERMINUS_WIDTH
    icon_half_height: float = ICON_HALF_HEIGHT
    line_width: float = DEFAULT_LINE_WIDTH
    label_font_size: float = LABEL_FONT_SIZE
    chevron_size: float = DIRECTIONAL_MARKER_HALF_EXTENT
    diagonal_run: float = DIAGONAL_RUN
    label_offset: float = LABEL_OFFSET
    section_x_padding: float = SECTION_X_PADDING
    section_y_padding: float = SECTION_Y_PADDING
    curve_radius_ratio: float | None = None

    @classmethod
    def of(cls, graph: MetroGraph, theme: Theme) -> LayoutMetrics:
        """The metrics of *graph* laid out for *theme*.

        Spacing options *graph* leaves unset take the engine's defaults.
        """
        defaults = cls()

        def option(name: str, default: float) -> float:
            value: float | None = getattr(graph, name)
            return default if value is None else value

        return cls(
            font_scale=graph.font_scale,
            stroke_scale=graph.stroke_scale,
            station_radius=theme.station_radius,
            station_stroke_width=theme.station_stroke_width,
            icon_width=theme.icon_width,
            icon_half_height=theme.icon_height / 2,
            line_width=theme.line_width,
            label_font_size=theme.label_font_size,
            chevron_size=theme.chevron_size,
            diagonal_run=option("diagonal_run", defaults.diagonal_run),
            label_offset=option("label_offset", defaults.label_offset),
            section_x_padding=option("section_x_padding", defaults.section_x_padding),
            section_y_padding=option("section_y_padding", defaults.section_y_padding),
            curve_radius_ratio=graph.curve_radius_ratio,
        )


_ACTIVE_METRICS: ContextVar[LayoutMetrics] = ContextVar(
    "active_metrics", default=LayoutMetrics()
)


def active_metrics() -> LayoutMetrics:
    """The metrics the current pass reserves against."""
    return _ACTIVE_METRICS.get()


@contextmanager
def metrics_context(
    metrics: LayoutMetrics | None = None, **changes: float
) -> Iterator[None]:
    """Reserve against *metrics* (the active ones when omitted), with
    *changes* applied, for the duration of the block."""
    token = _ACTIVE_METRICS.set(
        (metrics or active_metrics())._replace(**changes)  # type: ignore[arg-type]
    )
    try:
        yield
    finally:
        _ACTIVE_METRICS.reset(token)


def graph_layout_context(graph: MetroGraph) -> AbstractContextManager[None]:
    """Bind the metrics *graph* was laid out with.

    A graph not yet laid out binds the metrics of its own theme stack, never
    the theme a caller later draws it in.
    """
    metrics = graph.layout_metrics
    if metrics is None:
        from nf_metro.themes import resolve_theme

        metrics = LayoutMetrics.of(graph, resolve_theme(graph))
    return metrics_context(metrics)


def graph_radius_context(graph: MetroGraph) -> AbstractContextManager[None]:
    """Bind only the metrics that size *graph*'s corner radius.

    For a router entered outside any bound pass (a validator, a test): the
    radius has to be the one the graph was laid out with, while any other
    metric a caller has bound stays in force.
    """
    metrics = graph.layout_metrics
    if metrics is None:
        from nf_metro.themes import resolve_theme

        metrics = LayoutMetrics.of(graph, resolve_theme(graph))
    return metrics_context(
        active_metrics()._replace(
            curve_radius_ratio=metrics.curve_radius_ratio,
            line_width=metrics.line_width,
            stroke_scale=metrics.stroke_scale,
        )
    )


def active_font_scale() -> float:
    """Font-size multiplier in effect for the current layout/render pass."""
    return active_metrics().font_scale


def active_stroke_scale() -> float:
    """Stroke-weight multiplier in effect for the current layout/render pass."""
    return active_metrics().stroke_scale


def station_radius_approx() -> float:
    """Station pill radius to reserve against, under the active stroke scale."""
    metrics = active_metrics()
    return metrics.station_radius * metrics.stroke_scale


def station_stroke_approx() -> float:
    """Station marker stroke width to reserve against, under the active scale."""
    metrics = active_metrics()
    return metrics.station_stroke_width * metrics.stroke_scale


def icon_width_approx() -> float:
    """File icon width to reserve against, under the active font scale.

    ``font_scale`` grows the icon body to keep its label from being
    shrink-to-fit clamped back down, so layout has to reserve the same
    grown footprint.
    """
    metrics = active_metrics()
    return metrics.icon_width * metrics.font_scale


def icon_half_height_approx() -> float:
    """File icon half-height to reserve against, under the active font scale."""
    metrics = active_metrics()
    return metrics.icon_half_height * metrics.font_scale


def label_font_size_approx() -> float:
    """Station label size to measure against, under the active font scale."""
    metrics = active_metrics()
    return metrics.label_font_size * metrics.font_scale


def label_height_approx() -> float:
    """Station label line height to reserve against, under the active font scale.

    Layout measures a line at :data:`~nf_metro.layout.constants.FONT_HEIGHT`
    for a :data:`~nf_metro.layout.constants.LABEL_FONT_SIZE` label, and in the
    same proportion for any other label size.
    """
    metrics = active_metrics()
    return metrics.label_font_size * FONT_HEIGHT / LABEL_FONT_SIZE * metrics.font_scale


def canvas_edge_clearance() -> float:
    """Canvas-margin clearance to reserve against, under the active stroke scale.

    ``stroke_scale`` multiplies the stroke a route is drawn with but not the
    direction chevron's arms, so only the half-stroke term of
    :data:`~nf_metro.layout.constants.CANVAS_EDGE_CLEARANCE` scales.  Reading the
    unscaled constant would leave a coarsened render short of the margin by half
    the extra stroke weight.  A theme drawing wider lines or larger chevrons than
    any built-in widens the clearance to match.
    """
    metrics = active_metrics()
    line_width = max(WIDEST_THEME_LINE_WIDTH, metrics.line_width)
    clearance = (
        max(DIRECTIONAL_MARKER_HALF_EXTENT, metrics.chevron_size) + line_width / 2
    )
    extra_stroke = line_width * (metrics.stroke_scale - 1.0)
    return clearance + extra_stroke / 2


def diagonal_label_offset() -> float:
    """Extra drop for an angled label, kept in proportion to the label offset."""
    return active_metrics().label_offset * DIAGONAL_LABEL_OFFSET / LABEL_OFFSET


def active_curve_radius() -> float:
    """Centreline radius a route turns a corner with, in the current pass.

    ``curve_radius_ratio`` states it as a multiple of the theme's line width;
    unset, the engine's base :data:`~nf_metro.layout.constants.CURVE_RADIUS`
    applies.  ``stroke_scale`` scales either, as it scales the line.
    """
    metrics = active_metrics()
    if metrics.curve_radius_ratio is None:
        return CURVE_RADIUS * metrics.stroke_scale
    return metrics.curve_radius_ratio * metrics.line_width * metrics.stroke_scale


def min_corridor_y_overlap() -> float:
    """Minimum overlap for two gap channels to share one corridor.

    Channels in the same inter-section gap and direction are grouped into a
    concentric corridor only when their spans overlap by more than this.  Two
    channels overlapping by less are not running parallel: they are stacked
    segments meeting at a single elbow, their turning corners within one
    corner zone of each other, and packing them into one ``OFFSET_STEP``
    corridor makes those opposing elbows graze.  Twice the corner radius: an
    overlap that small lies wholly inside the two corners' rounding zones.
    """
    return 2 * active_curve_radius()


def perp_port_edge_clearance() -> float:
    """Room a port keeps from the two bbox edges it is *not* anchored to.

    A port is pinned to one edge and free along its other axis.  Flush against
    a second edge, its run is drawn along the box border and the two read as
    one stroke; it also blocks the section header's above-left position.  One
    corner radius is the shortest separation that reads as a route
    inside the box rather than on its edge.
    """
    return active_curve_radius()


def exit_corridor_icon_clearance() -> float:
    """Gap a flow-axis exit corridor keeps beyond a terminus icon hanging into
    the exit row of a vertical-flow (TB/BT) section: one corner radius, so the
    route turning onto the corridor does not graze the icon."""
    return active_curve_radius()


def merge_route_margin() -> float:
    """Distance between a section bbox edge and a merge branch or trunk
    vertical in the inter-section gap: room for a corner each side."""
    return 2 * active_curve_radius()


def merge_gap_min() -> float:
    """Minimum inter-section gap for a column pair with merge routing.

    A :func:`merge_route_margin` from each section edge, and one corner radius
    between the branch descent and the trunk ascent.
    """
    return 2 * merge_route_margin() + active_curve_radius()


def min_inter_section_gap() -> float:
    """Minimum physical gap between adjacent section bboxes.

    Puts the gap midpoint two corner radii from each section edge, room for a
    bypass corner each side.  Past a radius of a quarter of
    :data:`~nf_metro.layout.constants.SECTION_X_GAP` this floor, not the
    configured gap, sets the column spacing.
    """
    return 4 * active_curve_radius()


def min_station_flat_length() -> float:
    """Minimum polyline run reaching a visible station's X along its track.

    A corner consumes one radius of the run, so the run must exceed the radius
    for a visible flat to be drawn through the marker.  The base length holds
    that at the base radius; past it, two radii keep at least one radius of
    flat.
    """
    return max(MIN_STATION_FLAT_LENGTH, 2 * active_curve_radius())


def min_straight_edge() -> float:
    """Minimum straight track at a route end that is not a port.

    A corner off that end takes its radius out of the track, so the track is
    never shorter than one radius.
    """
    return max(MIN_STRAIGHT_EDGE, active_curve_radius())


def inter_row_edge_clearance(radius: float | None = None) -> float:
    """Minimum distance between an inter-row channel and the box edge it runs
    beneath, at *radius* (the active one when omitted).

    :data:`~nf_metro.layout.constants.INTER_ROW_EDGE_CLEARANCE` is the visual
    floor; the channel's two corners off and back onto it each take a radius,
    so a large radius widens it.
    """
    if radius is None:
        radius = active_curve_radius()
    return max(INTER_ROW_EDGE_CLEARANCE, 2 * radius)


def bypass_clearance() -> float:
    """Clearance below the lowest intervening section for a bypass channel.

    A bypass is a horizontal run beneath a section's box edge, the relationship
    :func:`inter_row_edge_clearance` states, and the reservation ledger insets
    every row-gap corridor by that same margin.  A narrower margin here would
    seat a bypass inside the band its own reservation allocates it.
    """
    return inter_row_edge_clearance()


def row_band_slack() -> float:
    """Slack a same-row inter-section route may extend past the row band.

    A same-row wrap routes below the row's tallest section through a bypass
    channel :func:`bypass_clearance` below the band bottom, stacks the bundle's
    nest offsets on it, and adds up to one ``Y_SPACING`` for the diagonal
    approach.  The slack admits that excursion while rejecting a route that
    dips a full row down.
    """
    return bypass_clearance() + Y_SPACING


def inter_row_header_clearance(radius: float | None = None) -> float:
    """Distance from a section's bbox top to an inter-row channel above it, at
    *radius* (the active one when omitted).

    A channel approaching a section from above clears the header badge (which
    protrudes ``SECTION_HEADER_PROTRUSION`` above ``bbox_y``) by
    :func:`inter_row_edge_clearance`, the margin the source side keeps from
    its bbox bottom.  Section placement reserves this band and routing centres
    within it, so the one definition keeps the two in lockstep.
    """
    return SECTION_HEADER_PROTRUSION + inter_row_edge_clearance(radius)
