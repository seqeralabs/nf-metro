# Constraint-solver spike: row-Y alignment

**Issue**: [#345](https://github.com/pinin4fjords/nf-metro/issues/345)
**Status**: SHELVE the full rewrite. One narrow follow-up worth a separate issue.
**Spike code**: `scratch/constraint_spike.py`, `scratch/constraint_spike_v2.py`

## Question

The CONTRACT-documented phases 8, 9, 10b-d, 11ca and 13c are a hysteresis
cluster: PRs #208, #209, #211, #212, #221, #223 each fixed a different
visual glitch in this region, and each fix had narrow preconditions so
it didn't re-break the others. Can this row-Y alignment region be
expressed as a kiwisolver constraint system (linear, with hard/soft
strength tiers) and dropped in as a parallel backend behind a feature
flag?

## What the spike did

1. **Catalogued the constraints** the six referenced issues encode.
2. **Built two kiwisolver prototypes** (`constraint_spike.py`,
   `constraint_spike_v2.py`) that load a fixture, run the engine, then
   re-solve station / port Ys from the post-engine topology
   (bbox geometry, layer/track assignment, edge graph, port sides).
3. **Compared solver output to engine output** numerically on two
   fixtures explicitly named by the hysteresis bugs:
   `genomeassembly.mmd` (issue #208) and `variantbenchmarking.mmd`
   (issues #221, #223).

## Constraint catalogue

For each constraint, the spike maps it to a kiwisolver expression and a
strength tier. R/S/M/W = REQUIRED / STRONG / MEDIUM / WEAK.

| ID  | Constraint                                                | Strength | Source phase(s)                       |
| --- | --------------------------------------------------------- | -------- | ------------------------------------- |
| C1  | `bbox_y + pad <= station.y <= bbox_y + h - pad`           | R        | Phase 2, validated in `_guard_*`      |
| C2  | Same-layer ordering linearised by engine's track sort     | R        | Phase 2 layering + Phase 13e snap     |
| C3  | Intra-section edge straight: `src.y == tgt.y`             | S        | Phase 2/10b (sole-layer snap)         |
| C4  | LR/RL port aligns with sole-connected station Y           | S        | Phase 10b/10c/10d                     |
| C5  | Inter-section edge straight: `exit.y == entry.y`          | M        | Phase 6/8/13c                         |
| C6  | Grid snap: `station.y == origin + n*pitch`                | W        | Phase 2.5 / Phase 13e                 |
| C7  | TOP/BOTTOM port on bbox edge; LR/RL port within bbox      | R        | Phase 5 + `_guard_ports_on_boundaries`|
| C8  | Same-row sections share `bbox_y`                          | S        | Phase 9 + Phase 13b                   |
| C9  | Same-row sections' trunk stations share Y                 | M        | Phase 11ca                            |
| C10 | Off-track input at `consumer.y - n*y_spacing`             | R        | Phase 13 (lift) / Phase 13g (reanchor)|

C1 / C2 / C7 / C10 are HARD. The rest are SOFT, ranked by visual
priority.

## What the spike found

### Findings the constraint approach handles well

1. **The catalogue is enumerable and mostly linear.** Every constraint
   above is either a linear equality / inequality or (in C2's case) a
   disjunction that *can* be linearised by inheriting the engine's
   track ordering. There is no truly non-linear constraint among the
   row-Y alignment phases.

2. **Off-track placement (C10) is a clean linear equality.** Phase 13's
   "lift `n * y_spacing` above consumer" is exactly
   `y_input = y_consumer - n * y_spacing`. The solver handles it
   trivially; the imperative version needs two passes (Phase 13 then
   Phase 13g re-anchor after grid snap).

3. **Trunk-alignment is more uniform under the solver than under the
   engine.** On `variantbenchmarking`, the engine's row 0 has *two*
   distinct trunk Ys (103.2 and 143.2) because the trunk aligner's
   bundle-match heuristic skipped some sections. The solver enforces
   a single trunk Y across the row (131.2). The solver is more
   consistent than the engine - which is the desired direction.

4. **Per-row variable pitch is not required by the spike.** The
   engine's `_align_row_y_grids` computes a per-row pitch
   (`effective_y_spacing`, e.g. 40 vs 47 in variantbenchmarking) when
   stations carry many lines. A unified pitch (max across the graph)
   simplifies the model with no observed quality cost on the spike
   fixtures.

### Findings that break the "drop-in replacement" plan

1. **bbox_h is non-linear in station Ys.** The engine grows bbox_h
   via `_expand_bbox_for_y` (Phase 11) and shrinks it via
   `_shrink_bboxes_to_content_bottom` (Phase 13j). Both depend on
   `max(station_y) - bbox_y + pad`, i.e. a `max` aggregate. Encoding
   `max` in kiwisolver needs an auxiliary variable per section and
   N + 1 inequalities per content-station - tractable but doubles the
   model size and adds a coupling that's invisible in the engine.

2. **Phase ORDER carries information the solver cannot recover.** Many
   engine passes do "apply rule A, then apply rule B which may undo
   part of A". Examples:
    - Phase 11d/11da fan stations around a stale port Y; Phase 13h
      re-fans them around the final trunk Y.
    - Phase 13 (off-track lift) calls `_shift_graph_into_canvas`,
      globally translating every coordinate.
    - Phase 13e snaps to grid AFTER Phase 11ca shifts down by a
      non-integer pitch delta.

   A constraint solver finds an assignment that maximises sum of soft
   satisfactions. It cannot model "do A, then conditionally do B".
   Approximating that with priority weights is fragile: kiwisolver's
   strength tiers (REQUIRED / STRONG / MEDIUM / WEAK) are coarse, and
   re-creating the engine's exact resolution by tuning relative weights
   is a tuning job for every fixture.

3. **The matching benchmark is fuzzy.** "Reproduce the engine pixel-
   perfect" is the wrong target: the engine's exact Ys are an artifact
   of phase ordering, not the intended layout. On `variantbenchmarking`,
   the solver and engine disagree by up to 174px on a single port, but
   the *solver's* output is more constraint-consistent. There is no
   ground truth to measure against without a separate quality rubric
   (e.g. "no kinks > N px", "trunk alignment within row delta < X").

### Numbers

`constraint_spike_v2.py` output (full engine reproduction target, NOT
the right benchmark, see Finding 3):

| fixture                    | n  | exact <0.5px | near <5px | big >=10px | max delta |
| -------------------------- | -- | ------------ | --------- | ---------- | --------- |
| genomeassembly.mmd         | 26 | 1 (4%)       | 2 (8%)    | 23         | 32px      |
| variantbenchmarking.mmd    | 64 | 0 (0%)       | 0 (0%)    | 64         | 174px     |

v1 (`constraint_spike.py`) was closer on genomeassembly (25/26 within
5px) only because it held bbox_y fixed at the engine's value, so the
row-align constraints had no freedom. That's a degenerate match -
v2's larger deltas are the *honest* signal once bbox_y is allowed to
shift.

## Verdict: SHELVE

A full constraint-system rewrite is **not viable as a drop-in
replacement** for the row-Y alignment region. The blockers are:

1. **Non-linear bbox_h coupling** - solvable with auxiliary variables,
   but doubles model size.
2. **Phase-order semantics not expressible** - kiwisolver's strength
   tiers can't represent "A, then conditionally undo".
3. **No clean correctness benchmark** - engine output isn't ground
   truth; visual review on rendered SVGs would be the only honest
   acceptance test, and that's not automatable.

Confidence: medium. Two fixtures is enough to see the structural
blockers but not enough to characterise every edge case.

## One viable narrow direction (would be a separate issue)

Rather than *replace* the row-Y phases, **add a constraint-based
"final-cleanup" phase**: take the engine's output, run a solver with
only the C3 / C4 / C5 / C8 / C9 / C6 soft constraints (no bbox_y
movement allowed), and accept the deltas where they reduce a
quantitative "kink" metric. This would:

- Catch residual kinks the imperative phases leave behind
- Not need to model bbox_h coupling (bbox stays fixed)
- Not need to reproduce phase order (it runs once, last)
- Be feature-flag-gateable as `--solver-cleanup`

The render-preview verdict gate could automatically reject solver
output that produces > N pixel shifts from the engine baseline,
keeping the change opt-in until trust is established.

Estimate: 1-2 agent-days if pursued. **Not opening this proactively
- file a separate issue if the maintainer wants it.**

## Files

- `scratch/constraint_spike.py` - v1: bbox_y held fixed, near-match on
  genomeassembly only.
- `scratch/constraint_spike_v2.py` - v2: bbox_y as variable, row-align
  + row-trunk + off-track constraints added. The honest signal.
- This document.

No `src/` code was added or modified, per the issue's scope rules.
