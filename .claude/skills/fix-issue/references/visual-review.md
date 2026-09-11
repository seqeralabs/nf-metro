# Visual review and narrowing

Step 8's detail and Step 9 in full.

### Primary method: CI render preview (authoritative)

**One CI-triggering push per candidate round, and one draft-PR creation in the
run.** Additional pushes are permitted only for findings CI surfaced that could
not be reproduced locally, which in practice means the routing ratchets when the
local run was not on Python 3.11. Anything you could have caught locally does not
earn a second push. The coordinator performs it; no worker performs these remote
mutations.
Steps 10 and 11 do not repeat it - Step 10 verifies the push landed and edits
the PR body, Step 11 only flips the draft to ready. Every push costs a full CI
matrix plus a gallery render of both branches, so batch accepted fixes into one
push rather than pushing per fix.

The one exception is a failure that **cannot** be seen locally. The routing
ratchets skip off Python 3.11, so they surface only in CI; if the diff touches
`src/nf_metro/layout/routing/`, require the verifier to run under 3.11 so the
failure lands before the push rather than after it. When CI still reveals a
gate failure the local run could not, fixing it earns a second push - that is
not a violation of the rule, it is the rule's stated exception. The CI workflow
(`.github/workflows/pr-renders.yml`) automatically renders all gallery
examples on both the PR branch and base, generates a before/after visual
diff page, and posts a sticky comment on the PR with the preview link:

```
https://seqeralabs.github.io/nf-metro/_pr/<PR_NUMBER>/
```

### Getting the renders in front of the reviewer

Two scripts do this. They are scripts, not blocks to paste, because the logic is
a chain that must share state: as separate Bash calls, variables do not survive
between steps and `set -e` does not fire under this harness. Both carry
`--self-test`, and `check_skill.py` runs it.

```bash
# Paths are relative to the worktree root; cd there first or use an absolute path.
cd /tmp/nf-metro-fix-<N>
source ~/.local/bin/mm-activate nf-metro-dev
.claude/skills/fix-issue/scripts/visual_preview.sh --pr <N> --branch <HEAD_BRANCH> --candidate <CANDIDATE_SHA>
```

Three outcomes, all of them verdicts:

- `VERDICT: no visual changes` - **this is the answer for a clean run**, and no
  page is published in that case. Report it and stop.
- a non-zero exit naming CI failure, a preview still publishing, or a stale page
  whose run marker does not match this run. None of these are "wait and hope";
  read the message.
- `STEMS: <n>` with `$ART/stems.txt` written. Then render both sides:

```bash
.claude/skills/fix-issue/scripts/render_pairs.sh --base <BASE_SHA> --candidate <CANDIDATE_SHA>
```

Source paths come from `corpus_map.py` beside these scripts, which reads `gallery.yaml` and
mirrors `build_gallery.py`'s group semantics: an output name is not always the
entry id (pipelines carry a `pipeline_` prefix, nextflow conversions declare an
explicit `output`), so never resolve a stem by searching for a matching basename.

`Read` the `base-<stem>.png` / `cand-<stem>.png` pairs it reports. Never `Read`
the preview page itself: it is one multi-megabyte inlined `index.html`, and its
SVG carries `var()` and `light-dark()` that resolve only in a live browser.

Every stem owes an I/N/D verdict. Reconcile against the script's summary line:

- `RENDER-FAILED(cand)` where base rendered **is the regression**, the worst kind
  of D-delta. Never skip it.
- failures on both sides pre-date this PR; still name them, and diff the two
  `.err` files, because the same fixture aborting for a new reason is a finding.
- `UNRESOLVED` is a gallery id with no matching `.mmd`. Name it; do not let it
  vanish.

If the preview cannot be trusted, `render_pairs.sh` falls back to enumerating the
whole corpus from `scripts/gallery.yaml` on its own. That is a few hundred renders
on both sides, so it is a real cost: prefer fixing the preview.

### Render-preview verdict gating

Read the sticky comment's verdict line first, then size the review to it. A
LIGHT worker may report a literal "No visual changes detected" verdict, since
there is nothing to judge. **The moment any delta exists, a fresh HIGH read-only
visual reviewer inspects every changed example**, classifies deltas I/N/D,
identifies uncertainty, and returns an acceptance verdict. Do not seed it with
the writer's preferred interpretation, and do not downgrade this because the
issue predicted no visual change - a delta nobody expected is the most important
kind. Every changed render gets HIGH eyes.

