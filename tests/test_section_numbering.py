"""Tests for connectivity-aware section numbering.

After layout, automatic sections follow connected visual routes.  Parallel
branches complete before their join, while authored numbers stay fixed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


def _load(name: str) -> MetroGraph:
    """Parse and lay out an example pipeline."""
    text = (EXAMPLES_DIR / f"{name}.mmd").read_text()
    g = parse_metro_mermaid(text)
    compute_layout(g)
    return g


def _ordered_section_ids(graph: MetroGraph, *, row: int | None = None) -> list[str]:
    return [
        section.id
        for section in sorted(
            (
                section
                for section in graph.sections.values()
                if row is None or section.grid_row == row
            ),
            key=lambda section: section.number,
        )
    ]


# Module-scoped fixtures to avoid redundant compute_layout calls.


@pytest.fixture(scope="module")
def variantprioritization():
    return _load("variantprioritization")


@pytest.fixture(scope="module")
def rnaseq_auto():
    return _load("rnaseq_auto")


@pytest.fixture(scope="module")
def longread_variant_calling():
    return _load("longread_variant_calling")


@pytest.fixture(scope="module")
def leftward_bypass():
    return _load("topologies/bypass_left_entry_from_right")


@pytest.fixture(scope="module")
def asymmetric_tree():
    return _load("topologies/asymmetric_tree")


class TestSectionNumberingOrder:
    """Automatic numbers should follow connected visual routes."""

    def test_numbers_are_sequential(self, variantprioritization):
        """Section numbers should be 1..N with no gaps."""
        numbers = sorted(s.number for s in variantprioritization.sections.values())
        assert numbers == list(range(1, len(variantprioritization.sections) + 1))

    def test_all_examples_sequential_with_only_cross_row_route_returns(self):
        """Backward edges should only rejoin a completed route from another row."""
        for mmd_path in sorted(EXAMPLES_DIR.rglob("*.mmd")):
            text = mmd_path.read_text()
            g = parse_metro_mermaid(text)
            if not g.sections:
                continue
            compute_layout(g)
            drawn = {sid for sid, s in g.sections.items() if s.draws_box}
            numbers = sorted(g.sections[sid].number for sid in drawn)
            assert numbers == list(range(1, len(drawn) + 1)), (
                f"{mmd_path.name}: section numbers not sequential: {numbers}"
            )
            section_edges = {
                (source, target)
                for source, target in (
                    g.section_dag.section_edges if g.section_dag else set()
                )
                if source in drawn and target in drawn
            }

            def lane(sid: str) -> int:
                return g.sections[sid].grid_row

            for source, target in section_edges:
                if g.sections[source].number < g.sections[target].number:
                    continue
                earlier_predecessors = [
                    predecessor
                    for predecessor, candidate in section_edges
                    if candidate == target
                    and g.sections[predecessor].number < g.sections[target].number
                ]
                assert lane(source) != lane(target)
                assert any(
                    lane(predecessor) == lane(target)
                    for predecessor in earlier_predecessors
                ), (
                    f"{mmd_path.name}: {source!r} rejoins {target!r} without an "
                    "earlier predecessor continuing along the target row"
                )

    def test_connected_routes_precede_secondary_inputs(self, variantprioritization):
        assert _ordered_section_ids(variantprioritization) == [
            "preprocessing",
            "format_files",
            "get_reference",
            "run_cpsr",
            "run_pcgr",
        ]

    def test_longread_primary_route_precedes_secondary_inputs(
        self, longread_variant_calling
    ):
        assert _ordered_section_ids(longread_variant_calling) == [
            "preprocessing",
            "small_variants",
            "phasing",
            "structural_variants",
            "jointcalling",
            "annotation",
            "cnv_calling",
            "tr_calling",
            "reports",
        ]

    def test_connected_flow_precedes_a_disconnected_rowmate(self, leftward_bypass):
        assert _ordered_section_ids(leftward_bypass, row=0) == [
            "source",
            "target",
            "blocker",
        ]

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            (
                "guide/03_fan_out",
                [
                    "preprocessing",
                    "wgs_analysis",
                    "wes_analysis",
                    "panel_analysis",
                    "annotation",
                ],
            ),
            (
                "guide/04_directions",
                [
                    "preprocessing",
                    "rna_analysis",
                    "dna_analysis",
                    "postprocessing",
                    "reporting",
                ],
            ),
            (
                "rnaseq_auto",
                [
                    "preprocessing",
                    "genome_align",
                    "pseudo_align",
                    "postprocessing",
                    "qc_report",
                ],
            ),
            (
                "topologies/around_below_ep_col_gt0",
                ["source", "middle", "target"],
            ),
            (
                "topologies/around_section_below",
                ["source", "middle", "target"],
            ),
            (
                "topologies/corridor_narrow_gap_fallback",
                ["source", "tall", "target"],
            ),
            (
                "topologies/bottom_row_climb_clear_corridor",
                ["feed", "mid_a", "mid_b", "dest", "low"],
            ),
            (
                "topologies/convergence_stacked_sink",
                ["prep", "align", "dedup", "aux", "repeats", "merge_pt", "report"],
            ),
            (
                "topologies/junction_entry_align",
                ["pre", "src", "dst_a", "dst_b", "beta_extra_1", "beta_extra_2"],
            ),
            (
                "topologies/merge_trunk_over_low_section",
                ["ingest", "tall", "proc2", "collect", "sub"],
            ),
        ],
    )
    def test_reviewed_render_order(self, name, expected):
        graph = _load(name)
        assert _ordered_section_ids(graph) == expected

    def test_authored_number_is_reserved_while_automatic_numbers_fill_gaps(self):
        text = (
            "%%metro line: main | Main | #ff0000\n"
            "graph LR\n"
            "    subgraph first [First]\n"
            "        a[A]\n"
            "    end\n"
            "    subgraph second [Second]\n"
            "        %%metro number: 7\n"
            "        b[B]\n"
            "    end\n"
            "    subgraph third [Third]\n"
            "        c[C]\n"
            "    end\n"
            "    a -->|main| b\n"
            "    b -->|main| c\n"
        )
        graph = parse_metro_mermaid(text)
        compute_layout(graph)
        assert {sid: section.number for sid, section in graph.sections.items()} == {
            "first": 1,
            "second": 7,
            "third": 2,
        }

    def test_fold_return_row_numbered_after_forward_row(self, rnaseq_auto):
        """RL sections after a fold should have higher numbers than all
        LR sections in the preceding sweep."""
        lr_nums = [
            s.number
            for s in rnaseq_auto.sections.values()
            if s.direction in ("LR", "TB")
        ]
        rl_nums = [
            s.number for s in rnaseq_auto.sections.values() if s.direction == "RL"
        ]
        if lr_nums and rl_nums:
            assert min(rl_nums) > max(lr_nums), (
                f"RL sections {rl_nums} should all be > LR/TB sections {lr_nums}"
            )

    def test_asymmetric_top_row_sequential(self, asymmetric_tree):
        top_row = sorted(
            (s for s in asymmetric_tree.sections.values() if s.grid_row == 0),
            key=lambda s: s.grid_col,
        )
        nums = [s.number for s in top_row]
        for i in range(len(nums) - 1):
            assert nums[i] < nums[i + 1], f"Top row numbers not increasing: {nums}"
