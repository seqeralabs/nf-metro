"""Tests for the Mermaid + metro directive parser."""

from pathlib import Path

import pytest

from nf_metro.parser.mermaid import parse_metro_mermaid

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_title():
    text = "%%metro title: My Pipeline\ngraph LR\n"
    graph = parse_metro_mermaid(text)
    assert graph.title == "My Pipeline"


def test_parse_style():
    text = "%%metro style: light\ngraph LR\n"
    graph = parse_metro_mermaid(text)
    assert graph.style == "light"


def test_parse_line_order():
    text = "%%metro line_order: span\ngraph LR\n"
    graph = parse_metro_mermaid(text)
    assert graph.line_order == "span"


def test_parse_line_order_default():
    text = "graph LR\n"
    graph = parse_metro_mermaid(text)
    assert graph.line_order == "definition"


def test_parse_label_angle():
    text = "%%metro label_angle: 45\ngraph LR\n"
    graph = parse_metro_mermaid(text)
    assert graph.label_angle == 45.0


def test_parse_label_angle_default_none():
    text = "graph LR\n"
    graph = parse_metro_mermaid(text)
    assert graph.label_angle is None


def test_parse_caption():
    text = "%%metro caption: Example attribution text\ngraph LR\n"
    graph = parse_metro_mermaid(text)
    assert graph.caption == "Example attribution text"


def test_parse_caption_default_empty():
    text = "graph LR\n"
    graph = parse_metro_mermaid(text)
    assert graph.caption == ""


def test_parse_label_angle_invalid_ignored():
    text = "%%metro label_angle: sideways\ngraph LR\n"
    graph = parse_metro_mermaid(text)
    assert graph.label_angle is None


def test_parse_line_order_invalid_ignored():
    text = "%%metro line_order: invalid\ngraph LR\n"
    graph = parse_metro_mermaid(text)
    assert graph.line_order == "definition"


def test_center_ports_directive_default():
    text = "graph LR\n"
    graph = parse_metro_mermaid(text)
    assert graph.center_ports is False


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "yes", "1"])
def test_center_ports_directive_parsed_truthy(value):
    text = f"%%metro center_ports: {value}\ngraph LR\n"
    graph = parse_metro_mermaid(text)
    assert graph.center_ports is True


@pytest.mark.parametrize("value", ["false", "False", "no", "0", "anything"])
def test_center_ports_directive_parsed_falsy(value):
    text = f"%%metro center_ports: {value}\ngraph LR\n"
    graph = parse_metro_mermaid(text)
    assert graph.center_ports is False


def test_parse_lines():
    text = (
        "%%metro line: main | Main Line | #ff0000\n"
        "%%metro line: alt | Alt Line | #0000ff\n"
        "graph LR\n"
    )
    graph = parse_metro_mermaid(text)
    assert len(graph.lines) == 2
    assert graph.lines["main"].display_name == "Main Line"
    assert graph.lines["main"].color == "#ff0000"
    assert graph.lines["alt"].color == "#0000ff"


def test_parse_line_style_dashed():
    text = "%%metro line: opt | Optional | #aaaaaa | dashed\ngraph LR\n"
    graph = parse_metro_mermaid(text)
    assert graph.lines["opt"].style == "dashed"


def test_parse_line_style_dotted():
    text = "%%metro line: opt | Optional | #aaaaaa | dotted\ngraph LR\n"
    graph = parse_metro_mermaid(text)
    assert graph.lines["opt"].style == "dotted"


def test_parse_line_style_default_solid():
    text = "%%metro line: main | Main Line | #ff0000\ngraph LR\n"
    graph = parse_metro_mermaid(text)
    assert graph.lines["main"].style == "solid"


def test_parse_line_style_invalid_ignored():
    text = "%%metro line: main | Main Line | #ff0000 | wavy\ngraph LR\n"
    graph = parse_metro_mermaid(text)
    assert graph.lines["main"].style == "solid"


def test_parse_nodes_square_bracket():
    text = "graph LR\n    fastqc[FastQC]\n"
    graph = parse_metro_mermaid(text)
    assert "fastqc" in graph.stations
    assert graph.stations["fastqc"].label == "FastQC"


def test_parse_nodes_bare():
    text = "graph LR\n    mynode\n"
    graph = parse_metro_mermaid(text)
    assert "mynode" in graph.stations
    assert graph.stations["mynode"].label == "mynode"


def test_parse_multiline_label():
    """Literal \\n in a label becomes a real newline."""
    text = r"graph LR" + "\n" + r"    clipper[Porechop ABI \n Porechop]" + "\n"
    graph = parse_metro_mermaid(text)
    assert graph.stations["clipper"].label == "Porechop ABI\nPorechop"


