"""Regression tests for shared Y grid alignment (Phase 2.5).

These tests verify the grid alignment behaviour across real pipeline
examples.  They cover the issues fixed on the feat/shared-y-grid-alignment
branch and should prevent regressions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


def _load(name: str):
    """Parse and lay out an example pipeline."""
    text = (EXAMPLES_DIR / f"{name}.mmd").read_text()
    g = parse_metro_mermaid(text)
    compute_layout(g)
    return g


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _grid_info(g):
    return g._row_y_grid_info


def _grid_secs(g) -> set[str]:
    secs: set[str] = set()
    for info in _grid_info(g).values():
        secs.update(info["section_ids"])
    return secs


# ---------------------------------------------------------------------------
# Generic grid invariants (parametrized across all grid-aligned examples)
# ---------------------------------------------------------------------------

GRID_EXAMPLES = [
    "variantbenchmarking",
    "variantprioritization",
    "variant_calling",
    "hlatyping",
    "rnaseq_sections",
    "rnaseq_sections_manual",
    "variant_calling_tuned",
]


@pytest.fixture(params=GRID_EXAMPLES)
def grid_graph(request):
    return request.param, _load(request.param)


class TestGridInvariants:
    """Invariants that must hold for every grid-aligned example."""

    def test_stations_on_grid(self, grid_graph):
        """Non-port stations in grid groups land on grid lines."""
        name, g = grid_graph
        for row, info in _grid_info(g).items():
            eff = info["slot_spacing"]
            for sec_id in info["section_ids"]:
                stations = [
                    s
                    for s in g.stations.values()
                    if s.section_id == sec_id and not s.is_port
                ]
                if not stations:
                    continue
                first_y = min(s.y for s in stations)
                for s in stations:
                    # Exempt hub stations (large fan-out centers)
                    if s.label == "" or "_hub" in s.id:
                        continue
                    off = (s.y - first_y) % eff
                    assert off < 1.0 or abs(off - eff) < 1.0, (
                        f"{name} {s.id}: y={s.y:.1f} off-grid "
                        f"(first_y={first_y:.1f}, eff={eff})"
                    )

    def test_bbox_top_aligned(self, grid_graph):
        """Section bbox tops align within each row group."""
        name, g = grid_graph
        for row, info in _grid_info(g).items():
            tops = [
                g.sections[sec_id].bbox_y
                for sec_id in info["section_ids"]
                if sec_id in g.sections and g.sections[sec_id].bbox_h > 0
            ]
            if len(tops) >= 2:
                assert max(tops) - min(tops) < 2.0, (
                    f"{name} row {row}: bbox tops differ: {tops}"
                )

    def test_bottom_padding_at_least_top(self, grid_graph):
        """Bot padding >= top padding (no content below bbox).

        With trunk-Y alignment, sections may shift content downward, so
        top padding can exceed the original symmetric value.  But bottom
        padding must remain non-negative (content within bbox) and at
        least as large as the original section_y_padding floor.
        """
        name, g = grid_graph
        for sec_id in _grid_secs(g):
            sec = g.sections.get(sec_id)
            if not sec or sec.bbox_w == 0:
                continue
            stations = [
                s
                for s in g.stations.values()
                if s.section_id == sec_id and not s.is_port
            ]
            if not stations:
                continue
            max_y = max(s.y for s in stations)
            bot_pad = (sec.bbox_y + sec.bbox_h) - max_y
            assert bot_pad >= -0.5, (
                f"{name} {sec_id}: content below bbox bot_pad={bot_pad:.1f}"
            )


# ---------------------------------------------------------------------------
# Issue K: variantbenchmarking preprocess exit -> normalization entry
# ---------------------------------------------------------------------------


class TestIssueK:
    """Phase 10d must not drag exit ports away from matching downstream entry."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.g = _load("variantbenchmarking")

    def test_preprocess_exit_matches_normalization_entry(self):
        pe = self.g.stations["preprocess__exit_right_1"]
        ne = self.g.stations["normalization__entry_left_7"]
        assert abs(pe.y - ne.y) < 1.0, (
            f"preprocess exit y={pe.y} != normalization entry y={ne.y}"
        )


# ---------------------------------------------------------------------------
# Issue L: variant_calling_tuned alignment exit -> variant_calling entry
# ---------------------------------------------------------------------------


