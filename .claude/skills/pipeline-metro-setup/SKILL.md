---
name: pipeline-metro-setup
description: Wire an nf-metro diagram into an nf-core (or any Nextflow) pipeline repo - the mechanical pipeline-side setup, not the authoring of the .mmd itself. Use whenever the user wants to add nf-metro tooling to a pipeline (assets/metro_map.mmd, docs/dev/metro_map.md, the rendered SVG/PNG/animated assets, README image swap, CHANGELOG entry), replace an existing workflow.png/svg with a metro map, mirror the nf-core/rnaseq setup on a new pipeline, or pin nf-metro to a named branch of a fork while waiting for upstream fixes to land. Trigger on phrases like "set up nf-metro on pipeline X", "wire the metro map into the repo", "replace workflow.png with a metro map", "ship the metro diagram in pipeline Y", "pin nf-metro to my fork branch while we wait for the fixes", "what assets do I need in docs/images for the metro map".
---

# Pipeline metro map setup

Wires an nf-metro diagram into a pipeline repository: the layout of source +
rendered assets, the regeneration commands developers run, the README swap,
the CHANGELOG entry, and the install-line pinning pattern when nf-metro main
isn't yet good enough to render the pipeline cleanly.

## Scope and relation to other skills

This skill is about the **pipeline repo plumbing**. It assumes the `.mmd`
already exists (or is being authored alongside) and focuses on shipping the
result.

- **`pipeline-metro-diagram`** (separate skill, nf-metro repo) authors the
  `.mmd` content - which lines and stations to draw, how to model branches,
  how to iterate on fidelity to the pipeline. Reach for that one when the
  question is "what does the diagram show?".
- **`nf-metro-layout-fix`** (separate skill, nf-metro repo) covers code-level
  fixes to nf-metro itself when the mmd is correct but the engine produces a
  bad layout. Stage 2 Case B (named-branch pin on a fork) is the bridge
  developers use while a fix chain over there is in flight.
- **`render-topologies`** (in this repo) is nf-metro's own gallery regression
  harness. Unrelated to pipeline-repo work.

If the user's request is "make a metro map for pipeline X end-to-end", both
skills apply: `pipeline-metro-diagram` for the mmd, this one for the wiring.

## The canonical layout: mirror nf-core/rnaseq

`nf-core/rnaseq` is the reference. New pipelines should match its file layout
so a maintainer moving between pipelines finds the same paths each time. For
a pipeline named `<name>` (e.g. `differentialabundance`, `rnaseq`, `funcscan`),
ship:

```
assets/
  metro_map.mmd                              # the source
docs/
  dev/
    metro_map.md                             # regeneration instructions
  images/
    nf-core-<name>_metro_map.png             # static raster, 2265 px wide
    nf-core-<name>_metro_map.svg             # static vector
    nf-core-<name>_metro_map_animated.svg    # animated, embedded in README
    nf-core-<name>_logo_light.png            # the pipeline logo (already there)
```

If the repo is community/Nextflow but not nf-core, drop the `nf-core-` prefix
but keep the suffixes. Consistency of the suffixes (`_metro_map`,
`_metro_map_animated`) matters more than the prefix.

## When the user runs the workflow

This skill captures four stages. Walk the user through whichever they need:

1. Pick render params (default to rnaseq's, override only with reason).
2. Decide whether to pin nf-metro to a fork branch or use a released version.
3. Write the dev doc and run the three render commands.
4. Swap the README image and remove the old `workflow.{png,svg}`.

## Stage 1: Render parameters

Visual parameters (theme, logo, output PNG width) stay aligned across
pipelines so the diagrams read as part of a family:

- `--theme light`
- `--logo docs/images/nf-core-<name>_logo_light.png`
- PNG output width `2265`

Layout parameters can diverge per pipeline if the topology demands it. Two
common baselines:

- **rnaseq defaults** - `--x-spacing 60 --y-spacing 40 --no-straight-diamonds`.
  Use this when the diagram is mostly linear with a few short fan-outs.
- **multi-branch / fan-heavy** - `--x-spacing 70 --y-spacing 55
  --no-straight-diamonds --line-order definition --center-ports`. Use this
  when the pipeline has 3+ study-type lines, multiple fan-outs from the same
  station, or sections that crowd at the smaller spacing.

Pick the baseline by rendering both and looking. If the rnaseq defaults
produce a clean diagram, prefer them - matching rnaseq is more valuable than
shaving a few millimetres off the layout.

`--line-order definition` keeps lines stacked in the order they appear in the
`%%metro line:` directives, which usually reads more naturally than the
default heuristic. `--center-ports` reduces kinks at section boundaries.

## Stage 2: Pin nf-metro, or use a release

There's a real choice here. Two cases:

### Case A: released nf-metro is enough

If the latest released nf-metro renders the pipeline cleanly, use a version
pin in the dev doc:

```bash
pip install 'nf-metro>=X.Y.Z' cairosvg
```

This is the simpler path and the long-term steady state. Once your fix chain
has merged upstream, every pipeline should converge here.

### Case B: pin to a named branch of your fork

If the pipeline needs nf-metro layout fixes that aren't released yet (the
common case while a fix chain is in flight against `pinin4fjords/nf-metro`):

