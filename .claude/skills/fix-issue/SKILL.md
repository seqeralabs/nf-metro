---
name: fix-issue
description: End-to-end workflow for fixing GitHub issues on the nf-metro repo with diagnostic rigor. Use when the user references a GitHub issue (by number, URL, or description) and wants it fixed. Handles worktree setup, environment creation, diagnostic-first investigation, invariant-test-first implementation, runtime validators, /simplify pass, full-repo lint, visual review via render preview, narrow-the-fix iteration on regressions, additive-only PR hygiene (no force-push, no narrative comments), origin verification after every push, and PR creation. Trigger on phrases like "fix issue #N", "address #N", "work on issue N", or any request to fix a bug or implement a feature that references an issue. For shepherding a chain of already-existing PRs back to main, see `pr-chain-vet` instead.
---

# Fix Issue

Structured workflow for fixing nf-metro GitHub issues in an isolated worktree.
Emphasises diagnostic-first investigation, invariant tests before code, and
additive-only PR hygiene so a fix never silently regresses the gallery.

**Conventions** (substitute if your setup differs):
- Local nf-metro checkout: `~/projects/nf-metro`
- Issues + PRs target the canonical upstream `pinin4fjords/nf-metro`. If
  you're working from a fork, resolve the owner with
  `gh repo view --json owner -q .owner.login`.
- micromamba: `/opt/homebrew/bin/micromamba` (macOS Apple Silicon codesign
  workaround). On other platforms, just `micromamba` if it's on PATH.

## Step 1: Understand the Issue

```bash
gh issue view <N> --repo pinin4fjords/nf-metro
```

Summarize the problem and proposed approach. Wait for user confirmation before proceeding.

## Step 2: Worktree + Environment Setup

```bash
# Worktree
cd ~/projects/nf-metro
git fetch origin main
git worktree add /tmp/nf-metro-fix-<N> -b fix/<N>-<slug> origin/main

# Fix environment
ulimit -n 1000000 && export CONDA_OVERRIDE_OSX=15.0 && /opt/homebrew/bin/micromamba create -n nf-metro-fix-<N> python=3.11 cairo -y
source ~/.local/bin/mm-activate nf-metro-fix-<N>
pip install -e "/tmp/nf-metro-fix-<N>[docs]"
```

All subsequent work happens inside `/tmp/nf-metro-fix-<N>`.

## Step 3: Diagnostic Before Fix

**Do not propose fixes from hypotheses.** Reproduce the symptom in numbers
before writing any code:

1. Render the affected example(s) on the current `main` (the before-state).
2. Inspect the rendered SVG: read the actual coordinates / element
   attributes that are wrong. Print them, log them, eyeball them.
3. Restate the bug as "element X has property P=<observed>, expected
   P=<target>" - a concrete numeric or structural claim. If you can't state
   the bug this way, you don't understand it yet; keep digging.

Only after the symptom is pinned down to specific numbers should you reason
about which layout pass / function produced them.

### Diagnostic tooling

The repo bundles two scripts that do exactly this render-and-read-the-numbers
work, usable for **any** layout issue regardless of how it was reported:

```bash
# Validator/crash/guard verdict: parse -> layout -> validate -> route, with
# findings split into authoring mistakes vs engine bugs.
python .claude/skills/nf-metro-stress-render/scripts/probe_layout.py <file.mmd> --json
# Per-section station coordinates, flagging stations off their section trunk,
# off-track in/outputs far from their consumer, and oversized inter-row gaps.
python .claude/skills/nf-metro-stress-render/scripts/inspect_layout.py <file.mmd>
```

Plus `nf-metro explain <file.mmd>` (the rule behind each inferred layout
decision) and `nf-metro info --json` (the structural model). These are
conveniences, not requirements - any way you pin the bug to numbers is fine.

If the issue happens to have been filed by the `nf-metro-stress-render` skill,
it carries a correct-by-construction repro `.mmd` in a `<details>` fold in the
issue body - start from that rather than re-deriving one. Most issues won't have
this; in that case build the reproducer yourself as usual.

## Step 4: Write the Invariant Test FIRST

### First, check for an existing regression lock

Most issues arrive bare and you write the failing test yourself (skip to the
numbered steps below). But some - notably those filed by the
`nf-metro-stress-render` skill - arrive with their regression infra **already
in place**: a fixture in `examples/topologies/`, a `GALLERY_ENTRIES` row in
`scripts/build_gallery.py`, and a `strict=True` xfail test referencing the issue
number. Grep before you write anything:

