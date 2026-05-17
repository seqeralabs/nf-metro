"""Animation motion path invariants.

Every line segment in a motion path must lie along a rendered
metro-line segment for the same line.  An animation polyline that
strays off the visible track produces a ball that flies off-piste.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.animate import _build_line_motion_paths
from nf_metro.themes import THEMES

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
TOPOLOGIES_DIR = Path(__file__).parent / "fixtures" / "topologies"

# Fixtures that exercise the merge-junction routing introduced in #207
# along with a couple of standard examples as regression guards.
ANIMATION_FIXTURES = [
    EXAMPLES_DIR / "genomeassembly.mmd",
    EXAMPLES_DIR / "rnaseq_sections.mmd",
    EXAMPLES_DIR / "epitopeprediction.mmd",
    EXAMPLES_DIR / "hlatyping.mmd",
    TOPOLOGIES_DIR / "fan_in_merge.mmd",
    TOPOLOGIES_DIR / "wide_fan_in.mmd",
    TOPOLOGIES_DIR / "section_diamond.mmd",
]
ANIMATION_FIXTURE_IDS = [p.stem for p in ANIMATION_FIXTURES]


def _build(fixture: Path):
    """Parse, layout, route, and build animation motion paths for a fixture."""
    graph = parse_metro_mermaid(fixture.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    theme = THEMES["nfcore"]
    motion_paths = _build_line_motion_paths(graph, routes, offsets, theme)
    return graph, routes, offsets, motion_paths


_NUMBER = r"-?\d+(?:\.\d+)?"
_TOKEN_RE = re.compile(rf"[MLQ]|{_NUMBER}")


def _parse_motion_path(d_attr: str) -> list[tuple[str, list[tuple[float, float]]]]:
    """Parse an SVG motion path 'd' attribute into segments.

    Returns a list of (command, points) where command is one of 'M', 'L',
    'Q' and points is the segment's coordinate tuples.  The 'Q' segment
    keeps the (control, end) pair.
    """
    tokens = _TOKEN_RE.findall(d_attr)
    out: list[tuple[str, list[tuple[float, float]]]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("M", "L"):
            x = float(tokens[i + 1])
            y = float(tokens[i + 2])
            out.append((tok, [(x, y)]))
            i += 3
        elif tok == "Q":
            cx = float(tokens[i + 1])
            cy = float(tokens[i + 2])
            ex = float(tokens[i + 3])
            ey = float(tokens[i + 4])
            out.append(("Q", [(cx, cy), (ex, ey)]))
            i += 5
        else:
            i += 1
    return out


def _end_point(segment: tuple[str, list[tuple[float, float]]]) -> tuple[float, float]:
    """Return the on-curve endpoint of a parsed segment."""
    cmd, pts = segment
    if cmd == "Q":
        return pts[1]
    return pts[0]


@pytest.mark.parametrize("fixture", ANIMATION_FIXTURES, ids=ANIMATION_FIXTURE_IDS)
def test_motion_path_segments_lie_on_rendered_geometry(fixture: Path):
    """Every L/Q segment of every motion path must coincide with a
    rendered metro-line segment of the same line."""
    from nf_metro.layout.routing.common import point_on_polyline
    from nf_metro.render.svg import apply_route_offsets

    graph, routes, offsets, motion_paths = _build(fixture)

    polylines_by_line: dict[str, list[list[tuple[float, float]]]] = {}
    for r in routes:
        polylines_by_line.setdefault(r.line_id, []).append(
            apply_route_offsets(r, offsets)
        )

    tol = 1.5  # absorb curve-approximation rounding

    def _segment_covered(
        a: tuple[float, float],
        b: tuple[float, float],
        line_id: str,
    ) -> bool:
        for pts in polylines_by_line.get(line_id, []):
            a_loc = point_on_polyline(a, pts, tol)
            if a_loc is None:
                continue
            b_loc = point_on_polyline(b, pts, tol)
            if b_loc is None:
                continue
            # Same segment or adjacent segments along the polyline are fine;
            # the test is whether both points sit on the same polyline.
            return True
        return False

    offences: list[str] = []
    for line_id, d_attr in motion_paths:
        parsed = _parse_motion_path(d_attr)
        if not parsed:
            continue
        prev_end = _end_point(parsed[0])
        for seg in parsed[1:]:
            cmd, pts = seg
            end = _end_point(seg)
            if cmd == "L":
                if not _segment_covered(prev_end, end, line_id):
                    offences.append(f"L {prev_end} -> {end} on line {line_id!r}")
            elif cmd == "Q":
                # Curve smoothing at a station corner: accept if either the
                # control leg or the chord lies on the rendered polyline.
                control = pts[0]
                if not (
                    _segment_covered(prev_end, control, line_id)
                    or _segment_covered(prev_end, end, line_id)
                ):
                    offences.append(
                        f"Q {prev_end} -> {control} -> {end} on line {line_id!r}"
                    )
            prev_end = end

    assert not offences, (
        f"{fixture.name}: motion path contains off-piste segments not on "
        f"any rendered metro-line edge:\n  " + "\n  ".join(offences[:5])
    )
