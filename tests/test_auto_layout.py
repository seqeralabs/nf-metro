"""Tests for auto-layout inference logic."""

import re
from pathlib import Path

import pytest

from nf_metro.layout.auto_layout import (
    _assign_grid_positions,
    _build_section_dag,
    _infer_directions,
    _infer_port_sides,
    infer_section_layout,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import (
    Edge,
    MetroGraph,
    MetroLine,
    PortSide,
    Section,
    Station,
)

EXAMPLES = Path(__file__).parent.parent / "examples"


def _make_graph_with_sections(
    section_ids: list[str],
    inter_edges: list[tuple[str, str, str, str, str]],
) -> MetroGraph:
    """Helper to build a graph with sections and inter-section edges.

    inter_edges: list of (source_station, source_section,
        target_station, target_section, line_id)
    """
    graph = MetroGraph()
    graph.add_line(MetroLine(id="main", display_name="Main", color="#ff0000"))

    for sid in section_ids:
        section = Section(id=sid, name=sid.title())
        graph.add_section(section)
        # Add a station in each section
        station = Station(id=f"{sid}_s1", label=f"{sid} S1", section_id=sid)
        graph.add_station(station)
        section.station_ids.append(station.id)

    for src_st, src_sec, tgt_st, tgt_sec, line_id in inter_edges:
        # Ensure stations exist
        if src_st not in graph.stations:
            st = Station(id=src_st, label=src_st, section_id=src_sec)
            graph.add_station(st)
            graph.sections[src_sec].station_ids.append(src_st)
        if tgt_st not in graph.stations:
            st = Station(id=tgt_st, label=tgt_st, section_id=tgt_sec)
            graph.add_station(st)
            graph.sections[tgt_sec].station_ids.append(tgt_st)
        graph.add_edge(Edge(source=src_st, target=tgt_st, line_id=line_id))

    return graph


# --- Phase 1: Build section DAG ---


def test_build_section_dag():
    """_build_section_dag correctly identifies successors, predecessors,
    and edge lines."""
    graph = _make_graph_with_sections(
        ["sec1", "sec2", "sec3"],
        [
            ("sec1_s1", "sec1", "sec2_s1", "sec2", "main"),
            ("sec2_s1", "sec2", "sec3_s1", "sec3", "main"),
        ],
    )
    successors, predecessors, edge_lines = _build_section_dag(graph)
    assert successors["sec1"] == {"sec2"}
    assert successors["sec2"] == {"sec3"}
    assert "sec3" not in successors
    assert "sec1" not in predecessors
    assert predecessors["sec2"] == {"sec1"}
    assert predecessors["sec3"] == {"sec2"}
    assert edge_lines[("sec1", "sec2")] == {"main"}


def test_build_section_dag_multi_line():
    """Multiple line IDs on the same section pair are tracked."""
    graph = _make_graph_with_sections(["sec1", "sec2"], [])
    graph.add_line(MetroLine(id="alt", display_name="Alt", color="#0000ff"))
    graph.add_edge(Edge(source="sec1_s1", target="sec2_s1", line_id="main"))
    graph.add_edge(Edge(source="sec1_s1", target="sec2_s1", line_id="alt"))

    _, _, edge_lines = _build_section_dag(graph)
    assert edge_lines[("sec1", "sec2")] == {"main", "alt"}


# --- Phase 2: Grid position assignment ---


def test_grid_assignment_linear_chain():
    """Three sections in a linear chain get cols 0, 1, 2."""
    graph = _make_graph_with_sections(
        ["sec1", "sec2", "sec3"],
        [
            ("sec1_s1", "sec1", "sec2_s1", "sec2", "main"),
            ("sec2_s1", "sec2", "sec3_s1", "sec3", "main"),
        ],
    )
    successors, predecessors, _ = _build_section_dag(graph)
    _assign_grid_positions(graph, successors, predecessors, max_station_columns=100)

    assert graph.sections["sec1"].grid_col == 0
    assert graph.sections["sec1"].grid_row == 0
    assert graph.sections["sec2"].grid_col == 1
    assert graph.sections["sec2"].grid_row == 0
    assert graph.sections["sec3"].grid_col == 2
    assert graph.sections["sec3"].grid_row == 0


def test_grid_assignment_branching():
    """Branching sections at the same topo level stack vertically."""
    graph = _make_graph_with_sections(
        ["root", "branch_a", "branch_b"],
        [
            ("root_s1", "root", "branch_a_s1", "branch_a", "main"),
            ("root_s1", "root", "branch_b_s1", "branch_b", "main"),
        ],
    )
    successors, predecessors, _ = _build_section_dag(graph)
    _assign_grid_positions(graph, successors, predecessors, max_station_columns=100)

    assert graph.sections["root"].grid_col == 0
    assert graph.sections["root"].grid_row == 0
    # Both branches in col 1, different rows
    assert graph.sections["branch_a"].grid_col == 1
    assert graph.sections["branch_b"].grid_col == 1
    assert graph.sections["branch_a"].grid_row != graph.sections["branch_b"].grid_row


def test_grid_assignment_fold():
    """Sections fold into a new row when cumulative station layers exceed threshold."""
    # Each section has 1 station = 1 layer wide, so max_station_columns=3
    # means the 4th section (cumulative=4>3) triggers a fold.
    sections = [f"sec{i}" for i in range(5)]
    edges = []
    for i in range(4):
        edges.append((f"sec{i}_s1", f"sec{i}", f"sec{i + 1}_s1", f"sec{i + 1}", "main"))
    graph = _make_graph_with_sections(sections, edges)
    successors, predecessors, _ = _build_section_dag(graph)
    fold_sections, _below, _conv = _assign_grid_positions(
        graph,
        successors,
        predecessors,
        max_station_columns=3,
    )

    # Row 0: sec0 at col 0, sec1 at col 1, sec2 at col 2
    assert graph.sections["sec0"].grid_col == 0
    assert graph.sections["sec0"].grid_row == 0
    assert graph.sections["sec1"].grid_col == 1
    assert graph.sections["sec1"].grid_row == 0
    assert graph.sections["sec2"].grid_col == 2
    assert graph.sections["sec2"].grid_row == 0
    # sec3 is the fold section (4th col would exceed threshold of 3)
    assert "sec3" in fold_sections
    assert graph.sections["sec3"].grid_col == 3
    assert graph.sections["sec3"].grid_row == 0
    # sec4 starts a new row band one column past the fold (leftward),
    # so it doesn't share the narrow fold column
    assert graph.sections["sec4"].grid_row == 1
    assert graph.sections["sec4"].grid_col == 2  # one left of fold section


def test_grid_preserves_explicit_overrides():
    """Sections in grid_overrides keep their positions."""
    graph = _make_graph_with_sections(
        ["sec1", "sec2"],
        [("sec1_s1", "sec1", "sec2_s1", "sec2", "main")],
    )
    graph.grid_overrides["sec2"] = (5, 3, 1, 1)
    successors, predecessors, _ = _build_section_dag(graph)
    _assign_grid_positions(graph, successors, predecessors, max_station_columns=100)

    # sec2 should retain its explicit position
    assert graph.grid_overrides["sec2"] == (5, 3, 1, 1)
    # sec1 gets auto-assigned
    assert graph.sections["sec1"].grid_col == 0
    assert graph.sections["sec1"].grid_row == 0


# --- Phase 3: Direction inference ---


def test_direction_inference_lr():
    """Section with successor to the right gets LR direction."""
    graph = _make_graph_with_sections(
        ["sec1", "sec2"],
        [("sec1_s1", "sec1", "sec2_s1", "sec2", "main")],
    )
    successors, predecessors, _ = _build_section_dag(graph)
    _assign_grid_positions(graph, successors, predecessors, max_station_columns=100)
    _infer_directions(graph, successors, predecessors, set())

    assert graph.sections["sec1"].direction == "LR"


def test_direction_inference_rl():
    """Section with all successors to the left gets RL direction."""
    graph = _make_graph_with_sections(
        ["sec1", "sec2"],
        [("sec1_s1", "sec1", "sec2_s1", "sec2", "main")],
    )
    successors, predecessors, _ = _build_section_dag(graph)
    _assign_grid_positions(graph, successors, predecessors, max_station_columns=100)

    # Manually force sec2 to be at a lower column than sec1
    # (simulating a serpentine row)
    graph.sections["sec2"].grid_col = 0
    graph.sections["sec2"].grid_row = 1
    graph.sections["sec1"].grid_col = 1
    graph.sections["sec1"].grid_row = 1

    # Now sec1's successor (sec2) is to the left and same row
    _infer_directions(graph, successors, predecessors, set())
    assert graph.sections["sec1"].direction == "RL"


def test_direction_explicit_preserved():
    """Explicit direction directives are not overwritten by auto-inference."""
    graph = _make_graph_with_sections(
        ["sec1", "sec2"],
        [("sec1_s1", "sec1", "sec2_s1", "sec2", "main")],
    )
    graph.sections["sec1"].direction = "TB"
    graph._explicit_directions.add("sec1")

    successors, predecessors, _ = _build_section_dag(graph)
    _assign_grid_positions(graph, successors, predecessors, max_station_columns=100)
    _infer_directions(graph, successors, predecessors, set())

    assert graph.sections["sec1"].direction == "TB"


def test_explicit_grid_section_keeps_lr_against_auto_successor_below():
    """An explicit-grid section keeps the LR default even when an auto-placed
    successor lands in a lower grid row.

    Regression lock for #446: during auto-layout an explicit-grid section
    still reads grid_col == -1, so comparing it against an auto neighbour
    (row >= 0) used to fire the "all successors below" TB branch and reorient
    the section vertically.
    """
    graph = _make_graph_with_sections(
        ["manual", "downstream"],
        [("manual_s1", "manual", "downstream_s1", "downstream", "main")],
    )
    # 'manual' is explicitly gridded; 'downstream' is left to auto-layout.
    graph.grid_overrides["manual"] = (0, 0, 1, 1)
    graph._explicit_grid.add("manual")

    successors, predecessors, _ = _build_section_dag(graph)
    _assign_grid_positions(graph, successors, predecessors, max_station_columns=100)
    _infer_directions(graph, successors, predecessors, set())

    assert graph.sections["manual"].direction == "LR"


@pytest.mark.parametrize(
    "fixture",
    [
        "sarek_metro.mmd",
        "genomeassembly_staggered.mmd",
        "topologies/stacked_lr_serpentine.mmd",
        "topologies/around_section_below.mmd",
        "topologies/inter_row_wrap_clearance.mmd",
    ],
)
def test_explicit_grid_sections_default_lr(fixture):
    """Explicit-grid sections with no %%metro direction stay LR after layout.

    These fixtures lay every section out by hand; their grid stacks
    successors below predecessors (serpentine wraps), which must not be read
    as vertical internal flow (#446).
    """
    text = (EXAMPLES / fixture).read_text()
    graph = parse_metro_mermaid(text)

    explicit = graph._explicit_grid - graph._explicit_directions
    assert explicit, f"{fixture} has no explicit-grid sections to check"
    for sec_id in explicit:
        assert graph.sections[sec_id].direction == "LR", (
            f"{fixture}: {sec_id} should default LR, got "
            f"{graph.sections[sec_id].direction}"
        )


# --- Phase 4: Port side inference ---


def test_port_side_inference_exit():
    """Exit hints point to the side facing the majority of successors."""
    graph = _make_graph_with_sections(
        ["sec1", "sec2"],
        [("sec1_s1", "sec1", "sec2_s1", "sec2", "main")],
    )
    successors, predecessors, edge_lines = _build_section_dag(graph)
    _assign_grid_positions(graph, successors, predecessors, max_station_columns=100)
    _infer_port_sides(graph, successors, predecessors, edge_lines, set())

    # sec1 at col 0, sec2 at col 1 -> exit should be RIGHT
    assert len(graph.sections["sec1"].exit_hints) == 1
    assert graph.sections["sec1"].exit_hints[0][0] == PortSide.RIGHT
    assert "main" in graph.sections["sec1"].exit_hints[0][1]


def test_port_side_inference_entry():
    """Entry hints point to the side facing the source section."""
    graph = _make_graph_with_sections(
        ["sec1", "sec2"],
        [("sec1_s1", "sec1", "sec2_s1", "sec2", "main")],
    )
    successors, predecessors, edge_lines = _build_section_dag(graph)
    _assign_grid_positions(graph, successors, predecessors, max_station_columns=100)
    _infer_port_sides(graph, successors, predecessors, edge_lines, set())

    # sec1 at col 0, sec2 at col 1 -> entry at LEFT (source is to the left)
    assert len(graph.sections["sec2"].entry_hints) == 1
    assert graph.sections["sec2"].entry_hints[0][0] == PortSide.LEFT
    assert "main" in graph.sections["sec2"].entry_hints[0][1]


def test_port_side_explicit_preserved():
    """Explicit entry/exit hints are not overwritten."""
    graph = _make_graph_with_sections(
        ["sec1", "sec2"],
        [("sec1_s1", "sec1", "sec2_s1", "sec2", "main")],
    )
    graph.sections["sec1"].exit_hints.append((PortSide.BOTTOM, ["main"]))
    graph.sections["sec2"].entry_hints.append((PortSide.TOP, ["main"]))

    successors, predecessors, edge_lines = _build_section_dag(graph)
    _assign_grid_positions(graph, successors, predecessors, max_station_columns=100)
    _infer_port_sides(graph, successors, predecessors, edge_lines, set())

    # Explicit hints preserved
    assert graph.sections["sec1"].exit_hints[0][0] == PortSide.BOTTOM
    assert graph.sections["sec2"].entry_hints[0][0] == PortSide.TOP


def test_stacked_lr_sections_flow_aligned_ports():
    """Stacked LR sections connect via a carriage-return wrap (#432).

    A left-to-right section stacked directly below another in the same
    grid column must present flow-aligned ports - exit RIGHT, entry LEFT -
    not a TOP/BOTTOM vertical hop.  The inter-section router carriage-
    returns (right -> down -> left -> down -> right) to join them.
    """
    graph = _make_graph_with_sections(
        ["top_sec", "bottom_sec"],
        [("top_sec_s1", "top_sec", "bottom_sec_s1", "bottom_sec", "main")],
    )
    successors, predecessors, edge_lines = _build_section_dag(graph)
    _assign_grid_positions(graph, successors, predecessors, max_station_columns=100)

    # Force bottom_sec to be below top_sec in the same column.
    graph.sections["top_sec"].grid_col = 0
    graph.sections["top_sec"].grid_row = 0
    graph.sections["bottom_sec"].grid_col = 0
    graph.sections["bottom_sec"].grid_row = 1
    assert graph.sections["top_sec"].direction == "LR"
    assert graph.sections["bottom_sec"].direction == "LR"

    _infer_port_sides(graph, successors, predecessors, edge_lines, set())

    # Exit on the trailing (RIGHT) edge, entry on the leading (LEFT) edge.
    assert graph.sections["top_sec"].exit_hints[0][0] == PortSide.RIGHT
    assert graph.sections["bottom_sec"].entry_hints[0][0] == PortSide.LEFT


def test_rl_sections_flow_aligned_ports():
    """RL sections mirror the flow-aligned rule: entry RIGHT, exit LEFT."""
    graph = _make_graph_with_sections(
        ["top_sec", "bottom_sec"],
        [("top_sec_s1", "top_sec", "bottom_sec_s1", "bottom_sec", "main")],
    )
    successors, predecessors, edge_lines = _build_section_dag(graph)
    _assign_grid_positions(graph, successors, predecessors, max_station_columns=100)

    graph.sections["top_sec"].grid_col = 0
    graph.sections["top_sec"].grid_row = 0
    graph.sections["top_sec"].direction = "RL"
    graph.sections["bottom_sec"].grid_col = 0
    graph.sections["bottom_sec"].grid_row = 1
    graph.sections["bottom_sec"].direction = "RL"

    _infer_port_sides(graph, successors, predecessors, edge_lines, set())

    assert graph.sections["top_sec"].exit_hints[0][0] == PortSide.LEFT
    assert graph.sections["bottom_sec"].entry_hints[0][0] == PortSide.RIGHT


# --- Edge cases ---


def test_no_sections_no_op():
    """Graph with no sections is unchanged."""
    graph = MetroGraph()
    graph.add_station(Station(id="a", label="A"))
    graph.add_station(Station(id="b", label="B"))
    graph.add_edge(Edge(source="a", target="b", line_id="main"))

    infer_section_layout(graph)

    assert len(graph.sections) == 0
    assert len(graph.grid_overrides) == 0


def test_single_section_no_op():
    """Single section with no inter-section edges is unchanged."""
    graph = MetroGraph()
    section = Section(id="only", name="Only")
    graph.add_section(section)
    station = Station(id="a", label="A", section_id="only")
    graph.add_station(station)
    section.station_ids.append("a")

    infer_section_layout(graph)

    # Should not modify anything
    assert len(graph.grid_overrides) == 0
    assert graph.sections["only"].direction == "LR"


# --- Integration tests ---


def test_stacked_sections_infer_left_entry():
    """the stacked col-1 sections auto-infer LEFT entry ports (#432).

    ``post_vc``/``annotation``/``reporting`` carry no explicit entry/exit
    directives; with only their grid positions declared they must still
    infer flow-aligned LEFT entries so they connect via a carriage-return
    wrap, not enter through the right edge.
    """
    from nf_metro.layout.engine import compute_layout

    text = (EXAMPLES / "genomic_pipeline.mmd").read_text()
    graph = parse_metro_mermaid(text)
    compute_layout(graph)

    for sec_id in ("post_vc", "annotation", "reporting"):
        entries = [
            p for p in graph.ports.values() if p.section_id == sec_id and p.is_entry
        ]
        assert entries, f"{sec_id} should have an entry port"
        for port in entries:
            assert port.side == PortSide.LEFT, (
                f"{sec_id} entry port {port.id} inferred on {port.side.name}, "
                "expected LEFT (flow-aligned carriage-return)"
            )


def test_rnaseq_auto_renders():
    """rnaseq_auto.mmd (no directives) parses and renders without errors."""
    from nf_metro.layout.engine import compute_layout
    from nf_metro.render.svg import render_svg
    from nf_metro.themes.nfcore import NFCORE_THEME

    text = (EXAMPLES / "rnaseq_auto.mmd").read_text()
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    svg = render_svg(graph, NFCORE_THEME)

    # Should produce valid SVG with all sections
    assert "<svg" in svg
    assert "Pre-processing" in svg
    assert "Genome alignment" in svg
    assert "Post-processing" in svg
    assert "Pseudo-alignment" in svg
    assert "Quality control" in svg


def test_rnaseq_auto_sections_have_ports():
    """rnaseq_auto.mmd produces port and junction infrastructure."""
    text = (EXAMPLES / "rnaseq_auto.mmd").read_text()
    graph = parse_metro_mermaid(text)

    # Should have ports from auto-inferred hints
    assert len(graph.ports) > 0

    # Sections with outgoing inter-section edges should have exit ports
    preprocessing_exits = [
        p
        for p in graph.ports.values()
        if p.section_id == "preprocessing" and not p.is_entry
    ]
    assert len(preprocessing_exits) >= 1

    # Genome alignment should have entry ports
    genome_entries = [
        p for p in graph.ports.values() if p.section_id == "genome_align" and p.is_entry
    ]
    assert len(genome_entries) >= 1


def test_rnaseq_auto_grid_positions():
    """rnaseq_auto.mmd sections get reasonable grid positions."""
    text = (EXAMPLES / "rnaseq_auto.mmd").read_text()
    graph = parse_metro_mermaid(text)

    # All sections should have grid positions assigned
    for sec_id, section in graph.sections.items():
        assert sec_id in graph.grid_overrides, f"{sec_id} missing from grid_overrides"

    # Preprocessing should be first (col 0, row 0)
    assert graph.sections["preprocessing"].grid_col == 0
    assert graph.sections["preprocessing"].grid_row == 0

    # Genome alignment should be after preprocessing in row 0
    assert (
        graph.sections["genome_align"].grid_col
        > graph.sections["preprocessing"].grid_col
    )
    assert graph.sections["genome_align"].grid_row == 0

    # Postprocessing is the fold section (TB bridge) at the right edge of row 0
    assert graph.sections["postprocessing"].grid_row == 0
    assert graph.sections["postprocessing"].direction == "TB"

    # QC report should be in the next row band (below the fold)
    assert graph.sections["qc_report"].grid_row > 0


# --- Below-fold row sharing ---


def test_below_fold_sections_share_rows_with_return():
    """Below-fold sections (in the fold column) should not push return-row
    sections to extra rows. They occupy different columns so can share rows."""
    # sec1 -> {sec2a, sec2b} -> sec3 (fold) -> sec4 (below) -> sec5
    # max_station_columns=2: fold at sec3 with band_height=2
    # sec3 has single successor sec4 -> below-fold placement
    # sec5 on the return row should share rows with sec4
    graph = _make_graph_with_sections(
        ["sec1", "sec2a", "sec2b", "sec3", "sec4", "sec5"],
        [
            ("sec1_s1", "sec1", "sec2a_s1", "sec2a", "main"),
            ("sec1_s1", "sec1", "sec2b_s1", "sec2b", "main"),
            ("sec2a_s1", "sec2a", "sec3_s1", "sec3", "main"),
            ("sec2b_s1", "sec2b", "sec3_s1", "sec3", "main"),
            ("sec3_s1", "sec3", "sec4_s1", "sec4", "main"),
            ("sec4_s1", "sec4", "sec5_s1", "sec5", "main"),
        ],
    )
    successors, predecessors, edge_lines = _build_section_dag(graph)
    fold_sections, below_fold, _conv = _assign_grid_positions(
        graph, successors, predecessors, max_station_columns=2
    )

    # sec3 should be the fold section
    assert "sec3" in fold_sections

    # sec4 should be placed below the fold
    assert "sec4" in below_fold

    # sec5 (return row) should share the same row as sec4 (below-fold),
    # not be pushed to a later row. They're in different columns so sharing is fine.
    sec4_row = graph.sections["sec4"].grid_row
    sec5_row = graph.sections["sec5"].grid_row
    assert sec5_row == sec4_row, (
        f"Return section sec5 at row {sec5_row} should share row with "
        f"below-fold sec4 at row {sec4_row}"
    )


# --- Folded-grid topological-order invariants (issue #256) ---

TOPOLOGIES_DIR = EXAMPLES / "topologies"

# Fold-exercising fixtures: each wraps into >=2 rows at small fold
# thresholds, stressing the serpentine packer's row/column assignment.
_FOLD_FIXTURES = [
    "fold_double",
    "fold_fan_across",
    "fold_stacked_branch",
    "u_turn_fold",
    "deep_linear",
]

# Fold thresholds that force serpentine wrapping on these fixtures.
_FOLD_THRESHOLDS = [3, 4, 9]


def _fold_layout(fixture: str, max_station_columns: int):
    """Parse a fold fixture and run full auto-layout inference (greedy
    packer + post-passes), returning the laid-out graph."""
    path = TOPOLOGIES_DIR / f"{fixture}.mmd"
    return parse_metro_mermaid(
        path.read_text(), max_station_columns=max_station_columns
    )


def _grid_of(graph):
    """{section_id: (grid_col, grid_row)} for every section."""
    return {sid: (sec.grid_col, sec.grid_row) for sid, sec in graph.sections.items()}


@pytest.mark.parametrize("fixture", _FOLD_FIXTURES)
@pytest.mark.parametrize("threshold", _FOLD_THRESHOLDS)
def test_folded_grid_has_no_negative_columns(fixture, threshold):
    """No auto-placed section may land at a negative grid column.

    A negative column means a section was pushed left of the entire
    layout (the spurious-trailing-fold defect in #256), which renders
    the badge to the left of everything and snakes the trunk down the
    left edge.
    """
    grid = _grid_of(_fold_layout(fixture, threshold))
    offenders = {sid: (c, r) for sid, (c, r) in grid.items() if c < 0}
    assert not offenders, (
        f"{fixture} (threshold={threshold}): sections at negative grid "
        f"columns: {offenders}"
    )


@pytest.mark.parametrize("fixture", _FOLD_FIXTURES)
@pytest.mark.parametrize("threshold", _FOLD_THRESHOLDS)
def test_folded_grid_preserves_topo_order_in_serpentine_read(fixture, threshold):
    """A folded grid must read in topological order along the serpentine.

    Reading row 0 left-to-right, row 1 right-to-left, row 2
    left-to-right, ... must visit each section no earlier than its
    topological predecessor. A section read before its predecessor means
    the inter-section trunk has to double back across the wrap - either
    the spurious-trailing-fold / negative-column defect or the
    sibling-scatter defect from #256 (a converging successor slotted
    between two stacked sibling predecessors).
    """
    graph = _fold_layout(fixture, threshold)
    # Use the section DAG captured during inference (built before
    # _resolve_sections rewrites inter-section edges into port chains);
    # rebuilding from the resolved graph would lose those edges.
    predecessors = graph.section_dag.predecessors
    sections = graph.sections
    grid = _grid_of(graph)

    # A row band can be several rows tall: stacked sections share a band and
    # thus a flow direction. Flow alternates per band, not per row. Derive
    # each row's flow from the horizontal section directions on it (LR ->
    # +col, RL -> -col), falling back to serpentine parity for all-vertical
    # rows. Consecutive rows sharing a flow direction form one band.
    rows = sorted({r for _, r in grid.values()})
    row_flow: dict[int, int] = {}
    for sec in sections.values():
        if sec.direction in ("LR", "RL"):
            row_flow.setdefault(sec.grid_row, 1 if sec.direction == "LR" else -1)
    for row in rows:
        row_flow.setdefault(row, 1 if row % 2 == 0 else -1)

    band_of: dict[int, int] = {}
    band_idx = 0
    for i, row in enumerate(rows):
        if i > 0 and row_flow[row] != row_flow[rows[i - 1]]:
            band_idx += 1
        band_of[row] = band_idx

    # Serpentine read-order rank: bands ascend; within a band, sections are
    # read along the band's flow direction (stacked sections at the same
    # column-position read consecutively, by row).
    def read_rank(item):
        _sid, (col, row) = item
        return (band_of[row], row_flow[row] * col, row)

    order = [sid for sid, _ in sorted(grid.items(), key=read_rank)]
    rank = {sid: i for i, sid in enumerate(order)}

    for sid, preds in predecessors.items():
        if sid not in rank:
            continue
        for pred in preds:
            if pred not in rank:
                continue
            assert rank[pred] <= rank[sid], (
                f"{fixture} (threshold={threshold}): predecessor {pred!r} "
                f"at grid {grid[pred]} (read-rank {rank[pred]}) comes AFTER "
                f"its successor {sid!r} at grid {grid[sid]} (read-rank "
                f"{rank[sid]}) in serpentine read order"
            )


_GRID_DIRECTIVE_RE = re.compile(r"^\s*%%metro\s+(grid|direction)\s*:", re.IGNORECASE)


def _strip_grid_and_direction(text: str) -> str:
    """Drop every ``%%metro grid:``/``direction:`` line so the map lays out
    under pure auto-inference."""
    return "\n".join(
        line for line in text.splitlines() if not _GRID_DIRECTIVE_RE.match(line)
    )


@pytest.mark.parametrize("fixture", ["sarek_metro.mmd", "rnaseq_auto.mmd"])
def test_perp_entry_run_stays_in_section_bbox(fixture):
    """A perpendicular-entry shift keeps the run inside its section bbox (#875).

    Auto-layout can place a horizontal (LR/RL) section as a run fed by a
    same-column vertical drop, so its only port is a perpendicular TOP entry.
    Opening the station-elbow gap shifts the run away from that port; the bbox
    must follow on the shift side.  Stripping ``sarek_metro``'s explicit
    ``grid:`` directives drives its ``annotation`` section into exactly this
    state -- before the fix the leftmost station spilled past ``bbox_x`` and
    the always-on bbox-containment guard aborted the render.  ``rnaseq_auto``
    is a clean auto-layout that must keep passing.
    """
    from nf_metro.layout.constants import GUARD_TOLERANCE
    from nf_metro.layout.engine import compute_layout

    text = _strip_grid_and_direction((EXAMPLES / fixture).read_text())
    graph = parse_metro_mermaid(text)
    compute_layout(graph)

    tol = GUARD_TOLERANCE
    junction_ids = graph.junction_ids
    for sid, station in graph.stations.items():
        if station.is_port or sid in junction_ids:
            continue
        sec = graph.sections.get(station.section_id or "")
        if sec is None or sec.bbox_w == 0:
            continue
        left = sec.bbox_x - tol
        right = sec.bbox_x + sec.bbox_w + tol
        assert left <= station.x <= right, (
            f"{fixture}: station {sid!r} x={station.x:.1f} outside section "
            f"{station.section_id!r} bbox x-range "
            f"[{sec.bbox_x:.1f}, {sec.bbox_x + sec.bbox_w:.1f}]"
        )


# --- BT direction & frame-symmetric port inference (#1442) ---


def test_direction_inference_bt():
    """A section whose sole successor sits directly above in the same column
    gets BT (the mirror of the TB drop turn).

    Auto-grid never stacks a successor same-column-above on its own, so the
    grid position is forced here the way ``test_direction_inference_rl`` forces
    a serpentine row.
    """
    graph = _make_graph_with_sections(
        ["work", "above"],
        [("work_s1", "work", "above_s1", "above", "main")],
    )
    successors, predecessors, _ = _build_section_dag(graph)
    _assign_grid_positions(graph, successors, predecessors, max_station_columns=100)

    graph.sections["work"].grid_col = 0
    graph.sections["work"].grid_row = 1
    graph.sections["above"].grid_col = 0
    graph.sections["above"].grid_row = 0

    _infer_directions(graph, successors, predecessors, set())
    assert graph.sections["work"].direction == "BT"


def test_bt_up_and_right_successor_stays_lr():
    """An up-and-to-the-right successor is forward flow, not a BT climb.

    BT's return column envelope excludes the forward (right) column, so a
    section handing off one row up and one column right stays LR and reaches
    its successor with an L-shaped exit.
    """
    graph = _make_graph_with_sections(
        ["work", "above_right"],
        [("work_s1", "work", "above_right_s1", "above_right", "main")],
    )
    successors, predecessors, _ = _build_section_dag(graph)
    _assign_grid_positions(graph, successors, predecessors, max_station_columns=100)

    graph.sections["work"].grid_col = 1
    graph.sections["work"].grid_row = 1
    graph.sections["above_right"].grid_col = 2
    graph.sections["above_right"].grid_row = 0

    _infer_directions(graph, successors, predecessors, set())
    assert graph.sections["work"].direction == "LR"


def test_bt_section_infers_bottom_entry_top_exit():
    """A BT section presents flow-aligned ports: entry BOTTOM, exit TOP.

    Without this the entry lands on the side facing the feeder and the line
    backtracks down through the section's stations to reach the flow-start one.
    """
    graph = _make_graph_with_sections(
        ["work", "feeder", "sink"],
        [
            ("feeder_s1", "feeder", "work_s1", "work", "main"),
            ("work_s1", "work", "sink_s1", "sink", "main"),
        ],
    )
    successors, predecessors, edge_lines = _build_section_dag(graph)
    _assign_grid_positions(graph, successors, predecessors, max_station_columns=100)

    graph.sections["work"].direction = "BT"
    graph.sections["work"].grid_col = 1
    graph.sections["work"].grid_row = 1
    graph.sections["feeder"].grid_col = 2
    graph.sections["feeder"].grid_row = 0
    graph.sections["sink"].grid_col = 0
    graph.sections["sink"].grid_row = 0

    _infer_port_sides(graph, successors, predecessors, edge_lines, set())
    assert graph.sections["work"].entry_hints[0][0] == PortSide.BOTTOM
    assert graph.sections["work"].exit_hints[0][0] == PortSide.TOP


def test_lr_fed_from_below_by_bt_infers_bottom_entry():
    """An LR section fed from directly below by a BT section enters BOTTOM.

    A BT predecessor exits TOP and climbs straight up, so the section it feeds
    accepts that climb on its BOTTOM edge rather than wrapping the feed around
    to the flow-aligned LEFT edge (the mirror of the TB-drop-onto-TOP rule).
    """
    graph = _make_graph_with_sections(
        ["output", "work"],
        [("work_s1", "work", "output_s1", "output", "main")],
    )
    successors, predecessors, edge_lines = _build_section_dag(graph)
    _assign_grid_positions(graph, successors, predecessors, max_station_columns=100)

    graph.sections["work"].direction = "BT"
    # Port inference reads grid positions via grid_overrides, so stack the
    # sections there: output at (0,0), work directly below at (0,1).
    graph.grid_overrides["output"] = (0, 0, 1, 1)
    graph.grid_overrides["work"] = (0, 1, 1, 1)

    _infer_port_sides(graph, successors, predecessors, edge_lines, set())
    assert graph.sections["output"].entry_hints[0][0] == PortSide.BOTTOM


def _bt_section_with_entry(port_y: float) -> MetroGraph:
    """A 3-station BT section (flow up) whose entry port sits at *port_y* and
    feeds the flow-start (bottom) station w1."""
    graph = MetroGraph()
    graph.add_line(MetroLine(id="a", display_name="A", color="#e63946"))
    graph.add_section(Section(id="work", name="Work"))
    graph.sections["work"].direction = "BT"
    for sid, y in [("w1", 300.0), ("w2", 260.0), ("w3", 220.0)]:
        graph.add_station(Station(id=sid, label=sid, section_id="work"))
        graph.stations[sid].x = 100.0
        graph.stations[sid].y = y
    graph.add_station(Station(id="p", label="", section_id="work", is_port=True))
    graph.stations["p"].x = 100.0
    graph.stations["p"].y = port_y
    graph.add_edge(Edge(source="p", target="w1", line_id="a"))
    return graph


def test_entry_landing_backtrack_guard_detects():
    """An entry port on a BT section's flow-end (TOP) edge, feeding the
    flow-start station, reverses past the interior stations and is caught.
    """
    from nf_metro.layout.phases.guards import (
        PhaseInvariantError,
        _guard_entry_landing_no_station_backtrack,
    )

    graph = _bt_section_with_entry(port_y=200.0)
    with pytest.raises(PhaseInvariantError, match="backtrack"):
        _guard_entry_landing_no_station_backtrack(graph, "test")


def test_entry_landing_flow_aligned_passes_guard():
    """A flow-aligned (BOTTOM edge) entry landing passes no interior station
    and does not trip the backtrack guard.
    """
    from nf_metro.layout.phases.guards import (
        _guard_entry_landing_no_station_backtrack,
    )

    graph = _bt_section_with_entry(port_y=340.0)
    _guard_entry_landing_no_station_backtrack(graph, "test")
