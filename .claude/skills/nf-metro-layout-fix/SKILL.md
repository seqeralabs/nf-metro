---
name: nf-metro-layout-fix
description: Drive code-level fixes to nf-metro when a real pipeline render exposes a layout bug that isn't a mmd mistake, without regressing pipelines that already ship metro maps. Use when working in nf-metro's src/ (layout engine, routing, parser) to fix kinks, station overlaps, breeze-past, asymmetric fans, bypass routing, or bbox overflow on a real pipeline diagram. Covers the savepoint tag pattern, the invariant-test-first-then-fix-then-runtime-validator loop, gallery regression vetting with build_gallery and build_render_diff, converting global fixes to conditional ones so other renders aren't pushed around, and the additive-commits-only PR chain rule. For the per-PR vetting workflow (reconcile vs main, /simplify, render+diff, sweep comments, rewrite description, trigger CI, post-merge cleanup), see the `pr-chain-vet` skill. For authoring the mmd content itself (deciding lines, stations, sections), see the `pipeline-metro-diagram` skill. For routine gallery regression testing of nf-metro, see `render-topologies`.
---

# Fixing nf-metro Layout Bugs Surfaced by a Real Pipeline Render

Captures the workflow for driving code-level fixes to nf-metro when a
pipeline's metro map exposes a layout case the engine handles wrong. The
goal: ship the fix without regressing the renders for other pipelines that
already use nf-metro.

If the bad render is actually a mmd mistake (line drawn through a station
that doesn't consume it, missing `off_track` directive, undersized
`rowspan`, etc.), don't reach for this skill — go fix the mmd in the
pipeline repo. The `pipeline-metro-diagram` skill covers the triage for
"is it mmd or nf-metro?". Only when the mmd is correct and the engine
still produces a bad layout do you arrive here.

## When to use this skill

Trigger when:

- A pipeline's rendered mmd looks wrong (kinks at section boundaries,
  station or icon overlaps, breeze-past, broken trunk alignment, bypass
  routing through a non-consumer, asymmetric fans, bbox overflow) **and**
  you've already verified the mmd is correct.
- You're about to modify code in `src/nf_metro/layout/engine.py`,
  `src/nf_metro/routing/`, or `src/nf_metro/parser/` to fix a layout case
  surfaced by a real pipeline diagram.
- You're managing a stack of PRs against nf-metro `main` from a single
  pipeline-integration session and want the chain rules in one place.

## Step 1: Make a save point first

Before touching nf-metro, tag the current state of the pipeline's render as
"good enough":

```bash
# In the pipeline repo
git tag pipeline-render-baseline <commit>

# In nf-metro
git tag <pipeline>-render-savepoint <commit>
git push origin <pipeline>-render-savepoint
```

Treat the savepoint as immutable. Every later step measures regressions
against it. If the iteration goes off the rails, you can return to this tag
and start over with a different approach.

## Step 2: Per-fix iteration loop

For each layout issue:

1. **Reproduce minimally.** Render the pipeline mmd with `--debug` and the
   exact params it ships with. Capture the SVG. Identify the offending
   coordinates by parsing the SVG (`<rect>` station markers, `<path>` edge
   data) — visual inspection alone is unreliable for sub-pixel issues.

2. **Form a hypothesis.** What in `src/nf_metro/layout/engine.py` (or
   `routing/core.py`, `parser/mermaid.py`) is producing the wrong output?
   The engine is a long pipeline of phases; the bug is almost always in a
   specific phase rather than across many.

