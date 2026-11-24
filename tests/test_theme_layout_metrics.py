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

# Its label reservations are tight enough that measuring the nfcore layout
# with a 14px-label theme's metrics trips a routing invariant.
CROSS_THEME_MAP = EXAMPLES / "genomic_pipeline.mmd"


@pytest.fixture(scope="module")
def nfcore_layout():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return prepare_graph(CROSS_THEME_MAP.read_text(), source_dir=str(EXAMPLES))


def test_compute_layout_records_the_metrics_it_laid_out_with(nfcore_layout) -> None:
    metrics = nfcore_layout.layout_metrics
    assert metrics == LayoutMetrics.of(nfcore_layout, THEMES["nfcore"])
    assert metrics.label_font_size == THEMES["nfcore"].label_font_size


@pytest.mark.parametrize("name", ["seqera", "transparent", "london-tube"])
def test_a_layout_renders_in_another_theme(nfcore_layout, name: str) -> None:
    svg = render_svg(copy.deepcopy(nfcore_layout), THEMES[name])
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
    text = CROSS_THEME_MAP.read_text().replace(
        "%%metro style: nfcore", f"%%metro style: {tmp_path / 'absent.yaml'}"
    )
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

    assert qc_width(None) == pytest.approx(qc_width("nfcore"))
    assert qc_width(str(roomy)) > qc_width("nfcore")


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


def test_file_icons_of_the_theme_size_stay_inside_their_sections(tmp_path):
    from nf_metro.layout.routing import compute_station_offsets
    from nf_metro.render.svg import _terminus_icon_centers_for

    big = write(tmp_path, "big.yaml", "name: big\nicon_width: 80\nicon_height: 100\n")
    path = EXAMPLES / "file_icons.mmd"
    graph = prepare_graph(path.read_text(), theme=str(big), source_dir=str(EXAMPLES))
    theme = resolve_theme(graph)
    offsets = compute_station_offsets(graph)
    termini = [s for s in graph.stations.values() if s.terminus_labels]
    assert termini
    for station in termini:
        section = graph.sections[station.section_id]
        offs = [
            offsets.get((station.id, lid), 0.0)
            for lid in graph.station_lines(station.id)
        ]
        for cx, cy in _terminus_icon_centers_for(
            station, graph, theme, min(offs), max(offs)
        ):
            assert section.bbox_x <= cx - theme.icon_width / 2
            assert cx + theme.icon_width / 2 <= section.bbox_x + section.bbox_w
            assert section.bbox_y <= cy - theme.icon_height / 2
            assert cy + theme.icon_height / 2 <= section.bbox_y + section.bbox_h
