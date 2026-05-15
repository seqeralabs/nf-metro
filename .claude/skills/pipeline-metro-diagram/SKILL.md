---
name: pipeline-metro-diagram
description: Author and iterate on an nf-metro diagram for an nf-core (or any Nextflow) pipeline. Use whenever the user wants to create a new metro map for a pipeline, set up the `assets/metro_map.mmd` + `docs/dev/metro_map.md` scaffolding, or iterate on an existing pipeline mmd to improve fidelity to the workflow code or visual clarity. Trigger on phrases like "make a metro map for pipeline X", "create an nf-metro diagram for Y", "set up nf-metro on my pipeline", "iterate on the mmd for pipeline Z", "the metro map for funcscan doesn't match the workflow", or any request that involves writing pipeline-level `.mmd` files (as opposed to gallery / fixture rendering, which is `render-topologies`).
---

# Authoring a Pipeline Metro Map

Captures the workflow for designing, drafting, and iterating on an nf-metro
`assets/metro_map.mmd` for a real Nextflow pipeline (typically nf-core).

## Scope and relation to other skills

This skill is about **upstream authoring** — turning a pipeline's workflow code
into a metro map that lives in the pipeline repo. It is distinct from:

- **render-topologies** (in this repo): regression-tests nf-metro itself by
  rendering all gallery fixtures and pixel-diffing against `origin/main`. Use
  that one when you've changed nf-metro's layout/render code, not when you're
  drafting a pipeline diagram.
- **nf-metro layout debugging**: when the mmd is correct but nf-metro itself
  produces a bad layout. Reach for that path only after you've ruled out mmd
  mistakes (see step 5 below).

## When to use this skill

Trigger when the user wants to:

1. Create a brand-new metro diagram for a pipeline that doesn't have one
2. Update an existing pipeline mmd because steps were added/renamed
3. Improve a mmd's fidelity to the actual workflow code (channel routing,
   branch logic, study-type variants)
4. Tune layout params (spacing, line order, port style) for a pipeline diagram

## Why the workflow matters

The hard part of authoring a pipeline metro map is **not** writing mermaid
syntax — it's deciding what the lines and stations should *be* so the diagram
reads as the same pipeline a developer sees in the code. Get the model right
first, then fight the layout.

## Step 1: Study the pipeline workflow code

Before drafting any mmd, read the pipeline. The mmd is a faithfulness exercise:
mistakes here cause every downstream iteration to chase visual symptoms of a
modelling error.

What to inspect:

- `workflows/*.nf` (the main workflow) and any subworkflows it `take:`s data
  through. Follow channel routing per study type / branch / option.
- `main.nf` and config files for top-level params that control which paths run.
- The `nextflow_schema.json` for input file types (these become file-input
  stations).
- Recent merged PRs touching the workflow — they often reveal which paths are
  optional or recently changed.

Build a mental list of:

- **Inputs**: every primary input file or string the pipeline accepts.
- **Outputs**: the visible deliverables (reports, bundles, plots).
- **Branches**: which inputs trigger which paths, and where paths reconverge.
- **Modules**: the named processes/subworkflows the user would recognize.

## Step 2: Decide on lines

Each major variant or study type becomes a metro line with a distinct color.
For most pipelines this is 1-6 lines. Some heuristics:

- If two inputs flow through identical modules end-to-end, they may be one
  line (e.g. paired vs single-end), not two.
- If a "main" path and an "optional QC" path co-exist, they are usually
  separate lines — even if they share most stations.
- Use nf-core branding colors where possible, but pick distinguishable hues
  (avoid two greens or two blues that read as the same line under
  colour-blind rendering).

Audit each line's edges for accuracy. The most common modelling bug is a
line passing through a station that doesn't actually consume it — a
"breeze-past". A station should only sit on a line if the underlying module
actually processes that line's data.

## Step 3: Section structure

Group stations into named subgraphs (sections). Each section is a logical
phase of the pipeline ("Data import and preparation", "Differential analysis",
"Reporting", etc.).

- Place sections on the grid with `%%metro grid: <id> | row,col,rowspan,colspan`.
- Use `rowspan` when one section is much taller than the rest and you want
  adjacent sections to stack alongside it (see the differentialabundance map
  where `data_prep` spans two rows so `differential` and `functional` can sit
  to its right at half height each).
