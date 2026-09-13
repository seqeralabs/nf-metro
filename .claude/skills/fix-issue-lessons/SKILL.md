---
name: fix-issue-lessons
description: Distill process lessons from a just-finished fix-issue run into the fix-issue skill's own reference files, as a small standalone PR. ONLY use when explicitly asked - phrases like "distill lessons from this session", "bake this into fix-issue", "capture what we learned into the skill", "update fix-issue with today's lessons". Do not trigger on a general request to summarize or reflect on the session, or on any fix-issue run itself - it never runs as part of that workflow. It edits `.claude/skills/fix-issue/` and opens a PR; it does not touch anything else.
---

# fix-issue lessons

Turns "we hit something worth remembering" at the end of a fix-issue run into
a small, reviewed diff against `.claude/skills/fix-issue/`. Never runs on its
own initiative - only when asked, and only after a run has actually finished.

## Why this stays deliberately small

Every reference file here is read again by a future run, and the
coordinator-facing ones - `SKILL.md`, `coordinator.md`, `agent-types.md`,
`scope-discipline.md`, `merge-and-cleanup.md`, `autonomous-mode.md` - are the
ones squeezed hardest by the post-compaction re-attachment cap that
`fix-issue/SKILL.md` documents at its own top (5,000 tokens per skill, shared
25,000 across all loaded skills). A lessons pass that bloats the skill it's
trying to improve defeats itself. Treat every candidate as a cost you have to
justify, not a freebie, and default to leaving it out.

**"Nothing worth adding" is the expected outcome, not a failure of this
pass.** Most runs execute the existing process correctly and surface nothing
new - that's the process working, not a gap. Err toward that verdict: when a
candidate is arguable, drop it rather than keep it. If Steps 1-3 leave zero
survivors, say so plainly and stop - do not open a PR to justify having run,
and do not stretch a minor or one-off wrinkle into a rule just to have
something to show.

## Step 1: Collect candidates from the finished run, not in general

A candidate lesson is:

- a near-miss or wrong turn a worker, reviewer, or the coordinator actually
  hit in that run - not a hypothetical "we could also...";
- a shape that would recur on a *different* issue, not a one-off quirk of
  this issue's specific input or code path;
- something the run's own gates did **not** already catch cleanly. If a
  worker made a mistake and an independent reviewer caught it exactly as
  designed, that is evidence the skill is working, not a gap to document.

Work from what's already in context. If the run's process work (diagnosis,
review, writer handoffs) happened in background agents whose transcripts have
scrolled out of context, fork one read-only agent to re-read the
conversation's task notifications and return a candidate list - don't pull
that material back into the main conversation just to scan it once.

## Step 2: Check each candidate against the *current* skill, not memory

Fetch first - another session may have already fixed the same gap:

```bash
cd ~/projects/nf-metro && git fetch origin main -q
git show origin/main:.claude/skills/fix-issue/references/<file>.md
```

Grep the fetched content (and `SKILL.md`) for the candidate's keywords before
drafting anything. Drop any candidate that's already covered, even partially
- extend the existing sentence instead of writing a new one beside it.

## Step 3: Write the tightest version that survives

For each surviving candidate:

- Prefer extending an existing section over adding a new heading; prefer
  merging two related candidates into one paragraph over two separate ones.
- Write no more than the rule needs, not as much as would be thorough -
  every word here is context every future run pays for, whether or not it
  needed this particular lesson. Before shipping, re-read your own diff and
  cut anything restating a point your edit already made elsewhere.
- Prefer a worker-facing reference file (`diagnosis.md`, `tests-and-validators.md`,
  `writer-steps.md`, `environment.md`, `gate-ratchet.md`, `regression-locks.md`,
  `visual-review.md`) over a coordinator-facing one. A worker loads its file
  once per spawn and discards it; `coordinator.md`/`SKILL.md` content is
  re-read on every turn of every future run regardless of what that run needs.
  Only touch a coordinator file when the lesson is genuinely about
  coordination - worker routing, gating, authority - not about how a worker
  does its own job.
- State the rule and the failure mode it prevents in the fewest words a
  reader with no memory of this session needs to apply it correctly. Name the
  general shape of the incident, not this instance of it - no issue numbers,
  branch names, or agent IDs inside the rule itself, matching the existing
  style in these files (e.g. "the visual gate was unsound in both directions",
  not "in PR #1234 the visual gate...").
- **Budget: stop and cut once net additions pass roughly 40 lines, or you have
  more than 2-3 distinct lessons.** A run that surfaced more than that is a
  candidate for a dedicated, user-reviewed skill-improvement pass with the
  full list in front of them first - not a bigger auto-drafted PR.

## Step 4: Ship it as an ordinary reviewed change

**Zero surviving candidates means zero diff.** Report that plainly - which
candidates you considered and why each was dropped - and stop; there is no
PR to open. Do not proceed past this line unless at least one candidate
survived Steps 1-3.

1. Worktree off latest `origin/main`; branch
   `docs/fix-issue-skill-lessons-<N>` (the issue number the originating run
   worked, or a short date-based slug if there wasn't one).
2. Edit only files under `.claude/skills/fix-issue/`.
3. Run `prek run --files` on the changed files - these are docs edits, no
   test suite is relevant. Also run
   `python .claude/skills/fix-issue/scripts/check_skill.py` locally: it scans
   `fix-issue`'s own reference files, which is exactly what this skill edits,
   and CI enforces it. It fails on prose that reads as a worker assignment
   (`brief`, `spawn`, `assign` + a role noun) without a tier or "sole writer"
   nearby - phrase around that rather than finding out from a red CI run.
4. Commit, push, open a **non-draft** PR (no render preview applies to a
   docs-only change). PR body: one bullet per file changed, naming the
   incident class each edit addresses, and naming any candidate you dropped
   because a prior PR already covered it.
5. **Never merge it yourself.** Whether a candidate is genuinely
   generalizable or just this session's noise is exactly the judgment call
   that needs the user's eyes before it lands - hand back the PR link and
   stop there.

## What this is not

Not a step in the `fix-issue` workflow - it does not run as part of Step 12
or any other numbered step, and nothing in `coordinator.md` invokes it.
It is a separate, user-invoked pass over a run that has already finished,
producing its own PR rather than folding into the run's own PR.
