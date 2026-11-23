"""Tests for the experimental Snakemake DAG converter."""

from __future__ import annotations

import re
import warnings
from pathlib import Path

import pytest
from click.testing import CliRunner

from nf_metro.api import _resolve_input_format, render_string
from nf_metro.cli import SNAKEMAKE_QUIPS, cli
from nf_metro.convert import (
    ExperimentalInputWarning,
    FeedbackEdgesDroppedWarning,
    convert_dag,
    convert_snakemake_dag,
    detect_input_format,
)
from nf_metro.layout import compute_layout
from nf_metro.parser import parse_metro_mermaid

FIXTURES = Path(__file__).parent / "fixtures" / "snakemake"
NEXTFLOW_FIXTURES = Path(__file__).parent / "fixtures" / "nextflow"
WORKFLOWS = ["flat_pipeline", "variant_calling", "two_terminals"]
EXPORTS = ["rulegraph.dot", "dag.dot", "rulegraph.mermaid", "dag.mermaid"]


def _convert(text: str, title: str = "") -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ExperimentalInputWarning)
        return convert_snakemake_dag(text, title)


def _edges(mmd: str) -> set[tuple[str, str]]:
    return {(s, t) for s, t in re.findall(r"(\w+) -->\|[^|]+\| (\w+)", mmd)}


def _stations(mmd: str) -> list[str]:
    return re.findall(r"^\s+(\w+)\(\[", mmd, flags=re.MULTILINE)


@pytest.mark.parametrize("workflow", WORKFLOWS)
@pytest.mark.parametrize("export", EXPORTS)
def test_every_export_of_a_workflow_converts_to_the_same_map(workflow, export):
    reference = _convert((FIXTURES / f"{workflow}.rulegraph.dot").read_text())
    assert _convert((FIXTURES / f"{workflow}.{export}").read_text()) == reference


@pytest.mark.parametrize("export", EXPORTS)
def test_export_is_detected_as_snakemake(export):
    text = (FIXTURES / f"variant_calling.{export}").read_text()
    assert detect_input_format(text) == "snakemake"


def test_nextflow_dag_is_not_detected_as_snakemake():
    text = (NEXTFLOW_FIXTURES / "flat_pipeline.mmd").read_text()
    assert detect_input_format(text) == "nextflow"


def test_metro_source_is_not_detected_as_a_dag():
    assert detect_input_format("graph LR\n    a[A] -->|l| b[B]\n") is None


def test_rules_become_stations_and_the_all_target_is_dropped():
    mmd = _convert((FIXTURES / "variant_calling.rulegraph.dot").read_text())

    assert sorted(_stations(mmd)) == [
        "bcftools_stats",
        "bwa_mem",
        "deepvariant",
        "fastqc",
        "gatk_haplotypecaller",
        "multiqc",
        "samtools_index",
    ]
    assert _edges(mmd) == {
        ("fastqc", "multiqc"),
        ("bcftools_stats", "multiqc"),
        ("bwa_mem", "samtools_index"),
        ("samtools_index", "gatk_haplotypecaller"),
        ("samtools_index", "deepvariant"),
        ("gatk_haplotypecaller", "bcftools_stats"),
        ("deepvariant", "bcftools_stats"),
    }


def test_an_edge_that_a_longer_path_covers_is_dropped():
    dot = (
        "digraph snakemake_dag {\n"
        '\t0[label = "align", color = "0 0 0", style="rounded"];\n'
        '\t1[label = "index", color = "0 0 0", style="rounded"];\n'
        '\t2[label = "call", color = "0 0 0", style="rounded"];\n'
        "\t0 -> 1\n\t1 -> 2\n\t0 -> 2\n}\n"
    )
    assert _edges(_convert(dot)) == {("align", "index"), ("index", "call")}


