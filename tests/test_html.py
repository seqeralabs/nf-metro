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
# Copied into a temp directory below, so it must name no asset that resolves
# relative to the map file.
STANDALONE_MMD = EXAMPLES_DIR / "variant_calling.mmd"


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
    mmd.write_text(STANDALONE_MMD.read_text())
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


def test_render_html_lines_json_escapes_script_close_in_label():
    """A line label containing a literal ``</script>`` must not terminate the
    outer ``<script>`` block that embeds the ``lines`` JSON early.

    The rendered SVG separately carries the manifest as ``<metadata>`` CDATA,
    where the same raw text is inert (CDATA sections are immune to
    ``</script>`` in SVG foreign content), so the assertion is scoped to the
    ``lines: [...]`` JS literal rather than the whole page.
    """
    graph = parse_metro_mermaid(
        "%%metro line: evil | </script><script>alert(1)</script> | #ff0000\n"
        "graph LR\n    a[A] -->|evil| b[B]\n"
    )
    compute_layout(graph)
    html_out = render_html(graph, THEMES["nfcore"])

    match = re.search(r"lines: (\[.*?\]),\n  embed:", html_out)
    assert match, "expected a `lines: [...]` JS literal in the standalone page"
    lines_literal = match.group(1)
    assert "</script>" not in lines_literal
    assert "<\\/script>" in lines_literal


DRIVER_JS = (
    Path(__file__).resolve().parent.parent / "src" / "nf_metro" / "render" / "driver.js"
)


def test_driver_js_escapes_untrusted_values_at_every_dom_sink():
    """driver.js re-reads ``getAttribute()``-sourced labels (which decode the
    SVG's own HTML-escaped entities) and line colour/label from the embedded
    ``lines`` JSON, then inserts both via ``innerHTML``; upstream Python-side
    escaping cannot protect that path, so this checks the source directly for
    the escaping helper being applied at each sink.

    This repo has no JS test runner wired up, so a source-inspection
    assertion is the fallback in place of an executed DOM test.
    """
    src = DRIVER_JS.read_text()
    assert "function escapeHtml(" in src

    chip_html = re.search(r"chip\.innerHTML =\n?(.*?);", src, re.DOTALL)
    assert chip_html, "expected the legend chip's innerHTML assignment"
    assert "escapeHtml(ln.color)" in chip_html.group(1)
    assert "escapeHtml(ln.label)" in chip_html.group(1)

    build_tip = re.search(r"function buildTip\(rect\) \{(.*?)\n  \}", src, re.DOTALL)
    assert build_tip, "expected the tooltip-building function"
    body = build_tip.group(1)
    assert "escapeHtml(label)" in body
    assert "escapeHtml(section)" in body
    assert "escapeHtml(ln.color)" in body
    assert "escapeHtml(ln.label)" in body


def test_render_html_embedded_svg_matches_standalone_render():
    """Embedded SVG matches the canonical legend-less SVG render for the input."""
    text = RNASEQ_MMD.read_text()
    theme = THEMES["nfcore"]

    graph_html = parse_metro_mermaid(text)
    graph_html.source_dir = str(EXAMPLES_DIR)
    compute_layout(graph_html)
    html_out = render_html(graph_html, theme)

    graph_svg = parse_metro_mermaid(text)
    graph_svg.source_dir = str(EXAMPLES_DIR)
    compute_layout(graph_svg)
    expected_svg = render_svg(graph_svg, theme, legend_position="none")

    assert expected_svg in html_out


def test_render_html_title_in_markup():
    """The graph title surfaces in the page header."""
    text = RNASEQ_MMD.read_text()
    graph = parse_metro_mermaid(text)
    graph.source_dir = str(EXAMPLES_DIR)
    compute_layout(graph)
    html_out = render_html(graph, THEMES["nfcore"])

    assert graph.title
    assert graph.title in html_out


# ---------------------------------------------------------------------------
# Embedding flags forwarded to the inlined SVG
# ---------------------------------------------------------------------------


def test_render_html_forwards_font_portability_embed():
    """font_portability='embed' inlines the webfont into the page's SVG."""
    text = RNASEQ_MMD.read_text()
    graph = parse_metro_mermaid(text)
    graph.source_dir = str(EXAMPLES_DIR)
    compute_layout(graph)

    plain = render_html(graph, THEMES["nfcore"])
    embedded = render_html(graph, THEMES["nfcore"], font_portability="embed")

    assert "@font-face" not in plain
    assert "@font-face" in embedded


def test_render_html_embed_font_via_cli(tmp_path):
    """render --format html --embed-font reaches the inlined SVG."""
    out = tmp_path / "out.html"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["render", str(RNASEQ_MMD), "-o", str(out), "--format", "html", "--embed-font"],
    )
    assert result.exit_code == 0, result.output
    assert "@font-face" in out.read_text()


def test_render_html_warns_on_svg_only_flags(tmp_path):
    """SVG-only sizing/namespacing flags warn (not silently ignored) for html."""
    out = tmp_path / "out.html"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "render",
            str(RNASEQ_MMD),
            "-o",
            str(out),
            "--format",
            "html",
            "--bare",
            "--svg-class-prefix",
            "foo",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "--bare" in result.output
    assert "--svg-class-prefix" in result.output
    assert "ignored for --format html" in result.output
