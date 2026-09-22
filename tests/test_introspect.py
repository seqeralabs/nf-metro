"""Tests for structured `nf-metro info` introspection (``nf_metro.introspect``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nf_metro.introspect import (
    build_info,
    format_info_json,
    format_info_text,
    station_kind,
)
from nf_metro.parser import parse_metro_mermaid

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"

# A spread of gallery fixtures: fully-inferred auto layout, a hand-tuned mix of
# explicit and inferred directives, and a couple of distinct topologies so the
# invariants generalise beyond a single .mmd.
FIXTURES = [
    "rnaseq_auto.mmd",
    "rnaseq_sections.mmd",
    "genomeassembly.mmd",
    "epitopeprediction.mmd",
]


def _graph(fixture: str):
    return parse_metro_mermaid((EXAMPLES_DIR / fixture).read_text())


@pytest.mark.parametrize("fixture", FIXTURES)
def test_info_has_all_top_level_keys(fixture: str) -> None:
    info = build_info(_graph(fixture))
    assert set(info) == {
        "title",
        "caption",
        "style",
        "warnings",
        "counts",
        "lines",
        "sections",
        "stations",
        "ports",
        "junctions",
        "section_dag",
        "layout",
    }


@pytest.mark.parametrize(
    "style, expected",
    [("", "nfcore"), ("dark", "nfcore"), ("seqera", "seqera"), ("nonesuch", "nfcore")],
)
def test_style_reports_the_resolved_theme(style: str, expected: str) -> None:
    """The reported style is a name ``--theme`` accepts, whatever was authored."""
    graph = parse_metro_mermaid(
        (f"%%metro style: {style}\n" if style else "")
        + "%%metro line: a | A | #ff0000\n"
        + "graph LR\n  n1[N1] -->|a| n2[N2]\n"
    )
    assert build_info(graph)["style"] == expected


@pytest.mark.parametrize("fixture", FIXTURES)
def test_counts_match_graph(fixture: str) -> None:
    graph = _graph(fixture)
    counts = build_info(graph)["counts"]
    assert counts["stations"] == len(graph.stations)
    assert counts["edges"] == len(graph.edges)
    assert counts["lines"] == len(graph.lines)
    assert counts["sections"] == len(graph.sections)
    assert counts["ports"] == len(graph.ports)
    assert counts["junctions"] == len(graph.junctions)


@pytest.mark.parametrize("fixture", FIXTURES)
def test_routes_exclude_synthetic_stations(fixture: str) -> None:
    """A line's ``route`` lists only authored stations, never ports/junctions."""
    graph = _graph(fixture)
    info = build_info(graph)
    for line in info["lines"]:
        for sid in line["route"]:
            assert sid not in graph.ports
            assert sid not in graph.junction_ids
            assert station_kind(graph, sid) == "station"


def test_verbose_routes_keeps_both_lines_sharing_a_display_name() -> None:
    """Two lines may share a display name; ``--verbose``'s routes: must not drop one.

    ``%%metro line:`` only enforces a unique id, not a unique display name,
    and ``routes:`` is a dict keyed by name for readability - a plain
    ``{display_name: route}`` comprehension collides and silently drops one
    line's route.
    """
    graph = parse_metro_mermaid(
        "%%metro line: main1 | Main Line | #ff0000\n"
        "%%metro line: main2 | Main Line | #00ff00\n"
        "graph LR\n"
        "  a[A] -->|main1| b[B]\n"
        "  b -->|main2| c[C]\n"
    )
    verbose = format_info_text(build_info(graph), verbose=True)
    assert "Main Line (main1):" in verbose
    assert "Main Line (main2):" in verbose


