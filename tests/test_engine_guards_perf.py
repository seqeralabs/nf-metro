"""Equivalence + unit tests pinning the closed-form line-crossing guard.

For every (non-consumer station, route segment) pair in every gallery
fixture, the closed-form Liang-Barsky result must be at least as strict
as the legacy 21-sample result.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import _station_marker_bbox, compute_layout
from nf_metro.layout.geometry import segment_intersects_bbox as _segment_intersects_bbox
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph
from nf_metro.render.svg import apply_route_offsets

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _discover_fixtures() -> list[str]:
    """Return absolute paths of every ``%%metro``-format .mmd file under
    ``tests/fixtures/`` and ``examples/``.  Excludes Nextflow-format
    flowcharts (parser inputs, not layout inputs).
    """
    roots = [FIXTURES, FIXTURES / "topologies", EXAMPLES, EXAMPLES / "topologies"]
    if (EXAMPLES / "guide").exists():
        roots.append(EXAMPLES / "guide")
    seen: set[Path] = set()
    out: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for p in sorted(root.glob("*.mmd")):
            if p in seen:
                continue
            text = p.read_text(errors="ignore")
            if "%%metro" not in text:
                continue
            seen.add(p)
            out.append(str(p))
    return out


ALL_FIXTURES = _discover_fixtures()


def _layout(path_str: str, **kwargs) -> MetroGraph:
    """Parse a fixture and run the full layout pipeline."""
    path = Path(path_str)
    graph = parse_metro_mermaid(path.read_text())
    # Legacy fixtures under tests/fixtures/ preserve the implicit
    # center_ports=True default (parsed in-file for examples/).
    if path.is_relative_to(FIXTURES) and "center_ports" not in kwargs:
        graph.center_ports = True
    elif "center_ports" in kwargs:
        graph.center_ports = kwargs.pop("center_ports")
    compute_layout(graph, **kwargs)
    return graph


def _segment_crosses_bbox_sampled(
    p1: tuple[float, float],
    p2: tuple[float, float],
    bbox: tuple[float, float, float, float],
) -> bool:
    """Legacy 21-sample segment-bbox intersection, kept here as the
    reference oracle for the closed-form replacement."""
    x1, y1 = p1
    x2, y2 = p2
    bx1, by1, bx2, by2 = bbox
    if max(x1, x2) < bx1 or min(x1, x2) > bx2:
        return False
    if max(y1, y2) < by1 or min(y1, y2) > by2:
        return False
    for k in range(21):
        f = k / 20.0
        x = x1 + f * (x2 - x1)
        y = y1 + f * (y2 - y1)
        if bx1 <= x <= bx2 and by1 <= y <= by2:
            return True
    return False


def _iter_guard_triplets(fixture: str):
    """Yield ``(sid, bbox, route, segment_index, p1, p2)`` for every
    (non-consumer station, route, segment) triplet that the
    ``_guard_no_line_crosses_non_consumer`` body iterates over.

    Skips fixtures whose ``route_edges`` raises (rare; matches the
    guard's own ``try/except`` fallback).
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    try:
        routes = route_edges(graph, station_offsets=offsets)
    except Exception:  # noqa: BLE001 - matches guard fallback
        return
    route_pts = [(r, apply_route_offsets(r, offsets)) for r in routes]
    for sid in graph.stations:
        bbox = _station_marker_bbox(graph, sid, offsets=offsets)
        if bbox is None:
            continue
        station_lines = set(graph.station_lines(sid))
        for r, pts in route_pts:
            if r.line_id in station_lines:
                continue
            if r.edge.source == sid or r.edge.target == sid:
                continue
            for k in range(len(pts) - 1):
                yield sid, bbox, r, k, pts[k], pts[k + 1]


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_closed_form_at_least_as_strict_as_sampled(fixture):
    """The closed-form clip must never miss an intersection that the
    21-sample loop catches.  ``sampled=False, closed=True`` is allowed
    (corner-clip cases the sampling missed) and strictly an improvement.
    """
    for sid, bbox, r, k, p1, p2 in _iter_guard_triplets(fixture):
        sampled = _segment_crosses_bbox_sampled(p1, p2, bbox)
        closed = _segment_intersects_bbox(p1[0], p1[1], p2[0], p2[1], bbox)
        if sampled and not closed:
            pytest.fail(
                f"{fixture}: sampled-but-not-closed at station {sid!r} "
                f"route {r.line_id}/{r.edge.source}->{r.edge.target} "
                f"segment[{k}] ({p1[0]:.1f},{p1[1]:.1f})->"
                f"({p2[0]:.1f},{p2[1]:.1f}) bbox {bbox}"
            )


def test_closed_form_unit_cases():
    """Synthetic edge cases for ``segment_intersects_bbox`` that the
    gallery may not exercise (single-point segments, exact corner clip,
    1-pixel miss)."""
    bbox = (0.0, 0.0, 10.0, 10.0)
    # Vertical segment through centre.
    assert _segment_intersects_bbox(5, -5, 5, 15, bbox)
    # Horizontal segment through centre.
    assert _segment_intersects_bbox(-5, 5, 15, 5, bbox)
    # Diagonal corner-clip: passes through corner (10, 10) only.
    assert _segment_intersects_bbox(5, 15, 15, 5, bbox)
    # Diagonal grazing one corner from outside (1px miss).
    assert not _segment_intersects_bbox(11, 11, 20, 20, bbox)
    # Segment entirely outside in X.
    assert not _segment_intersects_bbox(20, 5, 30, 5, bbox)
    # Segment entirely outside in Y.
    assert not _segment_intersects_bbox(5, 20, 5, 30, bbox)
    # Single point inside.
    assert _segment_intersects_bbox(5, 5, 5, 5, bbox)
    # Single point outside.
    assert not _segment_intersects_bbox(15, 15, 15, 15, bbox)
