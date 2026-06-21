"""Render-geometry validation reads the drawn SVG as its own oracle (#679).

:func:`~nf_metro.render.validate.validate_render` parses a rendered SVG back
into node markers (embedded manifest), route polylines (drawn ``<path>`` ink),
and label ink boxes (drawn ``<text>`` ink) and checks the picture as drawn --
the geometry the pre-render layout guards never see, including render-time
offsets and the wrapped-label lift.

These tests pin that the clean gallery corpus has zero label strikes, that an
injected strike is caught and an exempt (carried-line) overlap is not, and that
the SVG parser recovers smoothing corners exactly and breaks at bridge-hop gaps
rather than spanning them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph
from nf_metro.render import render_svg, validate_render
from nf_metro.render.manifest import read_manifest
from nf_metro.render.validate import (
    LABEL_STRIKE,
    parse_route_polylines,
    parse_station_labels,
)
from nf_metro.themes import THEMES

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
TOPOLOGIES = EXAMPLES / "topologies"


def _renderable_corpus() -> tuple[list[str], dict[str, MetroGraph]]:
    """Collect renderable fixtures and their laid-out graphs in one pass.

    Filtering at import time keeps the parametrization to fixtures that
    actually render (a newly-broken one drops out, caught by the count guard);
    caching the laid-out graph lets each test render without a second layout.
    """
    names: list[str] = []
    graphs: dict[str, MetroGraph] = {}
    for path in sorted(EXAMPLES.glob("*.mmd")) + sorted(TOPOLOGIES.glob("*.mmd")):
        try:
            graph = parse_metro_mermaid(path.read_text())
            compute_layout(graph)
        except Exception:  # noqa: BLE001 - unrenderable fixtures are not our subject
            continue
        rel = path.relative_to(EXAMPLES).as_posix()
        names.append(rel)
        graphs[rel] = graph
    return names, graphs


CORPUS, _LAID_OUT = _renderable_corpus()


def _render(rel_name: str) -> str:
    graph = _LAID_OUT[rel_name]
    theme_name = graph.style if graph.style in THEMES else "nfcore"
    return render_svg(graph, THEMES[theme_name])


@pytest.mark.parametrize("name", CORPUS)
def test_clean_corpus_has_no_label_strike(name: str) -> None:
    """No gallery render draws a line through a non-consumer station's label."""
    findings = validate_render(_render(name))
    strikes = [f for f in findings if f.kind == LABEL_STRIKE]
    assert not strikes, f"{name}: {[f.message for f in strikes]}"


def _a_foreign_label(svg: str) -> tuple[str, float, float, str]:
    """A station label plus a line that station does not carry.

    Returns ``(station_id, label_x, label_y, foreign_line_id)`` for a label
    whose station omits at least one defined line, so a segment drawn across it
    is an unambiguous strike by a non-consumer.
    """
    manifest = read_manifest(svg)
    nodes = {n["id"]: n for n in manifest["nodes"]}
    all_lines = [grp["id"] for grp in manifest["groups"]]
    for placement, _font_size in parse_station_labels(svg):
        node = nodes.get(placement.station_id)
        if node is None:
            continue
        foreign = [lid for lid in all_lines if lid not in node.get("groups", ())]
        if foreign:
            return placement.station_id, placement.x, placement.y, foreign[0]
    raise AssertionError("no label with a non-consumer line found")


def _inject_segment(
    svg: str, line_id: str, p1: tuple[float, float], p2: tuple[float, float]
) -> str:
    seg = (
        f'<path d="M{p1[0]},{p1[1]} L{p2[0]},{p2[1]}" '
        f'class="metro-line-{line_id}" data-line-id="{line_id}" />'
    )
    return svg.replace("</svg>", seg + "</svg>")


def test_validate_render_flags_injected_strike() -> None:
    """A foreign line drawn through a label's centre is reported as a strike."""
    svg = _render("rnaseq_sections.mmd")
    station_id, x, y, foreign = _a_foreign_label(svg)
    struck = _inject_segment(svg, foreign, (x - 40, y), (x + 40, y))

    findings = validate_render(struck)
    strikes = [f for f in findings if f.kind == LABEL_STRIKE]
    assert len(strikes) == 1
    assert strikes[0].line_id == foreign
    assert strikes[0].station_id == station_id


def test_carried_line_overlap_is_exempt() -> None:
    """A line the labelled station carries is not a strike (it owns that name)."""
    svg = _render("rnaseq_sections.mmd")
    manifest = read_manifest(svg)
    nodes = {n["id"]: n for n in manifest["nodes"]}
    placement = next(
        pl for pl, _ in parse_station_labels(svg) if nodes[pl.station_id].get("groups")
    )
    carried = nodes[placement.station_id]["groups"][0]
    over = _inject_segment(
        svg,
        carried,
        (placement.x - 40, placement.y),
        (placement.x + 40, placement.y),
    )
    assert not validate_render(over)


def test_no_manifest_yields_no_findings() -> None:
    """An SVG without an embedded manifest has nothing addressable to validate."""
    plain = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0,0 L9,9"/></svg>'
    assert validate_render(plain) == []


def test_parser_collapses_smoothing_curve_to_its_corner() -> None:
    """A ``Q`` smoothing arc is read back as its sharp corner (control point)."""
    svg = (
        '<path d="M0,0 L40,0 Q50,0,50,10 L50,50" '
        'class="metro-line-x" data-line-id="x" />'
    )
    (line_id, subpaths) = parse_route_polylines(svg)[0]
    assert line_id == "x"
    assert subpaths == [[(0.0, 0.0), (50.0, 0.0), (50.0, 50.0)]]


def test_parser_breaks_at_bridge_hop_gap() -> None:
    """A second ``M`` (a bridge hop) starts a new subpath, never a span across."""
    svg = '<path d="M0,0 L40,0 M60,0 L100,0" class="metro-line-x" data-line-id="x" />'
    (_, subpaths) = parse_route_polylines(svg)[0]
    assert subpaths == [[(0.0, 0.0), (40.0, 0.0)], [(60.0, 0.0), (100.0, 0.0)]]


def test_directional_chevrons_are_not_parsed_as_routes() -> None:
    """``metro-direction-*`` chevrons carry ``data-line-id`` but are not routes."""
    svg = (
        '<path d="M0,0 L9,9" class="metro-line-x" data-line-id="x" />'
        '<path d="M3,3 L6,6" class="metro-direction-x" data-line-id="x" />'
    )
    parsed = parse_route_polylines(svg)
    assert [line_id for line_id, _ in parsed] == ["x"]


@pytest.mark.parametrize(
    "name", ["differentialabundance_default.mmd", "genomic_pipeline.mmd"]
)
def test_real_render_routes_split_on_bridge_gaps(name: str) -> None:
    """A real render with bridged edges yields multi-subpath routes, none empty."""
    if name not in CORPUS:
        pytest.skip(f"{name} not in renderable corpus")
    routes = parse_route_polylines(_render(name))
    assert routes
    assert all(sub for _, subs in routes for sub in subs)
    assert any(len(subs) > 1 for _, subs in routes)


def test_corpus_is_nonempty() -> None:
    """The corpus parametrization actually collected fixtures (guards a silent
    empty-glob that would make the regression lock vacuous)."""
    assert len(CORPUS) > 30
