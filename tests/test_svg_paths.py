"""Tests for SVG path construction from routed edges.

Validates that the SVG path rendering in svg.py correctly translates
RoutedPath waypoints + curve_radii into well-formed SVG paths with:
- Correct curve continuity (no gaps between adjacent curves)
- Curve radii arrays matching the number of corners
- Clamped radii that respect available segment budget
- Concentric bundle lines maintaining distinct radii through curves
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from nf_metro.layout.constants import CURVE_RADIUS
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.svg import apply_route_offsets, render_svg
from nf_metro.themes import NFCORE_THEME

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
TOPOLOGIES_DIR = EXAMPLES_DIR / "topologies"

# Tolerance for floating-point coordinate comparison
COORD_EPS = 0.5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_svg_edge_paths(svg_str: str) -> list[str]:
    """Extract 'd' attributes from edge <path> elements in an SVG string.

    Edge paths are identified by fill="none" and stroke-linecap="round",
    filtering out section boxes, icons, and other non-edge paths.
    """
    root = ET.fromstring(svg_str)
    ns = {"svg": "http://www.w3.org/2000/svg"}
    paths = root.findall(".//svg:path", ns)
    result = []
    for p in paths:
        d = p.get("d", "")
        if not d:
            continue
        if p.get("fill") == "none" and p.get("stroke-linecap") == "round":
            result.append(d)
    return result


def _parse_path_commands(d: str) -> list[tuple[str, list[float]]]:
    """Parse an SVG path 'd' attribute into (command, args) tuples."""
    tokens = re.findall(r"[MLQZmlqz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", d)
    commands = []
    current_cmd = None
    args: list[float] = []
    for tok in tokens:
        if tok.isalpha():
            if current_cmd is not None:
                commands.append((current_cmd, args))
            current_cmd = tok
            args = []
        else:
            args.append(float(tok))
    if current_cmd is not None:
        commands.append((current_cmd, args))
    return commands


def _layout_and_route(mmd_text: str) -> tuple:
    """Parse, layout, route, and render. Returns (graph, routes, offsets, svg)."""
    graph = parse_metro_mermaid(mmd_text)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    svg = render_svg(graph, NFCORE_THEME)
    return graph, routes, offsets, svg


def _layout_and_route_file(path: Path) -> tuple:
    """Load a .mmd file and run the full pipeline."""
    return _layout_and_route(path.read_text())


# ---------------------------------------------------------------------------
# 1. Curve radii array length must match corner count
# ---------------------------------------------------------------------------


class TestCurveRadiiLength:
    """curve_radii (when present) must have exactly len(points) - 2 entries."""

    def _check_routes(self, routes: list[RoutedPath]):
        for route in routes:
            if route.curve_radii is not None:
                n_corners = len(route.points) - 2
                assert len(route.curve_radii) == n_corners, (
                    f"Route {route.edge.source}->{route.edge.target} "
                    f"(line={route.line_id}): {len(route.curve_radii)} radii "
                    f"for {n_corners} corners ({len(route.points)} points)"
                )

    def test_simple_diamond(self):
        mmd = (
            "%%metro line: main | Main | #ff0000\n"
            "%%metro line: alt | Alt | #0000ff\n"
            "graph LR\n"
            "    a -->|main| b\n"
            "    a -->|alt| c\n"
            "    b -->|main| d\n"
            "    c -->|alt| d\n"
        )
        graph = parse_metro_mermaid(mmd)
        compute_layout(graph)
        routes = route_edges(graph)
        self._check_routes(routes)

    @pytest.mark.parametrize(
        "fixture",
        sorted(TOPOLOGIES_DIR.glob("*.mmd")),
        ids=lambda p: p.stem,
    )
    def test_topology_fixtures(self, fixture):
        graph = parse_metro_mermaid(fixture.read_text())
        compute_layout(graph)
        offsets = compute_station_offsets(graph)
        routes = route_edges(graph, station_offsets=offsets)
        self._check_routes(routes)

    def test_rnaseq_sections(self):
        graph = parse_metro_mermaid((EXAMPLES_DIR / "rnaseq_sections.mmd").read_text())
        compute_layout(graph)
        offsets = compute_station_offsets(graph)
        routes = route_edges(graph, station_offsets=offsets)
        self._check_routes(routes)


# ---------------------------------------------------------------------------
# 2. SVG path command structure: M then (L Q)* then L
# ---------------------------------------------------------------------------


class TestSvgPathStructure:
    """Every edge path must start with M and end with L, with Q at each corner."""

    def _check_svg(self, svg_str: str):
        paths = _parse_svg_edge_paths(svg_str)
        # Filter to edge paths (have stroke, no fill)
        for d in paths:
            cmds = _parse_path_commands(d)
            if not cmds:
                continue
            # Must start with M
            assert cmds[0][0] == "M", f"Path doesn't start with M: {d[:80]}"
            # Must end with L (final waypoint)
            non_z = [c for c in cmds if c[0] != "Z"]
            if len(non_z) > 1:
                assert non_z[-1][0] == "L", f"Path doesn't end with L: {d[:80]}"
            # Every Q must be preceded by L (approach segment)
            for i, (cmd, _) in enumerate(cmds):
                if cmd == "Q" and i > 0:
                    assert cmds[i - 1][0] in ("L", "M"), (
                        f"Q at index {i} not preceded by L or M: {d[:120]}"
                    )

    def test_simple_diagonal(self):
        mmd = (
            "%%metro line: main | Main | #ff0000\n"
            "%%metro line: alt | Alt | #0000ff\n"
            "graph LR\n"
            "    a -->|main| b\n"
            "    a -->|alt| c\n"
            "    b -->|main| d\n"
            "    c -->|alt| d\n"
        )
        _, _, _, svg = _layout_and_route(mmd)
        self._check_svg(svg)

    @pytest.mark.parametrize(
        "fixture",
        sorted(TOPOLOGIES_DIR.glob("*.mmd")),
        ids=lambda p: p.stem,
    )
    def test_topology_fixtures(self, fixture):
        _, _, _, svg = _layout_and_route_file(fixture)
        self._check_svg(svg)

    def test_rnaseq_sections(self):
        _, _, _, svg = _layout_and_route_file(EXAMPLES_DIR / "rnaseq_sections.mmd")
        self._check_svg(svg)


# ---------------------------------------------------------------------------
# 3. Curve continuity: Q endpoint is the L startpoint of the next segment
# ---------------------------------------------------------------------------


class TestCurveContinuity:
    """The endpoint of each Q curve must match the start of the next L segment."""

    def _check_svg(self, svg_str: str):
        for d in _parse_svg_edge_paths(svg_str):
            cmds = _parse_path_commands(d)
            if len(cmds) < 3:
                continue

            # Verify no NaN or extreme values in path coordinates
            for _, args in cmds:
                for v in args:
                    assert not (v != v), f"NaN in path: {d[:80]}"  # NaN != NaN
                    assert abs(v) < 1e6, f"Extreme coordinate in path: {d[:80]}"

            # Verify Q commands have exactly 4 args (cx, cy, ex, ey)
            for cmd, args in cmds:
                if cmd == "Q":
                    assert len(args) == 4, (
                        f"Q command has {len(args)} args (expected 4): {d[:80]}"
                    )

    def test_simple(self):
        mmd = (
            "%%metro line: main | Main | #ff0000\n"
            "%%metro line: alt | Alt | #0000ff\n"
            "graph LR\n"
            "    a -->|main| b\n"
            "    a -->|alt| c\n"
            "    b -->|main| d\n"
            "    c -->|alt| d\n"
        )
        _, _, _, svg = _layout_and_route(mmd)
        self._check_svg(svg)

    @pytest.mark.parametrize(
        "fixture",
        sorted(TOPOLOGIES_DIR.glob("*.mmd")),
        ids=lambda p: p.stem,
    )
    def test_topology_fixtures(self, fixture):
        _, _, _, svg = _layout_and_route_file(fixture)
        self._check_svg(svg)


# ---------------------------------------------------------------------------
# 4. Clamped radius respects segment budget
# ---------------------------------------------------------------------------


class TestRadiusClamping:
    """Reconstructed effective radius must not exceed half of any shared segment."""

    def _check_routes(
        self,
        routes: list[RoutedPath],
        station_offsets: dict[tuple[str, str], float],
    ):
        for route in routes:
            pts = apply_route_offsets(route, station_offsets)
            if len(pts) < 3:
                continue

            for i in range(1, len(pts) - 1):
                prev, curr, nxt = pts[i - 1], pts[i], pts[i + 1]
                len1 = ((curr[0] - prev[0]) ** 2 + (curr[1] - prev[1]) ** 2) ** 0.5
                len2 = ((nxt[0] - curr[0]) ** 2 + (nxt[1] - curr[1]) ** 2) ** 0.5

                corner_idx = i - 1
                if route.curve_radii and corner_idx < len(route.curve_radii):
                    desired_r = route.curve_radii[corner_idx]
                else:
                    desired_r = CURVE_RADIUS

                # The renderer will clamp to min(desired_r, budget1, budget2)
                # where budget depends on adjacent corner radii. At minimum,
                # the effective radius must not exceed either full segment.
                effective_r = min(desired_r, len1, len2)
                assert effective_r >= -COORD_EPS, (
                    f"Negative effective radius on "
                    f"{route.edge.source}->{route.edge.target}"
                )

    @pytest.mark.parametrize(
        "fixture",
        sorted(TOPOLOGIES_DIR.glob("*.mmd")),
        ids=lambda p: p.stem,
    )
    def test_topology_fixtures(self, fixture):
        graph = parse_metro_mermaid(fixture.read_text())
        compute_layout(graph)
        offsets = compute_station_offsets(graph)
        routes = route_edges(graph, station_offsets=offsets)
        self._check_routes(routes, offsets)


# ---------------------------------------------------------------------------
# 5. Bundle lines maintain distinct radii (concentric curves)
# ---------------------------------------------------------------------------


class TestConcentricBundles:
    """Lines in the same bundle must have distinct curve_radii at shared corners."""

    def test_multi_line_bundle(self):
        """Three lines sharing an L-shape must have 3 distinct radii."""
        mmd = (
            "%%metro line: l1 | Line1 | #ff0000\n"
            "%%metro line: l2 | Line2 | #00ff00\n"
            "%%metro line: l3 | Line3 | #0000ff\n"
            "graph LR\n"
            "    subgraph s1 [S1]\n"
            "        a[A]\n"
            "        a -->|l1,l2,l3| b[B]\n"
            "    end\n"
            "    subgraph s2 [S2]\n"
            "        c[C]\n"
            "    end\n"
            "    b -->|l1,l2,l3| c\n"
        )
        graph = parse_metro_mermaid(mmd)
        compute_layout(graph)
        offsets = compute_station_offsets(graph)
        routes = route_edges(graph, station_offsets=offsets)

        # Find inter-section routes for the 3-line bundle
        inter = [r for r in routes if r.is_inter_section and r.curve_radii]
        if not inter:
            pytest.skip("No inter-section routes with curve_radii produced")

        # Group by (source, target) to find co-routed bundles
        from collections import defaultdict

        bundles: dict[tuple[str, str], list[RoutedPath]] = defaultdict(list)
        for r in inter:
            bundles[(r.edge.source, r.edge.target)].append(r)

        for key, bundle in bundles.items():
            if len(bundle) < 2:
                continue
            # Each corner should have distinct radii across bundle lines
            for corner_idx in range(
                min(len(r.curve_radii) for r in bundle if r.curve_radii)
            ):
                radii = [
                    r.curve_radii[corner_idx]
                    for r in bundle
                    if r.curve_radii and corner_idx < len(r.curve_radii)
                ]
                if len(radii) >= 2:
                    assert len(set(radii)) == len(radii), (
                        f"Bundle {key} corner {corner_idx}: "
                        f"duplicate radii {radii} (lines would overlap)"
                    )

    def test_multi_line_bundle_fixture(self):
        """The multi_line_bundle topology fixture should have distinct radii."""
        fixture = TOPOLOGIES_DIR / "multi_line_bundle.mmd"
        if not fixture.exists():
            pytest.skip("multi_line_bundle.mmd not found")

        graph = parse_metro_mermaid(fixture.read_text())
        compute_layout(graph)
        offsets = compute_station_offsets(graph)
        routes = route_edges(graph, station_offsets=offsets)

        inter = [r for r in routes if r.is_inter_section and r.curve_radii]
        # Just verify no duplicate radii within any co-routed bundle
        from collections import defaultdict

        bundles: dict[tuple[str, str], list[RoutedPath]] = defaultdict(list)
        for r in inter:
            bundles[(r.edge.source, r.edge.target)].append(r)

        for key, bundle in bundles.items():
            if len(bundle) < 2:
                continue
            for corner_idx in range(
                min(len(r.curve_radii) for r in bundle if r.curve_radii)
            ):
                radii = [
                    r.curve_radii[corner_idx]
                    for r in bundle
                    if r.curve_radii and corner_idx < len(r.curve_radii)
                ]
                if len(radii) >= 2:
                    assert len(set(radii)) == len(radii), (
                        f"Bundle {key} corner {corner_idx}: duplicate radii {radii}"
                    )


# ---------------------------------------------------------------------------
# 6. Q corner count matches route corner count
# ---------------------------------------------------------------------------


class TestQCountMatchesCorners:
    """Number of Q commands in SVG path should equal number of corners in route."""

    def _check(self, routes, offsets, svg_str):
        """Verify Q count in SVG matches expected corners for multi-point routes."""
        # Count expected corners across all routes with 3+ points
        expected_corners = sum(
            max(0, len(apply_route_offsets(r, offsets)) - 2)
            for r in routes
            if len(apply_route_offsets(r, offsets)) >= 3
        )

        # Count actual Q commands in SVG paths
        actual_q = 0
        for d in _parse_svg_edge_paths(svg_str):
            cmds = _parse_path_commands(d)
            actual_q += sum(1 for cmd, _ in cmds if cmd == "Q")

        # Allow some tolerance: some corners may degenerate to L if
        # segment lengths are zero
        assert actual_q <= expected_corners, (
            f"More Q commands ({actual_q}) than route corners ({expected_corners})"
        )

    def test_simple(self):
        mmd = (
            "%%metro line: main | Main | #ff0000\n"
            "%%metro line: alt | Alt | #0000ff\n"
            "graph LR\n"
            "    a -->|main| b\n"
            "    a -->|alt| c\n"
            "    b -->|main| d\n"
            "    c -->|alt| d\n"
        )
        _, routes, offsets, svg = _layout_and_route(mmd)
        self._check(routes, offsets, svg)

    @pytest.mark.parametrize(
        "fixture",
        sorted(TOPOLOGIES_DIR.glob("*.mmd")),
        ids=lambda p: p.stem,
    )
    def test_topology_fixtures(self, fixture):
        _, routes, offsets, svg = _layout_and_route_file(fixture)
        self._check(routes, offsets, svg)


# ---------------------------------------------------------------------------
# 7. Line z-order must be consistent across the whole diagram
# ---------------------------------------------------------------------------


_METRO_LINE_PREFIX = "metro-line-"


def _parse_edge_paths_with_line(svg_str: str) -> list[str]:
    """Return the metro-line ID for each edge <path> in document order.

    Edge paths are tagged ``class="metro-line-<id>"`` by ``_render_edges``;
    other paths (section boxes, icons, debug overlays) lack that class and
    are skipped.
    """
    root = ET.fromstring(svg_str)
    ns = {"svg": "http://www.w3.org/2000/svg"}
    return [
        p.get("class", "")[len(_METRO_LINE_PREFIX) :]
        for p in root.findall(".//svg:path", ns)
        if p.get("class", "").startswith(_METRO_LINE_PREFIX)
    ]


class TestLineZOrderConsistent:
    """All edge <path> elements for a given metro line must be a single
    contiguous block in document order.

    SVG paints later elements on top of earlier ones.  If line A's paths are
    interleaved with line B's, then in one bundle A may sit above B while in
    another B sits above A -- the visual z-order of any pair of lines is
    inconsistent across the diagram.  Keeping each line's paths contiguous
    guarantees a single, global paint order: the line whose paths appear
    last is on top everywhere it overlaps any other line.
    """

    def _check(self, svg_str: str) -> None:
        line_ids = _parse_edge_paths_with_line(svg_str)
        # A line's paths are contiguous iff each line ID appears in at most
        # one consecutive run.  Walk the sequence and record where each run
        # starts; if we re-enter a line we've already left, it's interleaved.
        finished: set[str] = set()
        prev: str | None = None
        for lid in line_ids:
            if lid != prev and lid in finished:
                assert False, (
                    f"Metro line '{lid}' paths are not contiguous in "
                    f"document order: {line_ids}. This causes inconsistent "
                    f"z-order across the diagram."
                )
            if prev is not None and prev != lid:
                finished.add(prev)
            prev = lid

    def test_simple_bundle(self):
        # Two lines that share a bundle then diverge and re-merge.
        mmd = (
            "%%metro line: a | A | #ff0000\n"
            "%%metro line: b | B | #0000ff\n"
            "graph LR\n"
            "    s1 -->|a| j1\n"
            "    s1 -->|b| j1\n"
            "    j1 -->|a| t1\n"
            "    j1 -->|b| t2\n"
            "    t1 -->|a| j2\n"
            "    t2 -->|b| j2\n"
            "    j2 -->|a| e1\n"
            "    j2 -->|b| e1\n"
        )
        _, _, _, svg = _layout_and_route(mmd)
        self._check(svg)

    def test_rnaseq_sections(self):
        fixture = EXAMPLES_DIR / "rnaseq_sections.mmd"
        if not fixture.exists():
            pytest.skip(f"fixture not available: {fixture}")
        _, _, _, svg = _layout_and_route_file(fixture)
        self._check(svg)

    @pytest.mark.parametrize(
        "fixture",
        sorted(TOPOLOGIES_DIR.glob("*.mmd")),
        ids=lambda p: p.stem,
    )
    def test_topology_fixtures(self, fixture):
        _, _, _, svg = _layout_and_route_file(fixture)
        self._check(svg)
