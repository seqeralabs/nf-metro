"""Tests for the ``%%metro process:`` directive (live-progress mapping)."""

import warnings

import pytest

from nf_metro.layout import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render import render_svg
from nf_metro.themes import THEMES

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


def test_process_directive_does_not_change_render():
    """Pure metadata: the directive must not perturb layout or SVG output."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        plain = parse_metro_mermaid(BASE)
        mapped = parse_metro_mermaid("%%metro process: trim | TRIMGALORE\n" + BASE)
    compute_layout(plain)
    compute_layout(mapped)
    theme = THEMES["nfcore"]
    assert render_svg(plain, theme) == render_svg(mapped, theme)
