"""Station glyphs a theme can choose: tabs, terminus bars and interchange rings.

Glyph sizes are multiples of the theme's ``line_width``. Offsets run across a
station's lines: along y in an LR/RL section, along x in a TB/BT one.
"""

from __future__ import annotations

__all__ = [
    "MUTED_CLASS",
    "Bundle",
    "CrossAxis",
    "RingLayout",
    "bundle_end_bar_box",
    "draw_line_station_glyph",
    "draw_ring_chain",
    "maybe_muted_class",
    "ring_groups",
    "ring_layout",
    "station_axis",
    "station_tab_box",
]

import math
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, NamedTuple

import drawsvg as draw

from nf_metro.layout.constants import COORD_TOLERANCE_FINE
from nf_metro.render.constants import effective_line_color
from nf_metro.render.ns import ns as _ns
from nf_metro.render.style import (
    GLYPH_RING,
    GLYPH_TICK,
    RING_GROW,
    TERMINUS_BAR,
    Theme,
    muted_paint_class,
)

if TYPE_CHECKING:
    from nf_metro.parser.model import MetroGraph, Station

MUTED_CLASS = "nf-metro-muted"
"""Class marking an element greyed because every line touching it is inactive."""


def maybe_muted_class(name: str, muted: bool) -> str:
    """Return *name*'s class attribute, carrying the muted marker when *muted*."""
    cls = _ns(name)
    return f"{cls} {_ns(MUTED_CLASS)}" if muted else cls


class CrossAxis(NamedTuple):
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


def station_axis(station: Station, is_tb_vert: bool) -> CrossAxis:
    """The cross axis through *station*, offsets measured from its position."""
    if is_tb_vert:
        return CrossAxis(station.y, station.x, True)
    return CrossAxis(station.x, station.y, False)


class Bundle(NamedTuple):
    """A station as drawn: the span of its lines across the flow, and how.

    ``station_data`` is the attributes every glyph element of the station
    carries; ``label_point`` is where its label sits, which a tab faces.
    """

    station: Station
    min_off: float
    max_off: float
    is_tb_vert: bool
    station_data: Mapping[str, str] = MappingProxyType({})
    muted: bool = False
    label_point: tuple[float, float] | None = None

    @property
    def axis(self) -> CrossAxis:
        """The cross axis through the station."""
        return station_axis(self.station, self.is_tb_vert)

    def point(self, offset: float) -> tuple[float, float]:
        """The station's position shifted *offset* across its lines."""
        return self.axis.point(offset)


def station_tab_box(bundle: Bundle, theme: Theme) -> tuple[float, float, float, float]:
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
    if bundle.label_point is not None:
        label_axis = bundle.label_point[0 if bundle.is_tb_vert else 1]
    forward = label_axis is not None and label_axis > line_axis
    lo = line_axis + lw / 2 if forward else line_axis - lw / 2 - reach
    return bundle.axis.box(theme.station_tick_width * lw, lo, reach)


def bundle_end_bar_box(
    bundle: Bundle, theme: Theme
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


def draw_ring_chain(
    d: draw.Group | draw.Drawing,
    groups: Sequence[RingLayout],
    axis: CrossAxis,
    theme: Theme,
    station_data: Mapping[str, str],
    muted: bool,
) -> None:
    """Draw interchange rings, one :class:`RingLayout` per track.

    Drawn in layers so a chain reads as one outline around joined centres:
    dumbbell links between tracks, every ring's outline, every ring's inside
    (which covers any neighbouring outline crossing it), then a strip in the
    station fill from the first centre to the last, through the outlines where
    rings meet. Ring centres are offsets along *axis*.
    """
    lw = theme.line_width
    stroke = theme.ring_stroke_width * lw
    half_bridge = theme.ring_bridge_width * lw / 2
    fill = theme.station_fill
    color = theme.station_stroke

    group_el = draw.Group(**station_data)
    for prev, nxt in zip(groups, groups[1:]):
        group_el.append(
            draw.Rectangle(
                *axis.strip(prev.centres[-1], nxt.centres[0], half_bridge + stroke),
                fill=color,
                class_=maybe_muted_class("nf-metro-ring-link", muted),
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
                class_=maybe_muted_class("nf-metro-ring", muted),
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


def _draw_bundle_rings(
    d: draw.Group | draw.Drawing, bundle: Bundle, line_count: int, theme: Theme
) -> None:
    """Draw the ring, or chain of rings, across a station's bundle."""
    layout = ring_layout(bundle.min_off, bundle.max_off, line_count, theme)
    draw_ring_chain(d, [layout], bundle.axis, theme, bundle.station_data, bundle.muted)


def draw_line_station_glyph(
    d: draw.Group | draw.Drawing, graph: MetroGraph, theme: Theme, bundle: Bundle
) -> bool:
    """Draw a station as a tab, terminus bar or ring when its theme asks for one.

    Returns whether anything was drawn; a ``pill`` theme draws nothing here.
    """
    station = bundle.station
    data = bundle.station_data
    lines = graph.station_lines(station.id)
    if theme.station_glyph == GLYPH_TICK and len(lines) == 1:
        color = effective_line_color(
            graph.lines.get(lines[0]),
            theme,
            frozenset(lines) if bundle.muted else frozenset(),
        )
        box = (
            bundle_end_bar_box(bundle, theme)
            if station.is_terminus
            else station_tab_box(bundle, theme)
        )
        paint = muted_paint_class(theme, bundle.muted, "fill")
        extra = {"class_": f"{data['class_']} {paint}"} if paint else {}
        d.append(draw.Rectangle(*box, fill=color, **{**data, **extra}))
        return True
    if station.is_blank_terminus and len(lines) >= 2:
        if theme.terminus_glyph == TERMINUS_BAR:
            bar_class = maybe_muted_class("nf-metro-terminus-bar", bundle.muted)
            d.append(
                draw.Rectangle(
                    *bundle_end_bar_box(bundle, theme),
                    fill=theme.station_stroke,
                    **{**data, "class_": f"{data['class_']} {bar_class}"},
                )
            )
            return True
        if theme.terminus_glyph != GLYPH_RING:
            return False
        _draw_bundle_rings(d, bundle, len(lines), theme)
        return True
    if theme.interchange_glyph == GLYPH_RING and len(lines) >= 2:
        _draw_bundle_rings(d, bundle, len(lines), theme)
        return True
    return False
