"""Station glyphs a theme can choose: tabs, terminus bars, interchange rings,
dots and station-number markers.

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
    "station_code",
    "station_tab_box",
    "uses_line_offsets",
]

import math
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, NamedTuple

import drawsvg as draw

from nf_metro.layout.constants import COORD_TOLERANCE_FINE
from nf_metro.layout.geometry import AxisFrame, lanes_run_along_x
from nf_metro.render.constants import effective_line_color
from nf_metro.render.ns import ns as _ns
from nf_metro.render.style import (
    GLYPH_DOT,
    GLYPH_DOTS,
    GLYPH_NUMBERED,
    GLYPH_RING,
    GLYPH_TICK,
    PAINT_LINE,
    RING_GROW,
    TERMINUS_BAR,
    Theme,
    line_paint_class,
    ordinal_code,
)

if TYPE_CHECKING:
    from nf_metro.parser.model import MetroGraph, MetroLine, Station

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
    carries; ``label_point`` is where its label sits, which a tab faces;
    ``line_offsets`` is each line's drawn offset, which a dot sits on, and
    ``inactive_line_ids`` the lines a dot in its line's colour greys for.
    """

    station: Station
    min_off: float
    max_off: float
    is_tb_vert: bool
    station_data: Mapping[str, str] = MappingProxyType({})
    muted: bool = False
    label_point: tuple[float, float] | None = None
    line_offsets: Mapping[str, float] = MappingProxyType({})
    inactive_line_ids: frozenset[str] = frozenset()

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


def _line_offset(bundle: Bundle, line_id: str, rank: int, count: int) -> float:
    """*line_id*'s drawn offset, else the *rank*-th of *count* even steps
    across the bundle."""
    if line_id in bundle.line_offsets:
        return bundle.line_offsets[line_id]
    if count == 1:
        return bundle.min_off
    return bundle.min_off + rank * (bundle.max_off - bundle.min_off) / (count - 1)


def _ring_paint(
    theme: Theme, line: MetroLine | None, bundle: Bundle, in_line_colour: bool
) -> tuple[str, str]:
    """``(stroke, class)`` of a dot or marker ring: in its line's colour, greyed
    when that line is inactive, or in ``station_stroke``, over a face in the
    station fill."""
    hole = _ns("nf-metro-ring-hole")
    if in_line_colour:
        muted = bundle.muted or (
            line is not None and line.id in bundle.inactive_line_ids
        )
        muted_ids = frozenset({line.id}) if muted and line else frozenset()
        stroke = effective_line_color(line, theme, muted_ids)
        paint = line_paint_class(theme, line, muted, "stroke")
    else:
        stroke = theme.station_stroke
        paint = maybe_muted_class("nf-metro-ring", bundle.muted)
    return stroke, f"{hole} {paint}".strip()


def _draw_dots(
    d: draw.Group | draw.Drawing,
    graph: MetroGraph,
    theme: Theme,
    bundle: Bundle,
    line_ids: Sequence[str],
) -> None:
    """Draw a dot on each of *line_ids* where it crosses the station.

    The outline is drawn as a stroke centred inside the dot's outside edge, so
    an outline half the diameter wide fills it.
    """
    lw = theme.line_width
    outer = theme.dot_diameter * lw / 2
    width = min(theme.dot_ring_width * lw, outer)
    group_el = draw.Group(**bundle.station_data)
    for rank, lid in enumerate(line_ids):
        stroke, cls = _ring_paint(
            theme, graph.lines.get(lid), bundle, theme.dot_ring == PAINT_LINE
        )
        group_el.append(
            draw.Circle(
                *bundle.point(_line_offset(bundle, lid, rank, len(line_ids))),
                outer - width / 2,
                fill=theme.station_fill,
                stroke=stroke,
                stroke_width=width,
                class_=cls,
            )
        )
    d.append(group_el)


def station_code(graph: MetroGraph, station: Station, style: str) -> tuple[str, str]:
    """``(section code, two-digit number)`` of *station*: its section's code
    and its place along that section's flow, counting the stations a
    ``numbered`` marker draws."""
    section = graph.sections.get(station.section_id or "")
    if section is None:
        return "", ""
    members = [
        st
        for sid in section.station_ids
        if (st := graph.stations.get(sid)) is not None
        and not st.is_port
        and not st.is_blank_terminus
    ]
    along_x = not lanes_run_along_x(section.direction)
    sign = AxisFrame.flow_sign(section.direction)

    def flow_key(st: Station) -> tuple[float, float]:
        along, across = (st.x, st.y) if along_x else (st.y, st.x)
        return sign * along, across

    members.sort(key=flow_key)
    place = next((i for i, st in enumerate(members, 1) if st.id == station.id), 0)
    return ordinal_code(section.number, style), f"{place:02d}"


# Station-number marker proportions, × the marker's outside diameter: the
# Tokyo Metro numbering mark's ring, code cap height and digit height, and
# where their baselines sit from the centre.
_NUMBER_RING = 0.1175
_CODE_FONT = 0.248 / 0.72
_CODE_BASELINE = -0.073
_DIGIT_FONT = 0.267 / 0.72
_DIGIT_BASELINE = 0.264


