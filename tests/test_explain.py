"""Tests for nf-metro explain: the causal layout decision reporter."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nf_metro.cli import cli
from nf_metro.explain import build_explain, format_explain_json, format_explain_text
from nf_metro.parser import parse_metro_mermaid

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
RNASEQ_AUTO_MMD = EXAMPLES_DIR / "rnaseq_auto.mmd"
RNASEQ_SECTIONS_MMD = EXAMPLES_DIR / "rnaseq_sections.mmd"
DA_MMD = EXAMPLES_DIR / "differentialabundance.mmd"


# ---------------------------------------------------------------------------
# build_explain contract
# ---------------------------------------------------------------------------


def test_explain_returns_required_keys():
    """build_explain returns a dict with the documented top-level keys."""
    graph = parse_metro_mermaid(RNASEQ_AUTO_MMD.read_text())
    data = build_explain(graph)
    assert {"title", "warnings", "decisions", "summary"} <= set(data)
    assert isinstance(data["decisions"], list)
    assert isinstance(data["summary"], dict)
    assert {"inferred", "synthetic"} <= set(data["summary"])


def test_explain_decision_schema():
    """Every decision has the required fields."""
    graph = parse_metro_mermaid(RNASEQ_AUTO_MMD.read_text())
    data = build_explain(graph)
    required = {
        "subject",
        "subject_type",
        "aspect",
        "decision",
        "source",
        "rule",
        "detail",
    }
    for d in data["decisions"]:
        assert required <= set(d), f"Decision missing keys: {d}"
        assert d["source"] in ("inferred", "synthetic")
        assert d["subject_type"] in ("section", "station", "layout")


def test_explain_rnaseq_auto_directions():
    """rnaseq_auto.mmd infers all section directions; each appears with correct rule."""
    graph = parse_metro_mermaid(RNASEQ_AUTO_MMD.read_text())
    data = build_explain(graph)

    dir_decisions = {
        d["subject"]: d for d in data["decisions"] if d["aspect"] == "direction"
    }
    # preprocessing: source section -> LR
    assert "preprocessing" in dir_decisions
    assert dir_decisions["preprocessing"]["decision"] == "LR"
    assert dir_decisions["preprocessing"]["rule"] == "source-section"

    # postprocessing: fold bridge -> TB
    assert "postprocessing" in dir_decisions
    assert dir_decisions["postprocessing"]["decision"] == "TB"
    assert dir_decisions["postprocessing"]["rule"] == "fold-bridge"

    # qc_report: return-row terminal -> RL
    assert "qc_report" in dir_decisions
    assert dir_decisions["qc_report"]["decision"] == "RL"
    assert dir_decisions["qc_report"]["rule"] == "return-row-leaf"


def test_explain_fold_fires_for_auto_layout():
    """A fold explanation is emitted when auto-layout wraps into multiple rows."""
    graph = parse_metro_mermaid(RNASEQ_AUTO_MMD.read_text())
    data = build_explain(graph)

    fold_decisions = [d for d in data["decisions"] if d["aspect"] == "layout"]
    assert fold_decisions, "Expected a fold/layout explanation"
    assert fold_decisions[0]["rule"] == "fold-threshold"
    assert "rows" in fold_decisions[0]["decision"]


def test_explain_fold_silent_for_explicit_grid():
    """No fold explanation when all sections are author-placed with %%metro grid:."""
    graph = parse_metro_mermaid(DA_MMD.read_text())
    data = build_explain(graph)

    fold_decisions = [d for d in data["decisions"] if d["aspect"] == "layout"]
    assert not fold_decisions, (
        "Fold explanation must not fire when all grid positions are explicit"
    )


def test_explain_explicit_direction_not_reported():
    """Sections with explicit %%metro direction: do not appear in inferred decisions."""
    graph = parse_metro_mermaid(RNASEQ_SECTIONS_MMD.read_text())
    data = build_explain(graph)

    inferred_dir_subjects = {
        d["subject"] for d in data["decisions"] if d["aspect"] == "direction"
    }
    # rnaseq_sections.mmd has two explicit direction directives; check none of
    # those sections show up as inferred.
    for sid in graph._explicit_directions:
        assert sid not in inferred_dir_subjects, (
            f"Section {sid!r} has an explicit direction but appeared as inferred"
        )


def test_explain_explicit_grid_sections_skipped_in_directions():
    """Sections in _explicit_grid do not appear in direction decisions."""
    graph = parse_metro_mermaid(DA_MMD.read_text())
    data = build_explain(graph)

    dir_subjects = {
        d["subject"] for d in data["decisions"] if d["aspect"] == "direction"
    }
    for sid in graph._explicit_grid:
        assert sid not in dir_subjects, (
            f"Explicitly-gridded section {sid!r} appeared in direction decisions"
        )


def test_explain_fan_out_junction():
    """Fan-out junctions are reported with source and target section details."""
    graph = parse_metro_mermaid(RNASEQ_AUTO_MMD.read_text())
    data = build_explain(graph)

    junctions = [d for d in data["decisions"] if d["aspect"] == "junction"]
    assert junctions, "Expected at least one junction explanation"
    j = junctions[0]
    assert j["source"] == "synthetic"
    assert j["rule"] == "fan-out-junction"
    assert "preprocessing" in j["detail"]


def test_explain_bypass_station():
    """Bypass-V stations are reported with the bypassed station's label."""
    graph = parse_metro_mermaid(DA_MMD.read_text())
    data = build_explain(graph)

    bypasses = [d for d in data["decisions"] if d["aspect"] == "bypass"]
    assert bypasses, (
        "Expected at least one bypass explanation for differentialabundance"
    )
    b = bypasses[0]
    assert b["source"] == "synthetic"
    assert b["rule"] == "bypass-v"
    assert "Annotate results" in b["detail"]


