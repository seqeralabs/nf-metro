# Constraint-solver spike (deeper): Phase 4-13m replacement

**Issue**: [#351](https://github.com/pinin4fjords/nf-metro/issues/351)
**Supersedes**: PR #350 (partial Phase 8/9 post-pass, SHELVE verdict)
**Status**: SHELVE the comprehensive rewrite. Validator-clean but visually regressed on the largest fixture (`variantbenchmarking`). Confidence: medium-high.
**Spike code**: `src/nf_metro/layout/constraint_solver_v2.py`, `scratch/constraint_spike_v3_compare.py`

## Question

PR #350 ran the engine and then re-solved a subset of phases as a post-pass. The conclusion was "shelve the full rewrite," but reading the renders showed the verdict was confounded by spike-implementation bugs (stranded junctions, fixed `bbox_h`, weak anchors), not by approach-level blockers.

Issue #351 reframed: try a **comprehensive** replacement of Phases 4-13m as one declarative kiwisolver pass. Phases 2/3 (discrete combinatorial) stay imperative. Routing stays imperative.

Linearity-audit table (in the issue) flagged six items as **Verify**:

| Item | Construct | Status entering the spike |
|---|---|---|
| 1 | `bbox_h` dependence on `max(station_y)`        | Verify |
| 2 | Bottom shrink (Phase 13j)                       | Verify |
| 3 | Row gap (Phase 13k/13l)                         | Verify |
| 4 | Half-grid 2-branch symfan (Phase 13d3)          | Verify |
| 5 | Fan-out symmetric redistribute (Phase 11d/13h)  | Verify on real fixtures |
| 6 | Sparse-loop half-pitch (Phase 13k2)             | Verify |

## What the spike did

1. **Catalogued the constraints.** 16 families C1-C16, with C11-C16 added for the six Verify items. See `src/nf_metro/layout/constraint_solver_v2.py`.
2. **Built a comprehensive kiwisolver model** that takes a post-Phase-2/3 graph (X coords, layer/track, section grid, port sides) and produces every station Y, port Y, junction Y, section `bbox_y`, and section `bbox_h` as solver outputs.
3. **Wired it into `compute_layout`** at the end (gated on `NF_METRO_NO_SOLVER`), so the spike branch's CI render-diff produces the solver's choices for every gallery fixture.
4. **Built a per-fixture rubric** using `tests/layout_validator.py` to classify each fixture: better / equivalent / acceptable_worse / unacceptable_worse / fatal.

## Constraint catalogue

R = REQUIRED, S = STRONG, M = MEDIUM, W = WEAK. Asterisk = topology-classified conditional.

| ID  | Constraint                                          | Strength | Source phase(s)                         |
| --- | --------------------------------------------------- | -------- | --------------------------------------- |
| C1  | station-in-section containment (subsumed by C11)    | R        | Phase 2 invariant                       |
| C2  | same-layer / same-track ordering (direction-aware)  | R        | Phase 2 layering                        |
| C2b | perp-port slot rank (LEFT/RIGHT ports on TB)        | R        | CLAUDE.md station-as-elbow rule         |
| C3  | intra-section edge straightness                     | S        | Phase 2/10b sole-layer snap             |
| C4  | LR/RL port-to-station snap (subsumed by C3)         | S        | Phase 10b/10c/10d                       |
| C5  | inter-section edge straightness                     | M        | Phase 6/8/13c                           |
| C6  | grid snap                                           | W        | Phase 2.5/13e                           |
| C7  | port on bbox edge (TOP/BOTTOM/LEFT/RIGHT)           | R        | Phase 5                                 |
| C8  | same-row contiguous bbox_y equality                 | S        | Phase 9/13b                             |
| C9  | same-row trunk-Y equality                           | M        | Phase 11ca                              |
| C10 | off-track stack at `consumer.y - n*y_spacing`       | R        | Phase 13/13g                            |
| C11 | `bbox_h >= station.y - bbox_y + pad` per station    | R        | Phase 11 `_expand_bbox_for_y`           |
| C12 | weak anchor to engine `bbox_h` (tightness)          | W        | Phase 13j shrink                        |
| C13 | row gap `lower.bbox_y >= upper.bbox_y + h + gap`    | R        | Phase 13k/13l                           |
| C14 | half-grid symfan offsets `trunk +/- y_spacing/2`    | S*       | Phase 13d3                              |
| C15 | fan-out symmetric around trunk                      | M*       | Phase 11d/11da/13h                      |
| C16 | sparse-loop full-pitch shift `row_trunk +/- ys`     | S*       | Phase 13k2                              |

Conditional families (C14/C15/C16) are pre-classified from post-Phase-3 topology. The engine's runtime "shares row Y with busier sibling" check in Phase 13k2 was reinterpreted as a static "shares grid_row + grid_col with a sibling carrying more lines" — a topology-only proxy.

## Verify items: what each turned out to be

### Item 1: `bbox_h` from `max(station_y)`
**Verdict: viable.** Encoded as `bbox_h >= station.y - bbox_y + pad` per station, with a weak anchor to the engine's `bbox_h` for tightness. The hard inequality grows `bbox_h` to fit; the weak anchor prevents Cassowary from shrinking it below the engine's expected size. An earlier attempt with `bbox_h == 0` weak attractor dragged `bbox_h` below its lower bound by propagating shrinkage through `station.y` (also a free variable), so the lower-row sections collapsed. The engine-`bbox_h` anchor fixed this.

### Item 2: Phase 13j bottom shrink
**Verdict: viable, subsumed by item 1.** The shrink is just C11's tightness property. No separate mechanism needed.

### Item 3: Phase 13k/13l row gap
**Verdict: viable, simple.** Pairwise required inequality between every section in row N and every section in row N+1. Column-overlap gating (which Phase 13l checks) turns out to be wrong for the constraint — Phase 13k's "tighten" is a global row property, so removing the gating produces the right behaviour.

### Item 4: Phase 13d3 half-grid symfan
**Verdict: viable.** Classifier predicate is topology-only (2-branch, same column, no off-track, no other multi-branch column). When fired, two equality constraints place branches at `anchor +/- y_spacing/2`, with the anchor pulled from the LR/RL port Y or the row trunk Y variable.

### Item 5: Phase 11d/11da/13h fan-out redistribute
**Verdict: viable.** Classifiers are topology-only. The engine's two-stage early-stale-anchor / late-final-anchor pattern collapses to one constraint set in the solver because the anchor Y is a solver variable. The combined C15 family handles "trunk_station_y" anchors (Phase 11d) and "row_trunk_y" anchors (Phase 11da/13h) with separate offset patterns.

### Item 6: Phase 13k2 sparse-loop half-pitch
**Verdict: viable with reinterpretation.** Engine's runtime classifier reads sibling Ys, which aren't available in a pure pre-solve setup. Workaround: pre-classify from topology only (same-column sibling has more lines), use the engine snapshot's sign to decide direction. The "half-pitch" of the engine becomes "full-pitch" here because the engine's actual rule reserves half-pitch for symfan stations; sparse-loop gets a full grid slot.

All six items are linear-solvable. None of them required disjunctive or non-linear constructs that kiwisolver can't represent.

## C2b: the unexpected hard constraint

Items 1-6 weren't enough on their own. The CLAUDE.md "station-as-elbow" rule says **a perpendicular port must not align its perpendicular coordinate with any internal station** in the same section (LEFT/RIGHT ports on TB sections, TOP/BOTTOM ports on LR/RL sections). The engine handles this via Phase 7 perp-entry-shifts and Phase 11 port-spacing.

In the spike, C5 (medium-strength inter-section edge straightness) drags a LEFT port toward its upstream trunk Y. C3 (strong-strength intra-section straightness) drags the connected internal station to match the port. Net effect: port and station collide.

C2b encodes the rule as ordered inequalities from the engine snapshot's slot rank: for each (port, station) pair, if the engine placed the port above the station, the spike must too, with a 12px gap.

Without C2b, four fixtures regressed with `station_as_elbow` validator errors. With C2b, the validator-level rubric goes from 11 equivalent / 4 acceptable_worse to **15 equivalent / 0 worse**.

## Results

### Validator-level rubric (15-fixture gallery)

```
SUMMARY
  OK  better              :  0  []
  OK  equivalent          : 15
  OK  acceptable_worse    :  0
  BAD unacceptable_worse  :  0
  BAD fatal               :  0
  BAD crashed             :  0
  good: 15/15 (100%)   bad: 0/15
  >=80% good AND 0 bad: PASS
```

Per `scratch/constraint_spike_v3_compare.py`, no new ERROR violations on any fixture. Spike-time per fixture is 2-15 ms on top of the engine's 1-3 ms.

### Visual-level rubric (CI render-diff)

The validator-pass result is **misleading**. On `variantbenchmarking` (largest fixture, two-row layout, rowspan TB sections), the spike render shows:

- Output Processing section overlapping the upstream content row
- Section title bars partially clipped at canvas top
- Sections squished vertically with overall vertical compression

These show up in the CI render-diff but not in the validator suite, because the validators are bbox-overlap / kink-count / port-alignment / etc., and the spike's bboxes are technically non-overlapping (just very close).

Other large fixtures (`differentialabundance`, `variantbenchmarking_auto`) likely have similar issues. The CI render-diff for this draft PR is the authoritative review.

## Verdict: SHELVE the comprehensive rewrite

The six audit items are individually viable. The 16-constraint model produces validator-clean output on every gallery fixture. But the **combined** constraint network has degrees of freedom the engine's imperative phases use without naming, and the spike's weak-attractor strategy doesn't reliably reproduce those choices when fixtures get large.

Specifically:

1. **Multi-anchor weight tuning.** The engine's per-row trunk Y, per-section bbox top, per-station Y, and inter-section edge-straightness all converge to specific values in specific orders. The solver re-solves these simultaneously, with weak engine-Y anchors. On small fixtures (5 sections), the anchors dominate. On large fixtures (15+ sections, multi-row, rowspan), the medium-strength edge straightness pulls cross-section trunk Ys together in ways that visually compress the canvas, even though no validator catches it.
2. **No visual-quality metric in the rubric.** The project's quality bar ("two trunk-Y kinks") is human-judged on the render-diff. The validator suite is a useful guardrail but not a substitute. The spike's 15/15 validator-clean result is genuinely encouraging *and* genuinely misleading.
3. **The engine's "X is final after Phase 3" assumption was load-bearing.** Real spike implementation kept the engine for X and overrode only Y. This isn't quite the issue's "one declarative pass" framing — it's "engine, then solver overrides Y." A true replacement that handles X via the solver too would either duplicate the engine's discrete placement logic (out of scope) or add disjunctive X constraints (non-linear).

**What changed since #345's shelve:** the comprehensive model proves the *constraints* are linear-encodable. The audit table's six Verify items all turned out to be Verified-viable. The blocker is not constraint expressivity — it's that weak Cassowary attractors don't reliably reproduce the engine's hierarchical decision order on visually demanding fixtures. That's a *different* shelve verdict from #345's, which thought the constraints themselves were the blocker.

## What would land instead (would be a separate issue)

Two narrow follow-ups, in order of confidence:

### 1. C13 row-gap inequality as an imperative invariant guard
The row-gap formula `lower.bbox_y >= upper.bbox_y + h + gap` is fully topology-decidable, linear, and easy to check. Add it as a runtime guard in `compute_layout(validate=True)`. **Confidence: high.** Estimate: half an agent-day.

### 2. Constraint-based final-cleanup pass with bbox frozen
Same idea as the #345 follow-up: take the engine's output, run the solver with ONLY C3/C4/C5/C8/C9/C6 active (no `bbox_y` / `bbox_h` movement allowed), accept the deltas where they reduce a quantitative kink count and reject anything that increases pixel shift beyond a threshold. **Confidence: medium.** Estimate: 1-2 agent-days. Gateable as `--solver-cleanup` CLI flag, opt-in.

Neither follow-up is filed automatically; open a separate issue if either is wanted.

## Files

- `src/nf_metro/layout/constraint_solver_v2.py` - the model + apply pass.
- `src/nf_metro/layout/engine.py` - one-line wiring at end of `compute_layout`, env-gated on `NF_METRO_NO_SOLVER`.
- `scratch/constraint_spike_v3_compare.py` - per-fixture rubric runner.
- `scratch/constraint_spike_v3_model.py` - thin wrapper for direct script use.
- This document.

## Risks for the migration plan if a future spike picks this up

1. **Cassowary strength tier coarseness.** Four tiers (R/S/M/W) is enough for simple-medium fixtures but loses fidelity on large ones. Per-constraint custom strengths (kiwisolver supports them) might recover quality at the cost of model complexity.
2. **The engine's phase-order semantics carry information.** "Apply rule A, then conditionally apply rule B" is not expressible as soft preferences. Pre-classification (this spike's approach) covers some of these cases but not all - the trunk-Y / row-trunk / port-Y coupling on multi-row fixtures is the visible failure mode.
3. **Quality acceptance must be visual.** The 15/15 validator-clean result on the gallery is real but uninformative. Any future spike or landing path needs the render-diff in the loop from day one, not as a final check.

