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
from nf_metro.parser import parse_metro_mermaid_file
from nf_metro.render.svg import render_svg
from nf_metro.text_metrics import (
    DEFAULT_TEXT_METRICS,
    MetricsFace,
    TextRole,
    TextStyle,
)
from nf_metro.themes import THEMES

EXAMPLES = sorted((Path(__file__).parent.parent / "examples").glob("*.mmd"))
assert EXAMPLES, "the examples corpus these render modes are exercised on is missing"
FIXTURE_FILE = EXAMPLES[0]


def _render(fixture: Path, font_portability: str | None = None) -> str:
    graph = parse_metro_mermaid_file(fixture)
    compute_layout(graph)
    return render_svg(graph, THEMES["default"], font_portability=font_portability)  # type: ignore[arg-type]


# ── embed ────────────────────────────────────────────────────────────────────


def test_embed_font_injects_font_face_block() -> None:
    """SVG produced with font_portability='embed' contains an @font-face declaration."""
    svg = _render(FIXTURE_FILE, "embed")
    assert "@font-face" in svg, "Expected @font-face in embedded-font SVG"


def test_embed_font_contains_base64_data_uri() -> None:
    """The @font-face src must use a data URI (base64-encoded WOFF2), not a URL."""
    svg = _render(FIXTURE_FILE, "embed")
    assert "data:font/woff2;base64," in svg, (
        "Expected base64 WOFF2 data URI in @font-face"
    )


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


def test_plain_render_uses_helvetica() -> None:
    """Default render (font_portability=None) uses the Helvetica font stack."""
    svg = _render(FIXTURE_FILE)
    assert "Helvetica" in svg, "Default render must keep Helvetica font stack"


# ── paths ────────────────────────────────────────────────────────────────────


def test_text_to_paths_removes_all_text_elements() -> None:
    """SVG produced with font_portability='paths' must contain no <text> elements."""
    svg = _render(FIXTURE_FILE, "paths")
    assert "<text" not in svg, "paths mode must not leave any <text> elements"


def test_text_to_paths_produces_path_elements() -> None:
    """paths output must have <path> elements where text was."""
    svg = _render(FIXTURE_FILE, "paths")
    assert svg.count("<path ") > 0, "Expected <path> elements in paths output"


def test_text_to_paths_no_font_family_attributes() -> None:
    """paths output must not reference any font family."""
    svg = _render(FIXTURE_FILE, "paths")
    assert "font-family" not in svg, "font-family must be absent in paths output"


def test_text_to_paths_is_valid_svg() -> None:
    """paths output must be well-formed XML."""
    import xml.etree.ElementTree as ET

    svg = _render(FIXTURE_FILE, "paths")
    try:
        ET.fromstring(svg)
    except ET.ParseError as exc:
        pytest.fail(f"paths output is not valid XML: {exc}")


def test_text_to_paths_anchor_uses_centralized_inter_advance() -> None:
    from nf_metro.render.font_embed import text_to_paths

    source = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<text x="100" y="20" font-size="13" font-weight="600" '
        'text-anchor="middle">WWW</text></svg>'
    )
    converted = text_to_paths(source)
    match = re.search(r"translate\(([-0-9.]+),20\.000\)", converted)
    assert match, converted
    style = TextStyle(13.0, "600", MetricsFace.INTER)
    expected_x = 100.0 - DEFAULT_TEXT_METRICS.advance("WWW", style, TextRole.DEBUG) / 2
    assert float(match.group(1)) == pytest.approx(expected_x, abs=0.001)


def test_text_to_paths_draws_visible_replacement_for_missing_glyph() -> None:
    from nf_metro.render.font_embed import text_to_paths

    template = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<text x="0" y="20" font-size="13" font-weight="bold">{}</text></svg>'
    )
    missing = text_to_paths(template.format("Ω"))
    replacement = text_to_paths(template.format("?"))
    assert missing.count("<path ") == 1
    assert re.findall(r'<path d="([^"]+)"', missing) == re.findall(
        r'<path d="([^"]+)"', replacement
    )


