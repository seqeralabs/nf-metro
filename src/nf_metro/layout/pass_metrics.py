"""Pass-scoped metrics the layout reserves against.

The layout engine reserves room for the theme it lays out for: its station
radius, label size, file-icon size, line width and chevron size
(:class:`ThemeMetrics`), and the map's spacing options (:class:`LayoutMetrics`).
The ``font_scale`` and ``stroke_scale`` directives multiply what the renderer
actually draws, so those sizes are read through the same multiplier or a scaled
render collides with reservations made for unscaled ink.

The scale is ambient for the duration of a layout or render pass rather than a
parameter because the readers are geometry helpers taking a ``Station`` or a
bare coordinate, far from any caller holding the ``MetroGraph``.  Wrap a pass in
:func:`font_scale_context` / :func:`stroke_scale_context` and read through the
accessors here.
"""

from __future__ import annotations

__all__ = [
    "LayoutMetrics",
    "ThemeMetrics",
    "active_layout_metrics",
    "diagonal_label_offset",
    "layout_metrics_context",
    "active_font_scale",
    "active_stroke_scale",
    "active_theme_metrics",
    "canvas_edge_clearance",
    "font_scale_context",
    "icon_half_height_approx",
    "label_font_size_approx",
    "station_radius_approx",
    "station_stroke_approx",
    "stroke_scale_context",
    "terminus_width_approx",
    "theme_metrics_context",
]

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, NamedTuple

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


class ThemeMetrics(NamedTuple):
    """The drawn sizes of the theme a pass lays out for, before any scaling.

    The defaults are the base theme's, for a pass run with no theme bound.
    """

    station_radius: float = STATION_RADIUS_APPROX
    terminus_width: float = TERMINUS_WIDTH
    icon_half_height: float = ICON_HALF_HEIGHT
    line_width: float = DEFAULT_LINE_WIDTH
    label_font_size: float = LABEL_FONT_SIZE
    marker_half_extent: float = DIRECTIONAL_MARKER_HALF_EXTENT

    @classmethod
    def of(cls, theme: object) -> ThemeMetrics:
        """The metrics of a :class:`~nf_metro.render.style.Theme`."""
        return cls(
            getattr(theme, "station_radius"),
            getattr(theme, "terminus_width"),
            getattr(theme, "terminus_height") / 2,
            getattr(theme, "line_width"),
            getattr(theme, "label_font_size"),
            getattr(theme, "directional_marker_size"),
        )


_ACTIVE_THEME_METRICS: ContextVar[ThemeMetrics] = ContextVar(
    "active_theme_metrics", default=ThemeMetrics()
)


def active_theme_metrics() -> ThemeMetrics:
    """The drawn sizes of the theme the current pass lays out for."""
    return _ACTIVE_THEME_METRICS.get()


@contextmanager
def theme_metrics_context(metrics: ThemeMetrics) -> Iterator[None]:
    """Reserve against *metrics* for the duration of the block."""
    token = _ACTIVE_THEME_METRICS.set(metrics)
    try:
        yield
    finally:
        _ACTIVE_THEME_METRICS.reset(token)


_ACTIVE_FONT_SCALE: ContextVar[float] = ContextVar("active_font_scale", default=1.0)
_ACTIVE_STROKE_SCALE: ContextVar[float] = ContextVar("active_stroke_scale", default=1.0)


def active_font_scale() -> float:
    """Font-size multiplier in effect for the current layout/render pass."""
    return _ACTIVE_FONT_SCALE.get()


@contextmanager
def font_scale_context(scale: float) -> Iterator[None]:
    """Apply ``scale`` to the text metrics for the duration of the block."""
    token = _ACTIVE_FONT_SCALE.set(scale)
    try:
        yield
    finally:
        _ACTIVE_FONT_SCALE.reset(token)


def active_stroke_scale() -> float:
    """Stroke-weight multiplier in effect for the current layout/render pass."""
    return _ACTIVE_STROKE_SCALE.get()


