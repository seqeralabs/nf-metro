---
title: "Routing gate-arm triage"
description: "The routing gate-arm triage campaign: the four verdicts, the ratchet tests, and the pitfalls."
sidebar:
  label: "Routing gate triage"
  order: 6
---

A triage campaign gives every un-exercised branch in `layout/routing/` a verdict.
This page is the _process_ side.
The auto-generated matrix it operates on is [`routing_gate_coverage.md`](/nf-metro/dev/routing_gate_coverage/), which `scripts/routing_gate_coverage.py` produces.

## Un-exercised gate arms

Every `if` and `while` in the routing subpackage is a _gate_ with two or more arms.
A gate written for the topologies in hand can fire, or fail to fire, on a novel pipeline and produce a visual defect.
That is the engine's characteristic fragility.
An arm reached by **zero corpus fixtures** is an untested assumption.
The coverage matrix turns "every new pipeline stress-tests every implicit assumption" into a finite, enumerated checklist.
Triage is the act of working that checklist to zero open gaps.

The campaign pays off twice over.
It hardens the engine.
Each _reachable_ arm gets a fixture, and an arm reachable only through a defective render spawns a bug report.
It also documents the rest.
A defensive guard or a piece of dead code gets a recorded reason, and no future reader has to re-investigate it cold.

## Artifacts

| File                                             | Role                                                                                                                                                                                                                                                     |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `scripts/routing_gate_coverage.py`               | The tool. Renders the whole `examples/` corpus under per-fixture branch coverage, restricted to routing modules, and maps each gate arm to the fixtures reaching it. `--write` regenerates the doc and baseline. `--json` dumps machine-readable output. |
| `docs/dev/routing_gate_coverage.md`              | Generated matrix. One row per gap gate, with a **Triage** column carrying the verdict. Do not hand-edit. Regenerate.                                                                                                                                     |
| `tests/data/routing_gate_triage.json`            | The verdict sidecar. One keyed entry per triaged arm (`module.py::<gate text>::#<n>`) with `status` + `note`.                                                                                                                                            |
| `tests/data/routing_gate_coverage_baseline.json` | The ratchet baseline (the frozen gap set).                                                                                                                                                                                                               |
| `tests/test_routing_gate_coverage.py`            | The ratchet. Three tests gate the program.                                                                                                                                                                                                               |

The ratchet (skipped off the pinned interpreter):

- `test_no_new_un_exercised_routing_gate_arm`: a new gate must ship with a fixture hitting both arms, or the gap set grows and this reds.
- `test_gate_coverage_baseline_in_sync`: the committed baseline matches what the script computes now.
- `test_triage_sidecar_references_open_gaps`: every triage key still corresponds to a live gap, with no **stale keys**.
  Closing or removing a gap requires removing its triage entry in the same change.

## The four verdicts (lane rules)

Every un-exercised arm resolves to exactly one of these:

- **reachable**: a valid topology takes this arm, but no shipped fixture does yet.
  Author a **minimal valid** topology fixture in `examples/topologies/`, wire it into `GALLERY_ENTRIES` in `scripts/build_gallery.py`, then **verify the arm flipped** from un-exercised to exercised by re-running the coverage script.
  The script is the oracle that makes this lane safe to delegate.
  If the arm did not flip, the fixture is wrong.
  Iterate rather than commit.
- **reachable-but-defective**: the _only_ topology that reaches the arm exposes a render you would not ship, such as a curve through a label, a bypass-V collision, a kink, an overlap, or a route through a section box.
  Do **not** commit the fixture, and do **not** distort it with shrunk labels or hacked spacing to dodge the defect.
  That is cheating.
  **File a bug** with the repro `.mmd`, the arm reference, and the expected clean behavior, then park the arm as `needs-review` linked to that issue.
- **defensive**: a guard clause no valid topology can violate, such as a null or contract check, an empty-collection skip, or a coincidence guard.
  Annotate it with _why_ a valid graph never takes it.
  Delete no code.
- **candidate-dead**: no constructible topology reaches it, but it is live code.
  Flag it `candidate-dead` **with reachability evidence**, and do **not** delete it here.
  Deletion is a separate, deliberate pass, because byte-identical renders are not proof of deadness.

`needs-review` is a _holding_ status rather than a final verdict: an arm waiting on a filed bug, or one not yet classified.
A campaign is not done while any arm is `needs-review`.

## Run a slice

1. **Worktree off current `origin/main`.** Re-run `python scripts/routing_gate_coverage.py` first.
   Gap counts drift as fixtures land elsewhere.
   Never trust a stale number from an issue body.
2. **One PR per module.** Cluster the tiny modules, such as `core.py`, `families.py`, and `corners.py`, into one PR.
   That keeps each PR reviewable and mergeable.