def test_a_loop_elsewhere_does_not_keep_a_covered_edge():
    dot = (
        "digraph snakemake_dag {\n"
        '\t0[label = "align", color = "0 0 0", style="rounded"];\n'
        '\t1[label = "index", color = "0 0 0", style="rounded"];\n'
        '\t2[label = "call", color = "0 0 0", style="rounded"];\n'
        '\t3[label = "x", color = "0 0 0", style="rounded"];\n'
        '\t4[label = "y", color = "0 0 0", style="rounded"];\n'
        "\t0 -> 1\n\t1 -> 2\n\t0 -> 2\n\t3 -> 4\n\t4 -> 3\n}\n"
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FeedbackEdgesDroppedWarning)
        edges = _edges(_convert(dot))
    assert ("align", "call") not in edges
    assert {("align", "index"), ("index", "call")} <= edges


def test_a_leading_log_line_does_not_hide_a_dot_export():
    text = (
        "Building DAG of jobs...\n"
        + (FIXTURES / "flat_pipeline.rulegraph.dot").read_text()
    )
    assert detect_input_format(text) == "snakemake"
    assert _convert(text) == _convert(
        (FIXTURES / "flat_pipeline.rulegraph.dot").read_text()
    )


def test_input_without_rules_is_rejected():
    with pytest.raises(ValueError, match="No Snakemake rules found"):
        _convert("nothing to see\n")


def test_rule_names_are_not_shortened():
    mmd = _convert((FIXTURES / "variant_calling.rulegraph.dot").read_text())
    assert "([Gatk Haplotypecaller])" in mmd


def test_every_rule_shares_one_section():
    mmd = _convert((FIXTURES / "two_terminals.rulegraph.dot").read_text())
    assert mmd.count("subgraph ") == 1


def test_an_all_rule_that_feeds_another_rule_is_kept():
    dot = (
        "digraph snakemake_dag {\n"
        '\t0[label = "report", color = "0 0 0", style="rounded"];\n'
        '\t1[label = "all", color = "0 0 0", style="rounded"];\n'
        '\t2[label = "align", color = "0 0 0", style="rounded"];\n'
        "\t1 -> 0\n\t2 -> 1\n}\n"
    )
    assert _edges(_convert(dot)) == {("align", "all"), ("all", "report")}


def test_a_rule_whose_jobs_chain_is_not_reported_as_feedback():
    dot = (
        "digraph snakemake_dag {\n"
        '\t0[label = "merge\\nround: 2", color = "0 0 0", style="rounded"];\n'
        '\t1[label = "merge\\nround: 1", color = "0 0 0", style="rounded"];\n'
        '\t2[label = "split", color = "0 0 0", style="rounded"];\n'
        "\t1 -> 0\n\t2 -> 1\n}\n"
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", FeedbackEdgesDroppedWarning)
        mmd = _convert(dot)
    assert _edges(mmd) == {("split", "merge")}


def test_a_rule_level_loop_is_reported_as_feedback():
    dot = (
        "digraph snakemake_dag {\n"
        '\t0[label = "a", color = "0 0 0", style="rounded"];\n'
        '\t1[label = "b", color = "0 0 0", style="rounded"];\n'
        "\t0 -> 1\n\t1 -> 0\n}\n"
    )
    with pytest.warns(FeedbackEdgesDroppedWarning):
        _convert(dot)


@pytest.mark.parametrize("export", ["rulegraph.dot", "rulegraph.mermaid"])
def test_rendering_an_unconverted_export_points_at_the_snakemake_flag(export):
    text = (FIXTURES / f"flat_pipeline.{export}").read_text()
    with pytest.raises(ValueError, match="--from-snakemake"):
        parse_metro_mermaid(text)


def test_title_is_applied():
    mmd = _convert((FIXTURES / "flat_pipeline.rulegraph.dot").read_text(), "Demo")
    assert mmd.startswith("%%metro title: Demo\n")


def test_conversion_warns_that_it_is_experimental():
    with pytest.warns(ExperimentalInputWarning, match="experimental"):
        convert_snakemake_dag((FIXTURES / "flat_pipeline.rulegraph.dot").read_text())


def test_unknown_input_format_is_rejected():
    with pytest.raises(ValueError, match="unknown input format 'cwl'"):
        convert_dag("", "cwl")


def test_from_nextflow_contradicting_input_format_is_rejected():
    with pytest.raises(ValueError, match="contradicts"):
        _resolve_input_format("snakemake", True)


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_converted_map_lays_out(workflow):
    mmd = _convert((FIXTURES / f"{workflow}.rulegraph.dot").read_text())
    graph = parse_metro_mermaid(mmd)
    compute_layout(graph, x_spacing=60.0, y_spacing=40.0)
    assert all(s.x is not None and s.y is not None for s in graph.stations.values())


def test_render_string_converts_snakemake_input():
    text = (FIXTURES / "variant_calling.rulegraph.dot").read_text()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ExperimentalInputWarning)
        svg = render_string(text, input_format="snakemake")
    assert svg.lstrip().startswith(("<?xml", "<svg"))
    assert "Bcftools Stats" in svg


