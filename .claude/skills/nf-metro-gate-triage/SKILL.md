---
name: nf-metro-gate-triage
description: Run a routing gate-arm triage slice on nf-metro - give every un-exercised branch arm in a layout/routing module a verdict (reachable -> author a fixture, defensive, candidate-dead, or reachable-but-defective -> file a bug). Use when the user wants to triage routing gate arms, close coverage-matrix gaps, work a #687 slice, classify un-exercised gates, run or resume the gate-coverage triage campaign, or regenerate the routing gate coverage matrix. Trigger on phrases like "triage the <module> gate arms", "close the gate coverage gaps", "work the #687 triage", "classify the un-exercised routing arms", "run a gate-arm triage slice". The full methodology, lane rules, and gotchas live in docs/dev/routing_gate_triage.md; this skill drives one module slice end-to-end on top of the fix-issue workflow. For finding new layout bugs by composing novel renders, see nf-metro-stress-render; for fixing an engine bug the triage spawned, see fix-issue / nf-metro-layout-fix.
---

# nf-metro gate-arm triage

Every `if`/`while` in `layout/routing/` is a *gate* whose arms a novel pipeline
can hit untested, producing a visual defect. The `scripts/routing_gate_coverage.py`
matrix enumerates which corpus fixtures reach each arm; this skill works the
un-exercised arms of one module to zero open gaps.

**The methodology is documented in full at
[`docs/dev/routing_gate_triage.md`](../../../docs/dev/routing_gate_triage.md).**
Read it first - it is the source of truth for the four verdicts, the artifacts,
the workflow, and the hard-won gotchas. This skill is the thin operational
wrapper; it does not restate the detail.

**Conventions** (substitute if your setup differs):
- Local nf-metro checkout: `~/projects/nf-metro`; upstream `pinin4fjords/nf-metro`.
- Render preview: `pinin4fjords.github.io/nf-metro/_pr/<N>/`.
- This skill builds on `fix-issue` for worktree/env/PR hygiene - follow that
  skill's setup and additive-only rules.

## What this skill does

Drives **one module slice** (or a cluster of tiny modules) end-to-end: from a
worktree off `origin/main`, through per-arm classification, the human visual
verdict, a regenerated matrix, to a reviewable PR. It ships **verdicts and
fixtures only** - never engine behaviour changes.

## Steps

1. **Scope.** Confirm which module (or tiny-module cluster) this slice covers.
   Re-run `python scripts/routing_gate_coverage.py` in a fresh worktree to get
   that module's *live* un-exercised arms - gap counts drift, so never trust a
   number from an issue body. If a per-module issue exists (the #727-#733 family,
   #748), it carries the arm list; reconfirm it against the live matrix.

2. **Classify every arm** into exactly one of the four verdicts from the doc:
   **reachable** (author a minimal fixture, wire `GALLERY_ENTRIES`, verify the arm
   flipped via the coverage script - the oracle), **reachable-but-defective**
   (file a bug, park `needs-review`, do NOT commit/distort the fixture),
   **defensive** (annotate why no valid topology takes it), **candidate-dead**
   (flag with reachability evidence, do NOT delete - that is the #689 pass).
   Opus drives; fan the reachable lane out to sonnet sub-agents, each confirming
   its flip with the coverage script. Append a triage-JSON card per fixture.

   Watch the two traps the doc calls out: **phantom arcs** (multi-line
   condition/literal/ternary mis-attribution - tooling noise, see #746, do not
   hand-classify as defensive) and **"corpus doesn't hit it" ≠ defensive** (a
   correction-pass arm with zero hits is usually reachable - author a fixture).

3. **Human visual verdict before PR-open.** Build the review page and get a
   verdict on every new fixture - the validator has blind spots the eyeball
   catches:
   ```
   source ~/.local/bin/mm-activate nf-metro && export PYTHONPATH="$PWD/src"
   python .claude/skills/nf-metro-layout-triage/build_review.py --worktree "$PWD" \
       --output-dir /tmp/gate-triage-out --violations /tmp/gate-triage-<module>.json
   cd /tmp/gate-triage-out && python -m http.server 8765
   ```
   Any **Bug** verdict not already classified defective: pull the fixture from
   `GALLERY_ENTRIES`, file an issue with the repro, park its arm `needs-review`
   linked to that issue. Nothing flagged gets silently dropped.

4. **Regenerate + ratchet.** Run the script with `--write` to regenerate
   `docs/dev/routing_gate_coverage.md` + the baseline. Keep the three ratchet
   tests in `tests/test_routing_gate_coverage.py` green (they skip off the pinned
   CPython - regenerate only under `BASELINE_PYTHON`). The stale-key test means a
   removed gap needs its triage entry removed in the same PR.

5. **Ship per `fix-issue` hygiene.** Invariant-test-first where a fixture asserts a
   layout property, validator pass, `/simplify` as its own commit, full CI lint
   (`ruff format --check` + `ruff check` + `mypy`), additive commits only, no
   force-push, verify origin after each push. Stop at PR-open against `main`.

**Done when** the module shows zero blank-Triage rows in the matrix and the PR is
open for review. The slice carries no engine behaviour change; any bug it
surfaced is filed separately (the `reachable-but-defective` lane).

## Mid-campaign merges

If another PR lands while this slice is in flight, resolve the shared coverage
files by **union**: start from `main`'s triage JSON, add only this module's keys,
then regenerate the doc + baseline. Never hand-merge the generated files.