3. **Fan reachable arms out concurrently.** Work through several gate conditions in parallel.
   Each worker reads one gate condition, authors a candidate fixture, and confirms through the coverage script that the arm flipped.
   Parallel work is safe because the script is the oracle, and each worker's result is independently verifiable.
4. **Classify every arm** into one of the four verdicts.
   Append a card per new fixture to a shared triage JSON.
5. **Human visual verdict before PR-open.** Build the review page and get a verdict on _every_ new fixture:
   ```bash frame="terminal"
   source ~/.local/bin/mm-activate nf-metro && export PYTHONPATH="$PWD/src"
   python .claude/skills/nf-metro-layout-triage/build_review.py --worktree "$PWD" \
       --output-dir /tmp/gate-triage-out --violations /tmp/gate-triage-<module>.json
   cd /tmp/gate-triage-out && python -m http.server 8765
   ```
   For any fixture flagged **Bug** that was not already classified defective, pull it from `GALLERY_ENTRIES`, file an issue with the repro, and park its arm as `needs-review` linked to that issue.
   Nothing flagged gets silently dropped.
6. **Regenerate** the doc + baseline (`--write`) and keep the ratchet green.
7. **Full fix-issue hygiene** (see the `fix-issue` skill): invariant-test-first where a fixture asserts a layout property, runtime validator pass, `/simplify` as its own commit, full CI lint (`ruff format --check` + `ruff check` + `mypy`), additive commits only, no force-push, verify origin after each push.
   Stop at PR-open against `main` for review.

A slice is **done** when its module shows zero blank-Triage rows in the matrix, meaning every arm is reachable-fixtured, defensive-annotated, candidate-dead-flagged, or needs-review-linked.

## Pitfalls

- **Phantom arcs inflate the backlog.** `FileReporter.arcs()` attributes a branch arc to the _opening_ line of a multi-line `if (`, list or tuple literal, or ternary, while CPython records the executed arc from an _operand_ line.
  The matrix then reports a gap on a gate whose arms both run.
  These are tooling noise.
  Do not hand-classify them as `defensive`.
  Fix the detector in the script instead.
  An un-exercised arc `(src, dst)` is phantom when `dst` is reached by an executed arc from a different source line in the same construct.
- **A collapsed phantom gate can hide a real operand gap.** When a wrapped `and` or `or` condition's opening line carries _no_ branch bytecode at all, every arc originates on an operand line.
  The matrix then re-attributes the decision to its operand lines, and each operand short-circuit becomes its own gate.
  That is what keeps a `defensive` verdict on the collapsed opening line from masking an operand whose short-circuit no fixture takes, such as an `or` chain's final fall-through.
  Triage the operand rows on their own merits.
  A contract-guard operand like `x is not None` is `defensive`, while a reachable-but-untested one wants a fixture.
  The script expands only conditions whose operands are each single-line and non-nested.
  Tangled ones stay collapsed.
- **"Corpus doesn't hit it" is not "no valid topology reaches it."** A _correction pass_ arm with zero corpus hits is usually **reachable**.
  Author a fixture that triggers the correction rather than marking it **defensive**.
  Labeling such an arm defensive on a "never fires across N corpus calls" basis loses a regression fixture for a real defect class.
  That is exactly how the `clear_channel_of_section_edge` graze arm was once misjudged.
- **Validators have gaps, and the human eye is load-bearing.** `nf-metro validate --with-layout` only sees `validate=True`-block guards, and route crossings are warnings rather than failures.
  The validator and the test suite cannot catch every class of defect.
  Always run the _full_ suite **and** put the new fixtures in front of a human through the review page.
- **The arc model is CPython-version-specific.** The script pins `BASELINE_PYTHON = (3, 11)`, and the ratchet tests skip on any other interpreter.
  Regenerate the baseline only under the pinned version.
- **Operand-level coverage is hash-seed sensitive.** The layout engine iterates hash-ordered sets while rendering.
  Which operand of a short-circuit decides a branch can therefore vary by `PYTHONHASHSEED`, even when the SVG is identical.
  The script pins `PINNED_HASH_SEED = "0"`, re-execing itself when run without it, and the ratchet test runs the sweep in a seed-pinned subprocess.
  Regenerate the baseline only at the pinned seed.
- **Use `FileReporter.arcs()`, not `missing_branch_arcs()`**, and exclude `__init__.py` along with `invariants.py`, which is the `validate=True` checker rather than a routing decision gate.
- **Triage JSON hygiene.** Keep it ordered, at `indent=2`, with a trailing newline.
  The stale-key ratchet means that removing a gap, whether by closing it with a fixture or by a phantom-arc fix dropping it, requires removing its triage entry in the same PR.
- **Mid-campaign merges.** When another PR lands while a slice is in flight, resolve the shared coverage files by **union**.
  Start from `main`'s triage JSON, add only your module's keys, then regenerate the doc and baseline.
  Do not hand-merge the generated files.

