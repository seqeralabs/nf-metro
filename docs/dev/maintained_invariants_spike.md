# Spike: declarative maintained invariants (#365)

**Verdict: partial land.** The mechanism works byte-identically for the
*simple, cheap-to-check* restore invariants (junction positioning, canvas
top-margin) and removes a real class of bug (#386). It does **not** pay for
the complex restores (off-track stacking, row top-align, fan re-center),
for a concrete mechanism-level reason documented below. This is outcome
(a) from the issue, scoped: "promising for a subset, procedural phases
survive elsewhere."

This is explicitly **not** a constraint solver. The #353 failure mode
(Cassowary weak attractors couldn't reproduce the engine's hierarchical
decision order) is not relitigated here: the repairs *are* the existing
constructive helpers, applied in a *declared* order, not numeric
attractors relaxed to equilibrium.

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

The restore phases **are** maintained invariants. The spike lifts them
into data: each declares a *predicate* (does it hold?), a *repair*
(re-establish it), and a *priority* (lower repairs first). `maintain`
applies the repairs in priority order to a fixpoint, so one
`maintain(graph, ...)` call after each constructive phase subsumes the
scattered manual re-runs.

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

## Why these two invariants work and the others don't

The decisive finding: **a guard predicate is not the same as the repair's
postcondition, and the maintained-invariant predicate must mirror the
*repair*.**

For an idempotent repair `R`, the faithful predicate is "would `R` be a
no-op?" - i.e. "is the state already exactly what `R` would produce?". The
hand-written pipeline re-ran each restore *unconditionally*, so the
invariant it kept is **exact equality**, not the looser "good enough"
condition the validate-mode guards check.

- `_position_junctions` writes `junction.xy = _compute_junction_xy(...)`,
  a pure function of port coordinates that reads no stored junction state.
  The "would it be a no-op?" predicate is a one-line exact comparison
  against `_compute_junction_xy`. **Cheap and exact.** (A first cut used a
  0.5px tolerance and diverged on `genomeassembly_staggered`, because a
  sub-pixel port nudge moved the target while the manual code re-snapped
  it exactly. Tightening to FP-epsilon restored byte-identity - direct
  evidence that the invariant is exact equality.)
- `_shift_graph_into_canvas` is a uniform whole-graph translate; its
  no-op predicate is `min_section_bbox_top >= margin`, identical to the
  helper's own early-return. **Cheap and exact.** Being a translate, it
  moves junctions and ports together, so it can never break the junction
  invariant - the two compose trivially.

The complex restores fail the cheapness test:

- `_reanchor_off_track_to_consumer` pins each off-track at *exactly*
  `consumer.y - n*y_spacing`, where the target accounts for trunk
  line-track bands, sibling-icon clearance, and iterative overlap
  stepping (`_place_off_track_above_consumers`). The matching guard,
  `_guard_off_track_inputs_above_consumer`, only checks the *weak*
  condition `off.y < consumer.y - tol`. A predicate that mirrors the
  *repair* would have to re-derive the entire placement computation - the
  predicate costs as much as the repair and is a fresh bug surface. The
  guard cannot be reused as the predicate (it would say "holds" while the
  repair would still move the icon, diverging).
- `_top_align_row_bboxes_only` and `_recenter_full_bundle_columns` have
  the same shape: complex postconditions whose faithful no-op predicate is
  a re-derivation.

So the value of the mechanism is gated on **predicate cheapness**, not on
the priority-ordered fixpoint (which is sound - the convergence backstop
and the junctions+canvas composition both hold). Where the repair's
postcondition is a cheap exact predicate, lifting it removes real
bookkeeping and a real bug class. Where it isn't, the predicate-authoring
cost exceeds the benefit of declaring the order, and the implicit
procedural ordering should stay.

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
2. **Adopt incrementally**: a new restore-class phase whose postcondition
   is a cheap exact predicate should be added as a `MaintainedInvariant`,
   not a hand-placed re-run.
3. **Do not** force the complex restores (off-track, top-align, recenter)
   through the mechanism. Their faithful predicate is a re-derivation of
   the repair; the ordering is better left procedural until/unless those
   helpers are refactored to expose a cheap "is this already placed?"
   query.
4. **Fix the contract drift** (separate change): add the 6.15a/6.16 rows,
   correct the 6.11 and 4.9/4.10 gating notes.
