---
name: fix-issue
description: Coordinator-led workflow for fixing GitHub issues on nf-metro: diagnostic-first, invariant-test-first, delegated to tiered workers. Use when the user references a GitHub issue (by number, URL, or description) and wants it fixed. Handles autonomous / net-negative requests. Trigger on "fix issue #N", "address #N", "work on issue N", or any request to fix a bug or implement a feature that references an issue - or with no issue number at all ("fix something impactful"), in which case the skill picks one itself. Use this whenever the work starts from a filed issue, including layout and routing bugs. For a bad render you are looking at with no issue filed, see `nf-metro-layout-fix`; for shepherding a chain of existing PRs back to main, see `pr-chain-vet`.
---

# Fix Issue

Structured workflow for fixing nf-metro GitHub issues in an isolated worktree.
Emphasises diagnostic-first investigation, invariant tests before code, and
additive-only PR hygiene so a fix never silently regresses the gallery.

**Communication:** keep status updates terse and lead any explanation of a
mechanism or a render with one plain-English sentence before the code or
coordinates. Prefer a narrow table to a wide one. When asked to "explain
simply" or for "less words", cut - don't re-expand.

**Conventions** (substitute if your setup differs):
- Local nf-metro checkout: `~/projects/nf-metro`
- Issues + PRs target the canonical upstream `seqeralabs/nf-metro`. If
  you're working from a fork, resolve the owner with
  `gh repo view --json owner -q .owner.login`.
- micromamba: `/opt/homebrew/bin/micromamba` (macOS Apple Silicon codesign
  workaround). On other platforms, just `micromamba` if it's on PATH.

## Reference files: load only what you own

**The coordinator reads only the files marked `coord`.** The rest are
worker-facing: name the file in the brief and let the worker read it in its own
context, which is discarded at handoff. Reading a worker's reference "to check
its work" is what the gates are for.

| File | Owner | Load when |
| --- | --- | --- |
| [`coordinator.md`](references/coordinator.md) | **coord** | always: briefing the issue (or picking one), worktree setup, the push, origin check, pre-ready gate, cleanup |
| [`agent-types.md`](references/agent-types.md) | **coord** | choosing an agent type, or checking the model resolution order |
| [`scope-discipline.md`](references/scope-discipline.md) | **coord** | fallout appears and you are tempted to defer it |
| [`merge-and-cleanup.md`](references/merge-and-cleanup.md) | **coord** | the PR body, pushing, merging, cleanup |
| [`autonomous-mode.md`](references/autonomous-mode.md) | coord | the user signalled autonomous / net-negative work |
| [`worker-contract.md`](references/worker-contract.md) | worker | every brief names it |
| [`diagnosis.md`](references/diagnosis.md) | diagnostician | its brief names it |
| [`tests-and-validators.md`](references/tests-and-validators.md) | writer | its brief names it |
| [`writer-steps.md`](references/writer-steps.md) | writer | its brief names it |
| [`regression-locks.md`](references/regression-locks.md) | writer | the Step 4 grep found a lock, or an xfail is proposed |
| [`gate-ratchet.md`](references/gate-ratchet.md) | gate specialist | `layout/routing/` changed, or a fixture was added |
| [`environment.md`](references/environment.md) | any worker running commands | its brief names it |
| [`visual-review.md`](references/visual-review.md) | visual reviewer | its brief names it |

The owner split exists so the coordinator does not carry material it is forbidden
to act on, and so this file survives compaction. It is not a cost saving:
measured, the resident difference is pennies a session.

**After an auto-compaction, re-invoke this skill.** Claude Code re-attaches only
the first 5,000 tokens of each skill, sharing a 25,000-token budget across all of
them, so a long run can lose the back half of this file or drop it entirely once
other skills are invoked. The ordering below puts the procedure and the tier
contract first for that reason; do not move the rationale above them.

## The twelve steps

Each step's detail is in the reference named beside it. Do not skip a step
because its detail is not inline.

0. **Select an issue, only when the user gave none.** A LIGHT investigator
   ranks open issues with no open PR, worktree, or assignee already against
   them; user confirms before Step 1. [`coordinator.md`](references/coordinator.md)
1. **Understand the issue.** A LIGHT investigator reads it and returns problem
   statement, scope, unknowns, and a proposed diagnostic brief. The issue body
   stays in the worker. Wait for user confirmation unless autonomous work is
   pre-authorised. [`coordinator.md`](references/coordinator.md)
2. **Worktree and environment.** Coordinator claims the issue (assignee),
   creates the worktree off latest `origin/main`, records the base SHA,
   assigns one writer. [`coordinator.md`](references/coordinator.md)
3. **Diagnose before fixing.** No fix proposed from a hypothesis. The defect
   becomes a numeric claim (geometry) or named call sites (structural), or the
   verdict is "not a bug" held to the same standard. Then the mandatory
   post-diagnosis gate. [`diagnosis.md`](references/diagnosis.md)
4. **Write the failing invariant test first**, and confirm it fails on `main`
   before any production change.
   [`tests-and-validators.md`](references/tests-and-validators.md),
   [`regression-locks.md`](references/regression-locks.md)