## Lifecycle: permanent infra vs episodic campaign

The infra and the campaign have different lifetimes:

- **The infra is permanent and self-maintaining.** The tool, the matrix doc, the baseline, the triage JSON, and the three ratchet tests live in the repo permanently, and CI keeps them honest on _every_ routing change rather than a standing owner doing so.
- **A campaign is episodic.** Driving the open gaps down to zero verdicts is a finite project you run when the backlog has grown.
  Between campaigns the ratchet holds the line, and it does not require a campaign to be running.

The ratchet does _not_ force a pre-existing open gap to get a verdict.
The open-gap backlog therefore drifts slowly upward as gates are added and acknowledged.
A campaign is what pays it back down.
That is the intended rhythm rather than a leak.

## Maintain the infra as routing evolves

Three CI-enforced events keep everything in sync.
Whoever touches `layout/routing/` handles each one in the PR that causes it:

1. **A new gate gains an un-exercised arm**, which reds `test_no_new_un_exercised_routing_gate_arm`.
   Resolve it _consciously_.
   Either author a fixture that hits both arms and close it, or, if the arm is unreachable, confirm that and regenerate the baseline with `--write` to acknowledge it as a new open gap.
   The baseline diff makes the acknowledgment visible to a reviewer, and gaps cannot slip in silently.
   Acknowledging is the cheap path, which is why backlogs accrete and campaigns exist.
2. **A change closes a gap or removes a gate**, which reds `test_gate_coverage_baseline_in_sync`, because the baseline now claims a gap the corpus exercises or one that no longer exists.
   Regenerate the baseline in the same PR so the ratchet stays tight.
3. **A gate's condition text is edited or removed, or its gap closes**, which makes its triage entry stale and reds `test_triage_sidecar_references_open_gaps`.
   Prune or update that entry in `tests/data/routing_gate_triage.json` in the same PR.

This stays low-friction in practice because **triage keys are `module.py::<gate text>::#<ordinal>`, not line numbers.** The most common churn, code shifting up or down, touches no key, and the matrix doc's line numbers regenerate.
Only _semantic_ edits disturb a key: changing a gate's condition, deleting a gate, or reordering identical-text gates.
The stale-key test catches exactly those.

Run `--write` and reconcile the triage JSON as a normal part of any routing PR that adds, removes, or rewrites a gate.
No separate maintenance pass is needed between campaigns.

## Reconcile `needs-review` when a parked bug closes

A `needs-review` arm filed through the **reachable-but-defective** lane is parked on a bug issue.
The arm is only reached by a topology that renders defectively, and no fixture was shippable yet.
When that bug is fixed and merged, the arm does **not** resolve itself.
It stays a `needs-review` gap until someone reconciles it against _how_ the bug was fixed.
The reconciliation has three outcomes, one per fix shape:

- **Fixed by rendering the topology cleanly.** The blocker is gone.
  Author the clean fixture now through the standard `reachable` lane, verify through the coverage script that the arm flips, and remove its `needs-review` entry.
  This is the common case and the bulk of finalizing a campaign.
- **Fixed by rejecting or reshaping the topology**, for instance where the fix adds a new `BackwardFlowError` or forces a different port side.
  The route that reached the arm may no longer be constructible.
  Check whether _another_ valid topology still reaches it.
  If none does, reclassify it **defensive** or **candidate-dead** with the rejection as evidence.
  If one does, it stays `reachable` and still wants a fixture by the surviving route.
- **Fixed, but the defect was re-filed as a follow-up.** The original bug closed and a sibling opened.
  The arm is still reachable-but-defective.
  Re-point its note to the open follow-up issue.
  It stays parked, now correctly attributed.

Watch the distinction between an arm _parked on_ a closed bug and one _citing_ a closed bug **as the pattern** while parked on an open follow-up.
Only the first is actionable when the bug closes.
A note reading "the same pattern as #NNN, filed as #MMM" is parked on #MMM, which is open, not #NNN, which is closed.

**Where this reconciliation should happen:** inside the bug-fix PR itself, ideally.
If a fix ships the fixture that flips its parked arm, the stale-key ratchet _forces_ that PR to remove the `needs-review` entry in the same change.
The arm leaves the gap set, and its triage key goes stale.
The cleanest path is therefore for each engine fix to retire its own parked arm.
A later finalization sweep then only mops up the arms whose fix PRs did not, plus the reject-or-reshape reclassifications.

## Re-run a campaign later

Run a fresh campaign when the open-gap backlog has grown enough to be worth a sweep, typically after a routing module has accreted new gates over several PRs, or after a refactor changes the dispatch structure.
Start at step 1 of _Run a slice_ for each module.
The four verdicts and the pitfalls described earlier do not change.
