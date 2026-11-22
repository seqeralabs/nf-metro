"""The map title renders alongside a configured logo, and the canvas-top band
the logo and title occupy is reserved after layout.

A logo folded into the legend leaves the title in its usual top-left spot.  A
standalone logo (``%%metro legend: none``, or any HTML output, whose embedded
SVG drops the legend) is drawn at the canvas top-left with the title to its
right, vertically centred on it; every drawn section's header badge must clear
the logo's bottom edge.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal

import pytest
from layout_validator import min_drawn_section_bbox_top

from nf_metro.api import (
    RenderConfig,
    _prepare_graph_state,
    prepare_graph,
    render_graph_result,
)
from nf_metro.layout import compute_layout
from nf_metro.layout.constants import SECTION_HEADER_PROTRUSION, TITLE_BAND_BOTTOM
from nf_metro.parser.model import MetroGraph
from nf_metro.render.constants import TITLE_CANVAS_SAFETY_MARGIN
from nf_metro.render.plan import RenderPlan
from nf_metro.text_metrics import (
    DEFAULT_TEXT_METRICS,
    MetricsFace,
    TextRole,
    metrics_face_context,
    text_style,
)
from nf_metro.themes import resolve_theme

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
PLACEHOLDER_LOGO = str(EXAMPLES / "placeholder_logo.png")

STANDALONE_LOGO_TOP = 5.0
LOGO_HEIGHT = 80.0
HEADER_GAP = 8.0
SVG_NS = "{http://www.w3.org/2000/svg}"

FLAT_TITLED_MMD = (
    "%%metro title: Testy McTestface\n"
    "%%metro line: main | Main | #d0006f\n"
    "graph LR\n"
    "    a[a]\n"
    "    b[b]\n"
    "    a -->|main| b\n"
)

# Sectioned maps, including ones whose topmost header sits high enough to
# overlap a canvas-top band when nothing lifts it.
SECTIONED_FIXTURES = (
    "examples/rnaseq_sections.mmd",
    "examples/sarek_metro.mmd",
    "examples/variantbenchmarking.mmd",
    "examples/topologies/rowmate_tb_side_entry_top_align.mmd",
)


def _render(
    text: str,
    *,
    source_dir: str,
    output_format: Literal["svg", "html"] = "svg",
    title: str | None = None,
    logo: str | None = None,
    legend: str | None = None,
    logo_scale: float | None = None,
) -> tuple[RenderPlan, ET.Element]:
    layout_options = {"logo_scale": logo_scale} if logo_scale is not None else None
    graph = prepare_graph(
        text,
        title=title,
        logo=logo,
        legend=legend,
        layout_options=layout_options,
        source_dir=source_dir,
        output_format=output_format,
    )
    result = render_graph_result(
        graph,
        resolve_theme(None, graph),
        RenderConfig(output_format=output_format),
    )
    svg = result.content
    if output_format == "html":
        match = re.search(r"<svg\b.*?</svg>", svg, re.DOTALL)
        assert match, "HTML output embeds no SVG"
        svg = match.group(0)
    return result.plan, ET.fromstring(svg)


def _title_elements(root: ET.Element) -> list[ET.Element]:
    return [
        el
        for el in root.iter(f"{SVG_NS}text")
        if "nf-metro-title" in el.get("class", "").split()
    ]


def _title_advance(plan: RenderPlan) -> float:
    with metrics_face_context(MetricsFace.FALLBACK):
        return DEFAULT_TEXT_METRICS.advance(
            plan.graph.title,
            text_style(plan.theme.title_font_size, "bold"),
            TextRole.TITLE,
        )


def _fixture(name: str) -> tuple[str, str]:
    path = ROOT / name
    return path.read_text(), str(path.parent)


def _assert_band_clear(plan: RenderPlan, label: str) -> None:
    top = min_drawn_section_bbox_top(plan.graph)  # type: ignore[arg-type]
    assert top is not None, f"{label}: no drawn section"
    assert plan.logo_y == pytest.approx(STANDALONE_LOGO_TOP), label
    assert plan.logo_h == pytest.approx(LOGO_HEIGHT * plan.graph.logo_scale), label
    logo_bottom = plan.logo_y + plan.logo_h
    header_top = top - SECTION_HEADER_PROTRUSION
    assert header_top >= logo_bottom + HEADER_GAP - 1e-6, (
        f"{label}: topmost header top y={header_top:.1f} does not clear the "
        f"standalone logo bottom {logo_bottom:.1f} by {HEADER_GAP}px"
    )


def test_title_drawn_when_logo_folds_into_legend() -> None:
    """A logo in the default (bottom) legend leaves the title drawn."""
    plan, root = _render(
        FLAT_TITLED_MMD, source_dir=str(EXAMPLES), logo=PLACEHOLDER_LOGO
    )
    assert plan.logo_in_legend
    titles = _title_elements(root)
    assert [t.text for t in titles] == ["Testy McTestface"]
    assert float(titles[0].get("x", "nan")) == pytest.approx(plan.padding)


@pytest.mark.parametrize("name", SECTIONED_FIXTURES)
def test_title_band_reserved_when_logo_folds_into_legend(name: str) -> None:
    """With the logo in a legend the title sits top-left, and the topmost
    header clears it exactly as on a titled map with no logo at all."""
    text, source_dir = _fixture(name)
    plan, root = _render(
        text, source_dir=source_dir, logo=PLACEHOLDER_LOGO, legend="bl"
    )
    assert plan.logo_in_legend
    assert len(_title_elements(root)) == 1
    top = min_drawn_section_bbox_top(plan.graph)  # type: ignore[arg-type]
    assert top is not None
    assert top - SECTION_HEADER_PROTRUSION >= TITLE_BAND_BOTTOM - 1e-6


@pytest.mark.parametrize("output_format", ["svg", "html"])
@pytest.mark.parametrize("scale", [1.0, 1.5])
@pytest.mark.parametrize("name", SECTIONED_FIXTURES)
def test_title_beside_standalone_logo_and_band_reserved(
    name: str, scale: float, output_format: Literal["svg", "html"]
) -> None:
    """A standalone logo puts the title to its right, vertically centred
    on it, inside the canvas; the topmost header clears the logo."""
    text, source_dir = _fixture(name)
    plan, root = _render(
        text,
        source_dir=source_dir,
        output_format=output_format,
        logo=PLACEHOLDER_LOGO,
        legend="none" if output_format == "svg" else None,
        logo_scale=scale,
    )
    label = f"{name} [{output_format}, logo_scale={scale}]"
    assert plan.show_logo and not plan.logo_in_legend, label
    assert plan.graph.logo_scale == scale

    titles = _title_elements(root)
    assert len(titles) == 1, f"{label}: expected one title, got {len(titles)}"
    title_x = float(titles[0].get("x", "nan"))
    title_y = float(titles[0].get("y", "nan"))
    assert title_x >= plan.logo_x + plan.logo_w, label
    assert plan.logo_y < title_y < plan.logo_y + plan.logo_h, label
    assert plan.svg_width >= title_x + _title_advance(plan) + (
        TITLE_CANVAS_SAFETY_MARGIN - 1
    ), label

    _assert_band_clear(plan, label)


@pytest.mark.parametrize("output_format", ["svg", "html"])
@pytest.mark.parametrize("name", SECTIONED_FIXTURES)
def test_standalone_logo_band_reserved_without_title(
    name: str, output_format: Literal["svg", "html"]
) -> None:
    """The logo band is reserved on an untitled map too."""
    text, source_dir = _fixture(name)
    plan, root = _render(
        text,
        source_dir=source_dir,
        output_format=output_format,
        title="",
        logo=PLACEHOLDER_LOGO,
        legend="none" if output_format == "svg" else None,
    )
    label = f"{name} [{output_format}, untitled]"
    assert plan.show_logo and not plan.logo_in_legend, label
    assert _title_elements(root) == [], label
    _assert_band_clear(plan, label)


@pytest.mark.parametrize("output_format", ["svg", "html"])
@pytest.mark.parametrize("scale", [1.0, 1.5])
@pytest.mark.parametrize("title", [None, ""])
def test_flat_map_content_clears_standalone_logo(
    title: str | None, scale: float, output_format: Literal["svg", "html"]
) -> None:
    """A map with no subgraphs keeps its stations and labels, which sit inside
    the implicit holder's box, below a standalone logo."""
    plan, root = _render(
        FLAT_TITLED_MMD,
        source_dir=str(EXAMPLES),
        output_format=output_format,
        title=title,
        logo=PLACEHOLDER_LOGO,
        legend="none" if output_format == "svg" else None,
        logo_scale=scale,
    )
    label = f"flat [{output_format}, title={title!r}, logo_scale={scale}]"
    assert plan.show_logo and not plan.logo_in_legend, label
    assert len(_title_elements(root)) == (0 if title == "" else 1), label
    holder_top = min(s.bbox_y for s in plan.graph.sections.values())
    logo_bottom = plan.logo_y + plan.logo_h
    assert holder_top >= logo_bottom - 1e-6, (
        f"{label}: content box top y={holder_top:.1f} rises above the logo "
        f"bottom {logo_bottom:.1f}"
    )


