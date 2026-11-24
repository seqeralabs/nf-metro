"""A graph keeps the metrics it was laid out with, whatever theme draws it."""

from __future__ import annotations

import copy
import warnings
from pathlib import Path

import pytest
from theme_helpers import RNASEQ, ROOT, write

from nf_metro.api import prepare_graph
from nf_metro.layout.pass_metrics import LayoutMetrics
from nf_metro.render import render_svg
from nf_metro.render.svg import build_render_plan
from nf_metro.themes import THEMES, resolve_stack, resolve_theme

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

# Its label reservations are tight enough that measuring the default layout
# with a 14px-label theme's metrics trips a routing invariant.
CROSS_THEME_MAP = EXAMPLES / "genomic_pipeline.mmd"


@pytest.fixture(scope="module")
def default_layout():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return prepare_graph(CROSS_THEME_MAP.read_text(), source_dir=str(EXAMPLES))


def test_compute_layout_records_the_metrics_it_laid_out_with(default_layout) -> None:
    metrics = default_layout.layout_metrics
    assert metrics == LayoutMetrics.of(default_layout, THEMES["default"])
    assert metrics.label_font_size == THEMES["default"].label_font_size


@pytest.mark.parametrize("name", ["seqera", "transparent", "london-tube"])
def test_a_layout_renders_in_another_theme(default_layout, name: str) -> None:
    svg = render_svg(copy.deepcopy(default_layout), THEMES[name])
    assert svg.startswith("<?xml") or svg.startswith("<svg")


def _register(stack: str, monkeypatch, text: str | None = None):
    """Register the map with a server on *stack*; return the laid-out graph."""
    from nf_metro.live import server

    laid_out = []

    def compute_layout(graph):
        laid_out.append(graph)
        layout(graph)

    layout = server.compute_layout
    monkeypatch.setattr(server, "compute_layout", compute_layout)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        server.RunRegistry(resolve_stack(stack)).register(
            text or CROSS_THEME_MAP.read_text(), "run"
        )
    return laid_out[0]


@pytest.mark.parametrize("name", ["seqera", "transparent"])
def test_serve_multi_lays_out_with_its_own_stack(name: str, monkeypatch) -> None:
    graph = _register(name, monkeypatch)
    assert graph.layout_metrics.label_font_size == THEMES[name].label_font_size


def test_serve_multi_applies_its_stacks_layout_block(tmp_path, monkeypatch) -> None:
    theme = tmp_path / "wide.yaml"
    theme.write_text("name: wide\nlayout:\n  x_spacing: 91\n")
    assert _register(str(theme), monkeypatch).x_spacing == 91


def test_a_posted_map_cannot_name_a_theme_file(tmp_path, monkeypatch) -> None:
    text = f"%%metro style: {tmp_path / 'absent.yaml'}\n" + CROSS_THEME_MAP.read_text()
    graph = _register("seqera", monkeypatch, text)
    assert graph.resolved_theme is resolve_stack("seqera")


# ---------------------------------------------------------------------------
# Layout reserves the theme's sizes
# ---------------------------------------------------------------------------


def test_layout_reserves_the_theme_label_size(tmp_path):
    big = write(tmp_path, "big.yaml", "name: big\nlabel_font_size: 20\n")

    def widest_section(theme: str | None) -> float:
        graph = prepare_graph(
            RNASEQ.read_text(), theme=theme, source_dir=str(RNASEQ.parent)
        )
        return max(s.bbox_w for s in graph.sections.values())

    assert widest_section(str(big)) > widest_section(None)


def test_layout_measures_file_icons_with_the_theme(tmp_path):
    path = ROOT / "examples" / "topologies" / "render_labelwrap_row_gap.mmd"
    roomy = write(
        tmp_path, "roomy.yaml", "name: roomy\nstation_radius: 6\nlabel_font_size: 14\n"
    )

    def qc_width(theme: str | None) -> float:
        graph = prepare_graph(
            path.read_text(), theme=theme, source_dir=str(path.parent)
        )
        return graph.sections["qc_sec"].bbox_w

    assert qc_width(None) == pytest.approx(qc_width("default"))
    assert qc_width(str(roomy)) > qc_width("default")


