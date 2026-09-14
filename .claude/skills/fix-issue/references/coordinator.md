# Coordinator actions

The steps the coordinator executes itself. Everything else it briefs a worker to
do; do not read the worker references to "check their work" - that is what the
independent gates are for, and reading them puts worker-facing bytes in the
context that is re-read every turn.

## Step 0: Select an issue (only when none was given)

Run this before Step 1 whenever the user did not name an issue - "fix
something", "find the most impactful thing you can do", "pick an issue and fix
it" - or asked for a session's worth of autonomous work with no specific
target. Skip it entirely when an issue number, URL, or unambiguous description
was given; go straight to Step 1.

Assign the same LIGHT `fix-issue-investigator` used in Step 1, briefed to
gather facts, not to make the final call:

```bash
gh issue list --repo seqeralabs/nf-metro --state open \
  --json number,title,labels,comments,assignees,createdAt --limit 200
gh pr list --repo seqeralabs/nf-metro --state open --draft \
  --json number,title,headRefName,body
git worktree list
```

From that:

1. **Build the exclusion set.** An issue already has ongoing work if its
   number appears in an open (including draft) PR's `headRefName` (the
   `fix/<N>-*` / `codex/<N>-*` branch-naming convention) or body (`Fixes #N`,
   `Closes #N`, `Resolves #N`), in a local worktree's branch name from
   `git worktree list`, or if it already carries a non-empty `assignees` list
   - another session or human has claimed it. Exclude every match and say why
   for each.
2. **Rank what's left** by simple, stated heuristics: a `bug` label over an
   `enhancement`, a clear repro or render attached over a vague report,
   comment or reaction volume, and any explicit maintainer priority signal in
   the thread. Do not invent a scoring formula; state the reasoning per
   candidate in one line.
3. **Return a short list** (3-5 candidates) with the one-line rationale each,
   plus the exclusion list. This is a survey, not a verdict: the investigator
   proposes, it does not pick.

**The worktree check is local-only; assignees are not.** `git worktree list`
only sees worktrees on this machine, so a peer session on another machine is
invisible to it. Assignees are the cross-machine signal - exclude any issue
that already carries one. A race still exists between this survey and the
actual claim, which is why Step 2 re-checks immediately before claiming.

Present the shortlist to the user and wait for them to confirm the top
candidate or pick a different one, exactly as Step 1 waits for confirmation -
unless autonomous work is pre-authorised, in which case take the top-ranked
candidate, state which one and why in one line, and proceed. Once an issue
number is settled, continue at Step 1 as normal.

## Step 1: Understand the Issue

Assign a LIGHT read-only investigator to run

```bash
gh issue view <N> --repo seqeralabs/nf-metro
```

assess the issue against current repository and remote evidence, and return the
problem statement, initial scope, unknowns, and proposed diagnostic brief. The
issue body and any comment thread stay in the worker; only its compact result
reaches the coordinator, including current assignees plainly stated - if
someone else already has it, surface that to the user rather than silently
proceeding. The coordinator summarizes that to the user and waits for
confirmation before proceeding unless the user has pre-authorised autonomous
work; never infer merge or issue-edit authority from that permission.

### Issue hygiene

Every issue is run through *this skill* fresh in a later session, so the
**issue body must be standalone and self-contained**. Have the relevant worker
return concise body-ready facts when it learns a cause, repro, or constraint.
Only the coordinator edits the issue body, and only with authority. Do not
scatter facts across comments or retain superseded approaches. Route a
separable defect through "Scope discipline" rather than filing a child issue
and walking away. File only when it is a multi-session undertaking in its own
right, the user has authorised the write, and the new body stands alone.

## Step 2: Worktree + Environment Setup

```bash
# Worktree (always off latest origin/main, never stale local main)
cd ~/projects/nf-metro
git fetch origin main
git worktree add /tmp/nf-metro-fix-<N> -b fix/<N>-<slug> --no-track origin/main
```

