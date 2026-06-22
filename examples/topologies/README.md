# Topology Examples

Example `.mmd` files demonstrating a range of pipeline topologies and the layout patterns they produce. Each example exercises different aspects of the auto-layout engine.

To render all examples:

```bash
nf-metro render examples/topologies/wide_fan_out.mmd -o /tmp/wide_fan_out.svg
```

---

## Structural class index

Each fixture is tagged with the layout class(es) it primarily exercises. Use this table to find a fixture that stresses a specific engine subsystem.

| Fixture | Structural class(es) |
|---|---|
| `single_section.mmd` | minimal / no-port edge case |
| `deep_linear.mmd` | linear chain / fold threshold |
| `parallel_independent.mmd` | disconnected components / row stacking |
| `wide_fan_out.mmd` | wide fan-out / junction creation |
| `wide_fan_in.mmd` | wide fan-in / bundle ordering at L-corners |
| `fan_in_merge.mmd` | same-line fan-in / merge-junction routing |
| `multi_input_convergence.mmd` | single-line multi-source convergence |
| `section_diamond.mmd` | section-level fork-join |
| `uneven_diamond.mmd` | fork-join with unequal-length branches / distinct track per branch (issue #610) |
| `shared_sink_parallel.mmd` | parallel multi-line branches with shared source and sink |
| `asymmetric_tree.mmd` | unbalanced branching / variable branch depth |
| `complex_multipath.mmd` | per-line route variation / bundle slot reservation |
| `trunk_through_fan.mmd` | trunk bundle entering and exiting a section that has an internal fork-join diamond |
| `terminal_symmetric_fan.mmd` | two-line bundle fanning out to three terminal nodes in a reporting section (no inter-terminal edges) |
| `multi_line_bundle.mmd` | dense bundle / tall station pills |
| `interchange_lane_reorder.mmd` | auto-interchange / interleaving-lane reorder (issue #779) |
| `mismatched_tracks.mmd` | per-line track mismatch between sections |
| `mixed_bundle_column.mmd` | mixed-cardinality fan-out into stacked column |
| `mixed_port_sides.mmd` | multi-side exit ports (RIGHT + BOTTOM) |
| `off_track_convergence.mmd` | multiple off-track inputs converging on one consumer |
| `off_track_convergence_multiline.mmd` | multiple off-track inputs converging on one consumer, carrying multiple lines |
| `upward_bypass.mmd` | tall section bypass (upward gap) |
| `bypass_label_rake.mmd` | bypass V climbs clear of a wide bypassed-station label |
| `rnaseq_lite.mmd` | realistic pipeline / TB+LR mix / diamond |
| `variant_calling.mmd` | realistic pipeline / asymmetric fork-join / 4-way fan-in |
| `funcprofiler_upstream.mmd` | dense fan-out + fan-in / known almost-horizontal defect |
| `fold_fan_across.mmd` | fan-in/out across fold boundary / rowspan optimization |
| `fold_double.mmd` | double-fold serpentine (LR -> RL -> LR) |
| `fold_stacked_branch.mmd` | stacked branches feeding through fold |
| `u_turn_fold.mmd` | fold with side line joining mid-trunk and leaving pre-end |
| `wide_label_fan.mmd` | wide station labels / auto label-wrap + column-spread (issue #405) |
| `wrapped_label_trunk.mmd` | wrapped label on a lower track pulled off the metro line above (issue #617) |
| `route_around_intervening.mmd` | inter-section line detouring around an intervening section box (issue #484) |
| `self_crossing_bridge.mmd` | same-colour self-crossing bridge glyph (issue #484) |
| `convergence_stacked_sink.mmd` | convergence return-row stacked-sibling migration (issue #484) |
| `cross_row_gap_wrap.mmd` | cross-row feed wrapping via the inter-row gap, no counter-flow (issue #484) |
| `stacked_lr_serpentine.mmd` | tall rowspan section alongside stacked single-row sections in the same column |
| `around_section_below.mmd` | inter-section edge routing around a section that sits below and between source and target |
| `inter_row_wrap_clearance.mmd` | three-line bundle exiting a top section right and entering a bottom section left via the inter-row gap |
| `tb_bottom_entry_flow_start.mmd` | flow-axis entry declared opposite its consumer (TB `entry: bottom` feeding the top station) re-anchored so the line does not fold back through the trunk (issue #885) |
| `tb_lr_exit_left.mmd` | TB section leaving through a LEFT exit into a section below-left (`_route_tb_lr_exit` LEFT arm) (issue #917) |
| `tb_left_exit_step.mmd` | TB section LEFT exit into a lower right-entry section: the exit bundle steps west-down-west and is routed as a parallel staircase that keeps the feed order (issue #671) |
| `tb_lr_exit_right.mmd` | TB section leaving through a RIGHT exit into the next forward section (`_route_tb_lr_exit` RIGHT arm) (issue #917) |
| `tb_internal_diagonal.mmd` | symmetric fan-out inside a TB section onto X tracks either side of the hub, routing both internal edges as 45-degree diagonals (`_route_tb_internal` diagonal arm) (issue #917) |

---

## Simple Topologies

### Single Section

A minimal pipeline with one section and one line. Tests the simplest case: no ports, no inter-section routing, no grid placement.

![Single Section](single_section.png)

### Deep Linear Chain

Seven sections connected in a straight chain with two lines. Exercises the grid fold threshold, where sections wrap to a second row when the chain gets too long.

![Deep Linear Chain](deep_linear.png)

### Parallel Independent

Two completely disconnected two-section pipelines (DNA and RNA). Tests row stacking of independent components that share no edges.

![Parallel Independent](parallel_independent.png)

---

## Fan-out and Fan-in

### Wide Fan-Out

One source section fanning out to four target sections, each carrying a different line. Tests junction creation, vertical stacking of sections in a single column, and port spacing when many lines diverge at once.

![Wide Fan-Out](wide_fan_out.png)

### Wide Fan-In

Four source sections converging into one target section. The inverse of fan-out: tests bundle ordering around L-shaped corners when multiple entry edges arrive from stacked sources.

![Wide Fan-In](wide_fan_in.png)

### Fan-In Merge

Same-line convergence: one line fans out from the source to all downstream sections, then reconverges at the sink. Each intermediate section also forwards to all subsequent sections, creating multiple bypass routes of the same line targeting one entry port. Tests merge junction insertion and trunk/branch routing, where the farthest bypass carries the full route and closer sources drop down to join it.

![Fan-In Merge](fan_in_merge.png)

### Section Diamond

A section-level fork-join: one source fans out to two parallel sections, which then reconverge into a single sink. Tests both fan-out junction creation and fan-in routing in the same topology.

### Terminal Symmetric Fan

A two-line bundle from a source section fans out to three independent terminal nodes (Shiny, MultiQC, Quarto) inside a reporting section. The terminals share no edges with each other. Tests fan-out routing where all targets are leaf nodes within a single entry-port section.

### Trunk Through Fan

Source and sink sections are connected through a middle section that contains an internal fork-join diamond (Split → Path Up/Down → Join). The two-line bundle enters the middle section, passes through the diamond, and exits as the same bundle into the sink. Tests that a trunk bundle is preserved end-to-end through a section whose interior contains parallel branches.

![Section Diamond](section_diamond.png)

### Uneven Diamond

A node-level fork-join where one branch (`b`) runs through an extra station before rejoining the shared sink while the other two branches (`a`, `c`) reach it directly. The branch length difference must not collapse the shorter branches onto a single track: each of the three branches gets a distinct track (issue #610).

---

## Branching and Multipath

### Asymmetric Tree

One root section branching into three paths of different depths (1, 2, and 3 sections deep). Tests unbalanced tree layout where branches occupy different numbers of grid columns.

![Asymmetric Tree](asymmetric_tree.png)

### Complex Multipath

Four lines taking different routes through six sections. Some lines skip sections entirely, others take detours through extra sections. Tests global bundle position reservation: when a line splits off and later rejoins, it returns to the same slot in the bundle.

![Complex Multipath](complex_multipath.png)

---

## Multi-line Bundles

### Multi-Line Bundle

Six lines travelling through the same three-section chain. Tests dense bundle rendering: station pill height, line offset stacking, and routing of thick bundles through inter-section gaps.

![Multi-Line Bundle](multi_line_bundle.png)

### Mixed Port Sides

A section with both RIGHT and BOTTOM exits, sending lines in two directions. Tests multi-side exit port placement and the combination of horizontal and vertical inter-section routing from the same source.

![Mixed Port Sides](mixed_port_sides.png)

---

## Realistic Pipelines

### RNA-seq Lite

A simplified RNA-seq pipeline with three analysis routes (STAR + Salmon, HISAT2, pseudo-alignment) diverging after a shared preprocessing section. Includes diamond patterns (FastP/Trim Galore) and line reconvergence at post-processing.

![RNA-seq Lite](rnaseq_lite.png)

### Variant Calling Pipeline

A variant calling pipeline with four lines (Whole Genome, Whole Exome, Targeted Panel, RNA Variants) sharing alignment but diverging to different callers before reconverging at annotation. Tests complex fork-join patterns with asymmetric branch depths.

![Variant Calling Pipeline](variant_calling.png)

---

## Fold Topologies

These examples trigger the auto-layout engine's **fold logic**, which wraps long pipelines into a serpentine layout when cumulative station layers exceed the fold threshold (default 15 columns). The threshold is configurable via `--max-layers-per-row`:

```bash
# Narrower layout with more folds
nf-metro render examples/topologies/deep_linear.mmd -o output.svg --max-layers-per-row 6

# Wider layout with fewer folds
nf-metro render examples/topologies/deep_linear.mmd -o output.svg --max-layers-per-row 20
```

### Fold Fan-Across

Three lines (TMT, Label-Free, DIA) diverge from a wide preprocessing section into three stacked quantification sections, then converge at a fold section (Normalization) before continuing on the return row. Tests junction creation across fold boundaries, rowspan optimization for the TB bridge, and post-fold RL direction inference.

![Fold Fan-Across](fold_fan_across.png)

### Fold Double (Serpentine)

A ten-section linear pipeline with two fold points, producing a true serpentine layout: LR on row 0, RL on row 1, LR on row 2. Tests the col_step zigzag toggle, ensuring the third row flows correctly instead of producing negative grid columns.

![Fold Double](fold_double.png)

### Fold Stacked Branch

Three stacked analysis sections (RNA, ATAC, Protein) feed into a fold section (Integration) that fans out to two stacked targets (Biological Interpretation, Technical QC) on the return row, converging into a final report. Tests rowspan optimization, fan-out from a TB fold section, and post-fold stacked branching.

![Fold Stacked Branch](fold_stacked_branch.png)

### U-Turn Fold

Long linear pipeline whose main line wraps via a fold into a return row, with a secondary line joining mid-trunk and exiting before the end. Tests fold rowspan transitions while a partial-coverage line shares the trunk only across a sub-range of sections.

---

## Structural Stress Tests

These fixtures don't appear in the gallery but back the topology validation suite.

### Multi-Input Convergence

Four independent single-station source sections all feeding the same `Merge` station in a sink section, all carrying one shared line. Tests single-line fan-in with sources stacked in a column.

### Shared Sink Parallel

One source feeds three structurally identical parallel branches that all converge into one sink. Every section carries the same 3-line bundle. Tests parallel multi-line trunks sharing a common source and a common sink.

### Mixed Bundle Column

One stacked column contains three siblings of different line counts: a 3-line branch, a 1-line branch, and a 1-line branch, all sourced from the same upstream section and converging at a shared sink. Tests fan-out from a wide bundle into mixed-cardinality siblings in the same grid column.

### Funcprofiler Upstream

Reduced upstream slice of nf-core/funcprofiler with one input section fanning out to seven profiler tools and back into a MultiQC section. Pinned via xfail in `test_no_almost_horizontal_edges` - documents a known almost-horizontal-edge defect in dense fan-out + fan-in topologies.

### Off-Track Convergence Multiline

Extends `off_track_convergence.mmd` with multiple off-track file inputs (FASTA reference, GTF annotation) converging on a processing section, this time carrying multiple lines (DNA, RNA, QC). The reference is used by the DNA and RNA lines; the annotation only by RNA. Tests off-track routing when different subsets of lines use each off-track input.

---

## #484 Regression Isolation

These minimal fixtures each isolate one layout/routing mechanism that was fixed for issue #484 (a dense long-read pipeline that exposed several engine bugs). Each triggers exactly one mechanism so a future regression in it makes a test fail.

### Route Around Intervening

Three sections in a row (Source, Middle, Target). The `skip` line runs Source to Target directly, skipping Middle. Tests that the inter-section edge detours *around* Middle's box (dropping into the inter-row band below it) rather than slicing through its interior. Backs `test_no_route_passes_through_unrelated_section` and the `_guard_no_route_through_section` guard.

### Self-Crossing Bridge

A single line whose long vertical bus (Top to Bus Sink, descending one column through an intermediate row) crosses its own horizontal connector (Mid Source to Mid Sink) belonging to a separate, non-reconverging branch of the same colour. Because the two legs share a colour but never rejoin, a bridge gap is drawn where the horizontal passes under the bus. Backs `test_bridge_glyph` and `compute_bridges`.

### Convergence Stacked Sink

A main spine (Prep, Align, Dedup) converges at Merge, which is fed both by the spine tail and by a Prep bypass spanning non-adjacent columns. The convergence drops Merge and its successors to a return row. `Repeats` (fed from a separate Aux input so it shares no predecessor with Merge) is a lone stacked spine-sibling that would otherwise sit alone in the spine band; the convergence placer migrates it into the return row. Tests the grid-collision migration in `auto_layout._detect_convergence_split` / `_place_with_convergence`: no two sections share a grid cell and no bboxes overlap.

### Cross-Row Gap Wrap

A convergence layout (Ingest, Align, Dedup on row 0; Merge, Report on the return row) where the `feed` line runs from Ingest down to the rightmost return-row section. Tests that the feed wraps via the clear inter-row gap above the return row (then drops straight into the port) rather than diving under the whole return row counter to its flow. Backs `test_no_artefactual_counter_flow`, `test_entry_approach_arrives_from_port_side`, and their guards.

### Stacked LR Serpentine

A tall section (Ingest, spanning 3 rows) sits in column 0 alongside three single-row sections (Alignment, Dedup, Variant Calling) stacked vertically in column 1. Tests rowspan layout where one section's height forces adjacent sections into a column stack rather than a horizontal chain.

### Around Section Below

Source (col 2, row 0) sends a two-line bundle both directly to Target (col 0, row 2) and sideways to Middle (col 1, row 1). The direct Source→Target inter-section edge must route around Middle, which sits between them diagonally. Tests that inter-section routing finds a path around a section occupying the space below and to the left of the source.

### Inter-Row Wrap Clearance

A three-line bundle exits the top section's right port, wraps via the inter-row gap, and enters the bottom section's left port. The two sections are stacked directly (same column, adjacent rows). Tests that the wrap uses the clear gap between rows rather than clipping the section boxes, and that port alignment is maintained across the wrap.

### Multi-Carrier Off-Row Exit Climb (`multicarrier_offrow_exit_climb.mmd`)

A pre-processing section whose lower trunk row carries two lines (`bam` from samtools sort/index, `other` from mosdepth) sitting below the section's port row. The exit fans out through a junction to a row-0 target (small variant calling) and a row-1 target (depth & repeats). Tests that a multi-carrier parallel bundle anchors on its shared carrier row so it runs flat inside the section, with the fan-out risers in the inter-section gap, rather than both lines climbing a diagonal up to the port inside the section (#938, extending the single-carrier anchor of #877).

### Junction Fan-out Convergence (`junction_fanout_convergence.mmd`)

Three lines converge into one joint-calling entry port on a single-row grid: `a` and `b` bypass the intervening sections and climb risers into the port, while `c` joins flat from the adjacent column. Tests that the flat shallow feeder (`c`) takes the port-near slot on top of the climbing risers so the bundle turns into the port concentrically, rather than the flat line weaving across the climbing pair at the corner (#940).

### Convergent Off-Row Exit Climb (`convergent_offrow_exit_climb.mmd`)

A single-row long-read variant-calling map. The annotation section carries only `snvvcf` and `svvcf` (the two highest-priority lines), reached through a bypass whose source section re-based those lines onto low slots. Tests that annotation's two-line bundle anchors on its own trunk (global slots 4,5 → local 0,1) rather than inheriting the high global slots, so its markers sit on their grid rows and the run into reports stays level instead of sloping (#941).

---

## Regression Catalogue

The fixtures below are targeted regression guards: each was added to pin a
specific routing or layout fix and is not individually gallery-illustrated.
They participate in the full topology validation suite (`pytest
tests/test_topology_validation.py`) alongside the documented fixtures
above.

To regenerate this catalogue from disk:

```bash
python scripts/list_topology_fixtures.py
```

### Bypass variants

| Fixture | What it tests |
|---|---|
| `bypass_fan_in_outer_slot.mmd` | Fan-in where the outermost bypass V lands in a slot beyond the inner bypasses - tests bypass slot reservation under mixed cardinality |
| `bypass_gap2_rightward_overflow.mmd` | Seven-line rightward bypass gap2 overflow clamp - tests that a wide bundle does not push bypass geometry off the canvas edge |
| `bypass_label_rake_left.mmd` | Bypass V climbing past a wide station label on the left side - extends `bypass_label_rake` for the left-overrun direction |
| `bypass_label_rake_wide.mmd` | Bypass V past an extra-wide label - tests the rake shift under maximal label width |
| `bypass_v_tight.mmd` | Two-line bypass V with minimal x-spacing - tests bypass geometry under the tightest legal x-spacing |

### Compact layout / gap heuristics

| Fixture | What it tests |
|---|---|
| `compact_gap_peer_conflict.mmd` | Compaction gap peer conflict - two peer sections compete for the same gap; tests that compact offsets resolve without overlap |
| `compact_hidden_passthrough.mmd` | Hidden pass-through compact - a hidden station sits in the compact gap; tests that compaction skips hidden-station rows correctly |
| `corridor_narrow_gap_fallback.mmd` | Corridor narrow gap fallback - an inter-section corridor is too narrow to route cleanly; tests the fallback routing path |
| `divergent_fanout_split.mmd` | Divergent fanout split - a fan-out where targets diverge immediately after the junction; tests that no false-positive overlap guard fires |
| `fan_bypass_nesting.mmd` | Fan-out combined with a nested bypass - tests that bypass nesting under a fan-out does not violate the crossing invariant |

### Cross-column perpendicular drop / perp entry

| Fixture | What it tests |
|---|---|
| `cross_col_top_entry.mmd` | Cross-column top entry - an LR section's TOP-entry port receiving from a horizontally-offset source; tests the dead-room removal fix (#890) |
| `cross_column_perp_drop.mmd` | Cross-column perpendicular drop - a line dropping from an LR section into a section below and to one side (#879) |
| `cross_column_perp_drop_far_exit.mmd` | Cross-column perp drop with a far-side exit - the source exits from the far face, requiring the lead-in to span only the source column (#892) |
| `lr_perp_bottom_exit_perp_entry.mmd` | LR section exiting via a BOTTOM port into a BOTTOM-entry section below - tests the perpendicular-to-perpendicular drop path |
| `lr_perp_bottom_exit_side_entry.mmd` | LR section BOTTOM exit into a side-entry section below - tests the BOTTOM-exit / side-entry routing arm |
| `lr_perp_top_exit_perp_entry.mmd` | LR section TOP exit into a TOP-entry section above - tests the perpendicular-to-perpendicular upward drop |
| `lr_perp_top_exit_perp_entry_diverging.mmd` | LR section TOP exit into a diverging TOP-entry target - tests the same path with multiple lines diverging at the entry port |
| `lr_perp_top_exit_side_entry.mmd` | LR section TOP exit into a side-entry section - tests the TOP-exit / side-entry routing arm |

### LR-to-TB top-entry routing

| Fixture | What it tests |
|---|---|
| `lr_to_tb_top_drop.mmd` | Single line from an LR section dropping into a TB section's TOP port - tests the clean vertical drop path |
| `lr_to_tb_top_drop_two_lines.mmd` | Two-line bundle dropping into a TB TOP port - tests bundle ordering at the drop |
| `lr_to_tb_top_cross_col.mmd` | LR-to-TB top drop where source and target are in different columns - tests the horizontal lead-in to the vertical drop |
| `lr_to_tb_top_near_vertical.mmd` | LR-to-TB near-vertical source - the source section is almost directly above the TB target; tests the near-vertical arm |
| `lr_to_tb_top_two_lines.mmd` | Two lines entering a TB top port from two separate source sections - tests independent drop routing under shared port alignment |

### Dogleg routing

| Fixture | What it tests |
|---|---|
| `dogleg_exempt_distinct.mmd` | Dogleg exemption under the distinct-line regime - a dogleg that should be suppressed when lines do not share a trunk (#939) |
| `dogleg_exempt_sameline.mmd` | Dogleg exemption under the same-line regime - the same topology with a shared line; tests that the same dogleg is correctly permitted |
| `dogleg_twoline_fanout.mmd` | Two-line fan-out producing a dogleg - tests that the dogleg guard fires correctly on a minimal fan-out case |
| `exit_corner_offset_dogleg.mmd` | Exit-corner offset dogleg (#939) - an off-grid exit corner produces a cosmetic jog; pinned as a known defect |

### Section-header placement

| Fixture | What it tests |
|---|---|
| `header_nudge.mmd` | Header nudged past a trunk route - tests the nudge-right fallback when the default above-section placement clashes with a route (#774) |
| `header_side_rotated.mmd` | Header rotated to a side face - tests the rotated-side placement arm of the header-placement chain (#774) |
| `top_entry_header_clash.mmd` | TOP-entry route clips the section header in its default position - tests that header placement relocates the badge clear of the incoming route |

### Junction entry

| Fixture | What it tests |
|---|---|
| `junction_entry_align.mmd` | Junction entry port alignment - tests that a multi-line bundle entering via a junction port aligns concentrically at the corner |
| `junction_entry_collision.mmd` | Junction entry collision skip - two lines enter the same junction with conflicting offsets; tests that the collision-skip logic produces a valid concentric order |
| `junction_entry_reversed_fold.mmd` | Junction entry under a reversed fold - tests that entry alignment is preserved when the section flows in the reverse (RL) direction (#760) |

### Left- and right-entry routing

| Fixture | What it tests |
|---|---|
| `around_below_ep_col_gt0.mmd` | Around-below routing when the entry point's column is > 0 - extends `around_section_below` to non-zero column positions |
| `bottom_row_climb_clear_corridor.mmd` | Bottom-row section receiving a line that must climb over a clear corridor - tests the corridor-clear climb path |
| `left_entry_up_wrap.mmd` | Left-entry bundle arriving via an upward wrap (source is below-right) - tests that bundle order is preserved through the up-then-left wrap corner (#758) |
| `right_entry_from_above.mmd` | RIGHT-entry section fed from a section in the row above - tests the drop-in path (#889) |
| `right_entry_from_above_far.mmd` | RIGHT-entry from above with the source far to the right - tests the drop-in path when the source is beyond the target's right edge (#889) |
| `right_entry_gap_above_empty_row.mmd` | RIGHT-entry with an empty row above the target - tests that the gap-above fallback fires when the drop-in is blocked by an empty row |
| `right_entry_wrap_no_fan.mmd` | RIGHT-entry wrap with a single line (no fan) - tests the wrap path without fan geometry |
| `rl_entry_runway.mmd` | RL-section entry runway - a section in RL direction requiring an extended approach runway; tests runway-length calculation |
| `stacked_left_exit_drop.mmd` | Stacked sections sharing a LEFT exit drop - tests that multiple stacked sections can share the same exit drop column without overlap |

### Merge / reconvergence routing

| Fixture | What it tests |
|---|---|
| `merge_around_below_leftmost.mmd` | Merge where the continuation must route around a section sitting below and to the left of the leftmost source |
| `merge_bottom_row_bypass.mmd` | Merge on the bottom row where one branch arrives via an inter-row bypass |
| `merge_leftmost_sink_branch.mmd` | Merge where the sink section is the leftmost section in its row - tests that the merge trunk does not overshoot left |
| `merge_offrow_continuation.mmd` | Merge continuation that lands off the trunk row - tests that the continuation trunk is re-anchored to the correct row |
| `merge_port_above_approach.mmd` | Merge port approached from above - tests the above-approach routing arm for a merge entry |
| `merge_pullaway.mmd` | Merge trunk pull-away across a cross-row sibling - tests that the trunk stays clear of the sibling section's bounding box |
| `merge_right_entry.mmd` | Merge feeder arriving via a cross-row RIGHT entry - tests the interaction of RIGHT-entry routing with merge-trunk continuation |
| `merge_trunk_out_of_range_section.mmd` | Merge trunk passing over a section outside its x-range - tests that the trunk does not clip sections it should not cross |
| `merge_trunk_over_low_section.mmd` | Merge trunk passing over a lower section - tests clear-corridor routing for trunks that cross over shorter sections |
| `post_convergence_trunk.mmd` | Trunk continuation after a convergence fold - tests that the post-convergence section inherits the correct trunk row and bundle offsets |
| `reconverge_reversed_fold.mmd` | Reconvergence from a reversed fold (#705) - tests that the back-run after a reversed fold stays level and the fan/merge order is preserved |

### Off-track / rail-mode / misc routing

| Fixture | What it tests |
|---|---|
| `clear_channel_target_aware_push.mmd` | Fan-descent target-aware channel push - the pushed descent lands on the target's side of the grazed section (#736) |
| `disjoint_sameline_trunks.mmd` | Two separate trunks for the same line in disjoint sections - tests that same-line bypass trunks do not falsely merge |
| `off_track_input_above_consumer.mmd` | Off-track file input positioned above its consumer - tests the above-consumer routing arm for off-track inputs |
| `peeloff_extra_line_consumer.mmd` | Peel-off where an extra line has its own consumer in the target section - tests that the extra-consumer line peels correctly from the bundle |
| `peeloff_riser_respace.mmd` | Peel-off riser respacing - tests that risers are re-spaced after a peel-off to maintain visual separation |
| `terminus_join.mmd` | Terminus join - two lines converging at a file terminus node; tests that the join routes cleanly when the terminus has a `%%metro file:` directive |
| `rail_offtrack_fan.mmd` | Rail-mode off-track fan-out - tests fan-out geometry under the `line_spread: rails` directive |
| `rail_offtrack_io.mmd` | Rail-mode off-track file input and output nodes - tests that rail-mode does not disturb off-track I/O node placement |
| `rail_offtrack_plain_io.mmd` | Rail-mode with plain (non-file) off-track I/O - tests the same path without the `%%metro file:` directive |

### TB section routing variants

| Fixture | What it tests |
|---|---|
| `tb_passthrough_trunk.mmd` | TB section acting as a pass-through trunk (no internal fork) - tests that a TB section with a straight trunk routes cleanly end to end |
| `tb_right_entry_stack.mmd` | TB section with a stacked RIGHT-entry - multiple lines entering a TB section from the right in a stacked configuration |
| `tb_trunk_through_fan.mmd` | TB section with an internal fan-out where the trunk continues through - tests the TB analogue of `trunk_through_fan` |
