---
name: pr-chain-vet
description: Per-PR vetting workflow for shepherding a stacked PR chain on nf-metro back into main - one PR at a time, gallery-diff against main, additive cleanups, no force-push, ready for human review. Use whenever the user is working through a chain of layout/refactor PRs on nf-metro and asks to "vet this PR before review", "walk through the chain", "check PR N for gallery regressions", "prepare PR for review", "do a vetting pass on PR N", "make this PR mergeable", or is otherwise driving stacked PRs toward main and needs each one cleaned up and CI-greened in turn. Covers worktree setup, reverting known-rejected commits, conflict reconciliation against main, the /simplify pass as a separate commit, render-and-diff against main, classifying every visual delta (improvement / neutral / detrimental), appending fix commits for detrimentals, sweeping narrative PR comments, rewriting PR descriptions to be standalone, triggering CI on the final commit, and the post-merge cleanup order (re-target children BEFORE deleting the merged branch). Assumes the chain itself was set up correctly (orphan-style stacked PRs with chained base branches); for the nf-metro code fixes the chain carries, see `nf-metro-layout-fix`; for upstream mmd authoring, see `pipeline-metro-diagram`; for pipeline-repo setup, see `pipeline-metro-setup`.
---

# Per-PR vetting on an nf-metro stacked chain

Captures the per-PR vetting workflow for driving a stacked chain of layout
fix / refactor PRs back into `nf-metro` `main`. The chain itself
(orphan-style stacked PRs, each based on the previous, with the bottom-most
based on `main`) is assumed to already exist - see the
`nf-metro-layout-fix` skill for the upstream concerns (savepoint pattern,
invariant tests, conditional-gating fixes) that produced the chain in the
first place.

This skill is about taking **one PR from that chain** and making it
mergeable to `main`: reconcile against main, simplify, prove via gallery
diff that the net effect on existing pipelines is acceptable, sweep
narrative artefacts, get CI green, and hand off to a human reviewer.

## When to use this skill

Trigger when the user is shepherding a stacked PR chain into `main` and is
working through them one at a time. Phrases like:

- "vet this PR before review"
- "walk through the chain, PR by PR"
- "check PR #N for gallery regressions"
- "prepare PR #N for review"
- "do a vetting pass on this one"
- "make this PR mergeable to main"

Not the right skill if:

- The user is still authoring the underlying fixes (use
  `nf-metro-layout-fix` for code fixes; `pipeline-metro-diagram` for mmd
  authoring; `pipeline-metro-setup` for pipeline-repo wiring).
- The chain doesn't exist yet (this skill assumes the stacked PRs and their
  base-branch graph are already in place).
- The user just wants to run the gallery regression harness in isolation
  (use `render-topologies` for that).

## Cross-cutting rules

These hold across every step:

- **No force-pushes, ever.** Every change to a PR is an additive commit.
  To undo something already in the branch, append a `git revert <hash>` -
  don't rewrite history. Rewrites silently destroy other people's local
  state and break GitHub review threads.
- **No narrative comments on the PR.** If something needs to be said about
  the change, it lives in the PR description. A reviewer landing on the PR
  cold should understand the change from the description alone.
- **Every PR gets a `/simplify` pass.** As a separate commit. The original
  fix stays readable; the simplify pass stays auditable.
- **CI must run on the latest commit before declaring the PR ready.** If
  the last commit was `[skip ci]`, append an empty commit to trigger CI.

## Project-specific extension points

Some judgement calls are project-specific. Keep them in memory entries the
skill consults rather than burying them here:

- **Known-rejected commits.** Some commits in a chain may be intentionally
  not wanted in `main` (e.g. an experimental label-stagger commit the user
  decided to abandon). Capture these in a memory entry like
  `project-stagger-commit-rejected.md` so the skill knows to revert them on
  sight.
- **Detrimental-delta criteria.** The list of things that make a visual
  delta "detrimental" (Step 8) is extensible. Pipeline-specific layout
  expectations can be added to a memory entry the skill reads before
  classifying renders.

## Step 1: Set up an isolated worktree

Always work from a fresh worktree on the PR branch. Editing in the main
checkout risks clobbering parallel sessions.

```bash
# Pick the PR branch and pull
gh pr view <N> --repo <owner>/nf-metro --json headRefName -q .headRefName
# -> <branch>

# Worktree alongside the main checkout
git worktree add /tmp/nf-metro-pr<N> <branch>
cd /tmp/nf-metro-pr<N>

# Editable install + baseline test pass
pip install -e .
pytest -x -q
```

