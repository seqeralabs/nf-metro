"""The title block starts at the leftmost ink the map paints.

Everything the render draws except the title and the attribution watermark
counts as ink: section boxes, line strokes, station markers, labels, section
headers, group captions and the legend.  The title sits at that edge; a
standalone logo does, with the title beside it.  The edge is measured here from
the emitted SVG alone, independently of the render's own geometry.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal

import pytest

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
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
SVG_NS = "{http://www.w3.org/2000/svg}"

INK_TOLERANCE = 1.0

# The first three have a box-aligned left edge.  The "around" fixtures route a
# line out past their leftmost box, riboseq's direction chevrons reach past
# the line they ride, header_side_rotated's leftmost ink is a section badge,
# and the last two have no sections at all.
FIXTURES = (
    "examples/rnaseq_sections.mmd",
    "examples/differentialabundance_default.mmd",
    "examples/sarek_metro.mmd",
    "examples/topologies/around_section_below.mmd",
    "examples/topologies/around_below_ep_col_gt0.mmd",
    "examples/topologies/merge_around_below_leftmost.mmd",
    "examples/riboseq_metro.mmd",
    "examples/topologies/header_side_rotated.mmd",
    "examples/simple_pipeline.mmd",
    "examples/guide/01_minimal.mmd",
)

# (logo, legend, output format); the logo is standalone when the legend is
# absent or the output is HTML.
MODES = {
    "title-only": (None, None, "svg"),
    "logo-in-legend": (PLACEHOLDER_LOGO, "bl", "svg"),
    "standalone-logo-svg": (PLACEHOLDER_LOGO, "none", "svg"),
    "standalone-logo-html": (PLACEHOLDER_LOGO, None, "html"),
}

SHORT_MODES = ("title-only", "standalone-logo-svg")

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:e-?\d+)?")
_ROTATE = re.compile(r"rotate\(\s*(-?[\d.]+)[ ,]+(-?[\d.]+)[ ,]+(-?[\d.]+)\s*\)")
_NON_INK_TAGS = {"defs", "mask", "style", "metadata", "title", "clipPath"}


def _local(el: ET.Element) -> str:
    return el.tag.split("}")[-1]


def _half_stroke(el: ET.Element) -> float:
    stroke = el.get("stroke")
    if stroke is None or stroke == "none":
        return 0.0
    return float(el.get("stroke-width", "1")) / 2


def _path_xs(d: str) -> list[float]:
    """X coordinates of an absolute ``M``/``L``/``Q``/``Z`` path."""
    xs: list[float] = []
    for command in re.findall(r"[MLQZ][^MLQZ]*", d):
        numbers = [float(n) for n in _NUMBER.findall(command[1:])]
        xs.extend(numbers[0::2])
    return xs


def _text_left(el: ET.Element) -> float:
    """Left x of a text element's glyph advance, rotation included."""
    size = float(el.get("font-size", "12"))
    style = text_style(size, el.get("font-weight", "normal"))
    tspans = list(el)
    lines = [(t.text or "", float(t.get("x", el.get("x", "0")))) for t in tspans]
    if not lines:
        lines = [(el.text or "", float(el.get("x", "0")))]
    anchor = el.get("text-anchor", "start")
    dy = el.get("dy", "0")
    y = float(el.get("y", "0")) + (
        float(dy.removesuffix("em")) * size if dy.endswith("em") else float(dy)
    )
    left = math.inf
    rotation = _ROTATE.fullmatch(el.get("transform", ""))
    for text, x in lines:
        if not text:
            continue
        with metrics_face_context(MetricsFace.FALLBACK):
            advance = DEFAULT_TEXT_METRICS.advance(text, style, TextRole.STATION_LABEL)
        x0 = {"end": x - advance, "middle": x - advance / 2}.get(anchor, x)
        corners = [(x0, y - size), (x0 + advance, y - size), (x0, y), (x0 + advance, y)]
        if rotation:
            degrees, ax, ay = map(float, rotation.groups())
            angle = math.radians(degrees)
            corners = [
                (
                    ax + (cx - ax) * math.cos(angle) - (cy - ay) * math.sin(angle),
                    ay + (cx - ax) * math.sin(angle) + (cy - ay) * math.cos(angle),
                )
                for cx, cy in corners
            ]
        left = min(left, *(cx for cx, _ in corners)) - _half_stroke(el)
    return left


