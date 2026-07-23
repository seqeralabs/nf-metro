---
name: fix-issue
description: End-to-end workflow for fixing GitHub issues on the nf-metro repo with diagnostic rigor. Use when the user references a GitHub issue (by number, URL, or description) and wants it fixed. Handles worktree setup, a reused persistent env (no per-issue env creation), diagnostic-first investigation, authoring-mistake-vs-engine-bug triage (never dodge an engine bug by doctoring the reproducer), invariant-test-first implementation, runtime validators, evidence-cited fix verification, /simplify pass, full-repo lint, visual review via render preview, narrow-the-fix iteration on regressions, cost discipline (targeted tests, CI render-diff over local gallery rebuilds, skip-ci on WIP), scope discipline that fixes adjacent fallout in-session via delegated subagents instead of deferring it to a child issue, standalone issue-body hygiene, additive-only PR hygiene (no force-push, no narrative comments), clean execution of an authorised admin-merge (preserve history, no CI re-run), origin verification after every push, and PR creation. Supports an optional autonomous / net-negative mode (activated by "no deferrals", "work overnight", "net-negative issues", "drive it to conclusion", or "push a complete fix and address all the fallout") that suspends the multi-session-deferral carve-out and drives every surfaced fallout issue to a landed, reviewable PR the same session, while still merging only what the user authorises per-PR. Trigger on phrases like "fix issue #N", "address #N", "work on issue N", or any request to fix a bug or implement a feature that references an issue. For shepherding a chain of already-existing PRs back to main, see `pr-chain-vet` instead.
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

## Scope discipline: fix the fallout, don't defer it

This skill has historically erred toward "that's a separate issue" the moment
a fix surfaces a second problem - a related engine bug, a coverage gap, a
stale test, a lint violation in a file already open, even one in a
completely different subsystem. That default is flipped: **resolving
fallout within this same session is the default; filing an issue and leaving
it for a future session is the exception.** A queue of maybe-someday child
issues is administrative burden on the user, not a scope win - re-triaging
each one later costs more than fixing it now while the context is already
loaded.

- When diagnosis, implementation, `/simplify`, lint, or CI turns up an
  adjacent, fixable problem anywhere in the repo - including in a different
  subsystem than the one you're already editing - fix it in this session
  rather than filing it and moving on. Being in a different subsystem,
  needing its own worktree, or requiring you to load unfamiliar code is
  **not, by itself**, a reason to defer.
