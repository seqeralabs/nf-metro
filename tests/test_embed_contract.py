"""Tests for the versioned embed contract.

Covers:
- The driver JS exposes the documented public API symbols.
- ``attachMetroMap`` return value is assigned so a host can retrieve it.
- The inline embed snippet dispatches ``nfmetro:ready`` with the API.
- The ``embed-script`` CLI command outputs the driver JS.
- Both output paths (standalone HTML, inline snippet) reference the
  same driver source so they cannot drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from nf_metro.cli import cli
from nf_metro.layout import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.html import render_html
from nf_metro.render.svg import render_svg
from nf_metro.themes import THEMES

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
RNASEQ_MMD = EXAMPLES_DIR / "rnaseq_sections.mmd"

REQUIRED_API_METHODS = ["highlightLine", "clearHighlight", "getManifest", "selectNode"]


@pytest.fixture(scope="module")
def rendered_html() -> str:
    text = RNASEQ_MMD.read_text()
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    return render_html(graph, THEMES["nfcore"])


@pytest.fixture(scope="module")
def rendered_svg() -> str:
    text = RNASEQ_MMD.read_text()
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    return render_svg(graph, THEMES["nfcore"])


def test_driver_js_exists_and_importable():
    """render.driver module is importable and exposes get_driver_js()."""
    from nf_metro.render.driver import get_driver_js

    js = get_driver_js()
    assert isinstance(js, str)
    assert len(js) > 100


def test_driver_js_exposes_required_api_methods():
    """Driver JS source defines all four documented public API methods."""
    from nf_metro.render.driver import get_driver_js

    js = get_driver_js()
    for method in REQUIRED_API_METHODS:
        assert method in js, f"driver JS missing public API method: {method}"


def test_driver_js_returns_api_object():
    """attachMetroMap returns an object containing the API methods."""
    from nf_metro.render.driver import get_driver_js

    js = get_driver_js()
    assert re.search(r"\breturn\s*\{", js), "attachMetroMap must return an API object"


def test_driver_contract_version_constant():
    """driver module exports DRIVER_CONTRACT_VERSION."""
    from nf_metro.render.driver import DRIVER_CONTRACT_VERSION

    assert isinstance(DRIVER_CONTRACT_VERSION, str)
    assert re.match(r"^\d+\.\d+$", DRIVER_CONTRACT_VERSION)


def test_standalone_html_assigns_api_to_window(rendered_html):
    """Standalone page assigns the attachMetroMap return value to window.nfMetroApi."""
    assert "nfMetroApi" in rendered_html, (
        "standalone HTML must expose nfMetroApi on window"
    )
    assert re.search(r"window\.nfMetroApi\s*=\s*attachMetroMap\(", rendered_html), (
        "window.nfMetroApi must be assigned the return value of attachMetroMap()"
    )


def test_standalone_html_contains_all_api_methods(rendered_html):
    """All public API method names appear in the rendered standalone HTML."""
    for method in REQUIRED_API_METHODS:
        assert method in rendered_html, f"standalone HTML missing API method: {method}"


def test_inline_snippet_dispatches_ready_event(rendered_html):
    """Inline embed snippet dispatches nfmetro:ready so a host can retrieve the API."""
    assert "nfmetro:ready" in rendered_html, (
        "inline embed snippet must dispatch nfmetro:ready custom event"
    )


def test_embed_script_cli_exits_zero():
    """embed-script command exits 0 and writes to stdout."""
    runner = CliRunner()
    result = runner.invoke(cli, ["embed-script"])
    assert result.exit_code == 0, result.output
    assert len(result.output) > 100


def test_embed_script_cli_output_contains_attach_function():
    """embed-script output contains attachMetroMap function definition."""
    runner = CliRunner()
    result = runner.invoke(cli, ["embed-script"])
    assert "function attachMetroMap" in result.output


def test_embed_script_cli_output_contains_api_methods():
    """embed-script output contains all documented public API methods."""
    runner = CliRunner()
    result = runner.invoke(cli, ["embed-script"])
    for method in REQUIRED_API_METHODS:
        assert method in result.output, f"embed-script output missing: {method}"


def test_embed_script_writes_file(tmp_path):
    """embed-script -o <path> writes the JS to a file."""
    out = tmp_path / "nf-metro-embed.js"
    runner = CliRunner()
    result = runner.invoke(cli, ["embed-script", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "attachMetroMap" in out.read_text()


def test_html_inlines_driver_source_verbatim(rendered_html):
    """The rendered HTML embeds the exact same JS that get_driver_js() returns."""
    from nf_metro.render.driver import get_driver_js

    driver = get_driver_js()
    assert driver in rendered_html, (
        "rendered HTML must embed driver JS verbatim so the two paths cannot drift"
    )


def test_embed_script_matches_inlined_driver():
    """embed-script output matches get_driver_js() exactly."""
    from nf_metro.render.driver import get_driver_js

    runner = CliRunner()
    result = runner.invoke(cli, ["embed-script"])
    assert result.output.strip() == get_driver_js().strip()


def test_svg_carries_station_data_attributes(rendered_svg):
    """SVG output carries data-station-id and data-station-lines on station elements."""
    assert "data-station-id=" in rendered_svg
    assert "data-station-lines=" in rendered_svg
    assert "data-station-label=" in rendered_svg


def test_svg_carries_section_data_attributes(rendered_svg):
    """SVG output carries data-section-id and data-section-lines on section boxes."""
    assert "data-section-id=" in rendered_svg
    assert "data-section-lines=" in rendered_svg


def test_svg_carries_line_data_attributes(rendered_svg):
    """SVG output carries data-line-id on edge elements."""
    assert "data-line-id=" in rendered_svg
