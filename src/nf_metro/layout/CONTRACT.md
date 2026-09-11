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
- **Lifecycle** classifies the stage by one objective question: does the
  property it establishes still hold at the *final* layout boundary?
  - **invariant** - it does. The one-line final-boundary property is
    given. (Some invariants are re-asserted by later re-runs of the same
    helper; re-assertion *maintains* the invariant, it does not negate
    it.)
  - **transient** - a later stage deliberately overrides it, so the stage
    has no final-boundary property to declare. The superseding stage is
    named.

  The distinction is about the *property*, not the coordinates. A stage
  stays **invariant** when a later stage recomputes the exact coordinates
  but the abstract property it established (no-kink flow, horizontal port
  connection, filled top band, grid-snapped Y) still holds at the end -
  that is maintenance. It is **transient** only when a later stage
  discards the decision itself, replacing the property with a different
  layout (flush row tops giving way to content-hugging tops; an early fan
  re-fanned around the final trunk Y). The test in doubt: does the
  property survive to the end, or is the decision overwritten?

  Lifecycle answers "what does this phase guarantee at the end" and is
  pinned by `tests/test_contract_lifecycle.py`. It is **orthogonal** to
  the question #365 explored: "is this invariant safe to *lift* into a
  declarative run-anytime `maintain()` registry?" Liftability requires
  the invariant *plus* idempotency, order-independence, and no (or a
  gated) precondition - properties the lifting work (#463, #464)
  establishes empirically. So an inline `liftable:` qualifier appears
  **only** where liftability is non-trivially anything other than "yes";
  its absence is not a promise of liftability.

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

## Axis vocabulary (TB policy)

TB sections run the identical LR machinery and swap axes only at coordinate
assignment (`single_section.py`). A heuristic written against the LR
*interpretation* of `x`/`y` (horizontal trunks, layers spread along X, lines
stacked along Y) is wrong by default for TB.

The sanctioned alternative is the `AxisFrame` primitive in `geometry.py`:
`AxisFrame.for_direction(direction, x_spacing, y_spacing)` returns the
**primary** axis (the layer/flow axis: X for LR/RL, Y for TB) and the
**secondary** axis (the track axis: Y for LR/RL, X for TB), each carrying its
`step` and `get`/`set` accessors, plus `primary_sign` (`-1` for RL, which runs
the LR primary axis reversed). A heuristic expressed against primary/secondary
instead of raw `x`/`y` has a TB path that is *the same code* as its LR path, so
it needs no branch.

**Lane sign (`secondary_sign`).** A transpose is a reflection: it flips
chirality, so a TB path written as an axis swap diverges from LR in behaviour,
not just orientation. The cure is a true 90-degree rotation, which a transpose
is not. `AxisFrame.secondary_sign` carries the lane fan direction: a 90-degree-CW
rotation maps LR's screen-down lane (`+Y`) to screen-left (`-X`), so TB is `-1`;
LR/RL are `+1` (RL reverses only the primary) and BT is `+1`, the flow-axis
reflection of TB (`primary_sign = -1`, `secondary_sign = +1`) so an upward flow
fans its lanes to the `+X` side. The **sanctioned offset->coordinate path** applies
this sign at the *draw accessor*, never to a stored offset, which stays positive:

- `geometry.station_lane_coord(frame, station, offset)` -> `station.y + offset`
  (LR), `station.x - offset` (TB): the screen coordinate of a positive lane
  offset from a station.
- `geometry.lane_delta(frame, offset)` -> `secondary_sign * offset`: the signed
  secondary-axis displacement for a positive offset, station-free.
- `geometry.lane_delta_to_normal_offset(lane_delta, travel)` bridges a lane delta
  to the bundle builder's right-normal offset (`routing.bundle._right_normal`),
  the sole point where the lane-sign and builder-normal conventions meet. The
  builder itself fans purely geometrically along `_right_normal` of travel and is
  not per-axis; rotation lives *above* it, in this offset->coordinate mapping.

`secondary_sign` governs the offsets of lines inside a station or bundle. Fan
station tracks use a separate plan-owned appearance sign: tracks progress along
the positive secondary axis for LR, RL, TB, and BT, then mirror when a feeder
arrives from the positive end of that axis. This keeps the hub on the nearest
track without changing bundle chirality. Symmetric fans remain centred; the sign
only determines their branch order on screen.

**Policy:** no new one-off TB branches. A heuristic that needs TB awareness is
the trigger to convert it to the axis vocabulary, not to add another branch.
This is machine-enforced by `tests/test_tb_branch_ratchet.py`, which counts
`"TB"` literals / `.TB` attribute accesses across the layout package and fails
CI if the total rises above its baseline (mirroring the corner-radius and
gate-coverage ratchets). Migrating a heuristic onto `AxisFrame` removes its
branch and lowers the count; lower the baseline in the same change to lock it in.

**Row / lane membership** is the inter-section corollary. The row-level passes
align the **Y (lane) axis**: row grouping, row trunk-Y alignment, the shared
row Y-grid, top-aligning row-mates. A horizontal-flow (LR/RL) section stacks
its lines along Y, so it is a first-class member of that machinery; a
vertical-flow (TB/BT) section stacks lines along X and shares no row Y-grid, so
those passes leave its Y alone. The predicate for this is
`geometry.lanes_run_along_y(direction)` (built on `AxisFrame.axes_for_direction`,
which names a section's axes without needing spacings). The predicate governs
`row_align.py`, `grid_snap.py`, `_common._section_trunk_y`, and
`section_placement.py`, and underlies `_common._is_fold_section`
(`grid_row_span > 1 or not lanes_run_along_y(...)`), the row-fold predicate that
routes a section's exit ports through the fold path rather than the row passes.

**Direction-specific helpers.** A *single-branch* TB-only heuristic with no LR
mirror gains no polymorphism from `AxisFrame`; expressing its reads as
`frame.primary`/`frame.secondary` would just rename `.x`/`.y` inside code that
only ever runs for one direction. These stay direct in `phases/ports.py`:
`_align_tb_entry_port` (its TB-trunk branch; the function also serves the LR/RL
perpendicular case), `_clamp_tb_entry_port`, `_resolve_tb_exit_y`,
`_align_tb_section_bbox_bottoms`, and `_tb_trunk_x` (the secondary-axis trunk
coordinate is a *median* for a vertical section but the bundle-connected topmost
for a horizontal one - `_section_trunk_y` - so the two are not the same code and
should not be forced behind one name). The `_apply_tb_fold_spans` selection is a
domain grouping, not an axis swap, and likewise stays.

## Validate-mode guards

`compute_layout(validate=True)` runs these guards at fixed checkpoints:

| Checkpoint | Guards |
|---|---|
| after Stage 1.1 | `_guard_section_bboxes_positive` |
| after Stage 2.1 | finite coords, stations-in-sections, bboxes-positive |
| after Stage 3.1 | ports-on-boundaries |
| after exit-port align + row re-flush and the X-axis perp-port inset (Stages 3.4 to 3.5) | ports-on-boundaries |
| after each Pass C sub-stage (bisection) | finite coords, bboxes-positive, ports-on-boundaries, station-x-column-drift, plus three phase-gated guards (see below) |
| after final | bisection set (all unconditional) + off-track-above-anchor, row-trunk-cy-consistent, inter-section-routes-in-row-band |

Bisection checkpoints fire after every Pass C sub-stage (see the
`# Stage 5.2:` through `# Stage 6.16:` comments in
`_compute_section_layout`). Three guards
hold continuously only from a specific checkpoint onward, and the
bisection runner skips them earlier; see `_BISECTION_FIRST_VALID` in
`engine.py` for the threshold table:

| Guard | First valid checkpoint | Transient because |
|---|---|---|
| `_guard_stations_in_sections` | after Stage 5.3 | Stage 5.2's off-track lift moves stations above the section bbox; Stage 5.3 grows the bbox to enclose them. |
| `_guard_no_station_overlap` | after Stage 6.4 | Pre-snap fan placement can sit a fraction of a pitch off the row grid; Stage 6.4's snap pulls every station onto the grid while keeping same-column stations on distinct slots, after which markers must be collision-free. |
| `_guard_no_line_crosses_non_consumer` | after Stage 6.14 | A sparse loop-side station sits on the trunk Y until Stage 6.14 shifts it to a half-grid offset; before that, sibling line bundles pass through its marker bbox. |

Three further guards are excluded from the bisection set entirely
(meaningful only at the final boundary); the `_run_pass_c_guards`
docstring in `engine.py` is the authoritative list.

Guard bodies live in `phases/guards.py` and are imported into `engine.py`;
the bisection runner is `_run_pass_c_guards`.

The routes a bisection checkpoint inspects come from
`route_edges_for_placement_guards` (`routing/core.py`), which routes with
`validate_final_route_frames=False`; the final checkpoint uses `route_edges`,
with frames armed. The difference is a precondition, not a weaker invariant.
Fan and convergence endpoint frames are not final until Stage 6.16, so a
frame assertion at an earlier checkpoint evaluates a check whose inputs the
pipeline has not finished writing. Forcing frames on at every checkpoint
raises 32 violations across 3 fixtures at mid-Pass-C placements, every one of
which clears by the final placement, and the final route validates clean with
frames armed on every fixture. That count is a function of how many systems
make ownership claims, so a tree that plans more systems shows more mid-pass
disagreement without its geometry being worse; it is not a quality metric.

## Anchor invariant

The **anchors** of a section are its port stations: synthetic points on the
section boundary where the inter-section line bundle crosses. A port anchors
the trunk on whichever axis its side dictates - LEFT/RIGHT (LR/RL) ports fix
the Y at which the bundle runs horizontally, TOP/BOTTOM (TB/BT) ports fix the
X at which it runs vertically - and a port's cross-axis (an LR port's X, a TB
port's Y) is likewise pinned to the section boundary by port positioning.
Anchors are set only by structural phases - port positioning along the section
DAG (align/snap entry/exit ports, inter-section port-pair snap), the row trunk
alignment (4.8), grid snapping, the inter-row cascade (6.13/6.14 phase 2) and
uniform canvas/row translation.

The **content-placement** phases - fan-out / full-bundle redistribution (4.9,
4.10), band-fill (6.1, 6.2), the 2-branch symfan half-grid (6.3), full-bundle
recenter (6.7), balance-around-trunk (6.11) and loop-side recenter (6.12) -
position content *around* the resolved anchors and must never move one. Each
runs through the `_run_placement` wrapper in `_compute_section_layout`, which
under `validate=True` calls `_guard_anchors_frozen_during_placement` to assert
that no port's `(x, y)` changed across the phase. The snapshot
(`_port_anchor_snapshot`) covers **every port on every side, on both axes** -
not just the LR/RL-Y subset - so the guard catches any anchor movement
regardless of port side or axis (a phase that nudged a TOP/BOTTOM port, or an
LR port's X, would be caught too). This separation (structural anchors vs.
dependent placement) is what makes the layout forward-resolvable: content is a
function of the frozen anchors, not the reverse.

### Content-placement purity

`_guard_anchors_frozen_during_placement` only forbids a content phase from
*moving* an anchor. A stronger property holds and is machine-checked separately:
every content-placement phase is a **pure function of (frozen anchors +
structure)**. The Y it assigns to the stations it governs depends only on the
frozen port anchors and the section structure (tracks, edges, columns), never on
the mutable intermediate state earlier phases happen to have left behind
(current station Y, section `bbox` geometry). This is strictly stronger than the
idempotence locked by `tests/test_content_placement_idempotent.py` (#488):
purity means re-running, re-ordering, *or perturbing the non-anchor state*
cannot change a phase's output. `tests/test_content_placement_pure.py` (#491)
is the guard - it perturbs the non-anchor state before each phase and asserts
the governed stations land identically, the test-time counterpart to the
anchor-frozen guard.

The phases that genuinely need an intermediate quantity - the empty-band slack
in 6.1 / 6.2, the balance arrangement in 6.11 - read it from a frozen *placement
reference* (`_snapshot_placement_refs` populates `graph._placement_ref_y` /
`_placement_ref_bbox_top`; phases read it via `_ref_y` / `_ref_bbox_top`)
captured once right before the consumer, rather than from live geometry. The
reference equals the live geometry at capture time. Planned fan materialisation
uses the same boundary pattern without a graph-state channel:
`_snapshot_planned_fan_centrelines` captures a read-only centreline mapping
after structural settlement and passes it into `_apply_planned_fan_geometry`.
These frozen inputs preserve the established render while keeping placement
independent of mutable station and bbox geometry.

## Inter-phase state protocol

Some stages hand intermediate results to later stages through private
`graph._*` fields rather than through station coordinates. These channels are
declared as data in [`phase_state.py`](phase_state.py) (`PHASE_FIELD_REGISTRY`),
which records each field's writer stage, its reader stages, and why it exists;
`tests/test_phase_state_registry.py` keeps that registry in sync with the
dataclass fields, the engine stage list, and this document.

Fields whose reader genuinely depends on the writer having run call
`require_phase_field` just before the read, which raises `PhaseInvariantError`
under `validate=True` when the writer stage has not completed in the current
pass:

- `graph._row_y_grid_info` - written by Stage 1.2 (`_align_row_y_grids`); read
  by the grid-group port snap (Stage 4.2-4.4), fan re-centre (6.3/6.7), and
  grid snap (6.4).
- `graph.half_grid_station_ids` - written by Stage 6.3 (`center_ports` only),
  Stage 6.4's own midpoint restore and Stage 6.17
  (`diamond_style='symmetric'`); read by the Stage 6.4 grid snap, which must
  skip these half-pitch stations. Stage 6.18 both reads the set and clears the
  marking off any station it seats back on a full row, so the post-layout
  readers (the straddle guard, the co-fanned drop-clearance rule in
  `routing/intra_handlers.py`) see only stations still at half pitch. A mark
  that a settled layout alone can make goes to
  `graph.post_layout_half_grid_station_ids` instead, so no stage reading this
  channel can be handed a value that did not exist while it ran.
- `graph.symfan_trunk_station_ids` - written by Stage 6.3 (`center_ports` only);
  read by the Stage 6.4 grid snap, which must skip these source/trunk stations
  so they stay on the symfan's local frame instead of snapping to a rowspan
  neighbour's fractional row-grid origin.
- `graph._partial_trunk_descents` - `{section_id: PartialTrunkDescent}` (the two
  endpoint ports and the descent) written by Stage 4.8 (`_align_row_trunk_ys`),
  reassigned in full each pass (empty when nothing descends); read by the Stage
  6.4 grid snap, which re-seats each partial from the carrier port's post-snap Y,
  and by the `_guard_partial_trunk_descent_seated` postcondition.
- `graph._consumers_grid_snapped` - set right after the Stage 6.4 snap; the
  Stage 6.6 off-track reanchor carries its own always-on guard on it.

The remaining channels tolerate an unwritten value by design (their read sites
fall back to live geometry or a `None`/empty default), so they are documented in
the registry but carry no runtime check: `graph._struct_height_below_top`
(snapshotted after 6.15a and retaken after a 6.18a planned-fan refit, read by
the 6.13 cascade), `graph._placement_ref_y` /
`graph._placement_ref_bbox_top` (frozen before 6.1/6.11, read via `_ref_y` /
`_ref_bbox_top`), `graph._base_y_spacing` (recorded before the spread loop
when `y_spacing` is auto-resolved), `graph._resolved_x_spacing` (the
resolved column pitch, refreshed on every spread-loop pass so it tracks the
widened `x_spacing` that pass uses, read as the cross-axis off-track step for
vertical-flow sections), `graph._resolved_y_spacing` (the row
pitch the spacing search settled on, recorded post-layout once that search
finishes, after the stage pipeline and the spread loop, and read by no layout
stage), and `graph.post_layout_half_grid_station_ids` (the half-pitch spine
branches of a station-rooted reconvergence fan, recorded once every layout and
re-layout pass has settled; read by no layout stage, and by the grid-alignment
invariants as a union with `half_grid_station_ids`).

A further group crosses a subsystem boundary rather than two numbered stages,
so their `PhaseFieldSpec` names a lifecycle phase (`pre-layout`, `post-layout`,
`station-offset-layout`, `rail-layout`) in place of a stage id. They carry no
runtime check either:

- `graph._cross_column_perp_bridges` - sections whose perpendicular drop was
  bridged across grid columns, accumulated by the Stage 3.2 / 3.4 port
  alignment; routing's render-curve invariant reads it to relax its abort to a
  warning for those bundles.
- `graph._fold_compressed_sections` - recorded at parse time for sections a
  lowered fold threshold relocated; read by the fold-exit-side guard and the
  render fold-abort chokepoint. A resolve-time flow reversal is recorded as a
  `FLOW_REORIENTED_DIRECTION` decision in `graph.layout_provenance`; routing's
  exit-port offset reads that typed reason instead of a second section set.
- `graph._linear_entry_pill_lines_cache` - accepted linear-entry cohorts
  projected by each station-offset computation. Marker bbox, label, and render
  consumers use the cohort with the offset map produced by that computation;
  the empty default means no entry frame owns marker geometry.
- `graph._rail_y` - the per-section `{line_id: rail_y}` map produced by the
  opt-in rail-mode layout; read by the rail router, label placement, and rail
  guards, empty when rail mode is off.
- `graph._defer_final_guards` / `graph._after_final_deferred` - pass-control
  flags `compute_layout` uses so the final-geometry guards defer while the
  pre-bypass passes run, then validate the settled post-bypass geometry once.
- `graph._final_route_guards_deferred` - set in `phases/guards.py` when the
  observed route plan carries boundary clearance requirements or reservations,
  so the routes the final route guards would judge are not settled yet.
  `compute_layout(validate=True)` then builds the settled render plan itself and
  re-raises a curve, fan, convergence, exit-turn, or section-header failure as
  `SettledRouteValidationError`, clearing the flag either way. `render/svg.py`
  reads the same flag, passing `include_deferred_final` and forcing strict
  judgement so those guards run on the geometry the renderer is handed.

## Stage overview

The pipeline groups into six stages aligned with the coord-regime
transitions and the Pass A / Pass B / Pass C divisions used throughout
this doc.  See [`docs/dev/layout_pipeline.mdx`](../../../docs/dev/layout_pipeline.mdx)
for a prose walkthrough of each stage; the matching
`# ---- Stage N - ... ----` comment dividers in `_compute_section_layout`
mark each stage's start in the source.  Stage-table entries below appear
in pipeline order.

## Stage table

### Stage 1.1: internal section layout
- **Purpose**: Lay out each section's real stations in section-local
  coordinates via layer/track assignment.
- **Helper**: `_layout_single_section` (`phases/single_section.py`).
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
- **Lifecycle:** invariant - each station's layer/track and
  section-local relative layout persist to the end; Stage 2.1 only
  translates them into global coordinates, it does not re-lay them out.

### Stage 1.2: align row Y grids
- **Purpose**: Snap station Ys to a shared row-wide grid so same-row
  same-direction sections agree on grid pitch and slot count.
- **Helper**: `_align_row_y_grids` (`phases/row_align.py`).
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
- **Lifecycle:** invariant - the shared per-row Y grid holds at the
  final boundary (re-asserted by Stage 6.4's grid snap).

### Stage 1.3: section placement
- **Purpose**: Place sections on the canvas grid via topological
  layering of the section DAG, then preserve the reversed lane order delivered
  by a stacked same-side half-turn, on either side, through a split consumer.
- **Helpers**: `place_sections` and
  `_reflect_stacked_split_consumer_tracks` in `section_placement.py`.
- **Precondition**: Sections have bboxes from Stage 1.1 and grid
  positions from `auto_layout`. Still all local-coord.
- **Postcondition**: Every section has `offset_x`, `offset_y` set such
  that `(local + offset)` lands sections on a non-overlapping grid. A direct
  stacked same-side seam feeding distinct branches - LEFT-to-LEFT or
  RIGHT-to-RIGHT - has those branch tracks reflected within the same local Y
  extent, so the seam and consumer agree on bundle order.
- **Column seating**: A column is seated on the edge its members' box
  extents grow away from (`box_growth_sign`, `layout/geometry.py`): the left
  one by default, the right one when a member's extent grows leftward (its
  flow runs that way, or its lanes fan that way). A right-seated member's
  slack is its column's width minus its own `_effective_section_width`, the
  same measure the column width was reserved from, so the column's boxes
  land on one X.
- **Disconnected graphs**: When the section meta-graph has 2+
  weakly-connected components and the author pinned no explicit
  `%%metro grid:` positions, each component is placed in its own local
  column grid (so a wide component never inflates another's columns)
  and the components are stacked vertically in a deterministic order
  (ascending min original row, then descending size, then smallest
  section id), left-aligned and separated by `section_y_gap`. Any
  explicit grid override falls back to the shared single-grid path.
- **Invariants preserved**: Station local coords unchanged. Bboxes
  still local-coord.
- **Runtime guard**: `_guard_independent_components_disjoint` (under
  `validate=True`) asserts stacked components occupy disjoint vertical
  bands.
- **Lifecycle:** invariant - the section grid (column/row placement,
  non-overlap) holds at the final boundary.

### Stage 1.4: renumber sections
- **Purpose**: Renumber sections by connected route continuity, using visual
  lanes to choose between alternative continuations.
- **Helper**: `_renumber_sections_by_route` (`phases/canvas.py`).
- **Precondition**: Section grid positions and directions finalised.
- **Postcondition**: Each disconnected flow is numbered completely before the
  next. The nearest connected section on the current lane is preferred;
  parallel branch starts remain together; joins wait for aligned or independent
  predecessor routes. A secondary cross-row route may rejoin a section already
  numbered on a dominant row. Authored numbers are preserved, and automatic
  sections take the lowest unused positive numbers.
- **Invariants preserved**: Section IDs, station coords, bboxes,
  edges. Pure metadata pass.
- **Related tests**: `tests/test_section_numbering.py`.
- **Lifecycle:** invariant - `number` metadata is final
  (cosmetic, never recomputed).

### Stage 1.5: offset overshoot correction
- **Purpose**: Grow `x_offset`/`y_offset` when section local extents
  reach left/above the canvas origin, so global coords stay positive
  after Stage 2.1.
- **Helper**: inline.
- **Precondition**: Section `offset_x/y` and local `bbox_x/y` set.
- **Postcondition**: For every laid-out section, `offset_{x,y} +
  bbox_{x,y} + {x,y}_offset >= section_{x,y}_padding`.
- **Invariants preserved**: Section bboxes (local), station local
  coords, grid layout.
- **Lifecycle:** invariant - positive in-canvas coordinates hold at the
  end (the canvas top margin is maintained by Stage 6.15 /
  `_shift_graph_into_canvas`).

### Stage 2.1: local-to-global translation
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
- **Lifecycle:** invariant - the global-coordinate regime is permanent;
  every later stage works in global coordinates.

### Stage 3.1: position ports on section boundaries
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
- **Lifecycle:** invariant - ports sit on their bbox edges at the final
  boundary (guarded continuously by `_guard_ports_on_boundaries`).

### Stage 3.2: align LR entry ports
- **Purpose**: For LEFT/RIGHT entry ports, set Y to the incoming
  source's Y so the inter-section horizontal run is straight; for
  TOP/BOTTOM entry ports, set X / Y accordingly.
- **Helper**: `_align_entry_ports` (`phases/ports.py`), dispatching to
  `_align_lr_entry_port` and `_align_tb_entry_port`.
- **Precondition**: Stage 3.1 placed ports on bbox edges. Junction
  positions are unknown - the helper uses `_resolve_source_xy` to
  derive junction coords on-the-fly.
- **Postcondition**: Each entry port's coordinate on the axis along
  its bbox edge matches the coordinate its source's inter-section run
  reaches the port on (within the section's bbox extent): the source's
  own for a source that leaves along the run's axis, and one turn's
  runway out from it for a perpendicular entry fed by a LEFT/RIGHT
  exit, which turns onto its descent column only once clear of the box
  (`_feeder_descent_x`).
- **Invariants preserved**: Real station coords (Pass-A is port- and
  bbox-only). Exit ports. Junctions still unpositioned.
- **Related tests**: `test_no_kink_at_section_boundary` (the
  straight-run property this phase establishes).
- **Lifecycle:** invariant - the entry-port straight-run (no-kink) Y
  holds at the end (re-asserted by Stages 5.5 / 6.16).

### Stage 3.3: shift LR/RL perp-entry internal stations
- **Purpose**: When an LR/RL section has a TOP or BOTTOM (perpendicular)
  entry port, shift internal stations' X so the entry port has
  in-section runway before stations begin.
- **Helper**: `_shift_lr_perp_entry_stations` (`phases/single_section.py`).
- **Precondition**: Stage 3.2 finalised LR/RL entry-port X for perp
  entries.
- **Postcondition**: Internal stations in such sections sit at least
  `ENTRY_SHIFT_LR * x_spacing` away from the perp entry port X, and the
  section's own bbox still contains the shifted run: a drop inside the
  run's span shifts the run further than `_adjust_lr_entry_inset`
  reserved, so the trailing edge grows by the uncovered remainder.
- **Invariants preserved**: Station Y, ports (the flow-axis exit ports
  re-pin to a moved edge), bboxes (X shift is bbox-bounded).
- **Related tests**: `test_terminus_not_directly_after_diagonal`,
  `test_no_kink_at_section_boundary` (entry-side geometry),
  `test_lr_perp_port_pair_1539.py::test_run_stays_inside_its_own_box`.
- **Lifecycle:** invariant - the perpendicular-entry runway
  (internal-station X clearance) holds at the final boundary.

### Stage 3.4: align fold-section exit ports
- **Purpose**: For row-spanning (fold) and TB-direction sections,
  shift LEFT/RIGHT exit ports to the target section's entry Y. May
  push the target section down via `_resolve_tb_exit_y`; the move then
  re-flushes the tops of the rows it pushed so it cleans up after
  itself rather than leaving the correction to a separate stage.
  Also seats every single-row LR/RL section's TOP/BOTTOM exit past its
  trailing station (`_align_perpendicular_exit_port`), whether or not
  the section also carries a flow-aligned port: an exit left on its
  feeder's own X collapses the turn to a zero-length corner that the
  lane fan then splays the wrong way round.
- **Helper**: `_align_exit_ports` (`phases/ports.py`), dispatching to
  `_align_lr_exit_port` and finishing with a `_top_align_row_sections`
  (`phases/row_align.py`) call scoped to the pushed rows.
- **Precondition**: Entry ports aligned (Stage 3.2); target sections
  positioned (Stage 1.3/4).
- **Postcondition**: Exit ports on fold/TB sections sit at the same Y
  as their target section's entry port (within section bbox extent);
  same-row contiguous-column sections whose top the exit move disturbed
  share `bbox_y` again (station/port Ys shift by the same delta,
  preserving Stage 3.2 alignment). The row re-flush is a transient
  intermediate property, not a final guarantee: Stage 6.15a later grows
  a fanned section's bbox top above the flush line, so finished same-row
  tops are not guaranteed flush (measured ~40px non-flush on
  `terminal_symmetric_fan` / `trunk_through_fan`; see Stage 4.7, which
  re-flushes and carries the same transient tag).
- **Invariants preserved**: Real station coords. Entry-port Ys.
- **Validate guard after**: `_guard_ports_on_boundaries` (the row
  re-flush preserves port-on-edge by shifting ports with stations).
- **Related tests**: `test_no_kink_at_section_boundary`,
  `test_inter_section_route_y_stays_within_row_band`,
  `test_align_exit_ports_reflushes_disturbed_rows`.
- **Lifecycle:** invariant - the fold/TB exit-port no-kink Y holds at
  the end (re-asserted by Stage 5.5).

### Stage 3.5: reserve the perpendicular-port edge inset on X
- **Purpose**: Grow a horizontal-flow (LR/RL) section's left and right
  bbox edges so each TOP/BOTTOM port keeps `PERP_PORT_EDGE_INSET` from
  them, the X-axis rotation of the inset the Y sizing keeps for a
  vertical flow's LEFT/RIGHT ports. X sizing measures real stations
  only, so a port seated past the trailing station (Stage 3.4) or
  dragged onto a drop column lands inside the padding band with nothing
  to push the edge out. Each such port owes its facing edges the inset on
  its own; the two edges are not levelled against each other, because an
  edge already held further out by content or a routing band is not the
  port's doing.
- **Helper**: `_reserve_perp_port_edge_inset` (`phases/bbox.py`),
  followed by `reenforce_column_gaps` (`section_placement.py`) when any
  box grew.
- **Precondition**: Perpendicular port X settled (Stages 3.2 to 3.4 are
  the last to move one relative to its own box).
- **Postcondition**: No LR/RL TOP/BOTTOM port's outermost drawn lane sits
  within `PERP_PORT_EDGE_INSET` of its section's left or right edge
  (`port_bundle_edge_reach`, as on the Y axis); adjacent columns still keep
  `MIN_INTER_SECTION_GAP`.
- **Invariants preserved**: Station coords; every port's own edge
  anchoring (LEFT/RIGHT ports move with the edge they are pinned to).
- **Validate guard after**: `_guard_ports_on_boundaries`.
- **Related tests**:
  `test_perp_port_edge_clearance_1494.py::test_horizontal_perp_ports_keep_the_designed_inset`.
- **Lifecycle:** invariant - the inset holds at the final boundary.

### Stage 3.6: level a grid column's shared-runway X edges
- **Purpose**: Give column mates that start their content at one X a
  common bbox edge on that side, so the runway between that edge and the
  shared content column is the same width in each. The X half of the same
  levelling primitive the row top-align uses (Stages 5.3 / 6.9), narrowed:
  a grid row's sections share a trunk Y, so their tops are always
  comparable, whereas a grid column's sections share no trunk X, and
  levelling boxes whose content starts at different X moves an edge
  without moving anything a viewer reads. Both X edges are levelled,
  because unlike the box top - which carries the header badge, and so is
  privileged by text a rotation does not carry with it - neither X edge is
  the one a column must agree on.
- **Helper**: `_level_column_anchor_edges` (`phases/bbox.py`), grouping
  via `_column_contiguous_row_groups` (`phases/_common.py`) then
  `_shared_anchor_runway_runs` (`phases/bbox.py`), levelling each run
  through `level_group_anchor_edges` (`phases/bbox.py`, shared with the
  row top-align).
- **Precondition**: Every X-axis box mover has run - Stage 1.1 sizing,
  the Stage 1.3 column seating, the Stage 3.3 perp-entry runway grow, the
  Stage 3.5 perp inset - so the levelled edge is not re-broken by a later
  widen.
- **Postcondition**: For each X side, within each maximal run of adjacent
  grid rows in one column whose sections' content stations nearest that
  side share an X, every section shares the run's outermost edge on that
  side, except one held short by a neighbour overlapping its own row band
  (which keeps `MIN_INTER_SECTION_GAP` of inter-column corridor). Members
  of a packed cell are out of scope: they sit side-by-side along X inside
  one cell, so no common vertical edge exists. Two kinds of section break a
  run: a rail-flagged one, because `_retrofit_section_rails_phase`
  re-derives its interior from its bbox, so growing its edge would slide
  its stations rather than widen a runway in front of them; and one whose
  exit port rides the edge under test, because that port's coordinate is
  where the inter-section route leaves and the clearances downstream of it
  are measured from there.
- **Invariants preserved**: Station coords (only `bbox_x` / `bbox_w`
  move); the opposite edge of the pair being levelled; every port's own
  edge anchoring (LEFT/RIGHT ports move with the edge they are pinned
  to). Because a run's members share a content X on the side being
  levelled, growing each to the run's outermost edge on that side can only
  raise a narrower runway to the widest already present in the run, so the
  spread of runway widths within a grid column never grows.
- **Validate guard after**: `_guard_ports_on_boundaries`.
- **Related tests**: `test_grid_column_anchor_edge.py`.
- **Lifecycle:** invariant - the levelled edge holds at the final
  boundary for the sections the stage moved
  (`_guard_column_run_shares_its_anchored_edge`).

### Stage 4.1: align ports to downstream
- **Purpose**: For non-fold LR/RL sections, pull exit-entry port
  pairs toward the downstream section's internal stations so lines
  flow without detour.
- **Helper**: `_align_ports_to_downstream` (`phases/ports.py`).
- **Precondition**: Section geometry final (Pass A complete).
- **Postcondition**: Each non-fold LR/RL exit-entry pair Y sits near
  the downstream section's connected station Y.
- **Invariants preserved**: Section bboxes (movement is bbox-bounded,
  Stage 4.6/c recompute bboxes where needed). Real stations.
- **Related tests**: `test_no_kink_at_section_boundary`.
- **Lifecycle:** invariant - exit/entry pairs flow to the downstream
  section (no-kink) at the final boundary (refined, not undone, by Stage
  5.5).

### Stage 4.2: snap sole-layer stations to ports
- **Purpose**: When a port-connected station is the only occupant of
  its layer, snap it to the port Y so the connection is horizontal.
- **Helper**: `_snap_sole_layer_stations_to_ports` (`phases/ports.py`).
- **Precondition**: Stage 4.1 settled port Ys.
- **Postcondition**: Sole-layer port-connected stations share Y with
  their port. Multi-station layers are skipped (would risk collision).
- **Invariants preserved**: Multi-station layer Ys. Shared row-Y grid
  is not respected here (Stage 6.4 re-snaps).
- **Related tests**: `test_section_entry_hub_on_grid` (downstream).
- **Lifecycle:** invariant - the horizontal sole-layer-station-to-port
  connection holds at the end (re-snapped onto the grid by Stage 6.4).

### Stage 4.3: snap grid-group entry ports
- **Purpose**: For grid-group sections (skipped by Stage 4.2), snap entry
  ports to the connected first-internal-station Y - straight
  port-to-station connection.
- **Helper**: `_snap_grid_group_entry_ports` (`phases/ports.py`).
- **Precondition**: Stage 4.2 complete.
- **Postcondition**: Grid-group entry ports share Y with their first
  connected internal station.
- **Invariants preserved**: Internal station Y. Exit ports.
- **Lifecycle:** invariant - grid-group entry ports share Y with their
  first connected station at the final boundary.

### Stage 4.4: snap grid-group exit ports
- **Purpose**: Mirror of Stage 4.3 for exit ports - snap to the downstream
  entry port's Y (which Stage 4.3 just snapped to a grid station).
- **Helper**: `_snap_grid_group_exit_ports` (`phases/ports.py`).
- **Precondition**: Stage 4.3 complete (downstream entry ports snapped).
- **Postcondition**: Grid-group exit ports share Y with their
  downstream entry port (i.e. with the downstream's connected
  station).
- **Invariants preserved**: Internal stations.
- **Lifecycle:** invariant - grid-group exit ports share Y with their
  downstream entry port at the final boundary.

### Stage 4.5: space ports from termini
- **Purpose**: Push ports away from terminus stations so a routed
  line clears any file-icon caption / label by at least `y_spacing`.
- **Helper**: `_space_ports_from_termini` (`phases/ports.py`).
- **Precondition**: Port Ys settled by Stages 4.1 to 4.4.
- **Postcondition**: For every (port, terminus) pair in the same
  section, `|port.y - terminus.y| >= y_spacing` (modulo bbox bounds).
  Bboxes may expand via `_expand_bbox_for_y` to keep ports on edges.
- **Invariants preserved**: Real non-terminus station Y. Other
  sections.
- **Lifecycle:** invariant - the port-to-terminus clearance holds at the
  final boundary.

### Stage 4.6: recompute grid-group bboxes
- **Purpose**: Reset grid-group bboxes to symmetric `max_y_pad`
  padding around final non-port station Y range, then expand for any
  ports outside.
- **Helper**: `_recompute_grid_group_bboxes` (`phases/row_align.py`).
- **Precondition**: Port Ys final (Stage 4.5).
- **Postcondition**: Each grid-group section bbox snugly bounds its
  content with consistent top/bottom padding.
- **Invariants preserved**: Station and port Ys.
- **Lifecycle:** transient - the snug grid-group bbox is superseded by
  the final bbox sizing in Stage 6.13 (bottom) and Stage 6.15a (top).

### Stage 4.7: re-run top-align
- **Purpose**: Re-flush row tops after Stage 4.5 expanded bboxes via
  `_expand_bbox_for_y` (the same row-top alignment Stage 3.4 applies to
  the rows it pushes, here run over every row).
- **Helper**: `_top_align_row_sections` (`phases/row_align.py`).
- **Precondition**: Stages 4.5 / 4.6 complete.
- **Postcondition**: Same-row contiguous-column sections share
  `bbox_y` (station/port Ys shift by the same delta).
- **Invariants preserved**: Relative station-to-section position inside
  each shifted section. Bbox heights.
- **Lifecycle:** transient - superseded by Stage 6.15a, which grows a
  fanned section's bbox top above the flush line.

### Stage 4.8: align row trunk Ys
- **Purpose**: Within each row, shift content downward in shallower
  sections so the inter-section trunk bundle passes through at a
  single Y, then seat each eligible flow exit on its internal carrier
  row so the level change occurs in the inter-section corridor.
- **Helpers**: `_align_row_trunk_ys` (`phases/row_align.py`), then
  `_reconcile_flow_exit_carrier_anchors` (`phases/ports.py`).
- **Precondition**: Stage 4.7 done.
- **Postcondition**: For sections in a row's contiguous column run that
  all carry the same through-lines, the trunk Y is the row's deepest
  pre-pass trunk Y. Where a row-mate carries only part of the row's
  through-trunk, the target is instead the Y the full-bundle carriers
  already share, so a partial member follows the trunk without
  redefining it and anything deeper than that Y stays put. In both
  cases the target is a Y at which some carrier's LEFT/RIGHT port
  already sits. A partial that hands a through-line straight to a carrier
  one grid column away -- port to port, no junction between them and no
  cell-mate forcing a bypass -- is seated an extra `s` below that shared
  target, where `s = off(port_on_carrier, lid) - off(port_on_partial, lid)`
  is the line's lane on the destination port minus its lane on the source
  port (`compute_station_offsets`); this descent makes the connector run
  level at the line's own lane instead of jogging into it. The descent
  fires only when every qualifying handover agrees on one positive `s` that
  is *stable*: re-derived on a frame where the partial section is shifted so
  its handover port lands level with the carrier port, `s` must survive
  unchanged. A carrier's exit port mid-pass has not yet settled onto the
  level its exiting lines live at, so a raw `s` can be a transient that the
  levelled re-derivation collapses to zero; seating a descent on that phantom
  step corrupts the exit geometry, so a step that does not survive is
  suppressed. The descent is recorded in `graph._partial_trunk_descents`
  (reassigned in full each pass) with its two endpoint ports, because it is a
  sub-grid offset the Stage 6.4 grid snap can round onto the carrier slot --
  Stage 6.4 re-seats it. A non-fold LR/RL exit selected by
  `flow_exit_carrier_anchor` shares its carrier Y; its downstream entry
  remains on the consumer row. Row-spanning sections are skipped.
- **Invariants preserved**: Bbox tops, downstream entry coordinates,
  perpendicular exits, and row-spanning sections.
- **Lifecycle:** invariant - the per-row trunk Y is consistent at the
  final boundary (`test_row_trunk_marker_cy_consistent`).

### Stage 4.9: redistribute fan-out siblings
- **Purpose**: For each fan-out column with a unique trunk junction
  (one station carrying the full bundle plus >=2 side branches),
  redistribute side stations symmetrically around the trunk Y. No-op
  unless `graph.center_ports` (guard inside the helper, not at the call
  site).
- **Helpers**: `_snapshot_planned_fan_centrelines` and
  `_apply_planned_fan_geometry` (`phases/planned_fans.py`) materialise complete
  semantic plans first; `_redistribute_fanout_siblings`
  (`phases/fan_bundles.py`) handles unsupported fans.
- **Precondition**: Trunk Ys aligned (Stage 4.8).
- **Postcondition**: In qualifying columns, fan-out siblings sit
  symmetrically around the section's LR/RL port trunk anchor (the trunk
  station's own Y only when the section has no such port). Linear chains,
  fan-in structures, and file inputs are left in place.
- **Invariants preserved**: Trunk station Y. Off-track stations.
- **Purity**: semantic plans read a centreline frozen immediately after
  structural settlement; legacy fans centre on the frozen port anchor. Neither
  path depends on a governed station's live Y (#491).
- **Lifecycle:** transient - superseded by Stage 6.7 / 6.11, which
  re-fan the siblings against the final trunk Y (this fan uses the early
  trunk Y).

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
- **Lifecycle:** transient - superseded by Stage 6.7, which re-fans the
  full-bundle columns against the final trunk Y (this fan uses the local
  port Y).

### Stage 4.10a: carry full-bundle continuations (engine.py)
- **Purpose**: Stage 4.10 fans a full-bundle column off the trunk row. A
  station whose only predecessor is one of those fanned members, and which is
  that member's only target, has no branch to peel off to, so leaving it on its
  own line's base track paints the in-section V-kink #977 forbids. Copy the
  predecessor's settled lane onto it.
- **Helper**: `_carry_full_bundle_continuations` (`phases/fan_bundles.py`),
  called directly rather than through `_run_placement`: reading a predecessor's
  *current* coordinate is the whole operation, so the purity and anchor-frozen
  guards of the "Anchor invariant" section cannot apply to it and it is absent
  from `CONTENT_PLACEMENT_PHASES`.
- **Precondition**: Stage 4.10 has fanned the full-bundle columns of this pass
  (against the local port Y).
- **Postcondition**: In every section whose lanes run along Y, each station the
  continuation relation names - a visible one-in/one-out chain proven against
  the whole graph - sits on its predecessor's Y wherever that track is free of
  another station at the same layer.
- **Invariants preserved**: Port anchors, section bboxes, stations the relation
  declines to name, and sections whose lanes run along X.
- **Related tests**: `test_bundle_terminator_successor_stays_on_trunk`.
- **Lifecycle:** transient - superseded by Stage 6.7a, which re-runs the same
  carry once Stage 6.7 has re-fanned those columns against the final trunk Y.

### Stage 5.1: position junctions
- **Purpose**: Place each junction station in the inter-section gap
  at the exit port's Y (fan-out) or near the entry port (merge).
- **Helper**: `_position_junctions` (`phases/junctions.py`).
- **Precondition**: All port Ys final (Pass B complete).
- **Postcondition**: Every junction has finite `(x, y)`. Fan-out
  junctions sit at `exit_port.y` plus a `JUNCTION_MARGIN` X offset
  toward the targets; merge junctions sit at
  `max(pred.x) + JUNCTION_MARGIN, entry_port.y`. A fan-out junction on a
  LEFT/RIGHT exit stands `EDGE_TO_BUNDLE_CLEARANCE` from the wall instead
  wherever a branch descends the junction's own X - the column is then a
  channel in the inter-column gap and owes the clearance a gap channel does
  (`_drops_down_the_junction_column`: the entry section shares a grid column
  with the feeder and is stacked beyond it on the port's own side, which is
  the condition under which `_align_tb_entry_port` gives the perpendicular
  port the junction's X). Every other branch turns a corner a runway past the
  junction, so its channel already stands a curve radius clear of the wall and
  the junction keeps only `JUNCTION_MARGIN`.
- **Invariants preserved**: Real stations, ports.
- **Lifecycle:** invariant - junctions track their ports at the final
  boundary (`junction.xy == _compute_junction_xy(ports)`, re-established
  after every later port move).

### Stage 5.2: lift off-track stations (engine.py)
- **Purpose**: Offset off-track file artefacts one step clear of their
  anchor along the section's cross axis (Y for an LR/RL trunk, X for a
  TB/BT one; `section_cross_axis`), stacking when several share one
  anchor. An input's anchor is its consumer; a producer-fed sink's anchor
  is its producer (see `_off_track_anchor_of`). Grow bbox along the cross
  axis to fit the band and along the flow axis to fit the icon extent;
  nudge same-side ports back to the new edges.
- **Helper**: `_lift_off_track_stations`.
- **Precondition**: Stage 5.1 complete; all on-track Ys final.
- **Postcondition**: Each off-track station sits at
  `anchor_cross +/- n*step` (n = stack rank) on the cross axis, keeping
  its own flow-axis (layer) coordinate. The `step` is the cross pitch:
  `y_spacing` for a horizontal section (base content pitch
  `graph._base_y_spacing` on a single-trunk section, so the diagonal-label
  widening doesn't strand the icon, issue #580), or the resolved column
  pitch for a vertical section (`_off_track_lift_step`). Section bbox
  extends to fit.  May leave the topmost section above the canvas margin --
  ``_shift_graph_into_canvas`` runs immediately afterwards to restore the
  margin (called explicitly by the caller, not by the helper).
- **Invariants preserved**: On-track station Y. Other sections' Ys
  (only the canvas Y-offset may shift the world uniformly).
- **Related tests**: `test_off_track_inputs_above_consumer`,
  `test_off_track_outputs_on_lift_side_and_adjacent_to_producer`,
  `test_off_track_icons_ordered_by_consumer_y`.
- **Lifecycle:** invariant - off-track stations sit a step clear of their
  anchor on the cross axis at the final boundary. *liftable:* only behind
  a "consumers final" precondition - the anchor uses the consumer/producer's
  final Y and is re-applied by Stages 6.6 / 6.8 (#463).

### Stage 5.3: re-align row bbox tops only
- **Purpose**: After Stage 5.2 grew some bboxes upward, grow other
  same-row bboxes upward to match. Station Ys in unlifted sections
  preserved.
- **Helper**: `_top_align_row_bboxes_only` (`phases/row_align.py`).
- **Precondition**: Stage 5.2 may have lifted some bboxes.
- **Postcondition**: Within each row's contiguous column group, all
  bboxes share `bbox_y` (heights extended upward as needed).
- **Invariants preserved**: All station / port Ys.
- **Lifecycle:** transient - superseded by Stage 6.15a (flush row tops,
  as Stage 4.7).

### Stage 5.4: compact row content to bbox top
- **Purpose**: Shift each row's column-group up by the smallest
  above-content slack, then shrink bbox heights to remove the empty
  band. Preserves trunk alignment.
- **Helper**: `_compact_row_content_to_bbox_top` (`phases/row_align.py`).
- **Precondition**: Bbox tops aligned (Stage 5.3).
- **Postcondition**: Each row's contiguous column group's bbox top
  sits at `min(content_top) - section_y_padding`, except where
  `_perp_port_lead_edge_reserve` caps the shift so a perpendicular port
  keeps `PERP_PORT_EDGE_INSET` inside the edge -- there the top stays
  higher and the group's content keeps more than the padding above it.
  The reserve is measured from the port station, which is also its topmost
  drawn lane: a port's bundle staggers below it, never above
  (`port_bundle_edge_reach`).
  Stations shift up by the same delta as their bbox.
- **Invariants preserved**: Inter-station relative positions inside
  each section. Trunk Y stays aligned across the row.
- **Related tests**: `test_section_bbox_has_bottom_padding`.
- **Lifecycle:** transient - superseded by Stage 6.1 (fans content back
  into the band) and Stage 6.13 (re-sizes the bbox bottom).

### Stage 5.5: snap inter-section port pairs + reposition junctions
- **Purpose**: Snap exit/entry port pairs in the same row to a shared
  Y (the entry's), then re-run Stage 5.1 to put junctions back on the
  exit port.
- **Helper**: `_snap_inter_section_port_pairs` (`phases/balancing.py`) then
  `_position_junctions`.
- **Precondition**: Row compaction done; port pair Ys may have drifted.
- **Postcondition**: Within each row, every LEFT/RIGHT exit port and
  its connected LEFT/RIGHT entry port share a Y, except an entry already
  seated on its own internal fan trunk (a lead line running straight into
  a root the port also feeds a deeper arm of): dragging it onto a fan-in
  exit's convergence Y would trade the flat inter-section bundle for an
  internal boundary dogleg, so it keeps the trunk row and the level change
  becomes a riser in the gap. Junctions back at exit-port Y.
- **Invariants preserved**: Internal station Y in each section.
- **Related tests**: `test_no_kink_at_section_boundary`,
  `test_inter_section_route_y_stays_within_row_band`.
- **Lifecycle:** invariant - LR/RL exit-entry port pairs share a Y
  (no-kink) and junctions track their ports at the final boundary.

### Stage 6.1: fan free content upward
- **Purpose**: When the row's compaction leaves visible empty top
  band but the section has trunk-candidate sibling stations,
  fan those upward into the empty band.
- **Helper**: `_fan_free_content_upward` (`phases/balancing.py`).
- **Precondition**: Trunk Y aligned (Stage 4.8). Compaction done
  (Stage 5.4).
- **Postcondition**: Eligible sections fan stations upward by at most
  one `y_spacing` slot, balancing content above/below trunk.
- **Invariants preserved**: Trunk station Y. Off-track stations
  (sections with off-track band are skipped).
- **Purity**: top slack and anchor are read from the frozen placement
  reference (see Content-placement purity), not live geometry (#491).
- **Related tests**: `test_section_top_band_filled`,
  `test_section1_input_above_trunk`.
- **Lifecycle:** invariant - the filled top band / content balanced
  around the trunk holds at the final boundary
  (`test_section_top_band_filled`). Stage 6.11 can fill the same band on
  the same section, but moves a *disjoint* station set (strict-subset,
  non-trunk siblings; this stage moves only full-bundle trunk
  candidates), so it does not override this placement.

### Stage 6.2: fan source inputs upward
- **Purpose**: Companion to Stage 6.1 for source-stack sections (single
  full-bundle trunk + subset-bundle file inputs at the entry column).
  Lift trunk-nearest source inputs into the empty top band.
- **Helper**: `_fan_source_inputs_upward` (`phases/balancing.py`).
- **Precondition**: Stage 6.1 done.
- **Postcondition**: Section is top- and bottom-weighted around the
  trunk row instead of stacked below it.
- **Invariants preserved**: Trunk station Y.
- **Purity**: trunk anchor is the frozen LR/RL port Y and the lift count
  reads the frozen placement-reference bbox top, not live geometry (#491).
- **Lifecycle:** invariant - source-stack sections stay
  top-and-bottom-weighted around the trunk at the final boundary.

### Stage 6.3: 2-branch symfan half-grid compaction (engine.py)
- **Purpose**: Sections containing exactly a 2-branch symmetric fan
  (no off-track / constraining content) collapse onto half-pitch
  offsets so the section is 1 grid-unit tall instead of 2. The two
  branches may be fed from upstream (entry port or a terminus source
  icon) or from a single in-section non-terminus source whose two
  consumers are equal siblings (identical line sets); that source is
  the fan hub and is excluded from the branch count. Records the placed
  branches on the public `MetroGraph.half_grid_station_ids` field so
  Stage 6.4 leaves them alone -- this is the only cross-phase channel
  for half-grid placement. The fan's remaining on-track stations (its
  source/trunk) are recorded on `MetroGraph.symfan_trunk_station_ids`
  so Stage 6.4 keeps them on the same local frame; a single in-section
  equal-sibling source hub is additionally moved to the trunk Y so the
  fork is a balanced Y-split rather than collinear with one branch.
  Gated on `center_ports`.
- **Helper**: `_apply_half_grid_2branch_symfan`
  (classification via `_symfan_branches_hub` /
  `_section_symfan_uses_half_grid`).
- **Precondition**: Stages 6.1 / 6.2 done; symfan classification stable
  (`_section_symfan_uses_half_grid`).
- **Postcondition**: Eligible symfan pairs share half-pitch offsets
  from the trunk Y; an in-section equal-sibling source hub sits on the
  trunk Y, centred between them. `graph.half_grid_station_ids` contains
  the branch IDs; `graph.symfan_trunk_station_ids` contains the fan's
  source/trunk IDs.
- **Invariants preserved**: Trunk station Y. Other sections.
- **Related tests**: `test_symfan_pairs_share_y`.
- **Lifecycle:** invariant - 2-branch symfan pairs keep their half-pitch
  offsets at the final boundary (Stage 6.4 skips
  `graph.half_grid_station_ids`); only Stage 6.18 may seat one on a full
  row, and only once its straddling partner has moved away.

### Stage 6.4: snap all Y to grid (engine.py)
- **Purpose**: Final pass snapping every station and port Y to the
  nearest row-wide grid slot, removing fractional Ys left by earlier
  shifts. Stations listed in `graph.half_grid_station_ids` (populated
  by Stage 6.3) are skipped so they keep their intentional half-pitch
  Y. The stage then restores each fan-in target to the midpoint of its
  sources. For a symmetric diamond, it also restores the fork hub and
  its unbranched trunk to the branch midpoint. This keeps the complete
  centreline straight. A restored station joins
  `graph.half_grid_station_ids` if it sits half a pitch from the branch
  grid. Finally it re-seats each `graph._partial_trunk_descents` entry:
  the whole partial section -- every internal station, every LR/RL port
  and its bbox -- is shifted so its handover port lands `descent` below
  the carrier port's post-snap Y. Working from that carrier reference
  lands it right whether the snap collapsed the Stage 4.8 handover descent
  onto the carrier slot (shared row grid) or left it in place (explicit-grid
  solo section), and finds zero gap on a re-run, so a preserved descent is
  never double-counted.
- **Helper**: `_snap_all_y_to_grid`, with
  `_restore_convergence_midpoints` / `_restore_divergence_midpoints`
  and `_centreline_trunk_followers` (`phases/fan_bundles.py`) for the
  midpoint restores, and `_restore_partial_trunk_descents` for the
  handover descents.
- **Precondition**: All semantic Y shifts done. If Stage 6.3 ran,
  `graph.half_grid_station_ids` is populated.
- **Postcondition**: Every station and port Y is a grid slot of the
  per-section / per-row pitch (except marked half-grid stations and
  partial row-mates re-seated a handover lane below their carrier). A
  symmetric diamond's fork hub, join and trunk run share one Y.
- **Invariants preserved**: X coordinates (tested by
  `test_grid_snap_does_not_mutate_x`). Half-grid station Ys.
- **Related tests**: `test_all_stations_snap_to_grid`,
  `test_grid_snap_does_not_mutate_x`,
  `test_fork_and_join_hub_share_centreline`,
  `test_ported_fan_centreline_reaches_ports_and_trunk`.
- **Lifecycle:** invariant - every (non-half-grid) station/port Y is a
  grid slot at the final boundary (re-asserted canvas-wide by Stage
  6.15).

### Stage 6.5: align TB-section bbox bottoms
- **Purpose**: Extend TB-section bbox bottom to match the downstream
  LR/RL section's *settled content* bottom so the line doesn't look
  pinned to the TB bbox edge, and the straight inter-section run clears
  both section bottoms by the same distance. The target's settled
  content bottom (`_predict_section_content_bottom`) is used rather than
  its live `bbox_h`, which the later bbox-shrink phase may collapse.
- **Helper**: `_align_tb_section_bbox_bottoms` (`phases/ports.py`).
- **Precondition**: All station/port Ys final (post-snap).
- **Postcondition**: For each TB section feeding an LR/RL target,
  `tb.bbox_y + tb.bbox_h >= target settled content bottom`. After the
  bbox-shrink phase the two edges are level for a straight run (guarded
  by `_guard_fold_lr_exit_sections_share_bbox_bottom`, #1162).
- **Invariants preserved**: All station and port Ys. Other bboxes.
- **Lifecycle:** invariant - TB-section bbox bottoms align with their
  downstream LR/RL target at the final boundary.

### Stage 6.6: reanchor off-track to consumer (engine.py)
- **Purpose**: Re-pin each off-track station `n*step` clear of its anchor
  on the cross axis using the anchor's final snapped coordinate (Stage 5.2
  used pre-snap ones); the anchor is the consumer for an input, the
  producer for a sink. Recompute the lift-side bbox edge to fit the band
  (grow **or** shrink); grow the opposite and flow edges as needed.
- **Helper**: `_reanchor_off_track_to_consumer`.
- **Precondition**: Stage 6.4 snapped consumers to grid. Enforced
  explicitly via `graph._consumers_grid_snapped` (set right after the
  Stage 6.4 snap); the helper raises `PhaseInvariantError` if it runs
  while unset. This makes the snapped-consumer dependency explicit.
- **Postcondition**: Off-track stations sit `n * step` clear of their
  anchor's final cross coordinate. The lift-side bbox edge hugs the band
  (recompute-to-fit, so re-running is order-independent). May leave the
  topmost section above the canvas margin -- ``_shift_graph_into_canvas``
  runs immediately afterwards (called explicitly by the caller, not by the
  helper).
- **Invariants preserved**: On-track station Y.
- **Related tests**: `test_off_track_inputs_above_consumer`,
  `test_off_track_outputs_on_lift_side_and_adjacent_to_producer`,
  `test_reanchor_off_track_requires_snapped_consumers`,
  `test_reanchor_off_track_bbox_fit_is_reversible`.
- **Lifecycle:** invariant - off-track stations sit a step clear of their
  anchor's final cross coordinate. *liftable:* as a **precondition-gated** invariant
  (#463): the bbox fit is now reversible, but the helper *raises* when
  `_consumers_grid_snapped` is unset, so a run-anytime `maintain()` pass
  must check that flag and skip while consumers are pre-snap rather than
  call-and-catch. Registry integration deferred to #459.

### Stage 6.7: re-center full-bundle columns (engine.py)
- **Purpose**: Re-fan full-bundle columns around the row's final trunk
  Y (Stage 4.10 used the local port Y which may now be stale).
  Gated on `center_ports`.
- **Helper**: `_recenter_full_bundle_columns`, then the port-seating pair
  `_center_lr_entry_ports_on_fork` / `_center_lr_exit_ports_on_join`, which
  seat a flow-aligned port on the centreline of the two-way fork it feeds or
  the two-way join that feeds it. A port already level with one of those
  branches is left there: that is a dead-end fan's legitimate seat, where the
  branch's track is the trunk the inter-section run continues along.
- **Precondition**: Final inter-section trunk Y known (post-snap).
- **Postcondition**: Full-bundle columns are symmetric around the
  row's final trunk Y; a flow-aligned port bounding a two-way fork or join
  sits on one of its branches' tracks or on their midpoint.
- **Invariants preserved**: Off-track Y anchoring (re-established by
  Stage 6.8) and bbox-top alignment (re-established by Stage 6.9)
  are temporarily broken; both are restored before leaving the
  `if center_ports:` block.
- **Lifecycle:** invariant - full-bundle columns are symmetric around
  the row's final trunk Y at the boundary; no later stage re-fans them,
  though Stage 6.18 seats a half-pitch member on a full row once its
  straddling partner has moved away.
  *liftable:* no - one-shot, order-dependent (computes against the final
  trunk Y, so a premature run is wrong).

### Stage 6.7a: re-carry full-bundle continuations (engine.py)
- **Purpose**: Re-run the Stage 4.10a carry over the columns Stage 6.7 has just
  re-fanned, so a sole successor follows its predecessor onto the final
  trunk-anchored lane instead of the earlier port-Y guess.
- **Helper**: `_carry_full_bundle_continuations` (`phases/fan_bundles.py`); the
  same helper Stage 4.10a calls, bare here for the same reason.
- **Precondition**: Stage 6.7 has re-centred full-bundle columns against the
  row's final trunk Y.
- **Postcondition**: Each station the continuation relation names sits on its
  predecessor's post-recenter Y wherever that track is free.
- **Invariants preserved**: Port anchors and the recenter's own symmetry; only
  continuation stations move.
- **Related tests**: `test_bundle_terminator_successor_stays_on_trunk`.
- **Lifecycle:** invariant - a carried continuation shares its predecessor's
  lane at the final boundary.

### Stage 6.7b: carry symmetric-branch continuations (engine.py)
- **Purpose**: Under `diamond_style: symmetric` a two-way fork's branch
  stations are fanned off the trunk; a branch that continues to further
  stations *inside* the section would leave those successors on the trunk, so
  the branch humps out and immediately back. Pull the in-section
  sole-successor chain onto the branch's Y.
- **Helper**: `_carry_symmetric_branch_continuations` (`phases/fan_bundles.py`),
  bare for the same reason as Stage 6.7a. No-op unless
  `diamond_style == "symmetric"`.
- **Precondition**: Stage 6.7 has fixed the branch Ys the chain aligns to; the
  section's direction is LR or RL.
- **Postcondition**: A dead-end branch stays fanned for its whole in-section
  length. The walk stops where the forward path leaves the branch - a second
  predecessor, a fork, or a continuation only through an exit port - so a branch
  that exits the section falls back to the trunk and meets its exit port there.
- **Invariants preserved**: Planned-fan stations (owned by the fan plan), the
  branch stations themselves, and every section that is not a symmetric fork.
- **Related tests**: `test_symmetric_fanout_branch_continuation_stays_fanned`.
- **Lifecycle:** invariant - a fanned branch's in-section continuation rides
  the branch track at the final boundary.

### Stage 6.7c: align a symmetric fork to its row feeder (engine.py)
- **Purpose**: Centring a fork on its entry port can leave that port off the
  trunk of the same-row section feeding it - the port settled on the fork
  midline rather than on the horizontal bundle arriving from the flow side.
  Translate the whole section so its entry port shares a Y with that feeder's
  exit port.
- **Helper**: `_align_symfan_section_to_row_feeder` (`phases/fan_bundles.py`),
  via `shift_section`. No-op unless `diamond_style == "symmetric"`, and it
  declines a shift under 1px.
- **Precondition**: Stage 6.7 / 6.7b have settled the fork's branch Ys, and the
  feeder's exit port Y is final.
- **Postcondition**: A symmetric-fork section whose entry port has an in-row
  feeder is translated so the inter-section run into it is level.
- **Invariants preserved**: The section's internal geometry - content, ports and
  bbox move together by one delta - and every section with no such feeder.
- **Related tests**: `test_symmetric_fork_entry_port_stays_on_feeder_trunk`.
- **Lifecycle:** invariant - the feeder run stays level at the final boundary.

### Stage 6.7d: seat flow-aligned ports on the fork or join (engine.py)
- **Purpose**: Seat a flow-aligned port on the centreline of the two-way fork it
  feeds, or of the two-way join that feeds it, so the diamond closes
  symmetrically and the inter-section run leaves straight. A port fork has no
  station on the centreline for the join to inherit from, so the join and its
  linear trunk continuation are seated with it.
- **Helpers**: `_center_lr_entry_ports_on_fork` and
  `_center_lr_exit_ports_on_join` (`phases/fan_bundles.py`), the pair also named
  in the Stage 6.7 entry. Both no-op unless
  `diamond_style == "symmetric"`, and both leave planned-fan ports to their fan
  plan.
- **Precondition**: Stages 6.7 / 6.7b / 6.7c have settled the branch Ys the
  midpoint is computed from.
- **Postcondition**: An entry port fanning into branches at exactly two
  distinct Ys sits at their midpoint, as does a clean in-section join and its
  trunk continuation; an off-grid midpoint is taken only when the fork
  reconverges. An exit port is moved only when it is seated outside its two
  feeders' span - a port already straddled by them would trade a level run for
  a step down and back up.
- **Invariants preserved**: The branch stations themselves keep their places,
  and a port already level with one branch is left there: that is a dead-end
  fan's legitimate seat, where the branch's track is the trunk the
  inter-section run continues along.
- **Related tests**: `test_symmetric_exit_port_join_stays_within_its_feeders_span`
  and `test_fork_and_join_hub_share_centreline`.
- **Lifecycle:** invariant - the seated port is on the fork or join centreline
  at the final boundary.

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
  their post-recenter consumer. Section tops are recomputed to fit the
  off-track band (grow or shrink), so re-running is order-independent.
- **Invariants preserved**: Row top-alignment may be broken when a
  bbox top moved; Stage 6.9 restores it.
- **Lifecycle:** invariant - off-track inputs sit a pitch above their
  post-recenter consumer at the final boundary. *liftable:* as a
  **precondition-gated** invariant (#463): reversible bbox fit, but the
  helper raises while `_consumers_grid_snapped` is unset, so a run-anytime
  `maintain()` pass must check that flag and skip until consumers are
  snapped rather than call-and-catch. Registry integration deferred to
  #459.

### Stage 6.9: re-run row top-align (engine.py)
- **Purpose**: A Stage 6.8 bbox grow can leave the grown section's
  bbox top above its row mates'. Pull row mates' bbox tops up to
  match so the section row stays flush along its top edge. Gated on
  `center_ports`.
- **Helper**: `_top_align_row_bboxes_only` (same helper as Stage 5.3).
- **Precondition**: Stage 6.8 has re-anchored off-track inputs.
- **Postcondition**: Row bboxes flush at the top across all row mates.
- **Invariants preserved**: Station Ys (only bbox tops move).
- **Lifecycle:** transient - superseded by Stage 6.15a (flush row tops,
  as Stage 4.7).

### Stage 6.10: align terminus to upstream
- **Purpose**: After Stage 6.7 re-pitched fanned columns, a single-station
  downstream column (e.g. a `file` terminus) may have stayed at its
  pre-fan Y. Pin it back onto its sole upstream's Y.
- **Helper**: `_align_terminus_to_upstream` (`phases/single_section.py`).
- **Precondition**: Stage 6.7 re-centered fans.
- **Postcondition**: Single-station downstream columns share Y with
  their unique upstream.
- **Invariants preserved**: Multi-station columns.
- **Related tests**: `test_terminus_not_directly_after_diagonal`.
- **Lifecycle:** invariant - single-station downstream columns share Y
  with their unique upstream at the final boundary.

### Stage 6.11: balance section content around trunk
- **Purpose**: Auto-balance pass. For sections whose final layout
  still has an empty band above the trunk while more siblings sit
  below than above, lift bottommost movable siblings into the empty
  top band. U-turn-safe and bbox-bounded.
- **Gating**: Early-returns unless `graph.layout_provenance` contains at least
  one author-owned grid decision and `graph.center_ports` is set (scoped to
  explicit-`%%metro grid:` + centre-ports pipelines), so it is a no-op on
  auto-laid graphs.
- **Helper**: `_balance_section_content_around_trunk` (`phases/balancing.py`).
- **Precondition**: All earlier 13-phase reshuffles done.
- **Postcondition**: Sibling count above trunk >= sibling count below
  trunk (where movable), inside bbox.
- **Invariants preserved**: Trunk station Y. Sections that already
  balance are left alone.
- **Purity**: an in-scope reset restores every station to its frozen
  placement-reference Y before the lift/swap loop, and the band gates /
  feeder check read the reference, so the balance decision does not depend
  on live geometry (#491).
- **Related tests**: `test_section_top_band_filled`.
- **Lifecycle:** invariant - section content is balanced around the
  trunk (siblings above >= below, where movable) at the final boundary.

### Stage 6.12: recenter loop side stations
- **Purpose**: Recompute the X of fan-out side stations (one trunk
  predecessor, one trunk successor - "loop side" stations like propd,
  dream, DESeq2 around limma) to the midpoint of their actual diagonal
  corner Xs from the routing geometry.
- **Helper**: `_recenter_loop_side_stations` (`phases/balancing.py`).
- **Precondition**: All Y phases done; routing geometry derivable.
- **Postcondition**: Loop side stations sit at the visual centre of
  their horizontal loop run.
- **Invariants preserved**: Station Y. Pure-side-branch classification
  is strict (see `test_loop_recenter_only_for_pure_side_branches`).
- **Related tests**: `test_fan_station_centered_on_loop`,
  `test_loop_recenter_only_for_pure_side_branches`,
  `test_loop_column_stations_share_x`.
- **Lifecycle:** invariant - loop-side stations sit at the visual centre
  of their loop run at the final boundary.

### Stage 6.13: shrink and tighten rows
- **Purpose**: Shrink each section's bbox bottom to
  `max_content_y + section_y_padding` (phase 1), then pull lower-row
  sections up to close any vertical slack the shrink revealed
  (phase 2).  Phase 1 handles bbox bottoms that drifted after earlier
  passes lifted content; phase 2 handles the pre-shrink row-height
  overestimate when a rowspan section collapses to less than its
  row claim.  Phase 2 must run as a second pass over the graph so
  every section's shrink is finalised before row-gap deficits are
  measured.  Phase 2 reads `bbox_y + bbox_h` from Phase 1's content-hugging
  bbox as the row-ending extent.  If `graph._struct_height_below_top`
  is populated, its per-section height is used instead (reconstructed
  on the current bbox top); that dict is populated after Stage 6.15a
  and retaken after a Stage 6.18a planned-fan refit, so it records the
  fully settled extent for structural-extent fidelity checks.
- **Helper**: `_shrink_and_tighten_rows` (orchestrates
  `_shrink_bboxes_to_content_bottom` then
  `_tighten_lower_rows_after_shrink`).
- **Precondition**: All content Ys final.
- **Postcondition**: Section bbox bottoms sit `section_y_padding`
  below the deepest content (trunk alignment unaffected -- only
  bottom shrinks), and clear the lowest drawn lane of every port the box
  holds: `PERP_PORT_EDGE_INSET` for a perpendicular port, otherwise
  `MIN_BUNDLE_EDGE_CLEARANCE` for a trunk port carrying a multi-line bundle,
  which owes the edge the label room an interior station's bundle owes it.  Both
  terms are measured from the port's outermost lane rather than the port station
  (`port_bundle_edge_reach`), and a trunk port carrying a single line reserves
  nothing here -- where it sits is the content padding's business.  For each
  row pair, the row gap is `section_y_gap` (no more, no less, except where rowspan
  sections filled their full row claim).  A row pair claimed by
  `_merge_trunk_row_minimums` keeps that wider minimum between the two row
  *envelopes*: the trunk's channel crosses the whole boundary, so no
  column-overlapping section pair bounds it and none records the two rows as
  related (its connectors are rewritten through fan and merge nodes).
- **Invariants preserved**: Bbox tops. Within-row trunk Ys. Bbox
  heights of upper rows.
- **Related tests**: `test_section_bbox_has_bottom_padding`,
  `test_section_bbox_matches_content_extent`.
- **Lifecycle:** invariant - content-hugging bbox bottoms and correct
  inter-row gaps hold at the final boundary (maintained by Stage 6.14,
  which restores the gap via `push_lower_rows_after_bbox_grow` whenever
  it grows a bbox downward). *liftable:* no - one-shot, order-dependent
  (computes against the final content extent).

### Stage 6.14: shift and propagate loop stations
- **Purpose**: Shift sparse loop-side stations (one inbound, one
  outbound, single-line consumer) onto a half-pitch Y when sharing
  the full-row Y with a busier sibling whose inbound bundle would
  otherwise breeze-past the sparse station's marker.  When a shift
  grows a section's bbox downward, push lower-row sections down
  internally to restore `section_y_gap`.
- **Helper**: `_shift_and_propagate_loop_stations`
  (calls `push_lower_rows_after_bbox_grow` when any bbox grew).
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
- **Lifecycle:** invariant - sparse loop-side stations keep their
  half-pitch offset at the final boundary; row gaps preserved across any
  bbox grow.

### Stage 6.15a: fit bbox tops to content (grow and shrink)
- **Purpose**: Size each bbox top to `section_y_padding` above its highest
  marker, bounded by the row above. Grows when fan re-distribution (Stages
  4.9 / 4.10 / 6.7 / 6.11) lifted a branch above the line the bbox was sized
  for, crowding the topmost marker (issue #406). Shrinks when the transient
  row-top flush left an empty band above content with nothing in it (no port
  or bypass helper); a band holding a port or bypass helper is left intact.
  The upward grow can breach the canvas top margin, so
  `_shift_graph_into_canvas` runs immediately after. That shift keeps every
  section `section_y_padding` below the canvas top and, on a titled map, keeps
  every *drawn* section `TITLE_BAND_CLEARANCE` below it so the header badge
  clears the title band (issue #1273).
  Two row-top seating passes run in this stage only under `row_align == "top"`
  and are a no-op under the content default: `_top_align_packed_row_bboxes`
  levels a packed cell's header line with its contiguous row, and
  `_top_align_side_entered_vertical_to_feeder` grows a side-entered vertical
  section's top up to the feeder row-mate flowing into it.
- **Helper**: `_fit_bboxes_to_content_top` (`phases/bbox.py`), then
  `_shift_graph_into_canvas`.
- **Precondition**: All content Ys final (post-6.14).
- **Postcondition**: Each bbox top sits `section_y_padding` above its
  highest marker, or `PERP_PORT_EDGE_INSET` above the topmost drawn lane of a
  perpendicular port that reaches higher, whichever is further out. For a
  section with an empty band (no port / bypass above content) the padding term
  is an equality, not just a floor: the excess band is reclaimed. Both port
  terms are measured from the port's outermost lane rather than the port
  station (`port_bundle_edge_reach`), and a port the inset does not cover but
  that carries a multi-line bundle owes `MIN_BUNDLE_EDGE_CLEARANCE` past that
  lane, the same floor an interior station's bundle owes the edge.
- **Invariants preserved**: Station Ys (only bbox tops move). Resolves #406.
- **Related tests**: `test_section_bbox_has_top_padding`,
  `test_section_bbox_top_hugs_content`.
- **Lifecycle:** invariant - each bbox top hugs its highest marker at the
  final boundary (a full `section_y_padding`, an equality for empty-band
  sections), the final top-sizing pass. Row-top flush alignment is not a
  maintained property; it is transient scaffolding superseded here.

### Stage 6.15b: distribute stacked rows across a rowspan band
- **Purpose**: When a column holds single-row sections stacked one per grid
  row beside an adjacent `grid_row_span > 1` section spanning those rows,
  distribute them across that section's vertical band so the topmost's bbox
  top meets the band top and the bottommost's bbox bottom meets the band
  bottom. Otherwise a `center_ports` fan in the top section spreads above the
  band into the title space, and the bottom section floats high with slack
  beneath it.
- **Helper**: `_distribute_stacked_rows_in_rowspan_band` (`phases/row_align.py`),
  after the Stage 6.15a fit and before `_shift_graph_into_canvas`.
- **Precondition**: Bbox tops content-fitted (post-fit), bboxes final-sized.
- **Postcondition**: For a qualifying stack (one section per band row, with
  band slack), the topmost top equals the band top and the bottommost bottom
  equals the band bottom; sections shift without resizing.
- **Invariants preserved**: Bbox heights; intra-section station geometry
  (each section's stations and ports shift together).
- **Related tests**: `test_stacked_rows_fill_rowspan_band`; runtime guard
  `_guard_stacked_rows_fill_rowspan_band`. Resolves #1207, #1209.
- **Lifecycle:** invariant - a qualifying stack fills its rowspan band at the
  final boundary.

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
  layouts and half-grid / convergence stations are left untouched. A
  candidate grid shift is rejected if it would pull the top above the
  canvas margin or (on a titled map) a drawn section into the title band.
- **Invariants preserved**: Relative station/section/port Ys (the whole
  canvas moves by one delta).
- **Related tests**: `test_auto_y_spacing_fits_content`.
- **Lifecycle:** invariant - canvas-wide grid alignment holds at the
  final boundary (the last Y pass; only ports/junctions move after, via
  Stage 6.16).

### Stage 6.16: re-align vertical-flow entry ports + re-anchor junctions
- **Purpose**: A vertical-flow (TB/BT) section's perpendicular entry port is
  pinned a fixed offset above its first internal station, so the late vertical
  settling (Stages 6.13-6.15) that shifts the section's content drags the entry
  port off the upstream feeder Y it was snapped to in Stage 3.2, re-introducing
  an inter-section S-kink. Re-run the port alignment for vertical-flow sections
  to re-snap them, then re-anchor every junction (any direction) to the settled
  exit/entry port Ys, since junctions live in inter-section space and the
  settling phases leave them stale.
- **Helper**: `_align_entry_ports(graph, vertical_only=True)`
  (`phases/ports.py`), then `_position_junctions`.
- **Precondition**: All vertical settling done (post-6.15).
- **Postcondition**: Vertical-flow entry ports share their upstream feeder's
  Y; all junctions re-anchored to the settled ports.
- **Invariants preserved**: Horizontal-flow (LR/RL) entry/exit geometry, which
  `vertical_only` leaves on the positions the settling phases deliberately gave
  it.
- **Validate guard after**: bisection set ("after Stage 6.16").
- **Lifecycle:** invariant - vertical-flow entry ports share their upstream
  feeder Y (no-kink) and junctions track them at the final boundary.
- **Scope**: `vertical_only` prevents settled horizontal-flow ports from moving.
  `_position_junctions` remains axis-generic because every junction depends on
  its final port positions.

### Stage 6.17: semantic fan settlement and symmetric compaction (engine.py)
- **Purpose**: Re-materialise every planned semantic fan against its settled
  centreline. Under `diamond_style='symmetric'`, a planned two-way fan keeps
  mirrored half-pitch lanes around that centreline even when topology identifies
  one branch as the unique continuation. For unsupported legacy fans, compact
  each clean 2-way fork-join diamond (`_iter_symmetric_diamonds`) onto half-pitch
  offsets `trunk_y +/- 0.5 * y_spacing`, so the diamond reads as a tight
  one-grid-unit bubble rather than straddling the trunk at full pitch
  (as tall as a 3-way fan with an empty trunk row between its branches).
  Per-diamond, so a diamond compacts even when it shares a section with a
  wider fan (which keeps its full-pitch slots) and regardless of
  `center_ports`. Records the branches on
  `MetroGraph.half_grid_station_ids`. Runs after every trunk-settling
  pass, so the branches straddle the section trunk's final Y exactly; the
  compaction only moves them inward toward the trunk, so it never breaks bbox
  containment.
- **Helpers**: `_snapshot_planned_fan_centrelines` captures the settled frame,
  `_apply_planned_fan_geometry` materialises it, then
  `_apply_half_grid_symmetric_diamonds` for symmetric legacy geometry.
- **Precondition**: Trunk Ys and section bboxes settled (post-6.16).
- **Postcondition**: Each planned station realises its immutable relative frame.
  Each symmetric two-way fan straddles one centreline at half pitch; legacy
  diamond branch IDs are recorded in `graph.half_grid_station_ids`.
- **Invariants preserved**: Trunk station Y, ports, section bboxes, unrelated
  row-mate bbox tops, and wider fan full-pitch slots.
- **Related tests**: `test_symmetric_diamond_compacts_to_half_pitch`,
  `test_symmetric_diamond_both_branches_deviate`,
  `test_symmetric_style_keeps_planned_two_way_fan_on_shared_centreline`,
  `test_planned_fan_does_not_level_unrelated_row_bbox_tops`,
  `_guard_symmetric_diamond_branches_straddle_trunk`, and
  `_guard_planned_fan_frame_realised`.
- **Lifecycle:** invariant - symmetric diamond branches keep their
  half-pitch offsets at the final boundary; only Stage 6.18 may move one,
  and only when its straddling partner is gone.

### Stage 6.18: orphaned half-pitch expansion (engine.py)
- **Purpose**: A half-pitch offset encodes one side of a symmetric pair
  straddling the section trunk, so the pair reads as one compact grid
  unit. Stage 6.10's `_align_terminus_to_upstream` may pull a terminus
  member onto its producer's trunk Y, leaving the other member holding an
  offset that straddles nothing and rendering as a branch stranded
  between two grid rows. `_straddles_nothing` mirrors each marked
  station's offset about the section's LR/RL port anchor; with no station
  at the mirrored slot, the branch is seated one full row from the anchor
  on the side it already sits, its half-grid marking cleared, and the
  section bbox grown over the moved branch alone. Stations marked
  half-grid whose settled Y is already a whole number of rows from the
  anchor are left alone. A Stage 6.4 centreline has no mirror member, so
  the fork hub and its midpoint trunk are exempt.
- **Helper**: `_expand_orphaned_half_grid_stations`
  (`phases/fan_bundles.py`), sharing `_half_grid_frame` /
  `_straddles_nothing` with the invariant test.
- **Precondition**: Every pass that places or dissolves a half-pitch pair
  has run (post-6.17), so the half-grid marks are final.
- **Postcondition**: No station in `graph.half_grid_station_ids` sits half
  a pitch off its section's LR/RL port anchor with the mirrored slot
  empty.
- **Invariants preserved**: Trunk station Y, ports, bbox containment.
  Deliberately not preserved: the half-grid marker set (the seated
  station's id is discarded, so the post-layout readers see only stations
  still at half pitch) and the section bbox extent, which grows over the
  seated branch. No runtime `_guard_*` arms this postcondition:
  `test_half_grid_stations_straddle_in_pairs` covers it across the corpus
  without the abort risk a live guard would add to novel input.
- **Related tests**: `test_half_grid_stations_straddle_in_pairs`.
- **Lifecycle:** invariant - the expanded branch keeps its full-row Y at
  the final boundary (no later Y mutation). The cleared marker reaches the
  next `_layout_once` pass, which re-derives the marks from scratch.

### Stage 6.18a: refit planned fan bbox bands (engine.py)
- **Purpose**: Stage 6.17 can move planned fan content after the general bbox
  fit in Stage 6.15a. Restate the padding band around where that placement left
  the content: remove top slack it no longer needs, widen the row boundary when
  the row above blocks the band it does need, then grow the box back to the
  content it now holds. Whatever the placement moved is what the structural
  extent must record, so the snapshot is retaken here.
- **Helper**: `refit_empty_section_tops_to_content`,
  `_reserve_row_gap_for_top_padding`, `grow_section_bands_to_content`,
  `push_lower_rows_after_bbox_grow`, then
  `_snapshot_struct_heights_below_top` (all `phases/bbox.py`), scoped
  by `planned_fan_layout_section_ids` (`phases/planned_fans.py`).
- **Precondition**: Planned fan geometry and half-pitch expansion are settled
  (post-6.18).
- **Postcondition**: A planned fan section with an unused top band has exactly
  `section_y_padding` above its highest visible content; every planned fan
  section keeps a full padding band on both Y edges, the row boundary above it
  having widened where the ceiling would otherwise cut the band short.
  `graph._struct_height_below_top` holds each section's settled extent.
- **Invariants preserved**: Station and route geometry within a section
  (a widened row boundary translates whole rows), unrelated section bboxes, and
  top bands used by ports or bypass helpers.
- **Related tests**: `test_section_bbox_top_hugs_content`,
  `test_section_bbox_has_top_padding`,
  `test_structural_extent_matches_settled_within_tolerance` and
  `_guard_section_top_padding`.
- **Lifecycle:** invariant - no geometry or bbox phase follows this refit.

## Post-layout offset boundary: late per-line offset phases

`compute_station_offsets` (`layout/routing/offsets.py`) assigns one lane per
`(station, line)` in numbered phases; its docstring lists every phase in order.
The two below are recorded here because they move lanes a whole section at a
time, after each base, station, port, junction and rail-boundary phase has
settled, and because the planned-fan offsets that follow them own the lanes a
fan plan states.

### Phase 14b: entry-arrival lane slotting

- **Purpose**: An inter-section run arrives on the lane its upstream carrier
  draws. Where the receiving section reserves a different slot for that line,
  the run between the entry port and the first station spends the difference as
  a sub-radius diagonal - and a port draws no marker, so that step lands in open
  space just inside the section box. Exchange the arriving line's slot for the
  one already holding the approach lane.
- **Helpers**: `_slot_entry_arrivals_on_their_approach_lane` walks each
  lane-stacked section's flow-side entry ports; `_slot_one_entry_arrival`
  decides one line; `_exchange_pair_beyond_exit` carries an exchange that
  reaches an exit port out along the pair's shared chain.
- **Precondition**: Non-compact mode, and a section whose lanes run along Y.
  The port, reconciliation and rail-boundary phases (5 through 14) have settled
  the upstream lane the exchange reads.
- **Postcondition**: Where the exchange is provably a permutation of lanes
  already in use at the first station and provably crossing-free - the approach
  lane held by exactly one other line, and that line originating at the station,
  so the two meet only under the station's own marker - the arriving line holds
  its approach lane, the displaced line holds the slot the arrival vacated, and
  the swap propagates along the section's horizontal runs. Outside the section
  the exchange continues until the pair stops travelling together, so the
  bundle's lead-in order at the divergence junction and its peel order at the
  destination agree. Every station keeps one lane per line it carries.
- **Invariants preserved**: Station, port and junction coordinates; the number
  of lanes each station spreads; the order of every line outside the exchanged
  pair. The approach lane is read as a stored lane offset, which names the same
  lane at the consumer only while the feeder's trunk is level with it, so a
  stepped seam is declined rather than converted through screen coordinates.
- **Related tests**:
  `test_entry_arrival_permutes_lanes_rather_than_sharing_one`.
- **Lifecycle:** invariant - the arriving line holds its approach lane in the
  published offset map: phase 14c can only relabel a whole section's levels
  rigidly, which preserves the exchange.

### Phase 14c: section free-lane closure

- **Purpose**: A station whose bundle skips a lane level draws a marker tall
  enough to span it. That reservation earns its space when a section-mate rides
  the level; a level *no* line of the section rides reserves nothing, so every
  lane above it drops one step.
- **Helpers**: `_close_section_dead_lanes`, admitting a section's shift only
  when `_dead_lane_squeeze_keeps_runs` confirms it.
- **Precondition**: Non-compact mode; phase 14b has run, being the last phase
  that can vacate a level. Rail-laid sections are skipped: their geometry is
  drawn from absolute rail coordinates, so their lane levels are not a bundle to
  tighten.
- **Postcondition**: No section reserves a lane level none of its lines ride,
  except where closing one would tilt a run. The drop is a rigid,
  order-preserving relabelling applied to every station and port of the section
  at once, and it is applied only when no edge carrying a shifted line ends with
  a larger lane difference across it - so a run that was level inside the
  section or across one of its seams stays level, and a hop that already stepped
  never steps further. A squeeze that fails that test is declined whole and the
  section keeps its dead level: the level costs a taller marker, a tilted run
  costs a kink. No fixture in the corpus needs that escape, which is why
  `tests/test_section_lane_reservations.py` can assert the unconditional form
  over every laid-out `.mmd`.
- **Invariants preserved**: Bundle order, one lane per line at every station,
  compact-mode and rail-section offsets, and the lanes a fan plan states: the
  planned-fan offsets run after this and have the last word on them.
- **Related tests**: `tests/test_section_lane_reservations.py`.
- **Lifecycle:** invariant - the published offset map holds no lane level
  reserved for a line its section never carries, save one a declined squeeze
  left behind.

## Post-layout routing boundary: exit-turn planning

- **Purpose**: Decide source-lane order and turn axes for every complete
  inter-section exit group before route emission.
- **Helpers**: `compute_station_offsets` produces the base offset map.
  `_route_edges` prepares exit-turn execution immediately before emission. A
  turn that needs a shared gap seat enters member-geometry allocation as a
  movable claim. The allocated axis and signed corner offsets are then supplied
  to `build_exit_turn_execution`, which publishes the complete group and
  commits its owned compact offsets to the routing context.
- **Precondition**: Layout coordinates and topology resolution are settled and
  remain immutable. The mutable per-line offset map has completed all local,
  port, junction, and rail-boundary phases.
- **Postcondition**: Each supported exit group has compact active lanes, one
  assignment per outbound member, and any needed ordered turn axes, lane
  transitions, references, and runway demands. Gap-allocated axes are fixed in
  the plan before emission. Any unsupported member records a declined child
  verdict; the route system must obtain complete ownership elsewhere or fail.
- **Invariants preserved**: Station, port, junction, and section coordinates.
  The planner may commit per-line station offsets at its owned seam. Downstream
  passes may change unowned route geometry but cannot move, remove, or replace
  a planner-owned source-turn segment or lane transition. The general gap pass
  validates a gap-allocated plan's exact axis and derives its expected radius
  with `concentric_corner_radius_at`; it does not allocate that axis. Re-seating a planned
  axis derives the opening corner from the source-lane displacement; the corner
  at the other end of that axis belongs to its destination or transition family
  and keeps that family's radius.
- **Related tests**: `tests/test_exit_turn_planner.py`,
  `tests/test_route_plan.py`, and the topology fixtures
  `leftward_up_exit_turn_order.mmd` and
  `terminated_exit_lane_compaction.mmd`.
- **Settlement replay**: `RoutePlan.exit_turn_dispositions` carries every plan's
  verdict, including the plans of systems whose published record is narrowed to
  the owners of shared settlement resources. A settlement re-route replans across
  translated geometry, so a plan sitting near a tolerance boundary can reach a
  different verdict on the second pass; `_adopt_prior_dispositions` replays the
  frozen one from this record, and the diagnostic it emits survives that
  narrowing because it is the only evidence the replay ran. A plan claiming no
  reference, demand, or turn axis is published whatever its system's disposition:
  it is inert, and the planning diagnostics retain its verdict.
- **Lifecycle:** invariant - every planned lane, lane transition, route family,
  and turn axis matches the final routed paths, and every assignment is
  consumed exactly once at the render boundary.

## Post-layout routing boundary: member geometry planning

- **Purpose**: Give every preliminarily planned non-convergence emission member
  one production seed with immutable declared channels before final route-system disposition and
  global convergence settlement.
- **Helpers**: `classify_inter_section_family` freezes one stable
  `RouteFamilyId` per member. `build_member_geometry_execution` visits members
  in scaffold order, calls only that family, materializes the candidate gap
  slots once, and freezes `RouteMemberGeometryPlan` records. The convergence-owned
  legs of the same systems are emitted as immutable context and carried through
  those channel passes: a pass seats a channel on the rank it earns among every
  stroke resident in the gap, and the freeze makes that rank permanent, so a rank
  read from the candidates alone would be a different rank that no later pass can
  correct. `_bundle_divergent_distinct_traverses` runs here for the same reason,
  in the position it holds in the emission chain. Same-line coincidence and
  bundle clearance are separate owners over those claims:
  `_align_same_line_channels` seats a member on the claim of the carrier it
  extends, keyed by route system and gap, while
  `_allocate_bundle_around_claims` translates a whole bundle clear of the claims
  it does not share, so neither can undo the other.
- **Precondition**: The semantic scaffold, exit-turn and fan decisions,
  station offsets, layout coordinates, and any realised reservation bands for
  this routing pass are settled. No production member has been emitted.
- **Postcondition**: Each eligible member has a content-derived template ID,
  pre-normalization points and radii, offset and normalization policy, gap and
  trunk slots, semantic plan references, claimant-exact reservation IDs
  consumed by this pass, and every owned vertical gap channel identified by
  segment rank, exact coordinates, grid gap, row, and direction. Temporary
  mutable routes used to build the templates have been removed from the routing
  context.
- **Invariants preserved**: The canonical family is classified once. Planned
  production copies the seed and does not call its handler again. Declared gap
  channels remain exact; other seed geometry may enter named global passes.
  Convergence planning may construct candidates for members it owns. Final
  global settlement consumes non-convergence channels only from these frozen
  records. If one eligible member cannot produce a complete template, the
  system fails closed with the registered `member-geometry-plan` reason and
  none of its provisional templates reaches production.
- **Related tests**: `tests/test_member_geometry.py`,
  `tests/test_route_system_emission.py`, and `tests/test_route_plan.py`.
- **Lifecycle:** invariant - `validate_member_geometry_emission` checks that
  the final planned paths retain the owned segment ranks and exact coordinates
  of every frozen gap channel after normalization.

## Post-layout routing boundary: convergence planning

- **Purpose**: Give each complete semantic convergence one immutable target-side
  decision before route emission.
- **Helpers**: `_route_edges` calls `build_convergence_plan_execution` after
  exit-turn planning and before member construction. Canonical inter-section
  templates provide the candidate trunk, approaches, joins, and continuation
  geometry. Preliminary settlement publishes exact convergence claims to the
  mutable member allocator. Before final route-system disposition,
  `settle_global_convergence_execution` allocates preliminarily planned
  convergence systems against frozen `RouteMemberGapChannel` records and
  immutable prior convergence claims. Its channel sequence bounds a shared
  corridor from both sides: `_pack_cotravelling_corridor_runs` holds runs that
  co-travel one corridor at bundle pitch, `_settle_shared_trunk_channels` lanes
  apart the ones that need clearance, and both read the separation from
  `cotravelling_lane_clearance`, so a maximum and a minimum cannot disagree.
  Two runs count as one bundle only on local evidence: one route system, one
  travel direction, an overlapping span, a shared carrier junction or entry
  port, and either one named inter-row gap or a separation already inside
  `BUNDLE_TO_BUNDLE_CLEARANCE`.
- **Precondition**: The semantic route scaffold, exit-turn decisions, station
  offsets, layout coordinates, topology resolution, merge
  classification, and stable member family IDs are settled. Final global
  settlement additionally requires preliminary system dispositions and
  immutable planned member channels.
- **Postcondition**: Every supported convergence records complete authored and
  resolved membership, its merge and entry bundle, primary trunk and structural
  reason, axis, extent, flanks and terminal caps, stable feeder and lane order,
  opening-turn coordinate, exact joins, handedness, runway, continuation,
  resource conflicts, and endpoint ownership. Unsupported geometry records a
  declined convergence verdict. Incomplete semantic membership and programming
  errors fail the invariant.
- **Invariants preserved**: Planning does not move stations, ports, junctions,
  section boxes, or unrelated offsets. Templates consume plan-owned joins and
  covered continuations during emission. Coincidence and normalization passes
  may inspect but cannot move or replace plan-owned convergence geometry.
- **Related tests**: `tests/test_convergence_planner.py`,
  `tests/test_merge_branch_trunk_invariant.py`, `tests/test_route_plan.py`, and
  the frozen hash-seed fixtures.
- **Lifecycle:** invariant - every planned feeder retains its exact join, every
  trunk retains its planned axis, flanks and terminal caps, every emitted
  continuation ends at its owned endpoint, and every covered continuation names
  its carrier.

## Post-layout routing boundary: atomic route-system emission

- **Purpose**: Emit one complete semantic route system under one ownership
  disposition, in canonical system and member order.
- **Helpers**: `_route_edges` calls `classify_route_system_dispositions` after
  convergence classification to identify the complete geometry owner. After
  member planning and convergence settlement it calls
  `build_route_system_emission_execution` once to freeze the final atomic
  dispositions. The system loop calls `fresh_member_route` for each planned
  non-convergence member; convergence members consume their complete plan.
  Whole-graph rail mode freezes a dedicated execution before its direct rail
  emitter runs, then attributes and validates the returned rail paths against
  those canonical identities without synthesizing member-geometry plans.
- **Precondition**: The semantic scaffold, exit-turn, fan, member-geometry, and
  convergence decisions are complete. Layout geometry and route reservations
  are read-only.
- **Postcondition**: Every production system is wholly `PLANNED` and is emitted
  exactly once. Every canonical member has
  exactly one emitted path or one explicit valid coverage binding. Every final
  system path carries
  its route-system ID, emission-member ID, disposition, plan IDs, and
  claimant-exact reservation IDs. The system record carries their reservation
  union. A compatibility disposition is rejected before production emission.
- **Invariants preserved**: A planned family ID and its production seed are
  canonical input to emission. Post-passes may treat planned channels as fixed
  anchors but cannot move or replace their owned segments.
- **Related tests**: `tests/test_route_system_emission.py`,
  `tests/test_route_plan.py`, and the planner-specific suites above.
- **Lifecycle:** invariant - `validate_route_system_emission` checks the final
  routed paths after normalization and reports the system, connectors, member,
  plans, and reservations on any attribution mismatch.

## Post-layout render boundary: envelope settlement

Experimental measurements and rejected alternatives are recorded in
[`docs/dev/layout_settlement_design_record.md`](../../../docs/dev/layout_settlement_design_record.md).
They are design evidence, not part of this specification.

- **Purpose**: Give every grid boundary the width it owes, by translating whole
  grid rows and whole grid columns and nothing else. Two demands say what a
  boundary owes and both are settled by one translation apiece: the width a
  reserved corridor's `RouteReservation` requires, and the clearance a
  `BoundaryClearanceDemand` measures between the boxes facing across it, which a
  render-time box resize can eat with no run involved. A boundary carrying both
  is widened once, by the larger. Being the single owner of the translation is
  the point: no separate row push runs before or behind this stage to make up a
  shortfall it could have paid.
- **Helpers**: `settle_route_envelopes` (`layout/envelope_settlement.py`),
  driven from `_settle_render_geometry` in `render/svg.py`. Each pass
  re-measures live geometry through `realise_reservation`, and re-measures the
  clearance demands the same way and for the same reason -- a figure taken
  before its own earlier translations would be stale.
  `measure_row_gap_clearance` (`layout/phases/bbox.py`) states the row-axis
  box-clearance demands. Convergence planning can publish a column-axis
  `BoundaryClearanceRequirement` when a landing pair lacks runway, and
  `measure_boundary_clearance_requirements` re-measures it against live box
  edges. `measure_drawn_corridor_clearance` states any positive-side
  containment owed by the strict post-grant ledger. The demand vocabulary is
  `layout/settlement_demand.py`, held apart from settlement so a layout phase
  can state a demand without importing the routing stack the ledger is built on.
  Rail layouts raise no clearance demand: their row pitch comes from the
  interchange idiom rather than the declared section gap, and widening one of
  their boundaries to that gap turns a flat inter-row run into a staircase --
  a decision change, which `_assert_settlement_decisions_frozen` refuses.
- **The two demands can cover either axis.** The reservation ledger is settled
  on both: `_settle_axis` runs once per axis and every row-gap and column-gap
  claim is measured. Generic box-growth clearance remains row-only because
  `measure_row_gap_clearance` represents `push_lower_rows_after_bbox_grow`.
  Column demands are route-published and narrowly scoped: a
  `BoundaryClearanceRequirement` grants runway only for a provisional
  `NO_APPROACH_SETTLEMENT_ROOM` convergence, while a drawn-corridor demand pays
  positive-side containment measured from the strict post-grant route. Label
  wrapping does not independently create a column demand.
- **Precondition**: `compute_layout` has finished, routing has published the
  reservation ledger, render-time label wrapping has taken its bbox growth, and
  the header-collision reconcile has run. Local station geometry, section bbox
  sizes, port anchors, plan frames, lane orders, and author pins are frozen.
- **Allowed writes**: `Section.bbox_x` / `Section.bbox_y` and the `x` / `y` of
  the stations and ports those sections own, all by one shared per-boundary
  amount. Junctions live in inter-section space and are reproduced by routing.
- **Translation ownership**: A section belongs to the band holding its grid
  start, so a boundary owns every section starting at or beyond it. A section
  straddling the boundary starts above it and stays: carrying it would take its
  upper portion into the gap above and narrow that separation. Both sets are
  recorded on the `SettlementTranslation`, and holding a straddling section is
  sound exactly when it bounds none of the corridors the translation settled --
  if it did, the widening never reached them. That is asserted on the settled
  geometry, together with the monotone claim, by re-measuring every facing pair
  of boxes and every straddling section's corridors.
- **A corridor is not bounded by a box its own runs end inside**: a boundary is
  measured from the section edges facing it, and a section spans it -- occupying
  it rather than bounding it -- when its box crosses the boundary. A run whose
  last leg stops at a station of some box has entered that box just as surely, so
  `RouteReservation.landing_section_ids` names it and
  `_row_region_measurement` / `_column_region_measurement` drop it from both
  sides. Without that, an entry lead-in is charged `INTER_ROW_HEADER_CLEARANCE`
  off the header of the very box it is arriving at, which nothing can satisfy:
  the leg's own endpoint is inside the box, so no widening of the boundary brings
  it into band, and settlement spends real height chasing a demand that cannot
  close. The set is the *intersection* over a reservation's claims, because one
  reservation states one measurement: a box only stops bounding the boundary when
  every run sharing the corridor ends inside it, and a box one of them merely
  passes bounds it for all of them. Region *selection* is unaffected: it asks
  which boundary a run occupies, not what that boundary has room for.
  `reportho.metro`'s column 4/5 corridor is the case
  `test_a_box_only_one_claims_run_ends_inside_bounds_the_whole_reservation` pins,
  where a union would drop `report` from the measurement for all three of its
  claims and measure the corridor to a box edge two of its runs are stopped by.
  The reduction is pinned by identity because a union can only remove blockers
  and therefore weakens the capacity bound.
- **A corridor is bounded by the station its own runs launch from**: a
  pre-routing plan that emits its runs out of a station standing inside the gap
  fixes the length of the opening leg and refuses emitted geometry that shortens
  it, so no widening of the far side brings the run any nearer to that station.
  `RouteReservation.launch_anchors` names it with the runway it owes, and
  `_launch_anchored_measurement` folds it into the region edge on the side of
  the run it stands on: the band the reservation states is then the band the
  plan is free to occupy, and the width the boundary is asked for is the width
  that band needs. Without it the measurement reads the departed box's edge, a
  proxy that sits behind the launch station, and states a band the plan cannot
  reach -- the mirror of the landing case above, and the reason
  `_route_planned_bottom_exit_right_landings` can seat its traverse in the band
  its own reservation realises instead of at a floor the ledger disagrees with.
  The set is the intersection over the reservation's claims, for the same reason
  `landing_section_ids` is. Settlement is unaffected by this blocker: an anchor
  stands on the side a translation holds still, so the ownership lemma below
  gives the corridor the full widening it asks for.
- **The width a boundary is asked for holds every corridor confined with or
  nested against each one**: a reservation's `minimum_width` is
  `negative_side_clearance + bundle_width + peer_width + positive_side_clearance`,
  and `peer_width` (`_peer_widths` in `layout/route_reservations.py`) is what the
  corridors sharing the boundary take beside this one. Two corridors crossing one
  boundary compete when their runs overlap along it (`spans_share_corridor`) and
  either of two further things holds. They are **confined**
  (`_lanes_are_confined`): neither one's own measured band can hold them the
  distance apart they need, so no seating draws both until the boundary grows.
  That reach is measured in the order the pair is drawn in, since that is the
  only order any seating may produce: the router moves a corridor up to its
  neighbour's lane and never past it, so crediting the pair with the better of
  the two orderings would report a boundary settled that in fact has no seating
  at all. Or they are **nested** (`_lanes_are_nested`): drawn travelling the same
  way no further apart than the clearance between them, so whatever the two bands
  could reach, the boundary carries the pair at the pitch it is drawn at until one
  of the two has moved. That second test is bounded by the clearance rather than
  by the boundary's own edges, which keeps a lane drawn elsewhere in a wide gap
  from being charged for at the distance it happens to lie away, and it excludes
  counter-running pairs, which are separate bundles rather than tracks of one
  stack and so are the reach test's business. Confinement alone leaves a boundary
  stated at the width its own lanes would take with an interloper drawn between
  them already gone -- nothing the interloper divides can then be brought
  together without a claim overrunning its band, so the reorder that would take
  the interloper out of the way is refused and it stays between them, as
  `tests/fixtures/curve_invariant_repros/inter_row_corridor_overflow.mmd` records.
  Where they do compete, the demand is the stack in drawn order: each
  neighbouring pair contributes `cotravelling_lane_clearance`
  (`layout/geometry.py`), which states in one place what
  `_required_channel_clearance` asks of counter-running channels and
  `_overlays_distinct_line` of co-travelling ones -- nothing between two tracks of
  one line running together, `OFFSET_STEP` between distinct co-travelling lines, a
  turn radius between a line and its own return leg, `BUNDLE_TO_BUNDLE_CLEARANCE`
  between counter-running distinct lines -- and never less than the pair is
  already drawn at, so a widening cannot be answered by bringing the pair
  together. Each competing reservation states the same stack, so settlement's
  per-boundary maximum widens the boundary once for all of them and the
  single-sweep argument below is untouched: a larger `minimum_width` is a larger
  capacity deficit and nothing else.
  A claim is not what makes a stroke take room, so the stack holds every leg
  drawn in the boundary and not only the filed ones. The region search asks which
  boundary a leg *crosses*, and a leg that dips into a gap and returns to the row
  it left crosses none -- it is drawn in that gap all the same. Such a leg is
  charged against a boundary whose measured gap its coordinate falls inside and
  whose own claims travel a stretch of corridor it shares, reading that
  boundary's band because it holds no reservation of its own. It is charged as a
  peer rather than filed as a claim because a reservation is a corridor a run may
  be *seated in*, and a leg no boundary crosses has no such corridor to be held
  inside: filing one would state a band a frozen route shape cannot reach, and
  gate its containment against a corridor it never enters. Charging only the
  filed lanes states a boundary wide enough for one stroke where two are drawn,
  and the second is left wherever the narrow gap forced it -- in
  `examples/topologies/merge_around_below_leftmost.mmd`, a merge trunk's return
  leg 14px below a box edge that asks `INTER_ROW_EDGE_CLEARANCE` of it, ungated
  because it carries no claim.
- **Postcondition**: No boundary still owes what it was measured for. For a
  clearance demand that is one count, re-measured on the settled geometry by
  `_assert_clearance_demands_are_met`: it follows arithmetically from the
  ownership lemma below, and is checked anyway because the lemma is a property of
  two predicates staying in step. For a reservation, every row-gap and
  column-gap reservation *contains* the run
  drawn in it. Containment is three counts, all of which
  `assert_reservations_are_settled` refuses on the strict path: non-negative
  capacity slack (the region is wide enough at all), and non-negative slack on
  each side (the run is drawn inside it, not seated off centre with one side
  absorbing the whole surplus and the other overrun). The two counts read
  different evidence, and must. Capacity is a property of the reservation and the
  settled envelopes, so it is measured by re-realising the reservation against the
  ledger settlement was handed. Where in the region the run *sits* is only
  knowable from the emitted polylines: the published ledger records the demand --
  frozen claims projected through the translations -- so its occupied interval
  states where the first pass observed the run, not where the settled re-route
  drew it, and a boundary widened so that the re-route can move into the new room
  leaves that interval untouched. The side slacks are therefore measured by
  `drawn_corridor_containment` on the polylines the renderer is about to draw,
  through each claim's own `(path_rank, segment_rank .. segment_end_rank + 1)`
  point range; `_settle_render_geometry` builds them once and hands the same list
  to the guard and to the renderer. That the frozen plan's ranks still name the
  re-routed geometry's points is what `_assert_settlement_decisions_frozen`
  already guarantees: it compares one signature entry per point pair, in route
  order, so equal fingerprints mean equal route order and equal point counts, and
  `apply_route_offsets` maps points one for one.
  Capacity holds unconditionally, for every arrangement an author can express, by
  the **ownership lemma**: `_row_region_measurement` splits the sections beside
  boundary `b` into an upper set `{row_end(s) <= b-1}` and a lower set
  `{grid_row(s) >= b}`, and `translation_ownership(b)` moves exactly
  `{grid_row(s) >= b}` and holds everything else. Those are the same inequality,
  so a translation raises the corridor's `end` by its full amount and leaves
  `start` fixed: the corridor widens by exactly what was asked. Columns are the
  same statement on `grid_col`. The premises are that an actionable
  `amount = max(2, ceil(deficit / SETTLEMENT_QUANTUM)) * SETTLEMENT_QUANTUM >=
  deficit` (`quantised_allocation`), so the allocation clears the coordinate
  tolerance that `ReservationCoordinateTranslation` refuses; that translations
  are unbounded above; that no directive pins a canvas coordinate
  or a maximum separation (`grid:` fixes grid indices,
  `section_x_gap`/`section_y_gap` are floors, `width`/`height` size the viewport,
  and `legend:` is not a corridor
  blocker); that row and column offsets are cumulative sums over ascending
  index, so "A above B" implies `A.grid_row <= B.grid_row`; and that section
  sizes are frozen between settlement and the guard, `shift_section` writing only
  origins. The same inequality covers a `BoundaryClearanceDemand`: every box its
  shortfall is measured *from* ends at row `b-1` or above and every box it is
  measured *to* starts at `b` or beyond, including the bypass-span and
  row-envelope variants, whose deeper edge belongs to a section in the upper row.
  Consequently the strict deficit path is a backstop against ledger or
  ownership drift rather than an authoring outcome: an "infeasible pinned
  arrangement" is not a state this model admits. The guard stays because the
  lemma is a property of two predicates staying in step, which a future edit
  could break. `tests/test_envelope_settlement.py` measures the lemma directly
  over one fixture per pin class -- explicit grid, row span, column span,
  inferred span, fold-driven rows, and all four flow directions -- asserting that
  a boundary's negative blockers are disjoint from the sections its translation
  moves, its positive blockers are contained in them, and no blocker straddles
  the boundary. A boundary that every relevant section spans
  across has no side to measure, so it is never selected as a corridor's region
  in the first place -- the measurement bounds a boundary by the sections lying
  wholly on each side of it, and raises otherwise. Settlement therefore allocates
  against a plan in which every convergence states its own geometry: no
  convergence plan the corpus produces carries a `legacy_reason`, and
  `test_every_corpus_convergence_is_planned_not_left_to_compatibility` holds that
  over every routable fixture. This stage has no compatibility population to
  attribute, so it publishes only its own translations, one
  `envelope-settlement-translation` diagnostic per widened boundary naming the
  demand that sized it.
- **Origin-independence**: The width a boundary is widened by is a function of
  its deficit and nothing else, so one arrangement described at two canvas
  origins allocates identically. This is the quantisation lemma's other half,
  and neither half is sufficient alone. `amount >= deficit` on its own permits
  an allocation that follows the coordinates a gap happens to be measured
  between: a gap is a difference of two box edges, binary64 subtraction of two
  coordinates carrying decimal fractions leaves an error set by the magnitude of
  the operands rather than by the distance between them, and `ceil` amplifies
  whatever it is handed into a whole `SETTLEMENT_QUANTUM`. The two halves hold
  together because the resolution belongs to the measurement rather than to the
  ceiling: `measured_distance` (`layout/route_reservations.py`) states every
  ledger width and every containment slack at `COORD_GROUP_DIGITS_FINE`, two
  orders of magnitude finer than `COORD_TOLERANCE_FINE`, so the ceiling
  allocates no less than the deficit it is handed and the deficit it is handed
  is the one the geometry states. A `RealisedRouteReservation`'s own two side
  slacks are raw subtractions, because every consumer reads them against
  `COORD_TOLERANCE`, a band 1e13 times that error: the resolution is owed where
  a reader is finer than the tolerance, which is the ceiling here and the sign
  test in `drawn_corridor_containment`.
  `test_the_allocation_is_a_function_of_the_deficit_not_the_canvas_origin` holds
  the property over both axes and establishes rigid translation before comparing
  allocations, so it measures the quantiser rather than route-shape changes.
- **Invariants preserved**: No row or column separation decreases. Section
  sizes, a station's position within its section, plan-owned frames, lane
  order, port sides, and author-pinned grid relationships are unchanged.
- **How settlement's self-checks fail**: every one of them --
  `_assert_no_separation_decreased`,
  `_assert_spanning_sections_bound_nothing_settled`,
  `_assert_the_column_phase_left_the_row_phase_standing`,
  `_assert_clearance_demands_are_met`, and the boundary with no translation owner
  -- raises `PhaseInvariantError`. Each states a conclusion the ownership and
  monotonicity lemmas above establish, so a violation is engine drift and not
  something an author can express in a `.mmd`; the type puts it inside
  `NfMetroError` alongside the mid-layout guards of the same class, and
  `render_string` documents it. None of them is gated on `graph.strict` or
  downgraded under `graph.permissive`: a best-effort diagram drawn past a broken
  allocation lemma is a diagram whose geometry nothing vouches for. The write is
  transactional, so the pre-settlement coordinates are restored before the error
  propagates.
- **Out of scope**: Canvas-side corridors, whose far boundary is the canvas
  edge rather than a grid neighbour; closing one grows a margin, which no row
  or column offset owns. The render owns that margin instead:
  `_settle_clear_of_the_canvas_margins` (`render/svg.py`) measures the ink that
  lands outside the section-box envelope on the two sides whose edge is pinned
  at zero, moves the whole map away from the edge by the shortfall
  (`translate_graph`), and re-derives the geometry on the moved copy; the far
  margins grow the canvas by the same amount, so the map keeps a first-quadrant
  frame and the zero edge a canvas measurement reads stays the zero edge. The
  move is rigid and post-routing, which is why it is not one of settlement's
  per-boundary allocations. Canvas corridors are gated separately, by
  `assert_canvas_corridors_hold_their_claims`, which runs once the render has
  sized its canvas -- the first point at which the number a canvas claim is
  measured against exists, and the reason the settlement guard could never
  measure one. A run is filed against a canvas side only when it lies beyond the
  extreme of every placed section, so the margin it is measured within is the one
  it occupies, and its clearance on that side is `CANVAS_EDGE_CLEARANCE`: the
  stroke's half-width plus a direction chevron, which is what is drawn there,
  scaled through `canvas_edge_clearance()` because `stroke_scale` multiplies the
  stroke but not the chevron's arms. A turn radius is not, because an arc beside
  the canvas is inscribed inboard of the centreline. The guard gates on that
  margin -- `canvas_edge_slack`, the room between the ink and the edge -- and on
  total capacity, which are different claims: a corridor can hold every pixel it
  reserved and bank all of it on the side facing content, leaving its stroke and
  chevron drawn through the margin and clipped by the viewport. The content
  boundary is resolved after header placement, against the final route
  polylines. A section box contributes its edge only over the corridor's declared
  longitudinal interval. A header contributes its drawn keepout only where that
  keepout overlaps the same interval and protrudes toward the corridor. The
  effective clearance is `INTER_ROW_EDGE_CLEARANCE` for horizontal box edges,
  `EDGE_TO_BUNDLE_CLEARANCE` for vertical box edges, and
  `SECTION_HEADER_ROUTE_CLEARANCE` for header ink. The corridor-normalisation
  pass seats movable canvas runs inside the corresponding box-edge band. Planned
  right-entry fans include their full outer-lane offset when placing their owned
  descent channel. `assert_canvas_corridors_hold_their_claims` gates the minimum
  of canvas-edge slack, content-side slack, and total capacity slack, so a
  content-side graze fails the same strict path as a clipped canvas margin.
- **Transactional**: The pre-settlement coordinates are restored before any
  exception propagates, so a failure leaves the graph as settlement found it.
  The reservation ledger is read-only here.
- **Idempotence**: Repeating one settlement against the same ledger and demand
  measurement finds no positive deficit and writes nothing.
- **Termination**: Each settlement runs once against one ledger. A pass visits
  each adjacent-index boundary once in ascending order; translating everything
  from boundary `b` onward widens `b` by exactly that amount, leaves earlier
  boundaries' blockers stationary, and moves later boundaries' blockers
  together, so boundaries do not interfere and the pass is finite in the number
  of boundaries. Ordinary routing settles one frozen ledger, then adopts it for
  the closing guard. A provisional convergence-clearance requirement uses a
  bounded two-ledger sequence instead: settle the provisional grant, observe a
  strict plan that may not publish another requirement, settle that plan once
  for drawn positive-side containment, then re-route while consuming it. The
  final decision-freeze check compares the strict plan with that consuming
  re-route. There is no retry or fixpoint search. A requirement surviving the
  first strict observation, or any deficit surviving final settlement, is an
  invariant failure.
  `attach_reroute_ledger_delta` compares each corridor's description together
  with the width it asks for, since a boundary whose corridor survives at a
  different `minimum_width` is one the translations were sized wrongly for.
  The decision freeze includes coordinate-independent system, member, family,
  plan, coverage, and declared channel ownership. Final routes are rebound to
  the claimant-exact reservation IDs of the ledger they consumed and validated
  against the published plan.
- **Consumed by**: the re-route. `_settle_render_geometry` hands the
  pre-settlement ledger back to `observe_route_edges_centred` whenever it holds
  any reservation, which builds `ReservedCorridors`
  (`layout/routing/reserved_bands.py`) by re-measuring each row-gap and
  column-gap reservation on the settled geometry. One axis-neutral measurement
  serves both, keyed by the higher grid index the boundary separates (the lower
  row, the right column). `_center_inter_row_channel` and
  `centre_inter_column_channel` place a claimed channel inside that band rather
  than deriving one from the row or column edges, and a published band always
  holds a channel, so a claimed corridor cannot take the narrow-gap fallback.
  Where a handler or normalisation pass sizes a channel from the boxes it has to
  hand -- `bypass_bottom_y`'s trunk depth, the L-shape and wrap clearance
  floors, `_clamp_inter_row_band_top`'s stack limit -- that proxy is applied
  through the band (`held_in_reserved_band`) so the reservation's answer wins
  where the two disagree. A boundary whose claims intersect to nothing, and
  every gap the ledger never reached, keep the row- or column-edge derivation.
- **A band bounds a corridor, it does not assign it a lane**: every claim
  crossing one boundary realises the same band, so corridors placed in it
  independently cannot see each other and two can settle less than one
  `OFFSET_STEP` apart, which draws two distinct lines as a single two-tone
  stripe. `_separate_fused_cotravelling_runs` closes the pass chain by
  restoring the step across every corridor at once, moving a whole track (each
  run of one line on one lane through one corridor) so a fused fan-out cannot
  be split, and never moving a track a plan owns.
  Plan-owned trunks take the same distinct-line pitch during global convergence
  settlement, before member geometry and convergence geometry freeze.
  `check_no_fused_cotravelling_lines` is the final postcondition on every track,
  including a pair where both coordinates are immutable. Such a violation names
  its route system and exact owning plans, which identifies the construction
  stage that failed instead of inviting a post-freeze repair. The empty
  `EXEMPT_FUSED_PAIRS` ledger in
  `tests/test_fused_cotravelling_lines_invariant.py` asserts that the corpus has
  no silent pair outside this live contract.
- **Containment is closed on the drawn geometry, not in the handlers**:
  `ReservedCorridors` answers "what is clear at this boundary", which is the
  intersection of every claim crossing it. That cannot separate two corridors
  crossing one boundary in opposite directions -- their intersection is narrower
  than either, sometimes a single coordinate -- so a pass allocating several
  corridors across one boundary at once (`_materialize_gap_slots`) keeps the raw
  gap instead. Eight post-passes therefore position channels without reading the
  ledger: `_separate_opposing_inter_row_trunks`, `_materialize_trunk_slots`,
  `_spread_diagonal_bundles`, `_bundle_divergent_distinct_traverses`,
  `_coincide_fanout_opening_descents`, `_stagger_convergent_distinct_lines`,
  `_coincide_same_line_tracks`, `_materialize_gap_slots`.
  `_hold_runs_in_corridor_clearance` closes the difference last instead, on the
  routed geometry. **A leg the ledger claims is held inside its own claim's
  realised band**, read through `ReservedCorridors.for_segment` by the claim's
  `(source, target, line_id, segment_rank)` identity, which is the same band the
  closing guard scores it against. Consuming the reservation rather than
  re-deriving one is the whole point of having allocated it: settlement widened
  that boundary for this corridor over the corridor's own declared span, and a
  band read back off live geometry can only ever confirm wherever the leg already
  sits. A leg no claim names has no reservation to consume and keeps the gap
  measurement (`gap_corridor_clearance_band`, which states the reservation's
  arithmetic against live geometry), which is what the first routing pass -- the
  one that publishes the ledger and has none to read -- runs on.
  Bundles move rigidly and only into the space their gap-mates leave them, so no
  move fuses two lines onto one stroke. How much room a pair needs is
  `cotravelling_lane_clearance`, the same rule the ledger sizes boundaries by, so
  a corridor is never denied a coordinate the ledger allocated it on a separation
  the ledger did not charge for. A bundle every shift is denied retries with the
  peers denying it as one rigid group: two corridors owed one boundary between
  them are seated by the same widening and neither can reach it alone, and a
  rigid move leaves every separation inside the group exactly as drawn.
  Every realised gap claim is drawn inside the band its own reservation
  realises. `tests/test_reserved_claim_consumption.py` holds the whole corpus to
  that invariant and pins the two claims accepted within tolerance by identity:
  the `hic_reads` lane turning up into `scaffolding` in
  `examples/genomeassembly.mmd` and in its organellar twin. Each is drawn 1.00px
  past its inter-column channel's positive edge, inside the `COORD_TOLERANCE`
  the bound allows. Their channel's lowest lane is a
  planned exit turn's descent, standing 4px above the band floor, and the stack
  seated from it takes 15px of the band's 18. That shortfall is a position rather
  than a width -- the reservation's own `minimum_width` is met with 14px to spare
  -- and settlement cannot pay it in any case: `SETTLEMENT_QUANTUM` is
  `COORD_TOLERANCE`, `_settle_axis` acts only above it, and
  `ReservationCoordinateTranslation` refuses an amount that small, so the least
  translation this stage can express is 2px and a 1px deficit is below the
  resolution the ledger works at.
  A merge feeding a TOP or BOTTOM entry port is seated on the vertical lead-in
  that port receives (`_position_merge_junction`). This puts its feeders in the
  row corridor they claim and the merged trunk in the column the port's own
  crossing gives that line. Three conditions govern that column.
  **The drop lands where its siblings land.** The junction-to-port hop is seated
  on `_perp_entry_landing_x` -- the port-crossing X the intra-section departure
  leaves from and every bundled feeder lands on -- and ends on the port's own
  edge. Carrying the lane offset along the axis the hop travels instead runs it 4
  and 8px past the boundary for `tumor_only` and `somatic`, on a column no
  sibling stands in; `_shared_terminal_axis` then finds no feeder terminating
  where the hop does, and the plan falls back to `OUTGOING_CONTINUATION` with its
  trunk disagreeing with its own landings.
  **A corridor shared with an unowned member is one system construction.** A
  same-line member landing on the plan's entry port is grouped by
  `_convergent_port_groups`; `_coincide_same_line_tracks` uses the planned
  channel as its fixed reference and seats the unowned approach onto it. The
  member-geometry planner freezes that approach's materialized gap channel.
  Final convergence settlement consumes the frozen channel as an allocation
  input instead of trial-routing the member and
  rejecting a collision that the production coincidence pass removes. A member
  with a genuinely separate corridor remains separate rather than being
  inferred from overlap.
  **A plan claims the segments its axis describes, and no more.** A trunk axis
  collapses its flanks onto its own coordinate when the trunk turns straight into
  the port, and a zero-length flank must not be matched as a run: doing so claims
  every leg passing through the corner it states -- here the
  horizontal runway -- and through `convergence_owns_segment_boundary` the
  feeder's opening descent before it. That takes the descent out of
  `_divergent_source_groups`, the pass that fuses each line's descents at one
  source onto the column its bundle occupies there, and the feeder stood one lane
  off its own colour: three doubled strokes over 40-60px, each overlapping a
  neighbouring line's lane. The corner itself stays owned by the boundary rule
  around the trunk's own run, so only coordinates the axis never stated are
  handed back. For the same reason the landing states **no**
  opening turn where a bundle outside the convergence seats its column
  (`_bundled_sibling_owns_opening_column`): `_divergent_source_groups` draws its
  reference from the bundled members, and a lone feeder's own handler column is
  not the plan's to freeze.
  **The convergence family states its own geometry.** No declined convergence
  verdict reaches emission as an alternate path: a
  `ConvergenceConflictKind` is not a registered compatibility reason. Three of
  the six conditions were retired for being unable to state a case at all.
  `CHAINED_SAME_LINE` compared two same-line trunks and asked them to sit within
  one channel's lanes, but `parser/route_topology.py` builds one convergence group
  per `(entry_group_id, line_id)`, so two distinct plans never share both keys;
  the pairs it reached were one stroke branching to several destinations, and the
  separation it measured was the row pitch between the sections served.
  `UNOWNED_MEMBER_CORRIDOR` and `UNOWNED_MEMBER_GROUP` read an edge as unowned
  when no convergence plan listed it, but
  `build_route_system_emission_execution(require_member_geometry=True)` requires
  every member of a planned system to hold exactly one geometry decision, so such
  an edge is owned by a member-geometry plan and the category is empty by
  construction.
  The four that remain state real cases, and each names a **pair** of runs.
  `NO_APPROACH_SETTLEMENT_ROOM` is the one width insufficiency: planning can
  publish its exact landing-flank runway requirement for envelope settlement,
  and the strict post-grant observation must return without another request.
  `SHARED_TRUNK_CHANNEL`, `SHARED_APPROACH_CHANNEL`, and
  `OPPOSING_OPENING_CHANNEL` describe incompatible ownership of a channel, not
  missing boundary width. They remain hard refusals in
  `_validate_final_convergence_feasibility` once every movable decision is
  frozen. Retrying those classified templates would state nothing the plan did
  not and lose the attribution it carried.
  **Construction failures are not dispositions.** Canonical convergence
  construction either returns complete geometry or raises at the condition that
  broke its contract. Only an upstream exit-turn ownership conflict produces a
  non-owning convergence diagnostic. At the template-selection site, a private
  absent-axis signal selects the outgoing continuation as trunk. Sites that
  require a shared terminal axis convert the signal to a descriptive construction
  error.
  Every decision the passes below **can** make they do make, and from the pair
  rather than from whichever run arrived last: laning only the arrival leaves
  one line's outward and return legs on one column whenever the arrival has no
  reachable lane, so a resident with the runway gives way instead. This stage's
  part is to charge for the result and nothing
  more. `_settle_shared_trunk_channels` lanes the runs of one route system's
  trunks. Each convergence plan reads its trunk geometry off a trial route taken
  with no knowledge of its siblings, so two plans of one system whose trunks take
  the same channel derive the same coordinate and each believes the channel is its
  own; the system assigns the lanes, by `cotravelling_lane_clearance` -- a full
  turn radius between a line and its own return leg, and nothing between two runs
  going the same way, which stay one fused stroke. Both channels a trunk shares
  are laned by that rule: the one its central run travels, and the one its flanks
  turn out into.
  Three properties of that decision matter here.
  It publishes **no demand of its own**. A lane is a drawn stroke, so the boundary
  carrying it is charged for it exactly as every other stroke is: the second lane
  is a run in the row gap, `_peer_widths` reads it, and `minimum_width` states the
  pair. `test_a_boundary_is_charged_for_the_unfiled_leg_drawn_in_it` asserts that
  the reservation's peer width equals the lane separation, its minimum width is
  the sum of all clearance terms, and the realised gap meets that minimum.
  `BoundaryClearanceDemand` is for a boundary owed clearance by something that
  is *not* a drawn run, so it is the wrong vocabulary for a lane and is not used.
  It is taken on the **first** routing pass, so this stage realises a demand
  against a plan that already exists. `_assert_settlement_decisions_frozen` is
  therefore unmodified and holds: disposition, membership, lane order and frame
  are identical either side of the sweep. Lanes are measured from the trunk that
  arrived first rather than from a boundary edge, so widening the boundary moves
  neither of them, which is what makes the separation invariant under allocation
  instead of growing at half the widening rate.
  Two corridors that compete at one boundary are not a source of residue either,
  whether they are confined there or nested against one another: `peer_width`
  states the room they take together, so settlement widens the boundary for both.
- **Related tests**: `tests/test_envelope_settlement.py`,
  `tests/test_reserved_corridor_placement.py`, and
  `assert_reservations_are_settled` in `layout/phases/guards.py`.
- **Lifecycle:** invariant - the settled geometry satisfies every reservation
  settlement owns, and re-running it changes nothing.

### The settlement stage trace

`SettlementStage` (`layout/route_plan.py`) is the vocabulary a render uses to
say how far settlement has got. Its declaration order *is* the settlement
order, which `SETTLEMENT_STAGE_ORDER` reads off it: `DISCOVERY`,
`GENERAL_SETTLEMENT`, `COHORT_INTENT`, `APERTURE_SETTLEMENT`, `FINAL_SOLVE`,
`TYPED_MATERIALIZATION`, `COHORT_FINAL`, `VALIDATION`.
`register_settlement_stage` appends one `SettlementStageObservation` to the
append-only `SettlementStageTrace` a published `RoutePlan` carries, and refuses
an observation that breaks the protocol: a trace begins at `DISCOVERY`, its
stage rank never decreases, `GENERAL_SETTLEMENT` is the only stage that may
repeat, and every observation names a non-empty geometry fingerprint.
`route_observation_rank` numbers the observations of the two stages that *are* a
route observation - `DISCOVERY` and `GENERAL_SETTLEMENT`, named by
`ROUTE_OBSERVATION_STAGES` - in the order they were taken, and is `None` for
every other stage. The stage decides it, so no caller can disagree with it.

The render path (`_settle_render_geometry` and the publish step in
`render/svg.py`) records `DISCOVERY` for the first route observation and
`GENERAL_SETTLEMENT` for each re-observation a carried port, a header-collision
reconcile, or an envelope-settlement translation forces. `COHORT_INTENT`,
`APERTURE_SETTLEMENT`, `FINAL_SOLVE` and `TYPED_MATERIALIZATION` are positions
the vocabulary reserves between them and the closing pair; no render records
one, which `tests/test_corridor_cohort_integration.py` asserts alongside the
frozen order.

`COHORT_FINAL` sits *after* both closing acts, not between them: the final
canvas geometry is published first (`_FinalPublishedGeometry` - canvas width and
height, header keepouts, route polylines and radii, labels, legend and logo
boxes), then `realise_route_reservations` realises the reservation ledger
against that canvas, and only then is `COHORT_FINAL` recorded, fingerprinting
the realised state. `VALIDATION` re-reads the same published state and is
refused unless a `COHORT_FINAL` record exists with an identical fingerprint, so
nothing may move the geometry between the two - the trace is what makes "the
published geometry is read twice and read the same" a checked claim rather than
a convention.

### A port travels with the box edge it is anchored to

Seating a label grows its section box outward (`_clamp_label_to_section`,
`_place_tb_label`, `_grow_section_for_box`), and that growth is render-time: the
wrapped text and the marker positions routing centres are not known until the
render path has both. A port's side names the edge it is pinned to, so an edge
that moves without it leaves the port inside its own box, its inbound run
crossing the drawn border and traversing the interior to reach it.
`carry_ports_with_section_edges` (`phases/ports.py`) therefore moves every port
already on a moved edge by that edge's displacement, at the step that moves it,
and `_settle_render_geometry` re-observes the routes so each still terminates on
its port.

The re-observation is one step, not a fixpoint. Routing centres a station marker
on its flat run, so lengthening that run by moving the port can move the marker,
its label, the section edge, and the port again. A label pass with no
re-observation behind it gives its growth back on anchored edges through
`hold_port_anchored_edges`, leaving the port where the drawn runs land and the
label seated within its bbox margin. This also prevents post-settlement growth
from consuming reserved corridor clearance.

### A caption's reserved band is the one on the side it took

`SECTION_HEADER_PROTRUSION` above a box top is the prospective band the layout
reserves for that box's caption, and `section_header_top` states it. Gap routing
is charged against it (`section_header_safe_cap`, `INTER_ROW_HEADER_CLEARANCE`)
before any caption has a position: the caption is picked from the routed
polylines it has to avoid, so routing against the final caption would require a
route-place-route fixpoint. What that prospective reservation buys is that the
caption's default top-left position is available. Final canvas-corridor
realisation is retrospective instead: it reads the chosen header keepout and
the emitted run's longitudinal span, then treats only ink that overlaps that
span as a blocker. A caption placed elsewhere therefore does not create empty
reserved space beside the canvas run.

A fixed band above `bbox_y` is therefore the wrong thing to hold a *drawn* caption
to, in both directions. It is too small: a wrapped title grows away from the box
until it reaches the map title or the box above (`_max_lines_upward`), and even a
single line can pass the prospective reservation as its font grows. It is also
in the wrong place for a caption drawn below or beside its box, while the gap
that caption occupies is the one that has to hold it.

`header_band_room` (`render/section_header.py`) therefore states the band from the
placement, on whichever side the caption hangs off: down to whatever stands above
the box and never less than the default position's own reach; up from the box
bottom to the next row's top less the `SECTION_HEADER_PROTRUSION` that box
reserves for its own badge; or out to the section beside. `header_band_protrusion`
states how far the ink reaches into it, the resolver only offers a side whose room
holds the caption, and `check_section_headers_hold_the_reserved_band` re-reads
both off the drawn placements and refuses the render with
`SectionHeaderBandError` otherwise. The guard establishes containment, not that
the band is empty; title width is governed separately by
`check_section_headers_fit_box_width`.

Stating the band per side is what lets a caption take the clear side. An
uncontested default position wins outright; once a route crosses it, the band slot
and the bottom edge are ranked by the room each keeps from route ink, with a
rotated side header a lower tier below both (see the module docstring).

### Tier-A layout guards read the settled geometry

`assert_render_layout_invariants` runs once per render, next to
`assert_render_header_clearance`, on the routes and offsets the renderer is
handed. The guard therefore certifies the geometry after label placement,
header reconciliation, and settlement, rather than an intermediate routing
observation those stages can move.

## Cross-stage contract: semantic fan planning

- **Purpose**: Give one immutable owner to a complete authored fan or diamond,
  including its branches, opening and landing order, relative lanes, runway
  demands, exact offset slots, centreline members, and dedicated route
  emissions.
- **Helpers**: `build_fan_plan_execution` runs before Stage 1.
  `_apply_planned_fan_port_geometry` seats owned boundary anchors.
  `_snapshot_planned_fan_centrelines` freezes each settled structural
  centreline before `_apply_planned_fan_geometry` materialises the relative
  frame at Stages 4.9 and 6.17. Routing applies `FanOffsetCarrier` assignments
  before dispatch.
- **Precondition**: Authored connector identity and resolver lineage are
  complete. Effective grid decisions are available even though section canvas
  coordinates are not.
- **Postcondition**: A fan is wholly `PLANNED` or wholly `LEGACY`. A planned
  fan has exact structural ownership and complete relative geometry. A
  symmetric two-way fan uses mirrored lanes around one centreline; structural
  continuation identity does not convert that appearance into a trunk-plus-peel
  frame. A straight reconvergence consumes the established section tracks,
  including phantom and collision-compacted slots, as its complete fixed frame.
  Its absolute centreline source is fixed by the planner, so later grid, port,
  or topology mutations cannot select another anchor. A legacy fan claims no
  layout geometry, offsets, anchor, or route emissions and records one
  deterministic reason.
- **Invariants preserved**: Planned materialisation reads frozen anchors and
  cannot move an unowned port or station. Structural membership is independent
  of route-emission ownership. Each claimed route emission is produced exactly
  once and carries its plan and emitter identity.
- **Related tests**: `tests/test_fan_plans.py` and the fan-plan topology
  fixtures listed in `examples/topologies/README.md`.
- **Lifecycle:** invariant - the same fan decision is consumed by layout,
  offset assignment, routing, validation, and diagnostics for one layout pass.

## Unclear / structural-debt signals

No open signals at this time. Add new entries here when phase
pre/postconditions reveal a candidate for cleanup.

## Adding a new stage: checklist

When adding a new stage to `_compute_section_layout`, document the
following before merging:

1. **Stage tag**: pick the next sequential number within the
   appropriate stage (e.g. a new Stage 6.x sub-step gets the next
   integer after Stage 6.16). Use the flat Stage.N scheme.
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
   defend the postcondition. If none, add one.
7. **Validate-mode coverage**: if the stage introduces a new property
   that should hold permanently, add a `_guard_*` helper and call it
   from `validate=True` mode.
8. **Update this doc**: extend the per-stage table above and call out
   any cross-stage coupling in the structural-debt section.
