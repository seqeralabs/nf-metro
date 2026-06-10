"""Tests for the ``%%metro process:`` directive (live-progress mapping)."""

import re
import warnings

import pytest

from nf_metro.layout import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render import render_svg
from nf_metro.render.manifest import MANIFEST_ELEMENT_ID, read_manifest
from nf_metro.themes import THEMES

_METADATA_BLOCK = re.compile(
    rf'<metadata id="{re.escape(MANIFEST_ELEMENT_ID)}".*?</metadata>', re.S
)


def _visual_svg(svg: str) -> str:
    """The SVG with the embedded manifest stripped (the drawn output only)."""
    return _METADATA_BLOCK.sub("", svg)


BASE = (
    "%%metro line: a | A | #ff0000 | solid\n"
    "graph LR\n"
    "    input[In] -->|a| trim[Trim]\n"
    "    trim -->|a| out[Out]\n"
)


def test_process_directive_maps_station():
    graph = parse_metro_mermaid("%%metro process: trim | TRIMGALORE\n" + BASE)
    assert graph.process_mapping == {"trim": ["TRIMGALORE"]}


def test_process_directive_appends_repeated_patterns():
    text = (
        "%%metro process: trim | TRIMGALORE\n%%metro process: trim | ^FASTP$\n" + BASE
    )
    graph = parse_metro_mermaid(text)
    assert graph.process_mapping == {"trim": ["TRIMGALORE", "^FASTP$"]}


def test_process_directive_pattern_keeps_commas():
    # A regex quantifier like {1,3} must survive: the value is one pattern,
    # not comma-split.
    graph = parse_metro_mermaid("%%metro process: trim | TRIM_.{1,3}\n" + BASE)
    assert graph.process_mapping == {"trim": ["TRIM_.{1,3}"]}


def test_process_directive_unknown_station_warns_and_drops():
    with pytest.warns(UserWarning, match="unknown station id 'ghost'"):
        graph = parse_metro_mermaid("%%metro process: ghost | NOPE\n" + BASE)
    assert "ghost" not in graph.process_mapping


def test_process_directive_invalid_regex_warns_and_drops():
    with pytest.warns(UserWarning, match="invalid regex"):
        graph = parse_metro_mermaid("%%metro process: trim | (unclosed\n" + BASE)
    assert graph.process_mapping == {}


def test_process_directive_malformed_warns():
    with pytest.warns(UserWarning, match="process"):
        graph = parse_metro_mermaid("%%metro process: trim\n" + BASE)
    assert graph.process_mapping == {}


def test_process_directive_does_not_change_visual_render():
    """The directive must not perturb layout or the drawn SVG output.

    It is no longer pure metadata at the byte level: the mapping is now
    serialized into the embedded ``<metadata>`` manifest (so a committed SVG
    carries it). But everything *drawn* must be byte-identical -- the directive
    still never moves a station or changes a glyph.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        plain = parse_metro_mermaid(BASE)
        mapped = parse_metro_mermaid("%%metro process: trim | TRIMGALORE\n" + BASE)
    compute_layout(plain)
    compute_layout(mapped)
    theme = THEMES["nfcore"]
    plain_svg = render_svg(plain, theme)
    mapped_svg = render_svg(mapped, theme)

    # Identical once the manifest is stripped (nothing drawn changed)...
    assert _visual_svg(plain_svg) == _visual_svg(mapped_svg)
    # ...but the manifest carries the mapping difference.
    plain_proc = {n["id"]: n["patterns"] for n in read_manifest(plain_svg)["nodes"]}
    mapped_proc = {n["id"]: n["patterns"] for n in read_manifest(mapped_svg)["nodes"]}
    assert plain_proc["trim"] == []
    assert mapped_proc["trim"] == ["TRIMGALORE"]
