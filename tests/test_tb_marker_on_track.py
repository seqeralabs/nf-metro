"""A vertical-flow station's marker sits centred on the lines drawn through it.

A TB section draws each line at its offset *reversed* against the station's
bundle max (``_tb_x_offset``), so the marker pill must span the reversed
(drawn) offsets, not the stored ones -- otherwise a one-line or off-trunk-subset
station draws its glyph beside its own track (issue #929).  A BT section rides
the offsets unreversed.  Either way the pill lies across the flow, along X.
The first oracle compares the marker box centre (``station_marker_box``)
against the lane coordinates the *routes* actually arrive on, an independent
source from the marker geometry; the second holds the layout's marker box (the
one bypass insertion and the marker-crossing guards test against) and the
overlay box to that same drawn span, laid across the flow.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import _station_marker_bbox, compute_layout
from nf_metro.layout.geometry import lanes_run_along_x
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.svg import station_marker_box
from nf_metro.themes import THEMES

REPO_ROOT = Path(__file__).resolve().parent.parent
THEME = next(iter(THEMES.values()))


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    for sub in ("examples", "examples/topologies", "tests/fixtures/topologies"):
        paths.extend(sorted((REPO_ROOT / sub).glob("*.mmd")))
    return paths


def _line_lane_at(route, station_id: str) -> float | None:
    """The X the route arrives on at *station_id* (None if not an endpoint)."""
    if route.edge.target == station_id:
        return route.points[-1][0]
    if route.edge.source == station_id:
        return route.points[0][0]
    return None


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_tb_marker_centred_on_drawn_lines(path: Path) -> None:
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)

    for sid, station in graph.stations.items():
        if station.is_port or station.is_hidden:
            continue
        section = graph.sections.get(station.section_id) if station.section_id else None
        if section is None or not lanes_run_along_x(section.direction):
            continue
        if graph.station_is_rail(sid) or station.marker is not None:
            continue
        lanes = [x for r in routes if (x := _line_lane_at(r, sid)) is not None]
        if not lanes:
            continue
        marker_cx = station_marker_box(graph, THEME, station, offsets)[0]
        drawn_mid = (min(lanes) + max(lanes)) / 2
        assert abs(marker_cx - drawn_mid) < 1.0, (
            f"{path.name}: {section.direction} station {sid!r} marker centre "
            f"{marker_cx:.1f} is off the lines it carries "
            f"(drawn span {min(lanes):.1f}..{max(lanes):.1f})"
        )


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_vertical_marker_boxes_lie_across_the_flow(path: Path) -> None:
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    r = THEME.station_radius

    for sid, station in graph.stations.items():
        if station.is_port or station.is_hidden:
            continue
        section = graph.sections.get(station.section_id) if station.section_id else None
        if section is None or not lanes_run_along_x(section.direction):
            continue
        if graph.station_is_rail(sid) or station.marker is not None:
            continue
        lanes = [x for rt in routes if (x := _line_lane_at(rt, sid)) is not None]
        if not lanes:
            continue
        cx, cy, w, h, _ = station_marker_box(graph, THEME, station, offsets)
        overlay = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
        drawn = (min(lanes) - r, station.y - r, max(lanes) + r, station.y + r)
        assert overlay == pytest.approx(drawn, abs=1.0), (
            f"{path.name}: overlay box of {section.direction} station {sid!r} is "
            f"{overlay}, its lines span {drawn}"
        )
        bbox = _station_marker_bbox(graph, sid, offsets=offsets, radius=r)
        assert bbox == pytest.approx(overlay, abs=0.5), (
            f"{path.name}: layout marker box of {section.direction} station "
            f"{sid!r} is {bbox}, the drawn pill spans {overlay}"
        )