def test_explain_port_sides_inferred():
    """Inferred entry/exit port sides appear for rnaseq_auto sections."""
    graph = parse_metro_mermaid(RNASEQ_AUTO_MMD.read_text())
    data = build_explain(graph)

    entry_sides = {
        d["subject"]: d["rule"]
        for d in data["decisions"]
        if d["aspect"] == "entry_side"
    }
    # qc_report gets TOP entry because postprocessing TB predecessor drops down
    assert entry_sides.get("qc_report") == "vertical-drop"
    # genome_align is LR -> flow-aligned LEFT entry
    assert entry_sides.get("genome_align") == "flow-aligned-entry"


def test_explain_no_decisions_single_section(tmp_path):
    """Single-section graph with no inferences produces an empty decisions list."""
    mmd = tmp_path / "simple.mmd"
    mmd.write_text(
        "%%metro title: Simple\n"
        "%%metro line: a | A | #ff0000\n"
        "graph LR\n"
        "    x[X] -->|a| y[Y]\n"
    )
    graph = parse_metro_mermaid(mmd.read_text())
    data = build_explain(graph)
    assert data["decisions"] == []
    assert data["summary"]["inferred"] == 0
    assert data["summary"]["synthetic"] == 0


def test_explain_summary_counts():
    """summary.inferred + summary.synthetic equals len(decisions)."""
    graph = parse_metro_mermaid(RNASEQ_AUTO_MMD.read_text())
    data = build_explain(graph)
    total = data["summary"]["inferred"] + data["summary"]["synthetic"]
    assert total == len(data["decisions"])


# ---------------------------------------------------------------------------
# Section and station filters
# ---------------------------------------------------------------------------


def test_explain_section_filter_restricts_output():
    """section_filter uses the structural sections field, not string matching."""
    graph = parse_metro_mermaid(RNASEQ_AUTO_MMD.read_text())
    data = build_explain(graph, section_filter="preprocessing")
    for d in data["decisions"]:
        assert "preprocessing" in d["sections"], (
            f"Decision for {d['subject']!r} slipped through section filter: "
            f"sections={d['sections']}"
        )