5. **Add a runtime validator** where a layout property could regress silently.
   Conditional: skip it outright for a class (c) structural defect, and say so.
   [`tests-and-validators.md`](references/tests-and-validators.md)
6. **`/simplify` pass**, run by default; skipping needs all four triviality
   conditions and must be declared. Once the fix and tests pass, the
   coordinator spawns a fresh MID `fix-issue-simplifier` against the writer's
   candidate SHA - never brief the writer to invoke it itself, since the
   writer role carries no `Agent` tool and cannot spawn its own reviewer.
   [`writer-steps.md`](references/writer-steps.md)
7. **Lint and tests.** Writer runs the mutating commands and commits; a LIGHT
   verifier re-runs the fixed block against the frozen SHA. CI owns the full
   suite. [`writer-steps.md`](references/writer-steps.md),
   [`gate-ratchet.md`](references/gate-ratchet.md)
8. **Visual review via the CI render preview.** The coordinator's single push
   and draft-PR creation happens here. Any delta at all gets HIGH eyes on every
   changed render. [`visual-review.md`](references/visual-review.md)
9. **Narrow an over-applying fix.** Every D-delta gets a precondition that
   separates the helped case from the hurt one.
   [`visual-review.md`](references/visual-review.md)
10. **Accept the candidate, verify origin.** `HEAD` equals the accepted SHA, the
    tree is clean, and the pushed ref matches local. Query the ref, not the PR
    API, which lags. [`coordinator.md`](references/coordinator.md),
    [`merge-and-cleanup.md`](references/merge-and-cleanup.md)
11. **The pre-ready gate**, then `gh pr ready`. One HIGH reviewer covering code
    review and aggregate progress together.
    [`coordinator.md`](references/coordinator.md)
12. **Post-merge cleanup**, only with authority, children retargeted first.
    [`merge-and-cleanup.md`](references/merge-and-cleanup.md)

For shepherding a whole stacked chain of PRs back into `main` rather than a
single issue fix, see `pr-chain-vet`. To capture process lessons from a
finished run into this skill, only when the user explicitly asks, see the
fix-issue-lessons skill.

## Worker tiers, briefs, and gates

### Worker tiers are explicit, never inherited

**Every worker launch names its tier explicitly.** Decide it before spawning and
say so in one line. Omitting the parameter is not a decision, it is a default
nobody chose - and where it lands depends on the resolution order in
[`agent-types.md`](references/agent-types.md), including one environment variable
that overrides every tier in this table. Never leave it to that.

The tier is the contract, not the model name. On Claude Code that is
`haiku`/`sonnet`/`claude-opus-4-8`. HIGH is pinned to that exact snapshot rather
than the `opus` alias, so it does not silently follow Anthropic's newer Opus
releases.

**This skill is Claude Code specific and does not run elsewhere as written.** It
leans on agent definitions, `Explore`/`Plan`, `effort`, `SendMessage` resumption,
the `CLAUDE_CODE_SUBAGENT_MODEL` precedence and the post-compaction re-attachment
cap, none of which port. The doctrine here does port - the two levers, the tiers,
one writer with independent readers, the gates, diagnose-before-fix - but porting
it means re-implementing the enforcement for that harness (on Codex the tiers map
to `luna`/`terra`/`sol`), not reading these files as-is.

Tiers come from the table below and do not drift upward because a task felt hard.
A re-briefed role keeps the tier it was spawned at. If you find a worker running
on an inherited default, restart it on the intended tier rather than justifying
the one it got: that call belongs at spawn time or not at all.

| Step | Role | Agent type | Tier |
| --- | --- | --- | --- |
| 0 | issue selection (only when none given) | `fix-issue-investigator` | LIGHT |
| 1 | issue investigator | `fix-issue-investigator` | LIGHT |
| 3 | diagnosis | `fix-issue-diagnostician` | HIGH; MID when the issue already names its own single-site cause and the brief is confirm-or-refute |
| 3, 11 | the two review gates | `fix-issue-reviewer` | HIGH |
| 4-7 | sole writer | `fix-issue-writer` | HIGH when the diff changes geometry-affecting logic in `src/nf_metro/layout/` (including its `routing/` package) or `src/nf_metro/parser/`; MID for a class (c) structural change in those dirs that alters no geometry, or for anything outside them. Highest tier wins on a mixed diff |
| 6 | `/simplify` review | `fix-issue-simplifier` | MID |
| 7 | lint/test verification | `fix-issue-verifier` | LIGHT |
| 7 | routing gate classification | `fix-issue-gate-specialist` | MID |
| 8 | local render / before-after sweep | `fix-issue-renderer` | LIGHT |
| 8 | admin-merge gate | `fix-issue-merge-assessor` | HIGH - it gates shipping code CI has not verified |
| 8, 9 | visual judgment and D-delta narrowing | `fix-issue-visual-reviewer` | HIGH |

