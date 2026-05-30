# Layout Stage Contract

Per-stage pre/postconditions for `_compute_section_layout` in
`src/nf_metro/layout/engine.py`. The pipeline is a long chain of mutating
passes over a shared `MetroGraph`; this doc records what each pass assumes
and what it guarantees, so that adding or reordering a stage doesn't
silently violate a downstream pass's expectations.

## How to read this doc

- **Stage tag** matches the `# Stage X.Y:` comments inside
  `_compute_section_layout`. The first digit is the stage number (1-6,
  see "Stage overview" below); the second is sequential within the
  stage.
- **Lines** point at the entry comment of the stage in `engine.py` at the
  current HEAD. Re-grep `# Stage ` if the file shifts.
- **Precondition** = what the helper assumes. Pass-A stages assume
  global coordinates and ports on bbox edges; Pass-C stages assume
  finalised station Ys.
- **Postcondition** = the property the stage establishes (and that
  later stages may depend on).
- **Invariants preserved** = state the stage does NOT touch. Useful when
  asking "can I move this stage earlier?"
- **Related tests** = invariants in `tests/test_layout_invariants.py`
  that exercise the postcondition. Many tests are full-pipeline
  end-to-end checks (no single stage owns them outright); the mapping is
  "this stage is the one that establishes the property the test
  asserts," not "this test fails iff this stage regresses."

A stage whose purpose isn't crisp here is a structural-debt signal -
those rows are flagged "UNCLEAR" in the Notes column. Don't paper over
them; investigate before adding another stage next to them.

## Coordinate-system convention

Stages split into three regimes:

1. **Pre-Stage-2.1**: stations have section-local coordinates. Bboxes are
   in local coordinates.
2. **Post-Stage-2.1**: stations and bboxes are in global canvas
   coordinates. Ports do not yet exist on bbox edges.
3. **Post-Stage-3.1**: ports sit on bbox edges (validated by
   `_guard_ports_on_boundaries`).

## Validate-mode guards

`compute_layout(validate=True)` runs these guards at fixed checkpoints:

| Checkpoint | Guards |
|---|---|
| after Stage 1.1 | `_guard_section_bboxes_positive` |
| after Stage 2.1 | finite coords, stations-in-sections, bboxes-positive |
| after Stage 3.1 | ports-on-boundaries |
| after top-align (Stage 3.5) | ports-on-boundaries |
| after each Pass C sub-stage (bisection) | finite coords, bboxes-positive, ports-on-boundaries, station-x-column-drift, plus three phase-gated guards (see below) |
| after final | bisection set (all unconditional) + maintained-invariants (`assert_maintained`), off-track-above-consumer, row-trunk-cy-consistent, inter-section-routes-in-row-band |

Bisection checkpoints fire after every Pass C sub-stage (see the
`# Stage 5.2:` through `# Stage 6.16:` comments in
`_compute_section_layout`). Three guards
hold continuously only from a specific checkpoint onward, and the
bisection runner skips them earlier; see `_BISECTION_FIRST_VALID` in
`engine.py` for the threshold table:

| Guard | First valid checkpoint | Transient because |
|---|---|---|
| `_guard_stations_in_sections` | after Stage 5.3 | Stage 5.2's off-track lift moves stations above the section bbox; Stage 5.3 grows the bbox to enclose them. |
| `_guard_no_station_overlap` | after Stage 6.6 | Stage 6.4's snap-to-grid can land an off-track terminus icon on its on-track column-mate's Y; Stage 6.6 re-anchors the off-track above its consumer. |
| `_guard_no_line_crosses_non_consumer` | after Stage 6.14 | A sparse loop-side station sits on the trunk Y until Stage 6.14 shifts it to a half-grid offset; before that, sibling line bundles pass through its marker bbox. |

Three further guards are excluded from the bisection set entirely
(meaningful only at the final boundary); the `_run_pass_c_guards`
docstring in `engine.py` is the authoritative list.

Guard bodies live in `phases/guards.py` and are imported into `engine.py`;
the bisection runner is `_run_pass_c_guards`.

## Maintained invariants (#365)

Several Pass-C properties are kept alive *declaratively* rather than by
hand-placed restore calls. `phases/maintained.py` declares each as a
`MaintainedInvariant` (predicate + idempotent repair + priority); the
orchestrator calls `maintain(graph, maintained)` after each constructive
Pass-C phase that may perturb one, and the repairs run in priority order to
a fixpoint. The set:

| Invariant | Priority | Predicate | Repair |
|---|---|---|---|
| `canvas_top_margin` | 20 | topmost section bbox top `>= section_y_padding` | `_shift_graph_into_canvas` |
| `junctions_track_ports` | 30 | `junction.xy == _compute_junction_xy` | `_position_junctions` |

This is why the per-stage "re-run `_position_junctions`" / "shift into
canvas" notes below describe an *effect* now produced by `maintain`, not a
literal helper call at that line. A new restore-class property with a
*cheap, exact* "is it already satisfied?" predicate should be added here
as an invariant rather than as another hand-placed re-run; see
[`docs/dev/maintained_invariants_spike.md`](../../../docs/dev/maintained_invariants_spike.md)
for why complex restores (off-track stacking, top-align) stay procedural.
`assert_maintained` re-checks the set at the final validate boundary.