- Use the `Agent` tool to protect your own context budget while doing this,
  not as a reason to skip it. **Always pass an explicit `model` param on the
  `Agent` call - never omit it and let the child silently inherit the
  session's model.** Omitting `model` is not "no decision was made", it's
  "opus got picked by default without anyone deciding that." Default to
  `model: "sonnet"`; set `model: "opus"` only when you decide *before
  spawning* that the triage or fix genuinely needs harder judgment, and say
  so in one line when you do. If you notice after the fact that a child ran
  on opus with no `model` param set, that's a mistake to correct (restart it
  on sonnet), not a choice to defend - don't rationalize keeping it because
  the task turned out to suit opus in hindsight; that judgment call has to
  happen at spawn time, explicitly, or not at all. Run agents in the
  background (or a separate worktree, per the global worktree rule, if the
  fallout touches files you're not actively editing) and fold results back
  in before the session ends. Independent pieces of fallout can run as
  concurrent subagents instead of serially eating your own context - that's
  what makes taking on a different subsystem's fix tractable in the same
  session.
- "Resolved in this session" does not mean "crammed into one PR." If folding
  the fallout into the primary PR would make it harder to review, ship it as
  its own sibling PR instead. The bar is that the fallout's PR gets built,
  pushed, and left in a reviewable state before the session ends - not that a
  future session has to rediscover it from a filed issue.
- Reserve an actual filed issue + deferral for cases that are genuinely
  disproportionate: a fix that would take multiple sessions to complete in
  its own right, or something that needs a decision only the user can make.
  **Size and duration are the test, not which subsystem the fallout lives
  in.** When you do defer, say so explicitly and why - deferral should be a
  stated judgment call, never the silent default.
- This is not licence for scope creep into features the user didn't ask
  about - it's about not walking away from problems the *current* work
  surfaced.

This governs every step below: the "second finding" in Step 3, coverage gaps
in the Step 7 gate-coverage ratchet, and anything `/simplify` or review turns
up in Step 6 all default to fix-now-via-subagent (in this PR or a sibling
one) over file-and-defer.

## Optional mode: autonomous / net-negative

Activate this mode when the user signals it - "no deferrals", "no giving up",
"drive it to conclusion", "net-negative issues", "work overnight", "I'll leave
you to it", or an explicit "push a complete fix and address all the fallout".
It does not relax the diagnostic or verification rigour of the steps below; it
changes two defaults.

**1. The deferral exception is suspended.** The "genuinely disproportionate /
multi-session" carve-out above does not apply in this mode. A coupled or hard
defect, and any incident issue the work surfaces (a second engine bug, a
seam/reversal soundness gap, a hygiene refactor `/simplify` flags), is driven
to a landed, reviewable PR *this session* - not filed and left. Delegate the
heavy work to agents (one own worktree each, explicit `model`, run in the
background so independent fallout runs concurrently), and give each a hard
acceptance bar rather than an open question: "clean render AND byte-identical
corpus verified via real `render_svg()` md5 hashes; iterate until both hold;
do not report not-bounded." The CI render-diff is the vetting gate that makes a
corpus-risky change to shared code safe to ship autonomously - a byte-identical
corpus plus a green render-diff is the proof, so you are free to touch a shared
handler as long as that holds. When a first agent reports "blocked / not
bounded", re-brief it with the diagnosis it produced and push through; a
thorough diagnosis is the *start* of the fix, never a substitute for it.

**2. Net-negative open-issue count.** The session must END with fewer open
issues than it started. Never open an issue and stop: opening one only to close
it via a same-session PR is fine (net zero on that issue); leaving it OPEN at
session end is the failure mode this mode exists to prevent. If you file a
child issue at all, you owe its resolution - an agent, a sibling PR - the same
session. Track the arithmetic explicitly at the end: issues closed vs. issues
opened-and-still-open; the second number must be 0, or the total must go down.

**Bounded to the fix's fallout, not a frontier.** This mode resolves what the
*current* work surfaces - a coupled defect, an incident bug, a `/simplify`
refactor of code you just touched - with a proportionate handful of agents. It
is not licence to keep pulling threads into a full-engine overhaul or a cascade
of dozens of child sessions. If a surfaced item is itself a large program - a
subsystem rewrite, a sweeping cross-cutting refactor, anything that would
genuinely need many agents or several sessions *in its own right* - that is the
one case that still stops: flag it, size it, and get a user decision rather
than silently recursing into it. The bar is "finish the thing I was asked to
fix, plus the debris it kicked up", not "fix everything adjacent". Deferral
returns here, and only here - narrowed to genuinely large programs, never to
"this is hard" or "it is a different subsystem".

**Still in force in this mode - do not over-read "autonomous":**
- Never merge without explicit per-PR authorisation from the user. Autonomy is
  about resolving and pushing *complete* work, not self-authorising merges.
  "Drive to conclusion" means "get every PR green and reviewable"; merge only
  what the user okays, per-PR (Step 8 / Step 12). Pre-authorisation to *work*
  overnight is not pre-authorisation to *merge*.
- Every step's rigour below still applies to every PR, including the fallout
  ones: diagnostic-first, invariant-test-first, `/simplify`, evidence-cited
  verification, additive-only pushes, origin check, standalone issue bodies.
- Verify each delegated agent's result yourself - re-run the byte-identical
  corpus diff with real renders, confirm the test fails without the fix - and
  eyeball the actual render; do not merge a claim.
- The one acceptable open-at-end issue is one that genuinely needs a decision
  only the user can make - and then you say so explicitly and get agreement,
  you do not default to it.

## Step 1: Understand the Issue

```bash
gh issue view <N> --repo seqeralabs/nf-metro
```

Summarize the problem and proposed approach. Wait for user confirmation before
proceeding - unless the user has pre-authorised autonomous work (the optional
mode above; e.g. "I'll leave you to work overnight, drive it to conclusion"),
in which case proceed without blocking and report as you go.

### Issue hygiene

Every issue is run through *this skill* fresh in a later session, so the
**issue body must be standalone and self-contained**. When you learn
something during the fix that a future session would need (the real cause, a
repro, a constraint), fold it into the **issue body** - do not scatter it
across comments, and do not leave superseded-approach detail that would
mislead a fresh reader. Keep the body concise. If the fix uncovers a
genuinely separable defect, default to fixing it in this session (see
"Scope discipline" above - delegate to a subagent if it would crowd your
diagnostic context, even for a different subsystem) rather than filing a
child issue and walking away. File a standalone child issue only when the
defect is a multi-session undertaking in its own right, and say so
explicitly.

## Step 2: Worktree + Environment Setup

```bash
# Worktree (always off latest origin/main, never stale local main)
cd ~/projects/nf-metro
git fetch origin main
git worktree add /tmp/nf-metro-fix-<N> -b fix/<N>-<slug> origin/main
```

All subsequent work happens inside `/tmp/nf-metro-fix-<N>`.

### Environment: reuse one persistent env, don't create one per issue

nf-metro is pure Python; the deps (`cairo`, drawsvg, networkx, pillow,
cairosvg, pytest, ruff, mypy, `types-networkx`) change rarely. Creating a
fresh `micromamba` env per issue re-solves and re-downloads all of that
every session for no benefit. Keep **one** long-lived deps env and point it
at the worktree's code per-command:

```bash
# One-time, reused across all issues (skip if it already exists):
ulimit -n 1000000 && export CONDA_OVERRIDE_OSX=15.0 && /opt/homebrew/bin/micromamba create -n nf-metro-dev python=3.11 cairo -y
source ~/.local/bin/mm-activate nf-metro-dev
pip install "drawsvg" "networkx" "pillow" "cairosvg" "pytest" "pytest-xdist" "ruff" "mypy" "types-networkx" "click"
# Refresh this env only when pyproject deps actually change.
```

Then run the worktree's code by prepending its `src/` to `PYTHONPATH` on
each command - **do not** `pip install -e` the worktree into this env:

```bash
source ~/.local/bin/mm-activate nf-metro-dev
cd /tmp/nf-metro-fix-<N>
export PYTHONPATH=/tmp/nf-metro-fix-<N>/src
python -m nf_metro render <file.mmd> -o /tmp/out.svg    # runs THIS worktree
python -m pytest -k <selector>
```

**Why per-command `PYTHONPATH`, not editable install:** an editable install
binds one env's `site-packages` to exactly one worktree path, so it collides
the moment you run two worktrees in parallel. `PYTHONPATH` is set per command
and shadows whatever is installed, so any number of parallel worktree
sessions share the single `nf-metro-dev` env with zero cross-talk. (If you
genuinely want an isolated editable install for one worktree, dedicate a
*separate* env to it - never editable-install a shared env against a
worktree.)

**Commit hooks** need the tools on `PATH` in the same Bash call: the repo
uses `prek` (config `prek.toml`, not `pre-commit`), whose `mypy` hook is
`language: system` and so needs `mypy` on `PATH`. Shell state does not
persist between Bash calls, so run the commit as one call with the env
activated: `source ~/.local/bin/mm-activate nf-metro-dev && cd <worktree> &&
PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit ...`.

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

### Check your premise against current `origin/main` first

Diagnose against latest remote, not a stale tree. `git fetch origin main` and
confirm the bug **still reproduces on latest** before reasoning about a cause -
a sibling PR may already have fixed it or changed the very code you're reading.
If the user says something is already addressed, re-fetch and look again before
disagreeing; "I'm looking at outdated code" is a recurring wrong turn. If a
related PR merges mid-session, `git merge origin/main` into the worktree and
re-diagnose before continuing.

### Classify: authoring mistake or engine bug?

Before touching anything, decide which of two things you're looking at:

- **(a) An mmd authoring mistake** - the `.mmd` misdescribes the pipeline
  (wrong line on a station, a missing edge, a bad directive). The fix *is* to
  edit the input. `probe_layout.py` labels many of these ("authoring
  mistakes vs engine bugs"); `nf-metro explain` shows the rule each inferred
  decision followed.
- **(b) An engine bug on correct mmd** - the input faithfully describes the
  pipeline and the *engine* lays it out badly. The fix goes in `src/`
  (layout / routing / parser). The reproducing `.mmd` stays untouched.

State which one it is, in numbers, before writing code.

### Once it's an engine bug, the reproducer is frozen evidence

**Never "fix" an engine bug by editing the input to dodge the bad layout.**
Do not simplify the reproducing `.mmd`'s labels, drop stations, reorder
lines, or add directives to make the ugly render go away. That changes the
question instead of answering it, and ships a false fix. The map is correct;
the engine must handle it.

This applies to fixtures you *author*, too: when building a new regression
fixture, don't file it down to sidestep a second bad render you notice while
constructing it (e.g. shortening a multi-line label so a label-interaction
bug won't show). A second bad render is a **second finding** - per "Scope
discipline" above, default to fixing it in this session (spin up a subagent
to diagnose/fix it in parallel rather than letting it derail your primary
diagnosis, even if it lands in an unrelated part of the engine) rather than
just noting it and moving on. Only file it standalone and defer if it is
genuinely a multi-session undertaking on its own. A fixture that has
been quietly simplified to look clean no longer locks the bug it was meant to
lock. (Real example: a `"ORF quant"` -> `"ORFquant"` relabel that hid a
multi-line-label interaction rather than reporting it.)

The only legitimate input edits during an engine fix are: authoring a
faithful *new* reproducer, or correcting a genuine (a)-class authoring
mistake you've identified as the actual cause.

### This rule holds for the whole fix, not just the freeze moment

The pull toward touching the `.mmd` instead of `src/` doesn't only show up
when you first classify the bug - it resurfaces later, when the code fix
turns out to be harder than expected: trimming a label, dropping a station,
reordering lines, splitting a section, or adding a directive so the
reproducer renders cleanly while the engine still mishandles the general
case. That is the same workaround wearing the disguise of "polish" or
"narrowing." Step 9's narrowing means gating the *code path* on a real
structural precondition (a topology predicate, a config flag) - never
gating it by rewording the input so the bad path isn't exercised anymore.
If you catch yourself thinking "what if I just changed the mmd instead"
partway through implementation, treat that as the signal to stop and go
fix the engine, not as a shortcut worth taking.

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

### xfail is a lock on a known bug, not an escape hatch

The `strict=True` xfail pattern above exists for issues that arrive with
regression infra already wired in - it locks a bug that's already filed and
tracked. It is not a tool for *this* session to reach for when the real fix
turns out to be harder than expected. Do not mark a new test `xfail`
(strict or otherwise) so you can move on and leave the actual bug for a
future session to untangle - that's the same deferral "Scope discipline"
above already rejects, just spelled with a pytest decorator instead of a
filed child issue. If a test you write in the steps below won't pass, that
means the fix isn't done yet; keep going. It does not mean the test should
be muted.

The only acceptable use of a *new* xfail is the genuine multi-session case
"Scope discipline" already covers: the underlying fix is disproportionate to
this issue, you've said so explicitly, and you're filing a follow-up issue
for it - the xfail marker is a byproduct of that stated deferral, not a
substitute for making it. Treat "I'll xfail this for now" as the same red
flag as "I'll simplify the mmd for now": both quietly convert a bug the
engine has into a fixture that dodges it instead of one that gets fixed.

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

**Re-running it later:** `/simplify` is expensive, so don't re-run it after
every follow-up commit. Only re-run it on the final aggregate diff if later
steps (narrowing a regression, lint/mypy fixes) added a **substantial** chunk
of new production code the first pass never saw. A couple of small,
already-clean follow-up edits don't warrant a second pass.

## Step 7: Lint and Tests

The repo's `prek` hooks (config `prek.toml`) run on every `git commit`: ruff
check/format on `src/` and `tests/`, mypy, trailing whitespace, yaml. If a
commit fails on a hook, fix the issue and re-commit. Never skip hooks with
`--no-verify`.

To run the checks without committing (needs `prek`, which lives on the
`nf-core` env, plus a stub-complete `mypy`):

```bash
micromamba run -n nf-core prek run --all-files
```

Then run the **targeted** tests for the area you changed - not the full
suite by default:

```bash
cd /tmp/nf-metro-fix-<N> && PYTHONPATH=src python -m pytest tests/test_layout_invariants.py -k "<fixture-or-invariant>" -q --no-header
```

CI already runs the complete suite across three Python versions x three
shards on every push (the `test` matrix in `.github/workflows/ci.yml`), so a
routine full local run mostly duplicates work CI is about to do anyway - if
something's broken, the push reds just as loudly, without paying for a local
pass first. Reserve a full local run for when it earns its cost:

- **The change is risky** - it touches shared/widely-used code (`engine.py`
  orchestration, a helper called from many phases, the parser's core model,
  a shared dispatch table) where a narrow selector genuinely can't see the
  blast radius. Here a full local run is *worth it precisely because it's
  faster than CI*: half a minute locally beats waiting on the full
  three-version x three-shard matrix to come back red on something a local
  run would have caught immediately.
- You're about to ask for an admin-merge and want that confidence *before*
  bypassing the review gate, not after.
- A targeted run passed but you have a concrete reason to suspect a wider
  regression it wouldn't reproduce (e.g. you touched a shared constant or a
  cross-cutting guard used well outside the issue's area).

For an ordinary fix scoped to one function, one routing handler, or one
layout phase, targeted tests (plus the gate-coverage ratchet below, if the
change is in `layout/routing/`) is enough - let CI's matrix be the
full-suite gate rather than re-running it locally "just to be safe."

### Cost discipline (applies throughout)

Layout iteration is where sessions burn tokens and compute. Keep it tight:

- **Reuse the persistent env** (Step 2). Do not `micromamba create` per
  issue - it re-solves the whole dependency set every session for nothing.
- **Full suite vs targeted.** Default to the narrowest selection
  (`python -m pytest tests/test_layout_invariants.py -k "<fixture-or-invariant>"`,
  then `--lf` to re-run only what just failed), with `-q --no-header -x` to
  keep the output tiny. Don't run the full suite locally as a routine
  pre-push step - see the reserved-cases list above; when none of them
  apply, skip straight to pushing and let CI's matrix be the full-suite
  check. `addopts` bakes in `-n auto` so a full run *is* cheap in
  wall-time (~half a minute), but the cost that matters here is
  *repetition* - each run's summary re-entering context - so don't pay it
  when CI is about to run the same suite for free. The routing/TB ratchets
  are 3.11-only, so if you do run locally, keep the env on 3.11 or they
  skip and only red in CI.
- **Read coordinates, don't rasterize, for non-visual questions.**
  `inspect_layout.py` / `probe_layout.py` print the geometry as cheap text;
  a render -> cairosvg PNG -> open -> image-into-context cycle is far heavier
  and only earns its cost for a genuine *visual* check. "Is station X on the
  trunk?" is a coordinate read, not a screenshot.
- **Poll CI once, in the background.** A single background watch
  (`until gh pr checks <N> ...; done`) pulls you back when checks resolve;
  re-running `gh pr checks` by hand each turn just dumps status into context
  repeatedly.
- **Lean on the CI render-diff for regression review; don't rebuild the
  gallery locally in a loop.** The CI preview (Step 8) is the authoritative
  whole-corpus diff. A local `build_gallery` / render-diff sweep repeated
  many times just duplicates it. Local rendering is for a *single* file's
  quick sanity check.
- **Read the big layout files in wide slices and stay oriented.**
  Re-fetching `engine.py` / `fan_bundles.py` / `ordering.py` /
  `routing/*` twenty times over a session is the single largest cache-read
  cost. Read the region once, generously, and keep it in working context.
- **Default `[skip ci]` on work-in-progress pushes** (WIP snapshots, refactor
  passes). Let CI run on the final pre-review push - which this repo needs
  anyway, because the render-diff *is* the visual review. (A commit that
  fixes a known CI failure must re-run CI: no `[skip ci]` on those.)

### If your change touched `layout/routing/`: the gate-coverage ratchet

Adding, removing, or rewriting an `if`/`while` in a `layout/routing/`
module - or adding a topology fixture that closes a gap - can red one of
the three ratchet tests in `tests/test_routing_gate_coverage.py`. These
are **not** flaky; each names a specific reconciliation you owe in this
same PR. Do not silence them by hand-editing the baseline or the
generated matrix doc, and do not delete a triage entry just to make a
test pass.

- `test_no_new_un_exercised_routing_gate_arm` - your change added a gate
  with an un-exercised arm. Either author a fixture that hits both arms,
  or - if the arm is genuinely unreachable - confirm that and regenerate
  the baseline to acknowledge it.
- `test_gate_coverage_baseline_in_sync` - your change closed a gap or
  removed a gate the baseline still lists. Regenerate the baseline.
- `test_triage_sidecar_references_open_gaps` - you edited a gate's
  condition text or removed it, so its entry in
  `tests/data/routing_gate_triage.json` now names a non-gap. Prune (or
  re-key) that entry.

Regenerate with the coverage script (needs the `[dev]` extra and the
pinned interpreter):

```bash
python scripts/routing_gate_coverage.py --write   # rewrites the matrix doc + baseline
```

**Gotcha:** the arc model is CPython-version-specific, so these tests
**skip** off the pinned `BASELINE_PYTHON` (3.11). If your fix env is a
different Python you will not see the failure locally - it surfaces only
in CI. When in doubt, regenerate under 3.11. The full methodology (the
four verdicts, why these tests exist, the phantom-arc trap) is in
[`docs/dev/routing_gate_triage.md`](../../../docs/dev/routing_gate_triage.md);
for a dedicated triage campaign use the `nf-metro-gate-triage` skill.

### If you added a topology fixture: regenerate the guard-trace golden

Every fixture under `examples/topologies/` carries a committed guard-trace
golden at `tests/data/guard_golden/examples/topologies/<stem>.json` (the
ordered list of which guard fired at which stage). A **new** fixture has no
golden yet, so `tests/test_guard_registry_golden.py` reds with
"`<stem>.mmd` absent from the golden baseline". This is a **full-corpus**
gate: the targeted tests you run for the fix's own area never touch it, so
it only surfaces when you run that test (or the full suite) or on the push.
Run one of those before pushing rather than eating a red CI cycle.

Regenerate:

```bash
NF_METRO_REGEN_GUARD_GOLDEN=1 python tests/test_guard_registry_golden.py
```

Then **check the blast radius with `git status`**: only your new fixture's
`.json` should appear. A handful of guards sit on a float threshold and fire
differently across architectures, so if the regen also rewrites *other*
fixtures' goldens you are on arm64 committing an x86_64-divergent trace -
revert those and keep only the new file. CI checks the golden on x86_64, so
when a fixture's trace is genuinely arch-sensitive, regenerate on a Linux
x86_64 box (per the cross-architecture rule).

A new topology fixture therefore owes **three** committed artifacts, not
one: the `.mmd`, its `GALLERY_ENTRIES`/`gallery.yaml` row (Step 8, so the
render-diff sees it), and this guard-trace golden.

## Step 8: Visual Review via Render Preview

### Primary method: CI render preview (authoritative)

Push the branch and create a PR. The CI workflow
(`.github/workflows/pr-renders.yml`) automatically renders all gallery
examples on both the PR branch and base, generates a before/after visual
diff page, and posts a sticky comment on the PR with the preview link:

```
https://seqeralabs.github.io/nf-metro/_pr/<PR_NUMBER>/
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

### When the user *does* authorise a merge

Once the user says "merge" / "admin merge" for this PR, that word **is** the
authorisation - execute it, don't re-litigate. The recurring mistakes (this
is the single most-corrected behaviour across sessions) are all forms of
doing *too much*:

- **Merge with a merge commit, never squash:** `gh pr merge <N> --admin
  --merge --delete-branch`. Preserve the branch's commit history;
  `--squash` collapses it and is the wrong default here. (`--admin` bypasses
  only the review gate, not CI - it's fine once CI is green or the unverified
  delta is CI-irrelevant.) Omit `--delete-branch` if a child PR is based on
  this branch - deleting it auto-closes the child; retarget children first
  per Step 12.
- **Don't update the branch first.** If GitHub says "head branch is not up
  to date", do not `git merge origin/main` into it, do not push a commit, do
  not "refresh" it - all of these fire a full CI re-run the user is
  explicitly trying to avoid. The diff was already CI-validated; a trivial
  base-behind is CI-irrelevant, which is exactly what `--admin` is for.
- **Don't re-run or wait on fresh CI**, and cancel in-flight runs first if
  any (`gh run cancel`). "Merge" means merge now, then clean up (Step 12) -
  not "start another test cycle".

This is the `pinin4fjords:eco-merge` philosophy; that skill encapsulates the
"bypass the up-to-date requirement when the unverified delta is
CI-irrelevant" check if you want it. The self-initiation guardrail above
still holds: you never reach for admin-merge on your own; you execute it
cleanly *when told*.

### State the evidence for every "it's fixed" claim

Never assert a fix works without naming what proved it. Every "resolved" /
"this is fixed" / "renders correctly" claim must cite the **specific render
and the concrete numbers** it was checked against - the file, and the
coordinate or element that moved from the observed value to the target value
you wrote down in Step 3. "I believe it's resolved" with no named render is
not a verdict; it invites the reply "which render did you re-assess on?".

Two traps this closes:

- **"Didn't abort" / "the one invariant passes" is not "renders
  correctly".** Removing an abort can merely expose a poor layout the abort
  was masking. After any layout/routing fix, look at the full render (crop
  the region and read it) and run `probe_layout` + `inspect_layout` for the
  whole-layout picture (crossings, port alignment, column gaps), not only the
  invariant you targeted.
- **A clean render-diff verdict only covers the gallery corpus.** It says
  nothing about a NEW fixture that isn't in the gallery yet. Put new
  regression fixtures in `scripts/build_gallery.py` (`GALLERY_ENTRIES`), not
  only `examples/topologies/`, so CI's render-diff makes them visible to a
  human. A topologies-only or tests-only fixture is invisible in the PR
  preview.

Do not present a prototype as an improvement before the user has agreed it
is one. If you rendered it and it still has problems, say so and keep
working; don't defend a weak fix.

### Optional: quick local render of a single file

For a fast sanity check of one specific `.mmd` file before pushing:

```bash
source ~/.local/bin/mm-activate nf-metro-dev
export PYTHONPATH=/tmp/nf-metro-fix-<N>/src
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

The bar is "no **meaningful** visual regression", not pixel-identity. A
subtle spacing or coordinate shift that comes with a cleaner, more elegant
implementation is fine (classify it N or I); do not contort the code to
preserve a byte-identical render. Only a genuine degradation is a D.

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
gh pr create --repo seqeralabs/nf-metro --base main --title "<title>" --body "$(cat <<'EOF'
## Summary
<bullets describing the aggregate diff against main, no narrative>

Fixes #<N>

## Test plan
- [ ] pytest passes (including new invariant test)
- [ ] ruff check + ruff format clean on whole repo
- [ ] Runtime validator added (if applicable)
- [ ] Visual review of [render preview](https://seqeralabs.github.io/nf-metro/_pr/<PR_NUMBER>/)
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

An ordinary additive (fast-forward) push is **not** blocked by that hook -
only rewrites are. Don't mistake an unrelated push failure for a force-push
block, and don't ask the user to run a plain push you can run yourself.

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

1. The fix lands in `src/`, not in a doctored reproducer (Step 3), and the
   "it's fixed" claim cites the render + numbers that prove it (Step 8).
2. Commits are pushed.
3. Origin HEAD verified against local.
4. CI is green on the final commit.
5. Render-preview verdict is captured and gated on per Step 8.
6. PR description is standalone (per Step 10).

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

Leave the shared `nf-metro-dev` env in place - it is reused across issues
(Step 2), so there is nothing per-issue to remove.

Offer this cleanup to the user; only run it after they agree.

For shepherding a whole stacked chain of PRs back into `main` (rather
than a single issue fix), see `pr-chain-vet`.
