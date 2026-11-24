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
"""

from __future__ import annotations

__all__ = [
    "LayoutMetrics",
    "active_font_scale",
    "active_metrics",
    "active_stroke_scale",
    "canvas_edge_clearance",
    "diagonal_label_offset",
    "graph_layout_context",
    "icon_half_height_approx",
    "icon_width_approx",
    "label_font_size_approx",
    "metrics_context",
    "station_radius_approx",
    "station_stroke_approx",
]

from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Iterator, NamedTuple

from nf_metro.layout.constants import (
    DEFAULT_LINE_WIDTH,
    DIAGONAL_LABEL_OFFSET,
    DIAGONAL_RUN,
    DIRECTIONAL_MARKER_HALF_EXTENT,
    ICON_HALF_HEIGHT,
    LABEL_FONT_SIZE,
    LABEL_OFFSET,
    SECTION_X_PADDING,
    SECTION_Y_PADDING,
    STATION_RADIUS_APPROX,
    STATION_STROKE_APPROX,
    TERMINUS_WIDTH,
    WIDEST_THEME_LINE_WIDTH,
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
            icon_width=theme.terminus_width,
            icon_half_height=theme.terminus_height / 2,
            line_width=theme.line_width,
            label_font_size=theme.label_font_size,
            chevron_size=theme.directional_marker_size,
            diagonal_run=option("diagonal_run", defaults.diagonal_run),
            label_offset=option("label_offset", defaults.label_offset),
            section_x_padding=option("section_x_padding", defaults.section_x_padding),
            section_y_padding=option("section_y_padding", defaults.section_y_padding),
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

        metrics = LayoutMetrics.of(graph, resolve_theme(None, graph))
    return metrics_context(metrics)


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