## Stage overview

The pipeline groups into six stages aligned with the coord-regime
transitions and the Pass A / Pass B / Pass C divisions used throughout
this doc.  See [`docs/dev/layout_pipeline.md`](../../../docs/dev/layout_pipeline.md)
for a prose walkthrough of each stage; the matching
`# ---- Stage N - ... ----` comment dividers in `_compute_section_layout`
mark each stage's start in the source.  Stage-table entries below appear
in pipeline order.

## Stage table

### Stage 1.1: internal section layout (engine.py:483-490)
- **Purpose**: Lay out each section's real stations in section-local
  coordinates via layer/track assignment.
- **Helper**: `_layout_single_section` (engine.py:3917).
- **Precondition**: Parser has populated `graph.sections`, `graph.stations`,
  `graph.edges`. Section directions and grid positions inferred by
  `auto_layout`. Ports exist in the graph but are not yet positioned.
- **Postcondition**: For every section with real stations, the section
  subgraph (returned via `section_subgraphs[sec_id]`) has every real
  station assigned a local `(x, y)`, a `layer`, and a `track`. Section
  `bbox_x/y/w/h` reflect the local content extent.
- **Invariants preserved**: Ports (`is_port=True`) are not positioned.
  Inter-section edges in `graph.edges` are untouched. Junctions are not
  positioned.
- **Related tests**: `test_section_bbox_contains_all_content`,
  `test_loop_column_stations_share_x`.

### Stage 1.2: align row Y grids (engine.py:495-496)
- **Purpose**: Snap station Ys to a shared row-wide grid so same-row
  same-direction sections agree on grid pitch and slot count.
- **Helper**: `_align_row_y_grids` (engine.py:958).
- **Precondition**: Stage 1.1 complete; sections still in local
  coordinates; section subgraphs available.
- **Postcondition**: Within each `(grid_row, direction)` group, all
  multi-station layers share one Y grid. Bbox `w/h` unchanged from
  Stage 1.1 (only station Ys shift). `graph._row_y_grid_info` stores
  grid metadata for the debug overlay.
- **Invariants preserved**: Isolated stations (sole layer occupants
  with off-grid Y) keep original Y - hub centering survives. Section
  bbox dimensions unchanged.
- **Related tests**: `test_row_trunk_marker_cy_consistent`,
  `test_all_stations_snap_to_grid`.

### Stage 1.3: section placement (engine.py:498-499)
- **Purpose**: Place sections on the canvas grid via topological
  layering of the section DAG.
- **Helper**: `place_sections` in `section_placement.py`.
- **Precondition**: Sections have bboxes from Stage 1.1 and grid
  positions from `auto_layout`. Still all local-coord.
- **Postcondition**: Every section has `offset_x`, `offset_y` set such
  that `(local + offset)` lands sections on a non-overlapping grid.
- **Invariants preserved**: Station local coords unchanged. Bboxes
  still local-coord.

### Stage 1.4: renumber sections (engine.py:501-502)
- **Purpose**: Renumber sections by visual reading order (sweep, col,
  row) so legend / debug numbering follows the eye.
- **Helper**: `_renumber_sections_by_grid` (engine.py:824).
- **Precondition**: Section grid positions and directions finalised.
- **Postcondition**: `section.display_number` reflects sweep-major,
  column-then-row order.
- **Invariants preserved**: Section IDs, station coords, bboxes,
  edges. Pure metadata pass.
- **Related tests**: none directly (cosmetic / debug-only).

### Stage 1.5: offset overshoot correction (engine.py:504-535)
- **Purpose**: Grow `x_offset`/`y_offset` when section local extents
  reach left/above the canvas origin, so global coords stay positive
  after Stage 2.1.
- **Helper**: inline.
- **Precondition**: Section `offset_x/y` and local `bbox_x/y` set.
- **Postcondition**: For every laid-out section, `offset_{x,y} +
  bbox_{x,y} + {x,y}_offset >= section_{x,y}_padding`.
- **Invariants preserved**: Section bboxes (local), station local
  coords, grid layout.

### Stage 2.1: local-to-global translation (engine.py:537-557)
- **Purpose**: Translate every real station and section bbox into
  global canvas coordinates.
- **Helper**: inline.
- **Precondition**: Stage 1.3 / 3b complete; `section.offset_{x,y}`,
  `x_offset`, `y_offset` final.
- **Postcondition**: Every real station's `x, y` and every section's
  `bbox_x, bbox_y` are global. `bbox_w, bbox_h` unchanged. Section
  subgraphs (local-coord) still exist but are not used downstream.
- **Invariants preserved**: Ports remain unpositioned. Junctions
  unpositioned.
- **Validate guards run after**: finite coords, stations-in-sections,
  bboxes-positive.
- **Related tests**: `test_section_bbox_contains_all_content` (the
  containment invariant first holds here).

