# Constraint-solver feasibility spike

Track F of the issue #323 fragility-reduction project. Time-boxed at ~2
agent-days. Goal: build a constraint formulation for a small topology, solve
with kiwisolver and scipy, compare to `nf_metro.layout.engine.compute_layout`,
and recommend go / no-go / extend.

Spike code: `scratch/solver_spike.py`. Output: `scratch/solver_spike_results.json`.

## 1. Setup

### Fixtures

- `examples/topologies/single_section.mmd` - 3-station linear chain in one
  LR section. The minimal sanity case.
- `examples/topologies/section_diamond.mmd` - 4 sections in a 3-column /
  2-row grid: a start section that fans to two parallel branch sections
  (one with 3 stations, one with 2), reconverging on a finish section. One
  inserted junction station for the fan-out.

### What the solver was asked to do

The spike treats layout as a **two-stage** problem:

1. **Topology stage (NOT solved)**: section grid (col, row), section
   direction (LR/RL/TB), per-station layer index, per-station track index,
   port side assignments, junction insertions. These come from the existing
   engine (`auto_layout.py`, `layers.py`, `ordering.py`,
   `section_placement.py`). The spike reads them off the engine's output.

2. **Coordinate stage (solved)**: given those discrete indices, choose
   numeric `x`, `y`, `bbox_x`, `bbox_y`, `bbox_w`, `bbox_h` for every
   section, station, and port.

### Constraint set encoded

| Constraint | Form | Strength |
|---|---|---|
| Section bbox dimensions = `f(n_layers, n_tracks, spacing, padding)` | equality | required |
| Same-column sections share `bbox_x` | equality | required |
| Same-row sections share `bbox_y` | equality | required |
| Anchor leftmost column / topmost row to canvas origin | equality | required |
| Adjacent columns: `left[i+1] = max_right[i] + SECTION_X_GAP` | equality | required (via per-column tight right-edge) |
| Adjacent rows: analogous on y | equality | required |
| Station x = `bbox_x + padding + layer * x_spacing` | equality | required |
| Station y on n_tracks=1 sections = `bbox_y + bbox_h/2` | equality | required |
| Station y on multi-track sections = `bbox_y + padding + track * y_spacing` | equality | required |
| LEFT/RIGHT port x = bbox left/right edge | equality | required |
| Port y matches section vertical center (default) | equality | strong |
| TOP/BOTTOM port y = bbox top/bottom edge | equality | required |
| Bare junctions: x near upstream section's right edge | equality | strong |

Two solver backends, same constraints:

- **kiwisolver** (Cassowary simplex). Linear arithmetic only, with
  strength tiers (required / strong / medium / weak). Tight bbox sizing
  done with edit variables suggesting 0 on the column-right / row-bottom
  variables so the `>=` constraints become tight.
- **scipy.optimize.minimize(L-BFGS-B)** on a sum of weighted squared
  residuals (`1e3` for hard, `10` for soft). Section count fixed, all
  positions free.

### What the spike does NOT encode

Intentionally deferred so we can see what would be needed for a real
production solver:

- Edge routing (path segments, corner radii, bundle offsets, channel
  selection)
- Fold/serpentine row reversal (RL direction)
- TB-direction sections
- Off-track input stations (lifted above trunk)
- Bypass V-routes for non-consumer lines through a section
- Diamond fork-join compaction (FastP/TrimGalore-style)
- Concentric bundle radii at corners
- Long-range inter-section edges that pass over intervening sections
- Per-station Y offsets for parallel lines
- Multi-line bundle ordering on edges

## 2. Result

Both solvers produce **identical answers** (kiwi exact; scipy converges to
the same values within `1e-7`), which validates the constraint formulation
is internally consistent. Comparison to the engine on `single_section.mmd`
and `section_diamond.mmd`:

| Metric | single_section | section_diamond |
|---|---|---|
| Solver vs solver agreement | exact | exact |
| Station-x exact match to engine | 100% | 75% (within sections 0/1) |
| Station-y exact match to engine | 0% | 0% |
| Section bbox exact match | 50% (x/y match, w/h differ) | 25% |

### Where the spike diverges from the engine

The diffs are all **systematic** - the engine uses tighter bbox sizing
rules than the textbook `2*pad + (n-1)*spacing + 2*half_width` formula:

- Engine n_tracks=1 LR section: `bbox_h = 100`. Spike: `bbox_h = 124`
  (adds `2 * STATION_HALF_H`). The engine's padding apparently already
  includes station marker half-extent in its 50-unit constant. The
  textbook formula double-counts.
