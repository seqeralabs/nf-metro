---
name: pipeline-metro-diagram
description: Author the mmd content for a pipeline's metro map - turn the pipeline's workflow code into a faithful, readable metro diagram. Use whenever the user is iterating on a pipeline's .mmd to improve fidelity to the workflow code, visual clarity, or layout params. Trigger on phrases like "make a metro map for pipeline X", "iterate on the mmd for pipeline Z", "the metro map for funcscan doesn't match the workflow", or "this station shouldn't be on the rnaseq line". Covers studying the workflow to model lines/stations/branches, deciding line and section structure, `%%metro file:` and `%%metro off_track:` directives, the render-inspect-edit iteration loop, common pitfalls (breeze-past, fan asymmetry, section-boundary kinks), and the "is it mmd or nf-metro?" triage. When the diagnosis lands on nf-metro itself producing a bad layout for a correct mmd, hand off to the `nf-metro-layout-fix` skill. For the mechanical pipeline-repo setup (assets/, docs/dev/metro_map.md, render commands, README swap), see `pipeline-metro-setup`. For nf-metro's own gallery regression testing, see `render-topologies`.
---

# Authoring a Pipeline Metro Map

Captures the workflow for designing the mmd content for a Nextflow pipeline
metro map: turning the pipeline's workflow code into a faithful, readable
metro diagram. Out of scope: pipeline-repo mechanical setup (file paths,
render commands, README) — see the `pipeline-metro-setup` skill. Out of
scope too: code-level fixes to nf-metro itself when a correct mmd still
renders badly — see the `nf-metro-layout-fix` skill.

## Scope and relation to other skills

This skill is about **upstream authoring** — turning a pipeline's workflow code
into a metro map that lives in the pipeline repo. It is distinct from:

- **pipeline-metro-setup**: the mechanical pipeline-repo wiring (assets/,
  docs/dev/metro_map.md, render commands, CHANGELOG). Hand off there once the
  diagram reads right.
- **nf-metro-layout-fix**: code-level changes to nf-metro itself when the mmd
  is correct but the engine produces a bad layout. Reach for that path only
  after you've ruled out mmd mistakes (see Step 5 below).
- **render-topologies** (in this repo): regression-tests nf-metro itself by
  rendering all gallery fixtures and pixel-diffing against `origin/main`. Use
  that one when you've changed nf-metro's layout/render code, not when you're
  drafting a pipeline diagram.

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

### Render iteratively while drafting

Render the mmd often while editing. Use `--debug` on early iterations to see
grid lines and bbox boundaries — this exposes most layout problems instantly.

Param choice for iteration is its own topic — see the `pipeline-metro-setup`
skill for the canonical nf-core param baselines and when to override them.
For pure mmd authoring, any consistent param set works.

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
nf-metro bug. Hand off to the `nf-metro-layout-fix` skill, which captures
the savepoint pattern, the invariant-test-then-fix-then-runtime-validator
loop, gallery vetting against `origin/main`, and the additive-commits-only
PR chain rules for nf-metro changes.

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

## Quick reference: the differentialabundance mmd

A worked-example mmd is in the differentialabundance pipeline repo. It
exercises:

- 4 study-type lines (rnaseq, affy, maxquant, geo) with distinct colours
- 5 sections with explicit `%%metro grid:` placement
- A 2-row rowspan on the data-import section to balance against two
  half-height sections
- File-input stations with `%%metro file:` and `%%metro off_track:` for
  optional inputs

Useful as a template for a new pipeline with multiple study-type branches.

## Once the diagram reads right

Hand off to the `pipeline-metro-setup` skill for wiring the rendered map
into the pipeline repo (file layout, render commands, README swap,
CHANGELOG entry, install-line pinning while a fix chain is in flight).