class TestIssueL:
    """Phase 10d must preserve straight alignment->variant_calling connection."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.g = _load("variant_calling_tuned")

    def test_alignment_exit_matches_variant_calling_entry(self):
        ae = self.g.stations["alignment__exit_right_1"]
        ve = self.g.stations["variant_calling__entry_left_4"]
        assert abs(ae.y - ve.y) < 1.0, (
            f"alignment exit y={ae.y} != variant_calling entry y={ve.y}"
        )


# ---------------------------------------------------------------------------
# Issue M: rnaseq_sections_manual 3-way fan-out
# ---------------------------------------------------------------------------


class TestIssueM:
    """Same-layer slot separation must produce 3 distinct Y levels, not 2."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.g = _load("rnaseq_sections_manual")

    def test_three_distinct_fan_out_levels(self):
        bb = self.g.stations["bbsplit"].y
        sm = self.g.stations["sortmerna"].y
        rd = self.g.stations["ribodetector"].y
        ys = sorted({bb, sm, rd})
        assert len(ys) == 3, (
            f"Expected 3 distinct Y levels, got {len(ys)}: "
            f"bbsplit={bb}, sortmerna={sm}, ribodetector={rd}"
        )

    def test_fastp_trimgalore_separated(self):
        fp = self.g.stations["fastp"].y
        tg = self.g.stations["trimgalore"].y
        assert abs(fp - tg) > 1.0, f"fastp and trimgalore collapsed: fp={fp}, tg={tg}"


# ---------------------------------------------------------------------------
# Issue N: variantbenchmarking MultiQC label placement
# ---------------------------------------------------------------------------


class TestIssueN:
    """MultiQC Report label must have a 2-slot gap for sandwiched multi-line labels."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.g = _load("variantbenchmarking")

    def test_multiqc_has_2_slot_gap_above(self):
        """Sandwiched multi-line label gets 2-slot gap from row above."""
        info = _grid_info(self.g)
        # Find the row containing output_processing
        eff = None
        for row, ri in info.items():
            if "output_processing" in ri["section_ids"]:
                eff = ri["slot_spacing"]
                break
        assert eff is not None

        mq = self.g.stations["multiqc"]
        mco = self.g.stations["merged_csvs_out"]
        gap = mq.y - mco.y
        assert gap >= eff * 1.9, (
            f"MultiQC gap above={gap:.1f} < 2 slots ({eff * 2:.1f})"
        )

    def test_liftover_subsample_single_slot(self):
        """Non-sandwiched multi-line label keeps 1-slot gap."""
        info = _grid_info(self.g)
        eff = None
        for row, ri in info.items():
            if "preprocess" in ri["section_ids"]:
                eff = ri["slot_spacing"]
                break
        assert eff is not None

        sub = self.g.stations["subsample"]
        lift = self.g.stations["liftover"]
        gap = lift.y - sub.y
        assert gap < eff * 1.5, (
            f"Liftover gap={gap:.1f} too large (should be ~{eff:.1f})"
        )


# ---------------------------------------------------------------------------
# Fan-in exit preservation (filtering section)
# ---------------------------------------------------------------------------


class TestFanInExitPreservation:
    """Phase 10d must keep centered midpoint for 3+ source fan-in exits."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.g = _load("variantbenchmarking")

    def test_filtering_exit_not_collapsed_to_source(self):
        filt = self.g.stations["filtering__exit_right_3"]
        bf = self.g.stations["bcftools_filter"]
        sf = self.g.stations["survivor_filter"]
        # Exit should be between the two sources, not at either one
        assert abs(filt.y - bf.y) > 5.0, (
            f"filtering exit y={filt.y} collapsed to bcftools_filter y={bf.y}"
        )
        assert abs(filt.y - sf.y) > 5.0, (
            f"filtering exit y={filt.y} collapsed to survivor_filter y={sf.y}"
        )

    def test_bcftools_survivor_same_x(self):
        bf = self.g.stations["bcftools_filter"]
        sf = self.g.stations["survivor_filter"]
        assert abs(bf.x - sf.x) < 1.0, (
            f"bcftools_filter x={bf.x} != survivor_filter x={sf.x}"
        )


# ---------------------------------------------------------------------------
# variantprioritization: germline line alignment
# ---------------------------------------------------------------------------


