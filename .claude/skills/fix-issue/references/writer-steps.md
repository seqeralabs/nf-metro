# Writer steps

Steps 6 and 7, executed by the worktree's sole writer. Named in the writer's
brief; the coordinator does not need this file.

## Step 6: /simplify Pass

**Run this by default.** `/simplify` is worthwhile on anything beyond the
trivial, and "the diff is small" is not by itself a reason to skip it. Skip only
when **every** one of these holds, and say in one line that you did:

- the production diff adds no new function, class, branch, or code path;
- it does not touch a shared helper, a dispatch table, or anything with more
  than one caller;
- it is under roughly 20 lines of non-test production code;
- no later step (narrowing, lint, review) has added production code since.

A five-line change that introduces a branch, or threads an argument through
three call sites, is not trivial. If you are weighing whether it qualifies, it
does not: run the pass.

After the fix and tests are passing, hand off your candidate SHA and report it
ready for a simplify pass. **Do not try to invoke `/simplify` or spawn a
reviewer yourself** - the writer role carries no `Agent` tool, so this step
belongs to the coordinator. The coordinator assigns a fresh MID read-only
`fix-issue-simplifier` against your SHA; it invokes the `pinin4fjords:simplify`
skill and returns findings, proposed edits, and a pass/fail verdict without
writing anything. When the coordinator routes accepted suggestions back to
you, apply them and record them in a **separate** local candidate commit:

```
refactor: tighten <area> after fix for #<N>
```

Keeping `fix:` and `refactor:` commits separate makes the fix itself easy
to review and easy to revert in isolation if regressions surface.
The writer then hands off the exact candidate SHA, and an independent verifier
checks that SHA before it can be accepted.

**Re-running it later:** `/simplify` is expensive, so don't assign it after
every follow-up commit. Only re-run it on the final aggregate diff if later
steps (narrowing a regression, lint/mypy fixes) added a **substantial** chunk
of new production code the first pass never saw. A couple of small,
already-clean follow-up edits don't warrant a second pass.

## Step 7: Lint and Tests

The sole writer runs all mutation-capable formatting, fixing, regeneration,
and hook commands, resolves their changes, then creates the candidate commit.
Never skip hooks with `--no-verify`.

Assign a LIGHT independent read-only verifier the exact candidate SHA. It runs
the fixed command block in
[`environment.md`](environment.md) against a frozen
checkout, requires a clean tree before and after, keeps every cache and log in
the external artifact directory, and returns the command, exit status, concise
failure excerpt, and verdict. Route failures back to the writer, then verify the
new SHA.

**CI owns the full suite.** Targeted local tests plus the CI matrix is the
default, not a compromise: CI already runs the complete suite across the
supported matrix on every push, and duplicating it locally per branch or per
worker buys no signal. A `PreToolUse` hook
(`.claude/hooks/block-full-suite-pytest.sh`) denies a `pytest` command that
names no test file, so a bare `pytest`, `pytest tests/` or `pytest -n auto`
is refused; the three cases below are run with `# full-suite-ok` appended to
the command, which is the opt-out and the audit trail in one. Run a local full
suite only when it earns its cost:

- shared orchestration, parser model, dispatch table, or widely used helper
  changed;
- a targeted pass cannot cover a concrete wider-regression risk;
- explicit admin-merge preparation needs local full-suite confidence.

One exception is genuinely full-corpus by construction: a new topology
fixture's guard-trace golden and any hardcoded corpus-digest test, under
"Generated-artifact gates" below.

### Generated-artifact gates

Three distinct triggers, and the path is literal. The gate-coverage ratchet
scans `src/nf_metro/layout/routing/` **only** (`ROUTING_DIR` in
`scripts/routing_gate_coverage.py`, with branch coverage scoped to it), so it
trips on an `if`/`while` change inside that nested package, or on a new fixture
that closes a gap. A change elsewhere under `layout/` - `engine.py`, a phase
module, `ordering.py` - cannot trip it, so do not spend a worker checking
defensively. The guard-trace golden is separate and trips on a **new** fixture under any discovered root:
`tests/fixtures`, `tests/fixtures/topologies`, `examples`, `examples/topologies`,
`examples/guide` (see `_discover_fixtures` in `tests/test_engine_guards_perf.py`).
A test fixture placed under `tests/` reds it too. A third, easy-to-miss trigger
is any test that hardcodes a corpus-wide digest over the same fixture roots
(`tests/test_route_topology.py` is one) - it reds with no hint that a new
fixture, not a real regression, moved the digest, and a targeted selector for
just the new fixture or the touched module never runs it. When any of the
three fires, each failure names a specific reconciliation you owe in this same
PR. Do not hand-edit baselines to silence them. Procedure, verdicts, and the
Python 3.11 pinning gotcha, plus the corpus-digest check: [`gate-ratchet.md`](gate-ratchet.md).
