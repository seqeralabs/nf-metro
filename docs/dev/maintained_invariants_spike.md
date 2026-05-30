# Spike: declarative maintained invariants (#365)

**Verdict: partial land.** The mechanism works byte-identically for the
restores that establish a *final-boundary invariant* (junction positioning,
canvas top-margin) and removes a real class of bug (#386). It **cannot** be
extended to a fully-uniform "run every restore to a fixpoint" system,
because not every "restore" phase maintains an invariant - some are
*transient transforms* whose effect is deliberately superseded later. This
is outcome (a) from the issue, scoped: "promising for a subset, procedural
phases survive elsewhere."

A full run-everywhere overhaul was prototyped and measured (see
"The fully-uniform experiment" below): it diverges on 7 gallery fixtures,
and the divergence is *not* a patchable bbox-edge artifact - it overrides
the intended layout. The hybrid (declarative invariants + procedural
transforms) is therefore **forced by the semantics of the phases, not a
scoping choice**.

This is explicitly **not** a constraint solver. The #353 failure mode
(Cassowary weak attractors couldn't reproduce the engine's hierarchical
decision order) is not relitigated here: the repairs *are* the existing
constructive helpers, applied in a *declared* order, not numeric
attractors relaxed to equilibrium. The negative result below is the same
wall #351/#353 hit, now with a sharper mechanism: a phase whose output is
intentionally overridden has no invariant to declare.

## What the pipeline actually looks like

`_compute_section_layout` has two kinds of Pass-C phase:

- **Constructive** phases that make a layout decision (lift off-track, fan
  content, compact rows, re-center bundles, snap to grid).
- **Restore** phases that re-establish an invariant a constructive phase
  just broke. These are the scattered re-runs:
  - `_position_junctions` - **6 sites** (Stages 5.1, 5.5, 6.4, 6.13, 6.14,
    6.16)
  - `_shift_graph_into_canvas` - **4 sites** (5.2, 6.6, 6.8, 6.15a)
  - `_top_align_row_bboxes_only` - **3 sites** (5.3, 6.9)
  - `_reanchor_off_track_to_consumer` - **2 sites** (6.6, 6.8)

The "run the restore after the thing that breaks it" ordering lives
*implicitly* in the call sequence. Adding a port-moving phase silently
regresses any junction whose restore the author forgot to re-trigger -
which is exactly #386 ("re-run `_position_junctions` after Stage 6.14 to
kill the dip").

Some of these restore phases maintain a true invariant (junctions, canvas);
others are transient transforms (see the taxonomy below). The spike lifts
the *invariant* ones into data: each declares a *predicate* (does it hold?),
a *repair* (re-establish it), and a *priority* (lower repairs first).
`maintain` applies them in priority order, so one `maintain(graph, ...)`
call after each constructive phase subsumes their scattered manual re-runs.

## What landed

`src/nf_metro/layout/phases/maintained.py`:

- `MaintainedInvariant(name, priority, predicate, repair, description)`.
- `maintain(graph, invariants, max_passes=8)` - priority-ordered fixpoint;
  raises loudly on non-convergence (the "two repairs are fighting" =
  #353-shape backstop).
- `assert_maintained(graph, invariants, phase)` - runtime guard for
  `validate=True`.
- Two invariants: `JUNCTIONS_TRACK_PORTS` (priority 30) and
  `canvas_top_margin(section_y_padding)` (priority 20).

The orchestrator's 9 manual restore calls (5 `_position_junctions`
re-runs + 4 `_shift_graph_into_canvas`) are replaced by `maintain(graph,
maintained)`. **All 76 gallery renders are byte-identical to main.**

## Invariants vs transient transforms (the decisive taxonomy)

The "restore" phases are not all the same kind of thing. They split by one
question: **does the property still hold at the final layout boundary?**

- **True invariants** - hold at the end, so re-establishing them whenever a
  later phase perturbs them is always correct:
  - `junctions_track_ports` - `junction.xy == _compute_junction_xy(ports)`,
    a pure function of port coordinates reading no stored junction state.
  - `canvas_top_margin` - topmost section bbox top `>= section_y_padding`;
    its repair is a uniform whole-graph translate (moves junctions + ports
    together, so it never breaks the junction invariant - they compose).
  - `off_track_above_consumer` - off-track inputs sit a pitch above their
    consumer. This *does* hold at the end, but its repair has a
    **precondition** (final/snapped consumer Ys) and an irreversible
    monotonic bbox-grow, so it is not safe to run before the consumers are
    final.
- **Transient transforms** - establish a property that a *later* phase
  deliberately overrides, so they have no invariant to maintain:
  - `_top_align_row_bboxes_only` flushes row bbox tops. But on `main` the
    finished layout's row tops are **not** flush - measured 40px of
    top-spread within a row group on `terminal_symmetric_fan` and
    `trunk_through_fan`, because Stage 6.15a (`_grow_bboxes_to_content_top`)
    and Stage 6.13 (shrink/tighten) re-establish *content-hugging* tops.
    Flush-tops is a transient state used to line up off-track input bands
    during the early settle, not a final property. Running it everywhere
    re-flushes the boxes and reintroduces the empty band above short
    sections - changing the design, not fixing a regression.
  - The fan family (`_redistribute_*`, `_recenter_full_bundle_columns`,
    `_compact_row_content_to_bbox_top`, `_shrink_and_tighten_rows`) is the
    same: each is a one-shot transform at a specific point, not an
    invariant.

Only the true invariants belong in the maintained registry. `junctions`
and `canvas` are run-anytime safe and are lifted (below). `off_track` is a
true invariant but precondition-gated, so it stays procedurally placed
after the snap until/unless the gate is worth encoding. The transforms stay
procedural because there is no invariant to declare.

This taxonomy is the **principled** basis for which phases are declarative:
*invariants are maintained; transforms are procedural*. It is not an
arbitrary scope line.

## The fully-uniform experiment (negative result)

To test whether the hybrid was merely a scoping choice, all four restores
(`junctions`, `canvas`, `off_track`, `top_align`) were lifted into the
registry with a **change-detection** `maintain` (run every repair to a
fixpoint, terminate when a pass mutates nothing - no predicate-skip), and
every hand-placed restore call was replaced by `maintain`. Result:

- **7 fixtures diverge**: `differentialabundance`,
  `differentialabundance_default`, `da_pipeline`, `off_track_convergence`
  (off-track), `terminal_symmetric_fan`, `trunk_through_fan`,
  `variantbenchmarking` (fans).
- Isolation: making `off_track` procedural again fixed the 4 off-track
  fixtures; the 3 fan fixtures still diverged from `top_align`.
- The `top_align` divergence is the empty-band-above-short-section change,
  confirmed against the 40px non-flush measurement above - i.e. it is the
  transform overriding the intended layout, not a patchable edge nudge.

The experiment lives on branch `experiment/365-full-uniform` (draft PR for
the render diff). It is kept as a documented second negative result in the
spirit of #351/#353: a fully-uniform declarative pipeline is blocked not by
predicate cost but by the existence of transient transforms.

## A genuinely-achievable extension (if uniformity is pursued further)

The principled way to extend declarativeness is to lift *more true
invariants*, not the transforms:

- `off_track_above_consumer`, gated on a "consumers are final" precondition
  (e.g. a post-snap flag), so it is safe in the run-everywhere set.
- Other final-boundary guards that already have an idempotent repair
  (`_guard_row_trunk_cy_consistent`, etc.) could be re-expressed as
  maintained invariants.

This should be done **one invariant at a time, gallery-vetted with human
eyes**, never as a blind sweep - the transforms interleave with the
invariants in order-dependent ways, and a wholesale rewrite is the
#351/#353 failure mode.

## Trust-but-verify finding (CONTRACT.md drift)

Mining the rules surfaced that `CONTRACT.md` had drifted from the code
(verified helper-by-helper):

- **Stage 6.15a** (`_grow_bboxes_to_content_top`) and **Stage 6.16**
  (`_align_entry_ports(tb_only=True)` + junction re-anchor) are real code
  stages **missing** from the contract table.
- **Stage 6.11** is documented as unconditional but is double-gated on
  `_explicit_grid AND center_ports`.
- **Stages 4.9 / 4.10** are gated *inside* the helper, not at the call
  site (the contract implies a caller-level `if`).
- All `engine.py:NNNN` helper line numbers are stale post-#451 (helpers
  now live in `phases/`).