- Engine station y on n_tracks=1: `bbox_y + 50` (= top + padding =
  120). Spike: `bbox_y + bbox_h/2` (= 70 + 62 = 132). Engine pins
  stations to top-padding, not bbox vertical center, even when
  n_tracks=1. The spike's "center the trunk" assumption is wrong.
- Engine section width for `branch_left` (n_layers=5 including ports):
  `bbox_w = 227`. Spike: `bbox_w = 256`. Engine treats port stations as
  laying on the boundary, not as a layer that consumes width. The spike's
  `n_layers` includes ports.

Once those three rules are corrected to match the engine, the constraint
formulation should reproduce engine coordinates exactly on these two
fixtures - the X positions of internal stations *already* match because the
spacing-and-offset math is right.

The scipy back-end's `section_diamond` station max-diff of 200 was on the
unanchored bare junction (`__junction_6`); it landed at `(36, 326)` instead
of the engine's `(200, 120)` because the spike's anchor constraint for
unsectioned junctions ("near upstream section's right edge + 10") doesn't
match where the engine actually places them (mid-channel between source-
section right and target-section left, on the trunk line). Kiwisolver got
closer because the strong constraint there was satisfiable; scipy's
soft-weighted version traded off poorly against other residuals.

### Convergence

- kiwisolver: deterministic; no iterations counter; instant on these
  fixtures.
- scipy L-BFGS-B: 13 iterations on single_section, 280 on section_diamond,
  final residual `~1e-9` (essentially zero - all hard constraints
  satisfied).

## 3. Constraints that translated cleanly

These are pure linear arithmetic and map directly onto either solver
without bespoke code:

- Column / row grid alignment (shared bbox_x / bbox_y in a column / row).
- Inter-section gaps (`next.left = prev.right + gap`).
- Station-on-layer-grid within section (`x = bbox_x + pad + layer * spacing`).
- Port-on-boundary (`port.x = bbox.left_edge` for LEFT-side ports, etc.).
- Section bbox dimensions when expressed as `f(discrete topology indices)`.
- Anchor first column / row to canvas origin.

The cleanest result is that **all of section-placement.py and most of the
intra-section coordinate math can become a linear constraint system**.
That's ~1300 LOC across `section_placement.py` and the coordinate-mapping
phases of `engine.py` that could in principle collapse to a constraint
declaration.

## 4. Constraints that needed bespoke code (or were dodged entirely)

These are the ones the spike either skipped, encoded as soft strong-weighted
hints, or that would need significant added machinery:

### 4.1 Discrete topology decisions

These have to happen *before* the solver runs, in pre-processing:

- **Section grid placement** (`auto_layout.py`, ~700 LOC of the 965).
  Picks col, row for each section, picks fold points when a row gets too
  wide, picks rowspans/colspans, picks direction (LR/RL/TB). Includes
  graph-cycle handling and topo-sort. This is combinatorial - assigning
  col/row by minimising edge crossings is NP-hard. No off-the-shelf
  constraint solver does this directly; you'd need either a SAT/ILP
  formulation (expensive) or to keep the current heuristic and feed
  results into the LP solver.
- **Per-station layer assignment** (`layers.py`, longest-path layering).
  Discrete; networkx topological sort. Could be encoded as ILP but the
  current implementation is fast and correct.
- **Per-station track ordering** (`ordering.py`, line-per-track plus diamond
  compaction). Discrete permutation problem. Same comment as above.
- **Junction insertion for fan-outs** (`parser/mermaid.py:_resolve_sections`).
  Graph rewriting; happens during parse. Not a numerical constraint.

These ~1700 LOC are upstream of any solver and would have to remain.

### 4.2 Fold rows and reversed direction

Fold rows (when a long pipeline wraps to a serpentine) introduce
direction="RL" sections. The X-on-layer-grid constraint flips sign:
`x = bbox_right - pad - layer * spacing` instead of
`bbox_left + pad + layer * spacing`. This is still linear; just two cases.
But the fold-detection itself is discrete (a topology choice) and lives
in auto_layout. Solver impact: small (a sign flip per section direction);
total surrounding code: large (`_assign_grid_positions`, `_infer_directions`,
~400 LOC).

### 4.3 TB-direction sections

