---
name: nf-metro-stress-render
description: Stress-test the nf-metro layout engine by composing a brand-new, deliberately complex pipeline metro map, rendering it, and hunting for layout bugs the way a real never-seen-before pipeline would surface them. Use this whenever the user wants to fuzz / stress / probe the layout engine, find new layout bugs, check robustness against novelty, "throw a weird pipeline at it", generate a novel test render, or run an ad-hoc bug-hunting pass. Each run invents ONE novel topology (braiding structural axes that have not been combined before), renders it, runs the programmatic layout validator, and PRESENTS the render for you to eyeball the aesthetic bugs a validator can't see. Every candidate defect - whether the validator flagged it or you reported it by eye - is CONFIRMED against the laid-out geometry (exact coordinates, off-trunk drags, off-track gaps, inter-row gaps) and understood at the mechanism level before anything is filed; reports are never filed on eyeball alone. Confirmed bugs are FILED as GitHub issues (after dedup + your confirmation) AND wired with regression infra - a fixture in examples/topologies/, a gallery render so CI's render-diff tracks it, and a strict-xfail test that reds CI when the bug is fixed - so the defect can't silently return. Trigger on "stress test the layout", "fuzz nf-metro", "find layout bugs", "try a novel/complex render", "probe the engine for robustness", "what breaks if we render something new", "confirm and file these layout bugs". This is the operational answer to issues #364 (mutation/fuzz testing) and #323 (fragility on new pipelines).
---

# nf-metro stress render

Every genuinely new pipeline we render tends to surface a layout bug, because
the engine is robust on shapes it has seen and brittle on novel combinations.
This skill simulates "a new pipeline arrives" on demand: it **composes one
novel, sufficiently-complex metro map**, renders it, catches the obvious
structural defects programmatically, and puts the render in front of you for the
aesthetic defects only a human eye catches. Every candidate - flagged or
eyeballed - is then confirmed against the laid-out geometry and understood at
the mechanism level before it's filed, and each confirmed bug ships with a
regression fixture + gallery render + guard test so it can't silently return.
Run it ad-hoc; over time, as confirmed finds become permanent guarded fixtures,
novelty stops being a risk.

The unit of work is **one rich render per run**, reviewed deeply - not a batch.

## Prerequisites

- Work from the nf-metro checkout (default `~/projects/nf-metro`). This is a
  read-mostly probing task; no worktree needed unless you end up promoting a
  fixture or the user asks you to also fix the bug.
- Activate the env in the same command chain: `source ~/.local/bin/mm-activate nf-metro && ...`
  (the env has nf-metro editable-installed plus cairosvg for PNG).
- `gh` for issue dedup + filing.

## The loop

### 1. Choose a novel combination

Read `references/failure_modes.md` (the axis menu + known-fragile list) and
`coverage_log.md` (at the skill root, next to this SKILL.md - what past runs
already drew). Pick **2-4 structural axes
that have not been braided together before**. The goal is a combination that is
new to both the `examples/topologies/` corpus and the coverage log - the seams
next to known cracks are where the next crack hides.

