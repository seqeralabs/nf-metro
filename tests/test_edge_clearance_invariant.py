"""Inter-section descent channels must clear section bbox edges.

A vertical descent channel of an inter-section route may legitimately
sit on a section edge when it is a port-to-port connection: the descent
x coincides with an exit or entry port that genuinely lives on that edge
(serpentine right-exit dropping to a right-entry below, around-section
exit drops, etc.).

It is a bug when the graze is *incidental*: the descent x falls within
``EDGE_TO_BUNDLE_CLEARANCE`` of a section bbox edge (and on the interior
side of it) yet is NOT a port at either endpoint of the route.  The line
then visibly grazes / crosses the section border.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import EDGE_TO_BUNDLE_CLEARANCE
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
TOPOLOGIES = FIXTURES / "topologies"
EXAMPLES = REPO_ROOT / "examples"

# Tolerance for "x coincides with a port that lives on this edge".
_PORT_TOL = 1.0
# Tolerance for "this vertical segment is actually vertical".
_V_TOL = 1.0


STACKED_FIXTURE = FIXTURES / "regressions" / "stacked_collector_fanin.mmd"


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = [STACKED_FIXTURE]
    paths.extend(sorted(TOPOLOGIES.glob("*.mmd")))
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
    return paths


def _route(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, routes


def _endpoint_port_xs(graph, route) -> list[float]:
    """X coordinates of any port stations at the route's two endpoints."""
    xs: list[float] = []
    for sid in (route.edge.source, route.edge.target):
        st = graph.stations.get(sid)
        if st is not None and getattr(st, "is_port", False):
            xs.append(st.x)
    return xs


def _incidental_grazes(graph, routes) -> list[str]:
    """Return human-readable descriptions of incidental edge grazes."""
    violations: list[str] = []
    for route in routes:
        if not route.is_inter_section:
            continue
        pts = route.points
        port_xs = _endpoint_port_xs(graph, route)
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            if abs(bx - ax) > _V_TOL:
                continue  # not a vertical segment
            vx = (ax + bx) / 2
            ylo, yhi = (ay, by) if ay <= by else (by, ay)
            for sec in graph.sections.values():
                if sec.bbox_w <= 0:
                    continue
                left = sec.bbox_x
                right = sec.bbox_x + sec.bbox_w
                top = sec.bbox_y
                bot = sec.bbox_y + sec.bbox_h
                # Vertical segment must overlap the section's Y span.
                if yhi < top or ylo > bot:
                    continue
                # Distance inside each edge (positive = interior side).
                from_left = vx - left
                from_right = right - vx
                grazes_left = -_V_TOL <= from_left < EDGE_TO_BUNDLE_CLEARANCE
                grazes_right = -_V_TOL <= from_right < EDGE_TO_BUNDLE_CLEARANCE
                if not (grazes_left or grazes_right):
                    continue
                # Legitimate when vx coincides with an endpoint port.
                if any(abs(vx - px) <= _PORT_TOL for px in port_xs):
                    continue
                edge_x = left if grazes_left else right
                violations.append(
                    f"{route.edge.source} -> {route.edge.target} "
                    f"[{route.line_id}] descent x={vx:.1f} grazes "
                    f"section {sec.id!r} edge x={edge_x:.1f} "
                    f"(gap {abs(vx - edge_x):.1f} < {EDGE_TO_BUNDLE_CLEARANCE})"
                )
    return violations


@pytest.mark.parametrize("path", _gather_fixtures(), ids=lambda p: p.stem)
def test_no_incidental_edge_graze(path: Path) -> None:
    graph, routes = _route(path)
    violations = _incidental_grazes(graph, routes)
    assert not violations, "Incidental section-edge grazes:\n" + "\n".join(violations)


def test_collector_merge_descent_clears_source_right_edge() -> None:
    """Targeted check for #423.

    The ``__junction_8 -> __merge_*`` MultiQC fan-in descent must sit at
    least ``EDGE_TO_BUNDLE_CLEARANCE`` outside preprocessing's right edge.
    """
    graph, routes = _route(STACKED_FIXTURE)
    prep = graph.sections["preprocessing"]
    right_edge = prep.bbox_x + prep.bbox_w

    descents: list[float] = []
    for route in routes:
        if route.edge.source != "__junction_8":
            continue
        if not route.edge.target.startswith("__merge_"):
            continue
        pts = route.points
        for (ax, _ay), (bx, _by) in zip(pts, pts[1:]):
            if abs(bx - ax) <= _V_TOL:
                descents.append((ax + bx) / 2)

    assert descents, "expected junction_8 -> merge descent segments"
    nearest = min(descents, key=lambda x: abs(x - right_edge))
    # Outward (the section interior is to the LEFT) means descent x must
    # be >= right_edge + clearance.
    assert nearest >= right_edge + EDGE_TO_BUNDLE_CLEARANCE, (
        f"descent x={nearest:.1f} not clear of preprocessing right edge "
        f"{right_edge:.1f} by {EDGE_TO_BUNDLE_CLEARANCE} "
        f"(need >= {right_edge + EDGE_TO_BUNDLE_CLEARANCE:.1f})"
    )