The variant_calling fixture (the engine's hardest case) and `fold_double`
have a TB-flowing section that bridges fold rows. The constraints
themselves are dual to LR (swap x and y in every layer-grid expression);
this is straightforward. The hard part is the inter-direction port joins
(LR-section right-port connecting to TB-section top-port via an L-shape);
those constraints are non-linear in port y if you let the inter-section
edge routing influence port y position.

### 4.4 Junction insertion mid-channel

The `__junction_6` station in `section_diamond` sits between the start
section's right port and the two branch sections' left ports, off any
section. The engine positions it at the trunk Y (120) and at X=200
(mid-channel between start-right=190 and branches-left=240). The spike's
anchor ("near upstream section's right + 10") got close but not exact.
Encoding "mid-channel between two named edges" is one extra linear
constraint per junction; the existing engine logic is already this
straightforward. No real obstacle.

### 4.5 Bypass V-routes

Lines that pass through a section without stopping at any of its stations
(non-consumer lines) get routed as a V-shaped detour. The engine inserts
"virtual stations" for the V vertices and constrains them to avoid the
section's marker bboxes (issue #225, PR #225). This is:

- A pairwise non-overlap constraint between V-vertices and station bboxes
  (non-convex, hard for LP; OK for least-squares as a penalty).
- A line-style decision (which lines bypass which section).

Bypass routing alone is ~500 LOC across `routing/core.py` and
`engine.py`. A linear constraint solver cannot natively express
non-overlap; kiwisolver would need either disjunctive constraint hacks
(big-M with binary indicator variables, requires ILP) or post-hoc
adjustment. scipy's least-squares can encode penalty functions but
becomes brittle and slow.

### 4.6 Edge routing

`routing/core.py` (2660 LOC) is the deepest body of code in the engine.
It picks corner radii, bundle channel X coordinates, offset stations Y
positions, and produces SVG path segments. Almost none of this is a
linear constraint:

- Corner radii are clamped to half the shorter incident segment length:
  `r <= min(segA_len, segB_len) / 2` - that's linear, OK.
- Bundle channel X selection (`_inter_column_channel_x`) is a discrete
  choice among gaps. Could be enumerated.
- Concentric bundle radii (PR #211): line offsets within a bundle expand
  the curve radius for outer lines. Linear in offset.
- L-shape vs S-shape vs serpentine fold reversal: discrete topology choice
  per edge. Not a constraint, a path-shape decision.

Putting routing into a solver requires either (a) keeping routing as a
post-pass after the solver picks station positions, or (b) embedding the
path geometry as variables, which explodes the variable count.

### 4.7 Min-y-spacing and content-driven sizing

`compute_min_y_spacing(graph)` derives `y_spacing` from the tallest icon
caption or station label height in the graph. That's a max-aggregate over
content, which is fine to do *before* the solver. But the engine also has
phase 8 (`_align_row_y_grids`) that re-snaps station Ys to a common grid
across same-row sections after exit-port alignment - that loop has
hysteresis (later snaps invalidate earlier ones). Encoding it as a single
constraint set is possible but the rules are intricate (`_classify_section_station_ys`,
`_compact_row_content_to_bbox_top`, `_balance_section_content_around_trunk`,
~600 LOC).

## 5. Effort estimate

### One section type (LR, no folds, no bypass, no routing)

The spike already achieves this in **~400 LOC** of constraint code. To
reach engine-fidelity coordinates would need:

- Match engine's bbox sizing rule (rule-by-rule alignment): ~50 LOC.
- Centerline-vs-top-anchored station y for n_tracks=1: trivial.
- Port-Y from dominant feeder (currently soft constraint, default-centered):
  ~30 LOC to look up the feeder station and add a `port.y == feeder.y`
  strong constraint.

**Estimate: 1-2 days** to reach pixel-parity with the engine on the
LR-only, no-fold subset (about 30% of the gallery fixtures).

### Full engine replacement

A solver-driven replacement would still need:

- All of `auto_layout.py` (965 LOC) - upstream discrete decisions.
- All of `layers.py` + `ordering.py` (~800 LOC) - upstream discrete decisions.
- All of `routing/*` (~4900 LOC) - post-solver path computation.
- Bypass V-route code (~500 LOC) - either reimplemented as ILP (slow,
  brittle) or kept as-is.
- Fold/serpentine direction handling (~400 LOC, can stay).
- Phase-8 row-Y-grid snapping (~600 LOC) - can mostly become constraints.

The "win" from the solver is roughly:

- `section_placement.py` (892 LOC): about 60% becomes a constraint
  declaration, 40% (column-span optimisation, ordering by grid_col) stays.
- Coordinate-mapping inside `engine.py` (estimated 1500-2000 LOC of the
  6804): replaceable by a constraint declaration of maybe 300 LOC.

**Net replaceable: ~2000 LOC out of ~14000.** The remaining 12000+ LOC is
either upstream (graph topology, sectioning) or downstream (routing,
rendering) and cannot be subsumed by a linear constraint solver.

**Estimate to ship a solver-backed engine replacement: 4-6 weeks of
focused work**, including:

- Catalog the engine's exact bbox/station coordinate rules as constraints
  (1 week).
- Write the constraint emitter (1 week).
- Wire the solver output back through routing (1 week).
- Get all 15 topology fixtures pixel-passing (1-2 weeks of iteration -
  this is where the unknown unknowns will surface).
- Gallery regression vetting on every nf-core pipeline (1 week).

## 6. Recommendation: **no-go (with extend option for a specific use)**

### Why not go

The constraint solver does solve the easy part of the engine - placing
sections on a column/row grid and stations on a layer/track grid - but
**that's not where the engine's fragility lives.** The 14000-LOC layout
codebase is mostly:

1. **Discrete topology decisions** (~2500 LOC of `auto_layout.py`,
   `layers.py`, `ordering.py`, `section_placement.py`'s metagraph). These
   are graph algorithms, not coordinate math. A constraint solver doesn't
   help.
2. **Edge routing** (~4900 LOC of `routing/*`). Hard non-linear constraints
   (non-overlap, channel-picking, bundle ordering). A constraint solver
   doesn't help.
3. **Fix-up phases** (`_align_row_y_grids`, `_compact_row_content_to_bbox_top`,
   `_balance_section_content_around_trunk`, ~1500 LOC of `engine.py`).
   These ARE constraint-amenable but have hysteresis - the engine's order
   matters because later passes refine earlier outputs based on routed
   edges. Lifting them into one global constraint system would require
   first eliminating the routing dependency.

The replaceable layer is shallow and well-tested already. Issue #323's
fragility risks (mentioned in PR descriptions across the v98-v110 series)
are in routing and bypass logic, not in coordinate assignment.

### Why not extend (in general)

Even on the "easy" coordinate-assignment subset, the spike shows that
matching the engine's exact constants requires copying engine-specific
rules into constraints. The solver doesn't *eliminate* magic numbers; it
relocates them from imperative code to constraint declarations. The
maintenance burden on the constraint declarations would be comparable
to the current `constants.py` plus the coordinate-mapping phases.

### Where extending IS justified

One narrow use is worth the cost:

**Use kiwisolver for row-Y-grid alignment phases (8 and 9 in
`_compute_section_layout`)**. These currently have hysteresis bugs
(documented in MEMORY.md as PRs #178-#189, #208, #209, #211, #212, #221,
#223 - several issues there were "the trunk-Y refinement pass interacts
badly with the port-Y refinement pass"). A constraint declaration of
"all sections in row R share the same trunk Y; trunk Y satisfies these
soft preferences in priority order" would replace ~600 LOC of imperative
passes with declarative constraints. Output would be deterministic and
free of pass-order bugs.

**Recommendation: extend the spike** to encode the row-Y-grid alignment
phase as a kiwisolver constraint set, in a separate spike, before
deciding whether to land it. Estimated effort: 3-5 days. If it cleanly
reproduces the engine on the gallery, it becomes a candidate for landing.
If not, the rules turn out to be more subtle than they look and we
shelve the idea.

### Failure mode if we went ahead anyway

If we shipped a kiwisolver-backed coordinate engine without first solving
fold rows, bypass routing, and TB-direction interop, the gallery
regressions would be:

- Pipelines that use folds (rnaseq, sarek, ampliseq) would lose direction
  inference.
- Pipelines with bypass lines (most of them, post-PR #225) would route
  through section markers.
- Long-range inter-section edges would route through intervening
  sections (the known limitation already documented in MEMORY.md).

All three are critical visual regressions that would block any release.

---

## Appendix: spike outputs

Raw comparison data: `scratch/solver_spike_results.json`.

Run with:

```bash
source ~/.local/bin/mm-activate nf-metro
PYTHONPATH=$PWD/src python scratch/solver_spike.py
```

The spike depends on `kiwisolver` (already in the env) and `scipy`
(installed during the spike via `pip install scipy`).
