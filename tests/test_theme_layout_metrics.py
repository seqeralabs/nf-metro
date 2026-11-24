"""A graph keeps the metrics it was laid out with, whatever theme draws it."""

from __future__ import annotations

import copy
import warnings
from pathlib import Path

import pytest

from nf_metro.api import prepare_graph
from nf_metro.layout.pass_metrics import LayoutMetrics
from nf_metro.render import render_svg
from nf_metro.themes import THEMES, resolve_stack

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
