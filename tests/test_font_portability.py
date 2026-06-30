"""Tests for font-portability SVG render modes (--embed-font, --text-to-paths).

Invariants:
- font_portability="embed" injects an @font-face block with base64-encoded
  Inter data and replaces font-family references with "Inter".
- font_portability="paths" replaces every <text> element with <path> elements
  and leaves no <text> elements in the output.
"""

import re
from pathlib import Path

import pytest

from nf_metro.layout import compute_layout
from nf_metro.parser import parse_metro_mermaid
from nf_metro.render.svg import render_svg
from nf_metro.themes import THEMES

EXAMPLES = list((Path(__file__).parent.parent / "examples").glob("*.mmd"))
FIXTURE_FILE = EXAMPLES[0] if EXAMPLES else None


def _render(fixture: Path, font_portability: str | None = None) -> str:
    text = fixture.read_text()
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    return render_svg(graph, THEMES["nfcore"], font_portability=font_portability)  # type: ignore[arg-type]


# ── embed ────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(FIXTURE_FILE is None, reason="no example fixtures found")
def test_embed_font_injects_font_face_block() -> None:
    """SVG produced with font_portability='embed' contains an @font-face declaration."""
    svg = _render(FIXTURE_FILE, "embed")
    assert "@font-face" in svg, "Expected @font-face in embedded-font SVG"


@pytest.mark.skipif(FIXTURE_FILE is None, reason="no example fixtures found")
def test_embed_font_contains_base64_data_uri() -> None:
    """The @font-face src must use a data URI (base64-encoded WOFF2), not a URL."""
    svg = _render(FIXTURE_FILE, "embed")
    assert "data:font/woff2;base64," in svg, (
        "Expected base64 WOFF2 data URI in @font-face"
    )


@pytest.mark.skipif(FIXTURE_FILE is None, reason="no example fixtures found")
@pytest.mark.parametrize("fixture", EXAMPLES, ids=lambda p: p.name)
def test_embed_font_family_has_generic_fallback(fixture: Path) -> None:
    """Every embedded font-family must end in a generic family so a stripped
    @font-face degrades to sans-serif (not the browser serif default)."""
    svg = _render(fixture, "embed")
    families = re.findall(r'font-family="([^"]*)"', svg)
    assert families, "expected font-family attributes in embedded SVG"
    for family in set(families):
        assert family.startswith("Inter"), f"Inter must lead the stack: {family!r}"
        assert family.rstrip().endswith("sans-serif"), (
            f"embedded font-family lacks a generic fallback: {family!r}"
        )


@pytest.mark.skipif(FIXTURE_FILE is None, reason="no example fixtures found")
def test_plain_render_uses_helvetica() -> None:
    """Default render (font_portability=None) uses the Helvetica font stack."""
    svg = _render(FIXTURE_FILE)
    assert "Helvetica" in svg, "Default render must keep Helvetica font stack"


# ── paths ────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(FIXTURE_FILE is None, reason="no example fixtures found")
def test_text_to_paths_removes_all_text_elements() -> None:
    """SVG produced with font_portability='paths' must contain no <text> elements."""
    svg = _render(FIXTURE_FILE, "paths")
    assert "<text" not in svg, "paths mode must not leave any <text> elements"


@pytest.mark.skipif(FIXTURE_FILE is None, reason="no example fixtures found")
def test_text_to_paths_produces_path_elements() -> None:
    """paths output must have <path> elements where text was."""
    svg = _render(FIXTURE_FILE, "paths")
    assert svg.count("<path ") > 0, "Expected <path> elements in paths output"


@pytest.mark.skipif(FIXTURE_FILE is None, reason="no example fixtures found")
def test_text_to_paths_no_font_family_attributes() -> None:
    """paths output must not reference any font family."""
    svg = _render(FIXTURE_FILE, "paths")
    assert "font-family" not in svg, "font-family must be absent in paths output"


@pytest.mark.skipif(FIXTURE_FILE is None, reason="no example fixtures found")
def test_text_to_paths_is_valid_svg() -> None:
    """paths output must be well-formed XML."""
    import xml.etree.ElementTree as ET

    svg = _render(FIXTURE_FILE, "paths")
    try:
        ET.fromstring(svg)
    except ET.ParseError as exc:
        pytest.fail(f"paths output is not valid XML: {exc}")
