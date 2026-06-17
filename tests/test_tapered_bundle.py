"""Tests for tapering inter-section bundles.

An inter-section L-shape connects a source fan (exit port / merge junction)
to a target entry trunk.  When the source-side spread and target-side spread
differ the bundle *tapers*: a single rigid perpendicular offset cannot land
each line on its true offset at both ends.  These tests pin:

* the builder primitive ``build_tapered_bundle`` -- rigid input (source offset
  == target offset) is byte-identical to ``build_concentric_bundle``, and a
  tapering input lands each line on its own source offset and target offset
  with concentric / transition corners;
* the end-to-end render -- a tapering bundle (the ``complex_multipath``
  junction -> ``standard_analysis`` case, plus any other tapering L-shape
  bundle discovered in the corpus) enters its target trunk at the correct
  per-line spread rather than collapsing every line onto one point.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import COORD_TOLERANCE
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.bundle import (
    build_concentric_bundle,
    build_tapered_bundle,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge
from nf_metro.render.svg import apply_route_offsets

REPO_ROOT = Path(__file__).resolve().parent.parent
TOPO = REPO_ROOT / "examples" / "topologies"


# ---------------------------------------------------------------------------
# Builder-level: rigid byte-identity + taper lands on offset
# ---------------------------------------------------------------------------


def _edge(line_id: str) -> Edge:
    return Edge(source="__src__", target="__tgt__", line_id=line_id)


def test_tapered_rigid_matches_concentric() -> None:
    """src offset == tgt offset reproduces ``build_concentric_bundle`` exactly."""
    centerline = [(0.0, 0.0), (20.0, 0.0), (20.0, -100.0), (40.0, -100.0)]
    offs = [("a", -3.0), ("b", 0.0), ("c", 3.0)]

    rigid = build_concentric_bundle(
        [(_edge(lid), lid, s) for lid, s in offs], centerline, base_radius=11.5
    )
    tapered = build_tapered_bundle(
        [(_edge(lid), lid, s, s) for lid, s in offs],
        centerline,
        transition_leg=1,
        base_radius=11.5,
    )

    assert len(rigid) == len(tapered)
    for r, t in zip(rigid, tapered):
        assert t.line_id == r.line_id
        assert t.points == r.points
        assert t.curve_radii == r.curve_radii
        assert t.offsets_applied == r.offsets_applied


def test_tapered_lands_on_both_offsets() -> None:
    """A 6px source / 3px target taper lands each line on its own offset.

    Centreline H -> V -> H, transition at the first corner: the source
    horizontal carries the source offset, the channel and target horizontal
    carry the target offset.
    """
    centerline = [(0.0, 0.0), (20.0, 0.0), (20.0, -100.0), (40.0, -100.0)]
    members = [
        (_edge("std"), "std", -3.0, -1.5),
        (_edge("leg"), "leg", 3.0, 1.5),
    ]
    routes = build_tapered_bundle(
        members, centerline, transition_leg=1, base_radius=11.5
    )
    by_line = {r.line_id: r for r in routes}

    # Source horizontal Y carries the source offset (6px spread).
    assert by_line["std"].points[0][1] == pytest.approx(-3.0)
    assert by_line["leg"].points[0][1] == pytest.approx(3.0)
    # Target horizontal Y carries the target offset (3px spread).
    assert by_line["std"].points[-1][1] == pytest.approx(-100.0 - 1.5)
    assert by_line["leg"].points[-1][1] == pytest.approx(-100.0 + 1.5)
    # Channel X carries the target offset (the corridor fan).
    assert by_line["std"].points[1][0] == pytest.approx(20.0 - 1.5)
    assert by_line["leg"].points[1][0] == pytest.approx(20.0 + 1.5)
    # Baked, so the renderer must not re-apply or deform.
    assert all(r.offsets_applied for r in routes)
    assert all(r.normalize_exempt for r in routes)


# ---------------------------------------------------------------------------
# End-to-end: tapering bundles preserve both spreads in the render
# ---------------------------------------------------------------------------


def _final_polylines(graph) -> list[tuple[Edge, list[tuple[float, float]]]]:
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return [(r.edge, apply_route_offsets(r, offsets)) for r in routes]


def _is_inter(graph, sid: str) -> bool:
    st = graph.stations.get(sid)
    return st is not None and (st.is_port or sid in graph.junction_ids)


def _horizontal(p0: tuple[float, float], p1: tuple[float, float]) -> bool:
    return abs(p1[0] - p0[0]) >= abs(p1[1] - p0[1])


def _lr_lshape_tapers(graph):
    """Tapering L-shapes of the LEFT/RIGHT-port family, per fixture.

    Yields ``(s, t, src_spread, tgt_spread, src_ys, tgt_ys)`` for each
    inter-section bundle whose rendered route enters and leaves on a horizontal
    leg (the H -> V -> H L-shape produced by the migrated handlers) and whose
    per-line source and target Y offsets span different widths.  TOP/BOTTOM-port
    families (vertical end legs, X-axis fan) are a different regime and excluded.
    """
    offsets = compute_station_offsets(graph)
    polys = _final_polylines(graph)
    groups: dict[tuple[str, str], list[tuple[Edge, list[tuple[float, float]]]]] = {}
    for e, pts in polys:
        if _is_inter(graph, e.source) and _is_inter(graph, e.target):
            groups.setdefault((e.source, e.target), []).append((e, pts))

    for (s, t), bundle in groups.items():
        if len(bundle) < 2:
            continue
        if not all(
            _horizontal(pts[0], pts[1]) and _horizontal(pts[-1], pts[-2])
            for _e, pts in bundle
        ):
            continue
        src = [offsets.get((s, e.line_id), 0.0) for e, _pts in bundle]
        tgt = [offsets.get((t, e.line_id), 0.0) for e, _pts in bundle]
        src_spread = max(src) - min(src)
        tgt_spread = max(tgt) - min(tgt)
        if abs(src_spread - tgt_spread) <= COORD_TOLERANCE:
            continue
        src_ys = [pts[0][1] for _e, pts in bundle]
        tgt_ys = [pts[-1][1] for _e, pts in bundle]
        yield s, t, src_spread, tgt_spread, src_ys, tgt_ys


def _fixtures_with_taper() -> list[str]:
    names = []
    for p in sorted(TOPO.glob("*.mmd")):
        graph = parse_metro_mermaid(p.read_text())
        compute_layout(graph)
        if any(True for _ in _lr_lshape_tapers(graph)):
            names.append(p.stem)
    return names


def test_complex_multipath_taper_present() -> None:
    """The named fixture really does carry a tapering L-shape (guards the test)."""
    assert "complex_multipath" in _fixtures_with_taper()


@pytest.mark.parametrize("fixture", _fixtures_with_taper())
def test_tapering_lshape_preserves_both_spreads(fixture: str) -> None:
    """Each tapering L-shape enters/leaves at its true per-line spread.

    A rigid bundle would bake the source spread onto the target endpoints,
    spanning the source width rather than the trunk's; a tapering bundle spans
    ``src_spread`` at the source endpoints and ``tgt_spread`` at the target.
    """
    graph = parse_metro_mermaid((TOPO / f"{fixture}.mmd").read_text())
    compute_layout(graph)

    for s, t, src_spread, tgt_spread, src_ys, tgt_ys in _lr_lshape_tapers(graph):
        got_src = max(src_ys) - min(src_ys)
        got_tgt = max(tgt_ys) - min(tgt_ys)
        assert got_src == pytest.approx(src_spread, abs=COORD_TOLERANCE), (
            f"{fixture}: {s}->{t} source spread {got_src} != {src_spread}"
        )
        assert got_tgt == pytest.approx(tgt_spread, abs=COORD_TOLERANCE), (
            f"{fixture}: {s}->{t} target spread {got_tgt} != {tgt_spread}"
        )
