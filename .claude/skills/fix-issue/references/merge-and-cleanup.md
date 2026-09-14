# Push hygiene, merge authority, cleanup

## When CI runs

- **Default `[skip ci]` on work-in-progress pushes** (WIP snapshots, refactor
  passes). Let CI run on the final pre-review push - which this repo needs
  anyway, because the render-diff *is* the visual review. (A commit that fixes a
  known CI failure must re-run CI: no `[skip ci]` on those.)
- **One CI-triggering push per round.** A push runs the full test matrix *and*
  renders the whole gallery twice, on the PR branch and the base. It is the most
  expensive action in this workflow. Batch accepted fixes into one push instead
  of pushing each one as it lands.

A `[skip ci]` push is not free. It leaves the render preview built from an older
tree, so the Step 8 provenance check fails and the fallback is to enumerate the
corpus and render both sides locally. Use it for genuine throwaway snapshots;
never on the commit you intend to review.

If checks don't appear on a push meant to trigger CI within a couple of
minutes, check that commit's message for a stray skip marker before assuming
propagation delay - a commit whose prose even names or quotes the marker text
can trip it (Actions scans the whole message), and a writer explaining an
earlier skipped commit can reproduce this by accident. Recover with a new
(never amended) commit that omits the marker. This applies even when several
prior commits in the same push legitimately carried the marker as WIP
snapshots - only the pushed HEAD commit's message needs to be clean, so close
the round with one more real commit (a genuine small fix or doc clarification,
never a no-op) rather than amending an earlier one.

A fail-fast test matrix cancels every sibling shard the instant one fails - a
cancelled shard carries no signal, it is not a pass. After any push, re-run
each cancelled job individually (or the whole run's `--failed` set) before
treating the round as settled, and expect to repeat this every round a
mid-round failure occurs, not just once per PR.

Read this at the push/merge/cleanup end of a run.

## Additive only - no force-push, ever

Force pushes are blocked by a Claude Code `PreToolUse` matcher on `Bash`
(`~/.claude/hooks/block-force-push.sh`, wired in `~/.claude/settings.json`), not
by a git hook - there is no `pre-push` hook in this repo. It denies `--force` and
`--force-with-lease` alike, and it only sees Bash tool calls, so it is a
backstop, not a boundary. To undo anything, use
`git revert <hash>` and push the revert as a new commit. Never rewrite shared
history (no `--force`, no `--force-with-lease`, no interactive rebase on a pushed
branch). This applies even when "it would be cleaner" - cleanliness is not worth
the risk of silently dropping work.

An ordinary additive (fast-forward) push is **not** blocked by that hook - only
rewrites are. Don't mistake an unrelated push failure for a force-push block.

## Narrative belongs in the PR description, not in comments

Do not post explanatory comments on the PR walking through what changed, what was
tried, or what was reverted. Edit the PR description instead:

```bash
gh pr edit <PR_NUMBER> --body-file /tmp/pr-body.md
```

The description should be a standalone summary of the current state of the diff
against main - not a chronology of how the PR got there. When a PR goes
through multiple rounds of D-delta narrowing, regenerate the summary against
the full base..HEAD diff each time, not incrementally against just the latest
round - otherwise earlier, already-reviewed deltas quietly drop out of the
record and a fresh reviewer correctly flags the body as incomplete.

If narrative comments already exist, the coordinator may sweep them via the
GraphQL `deleteIssueComment` mutation only with issue/PR edit authority. **Keep**
the CI sticky render-preview comment.

## When the user *does* authorise a merge

Never merge without explicit per-PR user authority. Prior admin merges are not
standing consent. A clean render-diff verdict is not consent either.

- **"Merge"** authorises one normal merge-commit attempt:
  `gh pr merge <N> --merge`. Never squash. If review, branch protection, or
  up-to-date policy blocks it, stop and return that blocker. Do not escalate to
  `--admin`.
- **"Admin merge"** explicitly authorises `gh pr merge <N> --admin --merge`. If
  CI is not green, first assign a fresh `fix-issue-merge-assessor` (HIGH, read-only), which
  carries the safelist inline and no tool that can merge and determine whether the sole unverified delta is
  CI-irrelevant. The coordinator may run the admin merge only with both explicit
  user admin-merge authority and that worker's pass verdict. Otherwise return the
  blocker. Do not update the branch or start fresh CI merely to satisfy
  up-to-date policy; cancel irrelevant in-flight runs only as part of the
  authorised, accepted eco-merge sequence.

## Post-merge cleanup

Once the PR merges, only the coordinator performs cleanup, and only with user
authority. Before deleting anything, require a clean tree, reconcile local
`HEAD`, pushed remote head, and merged PR head, and confirm no unpushed commits.
Stop on any mismatch. Then use this order:

1. **Retarget any child PRs** based on this branch over to `main` (or the next-up
   base) **first**, via `gh pr edit <child> --base main`. Confirm every retarget
   and stop if any fails; branch deletion can auto-close a dependent PR.
2. Delete the **remote** branch: `git push origin --delete fix/<N>-<slug>` (or via
   the GitHub UI's auto-delete on merge).
3. Remove the local worktree: `git worktree remove /tmp/nf-metro-fix-<N>`.
4. Delete the reconciled local branch with `git branch -d fix/<N>-<slug>`. Use
   `-D` only after explicit user authority and proof that `-d` rejects a fully
   reconciled branch for a harmless bookkeeping reason.

Leave the shared `nf-metro-dev` env in place - it is reused across issues, so
there is nothing per-issue to remove.

Offer this cleanup to the user; only run it after they agree.

## Draft PR body template

Used once, at Step 8/10. The preview link needs the PR number, which does not
exist until the PR does: create the body with the placeholder, then fill it in
with `gh pr edit` at Step 10.

```bash
cd /tmp/nf-metro-fix-<N>
gh pr create --draft --repo seqeralabs/nf-metro --base main --title "<title>" --body "$(cat <<'EOF'
## Summary
<bullets describing the aggregate diff against main, no narrative>

Fixes #<N>

## Test plan
- [ ] Targeted tests pass locally (including new invariant test); CI matrix runs the full suite
- [ ] ruff check + ruff format clean on whole repo
- [ ] Runtime validator added (if applicable)
- [ ] Visual review of [render preview](https://seqeralabs.github.io/nf-metro/_pr/<PR_NUMBER>/)
- [ ] Render-preview verdict: <No visual changes | deltas classified I/N>
EOF
)"
```

After every `git push`, **verify origin HEAD matches local**:

```bash
test "$(git ls-remote origin <branch> | cut -f1)" = "$(git rev-parse HEAD)" \
  || { echo "ORIGIN MISMATCH: the push did not land"; exit 1; }
echo "ORIGIN OK"
```

The two must match. Prior sessions have lost commits to silent push failures;
do not skip this check. Query the ref, not `gh pr view --json headRefOid`: the
PR API lags a push by seconds and will report the previous SHA, which reads as
a lost commit when nothing is wrong. If they genuinely differ, re-query the ref
once before treating it as a failure.
`merge-and-cleanup.md`.
