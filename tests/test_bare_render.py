"""Tests for bare render mode.

Bare mode omits the title and outer padding so the SVG is a tight content
fragment suitable for embedding.  The attribution watermark is NOT removed.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from click.testing import CliRunner

from nf_metro.cli import cli
from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.constants import (
    CANVAS_PADDING,
    WATERMARK_FONT_SIZE,
    WATERMARK_Y_INSET,
)
from nf_metro.render.svg import render_svg
from nf_metro.themes import NFCORE_THEME

_TITLED_MMD = (
    "%%metro title: My Pipeline\n"
    "%%metro line: main | Main | #ff0000\n"
    "graph LR\n"
    "    a[Input] -->|main| b[Output]\n"
)

_MULTI_SECTION_MMD = Path("examples/rnaseq_sections.mmd")


def _graph(text: str):
    g = parse_metro_mermaid(text)
    compute_layout(g)
    return g


def _title_text_elements(svg: str) -> list[ET.Element]:
    """Return all <text> elements that carry the nf-metro-title class."""
    root = ET.fromstring(svg)
    return [
        el
        for el in root.iter("{http://www.w3.org/2000/svg}text")
        if "nf-metro-title" in el.get("class", "")
    ]


def test_bare_omits_title():
    """No title text element is rendered in bare output."""
    g = _graph(_TITLED_MMD)
    bare_svg = render_svg(g, NFCORE_THEME, bare=True)
    assert _title_text_elements(bare_svg) == []


def test_full_chrome_still_has_title():
    """Full-chrome render includes the title."""
    g = _graph(_TITLED_MMD)
    full_svg = render_svg(g, NFCORE_THEME)
    assert "My Pipeline" in full_svg


def test_bare_keeps_watermark():
    """Watermark attribution must survive bare mode."""
    g = _graph(_TITLED_MMD)
    bare_svg = render_svg(g, NFCORE_THEME, bare=True)
    assert "nf-metro" in bare_svg


def test_bare_canvas_narrower_than_full():
    """Bare canvas must be narrower (right padding dropped)."""
    g = _graph(_TITLED_MMD)
    full_svg = render_svg(g, NFCORE_THEME)
    bare_svg = render_svg(g, NFCORE_THEME, bare=True)

    full_width = int(ET.fromstring(full_svg).attrib["width"])
    bare_width = int(ET.fromstring(bare_svg).attrib["width"])

    assert bare_width < full_width
    assert full_width - bare_width == pytest.approx(CANVAS_PADDING, abs=2)


def test_bare_canvas_narrower_multi_section():
    """Right-padding removal holds for a multi-section diagram."""
    if not _MULTI_SECTION_MMD.exists():
        pytest.skip("rnaseq_sections.mmd not found")
    g = _graph(_MULTI_SECTION_MMD.read_text())
    full_svg = render_svg(g, NFCORE_THEME)
    bare_svg = render_svg(g, NFCORE_THEME, bare=True)

    full_width = int(ET.fromstring(full_svg).attrib["width"])
    bare_width = int(ET.fromstring(bare_svg).attrib["width"])

    assert bare_width < full_width


def test_bare_viewbox_starts_at_origin():
    """viewBox must begin with '0 0' so overlay alignment is preserved."""
    g = _graph(_TITLED_MMD)
    bare_svg = render_svg(g, NFCORE_THEME, bare=True)
    root = ET.fromstring(bare_svg)
    vb = root.attrib.get("viewBox", "")
    assert vb.startswith("0 0"), f"viewBox={vb!r} does not start at origin"


def test_full_viewbox_starts_at_origin():
    """Full-chrome viewBox must also start at origin (no regression)."""
    g = _graph(_TITLED_MMD)
    full_svg = render_svg(g, NFCORE_THEME)
    root = ET.fromstring(full_svg)
    vb = root.attrib.get("viewBox", "")
    assert vb.startswith("0 0"), f"viewBox={vb!r} does not start at origin"


def test_bare_height_includes_watermark():
    """Height must accommodate the watermark even in bare mode."""
    g = _graph(_TITLED_MMD)
    bare_svg = render_svg(g, NFCORE_THEME, bare=True)
    h = int(ET.fromstring(bare_svg).attrib["height"])
    assert h >= WATERMARK_Y_INSET + WATERMARK_FONT_SIZE


def test_bare_is_valid_svg():
    """Bare output must be well-formed XML with an svg root."""
    g = _graph(_TITLED_MMD)
    bare_svg = render_svg(g, NFCORE_THEME, bare=True)
    root = ET.fromstring(bare_svg)
    assert root.tag.endswith("svg") or "svg" in root.tag


def test_bare_cli_flag(tmp_path):
    """--bare CLI flag produces tighter output than default."""
    runner = CliRunner()
    src = Path("examples/rnaseq_sections.mmd")
    if not src.exists():
        pytest.skip("rnaseq_sections.mmd not found")

    full_out = tmp_path / "full.svg"
    bare_out = tmp_path / "bare.svg"

    result_full = runner.invoke(cli, ["render", str(src), "-o", str(full_out)])
    assert result_full.exit_code == 0, result_full.output

    result_bare = runner.invoke(
        cli, ["render", str(src), "-o", str(bare_out), "--bare"]
    )
    assert result_bare.exit_code == 0, result_bare.output

    full_width = int(ET.parse(full_out).getroot().attrib["width"])
    bare_width = int(ET.parse(bare_out).getroot().attrib["width"])
    assert bare_width < full_width


def test_bare_no_title_class_element():
    """No SVG element with the nf-metro-title class is drawn in bare output."""
    g = _graph(_TITLED_MMD)
    bare_svg = render_svg(g, NFCORE_THEME, bare=True)
    assert _title_text_elements(bare_svg) == []
