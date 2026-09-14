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


def test_auto_process_off_by_default_leaves_mapping_empty():
    graph = parse_metro_mermaid(BASE)
    assert graph.process_mapping == {}


def test_auto_process_derives_id_pattern_for_every_real_station():
    graph = parse_metro_mermaid("%%metro auto_process: true\n" + BASE)
    assert set(graph.process_mapping) == {"input", "trim", "out"}
    from nf_metro.live.mapping import stations_for_process

    m = graph.process_mapping
    assert stations_for_process("NFCORE:PIPE:TRIM", m) == ["trim"]
    # The id matches as a prefix of the final segment, so an abstraction id
    # lights up its tool's process (e.g. 'star' -> STAR_ALIGN).
    assert stations_for_process("NFCORE:PIPE:TRIM_GALORE", m) == ["trim"]


def test_auto_process_anchors_to_final_segment_not_scope_path():
    """A station id must not match a tool name buried in the scope path."""
    from nf_metro.live.mapping import stations_for_process

    graph = parse_metro_mermaid("%%metro auto_process: true\n" + BASE)
    m = graph.process_mapping
    assert stations_for_process("NFCORE:TRIM_SUBWF:SAMTOOLS", m) == []


def test_auto_process_explicit_directive_overrides_default():
    text = "%%metro auto_process: true\n%%metro process: trim | ^FASTP$\n" + BASE
    graph = parse_metro_mermaid(text)
    assert graph.process_mapping["trim"] == ["^FASTP$"]
    assert graph.process_mapping["input"] == [r"(?:^|:)input[^:]*$"]


def test_process_scope_requires_prefix_and_tail():
    from nf_metro.live.mapping import stations_for_process

    text = (
        "%%metro process_scope: NFCORE:PIPE\n"
        "%%metro process: trim | SUBWF:TRIMGALORE\n" + BASE
    )
    m = parse_metro_mermaid(text).process_mapping
    assert stations_for_process("NFCORE:PIPE:SUBWF:TRIMGALORE", m) == ["trim"]
    # the scope prefix is required
    assert stations_for_process("OTHER:SUBWF:TRIMGALORE", m) == []


def test_process_scope_tolerates_intermediate_nesting():
    """The tail need not sit directly under the scope: any subworkflow nesting
    between the prefix and the tail is tolerated, so the author need not know
    the full path."""
    from nf_metro.live.mapping import stations_for_process

    text = (
        "%%metro process_scope: NFCORE:PIPE\n"
        "%%metro process: trim | SUBWF:TRIMGALORE\n" + BASE
    )
    m = parse_metro_mermaid(text).process_mapping
    nested = "NFCORE:PIPE:QC_TRIM_SETSTRANDEDNESS:SUBWF:TRIMGALORE"
    assert stations_for_process(nested, m) == ["trim"]


def test_process_scope_value_is_literal_not_regex():
    """A scoped tail is matched literally, so a '.' is a dot, not a wildcard."""
    from nf_metro.live.mapping import stations_for_process

    text = "%%metro process_scope: NFCORE\n%%metro process: trim | A.C\n" + BASE
    m = parse_metro_mermaid(text).process_mapping
    assert stations_for_process("NFCORE:A.C", m) == ["trim"]
    assert stations_for_process("NFCORE:AXC", m) == []


def test_process_scope_disambiguates_same_leaf_under_two_scopes():
    from nf_metro.live.mapping import stations_for_process

    text = (
        "%%metro line: a | A | #ff0000\n"
        "%%metro process_scope: NFCORE:PIPE\n"
        "%%metro process: q_star   | STAR:QUANT\n"
        "%%metro process: q_pseudo | PSEUDO:QUANT\n"
        "graph LR\n"
        "    in[In] -->|a| q_star[Q1]\n"
        "    in -->|a| q_pseudo[Q2]\n"
    )
    graph = parse_metro_mermaid(text)
    m = graph.process_mapping
    assert stations_for_process("NFCORE:PIPE:STAR:QUANT", m) == ["q_star"]
    assert stations_for_process("NFCORE:PIPE:PSEUDO:QUANT", m) == ["q_pseudo"]


def test_process_scope_absent_keeps_regex_semantics():
    from nf_metro.live.mapping import stations_for_process

    graph = parse_metro_mermaid("%%metro process: trim | TRIM.*\n" + BASE)
    assert graph.process_mapping["trim"] == ["TRIM.*"]
    assert stations_for_process("NFCORE:TRIMGALORE", graph.process_mapping) == ["trim"]


def test_process_directive_does_not_change_visual_render():
    """The directive must not perturb layout or the drawn SVG output.

    It is not pure metadata at the byte level: the mapping is serialized into
    the embedded ``<metadata>`` manifest, so a committed SVG carries it. But
    everything *drawn* must be byte-identical -- the directive never moves a
    station or changes a glyph.
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
