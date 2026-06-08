"""Grammar-parser coverage and behaviour tests.

The Mermaid front-end is a ``lark`` grammar plus a statement-applying driver.
These tests lock the contract the grammar must honour: every committed corpus
file parses (and the Nextflow-flowchart fixtures raise), every node shape and
arrow form is recognised, directive dispatch is key-exact rather than
order-sensitive, and an unrecognised line is dropped rather than aborting the
parse.
"""

from pathlib import Path

import pytest

from nf_metro.parser.mermaid import parse_metro_mermaid

REPO = Path(__file__).parent.parent
CORPUS = sorted(
    p
    for p in REPO.glob("**/*.mmd")
    if ".claude" not in p.parts and "site" not in p.parts
)
# The Nextflow-flowchart fixtures are intentionally rejected up front.
FLOWCHART = sorted((REPO / "tests/fixtures/nextflow").glob("*.mmd"))


@pytest.mark.parametrize("path", CORPUS, ids=lambda p: p.name)
def test_every_corpus_file_parses(path):
    """The grammar covers every committed ``.mmd``; none falls through to error."""
    if path in FLOWCHART:
        with pytest.raises(ValueError):
            parse_metro_mermaid(path.read_text())
        return
    graph = parse_metro_mermaid(path.read_text())
    assert graph.stations, f"{path.name} parsed to an empty graph"


SHAPE_CASES = {
    "square": "x[Label]",
    "stadium": "x([Label])",
    "subroutine": "x[[Label]]",
    "circle": "x((Label))",
    "round": "x(Label)",
    "rhombus": "x{Label}",
}


@pytest.mark.parametrize("decl", SHAPE_CASES.values(), ids=list(SHAPE_CASES))
def test_node_shapes_extract_label(decl):
    """Every Mermaid shape yields the same id + inner label."""
    text = f"graph LR\n%%metro line: a | A | #fff\n{decl}\ny[Y]\nx -->|a| y\n"
    graph = parse_metro_mermaid(text)
    assert graph.stations["x"].label == "Label"


def test_bare_node_label_is_its_id():
    graph = parse_metro_mermaid("graph LR\n%%metro line: a | A | #fff\nx\nx -->|a| y\n")
    assert graph.stations["x"].label == "x"


@pytest.mark.parametrize("arrow", ["-->", "---", "==>"])
def test_arrow_variants_form_edges(arrow):
    text = f"graph LR\n%%metro line: a | A | #fff\nx[X]\ny[Y]\nx {arrow}|a| y\n"
    graph = parse_metro_mermaid(text)
    assert any(e.source == "x" and e.target == "y" for e in graph.edges)


def test_comma_separated_line_ids_split_into_edges():
    text = (
        "graph LR\n%%metro line: a | A | #fff\n%%metro line: b | B | #000\n"
        "x[X]\ny[Y]\nx -->|a, b| y\n"
    )
    graph = parse_metro_mermaid(text)
    lines = {e.line_id for e in graph.edges if e.source == "x"}
    assert lines == {"a", "b"}


def test_multiline_label_converts_backslash_n():
    text = 'graph LR\n%%metro line: a | A | #fff\nx["Foo\\nBar"]\ny[Y]\nx -->|a| y\n'
    graph = parse_metro_mermaid(text)
    assert graph.stations["x"].label == "Foo\nBar"


def test_quoted_label_with_parens_is_unquoted():
    text = (
        "graph LR\n%%metro line: a | A | #fff\n"
        'x["Liftover (Picard, UCSC)"]\ny[Y]\nx -->|a| y\n'
    )
    graph = parse_metro_mermaid(text)
    assert graph.stations["x"].label == "Liftover (Picard, UCSC)"


def test_underscore_node_is_hidden():
    text = "graph LR\n%%metro line: a | A | #fff\n_h[Hidden]\ny[Y]\n_h -->|a| y\n"
    graph = parse_metro_mermaid(text)
    assert graph.stations["_h"].is_hidden is True


def test_directive_dispatch_is_key_exact_not_prefix():
    """legend_combo / logo_scale are not shadowed by legend / logo.

    Key-exact dispatch must route each directive to its own handler regardless
    of declaration order, so a key that is a prefix of another does not win.
    """
    text = (
        "graph LR\n%%metro line: a | A | #fff\n%%metro line: b | B | #000\n"
        "%%metro legend_combo: a, b | Combined\n"
        "%%metro logo: /p/logo.png\n%%metro logo_scale: 2.0\n"
        "x[X]\ny[Y]\nx -->|a| y\n"
    )
    graph = parse_metro_mermaid(text)
    assert graph.legend_combos == [(("a", "b"), "Combined")]
    assert graph.logo_path == "/p/logo.png"
    assert graph.logo_scale == 2.0


def test_icon_directive_keys_recognised():
    text = (
        "graph LR\n%%metro line: a | A | #fff\n"
        "%%metro file: y | results.csv | Samples\nx[X]\ny[Y]\nx -->|a| y\n"
    )
    graph = parse_metro_mermaid(text)
    assert graph.stations["y"].terminus_labels == ["results.csv"]


def test_plain_comment_is_ignored():
    text = (
        "graph LR\n%% just a note\n%%metro line: a | A | #fff\nx[X]\ny[Y]\nx -->|a| y\n"
    )
    graph = parse_metro_mermaid(text)
    assert "x" in graph.stations


def test_unrecognised_line_is_dropped_with_warning():
    """A line matching no statement is skipped, with a warning (not silently)."""
    with pytest.warns(UserWarning, match="unrecognised line"):
        graph = parse_metro_mermaid("not a valid mermaid file")
    assert graph.stations == {}


