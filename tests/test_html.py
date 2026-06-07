"""Tests for the interactive HTML output mode (``render --format html``)."""

import re
from pathlib import Path

from click.testing import CliRunner

from nf_metro.cli import cli
from nf_metro.layout import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.html import render_html
from nf_metro.render.svg import render_svg
from nf_metro.themes import THEMES

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
RNASEQ_MMD = EXAMPLES_DIR / "rnaseq_sections.mmd"


def _render_html_via_cli(tmp_path):
    out = tmp_path / "output.html"
    runner = CliRunner()
    result = runner.invoke(
        cli, ["render", str(RNASEQ_MMD), "-o", str(out), "--format", "html"]
    )
    return result, out


def test_render_html_exits_zero_and_writes_nonempty(tmp_path):
    """render --format html exits 0 and writes a non-empty .html file."""
    result, out = _render_html_via_cli(tmp_path)
    assert result.exit_code == 0, result.output
    assert out.exists()
    content = out.read_text()
    assert len(content) > 0
    assert content.lstrip().startswith("<!DOCTYPE html>")


def test_render_html_default_output_extension(tmp_path):
    """render --format html defaults the output filename to the input stem + .html."""
    mmd = tmp_path / "diagram.mmd"
    mmd.write_text(RNASEQ_MMD.read_text())
    runner = CliRunner()
    result = runner.invoke(cli, ["render", str(mmd), "--format", "html"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "diagram.html").exists()


def test_render_html_embeds_svg(tmp_path):
    """The rendered page inlines the SVG markup rather than linking to it."""
    _result, out = _render_html_via_cli(tmp_path)
    content = out.read_text()
    assert "<svg" in content
    assert "</svg>" in content


def test_render_html_is_self_contained(tmp_path):
    """No external network or script dependencies: JS/CSS/SVG are all inlined."""
    _result, out = _render_html_via_cli(tmp_path)
    content = out.read_text()

    assert not re.search(r"<script[^>]*\bsrc\s*=", content)
    assert not re.search(r"<link[^>]*\brel\s*=\s*[\"']stylesheet[\"']", content)
    assert not re.search(r"<img[^>]*\bsrc\s*=\s*[\"']https?://", content)

    fetchable = [
        url
        for url in re.findall(r"https?://[^\s\"'<>\\]+", content)
        if not url.startswith("http://www.w3.org/")
    ]
    assert fetchable == []

    assert "<style>" in content
    assert "<script>" in content


def test_render_html_interactive_scaffolding(tmp_path):
    """Pan/zoom/filter hooks and the shared attach function are present."""
    _result, out = _render_html_via_cli(tmp_path)
    content = out.read_text()

    assert "attachMetroMap(" in content
    assert "function attachMetroMap(opts)" in content

    assert "addEventListener('mousedown'" in content
    assert "addEventListener('wheel'" in content
    assert "setAttribute('viewBox'" in content

    assert "nf-metro-canvas" in content
    assert "nf-metro-legend" in content
    assert "nf-metro-reset" in content


def test_render_html_embed_snippet_present(tmp_path):
    """The embed modal carries inline / iframe / svg copy snippets."""
    _result, out = _render_html_via_cli(tmp_path)
    content = out.read_text()

    assert "nf-metro-embed-btn" in content
    assert "nf-metro-embed-modal" in content
    assert 'data-copy="inline"' in content
    assert 'data-copy="iframe"' in content
    assert 'data-copy="svg"' in content
    assert "nf-metro-snippet-inline" in content


def test_render_html_closing_script_tag_escaped(tmp_path):
    """The inlined embed snippet escapes </ so the outer <script> survives parsing.

    The snippet itself contains a nested ``</script>``; if it were emitted
    literally the browser would terminate the outer script element early.
    """
    _result, out = _render_html_via_cli(tmp_path)
    content = out.read_text()

    assert content.count("</script>") == 1
    assert "<\\/script>" in content


def test_render_html_embedded_svg_matches_standalone_render():
    """Embedded SVG matches the canonical legend-less SVG render for the input."""
    text = RNASEQ_MMD.read_text()
    theme = THEMES["nfcore"]

    graph_html = parse_metro_mermaid(text)
    compute_layout(graph_html)
    html_out = render_html(graph_html, theme)

    graph_svg = parse_metro_mermaid(text)
    compute_layout(graph_svg)
    expected_svg = render_svg(graph_svg, theme, legend_position="none")

    assert expected_svg in html_out


def test_render_html_title_in_markup():
    """The graph title surfaces in the page header."""
    text = RNASEQ_MMD.read_text()
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    html_out = render_html(graph, THEMES["nfcore"])

    assert graph.title
    assert graph.title in html_out