def test_parse_multiline_label_multiple_breaks():
    r"""Multiple \\n sequences each produce a line break."""
    text = r"graph LR" + "\n" + r"    node[A \n B \n C]" + "\n"
    graph = parse_metro_mermaid(text)
    assert graph.stations["node"].label == "A\nB\nC"


def test_parse_quoted_label_strips_quotes():
    """Double-quoted labels (Mermaid escaping for parens) lose the quotes."""
    text = 'graph LR\n    liftover["Liftover (Picard, UCSC)"]\n'
    graph = parse_metro_mermaid(text)
    assert graph.stations["liftover"].label == "Liftover (Picard, UCSC)"


def test_parse_quoted_label_with_newline():
    r"""A quoted label keeps \\n handling after the quotes are stripped."""
    text = r"graph LR" + "\n" + r'    liftover["Liftover \n (Picard, UCSC)"]' + "\n"
    graph = parse_metro_mermaid(text)
    assert graph.stations["liftover"].label == "Liftover\n(Picard, UCSC)"


def test_parse_quoted_subgraph_title_strips_quotes():
    """Double-quoted subgraph titles render without the surrounding quotes."""
    text = (
        "graph LR\n"
        '    subgraph prep ["Preprocessing (Optional)"]\n'
        "        a[A]\n"
        "    end\n"
    )
    graph = parse_metro_mermaid(text)
    assert graph.sections["prep"].name == "Preprocessing (Optional)"


def test_parse_edges():
    text = "graph LR\n    a[Input]\n    b[Output]\n    a -->|main| b\n"
    graph = parse_metro_mermaid(text)
    assert len(graph.edges) == 1
    assert graph.edges[0].source == "a"
    assert graph.edges[0].target == "b"
    assert graph.edges[0].line_id == "main"


def test_parse_edges_no_label():
    """Unannotated edges raise a clear error."""
    text = "graph LR\n    a --> b\n"
    with pytest.raises(ValueError, match="no metro line annotation"):
        parse_metro_mermaid(text)


def test_station_lines():
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro line: alt | Alt | #0000ff\n"
        "graph LR\n"
        "    a -->|main| b\n"
        "    a -->|alt| c\n"
    )
    graph = parse_metro_mermaid(text)
    lines = graph.station_lines("a")
    assert "main" in lines
    assert "alt" in lines


def test_line_stations():
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    a -->|main| b\n"
        "    b -->|main| c\n"
    )
    graph = parse_metro_mermaid(text)
    stations = graph.line_stations("main")
    assert stations == ["a", "b", "c"]


def test_parse_simple_fixture():
    text = (FIXTURES / "rnaseq_simple.mmd").read_text()
    graph = parse_metro_mermaid(text)
    assert graph.title == "Test Pipeline"
    assert len(graph.stations) == 4
    assert len(graph.edges) == 4
    assert len(graph.lines) == 2


def test_ignores_comments():
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "%% This is a regular comment\n"
        "%%metro title: Test\n"
        "graph LR\n"
        "    a -->|main| b\n"
    )
    graph = parse_metro_mermaid(text)
    assert graph.title == "Test"
    assert len(graph.edges) == 1


def test_parse_off_track_single():
    """%%metro off_track: marks a single station as off-track."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro off_track: samples_in\n"
        "graph LR\n"
        "    samples_in[Samples]\n"
        "    samples_in -->|main| validator\n"
        "    validator[Validate]\n"
    )
    graph = parse_metro_mermaid(text)
    assert graph.stations["samples_in"].off_track is True
    assert graph.stations["validator"].off_track is False


def test_parse_off_track_multiple():
    """%%metro off_track: accepts a comma-separated list."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro off_track: a, b, c\n"
        "graph LR\n"
        "    a -->|main| d\n"
        "    b -->|main| d\n"
        "    c -->|main| d\n"
        "    d[Sink]\n"
    )
    graph = parse_metro_mermaid(text)
    assert graph.stations["a"].off_track is True
    assert graph.stations["b"].off_track is True
    assert graph.stations["c"].off_track is True
    assert graph.stations["d"].off_track is False


def test_parse_off_track_unknown_id_ignored():
    """Unknown station ids in %%metro off_track: silently skip."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro off_track: a, missing\n"
        "graph LR\n"
        "    a[A]\n"
        "    a -->|main| b\n"
        "    b[B]\n"
    )
    graph = parse_metro_mermaid(text)
    assert graph.stations["a"].off_track is True
    assert "missing" not in graph.stations


# --- Subgraph (first-class section) tests ---


def test_parse_subgraph_basic():
    """Subgraphs create first-class sections."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph sec1 [Section One]\n"
        "        a[Input]\n"
        "        b[Middle]\n"
        "        a -->|main| b\n"
        "    end\n"
        "    subgraph sec2 [Section Two]\n"
        "        c[Output]\n"
        "    end\n"
        "    b -->|main| c\n"
    )
    graph = parse_metro_mermaid(text)
    assert len(graph.sections) == 2
    assert "sec1" in graph.sections
    assert "sec2" in graph.sections
    assert graph.sections["sec1"].name == "Section One"
    assert graph.sections["sec2"].name == "Section Two"


