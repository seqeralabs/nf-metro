"""Tests for Nextflow DAG conversion regressions."""

import re

import pytest

from nf_metro.convert import convert_nextflow_dag
from nf_metro.layout import compute_layout
from nf_metro.parser import parse_metro_mermaid


class TestDuplicateSectionNames:
    @pytest.mark.parametrize("names", [("QC", "QC"), ("QC", "QC", "QC_2")])
    def test_subworkflows_keep_distinct_sections(self, names):
        """Repeated short names and suffix collisions cannot merge sections."""
        text = "flowchart TB\n"
        for index, name in enumerate(names):
            text += (
                f'    subgraph "PIPELINE:STEP{index}:{name} [{name}]"\n'
                f'    v{index}(["PROCESS_{index}"])\n    end\n'
            )
        for index in range(len(names) - 1):
            text += f"    v{index} --> v{index + 1}\n"

        result = convert_nextflow_dag(text)
        graph = parse_metro_mermaid(result)

        assert len(graph.sections) == len(names)
        section_ids = [
            graph.stations[f"process_{i}"].section_id for i in range(len(names))
        ]
        assert len(set(section_ids)) == len(names)
        for index, section_id in enumerate(section_ids):
            assert f"process_{index}" in graph.sections[section_id].station_ids
        compute_layout(graph, validate=True)

    def test_repeated_names_keep_distinct_bypass_lines(self):
        """Two same-named sections bypassing to one target get two lines."""
        text = (
            "flowchart TB\n"
            '    subgraph "PIPELINE:FIRST:QC [QC]"\n    v1(["FASTQC"])\n    end\n'
            '    subgraph "PIPELINE:MID [MID]"\n    v2(["TRIM"])\n    end\n'
            '    subgraph "PIPELINE:SECOND:QC [QC]"\n    v3(["MULTIQC"])\n    end\n'
            '    subgraph "PIPELINE:ALIGN [ALIGN]"\n    v4(["STAR"])\n    end\n'
            '    subgraph "PIPELINE:END [END]"\n'
            '    v5(["REPORT"])\n    v6(["FINAL"])\n    end\n'
            "    v1 --> v2\n    v2 --> v3\n    v3 --> v4\n    v4 --> v5\n"
            "    v5 --> v6\n    v1 --> v6\n    v3 --> v6\n"
        )

        result = convert_nextflow_dag(text)

        assert len(parse_metro_mermaid(result).lines) == 3
        bypass = re.search(r"fastqc -->\|(\w+)\| final", result)
        second_bypass = re.search(r"multiqc -->\|(\w+)\| final", result)
        assert bypass and second_bypass
        assert bypass.group(1) != second_bypass.group(1)