Do not reproduce an existing fixture. Coverage of an axis *alone* is not
novelty; novelty is a never-drawn *triple* (e.g. "off-track output feeding a
station inside a folded return row that also carries a bypass"). Decide the
combination explicitly and say it out loud to the user before authoring, so the
intent is on record.

To keep runs genuinely varied rather than gravitating to the same favourites,
let the choice be arbitrary: glance at the least-recently-exercised axes in the
log and force yourself toward those.

### 2. Author a correct-by-construction `.mmd`

Compose a single connected pipeline that braids the chosen axes. Give it a
believable bioinformatics surface (real-ish tool names, line names like
`dna`/`rna`/`qc`) so it reads like a pipeline, not a graph-theory toy - realism
is what makes the render's aesthetic problems legible.

**Correctness is non-negotiable** (see the authoring rules in
`references/failure_modes.md`): lines only stop at stations they consume, every
line id is declared, off-track nodes carry their directive, ports match the
crossing edges. **Prefer auto-layout** - omit `grid:`/`direction:` unless the
axis under test *is* manual placement. A bug found on inferred layout is worth
ten found on a hand-pinned one.

Aim for "sufficiently complex": roughly 4-7 sections, 3-5 lines, 20-40
stations, with at least one fan, one convergence, and one of the trickier axes
(fold / mixed ports / off-track / bypass). Enough moving parts that an
interaction can go wrong, not so many that you can't reason about the result.
Gauge complexity by the probe's **station** count, not its edge count - a
comma-separated line list explodes into one edge per line, so `edges` runs
several times higher than the authored arrow count and overstates size.

Save it to a fresh workspace so the `.mmd` *is* the reproducer:

```bash
WS="/tmp/nf_metro_stress/$(date +%Y%m%d-%H%M%S)-<short-slug>"
mkdir -p "$WS"
# write the composed mmd to "$WS/layout.mmd"
```

### 3. Probe

```bash
source ~/.local/bin/mm-activate nf-metro && \
python .claude/skills/nf-metro-stress-render/scripts/probe_layout.py \
    "$WS/layout.mmd" --png "$WS/layout.png" --json | tee "$WS/verdict.json"
```

`probe_layout.py` runs the same stages a real render does and sorts findings
into four buckets, weakest-to-strongest engine-bug signal: `parse_issues`
(authoring), `validator` warnings/errors, `layout_crash`, `guard_failure`. It
exits non-zero only on an engine-level ERROR. Read `scripts/probe_layout.py`'s
header for the full contract.

### 4. Triage the verdict

- **`parse_issues` (error) present** -> the `.mmd` is malformed (a rule-1..4
  violation). This is *your* mistake, not an engine bug. Fix the `.mmd` and
  re-probe. Do not file.
- **`layout_crash` or `guard_failure`** -> hard engine failure / invariant
  violation on well-formed input. Strongest obvious-bug signal.
- **`validator` ERROR** -> structural defect (overlap, station-as-elbow, kink,
  containment...). Obvious bug.
- **clean** (`engine_error=false`, maybe warnings) -> no obvious structural
  bug; go to the visual review in step 5. Warnings are eyeball-worthy but not
  auto-file material on their own.

Validator findings are *candidates*, not yet filed bugs - they go through the
confirm-and-understand pass in step 6 alongside whatever the user's eye catches.

### 5. Present the render for the user's eye

The validator is blind to the defects that matter most visually (ugly detours,
asymmetric fans, near-miss label grazes, scrambled bundle identity,
reading-direction confusion). See the "only your eye catches" list in
`references/failure_modes.md`. Do this every run, even when nothing structural
fired.

1. **Look at it yourself first.** Read the PNG (`$WS/layout.png`) into context
   and do a genuine visual pass - call out anything that looks off, with
   region/coordinates. Catching a candidate before the user is part of the value.
2. **Open it for the user**: `open "$WS/layout.png"`.
3. Tell them what you composed (the axis combination), what the probe found, and
   what *you* noticed by eye, then ask them to report any other visual bugs.

The user's reported bugs and your own + the validator's candidates all feed the
next step. They are reports, not yet confirmed findings.

### 6. Confirm and understand every candidate before filing

This is the heart of credible bug-filing, and it applies equally to a validator
warning, something you spotted, and a defect the **user reported by eye**. A
report like "section 2 content is pulled too low" or "that input floats too
high" is a *hypothesis*; never file it on the strength of the eyeball alone.
Turn each one into a quantified, mechanism-level finding first. A filed issue
that names exact coordinates and a likely cause is actionable; one that says
"looks wrong" wastes the maintainer's time and erodes trust in the skill.

For each candidate:

1. **Quantify it.** Run the bundled inspector to get the real geometry:

   ```bash
   source ~/.local/bin/mm-activate nf-metro && \
   python .claude/skills/nf-metro-stress-render/scripts/inspect_layout.py "$WS/layout.mmd"
   ```

   It prints every station's `(x, y)` per section, flags stations sitting **off
   their section trunk**, off-track inputs/outputs **far from their consumer**,
   and **oversized inter-row gaps** - which directly confirm reports like
   "pulled too low" (off-trunk by +Npx), "floats too high" (gap to consumer),
   or "section too far down" (inter-row gap vs siblings). Cross-reference the
   probe's `validator` warnings (e.g. `route_segment_crossing`,
   `almost_horizontal_edge`) for the routing-level reports.
2. **Zoom in.** Crop the affected region from the PNG (PIL/`cairosvg`; remember
   the PNG is `scale=2` vs the SVG/inspector coordinates) and read it, so the
   numbers and the picture agree.
3. **Understand the mechanism.** State *why* it happens in engine terms (which
   phase / which placement rule), not just *that* it looks wrong. The engine can
   explain itself: `nf-metro explain "$WS/layout.mmd"` reports the rule that
   fired for each inferred decision (section direction, port sides, fold/row
   layout) and each synthetic element (fan-out junctions, bypass-V stations) -
   use it to name the cause behind a confirmed symptom, and `nf-metro info`
   (`--json`) for the structural WHAT. Where it's cheap, **try to reduce** to a
   minimal `.mmd` that still trips it (re-probe to confirm). If a plausible
   minimal repro does **not** reproduce, that negative result is itself signal -
   it tells the maintainer the trigger is a combination, so record it.