`--no-track` matters: without it the branch takes `main` as upstream, and a bare
`git push` then fails with "the upstream branch does not match the name of your
current branch" and helpfully suggests `git push origin HEAD:main`. Push
explicitly instead:

```bash
git push -u origin fix/<N>-<slug>
```

All repository-changing work for the primary fix happens inside
`/tmp/nf-metro-fix-<N>`. The coordinator performs this deterministic setup,
records the exact base SHA, and assigns one writer. Read-only workers may
inspect only a frozen SHA or snapshot, not the live worktree while the writer
is active. Do not allow a second writer until the first hands off and the
coordinator serializes the next assignment.

### Claim the issue before writing

Immediately before creating the worktree, re-check
`gh issue view <N> --repo seqeralabs/nf-metro --json assignees`. Step 0 or
Step 1 may have checked this minutes ago; a peer session can have claimed it
since. If it now carries an assignee other than the current `gh` actor, stop
and tell the user rather than silently proceeding or overriding someone
else's claim.

Otherwise claim it:

```bash
gh issue edit <N> --repo seqeralabs/nf-metro --add-assignee @me
```

This is a bookkeeping mutation, not a body edit: it needs no separate
authorization beyond the Step 1 confirmation to work this issue, and is
distinct from the "Issue hygiene" gate above on editing issue content. It is
also the cross-machine signal Step 0's `git worktree list` check cannot give
- an assignee is visible to every peer session and every human, on any
machine, whether or not a worktree or PR exists yet.

If the repository rejects the assignee (403 - some repos restrict assignees
to collaborators), fall back to a claim comment so the signal still posts:

```bash
gh issue comment <N> --repo seqeralabs/nf-metro \
  --body "Claiming this issue via fix-issue, working on branch fix/<N>-<slug>."
```

If the run ends abandoned or blocked with no PR opened, remove the claim
(`gh issue edit <N> --repo seqeralabs/nf-metro --remove-assignee @me`) so the
issue does not look claimed forever. Once a PR exists, leave the assignee in
place - the merge or Step 12's cleanup resolves it.

Env, `PYTHONPATH`, and commit-hook mechanics:
[`environment.md`](environment.md). Reuse the one
long-lived `nf-metro-dev` env; never create one per issue.

## Step 10: accept candidate, verify origin

After the writer hands off candidate commit SHA(s) and independent gates pass,
the coordinator confirms `HEAD` equals the accepted SHA and the tree is clean.
Only the coordinator pushes, creates or edits the PR, and performs later remote
mutations. Step 8 has normally already pushed and opened the draft PR: **do not
re-push or re-create it**, edit the existing body (`gh pr edit`) and go straight
to the origin check. The body template is in
[merge-and-cleanup.md](merge-and-cleanup.md).

## Step 11: Drive End-to-End

Before declaring readiness, run the **pre-ready gate**: one fresh HIGH read-only
reviewer given the accepted candidate SHA, aggregate diff, issue, diagnostic
evidence, test artifacts, and visual verdict. It covers correctness, scope,
invariants, safety, unresolved fallout, *and* aggregate progress in a single
brief. Revise any later brief from its findings. The draft PR already exists
from Step 8; this step adds no push and no new PR. Only after the gate passes
may the coordinator run `gh pr ready <N>`.

This gate reads the diff; it does not need the CI matrix to have finished. Run
it as soon as the candidate SHA, its test evidence, and the render-diff/visual
verdict are in hand, in parallel with a still-running CI - gate only the
merge itself, not this review, on CI going green.

A successful fix-issue run is not done when `/simplify` or a test worker
returns. It reaches PR-ready completion when:

1. The fix lands in `src/`, not in a doctored reproducer (Step 3), and the
   "it's fixed" claim cites the render + numbers that prove it (Step 8).
2. Commits are pushed.
3. Origin HEAD verified against local.
4. CI is green on the final commit - including the full test matrix, which is
   CI's job, not a local run's; any failure interpretation came from an assigned
   verifier or domain specialist.