def test_explain_station_filter():
    """station_filter returns only decisions for the named station id."""
    graph = parse_metro_mermaid(DA_MMD.read_text())
    # Find a junction id to filter on
    if not graph.junctions:
        pytest.skip("No junctions in graph")
    jid = graph.junctions[0]
    data = build_explain(graph, station_filter=jid)
    assert all(d["subject"] == jid for d in data["decisions"])


def test_explain_section_filter_unknown_section():
    """Filtering by a non-existent section returns an empty decisions list."""
    graph = parse_metro_mermaid(RNASEQ_AUTO_MMD.read_text())
    data = build_explain(graph, section_filter="does_not_exist")
    assert data["decisions"] == []


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def test_format_explain_json_is_valid():
    """format_explain_json produces valid, parseable JSON."""
    graph = parse_metro_mermaid(RNASEQ_AUTO_MMD.read_text())
    data = build_explain(graph)
    text = format_explain_json(data)
    parsed = json.loads(text)
    assert parsed["title"] == data["title"]
    assert len(parsed["decisions"]) == len(data["decisions"])


def test_format_explain_text_structure():
    """format_explain_text produces headers for each aspect present."""
    graph = parse_metro_mermaid(RNASEQ_AUTO_MMD.read_text())
    data = build_explain(graph)
    text = format_explain_text(data)
    assert "Explain:" in text
    assert "Section directions" in text
    assert "Entry port sides" in text
    assert "Fan-out junctions" in text


def test_format_explain_text_empty_graph(tmp_path):
    """Empty decisions list renders as 'no decisions' message."""
    mmd = tmp_path / "s.mmd"
    mmd.write_text(
        "%%metro title: Simple\n%%metro line: a | A | #ff0000\n"
        "graph LR\n    x[X] -->|a| y[Y]\n"
    )
    graph = parse_metro_mermaid(mmd.read_text())
    data = build_explain(graph)
    text = format_explain_text(data)
    assert "No layout decisions" in text


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_explain_cli_runs():
    """explain CLI command exits 0 and prints the title."""
    runner = CliRunner()
    result = runner.invoke(cli, ["explain", str(RNASEQ_AUTO_MMD)])
    assert result.exit_code == 0, result.output
    assert "Explain:" in result.output


def test_explain_cli_json():
    """explain --json emits valid JSON with decisions and summary keys."""
    runner = CliRunner()
    result = runner.invoke(cli, ["explain", str(RNASEQ_AUTO_MMD), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert {"title", "warnings", "decisions", "summary"} <= set(data)


def test_explain_cli_section_filter():
    """explain --section restricts output to the requested section."""
    runner = CliRunner()
    result = runner.invoke(
        cli, ["explain", str(RNASEQ_AUTO_MMD), "--section", "preprocessing"]
    )
    assert result.exit_code == 0, result.output
    assert "preprocessing" in result.output


def test_explain_cli_matches_formatter():
    """CLI default output matches format_explain_text byte-for-byte."""
    graph = parse_metro_mermaid(RNASEQ_AUTO_MMD.read_text())
    data = build_explain(graph, warnings=[])
    expected = format_explain_text(data)

    runner = CliRunner()
    result = runner.invoke(cli, ["explain", str(RNASEQ_AUTO_MMD)])
    assert result.exit_code == 0
    assert result.output == expected + "\n"


@pytest.mark.parametrize(
    "mmd_path",
    sorted((EXAMPLES_DIR).glob("*.mmd"))
    + sorted((EXAMPLES_DIR / "topologies").glob("*.mmd")),
    ids=lambda p: p.stem,
)
def test_explain_all_fixtures_no_crash(mmd_path):
    """explain never raises on any gallery or topology fixture."""
    graph = parse_metro_mermaid(mmd_path.read_text())
    data = build_explain(graph)
    _ = format_explain_text(data)
    _ = format_explain_json(data)