def test_subgraph_station_membership():
    """Stations inside subgraphs get section_id set."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph sec1 [Section One]\n"
        "        a[Input]\n"
        "        b[Middle]\n"
        "        a -->|main| b\n"
        "    end\n"
        "    subgraph sec2 [Section Two]\n"
        "        c[Output]\n"
        "    end\n"
        "    b -->|main| c\n"
    )
    graph = parse_metro_mermaid(text)
    assert graph.stations["a"].section_id == "sec1"
    assert graph.stations["b"].section_id == "sec1"
    assert graph.stations["c"].section_id == "sec2"


def test_subgraph_section_station_ids():
    """Section.station_ids lists the stations in the section."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph sec1 [Section One]\n"
        "        a[Input]\n"
        "        b[Middle]\n"
        "        a -->|main| b\n"
        "    end\n"
        "    subgraph sec2 [Section Two]\n"
        "        c[Output]\n"
        "    end\n"
        "    b -->|main| c\n"
    )
    graph = parse_metro_mermaid(text)
    # sec1 has a, b plus port stations
    real_stations_sec1 = [
        s for s in graph.sections["sec1"].station_ids if not graph.stations[s].is_port
    ]
    assert set(real_stations_sec1) == {"a", "b"}
    real_stations_sec2 = [
        s for s in graph.sections["sec2"].station_ids if not graph.stations[s].is_port
    ]
    assert set(real_stations_sec2) == {"c"}


def test_inter_section_edge_rewriting():
    """Inter-section edges are rewritten into 3-part chains with ports."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph sec1 [Section One]\n"
        "        a[Input]\n"
        "        b[Middle]\n"
        "        a -->|main| b\n"
        "    end\n"
        "    subgraph sec2 [Section Two]\n"
        "        c[Output]\n"
        "    end\n"
        "    b -->|main| c\n"
    )
    graph = parse_metro_mermaid(text)
    # Should have ports
    assert len(graph.ports) > 0
    # Original direct b->c edge should be gone,
    # replaced by b->exit, exit->entry, entry->c
    direct_edges = [e for e in graph.edges if e.source == "b" and e.target == "c"]
    assert len(direct_edges) == 0
    # Should have edges from b to an exit port
    b_to_port = [
        e for e in graph.edges if e.source == "b" and graph.stations[e.target].is_port
    ]
    assert len(b_to_port) >= 1


def test_port_directive_parsing():
    """%%metro entry/exit directives inside subgraphs create ports."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro line: alt | Alt | #0000ff\n"
        "graph LR\n"
        "    subgraph sec1 [Section One]\n"
        "        %%metro exit: right | main, alt\n"
        "        a[Input]\n"
        "    end\n"
        "    subgraph sec2 [Section Two]\n"
        "        %%metro entry: left | main, alt\n"
        "        b[Output]\n"
        "    end\n"
        "    a -->|main| b\n"
        "    a -->|alt| b\n"
    )
    graph = parse_metro_mermaid(text)
    # Should have explicit ports from directives plus auto-created ports
    exit_ports = [
        p for p in graph.ports.values() if not p.is_entry and p.section_id == "sec1"
    ]
    entry_ports = [
        p for p in graph.ports.values() if p.is_entry and p.section_id == "sec2"
    ]
    assert len(exit_ports) >= 1
    assert len(entry_ports) >= 1


def test_grid_directive_parsing():
    """%%metro grid: directives set grid overrides."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro grid: sec2 | 1,0\n"
        "%%metro grid: sec3 | 1,1\n"
        "graph LR\n"
        "    a -->|main| b\n"
    )
    graph = parse_metro_mermaid(text)
    assert graph.grid_overrides["sec2"] == (1, 0, 1, 1)
    assert graph.grid_overrides["sec3"] == (1, 1, 1, 1)


def test_section_numbering():
    """Sections are auto-numbered in definition order."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph first [First]\n"
        "        a[A]\n"
        "    end\n"
        "    subgraph second [Second]\n"
        "        b[B]\n"
        "    end\n"
        "    subgraph third [Third]\n"
        "        c[C]\n"
        "    end\n"
        "    a -->|main| b\n"
        "    b -->|main| c\n"
    )
    graph = parse_metro_mermaid(text)
    assert graph.sections["first"].number == 1
    assert graph.sections["second"].number == 2
    assert graph.sections["third"].number == 3


def test_subgraph_without_display_name():
    """Subgraph without [display name] uses the id as name."""
    text = "graph LR\n    subgraph mysection\n        a[A]\n    end\n"
    graph = parse_metro_mermaid(text)
    assert "mysection" in graph.sections
    assert graph.sections["mysection"].name == "mysection"


