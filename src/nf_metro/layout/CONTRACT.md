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
assignment (`single_section.py`). Every heuristic written against the LR
*interpretation* of `x`/`y` (horizontal trunks, layers spread along X, lines
stacked along Y) is wrong-by-default for TB, and the historical fix was to
hand-write a one-off `if direction == "TB"` mirror per heuristic. That count
only grew.

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
which names a section's axes without needing spacings). It replaced the
historical mix of `direction == "TB"` and `direction not in ("LR", "RL")`
exclusions in `row_align.py`, `grid_snap.py`, `_common._section_trunk_y`, and
`section_placement.py`, and underlies `_common._is_fold_section`
(`grid_row_span > 1 or not lanes_run_along_y(...)`), the row-fold predicate that
routes a section's exit ports through the fold path rather than the row passes.

**Deliberately left direct (not contortion-migrated).** Per the same judgement
as the in-section migration, a *single-branch* TB-only heuristic with no LR
mirror gains no polymorphism from `AxisFrame` - expressing its reads as
`frame.primary`/`frame.secondary` would just rename `.x`/`.y` inside code that
only ever runs for one direction. These stay direct in `phases/ports.py`:
`_align_tb_entry_port` (its TB-trunk branch; the function also serves the LR/RL
perpendicular case), `_clamp_tb_entry_port`, `_resolve_tb_exit_y`,
`_align_tb_section_bbox_bottoms`, and `_tb_trunk_x` (the secondary-axis trunk
coordinate is a *median* for a vertical section but the bundle-connected topmost
for a horizontal one - `_section_trunk_y` - so the two are not the same code and
should not be forced behind one name). The `section_placement.py` RL-or-TB
column right-alignment and `_apply_tb_fold_spans` selection are domain
groupings, not axis swaps, and likewise stay.

## Validate-mode guards

`compute_layout(validate=True)` runs these guards at fixed checkpoints:

