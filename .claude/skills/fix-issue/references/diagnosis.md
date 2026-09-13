# Diagnosis

Step 3 in full. Read before briefing the diagnostic worker.


Assign a read-only `fix-issue-diagnostician` at HIGH (or MID for a stated
single-site cause, see below) before the writer changes code. **Do not
propose fixes from hypotheses.** The worker must reproduce the symptom as a
falsifiable claim, in one of two forms.

**Geometry defects** (a bad render: overlap, kink, asymmetry, breeze-past,
bbox overflow). Tier HIGH:

1. Render the affected example(s) on the current `main` (the before-state).
2. Inspect the rendered SVG and actual coordinates or element attributes.
3. Restate the bug as "element X has property P=<observed>, expected
   P=<target>" - a concrete numeric claim.

**Non-geometry defects** (a plumbing, contract, API-surface, or exception-path
bug with no rendered symptom, possibly latent by the issue's own admission).
Tier HIGH, or MID when the issue already names the cause - see below:

1. Name every call site on the path from the caller to the defect.
2. Produce a failing observation that does not depend on geometry - a focused
   test, a monkeypatched upcall, an asserted argument value.
3. Restate the bug as "call site X receives/passes V=<observed>, expected
   V=<target>" - a concrete structural claim.

Either way, return blocked if the worker cannot state the claim yet; diagnosis
must continue before implementation. Only after the claim is pinned down may the
worker reason about which pass or function produced it.

**"Not a bug" is a first-class outcome.** Diagnosis may conclude that the
reported behaviour is correct, or acceptable, and that the issue should be
closed. Require the same rigour for that verdict as for a positive one: a
concrete claim, in numbers or call sites, showing what the code actually does
and why it is acceptable. Report it to the user with that evidence and ask for
authority to close, recording the reasoning in the issue. Never close an issue
unasked, and never manufacture a defect because a fix was expected. A wrong
"no bug" wastes the run exactly like a wrong "bug", so this outcome goes through
the post-diagnosis gate unchanged.

The post-diagnosis gate challenges **whether** there is a defect at all, not
only which kind, plus the claim behind whichever verdict came back. For an
engine-bug (b) verdict, it also challenges the recommended fix **path**, not
only the root-cause claim: a render-time correction that reconciles two
already-computed, disagreeing values is not the same thing as fixing the
assignment that made them disagree, and a diagnostician under pressure to
produce *a* fix will reach for the former even when the stated "unavoidable"
reason for skipping plan-time doesn't hold up. Passing invariants and a clean
render are not evidence the path is right - re-derive whether the disagreement
could have been prevented one step further upstream before accepting it.

**When the issue already states its own root cause** (it names the function,
the call site, and the acceptance bar), do not re-derive it from scratch at HIGH
cost. Brief a MID worker to *confirm or refute that specific claim* against
current `origin/main` and produce the failing observation. Independent
confirmation is still mandatory - taking the issue's word for it is not
diagnosis - but confirming a stated cause is bounded work, not open-ended
judgment.

This carve-out covers a **single-site** claim. If confirming it requires
surveying every caller of a function, or choosing between two valid designs,
that is open-ended judgment: the worker returns blocked naming the options in
its `DECIDE` field, and the work re-routes to HIGH. A stated cause bounds
*where to look*, not necessarily *how much there is to decide*.

Don't ask the user to pick a magnitude/spacing `DECIDE` (a padding, a margin)
blind before a render exists - implement the cheaper default, get a render in
front of them, let inspection set the number. A number chosen sight-unseen is
what a later visual-review round overturns anyway.

### Check your premise against current `origin/main` first

Diagnose against latest remote, not a stale tree. The coordinator fetches
`origin/main`; the diagnostic worker confirms the bug **still reproduces on
that exact SHA** before reasoning about a cause - a sibling PR may already have
fixed it or changed the very code you're reading. If the user says something is
already addressed, re-fetch and look again before disagreeing; "I'm looking at
outdated code" is a recurring wrong turn. If a related PR merges mid-session,
first require the writer to hand off a clean, committed candidate. The
coordinator may then serialize a base-merge assignment to that sole writer. Keep
conflicts and required edits with the writer. Assign re-diagnosis on the
resulting candidate SHA.

### Classify: authoring mistake, engine bug, or structural defect?

Require the diagnostic worker to decide which of three things it is looking at:

- **(a) An mmd authoring mistake** - the `.mmd` misdescribes the pipeline
  (wrong line on a station, a missing edge, a bad directive). The fix *is* to
  edit the input. `probe_layout.py` labels many of these ("authoring
  mistakes vs engine bugs"); `nf-metro explain` shows the rule each inferred
  decision followed.
- **(b) An engine bug on correct mmd** - the input faithfully describes the
  pipeline and the *engine* lays it out badly. The fix goes in `src/`
  (layout / routing / parser). The reproducing `.mmd` stays untouched.
- **(c) A structural defect independent of any input** - a plumbing, contract,
  API-surface, or exception-path bug where the `.mmd` is irrelevant and no
  render is wrong yet. The fix goes in `src/` at the named call sites. There is
  no reproducer to freeze and no geometry to classify; do not force this into
  (a) or (b), and do not manufacture a numeric claim for it.

- **(d) No defect** - the behaviour is correct or acceptable as it stands. See
  "Not a bug" above; this ends in a report and a close request, not a writer
  brief.

Record which one it is - in numbers for (a), (b) and (d), in named call sites
for (c) - before briefing the writer.

**Default to a plan-time fix for (b); a render-time post-hoc correction needs
a stated reason it's unavoidable, not just easier.** This is not a one-time
briefing note - it is the standard to hold every later decision point to as
well: a writer choosing between two implementations, a `/simplify` pass, and
both mandatory gates should all prefer whichever option fixes the value at the
point it is first assigned over one that patches a downstream symptom, even
when the patch is smaller or the symptom's fix already looks clean. Before briefing the
writer, check for an active architecture programme covering this defect class
(grep open issues, stale branches). Brief the writer for plan-time unless a
concrete blocker rules it out this run (an unstarted dependency, a
stale/conflicting branch, a data-flow inversion needing its own PR) - name it
in the handoff. File a tracking issue if the gap is untracked.

### Once it's an engine bug, the reproducer is frozen evidence

**Never "fix" an engine bug by editing the input to dodge the bad layout.**
Do not trim labels, remove or reorder stations or lines, split sections, or add
directives to avoid the bad path. This applies to existing and newly authored
fixtures. Legitimate input edits are a faithful new reproducer or correction of
the diagnosed authoring mistake. Treat any additional bad render as fallout.
Step 9 narrowing must gate code on a structural precondition, never reword the
input. If the writer proposes an input workaround during an engine fix, stop
and route the evidence back through diagnosis.

### Two falsification traps

Confirming the fix against a reduced repro that clears the *first* violation
is not falsification if the issue's repro names more than one - a second,
unrelated defect can sit behind the first (often an authoring mistake in the
reporter's own `.mmd`) and only shows up when the post-diagnosis gate reruns
the literal repro. Test against the complete repro, not a reduced one.

A rendered-corpus zero-diff shows the change moved no pixel; it does not show
the change is *right* if the corpus has no case where the changed decision
could actually disagree with itself (e.g. a single-line bundle has no order
to violate, so reclassifying its seam is inert). Check the corpus contains a
case that could reveal a wrong answer, or build one, before citing zero-diff
as safety evidence.

### Diagnostic tooling

The repo bundles two scripts that do exactly this render-and-read-the-numbers
work, usable for **any** layout issue regardless of how it was reported:

```bash
# Validator/crash/guard verdict: parse -> layout -> validate -> route, with
# findings split into authoring mistakes vs engine bugs.
PYTHONPATH=/tmp/nf-metro-fix-<N>/src \
  python .claude/skills/nf-metro-stress-render/scripts/probe_layout.py <file.mmd> --json
# Per-section station coordinates, flagging stations off their section trunk,
# off-track in/outputs far from their consumer, and oversized inter-row gaps.
PYTHONPATH=/tmp/nf-metro-fix-<N>/src \
  python .claude/skills/nf-metro-stress-render/scripts/inspect_layout.py <file.mmd>
```

Plus `PYTHONPATH=<worktree>/src python -m nf_metro explain <file.mmd>` (the rule
behind each inferred layout decision) and `PYTHONPATH=<worktree>/src python -m nf_metro info --json <file.mmd>` (the
structural model; the file argument is required). **`PYTHONPATH` is not optional on any of these.** Without it
they raise `ModuleNotFoundError`; worse, once the env has a non-editable install
they silently diagnose the *installed* snapshot instead of the worktree under
test, and the numeric claim the whole run rests on is then about the wrong code.
Use `python -m nf_metro`, not the bare `nf-metro` entry point, for the same
reason. These are
conveniences, not requirements - any way you pin the bug to numbers is fine.

If the issue happens to have been filed by the `nf-metro-stress-render` skill,
it carries a correct-by-construction repro `.mmd` in a `<details>` fold in the
issue body - start from that rather than re-deriving one. Most issues won't have
this; otherwise the same HIGH diagnostician builds a faithful reproducer.

Either way **the diagnostician materialises it itself**, with a Bash heredoc into
the artifact directory its brief names (`$ART/repro.mmd`), and reports that path.
A folded reproducer is text in an issue body: the coordinator must not absorb it,
a handoff must not carry file contents, and the read-only roles hold no `Write`
tool - so writing it via `Bash` is the route, and it is this worker's job.