A failing baseline pytest is a stop signal - resolve before touching the
PR. Otherwise later regressions are impossible to disentangle from
pre-existing breakage.

## Step 2: Strip known-rejected commits

Check the branch's commits against the project's known-rejected list (see
the project-specific memory entry, e.g. `project-stagger-commit-rejected.md`).

```bash
git log --oneline origin/main..HEAD
```

If any rejected commit appears in the ancestry, revert it - don't rewrite:

```bash
git revert <hash> --no-edit
git push
```

Reverts keep history honest. A reviewer can see that the commit was
considered and rejected, rather than wondering why the diff disagrees with
the PR description.

## Step 3: Reconcile conflicts vs main

If the PR shows as CONFLICTING on GitHub, merge `origin/main` into the
branch. Most conflicts on a stacked chain are test-file additions where
both sides added new tests - resolve by keeping both sides.

```bash
git fetch origin main
git merge origin/main
# resolve conflicts (usually `git checkout --ours/--theirs` or hand-merge)
git commit
git push
```

A merge commit is fine and preferred over a rebase. Rebases on a pushed
branch require force-push, which is forbidden by the rules above.

## Step 4: Run /simplify on the net diff

Invoke the `/simplify` slash command on the PR's net changes (i.e. the
diff vs the PR's base branch, not vs main). Commit the result as a
separate refactor commit:

```bash
git commit -am "refactor: tighten <area>"
git push
```

The simplify pass is a separate commit on purpose: the original fix stays
readable to reviewers focused on the bug, and the simplify pass stays
auditable for reviewers focused on style/structure. Don't fold them
together.

If the simplify pass produces no meaningful changes, that's a valid
outcome - skip the commit and move on.

## Step 5: Render the gallery on PR HEAD

`scripts/build_gallery.py` writes SVGs to `docs/assets/renders/`. Copy
them to a stable per-PR temp directory so the diff in Step 7 is
reproducible across iterations:

```bash
python scripts/build_gallery.py --debug
mkdir -p /tmp/gallery-pr<N>
cp docs/assets/renders/*.svg /tmp/gallery-pr<N>/
```

The `--debug` flag draws grid lines and bbox boundaries — they make most
layout regressions visible at a glance.

## Step 6: Render the gallery on main (if not cached)

```bash
# In a separate worktree on main
git -C /tmp/nf-metro-main fetch origin main
git -C /tmp/nf-metro-main checkout origin/main
cd /tmp/nf-metro-main && python scripts/build_gallery.py --debug
mkdir -p /tmp/gallery-main
cp /tmp/nf-metro-main/docs/assets/renders/*.svg /tmp/gallery-main/
```

If you already rendered main during a previous PR's vetting pass and `main`
hasn't moved, reuse the existing output - this is the slowest step in the
loop.

## Step 7: Diff and open the report

`build_render_diff.py` takes an output **directory** and writes
`index.html` inside it:

```bash
python scripts/build_render_diff.py \
  /tmp/gallery-main \
  /tmp/gallery-pr<N> \
  /tmp/diff-pr<N> \
  --pr <N>
open /tmp/diff-pr<N>/index.html
```

The diff report renders side-by-side SVGs and flags pixel-level changes.
Use it as the entry point for Step 8; don't try to classify deltas from
filenames alone.

## Step 8: Classify every changed example

For each example with a visible delta, classify it. The skill's job is to
**catch detrimentals before review**, not to litigate every pixel.

- **Improvement** - cleaner than main: less overlap, straighter routing,
  better bbox fit, fewer crossings, better label placement.
- **Neutral** - byte-only or visually indistinguishable. Usually
  coordinate-noise from refactors that don't change geometry.