The per-helper "Invariants preserved" claims otherwise held up. The
contract drift is itself the structural-debt signal the spike was filed to
probe: prose ordering documentation goes stale; machine-checkable
invariants (`assert_maintained`) do not.

## Recommendation

1. **Keep** the `maintained.py` mechanism and the two invariants. They are
   byte-identical, tested, and `assert_maintained` makes the junction/canvas
   ordering machine-checkable (closes the #386 regression class).
2. **Adopt incrementally**: a new phase that establishes a *final-boundary
   invariant* should be added as a `MaintainedInvariant`, not a hand-placed
   re-run. A phase that is a *transient transform* (its property is
   overridden later) stays procedural - it has no invariant to declare.
3. **Do not** attempt a fully-uniform run-everywhere overhaul. The
   experiment (branch `experiment/365-full-uniform`) shows it diverges on 7
   fixtures because `top_align` is a transient (the finished layout's row
   tops are deliberately non-flush by 40px) and `off_track` is
   precondition-gated. This is a documented negative result, not a TODO.
4. **If uniformity is pursued**, lift more *true invariants* one at a time,
   gallery-vetted (e.g. `off_track` behind a "consumers final" gate). Never
   a blind sweep - the transforms interleave order-dependently (#351/#353).
5. **Contract drift** is fixed separately in PR #460 (the 6.15a/6.16 rows,
   the 6.11 and 4.9/4.10 gating notes).