4. If confirmation fails - the geometry is actually fine, or it's an authoring
   artefact - **say so and drop it.** A candidate that dissolves under
   inspection is the triage working, not a miss.

### 7. File each confirmed bug AND wire its regression infra

A bug that's filed but not guarded will silently come back. Every confirmed
finding gets **both** an issue and the test/gallery wiring that catches a
regression - this is the core of how "novelty presents less of a risk over
time" (and it directly advances #364). Treat the regression infra as part of
filing, not an optional follow-up.

This step edits the repo, so work in a **worktree** per the repo conventions
(`../nf-metro-stress-<slug>`), and batch all of a run's findings into one PR.

1. **Dedup**: `gh issue list --state open --search "<symptom keywords>"`, scan
   titles/labels (`bug`, `layout`, `routing`, `parser`), and check the
   known-fragile list in `references/failure_modes.md`. If it already exists,
   add a comment with your confirmed repro + coordinates instead of duplicating.
2. **Draft + confirm + file** the issue. Include: plain-English symptom; the
   quantified evidence from step 6 (a small coordinate table reads well); the
   probe verdict line if any; the structural class (braided axes); and the
   correct-by-construction repro `.mmd` in a `<details>` block so it's
   self-contained. Show the draft to the user and get a yes before
   `gh issue create`. Suggest `bug` + the subsystem label.
3. **Add the regression fixture.** Drop the (reduced if possible) repro into
   `examples/topologies/<descriptive_name>.mmd` and add a row to that dir's
   `README.md` structural-class table, citing the issue number. Fixtures there
   are auto-collected by `tests/test_topology_validation.py`
   (`TOPOLOGY_FILES = sorted(TOPOLOGIES_DIR.glob("*.mmd"))`) into the validator
   and several always-on parametrized checks.
4. **Add the render to the gallery** so CI's render-diff tracks it: append a
   `(stem, source_dir, description)` tuple to `GALLERY_ENTRIES` in
   `scripts/build_gallery.py`. The render then shows up on every PR preview, so
   any future change to this layout is visible.
5. **Lock the defect with a test** so a fix is noticed and a regression reds CI.
   Follow the repo's existing patterns (read `tests/test_topology_validation.py`):
   - If the defect is **validator-detectable**, add a targeted test asserting the
     specific check is clean, marked `@pytest.mark.xfail(strict=True, reason="...#NNN")`
     (mirror `TestVariantCallingDefects` / `_VARIANT_CALLING_XFAIL`). It xfails
     now; when the engine fix lands it flips to XPASS and reds CI, prompting
     removal of the marker and closing the issue.
   - If the defect is **aesthetic / not yet validator-detectable**, the gallery
     render is the guard (CI render-diff surfaces any change), and note on the
     issue that a new validator check would let it be locked harder - or add one
     if it's tractable.
   - **Gotcha**: a new fixture is also fed to the always-on parametrized checks
     (chain alignment, etc.). Run `pytest tests/test_topology_validation.py` after
     adding it; if it reds on a check *other* than the one you're locking, either
     reduce the fixture so it only exhibits the target defect, or add matching
     xfail locks. Don't let a new fixture red CI on an unrelated check.
6. Open the PR (additive; per repo PR hygiene). The render-diff preview lets the
   user see every new fixture render and confirm the issues in context.

### 8. Log the run

Append one row to `coverage_log.md` (at the skill root): the date, the axis
combination, the workspace path, and the outcome (clean / filed #N + fixture /
known-dup #N / dropped-on-inspection). This keeps successive runs pushing into
fresh territory and shows novelty risk shrinking as fixtures accumulate.

## Scope boundaries

- This skill **finds, confirms, files, and guards** (fixture + gallery + xfail);
  it does not *fix* the engine. When the user wants a filed bug fixed, hand off
  to the `nf-metro-layout-fix` skill (code-level fixes with invariant tests +
  gallery regression vetting), or `fix-issue` for the full issue->PR workflow.
  The strict-xfail this skill leaves behind is what tells the fixer they're done.
- A finding that turns out to be an authoring mistake is a *success of the
  triage*, not a failure of the run - it means the engine was right. Say so.
- If the user wants to author a faithful map for a *real* pipeline (not a
  synthetic stressor), that's `pipeline-metro-diagram`, not this.
