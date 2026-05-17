# Layout Phase Contract

Per-phase pre/postconditions for `_compute_section_layout` in
`src/nf_metro/layout/engine.py`. The pipeline is a long chain of mutating
passes over a shared `MetroGraph`; this doc records what each pass assumes
and what it guarantees, so that adding or reordering a phase doesn't
silently violate a downstream pass's expectations.

## How to read this doc

- **Phase tag** matches the `# Phase N[suffix]` comments inside
  `_compute_section_layout`. Suffixes (`13d2`, `13ca`, ...) reflect
  organic history; treat them as opaque identifiers, not a hierarchy.
- **Lines** point at the entry comment of the phase in `engine.py` at the
  current HEAD. Re-grep `# Phase N` if the file shifts.
- **Precondition** = what the helper assumes. Pass-A phases assume
  global coordinates and ports on bbox edges; Pass-C phases assume
  finalised station Ys.
- **Postcondition** = the property the phase establishes (and that
  later phases may depend on).
- **Invariants preserved** = state the phase does NOT touch. Useful when
  asking "can I move this phase earlier?"
- **Related tests** = invariants in `tests/test_layout_invariants.py`
  that exercise the postcondition. Many tests are full-pipeline
  end-to-end checks (no single phase owns them outright); the mapping is
  "this phase is the one that establishes the property the test
  asserts," not "this test fails iff this phase regresses."

A phase whose purpose isn't crisp here is a structural-debt signal -
those rows are flagged "UNCLEAR" in the Notes column. Don't paper over
them; investigate before adding another phase next to them.

## Coordinate-system convention

Phases split into three regimes:

1. **Pre-Phase-4**: stations have section-local coordinates. Bboxes are
   in local coordinates.
2. **Post-Phase-4**: stations and bboxes are in global canvas
   coordinates. Ports do not yet exist on bbox edges.
3. **Post-Phase-5**: ports sit on bbox edges (validated by
   `_guard_ports_on_boundaries`).

## Validate-mode guards

`compute_layout(validate=True)` runs these guards at fixed checkpoints:

| Checkpoint | Guards |
|---|---|
| after Phase 2 | `_guard_section_bboxes_positive` |
| after Phase 4 | finite coords, stations-in-sections, bboxes-positive |
| after Phase 5 | ports-on-boundaries |
| after top-align (Phase 9) | ports-on-boundaries |
| after Phase 12 (final) | finite coords, bboxes-positive, stations-in-sections, ports-on-boundaries, no-station-overlap, no-line-crosses-non-consumer |

Guard bodies live at the top of `engine.py` (lines 83-275).

## Phase table

### Phase 2: internal section layout (engine.py:483-490)
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

### Phase 2.5: align row Y grids (engine.py:495-496)
- **Purpose**: Snap station Ys to a shared row-wide grid so same-row
  same-direction sections agree on grid pitch and slot count.
- **Helper**: `_align_row_y_grids` (engine.py:958).
- **Precondition**: Phase 2 complete; sections still in local
  coordinates; section subgraphs available.
- **Postcondition**: Within each `(grid_row, direction)` group, all
  multi-station layers share one Y grid. Bbox `w/h` unchanged from
  Phase 2 (only station Ys shift). `graph._row_y_grid_info` stores
  grid metadata for the debug overlay.
- **Invariants preserved**: Isolated stations (sole layer occupants
  with off-grid Y) keep original Y - hub centering survives. Section
  bbox dimensions unchanged.
- **Related tests**: `test_row_trunk_marker_cy_consistent`,
  `test_all_stations_snap_to_grid`.

### Phase 3: section placement (engine.py:498-499)
- **Purpose**: Place sections on the canvas grid via topological
  layering of the section DAG.
- **Helper**: `place_sections` in `section_placement.py`.
- **Precondition**: Sections have bboxes from Phase 2 and grid
  positions from `auto_layout`. Still all local-coord.
- **Postcondition**: Every section has `offset_x`, `offset_y` set such
  that `(local + offset)` lands sections on a non-overlapping grid.
