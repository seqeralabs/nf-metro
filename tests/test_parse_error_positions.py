"""Tests for parse and validation error positional context.

Covers:
- Lark syntax errors surface line/column and a caret-style context snippet.
- Semantic errors (missing annotation, undeclared line) include source line numbers.
- ValidationIssue carries an optional ``line`` field and formats as a compiler
  diagnostic.
- The ``validate`` CLI command surfaces parse errors via ClickException.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner
from lark import UnexpectedCharacters

from nf_metro.cli import cli
from nf_metro.parser import ERROR, ValidationIssue, validate_graph
from nf_metro.parser.grammar import parse_statements
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge, MetroGraph, MetroLine, Station

# ---------------------------------------------------------------------------
# Syntax error path: Lark UnexpectedInput → positioned ValueError
# ---------------------------------------------------------------------------


def test_lark_syntax_error_includes_line_and_column():
    """A Lark UnexpectedInput is re-raised as a ValueError naming line and column."""
    fake_exc = UnexpectedCharacters(
        seq="graph LR\nbad_line\n",
        lex_pos=10,
        line=2,
        column=1,
        allowed={"NAME"},
        token_history=None,
        state=None,
        terminals_by_name={},
    )
    # Patch the Lark parser to raise the exception so we exercise the handler
    # regardless of whether the earley/JUNK grammar would normally catch the line.
    with patch("nf_metro.parser.grammar._PARSER.parse", side_effect=fake_exc):
        with pytest.raises(ValueError) as exc_info:
            parse_statements("graph LR\nbad_line\n")

    msg = str(exc_info.value)
    assert "line 2" in msg
    assert "column 1" in msg


def test_lark_syntax_error_no_raw_traceback(tmp_path):
    """The validate CLI command converts parse errors to a clean ClickException."""
    bad_mmd = tmp_path / "bad.mmd"
    bad_mmd.write_text("graph LR\n")

    fake_exc = UnexpectedCharacters(
        seq="graph LR\n",
        lex_pos=0,
        line=1,
        column=1,
        allowed={"NAME"},
        token_history=None,
        state=None,
        terminals_by_name={},
    )
    with patch("nf_metro.parser.grammar._PARSER.parse", side_effect=fake_exc):
        runner = CliRunner()
        result = runner.invoke(cli, ["validate", str(bad_mmd)])

    assert result.exit_code != 0
    # ClickException wraps with "Error: <message>", no Python traceback
    assert "Error:" in result.output
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# Semantic errors: source line numbers thread through to error messages
# ---------------------------------------------------------------------------


def test_missing_annotation_includes_line_number():
    """An edge missing a metro annotation reports its source line number."""
    text = (
        "%%metro line: rna | RNA | #ff0000\n"
        "graph LR\n"
        "  a --> b\n"  # line 3 — no annotation
    )
    with pytest.raises(ValueError) as exc_info:
        parse_metro_mermaid(text)

    assert "line 3" in str(exc_info.value)


def test_undeclared_line_includes_line_number():
    """An edge referencing an undeclared line includes the source line number."""
    text = (
        "%%metro line: rna | RNA | #ff0000\n"
        "graph LR\n"
        "  a -->|dna| b\n"  # line 3 — 'dna' is not declared
    )
    with pytest.raises(ValueError) as exc_info:
        parse_metro_mermaid(text)

    assert "line 3" in str(exc_info.value)


# ---------------------------------------------------------------------------
# ValidationIssue: optional line field and format() helper
# ---------------------------------------------------------------------------


def test_validation_issue_line_defaults_to_none():
    issue = ValidationIssue(ERROR, "some message")
    assert issue.line is None


def test_validation_issue_carries_line():
    issue = ValidationIssue(ERROR, "some message", line=42)
    assert issue.line == 42


def test_validation_issue_format_with_line():
    issue = ValidationIssue(ERROR, "Edge a -> b references undefined line 'x'", line=5)
    formatted = issue.format()
    assert "line 5" in formatted
    assert "Edge a -> b" in formatted


def test_validation_issue_format_without_line():
    issue = ValidationIssue(ERROR, "Graph contains a cycle: a -> b -> a")
    formatted = issue.format()
    assert "Graph contains a cycle" in formatted
    # No spurious "line None" noise
    assert "None" not in formatted


def test_validation_issue_format_with_path():
    issue = ValidationIssue(ERROR, "something wrong", line=12)
    formatted = issue.format("pipeline.mmd")
    assert "pipeline.mmd" in formatted
    assert "line 12" in formatted


def test_validate_graph_includes_line_for_undeclared_edge():
    """validate_graph() populates line on the finding when edge carries source_line."""
    graph = MetroGraph()
    graph.lines["rna"] = MetroLine(id="rna", display_name="RNA", color="#abcdef")
    graph.stations["a"] = Station(id="a", label="A")
    graph.stations["b"] = Station(id="b", label="B")
    graph.edges.append(Edge(source="a", target="b", line_id="missing", source_line=7))

    issues = validate_graph(graph)

    assert len(issues) == 1
    assert issues[0].line == 7
    assert "missing" in issues[0].message


def test_validate_graph_no_line_when_source_line_absent():
    graph = MetroGraph()
    graph.lines["rna"] = MetroLine(id="rna", display_name="RNA", color="#abcdef")
    graph.stations["a"] = Station(id="a", label="A")
    graph.stations["b"] = Station(id="b", label="B")
    graph.edges.append(Edge(source="a", target="b", line_id="missing"))

    issues = validate_graph(graph)

    assert issues[0].line is None


# ---------------------------------------------------------------------------
# CLI: validate command error formatting
# ---------------------------------------------------------------------------


def test_validate_cli_shows_line_for_undeclared_line(tmp_path):
    """``nf-metro validate`` includes line number for a semantic error."""
    mmd = tmp_path / "test.mmd"
    mmd.write_text("%%metro line: rna | RNA | #ff0000\ngraph LR\n  a -->|dna| b\n")
    runner = CliRunner()
    result = runner.invoke(cli, ["validate", str(mmd)])

    assert result.exit_code != 0
    assert "line 3" in result.output


def test_validate_cli_clean_file_exits_zero(tmp_path):
    """``nf-metro validate`` succeeds on a well-formed minimal diagram."""
    mmd = tmp_path / "ok.mmd"
    mmd.write_text("%%metro line: rna | RNA | #ff0000\ngraph LR\n  a -->|rna| b\n")
    runner = CliRunner()
    result = runner.invoke(cli, ["validate", str(mmd)])

    assert result.exit_code == 0