@contextmanager
def stroke_scale_context(scale: float) -> Iterator[None]:
    """Apply ``scale`` to the station-marker metrics for the block's duration."""
    token = _ACTIVE_STROKE_SCALE.set(scale)
    try:
        yield
    finally:
        _ACTIVE_STROKE_SCALE.reset(token)


def station_radius_approx() -> float:
    """Station pill radius to reserve against, under the active stroke scale."""
    return active_theme_metrics().station_radius * active_stroke_scale()


def station_stroke_approx() -> float:
    """Station marker stroke width to reserve against, under the active scale."""
    return STATION_STROKE_APPROX * active_stroke_scale()


def terminus_width_approx() -> float:
    """Terminus (file) icon width to reserve against, under the active font scale.

    ``font_scale`` grows the icon body to keep its label from being
    shrink-to-fit clamped back down, so layout has to reserve the same
    grown footprint.
    """
    return active_theme_metrics().terminus_width * active_font_scale()


def icon_half_height_approx() -> float:
    """Terminus icon half-height to reserve against, under the active font scale."""
    return active_theme_metrics().icon_half_height * active_font_scale()


def label_font_size_approx() -> float:
    """Station label size to measure against, under the active font scale."""
    return active_theme_metrics().label_font_size * active_font_scale()


def canvas_edge_clearance() -> float:
    """Canvas-margin clearance to reserve against, under the active stroke scale.

    ``stroke_scale`` multiplies the stroke a route is drawn with but not the
    direction chevron's arms, so only the half-stroke term of
    :data:`~nf_metro.layout.constants.CANVAS_EDGE_CLEARANCE` scales.  Reading the
    unscaled constant would leave a coarsened render short of the margin by half
    the extra stroke weight.  A theme drawing wider lines or larger chevrons than
    any built-in widens the clearance to match.
    """
    metrics = active_theme_metrics()
    line_width = max(WIDEST_THEME_LINE_WIDTH, metrics.line_width)
    clearance = (
        max(DIRECTIONAL_MARKER_HALF_EXTENT, metrics.marker_half_extent) + line_width / 2
    )
    extra_stroke = line_width * (active_stroke_scale() - 1.0)
    return clearance + extra_stroke / 2


class LayoutMetrics(NamedTuple):
    """Spacing options a map may set, in force for the current pass.

    The defaults are the engine's own, for a pass run without a graph bound.
    """

    diagonal_run: float = DIAGONAL_RUN
    label_offset: float = LABEL_OFFSET
    section_x_padding: float = SECTION_X_PADDING
    section_y_padding: float = SECTION_Y_PADDING

    @classmethod
    def of(cls, graph: object) -> LayoutMetrics:
        """The metrics *graph*'s options set, the engine's defaults elsewhere."""
        values: list[float] = []
        for name, default in zip(cls._fields, cls(), strict=True):
            value = getattr(graph, name, None)
            values.append(default if value is None else value)
        return cls(*values)


_ACTIVE_LAYOUT_METRICS: ContextVar[LayoutMetrics] = ContextVar(
    "active_layout_metrics", default=LayoutMetrics()
)


def active_layout_metrics() -> LayoutMetrics:
    """The spacing options in force for the current pass."""
    return _ACTIVE_LAYOUT_METRICS.get()


@contextmanager
def layout_metrics_context(metrics: LayoutMetrics) -> Iterator[None]:
    """Lay out with *metrics* for the duration of the block."""
    token = _ACTIVE_LAYOUT_METRICS.set(metrics)
    try:
        yield
    finally:
        _ACTIVE_LAYOUT_METRICS.reset(token)


def diagonal_label_offset() -> float:
    """Extra drop for an angled label, kept in proportion to the label offset."""
    return active_layout_metrics().label_offset * DIAGONAL_LABEL_OFFSET / LABEL_OFFSET
