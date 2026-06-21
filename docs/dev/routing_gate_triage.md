# Routing gate-arm triage

How to run a campaign that gives every un-exercised branch in `layout/routing/`
a verdict. This is the *process* doc; the auto-generated matrix it operates on
is [`routing_gate_coverage.md`](routing_gate_coverage.md), and the tool that
produces it is `scripts/routing_gate_coverage.py`.

## Why this exists

Every `if`/`while` in the routing subpackage is a *gate* with two or more arms.
A gate written for the topologies in hand can fire (or fail to fire) on a novel
pipeline and produce a visual defect - that is the fragility the engine is prone
to. An arm reached by **zero corpus fixtures** is an untested assumption. The
coverage matrix turns "every new pipeline stress-tests every implicit
assumption" into a finite, enumerated checklist; triage is the act of working
that checklist to zero open gaps.

The payoff is twofold: the campaign hardens the engine (each *reachable* arm
gets a fixture, and arms that only reach via a defective render spawn bug
reports), and it documents the rest (defensive guards and dead code get a
recorded reason so no future reader re-investigates them cold).

## Artifacts

| File | Role |
|---|---|
| `scripts/routing_gate_coverage.py` | The tool. Renders the whole `examples/` corpus under per-fixture branch coverage, restricted to routing modules, and maps each gate arm to the fixtures reaching it. `--write` regenerates the doc + baseline; `--json` dumps machine-readable. |
| `docs/dev/routing_gate_coverage.md` | Generated matrix. One row per gap gate, with a **Triage** column carrying the verdict. Do not hand-edit; regenerate. |
| `tests/data/routing_gate_triage.json` | The verdict sidecar. One keyed entry per triaged arm (`module.py::<gate text>::#<n>`) with `status` + `note`. |
| `tests/data/routing_gate_coverage_baseline.json` | The ratchet baseline (the frozen gap set). |
| `tests/test_routing_gate_coverage.py` | The ratchet. Three tests gate the program (below). |

The ratchet (skipped off the pinned interpreter):

- `test_no_new_un_exercised_routing_gate_arm` - a new gate must ship with a
  fixture hitting both arms, or the gap set grows and this reds.
- `test_gate_coverage_baseline_in_sync` - the committed baseline matches what the
  script computes now.
- `test_triage_sidecar_references_open_gaps` - every triage key still corresponds
  to a live gap (no **stale keys**); closing or removing a gap requires removing
  its triage entry in the same change.

## The four verdicts (lane rules)

Every un-exercised arm resolves to exactly one of these. This is the heart of
the methodology - apply it per arm.

- **reachable** - a valid topology takes this arm; no shipped fixture does yet.
  Author a **minimal valid** topology fixture in `examples/topologies/`, wire it
  into `GALLERY_ENTRIES` (`scripts/build_gallery.py`), and **verify the arm
  flipped** un-exercised -> exercised by re-running the coverage script. The
  script is the oracle that makes this lane safe to delegate. If it didn't flip,
  the fixture is wrong - iterate, don't commit.
