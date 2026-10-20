# Scope discipline: fix the fallout, don't defer it

Resolve bounded fallout surfaced by diagnosis, implementation, `/simplify`,
lint, review, or CI in the current run. Filing and deferring is the exception.

- A different subsystem, separate worktree, or unfamiliar code is not by itself
  a reason to defer.
- Route each fallout item through the same worker protocol and tier table as the
  primary fix. Run independent read-only tasks concurrently. Give any fallout
  writer its own worktree unless the primary writer has finished and the
  assignment is explicitly serialized in the primary worktree.
- Keep a coherent fallout fix in the primary PR; use a sibling PR when that is
  more reviewable. Only the coordinator creates or edits the sibling PR.
- Reroute bounded blocks. Return a structured blocker when completion requires
  missing authority, unavailable capability, external-state change, a material
  user decision, or a genuinely multi-session program. Do not disguise the
  blocker with an xfail or child issue.
- This is not licence for scope creep into features the user didn't ask
  about - it's about not walking away from problems the *current* work
  surfaced.

This applies equally to second findings, gate-coverage gaps, `/simplify`, and
review findings.

## The snowball checkpoint

Fixing fallout in-run is right while the fallout is bounded. The record shows
where it stops being bounded: a 20-line repro landed as 88 commits across 83
files, a 7-defect port became 18, and the user's words at the end were that
they had lost track of the session. So the coordinator checks, at every writer
handoff:

- the candidate diff exceeds 20 files or 1,500 lines under `src/`;
- the run is fixing a third distinct defect class (a class is one rule broken,
  not one fixture);
- a third fresh writer has been spawned on the candidate;
- the same guard or failure class has recurred three times in the writer loop.

Crossing any one of these is a stop, not a note in the ledger. Report to the
user: what is committed and green, what is open with its blocker, and the two
paths (continue; land the green core as this PR and stack the rest). In
autonomous mode, take the second path without asking: the user has said that
finished, green, reviewable work beats a complete plan for the whole frontier.
Fallout that does not land in this PR is still filed as an issue with an MRE
and a base branch, so it is not lost; filing is tracking, not deferral, and it
does not stall the PR.