### Stage 3.1: position ports on section boundaries (engine.py:565-570)
- **Purpose**: Place every port on its section's bbox edge at the
  section's nominal centre line for its side.
- **Helper**: `position_ports` in `section_placement.py`.
- **Precondition**: Section bboxes in global coords (Stage 2.1).
- **Postcondition**: Every port station's `(x, y)` lies on the bbox
  edge corresponding to its side, within `GUARD_TOLERANCE`. Ports
  start at the bbox-edge midpoint for their side.
- **Invariants preserved**: Real station coords, section bboxes,
  junctions.
- **Validate guard after**: `_guard_ports_on_boundaries`.

### Stage 3.2: align LR entry ports (engine.py:572-576)
- **Purpose**: For LEFT/RIGHT entry ports, set Y to the incoming
  source's Y so the inter-section horizontal run is straight; for
  TOP/BOTTOM entry ports, set X / Y accordingly.
- **Helper**: `_align_entry_ports` (engine.py:4788), dispatching to
  `_align_lr_entry_port` and `_align_tb_entry_port`.
- **Precondition**: Stage 3.1 placed ports on bbox edges. Junction
  positions are unknown - the helper uses `_resolve_source_xy` to
  derive junction coords on-the-fly.
- **Postcondition**: Each entry port's coordinate on the axis along
  its bbox edge matches its source's coordinate on that axis (within
  the section's bbox extent).
- **Invariants preserved**: Real station coords (Pass-A is port- and
  bbox-only). Exit ports. Junctions still unpositioned.
- **Related tests**: `test_no_kink_at_section_boundary` (the
  straight-run property this phase establishes).

### Stage 3.3: shift LR/RL perp-entry internal stations (engine.py:578-582)
- **Purpose**: When an LR/RL section has a TOP or BOTTOM (perpendicular)
  entry port, shift internal stations' X so the entry port has
  in-section runway before stations begin.
- **Helper**: `_shift_lr_perp_entry_stations` (engine.py:4520).
- **Precondition**: Stage 3.2 finalised LR/RL entry-port X for perp
  entries.
- **Postcondition**: Internal stations in such sections sit at least
  `x_spacing` away from the perp entry port X.
- **Invariants preserved**: Station Y, ports, bboxes (X shift is
  bbox-bounded).
- **Related tests**: `test_terminus_not_directly_after_diagonal`,
  `test_no_kink_at_section_boundary` (entry-side geometry).

### Stage 3.4: align fold-section exit ports (engine.py:584-588)
- **Purpose**: For row-spanning (fold) and TB-direction sections,
  shift LEFT/RIGHT exit ports to the target section's entry Y. May
  push the target section down via `_resolve_tb_exit_y`.
- **Helper**: `_align_exit_ports` (engine.py:5410), dispatching to
  `_align_lr_exit_port`.
- **Precondition**: Entry ports aligned (Stage 3.2); target sections
  positioned (Stage 1.3/4).
- **Postcondition**: Exit ports on fold/TB sections sit at the same Y
  as their target section's entry port (within section bbox extent).