def test_verbose_routes_disambiguates_a_second_order_collision() -> None:
    """A disambiguated key can itself collide with a third line's plain name.

    Two lines named "Main" disambiguate to "Main (main1)"/"Main (main2)".
    A third, differently-named line whose own display name happens to be
    the literal string "Main (main1)" would then collide with the first
    line's disambiguated key, silently dropping its route, unless keys are
    checked for uniqueness against every key already assigned rather than
    only against their own name's duplicate count.
    """
    graph = parse_metro_mermaid(
        "%%metro line: main1 | Main | #ff0000\n"
        "%%metro line: main2 | Main | #00ff00\n"
        "%%metro line: other | Main (main1) | #0000ff\n"
        "graph LR\n"
        "  a[A] -->|main1| b[B]\n"
        "  b -->|main2| c[C]\n"
        "  c -->|other| d[D]\n"
    )
    verbose = format_info_text(build_info(graph), verbose=True)
    assert "Main (main1):" in verbose
    assert "Main (main2):" in verbose
    # "#2" needs quoting: a space before "#" opens a YAML comment.
    assert "'Main (main1) #2':" in verbose


@pytest.mark.parametrize("fixture", FIXTURES)
def test_synthetic_elements_surfaced(fixture: str) -> None:
    """Ports and junctions appear in the inventory with the right kind."""
    graph = _graph(fixture)
    info = build_info(graph)
    by_id = {st["id"]: st for st in info["stations"]}

    # Every junction is classified as a junction, not mislabelled a port, even
    # though its underlying station carries is_port=True.
    for jid in graph.junctions:
        assert by_id[jid]["kind"] == "junction"
    assert sorted(graph.junctions) == info["junctions"]

    # Every port is present and classified as a port.
    for pid in graph.ports:
        assert by_id[pid]["kind"] == "port"
    assert {p["id"] for p in info["ports"]} == set(graph.ports)


@pytest.mark.parametrize("fixture", FIXTURES)
def test_section_dag_edges_match(fixture: str) -> None:
    graph = _graph(fixture)
    info = build_info(graph)
    reported = {(e["from"], e["to"]) for e in info["section_dag"]["edges"]}
    assert reported == set(graph.section_dag.section_edges)
    for edge in info["section_dag"]["edges"]:
        assert edge["lines"] == sorted(
            graph.section_dag.edge_lines[(edge["from"], edge["to"])]
        )


def test_inferred_when_no_directives() -> None:
    """An all-auto fixture reports every direction, grid, and port side inferred."""
    info = build_info(_graph("rnaseq_auto.mmd"))
    for sec in info["sections"]:
        assert sec["direction_inferred"] is True
        assert sec["grid_inferred"] is True
        assert sec["entry_sides_inferred"] is (
            True if sec["entry_side_provenance"] else None
        )
        assert sec["exit_sides_inferred"] is (
            True if sec["exit_side_provenance"] else None
        )
    for port in info["ports"]:
        assert port["side_inferred"] is True


def test_sections_without_connector_endpoints_report_no_side_ownership() -> None:
    graph = parse_metro_mermaid(
        """\
%%metro line: a | A | #ff0000
graph LR
    subgraph source [Source]
        %%metro entry: top | a
        s1[S1]
    end
    subgraph target [Target]
        t1[T1]
    end
    s1 -->|a| t1
"""
    )
    sections = {item["id"]: item for item in build_info(graph)["sections"]}

    assert sections["source"]["entry_side_provenance"] == []
    assert sections["source"]["entry_sides_inferred"] is None
    assert sections["source"]["exit_sides_inferred"] is True
    assert sections["target"]["entry_sides_inferred"] is True
    assert sections["target"]["exit_side_provenance"] == []
    assert sections["target"]["exit_sides_inferred"] is None


def test_explicit_directives_reported_as_explicit() -> None:
    """Authored direction:/entry:/exit: directives are flagged explicit, not inferred.

    rnaseq_sections pins postprocessing to TB and qc_report to RL, and writes
    explicit entry/exit sides; the inferred sections around them stay inferred.
    """
    sections = {
        s["id"]: s for s in build_info(_graph("rnaseq_sections.mmd"))["sections"]
    }

    assert sections["postprocessing"]["direction"] == "TB"
    assert sections["postprocessing"]["direction_inferred"] is False
    assert sections["qc_report"]["direction"] == "RL"
    assert sections["qc_report"]["direction_inferred"] is False
    # A section without a direction: directive keeps the inferred default.
    assert sections["preprocessing"]["direction_inferred"] is True

    # preprocessing has authored right and bottom exit hints. Resolution keeps
    # the right hints and selects right for the bottom-hinted connectors.
    assert sections["preprocessing"]["exit_sides_inferred"] is None
    assert sections["preprocessing"]["entry_sides_inferred"] is None