def test_inline_shaped_edge_endpoints_declare_their_nodes():
    """An edge endpoint written with an inline shape also declares that node."""
    graph = parse_metro_mermaid(
        "graph LR\n%%metro line: a | A | #fff\nx[X] -->|a| y[Y]\n"
    )
    assert graph.stations["x"].label == "X"
    assert graph.stations["y"].label == "Y"
    assert any(
        e.source == "x" and e.target == "y" and e.line_id == "a" for e in graph.edges
    )


def test_inline_shaped_edge_mixed_with_bare_endpoint():
    """Only the shaped endpoint declares a label; the bare one keeps its id."""
    graph = parse_metro_mermaid("graph LR\n%%metro line: a | A | #fff\nx -->|a| y[Y]\n")
    assert graph.stations["x"].label == "x"
    assert graph.stations["y"].label == "Y"


def test_unknown_directive_warns():
    """An unrecognised %%metro key is ignored, with a warning."""
    text = (
        "graph LR\n%%metro frobnicate: nonsense\n%%metro line: a | A | #fff\n"
        "x[X]\ny[Y]\nx -->|a| y\n"
    )
    with pytest.warns(UserWarning, match="unknown directive"):
        graph = parse_metro_mermaid(text)
    assert "x" in graph.stations


MALFORMED_DIRECTIVES = [
    "%%metro line: onlyone",
    "%%metro line: b | B | #000 | wiggly",
    "%%metro grid: sec | notints",
    "%%metro grid: sec",
    "%%metro fold_threshold: abc",
    "%%metro label_angle: abc",
    "%%metro legend_min_height: abc",
    "%%metro line_order: sideways",
    "%%metro compact_offsets: maybe",
    "%%metro file: nolabels",
]


@pytest.mark.parametrize("directive", MALFORMED_DIRECTIVES)
def test_malformed_directive_payload_warns(directive):
    """Every directive warns (rather than silently ignoring) unusable payload."""
    text = (
        f"graph LR\n{directive}\n%%metro line: a | A | #fff\nx[X]\ny[Y]\nx -->|a| y\n"
    )
    with pytest.warns(UserWarning, match="ignoring"):
        graph = parse_metro_mermaid(text)
    # The rest of the diagram still parses.
    assert "x" in graph.stations


def test_section_scoped_directive_outside_subgraph_warns():
    text = (
        "graph LR\n%%metro line: a | A | #fff\n%%metro entry: left | a\n"
        "x[X]\ny[Y]\nx -->|a| y\n"
    )
    with pytest.warns(UserWarning, match="inside a subgraph"):
        parse_metro_mermaid(text)


def test_invalid_section_direction_warns():
    text = (
        "graph LR\n%%metro line: a | A | #fff\n"
        "subgraph s [S]\n%%metro direction: sideways\nx[X]\nend\n"
        "y[Y]\nx -->|a| y\n"
    )
    with pytest.warns(UserWarning, match="LR/RL/TB"):
        parse_metro_mermaid(text)


def test_non_lr_primary_direction_warns():
    text = "graph TB\n%%metro line: a | A | #fff\nx[X]\ny[Y]\nx -->|a| y\n"
    with pytest.warns(UserWarning, match="left-to-right"):
        parse_metro_mermaid(text)


def test_bare_graph_header_does_not_warn():
    import warnings

    text = "graph\n%%metro line: a | A | #fff\nx[X]\ny[Y]\nx -->|a| y\n"
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        parse_metro_mermaid(text)


def test_marker_legend_without_caption_warns():
    """A marker_legend with no '| Caption' warns and adds no legend entry."""
    text = (
        "graph LR\n%%metro line: a | A | #fff\n%%metro marker_legend: circle, solid\n"
        "x[X]\ny[Y]\nx -->|a| y\n"
    )
    with pytest.warns(UserWarning, match="marker_legend"):
        graph = parse_metro_mermaid(text)
    assert graph.marker_legend == []


def test_legend_combo_single_id_warns():
    """A legend_combo with a single id, no '| Label', and < 2 line ids warns."""
    text = (
        "graph LR\n%%metro line: a | A | #fff\n%%metro line: b | B | #000\n"
        "%%metro legend_combo: a\nx[X]\ny[Y]\nx -->|a| y\n"
    )
    with pytest.warns(UserWarning, match="legend_combo"):
        graph = parse_metro_mermaid(text)
    assert graph.legend_combos == []


def test_edge_before_node_declaration_sets_label_later():
    """An edge auto-creates a station; a later node declaration sets its label."""
    text = "graph LR\n%%metro line: m | M | #fff\na[A]\na -->|m| b\nb[Real Label]\n"
    graph = parse_metro_mermaid(text)
    assert graph.stations["b"].label == "Real Label"
    assert graph.stations["b"].is_hidden is False


def test_later_separate_declaration_overrides_inline_label():
    """A later explicit node declaration overrides an inline edge-endpoint label."""
    text = "graph LR\n%%metro line: a | A | #fff\nx[X] -->|a| y[Y]\ny[Y Final]\n"
    graph = parse_metro_mermaid(text)
    assert graph.stations["y"].label == "Y Final"
    assert graph.stations["x"].label == "X"


def test_bare_subgraph_header_warns_and_makes_no_section():
    """A 'subgraph' line with no id is unrecognised and creates no section."""
    text = (
        "graph LR\n%%metro line: a | A | #fff\nsubgraph\nx[X]\nend\ny[Y]\nx -->|a| y\n"
    )
    with pytest.warns(UserWarning, match="unrecognised line"):
        graph = parse_metro_mermaid(text)
    assert graph.sections == {}