class TestCli:
    def test_convert_detects_snakemake_and_flags_it_experimental(self, tmp_path):
        out = tmp_path / "out.mmd"
        result = CliRunner().invoke(
            cli,
            ["convert", str(FIXTURES / "flat_pipeline.rulegraph.dot"), "-o", str(out)],
        )
        assert result.exit_code == 0, result.output
        assert "Snakemake input is experimental" in result.output
        assert "Converted 5 rules, 1 sections -> " in result.output
        assert "multiqc" in out.read_text()

    @pytest.mark.parametrize("command", ["convert", "render"])
    def test_snakemake_runs_print_one_quip(self, command, tmp_path):
        args = [command, str(FIXTURES / "flat_pipeline.rulegraph.dot")]
        if command == "render":
            args.append("--from-snakemake")
        args += [
            "-o",
            str(tmp_path / ("out.mmd" if command == "convert" else "out.svg")),
        ]
        result = CliRunner().invoke(cli, args)
        assert result.exit_code == 0, result.output
        assert sum(quip in result.output for quip in SNAKEMAKE_QUIPS) == 1

    def test_nextflow_runs_print_no_quip(self, tmp_path):
        result = CliRunner().invoke(
            cli,
            [
                "convert",
                str(NEXTFLOW_FIXTURES / "flat_pipeline.mmd"),
                "-o",
                str(tmp_path / "out.mmd"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert not any(quip in result.output for quip in SNAKEMAKE_QUIPS)

    def test_convert_honours_explicit_from(self, tmp_path):
        out = tmp_path / "out.mmd"
        result = CliRunner().invoke(
            cli,
            [
                "convert",
                str(FIXTURES / "flat_pipeline.rulegraph.mermaid"),
                "--from",
                "snakemake",
                "-o",
                str(out),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Converted 5 rules" in result.output

    def test_convert_warns_on_unrecognised_input(self, tmp_path):
        src = tmp_path / "x.txt"
        src.write_text("nothing to see\n")
        result = CliRunner().invoke(cli, ["convert", str(src)])
        assert "could not recognise the input" in result.output

    def test_convert_reports_input_without_rules(self, tmp_path):
        src = tmp_path / "x.txt"
        src.write_text("nothing to see\n")
        result = CliRunner().invoke(cli, ["convert", str(src), "--from", "snakemake"])
        assert result.exit_code == 1
        assert "No Snakemake rules found" in result.output
        assert "Traceback" not in result.output

    def test_render_from_snakemake(self, tmp_path):
        out = tmp_path / "out.svg"
        result = CliRunner().invoke(
            cli,
            [
                "render",
                str(FIXTURES / "variant_calling.rulegraph.dot"),
                "--from-snakemake",
                "-o",
                str(out),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Gatk Haplotypecaller" in out.read_text()

    def test_render_rejects_both_from_flags(self, tmp_path):
        result = CliRunner().invoke(
            cli,
            [
                "render",
                str(FIXTURES / "variant_calling.rulegraph.dot"),
                "--from-snakemake",
                "--from-nextflow",
                "-o",
                str(tmp_path / "out.svg"),
            ],
        )
        assert result.exit_code != 0
        assert "cannot be combined" in result.output