def test_port_side_inferred_tracks_section_directive() -> None:
    """A port's summary is derived from its connector endpoint records."""
    graph = _graph("rnaseq_sections.mmd")
    info = build_info(graph)
    for port in info["ports"]:
        origins = {item["provenance"]["origin"] for item in port["side_provenance"]}
        expected = None if len(origins) > 1 else origins != {"authored"}
        assert port["side_inferred"] is expected


def test_partially_hinted_port_reports_connector_specific_provenance() -> None:
    graph = parse_metro_mermaid(
        """\
%%metro line: a | A | #ff0000
%%metro line: b | B | #0000ff
%%metro grid: source | 0,0
%%metro grid: target | 1,0
graph LR
    subgraph source [Source]
        s1[S1]
    end
    subgraph target [Target]
        %%metro entry: top | a
        t1[T1]
    end
    s1 -->|a,b| t1
"""
    )
    target = next(
        section
        for section in build_info(graph)["sections"]
        if section["id"] == "target"
    )
    by_line = {
        record["line_id"]: record["provenance"]
        for record in target["entry_side_provenance"]
    }

    assert target["entry_sides_inferred"] is None
    assert by_line["a"]["state"] == "authored"
    assert by_line["b"]["state"] == "inferred"
    assert by_line["b"]["reason"] == "shared-connector-entry-side"


def test_warnings_passed_through() -> None:
    info = build_info(_graph("rnaseq_auto.mmd"), ["something happened"])
    assert info["warnings"] == ["something happened"]


def test_layout_rows_reflect_folding() -> None:
    """sections_by_row partitions the real sections; folded iff >1 row."""
    graph = _graph("rnaseq_sections.mmd")
    info = build_info(graph)
    layout = info["layout"]
    placed = {sid for ids in layout["sections_by_row"].values() for sid in ids}
    real = {sid for sid, s in graph.sections.items() if not s.is_implicit}
    assert placed == real
    assert layout["rows"] == len(layout["sections_by_row"])
    assert layout["folded"] is (layout["rows"] > 1)


@pytest.mark.parametrize("fixture", FIXTURES)
def test_json_round_trips(fixture: str) -> None:
    info = build_info(_graph(fixture))
    assert json.loads(format_info_json(info)) == info


@pytest.mark.parametrize("fixture", FIXTURES)
def test_default_text_is_a_prefix_of_verbose(fixture: str) -> None:
    """Verbose output extends, rather than rewrites, the stable summary."""
    info = build_info(_graph(fixture))
    plain = format_info_text(info, verbose=False)
    verbose = format_info_text(info, verbose=True)
    assert verbose.startswith(plain)
    assert "section_dag:" not in plain
    assert "section_dag:" in verbose


# --- to_yaml scalar/key quoting ---

#: Strings a naive quoting rule could get wrong: booleans, null, YAML 1.1's
#: hex/octal/binary integers and dotted infinity/nan, dates, and sexagesimals.
_ADVERSARIAL_SCALARS = [
    "true",
    "True",
    "false",
    "null",
    "yes",
    "no",
    "on",
    "off",
    "0x10",
    "0b101",
    ".inf",
    "-.inf",
    ".nan",
    "123",
    "1_000",
    "2025-01-02",
    "1:30",
    "a:b",
    "#fragment",
    " leading space",
    "trailing space ",
    "",
    "=",  # YAML's bare "value" tag
    "<<",  # YAML's bare "merge" tag
]


@pytest.mark.parametrize("text", _ADVERSARIAL_SCALARS)
def test_yaml_scalar_round_trips_through_a_real_parser(text: str) -> None:
    """A value that reads like a YAML type still round-trips as the string."""
    yaml = pytest.importorskip("yaml")
    from nf_metro.introspect import to_yaml

    lines = to_yaml({"key": text})
    assert yaml.safe_load("\n".join(lines)) == {"key": text}