**Never use the `fork` subagent type.** A fork inherits the parent's entire
conversation and always runs on the parent's model - the `model` parameter is
ignored - so every tier rule here is void. Measured, fork spawns cost about 4x a comparable named
worker. Use the named role types.

**LIGHT is unvalidated.** No historical spawn in this repo ran on the LIGHT
model, so nothing shows those four roles can do their jobs there. Treat early
LIGHT spawns as an experiment: if one blocks or returns something wrong, re-route
it up and say so rather than trusting the table.

A LIGHT worker that returns "blocked, this needs judgment" is a correct
outcome, not a failure. Re-route it up a tier rather than pre-emptively
starting high.

`effort` is a second dial but fixed per definition, with no per-invocation
form: see [`agent-types.md`](references/agent-types.md).

Resolution order when tiers collide, including the `CLAUDE_CODE_SUBAGENT_MODEL`
override that beats everything in this skill:
[`agent-types.md`](references/agent-types.md).

### Prefer the named agent types

`.claude/agents/fix-issue-*.md` defines one type per role in the table above,
with its tier and tool set already set.

**Spawning by role name is enough.** Verified by test: omitting the `model`
parameter resolves to the definition's model, not the session's, so a named type
carries its tier whether or not anyone remembers to pass it. That is the whole
point of the definitions - the tier is structural rather than remembered.

Two cases still need the model passed explicitly. If you spawn a generic type
(`general-purpose`, `Explore`) there is no definition to fall back on, so an
omitted model means the session's. And if `CLAUDE_CODE_SUBAGENT_MODEL` is set it
overrides both; check it once at session start. Every definition carries an explicit `tools` allowlist: without one a subagent
inherits every tool the main conversation has, including all MCP servers and
`Agent`, which would let a worker spawn untiered children.

The read-only roles carry no `Edit` or `Write`, but all hold `Bash`: a backstop,
not a guarantee. The instruction is what enforces read-only.

Substituting `Explore`/`Plan` saves each spawn both CLAUDE.md files but discards
the role definition and blocks re-briefing, and is measured at about $1.65 a run:
a curiosity, not a lever.

### Briefing a worker

Fill the template in [`coordinator.md`](references/coordinator.md); do not
re-improvise the authority rules, the return schema or the verifier command block
per spawn, which spends coordinator output that is then re-read every later turn.
Check every handoff against the seven-item schema in
[`worker-contract.md`](references/worker-contract.md); evidence arrives as a path
plus the one figure carrying the verdict, never as pasted logs.

### Gates and writer discipline

Two mandatory review gates, one writer per worktree, readers always fresh and
independent: [`coordinator.md`](references/coordinator.md).

## Primary invariant: coordinate, delegate, verify independently

The coordinator owns user communication and authority, the ledger, worker
routing, integration gates, and final reporting. It **alone** pushes, creates or
edits issues and PRs, merges, retargets, and cleans up. It does **not** do
substantive diagnosis, implementation, domain assessment, visual judgment,
`/simplify`, gate interpretation, or code review.

Two separate levers, do not confuse them:

- **Delegate to protect coordinator context.** Bulk output - issue bodies, test
  logs, render analysis, file sweeps - goes to a worker even when the task is
  mechanically trivial: coordinator bytes are re-read every turn, a worker's are
  discarded at handoff. Delegating a cheap task is cheaper than absorbing it.
- **Choose the tier to protect cost.** Delegation decides *where* work runs; the
  tier decides what it costs. A mechanical worker on the top tier is the most
  expensive thing in this workflow.

The coordinator does not read `src/` at all: `routing/inter_section_handlers.py`,
`routing/invariants.py` and `phases/guards.py` - the three largest modules -
belong only in a worker's context. This is
hygiene, not a headline saving - measured, it is worth under 1% of a run.

It may run trivial deterministic assertions itself - `git rev-parse`, a hash or
OID comparison, `git status --porcelain`, an exit code, handoff-schema
completeness - since spawning for a few bytes buys nothing. It must not
substitute its own *substantive* review.

The ledger tracks: issue and authority state; worktree, branch, base, writer;
hypothesis and evidence links; assignments, tiers and verdicts; changed files and
commits; commands and outcomes; I/N/D classifications; fallout; blockers; CI/PR
state; next gate. Hold only the **live slice** in context - current gate, open
blockers, accepted SHA, active assignments - and append settled rows to a file
outside the worktree. Otherwise the ledger is the one item that grows every turn
and is re-read on all of them.

## Cost discipline

The measured levers, in order: keep bulk command output out of every context
(tool output is 74.5% of all resident-context cost), hand the writer off around
200 turns, name a tier on every spawn, never `fork`, and do not park a large
context across a CI wait. Full list with the figures behind each:
[`coordinator.md`](references/coordinator.md).

## Scope discipline

Resolve bounded fallout surfaced by diagnosis, implementation, `/simplify`, lint,
review, or CI **in the current run**. Filing and deferring is the exception, and
a different subsystem is not by itself a reason to defer. Full rules, including
when a sibling PR is the right home and what a legitimate blocker looks like:
[`scope-discipline.md`](references/scope-discipline.md).
