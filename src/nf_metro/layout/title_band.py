"""Depth of the canvas-top band the map title and a standalone logo occupy.

Render draws the title, and a logo not folded into the legend, in the canvas-top
padding.  Layout must keep every section's ink clear of that band, so the
band's depth is resolved before layout from the same facts render keys its
drawing on: whether chrome is drawn at all (``--bare``), and whether the legend
is drawn to take the logo (``%%metro legend: none`` and HTML output both leave
the logo standalone).
"""

from __future__ import annotations

from nf_metro.layout.constants import (
    LOGO_HEIGHT,
    LOGO_Y_STANDALONE,
    OVER_TOP_HEADROOM,
    SECTION_HEADER_PROTRUSION,
    TITLE_BAND_BOTTOM,
    TITLE_BAND_HEADER_GAP,
)
from nf_metro.layout.section_placement import over_top_headroom_rows
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
    drawn beside the logo is centred on it, so at the theme title sizes
    ``TITLE_BAND_BOTTOM`` is calibrated for it stays within this band.
    """
    if not draws_chrome:
        return 0.0
    bottom = TITLE_BAND_BOTTOM if graph.title else 0.0
    has_logo = bool(graph.logo_path or graph.logo_path_light or graph.logo_path_dark)
    if has_logo and not legend_drawn:
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
    """``(bbox_y, floor)`` for every non-empty section, where ``floor`` is the
    least ``bbox_y`` keeping what it draws above its box out of the band.
    Empty when nothing is drawn at the canvas top.

    A drawn section's header badge protrudes ``SECTION_HEADER_PROTRUSION``
    above its box, or an over-the-top loop ``OVER_TOP_HEADROOM`` in a row
    :func:`~nf_metro.layout.section_placement.over_top_headroom_rows` names; an
    implicit holder draws no badge, and its stations and labels sit inside its
    box, so the box top itself must clear the band.
    """
    band = title_band_bottom(graph)
    if band <= 0.0:
        return []
    over_top_rows = over_top_headroom_rows(graph)
    floors = []
    for section in graph.sections.values():
        if section.bbox_h <= 0:
            continue
        if section.is_implicit:
            above = 0.0
        elif section.grid_row in over_top_rows:
            above = OVER_TOP_HEADROOM
        else:
            above = SECTION_HEADER_PROTRUSION
        floors.append((section.bbox_y, band + above))
    return floors


def title_band_lift(graph: MetroGraph) -> float:
    """Downward shift seating every breaching section ``TITLE_BAND_HEADER_GAP``
    past its floor; 0 when none breaches, so a clear map is never moved."""
    return max(
        (
            floor + TITLE_BAND_HEADER_GAP - top
            for top, floor in title_band_floors(graph)
            if top < floor
        ),
        default=0.0,
    )
