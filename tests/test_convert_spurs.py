"""Tests for Nextflow DAG conversion regressions."""

import re

import pytest

from nf_metro.convert import convert_nextflow_dag
from nf_metro.layout import compute_layout
from nf_metro.parser import parse_metro_mermaid


class TestInterSectionSpurs:
    @pytest.mark.parametrize("through_first", [False, True])
    def test_dead_end_connection_is_emitted(self, through_first):
        """A spur in another subworkflow keeps its input dependency."""
        qc_id, publish_id = ("v3", "v2") if through_first else ("v2", "v3")
        text = (
            'flowchart TB\n    subgraph "SOURCE [SOURCE]"\n'
            '    v1(["ALIGN"])\n    end\n'
            '    subgraph "QC [QC]"\n'
            f'    {qc_id}(["QC_REPORT"])\n    v5(["QC_STATS"])\n    end\n'
            '    subgraph "THROUGH [THROUGH]"\n'
            f'    {publish_id}(["PUBLISH"])\n    end\n'
            '    subgraph "RESULTS [RESULTS]"\n'
            '    v4(["FINISH"])\n    end\n'
            f"    v1 --> {qc_id}\n    v1 --> {publish_id}\n"
            f"    {publish_id} --> v4\n    v1 --> v5\n"
        )

        result = convert_nextflow_dag(text)

        edges = re.findall(r"(\w+) -->\|([^|]+)\| (\w+)", result)
        assert {(source, target) for source, _, target in edges} == {
            ("align", "qc_report"),
            ("align", "qc_stats"),
            ("align", "publish"),
            ("publish", "finish"),
        }
        assert [edge for edge in edges if edge[1] == "spur"] == [
            ("align", "spur", "qc_report"),
            ("align", "spur", "qc_stats"),
        ]
        assert len(edges) == 4
        graph = parse_metro_mermaid(result)
        assert any(
            edge.line_id == "spur" and edge.target == "qc_report"
            for edge in graph.edges
        )


class TestCyclicSections:
    @staticmethod
    def _section_names(result):
        graph = parse_metro_mermaid(result)
        compute_layout(graph, validate=True)
        return {
            station: graph.sections[graph.stations[station].section_id].name
            for station in graph.stations
            if not graph.stations[station].is_port
            and graph.stations[station].section_id
        }, graph

    @pytest.mark.parametrize("summary_is_dead_end", [True, False])
    def test_unassigned_processes_split_around_subworkflow(self, summary_is_dead_end):
        """Top-level processes before and after a subworkflow get two sections."""
        text = (
            "flowchart TB\n"
            '    subgraph " "\n    v0["Channel.fromFilePairs"]\n    end\n'
            '    v1(["FASTQC"])\n'
            '    subgraph "QC [QC]"\n'
            '    v2(["QC_SUMMARY"])\n    v3(["SAMTOOLS_STATS"])\n    end\n'
            '    v4(["MULTIQC"])\n'
            "    v0 --> v1\n    v0 --> v3\n    v1 --> v2\n    v3 --> v4\n"
        )
        if not summary_is_dead_end:
            text += "    v2 --> v4\n"

        names, graph = self._section_names(convert_nextflow_dag(text))

        assert names == {
            "fastqc": "Main",
            "qc_summary": "Qc",
            "samtools_stats": "Qc",
            "multiqc": "Reporting",
        }
        assert len(graph.sections) == 3

    def test_parent_subworkflow_split_around_child(self):
        """A parent's own processes before and after its child are split."""
        text = (
            "flowchart TB\n"
            '    subgraph "PIPE:PARENT [PARENT]"\n'
            '    v1(["PREPARE"])\n    v4(["SUMMARISE"])\n    end\n'
            '    subgraph "PIPE:PARENT:CHILD [CHILD]"\n'
            '    v2(["ALIGN"])\n    v3(["SORT"])\n    end\n'
            "    v1 --> v2\n    v2 --> v3\n    v3 --> v4\n"
        )

        names, graph = self._section_names(convert_nextflow_dag(text))

        assert len(graph.sections) == 3
        assert names["prepare"] == names["summarise"] == "Parent"
        prepare = graph.stations["prepare"].section_id
        assert graph.stations["summarise"].section_id != prepare

    def test_child_stays_whole_when_splitting_parent_suffices(self):
        """Only the outer subworkflow splits, and the title lists each once."""
        text = (
            "flowchart TB\n"
            '    subgraph "PIPE:PARENT [PARENT]"\n'
            '    v1(["PREPARE"])\n    v4(["SUMMARISE"])\n    end\n'
            '    subgraph "PIPE:PARENT:CHILD [CHILD]"\n'
            '    v2(["ALIGN"])\n    v3(["SORT"])\n    end\n'
            '    subgraph "PIPE:QC [QC]"\n    v5(["FASTQC"])\n    end\n'
            "    v1 --> v2\n    v2 --> v4\n    v1 --> v5\n    v5 --> v3\n"
        )

        result = convert_nextflow_dag(text)
        names, graph = self._section_names(result)

        assert sorted(sec.name for sec in graph.sections.values()) == [
            "Child",
            "Parent",
            "Parent",
            "Qc",
        ]
        assert graph.stations["align"].section_id == graph.stations["sort"].section_id
        assert "%%metro title: Parent / Qc / Child Pipeline" in result

    def test_unassigned_split_uses_fewest_groups(self):
        """Dead-end and final top-level processes share the Reporting group."""
        text = (
            "flowchart TB\n"
            '    v1(["TRIM"])\n'
            '    subgraph "PIPE:ALIGN [ALIGN]"\n    v2(["STAR"])\n    end\n'
            '    subgraph "PIPE:QUANT [QUANT]"\n    v3(["SALMON"])\n    end\n'
            '    v4(["DESEQ2_QC"])\n    v5(["MULTIQC"])\n'
            "    v1 --> v2\n    v2 --> v4\n    v2 --> v3\n    v3 --> v5\n"
        )

        names, _ = self._section_names(convert_nextflow_dag(text))

        assert names == {
            "trim": "Main",
            "star": "Align",
            "salmon": "Quant",
            "deseq2_qc": "Reporting",
            "multiqc": "Reporting",
        }