class TestVariantprioritizationGermline:
    """Germline line through Reference -> CPSR must be straight horizontal."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.g = _load("variantprioritization")

    def test_cpsr_report_cpsr_same_y(self):
        assert abs(self.g.stations["cpsr"].y - self.g.stations["report_cpsr"].y) < 1.0

    def test_cpsr_get_pcgr_same_y(self):
        assert abs(self.g.stations["cpsr"].y - self.g.stations["get_pcgr"].y) < 1.0

    def test_sec5_entry_at_cpsr_y(self):
        cpsr_y = self.g.stations["cpsr"].y
        for pid, p in self.g.ports.items():
            if p.section_id == "run_cpsr" and p.is_entry:
                st = self.g.stations[pid]
                assert abs(st.y - cpsr_y) < 1.0, (
                    f"sec5 entry {pid} y={st.y} != cpsr y={cpsr_y}"
                )
                return
        pytest.fail("No entry port found for run_cpsr section")

    def test_sec3_exit_at_get_pcgr_y(self):
        pcgr_y = self.g.stations["get_pcgr"].y
        for pid, p in self.g.ports.items():
            if p.section_id == "get_reference" and not p.is_entry:
                st = self.g.stations[pid]
                if abs(st.y - pcgr_y) < 2.0:
                    return
        pytest.fail(f"No get_reference exit port at get_pcgr y={pcgr_y}")


# ---------------------------------------------------------------------------
# variantbenchmarking: liftover spacing and output_processing uniformity
# ---------------------------------------------------------------------------


class TestVariantbenchmarkingSpacing:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.g = _load("variantbenchmarking")

    def test_liftover_one_slot_below_subsample(self):
        info = _grid_info(self.g)
        eff = None
        for row, ri in info.items():
            if "preprocess" in ri["section_ids"]:
                eff = ri["slot_spacing"]
                break
        assert eff is not None
        sub = self.g.stations["subsample"]
        lift = self.g.stations["liftover"]
        gap = lift.y - sub.y
        assert abs(gap - eff) < 2.0, f"liftover gap={gap:.1f} != slot_spacing={eff}"

    def test_output_processing_uniform_gaps(self):
        op_ys = sorted(
            set(
                s.y
                for s in self.g.stations.values()
                if s.section_id == "output_processing" and not s.is_port
            )
        )
        gaps = [op_ys[i + 1] - op_ys[i] for i in range(len(op_ys) - 1)]
        unique_gaps = set(round(g, 1) for g in gaps)
        # Allow at most 2 distinct gap sizes (the 2-slot gap for
        # sandwiched multi-line labels creates a larger gap).
        assert len(unique_gaps) <= 2, f"Non-uniform gaps: {gaps}"

    def test_benchmarking_layer1_same_x(self):
        """All layer-1 stations in benchmarking section share X (bubble centering)."""
        layer1 = [
            s
            for s in self.g.stations.values()
            if s.section_id == "benchmarking" and s.layer == 1 and not s.is_port
        ]
        assert len(layer1) >= 2
        xs = [s.x for s in layer1]
        assert max(xs) - min(xs) < 2.0, f"layer-1 X spread: {xs}"


# ---------------------------------------------------------------------------
# Trunk-Y alignment is gated on identical bundles across a row group
# ---------------------------------------------------------------------------


class TestTrunkAlignBundleGate:
    """`_align_row_trunk_ys` must only fire when adjacent same-row sections
    share the same bundle of lines across their LR ports.  When bundles
    differ (e.g. one section carries an extra bypass line that the next
    drops), forcing a common trunk Y just shifts content downward for no
    geometric gain.  When bundles match, the alignment is necessary so the
    inter-section bundle flows horizontally across the row.
    """

    def test_differing_bundles_do_not_shift(self):
        """variant_calling: bundles differ across the row -> no station shift."""
        from nf_metro.layout.engine import (
            _align_row_trunk_ys,
            _section_bundle_lines,
            _section_trunk_y,
        )

        g = _load("variant_calling")
        # Sections in row 0 have differing bundles (preprocess/reporting
        # carry the qc line; alignment/variant_calling do not).
        row0 = sorted(
            (s for s in g.sections.values() if s.grid_row == 0 and s.bbox_h > 0),
            key=lambda s: s.grid_col,
        )
        bundles = [_section_bundle_lines(g, s) for s in row0 if s.direction == "LR"]
        non_empty = [b for b in bundles if b]
        assert any(b != non_empty[0] for b in non_empty), (
            "fixture precondition: bundles must differ across the row"
        )

        # Snapshot Y of every internal station, run the gated phase
        # again, and confirm nothing moves.
        before = {sid: s.y for sid, s in g.stations.items()}
        _align_row_trunk_ys(g)
        for sid, s in g.stations.items():
            assert abs(s.y - before[sid]) < 0.5, (
                f"{sid}: y shifted by {s.y - before[sid]:.2f} despite differing bundles"
            )

        # And: trunk Ys remain at their pre-alignment values (some at
        # 120, some at 160).  If the gate were absent, all would equal
        # the deepest trunk.
        trunks = {s.id: _section_trunk_y(g, s) for s in row0 if s.direction == "LR"}
        trunk_vals = {t for t in trunks.values() if t is not None}
        assert len(trunk_vals) > 1, (
            f"trunks should remain diverged when bundles differ; got {trunks}"
        )

    def test_matching_bundles_do_align(self):
        """Synthetic DA-style fixture: identical bundle, diverged trunks -> align.

        secA, secB, secC all carry the same {a, b} bundle through their
        LR ports.  secB has two stations above its trunk, so its
        unaligned trunk Y sits below secA's and secC's.  The fix must
        shift secA and secC down so all three trunks share secB's Y,
        keeping the bundle horizontal across the row.
        """
        from nf_metro.layout.engine import _section_trunk_y

        fixture = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "trunk_align_matching_bundle.mmd"
        )
        text = fixture.read_text()
        g = parse_metro_mermaid(text)
        compute_layout(g)

        row0 = sorted(
            (s for s in g.sections.values() if s.grid_row == 0 and s.direction == "LR"),
            key=lambda s: s.grid_col,
        )
        trunks = {s.id: _section_trunk_y(g, s) for s in row0}
        trunk_vals = [t for t in trunks.values() if t is not None]
        assert len(trunk_vals) >= 3, (
            f"fixture precondition: need 3 trunks; got {trunks}"
        )
        # With matching bundles the gate lets alignment proceed: all
        # trunks should land on a single Y.
        assert max(trunk_vals) - min(trunk_vals) < 1.0, (
            f"trunks should align when bundles match; got {trunks}"
        )

    def test_variantbenchmarking_inputs_not_pulled_down(self):
        """variantbenchmarking row 0 mixes bundles ({test,truth} vs {test}).

        Pre-fix, the gate-less algorithm shifted `inputs` (trunk 120)
        down to match preprocess/normalization (trunk 160), pulling
        samplesheet from y=120 to y=160 and inflating the inputs bbox
        height by 40px.  With the bundle gate, inputs stays on its
        natural Y because filtering breaks the row-wide bundle.
        """
        g = _load("variantbenchmarking")
        # samplesheet sits at the top of the inputs section; its
        # untouched Y is 120.  Over-application drags it to 160.
        ss = g.stations["samplesheet"]
        assert ss.y < 130, (
            f"samplesheet y={ss.y} suggests inputs was shifted down "
            f"despite bundle mismatch with filtering"
        )
        # The inputs bbox should not have been inflated.
        inputs_sec = g.sections["inputs"]
        assert inputs_sec.bbox_h < 300, (
            f"inputs bbox_h={inputs_sec.bbox_h} inflated; alignment leaked through gate"
        )


# ---------------------------------------------------------------------------
# Inter-section port pair snap (final-polish phase 13c)
# ---------------------------------------------------------------------------


class TestInterSectionPortSnap:
    """Exit port Y snaps to downstream entry Y in explicit-grid pipelines."""

    def test_snap_aligns_rowspan_neighbour_to_row_trunk(self):
        """With explicit grid, an LR exit port whose section's trunk Y
        differs from a same-row downstream entry snaps to the entry Y."""
        mmd = (
            "%%metro line: main | Main | #ff0000\n"
            "%%metro grid: a | 0,0,2,1\n"
            "%%metro grid: b | 1,0\n"
            "graph LR\n"
            "    subgraph a [A]\n"
            "        a1[A1]\n"
            "        a2[A2]\n"
            "        a1 -->|main| a2\n"
            "    end\n"
            "    subgraph b [B]\n"
            "        b1[B1]\n"
            "        b2[B2]\n"
            "        b1 -->|main| b2\n"
            "    end\n"
            "    a2 -->|main| b1\n"
        )
        g = parse_metro_mermaid(mmd)
        compute_layout(g)
        a_exit = next(g.stations[pid] for pid in g.sections["a"].exit_ports)
        b_entry = next(g.stations[pid] for pid in g.sections["b"].entry_ports)
        assert abs(a_exit.y - b_entry.y) < 1.0, (
            f"a exit y={a_exit.y} != b entry y={b_entry.y}"
        )

    def test_auto_layout_unaffected(self):
        """The snap stays off for purely auto-layout pipelines."""
        g = _load("variant_calling_tuned")
        # Without any %%metro grid: directive, no explicit_grid entries.
        assert not g._explicit_grid


# ---------------------------------------------------------------------------
# Grid-snap exclusions for TB exits, convergence ports, and TB bbox bottoms
# (PR #276 regression fixes)
# ---------------------------------------------------------------------------


TOPOLOGY_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "topologies"
NEXTFLOW_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "nextflow"


def _load_topology(name: str):
    text = (TOPOLOGY_FIXTURES / f"{name}.mmd").read_text()
    g = parse_metro_mermaid(text)
    compute_layout(g)
    return g


def _load_nextflow(name: str):
    from nf_metro.convert import convert_nextflow_dag

    text = (NEXTFLOW_FIXTURES / f"{name}.mmd").read_text()
    text = convert_nextflow_dag(text)
    g = parse_metro_mermaid(text)
    compute_layout(g)
    return g


class TestGridSnapExclusions:
    """Regressions for PR #276 grid-snap side effects."""

    def test_tb_section_exit_y_not_snapped(self):
        """TB-section LR/RL exit ports must align with their downstream
        target's entry-port Y, not be snapped to the TB's own grid.

        In ``fold_double`` the TB sections ``calling`` and ``integration``
        feed an LR/RL section in the row below.  ``_resolve_tb_exit_y``
        places the exit port at the downstream entry's Y (e.g. 330 for
        ``calling`` -> ``hard_filter``).  The grid snap must leave that Y
        alone so the inter-section line stays straight.
        """
        g = _load_topology("fold_double")
        # calling (TB row 0) -> hard_filter (RL row 1)
        calling_exits = [g.stations[pid] for pid in g.sections["calling"].exit_ports]
        hard_filter_entries = [
            g.stations[pid] for pid in g.sections["hard_filter"].entry_ports
        ]
        assert calling_exits and hard_filter_entries
        for ex in calling_exits:
            ey = min(
                (en.y for en in hard_filter_entries),
                key=lambda y: abs(y - ex.y),
            )
            assert abs(ex.y - ey) < 0.5, (
                f"TB calling exit y={ex.y} != hard_filter entry y={ey}"
            )
        # integration (TB row 1) -> reporting (LR row 2)
        integration_exits = [
            g.stations[pid] for pid in g.sections["integration"].exit_ports
        ]
        reporting_entries = [
            g.stations[pid] for pid in g.sections["reporting"].entry_ports
        ]
        for ex in integration_exits:
            ey = min(
                (en.y for en in reporting_entries),
                key=lambda y: abs(y - ex.y),
            )
            assert abs(ex.y - ey) < 0.5, (
                f"TB integration exit y={ex.y} != reporting entry y={ey}"
            )

    def test_convergence_port_y_is_midpoint(self):
        """Stations that converge two upstream sources at different Ys
        should sit at the midpoint of those Ys, not snap to grid.

        ``flat_pipeline`` joins Fastqc (top track) and Sort Bam (bottom
        track) at MULTIQC.  The MULTIQC station should be at the
        midpoint of those source Ys; snapping it to Fastqc's Y would
        force the lower line to fan up to reach it.
        """
        g = _load_nextflow("flat_pipeline")
        st_by_label = {
            (s.label or "").lower(): s for s in g.stations.values() if not s.is_port
        }
        fastqc = st_by_label["fastqc"]
        sort_bam = st_by_label["sort bam"]
        multiqc = st_by_label["multiqc"]
        midpoint = (fastqc.y + sort_bam.y) / 2.0
        assert abs(multiqc.y - midpoint) < 1.0, (
            f"multiqc y={multiqc.y} should be midpoint of "
            f"fastqc y={fastqc.y} and sort_bam y={sort_bam.y} "
            f"(midpoint={midpoint})"
        )

    def test_tb_section_bbox_aligns_with_row_lr_neighbours(self):
        """TB section bbox bottom must align with its downstream LR
        target's bbox bottom so the inter-section exit line sits inside
        the box rather than along the edge.

        In ``fold_double``:
          * calling (TB row 0) -> hard_filter (LR row 1) bbox bottoms align
          * integration (TB row 1) -> reporting (LR row 2) bbox bottoms align
        """
        g = _load_topology("fold_double")
        calling = g.sections["calling"]
        hard_filter = g.sections["hard_filter"]
        calling_bot = calling.bbox_y + calling.bbox_h
        hard_filter_bot = hard_filter.bbox_y + hard_filter.bbox_h
        assert abs(calling_bot - hard_filter_bot) < 1.0, (
            f"calling bbox bottom {calling_bot} != hard_filter bottom "
            f"{hard_filter_bot}; TB exit line sits at section edge"
        )
        integration = g.sections["integration"]
        reporting = g.sections["reporting"]
        integration_bot = integration.bbox_y + integration.bbox_h
        reporting_bot = reporting.bbox_y + reporting.bbox_h
        assert abs(integration_bot - reporting_bot) < 1.0, (
            f"integration bbox bottom {integration_bot} != reporting "
            f"bottom {reporting_bot}; TB exit line sits at section edge"
        )