5. Render-preview verdict is captured and gated on per Step 8.
6. PR description is standalone.
7. Independent verification, visual review when applicable, and the pre-ready
   gate pass.
8. The coordinator marks the draft PR ready only after those gates pass.

Reroute bounded failures until these gates pass. If missing authority,
unavailable capability, external state, or a material user decision prevents
completion, return the structured blocker with the accepted candidate SHA and
remaining gate. Do not claim PR-ready completion.

## Step 12: Post-Merge Cleanup

Retarget child PRs first, then delete remote branch, worktree, local branch, in
that order, and only with user authority. Full procedure and the reconciliation
checks that gate it: [`merge-and-cleanup.md`](merge-and-cleanup.md).

For shepherding a whole stacked chain of PRs back into `main` rather than a
single issue fix, see `pr-chain-vet`.

## Review gates: two mandatory, the rest on trigger

Independent review is load-bearing, but a reviewer that re-reads raw evidence is
the most expensive spawn in the run. Two mandatory gates, both HIGH:

1. **Post-diagnosis gate.** One reviewer that challenges the domain
   classification (Step 3: authoring mistake, engine bug, input-independent
   structural defect, or no defect at all, and the claim behind whichever came
   back). A wrong classification wastes the whole run, so this gate pays for
   itself; a second separate challenger does not.
2. **Pre-ready gate.** One reviewer combining the Step 11 code review and the
   final aggregate-progress review. These ask nearly the same question against
   the same diff; run them as one brief.

Run an extra mid-loop aggregate review only on a trigger: two repeated blocks,
material scope growth, conflicting worker verdicts, a changed acceptance bar, or
multiple active worktrees. Send it the compact ledger and *links* to evidence,
not the evidence inline. Record every review verdict and revise later briefs,
scope, or gates from its findings.

## One writer, independent readers

Allow exactly one writer in each worktree. Give concurrent writers separate
worktrees and non-overlapping write scopes; otherwise serialize them. Keep
diagnostic, verifier, visual-review, and code-review roles read-only and
independent of the writer. Read-only workers never persist tracked, untracked,
or ignored worktree changes; place their logs, caches, and generated evidence
outside the worktree. Readers run concurrently only against a frozen commit SHA
or snapshot, never a live worktree during an active writer assignment. User
authority determines whether the coordinator acts; it never transfers that
ownership to a worker.

The writer is **one continuing worker up to a point**. Steps 6, 7 and 9 re-brief
the same agent with `SendMessage` to its agent ID, so it keeps the large layout
modules it already read rather than re-paying for them. A per-invocation `model`
still applies on resume. Readers, by contrast, are fresh each time so their
judgment stays independent.

**But hand off at roughly 200 turns.** Measured once, over historical runs of
this workflow on one machine (not reproducible from this tree): top-tier spawns
in the 150-300 band averaged about $26 while those past 300 averaged about $69,
fitting an exponent near 1.25. Retained context stops paying for itself once
every turn re-reads all of it. So when the writer approaches ~200 turns, have it
hand off a candidate SHA plus a short state note and start a fresh writer from
that SHA.

Two honest caveats. A handoff costs $1.50 to $3.00 - re-establishing context is
not free - so break-even sits near 180 turns, and 150 would lose money on a
writer that finishes at 160. And the superlinearity is partly a model-mix
artifact: mid-tier spawns are close to linear, so this bites the top-tier writer
specifically. Worth about $28 a run, the same order as tiering rather than
multiples of it.

The writer is the only party that can see its own turn count, so
[`worker-contract.md`](worker-contract.md) requires it to report turns on every
handoff and to offer the split itself.

A writer's session can also expire before its turn budget - resume can fail
outright well under 200 turns. Treat that as expected: brief a fresh sole
writer from the last candidate SHA rather than waiting on it.