The sticky comment ends in a verdict line. Gate the next step on it:

- **"no visual changes detected"** (lowercase, as emitted) -> a clean result, but **not** a
  licence to merge. Report the verdict and wait for the user to say
  merge. There is no standing auto-merge authorisation.
- **"Ready for review"** (or any wording indicating visual deltas exist)
  -> gate on the independent visual verdict. Re-brief the writer for every D;
  surface accepted I/N deltas and unresolved uncertainty to the user with one
  short evidence-based line per affected gallery example.

Merge authority, push hygiene, and cleanup:
[`merge-and-cleanup.md`](merge-and-cleanup.md). Leave
branch deletion to Step 12 so dependent PRs can be retargeted safely.

### State the evidence for every "it's fixed" claim

Never assert a fix works without an independent verifier naming what proved it.
Every "resolved" / "this is fixed" / "renders correctly" claim must cite the
**specific render and the concrete numbers** it was checked against - the file,
and the coordinate or element that moved from the observed value to the target
value you wrote down in Step 3. "I believe it's resolved" with no named render
is not a verdict; it invites the reply "which render did you re-assess on?".

Two traps this closes:

- **"Didn't abort" / "the one invariant passes" is not "renders
  correctly".** Removing an abort can merely expose a poor layout the abort
  was masking. After any layout/routing fix, the LIGHT verifier runs `probe_layout` and
  `inspect_layout` on the *candidate* SHA and hands over the numbers; the HIGH
  visual reviewer inspects the full render (cropping as needed) and makes the
  judgment. Do not ask a LIGHT worker to assess a render - the after-state counterpart to
  Step 3's before-state reading, not a repeat of it - for the whole-layout picture (crossings, port alignment,
  column gaps), not only the targeted invariant.
- **A clean render-diff verdict only covers the gallery corpus.** It says
  nothing about a NEW fixture that isn't in the gallery yet. Add new regression
  fixtures to `scripts/gallery.yaml` (`GALLERY_ENTRIES` in
  `scripts/build_gallery.py` is derived from it), not only `examples/topologies/`, so CI's render-diff makes them visible to a
  human. A topologies-only or tests-only fixture is invisible in the PR
  preview.

Do not present a prototype as an improvement before independent review and the
user's judgment where needed. If the render has problems, revise the writer's
brief; do not defend a weak fix.

### Local renders

This is about **review**, not about the writer looking at its own draft. A
writer iterating on an in-progress change renders as often as it needs to inside
its own loop; it is not independent of its own work, so a spawn there buys
nothing. For review, a single-file sanity render or a local before/after sweep
belongs in a LIGHT read-only worker that returns the verdict and artifact path
rather than the imagery. Commands:
[`environment.md`](environment.md).

**Pick one, never both for the same SHA.** The local sweep computes the same
before/after corpus diff that CI already computes, so running it alongside the
CI preview is paying twice for one answer. Use it only when you need that answer
*before* spending a CI cycle; once the preview exists, the preview is the
review.

## Step 9: narrow over-applying fixes

If the render preview shows the fix changed **more than the targeted
example** unexpectedly, do not ship it as-is. Have the independent visual
reviewer classify each affected example as:

- **I** (improvement) - keep
- **N** (neutral) - keep
- **D** (detrimental) - must be narrowed

The bar is "no **meaningful** visual regression", not pixel-identity. A
subtle spacing or coordinate shift that comes with a cleaner, more elegant
implementation is fine (classify it N or I); do not contort the code to
preserve a byte-identical render. Only a genuine degradation is a D.

For each detrimental delta, assign a HIGH diagnostic worker to find the
**precondition** that distinguishes the target case (where the fix helps) from
the regressing case (where it hurts). Re-brief the sole writer to gate the fix
on that precondition (e.g. a topology predicate, a config flag, a layout
property test). Assign fresh re-rendering and re-verification before merging.

Each D-delta round costs a HIGH diagnostician, a writer re-brief, a fresh render
and another CI cycle - roughly $25-35. After two rounds stop and report rather
than looping: a fix needing a third narrowing is usually the wrong shape, and
whether to keep paying is the user's call.

A fix with an unaddressed D-delta is not PR-ready. Reroute it or return the
structured blocker that prevents correction or classification.