def test_empty_section_removed():
    """Subgraphs with only edges (no node definitions) are removed.

    Regression test for https://github.com/pinin4fjords/nf-metro/issues/51.
    When nodes are defined outside a subgraph but edges referencing them
    appear inside the subgraph, the section has no stations. The parser
    should remove it and fall back to flat layout instead of crashing.
    """
    text = (
        "%%metro line: dna | DNA | #004b86\n"
        "graph LR\n"
        "    cat[cat]\n"
        "    kraken2[Kraken2]\n"
        "    centrifuge[centrifuge]\n"
        "    subgraph blah\n"
        "        cat -->|dna| kraken2\n"
        "        cat -->|dna| centrifuge\n"
        "    end\n"
    )
    import warnings

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        graph = parse_metro_mermaid(text)
        assert len(w) == 1
        assert "blah" in str(w[0].message)
        assert "no node definitions" in str(w[0].message)

    # Empty section should be removed
    assert "blah" not in graph.sections
    assert len(graph.sections) == 0

    # All stations should still exist and be unsectioned
    assert "cat" in graph.stations
    assert "kraken2" in graph.stations
    assert "centrifuge" in graph.stations
    assert all(s.section_id is None for s in graph.stations.values())

    # Edges should still exist
    assert len(graph.edges) == 2


def test_empty_section_removed_render():
    """An empty-section graph can be rendered without error.

    End-to-end regression test: ensure the full parse -> layout -> render
    pipeline doesn't crash.
    """
    from nf_metro.layout.engine import compute_layout
    from nf_metro.render.svg import render_svg
    from nf_metro.themes import NFCORE_THEME

    text = (
        "%%metro line: dna | DNA | #004b86\n"
        "%%metro line: aa | AA | #d9aa00\n"
        "graph LR\n"
        "    cat[cat]\n"
        "    kraken2[Kraken2]\n"
        "    seqkit[SeqKit]\n"
        "    subgraph blah\n"
        "        cat -->|dna| kraken2\n"
        "    end\n"
        "    cat -->|aa| seqkit\n"
    )
    import warnings

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        graph = parse_metro_mermaid(text)

    compute_layout(graph)
    svg_str = render_svg(graph, NFCORE_THEME)

    # All station labels should appear in the SVG output
    assert "cat" in svg_str
    assert "Kraken2" in svg_str
    assert "SeqKit" in svg_str


# --- Hidden station tests ---


def test_hidden_station_underscore_prefix():
    """Stations with _ prefix are marked as hidden."""
    text = "graph LR\n    _hidden[Split Point]\n    visible[Visible]\n"
    graph = parse_metro_mermaid(text)
    assert graph.stations["_hidden"].is_hidden is True
    assert graph.stations["visible"].is_hidden is False


def test_hidden_station_auto_created_from_edge():
    """Stations with _ prefix are hidden even when auto-created from edges."""
    text = "graph LR\n    a -->|main| _split\n    _split -->|main| b\n"
    graph = parse_metro_mermaid(text)
    assert graph.stations["_split"].is_hidden is True
    assert graph.stations["a"].is_hidden is False
    assert graph.stations["b"].is_hidden is False


def test_hidden_station_edge_before_definition():
    """Hidden flag is set correctly when edge precedes node definition."""
    text = (
        "graph LR\n    a -->|main| _split\n    _split[Split]\n    _split -->|main| b\n"
    )
    graph = parse_metro_mermaid(text)
    assert graph.stations["_split"].is_hidden is True
    assert graph.stations["_split"].label == "Split"


def test_hidden_station_definition_before_edge():
    """Hidden flag is set correctly when node definition precedes edge."""
    text = (
        "graph LR\n    _split[Split]\n    a -->|main| _split\n    _split -->|main| b\n"
    )
    graph = parse_metro_mermaid(text)
    assert graph.stations["_split"].is_hidden is True


def test_hidden_station_in_section():
    """Hidden stations work inside sections."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro line: alt | Alt | #0000ff\n"
        "graph LR\n"
        "    subgraph sec1 [Section One]\n"
        "        a[Input]\n"
        "        _branch\n"
        "        a -->|main,alt| _branch\n"
        "        _branch -->|main| b[Output A]\n"
        "        _branch -->|alt| c[Output B]\n"
        "    end\n"
    )
    graph = parse_metro_mermaid(text)
    assert graph.stations["_branch"].is_hidden is True
    assert graph.stations["_branch"].section_id == "sec1"


# --- Edge validation tests ---


def test_unannotated_edge_error_message_includes_stations():
    """Error message lists the offending edge source and target."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph sec1 [Section]\n"
        "        fastp[fastp]\n"
        "        falco[falco]\n"
        "        fastp --> falco\n"
        "    end\n"
    )
    with pytest.raises(ValueError, match="fastp --> falco"):
        parse_metro_mermaid(text)