- **Invariants preserved**: Station local coords unchanged. Bboxes
  still local-coord.

### Phase 3a: renumber sections (engine.py:501-502)
- **Purpose**: Renumber sections by visual reading order (sweep, col,
  row) so legend / debug numbering follows the eye.
- **Helper**: `_renumber_sections_by_grid` (engine.py:824).
- **Precondition**: Section grid positions and directions finalised.
- **Postcondition**: `section.display_number` reflects sweep-major,
  column-then-row order.
- **Invariants preserved**: Section IDs, station coords, bboxes,
  edges. Pure metadata pass.
- **Related tests**: none directly (cosmetic / debug-only).

### Phase 3b: offset overshoot correction (engine.py:504-535)
- **Purpose**: Grow `x_offset`/`y_offset` when section local extents
  reach left/above the canvas origin, so global coords stay positive
  after Phase 4.
- **Helper**: inline.
- **Precondition**: Section `offset_x/y` and local `bbox_x/y` set.
- **Postcondition**: For every laid-out section, `offset_{x,y} +
  bbox_{x,y} + {x,y}_offset >= section_{x,y}_padding`.
- **Invariants preserved**: Section bboxes (local), station local
  coords, grid layout.

### Phase 4: local-to-global translation (engine.py:537-557)
- **Purpose**: Translate every real station and section bbox into
  global canvas coordinates.
- **Helper**: inline.
- **Precondition**: Phase 3 / 3b complete; `section.offset_{x,y}`,
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

### Phase 5: position ports on section boundaries (engine.py:565-570)
- **Purpose**: Place every port on its section's bbox edge at the
  section's nominal centre line for its side.
- **Helper**: `position_ports` in `section_placement.py`.
- **Precondition**: Section bboxes in global coords (Phase 4).
- **Postcondition**: Every port station's `(x, y)` lies on the bbox
  edge corresponding to its side, within `GUARD_TOLERANCE`. Ports
  start at the bbox-edge midpoint for their side.
- **Invariants preserved**: Real station coords, section bboxes,
  junctions.
- **Validate guard after**: `_guard_ports_on_boundaries`.

### Phase 5b: (none) - placeholder
- No 5b phase exists; numbering jumps to Phase 6.

### Phase 6: align LR entry ports (engine.py:572-576)
- **Purpose**: For LEFT/RIGHT entry ports, set Y to the incoming
  source's Y so the inter-section horizontal run is straight; for
  TOP/BOTTOM entry ports, set X / Y accordingly.
- **Helper**: `_align_entry_ports` (engine.py:4788), dispatching to
  `_align_lr_entry_port` and `_align_tb_entry_port`.
- **Precondition**: Phase 5 placed ports on bbox edges. Junction
  positions are unknown - the helper uses `_resolve_source_xy` to
  derive junction coords on-the-fly.
- **Postcondition**: Each entry port's coordinate on the axis along
  its bbox edge matches its source's coordinate on that axis (within
  the section's bbox extent).
- **Invariants preserved**: Real station coords (Pass-A is port- and
  bbox-only). Exit ports. Junctions still unpositioned.
- **Related tests**: `test_no_kink_at_section_boundary` (the
  straight-run property this phase establishes).

### Phase 7: shift LR/RL perp-entry internal stations (engine.py:578-582)
- **Purpose**: When an LR/RL section has a TOP or BOTTOM (perpendicular)
  entry port, shift internal stations' X so the entry port has
  in-section runway before stations begin.
- **Helper**: `_shift_lr_perp_entry_stations` (engine.py:4520).
- **Precondition**: Phase 6 finalised LR/RL entry-port X for perp
  entries.
- **Postcondition**: Internal stations in such sections sit at least
  `x_spacing` away from the perp entry port X.
- **Invariants preserved**: Station Y, ports, bboxes (X shift is
  bbox-bounded).
- **Related tests**: `test_terminus_not_directly_after_diagonal`,
  `test_no_kink_at_section_boundary` (entry-side geometry).