def test_portable_render_uses_inter_metrics_during_layout() -> None:
    from nf_metro.api import RenderConfig, render_string

    source = (
        "%%metro line: x | X | #0570b0\n"
        "graph LR\nsubgraph s [S]\n%%metro direction: TB\na[WWW] -->|x| b[Ill]\nend\n"
    )
    fallback = render_string(source)
    embedded = render_string(source, config=RenderConfig(embed_font=True))

    def section_width(svg: str) -> float:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(svg)
        section = next(
            element
            for element in root.iter()
            if element.attrib.get("class") == "nf-metro-section-box"
        )
        return float(section.attrib["width"])

    fallback_width = section_width(fallback)
    embedded_width = section_width(embedded)
    assert fallback_width == pytest.approx(100.0)
    assert embedded_width == pytest.approx(107.46630859375)


def test_text_to_paths_applies_dominant_baseline() -> None:
    """dominant-baseline shifts glyph baselines instead of dropping the attribute."""
    from fontTools.ttLib import TTFont  # type: ignore[import]

    from nf_metro.render.font_embed import _FONTS_DIR, text_to_paths

    font = TTFont(str(_FONTS_DIR / "Inter-Bold.woff2"))
    upem = font["head"].unitsPerEm
    scale = 13.0 / upem
    cap_height = font["OS/2"].sCapHeight * scale
    central = ((font["hhea"].ascent + font["hhea"].descent) / 2.0) * scale
    middle = (font["OS/2"].sxHeight / 2.0) * scale

    cases = [
        ("auto", 131.0),
        ("hanging", 131.0 + cap_height),
        ("central", 131.0 + central),
        ("middle", 131.0 + middle),
    ]
    for baseline, expected_y in cases:
        source = (
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<text x="80" y="131.0" font-size="13.0" font-weight="bold" '
            f'text-anchor="middle" dominant-baseline="{baseline}">Trim reads</text>'
            "</svg>"
        )
        converted = text_to_paths(source)
        match = re.search(r"translate\([-0-9.]+,([-0-9.]+)\)", converted)
        assert match, (
            f"no path transform for dominant-baseline={baseline!r}: {converted}"
        )
        assert float(match.group(1)) == pytest.approx(expected_y, abs=0.01), (
            f"dominant-baseline={baseline!r}: got y={match.group(1)}, "
            f"expected {expected_y:.3f}"
        )


def test_text_to_paths_hanging_station_label_matches_plain_svg() -> None:
    """Issue #2166: below-station labels keep their top edge at y in paths mode."""
    from nf_metro.api import RenderConfig, render_string

    source = (
        "%%metro title: Baseline\n"
        "%%metro line: a | A | #e63946\n"
        "graph LR\n"
        "    subgraph s1 [Section]\n"
        "        x[Trim reads]\n"
        "        y[Align]\n"
        "        x -->|a| y\n"
        "    end\n"
    )
    plain = render_string(source, config=RenderConfig(embed_font=True))
    ttp = render_string(source, config=RenderConfig(text_to_paths=True))

    plain_match = re.search(
        r'<text\b[^>]*\by="([^"]+)"[^>]*dominant-baseline="hanging"[^>]*data-station-id="x"',
        plain,
    )
    assert plain_match, "expected hanging station label for station x"
    anchor_y = float(plain_match.group(1))

    group_match = re.search(
        r'<g\b[^>]*data-station-id="x"[^>]*>(.*?)</g>', ttp, re.DOTALL
    )
    assert group_match, "expected <g> for station x in text-to-paths output"
    path_match = re.search(r"translate\([-0-9.]+,([-0-9.]+)\)", group_match.group(1))
    assert path_match
    # Inter cap height is 1490/2048 * 13 = ~9.458px; baseline must sit below anchor_y.
    assert float(path_match.group(1)) == pytest.approx(
        anchor_y + 13.0 * 1490 / 2048, abs=0.01
    )