def test_unannotated_edge_multiple():
    """Multiple unannotated edges are all listed in the error."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph sec1 [Section]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        c[C]\n"
        "        a --> b\n"
        "        b --> c\n"
        "    end\n"
    )
    with pytest.raises(ValueError, match="a --> b") as exc_info:
        parse_metro_mermaid(text)
    assert "b --> c" in str(exc_info.value)


def test_undeclared_line_error():
    """Edges referencing undeclared lines raise a clear error."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph sec1 [Section]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        a -->|oops| b\n"
        "    end\n"
    )
    with pytest.raises(ValueError, match="undeclared metro lines.*oops"):
        parse_metro_mermaid(text)


def test_no_edges_no_error():
    """Graph with no edges passes validation."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph sec1 [Section]\n"
        "        a[A]\n"
        "    end\n"
    )
    graph = parse_metro_mermaid(text)
    assert "a" in graph.stations


def test_unannotated_edge_without_sections():
    """Unannotated edges outside sections also raise error."""
    text = "graph LR\n    a --> b\n    b --> c\n"
    with pytest.raises(ValueError, match="no metro line annotation"):
        parse_metro_mermaid(text)


# --- File icon (terminus) parsing ---


def test_parse_single_file_icon():
    """A single %%metro file: directive produces one terminus label."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro file: reads_in | FASTQ\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        reads_in[ ]\n"
        "        trim[Trim]\n"
        "        reads_in -->|main| trim\n"
        "    end\n"
    )
    graph = parse_metro_mermaid(text)
    st = graph.stations["reads_in"]
    assert st.is_terminus
    assert st.terminus_labels == ["FASTQ"]


def test_parse_multiple_file_icons_comma():
    """Comma-separated labels in one directive produce multiple terminus labels."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro file: reads_in | FASTQ, BAM\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        reads_in[ ]\n"
        "        trim[Trim]\n"
        "        reads_in -->|main| trim\n"
        "    end\n"
    )
    graph = parse_metro_mermaid(text)
    st = graph.stations["reads_in"]
    assert st.is_terminus
    assert st.terminus_labels == ["FASTQ", "BAM"]


def test_parse_multiple_file_directives_same_station():
    """Multiple %%metro file: directives for the same station accumulate."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro file: reads_in | FASTQ\n"
        "%%metro file: reads_in | BAM\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        reads_in[ ]\n"
        "        trim[Trim]\n"
        "        reads_in -->|main| trim\n"
        "    end\n"
    )
    graph = parse_metro_mermaid(text)
    st = graph.stations["reads_in"]
    assert st.is_terminus
    assert st.terminus_labels == ["FASTQ", "BAM"]


def test_parse_no_file_icon_not_terminus():
    """Station without a %%metro file: directive is not a terminus."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        a[A]\n"
        "        b[B]\n"
        "        a -->|main| b\n"
        "    end\n"
    )
    graph = parse_metro_mermaid(text)
    assert not graph.stations["a"].is_terminus
    assert graph.stations["a"].terminus_labels == []


def test_parse_files_directive():
    """%%metro files: directive produces terminus with 'files' icon type."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro files: reads_in | FASTQ\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        reads_in[ ]\n"
        "        trim[Trim]\n"
        "        reads_in -->|main| trim\n"
        "    end\n"
    )
    graph = parse_metro_mermaid(text)
    st = graph.stations["reads_in"]
    assert st.is_terminus
    assert st.terminus_labels == ["FASTQ"]
    assert st.terminus_icon_types == ["files"]


def test_parse_dir_directive():
    """%%metro dir: directive produces terminus with 'dir' icon type."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro dir: output | Results\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        trim[Trim]\n"
        "        output[ ]\n"
        "        trim -->|main| output\n"
        "    end\n"
    )
    graph = parse_metro_mermaid(text)
    st = graph.stations["output"]
    assert st.is_terminus
    assert st.terminus_labels == ["Results"]
    assert st.terminus_icon_types == ["dir"]


def test_parse_mixed_icon_types():
    """Different directives on different stations produce correct types."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro file: src | FASTQ\n"
        "%%metro files: paired | FASTQ\n"
        "%%metro dir: out | Results\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        src[ ]\n"
        "        paired[ ]\n"
        "        step[Step]\n"
        "        out[ ]\n"
        "        src -->|main| step\n"
        "        paired -->|main| step\n"
        "        step -->|main| out\n"
        "    end\n"
    )
    graph = parse_metro_mermaid(text)
    assert graph.stations["src"].terminus_icon_types == ["file"]
    assert graph.stations["paired"].terminus_icon_types == ["files"]
    assert graph.stations["out"].terminus_icon_types == ["dir"]