- Keep flow left-to-right at the top level (`graph LR`); place fan-out
  sections vertically by row.
- Inter-section edges must live outside all `subgraph`/`end` blocks.

## Step 4: File-input stations and off-track inputs

For file inputs that should appear as document icons rather than as labelled
module stations, use empty-label stations and `%%metro file:` directives:

```text
%%metro file: meta_in | YAML | Contrasts
...
meta_in[ ]
meta_in -->|rnaseq| validator
```

If an input enters the pipeline mid-section but the line shouldn't be drawn
running into it from the trunk (e.g. an optional gene set file used only by
one downstream module), declare it `%%metro off_track: <id>` so it lifts above
the trunk instead of forcing the line to detour through it.

Stacked-files and folder icons are available for batched / directory inputs:
see `docs/guide.md` in this repo for the full directive reference.

## Step 5: Iteration loop

Render, inspect, edit. Repeat until the diagram both *matches the pipeline*
and *reads cleanly*.

### Render command

Start with rnaseq-style params (the canonical nf-core baseline). Use the
`--debug` flag on early iterations to see grid lines and bbox boundaries —
this exposes most layout problems instantly.

```bash
nf-metro render assets/metro_map.mmd \
  -o /tmp/out.svg \
  --theme light --x-spacing 60 --y-spacing 40 --debug
```

For a more visually relaxed diagram (or one with many fan-outs / sections
that crowd at the default spacing), use the savepoint-quality params:

```bash
nf-metro render assets/metro_map.mmd \
  -o /tmp/out.svg \
  --theme light --x-spacing 70 --y-spacing 55 \
  --no-straight-diamonds --line-order definition --center-ports
```

`--line-order definition` keeps lines stacked in the order they appear in the
`%%metro line:` directives, which usually reads more naturally than the
default heuristic.

### What to look for in each render

- **Stations sitting on a line that doesn't consume them** (breeze-past): fix
  the mmd by removing that line ID from the relevant edge labels.
- **Labels off-centre, station/icon overlaps, kinks at section boundaries**:
  these are usually layout issues — but check the mmd first.
- **A line crossing many non-consumers in a row**: usually means the mmd has
  the line passing through a section it shouldn't enter at all.
- **Bypass routing for non-consumed lines**: nf-metro handles this with
  virtual hidden stations. If a section explicitly doesn't process a line,
  the bypass should arc over the trunk cleanly.
- **Asymmetric fans**: lines fanning out from one station should land
  symmetrically on their targets. Check that the `line_order` matches the
  visual top-to-bottom order you want.
- **Stacked labels in a uniform X column**: not necessarily a problem.
  Labels naturally align when stations share an X coordinate, and a uniform
  column often reads cleaner than a staggered one.

### When the mmd is wrong vs when nf-metro is wrong

If a render looks bad, **first check the mmd**:

- Are the line IDs on each edge label what you actually meant?
- Are off-track directives set for inputs that shouldn't sit on the trunk?
- Do grid spans (`rowspan`, `colspan`) actually fit the content? An undersized
  span squeezes stations together; an oversized one leaves dead space.
- Are inter-section edges declared outside `subgraph` blocks?
- Are entry/exit port directives (`%%metro entry:`, `%%metro exit:`) consistent
  with the lines that actually cross those section boundaries?

If the mmd is correct but nf-metro still produces a bad layout, that's a
nf-metro bug. The rest of this skill (Steps 8–11) is the workflow for fixing
those bugs without regressing the renders for other pipelines that already
ship a metro diagram.

### Tradeoffs

Pipeline accuracy and visual cleanness sometimes pull against each other.
A diagram that captures every conditional branch is technically accurate but
unreadable; one that elides minor optional paths can be more useful as
documentation. Sensible simplifications (e.g. combining two trivially similar
inputs onto one line, eliding a one-off helper module) are fine — note them
in the dev doc so a future maintainer knows the simplification was deliberate.

## Step 6: Common patterns and pitfalls

Captured from real authoring sessions:

- **Mixed line-bundle membership**: stations where most columns carry the
  full bundle but one column carries a single line need careful routing.
  Check that the minor line doesn't visually attach to stations it doesn't
  use.
- **Bypass routing through a trunk station that doesn't consume the line**:
  nf-metro handles this with virtual hidden stations. If you see the bypass
  jumping back through the station, the mmd probably has the line on an edge
  it shouldn't be on.