| Checkpoint | Guards |
|---|---|
| after Stage 1.1 | `_guard_section_bboxes_positive` |
| after Stage 2.1 | finite coords, stations-in-sections, bboxes-positive |
| after Stage 3.1 | ports-on-boundaries |
| after exit-port align + row re-flush (Stage 3.4) | ports-on-boundaries |
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
idempotence locked by `test_content_placement_idempotent` (#488): purity means
re-running, re-ordering, *or perturbing the non-anchor state* cannot change a
phase's output. `tests/test_content_placement_pure.py` (#491) is the guard - it
perturbs the non-anchor state before each phase and asserts the governed
stations land identically, the test-time counterpart to the anchor-frozen guard.

The phases that genuinely need an intermediate quantity - the empty-band slack
in 6.1 / 6.2, the balance arrangement in 6.11 - read it from a frozen *placement
reference* (`_snapshot_placement_refs` populates `graph._placement_ref_y` /
`_placement_ref_bbox_top`; phases read it via `_ref_y` / `_ref_bbox_top`)
captured once right before the consumer, rather than from live geometry. The
reference equals the live geometry at capture time, so the property was added
with no change to any render.

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
- `graph.half_grid_station_ids` - written by Stage 6.3 (`center_ports` only)
  and Stage 6.17 (`diamond_style='symmetric'`); read by the Stage 6.4 grid
  snap, which must skip these half-pitch stations. Stage 6.17 runs after the
  last snap, so its writes mark branches for the invariant tests / straddle
  guard rather than feeding a later snap.
- `graph.symfan_trunk_station_ids` - written by Stage 6.3 (`center_ports` only);
  read by the Stage 6.4 grid snap, which must skip these source/trunk stations
  so they stay on the symfan's local frame instead of snapping to a rowspan
  neighbour's fractional row-grid origin.
- `graph._consumers_grid_snapped` - set right after the Stage 6.4 snap; the
  Stage 6.6 off-track reanchor carries its own always-on guard on it.

The remaining channels tolerate an unwritten value by design (their read sites
fall back to live geometry or a `None`/empty default), so they are documented in
the registry but carry no runtime check: `graph._struct_height_below_top`
(snapshotted after 6.15a, read by the 6.13 cascade), `graph._placement_ref_y` /
`graph._placement_ref_bbox_top` (frozen before 6.1/6.11, read via `_ref_y` /
`_ref_bbox_top`), `graph._base_y_spacing` (recorded before the spread loop
when `y_spacing` is auto-resolved), and `graph._resolved_x_spacing` (the
resolved column pitch recorded before layout, read as the cross-axis off-track
step for vertical-flow sections).

A further group crosses a subsystem boundary rather than two numbered stages,
so their `PhaseFieldSpec` names a lifecycle phase (`pre-layout`, `post-layout`,
`rail-layout`) in place of a stage id. They carry no runtime check either:

- `graph._cross_column_perp_bridges` - sections whose perpendicular drop was
  bridged across grid columns, accumulated by the Stage 3.2 / 3.4 port
  alignment; routing's render-curve invariant reads it to relax its abort to a
  warning for those bundles.
- `graph._fold_compressed_sections` / `graph._fold_reoriented_sections` -
  recorded at parse/resolve time for sections a lowered fold threshold
  relocated or whose flow direction was flipped; read by the fold-exit-side
  guard, the render fold-abort chokepoint, and routing's exit-port offset.
- `graph._rail_y` - the per-section `{line_id: rail_y}` map produced by the
  opt-in rail-mode layout; read by the rail router, label placement, and rail
  guards, empty when rail mode is off.
- `graph._defer_final_guards` / `graph._after_final_deferred` - pass-control
  flags `compute_layout` uses so the final-geometry guards defer while the
  pre-bypass passes run, then validate the settled post-bypass geometry once.

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
  layering of the section DAG.
- **Helper**: `place_sections` in `section_placement.py`.
- **Precondition**: Sections have bboxes from Stage 1.1 and grid
  positions from `auto_layout`. Still all local-coord.
- **Postcondition**: Every section has `offset_x`, `offset_y` set such
  that `(local + offset)` lands sections on a non-overlapping grid.
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
- **Purpose**: Renumber sections by visual reading order (sweep, col,
  row) so legend / debug numbering follows the eye.
- **Helper**: `_renumber_sections_by_grid` (`phases/canvas.py`).
- **Precondition**: Section grid positions and directions finalised.
- **Postcondition**: `section.display_number` reflects sweep-major,
  column-then-row order.
- **Invariants preserved**: Section IDs, station coords, bboxes,
  edges. Pure metadata pass.
- **Related tests**: none directly (cosmetic / debug-only).
- **Lifecycle:** invariant - `display_number` metadata is final
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
  its bbox edge matches its source's coordinate on that axis (within
  the section's bbox extent).
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
  `x_spacing` away from the perp entry port X.
- **Invariants preserved**: Station Y, ports, bboxes (X shift is
  bbox-bounded).
- **Related tests**: `test_terminus_not_directly_after_diagonal`,
  `test_no_kink_at_section_boundary` (entry-side geometry).
- **Lifecycle:** invariant - the perpendicular-entry runway
  (internal-station X clearance) holds at the final boundary.

### Stage 3.4: align fold-section exit ports
- **Purpose**: For row-spanning (fold) and TB-direction sections,
  shift LEFT/RIGHT exit ports to the target section's entry Y. May
  push the target section down via `_resolve_tb_exit_y`; the move then
  re-flushes the tops of the rows it pushed so it cleans up after
  itself rather than leaving the correction to a separate stage.
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
  `test_exit_port_row_reflush`.
- **Lifecycle:** invariant - the fold/TB exit-port no-kink Y holds at
  the end (re-asserted by Stage 5.5).

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
  single Y. Bbox tops preserved (heights grow downward).
- **Helper**: `_align_row_trunk_ys` (`phases/row_align.py`).
- **Precondition**: Stage 4.7 done.
- **Postcondition**: For sections in a row's contiguous column run,
  the trunk Y is the row's deepest pre-pass trunk Y. Row-spanning
  sections are skipped.
- **Invariants preserved**: Bbox tops. Row-spanning sections.
- **Lifecycle:** invariant - the per-row trunk Y is consistent at the
  final boundary (`test_row_trunk_marker_cy_consistent`).

### Stage 4.9: redistribute fan-out siblings
- **Purpose**: For each fan-out column with a unique trunk junction
  (one station carrying the full bundle plus >=2 side branches),
  redistribute side stations symmetrically around the trunk Y. No-op
  unless `graph.center_ports` (guard inside the helper, not at the call
  site).
- **Helper**: `_redistribute_fanout_siblings` (`phases/fan_bundles.py`).
- **Precondition**: Trunk Ys aligned (Stage 4.8).
- **Postcondition**: In qualifying columns, fan-out siblings sit
  symmetrically around the section's LR/RL port trunk anchor (the trunk
  station's own Y only when the section has no such port). Linear chains,
  fan-in structures, and file inputs are left in place.
- **Invariants preserved**: Trunk station Y. Off-track stations.
- **Purity**: centres on the frozen port anchor, so the fan does not
  depend on the trunk station's live Y (#491).
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

### Stage 5.1: position junctions
- **Purpose**: Place each junction station in the inter-section gap
  at the exit port's Y (fan-out) or near the entry port (merge).
- **Helper**: `_position_junctions` (`phases/junctions.py`).
- **Precondition**: All port Ys final (Pass B complete).
- **Postcondition**: Every junction has finite `(x, y)`. Fan-out
  junctions sit at `exit_port.y` plus a `JUNCTION_MARGIN` X offset
  toward the targets; merge junctions sit at
  `max(pred.x) + JUNCTION_MARGIN, entry_port.y`.
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
  `test_off_track_outputs_above_and_adjacent_to_producer`,
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
  sits at `min(content_top) - section_y_padding`. Stations shift up
  by the same delta as their bbox.
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
  its connected LEFT/RIGHT entry port share a Y. Junctions back at
  exit-port Y.
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
  `graph.half_grid_station_ids`).

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
  while unset, so the dependence on snapped consumers is no longer
  implicit in call position (#463).
- **Postcondition**: Off-track stations sit `n * step` clear of their
  anchor's final cross coordinate. The lift-side bbox edge hugs the band
  (recompute-to-fit, so re-running is order-independent). May leave the
  topmost section above the canvas margin -- ``_shift_graph_into_canvas``
  runs immediately afterwards (called explicitly by the caller, not by the
  helper).
- **Invariants preserved**: On-track station Y.
- **Related tests**: `test_off_track_inputs_above_consumer`,
  `test_off_track_outputs_above_and_adjacent_to_producer`,
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
- **Helper**: `_recenter_full_bundle_columns`.
- **Precondition**: Final inter-section trunk Y known (post-snap).
- **Postcondition**: Full-bundle columns are symmetric around the
  row's final trunk Y.
- **Invariants preserved**: Off-track Y anchoring (re-established by
  Stage 6.8) and bbox-top alignment (re-established by Stage 6.9)
  are temporarily broken; both are restored before leaving the
  `if center_ports:` block.
- **Lifecycle:** invariant - full-bundle columns are symmetric around
  the row's final trunk Y at the boundary; no later stage re-fans them.
  *liftable:* no - one-shot, order-dependent (computes against the final
  trunk Y, so a premature run is wrong).

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
- **Gating**: Early-returns unless **both** `graph._explicit_grid` and
  `graph.center_ports` are set (scoped to explicit-`%%metro grid:` +
  centre-ports pipelines), so it is a no-op on auto-laid graphs.
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
  so it records the fully settled extent for structural-extent fidelity
  checks, not as a cascade input.
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
- **Helper**: `_fit_bboxes_to_content_top` (`phases/bbox.py`), then
  `_shift_graph_into_canvas`.
- **Precondition**: All content Ys final (post-6.14).
- **Postcondition**: Each bbox top sits `section_y_padding` above its
  highest marker, bounded by the row above. For a section with an empty
  band (no port / bypass above content) this is an equality, not just a
  floor: the excess band is reclaimed.
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
- **Why this pass stays (axis-generic, not removed)**: the port re-align is
  scoped (`vertical_only`), not TB-special-cased, but it is load-bearing and
  irreducible. Re-running the *full* alignment here would drag horizontal-flow
  ports (9 across the corpus, e.g. longread `small_variants` by +86px) off
  their settled positions, so the scope cannot be dropped; and removing the
  pass re-introduces the S-kink on the vertical-flow ports it corrects (2
  across the corpus: longread `phasing` +16.8px, `tb_file_termini` `reporting`
  -14px). The companion `_position_junctions` is not TB-specific at all - it
  re-anchors stale junctions (any direction) after the settling phases (17
  across the corpus, some by hundreds of px).

### Stage 6.17: symmetric diamond half-pitch compaction (engine.py)
- **Purpose**: Under `diamond_style='symmetric'`, compact each clean
  2-way fork-join diamond (`_iter_symmetric_diamonds`) onto half-pitch
  offsets `trunk_y +/- 0.5 * y_spacing`, so the diamond reads as a tight
  one-grid-unit bubble rather than straddling the trunk at full pitch
  (as tall as a 3-way fan with an empty trunk row between its branches).
  Per-diamond, so a diamond compacts even when it shares a section with a
  wider fan (which keeps its full-pitch slots) and regardless of
  `center_ports`. Records the branches on
  `MetroGraph.half_grid_station_ids`. Runs last, after every
  trunk-settling pass, so the branches straddle the section trunk's final
  Y exactly; the compaction only moves them inward toward the trunk, so it
  never breaks bbox containment.
- **Helper**: `_apply_half_grid_symmetric_diamonds`.
- **Precondition**: Trunk Ys settled (post-6.16); `diamond_style`
  is `symmetric`.
- **Postcondition**: Each symmetric diamond's branches sit at
  `trunk_y +/- 0.5 * y_spacing`; their IDs are in
  `graph.half_grid_station_ids`.
- **Invariants preserved**: Trunk station Y, ports, bbox containment,
  the wider fan's full-pitch slots.
- **Related tests**: `test_symmetric_diamond_compacts_to_half_pitch`,
  `test_symmetric_diamond_both_branches_deviate`,
  `_guard_symmetric_diamond_branches_straddle_trunk`.
- **Lifecycle:** invariant - symmetric diamond branches keep their
  half-pitch offsets at the final boundary (no later Y mutation).

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