def test_parse_files_comma_separated():
    """Comma-separated labels in %%metro files: share the same icon type."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro files: reads_in | FASTQ, BAM\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        reads_in[ ]\n"
        "        trim[Trim]\n"
        "        reads_in -->|main| trim\n"
        "    end\n"
    )
    graph = parse_metro_mermaid(text)
    st = graph.stations["reads_in"]
    assert st.terminus_labels == ["FASTQ", "BAM"]
    assert st.terminus_icon_types == ["files", "files"]


def test_parse_file_icon_type_default():
    """%%metro file: directive defaults to 'file' icon type."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro file: reads_in | FASTQ\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        reads_in[ ]\n"
        "        trim[Trim]\n"
        "        reads_in -->|main| trim\n"
        "    end\n"
    )
    graph = parse_metro_mermaid(text)
    assert graph.stations["reads_in"].terminus_icon_types == ["file"]


def test_parse_file_icon_with_name():
    """Optional third field on %%metro file: sets a caption name."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro file: reads_in | CSV | Samples\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        reads_in[ ]\n"
        "        trim[Trim]\n"
        "        reads_in -->|main| trim\n"
        "    end\n"
    )
    graph = parse_metro_mermaid(text)
    st = graph.stations["reads_in"]
    assert st.terminus_labels == ["CSV"]
    assert st.terminus_names == ["Samples"]


def test_parse_file_icon_without_name_empty():
    """Without the optional name field, terminus_names is empty string."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro file: reads_in | CSV\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        reads_in[ ]\n"
        "        trim[Trim]\n"
        "        reads_in -->|main| trim\n"
        "    end\n"
    )
    graph = parse_metro_mermaid(text)
    assert graph.stations["reads_in"].terminus_names == [""]


def test_parse_file_icon_name_applies_to_all_comma_labels():
    """The optional name applies to every comma-separated label."""
    text = (
        "%%metro line: main | Main | #ff0000\n"
        "%%metro file: reads_in | FASTQ, BAM | Reads\n"
        "graph LR\n"
        "    subgraph sec [Section]\n"
        "        reads_in[ ]\n"
        "        trim[Trim]\n"
        "        reads_in -->|main| trim\n"
        "    end\n"
    )
    graph = parse_metro_mermaid(text)
    st = graph.stations["reads_in"]
    assert st.terminus_labels == ["FASTQ", "BAM"]
    assert st.terminus_names == ["Reads", "Reads"]


def test_parse_legend_min_height():
    text = "%%metro legend_min_height: 72\ngraph LR\n"
    graph = parse_metro_mermaid(text)
    assert graph.legend_min_height == 72.0


def test_parse_legend_min_height_default():
    text = "graph LR\n"
    graph = parse_metro_mermaid(text)
    assert graph.legend_min_height == 0.0


def test_parse_legend_min_height_invalid_ignored():
    text = "%%metro legend_min_height: abc\ngraph LR\n"
    graph = parse_metro_mermaid(text)
    assert graph.legend_min_height == 0.0


def test_parse_legend_keyword_defaults():
    graph = parse_metro_mermaid("%%metro legend: br\ngraph LR\n")
    assert graph.legend_position == "br"
    assert graph.legend_anchor == "content"
    assert graph.legend_offset is None
    assert graph.legend_at is None


def test_parse_legend_canvas_anchor():
    graph = parse_metro_mermaid("%%metro legend: br | canvas\ngraph LR\n")
    assert graph.legend_position == "br"
    assert graph.legend_anchor == "canvas"


def test_parse_legend_offset():
    graph = parse_metro_mermaid("%%metro legend: br | +0,120\ngraph LR\n")
    assert graph.legend_position == "br"
    assert graph.legend_offset == (0.0, 120.0)


def test_parse_legend_offset_negative():
    graph = parse_metro_mermaid("%%metro legend: tr | -10,-20\ngraph LR\n")
    assert graph.legend_offset == (-10.0, -20.0)


def test_parse_legend_absolute_coordinates():
    graph = parse_metro_mermaid("%%metro legend: 40,560\ngraph LR\n")
    assert graph.legend_position == "free"
    assert graph.legend_at == (40.0, 560.0)


def test_parse_legend_unknown_keyword_warns_and_ignores():
    with pytest.warns(UserWarning):
        graph = parse_metro_mermaid("%%metro legend: middle\ngraph LR\n")
    assert graph.legend_position == "bottom"  # default unchanged


def test_parse_legend_unknown_qualifier_warns():
    with pytest.warns(UserWarning):
        graph = parse_metro_mermaid("%%metro legend: br | sideways\ngraph LR\n")
    assert graph.legend_position == "br"
    assert graph.legend_anchor == "content"
    assert graph.legend_offset is None