1. In nf-metro, push the savepoint state of your fix chain to a named branch
   on your fork. Use the pipeline name as the branch name so the pin reads
   self-documenting:

   ```bash
   # In your nf-metro checkout
   git push origin <savepoint-tag-or-sha>:refs/heads/<pipeline-name>
   ```

   `<pipeline-name>` should match the pipeline's nf-core slug (e.g.
   `differentialabundance`, `funcscan`). One branch per pipeline, kept
   updated as the fix chain evolves.

2. Pin the install line in `docs/dev/metro_map.md`:

   ```bash
   pip install 'git+https://github.com/<owner>/nf-metro.git@<pipeline-name>' cairosvg
   ```

3. Once the fix chain merges to nf-metro `main` and a release is cut, swap
   back to the version pin (`pip install 'nf-metro>=X.Y.Z' cairosvg`) and
   delete the named branch on your fork. The pipeline diagram should
   reproduce identically from the released version - if it doesn't, the fix
   chain didn't fully land.

The pinning pattern is intentionally explicit. A floating ref like
`@main` makes pipeline renders non-reproducible. The named branch can be
moved forward deliberately as new fixes land, and each move is a discrete
event a maintainer can audit.

## Stage 3: Write the dev doc and render

Copy `references/dev-doc-template.md` into `docs/dev/metro_map.md` and fill
in:

- `<name>` - the pipeline name (e.g. `rnaseq`, `differentialabundance`)
- `<install-line>` - either the version pin or the branch pin from Stage 2
- `<x-spacing>`, `<y-spacing>` - from Stage 1
- `<extra-layout-flags>` - the rest of the layout flags from Stage 1, or
  empty if using rnaseq defaults

The template has three render blocks: static SVG, PNG conversion via
cairosvg, and animated SVG. Plus a trailing-newline normalisation step
because nf-core pre-commit hooks reject SVGs without a final newline.

Run the commands from the pipeline repo root, in order. The static SVG
must exist before the PNG conversion. The animated SVG is independent and
can run last.

## Stage 4: README swap and cleanup

Most nf-core pipelines historically embed a static `docs/images/workflow.png`
(or `.svg`) in `README.md`. Replace this with the animated metro map:

```html
<img src="docs/images/nf-core-<name>_metro_map_animated.svg" alt="..." width="100%">
```

The animated SVG works in the GitHub README renderer and is small enough not
to blow up the repo size. Keep the `width="100%"` to let the SVG scale.

Then:

- Delete `docs/images/workflow.png` and `docs/images/workflow.svg` if they
  exist. Don't leave them dangling - they confuse future maintainers and
  cost zero to remove.
- Update any other docs pages (`docs/usage.md`, `docs/output.md`) that
  referenced the old workflow image - usually a one-line `<img src>` swap.
- Add a CHANGELOG entry under `### Changed`:

  ```markdown
  - Replaced static workflow diagram with nf-metro-rendered metro map.
  ```

The CHANGELOG entry is the only narrative artifact users see. Keep it one
line - they'll see the new diagram themselves.

## Verifying the result

After running the three render commands:

1. Open the static SVG in a browser and confirm the layout reads. If a
   line passes through a station that doesn't consume it, that's an
   authoring bug - hand back to `pipeline-metro-diagram` to fix the mmd.
2. Open the animated SVG and confirm the line animations sweep along the
   intended paths (not backwards or in chunks).
3. Verify the PNG was produced at 2265 px wide (`identify`,
   `file`, or just opening it - the resolution shows in the title bar of
   most image viewers).
4. Confirm trailing newlines: `tail -c1 docs/images/*.svg | xxd` should
   show `0a` for each file. The template's `sed -i '' -e '$a\'` step
   normalises this on macOS; on Linux, drop the empty string argument
   (`sed -i -e '$a\'`). Mention the platform difference when handing the
   template over.

## Reference: worked examples

Two pipelines ship complete nf-metro setups to use as references:

- **`nf-core/rnaseq`** - canonical setup, rnaseq-default render params,
  released nf-metro version pin. Match this if your pipeline fits the
  linear / short-fan baseline.
- **`nf-core/differentialabundance`** - same file layout, savepoint render
  params (`--x-spacing 70 --y-spacing 55 --center-ports --line-order
  definition`), pinned to a fork branch while a fix chain is in flight
  against nf-metro `main`. Match this if your pipeline has multi-branch /
  fan-heavy topology.

Both ship the same five files (`assets/metro_map.mmd`,
`docs/dev/metro_map.md`, three images in `docs/images/`). When in doubt,
diff your pipeline's setup against these.
