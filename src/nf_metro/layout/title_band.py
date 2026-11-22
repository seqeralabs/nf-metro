"""The canvas-top band the map title and a standalone logo occupy.

Render draws the title, and a logo not folded into the legend, in the canvas-top
padding.  Layout must keep every section's ink clear of that band, so the
band's depth is resolved before layout from the same facts render keys its
drawing on: whether chrome is drawn at all (``--bare``), and whether the legend
is drawn to take the logo (``%%metro legend: none`` and HTML output both leave
the logo standalone).  The finished layout is then moved below the band as one
translation.
"""

from __future__ import annotations

from nf_metro.layout.constants import (
    CANVAS_ORIGIN_MARGIN,
    LOGO_HEIGHT,
    LOGO_Y_STANDALONE,
    SECTION_HEADER_PROTRUSION,
    TITLE_BAND_BOTTOM,
    TITLE_BAND_HEADER_GAP,
)
from nf_metro.layout.phases.canvas import translate_graph
from nf_metro.parser.model import MetroGraph


def standalone_logo_bottom(logo_scale: float) -> float:
    """Lowest y a standalone logo drawn at ``logo_scale`` reaches."""
    return LOGO_Y_STANDALONE + LOGO_HEIGHT * logo_scale


def resolve_title_band_bottom(
    graph: MetroGraph, *, draws_chrome: bool, legend_drawn: bool
) -> float:
    """Lowest y the canvas-top title and standalone logo reach; 0 when neither draws.

    Any configured logo variant reserves its band, including a lone light- or
    dark-only one whose visibility turns on the display mode resolved at render
    time: an unused band costs blank space, a missing one an overlap.  A title
    beside a standalone logo is vertically centred on it, so it never reaches
    below the logo's bottom edge at the theme title sizes
    ``TITLE_BAND_BOTTOM`` is calibrated for.
    """
    if not draws_chrome:
        return 0.0
    bottom = TITLE_BAND_BOTTOM if graph.title else 0.0
    if graph.has_logo and not legend_drawn:
        bottom = max(bottom, standalone_logo_bottom(graph.logo_scale))
    return bottom


def title_band_bottom(graph: MetroGraph) -> float:
    """The band depth resolved by ``prepare_graph``, else a default SVG render's."""
    if graph.title_band_bottom is not None:
        return graph.title_band_bottom
    return resolve_title_band_bottom(
        graph, draws_chrome=True, legend_drawn=graph.legend_position != "none"
    )


def title_band_floors(graph: MetroGraph) -> list[tuple[float, float]]:
    """``(bbox_y, floor)`` per non-empty section: the lowest ``bbox_y`` that
    keeps its ink out of the band.  Empty when nothing is drawn there.

    A drawn section's header badge protrudes ``SECTION_HEADER_PROTRUSION``
    above its box; an implicit holder draws no badge, and its stations and
    labels sit inside its box, so the box top itself must clear the band.
    """
    band = title_band_bottom(graph)
    if band <= 0.0:
        return []
    return [
        (
            section.bbox_y,
            band + (0.0 if section.is_implicit else SECTION_HEADER_PROTRUSION),
        )
        for section in graph.sections.values()
        if section.bbox_h > 0
    ]


def outboard_ink_lift(graph: MetroGraph) -> float:
    """Least lift that keeps ink drawn above the section boxes clear of the band.

    A run looping over the topmost row is drawn in the canvas-top padding,
    where no section floor reaches it.  Render seats any such run at least
    ``CANVAS_ORIGIN_MARGIN`` below the map's canvas top (``title_band_lift``),
    whichever route drew it, so a lift that puts that line
    ``TITLE_BAND_HEADER_GAP`` below the band clears every one of them without
    knowing which routes take the top.  Every drawn band renders with those
    margins: ``--bare`` drops them only where it drops the band too.
    """
    band = title_band_bottom(graph)
    if band <= 0.0:
        return 0.0
    return max(0.0, band + TITLE_BAND_HEADER_GAP - CANVAS_ORIGIN_MARGIN[1])


def title_band_lift(graph: MetroGraph) -> float:
    """Downward shift seating every breaching section at least
    ``TITLE_BAND_HEADER_GAP`` past its floor, and no less than
    :func:`outboard_ink_lift`; 0 when neither binds.

    Rounded to a micro-pixel, which strips the noise the floor arithmetic
    leaves in the shift's last bits (16.800000000000011 for a 16.8px shift).
    That cleans the shift only: adding a shift that is not a power-of-two
    fraction rounds each moved coordinate, so a comparison that ties exactly
    in the unbanded layout (a label nudge, a corner radius) can tip either
    way.
    """
    shift = max(
        (
            floor + TITLE_BAND_HEADER_GAP - top
            for top, floor in title_band_floors(graph)
            if top < floor
        ),
        default=0.0,
    )
    return round(max(shift, outboard_ink_lift(graph)), 6)


def apply_title_band_lift(graph: MetroGraph) -> None:
    """Move the settled layout down clear of the band, as one translation.

    Every placement decision has been taken against a canvas top at y=0, as
    for a map with no band.  The shift is recorded as ``graph.title_band_lift``,
    the y routing and render measure canvas-top clearances from, so the routes
    are the unbanded map's translated too.
    """
    lift = title_band_lift(graph)
    translate_graph(graph, 0.0, lift)
    graph.title_band_lift = lift