- **reachable-but-defective** (the #688 pattern) - the *only* topology that
  reaches the arm exposes a render you would not ship (curve through a label,
  bypass-V collision, kink, overlap, route through a section box). Do **not**
  commit the fixture, and do **not** distort it (shrunk labels, hacked spacing)
  to dodge the defect - that is cheating. **File a bug** with the repro `.mmd`,
  the arm reference, and the expected clean behaviour, then park the arm as
  `needs-review` linked to that issue.
- **defensive** - a guard clause no valid topology can violate (null/contract
  checks, empty-collection skips, coincidence guards). Annotate with *why* a
  valid graph never takes it. No code deletion.
- **candidate-dead** - no constructible topology reaches it, but it is live code.
  Flag it `candidate-dead` **with reachability evidence**; do **not** delete it
  here. Deletion is a separate, deliberate pass (#689), because byte-identical
  renders are not proof of deadness.

`needs-review` is a *holding* status, not a final verdict: an arm waiting on a
filed bug, or one not yet classified. A campaign is not done while any arm is
`needs-review`.

## Running a slice

1. **Worktree off current `origin/main`.** Re-run
   `python scripts/routing_gate_coverage.py` first - gap counts drift as
   fixtures land elsewhere, so never trust a stale number from an issue body.
2. **One PR per module** (cluster the tiny modules - e.g.
   `core.py` + `inter_section.py` + `corners.py` - into one PR). Keeps each
   reviewable and mergeable.
3. **Opus drives; fan the reachable lane out to sonnet sub-agents.** Each sonnet
   agent reads one gate condition, authors a candidate fixture, and confirms the
   flip via the coverage script. The script being the oracle is what makes the
   fan-out safe.
4. **Classify every arm** into one of the four verdicts. Append a card per new
   fixture to a shared triage JSON.
5. **Human visual verdict before PR-open.** Build the review page and get a
   verdict on *every* new fixture:
   ```
   source ~/.local/bin/mm-activate nf-metro && export PYTHONPATH="$PWD/src"
   python .claude/skills/nf-metro-layout-triage/build_review.py --worktree "$PWD" \
       --output-dir /tmp/gate-triage-out --violations /tmp/gate-triage-<module>.json
   cd /tmp/gate-triage-out && python -m http.server 8765
   ```
   Any fixture flagged **Bug** that was not already classified defective: pull it
   from `GALLERY_ENTRIES`, file an issue with the repro, park its arm
   `needs-review` linked to that issue. Nothing flagged gets silently dropped.
6. **Regenerate** the doc + baseline (`--write`) and keep the ratchet green.
7. **Full fix-issue hygiene** (see the `fix-issue` skill): invariant-test-first
   where a fixture asserts a layout property, runtime validator pass, `/simplify`
   as its own commit, full CI lint (`ruff format --check` + `ruff check` +
   `mypy`), additive commits only, no force-push, verify origin after each push.
   Stop at PR-open against `main` for review.

A slice is **done** when its module shows zero blank-Triage rows in the matrix:
every arm is reachable-fixtured / defensive-annotated / candidate-dead-flagged /
needs-review-linked.

## Gotchas (hard-won)

- **Phantom arcs inflate the backlog (#746).** `FileReporter.arcs()` attributes a
  branch arc to the *opening* line of a multi-line `if (`, list/tuple literal, or
  ternary, while CPython records the executed arc from an *operand* line. The
  matrix then reports a gap on a gate whose arms both actually run. These are
  tooling noise - do not hand-classify them as `defensive`; fix the detector in
  the script instead (an un-exercised arc `(src, dst)` is phantom when `dst` is
  reached by an executed arc from a different source line in the same construct).
- **A collapsed phantom gate can hide a real operand gap (#741).** When a wrapped
  `and`/`or` condition's opening line carries *no* branch bytecode at all (every
  arc originates on an operand line), the matrix re-attributes the decision to its
  operand lines: each operand short-circuit becomes its own gate. This is what
  keeps a `defensive` verdict on the collapsed opening line from masking an
  operand whose short-circuit no fixture takes (e.g. an `or` chain's final
  fall-through). Triage the operand rows on their own merits; a contract-guard
  operand (`x is not None`) is `defensive`, a reachable-but-untested one wants a
  fixture. Only conditions whose operands are each single-line and non-nested are
  expanded; tangled ones stay collapsed.
- **"Corpus doesn't hit it" is not "no valid topology reaches it."** A *correction
  pass* arm with zero corpus hits is usually **reachable** (author a fixture that
  triggers the correction), not **defensive**. Labeling such an arm defensive on a
  "never fires across N corpus calls" basis loses a regression fixture for a real
  defect class - this is exactly how the `clear_channel_of_section_edge` graze arm
  was misjudged before #736 was filed.
- **Validators have blind spots; the human eyeball is load-bearing.**
  `probe_layout.py` only sees `validate=True`-block guards, and route crossings are
  warnings, not failures. Neither the validator nor the suite caught the
  eager-bundling violations (#702) or the graze (#736). Always run the *full* suite
  **and** put the new fixtures in front of a human via the review page.
- **The arc model is CPython-version-specific.** The script pins
  `BASELINE_PYTHON = (3, 11)`; the ratchet tests skip on any other interpreter.
  Regenerate the baseline only under the pinned version.
- **Operand-level coverage is hash-seed sensitive.** The layout engine iterates
  hash-ordered sets while rendering, so which operand of a short-circuit decides
  a branch can vary by `PYTHONHASHSEED` even though the SVG is identical. The
  script pins `PINNED_HASH_SEED = "0"` (re-execing itself when run without it) and
  the ratchet test runs the sweep in a seed-pinned subprocess. Regenerate the
  baseline only at the pinned seed.
- **Use `FileReporter.arcs()`, not `missing_branch_arcs()`**, and exclude
  `invariants.py` (it is the `validate=True` checker, not a routing decision gate)
  and `__init__.py`.
- **Triage JSON hygiene.** Keep it ordered, `indent=2`, trailing newline. The
  stale-key ratchet means removing a gap (e.g. closing it with a fixture, or a
  phantom-arc fix dropping it) requires removing its triage entry in the same PR.
- **Mid-campaign merges.** When another PR lands while a slice is in flight,
  resolve the shared coverage files by **union**: start from `main`'s triage JSON,
  add only your module's keys, then regenerate the doc + baseline. Do not
  hand-merge the generated files.

## Program structure

The campaign is sliced by module under one umbrella, with a separate deletion
pass and a tooling-quality issue:

- **#677** built the coverage matrix (tool + doc + ratchet).
- **#687** is the umbrella: triage and close every un-exercised arm.
- Module slices: #690 `intra_handlers`, #691 `offsets`, #692
  `inter_section_handlers`, #701 `normalize` (with #748 for its tail), and
  #727-#733 the long-tail modules.
- **#689** is the deferred **deletion** pass over the `candidate-dead` arms.
- **#746** fixes the phantom-arc tooling defect so future slices triage only real
  gaps; **#741** extends it to operand granularity, expanding a fully-phantom
  wrapped `and`/`or` into per-operand gates so a hidden short-circuit gap surfaces
  as its own row.

The triage program is also a bug *finder*: the `reachable-but-defective` lane has
spawned engine fixes (#688, #695, #696, #698, #736, ...). Those are filed and
fixed on their own, outside the triage PRs - a triage slice ships verdicts and
fixtures, never engine behaviour changes.

## Lifecycle: permanent infra vs episodic campaign

The distinction matters, because it answers "do I have to babysit this?":

- **The infra is permanent and self-maintaining.** The tool, the matrix doc, the
  baseline, the triage JSON, and the three ratchet tests live in the repo forever
  and are kept honest by CI on *every* routing change - not by a standing owner.
- **A campaign is episodic.** "Drive the open gaps down to zero verdicts" is a
  finite project you run when the backlog has grown. Between campaigns the ratchet
  holds the line; it does not require a campaign to be running.

What the ratchet does *not* do is force a pre-existing open gap to get a verdict.
So the open-gap backlog drifts slowly upward as gates are added and acknowledged
(see below), and a campaign is what pays it back down. That is the intended
rhythm, not a leak.

## Maintaining the infra as routing evolves

Three CI-enforced events keep everything in sync; each is handled in the PR that
causes it, by whoever touches `layout/routing/`:

1. **A new gate gains an un-exercised arm** -> `test_no_new_un_exercised_routing_gate_arm`
   reds. Resolve it *consciously*: either author a fixture that hits both arms
   (close it), or - if the arm is genuinely unreachable - confirm that and
   regenerate the baseline (`--write`) to acknowledge it as a new open gap. The
   baseline diff makes the acknowledgement visible to a reviewer; gaps cannot slip
   in silently. (Acknowledging is the cheap path, which is why backlogs accrete
   and campaigns exist.)
2. **A change closes a gap or removes a gate** -> `test_gate_coverage_baseline_in_sync`
   reds (the baseline now claims a gap the corpus exercises, or that no longer
   exists). Regenerate the baseline in the same PR so the ratchet stays tight.
3. **A gate's condition text is edited, removed, or its gap closes** -> its triage
   entry goes stale and `test_triage_sidecar_references_open_gaps` reds. Prune or
   update that entry in `tests/data/routing_gate_triage.json` in the same PR.

The reason this is low-friction in practice: **triage keys are
`module.py::<gate text>::#<ordinal>`, not line numbers.** The most common churn -
code shifting up or down - does not touch any key; the matrix doc's line numbers
simply regenerate. Only *semantic* edits (changing a gate's condition, deleting a
gate, reordering identical-text gates) disturb a key, and the stale-key test
catches exactly those.

So: run `--write` and reconcile the triage JSON as a normal part of any routing PR
that adds, removes, or rewrites a gate. No separate maintenance pass is needed
between campaigns.

## Finalising: reconciling `needs-review` when a parked bug closes

A `needs-review` arm filed via the **reachable-but-defective** lane is parked on a
bug issue: the arm is only reached by a topology that renders defectively, so no
fixture was shippable yet. When that bug is fixed and merged, the arm does **not**
resolve itself - it stays a `needs-review` gap until someone reconciles it against
*how* the bug was fixed. Three outcomes, by fix shape:

- **Fixed by rendering the topology cleanly** -> the blocker is gone; author the
  clean fixture now (the standard `reachable` lane), verify the arm flips via the
  coverage script, and remove its `needs-review` entry. This is the common case
  and the bulk of finalising a campaign.
- **Fixed by rejecting or reshaping the topology** (e.g. the fix adds a new
  `BackwardFlowError` or forces a different port side) -> the route that reached
  the arm may no longer be constructible. Check whether *another* valid topology
  still reaches it: if not, reclassify **defensive**/**candidate-dead** with the
  rejection as evidence; if so, it stays `reachable` and still wants a fixture by
  the surviving route.
- **Fixed, but the defect was re-filed as a follow-up** (the original bug closed,
  a sibling opened) -> the arm is still reachable-but-defective; re-point its note
  to the open follow-up issue. It stays parked, now correctly attributed.

Watch the distinction between *parked on* a closed bug and *citing* a closed bug
**as the pattern** while parked on an open follow-up - only the former is
actionable when the bug closes. A note that reads "the #688 pattern, filed as
\#740" is parked on #740 (open), not #688 (closed).

**Where this reconciliation should happen:** ideally inside the bug-fix PR itself.
If a fix ships the fixture that flips its parked arm, the stale-key ratchet
*forces* that PR to remove the `needs-review` entry in the same change (the arm
leaves the gap set, so its triage key goes stale). So the cleanest path is for
each engine fix to retire its own parked arm; a later finalisation sweep then only
mops up the arms whose fix PRs did not, plus the reject/reshape reclassifications.

## Re-running a campaign in future

Run a fresh campaign when the open-gap backlog has grown enough to be worth a
sweep - typically after a routing module has accreted new gates over several PRs,
or after a refactor changes the dispatch structure. Start at step 1 of *Running a
slice* per module; the four verdicts and the gotchas above do not change.