def test_parse_logo_single_path():
    graph = parse_metro_mermaid("%%metro logo: my_logo.png\ngraph LR\n")
    assert graph.logo_path == "my_logo.png"
    assert graph.logo_path_light == ""
    assert graph.logo_path_dark == ""


def test_parse_logo_pipe_syntax():
    graph = parse_metro_mermaid(
        "%%metro logo: light_logo.png | dark_logo.png\ngraph LR\n"
    )
    assert graph.logo_path == ""
    assert graph.logo_path_light == "light_logo.png"
    assert graph.logo_path_dark == "dark_logo.png"


def test_parse_logo_pipe_syntax_strips_whitespace():
    graph = parse_metro_mermaid("%%metro logo:  light.png |  dark.png \ngraph LR\n")
    assert graph.logo_path_light == "light.png"
    assert graph.logo_path_dark == "dark.png"


def test_parse_logo_trailing_pipe_light_only():
    graph = parse_metro_mermaid("%%metro logo: light.png |\ngraph LR\n")
    assert graph.logo_path == ""
    assert graph.logo_path_light == "light.png"
    assert graph.logo_path_dark == ""


def test_parse_logo_leading_pipe_dark_only():
    graph = parse_metro_mermaid("%%metro logo: | dark.png\ngraph LR\n")
    assert graph.logo_path == ""
    assert graph.logo_path_light == ""
    assert graph.logo_path_dark == "dark.png"


def test_parse_logo_defaults():
    graph = parse_metro_mermaid("graph LR\n")
    assert graph.logo_path == ""
    assert graph.logo_path_light == ""
    assert graph.logo_path_dark == ""


def test_parse_logo_scale():
    graph = parse_metro_mermaid("%%metro logo_scale: 1.5\ngraph LR\n")
    assert graph.logo_scale == 1.5


def test_parse_logo_scale_default():
    graph = parse_metro_mermaid("graph LR\n")
    assert graph.logo_scale == 1.0


def test_parse_logo_scale_invalid_ignored():
    with pytest.warns(UserWarning):
        graph = parse_metro_mermaid("%%metro logo_scale: huge\ngraph LR\n")
    assert graph.logo_scale == 1.0


def test_parse_logo_scale_nonpositive_ignored():
    with pytest.warns(UserWarning):
        graph = parse_metro_mermaid("%%metro logo_scale: 0\ngraph LR\n")
    assert graph.logo_scale == 1.0


def test_parse_font_scale():
    graph = parse_metro_mermaid("%%metro font_scale: 1.4\ngraph LR\n")
    assert graph.font_scale == 1.4


def test_parse_font_scale_default():
    graph = parse_metro_mermaid("graph LR\n")
    assert graph.font_scale == 1.0


def test_parse_font_scale_invalid_ignored():
    with pytest.warns(UserWarning):
        graph = parse_metro_mermaid("%%metro font_scale: huge\ngraph LR\n")
    assert graph.font_scale == 1.0


def test_parse_font_scale_nonpositive_ignored():
    with pytest.warns(UserWarning):
        graph = parse_metro_mermaid("%%metro font_scale: 0\ngraph LR\n")
    assert graph.font_scale == 1.0


def test_parse_legend_logo_gap():
    graph = parse_metro_mermaid("%%metro legend_logo_gap: 40\ngraph LR\n")
    assert graph.legend_logo_gap == 40.0


def test_parse_legend_logo_gap_default():
    graph = parse_metro_mermaid("graph LR\n")
    assert graph.legend_logo_gap is None


def test_parse_legend_logo_gap_invalid_ignored():
    with pytest.warns(UserWarning):
        graph = parse_metro_mermaid("%%metro legend_logo_gap: wide\ngraph LR\n")
    assert graph.legend_logo_gap is None


def test_parse_legend_logo_gap_negative_ignored():
    with pytest.warns(UserWarning):
        graph = parse_metro_mermaid("%%metro legend_logo_gap: -5\ngraph LR\n")
    assert graph.legend_logo_gap is None


def test_no_duplicate_edges_after_resolve_sections():
    """Multiple inter-section edges to the same section should not create
    duplicate (source, target, line_id) triples after _resolve_sections."""
    text = (
        "%%metro line: asm | Assembly | #0570b0\n"
        "graph LR\n"
        "    subgraph sec1 [Source]\n"
        "        %%metro exit: right | asm\n"
        "        a[A]\n"
        "    end\n"
        "    subgraph sec2 [Target]\n"
        "        %%metro entry: left | asm\n"
        "        b[B]\n"
        "        c[C]\n"
        "    end\n"
        "    a -->|asm| b\n"
        "    a -->|asm| c\n"
    )
    graph = parse_metro_mermaid(text)
    edge_keys = [(e.source, e.target, e.line_id) for e in graph.edges]
    assert len(edge_keys) == len(set(edge_keys)), (
        f"Duplicate edges found: {[k for k in edge_keys if edge_keys.count(k) > 1]}"
    )