def leftmost_ink(root: ET.Element, *, skip_images: bool) -> float:
    """Smallest x any painted element reaches, excluding the title, the
    watermark and (when *skip_images*) the standalone logo."""
    reach: list[float] = []

    def visit(el: ET.Element) -> None:
        tag = _local(el)
        if tag in _NON_INK_TAGS:
            return
        classes = el.get("class", "").split()
        if tag == "rect" and "nf-metro-bg" not in classes:
            reach.append(float(el.get("x", "0")) - _half_stroke(el))
        elif tag == "circle":
            reach.append(
                float(el.get("cx", "0")) - float(el.get("r", "0")) - _half_stroke(el)
            )
        elif tag == "path":
            reach.extend(x - _half_stroke(el) for x in _path_xs(el.get("d", "")))
        elif tag == "image" and not skip_images:
            reach.append(float(el.get("x", "0")))
        elif tag == "text":
            text = "".join(el.itertext())
            if (
                "nf-metro-title" not in classes
                and not text.startswith("created with nf-metro")
                and text.strip()
            ):
                reach.append(_text_left(el))
            return
        for child in el:
            visit(child)

    visit(root)
    return min(reach)


def _render(
    name: str, mode: str
) -> tuple[RenderPlan, ET.Element, Literal["svg", "html"]]:
    logo, legend, output_format = MODES[mode]
    path = ROOT / name
    graph = prepare_graph(
        path.read_text(),
        title="Map title",
        logo=logo,
        legend=legend,
        source_dir=str(path.parent),
        output_format=output_format,  # type: ignore[arg-type]
    )
    result = render_graph_result(
        graph,
        resolve_theme(None, graph),
        RenderConfig(output_format=output_format),  # type: ignore[arg-type]
    )
    content = result.content
    if output_format == "html":
        match = re.search(r"<svg\b.*?</svg>", content, re.DOTALL)
        assert match, "HTML output embeds no SVG"
        content = match.group(0)
    return result.plan, ET.fromstring(content), output_format  # type: ignore[return-value]


def _title(root: ET.Element) -> ET.Element:
    titles = [
        el
        for el in root.iter(f"{SVG_NS}text")
        if "nf-metro-title" in el.get("class", "").split()
    ]
    assert len(titles) == 1
    return titles[0]


ALL_MODES_FIXTURES = (
    "examples/rnaseq_sections.mmd",
    "examples/riboseq_metro.mmd",
    "examples/topologies/header_side_rotated.mmd",
)
CASES = [
    (name, mode)
    for name in FIXTURES
    for mode in (MODES if name in ALL_MODES_FIXTURES else SHORT_MODES)
]


@pytest.mark.parametrize(("name", "mode"), CASES)
def test_title_block_starts_at_the_leftmost_ink(name: str, mode: str) -> None:
    """The title, or a standalone logo with the title beside it, starts at the
    leftmost painted x; the title never overlaps the logo."""
    plan, root, _ = _render(name, mode)
    label = f"{name} [{mode}]"
    standalone = plan.show_logo and not plan.logo_in_legend
    ink = leftmost_ink(root, skip_images=standalone)
    title_x = float(_title(root).attrib["x"])
    if standalone:
        assert abs(plan.logo_x - ink) <= INK_TOLERANCE, (
            f"{label}: logo x={plan.logo_x:.1f}, leftmost ink {ink:.1f}"
        )
        assert title_x >= plan.logo_x + plan.logo_w, f"{label}: title overlaps logo"
    else:
        assert abs(title_x - ink) <= INK_TOLERANCE, (
            f"{label}: title x={title_x:.1f}, leftmost ink {ink:.1f}"
        )
