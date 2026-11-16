"""Tests for Nextflow DAG conversion regressions."""

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