@pytest.mark.parametrize("scale", [1.0, 1.2])
@pytest.mark.parametrize(
    "name",
    [
        "examples/topologies/tb_right_entry_stack.mmd",
        "examples/topologies/tb_bottom_exit_fork_diamond.mmd",
        "examples/topologies/left_exit_wrap_under_own_section.mmd",
    ],
)
def test_over_the_top_loop_runs_below_standalone_logo(name: str, scale: float) -> None:
    """A topmost-row loop over a RIGHT-entry section's top runs below the
    logo's band, so it cannot cut through the logo or the title beside it."""
    text, source_dir = _fixture(name)
    plan, _ = _render(
        text,
        source_dir=source_dir,
        title="Map title here",
        logo=PLACEHOLDER_LOGO,
        legend="none",
        logo_scale=scale,
    )
    top_ink = min(y for line in plan.route_polylines for _x, y in line)
    logo_bottom = plan.logo_y + plan.logo_h
    assert top_ink >= logo_bottom, (
        f"{name} [logo_scale={scale}]: route climbs to y={top_ink:.1f}, above "
        f"the standalone logo bottom {logo_bottom:.1f}"
    )


# A RIGHT-entry section in the topmost row fed from the row below: the feed
# runs its traverse above the topmost row, in the canvas-top padding.
GAP_ABOVE_TOPMOST_ROW_MMD = """\
%%metro line: l1 | Left exit feeder | #3cb44b
%%metro line: l2 | Middle | #4363d8
%%metro grid: lower | 0,2
%%metro grid: middle | 1,1
%%metro grid: sink | 2,1

graph LR
    subgraph lower [Lower]
        %%metro exit: left | l1
        w0[qc]
        w1[classify]
        w2[gather]
        w0 -->|l1| w1
        w1 -->|l1| w2
    end
    subgraph middle [Middle]
        m0[index]
        m1[count]
        m0 -->|l2| m1
    end
    subgraph sink [Sink]
        %%metro direction: RL
        %%metro entry: right | l1
        k0[call]
        k1[report]
        k0 -->|l1| k1
    end
    w2 -->|l1| k0
"""