def test_uppercase_tracked_titles_are_measured_wider():
    from nf_metro.render.section_header import (
        HeaderLettering,
        estimate_section_label_width,
        header_lettering_context,
    )

    plain = estimate_section_label_width("Variant calling", 13.0)
    with header_lettering_context(HeaderLettering(True, 2.0)):
        drawn = estimate_section_label_width("Variant calling", 13.0)
    assert drawn >= plain + 2.0 * len("Variant calling")


def test_neighbouring_london_tube_titles_do_not_overlap():
    path = EXAMPLES / "legend_logo_placement.mmd"
    graph = prepare_graph(
        path.read_text(), theme="london-tube", source_dir=str(path.parent)
    )
    plan = build_render_plan(graph, resolve_theme(graph))
    keepouts = sorted(
        (p.keepout for p in plan.header_placements.values() if not p.label_rotation),
        key=lambda k: (round(k[1]), k[0]),
    )
    for a, b in zip(keepouts, keepouts[1:]):
        if abs(a[1] - b[1]) < 1.0:
            assert a[2] <= b[0] + 1e-6


_TB_ICONS_MMD = """\
%%metro line: main | Main | #4CAF50
%%metro files: reads_in | FASTQ | Reads
%%metro files: bam_out | BAM | Alignments
graph LR
    subgraph proc [Process]
        %%metro direction: TB
        reads_in[ ]
        a[A]
        b[B]
        bam_out[ ]
        reads_in -->|main| a
        a -->|main| b
        b -->|main| bam_out
    end
"""


def _drawn_terminus_ink(graph, theme, monkeypatch):
    """Every terminus's drawn icon pages and captions as (sid, kind, box) rects."""
    import drawsvg as draw

    from nf_metro.layout.routing import compute_station_offsets
    from nf_metro.render import icons, svg

    pages: list[tuple[float, float, float, float]] = []

    def record(d, *, cx, cy, width, height, **_):
        pages.append((cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2))

    for module in (icons, svg):
        monkeypatch.setattr(module, "render_file_icon", record)
    monkeypatch.setattr(svg, "render_folder_icon", record)
    offsets = compute_station_offsets(graph)
    ink = []
    for station in graph.stations.values():
        if not station.terminus_labels:
            continue
        offs = [
            offsets.get((station.id, lid), 0.0)
            for lid in graph.station_lines(station.id)
        ]
        pages.clear()
        d = draw.Drawing(1, 1)
        svg._render_terminus_icons(
            d, station, graph, theme, theme.station_radius, min(offs), max(offs), False
        )
        ink += [(station.id, "page", box) for box in pages]
        for el in d.elements:
            if isinstance(el, draw.Text):
                x, y = float(el.args["x"]), float(el.args["y"])
                height = max(1, len(el.children)) * float(el.args["font-size"])
                ink.append((station.id, "caption", (x, y, x, y + height)))
    return ink


def _ink_margins(text: str, theme: str | None, monkeypatch) -> dict:
    """Least room between terminus ink and each edge of its section's box."""
    graph = prepare_graph(text, theme=theme, source_dir=str(EXAMPLES))
    margins: dict[tuple[str, str], float] = {}
    for sid, _, (x0, y0, x1, y1) in _drawn_terminus_ink(
        graph, resolve_theme(graph), monkeypatch
    ):
        section = graph.sections[graph.stations[sid].section_id]
        for side, room in (
            ("left", x0 - section.bbox_x),
            ("right", section.bbox_x + section.bbox_w - x1),
            ("top", y0 - section.bbox_y),
            ("bottom", section.bbox_y + section.bbox_h - y1),
        ):
            key = (section.id, side)
            margins[key] = min(margins.get(key, room), room)
    return margins


@pytest.mark.parametrize("source", ["file_icons", "tb"], ids=["lr", "tb"])
def test_file_icons_of_the_theme_size_keep_the_default_room(
    tmp_path, monkeypatch, source: str
):
    big = write(tmp_path, "big.yaml", "name: big\nicon_width: 80\nicon_height: 100\n")
    text = (
        (EXAMPLES / "file_icons.mmd").read_text()
        if source == "file_icons"
        else _TB_ICONS_MMD
    )
    default = _ink_margins(text, None, monkeypatch)
    enlarged = _ink_margins(text, str(big), monkeypatch)
    assert default.keys() == enlarged.keys()
    short = {
        key: (round(enlarged[key], 1), round(room, 1))
        for key, room in default.items()
        if enlarged[key] < room - 1e-6
    }
    assert not short, f"(section, side): (enlarged room, default room) {short}"