def test_parse_group_directive():
    text = (
        "%%metro group: SNPs & Indels | a, b, c\n"
        "%%metro group: SV & CNV | d, e | above\n"
        "graph LR\n"
    )
    graph = parse_metro_mermaid(text)
    assert len(graph.groups) == 2
    g0, g1 = graph.groups
    assert g0.label == "SNPs & Indels"
    assert g0.station_ids == ["a", "b", "c"]
    assert g0.position == "below"
    assert g1.label == "SV & CNV"
    assert g1.station_ids == ["d", "e"]
    assert g1.position == "above"


def test_parse_group_directive_quoted_label():
    text = '%%metro group: "Callers (somatic)" | x\ngraph LR\n'
    graph = parse_metro_mermaid(text)
    assert len(graph.groups) == 1
    assert graph.groups[0].label == "Callers (somatic)"


def test_parse_group_directive_invalid_ignored():
    text = "%%metro group: NoStations\ngraph LR\n"
    with pytest.warns(UserWarning):
        graph = parse_metro_mermaid(text)
    assert graph.groups == []


def test_parse_group_directive_bad_position_defaults_below():
    text = "%%metro group: G | a | sideways\ngraph LR\n"
    with pytest.warns(UserWarning):
        graph = parse_metro_mermaid(text)
    assert graph.groups[0].position == "below"


@pytest.mark.parametrize("direction", ["TB", "TD", "BT", "RL"])
def test_non_lr_primary_direction_warns(direction):
    """A non-LR `graph` header warns that only LR primary is honoured."""
    text = (
        "%%metro line: a | A | #0570b0\n"
        f"graph {direction}\n"
        "    subgraph s1 [One]\n"
        "        x[X]\n"
        "        y[Y]\n"
        "        x -->|a| y\n"
        "    end\n"
    )
    with pytest.warns(UserWarning, match=f"'{direction}'.*ignored"):
        parse_metro_mermaid(text)


@pytest.mark.parametrize("header", ["graph LR", "graph", "graph    LR"])
def test_lr_primary_direction_does_not_warn(header, recwarn):
    """`graph LR` (or a bare `graph`) is the honoured primary; no warning."""
    parse_metro_mermaid(f"{header}\n    subgraph s1 [One]\n        x[X]\n    end\n")
    direction_warnings = [
        w for w in recwarn.list if "primary layout direction" in str(w.message)
    ]
    assert direction_warnings == []


def _wide_section_chain() -> str:
    """A linear chain of six sections (24 station-columns total), wide enough
    that the default row-wrap threshold (15) folds it onto multiple rows."""
    lines = ["%%metro line: a | A | #0570b0", "graph LR"]
    for i in range(6):
        lines.append(f"    subgraph s{i} [Section {i}]")
        for j in range(4):
            lines.append(f"        n{i}_{j}[N{i}{j}]")
        for j in range(3):
            lines.append(f"        n{i}_{j} -->|a| n{i}_{j + 1}")
        lines.append("    end")
    for i in range(5):
        lines.append(f"    n{i}_3 -->|a| n{i + 1}_0")
    return "\n".join(lines) + "\n"


def test_fold_threshold_directive_parsed():
    graph = parse_metro_mermaid("%%metro fold_threshold: 40\ngraph LR\n")
    assert graph.fold_threshold == 40


def test_fold_threshold_invalid_ignored():
    graph = parse_metro_mermaid("%%metro fold_threshold: wide\ngraph LR\n")
    assert graph.fold_threshold is None


def test_fold_threshold_keeps_wide_chain_on_one_row():
    """The default threshold wraps a wide section chain onto multiple rows; a
    high fold_threshold keeps every section on row 0."""
    default = parse_metro_mermaid(_wide_section_chain())
    rows_default = {s.grid_row for s in default.sections.values() if s.station_ids}
    assert max(rows_default) > 0, "wide chain should wrap at the default threshold"

    raised = parse_metro_mermaid(
        "%%metro fold_threshold: 100\n" + _wide_section_chain()
    )
    rows_raised = {s.grid_row for s in raised.sections.values() if s.station_ids}
    assert rows_raised == {0}, f"fold_threshold should keep one row, got {rows_raised}"


def test_max_station_columns_arg_overrides_fold_threshold():
    """An explicit caller value (the --fold-threshold CLI flag) wins over a
    fold_threshold directive, matching the CLI-overrides-directive convention."""
    graph = parse_metro_mermaid(
        "%%metro fold_threshold: 100\n" + _wide_section_chain(),
        max_station_columns=15,
    )
    rows = {s.grid_row for s in graph.sections.values() if s.station_ids}
    assert max(rows) > 0, "explicit max_station_columns=15 should force a wrap"