- **"Breeze-past" stations**: a line drawn through a station that doesn't
  actually consume it. Always a mmd bug.
- **Fan symmetry**: when one station fans out to N downstream stations,
  `--line-order definition` plus the order of your `%%metro line:` directives
  controls top-to-bottom placement.
- **Section boundary kinks**: usually caused by mismatched entry/exit port
  directives or by stations placed near the section edge. Try `--center-ports`
  before debugging the mmd.
- **Stacking labels**: when many stations share the same X coordinate, their
  labels stack. This is often fine and reads cleaner than staggered placement.

## Quick reference: the differentialabundance map

A worked example sits at
`~/projects/differentialabundance-metro/assets/metro_map.mmd` with the
rendered images alongside in `docs/images/`. It exercises:

- 4 study-type lines (rnaseq, affy, maxquant, geo)
- 5 sections with explicit `%%metro grid:` placement
- A 2-row rowspan on `data_prep` to balance against two half-height sections
- File-input stations with `%%metro file:` and `%%metro off_track:` for
  optional inputs
- `--line-order definition` and `--center-ports` for the savepoint render

Use it as a template for a new pipeline with multiple study-type branches.

---

## Step 8: When nf-metro itself needs to change

A new pipeline often exposes layout cases nf-metro hasn't seen before. The
goal of this section is to fix those without breaking renders for pipelines
that already ship a metro map.

### Make a save point first

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

### Per-fix iteration loop

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

### The "improvement ratchet"

Every regression we identify adds an invariant before its fix lands. The
test suite grows monotonically. Later PRs can't accidentally re-introduce
problems that earlier PRs solved. This is the only way the engine — built
as a long sequence of mutating phases — stays sustainable across many
pipelines.

When dispatching agents to do nf-metro work, include in their brief: "you
must add an invariant test that fails on the bug AND a matching runtime
validator. Both are mandatory; the fix is not complete without them."

## Step 9: Managing the PR chain back to nf-metro main

Layout fixes accumulate into a stack of PRs against nf-metro `main`. The
chain pattern:

- The bottom-of-chain PR is based on `main`.
- Each subsequent PR is based on the previous PR's branch.
- As each merges, the next PR's base auto-updates to `main`.

### Strict rules

- **NO force-pushes.** Every change to a PR is an additive commit. To undo
  an earlier commit, append a `git revert <hash>` — don't rewrite history.
- **Each PR must be vetted before review.** The vetting workflow:
  1. Render the gallery on the PR HEAD.
  2. Diff against the parent (the prior PR's HEAD, or `main` for the bottom
     of the chain).
  3. Inspect every changed example visually. Classify deltas.
  4. For each detrimental, append a fix commit.
  5. Run the simplify skill (`/simplify`) on the changed code; commit as a
     separate refactor commit.
  6. Sweep narrative comments off the PR. Keep the CI render-preview
     comment.
  7. Rewrite the PR description to be standalone: explain the net diff vs
     `main` without referencing the chain or in-flight history.
  8. Ensure CI's render-preview workflow ran on the latest commit (append
     `git commit --allow-empty -m "chore: trigger CI"` if needed).

### After merge

- Delete the remote branch (`gh api -X DELETE
  repos/<owner>/<repo>/git/refs/heads/<branch>`).
- Delete the local branch and worktree.
- Re-target the next PR's base to `main`.

## Step 10: Reconciling stack regressions

When the chain is built progressively, a regression introduced by an early
PR may only become visible later. The triage workflow:

1. After all fixes land in the savepoint, render every PR's tip (rebased onto
   main individually) and diff against main.
2. Classify each PR's effect as I / N / D.
3. For each detrimental, identify the **earliest** PR where the issue first
   appears. The fix should be folded back into that PR (as an additive
   commit on its branch), not as a top-of-stack bolt-on PR.
4. Re-vet the modified PR + all downstream PRs to confirm no new issues.

## Step 11: Set up the new pipeline to ship the map

Once the mmd reads right, replicate the nf-core/rnaseq tooling pattern in
the pipeline repo (`assets/metro_map.mmd`, `docs/dev/metro_map.md`, the
docs/images outputs, the pip install line). That's a separate skill —
see the `pipeline-metro-setup` skill in this repo for the template and
the bridge pattern (pinning to a named branch of your nf-metro fork while
your fix chain is in flight).