@pytest.mark.parametrize("text", _ADVERSARIAL_SCALARS)
def test_yaml_key_round_trips_through_a_real_parser(text: str) -> None:
    """A mapping key that reads like a YAML type still round-trips as the string.

    Line and section display names become mapping keys in ``info --verbose``'s
    ``routes:`` block, and are author-supplied, so a line named e.g. ``true``
    or ``123`` must not change type when parsed back.
    """
    yaml = pytest.importorskip("yaml")
    from nf_metro.introspect import to_yaml

    if text == "":
        pytest.skip("an empty key cannot appear as a line/section name")
    lines = to_yaml({text: "value"})
    assert yaml.safe_load("\n".join(lines)) == {text: "value"}


@pytest.mark.parametrize("text", ["1__000", "07_", "0__0", "1___000", "10_"])
def test_yaml_scalar_rejects_underscore_forms_float_would_accept(text: str) -> None:
    """A digit run with underscores in unusual places is still a YAML int.

    PyYAML's int resolver (``[0-9_]*``, no digit-adjacency rule) reads
    ``"1__000"`` as 1000 even though Python's ``float()`` rejects that
    literal, so a value like this must still come back quoted.
    """
    yaml = pytest.importorskip("yaml")
    from nf_metro.introspect import to_yaml

    lines = to_yaml({"key": text})
    assert yaml.safe_load("\n".join(lines)) == {"key": text}


def test_to_yaml_distinguishes_empty_dict_from_empty_list() -> None:
    """An empty dict and an empty list must not collapse to the same line.

    ``info --verbose``'s ``layout.sections_by_row`` is a dict that is empty
    for a section-less (flat) graph; it must round-trip as ``{}``, not
    silently become a list.
    """
    yaml = pytest.importorskip("yaml")
    from nf_metro.introspect import to_yaml

    document = {"a_dict": {}, "a_list": []}
    assert yaml.safe_load("\n".join(to_yaml(document))) == document


def test_to_yaml_handles_a_list_holding_an_empty_collection() -> None:
    """A list item that is itself an empty dict/list must not crash the emitter."""
    yaml = pytest.importorskip("yaml")
    from nf_metro.introspect import to_yaml

    document = {"items": [{}, [], "x"]}
    assert yaml.safe_load("\n".join(to_yaml(document))) == document


@pytest.mark.parametrize(
    "text",
    [
        "Step 1: \U0001f389 Party",  # astral emoji forced to quote by the colon
        "\U0001f600 alone",  # astral emoji, no other quote trigger
        "\U0001d400 math bold A",  # a different astral plane
    ],
)
def test_yaml_scalar_preserves_astral_characters(text: str) -> None:
    """An astral character (outside the BMP), e.g. an emoji, must round-trip whole.

    A naive ASCII-escaping encoder splits such a character into a UTF-16
    surrogate pair of ``\\uXXXX`` escapes; some parsers recombine the pair,
    but YAML's double-quoted scalar syntax does not, so it must be written
    as the literal character instead.
    """
    yaml = pytest.importorskip("yaml")
    from nf_metro.introspect import to_yaml

    lines = to_yaml({"key": text})
    assert yaml.safe_load("\n".join(lines)) == {"key": text}


@pytest.mark.parametrize(
    "char",
    [chr(c) for c in [0x00, 0x07, 0x0B, 0x1F, 0x7F, 0x85, 0x9F, 0x2028, 0x2029]],
)
def test_yaml_scalar_escapes_characters_yaml_forbids_or_folds(char: str) -> None:
    """A control character, C1 character, or line/paragraph separator must survive.

    YAML rejects most of these unescaped even inside a quoted scalar, and
    silently folds a couple of them (e.g. NEL, U+0085) to a plain space
    instead of erroring - either way the original character is lost unless
    it is escaped.
    """
    yaml = pytest.importorskip("yaml")
    from nf_metro.introspect import to_yaml

    text = f"a{char}b"
    lines = to_yaml({"key": text})
    assert yaml.safe_load("\n".join(lines)) == {"key": text}