### Phase 8: align fold-section exit ports (engine.py:584-588)
- **Purpose**: For row-spanning (fold) and TB-direction sections,
  shift LEFT/RIGHT exit ports to the target section's entry Y. May
  push the target section down via `_resolve_tb_exit_y`.
- **Helper**: `_align_exit_ports` (engine.py:5410), dispatching to
  `_align_lr_exit_port`.
- **Precondition**: Entry ports aligned (Phase 6); target sections
  positioned (Phase 3/4).
- **Postcondition**: Exit ports on fold/TB sections sit at the same Y
  as their target section's entry port (within section bbox extent).
- **Invariants preserved**: Real station coords. Entry-port Ys
  (Phase 9's top-align corrects any bbox push-down).
- **Related tests**: `test_no_kink_at_section_boundary`,
  `test_inter_section_route_y_stays_within_row_band`.

### Phase 9: top-align sections within each grid row (engine.py:590-594)
- **Purpose**: Shift sections up so contiguous column groups within a
  row share the same `bbox_y`.
- **Helper**: `_top_align_row_sections` (engine.py:1275).
- **Precondition**: All Phase-8 bbox shifts settled.
- **Postcondition**: Same-row contiguous-column sections share
  `bbox_y` (and station/port Y shifts by the same delta, preserving
  Phase 6 alignment).
- **Invariants preserved**: Relative station-to-section position
  inside each shifted section. Bbox heights.
- **Validate guard after**: `_guard_ports_on_boundaries` (top-align
  preserves port-on-edge by shifting ports with stations).

### Phase 10: align ports to downstream (engine.py:603-605)
- **Purpose**: For non-fold LR/RL sections, pull exit-entry port
  pairs toward the downstream section's internal stations so lines
  flow without detour.
- **Helper**: `_align_ports_to_downstream` (engine.py:4985).
- **Precondition**: Section geometry final (Pass A complete).
- **Postcondition**: Each non-fold LR/RL exit-entry pair Y sits near
  the downstream section's connected station Y.
- **Invariants preserved**: Section bboxes (movement is bbox-bounded,
  Phase 11b/c recompute bboxes where needed). Real stations.
- **Related tests**: `test_no_kink_at_section_boundary`.

### Phase 10b: snap sole-layer stations to ports (engine.py:607-609)
- **Purpose**: When a port-connected station is the only occupant of
  its layer, snap it to the port Y so the connection is horizontal.
- **Helper**: `_snap_sole_layer_stations_to_ports` (engine.py:5120).
- **Precondition**: Phase 10 settled port Ys.
- **Postcondition**: Sole-layer port-connected stations share Y with
  their port. Multi-station layers are skipped (would risk collision).
- **Invariants preserved**: Multi-station layer Ys. Shared row-Y grid
  is not respected here (Phase 13e re-snaps).
- **Related tests**: `test_section_entry_hub_on_grid` (downstream).

### Phase 10c: snap grid-group entry ports (engine.py:611-615)
- **Purpose**: For grid-group sections (skipped by 10b), snap entry
  ports to the connected first-internal-station Y - straight
  port-to-station connection.
- **Helper**: `_snap_grid_group_entry_ports` (engine.py:5237).
- **Precondition**: Phase 10b complete.
- **Postcondition**: Grid-group entry ports share Y with their first
  connected internal station.
- **Invariants preserved**: Internal station Y. Exit ports.

### Phase 10d: snap grid-group exit ports (engine.py:617-621)
- **Purpose**: Mirror of 10c for exit ports - snap to the downstream
  entry port's Y (which 10c just snapped to a grid station).
- **Helper**: `_snap_grid_group_exit_ports` (engine.py:5284).
- **Precondition**: 10c complete (downstream entry ports snapped).
- **Postcondition**: Grid-group exit ports share Y with their
  downstream entry port (i.e. with the downstream's connected
  station).
- **Invariants preserved**: Internal stations.

### Phase 11: space ports from termini (engine.py:623-625)
- **Purpose**: Push ports away from terminus stations so a routed
  line clears any file-icon caption / label by at least `y_spacing`.
- **Helper**: `_space_ports_from_termini` (engine.py:5695).
- **Precondition**: Port Ys settled by Phases 10-10d.
- **Postcondition**: For every (port, terminus) pair in the same
  section, `|port.y - terminus.y| >= y_spacing` (modulo bbox bounds).
  Bboxes may expand via `_expand_bbox_for_y` to keep ports on edges.
- **Invariants preserved**: Real non-terminus station Y. Other
  sections.

### Phase 11b: recompute grid-group bboxes (engine.py:627-632)
- **Purpose**: Reset grid-group bboxes to symmetric `max_y_pad`
  padding around final non-port station Y range, then expand for any
  ports outside.
- **Helper**: `_recompute_grid_group_bboxes` (engine.py:1232).
- **Precondition**: Port Ys final (Phase 11).
- **Postcondition**: Each grid-group section bbox snugly bounds its
  content with consistent top/bottom padding.
- **Invariants preserved**: Station and port Ys.

### Phase 11c: re-run top-align (engine.py:634-637)
- **Purpose**: Repeat Phase 9 after Phase 11 expanded bboxes via
  `_expand_bbox_for_y`.
- **Helper**: `_top_align_row_sections` (re-invoked).
- **Precondition**: Phase 11/11b complete.
- **Postcondition**: As Phase 9.
- **Invariants preserved**: As Phase 9.

### Phase 11ca: align row trunk Ys (engine.py:639-642)
- **Purpose**: Within each row, shift content downward in shallower
  sections so the inter-section trunk bundle passes through at a
  single Y. Bbox tops preserved (heights grow downward).
- **Helper**: `_align_row_trunk_ys` (engine.py:1414).
- **Precondition**: Phase 11c done.
- **Postcondition**: For sections in a row's contiguous column run,
  the trunk Y is the row's deepest pre-pass trunk Y. Row-spanning
  sections are skipped.
- **Invariants preserved**: Bbox tops. Row-spanning sections.

### Phase 11d: redistribute fan-out siblings (engine.py:644-648)
- **Purpose**: For each fan-out column with a unique trunk junction
  (one station carrying the full bundle plus >=2 side branches),
  redistribute side stations symmetrically around the trunk Y. Gated
  on `graph.center_ports`.
- **Helper**: `_redistribute_fanout_siblings` (engine.py:3163).
- **Precondition**: Trunk Ys aligned (Phase 11ca).
- **Postcondition**: In qualifying columns, fan-out siblings sit
  symmetrically around the trunk station's Y. Linear chains, fan-in
  structures, and file inputs are left in place.
- **Invariants preserved**: Trunk station Y. Off-track stations.

### Phase 11da: redistribute full-bundle columns (engine.py:650-653)
- **Purpose**: When a column has no unique trunk (every station
  carries the full bundle - e.g. Reporting's Shiny + Quarto),
  symmetrically fan stations around the local LR port Y. Gated on
  `center_ports`.
- **Helper**: `_redistribute_full_bundle_columns` (engine.py:3424).
- **Precondition**: Phase 11d ran.
- **Postcondition**: Full-bundle columns sit symmetric around the
  LR port Y. Re-centered later by Phase 13h once final trunk Y is
  known.
- **Invariants preserved**: Other columns.

### Phase 12: position junctions (engine.py:658-659)
- **Purpose**: Place each junction station in the inter-section gap
  at the exit port's Y (fan-out) or near the entry port (merge).
- **Helper**: `_position_junctions` (engine.py:4596).
- **Precondition**: All port Ys final (Pass B complete).
- **Postcondition**: Every junction has finite `(x, y)`. Fan-out
  junctions sit at `exit_port.y` plus a `JUNCTION_MARGIN` X offset
  toward the targets; merge junctions sit at
  `max(pred.x) + JUNCTION_MARGIN, entry_port.y`.
- **Invariants preserved**: Real stations, ports.

### Phase 13: lift off-track stations (engine.py:661-663)
- **Purpose**: Lift off-track file-input stations to the row above
  their consumer, stacking when multiple inputs share one consumer.
  Grow bbox upward; nudge same-section TOP ports back to new edge.
- **Helper**: `_lift_off_track_stations` (engine.py:6582).
- **Precondition**: Phase 12 complete; all on-track Ys final.
- **Postcondition**: Each off-track station sits at
  `consumer.y - n*y_spacing` (n = stack rank). Section bbox extends
  upward to fit. Canvas Y-offset increases if needed
  (`_shift_graph_into_canvas`).
- **Invariants preserved**: On-track station Y. Other sections' Ys
  (only the canvas Y-offset may shift the world uniformly).
- **Related tests**: `test_off_track_inputs_above_consumer`,
  `test_off_track_icons_ordered_by_consumer_y`.

### Phase 13a: re-align row bbox tops only (engine.py:665-670)
- **Purpose**: After Phase 13 grew some bboxes upward, grow other
  same-row bboxes upward to match. Station Ys in unlifted sections
  preserved.
- **Helper**: `_top_align_row_bboxes_only` (engine.py:1348).
- **Precondition**: Phase 13 may have lifted some bboxes.
- **Postcondition**: Within each row's contiguous column group, all
  bboxes share `bbox_y` (heights extended upward as needed).
- **Invariants preserved**: All station / port Ys.

### Phase 13b: compact row content to bbox top (engine.py:672-676)
- **Purpose**: Shift each row's column-group up by the smallest
  above-content slack, then shrink bbox heights to remove the empty
  band. Preserves trunk alignment.
- **Helper**: `_compact_row_content_to_bbox_top` (engine.py:1540).
- **Precondition**: Bbox tops aligned (Phase 13a).
- **Postcondition**: Each row's contiguous column group's bbox top
  sits at `min(content_top) - section_y_padding`. Stations shift up
  by the same delta as their bbox.
- **Invariants preserved**: Inter-station relative positions inside
  each section. Trunk Y stays aligned across the row.
- **Related tests**: `test_section_bbox_has_bottom_padding`.

### Phase 13c: snap inter-section port pairs + reposition junctions (engine.py:678-686)
- **Purpose**: Snap exit/entry port pairs in the same row to a shared
  Y (the entry's), then re-run Phase 12 to put junctions back on the
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

### Phase 13d: fan free content upward (engine.py:688-693)
- **Purpose**: When the row's compaction leaves visible empty top
  band but the section has trunk-candidate sibling stations,
  fan those upward into the empty band.
- **Helper**: `_fan_free_content_upward` (engine.py:1762).
- **Precondition**: Trunk Y aligned (Phase 11ca). Compaction done
  (Phase 13b).
- **Postcondition**: Eligible sections fan stations upward by at most
  one `y_spacing` slot, balancing content above/below trunk.
- **Invariants preserved**: Trunk station Y. Off-track stations
  (sections with off-track band are skipped).
- **Related tests**: `test_section_top_band_filled`,
  `test_section1_input_above_trunk`.

### Phase 13d2: fan source inputs upward (engine.py:695-700)
- **Purpose**: Companion to 13d for source-stack sections (single
  full-bundle trunk + subset-bundle file inputs at the entry column).
  Lift trunk-nearest source inputs into the empty top band.
- **Helper**: `_fan_source_inputs_upward` (engine.py:1852).
- **Precondition**: 13d done.
- **Postcondition**: Section is top- and bottom-weighted around the
  trunk row instead of stacked below it.
- **Invariants preserved**: Trunk station Y.

### Phase 13d3: 2-branch symfan half-grid compaction (engine.py:702-710)
- **Purpose**: Sections containing exactly a 2-branch symmetric fan
  (no off-track / constraining content) collapse onto half-pitch
  offsets so the section is 1 grid-unit tall instead of 2. Marks
  stations in `_half_grid_station_ids` so Phase 13e leaves them alone.
  Gated on `center_ports`.
- **Helper**: `_apply_half_grid_2branch_symfan` (engine.py:3266).
- **Precondition**: 13d/13d2 done; symfan classification stable
  (`_section_symfan_uses_half_grid`).
- **Postcondition**: Eligible symfan pairs share half-pitch offsets
  from the trunk Y.
- **Invariants preserved**: Trunk station Y. Other sections.
- **Related tests**: `test_symfan_pairs_share_y`.

### Phase 13e: snap all Y to grid (engine.py:712-717)
- **Purpose**: Final pass snapping every station and port Y to the
  nearest row-wide grid slot, removing fractional Ys left by earlier
  shifts. Half-grid stations from 13d3 are skipped.
- **Helper**: `_snap_all_y_to_grid` (engine.py:2882).
- **Precondition**: All semantic Y shifts done.
- **Postcondition**: Every station and port Y is a grid slot of the
  per-section / per-row pitch (except marked half-grid stations).
- **Invariants preserved**: X coordinates (tested by
  `test_grid_snap_does_not_mutate_x`). Half-grid station Ys.
- **Related tests**: `test_all_stations_snap_to_grid`,
  `test_grid_snap_does_not_mutate_x`.

### Phase 13f: align TB-section bbox bottoms (engine.py:719-723)
- **Purpose**: Extend TB-section bbox bottom to match downstream
  LR/RL section's bbox bottom so the line doesn't look pinned to the
  TB bbox edge.
- **Helper**: `_align_tb_section_bbox_bottoms` (engine.py:5550).
- **Precondition**: All station/port Ys final (post-snap).
- **Postcondition**: For each TB section feeding an LR/RL target,
  `tb.bbox_y + tb.bbox_h >= target.bbox_y + target.bbox_h`.
- **Invariants preserved**: All station and port Ys. Other bboxes.

### Phase 13g: reanchor off-track to consumer (engine.py:725-732)
- **Purpose**: Re-pin each off-track input at `consumer.y - n*y_spacing`
  using the consumer's final snapped Y (Phase 13 used pre-snap Ys).
  Grow bbox upward if needed.
- **Helper**: `_reanchor_off_track_to_consumer` (engine.py:6624).
- **Precondition**: Phase 13e snapped consumers to grid.
- **Postcondition**: Off-track inputs sit `n * y_spacing` above their
  consumer's final Y.
- **Invariants preserved**: On-track station Y.
- **Related tests**: `test_off_track_inputs_above_consumer`.

### Phase 13h: re-center full-bundle columns (engine.py:734-757)
- **Purpose**: Re-fan full-bundle columns around the row's final trunk
  Y (Phase 11da used the local port Y which may now be stale).
  Re-anchors off-track inputs and re-runs row-top-align afterwards.
  Gated on `center_ports`.
- **Helper**: `_recenter_full_bundle_columns` (engine.py:3578),
  then `_reanchor_off_track_to_consumer`, then
  `_top_align_row_bboxes_only`.
- **Precondition**: Final inter-section trunk Y known (post-snap).
- **Postcondition**: Full-bundle columns are symmetric around the
  row's final trunk Y, with off-track inputs re-pinned to their
  consumers' new Ys and row bboxes still flush at the top.
- **Invariants preserved**: Bbox tops (after the row re-top-align).

### Phase 13i: align terminus to upstream (engine.py:759-763)
- **Purpose**: After 13h re-pitched fanned columns, a single-station
  downstream column (e.g. a `file` terminus) may have stayed at its
  pre-fan Y. Pin it back onto its sole upstream's Y.
- **Helper**: `_align_terminus_to_upstream` (engine.py:3860).
- **Precondition**: Phase 13h re-centered fans.
- **Postcondition**: Single-station downstream columns share Y with
  their unique upstream.
- **Invariants preserved**: Multi-station columns.
- **Related tests**: `test_terminus_not_directly_after_diagonal`.

### Phase 13i2: balance section content around trunk (engine.py:765-771)
- **Purpose**: Auto-balance pass. For sections whose final layout
  still has an empty band above the trunk while more siblings sit
  below than above, lift bottommost movable siblings into the empty
  top band. U-turn-safe and bbox-bounded.
- **Helper**: `_balance_section_content_around_trunk` (engine.py:2030).
- **Precondition**: All earlier 13-phase reshuffles done.
- **Postcondition**: Sibling count above trunk >= sibling count below
  trunk (where movable), inside bbox.
- **Invariants preserved**: Trunk station Y. Sections that already
  balance are left alone.
- **Related tests**: `test_section_top_band_filled`.

### Phase 13h3: recenter loop side stations (engine.py:773-781)
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

### Phase 13j: shrink bbox to content bottom (engine.py:783-787)
- **Purpose**: Shrink each section's bbox bottom to
  `max_content_y + section_y_padding` after earlier phases lifted
  bottom rows.
- **Helper**: `_shrink_bboxes_to_content_bottom` (engine.py:3708).
- **Precondition**: All content Ys final.
- **Postcondition**: Section bbox bottoms sit `section_y_padding`
  below the deepest content. Trunk alignment unaffected (only bottom
  shrinks).
- **Invariants preserved**: Bbox tops. Station / port / junction Ys.
- **Related tests**: `test_section_bbox_has_bottom_padding`,
  `test_section_bbox_matches_content_extent`.

### Phase 13k: tighten lower rows after shrink (engine.py:789-794)
- **Purpose**: Pull lower-row sections up to close vertical slack left
  by the pre-shrink row-height estimate when rowspan sections
  collapsed via 13j.
- **Helper**: `_tighten_lower_rows_after_shrink` (engine.py:3802).
- **Precondition**: 13j shrank some rowspan sections.
- **Postcondition**: For each row pair, the row gap is
  `section_y_gap` (no more, no less, except where rowspan sections
  filled their full row claim).
- **Invariants preserved**: Within-row trunk Ys. Bbox heights of
  upper rows.
- **Notes**: Phase tag in code shadowed - there are two "Phase 13k"
  comments (engine.py:789 and engine.py:796). Engine numbering is
  unique by helper, but the comment labels collide; **UNCLEAR
  structural debt - the two 13k entries should be renumbered.**

### Phase 13k (second, sparse loop shift) (engine.py:796-800)
- **Purpose**: Shift sparse loop-side stations (one inbound, one
  outbound, single-line consumer) onto a half-pitch Y when sharing
  the full-row Y with a busier sibling whose inbound bundle would
  otherwise breeze-past the sparse station's marker.
- **Helper**: `_shift_sparse_loop_stations_to_clear_bundle`
  (engine.py:2563).
- **Precondition**: Bundle Ys final.
- **Postcondition**: Sparse single-line loop stations whose row Y
  conflicts with a busier sibling's bundle move to a half-pitch
  offset. May grow bbox downward.
- **Invariants preserved**: Busy sibling Y. Bundle Y.
- **Related tests**: `test_lines_dont_cross_non_consumer_markers`,
  `test_no_icon_overlaps_line_path`.
- **Notes**: Shares the "13k" tag with the lower-row tighten phase
  above. **UNCLEAR / collision.**

### Phase 13l: push lower rows after bbox grow (engine.py:802-806)
- **Purpose**: Companion to 13k - when 13k grew a section's bbox
  downward, push lower-row sections down to keep
  `section_y_gap`.
- **Helper**: `_push_lower_rows_after_bbox_grow` (engine.py:2698).
- **Precondition**: Phase 13k may have grown some bboxes.
- **Postcondition**: Row gaps preserved across the bbox grow.
- **Invariants preserved**: Within-row Ys.
- **Related tests**: `test_row_gap_accommodates_bypass`.

### Phase 13m: pad stacked captioned file icons (engine.py:808-813)
- **Purpose**: Pad vertical spacing between stacked file-input icons
  whose under-icon captions would overlap the icon below at default
  `y_spacing`.
- **Helper**: `_pad_stacked_captioned_file_icons` (engine.py:6696).
- **Precondition**: All other Y phases done.
- **Postcondition**: Stacked captioned-icon columns have at least
  `_required_captioned_icon_pitch(y_spacing)` between centres.
- **Invariants preserved**: Non-captioned-icon Ys.
- **Related tests**: `test_stacked_file_icons_label_clearance`,
  `test_auto_y_spacing_fits_content`.

## Unclear / structural-debt signals

The following observations came up while writing this doc. Each one is
a candidate for a follow-up cleanup PR.

1. **Two "Phase 13k" comments** (engine.py:789 and engine.py:796) refer
   to entirely different helpers. The phase numbering scheme has
   collided and should be renumbered.
2. **Phase 13d3 has more conditional logic than the rest** - it's
   gated on `center_ports` and tracks state on
   `_half_grid_station_ids` to coordinate with Phase 13e. That
   cross-phase coupling is hard to follow; the half-grid marker
   pattern would benefit from a dedicated discussion in the doc /
   code.
3. **Pass B is described as "single pass"** in the function docstring,
   but Phase 11 expands bboxes (`_expand_bbox_for_y`) and Phase 11c
   immediately re-runs top-align to undo the resulting bbox-top
   drift. Naming-wise, "single pass" is misleading.
4. **Phase 13h triggers further phases** (`_reanchor_off_track_to_consumer`
   and `_top_align_row_bboxes_only`) inside its `if center_ports:`
   block. These are effectively unnumbered sub-phases hiding in
   a conditional - they don't appear as `# Phase 13h.something`
   comments but are real graph-mutating passes.
5. **Phase 13 (off-track lift) calls `_shift_graph_into_canvas`**,
   which globally translates every station / port / junction / bbox.
   That's a Phase 4-style transformation hiding inside a Phase 13
   helper; if any later phase assumed Phase 4's global-coord origin
   was stable, the assumption is wrong.
6. **`compute_layout(validate=True)` runs `_guard_no_station_overlap`
   and `_guard_no_line_crosses_non_consumer` only at "after Phase 12
   (final)" - i.e. at the *end* of layout.** Many of the 13-suffixed
   phases exist precisely to fix overlap or breeze-past issues. A
   binary search bisecting which phase first introduces overlap is
   currently a manual exercise.
7. **Phase 11d/11da symmetrically fan content using a stale port Y**;
   Phase 13h re-centers using the final trunk Y. The two-pass pattern
   is necessary because Phase 11da runs before snap-to-grid, but it
   means the early pass's output is partially-discarded work.

## Adding a new phase: checklist

When adding a new phase to `_compute_section_layout`, document the
following before merging:

1. **Phase tag**: pick a new tag matching the position in the pipeline.
   Avoid suffix collisions (see structural debt #1 above).
2. **Helper location**: top-level function in `engine.py` (or a new
   module if it's substantial). Phase comments in the function body
   must reference the helper.
3. **Precondition**: what state on the graph the helper assumes.
   Mention coordinate-system regime (local vs global), whether ports
   are positioned, whether junctions are positioned, and whether
   trunks/grids are final.
4. **Postcondition**: the property the phase guarantees. Be concrete -
   "Y values are snapped to the row grid" not "Y values look nice".
5. **Invariants preserved**: what the phase does NOT change. Crucial
   for reasoning about reorder safety. Bboxes? Other sections?
   Off-track stations? Half-grid marker set?
6. **Related tests**: which invariants in `tests/test_layout_invariants.py`
   defend the postcondition. If none, add one - phases without test
   coverage are how the 13-suffix sprawl happened.
7. **Validate-mode coverage**: if the phase introduces a new property
   that should hold permanently, add a `_guard_*` helper and call it
   from `validate=True` mode.
8. **Update this doc**: extend the per-phase table above and call out
   any cross-phase coupling in the structural-debt section.