def _draw_number_marker(
    d: draw.Group | draw.Drawing,
    graph: MetroGraph,
    theme: Theme,
    bundle: Bundle,
    line_ids: Sequence[str],
) -> None:
    """Draw a station-number marker: the station's code on a face ringed in
    its one line's colour, or a capsule across several lines ringed in
    ``station_stroke``."""
    diameter = theme.number_marker_diameter * theme.line_width
    ring = _NUMBER_RING * diameter
    inset = diameter - ring
    lo, hi = bundle.min_off, bundle.max_off
    axis = bundle.axis
    group_el = draw.Group(**bundle.station_data)
    one_line = len(line_ids) == 1
    stroke, cls = _ring_paint(
        theme, graph.lines.get(line_ids[0]) if one_line else None, bundle, one_line
    )
    group_el.append(
        draw.Rectangle(
            *axis.box(inset, axis.base + lo - inset / 2, hi - lo + inset),
            rx=inset / 2,
            fill=theme.station_fill,
            stroke=stroke,
            stroke_width=ring,
            class_=cls,
        )
    )
    cx, cy = axis.point((lo + hi) / 2)
    code, number = station_code(graph, bundle.station, theme.section_codes)
    text_class = maybe_muted_class("nf-metro-station-code", bundle.muted)
    for text, font, baseline in (
        (code, _CODE_FONT, _CODE_BASELINE),
        (number, _DIGIT_FONT, _DIGIT_BASELINE),
    ):
        group_el.append(
            draw.Text(
                text,
                font * diameter,
                cx,
                cy + baseline * diameter,
                fill=theme.station_code_color,
                font_family=theme.label_font_family,
                font_weight="bold",
                text_anchor="middle",
                class_=text_class,
            )
        )
    d.append(group_el)


def _draw_unnumbered_end(
    d: draw.Group | draw.Drawing, theme: Theme, bundle: Bundle, line_count: int
) -> bool:
    """Draw a nameless line end under a ``numbered`` theme, which has no
    number to show: ``terminus_glyph`` across however many lines end there,
    rings standing in for the marker."""
    if theme.terminus_glyph == TERMINUS_BAR:
        _draw_terminus_bar(d, theme, bundle)
        return True
    if theme.terminus_glyph == GLYPH_RING:
        _draw_bundle_rings(d, bundle, line_count, theme)
        return True
    return False


def _draw_terminus_bar(
    d: draw.Group | draw.Drawing, theme: Theme, bundle: Bundle
) -> None:
    data = bundle.station_data
    bar_class = maybe_muted_class("nf-metro-terminus-bar", bundle.muted)
    d.append(
        draw.Rectangle(
            *bundle_end_bar_box(bundle, theme),
            fill=theme.station_stroke,
            **{**data, "class_": f"{data['class_']} {bar_class}"},
        )
    )


def uses_line_offsets(theme: Theme) -> bool:
    """Whether *theme* draws a glyph that sits on each line's own offset."""
    return theme.station_glyph == GLYPH_DOT or theme.interchange_glyph == GLYPH_DOTS


_GLYPH_DRAWERS = {
    GLYPH_DOT: _draw_dots,
    GLYPH_DOTS: _draw_dots,
    GLYPH_NUMBERED: _draw_number_marker,
}


def draw_line_station_glyph(
    d: draw.Group | draw.Drawing, graph: MetroGraph, theme: Theme, bundle: Bundle
) -> bool:
    """Draw a station as a tab, terminus bar or ring when its theme asks for one.

    Returns whether anything was drawn; a ``pill`` theme draws nothing here.
    """
    station = bundle.station
    data = bundle.station_data
    lines = graph.station_lines(station.id)
    numbers_blank_end = station.is_blank_terminus and GLYPH_NUMBERED in (
        theme.station_glyph,
        theme.interchange_glyph,
    )
    if numbers_blank_end:
        return _draw_unnumbered_end(d, theme, bundle, len(lines))
    if len(lines) == 1 and theme.station_glyph in _GLYPH_DRAWERS:
        _GLYPH_DRAWERS[theme.station_glyph](d, graph, theme, bundle, lines)
        return True
    if theme.station_glyph == GLYPH_TICK and len(lines) == 1:
        line = graph.lines.get(lines[0])
        color = effective_line_color(
            line,
            theme,
            frozenset(lines) if bundle.muted else frozenset(),
        )
        box = (
            bundle_end_bar_box(bundle, theme)
            if station.is_terminus
            else station_tab_box(bundle, theme)
        )
        paint = line_paint_class(theme, line, bundle.muted, "fill")
        extra = {"class_": f"{data['class_']} {paint}"} if paint else {}
        d.append(draw.Rectangle(*box, fill=color, **{**data, **extra}))
        return True
    if station.is_blank_terminus and len(lines) >= 2:
        if theme.terminus_glyph == TERMINUS_BAR:
            _draw_terminus_bar(d, theme, bundle)
            return True
        if theme.terminus_glyph != GLYPH_RING:
            return False
        if theme.interchange_glyph in _GLYPH_DRAWERS:
            _GLYPH_DRAWERS[theme.interchange_glyph](d, graph, theme, bundle, lines)
        else:
            _draw_bundle_rings(d, bundle, len(lines), theme)
        return True
    if len(lines) < 2:
        return False
    if theme.interchange_glyph == GLYPH_RING:
        _draw_bundle_rings(d, bundle, len(lines), theme)
        return True
    if theme.interchange_glyph in _GLYPH_DRAWERS:
        _GLYPH_DRAWERS[theme.interchange_glyph](d, graph, theme, bundle, lines)
        return True
    return False
