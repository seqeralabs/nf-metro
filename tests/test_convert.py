"""Tests for Nextflow DAG converter."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nf_metro.convert import (
    _break_cycles,
    _humanize_label,
    _parse_nextflow_mermaid,
    _reconnect_edges,
    _sanitize_id,
    convert_nextflow_dag,
    is_nextflow_dag,
)

FIXTURES = Path(__file__).parent / "fixtures" / "nextflow"


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
class TestIsNextflowDag:
    def test_flowchart_tb(self):
        assert is_nextflow_dag("flowchart TB\n    v0 --> v1")

    def test_flowchart_lr(self):
        assert is_nextflow_dag("flowchart LR\n    v0 --> v1")

    def test_graph_lr_is_not(self):
        assert not is_nextflow_dag("graph LR\n    a --> b")

    def test_metro_mmd_is_not(self):
        assert not is_nextflow_dag("%%metro title: Test\ngraph LR\n")


# ---------------------------------------------------------------------------
# Label helpers
# ---------------------------------------------------------------------------
class TestHumanizeLabel:
    def test_simple(self):
        assert _humanize_label("FASTQC") == "Fastqc"

    def test_multi_word(self):
        assert _humanize_label("STAR_ALIGN") == "Star Align"

    def test_abbreviation_long(self):
        label = _humanize_label("STAR_GENOMEGENERATE")
        assert len(label) <= 16
        assert label.startswith("Star")

    def test_abbreviation_very_long(self):
        label = _humanize_label("GATK_HAPLOTYPECALLER")
        assert len(label) <= 16
        assert label.startswith("Gatk")

    def test_no_abbreviation_when_short(self):
        assert _humanize_label("BWA_MEM") == "Bwa Mem"

    def test_abbreviation_disabled(self):
        assert _humanize_label("STAR_GENOMEGENERATE", abbreviate=False) == (
            "Star Genomegenerate"
        )


class TestSanitizeId:
    def test_simple(self):
        assert _sanitize_id("FASTQC") == "fastqc"

    def test_with_underscore(self):
        assert _sanitize_id("STAR_ALIGN") == "star_align"

    def test_special_chars(self):
        assert _sanitize_id("foo-bar baz") == "foo_bar_baz"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
class TestParseNextflowMermaid:
    def test_flat_pipeline(self):
        text = (FIXTURES / "flat_pipeline.mmd").read_text()
        dag = _parse_nextflow_mermaid(text)

        # Should have nodes of all shapes
        process_nodes = [n for n in dag.nodes.values() if n.shape == "stadium"]
        channel_nodes = [n for n in dag.nodes.values() if n.shape == "square"]
        operator_nodes = [n for n in dag.nodes.values() if n.shape == "circle"]

        assert len(process_nodes) >= 4  # FASTQC, TRIM_READS, ALIGN, etc.
        assert len(channel_nodes) >= 2  # Channel.of nodes
        assert len(operator_nodes) >= 1  # operator nodes

        # Should have edges
        assert len(dag.edges) > 0

    def test_with_subworkflows(self):
        text = (FIXTURES / "with_subworkflows.mmd").read_text()
        dag = _parse_nextflow_mermaid(text)

        # Should have named subgraphs
        named = [sg for sg in dag.subgraphs.values() if sg.short_name.strip()]
        assert len(named) >= 3  # PREPROCESS, ALIGNMENT, QUANTIFICATION

        # Process nodes should be assigned to subgraphs
        process_nodes = [n for n in dag.nodes.values() if n.shape == "stadium"]
        assigned = [n for n in process_nodes if n.subgraph is not None]
        assert len(assigned) >= 6

    def test_space_subgraphs_ignored(self):
        text = (FIXTURES / "flat_pipeline.mmd").read_text()
        dag = _parse_nextflow_mermaid(text)

        # Space-only subgraphs should not appear
        for sg in dag.subgraphs.values():
            assert sg.short_name.strip() != ""

    def test_variant_calling_subgraphs(self):
        text = (FIXTURES / "variant_calling.mmd").read_text()
        dag = _parse_nextflow_mermaid(text)

        names = {sg.short_name for sg in dag.subgraphs.values()}
        assert "PREPROCESS" in names
        assert "ALIGNMENT" in names
        assert "VARIANT_CALLING" in names


# ---------------------------------------------------------------------------
# Edge reconnection
# ---------------------------------------------------------------------------
class TestReconnectEdges:
    def test_simple_passthrough(self):
        # A -> drop -> B  =>  A -> B
        kept = {"a", "b"}
        edges = [("a", "x"), ("x", "b")]
        result = _reconnect_edges(kept, edges)
        assert ("a", "b") in result

    def test_chain_of_dropped(self):
        # A -> d1 -> d2 -> B  =>  A -> B
        kept = {"a", "b"}
        edges = [("a", "d1"), ("d1", "d2"), ("d2", "b")]
        result = _reconnect_edges(kept, edges)
        assert ("a", "b") in result

    def test_fanout_through_operator(self):
        # A -> op -> B, op -> C  =>  A -> B, A -> C
        kept = {"a", "b", "c"}
        edges = [("a", "op"), ("op", "b"), ("op", "c")]
        result = _reconnect_edges(kept, edges)
        assert ("a", "b") in result
        assert ("a", "c") in result

    def test_no_self_loops(self):
        kept = {"a"}
        edges = [("a", "x"), ("x", "a")]
        result = _reconnect_edges(kept, edges)
        assert ("a", "a") not in result

    def test_dropped_root_lost(self):
        # ch -> A (ch is dropped root, no kept predecessor)
        kept = {"a"}
        edges = [("ch", "a")]
        result = _reconnect_edges(kept, edges)
        # No edges since ch is dropped and has no kept predecessor
        assert len(result) == 0

    def test_direct_kept_to_kept(self):
        kept = {"a", "b"}
        edges = [("a", "b")]
        result = _reconnect_edges(kept, edges)
        assert ("a", "b") in result


# ---------------------------------------------------------------------------
# Cycle breaking
# ---------------------------------------------------------------------------
class TestBreakCycles:
    def test_no_cycle(self):
        edges = [("a", "b"), ("b", "c")]
        result = _break_cycles({"a", "b", "c"}, edges)
        assert len(result) == 2

    def test_simple_cycle(self):
        edges = [("a", "b"), ("b", "a")]
        result = _break_cycles({"a", "b"}, edges)
        assert len(result) == 1  # one back edge removed

    def test_triangle_cycle(self):
        edges = [("a", "b"), ("b", "c"), ("c", "a")]
        result = _break_cycles({"a", "b", "c"}, edges)
        assert len(result) == 2  # one back edge removed


# ---------------------------------------------------------------------------
# Full conversion
# ---------------------------------------------------------------------------
class TestConvertNextflowDag:
    def test_flat_pipeline_output(self):
        text = (FIXTURES / "flat_pipeline.mmd").read_text()
        result = convert_nextflow_dag(text)

        assert "%%metro title:" in result
        assert "%%metro line: main |" in result
        assert "graph LR" in result
        assert "subgraph pipeline" in result

        # Should contain process stations
        assert "fastqc" in result
        assert "trim_reads" in result
        assert "multiqc" in result

        # No bypass lines (single section)
        assert "bypass" not in result.lower()
        assert "spur" not in result.lower()

    def test_subworkflows_sections(self):
        text = (FIXTURES / "with_subworkflows.mmd").read_text()
        result = convert_nextflow_dag(text)

        assert "subgraph preprocess" in result
        assert "subgraph alignment" in result
        assert "subgraph quantification" in result
        assert "subgraph reporting" in result

    def test_subworkflows_spur_line(self):
        text = (FIXTURES / "with_subworkflows.mmd").read_text()
        result = convert_nextflow_dag(text)

        # SAMTOOLS_INDEX is a dead end, should be on spur line
        assert "%%metro line: spur |" in result
        assert "samtools_sort -->|spur| samtools_index" in result

    def test_subworkflows_bypass_lines(self):
        text = (FIXTURES / "with_subworkflows.mmd").read_text()
        result = convert_nextflow_dag(text)

        # Should have bypass lines for edges spanning 2+ sections
        assert "preprocess_reporting" in result
        assert "alignment_reporting" in result

    def test_variant_calling_sections(self):
        text = (FIXTURES / "variant_calling.mmd").read_text()
        result = convert_nextflow_dag(text)

        assert "subgraph preprocess" in result
        assert "subgraph alignment" in result
        assert "subgraph variant_calling" in result
        assert "subgraph reporting" in result

    def test_variant_calling_diamond(self):
        text = (FIXTURES / "variant_calling.mmd").read_text()
        result = convert_nextflow_dag(text)

        # Both callers should connect to bcftools_stats
        assert "gatk_haplotypecaller -->|main| bcftools_stats" in result
        assert "deepvariant -->|main| bcftools_stats" in result

    def test_abbreviation_applied(self):
        text = (FIXTURES / "with_subworkflows.mmd").read_text()
        result = convert_nextflow_dag(text)

        # Star Genomegenerate (19 chars) should be abbreviated
        assert "Star Genomegenerate" not in result
        assert "Star Genomegener" in result

    def test_custom_title(self):
        text = (FIXTURES / "flat_pipeline.mmd").read_text()
        result = convert_nextflow_dag(text, title="My Pipeline")
        assert "%%metro title: My Pipeline" in result

    def test_inter_section_edges_outside_subgraphs(self):
        text = (FIXTURES / "with_subworkflows.mmd").read_text()
        result = convert_nextflow_dag(text)

        # Inter-section edges should be outside subgraph blocks
        lines = result.split("\n")
        in_subgraph = False
        inter_comment_found = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("subgraph "):
                in_subgraph = True
            elif stripped == "end":
                in_subgraph = False
            elif "%% Inter-section edges" in stripped:
                inter_comment_found = True
                assert not in_subgraph

        assert inter_comment_found

    def test_empty_dag(self):
        result = convert_nextflow_dag("flowchart TB\n")
        assert "Empty Pipeline" in result


# ---------------------------------------------------------------------------
# Regression: unquoted labels (issue #249, Nextflow 23.x+)
# ---------------------------------------------------------------------------
class TestUnquotedLabels:
    """Stadium nodes parse whether or not labels are wrapped in quotes."""

    def test_unquoted_stadium_parses(self):
        text = (
            "flowchart TB\n"
            '    v1["input"]\n'
            "    v2([PROCESS_A])\n"
            "    v3([PROCESS_B])\n"
            '    v4[" "]\n'
            "    v1 --> v2\n"
            "    v2 --> v3\n"
            "    v3 --> v4\n"
        )
        dag = _parse_nextflow_mermaid(text)
        process_nodes = [n for n in dag.nodes.values() if n.shape == "stadium"]
        assert len(process_nodes) == 2
        assert {n.label for n in process_nodes} == {"PROCESS_A", "PROCESS_B"}

    def test_unquoted_convert_non_empty(self):
        text = (FIXTURES / "unquoted_labels.mmd").read_text()
        result = convert_nextflow_dag(text)
        assert "Empty Pipeline" not in result
        assert "fastqc" in result
        assert "trim_reads" in result
        assert "multiqc" in result

    def test_mixed_quoted_and_unquoted(self):
        text = (
            "flowchart TB\n"
            '    v1(["QUOTED_PROC"])\n'
            "    v2([UNQUOTED_PROC])\n"
            "    v1 --> v2\n"
        )
        dag = _parse_nextflow_mermaid(text)
        labels = {n.label for n in dag.nodes.values() if n.shape == "stadium"}
        assert labels == {"QUOTED_PROC", "UNQUOTED_PROC"}


# ---------------------------------------------------------------------------
# Regression: duplicate process labels across subworkflows (issue #249)
# ---------------------------------------------------------------------------
class TestDuplicateProcessLabels:
    """Distinct nodes that share a label get distinct station IDs."""

    def test_no_duplicate_station_declarations(self):
        text = (FIXTURES / "duplicate_processes.mmd").read_text()
        result = convert_nextflow_dag(text)
        decls = re.findall(r"^\s+([a-z0-9_]+)\(\[", result, re.MULTILINE)
        assert len(decls) == len(set(decls)), f"Duplicate station declarations: {decls}"

    def test_no_self_loops(self):
        text = (FIXTURES / "duplicate_processes.mmd").read_text()
        result = convert_nextflow_dag(text)
        for m in re.finditer(r"([a-z0-9_]+)\s*-->\|[^|]+\|\s*([a-z0-9_]+)", result):
            assert m.group(1) != m.group(2), f"Self-loop edge: {m.group(0)}"

    def test_duplicates_lay_out(self):
        from nf_metro.layout import compute_layout
        from nf_metro.parser import parse_metro_mermaid

        text = (FIXTURES / "duplicate_processes.mmd").read_text()
        mmd = convert_nextflow_dag(text)
        graph = parse_metro_mermaid(mmd)
        compute_layout(graph, x_spacing=60.0, y_spacing=40.0)

    def test_unnamed_subgraph_duplicates_disambiguated(self):
        text = (
            "flowchart TB\n"
            '    subgraph " "\n'
            "    v1([PROC])\n"
            "    end\n"
            '    subgraph " "\n'
            "    v2([PROC])\n"
            "    end\n"
            "    v1 --> v2\n"
        )
        result = convert_nextflow_dag(text)
        decls = re.findall(r"^\s+([a-z0-9_]+)\(\[", result, re.MULTILINE)
        assert len(decls) == 2 and len(set(decls)) == 2, (
            f"Expected two distinct stations, got {decls}"
        )


# ---------------------------------------------------------------------------
# Roundtrip: convert then parse through nf-metro
# ---------------------------------------------------------------------------
class TestRoundtrip:
    @pytest.fixture(
        params=[
            "flat_pipeline",
            "with_subworkflows",
            "variant_calling",
            "unquoted_labels",
            "duplicate_processes",
        ]
    )
    def fixture_name(self, request):
        return request.param

    def test_roundtrip_parse(self, fixture_name):
        """Converted output should parse without errors through nf-metro."""
        from nf_metro.parser import parse_metro_mermaid

        text = (FIXTURES / f"{fixture_name}.mmd").read_text()
        mmd = convert_nextflow_dag(text)
        graph = parse_metro_mermaid(mmd)

        assert len(graph.stations) > 0
        assert len(graph.edges) > 0
        assert len(graph.sections) > 0

    def test_roundtrip_layout(self, fixture_name):
        """Converted output should lay out without errors."""
        from nf_metro.layout import compute_layout
        from nf_metro.parser import parse_metro_mermaid

        text = (FIXTURES / f"{fixture_name}.mmd").read_text()
        mmd = convert_nextflow_dag(text)
        graph = parse_metro_mermaid(mmd)
        compute_layout(graph, x_spacing=60.0, y_spacing=40.0)

        # All stations should have coordinates
        for station in graph.stations.values():
            if not station.is_port:
                assert station.x != 0 or station.y != 0, (
                    f"Station {station.id} has no coordinates"
                )

    def test_roundtrip_render(self, fixture_name):
        """Converted output should render to SVG without errors."""
        from nf_metro.layout import compute_layout
        from nf_metro.parser import parse_metro_mermaid
        from nf_metro.render import render_svg
        from nf_metro.themes import THEMES

        text = (FIXTURES / f"{fixture_name}.mmd").read_text()
        mmd = convert_nextflow_dag(text)
        graph = parse_metro_mermaid(mmd)
        compute_layout(graph, x_spacing=60.0, y_spacing=40.0)
        svg = render_svg(graph, THEMES["nfcore"])

        assert "<svg" in svg
        assert "metro" in svg.lower() or "station" in svg.lower() or "<circle" in svg