- **Detrimental** - any of these:
  - Broken trunk alignment (kinks at section boundaries, trunk Y drift)
  - Station or icon overlap
  - Lines crossing non-consumer station markers ("breeze-past")
  - Mad routing (sharp doglegs, S-curves, lines wandering across the
    diagram)
  - Bbox overflow (content escaping its section's bounding box)
  - Label collisions (labels overlapping each other or stations)
  - Bypass routing through a station that doesn't consume the line
  - Asymmetric fans where they used to be symmetric
  - Anything the project's detrimental-delta criteria memory entry adds

Don't blanket-pass everything. Don't blanket-fail everything either - a
diff where 30 examples shifted by 1px and one example is cleaner is
exactly the kind of net win the chain is supposed to produce. The bar is:
**no example got worse**.

## Step 9: Fix detrimentals, re-render, re-diff

For each detrimental:

1. Append a fix commit on the PR branch (an additional commit, not a
   rewrite). The fix usually belongs in the same area of nf-metro code
   the PR is already touching - if it doesn't, that's a signal the chain
   structure is wrong and you should pause and discuss with the user
   before continuing.

2. Push.

3. Re-run Steps 5 and 7 (Step 6 is unchanged - main hasn't moved). Confirm:
   - The originally-flagged detrimental is now resolved (improvement or
     neutral).
   - No **new** detrimentals appeared in other examples. Fixes commonly
     produce regressions elsewhere; the gallery is what catches them.

Loop until every changed example is improvement or neutral.

## Step 10: Sweep narrative comments off the PR

GitHub review comments accumulate as a chain evolves. By the time a PR is
ready for human review, none of that history is useful - it just makes the
PR look messy and gives a reviewer the wrong context. Sweep them.

Keep the CI render-preview comment (the bot-posted "Render preview" one).
Delete everything else.

```bash
# List comment IDs that aren't the render-preview bot post
gh pr view <N> --repo <owner>/nf-metro --comments --json comments | \
  jq -r '.comments[] | select(.body | startswith("**Render preview**") | not) | .id'

# For each id, delete via GraphQL
gh api graphql -f query='mutation { deleteIssueComment(input:{id:"<ID>"}) { clientMutationId } }'
```

If the user has explicitly asked to preserve a particular comment (e.g. a
reviewer's outstanding question), keep that one too. Otherwise the rule is:
description carries the narrative, comments don't.

## Step 11: Rewrite the PR description to be standalone

The PR description should describe **the net diff vs `main`** as it
currently stands. Strip:

- References to the chain or savepoint
- "Will need matching PR X" notes that no longer apply
- Intermediate / superseded approaches
- The history of how the PR got to its current state

A reviewer landing on the PR cold should understand what it does, not how
it got here. Update the description with `gh pr edit`:

```bash
cat > /tmp/body.md <<'EOF'
## Summary
<1-3 bullets describing the net change>

## Why
<1-2 sentences on the user-visible problem this solves>

## Testing
<which invariant tests cover it, which gallery examples confirm it>
EOF

gh pr edit <N> --repo <owner>/nf-metro --body-file /tmp/body.md
```

## Step 12: Trigger CI if needed

CI must have run on the latest commit. If the last commit was a `[skip ci]`
(e.g. a doc-only fix) or no preview build appears for the current head,
append an empty commit:

```bash
git commit --allow-empty -m "chore: trigger CI"
git push
```

The empty commit is the lowest-friction way to re-fire CI without
rewriting history.

## Step 13: Wait for CI green, hand off to human review

Watch the checks:

```bash
gh pr checks <N> --repo <owner>/nf-metro --watch
```

When all checks are green, the PR is ready. Surface it to the user with a
short note: PR URL, what the description now says, which gallery examples
were affected and how. Then stop - human review is the gate.

## Step 14: After merge - in this exact order

Order matters here. GitHub will auto-close child PRs if the parent branch
is deleted before their bases are re-targeted.

1. **Re-target every child PR** whose base is the just-merged branch:

   ```bash
   gh pr edit <child-N> --repo <owner>/nf-metro --base main
   ```

   Do this for every direct child. (Grandchildren stay pointed at their
   parent; they'll get re-targeted as their parent merges in turn.)

2. **Delete the remote branch:**

   ```bash
   gh api -X DELETE repos/<owner>/nf-metro/git/refs/heads/<branch>
   ```

3. **Remove the local worktree:**

   ```bash
   git worktree remove /tmp/nf-metro-pr<N> --force
   ```

4. **Delete the local branch:**

   ```bash
   git branch -D <branch>
   ```

If you delete the remote branch before re-targeting children, GitHub
treats them as merged-into-deleted and closes them. Recovering from this
is annoying (you have to reopen each one and re-set its base) and risks
losing review threads.

## What "done" looks like

A vetted PR has:

- Clean ancestry: no known-rejected commits, no merge-conflict markers.
- A `/simplify` pass landed as a separate commit (or skipped if no-op).
- A gallery diff vs `main` where every changed example is improvement or
  neutral.
- Zero narrative comments outside the CI render-preview bot post.
- A standalone description that reads correctly without chain context.
- A green CI run on the latest commit.

At that point, hand off to human review and move to the next PR in the
chain.