```bash
grep -rn "#<N>" tests/ scripts/build_gallery.py examples/topologies/
```

- **If a strict-xfail lock exists**, that *is* your failing test - don't write a
  duplicate, and don't re-add the fixture or gallery entry. Confirm it xfails on
  the current tree (it documents the live defect).
- **Completing the fix flips that strict-xfail to XPASS, which reds CI** - that
  is the signal the bug is actually fixed. Finish by **removing the `xfail`
  marker** so the now-passing assertion becomes a permanent positive guard.
  (Deleting the whole test loses the guard; leaving the marker keeps CI red.)
- **If no lock exists** (the common case), proceed with the steps below.

Before any production code change:

1. Write a test that encodes the invariant the bug violates (e.g. "no two
   stations share a grid cell", "trunk centre is symmetric about the fan
   midpoint"). Place it under `tests/`, ideally extending the layout
   invariants suite.
2. **Parametrise the test over multiple fixtures**, not a single `.mmd`.
   The existing `test_layout_invariants.py` historically over-relies on
   `da_pipeline.mmd`; new invariants should be exercised against several
   gallery fixtures so they generalise.
3. Run the test and **verify it fails on `main`**. If it passes, the test
   doesn't actually encode the bug - rewrite it.
4. Now write the fix.
5. Re-run the test and verify it passes.

This guarantees the test is meaningful (it caught the bug) and the fix is
meaningful (the test now passes because of the fix, not coincidence).

## Step 5: Add a Runtime Validator

Where the invariant is about layout properties that could regress silently
(overlap, off-grid placement, asymmetry, etc.), also add a `_guard_*`
function and wire it into `compute_layout`'s validate block.

Validators must **fail loudly** - raise with a clear, contextual error
message. Silent warnings or `print()`s are not acceptable; they get
ignored. The runtime check protects future changes; the unit test pins the
current behaviour.

## Step 6: /simplify Pass

After the fix and tests are passing, invoke the `simplify` Skill on the
changed code. Apply its suggestions and commit as a **separate** commit:

```
refactor: tighten <area> after fix for #<N>
```

Keeping `fix:` and `refactor:` commits separate makes the fix itself easy
to review and easy to revert in isolation if regressions surface.

## Step 7: Whole-Repo Lint

CI lint scans the entire repository, not just `src/` and `tests/`.
Pre-existing mis-formats in scripts, docs config, etc. will trip CI on
your PR even though they predate your change.

```bash
source ~/.local/bin/mm-activate nf-metro-fix-<N> && cd /tmp/nf-metro-fix-<N> && ruff format . && ruff check .
```

Run from the repo root, no path restriction. Fix or commit any deltas
that appear (a separate `style: ruff format whole repo` commit is fine).
Then run the test suite:

```bash
pytest
```

## Step 8: Visual Review via Render Preview

### Primary method: CI render preview (authoritative)

Push the branch and create a PR. The CI workflow
(`.github/workflows/pr-renders.yml`) automatically renders all gallery
examples on both the PR branch and base, generates a before/after visual
diff page, and posts a sticky comment on the PR with the preview link:

```
https://pinin4fjords.github.io/nf-metro/_pr/<PR_NUMBER>/
```

### Render-preview verdict gating

The sticky comment ends in a verdict line. Gate the next step on it:

- **"No visual changes detected"** -> a clean result, but **not** a
  licence to merge. Report the verdict and wait for the user to say
  merge. There is no standing auto-merge authorisation.
- **"Ready for review"** (or any wording indicating visual deltas exist)
  -> **STOP**. Surface the deltas to the user with one short line per
  affected gallery example describing what changed (e.g.
  `da_pipeline.mmd: trunk shifted 12px right`).

In **all** cases, merging is the user's call, made per-PR:

- Never merge until the user explicitly asks for this PR to be merged.
- **Never** use `gh pr merge --admin` (or any other bypass of the
  repo's branch-protection / review-required policy) on your own
  initiative. If a normal merge is blocked because the repo requires
  review, that block is the policy working - stop and tell the user it
  needs their review or an explicit instruction to admin-merge. Do not
  cite "prior PRs were admin-merged" as authorisation; past instances
  are history, not standing consent.

### Optional: quick local render of a single file

For a fast sanity check of one specific `.mmd` file before pushing:

```bash
source ~/.local/bin/mm-activate nf-metro-fix-<N>
cd /tmp/nf-metro-fix-<N> && python -m nf_metro render <file.mmd> -o /tmp/<name>.svg
python -c "import cairosvg; cairosvg.svg2png(url='/tmp/<name>.svg', write_to='/tmp/<name>.png', scale=2)"
open /tmp/<name>.png
```

Useful for quick iteration but does not replace the full CI gallery
review.

### Optional: local before/after comparison

For a before/after sweep before pushing, use the `/render-topologies`
skill.

## Step 9: Narrow Over-Applying Fixes

If the render preview shows the fix changed **more than the targeted
example** unexpectedly, do not ship it as-is. For each affected example,
classify the visual delta as one of:

- **I** (improvement) - keep
- **N** (neutral) - keep
- **D** (detrimental) - must be narrowed

For each detrimental delta, find the **precondition** that distinguishes
the target case (where the fix helps) from the regressing case (where it
hurts). Gate the fix on that precondition (e.g. a topology predicate, a
config flag, a layout property test) so it only fires when applicable.
Re-render and re-verify the verdict before merging.

A fix that ships with even one unaddressed D-delta is not finished.

## Step 10: Commit, Push, Verify Origin

Open the PR:

```bash
cd /tmp/nf-metro-fix-<N>
gh pr create --repo pinin4fjords/nf-metro --base main --title "<title>" --body "$(cat <<'EOF'
## Summary
<bullets describing the aggregate diff against main, no narrative>

Fixes #<N>

## Test plan
- [ ] pytest passes (including new invariant test)
- [ ] ruff check + ruff format clean on whole repo
- [ ] Runtime validator added (if applicable)
- [ ] Visual review of [render preview](https://pinin4fjords.github.io/nf-metro/_pr/<PR_NUMBER>/)
- [ ] Render-preview verdict: <No visual changes | deltas classified I/N>

Generated with Claude Code
EOF
)"
```

After every `git push`, **verify origin HEAD matches local**:

```bash
gh pr view <PR_NUMBER> --json headRefOid -q .headRefOid
git rev-parse HEAD
```

The two must match. Past agents have lost commits to silent push
failures; do not skip this check.

### Additive only - no force-push, ever

The local pre-push hook blocks force-pushes for a reason. To undo
anything, use `git revert <hash>` and push the revert as a new commit.
Never rewrite shared history (no `--force`, no `--force-with-lease`, no
interactive rebase on a pushed branch). This applies even when "it would
be cleaner" - cleanliness is not worth the risk of an agent silently
dropping work.

### Narrative belongs in the PR description, not in comments

Do not post explanatory comments on the PR walking through what changed,
what was tried, or what was reverted. Edit the PR description instead:

```bash
gh pr edit <PR_NUMBER> --body-file /tmp/pr-body.md
```

The description should be a standalone summary of the current state of
the diff against main - not a chronology of how the PR got there.

If narrative comments already exist (yours or a prior agent's), sweep
them via the GraphQL `deleteIssueComment` mutation. **Keep** the CI
sticky render-preview comment.

## Step 11: Drive End-to-End

A fix-issue session is not done when `/simplify` returns control to the
parent, or when the local tests pass. It is done when:

1. Commits are pushed.
2. Origin HEAD verified against local.
3. CI is green on the final commit.
4. Render-preview verdict is captured and gated on per Step 8.
5. PR description is standalone (per Step 10).

Do not hand back to the user partway through this list saying "the
simplify pass is done" or "tests pass locally". Carry the work all the
way to a reviewable PR.

## Step 12: Post-Merge Cleanup

Once the PR merges, do cleanup operations **in this order** to avoid
GitHub auto-closing dependent PRs:

1. **Retarget any child PRs** based on this branch over to `main` (or
   the next-up base) **first**, via `gh pr edit <child> --base main`.
   GitHub auto-closes PRs whose base branch is deleted; closed PRs whose
   base ref no longer exists cannot be reopened without restoring the
   deleted branch.
2. Delete the **remote** branch: `git push origin --delete fix/<N>-<slug>`
   (or via the GitHub UI's auto-delete on merge).
3. Remove the local worktree: `git worktree remove /tmp/nf-metro-fix-<N>`.
4. Delete the local branch: `git branch -D fix/<N>-<slug>`.
5. Remove the conda env: `/opt/homebrew/bin/micromamba env remove -n nf-metro-fix-<N> -y`.

Offer this cleanup to the user; only run it after they agree.

For shepherding a whole stacked chain of PRs back into `main` (rather
than a single issue fix), see `pr-chain-vet`.
