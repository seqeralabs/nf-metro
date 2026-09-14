"""Parametrized topology stress tests for the auto-layout engine.

Loads diverse .mmd fixtures, runs layout, and validates programmatically
for layout defects. Also includes topology-specific assertions.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from layout_validator import (
    Severity,
    check_almost_horizontal_edges,
    check_coincident_stations,
    check_coordinate_sanity,
    check_edge_section_crossing,
    check_edge_waypoints,
    check_excessive_column_gaps,
    check_exit_port_feeder_alignment,
    check_intra_section_chain_alignment,
    check_perp_entry_precedes_flow_start,
    check_port_boundary,
    check_route_segment_crossings,
    check_section_overlap,
    check_single_segment_diagonals,
    check_station_containment,
    validate_layout,
)

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.geometry import perpendicular_port_sides
from nf_metro.layout.phases._common import _is_fan_branch_leaf
from nf_metro.layout.routing.common import row_bottom_edge
from nf_metro.layout.routing.context import _resolve_section_row
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
TOPOLOGIES_DIR = EXAMPLES_DIR / "topologies"

# Collect all topology fixtures
TOPOLOGY_FILES = sorted(TOPOLOGIES_DIR.glob("*.mmd"))
TOPOLOGY_IDS = [f.stem for f in TOPOLOGY_FILES]

# Include examples as regression guards
RNASEQ_FILE = EXAMPLES_DIR / "rnaseq_sections.mmd"
EPITOPEPREDICTION_FILE = EXAMPLES_DIR / "epitopeprediction.mmd"
HLATYPING_FILE = EXAMPLES_DIR / "hlatyping.mmd"
TB_FILE_TERMINI_FILE = EXAMPLES_DIR / "tb_file_termini.mmd"


def _load_and_layout(path: Path, max_station_columns: int = 15):
    """Parse a .mmd file and run layout."""
    text = path.read_text()
    graph = parse_metro_mermaid(text, max_station_columns=max_station_columns)
    graph.source_dir = str(path.parent)
    compute_layout(graph)
    return graph


def _compute_routes(graph):
    """Compute station offsets and route all edges."""
    from nf_metro.layout.routing import compute_station_offsets, route_edges

    offsets = compute_station_offsets(graph)
    return route_edges(graph, station_offsets=offsets)


# --- Parametrized validation across all topologies ---


_ERROR_VALIDATORS = (
    check_section_overlap,
    check_station_containment,
    check_port_boundary,
    check_coordinate_sanity,
    check_coincident_stations,
    check_edge_waypoints,
    check_edge_section_crossing,
    check_perp_entry_precedes_flow_start,
    check_intra_section_chain_alignment,
    check_exit_port_feeder_alignment,
)


@pytest.mark.parametrize("path", TOPOLOGY_FILES, ids=TOPOLOGY_IDS)
def test_topology_validation(path: Path) -> None:
    """Run every read-only layout validator against one settled topology."""
    graph = _load_and_layout(path)
    problems: list[str] = []
    for validator in _ERROR_VALIDATORS:
        problems.extend(
            f"{validator.__name__}: {violation.message}"
            for violation in validator(graph)
            if violation.severity == Severity.ERROR
        )
    problems.extend(
        f"{check_almost_horizontal_edges.__name__}: {violation.message}"
        for violation in check_almost_horizontal_edges(graph)
        if violation.severity == Severity.WARNING
    )
    problems.extend(
        f"station {sid!r} remains at the origin"
        for sid, station in graph.stations.items()
        if not station.is_port
        and sid not in graph.junctions
        and station.section_id is not None
        and station.x == 0
        and station.y == 0
    )
    assert not problems, "\n".join(problems)


# --- Serpentine stacked-section invariant (issue #421) ---

# Fixtures known to contain a serpentine run of stacked, chained,
# same-direction single-cell sections in one grid column. The new
# stacked_lr_serpentine fixture is the targeted case; variantbenchmarking_auto
# is an existing gallery pipeline whose Variant Filtering -> Benchmarking
# sections stack in one column, so the invariant must generalise to it.
SERPENTINE_FILES = [
    TOPOLOGIES_DIR / "stacked_lr_serpentine.mmd",
    EXAMPLES_DIR / "variantbenchmarking_auto.mmd",
]
SERPENTINE_IDS = [f.stem for f in SERPENTINE_FILES]


@pytest.mark.parametrize("path", SERPENTINE_FILES, ids=SERPENTINE_IDS)
def test_stacked_sections_serpentine_no_backtrack(path):
    """Stacked same-direction sections must connect via vertical drops.

    Each section in a detected serpentine run must flow internally without
    folding its route back across the section width: a section that fails to
    alternate direction would enter on the wrong side and wrap around. This
    test fails on the pre-#421 engine, which inferred LR for every stacked
    section regardless of position.
    """
    from layout_validator import check_serpentine_no_backtrack

    from nf_metro.layout.auto_layout import detect_serpentine_runs

    graph = _load_and_layout(path)

    dag = graph.section_dag
    assert dag is not None
    runs = detect_serpentine_runs(graph, dag.successors, dag.predecessors)
    assert runs, f"{path.stem}: expected at least one serpentine run to exist"

    violations = check_serpentine_no_backtrack(graph)
    errors = [v for v in violations if v.severity == Severity.ERROR]
    assert not errors, "\n".join(v.message for v in errors)


# --- Regression guard: funcprofiler_upstream reporting line ---
#
# funcprofiler_upstream is a real upstream pipeline (11 lines, 7 parallel
# profilers) whose reporting (multiqc) line is the through-trunk line: it
# visits MultiQC on the trunk between the profiling and output sections. The
# exit- and entry-port trunk-slot ordering keeps that merge line on the trunk
# end to end, so it runs level rather than diving through the fan bundle at
# either port boundary.

FUNCPROFILER_FIXTURE = TOPOLOGIES_DIR / "funcprofiler_upstream.mmd"


class TestFuncprofilerUpstreamReportingLine:
    """The reporting line rides the trunk through funcprofiler_upstream."""

    @pytest.fixture
    def graph(self):
        return _load_and_layout(FUNCPROFILER_FIXTURE)

    def test_reporting_line_no_qc_to_output_diagonal(self, graph):
        """The reporting (multiqc) line runs level from Quality Check to Output
        on the trunk, not as a single straight diagonal between the two ports.

        The through-trunk merge line rides the trunk slot at both the QC exit
        and the Output convergence entry, so its inter-section connector runs
        level across each port boundary (#1480).
        """
        violations = check_single_segment_diagonals(graph)
        flagged = {(v.context["source"], v.context["target"]) for v in violations}
        qc_to_output = {
            (s, t) for s, t in flagged if "QC__exit" in s and "Output__entry" in t
        }
        assert not qc_to_output, (
            f"reporting line should run level QC -> Output on the trunk, not a "
            f"single diagonal; got {sorted(flagged)}"
        )


# --- Regression guard: exit_run_three_drop_columns merge feeder ---

EXIT_RUN_THREE_DROP_FIXTURE = TOPOLOGIES_DIR / "exit_run_three_drop_columns.mmd"


class TestExitRunThreeDropColumnsMergeFeeder:
    """The adjacent merge feeder keeps its lane instead of sloping onto the trunk."""

    @pytest.fixture
    def graph(self):
        return _load_and_layout(EXIT_RUN_THREE_DROP_FIXTURE)

    def test_merge_feeder_is_not_a_bare_diagonal(self, graph):
        """No route in this fixture renders as a single sloped segment.

        The sheets feeder leaves section C's exit run one lane below the merge
        trunk and its merge station stands a margin away, so a lane change on the
        run itself has no room to round.  It terminates on the trunk's riser
        instead, which crosses its lane.
        """
        violations = check_single_segment_diagonals(graph)
        assert not violations, "\n".join(v.message for v in violations)


# --- variant_calling layout guards ---
#
# variant_calling.mmd pins three layout properties confirmed by eye with the
# user during validator development:
#
# 1. Section 2 (Alignment) chain alignment - bwa_mem is a fan-in (the
#    bwa_index branch and the fastp entry both carry Main into it), so the
#    entry phantom anchors the through-trunk while bwa_index fans in above
#    it, keeping bwa_mem -> samtools_sort -> samtools_index straight.
# 2. Section 3 (Variant Calling) column gaps - GATK HaplotypeCaller and
#    DeepVariant share column x=772 but sit 80px apart with one empty grid
#    row between them.  A live defect, held below as a strict xfail.
# 3. Section 1 -> Section 2/4 inter-section crossings - Main and QC
#    Reporting both fan out from junction __junction_6, and the straight
#    Alignment trunk keeps them from crossing en route to their targets.

VARIANT_CALLING_FILE = EXAMPLES_DIR / "variant_calling.mmd"


_VARIANT_CALLING_XFAIL = pytest.mark.xfail(
    strict=True,
    reason="known variant_calling layout defect; tracked in #1863",
)


class TestVariantCallingDefects:
    """Lock in known variant_calling layout defects via strict xfail.

    Each defect held here is currently present; when an engine fix lands
    the matching xfail flips to XPASS and reds CI, prompting the marker
    removal.
    """

    @pytest.fixture
    def graph(self):
        return _load_and_layout(VARIANT_CALLING_FILE)

    def test_no_intra_section_chain_misalignment(self, graph):
        v = check_intra_section_chain_alignment(graph)
        assert not v, "\n".join(vi.message for vi in v)

    @_VARIANT_CALLING_XFAIL
    def test_no_excessive_column_gaps(self, graph):
        v = check_excessive_column_gaps(graph)
        assert not v, "\n".join(vi.message for vi in v)

    def test_no_route_segment_crossings(self, graph):
        v = check_route_segment_crossings(graph)
        assert not v, "\n".join(vi.message for vi in v)


# --- #420: single-line linear chains must stay axis-aligned ---
#
# Parametrised across the whole gallery rather than only variant_calling:
# the zig-zag was a general track-stagger defect (entry-runway phantoms
# fanning out symmetrically with a fan-in branch instead of anchoring the
# trunk), so the invariant must hold for every fixture, not just the one
# that first exposed it.

_CHAIN_ALIGNMENT_FILES = [
    VARIANT_CALLING_FILE,
    RNASEQ_FILE,
    EPITOPEPREDICTION_FILE,
    HLATYPING_FILE,
    *TOPOLOGY_FILES,
]


@pytest.mark.parametrize("mmd_path", _CHAIN_ALIGNMENT_FILES, ids=lambda p: p.stem)
def test_no_intra_section_chain_misalignment_across_gallery(mmd_path):
    """Consecutive same-line stations inside one section run axis-aligned.

    Regression guard for #420 (TB/LR linear-chain zig-zag), generalised
    beyond the variant_calling fixture that first surfaced it.
    """
    graph = _load_and_layout(mmd_path)
    violations = check_intra_section_chain_alignment(graph)
    assert not violations, "\n".join(v.message for v in violations)


# --- #1599: alignment checks read the axis from the port side, not the flow ---

# A port is pinned to the section edge it sits on and can only slide along
# that edge, so the alignable axis follows the side: a LEFT/RIGHT port has a
# fixed x and a free y, a TOP/BOTTOM port the reverse. Spelled out here rather
# than imported from the validator's own helper so the two are independent.
_FREE_AXIS_FOR_PORT_SIDE = {"left": "y", "right": "y", "top": "x", "bottom": "x"}

_PORT_AXIS_FILES = [*_CHAIN_ALIGNMENT_FILES, TB_FILE_TERMINI_FILE]


@pytest.mark.parametrize("mmd_path", _PORT_AXIS_FILES, ids=lambda p: p.stem)
def test_exit_port_feeder_alignment_reports_only_satisfiable_offsets(mmd_path):
    """Every reported exit-port misalignment is one the layout could resolve.

    Two ways a complaint can be unsatisfiable. It can name the axis the port is
    pinned to, where no layout change moves the port at all. Or it can name a
    perpendicular port, which must stay offset from every internal station on
    its free axis or the line's 90-degree turn into it passes through a station
    marker - demanding alignment there asks for exactly the geometry
    ``check_station_as_elbow`` rejects, so the two checks would contradict.
    """
    graph = _load_and_layout(mmd_path)
    for violation in check_exit_port_feeder_alignment(graph):
        port = graph.ports[violation.context["port"]]
        side = port.side.value
        expected = _FREE_AXIS_FOR_PORT_SIDE[side]
        assert violation.context["axis"] == expected, (
            f"{violation.context['port']} sits on the {side} edge, so only "
            f"{expected} is alignable, but the violation compares "
            f"{violation.context['axis']}: {violation.message}"
        )

        direction = graph.sections[violation.context["section"]].direction
        assert port.side not in perpendicular_port_sides(direction), (
            f"{violation.context['port']} is a perpendicular ({side}) port on "
            f"a {direction} section, where the "
            f"{violation.context['delta']:.1f}px offset is required clearance: "
            f"{violation.message}"
        )


# Both alignment checks branch on the section's flow direction, so a missing
# direction arm silences them entirely rather than degrading them. These tests
# displace geometry the checks are meant to catch and assert the complaint
# surfaces, which fails outright when BT falls through unhandled.

_BT_CHAIN_FILE = TOPOLOGIES_DIR / "bt_chain.mmd"
_BT_FLOW_EXIT_FILE = TOPOLOGIES_DIR / "bt_exit_top_above.mmd"


def _displace_bt_stations(graph, attr: str, amount: float = 60.0) -> None:
    """Push every interior station of a BT section off its section trunk."""
    bt_sections = {
        sid for sid, section in graph.sections.items() if section.direction == "BT"
    }
    moved = 0
    for index, (station_id, station) in enumerate(sorted(graph.stations.items())):
        if station.is_port or station_id in graph.junctions:
            continue
        if station.section_id not in bt_sections:
            continue
        offset = amount if index % 2 else -amount
        setattr(station, attr, getattr(station, attr) + offset)
        moved += 1
    assert moved, "fixture has no interior BT stations to displace"


def test_intra_section_chain_alignment_covers_bt_sections():
    graph = _load_and_layout(_BT_CHAIN_FILE)
    assert not check_intra_section_chain_alignment(graph)
    _displace_bt_stations(graph, "x")
    assert check_intra_section_chain_alignment(graph), (
        "BT chains zig-zagging by 120px went unreported"
    )


def test_exit_port_feeder_alignment_covers_bt_sections():
    """A BT section's flow-aligned (top) exit port is checked like any other."""
    graph = _load_and_layout(_BT_FLOW_EXIT_FILE)
    assert not check_exit_port_feeder_alignment(graph)
    _displace_bt_stations(graph, "x")
    assert check_exit_port_feeder_alignment(graph), (
        "a BT feeder dragged 120px off its top exit port went unreported"
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


class TestTbFileTerminiRegression:
    """Ensure the TB-section file-termini example lays out cleanly (#254)."""

    @pytest.fixture
    def tb_graph(self):
        return _load_and_layout(TB_FILE_TERMINI_FILE)

    def test_no_section_overlap(self, tb_graph):
        violations = check_section_overlap(tb_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_station_containment(self, tb_graph):
        violations = check_station_containment(tb_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_coordinate_sanity(self, tb_graph):
        violations = check_coordinate_sanity(tb_graph)
        errors = [v for v in violations if v.severity == Severity.ERROR]
        assert not errors, "\n".join(v.message for v in errors)

    def test_reporting_section_is_tb_with_termini(self, tb_graph):
        """The reporting section must stay TB and own the file termini."""
        reporting = tb_graph.sections["reporting"]
        assert reporting.direction == "TB"
        termini = [
            tb_graph.stations[sid]
            for sid in reporting.station_ids
            if tb_graph.stations[sid].is_terminus
        ]
        assert len(termini) == 3

    def test_termini_icons_reserved_below_in_bbox(self, tb_graph):
        """The TB section bbox bottom must clear its sink terminus icons."""
        reporting = tb_graph.sections["reporting"]
        bottom = reporting.bbox_y + reporting.bbox_h
        sink_termini = [
            tb_graph.stations[sid]
            for sid in reporting.station_ids
            if tb_graph.stations[sid].is_terminus
        ]
        assert sink_termini
        # Each sink terminus sits above the bbox bottom with room for its
        # downward icon (a bare marker would only need ~5px).
        for st in sink_termini:
            assert bottom - st.y > 2 * 16.0, (
                f"{st.id}: only {bottom - st.y:.1f}px below station for icon"
            )

    def test_entry_port_aligned_with_feeder_no_kink(self, tb_graph):
        """The TB entry port shares its feeder's Y (Stage 6.16 re-align)."""
        entry_ports = tb_graph.sections["reporting"].entry_ports
        assert entry_ports
        for pid in entry_ports:
            feeder_ys = [
                tb_graph.stations[e.source].y
                for e in tb_graph.edges_to(pid)
                if e.source in tb_graph.stations
            ]
            assert feeder_ys
            port_y = tb_graph.stations[pid].y
            assert min(abs(port_y - fy) for fy in feeder_ys) < 1.0

    def test_validate_guards_pass(self):
        """compute_layout(validate=True) exercises the terminus-icon guard."""
        text = TB_FILE_TERMINI_FILE.read_text()
        graph = parse_metro_mermaid(text)
        compute_layout(graph, validate=True)


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

    def test_trunk_reaches_entry_port(self):
        """Trunk route's last point should match the entry port."""
        graph = _load_and_layout(GENOMEASSEMBLY_FILE)
        routes = _compute_routes(graph)
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
        routes = _compute_routes(graph)
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
        routes = _compute_routes(graph)
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
        routes = _compute_routes(graph)
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


class TestUpwardBypass:
    """Regression tests for upward bypass routes (#240).

    When a bypass source is below the trunk (bottom of a tall section
    bypassing a shorter neighbour), the gap1 vertical goes UP.  The
    fan-path direction must match the L-shape sibling so they share
    consistent positions at the first corner, and the r2 override must
    account for the reversed gap1 direction.
    """

    UPWARD_BYPASS_FILE = TOPOLOGIES_DIR / "upward_bypass.mmd"

    @pytest.fixture(scope="class")
    def routes(self):
        graph = _load_and_layout(self.UPWARD_BYPASS_FILE)
        return _compute_routes(graph)

    @pytest.fixture(scope="class")
    def bypass_routes(self, routes):
        return [r for r in routes if len(r.points) == 6 and r.curve_radii]

    def test_fan_positions_consistent(self, routes):
        """L-shape and bypass from the same junction share gap1 X."""
        a_gap1_xs = set()
        for r in routes:
            if r.line_id != "a" or not r.is_inter_section:
                continue
            if len(r.points) >= 4:
                a_gap1_xs.add(round(r.points[1][0], 1))
        assert len(a_gap1_xs) == 1, (
            f"Line 'a' L-shape and bypass have different gap1 X: {a_gap1_xs}"
        )

    def test_corner1_concentricity(self, bypass_routes):
        """All bypass curves at corner 1 should start at the same X."""
        xs = {round(r.points[1][0] - r.curve_radii[0], 1) for r in bypass_routes}
        assert len(xs) == 1, f"Corner 1 start X not constant: {xs}"

    def test_corner2_concentricity(self, bypass_routes):
        """All bypass lines should start their trunk horizontal at the same X."""
        xs = {round(r.points[1][0] + r.curve_radii[1], 1) for r in bypass_routes}
        assert len(xs) == 1, f"Corner 2 start X not constant: {xs}"

    def test_corner3_concentricity(self, bypass_routes):
        """All bypass lines should end their trunk horizontal at the same X."""
        xs = {round(r.points[3][0] - r.curve_radii[2], 1) for r in bypass_routes}
        assert len(xs) == 1, f"Corner 3 end X not constant: {xs}"

    def test_corner4_concentricity(self, bypass_routes):
        """All bypass lines should reach the target at the same X."""
        xs = {round(r.points[3][0] + r.curve_radii[3], 1) for r in bypass_routes}
        assert len(xs) == 1, f"Corner 4 target X not constant: {xs}"

    def test_no_line_crossings_on_segments(self, bypass_routes):
        """Lines should maintain monotonic ordering on each segment."""
        bypasses = sorted(bypass_routes, key=lambda r: r.line_id)
        for seg_idx, coord_idx, name in [
            (1, 0, "gap1_x"),
            (2, 1, "trunk_y"),
            (3, 0, "gap2_x"),
        ]:
            vals = [r.points[seg_idx][coord_idx] for r in bypasses]
            is_mono = all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)) or all(
                vals[i] >= vals[i + 1] for i in range(len(vals) - 1)
            )
            assert is_mono, (
                f"{name} not monotonic: "
                f"{[(r.line_id, vals[i]) for i, r in enumerate(bypasses)]}"
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
        assert not violations, "\n".join(v.message for v in violations)

    def test_variant_calling_no_slope(self):
        """The variant_calling example should be clean."""
        graph = _load_and_layout(EXAMPLES_DIR / "variant_calling.mmd")
        violations = check_almost_horizontal_edges(graph)
        assert not violations, "\n".join(v.message for v in violations)

    def test_with_subworkflows_no_slope(self):
        """with_subworkflows should be clean (#420).

        Its Alignment trunk co-travels to the exit port with the
        alignment_reporting branch; the exit-only reorder must not step the
        through-trunk's offset, which would slant its junction-to-entry
        segment between Preprocess and Alignment.
        """
        from nf_metro.convert import convert_nextflow_dag

        path = (
            Path(__file__).parent.parent
            / "tests"
            / "fixtures"
            / "nextflow"
            / "with_subworkflows.mmd"
        )
        graph = parse_metro_mermaid(convert_nextflow_dag(path.read_text()))
        compute_layout(graph)
        violations = check_almost_horizontal_edges(graph)
        assert not violations, "\n".join(v.message for v in violations)


# --- #652: junction fan-out + bypass concentric nesting ---

FAN_BYPASS_NESTING_FILE = TOPOLOGIES_DIR / "fan_bypass_nesting.mmd"


def _station_row(graph, station_id):
    """Grid row of a station's resolved section, or ``None``."""
    st = graph.stations.get(station_id)
    return _resolve_section_row(graph, st) if st else None


def _bypass_fan_weaves(graph):
    """Junction crossings where a bypass weaves a descender AT THE FAN.

    The bug is a bypass whose lead-in tangles across its sibling down-turns up
    at the fan corner.  A weave is a crossing of two edges from one junction
    where exactly one turns DOWN to a lower row (a descender) and the crossing
    point sits within the junction's own row band -- i.e. near the fan, before
    the bypass has peeled off.  A crossing down in the inter-row gap is the
    bypass diverging into its run (the clean fork), not a weave, and a crossing
    with the same-row trunk continuation is the unavoidable divergence; neither
    is counted.
    """
    out = []
    for v in check_route_segment_crossings(graph):
        edge_a = v.context["edge_a"]
        edge_b = v.context["edge_b"]
        jid = edge_a[0]
        if jid != edge_b[0] or not jid.startswith("__junction"):
            continue
        jrow = _station_row(graph, jid)
        row_a = _station_row(graph, edge_a[1])
        row_b = _station_row(graph, edge_b[1])
        if jrow is None or row_a is None or row_b is None:
            continue
        if (row_a > jrow) == (row_b > jrow):
            continue  # not a bypass-vs-descender pair
        band_bottom = row_bottom_edge(graph, jrow, default=None)
        cross_y = v.context["intersection"][1]
        if band_bottom is not None and cross_y <= band_bottom + 1.0:
            out.append(v.message)
    return out


def test_fan_bypass_nesting_fixture_fans_from_a_junction():
    """The fixture's source must fan out through a synthetic junction."""
    graph = _load_and_layout(FAN_BYPASS_NESTING_FILE)
    junctions = [s for s in graph.stations.values() if s.id.startswith("__junction")]
    assert junctions, "fixture lost its synthetic fan-out junction"


def test_fan_bypass_no_fan_weave():
    """A fan-out bypass must not weave across its down-turns at the fan (#652).

    The bypass stays bundled through the down-turns' shared concentric corner
    and only crosses over once it has peeled into the inter-row gap, so no
    crossing with a descending sibling lands in the junction's own row band.
    A crossover down at the divergence, and the crossing with the same-row
    trunk continuation, are both expected and not flagged.
    """
    graph = _load_and_layout(FAN_BYPASS_NESTING_FILE)
    weaves = _bypass_fan_weaves(graph)
    assert not weaves, "\n".join(weaves)


# --- #1844: a row-mate carrying part of the row trunk stays on it ---

ROW_TRUNK_PARTIAL_FILE = TOPOLOGIES_DIR / "row_trunk_partial_through_line.mmd"
RIBOSEQ_CORRIDOR_FILE = (
    Path(__file__).parent
    / "fixtures"
    / "curve_invariant_repros"
    / "riboseq_inter_row_corridor.mmd"
)

_PARTIAL_TRUNK_FIXTURES = [
    pytest.param(ROW_TRUNK_PARTIAL_FILE, id="row_trunk_partial_through_line"),
    pytest.param(RIBOSEQ_CORRIDOR_FILE, id="riboseq_inter_row_corridor"),
]


def _modal_station_y(graph, section_id: str) -> float:
    """The Y that most of a section's own on-trunk stations sit on."""
    ys = [
        graph.stations[sid].y
        for sid in graph.sections[section_id].station_ids
        if not graph.stations[sid].is_port and not graph.stations[sid].off_track
    ]
    return Counter(round(y, 1) for y in ys).most_common(1)[0][0]


def _sole_lr_port_y(graph, section_id: str, side: PortSide) -> float:
    """The Y of the section's only port on *side*."""
    ys = {
        round(port.y, 1)
        for pid in graph.sections[section_id].port_ids
        if (port := graph.ports.get(pid)) is not None and port.side is side
    }
    assert len(ys) == 1, f"{section_id} has {len(ys)} {side.name} ports, expected 1"
    return ys.pop()


def _lane_step(graph, section_id: str) -> float:
    """The vertical pitch between adjacent lanes inside a section."""
    ys = sorted(
        {
            round(graph.stations[sid].y, 1)
            for sid in graph.sections[section_id].station_ids
        }
    )
    return min(b - a for a, b in zip(ys, ys[1:]) if b > a)


class TestRowTrunkPartialThroughLine:
    """A row-mate fed by part of the row trunk keeps its interior on that trunk.

    ``novel_transcripts`` is entered by a single line that the same junction
    also fans into another grid row, so it carries only a subset of the lines
    crossing its row.  The trunk still arrives at its boundary, so its interior
    belongs on the row's trunk Y rather than centred in its own shorter box,
    and its off-track output rides one lane above rather than being paid for by
    dropping the trunk.
    """

    @pytest.mark.parametrize("path", _PARTIAL_TRUNK_FIXTURES)
    def test_trunk_runs_unbroken_into_the_partial_row_mate(self, path):
        graph = _load_and_layout(path)
        chain = {
            "alignment trunk": _modal_station_y(graph, "alignment"),
            "alignment exit port": _sole_lr_port_y(graph, "alignment", PortSide.RIGHT),
            "discovery entry port": _sole_lr_port_y(
                graph, "novel_transcripts", PortSide.LEFT
            ),
            "discovery trunk": _modal_station_y(graph, "novel_transcripts"),
        }
        assert len(set(chain.values())) == 1, (
            "the row trunk steps between alignment and novel_transcripts: "
            + ", ".join(f"{name}={y}" for name, y in chain.items())
        )

    def test_off_track_output_rides_one_lane_above_the_trunk(self):
        graph = _load_and_layout(ROW_TRUNK_PARTIAL_FILE)
        trunk_y = _modal_station_y(graph, "novel_transcripts")
        gtf_out = graph.stations["gtf_out"]
        assert gtf_out.off_track
        assert trunk_y - gtf_out.y == pytest.approx(_lane_step(graph, "alignment"))

    def test_partial_row_mate_is_not_a_fan_branch_leaf(self):
        graph = _load_and_layout(ROW_TRUNK_PARTIAL_FILE)
        assert not _is_fan_branch_leaf(
            graph, graph.sections["novel_transcripts"], full_carrier=False
        )


# --- #2001: a dead-end off-track tail peels below the exit trunk ---

TAIL_PEEL_FILE = TOPOLOGIES_DIR / "tail_peel_at_through_station.mmd"


def _point_to_segment_distance(p, a, b) -> float:
    """Shortest distance from point *p* to the segment *a*-*b*."""
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len_sq))
    cx, cy = ax + t * dx, ay + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


class TestTailPeelAtThroughStation:
    """A through-station's dead-end output tail peels off the exit trunk (#2001).

    ``dedup`` fans to the section's exit port (continuing to ``quant``) and to
    ``genomecov``, whose only path sinks into the off-track ``cov_out`` file.
    ``genomecov`` reaches no exit, so it must not inherit ``dedup``'s trunk row;
    otherwise the ``dedup`` -> exit through-route runs flat across the
    ``genomecov`` marker and the map reads as if the tail feeds ``quant``.
    """

    @pytest.fixture
    def graph(self):
        return _load_and_layout(TAIL_PEEL_FILE)

    def test_dead_end_tail_peels_below_the_trunk(self, graph):
        dedup = graph.stations["dedup"]
        genomecov = graph.stations["genomecov"]
        cov_out = graph.stations["cov_out"]
        assert genomecov.y > dedup.y + 1.0, (
            f"genomecov (y={genomecov.y:.1f}) should peel below dedup "
            f"(y={dedup.y:.1f}), not inherit the exit trunk"
        )
        assert cov_out.y > dedup.y + 1.0, (
            f"cov_out (y={cov_out.y:.1f}) should ride below the trunk with its "
            f"producer, not be lifted above it (dedup y={dedup.y:.1f})"
        )
        lane = _lane_step(graph, "align")
        assert abs(cov_out.y - genomecov.y) <= lane + 1.0, (
            f"cov_out (y={cov_out.y:.1f}) should sit with genomecov "
            f"(y={genomecov.y:.1f}), no re-lift S-bend"
        )

    def test_exit_route_runs_clear_of_the_tail_marker(self, graph):
        genomecov = graph.stations["genomecov"]
        exit_ports = set(graph.sections["align"].exit_ports)
        routes = _compute_routes(graph)
        through = [
            r
            for r in routes
            if r.edge.source == "dedup" and r.edge.target in exit_ports
        ]
        assert through, "expected a routed dedup -> exit-port through-run"
        for r in through:
            for a, b in zip(r.points, r.points[1:]):
                dist = _point_to_segment_distance((genomecov.x, genomecov.y), a, b)
                assert dist > 6.0, (
                    f"through-route {r.edge.source}->{r.edge.target} passes "
                    f"{dist:.1f}px from the genomecov marker "
                    f"({genomecov.x:.1f},{genomecov.y:.1f})"
                )
