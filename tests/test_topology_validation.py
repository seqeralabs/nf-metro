"""Parametrized topology stress tests for the auto-layout engine.

Loads diverse .mmd fixtures, runs layout, and validates programmatically
for layout defects. Also includes topology-specific assertions.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from layout_validator import (
    Severity,
    check_almost_horizontal_edges,
    check_coordinate_sanity,
    check_edge_section_crossing,
    check_edge_waypoints,
    check_port_boundary,
    check_section_overlap,
    check_station_containment,
    validate_layout,
)

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid

TOPOLOGIES_DIR = Path(__file__).parent / "fixtures" / "topologies"
EXAMPLES_DIR = Path(__file__).parent.parent / "examples"

# Collect all topology fixtures
TOPOLOGY_FILES = sorted(TOPOLOGIES_DIR.glob("*.mmd"))
TOPOLOGY_IDS = [f.stem for f in TOPOLOGY_FILES]

# Include examples as regression guards
RNASEQ_FILE = EXAMPLES_DIR / "rnaseq_sections.mmd"
EPITOPEPREDICTION_FILE = EXAMPLES_DIR / "epitopeprediction.mmd"
HLATYPING_FILE = EXAMPLES_DIR / "hlatyping.mmd"


def _load_and_layout(path: Path, max_station_columns: int = 15):
    """Parse a .mmd file and run layout."""
    text = path.read_text()
    graph = parse_metro_mermaid(text, max_station_columns=max_station_columns)
    compute_layout(graph)
    return graph


# --- Parametrized validation across all topologies ---


@pytest.fixture(params=TOPOLOGY_FILES, ids=TOPOLOGY_IDS)
def topology_graph(request):
    """Load and lay out each topology fixture."""
    return _load_and_layout(request.param)


class TestTopologyValidation:
    """Run all validator checks against every topology."""

    def test_no_section_overlap(self, topology_graph):
        violations = check_section_overlap(topology_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_station_containment(self, topology_graph):
        violations = check_station_containment(topology_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_port_boundary(self, topology_graph):
        violations = check_port_boundary(topology_graph)
        # Port boundary is a warning, but we still flag issues
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_coordinate_sanity(self, topology_graph):
        violations = check_coordinate_sanity(topology_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_edge_waypoints(self, topology_graph):
        violations = check_edge_waypoints(topology_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_no_edge_section_crossing(self, topology_graph):
        violations = check_edge_section_crossing(topology_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_no_almost_horizontal_edges(self, topology_graph):
        violations = check_almost_horizontal_edges(topology_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_all_stations_have_coordinates(self, topology_graph):
        """Every real station should have been assigned non-default coords."""
        for sid, station in topology_graph.stations.items():
            if station.is_port or sid in topology_graph.junctions:
                continue
            if station.section_id is None:
                continue
            # At least one coordinate should be non-zero (offset is >= 80)
            assert station.x != 0 or station.y != 0, (
                f"Station '{sid}' still at origin (0,0)"
            )


# --- Regression guard: rnaseq example ---


class TestRnaseqRegression:
    """Ensure the rnaseq example passes all layout checks."""

    @pytest.fixture
    def rnaseq_graph(self):
        return _load_and_layout(RNASEQ_FILE)

    def test_no_section_overlap(self, rnaseq_graph):
        violations = check_section_overlap(rnaseq_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_station_containment(self, rnaseq_graph):
        violations = check_station_containment(rnaseq_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_coordinate_sanity(self, rnaseq_graph):
        violations = check_coordinate_sanity(rnaseq_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_edge_waypoints(self, rnaseq_graph):
        violations = check_edge_waypoints(rnaseq_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_all_sections_placed(self, rnaseq_graph):
        """All 5 rnaseq sections should have valid bounding boxes."""
        assert len(rnaseq_graph.sections) == 5
        for sid, section in rnaseq_graph.sections.items():
            assert section.bbox_w > 0, f"Section '{sid}' has zero width"
            assert section.bbox_h > 0, f"Section '{sid}' has zero height"


class TestEpitopepredictionRegression:
    """Ensure the epitopeprediction example passes all layout checks."""

    @pytest.fixture
    def epitopeprediction_graph(self):
        return _load_and_layout(EPITOPEPREDICTION_FILE)

    def test_no_section_overlap(self, epitopeprediction_graph):
        violations = check_section_overlap(epitopeprediction_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_station_containment(self, epitopeprediction_graph):
        violations = check_station_containment(epitopeprediction_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_coordinate_sanity(self, epitopeprediction_graph):
        violations = check_coordinate_sanity(epitopeprediction_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_edge_waypoints(self, epitopeprediction_graph):
        violations = check_edge_waypoints(epitopeprediction_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_all_sections_placed(self, epitopeprediction_graph):
        """All 3 epitopeprediction sections should have valid bounding boxes."""
        assert len(epitopeprediction_graph.sections) == 3
        for sid, section in epitopeprediction_graph.sections.items():
            assert section.bbox_w > 0, f"Section '{sid}' has zero width"
            assert section.bbox_h > 0, f"Section '{sid}' has zero height"


class TestHlatypingRegression:
    """Ensure the hlatyping example passes all layout checks."""

    @pytest.fixture
    def hlatyping_graph(self):
        return _load_and_layout(HLATYPING_FILE)

    def test_no_section_overlap(self, hlatyping_graph):
        violations = check_section_overlap(hlatyping_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_station_containment(self, hlatyping_graph):
        violations = check_station_containment(hlatyping_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_coordinate_sanity(self, hlatyping_graph):
        violations = check_coordinate_sanity(hlatyping_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_edge_waypoints(self, hlatyping_graph):
        violations = check_edge_waypoints(hlatyping_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_all_sections_placed(self, hlatyping_graph):
        """All 3 hlatyping sections should have valid bounding boxes."""
        assert len(hlatyping_graph.sections) == 3
        for sid, section in hlatyping_graph.sections.items():
            assert section.bbox_w > 0, f"Section '{sid}' has zero width"
            assert section.bbox_h > 0, f"Section '{sid}' has zero height"


# --- Topology-specific assertions ---


class TestTopologySpecific:
    """Targeted assertions for individual topologies."""

    def test_fan_out_creates_junction(self):
        graph = _load_and_layout(TOPOLOGIES_DIR / "wide_fan_out.mmd")
        # With 4 targets from one source, we expect junction(s)
        assert len(graph.junctions) > 0, "Fan-out should create junction stations"

    def test_fan_out_has_5_sections(self):
        graph = _load_and_layout(TOPOLOGIES_DIR / "wide_fan_out.mmd")
        assert len(graph.sections) == 5

    def test_fan_in_has_5_sections(self):
        graph = _load_and_layout(TOPOLOGIES_DIR / "wide_fan_in.mmd")
        assert len(graph.sections) == 5

    def test_deep_linear_has_7_sections(self):
        graph = _load_and_layout(TOPOLOGIES_DIR / "deep_linear.mmd")
        assert len(graph.sections) == 7
        # Sections should progress left to right (or with fold)
        violations = validate_layout(graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_parallel_independent_separate_rows(self):
        graph = _load_and_layout(TOPOLOGIES_DIR / "parallel_independent.mmd")
        # DNA and RNA chains should not overlap
        violations = check_section_overlap(graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)
        # Should have 4 sections
        assert len(graph.sections) == 4

    def test_diamond_grid_structure(self):
        graph = _load_and_layout(TOPOLOGIES_DIR / "section_diamond.mmd")
        # 4 sections: start, branch_left, branch_right, finish
        assert len(graph.sections) == 4
        # Start should be in col 0, branches in col 1, finish in col 2
        start = graph.sections["start"]
        bl = graph.sections["branch_left"]
        br = graph.sections["branch_right"]
        finish = graph.sections["finish"]
        assert start.grid_col < bl.grid_col
        assert start.grid_col < br.grid_col
        assert bl.grid_col < finish.grid_col
        assert br.grid_col < finish.grid_col

    def test_diamond_branches_different_rows(self):
        graph = _load_and_layout(TOPOLOGIES_DIR / "section_diamond.mmd")
        bl = graph.sections["branch_left"]
        br = graph.sections["branch_right"]
        # Branches should be stacked vertically (different rows)
        assert bl.grid_row != br.grid_row

    def test_single_section_no_ports(self):
        graph = _load_and_layout(TOPOLOGIES_DIR / "single_section.mmd")
        assert len(graph.sections) == 1
        # Single section with no inter-section edges should have no ports
        assert len(graph.ports) == 0
        assert len(graph.junctions) == 0

    def test_single_section_valid(self):
        graph = _load_and_layout(TOPOLOGIES_DIR / "single_section.mmd")
        violations = validate_layout(graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_asymmetric_tree_sections(self):
        graph = _load_and_layout(TOPOLOGIES_DIR / "asymmetric_tree.mmd")
        # 7 sections total
        assert len(graph.sections) == 7
        violations = validate_layout(graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_mixed_port_sides_structure(self):
        graph = _load_and_layout(TOPOLOGIES_DIR / "mixed_port_sides.mmd")
        assert len(graph.sections) == 3
        violations = validate_layout(graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_multi_line_bundle_all_6_lines(self):
        graph = _load_and_layout(TOPOLOGIES_DIR / "multi_line_bundle.mmd")
        assert len(graph.lines) == 6
        assert len(graph.sections) == 3
        violations = validate_layout(graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_complex_multipath_structure(self):
        graph = _load_and_layout(TOPOLOGIES_DIR / "complex_multipath.mmd")
        assert len(graph.sections) == 6
        assert len(graph.lines) == 4
        violations = validate_layout(graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_rnaseq_lite_structure(self):
        graph = _load_and_layout(TOPOLOGIES_DIR / "rnaseq_lite.mmd")
        assert len(graph.sections) == 5
        assert len(graph.lines) == 3
        violations = validate_layout(graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_rnaseq_lite_top_alignment(self):
        """Same-row sections in rnaseq_lite should share the same top edge."""
        graph = _load_and_layout(TOPOLOGIES_DIR / "rnaseq_lite.mmd")
        # Group sections by grid_row
        rows: dict[int, list] = {}
        for sid, sec in graph.sections.items():
            rows.setdefault(sec.grid_row, []).append((sid, sec))
        # For each row with multiple sections, check top edges are flush
        for row, secs in rows.items():
            if len(secs) <= 1:
                continue
            top_ys = [(sid, sec.bbox_y) for sid, sec in secs]
            ref_y = top_ys[0][1]
            for sid, y in top_ys[1:]:
                assert abs(y - ref_y) < 1.0, (
                    f"Row {row}: {sid} bbox_y={y} differs from "
                    f"{top_ys[0][0]} bbox_y={ref_y} (not top-aligned)"
                )

    def test_variant_calling_structure(self):
        graph = _load_and_layout(TOPOLOGIES_DIR / "variant_calling.mmd")
        assert len(graph.sections) == 6
        assert len(graph.lines) == 4
        violations = validate_layout(graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    # --- Fold topology tests ---

    def test_fold_fan_across_structure(self):
        """Fan-out/fan-in across a fold boundary."""
        graph = _load_and_layout(TOPOLOGIES_DIR / "fold_fan_across.mmd")
        assert len(graph.sections) == 7
        assert len(graph.lines) == 3

        # normalize is the fold section (TB direction, rowspan=3 covering
        # the 3 quant rows but not the return row)
        normalize = graph.sections["normalize"]
        assert normalize.direction == "TB"
        assert normalize.grid_row_span == 3

        # Three quant sections stacked at the same column
        tmt = graph.sections["tmt_quant"]
        lfq = graph.sections["lfq_quant"]
        dia = graph.sections["dia_quant"]
        assert tmt.grid_col == lfq.grid_col == dia.grid_col
        assert len({tmt.grid_row, lfq.grid_row, dia.grid_row}) == 3

        # stat_analysis is RL (post-fold return row)
        stat = graph.sections["stat_analysis"]
        assert stat.direction == "RL"

        # All grid_cols are non-negative
        for sid, sec in graph.sections.items():
            assert sec.grid_col >= 0, f"{sid} has negative grid_col={sec.grid_col}"

        violations = validate_layout(graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_fold_double_structure(self):
        """Double fold producing a serpentine (zigzag) layout."""
        graph = _load_and_layout(TOPOLOGIES_DIR / "fold_double.mmd")
        assert len(graph.sections) == 10
        assert len(graph.lines) == 2

        # Two fold sections (TB direction)
        calling = graph.sections["calling"]
        integration = graph.sections["integration"]
        assert calling.direction == "TB"
        assert integration.direction == "TB"

        # Serpentine: row 0 (LR), row 1 (RL), row 2 (LR)
        row0_secs = [s for s in graph.sections.values() if s.grid_row == 0]
        row1_secs = [s for s in graph.sections.values() if s.grid_row == 1]
        row2_secs = [s for s in graph.sections.values() if s.grid_row == 2]
        assert len(row0_secs) == 4  # input_qc, alignment, base_recal, calling
        assert len(row1_secs) == 4  # hard_filter .. integration
        assert len(row2_secs) == 2  # reporting, archival

        # Row 1 post-fold sections flow RL
        hard_filter = graph.sections["hard_filter"]
        annotation = graph.sections["annotation"]
        interpretation = graph.sections["interpretation"]
        assert hard_filter.direction == "RL"
        assert annotation.direction == "RL"
        assert interpretation.direction == "RL"

        # Row 2 post-second-fold sections flow LR
        reporting = graph.sections["reporting"]
        archival = graph.sections["archival"]
        assert reporting.direction == "LR"
        assert archival.direction == "LR"

        # Negative grid_cols are valid: the return row may extend past
        # column 0 when there are more sections than columns. Section
        # placement handles negative columns correctly.
        assert integration.grid_col <= 0  # leftmost section on return row

        violations = validate_layout(graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_fold_stacked_branch_structure(self):
        """Stacked sections near fold + post-fold branching."""
        graph = _load_and_layout(TOPOLOGIES_DIR / "fold_stacked_branch.mmd")
        assert len(graph.sections) == 8
        assert len(graph.lines) == 3

        # integration is fold section (TB, rowspan=3)
        integration = graph.sections["integration"]
        assert integration.direction == "TB"
        assert integration.grid_row_span == 3

        # Three analysis sections stacked at same column
        rna = graph.sections["rna_analysis"]
        atac = graph.sections["atac_analysis"]
        prot = graph.sections["protein_analysis"]
        assert rna.grid_col == atac.grid_col == prot.grid_col
        assert len({rna.grid_row, atac.grid_row, prot.grid_row}) == 3

        # bio_interp and tech_qc are post-fold, stacked at same column
        bio = graph.sections["bio_interp"]
        tech = graph.sections["tech_qc"]
        assert bio.grid_col == tech.grid_col
        assert bio.grid_row != tech.grid_row

        # bio_interp is RL (post-fold, successor to left)
        assert bio.direction == "RL"

        # All grid_cols are non-negative
        for sid, sec in graph.sections.items():
            assert sec.grid_col >= 0, f"{sid} has negative grid_col={sec.grid_col}"

        violations = validate_layout(graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)


# --- Reflow (max_station_columns) tests ---

# Topologies with enough sections to exercise reflow at various widths.
REFLOW_FIXTURES = ["deep_linear", "fold_double"]
REFLOW_WIDTHS = [6, 8, 10]


class TestReflowValidation:
    """Validate layout correctness when topologies are reflowed at reduced widths."""

    @pytest.fixture(
        params=[(name, width) for name in REFLOW_FIXTURES for width in REFLOW_WIDTHS],
        ids=[
            f"{name}_cols{width}" for name in REFLOW_FIXTURES for width in REFLOW_WIDTHS
        ],
    )
    def reflow_graph(self, request):
        name, width = request.param
        return _load_and_layout(
            TOPOLOGIES_DIR / f"{name}.mmd", max_station_columns=width
        )

    def test_no_section_overlap(self, reflow_graph):
        violations = check_section_overlap(reflow_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_station_containment(self, reflow_graph):
        violations = check_station_containment(reflow_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_coordinate_sanity(self, reflow_graph):
        violations = check_coordinate_sanity(reflow_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_edge_waypoints(self, reflow_graph):
        violations = check_edge_waypoints(reflow_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)


class TestReflowStructure:
    """Verify that reducing max_station_columns produces more folds."""

    def test_deep_linear_reflow_adds_folds(self):
        """Narrower width produces more rows."""
        graph_wide = _load_and_layout(
            TOPOLOGIES_DIR / "deep_linear.mmd", max_station_columns=15
        )
        graph_narrow = _load_and_layout(
            TOPOLOGIES_DIR / "deep_linear.mmd", max_station_columns=6
        )
        wide_rows = {s.grid_row for s in graph_wide.sections.values()}
        narrow_rows = {s.grid_row for s in graph_narrow.sections.values()}
        assert len(narrow_rows) > len(wide_rows)

    def test_deep_linear_narrow_has_tb_fold(self):
        """At max_station_columns=6, deep_linear should have TB fold sections."""
        graph = _load_and_layout(
            TOPOLOGIES_DIR / "deep_linear.mmd", max_station_columns=6
        )
        tb_sections = [sid for sid, s in graph.sections.items() if s.direction == "TB"]
        assert len(tb_sections) >= 1

    def test_fold_double_more_folds_at_narrow_width(self):
        """fold_double at width 6 should produce more fold sections than default."""
        graph_default = _load_and_layout(
            TOPOLOGIES_DIR / "fold_double.mmd", max_station_columns=15
        )
        graph_narrow = _load_and_layout(
            TOPOLOGIES_DIR / "fold_double.mmd", max_station_columns=6
        )
        default_folds = sum(
            1 for s in graph_default.sections.values() if s.direction == "TB"
        )
        narrow_folds = sum(
            1 for s in graph_narrow.sections.values() if s.direction == "TB"
        )
        assert narrow_folds >= default_folds


GENOMEASSEMBLY_FILE = EXAMPLES_DIR / "genomeassembly.mmd"


class TestMergeJunctions:
    """Tests for merge junction insertion and positioning (#207)."""

    def test_merge_junctions_created(self):
        """genomeassembly should have merge junctions for convergent assembly lines."""
        graph = _load_and_layout(GENOMEASSEMBLY_FILE)
        merge_ids = [j for j in graph.junctions if j.startswith("__merge_")]
        assert len(merge_ids) > 0, "Expected merge junctions for convergent edges"

    def test_merge_junction_count(self):
        """Convergent assembly lines create merge junctions."""
        graph = _load_and_layout(GENOMEASSEMBLY_FILE)
        merge_ids = [j for j in graph.junctions if j.startswith("__merge_")]
        # scaffolding + genome_stats each get convergent assemblies
        assert len(merge_ids) >= 2, (
            f"Expected >= 2 merge junctions, got {len(merge_ids)}"
        )

    def test_merge_junction_has_correct_section(self):
        """Merge junctions should have section_id set to the target section."""
        graph = _load_and_layout(GENOMEASSEMBLY_FILE)
        for jid in graph.junctions:
            if not jid.startswith("__merge_"):
                continue
            junction = graph.stations[jid]
            assert junction.section_id is not None, (
                f"Merge junction {jid} should have a section_id"
            )
            # Verify it matches the successor entry port's section
            for edge in graph.edges:
                if edge.source == jid:
                    tgt_port = graph.ports.get(edge.target)
                    if tgt_port and tgt_port.is_entry:
                        assert junction.section_id == tgt_port.section_id, (
                            f"Merge junction {jid} section_id {junction.section_id} "
                            f"doesn't match entry port section {tgt_port.section_id}"
                        )

    def test_merge_junction_connectivity(self):
        """Merge junctions have N>1 preds and 1 entry port succ."""
        graph = _load_and_layout(GENOMEASSEMBLY_FILE)
        for jid in graph.junctions:
            if not jid.startswith("__merge_"):
                continue
            preds = [e.source for e in graph.edges if e.target == jid]
            succs = [e.target for e in graph.edges if e.source == jid]
            assert len(preds) > 1, (
                f"Merge junction {jid} should have >1 predecessors, got {len(preds)}"
            )
            assert len(succs) == 1, (
                f"Merge junction {jid} should have 1 successor, got {len(succs)}"
            )
            succ_port = graph.ports.get(succs[0])
            assert succ_port is not None and succ_port.is_entry, (
                f"Merge junction {jid} successor should be an entry port"
            )

    def test_merge_junction_positioned_near_entry(self):
        """Merge junctions should be positioned near their entry port."""
        graph = _load_and_layout(GENOMEASSEMBLY_FILE)
        for jid in graph.junctions:
            if not jid.startswith("__merge_"):
                continue
            junction = graph.stations[jid]
            # Find entry port successor
            for edge in graph.edges:
                if edge.source == jid:
                    tgt = graph.stations.get(edge.target)
                    if tgt and graph.ports.get(edge.target):
                        # Y should match entry port
                        assert abs(junction.y - tgt.y) < 1.0, (
                            f"Merge junction {jid} Y={junction.y} should match "
                            f"entry port Y={tgt.y}"
                        )
                        # X should be to the left of entry port (for LEFT entry)
                        assert junction.x < tgt.x, (
                            f"Merge junction {jid} X={junction.x} should be left "
                            f"of entry port X={tgt.x}"
                        )

    def test_genomeassembly_passes_validation(self):
        """genomeassembly example should pass all layout validation checks."""
        graph = _load_and_layout(GENOMEASSEMBLY_FILE)
        violations = validate_layout(graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)


class TestMergeRouting:
    """Tests for merge trunk/branch routing patterns (#207)."""

    @staticmethod
    def _routes(graph):
        from nf_metro.layout.routing import (
            compute_station_offsets,
            route_edges,
        )

        offsets = compute_station_offsets(graph)
        return route_edges(graph, station_offsets=offsets)

    def test_trunk_reaches_entry_port(self):
        """Trunk route's last point should match the entry port."""
        graph = _load_and_layout(GENOMEASSEMBLY_FILE)
        routes = self._routes(graph)
        merge_ids = {j for j in graph.junctions if j.startswith("__merge_")}
        for r in routes:
            if r.edge.target in merge_ids and len(r.points) == 6:
                # Find entry port for this merge junction
                for e in graph.edges:
                    if e.source == r.edge.target:
                        ep = graph.ports.get(e.target)
                        if ep and ep.is_entry:
                            ep_st = graph.stations[e.target]
                            last = r.points[-1]
                            assert abs(last[0] - ep_st.x) < 1, (
                                f"Trunk to {r.edge.target} ends at "
                                f"x={last[0]:.0f}, entry at "
                                f"x={ep_st.x:.0f}"
                            )

    def test_branch_is_4_point_descent(self):
        """Branch routes should be 4-point L-shape descents."""
        graph = _load_and_layout(GENOMEASSEMBLY_FILE)
        routes = self._routes(graph)
        merge_ids = {j for j in graph.junctions if j.startswith("__merge_")}
        for r in routes:
            if r.edge.target in merge_ids and len(r.points) != 6 and len(r.points) != 2:
                assert len(r.points) == 4, (
                    f"Branch {r.edge.source}->{r.edge.target} "
                    f"has {len(r.points)} points, expected 4"
                )

    def test_no_backward_segments(self):
        """No merge route should have backward (decreasing X) segments."""
        graph = _load_and_layout(GENOMEASSEMBLY_FILE)
        routes = self._routes(graph)
        merge_ids = {j for j in graph.junctions if j.startswith("__merge_")}
        for r in routes:
            if r.edge.target not in merge_ids:
                continue
            for k in range(len(r.points) - 1):
                x1 = r.points[k][0]
                x2 = r.points[k + 1][0]
                assert x2 >= x1 - 1, (
                    f"Backward segment in "
                    f"{r.edge.source}->{r.edge.target} "
                    f"seg {k}: x {x1:.0f}->{x2:.0f}"
                )

    def test_bypass_bundle_uses_offset_step(self):
        """Bundled bypass routes should be OFFSET_STEP apart."""
        from nf_metro.layout.constants import OFFSET_STEP

        graph = _load_and_layout(GENOMEASSEMBLY_FILE)
        routes = self._routes(graph)
        # Collect all horizontal segments per line for inter-section
        # bypass routes.  Bypass segments are horizontal runs that sit
        # between the source and target section Y ranges (i.e. below
        # the top-row stations).  We look for pairs at nearby Y values
        # that differ by exactly OFFSET_STEP.
        horiz_by_line: dict[str, list[float]] = {}
        for r in routes:
            if not r.is_inter_section:
                continue
            for k in range(len(r.points) - 1):
                y = r.points[k][1]
                if y > 200 and abs(r.points[k][1] - r.points[k + 1][1]) < 1:
                    horiz_by_line.setdefault(r.line_id, []).append(y)
        a_ys = horiz_by_line.get("assemblies", [])
        h_ys = horiz_by_line.get("hic_reads", [])
        if a_ys and h_ys:
            # Find the pair of horizontal segments closest to each other
            min_gap = min(abs(a - h) for a in a_ys for h in h_ys if abs(a - h) > 0)
            assert min_gap == OFFSET_STEP, (
                f"Bypass bundle gap {min_gap}px, expected {OFFSET_STEP}px"
            )


class TestAlmostHorizontalEdges:
    """Regression tests for almost-horizontal edge detection (#209).

    Uses real-world examples that are known to trigger offset mismatches
    between single-line and multi-line stations at the same Y.
    """

    def test_genomeassembly_no_slope(self):
        """The genomeassembly example (the original #209 report) should be clean."""
        graph = _load_and_layout(GENOMEASSEMBLY_FILE)
        violations = check_almost_horizontal_edges(graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_variant_calling_no_slope(self):
        """The variant_calling example should be clean."""
        graph = _load_and_layout(EXAMPLES_DIR / "variant_calling.mmd")
        violations = check_almost_horizontal_edges(graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)