- **Invariants preserved**: Real station coords. Entry-port Ys
  (Stage 3.5's top-align corrects any bbox push-down).
- **Related tests**: `test_no_kink_at_section_boundary`,
  `test_inter_section_route_y_stays_within_row_band`.

### Stage 3.5: top-align sections within each grid row (engine.py:590-594)
- **Purpose**: Shift sections up so contiguous column groups within a
  row share the same `bbox_y`.
- **Helper**: `_top_align_row_sections` (engine.py:1275).
- **Precondition**: All Stage-3.4 bbox shifts settled.
- **Postcondition**: Same-row contiguous-column sections share
  `bbox_y` (and station/port Y shifts by the same delta, preserving
  Stage 3.2 alignment).
- **Invariants preserved**: Relative station-to-section position
  inside each shifted section. Bbox heights.
- **Validate guard after**: `_guard_ports_on_boundaries` (top-align
  preserves port-on-edge by shifting ports with stations).

### Stage 4.1: align ports to downstream (engine.py:603-605)
- **Purpose**: For non-fold LR/RL sections, pull exit-entry port
  pairs toward the downstream section's internal stations so lines
  flow without detour.
- **Helper**: `_align_ports_to_downstream` (engine.py:4985).
- **Precondition**: Section geometry final (Pass A complete).
- **Postcondition**: Each non-fold LR/RL exit-entry pair Y sits near
  the downstream section's connected station Y.
- **Invariants preserved**: Section bboxes (movement is bbox-bounded,
  Stage 4.6/c recompute bboxes where needed). Real stations.
- **Related tests**: `test_no_kink_at_section_boundary`.

### Stage 4.2: snap sole-layer stations to ports (engine.py:607-609)
- **Purpose**: When a port-connected station is the only occupant of
  its layer, snap it to the port Y so the connection is horizontal.
- **Helper**: `_snap_sole_layer_stations_to_ports` (engine.py:5120).
- **Precondition**: Stage 4.1 settled port Ys.
- **Postcondition**: Sole-layer port-connected stations share Y with
  their port. Multi-station layers are skipped (would risk collision).
- **Invariants preserved**: Multi-station layer Ys. Shared row-Y grid
  is not respected here (Stage 6.4 re-snaps).
- **Related tests**: `test_section_entry_hub_on_grid` (downstream).

### Stage 4.3: snap grid-group entry ports (engine.py:611-615)
- **Purpose**: For grid-group sections (skipped by Stage 4.2), snap entry
  ports to the connected first-internal-station Y - straight
  port-to-station connection.
- **Helper**: `_snap_grid_group_entry_ports` (engine.py:5237).
- **Precondition**: Stage 4.2 complete.
- **Postcondition**: Grid-group entry ports share Y with their first
  connected internal station.
- **Invariants preserved**: Internal station Y. Exit ports.

### Stage 4.4: snap grid-group exit ports (engine.py:617-621)
- **Purpose**: Mirror of Stage 4.3 for exit ports - snap to the downstream
  entry port's Y (which Stage 4.3 just snapped to a grid station).
- **Helper**: `_snap_grid_group_exit_ports` (engine.py:5284).
- **Precondition**: Stage 4.3 complete (downstream entry ports snapped).
- **Postcondition**: Grid-group exit ports share Y with their
  downstream entry port (i.e. with the downstream's connected
  station).
- **Invariants preserved**: Internal stations.

### Stage 4.5: space ports from termini (engine.py:623-625)
- **Purpose**: Push ports away from terminus stations so a routed
  line clears any file-icon caption / label by at least `y_spacing`.
- **Helper**: `_space_ports_from_termini` (engine.py:5695).
- **Precondition**: Port Ys settled by Stages 4.1 to 4.4.
- **Postcondition**: For every (port, terminus) pair in the same
  section, `|port.y - terminus.y| >= y_spacing` (modulo bbox bounds).
  Bboxes may expand via `_expand_bbox_for_y` to keep ports on edges.
- **Invariants preserved**: Real non-terminus station Y. Other
  sections.

### Stage 4.6: recompute grid-group bboxes (engine.py:627-632)
- **Purpose**: Reset grid-group bboxes to symmetric `max_y_pad`
  padding around final non-port station Y range, then expand for any
  ports outside.
- **Helper**: `_recompute_grid_group_bboxes` (engine.py:1232).
- **Precondition**: Port Ys final (Stage 4.5).
- **Postcondition**: Each grid-group section bbox snugly bounds its
  content with consistent top/bottom padding.
- **Invariants preserved**: Station and port Ys.

### Stage 4.7: re-run top-align (engine.py:634-637)
- **Purpose**: Repeat Stage 3.5 after Stage 4.5 expanded bboxes via
  `_expand_bbox_for_y`.
- **Helper**: `_top_align_row_sections` (re-invoked).
- **Precondition**: Stages 4.5 / 4.6 complete.
- **Postcondition**: As Stage 3.5.
- **Invariants preserved**: As Stage 3.5.

### Stage 4.8: align row trunk Ys (engine.py:639-642)
- **Purpose**: Within each row, shift content downward in shallower
  sections so the inter-section trunk bundle passes through at a
  single Y. Bbox tops preserved (heights grow downward).
- **Helper**: `_align_row_trunk_ys` (engine.py:1414).
- **Precondition**: Stage 4.7 done.
- **Postcondition**: For sections in a row's contiguous column run,
  the trunk Y is the row's deepest pre-pass trunk Y. Row-spanning
  sections are skipped.
- **Invariants preserved**: Bbox tops. Row-spanning sections.

### Stage 4.9: redistribute fan-out siblings (engine.py:644-648)
- **Purpose**: For each fan-out column with a unique trunk junction
  (one station carrying the full bundle plus >=2 side branches),
  redistribute side stations symmetrically around the trunk Y. No-op
  unless `graph.center_ports` (guard inside the helper, not at the call
  site).
- **Helper**: `_redistribute_fanout_siblings` (`phases/fan_bundles.py`).
- **Precondition**: Trunk Ys aligned (Stage 4.8).
- **Postcondition**: In qualifying columns, fan-out siblings sit
  symmetrically around the trunk station's Y. Linear chains, fan-in
  structures, and file inputs are left in place.
- **Invariants preserved**: Trunk station Y. Off-track stations.

### Stage 4.10: redistribute full-bundle columns (engine.py)
- **Purpose**: When a column has no unique trunk (every station
  carries the full bundle - e.g. Reporting's Shiny + Quarto),
  symmetrically fan stations around the local LR port Y. No-op unless
  `center_ports` (guard inside the helper, not at the call site).
- **Helper**: `_redistribute_full_bundle_columns` (`phases/fan_bundles.py`).
- **Precondition**: Stage 4.9 ran.
- **Postcondition**: Full-bundle columns sit symmetric around the
  LR port Y.
- **Why both this and Stage 6.7**: Stage 6.7
  (``_recenter_full_bundle_columns``) re-fans the same columns
  using the final trunk Y, which can have drifted from Stage 4.10's
  port-Y anchor.  Stage 4.10's output is *not* redundant: the
  intermediate symmetric layout is read by Pass C's bbox-growth
  and compaction passes (an empty trunk row in fanned columns lets
  Stages 5.4 / 6.13 shrink the section bbox to the compact extent).
  Skipping Stage 4.10 changes intermediate bbox sizes and is not
  empty-render-diff -- the two passes are load-bearing in combination.
- **Invariants preserved**: Other columns.

### Stage 5.1: position junctions (engine.py:658-659)
- **Purpose**: Place each junction station in the inter-section gap
  at the exit port's Y (fan-out) or near the entry port (merge).
- **Helper**: `_position_junctions` (engine.py:4596).
- **Precondition**: All port Ys final (Pass B complete).
- **Postcondition**: Every junction has finite `(x, y)`. Fan-out
  junctions sit at `exit_port.y` plus a `JUNCTION_MARGIN` X offset
  toward the targets; merge junctions sit at
  `max(pred.x) + JUNCTION_MARGIN, entry_port.y`.
- **Invariants preserved**: Real stations, ports.

### Stage 5.2: lift off-track stations (engine.py)
- **Purpose**: Lift off-track file-input stations to the row above
  their consumer, stacking when multiple inputs share one consumer.
  Grow bbox upward; nudge same-section TOP ports back to new edge.
- **Helper**: `_lift_off_track_stations`.
- **Precondition**: Stage 5.1 complete; all on-track Ys final.
- **Postcondition**: Each off-track station sits at
  `consumer.y - n*y_spacing` (n = stack rank). Section bbox extends
  upward to fit.  May leave the topmost section above the canvas
  margin -- ``_shift_graph_into_canvas`` runs immediately afterwards
  to restore the margin (called explicitly by the caller, not by
  the helper).
- **Invariants preserved**: On-track station Y. Other sections' Ys
  (only the canvas Y-offset may shift the world uniformly).
- **Related tests**: `test_off_track_inputs_above_consumer`,
  `test_off_track_icons_ordered_by_consumer_y`.

### Stage 5.3: re-align row bbox tops only (engine.py:665-670)
- **Purpose**: After Stage 5.2 grew some bboxes upward, grow other
  same-row bboxes upward to match. Station Ys in unlifted sections
  preserved.
- **Helper**: `_top_align_row_bboxes_only` (engine.py:1348).
- **Precondition**: Stage 5.2 may have lifted some bboxes.
- **Postcondition**: Within each row's contiguous column group, all
  bboxes share `bbox_y` (heights extended upward as needed).
- **Invariants preserved**: All station / port Ys.

### Stage 5.4: compact row content to bbox top (engine.py:672-676)
- **Purpose**: Shift each row's column-group up by the smallest
  above-content slack, then shrink bbox heights to remove the empty
  band. Preserves trunk alignment.
- **Helper**: `_compact_row_content_to_bbox_top` (engine.py:1540).
- **Precondition**: Bbox tops aligned (Stage 5.3).
- **Postcondition**: Each row's contiguous column group's bbox top
  sits at `min(content_top) - section_y_padding`. Stations shift up
  by the same delta as their bbox.
- **Invariants preserved**: Inter-station relative positions inside
  each section. Trunk Y stays aligned across the row.
- **Related tests**: `test_section_bbox_has_bottom_padding`.

### Stage 5.5: snap inter-section port pairs + reposition junctions (engine.py:678-686)
- **Purpose**: Snap exit/entry port pairs in the same row to a shared
  Y (the entry's), then re-run Stage 5.1 to put junctions back on the
  exit port.
- **Helper**: `_snap_inter_section_port_pairs` (engine.py:1641) then
  `_position_junctions`.
- **Precondition**: Row compaction done; port pair Ys may have drifted.
- **Postcondition**: Within each row, every LEFT/RIGHT exit port and
  its connected LEFT/RIGHT entry port share a Y. Junctions back at
  exit-port Y.
- **Invariants preserved**: Internal station Y in each section.
- **Related tests**: `test_no_kink_at_section_boundary`,
  `test_inter_section_route_y_stays_within_row_band`.

### Stage 6.1: fan free content upward (engine.py:688-693)
- **Purpose**: When the row's compaction leaves visible empty top
  band but the section has trunk-candidate sibling stations,
  fan those upward into the empty band.
- **Helper**: `_fan_free_content_upward` (engine.py:1762).
- **Precondition**: Trunk Y aligned (Stage 4.8). Compaction done
  (Stage 5.4).
- **Postcondition**: Eligible sections fan stations upward by at most
  one `y_spacing` slot, balancing content above/below trunk.
- **Invariants preserved**: Trunk station Y. Off-track stations
  (sections with off-track band are skipped).
- **Related tests**: `test_section_top_band_filled`,
  `test_section1_input_above_trunk`.

### Stage 6.2: fan source inputs upward (engine.py:695-700)
- **Purpose**: Companion to Stage 6.1 for source-stack sections (single
  full-bundle trunk + subset-bundle file inputs at the entry column).
  Lift trunk-nearest source inputs into the empty top band.
- **Helper**: `_fan_source_inputs_upward` (engine.py:1852).
- **Precondition**: Stage 6.1 done.
- **Postcondition**: Section is top- and bottom-weighted around the
  trunk row instead of stacked below it.
- **Invariants preserved**: Trunk station Y.

### Stage 6.3: 2-branch symfan half-grid compaction (engine.py)
- **Purpose**: Sections containing exactly a 2-branch symmetric fan
  (no off-track / constraining content) collapse onto half-pitch
  offsets so the section is 1 grid-unit tall instead of 2. Records
  the placed stations on the public `MetroGraph.half_grid_station_ids`
  field so Stage 6.4 leaves them alone -- this is the only cross-
  phase channel for half-grid placement. Gated on `center_ports`.
- **Helper**: `_apply_half_grid_2branch_symfan`.
- **Precondition**: Stages 6.1 / 6.2 done; symfan classification stable
  (`_section_symfan_uses_half_grid`).
- **Postcondition**: Eligible symfan pairs share half-pitch offsets
  from the trunk Y. `graph.half_grid_station_ids` contains their IDs.
- **Invariants preserved**: Trunk station Y. Other sections.
- **Related tests**: `test_symfan_pairs_share_y`.

### Stage 6.4: snap all Y to grid (engine.py)
- **Purpose**: Final pass snapping every station and port Y to the
  nearest row-wide grid slot, removing fractional Ys left by earlier
  shifts. Stations listed in `graph.half_grid_station_ids` (populated
  by Stage 6.3) are skipped so they keep their intentional half-pitch
  Y.
- **Helper**: `_snap_all_y_to_grid`.
- **Precondition**: All semantic Y shifts done. If Stage 6.3 ran,
  `graph.half_grid_station_ids` is populated.
- **Postcondition**: Every station and port Y is a grid slot of the
  per-section / per-row pitch (except marked half-grid stations).
- **Invariants preserved**: X coordinates (tested by
  `test_grid_snap_does_not_mutate_x`). Half-grid station Ys.
- **Related tests**: `test_all_stations_snap_to_grid`,
  `test_grid_snap_does_not_mutate_x`.

### Stage 6.5: align TB-section bbox bottoms (engine.py:719-723)
- **Purpose**: Extend TB-section bbox bottom to match downstream
  LR/RL section's bbox bottom so the line doesn't look pinned to the
  TB bbox edge.
- **Helper**: `_align_tb_section_bbox_bottoms` (engine.py:5550).
- **Precondition**: All station/port Ys final (post-snap).
- **Postcondition**: For each TB section feeding an LR/RL target,
  `tb.bbox_y + tb.bbox_h >= target.bbox_y + target.bbox_h`.
- **Invariants preserved**: All station and port Ys. Other bboxes.

### Stage 6.6: reanchor off-track to consumer (engine.py)
- **Purpose**: Re-pin each off-track input at `consumer.y - n*y_spacing`
  using the consumer's final snapped Y (Stage 5.2 used pre-snap Ys).
  Grow bbox upward if needed.
- **Helper**: `_reanchor_off_track_to_consumer`.
- **Precondition**: Stage 6.4 snapped consumers to grid.
- **Postcondition**: Off-track inputs sit `n * y_spacing` above their
  consumer's final Y.  May leave the topmost section above the
  canvas margin -- ``_shift_graph_into_canvas`` runs immediately
  afterwards (called explicitly by the caller, not by the helper).
- **Invariants preserved**: On-track station Y.
- **Related tests**: `test_off_track_inputs_above_consumer`.

### Stage 6.7: re-center full-bundle columns (engine.py)
- **Purpose**: Re-fan full-bundle columns around the row's final trunk
  Y (Stage 4.10 used the local port Y which may now be stale).
  Gated on `center_ports`.
- **Helper**: `_recenter_full_bundle_columns`.
- **Precondition**: Final inter-section trunk Y known (post-snap).
- **Postcondition**: Full-bundle columns are symmetric around the
  row's final trunk Y.
- **Invariants preserved**: Off-track Y anchoring (re-established by
  Stage 6.8) and bbox-top alignment (re-established by Stage 6.9)
  are temporarily broken; both are restored before leaving the
  `if center_ports:` block.

### Stage 6.8: re-anchor off-track after recenter (engine.py)
- **Purpose**: The Stage 6.7 recenter moves consumers to the final
  trunk-anchored Y, leaving off-track icons stranded at the old
  consumer Y (and overlapping the consumer station). Re-pin each
  off-track at `consumer.y - n*y_spacing` on the post-recenter grid.
  Followed by ``_shift_graph_into_canvas`` to handle bbox grow that
  pushed the topmost section above the canvas margin.  Gated on
  `center_ports`.
- **Helper**: `_reanchor_off_track_to_consumer` (same helper as
  Stage 6.6; called again here on the post-recenter Ys).
- **Precondition**: Stage 6.7 has re-centred full-bundle columns.
- **Postcondition**: Off-track inputs sit one or more pitches above
  their post-recenter consumer. Section bboxes grow upward when
  lifted bands move above the existing top padding.
- **Invariants preserved**: Row top-alignment may be broken when a
  bbox grew upward; Stage 6.9 restores it.

### Stage 6.9: re-run row top-align (engine.py)
- **Purpose**: A Stage 6.8 bbox grow can leave the grown section's
  bbox top above its row mates'. Pull row mates' bbox tops up to
  match so the section row stays flush along its top edge. Gated on
  `center_ports`.
- **Helper**: `_top_align_row_bboxes_only` (same helper as Stage 5.3).
- **Precondition**: Stage 6.8 has re-anchored off-track inputs.
- **Postcondition**: Row bboxes flush at the top across all row mates.
- **Invariants preserved**: Station Ys (only bbox tops move).

### Stage 6.10: align terminus to upstream (engine.py:759-763)
- **Purpose**: After Stage 6.7 re-pitched fanned columns, a single-station
  downstream column (e.g. a `file` terminus) may have stayed at its
  pre-fan Y. Pin it back onto its sole upstream's Y.
- **Helper**: `_align_terminus_to_upstream` (engine.py:3860).
- **Precondition**: Stage 6.7 re-centered fans.
- **Postcondition**: Single-station downstream columns share Y with
  their unique upstream.
- **Invariants preserved**: Multi-station columns.
- **Related tests**: `test_terminus_not_directly_after_diagonal`.

### Stage 6.11: balance section content around trunk
- **Purpose**: Auto-balance pass. For sections whose final layout
  still has an empty band above the trunk while more siblings sit
  below than above, lift bottommost movable siblings into the empty
  top band. U-turn-safe and bbox-bounded.
- **Gating**: Early-returns unless **both** `graph._explicit_grid` and
  `graph.center_ports` are set (scoped to explicit-`%%metro grid:` +
  centre-ports pipelines), so it is a no-op on auto-laid graphs.
- **Helper**: `_balance_section_content_around_trunk` (engine.py:2030).
- **Precondition**: All earlier 13-phase reshuffles done.
- **Postcondition**: Sibling count above trunk >= sibling count below
  trunk (where movable), inside bbox.
- **Invariants preserved**: Trunk station Y. Sections that already
  balance are left alone.
- **Related tests**: `test_section_top_band_filled`.

### Stage 6.12: recenter loop side stations (engine.py:773-781)
- **Purpose**: Recompute the X of fan-out side stations (one trunk
  predecessor, one trunk successor - "loop side" stations like propd,
  dream, DESeq2 around limma) to the midpoint of their actual diagonal
  corner Xs from the routing geometry.
- **Helper**: `_recenter_loop_side_stations` (engine.py:2297).
- **Precondition**: All Y phases done; routing geometry derivable.
- **Postcondition**: Loop side stations sit at the visual centre of
  their horizontal loop run.
- **Invariants preserved**: Station Y. Pure-side-branch classification
  is strict (see `test_loop_recenter_only_for_pure_side_branches`).
- **Related tests**: `test_fan_station_centered_on_loop`,
  `test_loop_recenter_only_for_pure_side_branches`,
  `test_loop_column_stations_share_x`.

### Stage 6.13: shrink and tighten rows (engine.py:783-794)
- **Purpose**: Shrink each section's bbox bottom to
  `max_content_y + section_y_padding` (phase 1), then pull lower-row
  sections up to close any vertical slack the shrink revealed
  (phase 2).  Phase 1 handles bbox bottoms that drifted after earlier
  passes lifted content; phase 2 handles the pre-shrink row-height
  overestimate when a rowspan section collapses to less than its
  row claim.  Phase 2 must run as a second pass over the graph so
  every section's shrink is finalised before row-gap deficits are
  measured.
- **Helper**: `_shrink_and_tighten_rows` (orchestrates
  `_shrink_bboxes_to_content_bottom` then
  `_tighten_lower_rows_after_shrink`).
- **Precondition**: All content Ys final.
- **Postcondition**: Section bbox bottoms sit `section_y_padding`
  below the deepest content (trunk alignment unaffected -- only
  bottom shrinks).  For each row pair, the row gap is `section_y_gap`
  (no more, no less, except where rowspan sections filled their full
  row claim).
- **Invariants preserved**: Bbox tops. Within-row trunk Ys. Bbox
  heights of upper rows.
- **Related tests**: `test_section_bbox_has_bottom_padding`,
  `test_section_bbox_matches_content_extent`.

### Stage 6.14: shift and propagate loop stations (engine.py:796-806)
- **Purpose**: Shift sparse loop-side stations (one inbound, one
  outbound, single-line consumer) onto a half-pitch Y when sharing
  the full-row Y with a busier sibling whose inbound bundle would
  otherwise breeze-past the sparse station's marker.  When a shift
  grows a section's bbox downward, push lower-row sections down
  internally to restore `section_y_gap`.
- **Helper**: `_shift_and_propagate_loop_stations`
  (calls `_push_lower_rows_after_bbox_grow` when any bbox grew).
- **Precondition**: Bundle Ys final.
- **Postcondition**: Sparse single-line loop stations whose row Y
  conflicts with a busier sibling's bundle move to a half-pitch
  offset (may grow bbox downward).  Row gaps preserved across any
  bbox grow.
- **Invariants preserved**: Busy sibling Y. Bundle Y. Within-row Ys
  of unaffected sections.
- **Related tests**: `test_lines_dont_cross_non_consumer_markers`,
  `test_no_icon_overlaps_line_path`,
  `test_row_gap_accommodates_bypass`.

### Stage 6.15a: restore symmetric top padding
- **Purpose**: Fan re-distribution (Stages 4.9 / 4.10 / 6.7 / 6.11) can
  lift a branch above the content-top line the bbox was sized for,
  crowding the topmost marker against the bbox top while the bottom keeps
  its full band. Grow each bbox top to a full `section_y_padding` above the
  highest marker (bounded by the row above) so fanned-above content sits
  centred. The upward grow can breach the canvas top margin, restored by
  the `canvas_top_margin` invariant.
- **Helper**: `_grow_bboxes_to_content_top` (`phases/bbox.py`), then
  `maintain` (canvas restore).
- **Precondition**: All content Ys final (post-6.14).
- **Postcondition**: Each bbox top sits `section_y_padding` above its
  highest marker, bounded by the row above.
- **Invariants preserved**: Station Ys (only bbox tops grow). Resolves #406.
- **Related tests**: `test_section_bbox_has_top_padding`.

### Stage 6.15: snap canvas to the y-grid
- **Purpose**: After all settling, restore canvas-wide grid alignment.
  Stage 6.4 snaps to a per-row grid, but later helpers (notably
  `_shift_graph_into_canvas` shifting by a non-grid amount) can leave a
  uniform residue; shift the whole canvas back onto integer `y_spacing`
  multiples.
- **Helper**: `_snap_canvas_y_to_grid`.
- **Precondition**: All other Y phases done.
- **Postcondition**: Real stations sharing a single non-zero residue are
  shifted onto integer `y_spacing` multiples; mixed-residue (multi-row)
  layouts and half-grid / convergence stations are left untouched.
- **Invariants preserved**: Relative station/section/port Ys (the whole
  canvas moves by one delta).
- **Related tests**: `test_auto_y_spacing_fits_content`.

### Stage 6.16: re-align TB entry ports with feeders
- **Purpose**: A TB/BT section's perpendicular entry port is pinned a fixed
  offset above its first internal station, so the late vertical settling
  (Stages 6.13-6.15) that shifts the section's content drags the entry port
  off the upstream feeder Y it was snapped to in Stage 3.2, re-introducing
  an inter-section S-kink. Re-run the alignment (TB/BT only, to leave
  settled LR/RL geometry untouched) to re-snap the port to its now-settled
  feeder; the `junctions_track_ports` invariant re-anchors junctions after.
- **Helper**: `_align_entry_ports(graph, tb_only=True)` (`phases/ports.py`),
  then `maintain` (junction restore).
- **Precondition**: All vertical settling done (post-6.15).
- **Postcondition**: TB/BT entry ports share their upstream feeder's Y;
  junctions re-anchored to the settled ports.
- **Invariants preserved**: LR/RL entry/exit geometry (skipped by
  `tb_only`).
- **Validate guard after**: bisection set ("after Stage 6.16").

## Unclear / structural-debt signals

No open signals at this time. Add new entries here when phase
pre/postconditions reveal a candidate for cleanup.

## Adding a new stage: checklist

When adding a new stage to `_compute_section_layout`, document the
following before merging:

1. **Stage tag**: pick the next sequential number within the
   appropriate stage (e.g. a new Stage 6.x sub-step gets the next
   integer after Stage 6.16).  Historical note: the organic phase
   suffix tree (`13d2`, `13k2`, the `Phase 13k` -> `Phase 13k2`
   rename in PR #342) is what the flat Stage.N scheme is designed to
   prevent.
2. **Helper location**: top-level function in `engine.py` (or a new
   module if it's substantial). Stage comments in the function body
   must reference the helper.
3. **Precondition**: what state on the graph the helper assumes.
   Mention coordinate-system regime (local vs global), whether ports
   are positioned, whether junctions are positioned, and whether
   trunks/grids are final.
4. **Postcondition**: the property the stage guarantees. Be concrete -
   "Y values are snapped to the row grid" not "Y values look nice".
5. **Invariants preserved**: what the stage does NOT change. Crucial
   for reasoning about reorder safety. Bboxes? Other sections?
   Off-track stations? Half-grid marker set?
6. **Related tests**: which invariants in `tests/test_layout_invariants.py`
   defend the postcondition. If none, add one - stages without test
   coverage are how the Phase-13-suffix sprawl happened in the first
   place.
7. **Validate-mode coverage**: if the stage introduces a new property
   that should hold permanently, add a `_guard_*` helper and call it
   from `validate=True` mode.
8. **Update this doc**: extend the per-stage table above and call out
   any cross-stage coupling in the structural-debt section.