@pytest.mark.parametrize("output_format", ["svg", "html"])
@pytest.mark.parametrize("scale", [1.0, 1.5, 2.5])
@pytest.mark.parametrize(
    "title", [None, "Map title", "A much longer map title running beside the logo"]
)
def test_gap_above_topmost_row_runs_below_standalone_logo(
    title: str | None, scale: float, output_format: Literal["svg", "html"]
) -> None:
    """A feed whose traverse runs above the topmost row, in the canvas-top
    padding, keeps a visible gap below the logo's band."""
    plan, _ = _render(
        GAP_ABOVE_TOPMOST_ROW_MMD,
        source_dir=str(EXAMPLES),
        output_format=output_format,
        title=title,
        logo=PLACEHOLDER_LOGO,
        legend="none",
        logo_scale=scale,
    )
    label = f"[{output_format}, logo_scale={scale}, title={title!r}]"
    _assert_band_clear(plan, label)
    top_ink = min(y for line in plan.route_polylines for _x, y in line)
    logo_bottom = plan.logo_y + plan.logo_h
    assert top_ink >= logo_bottom + HEADER_GAP - 1e-6, (
        f"{label}: route climbs to y={top_ink:.1f}, within {HEADER_GAP}px of "
        f"the standalone logo bottom {logo_bottom:.1f}"
    )


def _validated_layout(text: str, source_dir: str, **options: object) -> MetroGraph:
    graph = _prepare_graph_state(text, source_dir=source_dir, **options)  # type: ignore[arg-type]
    compute_layout(graph, validate=True)
    return graph


def _placed_geometry(
    graph: MetroGraph, output_format: Literal["svg", "html"]
) -> dict[str, tuple[float, ...]]:
    """Every laid-out coordinate, plus the render's routed polylines, keyed so
    that each tuple holds y values at its odd indices only."""
    geometry: dict[str, tuple[float, ...]] = {}
    for sid, station in graph.stations.items():
        geometry[f"station:{sid}"] = (station.x, station.y)
    for pid, port in graph.ports.items():
        geometry[f"port:{pid}"] = (port.x, port.y)
    for sid, section in graph.sections.items():
        geometry[f"section:{sid}"] = (
            section.bbox_x,
            section.bbox_y,
            section.bbox_x + section.bbox_w,
            section.bbox_y + section.bbox_h,
        )
    plan = render_graph_result(
        graph, resolve_theme(None, graph), RenderConfig(output_format=output_format)
    ).plan
    for index, polyline in enumerate(plan.route_polylines):
        geometry[f"route:{index}"] = tuple(v for point in polyline for v in point)
    return geometry


