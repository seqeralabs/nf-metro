#!/usr/bin/env python3
"""Build the docs gallery: render .mmd examples to SVG and generate gallery/index.md.

Usage:
    python scripts/build_gallery.py
    python scripts/build_gallery.py --debug   # include debug overlay
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "tests"))

from layout_metrics import compute_metrics  # noqa: E402

from nf_metro.convert import convert_nextflow_dag  # noqa: E402
from nf_metro.layout.engine import compute_layout  # noqa: E402
from nf_metro.parser.mermaid import parse_metro_mermaid  # noqa: E402
from nf_metro.render.svg import render_svg  # noqa: E402
from nf_metro.themes import THEMES  # noqa: E402

DEBUG_RENDERS = "--debug" in sys.argv

EXAMPLES_DIR = project_root / "examples"
NEXTFLOW_FIXTURES_DIR = project_root / "tests" / "fixtures" / "nextflow"
TEST_FIXTURES_DIR = project_root / "tests" / "fixtures"
TOPOLOGIES_DIR = project_root / "examples" / "topologies"
GUIDE_DIR = project_root / "examples" / "guide"
# Markdown content lives in the repo-root docs/ dir (the Astro site in website/
# loads it via a symlink: website/src/content/docs -> ../../docs).
GALLERY_DIR = project_root / "docs" / "gallery"
PIPELINES_DIR = project_root / "docs" / "pipelines"
RENDERS_DIR = project_root / "docs" / "assets" / "renders"

# Base-absolute URL prefix for generated *page* links (matches astro.config `base`).
SITE_BASE = "/nf-metro/"

# Ordered list of examples. Each entry is (filename_stem, source_dir, description).
# Main examples first, then topologies grouped by category.
GALLERY_ENTRIES: list[tuple[str, Path, str]] = [
    # --- Main examples ---
    (
        "simple_pipeline",
        EXAMPLES_DIR,
        "Minimal two-line pipeline with no sections.",
    ),
    (
        "directional_flow",
        EXAMPLES_DIR,
        "Opt-in static flow chevrons via `%%metro directional: true` (CLI: "
        "`--directional`): periodic open chevrons ride each route pointing "
        "source to target. A three-line bundled trunk fans out to per-line "
        "analyses and converges again, so the chevrons make bundle, fan-out, "
        "and merge direction legible without the animated `--animate` balls. "
        "Marker size, spacing, opacity, and colour are `Theme` knobs.",
    ),
    (
        "line_spread",
        EXAMPLES_DIR,
        "Demonstrates the `%%metro line_spread:` axis with one per-section "
        "override per section: `bundle` (default) merges shared lines onto one "
        "trunk that cascades downward, `centered` balances that bundle about "
        "the midline, and `rails` draws each line as a parallel rail with "
        "shared stations as interchanges.",
    ),
    (
        "cross_track_interchange",
        EXAMPLES_DIR,
        "Demonstrates `%%metro interchange:`: parallel tumour/normal lanes share "
        "one MarkDuplicates step without merging, drawn as a cross-track "
        "interchange so each lane stays straight on its own track instead of "
        "pinching together to a point. Auto-layout infers the same interchange "
        "for fully-parallel lanes even without the directive.",
    ),
    (
        "rnaseq_auto",
        EXAMPLES_DIR,
        "Demonstrates fully auto-inferred layout: no `%%metro grid:` directives "
        f"needed. See [nf-core Pipelines]({SITE_BASE}pipelines/) for the full gallery.",
    ),
    (
        "rnaseq_sections",
        EXAMPLES_DIR,
        "Same pipeline with manual `%%metro grid:` overrides and file markers, "
        "showing how explicit directives can fine-tune placement.",
    ),
    (
        "genomic_pipeline",
        EXAMPLES_DIR,
        "Multi-section genomic variant-calling pipeline: same-direction sections "
        "stacked in one column (serpentine carriage-return) plus a multi-row QC "
        "collector fan-in descending a shared inter-column corridor.",
    ),
    (
        "marker_styles",
        EXAMPLES_DIR,
        "Per-station marker shapes & fills encoding tool attributes "
        "(mandatory/optional/accelerated/expanded-elsewhere) with a marker key "
        "alongside the line legend. Demonstrates `%%metro marker:` and "
        "`%%metro marker_legend:`.",
    ),
    (
        "diagonal_labels",
        EXAMPLES_DIR,
        "Opt-in diagonal station labels via `%%metro label_angle: 45`: a "
        "dense pre-processing trunk whose tilted names pack tighter than "
        "horizontal labels would, feeding a variant-calling section in the row "
        "below that fans out to three callers and back in -- the reserved "
        "vertical room keeps the hanging labels clear of the row beneath.",
    ),
    (
        "longread_variant_calling",
        EXAMPLES_DIR,
        "Dense long-read variant-calling pipeline (six lines, nine sections): "
        "exercises inter-section routing around non-connecting section boxes, "
        "cross-row feeds via the inter-row gap, same-line bundle coincidence, "
        "and a same-colour crossover bridge.",
    ),
    (
        "differentialabundance",
        EXAMPLES_DIR,
        "nf-core/differentialabundance with four input lines, off-track "
        "gene-set inputs, and a bypass-heavy reporting row. Uses "
        "`%%metro center_ports: true`.",
    ),
    (
        "differentialabundance_default",
        EXAMPLES_DIR,
        "Same nf-core/differentialabundance map at default (uncentered) "
        "layout — useful for spotting regressions that only show with "
        "default port placement.",
    ),
    (
        "off_track_outputs",
        EXAMPLES_DIR,
        "Off-track file artefacts hung above a pre-processing trunk at the "
        "step that writes each one: `%%metro off_track:` on a producer-fed "
        "sink anchors it to its producer (not the section top), mirroring the "
        "off-track input mechanism for outputs. Each output gets its own "
        "column clear of the next station, and a step writing several files "
        "(MarkDup here) stacks them above it.",
    ),
    (
        "legend_logo_placement",
        EXAMPLES_DIR,
        "Demonstrates positioning the bundled legend+logo block: `%%metro "
        "legend: br | canvas` pins it to the empty lower-right canvas corner and "
        "`%%metro logo_scale:` enlarges the embedded logo. The directive also "
        "supports `| dx,dy` offsets and absolute `x,y` placement; the block "
        "auto-avoids sections and routes. The QC line shows a downward "
        "cross-column feeder dropping straight into its consumer section.",
    ),
    (
        "file_icons",
        EXAMPLES_DIR,
        "Terminus icon variants: single-sheet `%%metro file:`, "
        "stacked `%%metro files:` for multiplicity, the `%%metro dir:` folder, "
        "and the optional `| banner` format strip.",
    ),
    (
        "legend_combo",
        EXAMPLES_DIR,
        "Demonstrates `%%metro legend_combo:`: a normal (blue) and tumor (red) "
        "line travel together as a tumor-normal pair. The combo renders one "
        "legend row with a striped red+blue swatch. Normal travels only within "
        "the bundle so its individual row is suppressed, while tumor breaks "
        "away alone to Annotate and keeps its own row; the QC line is unaffected.",
    ),
    (
        "tb_file_termini",
        EXAMPLES_DIR,
        "A `%%metro direction: TB` reporting section whose file outputs are "
        "line termini. Regression fixture: terminus file icons orient to a "
        "vertical flow, with the connector entering from the top.",
    ),
    (
        "genomeassembly_staggered",
        EXAMPLES_DIR,
        "sanger-tol/genomeassembly with explicit `%%metro grid:` directives "
        "stacking each section in its own grid row. Regression fixture: "
        "cross-column junction routes were going backward in X.",
    ),
    (
        "group_labels",
        EXAMPLES_DIR,
        "Annotative `%%metro group:` band captions labelling sarek-style "
        "sub-families of callers (SNPs & Indels / SV & CNV / MSI) within a "
        "single section, without splitting them apart.",
    ),
    (
        "sarek_metro",
        EXAMPLES_DIR,
        "Integration showcase: a sarek-style variant-calling pipeline drawn "
        "with diagonal (45-degree) labels for tight column packing, an "
        "off-track FASTQ input, file termini, marker styles, and a "
        "`%%metro line_spread: rails` panel where each caller keeps its own "
        "rail and shared stations render as interchanges.",
    ),
    (
        "disconnected_components",
        EXAMPLES_DIR,
        "A connected three-section trunk plus a separate, wide disconnected "
        "section. Each weakly-connected component of the section graph is "
        "placed in its own local column grid and the components are stacked "
        "vertically, so the wide panel never inflates the trunk's columns or "
        "flings its later sections to the right.",
    ),
    # --- Simple topologies ---
    (
        "single_section",
        TOPOLOGIES_DIR,
        "One section, one line. The simplest possible case.",
    ),
    (
        "bt_chain",
        TOPOLOGIES_DIR,
        "A `%%metro direction: BT` (bottom-to-top) section: a three-station chain "
        "whose flow runs up the column, the vertical mirror of a TB chain (#1044).",
    ),
    (
        "bt_fork",
        TOPOLOGIES_DIR,
        "A symmetric fan-out inside a `%%metro direction: BT` section: the hub sits "
        "at the bottom and both branches fan upward, the lane bundle riding the "
        "`+x` side as the rotation image of a TB fork (#1044).",
    ),
    (
        "bt_perp_entry_below",
        TOPOLOGIES_DIR,
        "A BT section fed from below: a lower BT section's trailing TOP exit "
        "continues up the shared lane into an upper BT section's BOTTOM entry, a "
        "two-line bundle riding one column across the seam (#1044).",
    ),
    (
        "bt_exit_top_above",
        TOPOLOGIES_DIR,
        "A BT section's trailing TOP exit dropping up into the BOTTOM entry of an "
        "LR section stacked above it (#1044).",
    ),
    (
        "bt_exit_top_above_2line",
        TOPOLOGIES_DIR,
        "The two-line form of the BT TOP-exit drop into an LR BOTTOM entry: the "
        "bundle keeps its lane across the perpendicular boundary, the BT feeder "
        "fanning +x rather than the downward-TB -x (#1066).",
    ),
    (
        "bt_to_lr",
        TOPOLOGIES_DIR,
        "A BT section's trailing TOP (perpendicular) exit taking the up-and-over "
        "corridor into the LEFT entry of a neighbouring LR section, a two-line "
        "bundle staying parallel across the seam (#1044).",
    ),
    (
        "bt_to_tb",
        TOPOLOGIES_DIR,
        "A BT section's RIGHT exit feeding a TB section's LEFT entry: an upward "
        "flow handing off to a downward one across the column seam (#1044).",
    ),
    (
        "deep_linear",
        TOPOLOGIES_DIR,
        "Seven sections in a straight chain. Exercises the grid fold threshold.",
    ),
    (
        "inrow_skip_breeze",
        TOPOLOGIES_DIR,
        "An express line skips a station three collinear stations share; the "
        "geometric bypass pass bows it around the skipped marker (#990).",
    ),
    (
        "parallel_independent",
        TOPOLOGIES_DIR,
        "Two disconnected pipelines stacked vertically.",
    ),
    # --- Fan-out and fan-in ---
    (
        "wide_fan_out",
        TOPOLOGIES_DIR,
        "One source fanning out to four target sections.",
    ),
    (
        "wide_fan_in",
        TOPOLOGIES_DIR,
        "Four sources converging into one target section.",
    ),
    (
        "section_diamond",
        TOPOLOGIES_DIR,
        "Section-level fork-join: fan-out then reconverge.",
    ),
    (
        "uneven_diamond",
        TOPOLOGIES_DIR,
        "Node-level fork-join whose branches differ in length; each branch "
        "holds its own track instead of collapsing the shorter ones together.",
    ),
    (
        "symmetric_diamond_beside_wide_fan",
        TOPOLOGIES_DIR,
        "A `diamond_style: symmetric` 2-way fork-join sharing a section with a "
        "wider 3-way fan. The 2-way diamond straddles its trunk evenly, but at "
        "full pitch -- as tall as the 3-way fan, with an empty trunk row "
        "between its branches -- because the half-pitch compaction is a "
        "per-section decision the mixed-fan section cannot qualify for (#1076).",
    ),
    (
        "terminal_symmetric_fan",
        TOPOLOGIES_DIR,
        "A terminal section whose entry fans into equal-rank sinks; the "
        "fan stays symmetric about the entry port (regression lock for "
        "top-anchored terminal fans).",
    ),
    (
        "trunk_through_fan",
        TOPOLOGIES_DIR,
        "A pass-through section: the trunk runs straight through a "
        "symmetric fan-and-reconverge, exit on the merge row (regression "
        "lock for detached reconvergence exits).",
    ),
    (
        "wide_label_fan",
        TOPOLOGIES_DIR,
        "A two-column fan whose station labels are wider than the column "
        "pitch; the engine wraps the labels and widens spacing so they "
        "don't collide.",
    ),
    (
        "wrapped_label_trunk",
        TOPOLOGIES_DIR,
        "A wrapped station label on a lower track whose block would grow into "
        "the metro line on the track above; the label is pulled back to its "
        "un-pushed anchor so the name clears the line.",
    ),
    (
        "funcprofiler_upstream",
        TOPOLOGIES_DIR,
        "A profiling section whose stacked tools share a fan-in/fan-out: a line "
        "the 'FMH FunProfiler' station does not carry would rake its wide "
        "below-station label, so the label flips to its clear side and the "
        "diagonal no longer strikes the glyphs.",
    ),
    # --- Branching and multipath ---
    (
        "asymmetric_tree",
        TOPOLOGIES_DIR,
        "One root branching into three paths of different depths.",
    ),
    (
        "complex_multipath",
        TOPOLOGIES_DIR,
        "Four lines taking different routes through six sections.",
    ),
    # --- Multi-line bundles ---
    (
        "interchange_lane_reorder",
        TOPOLOGIES_DIR,
        "Two lanes share one step while a third lane is declared between them. "
        "Auto-layout reorders the interleaving lane to an outer track so the "
        "two members become adjacent and infer a clean interchange, instead of "
        "abstaining (issue #779).",
    ),
    (
        "multi_line_bundle",
        TOPOLOGIES_DIR,
        "Six lines travelling through the same three-section chain.",
    ),
    (
        "mixed_port_sides",
        TOPOLOGIES_DIR,
        "A section with both RIGHT and BOTTOM exits.",
    ),
    # --- Realistic pipelines ---
    (
        "rnaseq_lite",
        TOPOLOGIES_DIR,
        "Simplified RNA-seq pipeline with three analysis routes.",
    ),
    (
        "variant_calling",
        TOPOLOGIES_DIR,
        "Variant calling pipeline with four lines sharing alignment.",
    ),
    # --- Fold topologies ---
    (
        "fold_fan_across",
        TOPOLOGIES_DIR,
        "Three lines diverge, converge at a fold, then continue on the return row.",
    ),
    (
        "fold_double",
        TOPOLOGIES_DIR,
        "Ten-section linear pipeline with two fold points (serpentine layout).",
    ),
    (
        "fold_stacked_branch",
        TOPOLOGIES_DIR,
        "Stacked analysis sections feeding through a fold into branching targets.",
    ),
    (
        "reconverge_reversed_fold",
        TOPOLOGIES_DIR,
        "Serpentine-fold reconvergence: a multi-modal pipeline fanning out to "
        "stacked analysis sections and reconverging onto a reversed return row.",
    ),
    (
        "stacked_lr_serpentine",
        TOPOLOGIES_DIR,
        "Same-direction sections stacked in one grid column, chained via short "
        "vertical drops on alternating sides (serpentine), no wrap-around.",
    ),
    (
        "tb_left_exit_step",
        TOPOLOGIES_DIR,
        "A TB alignment section exits LEFT into a lower right-entry section with "
        "a blocker directly below: the exit bundle steps west-down-west, routed "
        "as a parallel staircase that keeps the feed order (issue #671).",
    ),
    (
        "tb_convergence_straight_drop",
        TOPOLOGIES_DIR,
        "Two lines converge at a TB section's terminal merge; the feeder whose "
        "source is collinear with the merge drops dead straight while the sibling "
        "arrives diagonally, instead of the straight feeder kinking off its lane "
        "(issue #1007).",
    ),
    (
        "left_exit_sink_below",
        TOPOLOGIES_DIR,
        "A TB bridge's LEFT exit feeds a LEFT-entry sink one row below and to the "
        "left: the bundle leads out left and drops straight down a channel clear "
        "of both boxes, routing around the bridge rather than clawing back "
        "through its interior (issue #1083).",
    ),
    (
        "tb_passthrough_continuation",
        TOPOLOGIES_DIR,
        "A TB convergence that is not a sink: a diagonal feeder continues straight "
        "down to a station directly below the merge while a collinear feeder peels "
        "off. The continuation rides the trunk slot so it drops straight, instead "
        "of being forced outboard where it kinks at the merge and crosses the "
        "collinear feeder (issue #1012).",
    ),
    (
        "tb_bottom_exit_fork_diamond",
        TOPOLOGIES_DIR,
        "A TB section's BOTTOM exit forks to two stacked TB sections in different "
        "rows, the lower also fed by the upper (a diamond). The fork junction's "
        "leg into the nearer TOP entry drops straight in its column rather than "
        "jogging sideways and reversing at the boundary, and the leg continuing "
        "to the far section rides the intervening section's own trunk for the "
        "shared line as one stroke (issue #1058).",
    ),
    (
        "tb_bottom_exit_bundle_jog",
        TOPOLOGIES_DIR,
        "A four-line bundle leaves a TB section's BOTTOM exit and jogs down into "
        "the TOP entry of an RL section placed in the row below and one column "
        "to the left. The four lines keep distinct channels through the jog "
        "instead of collapsing onto one (issue #1074).",
    ),
    (
        "branch_fold_forward",
        TOPOLOGIES_DIR,
        "A side branch (Aux) shares a topo column with the spine (Genome). At a "
        "low fold threshold the serpentine packer skips that branch column as a "
        "fold point - folding it would strand Genome's consumer (Post) behind "
        "it - and folds the spine instead, so every inter-section edge flows "
        "forward and Genome's exit faces Post (issue #1080).",
    ),
    (
        "branch_fold_stability",
        TOPOLOGIES_DIR,
        "A wide side branch (Survey) shares a topo column with the spine and sits "
        "one station below its fold threshold. Adding a station inside Survey "
        "must not re-grid the downstream Report onto a backward return row: "
        "inter-section placement is a function of the DAG, not of intra-section "
        "size (issue #1082).",
    ),
    # --- Offset and bypass ---
    (
        "bypass_fan_in_outer_slot",
        TOPOLOGIES_DIR,
        "A bypass line (QC) skips hub and alignment to reach a deeper station "
        "(MultiQC Report) in the Integration section, while dna/meth/rna/atac "
        "converge at a fan-in entry port. The bypass line claims the outer slot "
        "so no empty interior gaps appear (issue #655).",
    ),
    (
        "mismatched_tracks",
        TOPOLOGIES_DIR,
        "Lines with mismatched track counts at shared stations.",
    ),
    (
        "upward_bypass",
        TOPOLOGIES_DIR,
        "Tall section bypass where the trunk is above the source (upward gap1).",
    ),
    (
        "bypass_label_rake",
        TOPOLOGIES_DIR,
        "A foreign line dips below a station to bypass its marker, then climbs "
        "back to the trunk past the wide 'Quantification' label. The router "
        "lengthens the dip's flat run so the climb seats clear of the glyphs "
        "(`_clear_bypass_v_label_strikes`).",
    ),
    (
        "bypass_label_rake_left",
        TOPOLOGIES_DIR,
        "Mirror of the bypass-label rake: the dip's descending leg, not its "
        "climb, crosses the wide 'Quantification Step' label, so its V corner "
        "lands in the label's left half and the router seats it clear of the "
        "left edge (`_clear_bypass_v_label_strikes`).",
    ),
    (
        "bypass_label_rake_wide",
        TOPOLOGIES_DIR,
        "An extra-wide bypassed-station label the router cannot seat the V's "
        "flat-run corner clear of: the strike-clearance loop pushes the bypassed "
        "node out by whole grid columns until the dip clears the glyphs, widening "
        "rather than relying on the router's partial corner-seating (issue #700).",
    ),
    (
        "bypass_v_tight",
        TOPOLOGIES_DIR,
        "An intra-section bypass V at a tight column pitch: without room for a "
        "lead-in the descent would diverge on the 'Process A' marker and rake "
        "its label. The engine pushes the bypassed node to a further grid "
        "column so the V diverges past the label and keeps a visible flat run "
        "through its X (issue #688).",
    ),
    (
        "fan_bypass_nesting",
        TOPOLOGIES_DIR,
        "A junction fans to a straight continuation, three down-turns into "
        "stacked rows, and one far-column bypass. The bypass joins the same "
        "concentric corner as the down-turns and descends in the shared "
        "channel, peeling into its lane at the inter-row gap rather than "
        "grazing the down-turn corners near the junction (issue #652).",
    ),
    (
        "divergent_fanout_split",
        TOPOLOGIES_DIR,
        "One line fans out from a single source to a near and a far target in "
        "the row below. The two descents stay fused as one trunk until the near "
        "branch turns off, so the farther branch never peels onto the inside of "
        "the nearer one and crosses it (issue #702).",
    ),
    (
        "disjoint_sameline_trunks",
        TOPOLOGIES_DIR,
        "Two lines diving into one below-row channel to bypass a section ride a "
        "tight concentric bundle until a member peels up at its turn column, "
        "rather than being split apart by a track reserved for a trunk that only "
        "appears further along the channel (issue #702).",
    ),
    (
        "dogleg_exempt_distinct",
        TOPOLOGIES_DIR,
        "A bypass line cleared off a different line's exempt wrap trunk in the "
        "inter-row gap runs parallel above it as a tight bundle, rather than "
        "doglegging onto the crossing side where its riser would pierce the "
        "wrap run twice (issue #702).",
    ),
    (
        "dogleg_exempt_sameline",
        TOPOLOGIES_DIR,
        "Two opposing flows of one line fused in the inter-row gap are pulled "
        "apart into a dogleg; the down-moved trunk stops short of the next "
        "row's section header badge, keeping the required clearance rather "
        "than crowding it (issue #698).",
    ),
    (
        "dogleg_twoline_fanout",
        TOPOLOGIES_DIR,
        "Two distinct lines leave one section through a shared exit junction to "
        "different sections in the row below. They descend as one concentric "
        "bundle and split only where the near line peels into its target while "
        "the far line continues over it, rather than diverging at the junction "
        "and crossing (issue #719).",
    ),
    (
        "merge_offrow_continuation",
        TOPOLOGIES_DIR,
        "A perpendicular feeder re-slots at a multi-feeder merge port, and the "
        "single re-joined line leaves the merge row before reaching its "
        "consumer one row up, so the bundle-offset walk stops at the off-row "
        "exit rather than carrying the slot off the row.",
    ),
    (
        "right_entry_gap_above_empty_row",
        TOPOLOGIES_DIR,
        "A right-entry feed from a source two rows above its target, where the "
        "target flows right-to-left so its flow-start consumer sits at the "
        "entry edge: the feed loops around below into the port beside the "
        "consumer rather than crossing the box and folding back (#885).",
    ),
    (
        "corridor_narrow_gap_fallback",
        TOPOLOGIES_DIR,
        "A left-entry feed crosses two rows past a wider intervening section "
        "whose inter-row gap is too narrow for the corridor's clearance band, "
        "so it falls back to the around-below loop clear of that section while "
        "the adjacent feeder takes the corridor (issue #722).",
    ),
    (
        "off_track_convergence",
        TOPOLOGIES_DIR,
        "Multiple off-track file inputs converging on a single consumer. "
        "The trunk stays horizontal while the inputs stack above the consumer column.",
    ),
    (
        "off_track_convergence_multiline",
        TOPOLOGIES_DIR,
        "A multi-line bundle enters a section and converges on a deep first "
        "station that also consumes off-track file inputs. The consumer stays "
        "on the section trunk, level with its continuation, rather than being "
        "dragged to the section floor (issue #650).",
    ),
    (
        "off_track_input_above_consumer",
        TOPOLOGIES_DIR,
        "A section whose mid-trunk station consumes an off-track input while a "
        "neighbouring station feeds an off-track output. The input hugs one row "
        "above its consumer instead of towering an extra slot up because it "
        "shares an anchor with the differently-columned output (issue #651).",
    ),
    (
        "around_section_below",
        TOPOLOGIES_DIR,
        "Cross-row route to a LEFT-entry target where the natural inter-row "
        "channel would cut through an intervening section's bbox. Exercises "
        "`_route_around_section_below` (collector-fan-in geometry).",
    ),
    (
        "inter_row_wrap_clearance",
        TOPOLOGIES_DIR,
        "A right-exit bundle wrapping down to a left-entry in the row below. "
        "The horizontal run sits centred in the inter-row gap, clear of both "
        "the section above and the next row's header.",
    ),
    # --- Routing-gate coverage fixtures ---
    (
        "junction_entry_reversed_fold",
        TOPOLOGIES_DIR,
        "A two-line bundle exits a TB section's RIGHT side, wraps into a Source "
        "section, then fans out at a junction into two same-row destinations. The "
        "bundle order is carried concentrically through the reversal corners so "
        "the lines never cross at a station, and the fan-out peels off cleanly "
        "(issue #760).",
    ),
    (
        "cross_col_top_entry",
        TOPOLOGIES_DIR,
        "A cross-column feed from a RIGHT-exit producer into a TOP-entry "
        "consumer: the entry port is placed on the section boundary rather "
        "than floating above the canvas (issue #740).",
    ),
    (
        "lr_top_entry_cross_column",
        TOPOLOGIES_DIR,
        "A TB section dropping from its BOTTOM exit into the TOP entry of an "
        "LR section whose run sits left of the drop column: the LR run is "
        "shifted right under the drop and the section bbox follows it so the "
        "trailing station stays contained (issue #1057).",
    ),
    (
        "lr_top_entry_cross_column_two_line",
        TOPOLOGIES_DIR,
        "Two lines dropping together from a TB BOTTOM exit into the TOP entry "
        "of an LR section: the in-section line order follows the arrival order "
        "so the entry corner nests concentrically and the bundle neither "
        "pinches nor crosses through the bend (issue #1061).",
    ),
    (
        "tb_column_continuation_two_lines",
        TOPOLOGIES_DIR,
        "A TB section with a two-line BOTTOM exit continuing straight down into "
        "the TB section below. The exit port seats close to the last station "
        "with normal section padding rather than the doubled gap the fold-span "
        "extension would add (issue #1062).",
    ),
    (
        "bypass_gap2_rightward_overflow",
        TOPOLOGIES_DIR,
        "A seven-line rightward bypass whose gap-2 bundle right edge overflows "
        "the inter-column gap limit and is clamped, keeping the bundle inside "
        "the gap.",
    ),
    (
        "bypass_leftward_overflow",
        TOPOLOGIES_DIR,
        "A seven-line reverse-flow (right-to-left) bypass: the trunk leads out "
        "leftward, the mirror of every other bypass. The concentric order and "
        "corner radii follow the trunk's travel direction so the bundle fans "
        "cleanly instead of twisting at the descent corner (issue #723).",
    ),
    (
        "bypass_leftward_far_side_entry",
        TOPOLOGIES_DIR,
        "A seven-line reverse-flow bypass into a far-side LEFT entry: the source "
        "exits its left edge and the target's entry port is on its own far "
        "(left) edge, so the bundle wraps around below into the port from its "
        "outward side. The half-turn transposes the bundle, so the target "
        "section's line order is reversed to match and no line crosses a mate "
        "(issue #974).",
    ),
    (
        "right_entry_wrap_no_fan",
        TOPOLOGIES_DIR,
        "A single line wrapping from an LR exit into a cross-row RL section's "
        "RIGHT entry, with no junction siblings (the solo `_route_right_entry_"
        "wrap` lead-in).",
    ),
    (
        "left_entry_up_wrap",
        TOPOLOGIES_DIR,
        "A two-line bundle wrapping up-and-left from a source below-and-right "
        "into a cross-row section's LEFT entry (the `_route_left_entry_wrap` "
        "up-riser path); the bundle order is preserved concentrically around "
        "the wrap.",
    ),
    (
        "tb_right_entry_stack",
        TOPOLOGIES_DIR,
        "A two-line bundle into a stacked TB section's RIGHT entry from a "
        "same-row left source: it loops over the section top and descends into "
        "the port, the U-turn transposing the bundle, with concentric corners "
        "built via `build_concentric_bundle` (#707).",
    ),
    (
        "tb_passthrough_trunk",
        TOPOLOGIES_DIR,
        "A three-line bundle running straight down a linear chain of stations "
        "in a `%%metro direction: TB` section. The trunk passes through each "
        "station as a clean vertical column: every line holds one offset, so "
        "no station reads as an elbow.",
    ),
    (
        "tb_bottom_entry_flow_start",
        TOPOLOGIES_DIR,
        "A `%%metro direction: TB` section given `%%metro entry: bottom` whose "
        "consumer is the flow-start (top) station. The bottom entry is "
        "re-anchored to the top so the line enters beside its consumer and "
        "flows down, rather than running up through MultiQC to reach Collect "
        "and folding back (#885).",
    ),
    (
        "tb_lr_exit_left",
        TOPOLOGIES_DIR,
        "A `%%metro direction: TB` section dropping in through its TOP entry and "
        "leaving through a LEFT exit into a section below-left (the "
        "`_route_tb_lr_exit` LEFT arm): the station drops, turns once, and runs "
        "out of the box's left side, the vertical leg fanned by the reversed "
        "station offset so the outermost line takes the widest arc (#917).",
    ),
    (
        "tb_lr_exit_right",
        TOPOLOGIES_DIR,
        "A `%%metro direction: TB` section dropping in through its TOP entry and "
        "leaving through a RIGHT exit into the next forward section (the "
        "`_route_tb_lr_exit` RIGHT arm): the mirror of the LEFT exit, fanned by "
        "the exit port's own offset (#917).",
    ),
    (
        "tb_internal_diagonal",
        TOPOLOGIES_DIR,
        "A symmetric fan-out inside a `%%metro direction: TB` section: the hub "
        "centres over its two branch stations, which sit on X tracks either "
        "side of it, so both internal edges route as 45-degree diagonals (the "
        "`_route_tb_internal` diagonal arm) (#917).",
    ),
    (
        "tb_trunk_through_fan",
        TOPOLOGIES_DIR,
        "An asymmetric fan-out inside a `%%metro direction: TB` section: one "
        "line continues straight down the trunk column to its child while a "
        "sibling peels off to another column.  The continuation is slotted onto "
        "the trunk so it drops straight instead of jogging one offset step "
        "(#929).",
    ),
    (
        "around_below_ep_col_gt0",
        TOPOLOGIES_DIR,
        "A two-line bundle looping around below the canvas into a non-zero-"
        "column LEFT-entry target, past an intervening middle-row section that "
        "blocks the direct wrap.",
    ),
    # --- Section-boundary routing discipline ---
    (
        "stacked_left_exit_drop",
        TOPOLOGIES_DIR,
        "A LEFT exit feeding a LEFT entry stacked directly below it: the "
        "connector leads out into a clean channel left of the column and drops, "
        "rather than dropping straight down the shared edge through the source "
        "box.",
    ),
    (
        "right_entry_from_above",
        TOPOLOGIES_DIR,
        "A RIGHT entry whose consumer is the section's flow-start station: the "
        "section flows right-to-left so the consumer sits at the entry edge and "
        "the feed enters beside it, rather than running across the box to the "
        "far station and folding back (#885).  Fed from a higher row past the "
        "target's right edge, the line drops straight down its outward side to "
        "the entry Y rather than looping below the box (#889).",
    ),
    (
        "right_entry_from_above_far",
        TOPOLOGIES_DIR,
        "A RIGHT entry fed from two rows up, past the target's right edge: the "
        "line drops straight down its outward side across the empty intervening "
        "row to the entry Y and turns in, rather than diving below the box and "
        "climbing back up (#889).",
    ),
    (
        "merge_leftmost_sink_branch",
        TOPOLOGIES_DIR,
        "A leftward merge whose trunk reaches a leftmost-column sink's LEFT "
        "entry: the trunk wraps to rise on the box's far (left) side, with the "
        "branch feeders converging on its shared channel, rather than crossing "
        "the sink interior to the far-side port.",
    ),
    (
        "merge_around_below_leftmost",
        TOPOLOGIES_DIR,
        "Two sources merging into a leftmost-column LEFT entry: the trunk routes "
        "around the target's left side to enter from outside while the second "
        "merge target is reached in-row.",
    ),
    # --- Complex auto-layout regression isolation ---
    (
        "route_around_intervening",
        TOPOLOGIES_DIR,
        "A line skips a middle section: it routes around the intervening "
        "section's bbox rather than slicing through it.",
    ),
    (
        "self_crossing_bridge",
        TOPOLOGIES_DIR,
        "One colour whose vertical bus crosses its own independent horizontal "
        "connector earns a bridge gap (same-colour crossover, not a fan).",
    ),
    (
        "convergence_stacked_sink",
        TOPOLOGIES_DIR,
        "A leaf sink that would otherwise sit alone in the spine band migrates "
        "into the convergence return row (no grid-cell collision).",
    ),
    (
        "cross_row_gap_wrap",
        TOPOLOGIES_DIR,
        "A cross-row feed runs its horizontal in the inter-row gap and drops "
        "straight in, rather than diving under the return row counter to its "
        "flow.",
    ),
    (
        "rl_entry_runway",
        TOPOLOGIES_DIR,
        "A source section feeding a left-hand target via `%%metro exit: left`: "
        "it flows right-to-left so its producers sit at the left exit edge and "
        "the runway leaves beside them, rather than the producers exiting left "
        "back through the start station (#885).",
    ),
    (
        "terminus_join",
        TOPOLOGIES_DIR,
        "Two lines converge on a single file-icon terminus in a sectionless "
        "flat graph, so the join lands directly on the terminus rather than a "
        "synthesised convergence junction.",
    ),
    (
        "compact_hidden_passthrough",
        TOPOLOGIES_DIR,
        "Compact mode keeps a hidden single-line pass-through station on its "
        "bundle slot so the two lines weave consistently through the section.",
    ),
    (
        "compact_gap_peer_conflict",
        TOPOLOGIES_DIR,
        "A fork-join whose hub carries non-consecutive offset slots safely "
        "abandons gap-compaction when a visible same-layer peer carries the "
        "intervening line, rather than cascading the reorder.",
    ),
    (
        "merge_port_above_approach",
        TOPOLOGIES_DIR,
        "A line descending into a multi-feeder merge port from a section above "
        "keeps the above-trunk slot all the way to the output, so its riser "
        "joins the bundle without crossing the trunk on the outgoing run "
        "(issue #704).",
    ),
    (
        "junction_entry_collision",
        TOPOLOGIES_DIR,
        "A three-line fan-out where one line continues straight to its own "
        "destination while the other two branch away: the straight line keeps "
        "a constant bundle slot across the source exit so its trunk stays "
        "horizontal (issue #704).",
    ),
    (
        "junction_entry_align",
        TOPOLOGIES_DIR,
        "A two-line bundle whose order is preserved across the "
        "junction-to-entry-port boundary, so the straight-through line stays "
        "horizontal instead of slanting to swap slots (issue #704).",
    ),
    (
        "merge_trunk_out_of_range_section",
        TOPOLOGIES_DIR,
        "Two same-row sources merge into one sink past an intervening section "
        "while another row's section sits outside the merge column range, so "
        "the merge trunk keeps its same-row bypass channel rather than crossing "
        "below the out-of-range section.",
    ),
    (
        "merge_trunk_over_low_section",
        TOPOLOGIES_DIR,
        "A same-row merge trunk bypasses past a tall intervening section while "
        "a lower-row section sits within the merge column range. The inter-row "
        "gap clears the lower section's header, so the trunk (and its branches) "
        "route through that gap rather than diving below the whole canvas.",
    ),
    (
        "merge_bottom_row_bypass",
        TOPOLOGIES_DIR,
        "A merge whose entry sits in the bottommost grid row: the trunk's "
        "inter-row bypass routes in the cramped gap above that row. Placement "
        "reserves the gap so the channel clears the upper row's section boxes "
        "instead of grazing them.",
    ),
    (
        "merge_pullaway",
        TOPOLOGIES_DIR,
        "One line converges on a merge from two stacked rows of the same "
        "column; the cross-row feeder drops onto the trunk's pull-away bypass "
        "channel and the two travel as a single stroke into the entry.",
    ),
    (
        "merge_right_entry",
        TOPOLOGIES_DIR,
        "One line converges on a RIGHT entry whose consumer is the sink's "
        "flow-start station: the sink flows right-to-left so the merge arrives "
        "beside its consumer, and the trunk loops under the sink onto that "
        "channel rather than slicing across the box to the far station (#885).",
    ),
    (
        "peeloff_riser_respace",
        TOPOLOGIES_DIR,
        "Four lines from two sources ride one shared bypass trunk and rise "
        "into a common destination entry port, where the trunk-Y order and "
        "the entry-port slot order disagree. Each source bundle keeps its "
        "declaration order at the peel-off corner instead of inverting "
        "(issue #695).",
    ),
    (
        "peeloff_extra_line_consumer",
        TOPOLOGIES_DIR,
        "Same peel-off topology as peeloff_riser_respace but the destination "
        "section also carries an extra internal branch (l5). The riser reorder "
        "must still fire and keep the bundle crossing-free at the shared entry "
        "port regardless of extra lines in the consumer section (issue #751).",
    ),
    # --- LR section feeding a TB section's TOP entry ---
    (
        "lr_to_tb_top_drop",
        TOPOLOGIES_DIR,
        "An LR section feeds the TOP entry of a TB section stacked directly "
        "below. With no explicit exit side the engine infers a BOTTOM exit: "
        "the line curves out of the trunk after the last station and drops "
        "straight onto the target trunk, which is aligned under the exit.",
    ),
    (
        "lr_to_tb_top_drop_two_lines",
        TOPOLOGIES_DIR,
        "Two co-travelling lines drop out of an LR section's explicit BOTTOM "
        "exit into a TB section's shared TOP entry below, staying parallel "
        "through the corner and down to the trunk without crossing.",
    ),
    (
        "lr_to_tb_top_near_vertical",
        TOPOLOGIES_DIR,
        "A RIGHT-exit LR section feeds the TOP entry of a TB section stacked "
        "directly below. The explicit right exit leaves on the right, clears "
        "the source box, and doubles back over the inter-row gap to drop "
        "straight onto the target trunk rather than elbowing in through the "
        "top-right corner.",
    ),
    (
        "lr_to_tb_top_cross_col",
        TOPOLOGIES_DIR,
        "A junction source feeds both a same-row RIGHT-entry consumer and a "
        "TB section's TOP entry two rows below. The downward branch drops onto "
        "the target trunk without crossing the section boundary off-port.",
    ),
    (
        "lr_to_tb_top_two_lines",
        TOPOLOGIES_DIR,
        "Two co-travelling lines from a RIGHT-exit LR section double back into "
        "a TB section's shared TOP entry below, landing on their trunk X "
        "offsets so the bundle stays parallel through the boundary without "
        "pinching or crossing.",
    ),
    # --- Section header relocated clear of a top-entry drop ---
    (
        "top_entry_header_clash",
        TOPOLOGIES_DIR,
        "A TB section's title is long enough to reach under the trunk that drops "
        "into its TOP entry. Rather than route the line around the title, the "
        "header relocates below the box so the drop enters cleanly.",
    ),
    (
        "header_side_rotated",
        TOPOLOGIES_DIR,
        "A TB section whose trunk drops through the top edge and exits the bottom "
        "edge blocks the header on both horizontal edges. The title rotates and "
        "runs down the clear left edge instead of crossing the line.",
    ),
    (
        "header_nudge",
        TOPOLOGIES_DIR,
        "A title too long to fit a rotated side, on a section blocked top and "
        "bottom by its trunk: the header shifts right past the trunk as a last "
        "resort, the canvas growing to keep it visible.",
    ),
    # --- Multi-line perpendicular exit that does not drop straight down ---
    (
        "lr_perp_top_exit_side_entry",
        TOPOLOGIES_DIR,
        "Two co-travelling lines leave an LR section through an explicit TOP "
        "exit and feed the LEFT entry of a same-row neighbour. The exit port "
        "sits past the last station, and the bundle rises into the header "
        "band, runs across, and descends to the consumer's row to enter "
        "straight, staying parallel through every concentric corner.",
    ),
    (
        "lr_perp_bottom_exit_side_entry",
        TOPOLOGIES_DIR,
        "The BOTTOM-exit mirror of lr_perp_top_exit_side_entry: the bundle "
        "drops below the source section, runs across the under-row band, and "
        "rises back to the consumer's row to enter straight.",
    ),
    (
        "lr_perp_top_exit_perp_entry",
        TOPOLOGIES_DIR,
        "Two co-travelling lines leave an LR section through a TOP exit and "
        "feed the TOP entry of a same-row neighbour in another column. The "
        "bundle rises over the header band and drops into the consumer trunk, "
        "keeping a single left/right order across the shared entry port so it "
        "stays parallel without crossing at the drop.",
    ),
    (
        "lr_perp_bottom_exit_perp_entry",
        TOPOLOGIES_DIR,
        "The BOTTOM-exit mirror of lr_perp_top_exit_perp_entry: the bundle "
        "drops under the row, runs across, and rises into the consumer's "
        "BOTTOM entry, staying parallel through the corridor.",
    ),
    (
        "lr_perp_top_exit_perp_entry_diverging",
        TOPOLOGIES_DIR,
        "A TOP-exit bundle taken over the corridor into a TOP entry where the "
        "two lines split to different downstream stations. Consistent corridor "
        "ordering routes each line to its target without a convergence jog at "
        "the entry.",
    ),
    (
        "cross_column_perp_drop",
        TOPOLOGIES_DIR,
        "A `%%metro direction: TB` section fed by a perpendicular drop from a "
        "section in a different grid column. The vertical trunk stays on the "
        "QC section's own column and the cross-column feed comes over the top "
        "and drops into the trunk head, rather than the trunk being dragged "
        "out toward the off-column source.",
    ),
    (
        "cross_column_perp_drop_far_exit",
        TOPOLOGIES_DIR,
        "The cross-column perpendicular drop where the source's exit side faces "
        "away from the target's entry side (a BOTTOM exit feeding a TOP entry). "
        "The lead-in crosses to the inter-column gap and reaches the TOP entry "
        "from above the box, rather than rising up the trunk through the "
        "section's stations.",
    ),
    (
        "rail_inter_section",
        TOPOLOGIES_DIR,
        "Two `%%metro line_spread: rails` sections joined by an inter-section "
        "edge. The connector wraps cleanly from the upstream right exit port, "
        "down the right margin, across the inter-section gap, and down the left "
        "margin into the downstream left entry port - no dangling port stubs "
        "and no avoidable crossings between co-travelling lines.",
    ),
    (
        "rail_offtrack_io",
        TOPOLOGIES_DIR,
        "A `%%metro line_spread: rails` section with off-track `%%metro file:` "
        "input and output. Each off-track file terminus carries a buffer-stop "
        "nub at the rail-side end of its vertical stub (like the on-rail "
        "CRAM/VCF termini) seated clear of its under-icon caption, rather than "
        "the line ending bare at the icon.",
    ),
    (
        "rail_offtrack_plain_io",
        TOPOLOGIES_DIR,
        "A `%%metro line_spread: rails` section with plain (non-file) off-track "
        "input and output. Each plain off-track node renders a station marker at "
        "its line end rather than a bare stub, and the input's label sits above "
        "the node clear of its drop and the adjacent station's label.",
    ),
    (
        "bottom_row_climb_clear_corridor",
        TOPOLOGIES_DIR,
        "A section in the bottommost grid row sends a line up and across to a "
        "higher-row target several columns away. The columns it spans hold no "
        "same-row section, so the line runs along its own row level and climbs "
        "at the end rather than diving below the source row to the canvas floor "
        "and looping back up.",
    ),
    (
        "exit_corner_offset_dogleg",
        TOPOLOGIES_DIR,
        "A passing line runs through a section on a per-line bundle offset, then "
        "exits and bypasses a higher row to climb to a far target. The onward "
        "run keeps the line's offset over the row-level traverse so it leaves "
        "the exit port straight, with the single level change a clean riser at "
        "the far gap rather than a one-offset-step jog at the exit corner.",
    ),
    (
        "multicarrier_offrow_exit_climb",
        TOPOLOGIES_DIR,
        "A section's exit port carries two lines from two stations that share a "
        "trunk row off the port row, then fans out through a junction to "
        "several rows. The parallel bundle anchors on the shared carrier row so "
        "it runs flat inside the section and the fan-out risers fall in the "
        "inter-section gap, rather than both lines climbing a diagonal up to "
        "the port inside the section.",
    ),
    (
        "post_convergence_trunk",
        TOPOLOGIES_DIR,
        "Two stacked inputs converge on a station inside one LR section. The "
        "merge station's single linear successor continues flat on the merge "
        "row rather than dropping back onto one of the incoming branch rows, so "
        "the post-merge trunk runs straight instead of zigzagging.",
    ),
    (
        "bundle_terminator_continuation",
        TOPOLOGIES_DIR,
        "A two-line bundle enters a station where one line terminates while the "
        "other continues to a single successor. The successor holds the trunk "
        "row rather than dropping to its own line base, so the chain runs flat "
        "instead of dipping into a V-kink before the section exit.",
    ),
    (
        "clear_channel_target_aware_push",
        TOPOLOGIES_DIR,
        "A hub fans one line down into a wide section stacked directly below and "
        "another down-and-right to a target. The fan descent for the rightward "
        "line grazes the wide block, so the graze correction pushes it toward "
        "the target's side of the block rather than the nearer edge, keeping the "
        "line heading to its target instead of detouring across the canvas.",
    ),
    (
        "junction_fanout_convergence",
        TOPOLOGIES_DIR,
        "Three lines converge into one joint-calling entry port: two bypass the "
        "intervening sections and climb risers into the port while the third "
        "joins flat from the adjacent column. The flat shallow feeder takes the "
        "port-near slot on top of the climbing pair so the bundle turns into the "
        "port concentrically, instead of the flat line weaving across the risers "
        "at the corner.",
    ),
    (
        "convergent_offrow_exit_climb",
        TOPOLOGIES_DIR,
        "A single-row long-read variant-calling map. The annotation section "
        "carries only snv and sv (the two highest-priority lines), reached through "
        "a bypass whose source re-based them onto low slots. Its two-line bundle "
        "anchors on its own trunk rather than inheriting the high global slots, so "
        "its markers sit on their rows and the run into reports stays level.",
    ),
    (
        "near_vertical_junction_hook",
        TOPOLOGIES_DIR,
        "A fan-out junction overhanging a same-column RIGHT entry one row below. "
        "The two-line bundle drops straight down the junction's own column and "
        "turns once into the port, rather than leading out to a centred gap "
        "channel and hooking back: a hook's opposite-handed corners cannot nest "
        "a multi-line bundle. The destination section carries the matching line "
        "order so the bundle arrives in the order the section lays it out (#1018).",
    ),
    (
        "tb_perp_exit_side_neighbour",
        TOPOLOGIES_DIR,
        "A vertical-flow section's BOTTOM exit feeds a side LR neighbour sharing "
        "the exit's Y. The connector leaves the port down into the inter-row "
        "corridor clear of the box, runs across, then turns up into the entry, "
        "rather than running straight along the section's bottom edge and out "
        "through the corner (#1052).",
    ),
    (
        "tb_two_line_vert_seam",
        TOPOLOGIES_DIR,
        "A vertical-flow section's RIGHT exit feeds another vertical-flow "
        "section's LEFT entry with a two-line bundle. The entry sits a station "
        "gap above the trunk head so both lines enter horizontally then drop "
        "straight onto their trunk lanes, rather than the staggered line "
        "slanting into the trunk for want of drop room (#1054).",
    ),
    (
        "aligner_row_pinned_continuation",
        TOPOLOGIES_DIR,
        "Three sibling aligners feed one dedup hub that lands on the lead "
        "aligner's row, while one aligner's line continues on a track pinned "
        "to the section bottom by hidden continuation stations. The aligners "
        "stack on consecutive grid rows instead of the low-line aligner being "
        "dragged down to crowd its neighbour and strand the middle row (#1071).",
    ),
]

# Category headers inserted before specific entries
CATEGORY_HEADERS: dict[str, str] = {
    "simple_pipeline": "Main Examples",
    "single_section": "Simple Topologies",
    "wide_fan_out": "Fan-out and Fan-in",
    "asymmetric_tree": "Branching and Multipath",
    "multi_line_bundle": "Multi-line Bundles",
    "rnaseq_lite": "Realistic Pipelines",
    "fold_fan_across": "Fold Topologies",
}


# Ordered list of nf-core pipeline examples.
# Each entry is (filename_stem, display_name, repo_url, description).
PIPELINE_ENTRIES: list[tuple[str, str, str, str]] = [
    (
        "rnaseq_auto",
        "nf-core/rnaseq",
        "https://github.com/nf-core/rnaseq",
        "RNA-seq analysis with multiple aligner and quantification routes "
        "(STAR/RSEM, STAR/Salmon, HISAT2, Salmon pseudo-alignment, Kallisto).",
    ),
    (
        "sarek_metro",
        "nf-core/sarek",
        "https://github.com/nf-core/sarek",
        "Germline and somatic variant calling, covering germline, tumor-only, "
        "and tumor-normal paired analysis through SNP/indel, SV/CNV, and MSI "
        "callers with downstream variant annotation.",
    ),
    (
        "epitopeprediction",
        "nf-core/epitopeprediction",
        "https://github.com/nf-core/epitopeprediction",
        "MHC binding prediction from VCF, protein FASTA, or peptide TSV inputs "
        "through five prediction tools.",
    ),
    (
        "hlatyping",
        "nf-core/hlatyping",
        "https://github.com/nf-core/hlatyping",
        "HLA typing from FASTQ or BAM inputs via OptiType and HLA-HD.",
    ),
    (
        "variantprioritization",
        "nf-core/variantprioritization",
        "https://github.com/nf-core/variantprioritization",
        "Somatic and germline variant prioritization using PCGR and CPSR.",
    ),
    (
        "variantbenchmarking",
        "nf-core/variantbenchmarking",
        "https://github.com/nf-core/variantbenchmarking",
        "Benchmarking of variant callers against truth sets with "
        "Truvari, hap.py, RTGtools, and more.",
    ),
    (
        "genomeassembly",
        "sanger-tol/genomeassembly",
        "https://github.com/sanger-tol/genomeassembly",
        "Genome assembly from long reads and Hi-C data through "
        "purging, polishing, scaffolding, and QC.",
    ),
]

# Manifest mapping SVG filename -> section for the render diff page.
# Populated by each render function, written to RENDERS_DIR/manifest.json.
_manifest: dict[str, str] = {}

# Layout-quality scorecard per SVG filename, written to RENDERS_DIR/metrics.json
# and reported as per-render deltas in the render-diff page. Advisory only.
_metrics: dict[str, dict[str, float]] = {}

_SVG_DIMS_RE = re.compile(r'<svg[^>]*\bwidth="([\d.]+)"[^>]*\bheight="([\d.]+)"')


def render_drawn_svg(graph, theme, **kwargs) -> str:
    """Render the drawn map only, with the embedded data manifest disabled.

    The gallery is the visual-regression surface: the render diff compares
    these SVGs byte-for-byte, and the data manifest carries no visual content.
    Disabling it keeps the diff a true picture of what changed on screen.
    """
    graph.embed_manifest = False
    return render_svg(graph, theme, **kwargs)


def _record_metrics(graph, svg_name: str, svg_str: str) -> None:
    """Compute the layout-quality scorecard for a freshly rendered graph.

    Computed alongside the render so the scores reflect the same engine version
    that drew the SVG. A failure here never aborts a render.
    """
    match = _SVG_DIMS_RE.search(svg_str)
    canvas = (float(match.group(1)), float(match.group(2))) if match else None
    try:
        _metrics[svg_name] = compute_metrics(graph, canvas=canvas)
    except Exception as e:  # noqa: BLE001 - metrics are advisory, never fatal
        print(f"    metrics FAIL for {svg_name}: {e}")


def render_mmd(
    mmd_path: Path,
    svg_path: Path,
    *,
    debug: bool = DEBUG_RENDERS,
    self_color_scheme: bool = True,
) -> str:
    """Parse, layout, and render a .mmd file to SVG; write it and return it.

    ``self_color_scheme`` is forwarded to the renderer: pages that inline the SVG
    (gallery, pipelines) pass False so the map inherits the page's color-scheme
    and follows the light/dark toggle.
    """
    text = mmd_path.read_text()
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    theme_name = graph.style if graph.style in THEMES else "nfcore"
    theme = THEMES[theme_name]
    svg_str = render_drawn_svg(
        graph, theme, debug=debug, self_color_scheme=self_color_scheme
    )
    svg_path.write_text(svg_str)
    _record_metrics(graph, svg_path.name, svg_str)
    return svg_str


def clean_name(stem: str) -> str:
    """Convert filename stem to a display-friendly heading."""
    return stem.replace("_", " ").title()


def _raw_import(prefix: str, ident_key: str, alias: str, path: str) -> tuple[str, str]:
    """Return ``(identifier, import_statement)`` for a Vite ``?raw`` MDX import.

    The identifier slugifies *ident_key* into a valid JS name; the statement
    pulls *path* in as a raw string through the *alias* import alias.
    """
    identifier = prefix + re.sub(r"\W", "_", ident_key)
    return identifier, f'import {identifier} from "@{alias}/{path}?raw";'


def mmd_import(stem: str, source_dir: Path) -> tuple[str, str, str]:
    """Describe how to embed a committed ``.mmd`` as imported source in an ``.mdx``
    page. Returns ``(identifier, import_statement, source_label)``.

    The generated pages render the example with Starlight's ``<Code>`` component
    fed by a Vite ``?raw`` import (via the ``@examples`` alias) rather than pasting
    the source inline. That keeps the page in lockstep with ``examples/`` and stops
    the generated Markdown from carrying a second copy of every fixture.
    """
    if source_dir == TOPOLOGIES_DIR:
        rel = f"topologies/{stem}"
        prefix = "topo_"
    else:
        rel = stem
        prefix = "mmd_"
    identifier, import_statement = _raw_import(prefix, stem, "examples", f"{rel}.mmd")
    source_label = f"examples/{rel}.mmd"
    return identifier, import_statement, source_label


def svg_import(svg_stem: str) -> tuple[str, str]:
    """Describe how to inline a rendered SVG into an ``.mdx`` page.

    Returns ``(identifier, import_statement)``. The SVG is pulled in as a raw
    string (via the ``@renders`` alias) and injected with ``set:html`` so the
    real SVG markup lands in the page DOM. Inlining (rather than ``<img>``) is
    what lets the map's ``light-dark()`` chrome inherit the page's color-scheme
    and follow the light/dark toggle - an ``<img>``-referenced SVG ignores the
    embedding page's scheme in WebKit.
    """
    return _raw_import("svg_", svg_stem, "renders", f"{svg_stem}.svg")


def _inline_svg(identifier: str) -> list[str]:
    """MDX lines that inline an imported raw SVG string into the page DOM.

    The XML prolog is stripped (it renders as a stray bogus comment in an HTML
    body), and blank lines around the element keep MDX parsing it as a child
    expression rather than literal text.
    """
    expr = f'{identifier}.replace(/^<\\?xml.*?\\?>\\s*/, "")'
    return ["", f"<Fragment set:html={{{expr}}} />", ""]


def _details_code(identifier: str, source_label: str) -> list[str]:
    """Markdown lines for a collapsed <details> wrapping an imported <Code> block.

    Blank lines around the component are required so MDX parses it as a child
    expression of the <details> element rather than literal text.
    """
    return [
        "<details>",
        "<summary>Mermaid source</summary>",
        "",
        f'<Code code={{{identifier}}} lang="metro" title="{source_label}" />',
        "",
        "</details>\n",
    ]


def mdx_page(title: str, imports: list[str], body: list[str]) -> list[str]:
    """Assemble an .mdx page: frontmatter, the <Code> import + per-example raw
    imports (deduped, order-preserved), then the body lines."""
    return [
        "---",
        f"title: {title}",
        "---",
        "",
        'import { Code } from "@astrojs/starlight/components";',
        *dict.fromkeys(imports),
        "",
        *body,
    ]


def render_guide_examples() -> None:
    """Render all guide examples to docs/assets/renders/."""
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)
    section = "Guide Examples"
    print("Guide examples:")
    for mmd_path in sorted(GUIDE_DIR.glob("*.mmd")):
        svg_path = RENDERS_DIR / f"{mmd_path.stem}.svg"
        try:
            render_mmd(mmd_path, svg_path)
            _manifest[svg_path.name] = section
            print(f"  {mmd_path.stem}: OK")
        except Exception as e:
            print(f"  {mmd_path.stem}: FAIL - {e}")

    # Top-level examples referenced directly from the guide
    for stem in (
        "rnaseq_auto",
        "variantbenchmarking",
        "variantbenchmarking_auto",
        "marker_styles",
    ):
        mmd_path = EXAMPLES_DIR / f"{stem}.mmd"
        if not mmd_path.exists():
            continue
        svg_path = RENDERS_DIR / f"{stem}.svg"
        try:
            render_mmd(mmd_path, svg_path)
            _manifest[svg_path.name] = section
            print(f"  {stem}: OK")
        except Exception as e:
            print(f"  {stem}: FAIL - {e}")

    # Debug overlay render for the guide
    debug_src = EXAMPLES_DIR / "rnaseq_auto.mmd"
    debug_svg = RENDERS_DIR / "rnaseq_auto_debug.svg"
    if debug_src.exists():
        try:
            text = debug_src.read_text()
            graph = parse_metro_mermaid(text)
            compute_layout(graph)
            theme_name = graph.style if graph.style in THEMES else "nfcore"
            theme = THEMES[theme_name]
            svg_str = render_drawn_svg(graph, theme, debug=True)
            debug_svg.write_text(svg_str)
            _record_metrics(graph, debug_svg.name, svg_str)
            _manifest[debug_svg.name] = section
            print("  rnaseq_auto_debug: OK")
        except Exception as e:
            print(f"  rnaseq_auto_debug: FAIL - {e}")

    print()


def build_gallery() -> None:
    """Generate docs/gallery/index.mdx and docs/assets/renders/*.svg."""
    GALLERY_DIR.mkdir(parents=True, exist_ok=True)
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)

    # Collected `import … from "@examples/…?raw"` statements, one per example,
    # spliced in below the frontmatter once the body is built.
    imports: list[str] = []
    lines: list[str] = [
        "Rendered examples covering a range of layout patterns. "
        "Click any heading in the right-hand table of contents to jump to an example.",
        "",
    ]

    current_category = "Gallery"
    for stem, source_dir, description in GALLERY_ENTRIES:
        mmd_path = source_dir / f"{stem}.mmd"
        svg_path = RENDERS_DIR / f"{stem}.svg"

        if not mmd_path.exists():
            print(f"  WARNING: {mmd_path} not found, skipping")
            continue

        # Category header
        if stem in CATEGORY_HEADERS:
            current_category = CATEGORY_HEADERS[stem]
            lines.append("---\n")
            lines.append(f"## {current_category}\n")

        # Render SVG
        try:
            render_mmd(mmd_path, svg_path, self_color_scheme=False)
            status = "OK"
        except Exception as e:
            status = f"FAIL: {e}"
            print(f"  {stem}: {status}")
            continue

        _manifest[svg_path.name] = current_category
        print(f"  {stem}: {status}")

        heading = clean_name(stem)
        identifier, import_statement, source_label = mmd_import(stem, source_dir)
        imports.append(import_statement)
        svg_id, svg_import_stmt = svg_import(stem)
        imports.append(svg_import_stmt)

        lines.append(f"### {heading}\n")
        lines.append(f"{description}\n")
        lines.append("**CLI command:**\n")
        lines.append(f"```bash\nnf-metro render {source_label} -o {stem}.svg\n```\n")
        lines.extend(_details_code(identifier, source_label))
        lines.append("**Rendered output:**\n")
        lines.extend(_inline_svg(svg_id))

    gallery_md = "\n".join(mdx_page("Gallery", imports, lines))
    gallery_path = GALLERY_DIR / "index.mdx"
    gallery_path.write_text(gallery_md)
    print(f"\nGallery written to {gallery_path}")
    print(f"SVG renders in {RENDERS_DIR}")


def render_nextflow_examples() -> None:
    """Render Nextflow DAG fixtures and hand-tuned example to docs/assets/renders/."""
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)
    section = "Nextflow Conversions"
    print("Nextflow examples:")

    # Auto-converted renders from Nextflow DAG fixtures
    for mmd_path in sorted(NEXTFLOW_FIXTURES_DIR.glob("*.mmd")):
        svg_path = RENDERS_DIR / f"nf_{mmd_path.stem}.svg"
        try:
            text = mmd_path.read_text()
            converted = convert_nextflow_dag(text)
            graph = parse_metro_mermaid(converted)
            compute_layout(graph)
            theme = THEMES[graph.style if graph.style in THEMES else "nfcore"]
            svg_str = render_drawn_svg(graph, theme, debug=DEBUG_RENDERS)
            svg_path.write_text(svg_str)
            _record_metrics(graph, svg_path.name, svg_str)
            _manifest[svg_path.name] = section
            print(f"  nf_{mmd_path.stem}: OK")
        except Exception as e:
            print(f"  nf_{mmd_path.stem}: FAIL - {e}")

    # Hand-tuned variant calling example (without file icons)
    tuned_path = EXAMPLES_DIR / "variant_calling.mmd"
    if tuned_path.exists():
        svg_path = RENDERS_DIR / "nf_variant_calling_tuned.svg"
        try:
            render_mmd(tuned_path, svg_path)
            _manifest[svg_path.name] = section
            print("  nf_variant_calling_tuned: OK")
        except Exception as e:
            print(f"  nf_variant_calling_tuned: FAIL - {e}")

    # Hand-tuned variant calling with file icons
    tuned_icons_path = EXAMPLES_DIR / "variant_calling_tuned.mmd"
    if tuned_icons_path.exists():
        svg_path = RENDERS_DIR / "nf_variant_calling_tuned_icons.svg"
        try:
            render_mmd(tuned_icons_path, svg_path)
            _manifest[svg_path.name] = section
            print("  nf_variant_calling_tuned_icons: OK")
        except Exception as e:
            print(f"  nf_variant_calling_tuned_icons: FAIL - {e}")

    print()


def build_pipelines_page() -> None:
    """Generate docs/pipelines/index.mdx and render pipeline SVGs."""
    PIPELINES_DIR.mkdir(parents=True, exist_ok=True)
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)
    section = "nf-core Pipelines"
    print("nf-core pipelines:")

    imports: list[str] = []
    lines: list[str] = [
        "Real-world pipelines rendered with nf-metro. These are maintained as "
        "`.mmd` files alongside the pipeline source code and rendered automatically.",
        "",
        f"See the [Gallery]({SITE_BASE}gallery/) for layout pattern examples and the "
        f"[Guide]({SITE_BASE}guide/) for how to write your own.",
        "",
    ]

    for stem, display_name, repo_url, description in PIPELINE_ENTRIES:
        mmd_path = EXAMPLES_DIR / f"{stem}.mmd"
        svg_path = RENDERS_DIR / f"pipeline_{stem}.svg"

        if not mmd_path.exists():
            print(f"  WARNING: {mmd_path} not found, skipping")
            continue

        try:
            render_mmd(mmd_path, svg_path, debug=True, self_color_scheme=False)
            status = "OK"
        except Exception as e:
            status = f"FAIL: {e}"
            print(f"  {stem}: {status}")
            continue

        _manifest[svg_path.name] = section
        print(f"  {stem}: {status}")

        identifier, import_statement, source_label = mmd_import(stem, EXAMPLES_DIR)
        imports.append(import_statement)
        svg_id, svg_import_stmt = svg_import(f"pipeline_{stem}")
        imports.append(svg_import_stmt)

        lines.append(f"## [{display_name}]({repo_url})\n")
        lines.append(f"{description}\n")
        lines.extend(_inline_svg(svg_id))
        lines.extend(_details_code(identifier, source_label))

    pipelines_md = "\n".join(mdx_page("nf-core pipelines", imports, lines))
    pipelines_path = PIPELINES_DIR / "index.mdx"
    pipelines_path.write_text(pipelines_md)
    print(f"\nPipelines page written to {pipelines_path}")
    print()

    # Also render rnaseq_sections_manual for the guide (not on pipelines page)
    for stem in ("rnaseq_sections_manual",):
        mmd_path = EXAMPLES_DIR / f"{stem}.mmd"
        if not mmd_path.exists():
            continue
        svg_path = RENDERS_DIR / f"{stem}.svg"
        try:
            render_mmd(mmd_path, svg_path)
            _manifest[svg_path.name] = "Guide Examples"
            print(f"  {stem}: OK")
        except Exception as e:
            print(f"  {stem}: FAIL - {e}")


def render_test_fixtures() -> None:
    """Render test-only fixtures not duplicated in examples/."""
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)
    section = "Test Fixtures"
    print("Test fixtures:")
    for stem in ("multiline_labels", "rnaseq_simple", "genomeassembly_organellar"):
        mmd_path = TEST_FIXTURES_DIR / f"{stem}.mmd"
        if not mmd_path.exists():
            continue
        svg_path = RENDERS_DIR / f"{stem}.svg"
        try:
            render_mmd(mmd_path, svg_path)
            _manifest[svg_path.name] = section
            print(f"  {stem}: OK")
        except Exception as e:
            print(f"  {stem}: FAIL - {e}")
    print()


def write_manifest() -> None:
    """Write render manifest mapping SVG filenames to sections."""
    manifest_path = RENDERS_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest, indent=2, sort_keys=True) + "\n")
    print(f"Manifest written to {manifest_path} ({len(_manifest)} entries)")


def write_metrics() -> None:
    """Write the per-render layout-quality scorecard for the render-diff page."""
    metrics_path = RENDERS_DIR / "metrics.json"
    metrics_path.write_text(json.dumps(_metrics, indent=2, sort_keys=True) + "\n")
    print(f"Metrics written to {metrics_path} ({len(_metrics)} entries)")


if __name__ == "__main__":
    # Clean stale renders so removed gallery entries don't persist
    if RENDERS_DIR.exists():
        for old_svg in RENDERS_DIR.glob("*.svg"):
            old_svg.unlink()
    render_guide_examples()
    render_nextflow_examples()
    build_pipelines_page()
    render_test_fixtures()
    build_gallery()
    write_manifest()
    write_metrics()
