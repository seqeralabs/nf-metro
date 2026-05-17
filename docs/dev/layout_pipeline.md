# Layout pipeline

A reader-friendly walkthrough of how nf-metro turns a parsed metro graph
into placed coordinates.  If you're hunting a layout bug, fixing a
visual regression, or adding a new transformation pass, start here.

For the rigorous per-sub-stage contract (preconditions,
postconditions, invariants preserved, related tests), see
[`src/nf_metro/layout/CONTRACT.md`](https://github.com/pinin4fjords/nf-metro/blob/main/src/nf_metro/layout/CONTRACT.md).
For the source itself, the orchestrator is `_compute_section_layout`
in `src/nf_metro/layout/engine.py`.

## What "layout" means here

Parsing produces a `MetroGraph` with sections, stations, edges, lines,
and ports - but with no coordinates yet (other than the optional
`%%metro grid:` directives).  The layout pipeline assigns every station,
port, junction, and section bbox an `(x, y)` on the canvas, subject to:

- Sections don't overlap each other.
- Stations sit inside their section's bbox.
- Ports sit on their section's bbox edge.
- Lines route between connected stations without crossing unrelated
  station markers.
- Same-row sections share a trunk Y so the inter-section bundle stays
  horizontal across boundaries.
- A bunch of other invariants documented in
  `tests/test_layout_invariants.py`.

Achieving all of this in one pass is intractable - some constraints are
naturally local (each section's internal layout) and some are global
(trunk Y alignment across an entire row).  The pipeline solves this by
chaining many small passes, each of which mutates the graph and
preserves the invariants of preceding passes.

## The six stages

The pipeline groups into six stages.  Stage boundaries align with
coord-regime transitions (when station coordinates become global,
when ports become positioned) and with the traditional Pass A / Pass B
/ Pass C divisions referenced throughout the codebase.

### Stage 1 - Section construction (local coords)

Lay out each section's internal stations on its own private coordinate
system, then place the sections on the global grid (still local-coord).

- **Stage 1.1**: Lay out each section independently via layer / track
  assignment (real stations only; ports and junctions stay
  unpositioned).
- **Stage 1.2**: Snap same-row, same-direction sections to a shared Y
  grid so they agree on pitch and slot count.
- **Stage 1.3**: Place sections on the canvas grid by topological
  layering of the section DAG.
- **Stage 1.4**: Renumber sections by visual reading order (column
  first, then row) so the legend numbering follows the eye.
- **Stage 1.5**: Grow `x_offset` / `y_offset` if section local extents
  overshoot the canvas origin.

At the end of Stage 1, every section has a `(local_x, local_y, w, h)`
bbox and an `(offset_x, offset_y)` placement.  No global coords yet.

### Stage 2 - Globalise (local -> global coords)

A single-step coord-regime transition.

- **Stage 2.1**: Translate every real station's `(x, y)` and every
  section's bbox into global canvas coordinates.

After this, all subsequent stages operate in global coords.  Ports and
junctions still have no positions.

### Stage 3 - Pass A: port initialisation & section geometry

Ports first appear on bbox edges, then get aligned with their incoming /
outgoing connections, then the section layout is adjusted to accommodate
them.

- **Stage 3.1**: Position every port on its section's bbox edge at the
  edge midpoint.
- **Stage 3.2**: Align LEFT / RIGHT entry ports to the incoming source
  Y so the inter-section horizontal run is straight; align TOP / BOTTOM
  entry ports analogously.
- **Stage 3.3**: For LR / RL sections with perpendicular (TOP / BOTTOM)
  entry, shift internal stations' X so the entry port has runway
  before stations begin.
- **Stage 3.4**: Align LEFT / RIGHT exit ports on row-spanning (fold)
  sections with the target section's Y.
- **Stage 3.5**: Top-align sections within each grid row so contiguous
  column groups share their bbox tops.

Pass A leaves ports on bbox edges with first-approximation alignment.
Subsequent passes refine.

### Stage 4 - Pass B: downstream alignment & trunk-Y consolidation

Pull ports toward downstream stations to remove unnecessary detours;
consolidate the inter-section trunk Y across each row; redistribute
fan-out and full-bundle columns around the trunk.

- **Stage 4.1**: For non-fold LR / RL sections, pull exit-entry port
  pairs toward the downstream section's connected station Y.
- **Stages 4.2 to 4.4**: Snap port pairs to grid-group / sole-layer
  station Ys so port-to-station connections are horizontal.
- **Stage 4.5**: Ensure ports maintain at least `y_spacing` from
  terminus stations so file icons don't overlap routed lines.
  (May expand bboxes.)
- **Stages 4.6 to 4.7**: Recompute grid-group bboxes; re-run row
  top-align after the Stage 4.5 expansions.
- **Stage 4.8**: Align trunk Ys across same-row sections.  Shifts
  shallower sections' content down so the inter-section bundle passes
  through at a single Y per row.
- **Stages 4.9 to 4.10**: Redistribute fan-out siblings and full-bundle
  columns symmetrically around the trunk.  Both gated on `center_ports`.

By the end of Pass B, all port Ys are final.

### Stage 5 - Pass C: junctions & off-track lift

Position junctions for the first time, lift off-track file inputs above
their consumers, then a few post-lift fixups.

- **Stage 5.1**: Position every junction station in the inter-section
  gap.  Fan-out junctions sit at the exit port's Y; merge junctions sit
  near the entry port.
- **Stage 5.2**: Lift off-track stations (file inputs that should sit
  above the trunk, not on it) to the row above their consumer, growing
  bboxes upward.
- **Stages 5.3 to 5.4**: Re-align row bbox tops to match the lifted
  sections, then compact each row's content to its bbox top.
- **Stage 5.5**: Snap inter-section LR / RL port pairs to a shared Y
  (the compaction in Stage 5.4 may have drifted them) and re-position
  junctions to follow.

### Stage 6 - Pass C: vertical settling & finishing

The long settle.  16 sub-stages clean up the consequences of Stages 1
through 5, snap everything to the grid, restore invariants broken by
each cleanup pass, then handle the final geometric details (loop-side
X recenter, bbox shrink, captioned-icon spacing).

- **Stages 6.1 to 6.3**: Fan free content / source inputs upward into
  empty top bands; collapse 2-branch symmetric fans onto half-grid
  offsets (gated on `center_ports`).
- **Stage 6.4**: Snap every station and port Y to the row's grid
  pitch, removing fractional drift from earlier passes.
- **Stages 6.5 to 6.6**: Grow TB-section bbox bottoms to match
  downstream LR / RL targets; re-anchor off-track inputs to their
  consumers' post-snap Y.
- **Stages 6.7 to 6.9**: Re-center full-bundle columns around the
  row's final trunk Y; restore the off-track-above-consumer and row
  top-align invariants that the recenter breaks.  All gated on
  `center_ports`.
- **Stages 6.10 to 6.12**: Pin single-station downstream columns to
  their unique upstream Y; auto-balance content around the trunk;
  re-center loop-side stations on their loop midpoint (X-axis pass).
- **Stages 6.13 to 6.15**: Shrink bbox bottoms to content, close
  vertical slack between rows; shift sparse loop-side stations onto
  half-pitch Ys to clear bundle pass-throughs (the same helper pushes
  lower rows down internally when a shift grew a bbox).
- **Stage 6.16**: Pad stacked captioned file-icon columns so
  under-icon captions don't overlap the icon below.

Stage 6 is where most of the historical organic-suffix sprawl (the old
13d / 13d2 / 13h.1 / 13k2 names) lived.  The flat Stage.N scheme makes
the sequence walkable; the per-sub-stage CONTRACT.md entries explain
each one's necessity.

## Passes vs stages

The codebase has two overlapping group labels.  They are not redundant
- they encode different axes of the structure:

- **Stage** (1-6) groups by **what kind of mutation** the pass
  performs: section construction, globalisation, port positioning,
  port refinement, junctions / off-track lift, vertical settling.
- **Pass** (A / B / C) groups by **how much of the layout is final**
  when the pass runs.  Pass A operates on a fresh station layout to
  position ports.  Pass B refines ports on a fixed station layout.
  Pass C operates on finalised stations and ports.

The Stage and Pass labels line up cleanly:

| Pass | Stages |
|---|---|
| Pre-pass setup | 1, 2 |
| Pass A | 3 |
| Pass B | 4 |
| Pass C | 5, 6 |

## When something breaks

Common scenarios and where to start looking:

- **A station moved when it shouldn't have**: which stage's
  postcondition does it violate?  Run `pytest tests/test_layout_invariants.py`
  - the failing invariant's "related tests" entry in CONTRACT.md
  names the stage that establishes the relevant property.
- **A guard fired with `after Stage X.Y: ...` at `validate=True`**:
  Stage X.Y is the *latest* sub-stage where the invariant could
  still have been broken.  Bisect by toggling preceding sub-stages.
- **A guard fired with `after final: ...`**: the invariant only
  holds at the very end, so the regression could be anywhere in
  Pass C.  Run with `validate=True` and use the per-checkpoint
  bisection (`_run_pass_c_guards`) to localise.
- **A new fixture lays out badly**: render it with `nf-metro render`,
  inspect the SVG against the stage descriptions above to guess
  which stage handles the problem area, then read the corresponding
  sub-stage entry in CONTRACT.md.

## Why so many sub-stages

The Pass C tail (Stages 6.1 to 6.16) looks excessive at first glance.
Each sub-stage exists because:

1. A bug was found in some real-world fixture.
2. A targeted helper was written to fix it.
3. The helper was placed at the point in the pipeline where it has
   the inputs it needs and won't disrupt earlier-established
   invariants.

Some sub-stages exist purely to **restore** an invariant that an
earlier sub-stage broke (e.g. Stages 6.8 and 6.9 restore the
off-track-above-consumer and row-top-align invariants that Stage 6.7's
full-bundle recenter breaks).  These "repair-only" sub-stages are
candidates for being folded back into the breaking stage, but each
fold is per-pair investigation and risks regressing other pipelines.

The flat Stage.N numbering replaces an earlier organic suffix tree
(`Phase 13`, `13a`, `13d2`, `13h.1`, `13k2`, ...) that grew suffixes
each time a sub-stage was inserted between two existing ones.  The new
scheme keeps the same ordering but makes the sequence walkable; the
historical context lives in the git log and in the "Adding a new stage"
section of CONTRACT.md.
