"""Tests for Nextflow DAG conversion regressions."""

import re

import pytest

from nf_metro.convert import convert_nextflow_dag
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