3. **Add an invariant test first, then fix.** Each layout invariant should
   fail on the bug case and pass after the fix. Examples from prior sessions:
   - `test_row_trunk_marker_cy_consistent` (trunk Y consistent across
     same-row sections)
   - `test_no_station_or_icon_overlap` (no two station/icon bboxes intersect)
   - `test_lines_dont_cross_non_consumer_markers` (no line passes through a
     station that doesn't consume it)
   - `test_all_stations_snap_to_grid` (with explicit exceptions for half-grid
     2-branch fans)
   - `test_bypass_v_has_horizontal_segment` (virtual bypass stations have a
     visible flat segment ≥ 20 px)
   - `test_section_bbox_contains_all_content` (no station/icon overflows its
     section's bbox)
   - `test_no_kink_at_section_boundary` (inter-section trunk Ys match)
   - `test_loop_column_stations_share_x` (column-mate stations share X)

   Parametrize each invariant over multiple fixture mmds AND multiple render
   parameter sets (savepoint params, rnaseq defaults, no-center-ports). A
   bug that only manifests at non-default params has bitten us before.

4. **Verify against the gallery.** After every fix, run `python
   scripts/build_gallery.py --debug` and `build_render_diff.py` against
   `origin/main`. If any existing example shifts visually, classify the
   shift:

   - **Improvement / neutral**: fine.
   - **Detrimental** (new crossings, mad routing, bbox overflow, label
     overlap, station collisions, broken trunk alignment, breeze-past): the
     fix needs to become conditional. Don't ship the fix in its current
     form.

5. **Convert "fix it everywhere" to "fix it conditionally" when needed.** A
   common failure mode: a fix that works for the new pipeline introduces
   spurious work for renders that didn't need it. Examples:
   - Bbox padding change that grows row gaps even where no bypass routing
     occurs → restrict to column-overlapping section pairs only.
   - Bypass V flat-segment minimum that pushes 4 unrelated gallery examples
     vertically → only enforce when there's horizontal headroom.

   The pattern: identify the precise topological precondition that makes
   the fix needed, and gate the new behaviour on that precondition.

6. **Add a runtime validator (not just a test).** End users running
   `nf-metro render` should see a clear error if the layout violates an
   invariant — not produce a silently broken render. Add a
   `_guard_<name>` function called from `compute_layout`'s `validate` block
   that raises `PhaseInvariantError` with the offending coordinates.

## Step 3: The "improvement ratchet"

Every regression we identify adds an invariant before its fix lands. The
test suite grows monotonically. Later PRs can't accidentally re-introduce
problems that earlier PRs solved. This is the only way the engine — built
as a long sequence of mutating phases — stays sustainable across many
pipelines.

When dispatching agents to do nf-metro work, include in their brief: "you
must add an invariant test that fails on the bug AND a matching runtime
validator. Both are mandatory; the fix is not complete without them."

## Step 4: Managing the PR chain back to nf-metro main

Layout fixes accumulate into a stack of PRs against nf-metro `main`. The
chain pattern:

- The bottom-of-chain PR is based on `main`.
- Each subsequent PR is based on the previous PR's branch.
- As each merges, the next PR's base auto-updates to `main`.

The chain-level rule that matters here: **no force-pushes**. Every change
to a PR in the chain is an additive commit; to undo something already in a
branch, append a `git revert <hash>`. Rewrites break GitHub review threads
and silently destroy other people's local state.

For the per-PR work of taking one PR from the chain and making it
mergeable - reconciling against `main`, the `/simplify` pass, gallery
render+diff, classifying deltas, sweeping narrative comments, rewriting
the description, triggering CI, and the post-merge re-target/delete order -
see the `pr-chain-vet` skill. It's the operational counterpart to the
authoring concerns this skill covers.

## Step 5: Reconciling stack regressions

When the chain is built progressively, a regression introduced by an early
PR may only become visible later. The triage workflow:

1. After all fixes land in the savepoint, render every PR's tip (rebased onto
   main individually) and diff against main.
2. Classify each PR's effect as I / N / D.
3. For each detrimental, identify the **earliest** PR where the issue first
   appears. The fix should be folded back into that PR (as an additive
   commit on its branch), not as a top-of-stack bolt-on PR.
4. Re-vet the modified PR + all downstream PRs to confirm no new issues.