def _translation_mismatches(
    bare: dict[str, tuple[float, ...]], banded: dict[str, tuple[float, ...]]
) -> tuple[float, dict[str, tuple[tuple[float, ...], tuple[float, ...]]]]:
    """``(dy, mismatches)``: the offset the first station moved by, and every
    element of *banded* that is not *bare* shifted down by it."""
    assert banded.keys() == bare.keys()
    anchor = next(iter(bare))
    dy = banded[anchor][1] - bare[anchor][1]
    shifted = {
        key: tuple(v + dy if i % 2 else v for i, v in enumerate(values))
        for key, values in bare.items()
    }
    return dy, {
        key: (shifted[key], banded[key])
        for key in bare
        if any(abs(a - b) > 0.01 for a, b in zip(shifted[key], banded[key]))
    }


@pytest.mark.parametrize("output_format", ["svg", "html"])
@pytest.mark.parametrize(
    "name",
    [
        "examples/riboseq_metro.mmd",
        "examples/sarek_metro.mmd",
        "examples/topologies/packed_cell_right_exit_left_entry_wrap.mmd",
        "examples/topologies/left_exit_wrap_under_own_section.mmd",
        "examples/topologies/bt_to_lr.mmd",
        "examples/topologies/samerow_bypass_over_top_flat_bundle.mmd",
        "examples/topologies/packed_cell_cellmate_bypass_entry_y.mmd",
        "examples/topologies/tb_right_entry_stack.mmd",
        "gap_above_topmost_row",
    ],
)
def test_title_band_only_translates_the_bare_layout(
    name: str, output_format: Literal["svg", "html"]
) -> None:
    """Reserving the title/logo band moves the whole map down by one offset:
    every station, port, section box and routed line sits exactly where the
    ``--bare`` layout (which reserves nothing) puts it, shifted by a single
    ``dy``."""
    text, source_dir = (
        (GAP_ABOVE_TOPMOST_ROW_MMD, str(EXAMPLES))
        if name == "gap_above_topmost_row"
        else _fixture(name)
    )
    bare = _placed_geometry(_validated_layout(text, source_dir, bare=True), "svg")
    banded = _placed_geometry(
        _validated_layout(
            text,
            source_dir,
            title="Map title",
            logo=PLACEHOLDER_LOGO,
            legend="none" if output_format == "svg" else None,
            layout_options={"logo_scale": 1.0},
            output_format=output_format,
        ),
        output_format,
    )
    dy, moved = _translation_mismatches(bare, banded)
    assert dy >= 0.0
    assert not moved, (
        f"{name} [{output_format}]: {len(moved)} elements are not the bare "
        f"layout shifted by dy={dy:.2f}; e.g. {sorted(moved.items())[:3]}"
    )


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0])
def test_rail_mode_lifts_clear_of_standalone_logo(scale: float) -> None:
    """A rail-mode map is moved below a standalone logo as one rigid offset of
    its ``--bare`` layout, routes included."""
    text, source_dir = _fixture("examples/rail_mode.mmd")
    bare = _placed_geometry(_validated_layout(text, source_dir, bare=True), "svg")
    graph = _validated_layout(
        text,
        source_dir,
        logo=PLACEHOLDER_LOGO,
        legend="none",
        layout_options={"logo_scale": scale},
    )
    banded = _placed_geometry(graph, "svg")
    dy, moved = _translation_mismatches(bare, banded)
    assert not moved, (
        f"rail_mode [logo_scale={scale}]: {len(moved)} elements are not the "
        f"bare layout shifted by dy={dy:.2f}; e.g. {sorted(moved.items())[:3]}"
    )
    plan, _ = _render(
        text,
        source_dir=source_dir,
        logo=PLACEHOLDER_LOGO,
        legend="none",
        logo_scale=scale,
    )
    _assert_band_clear(plan, f"rail_mode [logo_scale={scale}]")
    top_ink = min(y for line in plan.route_polylines for _x, y in line)
    assert top_ink >= plan.logo_y + plan.logo_h, (
        f"rail_mode [logo_scale={scale}]: route climbs to y={top_ink:.1f}, "
        f"into the standalone logo band"
    )