Use one candidate sequence throughout: the sole writer makes local candidate
commit(s), runs mutation-capable hooks or generators, and hands off the exact
SHA. Independent read-only workers verify and review that SHA without changing
it. If fixes are required, serialize them back to the writer and verify the new
SHA.

## Worker brief template

Fill this in. Do not restate the authority rules, the return schema, or the
verifier command block in the brief: they live in
[`references/worker-contract.md`](worker-contract.md) and the worker
reads them there. Restating them per spawn spends coordinator output that then
gets re-read on every later turn.

```
ROLE / TIER: <role> at <LIGHT|MID|HIGH>
OBJECTIVE:   <one sentence>
AUTHORITY:   read-only | sole writer in <worktree>
SCOPE:       <paths this worker may read or write>
INPUTS:      <frozen SHA, artifact dir, issue number>
ACCEPTANCE:  <the concrete bar for this task>
STOP IF:     <escalation conditions specific to this task>
DECIDE:      <options this worker must surface rather than pick, if any>
CONTRACT:    follow .claude/skills/fix-issue/references/worker-contract.md
```

Check every handoff against the six items: scope, files+SHA, commands and
outcomes, evidence, risks/blockers, verdict. Evidence arrives **as a path plus
the one figure that carries the verdict** - never pasted coordinate dumps,
render analysis, or test logs. A blocked handoff satisfying the schema is a
valid outcome: re-brief from its evidence, route a different tier, or escalate
to the user when authority or a material product decision is missing. Never
demand an unbounded worker loop or treat diagnosis as implementation. The
contract file carries the full wording, including what a worker does when the
work exceeds its briefed tier.

## Cost discipline (applies throughout)

Layout iteration is where sessions burn tokens and compute. Keep it tight:

- **Name a tier on every spawn** (above). Worth ~3-6% of spend, and most of the
  per-spawn gap between tiers is *turns*, not price per token, so the table takes
  some credit that belongs to task selection.
- **Lean on CI for the full suite.** Locally, run targeted tests: the new
  invariant test, the affected module, `--lf`, `-q --no-header -x`, Python 3.11
  for the routing/TB ratchets. CI runs the complete matrix on push and that is
  the authoritative full-suite signal. Do not run a local full suite per branch
  or per worker. Reserve it for the three cases in Step 7.
- **Reuse the persistent env.** Do not `micromamba create` per issue - it
  re-solves the whole dependency set every session for nothing. See
  [`references/environment.md`](environment.md).
- **Read coordinates, not pixels, for non-visual questions.** `inspect_layout.py`
  and `probe_layout.py` print geometry as cheap text; a render, rasterise and
  image-into-context cycle only earns its cost for a genuine visual check.
- **Do not park a large context across a CI wait.** Full re-encodes of a ~476k
  context are ~10% of spend in the one-off measurement. Finish the session at the
  push and start fresh for the CI verdict, or compact before waiting; collapsing
  the ledger trims kilobytes off half a million and is not the fix. Honest limit:
  only about a third of those re-encodes follow an idle gap, so this reaches
  roughly $10 of the ~$28 a session. When you do watch, watch once in the
  background rather than re-running `gh pr checks` each turn.
- **Lean on the CI render-diff for regression review; don't rebuild the gallery
  locally in a loop.** The CI preview (Step 8) is the authoritative whole-corpus
  diff. A local `build_gallery` / render-diff sweep repeated many times just
  duplicates it. Local rendering is for a *single* file's quick sanity check.
- **Keep bulk command output out of every context.** Measured, tool output is
  **74.5% of all resident-context cost** across 36k worker Bash calls - far more
  than file reads. So brief every worker: pipe to `tail`, `grep -c` or `wc -l`,
  redirect full output to the artifact directory, and cite the path. This is the
  largest cost lever in the workflow. Two specific cases the skill already
  names - never re-run `gh pr checks` each turn, never paste test logs - are
  instances of it, not the whole rule.
- **Push policy is governance, not economy**, and it lives with the push:
  [`merge-and-cleanup.md`](merge-and-cleanup.md).
